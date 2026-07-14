from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from tempfile import TemporaryDirectory
from unittest.mock import patch

from A03_RequirementsClarification import (
    build_parser,
    collect_requirements_clarification_agent_selection,
    collect_auto_requirements_hitl_response,
    preserve_worker_after_unhandled_stage_failure,
    render_requirements_clarification_progress_line,
    run_requirements_clarification,
    run_requirements_clarification_stage,
    RequirementsClarificationStageResult,
)
from tmux_core.runtime.tmux_runtime import TmuxControlUnavailable, TmuxMutationOutcomeUnknown


class PreserveFailedWorkerTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
