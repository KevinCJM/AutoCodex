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
from tmux_core.runtime.graphify import (
    GraphifyMode,
    GraphifyTurnProfile,
    GraphifyUnavailable,
    graphify_required_recovery_decision,
    set_graphify_required_recovery_decision,
)
from tmux_core.runtime.grill import BEGIN_MARKER as GRILL_BEGIN_MARKER, GrillTurnProfile
from tmux_core.runtime.ponytail import BEGIN_MARKER as PONYTAIL_BEGIN_MARKER
from tmux_core.runtime.tmux_runtime import AgentRunConfig, TmuxBatchWorker
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
            side_effect=lambda _profile, prompt: f"[[GRAPHIFY EVIDENCE]]\n{prompt}",
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
    assert shared_review.refresh_graphify_workers_for_checkpoint(
        [leader, peer, off], prompt="A08 checkpoint"
    ) == "profile"
    leader.refresh_graphify_generation.assert_called_once_with("A08 checkpoint")
    peer.refresh_graphify_generation.assert_not_called()
    peer.mark_graphify_generation_current.assert_called_once_with()
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
