from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from A07_Development import (
    DevelopmentAgentHandoff,
    DevelopmentReviewerSpec,
    DeveloperPlan,
    DeveloperRuntime,
    build_development_paths,
    build_development_runtime_root,
)
from A08_OverallReview import (
    OverallReviewStageResult,
    build_overall_review_metadata_repair_result_contract,
    build_overall_review_paths,
    build_overall_review_refine_result_contract,
    discover_live_development_handoffs,
    ensure_overall_review_inputs,
    refine_overall_review_code,
    run_overall_review_stage,
    _run_overall_review_developer_turn,
    _shutdown_workers as shutdown_overall_review_workers,
)
from canopy_core.stage_kernel.shared_review import ReviewAgentHandoff, ReviewAgentSelection, ReviewerRuntime


class _FakeWorker:
    def __init__(
        self,
        *,
        session_name: str,
        runtime_root: str | Path = "/tmp/runtime",
        runtime_dir: str | Path = "/tmp/runtime/worker",
        session_exists_value: bool = True,
    ) -> None:
        self.session_name = session_name
        self.runtime_root = Path(runtime_root)
        self.runtime_dir = Path(runtime_dir)
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self._session_exists_value = session_exists_value
        self.metadata_updates: list[dict[str, object]] = []
        self.killed = False

    def request_kill(self):
        self.killed = True
        return self.session_name

    def session_exists(self) -> bool:
        return self._session_exists_value

    def set_runtime_metadata(self, **kwargs) -> None:  # noqa: ANN003
        self.metadata_updates.append(dict(kwargs))


def _dummy_contract():
    return object()


def _write_required_inputs(paths: dict[str, Path]) -> None:
    paths["original_requirement_path"].write_text("原始需求\n", encoding="utf-8")
    paths["requirements_clear_path"].write_text("需求澄清\n", encoding="utf-8")
    paths["detailed_design_path"].write_text("详细设计\n", encoding="utf-8")
    paths["task_md_path"].write_text("任务单正文\n", encoding="utf-8")


class A08OverallReviewTests(unittest.TestCase):
    def test_build_overall_review_paths_uses_distinct_review_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            development_paths = build_development_paths(tmp_dir, "需求A")
            overall_review_paths = build_overall_review_paths(tmp_dir, "需求A")

        self.assertEqual(overall_review_paths["developer_output_path"], development_paths["developer_output_path"])
        self.assertNotEqual(overall_review_paths["merged_review_path"], development_paths["merged_review_path"])
        self.assertIn("整体代码复核记录", overall_review_paths["merged_review_path"].name)

    def test_shutdown_overall_review_workers_removes_requirement_scoped_runtime(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            project_dir = Path(tmp_dir)
            scoped_root = build_development_runtime_root(project_dir, "需求A")
            developer = DeveloperRuntime(
                selection=ReviewAgentSelection("codex", "gpt-5.4", "high", ""),
                worker=_FakeWorker(
                    session_name="开发工程师-天魁星",
                    runtime_root=scoped_root,
                    runtime_dir=scoped_root / "development-developer-aaaa",
                ),
                role_prompt="实现视角",
            )
            reviewer = ReviewerRuntime(
                reviewer_name="测试工程师",
                selection=ReviewAgentSelection("codex", "gpt-5.4", "high", ""),
                worker=_FakeWorker(
                    session_name="测试工程师-天英星",
                    runtime_root=scoped_root,
                    runtime_dir=scoped_root / "development-review-bbbb",
                ),
                review_md_path=project_dir / "需求A_整体代码复核记录_测试工程师.md",
                review_json_path=project_dir / "需求A_整体复核记录_测试工程师.json",
                contract=_dummy_contract(),
            )

            removed = shutdown_overall_review_workers(
                developer,
                [reviewer],
                project_dir=project_dir,
                requirement_name="需求A",
                cleanup_runtime=True,
            )

            self.assertTrue(developer.worker.killed)
            self.assertTrue(reviewer.worker.killed)
            self.assertFalse(developer.worker.runtime_dir.exists())
            self.assertFalse(reviewer.worker.runtime_dir.exists())
            self.assertFalse(scoped_root.exists())
            self.assertIn(str(developer.worker.runtime_dir.resolve()), removed)
            self.assertIn(str(reviewer.worker.runtime_dir.resolve()), removed)

    def test_metadata_repair_contract_accepts_check_develop_job_completion_token(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            paths = build_overall_review_paths(tmp_dir, "需求A")
            normal_contract = build_overall_review_refine_result_contract(paths)
            repair_contract = build_overall_review_metadata_repair_result_contract(paths)

        self.assertEqual(normal_contract.terminal_status_tokens["completed"], ("修改完成",))
        self.assertEqual(repair_contract.terminal_status_tokens["completed"], ("任务完成", "修改完成"))

    def test_refine_overall_review_code_uses_repair_contract_only_for_metadata_repair(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            paths = build_overall_review_paths(tmp_dir, "需求A")
            developer = DeveloperRuntime(
                selection=ReviewAgentSelection("codex", "gpt-5.4", "high", ""),
                worker=_FakeWorker(session_name="开发工程师-天魁星"),
                role_prompt="实现视角",
            )
            calls = []

            def fake_run(current_developer, **kwargs):  # noqa: ANN001
                calls.append(kwargs)
                if kwargs["label"] == "overall_review_refine_all_code":
                    paths["developer_output_path"].write_text("修订摘要\n", encoding="utf-8")
                return current_developer

            with patch("A08_OverallReview._run_overall_review_developer_turn", side_effect=fake_run), patch(
                "A08_OverallReview.check_develop_job",
                return_value="请补全工程师开发内容元数据，然后只返回 任务完成",
            ):
                _, code_change = refine_overall_review_code(
                    developer,
                    project_dir=tmp_dir,
                    requirement_name="需求A",
                    paths=paths,
                    review_msg="复核意见",
                )

        self.assertEqual(code_change, "修订摘要")
        self.assertEqual(
            [call["label"] for call in calls],
            ["overall_review_refine_all_code", "overall_review_refine_all_code_metadata_repair"],
        )
        self.assertEqual(calls[0]["result_contract"].terminal_status_tokens["completed"], ("修改完成",))
        self.assertEqual(calls[1]["result_contract"].terminal_status_tokens["completed"], ("任务完成", "修改完成"))

    def test_overall_review_refine_turn_uses_repair_contract_for_internal_repair_attempts(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            paths = build_overall_review_paths(tmp_dir, "需求A")
            developer = DeveloperRuntime(
                selection=ReviewAgentSelection("codex", "gpt-5.4", "high", ""),
                worker=_FakeWorker(session_name="开发工程师-天魁星"),
                role_prompt="实现视角",
            )
            calls = []

            def fake_run_task_result_turn_with_repair(**kwargs):  # noqa: ANN003
                calls.append(kwargs)
                return {}

            with patch(
                "A08_OverallReview.run_task_result_turn_with_repair",
                side_effect=fake_run_task_result_turn_with_repair,
            ):
                returned = _run_overall_review_developer_turn(
                    developer,
                    project_dir=tmp_dir,
                    requirement_name="需求A",
                    label="overall_review_refine_all_code",
                    prompt="请修复",
                    result_contract=build_overall_review_refine_result_contract(paths),
                    paths=paths,
                )

        self.assertIs(returned, developer)
        self.assertEqual(calls[0]["result_contract"].terminal_status_tokens["completed"], ("修改完成",))
        self.assertEqual(calls[0]["repair_result_contract"].terminal_status_tokens["completed"], ("任务完成", "修改完成"))

    def test_ensure_overall_review_inputs_requires_all_tasks_completed(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            paths = build_overall_review_paths(tmp_dir, "需求A")
            _write_required_inputs(paths)
            paths["task_json_path"].write_text(
                json.dumps({"M1": {"M1-T1": False}}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(RuntimeError, "任务单 JSON 必须全部完成"):
                ensure_overall_review_inputs(project_dir=tmp_dir, requirement_name="需求A")

    def test_run_overall_review_stage_reuses_live_handoffs_and_marks_state_passed(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            project_dir = Path(tmp_dir)
            paths = build_overall_review_paths(project_dir, "需求A")
            _write_required_inputs(paths)
            paths["task_json_path"].write_text(
                json.dumps({"M1": {"M1-T1": True}}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            developer = DeveloperRuntime(
                selection=ReviewAgentSelection("codex", "gpt-5.4", "high", ""),
                worker=_FakeWorker(session_name="开发工程师-天魁星"),
                role_prompt="实现视角",
            )
            reviewer_worker = _FakeWorker(session_name="测试工程师-天英星")
            reviewer_handoff = ReviewAgentHandoff(
                reviewer_key="测试工程师",
                role_name="测试工程师",
                role_prompt="测试视角",
                selection=ReviewAgentSelection("codex", "gpt-5.4", "high", ""),
                worker=reviewer_worker,
            )

            def fake_task_done(**kwargs):  # noqa: ANN001
                paths["merged_review_path"].write_text("", encoding="utf-8")
                return True

            with patch("A08_OverallReview.initialize_overall_review_reviewers", side_effect=lambda reviewers, **kwargs: list(reviewers)), patch(
                "A08_OverallReview._run_parallel_overall_reviewers",
                side_effect=lambda reviewers, **kwargs: list(reviewers),
            ), patch(
                "A08_OverallReview.repair_overall_review_outputs",
                side_effect=lambda reviewers, **kwargs: list(reviewers),
            ), patch(
                "A08_OverallReview.task_done",
                side_effect=fake_task_done,
            ), patch(
                "A08_OverallReview.create_developer_runtime",
            ) as create_developer_runtime_mock, patch(
                "A08_OverallReview._shutdown_workers",
                return_value=(),
            ):
                result = run_overall_review_stage(
                    ["--project-dir", str(project_dir), "--requirement-name", "需求A"],
                    developer_handoff=DevelopmentAgentHandoff(
                        selection=developer.selection,
                        role_prompt=developer.role_prompt,
                        worker=developer.worker,
                    ),
                    reviewer_handoff=(reviewer_handoff,),
                )

            self.assertIsInstance(result, OverallReviewStageResult)
            self.assertTrue(result.completed)
            self.assertTrue(json.loads(Path(result.state_path).read_text(encoding="utf-8"))["passed"])
            create_developer_runtime_mock.assert_not_called()

    def test_run_overall_review_stage_creates_developer_after_failed_review(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            project_dir = Path(tmp_dir)
            paths = build_overall_review_paths(project_dir, "需求A")
            _write_required_inputs(paths)
            paths["task_json_path"].write_text(
                json.dumps({"M1": {"M1-T1": True}}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            reviewer = ReviewerRuntime(
                reviewer_name="测试工程师",
                selection=ReviewAgentSelection("codex", "gpt-5.4", "high", ""),
                worker=_FakeWorker(session_name="测试工程师-天英星"),
                review_md_path=project_dir / "需求A_整体代码复核记录_测试工程师.md",
                review_json_path=project_dir / "需求A_整体复核记录_测试工程师.json",
                contract=_dummy_contract(),
            )
            developer = DeveloperRuntime(
                selection=ReviewAgentSelection("codex", "gpt-5.4", "high", ""),
                worker=_FakeWorker(session_name="开发工程师-天魁星"),
                role_prompt="实现视角",
            )
            task_done_results = iter([False, True])

            def fake_task_done(**kwargs):  # noqa: ANN001
                passed = next(task_done_results)
                paths["merged_review_path"].write_text("" if passed else "存在复核问题\n", encoding="utf-8")
                return passed

            with patch(
                "A08_OverallReview.resolve_developer_plan",
                return_value=DeveloperPlan(selection=developer.selection, role_prompt=developer.role_prompt),
            ), patch(
                "A08_OverallReview.resolve_overall_review_reviewer_specs",
                return_value=[DevelopmentReviewerSpec(role_name="测试工程师", role_prompt="测试视角", reviewer_key="测试工程师")],
            ), patch(
                "A08_OverallReview.collect_reviewer_agent_selections",
                return_value={"测试工程师": reviewer.selection},
            ), patch(
                "A08_OverallReview.build_reviewer_workers",
                return_value=[reviewer],
            ), patch(
                "A08_OverallReview.initialize_overall_review_reviewers",
                side_effect=lambda reviewers, **kwargs: list(reviewers),
            ), patch(
                "A08_OverallReview._run_parallel_overall_reviewers",
                side_effect=lambda reviewers, **kwargs: list(reviewers),
            ), patch(
                "A08_OverallReview.repair_overall_review_outputs",
                side_effect=lambda reviewers, **kwargs: list(reviewers),
            ), patch(
                "A08_OverallReview.task_done",
                side_effect=fake_task_done,
            ), patch(
                "A08_OverallReview.create_developer_runtime",
                return_value=developer,
            ) as create_developer_runtime_mock, patch(
                "A08_OverallReview.initialize_overall_review_developer",
                return_value=developer,
            ) as initialize_developer_mock, patch(
                "A08_OverallReview.refine_overall_review_code",
                return_value=(developer, "修订摘要"),
            ) as refine_mock, patch(
                "A08_OverallReview._shutdown_workers",
                return_value=(),
            ):
                result = run_overall_review_stage(
                    ["--project-dir", str(project_dir), "--requirement-name", "需求A"],
                )

            self.assertTrue(result.completed)
            create_developer_runtime_mock.assert_called_once()
            initialize_developer_mock.assert_called_once()
            refine_mock.assert_called_once()
            self.assertTrue(json.loads(Path(result.state_path).read_text(encoding="utf-8"))["passed"])

    def test_discover_live_development_handoffs_reads_a07_runtime_state(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            project_dir = Path(tmp_dir)
            resolved_project_dir = str(project_dir.resolve())
            runtime_root = project_dir / ".development_runtime"
            developer_state_path = runtime_root / "dev-worker" / "worker.state.json"
            reviewer_state_path = runtime_root / "reviewer-worker" / "worker.state.json"
            developer_state_path.parent.mkdir(parents=True, exist_ok=True)
            reviewer_state_path.parent.mkdir(parents=True, exist_ok=True)
            for state_path, payload in (
                (
                    developer_state_path,
                    {
                        "worker_id": "development-developer",
                        "session_name": "开发工程师-天魁星",
                        "work_dir": resolved_project_dir,
                        "project_dir": resolved_project_dir,
                        "requirement_name": "需求A",
                        "workflow_action": "stage.a07.start",
                        "role_prompt": "实现视角",
                        "updated_at": "2026-04-23T10:00:00+08:00",
                        "config": {
                            "vendor": "codex",
                            "model": "gpt-5.4",
                            "reasoning_effort": "high",
                            "proxy_url": "",
                        },
                    },
                ),
                (
                    reviewer_state_path,
                    {
                        "worker_id": "development-review-测试工程师",
                        "session_name": "测试工程师-天英星",
                        "work_dir": resolved_project_dir,
                        "project_dir": resolved_project_dir,
                        "requirement_name": "需求A",
                        "workflow_action": "stage.a07.start",
                        "role_prompt": "测试视角",
                        "role_name": "测试工程师",
                        "reviewer_key": "测试工程师",
                        "updated_at": "2026-04-23T10:00:01+08:00",
                        "config": {
                            "vendor": "codex",
                            "model": "gpt-5.4",
                            "reasoning_effort": "high",
                            "proxy_url": "",
                        },
                    },
                ),
            ):
                state_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            fake_developer_worker = _FakeWorker(session_name="开发工程师-天魁星")
            fake_reviewer_worker = _FakeWorker(session_name="测试工程师-天英星")

            with patch(
                "A08_OverallReview.load_worker_from_state_path",
                side_effect=[fake_developer_worker, fake_reviewer_worker],
            ):
                developer_handoff, reviewer_handoff = discover_live_development_handoffs(project_dir, "需求A")

        self.assertIsNotNone(developer_handoff)
        self.assertEqual(developer_handoff.selection.vendor, "codex")
        self.assertEqual(developer_handoff.role_prompt, "实现视角")
        self.assertEqual(len(reviewer_handoff), 1)
        self.assertEqual(reviewer_handoff[0].reviewer_key, "测试工程师")
        self.assertEqual(reviewer_handoff[0].role_prompt, "测试视角")

    def test_run_overall_review_stage_uses_discovered_live_reviewer_handoff_for_specs_and_workers(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            project_dir = Path(tmp_dir)
            paths = build_overall_review_paths(project_dir, "需求A")
            _write_required_inputs(paths)
            paths["task_json_path"].write_text(
                json.dumps({"M1": {"M1-T1": True}}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            developer_handoff = DevelopmentAgentHandoff(
                selection=ReviewAgentSelection("codex", "gpt-5.4", "high", ""),
                role_prompt="实现视角",
                worker=_FakeWorker(session_name="开发工程师-天魁星"),
            )
            dead_reviewer_handoff = ReviewAgentHandoff(
                reviewer_key="测试工程师",
                role_name="测试工程师",
                role_prompt="已失效配置",
                selection=ReviewAgentSelection("codex", "gpt-5.4", "high", ""),
                worker=_FakeWorker(session_name="测试工程师-已失效", session_exists_value=False),
            )
            live_reviewer_handoff = ReviewAgentHandoff(
                reviewer_key="测试工程师",
                role_name="测试工程师",
                role_prompt="发现到的存活配置",
                selection=ReviewAgentSelection("claude", "sonnet", "high", ""),
                worker=_FakeWorker(session_name="测试工程师-天英星"),
            )
            reviewer = ReviewerRuntime(
                reviewer_name="测试工程师",
                selection=live_reviewer_handoff.selection,
                worker=live_reviewer_handoff.worker,
                review_md_path=project_dir / "需求A_整体代码复核记录_测试工程师.md",
                review_json_path=project_dir / "需求A_整体复核记录_测试工程师.json",
                contract=_dummy_contract(),
            )

            def fake_task_done(**kwargs):  # noqa: ANN001
                paths["merged_review_path"].write_text("", encoding="utf-8")
                return True

            with patch(
                "A08_OverallReview.discover_live_development_handoffs",
                return_value=(None, (live_reviewer_handoff,)),
            ), patch(
                "A08_OverallReview.resolve_overall_review_reviewer_specs",
                return_value=[DevelopmentReviewerSpec(role_name="测试工程师", role_prompt="测试视角", reviewer_key="测试工程师")],
            ) as resolve_specs_mock, patch(
                "A08_OverallReview.collect_reviewer_agent_selections",
                return_value={},
            ), patch(
                "A08_OverallReview.build_reviewer_workers",
                return_value=[reviewer],
            ) as build_reviewer_workers_mock, patch(
                "A08_OverallReview.initialize_overall_review_reviewers",
                side_effect=lambda reviewers, **kwargs: list(reviewers),
            ), patch(
                "A08_OverallReview._run_parallel_overall_reviewers",
                side_effect=lambda reviewers, **kwargs: list(reviewers),
            ), patch(
                "A08_OverallReview.repair_overall_review_outputs",
                side_effect=lambda reviewers, **kwargs: list(reviewers),
            ), patch(
                "A08_OverallReview.task_done",
                side_effect=fake_task_done,
            ), patch(
                "A08_OverallReview._shutdown_workers",
                return_value=(),
            ):
                result = run_overall_review_stage(
                    ["--project-dir", str(project_dir), "--requirement-name", "需求A"],
                    developer_handoff=developer_handoff,
                    reviewer_handoff=(dead_reviewer_handoff,),
                )

            self.assertTrue(result.completed)
            self.assertEqual(
                resolve_specs_mock.call_args.kwargs.get("reviewer_handoff"),
                (live_reviewer_handoff,),
            )
            self.assertEqual(
                build_reviewer_workers_mock.call_args.kwargs.get("reviewer_handoff"),
                (live_reviewer_handoff,),
            )
