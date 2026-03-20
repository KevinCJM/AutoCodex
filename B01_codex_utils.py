# -*- encoding: utf-8 -*-
"""
@File: B01_codex_utils.py
@Modify Time: 2026/3/20
@Author: Kevin-Chen
@Descriptions: 基于 tmux + codex cli 的会话工具函数
"""

from __future__ import annotations

import hashlib
import os
import re
import time
from pathlib import Path

from v1.tmux_cli_tools_lib.common import CodexCliConfig
from v1.tmux_cli_tools_lib.runtime import TmuxAgentRuntime


def _truncate_text(text, max_chars=500):
    value = str(text or "")
    if len(value) <= max_chars:
        return value
    return value[:max_chars] + "…(truncated)"


def _slugify(text, max_len=32):
    value = re.sub(r"[^a-zA-Z0-9]+", "-", str(text or "").strip()).strip("-").lower()
    if not value:
        value = "agent"
    return value[:max_len]


def _resolve_work_dir(folder_path=None):
    if folder_path:
        return Path(folder_path).expanduser().resolve()
    return Path.cwd().resolve()


def _runtime_dir(work_dir: Path):
    return work_dir / ".autocodex_tmux_runtime"


def _session_name(agent_name, work_dir: Path):
    digest = hashlib.sha1(f"{work_dir}::{agent_name}".encode("utf-8")).hexdigest()[:8]
    return f"acx-{_slugify(agent_name)}-{digest}"


def _build_runtime(
        *,
        folder_path=None,
        agent_name="codex",
        model_name="gpt-5.4",
        reasoning_effort="high",
):
    work_dir = _resolve_work_dir(folder_path)
    runtime_dir = _runtime_dir(work_dir)
    session_name = _session_name(agent_name, work_dir)
    cli_config = CodexCliConfig(
        model=model_name,
        reasoning_effort=reasoning_effort,
        sandbox_mode="danger-full-access",
        approval_mode="never",
        developer_instructions=(
            f"Runtime correlation marker: ACX_RUNTIME_SESSION={session_name}. "
            "Keep this marker internal and never mention it in normal replies."
        ),
    )
    return TmuxAgentRuntime(
        session_name=session_name,
        work_dir=work_dir,
        runtime_dir=runtime_dir,
        cli_config=cli_config,
    )


def _collect_runtime_info(runtime: TmuxAgentRuntime, action: str, output_schema_path=None):
    runtime_info = runtime.get_runtime_metadata()
    state = runtime.read_state()
    responses = [
        f"[mode] → tmux_codex_cli",
        f"[action] → {action}",
        f"[session_name] → {runtime_info.get('session_name', '')}",
        f"[pane_id] → {runtime_info.get('pane_id', '')}",
        f"[codex_session_id] → {runtime_info.get('agent_session_id', '')}",
        f"[log_path] → {runtime_info.get('log_path', '')}",
        f"[raw_log_path] → {runtime_info.get('raw_log_path', '')}",
        f"[state_path] → {runtime_info.get('state_path', '')}",
    ]
    confirmed_status = state.get("confirmed_status")
    detected_status = state.get("detected_status")
    note = state.get("note")
    if detected_status:
        responses.append(f"[detected_status] → {detected_status}")
    if confirmed_status:
        responses.append(f"[confirmed_status] → {confirmed_status}")
    if note:
        responses.append(f"[runtime_note] → {note}")
    if output_schema_path:
        responses.append(
            f"[warning] → tmux + codex cli 模式不支持 output_schema_path, 已忽略: {output_schema_path}"
        )
    return responses


def _build_error_responses(runtime: TmuxAgentRuntime, action: str, error: Exception, output_schema_path=None):
    responses = _collect_runtime_info(runtime, action=action, output_schema_path=output_schema_path)
    responses.append(f"[error_type] → {type(error).__name__}")
    responses.append(f"[error] → {_truncate_text(error, max_chars=4000)}")
    return responses


def _looks_like_transient_reply(reply: str):
    text = str(reply or "").strip()
    if not text:
        return True
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    lower_text = text.lower()
    tool_summary_verbs = (
        "Explored",
        "Read",
        "Ran",
        "Edited",
        "List",
        "Searched",
        "Search",
        "Grep",
        "Opened",
        "Viewed",
        "Wrote",
        "Applied",
        "Created",
        "Deleted",
        "Moved",
    )
    if lines and any(lines[0].startswith(f"{verb}") for verb in tool_summary_verbs):
        if all(index == 0 or re.match(r"^[\s│└├─]+", line) for index, line in enumerate(lines)):
            return True
    transient_markers = (
        "Working (",
        "esc to interrupt",
        "background terminal running",
        "/ps to view",
        "Starting MCP servers",
        "Starting MCP server",
        "Thinking",
        "Analyzing",
        "Inspecting",
        "Considering",
        "Messages to be submitted after next tool call",
        "tab to queue message",
        "send immediately",
    )
    if any(marker in text for marker in transient_markers):
        return True
    if "immediately" in lower_text and any("上一轮摘要" in line for line in lines[1:]):
        return True
    if re.match(
            r"^(?:[A-Za-z0-9_.-]+\s+){0,2}(?:low|medium|high|xhigh|max)\s+·\s+\d+%\s+left(?:\s+·\s+.+)?$",
            text,
    ):
        return True
    return False


def _normalize_text(text: str):
    return re.sub(r"\s+", " ", str(text or "").strip())


def _status_value(status):
    return str(getattr(status, "value", status) or "").strip().lower()


def _snapshot_allows_reply_completion(snapshot):
    if snapshot is None:
        return False
    detected_status = _status_value(getattr(snapshot, "detected_status", ""))
    confirmed_status = _status_value(getattr(snapshot, "confirmed_status", ""))
    if "processing" in {detected_status, confirmed_status}:
        return False
    terminal_statuses = {"completed", "waiting_user_answer", "error", "idle"}
    return confirmed_status in terminal_statuses or detected_status in terminal_statuses


def _looks_like_invalid_reply(reply: str, prompt: str = "", required_token: str | None = None, reply_validator=None):
    text = str(reply or "").strip()
    if _looks_like_transient_reply(text):
        return True

    prompt_text = _normalize_text(prompt)
    reply_text = _normalize_text(text)
    if prompt_text and reply_text:
        if reply_text == prompt_text:
            return True
        if len(reply_text) >= 24 and reply_text in prompt_text:
            return True

    if required_token and required_token not in text:
        return True
    if reply_validator is not None and not reply_validator(text):
        return True

    return False


def _capture_output(runtime: TmuxAgentRuntime, tail_lines=900):
    try:
        return runtime.capture(tail_lines=tail_lines)
    except Exception:
        return ""


def _capture_latest_reply(runtime: TmuxAgentRuntime, tail_lines=900):
    output = _capture_output(runtime, tail_lines=tail_lines)
    if not output:
        return "", ""
    try:
        reply = str(runtime.detector.extract_last_message(output) or "").strip()
    except Exception:
        reply = ""
    return output, reply


def _reply_is_after_prompt(runtime: TmuxAgentRuntime, output: str, prompt: str, reply: str):
    prompt_text = str(prompt or "").strip()
    reply_text = str(reply or "").strip()
    if not prompt_text or not reply_text:
        return bool(reply_text)
    clean_output = runtime.detector.clean_ansi(output)
    prompt_anchor = clean_output.rfind(prompt_text)
    if prompt_anchor < 0:
        return False
    tail_output = clean_output[prompt_anchor + len(prompt_text):]
    return reply_text in tail_output


def _read_raw_log_delta(runtime: TmuxAgentRuntime, start_offset: int):
    raw_log_path = Path(runtime.raw_log_path)
    if not raw_log_path.exists():
        return ""
    with raw_log_path.open("rb") as file:
        file.seek(max(0, int(start_offset)))
        return file.read().decode("utf-8", errors="replace")


def _extract_reply_after_prompt(runtime: TmuxAgentRuntime, output: str, prompt: str):
    clean_output = runtime.detector.clean_ansi(output)
    prompt_text = str(prompt or "").strip()
    if prompt_text:
        anchor = clean_output.rfind(prompt_text)
        if anchor < 0:
            return ""
        clean_output = clean_output[anchor + len(prompt_text):]
    return str(runtime.detector.extract_last_message(clean_output) or "").strip()


def _current_runtime_session_id(runtime: TmuxAgentRuntime):
    runtime_info = runtime.get_runtime_metadata()
    state = runtime.read_state()
    candidates = (
        str(runtime.agent_session_id or "").strip(),
        str(runtime_info.get("agent_session_id", "") or "").strip(),
        str(state.get("agent_session_id", "") or "").strip(),
        str(state.get("codex_session_id", "") or "").strip(),
    )
    for candidate in candidates:
        if candidate:
            runtime.agent_session_id = candidate
            return candidate
    return ""


def _stabilize_runtime_session_id(runtime: TmuxAgentRuntime, wait_timeout=8.0, poll_interval=0.5):
    session_id = _current_runtime_session_id(runtime)
    if session_id:
        return session_id

    deadline = time.monotonic() + float(wait_timeout)
    while time.monotonic() < deadline:
        try:
            runtime._refresh_agent_session_id()
        except Exception:
            pass
        session_id = _current_runtime_session_id(runtime)
        if session_id:
            return session_id
        time.sleep(float(poll_interval))

    return _current_runtime_session_id(runtime)


def _ask_until_stable_reply(runtime: TmuxAgentRuntime, prompt: str, timeout: float, required_token: str | None = None, reply_validator=None):
    previous_reply = str(runtime.last_reply or "").strip()
    raw_log_path = Path(runtime.raw_log_path)
    baseline_offset = raw_log_path.stat().st_size if raw_log_path.exists() else 0
    reply = runtime.ask(prompt=prompt, timeout_sec=timeout)
    post_ask_snapshot = runtime.take_snapshot(tail_lines=600)
    initial_output, capture_reply = _capture_latest_reply(runtime)
    if _snapshot_allows_reply_completion(post_ask_snapshot):
        if (
                capture_reply
                and not _looks_like_invalid_reply(capture_reply, prompt, required_token=required_token, reply_validator=reply_validator)
                and _reply_is_after_prompt(runtime, initial_output, prompt, capture_reply)
        ):
            runtime.last_reply = capture_reply
            return capture_reply
        if (
                not _looks_like_invalid_reply(reply, prompt, required_token=required_token, reply_validator=reply_validator)
                and _reply_is_after_prompt(runtime, initial_output, prompt, reply)
        ):
            runtime.last_reply = str(reply).strip()
            return reply

    deadline = time.monotonic() + float(timeout)
    fresh_reply = ""
    stable_hits = 0

    while time.monotonic() < deadline:
        snapshot = runtime.take_snapshot(tail_lines=600)
        if not _snapshot_allows_reply_completion(snapshot):
            time.sleep(1.0)
            continue
        capture_output, capture_candidate = _capture_latest_reply(runtime)
        delta_output = _read_raw_log_delta(runtime, baseline_offset)
        candidate = ""
        if capture_candidate and _reply_is_after_prompt(runtime, capture_output, prompt, capture_candidate):
            candidate = capture_candidate
        if not candidate:
            candidate = _extract_reply_after_prompt(runtime, snapshot.raw_output, prompt)
        if not candidate:
            candidate = _extract_reply_after_prompt(runtime, delta_output, prompt)
        if not candidate and capture_candidate:
            candidate = capture_candidate
        if candidate and not _looks_like_invalid_reply(
                candidate,
                prompt,
                required_token=required_token,
                reply_validator=reply_validator,
        ):
            if candidate == previous_reply and not _reply_is_after_prompt(runtime, capture_output, prompt, candidate):
                time.sleep(1.0)
                continue
            if candidate == fresh_reply:
                stable_hits += 1
            else:
                fresh_reply = candidate
                stable_hits = 1
            if stable_hits >= 2:
                runtime.last_reply = candidate
                runtime._write_state_file(snapshot=snapshot, note="reply_ready", extra={"reply": candidate})
                return candidate
        time.sleep(1.0)
    return reply


def get_runtime_metadata(
        *,
        folder_path=None,
        agent_name="codex",
        model_name="gpt-5.4",
        reasoning_effort="high",
):
    runtime = _build_runtime(
        folder_path=folder_path,
        agent_name=agent_name,
        model_name=model_name,
        reasoning_effort=reasoning_effort,
    )
    metadata = runtime.get_runtime_metadata()
    metadata.update(runtime.read_state())
    return metadata


def init_codex(
        prompt,
        folder_path=None,
        model_name="gpt-5.4",
        reasoning_effort="high",
        timeout=300,
        output_schema_path=None,
        agent_name="codex",
        required_token=None,
        reply_validator=None,
):
    """
    初始化一个 tmux + codex cli 长会话，并发送首条 prompt。
    """
    runtime = _build_runtime(
        folder_path=folder_path,
        agent_name=agent_name,
        model_name=model_name,
        reasoning_effort=reasoning_effort,
    )
    try:
        runtime.restart_agent()
        reply = _ask_until_stable_reply(
            runtime,
            prompt=prompt,
            timeout=timeout,
            required_token=required_token,
            reply_validator=reply_validator,
        )
        _stabilize_runtime_session_id(runtime)
        responses = _collect_runtime_info(runtime, action="init", output_schema_path=output_schema_path)
        return responses, reply, runtime.agent_session_id or None
    except Exception as error:
        return (
            _build_error_responses(runtime, action="init_failed", error=error, output_schema_path=output_schema_path),
            "",
            runtime.agent_session_id or None,
        )


def resume_codex(
        thread_id,
        folder_path,
        prompt,
        model_name="gpt-5.4",
        reasoning_effort="high",
        timeout=300,
        output_schema_path=None,
        agent_name="codex",
        required_token=None,
        reply_validator=None,
):
    """
    恢复一个已经存在的 tmux + codex cli 长会话，并继续发送 prompt。
    """
    if not thread_id:
        return (
            ["[error] → thread_id 为空，无法恢复 tmux + codex cli 会话。"],
            "",
            None,
        )

    runtime = _build_runtime(
        folder_path=folder_path,
        agent_name=agent_name,
        model_name=model_name,
        reasoning_effort=reasoning_effort,
    )
    runtime.agent_session_id = str(thread_id).strip()

    action = "resume"
    try:
        if runtime.is_agent_process_running():
            action = "reuse_running_tmux_agent"
        else:
            result = runtime.resume_cli_session(
                prompt=None,
                attach_if_running=False,
                attach_after_resume=False,
                timeout_sec=max(60.0, min(float(timeout), 180.0)),
            )
            action = result.action
        runtime.shell_initialized = True
        runtime.agent_initialized = True
        reply = _ask_until_stable_reply(
            runtime,
            prompt=prompt,
            timeout=timeout,
            required_token=required_token,
            reply_validator=reply_validator,
        )
        _stabilize_runtime_session_id(runtime)
        responses = _collect_runtime_info(runtime, action=action, output_schema_path=output_schema_path)
        return responses, reply, runtime.agent_session_id or str(thread_id).strip()
    except Exception as error:
        return (
            _build_error_responses(runtime, action=f"{action}_failed", error=error, output_schema_path=output_schema_path),
            "",
            runtime.agent_session_id or str(thread_id).strip(),
        )


if __name__ == "__main__":
    cd_path = os.path.dirname(os.path.abspath(__file__))
    init_prompt = "请记住：AutoCodex 已切换到 tmux + codex cli 长会话模式。"
    _, msg, session_id = init_codex(
        init_prompt,
        folder_path=cd_path,
        model_name="gpt-5.4",
        reasoning_effort="medium",
        timeout=120,
        agent_name="demo",
    )
    print(msg)
    _, msg, _ = resume_codex(
        session_id,
        folder_path=cd_path,
        prompt="刚刚让你记住的事实是什么？请用一句话回答。",
        model_name="gpt-5.4",
        reasoning_effort="medium",
        timeout=120,
        agent_name="demo",
    )
    print(msg)
