# TmuxCodingTeam

[English](README.md) | [简体中文](README.zh-CN.md)

TmuxCodingTeam is a local multi-agent software development orchestration tool. It uses Python to connect requirements, design, task splitting, implementation, and review workflows, uses tmux to host long-running coding-agent sessions, and provides both an OpenTUI terminal interface and a Web console.

The project is designed for heterogeneous coding-agent collaboration: Codex, Claude, Gemini, OpenCode, MiMo, Antigravity, and DevEco Code can work together through one auditable workflow from initial requirements analysis to final full-code review.

This repository has been flattened into the current project root. The documentation below only describes code visible in this repository. External agent CLIs, authentication, network proxies, target project directories, and account setup are runtime requirements and are not included in this repository.

## Who Should Use This

- Developers who already use Codex, Claude, Gemini, OpenCode, MiMo, Antigravity, or DevEco Code on real codebases and want to turn requirements, design, implementation, and review into a repeatable workflow.
- Maintainers of medium or large local projects who want multiple agents to play distinct roles, such as requirements analyst, architect, task splitter, developer, and code reviewer.
- Teams that want long-running coding-agent work to happen inside local tmux sessions while preserving stage artifacts, review records, recovery state, and audit logs.
- Developers researching multi-agent software engineering workflows, especially HITL, human confirmation, task JSON state, and recoverable execution.

## Core Capabilities

- Generate or validate machine-first routing artifacts for a target project: `AGENTS.md`, `docs/repo_map.json`, `docs/task_routes.json`, and `docs/pitfalls.json`.
- Capture requirements from text, files, or Notion inputs.
- Use a requirements analyst agent to produce a clarification document and ask the human follow-up questions through an HITL file protocol.
- Launch multiple reviewer agents in parallel to review requirements clarification, detailed design, task lists, code changes, and final code quality.
- Split detailed designs into trackable Markdown task lists and JSON progress files.
- Drive development agents through long-running tmux sessions, then perform task-level review, repair, and status updates.
- Build a project-local, code-only Graphify relationship graph and expose bounded static evidence to every supported agent vendor.
- Support stage rollback, runtime recovery, worker reconstruction, model/vendor selection, proxy configuration, and review-round limits.
- Provide three user interfaces: OpenTUI terminal UI, Web console, and legacy Python CLI.

## Implemented Workflow

| Stage | Entry point / action | Main artifacts |
| --- | --- | --- |
| A01 Routing initialization | `A01_Routing_LayerPlanning.py` | `AGENTS.md`, `docs/repo_map.json`, `docs/task_routes.json`, `docs/pitfalls.json` |
| A02 Requirement intake | `A02_RequirementIntake.py` | `{requirement}_原始需求.md` |
| A03 Requirement clarification | `A03_RequirementsClarification.py` | `{requirement}_需求澄清.md`, `{requirement}_与人类交流.md`, `{requirement}_人机交互澄清记录.md` |
| A04 Requirement review | `A04_RequirementsReview.py` | `{requirement}_需求评审记录.md`, reviewer-specific review Markdown and JSON files |
| A05 Detailed design | `A05_DetailedDesign.py` | `{requirement}_详细设计.md`, `{requirement}_详设评审记录.md` |
| A06 Task splitting | `A06_TaskSplit.py` | `{requirement}_任务单.md`, `{requirement}_任务单.json`, `{requirement}_任务单评审记录.md` |
| A07 Development | `A07_Development.py` | `{requirement}_工程师开发内容.md`, `{requirement}_代码评审记录.md`, updated task JSON progress |
| A08 Final review | `A08_OverallReview.py` | `{requirement}_整体代码复核记录.md`, `{requirement}_复核阶段状态.json` |

`A00_main_tui.py` is the main entry point that currently wires A01 through A08 together. Post-A08 testing, compounding improvement, committing code, and submitting PRs are still placeholder stages.

## Repository Layout

```text
.
├── A00_main_tui.py              # Main workflow entry, starts OpenTUI by default when possible
├── A00_main_web.py              # One-command Web console launcher
├── A01_*.py ... A08_*.py        # Stage compatibility entry points / stage entry points
├── T01_*.py ... T12_*.py        # Shared tools, runtime, bridge, and terminal protocol
├── Prompt_*.py                  # Business prompt files; protected unless explicitly changed
├── tmux_core/
│   ├── workflow/                # Main workflow orchestration
│   ├── stage_kernel/            # Core stage implementations
│   ├── runtime/                 # tmux workers, task result protocol, model/vendor catalog
│   ├── bridge/                  # TUI/Web backend bridge
│   └── prompt_contracts/        # Stage output contracts and validation logic
├── packages/
│   ├── tui/                     # Bun + Solid + OpenTUI terminal UI
│   └── web/                     # Bun + Vite + Solid Web console
├── docs/                        # Machine-first routing facts
├── scripts/
│   ├── tmux-tui                 # OpenTUI launcher
│   └── tmux-graphify            # Read-only Graphify setup/build/query wrapper
├── tools/graphify/              # Pinned, isolated Graphify tool manifest and lock
└── tests/                       # Python regression tests
```

Some top-level files are compatibility entry points and map into real implementations through `tmux_core.compat.alias_module()`. When changing code, trace the actual implementation inside `tmux_core` instead of relying only on top-level filenames.

## Requirements

- macOS or a Unix-like environment with tmux.
- Python 3.9+. The current local validation environment is Python 3.9.13.
- tmux.
- Bun for `packages/tui` and `packages/web`.
- At least one available agent CLI: `codex`, `claude`, `gemini`, `opencode`, `mimo`, `agy`, or `deveco` (DevEco Code).
- Login, API authentication, and network proxy setup for the selected agent CLI.
- Optional: Node.js. Some provider/model detection code reads Node package metadata.
- Optional: `uv` for installing the isolated, project-managed Graphify 0.9.27 environment. The main workflow never replaces a user-global Graphify installation.
- Ponytail requires no separate plugin, Node.js process, MCP server, or network access; the audited skills are bundled under `third_party/ponytail`.

## Installation

```bash
git clone https://github.com/KevinCJM/TmuxCodingTeam.git
cd TmuxCodingTeam

python3 -m pip install pytest

cd packages/tui
bun install --frozen-lockfile

cd ../web
bun install --frozen-lockfile

cd ../..
```

The repository does not currently include a Python dependency manifest. Runtime Python code mostly uses the standard library, and tests require `pytest`.

If `pytest` is missing:

```bash
python3 -m pip install pytest
```

Install frontend dependencies separately:

```bash
cd packages/tui
bun install --frozen-lockfile

cd ../web
bun install --frozen-lockfile
```

The OpenTUI and Web launch paths can also install missing frontend dependencies automatically by running `bun install --frozen-lockfile`.

## Quick Start

### 1. Start the Web console

```bash
python3 A00_main_web.py
```

Default behavior:

- Starts the Python WebBackend at `http://127.0.0.1:8765`.
- Starts the Vite frontend at `http://127.0.0.1:5173`.
- Proxies frontend `/api/*` and `/healthz` requests through Vite.
- Opens `http://127.0.0.1:5173` in the browser after startup.

Useful options:

```bash
python3 A00_main_web.py --skip-install
python3 A00_main_web.py --backend-port 8765 --web-port 5173
```

The current Web setup expects the backend at `127.0.0.1:8765` and the frontend at `5173`.

### 2. Start the main workflow

Run this in an interactive terminal:

```bash
python3 A00_main_tui.py
```

When stdin/stdout are TTYs and no arguments are passed, it starts `scripts/tmux-tui` and enters OpenTUI. To explicitly use the legacy Python CLI:

```bash
python3 A00_main_tui.py --no-tui --legacy-cli
```

Common non-interactive usage:

```bash
python3 A00_main_tui.py \
  --project-dir /absolute/path/to/target-project \
  --requirement-name new-feature \
  --ponytail-mode full \
  --requirements-mode standard \
  --graphify-mode auto \
  --main-agent vendor=codex,model=default,effort=high \
  --reviewer-agent name=R1,vendor=codex,model=default,effort=medium \
  --requirements-review-max-rounds 5 \
  --detailed-design-review-max-rounds 5 \
  --task-split-review-max-rounds 5 \
  --development-review-max-rounds 5
```

To skip the A08 final review stage:

```bash
python3 A00_main_tui.py --skip-overall-review
```

### 3. Minimal demo

The following demo runs routing initialization in a temporary project. Use it first to verify that Python, tmux, and your agent CLI environment are available:

```bash
mkdir -p /tmp/tmuxcodingteam-demo
cd /tmp/tmuxcodingteam-demo
git init
printf '# Demo\n' > README.md

cd /path/to/TmuxCodingTeam
python3 A01_Routing_LayerPlanning.py \
  --project-dir /tmp/tmuxcodingteam-demo \
  --vendor codex \
  --model default \
  --effort medium \
  --yes
```

After success, the demo project should contain:

```text
/tmp/tmuxcodingteam-demo/AGENTS.md
/tmp/tmuxcodingteam-demo/docs/repo_map.json
/tmp/tmuxcodingteam-demo/docs/task_routes.json
/tmp/tmuxcodingteam-demo/docs/pitfalls.json
```

To try the full interactive workflow:

```bash
python3 A00_main_tui.py \
  --project-dir /tmp/tmuxcodingteam-demo \
  --requirement-name demo-requirement \
  --main-agent vendor=codex,model=default,effort=medium
```

### 4. Run a single stage directly

Each stage can be started independently, which is useful for recovery, debugging, or processing a single artifact:

```bash
python3 A01_Routing_LayerPlanning.py --project-dir /absolute/path/to/project
python3 A02_RequirementIntake.py --project-dir /absolute/path/to/project --requirement-name new-feature
python3 A03_RequirementsClarification.py --project-dir /absolute/path/to/project --requirement-name new-feature
python3 A04_RequirementsReview.py --project-dir /absolute/path/to/project --requirement-name new-feature
python3 A05_DetailedDesign.py --project-dir /absolute/path/to/project --requirement-name new-feature
python3 A06_TaskSplit.py --project-dir /absolute/path/to/project --requirement-name new-feature
python3 A07_Development.py --project-dir /absolute/path/to/project --requirement-name new-feature
python3 A08_OverallReview.py --project-dir /absolute/path/to/project --requirement-name new-feature
```

Most stages support:

- `--vendor codex|claude|gemini|opencode|mimo|agy|deveco`
- `--model <model>`
- `--effort low|medium|high|xhigh|max`
- `--proxy-url <port-or-url>` or the routing-stage `--proxy-port`
- `--ponytail-mode off|lite|full|ultra`
- `--graphify-mode off|auto|required`
- A00/A03: `--requirements-mode standard|grill|grill-with-docs`
- `--reviewer-agent name=<key>,vendor=...,model=...,effort=...,proxy=...`
- `--review-max-rounds <number|infinite>`
- `--yes`
- `--no-tui`
- `--legacy-cli`

## Agent Configuration

`--main-agent` and `--reviewer-agent` use comma-separated `key=value` strings:

```bash
--main-agent vendor=codex,model=default,effort=high,proxy=10809
--reviewer-agent name=Architect,vendor=claude,model=sonnet,effort=high,ponytail=ultra
--reviewer-agent name=Tester,vendor=gemini,model=flash,effort=medium,ponytail_mode=lite
```

You can also write configuration into a JSON file and pass it with `--agent-config`. Global configuration acts as the default, and `stages.<stage_key>` can override a single stage:

```json
{
  "ponytail_mode": "full",
  "requirements_mode": "grill",
  "graphify_mode": "auto",
  "graphify": {
    "include": ["src/**", "tests/**"],
    "exclude": ["generated/**"],
    "max_workers": 1,
    "initial_timeout_sec": 120,
    "incremental_timeout_sec": 30
  },
  "main": {
    "vendor": "codex",
    "model": "default",
    "effort": "high"
  },
  "reviewers": [
    {
      "name": "R1",
      "vendor": "codex",
      "model": "default",
      "effort": "medium"
    }
  ],
  "stages": {
    "requirements_clarification": {
      "requirements_mode": "grill-with-docs"
    },
    "routing": {
      "graphify_mode": "required"
    },
    "development": {
      "main": {
        "vendor": "gemini",
        "model": "flash",
        "effort": "medium",
        "proxy": "10809",
        "ponytail_mode": "ultra"
      },
      "reviewers": [
        {
          "name": "CodeReview",
          "vendor": "opencode",
          "model": "default",
          "effort": "high",
          "ponytail": "lite"
        }
      ]
    }
  }
}
```

Current stage keys used by the main entry point:

- `routing`
- `requirements_clarification`
- `requirements_review`
- `detailed_design`
- `task_split`
- `development`
- `overall_review`

Command-line `--main-agent` and `--reviewer-agent` options take precedence over the `--agent-config` file.

## Bundled Ponytail Modes

Ponytail is an optional behavior profile, not an agent vendor. A new workflow selects it once and all main and review agents inherit that mode. Interactive choices are Full, Lite, Ultra, and Off; Full is the default for new, `--yes`, and non-interactive workflows. A role can override the workflow mode with `ponytail_mode=...` or the compatible `ponytail=...` key. Existing worker state without this field resumes as Off, so old sessions do not receive new instructions unexpectedly.

The first task in each new tmux session receives the complete selected rules. Later tasks receive a compact reminder. Explicit task instructions, repository rules, artifact contracts, completion protocols, safety, and permissions always take priority. The project also preserves the upstream `ponytail-review`, `ponytail-audit`, `ponytail-debt`, `ponytail-gain`, and `ponytail-help` skills for provenance, but this release does not expose or invoke them automatically.

The bundled source version, commit, license, and integrity hashes are recorded in `third_party/ponytail/UPSTREAM.json`. A user-installed Ponytail plugin is not required.

## Bundled Grill Requirements Modes

Grill is an optional A03 requirements-interview strategy, not an agent vendor. `standard` keeps the existing behavior, `grill` asks one decision question at a time with a recommended answer, and `grill-with-docs` additionally maintains controlled `CONTEXT.md` and ADR drafts. Standard is the default. Grill modes require an interactive human, so `--yes` and headless runs must use Standard.

The workflow injects a self-contained, vendor-neutral rules block through the shared tmux prompt path. Therefore Codex, Claude, Gemini, OpenCode, MiMo, AGY, and DevEco can all act as the A03 analyst without installing skills or using vendor-specific slash commands. Reviewers and development agents do not receive Grill instructions.

The first confirmed Grill turn in a tmux session receives the complete rules; later turns receive a compact reminder. A03 cannot complete until the human explicitly confirms shared understanding. In `grill-with-docs`, drafts stay in the requirement runtime and are atomically published only after that confirmation; ADR paths and numbers remain host-controlled. The pinned upstream commit, MIT license, and integrity hashes are recorded in `third_party/mattpocock-skills/UPSTREAM.json`.

## Project Code Graph With Graphify

Graphify is an optional project-level code relationship service, not an eighth coding-agent vendor. This integration is pinned to the Apache-2.0 `graphifyy==0.9.27` package and runs from an isolated Python 3.11 environment described by `tools/graphify/pyproject.toml` and `tools/graphify/uv.lock`. It does not upgrade or overwrite a Graphify executable already installed by the user.

`--graphify-mode` supports `off`, `auto`, and `required`. New workflows default to `auto`; restored legacy runner or worker state without the field remains `off`. CLI configuration takes precedence over `stages.<stage>.graphify_mode`, then the top-level `graphify_mode`. Graphify is project-level, so role-level overrides are rejected. In `auto`, missing tools or failed refreshes degrade to the existing routing workflow and can reuse a stale successful graph. In `required`, tool, build, or schema failures stop the stage before an agent is created.

Use the maintenance wrapper from the repository root:

```bash
scripts/tmux-graphify setup
scripts/tmux-graphify doctor
scripts/tmux-graphify status
scripts/tmux-graphify build --project /absolute/path/to/project
scripts/tmux-graphify query "callers of calculate_total"
scripts/tmux-graphify affected "calculate_total"
scripts/tmux-graphify path "HTTP handler" "calculate_total"
scripts/tmux-graphify prune
```

`setup` installs only the pinned managed environment under `$XDG_DATA_HOME/tmux_coding_team/tools/graphify/0.9.27/` (or `~/.local/share/...`) and requires an explicit command plus network access. It never runs Graphify platform installers, Git hooks, watch mode, MCP, or global graph commands. `doctor` verifies the exact version and CLI contract. The agent-facing wrapper permits only read operations such as `query`, `affected`, `path`, `explain`, and `god-nodes`.

Graph builds use `--code-only`, `--no-cluster`, and a controlled source snapshot. Symlinks are not followed, secret-like files and runtime/build/vendor directories are excluded, and bounded file/count/size limits fail explicitly instead of silently truncating input. Graphify subprocesses receive `GRAPHIFY_QUERY_LOG_DISABLE=1` and no model-provider credentials. Immutable generations live in the user cache (`$XDG_CACHE_HOME/tmux_coding_team/graphify/` or `~/.cache/...`); partial builds are staged and never replace the last valid graph. Neither the raw graph nor cache paths are exposed through TUI/Web snapshots.

Codex, Claude, Gemini, OpenCode, MiMo, AGY, and DevEco receive the same bounded Graphify evidence through the ordinary prompt chain and the same read-only command environment. No vendor needs a Graphify Skill, MCP server, hook, or plugin. Whenever evidence exists, the prompt requires the agent to read it first. Queries remain conditional: sufficiently complete `EXTRACTED` evidence makes them optional, while routing discovery, missing code facts/edges, uncovered implementation seeds, and changed-file reviews receive one concrete required `query`, `affected`, `path`, `explain`, or `god-nodes --top 10` command. The wrapper audit binds a successful query to the current runner, session, turn, evidence, graph fingerprint, and invocation digest without storing query arguments or output. Auto mode reminds once and then records a degraded result when the query receipt remains missing; Required mode keeps the worker alive and requests human recheck, explicit source-verified override, or termination. Graphify still provides navigation only, so agents must verify results against `AGENTS.md` when present, source, tests, and configuration.

Evidence is ranked with the current stage, role, task, stage-materialized AI Hermes routed paths, explicit symbols, and the actual A07 change set, and includes at most three executable query suggestions. The first confirmed Graphify turn in a tmux session receives the full guide; later turns receive only a self-contained reminder and task-specific suggestions. An unconfirmed or outcome-unknown submission does not latch the guide. A07 derives its task changes from controlled source manifests captured before and after development, and A08 consumes the accumulated ledger and refreshes after every developer repair, so pre-existing dirty files are not attributed to the requirement. For a manifest-confirmed deleted source path, evidence and the optional `affected <path> --previous` query use the previous immutable generation and mark every result `OLD_GENERATION/AMBIGUOUS` rather than presenting it as current code truth.

Every agent query performs a lightweight source-freshness assessment first. A stale or unknown graph may still answer from an immutable generation, but the result is visibly marked and must be checked against current source; agent queries never trigger a build. The default bounded text envelope has stable BEGIN/END markers, while `--format json` returns `tmux-graphify-query-result/1`. Absolute paths, local file/editor URIs, UNC paths, ANSI and control characters are sanitized, and stored output is capped at 6,000 characters. Query aggregation is isolated by stage, runner and tmux session generation, so reused sessions count in the current stage while superseded sessions cannot pollute it. TUI/Web expose only that stage aggregate and the latest command/freshness, never the question or result body.

Routing authority does not change: active code, tests, and configuration are implementation truth; `AGENTS.md`, `repo_map.json`, `task_routes.json`, and `pitfalls.json` remain the machine-routing authority; Graphify only supplies static navigation and impact candidates. A01 creation may consume that evidence, while A01 audit/refine turns intentionally disable it and stay within the four routing files. Dynamic imports, reflection, generated code, runtime configuration, and cross-service behavior always require direct code or runtime confirmation.

## Runtime Files And State

The target project directory will contain stage artifacts and runtime directories.

Common files:

```text
{requirement}_原始需求.md
{requirement}_需求澄清.md
{requirement}_与人类交流.md
{requirement}_人机交互澄清记录.md
{requirement}_需求评审记录.md
{requirement}_详细设计.md
{requirement}_详设评审记录.md
{requirement}_任务单.md
{requirement}_任务单.json
{requirement}_任务单评审记录.md
{requirement}_工程师开发内容.md
{requirement}_代码评审记录.md
{requirement}_整体代码复核记录.md
{requirement}_复核阶段状态.json
{requirement}_开发前期.json
```

Common runtime directories:

```text
.routing_init_runtime/
.requirements_analysis_runtime/
.requirements_review_runtime/
.detailed_design_runtime/
.task_split_runtime/
.development_runtime/
.tmux_workflow/
```

These directories store worker state, turn state, task results, failure records, and recovery metadata. Do not manually delete the runtime directory for an active requirement unless you explicitly want to abandon recovery.

## Web And TUI Bridge

The Python bridge layer lives under `tmux_core/bridge`:

- `T11_tui_backend.py` is the OpenTUI stdio backend compatibility entry point.
- `T11_web_backend.py` is the Web HTTP/SSE backend compatibility entry point.
- `tmux_core/bridge/backend.py` handles action dispatch, snapshot construction, worker control, file preview, prompt responses, HITL state, and runtime events.
- `tmux_core/bridge/web_backend.py` exposes the local HTTP API.
- Graphify is exposed only as optional project-level `snapshot.app.graphify` state and a sanitized evidence report preview; it does not add an endpoint or change the NDJSON protocol version.

Main Web backend endpoints:

- `GET /healthz`
- `GET /api/bootstrap`
- `GET /api/snapshots`
- `GET /api/prompt`
- `GET /api/agent-catalog`
- `GET /api/requirements?project_dir=...`
- `GET /api/file-preview?path=...`
- `GET /api/events`
- `POST /api/request`
- `POST /api/prompt-response`

The Web backend only binds to `127.0.0.1`.

## Testing And Validation

Python tests:

```bash
python3 -m pytest
```

Run key boundary tests only:

```bash
python3 -m pytest tests/test_architecture_boundaries.py tests/test_runtime_contract_compat.py tests/test_t10_tui_protocol.py
```

TUI tests:

```bash
cd packages/tui
bun test
bun run typecheck
```

Web tests:

```bash
cd packages/web
bun test
bun run typecheck
bun run build
```

Web E2E:

```bash
cd packages/web
bun run test:e2e
```

`test_models.py` is a local model probing script. It depends on external CLIs and user-local scripts, so it is not a stable repository regression test entry point.

## Development Constraints

- `docs/repo_map.json`, `docs/task_routes.json`, and `docs/pitfalls.json` are machine-first routing facts. README is human-facing documentation and does not replace those files.
- Read `AGENTS.md` before changing business code, then use the routing files to find the real implementation path.
- Do not treat top-level compatibility entry points as the only implementation facts. Trace into `tmux_core` first.
- Do not modify `Prompt_*.py` or `tmux_core/prompt_contracts` business prompts unless explicitly asked.
- Do not modify `packages/tui/node_modules/**` or `packages/web/node_modules/**`.
- When changing the bridge protocol, check the Python backend, TUI/Web clients, and protocol tests together.
- When changing stage completion logic, check file contracts, JSON writes, validators, and recovery paths together.

## Roadmap

- v0.1.x: Stabilize the A01-A08 workflow, keep README/license/release notes/minimal demo current, and make the basic local workflow easy for new users to run.
- v0.2.x: Improve Web console and OpenTUI runtime observability, including worker state, stage events, file preview, and failure recovery hints.
- v0.3.x: Improve release automation, test execution, and PR review support so Codex/Claude/Gemini/OpenCode/MiMo/Antigravity/DevEco Code can participate more reliably in maintenance workflows.
- v0.4.x: Add plugin-style agent provider configuration, more model/vendor adapters, and reusable workflow templates.
- Long term: Turn TmuxCodingTeam into an auditable, recoverable, extensible local multi-agent software maintenance toolkit.

## License

This project is licensed under the MIT License. See `LICENSE`.

## FAQ

### Why did it not enter OpenTUI after startup?

The main entry point enters OpenTUI only when no arguments are passed, stdin/stdout are interactive TTYs, and neither `--no-tui` nor `--legacy-cli` is provided. Otherwise, it uses the Python CLI argument flow.

### Bun or frontend dependencies are missing

Install Bun, then run:

```bash
bun install --frozen-lockfile
```

inside the corresponding package directory. You can also rerun the entry point and let the startup logic install missing dependencies automatically.

### Agent startup fails or gets stuck on authentication

First confirm that the selected CLI works directly in the current shell:

```bash
codex --help
claude --help
gemini --help
opencode --help
mimo --help
agy --help
deveco --help
tmux -V
```

Then confirm that the CLI is logged in, the network proxy is available, and the selected model name and reasoning effort are supported by the provider.

### How do I clean up abnormal tmux sessions?

Prefer stopping or restarting workers through the TUI/Web console. Before manually cleaning up, inspect sessions:

```bash
tmux ls
```

Then kill only the confirmed abandoned session:

```bash
tmux kill-session -t <session-name>
```

Manual kills may affect runtime recovery, so only use them for sessions you are sure are abandoned.

### How do I know whether a requirement is complete?

Check whether all tasks in `{requirement}_任务单.json` are `true`, then check whether `{requirement}_复核阶段状态.json` has `passed: true`. Keep all stage review records for traceability.
