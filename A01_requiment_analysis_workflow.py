# -*- encoding: utf-8 -*-
"""
@File: agents.py
@Modify Time: 2026/1/11 13:44       
@Author: Kevin-Chen
@Descriptions: 
"""
from A01_coding_agent_workflow import _parse_director_response
from concurrent.futures import ThreadPoolExecutor, as_completed

from B00_agent_config import *
from A02_init_function_agents import init_agent, custom_init_agent

print_lock = threading.Lock()

base_director_prompt = f"""你是一个专业的调度智能体.
现在有: {agent_names_list} 这{len(agent_names_list)}个智能体.
当前阶段为 需求分析&详细设计 阶段. 当前的需求描述如下:
```
{requirement_str}
```
需求分析师已经根据需求描述写了一份详细设计文档 {design_md}
需要其他智能体对这个详细设计文档进行检查, 如何结合各个智能体的输出, 让需求分析师对 {design_md} 文档进行优化修改. 
直到所有智能体都返回检查通过.

---
主要流程如下:

1) 通知 审核员智能体, 测试工程师智能体, 开发工程师智能体 进行文档审核.

1.1) 调用 审核员智能体 时, prompt 模板如下:
    ```
    你是一个专业的代码与架构审核员. 现在有以下需求补充:
    {requirement_str}
    
    需求分析师已经根据需求补充, 进行了详细设计, 并且写入了 {design_md} 文档中.
    请你以专业代码与架构审核员的角度, 根据以上需求补充 结合 当前代码, 审查 {design_md} 中的详细设计.
    检查是否存在设计错误或者设计遗漏. 如果有疑问或歧义也可以提出.
    返回你发现的错误/遗漏点 以及 疑问/歧义点. 如果没有任何错误或疑问, 则返回 '检查通过'.
    不要返回多余的信息. 不要修改代码与文档.
    ```

1.2) 调用 测试工程师 时, prompt 模板如下:
    ```
    你是一个专业的测试工程师. 现在有以下需求补充:
    {requirement_str}
    
    需求分析师已经根据需求补充, 进行了详细设计, 并且写入了 {design_md} 文档中.
    请你以专业测试工程师的角度, 根据以上需求补充 结合 当前代码, 审查 {design_md} 中的详细设计.
    检查是否存在设计错误或者设计遗漏. 如果有疑问或歧义也可以提出.
    返回你发现的错误/遗漏点 以及 疑问/歧义点. 如果没有任何错误或疑问, 则返回 '检查通过'.
    不要返回多余的信息. 不要修改代码与文档.
    ```
    
1.3) 调用 开发工程师 时, prompt 模板如下:
    ```
    你是一个专业的Python开发工程师. 现在有以下需求补充:
    {requirement_str}
    
    需求分析师已经根据需求补充, 进行了详细设计, 并且写入了 {design_md} 文档中.
    请你以专业开发工程师的角度, 根据以上需求补充 结合 当前代码, 审查 {design_md} 中的详细设计.
    检查是否存在设计错误或者设计遗漏. 如果有疑问或歧义也可以提出.
    返回你发现的错误/遗漏点 以及 疑问/歧义点. 如果没有任何错误或疑问, 则返回 '检查通过'.
    不要返回多余的信息. 不要修改代码与文档.
    ```

---
2) 收集 审核员智能体, 测试工程师智能体, 开发工程师智能体 的审核结果.

2.1) 如果所有智能体都没有提出问题或者有疑问点, 则认为该任务开发完成, 直接到步骤5, 返回 {{"success": "所有任务开发完成"}}

2.2) 如果有任一智能体提出错误点或者有疑问点, 则认为该需求详细设计未完成. 需要通知 需求分析师智能体 进行修复. prompt 模板如下:
    ```
    你是一个专业 需求分析师, 针对 {design_md} 文档, 专家团队发现一些错误/遗漏点, 或者存在疑问/歧义点:

    审核员发现以下错误/遗漏点:
    {{具体内容}}

    审核员有以下疑问/歧义点:
    {{具体内容}}

    测试工程师发现以下错误/遗漏点:
    {{具体内容}}

    测试工程师有以下疑问/歧义点:
    {{具体内容}}
    
    开发工程师发现以下错误/遗漏点:
    {{具体内容}}

    开发工程师有以下疑问/歧义点:
    {{具体内容}}

    1) 总结并去重上面提到错误/遗漏点, 疑问/歧义点.
    2) 详细分析这些 错误/遗漏点 是否属实.
    3) 说明属实的问题是哪些.
    4) 对这些疑问/分歧点进行深度设计.
    5) 对属实的问题进行修复. 并且说明对应的假设.
    6) 修改并补充说明 {design_md} 文档. 并且说明你改了什么.
    注意: 只返回上面1~6点相关的内容, 不要返回其他无关的内容.
    ```

---
3) 需求分析师智能体 修改完 详细设计文档, 然后你需要告诉各个其他智能体修改内容是什么, 并且让各个智能体再次检查.

3.1) 调用 审核员智能体 时, prompt 模板如下:
    ```
    你是一个专业的代码与架构审核员. 
    刚刚 需求分析师修改了 {design_md} 文档. 其修改内容如下:
    {{修改内容}}
    
    以专业代码与架构审核员的角度, 再次审查 {design_md} 中的详细设计.
    结合代码以及原始需求说明: {requirement_str}
    
    检查是否存在设计错误或者设计遗漏. 如果有疑问或歧义也可以提出.
    返回你发现的错误/遗漏点 以及 疑问/歧义点. 如果没有任何错误或疑问, 则返回 '检查通过'.
    不要返回多余的信息. 不要修改代码与文档.
    ```

3.2) 调用 测试工程师 时, prompt 模板如下:
    ```
    你是一个专业的测试工程师. 
    刚刚 需求分析师修改了 {design_md} 文档. 其修改内容如下:
    {{修改内容}}
    
    以专业测试工程师的角度, 再次审查 {design_md} 中的详细设计.
    结合代码以及原始需求说明: {requirement_str}
    
    检查是否存在设计错误或者设计遗漏. 如果有疑问或歧义也可以提出.
    返回你发现的错误/遗漏点 以及 疑问/歧义点. 如果没有任何错误或疑问, 则返回 '检查通过'.
    不要返回多余的信息. 不要修改代码与文档.
    ```

3.3) 调用 开发工程师 时, prompt 模板如下:
    ```
    你是一个专业的开发工程师. 现在有以下需求补充:
    刚刚 需求分析师修改了 {design_md} 文档. 其修改内容如下:
    {{修改内容}}
    
    以专业开发工程师的角度, 再次审查 {design_md} 中的详细设计.
    结合代码以及原始需求说明: {requirement_str}
    
    检查是否存在设计错误或者设计遗漏. 如果有疑问或歧义也可以提出.
    返回你发现的错误/遗漏点 以及 疑问/歧义点. 如果没有任何错误或疑问, 则返回 '检查通过'.
    不要返回多余的信息. 不要修改代码与文档.
    ```

---
4) 收集 审核员智能体, 测试工程师智能体, 开发工程师智能体 的审核结果.

4.1) 如果所有智能体都没有提出问题或者有疑问点, 则认为该任务开发完成, 直接到步骤5, 返回 {{"success": "所有任务开发完成"}}

4.2) 如果有任一智能体提出错误点或者有疑问点, 则认为该需求详细设计未完成. 需要通知 需求分析师智能体 进行修复. 回到 2.2)

---
5) 当所有智能体都认为当前需求分析与详细设计没有问题了, 则结束整个流程. 返回如下JSON:
    ```
    {{"success": "所有任务开发完成"}}
    ```

---
返回JSON格式的数据, 格式需要是 {{智能体名称: 提示词}} 格式如下:
{{"需求分析师": "相关提示词..."}}
{{"审核员": "相关提示词...", "测试工程师": "相关提示词...", "开发工程师": "相关提示词...", }}
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
# 多线程个性化初始化智能体
with ThreadPoolExecutor(max_workers=len(agent_names_list)) as executor:
    futures = [executor.submit(custom_init_agent, agent_name, agent_session_id_dict[agent_name],
                               coding_agent_init_prompt[agent_name]) for agent_name in agent_names_list]
    for future in as_completed(futures):
        a_name, s_id = future.result()
        agent_session_id_dict[a_name] = s_id

''' 2) 初始化 调度器智能体 ------------------------------------------------------------------------------------------- '''
director_agent_name = "调度器"
director_log_file_path = f"{working_path}/agent_{director_agent_name}_{today_str}.log"
init_director_prompt = f"""
---
现在开始你的调度任务.
"""
init_director_prompt = base_director_prompt + init_director_prompt
msg, director_session_id = run_agent(director_agent_name, director_log_file_path,
                                     init_director_prompt, init_yn=True, session_id=None)
msg_dict = _parse_director_response(msg, director_log_file_path)
first_agent_name = list(msg_dict.keys())[0]

''' 3) 调用 各个功能型智能体 ----------------------------------------------------------------------------------------- '''
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
                agent_prompt,
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

    ''' 4) 调用 调度器智能体 ---------------------------------------------------------------------------------------- '''
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
    msg, session_id = run_agent(director_agent_name, director_log_file_path, doing_director_prompt,
                                init_yn=False, session_id=director_session_id)
    msg_dict = _parse_director_response(msg, director_log_file_path)
    first_agent_name = list(msg_dict.keys())[0]
