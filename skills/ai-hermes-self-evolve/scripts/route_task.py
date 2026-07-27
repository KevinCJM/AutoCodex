#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

ROUTING_DATA_FILES = [
    "AGENTS.md",
    "docs/repo_map.json",
    "docs/task_routes.json",
    "docs/pitfalls.json",
    "docs/ai_routing_evolution_policy.json",
]
ROUTING_INIT_NEXT_ACTION = "Run $ai-hermes-routing-init before resolving AI Hermes routes."

_SELECTOR_FIELD_TO_OUTPUT = {
    "first_read_selectors": "first_read_files",
    "then_check_selectors": "then_check_files",
    "related_test_selectors": "related_tests",
    "related_config_selectors": "related_configs",
    "minimum_regression_selectors": "minimum_regression",
}
_DEFAULT_SELECTOR_FIELDS = tuple(_SELECTOR_FIELD_TO_OUTPUT)


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _missing_routing_data_files(project_root: Path) -> list[str]:
    return [rel for rel in ROUTING_DATA_FILES if not (project_root / rel).exists()]


def _stable_union(*lists: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    merged: list[str] = []
    for values in lists:
        for value in values:
            if value not in seen:
                seen.add(value)
                merged.append(value)
    return merged


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _select_route_by_id(
    routes: Sequence[Mapping[str, Any]],
    route_id: str,
) -> Mapping[str, Any] | None:
    route_by_id = {str(route.get("id", "")): route for route in routes}
    return route_by_id.get(route_id)


def _module_closure(module_ids: Sequence[str], module_by_id: Mapping[str, Mapping[str, Any]]) -> list[str]:
    resolved: list[str] = []
    seen: set[str] = set()

    def visit(module_id: str) -> None:
        if module_id in seen:
            return
        seen.add(module_id)
        resolved.append(module_id)
        module = module_by_id.get(module_id, {})
        for child_id in _string_list(module.get("child_modules")):
            visit(child_id)

    for module_id in module_ids:
        visit(module_id)
    return resolved


def _actions(
    route: Mapping[str, Any],
    task_routes: Mapping[str, Any],
) -> dict[str, list[dict[str, str]]]:
    rule_catalog = task_routes.get("rule_catalog", {})
    if not isinstance(rule_catalog, Mapping):
        rule_catalog = {}
    condition_registry = task_routes.get("condition_code_registry", {})
    if not isinstance(condition_registry, Mapping):
        condition_registry = {}
    field_to_catalog = (
        ("expand_search_codes", "expand_search", "expand_search_codes"),
        ("stop_and_verify_codes", "stop_and_verify", "stop_codes"),
        ("stop_codes", "stop_and_verify", "stop_codes"),
        ("fact_check_codes", "fact_check", "fact_check_codes"),
    )
    resolved: dict[str, list[dict[str, str]]] = {}
    for field, catalog_name, condition_name in field_to_catalog:
        catalog = rule_catalog.get(catalog_name, {})
        if not isinstance(catalog, Mapping):
            catalog = {}
        condition_catalog = condition_registry.get(condition_name, {})
        if not isinstance(condition_catalog, Mapping):
            condition_catalog = {}
        entries = resolved.setdefault(catalog_name, [])
        existing_codes = {item["code"] for item in entries}
        for code in _string_list(route.get(field)):
            if code in existing_codes:
                continue
            rule = catalog.get(code, {})
            action = rule.get("action") if isinstance(rule, Mapping) else None
            if not action:
                action = condition_catalog.get(code)
            entries.append({"code": code, "action": str(action or "unknown")})
            existing_codes.add(code)
    return resolved


def _collect_operational_lists(
    selected_modules: Sequence[str],
    module_by_id: Mapping[str, Mapping[str, Any]],
    fields: Sequence[str],
) -> dict[str, list[str]]:
    operational_lists: dict[str, list[str]] = {}
    for field in fields:
        merged: list[str] = []
        for module_id in selected_modules:
            merged = _stable_union(merged, _string_list(module_by_id.get(module_id, {}).get(field)))
        operational_lists[field] = merged
    return operational_lists


def _selector_fields(task_routes: Mapping[str, Any]) -> list[str]:
    """Read module selector fields from the current resolution_model contract."""
    resolution_model = task_routes.get("resolution_model")
    if not isinstance(resolution_model, Mapping):
        return []
    operations = resolution_model.get("operations", [])
    if not isinstance(operations, list):
        operations = []
    for operation in operations:
        if not isinstance(operation, Mapping) or operation.get("op") != "collect_module_defaults":
            continue
        fields = _string_list(operation.get("module_fields"))
        return fields or list(_DEFAULT_SELECTOR_FIELDS)
    # A resolution_model marks the selector schema even when an older producer
    # omitted the descriptive operations list.
    return list(_DEFAULT_SELECTOR_FIELDS)


def _selector_fallback(task_routes: Mapping[str, Any], token: str = "") -> str:
    contract = task_routes.get("selector_contract", {})
    if not isinstance(contract, Mapping):
        contract = {}
    registry = contract.get("token_registry", {})
    if not isinstance(registry, Mapping):
        registry = {}
    token_contract = registry.get(token, {})
    fallback = token_contract.get("fallback") if isinstance(token_contract, Mapping) else None
    allowed = _string_list(task_routes.get("allowed_unresolved_sentinels"))
    if isinstance(fallback, str) and fallback in allowed:
        return fallback
    if "needs_code_confirmation" in allowed:
        return "needs_code_confirmation"
    if "unknown" in allowed:
        return "unknown"
    return ""


def _materialize_selector_lists(
    *,
    selected_modules: Sequence[str],
    module_by_id: Mapping[str, Mapping[str, Any]],
    route: Mapping[str, Any],
    task_routes: Mapping[str, Any],
    fields: Sequence[str],
) -> dict[str, list[str]]:
    """Resolve the selector schema into the legacy public operational lists.

    ``field_ref`` is scoped to the module that owns the selector, matching the
    selector contract. Route-level selectors can name ``module_id`` explicitly
    for selectors such as ``owned_paths_role``; unresolved tokens emit only an
    allowed sentinel.
    """
    cache: dict[tuple[str, str], list[str]] = {}

    def materialize_module_field(module_id: str, field: str, stack: set[tuple[str, str]]) -> list[str]:
        cache_key = (module_id, field)
        if cache_key in cache:
            return list(cache[cache_key])
        if cache_key in stack:
            fallback = _selector_fallback(task_routes)
            return [fallback] if fallback else []
        module = module_by_id.get(module_id, {})
        values = module.get(field, [])
        if not isinstance(values, list):
            values = []
        next_stack = set(stack)
        next_stack.add(cache_key)
        resolved: list[str] = []
        for selector in values:
            resolved = _stable_union(
                resolved,
                materialize_selector(selector, origin_module_id=module_id, stack=next_stack),
            )
        cache[cache_key] = resolved
        return list(resolved)

    def materialize_selector(
        selector: Any,
        *,
        origin_module_id: str | None,
        stack: set[tuple[str, str]],
    ) -> list[str]:
        if isinstance(selector, str):
            return [selector] if selector else []
        if not isinstance(selector, Mapping):
            return []
        selector_type = str(selector.get("type", "") or "").strip()
        if selector_type in {"literal", "subtree"}:
            path = selector.get("path")
            return [path] if isinstance(path, str) and path else []
        if selector_type == "field_ref":
            field = selector.get("field")
            if not isinstance(field, str) or not field or not origin_module_id:
                fallback = _selector_fallback(task_routes)
                return [fallback] if fallback else []
            return materialize_module_field(origin_module_id, field, stack)
        if selector_type == "token":
            token = str(selector.get("token", "") or "")
            fallback = _selector_fallback(task_routes, token)
            return [fallback] if fallback else []
        if selector_type == "owned_paths_role":
            module_id = str(selector.get("module_id", "") or origin_module_id or "")
            role = str(selector.get("role", "") or "")
            module = module_by_id.get(module_id, {})
            owned_paths = module.get("owned_paths", [])
            if not isinstance(owned_paths, list):
                owned_paths = []
            paths = [
                str(item.get("path"))
                for item in owned_paths
                if isinstance(item, Mapping)
                and item.get("role") == role
                and isinstance(item.get("path"), str)
                and item.get("path")
            ]
            if paths:
                return _stable_union(paths)
            fallback = _selector_fallback(task_routes)
            return [fallback] if fallback else []
        fallback = _selector_fallback(task_routes)
        return [fallback] if fallback else []

    selector_lists: dict[str, list[str]] = {}
    deltas = route.get("selector_deltas", {})
    overrides = route.get("selector_overrides", {})
    if not isinstance(deltas, Mapping):
        deltas = {}
    if not isinstance(overrides, Mapping):
        overrides = {}

    for field in fields:
        merged: list[str] = []
        for module_id in selected_modules:
            merged = _stable_union(merged, materialize_module_field(module_id, field, set()))
        output_field = _SELECTOR_FIELD_TO_OUTPUT.get(field, field)
        delta_key = field if field in deltas else output_field
        delta_values = deltas.get(delta_key, [])
        if isinstance(delta_values, list):
            for selector in delta_values:
                merged = _stable_union(
                    merged,
                    materialize_selector(selector, origin_module_id=None, stack=set()),
                )
        override_key = field if field in overrides else output_field
        if override_key in overrides:
            merged = []
            override_values = overrides.get(override_key, [])
            if isinstance(override_values, list):
                for selector in override_values:
                    merged = _stable_union(
                        merged,
                        materialize_selector(selector, origin_module_id=None, stack=set()),
                    )
        selector_lists[field] = merged
        selector_lists[output_field] = merged
    return selector_lists


def _collect_pitfalls(
    route: Mapping[str, Any],
    selected_modules: Sequence[str],
    module_by_id: Mapping[str, Mapping[str, Any]],
    pitfall_by_id: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    pitfall_ids = _string_list(route.get("pitfall_ids"))
    for module_id in selected_modules:
        pitfall_ids = _stable_union(pitfall_ids, _string_list(module_by_id.get(module_id, {}).get("pitfall_ids")))

    pitfalls: list[dict[str, Any]] = []
    for pitfall_id in pitfall_ids:
        row = pitfall_by_id.get(pitfall_id, {})
        pitfalls.append(
            {
                "id": pitfall_id,
                "title": row.get("title", "unknown"),
                "severity": row.get("severity", "unknown"),
                "check_before_edit": _string_list(row.get("check_before_edit")),
            }
        )
    return pitfalls


def _collect_grounding(
    selected_modules: Sequence[str],
    module_by_id: Mapping[str, Mapping[str, Any]],
    blocking_statuses: Sequence[str],
) -> dict[str, Any]:
    unknowns: list[dict[str, Any]] = []
    for module_id in selected_modules:
        grounding = module_by_id.get(module_id, {}).get("grounding", {})
        raw_status = grounding.get("fact_status") if isinstance(grounding, Mapping) else None
        fact_status = raw_status if isinstance(raw_status, str) else "unknown"
        if fact_status != "grounded":
            unknowns.append(
                {
                    "module_id": module_id,
                    "fact_status": fact_status,
                    "is_blocking": fact_status in blocking_statuses,
                    "unsampled_paths": _string_list(grounding.get("unsampled_paths") if isinstance(grounding, Mapping) else []),
                }
            )
    return {
        "blocking_fact_status": list(blocking_statuses),
        "unknowns": unknowns,
    }


def resolve_route(
    *,
    project_root: Path,
    route_id: str,
    expand: str = "conditional",
) -> dict[str, Any]:
    project_root = project_root.resolve()
    missing_required_files = _missing_routing_data_files(project_root)
    if missing_required_files:
        return {
            "status": "routing_not_initialized",
            "route_id": route_id,
            "missing_required_files": missing_required_files,
            "next_action": ROUTING_INIT_NEXT_ACTION,
        }
    agents_text = (project_root / "AGENTS.md").read_text(encoding="utf-8")
    repo_map = _load_json(project_root / "docs" / "repo_map.json")
    task_routes = _load_json(project_root / "docs" / "task_routes.json")
    pitfalls = _load_json(project_root / "docs" / "pitfalls.json")

    routes = task_routes.get("routes", [])
    modules = repo_map.get("modules", [])
    pitfall_rows = pitfalls.get("pitfalls", [])
    if not isinstance(routes, list):
        routes = []
    if not isinstance(modules, list):
        modules = []
    if not isinstance(pitfall_rows, list):
        pitfall_rows = []

    selected_route = _select_route_by_id(routes, route_id)
    if selected_route is None:
        return {
            "status": "no_match",
            "route_id": route_id,
        }

    module_by_id = {
        str(module.get("id", "")): module
        for module in modules
        if isinstance(module, Mapping) and isinstance(module.get("id"), str)
    }
    pitfall_by_id = {
        str(pitfall.get("id", "")): pitfall
        for pitfall in pitfall_rows
        if isinstance(pitfall, Mapping) and isinstance(pitfall.get("id"), str)
    }

    primary_modules = _string_list(selected_route.get("first_read_modules"))
    primary_with_children = _module_closure(primary_modules, module_by_id)
    route_expand_modules = _string_list(selected_route.get("expand_to_modules"))
    expand_with_children = _module_closure(route_expand_modules, module_by_id)

    if expand == "always":
        selected_expand_modules = expand_with_children
        conditional_expand_modules: list[str] = []
        skipped_expand_modules: list[str] = []
    elif expand == "never":
        selected_expand_modules = []
        conditional_expand_modules = []
        skipped_expand_modules = expand_with_children
    else:
        selected_expand_modules = []
        conditional_expand_modules = expand_with_children
        skipped_expand_modules = []

    selected_modules = _stable_union(primary_with_children, selected_expand_modules)

    selector_fields = _selector_fields(task_routes)
    if selector_fields:
        operational_fields = selector_fields
        operational_lists = _materialize_selector_lists(
            selected_modules=selected_modules,
            module_by_id=module_by_id,
            route=selected_route,
            task_routes=task_routes,
            fields=selector_fields,
        )
        operations = task_routes.get("resolution_model", {}).get("operations", [])
        if not isinstance(operations, list):
            operations = []
        operation_by_name = {
            str(operation.get("op", "")): operation
            for operation in operations
            if isinstance(operation, Mapping)
        }
        merge_strategy = operation_by_name.get("apply_route_deltas", {}).get("merge")
        route_level_override = operation_by_name.get("apply_route_overrides", {}).get("merge")
    else:
        routing_policy = task_routes.get("routing_policy", {})
        if not isinstance(routing_policy, Mapping):
            routing_policy = {}
        resolution = routing_policy.get("operational_list_resolution", {})
        if not isinstance(resolution, Mapping):
            resolution = {}
        operational_fields = _string_list(resolution.get("apply_to_fields"))
        operational_lists = _collect_operational_lists(selected_modules, module_by_id, operational_fields)
        merge_strategy = resolution.get("merge_strategy")
        route_level_override = resolution.get("route_level_override")
    routing_policy = task_routes.get("routing_policy", {})
    if not isinstance(routing_policy, Mapping):
        routing_policy = {}
    blocking_statuses = _string_list(
        routing_policy.get("grounding_gate", {}).get("blocking_fact_status")
        if isinstance(routing_policy.get("grounding_gate"), Mapping)
        else []
    )

    return {
        "status": "ok",
        "route_id": route_id,
        "agents_protocol": {
            "path": "AGENTS.md",
            "loaded": True,
            "has_required_read_order": "# Required Read Order" in agents_text,
        },
        "route": {
            "id": selected_route.get("id"),
            "task_type": selected_route.get("task_type"),
            "route_priority": selected_route.get("route_priority"),
        },
        "routing_policy": {
            "operational_fields": operational_fields,
            "merge_strategy": merge_strategy,
            "route_level_override": route_level_override,
        },
        "modules": {
            "primary": primary_modules,
            "primary_with_children": primary_with_children,
            "selected": selected_modules,
            "selected_expand": selected_expand_modules,
            "conditional_expand": conditional_expand_modules,
            "skipped_expand": skipped_expand_modules,
        },
        "expansion": {
            "mode": expand,
            "note": "expand_to_modules are selected only when rule codes trigger; --expand always/never are diagnostic overrides.",
        },
        "operational_lists": operational_lists,
        "files": {
            "first_read": operational_lists.get("first_read_files", []),
            "then_check": operational_lists.get("then_check_files", []),
        },
        "tests": operational_lists.get("related_tests", []),
        "configs": operational_lists.get("related_configs", []),
        "minimum_regression": operational_lists.get("minimum_regression", []),
        "pitfalls": _collect_pitfalls(selected_route, selected_modules, module_by_id, pitfall_by_id),
        "scope_flags": _stable_union(
            _string_list(selected_route.get("scope_flags")),
            *[_string_list(module_by_id.get(module_id, {}).get("scope_flags")) for module_id in selected_modules],
        ),
        "risk_flags": _stable_union(
            *[_string_list(module_by_id.get(module_id, {}).get("risk_flags")) for module_id in selected_modules],
        ),
        "grounding": _collect_grounding(selected_modules, module_by_id, blocking_statuses),
        "actions": _actions(selected_route, task_routes),
    }


def route_resolver(
    *,
    project_root: Path,
    route_id: str,
    expand: str = "conditional",
) -> dict[str, Any]:
    return resolve_route(project_root=project_root, route_id=route_id, expand=expand)


def _md_list(values: Sequence[str], empty: str = "None") -> str:
    if not values:
        return f"- {empty}"
    return "\n".join(f"- {value}" for value in values)


def _md_pitfalls(pitfalls: Sequence[Mapping[str, Any]]) -> str:
    if not pitfalls:
        return "- None"
    return "\n".join(
        f"- {item.get('id')}: {item.get('title')} ({item.get('severity')})" for item in pitfalls
    )


def _md_unknowns(grounding: Mapping[str, Any]) -> str:
    unknowns = grounding.get("unknowns", [])
    if not unknowns:
        return "- None"
    lines: list[str] = []
    for item in unknowns:
        paths = item.get("unsampled_paths") or []
        path_note = f"; unsampled: {', '.join(paths)}" if paths else ""
        lines.append(
            f"- {item.get('module_id')}: {item.get('fact_status')} "
            f"(blocking={item.get('is_blocking')}){path_note}"
        )
    return "\n".join(lines)


def _md_actions(actions: Mapping[str, Sequence[Mapping[str, str]]]) -> str:
    lines: list[str] = []
    for group in ("expand_search", "stop_and_verify", "fact_check"):
        for item in actions.get(group, []):
            lines.append(f"- {group}.{item.get('code')}: {item.get('action')}")
    return "\n".join(lines) if lines else "- None"


def render_context_markdown(resolved: Mapping[str, Any]) -> str:
    if resolved.get("status") != "ok":
        return _render_non_ok_markdown(resolved)
    route = resolved["route"]
    modules = resolved["modules"]
    expansion = resolved["expansion"]
    lines = [
        "Route",
        f"{route.get('id')} / {route.get('task_type')}",
        "",
        "Read first",
        _md_list(resolved.get("files", {}).get("first_read", [])),
        "",
        "Selected modules",
        _md_list(modules.get("selected", [])),
        "",
        "Conditional expansion",
        f"- mode: {expansion.get('mode')}",
        _md_list(modules.get("conditional_expand", [])),
        "",
        "Key pitfalls",
        _md_pitfalls(resolved.get("pitfalls", [])),
        "",
        "Unknowns",
        _md_unknowns(resolved.get("grounding", {})),
    ]
    return "\n".join(lines)


def route_to_context(resolved: Mapping[str, Any]) -> str:
    return render_context_markdown(resolved)


def render_brief_markdown(resolved: Mapping[str, Any]) -> str:
    if resolved.get("status") != "ok":
        return _render_non_ok_markdown(resolved)
    route = resolved["route"]
    modules = resolved["modules"]
    expansion = resolved["expansion"]
    lines = [
        "Context",
        f"- Route: {route.get('id')} / {route.get('task_type')}",
        f"- Expansion mode: {expansion.get('mode')}",
        f"- Expansion note: {expansion.get('note')}",
        "",
        "Primary modules",
        _md_list(modules.get("primary_with_children", [])),
        "",
        "Selected expand modules",
        _md_list(modules.get("selected_expand", [])),
        "",
        "First read",
        _md_list(resolved.get("files", {}).get("first_read", [])),
        "",
        "Then check",
        _md_list(resolved.get("files", {}).get("then_check", [])),
        "",
        "Constraints",
        _md_list(resolved.get("scope_flags", [])),
        _md_list(resolved.get("risk_flags", [])),
        _md_pitfalls(resolved.get("pitfalls", [])),
        "",
        "Grounding constraints",
        _md_unknowns(resolved.get("grounding", {})),
        "",
        "Required actions",
        _md_actions(resolved.get("actions", {})),
        "",
        "Done when",
        _md_list(resolved.get("minimum_regression", [])),
    ]
    return "\n".join(lines)


def route_to_brief(resolved: Mapping[str, Any]) -> str:
    return render_brief_markdown(resolved)


def _render_non_ok_markdown(resolved: Mapping[str, Any]) -> str:
    status = resolved.get("status")
    lines = [
        "Route id",
        str(resolved.get("route_id", "")),
        "",
        "Routing status",
        str(status),
    ]
    missing = resolved.get("missing_required_files")
    if isinstance(missing, Sequence) and not isinstance(missing, (str, bytes, bytearray)):
        lines.extend(["", "Missing required routing files"])
        lines.extend(f"- {item}" for item in missing)
    if resolved.get("next_action"):
        lines.extend(["", "Next action", str(resolved.get("next_action"))])
    return "\n".join(lines)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Resolve an explicit AI Hermes route id into discussion context or execution brief."
    )
    parser.add_argument("--route-id", required=True, help="Explicit route id, such as R12.")
    parser.add_argument("--mode", choices=["context", "brief"], required=True)
    parser.add_argument("--format", choices=["md", "json"], default="md")
    parser.add_argument("--expand", choices=["never", "conditional", "always"], default="conditional")
    parser.add_argument(
        "--project-root",
        type=Path,
        default=Path.cwd(),
        help="Repository root containing AGENTS.md and docs/.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    resolved = resolve_route(
        project_root=args.project_root,
        route_id=args.route_id,
        expand=args.expand,
    )
    if args.format == "json":
        print(json.dumps(resolved, indent=2, sort_keys=True))
    elif args.mode == "brief":
        print(render_brief_markdown(resolved))
    else:
        print(render_context_markdown(resolved))
    return 0 if resolved.get("status") in {"ok", "no_match"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
