from __future__ import annotations

import unittest

import T02_tmux_agents
import T05_hitl_runtime
from canopy_core.runtime import contracts, hitl, tmux_runtime


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
