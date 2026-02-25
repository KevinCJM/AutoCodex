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
import sys
from datetime import timedelta

from B00_agent_config import *
from B02_log_tools import Colors, log_message
from B03_init_function_agents import init_agent

print_lock = threading.Lock()

DIRECTOR_NAME = "调度器"
STATE_FILE_NAME = "workflow_state.json"

_TOKEN_RE = re.compile(r"^--(.+?)--$")

base_director_prompt = f"""
你是一个专业的调度智能体.
现在有: 需求分析师, 审核员, 测试工程师, 开发工程师 四个智能体
当前阶段为开发阶段. 开发流程需要按照 {task_md} 中的任务安排一步步的让开发工程师智能体执行对应任务的开发.
当开发工程师智能体完成对应任务的开发后, 你需要通知 需求分析师, 审核员, 测试工程师 分别对 开发工程师 智能体的代码进行审核和测试.

---
主要流程如下:

1) 阅读 {task_md}, 检查下一个需要开发的任务是什么. 通知开发工程师智能体进行对应任务的开发.
调用开发工程师智能体时, prompt 模板如下:
    ```
    开始开发 {task_md} 中的 任务xx
    开发完成后, 重新走读自己开发的新代码, 检查是否与需求对齐, 检查是否存在逻辑错误. 完成检查后, 你需要设计单元测试用例, 进行自测.
    开发, 审核, 自测全部完成后在 {task_md} 中勾选掉对应的任务, 然后返回简要说明你具体修改了什么. 不要返回多余的信息.
    ```

2) 当开发工程师智能体完成对应任务的开发后, 整理开发工程师返回的内容. 然后结合开发内容, 通知 需求分析智能体, 审核员智能体, 测试工程师智能体 进行代码审核.
2.1) 调用 需求分析智能体 时, prompt 模板如下:
    ```
    开发工程师完成了以下开发:
    {{开发工程师返回的开发说明}}

    走读修改后的新代码, 然后分析代码中的逻辑是否与 {task_md} 一致, 是否与 {design_md} 一致.
    审核完成后说明有问题的地方, 若无问题则返回 '检查通过'. 禁止修改代码与文档.
    ```
2.2) 调用 审核员智能体 时, prompt 模板如下:
    ```
    开发工程师完成了以下开发:
    {{开发工程师返回的开发说明}}

    走读我修改后的新代码, 然后分析代码中是否存在错误和逻辑问题. 是否与 {task_md} 和 {design_md} 保持逻辑一致.
    审核完成后说明有问题的地方, 若无问题则返回 '审核通过'. 禁止修改代码与文档.
    ```
2.3) 调用 测试工程师智能体 时, prompt 模板如下:
    ```
    开发工程师完成了以下开发:
    {{开发工程师返回的开发说明}}

    走读我修改后的新代码, 分析代码中是否存在错误和逻辑问题. 是否与 {task_md} 一致, 是否与 {design_md} 一致.
    然后根据 {test_plan_md} 执行测试. 审核以及测试完成后说明有问题的地方, 若无问题则返回 '测试通过'. 不要返回多余的信息.
    禁止修改主体代码, 可以创建和修改测试用代码. 测试相关的代码与问题都需要由你来处理.
    ```

3) 收集 需求分析智能体, 审核员智能体, 测试工程师智能体 的审核结果.
3.1) 如果所有智能体都没有提出问题或者有疑问点, 则认为该任务开发完成, 回到步骤1, 继续下一个任务的开发.
3.2) 如果有智能体提出问题, 则整理所有智能体提出的问题点和疑问点, 回到步骤2, 通知开发工程师智能体进行修复. prompt 模板如下:
    ```
    你是一个专业的 python 开发工程师, 针对你刚刚完成的开发内容, 专家团队有一些问题/疑问点:

    需求分析师发现以下问题:
    {{具体内容}}

    需求分析师有以下疑问点:
    {{具体内容}}

    审核员发现以下问题:
    {{具体内容}}

    审核员有以下疑问点:
    {{具体内容}}

    测试工程师发现以下问题:
    {{具体内容}}

    测试工程师有以下疑问点:
    {{具体内容}}

    1) 总结并去重上面提到的问题, 总结并去重上面的疑问/假设
    2) 详细分析这些问题是否属实, 回答疑问, 回答假设
    3) 说明属实的问题是哪些.
    4) 是否存在疑问/分歧的问题, 并说明疑问/分歧点是什么. 然后对这些疑问/分歧点进行深度设计.
    5) 对属实的问题进行修复. 并且说明对应的假设.
    6) 说明你改了什么
    注意: 只返回上面1~6点相关的内容, 不要返回其他无关的内容.
    ```

4) 当所有任务都开发完成后, 结束整个开发流程. 返回如下JSON:
    ```
    {{"success": "所有任务开发完成"}}
    ```
---
返回JSON格式的数据, 格式需要是 {{智能体名称: 提示词}} 格式如下:
{{"开发工程师": "相关提示词..."}}
{{"需求分析师": "相关提示词...", "审核员": "相关提示词...", "测试工程师": "相关提示词..."}}
注意JSON内部双引号需要有转义符, 保证JSON合法

---
智能体名称必须是: 开发工程师, 需求分析师, 审核员, 测试工程师
返回的格式必须严格满足JSON格式要求.
"""


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


def _parse_output_entry(entry, expected_agent_name=None):
    lines = entry.splitlines()
    for idx in range(len(lines) - 1):
        session_id = _extract_token(lines[idx])
        agent_name = _extract_token(lines[idx + 1])
        if session_id and agent_name:
            if expected_agent_name and agent_name != expected_agent_name:
                continue
            message = "\n".join(lines[idx + 2:]).strip()
            return {
                "session_id": session_id,
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
    fenced = text.strip()
    if fenced.startswith("```") and fenced.endswith("```"):
        fenced = fenced.strip("`").strip()
        try:
            return json.loads(fenced)
        except json.JSONDecodeError:
            pass
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


def _save_state(state_path, state):
    if not state_path:
        return
    with open(state_path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def _log_error(log_file_path, message):
    with print_lock:
        log_message(log_file_path=log_file_path, message=message, color=Colors.RED)


def _rebuild_state_from_logs(log_dir, max_log_days, strict_json):
    director_log = _find_latest_log_file(log_dir, DIRECTOR_NAME, max_log_days)
    if not director_log:
        raise RuntimeError("未找到调度器日志，无法恢复。")
    director_entries = _read_log_entries(director_log)
    director_output = _find_last_output(director_entries, DIRECTOR_NAME)
    if not director_output:
        raise RuntimeError("调度器日志中未找到有效输出，无法恢复。")
    director_session_id = director_output["session_id"]
    msg_dict = _try_parse_json(director_output["message"], strict_json=strict_json)
    if not msg_dict:
        raise RuntimeError("调度器输出无法解析为JSON，无法恢复。")

    state = {
        "phase": "director_ready",
        "iteration": 0,
        "director_session_id": director_session_id,
        "agent_session_id_dict": {},
        "pending_agents": [],
        "agent_prompts": {},
        "agent_responses": {},
        "last_director_prompt": "",
        "last_director_response": director_output["message"],
        "msg_dict": msg_dict,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }

    if "success" in msg_dict:
        state["phase"] = "completed"
        return state

    for agent_name, prompt in msg_dict.items():
        if agent_name not in agent_names_list:
            raise RuntimeError(f"调度器返回了未知的智能体名称: {agent_name}")
        agent_log = _find_latest_log_file(log_dir, agent_name, max_log_days)
        entries = _read_log_entries(agent_log)
        last_output = _find_last_output(entries, agent_name)
        if last_output:
            state["agent_session_id_dict"][agent_name] = last_output["session_id"]
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
        dry_run=False,
):
    if state_path is None:
        state_path = os.path.join(working_path, STATE_FILE_NAME)
    if log_dir is None:
        log_dir = working_path

    state = None
    if prefer_checkpoint:
        state = _load_state(state_path)
    if not state:
        state = _rebuild_state_from_logs(log_dir, max_log_days, strict_json)

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
        }

    director_log_path = os.path.join(log_dir, f"agent_{DIRECTOR_NAME}_{today_str}.log")
    director_session_id = state.get("director_session_id")
    if not director_session_id:
        raise RuntimeError("调度器 session_id 缺失，无法恢复。")

    msg_dict = state.get("msg_dict", {})
    iteration = int(state.get("iteration", 0))
    if "success" in msg_dict:
        return {"status": "completed", "phase": "completed", "iteration": iteration}

    agent_session_id_dict = state.get("agent_session_id_dict", {})
    agent_responses = state.get("agent_responses", {})

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

        pending_agents = []
        for agent_name, agent_prompt in msg_dict.items():
            if agent_name not in agent_names_list:
                raise RuntimeError(f"调度器返回了未知的智能体名称: {agent_name}")
            if agent_name in agent_responses:
                continue
            session_id = agent_session_id_dict.get(agent_name)
            if not session_id:
                if allow_reinit_on_missing_session:
                    _, session_id = init_agent(agent_name)
                    agent_session_id_dict[agent_name] = session_id
                else:
                    raise RuntimeError(f"{agent_name} 缺失 session_id，无法恢复。")
            pending_agents.append(agent_name)
            log_file_path = os.path.join(log_dir, f"agent_{agent_name}_{today_str}.log")
            msg, _ = run_agent(
                agent_name,
                log_file_path,
                agent_prompt,
                init_yn=False,
                session_id=session_id,
            )
            agent_responses[agent_name] = msg

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

        msg, _ = run_agent(
            DIRECTOR_NAME,
            director_log_path,
            doing_director_prompt,
            init_yn=False,
            session_id=director_session_id,
        )
        msg_dict = _try_parse_json(msg, strict_json=strict_json)
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


if __name__ == "__main__":
    recover_workflow(dry_run="--dry-run" in sys.argv)
