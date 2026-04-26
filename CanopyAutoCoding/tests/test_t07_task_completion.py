from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from canopy_core.runtime.contracts import (
    TASK_RESULT_COMPLETED,
    TASK_RESULT_ERROR,
    TASK_RESULT_HITL,
    TASK_RESULT_READY,
    TASK_RESULT_REVIEW_FAIL,
    TASK_RESULT_REVIEW_PASS,
    TASK_STATUS_DONE,
    TASK_STATUS_RUNNING,
    TaskResultContract,
    finalize_task_result,
    materialize_task_result,
    read_task_status,
    resolve_task_result_decision,
    write_task_status,
)


class TaskResultContractsTests(unittest.TestCase):
    def _build_contract(
        self,
        root: Path,
        *,
        mode: str,
        required_artifacts: dict[str, Path] | None = None,
        optional_artifacts: dict[str, Path] | None = None,
        expected_statuses: tuple[str, ...] = (
            TASK_RESULT_READY,
            TASK_RESULT_COMPLETED,
            TASK_RESULT_HITL,
            TASK_RESULT_ERROR,
            TASK_RESULT_REVIEW_PASS,
            TASK_RESULT_REVIEW_FAIL,
        ),
    ) -> tuple[TaskResultContract, Path, Path]:
        result_path = root / "result.json"
        task_status_path = root / "task_status.json"
        write_task_status(task_status_path, status=TASK_STATUS_RUNNING)
        return (
            TaskResultContract(
                turn_id="turn-1",
                phase="phase-1",
                task_kind=mode,
                mode=mode,
                expected_statuses=expected_statuses,
                required_artifacts=required_artifacts or {},
                optional_artifacts=optional_artifacts or {},
            ),
            result_path,
            task_status_path,
        )

    def test_finalize_task_result_writes_ready_result_for_ba_resume(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            requirements_clear = root / "需求澄清.md"
            requirements_clear.write_text("现有需求澄清\n", encoding="utf-8")
            contract, result_path, status_path = self._build_contract(
                root,
                mode="a03_ba_resume",
                optional_artifacts={"requirements_clear": requirements_clear},
            )
            task_result = finalize_task_result(
                contract=contract,
                result_path=result_path,
                task_status_path=status_path,
            )
            self.assertEqual(task_result.payload["status"], TASK_RESULT_READY)
            self.assertEqual(read_task_status(status_path), TASK_STATUS_DONE)

    def test_resolve_task_result_decision_supports_detailed_design_feedback_states(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            ask_human = root / "ask_human.md"
            ba_feedback = root / "ba_feedback.md"
            detailed_design = root / "详细设计.md"
            contract, _, _ = self._build_contract(
                root,
                mode="a05_detailed_design_feedback",
                optional_artifacts={
                    "ask_human": ask_human,
                    "ba_feedback": ba_feedback,
                    "detailed_design": detailed_design,
                },
            )

            ask_human.write_text("请补充边界\n", encoding="utf-8")
            self.assertEqual(resolve_task_result_decision(contract).status, TASK_RESULT_HITL)

            ask_human.write_text("", encoding="utf-8")
            ba_feedback.write_text("已修订\n", encoding="utf-8")
            detailed_design.write_text("设计正文\n", encoding="utf-8")
            self.assertEqual(resolve_task_result_decision(contract).status, TASK_RESULT_COMPLETED)

    def test_finalize_task_result_validates_task_split_json_generate(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            task_json = root / "任务单.json"
            contract, result_path, status_path = self._build_contract(
                root,
                mode="a06_task_split_json_generate",
                required_artifacts={"task_json": task_json},
            )
            task_json.write_text(json.dumps({"M1": {"M1-T1": True}}, ensure_ascii=False, indent=2), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "任务单 JSON 未生成有效初始结构"):
                finalize_task_result(contract=contract, result_path=result_path, task_status_path=status_path)

            task_json.write_text(json.dumps({"M1": {"M1-T1": False}}, ensure_ascii=False, indent=2), encoding="utf-8")
            task_result = finalize_task_result(contract=contract, result_path=result_path, task_status_path=status_path)
            self.assertEqual(task_result.payload["status"], TASK_RESULT_COMPLETED)

    def test_resolve_task_result_decision_supports_a07_init_and_completion(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            ask_human = root / "与人类交流.md"
            hitl_record = root / "人机交互澄清记录.md"
            developer_output = root / "工程师开发内容.md"

            init_contract, _, _ = self._build_contract(
                root,
                mode="a07_developer_init",
                optional_artifacts={"ask_human": ask_human, "hitl_record": hitl_record},
            )
            ask_human.write_text("请确认接口差异\n", encoding="utf-8")
            self.assertEqual(resolve_task_result_decision(init_contract).status, TASK_RESULT_HITL)
            ask_human.write_text("", encoding="utf-8")
            self.assertEqual(resolve_task_result_decision(init_contract).status, TASK_RESULT_READY)

            complete_contract, result_path, status_path = self._build_contract(
                root,
                mode="a07_developer_task_complete",
                required_artifacts={"developer_output": developer_output},
            )
            developer_output.write_text("- 完成任务\n", encoding="utf-8")
            task_result = finalize_task_result(
                contract=complete_contract,
                result_path=result_path,
                task_status_path=status_path,
            )
            self.assertEqual(task_result.payload["status"], TASK_RESULT_COMPLETED)

    def test_resolve_task_result_decision_supports_a07_review_feedback_states(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            ask_human = root / "与人类交流.md"
            hitl_record = root / "人机交互澄清记录.md"
            developer_output = root / "工程师开发内容.md"
            contract, _, _ = self._build_contract(
                root,
                mode="a07_developer_review_feedback",
                optional_artifacts={
                    "ask_human": ask_human,
                    "hitl_record": hitl_record,
                    "developer_output": developer_output,
                },
                expected_statuses=(TASK_RESULT_HITL, TASK_RESULT_COMPLETED),
            )

            developer_output.write_text("- 旧的开发内容\n", encoding="utf-8")
            ask_human.write_text("请人类确认评审冲突\n", encoding="utf-8")
            self.assertEqual(resolve_task_result_decision(contract).status, TASK_RESULT_HITL)

            ask_human.write_text("", encoding="utf-8")
            self.assertEqual(resolve_task_result_decision(contract).status, TASK_RESULT_COMPLETED)

    def test_resolve_task_result_decision_supports_reviewer_round(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            review_json = root / "review.json"
            review_md = root / "review.md"
            contract, _, _ = self._build_contract(
                root,
                mode="a03_reviewer_round",
                required_artifacts={"review_json": review_json, "review_md": review_md},
            )

            review_json.write_text(json.dumps([{"task_name": "需求评审", "review_pass": True}], ensure_ascii=False), encoding="utf-8")
            review_md.write_text("", encoding="utf-8")
            self.assertEqual(resolve_task_result_decision(contract).status, TASK_RESULT_REVIEW_PASS)

            review_json.write_text(json.dumps([{"task_name": "需求评审", "review_pass": False}], ensure_ascii=False), encoding="utf-8")
            review_md.write_text("未通过\n", encoding="utf-8")
            self.assertEqual(resolve_task_result_decision(contract).status, TASK_RESULT_REVIEW_FAIL)

            review_json.write_text(json.dumps({"task_name": "需求评审", "review_pass": True}, ensure_ascii=False), encoding="utf-8")
            review_md.write_text("", encoding="utf-8")
            self.assertEqual(resolve_task_result_decision(contract).status, TASK_RESULT_REVIEW_PASS)

    def test_resolve_task_result_decision_supports_requirement_intake_variants(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            original_requirement = root / "原始需求.md"
            ask_human = root / "ask_human.md"
            hitl_record = root / "record.md"
            contract, _, _ = self._build_contract(
                root,
                mode="a02_requirement_intake",
                optional_artifacts={
                    "original_requirement": original_requirement,
                    "ask_human": ask_human,
                    "hitl_record": hitl_record,
                },
            )

            ask_human.write_text("请补充原始链接\n", encoding="utf-8")
            hitl_record.write_text("round1\n", encoding="utf-8")
            self.assertEqual(resolve_task_result_decision(contract).status, TASK_RESULT_HITL)

            hitl_record.write_text("", encoding="utf-8")
            self.assertEqual(resolve_task_result_decision(contract).status, TASK_RESULT_HITL)

            original_requirement.write_text("正文\n", encoding="utf-8")
            self.assertEqual(resolve_task_result_decision(contract).status, TASK_RESULT_COMPLETED)

    def test_materialize_task_result_writes_payload_without_task_status_side_effect(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            artifact = root / "artifact.md"
            artifact.write_text("artifact\n", encoding="utf-8")
            contract, result_path, status_path = self._build_contract(
                root,
                mode="a05_ba_init",
                optional_artifacts={"requirements_clear": artifact},
            )
            task_result = materialize_task_result(
                contract=contract,
                result_path=result_path,
                status=TASK_RESULT_READY,
                summary="ready",
            )
            self.assertEqual(task_result.payload["status"], TASK_RESULT_READY)
            self.assertEqual(read_task_status(status_path), TASK_STATUS_RUNNING)


if __name__ == "__main__":
    unittest.main()
