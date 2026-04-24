from __future__ import annotations

import ast
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _collect_imports(relative_path: str) -> set[str]:
    source = (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")
    tree = ast.parse(source, filename=relative_path)
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
    return imports


class ArchitectureBoundaryTests(unittest.TestCase):
    def test_top_level_entrypoints_are_real_module_aliases(self):
        import A00_main
        import A04_RequirementsReview
        import A05_DetailedDesign
        import A08_OverallReview
        import T02_tmux_agents
        import T05_hitl_runtime
        import T11_tui_backend
        from canopy_core.bridge import backend
        from canopy_core.runtime import contracts, hitl, tmux_runtime
        from canopy_core.stage_kernel import detailed_design, overall_review, requirements_review
        from canopy_core.workflow import entry

        self.assertIs(A00_main, entry)
        self.assertIs(A04_RequirementsReview, requirements_review)
        self.assertIs(A05_DetailedDesign, detailed_design)
        self.assertIs(A08_OverallReview, overall_review)
        self.assertIs(T02_tmux_agents, tmux_runtime)
        self.assertIs(T05_hitl_runtime, hitl)
        self.assertIs(T11_tui_backend, backend)

    def test_prompt_contract_modules_do_not_import_runtime_or_stage_kernel(self):
        for relative_path in (
            "canopy_core/prompt_contracts/common.py",
            "canopy_core/prompt_contracts/requirements_clarification.py",
            "canopy_core/prompt_contracts/requirements_review.py",
            "canopy_core/prompt_contracts/detailed_design.py",
            "canopy_core/prompt_contracts/overall_review.py",
        ):
            imports = _collect_imports(relative_path)
            self.assertFalse(
                any(name.startswith("canopy_core.runtime") for name in imports),
                relative_path,
            )
            self.assertFalse(
                any(name.startswith("canopy_core.stage_kernel") for name in imports),
                relative_path,
            )

    def test_internal_workflow_bridge_and_runtime_use_package_modules(self):
        workflow_imports = _collect_imports("canopy_core/workflow/entry.py")
        bridge_imports = _collect_imports("canopy_core/bridge/backend.py")
        hitl_imports = _collect_imports("canopy_core/runtime/hitl.py")
        self.assertIn("canopy_core.stage_kernel.requirements_review", workflow_imports)
        self.assertIn("canopy_core.stage_kernel.detailed_design", workflow_imports)
        self.assertIn("canopy_core.stage_kernel.overall_review", workflow_imports)
        self.assertIn("canopy_core.runtime.tmux_runtime", workflow_imports)
        self.assertNotIn("A04_RequirementsReview", workflow_imports)
        self.assertNotIn("A05_DetailedDesign", workflow_imports)
        self.assertNotIn("A08_OverallReview", workflow_imports)
        self.assertNotIn("T02_tmux_agents", workflow_imports)

        self.assertIn("canopy_core.workflow.entry", bridge_imports)
        self.assertIn("canopy_core.stage_kernel.requirements_review", bridge_imports)
        self.assertIn("canopy_core.stage_kernel.detailed_design", bridge_imports)
        self.assertIn("canopy_core.stage_kernel.overall_review", bridge_imports)
        self.assertIn("canopy_core.runtime.tmux_runtime", bridge_imports)
        self.assertNotIn("A00_main", bridge_imports)
        self.assertNotIn("A04_RequirementsReview", bridge_imports)
        self.assertNotIn("A05_DetailedDesign", bridge_imports)
        self.assertNotIn("A08_OverallReview", bridge_imports)
        self.assertNotIn("T02_tmux_agents", bridge_imports)

        self.assertIn("canopy_core.runtime.contracts", hitl_imports)
        self.assertIn("canopy_core.runtime.tmux_runtime", hitl_imports)
        self.assertNotIn("T02_tmux_agents", hitl_imports)
        contracts_imports = _collect_imports("canopy_core/runtime/contracts.py")
        self.assertNotIn("T04_common_prompt", contracts_imports)
