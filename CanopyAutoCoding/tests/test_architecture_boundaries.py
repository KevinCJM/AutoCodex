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
        import A00_main_tui
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

        self.assertIs(A00_main_tui, entry)
        self.assertIs(A04_RequirementsReview, requirements_review)
        self.assertIs(A05_DetailedDesign, detailed_design)
        self.assertIs(A08_OverallReview, overall_review)
        self.assertIs(T02_tmux_agents, tmux_runtime)
        self.assertIs(T05_hitl_runtime, hitl)
        self.assertIs(T11_tui_backend, backend)

    def test_prompt_contract_modules_do_not_import_runtime_or_stage_kernel(self):
        for relative_path in (
            "canopy_core/prompt_contracts/spec.py",
            "canopy_core/prompt_contracts/common.py",
            "canopy_core/prompt_contracts/requirements_clarification.py",
            "canopy_core/prompt_contracts/requirements_review.py",
            "canopy_core/prompt_contracts/detailed_design.py",
            "canopy_core/prompt_contracts/task_split.py",
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
        self.assertNotIn("A00_main_tui", bridge_imports)
        self.assertNotIn("A04_RequirementsReview", bridge_imports)
        self.assertNotIn("A05_DetailedDesign", bridge_imports)
        self.assertNotIn("A08_OverallReview", bridge_imports)
        self.assertNotIn("T02_tmux_agents", bridge_imports)

        self.assertIn("canopy_core.runtime.contracts", hitl_imports)
        self.assertIn("canopy_core.runtime.tmux_runtime", hitl_imports)
        self.assertNotIn("T02_tmux_agents", hitl_imports)
        contracts_imports = _collect_imports("canopy_core/runtime/contracts.py")
        self.assertNotIn("T04_common_prompt", contracts_imports)

    def test_agent_runtime_state_has_single_source_of_truth(self):
        runtime_source = (PROJECT_ROOT / "canopy_core/runtime/tmux_runtime.py").read_text(encoding="utf-8")
        self.assertNotIn("ProviderPhase", runtime_source)
        self.assertNotIn("provider_phase", runtime_source)
        self.assertNotIn("classify_phase", runtime_source)
        self.assertNotIn("_debounce_provider_phase", runtime_source)

        tui_root = PROJECT_ROOT / "packages/tui/src"
        for path in tui_root.rglob("*"):
            if not path.is_file() or path.suffix not in {".ts", ".tsx"}:
                continue
            source = path.read_text(encoding="utf-8")
            self.assertNotIn("provider_phase", source, str(path))
            self.assertNotIn("providerPhase", source, str(path))
            self.assertNotIn("waiting_input", source, str(path))
            self.assertNotIn("idle_ready", source, str(path))
            self.assertNotIn("completed_response", source, str(path))

        bridge_source = (PROJECT_ROOT / "canopy_core/bridge/backend.py").read_text(encoding="utf-8")
        normalize_body = bridge_source.split("def _normalize_worker_agent_state", 1)[1].split(
            "def _normalize_worker_session_state", 1
        )[0]
        self.assertNotIn("current_command", normalize_body)
        self.assertNotIn("agent_alive", normalize_body)
        self.assertNotIn('return "BUSY"', normalize_body)
        self.assertNotIn('return "STARTING"', normalize_body)

        stage_source = (PROJECT_ROOT / "canopy_core/stage_kernel/role_orchestration.py").read_text(encoding="utf-8")
        self.assertNotIn('return "READY"', stage_source)
        self.assertNotIn('or "READY"', stage_source)

        runtime_source_without_classifier = runtime_source.replace(
            "return detector.classify_agent_state(observation)",
            "",
        ).replace(
            "detected_state = detector.classify_agent_state(observation)",
            "",
        )
        self.assertNotIn("self.detector.classify_agent_state", runtime_source_without_classifier)

        in_legacy_map = False
        for line_number, line in enumerate(bridge_source.splitlines(), start=1):
            if "awaiting_input" in line:
                continue
            if line.startswith("_LEGACY_PROVIDER_PHASE_AGENT_STATE = {"):
                in_legacy_map = True
            if in_legacy_map and line.startswith("}"):
                in_legacy_map = False
                continue
            if any(
                token in line
                for token in (
                    "provider_phase",
                    "waiting_input",
                    "idle_ready",
                    "completed_response",
                    "processing",
                    "auth_prompt",
                    "update_prompt",
                    "recovering",
                )
            ):
                self.assertTrue(
                    in_legacy_map
                    or "_legacy_agent_state_from_provider_phase" in line
                    or 'snapshot.get("provider_phase"' in line
                    or "legacy_candidate" in line,
                    f"backend.py:{line_number}: {line}",
                )
