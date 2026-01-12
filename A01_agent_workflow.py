# -*- encoding: utf-8 -*-
"""
@File: agents.py
@Modify Time: 2026/1/11 13:44       
@Author: Kevin-Chen
@Descriptions: 
"""
import json
from concurrent.futures import ThreadPoolExecutor, as_completed

from B00_agent_config import *
from A02_function_agents import init_agent

print_lock = threading.Lock()

base_director_prompt = f"""
你是一个专业的调度智能体.
现在有: 需求分析师, 审核员, 测试工程师, 开发工程师 四个智能体
当前阶段为开发阶段. 开发流程需要按照 {task_md} 中的任务安排一步步的让开发工程师智能体执行对应任务的开发.
当开发工程师智能体完成对应任务的开发后, 你需要通知 需求分析师, 审核员, 测试工程师 分别对 开发工程师 智能体的代码进行审核和测试.

---
主要流程如下:

1) 阅读 {task_md}, 检查下一个需要开发的任务是什么. 通知开发工程师智能体进行对应任务的开发.
调用开发工程师智能体时, prompt 模板如下:
    ```
    开始开发 {task_md} 中的 任务xx
    开发完成后, 重新走读自己开发的新代码, 检查是否与需求对齐, 检查是否存在逻辑错误. 完成检查后, 你需要设计单元测试用例, 进行自测.
    开发, 审核, 自测全部完成后在 {task_md} 中勾选掉对应的任务, 然后返回简要说明你具体修改了什么. 不要返回多余的信息.
    ```

2) 当开发工程师智能体完成对应任务的开发后, 整理开发工程师返回的内容. 然后结合开发内容, 通知 需求分析智能体, 审核员智能体, 测试工程师智能体 进行代码审核.
2.1) 调用 需求分析智能体 时, prompt 模板如下:
    ```
    开发工程师完成了以下开发:
    {{开发工程师返回的开发说明}}

    走读修改后的新代码, 然后分析代码中的逻辑是否与 {task_md} 一致, 是否与 {design_md} 一致.
    审核完成后说明有问题的地方, 若无问题则返回 '检查通过'. 禁止修改代码与文档.
    ```
2.2) 调用 审核员智能体 时, prompt 模板如下:
    ```
    开发工程师完成了以下开发:
    {{开发工程师返回的开发说明}}

    走读我修改后的新代码, 然后分析代码中是否存在错误和逻辑问题. 是否与 {task_md} 和 {design_md} 保持逻辑一致.
    审核完成后说明有问题的地方, 若无问题则返回 '审核通过'. 禁止修改代码与文档.
    ```
2.3) 调用 测试工程师智能体 时, prompt 模板如下:
    ```
    开发工程师完成了以下开发:
    {{开发工程师返回的开发说明}}

    走读我修改后的新代码, 分析代码中是否存在错误和逻辑问题. 是否与 {task_md} 一致, 是否与 {design_md} 一致.
    然后根据 {test_plan_md} 执行测试. 审核以及测试完成后说明有问题的地方, 若无问题则返回 '测试通过'. 不要返回多余的信息. 
    禁止修改主体代码, 可以创建和修改测试用代码. 测试相关的代码与问题都需要由你来处理.
    ```

3) 收集 需求分析智能体, 审核员智能体, 测试工程师智能体 的审核结果.
3.1) 如果所有智能体都没有提出问题或者有疑问点, 则认为该任务开发完成, 回到步骤1, 继续下一个任务的开发.
3.2) 如果有智能体提出问题, 则整理所有智能体提出的问题点和疑问点, 回到步骤2, 通知开发工程师智能体进行修复. prompt 模板如下:
    ```
    你是一个专业的 python 开发工程师, 针对你刚刚完成的开发内容, 专家团队有一些问题/疑问点:

    需求分析师发现以下问题:
    {{具体内容}}

    需求分析师有以下疑问点:
    {{具体内容}}

    审核员发现以下问题:
    {{具体内容}}

    审核员有以下疑问点:
    {{具体内容}}

    测试工程师发现以下问题:
    {{具体内容}}

    测试工程师有以下疑问点:
    {{具体内容}}

    1) 总结并去重上面提到的问题, 总结并去重上面的疑问/假设
    2) 详细分析这些问题是否属实, 回答疑问, 回答假设
    3) 说明属实的问题是哪些.
    4) 是否存在疑问/分歧的问题, 并说明疑问/分歧点是什么. 然后对这些疑问/分歧点进行深度设计.
    5) 对属实的问题进行修复. 并且说明对应的假设.
    6) 说明你改了什么
    注意: 只返回上面1~6点相关的内容, 不要返回其他无关的内容.
    ```

4) 当所有任务都开发完成后, 结束整个开发流程. 返回如下JSON:
    ```
    {{"success": "所有任务开发完成"}}
    ```
---
返回JSON格式的数据, 格式需要是 {{智能体名称: 提示词}} 格式如下:
{{"开发工程师": "相关提示词..."}}
{{"需求分析师": "相关提示词...", "审核员": "相关提示词...", "测试工程师": "相关提示词..."}}
注意JSON内部双引号需要有转义符, 保证JSON合法

---
智能体名称必须是: 开发工程师, 需求分析师, 审核员, 测试工程师
返回的格式必须严格满足JSON格式要求.
"""

''' 1) 初始化 各个功能型智能体 ----------------------------------------------------------------------------------------- '''
agent_session_id_dict = dict()
# 使用线程池并发初始化多个agent，将agent名称和对应的session ID存储到字典中
with ThreadPoolExecutor(max_workers=len(agent_names_list)) as executor:
    futures = [executor.submit(init_agent, agent_name) for agent_name in agent_names_list]
    for future in as_completed(futures):
        a_name, s_id = future.result()
        agent_session_id_dict[a_name] = s_id

''' 2) 初始化 调度器智能体 ------------------------------------------------------------------------------------------- '''
director_agent_name = "调度器"
log_file_path = f"{working_path}/agent_{director_agent_name}_{today_str}.log"
init_director_prompt = f"""
---
现在开始你的调度任务, 先分析 {task_md} 中下一个需要开发的任务是什么.
"""
init_director_prompt = base_director_prompt + init_director_prompt
msg, director_session_id = run_agent(director_agent_name, log_file_path,
                                     init_director_prompt, init_yn=True, session_id=None)
msg_dict = json.loads(msg)
first_agent_name = list(msg_dict.keys())[0]

''' 3) 调用 各个功能型智能体 ----------------------------------------------------------------------------------------- '''
while first_agent_name != 'success':
    if first_agent_name not in ['需求分析师', '审核员', '测试工程师', '开发工程师']:
        raise ValueError(f"调度器智能体返回了未知的智能体名称: {first_agent_name}")
    # 执行 智能体
    what_agent_just_use = list()
    what_agent_replay_dict = dict()
    for agent_name, agent_prompt in msg_dict.items():
        log_file_path = f"{working_path}/agent_{agent_name}_{today_str}.log"
        session_id = agent_session_id_dict[agent_name]
        msg, _ = run_agent(agent_name, log_file_path, agent_prompt,
                           init_yn=False, session_id=session_id)
        what_agent_just_use.append(agent_name)
        what_agent_replay_dict[agent_name] = msg

    ''' 4) 调用 调度器智能体 ---------------------------------------------------------------------------------------- '''
    # 合并处理结果, 生成 新的调度器提示词
    what_agent_replay = ''
    for agent_name, msg in what_agent_replay_dict.items():
        what_agent_replay += f"{agent_name}: \n{msg}\n"
    doing_director_prompt = f"""
---
你刚刚调用的智能体为 {what_agent_just_use} 返回内容如下:
{what_agent_replay}
---
继续按照上述流程进行调度.
    """
    doing_director_prompt = base_director_prompt + doing_director_prompt

    # 调用 调度器智能体
    msg, session_id = run_agent(director_agent_name, log_file_path, doing_director_prompt,
                                init_yn=False, session_id=director_session_id)
    msg_dict = json.loads(msg)
    first_agent_name = list(msg_dict.keys())[0]
