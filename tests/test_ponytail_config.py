from __future__ import annotations

import argparse
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import A01_Routing_LayerPlanning as routing
import A02_RequirementIntake as intake
import A03_RequirementsClarification as clarification
from tmux_core.runtime.ponytail import PonytailMode
from tmux_core.stage_kernel import detailed_design, development, overall_review, requirements_review, task_split
from tmux_core.stage_kernel.shared_review import (
    ReviewAgentSelection,
    resolve_main_ponytail_mode,
    resolve_stage_agent_config,
    resolve_workflow_ponytail_mode,
)


class PonytailConfigTests(unittest.TestCase):
    @staticmethod
    def _write_config(root: str | Path, payload: dict[str, object]) -> Path:
        path = Path(root) / "agents.json"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path

    def test_legacy_selection_defaults_to_off(self):
        selection = ReviewAgentSelection("codex", "gpt-5.4", "high", "")
        self.assertEqual(selection.ponytail_mode, PonytailMode.OFF.value)

    def test_cli_global_and_role_overrides_follow_precedence(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = self._write_config(
                tmpdir,
                {
                    "ponytail_mode": "full",
                    "stages": {
                        "development": {
                            "main": {
                                "vendor": "codex",
                                "model": "gpt-5.4",
                                "effort": "high",
                                "ponytail_mode": "ultra",
                            },
                            "reviewers": [
                                {
                                    "name": "R1",
                                    "vendor": "codex",
                                    "model": "gpt-5.4-mini",
                                    "effort": "medium",
                                    "ponytail": "lite",
                                }
                            ],
                        }
                    },
                },
            )
            args = argparse.Namespace(
                agent_config=str(path),
                ponytail_mode="off",
                main_ponytail_mode="",
                main_agent="",
                reviewer_agent=[],
                yes=True,
            )
            with patch("tmux_core.stage_kernel.shared_review.normalize_vendor_choice", side_effect=lambda value: value), patch(
                "tmux_core.stage_kernel.shared_review.normalize_model_choice",
                side_effect=lambda _vendor, value: value,
            ), patch(
                "tmux_core.stage_kernel.shared_review.normalize_effort_choice",
                side_effect=lambda _vendor, _model, value: value,
            ), patch(
                "tmux_core.stage_kernel.shared_review.get_default_model_for_vendor",
                return_value="gpt-5.4",
            ):
                config = resolve_stage_agent_config(args, stage_key="development")

            self.assertEqual(config.ponytail_mode, "off")
            self.assertEqual(config.main.ponytail_mode, "ultra")
            self.assertEqual(config.reviewer_selection("R1").ponytail_mode, "lite")
            self.assertEqual(resolve_main_ponytail_mode(args, agent_config=config), "ultra")

    def test_workflow_interactive_choice_is_cached_once(self):
        args = argparse.Namespace(agent_config="", ponytail_mode="", yes=False)
        with patch("tmux_core.stage_kernel.shared_review.stdin_is_interactive", return_value=True), patch(
            "tmux_core.stage_kernel.shared_review.prompt_ponytail_mode",
            return_value="lite",
        ) as prompt:
            self.assertEqual(resolve_workflow_ponytail_mode(args), "lite")
            self.assertEqual(resolve_workflow_ponytail_mode(args), "lite")
        prompt.assert_called_once_with("full")

    def test_yes_and_noninteractive_default_to_full(self):
        yes_args = argparse.Namespace(agent_config="", ponytail_mode="", yes=True)
        self.assertEqual(resolve_workflow_ponytail_mode(yes_args), "full")

        noninteractive_args = argparse.Namespace(agent_config="", ponytail_mode="", yes=False)
        with patch("tmux_core.stage_kernel.shared_review.stdin_is_interactive", return_value=False):
            self.assertEqual(resolve_workflow_ponytail_mode(noninteractive_args), "full")

    def test_direct_stage_parsers_accept_agent_config_and_mode(self):
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
            args = parser.parse_args(["--agent-config", "/tmp/agents.json", "--ponytail-mode", "lite"])
            self.assertEqual(args.agent_config, "/tmp/agents.json")
            self.assertEqual(args.ponytail_mode, "lite")

    def test_invalid_mode_is_rejected_before_stage_execution(self):
        with self.assertRaises(SystemExit):
            development.build_parser().parse_args(["--ponytail-mode", "invalid"])

    def test_development_resume_rejects_legacy_off_worker_for_full_policy(self):
        state = {
            "agent_role": "developer",
            "worker_id": development.build_developer_worker_id(),
            "config": {
                "vendor": "codex",
                "model": "gpt-5.4",
                "reasoning_effort": "high",
                # Missing ponytail_mode is intentionally decoded as legacy Off.
            },
        }
        with tempfile.TemporaryDirectory() as tmpdir, patch.object(
            development,
            "_iter_scoped_development_worker_states",
            return_value=[(Path(tmpdir) / "worker.state.json", state)],
        ), patch.object(development, "_recover_worker_from_state") as recover_worker:
            resumed = development._recover_development_runtime_resume(
                project_dir=tmpdir,
                requirement_name="R",
                paths={},
                reviewer_specs_by_name={},
                developer_selection=ReviewAgentSelection(
                    "codex",
                    "gpt-5.4",
                    "high",
                    "",
                    "full",
                ),
                reviewer_selections_by_name={},
            )
        self.assertIsNone(resumed)
        recover_worker.assert_not_called()


if __name__ == "__main__":
    unittest.main()
