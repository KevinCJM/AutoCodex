# -*- encoding: utf-8 -*-
"""
@File: agents.py
@Modify Time: 2026/1/11 13:44       
@Author: Kevin-Chen
@Descriptions: 代码开发 工作流
"""
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from B00_agent_config import (
    format_agent_skills,
    REQUIREMENT_CLARIFICATION_MD,
    TASK_DOC_STRUCTURE_PROMPT,
    TASK_EXECUTION_PROMPT,
    agent_names_list,
    coding_agent_init_prompt,
    coding_test_agent_init_prompt,
    design_md,
    run_agent,
    task_md,
    test_plan_md,
    today_str,
    working_path,
)
from B02_log_tools import Colors, log_message
from B03_init_function_agents import init_agent, custom_init_agent, parse_director_response

print_lock = threading.Lock()
DIRECTOR_NAME = "调度器"
TASK_HEADING_RE = re.compile(
    r"^####\s+(?:\[(?P<checked>[ xX])\]\s+)?(?P<task_id>US-(?P<epic>\d+)\.\d+)\s+(?P<title>.+?)\s*$"
)
INVALID_COMPLETION_RETRY_MAX = 2

# 各个智能体的 skills 标签
agent_skills_dict = {
    '需求分析师': ['$Product Manager'],
    '审核员': ['$System Architect'],
    '测试工程师': ['$Business Analyst'],
    '开发工程师': ['$Developer'],
}
DIRECTOR_SUCCESS_TEXT = "所有任务开发完成"


def _resolve_task_file_path(task_file_name=None):
    task_file_name = task_file_name or task_md
    if os.path.isabs(task_file_name):
        return task_file_name
    return os.path.join(working_path, task_file_name)


def find_next_unfinished_coding_task(task_file_name=None):
    task_file_path = _resolve_task_file_path(task_file_name)
    if not os.path.exists(task_file_path):
        return None

    with open(task_file_path, "r", encoding="utf-8") as f:
        for line_no, raw_line in enumerate(f, 1):
            match = TASK_HEADING_RE.match(raw_line.strip())
            if not match:
                continue

            epic = int(match.group("epic"))
            if epic < 1:
                continue

            title = match.group("title").strip()
            if "条件性任务" in title:
                continue

            checked = str(match.group("checked") or "").lower() == "x"
            if checked:
                continue

            return {
                "task_id": match.group("task_id"),
                "title": title,
                "line_no": line_no,
                "task_file_path": task_file_path,
            }
    return None


def build_director_invalid_completion_retry_prompt(last_director_prompt, unfinished_task):
    task_label = str(unfinished_task.get("task_id", "")).strip()
    title = str(unfinished_task.get("title", "")).strip()
    line_no = unfinished_task.get("line_no")
    location = f"{task_md}:{line_no}" if line_no else task_md
    task_desc = " ".join(part for part in [task_label, title] if part).strip()
    return f"""{str(last_director_prompt or '').rstrip()}

---
补充要求:
你上一条回复错误地返回了 "{DIRECTOR_SUCCESS_TEXT}", 但 {location} 仍存在未完成任务:
- {task_desc}
这一次不能结束流程.
你必须重新阅读 {task_md}, 找到下一个未完成任务并继续调度.
- 如果仍有未完成任务, success 必须为空字符串
- 需要调度哪个智能体, 就把 prompt 填到哪个智能体字段
- 最终只能输出固定字段 JSON 本身, 禁止输出解释说明
"""


def ensure_valid_coding_director_response(
        msg,
        director_session_id,
        director_log_file_path,
        director_prompt,
        max_invalid_completion_retries=INVALID_COMPLETION_RETRY_MAX,
):
    current_msg = msg
    current_prompt = director_prompt

    for attempt in range(max_invalid_completion_retries + 1):
        msg_dict = parse_director_response(
            current_msg,
            director_log_file_path,
            allowed_success_values={DIRECTOR_SUCCESS_TEXT},
        )
        unfinished_task = find_next_unfinished_coding_task()
        if "success" not in msg_dict or not unfinished_task:
            return current_msg, msg_dict, current_prompt

        blocker_desc = " ".join(
            part for part in [unfinished_task.get("task_id", ""), unfinished_task.get("title", "")]
            if str(part).strip()
        )
        with print_lock:
            log_message(
                log_file_path=director_log_file_path,
                message=f"调度器误报完成，任务仍未结束: {blocker_desc}",
                color=Colors.RED,
            )
        if attempt >= max_invalid_completion_retries:
            raise RuntimeError(
                f'调度器连续返回 "{DIRECTOR_SUCCESS_TEXT}", '
                f"但 {task_md} 仍存在未完成任务: {blocker_desc}"
            )

        current_prompt = build_director_invalid_completion_retry_prompt(current_prompt, unfinished_task)
        current_msg, _ = run_agent(
            DIRECTOR_NAME,
            director_log_file_path,
            current_prompt,
            init_yn=False,
            session_id=director_session_id,
        )

    raise RuntimeError("调度器回复校验失败，无法继续。")


# 调度智能体的 prompt 主体
base_director_prompt = f"""你是一个专业的调度智能体.
现在有: {agent_names_list} 这{len(agent_names_list)}个智能体.
当前阶段为开发阶段. 开发流程需要按照 {task_md} 中的任务安排一步步的让开发工程师智能体执行对应任务的开发.
当开发工程师智能体完成对应任务的开发后, 你需要通知 需求分析师, 审核员, 测试工程师 分别对 开发工程师 智能体的代码进行审核和测试.

{TASK_DOC_STRUCTURE_PROMPT}

{TASK_EXECUTION_PROMPT}

---
主要流程如下:

1) 阅读 {task_md}, 检查当前最早未完成里程碑下的下一个未完成任务单是什么. 通知开发工程师智能体进行对应任务的开发.
绝不能把“里程碑”本身当成开发派发单位, 只能派发某个具体任务单.
调用开发工程师智能体时, prompt 模板如下:
```
开始开发 {task_md} 中 里程碑 Mx 下的任务单 Mx-Tz
开发前先确认该任务单的目标、涉及模块/文件、完成标准、验证方式, 并确保本次开发只覆盖这个具体任务单.
开发完成后, 重新走读自己开发的新代码, 检查是否与 {design_md} 对齐, 检查是否存在逻辑错误. 完成检查后, 你需要设计单元测试用例, 进行自测.
开发, 审核, 自测全部完成后在 {task_md} 中勾选掉对应的任务单; 若该里程碑下任务单全部完成, 同步把该里程碑状态更新为“已完成”. 然后返回简要说明你具体修改了什么. 不要返回多余的信息.
```

2) 当开发工程师智能体完成对应任务的开发后, 整理开发工程师返回的内容. 然后结合开发内容, 通知 需求分析智能体, 审核员智能体, 测试工程师智能体 进行代码审核.
2.1) 调用 需求分析智能体 时, prompt 模板如下:
```
开发工程师完成了以下开发:
{{开发工程师返回的开发说明}}

开发工程师对于疑问/歧义点的回答与设计如下:
{{回答与设计内容}}

你是一个专业的需求分析师.
走读修改后的新代码, 然后分析代码中的逻辑是否与 {task_md} 中所属里程碑及具体任务单一致, 是否与 {design_md} 一致.

约束：
    - 禁止修改主体代码。
    - 仅审核主体代码，不评价测试代码质量。
    - 不要汇报过程，不要说明计划，不要写“我将先/接下来/然后”等过渡句。
    - 不要复述任务，不要解释你做了什么。
    - 直接返回最终审核结论，不要返回过程说明。

最终输出只能二选一：
    A. 若无问题，输出：
        审核通过
    B. 若有问题，严格按以下格式输出，除此之外禁止输出任何内容：
        开发错误/遗漏:
            1. <问题1>
            2. <问题2>
        疑问/分歧:
            1. <疑问1>
            2. <疑问2>
```

2.2) 调用 审核员智能体 时, prompt 模板如下:
```
开发工程师完成了以下开发:
{{开发工程师返回的开发说明}}

开发工程师对于疑问/歧义点的回答与设计如下:
{{回答与设计内容}}

你是一个专业的python代码审核员.
走读我修改后的新代码, 然后分析代码中是否存在错误和逻辑问题. 是否与 {task_md} 中所属里程碑及具体任务单、以及 {design_md} 保持逻辑一致.

约束：
    - 禁止修改主体代码。
    - 仅审核主体代码，不评价测试代码质量。
    - 不要汇报过程，不要说明计划，不要写“我将先/接下来/然后”等过渡句。
    - 不要复述任务，不要解释你做了什么。
    - 直接返回最终审核结论，不要返回过程说明。

最终输出只能二选一：
    A. 若无问题，输出：
        审核通过
    B. 若有问题，严格按以下格式输出，除此之外禁止输出任何内容：
        开发错误/遗漏:
            1. <问题1>
            2. <问题2>
        疑问/分歧:
            1. <疑问1>
            2. <疑问2>
```

2.3) 调用 测试工程师智能体 时, prompt 模板如下:
```
开发工程师完成了以下开发:
{{开发工程师返回的开发说明}}

开发工程师对于疑问/歧义点的回答与设计如下:
{{回答与设计内容}}

你是一个专业的测试工程师.
走读我修改后的新代码, 分析代码中是否存在错误和逻辑问题. 是否与 {task_md} 中所属里程碑及具体任务单一致, 是否与 {design_md} 一致.
然后根据 {test_plan_md} 执行测试. 审核以及测试完成后说明是否有开发错误/遗漏的点, 或者提出你的疑问和分歧点.

约束：
    - 禁止修改主体代码。
    - 仅审核主体代码，不评价测试代码质量。
    - 允许创建、修改测试代码，且测试代码相关问题由你自行处理。
    - 不要汇报过程，不要说明计划，不要写“我将先/接下来/然后”等过渡句。
    - 不要复述任务，不要解释你做了什么。
    - 直接返回最终审核结论，不要返回过程说明。

最终输出只能二选一：
    A. 若无问题，输出：
        测试通过
    B. 若有问题，严格按以下格式输出，除此之外禁止输出任何内容：
        开发错误/遗漏:
            1. <问题1>
            2. <问题2>
        疑问/分歧:
            1. <疑问1>
            2. <疑问2>
```

3) 收集 需求分析智能体, 审核员智能体, 测试工程师智能体 的审核结果.
3.1) 如果所有智能体都没有提出问题或疑问点, 则认为该任务开发完成, 回到步骤1, 继续下一个任务的开发.
3.2) 如果有智能体提出错误/疑问, 则整理所有智能体提出的错误点和疑问点, 回到步骤2, 通知开发工程师智能体进行修复. prompt 模板如下:
```
你是一个专业的 python 开发工程师, 针对你刚刚完成的开发内容, 专家团队有一些问题/疑问点:

需求分析师发现以下错误/遗漏点:
{{具体内容}}

需求分析师有以下疑问点:
{{具体内容}}

审核员发现以下错误/遗漏点:
{{具体内容}}

审核员有以下疑问点:
{{具体内容}}

测试工程师发现以下错误/遗漏点:
{{具体内容}}

测试工程师有以下疑问点:
{{具体内容}}

- 总结并去重上面提到错误/遗漏点, 疑问/歧义点.
- 详细分析这些 错误/遗漏点 是否属实.
- 对这些疑问/分歧点进行深度设计.
- 对属实的问题进行修复. 并且说明对应的假设.
- 修复代码

修改完成后, 返回以下内容:
1) 说明属实的问题是哪些
2) 对疑问/分歧点的回答与设计是什么
3) 你对代码做了哪些修改
注意: 只返回上面3点内容, 不要返回其他无关的内容.
```

4) 当所有任务都开发完成后, 结束整个开发流程. 返回如下JSON:
```
{{"success": "{DIRECTOR_SUCCESS_TEXT}"}}
```

---
返回JSON格式的数据, 格式需要是 {{智能体名称: 提示词}} 格式如下:
{{"开发工程师": "相关提示词..."}}
{{"需求分析师": "相关提示词...", "审核员": "相关提示词...", "测试工程师": "相关提示词..."}}
注意JSON内部双引号需要有转义符, 保证JSON合法

最终回复必须是一个固定字段的 JSON 对象, 且只能输出 JSON 本身, 禁止解释说明:
```json
{{"success":"","需求分析师":"","审核员":"","测试工程师":"","开发工程师":""}}
```
规则:
- 5个字段必须全部出现
- 当前不使用的字段必须填空字符串 ""
- 如果流程结束, 只能填写 success, 其他4个字段必须为空字符串
- 如果 success 非空, 它的值必须精确等于 "{DIRECTOR_SUCCESS_TEXT}"
- 如果当前只是准备读取、分析、规划下一步, 绝不能填写 success
- 如果要调度智能体, success 必须为空字符串, 其余需要调度的智能体字段填写 prompt, 不调度的智能体字段填空字符串

---
智能体名称必须是: 开发工程师, 需求分析师, 审核员, 测试工程师
返回的格式必须严格满足JSON格式要求.
"""


def prepare_agent_prompt(agent_name, agent_prompt):
    skills_prefix = format_agent_skills(agent_name, agent_skills_dict)
    clarification_rule = f"""
前置规则:
优先阅读并参考需求澄清记录文件: {REQUIREMENT_CLARIFICATION_MD}. 文件绝对路径: {working_path}/{REQUIREMENT_CLARIFICATION_MD} 
若文件不存在则忽略
"""
    return f"{skills_prefix} {clarification_rule}\n{agent_prompt}".strip()


def main():
    """
    代码开发 工作流 主函数
    :return:
    """
    ''' 1) 初始化 各个功能型智能体 ------------------------------------------------------------------------------------ '''
    agent_session_id_dict = dict()
    # 使用线程池并发初始化多个agent，将agent名称和对应的session ID存储到字典中
    with ThreadPoolExecutor(max_workers=len(agent_names_list)) as executor:
        futures = [executor.submit(init_agent, agent_name) for agent_name in agent_names_list]
        for future in as_completed(futures):
            a_name, s_id = future.result()
            agent_session_id_dict[a_name] = s_id
    # 个性化初始化 需求分析师 智能体, 写测试计划文档
    custom_init_agent('测试工程师', agent_session_id_dict['测试工程师'],
                      coding_test_agent_init_prompt)
    # 多线程个性化初始化智能体
    with ThreadPoolExecutor(max_workers=len(agent_names_list)) as executor:
        futures = [executor.submit(custom_init_agent, agent_name, agent_session_id_dict[agent_name],
                                   coding_agent_init_prompt[agent_name]) for agent_name in agent_names_list]
        for future in as_completed(futures):
            a_name, s_id = future.result()
            agent_session_id_dict[a_name] = s_id

    ''' 2) 初始化 调度器智能体 --------------------------------------------------------------------------------------- '''
    director_log_file_path = f"{working_path}/agent_{DIRECTOR_NAME}_{today_str}.log"
    init_director_prompt = f"""
    ---
    现在开始你的调度任务, 先分析 {task_md} 中下一个需要开发的任务是什么.
    """
    init_director_prompt = base_director_prompt + init_director_prompt
    msg, director_session_id = run_agent(DIRECTOR_NAME, director_log_file_path,
                                         init_director_prompt, init_yn=True, session_id=None)
    _, msg_dict, _ = ensure_valid_coding_director_response(
        msg,
        director_session_id,
        director_log_file_path,
        init_director_prompt,
    )
    first_agent_name = list(msg_dict.keys())[0]

    ''' 3) 调用 各个功能型智能体 ------------------------------------------------------------------------------------- '''
    while first_agent_name != 'success':
        if first_agent_name not in ['需求分析师', '审核员', '测试工程师', '开发工程师']:
            raise ValueError(f"调度器智能体返回了未知的智能体名称: {first_agent_name}")
        # 并发执行智能体
        agent_items = list(msg_dict.items())
        what_agent_replay_dict = dict()
        with ThreadPoolExecutor(max_workers=len(agent_items)) as executor:
            futures = {
                executor.submit(
                    run_agent,
                    agent_name,
                    f"{working_path}/agent_{agent_name}_{today_str}.log",
                    prepare_agent_prompt(agent_name, agent_prompt),
                    False,
                    agent_session_id_dict[agent_name],
                ): agent_name
                for agent_name, agent_prompt in agent_items
            }
            for future in as_completed(futures):
                agent_name = futures[future]
                msg, _ = future.result()
                what_agent_replay_dict[agent_name] = msg
        what_agent_just_use = [agent_name for agent_name, _ in agent_items]

        ''' 4) 调用 调度器智能体 ------------------------------------------------------------------------------------ '''
        # 合并处理结果, 生成 新的调度器提示词
        what_agent_replay = ''
        for agent_name, msg in what_agent_replay_dict.items():
            what_agent_replay += f"""
    {agent_name}: 
    ```
    {msg}
    ```
            """
        doing_director_prompt = f"""
    ---
    你刚刚调用的智能体为 {what_agent_just_use} 返回内容如下:
    {what_agent_replay}
    ---
    继续按照上述流程进行调度.
        """
        doing_director_prompt = base_director_prompt + doing_director_prompt

        # 调用 调度器智能体
        msg, _ = run_agent(
            DIRECTOR_NAME,
            director_log_file_path,
            doing_director_prompt,
            init_yn=False,
            session_id=director_session_id,
        )
        _, msg_dict, _ = ensure_valid_coding_director_response(
            msg,
            director_session_id,
            director_log_file_path,
            doing_director_prompt,
        )
        first_agent_name = list(msg_dict.keys())[0]
