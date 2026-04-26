# Purpose
Strict machine-first routing protocol for downstream agents inside this subtree only.

# Scope Boundary
- Treat `.` as the full visible boundary.
- Never inspect, infer, or route to parent paths.
- Mark unresolved edges as `out_of_scope`, `unknown`, or `needs_code_confirmation`.
- Use code, tests, and configs as implementation truth; routing files are navigation only.

# Required Read Order
1. `AGENTS.md`
2. `docs/repo_map.json`
3. `docs/task_routes.json`
4. `docs/pitfalls.json`
5. resolve matched `first_read_selectors`
6. resolve matched `then_check_selectors`
7. inspect visible tests/configs before edit

# Hard Rules
- `HR01_module_facts_only`: Keep module facts only in `docs/repo_map.json`.
- `HR02_task_routes_only`: Keep task-routing facts only in `docs/task_routes.json`.
- `HR03_pitfalls_only`: Keep pitfall facts only in `docs/pitfalls.json`.
- `HR04_subtree_only`: Do not infer anything outside the current subtree.
- `HR05_prompt_files_protected`: 未经用户明确允许，禁止修改业务提示词文件。
- `HR06_vendor_tree_read_only`: 禁止修改 `packages/tui/node_modules/**`.
- `HR07_trace_alias_before_edit`: Do not treat top-level alias files as implementation truth before tracing imports.
- `HR08_relations_graph_only`: Interpret module links only from `docs/repo_map.json` `relations[]`; each relation points from the current module to the target module.
- `HR09_route_merge_contract`: Resolve derived route selectors/tests/configs/regressions only by `docs/task_routes.json` `resolution_model`, including `merge_steps` and `override_precedence`.
- `HR10_owned_paths_authoritative`: Treat `modules[].owned_paths[]` as authoritative path ownership; `modules[].path` is canonical anchor only.
- `HR11_selector_schema_only`: Resolve selector objects only by `docs/task_routes.json` `selector_contract`; do not infer selector meaning from prose.
- `HR12_condition_codes_only`: Interpret module and route condition codes only from `docs/task_routes.json` `condition_code_registry`; interpret pitfall condition codes only from `docs/pitfalls.json` `condition_code_registry`.

# Default Operating Sequence
1. Match the task in `docs/task_routes.json`.
2. Read referenced modules in `docs/repo_map.json`.
3. Read linked pitfalls in `docs/pitfalls.json`.
4. Resolve route/module selectors by `docs/task_routes.json` `selector_contract` before touching code.
5. Re-check active code, callers/callees, tests, and configs.
6. Edit the smallest confirmed in-scope surface.
7. Run the listed `minimum_regression`.
8. Report `unknown` and `out_of_scope` edges explicitly.

# Edit Safety Rules
- Before editing workflow, bridge, runtime, or shared support, widen reads to all directly linked modules.
- Before editing stage code, confirm the active wrapper target, prompt-contract companion, and runtime companion.
- Before editing HITL, cleanup, or reuse logic, confirm artifact paths, runtime dirs, and resume behavior.
- Do not use routing docs or terminal prose as completion truth when file contracts exist.

# Verification Rules
- Prefer visible tests listed in `minimum_regression`.
- If tests use wrapper-style names, confirm the active implementation target first.
- Re-check schema writers, protocol writers, and validators before changing completion logic.
- If no direct visible test exists, state the gap and verify imports and call chains manually.

# Output Discipline
- Keep routing facts only in `docs/repo_map.json`, `docs/task_routes.json`, and `docs/pitfalls.json`.
- Do not add parallel descriptive routing documents.
- Keep protocol concise and machine-directed.
