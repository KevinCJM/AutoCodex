# -*- encoding: utf-8 -*-
"""
@File: B04_human_prompts.py
@Modify Time: 2026/3/20
@Author: Kevin-Chen
@Descriptions: 人工维护的项目输入配置
"""

import os
from pathlib import Path

# 这个文件放“需要人类根据项目背景自行改写”的项目输入。
# 默认情况下直接使用当前仓库根目录，便于本仓库自举验证。
PROJECT_ROOT = Path(
    os.environ.get("AUTOCODEX_PROJECT_ROOT", str(Path(__file__).resolve().parent))
).resolve()

HUMAN_WORKING_PATH = PROJECT_ROOT

# 四阶段产物与结构化状态文件
HUMAN_REQUIREMENT_SPEC_MD = "01_requirement_spec.md"
HUMAN_REQUIREMENT_CLARIFICATION_MD = "01_clarification.md"
HUMAN_DESIGN_MD = "02_design.md"
HUMAN_DESIGN_TRACE_JSON = "02_design_trace.json"
HUMAN_TASK_MD = "03_task_plan.md"
HUMAN_TASK_SCHEDULE_JSON = "03_schedule.json"
HUMAN_TEST_PLAN_MD = "04_test_plan.md"
HUMAN_DELIVERY_REPORT_MD = "04_delivery_report.md"
HUMAN_TASK_RUN_REPORT_DIR = "04_task_runs"
HUMAN_WORKFLOW_STATE_JSON = "workflow_state.json"
HUMAN_WORKFLOW_EVENT_JSONL = "workflow_event.jsonl"

HUMAN_AGENT_MODEL_EFFORT_CONFIG = {
    "owner": {"model_name": "gpt-5.4", "reasoning_effort": "xhigh", "turn_timeout_sec": 3600},
    "analyst": {"model_name": "gpt-5.4", "reasoning_effort": "high", "turn_timeout_sec": 1800},
    "tester": {"model_name": "gpt-5.4", "reasoning_effort": "xhigh", "turn_timeout_sec": 2400},
    "auditor": {"model_name": "gpt-5.4", "reasoning_effort": "high", "turn_timeout_sec": 1800},
}

HUMAN_COMMON_INIT_PROMPT_1 = """记住:
1) 使用中文进行对话和文档编写;
2) 优先使用 "python3" 命令执行 Python 代码，只有项目明确要求时再切换解释器;
3) 修改前先阅读现有实现，尽量做小步、可验证的改动;
4) 你运行在 tmux + codex cli 长会话模式中，需要保持上下文连续性和阶段一致性。
"""

HUMAN_COMMON_INIT_PROMPT_2 = """请先建立一个轻量上下文，要求如下:
1) 只快速确认当前工作目录的主入口、主要阶段产物文件和关键运行时模块;
2) 此阶段不要做全面代码审查，不要长时间遍历旧模块;
3) 后续如果具体任务需要，再按需继续深入阅读相关文件;
4) 只需回复一句中文，确认你已经完成轻量初始化并会在后续按需继续阅读。
"""

HUMAN_REQUIREMENT_PROMPT = """
请将当前工作目录中的 AutoCodex 从旧的 `codex exec + director JSON` 模式，改造成 `tmux + codex cli` 长会话模式。

新的目标架构必须满足:
1. 使用 1 个 owner 主agent 负责四个连续步骤:
   - 需求指定
   - 详细设计
   - 任务规划
   - 开发与测试
2. 使用 3 个 reviewer subagent:
   - analyst
   - tester
   - auditor
3. owner 是唯一的阶段 owner，负责真实落盘文档、修改代码、执行测试、更新任务状态。
4. analyst / tester / auditor 在每个步骤都要从各自角度审核 owner 的阶段产物，并通过结构化 verdict token 控制是否进入下一阶段。
5. 工作流必须以 Python 状态机推进，不再依赖 director 自报完成；需要显式写入 workflow_state.json 和 workflow_event.jsonl。
6. 任务规划必须同时输出人类可读的 Markdown 与机器可读的 03_schedule.json，开发阶段以 JSON 计划为真值源。
7. 整体实现应优先复用 v1/tmux_cli_tools_lib 的 runtime 能力，而不是继续围绕 codex exec 的阻塞式调用打补丁。
"""
