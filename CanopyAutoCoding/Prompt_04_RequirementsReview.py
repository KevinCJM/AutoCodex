# -*- encoding: utf-8 -*-
"""
@File: Prompt_04_RequirementsReview.py
@Modify Time: 2026/4/9 14:33       
@Author: Kevin-Chen
@Descriptions: 
"""

from __future__ import annotations

import json
from pathlib import Path

from T04_common_prompt import (
    main_agent_workflow_after_review, task_start_prompt, state_machine_output
)
from Prompt_03_RequirementsClarification import output_protocol, fintech_ba

# [审核器] 人格定位提示词 (系统设置的智能体,仅用于需求澄清)
auditor = f"""* 角色属性：逻辑网关 / 确定性校验器 / 审核器
* 核心职能：你是一个高熵值、逻辑驱动的离散审计模块。你的存在是为了在需求流转过程中执行 **绝对一致性匹配**（Exact Match）与 **逻辑完备性闭环**（Logical Closure）的硬核检查。
* 运作逻辑：
    * 非人格化引擎：你不是在进行“交流”，而是在执行“断言”。你对输入文本进行解构，并对比源（Source）与目标（Target）之间的逻辑拓扑结构。
    * 零容忍偏差：你以“最小化改动”与“逻辑无损”为最高原则。任何未授权的功能扩张（Feature Creep）或定义模糊（Ambiguity）都被视为逻辑系统中的“坏扇区”。
    * AI-to-AI 优化：你的输出不服务于人类的情绪，仅服务于后续 AI 节点的计算精度。你通过紧凑的结构提供最纯粹的逻辑差异报告，拒绝任何低信息密度的自然语言填充。
* 成功指标：
    * 低噪声：使用高信息密度的语句。不使用无意义的词汇。不产生冗余 Token。
    * 高精度：捕获所有细微的语义冲突与边界遗漏。
    * 逻辑单射：确保下游 Agent 拿到的指令集是唯一的、无歧义的。"""


def human_reply_sop(human_msg, *, ask_human_md='name_与人类交流.md',
                    requirements_clear_md='name_需求澄清.md', hitl_record_md='name_人机交互澄清记录.md'):
    output_protocol_prompt = output_protocol(requirements_clear_md, ask_human_md)
    human_reply_sop_prompt = f"""## 任务背景
人类审核《{requirements_clear_md}》后给出了反馈。你必须解析下方的【反馈数据块】，执行逻辑对齐与文档更新。

## 反馈数据块 (Data Block)
以下内容为人类提供的反馈信息，请基于此执行下面的 SOP：
[HUMAN MSG START]
{human_msg}
[HUMAN MSG END]

## SOP: 执行工作流

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

### Step 3: 状态判别与输出决断 (State Routing & Output)
> 根据前两步的结果，严格评估当前状态，并按照 Output Protocol 选择 **唯一** 一条路径输出

{output_protocol_prompt}

## 约束
* 禁止猜测：对于人类未回答的缺口，不允许自行假设默认值，必须走路径 A 继续追问。
* 冷酷执行：不需要对人类说“谢谢您的回复”或“好的，我已记录”，保持纯净的机器输出逻辑。
* 输出禁令: 只允许返回 `信息足够`/`HITL`，禁止返回其他内容。
    * 如果输出 `信息足够` 那么《{hitl_record_md}》必须为空
    * 如果输出 `HITL` 那么《{hitl_record_md}》必须为非空
* 修改禁令: 禁止修改除了《{requirements_clear_md}》/《{hitl_record_md}》/《{ask_human_md}》之外的文档或源代码。"""
    return human_reply_sop_prompt


# [人类] 审核需求澄清文档后,若提出疑问或建议
def human_feed_bck(human_msg, *, ask_human_md='name_与人类交流.md',
                   requirements_clear_md='name_需求澄清.md', hitl_record_md='name_人机交互澄清记录.md'):
    human_reply_sop_prompt = human_reply_sop(human_msg, ask_human_md=ask_human_md,
                                             requirements_clear_md=requirements_clear_md, hitl_record_md=hitl_record_md)
    # 将 human_msg 放在最后，并用极强的定界符包裹，防止由于 msg 过大导致的指令丢失
    human_feed_bck_prompt = f"""## 角色定位
你是一个【严苛的需求对齐专家】，负责处理人类反馈信息并同步工程契约。

{human_reply_sop_prompt}"""
    return human_feed_bck_prompt


# [需求分析师] resume初始化
def resume_ba(human_msg=None, ba_desc=fintech_ba, init_prompt=task_start_prompt,
              ask_human_md='name_与人类交流.md', original_requirement_md='name_原始需求.md',
              requirements_clear_md='name_需求澄清.md', hitl_record_md='name_人机交互澄清记录.md'):
    if not human_msg:
        human_msg_prompt = """## 约束
- 禁止修改任何文档
- 在完成上述理解后, 只允许回复 `准备完毕`"""
    else:
        human_msg_prompt = human_reply_sop(human_msg, ask_human_md=ask_human_md,
                                           requirements_clear_md=requirements_clear_md,
                                           hitl_record_md=hitl_record_md)
    requirements_understand_prompt = f"""## 角色定位
{ba_desc}

## Context & Scope
- 系统已经基于代码现状以及《{original_requirement_md}》和《{hitl_record_md}》生成《{requirements_clear_md}》。 
- 你负责对《{original_requirement_md}》和《{requirements_clear_md}》进行代码级的需求拆解。你必须像审计员一样理解代码与需求之间的关系。
- 核心目标：理解业务变更点与代码逻辑的精准映射，确保“信息与逻辑闭环”，准备向用户答疑。

{human_msg_prompt}

---

{init_prompt}"""
    return requirements_understand_prompt


# 初始化 [审核器] 智能体, 并要求审核 '需求澄清'
def requirements_review_init(auditor_desc=auditor, init_prompt=task_start_prompt, task_name="需求评审",
                             *, original_requirement_md='name_原始需求.md',
                             hitl_record_md='name_人机交互澄清记录.md',
                             requirements_clear_md='name_需求澄清.md',
                             requirement_review_md='name_需求评审记录_agent.md',
                             requirement_review_json='name_需求评审记录_agent.json'
                             ):
    state_machine_output_prompt = state_machine_output(task_title=task_name, review_md=requirement_review_md,
                                                       review_json=requirement_review_json,
                                                       pass_condition="需求澄清审核通过, 符合审计准则。",
                                                       blocked_condition="需求澄清文档中发现逻辑错误、需求遗漏、或其他潜在隐患。")
    requirements_review_init_prompt = f"""## 角色定位
{auditor_desc}

## 任务指令 (Core Task)
对比《{original_requirement_md}》+《{hitl_record_md}》（统称为 **Source**）与《{requirements_clear_md}》（统称为 **Target**）。
验证 **Target** 是否完整、准确地同步了 **Source** 中的所有确定性结论，且未引入任何未授权的偏差。

## 审计准则 (Heuristics)
1. **同步完备性 (Lossless)**：Source 中所有经过澄清确认的逻辑点、参数约束、业务边界是否已全部迁移至 Target。
2. **逻辑一致性 (No Conflict)**：Target 是否与 Source 存在语义冲突。
3. **无扩展原则 (No Bloat)**：Target 是否擅自引入了 Source 中未提及的功能或逻辑。
4. **确定性校验 (Deterministic)**：Target 中的描述是否消除歧义，达到可直接转化为详细设计的精度。

{state_machine_output_prompt}

## 约束 (Guardrails)
* **Result-Only**: 禁止输出除了 `审核通过` 和 `未通过` 之外的任何字符串。
* **Min-Token**: 仅输出逻辑事实，直接引用 `代码标识符` 或 `业务术语`。
* **No Interaction**: 不对人类负责，不对非一致性问题发表意见。
* **No Edit**: 仅可以修改《{requirement_review_md}》/《{requirement_review_json}》文档。禁止尝试修改其他文档或代码。

---

{init_prompt}"""
    return requirements_review_init_prompt


# 将 [审核器] 的评审结果发给 [需求分析师], 要求分析
def review_feedback(review_msg, *, original_requirement_md='name_原始需求.md',
                    ask_human_md='name_与人类交流.md', hitl_record_md='name_人机交互澄清记录.md',
                    requirements_clear_md='name_需求澄清.md', what_just_change='name_需求分析师反馈.md'):
    main_agent_workflow_after_review_prompt = main_agent_workflow_after_review(ask_human_md, what_just_change)
    review_feedback_prompt = f"""## 任务背景
审计员已基于《{original_requirement_md}》+《{hitl_record_md}》对比了你的《{requirements_clear_md}》。
你需要对这些审计员提出的评审意见进行鉴定、修复，并在信息不足时向人类发起求助。

## 输入上下文
### 审计反馈原始记录 (Raw Feedback)
[REVIEW MSG START]
{review_msg}
[REVIEW MSG END]

{main_agent_workflow_after_review_prompt}

## 禁令
- 你的工作范围仅限 **业务逻辑决断** 与 **需求边界澄清**。严禁在答复或文档中进行任何“数据库表设计”、“技术架构选型”、或“代码实现论证”。
- 禁止修改源代码, 禁止修改除了《{what_just_change}》/《{requirements_clear_md}》/《{ask_human_md}》之外的文档。
- 如果触发 `HITL`, 则一定要写《{ask_human_md}》。
- 只能输出 `HITL` 或 `修改完成`。"""
    return review_feedback_prompt


# 将 [需求分析师] 的回复发回给 [审核器] 智能体, 并要求重新审核
def requirements_review_reply(ba_reply, task_name="需求评审", *,
                              requirement_review_md='name_需求评审记录_agent.md',
                              requirement_review_json='name_需求评审记录_agent.json',
                              requirements_clear_md='name_需求澄清.md'):
    state_machine_output_prompt = state_machine_output(task_title=task_name, review_md=requirement_review_md,
                                                       review_json=requirement_review_json,
                                                       pass_condition="需求澄清审核通过, 符合审计准则。",
                                                       blocked_condition="需求澄清文档中发现逻辑错误、需求遗漏、或其他潜在隐患。")
    requirements_review_reply_prompt = f"""## Context
根据你上一轮对《{requirements_clear_md}》提出的评审结论, 以下是需求分析师根据《{requirement_review_md}》返回的信息:
[ANALYST_FEEDBACK_START]
{ba_reply}
[ANALYST_FEEDBACK_END]

## 需要你做:
1) 分析哪些错误已经修复? 分析哪些疑问和歧义已经解答?
2) 删除《{requirement_review_md}》中已经解决的 错误点&疑问点&歧义点.
3) 评审当前《{requirements_clear_md}》是否已经闭环? 是否仍然有错误和未解决的问题? 如果发现新问题则写入《{requirement_review_md}》
4) 检查当前《{requirement_review_md}》内是否仍然有错误点&疑问点&歧义点. 如果还有则返回 `未通过`, 若已经清空则返回 `审核通过`

{state_machine_output_prompt}

**注意**: 
* 只能回答 `未通过` 或 `审核通过`. 
* 如果是 `未通过` 则《{requirement_review_md}》内一定要有未解决的点, 
* 如果是 `审核通过` 则《{requirement_review_md}》内的问题一定要全部解决完毕."""
    return requirements_review_reply_prompt


if __name__ == '__main__':
    from T04_common_prompt import check_reviewer_job
    from T01_tools import create_empty_json_files, merge_review_records, task_done, get_markdown_content

    requirement_name = 'TimeFrequencyExtension'
    the_dir = '/Users/chenjunming/Desktop/v3_dev/canopy-api-v3'
    t_name = "需求评审"
    agent_n_list = ['C1', 'C2']

    '''1) 创建空的审核结果JSON文件'''
    # create_empty_json_files(directory=the_dir,
    #                         name_list=agent_n_list,
    #                         pattern=f'{requirement_name}_评审记录_*.json'
    #                         )

    '''2) 人类反馈'''
    # print(human_reply_sop(human_msg='测1212试', ask_human_md=f'{requirement_name}_与人类交流.md',
    #                       requirements_clear_md=f'{requirement_name}_需求澄清.md',
    #                       hitl_record_md=f'{requirement_name}_人机交互澄清记录.md'))
    print(resume_ba(human_msg='', ba_desc=fintech_ba,
                    init_prompt=task_start_prompt,
                    ask_human_md=f'{requirement_name}_与人类交流.md',
                    original_requirement_md=f'{requirement_name}_原始需求.md',
                    requirements_clear_md=f'{requirement_name}_需求澄清.md',
                    hitl_record_md=f'{requirement_name}_人机交互澄清记录.md'))

    '''3) 审核初始化'''
    # print(requirements_review_init(auditor_desc=auditor, init_prompt=task_start_prompt,
    #                                original_requirement_md=f'{requirement_name}_原始需求.md',
    #                                hitl_record_md=f'{requirement_name}_人机交互澄清记录.md',
    #                                requirements_clear_md=f'{requirement_name}_需求澄清.md',
    #                                requirement_review_md=f'{requirement_name}_需求评审记录_C2.md',
    #                                requirement_review_json=f'{requirement_name}_评审记录_C2.json'))

    # # 3) 检查审核员有没有按提示词要求更新
    # check_res = check_reviewer_job(agent_n_list,
    #                                directory=the_dir,
    #                                task_name=t_name,
    #                                json_pattern=f"{requirement_name}_评审记录_*.json",
    #                                md_pattern=f"{requirement_name}_需求评审记录_*.md")
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
    #                       md_pattern=f"{requirement_name}_需求评审记录_*.md",
    #                       md_output_name=f"{requirement_name}_需求评审记录.md")
    #
    # if pass_bool:
    #     print(f"{t_name}阶段, 全部评审通过", '\n', '-' * 100, '\n')
    # else:
    #     print(f"{t_name}阶段, 评审未通过", '\n', '-' * 100, '\n')
    #
    #     '''告诉需求分析师,评审结果'''
    #     # ba_msg = get_markdown_content(f'{the_dir}/{requirement_name}_需求评审记录.md')
    #     # print(review_feedback(ba_msg, original_requirement_md=f'{requirement_name}_原始需求.md',
    #     #                       ask_human_md=f'{requirement_name}_与人类交流.md',
    #     #                       hitl_record_md=f'{requirement_name}_人机交互澄清记录.md',
    #     #                       requirements_clear_md=f'{requirement_name}_需求澄清.md',
    #     #                       what_just_change=f'{requirement_name}_需求分析师反馈.md'))
    #
    #     ba_msg = get_markdown_content(f'{the_dir}/{requirement_name}_需求分析师反馈.md')
    #     print(requirements_review_reply(ba_msg, t_name,
    #                                     requirement_review_md=f'{requirement_name}_需求评审记录_C2.md',
    #                                     requirement_review_json=f'{requirement_name}_评审记录_C2.json',
    #                                     requirements_clear_md=f'{requirement_name}_需求澄清.md'))
