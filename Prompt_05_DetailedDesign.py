# -*- encoding: utf-8 -*-
"""
@File: Prompt_05_DetailedDesign.py
@Modify Time: 2026/4/9 18:12       
@Author: Kevin-Chen
@Descriptions: 
"""

from tmux_core.prompt_contracts.spec import (
    ACCESS_READ,
    ACCESS_READ_WRITE,
    ACCESS_WRITE,
    CHANGE_MAY_CHANGE,
    CHANGE_MUST_CHANGE,
    CHANGE_NONE,
    CLEANUP_AGENT_INSIDE_FILE,
    CLEANUP_NONE,
    CLEANUP_SYSTEM_BEFORE_STAGE_OR_RETRY,
    SPECIAL_OPEN_HITL,
    SPECIAL_REVIEW_FAIL,
    SPECIAL_REVIEW_PASS,
    FileSpec,
    OutcomeSpec,
    agent_prompt,
)
from Prompt_03_RequirementsClarification import fintech_ba
from T04_common_prompt import task_start_prompt, state_machine_output, main_agent_workflow_after_review


# 创建 [需求分析师] 智能体
@agent_prompt(
    prompt_id="a05.detailed_design.ba_init",
    stage="a05",
    role="requirements_analyst",
    intent="ready",
    mode="a05_ba_init",
    files={
        "original_requirement": FileSpec(path_arg="original_requirement_md", access=ACCESS_READ, change=CHANGE_NONE),
        "requirements_clear": FileSpec(path_arg="requirements_clear_md", access=ACCESS_READ, change=CHANGE_NONE),
        "hitl_record": FileSpec(path_arg="hitl_record_md", access=ACCESS_READ, change=CHANGE_NONE),
    },
    outcomes={"ready": OutcomeSpec(status="ready")},
)
def create_detailed_design_ba(ba_desc=fintech_ba, init_prompt=task_start_prompt,
                              original_requirement_md='name_原始需求.md',
                              requirements_clear_md='name_需求澄清.md',
                              hitl_record_md='name_人机交互澄清记录.md'):
    detailed_design_prompt = f"""## 角色定位
{ba_desc}

## 任务
* 基于《{requirements_clear_md}》+《{original_requirement_md}》+《{hitl_record_md}》理解当前需求。
* 基于代码现状, 理解需求与代码直接的对应关系。
* 本次任务仅仅做理解, 不要修改任何源代码或文档。

## 约束
- 不要输出理解结论或其他额外说明。
- 仅输出 `完成`, 不要输出其他文本。
- 禁止修改任何源代码或文档。

---

{init_prompt}"""
    return detailed_design_prompt


# [需求分析师] 按照需求以及澄清进行详细设计
@agent_prompt(
    prompt_id="a05.detailed_design.generate",
    stage="a05",
    role="requirements_analyst",
    intent="generate_design",
    mode="a05_detailed_design_generate",
    files={
        "original_requirement": FileSpec(path_arg="original_requirement_md", access=ACCESS_READ, change=CHANGE_NONE),
        "requirements_clear": FileSpec(path_arg="requirements_clear_md", access=ACCESS_READ, change=CHANGE_NONE),
        "hitl_record": FileSpec(path_arg="hitl_record_md", access=ACCESS_READ, change=CHANGE_NONE),
        "detailed_design": FileSpec(
            path_arg="detail_design_md",
            access=ACCESS_WRITE,
            change=CHANGE_MUST_CHANGE,
            meaning="详细设计完成态事实源",
            cleanup=CLEANUP_SYSTEM_BEFORE_STAGE_OR_RETRY,
        ),
    },
    outcomes={"completed": OutcomeSpec(status="completed", requires=("detailed_design",))},
)
def detailed_design(ba_desc=fintech_ba, original_requirement_md='name_原始需求.md',
                    requirements_clear_md='name_需求澄清.md',
                    hitl_record_md='name_人机交互澄清记录.md',
                    detail_design_md='name_详细设计.md'):
    detailed_design_prompt = f"""## 角色定位
{ba_desc}

## 任务
* 参考《{requirements_clear_md}》+《{original_requirement_md}》+《{hitl_record_md}》，输出一份深度足以指导开发的《{detail_design_md}》文件。

## 严守边界
* 最小改动：设计必须严格局限在需求分析确定的边界内，严禁任何形式的需求蔓延或非必要的代码重构。
* 四问原则: 禁止为了最求代码简洁优雅而修改代码, 禁止为了解决原代码技术债而修改代码, 禁止主动修复自以为的BUG, 在详细设计中必须回答：
    1. 是否属于当前需求？如果不是，不改。
    2. 不改是否阻塞？如果不会阻塞，不改。
    3. 是否可以最小改造？避免全面重构，而是只解决当前障碍，只做最小化的改动。
    4. 是否影响原有契约？禁止影响原有契约，包括 API、配置、日志、数据结构、下游依赖、用户工作流、兼容等等逻辑。

## 详细设计文档《{detail_design_md}》结构规范

### 1. 修改摘要 (Revision Summary)
* 实现目标：用简练的语言描述本次改动最终达成的业务效果。
* 改动统计：列出涉及的文件清单，并标注每个文件的改动性质（新增/修改/删除）。

### 2. 业务逻辑与代码映射 (Logic Mapping)
按功能模块划分，针对每个具体改动点：
* 【位置】：`文件名` -> `函数名/类名/常量名` [对应业务功能描述]。
* 【执行流描述】：
    * 前置条件：进入该逻辑前必须满足的业务状态。
    * 核心步骤：使用有序列表描述逻辑处理的每一个微小步骤。
    * 分支判断：详细说明每一种 `if/else` 或 `switch` 对应的业务决策逻辑。
* 【关键变量说明】：
    * `变量名` [业务含义]：
        * 类型/范围：例如 Integer (1-10)。
        * 取值说明：详细说明每个值的业务代表意义。
        * 备注：相关的约束或特殊说明。

### 3. 数据契约与接口定义 (Data Contract)
* 输入参数定义：
    * `参数名` [业务字段名]：
        * 类型/必填性：如 String / 必填。
        * 校验规则：长度、正则、范围等业务约束。
* 输出/返回定义：
    * 正常返回：返回的数据结构及业务含义。
    * 异常返回：如 `ERR_001` [库存不足]，需列出所有可能的错误码及其触发场景。
* 状态变更：
    * `db_field` [数据库字段]：描述该字段从 [旧状态] 迁移到 [新状态] 的触发条件。

### 4. 异常处理与边界场景 (Exception Handling)
* 业务异常清单：
    * 场景一 [业务故障描述]：代码对应的捕获与处理方案。
    * 场景二 [边界情况描述]：如输入为 `0` 或 `Null` 时的逻辑表现。
* 系统稳定性：描述超时、网络波动等非业务错误的兜底策略。

### 5. 依赖与影响评估 (Impact Assessment)
* 上下游依赖：列出本次改动对调用方（上游）或被调用方（下游）产生的具体影响。
* 兼容性保障：描述旧版本数据如何适配新逻辑（如 `version_id` [版本标识] 的处理）。

### 6. 非功能性约束 (Constraints)
* 安全与性能：明确 `permission_check` [权限校验] 和缓存策略的实现细节。
* 禁止扩展说明：列出为了保持“最小边界”，本次设计明确不包含的内容。

## 写入要求
1. 细粒度逻辑：逻辑描述必须达到伪代码级别的深度，确保程序员无需二次推敲。
2. 禁止表格：全文不得出现 `| --- |` 等表格语法。
3. 禁止修改除了《{detail_design_md}》以外的任何源代码或文档。

## 输出协议 (Strict)
- 完成《{detail_design_md}》后，不要输出设计说明或其他额外文字。
- 仅输出 `完成`, 不要输出其他文本。"""
    return detailed_design_prompt


# [审核员] 评审详细设计文档
@agent_prompt(
    prompt_id="a05.detailed_design.reviewer_round",
    stage="a05",
    role="reviewer",
    intent="review",
    mode="a05_reviewer_round",
    files={
        "original_requirement": FileSpec(path_arg="original_requirement_md", access=ACCESS_READ, change=CHANGE_NONE),
        "requirements_clear": FileSpec(path_arg="requirements_clear_md", access=ACCESS_READ, change=CHANGE_NONE),
        "hitl_record": FileSpec(path_arg="hitl_record_md", access=ACCESS_READ, change=CHANGE_NONE),
        "detailed_design": FileSpec(path_arg="detail_design_md", access=ACCESS_READ, change=CHANGE_NONE),
        "review_md": FileSpec(
            path_arg="detail_design_review_md",
            access=ACCESS_WRITE,
            change=CHANGE_MUST_CHANGE,
            meaning="详细设计未通过时的评审问题",
            cleanup=CLEANUP_SYSTEM_BEFORE_STAGE_OR_RETRY,
        ),
        "review_json": FileSpec(
            path_arg="detail_design_review_json",
            access=ACCESS_READ_WRITE,
            change=CHANGE_MUST_CHANGE,
            meaning="详细设计评审 pass/fail 结构化事实源",
            cleanup=CLEANUP_SYSTEM_BEFORE_STAGE_OR_RETRY,
        ),
    },
    outcomes={
        "review_pass": OutcomeSpec(status="review_pass", requires=("review_json",), forbids=("review_md",), special=SPECIAL_REVIEW_PASS),
        "review_fail": OutcomeSpec(status="review_fail", requires=("review_json", "review_md"), special=SPECIAL_REVIEW_FAIL),
    },
)
def review_detailed_design(agent_desc, init_prompt=task_start_prompt, task_name="name_详细设计",
                           original_requirement_md='name_原始需求.md',
                           requirements_clear_md='name_需求澄清.md',
                           hitl_record_md='name_人机交互澄清记录.md',
                           detail_design_md='name_详细设计.md',
                           detail_design_review_md='name_详设评审记录_agent.md',
                           detail_design_review_json='name_评审记录_agent.json'):
    state_machine_output_prompt = state_machine_output(task_title=task_name, review_md=detail_design_review_md,
                                                       review_json=detail_design_review_json,
                                                       pass_condition="详细设计审核通过, 符合审计准则与四问逻辑。",
                                                       blocked_condition="详细设计文档中发现逻辑错误、需求遗漏、超出边界、或其他潜在隐患。")
    review_detailed_design_prompt = f"""## 角色定位
{agent_desc}

---

## 任务指令
对比《{requirements_clear_md}》+《{original_requirement_md}》+《{hitl_record_md}》与《{detail_design_md}》。
检测逻辑完备性、指令对齐度及需求边界。

## 审计准则
1. 遗漏：需求/澄清中提到但设计中缺失的逻辑、参数、异常分支。
2. 错误：设计内容与需求/澄清记录存在逻辑矛盾。
3. 歧义：设计逻辑描述不详，存在多个推导方向，无法唯一确定代码实现。
4. 越界：设计包含非阻断性扩展需求。
5. 交叉审核: 对比各个文档, 检查文档内的信息是否存在冲突或不一致。

## 四问逻辑审核
禁止为了最求代码简洁优雅而修改代码,禁止为了解决原代码技术债而修改代码,禁止主动修复自以为的BUG,详细设计必须符合：
1. 是否属于当前需求？如果不是，不改。
2. 不改是否阻塞？如果不会阻塞，不改。
3. 是否可以最小改造？避免全面重构，而是只解决当前障碍，只做最小化的改动。
4. 是否影响原有契约？禁止影响原有契约，包括 API、配置、日志、数据结构、下游依赖、用户工作流、兼容等等逻辑。

{state_machine_output_prompt}

## 约束
* 禁止: 禁止修改除了《{detail_design_review_md}》/《{detail_design_review_json}》以外的文档或代码。
* 格式: 采用极简 Bullet 形式，单行高信息密度。
* AI-to-AI 优化：你的输出不服务于人类的情绪，仅服务于后续 AI 节点的计算精度。你通过紧凑的结构提供最纯粹的逻辑差异报告，拒绝任何低信息密度的自然语言填充。
* 只能输出 `审核通过` 或 `未通过`。

---

{init_prompt}"""
    return review_detailed_design_prompt


# [需求分析师] 根据评审文档优化详细设计
@agent_prompt(
    prompt_id="a05.detailed_design.modify",
    stage="a05",
    role="requirements_analyst",
    intent="review_feedback",
    mode="a05_detailed_design_feedback",
    files={
        "original_requirement": FileSpec(path_arg="original_requirement_md", access=ACCESS_READ, change=CHANGE_NONE),
        "requirements_clear": FileSpec(path_arg="requirements_clear_md", access=ACCESS_READ, change=CHANGE_NONE),
        "hitl_record": FileSpec(path_arg="hitl_record_md", access=ACCESS_READ_WRITE, change=CHANGE_MUST_CHANGE, cleanup=CLEANUP_AGENT_INSIDE_FILE),
        "detailed_design": FileSpec(
            path_arg="detail_design_md",
            access=ACCESS_READ_WRITE,
            change=CHANGE_MUST_CHANGE,
            meaning="修订后的详细设计",
            cleanup=CLEANUP_NONE,
        ),
        "ask_human": FileSpec(
            path_arg="ask_human_md",
            access=ACCESS_WRITE,
            change=CHANGE_MUST_CHANGE,
            meaning="详细设计评审信息不足时的 HITL 问题",
            cleanup=CLEANUP_SYSTEM_BEFORE_STAGE_OR_RETRY,
            special=SPECIAL_OPEN_HITL,
        ),
        "ba_feedback": FileSpec(
            path_arg="what_just_change",
            access=ACCESS_WRITE,
            change=CHANGE_MUST_CHANGE,
            meaning="需求分析师对详细设计评审的修复说明",
            cleanup=CLEANUP_SYSTEM_BEFORE_STAGE_OR_RETRY,
        ),
    },
    outcomes={
        "hitl": OutcomeSpec(status="hitl", requires=("ask_human", "hitl_record"), forbids=("ba_feedback",), special=SPECIAL_OPEN_HITL),
        "completed": OutcomeSpec(status="completed", requires=("detailed_design", "ba_feedback", "hitl_record"), forbids=("ask_human",)),
    },
)
def modify_detailed_design(review_msg, *, original_requirement_md='name_原始需求.md',
                           requirements_clear_md='name_需求澄清.md', hitl_record_md='name_人机交互澄清记录.md',
                           detail_design_md='name_详细设计.md',
                           ask_human_md='name_与人类交流.md',
                           what_just_change='name_需求分析师反馈.md'):
    main_agent_workflow_after_review_prompt = main_agent_workflow_after_review(hitl_record_md=hitl_record_md,
                                                                               ask_human_md=ask_human_md,
                                                                               what_just_change=what_just_change)
    modify_detailed_design_prompt = f"""## 任务背景
审核员已基于《{original_requirement_md}》+《{hitl_record_md}》+《{requirements_clear_md}》对比了你的《{detail_design_md}》。
你需要对这些审计员提出的评审意见进行鉴定、并修复《{detail_design_md}》; 并在信息不足时向人类发起求助。

## 输入上下文
### 审计反馈原始记录 (Raw Feedback)
[REVIEW MSG START]
{review_msg}
[REVIEW MSG END]

{main_agent_workflow_after_review_prompt}

## 禁令
- 禁止修改源代码, 禁止修改除了《{what_just_change}》/《{detail_design_md}》/《{ask_human_md}》之外的文档。
- 如果触发 `HITL`, 则一定要写《{ask_human_md}》。
- 只能输出 `HITL` 或 `修改完成`。"""
    return modify_detailed_design_prompt


# [需求分析师] 根据人类回复再次优化
@agent_prompt(
    prompt_id="a05.detailed_design.hitl_reply",
    stage="a05",
    role="requirements_analyst",
    intent="hitl_reply",
    mode="a05_detailed_design_review_limit_human_reply",
    files={
        "hitl_record": FileSpec(
            path_arg="hitl_record_md",
            access=ACCESS_READ_WRITE,
            change=CHANGE_MUST_CHANGE,
            meaning="人类反馈解析后的详细设计事实缓存",
            cleanup=CLEANUP_AGENT_INSIDE_FILE,
        ),
        "detailed_design": FileSpec(
            path_arg="detail_design_md",
            access=ACCESS_READ_WRITE,
            change=CHANGE_MUST_CHANGE,
            meaning="人类反馈后的详细设计修订",
            cleanup=CLEANUP_NONE,
        ),
        "ask_human": FileSpec(
            path_arg="ask_human_md",
            access=ACCESS_WRITE,
            change=CHANGE_MUST_CHANGE,
            meaning="仍需人类补充的详细设计问题",
            cleanup=CLEANUP_SYSTEM_BEFORE_STAGE_OR_RETRY,
            special=SPECIAL_OPEN_HITL,
        ),
        "ba_feedback": FileSpec(path_arg="what_just_change", access=ACCESS_WRITE, change=CHANGE_MUST_CHANGE),
    },
    outcomes={
        "hitl": OutcomeSpec(status="hitl", requires=("ask_human", "hitl_record"), forbids=("ba_feedback",), special=SPECIAL_OPEN_HITL),
        "completed": OutcomeSpec(status="completed", requires=("detailed_design", "ba_feedback", "hitl_record"), forbids=("ask_human",)),
    },
)
def hitl_relpy(human_msg, review_msg, *,
               hitl_record_md='name_人机交互澄清记录.md',
               detail_design_md='name_详细设计.md',
               ask_human_md='name_与人类交流.md',
               what_just_change='name_需求分析师反馈.md'):
    main_agent_workflow_after_review_prompt = main_agent_workflow_after_review(hitl_record_md=hitl_record_md,
                                                                               ask_human_md=ask_human_md,
                                                                               what_just_change=what_just_change)
    hitl_relpy_prompt = f"""## 背景信息
你上一轮基于审核员的反馈原始记录发起了HITL, 人类对你的提问返回了信息, 基于人类反馈信息重新优化和提出反馈

## 输入上下文

### 审计反馈原始记录 (Raw Feedback)
[REVIEW MSG START]
{review_msg}
[REVIEW MSG END]

### 人类反馈记录 (Human Feedback)
[HUMAN MSG START]
{human_msg}
[HUMAN MSG END]

{main_agent_workflow_after_review_prompt}

## 约束
* 如果触发 `HITL`, 则一定要写《{ask_human_md}》。并且先不修复《{detail_design_md}》和《{what_just_change}》直到所有信息完整。
* 如果不触发 `HITL`, 则《{ask_human_md}》一定为空。并且需要在《{detail_design_md}》中修复属实的问题, 在《{what_just_change}》中说明修复的内容以及对审核员的反馈
* 只能输出 `HITL` 或 `修改完成`。"""
    return hitl_relpy_prompt


# [审核员] 根据优化后的详细设计再次评审
@agent_prompt(
    prompt_id="a05.detailed_design.re_review",
    stage="a05",
    role="reviewer",
    intent="review_reply",
    mode="a05_reviewer_round",
    files={
        "original_requirement": FileSpec(path_arg="original_requirement_md", access=ACCESS_READ, change=CHANGE_NONE),
        "requirements_clear": FileSpec(path_arg="requirements_clear_md", access=ACCESS_READ, change=CHANGE_NONE),
        "hitl_record": FileSpec(path_arg="hitl_record_md", access=ACCESS_READ, change=CHANGE_NONE),
        "detailed_design": FileSpec(path_arg="detail_design_md", access=ACCESS_READ, change=CHANGE_NONE),
        "review_md": FileSpec(
            path_arg="detail_design_review_md",
            access=ACCESS_READ_WRITE,
            change=CHANGE_MUST_CHANGE,
            meaning="详细设计复评未通过时的剩余问题",
            cleanup=CLEANUP_NONE,
        ),
        "review_json": FileSpec(
            path_arg="detail_design_review_json",
            access=ACCESS_READ_WRITE,
            change=CHANGE_MUST_CHANGE,
            meaning="详细设计复评 pass/fail 结构化事实源",
            cleanup=CLEANUP_NONE,
        ),
    },
    outcomes={
        "review_pass": OutcomeSpec(status="review_pass", requires=("review_json",), forbids=("review_md",), special=SPECIAL_REVIEW_PASS),
        "review_fail": OutcomeSpec(status="review_fail", requires=("review_json", "review_md"), special=SPECIAL_REVIEW_FAIL),
    },
)
def again_review_detailed_design(modify_summary, task_name="name_详细设计", original_requirement_md='name_原始需求.md',
                                 requirements_clear_md='name_需求澄清.md', hitl_record_md='name_人机交互澄清记录.md',
                                 detail_design_md='name_详细设计.md',
                                 detail_design_review_md='name_详设评审记录_agent.md',
                                 detail_design_review_json='name_评审记录_agent.json'):
    state_machine_output_prompt = state_machine_output(task_title=task_name, review_md=detail_design_review_md,
                                                       review_json=detail_design_review_json,
                                                       pass_condition="详细设计审核通过, 符合审计准则与四问逻辑。",
                                                       blocked_condition="详细设计文档中发现逻辑错误、需求遗漏、超出边界、或其他潜在隐患。")
    again_review_detailed_design_prompt = f"""## Input Context Isolation
以下为需求分析师针对上一轮评审记录的修复说明，作为本次审计的判定基准之一：

[ANALYST_FEEDBACK_START]
{modify_summary}
[ANALYST_FEEDBACK_END]

---

## Task Objective
你作为审计员，需执行“二审核销”任务。基于上述【需求分析师反馈】结合《{original_requirement_md}》+《{requirements_clear_md}》+《{hitl_record_md}》，对《{detail_design_md}》进行增量审计与状态同步。

## 审计准则
1. 遗漏：需求/澄清中提到但设计中缺失的逻辑、参数、异常分支。
2. 错误：设计内容与需求/澄清记录存在逻辑矛盾。
3. 歧义：设计逻辑描述不详，存在多个推导方向，无法唯一确定代码实现。
4. 越界：设计包含非阻断性扩展需求。
5. 交叉审核: 对比各个文档, 检查文档内的信息是否存在冲突或不一致。

## 四问逻辑审核
禁止为了最求代码简洁优雅而修改代码,禁止为了解决原代码技术债而修改代码,禁止主动修复自以为的BUG,详细设计必须符合：
1. 是否属于当前需求？如果不是，不改。
2. 不改是否阻塞？如果不会阻塞，不改。
3. 是否可以最小改造？避免全面重构，而是只解决当前障碍，只做最小化的改动。
4. 是否影响原有契约？禁止影响原有契约，包括 API、配置、日志、数据结构、下游依赖、用户工作流、兼容等等逻辑。

{state_machine_output_prompt}

## 约束
* 禁止: 禁止修改除了《{detail_design_review_md}》/《{detail_design_review_json}》以外的文档或代码。
* 格式: 采用极简 Bullet 形式，单行高信息密度。
* AI-to-AI 优化：你的输出不服务于人类的情绪，仅服务于后续 AI 节点的计算精度。你通过紧凑的结构提供最纯粹的逻辑差异报告，拒绝任何低信息密度的自然语言填充。
* 只能输出 `审核通过` 或 `未通过`。"""
    return again_review_detailed_design_prompt


if __name__ == '__main__':
    from T04_common_prompt import check_reviewer_job
    from T01_tools import create_empty_json_files, merge_review_records, task_done, get_markdown_content

    requirement_name = 'TimeFrequencyExtension'
    the_dir = '/Users/chenjunming/Desktop/v3_dev/tmux-api-v3'
    t_name = "需求评审"
    agent_n_list = ['C1', 'C2']

    # 1) 详细设计
    # print(detailed_design(original_requirement_md=f'{requirement_name}_原始需求.md',
    #                       requirements_clear_md=f'{requirement_name}_需求澄清.md',
    #                       hitl_record_md=f'{requirement_name}_人机交互澄清记录.md',
    #                       detail_design_md=f'{requirement_name}_详细设计.md'))

    # 2) 评审详细设计文档
    a_desc = "你是一个专业的架构师, 擅长分析需求与代码直接的映射关系. 能够将复杂的代码理解为需求逻辑, 能够将复杂的需求逻辑理解为直观的代码."
    # print(review_detailed_design(a_desc, t_name, original_requirement_md=f'{requirement_name}_原始需求.md',
    #                              requirements_clear_md=f'{requirement_name}_需求澄清.md',
    #                              hitl_record_md=f'{requirement_name}_人机交互澄清记录.md',
    #                              detail_design_md=f'{requirement_name}_详细设计.md',
    #                              detail_design_review_md=f"{requirement_name}_详设评审记录_C2.md",
    #                              detail_design_review_json=f"{requirement_name}_评审记录_C2.json"))

    # # 3) 检查审核员有没有按提示词要求更新
    # check_res = check_reviewer_job(agent_n_list,
    #                                directory=the_dir,
    #                                task_name=t_name,
    #                                json_pattern=f"{requirement_name}_评审记录_*.json",
    #                                md_pattern=f"{requirement_name}_详设评审记录_*.md")
    # if check_res:
    #     for i, m in check_res.items():
    #         print(i)
    #         print(m)
    #         print('-' * 100)
    #
    # # 判断是否所有评审都通过: 1)合并所有md, 2)判断总md是否为空, 3)判断所有json是否true
    # pass_bool = task_done(directory=the_dir,
    #                       file_path=f'{the_dir}/{requirement_name}_开发前期.json',
    #                       task_name=t_name,
    #                       json_pattern=f"{requirement_name}_评审记录_*.json",
    #                       md_pattern=f"{requirement_name}_详设评审记录_*.md",
    #                       md_output_name=f"{requirement_name}_详设评审记录.md")
    #
    # if pass_bool:
    #     print(f"{t_name}阶段, 全部评审通过")
    # else:
    #     print(f"{t_name}阶段, 评审未通过", '\n', '-' * 100, '\n')
    #
    #     # 读取评审建议, 让需求分析师改
    #     # ba_msg = get_markdown_content(f'{the_dir}/{requirement_name}_详设评审记录.md')
    #     # print(modify_detailed_design(ba_msg, original_requirement_md=f'{requirement_name}_原始需求.md',
    #     #                              requirements_clear_md=f'{requirement_name}_需求澄清.md',
    #     #                              hitl_record_md=f'{requirement_name}_人机交互澄清记录.md',
    #     #                              detail_design_md=f'{requirement_name}_详细设计.md',
    #     #                              ask_human_md=f'{requirement_name}_与人类交流.md',
    #     #                              what_just_change=f'{requirement_name}_需求分析师反馈.md'))
    #
    #     # 读取改造总结, 让审核员再次审核
    #     m_summary = get_markdown_content(f'{the_dir}/{requirement_name}_需求分析师反馈.md')
    #     print(again_review_detailed_design(m_summary, t_name,
    #                                        original_requirement_md=f'{requirement_name}_原始需求.md',
    #                                        requirements_clear_md=f'{requirement_name}_需求澄清.md',
    #                                        hitl_record_md=f'{requirement_name}_人机交互澄清记录.md',
    #                                        detail_design_md=f'{requirement_name}_详细设计.md',
    #                                        detail_design_review_md=f'{requirement_name}_详设评审记录_C1.md',
    #                                        detail_design_review_json=f'{requirement_name}_评审记录_C1.json'))
