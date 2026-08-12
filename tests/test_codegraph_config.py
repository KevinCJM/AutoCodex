from __future__ import annotations

import argparse
import importlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

import A01_Routing_LayerPlanning as routing
import A02_RequirementIntake as intake
import A02_RequirementsAnalysis as requirements_analysis
import A03_RequirementsClarification as clarification
from tmux_core.runtime.codegraph import (
    CODEGRAPH_FULL_GUIDE_MARKER,
    CODEGRAPH_GUIDE_VERSION,
    CodeGraphMode,
    CodeGraphProjectStatus,
    CodeGraphQueryIntent,
    CodeGraphTurnContext,
    CodeGraphTurnProfile,
    CodeGraphUnavailable,
    codegraph_required_recovery_decision,
    set_codegraph_required_recovery_decision,
)
from tmux_core.runtime.grill import BEGIN_MARKER as GRILL_BEGIN_MARKER, GrillTurnProfile
from tmux_core.runtime.ponytail import BEGIN_MARKER as PONYTAIL_BEGIN_MARKER
from tmux_core.runtime.tmux_runtime import AgentRunConfig, CommandResult, TmuxBatchWorker, load_worker_from_state_path
from tmux_core.stage_kernel import detailed_design, development, overall_review, requirements_review, shared_review, task_split
from tmux_core.workflow import entry as workflow_entry
from T09_terminal_ops import cleanup_codegraph_processes_on_exit
from tmux_core.stage_kernel.shared_review import (
    ReviewAgentSelection,
    parse_agent_selection_spec,
    resolve_stage_agent_config,
    resolve_workflow_codegraph_mode,
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


def _config(vendor: str = "codex", *, mode: str = "off") -> AgentRunConfig:
    with mock.patch("tmux_core.runtime.tmux_runtime.resolve_launch", return_value=_resolution(vendor, "model")):
        return AgentRunConfig(vendor=vendor, model="model", codegraph_mode=mode)


def _prompt_worker() -> TmuxBatchWorker:
    worker = object.__new__(TmuxBatchWorker)
    worker.config = SimpleNamespace(
        ponytail_mode="full",
        requirements_mode="grill",
        codegraph_mode="auto",
        codegraph_config={},
    )
    worker.ponytail_mode = "full"
    worker.ponytail_full_delivered = False
    worker.ponytail_delivered_mode = ""
    worker.requirements_mode = "grill"
    worker.requirements_behavior = "interview"
    worker.requirements_transition_pending = False
    worker.grill_full_delivered = False
    worker.grill_delivered_mode = ""
    worker.grill_question_seq = 0
    worker.codegraph_mode = "auto"
    worker.codegraph_session_generation = "cg-session"
    worker.codegraph_orientation_delivered = False
    worker.codegraph_delivered_mode = ""
    worker.codegraph_delivered_guide_version = ""
    worker.session_name = "test-session"
    return worker


def _profile() -> CodeGraphTurnProfile:
    status = CodeGraphProjectStatus(mode="auto", state="ready", initialized=True, freshness="fresh")
    return CodeGraphTurnProfile(
        mode="auto",
        status=status,
        suggestion='"$TMUX_CODEGRAPH_CMD" explore "callers of service"',
        full_text=f"{CODEGRAPH_FULL_GUIDE_MARKER}\nFULL GUIDE",
        reminder_text="SHORT REMINDER",
    )


def test_low_level_defaults_remain_off() -> None:
    assert ReviewAgentSelection("codex", "model", "high", "").codegraph_mode == "off"
    assert _config().codegraph_mode == "off"


@pytest.mark.parametrize(
    "module_name",
    (
        "A01_Routing_LayerPlanning",
        "A02_RequirementIntake",
        "A02_RequirementsAnalysis",
        "A03_RequirementsClarification",
        "tmux_core.stage_kernel.requirements_review",
        "tmux_core.stage_kernel.detailed_design",
        "tmux_core.stage_kernel.task_split",
        "tmux_core.stage_kernel.development",
        "tmux_core.stage_kernel.overall_review",
    ),
)
def test_standalone_stage_main_always_owns_codegraph_cleanup(module_name: str) -> None:
    module = importlib.import_module(module_name)
    with mock.patch.object(module, "maybe_launch_tui", return_value=(True, 0)), mock.patch(
        "tmux_core.runtime.codegraph.cancel_codegraph_processes",
    ) as cancel:
        assert module.main([]) == 0
    cancel.assert_called_once_with()


@pytest.mark.parametrize("outcome", ("return", "error", "interrupt"))
def test_codegraph_cli_cleanup_guard_covers_all_exit_kinds(outcome: str) -> None:
    @cleanup_codegraph_processes_on_exit
    def target() -> int:
        if outcome == "error":
            raise RuntimeError("boom")
        if outcome == "interrupt":
            raise KeyboardInterrupt
        return 7

    with mock.patch("tmux_core.runtime.codegraph.cancel_codegraph_processes") as cancel:
        if outcome == "error":
            with pytest.raises(RuntimeError, match="boom"):
                target()
        elif outcome == "interrupt":
            with pytest.raises(KeyboardInterrupt):
                target()
        else:
            assert target() == 7
    cancel.assert_called_once_with()


def test_codegraph_cli_cleanup_failure_does_not_hide_stage_failure() -> None:
    @cleanup_codegraph_processes_on_exit
    def target() -> None:
        raise RuntimeError("stage failure")

    with mock.patch(
        "tmux_core.runtime.codegraph.cancel_codegraph_processes",
        side_effect=RuntimeError("cleanup failure"),
    ):
        with pytest.raises(RuntimeError, match="stage failure"):
            target()


def test_legacy_graphify_worker_state_is_not_reconstructed_as_codegraph_off(tmp_path: Path) -> None:
    runtime_dir = tmp_path / "runtime" / "worker"
    runtime_dir.mkdir(parents=True)
    state_path = runtime_dir / "worker.state.json"
    state_path.write_text(json.dumps({
        "worker_id": "worker",
        "work_dir": str(tmp_path),
        "session_name": "legacy-session",
        "config": {
            "vendor": "codex",
            "model": "model",
            "graphify_mode": "required",
            "graphify_config": {"max_files": 3},
        },
        "graphify_policy": {"session_generation": "legacy"},
    }), encoding="utf-8")

    with mock.patch("tmux_core.runtime.tmux_runtime.TmuxBatchWorker") as constructor:
        assert load_worker_from_state_path(state_path) is None
    constructor.assert_not_called()


def test_pre_graph_worker_state_keeps_off_compatibility(tmp_path: Path) -> None:
    runtime_dir = tmp_path / "runtime" / "worker"
    runtime_dir.mkdir(parents=True)
    state_path = runtime_dir / "worker.state.json"
    state_path.write_text(json.dumps({
        "worker_id": "worker",
        "work_dir": str(tmp_path),
        "session_name": "old-session",
        "config": {"vendor": "codex", "model": "model"},
    }), encoding="utf-8")
    reconstructed = SimpleNamespace()
    with mock.patch("tmux_core.runtime.tmux_runtime.AgentRunConfig") as config_factory, mock.patch(
        "tmux_core.runtime.tmux_runtime.TmuxBatchWorker",
        return_value=reconstructed,
    ):
        assert load_worker_from_state_path(state_path) is reconstructed
    assert config_factory.call_args.kwargs["codegraph_mode"] == "off"


@pytest.mark.parametrize(
    "helper_name,needs_confirmation",
    (("prompt_replacement_review_agent_selection", True), ("prompt_required_replacement_review_agent_selection", False)),
)
def test_replacement_selection_preserves_codegraph_policy(helper_name: str, needs_confirmation: bool) -> None:
    previous = ReviewAgentSelection(
        "codex", "old", "high", "", ponytail_mode="lite",
        codegraph_mode="required", codegraph_config={"max_files": 3},
    )
    candidate = ReviewAgentSelection("claude", "new", "high", "")
    helper = getattr(shared_review, helper_name)
    patches = [mock.patch.object(shared_review, "prompt_review_agent_selection", return_value=candidate)]
    if needs_confirmation:
        patches.append(mock.patch.object(shared_review, "prompt_yes_no_choice", return_value=True))
    with patches[0] as selection_prompt:
        if len(patches) > 1:
            with patches[1]:
                selected = helper(
                    reason_text="replace", previous_selection=previous,
                    force_model_change=False, role_label="审核员",
                )
        else:
            selected = helper(
                reason_text="replace", previous_selection=previous,
                force_model_change=False, role_label="审核员",
            )
    assert selected is not None
    assert selected.codegraph_mode == "required"
    assert selected.codegraph_config == {"max_files": 3}
    assert selection_prompt.call_args.kwargs["default_codegraph_mode"] == "required"
    assert selection_prompt.call_args.kwargs["default_codegraph_config"] == {"max_files": 3}


def test_a02_selection_and_wrapper_preserve_codegraph_config(tmp_path: Path) -> None:
    config_path = tmp_path / "agents.json"
    config_path.write_text(json.dumps({
        "codegraph": {"max_files": 4, "max_output_chars": 9000},
        "stages": {"requirements_clarification": {"codegraph_mode": "required"}},
    }), encoding="utf-8")
    args = SimpleNamespace(
        vendor="codex", model="model", effort="high", proxy_url="",
        ponytail_mode="off", main_ponytail_mode="", requirements_mode="standard",
        codegraph_mode="", graphify_mode="", agent_config=str(config_path),
        project_dir=str(tmp_path), yes=True, reviewer_agent=[],
    )
    with mock.patch.object(requirements_analysis, "stdin_is_interactive", return_value=False), mock.patch.object(
        requirements_analysis, "normalize_vendor_choice", side_effect=lambda value: value,
    ), mock.patch.object(
        requirements_analysis, "normalize_model_choice", side_effect=lambda _vendor, value: value,
    ), mock.patch.object(
        requirements_analysis, "normalize_effort_choice", side_effect=lambda _vendor, _model, value: value,
    ):
        selection = requirements_analysis.collect_requirements_analysis_agent_selection(args)
    assert selection.codegraph_mode == "required"
    assert selection.codegraph_config["max_files"] == 4

    with mock.patch.object(
        requirements_analysis.clarification_module,
        "run_requirements_clarification",
        return_value=SimpleNamespace(),
    ) as run:
        requirements_analysis.run_requirements_analysis(
            tmp_path, "需求A", codegraph_mode=selection.codegraph_mode,
            codegraph_config=selection.codegraph_config,
        )
    assert run.call_args.kwargs["codegraph_config"]["max_files"] == 4


def test_a04_new_ba_receives_codegraph_config(tmp_path: Path) -> None:
    selection = ReviewAgentSelection(
        "codex", "model", "high", "", codegraph_mode="required",
        codegraph_config={"max_files": 4},
    )
    run_config = SimpleNamespace(requirements_mode="standard")
    worker = SimpleNamespace(
        session_name="需求分析师",
        runtime_dir=tmp_path / "runtime",
        config=run_config,
    )
    with mock.patch.object(
        requirements_review, "prompt_review_agent_selection", return_value=selection,
    ) as prompt, mock.patch.object(
        requirements_review, "resolve_agent_run_config_with_recovery",
        return_value=(selection, run_config),
    ), mock.patch.object(requirements_review, "TmuxBatchWorker", return_value=worker):
        handoff = requirements_review._create_review_ba_handoff(  # noqa: SLF001
            project_dir=tmp_path, requirement_name="需求A", selection_title="A04",
            codegraph_mode="required", codegraph_config={"max_files": 4},
        )
    assert prompt.call_args.kwargs["default_codegraph_config"] == {"max_files": 4}
    assert handoff.codegraph_config == {"max_files": 4}


def test_new_workflow_precedence_and_legacy_mapping(tmp_path: Path) -> None:
    config_path = tmp_path / "agents.json"
    config_path.write_text(json.dumps({
        "codegraph_mode": "required",
        "stages": {"routing": {"codegraph_mode": "off"}, "development": {"codegraph_mode": "auto"}},
    }), encoding="utf-8")
    common = dict(agent_config=str(config_path), yes=True, project_dir=str(tmp_path))
    args = argparse.Namespace(codegraph_mode="", graphify_mode="", **common)
    assert resolve_workflow_codegraph_mode(args, stage_key="routing") == "off"
    assert resolve_workflow_codegraph_mode(args, stage_key="development") == "auto"
    assert resolve_workflow_codegraph_mode(args, stage_key="overall_review") == "required"
    cli = argparse.Namespace(codegraph_mode="auto", graphify_mode="off", **common)
    with pytest.warns(FutureWarning, match="graphify_mode"):
        assert resolve_workflow_codegraph_mode(cli, stage_key="routing") == "auto"

    legacy = argparse.Namespace(codegraph_mode="", graphify_mode="required", agent_config="", yes=True, project_dir=str(tmp_path))
    with pytest.warns(FutureWarning, match="graphify_mode"):
        assert resolve_workflow_codegraph_mode(legacy, stage_key="development") == "required"


def test_project_preference_applies_only_without_explicit_configuration(tmp_path: Path) -> None:
    preference = tmp_path / ".tmux_workflow" / "codegraph.preference.json"
    preference.parent.mkdir()
    preference.write_text('{"schema":"tmux-codegraph-preference/1","mode":"off"}\n', encoding="utf-8")
    args = argparse.Namespace(codegraph_mode="", graphify_mode="", agent_config="", yes=True, project_dir=str(tmp_path))
    assert resolve_workflow_codegraph_mode(args, stage_key="development") == "off"
    args.codegraph_mode = "auto"
    args._resolved_codegraph_modes = {}
    assert resolve_workflow_codegraph_mode(args, stage_key="development") == "auto"


def test_stage_config_is_shared_and_role_override_rejected(tmp_path: Path) -> None:
    path = tmp_path / "agents.json"
    path.write_text(json.dumps({
        "codegraph": {"max_files": 4, "max_output_chars": 9000, "init_timeout_sec": 90, "sync_timeout_sec": 20},
        "stages": {"development": {
            "codegraph_mode": "required",
            "main": {"vendor": "codex", "model": "model"},
            "reviewers": [{"name": "R1", "vendor": "claude", "model": "model"}],
        }},
    }), encoding="utf-8")
    args = argparse.Namespace(
        codegraph_mode="", graphify_mode="", agent_config=str(path), ponytail_mode="off",
        main_agent="", reviewer_agent=[], yes=True, project_dir=str(tmp_path),
    )
    with (
        mock.patch.object(shared_review, "normalize_vendor_choice", side_effect=lambda value: value),
        mock.patch.object(shared_review, "normalize_model_choice", side_effect=lambda _vendor, value: value),
        mock.patch.object(shared_review, "normalize_effort_choice", side_effect=lambda _vendor, _model, value: value),
        mock.patch.object(shared_review, "get_default_model_for_vendor", return_value="model"),
    ):
        config = resolve_stage_agent_config(args, stage_key="development")
    assert config.main and config.main.codegraph_mode == "required"
    assert config.reviewer_selection("R1").codegraph_config == config.main.codegraph_config
    assert config.main.codegraph_config["max_files"] == 4

    with pytest.raises(RuntimeError, match="不支持角色级 codegraph_mode"):
        parse_agent_selection_spec(
            {"vendor": "codex", "model": "model", "codegraph_mode": "off"},
            source="main",
        )


def test_all_direct_stage_parsers_accept_new_and_legacy_flags() -> None:
    parsers = (
        workflow_entry.build_parser(), routing.build_parser(), intake.build_parser(), clarification.build_parser(),
        requirements_review.build_parser(), detailed_design.build_parser(), task_split.build_parser(),
        development.build_parser(), overall_review.build_parser(),
    )
    for parser in parsers:
        assert parser.parse_args(["--codegraph-mode", "required"]).codegraph_mode == "required"
        assert parser.parse_args(["--graphify-mode", "auto"]).graphify_mode == "auto"


@pytest.mark.parametrize("vendor", ("codex", "claude", "gemini", "opencode", "mimo", "agy", "deveco"))
def test_all_vendor_commands_receive_same_readonly_environment(vendor: str) -> None:
    command = _config(vendor, mode="auto").build_launch_command(Path("/tmp/project"))
    assert "TMUX_CODEGRAPH_CMD=" in command
    assert "TMUX_CODEGRAPH_PROJECT_DIR=/tmp/project" in command
    assert "TMUX_CODEGRAPH_MODE=auto" in command
    assert "TMUX_CODEGRAPH_READ_ONLY=1" in command
    assert "CODEGRAPH_DIR=.codegraph" in command
    assert "CODEGRAPH_TELEMETRY=0" in command
    assert "CODEGRAPH_NO_DAEMON=1" in command
    assert "CODEGRAPH_NO_UPDATE_CHECK=1" in command
    assert "CODEGRAPH_NO_PROMPT_HOOK=1" in command
    assert "DO_NOT_TRACK=1" in command
    assert "NO_COLOR=1" in command
    assert "CODEGRAPH_QUERY_LOG_DISABLE" not in command


def test_prompt_order_and_full_then_reminder() -> None:
    worker = _prompt_worker()
    profile = _profile()
    first = worker._build_turn_prompt(  # noqa: SLF001
        "Do the task", "[[DONE]]", (), task_status_path=Path("status.json"),
        include_turn_protocol=True, grill_profile=GrillTurnProfile("grill", 1), codegraph_profile=profile,
    )
    assert first.index(PONYTAIL_BEGIN_MARKER) < first.index(GRILL_BEGIN_MARKER)
    assert first.index(GRILL_BEGIN_MARKER) < first.index(CODEGRAPH_FULL_GUIDE_MARKER)
    assert first.index(CODEGRAPH_FULL_GUIDE_MARKER) < first.index("Do the task")
    assert first.index("Do the task") < first.index("Turn completion protocol")

    worker.codegraph_orientation_delivered = True
    worker.codegraph_delivered_mode = "auto"
    worker.codegraph_delivered_guide_version = CODEGRAPH_GUIDE_VERSION
    second = worker._build_turn_prompt(  # noqa: SLF001
        "Second", "[[DONE]]", (), task_status_path=Path("status.json"), include_turn_protocol=False,
        codegraph_profile=profile,
    )
    assert "SHORT REMINDER" in second
    assert CODEGRAPH_FULL_GUIDE_MARKER not in second


def test_orientation_latches_only_after_confirmed_full_submission() -> None:
    worker = _prompt_worker()
    worker._persist_codegraph_policy_fast = mock.Mock()  # type: ignore[method-assign]
    profile = _profile()
    worker._confirm_codegraph_orientation_delivery("ordinary", profile, prompt_kind="full")  # noqa: SLF001
    assert worker.codegraph_orientation_delivered
    worker._persist_codegraph_policy_fast.assert_called_once()

    uncertain = _prompt_worker()
    uncertain._persist_codegraph_policy_fast = mock.Mock()  # type: ignore[method-assign]
    uncertain._confirm_codegraph_orientation_delivery("ordinary", profile, prompt_kind="")  # noqa: SLF001
    assert not uncertain.codegraph_orientation_delivered


def test_run_turn_has_exactly_one_business_turn_without_usage_repair() -> None:
    worker = object.__new__(TmuxBatchWorker)
    worker._runtime_intervention_handler = None
    profile = _profile()
    result = CommandResult(
        label="task", command="prompt", exit_code=0, clean_output="done", raw_output="done",
        started_at="2026-08-10T00:00:00Z", finished_at="2026-08-10T00:00:01Z",
    )
    worker._resolve_codegraph_profile_for_turn = mock.Mock(return_value=profile)  # type: ignore[method-assign]
    worker._run_turn_impl = mock.Mock(return_value=result)  # type: ignore[method-assign]
    returned = worker.run_turn(label="task", prompt="business")
    assert returned is result
    worker._run_turn_impl.assert_called_once()
    assert not hasattr(worker, "_enforce_codegraph_usage_after_turn")


def test_off_profile_is_noop_without_tool_probe() -> None:
    worker = object.__new__(TmuxBatchWorker)
    worker.config = SimpleNamespace(codegraph_mode="off", codegraph_config={})
    worker.codegraph_mode = "off"
    with mock.patch("tmux_core.runtime.tmux_runtime.resolve_codegraph_turn_profile") as resolver:
        profile = worker.prepare_codegraph_turn_profile("task")
    assert profile.mode == "off"
    resolver.assert_not_called()


def test_required_preflight_fails_before_tmux_mutation() -> None:
    worker = object.__new__(TmuxBatchWorker)
    worker.config = SimpleNamespace(codegraph_mode="required", codegraph_config={})
    worker.codegraph_mode = "required"
    worker.refresh_codegraph_generation = mock.Mock(side_effect=CodeGraphUnavailable("index missing"))  # type: ignore[method-assign]
    worker.create_session = mock.Mock()  # type: ignore[method-assign]
    worker.work_dir = Path("/tmp/project")
    with mock.patch("tmux_core.runtime.tmux_runtime.codegraph_interactive_recovery_enabled", return_value=False):
        with pytest.raises(CodeGraphUnavailable, match="index missing"):
            worker.launch_agent()
    worker.create_session.assert_not_called()


def test_enabled_worker_construction_does_not_claim_sync_started(tmp_path: Path) -> None:
    config = _config(mode="auto")
    with (
        mock.patch(
            "tmux_core.runtime.codegraph.publish_codegraph_pending_status"
        ) as publish,
        mock.patch(
            "tmux_core.runtime.tmux_runtime.build_session_name",
            return_value="codegraph-worker",
        ),
    ):
        worker = TmuxBatchWorker(
            worker_id="codegraph-worker",
            work_dir=tmp_path,
            config=config,
            runtime_root=tmp_path / "runtime",
        )
    publish.assert_not_called()
    assert worker.codegraph_mode == "auto"


def test_development_default_selection_preserves_codegraph_config() -> None:
    selection = development._reviewer_default_selection(  # noqa: SLF001
        "full",
        "required",
        {"max_files": 4, "max_output_chars": 9000},
    )
    assert selection.codegraph_mode == "required"
    assert selection.codegraph_config == {
        "max_files": 4,
        "max_output_chars": 9000,
    }


def test_replacement_selection_preserves_codegraph_config() -> None:
    previous = ReviewAgentSelection(
        "codex",
        "old-model",
        "high",
        "",
        "full",
        "required",
        {"max_files": 4, "max_output_chars": 9000},
    )
    replacement = ReviewAgentSelection("claude", "new-model", "high", "")
    with (
        mock.patch.object(requirements_review, "prompt_yes_no_choice", return_value=True),
        mock.patch.object(
            requirements_review,
            "prompt_review_agent_selection",
            return_value=replacement,
        ) as prompt,
    ):
        result = requirements_review.prompt_replacement_review_agent_selection(
            reason_text="replace",
            previous_selection=previous,
            force_model_change=True,
            role_label="reviewer",
        )
    assert result is not None
    assert result.codegraph_mode == "required"
    assert result.codegraph_config == previous.codegraph_config
    assert prompt.call_args.kwargs["default_codegraph_config"] == previous.codegraph_config


def test_auto_interactive_can_degrade_without_install(tmp_path: Path) -> None:
    worker = object.__new__(TmuxBatchWorker)
    worker.work_dir = tmp_path
    worker.codegraph_mode = "auto"
    worker.stage_runner_id = "runner-auto"
    worker.config = _config(mode="auto")
    unavailable = SimpleNamespace(compatible=False, error="missing")
    with (
        mock.patch("tmux_core.runtime.tmux_runtime.codegraph_interactive_recovery_enabled", return_value=True),
        mock.patch("tmux_core.runtime.tmux_runtime.resolve_codegraph_tool", return_value=unavailable),
        mock.patch("T09_terminal_ops.prompt_select_option", return_value="continue_auto") as prompt,
        mock.patch("tmux_core.runtime.tmux_runtime.setup_managed_codegraph") as setup,
    ):
        worker._run_auto_codegraph_availability_decision()  # noqa: SLF001
    prompt.assert_called_once()
    setup.assert_not_called()
    assert worker.codegraph_mode == "auto"


def test_parallel_stage_workers_share_auto_recovery_decision_without_stage_seq(tmp_path: Path) -> None:
    def worker(runtime_name: str) -> TmuxBatchWorker:
        item = object.__new__(TmuxBatchWorker)
        item.work_dir = tmp_path
        item.runtime_dir = tmp_path / runtime_name
        item.codegraph_mode = "auto"
        item.stage_runner_id = ""
        item._runtime_metadata = {
            "workflow_action": "stage.a07.start",
            "requirement_name": "RequirementA",
        }
        item.config = _config(mode="auto")
        return item

    first = worker("runtime-one")
    second = worker("runtime-two")
    unavailable = SimpleNamespace(compatible=False, error="missing")
    with (
        mock.patch("tmux_core.runtime.tmux_runtime.codegraph_interactive_recovery_enabled", return_value=True),
        mock.patch("tmux_core.runtime.tmux_runtime.resolve_codegraph_tool", return_value=unavailable),
        mock.patch("T09_terminal_ops.prompt_select_option", return_value="continue_auto") as prompt,
    ):
        first._run_auto_codegraph_availability_decision()  # noqa: SLF001
        second._run_auto_codegraph_availability_decision()  # noqa: SLF001
    assert first._codegraph_recovery_interaction_scope() == second._codegraph_recovery_interaction_scope()  # noqa: SLF001
    prompt.assert_called_once()


def test_recovery_decisions_are_runner_scoped(tmp_path: Path) -> None:
    set_codegraph_required_recovery_decision(tmp_path, "auto", interaction_scope="old")
    assert codegraph_required_recovery_decision(tmp_path, interaction_scope="old") == "auto"
    assert codegraph_required_recovery_decision(tmp_path, interaction_scope="new") == ""


def test_checkpoint_syncs_once_and_marks_peer() -> None:
    leader = SimpleNamespace(codegraph_mode="auto", refresh_codegraph_generation=mock.Mock(return_value="profile"), mark_codegraph_generation_current=mock.Mock())
    peer = SimpleNamespace(codegraph_mode="auto", refresh_codegraph_generation=mock.Mock(), mark_codegraph_generation_current=mock.Mock())
    off = SimpleNamespace(codegraph_mode="off", refresh_codegraph_generation=mock.Mock(), mark_codegraph_generation_current=mock.Mock())
    context = CodeGraphTurnContext("A08", "checkpoint", "reviewer", CodeGraphQueryIntent.WHOLE_CHANGE_REVIEW)
    assert shared_review.refresh_codegraph_workers_for_checkpoint([leader, peer, off], prompt="checkpoint", turn_context=context) == "profile"
    leader.refresh_codegraph_generation.assert_called_once_with("checkpoint", turn_context=context)
    peer.refresh_codegraph_generation.assert_not_called()
    peer.mark_codegraph_generation_current.assert_called_once_with(turn_context=context)
    off.refresh_codegraph_generation.assert_not_called()
