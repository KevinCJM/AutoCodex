# AutoCodex

AutoCodex 是一个“外层 Python 编排器 + 内层 Codex CLI 会话”的多智能体自动开发工具。

它不是通过 Python SDK 直接调用模型，而是由 Python 脚本启动多个 `codex exec --json --full-auto` 会话，并让一个“调度器”智能体协调四个功能型智能体，按固定流程完成：

1. 需求分析与详细设计
2. 任务拆分
3. 代码开发、审核与测试

当前仓库已经针对一个具体项目做了默认配置：围绕 `canopy-api-v3` 的 calculation 能力拆分为独立服务。也就是说，这个仓库本质上是一个“自动化开发编排器”，不是一个开箱即用的通用框架模板。你如果要用于别的项目，第一步不是直接运行，而是先改配置。

## 1. 先理解这个仓库到底在做什么

这个仓库本身不承载业务代码，它只负责“调度”。

真正被分析、被改造、被测试的代码库，是 `B00_agent_config.py` 里的 `working_path` 指向的目标项目目录。当前默认值是：

```python
working_path = "/Users/chenjunming/Desktop/Canopy/canopy-api-v3"
```

因此可以把整个系统分成两层：

- 外层控制层：当前这个 `AutoCodex` 仓库，负责启停工作流、拼接 prompt、维护会话、写日志、做恢复。
- 内层执行层：`working_path` 指向的目标仓库，Codex 智能体会在那个目录里阅读代码、写设计文档、拆任务、改代码、跑测试。

这也是这个项目最容易误解的点：

- 运行脚本是在 `AutoCodex` 仓库里执行。
- 生成的文档、日志、状态文件默认都写进 `working_path` 指向的目标仓库里。
- `codex exec --cd <working_path>` 会让目标仓库自己的 `AGENTS.md`、代码结构、测试工具、虚拟环境成为智能体的真实上下文。

## 2. 整体架构

### 2.1 工作流阶段

整个系统分为 3 个主阶段，对应 3 个主脚本：

| 脚本 | 阶段 | 作用 |
| --- | --- | --- |
| `A01_requiment_analysis_workflow.py` | 需求分析与详细设计 | 需求澄清、写详细设计、组织多角色评审 |
| `A02_task_workflow.py` | 任务拆分 | 基于详细设计产出任务单并组织评审 |
| `A03_coding_agent_workflow.py` | 代码开发 | 按任务单逐项开发、审核、测试 |

总入口是：

| 脚本 | 作用 |
| --- | --- |
| `A00_main.py` | 按顺序依次执行 A01 -> A02 -> A03 |

### 2.2 智能体角色

系统固定使用 5 个智能体角色：

- 调度器
- 需求分析师
- 审核员
- 测试工程师
- 开发工程师

其中：

- 调度器只负责“决定下一步该让谁做什么”，并且要求返回严格 JSON。
- 其他四个是功能型智能体，分别负责分析、评审、测试、开发。

### 2.3 调度方式

每个功能型智能体都对应一个独立的 Codex 会话线程。

运行过程大致是：

1. Python 先初始化多个功能型智能体会话。
2. Python 再初始化一个调度器会话。
3. 调度器返回一段 JSON，例如：

```json
{"需求分析师": "请先补全详细设计文档"}
```

或者：

```json
{"审核员": "请审核任务单", "测试工程师": "请审核任务单", "开发工程师": "请审核任务单"}
```

4. Python 并发调用这些被调度到的智能体。
5. Python 收集各智能体回复，拼成新的 prompt 再喂回调度器。
6. 循环直到调度器返回：

```json
{"success": "详细设计完成"}
```

或：

```json
{"success": "任务拆分完成"}
```

或：

```json
{"success": "所有任务开发完成"}
```

## 3. 代码文件职责

| 文件 | 作用 |
| --- | --- |
| `A00_main.py` | 顺序执行三个主阶段 |
| `A01_requiment_analysis_workflow.py` | 需求分析、详细设计、人类澄清闭环 |
| `A02_task_workflow.py` | 任务拆解与多角色评审 |
| `A03_coding_agent_workflow.py` | 开发、审核、测试的迭代闭环 |
| `B00_agent_config.py` | 全局配置中心，最重要的文件 |
| `B01_codex_utils.py` | 封装 `codex exec`、解析 JSON 事件流、初始化/恢复会话 |
| `B02_log_tools.py` | 彩色打印与日志落盘 |
| `B03_init_function_agents.py` | 初始化功能型智能体、个性化初始化、解析调度器 JSON |
| `C01_recover_requirement_workflow.py` | 恢复 A01 阶段 |
| `C02_recover_task_workflow.py` | 恢复 A02 阶段 |
| `C03_recover_coding_workflow.py` | 恢复 A03 阶段 |

## 4. 运行依赖

这个仓库没有 `requirements.txt`、`pyproject.toml` 或第三方 Python 依赖声明。就当前代码看，Python 层只使用标准库。

真正的运行依赖主要有 4 类：

### 4.1 Python

- 建议 Python 3.10+
- 当前 prompt 里硬编码了一个 Python 解释器路径：

```text
/Users/chenjunming/Desktop/myenv_310/bin/python
```

注意：

- 这个路径不是外层编排器必须使用的解释器。
- 这是告诉“内层 Codex 智能体”在目标仓库里执行 Python 时优先用哪个解释器。
- 如果你的环境没有这个路径，必须改 `B00_agent_config.py` 中的 `common_init_prompt_1`。

### 4.2 Codex CLI

本项目的核心依赖是 `codex` 命令行工具。

脚本内部实际调用的是：

```bash
codex exec --model <model> --config model_reasoning_effort=<effort> --skip-git-repo-check --json --full-auto --cd <working_path> <prompt>
```

因此你必须满足：

- 本机已安装 `codex`
- `codex` 已经能正常执行
- 当前用户已经完成 Codex CLI 所需登录或鉴权

可以先手动验证：

```bash
codex --version
```

### 4.3 目标仓库

`working_path` 指向的目标仓库必须真实存在，并且是 Codex 可以读写的目录。

当前默认路径是：

```text
/Users/chenjunming/Desktop/Canopy/canopy-api-v3
```

如果这个目录不存在，或者你想分析别的项目，必须先改 `working_path`。

### 4.4 技能环境（可选但强相关）

prompt 中写入了技能标签，例如：

- `$Product Manager`
- `$Scrum Master`
- `$System Architect`
- `$Business Analyst`
- `$Developer`

这些标签不是 Python 代码依赖，但它们会影响 Codex 在目标仓库中的行为质量。若你的 Codex 环境没有这些技能，可以：

1. 保留标签，观察 Codex 是否能兼容处理。
2. 替换成你环境里已有的技能名。
3. 直接删除这些技能前缀。

## 5. 最重要的配置文件：`B00_agent_config.py`

如果你只读一个文件，请先读这个文件。

### 5.1 必改配置

| 配置项 | 当前含义 | 是否通常需要修改 |
| --- | --- | --- |
| `working_path` | 目标项目目录，也是文档/日志/状态文件落盘目录 | 是 |
| `common_init_prompt_1` | 给所有智能体的通用约束，包含内层 Python 解释器路径 | 是 |
| `common_init_prompt_2` | 给所有智能体的代码理解入口说明，当前完全绑定 `canopy-api-v3` | 是 |
| `requirement_str` | 原始业务需求，当前写死为“指标计算服务独立化” | 是 |

### 5.2 常改配置

| 配置项 | 作用 |
| --- | --- |
| `design_md` | 详细设计文档名 |
| `task_md` | 任务拆分文档名 |
| `test_plan_md` | 测试计划文档名 |
| `REQUIREMENT_CLARIFICATION_MD` | 需求澄清记录文档名 |
| `AGENT_MODEL_EFFORT_CONFIG` | 每个智能体的模型和推理强度 |
| `working_timeout` | 单次 `codex exec` 超时时间 |
| `resume_retry_max` | 初始化/恢复失败后的最大重试次数 |
| `resume_retry_interval` | 重试间隔秒数 |

### 5.3 当前默认配置不是“通用模板”

当前默认配置做了很强的场景绑定，例如：

- 指定了目标项目绝对路径
- 指定了代码理解入口文件
- 指定了一个具体改造任务
- 指定了输出文档名称

所以你要迁移到新项目时，推荐至少改成下面这种形态：

```python
working_path = "/absolute/path/to/your-project"
design_md = "详细设计.md"
task_md = "任务拆分.md"
test_plan_md = "测试计划.md"

common_init_prompt_1 = """记住:
1) 使用中文进行对话和文档编写;
2) 使用 "/absolute/path/to/venv/bin/python" 命令来执行python代码
"""

common_init_prompt_2 = """了解代码架构, 主要是:
1) 从你的主入口文件理解系统全链路
2) 从测试入口或核心模块理解关键执行路径
"""

requirement_str = """
这里改成你的真实需求说明。
"""
```

## 6. 运行前准备

推荐顺序如下：

1. 打开 `B00_agent_config.py`
2. 修改 `working_path`
3. 修改 `common_init_prompt_1` 中的 Python 路径
4. 修改 `common_init_prompt_2` 中的代码入口说明
5. 修改 `requirement_str`
6. 确认目标仓库可读写
7. 确认 `codex --version` 能执行

建议再做一次快速检查：

```bash
python3 -m py_compile *.py
```

## 7. 如何使用

### 7.1 推荐用法：按阶段运行

第一次使用时，建议不要直接跑 `A00_main.py`，而是分阶段执行，更容易观察中间产物。

#### 第 1 阶段：需求分析与详细设计

```bash
python3 A01_requiment_analysis_workflow.py
```

这一阶段会：

- 初始化四个功能型智能体会话
- 先让需求分析师进入“需求理解与需求澄清”
- 如果需求分析师需要人类补充信息，会通过 `[[ASK_HUMAN]]` 触发人工输入
- 产出详细设计文档
- 让审核员、测试工程师、开发工程师循环评审，直到通过

#### 第 2 阶段：任务拆分

```bash
python3 A02_task_workflow.py
```

这一阶段会：

- 读取上一步的详细设计文档
- 让需求分析师生成任务单
- 让多个角色循环评审任务单，直到通过

#### 第 3 阶段：开发、审核、测试

```bash
python3 A03_coding_agent_workflow.py
```

这一阶段会：

- 先让测试工程师根据设计文档和任务单写测试计划
- 调度器从 `task_md` 中找下一个任务
- 让开发工程师逐项开发
- 开发完成后让需求分析师、审核员、测试工程师并发审核
- 如有问题，回到开发工程师修复
- 直到任务全部完成

### 7.2 一次性跑完整流程

```bash
python3 A00_main.py
```

它会顺序执行：

1. `A01_requiment_analysis_workflow.py`
2. `A02_task_workflow.py`
3. `A03_coding_agent_workflow.py`

注意：

- 这不是“带事务控制”的总控脚本。
- 它只是顺序调用三个 `main()`。
- 如果 A01 的产物不符合预期，A02/A03 仍然可能继续执行。
- 因此第一次接入新项目时，更推荐按阶段单独运行。

## 8. 人类交互机制

只有 `A01_requiment_analysis_workflow.py` 内的“需求分析师”允许向人类提问。

规则是：

- 提问必须带触发词：`[[ASK_HUMAN]]`
- 一次只问一个关键问题
- Python 外层会在终端里打印问题并阻塞等待你的输入
- 你的回答会被写入 `需求澄清记录.md`
- 随后系统会继续推进需求分析流程

如果其他智能体错误地使用了 `[[ASK_HUMAN]]`，系统会把它视为违规回复，并要求调度器改成由需求分析师处理。

## 9. 输出产物会写到哪里

所有产物默认都写入 `working_path` 指向的目标仓库，而不是当前 AutoCodex 仓库。

### 9.1 文档类产物

| 文件 | 说明 |
| --- | --- |
| `<working_path>/<design_md>` | 详细设计文档 |
| `<working_path>/<task_md>` | 任务拆分文档 |
| `<working_path>/<test_plan_md>` | 测试计划 |
| `<working_path>/<REQUIREMENT_CLARIFICATION_MD>` | 人类问答澄清记录 |

当前默认文件名分别是：

- `指标计算服务独立详细设计.md`
- `任务拆分.md`
- `测试计划.md`
- `需求澄清记录.md`

### 9.2 日志类产物

每个智能体每天一个日志文件，命名规则：

```text
agent_<智能体名称>_<YYYYMMDD>.log
```

例如：

- `agent_调度器_20260311.log`
- `agent_需求分析师_20260311.log`
- `agent_审核员_20260311.log`
- `agent_测试工程师_20260311.log`
- `agent_开发工程师_20260311.log`

这些日志非常重要，因为恢复脚本要靠它们反推出：

- session_id
- 最近一次调度器 JSON 输出
- 某个 agent 的 prompt 和 response 是否已经执行过

### 9.3 状态文件

状态文件只在恢复脚本执行时使用和写入。

| 文件 | 对应阶段 |
| --- | --- |
| `requirement_workflow_state.json` | A01 恢复状态 |
| `task_workflow_state.json` | A02 恢复状态 |
| `workflow_state.json` | A03 恢复状态 |

## 10. 中断后如何恢复

恢复逻辑的核心思想是：

1. 优先读取状态文件
2. 如果没有状态文件，就从日志反推当前阶段
3. 补齐各个 agent 的 session_id
4. 从未完成的 agent 或调度器轮次继续跑

### 10.1 恢复 A01：需求分析与详细设计

先看状态但不执行：

```bash
python3 C01_recover_requirement_workflow.py --dry-run
```

正式恢复：

```bash
python3 C01_recover_requirement_workflow.py
```

常用参数：

```bash
python3 C01_recover_requirement_workflow.py \
  --max-log-days 7 \
  --allow-reinit-on-missing-session
```

可用参数包括：

- `--state-path`
- `--log-dir`
- `--max-log-days`
- `--no-prefer-checkpoint`
- `--strict-json`
- `--allow-reinit-on-missing-session`
- `--dry-run`

### 10.2 恢复 A02：任务拆分

先 dry run：

```bash
python3 C02_recover_task_workflow.py --dry-run
```

正式恢复：

```bash
python3 C02_recover_task_workflow.py
```

可用参数与 A01 基本一致：

- `--state-path`
- `--log-dir`
- `--max-log-days`
- `--no-prefer-checkpoint`
- `--strict-json`
- `--allow-reinit-on-missing-session`
- `--dry-run`

### 10.3 恢复 A03：代码开发

这个脚本的 CLI 能力和前两个不对称。

直接从命令行可用的只有：

```bash
python3 C03_recover_coding_workflow.py --dry-run
python3 C03_recover_coding_workflow.py
```

原因是它没有用 `argparse` 暴露完整参数，而是在 `__main__` 中只判断了是否包含 `--dry-run`。

如果你要给 A03 恢复流程传自定义参数，建议这样调用：

```bash
python3 - <<'PY'
from C03_recover_coding_workflow import recover_workflow

result = recover_workflow(
    log_dir="/absolute/path/to/your-project",
    max_log_days=7,
    strict_json=True,
    allow_reinit_on_missing_session=False,
    dry_run=True,
)
print(result)
PY
```

### 10.4 恢复流程的注意点

- 恢复强依赖日志格式，不要手工破坏日志内容。
- `--allow-reinit-on-missing-session` 会在 session 丢失时新建会话，但这意味着该 agent 的上下文记忆会被重建。
- A01 恢复脚本额外处理了“需求分析师向人类提问”的闭环。
- A02/A03 没有 A01 那种终端问答闭环。

## 11. 提示词与技能是怎么接进去的

这个项目不是把所有 agent 行为写成类，而是大量通过 prompt 驱动。

### 11.1 通用初始化

每个 agent 初始化时会做两步：

1. 通用初始化 1：使用中文；指定 Python 解释器路径
2. 通用初始化 2：告诉 agent 目标项目代码应该从哪些入口理解

### 11.2 个性化初始化

不同阶段会给 agent 注入不同角色 prompt，例如：

- A01 需求分析师负责详细设计
- A02 需求分析师负责任务拆分
- A03 测试工程师先生成测试计划
- A03 开发工程师按任务单逐项开发

### 11.3 技能前缀

每个阶段还会通过 `agent_skills_dict` 给 agent 的 prompt 前面拼接技能标签。

例如：

- A01 / A03 中需求分析师使用 `$Product Manager`
- A02 中需求分析师使用 `$Scrum Master`
- 审核员通常使用 `$System Architect`
- 测试工程师通常使用 `$Business Analyst`
- 开发工程师通常使用 `$Developer`

如果你迁移到别的 Codex 环境，这部分可以按你自己的技能体系替换。

## 12. 推荐的实际使用姿势

如果你想把它迁移到自己的项目，推荐按下面步骤做：

1. 先只改 `B00_agent_config.py`
2. 把 `working_path` 改到你的目标仓库
3. 把 `common_init_prompt_2` 改成你的代码入口说明
4. 把 `requirement_str` 改成你的真实需求
5. 先单独跑 A01，确认能稳定产出详细设计
6. 再跑 A02，确认任务单结构符合你的开发节奏
7. 最后再跑 A03，让开发闭环落地
8. 等流程稳定后，再考虑用 `A00_main.py` 一键串起来

## 13. 常见问题

### 13.1 为什么运行后什么都没写到当前仓库？

因为默认所有产物都写到 `working_path`，不是写到 `AutoCodex` 当前目录。

### 13.2 为什么 agent 会去读另一个仓库的 `AGENTS.md`？

因为 `codex exec` 使用了 `--cd <working_path>`。对内层 Codex 来说，目标仓库才是当前工作目录。

### 13.3 为什么 agent 跑测试时报 Python 路径错误？

通常是 `common_init_prompt_1` 里的 Python 路径写死成了作者本机路径，你没有改。

### 13.4 为什么恢复失败说找不到可解析 JSON？

因为调度器输出必须是合法 JSON。若日志里的调度器输出被污染，恢复脚本可能无法重建状态。

### 13.5 这个仓库适合什么任务？

适合：

- 中大型改造任务
- 需要先做设计、再拆任务、再分阶段开发的任务
- 需要中断恢复能力的长流程任务

不太适合：

- 一两个文件的小改动
- 不需要评审与测试闭环的简单脚本任务

## 14. 一个最小可执行示例

假设你已经改好了 `B00_agent_config.py`，最小启动顺序如下：

```bash
cd /Users/chenjunming/Desktop/KevinGit/AutoCodex

python3 A01_requiment_analysis_workflow.py
python3 A02_task_workflow.py
python3 A03_coding_agent_workflow.py
```

如果你确认流程已经稳定，也可以改成：

```bash
cd /Users/chenjunming/Desktop/KevinGit/AutoCodex
python3 A00_main.py
```

## 15. 当前版本的几个关键信息

- 这个仓库当前没有 Python 三方依赖清单
- 当前默认目标项目是 `canopy-api-v3`
- 当前默认需求是“指标计算服务独立化”
- 当前恢复能力最完整的是 A01 和 A02
- A03 恢复脚本可用，但 CLI 参数暴露较少

如果你把上面这些都理解清楚，再去改配置和运行，基本就不会把它当成“普通脚手架”误用了。
