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

# 工作目录
working_path = "/Users/chenjunming/Desktop/Canopy/canopy-api-v3"
# 模型名称
working_model = "gpt-5.2"
# 推理强度
working_effort = "high"
# 模型推理超时时间
working_timeout = 60 * 10
# 恢复会话重试次数
resume_retry_max = 5
# 恢复会话重试间隔时间
resume_retry_interval = 2

# 需求描述文件, 由[详细设计模式]生成,[任务拆分模式]和[开发模式]使用
design_md = "指标计算服务独立详细设计.md"
# 任务说明文件, 由[任务拆分模式]生成,[开发模式]使用
task_md = "任务拆分.md"
# 测试计划文件, 由[开发模式]生成和使用
test_plan_md = "测试计划.md"  # 由测试工程师智能体生成的

# [通用] 初始化提示词
common_init_prompt_1 = """记住:
1) 使用中文进行对话和文档编写;
2) 使用 "/Users/chenjunming/Desktop/myenv_310/bin/python" 命令来执行python代码
"""
# [通用] 初始化提示词2
common_init_prompt_2 = f"""了解代码架构, 主要是:
深度理解以 canopy-api-v3/canopy_api_v3/app.py 和 canopy-api-v3/canopy_api_v3/asgi.py 为入口了解API调用全链路逻辑, 另外 canopy-api-v3/canopy_api_v3/core/calculation/call_table_data_api_demo.py 为API测试逻辑
深度理解以 canopy-api-v3/canopy_api_v3/core/calculation/indicator_tester.py 为入口的指标计算全链路逻辑
"""

# 可用智能体列表
agent_names_list = ['需求分析师', '审核员', '测试工程师', '开发工程师']

# [开发模式] 下的测试工程师智能体初始化提示词
coding_test_agent_init_prompt = f"""你是一个专业的python测试工程师. 
你需要根据 {design_md} 中的描述, 以及 {task_md} 中的任务计划, 设计各个任务对应的测试.
要求每个任务完成后有一个对应的功能测试 (覆盖度>90%), 以及一个对应的集成测试 (覆盖度>90%). 
根据此, 设计对应的测试用例. 写入 {test_plan_md} 文件中.
"""
# [开发模式] 下的各个智能体初始化提示词
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

# [通用] 原始需求说明
requirement_str = """
将 canopy-api-v3/canopy_api_v3/core/calculation 文件夹下面的代码进行改造, 使其可以独立为单独的服务.
改造完成后, 需要满足以下要求:
1. 代码需要独立为单独的服务, 不能依赖 canopy-api-v3/canopy_api_v3/core/calculation 外部代码
2. API需要使用 fastapi 框架实现.
3. 对已有代码尽可能少的改动

API主要功能为:
1) 解析header, 识别权限
    - 当前系统里负责解析 Header 并做权限校验的代码入口是: authorizer.py, guard.py, ctx.py
2) 改写前端传入的json为可用于指标计算的json
3) 指标计算主流程
4) 将结果解析为传给前端的json

能力范围：
- 必须覆盖
    - /api/v3/data/table_data/{target_user_id}
    - /api/v3/data/table_data
    - /api/v3/data/chart_data/{target_user_id}
    - /api/v3/data/chart_data
    - /api/v3/calculation/fetch_data
    - /api/v3/filters/column_option
    - /api/v3/mobile/filters/options
- 服务内统一做这四步
    - 解析 Authorization 并鉴权
    - 前端 JSON 预处理（编译成 calculation profile）
    - 执行 calculation（CalculationSessionFactory）
    - 输出与现有前端完全兼容的 JSON
"""
# [详细设计模式] 下需求分析师 智能体的初始化提示
analysis_agent_init_prompt = f"""现在起, 你是一个专业的需求分析师 和 产品经理. $Product Manager
现在有以下需求: {requirement_str}

根据以上需求补充, 审视当前代码. 进行代码改造的详细设计.
将详细设计写入 {design_md} 中.
"""
# [任务拆分模式] 下需求分析师 智能体的初始化提示
task_agent_init_prompt = f"""现在起, 你是一个专业的需求分析师 和 产品经理. $Scrum Master
现在有以下需求: {requirement_str}

当前已经根据需求进行了详细设计, 并且写了详细设计文档: {design_md}
根据详细设计 {design_md} 拆分任务单. 将写入 {task_md} 中.
"""


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
            if session_id and msg and str(msg[0]).strip():
                break
            retry_count += 1
            if retry_count > resume_retry_max:
                if not session_id:
                    msg = ["init_codex 获取 session_id 失败，已达到最大重试次数。"]
                else:
                    msg = ["init_codex 未返回有效内容，已达到最大重试次数。"]
                break
            with print_lock:
                log_message(log_file_path=log_file_path,
                            message=f"--{session_id}--\n--{agent_name}--\n"
                                    f"--{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}--\n"
                                    f"init_codex 未返回有效内容或 session_id 为空，准备重试 {retry_count}/{resume_retry_max}\n"
                                    f"msg: {msg}",
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
