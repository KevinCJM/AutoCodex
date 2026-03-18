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

PROJECT_ROOT = Path("/your/project/path")

HUMAN_WORKING_PATH = PROJECT_ROOT
HUMAN_DESIGN_MD = "示例详细设计.md"
HUMAN_TASK_MD = "示例任务拆分.md"
HUMAN_TEST_PLAN_MD = "示例测试计划.md"
HUMAN_REQUIREMENT_CLARIFICATION_MD = "需求澄清记录.md"
HUMAN_AGENT_MODEL_EFFORT_CONFIG = {
    "调度器": {"model_name": "gpt-5.3-codex", "reasoning_effort": "medium"},
    "需求分析师": {"model_name": "gpt-5.3-codex", "reasoning_effort": "high"},
    "审核员": {"model_name": "gpt-5.4", "reasoning_effort": "xhigh"},
    "测试工程师": {"model_name": "gpt-5.3-codex", "reasoning_effort": "high"},
    "开发工程师": {"model_name": "gpt-5.4", "reasoning_effort": "xhigh"},
}

HUMAN_COMMON_INIT_PROMPT_1 = """记住:
1) 使用中文进行对话和文档编写;
2) 优先使用 "python3" 命令执行 Python 代码，只有项目明确要求时再切换解释器;
3) 修改前先阅读现有实现，尽量做小步、可验证的改动
"""

HUMAN_COMMON_INIT_PROMPT_2 = """了解当前工作目录中的测试项目架构, 主要是:
1) 以 A00_main.py 为总入口理解三个阶段的串联方式
2) 深度理解 A01_requiment_analysis_workflow.py、A02_task_workflow.py、A03_coding_agent_workflow.py 的阶段职责与调度关系
3) 深度理解 B00_agent_config.py、B01_codex_utils.py、B03_init_function_agents.py 的配置、会话管理与初始化逻辑
4) 深度理解 C01_recover_requirement_workflow.py、C02_recover_task_workflow.py、C03_recover_coding_workflow.py 的恢复机制
"""

HUMAN_REQUIREMENT_PROMPT = """
请基于当前工作目录中的 Python 项目做一次测试性质的自动化改造演练，要求如下:
1. 保持现有三阶段工作流结构不变
2. 将人工维护的 prompt 与运行参数解耦，便于后续替换为真实项目提示
3. README 需要说明哪些配置属于运行参数，哪些 prompt 需要人工维护
4. 尽量少改动既有流程控制逻辑
5. 所有文档与输出都以演示/测试场景为准，不要假设存在公司内部仓库背景
"""
