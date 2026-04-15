# -*- encoding: utf-8 -*-
"""
@File: B04_human_prompts.py
@Modify Time: 2026/3/11
@Author: Kevin-Chen
@Descriptions: 人工维护的项目输入配置
"""

import os
from pathlib import Path

# 这个文件放“需要人类根据项目背景自行改写”的项目输入。
# 包括工作目录、产物文件名，以及 prompt 文本。
# 默认内容使用测试/演示场景，不包含任何公司内部仓库提示。

PROJECT_ROOT = Path("/Users/chenjunming/Desktop/canopy-api-v3")

HUMAN_WORKING_PATH = PROJECT_ROOT
HUMAN_DESIGN_MD = "详细设计.md"
HUMAN_TASK_MD = "任务拆分.md"
HUMAN_TEST_PLAN_MD = "示例测试计划.md"
HUMAN_REQUIREMENT_CLARIFICATION_MD = "需求澄清记录.md"
HUMAN_AGENT_MODEL_EFFORT_CONFIG = {
    "调度器": {"model_name": "gpt-5.3-codex", "reasoning_effort": "xhigh"},
    "需求分析师": {"model_name": "gpt-5.4", "reasoning_effort": "xhigh"},
    "审核员": {"model_name": "gpt-5.4", "reasoning_effort": "xhigh"},
    "测试工程师": {"model_name": "gpt-5.3-codex", "reasoning_effort": "xhigh"},
    "开发工程师": {"model_name": "gpt-5.4", "reasoning_effort": "xhigh"},
}

HUMAN_COMMON_INIT_PROMPT_1 = """记住:
1) 使用中文为主语言进行对话和文档编写;
2) 使用 "/Users/chenjunming/Desktop/myenv_310/bin/python" 命令来执行python代码;
3) 本次 session 可以, 每个指令都可以使用 subagent, 但是不超过5个
4) 禁止修改代码
"""

HUMAN_COMMON_INIT_PROMPT_2 = """深度了解当前代码架构, 以及各个API的调用链路, 主要了解哪些涉及到 calculation 计算层的链路.
我写了一个文档 老架构API_calculation使用分析.md 可以帮助你理解.
"""

HUMAN_REQUIREMENT_PROMPT = """深度了解改造前的原始代码架构, 以及各个API的调用链路, 主要了解哪些涉及到 calculation 计算层的链路.
当前有个新需求, 并且已经开发完成: 将 calculation 计算层服务做独立化, 用fastapi做独立的接口供后端python调用.
基于这个需求, 写了以下文档:
    需求说明.md
    需求补充_shared拆分说明.md
    需求附录_后端接入与灰度.md
    需求附录_数据结构定义.md
    需求附录_测试与验收计划.md
    需求附录_部署与运行前置.md
    老架构API_calculation使用分析.md
深入理解这些文档.

我根据需求写了 详细设计.md 文档 以及 任务拆分.md 文档
并且按照 详细设计.md 文档 以及 任务拆分.md 文档进行了代码的改造.

我现在需要你对架构做深度了解, 对需求和详细设计做深度了解: 需求说明.md , 详细设计.md
然后按照 任务拆分.md 文档, 逐个核对当前改造后的代码改造. 分析是否存在 bug, 是否存在遗漏, 是否存在错误, 是否存在偏离需求或设计的改动.

注意, 使用 Linus三问原则 做审核. 确保不做过度设计和代码修改. 对于原架构中原有的逻辑尽量不做改动. 
如果要改原架构代码, 需要自问这个改动是不是需求必须的? 如果不改会不会导致当前需求无法进行? 能不能不改?

可以使用 subagent 做协助, 但是不超过3个.
分析所有还未提交的代码, 检查代码改造是否有错误或遗漏.
禁止修改代码.
"""
