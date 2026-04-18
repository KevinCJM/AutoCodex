# Purpose
Strict routing protocol for downstream agents operating inside this subtree only.

# Scope Boundary
- Treat `.` as the full visible project boundary for routing.
- Do not route to parent paths or infer parent-level structure.
- Mark unresolved external links as `out_of_scope`, `unknown`, or `needs_code_confirmation`.
- Treat routing JSON as navigation hints; code, tests, and configs remain implementation truth.

# Required Read Order
1. `AGENTS.md`
2. `docs/repo_map.json`
3. `docs/task_routes.json`
4. `docs/pitfalls.json`
5. matched `first_read_files`
6. matched `then_check_files`
7. visible tests/configs before edit

# Hard Rules
- Keep module facts only in `docs/repo_map.json`.
- Keep task-routing facts only in `docs/task_routes.json`.
- Keep pitfall facts only in `docs/pitfalls.json`.
- Do not use terminal prose as completion truth when file contracts exist.
- Do not infer active stage targets from legacy filenames; confirm imports/callers first.

# Default Operating Sequence
1. Match the task in `docs/task_routes.json`.
2. Read referenced module entries in `docs/repo_map.json`.
3. Read linked pitfall entries in `docs/pitfalls.json`.
4. Re-check active code, tests, configs, and caller/callee links.
5. Edit the smallest confirmed in-scope surface.
6. Run the listed minimum visible regression.
7. Report unknowns and out-of-scope edges explicitly.

# Edit Safety Rules
- Before editing shared runtime, widen reads to all directly linked modules in `repo_map.json`.
- Before editing stage files, check matching prompt/support files from `then_check_files`.
- Before editing HITL or cleanup logic, confirm artifact paths, hashes, and reuse behavior.
- Do not edit vendored files under `packages/tui/node_modules`.

# Verification Rules
- Prefer visible tests listed in `minimum_regression`.
- If tests use legacy stage names, confirm the active target file before editing.
- If no direct test is visible, state the gap and verify callers/callees manually.
- Re-check schema/protocol writers and validators before changing completion logic.

# Output Discipline
- Keep new routing facts in the JSON files only.
- Do not add parallel human-oriented routing documents.
- Keep execution notes short and path-specific.
