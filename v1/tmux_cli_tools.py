from __future__ import annotations

import uuid
from pathlib import Path

from tmux_cli_tools_lib import (
    DEFAULT_PROMPT,
    DEFAULT_RUNTIME_DIR,
    DEFAULT_SESSION_NAME,
    DEFAULT_WORKDIR,
    AgentCliConfig,
    BaseOutputDetector,
    ClaudeCliConfig,
    ClaudeOutputDetector,
    CliBackend,
    CodexCliConfig,
    CodexOutputDetector,
    GeminiCliConfig,
    GeminiOutputDetector,
    ResumeResult,
    RunResult,
    SessionSnapshot,
    TerminalStatus,
    TmuxAgentRuntime,
    TmuxCodexRuntime,
    build_output_detector,
    cleanup_tmux_session,
    create_agent_cli,
    create_claude_cli,
    create_codex_cli,
    create_gemini_cli,
    create_tmux_session,
    get_agent_info,
    get_codex_info,
    resume_claude_cli,
    resume_cli_session,
    resume_codex_cli,
    resume_gemini_cli,
    run_tmux_agent_once,
    run_tmux_codex_once,
    send_message_to_agent,
    send_message_to_codex,
    start_tmux_cli_session,
)

__all__ = [
    "DEFAULT_PROMPT",
    "DEFAULT_RUNTIME_DIR",
    "DEFAULT_SESSION_NAME",
    "DEFAULT_WORKDIR",
    "AgentCliConfig",
    "BaseOutputDetector",
    "ClaudeCliConfig",
    "ClaudeOutputDetector",
    "CliBackend",
    "CodexCliConfig",
    "CodexOutputDetector",
    "GeminiCliConfig",
    "GeminiOutputDetector",
    "ResumeResult",
    "RunResult",
    "SessionSnapshot",
    "TerminalStatus",
    "TmuxAgentRuntime",
    "TmuxCodexRuntime",
    "build_output_detector",
    "cleanup_tmux_session",
    "create_agent_cli",
    "create_claude_cli",
    "create_codex_cli",
    "create_gemini_cli",
    "create_tmux_session",
    "get_agent_info",
    "get_codex_info",
    "resume_claude_cli",
    "resume_cli_session",
    "resume_codex_cli",
    "resume_gemini_cli",
    "run_tmux_agent_once",
    "run_tmux_codex_once",
    "send_message_to_agent",
    "send_message_to_codex",
    "start_tmux_cli_session",
]


if __name__ == "__main__":
    # 这里用当前脚本所在目录作为工作目录，方便直接运行这个文件做验证。
    demo_work_dir = Path(__file__).resolve().parent
    # 这里为示例生成一个唯一 session 名，避免重复运行时与旧 session 冲突。
    demo_session_name = f"codex-demo-{uuid.uuid4().hex[:8]}"

    # 这里先创建并启动一个已经就绪的 Codex runtime，后续可以在同一个会话里反复对话。
    runtime = create_codex_cli(
        work_dir=demo_work_dir,
        session_name=demo_session_name,
    )

    try:
        # 这里向 Codex 发送一条最简单的测试消息，用来验证调用链是否正常。
        reply = send_message_to_codex(runtime, "你好，请回复收到")
        # 这里读取当前会话的状态和日志路径，方便观察运行结果。
        info = get_codex_info(runtime)

        print(f"session_name: {demo_session_name}")
        print(f"reply: {reply}")
        print(f"confirmed_status: {info['confirmed_status']}")
        print(f"log_path: {info['log_path']}")
        print(f"state_path: {info['state_path']}")
    finally:
        # 这里在示例结束后主动清理 tmux session，避免留下无用后台会话。
        cleanup_tmux_session(runtime)
