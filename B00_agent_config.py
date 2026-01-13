# -*- encoding: utf-8 -*-
"""
@File: B00_agent_config.py
@Modify Time: 2026/1/12 11:03       
@Author: Kevin-Chen
@Descriptions: 智能体配置参数
"""

import threading
import time
from datetime import datetime

from B01_codex_utils import init_codex, resume_codex
from B02_log_tools import Colors, log_message

print_lock = threading.Lock()

now = datetime.now()
today_str = f"{now.year}{now.month:02d}{now.day:02d}"
working_path = "/Users/chenjunming/Desktop/AutomaticTypesettingTool"
working_model = "gpt-5.2-codex"
working_effort = "xhigh"
working_timeout = 60 * 10
resume_retry_max = 5
resume_retry_interval = 2

requirement_md = "论文自动排版工具-详细设计说明书.md"
design_md = "里程碑1详细设计.md"
task_md = "里程碑1任务单.md"
test_plan_md = "里程碑1测试计划.md"
common_init_prompt_1 = """记住:
1) 使用中文进行对话和文档编写;
2) 使用 "/Users/chenjunming/Desktop/myenv_312/bin/python3.12" 命令来执行python代码"""
common_init_prompt_2 = f"""深度理解:
1) 当前代码结构, 
2) {design_md} 文档
3) {task_md} 文档
4) {requirement_md} 文档"""

# 开发模式下的各个智能体初始化提示
coding_agent_init_prompt = {
    '需求分析师': f"""现在起, 你是一个专业的需求分析师, 并且十分了解python代码.
我正在进行代码开发. 我**待会儿**会告诉你我刚刚做了什么修改. 
在收到我的代码修改描述后, 我需要你:
走读修改后的新代码, 然后分析代码中的逻辑是否与 {task_md} 一致, 是否与 {design_md} 一致.
审核完成后说明有问题的地方, 若无问题则返回 '检查通过'. 禁止修改代码与文档. 不要返回多余的信息.
""",
    '审核员': f"""现在起, 你是一个专业的python代码审核员, 熟悉python的用法和语法. 
我正在进行代码开发. 我**待会儿**会告诉你我刚刚做了什么修改.
在收到我的代码修改描述后, 我需要你:
走读我修改后的新代码, 然后分析代码中是否存在错误和逻辑问题. 是否与 {task_md} 和 {design_md} 保持逻辑一致.
审核完成后说明有问题的地方, 若无问题则返回 '审核通过'. 禁止修改代码与文档. 不要返回多余的信息.
""",
    '测试工程师': f"""现在起, 你是一个专业的python测试工程师, 熟悉python的用法和语法. 
我正在进行代码开发. 我**待会儿**会告诉你我刚刚做了什么修改. 
在收到我的代码修改描述后, 我需要你:
走读我修改后的新代码, 分析代码中是否存在错误和逻辑问题. 是否与 {task_md} 一致, 是否与 {design_md} 一致.
然后根据 {test_plan_md} 执行测试. 审核以及测试完成后说明有问题的地方, 若无问题则返回 '测试通过'. 不要返回多余的信息.
禁止修改主体代码, 但是可以创建和修改测试用代码. 所有与测试相关的工作都需要由你来做. 
""",
    '开发工程师': f"""现在起, 你是一个专业的Python开发工程师.
我待会儿会按照 {task_md} 的任务安排一步步的让你执行对应任务的开发.
在收到我的任务安排后, 你需要你:
根据 {design_md} 以及 {task_md} 中的描述, 进行对应任务的开发.
开发完成后, 你需要重新走读自己开发的新代码, 检查是否与需求对齐, 是否与任务描述一致, 检查是否存在逻辑错误.
完成检查后, 你需要设计单元测试用例, 进行自测.
开发, 审核, 自测全部完成后在 {task_md} 中勾选掉对应的任务, 然后返回简要说明你具体修改了什么. 不要返回多余的信息.
"""
}
agent_names_list = ['需求分析师', '审核员', '测试工程师', '开发工程师']


# 运行智能代理函数
def run_agent(agent_name, log_file_path, prompt, init_yn=True, session_id=None):
    """
    运行智能代理函数

    Args:
        agent_name (str): 智能代理的名称
        log_file_path (str): 日志文件路径，用于记录运行过程中的消息
        prompt (str): 提示信息，传递给智能代理的输入内容
        init_yn (bool, optional): 是否初始化新会话，默认为True表示新建会话，False表示恢复现有会话
        session_id (str, optional): 会话ID，当init_yn为False时必须提供有效的会话ID
    Returns:
        str: 返回当前会话的ID，用于后续的会话管理
    Raises:
        ValueError: 当 init_yn 为 False 且 session_id 为空时抛出异常
    """
    # 记录用户输入的提示信息到日志文件
    with print_lock:
        log_message(log_file_path=log_file_path,
                    message=f"--{agent_name}--\n--{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}--\n"
                            f"{prompt}\n",
                    color=Colors.BLUE)

    # 根据初始化标志选择不同的处理方式：新建会话或恢复会话
    if init_yn:
        retry_count = 0
        while True:
            _, msg, session_id = init_codex(prompt=prompt,
                                            folder_path=working_path,
                                            model_name=working_model,
                                            reasoning_effort=working_effort,
                                            timeout=working_timeout
                                            )
            if session_id:
                break
            retry_count += 1
            if retry_count > resume_retry_max:
                msg = ["init_codex 获取 session_id 失败，已达到最大重试次数。"]
                break
            with print_lock:
                log_message(log_file_path=log_file_path,
                            message=f"--{session_id}--\n--{agent_name}--\n"
                                    f"--{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}--\n"
                                    f"init_codex 获取 session_id 失败，准备重试 {retry_count}/{resume_retry_max}\n",
                            color=Colors.YELLOW)
            time.sleep(resume_retry_interval)
    else:
        if session_id is None:
            raise ValueError("resume 模式下, session_id 不能为空")
        retry_count = 0
        while True:
            _, msg, _ = resume_codex(thread_id=session_id,
                                     folder_path=working_path,
                                     prompt=prompt,
                                     model_name=working_model,
                                     reasoning_effort=working_effort,
                                     timeout=working_timeout
                                     )
            if msg and str(msg[0]).strip():
                break
            retry_count += 1
            if retry_count > resume_retry_max:
                msg = ["resume_codex 超时或无响应，已达到最大重试次数。"]
                break
            with print_lock:
                log_message(log_file_path=log_file_path,
                            message=f"--{session_id}--\n--{agent_name}--\n"
                                    f"--{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}--\n"
                                    f"resume_codex 超时或无响应，准备重试 {retry_count}/{resume_retry_max}\n",
                            color=Colors.YELLOW)
            time.sleep(resume_retry_interval)
    # 记录会话结果到日志文件
    with print_lock:
        log_message(log_file_path=log_file_path,
                    message=f"--{session_id}--\n--{agent_name}--\n"
                            f"--{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}--\n"
                            f"{msg[0]}\n",
                    color=Colors.GREEN)
    return msg[0], session_id
