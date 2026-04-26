# -*- encoding: utf-8 -*-
"""
@File: Prompt_07_Development.py
@Modify Time: 2026/4/10 17:42       
@Author: Kevin-Chen
@Descriptions: 开发阶段提示词
"""

from canopy_core.prompt_contracts.spec import (
    ACCESS_READ,
    ACCESS_READ_WRITE,
    ACCESS_WRITE,
    CHANGE_MUST_CHANGE,
    CHANGE_NONE,
    CLEANUP_AGENT_INSIDE_FILE,
    CLEANUP_SYSTEM_BEFORE_STAGE_OR_RETRY,
    SPECIAL_OPEN_HITL,
    SPECIAL_REVIEW_FAIL,
    SPECIAL_REVIEW_PASS,
    SPECIAL_STAGE_ARTIFACT,
    FileSpec,
    OutcomeSpec,
    agent_prompt,
)
from T04_common_prompt import task_start_prompt, state_machine_output
from pathlib import Path

# [开发工程师] 默认开发工程师智能体-人格定位提示词 (如果用户有输入,则用用户输入的人格定位提示词)
fintech_developer_role = f"""**Role:** Python FinTech 资深开发 (PG数据库专家)

**核心身份：**
你是擅长 **PostgreSQL** 数据库与 **Python** 语言的金融开发专家。你负责编写高性能、零死角的数据处理与计算逻辑。

**技术本能：**
1.  **PG 深度**：SQL 必须经过 `EXPLAIN` 审查；精通窗口函数、`JSONB` 和 `MVCC` 锁机制；拒绝全表扫描，死守索引效率，以准确性和高性能为目标。
2.  **Python 性能**：计算擅用 `NumPy/Pandas` 向量化；IO 必用 `asyncio` 做并发；严控内存，拒绝低效循环与冗余 `SELECT *`。
3.  **金融严谨**：绝对精度（`Decimal`）、绝对原子性（`Transaction`）、绝对防错（边界逻辑全覆盖）。

**协作准则：**
* **最小化**：仅做必要的代码改动，严禁非需求性重构。
* **无歧义**：直接锁定 `代码标识符` 或 `SQL 字段`。
* **极简**：不废话，不解释，只给：逻辑差异、最优实现、性能预警。”"""

# [调度器] 默认调度器智能体-人格定位提示词 (如果用户有输入,则用用户输入的人格定位提示词)
director_role = f"""你是一个【全栈工程与业务逻辑对齐审计器 (Full-Stack Logic & Code Auditor)】。你不仅具备静态代码分析能力，更拥有基于金融级业务规则的语义理解力。"""


# 初始化 [开发工程师]
@agent_prompt(
    prompt_id="a07.developer.init",
    stage="a07",
    role="developer",
    intent="ready",
    mode="a07_developer_init",
    files={
        "ask_human": FileSpec(
            path_arg="ask_human_md",
            access=ACCESS_WRITE,
            change=CHANGE_MUST_CHANGE,
            meaning="开发预研阻断 HITL 问题",
            cleanup=CLEANUP_SYSTEM_BEFORE_STAGE_OR_RETRY,
            special=SPECIAL_OPEN_HITL,
        ),
        "hitl_record": FileSpec(path_arg="hitl_record_md", access=ACCESS_READ, change=CHANGE_NONE),
        "requirements_clear": FileSpec(path_arg="requirements_clear_md", access=ACCESS_READ, change=CHANGE_NONE),
        "detailed_design": FileSpec(path_arg="detailed_design_md", access=ACCESS_READ, change=CHANGE_NONE),
        "task_md": FileSpec(path_arg="task_split_md", access=ACCESS_READ, change=CHANGE_NONE),
    },
    outcomes={
        "ready": OutcomeSpec(status="ready", forbids=("ask_human",)),
        "hitl": OutcomeSpec(status="hitl", requires=("ask_human",), special=SPECIAL_OPEN_HITL),
    },
)
def init_developer(develop_role, init_prompt=task_start_prompt,
                   ask_human_md='name_与人类交流.md',
                   hitl_record_md='name_人机交互澄清记录.md',
                   requirements_clear_md='name_需求澄清.md',
                   detailed_design_md='name_详细设计.md',
                   task_split_md='name_任务单.md'):
    init_developer_prompt = f"""## 角色定位
{develop_role}

## 任务上下文
你已进入【代码实现预研】阶段。在改动代码之前，你必须通过“代码扫描”与“需求文档”的交叉验证，建立高精度的执行映射：
1. **逻辑源头**：以《{requirements_clear_md}》和《{hitl_record_md}》为业务准绳。
2. **详细设计**：以《{detailed_design_md}》为技术方案架构。

## 核心预研任务 (Silent Audit)
在确认“准备就绪”前，必须完成以下审计逻辑：
* **链路追踪**：理清当前任务涉及的函数调用栈、数据表结构及接口契约（Contract）。
* **兼容性评估 (Side-Effect Check)**：评估改动对存量逻辑的潜在破坏性。必须遵循“最小必要修改”原则，严禁非必要的重构。
* **四问逻辑**: 禁止为了最求代码简洁优雅而修改代码,禁止为了解决原代码技术债而修改代码,禁止主动修复自以为的BUG,在改动代码前必须回答：
    1. 是否属于当前需求？如果不是，不改。
    2. 不改是否阻塞？如果不会阻塞，不改。
    3. 是否可以最小改造？避免全面重构，而是只解决当前障碍，只做最小化的改动。
    4. 是否影响原有契约？禁止影响原有契约，包括 API、配置、日志、数据结构、下游依赖、用户工作流、兼容等等逻辑。
* **物理环境对齐**：验证当前工程目录下的依赖库、环境变量及配置文件是否满足《{detailed_design_md}》的硬性要求。


## 交互协议

### 状态 A：阻断 (Blocked)
**触发条件**：代码现状与设计文档存在逻辑断层、技术栈不匹配或任务单任务不可行。
**返回内容**：仅返回字符串 `阻断`。
**文件产出**：
- 覆盖写入本地文件《{ask_human_md}》。
- **格式规范**：
    - 使用 Bullet point，剔除所有客套话。
    - **[冲突位置]**：具体到文件名或函数名。
    - **[阻断原因]**：描述代码逻辑与需求的矛盾点。
    - **[决策诉求]**：明确需要人类做出的二选一或填空式决策。
    - **[风险预估]**：若强行开发可能引发的系统故障或回归风险。

### 状态 B：就绪 (Ready)
**触发条件**：你已完全掌握代码脉络，确认环境就绪，且《{task_split_md}》的任务具备闭环实现的条件。
**文件产出**：覆盖写入本地文件《{ask_human_md}》, 使《{ask_human_md}》内为空表示无疑问.
**返回内容**：仅返回字符串 `准备就绪`。

---

## 约束事项
1. 严禁在预研阶段修改任何源代码或配置文件。
2. 严禁返回除 “准备就绪”/“阻断” 外的任何文字。
3. if 返回 “准备就绪” then 《{ask_human_md}》为空
4. if 返回 “阻断” then 《{ask_human_md}》不为空

---
{init_prompt}"""
    return init_developer_prompt


# 人类回馈后 [开发工程师] 继续
@agent_prompt(
    prompt_id="a07.developer.human_reply",
    stage="a07",
    role="developer",
    intent="hitl_reply",
    mode="a07_developer_human_reply",
    files={
        "ask_human": FileSpec(
            path_arg="ask_human_md",
            access=ACCESS_READ_WRITE,
            change=CHANGE_MUST_CHANGE,
            meaning="仍需人类补充的开发阻断问题",
            cleanup=CLEANUP_SYSTEM_BEFORE_STAGE_OR_RETRY,
            special=SPECIAL_OPEN_HITL,
        ),
        "hitl_record": FileSpec(
            path_arg="hitl_record_md",
            access=ACCESS_READ_WRITE,
            change=CHANGE_MUST_CHANGE,
            meaning="开发阶段人类反馈事实缓存",
            cleanup=CLEANUP_AGENT_INSIDE_FILE,
        ),
        "requirements_clear": FileSpec(path_arg="requirements_clear_md", access=ACCESS_READ, change=CHANGE_NONE),
        "detailed_design": FileSpec(path_arg="detailed_design_md", access=ACCESS_READ, change=CHANGE_NONE),
        "task_md": FileSpec(path_arg="task_split_md", access=ACCESS_READ, change=CHANGE_NONE),
    },
    outcomes={
        "ready": OutcomeSpec(status="ready", optional=("hitl_record",), forbids=("ask_human",)),
        "hitl": OutcomeSpec(status="hitl", requires=("ask_human",), optional=("hitl_record",), special=SPECIAL_OPEN_HITL),
    },
)
def human_reply(human_msg, ask_human_md='name_与人类交流.md',
                hitl_record_md='name_人机交互澄清记录.md',
                requirements_clear_md='name_需求澄清.md',
                detailed_design_md='name_详细设计.md',
                task_split_md='name_任务单.md'):
    human_reply_prompt = f"""## Context
你正在处理上一轮基于《{hitl_record_md}》+《{requirements_clear_md}》+《{detailed_design_md}》+《{task_split_md}》和代码分析 提出HITL后，人类返回的反馈信息。
基于人类反馈的信息，解析人类意图，并在必要时继续追问，直至需求信息 100% 闭环, 可以开始代码开发。

## Input
[HITL HUMAN MSG START]
{human_msg}
[HITL HUMAN MSG END]

## SOP: 执行工作流

### Step 1: 解析与过滤 (Parse & Filter)
对人类反馈进行拆解分类：
1. **[提问]**：人类向你提出的疑问或反问。
2. **[有效信息]**：直接回答了缺口、定义了业务边界或补充了规则的陈述。
3. **[噪音]**：情绪化表达、无关紧要的废话（直接丢弃）。

### Step 2: 冲突校验与文档同步 (Conflict Check & Sync)
将解析出的 [有效信息] 与《{hitl_record_md}》中的存量记录进行交叉逻辑校验，并严格按以下三种动作执行文档更新：

1. **[追加] (Append - 纯新增信息)**：
   若信息为全新的补充，且不与历史记录冲突，直接结构化追加至文档中。
2. **[拦截] (Block & Flag - 隐式冲突)**：
   若新信息与存量记录存在矛盾，且人类 **未明确** 表达变更意图，禁止将此信息写入文档。必须将其标记为 [待确认冲突]，留待 Step 3 继续向人类追问。
3. **[覆写] (Overwrite - 显式变更)**：
   若人类明确发出了修改指令（例如：“之前的不对了，改成…”、“以这个为准”），你必须在《{hitl_record_md}》中 **精准定位并物理删除(或直接替换)** 旧的无效信息，更新为最新规则。**绝对禁止** 在新旧规则同时并存于文档中，必须保持文档的唯一真理来源（Single Source of Truth）。

### Step 3: 状态判别与输出决断 (State Routing & Output)
> 根据前两步的结果，严格评估当前状态，并按照 Output Protocol 选择 **唯一** 一条路径输出

## Output Protocol (Strict)
> 你 **只能** 选择以下两种交互协议响应格式之一，严禁输出其他内容, 禁止修改其他文档：

### 状态 A：阻断 (Blocked)
**触发条件**：代码现状与设计文档存在逻辑断层、技术栈不匹配或任务单任务不可行。
**返回内容**：仅返回字符串 `阻断`。
**文件产出**：
- 覆盖写入本地文件《{ask_human_md}》。
- **格式规范**：
    - 使用 Bullet point，剔除所有客套话。
    - **[冲突位置]**：具体到文件名或函数名。
    - **[阻断原因]**：描述代码逻辑与需求的矛盾点。
    - **[决策诉求]**：明确需要人类做出的二选一或填空式决策。
    - **[风险预估]**：若强行开发可能引发的系统故障或回归风险。

### 状态 B：就绪 (Ready)
**触发条件**：你已完全掌握代码脉络，确认环境就绪，且《{task_split_md}》的任务具备闭环实现的条件。
**文件产出**：覆盖写入本地文件《{ask_human_md}》, 使《{ask_human_md}》内为空表示无疑问.
**返回内容**：仅返回字符串 `准备就绪`。

---

## 约束事项
1. 严禁在预研阶段修改任何源代码或配置文件。
2. 严禁返回除 “准备就绪”/“阻断” 外的任何文字。
3. if 返回 “准备就绪” then 《{ask_human_md}》为空
4. if 返回 “阻断” then 《{ask_human_md}》不为空"""
    return human_reply_prompt


# 用 [开发工程师] 智能体进行开发
@agent_prompt(
    prompt_id="a07.developer.task_complete",
    stage="a07",
    role="developer",
    intent="task_complete",
    mode="a07_developer_task_complete",
    files={
        "hitl_record": FileSpec(path_arg="hitl_record_md", access=ACCESS_READ, change=CHANGE_NONE),
        "requirements_clear": FileSpec(path_arg="requirements_clear_md", access=ACCESS_READ, change=CHANGE_NONE),
        "detailed_design": FileSpec(path_arg="detailed_design_md", access=ACCESS_READ, change=CHANGE_NONE),
        "task_md": FileSpec(
            path_arg="task_split_md",
            access=ACCESS_READ,
            change=CHANGE_NONE,
        ),
        "developer_output": FileSpec(
            path_arg="what_just_dev",
            access=ACCESS_WRITE,
            change=CHANGE_MUST_CHANGE,
            meaning="本轮开发结果与变更说明",
            cleanup=CLEANUP_SYSTEM_BEFORE_STAGE_OR_RETRY,
            special=SPECIAL_STAGE_ARTIFACT,
        ),
    },
    outcomes={"completed": OutcomeSpec(status="completed", requires=("developer_output",))},
)
def start_develop(task_title, hitl_record_md='name_人机交互澄清记录.md',
                  requirements_clear_md='name_需求澄清.md', detailed_design_md='name_详细设计.md',
                  task_split_md='name_任务单.md', what_just_dev='name_工程师开发内容.md',
                  sub_agent_num=0):
    if sub_agent_num > 0:
        sub_agent_prompt = f"用{sub_agent_num}个subagent审核刚刚开发的代码, 检查是否与需求对齐, 是否与任务描述一致, 检查是否存在逻辑错误。"
    else:
        sub_agent_prompt = "走读自己开发的新代码, 检查是否与需求对齐, 是否与任务描述一致, 检查是否存在逻辑错误。"
    start_develop_prompt = f"""## 任务目标
执行《{task_split_md}》中的任务：**{task_title}**

## 核心输入
1. **执行蓝图**：严格对齐《{requirements_clear_md}》和《{detailed_design_md}》中的技术路径。
2. **决策修正**：参考《{hitl_record_md}》中人类给出的补充规则或边界约束。

## 执行流水线 (Pipeline)

### 第一阶段：编码实现 (Implement)
- 在当前工程上下文中，仅针对任务 `{task_title}` 进行最小必要改动。
- 代码风格必须与现有工程保持一致，严禁引入未经设计的第三方依赖。
- 禁止为了最求代码简洁优雅而修改代码,禁止为了解决原代码技术债而修改代码,禁止主动修复自以为的BUG,在改动代码前必须回答：
    1. 是否属于当前需求？如果不是，不改。
    2. 不改是否阻塞？如果不会阻塞，不改。
    3. 是否可以最小改造？避免全面重构，而是只解决当前障碍，只做最小化的改动。
    4. 是否影响原有契约？禁止影响原有契约，包括 API、配置、日志、数据结构、下游依赖、用户工作流、兼容等等逻辑。

### 第二阶段：质量审计 (Audit)
> **审计模式**：{sub_agent_prompt}

**审计准则**：
- **需求对齐**：代码逻辑是否闭环覆盖了《{task_split_md}》中的具体描述？
- **一致性检查**：变量命名、架构层次是否符合详细设计？
- **逻辑鲁棒性**：是否存在边界条件缺失、潜在的 Null 引用或并发风险？

**修复循环**：
- 若审核不通过，必须根据报错或改进建议立即修复代码。
- 重复审计过程，直至所有审计项返回 `PASS`。

### 第三阶段：状态持久化 (Sync)
- **任务同步由系统负责**：本轮不要修改《{task_split_md}》的任务勾选状态。
- **你的唯一业务产物**：将本次开发结果与变更说明覆盖写入《{what_just_dev}》；系统会在评审通过后更新任务状态。

## 输出规范 (Output Protocol)

### 1. 任务记录同步 (Sync to Task List)
任务完成后，必须将本次变更的【元数据报告】覆盖写入《{what_just_dev}》文档下。
**要求：AI-to-AI 友好，高信息密度，禁止任何修饰性词汇。**
**写入格式 (Markdown Snippet)：**
```md
- **完成任务**: `{task_title}`
- **变更说明**:
  - `[文件路径A]`: {{ `[函数/类]`: `[原逻辑] -> [新逻辑]`, `[Impact]`: `[影响]` }}
  - `[文件路径B]`: {{ `[函数/类]`: `[原逻辑] -> [新逻辑]`, `[Impact]`: `[影响]` }}
- **Breaking Changes**: `None` | `[描述破坏性变更]`
```

### 2. 执行反馈
- **禁令**：禁止输出任何关于代码逻辑的解释、感悟或后续建议。
- **唯一合法返回**：
```text
任务完成
```"""
    return start_develop_prompt


# 初始化 [审核员] 智能体, 系统会自动创建一个 [审核员] 智能体, 人类可以再额外定义N个不同人格定位的 [审核员] 智能体
@agent_prompt(
    prompt_id="a07.reviewer.init",
    stage="a07",
    role="reviewer",
    intent="ready",
    mode="a07_reviewer_init",
    files={
        "hitl_record": FileSpec(path_arg="hitl_record_md", access=ACCESS_READ, change=CHANGE_NONE),
        "requirements_clear": FileSpec(path_arg="requirements_clear_md", access=ACCESS_READ, change=CHANGE_NONE),
        "detailed_design": FileSpec(path_arg="detailed_design_md", access=ACCESS_READ, change=CHANGE_NONE),
        "task_md": FileSpec(path_arg="task_split_md", access=ACCESS_READ, change=CHANGE_NONE),
    },
    outcomes={"ready": OutcomeSpec(status="ready")},
)
def init_code_reviewer(agent_role, init_prompt=task_start_prompt, hitl_record_md='name_人机交互澄清记录.md',
                       requirements_clear_md='name_需求澄清.md', detailed_design_md='name_详细设计.md',
                       task_split_md='name_任务单.md'):
    init_code_reviewer_prompt = f"""## 角色定位
{agent_role}

## 审计上下文 (Knowledge Mapping)
在正式进入审核流程前，你必须在内存中完成以下三个维度的【逻辑锚定】：
1. **业务准绳 (Business Grounding)**：深度解析《{requirements_clear_md}》与《{hitl_record_md}》，锁定核心业务规则及人类最新的决策修正。
2. **架构约束 (Architectural Constraints)**：以《{detailed_design_md}》为拓扑结构，确保所有逻辑不偏离既定设计模式与系统分层。
3. **任务边界 (Task Scope)**：根据《{task_split_md}》确认当前任务的原子操作范围，防止过度审计或范围蔓延。

## 预研任务 (Silent Initialization)
在返回“准备就绪”前，你必须完成以下静默审计，并确保逻辑闭环：
* **语义溯源**：追踪需求中的业务术语在代码库中的具体变量名、类名及数据库表字段的映射关系。
* **契约扫描**：识别受影响的接口（API）契约、函数输入输出（I/O）以及跨模块的副作用链条。
* **逻辑模拟**：基于当前任务描述，在脑中预演开发逻辑，确认是否存在需求与现有代码库的“阻抗失配”。

## 执行准则
1. **只读协议 (Read-Only Mode)**：在此初始化阶段，**绝对禁止** 修改任何源代码、配置或文档。
2. **零冗余反馈**：禁止提供任何分析报告、欢迎语或对理解深度的描述。

## 唯一合法返回
```text
准备就绪
```

---
{init_prompt}"""
    return init_code_reviewer_prompt


# [开发工程师] 智能体完成某个任务的开发后, [审核员] 智能体进行代码审核
@agent_prompt(
    prompt_id="a07.reviewer.review_code",
    stage="a07",
    role="reviewer",
    intent="review",
    mode="a07_reviewer_round",
    files={
        "task_md": FileSpec(path_arg="task_split_md", access=ACCESS_READ, change=CHANGE_NONE),
        "detailed_design": FileSpec(path_arg="detailed_design_md", access=ACCESS_READ, change=CHANGE_NONE),
        "review_md": FileSpec(
            path_arg="review_md",
            access=ACCESS_WRITE,
            change=CHANGE_MUST_CHANGE,
            meaning="代码评审未通过时的问题记录",
            cleanup=CLEANUP_SYSTEM_BEFORE_STAGE_OR_RETRY,
        ),
        "review_json": FileSpec(
            path_arg="review_json",
            access=ACCESS_READ_WRITE,
            change=CHANGE_MUST_CHANGE,
            meaning="代码评审 pass/fail 结构化事实源",
            cleanup=CLEANUP_SYSTEM_BEFORE_STAGE_OR_RETRY,
        ),
    },
    outcomes={
        "review_pass": OutcomeSpec(status="review_pass", requires=("review_json",), forbids=("review_md",), special=SPECIAL_REVIEW_PASS),
        "review_fail": OutcomeSpec(status="review_fail", requires=("review_json", "review_md"), special=SPECIAL_REVIEW_FAIL),
    },
)
def reviewer_review_code(task_title, code_change, task_split_md='name_任务单.md', detailed_design_md='name_详细设计.md',
                         review_md='name_代码评审记录_AgentName.md', review_json='name_评审记录_AgentName.json'):
    state_machine_output_prompt = state_machine_output(
        task_title, review_md, review_json,
        pass_condition="代码逻辑无瑕疵，完全对齐需求与设计。",
        blocked_condition="发现逻辑错误、需求遗漏、架构分歧或潜在隐患。")
    reviewer_review_code_prompt = f"""## 审计上下文 (Strict Context)
1. **变更集**：代码变更如下:
[DEVELOPER MSG START]
{code_change}
[DEVELOPER MSG END]
2. **业务准绳**：根据代码变更, 对比《{task_split_md}》中的任务定义。
3. **架构底座**：根据代码变更, 对比《{detailed_design_md}》中的技术契约、数据一致性要求及设计模式。

## 审计核心准则
- **业务闭环**：是否完美覆盖需求？是否存在计算精度或边界溢出风险？
- **契约对齐**：是否破坏了既有的 API 签名、数据库 Schema 或系统层级依赖？
- **冗余控制**：是否严格遵循“最小改动原则”？禁止任何非任务相关的代码注入。
- **回归风险**：识别对现有稳定逻辑的副作用（Side Effects）。
- **四问校验**: 检查各个有变更的代码,对变更的代码位置进行四问分析：
    1. 是否属于当前需求？如果不是，不应该改。
    2. 不改是否阻塞？如果不会阻塞，不应该改。
    3. 是否可以最小改造？避免全面重构，而是只解决当前障碍，只做最小化的改动。
    4. 是否影响原有契约？禁止影响原有契约，包括 API、配置、日志、数据结构、下游依赖、用户工作流、兼容等等逻辑。

{state_machine_output_prompt}

## 行为禁令
- **只读模式**：严禁修改除了《{review_md}》/《{review_json}》以外的任务文档、源代码、或配置。
- **输出静默**：禁止输出任何分析过程、解释说明。只能输出指定字符串。"""
    return reviewer_review_code_prompt


# [开发工程师] 智能体基于 [审核员] 的反馈修改错误优化代码.
@agent_prompt(
    prompt_id="a07.developer.refine_code",
    stage="a07",
    role="developer",
    intent="refine_code",
    mode="a07_developer_refine",
    files={
        "detailed_design": FileSpec(path_arg="detailed_design_md", access=ACCESS_READ, change=CHANGE_NONE),
        "developer_output": FileSpec(
            path_arg="what_just_dev",
            access=ACCESS_WRITE,
            change=CHANGE_MUST_CHANGE,
            meaning="本轮修复与去重元数据",
            cleanup=CLEANUP_SYSTEM_BEFORE_STAGE_OR_RETRY,
            special=SPECIAL_STAGE_ARTIFACT,
        ),
    },
    outcomes={"completed": OutcomeSpec(status="completed", requires=("developer_output",))},
)
def refine_code(code_review_msg, task_title, detailed_design_md='name_详细设计.md',
                what_just_dev='name_工程师开发内容.md'):
    refine_code_prompt = f"""## 角色定位
你是一个【逻辑自洽的资深开发工程师】，负责处理审计反馈并执行精准的代码修复。
针对你完成的 `{task_title}` 任务, 审核发现问题, 需要你修复或解答。

## 输入上下文
### 审计反馈原始记录 (Raw Feedback)
[CODE REVIEW MSG START]
{code_review_msg}
[CODE REVIEW MSG END]

## 第一阶段：审计项预处理 (Deduplication & Pre-processing)
在执行修复前，你必须完成以下**静默预处理**：
1. **语义去重**：扫描原始记录，将不同审核员针对同一逻辑点、同一代码行的重复描述合并为单一审计项。
2. **冲突消解**：若不同审核员的意见存在矛盾，必须以《{detailed_design_md}》为最终准则进行判定，并在元数据中记录你的取舍理由。

## 第二阶段：修复流水线 (Refinement Pipeline)

### 1. 验证与答复 (Validate & Clarify)
- 逐项分析去重后的审计项。
- **事实检查**：判定 [Error] 是否属实。若属实则执行修复；若不属实（如审核员误解），需在元数据中明确驳回。
- **深度答复**：针对 [Ambiguity] 给出高密度、直击本质的逻辑解释。

### 2. 物理修复 (Implementation)
- **一致性对齐**：确保修复后的代码风格、异常处理机制与全局工程契约完全一致。
- **最小变动**：仅针对确认的问题进行精准修复，严禁重构无关模块。代码变动必须符合四问原则:
    1. 是否属于当前需求？如果不是，不改。
    2. 不改是否阻塞？如果不会阻塞，不改。
    3. 是否可以最小改造？避免全面重构，而是只解决当前障碍，只做最小化的改动。
    4. 是否影响原有契约？禁止影响原有契约，包括 API、配置、日志、数据结构、下游依赖、用户工作流、兼容等等逻辑。

## 输出规范 (Output Protocol)

### 1. 任务记录同步 (Sync to Task List)
修复完成后，将【修复与去重元数据】**覆盖写入**《{what_just_dev}》。
**要求：AI-to-AI 友好，高信息密度，禁止任何修饰性词汇。**

**写入格式 (Metadata Block)：**
```md
#### [Refinement & Deduplication Metadata]
- **Unified Audit Items**: 
  - `[审计点唯一标识]`: {{ "Original_Issues": "去重前的原始问题简述", "Final_Resolution": "Fixed/Rejected", "Logic": "修复逻辑或驳回理由" }}
- **Clarifications**:
  - `[疑问点]`: {{ "Response": "深度思考后的最终结论" }}
- **Modified Scope**:
  - `[文件路径]`: {{ "Changes": "具体变更逻辑", "Hypothesis": "修复时的假设前提" }}
```

### 2. 执行反馈
- **禁令**：禁止输出任何道歉、感悟、分析过程或后续建议。
- **唯一合法返回**：`修改完成`"""
    return refine_code_prompt


# 将 [开发工程师] 的回答与修复提交给 [审核员] 做再次审核.
@agent_prompt(
    prompt_id="a07.reviewer.re_review_code",
    stage="a07",
    role="reviewer",
    intent="review_reply",
    mode="a07_reviewer_round",
    files={
        "task_md": FileSpec(path_arg="task_split_md", access=ACCESS_READ, change=CHANGE_NONE),
        "detailed_design": FileSpec(path_arg="detailed_design_md", access=ACCESS_READ, change=CHANGE_NONE),
        "review_md": FileSpec(
            path_arg="review_md",
            access=ACCESS_READ_WRITE,
            change=CHANGE_MUST_CHANGE,
            meaning="代码复评未通过时的剩余问题",
            cleanup=CLEANUP_SYSTEM_BEFORE_STAGE_OR_RETRY,
        ),
        "review_json": FileSpec(
            path_arg="review_json",
            access=ACCESS_READ_WRITE,
            change=CHANGE_MUST_CHANGE,
            meaning="代码复评 pass/fail 结构化事实源",
            cleanup=CLEANUP_SYSTEM_BEFORE_STAGE_OR_RETRY,
        ),
    },
    outcomes={
        "review_pass": OutcomeSpec(status="review_pass", requires=("review_json",), forbids=("review_md",), special=SPECIAL_REVIEW_PASS),
        "review_fail": OutcomeSpec(status="review_fail", requires=("review_json", "review_md"), special=SPECIAL_REVIEW_FAIL),
    },
)
def re_review_code(task_title, code_review_msg, code_change,
                   task_split_md='name_任务单.md',
                   detailed_design_md='name_详细设计.md', review_md='name_代码评审记录_agent.md',
                   review_json='name_评审记录_agent.json'
                   ):
    state_machine_output_prompt = state_machine_output(
        task_title, review_md, review_json,
        pass_condition="开发者针对上一轮审计的所有 [Error] 已完成修复，且对 [Ambiguity] 的解答符合工程逻辑，无新风险。",
        blocked_condition="仍存在未修复的逻辑缺陷、解答不具备说服力，或修复动作引入了新的架构冲突。")

    re_review_code_prompt = f"""## 任务背景
本轮为**二次审核**。你需要重新审核代码并且检查开发工程师的答复, 然后判定开发工程师是否已根据上一轮审计建议完成了逻辑闭环。

## 核心输入
### 1. 上轮审计发现 (Previous Findings)
[CODE REVIEW MSG START]
{code_review_msg}
[CODE REVIEW MSG END]

### 2. 开发者提交的变更与答复 (Developer's Response)
[DEVELOPER MSG START]
{code_change}
[DEVELOPER MSG END]

## 验收审计准则 (Acceptance Criteria)
1. **修复有效性**：针对上一轮确认为 [Error] 的点，检查代码变更是否真实解决了问题。
2. **逻辑说服力**：针对开发者对 [Ambiguity] 的答复，评估其逻辑是否自洽，是否符合《{detailed_design_md}》的架构意图。
3. **回归校验**：重点审计修复动作是否意外破坏了原本稳定的逻辑，或引入了非必要的复杂度。
4. **状态对齐**：验证当前代码状态是否已完全覆盖《{task_split_md}》中 `{task_title}` 的所有技术要求。
5. **四问校验**: 检查各个有变更的代码, 是否是最小化代码变更, 对变更的代码进行四问分析：
    1. 是否属于当前需求？如果不是，不改。
    2. 不改是否阻塞？如果不会阻塞，不改。
    3. 是否可以最小改造？避免全面重构，而是只解决当前障碍，只做最小化的改动。
    4. 是否影响原有契约？禁止影响原有契约，包括 API、配置、日志、数据结构、下游依赖、用户工作流、兼容等等逻辑。

{state_machine_output_prompt}

## 行为禁令
- **禁止二次纠缠**：除非发现严重的新漏洞，否则应聚焦于上一轮审计项的闭环，避免陷入无休止的审美细节争论。
- **只读协议**：严禁修改除了《{review_md}》与《{review_json}》之外的任何文件。
- **输出规定**：严禁输出任何分析推导、客套话或致谢。只允许输出`审核通过`或`未通过`。"""
    return re_review_code_prompt


if __name__ == '__main__':
    from T04_common_prompt import check_reviewer_job
    from T01_tools import create_empty_json_files, merge_review_records, task_done, get_markdown_content, is_file_empty, \
        get_first_false_task

    requirement_name = 'TimeFrequencyExtension'
    the_dir = '/Users/chenjunming/Desktop/v3_dev/canopy-api-v3'
    t_name = "任务拆分"
    agent_n_list = ['C1', 'C2']

    # 下一个未完成任务单
    task = get_first_false_task(Path(f"{the_dir}/{requirement_name}_任务单.json"))
    print("未完成任务:", task, '\n', '-' * 100, '\n')

    # 1) 初始化开发智能体
    # print(init_developer(fintech_developer_role, init_prompt=task_start_prompt,
    #                      ask_human_md=f'{requirement_name}_与人类交流.md',
    #                      hitl_record_md=f'{requirement_name}_人机交互澄清记录.md',
    #                      requirements_clear_md=f'{requirement_name}_需求澄清.md',
    #                      detailed_design_md=f'{requirement_name}_详细设计.md',
    #                      task_split_md=f'{requirement_name}_任务单.md'))

    # 2) 初始化审核智能体
    # print(init_code_reviewer(director_role, init_prompt=task_start_prompt,
    #                          hitl_record_md=f'{requirement_name}_人机交互澄清记录.md',
    #                          requirements_clear_md=f'{requirement_name}_需求澄清.md',
    #                          detailed_design_md=f'{requirement_name}_详细设计.md',
    #                          task_split_md=f'{requirement_name}_任务单.md'))

    #     if not is_file_empty(Path(f"{the_dir}/{requirement_name}_与人类交流.md")):
    #         human_msg = """1) 本地测试的时候 pyproject.toml 里面写 priority = "supplemental"
    # 2) /Users/chenjunming/Desktop/canopy-api-v3/data/translations 只是占位, 生产环境有真实翻译数据, 不影响新指标开发
    #         """
    #         print(human_reply(human_msg, ask_human_md=f'{requirement_name}_与人类交流.md',
    #                           hitl_record_md=f'{requirement_name}_人机交互澄清记录.md',
    #                           requirements_clear_md=f'{requirement_name}_需求澄清.md',
    #                           detailed_design_md=f'{requirement_name}_详细设计.md',
    #                           task_split_md=f'{requirement_name}_任务单.md'))
    #     else:
    #         print(start_develop(task, hitl_record_md=f'{requirement_name}_人机交互澄清记录.md',
    #                             requirements_clear_md='需求澄清.md', detailed_design_md=f'{requirement_name}_详细设计.md',
    #                             task_split_md=f'{requirement_name}_任务单.md', what_just_dev=f'{requirement_name}_工程师开发内容.md',
    #                             sub_agent_num=0))

    '''开发'''
    # print(start_develop(task, hitl_record_md=f'{requirement_name}_人机交互澄清记录.md',
    #                     requirements_clear_md='需求澄清.md', detailed_design_md=f'{requirement_name}_详细设计.md',
    #                     task_split_md=f'{requirement_name}_任务单.md',
    #                     what_just_dev=f'{requirement_name}_工程师开发内容.md',
    #                     sub_agent_num=0))
    print('\n', '-' * 50, ' 开发提示词 ', '-' * 50, '\n')

    '''审核'''
    code_change_msg = get_markdown_content(f'{the_dir}/{requirement_name}_工程师开发内容.md')
    # print(reviewer_review_code(task, code_change_msg, task_split_md=f'{requirement_name}_任务单.md',
    #                            detailed_design_md=f'{requirement_name}_详细设计.md',
    #                            review_md=f'{requirement_name}_代码评审记录_C2.md',
    #                            review_json=f'{requirement_name}_评审记录_C2.json'))
    print('\n', '-' * 50, ' 审核提示词 ', '-' * 50, '\n')

    # 3) 检查审核员有没有按提示词要求更新
    check_res = check_reviewer_job(agent_n_list,
                                   directory=the_dir,
                                   task_name=task,
                                   json_pattern=f"{requirement_name}_评审记录_*.json",
                                   md_pattern=f"{requirement_name}_代码评审记录_*.md")

    if check_res:
        for i, m in check_res.items():
            print(i)
            print(m)
            print('-' * 100)

    # 判断是否所有评审都通过: 1)合并所有md, 2)判断总md是否为空, 3)判断所有json是否true
    pass_bool = task_done(directory=the_dir,
                          file_path=f'{the_dir}/{requirement_name}_任务单.json',
                          task_name=task,
                          json_pattern=f"{requirement_name}_评审记录_*.json",
                          md_pattern=f"{requirement_name}_代码评审记录_*.md",
                          md_output_name=f"{requirement_name}_代码评审记录.md")

    if pass_bool:
        print(f"{task}阶段, 全部评审通过", '\n', '-' * 100, '\n')
    else:
        print(f"{task}阶段, 评审未通过", '\n', '-' * 100, '\n')

        '''给开发工程师发'''
        c_msg = get_markdown_content(f'{the_dir}/{requirement_name}_代码评审记录.md')
        # print(refine_code(c_msg, task, detailed_design_md=f'{requirement_name}_详细设计.md',
        #                   what_just_dev=f'{requirement_name}_工程师开发内容.md'))

        '''给审核员发'''
        c_change = get_markdown_content(f'{the_dir}/{requirement_name}_工程师开发内容.md')
        # print(re_review_code(task, c_msg, c_change,
        #                      task_split_md=f'{requirement_name}_任务单.md',
        #                      detailed_design_md=f'{requirement_name}_详细设计.md',
        #                      review_md=f'{requirement_name}_代码评审记录_C1.md',
        #                      review_json=f'{requirement_name}_评审记录_C1.json'
        #                      ))
