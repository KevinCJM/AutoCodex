# -*- encoding: utf-8 -*-
"""
@File: A02_function_agents.py
@Modify Time: 2026/1/11 16:28       
@Author: Kevin-Chen
@Descriptions: 初始化多个功能型智能体
"""
from concurrent.futures import ThreadPoolExecutor, as_completed

from B00_agent_config import *

print_lock = threading.Lock()


# 初始化智能代理
def init_agent(agent_name: str):
    """
    初始化智能代理

    参数:
        agent_name (str): 智能代理的名称
    返回:
        tuple: 包含代理名称和会话ID的元组 (agent_name, session_id)
    """
    log_file_path = f"{working_path}/agent_{agent_name}_{today_str}.log"

    ''' 1) 通用初始化1 --------------------------------------------------------------------------------- '''
    # 执行第一个通用初始化步骤，创建新的会话
    _, session_id = run_agent(agent_name, log_file_path, common_init_prompt_1,
                              init_yn=True, session_id=None)

    ''' 2) 通用初始化2 --------------------------------------------------------------------------------- '''
    # 执行第二个通用初始化步骤，使用已创建的会话继续初始化
    run_agent(agent_name, log_file_path, common_init_prompt_2,
              init_yn=False, session_id=session_id)

    ''' 3) 个性化初始化 --------------------------------------------------------------------------------- '''
    # 根据代理名称获取个性化初始化提示，并执行个性化初始化
    agent_prompt = coding_agent_init_prompt[agent_name]
    run_agent(agent_name, log_file_path, agent_prompt,
              init_yn=False, session_id=session_id)

    return agent_name, session_id


if __name__ == '__main__':
    agent_session_id_dict = dict()
    # 使用线程池并发初始化多个agent，将agent名称和对应的session ID存储到字典中
    with ThreadPoolExecutor(max_workers=len(agent_names_list)) as executor:
        futures = [executor.submit(init_agent, agent_name) for agent_name in agent_names_list]
        for future in as_completed(futures):
            a_name, s_id = future.result()
            agent_session_id_dict[a_name] = s_id
