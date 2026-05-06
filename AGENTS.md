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
5. Re-check active code, callers/callees, tests, and configs.
6. Edit the smallest confirmed in-scope surface.
7. Run materialized `minimum_regression_selectors`.
8. Report `unknown`, `out_of_scope`, or `needs_code_confirmation` explicitly.

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
