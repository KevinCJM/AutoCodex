from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest


ROOT = Path(__file__).resolve().parents[1]
ROUTE_TASK_PATHS = (
    ROOT / "skills/ai-hermes-self-evolve/scripts/route_task.py",
    ROOT / "skills/ai-hermes-routing-init/scripts/route_task.py",
)


def _load_route_task(path: Path, index: int) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"route_task_{index}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(params=enumerate(ROUTE_TASK_PATHS), ids=("self-evolve", "routing-init"))
def route_task(request: pytest.FixtureRequest) -> ModuleType:
    index, path = request.param
    return _load_route_task(path, index)


def _write_routing_project(
    root: Path,
    *,
    repo_map: dict[str, object],
    task_routes: dict[str, object],
) -> None:
    docs = root / "docs"
    docs.mkdir()
    (root / "AGENTS.md").write_text("# Required Read Order\n", encoding="utf-8")
    (docs / "repo_map.json").write_text(json.dumps(repo_map), encoding="utf-8")
    (docs / "task_routes.json").write_text(json.dumps(task_routes), encoding="utf-8")
    (docs / "pitfalls.json").write_text(json.dumps({"pitfalls": []}), encoding="utf-8")
    (docs / "ai_routing_evolution_policy.json").write_text("{}", encoding="utf-8")


def test_r12_materializes_current_project_selectors(route_task: ModuleType) -> None:
    resolved = route_task.resolve_route(
        project_root=ROOT,
        route_id="R12_graphify_code_graph",
    )

    assert resolved["status"] == "ok"
    assert "tmux_core/runtime/graphify.py" in resolved["files"]["first_read"]
    assert "tests/test_graphify_runtime.py" in resolved["minimum_regression"]
    assert resolved["routing_policy"]["merge_strategy"] == "append"
    assert resolved["actions"]["expand_search"][0]["action"] != "unknown"


def test_current_schema_expands_refs_deltas_overrides_and_tokens(
    route_task: ModuleType,
    tmp_path: Path,
) -> None:
    repo_map = {
        "modules": [
            {
                "id": "M1",
                "owned_paths": [{"role": "source", "path": "src"}],
                "entry_selectors": [{"type": "literal", "path": "src/main.py"}],
                "first_read_selectors": [
                    {"type": "field_ref", "field": "entry_selectors"},
                    {"type": "owned_paths_role", "role": "source"},
                ],
                "then_check_selectors": [{"type": "subtree", "path": "src/lib"}],
                "related_test_selectors": [{"type": "token", "token": "dynamic_test"}],
                "related_config_selectors": [{"type": "literal", "path": "pyproject.toml"}],
                "minimum_regression_selectors": [
                    {"type": "literal", "path": "tests/test_original.py"}
                ],
                "grounding": {"fact_status": "grounded"},
            }
        ]
    }
    selector_fields = [
        "first_read_selectors",
        "then_check_selectors",
        "related_test_selectors",
        "related_config_selectors",
        "minimum_regression_selectors",
    ]
    task_routes = {
        "allowed_unresolved_sentinels": ["needs_code_confirmation"],
        "resolution_model": {
            "operations": [
                {
                    "op": "collect_module_defaults",
                    "module_fields": selector_fields,
                },
                {"op": "apply_route_deltas", "merge": "append"},
                {
                    "op": "apply_route_overrides",
                    "merge": "replace_named_output_field",
                },
            ]
        },
        "selector_contract": {
            "token_registry": {
                "dynamic_test": {"fallback": "needs_code_confirmation"}
            }
        },
        "routes": [
            {
                "id": "R1",
                "first_read_modules": ["M1"],
                "selector_deltas": {
                    "first_read_selectors": [
                        {"type": "literal", "path": "extra.py"}
                    ]
                },
                "selector_overrides": {
                    "minimum_regression_selectors": [
                        {"type": "literal", "path": "tests/test_override.py"}
                    ]
                },
            }
        ],
    }
    _write_routing_project(tmp_path, repo_map=repo_map, task_routes=task_routes)

    resolved = route_task.resolve_route(project_root=tmp_path, route_id="R1")

    assert resolved["files"]["first_read"] == ["src/main.py", "src", "extra.py"]
    assert resolved["files"]["then_check"] == ["src/lib"]
    assert resolved["tests"] == ["needs_code_confirmation"]
    assert resolved["configs"] == ["pyproject.toml"]
    assert resolved["minimum_regression"] == ["tests/test_override.py"]


def test_legacy_routing_policy_output_is_unchanged(
    route_task: ModuleType,
    tmp_path: Path,
) -> None:
    legacy_fields = [
        "first_read_files",
        "then_check_files",
        "related_tests",
        "related_configs",
        "minimum_regression",
    ]
    repo_map = {
        "modules": [
            {
                "id": "M1",
                "first_read_files": ["src/main.py"],
                "then_check_files": ["src/lib.py"],
                "related_tests": ["tests/test_main.py"],
                "related_configs": ["pyproject.toml"],
                "minimum_regression": ["pytest -q tests/test_main.py"],
                "grounding": {"fact_status": "grounded"},
            }
        ]
    }
    task_routes = {
        "routing_policy": {
            "operational_list_resolution": {
                "apply_to_fields": legacy_fields,
                "merge_strategy": "stable_order_union",
                "route_level_override": "replace_named_output_field",
            },
            "grounding_gate": {"blocking_fact_status": ["unknown"]},
        },
        "routes": [{"id": "R1", "first_read_modules": ["M1"]}],
    }
    _write_routing_project(tmp_path, repo_map=repo_map, task_routes=task_routes)

    resolved = route_task.resolve_route(project_root=tmp_path, route_id="R1")

    assert resolved["routing_policy"] == {
        "operational_fields": legacy_fields,
        "merge_strategy": "stable_order_union",
        "route_level_override": "replace_named_output_field",
    }
    assert resolved["files"] == {
        "first_read": ["src/main.py"],
        "then_check": ["src/lib.py"],
    }
    assert resolved["tests"] == ["tests/test_main.py"]
    assert resolved["configs"] == ["pyproject.toml"]
    assert resolved["minimum_regression"] == ["pytest -q tests/test_main.py"]
