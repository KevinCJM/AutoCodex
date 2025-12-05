# -*- encoding: utf-8 -*-
"""
@File: codex_utils.py
@Modify Time: 2025/12/5 11:12       
@Author: Kevin-Chen
@Descriptions: codex exec 工具函数
"""
import time
import json
import shutil
import subprocess
from other_utils import write_json, json_key_exists, CURRENT_DIR


# 运行 Codex 并解析返回的 JSON 事件流
def run_codex(cmd, timeout=300):
    """
    执行命令并解析返回的JSON事件流

    参数:
        cmd: 要执行的命令，可以是字符串或字符串列表
        timeout: 命令执行超时时间（秒），默认为300秒

    返回值:
        tuple: 包含三个元素的元组
            - events: 解析出的JSON事件对象列表
            - errs: 命令执行的错误输出
            - proc.returncode: 命令执行的返回码
    """
    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=timeout,
    )
    raw = proc.stdout
    errs = proc.stderr

    # 解析标准输出中的JSON事件
    events = []
    for line in raw.splitlines():  # 遍历输出中的每一行
        line = line.strip()  # 去除空行
        if not line:
            continue
        try:
            ev = json.loads(line)  # 解析每一行中的JSON对象
            events.append(ev)  # 添加到列表中
        except json.JSONDecodeError:
            # 如果某行不是 JSON —— 输出&跳过
            print("Warning: skipped non-JSON line:", line)
            continue

    return (events,  # 解析的 JSON 事件对象列表
            errs,  # 错误输出
            proc.returncode  # 命令执行的返回码
            )


# 处理事件列表
def handle_events(events):
    """
    遍历事件列表，根据事件类型进行分类处理、打印和信息收集。

    参数:
        events (list): 包含多个事件字典的列表。每个事件是一个具有 'type' 键的字典，
                       可能还包含其他与该事件相关的数据字段。

    返回:
        dict: 包含以下键值对的字典：
            - "responses": list，所有 agent_message 类型的消息文本内容。
            - "reasoning": list，所有 reasoning 类型的推理步骤文本。
            - "commands": list，所有 command_execution 类型的命令执行信息。
            - "file_changes": list，所有 file_change 类型的文件变更信息。
            - "errors": list，所有 error 类型的错误信息。
    """
    responses = []
    agent_message = []
    thread_id = None

    # 遍历所有事件并按类型分别处理
    for ev in events:
        t = ev.get("type")
        if t == "thread.started":
            thread_id = ev.get("thread_id")
            responses.append(f"[thread_id] → {thread_id}")
        elif t == "turn.started":
            # turn 开始 — 你也可以记录 prompt / turn index
            continue
        elif t.startswith("item."):  # item.started / item.completed / item.updated etc.
            item = ev.get("item", {})
            i_type = item.get("type")
            if i_type == "agent_message":
                # agent 的自然语言回答／消息
                text = item.get("text")
                responses.append(f"[agent_message] → {text}")
                agent_message.append(text)
            elif i_type == "reasoning":
                #  推理步骤
                text = item.get("text")
                responses.append(f"[reasoning] → {text}")
            elif i_type == "command_execution":
                # 命令执行
                cmd = item.get("command")
                exit_code = item.get("exit_code")
                output = item.get("aggregated_output", "")
                responses.append(f"[command execution] → {cmd}")
                responses.append(f"[command exit_code] → {exit_code}")
                responses.append(f"[command output] → {output}")
            elif i_type == "file_change":
                # file 修改／patch
                diff = item.get("diff") or item.get("patch") or item
                responses.append(f"[file_change] → {diff}")
            elif i_type == "error":
                err = item.get("error") or item
                responses.append(f"[error] → {err}")
            else:
                # 未列出 / 新类型 —— 统一打印 /记录
                responses.append(f"[other item type = {i_type}] → {item}")
        elif t == "turn.completed":
            usage = ev.get("usage")
            responses.append(f"[TURN completed] → {usage}")
        else:
            # 可能是 other event type（session metadata, tool calls, etc.）
            responses.append(f"[Other event] → {ev}")

    return (responses,  # 信息列表
            agent_message,  # 智能体的回答
            thread_id  # session ID
            )


# 初始化一个 codex 对话 session
def init_codex(prompt, folder_path=None, model_name="gpt-5.1-codex-mini", reasoning_effort="low", timeout=300):
    """
    初始化一个 codex 对话 session。

    参数:
        prompt (str): 输入的提示词或指令
        folder_path (str | None): codex 的工作目录；默认为 None 时使用 CURRENT_DIR/session_name 并自动创建目录
        model_name (str): 使用的模型名称，默认为"gpt-5.1-codex-mini"
        reasoning_effort (str): 推理努力程度，可选值有"low"等，默认为"low"
        timeout (int): 命令执行超时时间（秒），默认为300秒
    返回值:
        tuple: 包含三个元素的元组 (信息列表, 智能体的回答, session_ID)
    """
    # 构造codex执行命令的参数列表
    init_cmd = [
        "codex", "exec",
        "--model", model_name,
        "--config", f"model_reasoning_effort={reasoning_effort}",
        "--skip-git-repo-check",
        "--json", "--full-auto",
        "--cd", folder_path,
        prompt
    ]
    # 运行命令并解析结果
    events, errs, return_code = run_codex(init_cmd, timeout=timeout)
    # 返回处理结果 (信息列表, 智能体的回答, session_ID)
    return handle_events(events)


# 恢复一个已经存在的 codex 对话 session
def resume_codex(thread_id, folder_path, prompt, model_name="gpt-5.1-codex-mini", reasoning_effort="low", timeout=300):
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
    # 构造codex执行命令的参数列表
    init_cmd = [
        "codex", "exec",
        "--model", model_name,
        "--config", f"model_reasoning_effort={reasoning_effort}",
        "--skip-git-repo-check",
        "--json", "--full-auto",
        "--cd", folder_path,
        "resume", thread_id,
        prompt
    ]
    # 运行命令并解析结果
    events, errs, return_code = run_codex(init_cmd, timeout=timeout)
    # 处理结果 (信息列表, 智能体的回答, session_ID)
    return handle_events(events)


if __name__ == "__main__":
    cd_path = CURRENT_DIR
    init_prompt = """记住: 使用中文进行对话和文档编写"""
    _, msg, session_id = init_codex(init_prompt, cd_path)
    print(msg)
    resume_prompt = """记住: 北极熊和小白兔在洞里睡觉, 小熊猫来找他们但是却找不到."""
    _, msg, _ = resume_codex(session_id, cd_path, resume_prompt,
                             "gpt-5.1-codex-mini", "low", 300)
    resume_prompt = """谁来找小白兔? 小白兔在哪? 谁和小白兔在一起? 小白兔在做什么?"""
    print(msg)
    _, msg, _ = resume_codex(session_id, cd_path, resume_prompt,
                             "gpt-5.1-codex-mini", "low", 300)
    print(msg)
