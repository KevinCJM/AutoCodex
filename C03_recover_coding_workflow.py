# -*- encoding: utf-8 -*-
"""
@File: C02_recover_coding_workflow.py
@Modify Time: 2026/1/12 12:30
@Author: Kevin-Chen
@Descriptions: 恢复被中断的多智能体代码开发流程
"""
import json
import os
import re
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import timedelta

from A03_coding_agent_workflow import (
    DIRECTOR_SUCCESS_TEXT,
    base_director_prompt,
    build_director_invalid_completion_retry_prompt,
    ensure_valid_coding_director_response,
    find_next_unfinished_coding_task,
    prepare_agent_prompt,
)
from B00_agent_config import *
from B02_log_tools import Colors, log_message

print_lock = threading.Lock()

DIRECTOR_NAME = "调度器"
STATE_FILE_NAME = "workflow_state.json"
FRESH_SESSION_SUMMARY_LIMIT = 1500
FRESH_SESSION_AGENT_RESPONSE_LIMIT = 900

_TOKEN_RE = re.compile(r"^--(.+?)--$")
_TIMESTAMP_TOKEN_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")


def _is_separator_line(line):
    stripped = line.strip()
    if not stripped:
        return False
    if len(stripped) < 50:
        return False
    return all(ch == "=" for ch in stripped)


def _split_log_entries(text):
    entries = []
    buf = []
    for raw_line in text.splitlines():
        if _is_separator_line(raw_line):
            entry = "\n".join(buf).strip("\n")
            if entry:
                entries.append(entry)
            buf = []
        else:
            buf.append(raw_line)
    entry = "\n".join(buf).strip("\n")
    if entry:
        entries.append(entry)
    return entries


def _extract_token(line):
    match = _TOKEN_RE.match(line.strip())
    if match:
        return match.group(1)
    return None


def _is_timestamp_token(token):
    if not token:
        return False
    return bool(_TIMESTAMP_TOKEN_RE.match(token))


def _parse_output_entry(entry, expected_agent_name=None):
    lines = entry.splitlines()
    for idx in range(len(lines) - 1):
        session_id = _extract_token(lines[idx])
        agent_name = _extract_token(lines[idx + 1])
        if session_id and agent_name:
            if expected_agent_name and agent_name != expected_agent_name:
                continue
            message_start = idx + 2
            timestamp_token = _extract_token(lines[idx + 2]) if idx + 2 < len(lines) else None
            if _is_timestamp_token(timestamp_token):
                message_start = idx + 3
            message = "\n".join(lines[message_start:]).strip()
            return {
                "session_id": session_id,
                "agent_name": agent_name,
                "message": message,
            }
    return None


def _parse_prompt_entry(entry, expected_agent_name=None):
    lines = entry.splitlines()
    for idx in range(len(lines) - 1):
        prev_token = _extract_token(lines[idx - 1]) if idx > 0 else None
        agent_name = _extract_token(lines[idx])
        timestamp_token = _extract_token(lines[idx + 1])
        if prev_token or not agent_name or not _is_timestamp_token(timestamp_token):
            continue
        if expected_agent_name and agent_name != expected_agent_name:
            continue
        message = "\n".join(lines[idx + 2:]).strip()
        return {
            "agent_name": agent_name,
            "message": message,
        }
    return None


def _find_last_output(entries, agent_name):
    for entry in reversed(entries):
        parsed = _parse_output_entry(entry, expected_agent_name=agent_name)
        if parsed:
            return parsed
    return None


def _find_last_prompt(entries, agent_name):
    for entry in reversed(entries):
        parsed = _parse_prompt_entry(entry, expected_agent_name=agent_name)
        if parsed:
            return parsed
    return None


def _find_latest_json_output(entries, agent_name, strict_json, session_id=None):
    for entry in reversed(entries):
        parsed = _parse_output_entry(entry, expected_agent_name=agent_name)
        if not parsed:
            continue
        if session_id and parsed["session_id"] != session_id:
            continue
        msg_dict = _try_parse_json(parsed["message"], strict_json=strict_json)
        if not msg_dict:
            continue
        try:
            msg_dict = normalize_director_payload(msg_dict, allowed_success_values={DIRECTOR_SUCCESS_TEXT})
        except ValueError:
            continue
        return parsed, msg_dict
    return None, None


def _find_prompt_index(entries, prompt_text):
    if not prompt_text:
        return None
    for idx in range(len(entries) - 1, -1, -1):
        if prompt_text in entries[idx]:
            return idx
    return None


def _find_next_output_after(entries, start_index, agent_name):
    for idx in range(start_index + 1, len(entries)):
        parsed = _parse_output_entry(entries[idx], expected_agent_name=agent_name)
        if parsed:
            return parsed
    return None


def _read_log_entries(log_path):
    if not log_path or not os.path.exists(log_path):
        return []
    with open(log_path, "r", encoding="utf-8") as f:
        return _split_log_entries(f.read())


def _find_latest_log_file(log_dir, agent_name, max_log_days):
    if not log_dir or not os.path.isdir(log_dir):
        return None
    prefix = f"agent_{agent_name}_"
    candidates = []
    for name in os.listdir(log_dir):
        if not name.startswith(prefix) or not name.endswith(".log"):
            continue
        path = os.path.join(log_dir, name)
        if not os.path.isfile(path):
            continue
        candidates.append(path)
    if not candidates:
        return None
    candidates.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    if max_log_days is None:
        return candidates[0]
    deadline = datetime.now() - timedelta(days=max_log_days)
    for path in candidates:
        if datetime.fromtimestamp(os.path.getmtime(path)) >= deadline:
            return path
    return candidates[0]


def _try_parse_json(text, strict_json):
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        if strict_json:
            return None
    fenced = str(text or "").strip()
    if fenced.startswith("```") and fenced.endswith("```"):
        fenced = fenced.strip("`").strip()
        try:
            return json.loads(fenced)
        except json.JSONDecodeError:
            pass
    text = str(text or "")
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            return None
    return None


def _load_state(state_path):
    if not state_path or not os.path.exists(state_path):
        return None
    with open(state_path, "r", encoding="utf-8") as f:
        return json.load(f)


def _coerce_invalid_completed_state(state):
    if not state:
        return state

    msg_dict = state.get("msg_dict") or {}
    if "success" not in msg_dict:
        if state.get("phase") == "director_retry":
            if state.get("completion_blocker"):
                return state
            last_director_response = state.get("last_director_response", "")
            last_response_dict = _try_parse_json(last_director_response, strict_json=False)
            if last_response_dict:
                try:
                    last_response_dict = normalize_director_payload(
                        last_response_dict,
                        allowed_success_values={DIRECTOR_SUCCESS_TEXT},
                    )
                except ValueError:
                    last_response_dict = None
            if isinstance(last_response_dict, dict) and "success" in last_response_dict:
                unfinished_task = find_next_unfinished_coding_task()
                if unfinished_task:
                    state["completion_blocker"] = unfinished_task
                    return state
        state.pop("completion_blocker", None)
        return state

    unfinished_task = find_next_unfinished_coding_task()
    if not unfinished_task:
        state.pop("completion_blocker", None)
        return state

    if not state.get("director_session_id") or not state.get("last_director_prompt"):
        raise RuntimeError(
            f'调度器返回了 "{DIRECTOR_SUCCESS_TEXT}", '
            f'但 {task_md} 仍存在未完成任务且缺少可重试上下文，无法恢复。'
        )

    state.update({
        "phase": "director_retry",
        "pending_agents": [],
        "agent_prompts": {},
        "agent_responses": {},
        "msg_dict": {},
        "completion_blocker": unfinished_task,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    })
    return state


def _normalize_loaded_state(state):
    if not state:
        return None
    msg_dict = state.get("msg_dict")
    if not msg_dict:
        return state
    state["msg_dict"] = normalize_director_payload(msg_dict, allowed_success_values={DIRECTOR_SUCCESS_TEXT})
    return state


def _save_state(state_path, state):
    if not state_path:
        return
    with open(state_path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def _log_error(log_file_path, message):
    with print_lock:
        log_message(log_file_path=log_file_path, message=message, color=Colors.RED)


def _backfill_agent_sessions_from_logs(state, log_dir, max_log_days):
    agent_session_id_dict = state.setdefault("agent_session_id_dict", {})
    for agent_name in agent_names_list:
        if agent_session_id_dict.get(agent_name):
            continue
        agent_log = _find_latest_log_file(log_dir, agent_name, max_log_days)
        entries = _read_log_entries(agent_log)
        last_output = _find_last_output(entries, agent_name)
        if last_output:
            agent_session_id_dict[agent_name] = last_output["session_id"]
    return state


def _build_director_retry_prompt(last_director_prompt):
    return f"""{str(last_director_prompt or '').rstrip()}

---
补充要求:
你上一条回复没有返回合法且符合调度协议的 JSON.
这一次只能返回严格合法的 JSON, 不要返回解释、计划、思考过程、Markdown 代码块.
- success 字段只能在整个开发流程真正完成时使用
- 如果 success 非空, 它的值必须精确等于 "{DIRECTOR_SUCCESS_TEXT}"
- 如果当前只是准备读取、分析、规划下一步, 必须把 prompt 放到对应智能体字段, success 必须为空字符串
"""


def _clip_text(text, limit):
    text = str(text or "").strip()
    if len(text) <= limit:
        return text
    return f"{text[:limit].rstrip()}...(截断)"


def _strip_base_director_prompt(prompt):
    prompt = str(prompt or "")
    if prompt.startswith(base_director_prompt):
        return prompt[len(base_director_prompt):].lstrip()
    return prompt


def _build_fresh_session_handoff_summary(state):
    next_task = find_next_unfinished_coding_task()
    pending_agents = state.get("pending_agents") or list((state.get("msg_dict") or {}).keys())
    lines = [
        "这是一个被中断后恢复的多智能体开发流程，不是新需求。",
        f"- 工作目录: {working_path}",
        f"- 关键文档: {design_md}, {task_md}, {REQUIREMENT_CLARIFICATION_MD}, {test_plan_md}",
        f"- 当前恢复阶段: {state.get('phase')}",
        f"- 当前迭代: {int(state.get('iteration', 0))}",
    ]

    completion_blocker = state.get("completion_blocker")
    if completion_blocker:
        blocker_desc = " ".join(
            part for part in [completion_blocker.get("task_id", ""), completion_blocker.get("title", "")]
            if str(part).strip()
        )
        lines.append(f"- 上一轮错误地宣称已完成，但实际阻塞任务是: {blocker_desc}")

    if next_task:
        next_desc = " ".join(
            part for part in [next_task.get("task_id", ""), next_task.get("title", "")] if str(part).strip())
        lines.append(f"- 当前下一个未完成任务: {next_desc}")

    if pending_agents:
        lines.append(f"- 当前待继续处理的智能体: {pending_agents}")

    last_director_response = _clip_text(state.get("last_director_response", ""), FRESH_SESSION_SUMMARY_LIMIT)
    if last_director_response:
        lines.append("最近一次调度器返回摘要:")
        lines.append(last_director_response)

    agent_responses = state.get("agent_responses", {}) or {}
    if agent_responses:
        lines.append("当前轮已拿到的智能体输出摘要:")
        for agent_name, response in agent_responses.items():
            lines.append(f"- {agent_name}: {_clip_text(response, FRESH_SESSION_AGENT_RESPONSE_LIMIT)}")

    return "\n".join(lines).strip()


def _build_fresh_agent_prompt(agent_name, agent_prompt, state):
    handoff_summary = _build_fresh_session_handoff_summary(state)
    extra_rule = ""
    if agent_name == "测试工程师":
        extra_rule = (
            f"\n额外要求:\n"
            f"- {test_plan_md} 若已存在，先阅读并沿用其当前内容，不要因为 session 重建而整份重写。\n"
        )
    prompt_body = f"""{common_init_prompt_1}

{common_init_prompt_2}

{coding_agent_init_prompt[agent_name]}

---
恢复上下文:
{handoff_summary}

执行要求:
- 你现在接管的是一个已经跑到中途的开发流程。
- {task_md} 中已勾选的任务视为已经完成，不要重复开发或重复审核它们。
- 先理解当前代码库、文档和上面的恢复信息，再继续执行下面这一轮任务。{extra_rule}

当前轮任务如下:
{agent_prompt}
""".strip()
    return prepare_agent_prompt(agent_name, prompt_body)


def _build_fresh_director_prompt(director_prompt, state):
    handoff_summary = _build_fresh_session_handoff_summary(state)
    prompt_tail = _strip_base_director_prompt(director_prompt)
    return f"""{base_director_prompt}

---
恢复上下文:
{handoff_summary}

执行要求:
- 这是被中断后的续跑，不是从零开始。
- {task_md} 中已勾选任务视为已完成，不要重复调度已勾选任务。
- 你必须基于当前代码、任务单勾选状态、已有审查/测试输出继续调度。

{prompt_tail}
""".strip()


def _run_director_recovery_turn(director_prompt, state, log_dir, session_id):
    director_log_path = os.path.join(log_dir, f"agent_{DIRECTOR_NAME}_{today_str}.log")
    if not session_id:
        effective_prompt = _build_fresh_director_prompt(director_prompt, state)
        msg, session_id = run_agent(
            DIRECTOR_NAME,
            director_log_path,
            effective_prompt,
            init_yn=True,
            session_id=None,
        )
        return msg, session_id, effective_prompt

    msg, _ = run_agent(
        DIRECTOR_NAME,
        director_log_path,
        director_prompt,
        init_yn=False,
        session_id=session_id,
    )
    return msg, session_id, director_prompt


def _run_agent_recovery_turn(agent_name, agent_prompt, state, log_dir, session_id):
    log_file_path = os.path.join(log_dir, f"agent_{agent_name}_{today_str}.log")
    if not session_id:
        effective_prompt = _build_fresh_agent_prompt(agent_name, agent_prompt, state)
        msg, session_id = run_agent(
            agent_name,
            log_file_path,
            effective_prompt,
            init_yn=True,
            session_id=None,
        )
        return agent_name, msg, session_id

    msg, _ = run_agent(
        agent_name,
        log_file_path,
        prepare_agent_prompt(agent_name, agent_prompt),
        False,
        session_id,
    )
    return agent_name, msg, session_id


def _rebuild_state_from_logs(log_dir, max_log_days, strict_json):
    director_log = _find_latest_log_file(log_dir, DIRECTOR_NAME, max_log_days)
    if not director_log:
        raise RuntimeError("未找到调度器日志，无法恢复。")
    director_entries = _read_log_entries(director_log)
    last_director_output = _find_last_output(director_entries, DIRECTOR_NAME)
    last_director_prompt = _find_last_prompt(director_entries, DIRECTOR_NAME)
    if not last_director_output and not last_director_prompt:
        raise RuntimeError("调度器日志中未找到有效输入或输出，无法恢复。")

    director_output = last_director_output
    director_session_id = director_output["session_id"] if director_output else None
    msg_dict = None
    if director_output:
        msg_dict = _try_parse_json(director_output["message"], strict_json=strict_json)
        if msg_dict:
            try:
                msg_dict = normalize_director_payload(msg_dict, allowed_success_values={DIRECTOR_SUCCESS_TEXT})
            except ValueError:
                msg_dict = None
    if not msg_dict and director_session_id:
        director_output, msg_dict = _find_latest_json_output(
            director_entries,
            DIRECTOR_NAME,
            strict_json=strict_json,
            session_id=director_session_id,
        )

    if not msg_dict:
        if director_session_id and last_director_prompt:
            return {
                "phase": "director_retry",
                "iteration": 0,
                "director_session_id": director_session_id,
                "agent_session_id_dict": {},
                "pending_agents": [],
                "agent_prompts": {},
                "agent_responses": {},
                "last_director_prompt": last_director_prompt["message"],
                "last_director_response": last_director_output["message"] if last_director_output else "",
                "msg_dict": {},
                "updated_at": datetime.now().isoformat(timespec="seconds"),
            }
        raise RuntimeError("调度器输出无法解析为JSON，且当前会话缺少可重试的提示词，无法恢复。")

    state = {
        "phase": "director_ready",
        "iteration": 0,
        "director_session_id": director_session_id,
        "agent_session_id_dict": {},
        "pending_agents": [],
        "agent_prompts": {},
        "agent_responses": {},
        "last_director_prompt": last_director_prompt["message"] if last_director_prompt else "",
        "last_director_response": director_output["message"],
        "msg_dict": msg_dict,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }

    state = _backfill_agent_sessions_from_logs(state, log_dir, max_log_days)
    state = _coerce_invalid_completed_state(state)
    msg_dict = state.get("msg_dict", msg_dict)

    if "success" in msg_dict:
        state["phase"] = "completed"
        return state

    for agent_name, prompt in msg_dict.items():
        if agent_name not in agent_names_list:
            raise RuntimeError(f"调度器返回了未知的智能体名称: {agent_name}")
        agent_log = _find_latest_log_file(log_dir, agent_name, max_log_days)
        entries = _read_log_entries(agent_log)
        state["agent_prompts"][agent_name] = prompt
        prompt_index = _find_prompt_index(entries, prompt)
        if prompt_index is not None:
            output = _find_next_output_after(entries, prompt_index, agent_name)
            if output:
                state["agent_responses"][agent_name] = output["message"]
                continue
        state["pending_agents"].append(agent_name)

    if state["pending_agents"]:
        state["phase"] = "agents_pending"
    else:
        state["phase"] = "director_pending"
    return state


def recover_workflow(
        state_path=None,
        log_dir=None,
        prefer_checkpoint=True,
        max_log_days=3,
        strict_json=True,
        allow_reinit_on_missing_session=False,
        use_fresh_sessions=False,
        dry_run=False,
):
    if state_path is None:
        state_path = os.path.join(working_path, STATE_FILE_NAME)
    if log_dir is None:
        log_dir = working_path

    state = None
    if prefer_checkpoint:
        state = _load_state(state_path)
        try:
            state = _normalize_loaded_state(state)
        except ValueError:
            state = None
    if not state:
        state = _rebuild_state_from_logs(log_dir, max_log_days, strict_json)
    state = _backfill_agent_sessions_from_logs(state, log_dir, max_log_days)
    state = _coerce_invalid_completed_state(state)

    if dry_run:
        state["updated_at"] = datetime.now().isoformat(timespec="seconds")
        _save_state(state_path, state)
        return {
            "status": "dry_run",
            "phase": state.get("phase"),
            "iteration": int(state.get("iteration", 0)),
            "pending_agents": state.get("pending_agents", []),
            "director_session_id": state.get("director_session_id"),
            "agent_session_id_dict": state.get("agent_session_id_dict", {}),
            "completion_blocker": state.get("completion_blocker"),
            "use_fresh_sessions": use_fresh_sessions,
        }

    director_log_path = os.path.join(log_dir, f"agent_{DIRECTOR_NAME}_{today_str}.log")
    director_session_id = None if use_fresh_sessions else state.get("director_session_id")
    if not director_session_id and not (use_fresh_sessions or allow_reinit_on_missing_session):
        raise RuntimeError("调度器 session_id 缺失，无法恢复。")

    msg_dict = state.get("msg_dict", {})
    iteration = int(state.get("iteration", 0))
    if "success" in msg_dict:
        return {"status": "completed", "phase": "completed", "iteration": iteration}

    agent_session_id_dict = {} if use_fresh_sessions else dict(state.get("agent_session_id_dict", {}))
    agent_responses = state.get("agent_responses", {})

    if state.get("phase") == "director_retry":
        retry_prompt = state.get("last_director_prompt")
        if not retry_prompt:
            raise RuntimeError("缺少调度器上一次提示词，无法重试。")
        completion_blocker = state.get("completion_blocker")
        if completion_blocker:
            retry_prompt = build_director_invalid_completion_retry_prompt(retry_prompt, completion_blocker)
        else:
            retry_prompt = _build_director_retry_prompt(retry_prompt)
        msg, director_session_id, retry_prompt = _run_director_recovery_turn(
            retry_prompt,
            state,
            log_dir,
            director_session_id,
        )
        try:
            msg, msg_dict, retry_prompt = ensure_valid_coding_director_response(
                msg,
                director_session_id,
                director_log_path,
                retry_prompt,
            )
        except ValueError:
            msg_dict = None
        if not msg_dict:
            _log_error(director_log_path, f"调度器重试后仍返回非JSON，无法解析:\n{msg}")
            raise RuntimeError("调度器重试后仍未返回合法JSON，恢复中断。")
        state.update({
            "phase": "director_ready",
            "director_session_id": director_session_id,
            "last_director_prompt": retry_prompt,
            "last_director_response": msg,
            "msg_dict": msg_dict,
            "completion_blocker": None,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        })
        _save_state(state_path, state)

    while True:
        if not msg_dict:
            raise RuntimeError("调度器输出为空，无法继续恢复。")
        if "success" in msg_dict:
            state.update({
                "phase": "completed",
                "msg_dict": msg_dict,
                "updated_at": datetime.now().isoformat(timespec="seconds"),
            })
            _save_state(state_path, state)
            return {"status": "completed", "phase": "completed", "iteration": iteration}

        pending_agent_calls = []
        for agent_name, agent_prompt in msg_dict.items():
            if agent_name not in agent_names_list:
                raise RuntimeError(f"调度器返回了未知的智能体名称: {agent_name}")
            if agent_name in agent_responses:
                continue
            session_id = agent_session_id_dict.get(agent_name)
            if not session_id:
                if allow_reinit_on_missing_session or use_fresh_sessions:
                    session_id = None
                else:
                    raise RuntimeError(f"{agent_name} 缺失 session_id，无法恢复。")
            pending_agent_calls.append({
                "agent_name": agent_name,
                "agent_prompt": agent_prompt,
                "session_id": session_id,
            })

        pending_agents = [item["agent_name"] for item in pending_agent_calls]
        with ThreadPoolExecutor(max_workers=max(1, len(pending_agent_calls))) as executor:
            futures = {
                executor.submit(
                    _run_agent_recovery_turn,
                    item["agent_name"],
                    item["agent_prompt"],
                    state,
                    log_dir,
                    item["session_id"],
                ): item
                for item in pending_agent_calls
            }
            for future in as_completed(futures):
                item = futures[future]
                agent_name, msg, session_id = future.result()
                agent_responses[agent_name] = msg
                agent_session_id_dict[agent_name] = session_id

        what_agent_just_use = list(msg_dict.keys())
        what_agent_replay = ""
        for agent_name in what_agent_just_use:
            msg = agent_responses.get(agent_name, "")
            what_agent_replay += f"{agent_name}: \n{msg}\n"

        doing_director_prompt = f"""
---
你刚刚调用的智能体为 {what_agent_just_use} 返回内容如下:
{what_agent_replay}
---
继续按照上述流程进行调度.
"""
        doing_director_prompt = base_director_prompt + doing_director_prompt

        msg, director_session_id, doing_director_prompt = _run_director_recovery_turn(
            doing_director_prompt,
            state,
            log_dir,
            director_session_id,
        )
        try:
            msg, msg_dict, doing_director_prompt = ensure_valid_coding_director_response(
                msg,
                director_session_id,
                director_log_path,
                doing_director_prompt,
            )
        except ValueError:
            msg_dict = None
        if not msg_dict:
            _log_error(director_log_path, f"调度器返回非JSON，无法解析:\n{msg}")
            raise RuntimeError("调度器输出无法解析为JSON，恢复中断。")

        iteration += 1
        agent_responses = {}
        state.update({
            "phase": "director_ready",
            "iteration": iteration,
            "director_session_id": director_session_id,
            "agent_session_id_dict": agent_session_id_dict,
            "pending_agents": pending_agents,
            "agent_prompts": msg_dict,
            "agent_responses": {},
            "last_director_prompt": doing_director_prompt,
            "last_director_response": msg,
            "msg_dict": msg_dict,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        })
        _save_state(state_path, state)


def _build_arg_parser():
    parser = argparse.ArgumentParser(description="恢复 A03 代码开发工作流")
    parser.add_argument("--state-path", default=None, help="状态文件路径")
    parser.add_argument("--log-dir", default=None, help="日志目录，默认使用 working_path")
    parser.add_argument("--max-log-days", default=3, type=int, help="日志回溯天数")
    parser.add_argument("--no-prefer-checkpoint", action="store_true", default=False, help="不优先使用 checkpoint")
    parser.add_argument("--strict-json", action="store_true", default=False, help="启用严格调度器 JSON 解析")
    parser.add_argument(
        "--allow-reinit-on-missing-session",
        action="store_true",
        default=False,
        help="缺失 session_id 时允许重建智能体会话",
    )
    parser.add_argument(
        "--fresh-sessions",
        action="store_true",
        default=False,
        help="忽略旧 session，使用一批全新的 codex session 从当前状态继续恢复",
    )
    parser.add_argument("--dry-run", action="store_true", default=False, help="仅重建状态，不执行恢复")
    return parser


if __name__ == "__main__":
    args = _build_arg_parser().parse_args()
    result = recover_workflow(
        state_path=args.state_path,
        log_dir=args.log_dir,
        prefer_checkpoint=not args.no_prefer_checkpoint,
        max_log_days=args.max_log_days,
        strict_json=args.strict_json,
        allow_reinit_on_missing_session=args.allow_reinit_on_missing_session,
        use_fresh_sessions=args.fresh_sessions,
        dry_run=args.dry_run,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
