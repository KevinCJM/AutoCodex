from __future__ import annotations

import ast
import importlib
import inspect
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tmux_core.prompt_contracts.spec import (
    CHANGE_MUST_CHANGE,
    CHANGE_MUST_EXIST_NONEMPTY,
    CHANGE_MUST_NOT_CHANGE,
    get_prompt_spec,
    is_prompt_helper,
)
from tmux_core.runtime.contracts import (
    TaskResultContract,
    TurnFileResult,
    collect_contract_artifacts,
    resolve_task_result_decision,
    snapshot_file_fingerprint,
)
from tmux_core.stage_kernel.prompt_turns import (
    build_prompt_task_turn,
    run_prompt_completion_turn,
    run_prompt_turn,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROMPT_MODULES = (
    "Prompt_01_RoutingLayerPlanning",
    "Prompt_02_RequirementIntake",
    "Prompt_03_RequirementsClarification",
    "Prompt_04_RequirementsReview",
    "Prompt_05_DetailedDesign",
    "Prompt_06_TaskSplit",
    "Prompt_07_Development",
    "Prompt_08_OverallReview",
)


class PromptContractSpecTests(unittest.TestCase):
    def test_every_prompt_function_has_turn_metadata_or_helper_marker(self):
        missing: list[str] = []
        for module_name in PROMPT_MODULES:
            module = importlib.import_module(module_name)
            for name, fn in inspect.getmembers(module, inspect.isfunction):
                if name.startswith("_") or fn.__module__ != module.__name__:
                    continue
                if get_prompt_spec(fn) is None and not is_prompt_helper(fn):
                    missing.append(f"{module_name}.{name}")
        self.assertEqual(missing, [])

    def test_prompt_files_only_import_lightweight_prompt_contract_spec(self):
        violations: list[str] = []
        for module_name in PROMPT_MODULES:
            path = PROJECT_ROOT / f"{module_name}.py"
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                imported = ""
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        imported = alias.name
                        if imported.startswith("tmux_core.") and imported != "tmux_core.prompt_contracts.spec":
                            violations.append(f"{module_name}:{imported}")
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported = node.module
                    if imported.startswith("tmux_core.") and imported != "tmux_core.prompt_contracts.spec":
                        violations.append(f"{module_name}:{imported}")
        self.assertEqual(violations, [])

    def test_prompt_adapter_generates_appendix_and_must_change_baseline(self):
        from Prompt_07_Development import start_develop
        from tmux_core.stage_kernel.development import build_developer_task_complete_result_contract

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            paths = {
                "hitl_record_md": root / "hitl.md",
                "requirements_clear_md": root / "requirements.md",
                "detailed_design_md": root / "design.md",
                "task_split_md": root / "tasks.md",
                "what_just_dev": root / "developer.md",
            }
            for path in paths.values():
                path.write_text("old\n", encoding="utf-8")
            built = build_prompt_task_turn(
                start_develop,
                "M1-T1",
                **{key: str(value) for key, value in paths.items()},
            )
            self.assertIn("## 本轮文件契约", built.prompt)
            self.assertIsNotNone(built.task_result_contract)
            contract = built.task_result_contract
            assert contract is not None
            self.assertEqual(contract.mode, "a07_developer_task_complete")
            self.assertIn("developer_output", contract.required_artifacts)
            legacy_contract = build_developer_task_complete_result_contract(
                {
                    "developer_output_path": paths["what_just_dev"],
                    "task_md_path": paths["task_split_md"],
                    "task_json_path": root / "tasks.json",
                }
            )
            self.assertEqual(contract.required_artifacts, legacy_contract.required_artifacts)
            self.assertEqual(contract.expected_statuses, legacy_contract.expected_statuses)
            rule = contract.artifact_rules["developer_output"]
            self.assertIsInstance(rule, dict)
            self.assertEqual(rule["change"], CHANGE_MUST_CHANGE)
            self.assertEqual(rule["baseline"], snapshot_file_fingerprint(paths["what_just_dev"]))

    def test_a07_ready_hitl_prompt_metadata_matches_legacy_init_contract_outcomes(self):
        from Prompt_07_Development import human_reply, init_developer
        from tmux_core.stage_kernel.development import build_developer_init_result_contract

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            prompt_paths = {
                "ask_human_md": root / "ask.md",
                "hitl_record_md": root / "hitl.md",
                "requirements_clear_md": root / "requirements.md",
                "detailed_design_md": root / "design.md",
                "task_split_md": root / "tasks.md",
            }
            for path in prompt_paths.values():
                path.write_text("", encoding="utf-8")
            legacy_paths = {
                "ask_human_path": prompt_paths["ask_human_md"],
                "hitl_record_path": prompt_paths["hitl_record_md"],
                "requirements_clear_path": prompt_paths["requirements_clear_md"],
                "detailed_design_path": prompt_paths["detailed_design_md"],
                "task_md_path": prompt_paths["task_split_md"],
                "task_json_path": root / "tasks.json",
            }

            checks = (
                (
                    build_prompt_task_turn(
                        init_developer,
                        "开发工程师",
                        **{key: str(value) for key, value in prompt_paths.items()},
                    ).task_result_contract,
                    build_developer_init_result_contract(legacy_paths, mode="a07_developer_init"),
                ),
                (
                    build_prompt_task_turn(
                        human_reply,
                        "人类反馈",
                        **{key: str(value) for key, value in prompt_paths.items()},
                    ).task_result_contract,
                    build_developer_init_result_contract(legacy_paths, mode="a07_developer_human_reply"),
                ),
            )

            for prompt_contract, legacy_contract in checks:
                assert prompt_contract is not None
                self.assertEqual(prompt_contract.expected_statuses, legacy_contract.expected_statuses)
                self.assertEqual(
                    prompt_contract.outcome_artifacts["ready"]["forbids"],
                    legacy_contract.outcome_artifacts["ready"]["forbids"],
                )
                self.assertEqual(
                    prompt_contract.outcome_artifacts["hitl"]["requires"],
                    legacy_contract.outcome_artifacts["hitl"]["requires"],
                )

    def test_must_change_fails_when_output_hash_is_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            output = Path(tmp_dir) / "out.md"
            output.write_text("same\n", encoding="utf-8")
            contract = TaskResultContract(
                turn_id="turn",
                phase="phase",
                task_kind="kind",
                mode="mode",
                expected_statuses=("completed",),
                required_artifacts={"out": output},
                artifact_rules={
                    "out": {
                        "change": CHANGE_MUST_CHANGE,
                        "baseline": snapshot_file_fingerprint(output),
                    }
                },
            )
            with self.assertRaisesRegex(ValueError, "未变化"):
                collect_contract_artifacts(contract)
            output.write_text("changed\n", encoding="utf-8")
            artifacts, _ = collect_contract_artifacts(contract)
            self.assertEqual(artifacts["out"], str(output.resolve()))

    def test_must_exist_nonempty_allows_unchanged_existing_file(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            output = Path(tmp_dir) / "out.md"
            output.write_text("same\n", encoding="utf-8")
            contract = TaskResultContract(
                turn_id="turn",
                phase="phase",
                task_kind="kind",
                mode="mode",
                expected_statuses=("completed",),
                required_artifacts={"out": output},
                artifact_rules={
                    "out": {
                        "change": CHANGE_MUST_EXIST_NONEMPTY,
                        "baseline": snapshot_file_fingerprint(output),
                    }
                },
            )
            artifacts, _ = collect_contract_artifacts(contract)
            self.assertEqual(artifacts["out"], str(output.resolve()))

    def test_must_not_change_fails_when_protected_file_changes(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            output = Path(tmp_dir) / "out.md"
            output.write_text("same\n", encoding="utf-8")
            contract = TaskResultContract(
                turn_id="turn",
                phase="phase",
                task_kind="kind",
                mode="mode",
                expected_statuses=("completed",),
                required_artifacts={"out": output},
                artifact_rules={
                    "out": {
                        "change": CHANGE_MUST_NOT_CHANGE,
                        "baseline": snapshot_file_fingerprint(output),
                    }
                },
            )
            collect_contract_artifacts(contract)
            output.write_text("changed\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "不应变化"):
                collect_contract_artifacts(contract)

    def test_generic_single_outcome_contract_resolves_without_mode_specific_branch(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            output = Path(tmp_dir) / "routing.json"
            output.write_text("{}\n", encoding="utf-8")
            contract = TaskResultContract(
                turn_id="a01_routing_create",
                phase="a01_routing_create",
                task_kind="a01_routing_create",
                mode="a01_routing_create",
                expected_statuses=("completed",),
                required_artifacts={"routing_doc": output},
                artifact_rules={
                    "routing_doc": {
                        "change": CHANGE_MUST_EXIST_NONEMPTY,
                        "baseline": {"exists": False, "path": str(output.resolve())},
                    }
                },
            )
            decision = resolve_task_result_decision(contract)
            self.assertEqual(decision.status, "completed")
            self.assertIn("routing_doc", decision.artifacts)

    def test_outcome_scoped_must_change_ignores_unselected_branch_outputs(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            detailed_design = root / "design.md"
            ask_human = root / "ask.md"
            detailed_design.write_text("old design\n", encoding="utf-8")
            detailed_baseline = snapshot_file_fingerprint(detailed_design)
            ask_baseline = snapshot_file_fingerprint(ask_human)
            ask_human.write_text("need human\n", encoding="utf-8")
            contract = TaskResultContract(
                turn_id="turn",
                phase="phase",
                task_kind="kind",
                mode="multi_branch",
                expected_statuses=("completed", "hitl"),
                optional_artifacts={"detailed_design": detailed_design, "ask_human": ask_human},
                artifact_rules={
                    "detailed_design": {"change": CHANGE_MUST_CHANGE, "baseline": detailed_baseline},
                    "ask_human": {"change": CHANGE_MUST_CHANGE, "baseline": ask_baseline},
                },
                outcome_artifacts={
                    "completed": {"requires": ("detailed_design",), "optional": (), "forbids": ()},
                    "hitl": {"requires": ("ask_human",), "optional": (), "forbids": ()},
                },
            )
            decision = resolve_task_result_decision(contract)
            self.assertEqual(decision.status, "hitl")
            self.assertNotIn("detailed_design", decision.artifacts)

    def test_outcome_scoped_must_change_fails_for_selected_branch_output(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            output = Path(tmp_dir) / "design.md"
            output.write_text("old design\n", encoding="utf-8")
            contract = TaskResultContract(
                turn_id="turn",
                phase="phase",
                task_kind="kind",
                mode="multi_branch",
                expected_statuses=("completed",),
                optional_artifacts={"detailed_design": output},
                artifact_rules={
                    "detailed_design": {
                        "change": CHANGE_MUST_CHANGE,
                        "baseline": snapshot_file_fingerprint(output),
                    }
                },
                outcome_artifacts={
                    "completed": {"requires": ("detailed_design",), "optional": (), "forbids": ()},
                },
            )
            with self.assertRaisesRegex(ValueError, "未变化"):
                resolve_task_result_decision(contract)

    def test_a01_active_wrappers_require_routing_files(self):
        from Prompt_01_RoutingLayerPlanning import build_create_prompt, build_refine_prompt

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            paths = {
                "agents_md": root / "AGENTS.md",
                "repo_map_json": root / "docs" / "repo_map.json",
                "task_routes_json": root / "docs" / "task_routes.json",
                "pitfalls_json": root / "docs" / "pitfalls.json",
            }
            audit_record = root / "audit.md"
            audit_status = root / "audit_status.json"
            audit_record.write_text("fix routing\n", encoding="utf-8")
            audit_status.write_text("{}\n", encoding="utf-8")

            create_turn = build_prompt_task_turn(
                build_create_prompt,
                **{key: str(value) for key, value in paths.items()},
            )
            refine_turn = build_prompt_task_turn(
                build_refine_prompt,
                str(audit_record),
                routing_audit_status_file=str(audit_status),
                **{key: str(value) for key, value in paths.items()},
            )
            for built in (create_turn, refine_turn):
                contract = built.task_result_contract
                assert contract is not None
                self.assertEqual(
                    set(contract.required_artifacts),
                    {"agents_md", "repo_map_json", "task_routes_json", "pitfalls_json"},
                )
                for alias in contract.required_artifacts:
                    self.assertEqual(contract.artifact_rules[alias]["change"], CHANGE_MUST_CHANGE)

    def test_a02_notion_ask_human_only_resolves_hitl(self):
        from Prompt_02_RequirementIntake import get_notion_requirement

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            original = root / "original.md"
            ask_human = root / "ask.md"
            built = build_prompt_task_turn(
                get_notion_requirement,
                "https://notion.example/page",
                original_requirement_md=str(original),
                ask_human_md=str(ask_human),
            )
            ask_human.write_text("Notion token failed\n", encoding="utf-8")
            contract = built.task_result_contract
            assert contract is not None
            decision = resolve_task_result_decision(contract)
            self.assertEqual(decision.status, "hitl")
            self.assertIn("ask_human", decision.artifacts)
            self.assertNotIn("original_requirement", decision.artifacts)

    def test_required_outcome_files_have_strong_change_policy(self):
        weak_required: list[str] = []
        strong_changes = {"must_change", "must_exist_nonempty"}
        for module_name in PROMPT_MODULES:
            module = importlib.import_module(module_name)
            for name, fn in inspect.getmembers(module, inspect.isfunction):
                if fn.__module__ != module.__name__:
                    continue
                spec = get_prompt_spec(fn)
                if spec is None:
                    continue
                for outcome_name, outcome in spec.outcomes.items():
                    if outcome.status == "ready":
                        continue
                    for alias in outcome.requires:
                        change = spec.files[alias].change
                        if change not in strong_changes:
                            weak_required.append(f"{module_name}.{name}.{outcome_name}.{alias}:{change}")
        self.assertEqual(weak_required, [])

    def test_reviewer_prompt_requires_completion_adapter_and_validator(self):
        from Prompt_07_Development import reviewer_review_code

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            task_md = root / "task.md"
            design_md = root / "design.md"
            review_md = root / "review.md"
            review_json = root / "review.json"
            task_md.write_text("task\n", encoding="utf-8")
            design_md.write_text("design\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "reviewer prompt"):
                run_prompt_turn(
                    worker=SimpleNamespace(),
                    prompt_fn=reviewer_review_code,
                    args=("M1-T1", "changed"),
                    kwargs={
                        "task_split_md": str(task_md),
                        "detailed_design_md": str(design_md),
                        "review_md": str(review_md),
                        "review_json": str(review_json),
                    },
                )

            class FakeWorker:
                def run_turn(self, *, label, prompt, completion_contract, timeout_sec):
                    review_json.write_text(
                        json.dumps([{"task_name": "M1-T1", "review_pass": True}], ensure_ascii=False),
                        encoding="utf-8",
                    )
                    review_md.write_text("", encoding="utf-8")
                    return SimpleNamespace(ok=True, clean_output="")

            def validator(path: Path) -> TurnFileResult:
                payload = json.loads(path.read_text(encoding="utf-8"))
                item = payload[0]
                review_pass = item.get("review_pass")
                if not isinstance(review_pass, bool):
                    raise ValueError("review_pass must be bool")
                review_text = review_md.read_text(encoding="utf-8").strip()
                if review_pass and review_text:
                    raise ValueError("review_md must be empty on pass")
                if not review_pass and not review_text:
                    raise ValueError("review_md must be non-empty on fail")
                return TurnFileResult(
                    status_path=str(path.resolve()),
                    payload={"review_pass": review_pass},
                    artifact_paths={"review_json": str(review_json.resolve()), "review_md": str(review_md.resolve())},
                    artifact_hashes={},
                    validated_at="now",
                )

            run_prompt_completion_turn(
                worker=FakeWorker(),
                prompt_fn=reviewer_review_code,
                status_path=review_json,
                validator=validator,
                args=("M1-T1", "changed"),
                kwargs={
                    "task_split_md": str(task_md),
                    "detailed_design_md": str(design_md),
                    "review_md": str(review_md),
                    "review_json": str(review_json),
                },
                label="review",
            )

            class FailWorker:
                def run_turn(self, *, label, prompt, completion_contract, timeout_sec):
                    review_json.write_text(
                        json.dumps([{"task_name": "M1-T1", "review_pass": False}], ensure_ascii=False),
                        encoding="utf-8",
                    )
                    review_md.write_text("bug\n", encoding="utf-8")
                    return SimpleNamespace(ok=True, clean_output="")

            run_prompt_completion_turn(
                worker=FailWorker(),
                prompt_fn=reviewer_review_code,
                status_path=review_json,
                validator=validator,
                args=("M1-T1", "changed"),
                kwargs={
                    "task_split_md": str(task_md),
                    "detailed_design_md": str(design_md),
                    "review_md": str(review_md),
                    "review_json": str(review_json),
                },
                label="review_fail",
            )

            class StalePassWorker:
                def run_turn(self, *, label, prompt, completion_contract, timeout_sec):
                    review_json.write_text(
                        json.dumps([{"task_name": "M1-T1", "review_pass": True}], ensure_ascii=False),
                        encoding="utf-8",
                    )
                    review_md.write_text("stale bug\n", encoding="utf-8")
                    return SimpleNamespace(ok=True, clean_output="")

            with patch(
                "tmux_core.stage_kernel.turn_output_goals.request_file_noncompliance_intervention",
                side_effect=RuntimeError("review_md must be empty on pass"),
            ), self.assertRaisesRegex(RuntimeError, "review_md|forbidden"):
                run_prompt_completion_turn(
                    worker=StalePassWorker(),
                    prompt_fn=reviewer_review_code,
                    status_path=review_json,
                    validator=validator,
                    args=("M1-T1", "changed"),
                    kwargs={
                        "task_split_md": str(task_md),
                        "detailed_design_md": str(design_md),
                        "review_md": str(review_md),
                        "review_json": str(review_json),
                    },
                    label="review_stale_pass",
                )

            review_md.write_text("old bug\n", encoding="utf-8")

            class StaleFailWorker:
                def run_turn(self, *, label, prompt, completion_contract, timeout_sec):
                    review_json.write_text(
                        json.dumps([{"task_name": "M1-T1", "review_pass": False}], ensure_ascii=False),
                        encoding="utf-8",
                    )
                    review_md.write_text("old bug\n", encoding="utf-8")
                    return SimpleNamespace(ok=True, clean_output="")

            with patch(
                "tmux_core.stage_kernel.turn_output_goals.request_file_noncompliance_intervention",
                side_effect=RuntimeError("artifact 未变化: review_md"),
            ), self.assertRaisesRegex(RuntimeError, "未变化"):
                run_prompt_completion_turn(
                    worker=StaleFailWorker(),
                    prompt_fn=reviewer_review_code,
                    status_path=review_json,
                    validator=validator,
                    args=("M1-T1", "changed"),
                    kwargs={
                        "task_split_md": str(task_md),
                        "detailed_design_md": str(design_md),
                        "review_md": str(review_md),
                        "review_json": str(review_json),
                    },
                    label="review_stale_fail",
                )


if __name__ == "__main__":
    unittest.main()
