# -*- encoding: utf-8 -*-
"""
@File: T04_common_prompt.py
@Modify Time: 2026/4/13 12:08
@Author: Kevin-Chen
@Descriptions:
"""

from __future__ import annotations

import json
from pathlib import Path
from T01_tools import check_task_exists, is_file_empty, get_task_review_status, check_string_in_markdown

# 多个阶段复用的智能体任务启动提示词
task_start_prompt = """Before proceeding with this task, read the routing layer first. Do **not** start with a full repo scan.

### Step 1 — Read only these files first

* `AGENTS.md`
* `docs/repo_map.json`
* `docs/task_routes.json`
* `docs/pitfalls.json`

At this stage:

* do **not** read large amounts of implementation code
* do **not** broad-search the repo
* do **not** jump into tests/configs/schemas yet
* do **not** propose fixes or conclusions

### Step 2 — Output a Task Routing Assessment first

Before reading code, output exactly these sections:

* `Project Overview`
* `Task Categorization`
* `High-Probability Modules`
* `High-Probability File Paths`
* `Implicit Risks`
* `Next Steps`

Rules:

* use module IDs / route info from the routing layer when available
* list only likely first-hop files grounded in the routing layer
* `Next Steps` must be verification steps, not solutions

### Step 3 — Then verify in code

Only after the routing assessment, read code to verify:

* actual logic
* function signatures
* call chains
* dependencies
* related tests
* configs
* schemas

### Step 4 — Only then propose solutions

Do **not** output:

* fix plans
* root-cause claims
* implementation conclusions
* final recommendations

until code verification is complete.

### Hard rules

* routing layer = navigation only, not source of truth
* code/tests/configs/schemas override routing docs
* no guessing from docs alone
* start narrow, expand only if needed
* after first-hop files, expand in this order:

  1. directly referenced files
  2. related tests
  3. related configs/schemas
  4. adjacent callers/callees
  5. broader search only if necessary

### Output order

1. `Task Routing Assessment`
2. `Code Fact Verification`
3. `Solution / Recommendation`

**Output the Task Routing Assessment first, before reading implementation code.**"""


def build_turn_status_contract_prompt(
        *,
        turn_status_path: str | Path,
        turn_id: str,
        turn_phase: str,
        stage_status_path: str | Path,
        turn_status_schema_version: str = "1.0",
) -> str:
    turn_status_text = str(Path(turn_status_path).expanduser().resolve())
    stage_status_text = str(Path(stage_status_path).expanduser().resolve())
    example = {
        "schema_version": turn_status_schema_version,
        "turn_id": turn_id,
        "phase": turn_phase,
        "status": "done",
        "artifacts": {
            "stage_status": stage_status_text,
            "primary_artifact": "<non_empty_file_path_referenced_by_stage_status_json>",
        },
        "artifact_hashes": {
            stage_status_text: "sha256:<hex>",
            "<non_empty_file_path_referenced_by_stage_status_json>": "sha256:<hex>",
        },
        "written_at": "<ISO8601 timestamp>",
    }
    return (
        "Runtime completion file contract:\n"
        f"- This turn_id is `{turn_id}`.\n"
        f"- First finish the stage status file `{stage_status_text}`.\n"
        f"- Then write `{turn_status_text}` as the final completion file.\n"
        f"- `{turn_status_text}` must be valid JSON using this shape:\n"
        f"{json.dumps(example, ensure_ascii=False, indent=2)}\n"
        f"- `artifacts.stage_status` must equal `{stage_status_text}`.\n"
        f"- Also include every non-empty file path referenced by `{stage_status_text}` as additional artifact entries.\n"
        "- Compute real sha256 hashes for every path listed in `artifacts` and store them in `artifact_hashes`.\n"
        f"- `{turn_status_text}` must be written last.\n"
        "- stdout is not the completion protocol.\n"
    )


def build_hitl_status_contract_prompt(
        *,
        stage_status_path: str | Path,
        stage_name: str,
        turn_id: str,
        hitl_round: int,
        output_path: str | Path,
        question_path: str | Path,
        record_path: str | Path,
        status_schema_version: str = "1.0",
) -> str:
    stage_status_text = str(Path(stage_status_path).expanduser().resolve())
    output_text = str(Path(output_path).expanduser().resolve())
    question_text = str(Path(question_path).expanduser().resolve())
    record_text = str(Path(record_path).expanduser().resolve())
    example = {
        "schema_version": status_schema_version,
        "stage": stage_name,
        "turn_id": turn_id,
        "hitl_round": hitl_round,
        "status": "completed|hitl|error",
        "summary": "<single_line_summary>",
        "output_path": output_text,
        "question_path": question_text,
        "record_path": record_text,
        "artifact_hashes": {
            output_text: "sha256:<hex>",
            question_text: "sha256:<hex>",
            record_text: "sha256:<hex>",
        },
        "written_at": "<ISO8601 timestamp>",
    }
    return (
        "Runtime stage status contract:\n"
        f"- Write `{stage_status_text}` on every turn using this JSON shape:\n"
        f"{json.dumps(example, ensure_ascii=False, indent=2)}\n"
        f"- `stage` must equal `{stage_name}`.\n"
        f"- `turn_id` must equal `{turn_id}`.\n"
        f"- `hitl_round` must equal `{hitl_round}`.\n"
        f"- If `status` is `completed`, `output_path` must equal `{output_text}` and the file must exist and be non-empty.\n"
        f"- If `status` is `hitl`, `question_path` must equal `{question_text}` and the file must exist and be non-empty.\n"
        f"- If `status` is `hitl`, `record_path` must equal `{record_text}` and the file must exist after this turn.\n"
        f"- If `status` is `completed`, `record_path` may be empty or `{record_text}` if you updated it.\n"
        "- Use `error` only for unexpected runtime/tooling failures that cannot be expressed as HITL.\n"
        "- `artifact_hashes` must contain every non-empty file path referenced by this status JSON.\n"
        "- stdout is not the state protocol.\n"
    )


# [审核智能体] 通用输出与交互协议
def state_machine_output(task_title, review_md='name_代码评审记录_AgentName.md',
                         review_json='name_评审记录_AgentName.json',
                         pass_condition="代码逻辑无瑕疵，完全对齐需求与设计。",
                         blocked_condition="发现逻辑错误、需求遗漏、架构分歧或潜在隐患。"):
    """
    智能体输出与交互协议

    :param task_title:
    :param review_md:
    :param review_json:
    :param pass_condition:
    :param blocked_condition:
    :return:
    """
    exact_task_name_json = json.dumps(str(task_title), ensure_ascii=False)
    state_machine_output_prompt = f"""## 交互协议 (State Machine Output)

### JSON task_name 精确匹配要求
`task_name` 字段必须逐字等于这个 JSON 字符串值，不要删除反引号、标点、空格或代码标识符：
```json
{exact_task_name_json}
```

### 场景 A：审核通过 (Pass)
**触发条件**：{pass_condition}
**操作流水线**：
1. **清理报告**：创建/清空本地文件《{review_md}》（使其为空文件）。
2. **JSON 持久化**：对《{review_json}》中的 **JSON List** 执行以下逻辑：
   - 若 List 中已存在 `task_name` 为 `{task_title}` 的对象，更新其 `review_pass` 为 `true`。
   - 若不存在，则在 List 末尾追加新对象：`{{"task_name": "{task_title}", "review_pass": true}}`。
3. **反馈**：**仅**返回字符串 `审核通过`。**禁止**返回其他文字内容。

### 场景 B：存在瑕疵 (Blocked)
**触发条件**：{blocked_condition}
**操作流水线**：
1. **审计定格**：将审计发现覆盖写入本地文件《{review_md}》(完全覆盖旧内容)。
   - **格式**：Bullet Point；高信息密度；AI机器友好。
   - **标签**：明确区分 `[Error]` (错误/遗漏) 与 `[Ambiguity]` (疑问/歧义)。
   - **内容**：指明具体问题。
2. **JSON 持久化**：对《{review_json}》中的 **JSON List** 执行以下逻辑：
   - 若 List 中已存在 `task_name` 为 `{task_title}` 的对象，更新其 `review_pass` 为 `false`。
   - 若不存在，则在 List 末尾追加新对象：`{{"task_name": "{task_title}", "review_pass": false}}`。
3. **反馈**：**仅**返回字符串 `未通过`。**禁止**返回其他文字内容。"""
    return state_machine_output_prompt


# [主智能体] 通用输出与交互协议
def main_agent_workflow_after_review(*, hitl_record_md=None,
                                     ask_human_md=None,
                                     what_just_change='name_需求分析师反馈.md'):
    # 有人类反馈信息, 需要人类反馈
    if hitl_record_md and ask_human_md:
        main_agent_output_prompt = f"""## Workflow (SOP)

### Step 1: 解析与过滤 (Parse & Filter)
对人类反馈进行拆解分类：
1. **[提问]**：人类向你提出的疑问或反问。
2. **[有效信息]**：陈述了缺口、重新定义了业务边界、或补充了其他规则。
3. **[噪音]**：情绪化表达、无关紧要的废话（直接丢弃）。

### Step 2: 冲突校验与文档同步 (Conflict Check & Sync)
将解析出的 [有效信息] 与《{hitl_record_md}》中的存量记录进行交叉逻辑校验，并严格按以下三种动作执行文档更新：
1. **[追加] (Append - 纯新增信息)**：
   若信息为全新的补充，且不与历史记录冲突，直接结构化追加至文档中。
2. **[拦截] (Block & Flag - 隐式冲突)**：
   若新信息与存量记录存在矛盾，且人类 **未明确** 表达变更意图，禁止将此信息写入文档。必须将其标记为 [待确认冲突]，留待 Step 3 继续向人类追问。
3. **[覆写] (Overwrite - 显式变更)**：
   若人类明确发出了修改指令（例如：“之前的不对了，改成…”、“以这个为准”），你必须在《{hitl_record_md}》中 **精准定位并物理删除(或直接替换)** 旧的无效信息，更新为最新规则。**绝对禁止** 在新旧规则同时并存于文档中，必须保持文档的唯一真理来源（Single Source of Truth）。

### Step 3: 审计项预处理 (Deduplication & Pre-processing)
在执行修复前，你需要先完成：
1. **语义去重**：扫描原始审核记录，将不同审核员针对同一逻辑点、同一代码的重复问题描述合并为单一审计项。
2. **冲突消解**：若不同审核员的意见存在矛盾，必须基于已有文档记录, 代码事实, 以及人类反馈进行判定，并在元数据中记录你的取舍理由。

### Step 4: 评审意见解析与定性 (Parse & Categorize)
逐条分析, 并将其归类为：
1. **[属实问题]**：确实存在的逻辑漏洞、边界遗漏、与原始约束冲突、或其他属实的问题。
2. **[误判问题]**：审计员理解有误，实际上不是问题, 并分析驳回理由。
3. **[待决疑问/歧义]**：存在多种业务解释分支或者疑问点。判断当前所有输入文档和已有信息是否足以做出唯一的业务判定？
  - **是**：直接做出业务逻辑决断。
  - **否**：标记为 `HITL` 阻断项, 需要向人类询问。

### Step 5: 状态路由与输出 (State Routing & Output)
> 根据 Step 4 的校验结果，严格选择以下 **唯一** 一条路径进行输出：

#### 路径 A: [触发 HITL] (存在无法决断的歧义或疑问)
如果发现任何超出现有上下文、必须人类拍板的业务分歧 或 当前信息不足以决策时 或 需要回答人类提问时，必须直接触发 HITL。
1. 将问题写入本地文件《{ask_human_md}》, 若已经存在《{ask_human_md}》则覆盖。
    - 要求：使用分层 Bullet point；直截了当说明阻断原因，不加修饰词，人类友好的语句。要假设人类没看过代码。
    - 写入格式 (Metadata Block)：
```md
- [发现的问题]：描述代码逻辑与需求之间的断层/冲突
- [缺失的信息]：明确需要人类补充的业务规则或判定标准
- [建议/影响]：如果不明确此点，会导致什么技术后果
```
2. 输出：
```text
HITL
```

#### 路径 B: [内部闭环反馈] (所有评审项均可依靠现有信息解决)
1. 修复 [属实问题]
2. 在《{what_just_change}》中说明修复了哪些 [属实问题], 驳回 [误判问题], 以及对 [待决疑问/歧义] 的答复.
    - 要求：AI-to-AI 友好，高信息密度，禁止任何修饰性词汇。
    - 写入格式 (Metadata Block)：
```md
- [属实问题]: 说明修复了哪些属实的问题以及改了哪里
- [误判问题]: 明确反驳理由
- [待决疑问/歧义]: 答复疑问和裁决歧义
```
3. 输出：
```text
修改完成
```"""
    # 无人类反馈信息, 但是需要人类反馈
    elif ask_human_md:
        main_agent_output_prompt = f"""## Workflow (SOP)

### Step 1: 审计项预处理 (Deduplication & Pre-processing)
在执行修复前，你需要先完成：
1. **语义去重**：扫描原始审核记录，将不同审核员针对同一逻辑点、同一代码的重复问题描述合并为单一审计项。
2. **冲突消解**：若不同审核员的意见存在矛盾，必须基于已有文档记录和代码事实进行判定，并在元数据中记录你的取舍理由。

### Step 2: 评审意见解析与定性 (Parse & Categorize)
逐条分析, 并将其归类为：
1. **[属实问题]**：确实存在的逻辑漏洞、边界遗漏、与原始约束冲突、或其他属实的问题。
2. **[误判问题]**：审计员理解有误，实际上不是问题, 并分析驳回理由。
3. **[待决疑问/歧义]**：存在多种业务解释分支或者疑问点。判断当前所有输入文档和已有信息是否足以做出唯一的业务判定？
  - **是**：直接做出业务逻辑决断。
  - **否**：标记为 `HITL` 阻断项, 需要向人类询问。

### Step 3: 状态路由与输出 (State Routing & Output)
> 根据 Step 2 的校验结果，严格选择以下 **唯一** 一条路径进行输出：

#### 路径 A: [触发 HITL] (存在无法决断的歧义或疑问)
如果发现任何超出现有上下文、必须人类拍板的业务分歧 或 当前信息不足以决策时，必须直接触发 HITL。
1. 将问题写入本地文件《{ask_human_md}》, 若已经存在《{ask_human_md}》则覆盖。
    - 要求：使用分层 Bullet point；直截了当说明阻断原因，不加修饰词，人类友好的语句。要假设人类没看过代码。
    - 写入格式 (Metadata Block)：
```md
- [发现的问题]：描述代码逻辑与需求之间的断层/冲突
- [缺失的信息]：明确需要人类补充的业务规则或判定标准
- [建议/影响]：如果不明确此点，会导致什么技术后果
```
2. 输出：
```text
HITL
```

#### 路径 B: [内部闭环反馈] (所有评审项均可依靠现有信息解决)
1. 修复 [属实问题]
2. 在《{what_just_change}》中说明修复了哪些 [属实问题], 驳回 [误判问题], 以及对 [待决疑问/歧义] 的答复.
    - 要求：AI-to-AI 友好，高信息密度，禁止任何修饰性词汇。
    - 写入格式 (Metadata Block)：
```md
- [属实问题]: 说明修复了哪些属实的问题以及改了哪里
- [误判问题]: 明确反驳理由
- [待决疑问/歧义]: 答复疑问和裁决歧义
```
3. 输出：
```text
修改完成
```"""
    # 无人类反馈信息, 无需人类反馈
    else:
        main_agent_output_prompt = f"""## Workflow (SOP)

### Step 1: 审计项预处理 (Deduplication & Pre-processing)
在执行修复前，你需要先完成：
1. **语义去重**：扫描原始审核记录，将不同审核员针对同一逻辑点、同一代码的重复问题描述合并为单一审计项。
2. **冲突消解**：若不同审核员的意见存在矛盾，必须基于已有文档记录和代码事实进行判定，并在元数据中记录你的取舍理由。

### Step 2: 评审意见解析与定性 (Parse & Categorize)
逐条分析, 并将其归类为：
1. **[属实问题]**：确实存在的逻辑漏洞、边界遗漏、与原始约束冲突、或其他属实的问题。
2. **[误判问题]**：审计员理解有误，实际上不是问题, 并分析驳回理由。
3. **[待决疑问/歧义]**：如果存在多种业务解释分支或者疑问点。需要根据已有文档详细做出业务逻辑决断。

### Step 3: 状态路由与输出 (State Routing & Output)
1. 修复 [属实问题]
2. 在《{what_just_change}》中说明修复了哪些 [属实问题], 驳回 [误判问题], 以及对 [待决疑问/歧义] 的答复.
    - 要求：AI-to-AI 友好，高信息密度，禁止任何修饰性词汇。
    - 写入格式 (Metadata Block)：
```md
- [属实问题]: 说明修复了哪些属实的问题以及改了哪里
- [误判问题]: 明确反驳理由
- [待决疑问/歧义]: 答复疑问和裁决歧义
```
3. 输出：
```text
修改完成
```"""
    return main_agent_output_prompt


def check_reviewer_job(agent_name_list, directory, task_name="M1-T1",
                       json_pattern="代码评审记录_*.json", md_pattern="代码评审记录_*.md"):
    print("检查各个审核智能体是否按照操作要求更新了文档...")
    agent_job_check_dict = dict()
    for agent_name in agent_name_list:
        print(agent_name)
        json_file_name = json_pattern.replace('*', agent_name)
        res = check_task_exists(Path(f'{directory}/{json_file_name}'), task_name)
        if not res:
            print(f"{json_file_name} 中不存在 {task_name}")
            agent_job_check_dict[agent_name] = f"""## 协议违态提醒 (Protocol Violation)
检测到你未按【交互协议】更新任务索引文件。

**缺失操作**：
你已完成评审，但《{json_file_name}》中缺失 `{task_name}` 的状态记录。

**强制补全指令**：
立即对《{json_file_name}》中的 **JSON List** 执行以下原子操作：
- 若 `{task_name}` 存在，按照评审结论更新其 `review_pass` 为 `false`或`true`；
- 若不存在，追加对象：`{{"task_name": "{task_name}", "review_pass": false 或 true}}`。

**禁止输出任何解释，执行后仅返回 `审核通过`或`未通过`。**"""
        else:
            md_file_name = md_pattern.replace('*', agent_name)
            # 判断 md_file_name 是否为空 (文件不存在视为空)
            md_bool = is_file_empty(Path(f'{directory}/{md_file_name}'))
            json_bool = get_task_review_status(Path(f'{directory}/{json_file_name}'), task_name)
            if md_bool != json_bool:
                state_machine_output_prompt = state_machine_output(
                    task_name, review_md=md_file_name, review_json=json_file_name,
                    pass_condition="代码逻辑无瑕疵，完全对齐需求与设计。",
                    blocked_condition="发现逻辑错误、需求遗漏、架构分歧或潜在隐患。")
                if md_bool and not json_bool:
                    reason_prompt = f"- **逻辑矛盾**：审计文档《{md_file_name}》为空（暗示通过），但状态索引《{json_file_name}》记录为 `false`。"
                else:
                    reason_prompt = f"- **逻辑矛盾**：审计文档《{md_file_name}》存在错误记录，但状态索引《{json_file_name}》却标记为 `true`。"
                agent_job_check_dict[
                    agent_name] = f"""## 审计状态冲突 (Audit Conflict Detected)
你在本次评审中产生的物理文件状态不一致，请立即根据以下逻辑进行【状态对齐】：

**矛盾原因**：
{reason_prompt}

**对齐要求**：
{state_machine_output_prompt}

**执行禁令**：
- 禁止修改源代码。
- 必须确保《{md_file_name}》的内容与《{json_file_name}》的 `review_pass` 布尔值具有物理一致性。
- 修正后仅返回字符串：`审核通过`或`未通过`。"""
        print("-" * 20)
    return agent_job_check_dict


def check_develop_job(file_path, task_name, task_split_md='任务单.md', what_just_dev='工程师开发内容.md'):
    check_job = check_string_in_markdown(file_path, task_name)
    if not check_job:
        return f"""## 协议违态提醒 (Protocol Violation)
检测到你已完成《{task_split_md}》中的 `{task_name}` 任务的代码开发，但未按【交互协议】同步更新元数据文档。

**缺失操作**：
你必须将本次变更的内容覆盖写入《{what_just_dev}》。

**强制写入格式 (Metadata Block)**：
要求：AI-to-AI 友好，高信息密度，禁止任何修饰性词汇。必须包含以下内容：
```md
- **完成任务**: `{task_name}`
- **变更说明**:
  - `[文件路径A]`: {{ `[函数/类]`: `[原逻辑] -> [新逻辑]`, `[Impact]`: `[影响]` }}
  - `[文件路径B]`: {{ `[函数/类]`: `[原逻辑] -> [新逻辑]`, `[Impact]`: `[影响]` }}
- **Breaking Changes**: `None` | `[描述破坏性变更]`
```

**执行禁令**：
禁止输出任何关于代码逻辑的解释、致谢或后续计划。
写入完成后，仅返回字符串：`任务完成`"""
    else:
        return None


if __name__ == '__main__':
    task = "M4-T2"
    check_res = check_reviewer_job(['G1', 'G2', 'Q', 'CC'],
                                   directory="/Users/chenjunming/Desktop/Canopy/canopy-api-v3",
                                   task_name=task)
    if check_res:
        for i, m in check_res.items():
            print(i)
            print(m)
            print('-' * 100)
    check_res = check_develop_job('/Users/chenjunming/Desktop/Canopy/canopy-api-v3/工程师开发内容.md', task,
                                  task_split_md='任务单.md', what_just_dev='工程师开发内容.md')
    print(check_res)
