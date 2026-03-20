from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import A10_owner_agent_workflow as workflow_module
from A10_owner_agent_workflow import (
    _development_post_review_sync_issues,
    _artifacts_readiness_error,
    _owner_completion_issues,
    reviewer_reply_is_complete,
)
from B01_codex_utils import _looks_like_transient_reply, _snapshot_allows_reply_completion


class OwnerCompletionGuardTests(unittest.TestCase):
    def test_artifacts_readiness_error_reports_missing_and_empty_files(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            existing_empty = tmp_path / "empty.md"
            existing_empty.write_text("", encoding="utf-8")
            missing = tmp_path / "missing.md"
            error = _artifacts_readiness_error([existing_empty, missing])
            self.assertIn("missing.md", error)
            self.assertIn("empty.md", error)

    def test_owner_completion_issues_require_token_and_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            spec = tmp_path / "01_requirement_spec.md"
            spec.write_text("# spec", encoding="utf-8")
            clarification = tmp_path / "01_clarification.md"
            issues = _owner_completion_issues(
                owner_summary="需求已整理，但还没结束标记。",
                required_artifacts=[spec, clarification],
                completion_token="[[ACX_STAGE_DONE]]",
            )
            self.assertTrue(any("[[ACX_STAGE_DONE]]" in issue for issue in issues))
            self.assertTrue(any("01_clarification.md" in issue for issue in issues))

    def test_owner_completion_token_must_be_standalone_marker(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            spec = tmp_path / "01_requirement_spec.md"
            clarification = tmp_path / "01_clarification.md"
            spec.write_text("# spec", encoding="utf-8")
            clarification.write_text("# clarification", encoding="utf-8")

            issues = _owner_completion_issues(
                owner_summary="当前不会给出 [[ACX_STAGE_DONE]]，仍在等待 reviewer。",
                required_artifacts=[spec, clarification],
                completion_token="[[ACX_STAGE_DONE]]",
            )
            self.assertTrue(any("回复缺少完成标记" in issue for issue in issues))

    def test_owner_completion_token_allows_sentence_ending_sentinel(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            spec = tmp_path / "01_requirement_spec.md"
            clarification = tmp_path / "01_clarification.md"
            spec.write_text("# spec", encoding="utf-8")
            clarification.write_text("# clarification", encoding="utf-8")

            issues = _owner_completion_issues(
                owner_summary="需求和澄清文档均已落盘，等待 reviewer 继续核验。[[ACX_STAGE_DONE]]",
                required_artifacts=[spec, clarification],
                completion_token="[[ACX_STAGE_DONE]]",
            )
            self.assertEqual([], issues)

    def test_tool_summary_reply_is_treated_as_transient(self):
        reply = "Explored\n  └ Read 01_requirement_spec.md"
        self.assertTrue(_looks_like_transient_reply(reply))

    def test_queued_message_fragment_is_treated_as_transient(self):
        reply = "immediately)\n\n    上一轮摘要:\n    …"
        self.assertTrue(_looks_like_transient_reply(reply))

    def test_processing_snapshot_is_not_treated_as_reply_complete(self):
        snapshot = SimpleNamespace(detected_status="processing", confirmed_status="processing")
        self.assertFalse(_snapshot_allows_reply_completion(snapshot))

    def test_completed_snapshot_is_treated_as_reply_complete(self):
        snapshot = SimpleNamespace(detected_status="completed", confirmed_status="completed")
        self.assertTrue(_snapshot_allows_reply_completion(snapshot))

    def test_development_post_review_sync_issues_report_schedule_and_markdown(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            schedule = tmp_path / "03_schedule.json"
            task_doc = tmp_path / "03_task_plan.md"
            schedule.write_text(
                """
{
  "milestones": [
    {
      "milestone_id": "M1",
      "tasks": [
        {"task_id": "M1-T1", "status": "todo"}
      ]
    }
  ]
}
""".strip(),
                encoding="utf-8",
            )
            task_doc.write_text("- [ ] M1-T1 Placeholder", encoding="utf-8")

            with patch.object(workflow_module, "WORK_DIR", tmp_path), patch.object(
                workflow_module, "task_schedule_json", "03_schedule.json"
            ), patch.object(workflow_module, "task_md", "03_task_plan.md"):
                issues = _development_post_review_sync_issues("M1-T1")

            self.assertTrue(any("03_schedule.json" in issue for issue in issues))
            self.assertTrue(any("03_task_plan.md" in issue for issue in issues))

    def test_reviewer_reply_validator_requires_issue_details_for_revise(self):
        partial_reply = """[[ACX_VERDICT:REVISE]]
phase_id: requirement_specification
owner_id: owner
task_id: NONE
artifact_sha: abc
review_round: 3
issues_count: 1
summary: 需要补充验证细节。
issues:
"""
        complete_reply = partial_reply + "\n1. 需要明确 60fps 的验证步骤。\n"

        self.assertFalse(
            reviewer_reply_is_complete(
                text=partial_reply,
                expected_phase_id="requirement_specification",
                expected_review_round=3,
                expected_artifact_sha="abc",
                expected_task_id="",
            )
        )
        self.assertTrue(
            reviewer_reply_is_complete(
                text=complete_reply,
                expected_phase_id="requirement_specification",
                expected_review_round=3,
                expected_artifact_sha="abc",
                expected_task_id="",
            )
        )

    def test_reviewer_pass_allows_numbered_wu_issue_line(self):
        reply = """[[ACX_VERDICT:PASS]]
phase_id: requirement_specification
owner_id: owner
task_id: NONE
artifact_sha: abc
review_round: 3
issues_count: 0
summary: 文档清晰完整。
issues:
1. 无
"""
        self.assertTrue(
            reviewer_reply_is_complete(
                text=reply,
                expected_phase_id="requirement_specification",
                expected_review_round=3,
                expected_artifact_sha="abc",
                expected_task_id="",
            )
        )


if __name__ == "__main__":
    unittest.main()
