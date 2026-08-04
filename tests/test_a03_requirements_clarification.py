from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from tempfile import TemporaryDirectory
from unittest.mock import patch

from A03_RequirementsClarification import (
    build_requirements_grill_paths,
    build_parser,
    collect_requirements_clarification_agent_selection,
    collect_auto_requirements_hitl_response,
    load_persisted_requirements_grill_selection,
    preserve_worker_after_unhandled_stage_failure,
    restore_live_requirements_grill_worker,
    render_requirements_clarification_progress_line,
    run_requirements_clarification,
    run_requirements_clarification_stage,
    RequirementsClarificationStageResult,
)
from T05_hitl_runtime import GrillSessionState, save_grill_session_state
from tmux_core.runtime.tmux_runtime import TmuxControlUnavailable, TmuxMutationOutcomeUnknown


class PreserveFailedWorkerTests(unittest.TestCase):
    def test_active_grill_worker_selection_is_frozen_for_dead_pane_recreate(self):
        with TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir).resolve()
            runtime_root = project_dir / ".requirements_clarification_runtime"
            state_path = runtime_root / "requirements-old" / "worker.state.json"
            state_path.parent.mkdir(parents=True)
            state_path.write_text(
                """{
  "work_dir": "%s",
  "config": {
    "vendor": "deveco",
    "model": "deveco/model-a",
    "reasoning_effort": "max",
    "proxy_url": "http://127.0.0.1:10900",
    "ponytail_mode": "full",
    "requirements_mode": "grill-with-docs"
  }
}
""" % project_dir,
                encoding="utf-8",
            )

            selection = load_persisted_requirements_grill_selection(
                project_dir=project_dir,
                runtime_root=runtime_root,
                state_path=state_path,
                requirements_mode="grill-with-docs",
            )

            self.assertIsNotNone(selection)
            self.assertEqual(selection.vendor, "deveco")
            self.assertEqual(selection.model, "deveco/model-a")
            self.assertEqual(selection.reasoning_effort, "max")
            self.assertEqual(selection.ponytail_mode, "full")

    def test_restore_live_grill_worker_never_treats_tmux_unavailable_as_dead(self):
        with TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            runtime_root = project_dir / ".requirements_clarification_runtime"
            state_path = runtime_root / "worker-old" / "worker.state.json"
            state_path.parent.mkdir(parents=True)
            state_path.write_text("{}", encoding="utf-8")

            class UnavailableWorker:
                work_dir = project_dir.resolve()
                config = SimpleNamespace(requirements_mode="grill")

                def session_exists(self):
                    raise TmuxControlUnavailable(
                        operation="has-session",
                        error="timeout",
                        elapsed_sec=60.0,
                        attempts=4,
                    )

            with patch(
                "A03_RequirementsClarification.load_worker_from_state_path",
                return_value=UnavailableWorker(),
            ):
                with self.assertRaises(TmuxControlUnavailable):
                    restore_live_requirements_grill_worker(
                        project_dir=project_dir,
                        runtime_root=runtime_root,
                        state_path=state_path,
                        requirements_mode="grill",
                    )

    def test_dead_replacement_keeps_original_grill_contract_paths(self):
        with TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            requirement_name = "需求A"
            (project_dir / f"{requirement_name}_原始需求.md").write_text(
                "原始需求\n", encoding="utf-8"
            )
            runtime_root = project_dir / ".requirements_clarification_runtime"
            old_runtime = runtime_root / "requirements-old"
            old_turns = old_runtime / "turns"
            old_turn_path = old_turns / "requirements_clarification_1" / "turn_status.json"
            old_stage_path = old_runtime / "requirements_clarification_status.json"
            old_state_path = old_runtime / "worker.state.json"
            old_runtime.mkdir(parents=True)
            old_state_path.write_text("{}", encoding="utf-8")
            _, session_path, _ = build_requirements_grill_paths(project_dir, requirement_name)
            save_grill_session_state(
                session_path,
                GrillSessionState(
                    session_id="old-contract",
                    requirements_mode="grill",
                    state="turn_in_progress",
                    turn_seq=1,
                    active_worker_state_path=str(old_state_path.resolve()),
                    active_runtime_dir=str(old_runtime.resolve()),
                    active_session_name="dead-session",
                    active_pane_id="%1",
                    turn_id="requirements_clarification_1",
                    turn_label="requirements_clarification_round_1",
                    turn_status_path=str(old_turn_path.resolve()),
                    turn_stage_status_path=str(old_stage_path.resolve()),
                    turn_submission_cursor="submitted",
                ),
            )
            captured: dict[str, object] = {}
            created_workers: list[object] = []

            class FakeAgentRunConfig:
                def __init__(self, **kwargs):  # noqa: ANN003
                    self.vendor = SimpleNamespace(value=str(kwargs["vendor"]))
                    self.model = str(kwargs["model"])
                    self.reasoning_effort = str(kwargs["reasoning_effort"])
                    self.proxy_url = str(kwargs.get("proxy_url", ""))
                    self.ponytail_mode = str(kwargs.get("ponytail_mode", "off"))
                    self.requirements_mode = str(
                        kwargs.get("requirements_mode", "standard")
                    )

            class ReplacementWorker:
                def __init__(self, **kwargs):  # noqa: ANN003
                    self.runtime_dir = runtime_root / f"requirements-new-{len(created_workers)}"
                    self.runtime_dir.mkdir(parents=True)
                    self.state_path = self.runtime_dir / "worker.state.json"
                    self.session_name = f"replacement-session-{len(created_workers)}"
                    self.pane_id = ""
                    self.config = kwargs["config"]
                    created_workers.append(self)

                def set_runtime_metadata(self, **_metadata):  # noqa: ANN003
                    return None

                def request_kill(self):
                    return None

            def complete_turn(**kwargs):  # noqa: ANN003
                captured.update(kwargs)
                replacement = kwargs["replace_dead_worker"](
                    kwargs["worker"],
                    RuntimeError("tmux pane died"),
                )
                captured["replacement"] = replacement
                (project_dir / f"{requirement_name}_需求澄清.md").write_text(
                    "已完成\n", encoding="utf-8"
                )
                return SimpleNamespace(
                    decision=SimpleNamespace(payload={"status": "completed"}, summary=""),
                )

            with patch(
                "A03_RequirementsClarification.restore_live_requirements_grill_worker",
                return_value=None,
            ), patch(
                "A03_RequirementsClarification.TmuxBatchWorker",
                ReplacementWorker,
            ), patch(
                "A03_RequirementsClarification.AgentRunConfig",
                FakeAgentRunConfig,
            ), patch(
                "A03_RequirementsClarification.run_hitl_agent_loop",
                side_effect=complete_turn,
            ):
                run_requirements_clarification(
                    project_dir,
                    requirement_name,
                    vendor="codex",
                    model="model",
                    reasoning_effort="high",
                    requirements_mode="grill",
                )

            self.assertEqual(Path(captured["stage_status_path"]).resolve(), old_stage_path.resolve())
            self.assertEqual(Path(captured["turns_root"]).resolve(), old_turns.resolve())
            self.assertEqual(len(created_workers), 2)
            self.assertIs(created_workers[1].config, created_workers[0].config)
            self.assertEqual(created_workers[1].config.requirements_mode, "grill")

    def test_confirmed_grill_preserves_same_worker_and_switches_to_standard_behavior(self):
        with TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir).resolve()
            requirement_name = "需求A"
            (project_dir / f"{requirement_name}_原始需求.md").write_text(
                "原始需求\n", encoding="utf-8"
            )
            requirements_clear_path = project_dir / f"{requirement_name}_需求澄清.md"
            _, session_path, _ = build_requirements_grill_paths(
                project_dir,
                requirement_name,
            )
            created_workers: list[object] = []

            class FakeWorker:
                def __init__(self, **kwargs):  # noqa: ANN003
                    self.config = kwargs["config"]
                    self.runtime_root = Path(kwargs["runtime_root"])
                    self.runtime_dir = self.runtime_root / "requirements-live"
                    self.runtime_dir.mkdir(parents=True)
                    self.state_path = self.runtime_dir / "worker.state.json"
                    self.session_name = "需求分析师-天哭星"
                    self.pane_id = "%1"
                    self.grill_session_generation = "generation-a03"
                    self.transition_calls: list[tuple[str, str, str]] = []
                    self.killed = False
                    created_workers.append(self)

                def set_runtime_metadata(self, **_metadata):  # noqa: ANN003
                    return None

                def transition_requirements_behavior(
                    self,
                    behavior,
                    *,
                    grill_session_id="",
                    expected_session_generation="",
                ):
                    self.transition_calls.append(
                        (behavior, grill_session_id, expected_session_generation)
                    )

                def request_kill(self):
                    self.killed = True

            def complete_grill(**_kwargs):  # noqa: ANN003
                requirements_clear_path.write_text("已确认需求\n", encoding="utf-8")
                save_grill_session_state(
                    session_path,
                    GrillSessionState(
                        session_id="grill-session-a03",
                        requirements_mode="grill",
                        state="confirmed",
                    ),
                )
                return SimpleNamespace(
                    decision=SimpleNamespace(payload={"status": "completed"}, summary=""),
                )

            with patch(
                "A03_RequirementsClarification.TmuxBatchWorker",
                FakeWorker,
            ), patch(
                "A03_RequirementsClarification.AgentRunConfig",
                side_effect=lambda **kwargs: SimpleNamespace(
                    vendor=SimpleNamespace(value=str(kwargs["vendor"])),
                    model=str(kwargs["model"]),
                    reasoning_effort=str(kwargs["reasoning_effort"]),
                    proxy_url=str(kwargs.get("proxy_url", "")),
                    ponytail_mode=str(kwargs.get("ponytail_mode", "off")),
                    requirements_mode=str(kwargs.get("requirements_mode", "standard")),
                    graphify_mode=str(kwargs.get("graphify_mode", "off")),
                    graphify_config=dict(kwargs.get("graphify_config", {})),
                ),
            ), patch(
                "A03_RequirementsClarification.run_hitl_agent_loop",
                side_effect=complete_grill,
            ):
                result = run_requirements_clarification(
                    project_dir,
                    requirement_name,
                    requirements_mode="grill",
                    preserve_ba_worker=True,
                )

            self.assertEqual(len(created_workers), 1)
            worker = created_workers[0]
            self.assertIs(result.ba_handoff.worker, worker)
            self.assertEqual(result.ba_handoff.requirements_mode, "grill")
            self.assertEqual(result.ba_handoff.requirements_behavior, "standard")
            self.assertEqual(
                worker.transition_calls,
                [("standard", "grill-session-a03", "generation-a03")],
            )
            self.assertFalse(worker.killed)

    def test_stage_cleanup_never_removes_other_requirement_runtime(self):
        with TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            (project_dir / "需求A_原始需求.md").write_text("原始需求\n", encoding="utf-8")
            clear_path = project_dir / "需求A_需求澄清.md"
            runtime_root = project_dir / ".requirements_clarification_runtime"
            current_runtime = runtime_root / "current-worker"
            foreign_runtime = runtime_root / "other-requirement-worker"
            current_runtime.mkdir(parents=True)
            foreign_runtime.mkdir(parents=True)
            (foreign_runtime / "worker.state.json").write_text("{}", encoding="utf-8")

            def complete_stage(*_args, **_kwargs):  # noqa: ANN002, ANN003
                clear_path.write_text("已完成澄清\n", encoding="utf-8")
                return RequirementsClarificationStageResult(
                    project_dir=str(project_dir),
                    requirement_name="需求A",
                    requirements_clear_path=str(clear_path),
                    cleanup_paths=(str(current_runtime),),
                )

            selection = SimpleNamespace(
                vendor="codex",
                model="gpt-5.4",
                reasoning_effort="high",
                proxy_url="",
            )
            with patch(
                "A03_RequirementsClarification.collect_requirements_clarification_agent_selection",
                return_value=selection,
            ), patch(
                "A03_RequirementsClarification.run_requirements_clarification",
                side_effect=complete_stage,
            ):
                run_requirements_clarification_stage(
                    ["--project-dir", str(project_dir), "--requirement-name", "需求A"],
                )

            self.assertFalse(current_runtime.exists())
            self.assertTrue(foreign_runtime.exists())
            self.assertTrue(runtime_root.exists())

    def test_successful_turn_preserves_runtime_when_worker_kill_is_uncertain(self):
        errors = (
            TmuxControlUnavailable(
                operation="has-session",
                error="timeout",
                elapsed_sec=60.0,
                attempts=4,
            ),
            TmuxMutationOutcomeUnknown(operation="kill-session", error="timeout"),
        )
        for error in errors:
            with self.subTest(error=type(error).__name__), TemporaryDirectory() as tmpdir:
                project_dir = Path(tmpdir)
                requirements_clear_path = project_dir / "需求A_需求澄清.md"
                (project_dir / "需求A_原始需求.md").write_text("原始需求\n", encoding="utf-8")

                class FakeWorker:
                    def __init__(self, **_kwargs):  # noqa: ANN003
                        self.runtime_dir = project_dir / ".requirements_clarification_runtime" / "worker"
                        self.runtime_dir.mkdir(parents=True)
                        self.session_name = "需求分析师-天哭星"

                    def set_runtime_metadata(self, **_metadata):  # noqa: ANN003
                        return None

                    def request_kill(self):
                        raise error

                def complete_turn(**_kwargs):  # noqa: ANN003
                    requirements_clear_path.write_text("已完成澄清\n", encoding="utf-8")
                    return SimpleNamespace(
                        decision=SimpleNamespace(payload={"status": "completed"}, summary=""),
                    )

                with patch("A03_RequirementsClarification.TmuxBatchWorker", FakeWorker), patch(
                    "A03_RequirementsClarification.AgentRunConfig",
                    side_effect=lambda **kwargs: SimpleNamespace(**kwargs),
                ), patch(
                    "A03_RequirementsClarification.run_hitl_agent_loop",
                    side_effect=complete_turn,
                ):
                    with self.assertRaises(type(error)):
                        run_requirements_clarification(project_dir, "需求A")

                self.assertTrue(
                    (project_dir / ".requirements_clarification_runtime" / "worker").exists()
                )

    def test_unhandled_failure_preserves_worker_without_marking_before_runner_commit(self):
        class FakeWorker:
            def mark_orphaned(self, reason: str) -> None:
                _ = reason
                raise AssertionError("stage must not mark orphaned before failure persistence")

        preserved = preserve_worker_after_unhandled_stage_failure(FakeWorker(), RuntimeError("boom"))  # type: ignore[arg-type]

        self.assertTrue(preserved)

    def test_worker_is_scoped_and_progress_stop_failure_does_not_replace_stage_error(self):
        with TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            (project_dir / "需求A_原始需求.md").write_text("原始需求\n", encoding="utf-8")

            class FakeWorker:
                def __init__(self, **_kwargs):  # noqa: ANN003
                    self.runtime_dir = project_dir / ".requirements_clarification_runtime" / "worker"
                    self.runtime_dir.mkdir(parents=True, exist_ok=True)
                    self.config = type(
                        "Config",
                        (),
                        {
                            "vendor": type("Vendor", (), {"value": "codex"})(),
                            "model": "gpt-5.4",
                            "reasoning_effort": "high",
                            "proxy_url": "",
                        },
                    )()
                    self.session_name = "需求分析师-天哭星"
                    self.metadata: dict[str, object] = {}
                    self.killed = False

                def set_runtime_metadata(self, **metadata):  # noqa: ANN003
                    self.metadata.update(metadata)

                def read_state(self):
                    return {}

                def request_kill(self):
                    self.killed = True

            fake_worker = FakeWorker()

            class FailingStopMonitor:
                def __init__(self, **_kwargs):  # noqa: ANN003
                    pass

                def start(self):
                    return None

                def stop(self):
                    raise BrokenPipeError("progress transport closed")

            def fail_after_progress_started(**kwargs):  # noqa: ANN003
                kwargs["on_worker_starting"](fake_worker)
                kwargs["on_worker_started"](fake_worker)
                kwargs["on_agent_turn_started"](object(), fake_worker)
                raise RuntimeError("original stage failure")

            with patch("A03_RequirementsClarification.TmuxBatchWorker", return_value=fake_worker), patch(
                "A03_RequirementsClarification.AgentRunConfig",
                side_effect=lambda **kwargs: SimpleNamespace(**kwargs),
            ), patch(
                "A03_RequirementsClarification.SingleLineSpinnerMonitor",
                FailingStopMonitor,
            ), patch(
                "A03_RequirementsClarification.run_hitl_agent_loop",
                side_effect=fail_after_progress_started,
            ):
                with self.assertRaisesRegex(RuntimeError, "original stage failure"):
                    run_requirements_clarification(project_dir, "需求A")

            self.assertEqual(fake_worker.metadata["project_dir"], str(project_dir.resolve()))
            self.assertEqual(fake_worker.metadata["requirement_name"], "需求A")
            self.assertEqual(fake_worker.metadata["workflow_action"], "stage.a03.start")
            self.assertFalse(fake_worker.killed)

    def test_unhandled_failure_preserves_worker_without_marker_api(self):
        class FakeWorker:
            pass

        preserved = preserve_worker_after_unhandled_stage_failure(FakeWorker(), RuntimeError("boom"))  # type: ignore[arg-type]

        self.assertTrue(preserved)


class RequirementsClarificationAgentSelectionTests(unittest.TestCase):
    def test_yes_with_complete_agent_args_skips_interactive_proxy_prompt(self):
        args = build_parser().parse_args(
            [
                "--vendor",
                "gemini",
                "--model",
                "flash",
                "--effort",
                "high",
                "--proxy-url",
                "10900",
                "--yes",
            ]
        )

        with patch("A03_RequirementsClarification.stdin_is_interactive", return_value=True), patch(
            "A03_RequirementsClarification.normalize_model_choice",
            return_value="flash",
        ), patch(
            "A03_RequirementsClarification.normalize_effort_choice",
            return_value="high",
        ), patch(
            "A03_RequirementsClarification.prompt_vendor",
            side_effect=AssertionError("vendor prompt should not be called"),
        ), patch(
            "A03_RequirementsClarification.prompt_model",
            side_effect=AssertionError("model prompt should not be called"),
        ), patch(
            "A03_RequirementsClarification.prompt_effort",
            side_effect=AssertionError("effort prompt should not be called"),
        ), patch(
            "A03_RequirementsClarification.prompt_proxy_url",
            side_effect=AssertionError("proxy prompt should not be called"),
        ):
            selection = collect_requirements_clarification_agent_selection(args)

        self.assertEqual(selection.vendor, "gemini")
        self.assertEqual(selection.model, "flash")
        self.assertEqual(selection.reasoning_effort, "high")
        self.assertEqual(selection.proxy_url, "10900")

    def test_auto_hitl_response_instructs_non_interactive_closure(self):
        with TemporaryDirectory() as tmpdir:
            question_path = Path(tmpdir) / "ask.md"
            question_path.write_text("需要确认边界\n", encoding="utf-8")

            response = collect_auto_requirements_hitl_response(question_path, hitl_round=1)

        self.assertIn("--yes", response)
        self.assertIn("不要再次发起 HITL", response)
        self.assertIn("需要确认边界", response)

    def test_render_requirements_clarification_progress_line_displays_prelaunch_dead_state_as_starting(self):
        class _Worker:
            def read_state(self):
                return {
                    "status": "running",
                    "result_status": "running",
                    "agent_state": "DEAD",
                    "agent_started": False,
                    "pane_id": "",
                    "health_status": "missing_session",
                    "note": "turn:requirements_clarification_round_1",
                    "workflow_stage": "pending",
                }

        text = render_requirements_clarification_progress_line(worker=_Worker(), requirement_name="需求A", tick=7)

        self.assertIn("需求A:running/STARTING", text)

    def test_stage_passes_auto_hitl_provider_when_yes(self):
        with TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            (project_dir / "slug_tool_原始需求.md").write_text("原始需求\n", encoding="utf-8")
            clear_path = project_dir / "slug_tool_需求澄清.md"
            captured_kwargs: dict[str, object] = {}

            def fake_run_requirements_clarification(*args, **kwargs):  # noqa: ANN002, ANN003
                captured_kwargs.update(kwargs)
                clear_path.write_text("需求澄清\n", encoding="utf-8")
                return RequirementsClarificationStageResult(
                    project_dir=str(project_dir),
                    requirement_name="slug_tool",
                    requirements_clear_path=str(clear_path),
                )

            with patch(
                "A03_RequirementsClarification.run_requirements_clarification",
                side_effect=fake_run_requirements_clarification,
            ), patch(
                "A03_RequirementsClarification.normalize_model_choice",
                return_value="flash",
            ), patch(
                "A03_RequirementsClarification.normalize_effort_choice",
                return_value="high",
            ):
                run_requirements_clarification_stage(
                    [
                        "--project-dir",
                        str(project_dir),
                        "--requirement-name",
                        "slug_tool",
                        "--vendor",
                        "gemini",
                        "--model",
                        "flash",
                        "--effort",
                        "high",
                        "--yes",
                    ],
                    preserve_ba_worker=False,
                )

        self.assertIs(captured_kwargs.get("human_input_provider"), collect_auto_requirements_hitl_response)

    def test_active_grill_session_owns_mode_and_cannot_reuse_unconfirmed_candidate(self):
        with TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            requirement_name = "需求A"
            (project_dir / f"{requirement_name}_原始需求.md").write_text(
                "原始需求\n", encoding="utf-8"
            )
            clear_path = project_dir / f"{requirement_name}_需求澄清.md"
            clear_path.write_text("尚未确认的候选\n", encoding="utf-8")
            _, session_path, _ = build_requirements_grill_paths(project_dir, requirement_name)
            save_grill_session_state(
                session_path,
                GrillSessionState(
                    session_id="active-session",
                    requirements_mode="grill-with-docs",
                    state="answer_pending",
                    turn_seq=2,
                    question_seq=1,
                    pending_answer="已持久化回答",
                ),
            )
            captured: dict[str, object] = {}
            selection = SimpleNamespace(
                vendor="codex",
                model="gpt-5.4",
                reasoning_effort="high",
                proxy_url="",
                ponytail_mode="full",
                requirements_mode="grill-with-docs",
            )

            def fake_run(*_args, **kwargs):  # noqa: ANN002, ANN003
                captured.update(kwargs)
                clear_path.write_text("已确认\n", encoding="utf-8")
                return RequirementsClarificationStageResult(
                    project_dir=str(project_dir),
                    requirement_name=requirement_name,
                    requirements_clear_path=str(clear_path),
                )

            with patch(
                "A03_RequirementsClarification.stdin_is_interactive",
                return_value=True,
            ), patch(
                "A03_RequirementsClarification.resolve_workflow_requirements_mode",
                side_effect=AssertionError("active session must bypass mode selection"),
            ), patch(
                "A03_RequirementsClarification.should_reuse_existing_requirements_clarification",
                side_effect=AssertionError("unconfirmed Grill candidate must not be reused"),
            ), patch(
                "A03_RequirementsClarification.collect_requirements_clarification_agent_selection",
                return_value=selection,
            ), patch(
                "A03_RequirementsClarification.run_requirements_clarification",
                side_effect=fake_run,
            ):
                run_requirements_clarification_stage(
                    [
                        "--project-dir",
                        str(project_dir),
                        "--requirement-name",
                        requirement_name,
                    ]
                )

            self.assertEqual(captured["requirements_mode"], "grill-with-docs")
            self.assertTrue(captured["resume_existing"])

    def test_confirmed_grill_reuse_recovers_live_worker_for_a00_handoff(self):
        with TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir).resolve()
            requirement_name = "需求A"
            (project_dir / f"{requirement_name}_原始需求.md").write_text(
                "原始需求\n", encoding="utf-8"
            )
            (project_dir / f"{requirement_name}_需求澄清.md").write_text(
                "已确认需求\n", encoding="utf-8"
            )
            worker_runtime_dir = project_dir / ".requirements_clarification_runtime" / "live"
            worker_runtime_dir.mkdir(parents=True)
            worker_state_path = worker_runtime_dir / "worker.state.json"
            worker_state_path.write_text("{}", encoding="utf-8")
            _, session_path, _ = build_requirements_grill_paths(
                project_dir,
                requirement_name,
            )
            save_grill_session_state(
                session_path,
                GrillSessionState(
                    session_id="confirmed-session",
                    requirements_mode="grill",
                    state="confirmed",
                    active_worker_state_path=str(worker_state_path),
                    active_runtime_dir=str(worker_runtime_dir),
                    active_session_name="需求分析师-天哭星",
                    active_pane_id="%1",
                    active_worker_generation="generation-a03",
                ),
            )

            class LiveWorker:
                state_path = worker_state_path
                runtime_dir = worker_runtime_dir
                runtime_root = worker_runtime_dir.parent
                session_name = "需求分析师-天哭星"
                grill_session_generation = "generation-a03"
                config = SimpleNamespace(
                    vendor=SimpleNamespace(value="codex"),
                    model="gpt-5.4",
                    reasoning_effort="high",
                    proxy_url="",
                    ponytail_mode="full",
                    graphify_mode="auto",
                    graphify_config={},
                    requirements_mode="grill",
                )

                def __init__(self):
                    self.transitions: list[tuple[str, str, str]] = []

                def transition_requirements_behavior(
                    self,
                    behavior,
                    *,
                    grill_session_id="",
                    expected_session_generation="",
                ):
                    self.transitions.append(
                        (behavior, grill_session_id, expected_session_generation)
                    )

            worker = LiveWorker()
            with patch(
                "A03_RequirementsClarification.stdin_is_interactive",
                return_value=True,
            ), patch(
                "A03_RequirementsClarification.resolve_workflow_requirements_mode",
                return_value="grill",
            ), patch(
                "A03_RequirementsClarification.should_reuse_existing_requirements_clarification",
                return_value=True,
            ), patch(
                "A03_RequirementsClarification.restore_live_requirements_grill_worker",
                return_value=worker,
            ), patch(
                "A03_RequirementsClarification.collect_requirements_clarification_agent_selection",
                side_effect=AssertionError("恢复存活 worker 时不应重新选择模型"),
            ):
                result = run_requirements_clarification_stage(
                    [
                        "--project-dir",
                        str(project_dir),
                        "--requirement-name",
                        requirement_name,
                        "--requirements-mode",
                        "grill",
                    ],
                    preserve_ba_worker=True,
                )

            self.assertIs(result.ba_handoff.worker, worker)
            self.assertEqual(result.ba_handoff.requirements_behavior, "standard")
            self.assertEqual(
                worker.transitions,
                [("standard", "confirmed-session", "generation-a03")],
            )


if __name__ == "__main__":
    unittest.main()
