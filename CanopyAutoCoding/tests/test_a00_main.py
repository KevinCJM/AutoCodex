from __future__ import annotations

import io
import json
import tempfile
import threading
import unittest
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from A00_main import (
    build_parser,
    build_pre_development_task_record_path,
    build_pre_development_task_record_payload,
    build_stage_args,
    clear_pending_tty_input,
    ensure_pre_development_task_record,
    main,
    render_remaining_stage_placeholders,
)
from canopy_core.stage_kernel.requirement_concurrency import requirement_concurrency_lock
from canopy_core.stage_kernel.shared_review import ReviewAgentSelection
from T09_terminal_ops import PromptBackRequested


@dataclass
class _RequirementsStageResult:
    requirement_name: str
    ba_handoff: object | None = None
    reviewer_handoff: object | None = None
    developer_handoff: object | None = None


def _hold_requirement_lock(
    project_dir: str,
    requirement_name: str,
    ready: threading.Event,
    release: threading.Event,
    errors: list[Exception],
) -> None:
    try:
        with requirement_concurrency_lock(project_dir, requirement_name, action="thread-holder"):
            ready.set()
            release.wait(timeout=5)
    except Exception as error:  # noqa: BLE001
        errors.append(error)


class A00MainTests(unittest.TestCase):
    def test_build_stage_args_passes_project_dir_and_optional_yes(self):
        self.assertEqual(build_stage_args("/tmp/project", auto_confirm=False), ["--project-dir", "/tmp/project"])
        self.assertEqual(build_stage_args("/tmp/project", auto_confirm=True), ["--project-dir", "/tmp/project", "--yes"])
        self.assertEqual(
            build_stage_args("/tmp/project", auto_confirm=True, requirement_name="需求A"),
            ["--project-dir", "/tmp/project", "--requirement-name", "需求A", "--yes"],
        )

    def test_build_stage_args_appends_review_max_rounds(self):
        self.assertEqual(
            build_stage_args("/tmp/project", auto_confirm=False, review_max_rounds="infinite"),
            ["--project-dir", "/tmp/project", "--review-max-rounds", "infinite"],
        )

    def test_build_stage_args_can_include_existing_requirement_reuse_flag(self):
        self.assertEqual(
            build_stage_args(
                "/tmp/project",
                auto_confirm=True,
                requirement_name="需求A",
                reuse_existing_original_requirement=True,
            ),
            [
                "--project-dir",
                "/tmp/project",
                "--requirement-name",
                "需求A",
                "--reuse-existing-original-requirement",
                "--yes",
            ],
        )

    def test_build_stage_args_can_include_main_and_two_reviewers(self):
        main_agent = ReviewAgentSelection("codex", "gpt-5.4", "high", "10900")

        args = build_stage_args(
            "/tmp/project",
            auto_confirm=True,
            requirement_name="需求A",
            main_agent=main_agent,
            main_proxy_arg="--proxy-port",
            reviewer_agents=(
                "name=R1,vendor=codex,model=gpt-5.4-mini,effort=medium,proxy=10900",
                "name=R2,vendor=codex,model=gpt-5.4,effort=high",
            ),
        )

        self.assertEqual(
            args,
            [
                "--project-dir",
                "/tmp/project",
                "--requirement-name",
                "需求A",
                "--vendor",
                "codex",
                "--model",
                "gpt-5.4",
                "--effort",
                "high",
                "--proxy-port",
                "10900",
                "--reviewer-agent",
                "name=R1,vendor=codex,model=gpt-5.4-mini,effort=medium,proxy=10900",
                "--reviewer-agent",
                "name=R2,vendor=codex,model=gpt-5.4,effort=high",
                "--yes",
            ],
        )

    def test_render_remaining_stage_placeholders_lists_future_stages(self):
        text = render_remaining_stage_placeholders()
        self.assertNotIn("详细设计阶段（占位）", text)
        self.assertNotIn("任务拆分阶段（占位）", text)
        self.assertIn("测试阶段（功能测试 + 全面回归，占位）", text)

    def test_main_runs_a01_then_a02_then_a03_then_a04_then_a05_then_a06_then_a07_then_a08_then_prints_future_placeholders(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            stdout = io.StringIO()
            calls: list[tuple[str, list[str]]] = []
            lifecycle: list[str] = []
            stage_notifications: list[str] = []

            def fake_a01(argv):  # noqa: ANN001
                calls.append(("a01", list(argv)))
                lifecycle.append("a01")
                return 0

            def fake_a02(argv):  # noqa: ANN001
                calls.append(("a02", list(argv)))
                lifecycle.append("a02")
                return _RequirementsStageResult(requirement_name="需求A")

            def fake_a03(argv, preserve_ba_worker=False):  # noqa: ANN001
                calls.append(("a03", list(argv)))
                lifecycle.append("a03")
                self.assertTrue(preserve_ba_worker)
                return _RequirementsStageResult(requirement_name="需求A", ba_handoff="live-ba")

            def fake_a04(argv, ba_handoff=None, preserve_ba_worker=False):  # noqa: ANN001
                calls.append(("a04", list(argv)))
                lifecycle.append("a04")
                self.assertEqual(ba_handoff, "live-ba")
                self.assertTrue(preserve_ba_worker)
                return _RequirementsStageResult(requirement_name="需求A", ba_handoff="review-ba")

            def fake_a05(argv, ba_handoff=None, preserve_workers=False):  # noqa: ANN001
                calls.append(("a05", list(argv)))
                lifecycle.append("a05")
                self.assertEqual(ba_handoff, "review-ba")
                self.assertTrue(preserve_workers)
                return _RequirementsStageResult(requirement_name="需求A", ba_handoff="design-ba", reviewer_handoff=())

            def fake_a06(argv, ba_handoff=None, reviewer_handoff=None):  # noqa: ANN001
                calls.append(("a06", list(argv)))
                lifecycle.append("a06")
                self.assertEqual(ba_handoff, "design-ba")
                self.assertEqual(reviewer_handoff, ())
                return _RequirementsStageResult(requirement_name="需求A")

            def fake_a07(argv, preserve_workers=False):  # noqa: ANN001
                calls.append(("a07", list(argv)))
                lifecycle.append("a07")
                self.assertTrue(preserve_workers)
                return _RequirementsStageResult(
                    requirement_name="需求A",
                    developer_handoff="live-dev",
                    reviewer_handoff=(),
                )

            def fake_a08(argv, developer_handoff=None, reviewer_handoff=None):  # noqa: ANN001
                calls.append(("a08", list(argv)))
                lifecycle.append("a08")
                self.assertEqual(developer_handoff, "live-dev")
                self.assertEqual(reviewer_handoff, ())
                return _RequirementsStageResult(requirement_name="需求A")

            with patch("A00_main.routing_stage_main", side_effect=fake_a01), patch(
                "A00_main.run_requirement_intake_stage",
                side_effect=fake_a02,
            ), patch(
                "A00_main.run_requirements_clarification_stage",
                side_effect=fake_a03,
            ), patch(
                "A00_main.run_requirements_review_stage",
                side_effect=fake_a04,
            ), patch(
                "A00_main.run_detailed_design_stage",
                side_effect=fake_a05,
            ), patch(
                "A00_main.run_task_split_stage",
                side_effect=fake_a06,
            ), patch(
                "A00_main.run_development_stage",
                side_effect=fake_a07,
            ), patch(
                "A00_main.run_overall_review_stage",
                side_effect=fake_a08,
            ), patch("A00_main.clear_pending_tty_input", side_effect=lambda: lifecycle.append("flush")), patch(
                "A00_main.notify_stage_action_changed",
                side_effect=stage_notifications.append,
            ), patch("sys.stdout", stdout):
                exit_code = main(["--project-dir", tmpdir, "--requirement-name", "需求A", "--yes"])

            self.assertEqual(exit_code, 0)
            self.assertEqual(
                calls,
                [
                    ("a01", ["--project-dir", tmpdir, "--requirement-name", "需求A", "--yes"]),
                    ("a02", ["--project-dir", tmpdir, "--requirement-name", "需求A", "--yes"]),
                    ("a03", ["--project-dir", tmpdir, "--requirement-name", "需求A", "--allow-previous-stage-back", "--yes"]),
                    ("a04", ["--project-dir", tmpdir, "--requirement-name", "需求A", "--allow-previous-stage-back", "--yes"]),
                    ("a05", ["--project-dir", tmpdir, "--requirement-name", "需求A", "--allow-previous-stage-back", "--yes"]),
                    ("a06", ["--project-dir", tmpdir, "--requirement-name", "需求A", "--allow-previous-stage-back", "--yes"]),
                    ("a07", ["--project-dir", tmpdir, "--requirement-name", "需求A", "--allow-previous-stage-back", "--yes"]),
                    ("a08", ["--project-dir", tmpdir, "--requirement-name", "需求A", "--allow-previous-stage-back", "--yes"]),
                ],
            )
            self.assertEqual(lifecycle, ["a01", "flush", "a02", "flush", "a03", "flush", "a04", "flush", "a05", "flush", "a06", "flush", "a07", "flush", "a08"])
            self.assertEqual(stage_notifications, ["stage.a01.start", "stage.a02.start", "stage.a03.start", "stage.a04.start", "stage.a05.start", "stage.a06.start", "stage.a07.start", "stage.a08.start"])
            self.assertIn("AGENT初始化阶段", stdout.getvalue())
            self.assertIn("需求录入阶段", stdout.getvalue())
            self.assertIn("需求澄清阶段", stdout.getvalue())
            self.assertIn("需求评审阶段", stdout.getvalue())
            self.assertIn("详细设计阶段", stdout.getvalue())
            self.assertIn("任务拆分阶段", stdout.getvalue())
            self.assertIn("任务开发阶段", stdout.getvalue())
            self.assertIn("复核阶段", stdout.getvalue())
            record_path = build_pre_development_task_record_path(tmpdir, requirement_name="需求A")
            self.assertTrue(record_path.exists())
            self.assertEqual(
                json.loads(record_path.read_text(encoding="utf-8")),
                build_pre_development_task_record_payload(),
            )

    def test_main_forwards_existing_requirement_reuse_only_to_intake_stage(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            calls: list[tuple[str, list[str]]] = []

            def remember(stage: str, result):
                def inner(argv, *args, **kwargs):  # noqa: ANN001, ARG001
                    calls.append((stage, list(argv)))
                    return result

                return inner

            with patch("A00_main.routing_stage_main", side_effect=remember("a01", 0)), patch(
                "A00_main.run_requirement_intake_stage",
                side_effect=remember("a02", _RequirementsStageResult(requirement_name="需求A")),
            ), patch(
                "A00_main.run_requirements_clarification_stage",
                side_effect=remember("a03", _RequirementsStageResult(requirement_name="需求A", ba_handoff="live-ba")),
            ), patch(
                "A00_main.run_requirements_review_stage",
                side_effect=remember("a04", _RequirementsStageResult(requirement_name="需求A", ba_handoff="review-ba")),
            ), patch(
                "A00_main.run_detailed_design_stage",
                side_effect=remember("a05", _RequirementsStageResult(requirement_name="需求A", ba_handoff="design-ba", reviewer_handoff=())),
            ), patch(
                "A00_main.run_task_split_stage",
                side_effect=remember("a06", _RequirementsStageResult(requirement_name="需求A")),
            ), patch(
                "A00_main.run_development_stage",
                side_effect=remember("a07", _RequirementsStageResult(requirement_name="需求A", developer_handoff="live-dev", reviewer_handoff=())),
            ), patch(
                "A00_main.run_overall_review_stage",
                side_effect=remember("a08", _RequirementsStageResult(requirement_name="需求A")),
            ), patch("A00_main.clear_pending_tty_input"), patch("A00_main.notify_stage_action_changed"):
                exit_code = main([
                    "--project-dir",
                    tmpdir,
                    "--requirement-name",
                    "需求A",
                    "--reuse-existing-original-requirement",
                    "--yes",
                ])

        self.assertEqual(exit_code, 0)
        call_map = dict(calls)
        self.assertIn("--reuse-existing-original-requirement", call_map["a02"])
        for stage in ("a01", "a03", "a04", "a05", "a06", "a07", "a08"):
            self.assertNotIn("--reuse-existing-original-requirement", call_map[stage])

    def test_main_allows_back_from_intake_first_prompt_to_skipped_routing_stage(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            routing_calls: list[list[str]] = []
            intake_calls: list[list[str]] = []

            def route(argv):  # noqa: ANN001
                routing_calls.append(list(argv))
                return SimpleNamespace(project_dir=tmpdir, skipped=True, exit_code=0)

            def intake(argv):  # noqa: ANN001
                intake_calls.append(list(argv))
                if len(intake_calls) == 1:
                    raise PromptBackRequested()
                return _RequirementsStageResult(requirement_name="需求A")

            with patch("A00_main.routing_stage_main", side_effect=route), patch(
                "A00_main.run_requirement_intake_stage",
                side_effect=intake,
            ), patch(
                "A00_main.run_requirements_clarification_stage",
                return_value=_RequirementsStageResult(requirement_name="需求A", ba_handoff="live-ba"),
            ), patch(
                "A00_main.run_requirements_review_stage",
                return_value=_RequirementsStageResult(requirement_name="需求A", ba_handoff="review-ba"),
            ), patch(
                "A00_main.run_detailed_design_stage",
                return_value=_RequirementsStageResult(requirement_name="需求A", ba_handoff="design-ba", reviewer_handoff=()),
            ), patch(
                "A00_main.run_task_split_stage",
                return_value=_RequirementsStageResult(requirement_name="需求A"),
            ), patch(
                "A00_main.run_development_stage",
                return_value=_RequirementsStageResult(requirement_name="需求A", developer_handoff="live-dev", reviewer_handoff=()),
            ), patch(
                "A00_main.run_overall_review_stage",
                return_value=_RequirementsStageResult(requirement_name="需求A"),
            ), patch("A00_main.clear_pending_tty_input"), patch("A00_main.notify_stage_action_changed"):
                exit_code = main([])

        self.assertEqual(exit_code, 0)
        self.assertEqual(len(routing_calls), 2)
        self.assertEqual(len(intake_calls), 2)
        self.assertIn("--allow-previous-stage-back", intake_calls[0])
        self.assertIn("--project-dir", routing_calls[1])
        self.assertIn(tmpdir, routing_calls[1])
        self.assertIn("--allow-project-dir-back", routing_calls[1])

    def test_main_allows_back_from_clarification_first_prompt_to_intake_stage(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            intake_calls: list[list[str]] = []
            clarification_calls: list[list[str]] = []

            def intake(argv):  # noqa: ANN001
                intake_calls.append(list(argv))
                return _RequirementsStageResult(requirement_name="需求A")

            def clarification(argv, preserve_ba_worker=False):  # noqa: ANN001, ARG001
                clarification_calls.append(list(argv))
                if len(clarification_calls) == 1:
                    raise PromptBackRequested()
                return _RequirementsStageResult(requirement_name="需求A", ba_handoff="live-ba")

            with patch(
                "A00_main.routing_stage_main",
                return_value=SimpleNamespace(project_dir=tmpdir, skipped=False, exit_code=0),
            ), patch(
                "A00_main.run_requirement_intake_stage",
                side_effect=intake,
            ), patch(
                "A00_main.run_requirements_clarification_stage",
                side_effect=clarification,
            ), patch(
                "A00_main.run_requirements_review_stage",
                return_value=_RequirementsStageResult(requirement_name="需求A", ba_handoff="review-ba"),
            ), patch(
                "A00_main.run_detailed_design_stage",
                return_value=_RequirementsStageResult(requirement_name="需求A", ba_handoff="design-ba", reviewer_handoff=()),
            ), patch(
                "A00_main.run_task_split_stage",
                return_value=_RequirementsStageResult(requirement_name="需求A"),
            ), patch(
                "A00_main.run_development_stage",
                return_value=_RequirementsStageResult(requirement_name="需求A", developer_handoff="live-dev", reviewer_handoff=()),
            ), patch(
                "A00_main.run_overall_review_stage",
                return_value=_RequirementsStageResult(requirement_name="需求A"),
            ), patch("A00_main.clear_pending_tty_input"), patch("A00_main.notify_stage_action_changed"):
                exit_code = main(["--project-dir", tmpdir])

        self.assertEqual(exit_code, 0)
        self.assertEqual(len(intake_calls), 2)
        self.assertEqual(len(clarification_calls), 2)
        self.assertIn("--allow-previous-stage-back", clarification_calls[0])
        self.assertIn("--allow-previous-stage-back", clarification_calls[1])

    def test_main_allows_back_from_requirements_review_first_prompt_to_clarification_stage(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            clarification_calls: list[list[str]] = []
            review_calls: list[list[str]] = []

            def clarification(argv, preserve_ba_worker=False):  # noqa: ANN001, ARG001
                clarification_calls.append(list(argv))
                return _RequirementsStageResult(requirement_name="需求A", ba_handoff="live-ba")

            def review(argv, ba_handoff=None, preserve_ba_worker=False):  # noqa: ANN001, ARG001
                review_calls.append(list(argv))
                if len(review_calls) == 1:
                    raise PromptBackRequested()
                return _RequirementsStageResult(requirement_name="需求A", ba_handoff="review-ba")

            with patch(
                "A00_main.routing_stage_main",
                return_value=SimpleNamespace(project_dir=tmpdir, skipped=False, exit_code=0),
            ), patch(
                "A00_main.run_requirement_intake_stage",
                return_value=_RequirementsStageResult(requirement_name="需求A"),
            ), patch(
                "A00_main.run_requirements_clarification_stage",
                side_effect=clarification,
            ), patch(
                "A00_main.run_requirements_review_stage",
                side_effect=review,
            ), patch(
                "A00_main.run_detailed_design_stage",
                return_value=_RequirementsStageResult(requirement_name="需求A", ba_handoff="design-ba", reviewer_handoff=()),
            ), patch(
                "A00_main.run_task_split_stage",
                return_value=_RequirementsStageResult(requirement_name="需求A"),
            ), patch(
                "A00_main.run_development_stage",
                return_value=_RequirementsStageResult(requirement_name="需求A", developer_handoff="live-dev", reviewer_handoff=()),
            ), patch(
                "A00_main.run_overall_review_stage",
                return_value=_RequirementsStageResult(requirement_name="需求A"),
            ), patch("A00_main.clear_pending_tty_input"), patch("A00_main.notify_stage_action_changed"):
                exit_code = main(["--project-dir", tmpdir])

        self.assertEqual(exit_code, 0)
        self.assertEqual(len(clarification_calls), 2)
        self.assertEqual(len(review_calls), 2)
        self.assertIn("--allow-previous-stage-back", review_calls[0])
        self.assertIn("--allow-previous-stage-back", clarification_calls[1])

    def test_main_forwards_agent_config_and_can_skip_overall_review(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            calls: dict[str, list[str]] = {}
            stage_notifications: list[str] = []
            config_path = Path(tmpdir) / "agents.json"
            config_path.write_text(
                json.dumps(
                    {
                        "main": {"vendor": "codex", "model": "gpt-5.4", "effort": "high", "proxy": "10900"},
                        "reviewers": [
                            {"name": "R1", "vendor": "codex", "model": "gpt-5.4-mini", "effort": "medium", "proxy": "10900"},
                            {"name": "R2", "vendor": "codex", "model": "gpt-5.4", "effort": "high"},
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            def remember(stage: str, result: _RequirementsStageResult):  # noqa: ANN001
                def _runner(argv, *args, **kwargs):  # noqa: ANN001
                    _ = args, kwargs
                    calls[stage] = list(argv)
                    return result

                return _runner

            with patch("A00_main.routing_stage_main", side_effect=remember("a01", 0)), patch(
                "A00_main.run_requirement_intake_stage",
                side_effect=remember("a02", _RequirementsStageResult(requirement_name="需求A")),
            ), patch(
                "A00_main.run_requirements_clarification_stage",
                side_effect=remember("a03", _RequirementsStageResult(requirement_name="需求A", ba_handoff="live-ba")),
            ), patch(
                "A00_main.run_requirements_review_stage",
                side_effect=remember("a04", _RequirementsStageResult(requirement_name="需求A", ba_handoff="review-ba")),
            ), patch(
                "A00_main.run_detailed_design_stage",
                side_effect=remember("a05", _RequirementsStageResult(requirement_name="需求A", ba_handoff="design-ba", reviewer_handoff=())),
            ), patch(
                "A00_main.run_task_split_stage",
                side_effect=remember("a06", _RequirementsStageResult(requirement_name="需求A")),
            ), patch(
                "A00_main.run_development_stage",
                side_effect=remember("a07", _RequirementsStageResult(requirement_name="需求A", developer_handoff="live-dev", reviewer_handoff=())),
            ), patch(
                "A00_main.run_overall_review_stage",
            ) as a08_mock, patch(
                "A00_main.notify_stage_action_changed",
                side_effect=stage_notifications.append,
            ):
                exit_code = main(
                    [
                        "--project-dir",
                        tmpdir,
                        "--requirement-name",
                        "需求A",
                        "--agent-config",
                        str(config_path),
                        "--skip-overall-review",
                    ]
                )

        self.assertEqual(exit_code, 0)
        self.assertEqual(calls["a01"][-8:], ["--vendor", "codex", "--model", "gpt-5.4", "--effort", "high", "--proxy-port", "10900"])
        self.assertIn("--proxy-url", calls["a03"])
        self.assertNotIn("--proxy-port", calls["a03"])
        for stage in ("a04", "a05", "a06", "a07"):
            self.assertEqual(calls[stage].count("--reviewer-agent"), 2)
        self.assertIn("name=R1,vendor=codex,model=gpt-5.4-mini,effort=medium,proxy=10900", calls["a04"])
        self.assertIn("name=R2,vendor=codex,model=gpt-5.4,effort=high", calls["a07"])
        a08_mock.assert_not_called()
        self.assertNotIn("stage.a08.start", stage_notifications)

    def test_main_forwards_stage_specific_agent_config(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            calls: dict[str, list[str]] = {}
            config_path = Path(tmpdir) / "agents.json"
            config_path.write_text(
                json.dumps(
                    {
                        "main": {"vendor": "codex", "model": "gpt-5.4", "effort": "high"},
                        "reviewers": [{"name": "R1", "vendor": "codex", "model": "gpt-5.4-mini", "effort": "medium"}],
                        "stages": {
                            "routing": {"main": {"vendor": "gemini", "model": "flash", "effort": "medium", "proxy": "10809"}},
                            "development": {
                                "main": {"vendor": "claude", "model": "sonnet", "effort": "high"},
                                "reviewers": [{"name": "R1", "vendor": "opencode", "model": "opencode/big-pickle", "effort": "xhigh"}],
                            },
                            "overall_review": {
                                "reviewers": [{"name": "R1", "vendor": "gemini", "model": "flash", "effort": "medium", "proxy": "10900"}]
                            },
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            def remember(stage: str, result: _RequirementsStageResult):  # noqa: ANN001
                def _runner(argv, *args, **kwargs):  # noqa: ANN001
                    _ = args, kwargs
                    calls[stage] = list(argv)
                    return result

                return _runner

            with patch("A00_main.routing_stage_main", side_effect=remember("a01", 0)), patch(
                "A00_main.run_requirement_intake_stage",
                side_effect=remember("a02", _RequirementsStageResult(requirement_name="需求A")),
            ), patch(
                "A00_main.run_requirements_clarification_stage",
                side_effect=remember("a03", _RequirementsStageResult(requirement_name="需求A", ba_handoff="live-ba")),
            ), patch(
                "A00_main.run_requirements_review_stage",
                side_effect=remember("a04", _RequirementsStageResult(requirement_name="需求A", ba_handoff="review-ba")),
            ), patch(
                "A00_main.run_detailed_design_stage",
                side_effect=remember("a05", _RequirementsStageResult(requirement_name="需求A", ba_handoff="design-ba", reviewer_handoff=())),
            ), patch(
                "A00_main.run_task_split_stage",
                side_effect=remember("a06", _RequirementsStageResult(requirement_name="需求A")),
            ), patch(
                "A00_main.run_development_stage",
                side_effect=remember("a07", _RequirementsStageResult(requirement_name="需求A", developer_handoff="live-dev", reviewer_handoff=())),
            ), patch(
                "A00_main.run_overall_review_stage",
                side_effect=remember("a08", _RequirementsStageResult(requirement_name="需求A")),
            ), patch("A00_main.notify_stage_action_changed"):
                exit_code = main(["--project-dir", tmpdir, "--requirement-name", "需求A", "--agent-config", str(config_path)])

        self.assertEqual(exit_code, 0)
        self.assertEqual(calls["a01"][-8:], ["--vendor", "gemini", "--model", "flash", "--effort", "medium", "--proxy-port", "10809"])
        self.assertIn("name=R1,vendor=codex,model=gpt-5.4-mini,effort=medium", calls["a05"])
        self.assertIn("--vendor", calls["a07"])
        self.assertIn("claude", calls["a07"])
        self.assertIn("name=R1,vendor=opencode,model=opencode/big-pickle,effort=xhigh", calls["a07"])
        self.assertIn("name=R1,vendor=gemini,model=flash,effort=medium,proxy=10900", calls["a08"])

    def test_main_reraises_stage_exception_in_bridge_mode(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("A00_main._bridge_terminal_active", return_value=True), patch(
                "A00_main.routing_stage_main",
                return_value=0,
            ), patch(
                "A00_main.run_requirement_intake_stage",
                return_value=_RequirementsStageResult(requirement_name="需求A"),
            ), patch(
                "A00_main.run_requirements_clarification_stage",
                return_value=_RequirementsStageResult(requirement_name="需求A", ba_handoff="live-ba"),
            ), patch(
                "A00_main.run_requirements_review_stage",
                return_value=_RequirementsStageResult(requirement_name="需求A", ba_handoff="review-ba"),
            ), patch(
                "A00_main.run_detailed_design_stage",
                side_effect=RuntimeError("详细设计失败"),
            ):
                with self.assertRaisesRegex(RuntimeError, "详细设计失败"):
                    main(["--project-dir", tmpdir, "--requirement-name", "需求A"])

    def test_main_keeps_return_one_for_stage_exception_in_cli_mode(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            stdout = io.StringIO()
            with patch("A00_main._bridge_terminal_active", return_value=False), patch(
                "A00_main.routing_stage_main",
                return_value=0,
            ), patch(
                "A00_main.run_requirement_intake_stage",
                return_value=_RequirementsStageResult(requirement_name="需求A"),
            ), patch(
                "A00_main.run_requirements_clarification_stage",
                return_value=_RequirementsStageResult(requirement_name="需求A", ba_handoff="live-ba"),
            ), patch(
                "A00_main.run_requirements_review_stage",
                return_value=_RequirementsStageResult(requirement_name="需求A", ba_handoff="review-ba"),
            ), patch(
                "A00_main.run_detailed_design_stage",
                side_effect=RuntimeError("详细设计失败"),
            ), patch("sys.stdout", stdout):
                exit_code = main(["--project-dir", tmpdir, "--requirement-name", "需求A"])

        self.assertEqual(exit_code, 1)
        self.assertIn("详细设计失败", stdout.getvalue())

    def test_main_continues_to_a05_when_a04_skips_review_and_clears_ba_handoff(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            observed: dict[str, object] = {}

            def fake_a05(argv, ba_handoff=None, preserve_workers=False):  # noqa: ANN001
                observed["argv"] = list(argv)
                observed["ba_handoff"] = ba_handoff
                observed["preserve_workers"] = preserve_workers
                return _RequirementsStageResult(requirement_name="需求A", ba_handoff=None, reviewer_handoff=None)

            def fake_a06(argv, ba_handoff=None, reviewer_handoff=None):  # noqa: ANN001
                observed["a06_argv"] = list(argv)
                observed["a06_ba_handoff"] = ba_handoff
                observed["a06_reviewer_handoff"] = reviewer_handoff
                return _RequirementsStageResult(requirement_name="需求A")

            with patch("A00_main.routing_stage_main", return_value=0), patch(
                "A00_main.run_requirement_intake_stage",
                return_value=_RequirementsStageResult(requirement_name="需求A"),
            ), patch(
                "A00_main.run_requirements_clarification_stage",
                return_value=_RequirementsStageResult(requirement_name="需求A", ba_handoff="live-ba"),
            ), patch(
                "A00_main.run_requirements_review_stage",
                return_value=_RequirementsStageResult(requirement_name="需求A", ba_handoff=None),
            ), patch(
                "A00_main.run_detailed_design_stage",
                side_effect=fake_a05,
            ), patch(
                "A00_main.run_task_split_stage",
                side_effect=fake_a06,
            ), patch(
                "A00_main.run_development_stage",
                return_value=_RequirementsStageResult(requirement_name="需求A", developer_handoff=None, reviewer_handoff=()),
            ), patch(
                "A00_main.run_overall_review_stage",
                return_value=_RequirementsStageResult(requirement_name="需求A"),
            ):
                exit_code = main(["--project-dir", tmpdir, "--requirement-name", "需求A", "--yes"])

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            observed["argv"],
            ["--project-dir", tmpdir, "--requirement-name", "需求A", "--allow-previous-stage-back", "--yes"],
        )
        self.assertIsNone(observed["ba_handoff"])
        self.assertTrue(observed["preserve_workers"])
        self.assertIsNone(observed["a06_ba_handoff"])
        self.assertIsNone(observed["a06_reviewer_handoff"])

    def test_main_continues_to_a06_when_a05_skips_detailed_design(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            observed: dict[str, object] = {}

            def fake_a06(argv, ba_handoff=None, reviewer_handoff=None):  # noqa: ANN001
                observed["argv"] = list(argv)
                observed["ba_handoff"] = ba_handoff
                observed["reviewer_handoff"] = reviewer_handoff
                return _RequirementsStageResult(requirement_name="需求A")

            with patch("A00_main.routing_stage_main", return_value=0), patch(
                "A00_main.run_requirement_intake_stage",
                return_value=_RequirementsStageResult(requirement_name="需求A"),
            ), patch(
                "A00_main.run_requirements_clarification_stage",
                return_value=_RequirementsStageResult(requirement_name="需求A", ba_handoff="live-ba"),
            ), patch(
                "A00_main.run_requirements_review_stage",
                return_value=_RequirementsStageResult(requirement_name="需求A", ba_handoff="review-ba"),
            ), patch(
                "A00_main.run_detailed_design_stage",
                return_value=_RequirementsStageResult(requirement_name="需求A", ba_handoff=None, reviewer_handoff=()),
            ), patch(
                "A00_main.run_task_split_stage",
                side_effect=fake_a06,
            ), patch(
                "A00_main.run_development_stage",
                return_value=_RequirementsStageResult(requirement_name="需求A", developer_handoff=None, reviewer_handoff=()),
            ), patch(
                "A00_main.run_overall_review_stage",
                return_value=_RequirementsStageResult(requirement_name="需求A"),
            ):
                exit_code = main(["--project-dir", tmpdir, "--requirement-name", "需求A", "--yes"])

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            observed["argv"],
            ["--project-dir", tmpdir, "--requirement-name", "需求A", "--allow-previous-stage-back", "--yes"],
        )
        self.assertIsNone(observed["ba_handoff"])
        self.assertEqual(observed["reviewer_handoff"], ())

    def test_main_continues_to_a08_when_a06_skips_task_split(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            calls: list[tuple[str, list[str]]] = []
            lifecycle: list[str] = []

            def fake_a07(argv, preserve_workers=False):  # noqa: ANN001
                lifecycle.append("a07")
                calls.append(("a07", list(argv)))
                self.assertTrue(preserve_workers)
                return _RequirementsStageResult(requirement_name="需求A", developer_handoff="live-dev", reviewer_handoff=())

            def fake_a08(argv, developer_handoff=None, reviewer_handoff=None):  # noqa: ANN001
                lifecycle.append("a08")
                calls.append(("a08", list(argv)))
                self.assertEqual(developer_handoff, "live-dev")
                self.assertEqual(reviewer_handoff, ())
                return _RequirementsStageResult(requirement_name="需求A")

            with patch("A00_main.routing_stage_main", return_value=0), patch(
                "A00_main.run_requirement_intake_stage",
                return_value=_RequirementsStageResult(requirement_name="需求A"),
            ), patch(
                "A00_main.run_requirements_clarification_stage",
                return_value=_RequirementsStageResult(requirement_name="需求A", ba_handoff="live-ba"),
            ), patch(
                "A00_main.run_requirements_review_stage",
                return_value=_RequirementsStageResult(requirement_name="需求A", ba_handoff="review-ba"),
            ), patch(
                "A00_main.run_detailed_design_stage",
                return_value=_RequirementsStageResult(requirement_name="需求A", ba_handoff=None, reviewer_handoff=()),
            ), patch(
                "A00_main.run_task_split_stage",
                return_value=_RequirementsStageResult(requirement_name="需求A"),
            ), patch(
                "A00_main.run_development_stage",
                side_effect=fake_a07,
            ), patch(
                "A00_main.run_overall_review_stage",
                side_effect=fake_a08,
            ), patch(
                "A00_main.cleanup_stale_development_runtime_state",
                side_effect=lambda project_dir, requirement_name: lifecycle.append(f"cleanup:{project_dir}:{requirement_name}") or (),
            ), patch(
                "A00_main.notify_stage_action_changed",
                side_effect=lambda action: lifecycle.append(f"stage:{action}"),
            ):
                exit_code = main(["--project-dir", tmpdir, "--requirement-name", "需求A", "--yes"])

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            calls,
            [
                ("a07", ["--project-dir", tmpdir, "--requirement-name", "需求A", "--allow-previous-stage-back", "--yes"]),
                ("a08", ["--project-dir", tmpdir, "--requirement-name", "需求A", "--allow-previous-stage-back", "--yes"]),
            ],
        )
        self.assertLess(lifecycle.index(f"cleanup:{tmpdir}:需求A"), lifecycle.index("stage:stage.a07.start"))
        self.assertLess(lifecycle.index("stage:stage.a07.start"), lifecycle.index("a07"))

    def test_main_stops_when_a01_fails(self):
        stdout = io.StringIO()
        with patch("A00_main.routing_stage_main", return_value=1), patch(
            "A00_main.run_requirement_intake_stage",
            return_value=_RequirementsStageResult(requirement_name="需求A"),
        ) as mocked_a02, patch(
            "A00_main.run_requirements_clarification_stage",
            return_value=_RequirementsStageResult(requirement_name="需求A", ba_handoff="live-ba"),
        ) as mocked_a03, patch(
            "A00_main.run_requirements_review_stage",
            return_value=_RequirementsStageResult(requirement_name="需求A", ba_handoff="review-ba"),
        ) as mocked_a04, patch(
            "A00_main.run_detailed_design_stage",
            return_value=_RequirementsStageResult(requirement_name="需求A", ba_handoff=None),
        ) as mocked_a05, patch(
            "A00_main.run_task_split_stage",
            return_value=_RequirementsStageResult(requirement_name="需求A"),
        ) as mocked_a06, patch("sys.stdout", stdout):
            exit_code = main(["--project-dir", "/tmp/project"])

        self.assertEqual(exit_code, 1)
        mocked_a02.assert_not_called()
        mocked_a03.assert_not_called()
        mocked_a04.assert_not_called()
        mocked_a05.assert_not_called()
        mocked_a06.assert_not_called()

    def test_main_stops_when_a04_fails(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            stdout = io.StringIO()
            with patch("A00_main.routing_stage_main", return_value=0), patch(
                "A00_main.run_requirement_intake_stage",
                return_value=_RequirementsStageResult(requirement_name="需求A"),
            ), patch(
                "A00_main.run_requirements_clarification_stage",
                return_value=_RequirementsStageResult(requirement_name="需求A", ba_handoff="live-ba"),
            ), patch(
                "A00_main.run_requirements_review_stage",
                side_effect=RuntimeError("review failed"),
            ), patch(
                "A00_main.run_detailed_design_stage",
                return_value=_RequirementsStageResult(requirement_name="需求A", ba_handoff=None),
            ), patch(
                "A00_main.run_task_split_stage",
                return_value=_RequirementsStageResult(requirement_name="需求A"),
            ), patch(
                "A00_main.run_development_stage",
                return_value=_RequirementsStageResult(requirement_name="需求A", developer_handoff=None, reviewer_handoff=()),
            ), patch("sys.stdout", stdout):
                exit_code = main(["--project-dir", tmpdir, "--requirement-name", "需求A"])

        self.assertEqual(exit_code, 1)
        self.assertIn("review failed", stdout.getvalue())

    def test_main_stops_when_a05_fails(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            stdout = io.StringIO()
            with patch("A00_main.routing_stage_main", return_value=0), patch(
                "A00_main.run_requirement_intake_stage",
                return_value=_RequirementsStageResult(requirement_name="需求A"),
            ), patch(
                "A00_main.run_requirements_clarification_stage",
                return_value=_RequirementsStageResult(requirement_name="需求A", ba_handoff="live-ba"),
            ), patch(
                "A00_main.run_requirements_review_stage",
                return_value=_RequirementsStageResult(requirement_name="需求A", ba_handoff="review-ba"),
            ), patch(
                "A00_main.run_detailed_design_stage",
                side_effect=RuntimeError("design failed"),
            ), patch(
                "A00_main.run_task_split_stage",
                return_value=_RequirementsStageResult(requirement_name="需求A"),
            ), patch(
                "A00_main.run_development_stage",
                return_value=_RequirementsStageResult(requirement_name="需求A", developer_handoff=None, reviewer_handoff=()),
            ), patch("sys.stdout", stdout):
                exit_code = main(["--project-dir", tmpdir, "--requirement-name", "需求A"])

        self.assertEqual(exit_code, 1)
        self.assertIn("design failed", stdout.getvalue())

    def test_main_stops_when_a06_fails(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            stdout = io.StringIO()
            with patch("A00_main.routing_stage_main", return_value=0), patch(
                "A00_main.run_requirement_intake_stage",
                return_value=_RequirementsStageResult(requirement_name="需求A"),
            ), patch(
                "A00_main.run_requirements_clarification_stage",
                return_value=_RequirementsStageResult(requirement_name="需求A", ba_handoff="live-ba"),
            ), patch(
                "A00_main.run_requirements_review_stage",
                return_value=_RequirementsStageResult(requirement_name="需求A", ba_handoff="review-ba"),
            ), patch(
                "A00_main.run_detailed_design_stage",
                return_value=_RequirementsStageResult(requirement_name="需求A", ba_handoff="design-ba"),
            ), patch(
                "A00_main.run_task_split_stage",
                side_effect=RuntimeError("task split failed"),
            ), patch(
                "A00_main.run_development_stage",
                return_value=0,
            ), patch("sys.stdout", stdout):
                exit_code = main(["--project-dir", tmpdir, "--requirement-name", "需求A"])

        self.assertEqual(exit_code, 1)
        self.assertIn("task split failed", stdout.getvalue())

    def test_main_delegates_missing_project_dir_to_a01(self):
        with patch(
            "A00_main.routing_stage_main",
            return_value=SimpleNamespace(project_dir="/tmp/project", exit_code=0),
        ) as routing_stage, patch(
            "A00_main.run_requirement_intake_stage",
            return_value=_RequirementsStageResult(requirement_name="需求A"),
        ) as intake_stage, patch(
            "A00_main.run_requirements_clarification_stage",
            return_value=_RequirementsStageResult(requirement_name="需求A", ba_handoff="live-ba"),
        ), patch(
            "A00_main.run_requirements_review_stage",
            return_value=_RequirementsStageResult(requirement_name="需求A", ba_handoff="review-ba"),
        ), patch(
            "A00_main.run_detailed_design_stage",
            return_value=_RequirementsStageResult(requirement_name="需求A", ba_handoff=None, reviewer_handoff=None),
        ), patch(
            "A00_main.run_task_split_stage",
            return_value=_RequirementsStageResult(requirement_name="需求A"),
        ), patch(
            "A00_main.run_development_stage",
            return_value=_RequirementsStageResult(requirement_name="需求A", developer_handoff=None, reviewer_handoff=()),
        ), patch(
            "A00_main.run_overall_review_stage",
            return_value=_RequirementsStageResult(requirement_name="需求A"),
        ):
            exit_code = main([])
        self.assertEqual(exit_code, 0)
        routing_stage.assert_called_once_with([])
        self.assertIn("--project-dir", intake_stage.call_args.args[0])
        self.assertIn("/tmp/project", intake_stage.call_args.args[0])

    def test_parser_accepts_project_dir_and_yes(self):
        parser = build_parser()
        args = parser.parse_args(
            [
                "--project-dir",
                "/tmp/project",
                "--requirement-name",
                "需求A",
                "--reuse-existing-original-requirement",
                "--yes",
                "--requirements-review-max-rounds",
                "2",
                "--detailed-design-review-max-rounds",
                "3",
                "--task-split-review-max-rounds",
                "4",
                "--development-review-max-rounds",
                "infinite",
                "--main-agent",
                "vendor=codex,model=gpt-5.4,effort=high,proxy=10900",
                "--reviewer-agent",
                "name=R1,vendor=codex,model=gpt-5.4-mini,effort=medium",
                "--reviewer-agent",
                "name=R2,vendor=codex,model=gpt-5.4,effort=high",
                "--agent-config",
                "/tmp/agents.json",
                "--skip-overall-review",
            ]
        )
        self.assertEqual(args.project_dir, "/tmp/project")
        self.assertEqual(args.requirement_name, "需求A")
        self.assertTrue(args.reuse_existing_original_requirement)
        self.assertTrue(args.yes)
        self.assertEqual(args.requirements_review_max_rounds, "2")
        self.assertEqual(args.detailed_design_review_max_rounds, "3")
        self.assertEqual(args.task_split_review_max_rounds, "4")
        self.assertEqual(args.development_review_max_rounds, "infinite")
        self.assertEqual(args.main_agent, "vendor=codex,model=gpt-5.4,effort=high,proxy=10900")
        self.assertEqual(
            args.reviewer_agent,
            [
                "name=R1,vendor=codex,model=gpt-5.4-mini,effort=medium",
                "name=R2,vendor=codex,model=gpt-5.4,effort=high",
            ],
        )
        self.assertEqual(args.agent_config, "/tmp/agents.json")
        self.assertTrue(args.skip_overall_review)

    def test_main_routes_each_stage_review_limit_to_matching_stage_only(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            calls: dict[str, list[str]] = {}

            def fake_a01(argv):  # noqa: ANN001
                calls["a01"] = list(argv)
                return 0

            def fake_a02(argv):  # noqa: ANN001
                calls["a02"] = list(argv)
                return _RequirementsStageResult(requirement_name="需求A")

            def fake_a03(argv, preserve_ba_worker=False):  # noqa: ANN001
                _ = preserve_ba_worker
                calls["a03"] = list(argv)
                return _RequirementsStageResult(requirement_name="需求A", ba_handoff="live-ba")

            def fake_a04(argv, ba_handoff=None, preserve_ba_worker=False):  # noqa: ANN001
                _ = ba_handoff, preserve_ba_worker
                calls["a04"] = list(argv)
                return _RequirementsStageResult(requirement_name="需求A", ba_handoff="review-ba")

            def fake_a05(argv, ba_handoff=None, preserve_workers=False):  # noqa: ANN001
                _ = ba_handoff, preserve_workers
                calls["a05"] = list(argv)
                return _RequirementsStageResult(requirement_name="需求A", ba_handoff="design-ba", reviewer_handoff=())

            def fake_a06(argv, ba_handoff=None, reviewer_handoff=None):  # noqa: ANN001
                _ = ba_handoff, reviewer_handoff
                calls["a06"] = list(argv)
                return _RequirementsStageResult(requirement_name="需求A")

            def fake_a07(argv, preserve_workers=False):  # noqa: ANN001
                calls["a07"] = list(argv)
                self.assertTrue(preserve_workers)
                return _RequirementsStageResult(requirement_name="需求A", developer_handoff=None, reviewer_handoff=())

            def fake_a08(argv, developer_handoff=None, reviewer_handoff=None):  # noqa: ANN001
                _ = developer_handoff, reviewer_handoff
                calls["a08"] = list(argv)
                return _RequirementsStageResult(requirement_name="需求A")

            with patch("A00_main.routing_stage_main", side_effect=fake_a01), patch(
                "A00_main.run_requirement_intake_stage",
                side_effect=fake_a02,
            ), patch(
                "A00_main.run_requirements_clarification_stage",
                side_effect=fake_a03,
            ), patch(
                "A00_main.run_requirements_review_stage",
                side_effect=fake_a04,
            ), patch(
                "A00_main.run_detailed_design_stage",
                side_effect=fake_a05,
            ), patch(
                "A00_main.run_task_split_stage",
                side_effect=fake_a06,
            ), patch(
                "A00_main.run_development_stage",
                side_effect=fake_a07,
            ), patch(
                "A00_main.run_overall_review_stage",
                side_effect=fake_a08,
            ):
                exit_code = main(
                    [
                        "--project-dir",
                        tmpdir,
                        "--requirement-name",
                        "需求A",
                        "--requirements-review-max-rounds",
                        "2",
                        "--detailed-design-review-max-rounds",
                        "3",
                        "--task-split-review-max-rounds",
                        "4",
                        "--development-review-max-rounds",
                        "infinite",
                    ]
                )

        self.assertEqual(exit_code, 0)
        self.assertNotIn("--review-max-rounds", calls["a03"])
        self.assertEqual(calls["a04"][-2:], ["--review-max-rounds", "2"])
        self.assertEqual(calls["a05"][-2:], ["--review-max-rounds", "3"])
        self.assertEqual(calls["a06"][-2:], ["--review-max-rounds", "4"])
        self.assertEqual(calls["a07"][-2:], ["--review-max-rounds", "infinite"])
        self.assertNotIn("--review-max-rounds", calls["a08"])

    def test_ensure_pre_development_task_record_keeps_existing_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            record_path = build_pre_development_task_record_path(tmpdir, requirement_name="需求A")
            existing_payload = {"custom": {"custom": True}}
            record_path.write_text(json.dumps(existing_payload, ensure_ascii=False, indent=2), encoding="utf-8")

            returned_path = ensure_pre_development_task_record(tmpdir, requirement_name="需求A")

            self.assertEqual(returned_path, record_path.resolve())
            self.assertEqual(json.loads(record_path.read_text(encoding="utf-8")), existing_payload)

    def test_clear_pending_tty_input_skips_non_tty(self):
        class _FakeStdin(io.StringIO):
            def isatty(self) -> bool:
                return False

        with patch("sys.stdin", _FakeStdin("")):
            clear_pending_tty_input()

    def test_requirement_concurrency_lock_rejects_same_scope_from_other_thread(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ready = threading.Event()
            release = threading.Event()
            errors: list[Exception] = []
            holder = threading.Thread(
                target=_hold_requirement_lock,
                args=(tmpdir, "需求A", ready, release, errors),
                daemon=True,
            )
            holder.start()
            self.assertTrue(ready.wait(timeout=2.0))
            try:
                with self.assertRaisesRegex(RuntimeError, "并发冲突"):
                    with requirement_concurrency_lock(tmpdir, "需求A", action="contender"):
                        pass
            finally:
                release.set()
                holder.join(timeout=2.0)
            self.assertFalse(holder.is_alive())
            self.assertEqual(errors, [])

    def test_requirement_concurrency_lock_allows_same_thread_reentry(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with requirement_concurrency_lock(tmpdir, "需求A", action="outer"):
                with requirement_concurrency_lock(tmpdir, "需求A", action="inner"):
                    self.assertTrue(True)

    def test_requirement_concurrency_lock_allows_different_requirement_or_project(self):
        with tempfile.TemporaryDirectory() as tmpdir_a, tempfile.TemporaryDirectory() as tmpdir_b:
            with requirement_concurrency_lock(tmpdir_a, "需求A", action="one"):
                with requirement_concurrency_lock(tmpdir_a, "需求B", action="two"):
                    self.assertTrue(True)
                with requirement_concurrency_lock(tmpdir_b, "需求A", action="three"):
                    self.assertTrue(True)


if __name__ == "__main__":
    unittest.main()
