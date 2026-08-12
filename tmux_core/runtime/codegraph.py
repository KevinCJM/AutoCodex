from __future__ import annotations

import argparse
import contextlib
import dataclasses
import enum
import hashlib
import json
import math
import os
import platform
import re
import shlex
import shutil
import signal
import stat
import subprocess
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence


CODEGRAPH_VERSION = "1.5.0"
CODEGRAPH_GUIDE_VERSION = "1"
CODEGRAPH_FULL_GUIDE_MARKER = f"[CodeGraph Usage Guide v{CODEGRAPH_GUIDE_VERSION}]"
CODEGRAPH_QUERY_RESULT_SCHEMA = "tmux-codegraph-query-result/1"
CODEGRAPH_MAX_HINT_CHARS = 600
CODEGRAPH_MAX_REMINDER_CHARS = 250
CODEGRAPH_DEFAULT_MAX_FILES = 6
CODEGRAPH_DEFAULT_MAX_OUTPUT_CHARS = 12_000
CODEGRAPH_MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
CODEGRAPH_MAX_ARCHIVE_MEMBERS = 100_000
CODEGRAPH_MAX_ARCHIVE_MEMBER_BYTES = 1024 * 1024 * 1024
CODEGRAPH_MAX_EXTRACTED_BYTES = 4 * 1024 * 1024 * 1024
PROJECT_ROOT = Path(__file__).resolve().parents[2]
_STATUS_FILE_NAME = "codegraph.status.json"
_PREFERENCE_FILE_NAME = "codegraph.preference.json"


class CodeGraphMode(str, enum.Enum):
    OFF = "off"
    AUTO = "auto"
    REQUIRED = "required"


class CodeGraphState(str, enum.Enum):
    OFF = "off"
    UNAVAILABLE = "unavailable"
    INITIALIZING = "initializing"
    SYNCING = "syncing"
    READY = "ready"
    STALE = "stale"
    DEGRADED = "degraded"
    FAILED = "failed"


class CodeGraphQueryIntent(str, enum.Enum):
    ROUTING_DISCOVERY = "routing_discovery"
    CODE_FACT_DISCOVERY = "code_fact_discovery"
    REQUIREMENT_IMPACT = "requirement_impact"
    ARCHITECTURE_BOUNDARY = "architecture_boundary"
    TASK_DEPENDENCY = "task_dependency"
    IMPLEMENTATION = "implementation"
    CHANGE_REVIEW = "change_review"
    WHOLE_CHANGE_REVIEW = "whole_change_review"


class CodeGraphError(RuntimeError):
    """Base error for the project-managed CodeGraph adapter."""


class CodeGraphUnavailable(CodeGraphError):
    pass


class CodeGraphSyncFailed(CodeGraphError):
    pass


class CodeGraphContractError(CodeGraphError):
    pass


@dataclasses.dataclass(frozen=True)
class CodeGraphToolResolution:
    executable_path: str = ""
    version: str = ""
    source: str = ""
    compatible: bool = False
    error: str = ""


@dataclasses.dataclass(frozen=True)
class CodeGraphConfig:
    max_files: int = CODEGRAPH_DEFAULT_MAX_FILES
    max_output_chars: int = CODEGRAPH_DEFAULT_MAX_OUTPUT_CHARS
    init_timeout_sec: float = 300.0
    sync_timeout_sec: float = 60.0

    def __post_init__(self) -> None:
        def normalize_int(name: str, value: object) -> int:
            if isinstance(value, bool):
                raise ValueError(f"CodeGraph {name} 必须是整数")
            if isinstance(value, float) and not value.is_integer():
                raise ValueError(f"CodeGraph {name} 必须是整数")
            try:
                normalized = int(value)
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError(f"CodeGraph {name} 必须是整数") from exc
            return normalized

        def normalize_timeout(name: str, value: object) -> float:
            if isinstance(value, bool):
                raise ValueError(f"CodeGraph {name} 必须是有限正数")
            try:
                normalized = float(value)
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError(f"CodeGraph {name} 必须是有限正数") from exc
            if not math.isfinite(normalized) or normalized <= 0:
                raise ValueError(f"CodeGraph {name} 必须是有限正数")
            return normalized

        max_files = normalize_int("max_files", self.max_files)
        max_output_chars = normalize_int("max_output_chars", self.max_output_chars)
        init_timeout_sec = normalize_timeout("init_timeout_sec", self.init_timeout_sec)
        sync_timeout_sec = normalize_timeout("sync_timeout_sec", self.sync_timeout_sec)
        object.__setattr__(self, "max_files", max_files)
        object.__setattr__(self, "max_output_chars", max_output_chars)
        object.__setattr__(self, "init_timeout_sec", init_timeout_sec)
        object.__setattr__(self, "sync_timeout_sec", sync_timeout_sec)
        if max_files < 1 or max_files > 20:
            raise ValueError("CodeGraph max_files 必须在 1..20 之间")
        if max_output_chars < 1_000 or max_output_chars > 50_000:
            raise ValueError("CodeGraph max_output_chars 必须在 1000..50000 之间")


@dataclasses.dataclass(frozen=True)
class CodeGraphTurnContext:
    stage_key: str
    phase: str
    role: str
    intent: CodeGraphQueryIntent | str
    requirement_name: str = ""
    task_name: str = ""
    routed_paths: tuple[str, ...] = ()
    changed_files: tuple[str, ...] = ()
    deleted_files: tuple[str, ...] = ()
    symbols: tuple[str, ...] = ()
    query_seeds: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        try:
            intent = self.intent if isinstance(self.intent, CodeGraphQueryIntent) else CodeGraphQueryIntent(str(self.intent))
        except ValueError as exc:
            allowed = ", ".join(item.value for item in CodeGraphQueryIntent)
            raise ValueError(f"非法 CodeGraph query intent: {self.intent!r}; 合法值: {allowed}") from exc
        object.__setattr__(self, "intent", intent)
        for name in ("stage_key", "phase", "role", "requirement_name", "task_name"):
            object.__setattr__(self, name, str(getattr(self, name) or "").strip())
        for name in ("routed_paths", "changed_files", "deleted_files", "symbols", "query_seeds"):
            raw = getattr(self, name)
            if isinstance(raw, (str, Path)):
                raw = (str(raw),)
            normalized = tuple(dict.fromkeys(str(value).strip() for value in (raw or ()) if str(value).strip()))
            object.__setattr__(self, name, normalized)


@dataclasses.dataclass(frozen=True)
class CodeGraphProjectStatus:
    mode: str
    state: str
    version: str = ""
    initialized: bool = False
    freshness: str = "unknown"
    last_indexed: str = ""
    file_count: int = 0
    node_count: int = 0
    edge_count: int = 0
    pending_added: int = 0
    pending_modified: int = 0
    pending_removed: int = 0
    index_state: str = ""
    pending_refs: int = 0
    reindex_recommended: bool = False
    worktree_mismatch: bool = False
    last_error: str = ""

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "state": self.state,
            "version": self.version,
            "initialized": self.initialized,
            "freshness": self.freshness,
            "last_indexed": self.last_indexed,
            "file_count": self.file_count,
            "node_count": self.node_count,
            "edge_count": self.edge_count,
            "pending_changes": {
                "added": self.pending_added,
                "modified": self.pending_modified,
                "removed": self.pending_removed,
            },
            "index_state": self.index_state,
            "pending_refs": self.pending_refs,
            "reindex_recommended": self.reindex_recommended,
            "worktree_mismatch": self.worktree_mismatch,
            "last_error": self.last_error,
        }


# Existing bridge code historically imported CodeGraphStatus. Keep the public
# name while converging all writers on CodeGraphProjectStatus.
CodeGraphStatus = CodeGraphProjectStatus


@dataclasses.dataclass(frozen=True)
class CodeGraphTurnHint:
    mode: str = CodeGraphMode.OFF.value
    status: CodeGraphProjectStatus | None = None
    turn_context: CodeGraphTurnContext | None = None
    suggestion: str = ""
    full_text: str = ""
    reminder_text: str = ""

    @property
    def enabled(self) -> bool:
        return self.mode != CodeGraphMode.OFF.value and bool(self.full_text)


# Transitional internal name used by stage call sites. It is a compact hint,
# not a precomputed evidence profile.
CodeGraphTurnProfile = CodeGraphTurnHint


@dataclasses.dataclass(frozen=True)
class CodeGraphQueryResult:
    ok: bool
    query_id: str
    command: str
    freshness: str
    truncated: bool
    duration_ms: int
    result_text: str
    warnings: tuple[str, ...] = ()
    error_kind: str = ""

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "schema": CODEGRAPH_QUERY_RESULT_SCHEMA,
            "ok": self.ok,
            "query_id": self.query_id,
            "command": self.command,
            "freshness": self.freshness,
            "warnings": list(self.warnings),
            "truncated": self.truncated,
            "duration_ms": self.duration_ms,
            "result_text": self.result_text,
            "error_kind": self.error_kind,
        }

    def to_text(self) -> str:
        lines = [
            "[TMUX_CODEGRAPH_QUERY]",
            f"schema: {CODEGRAPH_QUERY_RESULT_SCHEMA}",
            f"query_id: {self.query_id}",
            f"command: {self.command}",
            f"freshness: {self.freshness}",
            f"ok: {str(self.ok).lower()}",
            f"truncated: {str(self.truncated).lower()}",
            f"duration_ms: {self.duration_ms}",
        ]
        lines.extend(f"warning: {item}" for item in self.warnings)
        if self.error_kind:
            lines.append(f"error_kind: {self.error_kind}")
        lines.extend(("[RESULT]", self.result_text, "[END_TMUX_CODEGRAPH_QUERY]"))
        return "\n".join(lines)


_PROCESS_LOCK = threading.RLock()
_ACTIVE_PROCESSES: set[subprocess.Popen[str]] = set()
_TOOL_CACHE_LOCK = threading.RLock()
_ExecutableFingerprint = tuple[int, int, int, int, int]
_TOOL_CACHE: tuple[
    tuple[str, str, str],
    CodeGraphToolResolution,
    _ExecutableFingerprint | None,
] | None = None
_PROJECT_LOCKS_GUARD = threading.RLock()
_PROJECT_LOCKS: dict[str, threading.RLock] = {}
_RECOVERY_LOCK = threading.RLock()
_RECOVERY_GUARDS_LOCK = threading.RLock()
_RECOVERY_GUARDS: dict[str, threading.RLock] = {}
_INSTALL_LOCK = threading.RLock()
_RECOVERY_PROJECTS: set[str] = set()
_RECOVERY_DECISIONS: dict[tuple[str, str], str] = {}


def normalize_codegraph_mode(
    value: object,
    *,
    default: CodeGraphMode = CodeGraphMode.OFF,
) -> CodeGraphMode:
    if isinstance(value, CodeGraphMode):
        return value
    text = str(value or "").strip().casefold()
    if not text:
        return default
    try:
        return CodeGraphMode(text)
    except ValueError as exc:
        allowed = ", ".join(item.value for item in CodeGraphMode)
        raise ValueError(f"非法 CodeGraph 模式: {value!r}; 合法值: {allowed}") from exc


def normalize_codegraph_config(value: Mapping[str, object] | None) -> dict[str, object]:
    raw = dict(value or {})
    allowed = {"max_files", "max_output_chars", "init_timeout_sec", "sync_timeout_sec"}
    unknown = sorted(set(raw).difference(allowed))
    if unknown:
        raise ValueError(f"未知 CodeGraph 配置: {', '.join(unknown)}")
    config = CodeGraphConfig(**raw)
    return dataclasses.asdict(config)


def _project_root(project_dir: str | Path) -> Path:
    root = Path(project_dir).expanduser().resolve()
    if not root.is_dir():
        raise CodeGraphContractError(f"项目目录不存在: {root}")
    return root


def _project_key(project_dir: str | Path) -> str:
    return hashlib.sha256(str(_project_root(project_dir)).encode("utf-8")).hexdigest()


def _project_lock(project_dir: str | Path) -> threading.RLock:
    key = _project_key(project_dir)
    with _PROJECT_LOCKS_GUARD:
        return _PROJECT_LOCKS.setdefault(key, threading.RLock())


def _xdg_data_root() -> Path:
    configured = str(os.environ.get("XDG_DATA_HOME", "") or "").strip()
    if configured:
        candidate = Path(configured).expanduser()
        if candidate.is_absolute():
            return candidate
    return Path.home() / ".local" / "share"


def codegraph_data_root() -> Path:
    return _xdg_data_root() / "tmux_coding_team" / "tools" / "codegraph" / CODEGRAPH_VERSION


def _platform_target() -> str:
    system = platform.system().casefold()
    machine = platform.machine().casefold()
    os_name = {"darwin": "darwin", "linux": "linux", "windows": "win32"}.get(system)
    arch = "arm64" if machine in {"arm64", "aarch64"} else "x64" if machine in {"x86_64", "amd64"} else None
    if not os_name or not arch:
        raise CodeGraphUnavailable(f"不支持的平台: {platform.system()} {platform.machine()}")
    return f"{os_name}-{arch}"


def managed_codegraph_executable() -> Path:
    target = _platform_target()
    name = "codegraph.exe" if target.startswith("win32-") else "codegraph"
    return codegraph_data_root() / target / "bin" / name


def _version_from_output(output: str) -> str:
    match = re.search(r"(?<!\d)(\d+\.\d+\.\d+)(?!\d)", str(output or ""))
    return match.group(1) if match else ""


def _executable_fingerprint(path: str | Path) -> _ExecutableFingerprint | None:
    try:
        resolved = Path(path).expanduser().resolve(strict=True)
        metadata = resolved.lstat()
    except (OSError, RuntimeError, ValueError):
        return None
    if not stat.S_ISREG(metadata.st_mode) or not os.access(resolved, os.X_OK):
        return None
    return (
        int(metadata.st_dev),
        int(metadata.st_ino),
        int(metadata.st_size),
        int(metadata.st_mtime_ns),
        int(metadata.st_mode),
    )


def _cache_tool_resolution(
    key: tuple[str, str, str],
    resolution: CodeGraphToolResolution,
) -> CodeGraphToolResolution:
    global _TOOL_CACHE
    fingerprint = (
        _executable_fingerprint(resolution.executable_path)
        if resolution.compatible and resolution.executable_path
        else None
    )
    if resolution.compatible and fingerprint is None:
        resolution = CodeGraphToolResolution(
            source=resolution.source,
            error="CodeGraph可执行文件在探测后发生变化",
        )
    _TOOL_CACHE = (key, resolution, fingerprint)
    return resolution


def _sanitized_env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ)
    if extra:
        env.update({str(key): str(value) for key, value in extra.items()})
    # Apply the security contract last. Neither the parent shell nor an
    # internal caller may redirect the index or re-enable background/network
    # behavior for a system-owned CodeGraph process.
    env.update(
        {
            "CODEGRAPH_DIR": ".codegraph",
            "CODEGRAPH_TELEMETRY": "0",
            "CODEGRAPH_NO_DAEMON": "1",
            "CODEGRAPH_NO_UPDATE_CHECK": "1",
            "CODEGRAPH_NO_PROMPT_HOOK": "1",
            "DO_NOT_TRACK": "1",
            "NO_COLOR": "1",
        }
    )
    env.pop("FORCE_COLOR", None)
    return env


def _probe_tool(path: Path, *, source: str) -> CodeGraphToolResolution:
    try:
        if not path.is_file() or not os.access(path, os.X_OK):
            return CodeGraphToolResolution(source=source, error=f"不可执行: {path}")
        code, stdout, stderr, _ = _run_process_separate(
            [str(path), "--version"],
            timeout_sec=10,
            max_output_chars=10_000,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return CodeGraphToolResolution(source=source, error=f"版本探测失败: {type(exc).__name__}")
    version = _version_from_output(f"{stdout}\n{stderr}")
    compatible = code == 0 and version == CODEGRAPH_VERSION
    if compatible:
        try:
            contract_code, contract_stdout, contract_stderr, _ = _run_process_separate(
                [str(path), "--help"],
                timeout_sec=10,
                max_output_chars=50_000,
            )
            help_text = f"{contract_stdout}\n{contract_stderr}"
            compatible = contract_code == 0 and all(
                re.search(rf"\b{name}\b", help_text) for name in ("init", "sync", "status", "explore")
            )
        except (OSError, subprocess.SubprocessError):
            compatible = False
    return CodeGraphToolResolution(
        executable_path=str(path.resolve()) if compatible else "",
        version=version,
        source=source,
        compatible=compatible,
        error="" if compatible else f"需要 CodeGraph {CODEGRAPH_VERSION}，实际为 {version or 'unknown'}",
    )


def resolve_codegraph_tool(*, force_refresh: bool = False) -> CodeGraphToolResolution:
    global _TOOL_CACHE
    env_path = str(os.environ.get("TMUX_CODEGRAPH_EXECUTABLE", "") or "").strip()
    key = (env_path, os.environ.get("PATH", ""), os.environ.get("XDG_DATA_HOME", ""))
    with _TOOL_CACHE_LOCK:
        if not force_refresh and _TOOL_CACHE and _TOOL_CACHE[0] == key:
            cached = _TOOL_CACHE[1]
            if not cached.compatible or (
                _executable_fingerprint(cached.executable_path) == _TOOL_CACHE[2]
            ):
                return cached
        candidates: list[tuple[Path, str]] = []
        if env_path:
            explicit = Path(env_path).expanduser()
            if not explicit.is_absolute():
                result = CodeGraphToolResolution(source="env", error="TMUX_CODEGRAPH_EXECUTABLE 必须是绝对路径")
                return _cache_tool_resolution(key, result)
            resolution = _probe_tool(explicit, source="env")
            return _cache_tool_resolution(key, resolution)
        with contextlib.suppress(CodeGraphUnavailable):
            candidates.append((managed_codegraph_executable(), "managed"))
        discovered = shutil.which("codegraph")
        if discovered:
            candidates.append((Path(discovered), "path"))
        errors: list[str] = []
        seen: set[str] = set()
        for candidate, source in candidates:
            normalized = str(candidate.expanduser().absolute())
            if normalized in seen:
                continue
            seen.add(normalized)
            resolution = _probe_tool(candidate.expanduser(), source=source)
            if resolution.compatible:
                return _cache_tool_resolution(key, resolution)
            if resolution.error:
                errors.append(f"{source}: {resolution.error}")
        result = CodeGraphToolResolution(error="; ".join(errors) or "未找到 CodeGraph 1.5.0")
        return _cache_tool_resolution(key, result)


def _register_process(process: subprocess.Popen[str]) -> None:
    with _PROCESS_LOCK:
        _ACTIVE_PROCESSES.add(process)


def _unregister_process(process: subprocess.Popen[str]) -> None:
    with _PROCESS_LOCK:
        _ACTIVE_PROCESSES.discard(process)


def _terminate_process_group(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:  # pragma: no cover - Windows CI owns this branch.
            process.terminate()
        process.wait(timeout=3)
    except (OSError, subprocess.SubprocessError):
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
            process.wait(timeout=1)


def cancel_codegraph_processes() -> None:
    with _PROCESS_LOCK:
        processes = tuple(_ACTIVE_PROCESSES)
    for process in processes:
        _terminate_process_group(process)
        _unregister_process(process)


def _run_process(
    argv: Sequence[str],
    *,
    timeout_sec: float,
    cwd: Path | None = None,
    max_output_chars: int = CODEGRAPH_DEFAULT_MAX_OUTPUT_CHARS,
) -> tuple[int, str, bool]:
    process = subprocess.Popen(
        list(argv),
        cwd=str(cwd) if cwd else None,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=_sanitized_env(),
        start_new_session=True,
        bufsize=1,
    )
    _register_process(process)
    chunks: list[str] = []
    total = 0
    truncated = False

    def drain() -> None:
        nonlocal total, truncated
        assert process.stdout is not None
        # Fixed-size reads keep a tool that emits a huge line from allocating
        # that entire line before the adapter can apply its output bound.
        for piece in iter(lambda: process.stdout.read(8192), ""):
            remaining = max(0, max_output_chars - total)
            if remaining:
                kept = piece[:remaining]
                chunks.append(kept)
                total += len(kept)
            if len(piece) > remaining:
                truncated = True

    reader = threading.Thread(target=drain, name="tmux-codegraph-output", daemon=True)
    reader.start()
    try:
        try:
            process.wait(timeout=max(float(timeout_sec), 0.1))
        except subprocess.TimeoutExpired:
            _terminate_process_group(process)
            raise
        reader.join(timeout=2)
        return int(process.returncode or 0), "".join(chunks), truncated
    finally:
        if process.poll() is None:
            _terminate_process_group(process)
        reader.join(timeout=2)
        _unregister_process(process)


def _run_process_separate(
    argv: Sequence[str],
    *,
    timeout_sec: float,
    cwd: Path | None = None,
    max_output_chars: int = CODEGRAPH_DEFAULT_MAX_OUTPUT_CHARS,
) -> tuple[int, str, str, bool]:
    """Run an owned process while preserving stdout as a machine channel."""

    process = subprocess.Popen(
        list(argv),
        cwd=str(cwd) if cwd else None,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_sanitized_env(),
        start_new_session=True,
        bufsize=0,
    )
    _register_process(process)
    streams: dict[str, list[str]] = {"stdout": [], "stderr": []}
    totals = {"stdout": 0, "stderr": 0}
    truncated = False
    output_lock = threading.Lock()

    def drain(name: str, stream: Any) -> None:
        nonlocal truncated
        for piece in iter(lambda: stream.read(8192), ""):
            with output_lock:
                remaining = max(0, max_output_chars - totals[name])
                if remaining:
                    kept = piece[:remaining]
                    streams[name].append(kept)
                    totals[name] += len(kept)
                if len(piece) > remaining:
                    truncated = True

    assert process.stdout is not None and process.stderr is not None
    readers = [
        threading.Thread(
            target=drain,
            args=(name, stream),
            name=f"tmux-codegraph-{name}",
            daemon=True,
        )
        for name, stream in (("stdout", process.stdout), ("stderr", process.stderr))
    ]
    for reader in readers:
        reader.start()
    try:
        try:
            process.wait(timeout=max(float(timeout_sec), 0.1))
        except subprocess.TimeoutExpired:
            _terminate_process_group(process)
            raise
        for reader in readers:
            reader.join(timeout=2)
        return (
            int(process.returncode or 0),
            "".join(streams["stdout"]),
            "".join(streams["stderr"]),
            truncated,
        )
    finally:
        if process.poll() is None:
            _terminate_process_group(process)
        for reader in readers:
            reader.join(timeout=2)
        _unregister_process(process)


def _int_value(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _bool_value(payload: Mapping[str, Any], names: Sequence[str], *, default: bool = False) -> bool:
    for name in names:
        if name not in payload:
            continue
        value = payload[name]
        if not isinstance(value, bool):
            raise CodeGraphContractError(f"CodeGraph status字段 {name} 必须是布尔值")
        return value
    return default


def _worktree_mismatch(payload: Mapping[str, Any]) -> bool:
    for name in ("worktreeMismatch", "worktree_mismatch"):
        if name not in payload:
            continue
        value = payload[name]
        if value is None:
            return False
        if isinstance(value, Mapping):
            return True
        raise CodeGraphContractError(
            f"CodeGraph status字段 {name} 必须是 null 或对象"
        )
    return False


def _pending_counts(payload: Mapping[str, Any]) -> tuple[int, int, int]:
    pending = payload.get("pendingChanges", payload.get("pending_changes", {}))
    if not isinstance(pending, Mapping):
        pending = {}
    def count(name: str) -> int:
        value = pending.get(name, 0)
        if isinstance(value, (list, tuple, set)):
            return len(value)
        return _int_value(value)
    return count("added"), count("modified"), count("removed")


def _status_from_payload(
    payload: Mapping[str, Any],
    *,
    mode: CodeGraphMode,
    error: str = "",
) -> CodeGraphProjectStatus:
    initialized = _bool_value(payload, ("initialized",))
    version = str(payload.get("version", "") or "").strip()
    added, modified, removed = _pending_counts(payload)
    index = payload.get("index", {})
    if not isinstance(index, Mapping):
        index = {}
    index_state = str(index.get("state", payload.get("indexState", payload.get("index_state", ""))) or "").strip().lower()
    pending_refs = _int_value(payload.get("pendingRefs", payload.get("pending_refs", index.get("pendingRefs", 0))))
    reindex = _bool_value(
        payload,
        ("reindexRecommended", "reindex_recommended"),
        default=_bool_value(index, ("reindexRecommended",), default=False),
    )
    mismatch = _worktree_mismatch(payload)
    has_pending = bool(added or modified or removed)
    status_error = str(error or "")
    if mode == CodeGraphMode.OFF:
        state, freshness = CodeGraphState.OFF.value, "unknown"
    elif status_error:
        state, freshness = CodeGraphState.DEGRADED.value, "unknown"
    elif not initialized:
        state, freshness = CodeGraphState.UNAVAILABLE.value, "unknown"
    elif index_state != "complete":
        state, freshness = CodeGraphState.DEGRADED.value, "stale" if has_pending else "unknown"
        status_error = f"未知或未完成的 CodeGraph index state: {index_state or '<missing>'}"
    elif mismatch or reindex or pending_refs:
        state, freshness = CodeGraphState.DEGRADED.value, "stale" if has_pending else "unknown"
    elif has_pending:
        state, freshness = CodeGraphState.STALE.value, "stale"
    else:
        state, freshness = CodeGraphState.READY.value, "fresh"
    return CodeGraphProjectStatus(
        mode=mode.value,
        state=state,
        version=version,
        initialized=initialized,
        freshness=freshness,
        last_indexed=str(payload.get("lastIndexed", payload.get("last_indexed", "")) or ""),
        file_count=_int_value(payload.get("fileCount", payload.get("file_count", 0))),
        node_count=_int_value(payload.get("nodeCount", payload.get("node_count", 0))),
        edge_count=_int_value(payload.get("edgeCount", payload.get("edge_count", 0))),
        pending_added=added,
        pending_modified=modified,
        pending_removed=removed,
        index_state=index_state,
        pending_refs=pending_refs,
        reindex_recommended=reindex,
        worktree_mismatch=mismatch,
        last_error=status_error,
    )


def _status_path(project_dir: str | Path) -> Path:
    project = _project_root(project_dir)
    return project / ".tmux_workflow" / _STATUS_FILE_NAME


def read_codegraph_project_preference(project_dir: str | Path) -> str:
    path = _project_root(project_dir) / ".tmux_workflow" / _PREFERENCE_FILE_NAME
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    if not isinstance(payload, Mapping):
        return ""
    mode = str(payload.get("mode", "") or "").strip().casefold()
    return mode if mode in {CodeGraphMode.OFF.value, CodeGraphMode.AUTO.value} else ""


def write_codegraph_project_preference(project_dir: str | Path, mode: CodeGraphMode | str) -> None:
    normalized = normalize_codegraph_mode(mode, default=CodeGraphMode.AUTO)
    path = _project_root(project_dir) / ".tmux_workflow" / _PREFERENCE_FILE_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temp.write_text(
        json.dumps({"schema": "tmux-codegraph-preference/1", "mode": normalized.value}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    os.replace(temp, path)


def _atomic_write_status(project_dir: str | Path, status: CodeGraphProjectStatus) -> None:
    path = _status_path(project_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temp.write_text(json.dumps(status.to_public_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, path)


def _unsafe_index_member(project: Path) -> str:
    index_dir = project / ".codegraph"
    if index_dir.is_symlink():
        return ".codegraph目录不允许使用符号链接"
    if index_dir.exists() and not index_dir.is_dir():
        return ".codegraph必须是普通目录"
    for name in ("codegraph.db", "codegraph.db-wal", "codegraph.db-shm"):
        member = index_dir / name
        if member.is_symlink():
            return f".codegraph/{name}不允许使用符号链接"
        if not member.exists():
            continue
        if not member.is_file():
            return f".codegraph/{name}必须是普通文件"
        try:
            member.resolve().relative_to(index_dir.resolve())
        except (OSError, RuntimeError, ValueError):
            return f".codegraph/{name}越出项目索引目录"
    return ""


def inspect_codegraph_project(
    project_dir: str | Path,
    *,
    mode: CodeGraphMode | str = CodeGraphMode.AUTO,
    force_tool_refresh: bool = False,
) -> CodeGraphProjectStatus:
    normalized_mode = normalize_codegraph_mode(mode, default=CodeGraphMode.AUTO)
    if normalized_mode == CodeGraphMode.OFF:
        status = _status_from_payload({}, mode=normalized_mode)
        with contextlib.suppress(OSError, CodeGraphError):
            _atomic_write_status(project_dir, status)
        return status
    project = _project_root(project_dir)
    unsafe_index = _unsafe_index_member(project)
    if unsafe_index:
        status = CodeGraphProjectStatus(
            mode=normalized_mode.value,
            state=(
                CodeGraphState.FAILED.value
                if normalized_mode == CodeGraphMode.REQUIRED
                else CodeGraphState.DEGRADED.value
            ),
            initialized=False,
            freshness="unknown",
            worktree_mismatch=True,
            last_error=f"CodeGraph项目索引不安全: {unsafe_index}",
        )
        with contextlib.suppress(OSError, CodeGraphError):
            _atomic_write_status(project, status)
        return status
    tool = resolve_codegraph_tool(force_refresh=force_tool_refresh)
    if not tool.compatible:
        status = CodeGraphProjectStatus(
            mode=normalized_mode.value,
            state=CodeGraphState.UNAVAILABLE.value,
            freshness="unknown",
            last_error=tool.error,
        )
        with contextlib.suppress(OSError, CodeGraphError):
            _atomic_write_status(project, status)
        return status
    try:
        code, stdout, stderr, truncated = _run_process_separate(
            [tool.executable_path, "status", "--json", str(project)],
            timeout_sec=20,
            cwd=project,
            max_output_chars=1_000_000,
        )
        if code != 0:
            raise CodeGraphContractError(_sanitize_error(stderr or stdout))
        if truncated:
            raise CodeGraphContractError("CodeGraph status JSON 超过大小上限")
        payload = json.loads(stdout)
        if not isinstance(payload, Mapping):
            raise CodeGraphContractError("CodeGraph status JSON 不是对象")
        normalized_payload = dict(payload)
        initialized = _bool_value(normalized_payload, ("initialized",))
        identity_error = ""
        if initialized:
            version = str(normalized_payload.get("version", "") or "").strip()
            if version != CODEGRAPH_VERSION:
                identity_error = (
                    f"CodeGraph status版本不兼容: 需要 {CODEGRAPH_VERSION}，实际为 {version or '<missing>'}"
                )
            raw_project = normalized_payload.get(
                "projectPath",
                normalized_payload.get("project_path", ""),
            )
            indexed_project = raw_project.strip() if isinstance(raw_project, str) else ""
            if not indexed_project:
                identity_error = identity_error or "CodeGraph status缺少有效 projectPath"
                normalized_payload["worktreeMismatch"] = {"detected": True}
            else:
                try:
                    resolved_project = Path(indexed_project).expanduser().resolve()
                except (OSError, RuntimeError, ValueError):
                    identity_error = identity_error or "CodeGraph status projectPath无法解析"
                    normalized_payload["worktreeMismatch"] = {"detected": True}
                else:
                    if resolved_project != project:
                        identity_error = identity_error or "CodeGraph status projectPath与目标项目不一致"
                        normalized_payload["worktreeMismatch"] = {"detected": True}
        status = _status_from_payload(normalized_payload, mode=normalized_mode)
        if identity_error:
            status = dataclasses.replace(
                status,
                state=(
                    CodeGraphState.FAILED.value
                    if normalized_mode == CodeGraphMode.REQUIRED
                    else CodeGraphState.DEGRADED.value
                ),
                freshness="unknown",
                last_error=identity_error,
            )
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError, CodeGraphError) as exc:
        status = CodeGraphProjectStatus(
            mode=normalized_mode.value,
            state=CodeGraphState.FAILED.value,
            freshness="unknown",
            last_error=f"status失败: {type(exc).__name__}: {_sanitize_error(exc)}",
        )
    with contextlib.suppress(OSError, CodeGraphError):
        _atomic_write_status(project, status)
    return status


def read_codegraph_project_status(project_dir: str | Path) -> dict[str, Any] | None:
    project = _project_root(project_dir)
    workflow_root = project / ".tmux_workflow"
    project_status_path = workflow_root / _STATUS_FILE_NAME
    # One migration release can read requirement-scoped status files written
    # by the retired adapter.  Once the project-scoped writer exists it is the
    # sole authority; a newer legacy file must never push the UI backwards.
    if project_status_path.is_file():
        selected = project_status_path
    else:
        legacy_candidates: list[Path] = []
        with contextlib.suppress(OSError):
            legacy_candidates.extend(workflow_root.glob(f"*/{_STATUS_FILE_NAME}"))
        existing = [path for path in legacy_candidates if path.is_file()]
        if not existing:
            return None
        selected = max(existing, key=lambda path: path.stat().st_mtime_ns)
    try:
        payload = json.loads(selected.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, Mapping):
        return None
    public = dict(payload)
    mode = str(public.get("mode", "") or "").strip().casefold()
    state = str(public.get("state", "") or "").strip().casefold()
    if (
        mode != CodeGraphMode.OFF.value
        and public.get("initialized") is True
        and state in {CodeGraphState.READY.value, CodeGraphState.STALE.value}
        and (
            not (project / ".codegraph").is_dir()
            or (project / ".codegraph").is_symlink()
        )
    ):
        public.update(
            {
                "state": CodeGraphState.UNAVAILABLE.value,
                "initialized": False,
                "freshness": "unknown",
                "last_error": "CodeGraph 项目索引目录不存在",
            }
        )
    return public


def publish_codegraph_off_status(project_dir: str | Path) -> bool:
    try:
        _atomic_write_status(project_dir, _status_from_payload({}, mode=CodeGraphMode.OFF))
        return True
    except (OSError, CodeGraphError):
        return False


def publish_codegraph_pending_status(
    project_dir: str | Path,
    mode: CodeGraphMode | str,
    *,
    operation: str = "syncing",
    **_: object,
) -> bool:
    normalized = normalize_codegraph_mode(mode, default=CodeGraphMode.AUTO)
    state = (
        CodeGraphState.INITIALIZING.value
        if str(operation or "").strip().lower() == "initializing"
        else CodeGraphState.SYNCING.value
    )
    status = CodeGraphProjectStatus(mode=normalized.value, state=state, freshness="unknown")
    try:
        _atomic_write_status(project_dir, status)
        return True
    except (OSError, CodeGraphError):
        return False


def _operation_failure(operation: str, error: BaseException) -> CodeGraphError:
    if isinstance(error, CodeGraphError):
        return error
    return CodeGraphSyncFailed(
        f"CodeGraph {operation}失败: {type(error).__name__}: {_sanitize_error(error)}"
    )


def _persist_operation_failure(
    project: Path,
    *,
    mode: CodeGraphMode,
    previous: CodeGraphProjectStatus | None,
    operation: str,
    error: BaseException,
) -> CodeGraphProjectStatus:
    base = previous or CodeGraphProjectStatus(
        mode=mode.value,
        state=CodeGraphState.UNAVAILABLE.value,
        freshness="unknown",
    )
    detail = _sanitize_error(error)
    prefix = (
        f"CodeGraph {operation}失败"
        if mode == CodeGraphMode.REQUIRED
        else f"CodeGraph {operation}失败，保留当前索引"
    )
    status = dataclasses.replace(
        base,
        mode=mode.value,
        state=(
            CodeGraphState.FAILED.value
            if mode == CodeGraphMode.REQUIRED
            else CodeGraphState.DEGRADED.value
        ),
        last_error=f"{prefix}: {detail}",
    )
    # Failure persistence is best effort and must never replace the operation's
    # original exception with an unrelated filesystem error.
    with contextlib.suppress(OSError, CodeGraphError):
        _atomic_write_status(project, status)
    return status


def initialize_codegraph_project(
    project_dir: str | Path,
    *,
    mode: CodeGraphMode | str = CodeGraphMode.AUTO,
    config: CodeGraphConfig | None = None,
) -> CodeGraphProjectStatus:
    normalized = normalize_codegraph_mode(mode, default=CodeGraphMode.AUTO)
    if normalized == CodeGraphMode.OFF:
        return inspect_codegraph_project(project_dir, mode=normalized)
    project = _project_root(project_dir)
    effective = config or CodeGraphConfig()
    with _project_lock(project):
        previous = inspect_codegraph_project(project, mode=normalized)
        local_index = project / ".codegraph"
        local_index_unsafe = bool(_unsafe_index_member(project))
        local_database_present = (local_index / "codegraph.db").exists()
        if previous.worktree_mismatch and (local_index_unsafe or local_database_present):
            if normalized == CodeGraphMode.REQUIRED:
                raise CodeGraphContractError(previous.last_error or "CodeGraph索引属于其他 worktree")
            return previous
        if previous.initialized and previous.state in {
            CodeGraphState.READY.value,
            CodeGraphState.STALE.value,
        }:
            return previous
        tool = resolve_codegraph_tool()
        if not tool.compatible:
            failure = CodeGraphUnavailable(tool.error)
            if normalized == CodeGraphMode.REQUIRED:
                _persist_operation_failure(
                    project,
                    mode=normalized,
                    previous=previous,
                    operation="init",
                    error=failure,
                )
                raise failure
            with contextlib.suppress(OSError, CodeGraphError):
                _atomic_write_status(project, previous)
            return previous
        publish_codegraph_pending_status(project, normalized, operation="initializing")
        try:
            code, output, _ = _run_process(
                [tool.executable_path, "init", str(project)],
                timeout_sec=effective.init_timeout_sec,
                cwd=project,
                max_output_chars=effective.max_output_chars,
            )
            if code != 0:
                raise CodeGraphSyncFailed(f"CodeGraph init失败: {_sanitize_error(output)}")
            status = inspect_codegraph_project(project, mode=normalized, force_tool_refresh=True)
            if not status.initialized:
                raise CodeGraphContractError("CodeGraph init完成后仍未检测到索引")
            if status.worktree_mismatch or status.state not in {
                CodeGraphState.READY.value,
                CodeGraphState.STALE.value,
            }:
                raise CodeGraphContractError(
                    status.last_error or f"CodeGraph初始化后状态为 {status.state}"
                )
            if normalized == CodeGraphMode.REQUIRED and status.state != CodeGraphState.READY.value:
                raise CodeGraphSyncFailed(status.last_error or f"CodeGraph初始化后状态为 {status.state}")
            return status
        except (CodeGraphError, OSError, subprocess.SubprocessError) as exc:
            failure = _operation_failure("init", exc)
            terminal = _persist_operation_failure(
                project,
                mode=normalized,
                previous=previous,
                operation="init",
                error=failure,
            )
            if normalized == CodeGraphMode.REQUIRED:
                if failure is exc:
                    raise
                raise failure from exc
            return terminal


def sync_codegraph_project(
    project_dir: str | Path,
    *,
    mode: CodeGraphMode | str = CodeGraphMode.AUTO,
    config: CodeGraphConfig | None = None,
) -> CodeGraphProjectStatus:
    normalized = normalize_codegraph_mode(mode, default=CodeGraphMode.AUTO)
    if normalized == CodeGraphMode.OFF:
        return inspect_codegraph_project(project_dir, mode=normalized)
    project = _project_root(project_dir)
    effective = config or CodeGraphConfig()
    with _project_lock(project):
        # Re-inspect after acquiring the project lock. A peer may have already
        # synchronized the shared index while this worker was waiting.
        before = inspect_codegraph_project(project, mode=normalized)
        if before.state == CodeGraphState.READY.value and before.freshness == "fresh":
            return before
        if before.worktree_mismatch:
            if normalized == CodeGraphMode.REQUIRED:
                raise CodeGraphContractError(before.last_error or "CodeGraph索引属于其他 worktree")
            return before
        if not before.initialized:
            if normalized == CodeGraphMode.REQUIRED:
                raise CodeGraphUnavailable("当前项目尚未初始化 CodeGraph")
            return before
        tool = resolve_codegraph_tool()
        if not tool.compatible:
            if normalized == CodeGraphMode.REQUIRED:
                raise CodeGraphUnavailable(tool.error)
            return before
        publish_codegraph_pending_status(project, normalized)
        try:
            code, output, _ = _run_process(
                [tool.executable_path, "sync", str(project)],
                timeout_sec=effective.sync_timeout_sec,
                cwd=project,
                max_output_chars=effective.max_output_chars,
            )
            if code != 0:
                raise CodeGraphSyncFailed(f"CodeGraph sync失败: {_sanitize_error(output)}")
            after = inspect_codegraph_project(project, mode=normalized)
            if normalized == CodeGraphMode.REQUIRED and after.state != CodeGraphState.READY.value:
                raise CodeGraphSyncFailed(after.last_error or f"CodeGraph同步后状态为 {after.state}")
            return after
        except (CodeGraphError, OSError, subprocess.SubprocessError) as exc:
            failure = _operation_failure("sync", exc)
            terminal = _persist_operation_failure(
                project,
                mode=normalized,
                previous=before,
                operation="sync",
                error=failure,
            )
            if normalized == CodeGraphMode.REQUIRED:
                if failure is exc:
                    raise
                raise failure from exc
            return terminal


def _safe_seed(value: object, *, max_chars: int = 180) -> str:
    text = " ".join(str(value or "").split())
    text = re.sub(r"[\x00-\x1f\x7f]", "", text)
    return text[:max_chars]


def _suggestion(context: CodeGraphTurnContext | None) -> str:
    if context is None:
        return ""
    query = ""
    if context.changed_files:
        seed = _safe_seed(context.changed_files[0], max_chars=100)
        query = f"Explain the callers, callees, tests, and impact of changes to {seed}"
    elif context.deleted_files:
        seed = _safe_seed(context.deleted_files[0], max_chars=100)
        query = f"Find callers and remaining dependencies of deleted file {seed}"
    elif context.symbols:
        seed = _safe_seed(context.symbols[0], max_chars=100)
        query = f"Show callers, callees, and impact of symbol {seed}"
    elif context.routed_paths:
        seed = _safe_seed(context.routed_paths[0], max_chars=100)
        query = f"Explain {seed} and its callers, callees, and related tests"
    elif context.task_name:
        query = f"Find the code and call paths relevant to task {_safe_seed(context.task_name, max_chars=100)}"
    elif context.requirement_name:
        query = f"Find the code and call paths relevant to requirement {_safe_seed(context.requirement_name, max_chars=100)}"
    elif context.intent == CodeGraphQueryIntent.ROUTING_DISCOVERY:
        query = "Find the main entry points, core modules, and tests in this project"
    if not query:
        return ""
    return f'"$TMUX_CODEGRAPH_CMD" explore {shlex.quote(query)}'


def _append_hint_if_complete(base: str, label: str, suggestion: str, *, limit: int) -> str:
    if len(base) > limit:
        raise CodeGraphContractError("CodeGraph内置提示超过长度上限")
    addition = f"\n{label}{suggestion}" if suggestion else ""
    return f"{base}{addition}" if len(base) + len(addition) <= limit else base


def _full_hint(suggestion: str) -> str:
    lines = [
        CODEGRAPH_FULL_GUIDE_MARKER,
        "需要理解跨文件调用链、符号关系或改动影响时使用 CodeGraph。",
        "若当前环境存在 codegraph_explore，使用该工具；否则使用只读命令：",
        '"$TMUX_CODEGRAPH_CMD" explore "具体问题"',
        "两种方式选择一种，不要重复查询。CodeGraph 只用于导航；任务范围以 AGENTS.md 和 AI Hermes 路由层为准，行为必须回到当前源码、测试和配置核验。",
        "禁止执行 init、index、sync、install、serve、watch、uninit 或修改索引的命令。",
    ]
    return _append_hint_if_complete(
        "\n".join(lines),
        "本轮可选查询：",
        suggestion,
        limit=CODEGRAPH_MAX_HINT_CHARS,
    )


def _reminder(suggestion: str) -> str:
    text = "CodeGraph 可选导航：仅在需要跨文件调用链或影响面时查询；范围以 AGENTS.md 为准，结论回到源码/测试/配置核验。"
    return _append_hint_if_complete(
        text,
        " 可选：",
        suggestion,
        limit=CODEGRAPH_MAX_REMINDER_CHARS,
    )


def resolve_codegraph_turn_profile(
    *,
    project_dir: str | Path,
    mode: CodeGraphMode | str,
    prompt: str = "",
    refresh: bool = False,
    config: CodeGraphConfig | None = None,
    turn_context: CodeGraphTurnContext | None = None,
    **_: object,
) -> CodeGraphTurnHint:
    del prompt
    normalized = normalize_codegraph_mode(mode, default=CodeGraphMode.OFF)
    if normalized == CodeGraphMode.OFF:
        return CodeGraphTurnHint(mode=normalized.value)
    status = sync_codegraph_project(project_dir, mode=normalized, config=config) if refresh else inspect_codegraph_project(project_dir, mode=normalized)
    if status.state not in {CodeGraphState.READY.value, CodeGraphState.STALE.value}:
        if normalized == CodeGraphMode.REQUIRED:
            raise CodeGraphUnavailable(status.last_error or f"CodeGraph不可用: {status.state}")
        return CodeGraphTurnHint(mode=normalized.value, status=status, turn_context=turn_context)
    suggestion = _suggestion(turn_context)
    return CodeGraphTurnHint(
        mode=normalized.value,
        status=status,
        turn_context=turn_context,
        suggestion=suggestion,
        full_text=_full_hint(suggestion),
        reminder_text=_reminder(suggestion),
    )


def build_codegraph_hint_block(
    profile: CodeGraphTurnHint,
    business_prompt: str,
    *,
    include_full_guide: bool,
) -> str:
    prompt = str(business_prompt or "").strip()
    if not profile.enabled:
        return prompt
    hint = profile.full_text if include_full_guide else profile.reminder_text
    return f"{hint}\n\n{prompt}".strip()


_QUOTED_ABSOLUTE_PATH_RE = re.compile(
    r'''(?P<quote>["'])(?:(?:file|vscode)://[^\r\n]*?|[A-Za-z]:[\\/][^\r\n]*?|\\\\[^\r\n]*?|(?:~[\\/]|/)[^\r\n]*?)(?P=quote)''',
    re.IGNORECASE,
)


def _redact_absolute_paths(value: str) -> str:
    # Quoted paths are handled first so spaces cannot leave a sensitive suffix
    # behind after the unquoted token-based rules run.
    text = _QUOTED_ABSOLUTE_PATH_RE.sub("<redacted-path>", str(value or ""))
    text = re.sub(
        r"(?i)(?<![A-Za-z0-9_.])(?:file|vscode)://[^\s,;]+",
        "<redacted-path>",
        text,
    )
    text = re.sub(
        r"(?<![A-Za-z0-9_.])(?:[A-Za-z]:[\\/]|\\\\)[^\s,;]+",
        "<redacted-path>",
        text,
    )
    return re.sub(
        r"(?<![A-Za-z0-9_.])(?:~|/)[^\s,;]+",
        "<redacted-path>",
        text,
    )


def _sanitize_error(value: object) -> str:
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", str(value or ""))
    text = _redact_absolute_paths(text)
    return " ".join(text.split())[:500]


def _sanitize_query_output(text: str, *, project: Path) -> str:
    cleaned = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", str(text or ""))
    cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", cleaned)
    cleaned = cleaned.replace(str(project), ".")
    home = str(Path.home())
    if home:
        cleaned = cleaned.replace(home, "<home>")
    return _redact_absolute_paths(cleaned).strip()


def execute_readonly_query(
    project_dir: str | Path,
    command: str,
    values: Sequence[str],
    *,
    config: CodeGraphConfig | None = None,
) -> CodeGraphQueryResult:
    project = _project_root(project_dir)
    effective = config or CodeGraphConfig()
    command_text = str(command or "").strip().casefold()
    if command_text not in {"explore", "status"}:
        raise CodeGraphUnavailable(f"Agent只允许只读命令: explore/status；拒绝 {command_text or '<empty>'}")
    tool = resolve_codegraph_tool()
    if not tool.compatible:
        raise CodeGraphUnavailable(tool.error)
    status = inspect_codegraph_project(project, mode=os.environ.get("TMUX_CODEGRAPH_MODE", "auto"))
    if not status.initialized:
        raise CodeGraphUnavailable("当前项目尚未初始化 CodeGraph")
    if status.worktree_mismatch or status.state not in {
        CodeGraphState.READY.value,
        CodeGraphState.STALE.value,
    }:
        raise CodeGraphUnavailable(
            status.last_error or f"当前 CodeGraph 索引不可安全查询: {status.state}"
        )
    if command_text == "status":
        argv = [tool.executable_path, "status", "--json", str(project)]
    else:
        query = _safe_seed(" ".join(str(value) for value in values), max_chars=1_000)
        if not query or query.startswith("-"):
            raise CodeGraphContractError("CodeGraph explore需要非选项查询文本")
        argv = [tool.executable_path, "explore", "--path", str(project), "--max-files", str(effective.max_files), query]
    started = time.monotonic()
    try:
        code, output, truncated = _run_process(
            argv,
            timeout_sec=effective.sync_timeout_sec,
            cwd=project,
            max_output_chars=effective.max_output_chars,
        )
        result_text = _sanitize_query_output(output, project=project)
        return CodeGraphQueryResult(
            ok=code == 0,
            query_id=uuid.uuid4().hex,
            command=command_text,
            freshness=status.freshness,
            truncated=truncated,
            duration_ms=int((time.monotonic() - started) * 1000),
            result_text=result_text if code == 0 else "",
            warnings=(("索引不是 fresh，仅供导航，必须核验当前源码。",) if status.freshness != "fresh" else ()),
            error_kind="" if code == 0 else "query_failed",
        )
    except (OSError, subprocess.SubprocessError) as exc:
        error_kind = "timeout" if isinstance(exc, subprocess.TimeoutExpired) else (
            "spawn_failed" if isinstance(exc, OSError) else "query_failed"
        )
        return CodeGraphQueryResult(
            ok=False,
            query_id=uuid.uuid4().hex,
            command=command_text,
            freshness=status.freshness,
            truncated=False,
            duration_ms=int((time.monotonic() - started) * 1000),
            result_text="",
            error_kind=error_kind,
        )


def run_readonly_query(
    project_dir: str | Path,
    command: str,
    values: Sequence[str],
    *,
    output_format: str = "text",
) -> str:
    result = execute_readonly_query(project_dir, command, values)
    if output_format == "json":
        return json.dumps(result.to_public_dict(), ensure_ascii=False)
    if output_format != "text":
        raise CodeGraphContractError("format必须为 text 或 json")
    return result.to_text()


def _manifest() -> Mapping[str, Any]:
    path = PROJECT_ROOT / "tools" / "codegraph" / "checksums.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CodeGraphUnavailable(f"CodeGraph checksums manifest不可用: {type(exc).__name__}") from exc
    if not isinstance(payload, Mapping):
        raise CodeGraphUnavailable("CodeGraph checksums manifest格式错误")
    return payload


def _safe_archive_destination(root: Path, name: str) -> Path:
    member = Path(str(name or ""))
    if member.is_absolute() or ".." in member.parts:
        raise CodeGraphContractError("CodeGraph发布包包含越界路径")
    destination = (root / member).resolve()
    try:
        destination.relative_to(root.resolve())
    except ValueError as exc:
        raise CodeGraphContractError("CodeGraph发布包包含越界路径") from exc
    return destination


def _copy_limited(source: Any, output: Any, *, limit: int, label: str) -> int:
    copied = 0
    while True:
        chunk = source.read(min(1024 * 1024, limit - copied + 1))
        if not chunk:
            return copied
        copied += len(chunk)
        if copied > limit:
            raise CodeGraphContractError(f"CodeGraph {label}超过大小上限")
        output.write(chunk)


def _validate_archive_sizes(sizes: Sequence[int]) -> None:
    if len(sizes) > CODEGRAPH_MAX_ARCHIVE_MEMBERS:
        raise CodeGraphContractError("CodeGraph发布包成员数量超过上限")
    total = 0
    for size in sizes:
        if size < 0 or size > CODEGRAPH_MAX_ARCHIVE_MEMBER_BYTES:
            raise CodeGraphContractError("CodeGraph发布包单成员超过大小上限")
        total += size
        if total > CODEGRAPH_MAX_EXTRACTED_BYTES:
            raise CodeGraphContractError("CodeGraph发布包解压总量超过上限")


def _extracted_mode(name: str, *, directory: bool = False) -> int:
    if directory:
        return 0o755
    return 0o755 if Path(name).name.casefold() in {"codegraph", "codegraph.exe"} else 0o644


def _extract_release_archive(archive: Path, destination: Path) -> None:
    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as bundle:
            members = bundle.infolist()
            _validate_archive_sizes([int(member.file_size) for member in members])
            extracted_total = 0
            for member in members:
                target = _safe_archive_destination(destination, member.filename)
                unix_mode = (member.external_attr >> 16) & 0xFFFF
                file_type = unix_mode & 0o170000
                if file_type not in {0, stat.S_IFREG, stat.S_IFDIR}:
                    raise CodeGraphContractError("CodeGraph发布包不允许链接或特殊成员")
                if member.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    target.chmod(_extracted_mode(member.filename, directory=True))
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(member) as source, target.open("wb") as output:
                    copied = _copy_limited(
                        source,
                        output,
                        limit=min(
                            CODEGRAPH_MAX_ARCHIVE_MEMBER_BYTES,
                            CODEGRAPH_MAX_EXTRACTED_BYTES - extracted_total,
                        ),
                        label="发布包解压内容",
                    )
                extracted_total += copied
                target.chmod(_extracted_mode(member.filename))
        return
    with tarfile.open(archive, "r:gz") as bundle:
        members = bundle.getmembers()
        _validate_archive_sizes([int(member.size) for member in members])
        extracted_total = 0
        for member in members:
            target = _safe_archive_destination(destination, member.name)
            if not (member.isfile() or member.isdir()):
                raise CodeGraphContractError("CodeGraph发布包不允许链接或特殊成员")
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                target.chmod(_extracted_mode(member.name, directory=True))
                continue
            source = bundle.extractfile(member)
            if source is None:
                raise CodeGraphContractError("CodeGraph发布包文件成员不可读取")
            target.parent.mkdir(parents=True, exist_ok=True)
            with source, target.open("wb") as output:
                copied = _copy_limited(
                    source,
                    output,
                    limit=min(
                        CODEGRAPH_MAX_ARCHIVE_MEMBER_BYTES,
                        CODEGRAPH_MAX_EXTRACTED_BYTES - extracted_total,
                    ),
                    label="发布包解压内容",
                )
            extracted_total += copied
            target.chmod(_extracted_mode(member.name))


def _reuse_concurrent_managed_install(destination: Path) -> CodeGraphToolResolution | None:
    """Accept a concurrently published managed generation only after probing it."""

    result = resolve_codegraph_tool(force_refresh=True)
    if not result.compatible or not result.executable_path:
        return None
    try:
        Path(result.executable_path).expanduser().resolve().relative_to(destination.resolve())
    except (OSError, RuntimeError, ValueError):
        return None
    return result


def setup_managed_codegraph() -> CodeGraphToolResolution:
    with _INSTALL_LOCK:
        # Another project's recovery may have completed the shared managed
        # installation while this caller waited.
        existing = resolve_codegraph_tool(force_refresh=True)
        if existing.compatible:
            return existing
        target = _platform_target()
        destination = codegraph_data_root() / target
        install_staging = destination.parent / f".{destination.name}.{uuid.uuid4().hex}.staging"
        published = False
        verified = False
        try:
            assets = _manifest().get("assets", {})
            if not isinstance(assets, Mapping) or not isinstance(assets.get(target), Mapping):
                raise CodeGraphUnavailable(f"没有 {target} 的固定CodeGraph资产")
            asset = assets[target]
            url = str(asset.get("url", "") or "")
            expected = str(asset.get("sha256", "") or "").casefold()
            if not url.startswith("https://github.com/colbymchenry/codegraph/releases/download/v1.5.0/") or not re.fullmatch(r"[0-9a-f]{64}", expected):
                raise CodeGraphContractError("CodeGraph固定资产manifest无效")
            destination.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix="tmux-codegraph-") as temp_dir:
                archive = Path(temp_dir) / Path(url).name
                with urllib.request.urlopen(url, timeout=60) as response, archive.open("wb") as output:
                    _copy_limited(
                        response,
                        output,
                        limit=CODEGRAPH_MAX_ARCHIVE_BYTES,
                        label="下载包",
                    )
                digest = hashlib.sha256()
                with archive.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(chunk)
                actual = digest.hexdigest()
                if actual != expected:
                    raise CodeGraphContractError("CodeGraph下载校验失败")
                extraction = Path(temp_dir) / "extract"
                extraction.mkdir()
                _extract_release_archive(archive, extraction)
                roots = [path for path in extraction.iterdir() if path.is_dir()]
                source = roots[0] if len(roots) == 1 else extraction
                if destination.exists():
                    concurrent = _reuse_concurrent_managed_install(destination)
                    if concurrent is not None:
                        verified = True
                        return concurrent
                    raise CodeGraphContractError(f"托管目录已存在，拒绝覆盖: {destination}")
                # Copy/move across filesystems only into a hidden staging path.
                # The final same-filesystem rename publishes an all-or-nothing
                # managed generation.
                shutil.move(str(source), str(install_staging))
                if destination.exists():
                    concurrent = _reuse_concurrent_managed_install(destination)
                    if concurrent is not None:
                        verified = True
                        return concurrent
                    raise CodeGraphContractError(f"托管目录已存在，拒绝覆盖: {destination}")
                os.rename(install_staging, destination)
                published = True
            global _TOOL_CACHE
            with _TOOL_CACHE_LOCK:
                _TOOL_CACHE = None
            result = resolve_codegraph_tool(force_refresh=True)
            if not result.compatible:
                raise CodeGraphUnavailable(result.error)
            verified = True
            return result
        except CodeGraphError:
            raise
        except OSError as exc:
            if destination.exists():
                concurrent = _reuse_concurrent_managed_install(destination)
                if concurrent is not None:
                    verified = True
                    return concurrent
            raise CodeGraphUnavailable(
                f"CodeGraph托管安装失败: {type(exc).__name__}"
            ) from exc
        except (urllib.error.URLError, zipfile.BadZipFile, tarfile.TarError, EOFError) as exc:
            raise CodeGraphUnavailable(
                f"CodeGraph托管安装失败: {type(exc).__name__}"
            ) from exc
        finally:
            with contextlib.suppress(OSError):
                shutil.rmtree(install_staging)
            if published and not verified:
                with contextlib.suppress(OSError):
                    shutil.rmtree(destination)


def enable_codegraph_interactive_recovery(project_dir: str | Path) -> None:
    with _RECOVERY_LOCK:
        _RECOVERY_PROJECTS.add(_project_key(project_dir))


def codegraph_interactive_recovery_enabled(project_dir: str | Path) -> bool:
    with _RECOVERY_LOCK:
        return _project_key(project_dir) in _RECOVERY_PROJECTS


def codegraph_required_recovery_decision(
    project_dir: str | Path,
    *,
    runner_id: str = "",
    interaction_scope: str = "",
) -> str:
    runner_id = str(runner_id or interaction_scope or "").strip()
    if not runner_id:
        return ""
    with _RECOVERY_LOCK:
        return _RECOVERY_DECISIONS.get((_project_key(project_dir), str(runner_id)), "")


def set_codegraph_required_recovery_decision(
    project_dir: str | Path,
    decision: str,
    *,
    runner_id: str = "",
    interaction_scope: str = "",
) -> None:
    runner_id = str(runner_id or interaction_scope or "").strip()
    if not runner_id:
        return
    with _RECOVERY_LOCK:
        _RECOVERY_DECISIONS[(_project_key(project_dir), str(runner_id))] = str(decision or "")


@contextlib.contextmanager
def codegraph_required_recovery_guard(project_dir: str | Path) -> Iterator[None]:
    key = _project_key(project_dir)
    with _RECOVERY_GUARDS_LOCK:
        guard = _RECOVERY_GUARDS.setdefault(key, threading.RLock())
    with guard:
        yield


def _resolve_cli_project(args: argparse.Namespace, *, read_only: bool) -> Path:
    env_project = str(os.environ.get("TMUX_CODEGRAPH_PROJECT_DIR", "") or "").strip()
    explicit = str(getattr(args, "project", "") or "").strip()
    if read_only and env_project:
        project = _project_root(env_project)
        if explicit and _project_root(explicit) != project:
            raise CodeGraphContractError("只读worker禁止跨项目查询")
        return project
    return _project_root(explicit or os.getcwd())


def _build_cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tmux-codegraph", description="TmuxCodingTeam CodeGraph adapter")
    parser.add_argument("--project", default="")
    subparsers = parser.add_subparsers(dest="command", required=True)
    explore = subparsers.add_parser("explore")
    explore.add_argument("query", nargs="+")
    explore.add_argument("--format", choices=("text", "json"), default="text")
    status = subparsers.add_parser("status")
    status.add_argument("--format", choices=("text", "json"), default="json")
    for name in ("sync", "init", "setup", "doctor"):
        subparsers.add_parser(name)
    return parser


def cli_main(argv: Sequence[str] | None = None) -> int:
    parser = _build_cli_parser()
    args = parser.parse_args(argv)
    read_only = str(os.environ.get("TMUX_CODEGRAPH_READ_ONLY", "") or "").strip() == "1"
    if read_only and args.command not in {"explore", "status"}:
        parser.error("worker只读环境仅允许 explore/status")
    try:
        if args.command == "setup":
            resolution = setup_managed_codegraph()
            print(json.dumps(dataclasses.asdict(resolution), ensure_ascii=False))
            return 0
        project = _resolve_cli_project(args, read_only=read_only)
        if args.command == "explore":
            result = execute_readonly_query(project, "explore", args.query)
            if args.format == "json":
                print(json.dumps(result.to_public_dict(), ensure_ascii=False))
            else:
                print(result.to_text())
            return 0 if result.ok else 2
        if args.command == "status":
            status = inspect_codegraph_project(project, mode=os.environ.get("TMUX_CODEGRAPH_MODE", "auto"))
            print(json.dumps(status.to_public_dict(), ensure_ascii=False) if args.format == "json" else f"{status.state} {status.version}")
            return 0
        if args.command == "sync":
            status = sync_codegraph_project(project, mode=os.environ.get("TMUX_CODEGRAPH_MODE", "auto"))
            print(json.dumps(status.to_public_dict(), ensure_ascii=False))
            return 0
        if args.command == "init":
            status = initialize_codegraph_project(project)
            print(json.dumps(status.to_public_dict(), ensure_ascii=False))
            return 0
        if args.command == "doctor":
            print(json.dumps(dataclasses.asdict(resolve_codegraph_tool(force_refresh=True)), ensure_ascii=False))
            return 0
    except CodeGraphError as exc:
        print(f"CodeGraph: {_sanitize_error(exc)}", file=os.sys.stderr)
        return 2
    return 2


__all__ = [
    "CODEGRAPH_FULL_GUIDE_MARKER",
    "CODEGRAPH_GUIDE_VERSION",
    "CODEGRAPH_VERSION",
    "CodeGraphConfig",
    "CodeGraphContractError",
    "CodeGraphError",
    "CodeGraphMode",
    "CodeGraphProjectStatus",
    "CodeGraphQueryIntent",
    "CodeGraphQueryResult",
    "CodeGraphState",
    "CodeGraphStatus",
    "CodeGraphSyncFailed",
    "CodeGraphToolResolution",
    "CodeGraphTurnContext",
    "CodeGraphTurnHint",
    "CodeGraphTurnProfile",
    "CodeGraphUnavailable",
    "build_codegraph_hint_block",
    "cancel_codegraph_processes",
    "cli_main",
    "codegraph_interactive_recovery_enabled",
    "codegraph_required_recovery_decision",
    "codegraph_required_recovery_guard",
    "enable_codegraph_interactive_recovery",
    "execute_readonly_query",
    "initialize_codegraph_project",
    "inspect_codegraph_project",
    "managed_codegraph_executable",
    "normalize_codegraph_config",
    "normalize_codegraph_mode",
    "publish_codegraph_off_status",
    "publish_codegraph_pending_status",
    "read_codegraph_project_status",
    "read_codegraph_project_preference",
    "resolve_codegraph_tool",
    "resolve_codegraph_turn_profile",
    "run_readonly_query",
    "set_codegraph_required_recovery_decision",
    "setup_managed_codegraph",
    "sync_codegraph_project",
    "write_codegraph_project_preference",
]
