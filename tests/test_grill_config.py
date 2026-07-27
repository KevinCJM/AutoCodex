from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tmux_core.stage_kernel.shared_review import (
    configured_workflow_requirements_mode,
    resolve_workflow_requirements_mode,
)


class GrillConfigTests(unittest.TestCase):
    @staticmethod
    def _config_file(payload: dict[str, object], root: str) -> str:
        path = Path(root) / "agent-config.json"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return str(path)

    def test_cli_overrides_stage_and_root_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = self._config_file(
                {
                    "requirements_mode": "grill",
                    "stages": {"requirements_clarification": {"requirements_mode": "grill-with-docs"}},
                },
                tmpdir,
            )
            args = SimpleNamespace(
                requirements_mode="standard",
                agent_config=config,
                yes=False,
            )
            with patch("tmux_core.stage_kernel.shared_review.stdin_is_interactive", return_value=True):
                self.assertEqual(resolve_workflow_requirements_mode(args), "standard")

    def test_stage_config_overrides_root_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = self._config_file(
                {
                    "requirements_mode": "grill",
                    "stages": {"requirements_clarification": {"requirements_mode": "grill-with-docs"}},
                },
                tmpdir,
            )
            args = SimpleNamespace(requirements_mode="", agent_config=config, yes=False)
            with patch("tmux_core.stage_kernel.shared_review.stdin_is_interactive", return_value=True):
                self.assertEqual(resolve_workflow_requirements_mode(args), "grill-with-docs")

    def test_interactive_default_is_prompted_once_and_cached(self) -> None:
        args = SimpleNamespace(requirements_mode="", agent_config="", yes=False)
        with (
            patch("tmux_core.stage_kernel.shared_review.stdin_is_interactive", return_value=True),
            patch(
                "tmux_core.stage_kernel.shared_review.prompt_requirements_mode",
                return_value="grill",
            ) as prompt,
        ):
            self.assertEqual(resolve_workflow_requirements_mode(args), "grill")
            self.assertEqual(resolve_workflow_requirements_mode(args), "grill")
        prompt.assert_called_once()

    def test_configured_mode_lookup_never_prompts_or_applies_a_default(self) -> None:
        args = SimpleNamespace(requirements_mode="", agent_config="", yes=False)
        with patch("tmux_core.stage_kernel.shared_review.prompt_requirements_mode") as prompt:
            self.assertEqual(configured_workflow_requirements_mode(args), "")
        prompt.assert_not_called()

    def test_configured_mode_lookup_keeps_cli_stage_root_precedence(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = self._config_file(
                {
                    "requirements_mode": "grill",
                    "stages": {"requirements_clarification": {"requirements_mode": "grill-with-docs"}},
                },
                tmpdir,
            )
            self.assertEqual(
                configured_workflow_requirements_mode(
                    SimpleNamespace(requirements_mode="", agent_config=config),
                ),
                "grill-with-docs",
            )
            self.assertEqual(
                configured_workflow_requirements_mode(
                    SimpleNamespace(requirements_mode="standard", agent_config=config),
                ),
                "standard",
            )

    def test_yes_or_headless_defaults_to_standard(self) -> None:
        yes_args = SimpleNamespace(requirements_mode="", agent_config="", yes=True)
        headless_args = SimpleNamespace(requirements_mode="", agent_config="", yes=False)
        with patch("tmux_core.stage_kernel.shared_review.stdin_is_interactive", return_value=True):
            self.assertEqual(resolve_workflow_requirements_mode(yes_args), "standard")
        with patch("tmux_core.stage_kernel.shared_review.stdin_is_interactive", return_value=False):
            self.assertEqual(resolve_workflow_requirements_mode(headless_args), "standard")

    def test_explicit_grill_rejects_yes_or_headless_execution(self) -> None:
        for args, interactive in (
            (SimpleNamespace(requirements_mode="grill", agent_config="", yes=True), True),
            (SimpleNamespace(requirements_mode="grill-with-docs", agent_config="", yes=False), False),
        ):
            with self.subTest(args=args, interactive=interactive):
                with (
                    patch(
                        "tmux_core.stage_kernel.shared_review.stdin_is_interactive",
                        return_value=interactive,
                    ),
                    self.assertRaisesRegex(RuntimeError, "必须由人类逐题确认"),
                ):
                    resolve_workflow_requirements_mode(args)


if __name__ == "__main__":
    unittest.main()
