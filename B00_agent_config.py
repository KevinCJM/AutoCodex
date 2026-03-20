# -*- encoding: utf-8 -*-
"""
@File: B00_agent_config.py
@Modify Time: 2026/3/20
@Author: Kevin-Chen
@Descriptions: 智能体配置参数
"""

from __future__ import annotations

import json
import os
import threading
import time
import tempfile
from datetime import datetime

from B01_codex_utils import (
    get_runtime_metadata as get_codex_runtime_metadata,
    init_codex,
    resume_codex,
)
from B02_log_tools import Colors, log_message
from B04_human_prompts import (
    HUMAN_AGENT_MODEL_EFFORT_CONFIG,
    HUMAN_COMMON_INIT_PROMPT_1,
    HUMAN_COMMON_INIT_PROMPT_2,
    HUMAN_DELIVERY_REPORT_MD,
    HUMAN_DESIGN_MD,
    HUMAN_DESIGN_TRACE_JSON,
    HUMAN_REQUIREMENT_CLARIFICATION_MD,
    HUMAN_REQUIREMENT_PROMPT,
    HUMAN_REQUIREMENT_SPEC_MD,
    HUMAN_TASK_MD,
    HUMAN_TASK_RUN_REPORT_DIR,
    HUMAN_TASK_SCHEDULE_JSON,
    HUMAN_TEST_PLAN_MD,
    HUMAN_WORKFLOW_EVENT_JSONL,
    HUMAN_WORKFLOW_STATE_JSON,
    HUMAN_WORKING_PATH,
)

print_lock = threading.Lock()

OWNER_AGENT_NAME = "owner"
REVIEWER_AGENT_NAMES = ("analyst", "tester", "auditor")
DIRECTOR_NAME = "director"
DIRECTOR_OUTPUT_KEYS = ("success", OWNER_AGENT_NAME, *REVIEWER_AGENT_NAMES)

now = datetime.now()
today_str = f"{now.year}{now.month:02d}{now.day:02d}"

# 工作目录
working_path = HUMAN_WORKING_PATH
# 默认单轮推理等待时长（秒）
working_timeout = 60 * 30
# 恢复会话重试次数
resume_retry_max = 3
# 恢复会话重试间隔时间
resume_retry_interval = 2

# 四阶段产物
requirement_spec_md = HUMAN_REQUIREMENT_SPEC_MD
REQUIREMENT_CLARIFICATION_MD = HUMAN_REQUIREMENT_CLARIFICATION_MD
design_md = HUMAN_DESIGN_MD
design_trace_json = HUMAN_DESIGN_TRACE_JSON
task_md = HUMAN_TASK_MD
task_schedule_json = HUMAN_TASK_SCHEDULE_JSON
test_plan_md = HUMAN_TEST_PLAN_MD
delivery_report_md = HUMAN_DELIVERY_REPORT_MD
task_run_report_dir = HUMAN_TASK_RUN_REPORT_DIR
workflow_state_json = HUMAN_WORKFLOW_STATE_JSON
workflow_event_jsonl = HUMAN_WORKFLOW_EVENT_JSONL

HUMAN_QUESTION_TRIGGER = "[[ASK_HUMAN]]"
MAX_HUMAN_QA_ROUND = 100

REVIEW_VERDICT_PASS = "[[ACX_VERDICT:PASS]]"
REVIEW_VERDICT_REVISE = "[[ACX_VERDICT:REVISE]]"
REVIEW_VERDICT_BLOCKED = "[[ACX_VERDICT:BLOCKED]]"
REVIEW_VERDICT_ASK_HUMAN = "[[ACX_VERDICT:ASK_HUMAN]]"
REVIEW_VERDICT_TOKENS = (
    REVIEW_VERDICT_PASS,
    REVIEW_VERDICT_REVISE,
    REVIEW_VERDICT_BLOCKED,
    REVIEW_VERDICT_ASK_HUMAN,
)
REVIEW_PASS_TOKEN = REVIEW_VERDICT_PASS
OWNER_STAGE_DONE_TOKEN = "[[ACX_STAGE_DONE]]"
DEVELOPMENT_DONE_TOKEN = "[[ACX_DEVELOPMENT_DONE]]"
BOOTSTRAP_INIT_TOKEN = "[[ACX_INIT_READY]]"
BOOTSTRAP_CONTEXT_TOKEN = "[[ACX_CONTEXT_READY]]"
BOOTSTRAP_ROLE_TOKEN = "[[ACX_ROLE_READY]]"

# [人工维护] 智能体模型与推理强度配置
AGENT_MODEL_EFFORT_CONFIG = HUMAN_AGENT_MODEL_EFFORT_CONFIG
# [人工维护] 初始化提示词
common_init_prompt_1 = HUMAN_COMMON_INIT_PROMPT_1
common_init_prompt_2 = HUMAN_COMMON_INIT_PROMPT_2
# [人工维护] 原始需求说明
requirement_str = HUMAN_REQUIREMENT_PROMPT

# agent skills
AGENT_SKILLS_DICT = {
    OWNER_AGENT_NAME: ["$Product Manager", "$System Architect", "$Scrum Master", "$Developer"],
    "analyst": ["$Business Analyst"],
    "tester": ["$Developer"],
    "auditor": ["$System Architect"],
}

agent_names_list = [OWNER_AGENT_NAME, *REVIEWER_AGENT_NAMES]
ANALYST_NAME = OWNER_AGENT_NAME  # 兼容旧模块导入

TASK_DOC_STRUCTURE_PROMPT = f"""`{task_md}` 必须与 `{task_schedule_json}` 保持一致，并同时满足:
1) Markdown 文档用于人类阅读，JSON 用于机器调度，两者都必须更新;
2) 文档必须先按里程碑分组，再在每个里程碑下列出任务单;
3) 调度真值源是 `{task_schedule_json}`，不得只写 Markdown 不写 JSON;
4) 每个任务单必须足够具体，单次可开发、可审核、可测试、可勾选;
5) 每个任务单都必须写清目标、涉及模块/文件、完成标准、验证方式。

推荐 Markdown 模板:
## 里程碑 M1: <名称>
- 状态: todo / doing / done
- 阶段性成果: <该里程碑完成后应达成的结果>
- 完成判定: <如何判断该里程碑已经达成>
- 任务单:
  - [ ] M1-T1 <任务标题> | 目标: <做什么> | 涉及: <模块/文件> | 完成标准: <完成判定> | 验证: <测试/检查方式>
  - [ ] M1-T2 <任务标题> | 目标: <做什么> | 涉及: <模块/文件> | 完成标准: <完成判定> | 验证: <测试/检查方式>

推荐 JSON 模板:
```json
{{
  "milestones": [
    {{
      "milestone_id": "M1",
      "title": "里程碑名称",
      "status": "todo",
      "goal": "阶段性成果",
      "completion_criteria": ["完成判定1"],
      "tasks": [
        {{
          "task_id": "M1-T1",
          "title": "任务标题",
          "status": "todo",
          "objective": "做什么",
          "files": ["path/to/file.py"],
          "done_criteria": ["完成标准1"],
          "verification": ["pytest ..."]
        }}
      ]
    }}
  ]
}}
```
"""

TASK_EXECUTION_PROMPT = f"""执行 `{task_schedule_json}` / `{task_md}` 时必须遵守:
1) 开发阶段永远以“当前最早未完成里程碑下的下一个未完成任务单”为推进单位;
2) owner 只能处理一个具体任务单，不能跨任务单越界开发;
3) 完成后必须同步更新:
   - `{task_md}`
   - `{task_schedule_json}`
   - `{test_plan_md}`
   - `{delivery_report_md}`
4) 每个任务单必须生成一份 `{task_run_report_dir}/run_<task_id>.json` 运行记录;
5) reviewer 只审核，不直接修改主体代码;
6) 三个 reviewer 对同一个 artifact_sha 都返回 `{REVIEW_VERDICT_PASS}` 之后，才允许进入下一阶段或下一个任务单。
"""

OWNER_AGENT_INIT_PROMPT = f"""你现在是 AutoCodex 的主agent owner。
你负责整个项目从需求到实现的四阶段闭环:
1) 需求指定
2) 详细设计
3) 任务规划
4) 开发与测试

你的职责:
1) 你是唯一 owner，负责阅读代码、编写/修改阶段文档、修改主体代码、执行测试、更新任务状态;
2) analyst / tester / auditor 只负责审核与你对话，不直接改主体代码;
3) 任何阶段在 reviewer 未全部通过前，禁止擅自推进到下一阶段;
4) 原则上在“需求指定”阶段完成全部人类澄清；如果 reviewer 明确返回 `{REVIEW_VERDICT_ASK_HUMAN}`，你也可以补充向人类一次只提一个关键问题，并把结论同步回需求与澄清文档;
5) 所有阶段都必须真实落盘文档或代码，不得只在回复里声称已完成;
6) 开发阶段必须严格遵守 `{task_schedule_json}` 的任务顺序和 `{TASK_EXECUTION_PROMPT}`。

完成角色设定后，只回复 `{BOOTSTRAP_ROLE_TOKEN}`，不要补充其它内容。
"""

REVIEWER_INIT_PROMPTS = {
    "analyst": f"""你现在是 reviewer analyst。
你只负责审核 owner 的阶段产物，不允许修改主体代码与阶段文档。
你的关注点:
1) 需求边界是否单一清晰、可验证、无隐含假设;
2) 需求、设计、任务、实现之间是否一致，是否存在越界实现;
3) 文档与代码是否遗漏关键信息、关键场景、失败路径或兼容性约束。
你的输出必须以 verdict token 开头，只能使用以下四种之一:
{REVIEW_VERDICT_PASS}
{REVIEW_VERDICT_REVISE}
{REVIEW_VERDICT_BLOCKED}
{REVIEW_VERDICT_ASK_HUMAN}

完成角色设定后，只回复 `{BOOTSTRAP_ROLE_TOKEN}`，不要补充其它内容。
""",
    "tester": f"""你现在是 reviewer tester。
你只负责审核 owner 的阶段产物，不允许修改主体代码与阶段文档。
你的关注点:
1) 需求、设计、任务是否具备可测试性和可验收性;
2) 每个任务单是否写清验证命令、测试范围、回归影响;
3) 开发阶段的代码改动是否有对应测试证据，是否存在漏测、伪通过或缺失回归。
你的输出必须以 verdict token 开头，只能使用以下四种之一:
{REVIEW_VERDICT_PASS}
{REVIEW_VERDICT_REVISE}
{REVIEW_VERDICT_BLOCKED}
{REVIEW_VERDICT_ASK_HUMAN}

完成角色设定后，只回复 `{BOOTSTRAP_ROLE_TOKEN}`，不要补充其它内容。
""",
    "auditor": f"""你现在是 reviewer auditor。
你只负责审核 owner 的阶段产物，不允许修改主体代码与阶段文档。
你的关注点:
1) 架构边界、状态一致性、失败路径、恢复路径、日志与可观测性是否合理;
2) 工作流是否存在死循环、误判完成、串 session、任务重复派发等风险;
3) 文档、JSON 真值源、代码与运行记录是否一致。
你的输出必须以 verdict token 开头，只能使用以下四种之一:
{REVIEW_VERDICT_PASS}
{REVIEW_VERDICT_REVISE}
{REVIEW_VERDICT_BLOCKED}
{REVIEW_VERDICT_ASK_HUMAN}

完成角色设定后，只回复 `{BOOTSTRAP_ROLE_TOKEN}`，不要补充其它内容。
""",
}

# 兼容旧模块导入
analysis_agent_init_prompt = OWNER_AGENT_INIT_PROMPT
task_agent_init_prompt = OWNER_AGENT_INIT_PROMPT
coding_test_agent_init_prompt = REVIEWER_INIT_PROMPTS["tester"]
coding_agent_init_prompt = {
    OWNER_AGENT_NAME: OWNER_AGENT_INIT_PROMPT,
    "analyst": REVIEWER_INIT_PROMPTS["analyst"],
    "tester": REVIEWER_INIT_PROMPTS["tester"],
    "auditor": REVIEWER_INIT_PROMPTS["auditor"],
    "需求分析师": REVIEWER_INIT_PROMPTS["analyst"],
    "审核员": REVIEWER_INIT_PROMPTS["auditor"],
    "测试工程师": REVIEWER_INIT_PROMPTS["tester"],
    "开发工程师": OWNER_AGENT_INIT_PROMPT,
}


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


def get_agent_runtime_info(agent_name):
    agent_runtime_cfg = AGENT_MODEL_EFFORT_CONFIG.get(agent_name) or {}
    model_name = str(agent_runtime_cfg.get("model_name", "gpt-5.4")).strip() or "gpt-5.4"
    reasoning_effort = str(agent_runtime_cfg.get("reasoning_effort", "high")).strip() or "high"
    return get_codex_runtime_metadata(
        folder_path=working_path,
        agent_name=agent_name,
        model_name=model_name,
        reasoning_effort=reasoning_effort,
    )


def run_agent(agent_name, log_file_path, prompt, init_yn=True, session_id=None, required_token=None, reply_validator=None):
    """
    运行智能代理函数
    """
    agent_runtime_cfg = AGENT_MODEL_EFFORT_CONFIG.get(agent_name)
    if not agent_runtime_cfg:
        raise ValueError(
            f"AGENT_MODEL_EFFORT_CONFIG 缺少智能体配置: {agent_name}. "
            f"请在 B04_human_prompts.py 中显式配置 model_name 和 reasoning_effort."
        )
    model_name = str(agent_runtime_cfg.get("model_name", "")).strip()
    reasoning_effort = str(agent_runtime_cfg.get("reasoning_effort", "")).strip()
    turn_timeout_sec = int(agent_runtime_cfg.get("turn_timeout_sec", working_timeout))
    if not model_name or not reasoning_effort:
        raise ValueError(
            f"AGENT_MODEL_EFFORT_CONFIG 配置不完整: {agent_name}. "
            f"当前配置: {agent_runtime_cfg}"
        )
    output_schema_path = ensure_director_output_schema() if agent_name == DIRECTOR_NAME else None

    with print_lock:
        log_message(
            log_file_path=log_file_path,
            message=f"--{agent_name}--\n--{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}--\n{prompt}\n",
            color=Colors.BLUE,
        )

    if init_yn:
        retry_count = 0
        while True:
            responses, msg, session_id = init_codex(
                prompt=prompt,
                folder_path=working_path,
                model_name=model_name,
                reasoning_effort=reasoning_effort,
                timeout=turn_timeout_sec,
                output_schema_path=output_schema_path,
                agent_name=agent_name,
                required_token=required_token,
                reply_validator=reply_validator,
            )
            if session_id and str(msg or "").strip() and (
                    required_token is None or required_token in str(msg or "")
            ) and (
                    reply_validator is None or reply_validator(str(msg or ""))
            ):
                break
            retry_count += 1
            if retry_count > resume_retry_max:
                if not session_id:
                    msg = "init_codex 获取 session_id 失败，已达到最大重试次数。"
                elif required_token and required_token not in str(msg or ""):
                    msg = f"init_codex 未返回期望 token {required_token}，已达到最大重试次数。"
                else:
                    msg = "init_codex 未返回有效内容，已达到最大重试次数。"
                break
            with print_lock:
                log_message(
                    log_file_path=log_file_path,
                    message=(
                        f"--{session_id}--\n--{agent_name}--\n"
                        f"--{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}--\n"
                        f"init_codex 未返回有效内容或 session_id 为空，准备重试 {retry_count}/{resume_retry_max}\n"
                        f"responses: {responses}\nmsg: {msg}"
                    ),
                    color=Colors.YELLOW,
                )
            time.sleep(resume_retry_interval)
    else:
        if session_id is None:
            raise ValueError("resume 模式下, session_id 不能为空")
        retry_count = 0
        while True:
            responses, msg, _ = resume_codex(
                thread_id=session_id,
                folder_path=working_path,
                prompt=prompt,
                model_name=model_name,
                reasoning_effort=reasoning_effort,
                timeout=turn_timeout_sec,
                output_schema_path=output_schema_path,
                agent_name=agent_name,
                required_token=required_token,
                reply_validator=reply_validator,
            )
            if str(msg or "").strip() and (
                    required_token is None or required_token in str(msg or "")
            ) and (
                    reply_validator is None or reply_validator(str(msg or ""))
            ):
                break
            retry_count += 1
            if retry_count > resume_retry_max:
                if required_token and required_token not in str(msg or ""):
                    msg = f"resume_codex 未返回期望 token {required_token}，已达到最大重试次数。"
                else:
                    msg = "resume_codex 超时或无响应，已达到最大重试次数。"
                break
            with print_lock:
                log_message(
                    log_file_path=log_file_path,
                    message=(
                        f"--{session_id}--\n--{agent_name}--\n"
                        f"--{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}--\n"
                        f"resume_codex 超时或无响应，准备重试 {retry_count}/{resume_retry_max}\n"
                        f"responses: {responses}\n"
                    ),
                    color=Colors.YELLOW,
                )
            time.sleep(resume_retry_interval)

    runtime_info = get_agent_runtime_info(agent_name)
    runtime_summary = json.dumps(runtime_info, ensure_ascii=False, indent=2)
    with print_lock:
        log_message(
            log_file_path=log_file_path,
            message=(
                f"--{session_id}--\n--{agent_name}--\n"
                f"--{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}--\n"
                f"{msg}\n\n[runtime]\n{runtime_summary}\n"
            ),
            color=Colors.GREEN,
        )
    return msg, session_id


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
