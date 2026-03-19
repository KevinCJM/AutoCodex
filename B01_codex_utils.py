# -*- encoding: utf-8 -*-
"""
@File: codex_utils.py
@Modify Time: 2025/12/5 11:12       
@Author: Kevin-Chen
@Descriptions: codex exec 工具函数
"""
import json
import os
import subprocess
import tempfile
from subprocess import TimeoutExpired


def _truncate_text(text, max_chars=500):
    value = str(text or "")
    if len(value) <= max_chars:
        return value
    return value[:max_chars] + "…(truncated)"


# 运行 Codex 并解析返回的 JSON 事件流
def run_codex(cmd, timeout=300):
    """
    执行命令并解析返回的JSON事件流

    参数:
        cmd: 要执行的命令，可以是字符串或字符串列表
        timeout: 命令执行超时时间（秒），默认为300秒

    返回值:
        tuple: 包含四个元素的元组
            - events: 解析出的JSON事件对象列表
            - errs: 命令执行的错误输出
            - proc.returncode: 命令执行的返回码
            - parse_warnings: 被跳过的非 JSON 输出行（摘要）
    """
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=timeout,
        )
    except FileNotFoundError as e:
        return ([], f"找不到可执行文件：{e}", 127, [])
    except TimeoutExpired:
        return ([], f"命令执行超时（{timeout}s）：{cmd}", 124, [])
    raw = proc.stdout
    errs = proc.stderr

    # 解析标准输出中的JSON事件
    events = []
    parse_warnings = []
    for line in raw.splitlines():  # 遍历输出中的每一行
        line = line.strip()  # 去除空行
        if not line:
            continue
        try:
            ev = json.loads(line)  # 解析每一行中的JSON对象
            events.append(ev)  # 添加到列表中
        except json.JSONDecodeError:
            parse_warnings.append(_truncate_text(line, max_chars=200))
            continue

    return (events,  # 解析的 JSON 事件对象列表
            errs,  # 错误输出
            proc.returncode,  # 命令执行的返回码
            parse_warnings,  # 非 JSON 输出摘要
            )


# 处理事件列表
def handle_events(events):
    """
    遍历事件列表，根据事件类型进行分类处理、打印和信息收集。

    参数:
        events (list): 包含多个事件字典的列表。每个事件是一个具有 'type' 键的字典，
                       可能还包含其他与该事件相关的数据字段。

    返回:
        tuple:
            - responses: 调试与事件摘要信息
            - agent_message: 优先选择 phase=final_answer 的最终消息；若缺失则退化为最后一个 agent_message
            - thread_id: 会话 ID
    """
    responses = []
    thread_id = None
    final_messages = []
    fallback_agent_messages = []
    reasoning_count = 0
    command_count = 0
    file_change_count = 0
    error_count = 0
    other_item_counts = {}

    # 遍历所有事件并按类型分别处理
    for ev in events:
        t = ev.get("type")
        if t == "thread.started":
            thread_id = ev.get("thread_id")
            responses.append(f"[thread_id] → {thread_id}")
        elif t == "turn.started":
            # turn 开始 — 你也可以记录 prompt / turn index
            continue
        elif t == "item.completed":
            item = ev.get("item", {})
            i_type = item.get("type")
            if i_type == "agent_message":
                # 只消费已完成的 agent_message，并优先识别 final_answer
                text = str(item.get("text") or "")
                phase = item.get("phase")
                responses.append(f"[agent_message][phase={phase}] → {_truncate_text(text, max_chars=500)}")
                if text.strip():
                    fallback_agent_messages.append(text)
                    if phase == "final_answer":
                        final_messages.append(text)
            elif i_type == "reasoning":
                reasoning_count += 1
            elif i_type == "command_execution":
                command_count += 1
                cmd = item.get("command")
                exit_code = item.get("exit_code")
                responses.append(
                    f"[command #{command_count}] → exit_code={exit_code} cmd={_truncate_text(cmd, max_chars=300)}"
                )
            elif i_type == "file_change":
                file_change_count += 1
            elif i_type == "error":
                error_count += 1
                err = item.get("error") or item
                responses.append(f"[error #{error_count}] → {_truncate_text(err, max_chars=500)}")
            else:
                key = str(i_type or "unknown")
                other_item_counts[key] = other_item_counts.get(key, 0) + 1
        elif t.startswith("item."):
            # 只消费 item.completed，忽略 started / updated 等中间态
            continue
        elif t == "turn.completed":
            usage = ev.get("usage")
            responses.append(f"[TURN completed] → {usage}")
        else:
            # 可能是 other event type（session metadata, tool calls, etc.）
            responses.append(f"[Other event] → {ev}")

    if final_messages:
        agent_message = final_messages[-1]
    elif fallback_agent_messages:
        agent_message = fallback_agent_messages[-1]
    else:
        agent_message = ""

    if reasoning_count:
        responses.append(f"[reasoning_count] → {reasoning_count}")
    if file_change_count:
        responses.append(f"[file_change_count] → {file_change_count}")
    for item_type in sorted(other_item_counts):
        responses.append(f"[other_item_count:{item_type}] → {other_item_counts[item_type]}")

    return (responses,  # 信息列表
            agent_message,  # 智能体的回答
            thread_id  # session ID
            )


def _read_last_message(output_path):
    if not output_path or not os.path.exists(output_path):
        return ""
    try:
        with open(output_path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def _build_exec_cmd(
        *,
        model_name,
        reasoning_effort,
        output_last_message_path,
        folder_path=None,
        output_schema_path=None,
):
    cmd = [
        "codex", "exec",
        "--model", model_name,
        "--config", f"model_reasoning_effort={reasoning_effort}",
        "--skip-git-repo-check",
        "--json", "--full-auto",
        "--output-last-message", output_last_message_path,
    ]
    if folder_path:
        cmd.extend(["--cd", folder_path])
    if output_schema_path:
        cmd.extend(["--output-schema", output_schema_path])
    return cmd


def _finalize_exec_result(
        *,
        events,
        errs,
        return_code,
        parse_warnings,
        output_last_message_path,
        thread_id_hint=None,
):
    responses, fallback_message, thread_id = handle_events(events)
    output_last_message = _read_last_message(output_last_message_path)
    if output_last_message:
        agent_message = output_last_message
    else:
        agent_message = fallback_message

    try:
        os.unlink(output_last_message_path)
    except OSError:
        pass

    if parse_warnings:
        responses.append(f"[non_json_line_count] → {len(parse_warnings)}")
    if return_code != 0:
        responses.append(f"[return_code] → {return_code}")
    if errs:
        errs_trimmed = errs.strip()
        if len(errs_trimmed) > 4000:
            errs_trimmed = errs_trimmed[:4000] + "…(truncated)"
        responses.append(f"[stderr] → {errs_trimmed}")

    # 对上层来说，非零返回码或空最终消息都应该视为未拿到有效结果，以便触发重试。
    if return_code != 0 or not str(agent_message or "").strip():
        return responses, "", (thread_id or thread_id_hint)

    return responses, agent_message, (thread_id or thread_id_hint)


# 初始化一个 codex 对话 session
def init_codex(
        prompt,
        folder_path=None,
        model_name="gpt-5.1-codex-mini",
        reasoning_effort="low",
        timeout=300,
        output_schema_path=None,
):
    """
    初始化一个 codex 对话 session。

    参数:
        prompt (str): 输入的提示词或指令
        folder_path (str | None): codex 的工作目录；为 None 时不传 --cd，使用当前进程工作目录
        model_name (str): 使用的模型名称，默认为"gpt-5.1-codex-mini"
        reasoning_effort (str): 推理努力程度，可选值有"low"等，默认为"low"
        timeout (int): 命令执行超时时间（秒），默认为300秒
    返回值:
        tuple: 包含三个元素的元组 (信息列表, 智能体的回答, session_ID)
    """
    # 构造codex执行命令的参数列表
    output_last_message_file = tempfile.NamedTemporaryFile(delete=False, suffix=".txt")
    output_last_message_path = output_last_message_file.name
    output_last_message_file.close()
    init_cmd = _build_exec_cmd(
        model_name=model_name,
        reasoning_effort=reasoning_effort,
        output_last_message_path=output_last_message_path,
        folder_path=folder_path,
        output_schema_path=output_schema_path,
    )
    init_cmd.append(prompt)
    # 运行命令并解析结果
    events, errs, return_code, parse_warnings = run_codex(init_cmd, timeout=timeout)
    return _finalize_exec_result(
        events=events,
        errs=errs,
        return_code=return_code,
        parse_warnings=parse_warnings,
        output_last_message_path=output_last_message_path,
    )


# 恢复一个已经存在的 codex 对话 session
def resume_codex(
        thread_id,
        folder_path,
        prompt,
        model_name="gpt-5.1-codex-mini",
        reasoning_effort="low",
        timeout=300,
        output_schema_path=None,
):
    """
    恢复Codex会话并执行指定的提示

    参数:
        thread_id (str): 会话线程ID，用于标识要恢复的会话
        folder_path (str): 工作目录路径，命令将在该目录下执行
        prompt (str): 要执行的提示内容
        model_name (str, optional): 使用的模型名称，默认为"gpt-5.1-codex-mini"
        reasoning_effort (str, optional): 推理努力程度，可选值通常为"low"/"medium"/"high"，默认为"low"
        timeout (int, optional): 命令执行超时时间(秒)，默认为300秒
    返回:
        tuple: 包含处理结果的元组，通常为(信息列表, 智能体的回答, session_ID)
    """
    if thread_id is None:
        return (["[error] → thread_id 为空，无法 resume；请先确保 init_codex 成功返回 thread_id。"],
                "thread_id 为空，无法恢复会话。",
                None)
    # 构造codex执行命令的参数列表
    output_last_message_file = tempfile.NamedTemporaryFile(delete=False, suffix=".txt")
    output_last_message_path = output_last_message_file.name
    output_last_message_file.close()
    init_cmd = _build_exec_cmd(
        model_name=model_name,
        reasoning_effort=reasoning_effort,
        output_last_message_path=output_last_message_path,
        folder_path=folder_path,
        output_schema_path=output_schema_path,
    )
    init_cmd.extend(["resume", thread_id, prompt])
    # 运行命令并解析结果
    events, errs, return_code, parse_warnings = run_codex(init_cmd, timeout=timeout)
    return _finalize_exec_result(
        events=events,
        errs=errs,
        return_code=return_code,
        parse_warnings=parse_warnings,
        output_last_message_path=output_last_message_path,
        thread_id_hint=thread_id,
    )


if __name__ == "__main__":
    cd_path = os.path.dirname(os.path.abspath(__file__))
    init_prompt = """记住: 使用中文进行对话和文档编写。后续我会做一个简单的恢复测试。"""
    _, msg, session_id = init_codex(init_prompt, cd_path)
    print(msg)
    resume_prompt = """请记住一个测试事实: AutoCodex 是一个多智能体编排器。"""
    _, msg, _ = resume_codex(session_id, cd_path, resume_prompt,
                             "gpt-5.1-codex-mini", "low", 300)
    resume_prompt = """刚刚让你记住的测试事实是什么? 请用一句话回答。"""
    print(msg)
    _, msg, _ = resume_codex(session_id, cd_path, resume_prompt,
                             "gpt-5.1-codex-mini", "low", 300)
    print(msg)
