from __future__ import annotations

import json
from pathlib import Path

from tmux_core.runtime.graphify import GraphifyQueryIntent
from tmux_core.stage_kernel.graphify_route_context import (
    build_stage_graphify_turn_context,
    resolve_stage_graphify_route_hints,
)


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _build_routed_project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    (project / "docs").mkdir(parents=True)
    (project / "src").mkdir()
    (project / "tests").mkdir()
    (project / "config").mkdir()
    (project / "AGENTS.md").write_text("# Required Read Order\n", encoding="utf-8")
    (project / "src" / "main.py").write_text("class BillingService: pass\n", encoding="utf-8")
    (project / "src" / "extra.py").write_text("def helper(): pass\n", encoding="utf-8")
    (project / "tests" / "test_main.py").write_text("def test_main(): pass\n", encoding="utf-8")
    (project / "config" / "runtime.py").write_text("ENABLED = True\n", encoding="utf-8")
    (project / "docs" / "spec.md").write_text("not source\n", encoding="utf-8")
    _write_json(project / "docs" / "ai_routing_evolution_policy.json", {})
    _write_json(
        project / "docs" / "repo_map.json",
        {
            "modules": [
                {
                    "id": "M_MAIN",
                    "first_read_selectors": [
                        {"type": "literal", "path": "src/main.py"},
                        {"type": "literal", "path": "docs/spec.md"},
                        {"type": "subtree", "path": "src"},
                    ],
                    "then_check_selectors": [
                        {"type": "token", "token": "direct_callers"},
                    ],
                    "related_test_selectors": [
                        {"type": "literal", "path": "tests/test_main.py"},
                    ],
                    "related_config_selectors": [
                        {"type": "literal", "path": "config/runtime.py"},
                    ],
                    "minimum_regression_selectors": [],
                    "key_symbols": ["BillingService", "BillingService.handle", "src/main.py"],
                    "pitfall_ids": [],
                    "grounding": {"fact_status": "grounded"},
                },
                {
                    "id": "M_LOW",
                    "first_read_selectors": [{"type": "literal", "path": "src/extra.py"}],
                    "then_check_selectors": [],
                    "related_test_selectors": [],
                    "related_config_selectors": [],
                    "minimum_regression_selectors": [],
                    "key_symbols": ["LowPriority"],
                    "pitfall_ids": [],
                    "grounding": {"fact_status": "grounded"},
                },
            ]
        },
    )
    _write_json(project / "docs" / "pitfalls.json", {"pitfalls": []})
    _write_json(
        project / "docs" / "task_routes.json",
        {
            "allowed_unresolved_sentinels": ["needs_code_confirmation", "unknown"],
            "resolution_model": {
                "operations": [
                    {
                        "op": "collect_module_defaults",
                        "module_fields": [
                            "first_read_selectors",
                            "then_check_selectors",
                            "related_test_selectors",
                            "related_config_selectors",
                            "minimum_regression_selectors",
                        ],
                    },
                    {"op": "apply_route_deltas", "merge": "append"},
                    {"op": "apply_route_overrides", "merge": "replace_named_output_field"},
                ]
            },
            "selector_contract": {
                "token_registry": {
                    "direct_callers": {"fallback": "needs_code_confirmation"},
                }
            },
            "routes": [
                {
                    "id": "R_LOW",
                    "match_keywords": ["billing"],
                    "negative_keywords": [],
                    "route_priority": 10,
                    "first_read_modules": ["M_LOW"],
                    "selector_deltas": {},
                    "selector_overrides": {},
                },
                {
                    "id": "R_BLOCKED",
                    "match_keywords": ["billing"],
                    "negative_keywords": ["architecture"],
                    "route_priority": 100,
                    "first_read_modules": ["M_LOW"],
                    "selector_deltas": {},
                    "selector_overrides": {},
                },
                {
                    "id": "R_MAIN",
                    "match_keywords": ["architecture", "billing"],
                    "negative_keywords": ["legacy-only"],
                    "route_priority": 50,
                    "first_read_modules": ["M_MAIN"],
                    "selector_deltas": {},
                    "selector_overrides": {},
                },
            ],
        },
    )
    return project


def test_stage_route_hints_reuse_materialized_selectors_and_filter_to_safe_source_paths(
    tmp_path: Path,
) -> None:
    project = _build_routed_project(tmp_path)
    artifact = project / "Requirement.md"
    artifact.write_text(
        "Billing architecture uses `BillingService.handle` and `src/extra.py`. "
        "Reject `../outside.py` and `docs/spec.md`.",
        encoding="utf-8",
    )

    hints = resolve_stage_graphify_route_hints(
        project,
        task_text="billing architecture review",
        business_artifact_paths=(artifact,),
    )

    assert hints.route_id == "R_MAIN"
    assert hints.routed_paths == (
        "src/main.py",
        "tests/test_main.py",
        "config/runtime.py",
        "src/extra.py",
    )
    assert "needs_code_confirmation" not in hints.routed_paths
    assert "docs/spec.md" not in hints.routed_paths
    assert hints.symbols[0] == "BillingService.handle"
    assert "BillingService" in hints.symbols
    assert "BillingService.handle" in hints.symbols
    assert "src/main.py" not in hints.symbols


def test_stage_route_hints_fail_soft_when_routing_is_missing_or_invalid(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    missing = resolve_stage_graphify_route_hints(project, task_text="billing")
    assert missing.route_id == ""
    assert missing.routed_paths == ()
    assert missing.symbols == ()

    (project / "docs").mkdir()
    (project / "docs" / "task_routes.json").write_text("{broken", encoding="utf-8")
    assert resolve_stage_graphify_route_hints(project, task_text="billing").route_id == ""


def test_build_stage_graphify_turn_context_propagates_routing_hints(tmp_path: Path) -> None:
    project = _build_routed_project(tmp_path)
    artifact = project / "Requirement.md"
    artifact.write_text("billing architecture for `BillingService.handle`", encoding="utf-8")

    context = build_stage_graphify_turn_context(
        project,
        stage_key="A05",
        phase="a05_design",
        role="design_analyst",
        intent=GraphifyQueryIntent.ARCHITECTURE_BOUNDARY,
        requirement_name="billing",
        task_name="architecture",
        query_seeds=("shared dependencies",),
        business_artifact_paths=(artifact,),
    )

    assert context.stage_key == "A05"
    assert context.intent is GraphifyQueryIntent.ARCHITECTURE_BOUNDARY
    assert context.routed_paths[:3] == (
        "src/main.py",
        "tests/test_main.py",
        "config/runtime.py",
    )
    assert "BillingService.handle" in context.symbols
