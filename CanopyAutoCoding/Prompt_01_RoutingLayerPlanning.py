# -*- encoding: utf-8 -*-
"""
@File: Prompt_01_RoutingLayerPlanning.py
@Modify Time: 2026/4/9 14:32       
@Author: Kevin-Chen
@Descriptions: 
"""

from __future__ import annotations

from pathlib import Path

from canopy_core.prompt_contracts.spec import (
    ACCESS_READ,
    ACCESS_WRITE,
    CHANGE_MUST_CHANGE,
    CHANGE_NONE,
    CLEANUP_SYSTEM_BEFORE_STAGE_OR_RETRY,
    SPECIAL_REVIEW_FAIL,
    SPECIAL_REVIEW_PASS,
    SPECIAL_STAGE_ARTIFACT,
    FileSpec,
    OutcomeSpec,
    agent_prompt,
    prompt_helper,
    wraps_prompt,
)


# 创建路由层文件, 由 [路由创建器] 执行
@agent_prompt(
    prompt_id="a01.routing.create",
    stage="a01",
    role="routing_agent",
    intent="create_routing_layer",
    mode="a01_routing_create",
    files={
        "agents_md": FileSpec(path_arg="agents_md", access=ACCESS_WRITE, change=CHANGE_MUST_CHANGE, cleanup=CLEANUP_SYSTEM_BEFORE_STAGE_OR_RETRY, special=SPECIAL_STAGE_ARTIFACT),
        "repo_map_json": FileSpec(path_arg="repo_map_json", access=ACCESS_WRITE, change=CHANGE_MUST_CHANGE, cleanup=CLEANUP_SYSTEM_BEFORE_STAGE_OR_RETRY, special=SPECIAL_STAGE_ARTIFACT),
        "task_routes_json": FileSpec(path_arg="task_routes_json", access=ACCESS_WRITE, change=CHANGE_MUST_CHANGE, cleanup=CLEANUP_SYSTEM_BEFORE_STAGE_OR_RETRY, special=SPECIAL_STAGE_ARTIFACT),
        "pitfalls_json": FileSpec(path_arg="pitfalls_json", access=ACCESS_WRITE, change=CHANGE_MUST_CHANGE, cleanup=CLEANUP_SYSTEM_BEFORE_STAGE_OR_RETRY, special=SPECIAL_STAGE_ARTIFACT),
    },
    outcomes={"completed": OutcomeSpec(status="completed", requires=("agents_md", "repo_map_json", "task_routes_json", "pitfalls_json"))},
)
def create_routing_layer_file(
        agents_md="AGENTS.md",
        repo_map_json="docs/repo_map.json",
        task_routes_json="docs/task_routes.json",
        pitfalls_json="docs/pitfalls.json",
):
    create_routing_layer_file_prompt = """Your task is **not** to modify business code. Your task is to generate a **strictly machine-first AI-to-AI routing layer** for the **current agent working directory subtree only**.

The routing layer is for:

* downstream coding agents
* schedulers / orchestrators
* automated task-to-path narrowing
* edit-risk injection
* pre-edit verification
* regression routing

It is **not** for human-oriented project documentation.

This is a **bounded routing initialization task**, not an exhaustive reverse-engineering task.
Do not keep exploring once you already have enough grounded information to produce the 4 required outputs.
Good-enough grounded routing is preferred over exhaustive coverage.

## Scan budget (hard rule)

Before drafting the 4 required outputs, use a **small bounded scan budget**:

* inspect at most about 12 files total in the first pass
* prioritize README, packaging/build files, top-level source directories, and visible tests first
* do **not** read every source file in a large homogeneous directory
* for a directory with many similar files, inspect only 1-3 representative files, then route the rest as a family
* CI/workflow/editor metadata is **low priority** unless the visible subtree is mostly tooling and contains little or no source code

If the first bounded scan already reveals likely module families, entry points, tests, and risks, start writing the 4 required files immediately.

## Required output paths (authoritative)

If any earlier instruction mentions default routing filenames, the following exact paths override it for this run:

* AGENTS instructions: `{agents_md}`
* module facts: `{repo_map_json}`
* task routes: `{task_routes_json}`
* pitfalls: `{pitfalls_json}`

## First-write rule (hard rule)

Do not spend long in planning mode after the bounded scan.

After the first bounded scan, you must immediately do the first write by creating or overwriting all 4 required output files.
After the first bounded scan, your next shell action must be a write action that creates at least one of the 4 required files.
Do not do another read/search/list command before that first write.

The first write may use grounded placeholders such as:

* `unknown`
* `needs_code_confirmation`
* empty arrays where no grounded item is visible yet

Do not wait for perfect completeness before creating the files.
Write the file skeletons first, then fill them in place.
If you catch yourself writing a planning note instead of files, stop and write the files.

This task is document generation only.
Do not plan code changes, patches, or review workflows.

## Primary goals

1. Reduce first-pass code-location cost for downstream agents
2. Force downstream agents to re-check code, tests, config, schema, and call chains before acting
3. Expose routing, task mapping, dependency edges, and risks in a **machine-consumable** format
4. Prevent downstream agents from over-claiming facts outside the current visible scope

---

## Required outputs

Create or overwrite exactly these 4 files:

1. `AGENTS.md`
2. `docs/repo_map.json`
3. `docs/task_routes.json`
4. `docs/pitfalls.json`

---

# 0. Scope boundary rules (highest priority)

Your analysis scope is strictly limited to:

* the current agent working directory
* files and directories under that directory only

You must **not**:

* inspect parent directories
* infer repository root from parent paths
* assume the current working directory is the full repo root
* describe modules, tests, configs, entry points, or services outside the current subtree
* route future agents to parent-level files

If the current working directory is only a subdirectory of a larger repository, you must still treat it as the **entire visible project boundary for this task**.

When some dependency or caller/callee relationship appears to cross outside the visible subtree:

* do not guess
* do not invent missing structure
* mark it explicitly as `out_of_scope`
* record only what can be grounded from visible code

Use subtree-only routing semantics everywhere.

---

# 1. Output philosophy

This routing layer must be **strict AI-to-AI** and **machine-first**.

That means:

* structured data owns routing facts
* prose is minimized
* human readability is not a design goal
* repeated narrative explanation is forbidden
* no parallel descriptive views
* no markdown module encyclopedia
* no markdown architecture summary
* no markdown task explanation
* no markdown pitfall cards

The only markdown file allowed is `AGENTS.md`, and it is a **protocol file for agents**, not a human project document.

---

# 2. Ownership model

## 2.1 `docs/repo_map.json`

This is the **only module-level routing source of truth**.

It owns:

* module identity
* module path
* module purpose
* entry files
* key symbols
* internal dependency edges
* visible external dependency edges
* related tests
* related configs
* first-read files
* then-check files
* risk flags
* blast radius
* minimum regression
* scope limitations

No other file may redefine modules independently.

---

## 2.2 `docs/task_routes.json`

This is the **only task-routing source of truth**.

It owns:

* task types
* keyword matching
* task-to-module mapping
* task-to-file routing
* routing priority
* first-read / then-check
* expand-search conditions
* fact-check reminders
* relevant pitfall linkage

No other file may restate task-routing logic in prose.

---

## 2.3 `docs/pitfalls.json`

This is the **only pitfall / hidden-risk source of truth**.

It owns:

* pitfall IDs
* severity
* trigger actions
* symptoms
* related paths
* affected modules
* blast radius
* required checks before edit

No other file may contain long pitfall explanations.

---

## 2.4 `AGENTS.md`

This is the **only agent protocol file**.

It owns:

* required read order
* hard rules
* scope boundary rules
* verification rules
* edit safety rules
* default operating sequence

It must **not** become a module guide, task guide, architecture guide, or pitfall database.

---

# 3. Non-goals

Do **not** produce:

* human-friendly repo documentation
* architecture prose
* module cards
* task explanation pages
* pitfall essays
* function-by-function commentary
* guessed repository-wide structure
* any “helpful summary” that duplicates structured routing facts

Do **not** optimize for human scanning.

Do **not** create parallel descriptive documents.

Do **not** turn JSON into a narrative knowledge base.

---

# 4. Data model requirements

## 4.1 `docs/repo_map.json`

Generate this file first.

Suggested top-level structure:

```json
{
  "schema_version": "1.0",
  "scope": {
    "root": ".",
    "mode": "subtree_only",
    "parent_inspection_allowed": false,
    "notes": []
  },
  "modules": []
}
```

Each module should prefer fields such as:

* `id`
* `name`
* `path`
* `kind`
* `purpose`
* `task_keywords`
* `entry_files`
* `key_symbols`
* `first_read_files`
* `then_check_files`
* `upstream_dependencies`
* `downstream_dependencies`
* `out_of_scope_dependencies`
* `related_tests`
* `related_configs`
* `risk_flags`
* `edit_risk`
* `blast_radius`
* `pitfall_ids`
* `minimum_regression`
* `read_before_edit`
* `routing_confidence`
* `expand_search_when`
* `scope_notes`
* `notes`

Constraints:

* include only high-value modules inside the current subtree
* for many similar files in one directory, prefer one family-level module entry plus representative files over one module per file
* use stable concise IDs
* do not include modules outside visible scope
* use arrays, not long prose paragraphs
* prefer short machine-usable phrases over narrative text
* when uncertain, use `unknown`, `needs_code_confirmation`, or `out_of_scope`

---

## 4.2 `docs/task_routes.json`

Suggested top-level structure:

```json
{
  "schema_version": "1.0",
  "scope_mode": "subtree_only",
  "routes": []
}
```

Each route should prefer fields such as:

* `id`
* `task_type`
* `match_keywords`
* `negative_keywords`
* `route_priority`
* `first_read_modules`
* `first_read_files`
* `then_check_files`
* `related_tests`
* `related_configs`
* `pitfall_ids`
* `expand_search_when`
* `stop_and_verify_when`
* `fact_check`
* `minimum_regression`
* `scope_notes`
* `notes`

Constraints:

* routes must reference module IDs defined in `repo_map.json`
* do not duplicate full module descriptions
* do not include prose task tutorials
* optimize for downstream agent narrowing, not human explanation
* prefer compact lists over paragraphs

---

## 4.3 `docs/pitfalls.json`

Suggested top-level structure:

```json
{
  "schema_version": "1.0",
  "scope_mode": "subtree_only",
  "pitfalls": []
}
```

Each pitfall should prefer fields such as:

* `id`
* `title`
* `severity`
* `confidence`
* `symptoms`
* `trigger_actions`
* `why_risky`
* `related_paths`
* `affected_modules`
* `blast_radius`
* `check_before_edit`
* `safe_observation_methods`
* `notes`

Constraints:

* only include pitfalls grounded by visible code
* no essay-style explanations
* prefer edit-trigger-oriented wording
* use stable IDs such as `P01`, `P02`
* keep phrasing short and operational

---

## 4.4 `AGENTS.md`

This file is allowed to be markdown, but it must stay short and protocol-only.

Required sections:

* `Purpose`
* `Scope Boundary`
* `Required Read Order`
* `Hard Rules`
* `Default Operating Sequence`
* `Edit Safety Rules`
* `Verification Rules`
* `Output Discipline`

Constraints:

* do not explain architecture
* do not explain modules
* do not explain tasks
* do not explain pitfalls in detail
* do not add narrative project overview
* keep instructions short, strict, and machine-directed

---

# 5. Execution order (must follow)

Perform work in this exact order:

1. Scan the current working directory subtree only
2. Identify high-value modules, entry files, tests, configs, and risk surfaces only within visible scope
3. Generate `docs/repo_map.json`
4. Generate `docs/task_routes.json`
5. Generate `docs/pitfalls.json`
6. Generate `AGENTS.md`
7. Run consistency and dedup checks

Do not create any human-oriented projection files.

## 5.1 Bounded scan rule

Use a bounded first pass.

That means:

* prioritize obvious entry files, packaging/build files, visible tests, and top-level source directories first
* treat `.github`, IDE files, and other tooling metadata as secondary unless source structure is too thin to route without them
* stop broad exploration once you can already identify high-value modules and routing edges with reasonable confidence
* prefer incomplete-but-grounded routing with explicit `unknown` / `needs_code_confirmation` over prolonged open-ended scanning
* if a directory contains many similarly named implementation files, sample a few representative files and route the directory as a module family
* do not attempt to read every sibling file just to improve completeness
* after the first bounded scan, start writing the 4 required files immediately
* after the first bounded scan, create or overwrite all 4 required files before doing any further extended exploration
* if some fields are still incomplete, write grounded placeholders first and refine them in place
* do not stay in planning mode once the first bounded scan is finished
* after the first bounded scan, the next shell action must be a file write, not another read/search/list command
* if a detail is low-value for routing, do not keep digging for it

This stage is successful when the 4 routing files are correctly produced, not when every visible file has been read.

---

# 6. Consistency rules (must enforce)

## 6.1 Boundary consistency

* no parent-directory references
* no upward routing
* no invisible module claims
* all out-of-scope edges explicitly marked

## 6.2 Ownership consistency

* modules exist only in `repo_map.json`
* routes exist only in `task_routes.json`
* pitfalls exist only in `pitfalls.json`
* `AGENTS.md` contains protocol only

## 6.3 Reference consistency

* every module ID used by `task_routes.json` must exist in `repo_map.json`
* every pitfall ID referenced by modules or routes must exist in `pitfalls.json`
* every listed file path must be inside the current subtree
* every regression/test reference must be visible in current scope

## 6.4 Dedup consistency

* no duplicated module catalogs
* no duplicated route logic
* no duplicated pitfall explanations
* no descriptive markdown mirrors of JSON content

---

# 7. Quality bar

The routing layer succeeds only if a downstream coding agent can:

* identify likely modules quickly
* choose first files to read
* know what tests/configs to inspect
* detect scope boundaries
* recognize hidden risks before editing
* avoid treating routing files as implementation facts

The routing layer fails if:

* it behaves like human documentation
* it repeats the same information in multiple files
* it contains long narrative explanation
* it overclaims beyond visible subtree
* it lacks machine-usable routing fields
* it pushes agents toward assumptions instead of code confirmation

---

# 8. Final self-check (must perform)

Before finishing, verify:

1. Only the required 4 files are created or overwritten
2. No human-oriented routing markdown files were produced
3. JSON files are the only routing fact sources
4. All references are internally consistent
5. Scope is strictly subtree-only
6. No parent-level structure was inferred
7. `AGENTS.md` stayed protocol-only
8. Structured fields are concise and operational
9. Repeated explanatory prose has been removed

---

# 9. Terminal output discipline

No terminal summary is required for this stage.

Do not spend time preparing a prose answer after the files are ready.

Once the 4 required files are written and the self-check passes, stop further exploration."""
    return create_routing_layer_file_prompt


# 路由层文件审核, 由 [路由创建器] 执行
@prompt_helper(no_turn=True)
def routing_layer_file_audit():
    routing_layer_file_audit_prompt = """# Optimized Prompt — Audit of the Machine-First Routing Layer (WORKDIR Only)

Your task is to **audit the architectural design and system quality** of the **machine-first Project Routing Layer that exists inside the current working directory only**, referred to as `WORKDIR`.

This is **not** a content rewrite task.
This is **not** a business-logic validation task.
This is a **routing-layer architecture audit** focused on whether the current routing system is truly **AI-to-AI**, **machine-consumable**, and **low-drift**.

The routing layer under audit consists of exactly these files inside `WORKDIR`:

1. `AGENTS.md`
2. `docs/repo_map.json`
3. `docs/task_routes.json`
4. `docs/pitfalls.json`

---

## 0. Working Directory Boundary Rule (Highest Priority)

For this task, the audit root is **exactly `WORKDIR`**.

All reading and analysis must obey these rules:

* Treat `WORKDIR` as the **hard boundary** of the task.
* Audit only the routing-layer files that exist **inside `WORKDIR`**.
* **Do not** read, inspect, search, list, compare against, or infer from:
  * any parent directory of `WORKDIR`
  * any sibling directory of `WORKDIR`
  * any external directory outside `WORKDIR`
* **Do not** use `git rev-parse`, `find ..`, `rg ..`, or any equivalent method to discover a broader repo root.
* **Do not** climb upward to look for another `AGENTS.md`, `docs/`, `.git`, `pyproject.toml`, or any other “more complete” routing layer outside `WORKDIR`.
* If one of the expected routing-layer files is missing inside `WORKDIR`, report it as **missing in scope**. Do **not** search elsewhere for a substitute.
* If a symlink resolves outside `WORKDIR`, treat it as **out of scope** and do not follow it.

This boundary rule overrides all repo-root heuristics, monorepo assumptions, or Git-based discovery methods.

---

## I. Hard Constraints

The following actions are strictly prohibited:

* **Do not modify** any files
* **Do not create** or delete any files
* **Do not output** patches, diffs, or `apply_patch` content
* **Do not rewrite** document bodies
* **Do not propose** finalized replacement documents
* **Do not read any file outside `WORKDIR`**
* **Do not broaden the audit to implementation code, tests, configs, schemas, or general docs**

Your mandate is limited to:

1. Reading the existing routing-layer files inside `WORKDIR`
2. Analyzing their structure, ownership boundaries, machine-compatibility, redundancy, and routing value
3. Outputting audit conclusions and strategic improvement directions

If an adjustment is needed, describe only:
* **Problem**
* **Suggested Direction**

Do **not** provide rewritten replacement text.

---

## II. Audit Scope

Audit only these routing-layer files **inside `WORKDIR`**:

1. `AGENTS.md`
2. `docs/repo_map.json`
3. `docs/task_routes.json`
4. `docs/pitfalls.json`

If any file is missing inside `WORKDIR`, report it as **missing in scope** and continue with the files that do exist.

---

## III. Audit Goal

Determine whether the routing layer inside `WORKDIR` actually behaves like a **strict AI-to-AI, machine-first routing system**.

You are evaluating whether the system supports:

* **Machine Routing:** Can downstream agents narrow from task to likely modules/files quickly?
* **Structured Ownership:** Are routing facts owned by structured artifacts rather than prose?
* **Boundary Control:** Are file responsibilities sharply separated?
* **Low Drift Risk:** Does the system avoid duplicated truth sources?
* **Operational Usefulness:** Can an agent extract first-read files, likely task targets, risk cues, and escalation conditions efficiently?
* **Protocol Separation:** Is `AGENTS.md` acting only as protocol, not as a knowledge base?
* **Fact Discipline:** Does the routing layer avoid acting like implementation truth, encyclopedia, or detailed design?
* **Machine Friendliness:** Is the system optimized for programmatic consumption rather than human reading?

---

## IV. Expected File Roles

### 1. `AGENTS.md`

Expected role:

* protocol only
* required read order
* scope boundary rules
* hard rules
* edit safety rules
* verification discipline
* output discipline

It should **not** become:

* module catalog
* task routing guide
* pitfall database
* architecture explanation
* project overview

### 2. `docs/repo_map.json`

Expected role:

* only structured module-level routing source of truth

It should own:

* module IDs
* paths
* purposes
* entry files
* key symbols
* first-read files
* then-check files
* internal dependency edges
* out-of-scope dependency edges
* related tests/configs if present in routing design
* risk flags
* blast radius
* pitfall references
* minimum regression
* scope notes

It should **not** become:

* narrative architecture doc
* duplicate task-routing table
* prose encyclopedia

### 3. `docs/task_routes.json`

Expected role:

* only structured task-routing source of truth

It should own:

* task types
* match keywords
* first-read modules/files
* then-check files
* pitfall linkage
* expand-search conditions
* stop-and-verify conditions
* minimum regression hooks
* routing priority

It should **not** become:

* duplicate module catalog
* prose task tutorial
* risk database

### 4. `docs/pitfalls.json`

Expected role:

* only structured pitfall / hidden-risk source of truth

It should own:

* pitfall IDs
* severity
* symptoms
* trigger actions
* related paths
* affected modules
* blast radius
* checks before edit
* safe observation methods

It should **not** become:

* duplicate module map
* task-routing file
* prose essay collection

---

## V. Key Dimensions for Inspection

### 1. Role Clarity

Check whether each file stays inside its intended role.

### 2. Machine-First Design Quality

Assess whether the routing layer is truly machine-first.

Check for:

* structured ownership vs prose ownership
* stable IDs vs informal naming
* compact atomic fields vs long narrative values
* deterministic references vs vague description
* cross-referenceability vs isolated text
* downstream usability for agents and schedulers

### 3. Functional Duplication

Check whether multiple files are maintaining overlapping truth in these categories:

* module definitions
* task routing logic
* risk ownership
* first-read guidance
* dependency boundary descriptions

### 4. Content Redundancy

Check whether content is repeated across files without increasing routing value.

### 5. Navigational Efficiency for Agents

Check whether a downstream agent can quickly determine:

* what to read first
* where likely changes belong
* what risks apply
* when to expand search
* when to stop and verify
* what remains out of scope

### 6. Over-Documentation

Check whether any file has drifted from routing/protocol into explanation-heavy documentation.

### 7. Drift Risk and Maintenance Fragility

Assess whether the current structure is likely to drift because of:

* duplicated ownership
* weak cross-references
* unstable naming
* hidden parallel truth sources
* prose fields carrying structured facts

---

## VI. Audit Methodology

Perform the audit in this order:

1. Identify the **intended role** of each file
2. Identify the **actual role** it is currently playing
3. Check whether that role is compatible with a strict AI-to-AI routing system
4. Locate overlaps, conflicts, and ownership violations
5. Distinguish necessary minimal redundancy from wasteful duplication
6. Assess long-term drift risk
7. Judge whether the system is structurally fit for downstream machine use

Do **not** judge correctness against code or implementation.
Only inspect the architecture of the routing-layer file system itself.

---

## VII. Evaluation Standard

A strong machine-first routing layer should have these properties:

* structured files own routing facts
* module/task/risk ownership is centralized
* references are stable and machine-usable
* protocol is separate from routing knowledge
* fields are concise and operational
* downstream agents can act with low ambiguity
* drift risk is low
* the system stays inside navigation/routing scope

A weak system will show one or more of these symptoms:

* `AGENTS.md` owning knowledge instead of protocol
* weak or missing stable IDs
* routes duplicated in multiple places
* risks duplicated in multiple places
* JSON files containing long prose blocks
* weak cross-reference discipline
* missing or ambiguous ownership boundaries
* machine-unfriendly structure despite JSON file extensions

---

## VIII. Output Contract

Output must be optimized for **AI-to-AI consumption**, not for human readability.

Use:

* short, high-density bullet lines
* compact labels
* stable section headers
* direct evidence references
* low prose / low filler

Do **not** use:

* essay paragraphs
* human-friendly explanations
* polite filler
* narrative summaries
* long recommendations

Required structure:

## File-Based Binary Gate (mandatory)

This audit is a **hard binary gate** for the outer orchestrator, but the result must be written to files instead of inline terminal prose.

You must end in exactly one of two final states:

* `审核通过`
* `审核未通过`

Use this strict rule:

* `审核通过` is allowed only when the routing layer is already directly usable **without any file changes**
* `审核未通过` is required if **any** structural problem, ambiguity, missing area, ownership drift, wasteful duplication, or actionable fix remains

More specifically:

* `审核通过` is allowed only if all of the following are true:
  * verdict = `strong`
  * recommendation = `use_as_is`
  * there are **no** `- finding:` bullets
  * there are **no** `- missing:` bullets
  * there are **no** `- boundary_conflict:` bullets
  * there are **no** `- duplication:` bullets with `value=wasteful`
* Otherwise you must use `审核未通过`

Do **not** declare `审核通过` merely because the system is “mostly usable”.
If there is still anything that should be structurally refined, the result is `审核未通过`.

Output form is also binary:

* If final result is `审核通过`, overwrite the audit record file with a minimal pass record only.
* If final result is `审核未通过`, write the final structured audit bullets to the audit record file.
* In all cases, the machine status JSON is the single source of truth for pass/fail.
* Do **not** emit final PASS/REVISE decision prose directly to stdout.

### 1. `- verdict`

Exactly one bullet:

* `- verdict: strong | usable_but_drift_prone | weak | fundamentally_misdesigned`

### 2. `- file_role`

One bullet per in-scope file:

* `- file_role: file=<path> | presence=present|missing_in_scope | intended=<role> | actual=<role> | boundary=clear|partial_overlap|major_conflict | machine=high|medium|low | note=<short>`

### 3. `- finding`

One bullet per finding, ordered by severity:

* `- finding: severity=critical|high|medium|low | title=<short> | problem=<short> | impact=<short> | evidence=<path[:line]|...> | direction=<short structural fix>`

### 4. `- duplication`

One bullet per duplicated topic:

* `- duplication: topic=<short> | owner=<file> | overlaps=<file1,file2,...> | value=necessary|wasteful | note=<short>`

### 5. `- boundary_conflict`

One bullet per boundary violation:

* `- boundary_conflict: file=<path> | violation=<short> | impact=<short>`

### 6. `- missing`

One bullet per missing / under-specified structural area:

* `- missing: area=<short> | effect=<short> | direction=<short>`

### 7. `- recommendation`

Exactly one bullet:

* `- recommendation: use_as_is | minor_structural_fixes | major_structural_redesign`

### 8. `- top_priority`

Exactly three bullets:

* `- top_priority: <short>`

Important:

* The bullet structure above applies only to the `审核未通过` branch.
* The `审核通过` branch should leave only a minimal pass record in the audit record file.

---

## IX. Style Requirements

* Audit-only
* No rewriting
* No patches
* No scope expansion
* Blunt conclusions
* Structure over wording
* Machine-first critique
* High density
* No polite filler

If a required file is missing inside `WORKDIR`, say so plainly and continue.

---

## X. Final Scope Reminder

You are auditing a **machine-first routing-layer system**, not the codebase.

Remain:

* strictly inside `WORKDIR`
* strictly inside the 4 listed routing-layer files
* strictly focused on routing architecture
* strictly focused on whether the system is genuinely AI-to-AI and machine-first

If the system is still human-oriented, say so.
If ownership is broken, say where.
If structured truth is weak, say how.
If files are missing, report them.
Do **not** climb outside the assigned working directory for context.

**Begin the audit now. Focus on whether the routing layer inside `WORKDIR` is a coherent, low-drift, machine-first AI-to-AI system.**"""
    return routing_layer_file_audit_prompt


# 路由层文件优化,  由 [路由创建器] 执行
@agent_prompt(
    prompt_id="a01.routing.refine",
    stage="a01",
    role="routing_agent",
    intent="refine_routing_layer",
    mode="a01_routing_refine",
    files={
        "audit_record": FileSpec(path_arg="audit_record_path", access=ACCESS_READ, change=CHANGE_NONE),
        "agents_md": FileSpec(path_arg="agents_md", access=ACCESS_WRITE, change=CHANGE_MUST_CHANGE, cleanup=CLEANUP_SYSTEM_BEFORE_STAGE_OR_RETRY, special=SPECIAL_STAGE_ARTIFACT),
        "repo_map_json": FileSpec(path_arg="repo_map_json", access=ACCESS_WRITE, change=CHANGE_MUST_CHANGE, cleanup=CLEANUP_SYSTEM_BEFORE_STAGE_OR_RETRY, special=SPECIAL_STAGE_ARTIFACT),
        "task_routes_json": FileSpec(path_arg="task_routes_json", access=ACCESS_WRITE, change=CHANGE_MUST_CHANGE, cleanup=CLEANUP_SYSTEM_BEFORE_STAGE_OR_RETRY, special=SPECIAL_STAGE_ARTIFACT),
        "pitfalls_json": FileSpec(path_arg="pitfalls_json", access=ACCESS_WRITE, change=CHANGE_MUST_CHANGE, cleanup=CLEANUP_SYSTEM_BEFORE_STAGE_OR_RETRY, special=SPECIAL_STAGE_ARTIFACT),
    },
    outcomes={"completed": OutcomeSpec(status="completed", requires=("agents_md", "repo_map_json", "task_routes_json", "pitfalls_json"))},
)
def routing_layer_refine(
        audit_record_path='路由层审核记录.md',
        agents_md="AGENTS.md",
        repo_map_json="docs/repo_map.json",
        task_routes_json="docs/task_routes.json",
        pitfalls_json="docs/pitfalls.json",
):
    routing_layer_refine_prompt = f"""读取审核记录文件《{audit_record_path}》, 基于其中的最终审核意见优化路由层。

要求:
1. 只依据《{audit_record_path}》中的审核意见做本轮修复
2. 只允许修改当前工作目录内的 4 个路由层文件
3. 不要修改《{audit_record_path}》本身
4. 不要启动 subagent
5. 不要自行继续审核
6. 不要进入循环

系统会在你完成后重新执行 audit, 并重写审核记录文件。"""
    routing_layer_refine_prompt += f"""

## 本轮必须修改的路由层文件
如果上文出现默认文件名，本段路径优先级最高：
- AGENTS instructions: `{agents_md}`
- module facts: `{repo_map_json}`
- task routes: `{task_routes_json}`
- pitfalls: `{pitfalls_json}`
"""
    return routing_layer_refine_prompt


@wraps_prompt(create_routing_layer_file)
def build_create_prompt(
        agents_md="AGENTS.md",
        repo_map_json="docs/repo_map.json",
        task_routes_json="docs/task_routes.json",
        pitfalls_json="docs/pitfalls.json",
) -> str:
    return create_routing_layer_file(
        agents_md=agents_md,
        repo_map_json=repo_map_json,
        task_routes_json=task_routes_json,
        pitfalls_json=pitfalls_json,
    ).strip()


@agent_prompt(
    prompt_id="a01.routing.audit",
    stage="a01",
    role="routing_agent",
    intent="audit_routing_layer",
    mode="a01_routing_audit",
    files={
        "routing_audit_status": FileSpec(path_arg="routing_audit_status_file", access=ACCESS_WRITE, change=CHANGE_MUST_CHANGE, cleanup=CLEANUP_SYSTEM_BEFORE_STAGE_OR_RETRY),
        "routing_audit_record": FileSpec(path_arg="routing_audit_record_file", access=ACCESS_WRITE, change=CHANGE_MUST_CHANGE, cleanup=CLEANUP_SYSTEM_BEFORE_STAGE_OR_RETRY),
    },
    outcomes={
        "review_pass": OutcomeSpec(status="review_pass", requires=("routing_audit_status", "routing_audit_record"), special=SPECIAL_REVIEW_PASS),
        "review_fail": OutcomeSpec(status="review_fail", requires=("routing_audit_status", "routing_audit_record"), special=SPECIAL_REVIEW_FAIL),
    },
)
def build_audit_prompt(
        *,
        audit_round: int,
        routing_audit_status_file: str,
        routing_audit_record_file: str,
        routing_audit_status_pass: str,
        routing_audit_status_fail: str,
) -> str:
    base_prompt = routing_layer_file_audit() if callable(routing_layer_file_audit) else str(routing_layer_file_audit)
    return f"""{str(base_prompt).strip()}

Audit file output requirements:
- This is audit round `{int(audit_round)}`.
- You must write the detailed audit record into `{routing_audit_record_file}` in the current working directory.
- You must write the machine status file into `{routing_audit_status_file}` in the current working directory.
- In `{routing_audit_status_file}`, write valid JSON with exactly these required fields:
{{
  "schema_version": "1.0",
  "stage": "routing_layer_audit",
  "audit_round": {int(audit_round)},
  "status": "{routing_audit_status_pass}" | "{routing_audit_status_fail}",
  "review_record_path": "{routing_audit_record_file}"
}}
- Machine outcome mapping:
  - `review_pass` means JSON `status` must be `{routing_audit_status_pass}`.
  - `review_fail` means JSON `status` must be `{routing_audit_status_fail}`.
- If the audit passes, `{routing_audit_record_file}` must still be overwritten with a minimal record that states the pass result.
- If the audit fails, write the final AI-to-AI bullet audit into `{routing_audit_record_file}`.
- `{routing_audit_status_file}` is the single source of truth for pass/fail.
"""


@agent_prompt(
    prompt_id="a01.routing.refine_wrapped",
    stage="a01",
    role="routing_agent",
    intent="refine_routing_layer",
    mode="a01_routing_refine",
    files={
        "audit_record": FileSpec(path_arg="audit_record_path", access=ACCESS_READ, change=CHANGE_NONE),
        "routing_audit_status": FileSpec(path_arg="routing_audit_status_file", access=ACCESS_READ, change=CHANGE_NONE),
        "agents_md": FileSpec(path_arg="agents_md", access=ACCESS_WRITE, change=CHANGE_MUST_CHANGE, cleanup=CLEANUP_SYSTEM_BEFORE_STAGE_OR_RETRY, special=SPECIAL_STAGE_ARTIFACT),
        "repo_map_json": FileSpec(path_arg="repo_map_json", access=ACCESS_WRITE, change=CHANGE_MUST_CHANGE, cleanup=CLEANUP_SYSTEM_BEFORE_STAGE_OR_RETRY, special=SPECIAL_STAGE_ARTIFACT),
        "task_routes_json": FileSpec(path_arg="task_routes_json", access=ACCESS_WRITE, change=CHANGE_MUST_CHANGE, cleanup=CLEANUP_SYSTEM_BEFORE_STAGE_OR_RETRY, special=SPECIAL_STAGE_ARTIFACT),
        "pitfalls_json": FileSpec(path_arg="pitfalls_json", access=ACCESS_WRITE, change=CHANGE_MUST_CHANGE, cleanup=CLEANUP_SYSTEM_BEFORE_STAGE_OR_RETRY, special=SPECIAL_STAGE_ARTIFACT),
    },
    outcomes={"completed": OutcomeSpec(status="completed", requires=("agents_md", "repo_map_json", "task_routes_json", "pitfalls_json"))},
)
def build_refine_prompt(
        audit_record_path: str | Path,
        *,
        routing_audit_status_file: str,
        agents_md="AGENTS.md",
        repo_map_json="docs/repo_map.json",
        task_routes_json="docs/task_routes.json",
        pitfalls_json="docs/pitfalls.json",
) -> str:
    base_prompt = routing_layer_refine(
        str(audit_record_path),
        agents_md=agents_md,
        repo_map_json=repo_map_json,
        task_routes_json=task_routes_json,
        pitfalls_json=pitfalls_json,
    ) if callable(routing_layer_refine) else str(routing_layer_refine)
    return f"""{str(base_prompt).strip()}

执行约束:
- 只允许修改当前工作目录内的 4 个路由层文件。
- 必须读取 `{audit_record_path}` 作为本轮唯一审核依据。
- 不要修改 `{audit_record_path}` 或 `{routing_audit_status_file}`。
- 这一次调用只做单次修复，不要启动 subagent，不要自发继续审核，不要进入循环。
- 下一轮审核由外层 Python 编排器负责。
- 如果审核意见与现有文件冲突，以审核记录中可执行且在 scope 内的结构修复为准。
"""


if __name__ == '__main__':
    print(create_routing_layer_file())
    # print(routing_layer_file_audit())
