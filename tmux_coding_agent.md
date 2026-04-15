# tmux Coding Agent 编排研究笔记

基于以下四个开源项目的源码阅读，重点看它们如何做智能体编排，以及哪些项目把 `tmux` 当成核心运行时：

- `/Users/chenjunming/Desktop/KevinGit/AutoCodex/amux`
- `/Users/chenjunming/Desktop/KevinGit/AutoCodex/ccmanager`
- `/Users/chenjunming/Desktop/KevinGit/AutoCodex/claude_code_agent_farm`
- `/Users/chenjunming/Desktop/KevinGit/AutoCodex/cli-agent-orchestrator`

本文只关注后端/运行时/会话编排，忽略前端展示层。

## 快速结论

| 项目 | 核心定位 | 是否用 tmux | tmux 粒度 | 编排风格 |
|---|---|---|---|---|
| `amux` | Claude Code 长会话管理器 + dashboard | 是 | 1 个 session 对应 1 个 agent 会话 | 人类主导、多长会话并行、自动健康管理 |
| `ccmanager` | 多 AI CLI 会话管理器 | 否 | 不使用 tmux，直接用 PTY | 人类主导、多 worktree 会话管理 |
| `claude_code_agent_farm` | Claude Code 批量并行改代码农场 | 是 | 1 个 tmux session + 多 pane | 中央控制器批量派发任务、集中监控 |
| `cli-agent-orchestrator` | 通用多智能体编排系统 | 是 | 1 个 tmux session 包含多个 window，每个 window 一个 agent | Supervisor/Worker 分层、多 agent 消息传递、MCP 编排 |

结论很清楚：

- `amux`、`claude_code_agent_farm`、`cli-agent-orchestrator` 都是 **tmux 核心派**。
- `ccmanager` 明确是 **非 tmux 派**，它的底座是 Bun 的 PTY/Terminal。

## 1. amux

### 1.1 整体模型

核心入口在：

- `/Users/chenjunming/Desktop/KevinGit/AutoCodex/amux/amux-server.py`

`amux` 本质上是一个 **Claude Code 长会话管理器**。它不是“任务图编排器”，而是：

- 用 `tmux` 承载每个 Claude 长会话
- 用 `~/.amux/sessions/*.env` 保存会话配置
- 用 `~/.amux/logs/*.log`、`~/.amux/transcripts/` 保存日志与转录
- 用后台 health loop 持续做自动修复、自动继续、自动压缩上下文

它更像“给很多 agent 开长时间工位”，而不是“给 agent 做复杂任务树调度”。

### 1.2 tmux 是怎么用的

关键实现点：

- `tmux_name(session)`：统一把逻辑会话名映射为 `amux-<name>`
- `is_running(session)`：不用 `tmux has-session` 做存在性判断，而是 `tmux list-sessions -F "#{session_name}"` 后做精确匹配，规避前缀误判
- `tmux_capture(session, lines)`：通过 `tmux capture-pane -p` 抓 pane 输出
- `start_session(...)`：直接 `tmux new-session -d -s ... -n ... -c <work_dir> ... claude ...`
- `pipe-pane`：启动后立刻 `tmux pipe-pane -o "cat >> logfile"`，把 pane 输出持续流式写入磁盘
- `send_text(...)`：长文本不是 `send-keys` 一把梭，而是 `load-buffer + paste-buffer -p + Enter`

这套做法很成熟，尤其是两点值得直接借鉴：

- **长文本发送必须走 tmux buffer/paste，而不是逐字符 send-keys**
- **日志采集不要靠频繁 capture-pane，全量日志应交给 pipe-pane 做流式落盘**

### 1.3 编排方式

`amux` 的“编排”不体现在 supervisor-worker，而体现在 **会话自治 + 自动运维**：

- 自动检测上下文不足并发送 `/compact`
- 自动检测“thinking block corruption”并重启会话
- 会话 waiting 太久时自动继续
- Claude 异常退回 shell prompt 时自动拉起
- 发送前使用 per-session lock，避免多个线程并发向同一 pane 写入
- 在服务重启后重新接回 surviving tmux session 的 `pipe-pane`

所以 `amux` 借鉴价值最大的是：

- `tmux` 会话稳定命名
- 长会话恢复
- 输出持久化
- 自动健康管理
- 对 `tmux` 命令边界情况的处理非常务实

### 1.4 对我们最有价值的点

- 适合借鉴做 **“一个目录一个长会话 agent”**
- 适合借鉴 **日志/转录/元数据分层落盘**
- 适合借鉴 **per-session send lock**
- 适合借鉴 **health check + auto-continue + auto-restart**
- 不太适合直接照搬成复杂 supervisor-worker 编排框架，因为它的重点不是任务树，而是 session manager

## 2. ccmanager

### 2.1 整体模型

核心入口与关键代码：

- `/Users/chenjunming/Desktop/KevinGit/AutoCodex/ccmanager/README.md`
- `/Users/chenjunming/Desktop/KevinGit/AutoCodex/ccmanager/src/services/sessionManager.ts`
- `/Users/chenjunming/Desktop/KevinGit/AutoCodex/ccmanager/src/services/globalSessionOrchestrator.ts`
- `/Users/chenjunming/Desktop/KevinGit/AutoCodex/ccmanager/src/services/bunTerminal.ts`

`ccmanager` 的定位是：

- 管理多个 AI coding assistant session
- 重点面向 **git worktree / multi-project** 场景
- 重点做 **状态监控、切换、工作树管理、命令 preset、自动审批**

它是一个多 session manager，但不是 tmux 方案。

### 2.2 它不是 tmux

README 已经写得很明确：

- `No tmux dependency`

源码层面也能确认：

- `bunTerminal.ts` 直接基于 `Bun.Terminal` + `Bun.spawn(...)`
- `sessionManager.ts` 里 `spawn(...)` 返回的是 PTY 抽象 `IPty`
- 状态检测通过 `@xterm/headless` 的虚拟终端内容做

也就是说，它的底座是：

- **Bun PTY**
- **Headless terminal model**
- **进程内状态检测**

不是：

- `tmux session/window/pane`

### 2.3 编排方式

`ccmanager` 的编排粒度是 **每个 worktree 一个会话**：

- `GlobalSessionOrchestrator` 负责给不同 project path 分发独立 `SessionManager`
- `SessionManager` 内部按 `worktreePath` 维护 session
- 每个 session 启一个 PTY 进程
- 每个 session 有自己的 state detector
- 状态变化带 persistence guard，避免 TUI 瞬时重绘导致 busy/idle 误判
- 支持 fallback command preset
- 支持 auto approval verifier

所以它的强项是：

- 多项目/多 worktree 的人类操控
- provider-specific 状态检测
- 非 tmux 的跨平台终端抽象

### 2.4 对我们最有价值的点

虽然它不用 tmux，但有几件事非常值得借鉴：

- **session 不要只存 process，要同时存 terminal model + state detector**
- **状态切换要有 persistence duration，不要看到一帧 idle 就当 idle**
- **每个 provider 都要有自己的状态检测策略**
- **worktree/project 维度的 session 分组很实用**

如果以后我们想把 tmux 之外再抽象出一层“统一 terminal runtime interface”，`ccmanager` 是很好的参考。

## 3. claude_code_agent_farm

### 3.1 整体模型

核心文件：

- `/Users/chenjunming/Desktop/KevinGit/AutoCodex/claude_code_agent_farm/README.md`
- `/Users/chenjunming/Desktop/KevinGit/AutoCodex/claude_code_agent_farm/claude_code_agent_farm.py`

这个项目是一个典型的 **集中控制型 tmux swarm**：

- 用一个 Python orchestrator 当“大脑”
- 用一个 tmux session 承载一组 Claude Code agent
- 每个 agent 占一个 pane
- orchestrator 统一发 prompt、统一监控、统一重启

它是“并行农场”，不是“点对点 agent 通信网络”。

### 3.2 tmux 是怎么用的

关键模式很鲜明：

- `setup_tmux_session()`：
  - `tmux new-session -d -s <session> -n controller`
  - 再建 `agents` window
  - 再 `split-window` 成 tiled 布局
- `pane_mapping[agent_id] = "<session>:agents.<pane_id>"`
- `tmux_send(...)`：
  - 长 prompt 用 `load-buffer + paste-buffer`
  - 再发 Enter
- `tmux_capture(...)`：
  - 用 `capture-pane -p` 抓每个 pane 的内容
- pane title 会根据状态与 context 百分比动态更新
- controller window 里跑 monitor dashboard

这类设计的优点是：

- 人类 attach 一次就能看到很多 agent
- 可视化并发很强
- 适合同一项目里大规模并行扫问题

### 3.3 编排方式

它的编排更接近 **中央广播/中央监控**：

- orchestrator 生成问题文件
- 依次拉起多个 Claude Code pane
- 每个 pane 进入同一项目目录
- 检测 ready 后注入各自 prompt
- monitor loop 周期性扫描 pane 输出
- 根据状态执行 `/clear`、restart、commit、regenerate

另外它有几个很实用的工程细节：

- 用 `~/.claude/.agent_farm_launch.lock` 避免并发拉起 Claude Code 时污染共享配置
- agent 启动采用 stagger，并根据成功/失败动态调整间隔
- heartbeat 文件 + pane 内容双重判断 agent 是否卡死
- 强依赖 readiness detection，没 ready 不发 prompt

### 3.4 对我们最有价值的点

- 如果目标是 **同一项目多个 agent 并行跑同类任务**，pane farm 非常适合
- **启动锁 + stagger** 很值得借鉴，尤其是 Claude/Gemini 这类会写本地配置的 CLI
- **先等 shell ready，再等 agent ready，再发 prompt** 这个三段式很重要
- **pane title 显示状态** 很适合人类巡检

它的短板是：

- 只偏 Claude Code
- 没有通用 provider 抽象
- 没有像 CAO 那样的 agent-to-agent 消息收发协议

## 4. cli-agent-orchestrator

### 4.1 整体模型

关键文件：

- `/Users/chenjunming/Desktop/KevinGit/AutoCodex/cli-agent-orchestrator/README.md`
- `/Users/chenjunming/Desktop/KevinGit/AutoCodex/cli-agent-orchestrator/src/cli_agent_orchestrator/clients/tmux.py`
- `/Users/chenjunming/Desktop/KevinGit/AutoCodex/cli-agent-orchestrator/src/cli_agent_orchestrator/services/terminal_service.py`
- `/Users/chenjunming/Desktop/KevinGit/AutoCodex/cli-agent-orchestrator/src/cli_agent_orchestrator/services/session_service.py`
- `/Users/chenjunming/Desktop/KevinGit/AutoCodex/cli-agent-orchestrator/src/cli_agent_orchestrator/services/inbox_service.py`

这是四个项目里 **最完整的多智能体编排框架**。

它不只是“多 session 管理”，而是：

- supervisor/worker 分层
- provider 抽象
- tmux 隔离运行时
- database 元数据
- MCP server 做 handoff / assign / send_message

### 4.2 tmux 是怎么用的

它对 tmux 的抽象非常清晰：

- `session_service.py` 明确规定：
  - `Session = tmux session`
  - `Terminal = tmux window`
- `TmuxClient.create_session(...)`：创建新 tmux session
- `TmuxClient.create_window(...)`：往现有 session 加一个 window
- `TmuxClient.send_keys(...)`：统一走 `load-buffer + paste-buffer -p + Enter`
- `TmuxClient.get_history(...)`：用 `capture-pane -e -p`
- `TmuxClient.pipe_pane(...)`：流式日志到文件
- `TmuxClient.get_pane_working_directory(...)`：直接查 pane 当前目录

所以它不是把 tmux 当“临时命令壳”，而是把 tmux 当 **正式 runtime substrate**。

### 4.3 编排方式

它的核心模式是：

- `create_terminal()`：
  - 创建 tmux session 或 tmux window
  - 落 DB 元数据
  - 初始化 provider
  - 开启 `pipe-pane`
- 每个 terminal 对应一个 provider instance
- provider 自己负责：
  - 初始化 agent
  - 状态检测
  - 提取最后一条回复
  - 清理

在更高一层，它通过 MCP server 提供三种编排模式：

- `handoff`
  - 新建 worker，阻塞等待完成，再把结果返回给调用者
- `assign`
  - 新建 worker，异步执行，完成后由 worker 主动回消息
- `send_message`
  - 给已存在的 worker 发消息

这比纯“控制台脚本 + tmux”强很多，因为它定义了 **agent 间的协议**。

### 4.4 inbox / 消息投递

这是它最值得借鉴的一块：

- agent 消息先入数据库 inbox
- `pipe-pane` 持续把 terminal 输出写 log
- `watchdog` 监听 log 文件变化
- 先用 log tail 快速判断 target terminal 是否 idle
- 真正空闲时再把消息送进去

这解决了一个很真实的问题：

- 不是“想发消息就能发”
- 而是“要等目标 agent 进入可接收状态再发”

这个设计比轮询 `capture-pane` 更高效，也更稳定。

### 4.5 provider 抽象

它支持多 provider：

- Claude Code
- Codex
- Gemini CLI
- Kimi CLI
- Q CLI
- Copilot CLI
- Kiro CLI

每个 provider 的实现都知道：

- 这个 CLI 怎么启动
- ready/processing/completed/error 怎么检测
- 最后一条消息怎么提取
- 需要几次 Enter
- 哪些 TUI footer 要过滤

这套思想非常适合多厂商 coding agent 场景。

### 4.6 对我们最有价值的点

- **session / terminal / provider 三层分离**
- **provider-specific ready/status/output 抽象**
- **tmux window 级别 agent 隔离**
- **消息投递不要直接写 pane，要做 inbox + idle delivery**
- **给每个 terminal 注入唯一 ID 环境变量，便于跨 agent 路由**

如果要做“真正的多 agent 编排平台”，它是四个项目里最值得优先吸收的参考。

## 哪些项目是 tmux 实现的

明确使用 `tmux` 作为核心运行时的项目：

- `amux`
- `claude_code_agent_farm`
- `cli-agent-orchestrator`

明确不依赖 `tmux` 的项目：

- `ccmanager`

## 各项目 tmux 使用方式对比

### amux

- 粒度：`1 tmux session = 1 Claude 长会话`
- 优点：简单稳定，长会话管理好做，恢复容易
- 更适合：人类管理多个独立 agent

### claude_code_agent_farm

- 粒度：`1 tmux session = 1 run`，内部 `多个 pane = 多 agent`
- 优点：并行可视化极强
- 更适合：同一项目大批量并发执行同类任务

### cli-agent-orchestrator

- 粒度：`1 tmux session = 1 orchestrated workspace`，内部 `多个 window = 多 agent`
- 优点：结构清晰，支持 supervisor-worker、多 provider、消息路由
- 更适合：复杂多智能体编排系统

## 最值得借鉴的方法

结合四个项目，最值得在 tmux coding agent 系统里吸收的做法如下。

### 1. 发送大 prompt 一律用 tmux buffer/paste

不要依赖逐字符 `send-keys`。

推荐统一策略：

- `tmux load-buffer`
- `tmux paste-buffer -p`
- 延迟一小段时间
- 再发送 `Enter`

原因：

- 多行 prompt 不会被 TUI 热键误吃掉
- 特殊字符更安全
- 性能和稳定性都更好

### 2. 日志采集用 pipe-pane，状态检测再用 capture-pane

不要把所有事情都压在 `capture-pane` 上。

更合理的分工：

- `pipe-pane`：负责实时日志、转录、文件监听
- `capture-pane`：负责临时状态判定、提取最后回复

这是 `amux` 和 `cli-agent-orchestrator` 都在做的事。

### 3. 给每个 agent 做 provider-specific detector

不要幻想一个正则能同时兼容 Claude/Gemini/Codex/Qwen/Kimi。

应该拆成：

- `ready detector`
- `processing detector`
- `waiting detector`
- `response extractor`

`ccmanager` 和 `cli-agent-orchestrator` 在这点上最成熟。

### 4. 启动流程要分三段

推荐严格分开：

1. shell ready
2. agent ready
3. prompt send

`claude_code_agent_farm` 和 `cli-agent-orchestrator` 都证明了这一点很重要，尤其是 Gemini 这类 Ink TUI。

### 5. 发消息前要串行化

对同一个 tmux target，必须有 lock。

否则两个线程同时往同一个 pane 写内容，很容易把 prompt 粘在一起，或让 Enter 打到错误时机。

`amux` 的 per-session send lock 很值得直接借鉴。

### 6. 共享本地配置的 CLI 要做 launch lock / stagger

像 Claude Code、Gemini CLI 这类工具，启动时可能会：

- 读写全局配置
- 读写认证文件
- 注册 MCP

如果同时拉起很多实例，可能互相污染。

`claude_code_agent_farm` 的两招很实用：

- launch lock
- staggered startup

### 7. 消息投递最好做成 inbox，而不是立即写 pane

如果以后要做多 agent 协作，推荐采用 `cli-agent-orchestrator` 的思路：

- 消息先入队
- 等目标 agent idle
- 再实际投递

这比“直接 send 到 pane”稳定很多。

### 8. 把 runtime 元数据落盘

至少建议有：

- `state.json`
- `log`
- `transcript`
- `session meta`

这样人类 CLI、后台监控、恢复逻辑才能解耦。

`amux` 在这方面尤其完整。

## 对当前 Canopy / tmux coding agent 的建议

如果从这四个项目里提炼一套最适合当前项目的组合，我建议是：

- 运行时骨架：参考 `cli-agent-orchestrator`
  - `session / terminal / provider` 三层拆分
- tmux 发送与日志：参考 `amux`
  - `pipe-pane`
  - 精确 session 名判断
  - send lock
- 启动保护：参考 `claude_code_agent_farm`
  - ready 检测
  - stagger
  - launch lock
- 状态检测思想：参考 `ccmanager`
  - provider-specific state detector
  - 状态 persistence 防抖

如果要按“人类通过一个终端控制多个 tmux 会话”的方向继续演进，更推荐：

- `1 工作目录 = 1 长会话 agent`
- `B01` 只做人类控制台
- runtime 层做：
  - per-agent state/log/transcript
  - provider detector
  - proxy/env 注入
  - idle/ready 判断
- 如果后面要做 agent 之间协作，再引入类似 CAO 的 inbox / handoff / assign 机制

## 最后总结

这四个项目里：

- `amux` 最像 **长会话 agent 运维平台**
- `ccmanager` 最像 **非 tmux 的多 session 管理器**
- `claude_code_agent_farm` 最像 **并行 pane 农场**
- `cli-agent-orchestrator` 最像 **正式的多智能体编排框架**

如果问题是“哪些项目也是用 tmux 实现的”，答案是：

- `amux`
- `claude_code_agent_farm`
- `cli-agent-orchestrator`

如果问题是“哪一个最值得作为多厂商 tmux coding agent 的主参考”，答案是：

- **`cli-agent-orchestrator`**

如果问题是“哪一个最值得借鉴 tmux 细节和长会话运维经验”，答案是：

- **`amux`**
