from __future__ import annotations

import argparse
import json
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

import A01_Routing_LayerPlanning as routing
import A02_RequirementIntake as intake
import A03_RequirementsClarification as clarification
from T03_agent_init_workflow import RunManifest, RunStore
from tmux_core.runtime import graphify as graphify_runtime
from tmux_core.runtime.contracts import TurnFileContract, TurnFileResult
from tmux_core.runtime.graphify import (
    GRAPHIFY_FULL_GUIDE_MARKER,
    GRAPHIFY_GUIDE_VERSION,
    GraphifyEvidence,
    GraphifyMode,
    GraphifyQueryIntent,
    GraphifyQueryResult,
    GraphifyState,
    GraphifyStatus,
    GraphifyTurnContext,
    GraphifyTurnProfile,
    GraphifyUnavailable,
    graphify_required_recovery_decision,
    publish_graphify_pending_status,
    read_graphify_project_status,
    set_graphify_required_recovery_decision,
)
from tmux_core.runtime.grill import BEGIN_MARKER as GRILL_BEGIN_MARKER, GrillTurnProfile
from tmux_core.runtime.ponytail import BEGIN_MARKER as PONYTAIL_BEGIN_MARKER
from tmux_core.runtime.tmux_runtime import (
    AgentRunConfig,
    AgentRuntimeState,
    TmuxBatchWorker,
    WorkerStatus,
)
from tmux_core.stage_kernel import detailed_design, development, overall_review, requirements_review, task_split
from tmux_core.stage_kernel.shared_review import (
    ReviewAgentSelection,
    parse_agent_selection_spec,
    resolve_stage_agent_config,
    resolve_workflow_graphify_mode,
)
from tmux_core.stage_kernel import shared_review


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


def _config(vendor: str = "codex", *, graphify_mode: str = "off") -> AgentRunConfig:
    with mock.patch(
        "tmux_core.runtime.tmux_runtime.resolve_launch",
        return_value=_resolution(vendor, "model"),
    ):
        return AgentRunConfig(vendor=vendor, model="model", graphify_mode=graphify_mode)


def _prompt_worker() -> TmuxBatchWorker:
    worker = object.__new__(TmuxBatchWorker)
    worker.config = SimpleNamespace(
        ponytail_mode="full",
        requirements_mode="grill",
        graphify_mode="auto",
    )
    worker.ponytail_mode = "full"
    worker.ponytail_full_delivered = False
    worker.ponytail_delivered_mode = ""
    worker.requirements_mode = "grill"
    worker.grill_full_delivered = False
    worker.grill_delivered_mode = ""
    worker.grill_question_seq = 0
    worker.graphify_mode = "auto"
    worker.graphify_session_generation = "graphify-session"
    worker.graphify_orientation_delivered = False
    worker.graphify_delivered_mode = ""
    worker.graphify_delivered_guide_version = ""
    worker.session_name = "test-session"
    return worker


def test_low_level_defaults_remain_off() -> None:
    assert ReviewAgentSelection("codex", "model", "high", "").graphify_mode == "off"
    config = _config()
    assert config.graphify_mode == "off"
    assert config.to_summary()["graphify_mode"] == "off"


def test_cli_stage_root_and_new_workflow_default_precedence(tmp_path: Path) -> None:
    path = tmp_path / "agents.json"
    path.write_text(
        json.dumps(
            {
                "graphify_mode": "required",
                "stages": {
                    "routing": {"graphify_mode": "off"},
                    "development": {"graphify_mode": "auto"},
                },
            }
        ),
        encoding="utf-8",
    )
    args = argparse.Namespace(graphify_mode="", agent_config=str(path))
    assert resolve_workflow_graphify_mode(args, stage_key="routing") == "off"
    assert resolve_workflow_graphify_mode(args, stage_key="development") == "auto"
    assert resolve_workflow_graphify_mode(args, stage_key="overall_review") == "required"

    cli_args = argparse.Namespace(graphify_mode="auto", agent_config=str(path))
    assert resolve_workflow_graphify_mode(cli_args, stage_key="routing") == "auto"
    assert resolve_workflow_graphify_mode(
        argparse.Namespace(graphify_mode="", agent_config=""),
        stage_key="development",
    ) == "auto"


def test_stage_config_applies_one_mode_to_main_and_reviewers(tmp_path: Path) -> None:
    path = tmp_path / "agents.json"
    path.write_text(
        json.dumps(
            {
                "graphify": {
                    "include": ["src/**", "tests/**"],
                    "exclude": ["generated/**"],
                    "max_workers": 1,
                    "initial_timeout_sec": 120,
                    "incremental_timeout_sec": 30,
                },
                "stages": {
                    "development": {
                        "graphify_mode": "required",
                        "main": {"vendor": "codex", "model": "model"},
                        "reviewers": [
                            {"name": "R1", "vendor": "claude", "model": "model"}
                        ],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    args = argparse.Namespace(
        graphify_mode="",
        agent_config=str(path),
        ponytail_mode="off",
        main_agent="",
        reviewer_agent=[],
        yes=True,
    )
    with (
        mock.patch("tmux_core.stage_kernel.shared_review.normalize_vendor_choice", side_effect=lambda value: value),
        mock.patch("tmux_core.stage_kernel.shared_review.normalize_model_choice", side_effect=lambda _vendor, value: value),
        mock.patch("tmux_core.stage_kernel.shared_review.normalize_effort_choice", side_effect=lambda _vendor, _model, value: value),
        mock.patch("tmux_core.stage_kernel.shared_review.get_default_model_for_vendor", return_value="model"),
    ):
        config = resolve_stage_agent_config(args, stage_key="development")
    assert config.graphify_mode == "required"
    assert config.main is not None and config.main.graphify_mode == "required"
    assert config.main.graphify_config["include"] == ("src/**", "tests/**")
    assert config.reviewer_selection("R1") is not None
    assert config.reviewer_selection("R1").graphify_mode == "required"
    assert config.reviewer_selection("R1").graphify_config == config.main.graphify_config


def test_role_level_graphify_override_is_rejected() -> None:
    with pytest.raises(RuntimeError, match="不支持角色级 graphify_mode"):
        parse_agent_selection_spec(
            {"vendor": "codex", "model": "model", "graphify_mode": "off"},
            default_graphify_mode="required",
        )
    with pytest.raises(RuntimeError, match="不支持角色级 graphify"):
        parse_agent_selection_spec(
            {"vendor": "codex", "model": "model", "graphify": {"include": ["src/**"]}},
            default_graphify_mode="required",
        )


def test_all_direct_stage_parsers_accept_graphify_mode() -> None:
    parsers = (
        routing.build_parser(),
        intake.build_parser(),
        clarification.build_parser(),
        requirements_review.build_parser(),
        detailed_design.build_parser(),
        task_split.build_parser(),
        development.build_parser(),
        overall_review.build_parser(),
    )
    for parser in parsers:
        args = parser.parse_args(["--graphify-mode", "required"])
        assert args.graphify_mode == "required"


@pytest.mark.parametrize("vendor", ("codex", "claude", "gemini", "opencode", "mimo", "agy", "deveco"))
def test_all_vendor_commands_receive_graphify_read_only_environment(vendor: str) -> None:
    command = _config(vendor, graphify_mode="auto").build_launch_command(Path("/tmp/project"))
    assert "TMUX_GRAPHIFY_CMD=" in command
    assert "TMUX_GRAPHIFY_PROJECT_DIR=/tmp/project" in command
    assert "TMUX_GRAPHIFY_MODE=auto" in command
    assert "TMUX_GRAPHIFY_READ_ONLY=1" in command
    assert "GRAPHIFY_QUERY_LOG_DISABLE=1" in command


def test_prompt_order_is_ponytail_grill_graphify_business_protocol() -> None:
    worker = _prompt_worker()
    profile = GraphifyTurnProfile(mode=GraphifyMode.AUTO.value)
    with (
        mock.patch.object(
            worker,
            "_normalize_graphify_profile_for_turn",
            return_value=SimpleNamespace(enabled=True, mode="auto"),
        ),
        mock.patch(
            "tmux_core.runtime.tmux_runtime.build_graphify_evidence_block",
            side_effect=lambda _profile, prompt, **_kwargs: f"[[GRAPHIFY EVIDENCE]]\n{prompt}",
        ),
    ):
        prompt = worker._build_turn_prompt(  # noqa: SLF001
            "Do the task",
            "[[DONE]]",
            (),
            task_status_path=Path("status.json"),
            include_turn_protocol=True,
            grill_profile=GrillTurnProfile("grill", 1),
            graphify_profile=profile,
        )
    assert prompt.index(PONYTAIL_BEGIN_MARKER) < prompt.index(GRILL_BEGIN_MARKER)
    assert prompt.index(GRILL_BEGIN_MARKER) < prompt.index("[[GRAPHIFY EVIDENCE]]")
    assert prompt.index("[[GRAPHIFY EVIDENCE]]") < prompt.index("Do the task")
    assert prompt.index("Do the task") < prompt.index("Turn completion protocol")


def test_graphify_orientation_is_full_once_then_compact_for_same_session() -> None:
    worker = _prompt_worker()
    profile = GraphifyTurnProfile(mode=GraphifyMode.AUTO.value)
    guide_flags: list[bool] = []

    def render(_profile, prompt, *, include_full_guide=True):  # noqa: ANN001
        guide_flags.append(bool(include_full_guide))
        return f"[[GRAPHIFY {'FULL' if include_full_guide else 'REMINDER'}]]\n{prompt}"
    with (
        mock.patch.object(
            worker,
            "_normalize_graphify_profile_for_turn",
            return_value=SimpleNamespace(enabled=True, mode="auto"),
        ),
        mock.patch(
            "tmux_core.runtime.tmux_runtime.build_graphify_evidence_block",
            side_effect=render,
        ),
    ):
        first = worker._build_turn_prompt(  # noqa: SLF001
            "First",
            "[[DONE]]",
            (),
            task_status_path=Path("status.json"),
            include_turn_protocol=False,
            graphify_profile=profile,
        )
        worker.graphify_orientation_delivered = True
        worker.graphify_delivered_mode = "auto"
        worker.graphify_delivered_guide_version = GRAPHIFY_GUIDE_VERSION
        second = worker._build_turn_prompt(  # noqa: SLF001
            "Second",
            "[[DONE]]",
            (),
            task_status_path=Path("status.json"),
            include_turn_protocol=False,
            graphify_profile=profile,
        )

    assert guide_flags == [True, False]
    assert "[[GRAPHIFY FULL]]" in first
    assert "[[GRAPHIFY REMINDER]]" in second


def test_graphify_orientation_latches_only_a_confirmed_full_guide() -> None:
    worker = _prompt_worker()
    managed_evidence = (
        f"{GRAPHIFY_FULL_GUIDE_MARKER}\n"
        "managed evidence\n"
        "[END_GRAPHIFY_EVIDENCE]"
    )
    profile = GraphifyTurnProfile(
        mode=GraphifyMode.AUTO.value,
        evidence=GraphifyEvidence(
            evidence_id="evidence-1",
            graph_fingerprint="f" * 64,
            freshness="fresh",
            block_text=managed_evidence,
            compact_block_text="compact evidence",
        ),
    )
    worker._persist_graphify_policy_fast = mock.Mock()  # type: ignore[method-assign]

    worker._confirm_graphify_orientation_delivery("ordinary prompt", profile)  # noqa: SLF001
    assert worker.graphify_orientation_delivered is False

    worker._confirm_graphify_orientation_delivery(  # noqa: SLF001
        f"business prompt quoted {GRAPHIFY_FULL_GUIDE_MARKER}",
        profile,
    )
    assert worker.graphify_orientation_delivered is False

    worker._confirm_graphify_orientation_delivery(  # noqa: SLF001
        f"{managed_evidence}\n\nbusiness prompt",
        profile,
    )
    assert worker.graphify_orientation_delivered is True
    assert worker.graphify_delivered_mode == "auto"
    assert worker.graphify_delivered_guide_version == GRAPHIFY_GUIDE_VERSION
    worker._persist_graphify_policy_fast.assert_called_once()


def test_resumed_contract_latches_persisted_full_graphify_delivery() -> None:
    worker = _prompt_worker()
    profile = GraphifyTurnProfile(
        mode=GraphifyMode.AUTO.value,
        evidence=GraphifyEvidence(
            evidence_id="evidence-1",
            graph_fingerprint="f" * 64,
            freshness="fresh",
            block_text=f"{GRAPHIFY_FULL_GUIDE_MARKER}\nmanaged evidence",
        ),
    )
    persisted_state = {
        "current_graphify_prompt_kind": "full",
        "current_graphify_guide_version": GRAPHIFY_GUIDE_VERSION,
        "started_at": "2026-07-27T00:00:00+00:00",
    }
    worker.read_state = mock.Mock(return_value=persisted_state)  # type: ignore[method-assign]
    worker._persist_graphify_policy_fast = mock.Mock()  # type: ignore[method-assign]
    worker._confirm_grill_profile_delivery = mock.Mock()  # type: ignore[method-assign]
    worker._record_result = mock.Mock()  # type: ignore[method-assign]
    worker._log_event = mock.Mock()  # type: ignore[method-assign]
    contract = TurnFileContract(
        turn_id="turn-1",
        phase="requirements_clarification",
        status_path=Path("/tmp/graphify-resume-status.json"),
        validator=mock.Mock(),
    )
    file_result = TurnFileResult(
        status_path=str(contract.status_path),
        payload={"status": "completed"},
        artifact_paths={},
        artifact_hashes={},
        validated_at="2026-07-27T00:00:01+00:00",
    )

    worker._record_resumed_completion_turn(  # noqa: SLF001
        label="resume",
        contract=contract,
        file_result=file_result,
        task_status_path=None,
        grill_profile=None,
        graphify_profile=profile,
    )

    assert worker.graphify_orientation_delivered is True
    assert worker.graphify_delivered_mode == GraphifyMode.AUTO.value
    assert worker.graphify_delivered_guide_version == GRAPHIFY_GUIDE_VERSION
    worker._persist_graphify_policy_fast.assert_called_once()


@pytest.mark.parametrize(
    "vendor",
    ("codex", "claude", "gemini", "opencode", "mimo", "agy", "deveco"),
)
def test_all_vendor_launches_receive_real_graphify_runner_id(
    tmp_path: Path,
    vendor: str,
) -> None:
    worker = object.__new__(TmuxBatchWorker)
    worker.config = _config(vendor, graphify_mode="auto")
    worker.work_dir = tmp_path
    worker.stage_runner_id = "runner-123"
    worker.graphify_session_generation = "session-123"

    command = worker._build_agent_launch_command()  # noqa: SLF001

    assert command.startswith("env TMUX_GRAPHIFY_RUNNER_ID=runner-123 ")
    assert command.count("TMUX_GRAPHIFY_RUNNER_ID=") == 1
    assert "TMUX_GRAPHIFY_SESSION_GENERATION=session-123" in command

    worker.stage_runner_id = ""
    assert "TMUX_GRAPHIFY_RUNNER_ID=" not in worker._build_agent_launch_command()  # noqa: SLF001
    assert "TMUX_GRAPHIFY_SESSION_GENERATION=session-123" in worker._build_agent_launch_command()  # noqa: SLF001


def test_new_session_generation_clears_old_graphify_evidence_identity(
    tmp_path: Path,
) -> None:
    backend = mock.Mock()
    backend.control_state.return_value = {}
    runtime_dir = tmp_path / "runtime" / "worker-existing"
    runtime_dir.mkdir(parents=True)
    state_path = runtime_dir / "worker.state.json"
    state_path.write_text(
        json.dumps(
            {
                "session_name": "old-session",
                "config": {
                    "vendor": "codex",
                    "model": "model",
                    "graphify_mode": "auto",
                    "graphify_config": {},
                },
                "graphify_evidence_id": "old-evidence",
                "graphify_fingerprint": "f" * 64,
                "graphify_freshness": "fresh",
                "graphify_policy": {
                    "session_generation": "old-generation",
                    "guide_version": GRAPHIFY_GUIDE_VERSION,
                    "orientation_delivered": True,
                    "delivered_mode": "auto",
                },
            }
        ),
        encoding="utf-8",
    )
    with mock.patch(
        "tmux_core.runtime.tmux_runtime.publish_graphify_pending_status"
    ):
        worker = TmuxBatchWorker(
            worker_id="worker",
            work_dir=tmp_path,
            config=_config(graphify_mode="auto"),
            runtime_root=tmp_path / "runtime",
            existing_runtime_dir=runtime_dir,
            existing_session_name="old-session",
            backend=backend,
        )
    old_generation = worker.graphify_session_generation

    worker._reset_graphify_delivery_generation()  # noqa: SLF001
    worker._write_session_created_state_fast()  # noqa: SLF001
    persisted = json.loads(state_path.read_text(encoding="utf-8"))

    assert persisted["graphify_evidence_id"] == ""
    assert persisted["graphify_fingerprint"] == ""
    assert persisted["graphify_freshness"] == ""
    assert persisted["graphify_policy"]["orientation_delivered"] is False
    assert persisted["graphify_policy"]["session_generation"] != old_generation


def test_graphify_pending_and_query_aggregate_are_runner_generation_scoped(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    with mock.patch.dict("os.environ", {"XDG_CACHE_HOME": str(tmp_path / "cache")}, clear=False):
        assert publish_graphify_pending_status(
            project,
            "auto",
            runner_id="root-runner",
            stage_key="A07",
            session_generation="session-reused",
        )
        graphify_runtime._write_status(  # noqa: SLF001
            project,
            GraphifyStatus(
                mode="auto",
                state=GraphifyState.READY.value,
                version="0.9.27",
                freshness="fresh",
                query_count_stage=4,
                last_query_command="affected",
            ),
        )

        # Another worker in the same runner must not flash Ready back to
        # Building or clear the already observed query count.
        assert publish_graphify_pending_status(
            project,
            "auto",
            runner_id="root-runner",
            stage_key="A07",
            session_generation="session-old-peer",
        )
        same_runner = read_graphify_project_status(project)
        assert same_runner is not None
        assert same_runner["state"] == GraphifyState.READY.value
        assert same_runner["query_count_stage"] == 4

        # A00 reuses one root runner, so the stage key must reset the aggregate.
        assert publish_graphify_pending_status(
            project,
            "auto",
            runner_id="root-runner",
            stage_key="A08",
            session_generation="session-reused",
        )
        new_runner = read_graphify_project_status(project)
        assert new_runner is not None
        assert new_runner["state"] == GraphifyState.BUILDING.value
        assert new_runner["query_count_stage"] == 0

        query_result = GraphifyQueryResult(
            ok=True,
            query_id="query-1",
            command="affected",
            graph_fingerprint="f" * 64,
            freshness="fresh",
            warnings=(),
            truncated=False,
            duration_ms=1,
            result_text="result",
        )
        # A late query from an A07-only session cannot contaminate A08 even
        # though both stages share one root runner.
        with mock.patch.dict(
            "os.environ",
            {
                "TMUX_GRAPHIFY_RUNNER_ID": "root-runner",
                "TMUX_GRAPHIFY_SESSION_GENERATION": "session-old-peer",
                "TMUX_GRAPHIFY_MODE": "auto",
            },
            clear=False,
        ):
            graphify_runtime._publish_query_result(project, query_result)  # noqa: SLF001
        after_late_query = read_graphify_project_status(project)
        assert after_late_query is not None
        assert after_late_query["query_count_stage"] == 0

        with mock.patch.dict(
            "os.environ",
            {
                # A reused session can retain the A07 runner env. The trusted
                # ledger and stable session generation own A08 attribution.
                "TMUX_GRAPHIFY_RUNNER_ID": "stale-a07-env",
                "TMUX_GRAPHIFY_SESSION_GENERATION": "session-reused",
                "TMUX_GRAPHIFY_MODE": "auto",
            },
            clear=False,
        ):
            graphify_runtime._publish_query_result(project, query_result)  # noqa: SLF001
            query_scope = graphify_runtime._resolve_query_scope(project)  # noqa: SLF001
        after_current_query = read_graphify_project_status(project)
        assert after_current_query is not None
        assert after_current_query["query_count_stage"] == 1
        assert query_scope.runner_id == "root-runner"
        assert query_scope.stage_key == "A08"
        assert query_scope.accepted is True


def test_graphify_identity_and_orientation_survive_intermediate_state_writes(
    tmp_path: Path,
) -> None:
    backend = mock.Mock()
    backend.control_state.return_value = {}
    runtime_dir = tmp_path / "runtime" / "worker-existing"
    runtime_dir.mkdir(parents=True)
    state_path = runtime_dir / "worker.state.json"
    state_path.write_text(
        json.dumps(
            {
                "session_name": "same-session",
                "config": {
                    "vendor": "codex",
                    "model": "model",
                    "graphify_mode": "auto",
                    "graphify_config": {},
                },
                "graphify_evidence_id": "evidence-1",
                "graphify_fingerprint": "f" * 64,
                "graphify_freshness": "fresh",
                "graphify_generation_refreshed": True,
                "graphify_policy": {
                    "session_generation": "generation-1",
                    "guide_version": GRAPHIFY_GUIDE_VERSION,
                    "orientation_delivered": True,
                    "delivered_mode": "auto",
                },
            }
        ),
        encoding="utf-8",
    )
    with mock.patch(
        "tmux_core.runtime.tmux_runtime.publish_graphify_pending_status"
    ):
        worker = TmuxBatchWorker(
            worker_id="worker",
            work_dir=tmp_path,
            config=_config(graphify_mode="auto"),
            runtime_root=tmp_path / "runtime",
            existing_runtime_dir=runtime_dir,
            existing_session_name="same-session",
            backend=backend,
        )
    worker.is_agent_alive = mock.Mock(return_value=True)  # type: ignore[method-assign]
    worker.get_agent_state = mock.Mock(return_value=AgentRuntimeState.READY)  # type: ignore[method-assign]

    worker._write_state(WorkerStatus.RUNNING, note="health_refresh")  # noqa: SLF001
    persisted = json.loads(state_path.read_text(encoding="utf-8"))

    assert persisted["graphify_evidence_id"] == "evidence-1"
    assert persisted["graphify_fingerprint"] == "f" * 64
    assert persisted["graphify_freshness"] == "fresh"
    assert persisted["graphify_generation_refreshed"] is True
    assert persisted["graphify_policy"]["orientation_delivered"] is True
    assert worker.graphify_session_generation == "generation-1"


def test_run_turn_resolves_graphify_once_before_internal_retry_loop() -> None:
    worker = object.__new__(TmuxBatchWorker)
    worker._runtime_intervention_handler = None  # noqa: SLF001
    profile = GraphifyTurnProfile(mode=GraphifyMode.AUTO.value)
    worker._resolve_graphify_profile_for_turn = mock.Mock(return_value=profile)  # type: ignore[method-assign]  # noqa: SLF001
    worker._run_turn_impl = mock.Mock(return_value="result")  # type: ignore[method-assign]  # noqa: SLF001

    assert worker.run_turn(label="task", prompt="Do the task") == "result"
    worker._resolve_graphify_profile_for_turn.assert_called_once_with("Do the task", None)  # type: ignore[attr-defined]  # noqa: SLF001
    assert worker._run_turn_impl.call_args.kwargs["graphify_profile"] is profile  # type: ignore[attr-defined]  # noqa: SLF001


def test_off_profile_is_hard_noop_without_cache_or_tool_probe() -> None:
    worker = object.__new__(TmuxBatchWorker)
    worker.config = SimpleNamespace(graphify_mode="off", graphify_config={})
    worker.graphify_mode = "off"
    with mock.patch("tmux_core.runtime.tmux_runtime.resolve_graphify_turn_profile") as resolver:
        profile = worker.prepare_graphify_turn_profile("Do the task")
    assert profile.mode == "off"
    resolver.assert_not_called()


def test_required_preflight_fails_before_tmux_session_creation() -> None:
    worker = object.__new__(TmuxBatchWorker)
    worker.config = SimpleNamespace(graphify_mode="required")
    worker.graphify_mode = "required"
    worker._resolve_graphify_profile_for_turn = mock.Mock(  # type: ignore[method-assign]  # noqa: SLF001
        side_effect=RuntimeError("graph schema invalid")
    )
    worker.create_session = mock.Mock()  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="graph schema invalid"):
        worker.launch_agent()

    worker.create_session.assert_not_called()  # type: ignore[attr-defined]


def test_interactive_config_resolution_defers_tool_prompt_until_worker_launch() -> None:
    args = argparse.Namespace(
        graphify_mode="",
        agent_config="",
        yes=False,
        project_dir="/tmp/graphify-config-test",
    )
    with (
        mock.patch.object(shared_review, "stdin_is_interactive", return_value=True),
        mock.patch.object(shared_review, "enable_graphify_interactive_recovery") as enable,
    ):
        assert resolve_workflow_graphify_mode(args, stage_key="routing") == "auto"
        assert resolve_workflow_graphify_mode(args, stage_key="development") == "auto"
    assert enable.call_count == 2


def test_auto_worker_launch_prompts_once_and_can_degrade_without_install(tmp_path: Path) -> None:
    worker = object.__new__(TmuxBatchWorker)
    worker.work_dir = tmp_path
    worker.graphify_mode = "auto"
    worker.stage_runner_id = "runner-auto"
    worker.graphify_generation_refreshed = False
    worker.config = _config(graphify_mode="auto")
    worker.launch_command = worker.config.build_launch_command(tmp_path)
    unavailable = SimpleNamespace(compatible=False, error="version mismatch")
    with (
        mock.patch("tmux_core.runtime.tmux_runtime.graphify_interactive_recovery_enabled", return_value=True),
        mock.patch("tmux_core.runtime.tmux_runtime.resolve_graphify_tool", return_value=unavailable),
        mock.patch("tmux_core.runtime.tmux_runtime.graphify_required_recovery_decision", return_value=""),
        mock.patch("tmux_core.runtime.tmux_runtime.set_graphify_required_recovery_decision") as remember,
        mock.patch("tmux_core.runtime.tmux_runtime.setup_managed_graphify") as setup,
        mock.patch("T09_terminal_ops.prompt_select_option", return_value="continue_auto") as prompt,
    ):
        worker._run_auto_graphify_availability_decision()  # noqa: SLF001
    prompt.assert_called_once()
    setup.assert_not_called()
    remember.assert_called_once_with(
        tmp_path,
        "continue_auto",
        interaction_scope="runner-auto",
    )
    assert worker.graphify_mode == "auto"


def test_noninteractive_graphify_resolution_does_not_probe_or_install() -> None:
    args = argparse.Namespace(graphify_mode="required", agent_config="", yes=True)
    with mock.patch.object(shared_review, "enable_graphify_interactive_recovery") as enable:
        assert resolve_workflow_graphify_mode(args, stage_key="development") == "required"
    enable.assert_not_called()


def test_required_runtime_recovery_can_switch_to_auto_before_tmux_mutation(tmp_path: Path) -> None:
    worker = object.__new__(TmuxBatchWorker)
    worker.work_dir = tmp_path
    worker.runtime_dir = tmp_path / "runtime"
    worker.runtime_dir.mkdir()
    worker.graphify_mode = "required"
    worker.stage_runner_id = "runner-required"
    worker.graphify_generation_refreshed = False
    worker.config = _config(graphify_mode="required")
    worker.launch_command = worker.config.build_launch_command(tmp_path)
    auto_profile = GraphifyTurnProfile(mode="auto")
    worker._resolve_graphify_profile_for_turn = mock.Mock(  # type: ignore[method-assign]  # noqa: SLF001
        side_effect=[GraphifyUnavailable("schema invalid"), auto_profile]
    )
    with (
        mock.patch("tmux_core.runtime.tmux_runtime.graphify_interactive_recovery_enabled", return_value=True),
        mock.patch("tmux_core.runtime.tmux_runtime.graphify_required_recovery_decision", return_value=""),
        mock.patch("tmux_core.runtime.tmux_runtime.set_graphify_required_recovery_decision") as remember,
        mock.patch("T09_terminal_ops.prompt_select_option", return_value="auto"),
    ):
        worker._run_required_graphify_preflight()  # noqa: SLF001
    assert worker.graphify_mode == "auto"
    assert worker.config.graphify_mode == "auto"
    assert "TMUX_GRAPHIFY_MODE=auto" in worker.launch_command
    assert worker.graphify_generation_refreshed is True
    remember.assert_called_once_with(
        tmp_path,
        "auto",
        interaction_scope="runner-required",
    )


def test_graphify_recovery_decision_is_runner_scoped_and_legacy_calls_do_not_cache(
    tmp_path: Path,
) -> None:
    set_graphify_required_recovery_decision(
        tmp_path,
        GraphifyMode.AUTO.value,
        interaction_scope="runner-old",
    )
    assert graphify_required_recovery_decision(
        tmp_path,
        interaction_scope="runner-old",
    ) == GraphifyMode.AUTO.value
    assert graphify_required_recovery_decision(
        tmp_path,
        interaction_scope="runner-new",
    ) == ""

    set_graphify_required_recovery_decision(tmp_path, GraphifyMode.OFF.value)
    assert graphify_required_recovery_decision(tmp_path) == ""


def test_auto_recovery_prompts_once_per_runner_generation(tmp_path: Path) -> None:
    def worker_for(runner_id: str) -> TmuxBatchWorker:
        worker = object.__new__(TmuxBatchWorker)
        worker.work_dir = tmp_path
        worker.graphify_mode = GraphifyMode.AUTO.value
        worker.stage_runner_id = runner_id
        return worker

    unavailable = SimpleNamespace(compatible=False, error="version mismatch")
    with (
        mock.patch("tmux_core.runtime.tmux_runtime.graphify_interactive_recovery_enabled", return_value=True),
        mock.patch("tmux_core.runtime.tmux_runtime.resolve_graphify_tool", return_value=unavailable),
        mock.patch("T09_terminal_ops.prompt_select_option", return_value="continue_auto") as prompt,
    ):
        worker_for("runner-old")._run_auto_graphify_availability_decision()  # noqa: SLF001
        worker_for("runner-old")._run_auto_graphify_availability_decision()  # noqa: SLF001
        worker_for("runner-new")._run_auto_graphify_availability_decision()  # noqa: SLF001

    assert prompt.call_count == 2


def test_graphify_checkpoint_refreshes_one_worker_and_marks_peers() -> None:
    leader = SimpleNamespace(
        graphify_mode="auto",
        refresh_graphify_generation=mock.Mock(return_value="profile"),
        mark_graphify_generation_current=mock.Mock(),
    )
    peer = SimpleNamespace(
        graphify_mode="auto",
        refresh_graphify_generation=mock.Mock(),
        mark_graphify_generation_current=mock.Mock(),
    )
    off = SimpleNamespace(
        graphify_mode="off",
        refresh_graphify_generation=mock.Mock(),
        mark_graphify_generation_current=mock.Mock(),
    )
    context = GraphifyTurnContext(
        stage_key="A08",
        phase="a08_checkpoint",
        role="reviewer",
        intent=GraphifyQueryIntent.WHOLE_CHANGE_REVIEW,
    )
    assert shared_review.refresh_graphify_workers_for_checkpoint(
        [leader, peer, off],
        prompt="A08 checkpoint",
        turn_context=context,
    ) == "profile"
    leader.refresh_graphify_generation.assert_called_once_with(
        "A08 checkpoint",
        turn_context=context,
    )
    peer.refresh_graphify_generation.assert_not_called()
    peer.mark_graphify_generation_current.assert_called_once_with(
        turn_context=context,
    )
    off.refresh_graphify_generation.assert_not_called()


def test_a01_run_store_restores_graphify_scan_policy(tmp_path: Path) -> None:
    manifest = RunManifest(
        manifest_version=1,
        run_id="run-test",
        runtime_dir=str(tmp_path),
        project_dir=str(tmp_path),
        selection={},
        config={
            "vendor": "codex",
            "model": "model",
            "reasoning_effort": "high",
            "graphify_mode": "auto",
            "graphify_config": {
                "include": ["src/**"],
                "max_workers": 1,
                "initial_timeout_sec": 120,
                "incremental_timeout_sec": 30,
            },
        },
        status="running",
        created_at="",
        updated_at="",
    )
    store = RunStore(run_root=tmp_path, manifest=manifest)
    with mock.patch(
        "tmux_core.runtime.tmux_runtime.resolve_launch",
        return_value=_resolution("codex", "model"),
    ):
        config = store.config_object()
    assert config.graphify_mode == "auto"
    assert config.graphify_config["include"] == ("src/**",)
    assert config.graphify_config["initial_timeout_sec"] == 120.0
