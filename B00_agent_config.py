# -*- encoding: utf-8 -*-
"""
@File: B00_agent_config.py
@Modify Time: 2026/1/12 11:03       
@Author: Kevin-Chen
@Descriptions: 智能体配置参数
"""

import json
import os
import threading
import time
import tempfile
from datetime import datetime

from B01_codex_utils import init_codex, resume_codex
from B02_log_tools import Colors, log_message
from B04_human_prompts import (
    HUMAN_AGENT_MODEL_EFFORT_CONFIG,
    HUMAN_COMMON_INIT_PROMPT_1,
    HUMAN_COMMON_INIT_PROMPT_2,
    HUMAN_DESIGN_MD,
    HUMAN_REQUIREMENT_PROMPT,
    HUMAN_REQUIREMENT_CLARIFICATION_MD,
    HUMAN_TASK_MD,
    HUMAN_TEST_PLAN_MD,
    HUMAN_WORKING_PATH,
)

print_lock = threading.Lock()
DIRECTOR_NAME = "调度器"
DIRECTOR_OUTPUT_KEYS = ("success", "需求分析师", "审核员", "测试工程师", "开发工程师")

now = datetime.now()
today_str = f"{now.year}{now.month:02d}{now.day:02d}"

# 工作目录
working_path = HUMAN_WORKING_PATH
# 模型推理超时时间
working_timeout = 60 * 10
# 恢复会话重试次数
resume_retry_max = 5
# 恢复会话重试间隔时间
resume_retry_interval = 2

# 需求描述文件, 由[详细设计模式]生成,[任务拆分模式]和[开发模式]使用
design_md = HUMAN_DESIGN_MD
# 任务说明文件, 由[任务拆分模式]生成,[开发模式]使用
task_md = HUMAN_TASK_MD
# 测试计划文件, 由[开发模式]生成和使用
test_plan_md = HUMAN_TEST_PLAN_MD  # 由测试工程师智能体生成的
# [详细设计模式] 人类问答触发词
HUMAN_QUESTION_TRIGGER = "[[ASK_HUMAN]]"
# [详细设计模式] 允许向人类提问的智能体名称
ANALYST_NAME = "需求分析师"
# [详细设计模式] 最大人类问答轮数
MAX_HUMAN_QA_ROUND = 100
# [跨阶段] 人类问答,需求澄清记录文件名
REQUIREMENT_CLARIFICATION_MD = HUMAN_REQUIREMENT_CLARIFICATION_MD
# [人工维护] 智能体模型与推理强度配置
AGENT_MODEL_EFFORT_CONFIG = HUMAN_AGENT_MODEL_EFFORT_CONFIG

# [人工维护] 初始化提示词, 默认使用测试/演示内容
common_init_prompt_1 = HUMAN_COMMON_INIT_PROMPT_1
common_init_prompt_2 = HUMAN_COMMON_INIT_PROMPT_2

# 可用智能体列表
agent_names_list = ['需求分析师', '审核员', '测试工程师', '开发工程师']


def _build_director_output_schema():
    all_properties = {
        key: {
            "type": "string",
        }
        for key in DIRECTOR_OUTPUT_KEYS
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": all_properties,
        "required": list(DIRECTOR_OUTPUT_KEYS),
    }


def ensure_director_output_schema():
    schema_path = os.path.join(tempfile.gettempdir(), "autocodex_director_output_schema.json")
    schema = _build_director_output_schema()
    need_write = True
    if os.path.exists(schema_path):
        try:
            with open(schema_path, "r", encoding="utf-8") as f:
                need_write = json.load(f) != schema
        except (json.JSONDecodeError, OSError):
            need_write = True
    if need_write:
        with open(schema_path, "w", encoding="utf-8") as f:
            json.dump(schema, f, ensure_ascii=False, indent=2)
    return schema_path


def normalize_director_payload(payload, allow_nested_success=True, allowed_success_values=None):
    if not isinstance(payload, dict):
        raise ValueError("调度器返回的JSON必须是对象")

    if allowed_success_values is None:
        normalized_success_values = None
    elif isinstance(allowed_success_values, str):
        normalized_success_values = {allowed_success_values.strip()}
    else:
        normalized_success_values = {str(value).strip() for value in allowed_success_values if str(value).strip()}
        if not normalized_success_values:
            normalized_success_values = None

    allowed_keys = set(DIRECTOR_OUTPUT_KEYS)
    unknown_keys = [key for key in payload.keys() if key not in allowed_keys]
    if unknown_keys:
        raise ValueError(f"调度器返回了未知字段: {unknown_keys}")

    normalized = {}
    for key in DIRECTOR_OUTPUT_KEYS:
        if key not in payload:
            continue
        value = payload[key]
        if value is None:
            text = ""
        elif isinstance(value, str):
            text = value.strip()
        else:
            raise ValueError(f"调度器字段 {key} 的值必须是字符串")
        if text:
            normalized[key] = text

    if not normalized:
        raise ValueError("调度器返回的JSON归一化后为空")
    if "success" in normalized and len(normalized) != 1:
        raise ValueError("调度器返回格式非法: success 不能与其他字段同时出现")
    if allow_nested_success and "success" in normalized:
        success_text = normalized["success"]
        if success_text.startswith("{") and success_text.endswith("}"):
            try:
                nested_payload = json.loads(success_text)
            except json.JSONDecodeError:
                nested_payload = None
            if isinstance(nested_payload, dict):
                return normalize_director_payload(
                    nested_payload,
                    allow_nested_success=False,
                    allowed_success_values=normalized_success_values,
                )
    if "success" in normalized and normalized_success_values is not None:
        success_text = normalized["success"]
        if success_text not in normalized_success_values:
            raise ValueError(
                f"调度器 success 字段非法: {success_text!r}. "
                f"当前阶段只允许: {sorted(normalized_success_values)}"
            )
    return normalized

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

# [人工维护] 原始需求说明, 默认使用测试/演示内容
requirement_str = HUMAN_REQUIREMENT_PROMPT
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
    # 解析当前智能体对应的模型与推理强度（强校验：必须显式配置）
    agent_runtime_cfg = AGENT_MODEL_EFFORT_CONFIG.get(agent_name)
    if not agent_runtime_cfg:
        raise ValueError(
            f"AGENT_MODEL_EFFORT_CONFIG 缺少智能体配置: {agent_name}. "
            f"请在 B00_agent_config.py 中显式配置 model_name 和 reasoning_effort."
        )
    model_name = str(agent_runtime_cfg.get("model_name", "")).strip()
    reasoning_effort = str(agent_runtime_cfg.get("reasoning_effort", "")).strip()
    if not model_name or not reasoning_effort:
        raise ValueError(
            f"AGENT_MODEL_EFFORT_CONFIG 配置不完整: {agent_name}. "
            f"当前配置: {agent_runtime_cfg}"
        )
    output_schema_path = ensure_director_output_schema() if agent_name == DIRECTOR_NAME else None

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
                                            model_name=model_name,
                                            reasoning_effort=reasoning_effort,
                                            timeout=working_timeout,
                                            output_schema_path=output_schema_path,
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
                                     model_name=model_name,
                                     reasoning_effort=reasoning_effort,
                                     timeout=working_timeout,
                                     output_schema_path=output_schema_path,
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


def format_agent_skills(agent_name, agent_skills_dict):
    """
    统一将技能配置格式化为 prompt 前缀，支持 str / list / tuple / set
    """
    skills = agent_skills_dict.get(agent_name, [])
    if isinstance(skills, str):
        skills_list = [skills.strip()] if skills.strip() else []
    elif isinstance(skills, (list, tuple, set)):
        skills_list = [str(skill).strip() for skill in skills if str(skill).strip()]
    else:
        raise ValueError(f"agent_skills_dict 配置类型错误: {agent_name} -> {type(skills)}")
    return " ".join(skills_list)
