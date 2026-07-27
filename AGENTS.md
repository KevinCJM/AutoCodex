# Purpose
Strict machine-first routing protocol for downstream agents inside this subtree only.

# Scope Boundary
- Treat `.` as the full visible boundary.
- Never inspect, infer, or route to parent paths.
- Mark unresolved edges only with allowed sentinels from `docs/task_routes.json`.
- Use routing files for navigation only; implementation truth is code/tests/configs.

# Required Read Order
1. `AGENTS.md`
2. `docs/repo_map.json`
3. `docs/task_routes.json`
4. `docs/pitfalls.json`
5. materialized route/module `first_read_selectors`
6. materialized route/module `then_check_selectors`
7. materialized route/module regression/config selectors

# Hard Rules
- `HR01_module_facts_only`: Module facts live only in `docs/repo_map.json`.
- `HR02_task_routes_only`: Task routing and selector resolution live only in `docs/task_routes.json`.
- `HR03_pitfalls_only`: Risk semantics live only in `docs/pitfalls.json`.
- `HR04_subtree_only`: Do not infer anything outside the current subtree.
- `HR05_relation_refs_only`: Interpret module links only from `docs/repo_map.json` `relations[]`.
- `HR06_route_merge_contract`: Materialize routes only by `docs/task_routes.json` `resolution_model`.
- `HR07_selector_schema_only`: Resolve selectors only by `docs/task_routes.json` `selector_contract`.
- `HR08_owned_paths_authoritative`: Treat `modules[].owned_paths[]` as authoritative ownership.
- `HR09_pitfall_codes_authoritative`: Follow risk gates by pitfall IDs/codes, not duplicated prose.
- `HR10_no_parallel_truth`: Do not add module, route, or pitfall facts outside the three JSON files.

# Default Operating Sequence
1. Match route in `docs/task_routes.json`.
2. Collect referenced modules from `docs/repo_map.json`.
3. Collect referenced pitfalls from `docs/pitfalls.json`.
4. Materialize selectors by the route merge contract.
5. Optionally use project Graphify evidence to widen candidate reads; never let it override routing or prove implementation behavior.
6. Re-check active code, callers/callees, tests, and configs.
7. Edit the smallest confirmed in-scope surface.
8. Run materialized `minimum_regression_selectors`.
9. Report `unknown`, `out_of_scope`, or `needs_code_confirmation` explicitly.

# Edit Safety Rules
- Before editing workflow, bridge, runtime, or shared support, widen reads through `relations[]`.
- Before editing stage code, resolve stage, prompt, and runtime companion selectors.
- Before editing HITL, cleanup, or reuse logic, resolve artifact/runtime/resume selectors.
- Do not use routing docs or terminal prose as completion truth when file contracts exist.

# Verification Rules
- Prefer materialized regression selectors from `docs/task_routes.json`.
- If a selector resolves to a wrapper, trace the implementation target before edit.
- Re-check schema writers, protocol writers, and validators before completion changes.
- If no direct visible test selector exists, state the gap and verify imports/call chains manually.

# Output Discipline
- Keep routing facts only in `docs/repo_map.json`, `docs/task_routes.json`, and `docs/pitfalls.json`.
- Keep `AGENTS.md` protocol-only.
- Do not add parallel descriptive routing documents.

<!-- AI-HERMES-ROUTING-PROTOCOL:BEGIN -->
# AI Hermes Routing Protocol

## Purpose

Machine-first routing protocol for downstream agents operating from the current working directory.

## Scope Boundary

- Treat `.` as the writable project boundary unless higher-priority instructions say otherwise.
- External folders may be read for task understanding, comparison, or integration analysis; do not route edits outside the target project.
- Treat external services, DB schema, and invisible callers/callees as `out_of_scope` unless directly observed from readable files.
- Keep routing facts in JSON files under `docs/`; keep `AGENTS.md` protocol-only.

## Required Read Order

1. `AGENTS.md`
2. `docs/repo_map.json`
3. `docs/task_routes.json`
4. `docs/pitfalls.json`
5. Routed code, tests, and configs

## Routing Ownership

- `docs/task_routes.json` owns task matching, module expansion, and operational-list merge policy.
- `docs/repo_map.json` owns module facts, operational file lists, tests, configs, and regression commands.
- `docs/pitfalls.json` owns hidden contracts, recurring pitfalls, affected modules, and safe checks.
- `AGENTS.md` owns protocol, required read order, scope rules, and tool workflow only.
- Do not duplicate module-level file, test, config, or regression lists in `docs/task_routes.json`.

## Default Operating Sequence

1. Match the task in `docs/task_routes.json`.
2. Load `first_read_modules` from the selected route.
3. Expand into `expand_to_modules` only when route rule codes trigger.
4. Resolve `first_read_files`, `then_check_files`, `related_tests`, `related_configs`, and `minimum_regression` from `docs/repo_map.json` using `docs/task_routes.json` merge policy.
5. Load linked pitfalls from `docs/pitfalls.json`.
6. Use Graphify only as optional static discovery evidence after route resolution.
7. Verify claims from code, tests, configs, or command output before promoting them to routing memory.

## AI Routing Validation

- Use `skills/ai-hermes-self-evolve/scripts/validate_ai_routing.py` after editing `AGENTS.md`, `docs/repo_map.json`, `docs/task_routes.json`, `docs/pitfalls.json`, or matching service routing files.
- The validator checks route/module/pitfall references, routed path existence, git-tracked reproducibility for stable references, minimum regression command targets, and `grounding.fact_status` values.

# AI Routing Self-Evolution

- Treat `docs/ai_routing_evolution_policy.json` as governance only; routing facts belong in `docs/task_routes.json`, `docs/repo_map.json`, and `docs/pitfalls.json`.
- Update `AGENTS.md` only when protocol, required read order, scope rules, or tool workflow changes.
- Promote verified hidden contracts and recurring pitfalls to the correct JSON owner.
- Use `skills/ai-hermes-self-evolve/scripts/evolve_ai_routing.py` after code, test, config, tool, or routing changes to check coverage.
- For routing-only work, run `skills/ai-hermes-self-evolve/scripts/evolve_ai_routing.py --routing-only` with explicit changed paths.
- Re-run `skills/ai-hermes-self-evolve/scripts/validate_ai_routing.py` after routing file changes.

## Output Discipline

- Keep routing facts in JSON only.
- Keep `AGENTS.md` protocol-only.
- Stop exploration once routing is sufficient for first-pass narrowing.
<!-- AI-HERMES-ROUTING-PROTOCOL:END -->
