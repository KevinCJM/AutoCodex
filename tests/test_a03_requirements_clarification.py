from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from A03_RequirementsClarification import (
    build_parser,
    collect_requirements_clarification_agent_selection,
    collect_auto_requirements_hitl_response,
    render_requirements_clarification_progress_line,
    run_requirements_clarification_stage,
    RequirementsClarificationStageResult,
)


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
