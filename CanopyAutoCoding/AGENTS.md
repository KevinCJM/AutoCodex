# Purpose
Strict machine-first routing protocol for downstream agents inside this subtree only.

# Scope Boundary
- Treat `.` as the full visible boundary.
- Never route to parent paths or infer parent-level structure.
- Mark unresolved edges as `out_of_scope`, `unknown`, or `needs_code_confirmation`.
- Use code, tests, and configs as implementation truth; routing files are navigation only.

# Required Read Order
1. `AGENTS.md`
2. `docs/repo_map.json`
3. `docs/task_routes.json`
4. `docs/pitfalls.json`
5. matched `first_read_files`
6. matched `then_check_files`
7. visible tests/configs before edit

# Hard Rules
- `HR01_module_facts_only`: Keep module facts only in `docs/repo_map.json`.
- `HR02_task_routes_only`: Keep task-routing facts only in `docs/task_routes.json`.
- `HR03_pitfalls_only`: Keep pitfall facts only in `docs/pitfalls.json`.
- `HR04_subtree_only`: Do not infer anything outside the current subtree.
- `HR05_prompt_files_protected`: 未经用户明确允许，禁止修改业务提示词文件。
- `HR06_vendor_tree_read_only`: 禁止修改 `packages/tui/node_modules/**`.
- `HR07_trace_alias_before_edit`: Do not treat top-level alias files as implementation truth before tracing imports.
- `HR08_relations_graph_only`: Interpret module links only from `docs/repo_map.json` `relations[]`; each relation points from the current module to the target module.
- `HR09_route_merge_contract`: Expand derived route fields only by `docs/task_routes.json` `resolution_model`.

# Default Operating Sequence
1. Match the task in `docs/task_routes.json`.
2. Read referenced modules in `docs/repo_map.json`.
3. Read linked pitfalls in `docs/pitfalls.json`.
4. Re-check active code, callers/callees, tests, and configs.
5. Edit the smallest confirmed in-scope surface.
6. Run the listed `minimum_regression`.
7. Report `unknown` and `out_of_scope` edges explicitly.

# Edit Safety Rules
- Before editing shared workflow, bridge, or runtime code, widen reads to all directly linked modules.
- Before editing stage code, confirm the current imported stage target and its prompt/support companions.
- Before editing HITL, cleanup, or reuse logic, confirm artifact paths, hashes, runtime dirs, and resume behavior.
- Do not use terminal prose as completion truth when file contracts exist.

# Verification Rules
- Prefer visible tests listed in `minimum_regression`.
- If tests use legacy names or wrappers, confirm the active package target first.
- Re-check schema writers, protocol writers, and validators before changing completion logic.
- If no direct visible test exists, state the gap and verify imports and call chains manually.

# Output Discipline
- Keep routing facts only in the three JSON files.
- Do not add parallel descriptive routing documents.
- Keep notes short and path-specific.
