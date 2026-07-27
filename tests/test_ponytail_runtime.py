from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from tmux_core.runtime.ponytail import BEGIN_MARKER, PonytailBundleError, build_ponytail_bootstrap
from tmux_core.runtime.tmux_runtime import AgentRunConfig, PonytailSessionModeMismatch, TmuxBatchWorker


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


def _config(vendor: str, *, ponytail_mode: str = "off") -> AgentRunConfig:
    with mock.patch(
        "tmux_core.runtime.tmux_runtime.resolve_launch",
        return_value=_resolution(vendor, "model"),
    ):
        return AgentRunConfig(vendor=vendor, model="model", ponytail_mode=ponytail_mode)


def _prompt_worker(mode: str) -> TmuxBatchWorker:
    worker = object.__new__(TmuxBatchWorker)
    worker.config = SimpleNamespace(ponytail_mode=mode)
    worker.ponytail_full_delivered = False
    worker.ponytail_delivered_mode = ""
    return worker


def _build_prompt(worker: TmuxBatchWorker, prompt: str = "Do the task") -> str:
    return worker._build_turn_prompt(  # noqa: SLF001
        prompt,
        "[[DONE]]",
        (),
        task_status_path=Path("status.json"),
        include_turn_protocol=True,
    )


def test_agent_run_config_keeps_low_level_default_off_and_rejects_invalid_mode() -> None:
    assert _config("codex").ponytail_mode == "off"
    with pytest.raises(ValueError, match="expected one of"):
        _config("codex", ponytail_mode="unknown")


@pytest.mark.parametrize("vendor", ("codex", "claude", "gemini", "opencode", "mimo", "agy", "deveco"))
def test_all_managed_vendor_commands_disable_native_ponytail(vendor: str) -> None:
    command = _config(vendor, ponytail_mode="full").build_launch_command(Path("/tmp/project"))
    assert command.startswith("env PONYTAIL_DEFAULT_MODE=off ")
    if vendor == "codex":
        assert " --disable hooks " in command
        assert "--dangerously-bypass-hook-trust" not in command
    if vendor in {"opencode", "mimo", "deveco"}:
        assert " --pure " in command
    if vendor == "deveco":
        assert "DEVECO_DISABLE_AUTOUPDATE=1" in command


def test_first_turn_bootstraps_then_subsequent_turn_uses_self_contained_reminder() -> None:
    worker = _prompt_worker("full")

    first = _build_prompt(worker)
    assert first.count(BEGIN_MARKER) == 1
    assert "PONYTAIL MODE ACTIVE" in first
    assert first.index(BEGIN_MARKER) < first.index("Do the task") < first.index("Turn completion protocol")

    worker._mark_ponytail_bootstrap_delivered(first)  # noqa: SLF001
    second = _build_prompt(worker, "Do the next task")
    assert second.count(BEGIN_MARKER) == 1
    assert "PONYTAIL REMINDER" in second
    assert "YAGNI → existing code → stdlib" in second


def test_off_mode_does_not_add_ponytail_context() -> None:
    prompt = _build_prompt(_prompt_worker("off"))
    assert BEGIN_MARKER not in prompt
    assert prompt.startswith("Do the task")


def test_corrupt_enabled_bundle_fails_before_any_tmux_probe(tmp_path: Path) -> None:
    backend = mock.Mock()
    with mock.patch(
        "tmux_core.runtime.tmux_runtime.validate_ponytail_bundle",
        side_effect=PonytailBundleError("bundle corrupt"),
    ):
        with pytest.raises(PonytailBundleError, match="bundle corrupt"):
            TmuxBatchWorker(
                worker_id="worker",
                work_dir=tmp_path,
                config=_config("codex", ponytail_mode="full"),
                runtime_root=tmp_path / "runtime",
                backend=backend,
            )

    backend.assert_not_called()


def test_delivery_latch_rolls_back_when_policy_persistence_fails() -> None:
    worker = _prompt_worker("full")
    prompt = _build_prompt(worker)
    worker._persist_ponytail_policy_fast = mock.Mock(side_effect=OSError("state write failed"))  # type: ignore[method-assign]

    with pytest.raises(OSError, match="state write failed"):
        worker._confirm_ponytail_bootstrap_delivery(prompt)  # noqa: SLF001

    assert worker.ponytail_full_delivered is False
    assert worker.ponytail_delivered_mode == ""


def test_submission_unknown_clears_only_the_matching_full_bootstrap() -> None:
    worker = _prompt_worker("full")
    prompt = _build_prompt(worker)
    worker.ponytail_full_delivered = True
    worker.ponytail_delivered_mode = "full"
    worker._write_state = mock.Mock()  # type: ignore[method-assign]
    worker._log_event = mock.Mock()  # type: ignore[method-assign]

    worker._record_prompt_submission_unconfirmed(  # noqa: SLF001
        label="task",
        error=RuntimeError("not confirmed"),
        timeout_sec=1.0,
        submitted_prompt=prompt,
    )

    assert worker.ponytail_full_delivered is False
    assert worker.ponytail_delivered_mode == ""


def test_wrong_mode_or_forged_block_cannot_latch_current_mode() -> None:
    worker = _prompt_worker("full")
    worker._mark_ponytail_bootstrap_delivered(build_ponytail_bootstrap("ultra", "TASK"))  # noqa: SLF001
    worker._mark_ponytail_bootstrap_delivered(  # noqa: SLF001
        f"{BEGIN_MARKER}\nPONYTAIL MODE ACTIVE — level: full\nTASK"
    )

    assert worker.ponytail_full_delivered is False
    assert worker.ponytail_delivered_mode == ""


def test_existing_session_mode_mismatch_is_rejected_and_legacy_state_means_off(tmp_path: Path) -> None:
    backend = mock.Mock()
    backend.control_state.return_value = {}
    runtime_dir = tmp_path / "runtime" / "worker-existing"
    runtime_dir.mkdir(parents=True)
    state_path = runtime_dir / "worker.state.json"
    state_path.write_text(
        '{"session_name":"legacy-session","config":{"vendor":"codex","model":"model"}}',
        encoding="utf-8",
    )

    with pytest.raises(PonytailSessionModeMismatch, match="existing=off, requested=full"):
        TmuxBatchWorker(
            worker_id="worker",
            work_dir=tmp_path,
            config=_config("codex", ponytail_mode="full"),
            runtime_root=tmp_path / "runtime",
            existing_runtime_dir=runtime_dir,
            existing_session_name="legacy-session",
            backend=backend,
        )


def test_delivery_policy_survives_same_session_resume_and_resets_for_new_generation(tmp_path: Path) -> None:
    backend = mock.Mock()
    backend.control_state.return_value = {}
    runtime_dir = tmp_path / "runtime" / "worker-existing"
    config = _config("codex", ponytail_mode="full")
    worker = TmuxBatchWorker(
        worker_id="worker",
        work_dir=tmp_path,
        config=config,
        runtime_root=tmp_path / "runtime",
        existing_runtime_dir=runtime_dir,
        existing_session_name="same-session",
        backend=backend,
    )
    first_prompt = _build_prompt(worker)
    initial_generation = worker.ponytail_session_generation
    worker._confirm_ponytail_bootstrap_delivery(first_prompt)  # noqa: SLF001

    resumed = TmuxBatchWorker(
        worker_id="worker",
        work_dir=tmp_path,
        config=config,
        runtime_root=tmp_path / "runtime",
        existing_runtime_dir=runtime_dir,
        existing_session_name="same-session",
        backend=backend,
    )
    resumed_prompt = _build_prompt(resumed, "NEXT")
    assert resumed.ponytail_session_generation == initial_generation
    assert resumed.ponytail_full_delivered is True
    assert "PONYTAIL REMINDER" in resumed_prompt

    resumed._reset_ponytail_delivery_generation()  # noqa: SLF001
    restarted_prompt = _build_prompt(resumed, "AFTER RESTART")
    assert resumed.ponytail_session_generation != initial_generation
    assert resumed.ponytail_full_delivered is False
    assert "PONYTAIL MODE ACTIVE" in restarted_prompt
