# TmuxCodingTeam

[English](README.md) | [简体中文](README.zh-CN.md)

TmuxCodingTeam 是一个本地运行的多智能体自动化开发编排工具。它用 Python 串联需求、设计、任务拆分、开发和复核流程，用 tmux 承载长期运行的 coding agent 会话，并提供 OpenTUI 终端界面和 Web 控制台两种交互入口。

这个仓库已经扁平化为当前项目根目录；以下说明只描述当前仓库内可见代码。运行时依赖的外部 agent CLI、认证、代理、目标项目目录等环境能力不在仓库内。

## 谁适合用

- 正在用 Codex、Claude、Gemini、OpenCode、MiMo、Antigravity 或 DevEco Code 处理真实代码库，希望把需求、设计、开发和复核串成固定流程的开发者。
- 维护中大型本地项目，需要把多个 agent 拆成需求分析、架构评审、任务拆分、开发、代码复核等角色的维护者。
- 想在本机 tmux 会话里长期运行 coding agent，并保留阶段产物、评审记录、恢复状态和审计日志的团队。
- 想研究 multi-agent software engineering workflow 的开发者，尤其关注 HITL、人类确认、任务单 JSON 状态和可恢复执行。

## 核心能力

- 从项目目录开始生成或校验机器优先的路由层：`AGENTS.md`、`docs/repo_map.json`、`docs/task_routes.json`、`docs/pitfalls.json`。
- 录入需求，支持文本、文件和 Notion 输入方式。
- 由需求分析师 agent 生成需求澄清文档，并通过 HITL 文件协议向人类追问。
- 并行启动多个评审 agent，对需求澄清、详细设计、任务单、代码修改和整体代码进行评审。
- 将详细设计拆成可跟踪的任务单 Markdown 和 JSON 进度文件。
- 用 tmux 长会话驱动开发 agent 执行任务，并在任务级别做评审、修复和状态更新。
- 构建项目级、仅代码的 Graphify 关系图，并向所有已支持厂商提供有边界的静态证据。
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
├── scripts/
│   ├── tmux-tui                 # OpenTUI 启动脚本
│   └── tmux-graphify            # Graphify 只读安装/构建/查询包装器
├── tools/graphify/              # 固定版本、隔离的 Graphify 工具清单与锁文件
└── tests/                       # Python 回归测试
```

顶层部分文件是兼容入口，会通过 `tmux_core.compat.alias_module()` 映射到 `tmux_core` 内实现。改代码前要先追踪真实实现文件，不要只看顶层文件名。

## 环境要求

- macOS 或可用 tmux 的 Unix-like 环境。
- Python 3.9+。当前本地验证环境为 Python 3.9.13。
- tmux。
- Bun，用于 `packages/tui` 和 `packages/web`。
- 至少一个可用的 agent CLI：`codex`、`claude`、`gemini`、`opencode`、`mimo`、`agy` 或 `deveco`（DevEco Code）。
- 对应 agent CLI 的登录状态、API 认证和网络代理。
- 可选：Node.js。部分厂商模型探测会读取 Node 包元数据。
- 可选：`uv`，用于安装隔离的项目托管 Graphify 0.9.27 环境；主流程不会替换用户全局安装的 Graphify。
- Ponytail 无需另外安装插件、Node.js 进程、MCP 服务或联网；经审计的 Skill 已内置于 `third_party/ponytail`。

## 安装命令

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

需要跳过 A08 整体复核时：

```bash
python3 A00_main_tui.py --skip-overall-review
```

### 3. 最小 demo

下面的 demo 会在一个临时项目里跑路由初始化，适合先确认本机 Python、tmux 和 agent CLI 环境是否可用：

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

成功后，demo 项目中应出现：

```text
/tmp/tmuxcodingteam-demo/AGENTS.md
/tmp/tmuxcodingteam-demo/docs/repo_map.json
/tmp/tmuxcodingteam-demo/docs/task_routes.json
/tmp/tmuxcodingteam-demo/docs/pitfalls.json
```

如果你想直接体验完整交互流程，可以改为：

```bash
python3 A00_main_tui.py \
  --project-dir /tmp/tmuxcodingteam-demo \
  --requirement-name demo需求 \
  --main-agent vendor=codex,model=default,effort=medium
```

### 4. 直接运行某个阶段

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

- `--vendor codex|claude|gemini|opencode|mimo|agy|deveco`
- `--model <model>`
- `--effort low|medium|high|xhigh|max`
- `--proxy-url <port-or-url>` 或路由阶段的 `--proxy-port`
- `--ponytail-mode off|lite|full|ultra`
- `--graphify-mode off|auto|required`
- A00/A03：`--requirements-mode standard|grill|grill-with-docs`
- `--reviewer-agent name=<key>,vendor=...,model=...,effort=...,proxy=...`
- `--review-max-rounds <number|infinite>`
- `--yes`
- `--no-tui`
- `--legacy-cli`

## Agent 配置

`--main-agent` 和 `--reviewer-agent` 使用逗号分隔的 `key=value` 字符串：

```bash
--main-agent vendor=codex,model=default,effort=high,proxy=10809
--reviewer-agent name=架构师,vendor=claude,model=sonnet,effort=high,ponytail=ultra
--reviewer-agent name=测试工程师,vendor=gemini,model=flash,effort=medium,ponytail_mode=lite
```

也可以把配置写入 JSON 文件，通过 `--agent-config` 传入。全局配置会作为默认值，`stages.<stage_key>` 可以覆盖单个阶段：

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
          "name": "代码评审",
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

当前总入口使用的阶段 key 包括：

- `routing`
- `requirements_clarification`
- `requirements_review`
- `detailed_design`
- `task_split`
- `development`
- `overall_review`

命令行 `--main-agent`、`--reviewer-agent` 优先级高于 `--agent-config` 文件。

## 内置 Ponytail 模式

Ponytail 是可选的智能体行为模式，不是 coding vendor。新工作流只选择一次，所有主智能体和审核智能体继承该模式。交互选项依次为 Full、Lite、Ultra、Off；新工作流、`--yes` 和非交互运行默认 Full。角色配置可用 `ponytail_mode=...` 或兼容键 `ponytail=...` 单独覆盖。旧 worker state 缺少此字段时按 Off 恢复，避免旧会话被突然注入新规则。

每个新 tmux 会话的第一条任务会收到完整规则，后续任务只收到精简提醒。明确的任务要求、仓库规则、文件合同、完成协议、安全和权限要求始终优先。项目同时保留上游 `ponytail-review`、`ponytail-audit`、`ponytail-debt`、`ponytail-gain`、`ponytail-help` 五个辅助 Skill，但本期不提供 UI，也不会自动调用。

内置来源版本、提交、许可证与完整性哈希记录在 `third_party/ponytail/UPSTREAM.json`；用户无需安装 Ponytail 插件。

## 内置 Grill 需求澄清模式

Grill 是 A03 可选的需求访谈策略，不是 coding vendor。`standard` 保持现有行为；`grill` 每轮只询问一个人类决策并给出推荐答案；`grill-with-docs` 还会维护受控的 `CONTEXT.md` 和 ADR 草稿。默认使用 Standard。Grill 必须由人类逐题回答，因此 `--yes` 和无交互运行只能使用 Standard。

系统通过统一 tmux 提示词投递链注入自包含的厂商无关规则，所以 Codex、Claude、Gemini、OpenCode、MiMo、AGY、DevEco 都可以担任 A03 需求分析师，无需安装 Skill，也不会调用厂商专属斜杠命令。审核员和开发智能体不会收到 Grill 规则。

同一 tmux 会话首次确认提交的 Grill turn 收到完整规则，后续只收到精简提醒。人类没有明确确认“共享理解一致”前，A03 不会完成。`grill-with-docs` 的文档在访谈期间只保存在需求运行时目录，确认后才原子发布；ADR 路径和编号由系统控制。固定的上游提交、MIT 许可证和完整性哈希记录在 `third_party/mattpocock-skills/UPSTREAM.json`。

## Graphify 项目代码图谱

Graphify 是可选的项目级代码关系服务，不是第八个 coding agent 厂商。本项目固定兼容 Apache-2.0 许可的 `graphifyy==0.9.27`，并通过 `tools/graphify/pyproject.toml`、`tools/graphify/uv.lock` 使用隔离的 Python 3.11 环境；不会升级或覆盖用户已安装的 Graphify。

`--graphify-mode` 支持 `off`、`auto`、`required`。新工作流默认 `auto`；旧 runner/worker state 缺少字段时仍按 `off` 恢复。优先级依次为 CLI、`stages.<stage>.graphify_mode`、顶层 `graphify_mode`。Graphify 属于项目级配置，角色级覆盖会被拒绝。`auto` 在工具缺失或刷新失败时降级继续，并可复用上一份成功图；`required` 在工具、构图或 schema 失败时会在创建智能体前停止阶段。

在仓库根目录使用维护包装器：

```bash
scripts/tmux-graphify setup
scripts/tmux-graphify doctor
scripts/tmux-graphify status
scripts/tmux-graphify build --project /absolute/path/to/project
scripts/tmux-graphify query "calculate_total 的调用者"
scripts/tmux-graphify affected "calculate_total"
scripts/tmux-graphify path "HTTP handler" "calculate_total"
scripts/tmux-graphify prune
```

`setup` 只把固定版本安装到 `$XDG_DATA_HOME/tmux_coding_team/tools/graphify/0.9.27/`（未设置时为 `~/.local/share/...`），必须由用户显式执行且需要联网；不会运行 Graphify 平台安装器、Git Hook、watch、MCP 或 global graph 命令。`doctor` 会校验精确版本与 CLI 契约。智能体可见的包装器只允许 `query`、`affected`、`path`、`explain`、`god-nodes` 等只读操作。

构图固定使用 `--code-only`、`--no-cluster` 和受控源码快照：不跟随符号链接，排除凭据类文件、运行时目录、构建目录和 vendor 目录；文件大小、文件数和总输入上限超出时明确失败，不静默截断。Graphify 子进程固定使用 `GRAPHIFY_QUERY_LOG_DISABLE=1`，并移除模型厂商凭据。不可变 generation 存放在用户缓存 `$XDG_CACHE_HOME/tmux_coding_team/graphify/`（未设置时为 `~/.cache/...`）；失败的 staging 永远不会替换上一份有效图。TUI/Web 不暴露 raw graph、缓存路径或可执行文件路径。

Codex、Claude、Gemini、OpenCode、MiMo、AGY、DevEco 都通过普通提示词链获得同一份有边界的 Graphify 证据，并获得同一套只读命令环境；无需安装 Graphify Skill、MCP、Hook 或插件。只要本轮存在证据，提示词就会明确要求智能体先阅读。查询采用条件强制：`EXTRACTED` 证据充分时可不查；路由首次发现、代码事实或关系边缺失、实现种子未覆盖、真实改动评审等场景会给出一条无占位符的 `query`、`affected`、`path`、`explain` 或 `god-nodes --top 10` 必查命令。只读包装器用 runner、session、turn、evidence、graph fingerprint 和调用摘要核验真实执行，不保存查询参数或结果。Auto 模式漏查时提醒一次后记录降级并继续；Required 模式保留智能体现场，要求人工复检、明确按源码核验结果 override，或终止阶段。Graphify 始终只作导航，结论仍须回到 `AGENTS.md`（存在时）、源码、测试和配置核实。

证据会结合当前阶段、角色、任务、由阶段层按 AI Hermes 合同解析的路由路径、明确符号和 A07 实际改动，生成最多三条可直接执行的推荐查询。每个 tmux 会话首次确认提交的 Graphify turn 会收到完整使用指南，后续只保留自包含提醒和本轮建议；未确认或结果不确定的提交不会锁存指南。A07 使用任务开始前后的受控源码 manifest 记录真实改动，A08 使用累计账本，并在每次开发修复后刷新图谱，避免把项目启动前已有的 dirty 文件误算为本需求修改。对于 manifest 已确认删除的源码路径，证据和可选命令 `affected <路径> --previous` 会使用上一份不可变 generation，并将结果明确标记为 `OLD_GENERATION/AMBIGUOUS`，不会冒充当前代码事实。

五类智能体查询都会先轻量核对图谱与当前源码。图谱过期或新鲜度未知时仍可查询不可变 generation，但结果会明确标记 `stale/unknown`，要求回到当前源码核验，且不会由智能体命令自动触发构图。查询默认返回带 BEGIN/END marker 的有界文本，也支持 `--format json` 的 `tmux-graphify-query-result/1` 结果；绝对路径、本地文件/编辑器 URI、UNC 路径、ANSI 和控制字符都会被脱敏，保存的输出限制为 6,000 字符。查询统计按阶段、runner 和 tmux 会话 generation 隔离：复用会话会计入当前阶段，旧会话不能污染新阶段。TUI/Web 只展示该阶段查询次数与最近一次命令/新鲜度，不保存问题或结果正文。

事实优先级不变：当前代码、测试、配置是实现事实；`AGENTS.md`、`repo_map.json`、`task_routes.json`、`pitfalls.json`仍是机器路由权威；Graphify 只提供静态导航和影响面候选。A01 create 可以消费这些证据，A01 audit/refine 则会明确关闭 Graphify，并保持只审核或修改四个路由文件。动态 import、反射、代码生成、运行时配置与跨服务行为必须重新核查代码或运行证据。

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
- Graphify 只通过可选的项目级 `snapshot.app.graphify` 状态和脱敏证据报告预览展示；不新增 endpoint，也不升级 NDJSON 协议版本。

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

## 维护路线图

- v0.1.x：稳定 A01-A08 主流程，补齐 README、license、release notes 和最小 demo，保证新用户能在本地跑通基础流程。
- v0.2.x：完善 Web 控制台和 OpenTUI 的运行时可观测性，包括 worker 状态、阶段事件、文件预览和失败恢复提示。
- v0.3.x：增强 release automation、测试执行和 PR review 支持，让 Codex/Claude/Gemini/OpenCode/MiMo/Antigravity/DevEco Code 可以更稳定地参与维护工作流。
- v0.4.x：补充插件化 agent provider 配置、更多模型厂商适配和可复用 workflow template。
- 长期方向：把 TmuxCodingTeam 打磨成可审计、可恢复、可扩展的本地 multi-agent software maintenance toolkit。

## 许可证

本项目使用 MIT License。详见 `LICENSE`。

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
mimo --help
agy --help
deveco --help
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
