from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from T01_tools import task_done
from tmux_core.stage_kernel.stage_audit import (
    STAGE_AUDIT_SCHEMA_VERSION,
    StageAuditRunContext,
    append_stage_audit_record,
    begin_stage_audit_run,
    build_stage_audit_log_path,
    record_before_cleanup,
)


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _read_valid_jsonl(path: Path) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        records.append(payload)
    return records


class StageAuditTests(unittest.TestCase):
    def test_begin_stage_audit_run_appends_stage_started_and_increments_stage_run(self):
        with TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)

            first = begin_stage_audit_run(project_dir, "需求: A", "A03", metadata={"trigger": "unit"})
            second = begin_stage_audit_run(project_dir, "需求: A", "A03")

            self.assertIsNotNone(first)
            self.assertIsNotNone(second)
            assert first is not None
            assert second is not None
            self.assertEqual(first.stage_run_index, 1)
            self.assertEqual(second.stage_run_index, 2)
            self.assertEqual(first.audit_log_path, project_dir.resolve() / "需求_A_A03_流水记录.jsonl")

            records = _read_jsonl(first.audit_log_path)
            self.assertEqual([record["record_index"] for record in records], [1, 2])
            self.assertEqual([record["stage_run_index"] for record in records], [1, 2])
            self.assertEqual(records[0]["schema_version"], STAGE_AUDIT_SCHEMA_VERSION)
            self.assertEqual(records[0]["event_type"], "stage_started")
            self.assertEqual(records[0]["event_scope"], "stage")
            self.assertEqual(records[0]["source_paths"], {})
            self.assertEqual(records[0]["snapshots"], {})

    def test_append_record_preserves_raw_text_and_json_escaping(self):
        with TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            source = project_dir / "开发.md"
            text = '第一行 "quote"\n```python\nprint("\\\\")\n```\n中文\n'
            source.write_text(text, encoding="utf-8")
            context = begin_stage_audit_run(project_dir, "需求", "A07")
            assert context is not None

            ok = append_stage_audit_record(
                context,
                "developer_output",
                {"developer_output": source},
                review_round_index=3,
                task_name="任务一",
            )

            self.assertTrue(ok)
            record = _read_jsonl(context.audit_log_path)[-1]
            self.assertEqual(record["event_scope"], "task")
            self.assertEqual(record["task_name"], "任务一")
            self.assertEqual(record["review_round_index"], 3)
            self.assertIsNone(record["hitl_round_index"])
            self.assertEqual(record["snapshots"]["developer_output"], text)

    def test_snapshot_overrides_allow_runtime_payload_audit_truth(self):
        with TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            hitl_record = project_dir / "hitl.md"
            hitl_record.write_text("旧记录\n", encoding="utf-8")
            context = begin_stage_audit_run(project_dir, "需求", "A03")
            assert context is not None

            ok = append_stage_audit_record(
                context,
                "hitl_answer",
                {"human_answer": "", "hitl_record": hitl_record},
                hitl_round_index=2,
                metadata={"human_answer_source": "runtime_payload"},
                snapshot_overrides={"human_answer": '人类回答 "A"\n第二行'},
            )

            self.assertTrue(ok)
            record = _read_jsonl(context.audit_log_path)[-1]
            self.assertEqual(record["source_paths"]["human_answer"], "")
            self.assertEqual(record["snapshots"]["human_answer"], '人类回答 "A"\n第二行')
            self.assertEqual(record["snapshots"]["hitl_record"], "旧记录\n")

    def test_missing_and_read_error_sources_are_recorded_without_failure(self):
        with TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            bad_utf8 = project_dir / "bad.md"
            bad_utf8.write_bytes(b"\xff")
            missing = project_dir / "missing.md"
            context = begin_stage_audit_run(project_dir, "需求", "A04")
            assert context is not None

            ok = append_stage_audit_record(
                context,
                "feedback_written",
                {"ba_feedback": bad_utf8, "merged_review": missing},
            )

            self.assertTrue(ok)
            record = _read_jsonl(context.audit_log_path)[-1]
            metadata = record["metadata"]
            self.assertEqual(record["snapshots"]["ba_feedback"], "")
            self.assertEqual(record["snapshots"]["merged_review"], "")
            self.assertIn(str(missing.resolve()), metadata["missing_files"])
            self.assertEqual(metadata["read_errors"][0]["path"], str(bad_utf8.resolve()))

    def test_dynamic_reviewer_arrays_are_stable_and_include_hashes(self):
        with TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            merged = project_dir / "评审.md"
            reviewer_md = project_dir / "reviewer.md"
            reviewer_json = project_dir / "reviewer.json"
            merged.write_text("汇总\n", encoding="utf-8")
            reviewer_md.write_text("明细\n", encoding="utf-8")
            reviewer_json.write_text('[{"status":"failed"}]\n', encoding="utf-8")
            context = begin_stage_audit_run(project_dir, "需求", "A04")
            assert context is not None

            ok = append_stage_audit_record(
                context,
                "review_merged",
                {"merged_review": merged},
                reviewer_markdown_paths=[reviewer_md],
                reviewer_json_paths=[reviewer_json],
                review_round_index=1,
                metadata={"agent_names": ["审核员"], "session_names": ["s1"]},
            )

            self.assertTrue(ok)
            record = _read_jsonl(context.audit_log_path)[-1]
            self.assertEqual(record["source_paths"]["reviewer_markdowns"], [str(reviewer_md.resolve())])
            self.assertEqual(record["source_paths"]["reviewer_jsons"], [str(reviewer_json.resolve())])
            self.assertEqual(record["snapshots"]["reviewer_markdowns"][0]["content"], "明细\n")
            self.assertRegex(record["snapshots"]["reviewer_markdowns"][0]["sha256"], r"^[0-9a-f]{64}$")
            self.assertEqual(record["metadata"]["agent_names"], ["审核员"])
            self.assertEqual(record["metadata"]["agent_name"], "")

    def test_review_event_with_no_reviewer_files_writes_empty_arrays(self):
        with TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            merged = project_dir / "评审.md"
            merged.write_text("汇总\n", encoding="utf-8")
            context = begin_stage_audit_run(project_dir, "需求", "A04")
            assert context is not None

            ok = append_stage_audit_record(context, "review_merged", {"merged_review": merged})

            self.assertTrue(ok)
            record = _read_jsonl(context.audit_log_path)[-1]
            self.assertEqual(record["source_paths"]["reviewer_markdowns"], [])
            self.assertEqual(record["source_paths"]["reviewer_jsons"], [])
            self.assertEqual(record["snapshots"]["reviewer_markdowns"], [])
            self.assertEqual(record["snapshots"]["reviewer_jsons"], [])

    def test_bad_existing_lines_are_ignored_and_before_cleanup_helper_writes_event(self):
        with TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            audit_path = build_stage_audit_log_path(project_dir, "需求", "A05")
            audit_path.write_text("not json\n", encoding="utf-8")
            context = begin_stage_audit_run(project_dir, "需求", "A05")
            assert context is not None
            old_file = project_dir / "old.md"
            old_file.write_text("旧内容\n", encoding="utf-8")

            ok = record_before_cleanup(context, {"merged_review": old_file})

            self.assertTrue(ok)
            records = _read_valid_jsonl(audit_path)
            self.assertEqual(records[-1]["record_index"], 2)
            self.assertEqual(records[-1]["event_type"], "before_cleanup")
            self.assertEqual(records[-1]["event_scope"], "stage")
            self.assertEqual(records[-1]["snapshots"]["merged_review"], "旧内容\n")
            self.assertEqual(records[-1]["source_paths"]["reviewer_markdowns"], [])

    def test_unknown_event_and_write_failure_return_false(self):
        with TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            context = begin_stage_audit_run(project_dir, "需求", "A06")
            assert context is not None

            self.assertFalse(append_stage_audit_record(context, "unknown_event", {}))

            bad_context = StageAuditRunContext(
                project_dir=project_dir,
                requirement_name="需求",
                stage="A06",
                audit_log_path=project_dir,
                stage_run_index=1,
            )
            self.assertFalse(append_stage_audit_record(bad_context, "stage_passed", {}))

    def test_task_done_ignores_stage_audit_jsonl_files(self):
        with TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            task_json = project_dir / "需求_任务单.json"
            task_json.write_text(json.dumps({"M1": {"M1-T1": False}}, ensure_ascii=False), encoding="utf-8")
            (project_dir / "需求_评审记录_R1.json").write_text(
                json.dumps([{"task_name": "M1-T1", "review_pass": True}], ensure_ascii=False),
                encoding="utf-8",
            )
            (project_dir / "需求_代码评审记录_R1.md").write_text("", encoding="utf-8")
            (project_dir / "需求_A07_流水记录.jsonl").write_text(
                '{"event_type":"review_merged","snapshots":{"merged_review":"不应被 task_done 读取"}}\n',
                encoding="utf-8",
            )

            passed = task_done(
                directory=project_dir,
                file_path=task_json,
                task_name="M1-T1",
                json_pattern="需求_评审记录_*.json",
                md_pattern="需求_代码评审记录_*.md",
                md_output_name="需求_代码评审记录.md",
            )

            self.assertTrue(passed)
            self.assertTrue(json.loads(task_json.read_text(encoding="utf-8"))["M1"]["M1-T1"])
            self.assertEqual((project_dir / "需求_代码评审记录.md").read_text(encoding="utf-8"), "")


if __name__ == "__main__":
    unittest.main()
