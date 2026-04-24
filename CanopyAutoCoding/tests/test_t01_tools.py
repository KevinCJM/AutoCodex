from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from T01_tools import check_all_reviews_passed, task_done


class T01ToolsTests(unittest.TestCase):
    def test_check_all_reviews_passed_returns_false_when_explicit_required_json_is_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            review_json = root / "评审记录_R1.json"
            review_json.write_text(
                json.dumps([{"task_name": "M1-T1", "review_pass": True}], ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

            passed = check_all_reviews_passed(
                root,
                "M1-T1",
                json_files=[review_json, root / "评审记录_R2.json"],
            )

        self.assertFalse(passed)

    def test_task_done_returns_false_when_explicit_required_md_is_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            task_json = root / "任务单.json"
            task_json.write_text(
                json.dumps({"M1": {"M1-T1": False}}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            review_json = root / "评审记录_R1.json"
            review_json.write_text(
                json.dumps([{"task_name": "M1-T1", "review_pass": True}], ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            review_md = root / "评审记录_R1.md"
            review_md.write_text("", encoding="utf-8")

            passed = task_done(
                root,
                task_json,
                task_name="M1-T1",
                json_files=[review_json],
                md_files=[review_md, root / "评审记录_R2.md"],
            )

            payload = json.loads(task_json.read_text(encoding="utf-8"))

        self.assertFalse(passed)
        self.assertFalse(payload["M1"]["M1-T1"])

    def test_task_done_returns_false_when_explicit_required_json_is_missing_even_if_md_is_empty(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            task_json = root / "任务单.json"
            task_json.write_text(
                json.dumps({"M1": {"M1-T1": False}}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            review_json = root / "评审记录_R1.json"
            review_json.write_text(
                json.dumps([{"task_name": "M1-T1", "review_pass": True}], ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            review_md_1 = root / "评审记录_R1.md"
            review_md_2 = root / "评审记录_R2.md"
            review_md_1.write_text("", encoding="utf-8")
            review_md_2.write_text("", encoding="utf-8")

            passed = task_done(
                root,
                task_json,
                task_name="M1-T1",
                json_files=[review_json, root / "评审记录_R2.json"],
                md_files=[review_md_1, review_md_2],
            )

            payload = json.loads(task_json.read_text(encoding="utf-8"))

        self.assertFalse(passed)
        self.assertFalse(payload["M1"]["M1-T1"])


if __name__ == "__main__":
    unittest.main()
