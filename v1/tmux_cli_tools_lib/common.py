from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import threading
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

# 这是默认的 tmux session 名称，便于人工 attach 和排障。
DEFAULT_SESSION_NAME = "agent-minimal-demo"
# 这是默认工作目录，函数调用时也可以显式覆盖。
DEFAULT_WORKDIR = Path("/Users/chenjunming/Desktop/KevinGit/AutoCodex")
# 这是默认发给 CLI agent 的消息，用于快速验证调用链是否通畅。
DEFAULT_PROMPT = "你好, 请回复收到"
# 这是运行时文件的默认目录，用来存放日志和状态快照。
DEFAULT_RUNTIME_DIR = Path(__file__).resolve().parent.parent / ".tmux_cli_tools_runtime"

# 这些命令名表示 pane 当前仍然停留在 shell，而不是 agent TUI。
SHELL_COMMANDS = {"bash", "fish", "sh", "zsh"}
# 这里只拦截少数明显危险的系统目录，避免误把 agent 启动到根目录等位置。
BLOCKED_WORKDIRS = {
    "/",
    "/bin",
    "/sbin",
    "/usr/bin",
    "/usr/sbin",
    "/etc",
    "/var",
    "/tmp",
    "/dev",
    "/proc",
    "/sys",
    "/root",
    "/boot",
    "/lib",
    "/lib64",
    "/private/etc",
    "/private/var",
    "/private/tmp",
}

# 下面这些规则用于清洗终端输出，并辅助识别不同 CLI 的状态和答复。
ANSI_CODE_PATTERN = (
    r"\x1b\[[0-9;?]*[A-Za-z]"
    r"|\x1b\]8;[^\x1b]*\x1b\\"
    r"|\x1b\][^\x07]*\x07"
    r"|\x1b\][^\x1b]*\x1b\\"
    r"|\x1b[()][A-Z0-9]"
    r"|\x1b[\x20-\x2f]*[\x40-\x7e]"
)
IDLE_PROMPT_PATTERN = r"(?:❯|›|codex>)"
IDLE_PROMPT_STRICT_PATTERN = r"^\s*(?:❯|›|codex>)\s*$"
IDLE_PROMPT_TAIL_LINES = 5
ASSISTANT_PREFIX_PATTERN = r"^(?:(?:assistant|codex|agent)\s*:|\s*•)"
USER_PREFIX_PATTERN = r"^(?:You\b|›[^\S\n]*\S)"
WAITING_PROMPT_PATTERN = r"^(?:Approve|Allow)\b.*\b(?:y/n|yes/no|yes|no)\b"
ERROR_PATTERN = r"^(?:Error:|ERROR:|Traceback \(most recent call last\):|panic:)"
TUI_FOOTER_PATTERN = r"(?:\?\s+for shortcuts|context left|\d+%\s+left)"
TUI_PROGRESS_PATTERN = r"•.*\((?:\d+[hms]\s*)+•\s*esc(?:\s+to(?:\s+interrupt)?)?"
CODEX_WELCOME_PATTERN = r"OpenAI Codex"
CLAUDE_WELCOME_PATTERN = r"Claude Code"
GEMINI_WELCOME_PATTERN = r"Gemini CLI"
CODEX_TRUST_PROMPT_PATTERNS = (
    r"allow Codex to work in this folder",
    r"Do you trust the contents of this directory\?",
)
CODEX_MODEL_SELECTION_PROMPT_PATTERNS = (
    r"Introducing GPT-5\.4",
    r"Choose how you'd like Codex to proceed",
    r"Try new model",
    r"Use existing model",
)
CODEX_UPDATE_PROMPT_PATTERNS = (
    r"Update available!",
    r"Update now",
    r"Skip until next version",
    r"Press enter to continue",
)
CODEX_QUEUED_MESSAGE_PATTERNS = (
    r"Messages to be submitted after next tool call",
    r"queued message",
    r"press esc to interrupt(?: and send immediately)?",
    r"tab to queue message",
)
CLAUDE_SPINNER_CHARS = "✱✲✳✴✵✶✷✸✹✺✻✼✽✾✿❀❁❂❃❇❈❉❊❋✢✣✤✥✦✧✨⊛⊕⊙◉◎◍⁂⁕※⍟☼★☆"
CLAUDE_SPINNER_ACTIVITY_PATTERN = re.compile(
    rf"^[{re.escape(CLAUDE_SPINNER_CHARS)}] \S+ing.*…",
    re.MULTILINE,
)
GEMINI_REASONING_MODEL_MAP = {
    "low": "flash",
    "medium": "flash",
    "high": "pro",
    "xhigh": "pro",
    "max": "pro",
}


class TerminalStatus(str, Enum):
    """这是运行时内部统一使用的终端状态枚举。"""

    IDLE = "idle"
    PROCESSING = "processing"
    COMPLETED = "completed"
    WAITING_USER_ANSWER = "waiting_user_answer"
    ERROR = "error"


class CliBackend(str, Enum):
    """这是当前支持的 CLI 后端类型。"""

    CODEX = "codex"
    CLAUDE = "claude"
    GEMINI = "gemini"


@dataclass
class AgentCliConfig:
    """
    这是统一的 CLI 配置模型。

    不同后端支持的参数并不完全对等，所以这里做的是“统一建模 + 后端内部分流”。
    """

    backend: CliBackend = CliBackend.CODEX
    model: str | None = None
    reasoning_effort: str | None = None
    sandbox_mode: str | None = None
    approval_mode: str | None = None
    developer_instructions: str | None = None
    disable_features: tuple[str, ...] = ()
    extra_args: tuple[str, ...] = ()

    def executable_name(self) -> str:
        """返回当前后端对应的 CLI 可执行名。"""
        return {
            CliBackend.CODEX: "codex",
            CliBackend.CLAUDE: "claude",
            CliBackend.GEMINI: "gemini",
        }[self.backend]

    def expected_current_commands(self) -> tuple[str, ...]:
        """
        返回 tmux `pane_current_command` 里可能出现的前台命令名。

        某些 npm 安装的 CLI 运行时会显示成 `node`，所以这里保留一层宽松匹配。
        """
        if self.backend == CliBackend.CODEX:
            return ("codex", "node")
        if self.backend == CliBackend.CLAUDE:
            return ("claude", "node")
        return ("gemini", "node")

    def display_name(self) -> str:
        """返回用于日志和异常消息的后端展示名。"""
        return {
            CliBackend.CODEX: "Codex",
            CliBackend.CLAUDE: "Claude Code",
            CliBackend.GEMINI: "Gemini CLI",
        }[self.backend]

    def submit_enter_count(self) -> int:
        """
        返回发送用户消息时默认补发的 Enter 次数。

        Codex 的输入框对多行粘贴更敏感，通常需要两次 Enter 更稳；Claude 和 Gemini 默认一次即可。
        """
        return 2 if self.backend == CliBackend.CODEX else 1

    def default_model(self) -> str:
        """返回当前后端的默认模型。"""
        if self.backend == CliBackend.CODEX:
            return "gpt-5.4"
        if self.backend == CliBackend.CLAUDE:
            return "sonnet"
        if self.backend == CliBackend.GEMINI:
            return "auto"
        raise ValueError(f"Unsupported backend: {self.backend}")

    def default_reasoning_effort(self) -> str | None:
        """返回当前后端默认的推理档位。"""
        if self.backend == CliBackend.CODEX:
            return "xhigh"
        if self.backend == CliBackend.CLAUDE:
            return "high"
        if self.backend == CliBackend.GEMINI:
            return None
        raise ValueError(f"Unsupported backend: {self.backend}")

    def default_sandbox_mode(self) -> str | None:
        """返回当前后端默认的沙箱配置。"""
        if self.backend == CliBackend.CODEX:
            return "danger-full-access"
        if self.backend == CliBackend.CLAUDE:
            return "host"
        if self.backend == CliBackend.GEMINI:
            return "disabled"
        raise ValueError(f"Unsupported backend: {self.backend}")

    def default_approval_mode(self) -> str:
        """返回当前后端默认的审批模式。"""
        if self.backend == CliBackend.CODEX:
            return "never"
        if self.backend == CliBackend.CLAUDE:
            return "default"
        if self.backend == CliBackend.GEMINI:
            return "default"
        raise ValueError(f"Unsupported backend: {self.backend}")

    def resolved_model(self) -> str:
        """
        计算最终应使用的模型名。

        Gemini CLI 当前没有独立的推理强度参数，所以在未显式指定模型时，会按推理档位映射到更偏速度或更偏推理的模型档位。
        """
        if self.model:
            return self.model
        if self.backend == CliBackend.GEMINI and self.reasoning_effort:
            mapped = GEMINI_REASONING_MODEL_MAP.get(self.reasoning_effort.lower())
            if mapped:
                return mapped
        return self.default_model()

    def resolved_reasoning_effort(self) -> str | None:
        """计算最终推理档位，并把通用别名收敛成后端真正接受的值。"""
        effort = (self.reasoning_effort or self.default_reasoning_effort() or "").lower()
        if not effort:
            return None

        if self.backend == CliBackend.CODEX:
            codex_map = {
                "low": "low",
                "medium": "medium",
                "high": "high",
                "xhigh": "xhigh",
                "max": "xhigh",
            }
            return codex_map.get(effort, effort)

        if self.backend == CliBackend.CLAUDE:
            claude_map = {
                "low": "low",
                "medium": "medium",
                "high": "high",
                "xhigh": "max",
                "max": "max",
            }
            return claude_map.get(effort, effort)

        # Gemini CLI 当前没有独立的推理强度 flag，这里只把结果留给模型映射逻辑消费。
        return effort

    def resolved_sandbox_mode(self) -> str | None:
        """计算最终沙箱模式，并做少量通用别名归一化。"""
        sandbox_mode = (self.sandbox_mode or self.default_sandbox_mode() or "").strip().lower()
        if not sandbox_mode:
            return None

        if self.backend == CliBackend.CODEX:
            codex_map = {
                "full-access": "danger-full-access",
                "danger-full-access": "danger-full-access",
                "workspace-write": "workspace-write",
                "read-only": "read-only",
            }
            return codex_map.get(sandbox_mode, sandbox_mode)

        if self.backend == CliBackend.CLAUDE:
            claude_allowed = {"host", "disabled", "off", "none", "full-access", "danger-full-access"}
            if sandbox_mode not in claude_allowed:
                raise ValueError(
                    "Claude Code CLI 当前没有独立的原生 sandbox flag；"
                    "请把 sandbox_mode 设为 host/full-access，或在外部容器里运行 Claude。"
                )
            return sandbox_mode

        gemini_map = {
            "disabled": "disabled",
            "off": "disabled",
            "none": "disabled",
            "host": "disabled",
            "enabled": "true",
            "sandbox": "true",
            "true": "true",
            "docker": "docker",
            "podman": "podman",
            "sandbox-exec": "sandbox-exec",
            "runsc": "runsc",
            "lxc": "lxc",
        }
        return gemini_map.get(sandbox_mode, sandbox_mode)

    def resolved_approval_mode(self) -> str:
        """计算最终审批模式，并兼容少量通用别名。"""
        approval_mode = (self.approval_mode or self.default_approval_mode()).strip()

        if self.backend == CliBackend.CODEX:
            codex_map = {
                "manual": "on-request",
                "default": "on-request",
                "on-request": "on-request",
                "never": "never",
                "on-failure": "on-failure",
                "untrusted": "untrusted",
            }
            return codex_map.get(approval_mode, approval_mode)

        if self.backend == CliBackend.CLAUDE:
            claude_map = {
                "manual": "default",
                "default": "default",
                "on-request": "default",
                "accept_edits": "acceptEdits",
                "acceptEdits": "acceptEdits",
                "edit": "acceptEdits",
                "plan": "plan",
                "dontAsk": "dontAsk",
                "dont_ask": "dontAsk",
                "auto": "auto",
                "bypass": "bypassPermissions",
                "full-access": "bypassPermissions",
                "never": "bypassPermissions",
                "bypassPermissions": "bypassPermissions",
            }
            return claude_map.get(approval_mode, approval_mode)

        gemini_map = {
            "manual": "default",
            "default": "default",
            "on-request": "default",
            "auto_edit": "auto_edit",
            "acceptEdits": "auto_edit",
            "edit": "auto_edit",
            "plan": "plan",
            "yolo": "yolo",
            "bypass": "yolo",
            "never": "yolo",
            "full-access": "yolo",
        }
        return gemini_map.get(approval_mode, approval_mode)

    def build_command(self) -> str:
        """把结构化配置转成真正可在 shell 里执行的 CLI 命令。"""
        if self.backend == CliBackend.CODEX:
            return self._build_codex_command()
        if self.backend == CliBackend.CLAUDE:
            return self._build_claude_command()
        if self.backend == CliBackend.GEMINI:
            return self._build_gemini_command()
        raise ValueError(f"Unsupported backend: {self.backend}")

    def _build_codex_command(self) -> str:
        """构造 Codex CLI 命令。"""
        args = [
            "codex",
            "--model",
            self.resolved_model(),
            "--config",
            f'model_reasoning_effort="{self.resolved_reasoning_effort()}"',
            "--sandbox",
            self.resolved_sandbox_mode() or "danger-full-access",
            "--ask-for-approval",
            self.resolved_approval_mode(),
            "--no-alt-screen",
        ]
        developer_instructions = str(self.developer_instructions or "").strip()
        if developer_instructions:
            escaped_prompt = (
                developer_instructions.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
            )
            args.extend(["-c", f'developer_instructions="{escaped_prompt}"'])
        for feature in self.disable_features or ("shell_snapshot",):
            args.extend(["--disable", feature])
        args.extend(self.extra_args)
        return " ".join(shlex.quote(arg) for arg in args)

    def build_resume_command(
        self,
        session_id: str,
        work_dir: Path | None = None,
        prompt: str | None = None,
    ) -> str:
        """
        构造恢复已有会话的命令。

        这里只有 Codex 提供明确的 resume 子命令；其他后端如果需要恢复，会由各自上层逻辑处理。
        """
        raise NotImplementedError(f"{self.display_name()} does not support resume command construction")

    def _build_claude_command(self) -> str:
        """构造 Claude Code CLI 命令。"""
        args = [
            "claude",
            "--model",
            self.resolved_model(),
            "--permission-mode",
            self.resolved_approval_mode(),
        ]
        reasoning_effort = self.resolved_reasoning_effort()
        if reasoning_effort:
            args.extend(["--effort", reasoning_effort])
        args.extend(self.extra_args)
        return " ".join(shlex.quote(arg) for arg in args)

    def _build_gemini_command(self) -> str:
        """构造 Gemini CLI 命令。"""
        args = [
            "gemini",
            "--model",
            self.resolved_model(),
            "--approval-mode",
            self.resolved_approval_mode(),
        ]
        sandbox_mode = self.resolved_sandbox_mode()
        env_assignments: list[str] = []
        if sandbox_mode and sandbox_mode != "disabled":
            args.append("--sandbox")
            env_assignments.append(f"GEMINI_SANDBOX={shlex.quote(sandbox_mode)}")
        args.extend(self.extra_args)
        command = " ".join(shlex.quote(arg) for arg in args)
        if env_assignments:
            return f"env {' '.join(env_assignments)} {command}"
        return command


class CodexCliConfig(AgentCliConfig):
    """这是 Codex 的便捷配置封装。"""

    def __init__(
        self,
        model: str = "gpt-5.4",
        reasoning_effort: str = "xhigh",
        sandbox_mode: str = "danger-full-access",
        approval_mode: str = "never",
        developer_instructions: str | None = None,
        disable_features: tuple[str, ...] = ("shell_snapshot",),
        extra_args: tuple[str, ...] = (),
    ):
        super().__init__(
            backend=CliBackend.CODEX,
            model=model,
            reasoning_effort=reasoning_effort,
            sandbox_mode=sandbox_mode,
            approval_mode=approval_mode,
            developer_instructions=developer_instructions,
            disable_features=disable_features,
            extra_args=extra_args,
        )

    def build_resume_command(
        self,
        session_id: str,
        work_dir: Path | None = None,
        prompt: str | None = None,
    ) -> str:
        """构造 Codex 恢复命令，并显式携带工作目录和运行策略。"""
        args = [
            "codex",
            "resume",
            session_id,
            "--model",
            self.resolved_model(),
            "--config",
            f'model_reasoning_effort="{self.resolved_reasoning_effort()}"',
            "--sandbox",
            self.resolved_sandbox_mode() or "danger-full-access",
            "--ask-for-approval",
            self.resolved_approval_mode(),
            "--no-alt-screen",
        ]
        if work_dir is not None:
            args.extend(["--cd", str(work_dir)])
        for feature in self.disable_features or ("shell_snapshot",):
            args.extend(["--disable", feature])
        if prompt:
            args.append(prompt)
        args.extend(self.extra_args)
        return " ".join(shlex.quote(arg) for arg in args)


class ClaudeCliConfig(AgentCliConfig):
    """这是 Claude Code 的便捷配置封装。"""

    def __init__(
        self,
        model: str = "sonnet",
        reasoning_effort: str = "high",
        sandbox_mode: str = "host",
        approval_mode: str = "default",
        extra_args: tuple[str, ...] = (),
    ):
        super().__init__(
            backend=CliBackend.CLAUDE,
            model=model,
            reasoning_effort=reasoning_effort,
            sandbox_mode=sandbox_mode,
            approval_mode=approval_mode,
            extra_args=extra_args,
        )

    def build_resume_command(
        self,
        session_id: str,
        work_dir: Path | None = None,
        prompt: str | None = None,
    ) -> str:
        """构造 Claude Code 的恢复命令。"""
        args = [
            "claude",
            "--resume",
            session_id,
            "--model",
            self.resolved_model(),
            "--permission-mode",
            self.resolved_approval_mode(),
        ]
        reasoning_effort = self.resolved_reasoning_effort()
        if reasoning_effort:
            args.extend(["--effort", reasoning_effort])
        if prompt:
            args.append(prompt)
        args.extend(self.extra_args)
        return " ".join(shlex.quote(arg) for arg in args)


class GeminiCliConfig(AgentCliConfig):
    """这是 Gemini CLI 的便捷配置封装。"""

    def __init__(
        self,
        model: str = "auto",
        reasoning_effort: str | None = None,
        sandbox_mode: str = "disabled",
        approval_mode: str = "default",
        extra_args: tuple[str, ...] = (),
    ):
        super().__init__(
            backend=CliBackend.GEMINI,
            model=model,
            reasoning_effort=reasoning_effort,
            sandbox_mode=sandbox_mode,
            approval_mode=approval_mode,
            extra_args=extra_args,
        )

    def build_resume_command(
        self,
        session_id: str,
        work_dir: Path | None = None,
        prompt: str | None = None,
    ) -> str:
        """构造 Gemini CLI 的恢复命令。"""
        args = [
            "gemini",
            "--resume",
            session_id,
            "--model",
            self.resolved_model(),
            "--approval-mode",
            self.resolved_approval_mode(),
        ]
        sandbox_mode = self.resolved_sandbox_mode()
        env_assignments: list[str] = []
        if sandbox_mode and sandbox_mode != "disabled":
            args.append("--sandbox")
            env_assignments.append(f"GEMINI_SANDBOX={shlex.quote(sandbox_mode)}")
        if prompt:
            args.append(prompt)
        args.extend(self.extra_args)
        command = " ".join(shlex.quote(arg) for arg in args)
        if env_assignments:
            return f"env {' '.join(env_assignments)} {command}"
        return command


@dataclass
class SessionSnapshot:
    """这是一次轮询得到的 pane 快照，既保留检测结果，也保留原始输出。"""

    timestamp: float
    detected_status: TerminalStatus
    confirmed_status: TerminalStatus
    current_command: str
    current_path: str
    pane_dead: bool
    raw_output: str
    clean_output: str


@dataclass
class RunResult:
    """这是一次完整调用结束后返回给上层的结果。"""

    backend: str
    terminal_id: str
    session_name: str
    pane_id: str
    agent_session_id: str
    reply: str
    log_path: Path
    raw_log_path: Path
    state_path: Path


@dataclass
class ResumeResult:
    """这是一次恢复流程结束后返回给上层的结果。"""

    backend: str
    terminal_id: str
    session_name: str
    pane_id: str
    agent_session_id: str
    action: str
    attached: bool
    log_path: Path
    raw_log_path: Path
    state_path: Path


class DebouncedStateMachine:
    """
    这是一个很小的去抖状态机。

    目的不是让状态“更慢”，而是避免 TUI 在重绘、滚动和短暂闪烁时把状态误判成来回跳变。
    """

    def __init__(self, persistence_sec: float = 0.2, minimum_state_sec: float = 0.5):
        self.persistence_sec = persistence_sec
        self.minimum_state_sec = minimum_state_sec
        self.confirmed_state: TerminalStatus | None = None
        self.confirmed_at = 0.0
        self.pending_state: TerminalStatus | None = None
        self.pending_since = 0.0

    def observe(self, detected_state: TerminalStatus, now: float) -> TerminalStatus:
        """把一次检测结果喂给状态机，并返回当前确认后的状态。"""
        if self.confirmed_state is None:
            self.confirmed_state = detected_state
            self.confirmed_at = now
            self.pending_state = None
            self.pending_since = 0.0
            return detected_state

        if detected_state == self.confirmed_state:
            self.pending_state = None
            self.pending_since = 0.0
            return self.confirmed_state

        if self.pending_state != detected_state:
            self.pending_state = detected_state
            self.pending_since = now
            return self.confirmed_state

        pending_duration = now - self.pending_since
        current_duration = now - self.confirmed_at
        if pending_duration >= self.persistence_sec and current_duration >= self.minimum_state_sec:
            self.confirmed_state = detected_state
            self.confirmed_at = now
            self.pending_state = None
            self.pending_since = 0.0

        return self.confirmed_state
