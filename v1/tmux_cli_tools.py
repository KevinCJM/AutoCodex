from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import threading
import time
import uuid
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
DEFAULT_RUNTIME_DIR = Path(__file__).resolve().parent / ".tmux_cli_tools_runtime"

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
TUI_PROGRESS_PATTERN = r"•.*\(\d+s"
CODEX_WELCOME_PATTERN = r"OpenAI Codex"
CLAUDE_WELCOME_PATTERN = r"Claude Code"
GEMINI_WELCOME_PATTERN = r"Gemini CLI"
CODEX_TRUST_PROMPT_PATTERNS = (
    r"allow Codex to work in this folder",
    r"Do you trust the contents of this directory\?",
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


@dataclass
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
        for feature in self.disable_features or ("shell_snapshot",):
            args.extend(["--disable", feature])
        args.extend(self.extra_args)
        return " ".join(shlex.quote(arg) for arg in args)

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
        disable_features: tuple[str, ...] = ("shell_snapshot",),
        extra_args: tuple[str, ...] = (),
    ):
        super().__init__(
            backend=CliBackend.CODEX,
            model=model,
            reasoning_effort=reasoning_effort,
            sandbox_mode=sandbox_mode,
            approval_mode=approval_mode,
            disable_features=disable_features,
            extra_args=extra_args,
        )


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
    reply: str
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


class BaseOutputDetector:
    """这是所有 CLI 输出检测器的共同基类。"""

    @staticmethod
    def clean_ansi(text: str) -> str:
        """清理 ANSI 转义序列，保证后续正则匹配更稳定。"""
        return re.sub(ANSI_CODE_PATTERN, "", text)

    @staticmethod
    def _split_blocks(text: str) -> list[str]:
        """把连续文本按空行切成块，便于抽取最后一个有效答复。"""
        blocks: list[str] = []
        current_lines: list[str] = []

        for line in text.splitlines():
            if not line.strip():
                if current_lines:
                    blocks.append("\n".join(current_lines).strip())
                    current_lines = []
                continue
            current_lines.append(line.rstrip())

        if current_lines:
            blocks.append("\n".join(current_lines).strip())

        return [block for block in blocks if block]

    @staticmethod
    def _is_shell_prompt_line(line: str) -> bool:
        """识别普通 shell 提示符，避免把它误当成模型输出。"""
        stripped = line.strip()
        return bool(re.search(r"[$%#]\s*$", stripped)) and "❯" not in stripped

    def has_trust_prompt(self, clean_output: str) -> bool:
        """默认不识别信任提示，由具体后端覆盖。"""
        return False

    def has_welcome_banner(self, clean_output: str) -> bool:
        """默认不识别欢迎横幅，由具体后端覆盖。"""
        return False

    def looks_like_shell_prompt(self, clean_output: str) -> bool:
        """默认识别底部几行里常见的 shell prompt。"""
        lines = [line for line in clean_output.splitlines() if line.strip()]
        return any(self._is_shell_prompt_line(line) for line in lines[-5:])

    def detect_status(self, output: str) -> TerminalStatus:
        """默认状态识别比较保守，只区分空输出和普通空闲状态。"""
        clean_output = self.clean_ansi(output)
        if not clean_output.strip():
            return TerminalStatus.ERROR
        return TerminalStatus.IDLE

    def extract_last_message(self, output: str) -> str:
        """
        默认从清洗后的输出里提取最后一个有效文本块。

        这是 Claude 和 Gemini 的兜底方案，避免在没有结构化消息标记时完全拿不到答复。
        """
        clean_output = self.clean_ansi(output)
        lines: list[str] = []
        for line in clean_output.splitlines():
            stripped = line.rstrip()
            normalized = stripped.strip()
            if not normalized:
                lines.append("")
                continue

            if self._should_skip_line(normalized):
                continue
            lines.append(stripped)

        blocks = self._split_blocks("\n".join(lines))
        if not blocks:
            raise ValueError("No assistant response found in terminal output")
        return blocks[-1]

    def _should_skip_line(self, line: str) -> bool:
        """过滤明显属于 UI chrome 的行，减少把提示栏误当答复的概率。"""
        skip_patterns = (
            r"^\? for shortcuts",
            r"^\d+%\s+left",
            r"^context left",
            r"^(?:model|directory)\s*:",
            r"^Tip:",
            r"^────────────────+",
            r"^[│┌┐└┘╭╮╰╯╷╵─═]+$",
            r"^Press (?:ESC|Esc|esc)",
        )
        if any(re.search(pattern, line, re.IGNORECASE) for pattern in skip_patterns):
            return True
        if self._is_shell_prompt_line(line):
            return True
        return False


class CodexOutputDetector(BaseOutputDetector):
    """这个检测器只负责理解 Codex 的输出，不负责 tmux 生命周期。"""

    @staticmethod
    def _compute_tui_footer_cutoff(all_lines: list[str]) -> int:
        """
        计算 TUI footer 的起点，避免把底部建议文本误判成用户输入。
        """
        line_count = len(all_lines)
        footer_start_index = line_count

        for index in range(line_count - 1, max(line_count - IDLE_PROMPT_TAIL_LINES - 1, -1), -1):
            if re.search(TUI_FOOTER_PATTERN, all_lines[index]):
                footer_start_index = index
                break

        if footer_start_index == line_count:
            return len("\n".join(all_lines))

        for index in range(footer_start_index - 1, max(footer_start_index - 4, -1), -1):
            line = all_lines[index]
            if not line.strip():
                footer_start_index = index
            elif re.match(rf"\s*{IDLE_PROMPT_PATTERN}", line):
                footer_start_index = index
                break
            else:
                break

        return len("\n".join(all_lines[:footer_start_index]))

    def has_trust_prompt(self, clean_output: str) -> bool:
        """识别工作区信任提示，便于启动时自动确认默认项。"""
        return any(re.search(pattern, clean_output, re.IGNORECASE) for pattern in CODEX_TRUST_PROMPT_PATTERNS)

    def has_welcome_banner(self, clean_output: str) -> bool:
        """识别 Codex 欢迎页，判断 TUI 是否已经正常拉起。"""
        return bool(re.search(CODEX_WELCOME_PATTERN, clean_output))

    def looks_like_shell_prompt(self, clean_output: str) -> bool:
        """识别底部是否更像普通 shell 提示，而不是 Codex 自己的输入框。"""
        return super().looks_like_shell_prompt(clean_output)

    def detect_status(self, output: str) -> TerminalStatus:
        """根据 pane 输出识别 Codex 当前状态。"""
        clean_output = self.clean_ansi(output)
        if not clean_output.strip():
            return TerminalStatus.ERROR

        tail_output = "\n".join(clean_output.splitlines()[-25:])
        all_lines = clean_output.splitlines()
        tui_footer_detected = any(
            re.search(TUI_FOOTER_PATTERN, line) for line in all_lines[-IDLE_PROMPT_TAIL_LINES:]
        )
        cutoff_position = (
            self._compute_tui_footer_cutoff(all_lines) if tui_footer_detected else len(clean_output)
        )

        last_user_match: re.Match[str] | None = None
        for match in re.finditer(USER_PREFIX_PATTERN, clean_output, re.IGNORECASE | re.MULTILINE):
            if match.start() < cutoff_position:
                last_user_match = match

        output_after_last_user = clean_output[last_user_match.start():] if last_user_match else clean_output
        assistant_after_last_user = bool(
            last_user_match
            and re.search(
                ASSISTANT_PREFIX_PATTERN,
                output_after_last_user,
                re.IGNORECASE | re.MULTILINE,
            )
        )

        if self.has_trust_prompt(clean_output):
            return TerminalStatus.WAITING_USER_ANSWER

        bottom_lines = clean_output.strip().splitlines()[-IDLE_PROMPT_TAIL_LINES:]
        has_idle_prompt_at_end = any(
            re.match(rf"\s*{IDLE_PROMPT_PATTERN}", line, re.IGNORECASE) for line in bottom_lines
        )

        if last_user_match is not None:
            if not assistant_after_last_user:
                if re.search(WAITING_PROMPT_PATTERN, output_after_last_user, re.IGNORECASE | re.MULTILINE):
                    return TerminalStatus.WAITING_USER_ANSWER
                if re.search(ERROR_PATTERN, output_after_last_user, re.IGNORECASE | re.MULTILINE):
                    return TerminalStatus.ERROR
        else:
            if re.search(WAITING_PROMPT_PATTERN, tail_output, re.IGNORECASE | re.MULTILINE):
                return TerminalStatus.WAITING_USER_ANSWER
            if re.search(ERROR_PATTERN, tail_output, re.IGNORECASE | re.MULTILINE):
                return TerminalStatus.ERROR

        if has_idle_prompt_at_end:
            if re.search(TUI_PROGRESS_PATTERN, tail_output, re.MULTILINE):
                return TerminalStatus.PROCESSING

            if last_user_match is not None:
                if re.search(
                        ASSISTANT_PREFIX_PATTERN,
                        clean_output[last_user_match.start():],
                        re.IGNORECASE | re.MULTILINE,
                ):
                    return TerminalStatus.COMPLETED
                return TerminalStatus.IDLE

            return TerminalStatus.IDLE

        return TerminalStatus.PROCESSING

    def extract_last_message(self, output: str) -> str:
        """提取最后一轮 assistant 的答复，并过滤掉 spinner/progress 块。"""

        def extract_bullet_blocks(text: str) -> list[str]:
            blocks: list[str] = []
            current_block: list[str] = []

            for line in text.splitlines():
                if re.match(r"^\s*•\s*", line):
                    if current_block:
                        blocks.append("\n".join(current_block).strip())
                    current_block = [re.sub(r"^\s*•\s*", "", line, count=1).rstrip()]
                    continue

                if current_block:
                    if not line.strip():
                        blocks.append("\n".join(current_block).strip())
                        current_block = []
                    else:
                        current_block.append(line.rstrip())

            if current_block:
                blocks.append("\n".join(current_block).strip())

            return [block for block in blocks if block]

        def is_progress_block(block: str) -> bool:
            first_line = block.splitlines()[0] if block.splitlines() else ""
            return bool(re.search(r"\(\d+s", first_line))

        clean_output = self.clean_ansi(output)
        all_lines = clean_output.splitlines()
        tui_footer_detected = any(
            re.search(TUI_FOOTER_PATTERN, line) for line in all_lines[-IDLE_PROMPT_TAIL_LINES:]
        )
        cutoff_position = (
            self._compute_tui_footer_cutoff(all_lines) if tui_footer_detected else len(clean_output)
        )

        user_matches = [
            match
            for match in re.finditer(USER_PREFIX_PATTERN, clean_output, re.IGNORECASE | re.MULTILINE)
            if match.start() < cutoff_position
        ]

        if user_matches:
            last_user_match = user_matches[-1]
            assistant_after_user = re.search(
                ASSISTANT_PREFIX_PATTERN,
                clean_output[last_user_match.start():],
                re.IGNORECASE | re.MULTILINE,
            )
            if assistant_after_user:
                response_start = last_user_match.start() + assistant_after_user.start()
            else:
                user_line_end = clean_output.find("\n", last_user_match.start())
                response_start = len(clean_output) if user_line_end == -1 else user_line_end + 1

            idle_after = re.search(
                IDLE_PROMPT_STRICT_PATTERN,
                clean_output[response_start:],
                re.MULTILINE,
            )
            if idle_after:
                end_position = response_start + idle_after.start()
            elif tui_footer_detected:
                end_position = cutoff_position
            else:
                end_position = len(clean_output)

            response_text = clean_output[response_start:end_position].strip()
            if response_text:
                response_text = re.sub(
                    r"^(?:assistant|codex|agent)\s*:\s*",
                    "",
                    response_text,
                    count=1,
                    flags=re.IGNORECASE,
                )
                bullet_blocks = extract_bullet_blocks(response_text)
                if bullet_blocks:
                    non_progress_blocks = [block for block in bullet_blocks if not is_progress_block(block)]
                    return non_progress_blocks[-1] if non_progress_blocks else bullet_blocks[-1]
                return re.sub(r"^\s*•\s*", "", response_text, count=1).strip()

        assistant_matches = list(
            re.finditer(ASSISTANT_PREFIX_PATTERN, clean_output, re.IGNORECASE | re.MULTILINE)
        )
        if not assistant_matches:
            raise ValueError("No Codex response found - no assistant marker detected")

        last_assistant_match = assistant_matches[-1]
        start_position = last_assistant_match.end()
        idle_after = re.search(IDLE_PROMPT_STRICT_PATTERN, clean_output[start_position:], re.MULTILINE)
        end_position = start_position + idle_after.start() if idle_after else len(clean_output)
        final_answer = clean_output[start_position:end_position].strip()

        bullet_blocks = extract_bullet_blocks(final_answer)
        if bullet_blocks:
            non_progress_blocks = [block for block in bullet_blocks if not is_progress_block(block)]
            return non_progress_blocks[-1] if non_progress_blocks else bullet_blocks[-1]

        final_answer = re.sub(r"^\s*•\s*", "", final_answer, count=1).strip()
        if not final_answer:
            raise ValueError("Empty Codex response - no content found")
        return final_answer


class ClaudeOutputDetector(BaseOutputDetector):
    """这个检测器负责识别 Claude Code 的常见状态和最后答复。"""

    def has_welcome_banner(self, clean_output: str) -> bool:
        """识别 Claude Code 欢迎页。"""
        return bool(re.search(CLAUDE_WELCOME_PATTERN, clean_output))

    def _content_above_prompt_box(self, clean_output: str, max_lines: int = 40) -> str:
        """
        提取 Claude 底部输入框上方的内容。

        Claude 常把输入区画成上下两条横线，中间是 `❯` 提示，所以要先把这块 UI chrome 剥掉。
        """
        lines = clean_output.splitlines()[-max_lines:]
        border_count = 0
        for index in range(len(lines) - 1, -1, -1):
            if re.fullmatch(r"─+", lines[index].strip()):
                border_count += 1
                if border_count == 2:
                    return "\n".join(lines[:index])
        return "\n".join(lines)

    def detect_status(self, output: str) -> TerminalStatus:
        """根据 Claude Code 的常见 TUI 信号识别状态。"""
        clean_output = self.clean_ansi(output)
        if not clean_output.strip():
            return TerminalStatus.ERROR

        extended_content = "\n".join(clean_output.splitlines()[-200:])
        if "⌕ Search…" in extended_content:
            return TerminalStatus.IDLE

        full_content = "\n".join(clean_output.splitlines()[-40:])
        full_lower_content = full_content.lower()

        if "ctrl+r to toggle" in full_lower_content:
            return TerminalStatus.IDLE

        if re.search(r"(?:do you want|would you like).+\n+[\s\S]*?(?:yes|❯)", full_lower_content):
            return TerminalStatus.WAITING_USER_ANSWER
        if re.search(r"❯\s+\d+\.", full_content):
            return TerminalStatus.WAITING_USER_ANSWER
        if "esc to cancel" in full_lower_content:
            return TerminalStatus.WAITING_USER_ANSWER
        if "enter to select" in full_lower_content:
            return TerminalStatus.WAITING_USER_ANSWER

        above_prompt_box = self._content_above_prompt_box(clean_output)
        above_lower_content = above_prompt_box.lower()
        if "esc to interrupt" in above_lower_content or "ctrl+c to interrupt" in above_lower_content:
            return TerminalStatus.PROCESSING
        if CLAUDE_SPINNER_ACTIVITY_PATTERN.search(above_prompt_box):
            return TerminalStatus.PROCESSING
        if re.search(r"^[{}].* for \d+[smh]".format(re.escape(CLAUDE_SPINNER_CHARS)), above_prompt_box, re.MULTILINE):
            return TerminalStatus.COMPLETED

        return TerminalStatus.IDLE

    def extract_last_message(self, output: str) -> str:
        """优先从 Claude 输入框上方提取最后一个有效文本块。"""
        clean_output = self.clean_ansi(output)
        content = self._content_above_prompt_box(clean_output)
        lines: list[str] = []
        for line in content.splitlines():
            normalized = line.strip()
            if not normalized:
                lines.append("")
                continue
            if self._should_skip_line(normalized):
                continue
            if re.search(r"^❯\s+\d+\.", normalized):
                continue
            if CLAUDE_SPINNER_ACTIVITY_PATTERN.search(normalized):
                continue
            if re.search(r"^[{}].* for \d+[smh]".format(re.escape(CLAUDE_SPINNER_CHARS)), normalized):
                continue
            lines.append(line.rstrip())

        blocks = self._split_blocks("\n".join(lines))
        if not blocks:
            raise ValueError("No Claude Code response found in terminal output")
        return blocks[-1]


class GeminiOutputDetector(BaseOutputDetector):
    """这个检测器负责识别 Gemini CLI 的常见状态和最后答复。"""

    def has_welcome_banner(self, clean_output: str) -> bool:
        """识别 Gemini CLI 欢迎页。"""
        return bool(re.search(GEMINI_WELCOME_PATTERN, clean_output))

    def detect_status(self, output: str) -> TerminalStatus:
        """根据 Gemini CLI 的确认框和取消提示识别状态。"""
        clean_output = self.clean_ansi(output)
        if not clean_output.strip():
            return TerminalStatus.ERROR

        lower_content = clean_output.lower()
        if "waiting for user confirmation" in lower_content:
            return TerminalStatus.WAITING_USER_ANSWER
        if (
            "│ apply this change" in lower_content
            or "│ allow execution" in lower_content
            or "│ do you want to proceed" in lower_content
        ):
            return TerminalStatus.WAITING_USER_ANSWER
        if re.search(
            r"(allow execution|do you want to|apply this change)[\s\S]*?\n+[\s\S]*?\byes\b",
            lower_content,
        ):
            return TerminalStatus.WAITING_USER_ANSWER
        if "esc to cancel" in lower_content:
            return TerminalStatus.PROCESSING
        return TerminalStatus.IDLE

    def extract_last_message(self, output: str) -> str:
        """优先剥离底部输入框，再提取 Gemini 的最后一个有效文本块。"""
        clean_output = self.clean_ansi(output)
        lines = clean_output.splitlines()

        input_box_start = len(lines)
        for index in range(len(lines) - 1, -1, -1):
            normalized = lines[index].strip().lower()
            if normalized.startswith("│ >") or normalized == ">" or normalized.endswith("│ >"):
                input_box_start = index
                break
        content = "\n".join(lines[:input_box_start])

        filtered_lines: list[str] = []
        for line in content.splitlines():
            normalized = line.strip()
            if not normalized:
                filtered_lines.append("")
                continue
            if self._should_skip_line(normalized):
                continue
            if normalized.lower().startswith("waiting for user confirmation"):
                continue
            if normalized.startswith("│ Apply this change") or normalized.startswith("│ Allow execution"):
                continue
            if normalized.startswith("│ Do you want to proceed"):
                continue
            filtered_lines.append(line.rstrip())

        blocks = self._split_blocks("\n".join(filtered_lines))
        if not blocks:
            raise ValueError("No Gemini CLI response found in terminal output")
        return blocks[-1]


def build_output_detector(backend: CliBackend) -> BaseOutputDetector:
    """按后端返回对应的输出检测器。"""
    if backend == CliBackend.CODEX:
        return CodexOutputDetector()
    if backend == CliBackend.CLAUDE:
        return ClaudeOutputDetector()
    if backend == CliBackend.GEMINI:
        return GeminiOutputDetector()
    raise ValueError(f"Unsupported backend: {backend}")


class TmuxAgentRuntime:
    """这个类把 tmux 生命周期、CLI 启动、消息发送和状态观察统一收口。"""

    def __init__(
            self,
            session_name: str,
            work_dir: Path,
            runtime_dir: Path,
            cli_config: AgentCliConfig | None = None,
            state_persistence_sec: float = 0.2,
            state_minimum_sec: float = 0.5,
    ):
        self.session_name = session_name
        self.work_dir = self._resolve_work_dir(work_dir)
        self.runtime_dir = runtime_dir.resolve()
        self.cli_config = cli_config or CodexCliConfig()
        self.state_persistence_sec = state_persistence_sec
        self.state_minimum_sec = state_minimum_sec
        self.terminal_id = uuid.uuid4().hex[:8]
        self.pane_id = ""
        self.send_lock = threading.Lock()
        self.detector = build_output_detector(self.cli_config.backend)
        self.state_machine = DebouncedStateMachine(
            persistence_sec=state_persistence_sec,
            minimum_state_sec=state_minimum_sec,
        )
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.log_path = self.runtime_dir / f"{self.session_name}.log"
        self.raw_log_path = self.runtime_dir / f"{self.session_name}.raw.log"
        self.state_path = self.runtime_dir / f"{self.session_name}.state.json"
        self.last_prompt = ""
        self.last_reply = ""
        self.last_logged_screen = ""
        self.last_logged_note = ""
        self.shell_initialized = False
        self.agent_initialized = False

    def _resolve_work_dir(self, work_dir: Path) -> Path:
        """统一解析工作目录，并拦截极少数明显不应该直接运行 agent 的路径。"""
        resolved = work_dir.expanduser().resolve()
        if not resolved.is_dir():
            raise FileNotFoundError(f"Directory does not exist: {resolved}")
        if str(resolved) in BLOCKED_WORKDIRS:
            raise ValueError(f"Working directory is not allowed: {resolved}")
        return resolved

    def _tmux(self, *args: str, input_text: str | None = None, timeout_sec: float = 10.0) -> \
    subprocess.CompletedProcess[str]:
        """统一执行 tmux 命令，便于集中处理超时、输入和异常。"""
        return subprocess.run(
            ["tmux", *args],
            check=True,
            text=True,
            capture_output=True,
            input=input_text,
            timeout=timeout_sec,
        )

    def session_exists(self) -> bool:
        """检查目标 tmux session 是否已经存在。"""
        result = subprocess.run(
            ["tmux", "has-session", "-t", self.session_name],
            text=True,
            capture_output=True,
        )
        return result.returncode == 0

    def target_exists(self, target: str | None = None) -> bool:
        """检查 pane target 是否仍然有效。"""
        target = target or self.pane_id
        if not target:
            return False
        result = subprocess.run(
            ["tmux", "list-panes", "-t", target],
            text=True,
            capture_output=True,
        )
        return result.returncode == 0

    def _display(self, format_string: str, target: str | None = None) -> str:
        """读取 tmux 的格式化字段，用于获取 pane 状态和路径。"""
        target = target or self.pane_id
        return self._tmux("display-message", "-p", "-t", target, format_string).stdout.strip()

    def pane_dead(self, target: str | None = None) -> bool:
        """判断 pane 是否已经结束。"""
        return self._display("#{pane_dead}", target=target) == "1"

    def pane_current_command(self, target: str | None = None) -> str:
        """读取 pane 前台命令名。"""
        return self._display("#{pane_current_command}", target=target)

    def pane_current_path(self, target: str | None = None) -> str:
        """读取 pane 当前工作目录。"""
        return self._display("#{pane_current_path}", target=target)

    def capture(self, tail_lines: int = 500, target: str | None = None) -> str:
        """抓取 tmux pane 历史输出，保留转义序列后再统一清洗。"""
        target = target or self.pane_id
        return self._tmux(
            "capture-pane",
            "-e",
            "-p",
            "-t",
            target,
            "-S",
            f"-{tail_lines}",
            timeout_sec=15.0,
        ).stdout

    def capture_visible(self, tail_lines: int = 500, target: str | None = None) -> str:
        """
        抓取 tmux 当前屏幕上“已经渲染后的文本”。

        这里故意不带 `-e`，目的是拿到更适合人阅读的可见内容，而不是原始终端控制流。
        """
        target = target or self.pane_id
        return self._tmux(
            "capture-pane",
            "-J",
            "-p",
            "-t",
            target,
            "-S",
            f"-{tail_lines}",
            timeout_sec=15.0,
        ).stdout

    def _reset_runtime_files(self) -> None:
        """在每次新建 session 前清理同名运行时文件，避免不同 run 的内容混在一起。"""
        for path in (self.log_path, self.raw_log_path, self.state_path):
            path.unlink(missing_ok=True)

    def _append_session_banner(self) -> None:
        """在日志文件前插入本次 session 的起始标记，方便后续排障。"""
        timestamp = time.strftime("%Y-%m-%dT%H:%M:%S")
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as file:
            file.write(f"\n\n=== Session started: {timestamp} ===\n\n")
        with self.raw_log_path.open("a", encoding="utf-8") as file:
            file.write(f"\n\n=== Session started: {timestamp} ===\n\n")

    def _start_pipe_logging(self) -> None:
        """把原始终端流持续追加到 raw 日志，作为底层排障材料保留。"""
        self._append_session_banner()
        command = f"cat >> {shlex.quote(str(self.raw_log_path))}"
        self._tmux("pipe-pane", "-t", self.pane_id, "-o", command)

    def _normalize_visible_log_text(self, text: str) -> str:
        """
        清洗可见屏幕文本，让日志更接近“人眼看到的内容”。

        这里会去掉控制字符、右侧多余空白，并压缩过多的空行，避免日志被 UI 边框和刷新噪声污染。
        """
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        normalized = self.detector.clean_ansi(normalized)
        normalized = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", normalized)

        lines = [line.rstrip() for line in normalized.splitlines()]

        compact_lines: list[str] = []
        blank_run = 0
        for line in lines:
            if not line.strip():
                blank_run += 1
                if blank_run <= 2:
                    compact_lines.append("")
                continue
            blank_run = 0
            compact_lines.append(line)

        return "\n".join(compact_lines).strip()

    def _append_clean_log_snapshot(self, note: str, snapshot: SessionSnapshot | None = None) -> None:
        """
        追加一段可读日志快照。

        这里记录的是“当前屏幕渲染结果”，而不是原始字节流，所以适合直接打开阅读。
        """
        if self.pane_id and self.target_exists():
            visible_text = self.capture_visible(tail_lines=500)
        elif snapshot is not None:
            visible_text = snapshot.clean_output
        else:
            visible_text = ""

        normalized = self._normalize_visible_log_text(visible_text)
        if not normalized:
            return
        if normalized == self.last_logged_screen and note == self.last_logged_note:
            return

        timestamp = time.strftime("%Y-%m-%dT%H:%M:%S")
        header_parts = [f"=== {timestamp}", note]
        if snapshot is not None:
            header_parts.append(f"detected={snapshot.detected_status.value}")
            header_parts.append(f"confirmed={snapshot.confirmed_status.value}")
        header = " | ".join(header_parts) + " ==="

        with self.log_path.open("a", encoding="utf-8") as file:
            file.write(f"{header}\n{normalized}\n\n")

        self.last_logged_screen = normalized
        self.last_logged_note = note

    def _write_state_file(
            self,
            snapshot: SessionSnapshot | None = None,
            note: str = "",
            extra: dict[str, str] | None = None,
    ) -> None:
        """把当前运行时状态原子写入 JSON，方便外部观察和断点排障。"""
        payload: dict[str, object] = {
            "backend": self.cli_config.backend.value,
            "session_name": self.session_name,
            "pane_id": self.pane_id,
            "terminal_id": self.terminal_id,
            "work_dir": str(self.work_dir),
            "log_path": str(self.log_path),
            "raw_log_path": str(self.raw_log_path),
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "note": note,
        }
        if snapshot is not None:
            payload.update(
                {
                    "detected_status": snapshot.detected_status.value,
                    "confirmed_status": snapshot.confirmed_status.value,
                    "current_command": snapshot.current_command,
                    "current_path": snapshot.current_path,
                    "pane_dead": snapshot.pane_dead,
                    "snapshot_timestamp": snapshot.timestamp,
                }
            )
        if extra:
            payload.update(extra)

        temp_path = self.state_path.with_suffix(".tmp")
        temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temp_path.replace(self.state_path)

    def _build_shell_bootstrap_command(self) -> str:
        """构造 session 启动命令，并把会话标识注入 shell 环境。"""
        shell_path = os.environ.get("SHELL", "/bin/zsh")
        parts = [
            "env",
            f"CAO_TERMINAL_ID={shlex.quote(self.terminal_id)}",
            f"TEAM_TERMINAL_ID={shlex.quote(self.terminal_id)}",
            f"TEAM_AGENT_BACKEND={shlex.quote(self.cli_config.backend.value)}",
            shlex.quote(shell_path),
            "-il",
        ]
        return " ".join(parts)

    def create_session(self) -> str:
        """创建新的 detached session，并返回稳定的 pane_id。"""
        if self.session_exists():
            self._tmux("kill-session", "-t", self.session_name)

        self._reset_runtime_files()

        result = self._tmux(
            "new-session",
            "-d",
            "-P",
            "-F",
            "#{pane_id}",
            "-s",
            self.session_name,
            "-c",
            str(self.work_dir),
            self._build_shell_bootstrap_command(),
        )
        self.pane_id = result.stdout.strip()

        # 关闭自动改名，避免后续日志和状态文件里的 target 发生漂移。
        self._tmux("set-option", "-t", self.session_name, "allow-rename", "off")
        self._tmux("set-window-option", "-t", f"{self.session_name}:0", "automatic-rename", "off")

        self._start_pipe_logging()
        self.shell_initialized = False
        self.agent_initialized = False
        self.last_reply = ""
        self.state_machine = DebouncedStateMachine(
            persistence_sec=self.state_persistence_sec,
            minimum_state_sec=self.state_minimum_sec,
        )
        self._write_state_file(note="session_created")
        return self.pane_id

    def kill_session(self) -> None:
        """清理 session，避免留下孤儿 pane。"""
        if self.session_exists():
            self._write_state_file(note="session_killed")
            self._tmux("kill-session", "-t", self.session_name)
        self.pane_id = ""
        self.shell_initialized = False
        self.agent_initialized = False

    def send_special_key(self, key: str) -> None:
        """发送特殊键位，例如 Enter 或 Ctrl+C。"""
        with self.send_lock:
            self._tmux("send-keys", "-t", self.pane_id, key)

    def send_text(self, text: str, enter_count: int) -> None:
        """
        使用 bracketed paste 发送文本，并串行化同一 pane 的所有输入。

        这样可以同时解决三个问题：
        1. 避免逐字符输入时触发 TUI 快捷键。
        2. 避免长文本被 tmux `send-keys -l` 截断。
        3. 避免多个并发输入源打乱同一个 pane 的顺序。
        """
        buffer_name = f"{self.cli_config.backend.value}_{uuid.uuid4().hex[:8]}"
        with self.send_lock:
            try:
                self._tmux("load-buffer", "-b", buffer_name, "-", input_text=text)
                self._tmux("paste-buffer", "-p", "-b", buffer_name, "-t", self.pane_id)

                # 先让 TUI 完整处理粘贴结束序列，再发送 Enter，避免回车被吞掉。
                time.sleep(0.3)
                for index in range(enter_count):
                    if index > 0:
                        # 某些输入框第一次 Enter 会先结束多行态，第二次才会真正提交。
                        time.sleep(0.5)
                    self._tmux("send-keys", "-t", self.pane_id, "Enter")
            finally:
                subprocess.run(
                    ["tmux", "delete-buffer", "-b", buffer_name],
                    check=False,
                    capture_output=True,
                )

    def take_snapshot(self, tail_lines: int = 500) -> SessionSnapshot:
        """获取一次 pane 快照，并产出检测状态和去抖后的确认状态。"""
        raw_output = self.capture(tail_lines=tail_lines)
        clean_output = self.detector.clean_ansi(raw_output)
        detected_status = self.detector.detect_status(raw_output)
        now = time.monotonic()
        confirmed_status = self.state_machine.observe(detected_status, now)
        snapshot = SessionSnapshot(
            timestamp=now,
            detected_status=detected_status,
            confirmed_status=confirmed_status,
            current_command=self.pane_current_command(),
            current_path=self.pane_current_path(),
            pane_dead=self.pane_dead(),
            raw_output=raw_output,
            clean_output=clean_output,
        )
        self._write_state_file(snapshot=snapshot, note="snapshot")
        return snapshot

    def get_snapshot(self, tail_lines: int = 500) -> SessionSnapshot:
        """对外暴露快照接口，方便调用方按需读取当前终端状态。"""
        return self.take_snapshot(tail_lines=tail_lines)

    def get_current_status(self, tail_lines: int = 500) -> TerminalStatus:
        """返回当前确认后的状态，适合上层做轮询或 UI 展示。"""
        return self.take_snapshot(tail_lines=tail_lines).confirmed_status

    def get_runtime_metadata(self) -> dict[str, str]:
        """返回当前运行时的核心元信息，便于别的模块接入。"""
        return {
            "backend": self.cli_config.backend.value,
            "terminal_id": self.terminal_id,
            "session_name": self.session_name,
            "pane_id": self.pane_id,
            "work_dir": str(self.work_dir),
            "log_path": str(self.log_path),
            "raw_log_path": str(self.raw_log_path),
            "state_path": str(self.state_path),
        }

    def read_state(self) -> dict[str, object]:
        """读取当前状态文件，给外部监控或调试代码直接消费。"""
        if not self.state_path.exists():
            return {}
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def read_clean_log(self) -> str:
        """读取清洗后的可读日志。"""
        if not self.log_path.exists():
            return ""
        return self.log_path.read_text(encoding="utf-8")

    def read_raw_log(self) -> str:
        """读取原始终端流日志，只用于深入排障。"""
        if not self.raw_log_path.exists():
            return ""
        return self.raw_log_path.read_text(encoding="utf-8", errors="replace")

    def wait_for_shell_ready(self, timeout_sec: float = 12.0) -> None:
        """
        等待 shell 真正可用。

        这里不是单纯等几秒，而是先做被动判断；如果迟迟没有稳定迹象，就主动发探针确认 shell 能执行命令。
        """
        started_at = time.monotonic()
        deadline = started_at + timeout_sec
        previous_output: str | None = None
        probe_marker = f"TMUX_READY_{uuid.uuid4().hex[:8]}"
        probe_sent = False

        while time.monotonic() < deadline:
            if not self.target_exists():
                raise RuntimeError("tmux pane exited before shell became ready")
            if self.pane_dead():
                raise RuntimeError(f"tmux pane died before shell became ready:\n{self.capture(120)}")

            output = self.capture(tail_lines=80)
            output_stable = bool(output.strip()) and previous_output is not None and output == previous_output
            command_ready = self.pane_current_command() in SHELL_COMMANDS
            path_ready = self.pane_current_path() == str(self.work_dir)
            if output_stable and command_ready and path_ready:
                snapshot = self.take_snapshot(tail_lines=120)
                self._append_clean_log_snapshot("shell_ready", snapshot=snapshot)
                self._write_state_file(
                    note="shell_ready",
                    extra={
                        "current_command": self.pane_current_command(),
                        "current_path": self.pane_current_path(),
                    },
                )
                self.shell_initialized = True
                return

            if probe_sent and probe_marker in output:
                self.send_text("clear", enter_count=1)
                snapshot = self.take_snapshot(tail_lines=120)
                self._append_clean_log_snapshot("shell_ready_by_probe", snapshot=snapshot)
                self._write_state_file(note="shell_ready_by_probe")
                self.shell_initialized = True
                return

            if not probe_sent and time.monotonic() - started_at >= 3.0:
                probe_sent = True
                self.send_text(f"echo {probe_marker}", enter_count=1)

            previous_output = output
            time.sleep(0.4)

        raise RuntimeError(
            "Shell initialization timed out.\n"
            f"pane_current_command={self.pane_current_command()!r}\n"
            f"pane_current_path={self.pane_current_path()!r}\n"
            f"{self.capture(120)}"
        )

    def warmup_shell(self) -> None:
        """先让新建 shell 执行一条简单命令，减少首次直接启动 CLI 的异常。"""
        self.send_text("echo ready", enter_count=1)
        time.sleep(1.5)

    def prepare_shell(self, recreate: bool = False) -> SessionSnapshot:
        """
        准备一个可用的 tmux shell 环境。

        这是“创建 tmux + 等 shell 就绪 + 做 warmup”的组合封装，适合被别的流程直接复用。
        """
        if recreate or not self.pane_id or not self.target_exists():
            self.create_session()
        self.wait_for_shell_ready()
        self.warmup_shell()
        return self.take_snapshot(tail_lines=120)

    def launch_agent(self, timeout_sec: float = 60.0) -> SessionSnapshot:
        """
        启动目标 CLI，并等待它进入可交互状态。

        启动期间如果出现工作区信任提示，会自动确认默认项；如果 CLI 直接掉回 shell，会明确抛错。
        """
        agent_name = self.cli_config.display_name()
        self.send_text(self.cli_config.build_command(), enter_count=1)
        deadline = time.monotonic() + timeout_sec
        last_trust_ack = 0.0

        while time.monotonic() < deadline:
            if not self.target_exists():
                raise RuntimeError(f"tmux pane exited while {agent_name} was starting")

            snapshot = self.take_snapshot(tail_lines=220)
            if snapshot.pane_dead:
                raise RuntimeError(f"tmux pane died while {agent_name} was starting:\n{snapshot.raw_output}")

            if self.detector.has_trust_prompt(snapshot.clean_output):
                now = time.monotonic()
                if now - last_trust_ack > 1.0:
                    self.send_special_key("Enter")
                    last_trust_ack = now
                    self._append_clean_log_snapshot("trust_prompt_ack", snapshot=snapshot)
                time.sleep(0.5)
                continue

            if snapshot.confirmed_status in {TerminalStatus.IDLE, TerminalStatus.COMPLETED}:
                if (
                    snapshot.current_command in self.cli_config.expected_current_commands()
                    or self.detector.has_welcome_banner(snapshot.clean_output)
                ):
                    self._append_clean_log_snapshot("agent_ready", snapshot=snapshot)
                    self._write_state_file(snapshot=snapshot, note="agent_ready")
                    self.agent_initialized = True
                    return snapshot

            # 如果已经明确回到 shell prompt，说明 CLI 启动失败或提前退出了。
            if snapshot.current_command in SHELL_COMMANDS and self.detector.looks_like_shell_prompt(
                    snapshot.clean_output):
                raise RuntimeError(f"{agent_name} exited back to shell before becoming ready.\n{snapshot.raw_output}")

            time.sleep(0.5)

        raise RuntimeError(f"Timed out waiting for {agent_name} to become ready.\n{self.capture(240)}")

    def ensure_agent_ready(self, recreate_session: bool = False) -> SessionSnapshot:
        """
        确保当前 runtime 已经拥有一个可交互的 CLI 会话。

        如果 session 丢失、pane 回到 shell，或者调用方明确要求重建，就自动重走启动链路。
        """
        if recreate_session or not self.pane_id or not self.target_exists():
            self.shell_initialized = False
            self.agent_initialized = False

        if not self.shell_initialized:
            self.prepare_shell(recreate=recreate_session or not self.target_exists())

        if self.agent_initialized and self.target_exists():
            snapshot = self.take_snapshot(tail_lines=220)
            if snapshot.current_command in self.cli_config.expected_current_commands():
                return snapshot
            self.agent_initialized = False

        return self.launch_agent()

    def restart_agent(self) -> SessionSnapshot:
        """强制重建 tmux session 并重新启动目标 CLI。"""
        self.kill_session()
        self.shell_initialized = False
        self.agent_initialized = False
        return self.ensure_agent_ready(recreate_session=True)

    def launch_codex(self, timeout_sec: float = 60.0) -> SessionSnapshot:
        """这是给旧调用方保留的兼容方法，内部会转到通用启动逻辑。"""
        return self.launch_agent(timeout_sec=timeout_sec)

    def ensure_codex_ready(self, recreate_session: bool = False) -> SessionSnapshot:
        """这是给旧调用方保留的兼容方法，内部会转到通用就绪逻辑。"""
        return self.ensure_agent_ready(recreate_session=recreate_session)

    def restart_codex(self) -> SessionSnapshot:
        """这是给旧调用方保留的兼容方法，内部会转到通用重启逻辑。"""
        return self.restart_agent()

    def send_shell_command(self, command: str, enter_count: int = 1) -> SessionSnapshot:
        """
        向 shell 层发送命令并返回最新快照。

        这个方法适合 warmup、探针、环境检查等场景，不限定必须是某个特定 CLI TUI 内的消息。
        """
        if not self.shell_initialized:
            self.prepare_shell(recreate=not self.target_exists())
        self.send_text(command, enter_count=enter_count)
        return self.take_snapshot(tail_lines=180)

    def wait_for_state(
            self,
            targets: set[TerminalStatus],
            timeout_sec: float,
            polling_interval: float = 0.5,
            note: str = "waiting_state",
    ) -> SessionSnapshot:
        """等待确认状态进入目标集合中的任意一个。"""
        deadline = time.monotonic() + timeout_sec
        last_snapshot: SessionSnapshot | None = None

        while time.monotonic() < deadline:
            if not self.target_exists():
                raise RuntimeError("tmux pane exited while waiting for state")

            snapshot = self.take_snapshot(tail_lines=500)
            last_snapshot = snapshot
            if snapshot.pane_dead:
                raise RuntimeError(f"tmux pane died while waiting for state:\n{snapshot.raw_output}")

            if snapshot.confirmed_status in targets:
                self._write_state_file(snapshot=snapshot, note=note)
                return snapshot

            time.sleep(polling_interval)

        target_names = ", ".join(status.value for status in sorted(targets, key=lambda item: item.value))
        final_output = last_snapshot.raw_output if last_snapshot else self.capture(220)
        raise RuntimeError(
            f"Timed out waiting for state {{{target_names}}}.\n"
            f"{final_output}"
        )

    def submit_user_message(self, message: str, timeout_sec: float = 20.0) -> SessionSnapshot:
        """
        向当前 CLI 提交用户消息，并确认它确实开始处理或明确返回别的终态。

        如果消息发出后长时间仍停留在 idle，会补发一次 Enter，处理某些输入框把第一次 Enter 当作结束多行输入的问题。
        """
        self.last_prompt = message
        self.send_text(message, enter_count=self.cli_config.submit_enter_count())
        deadline = time.monotonic() + timeout_sec
        extra_enter_sent = False
        submit_started_at = time.monotonic()

        while time.monotonic() < deadline:
            snapshot = self.take_snapshot(tail_lines=320)
            if snapshot.pane_dead:
                raise RuntimeError(f"tmux pane died after sending message:\n{snapshot.raw_output}")

            if snapshot.detected_status == TerminalStatus.PROCESSING:
                self._append_clean_log_snapshot("message_processing", snapshot=snapshot)
                self._write_state_file(snapshot=snapshot, note="message_processing")
                return snapshot

            if snapshot.confirmed_status in {
                TerminalStatus.COMPLETED,
                TerminalStatus.WAITING_USER_ANSWER,
                TerminalStatus.ERROR,
            }:
                self._append_clean_log_snapshot("message_terminal_state", snapshot=snapshot)
                self._write_state_file(snapshot=snapshot, note="message_terminal_state")
                return snapshot

            if (
                    snapshot.confirmed_status == TerminalStatus.IDLE
                    and not extra_enter_sent
                    and time.monotonic() - submit_started_at >= 3.0
            ):
                self.send_special_key("Enter")
                extra_enter_sent = True
                self._append_clean_log_snapshot("extra_enter_sent", snapshot=snapshot)
                self._write_state_file(snapshot=snapshot, note="extra_enter_sent")

            time.sleep(0.5)

        raise RuntimeError(
            f"Timed out waiting for {self.cli_config.display_name()} to acknowledge the submitted message.\n"
            f"{self.capture(260)}"
        )

    def wait_for_final_reply(self, timeout_sec: float = 120.0) -> SessionSnapshot:
        """等待当前 CLI 回到最终终态，用于提取最后一轮答复。"""
        deadline = time.monotonic() + timeout_sec
        seen_processing = False
        last_snapshot: SessionSnapshot | None = None

        while time.monotonic() < deadline:
            snapshot = self.take_snapshot(tail_lines=500)
            last_snapshot = snapshot
            if snapshot.pane_dead:
                raise RuntimeError(f"tmux pane died while waiting for reply:\n{snapshot.raw_output}")

            if snapshot.detected_status == TerminalStatus.PROCESSING or snapshot.confirmed_status == TerminalStatus.PROCESSING:
                seen_processing = True

            if snapshot.confirmed_status in {
                TerminalStatus.COMPLETED,
                TerminalStatus.WAITING_USER_ANSWER,
                TerminalStatus.ERROR,
            }:
                # 一旦曾经进入 processing，再看到终态时就可以认为本轮交互已经收束。
                if seen_processing or snapshot.confirmed_status != TerminalStatus.COMPLETED:
                    self._append_clean_log_snapshot("reply_terminal_state", snapshot=snapshot)
                    self._write_state_file(snapshot=snapshot, note="reply_terminal_state")
                    return snapshot

            time.sleep(0.5)

        final_output = last_snapshot.raw_output if last_snapshot else self.capture(300)
        raise RuntimeError(f"Timed out waiting for {self.cli_config.display_name()} final reply.\n{final_output}")

    def run_once(self, prompt: str) -> RunResult:
        """跑通一次完整的 shell -> CLI -> 发送消息 -> 等回复 流程。"""
        self.create_session()
        try:
            self.ensure_agent_ready()
            first_snapshot = self.submit_user_message(prompt)

            final_snapshot = first_snapshot
            if first_snapshot.detected_status == TerminalStatus.PROCESSING:
                final_snapshot = self.wait_for_final_reply()
            elif first_snapshot.confirmed_status not in {
                TerminalStatus.COMPLETED,
                TerminalStatus.WAITING_USER_ANSWER,
                TerminalStatus.ERROR,
            }:
                final_snapshot = self.wait_for_final_reply()

            if final_snapshot.confirmed_status == TerminalStatus.WAITING_USER_ANSWER:
                raise RuntimeError(
                    f"{self.cli_config.display_name()} is waiting for more user input.\n{final_snapshot.raw_output}"
                )
            if final_snapshot.confirmed_status == TerminalStatus.ERROR:
                raise RuntimeError(f"{self.cli_config.display_name()} reported an error.\n{final_snapshot.raw_output}")

            final_output = self.capture(tail_lines=600)
            reply = self.detector.extract_last_message(final_output)
            self.last_reply = reply
            self._append_clean_log_snapshot("reply_ready", snapshot=final_snapshot)
            self._write_state_file(
                snapshot=final_snapshot,
                note="reply_ready",
                extra={"reply": reply},
            )
            return RunResult(
                backend=self.cli_config.backend.value,
                terminal_id=self.terminal_id,
                session_name=self.session_name,
                pane_id=self.pane_id,
                reply=reply,
                log_path=self.log_path,
                raw_log_path=self.raw_log_path,
                state_path=self.state_path,
            )
        except Exception as error:
            self._append_clean_log_snapshot("run_failed")
            self._write_state_file(note="run_failed", extra={"error": str(error)})
            raise

    def ask(self, prompt: str, timeout_sec: float = 120.0) -> str:
        """
        在当前 runtime 上向 CLI 发送一条消息，并返回最终答复文本。

        这个方法适合复用同一个 tmux session 连续多轮对话。
        """
        self.ensure_agent_ready()
        first_snapshot = self.submit_user_message(prompt)

        final_snapshot = first_snapshot
        if first_snapshot.detected_status == TerminalStatus.PROCESSING:
            final_snapshot = self.wait_for_final_reply(timeout_sec=timeout_sec)
        elif first_snapshot.confirmed_status not in {
            TerminalStatus.COMPLETED,
            TerminalStatus.WAITING_USER_ANSWER,
            TerminalStatus.ERROR,
        }:
            final_snapshot = self.wait_for_final_reply(timeout_sec=timeout_sec)

        if final_snapshot.confirmed_status == TerminalStatus.WAITING_USER_ANSWER:
            raise RuntimeError(
                f"{self.cli_config.display_name()} is waiting for more user input.\n{final_snapshot.raw_output}"
            )
        if final_snapshot.confirmed_status == TerminalStatus.ERROR:
            raise RuntimeError(f"{self.cli_config.display_name()} reported an error.\n{final_snapshot.raw_output}")

        final_output = self.capture(tail_lines=600)
        reply = self.detector.extract_last_message(final_output)
        self.last_reply = reply
        self._append_clean_log_snapshot("reply_ready", snapshot=final_snapshot)
        self._write_state_file(
            snapshot=final_snapshot,
            note="reply_ready",
            extra={"reply": reply},
        )
        return reply

    def ask_with_info(self, prompt: str, timeout_sec: float = 120.0) -> dict[str, str]:
        """
        在连续会话模式下发送一条消息，并返回一份适合上层编排器消费的结构化结果。
        """
        reply = self.ask(prompt=prompt, timeout_sec=timeout_sec)
        result = self.get_runtime_metadata()
        result["reply"] = reply
        return result


TmuxCodexRuntime = TmuxAgentRuntime


def run_tmux_agent_once(
        work_dir: Path | str,
        prompt: str = DEFAULT_PROMPT,
        session_name: str = DEFAULT_SESSION_NAME,
        runtime_dir: Path | str = DEFAULT_RUNTIME_DIR,
        cleanup: bool = False,
        cli_config: AgentCliConfig | None = None,
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
    )


def create_tmux_session(
        work_dir: Path | str,
        session_name: str = DEFAULT_SESSION_NAME,
        runtime_dir: Path | str = DEFAULT_RUNTIME_DIR,
        cli_config: AgentCliConfig | None = None,
) -> TmuxAgentRuntime:
    """
    创建并准备一个可用的 tmux shell，会返回可继续复用的 runtime 对象。
    """
    runtime = TmuxAgentRuntime(
        session_name=session_name,
        work_dir=Path(work_dir),
        runtime_dir=Path(runtime_dir),
        cli_config=cli_config,
    )
    runtime.prepare_shell(recreate=True)
    return runtime


def create_agent_cli(
        work_dir: Path | str,
        session_name: str = DEFAULT_SESSION_NAME,
        runtime_dir: Path | str = DEFAULT_RUNTIME_DIR,
        cli_config: AgentCliConfig | None = None,
) -> TmuxAgentRuntime:
    """
    创建一个已经启动好目标 CLI 的 runtime，方便后续反复调用 `ask()`。
    """
    runtime = TmuxAgentRuntime(
        session_name=session_name,
        work_dir=Path(work_dir),
        runtime_dir=Path(runtime_dir),
        cli_config=cli_config,
    )
    runtime.ensure_agent_ready(recreate_session=True)
    return runtime


def create_codex_cli(
        work_dir: Path | str,
        session_name: str = DEFAULT_SESSION_NAME,
        runtime_dir: Path | str = DEFAULT_RUNTIME_DIR,
) -> TmuxAgentRuntime:
    """
    创建一个已经启动好 Codex 的 runtime，方便后续反复调用 `ask()`。
    """
    return create_agent_cli(
        work_dir=work_dir,
        session_name=session_name,
        runtime_dir=runtime_dir,
        cli_config=CodexCliConfig(),
    )


def create_claude_cli(
        work_dir: Path | str,
        session_name: str = DEFAULT_SESSION_NAME,
        runtime_dir: Path | str = DEFAULT_RUNTIME_DIR,
        cli_config: ClaudeCliConfig | None = None,
) -> TmuxAgentRuntime:
    """
    创建一个已经启动好 Claude Code 的 runtime。
    """
    return create_agent_cli(
        work_dir=work_dir,
        session_name=session_name,
        runtime_dir=runtime_dir,
        cli_config=cli_config or ClaudeCliConfig(),
    )


def create_gemini_cli(
        work_dir: Path | str,
        session_name: str = DEFAULT_SESSION_NAME,
        runtime_dir: Path | str = DEFAULT_RUNTIME_DIR,
        cli_config: GeminiCliConfig | None = None,
) -> TmuxAgentRuntime:
    """
    创建一个已经启动好 Gemini CLI 的 runtime。
    """
    return create_agent_cli(
        work_dir=work_dir,
        session_name=session_name,
        runtime_dir=runtime_dir,
        cli_config=cli_config or GeminiCliConfig(),
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
    )
    return {
        "backend": result.backend,
        "terminal_id": result.terminal_id,
        "session_name": result.session_name,
        "pane_id": result.pane_id,
        "reply": result.reply,
        "log_path": str(result.log_path),
        "raw_log_path": str(result.raw_log_path),
        "state_path": str(result.state_path),
    }


if __name__ == '__main__':
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
