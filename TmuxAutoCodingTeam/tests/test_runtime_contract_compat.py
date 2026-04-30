from __future__ import annotations

import unittest

import T02_tmux_agents
import T05_hitl_runtime
from tmux_core.runtime import contracts, hitl, tmux_runtime


class RuntimeContractCompatTests(unittest.TestCase):
    def test_runtime_contract_module_exports_tmux_contract_types(self):
        self.assertIs(contracts.TaskResultContract, tmux_runtime.TaskResultContract)
        self.assertIs(contracts.TurnFileContract, tmux_runtime.TurnFileContract)
        self.assertIs(contracts.TurnFileResult, tmux_runtime.TurnFileResult)

    def test_top_level_runtime_modules_alias_internal_runtime_modules(self):
        self.assertIs(T02_tmux_agents, tmux_runtime)
        self.assertIs(T05_hitl_runtime, hitl)

    def test_runtime_finalize_contracts_live_in_contracts_module(self):
        self.assertTrue(hasattr(contracts, "TaskResultDecision"))
        self.assertTrue(hasattr(contracts, "finalize_task_result"))
        self.assertTrue(hasattr(contracts, "materialize_task_result"))
        self.assertIs(hitl.TurnFileContract, contracts.TurnFileContract)
        self.assertIs(hitl.TurnFileResult, contracts.TurnFileResult)

    def test_a06_reviewer_init_resolves_ready_independently_from_ba_init(self):
        contract = contracts.TaskResultContract(
            turn_id="a06_reviewer_init",
            phase="a06_reviewer_init",
            task_kind="a06_reviewer_init",
            mode="a06_reviewer_init",
            expected_statuses=(contracts.TASK_RESULT_READY,),
            stage_name="任务拆分阶段",
        )

        decision = contracts.resolve_task_result_decision(contract)

        self.assertEqual(decision.status, contracts.TASK_RESULT_READY)
        self.assertIn("审核智能体", decision.summary)
