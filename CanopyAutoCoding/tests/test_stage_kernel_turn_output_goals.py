from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from canopy_core.runtime.contracts import (
    TaskResultContract,
    TurnFileContract,
    TurnFileResult,
    materialize_task_result,
    observe_completion_state,
    observe_task_result_state,
)
from canopy_core.runtime.tmux_runtime import (
    TASK_RESULT_CONTRACT_ERROR_PREFIX,
    TURN_ARTIFACT_CONTRACT_ERROR_PREFIX,
)
from canopy_core.stage_kernel.turn_output_goals import (
    CompletionTurnGoal,
    OutcomeGoal,
    TaskTurnGoal,
    run_completion_turn_with_repair,
    run_task_result_turn_with_repair,
)


class _FakeTaskWorker:
    def __init__(self, responses: list[callable]) -> None:
        self._responses = list(responses)
        self.prompts: list[str] = []
        self.result_contracts: list[TaskResultContract | None] = []
        self.current_task_result_path = ""

    def run_turn(self, *, label, prompt, result_contract=None, completion_contract=None, timeout_sec):  # noqa: ANN001, ARG002
        self.prompts.append(prompt)
        self.result_contracts.append(result_contract)
        response = self._responses.pop(0)
        return response(result_contract=result_contract, completion_contract=completion_contract)


class TurnOutputGoalsTests(unittest.TestCase):
    def test_observe_task_result_state_reports_missing_required_alias(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            result_path = root / "result.json"
            ask_human = root / "与人类交流.md"
            contract = TaskResultContract(
                turn_id="turn-1",
                phase="a06_task_split_review_limit_force_hitl",
                task_kind="a06_task_split_review_limit_force_hitl",
                mode="a06_task_split_review_limit_force_hitl",
                expected_statuses=("hitl",),
                optional_artifacts={"ask_human": ask_human},
            )
            observation = observe_task_result_state(contract, result_path)
            self.assertEqual(observation.observed_status, "")
            self.assertIn("result.json", observation.last_validation_error)

    def test_run_task_result_turn_with_repair_retries_when_force_hitl_output_missing(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            result_path = root / "result.json"
            ask_human = root / "与人类交流.md"
            contract = TaskResultContract(
                turn_id="turn-1",
                phase="a06_task_split_review_limit_force_hitl",
                task_kind="a06_task_split_review_limit_force_hitl",
                mode="a06_task_split_review_limit_force_hitl",
                expected_statuses=("hitl",),
                optional_artifacts={"ask_human": ask_human},
            )

            def first_response(*, result_contract=None, completion_contract=None):  # noqa: ANN001, ARG001
                worker.current_task_result_path = str(result_path)
                return SimpleNamespace(
                    ok=False,
                    clean_output=f"{TASK_RESULT_CONTRACT_ERROR_PREFIX}: missing ask_human",
                )

            def second_response(*, result_contract=None, completion_contract=None):  # noqa: ANN001, ARG001
                ask_human.write_text("请补充边界条件\n", encoding="utf-8")
                materialize_task_result(
                    contract=contract,
                    result_path=result_path,
                    status="hitl",
                    summary="需要 HITL",
                )
                worker.current_task_result_path = str(result_path)
                return SimpleNamespace(ok=True, clean_output=json.dumps({"status": "hitl"}, ensure_ascii=False))

            worker = _FakeTaskWorker([first_response, second_response])
            payload = run_task_result_turn_with_repair(
                worker=worker,
                label="force_hitl_turn",
                prompt="原始 prompt",
                result_contract=contract,
                parse_result_payload=json.loads,
                turn_goal=TaskTurnGoal(
                    goal_id="force_hitl",
                    outcomes={"hitl": OutcomeGoal(status="hitl", required_aliases=("ask_human",))},
                ),
                stage_label="任务拆分",
                role_label="需求分析师",
            )
            self.assertEqual(payload["status"], "hitl")
            self.assertEqual(len(worker.prompts), 2)
            self.assertIn("遗漏了本轮协议要求的产物", worker.prompts[1])
            self.assertIn(str(ask_human), worker.prompts[1])

    def test_run_task_result_turn_with_repair_uses_repair_result_contract_on_retry(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            result_path = root / "result.json"
            output_path = root / "developer.md"
            initial_contract = TaskResultContract(
                turn_id="turn-initial",
                phase="phase",
                task_kind="kind",
                mode="mode",
                expected_statuses=("completed",),
                required_artifacts={"developer_output": output_path},
                terminal_status_tokens={"completed": ("修改完成",)},
            )
            repair_contract = TaskResultContract(
                turn_id="turn-repair",
                phase="phase",
                task_kind="kind",
                mode="mode",
                expected_statuses=("completed",),
                required_artifacts={"developer_output": output_path},
                terminal_status_tokens={"completed": ("任务完成", "修改完成")},
            )

            def first_response(*, result_contract=None, completion_contract=None):  # noqa: ANN001, ARG001
                worker.current_task_result_path = str(result_path)
                return SimpleNamespace(
                    ok=False,
                    clean_output=f"{TASK_RESULT_CONTRACT_ERROR_PREFIX}: missing developer_output",
                )

            def second_response(*, result_contract=None, completion_contract=None):  # noqa: ANN001, ARG001
                output_path.write_text("修复摘要\n", encoding="utf-8")
                materialize_task_result(
                    contract=result_contract,
                    result_path=result_path,
                    status="completed",
                    summary="任务完成",
                )
                worker.current_task_result_path = str(result_path)
                return SimpleNamespace(ok=True, clean_output=json.dumps({"status": "completed"}, ensure_ascii=False))

            worker = _FakeTaskWorker([first_response, second_response])
            payload = run_task_result_turn_with_repair(
                worker=worker,
                label="refine_turn",
                prompt="原始 prompt",
                result_contract=initial_contract,
                repair_result_contract=repair_contract,
                parse_result_payload=json.loads,
                turn_goal=TaskTurnGoal(
                    goal_id="refine",
                    outcomes={"completed": OutcomeGoal(status="completed", required_aliases=("developer_output",))},
                ),
            )

        self.assertEqual(payload["status"], "completed")
        self.assertIs(worker.result_contracts[0], initial_contract)
        self.assertIs(worker.result_contracts[1], repair_contract)

    def test_run_completion_turn_with_repair_retries_when_review_md_missing(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            review_json = root / "review.json"
            review_md = root / "review.md"

            def validator(status_path: Path) -> TurnFileResult:
                payload = json.loads(status_path.read_text(encoding="utf-8"))
                matched = payload[0]
                review_pass = matched["review_pass"]
                review_md_empty = not review_md.read_text(encoding="utf-8").strip()
                if review_pass and not review_md_empty:
                    raise ValueError("通过时 review_md 必须为空")
                if (not review_pass) and review_md_empty:
                    raise ValueError("未通过时 review_md 不能为空")
                return TurnFileResult(
                    status_path=str(status_path),
                    payload={"review_pass": review_pass},
                    artifact_paths={"review_json": str(review_json), "review_md": str(review_md)},
                    artifact_hashes={},
                    validated_at="0",
                )

            contract = TurnFileContract(
                turn_id="reviewer-turn",
                phase="任务开发",
                status_path=review_json,
                validator=validator,
                kind="review_round",
                tracked_artifacts={"review_json": review_json, "review_md": review_md},
            )

            def first_response(*, result_contract=None, completion_contract=None):  # noqa: ANN001, ARG001
                review_json.write_text(json.dumps([{"task_name": "M1-T1", "review_pass": False}], ensure_ascii=False), encoding="utf-8")
                review_md.write_text("", encoding="utf-8")
                return SimpleNamespace(
                    ok=False,
                    clean_output=f"{TURN_ARTIFACT_CONTRACT_ERROR_PREFIX}: review_md missing",
                )

            def second_response(*, result_contract=None, completion_contract=None):  # noqa: ANN001, ARG001
                review_md.write_text("需要补充测试覆盖\n", encoding="utf-8")
                return SimpleNamespace(ok=True, clean_output="")

            worker = _FakeTaskWorker([first_response, second_response])
            run_completion_turn_with_repair(
                worker=worker,
                label="reviewer_turn",
                prompt="评审 prompt",
                completion_contract=contract,
                turn_goal=CompletionTurnGoal(
                    goal_id="reviewer_round",
                    outcomes={
                        "review_pass": OutcomeGoal(status="review_pass", required_aliases=("review_json",), forbidden_aliases=("review_md",)),
                        "review_fail": OutcomeGoal(status="review_fail", required_aliases=("review_json", "review_md")),
                    },
                ),
                stage_label="任务开发",
                role_label="架构师",
            )
            observation = observe_completion_state(contract)
            self.assertEqual(observation.observed_status, "review_fail")
            self.assertEqual(len(worker.prompts), 2)
            self.assertIn("评审输出未通过协议校验", worker.prompts[1])
            self.assertIn(str(review_md), worker.prompts[1])


if __name__ == "__main__":
    unittest.main()
