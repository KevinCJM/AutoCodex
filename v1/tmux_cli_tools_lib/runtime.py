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
from pathlib import Path

from .common import (
    BLOCKED_WORKDIRS,
    CODEX_QUEUED_MESSAGE_PATTERNS,
    SHELL_COMMANDS,
    AgentCliConfig,
    CliBackend,
    CodexCliConfig,
    DebouncedStateMachine,
    ResumeResult,
    RunResult,
    SessionSnapshot,
    TerminalStatus,
)
from .detectors import build_output_detector


class TmuxAgentRuntime:
    """这个类把 tmux 生命周期、CLI 启动、消息发送和状态观察统一收口。"""

    def __init__(
            self,
            session_name: str,
            work_dir: Path,
            runtime_dir: Path,
            cli_config: AgentCliConfig | None = None,
            prelaunch_hooks: Sequence[str | Mapping[str, object]] | None = None,
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
        self.agent_session_id = ""
        self.last_logged_screen = ""
        self.last_logged_note = ""
        self.shell_initialized = False
        self.agent_initialized = False
        self.prelaunch_hooks = tuple(self._normalize_hook(hook) for hook in (prelaunch_hooks or ()))
        self.prelaunch_completed = False
        self._hydrate_from_state_file()

    @staticmethod
    def _normalize_hook(hook: str | Mapping[str, object]) -> str:
        """把前置脚本配置统一收敛成非空字符串。"""
        if isinstance(hook, str):
            normalized = hook.strip()
        elif isinstance(hook, Mapping):
            normalized = str(hook.get("inline", "")).strip()
        else:
            normalized = ""

        if not normalized:
            raise ValueError(f"Invalid prelaunch hook: {hook!r}")
        return normalized

    def _hydrate_from_state_file(self) -> None:
        """
        从已有状态文件回填运行时关键信息。

        这样新的 Python 进程也能继承之前记录下来的 tmux pane、最近 prompt 和各后端 session_id。
        """
        if not self.state_path.exists():
            return

        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except Exception:
            return

        self.pane_id = str(payload.get("pane_id", "") or "")
        self.last_prompt = str(payload.get("last_prompt", "") or "")
        self.last_reply = str(payload.get("last_reply", "") or "")
        self.agent_session_id = str(
            payload.get("agent_session_id")
            or payload.get("codex_session_id")
            or payload.get("claude_session_id")
            or payload.get("gemini_session_id")
            or ""
        )

    def _resolve_work_dir(self, work_dir: Path) -> Path:
        """统一解析工作目录，并拦截极少数明显不应该直接运行 agent 的路径。"""
        resolved = work_dir.expanduser().resolve()
        if not resolved.is_dir():
            raise FileNotFoundError(f"Directory does not exist: {resolved}")
        if str(resolved) in BLOCKED_WORKDIRS:
            raise ValueError(f"Working directory is not allowed: {resolved}")
        return resolved

    @staticmethod
    def _paths_equivalent(left: str | Path, right: str | Path) -> bool:
        """比较两个路径是否指向同一位置，兼容 /var 与 /private/var 等别名差异。"""
        try:
            return Path(str(left)).expanduser().resolve() == Path(str(right)).expanduser().resolve()
        except Exception:
            return str(left) == str(right)

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

    def _primary_pane_id_for_session(self) -> str:
        """读取当前 session 的主 pane id，便于新进程重新接管已存在的 tmux session。"""
        if not self.session_exists():
            return ""
        result = self._tmux("list-panes", "-t", self.session_name, "-F", "#{pane_id}")
        pane_ids = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        return pane_ids[0] if pane_ids else ""

    def _adopt_existing_session(self) -> bool:
        """
        认领一个已经存在的 tmux session。

        这一步只更新 pane id，不会重建 session，也不会清理现有内容。
        """
        if self.target_exists():
            return True
        pane_id = self._primary_pane_id_for_session()
        if not pane_id:
            return False
        self.pane_id = pane_id
        return self.target_exists()

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

    def _run_shell_command_and_wait(self, command: str, timeout_sec: float = 20.0) -> str:
        """
        在当前 shell 里执行一条命令，并等待命令明确结束。

        这里通过唯一标记回传退出码，避免只靠 sleep 猜命令是否执行完成。
        """
        if not command.strip():
            return self.capture(tail_lines=120)

        marker = f"TMUX_CLI_TOOLS_DONE_{uuid.uuid4().hex[:10]}"
        wrapped_command = (
            f"{command}\n"
            f"__tmux_cli_tools_status=$?\n"
            f'printf "\\n{marker}:%s\\n" "$__tmux_cli_tools_status"\n'
        )
        self.send_text(wrapped_command, enter_count=1)

        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            output = self.capture(tail_lines=240)
            match = re.search(rf"{re.escape(marker)}:(\d+)", output)
            if match:
                exit_code = int(match.group(1))
                if exit_code != 0:
                    raise RuntimeError(f"Shell command failed with exit code {exit_code}: {command}\n{output}")
                return output
            time.sleep(0.4)

        raise RuntimeError(f"Timed out waiting for shell command to finish: {command}\n{self.capture(240)}")

    def _run_prelaunch_hooks(self, force: bool = False, timeout_sec: float = 30.0) -> None:
        """
        在当前 shell 里执行前置脚本。

        只有需要启动或恢复 CLI 时才会真正执行，避免普通消息交互时反复污染 shell 环境。
        """
        if not self.prelaunch_hooks:
            self.prelaunch_completed = True
            return
        if self.prelaunch_completed and not force:
            return

        for hook in self.prelaunch_hooks:
            self._run_shell_command_and_wait(hook, timeout_sec=timeout_sec)

        self.prelaunch_completed = True

    def _iter_recent_codex_session_files(self, limit: int = 200) -> list[Path]:
        """按修改时间倒序枚举最近的 Codex session 文件，用于反查 session_id。"""
        sessions_root = Path.home() / ".codex" / "sessions"
        if not sessions_root.exists():
            return []

        candidates = [path for path in sessions_root.rglob("*.jsonl") if path.is_file()]
        candidates.sort(key=lambda item: item.stat().st_mtime, reverse=True)
        return candidates[:limit]

    @staticmethod
    def _read_codex_session_meta(session_file: Path) -> dict[str, object]:
        """从 Codex session 文件里提取首个 `session_meta` 事件。"""
        try:
            with session_file.open("r", encoding="utf-8") as file:
                for _ in range(8):
                    line = file.readline()
                    if not line:
                        break
                    payload = json.loads(line)
                    if payload.get("type") == "session_meta":
                        return dict(payload.get("payload", {}))
        except Exception:
            return {}
        return {}

    def _codex_session_contains_runtime_marker(self, session_file: Path, max_lines: int = 16) -> bool:
        """检查早期 developer 指令中是否包含当前 runtime marker。"""
        marker = self._codex_runtime_marker()
        if not marker:
            return False

        try:
            with session_file.open("r", encoding="utf-8", errors="ignore") as file:
                for _ in range(max_lines):
                    line = file.readline()
                    if not line:
                        break
                    payload = json.loads(line)
                    if payload.get("type") != "response_item":
                        continue
                    message = payload.get("payload") or {}
                    if str(message.get("role", "") or "") != "developer":
                        continue
                    for content_item in message.get("content", []) or []:
                        text = str(content_item.get("text", "") or "")
                        if marker in text:
                            return True
        except Exception:
            return False
        return False

    def _codex_runtime_marker(self) -> str:
        """返回写入 Codex developer instructions 的运行时关联标记。"""
        return f"ACX_RUNTIME_SESSION={self.session_name}"

    def _codex_session_matches_runtime(self, meta: dict[str, object], session_file: Path | None = None) -> bool:
        """判断某个 Codex session_meta 是否属于当前 runtime。"""
        if not self._paths_equivalent(str(meta.get("cwd", "")), self.work_dir):
            return False
        base_instructions = meta.get("base_instructions") or {}
        if isinstance(base_instructions, Mapping):
            instructions_text = str(base_instructions.get("text", "") or "")
            if self._codex_runtime_marker() in instructions_text:
                return True
        if session_file and self._codex_session_contains_runtime_marker(session_file):
            return True
        return False

    def _discover_latest_codex_session_id(self, started_after: float | None = None) -> str:
        """
        根据工作目录反查最近的 Codex session_id。

        这是启动或恢复后用于落盘 `session_id` 的兜底策略，不依赖人工输入。
        """
        if self.cli_config.backend != CliBackend.CODEX:
            return ""

        best_path: Path | None = None
        best_mtime = -1.0
        fallback_path: Path | None = None
        fallback_mtime = -1.0
        for session_file in self._iter_recent_codex_session_files():
            stat = session_file.stat()
            if started_after is not None and stat.st_mtime + 2 < started_after:
                continue
            meta = self._read_codex_session_meta(session_file)
            if not meta:
                continue
            session_id = str(meta.get("id", "") or "")
            if not session_id:
                continue
            if self._codex_session_matches_runtime(meta, session_file=session_file) and stat.st_mtime > best_mtime:
                best_mtime = stat.st_mtime
                best_path = session_file
                continue
            if not self._paths_equivalent(str(meta.get("cwd", "")), self.work_dir):
                continue
            if stat.st_mtime > fallback_mtime:
                fallback_mtime = stat.st_mtime
                fallback_path = session_file

        if best_path is None:
            best_path = fallback_path
        if best_path is None:
            return ""

        meta = self._read_codex_session_meta(best_path)
        return str(meta.get("id", "") or "")

    def _iter_recent_claude_session_files(self, limit: int = 200) -> list[Path]:
        """按修改时间倒序枚举最近的 Claude 会话文件。"""
        sessions_root = Path.home() / ".claude" / "projects"
        if not sessions_root.exists():
            return []

        candidates = [
            path
            for path in sessions_root.rglob("*.jsonl")
            if path.is_file() and path.name != "sessions-index.json"
        ]
        candidates.sort(key=lambda item: item.stat().st_mtime, reverse=True)
        return candidates[:limit]

    @staticmethod
    def _read_claude_session_meta(session_file: Path) -> dict[str, object]:
        """从 Claude 会话文件里提取 `sessionId` 和 `cwd`。"""
        try:
            with session_file.open("r", encoding="utf-8", errors="ignore") as file:
                for _ in range(24):
                    line = file.readline()
                    if not line:
                        break
                    payload = json.loads(line)
                    session_id = str(payload.get("sessionId", "") or "")
                    cwd = str(payload.get("cwd", "") or "")
                    if session_id and cwd:
                        return {"id": session_id, "cwd": cwd}
        except Exception:
            return {}
        return {}

    def _discover_latest_claude_session_id(self, started_after: float | None = None) -> str:
        """根据工作目录反查最近的 Claude session_id。"""
        best_path: Path | None = None
        best_mtime = -1.0
        work_dir = str(self.work_dir)
        for session_file in self._iter_recent_claude_session_files():
            stat = session_file.stat()
            if started_after is not None and stat.st_mtime + 2 < started_after:
                continue
            meta = self._read_claude_session_meta(session_file)
            if not meta:
                continue
            if not self._paths_equivalent(str(meta.get("cwd", "")), work_dir):
                continue
            session_id = str(meta.get("id", "") or "")
            if not session_id:
                continue
            if stat.st_mtime > best_mtime:
                best_mtime = stat.st_mtime
                best_path = session_file

        if best_path is None:
            return ""

        meta = self._read_claude_session_meta(best_path)
        return str(meta.get("id", "") or "")

    def _iter_recent_gemini_session_files(self, limit: int = 200) -> list[Path]:
        """按修改时间倒序枚举最近的 Gemini 会话文件。"""
        sessions_root = Path.home() / ".gemini" / "tmp"
        if not sessions_root.exists():
            return []

        candidates = [path for path in sessions_root.rglob("session-*.json") if path.is_file()]
        candidates.sort(key=lambda item: item.stat().st_mtime, reverse=True)
        return candidates[:limit]

    @staticmethod
    def _read_gemini_project_root(session_file: Path) -> str:
        """读取 Gemini 会话文件所属项目的根目录。"""
        try:
            project_root_file = session_file.parent.parent / ".project_root"
            if project_root_file.exists():
                return project_root_file.read_text(encoding="utf-8", errors="ignore").strip()
        except Exception:
            return ""
        return ""

    def _read_gemini_session_meta(self, session_file: Path) -> dict[str, object]:
        """从 Gemini 会话文件里提取 `sessionId` 和项目根目录。"""
        try:
            payload = json.loads(session_file.read_text(encoding="utf-8", errors="ignore"))
        except Exception:
            return {}

        session_id = str(payload.get("sessionId", "") or "")
        if not session_id:
            return {}

        cwd = self._read_gemini_project_root(session_file)
        if not cwd:
            projects_file = Path.home() / ".gemini" / "projects.json"
            try:
                projects_payload = json.loads(projects_file.read_text(encoding="utf-8"))
                projects = dict(projects_payload.get("projects", {}))
                current_slug = session_file.parent.parent.name
                for root_path, slug in projects.items():
                    if str(slug) == current_slug:
                        cwd = str(root_path)
                        break
            except Exception:
                cwd = ""

        return {
            "id": session_id,
            "cwd": cwd,
        }

    def _discover_latest_gemini_session_id(self, started_after: float | None = None) -> str:
        """根据工作目录反查最近的 Gemini session_id。"""
        best_path: Path | None = None
        best_mtime = -1.0
        work_dir = str(self.work_dir)
        for session_file in self._iter_recent_gemini_session_files():
            stat = session_file.stat()
            if started_after is not None and stat.st_mtime + 2 < started_after:
                continue
            meta = self._read_gemini_session_meta(session_file)
            if not meta:
                continue
            if not self._paths_equivalent(str(meta.get("cwd", "")), work_dir):
                continue
            session_id = str(meta.get("id", "") or "")
            if not session_id:
                continue
            if stat.st_mtime > best_mtime:
                best_mtime = stat.st_mtime
                best_path = session_file

        if best_path is None:
            return ""

        meta = self._read_gemini_session_meta(best_path)
        return str(meta.get("id", "") or "")

    def _discover_latest_agent_session_id(self, started_after: float | None = None) -> str:
        """根据当前后端分发到对应的 session_id 发现逻辑。"""
        if self.cli_config.backend == CliBackend.CODEX:
            return self._discover_latest_codex_session_id(started_after=started_after)
        if self.cli_config.backend == CliBackend.CLAUDE:
            return self._discover_latest_claude_session_id(started_after=started_after)
        if self.cli_config.backend == CliBackend.GEMINI:
            return self._discover_latest_gemini_session_id(started_after=started_after)
        return ""

    def _refresh_agent_session_id(self, started_after: float | None = None) -> str:
        """尝试更新并记录当前后端对应的 session_id。"""
        session_id = self._discover_latest_agent_session_id(started_after=started_after)
        if session_id:
            self.agent_session_id = session_id
            note_prefix = self.cli_config.backend.value
            self._write_state_file(
                note=f"{note_prefix}_session_identified",
                extra=self._build_session_id_payload(session_id),
            )
        return session_id

    def _build_session_id_payload(self, session_id: str | None = None) -> dict[str, object]:
        """按后端生成统一和特定字段并存的 session_id 载荷。"""
        current_session_id = str(session_id or self.agent_session_id or "")
        payload: dict[str, object] = {"agent_session_id": current_session_id}
        if self.cli_config.backend == CliBackend.CODEX:
            payload["codex_session_id"] = current_session_id
        elif self.cli_config.backend == CliBackend.CLAUDE:
            payload["claude_session_id"] = current_session_id
        elif self.cli_config.backend == CliBackend.GEMINI:
            payload["gemini_session_id"] = current_session_id
        return payload

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
            extra: dict[str, object] | None = None,
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
            "last_prompt": self.last_prompt,
            "last_reply": self.last_reply,
            "agent_session_id": self.agent_session_id,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "note": note,
        }
        payload.update(self._build_session_id_payload())
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
        self.prelaunch_completed = False
        self.agent_session_id = ""
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
        self.prelaunch_completed = False

    def attach_session(self) -> None:
        """
        把当前终端 attach 到目标 tmux session。

        这是人工接管或直接恢复到可交互界面的入口。
        """
        if not self.session_exists():
            raise RuntimeError(f"tmux session does not exist: {self.session_name}")
        subprocess.run(["tmux", "attach-session", "-t", self.session_name], check=True)

    def is_agent_process_running(self) -> bool:
        """判断当前 pane 前台是否仍然是目标 CLI 进程。"""
        if not self._adopt_existing_session():
            return False
        if self.pane_dead():
            return False
        snapshot = self.take_snapshot(tail_lines=120)
        if snapshot.current_command not in self.cli_config.expected_current_commands():
            return False
        if snapshot.current_command in SHELL_COMMANDS:
            return False
        if self.detector.looks_like_shell_prompt(snapshot.clean_output):
            return False
        return True

    def is_shell_process_running(self) -> bool:
        """判断当前 tmux pane 是否已经回到了普通 shell。"""
        if not self._adopt_existing_session():
            return False
        if self.pane_dead():
            return False
        return self.pane_current_command() in SHELL_COMMANDS

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
            "agent_session_id": self.agent_session_id,
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

    @staticmethod
    def _normalize_text(text: str) -> str:
        """压缩空白，便于在 tmux 捕获文本里做 prompt 锚点匹配。"""
        return re.sub(r"\s+", " ", str(text or "")).strip()

    def _prompt_anchor_candidates(self, message: str, max_lines: int = 3, max_chars: int = 80) -> list[str]:
        """为当前 prompt 提取少量稳定锚点，避免全量长文本在 TUI 中匹配失败。"""
        anchors: list[str] = []
        for line in str(message or "").splitlines():
            normalized = self._normalize_text(line)
            if not normalized:
                continue
            fragment = normalized[:max_chars].strip()
            if len(fragment) < 8:
                continue
            if fragment not in anchors:
                anchors.append(fragment)
            if len(anchors) >= max_lines:
                break
        if anchors:
            return anchors

        fallback = self._normalize_text(message)[:max_chars].strip()
        return [fallback] if fallback else []

    def _output_mentions_prompt(self, text: str, prompt: str) -> bool:
        """判断当前输出里是否已经出现本轮 prompt 的可识别锚点。"""
        normalized_output = self._normalize_text(text)
        if not normalized_output:
            return False
        return any(anchor in normalized_output for anchor in self._prompt_anchor_candidates(prompt))

    def _read_raw_log_size(self) -> int:
        """读取 raw log 当前字节大小，便于识别新一轮消息是否已落到终端流中。"""
        try:
            return self.raw_log_path.stat().st_size
        except FileNotFoundError:
            return 0

    def _read_raw_log_delta(self, start_offset: int) -> str:
        """读取从指定偏移量开始新增的 raw log 内容。"""
        if not self.raw_log_path.exists():
            return ""
        with self.raw_log_path.open("rb") as file:
            file.seek(max(0, int(start_offset)))
            return file.read().decode("utf-8", errors="replace")

    def _has_queued_submission_ui(self, text: str) -> bool:
        """识别 Codex 将消息暂存到下一次 tool call 的队列提示。"""
        return any(re.search(pattern, str(text or ""), re.IGNORECASE) for pattern in CODEX_QUEUED_MESSAGE_PATTERNS)

    def _extract_reply_if_ready(self, output: str) -> str:
        """尝试提取当前 pane 的最后答复；如果还不到稳定答复阶段则返回空串。"""
        try:
            return str(self.detector.extract_last_message(output) or "").strip()
        except Exception:
            return ""

    def wait_for_shell_ready(self, timeout_sec: float = 12.0, require_work_dir: bool = True) -> None:
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
            path_ready = self.pane_current_path() == str(self.work_dir) if require_work_dir else True
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

    def _wait_for_agent_ready(
            self,
            *,
            started_after: float | None,
            timeout_sec: float,
            action_label: str,
            trust_note: str,
            update_note: str,
            model_note: str,
            ready_note: str,
            ready_extra: Mapping[str, object] | None = None,
    ) -> SessionSnapshot:
        """
        等待当前 pane 中的 CLI 真正进入可交互状态。

        这里统一处理启动/恢复阶段可能出现的拦截式提示，例如工作区信任确认或模型升级选择菜单。
        """
        agent_name = self.cli_config.display_name()
        deadline = time.monotonic() + timeout_sec
        last_trust_ack = 0.0
        last_update_ack = 0.0
        last_model_ack = 0.0

        while time.monotonic() < deadline:
            if not self.target_exists():
                raise RuntimeError(f"tmux pane exited while {agent_name} was {action_label}")

            snapshot = self.take_snapshot(tail_lines=220)
            if snapshot.pane_dead:
                raise RuntimeError(f"tmux pane died while {agent_name} was {action_label}:\n{snapshot.raw_output}")

            now = time.monotonic()
            if self.detector.has_trust_prompt(snapshot.clean_output):
                if now - last_trust_ack > 1.0:
                    self.send_special_key("Enter")
                    last_trust_ack = now
                    self._append_clean_log_snapshot(trust_note, snapshot=snapshot)
                time.sleep(0.5)
                continue

            if self.detector.has_update_prompt(snapshot.clean_output):
                if now - last_update_ack > 1.0:
                    self.send_special_key("Down")
                    time.sleep(0.1)
                    self.send_special_key("Down")
                    time.sleep(0.1)
                    self.send_special_key("Enter")
                    last_update_ack = now
                    self._append_clean_log_snapshot(update_note, snapshot=snapshot)
                time.sleep(0.5)
                continue

            if self.detector.has_model_selection_prompt(snapshot.clean_output):
                if now - last_model_ack > 1.0:
                    self.send_special_key("Down")
                    time.sleep(0.1)
                    self.send_special_key("Enter")
                    last_model_ack = now
                    self._append_clean_log_snapshot(model_note, snapshot=snapshot)
                time.sleep(0.5)
                continue

            if snapshot.confirmed_status in {TerminalStatus.IDLE, TerminalStatus.COMPLETED}:
                if (
                    snapshot.current_command in self.cli_config.expected_current_commands()
                    or self.detector.has_welcome_banner(snapshot.clean_output)
                ):
                    if started_after is None:
                        self._refresh_agent_session_id()
                    else:
                        self._refresh_agent_session_id(started_after=started_after)
                    self._append_clean_log_snapshot(ready_note, snapshot=snapshot)
                    if ready_extra:
                        self._write_state_file(snapshot=snapshot, note=ready_note, extra=dict(ready_extra))
                    else:
                        self._write_state_file(snapshot=snapshot, note=ready_note)
                    self.agent_initialized = True
                    return snapshot

            if snapshot.current_command in SHELL_COMMANDS and self.detector.looks_like_shell_prompt(
                    snapshot.clean_output):
                raise RuntimeError(f"{agent_name} exited back to shell while {action_label}.\n{snapshot.raw_output}")

            time.sleep(0.5)

        raise RuntimeError(f"Timed out waiting for {agent_name} while {action_label}.\n{self.capture(240)}")

    def prepare_shell(self, recreate: bool = False, rerun_prelaunch: bool = False) -> SessionSnapshot:
        """
        准备一个可用的 tmux shell 环境。

        这是“接管或创建 tmux + 等 shell 就绪 + 执行前置脚本 + 做 warmup”的组合封装。
        """
        adopted_existing = False
        if not recreate:
            adopted_existing = self._adopt_existing_session()
        if recreate or not self.target_exists():
            self.create_session()
            adopted_existing = False
        self.wait_for_shell_ready(require_work_dir=not adopted_existing)
        if adopted_existing and self.pane_current_path() != str(self.work_dir):
            self._run_shell_command_and_wait(f"cd {shlex.quote(str(self.work_dir))}")
        self._run_prelaunch_hooks(force=rerun_prelaunch or recreate)
        self.warmup_shell()
        return self.take_snapshot(tail_lines=120)

    def launch_agent(self, timeout_sec: float = 60.0) -> SessionSnapshot:
        """
        启动目标 CLI，并等待它进入可交互状态。

        启动期间如果出现工作区信任提示，会自动确认默认项；如果 CLI 直接掉回 shell，会明确抛错。
        """
        launch_started_at = time.time()
        self.send_text(self.cli_config.build_command(), enter_count=1)
        return self._wait_for_agent_ready(
            started_after=launch_started_at,
            timeout_sec=timeout_sec,
            action_label="starting",
            trust_note="trust_prompt_ack",
            update_note="update_prompt_skip",
            model_note="model_selection_prompt_ack",
            ready_note="agent_ready",
        )

    def ensure_agent_ready(self, recreate_session: bool = False) -> SessionSnapshot:
        """
        确保当前 runtime 已经拥有一个可交互的 CLI 会话。

        如果 session 丢失、pane 回到 shell，或者调用方明确要求重建，就自动重走启动链路。
        """
        if not recreate_session:
            self._adopt_existing_session()

        if recreate_session or not self.target_exists():
            self.shell_initialized = False
            self.agent_initialized = False
            self.prelaunch_completed = False

        if not self.shell_initialized:
            self.prepare_shell(recreate=recreate_session or not self.target_exists())

        if self.agent_initialized and self.target_exists():
            snapshot = self.take_snapshot(tail_lines=220)
            if (
                snapshot.current_command in self.cli_config.expected_current_commands()
                and snapshot.confirmed_status in {TerminalStatus.IDLE, TerminalStatus.COMPLETED}
                and not self.detector.has_trust_prompt(snapshot.clean_output)
                and not self.detector.has_model_selection_prompt(snapshot.clean_output)
            ):
                return snapshot
            self.agent_initialized = False

        if self.target_exists():
            snapshot = self.take_snapshot(tail_lines=220)
            if snapshot.current_command in self.cli_config.expected_current_commands():
                return self._wait_for_agent_ready(
                    started_after=None,
                    timeout_sec=60.0,
                    action_label="becoming ready",
                    trust_note="ready_trust_prompt_ack",
                    update_note="ready_update_prompt_skip",
                    model_note="ready_model_selection_prompt_ack",
                    ready_note="agent_ready",
                )

        return self.launch_agent()

    def restart_agent(self) -> SessionSnapshot:
        """强制重建 tmux session 并重新启动目标 CLI。"""
        self.kill_session()
        self.shell_initialized = False
        self.agent_initialized = False
        return self.ensure_agent_ready(recreate_session=True)

    def _resume_agent_in_shell(self, prompt: str | None = None, timeout_sec: float = 60.0) -> SessionSnapshot:
        """
        在已经准备好的 shell 中恢复当前 CLI 会话。

        这一步假设 tmux 和 shell 都已就绪，只负责执行对应后端的 resume 命令并等待 TUI 回到可交互状态。
        """
        if not self.agent_session_id:
            self._refresh_agent_session_id()
        if not self.agent_session_id:
            raise RuntimeError(f"No recorded {self.cli_config.display_name()} session_id found for resume")

        resume_started_at = time.time()
        resume_command = self.cli_config.build_resume_command(
            session_id=self.agent_session_id,
            work_dir=self.work_dir,
            prompt=prompt,
        )
        self.send_text(resume_command, enter_count=1)
        return self._wait_for_agent_ready(
            started_after=resume_started_at,
            timeout_sec=timeout_sec,
            action_label="resuming",
            trust_note="resume_trust_prompt_ack",
            update_note="resume_update_prompt_skip",
            model_note="resume_model_selection_prompt_ack",
            ready_note="agent_resumed",
            ready_extra={
                "resume_action": "resume_in_shell",
                **self._build_session_id_payload(),
            },
        )

    def resume_cli_session(
        self,
        prompt: str | None = None,
        attach_if_running: bool = True,
        attach_after_resume: bool = False,
        timeout_sec: float = 60.0,
    ) -> ResumeResult:
        """
        恢复一个已存在的 CLI 会话。

        恢复策略分三种：
        1. tmux 和 CLI 都还活着：直接 attach 回去，或者返回当前状态。
        2. tmux 还在但 CLI 已退出：回到工作目录，重新执行前置脚本，然后执行恢复命令。
        3. tmux 也没了：创建新 tmux，进入工作目录，执行前置脚本，然后执行恢复命令。
        """
        if not self.agent_session_id:
            self._refresh_agent_session_id()
        if not self.agent_session_id:
            raise RuntimeError(f"No recorded {self.cli_config.display_name()} session_id found for resume")

        action = ""
        attached = False

        if self.is_agent_process_running():
            self.agent_initialized = True
            action = "attach_existing_agent"
            self._write_state_file(
                note=action,
                extra={
                    **self._build_session_id_payload(),
                },
            )
            if attach_if_running:
                self.attach_session()
                attached = True
            snapshot = self.take_snapshot(tail_lines=220)
        else:
            if self.session_exists() and self.is_shell_process_running():
                self._adopt_existing_session()
                self.prepare_shell(recreate=False, rerun_prelaunch=True)
                snapshot = self._resume_agent_in_shell(prompt=prompt, timeout_sec=timeout_sec)
                action = "resume_in_existing_tmux"
            else:
                self.prepare_shell(recreate=True, rerun_prelaunch=True)
                snapshot = self._resume_agent_in_shell(prompt=prompt, timeout_sec=timeout_sec)
                action = "recreate_tmux_and_resume"

            if attach_after_resume:
                self.attach_session()
                attached = True

        self._write_state_file(
            snapshot=snapshot,
            note="resume_completed",
            extra={
                "resume_action": action,
                "attached": str(attached).lower(),
                **self._build_session_id_payload(),
            },
        )
        return ResumeResult(
            backend=self.cli_config.backend.value,
            terminal_id=self.terminal_id,
            session_name=self.session_name,
            pane_id=self.pane_id,
            agent_session_id=self.agent_session_id,
            action=action,
            attached=attached,
            log_path=self.log_path,
            raw_log_path=self.raw_log_path,
            state_path=self.state_path,
        )

    def resume_codex_session(
        self,
        prompt: str | None = None,
        attach_if_running: bool = True,
        attach_after_resume: bool = False,
        timeout_sec: float = 60.0,
    ) -> ResumeResult:
        """这是保留给旧调用方的兼容方法，内部会转到通用恢复逻辑。"""
        return self.resume_cli_session(
            prompt=prompt,
            attach_if_running=attach_if_running,
            attach_after_resume=attach_after_resume,
            timeout_sec=timeout_sec,
        )

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
        baseline_raw_offset = self._read_raw_log_size()
        self.last_prompt = message
        self.send_text(message, enter_count=self.cli_config.submit_enter_count())
        deadline = time.monotonic() + timeout_sec
        extra_enter_sent = False
        submit_started_at = time.monotonic()
        submission_observed = False

        while time.monotonic() < deadline:
            snapshot = self.take_snapshot(tail_lines=320)
            if snapshot.pane_dead:
                raise RuntimeError(f"tmux pane died after sending message:\n{snapshot.raw_output}")

            raw_delta = self._read_raw_log_delta(baseline_raw_offset)
            prompt_visible = self._output_mentions_prompt(snapshot.clean_output, message) or self._output_mentions_prompt(
                raw_delta,
                message,
            )
            queued_submission = self._has_queued_submission_ui(snapshot.clean_output) or self._has_queued_submission_ui(
                raw_delta,
            )
            submission_observed = submission_observed or prompt_visible or queued_submission

            if snapshot.detected_status == TerminalStatus.PROCESSING and (submission_observed or raw_delta.strip()):
                self._append_clean_log_snapshot("message_processing", snapshot=snapshot)
                self._write_state_file(snapshot=snapshot, note="message_processing")
                return snapshot

            if snapshot.confirmed_status == TerminalStatus.COMPLETED:
                if submission_observed and self._extract_reply_if_ready(snapshot.raw_output):
                    self._append_clean_log_snapshot("message_terminal_state", snapshot=snapshot)
                    self._write_state_file(snapshot=snapshot, note="message_terminal_state")
                    return snapshot

            if snapshot.confirmed_status in {TerminalStatus.WAITING_USER_ANSWER, TerminalStatus.ERROR}:
                if submission_observed or raw_delta.strip():
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

            if snapshot.confirmed_status in {
                TerminalStatus.COMPLETED,
                TerminalStatus.WAITING_USER_ANSWER,
                TerminalStatus.ERROR,
            } and not submission_observed:
                time.sleep(0.5)
                continue

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
        completed_candidate_key: tuple[str, int] | None = None
        completed_candidate_since = 0.0
        reply_settle_sec = max(1.0, self.state_minimum_sec)

        while time.monotonic() < deadline:
            snapshot = self.take_snapshot(tail_lines=500)
            last_snapshot = snapshot
            if snapshot.pane_dead:
                raise RuntimeError(f"tmux pane died while waiting for reply:\n{snapshot.raw_output}")

            if snapshot.detected_status == TerminalStatus.PROCESSING or snapshot.confirmed_status == TerminalStatus.PROCESSING:
                seen_processing = True
                completed_candidate_key = None
                completed_candidate_since = 0.0
                time.sleep(0.5)
                continue

            if snapshot.confirmed_status in {TerminalStatus.WAITING_USER_ANSWER, TerminalStatus.ERROR}:
                self._append_clean_log_snapshot("reply_terminal_state", snapshot=snapshot)
                self._write_state_file(snapshot=snapshot, note="reply_terminal_state")
                return snapshot

            if snapshot.confirmed_status == TerminalStatus.COMPLETED:
                reply = self._extract_reply_if_ready(snapshot.raw_output)
                if reply and (seen_processing or snapshot.detected_status == TerminalStatus.COMPLETED):
                    candidate_key = (reply, self._read_raw_log_size())
                    if candidate_key != completed_candidate_key:
                        completed_candidate_key = candidate_key
                        completed_candidate_since = snapshot.timestamp
                    elif snapshot.timestamp - completed_candidate_since >= reply_settle_sec:
                        self._append_clean_log_snapshot("reply_terminal_state", snapshot=snapshot)
                        self._write_state_file(snapshot=snapshot, note="reply_terminal_state")
                        return snapshot
                    time.sleep(0.5)
                    continue

            completed_candidate_key = None
            completed_candidate_since = 0.0

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
            self._refresh_agent_session_id()
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
                agent_session_id=self.agent_session_id,
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
        self._refresh_agent_session_id()
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
