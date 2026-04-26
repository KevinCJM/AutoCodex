# -*- encoding: utf-8 -*-
"""
@File: Prompt_07_Development.py
@Modify Time: 2026/4/10 17:42       
@Author: Kevin-Chen
@Descriptions: 复核阶段提示词
"""

from canopy_core.prompt_contracts.spec import (
    ACCESS_READ,
    ACCESS_READ_WRITE,
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
)
from T04_common_prompt import task_start_prompt, state_machine_output
from pathlib import Path


# 初始化 [审核员] 智能体
@agent_prompt(
    prompt_id="a08.reviewer.init",
    stage="a08",
    role="reviewer",
    intent="ready",
    mode="a08_reviewer_init",
    files={
        "hitl_record": FileSpec(path_arg="hitl_record_md", access=ACCESS_READ, change=CHANGE_NONE),
        "requirements_clear": FileSpec(path_arg="requirements_clear_md", access=ACCESS_READ, change=CHANGE_NONE),
        "original_requirement": FileSpec(path_arg="original_requirement_md", access=ACCESS_READ, change=CHANGE_NONE),
        "detailed_design": FileSpec(path_arg="detailed_design_md", access=ACCESS_READ, change=CHANGE_NONE),
        "task_md": FileSpec(path_arg="task_split_md", access=ACCESS_READ, change=CHANGE_NONE),
    },
    outcomes={"ready": OutcomeSpec(status="ready")},
)
def init_reviewer(init_prompt=task_start_prompt,
                  *,
                  hitl_record_md='name_人机交互澄清记录.md',
                  requirements_clear_md='name_需求澄清.md',
                  original_requirement_md='name_原始需求.md',
                  detailed_design_md='name_详细设计.md',
                  task_split_md='name_任务单.md'):
    init_reviewer_prompt = f"""##背景
当前本项目已经基于以下文档完成了所有任务的代码开发：
1. **原始需求**:《{original_requirement_md}》
2. **需求澄清**:《{requirements_clear_md}》
3. **人类补充信息**:《{hitl_record_md}》
4. **详细设计**:《{detailed_design_md}》
5. **任务拆分**:《{task_split_md}》

##任务
- 基于上述文档了解需求以及对应的详细设计方案
- 了解当前完成需求修改后的代码状态 
- 本次了解需求,原始代码架构,以及修改的代码架构; 后续有更多指令给你

##约束
- 禁止修改任何文档与代码, 只做现状了解
- 只能返回 `完成`, 禁止返回其他任何文字

## 路由代码理解
- 在执行指令前, 先基于路由了解代码架构
{init_prompt}"""
    return init_reviewer_prompt


# [审核员] 智能体, 重新全面评估完成开发后的代码
@agent_prompt(
    prompt_id="a08.reviewer.review_all_code",
    stage="a08",
    role="reviewer",
    intent="overall_review",
    mode="a08_reviewer_round",
    files={
        "hitl_record": FileSpec(path_arg="hitl_record_md", access=ACCESS_READ, change=CHANGE_NONE),
        "requirements_clear": FileSpec(path_arg="requirements_clear_md", access=ACCESS_READ, change=CHANGE_NONE),
        "original_requirement": FileSpec(path_arg="original_requirement_md", access=ACCESS_READ, change=CHANGE_NONE),
        "detailed_design": FileSpec(path_arg="detailed_design_md", access=ACCESS_READ, change=CHANGE_NONE),
        "task_md": FileSpec(path_arg="task_split_md", access=ACCESS_READ, change=CHANGE_NONE),
        "review_md": FileSpec(
            path_arg="review_md",
            access=ACCESS_WRITE,
            change=CHANGE_MUST_CHANGE,
            meaning="整体复核未通过时的问题记录",
            cleanup=CLEANUP_SYSTEM_BEFORE_STAGE_OR_RETRY,
        ),
        "review_json": FileSpec(
            path_arg="review_json",
            access=ACCESS_READ_WRITE,
            change=CHANGE_MUST_CHANGE,
            meaning="整体复核 pass/fail 结构化事实源",
            cleanup=CLEANUP_SYSTEM_BEFORE_STAGE_OR_RETRY,
        ),
    },
    outcomes={
        "review_pass": OutcomeSpec(status="review_pass", requires=("review_json",), forbids=("review_md",), special=SPECIAL_REVIEW_PASS),
        "review_fail": OutcomeSpec(status="review_fail", requires=("review_json", "review_md"), special=SPECIAL_REVIEW_FAIL),
    },
)
def review_all_code(agent_role, task_title,
                    *,
                    hitl_record_md='name_人机交互澄清记录.md',
                    requirements_clear_md='name_需求澄清.md',
                    original_requirement_md='name_原始需求.md',
                    detailed_design_md='name_详细设计.md',
                    task_split_md='name_任务单.md',
                    review_md='name_代码评审记录_AgentName.md',
                    review_json='评审记录_AgentName.json'):
    state_machine_output_prompt = state_machine_output(task_title,
                                                       review_md=review_md, review_json=review_json,
                                                       pass_condition="代码逻辑无瑕疵完全对齐需求与设计。",
                                                       blocked_condition="代码逻辑存在错误/遗漏/或其他潜在隐患。")
    review_all_code_prompt = f"""## 角色定位
{agent_role}

## 任务
- **代码评审**: 对本次需要的代码改动做全面回归评审
- **交叉核对**: 对《{original_requirement_md}》,《{requirements_clear_md}》,《{hitl_record_md}》,《{detailed_design_md}》,《{task_split_md}》文档内容进行交叉核对, 检查是否存在冲突或不一致的地方。

## 审计准则
- **业务闭环**：是否完美覆盖需求？是否存在计算精度或边界溢出风险？
- **契约对齐**：是否破坏了既有的 API 签名、数据库 Schema 或系统层级依赖？
- **冗余控制**：是否严格遵循“最小改动原则”？禁止任何非任务相关的代码注入。
- **回归风险**：识别对现有稳定逻辑的副作用（Side Effects）。
- **四问约束**: 检查各个有变更的代码, 进行四问分析：
    1. 是否属于当前需求？如果不是，不改。
    2. 不改是否阻塞？如果不会阻塞，不改。
    3. 是否可以最小改造？避免全面重构，而是只解决当前障碍，只做最小化的改动。
    4. 是否影响原有契约？禁止影响原有契约，包括 API、配置、日志、数据结构、下游依赖、用户工作流、兼容等等逻辑。

{state_machine_output_prompt}

## 行为禁令
- **只读模式**：严禁修改除了《{review_md}》和《{review_json}》以外的任务文档、源代码、或配置。
- **输出静默**：只能输出 `未通过` 或 `审核通过`。禁止输出任何分析过程、解释说明。
- **按条件修改文件**: 如果输出 `未通过`,《{review_md}》必须非空。如果输出 `审核通过`,《{review_md}》必须为空文件。"""
    return review_all_code_prompt


# [开发工程师] 基于审核员的评审优化代码
@agent_prompt(
    prompt_id="a08.developer.refine_all_code",
    stage="a08",
    role="developer",
    intent="overall_refine",
    mode="a08_developer_refine_all_code",
    files={
        "hitl_record": FileSpec(path_arg="hitl_record_md", access=ACCESS_READ, change=CHANGE_NONE),
        "requirements_clear": FileSpec(path_arg="requirements_clear_md", access=ACCESS_READ, change=CHANGE_NONE),
        "original_requirement": FileSpec(path_arg="original_requirement_md", access=ACCESS_READ, change=CHANGE_NONE),
        "detailed_design": FileSpec(path_arg="detailed_design_md", access=ACCESS_READ, change=CHANGE_NONE),
        "developer_output": FileSpec(
            path_arg="what_just_dev",
            access=ACCESS_WRITE,
            change=CHANGE_MUST_CHANGE,
            meaning="整体复核修复与去重元数据",
            cleanup=CLEANUP_SYSTEM_BEFORE_STAGE_OR_RETRY,
            special=SPECIAL_STAGE_ARTIFACT,
        ),
    },
    outcomes={"completed": OutcomeSpec(status="completed", requires=("developer_output",))},
)
def refine_all_code(code_review_msg,
                    hitl_record_md='name_人机交互澄清记录.md',
                    requirements_clear_md='name_需求澄清.md',
                    original_requirement_md='name_原始需求.md',
                    detailed_design_md='name_详细设计.md',
                    what_just_dev='name_工程师开发内容.md'):
    refine_code_prompt = f"""## 角色定位
你是一个【逻辑自洽的资深开发工程师】，刚刚审计员针对当前代码进行了审核。现在由你负责处理审计反馈并执行精准的代码修复。
当前需求是《{original_requirement_md}》+《{requirements_clear_md}》+《{hitl_record_md}》

## 审计反馈
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


# [审核员] 智能体, 检查工程师的修改记录, 再次检查代码
@agent_prompt(
    prompt_id="a08.reviewer.review_all_code_again",
    stage="a08",
    role="reviewer",
    intent="overall_review_reply",
    mode="a08_reviewer_round",
    files={
        "hitl_record": FileSpec(path_arg="hitl_record_md", access=ACCESS_READ, change=CHANGE_NONE),
        "requirements_clear": FileSpec(path_arg="requirements_clear_md", access=ACCESS_READ, change=CHANGE_NONE),
        "original_requirement": FileSpec(path_arg="original_requirement_md", access=ACCESS_READ, change=CHANGE_NONE),
        "detailed_design": FileSpec(path_arg="detailed_design_md", access=ACCESS_READ, change=CHANGE_NONE),
        "review_md": FileSpec(
            path_arg="review_md",
            access=ACCESS_READ_WRITE,
            change=CHANGE_MUST_CHANGE,
            meaning="整体复核复评未通过时的剩余问题",
            cleanup=CLEANUP_SYSTEM_BEFORE_STAGE_OR_RETRY,
        ),
        "review_json": FileSpec(
            path_arg="review_json",
            access=ACCESS_READ_WRITE,
            change=CHANGE_MUST_CHANGE,
            meaning="整体复核复评 pass/fail 结构化事实源",
            cleanup=CLEANUP_SYSTEM_BEFORE_STAGE_OR_RETRY,
        ),
    },
    outcomes={
        "review_pass": OutcomeSpec(status="review_pass", requires=("review_json",), forbids=("review_md",), special=SPECIAL_REVIEW_PASS),
        "review_fail": OutcomeSpec(status="review_fail", requires=("review_json", "review_md"), special=SPECIAL_REVIEW_FAIL),
    },
)
def review_all_code_again(code_review_msg, code_change_msg, task_title,
                          *,
                          hitl_record_md='name_人机交互澄清记录.md',
                          requirements_clear_md='name_需求澄清.md',
                          original_requirement_md='name_原始需求.md',
                          detailed_design_md='name_详细设计.md',
                          review_md='name_代码评审记录_AgentName.md',
                          review_json='评审记录_AgentName.json'):
    state_machine_output_prompt = state_machine_output(task_title,
                                                       review_md=review_md, review_json=review_json,
                                                       pass_condition="代码逻辑无瑕疵完全对齐需求与设计。",
                                                       blocked_condition="代码逻辑存在错误/遗漏/或其他潜在隐患。")
    review_all_code_prompt = f"""## 任务背景
本轮为**二次审核**。你需要重新审核代码并且检查开发工程师的答复, 然后判定开发工程师是否已根据上一轮审计建议完成了逻辑闭环。

## 核心输入
### 1. 上轮审计发现 (Previous Findings)
[CODE REVIEW MSG START]
{code_review_msg}
[CODE REVIEW MSG END]

### 2. 开发者提交的变更与答复 (Developer's Response)
[DEVELOPER MSG START]
{code_change_msg}
[DEVELOPER MSG END]

### 3. 相关文档
-《{original_requirement_md}》+《{requirements_clear_md}》+《{hitl_record_md}》+《{detailed_design_md}》

## 审计准则
- **业务闭环**：是否完美覆盖需求？是否存在计算精度或边界溢出风险？
- **契约对齐**：是否破坏了既有的 API 签名、数据库 Schema 或系统层级依赖？
- **冗余控制**：是否严格遵循“最小改动原则”？禁止任何非任务相关的代码注入。
- **回归风险**：识别对现有稳定逻辑的副作用（Side Effects）。
- **四问约束**: 检查各个有变更的代码, 进行四问分析：
    1. 是否属于当前需求？如果不是，不改。
    2. 不改是否阻塞？如果不会阻塞，不改。
    3. 是否可以最小改造？避免全面重构，而是只解决当前障碍，只做最小化的改动。
    4. 是否影响原有契约？禁止影响原有契约，包括 API、配置、日志、数据结构、下游依赖、用户工作流、兼容等等逻辑。

{state_machine_output_prompt}

## 行为禁令
- **只读模式**：严禁修改除了《{review_md}》和《{review_json}》以外的任务文档、源代码、或配置。
- **输出静默**：只能输出 `未通过` 或 `审核通过`。禁止输出任何分析过程、解释说明。
- **按条件修改文件**: 如果输出 `未通过`,《{review_md}》必须非空。如果输出 `审核通过`,《{review_md}》必须为空文件。"""
    return review_all_code_prompt


if __name__ == '__main__':
    from T04_common_prompt import check_reviewer_job
    from T01_tools import create_empty_json_files, merge_review_records
    from T01_tools import task_done, get_markdown_content, is_file_empty, get_first_false_task

    requirement_name = '服务独立'
    the_dir = '/Users/chenjunming/Desktop/abs_return/canopy-api-v3'
    task_name = '全面复核'
    agent_n_list = ['C1', 'C2']

    '''1) 初始化审核智能体'''
    print(init_reviewer(init_prompt=task_start_prompt,
                        hitl_record_md=f'{requirement_name}_人机交互澄清记录.md',
                        requirements_clear_md=f'{requirement_name}_需求澄清.md',
                        original_requirement_md=f'{requirement_name}_原始需求.md',
                        detailed_design_md=f'{requirement_name}_详细设计.md',
                        task_split_md=f'{requirement_name}_任务单.md'))

    '''2) 用审核智能体做代码审核'''
    #     auditor = f"""你是一个冷酷、逻辑化的代码审计代理。遵循“最小必要变更”原则，禁止冗余废话。
    # 你以静态分析引擎视角，严格扫描 AST 完整性、需求符合性、架构泄露与 $O(n^2)$ 复杂度。输出必须是机器友好的：仅包含状态、违规类型、精确诊断及修复最小字符。"""
    #     print(review_all_code(agent_role=auditor, task_title='task_name',
    #                           hitl_record_md=f'{requirement_name}_人机交互澄清记录.md',
    #                           requirements_clear_md=f'{requirement_name}_需求澄清.md',
    #                           original_requirement_md=f'{requirement_name}_原始需求.md',
    #                           detailed_design_md=f'{requirement_name}_详细设计.md',
    #                           task_split_md=f'{requirement_name}_任务单.md',
    #                           review_json=f'{requirement_name}_代码评审记录_C1.json',
    #                           review_md=f'{requirement_name}_代码评审记录_C1.md'))

    '''3) 审核完成后检查有没有正常写文档'''
    check_res = check_reviewer_job(agent_n_list,
                                   directory=the_dir,
                                   task_name=task_name,
                                   json_pattern=f"{requirement_name}_评审记录_*.json",
                                   md_pattern=f"{requirement_name}_代码评审记录_*.md")
    if check_res:
        for i, m in check_res.items():
            print(i)
            print(m)
            print('-' * 100)

    '''4) 判断是否审核通过'''
    # 判断是否所有评审都通过: 1)合并所有md, 2)判断总md是否为空, 3)判断所有json是否true
    pass_bool = task_done(directory=the_dir,
                          file_path=f'{the_dir}/{requirement_name}_任务单.json',
                          task_name=task_name,
                          json_pattern=f"{requirement_name}_评审记录_*.json",
                          md_pattern=f"{requirement_name}_代码评审记录_*.md",
                          md_output_name=f"{requirement_name}_代码评审记录.md")

    if pass_bool:
        '''5) 如果审核通过则进入下一阶段'''
        print(f"{task_name}阶段, 全部评审通过", '\n', '-' * 100, '\n')
    else:
        '''6) 如果审核未通过则启动 [开发工程师] 智能体进行代码优化'''
        print(f"{task_name}阶段, 评审未通过", '\n', '-' * 100, '\n')

        '''7) 初始化 [开发工程师] 智能体'''
        print(init_reviewer(init_prompt=task_start_prompt,
                            hitl_record_md=f'{requirement_name}_人机交互澄清记录.md',
                            requirements_clear_md=f'{requirement_name}_需求澄清.md',
                            original_requirement_md=f'{requirement_name}_原始需求.md',
                            detailed_design_md=f'{requirement_name}_详细设计.md',
                            task_split_md=f'{requirement_name}_任务单.md'))

        '''8) 读取评审建议, 让开发工程师改'''
        reviewer_msg = get_markdown_content(f'{the_dir}/{requirement_name}_代码评审记录.md')
        print(refine_all_code(reviewer_msg,
                              hitl_record_md=f'{requirement_name}_人机交互澄清记录.md',
                              requirements_clear_md=f'{requirement_name}_需求澄清.md',
                              original_requirement_md=f'{requirement_name}_原始需求.md',
                              detailed_design_md=f'{requirement_name}_详细设计.md',
                              what_just_dev=f'{requirement_name}_工程师开发内容.md'))

        '''9) 读取变更内容, 让评审智能体再次审核'''
        developer_msg = get_markdown_content(f'{the_dir}/{requirement_name}_工程师开发内容.md')
        print(review_all_code_again(reviewer_msg, developer_msg, task_name,
                                    hitl_record_md=f'{requirement_name}_人机交互澄清记录.md',
                                    requirements_clear_md=f'{requirement_name}_需求澄清.md',
                                    original_requirement_md=f'{requirement_name}_原始需求.md',
                                    detailed_design_md=f'{requirement_name}_详细设计.md',
                                    review_json=f'{requirement_name}_代码评审记录_C1.json',
                                    review_md=f'{requirement_name}_代码评审记录_C1.md'))
