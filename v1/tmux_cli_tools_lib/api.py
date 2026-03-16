from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

from .common import (
    DEFAULT_PROMPT,
    DEFAULT_RUNTIME_DIR,
    DEFAULT_SESSION_NAME,
    AgentCliConfig,
    ClaudeCliConfig,
    GeminiCliConfig,
    ResumeResult,
    RunResult,
    CodexCliConfig,
)
from .runtime import TmuxAgentRuntime


def run_tmux_agent_once(
        work_dir: Path | str,
        prompt: str = DEFAULT_PROMPT,
        session_name: str = DEFAULT_SESSION_NAME,
        runtime_dir: Path | str = DEFAULT_RUNTIME_DIR,
        cleanup: bool = False,
        cli_config: AgentCliConfig | None = None,
        prelaunch_hooks: Sequence[str | Mapping[str, object]] | None = None,
) -> RunResult:
    """
    这是推荐的通用函数调用入口。

    调用方可以显式传入不同后端的 `cli_config`，从而复用同一套 tmux 调用链。
    """
    runtime = TmuxAgentRuntime(
        session_name=session_name,
        work_dir=Path(work_dir),
        runtime_dir=Path(runtime_dir),
        cli_config=cli_config,
        prelaunch_hooks=prelaunch_hooks,
    )
    try:
        return runtime.run_once(prompt)
    finally:
        if cleanup:
            runtime.kill_session()


def run_tmux_codex_once(
        work_dir: Path | str,
        prompt: str = DEFAULT_PROMPT,
        session_name: str = DEFAULT_SESSION_NAME,
        runtime_dir: Path | str = DEFAULT_RUNTIME_DIR,
        cleanup: bool = False,
        prelaunch_hooks: Sequence[str | Mapping[str, object]] | None = None,
) -> RunResult:
    """
    这是推荐的函数调用入口。

    调用方可以直接 import 这个函数，拿到结构化的 `RunResult`；
    如果需要在调用结束后自动清理 tmux session，把 `cleanup=True` 即可。
    """
    return run_tmux_agent_once(
        work_dir=work_dir,
        prompt=prompt,
        session_name=session_name,
        runtime_dir=runtime_dir,
        cleanup=cleanup,
        cli_config=CodexCliConfig(),
        prelaunch_hooks=prelaunch_hooks,
    )


def create_tmux_session(
        work_dir: Path | str,
        session_name: str = DEFAULT_SESSION_NAME,
        runtime_dir: Path | str = DEFAULT_RUNTIME_DIR,
        cli_config: AgentCliConfig | None = None,
        prelaunch_hooks: Sequence[str | Mapping[str, object]] | None = None,
) -> TmuxAgentRuntime:
    """
    创建并准备一个可用的 tmux shell，会返回可继续复用的 runtime 对象。
    """
    runtime = TmuxAgentRuntime(
        session_name=session_name,
        work_dir=Path(work_dir),
        runtime_dir=Path(runtime_dir),
        cli_config=cli_config,
        prelaunch_hooks=prelaunch_hooks,
    )
    runtime.prepare_shell(recreate=True)
    return runtime


def create_agent_cli(
        work_dir: Path | str,
        session_name: str = DEFAULT_SESSION_NAME,
        runtime_dir: Path | str = DEFAULT_RUNTIME_DIR,
        cli_config: AgentCliConfig | None = None,
        prelaunch_hooks: Sequence[str | Mapping[str, object]] | None = None,
) -> TmuxAgentRuntime:
    """
    创建一个已经启动好目标 CLI 的 runtime，方便后续反复调用 `ask()`。
    """
    runtime = TmuxAgentRuntime(
        session_name=session_name,
        work_dir=Path(work_dir),
        runtime_dir=Path(runtime_dir),
        cli_config=cli_config,
        prelaunch_hooks=prelaunch_hooks,
    )
    runtime.ensure_agent_ready(recreate_session=True)
    return runtime


def create_codex_cli(
        work_dir: Path | str,
        session_name: str = DEFAULT_SESSION_NAME,
        runtime_dir: Path | str = DEFAULT_RUNTIME_DIR,
        prelaunch_hooks: Sequence[str | Mapping[str, object]] | None = None,
) -> TmuxAgentRuntime:
    """
    创建一个已经启动好 Codex 的 runtime，方便后续反复调用 `ask()`。
    """
    return create_agent_cli(
        work_dir=work_dir,
        session_name=session_name,
        runtime_dir=runtime_dir,
        cli_config=CodexCliConfig(),
        prelaunch_hooks=prelaunch_hooks,
    )


def resume_cli_session(
        work_dir: Path | str,
        session_name: str = DEFAULT_SESSION_NAME,
        runtime_dir: Path | str = DEFAULT_RUNTIME_DIR,
        cli_config: AgentCliConfig | None = None,
        prompt: str | None = None,
        prelaunch_hooks: Sequence[str | Mapping[str, object]] | None = None,
        attach_if_running: bool = True,
        attach_after_resume: bool = False,
        timeout_sec: float = 60.0,
) -> ResumeResult:
    """
    恢复一个已经存在的 CLI 会话。

    调用方可以显式传入不同后端的 `cli_config`，从而复用同一套恢复逻辑。
    """
    runtime = TmuxAgentRuntime(
        session_name=session_name,
        work_dir=Path(work_dir),
        runtime_dir=Path(runtime_dir),
        cli_config=cli_config,
        prelaunch_hooks=prelaunch_hooks,
    )
    return runtime.resume_cli_session(
        prompt=prompt,
        attach_if_running=attach_if_running,
        attach_after_resume=attach_after_resume,
        timeout_sec=timeout_sec,
    )


def resume_codex_cli(
        work_dir: Path | str,
        session_name: str = DEFAULT_SESSION_NAME,
        runtime_dir: Path | str = DEFAULT_RUNTIME_DIR,
        prompt: str | None = None,
        prelaunch_hooks: Sequence[str | Mapping[str, object]] | None = None,
        attach_if_running: bool = True,
        attach_after_resume: bool = False,
        timeout_sec: float = 60.0,
) -> ResumeResult:
    """这是保留给旧调用方的兼容入口，内部会转到通用恢复函数。"""
    return resume_cli_session(
        work_dir=work_dir,
        session_name=session_name,
        runtime_dir=runtime_dir,
        cli_config=CodexCliConfig(),
        prompt=prompt,
        prelaunch_hooks=prelaunch_hooks,
        attach_if_running=attach_if_running,
        attach_after_resume=attach_after_resume,
        timeout_sec=timeout_sec,
    )


def resume_claude_cli(
        work_dir: Path | str,
        session_name: str = DEFAULT_SESSION_NAME,
        runtime_dir: Path | str = DEFAULT_RUNTIME_DIR,
        prompt: str | None = None,
        cli_config: ClaudeCliConfig | None = None,
        prelaunch_hooks: Sequence[str | Mapping[str, object]] | None = None,
        attach_if_running: bool = True,
        attach_after_resume: bool = False,
        timeout_sec: float = 60.0,
) -> ResumeResult:
    """恢复一个已经存在的 Claude Code 会话。"""
    return resume_cli_session(
        work_dir=work_dir,
        session_name=session_name,
        runtime_dir=runtime_dir,
        cli_config=cli_config or ClaudeCliConfig(),
        prompt=prompt,
        prelaunch_hooks=prelaunch_hooks,
        attach_if_running=attach_if_running,
        attach_after_resume=attach_after_resume,
        timeout_sec=timeout_sec,
    )


def resume_gemini_cli(
        work_dir: Path | str,
        session_name: str = DEFAULT_SESSION_NAME,
        runtime_dir: Path | str = DEFAULT_RUNTIME_DIR,
        prompt: str | None = None,
        cli_config: GeminiCliConfig | None = None,
        prelaunch_hooks: Sequence[str | Mapping[str, object]] | None = None,
        attach_if_running: bool = True,
        attach_after_resume: bool = False,
        timeout_sec: float = 60.0,
) -> ResumeResult:
    """恢复一个已经存在的 Gemini CLI 会话。"""
    return resume_cli_session(
        work_dir=work_dir,
        session_name=session_name,
        runtime_dir=runtime_dir,
        cli_config=cli_config or GeminiCliConfig(),
        prompt=prompt,
        prelaunch_hooks=prelaunch_hooks,
        attach_if_running=attach_if_running,
        attach_after_resume=attach_after_resume,
        timeout_sec=timeout_sec,
    )


def create_claude_cli(
        work_dir: Path | str,
        session_name: str = DEFAULT_SESSION_NAME,
        runtime_dir: Path | str = DEFAULT_RUNTIME_DIR,
        cli_config: ClaudeCliConfig | None = None,
        prelaunch_hooks: Sequence[str | Mapping[str, object]] | None = None,
) -> TmuxAgentRuntime:
    """
    创建一个已经启动好 Claude Code 的 runtime。
    """
    return create_agent_cli(
        work_dir=work_dir,
        session_name=session_name,
        runtime_dir=runtime_dir,
        cli_config=cli_config or ClaudeCliConfig(),
        prelaunch_hooks=prelaunch_hooks,
    )


def create_gemini_cli(
        work_dir: Path | str,
        session_name: str = DEFAULT_SESSION_NAME,
        runtime_dir: Path | str = DEFAULT_RUNTIME_DIR,
        cli_config: GeminiCliConfig | None = None,
        prelaunch_hooks: Sequence[str | Mapping[str, object]] | None = None,
) -> TmuxAgentRuntime:
    """
    创建一个已经启动好 Gemini CLI 的 runtime。
    """
    return create_agent_cli(
        work_dir=work_dir,
        session_name=session_name,
        runtime_dir=runtime_dir,
        cli_config=cli_config or GeminiCliConfig(),
        prelaunch_hooks=prelaunch_hooks,
    )


def send_message_to_codex(
        runtime: TmuxAgentRuntime,
        prompt: str,
        timeout_sec: float = 120.0,
) -> str:
    """向已有的 Codex runtime 发送消息，并返回最终答复。"""
    return runtime.ask(prompt=prompt, timeout_sec=timeout_sec)


def send_message_to_agent(
        runtime: TmuxAgentRuntime,
        prompt: str,
        timeout_sec: float = 120.0,
) -> str:
    """向已有的 agent runtime 发送消息，并返回最终答复。"""
    return runtime.ask(prompt=prompt, timeout_sec=timeout_sec)


def get_codex_info(
        runtime: TmuxAgentRuntime,
        tail_lines: int = 200,
) -> dict[str, object]:
    """
    读取当前 Codex 会话的元信息、状态和最近可见输出，适合外部监控面板直接调用。
    """
    snapshot = runtime.get_snapshot(tail_lines=tail_lines)
    info: dict[str, object] = runtime.get_runtime_metadata()
    info.update(
        {
            "detected_status": snapshot.detected_status.value,
            "confirmed_status": snapshot.confirmed_status.value,
            "current_command": snapshot.current_command,
            "current_path": snapshot.current_path,
            "visible_output": runtime.capture_visible(tail_lines=tail_lines),
            "last_reply": runtime.last_reply,
            "last_prompt": runtime.last_prompt,
        }
    )
    return info


def get_agent_info(
        runtime: TmuxAgentRuntime,
        tail_lines: int = 200,
) -> dict[str, object]:
    """读取当前 agent 会话的元信息、状态和最近可见输出。"""
    return get_codex_info(runtime=runtime, tail_lines=tail_lines)


def cleanup_tmux_session(runtime: TmuxAgentRuntime) -> None:
    """释放 runtime 持有的 tmux session，给上层一个明确的清理入口。"""
    runtime.kill_session()


def start_tmux_cli_session(
        work_dir: Path,
        prompt: str = DEFAULT_PROMPT,
        session_name: str = DEFAULT_SESSION_NAME,
        runtime_dir: Path = DEFAULT_RUNTIME_DIR,
        cleanup: bool = False,
        prelaunch_hooks: Sequence[str | Mapping[str, object]] | None = None,
) -> dict[str, str]:
    """
    这是保留给旧调用方的兼容入口。

    它内部仍然走新的函数调用链，只是把结果压平成字典，方便历史代码平滑迁移。
    """
    result = run_tmux_codex_once(
        work_dir=work_dir,
        prompt=prompt,
        session_name=session_name,
        runtime_dir=runtime_dir,
        cleanup=cleanup,
        prelaunch_hooks=prelaunch_hooks,
    )
    return {
        "backend": result.backend,
        "terminal_id": result.terminal_id,
        "session_name": result.session_name,
        "pane_id": result.pane_id,
        "agent_session_id": result.agent_session_id,
        "reply": result.reply,
        "log_path": str(result.log_path),
        "raw_log_path": str(result.raw_log_path),
        "state_path": str(result.state_path),
    }

