from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from tmux_core.runtime.grill import (
    BUNDLE_COMMIT as GRILL_BUNDLE_COMMIT,
    BEGIN_MARKER as GRILL_BEGIN_MARKER,
    GrillBundleError,
    GrillTurnProfile,
    build_grill_bootstrap,
)
from tmux_core.runtime.ponytail import BEGIN_MARKER as PONYTAIL_BEGIN_MARKER
from tmux_core.runtime.tmux_runtime import (
    AgentRunConfig,
    GrillSessionModeMismatch,
    PromptSubmissionRejectedError,
    TmuxBatchWorker,
    TmuxMutationOutcomeUnknown,
    TurnFileContract,
)


def _resolution(vendor: str, model: str, effort: str = "high") -> SimpleNamespace:
    return SimpleNamespace(
        resolved_model=model,
        resolved_variant="",
        reasoning_control_mode="implicit_default",
        catalog_source_kind="test",
        confidence="high",
        native_reasoning_level=effort,
        normalized_effort=effort,
        supports_reasoning=True,
        notes=(),
        executable_path=f"/test/bin/{vendor}",
    )


def _config(
    vendor: str = "codex",
    *,
    requirements_mode: str = "standard",
    ponytail_mode: str = "off",
) -> AgentRunConfig:
    with mock.patch(
        "tmux_core.runtime.tmux_runtime.resolve_launch",
        return_value=_resolution(vendor, "model"),
    ):
        return AgentRunConfig(
            vendor=vendor,
            model="model",
            requirements_mode=requirements_mode,
            ponytail_mode=ponytail_mode,
        )


def _prompt_worker(mode: str, *, ponytail_mode: str = "off") -> TmuxBatchWorker:
    worker = object.__new__(TmuxBatchWorker)
    worker.config = SimpleNamespace(requirements_mode=mode, ponytail_mode=ponytail_mode)
    worker.requirements_mode = mode
    worker.ponytail_mode = ponytail_mode
    worker.ponytail_full_delivered = False
    worker.ponytail_delivered_mode = ""
    worker.grill_session_generation = "generation"
    worker.grill_full_delivered = False
    worker.grill_delivered_mode = ""
    worker.grill_question_seq = 0
    worker.session_name = "test-session"
    return worker


def _build_prompt(
    worker: TmuxBatchWorker,
    *,
    profile: GrillTurnProfile | None,
    prompt: str = "Do the task",
) -> str:
    return worker._build_turn_prompt(  # noqa: SLF001
        prompt,
        "[[DONE]]",
        (),
        task_status_path=Path("status.json"),
        include_turn_protocol=True,
        grill_profile=profile,
    )


def test_agent_run_config_default_and_summary_are_backward_compatible() -> None:
    config = _config()
    assert config.requirements_mode == "standard"
    assert config.to_summary()["requirements_mode"] == "standard"
    with pytest.raises(ValueError, match="expected one of"):
        _config(requirements_mode="automatic")


@pytest.mark.parametrize("vendor", ("codex", "claude", "gemini", "opencode", "mimo", "agy", "deveco"))
def test_all_seven_vendors_receive_the_same_vendor_neutral_grill_prompt(vendor: str) -> None:
    config = _config(vendor, requirements_mode="grill")
    worker = _prompt_worker(config.requirements_mode)
    worker.config = config
    prompt = _build_prompt(worker, profile=GrillTurnProfile("grill", 1))

    assert prompt.startswith(GRILL_BEGIN_MARKER)
    assert "Ask exactly one decision question per turn" in prompt
    assert prompt.index(GRILL_BEGIN_MARKER) < prompt.index("Do the task") < prompt.index("Turn completion protocol")


def test_prompt_order_is_ponytail_then_grill_then_business_then_protocol() -> None:
    worker = _prompt_worker("grill-with-docs", ponytail_mode="full")
    prompt = _build_prompt(worker, profile=GrillTurnProfile("grill-with-docs", 2))

    assert prompt.index(PONYTAIL_BEGIN_MARKER) < prompt.index(GRILL_BEGIN_MARKER)
    assert prompt.index(GRILL_BEGIN_MARKER) < prompt.index("Do the task")
    assert prompt.index("Do the task") < prompt.index("Turn completion protocol")


def test_first_grill_turn_bootstraps_and_confirmed_next_turn_uses_reminder() -> None:
    worker = _prompt_worker("grill")
    first = _build_prompt(worker, profile=GrillTurnProfile("grill", 1))
    assert "GRILL REQUIREMENTS MODE ACTIVE" in first

    worker._mark_grill_prompt_delivered(first)  # noqa: SLF001
    assert worker.grill_full_delivered is True
    assert worker.grill_question_seq == 1
    second = _build_prompt(worker, profile=GrillTurnProfile("grill", 2), prompt="NEXT")
    assert "GRILL PROFILE REMINDER" in second
    assert "question_seq: 2" in second


def test_grill_is_never_injected_without_an_explicit_turn_profile() -> None:
    worker = _prompt_worker("grill")
    prompt = _build_prompt(worker, profile=None)
    assert GRILL_BEGIN_MARKER not in prompt
    assert prompt.startswith("Do the task")


def test_turn_profile_must_match_worker_mode() -> None:
    worker = _prompt_worker("grill")
    with pytest.raises(GrillSessionModeMismatch, match="configured=grill, turn=grill-with-docs"):
        _build_prompt(worker, profile=GrillTurnProfile("grill-with-docs", 1))


def test_corrupt_enabled_bundle_fails_before_any_tmux_probe(tmp_path: Path) -> None:
    backend = mock.Mock()
    with mock.patch(
        "tmux_core.runtime.tmux_runtime.validate_grill_bundle",
        side_effect=GrillBundleError("bundle corrupt"),
    ):
        with pytest.raises(GrillBundleError, match="bundle corrupt"):
            TmuxBatchWorker(
                worker_id="worker",
                work_dir=tmp_path,
                config=_config(requirements_mode="grill"),
                runtime_root=tmp_path / "runtime",
                backend=backend,
            )
    backend.assert_not_called()


def test_delivery_latch_rolls_back_when_policy_persistence_fails() -> None:
    worker = _prompt_worker("grill")
    prompt = _build_prompt(worker, profile=GrillTurnProfile("grill", 1))
    worker._persist_grill_policy_fast = mock.Mock(side_effect=OSError("state failed"))  # type: ignore[method-assign]

    with pytest.raises(OSError, match="state failed"):
        worker._confirm_grill_prompt_delivery(prompt)  # noqa: SLF001
    assert worker.grill_full_delivered is False
    assert worker.grill_delivered_mode == ""
    assert worker.grill_question_seq == 0


def test_recovered_contract_only_latches_the_bundle_that_created_its_prompt() -> None:
    current = _prompt_worker("grill")
    current._persist_grill_policy_fast = mock.Mock()  # type: ignore[method-assign]
    current._confirm_grill_profile_delivery(  # noqa: SLF001
        GrillTurnProfile("grill", 1),
        delivery_bundle_commit=GRILL_BUNDLE_COMMIT,
    )
    assert current.grill_full_delivered is True
    assert current.grill_delivered_mode == "grill"

    stale = _prompt_worker("grill")
    stale._persist_grill_policy_fast = mock.Mock()  # type: ignore[method-assign]
    stale._confirm_grill_profile_delivery(  # noqa: SLF001
        GrillTurnProfile("grill", 1),
        delivery_bundle_commit="older-upstream-commit",
    )
    assert stale.grill_full_delivered is False
    assert stale.grill_delivered_mode == ""
    assert stale.grill_question_seq == 1


def test_run_turn_latches_full_rules_only_after_reply_evidence(tmp_path: Path) -> None:
    worker = _prompt_worker("grill")
    task_status_path = tmp_path / "task.status.json"
    result_path = tmp_path / "task.result.json"
    worker.current_task_runtime_status = ""
    worker.current_task_status_path = ""
    worker.current_task_result_path = ""
    worker.current_command = "codex"
    worker.current_path = str(tmp_path)
    worker.last_reply = ""
    worker.last_pane_title = ""
    worker.health_supervisor = None
    worker.results = []
    worker.read_state = mock.Mock(return_value={})  # type: ignore[method-assign]
    worker._build_task_status_path = mock.Mock(return_value=task_status_path)  # type: ignore[method-assign]
    worker._build_task_result_path = mock.Mock(return_value=result_path)  # type: ignore[method-assign]
    worker._append_transcript = mock.Mock()  # type: ignore[method-assign]
    worker._write_state = mock.Mock()  # type: ignore[method-assign]
    worker._log_event = mock.Mock()  # type: ignore[method-assign]
    worker._ensure_agent_ready_for_turn_start = mock.Mock()  # type: ignore[method-assign]
    worker._handle_runtime_intervention_if_needed = mock.Mock(return_value=False)  # type: ignore[method-assign]
    worker._business_monotonic = mock.Mock(return_value=0.0)  # type: ignore[method-assign]
    worker.observe = mock.Mock(  # type: ignore[method-assign]
        return_value=SimpleNamespace(visible_text="", raw_log_tail="")
    )
    worker._title_indicates_ready = mock.Mock(return_value=False)  # type: ignore[method-assign]
    worker._persist_grill_policy_fast = mock.Mock()  # type: ignore[method-assign]

    def send_without_submission_evidence(_prompt: str) -> None:
        assert worker.grill_full_delivered is False

    def complete_reply_contract(**_kwargs: object) -> str:
        # Returning from tmux paste/send is not enough to consume the full
        # bootstrap.  The reply is the first authoritative evidence here.
        assert worker.grill_full_delivered is False
        return "done"

    worker._send_text = mock.Mock(side_effect=send_without_submission_evidence)  # type: ignore[method-assign]
    worker._wait_for_turn_reply = mock.Mock(side_effect=complete_reply_contract)  # type: ignore[method-assign]

    result = worker._run_turn_impl(  # noqa: SLF001
        label="grill-question",
        prompt="Ask one question",
        timeout_sec=1.0,
        grill_profile=GrillTurnProfile("grill", 1),
    )

    assert result.clean_output == "done"
    assert worker.grill_full_delivered is True
    worker._persist_grill_policy_fast.assert_called_once()


def test_submission_unknown_clears_only_a_matching_full_bootstrap() -> None:
    worker = _prompt_worker("grill")
    full = _build_prompt(worker, profile=GrillTurnProfile("grill", 1))
    worker._mark_grill_prompt_delivered(full)  # noqa: SLF001
    worker._write_state = mock.Mock()  # type: ignore[method-assign]
    worker._log_event = mock.Mock()  # type: ignore[method-assign]
    worker.turn_state = SimpleNamespace(value="submitted")

    worker._record_prompt_submission_unconfirmed(  # noqa: SLF001
        label="task",
        error=RuntimeError("not confirmed"),
        timeout_sec=1.0,
        submitted_prompt=full,
    )
    assert worker.grill_full_delivered is False
    assert worker.grill_delivered_mode == ""


def test_rejected_submission_does_not_consume_full_bootstrap() -> None:
    worker = _prompt_worker("grill")
    full = _build_prompt(worker, profile=GrillTurnProfile("grill", 1))
    worker._mark_grill_prompt_delivered(full)  # noqa: SLF001
    worker._write_state = mock.Mock()  # type: ignore[method-assign]
    worker._log_event = mock.Mock()  # type: ignore[method-assign]

    worker._record_prompt_submission_rejected(  # noqa: SLF001
        label="task",
        error=PromptSubmissionRejectedError("no_active_thread"),
        timeout_sec=1.0,
        submitted_prompt=full,
    )

    assert worker.grill_full_delivered is False
    assert worker.grill_delivered_mode == ""


def test_forged_or_wrong_mode_block_cannot_latch_current_mode() -> None:
    worker = _prompt_worker("grill")
    worker._mark_grill_prompt_delivered(build_grill_bootstrap(GrillTurnProfile("grill-with-docs", 1), "TASK"))  # noqa: SLF001
    worker._mark_grill_prompt_delivered(  # noqa: SLF001
        f"{GRILL_BEGIN_MARKER}\nGRILL PROFILE — mode: grill; question_seq: 1\nforged"
    )
    assert worker.grill_full_delivered is False
    assert worker.grill_delivered_mode == ""


def test_existing_session_mode_mismatch_is_rejected_and_legacy_means_standard(tmp_path: Path) -> None:
    backend = mock.Mock()
    backend.control_state.return_value = {}
    runtime_dir = tmp_path / "runtime" / "worker-existing"
    runtime_dir.mkdir(parents=True)
    (runtime_dir / "worker.state.json").write_text(
        '{"session_name":"legacy-session","config":{"vendor":"codex","model":"model"}}',
        encoding="utf-8",
    )

    with pytest.raises(GrillSessionModeMismatch, match="existing=standard, requested=grill"):
        TmuxBatchWorker(
            worker_id="worker",
            work_dir=tmp_path,
            config=_config(requirements_mode="grill"),
            runtime_root=tmp_path / "runtime",
            existing_runtime_dir=runtime_dir,
            existing_session_name="legacy-session",
            backend=backend,
        )


def test_grill_policy_survives_resume_and_resets_for_new_session_generation(tmp_path: Path) -> None:
    backend = mock.Mock()
    backend.control_state.return_value = {}
    runtime_dir = tmp_path / "runtime" / "worker-existing"
    config = _config(requirements_mode="grill")
    worker = TmuxBatchWorker(
        worker_id="worker",
        work_dir=tmp_path,
        config=config,
        runtime_root=tmp_path / "runtime",
        existing_runtime_dir=runtime_dir,
        existing_session_name="same-session",
        backend=backend,
    )
    first = _build_prompt(worker, profile=GrillTurnProfile("grill", 1))
    initial_generation = worker.grill_session_generation
    worker._confirm_grill_prompt_delivery(first)  # noqa: SLF001

    resumed = TmuxBatchWorker(
        worker_id="worker",
        work_dir=tmp_path,
        config=config,
        runtime_root=tmp_path / "runtime",
        existing_runtime_dir=runtime_dir,
        existing_session_name="same-session",
        backend=backend,
    )
    assert resumed.grill_session_generation == initial_generation
    assert resumed.grill_full_delivered is True
    assert "GRILL PROFILE REMINDER" in _build_prompt(
        resumed,
        profile=GrillTurnProfile("grill", 2),
    )

    resumed._reset_grill_delivery_generation()  # noqa: SLF001
    assert resumed.grill_session_generation != initial_generation
    assert resumed.grill_full_delivered is False
    assert "GRILL REQUIREMENTS MODE ACTIVE" in _build_prompt(
        resumed,
        profile=GrillTurnProfile("grill", 1),
    )


def test_unknown_resume_does_not_materialize_contract_from_stale_output(tmp_path: Path) -> None:
    worker = _prompt_worker("grill")
    turn_status_path = tmp_path / "missing-turn-status.json"
    validator = mock.Mock(side_effect=AssertionError("missing persisted status is not completion proof"))
    contract = TurnFileContract(
        turn_id="requirements_clarification_1",
        phase="requirements_clarification",
        status_path=turn_status_path,
        validator=validator,
        quiet_window_sec=0.0,
    )
    worker._runtime_intervention_handler = None
    worker.read_state = mock.Mock(  # type: ignore[method-assign]
        return_value={
            "current_turn_id": contract.turn_id,
            "current_turn_status_path": str(turn_status_path.resolve()),
            "turn_state": "submission_unknown",
            "dispatch_state": "submission_unknown",
            "current_task_status_path": "",
        }
    )
    worker.wait_for_turn_artifacts = mock.Mock(  # type: ignore[method-assign]
        side_effect=RuntimeError("no BUSY, echo, or contract proof")
    )

    with pytest.raises(TmuxMutationOutcomeUnknown, match="outcome unknown"):
        worker.resume_completion_turn(
            label="requirements_clarification_round_1",
            completion_contract=contract,
            timeout_sec=1.0,
            submission_cursor="submission_unknown",
            grill_profile=GrillTurnProfile("grill", 0),
        )

    validator.assert_not_called()
    worker.wait_for_turn_artifacts.assert_called_once()


def test_replacement_resume_validates_persisted_stage_contract_before_replay(
    tmp_path: Path,
) -> None:
    worker = _prompt_worker("grill")
    turn_status_path = tmp_path / "missing-turn-status.json"
    stage_status_path = tmp_path / "requirements-status.json"
    stage_status_path.write_text("{}", encoding="utf-8")
    file_result = SimpleNamespace(
        status_path=str(turn_status_path),
        payload={"status": "completed"},
        artifact_paths={},
        artifact_hashes={},
    )
    validator = mock.Mock(return_value=file_result)
    contract = TurnFileContract(
        turn_id="requirements_clarification_1",
        phase="requirements_clarification",
        status_path=turn_status_path,
        validator=validator,
        quiet_window_sec=0.0,
    )
    worker.read_state = mock.Mock(return_value={})  # type: ignore[method-assign]
    worker._record_resumed_completion_turn = mock.Mock(  # type: ignore[method-assign]  # noqa: SLF001
        return_value="completed-without-replay"
    )
    worker.wait_for_turn_artifacts = mock.Mock(  # type: ignore[method-assign]
        side_effect=AssertionError("a persisted stage contract must be checked first")
    )

    result = worker.resume_completion_turn(
        label="requirements_clarification_round_1",
        completion_contract=contract,
        timeout_sec=1.0,
        submission_cursor="not_started",
        stage_status_path=stage_status_path,
    )

    assert result == "completed-without-replay"
    validator.assert_called_once_with(turn_status_path.resolve())
    worker.wait_for_turn_artifacts.assert_not_called()
