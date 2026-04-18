# -*- encoding: utf-8 -*-
"""
@File: T02_tmux_agents.py
@Modify Time: 2026/4/12
@Author: Kevin-Chen
@Descriptions: tmux + 多厂商 coding agent 的长会话运行时
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shlex
import subprocess
import threading
import time
import uuid
import weakref
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Sequence
from contextlib import contextmanager
from urllib.parse import urlparse
from T04_common_prompt import TASK_DONE_MARKER, build_task_completion_runtime_prompt
from U01_common_config import SYSTEM_PYTHON_PATH

DEFAULT_RUNTIME_ROOT = Path(__file__).resolve().parent / ".agent_init_runtime"
DEFAULT_COMMAND_TIMEOUT_SEC = 60 * 20
DEFAULT_PROXY_HOST = "127.0.0.1"
TERMINAL_ACTIVITY_IDLE_WINDOW_SEC = 1.5
TASK_COMPLETION_NUDGE_IDLE_SEC = 60.0
TASK_COMPLETION_NUDGE_GRACE_SEC = 25.0
TASK_COMPLETION_NUDGE_MAX_COUNT = 2
WORKER_DEATH_ERROR_MARKERS = (
    "tmux pane died",
    "tmux pane exited",
    "agent exited back to shell",
    "missing_session",
    "pane_dead",
)
PROVIDER_AUTH_ERROR_MARKERS = (
    "api error: 401",
    "401 invalid access token",
    "401 unauthorized",
    "invalid access token",
    "token expired",
    "access token expired",
    "token has expired",
)
AGENT_READY_TIMEOUT_ERROR_MARKERS = (
    "timed out waiting for agent ready",
)
TIMEOUT_EXIT_CODE = -1
GENERIC_ERROR_EXIT_CODE = 1
PROXY_ENV_KEYS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
)
SHELL_COMMANDS = {"bash", "fish", "sh", "zsh"}
CODEX_TRUST_PROMPT_PATTERNS = (
    r"allow Codex to work in this folder",
    r"Do you trust the contents of this directory\?",
)
CODEX_READY_PATTERNS = (
    r"^\s*(?:❯|›|codex>)\s*$",
    r"^\s*[›❯]\s+\S.*$",
    r"^\s*[›❯]\s+.*@filename.*$",
    r"^\s*[›❯]\s+.*@path/to/file.*$",
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
CODEX_STARTING_PATTERNS = (
    r"Starting MCP servers",
    r"MCP servers \(\d+/\d+\)",
)
CODEX_PROCESSING_PATTERNS = (
    r"\besc to interrupt\b",
)
GEMINI_READY_PATTERNS = (
    r"Type your message",
    r"@path/to/file",
)
GEMINI_TRUST_PROMPT_PATTERNS = (
    r"Do you trust the files in this folder\?",
    r"Trust folder",
    r"Trust parent folder",
    r"Don't trust",
)
GEMINI_NOT_READY_PATTERNS = (
    r"Waiting for authentication",
    r"Press Esc or Ctrl\+C to cancel",
)
GEMINI_PROCESSING_PATTERNS = (
    r"Working…",
    r"Working\.\.\.",
    r"Thinking…",
    r"Thinking\.\.\.",
)
GEMINI_INPUT_BOX_PATTERNS = (
    r"Type your message or @path/to/file",
    r"^│\s*>",
    r"^>$",
)
QWEN_READY_PATTERNS = (
    r"输入您的消息",
    r"@ 文件路径",
)
QWEN_INPUT_BOX_PATTERNS = (
    r"输入您的消息或\s*@\s*文件路径",
    r"^输入您的消息$",
)
KIMI_NOT_READY_PATTERNS = (
    r'LLM not set, send "/login" to login',
    r"Model:\s*not set",
    r"send /login to login",
)
KIMI_UPDATE_PROMPT_PATTERNS = (
    r"kimi-cli update available",
    r"\[Enter\]\s+Upgrade now",
    r"\[q\]\s+Not now",
    r"\[s\]\s+Skip reminders",
)
GEMINI_FOOTER_PATTERNS = (
    r"^\?\s+for shortcuts$",
    r"^YOLO\b",
    r"^[▀▄]+$",
    r"^workspace \(/directory\)",
)
AUDIT_STRUCTURED_PREFIXES = (
    "- verdict:",
    "- file_role:",
    "- finding:",
    "- duplication:",
    "- boundary_conflict:",
    "- missing:",
    "- recommendation:",
    "- top_priority:",
)
RUNTIME_NOISE_PATTERNS = (
    rf"^{re.escape(TASK_DONE_MARKER)}$",
    r"Thinking\.\.\.",
    r"^\?\s+for shortcuts$",
    r"^YOLO\b",
    r"^YOLO 模式",
    r"Type your message(?:\s+or\s+@path/to/file)?",
    r"输入您的消息(?:\s*或\s*@\s*文件路径)?",
    r"@path/to/file",
    r"^workspace \(/directory\)",
    r"^[─━]{5,}$",
    r"^[▀▄]+$",
    r"^❯$",
    r"^[·✳✽✢]?\s*[A-Za-z]+…(?:\s*\(.*\))?$",
    r"^\S+…(?:\s*\(.*\))?$",
    r"^[A-Za-z]+…\d*$",
    r"^✻\s*.+ for .*$",
    r"^✳\s*Metamorphosing.*$",
    r"^Metamorphosing….*$",
    r"^[·✳✽]?\s*(?:Precipitating|Metamorphosing|Moseying)….*$",
    r"^✻\s*(?:Baked|Cogitated|Churned|Brewed) for.*$",
    r"^\(·oo·\).*$",
    r"^⏵⏵.*$",
    r"^✗\s*Auto-update.*$",
    r"^(?:~|/)\S+\s+.+$",
    r"^(?:~|/).+\s{2,}.+$",
    r"^(?:gemini|claude|codex|qwen|kimi)(?:[-_.a-z0-9]+)?$",
)

_LIVE_WORKERS: "weakref.WeakSet[TmuxBatchWorker]" = weakref.WeakSet()
_LIVE_WORKERS_LOCK = threading.RLock()
_RESERVED_SESSION_NAMES: set[str] = set()
_RESERVED_SESSION_NAMES_LOCK = threading.RLock()
SESSION_CONSTELLATION_NAMES: tuple[str, ...] = (
    "角木蛟",
    "亢金龙",
    "氐土貉",
    "房日兔",
    "心月狐",
    "尾火虎",
    "箕水豹",
    "斗木獬",
    "牛金牛",
    "女土蝠",
    "虚日鼠",
    "危月燕",
    "室火猪",
    "壁水貐",
    "奎木狼",
    "娄金狗",
    "胃土雉",
    "昴日鸡",
    "毕月乌",
    "觜火猴",
    "参水猿",
    "井木犴",
    "鬼金羊",
    "柳土獐",
    "星日马",
    "张月鹿",
    "翼火蛇",
    "轸水蚓",
    "天魁星",
    "天罡星",
    "天机星",
    "天闲星",
    "天勇星",
    "天雄星",
    "天猛星",
    "天威星",
    "天英星",
    "天贵星",
    "天富星",
    "天满星",
    "天孤星",
    "天伤星",
    "天立星",
    "天捷星",
    "天暗星",
    "天佑星",
    "天空星",
    "天速星",
    "天异星",
    "天杀星",
    "天微星",
    "天究星",
    "天退星",
    "天寿星",
    "天剑星",
    "天平星",
    "天罪星",
    "天损星",
    "天败星",
    "天牢星",
    "天慧星",
    "天暴星",
    "天哭星",
    "天巧星",
    "地魁星",
    "地煞星",
    "地勇星",
    "地杰星",
    "地雄星",
    "地威星",
    "地英星",
    "地奇星",
    "地猛星",
    "地文星",
    "地正星",
    "地辟星",
    "地阖星",
    "地强星",
    "地暗星",
    "地轴星",
    "地会星",
    "地佐星",
    "地佑星",
    "地灵星",
    "地兽星",
    "地微星",
    "地慧星",
    "地暴星",
    "地默星",
    "地猖星",
    "地狂星",
    "地飞星",
    "地走星",
    "地巧星",
    "地明星",
    "地进星",
    "地退星",
    "地满星",
    "地遂星",
    "地周星",
    "地隐星",
    "地异星",
    "地理星",
    "地俊星",
    "地乐星",
    "地捷星",
    "地速星",
    "地镇星",
)
_SESSION_ALLOWED_CHARS_RE = re.compile(r"[^A-Za-z0-9\u4e00-\u9fff-]+")
_SESSION_ROLE_REVIEWER_RE = re.compile(r"^requirements-review-r(\d+)$")


class Vendor(str, Enum):
    CODEX = "codex"
    CLAUDE = "claude"
    GEMINI = "gemini"
    QWEN = "qwen"
    KIMI = "kimi"


class WorkerStatus(str, Enum):
    READY = "ready"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class ProviderPhase(str, Enum):
    SHELL = "shell"
    BOOTING = "booting"
    AUTH_PROMPT = "auth_prompt"
    UPDATE_PROMPT = "update_prompt"
    WAITING_INPUT = "waiting_input"
    IDLE_READY = "idle_ready"
    PROCESSING = "processing"
    COMPLETED_RESPONSE = "completed_response"
    RECOVERING = "recovering"
    ERROR = "error"
    UNKNOWN = "unknown"


class WrapperState(str, Enum):
    READY = "READY"
    NOT_READY = "NOT_READY"


def _register_live_worker(worker: "TmuxBatchWorker") -> None:
    with _LIVE_WORKERS_LOCK:
        _LIVE_WORKERS.add(worker)


def list_registered_tmux_workers() -> list["TmuxBatchWorker"]:
    with _LIVE_WORKERS_LOCK:
        return list(_LIVE_WORKERS)


def cleanup_registered_tmux_workers(*, reason: str = "process_exit") -> list[str]:
    cleaned_sessions: list[str] = []
    for worker in list_registered_tmux_workers():
        try:
            if not worker.session_exists():
                continue
            session_name = worker.request_kill()
            if session_name:
                cleaned_sessions.append(session_name)
                worker._log_event("process_cleanup_kill", reason=reason, session_name=session_name)
        except Exception:
            continue
    return sorted(set(cleaned_sessions))


@dataclass(frozen=True)
class WorkerObservation:
    visible_text: str
    raw_log_delta: str
    raw_log_tail: str
    current_command: str
    current_path: str
    pane_dead: bool
    session_exists: bool
    log_mtime: float
    observed_at: str


@dataclass(frozen=True)
class WorkerHealthSnapshot:
    session_exists: bool
    health_status: str
    health_note: str
    provider_phase: str
    last_heartbeat_at: str
    last_log_offset: int
    current_command: str
    current_path: str
    pane_id: str
    session_name: str


@dataclass(frozen=True)
class TurnFileResult:
    status_path: str
    payload: dict[str, object]
    artifact_paths: dict[str, str]
    artifact_hashes: dict[str, str]
    validated_at: str


@dataclass(frozen=True)
class TurnFileContract:
    turn_id: str
    phase: str
    status_path: Path
    validator: Callable[[Path], TurnFileResult]
    quiet_window_sec: float = 1.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "status_path", Path(self.status_path).expanduser().resolve())


@dataclass(frozen=True)
class TaskResultFile:
    result_path: str
    payload: dict[str, object]
    artifact_paths: dict[str, str]
    artifact_hashes: dict[str, str]
    validated_at: str


@dataclass(frozen=True)
class TaskResultContract:
    turn_id: str
    phase: str
    task_kind: str
    mode: str
    expected_statuses: tuple[str, ...]
    stage_name: str = ""
    turn_status_path: Path | None = None
    stage_status_path: Path | None = None
    required_artifacts: dict[str, Path] = field(default_factory=dict)
    optional_artifacts: dict[str, Path] = field(default_factory=dict)
    terminal_status_tokens: dict[str, tuple[str, ...]] = field(default_factory=dict)
    terminal_status_summaries: dict[str, str] = field(default_factory=dict)
    artifact_rules: dict[str, object] = field(default_factory=dict)
    retry_policy: dict[str, object] = field(default_factory=dict)
    resume_policy: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "turn_status_path", _resolve_optional_path(self.turn_status_path))
        object.__setattr__(self, "stage_status_path", _resolve_optional_path(self.stage_status_path))
        object.__setattr__(
            self,
            "required_artifacts",
            {key: Path(value).expanduser().resolve() for key, value in self.required_artifacts.items()},
        )
        object.__setattr__(
            self,
            "optional_artifacts",
            {key: Path(value).expanduser().resolve() for key, value in self.optional_artifacts.items()},
        )
        object.__setattr__(self, "expected_statuses", tuple(str(item).strip() for item in self.expected_statuses if str(item).strip()))
        object.__setattr__(
            self,
            "terminal_status_tokens",
            {
                str(status).strip(): tuple(str(token).strip() for token in tokens if str(token).strip())
                for status, tokens in self.terminal_status_tokens.items()
                if str(status).strip()
            },
        )
        object.__setattr__(
            self,
            "terminal_status_summaries",
            {
                str(status).strip(): str(summary).strip()
                for status, summary in self.terminal_status_summaries.items()
                if str(status).strip() and str(summary).strip()
            },
        )


class TmuxBackend:
    def run(
            self,
            *args: str,
            input_text: str | None = None,
            timeout_sec: float = 10.0,
            check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["tmux", *args],
            check=check,
            text=True,
            capture_output=True,
            input=input_text,
            timeout=timeout_sec,
        )

    def has_session(self, session_name: str) -> bool:
        result = self.run("has-session", "-t", session_name, check=False)
        return result.returncode == 0

    def list_sessions(self) -> list[str]:
        result = self.run("list-sessions", "-F", "#S", check=False)
        if result.returncode != 0:
            return []
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    def create_session(self, session_name: str, work_dir: Path, command: str) -> str:
        result = self.run(
            "new-session",
            "-d",
            "-P",
            "-F",
            "#{pane_id}",
            "-s",
            session_name,
            "-c",
            str(work_dir),
            command,
        )
        return result.stdout.strip()

    def kill_session(self, session_name: str) -> None:
        self.run("kill-session", "-t", session_name)

    def attach_session(self, session_name: str) -> None:
        subprocess.run(["tmux", "attach-session", "-t", session_name], check=True)

    def detach_session(self, session_name: str) -> None:
        self.run("detach-client", "-s", session_name)

    def target_exists(self, target_name: str) -> bool:
        result = self.run("list-panes", "-t", target_name, check=False)
        return result.returncode == 0

    def display_message(self, target: str, expression: str) -> str:
        return self.run("display-message", "-p", "-t", target, expression).stdout.strip()

    def capture_visible(self, target: str, *, tail_lines: int = 500) -> str:
        return self.run(
            "capture-pane",
            "-J",
            "-p",
            "-t",
            target,
            "-S",
            f"-{tail_lines}",
            timeout_sec=15.0,
        ).stdout

    def pipe_log(self, target: str, raw_log_path: Path) -> None:
        command = f"cat >> {shlex.quote(str(raw_log_path))}"
        self.run("pipe-pane", "-t", target, "-o", command)

    def send_key(self, target: str, key: str) -> None:
        self.run("send-keys", "-t", target, key)

    def send_text(self, target: str, text: str, *, submit_count: int) -> None:
        buffer_name = f"acx_{uuid.uuid4().hex[:8]}"
        try:
            self.run("load-buffer", "-b", buffer_name, "-", input_text=text)
            self.run("paste-buffer", "-p", "-b", buffer_name, "-t", target)
            time.sleep(0.3)
            for index in range(submit_count):
                if index > 0:
                    time.sleep(0.5)
                self.run("send-keys", "-t", target, "Enter")
        finally:
            subprocess.run(["tmux", "delete-buffer", "-b", buffer_name], check=False, capture_output=True)

    def tail_raw_log(
            self,
            raw_log_path: str | Path,
            *,
            last_offset: int = 0,
            tail_bytes: int = 24000,
    ) -> tuple[str, str, int, float]:
        path = Path(raw_log_path)
        if not path.exists():
            return "", "", 0, 0.0
        size = path.stat().st_size
        start = min(max(last_offset, 0), size)
        with path.open("rb") as file:
            file.seek(start)
            delta_bytes = file.read()
            tail_start = max(size - tail_bytes, 0)
            file.seek(tail_start)
            tail_data = file.read()
        mtime = path.stat().st_mtime
        return (
            delta_bytes.decode("utf-8", errors="replace"),
            tail_data.decode("utf-8", errors="replace"),
            size,
            mtime,
        )


class LaunchCoordinator:
    _vendor_locks: dict[str, threading.Lock] = {}
    _stagger_by_vendor: dict[str, float] = {}
    _guard = threading.Lock()
    base_stagger_sec = 2.0
    max_stagger_sec = 10.0

    def __init__(self, runtime_root: str | Path) -> None:
        self.runtime_root = Path(runtime_root).expanduser().resolve()
        self.lock_root = self.runtime_root / "_locks"
        self.lock_root.mkdir(parents=True, exist_ok=True)

    @classmethod
    def _get_vendor_lock(cls, vendor: Vendor) -> threading.Lock:
        with cls._guard:
            return cls._vendor_locks.setdefault(vendor.value, threading.Lock())

    @classmethod
    def current_stagger(cls, vendor: Vendor) -> float:
        return cls._stagger_by_vendor.get(vendor.value, cls.base_stagger_sec)

    @classmethod
    def record_launch_result(cls, vendor: Vendor, *, success: bool) -> None:
        if success:
            cls._stagger_by_vendor[vendor.value] = cls.base_stagger_sec
            return
        previous = cls._stagger_by_vendor.get(vendor.value, cls.base_stagger_sec)
        cls._stagger_by_vendor[vendor.value] = min(previous * 2, cls.max_stagger_sec)

    @contextmanager
    def startup_slot(self, vendor: Vendor):
        vendor_lock = self._get_vendor_lock(vendor)
        lock_path = self.lock_root / f"launch_{vendor.value}.lock"
        with vendor_lock:
            with lock_path.open("a+", encoding="utf-8") as lock_file:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                try:
                    time.sleep(self.current_stagger(vendor))
                    yield
                finally:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


class HealthSupervisor:
    def __init__(
            self,
            refresh_callback: Callable[[], None],
            *,
            interval_sec: float = 2.0,
            thread_name: str = "tmux-health",
    ) -> None:
        self.refresh_callback = refresh_callback
        self.interval_sec = interval_sec
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run, name=thread_name, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._thread.join(timeout=2.0)

    def _run(self) -> None:
        while not self._stop_event.wait(self.interval_sec):
            try:
                self.refresh_callback()
            except Exception:
                continue


class TmuxRuntimeController:
    def __init__(self, backend: TmuxBackend | None = None) -> None:
        self.backend = backend or TmuxBackend()

    def session_exists(self, session_name: str) -> bool:
        return bool(session_name) and self.backend.has_session(session_name)

    def list_sessions(self) -> list[str]:
        return self.backend.list_sessions()

    def attach_session(self, session_name: str) -> None:
        if not self.session_exists(session_name):
            raise RuntimeError(f"tmux 会话尚未创建: {session_name}")
        self.backend.attach_session(session_name)

    def detach_session(self, session_name: str) -> str:
        if not session_name:
            raise RuntimeError("tmux 会话尚未创建")
        self.backend.detach_session(session_name)
        return session_name

    def kill_session(self, session_name: str, *, missing_ok: bool = True) -> str:
        if not session_name:
            if missing_ok:
                return ""
            raise RuntimeError("tmux 会话尚未创建")
        if not self.session_exists(session_name):
            if missing_ok:
                return session_name
            raise RuntimeError(f"tmux 会话尚未创建: {session_name}")
        self.backend.kill_session(session_name)
        return session_name

    def read_transcript_tail(self, transcript_path: str | Path, *, max_lines: int = 60) -> str:
        return read_text_tail(transcript_path, max_lines=max_lines)


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def is_worker_death_error(error: BaseException | str) -> bool:
    message = str(error or "").strip().lower()
    if not message:
        return False
    return any(marker in message for marker in WORKER_DEATH_ERROR_MARKERS)


def is_provider_auth_error(error: BaseException | str) -> bool:
    message = str(error or "").strip().lower()
    if not message:
        return False
    return any(marker in message for marker in PROVIDER_AUTH_ERROR_MARKERS)


def is_agent_ready_timeout_error(error: BaseException | str) -> bool:
    message = str(error or "").strip().lower()
    if not message:
        return False
    return any(marker in message for marker in AGENT_READY_TIMEOUT_ERROR_MARKERS)


def try_resume_worker(worker: "TmuxBatchWorker", *, timeout_sec: float = 60.0) -> bool:
    try:
        worker.request_restart()
        worker.ensure_agent_ready(timeout_sec=timeout_sec)
        return True
    except Exception:
        return False


def _slugify(text: str, max_len: int = 40) -> str:
    raw = str(text or "").strip()
    value = re.sub(r"[^a-zA-Z0-9]+", "-", raw).strip("-").lower()
    if not value:
        if raw:
            value = f"worker-{hashlib.sha1(raw.encode('utf-8')).hexdigest()[:8]}"
        else:
            value = "worker"
    return value[:max_len].strip("-") or "worker"


def _sanitize_session_fragment(text: str, *, fallback: str) -> str:
    value = str(text or "").strip()
    if not value:
        value = fallback
    value = re.sub(r"[\s/\\]+", "-", value)
    value = _SESSION_ALLOWED_CHARS_RE.sub("-", value)
    value = re.sub(r"-{2,}", "-", value).strip("-")
    return value or fallback


def _sanitize_task_runtime_fragment(text: str, *, fallback: str, max_len: int) -> str:
    value = _sanitize_session_fragment(text, fallback=fallback)
    value = value[:max_len].strip("-")
    return value or fallback


def _worker_role_key(worker_id: str, work_dir: str | Path) -> str:
    worker_key = str(worker_id or "").strip().lower()
    if not worker_key:
        return "generic-executor"
    if worker_key == "requirements-analyst":
        return worker_key
    if worker_key == "requirements-notion-reader":
        return worker_key
    if worker_key == "requirements-review-analyst":
        return worker_key
    if _SESSION_ROLE_REVIEWER_RE.fullmatch(worker_key):
        return worker_key
    work_dir_path = Path(work_dir).expanduser().resolve()
    work_dir_name = work_dir_path.name.strip().lower()
    work_dir_slug = _slugify(work_dir_name, max_len=48)
    if worker_key in {"project", work_dir_name, work_dir_slug}:
        return "routing-initializer"
    return "generic-executor"


def _worker_role_label(role_key: str) -> str:
    if role_key == "requirements-analyst":
        return "分析师"
    if role_key == "requirements-notion-reader":
        return "需求录入员"
    if role_key == "requirements-review-analyst":
        return "需求分析师"
    reviewer_match = _SESSION_ROLE_REVIEWER_RE.fullmatch(role_key)
    if reviewer_match:
        return "审核器"
    if role_key == "routing-initializer":
        return "路由器"
    return "执行者"


def _notify_runtime_state_changed_best_effort() -> None:
    try:
        from T09_terminal_ops import notify_runtime_state_changed
    except Exception:
        return
    try:
        notify_runtime_state_changed()
    except Exception:
        return


def _preferred_constellation_index(work_dir: str | Path, role_key: str) -> int:
    stable_key = f"{Path(work_dir).expanduser().resolve()}::{role_key}"
    digest = hashlib.sha1(stable_key.encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % len(SESSION_CONSTELLATION_NAMES)


def _list_backend_session_names(backend: Any | None) -> set[str]:
    list_sessions = getattr(backend, "list_sessions", None)
    if not callable(list_sessions):
        return set()
    try:
        return {str(name).strip() for name in list_sessions() if str(name).strip()}
    except Exception:
        return set()


def _occupied_session_names(backend: Any | None = None) -> set[str]:
    occupied = _list_backend_session_names(backend)
    for worker in list_registered_tmux_workers():
        name = str(getattr(worker, "session_name", "") or "").strip()
        if name:
            occupied.add(name)
    return occupied


def _reserve_session_name(
        *,
        worker_id: str,
        work_dir: str | Path,
        vendor: Vendor,
        instance_id: str = "",
        backend: Any | None = None,
) -> str:
    with _RESERVED_SESSION_NAMES_LOCK:
        occupied = _occupied_session_names(backend)
        occupied.update(_RESERVED_SESSION_NAMES)
        session_name = build_session_name(
            worker_id,
            Path(work_dir),
            vendor,
            instance_id=instance_id,
            occupied_session_names=occupied,
        )
        _RESERVED_SESSION_NAMES.add(session_name)
        return session_name


def _release_reserved_session_name(session_name: str) -> None:
    with _RESERVED_SESSION_NAMES_LOCK:
        _RESERVED_SESSION_NAMES.discard(str(session_name or "").strip())


def _resolve_optional_path(path: str | Path | None) -> Path | None:
    text = str(path or "").strip()
    if not text:
        return None
    return Path(text).expanduser().resolve()


def _build_prefixed_sha256(path: str | Path) -> str:
    target = Path(path).expanduser().resolve()
    digest = hashlib.sha256()
    with target.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def clean_ansi(text: str) -> str:
    return re.sub(
        r"\x1b\[[0-9;?]*[A-Za-z]"
        r"|\x1b\]8;[^\x1b]*\x1b\\"
        r"|\x1b\][^\x07]*\x07"
        r"|\x1b\][^\x1b]*\x1b\\"
        r"|\x1b[()][A-Z0-9]"
        r"|\x1b[\x20-\x2f]*[\x40-\x7e]",
        "",
        str(text or ""),
    )


def read_text_tail(path: str | Path, max_lines: int = 40) -> str:
    file_path = Path(path)
    if not file_path.exists():
        return "(文件不存在)"
    text = file_path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    return "\n".join(lines[-max_lines:]).strip() or "(文件为空)"


def _canonical_audit_line(line: str) -> str:
    text = clean_ansi(line).strip()
    if not text or text.startswith("[[ACX_TURN:"):
        return ""
    for prefix in AUDIT_STRUCTURED_PREFIXES:
        marker_index = text.find(prefix)
        if marker_index >= 0:
            return text[marker_index:].strip()
    return ""


def _is_prompt_example_audit_line(line: str) -> bool:
    text = clean_ansi(line).strip()
    if not text:
        return False
    return bool(re.search(r"<[^>]+>", text))


def is_runtime_noise_line(line: str) -> bool:
    text = clean_ansi(line).strip()
    if not text:
        return True
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in RUNTIME_NOISE_PATTERNS)


def _last_seen_protocol_token(text: str, allowed_tokens: Sequence[str]) -> str:
    allowed = set(allowed_tokens)
    for line in reversed(str(text or "").splitlines()):
        candidate = _extract_protocol_token_from_line(line, allowed)
        if candidate:
            return candidate
    return ""


def _extract_protocol_token_from_line(text: str, allowed_tokens: Sequence[str]) -> str:
    cleaned = clean_ansi(text).strip()
    if not cleaned:
        return ""
    for token in allowed_tokens:
        index = cleaned.find(token)
        if index < 0:
            continue
        prefix = cleaned[:index].strip()
        suffix = cleaned[index + len(token):].strip()
        if suffix:
            continue
        if prefix and not _is_symbolic_protocol_prefix(prefix):
            continue
        return token
    return ""


def _is_symbolic_protocol_prefix(prefix: str) -> bool:
    text = str(prefix or "").strip()
    if not text:
        return True
    if len(text) > 8:
        return False
    for ch in text:
        if ch.isalnum() or ch in "<>{}`[]/\\'\"":
            return False
        if "\u4e00" <= ch <= "\u9fff":
            return False
    return True


def _is_turn_token_line(text: str) -> bool:
    cleaned = clean_ansi(text).strip()
    match = re.search(r"\[\[ACX_TURN:[^:\]]+:DONE\]\]", cleaned)
    if not match:
        return False
    prefix = cleaned[: match.start()].strip()
    suffix = cleaned[match.end():].strip()
    return not suffix and _is_symbolic_protocol_prefix(prefix)


def normalize_effort(effort: str | None) -> str:
    value = str(effort or "high").strip().lower()
    allowed = {"low", "medium", "high", "xhigh", "max"}
    if value not in allowed:
        raise ValueError(f"不支持的推理强度: {effort}")
    return value


def normalize_vendor(vendor: str | Vendor) -> Vendor:
    if isinstance(vendor, Vendor):
        return vendor
    value = str(vendor or "").strip().lower()
    try:
        return Vendor(value)
    except ValueError as error:
        raise ValueError(f"不支持的厂商: {vendor}") from error


def normalize_proxy_url(proxy_value: str | int | None) -> str:
    if proxy_value is None:
        return ""
    text = str(proxy_value).strip()
    if not text:
        return ""
    if text.isdigit():
        return f"http://{DEFAULT_PROXY_HOST}:{text}"
    if re.fullmatch(r"[A-Za-z0-9_.-]+:\d+", text):
        return f"http://{text}"
    if re.fullmatch(r"\d+\.\d+\.\d+\.\d+:\d+", text):
        return f"http://{text}"
    if re.match(r"^(?:https?|socks5h?|socks4)://", text):
        return text
    raise ValueError(f"无法解析代理端口/地址: {proxy_value}")


def build_proxy_env(proxy_url: str) -> dict[str, str]:
    if not proxy_url:
        return {}
    parsed = urlparse(proxy_url)
    http_proxy = proxy_url
    all_proxy = proxy_url
    if parsed.scheme in {"http", "https"} and parsed.hostname and parsed.port:
        all_proxy = f"socks5h://{parsed.hostname}:{parsed.port}"
    return {
        "HTTP_PROXY": http_proxy,
        "HTTPS_PROXY": http_proxy,
        "http_proxy": http_proxy,
        "https_proxy": http_proxy,
        "ALL_PROXY": all_proxy,
        "all_proxy": all_proxy,
    }


def build_reasoning_note(vendor: Vendor, effort: str) -> str:
    effort = normalize_effort(effort)
    if vendor == Vendor.CODEX:
        return f"reasoning_effort={effort}"
    if vendor == Vendor.CLAUDE:
        mapped = {"low": "low", "medium": "medium", "high": "high", "xhigh": "max", "max": "max"}[effort]
        return f"reasoning_effort={effort}; claude_effort={mapped}"
    if vendor == Vendor.GEMINI:
        mapped = "flash" if effort in {"low", "medium"} else "pro"
        return f"reasoning_effort={effort}; gemini_model_family={mapped}"
    if vendor == Vendor.QWEN:
        return f"reasoning_effort={effort}; qwen_prompt_hint=true"
    if vendor == Vendor.KIMI:
        thinking = "off" if effort == "low" else "on"
        return f"reasoning_effort={effort}; kimi_thinking={thinking}"
    return f"reasoning_effort={effort}"


def build_prompt_header(vendor: Vendor, model: str, effort: str) -> str:
    note = build_reasoning_note(vendor, effort)
    return (
        "[Agent Runtime Context]\n"
        f"- vendor: {vendor.value}\n"
        f"- model: {model}\n"
        f"- {note}\n"
        "- execution_mode: tmux_interactive_conversation\n"
        "- keep_scope_strict: true\n"
    )


class BaseOutputDetector:
    @staticmethod
    def _contains_any(text: str, patterns: Sequence[str]) -> bool:
        return any(re.search(pattern, text, re.IGNORECASE | re.MULTILINE) for pattern in patterns)

    @staticmethod
    def _has_turn_token(text: str) -> bool:
        return bool(re.search(r"\[\[ACX_TURN:[^:\]]+:DONE\]\]", text))

    def observation_text(self, observation: WorkerObservation) -> str:
        return clean_ansi("\n".join(part for part in [observation.raw_log_tail, observation.visible_text] if part))

    @staticmethod
    def current_visible_text(observation: WorkerObservation) -> str:
        return clean_ansi(observation.visible_text or "")

    @staticmethod
    def recent_log_text(observation: WorkerObservation, *, max_lines: int = 120) -> str:
        lines = clean_ansi(observation.raw_log_tail or "").splitlines()
        return "\n".join(lines[-max_lines:])

    def classify_phase(self, observation: WorkerObservation) -> ProviderPhase:
        text = self.observation_text(observation)
        if observation.pane_dead:
            return ProviderPhase.ERROR
        if not observation.session_exists:
            return ProviderPhase.UNKNOWN
        if observation.current_command in SHELL_COMMANDS:
            return ProviderPhase.SHELL
        if self._has_turn_token(text):
            return ProviderPhase.COMPLETED_RESPONSE
        if observation.current_command:
            return ProviderPhase.PROCESSING
        return ProviderPhase.UNKNOWN

    @staticmethod
    def _split_blocks(text: str) -> list[str]:
        blocks: list[str] = []
        current_lines: list[str] = []
        for line in str(text or "").splitlines():
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
        stripped = line.strip()
        return bool(re.search(r"[$%#]\s*$", stripped))

    def _should_skip_line(self, line: str) -> bool:
        skip_patterns = (
            r"^\? for shortcuts",
            r"^context left",
            r"^\d+%\s+left",
            r"^Tip:",
            r"^Press (?:ESC|Esc|esc)",
            r"^Use the arrow keys",
            r"^Select an option",
            r"^[│┌┐└┘╭╮╰╯╷╵─═]+$",
            r"^╭─",
            r"^╰─",
            r"^│\s*$",
        )
        return any(re.search(pattern, line, re.IGNORECASE) for pattern in skip_patterns) or self._is_shell_prompt_line(
            line)

    def extract_last_message(self, output: str) -> str:
        clean_output = clean_ansi(output)
        lines: list[str] = []
        for line in clean_output.splitlines():
            normalized = line.strip()
            if not normalized:
                lines.append("")
                continue
            if self._should_skip_line(normalized):
                continue
            lines.append(line.rstrip())
        blocks = self._split_blocks("\n".join(lines))
        if not blocks:
            raise ValueError("No assistant response found in terminal output")
        return blocks[-1]


class CodexOutputDetector(BaseOutputDetector):
    def classify_phase(self, observation: WorkerObservation) -> ProviderPhase:
        visible_text = self.current_visible_text(observation)
        recent_log = self.recent_log_text(observation)
        text = self.observation_text(observation)
        base_phase = super().classify_phase(observation)
        if base_phase in {ProviderPhase.SHELL, ProviderPhase.ERROR, ProviderPhase.UNKNOWN,
                          ProviderPhase.COMPLETED_RESPONSE}:
            return base_phase
        ready_visible = self._contains_any(visible_text or text, CODEX_READY_PATTERNS)
        processing_visible = self._contains_any(visible_text or recent_log or text, CODEX_PROCESSING_PATTERNS)
        if self._contains_any(visible_text or text, CODEX_TRUST_PROMPT_PATTERNS):
            return ProviderPhase.AUTH_PROMPT
        if ready_visible and not processing_visible and not self._contains_any(visible_text or text, CODEX_STARTING_PATTERNS):
            return ProviderPhase.WAITING_INPUT
        if self._contains_any(visible_text or text, CODEX_UPDATE_PROMPT_PATTERNS) or self._contains_any(
            visible_text or text,
            CODEX_MODEL_SELECTION_PROMPT_PATTERNS,
        ):
            return ProviderPhase.UPDATE_PROMPT
        if self._contains_any(visible_text or text, CODEX_STARTING_PATTERNS):
            return ProviderPhase.BOOTING if observation.current_command else ProviderPhase.UNKNOWN
        if processing_visible:
            return ProviderPhase.PROCESSING
        if ready_visible:
            return ProviderPhase.WAITING_INPUT
        return ProviderPhase.PROCESSING

    def extract_last_message(self, output: str) -> str:
        clean_output = clean_ansi(output)
        assistant_matches = list(
            re.finditer(r"^(?:assistant|codex|agent)\s*:\s*", clean_output, re.IGNORECASE | re.MULTILINE)
        )
        if assistant_matches:
            last_match = assistant_matches[-1]
            content = clean_output[last_match.end():]
            idle_match = re.search(r"^\s*(?:❯|›|codex>)\s*$", content, re.MULTILINE)
            if idle_match:
                content = content[:idle_match.start()]
            return super().extract_last_message(content)
        idle_match = re.search(r"^\s*(?:❯|›|codex>)(?:\s|$)", clean_output, re.MULTILINE)
        if idle_match:
            clean_output = clean_output[:idle_match.start()]
        return super().extract_last_message(clean_output)


class ClaudeOutputDetector(BaseOutputDetector):
    def classify_phase(self, observation: WorkerObservation) -> ProviderPhase:
        text = self.observation_text(observation)
        base_phase = super().classify_phase(observation)
        if base_phase in {ProviderPhase.SHELL, ProviderPhase.ERROR, ProviderPhase.UNKNOWN,
                          ProviderPhase.COMPLETED_RESPONSE}:
            return base_phase
        if re.search(r"^\s*❯", text, re.MULTILINE):
            return ProviderPhase.WAITING_INPUT
        return ProviderPhase.PROCESSING

    def extract_last_message(self, output: str) -> str:
        clean_output = clean_ansi(output)
        lines = clean_output.splitlines()
        content_lines: list[str] = []
        for line in lines:
            normalized = line.strip()
            if normalized.startswith("❯"):
                continue
            content_lines.append(line)
        return super().extract_last_message("\n".join(content_lines))


class GeminiOutputDetector(BaseOutputDetector):
    def classify_phase(self, observation: WorkerObservation) -> ProviderPhase:
        visible_text = self.current_visible_text(observation)
        recent_log = self.recent_log_text(observation)
        text = self.observation_text(observation)
        base_phase = super().classify_phase(observation)
        if base_phase in {ProviderPhase.SHELL, ProviderPhase.ERROR, ProviderPhase.UNKNOWN,
                          ProviderPhase.COMPLETED_RESPONSE}:
            return base_phase
        if self._contains_any(visible_text, GEMINI_INPUT_BOX_PATTERNS) or self._contains_any(
            visible_text,
            GEMINI_READY_PATTERNS,
        ):
            return ProviderPhase.WAITING_INPUT
        if self._contains_any(visible_text, GEMINI_PROCESSING_PATTERNS):
            return ProviderPhase.PROCESSING
        if self._contains_any(visible_text, GEMINI_TRUST_PROMPT_PATTERNS):
            return ProviderPhase.AUTH_PROMPT
        if self._contains_any(visible_text, GEMINI_NOT_READY_PATTERNS):
            return ProviderPhase.AUTH_PROMPT
        if self._contains_any(recent_log or text, GEMINI_TRUST_PROMPT_PATTERNS):
            return ProviderPhase.AUTH_PROMPT
        if self._contains_any(recent_log or text, GEMINI_NOT_READY_PATTERNS):
            return ProviderPhase.AUTH_PROMPT
        if self._contains_any(recent_log or text, GEMINI_INPUT_BOX_PATTERNS) or self._contains_any(
            recent_log or text,
            GEMINI_READY_PATTERNS,
        ):
            return ProviderPhase.WAITING_INPUT
        if self._contains_any(recent_log or text, GEMINI_PROCESSING_PATTERNS):
            return ProviderPhase.PROCESSING
        return ProviderPhase.BOOTING if observation.current_command else ProviderPhase.UNKNOWN

    def extract_last_message(self, output: str) -> str:
        clean_output = clean_ansi(output)
        lines = clean_output.splitlines()
        input_box_start = len(lines)
        for index in range(len(lines) - 1, -1, -1):
            normalized = lines[index].strip()
            if any(re.search(pattern, normalized, re.IGNORECASE) for pattern in GEMINI_INPUT_BOX_PATTERNS):
                input_box_start = index
                break
        content_lines: list[str] = []
        for line in lines[:input_box_start]:
            normalized = line.strip()
            if any(re.search(pattern, normalized, re.IGNORECASE) for pattern in GEMINI_FOOTER_PATTERNS):
                continue
            content_lines.append(line)
        return super().extract_last_message("\n".join(content_lines))


class QwenOutputDetector(GeminiOutputDetector):
    def classify_phase(self, observation: WorkerObservation) -> ProviderPhase:
        text = self.observation_text(observation)
        base_phase = BaseOutputDetector.classify_phase(self, observation)
        if base_phase in {ProviderPhase.SHELL, ProviderPhase.ERROR, ProviderPhase.UNKNOWN,
                          ProviderPhase.COMPLETED_RESPONSE}:
            return base_phase
        if self._contains_any(text, QWEN_INPUT_BOX_PATTERNS) or self._contains_any(text, QWEN_READY_PATTERNS):
            return ProviderPhase.WAITING_INPUT
        return ProviderPhase.BOOTING if observation.current_command else ProviderPhase.UNKNOWN


class KimiOutputDetector(BaseOutputDetector):
    def classify_phase(self, observation: WorkerObservation) -> ProviderPhase:
        text = self.observation_text(observation)
        base_phase = super().classify_phase(observation)
        if base_phase in {ProviderPhase.SHELL, ProviderPhase.ERROR, ProviderPhase.UNKNOWN,
                          ProviderPhase.COMPLETED_RESPONSE}:
            return base_phase
        if self._contains_any(text, KIMI_UPDATE_PROMPT_PATTERNS):
            return ProviderPhase.UPDATE_PROMPT
        if self._contains_any(text, KIMI_NOT_READY_PATTERNS):
            return ProviderPhase.AUTH_PROMPT
        if re.search(r"^\s*>\s*$", text, re.MULTILINE):
            return ProviderPhase.WAITING_INPUT
        return ProviderPhase.PROCESSING

    def extract_last_message(self, output: str) -> str:
        clean_output = clean_ansi(output)
        lines: list[str] = []
        for line in clean_output.splitlines():
            normalized = line.strip()
            if normalized.startswith(">"):
                continue
            if normalized.startswith("Kimi, your next CLI agent."):
                continue
            lines.append(line)
        return super().extract_last_message("\n".join(lines))


def build_output_detector(vendor: Vendor) -> BaseOutputDetector:
    if vendor == Vendor.CODEX:
        return CodexOutputDetector()
    if vendor == Vendor.CLAUDE:
        return ClaudeOutputDetector()
    if vendor == Vendor.GEMINI:
        return GeminiOutputDetector()
    if vendor == Vendor.QWEN:
        return QwenOutputDetector()
    if vendor == Vendor.KIMI:
        return KimiOutputDetector()
    raise ValueError(f"不支持的厂商: {vendor}")


@dataclass(frozen=True)
class AgentRunConfig:
    vendor: Vendor
    model: str
    reasoning_effort: str = "high"
    proxy_url: str = ""
    extra_args: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "vendor", normalize_vendor(self.vendor))
        object.__setattr__(self, "reasoning_effort", normalize_effort(self.reasoning_effort))
        object.__setattr__(self, "proxy_url", normalize_proxy_url(self.proxy_url))
        object.__setattr__(self, "model", str(self.model or "").strip())
        if not self.model:
            raise ValueError("model 不能为空")

    def with_prompt_header(self, prompt: str) -> str:
        header = build_prompt_header(self.vendor, self.model, self.reasoning_effort)
        return f"{header}\n\n{str(prompt or '').strip()}".strip()

    def to_summary(self) -> dict[str, str]:
        return {
            "vendor": self.vendor.value,
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "proxy_url": self.proxy_url,
            "reasoning_note": build_reasoning_note(self.vendor, self.reasoning_effort),
        }

    def expected_current_commands(self) -> tuple[str, ...]:
        if self.vendor == Vendor.KIMI:
            return ("kimi", "python", "python3", "uv")
        return {
            Vendor.CODEX: ("codex", "node"),
            Vendor.CLAUDE: ("claude", "node"),
            Vendor.GEMINI: ("gemini", "node"),
            Vendor.QWEN: ("qwen", "node"),
        }[self.vendor]

    def submit_enter_count(self) -> int:
        return 2 if self.vendor == Vendor.CODEX else 1

    def build_launch_command(self, work_dir: Path) -> str:
        args: list[str] = []
        if self.vendor == Vendor.CODEX:
            effort = {"low": "low", "medium": "medium", "high": "high", "xhigh": "xhigh", "max": "xhigh"}[
                self.reasoning_effort
            ]
            args = [
                "codex",
                "--model",
                self.model,
                "--config",
                f'model_reasoning_effort="{effort}"',
                "--sandbox",
                "danger-full-access",
                "--ask-for-approval",
                "never",
                "--cd",
                str(work_dir),
                "--no-alt-screen",
            ]
        elif self.vendor == Vendor.CLAUDE:
            effort = {"low": "low", "medium": "medium", "high": "high", "xhigh": "max", "max": "max"}[
                self.reasoning_effort
            ]
            args = [
                "claude",
                "--model",
                self.model,
                "--permission-mode",
                "bypassPermissions",
                "--effort",
                effort,
            ]
        elif self.vendor == Vendor.GEMINI:
            gemini_model = self.model
            if self.model.lower() in {"auto", "default"}:
                gemini_model = "flash" if self.reasoning_effort in {"low", "medium"} else "pro"
            args = [
                "gemini",
                "--model",
                gemini_model,
                "--approval-mode",
                "yolo",
            ]
        elif self.vendor == Vendor.QWEN:
            args = [
                "qwen",
                "--model",
                self.model,
                "--approval-mode",
                "yolo",
            ]
            if self.proxy_url:
                args.extend(["--proxy", self.proxy_url])
        elif self.vendor == Vendor.KIMI:
            args = [
                "kimi",
                "--work-dir",
                str(work_dir),
                "--model",
                self.model,
                "--yolo",
            ]
            if self.reasoning_effort == "low":
                args.append("--no-thinking")
            else:
                args.append("--thinking")
        else:
            raise ValueError(f"不支持的厂商: {self.vendor}")

        args.extend(self.extra_args)
        return " ".join(shlex.quote(item) for item in args)


@dataclass
class CommandResult:
    label: str
    command: str
    exit_code: int
    raw_output: str
    clean_output: str
    started_at: str
    finished_at: str

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


@dataclass
class WorkerResult:
    worker_id: str
    session_name: str
    pane_id: str
    runtime_dir: str
    work_dir: str
    config: dict[str, str]
    status: str
    commands: list[CommandResult] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["commands"] = [asdict(item) for item in self.commands]
        return payload


class TmuxBatchWorker:
    def __init__(
            self,
            *,
            worker_id: str,
            work_dir: str | Path,
            config: AgentRunConfig,
            runtime_root: str | Path | None = None,
            existing_runtime_dir: str | Path | None = None,
            existing_session_name: str = "",
            existing_pane_id: str = "",
            backend: TmuxBackend | None = None,
            launch_coordinator: LaunchCoordinator | None = None,
    ) -> None:
        reserved_session_name = ""
        self._session_name_reserved = False
        self.worker_id = _slugify(worker_id, max_len=48)
        self.work_dir = Path(work_dir).expanduser().resolve()
        if not self.work_dir.is_dir():
            raise FileNotFoundError(f"工作目录不存在: {self.work_dir}")
        self.config = config
        self.backend = backend or TmuxBackend()
        self.detector = build_output_detector(self.config.vendor)
        self.runtime_root = Path(runtime_root or DEFAULT_RUNTIME_ROOT).expanduser().resolve()
        self.launch_coordinator = launch_coordinator or LaunchCoordinator(self.runtime_root)
        if existing_runtime_dir:
            self.runtime_dir = Path(existing_runtime_dir).expanduser().resolve()
            self.runtime_dir.mkdir(parents=True, exist_ok=True)
            self.instance_id = self.runtime_dir.name.rsplit("-", 1)[
                -1] if "-" in self.runtime_dir.name else uuid.uuid4().hex[:8]
        else:
            self.instance_id = uuid.uuid4().hex[:8]
            self.runtime_dir = self.runtime_root / f"{self.worker_id}-{self.instance_id}"
            self.runtime_dir.mkdir(parents=True, exist_ok=True)
        if existing_session_name:
            self.session_name = existing_session_name
        else:
            reserved_session_name = _reserve_session_name(
                worker_id=self.worker_id,
                work_dir=self.work_dir,
                vendor=self.config.vendor,
                instance_id=self.instance_id,
                backend=self.backend,
            )
            self.session_name = reserved_session_name
        self.log_path = self.runtime_dir / "worker.log"
        self.raw_log_path = self.runtime_dir / "worker.raw.log"
        self.state_path = self.runtime_dir / "worker.state.json"
        self.transcript_path = self.runtime_dir / "transcript.md"
        self.pane_id = existing_pane_id
        self.send_lock = threading.Lock()
        self.state_lock = threading.RLock()
        self.results: list[CommandResult] = []
        self.health_supervisor: HealthSupervisor | None = None
        self.agent_ready = False
        self.recoverable = True
        self.last_reply = ""
        self.last_log_offset = 0
        self.current_command = ""
        self.current_path = ""
        self.last_heartbeat_at = ""
        self.provider_phase = ProviderPhase.UNKNOWN
        self.wrapper_state = WrapperState.NOT_READY
        self.current_task_status_path = ""
        self.current_task_manifest_path = ""
        self.current_task_result_path = ""
        self.current_task_completion_command = ""
        self.current_task_runtime_status = ""
        self._phase_candidate = ProviderPhase.UNKNOWN
        self._phase_candidate_count = 0
        self._phase_candidate_since = 0.0
        self.last_terminal_signature = ""
        self.last_terminal_changed_at = ""
        self.terminal_recently_changed = False
        self._last_terminal_change_monotonic = 0.0
        self.current_task_completion_nudge_count = 0
        self.current_task_completion_nudge_at = ""
        self._current_task_last_nudge_monotonic = 0.0
        self.task_completion_nudge_idle_sec = TASK_COMPLETION_NUDGE_IDLE_SEC
        self.task_completion_nudge_grace_sec = TASK_COMPLETION_NUDGE_GRACE_SEC
        self.task_completion_nudge_max_count = TASK_COMPLETION_NUDGE_MAX_COUNT
        self._last_boot_action_signature = ""
        self._last_boot_action_at = 0.0
        self.launch_command = self.config.build_launch_command(self.work_dir)
        if self.state_path.exists():
            existing_state = self.read_state()
            self.pane_id = existing_pane_id or str(existing_state.get("pane_id", self.pane_id))
            self.session_name = existing_session_name or str(existing_state.get("session_name", self.session_name))
            if reserved_session_name and self.session_name != reserved_session_name:
                _release_reserved_session_name(reserved_session_name)
                reserved_session_name = ""
            self.agent_ready = bool(existing_state.get("agent_ready", False))
            self.recoverable = bool(existing_state.get("recoverable", True))
            self.last_reply = str(existing_state.get("last_reply", ""))
            self.last_log_offset = int(existing_state.get("last_log_offset", 0))
            self.current_command = str(existing_state.get("current_command", ""))
            self.current_path = str(existing_state.get("current_path", ""))
            self.last_heartbeat_at = str(existing_state.get("last_heartbeat_at", ""))
            self.current_task_status_path = str(existing_state.get("current_task_status_path", ""))
            self.current_task_manifest_path = str(existing_state.get("current_task_manifest_path", ""))
            self.current_task_result_path = str(existing_state.get("current_task_result_path", ""))
            self.current_task_completion_command = str(existing_state.get("current_task_completion_command", ""))
            self.current_task_runtime_status = str(existing_state.get("current_task_runtime_status", ""))
            self.current_task_completion_nudge_count = int(
                existing_state.get("current_task_completion_nudge_count", 0)
            )
            self.current_task_completion_nudge_at = str(
                existing_state.get("current_task_completion_nudge_at", "")
            )
            self.last_terminal_signature = str(existing_state.get("last_terminal_signature", ""))
            self.last_terminal_changed_at = str(existing_state.get("last_terminal_changed_at", ""))
            self.terminal_recently_changed = bool(existing_state.get("terminal_recently_changed", False))
            try:
                self.provider_phase = ProviderPhase(
                    str(existing_state.get("provider_phase", ProviderPhase.UNKNOWN.value)))
            except ValueError:
                self.provider_phase = ProviderPhase.UNKNOWN
            try:
                self.wrapper_state = WrapperState(
                    str(existing_state.get("wrapper_state", WrapperState.NOT_READY.value)))
            except ValueError:
                self.wrapper_state = WrapperState.NOT_READY
        self._session_name_reserved = bool(reserved_session_name)
        _register_live_worker(self)

    def _tmux(self, *args: str, input_text: str | None = None, timeout_sec: float = 10.0) -> \
    subprocess.CompletedProcess[str]:
        return self.backend.run(*args, input_text=input_text, timeout_sec=timeout_sec, check=True)

    def session_exists(self) -> bool:
        return self.backend.has_session(self.session_name)

    def attach_session(self) -> None:
        if not self.session_exists():
            raise RuntimeError(f"tmux session 不存在: {self.session_name}")
        self.backend.attach_session(self.session_name)

    def detach_session(self) -> str:
        if not self.session_name:
            raise RuntimeError("tmux 会话尚未创建")
        if not self.session_exists():
            raise RuntimeError(f"tmux 会话尚未创建: {self.session_name}")
        self.backend.detach_session(self.session_name)
        return self.session_name

    def show_transcript_tail(self, *, max_lines: int = 60) -> str:
        return read_text_tail(self.transcript_path, max_lines=max_lines)

    def read_state(self) -> dict[str, object]:
        with self.state_lock:
            if not self.state_path.exists():
                return {}
            return json.loads(self.state_path.read_text(encoding="utf-8"))

    def runtime_metadata(self) -> dict[str, str]:
        return {
            "worker_id": self.worker_id,
            "session_name": self.session_name,
            "pane_id": self.pane_id,
            "runtime_dir": str(self.runtime_dir),
            "work_dir": str(self.work_dir),
            "log_path": str(self.log_path),
            "raw_log_path": str(self.raw_log_path),
            "state_path": str(self.state_path),
            "transcript_path": str(self.transcript_path),
        }

    def target_exists(self, target: str | None = None) -> bool:
        target_name = target or self.pane_id
        if not target_name:
            return False
        return self.backend.target_exists(target_name)

    def pane_current_command(self) -> str:
        return self.backend.display_message(self.pane_id, "#{pane_current_command}")

    def pane_current_path(self) -> str:
        return self.backend.display_message(self.pane_id, "#{pane_current_path}")

    def pane_dead(self) -> bool:
        return self.backend.display_message(self.pane_id, "#{pane_dead}") == "1"

    def capture_visible(self, tail_lines: int = 500) -> str:
        return self.backend.capture_visible(self.pane_id, tail_lines=tail_lines)

    def _capture_pane_snapshot(self, *, tail_lines: int) -> tuple[bool, str, str, str, bool]:
        session_exists = self.session_exists()
        if not session_exists or not self.pane_id:
            return False, "", "", "", False
        try:
            if not self.target_exists():
                return False, "", "", "", False
            visible_text = clean_ansi(self.capture_visible(tail_lines))
            current_command = self.pane_current_command()
            current_path = self.pane_current_path()
            pane_dead = self.pane_dead()
            return True, visible_text, current_command, current_path, pane_dead
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            return False, "", "", "", False

    def _build_shell_bootstrap_command(self) -> str:
        env_parts = [
            f"{key}={shlex.quote(value)}"
            for key, value in build_proxy_env(self.config.proxy_url).items()
        ]
        shell_path = os.environ.get("SHELL", "/bin/zsh")
        if env_parts:
            return f"env {' '.join(env_parts)} {shlex.quote(shell_path)} -il"
        return f"{shlex.quote(shell_path)} -il"

    def _log_event(self, event: str, **payload: object) -> None:
        entry = {"at": _now_iso(), "event": event, **payload}
        with self.log_path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def _ensure_health_supervisor_started(self) -> None:
        if self.health_supervisor is not None:
            return
        self.health_supervisor = HealthSupervisor(
            self._refresh_health_state_nonintrusive,
            interval_sec=2.0,
            thread_name=f"worker-health-{self.instance_id}",
        )
        self.health_supervisor.start()

    def _stop_health_supervisor(self) -> None:
        if self.health_supervisor is None:
            return
        supervisor = self.health_supervisor
        self.health_supervisor = None
        supervisor.stop()

    def create_session(self) -> str:
        if self.session_exists():
            self._stop_health_supervisor()
            self.backend.kill_session(self.session_name)

        self._reset_terminal_activity()
        self._reset_task_completion_nudge_state()
        self.current_task_status_path = ""
        self.current_task_manifest_path = ""
        self.current_task_result_path = ""
        self.current_task_completion_command = ""
        self.current_task_runtime_status = ""
        try:
            self.pane_id = self.backend.create_session(
                self.session_name,
                self.work_dir,
                self._build_shell_bootstrap_command(),
            )
        except Exception:
            if self._session_name_reserved:
                _release_reserved_session_name(self.session_name)
                self._session_name_reserved = False
            raise
        if self._session_name_reserved:
            _release_reserved_session_name(self.session_name)
            self._session_name_reserved = False
        self.wrapper_state = WrapperState.NOT_READY
        self._tmux("set-option", "-t", self.session_name, "allow-rename", "off")
        self._tmux("set-window-option", "-t", f"{self.session_name}:0", "automatic-rename", "off")
        self._start_pipe_logging()
        self._ensure_health_supervisor_started()
        self._refresh_health_state_nonintrusive()
        self._log_event("session_created", pane_id=self.pane_id, session_name=self.session_name)
        self._write_state(WorkerStatus.READY, note="session_created")
        return self.pane_id

    def _start_pipe_logging(self) -> None:
        self.log_path.write_text("", encoding="utf-8")
        self.raw_log_path.write_text("", encoding="utf-8")
        self.backend.pipe_log(self.pane_id, self.raw_log_path)
        self._log_event("pipe_log_started", raw_log_path=str(self.raw_log_path))

    def tail_raw_log(self, *, tail_bytes: int = 24000) -> tuple[str, str, int, float]:
        delta, tail, next_offset, log_mtime = self.backend.tail_raw_log(
            self.raw_log_path,
            last_offset=self.last_log_offset,
            tail_bytes=tail_bytes,
        )
        self.last_log_offset = next_offset
        return delta, tail, next_offset, log_mtime

    def observe(self, *, tail_lines: int = 500, tail_bytes: int = 24000) -> WorkerObservation:
        observed_at = _now_iso()
        session_exists, visible_text, current_command, current_path, pane_dead = self._capture_pane_snapshot(
            tail_lines=tail_lines
        )
        raw_log_delta, raw_log_tail, _, log_mtime = self.tail_raw_log(tail_bytes=tail_bytes)
        self.current_command = current_command or self.current_command
        self.current_path = current_path or self.current_path
        self.last_heartbeat_at = observed_at
        terminal_surface = visible_text or clean_ansi(raw_log_tail)
        self._update_terminal_activity(terminal_surface, observed_at=observed_at)
        observation = WorkerObservation(
            visible_text=visible_text,
            raw_log_delta=clean_ansi(raw_log_delta),
            raw_log_tail=clean_ansi(raw_log_tail),
            current_command=current_command,
            current_path=current_path,
            pane_dead=pane_dead,
            session_exists=session_exists,
            log_mtime=log_mtime,
            observed_at=observed_at,
        )
        self.provider_phase = self._debounce_provider_phase(self.detector.classify_phase(observation))
        self.wrapper_state = self._infer_wrapper_state(
            current_command=observation.current_command,
            visible_text=observation.visible_text,
        )
        return observation

    def _debounce_provider_phase(self, phase: ProviderPhase) -> ProviderPhase:
        if phase in {ProviderPhase.PROCESSING, ProviderPhase.SHELL, ProviderPhase.ERROR, ProviderPhase.AUTH_PROMPT,
                     ProviderPhase.UPDATE_PROMPT, ProviderPhase.BOOTING, ProviderPhase.RECOVERING,
                     ProviderPhase.UNKNOWN}:
            self._phase_candidate = phase
            self._phase_candidate_count = 1
            self._phase_candidate_since = time.monotonic()
            return phase

        now = time.monotonic()
        if phase != self._phase_candidate:
            self._phase_candidate = phase
            self._phase_candidate_count = 1
            self._phase_candidate_since = now
            return self.provider_phase

        if now - self._phase_candidate_since >= 0.5:
            self._phase_candidate_count += 1
        if self._phase_candidate_count >= 2:
            return phase
        return self.provider_phase

    def _write_state(self, status: WorkerStatus, *, note: str, extra: dict[str, object] | None = None) -> None:
        with self.state_lock:
            previous = self.read_state()
            payload: dict[str, object] = {
                "worker_id": self.worker_id,
                "session_name": self.session_name,
                "pane_id": self.pane_id,
                "work_dir": str(self.work_dir),
                "status": status.value,
                "note": note,
                "updated_at": _now_iso(),
                "config": self.config.to_summary(),
                "log_path": str(self.log_path),
                "raw_log_path": str(self.raw_log_path),
                "transcript_path": str(self.transcript_path),
                "agent_ready": self.agent_ready,
                "last_reply": self.last_reply,
                "state_revision": int(previous.get("state_revision", 0)) + 1,
                "last_writer": "TmuxBatchWorker",
                "workflow_stage": str(previous.get("workflow_stage", "pending")),
                "workflow_round": int(previous.get("workflow_round", 0)),
                "provider_phase": self.provider_phase.value,
                "wrapper_state": self.wrapper_state.value,
                "health_status": str(previous.get("health_status", "unknown")),
                "health_note": str(previous.get("health_note", "")),
                "retry_count": int(previous.get("retry_count", 0)),
                "last_log_offset": self.last_log_offset,
                "auto_recovery_mode": str(previous.get("auto_recovery_mode", "standard")),
                "recoverable": self.recoverable,
                "result_status": str(previous.get("result_status", "pending")),
                "current_command": self.current_command or str(previous.get("current_command", "")),
                "current_path": self.current_path or str(previous.get("current_path", "")),
                "last_turn_token": str(previous.get("last_turn_token", "")),
                "last_prompt_hash": str(previous.get("last_prompt_hash", "")),
                "last_heartbeat_at": self.last_heartbeat_at or str(previous.get("last_heartbeat_at", "")),
                "current_turn_id": str(previous.get("current_turn_id", "")),
                "current_turn_phase": str(previous.get("current_turn_phase", "")),
                "current_turn_status_path": str(previous.get("current_turn_status_path", "")),
                "current_task_status_path": self.current_task_status_path or str(previous.get("current_task_status_path", "")),
                "current_task_manifest_path": self.current_task_manifest_path or str(previous.get("current_task_manifest_path", "")),
                "current_task_result_path": self.current_task_result_path or str(previous.get("current_task_result_path", "")),
                "current_task_completion_command": self.current_task_completion_command or str(previous.get("current_task_completion_command", "")),
                "current_task_runtime_status": self.current_task_runtime_status or str(previous.get("current_task_runtime_status", "")),
                "current_task_completion_nudge_count": self.current_task_completion_nudge_count,
                "current_task_completion_nudge_at": self.current_task_completion_nudge_at,
                "last_terminal_signature": self.last_terminal_signature,
                "last_terminal_changed_at": self.last_terminal_changed_at,
                "terminal_recently_changed": self.terminal_recently_changed,
            }
            if extra:
                payload.update(extra)
            tmp_path = self.state_path.with_suffix(".tmp")
            tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp_path.replace(self.state_path)
        self._log_event("state_changed", status=status.value, note=note)
        _notify_runtime_state_changed_best_effort()

    def _build_passive_health_snapshot(self) -> WorkerHealthSnapshot:
        observed_at = _now_iso()
        session_exists, visible_text, current_command, current_path, pane_dead = self._capture_pane_snapshot(
            tail_lines=120
        )
        if not session_exists:
            return WorkerHealthSnapshot(
                session_exists=False,
                health_status="missing_session",
                health_note="missing_session",
                provider_phase=self.provider_phase.value,
                last_heartbeat_at=observed_at,
                last_log_offset=self.last_log_offset,
                current_command=self.current_command,
                current_path=self.current_path,
                pane_id=self.pane_id,
                session_name=self.session_name,
            )
        passive_observation = WorkerObservation(
            visible_text=visible_text,
            raw_log_delta="",
            raw_log_tail="",
            current_command=current_command,
            current_path=current_path,
            pane_dead=pane_dead,
            session_exists=session_exists,
            log_mtime=0.0,
            observed_at=observed_at,
        )
        phase = self.detector.classify_phase(passive_observation)
        if is_provider_auth_error(visible_text):
            health_status = "provider_auth_error"
            health_note = "provider_auth_error"
        else:
            health_status = "pane_dead" if pane_dead else "alive"
            health_note = phase.value
        return WorkerHealthSnapshot(
            session_exists=True,
            health_status=health_status,
            health_note=health_note,
            provider_phase=phase.value,
            last_heartbeat_at=observed_at,
            last_log_offset=self.last_log_offset,
            current_command=current_command or self.current_command,
            current_path=current_path or self.current_path,
            pane_id=self.pane_id,
            session_name=self.session_name,
        )

    def _refresh_health_state_nonintrusive(self) -> WorkerHealthSnapshot:
        snapshot = self._build_passive_health_snapshot()
        with self.state_lock:
            previous = self.read_state()
            health_changed = (
                str(previous.get("health_status", "unknown")) != snapshot.health_status
                or str(previous.get("health_note", "")) != snapshot.health_note
                or str(previous.get("current_command", "")) != snapshot.current_command
                or str(previous.get("current_path", "")) != snapshot.current_path
            )
            if health_changed:
                payload = dict(previous)
                payload.update(
                    {
                        "health_status": snapshot.health_status,
                        "health_note": snapshot.health_note,
                        "current_command": snapshot.current_command,
                        "current_path": snapshot.current_path,
                        "last_heartbeat_at": snapshot.last_heartbeat_at,
                    }
                )
                tmp_path = self.state_path.with_suffix(".tmp")
                tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
                tmp_path.replace(self.state_path)
        return snapshot

    def request_restart(self) -> str:
        if self.session_exists():
            self._stop_health_supervisor()
            self.backend.kill_session(self.session_name)
        self.agent_ready = False
        self.recoverable = True
        self.provider_phase = ProviderPhase.RECOVERING
        self.wrapper_state = WrapperState.NOT_READY
        self._reset_terminal_activity()
        self._reset_task_completion_nudge_state()
        self.current_task_status_path = ""
        self.current_task_manifest_path = ""
        self.current_task_result_path = ""
        self.current_task_completion_command = ""
        self.current_task_runtime_status = ""
        self._log_event("manual_restart_requested", session_name=self.session_name)
        return self.session_name

    def request_kill(self) -> str:
        if self.session_exists():
            self._stop_health_supervisor()
            self.backend.kill_session(self.session_name)
        self.agent_ready = False
        self.recoverable = False
        self.provider_phase = ProviderPhase.ERROR
        self.wrapper_state = WrapperState.NOT_READY
        self._reset_terminal_activity()
        self._reset_task_completion_nudge_state()
        self.current_task_status_path = ""
        self.current_task_manifest_path = ""
        self.current_task_result_path = ""
        self.current_task_completion_command = ""
        self.current_task_runtime_status = ""
        self._log_event("manual_kill_requested", session_name=self.session_name)
        return self.session_name

    def refresh_health(
            self,
            *,
            auto_relaunch: bool = False,
            relaunch_timeout_sec: float = 30.0,
    ) -> WorkerHealthSnapshot:
        if not auto_relaunch:
            return self._refresh_health_state_nonintrusive()
        session_exists = self.session_exists()
        health_status = "alive" if session_exists else "missing_session"
        health_note = self.provider_phase.value if session_exists else "missing_session"

        if session_exists:
            try:
                observation = self.observe(tail_lines=160)
                health_status = "pane_dead" if observation.pane_dead else "alive"
                health_note = self.provider_phase.value
                return WorkerHealthSnapshot(
                    session_exists=True,
                    health_status=health_status,
                    health_note=health_note,
                    provider_phase=self.provider_phase.value,
                    last_heartbeat_at=self.last_heartbeat_at,
                    last_log_offset=self.last_log_offset,
                    current_command=self.current_command,
                    current_path=self.current_path,
                    pane_id=self.pane_id,
                    session_name=self.session_name,
                )
            except Exception as error:
                health_status = "observe_error"
                health_note = str(error)

        if auto_relaunch:
            try:
                self.provider_phase = ProviderPhase.RECOVERING
                self.ensure_agent_ready(timeout_sec=relaunch_timeout_sec)
                health_status = "auto_relaunched"
                health_note = "auto_relaunched"
            except Exception as error:
                health_status = "relaunch_failed"
                health_note = str(error)

        return WorkerHealthSnapshot(
            session_exists=self.session_exists(),
            health_status=health_status,
            health_note=health_note,
            provider_phase=self.provider_phase.value,
            last_heartbeat_at=self.last_heartbeat_at,
            last_log_offset=self.last_log_offset,
            current_command=self.current_command,
            current_path=self.current_path,
            pane_id=self.pane_id,
            session_name=self.session_name,
        )

    def _append_transcript(self, title: str, body: str) -> None:
        with self.transcript_path.open("a", encoding="utf-8") as file:
            file.write(f"## {title}\n\n{body.rstrip()}\n\n")

    def _record_result(self, result: CommandResult, *, status: WorkerStatus, note: str,
                       extra: dict[str, object] | None = None) -> None:
        self.results.append(result)
        self._write_state(status, note=note, extra=extra)
        self._append_transcript(f"{result.label} / output", f"```text\n{result.clean_output}\n```")

    def send_special_key(self, key: str) -> None:
        with self.send_lock:
            self.backend.send_key(self.pane_id, key)
        self._log_event("send_key", key=key)

    def _send_text(self, text: str, enter_count: int | None = None) -> None:
        submit_count = enter_count if enter_count is not None else self.config.submit_enter_count()
        with self.send_lock:
            self.backend.send_text(self.pane_id, text, submit_count=submit_count)
        self._log_event("send_text", submit_count=submit_count, size=len(text))

    @staticmethod
    def _normalize_prompt_text(prompt: str) -> str:
        return clean_ansi(str(prompt or "")).strip()

    @classmethod
    def _source_mentions_prompt(cls, source: str, prompt: str) -> bool:
        prompt_text = cls._normalize_prompt_text(prompt)
        if not prompt_text:
            return False
        source_text = cls._normalize_prompt_text(source)
        return bool(source_text) and prompt_text in source_text

    def _wait_for_prompt_submission(
            self,
            *,
            prompt: str,
            timeout_sec: float,
    ) -> WorkerObservation:
        deadline = time.monotonic() + timeout_sec
        extra_enter_sent = False
        submit_started_at = time.monotonic()
        submission_observed = False

        while time.monotonic() < deadline:
            observation = self.observe(tail_lines=320)
            if not observation.session_exists:
                raise RuntimeError("tmux pane exited while waiting for prompt submission")
            if observation.pane_dead:
                raise RuntimeError(f"tmux pane died after sending prompt:\n{self.capture_visible(160)}")

            current_command = observation.current_command
            if current_command in SHELL_COMMANDS:
                self.agent_ready = False
                raise RuntimeError(f"agent exited back to shell after prompt submission:\n{observation.visible_text}")

            prompt_visible = self._source_mentions_prompt(observation.visible_text, prompt)
            prompt_in_delta = self._source_mentions_prompt(observation.raw_log_delta, prompt)
            delta_observed = bool(clean_ansi(observation.raw_log_delta).strip())
            phase_observed = self.provider_phase in {
                ProviderPhase.PROCESSING,
                ProviderPhase.COMPLETED_RESPONSE,
                ProviderPhase.WAITING_INPUT,
                ProviderPhase.IDLE_READY,
            }
            submission_observed = submission_observed or prompt_visible or prompt_in_delta or delta_observed

            if submission_observed and phase_observed:
                self.current_command = current_command
                self.current_path = observation.current_path
                self.last_heartbeat_at = observation.observed_at
                self._log_event("prompt_submitted", phase=self.provider_phase.value)
                return observation

            if (
                    not submission_observed
                    and not extra_enter_sent
                    and time.monotonic() - submit_started_at >= 3.0
                    and self.provider_phase in {ProviderPhase.WAITING_INPUT, ProviderPhase.IDLE_READY,
                                                ProviderPhase.UNKNOWN}
            ):
                self.send_special_key("Enter")
                extra_enter_sent = True
                self._log_event("prompt_extra_enter", phase=self.provider_phase.value)

            time.sleep(0.5)

        raise TimeoutError(f"等待智能体确认收到 prompt 超时:\n{clean_ansi(self.capture_visible(200))[-4000:]}")

    def wait_for_turn_artifacts(
            self,
            *,
            contract: TurnFileContract,
            task_status_path: Path | None = None,
            timeout_sec: float,
    ) -> TurnFileResult:
        deadline = time.monotonic() + timeout_sec
        stable_signature: tuple[object, ...] | None = None
        stable_since_monotonic = 0.0
        status_done_seen = task_status_path is None

        while time.monotonic() < deadline:
            observation = self.observe(tail_lines=220)
            if not observation.session_exists:
                raise RuntimeError("tmux pane exited while waiting for turn artifacts")
            if observation.pane_dead:
                raise RuntimeError(f"tmux pane died while waiting for turn artifacts:\n{self.capture_visible(160)}")

            status_done_seen = self._track_task_completion_signal(
                task_status_path=task_status_path,
                status_done_seen=status_done_seen,
            )

            try:
                file_result = contract.validator(contract.status_path)
            except Exception:
                stable_signature = None
                stable_since_monotonic = 0.0
                if self._maybe_send_task_completion_nudge(
                    current_command=observation.current_command,
                    visible_text=observation.visible_text,
                    task_status_path=task_status_path,
                ):
                    time.sleep(0.5)
                    continue
                time.sleep(0.5)
                continue

            status_stat = contract.status_path.stat()
            signature = (
                status_stat.st_size,
                status_stat.st_mtime,
                tuple(sorted(file_result.artifact_hashes.items())),
            )
            if signature == stable_signature:
                stable_elapsed = time.monotonic() - stable_since_monotonic if stable_since_monotonic else 0.0
            else:
                stable_signature = signature
                stable_since_monotonic = time.monotonic()
                stable_elapsed = 0.0

            self.last_heartbeat_at = observation.observed_at
            if stable_elapsed >= max(float(contract.quiet_window_sec), 0.0) and status_done_seen:
                self._log_event(
                    "turn_artifacts_ready",
                    turn_id=contract.turn_id,
                    phase=contract.phase,
                    status_path=str(contract.status_path),
                )
                return file_result
            if (
                    stable_elapsed >= max(float(contract.quiet_window_sec), 0.0)
                    and not status_done_seen
                    and self._can_finalize_turn_artifacts_without_helper(observation)
            ):
                if task_status_path is not None:
                    self._write_task_status_file(task_status_path, status="done")
                self.current_task_runtime_status = "done"
                self.current_command = observation.current_command
                self.current_path = observation.current_path
                self._log_event(
                    "turn_artifacts_ready_without_helper",
                    turn_id=contract.turn_id,
                    phase=contract.phase,
                    status_path=str(contract.status_path),
                )
                return file_result
            if self._maybe_send_task_completion_nudge(
                current_command=observation.current_command,
                visible_text=observation.visible_text,
                task_status_path=task_status_path,
            ):
                time.sleep(0.5)
                continue
            if observation.current_command in SHELL_COMMANDS:
                self.agent_ready = False
                raise RuntimeError(
                    f"agent exited back to shell while waiting for turn artifacts:\n{observation.visible_text}")
            time.sleep(0.5)

        raise TimeoutError(
            f"等待 turn 文件结果超时: phase={contract.phase} status_path={contract.status_path}\n"
            f"{clean_ansi(self.capture_visible(200))[-4000:]}"
        )

    def wait_for_task_result(
            self,
            *,
            contract: TaskResultContract,
            task_status_path: Path | None,
            result_path: Path,
            timeout_sec: float,
    ) -> TaskResultFile:
        deadline = time.monotonic() + timeout_sec
        stable_signature: tuple[object, ...] | None = None
        stable_hits = 0
        status_done_seen = task_status_path is None

        while time.monotonic() < deadline:
            observation = self.observe(tail_lines=220)
            if not observation.session_exists:
                raise RuntimeError("tmux pane exited while waiting for task result")
            if observation.pane_dead:
                raise RuntimeError(f"tmux pane died while waiting for task result:\n{self.capture_visible(160)}")

            status_done_seen = self._track_task_completion_signal(
                task_status_path=task_status_path,
                status_done_seen=status_done_seen,
            )
            if self._maybe_send_task_completion_nudge(
                current_command=observation.current_command,
                visible_text=observation.visible_text,
                task_status_path=task_status_path,
            ):
                time.sleep(0.5)
                continue

            try:
                result_file = self._validate_task_result_file(
                    contract=contract,
                    result_path=result_path,
                )
            except Exception:
                stable_signature = None
                stable_hits = 0
                if not result_path.exists() and self._can_finalize_turn_artifacts_without_helper(observation):
                    terminal_result = self._match_terminal_task_result_status(
                        contract=contract,
                        observation=observation,
                    )
                    if terminal_result is not None:
                        matched_status, matched_summary = terminal_result
                        result_file = self._materialize_task_result_without_helper(
                            contract=contract,
                            result_path=result_path,
                            status=matched_status,
                            summary=matched_summary,
                        )
                        if task_status_path is not None:
                            self._write_task_status_file(task_status_path, status="done")
                        self.current_task_runtime_status = "done"
                        self.current_command = observation.current_command
                        self.current_path = observation.current_path
                        self._log_event(
                            "task_result_ready_without_helper",
                            turn_id=contract.turn_id,
                            phase=contract.phase,
                            result_path=str(result_path),
                            status=matched_status,
                        )
                        return result_file
                time.sleep(0.5)
                continue

            result_stat = result_path.stat()
            signature = (
                result_stat.st_size,
                result_stat.st_mtime,
                str(result_file.payload.get("status", "")),
                tuple(sorted(result_file.artifact_hashes.items())),
            )
            if signature == stable_signature:
                stable_hits += 1
            else:
                stable_signature = signature
                stable_hits = 1

            self.last_heartbeat_at = observation.observed_at
            if stable_hits >= 2 and status_done_seen:
                self._log_event(
                    "task_result_ready",
                    turn_id=contract.turn_id,
                    phase=contract.phase,
                    result_path=str(result_path),
                    status=str(result_file.payload.get("status", "")),
                )
                return result_file
            if observation.current_command in SHELL_COMMANDS:
                self.agent_ready = False
                raise RuntimeError(
                    f"agent exited back to shell while waiting for task result:\n{observation.visible_text}"
                )
            time.sleep(0.5)

        raise TimeoutError(
            f"等待任务结果超时: phase={contract.phase} result_path={result_path}\n"
            f"{clean_ansi(self.capture_visible(200))[-4000:]}"
        )

    def _wait_for_shell_ready(self, timeout_sec: float = 12.0) -> None:
        deadline = time.monotonic() + timeout_sec
        previous_output = ""
        stable_count = 0
        while time.monotonic() < deadline:
            observation = self.observe(tail_lines=120)
            if not observation.session_exists:
                raise RuntimeError("tmux pane exited before shell became ready")
            current_command = observation.current_command
            current_path = observation.current_path
            visible = observation.raw_log_tail or observation.visible_text
            if current_command in SHELL_COMMANDS and current_path == str(self.work_dir):
                if visible == previous_output and visible.strip():
                    stable_count += 1
                else:
                    stable_count = 0
                if stable_count >= 1:
                    return
            previous_output = visible
            time.sleep(0.4)
        raise RuntimeError(f"Shell initialization timed out.\n{self.capture_visible(120)}")

    def _boot_action_allowed(self, action_signature: str, cooldown_sec: float = 3.0) -> bool:
        if (
            action_signature == self._last_boot_action_signature
            and time.monotonic() - self._last_boot_action_at < cooldown_sec
        ):
            return False
        self._last_boot_action_signature = action_signature
        self._last_boot_action_at = time.monotonic()
        return True

    def _maybe_handle_codex_boot_prompt(self, visible_text: str) -> bool:
        if self.config.vendor != Vendor.CODEX:
            return False
        recent_output = "\n".join(str(visible_text or "").splitlines()[-80:])
        if re.search(r"Press enter to continue", recent_output, re.IGNORECASE) and any(
                re.search(pattern, recent_output, re.IGNORECASE) for pattern in CODEX_TRUST_PROMPT_PATTERNS
        ):
            action_signature = f"codex-trust:{hashlib.sha1(recent_output.encode('utf-8')).hexdigest()[:12]}"
            if not self._boot_action_allowed(action_signature):
                return False
            self.send_special_key("Enter")
            return True
        if all(re.search(pattern, recent_output, re.IGNORECASE) for pattern in CODEX_UPDATE_PROMPT_PATTERNS):
            action_signature = f"codex-update:{hashlib.sha1(recent_output.encode('utf-8')).hexdigest()[:12]}"
            if not self._boot_action_allowed(action_signature):
                return False
            self.send_special_key("Down")
            time.sleep(0.1)
            self.send_special_key("Down")
            time.sleep(0.1)
            self.send_special_key("Enter")
            return True
        if all(re.search(pattern, recent_output, re.IGNORECASE) for pattern in CODEX_MODEL_SELECTION_PROMPT_PATTERNS):
            action_signature = f"codex-model:{hashlib.sha1(recent_output.encode('utf-8')).hexdigest()[:12]}"
            if not self._boot_action_allowed(action_signature):
                return False
            self.send_special_key("Down")
            time.sleep(0.1)
            self.send_special_key("Enter")
            return True
        return False

    def _maybe_handle_gemini_boot_prompt(self, visible_text: str) -> bool:
        if self.config.vendor != Vendor.GEMINI:
            return False
        recent_output = "\n".join(str(visible_text or "").splitlines()[-80:])
        if not all(re.search(pattern, recent_output, re.IGNORECASE) for pattern in GEMINI_TRUST_PROMPT_PATTERNS):
            return False
        action_signature = f"gemini-trust:{hashlib.sha1(recent_output.encode('utf-8')).hexdigest()[:12]}"
        if not self._boot_action_allowed(action_signature):
            return False
        self.send_special_key("Enter")
        return True

    def _maybe_handle_kimi_boot_prompt(self, visible_text: str) -> bool:
        if self.config.vendor != Vendor.KIMI:
            return False
        recent_output = "\n".join(str(visible_text or "").splitlines()[-80:])
        if not all(re.search(pattern, recent_output, re.IGNORECASE) for pattern in KIMI_UPDATE_PROMPT_PATTERNS):
            return False
        action_signature = "kimi-update"
        if not self._boot_action_allowed(action_signature):
            return False
        self.send_special_key("q")
        return True

    def _build_ready_signature(self, visible_text: str) -> str:
        lines: list[str] = []
        for raw_line in clean_ansi(visible_text).splitlines():
            text = raw_line.strip()
            if not text or is_runtime_noise_line(text):
                continue
            lines.append(text)
        return "\n".join(lines[-6:])

    def _task_runtime_dir(self) -> Path:
        path = self.runtime_dir / "task_runtime"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _build_task_runtime_basename(self, *, label: str, attempt: int) -> str:
        session_fragment = _sanitize_task_runtime_fragment(
            self.session_name or self.worker_id,
            fallback=_slugify(self.session_name or self.worker_id, max_len=72),
            max_len=72,
        )
        label_fragment = _sanitize_task_runtime_fragment(
            label,
            fallback=_slugify(label, max_len=32),
            max_len=32,
        )
        return f"{session_fragment}_{label_fragment}_attempt_{attempt}"

    def _build_task_status_path(self, *, label: str, attempt: int) -> Path:
        return self._task_runtime_dir() / f"{self._build_task_runtime_basename(label=label, attempt=attempt)}.json"

    def _build_task_manifest_path(self, *, label: str, attempt: int) -> Path:
        return self._task_runtime_dir() / f"{self._build_task_runtime_basename(label=label, attempt=attempt)}_manifest.json"

    def _build_task_result_path(self, *, label: str, attempt: int) -> Path:
        return self._task_runtime_dir() / f"{self._build_task_runtime_basename(label=label, attempt=attempt)}_result.json"

    def _task_completion_helper_module_path(self) -> Path:
        return Path(__file__).resolve().parent / "T07_task_completion.py"

    def _build_task_completion_helper_path(self, *, label: str, attempt: int) -> Path:
        return self._task_runtime_dir() / (
            f"{self._build_task_runtime_basename(label=label, attempt=attempt)}_complete_task"
        )

    def _write_task_completion_helper(
            self,
            *,
            label: str,
            attempt: int,
            task_status_path: Path,
            manifest_path: Path | None = None,
    ) -> tuple[Path, str]:
        helper_path = self._build_task_completion_helper_path(label=label, attempt=attempt)
        helper_module = self._task_completion_helper_module_path()
        python_executable = shlex.quote(SYSTEM_PYTHON_PATH)
        manifest_args = (
            f"--manifest-path {shlex.quote(str(manifest_path))}"
            if manifest_path is not None else
            f"--task-status-path {shlex.quote(str(task_status_path))}"
        )
        script_body = f"""#!/bin/sh
set -eu
status="done"
if [ "${{1:-}}" = "--status" ]; then
  status="${{2:-done}}"
fi
exec {python_executable} {shlex.quote(str(helper_module))} {manifest_args} --status "$status"
"""
        helper_path.write_text(script_body, encoding="utf-8")
        helper_path.chmod(0o755)
        command = f"{shlex.quote(str(helper_path))} --status done"
        return helper_path, command

    def _write_task_manifest_file(
            self,
            *,
            task_status_path: Path,
            manifest_path: Path,
            result_path: Path,
            contract: TaskResultContract,
    ) -> None:
        payload = {
            "schema_version": "1.0",
            "stage_name": contract.stage_name,
            "turn_id": contract.turn_id,
            "phase": contract.phase,
            "task_kind": contract.task_kind,
            "mode": contract.mode,
            "task_status_path": str(task_status_path.resolve()),
            "result_path": str(result_path.resolve()),
            "turn_status_path": str(contract.turn_status_path.resolve()) if contract.turn_status_path else "",
            "stage_status_path": str(contract.stage_status_path.resolve()) if contract.stage_status_path else "",
            "required_artifacts": {
                key: str(path.resolve()) for key, path in contract.required_artifacts.items()
            },
            "optional_artifacts": {
                key: str(path.resolve()) for key, path in contract.optional_artifacts.items()
            },
            "artifact_rules": contract.artifact_rules,
            "retry_policy": contract.retry_policy,
            "resume_policy": contract.resume_policy,
        }
        target = manifest_path.expanduser().resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = target.with_suffix(target.suffix + ".tmp")
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_path.replace(target)

    @staticmethod
    def _write_task_status_file(path: str | Path, *, status: str) -> None:
        target = Path(path).expanduser().resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = target.with_suffix(target.suffix + ".tmp")
        tmp_path.write_text(json.dumps({"status": status}, ensure_ascii=False), encoding="utf-8")
        tmp_path.replace(target)

    @staticmethod
    def _read_task_status_file(path: str | Path) -> str:
        target = Path(path).expanduser().resolve()
        if not target.exists():
            return ""
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
        except Exception:
            return ""
        if payload == {"status": "running"}:
            return "running"
        if payload == {"status": "done"}:
            return "done"
        return ""

    @staticmethod
    def _read_task_result_file(path: str | Path) -> dict[str, Any]:
        target = Path(path).expanduser().resolve()
        if not target.exists():
            return {}
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
        except Exception:
            return {}
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _write_task_result_file(path: str | Path, payload: dict[str, Any]) -> None:
        target = Path(path).expanduser().resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = target.with_suffix(target.suffix + ".tmp")
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_path.replace(target)

    def _validate_task_result_file(
            self,
            *,
            contract: TaskResultContract,
            result_path: Path,
    ) -> TaskResultFile:
        payload = self._read_task_result_file(result_path)
        if not payload:
            raise FileNotFoundError(f"缺少 result.json: {result_path}")
        if str(payload.get("schema_version", "")).strip() != "1.0":
            raise ValueError("result.json schema_version 非法")
        if str(payload.get("turn_id", "")).strip() != contract.turn_id:
            raise ValueError("result.json turn_id 非法")
        if str(payload.get("phase", "")).strip() != contract.phase:
            raise ValueError("result.json phase 非法")
        if str(payload.get("task_kind", "")).strip() != contract.task_kind:
            raise ValueError("result.json task_kind 非法")
        status = str(payload.get("status", "")).strip()
        if contract.expected_statuses and status not in contract.expected_statuses:
            raise ValueError(f"result.json status 非法: {status}")
        if not isinstance(payload.get("summary", ""), str):
            raise ValueError("result.json summary 必须是字符串")
        artifacts = payload.get("artifacts", {})
        artifact_hashes = payload.get("artifact_hashes", {})
        if not isinstance(artifacts, dict):
            raise ValueError("result.json artifacts 必须是对象")
        if not isinstance(artifact_hashes, dict):
            raise ValueError("result.json artifact_hashes 必须是对象")
        for alias, required_path in contract.required_artifacts.items():
            resolved_required = str(required_path.resolve())
            actual_path = str(artifacts.get(alias, "")).strip()
            if not actual_path:
                raise ValueError(f"result.json 缺少必填 artifact: {alias}")
            if str(Path(actual_path).expanduser().resolve()) != resolved_required:
                raise ValueError(f"result.json artifact 路径非法: {alias}")
        for alias, artifact_path in artifacts.items():
            resolved = Path(str(artifact_path)).expanduser().resolve()
            if not resolved.exists():
                raise FileNotFoundError(f"result.json 引用的文件不存在: {resolved}")
            expected_hash = str(artifact_hashes.get(str(resolved), "")).strip()
            if not expected_hash:
                raise ValueError(f"result.json 缺少 artifact_hashes: {resolved}")
            if expected_hash != _build_prefixed_sha256(resolved):
                raise ValueError(f"result.json artifact_hashes 不匹配: {resolved}")
        return TaskResultFile(
            result_path=str(result_path.resolve()),
            payload=payload,
            artifact_paths={str(key): str(value) for key, value in artifacts.items()},
            artifact_hashes={str(key): str(value) for key, value in artifact_hashes.items()},
            validated_at=_now_iso(),
        )

    @staticmethod
    def _collect_contract_artifacts(contract: TaskResultContract) -> tuple[dict[str, str], dict[str, str]]:
        artifacts: dict[str, str] = {}
        artifact_hashes: dict[str, str] = {}
        for alias, artifact_path in contract.required_artifacts.items():
            resolved = artifact_path.expanduser().resolve()
            if not resolved.exists():
                raise FileNotFoundError(f"缺少必填 artifact: {resolved}")
            artifacts[alias] = str(resolved)
            artifact_hashes[str(resolved)] = _build_prefixed_sha256(resolved)
        for alias, artifact_path in contract.optional_artifacts.items():
            resolved = artifact_path.expanduser().resolve()
            if not resolved.exists():
                continue
            artifacts[alias] = str(resolved)
            artifact_hashes[str(resolved)] = _build_prefixed_sha256(resolved)
        return artifacts, artifact_hashes

    def _match_terminal_task_result_status(
            self,
            *,
            contract: TaskResultContract,
            observation: WorkerObservation,
    ) -> tuple[str, str] | None:
        if not contract.terminal_status_tokens:
            return None
        visible_message = clean_ansi(self._extract_last_message(observation.visible_text)).strip()
        if not visible_message:
            return None
        for status, tokens in contract.terminal_status_tokens.items():
            for token in tokens:
                if token and token in visible_message:
                    summary = contract.terminal_status_summaries.get(status, visible_message)
                    return status, summary
        return None

    def _materialize_task_result_without_helper(
            self,
            *,
            contract: TaskResultContract,
            result_path: Path,
            status: str,
            summary: str,
    ) -> TaskResultFile:
        artifacts, artifact_hashes = self._collect_contract_artifacts(contract)
        payload = {
            "schema_version": "1.0",
            "turn_id": contract.turn_id,
            "phase": contract.phase,
            "task_kind": contract.task_kind,
            "status": status,
            "summary": summary,
            "artifacts": artifacts,
            "artifact_hashes": artifact_hashes,
            "written_at": _now_iso(),
        }
        self._write_task_result_file(result_path, payload)
        return self._validate_task_result_file(contract=contract, result_path=result_path)

    def _track_task_completion_signal(
            self,
            *,
            task_status_path: Path | None,
            status_done_seen: bool,
    ) -> bool:
        if task_status_path is None:
            return True
        if not status_done_seen:
            current_status = self._read_task_status_file(task_status_path)
            self.current_task_runtime_status = current_status
            if current_status == "done":
                self._log_event("task_status_done", task_status_path=str(task_status_path))
                return True
        return status_done_seen

    def _can_finalize_turn_artifacts_without_helper(self, observation: WorkerObservation) -> bool:
        current_command = str(observation.current_command or "").strip()
        if current_command in SHELL_COMMANDS:
            return False
        if not self._agent_running(current_command):
            return False
        ready_visible = self._phase_accepts_followup_prompt(self.provider_phase)
        if observation.visible_text and self._visible_indicates_agent_ready(observation.visible_text):
            ready_visible = True
        if not ready_visible:
            return False
        if self.terminal_recently_changed:
            return False
        return True

    def _reset_task_completion_nudge_state(self) -> None:
        self.current_task_completion_nudge_count = 0
        self.current_task_completion_nudge_at = ""
        self._current_task_last_nudge_monotonic = 0.0

    def _terminal_idle_duration_sec(self) -> float:
        if not self.last_terminal_signature or not self._last_terminal_change_monotonic:
            return 0.0
        return max(0.0, time.monotonic() - self._last_terminal_change_monotonic)

    def _build_task_completion_nudge_prompt(self, complete_task_command: str) -> str:
        return (
            "仅当你已经完成当前任务但尚未收尾时：执行以下命令并立刻停止；"
            "否则继续当前任务，不要回复。\n"
            f"{complete_task_command}"
        )

    def _maybe_send_task_completion_nudge(
            self,
            *,
            current_command: str,
            visible_text: str,
            task_status_path: Path | None,
    ) -> bool:
        if task_status_path is None:
            return False
        if self.current_task_runtime_status != "running":
            return False
        if self.current_task_completion_nudge_count >= self.task_completion_nudge_max_count:
            return False
        if not self.current_task_completion_command:
            return False
        if not self._agent_running(current_command):
            return False
        if visible_text and self._visible_indicates_agent_starting(visible_text):
            return False

        ready_visible = self._phase_accepts_followup_prompt(self.provider_phase)
        if visible_text and self._visible_indicates_agent_ready(visible_text):
            ready_visible = True
        if not ready_visible:
            return False
        if self.terminal_recently_changed:
            return False
        if self._terminal_idle_duration_sec() < self.task_completion_nudge_idle_sec:
            return False

        now_monotonic = time.monotonic()
        if (
            self._current_task_last_nudge_monotonic
            and now_monotonic - self._current_task_last_nudge_monotonic < self.task_completion_nudge_grace_sec
        ):
            return False

        self._send_text(self._build_task_completion_nudge_prompt(self.current_task_completion_command))
        self.current_task_completion_nudge_count += 1
        self.current_task_completion_nudge_at = _now_iso()
        self._current_task_last_nudge_monotonic = now_monotonic
        self.wrapper_state = WrapperState.NOT_READY
        self._log_event(
            "task_completion_nudge_sent",
            task_status_path=str(task_status_path),
            nudge_count=self.current_task_completion_nudge_count,
        )
        self._write_state(
            WorkerStatus.RUNNING,
            note="task_completion_nudge",
            extra={
                "result_status": "running",
                "current_task_status_path": str(task_status_path),
                "current_task_manifest_path": self.current_task_manifest_path,
                "current_task_result_path": self.current_task_result_path,
                "current_task_completion_command": self.current_task_completion_command,
                "current_task_runtime_status": self.current_task_runtime_status,
                "current_task_completion_nudge_count": self.current_task_completion_nudge_count,
                "current_task_completion_nudge_at": self.current_task_completion_nudge_at,
            },
        )
        return True

    @staticmethod
    def _build_terminal_signature(terminal_text: str) -> str:
        normalized_lines = [line.rstrip() for line in clean_ansi(terminal_text).splitlines()]
        while normalized_lines and not normalized_lines[-1].strip():
            normalized_lines.pop()
        payload = "\n".join(normalized_lines[-160:]).strip()
        if not payload:
            return ""
        return hashlib.sha1(payload.encode("utf-8")).hexdigest()

    def _reset_terminal_activity(self) -> None:
        self.last_terminal_signature = ""
        self.last_terminal_changed_at = ""
        self.terminal_recently_changed = False
        self._last_terminal_change_monotonic = 0.0

    def _update_terminal_activity(self, terminal_text: str, *, observed_at: str) -> None:
        now = time.monotonic()
        signature = self._build_terminal_signature(terminal_text)
        if signature != self.last_terminal_signature:
            self.last_terminal_signature = signature
            self.last_terminal_changed_at = observed_at if signature else ""
            self._last_terminal_change_monotonic = now if signature else 0.0
        elif signature and not self.last_terminal_changed_at:
            self.last_terminal_changed_at = observed_at
            self._last_terminal_change_monotonic = now
        self.terminal_recently_changed = bool(signature) and bool(self._last_terminal_change_monotonic) and (
            now - self._last_terminal_change_monotonic < TERMINAL_ACTIVITY_IDLE_WINDOW_SEC
        )

    def _agent_running(self, current_command: str) -> bool:
        if current_command in self.config.expected_current_commands():
            return True
        return bool(current_command) and current_command not in SHELL_COMMANDS

    def _visible_indicates_agent_starting(self, visible_text: str) -> bool:
        recent_output = "\n".join(str(visible_text or "").splitlines()[-120:])
        if not recent_output.strip():
            return False
        if self.config.vendor == Vendor.CODEX:
            if (
                    any(re.search(pattern, recent_output, re.IGNORECASE | re.MULTILINE) for pattern in CODEX_READY_PATTERNS)
                    and not any(re.search(pattern, recent_output, re.IGNORECASE) for pattern in CODEX_STARTING_PATTERNS)
                    and not any(re.search(pattern, recent_output, re.IGNORECASE) for pattern in CODEX_TRUST_PROMPT_PATTERNS)
                    and not any(re.search(pattern, recent_output, re.IGNORECASE) for pattern in CODEX_MODEL_SELECTION_PROMPT_PATTERNS)
            ):
                return False
            return any(
                re.search(pattern, recent_output, re.IGNORECASE)
                for pattern in (
                    *CODEX_TRUST_PROMPT_PATTERNS,
                    *CODEX_UPDATE_PROMPT_PATTERNS,
                    *CODEX_MODEL_SELECTION_PROMPT_PATTERNS,
                    *CODEX_STARTING_PATTERNS,
                )
            )
        if self.config.vendor == Vendor.GEMINI:
            return any(
                re.search(pattern, recent_output, re.IGNORECASE)
                for pattern in (
                    *GEMINI_TRUST_PROMPT_PATTERNS,
                    *GEMINI_NOT_READY_PATTERNS,
                )
            )
        if self.config.vendor == Vendor.KIMI:
            return any(
                re.search(pattern, recent_output, re.IGNORECASE)
                for pattern in (
                    *KIMI_NOT_READY_PATTERNS,
                    *KIMI_UPDATE_PROMPT_PATTERNS,
                )
            )
        return False

    def _visible_indicates_agent_ready(self, visible_text: str) -> bool:
        if self.config.vendor == Vendor.CODEX:
            recent_output = "\n".join(str(visible_text or "").splitlines()[-120:])
            if any(re.search(pattern, recent_output, re.IGNORECASE) for pattern in CODEX_TRUST_PROMPT_PATTERNS):
                return False
            if any(re.search(pattern, recent_output, re.IGNORECASE) for pattern in
                   CODEX_MODEL_SELECTION_PROMPT_PATTERNS):
                return False
            if any(re.search(pattern, recent_output, re.IGNORECASE) for pattern in CODEX_STARTING_PATTERNS):
                return False
            if any(re.search(pattern, recent_output, re.IGNORECASE) for pattern in CODEX_PROCESSING_PATTERNS):
                return False
            ready_detected = any(
                re.search(pattern, recent_output, re.IGNORECASE | re.MULTILINE)
                for pattern in CODEX_READY_PATTERNS
            )
            if ready_detected:
                return True
            if any(re.search(pattern, recent_output, re.IGNORECASE) for pattern in CODEX_UPDATE_PROMPT_PATTERNS):
                return False
            return False
        if self.config.vendor == Vendor.GEMINI:
            recent_output = "\n".join(str(visible_text or "").splitlines()[-120:])
            if any(re.search(pattern, recent_output, re.IGNORECASE) for pattern in GEMINI_TRUST_PROMPT_PATTERNS):
                return False
            if any(re.search(pattern, recent_output, re.IGNORECASE) for pattern in GEMINI_NOT_READY_PATTERNS):
                return False
            return any(re.search(pattern, recent_output, re.IGNORECASE) for pattern in
                       GEMINI_INPUT_BOX_PATTERNS + GEMINI_READY_PATTERNS)
        if self.config.vendor == Vendor.QWEN:
            recent_output = "\n".join(str(visible_text or "").splitlines()[-120:])
            return any(re.search(pattern, recent_output, re.IGNORECASE) for pattern in
                       QWEN_INPUT_BOX_PATTERNS + QWEN_READY_PATTERNS)
        if self.config.vendor == Vendor.KIMI:
            recent_output = "\n".join(str(visible_text or "").splitlines()[-120:])
            if any(re.search(pattern, recent_output, re.IGNORECASE) for pattern in KIMI_NOT_READY_PATTERNS):
                return False
            return bool(re.search(r"^\s*>\s*$", recent_output, re.MULTILINE))
        return True

    @staticmethod
    def _phase_accepts_followup_prompt(phase: ProviderPhase) -> bool:
        return phase in {
            ProviderPhase.WAITING_INPUT,
            ProviderPhase.IDLE_READY,
            ProviderPhase.COMPLETED_RESPONSE,
        }

    def _infer_wrapper_state(
            self,
            *,
            current_command: str,
            visible_text: str,
    ) -> WrapperState:
        if not self._agent_running(current_command):
            return WrapperState.NOT_READY
        if visible_text and self._visible_indicates_agent_starting(visible_text):
            return WrapperState.NOT_READY
        ready_visible = self._phase_accepts_followup_prompt(self.provider_phase)
        if visible_text and self._visible_indicates_agent_ready(visible_text):
            ready_visible = True
        if not ready_visible:
            return WrapperState.NOT_READY
        if self.terminal_recently_changed:
            return WrapperState.NOT_READY
        return WrapperState.READY

    def _wait_for_agent_ready(self, timeout_sec: float = 60.0) -> None:
        deadline = time.monotonic() + timeout_sec
        previous_ready_signature = ""
        stable_count = 0
        while time.monotonic() < deadline:
            observation = self.observe(tail_lines=220)
            if not observation.session_exists:
                raise RuntimeError("tmux pane exited while agent was starting")
            if observation.pane_dead:
                raise RuntimeError(f"tmux pane died while agent was starting:\n{self.capture_visible(120)}")

            current_command = observation.current_command
            visible = observation.raw_log_tail or observation.visible_text
            fallback_visible = observation.visible_text
            if (
                self._maybe_handle_codex_boot_prompt(visible)
                or self._maybe_handle_codex_boot_prompt(fallback_visible)
                or self._maybe_handle_gemini_boot_prompt(visible)
                or self._maybe_handle_gemini_boot_prompt(fallback_visible)
                or self._maybe_handle_kimi_boot_prompt(visible)
                or self._maybe_handle_kimi_boot_prompt(fallback_visible)
            ):
                self.wrapper_state = WrapperState.NOT_READY
                time.sleep(0.6)
                previous_ready_signature = ""
                stable_count = 0
                continue

            if self._agent_running(current_command):
                ready_visible = self._phase_accepts_followup_prompt(self.provider_phase)
                ready_signature = ""
                if self._visible_indicates_agent_ready(fallback_visible):
                    ready_visible = True
                    ready_signature = self._build_ready_signature(fallback_visible)
                elif self._visible_indicates_agent_ready(visible):
                    ready_visible = True
                    ready_signature = self._build_ready_signature(visible)
                elif ready_visible:
                    ready_signature = f"phase:{self.provider_phase.value}:{current_command}"

                if ready_signature and ready_signature == previous_ready_signature:
                    stable_count += 1
                else:
                    stable_count = 0
                if stable_count >= 2 and ready_visible and not self.terminal_recently_changed:
                    self.agent_ready = True
                    self.wrapper_state = WrapperState.READY
                    self.current_command = current_command
                    self.current_path = observation.current_path
                    self.last_heartbeat_at = observation.observed_at
                    self._write_state(
                        WorkerStatus.READY,
                        note="agent_ready",
                        extra={"current_command": current_command, "current_path": observation.current_path},
                    )
                    return
            elif current_command in SHELL_COMMANDS and previous_ready_signature and previous_ready_signature == self._build_ready_signature(visible):
                raise RuntimeError(f"agent exited back to shell while starting:\n{visible}")

            previous_ready_signature = ready_signature if self._agent_running(current_command) else ""
            time.sleep(0.5)

        raise RuntimeError(f"Timed out waiting for agent ready.\n{self.capture_visible(240)}")

    def launch_agent(self, timeout_sec: float = 60.0) -> None:
        last_error: Exception | None = None
        for attempt in range(1, 4):
            try:
                with self.launch_coordinator.startup_slot(self.config.vendor):
                    if not self.pane_id or not self.target_exists():
                        self.create_session()
                    else:
                        self._ensure_health_supervisor_started()
                    self._wait_for_shell_ready()
                    self.provider_phase = ProviderPhase.BOOTING
                    self.wrapper_state = WrapperState.NOT_READY
                    self._append_transcript("launch / command", f"```bash\n{self.launch_command}\n```")
                    self._log_event("launch_attempt", attempt=attempt, vendor=self.config.vendor.value)
                    self._send_text(self.launch_command, enter_count=1)
                    self._wait_for_agent_ready(timeout_sec=timeout_sec)
                self.launch_coordinator.record_launch_result(self.config.vendor, success=True)
                return
            except Exception as error:
                last_error = error
                self.launch_coordinator.record_launch_result(self.config.vendor, success=False)
                self.provider_phase = ProviderPhase.ERROR
                self.agent_ready = False
                self.wrapper_state = WrapperState.NOT_READY
                self._log_event("launch_failed", attempt=attempt, error=str(error))
                if attempt >= 3:
                    break
                if self.session_exists():
                    try:
                        self._stop_health_supervisor()
                        self.backend.kill_session(self.session_name)
                    except Exception:
                        pass
                self.pane_id = ""
        if last_error is not None:
            raise last_error

    def ensure_agent_ready(self, timeout_sec: float = 60.0) -> None:
        if self.session_exists() and self.pane_id:
            self._ensure_health_supervisor_started()
        if not self.pane_id or not self.target_exists():
            self.provider_phase = ProviderPhase.RECOVERING
            self.wrapper_state = WrapperState.NOT_READY
            self._log_event("ensure_ready_relaunch", reason="missing_pane")
            self.launch_agent(timeout_sec=timeout_sec)
            return

        current_command = self.pane_current_command()
        if self.agent_ready and self._agent_running(
                current_command) and not self.pane_dead() and self.provider_phase in {
            ProviderPhase.WAITING_INPUT,
            ProviderPhase.IDLE_READY,
            ProviderPhase.COMPLETED_RESPONSE,
        }:
            self.wrapper_state = WrapperState.READY
            return

        if current_command in SHELL_COMMANDS or self.provider_phase == ProviderPhase.SHELL:
            self.provider_phase = ProviderPhase.RECOVERING
            self.wrapper_state = WrapperState.NOT_READY
            self._log_event("ensure_ready_relaunch", reason="shell_fallback")
            self.launch_agent(timeout_sec=timeout_sec)
            return

        self._wait_for_agent_ready(timeout_sec=timeout_sec)

    def _build_turn_prompt(
            self,
            prompt: str,
            turn_token: str,
            required_tokens: Sequence[str],
            *,
            task_status_path: Path,
            complete_task_command: str,
            include_turn_protocol: bool,
    ) -> str:
        sections = [str(prompt or "").strip()]
        if include_turn_protocol:
            if required_tokens:
                turn_protocol_prompt = f"""Turn completion protocol:
- Output exactly `{turn_token}` on its own line after the substantive answer.
- Keep the workflow-required token as the final workflow token before the runtime completion marker.
"""
            else:
                turn_protocol_prompt = f"""Turn completion protocol:
- Output exactly `{turn_token}` on its own line after the substantive answer.
- If no workflow token is required, `{turn_token}` must be the final workflow token before the runtime completion marker.
"""
            sections.append(turn_protocol_prompt)
        sections.append(
            build_task_completion_runtime_prompt(
                complete_task_command=complete_task_command,
            )
        )
        return "\n\n".join(part for part in sections if part)

    @staticmethod
    def _strip_turn_token(text: str, turn_token: str) -> str:
        lines = [line.rstrip() for line in str(text or "").splitlines()]
        kept = [line for line in lines if line.strip() != turn_token]
        return "\n".join(kept).strip()

    @staticmethod
    def _truncate_source_at_completion_token(source: str, turn_token: str, required_tokens: Sequence[str]) -> str:
        lines = clean_ansi(source).splitlines()
        token_index = -1
        for index, line in enumerate(lines):
            if _extract_protocol_token_from_line(line, [turn_token]) == turn_token:
                token_index = index
        if token_index < 0:
            return clean_ansi(source)
        completion_index = token_index
        if required_tokens:
            for index in range(token_index + 1, len(lines)):
                if _extract_protocol_token_from_line(lines[index], required_tokens):
                    completion_index = index
        return "\n".join(lines[:completion_index + 1])

    def _extract_last_message(self, visible_text: str) -> str:
        return self.detector.extract_last_message(visible_text)

    def _extract_audit_reply_from_source(
            self,
            source: str,
            *,
            turn_token: str,
            required_tokens: Sequence[str],
    ) -> str:
        clean_source = clean_ansi(source)
        if turn_token not in clean_source:
            return ""

        lines = clean_source.splitlines()
        allowed_tokens = set(required_tokens)
        turn_index = -1
        for index, line in enumerate(lines):
            if _extract_protocol_token_from_line(line, [turn_token]) == turn_token:
                turn_index = index
        if turn_index < 0:
            return ""

        token_positions = [
            index
            for index in range(turn_index + 1, len(lines))
            if _extract_protocol_token_from_line(lines[index], allowed_tokens)
        ]
        if not token_positions:
            return ""
        token_index = token_positions[-1]
        final_token = _extract_protocol_token_from_line(lines[token_index], allowed_tokens)
        if final_token == "[[ROUTING_AUDIT:WRITTEN]]":
            return "\n".join([turn_token, final_token])

        segment_start = 0
        for index in range(turn_index - 1, -1, -1):
            if _is_turn_token_line(lines[index]):
                segment_start = index + 1
                break

        tail_window_start = max(segment_start, turn_index - 60)

        def collect_structured_before_turn(max_gap: int = 60) -> list[str]:
            reverse_lines: list[str] = []
            reverse_seen: set[str] = set()
            collecting = False
            gap = 0
            for raw_line in reversed(lines[segment_start:turn_index]):
                canonical = _canonical_audit_line(raw_line)
                if canonical:
                    if _is_prompt_example_audit_line(canonical):
                        continue
                    if canonical not in reverse_seen:
                        reverse_lines.append(canonical)
                        reverse_seen.add(canonical)
                    collecting = True
                    gap = 0
                    continue
                text = clean_ansi(raw_line).strip()
                if is_runtime_noise_line(text):
                    continue
                if not collecting:
                    continue
                gap += 1
                if gap >= max_gap:
                    break
            reverse_lines.reverse()
            return reverse_lines

        unexpected_lines: list[str] = []
        seen_unexpected: set[str] = set()
        structured_lines: list[str] = []
        if final_token == "[[ROUTING_AUDIT:PASS]]":
            for line in list(lines[tail_window_start:turn_index]) + list(lines[turn_index + 1: token_index]):
                text = clean_ansi(line).strip()
                if is_runtime_noise_line(text):
                    continue
                if text in seen_unexpected:
                    continue
                unexpected_lines.append(text)
                seen_unexpected.add(text)
        else:
            structured_lines = collect_structured_before_turn()
            seen: set[str] = set(structured_lines)
            for line in lines[turn_index + 1: token_index]:
                canonical = _canonical_audit_line(line)
                if canonical:
                    if _is_prompt_example_audit_line(canonical):
                        continue
                    if canonical in seen:
                        continue
                    structured_lines.append(canonical)
                    seen.add(canonical)
                    continue
                text = clean_ansi(line).strip()
                if is_runtime_noise_line(text):
                    continue
                if text in seen_unexpected:
                    continue
                unexpected_lines.append(text)
                seen_unexpected.add(text)

        trailing_lines: list[str] = []
        seen_trailing: set[str] = set()
        for line in lines[token_index + 1:]:
            text = clean_ansi(line).strip()
            if is_runtime_noise_line(text):
                continue
            if text in seen_trailing:
                continue
            trailing_lines.append(text)
            seen_trailing.add(text)

        if final_token == "[[ROUTING_AUDIT:REVISE]]" and not structured_lines:
            return ""

        payload_lines = list(structured_lines)
        if final_token == "[[ROUTING_AUDIT:PASS]]":
            payload_lines.extend(unexpected_lines)
        payload_lines.append(turn_token)
        payload_lines.append(final_token)
        payload_lines.extend(trailing_lines)
        return "\n".join(payload_lines)

    def _extract_reply_from_observation(
            self,
            observation: WorkerObservation,
            *,
            turn_token: str,
            required_tokens: Sequence[str],
    ) -> str:
        candidate_sources = [observation.raw_log_tail, observation.visible_text]
        audit_turn = any(token.startswith("[[ROUTING_AUDIT:") for token in required_tokens)
        for source in candidate_sources:
            if not source or turn_token not in source:
                continue
            if audit_turn:
                reply = self._extract_audit_reply_from_source(
                    source,
                    turn_token=turn_token,
                    required_tokens=required_tokens,
                )
                if reply:
                    return self._strip_turn_token(reply, turn_token)
                continue
            truncated_source = self._truncate_source_at_completion_token(source, turn_token, required_tokens)
            try:
                reply = self._extract_last_message(truncated_source)
            except Exception:
                continue
            if reply.strip() == turn_token:
                blocks = BaseOutputDetector._split_blocks(truncated_source)
                if len(blocks) >= 2 and blocks[-1].strip() == turn_token:
                    reply = f"{blocks[-2]}\n{turn_token}"
            if turn_token not in reply:
                continue
            if required_tokens and not extract_final_protocol_token(reply, required_tokens):
                continue
            return self._strip_turn_token(reply, turn_token)
        return ""

    def _extract_required_token_reply_without_turn_token(
            self,
            observation: WorkerObservation,
            *,
            required_tokens: Sequence[str],
    ) -> str:
        if not required_tokens:
            return ""
        phase = self.detector.classify_phase(observation)
        if phase not in {
            ProviderPhase.WAITING_INPUT,
            ProviderPhase.IDLE_READY,
            ProviderPhase.COMPLETED_RESPONSE,
        }:
            return ""
        candidate_sources = [observation.visible_text, observation.raw_log_tail]
        for source in candidate_sources:
            if not source:
                continue
            try:
                reply = self.detector.extract_last_message(source)
            except Exception:
                continue
            lines = clean_ansi(reply).splitlines()
            for line in reversed(lines):
                token = _extract_protocol_token_from_line(line, required_tokens)
                if token:
                    return token
        return ""

    def _wait_for_turn_reply(
            self,
            *,
            baseline_reply: str,
            baseline_visible: str,
            turn_token: str,
            required_tokens: Sequence[str],
            task_status_path: Path | None = None,
            timeout_sec: float,
    ) -> str:
        deadline = time.monotonic() + timeout_sec
        resolved_reply = ""
        status_done_seen = task_status_path is None
        while time.monotonic() < deadline:
            observation = self.observe(tail_lines=500)
            if not observation.session_exists:
                raise RuntimeError("tmux pane exited while waiting for reply")
            if observation.pane_dead:
                raise RuntimeError(f"tmux pane died while waiting for reply:\n{self.capture_visible(160)}")

            current_command = observation.current_command
            visible = observation.visible_text
            if current_command in SHELL_COMMANDS:
                self.agent_ready = False
                raise RuntimeError(f"agent exited back to shell during turn:\n{visible}")

            status_done_seen = self._track_task_completion_signal(
                task_status_path=task_status_path,
                status_done_seen=status_done_seen,
            )
            if self._maybe_send_task_completion_nudge(
                current_command=current_command,
                visible_text=visible,
                task_status_path=task_status_path,
            ):
                time.sleep(0.4)
                continue

            reply = self._extract_reply_from_observation(
                observation,
                turn_token=turn_token,
                required_tokens=required_tokens,
            )
            if reply:
                resolved_reply = reply

            if not resolved_reply and baseline_reply:
                time.sleep(0.4)
                continue
            if not resolved_reply and status_done_seen:
                fallback_reply = self._extract_required_token_reply_without_turn_token(
                    observation,
                    required_tokens=required_tokens,
                )
                if fallback_reply:
                    resolved_reply = fallback_reply
            if not resolved_reply:
                time.sleep(0.4)
                continue
            if not status_done_seen:
                time.sleep(0.4)
                continue
            self.current_command = current_command
            self.current_path = observation.current_path
            self.last_heartbeat_at = observation.observed_at
            self.last_reply = resolved_reply
            self.current_task_runtime_status = "done"
            return resolved_reply

        try:
            self.send_special_key("C-c")
        except Exception:
            pass
        self.agent_ready = False
        raise TimeoutError(f"等待智能体回复超时:\n{clean_ansi(self.capture_visible(200))[-4000:]}")

    def run_turn(
            self,
            *,
            label: str,
            prompt: str,
            required_tokens: Sequence[str] = (),
            completion_contract: TurnFileContract | None = None,
            result_contract: TaskResultContract | None = None,
            timeout_sec: float = DEFAULT_COMMAND_TIMEOUT_SEC,
    ) -> CommandResult:
        started_at = _now_iso()
        last_timeout: TimeoutError | None = None
        self._reset_task_completion_nudge_state()
        for attempt in range(1, 3):
            turn_token = f"[[ACX_TURN:{uuid.uuid4().hex[:8]}:DONE]]"
            task_status_path = self._build_task_status_path(label=label, attempt=attempt)
            manifest_path = self._build_task_manifest_path(label=label, attempt=attempt)
            result_path = self._build_task_result_path(label=label, attempt=attempt)
            self._write_task_status_file(task_status_path, status="running")
            if manifest_path.exists():
                manifest_path.unlink()
            if result_path.exists():
                result_path.unlink()
            if result_contract is not None:
                self._write_task_manifest_file(
                    task_status_path=task_status_path,
                    manifest_path=manifest_path,
                    result_path=result_path,
                    contract=result_contract,
                )
            _, complete_task_command = self._write_task_completion_helper(
                label=label,
                attempt=attempt,
                task_status_path=task_status_path,
                manifest_path=manifest_path if result_contract is not None else None,
            )
            self.current_task_status_path = str(task_status_path)
            self.current_task_manifest_path = str(manifest_path) if result_contract is not None else ""
            self.current_task_result_path = str(result_path) if result_contract is not None else ""
            self.current_task_completion_command = complete_task_command
            self.current_task_runtime_status = "running"
            submitted_prompt = self._build_turn_prompt(
                prompt,
                turn_token,
                required_tokens,
                task_status_path=task_status_path,
                complete_task_command=complete_task_command,
                include_turn_protocol=completion_contract is None and result_contract is None,
            )
            prompt_hash = hashlib.sha1(submitted_prompt.encode("utf-8")).hexdigest()[:12]
            self._append_transcript(f"{label} / prompt", f"```text\n{submitted_prompt}\n```")
            self._write_state(
                WorkerStatus.RUNNING,
                note=f"turn:{label}",
                extra={
                    "label": label,
                    "started_at": started_at,
                    "last_turn_token": turn_token,
                    "last_prompt_hash": prompt_hash,
                    "current_turn_id": completion_contract.turn_id if completion_contract else "",
                    "current_turn_phase": completion_contract.phase if completion_contract else "",
                    "current_turn_status_path": str(completion_contract.status_path) if completion_contract else "",
                    "current_task_status_path": str(task_status_path),
                    "current_task_manifest_path": self.current_task_manifest_path,
                    "current_task_result_path": self.current_task_result_path,
                    "current_task_completion_command": complete_task_command,
                    "current_task_runtime_status": "running",
                    "result_status": "running",
                    "retry_count": attempt - 1,
                },
            )

            try:
                self.ensure_agent_ready()
                baseline_observation = self.observe(tail_lines=500)
                baseline_visible = baseline_observation.visible_text
                baseline_reply = self.last_reply
                self.wrapper_state = WrapperState.NOT_READY
                self._write_state(
                    WorkerStatus.RUNNING,
                    note=f"turn:{label}",
                    extra={
                        "label": label,
                        "started_at": started_at,
                        "phase": "submit",
                        "current_command": self.current_command,
                        "current_path": self.current_path,
                        "current_task_status_path": str(task_status_path),
                        "current_task_manifest_path": self.current_task_manifest_path,
                        "current_task_result_path": self.current_task_result_path,
                        "current_task_completion_command": complete_task_command,
                        "current_task_runtime_status": "running",
                        "retry_count": attempt - 1,
                    },
                )
                self._send_text(submitted_prompt)
                if completion_contract is not None:
                    self._wait_for_prompt_submission(prompt=submitted_prompt, timeout_sec=min(timeout_sec, 20.0))
                    file_result = self.wait_for_turn_artifacts(
                        contract=completion_contract,
                        task_status_path=task_status_path,
                        timeout_sec=timeout_sec,
                    )
                    reply = json.dumps(
                        {
                            "status_path": file_result.status_path,
                            "artifact_paths": file_result.artifact_paths,
                            "artifact_hashes": file_result.artifact_hashes,
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                elif result_contract is not None:
                    self._wait_for_prompt_submission(prompt=submitted_prompt, timeout_sec=min(timeout_sec, 20.0))
                    task_result = self.wait_for_task_result(
                        contract=result_contract,
                        task_status_path=task_status_path,
                        result_path=result_path,
                        timeout_sec=timeout_sec,
                    )
                    reply = json.dumps(task_result.payload, ensure_ascii=False, indent=2)
                else:
                    reply = self._wait_for_turn_reply(
                        baseline_reply=baseline_reply,
                        baseline_visible=baseline_visible,
                        turn_token=turn_token,
                        required_tokens=required_tokens,
                        task_status_path=task_status_path,
                        timeout_sec=timeout_sec,
                    )
                finished_at = _now_iso()
                self.current_task_runtime_status = self._read_task_status_file(task_status_path)
                self.agent_ready = True
                if self.provider_phase in {
                    ProviderPhase.WAITING_INPUT,
                    ProviderPhase.IDLE_READY,
                    ProviderPhase.COMPLETED_RESPONSE,
                }:
                    self.wrapper_state = WrapperState.READY
                result = CommandResult(
                    label=label,
                    command=submitted_prompt,
                    exit_code=0,
                    raw_output=reply,
                    clean_output=reply,
                    started_at=started_at,
                    finished_at=finished_at,
                )
                self._record_result(
                    result,
                    status=WorkerStatus.SUCCEEDED,
                    note=f"done:{label}",
                    extra={
                        "label": label,
                        "result_status": "succeeded",
                        "retry_count": attempt - 1,
                        "current_turn_id": completion_contract.turn_id if completion_contract else "",
                        "current_turn_phase": completion_contract.phase if completion_contract else "",
                        "current_turn_status_path": str(completion_contract.status_path) if completion_contract else "",
                        "current_task_status_path": str(task_status_path),
                        "current_task_manifest_path": self.current_task_manifest_path,
                        "current_task_result_path": self.current_task_result_path,
                        "current_task_completion_command": complete_task_command,
                        "current_task_runtime_status": self.current_task_runtime_status,
                    },
                )
                return result
            except TimeoutError as error:
                last_timeout = error
                self.agent_ready = False
                self.provider_phase = ProviderPhase.RECOVERING
                self.wrapper_state = WrapperState.NOT_READY
                self.current_task_runtime_status = self._read_task_status_file(task_status_path)
                if attempt < 2:
                    self._log_event("turn_timeout_retry", label=label, attempt=attempt)
                    self._write_state(
                        WorkerStatus.RUNNING,
                        note=f"retry:{label}",
                        extra={
                            "label": label,
                            "retry_count": attempt,
                            "result_status": "running",
                            "current_task_status_path": str(task_status_path),
                            "current_task_manifest_path": self.current_task_manifest_path,
                            "current_task_result_path": self.current_task_result_path,
                            "current_task_completion_command": complete_task_command,
                            "current_task_runtime_status": self.current_task_runtime_status,
                        },
                    )
                    self.ensure_agent_ready(timeout_sec=timeout_sec)
                    continue
                finished_at = _now_iso()
                clean_output = str(error).strip()
                result = CommandResult(
                    label=label,
                    command=submitted_prompt,
                    exit_code=TIMEOUT_EXIT_CODE,
                    raw_output=clean_output,
                    clean_output=clean_output,
                    started_at=started_at,
                    finished_at=finished_at,
                )
                self._record_result(
                    result,
                    status=WorkerStatus.FAILED,
                    note=f"timeout:{label}",
                    extra={
                        "label": label,
                        "timeout_sec": timeout_sec,
                        "result_status": "failed",
                        "retry_count": attempt,
                        "current_task_status_path": str(task_status_path),
                        "current_task_manifest_path": self.current_task_manifest_path,
                        "current_task_result_path": self.current_task_result_path,
                        "current_task_completion_command": complete_task_command,
                        "current_task_runtime_status": self.current_task_runtime_status,
                    },
                )
                return result
            except Exception as error:
                finished_at = _now_iso()
                current_visible = clean_ansi(self.capture_visible(200)) if self.pane_id and self.target_exists() else ""
                clean_output = "\n".join(part for part in [str(error).strip(), current_visible.strip()] if part).strip()
                self.agent_ready = False
                self.provider_phase = ProviderPhase.ERROR
                self.wrapper_state = WrapperState.NOT_READY
                self.current_task_runtime_status = self._read_task_status_file(task_status_path)
                result = CommandResult(
                    label=label,
                    command=submitted_prompt,
                    exit_code=GENERIC_ERROR_EXIT_CODE,
                    raw_output=clean_output,
                    clean_output=clean_output,
                    started_at=started_at,
                    finished_at=finished_at,
                )
                self._record_result(
                    result,
                    status=WorkerStatus.FAILED,
                    note=f"error:{label}",
                    extra={
                        "label": label,
                        "result_status": "failed",
                        "retry_count": attempt - 1,
                        "current_task_status_path": str(task_status_path),
                        "current_task_manifest_path": self.current_task_manifest_path,
                        "current_task_result_path": self.current_task_result_path,
                        "current_task_completion_command": complete_task_command,
                        "current_task_runtime_status": self.current_task_runtime_status,
                    },
                )
                return result

        raise AssertionError(f"unreachable turn state for {label}: {last_timeout}")

    def collect_result(self) -> WorkerResult:
        status = WorkerStatus.READY.value
        if self.results:
            status = WorkerStatus.SUCCEEDED.value if all(
                item.ok for item in self.results) else WorkerStatus.FAILED.value
        return WorkerResult(
            worker_id=self.worker_id,
            session_name=self.session_name,
            pane_id=self.pane_id,
            runtime_dir=str(self.runtime_dir),
            work_dir=str(self.work_dir),
            config=self.config.to_summary(),
            status=status,
            commands=list(self.results),
        )


def extract_final_protocol_token(text: str, allowed_tokens: Sequence[str]) -> str:
    lines = [clean_ansi(line).strip() for line in str(text or "").splitlines() if line.strip()]
    while lines and is_runtime_noise_line(lines[-1]):
        lines.pop()
    if not lines:
        return ""
    return _extract_protocol_token_from_line(lines[-1], allowed_tokens)


def build_session_name(
        worker_id: str,
        work_dir: Path,
        vendor: Vendor,
        instance_id: str = "",
        *,
        occupied_session_names: Sequence[str] | None = None,
) -> str:
    del vendor
    del instance_id
    role_key = _worker_role_key(worker_id, work_dir)
    role_label = _sanitize_session_fragment(_worker_role_label(role_key), fallback="执行者")
    occupied = {str(name).strip() for name in occupied_session_names or () if str(name).strip()}
    preferred_index = _preferred_constellation_index(work_dir, role_key)
    for offset in range(len(SESSION_CONSTELLATION_NAMES)):
        constellation = _sanitize_session_fragment(
            SESSION_CONSTELLATION_NAMES[(preferred_index + offset) % len(SESSION_CONSTELLATION_NAMES)],
            fallback="天魁星",
        )
        candidate = f"{role_label}-{constellation}"
        if candidate not in occupied:
            return candidate
    base_constellation = _sanitize_session_fragment(SESSION_CONSTELLATION_NAMES[preferred_index], fallback="天魁星")
    base_name = f"{role_label}-{base_constellation}"
    suffix = 2
    candidate = f"{base_name}-{suffix}"
    while candidate in occupied:
        suffix += 1
        candidate = f"{base_name}-{suffix}"
    return candidate
