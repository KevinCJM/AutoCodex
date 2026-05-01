# TmuxCodingTeam

TmuxCodingTeam 是一个本地运行的多智能体自动化开发编排工具。它用 Python 串联需求、设计、任务拆分、开发和复核流程，用 tmux 承载长期运行的 coding agent 会话，并提供 OpenTUI 终端界面和 Web 控制台两种交互入口。

这个仓库已经扁平化为当前项目根目录；以下说明只描述当前仓库内可见代码。运行时依赖的外部 agent CLI、认证、代理、目标项目目录等环境能力不在仓库内。

## 核心能力

- 从项目目录开始生成或校验机器优先的路由层：`AGENTS.md`、`docs/repo_map.json`、`docs/task_routes.json`、`docs/pitfalls.json`。
- 录入需求，支持文本、文件和 Notion 输入方式。
- 由需求分析师 agent 生成需求澄清文档，并通过 HITL 文件协议向人类追问。
- 并行启动多个评审 agent，对需求澄清、详细设计、任务单、代码修改和整体代码进行评审。
- 将详细设计拆成可跟踪的任务单 Markdown 和 JSON 进度文件。
- 用 tmux 长会话驱动开发 agent 执行任务，并在任务级别做评审、修复和状态更新。
- 支持阶段回退、运行时恢复、worker 重建、模型/厂商选择、代理配置和评审轮次限制。
- 提供 OpenTUI 终端 UI、Web 控制台、legacy Python CLI 三种使用形态。

## 当前已实现流程

| 阶段 | 入口/动作 | 主要产物 |
| --- | --- | --- |
| A01 路由初始化 | `A01_Routing_LayerPlanning.py` | `AGENTS.md`、`docs/repo_map.json`、`docs/task_routes.json`、`docs/pitfalls.json` |
| A02 需求录入 | `A02_RequirementIntake.py` | `{需求名}_原始需求.md` |
| A03 需求澄清 | `A03_RequirementsClarification.py` | `{需求名}_需求澄清.md`、`{需求名}_与人类交流.md`、`{需求名}_人机交互澄清记录.md` |
| A04 需求评审 | `A04_RequirementsReview.py` | `{需求名}_需求评审记录.md`、`{需求名}_需求评审记录_{评审者}.md`、`{需求名}_评审记录_{评审者}.json` |
| A05 详细设计 | `A05_DetailedDesign.py` | `{需求名}_详细设计.md`、`{需求名}_详设评审记录.md` |
| A06 任务拆分 | `A06_TaskSplit.py` | `{需求名}_任务单.md`、`{需求名}_任务单.json`、`{需求名}_任务单评审记录.md` |
| A07 任务开发 | `A07_Development.py` | `{需求名}_工程师开发内容.md`、`{需求名}_代码评审记录.md`、任务单 JSON 进度更新 |
| A08 整体复核 | `A08_OverallReview.py` | `{需求名}_整体代码复核记录.md`、`{需求名}_复核阶段状态.json` |

`A00_main_tui.py` 是当前串联 A01 到 A08 的总入口。A08 后的测试、复利、提交代码、提交 PR 仍是占位阶段。

## 目录结构

```text
.
├── A00_main_tui.py              # 总调度入口，默认会尝试启动 OpenTUI
├── A00_main_web.py              # Web 控制台一键启动入口
├── A01_*.py ... A08_*.py        # 各阶段兼容入口/阶段入口
├── T01_*.py ... T12_*.py        # 共享工具、运行时、桥接、终端协议
├── Prompt_*.py                  # 业务提示词文件，受保护，未明确允许不要修改
├── tmux_core/
│   ├── workflow/                # 总流程编排实现
│   ├── stage_kernel/            # 各阶段核心实现
│   ├── runtime/                 # tmux worker、任务结果协议、模型厂商目录
│   ├── bridge/                  # TUI/Web 后端桥接
│   └── prompt_contracts/        # 各阶段输出契约和校验逻辑
├── packages/
│   ├── tui/                     # Bun + Solid + OpenTUI 终端 UI
│   └── web/                     # Bun + Vite + Solid Web 控制台
├── docs/                        # 机器优先路由层事实源
├── scripts/tmux-tui             # OpenTUI 启动脚本
└── tests/                       # Python 回归测试
```

顶层部分文件是兼容入口，会通过 `tmux_core.compat.alias_module()` 映射到 `tmux_core` 内实现。改代码前要先追踪真实实现文件，不要只看顶层文件名。

## 环境要求

- macOS 或可用 tmux 的 Unix-like 环境。
- Python 3.9+。当前本地验证环境为 Python 3.9.13。
- tmux。
- Bun，用于 `packages/tui` 和 `packages/web`。
- 至少一个可用的 agent CLI：`codex`、`claude`、`gemini` 或 `opencode`。
- 对应 agent CLI 的登录状态、API 认证和网络代理。
- 可选：Node.js。部分厂商模型探测会读取 Node 包元数据。

仓库没有 Python 依赖清单文件；运行时代码主要使用标准库，测试需要 `pytest`。如果本机没有 pytest：

```bash
python3 -m pip install pytest
```

前端依赖分别安装：

```bash
cd packages/tui
bun install --frozen-lockfile

cd ../web
bun install --frozen-lockfile
```

也可以让启动脚本自动安装缺失依赖：OpenTUI 和 Web 启动逻辑都会在发现依赖缺失时执行 `bun install --frozen-lockfile`。

## 快速开始

### 1. 启动 Web 控制台

```bash
python3 A00_main_web.py
```

默认行为：

- 启动 Python WebBackend：`http://127.0.0.1:8765`
- 启动 Vite 前端：`http://127.0.0.1:5173`
- 前端通过 Vite proxy 访问 `/api/*`、`/healthz`
- 启动完成后在浏览器打开 `http://127.0.0.1:5173`

可用参数：

```bash
python3 A00_main_web.py --skip-install
python3 A00_main_web.py --backend-port 8765 --web-port 5173
```

当前 Web 配置固定代理到 `127.0.0.1:8765`，前端固定端口 `5173`。

### 2. 启动总工作流

交互式终端中直接运行：

```bash
python3 A00_main_tui.py
```

当 stdin/stdout 是 TTY 且没有传入参数时，它会启动 `scripts/tmux-tui` 进入 OpenTUI。要显式使用 legacy Python CLI：

```bash
python3 A00_main_tui.py --no-tui --legacy-cli
```

常用非交互参数：

```bash
python3 A00_main_tui.py \
  --project-dir /absolute/path/to/target-project \
  --requirement-name 新需求 \
  --main-agent vendor=codex,model=gpt-5.4,effort=high \
  --reviewer-agent name=R1,vendor=codex,model=gpt-5.4-mini,effort=medium \
  --requirements-review-max-rounds 5 \
  --detailed-design-review-max-rounds 5 \
  --task-split-review-max-rounds 5 \
  --development-review-max-rounds 5
```

需要跳过 A08 整体复核时：

```bash
python3 A00_main_tui.py --skip-overall-review
```

### 3. 直接运行某个阶段

每个阶段都可以独立启动，适合恢复、调试或只处理某个产物：

```bash
python3 A01_Routing_LayerPlanning.py --project-dir /absolute/path/to/project
python3 A02_RequirementIntake.py --project-dir /absolute/path/to/project --requirement-name 新需求
python3 A03_RequirementsClarification.py --project-dir /absolute/path/to/project --requirement-name 新需求
python3 A04_RequirementsReview.py --project-dir /absolute/path/to/project --requirement-name 新需求
python3 A05_DetailedDesign.py --project-dir /absolute/path/to/project --requirement-name 新需求
python3 A06_TaskSplit.py --project-dir /absolute/path/to/project --requirement-name 新需求
python3 A07_Development.py --project-dir /absolute/path/to/project --requirement-name 新需求
python3 A08_OverallReview.py --project-dir /absolute/path/to/project --requirement-name 新需求
```

多数阶段支持：

- `--vendor codex|claude|gemini|opencode`
- `--model <model>`
- `--effort low|medium|high|xhigh|max`
- `--proxy-url <port-or-url>` 或路由阶段的 `--proxy-port`
- `--reviewer-agent name=<key>,vendor=...,model=...,effort=...,proxy=...`
- `--review-max-rounds <number|infinite>`
- `--yes`
- `--no-tui`
- `--legacy-cli`

## Agent 配置

`--main-agent` 和 `--reviewer-agent` 使用逗号分隔的 `key=value` 字符串：

```bash
--main-agent vendor=codex,model=gpt-5.4,effort=high,proxy=10809
--reviewer-agent name=架构师,vendor=claude,model=sonnet,effort=high
--reviewer-agent name=测试工程师,vendor=gemini,model=flash,effort=medium
```

也可以把配置写入 JSON 文件，通过 `--agent-config` 传入。全局配置会作为默认值，`stages.<stage_key>` 可以覆盖单个阶段：

```json
{
  "main": {
    "vendor": "codex",
    "model": "gpt-5.4",
    "effort": "high"
  },
  "reviewers": [
    {
      "name": "R1",
      "vendor": "codex",
      "model": "gpt-5.4-mini",
      "effort": "medium"
    }
  ],
  "stages": {
    "development": {
      "main": {
        "vendor": "gemini",
        "model": "flash",
        "effort": "medium",
        "proxy": "10809"
      },
      "reviewers": [
        {
          "name": "代码评审",
          "vendor": "opencode",
          "model": "default",
          "effort": "high"
        }
      ]
    }
  }
}
```

当前总入口使用的阶段 key 包括：

- `routing`
- `requirements_clarification`
- `requirements_review`
- `detailed_design`
- `task_split`
- `development`
- `overall_review`

命令行 `--main-agent`、`--reviewer-agent` 优先级高于 `--agent-config` 文件。

## 运行时文件和状态

目标项目目录中会出现阶段产物和运行时目录。常见文件：

```text
{需求名}_原始需求.md
{需求名}_需求澄清.md
{需求名}_与人类交流.md
{需求名}_人机交互澄清记录.md
{需求名}_需求评审记录.md
{需求名}_详细设计.md
{需求名}_详设评审记录.md
{需求名}_任务单.md
{需求名}_任务单.json
{需求名}_任务单评审记录.md
{需求名}_工程师开发内容.md
{需求名}_代码评审记录.md
{需求名}_整体代码复核记录.md
{需求名}_复核阶段状态.json
{需求名}_开发前期.json
```

常见运行时目录：

```text
.routing_init_runtime/
.requirements_analysis_runtime/
.requirements_review_runtime/
.detailed_design_runtime/
.task_split_runtime/
.development_runtime/
.tmux_workflow/
```

这些目录用于保存 worker 状态、turn 状态、任务结果、失败记录和恢复信息。不要手工删除正在运行的需求对应 runtime，除非明确要放弃恢复。

## Web 和 TUI 桥接

Python 桥接层在 `tmux_core/bridge`：

- `T11_tui_backend.py` 是 OpenTUI stdio 后端兼容入口。
- `T11_web_backend.py` 是 Web HTTP/SSE 后端兼容入口。
- `tmux_core/bridge/backend.py` 负责统一 action 分发、快照构建、worker 控制、文件预览、prompt 响应、HITL 状态和运行时事件。
- `tmux_core/bridge/web_backend.py` 暴露本地 HTTP API。

Web 后端提供的主要接口：

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

Web 后端只允许绑定 `127.0.0.1`。

## 测试和校验

Python 测试：

```bash
python3 -m pytest
```

只跑关键边界测试：

```bash
python3 -m pytest tests/test_architecture_boundaries.py tests/test_runtime_contract_compat.py tests/test_t10_tui_protocol.py
```

TUI 测试：

```bash
cd packages/tui
bun test
bun run typecheck
```

Web 测试：

```bash
cd packages/web
bun test
bun run typecheck
bun run build
```

Web E2E：

```bash
cd packages/web
bun run test:e2e
```

`test_models.py` 是本机模型探测脚本，它依赖用户本机的外部 CLI/脚本，不属于稳定仓库回归测试入口。

## 开发约束

- `docs/repo_map.json`、`docs/task_routes.json`、`docs/pitfalls.json` 是机器优先路由事实源；README 只做人工说明，不替代这些文件。
- 修改业务代码前先读 `AGENTS.md`，并按路由文件选择真实实现路径。
- 不要把顶层兼容入口当成唯一实现事实，先追踪到 `tmux_core` 内模块。
- 未经明确允许，不要修改 `Prompt_*.py` 和 `tmux_core/prompt_contracts` 中的业务提示词内容。
- 不要修改 `packages/tui/node_modules/**` 或 `packages/web/node_modules/**`。
- 改桥接协议时同时检查 Python 后端、TUI/Web 客户端和协议测试。
- 改阶段完成逻辑时同时检查文件契约、JSON 写入、validator 和恢复路径。

## 常见问题

### 运行后没有进入 OpenTUI

只有在没有传参数、stdin/stdout 是交互式 TTY、且没有 `--no-tui`/`--legacy-cli` 时，总入口才会自动进入 OpenTUI。否则会走 Python CLI 参数流程。

### 提示缺少 Bun 或前端依赖

安装 Bun，然后在对应包目录执行：

```bash
bun install --frozen-lockfile
```

也可以重新运行入口，让启动逻辑自动安装缺失依赖。

### agent 无法启动或卡在认证

先确认对应 CLI 可直接在当前 shell 运行：

```bash
codex --help
claude --help
gemini --help
opencode --help
tmux -V
```

再确认 CLI 已登录、网络代理可用、模型名和 reasoning effort 被当前厂商支持。

### 如何清理异常 tmux 会话

优先通过 TUI/Web 控制台的 worker 控制能力停止或重启 worker。手工清理前先查看：

```bash
tmux ls
```

再按会话名清理：

```bash
tmux kill-session -t <session-name>
```

手工 kill 可能影响运行时恢复，应只处理确认已经废弃的会话。

### 如何判断一个需求是否已经完成

检查 `{需求名}_任务单.json` 中任务是否全部为 `true`，再检查 `{需求名}_复核阶段状态.json` 中 `passed` 是否为 `true`。同时保留各阶段评审记录，便于回溯。

