from __future__ import annotations

import argparse
import atexit
import contextlib
import dataclasses
import datetime as dt
import enum
import fnmatch
import hashlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

try:  # pragma: no cover - Windows compatibility is exercised by import tests.
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]


GRAPHIFY_PACKAGE = "graphifyy"
GRAPHIFY_VERSION = "0.9.27"
GRAPHIFY_ADAPTER_VERSION = "1"
GRAPHIFY_GRAPH_SCHEMA = "graphify-code-graph-v1"
GRAPHIFY_MAX_GRAPH_BYTES = 256 * 1024 * 1024
GRAPHIFY_EVIDENCE_MAX_CHARS = 6_000
PROJECT_ROOT = Path(__file__).resolve().parents[2]


class GraphifyMode(str, enum.Enum):
    OFF = "off"
    AUTO = "auto"
    REQUIRED = "required"


class GraphifyState(str, enum.Enum):
    OFF = "off"
    UNAVAILABLE = "unavailable"
    BUILDING = "building"
    READY = "ready"
    STALE = "stale"
    DEGRADED = "degraded"
    FAILED = "failed"


class GraphifyError(RuntimeError):
    """Base class for project-managed Graphify failures."""


class GraphifyUnavailable(GraphifyError):
    pass


class GraphifyBuildFailed(GraphifyError):
    pass


class GraphifySchemaError(GraphifyError):
    pass


class GraphifySnapshotError(GraphifyError):
    pass


@dataclasses.dataclass(frozen=True)
class GraphifyToolResolution:
    executable_path: str = ""
    version: str = ""
    source: str = ""
    compatible: bool = False
    error: str = ""


@dataclasses.dataclass(frozen=True)
class GraphifyBuildConfig:
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    max_workers: int = 1
    initial_timeout_sec: float = 120.0
    incremental_timeout_sec: float = 30.0
    max_file_bytes: int = 2 * 1024 * 1024
    max_files: int = 50_000
    max_total_bytes: int = 512 * 1024 * 1024

    def __post_init__(self) -> None:
        if int(self.max_workers) != 1:
            raise ValueError("Graphify 第一版固定 max_workers=1")


@dataclasses.dataclass(frozen=True)
class GraphifyProjectSnapshot:
    project_dir: str
    project_key: str
    source_dir: str
    source_fingerprint: str
    manifest_path: str
    file_count: int
    total_bytes: int


@dataclasses.dataclass(frozen=True)
class GraphifyEvidence:
    evidence_id: str
    graph_fingerprint: str
    freshness: str
    extracted_nodes: tuple[str, ...] = ()
    extracted_edges: tuple[str, ...] = ()
    inferred_candidates: tuple[str, ...] = ()
    related_paths: tuple[str, ...] = ()
    routed_module_candidates: tuple[str, ...] = ()
    report_path: str = ""
    block_text: str = ""


@dataclasses.dataclass(frozen=True)
class TurnContextBlock:
    source: str
    content: str
    evidence_id: str = ""


@dataclasses.dataclass(frozen=True)
class GraphifyStatus:
    mode: str
    state: str
    version: str = ""
    freshness: str = ""
    generated_at: str = ""
    source_fingerprint: str = ""
    node_count: int = 0
    edge_count: int = 0
    evidence_id: str = ""
    direct_count: int = 0
    inferred_count: int = 0
    report_path: str = ""
    last_error: str = ""

    def to_public_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True)
class GraphifyTurnProfile:
    mode: str = GraphifyMode.OFF.value
    evidence: GraphifyEvidence | None = None
    status: GraphifyStatus | None = None
    routed_paths: tuple[str, ...] = ()
    changed_files: tuple[str, ...] = ()
    symbols: tuple[str, ...] = ()
    query_seeds: tuple[str, ...] = ()
    prompt_file_references: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "mode",
            normalize_graphify_mode(self.mode, default=GraphifyMode.OFF).value,
        )
        for field_name in (
            "routed_paths",
            "changed_files",
            "symbols",
            "query_seeds",
            "prompt_file_references",
        ):
            values = getattr(self, field_name)
            if isinstance(values, (str, Path)):
                values = (values,)
            normalized = tuple(
                dict.fromkeys(
                    text
                    for item in (values or ())
                    if (text := str(item or "").strip())
                )
            )
            object.__setattr__(self, field_name, normalized)

    @property
    def enabled(self) -> bool:
        return self.mode != GraphifyMode.OFF.value and self.evidence is not None


@dataclasses.dataclass(frozen=True)
class _GraphGeneration:
    graph_path: Path
    fingerprint: str
    source_fingerprint: str
    node_count: int
    edge_count: int
    freshness: str
    generated_at: str


_SOURCE_EXTENSIONS = {
    ".py", ".pyi", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx",
    ".java", ".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp",
    ".go", ".rs", ".rb", ".php", ".swift", ".kt", ".kts", ".scala",
    ".sh", ".bash", ".zsh", ".sql", ".vue", ".svelte", ".cs", ".fs",
    ".fsx", ".lua", ".r", ".ex", ".exs", ".erl", ".hrl", ".clj",
    ".cljs", ".groovy", ".dart",
}
_SOURCE_BASENAMES = {"dockerfile", "makefile", "justfile", "rakefile"}
_EXCLUDED_DIR_NAMES = {
    ".git", ".hg", ".svn", ".tmux_workflow", ".routing_init_runtime",
    ".requirement_intake_runtime", ".requirement_clarification_runtime",
    ".requirements_review_runtime", ".detailed_design_runtime",
    ".task_split_runtime", ".development_runtime", ".overall_review_runtime",
    ".agent_init_runtime", "node_modules", "vendor", "venv", ".venv", "env",
    ".env", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    ".tox", ".nox", ".cache", "coverage", ".coverage", "htmlcov", "build",
    "dist", "target", "out", "graphify-out", ".graphify", ".next", ".nuxt",
    ".tmp", "tmp", "temp", "generated",
}
_SENSITIVE_SUFFIXES = {
    ".pem", ".key", ".p12", ".pfx", ".jks", ".keystore", ".crt", ".cer",
    ".der", ".ovpn", ".kdbx", ".sqlite", ".sqlite3", ".db", ".dump",
    ".zip", ".tar", ".gz", ".tgz", ".7z", ".rar", ".pickle", ".pkl",
}
_SENSITIVE_NAMES = {
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", ".netrc", ".npmrc",
    ".pypirc", "credentials", "credentials.json", "service-account.json",
    "secrets.json", "secret.json",
}
_MODEL_ENV_MARKERS = (
    "OPENAI", "ANTHROPIC", "GEMINI", "GOOGLE_API", "GOOGLE_GENAI",
    "MOONSHOT", "KIMI", "DEEPSEEK", "AZURE", "AWS_", "BEDROCK", "OLLAMA",
    "MISTRAL", "COHERE", "GROQ", "TOGETHER", "FIREWORKS",
)
_MODEL_ENV_EXACT_KEYS = {
    "GOOGLE_APPLICATION_CREDENTIALS",
    "CLOUDSDK_AUTH_ACCESS_TOKEN",
    "CLOUDSDK_CONFIG",
    "GOOGLE_CLOUD_ACCESS_TOKEN",
    "AZURE_CLIENT_SECRET",
    "AZURE_FEDERATED_TOKEN_FILE",
    "AWS_WEB_IDENTITY_TOKEN_FILE",
}
_MODEL_ENV_PREFIXES = (
    "VERTEX_",
    "GCLOUD_",
    "CLOUDSDK_AUTH_",
    "GOOGLE_CLOUD_",
)
_QUERY_COMMANDS = {"query", "affected", "path", "explain", "god-nodes"}
_WRITE_COMMANDS = {"setup", "build", "prune"}
_NODE_REFERENCE_KEYS = ("id", "key", "name", "label", "qualified_name", "title")
_EDGE_SOURCE_KEYS = (
    "source", "from", "src", "source_id", "sourceId",
    "source_node", "sourceNode", "source_node_id", "sourceNodeId",
)
_EDGE_TARGET_KEYS = (
    "target", "to", "dst", "target_id", "targetId",
    "target_node", "targetNode", "target_node_id", "targetNodeId",
)
_COMMON_QUERY_STOP_WORDS = {
    "the", "and", "for", "from", "with", "this", "that", "into", "only",
    "must", "should", "please", "project", "file", "files", "code", "task",
    "stage", "agent", "当前", "项目", "文件", "代码", "任务", "阶段", "智能体",
    "需要", "以及", "进行", "实现", "检查", "分析", "修改", "使用",
}

_PROJECT_LOCKS: dict[str, threading.RLock] = {}
_PROJECT_LOCKS_GUARD = threading.Lock()
_ACTIVE_PROCESSES: set[subprocess.Popen[str]] = set()
_ACTIVE_PROCESSES_GUARD = threading.Lock()
_TOOL_RESOLUTION_LOCK = threading.Lock()
_TOOL_RESOLUTION_CACHE: tuple[tuple[str, str, str], float, GraphifyToolResolution] | None = None
_INTERACTIVE_RECOVERY_PROJECTS: set[str] = set()
_REQUIRED_RECOVERY_LOCK = threading.RLock()
_REQUIRED_RECOVERY_DECISIONS: dict[tuple[str, str], str] = {}
_MAX_RECOVERY_DECISIONS = 256


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def normalize_graphify_mode(
    value: GraphifyMode | str | None,
    *,
    default: GraphifyMode = GraphifyMode.OFF,
) -> GraphifyMode:
    if isinstance(value, GraphifyMode):
        return value
    text = str(value or "").strip().lower()
    if not text:
        return default
    try:
        return GraphifyMode(text)
    except ValueError as exc:
        allowed = ", ".join(mode.value for mode in GraphifyMode)
        raise ValueError(f"非法 graphify_mode: {value!r}; 合法值: {allowed}") from exc


def _recovery_project_token(project_dir: str | Path) -> str:
    try:
        return str(Path(project_dir).expanduser().resolve())
    except OSError:
        return str(project_dir or "").strip()


def enable_graphify_interactive_recovery(project_dir: str | Path) -> None:
    token = _recovery_project_token(project_dir)
    if token:
        _INTERACTIVE_RECOVERY_PROJECTS.add(token)


def graphify_interactive_recovery_enabled(project_dir: str | Path) -> bool:
    return _recovery_project_token(project_dir) in _INTERACTIVE_RECOVERY_PROJECTS


@contextlib.contextmanager
def graphify_required_recovery_guard() -> Iterator[None]:
    with _REQUIRED_RECOVERY_LOCK:
        yield


def _recovery_decision_key(
    project_dir: str | Path,
    interaction_scope: str,
) -> tuple[str, str] | None:
    project_token = _recovery_project_token(project_dir)
    scope = str(interaction_scope or "").strip()
    if not project_token or not scope:
        return None
    return project_token, scope


def graphify_required_recovery_decision(
    project_dir: str | Path,
    *,
    interaction_scope: str = "",
) -> str:
    key = _recovery_decision_key(project_dir, interaction_scope)
    if key is None:
        # Legacy callers without a runner/generation scope remain compatible,
        # but must not create project-global decisions that leak to later runs.
        return ""
    with _REQUIRED_RECOVERY_LOCK:
        return str(_REQUIRED_RECOVERY_DECISIONS.get(key, "") or "")


def set_graphify_required_recovery_decision(
    project_dir: str | Path,
    value: str,
    *,
    interaction_scope: str = "",
) -> None:
    key = _recovery_decision_key(project_dir, interaction_scope)
    if key is None:
        return
    with _REQUIRED_RECOVERY_LOCK:
        _REQUIRED_RECOVERY_DECISIONS[key] = str(value or "").strip().lower()
        while len(_REQUIRED_RECOVERY_DECISIONS) > _MAX_RECOVERY_DECISIONS:
            _REQUIRED_RECOVERY_DECISIONS.pop(next(iter(_REQUIRED_RECOVERY_DECISIONS)))


def _xdg_path(env_name: str, fallback: Path) -> Path:
    configured = str(os.environ.get(env_name, "") or "").strip()
    return Path(configured).expanduser() if configured else fallback.expanduser()


def graphify_data_root() -> Path:
    return _xdg_path("XDG_DATA_HOME", Path.home() / ".local" / "share") / "tmux_coding_team" / "tools" / "graphify"


def graphify_cache_root() -> Path:
    return _xdg_path("XDG_CACHE_HOME", Path.home() / ".cache") / "tmux_coding_team" / "graphify"


def managed_graphify_executable() -> Path:
    name = "graphify.exe" if os.name == "nt" else "graphify"
    scripts_dir = "Scripts" if os.name == "nt" else "bin"
    return graphify_data_root() / GRAPHIFY_VERSION / scripts_dir / name


def _project_root(project_dir: str | Path) -> Path:
    path = Path(project_dir).expanduser().resolve()
    if not path.is_dir():
        raise GraphifySnapshotError(f"Graphify 项目目录不存在: {path}")
    return path


def project_cache_key(project_dir: str | Path) -> str:
    return hashlib.sha256(str(_project_root(project_dir)).encode("utf-8")).hexdigest()


def project_cache_dir(project_dir: str | Path) -> Path:
    return graphify_cache_root() / project_cache_key(project_dir)


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(dict(payload), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def _status_path(project_dir: str | Path) -> Path:
    return project_cache_dir(project_dir) / "status.json"


def _write_status(project_dir: str | Path, status: GraphifyStatus) -> None:
    _atomic_write_json(_status_path(project_dir), status.to_public_dict())


def publish_graphify_off_status(project_dir: str | Path) -> bool:
    """Clear stale project graph UI state without probing or scanning source."""

    try:
        _write_status(
            project_dir,
            GraphifyStatus(mode=GraphifyMode.OFF.value, state=GraphifyState.OFF.value),
        )
    except OSError:
        # UI metadata must never prevent an otherwise valid worker launch.
        return False
    return True


def publish_graphify_pending_status(
    project_dir: str | Path,
    mode: GraphifyMode | str,
) -> bool:
    normalized = normalize_graphify_mode(mode, default=GraphifyMode.AUTO)
    if normalized == GraphifyMode.OFF:
        return publish_graphify_off_status(project_dir)
    previous = read_graphify_project_status(project_dir) or {}
    try:
        _write_status(
            project_dir,
            GraphifyStatus(
                mode=normalized.value,
                state=GraphifyState.BUILDING.value,
                version=str(previous.get("version", "") or ""),
                freshness="pending_refresh",
            ),
        )
    except OSError:
        return False
    return True


def read_graphify_project_status(project_dir: str | Path) -> dict[str, Any] | None:
    """Read cached public status without probing executables or scanning source."""
    try:
        path = _status_path(project_dir)
    except GraphifyError:
        return None
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    allowed = {field.name for field in dataclasses.fields(GraphifyStatus)}
    return {key: payload[key] for key in allowed if key in payload}


def _version_from_output(output: str) -> str:
    match = re.search(r"(?<!\d)(\d+\.\d+\.\d+)(?!\d)", str(output or ""))
    return match.group(1) if match else ""


def _probe_tool(path: Path, *, source: str) -> GraphifyToolResolution:
    try:
        resolved = path.expanduser().resolve(strict=True)
    except OSError as exc:
        return GraphifyToolResolution(source=source, error=f"executable not found: {exc}")
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        return GraphifyToolResolution(source=source, error="path is not an executable file")
    try:
        version_process = subprocess.run(
            [str(resolved), "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            env=_sanitized_graphify_env(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return GraphifyToolResolution(source=source, error=f"version probe failed: {exc}")
    version = _version_from_output(f"{version_process.stdout}\n{version_process.stderr}")
    if version != GRAPHIFY_VERSION:
        found = version or "unknown"
        return GraphifyToolResolution(
            executable_path=str(resolved),
            version=found,
            source=source,
            error=f"Graphify version mismatch: required {GRAPHIFY_VERSION}, found {found}",
        )
    try:
        help_process = subprocess.run(
            [str(resolved), "--help"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            env=_sanitized_graphify_env(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return GraphifyToolResolution(
            executable_path=str(resolved), version=version, source=source,
            error=f"CLI contract probe failed: {exc}",
        )
    help_text = f"{help_process.stdout}\n{help_process.stderr}".lower()
    missing = [command for command in ("extract", "update", "query", "affected", "path", "god-nodes") if command not in help_text]
    if help_process.returncode != 0 or missing:
        return GraphifyToolResolution(
            executable_path=str(resolved), version=version, source=source,
            error="CLI contract missing commands: " + ", ".join(missing),
        )
    return GraphifyToolResolution(
        executable_path=str(resolved), version=version, source=source, compatible=True,
    )


def _tool_resolution_key() -> tuple[str, str, str]:
    explicit = str(os.environ.get("TMUX_GRAPHIFY_EXECUTABLE", "") or "").strip()
    managed = managed_graphify_executable()
    try:
        managed_marker = f"{managed}:{managed.stat().st_mtime_ns}:{managed.stat().st_size}"
    except OSError:
        managed_marker = str(managed)
    return explicit, managed_marker, str(shutil.which("graphify") or "")


def _clear_tool_resolution_cache() -> None:
    global _TOOL_RESOLUTION_CACHE
    with _TOOL_RESOLUTION_LOCK:
        _TOOL_RESOLUTION_CACHE = None


def resolve_graphify_tool(*, force_refresh: bool = False) -> GraphifyToolResolution:
    global _TOOL_RESOLUTION_CACHE
    cache_key = _tool_resolution_key()
    if not force_refresh:
        with _TOOL_RESOLUTION_LOCK:
            cached = _TOOL_RESOLUTION_CACHE
            if cached is not None and cached[0] == cache_key and time.monotonic() - cached[1] <= 15.0:
                return cached[2]
    explicit = str(os.environ.get("TMUX_GRAPHIFY_EXECUTABLE", "") or "").strip()
    if explicit:
        explicit_path = Path(explicit).expanduser()
        if not explicit_path.is_absolute():
            result = GraphifyToolResolution(
                source="environment",
                error="TMUX_GRAPHIFY_EXECUTABLE 必须是绝对路径",
            )
        else:
            result = _probe_tool(explicit_path, source="environment")
    else:
        managed = managed_graphify_executable()
        result = GraphifyToolResolution()
        if managed.exists():
            result = _probe_tool(managed, source="managed")
        if not result.compatible:
            path_candidate = shutil.which("graphify")
            if path_candidate:
                result = _probe_tool(Path(path_candidate), source="path")
        if not result.compatible and not result.error:
            result = GraphifyToolResolution(
                source="none",
                error=(
                    f"Graphify {GRAPHIFY_VERSION} 不可用。运行 scripts/tmux-graphify setup，"
                    "或将 TMUX_GRAPHIFY_EXECUTABLE 指向通过契约探测的绝对路径。"
                ),
            )
    with _TOOL_RESOLUTION_LOCK:
        _TOOL_RESOLUTION_CACHE = (cache_key, time.monotonic(), result)
    return result


def _sanitized_graphify_env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    environment: dict[str, str] = {}
    for key, value in os.environ.items():
        upper = key.upper()
        if (
            upper in _MODEL_ENV_EXACT_KEYS
            or any(upper.startswith(prefix) for prefix in _MODEL_ENV_PREFIXES)
            or any(marker in upper for marker in _MODEL_ENV_MARKERS)
        ):
            continue
        environment[key] = value
    environment["GRAPHIFY_QUERY_LOG_DISABLE"] = "1"
    if extra:
        environment.update({str(key): str(value) for key, value in extra.items()})
    return environment


def _register_process(process: subprocess.Popen[str]) -> None:
    with _ACTIVE_PROCESSES_GUARD:
        _ACTIVE_PROCESSES.add(process)


def _unregister_process(process: subprocess.Popen[str]) -> None:
    with _ACTIVE_PROCESSES_GUARD:
        _ACTIVE_PROCESSES.discard(process)


def _terminate_process_group(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "nt":  # pragma: no cover
            process.terminate()
        else:
            os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=3)
    except Exception:
        try:
            if os.name == "nt":  # pragma: no cover
                process.kill()
            else:
                os.killpg(process.pid, signal.SIGKILL)
        except Exception:
            pass


def cancel_graphify_processes() -> None:
    with _ACTIVE_PROCESSES_GUARD:
        processes = tuple(_ACTIVE_PROCESSES)
    for process in processes:
        _terminate_process_group(process)
        _unregister_process(process)


atexit.register(cancel_graphify_processes)


def _run_graphify_process(
    args: Sequence[str],
    *,
    cwd: Path,
    timeout_sec: float,
    environment: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        process = subprocess.Popen(
            [str(value) for value in args],
            cwd=str(cwd),
            env=dict(environment) if environment is not None else _sanitized_graphify_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=(os.name != "nt"),
        )
    except OSError as exc:
        raise GraphifyBuildFailed(f"Graphify process launch failed: {exc}") from exc
    _register_process(process)
    try:
        try:
            stdout, stderr = process.communicate(timeout=max(float(timeout_sec), 0.1))
        except subprocess.TimeoutExpired as exc:
            _terminate_process_group(process)
            raise GraphifyBuildFailed(f"Graphify process timed out after {timeout_sec:g}s") from exc
        except BaseException:
            # KeyboardInterrupt/SystemExit must not strand a tree-sitter build
            # after the TUI, Web backend, or legacy CLI has already exited.
            _terminate_process_group(process)
            raise
    finally:
        _unregister_process(process)
    completed = subprocess.CompletedProcess(args=list(args), returncode=process.returncode, stdout=stdout, stderr=stderr)
    if completed.returncode != 0:
        detail = " ".join((stderr or stdout or "unknown error").split())[:1_000]
        raise GraphifyBuildFailed(f"Graphify command failed ({completed.returncode}): {detail}")
    return completed


def _project_lock(project_key: str) -> threading.RLock:
    with _PROJECT_LOCKS_GUARD:
        return _PROJECT_LOCKS.setdefault(project_key, threading.RLock())


@contextlib.contextmanager
def _build_lock(cache_dir: Path, *, timeout_sec: float = 180.0) -> Iterator[None]:
    lock = _project_lock(cache_dir.name)
    with lock:
        cache_dir.mkdir(parents=True, exist_ok=True)
        lock_path = cache_dir / "build.lock"
        handle = lock_path.open("a+", encoding="utf-8")
        try:
            if fcntl is not None:
                deadline = time.monotonic() + max(timeout_sec, 0.1)
                while True:
                    try:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            raise GraphifyBuildFailed("等待 Graphify 项目构建锁超时")
                        time.sleep(0.1)
            yield
        finally:
            if fcntl is not None:
                with contextlib.suppress(OSError):
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()


def _is_sensitive_path(relative: Path) -> bool:
    lowered_parts = tuple(part.lower() for part in relative.parts)
    if any(part in _EXCLUDED_DIR_NAMES for part in lowered_parts[:-1]):
        return True
    name = relative.name.lower()
    if name.startswith(".env") or name in _SENSITIVE_NAMES:
        return True
    if relative.suffix.lower() in _SENSITIVE_SUFFIXES:
        return True
    if any(marker in name for marker in ("credential", "private_key", "private-key", "secret")):
        return True
    return False


def _is_source_path(relative: Path) -> bool:
    return relative.suffix.lower() in _SOURCE_EXTENSIONS or relative.name.lower() in _SOURCE_BASENAMES


def _matches_scan_policy(relative: Path, config: GraphifyBuildConfig) -> bool:
    relative_text = relative.as_posix()
    if config.include and not any(fnmatch.fnmatch(relative_text, pattern) for pattern in config.include):
        return False
    if config.exclude and any(fnmatch.fnmatch(relative_text, pattern) for pattern in config.exclude):
        return False
    return True


def _git_source_candidates(project: Path) -> list[str] | None:
    try:
        inside = subprocess.run(
            ["git", "-C", str(project), "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        return None
    try:
        listed = subprocess.run(
            ["git", "-C", str(project), "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise GraphifySnapshotError(f"git source listing failed: {exc}") from exc
    if listed.returncode != 0:
        detail = listed.stderr.decode("utf-8", errors="replace") if isinstance(listed.stderr, bytes) else str(listed.stderr or "")
        raise GraphifySnapshotError(f"git source listing failed: {detail.strip()}")
    raw = listed.stdout if isinstance(listed.stdout, bytes) else str(listed.stdout).encode()
    return [item.decode("utf-8", errors="surrogateescape") for item in raw.split(b"\0") if item]


def _git_clean_blob_ids(project: Path) -> dict[str, str]:
    """Return index blob IDs only for files whose worktree bytes are unchanged."""
    try:
        index = subprocess.run(
            ["git", "-C", str(project), "ls-files", "-s", "-z"],
            capture_output=True,
            timeout=30,
            check=False,
        )
        dirty = subprocess.run(
            ["git", "-C", str(project), "diff", "--name-only", "-z"],
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    if index.returncode != 0 or dirty.returncode != 0:
        return {}
    dirty_raw = dirty.stdout if isinstance(dirty.stdout, bytes) else str(dirty.stdout).encode()
    dirty_paths = {
        item.decode("utf-8", errors="surrogateescape")
        for item in dirty_raw.split(b"\0")
        if item
    }
    index_raw = index.stdout if isinstance(index.stdout, bytes) else str(index.stdout).encode()
    result: dict[str, str] = {}
    for record in index_raw.split(b"\0"):
        if not record or b"\t" not in record:
            continue
        metadata, raw_path = record.split(b"\t", 1)
        metadata_parts = metadata.split()
        if len(metadata_parts) < 3 or metadata_parts[2] != b"0":
            continue
        path = raw_path.decode("utf-8", errors="surrogateescape")
        if path not in dirty_paths:
            result[path] = metadata_parts[1].decode("ascii", errors="ignore")
    return result


def _filesystem_source_candidates(project: Path) -> list[str]:
    candidates: list[str] = []
    for root, dir_names, file_names in os.walk(project, followlinks=False):
        root_path = Path(root)
        dir_names[:] = [
            name for name in dir_names
            if name.lower() not in _EXCLUDED_DIR_NAMES and not (root_path / name).is_symlink()
        ]
        for name in file_names:
            path = root_path / name
            with contextlib.suppress(ValueError):
                candidates.append(path.relative_to(project).as_posix())
    return candidates


def _has_symlink_component(project: Path, source: Path) -> bool:
    try:
        relative = source.relative_to(project)
    except ValueError:
        return True
    current = project
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            return True
    return False


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _read_snapshot_source(
    *,
    project: Path,
    source: Path,
    relative: Path,
    max_file_bytes: int,
) -> bytes | None:
    """Read one stable, regular, in-project file without following symlinks."""

    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(source, flags)
    except OSError:
        return None
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            return None
        if before.st_size > max_file_bytes:
            raise GraphifySnapshotError(
                f"Graphify 源文件超过 {max_file_bytes} bytes 上限: "
                f"{relative.as_posix()} ({before.st_size})"
            )
        chunks: list[bytes] = []
        remaining = max_file_bytes + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        content = b"".join(chunks)
        if len(content) > max_file_bytes:
            raise GraphifySnapshotError(
                f"Graphify 源文件超过 {max_file_bytes} bytes 上限: {relative.as_posix()}"
            )
        after = os.fstat(descriptor)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise GraphifySnapshotError(f"Graphify 扫描期间源码发生变化: {relative.as_posix()}")
    finally:
        os.close(descriptor)

    try:
        current = os.stat(source, follow_symlinks=False)
        resolved = source.resolve(strict=True)
        resolved.relative_to(project)
    except (OSError, ValueError):
        raise GraphifySnapshotError(f"Graphify 扫描期间源码路径发生变化: {relative.as_posix()}")
    if stat.S_ISLNK(current.st_mode) or (current.st_dev, current.st_ino) != (after.st_dev, after.st_ino):
        raise GraphifySnapshotError(f"Graphify 扫描期间源码被替换: {relative.as_posix()}")
    # Extension allowlisting is necessary but not sufficient: generated test
    # fixtures can still carry binary bytes under a source-looking suffix.
    if b"\0" in content[:8192]:
        return None
    return content


def _git_blob_content_id(blob_id: str, content: bytes) -> str:
    normalized = str(blob_id or "").strip().lower()
    algorithm = "sha1" if len(normalized) == 40 else "sha256" if len(normalized) == 64 else ""
    if algorithm:
        digest = hashlib.new(algorithm)
        digest.update(f"blob {len(content)}\0".encode("ascii"))
        digest.update(content)
        if digest.hexdigest() == normalized:
            return f"git:{normalized}"
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def create_graphify_snapshot(
    project_dir: str | Path,
    destination: str | Path,
    *,
    config: GraphifyBuildConfig | None = None,
) -> GraphifyProjectSnapshot:
    config = config or GraphifyBuildConfig()
    project = _project_root(project_dir)
    destination_path = Path(destination).expanduser().resolve()
    destination_path.mkdir(parents=True, exist_ok=True)
    candidates = _git_source_candidates(project)
    clean_blob_ids: dict[str, str] = {}
    if candidates is None:
        candidates = _filesystem_source_candidates(project)
    else:
        clean_blob_ids = _git_clean_blob_ids(project)
    records: list[dict[str, Any]] = []
    total_bytes = 0
    for candidate in sorted(set(candidates)):
        relative = Path(candidate)
        if relative.is_absolute() or ".." in relative.parts:
            continue
        if _is_sensitive_path(relative) or not _is_source_path(relative) or not _matches_scan_policy(relative, config):
            continue
        source = project / relative
        try:
            resolved = source.resolve(strict=True)
            resolved.relative_to(project)
        except (OSError, ValueError):
            continue
        if _has_symlink_component(project, source) or not resolved.is_file():
            continue
        content = _read_snapshot_source(
            project=project,
            source=source,
            relative=relative,
            max_file_bytes=config.max_file_bytes,
        )
        if content is None:
            continue
        size = len(content)
        total_bytes += size
        if total_bytes > config.max_total_bytes:
            raise GraphifySnapshotError(
                f"Graphify 输入超过总量上限 {config.max_total_bytes} bytes；不会静默截断"
            )
        if len(records) + 1 > config.max_files:
            raise GraphifySnapshotError(
                f"Graphify 输入超过文件数上限 {config.max_files}；不会静默截断"
            )
        blob_id = clean_blob_ids.get(relative.as_posix(), "")
        content_hash = _git_blob_content_id(blob_id, content)
        target = destination_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        records.append({"path": relative.as_posix(), "size": size, "content_id": content_hash})
    fingerprint_payload = {
        "graphify_version": GRAPHIFY_VERSION,
        "adapter_version": GRAPHIFY_ADAPTER_VERSION,
        "schema": GRAPHIFY_GRAPH_SCHEMA,
        "include": list(config.include),
        "exclude": list(config.exclude),
        "files": records,
    }
    fingerprint = hashlib.sha256(
        json.dumps(fingerprint_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    manifest_path = destination_path.parent / "source-manifest.json"
    _atomic_write_json(
        manifest_path,
        {
            **fingerprint_payload,
            "project_key": project_cache_key(project),
            "source_fingerprint": fingerprint,
            "file_count": len(records),
            "total_bytes": total_bytes,
        },
    )
    return GraphifyProjectSnapshot(
        project_dir=str(project),
        project_key=project_cache_key(project),
        source_dir=str(destination_path),
        source_fingerprint=fingerprint,
        manifest_path=str(manifest_path),
        file_count=len(records),
        total_bytes=total_bytes,
    )


def _graph_lists(payload: Mapping[str, Any]) -> tuple[list[Any], list[Any]]:
    nodes = payload.get("nodes", [])
    edges = payload.get("edges", payload.get("links", []))
    if not isinstance(nodes, list) or not isinstance(edges, list):
        raise GraphifySchemaError("graph.json 必须包含 list 类型的 nodes 以及 edges/links")
    return nodes, edges


def _node_source_path(node: Mapping[str, Any]) -> str:
    for key in ("file_path", "filepath", "source_path", "source_file", "file"):
        value = node.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    metadata = node.get("metadata")
    if isinstance(metadata, Mapping):
        for key in ("file_path", "filepath", "source_path", "source_file", "file"):
            value = metadata.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def _graph_reference(value: Any) -> str:
    if isinstance(value, Mapping):
        for key in _NODE_REFERENCE_KEYS:
            nested = value.get(key)
            if nested is not None and not isinstance(nested, (Mapping, list, tuple, set, dict)):
                text = str(nested).strip()
                if text:
                    return text
        return ""
    if value is None or isinstance(value, (list, tuple, set, dict)):
        return ""
    return str(value).strip()


def _node_references(node: Mapping[str, Any]) -> tuple[str, ...]:
    references: list[str] = []
    for key in _NODE_REFERENCE_KEYS:
        reference = _graph_reference(node.get(key))
        if reference and reference not in references:
            references.append(reference)
    return tuple(references)


def _edge_end(edge: Mapping[str, Any], keys: Sequence[str]) -> str:
    for key in keys:
        reference = _graph_reference(edge.get(key))
        if reference:
            return reference
    return ""


def _load_and_validate_graph(
    graph_path: Path,
    *,
    snapshot_source: Path | None = None,
    manifest_paths: set[str] | None = None,
) -> tuple[dict[str, Any], int, int]:
    try:
        size = graph_path.stat().st_size
    except OSError as exc:
        raise GraphifySchemaError(f"graph.json 不存在: {graph_path}") from exc
    if size <= 0 or size > GRAPHIFY_MAX_GRAPH_BYTES:
        raise GraphifySchemaError(f"graph.json 大小非法: {size}")
    try:
        payload = json.loads(graph_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GraphifySchemaError(f"graph.json 无法解析: {exc}") from exc
    if not isinstance(payload, dict):
        raise GraphifySchemaError("graph.json 根节点必须是 object")
    nodes, edges = _graph_lists(payload)
    source_root = snapshot_source.resolve() if snapshot_source is not None else None
    known_node_references: set[str] = set()
    for index, item in enumerate(nodes):
        if not isinstance(item, Mapping):
            raise GraphifySchemaError(f"graph node[{index}] 必须是 object")
        references = _node_references(item)
        if not references:
            raise GraphifySchemaError(f"graph node[{index}] 缺少可引用标识")
        known_node_references.update(references)
        if source_root is not None and manifest_paths is not None:
            raw_path = _node_source_path(item)
            if not raw_path:
                continue
            lowered_raw_path = raw_path.strip().lower()
            if (
                (lowered_raw_path.startswith("<") and lowered_raw_path.endswith(">"))
                or lowered_raw_path in {"external", "builtin", "builtins", "generated", "unknown"}
            ):
                continue
            candidate = Path(raw_path)
            if candidate.is_absolute():
                try:
                    relative = candidate.resolve().relative_to(source_root).as_posix()
                except (OSError, ValueError) as exc:
                    raise GraphifySchemaError(f"graph node 源码路径越界: {raw_path}") from exc
            else:
                normalized = raw_path.replace("\\", "/").lstrip("./")
                marker = "/graphify-out/"
                if marker in normalized:
                    continue
                relative = normalized
            if relative and relative not in manifest_paths:
                # Some parsers store a directory/package path rather than a concrete file.
                is_source_like = Path(relative).suffix.lower() in _SOURCE_EXTENSIONS
                if is_source_like and not any(path.startswith(relative.rstrip("/") + "/") for path in manifest_paths):
                    raise GraphifySchemaError(f"graph node 路径不在受控源码清单中: {relative}")
    for index, item in enumerate(edges):
        if not isinstance(item, Mapping):
            raise GraphifySchemaError(f"graph edge[{index}] 必须是 object")
        source = _edge_end(item, _EDGE_SOURCE_KEYS)
        target = _edge_end(item, _EDGE_TARGET_KEYS)
        if not source or not target:
            raise GraphifySchemaError(f"graph edge[{index}] 缺少合法 source/target 端点")
        unknown = [reference for reference in (source, target) if reference not in known_node_references]
        if unknown:
            rendered = ", ".join(unknown)
            raise GraphifySchemaError(f"graph edge[{index}] 引用了未知节点: {rendered}")
    return payload, len(nodes), len(edges)


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return dict(payload) if isinstance(payload, dict) else {}


def _validate_cached_generation(
    cache_dir: Path,
    fingerprint: str,
    *,
    freshness: str,
) -> _GraphGeneration | None:
    if not fingerprint or not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
        return None
    generation_dir = cache_dir / "graphs" / fingerprint
    graph_path = generation_dir / "graph.json"
    manifest_path = generation_dir / "source-manifest.json"
    metadata = _read_json_object(generation_dir / "metadata.json")
    manifest = _read_json_object(manifest_path)
    if (
        str(metadata.get("schema", "") or "") != GRAPHIFY_GRAPH_SCHEMA
        or str(metadata.get("graphify_version", "") or "") != GRAPHIFY_VERSION
        or str(metadata.get("adapter_version", "") or "") != GRAPHIFY_ADAPTER_VERSION
        or str(metadata.get("source_fingerprint", "") or "") != fingerprint
        or str(manifest.get("source_fingerprint", "") or "") != fingerprint
    ):
        return None
    expected_graph_hash = str(metadata.get("graph_sha256", "") or "").strip()
    expected_manifest_hash = str(metadata.get("manifest_sha256", "") or "").strip()
    try:
        if not expected_graph_hash or _hash_file(graph_path) != expected_graph_hash:
            return None
        if not expected_manifest_hash or _hash_file(manifest_path) != expected_manifest_hash:
            return None
    except OSError:
        return None
    try:
        payload, node_count, edge_count = _load_and_validate_graph(graph_path)
    except GraphifySchemaError:
        return None
    manifest_paths = _manifest_path_set(manifest_path)
    if not manifest_paths and int(manifest.get("file_count", 0) or 0) > 0:
        return None
    for item in _graph_lists(payload)[0]:
        if not isinstance(item, Mapping):
            return None
        raw_path = _node_source_path(item)
        if not raw_path:
            continue
        lowered = raw_path.strip().lower()
        if (
            (lowered.startswith("<") and lowered.endswith(">"))
            or lowered in {"external", "builtin", "builtins", "generated", "unknown"}
        ):
            continue
        relative = _relative_graph_path(raw_path, graph_path=graph_path)
        if Path(raw_path).is_absolute() and not relative:
            return None
        if relative and Path(relative).suffix.lower() in _SOURCE_EXTENSIONS and relative not in manifest_paths:
            return None
    return _GraphGeneration(
        graph_path=graph_path,
        fingerprint=fingerprint,
        source_fingerprint=fingerprint,
        node_count=node_count,
        edge_count=edge_count,
        freshness=str(freshness or "fresh"),
        generated_at=str(metadata.get("generated_at", "") or ""),
    )


def _current_generation(cache_dir: Path) -> _GraphGeneration | None:
    current = _read_json_object(cache_dir / "current.json")
    if (
        str(current.get("version", "") or "") != GRAPHIFY_VERSION
        or str(current.get("schema", "") or "") != GRAPHIFY_GRAPH_SCHEMA
    ):
        return None
    fingerprint = str(current.get("fingerprint", "") or "").strip()
    generation = _validate_cached_generation(
        cache_dir,
        fingerprint,
        freshness=str(current.get("freshness", "fresh") or "fresh"),
    )
    if generation is not None:
        return generation
    previous_fingerprint = str(current.get("previous_fingerprint", "") or "").strip()
    return _validate_cached_generation(
        cache_dir,
        previous_fingerprint,
        freshness="cache_fallback",
    )


def _cleanup_staging(cache_dir: Path, *, max_age_sec: float = 24 * 60 * 60) -> None:
    staging_root = cache_dir / "staging"
    if not staging_root.is_dir():
        return
    threshold = time.time() - max_age_sec
    for child in staging_root.iterdir():
        with contextlib.suppress(OSError):
            if child.stat().st_mtime < threshold:
                shutil.rmtree(child, ignore_errors=True)


def _manifest_path_set(manifest_path: Path) -> set[str]:
    payload = _read_json_object(manifest_path)
    files = payload.get("files", [])
    if not isinstance(files, list):
        return set()
    return {
        str(item.get("path", "") or "").strip()
        for item in files
        if isinstance(item, Mapping) and str(item.get("path", "") or "").strip()
    }


def _publish_generation(
    *,
    cache_dir: Path,
    snapshot: GraphifyProjectSnapshot,
    built_graph_path: Path,
    previous: _GraphGeneration | None,
) -> _GraphGeneration:
    manifest_path = Path(snapshot.manifest_path)
    payload, node_count, edge_count = _load_and_validate_graph(
        built_graph_path,
        snapshot_source=Path(snapshot.source_dir),
        manifest_paths=_manifest_path_set(manifest_path),
    )
    fingerprint = snapshot.source_fingerprint
    generation_dir = cache_dir / "graphs" / fingerprint
    temporary_dir = cache_dir / "graphs" / f".{fingerprint}.{uuid.uuid4().hex}.tmp"
    backup_dir = cache_dir / "graphs" / f".{fingerprint}.{uuid.uuid4().hex}.bak"
    temporary_dir.mkdir(parents=True, exist_ok=False)
    try:
        graph_text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        _atomic_write_text(
            temporary_dir / "graph.json",
            graph_text,
        )
        shutil.copyfile(manifest_path, temporary_dir / "source-manifest.json")
        generated_at = _now_iso()
        _atomic_write_json(
            temporary_dir / "metadata.json",
            {
                "schema": GRAPHIFY_GRAPH_SCHEMA,
                "graphify_version": GRAPHIFY_VERSION,
                "adapter_version": GRAPHIFY_ADAPTER_VERSION,
                "source_fingerprint": snapshot.source_fingerprint,
                "node_count": node_count,
                "edge_count": edge_count,
                "generated_at": generated_at,
                "graph_sha256": hashlib.sha256(graph_text.encode("utf-8")).hexdigest(),
                "manifest_sha256": _hash_file(temporary_dir / "source-manifest.json"),
            },
        )
        if generation_dir.exists():
            os.replace(generation_dir, backup_dir)
        try:
            os.replace(temporary_dir, generation_dir)
        except BaseException:
            if backup_dir.exists() and not generation_dir.exists():
                os.replace(backup_dir, generation_dir)
            raise
    finally:
        if temporary_dir.exists():
            shutil.rmtree(temporary_dir, ignore_errors=True)
        if backup_dir.exists():
            shutil.rmtree(backup_dir, ignore_errors=True)
    generated_at = _read_json_object(generation_dir / "metadata.json").get("generated_at", _now_iso())
    current_payload = {
        "schema": GRAPHIFY_GRAPH_SCHEMA,
        "version": GRAPHIFY_VERSION,
        "fingerprint": fingerprint,
        "source_fingerprint": snapshot.source_fingerprint,
        "previous_fingerprint": previous.fingerprint if previous and previous.fingerprint != fingerprint else "",
        "freshness": "fresh",
        "generated_at": generated_at,
        "node_count": node_count,
        "edge_count": edge_count,
    }
    _atomic_write_json(cache_dir / "current.json", current_payload)
    keep = {fingerprint, str(current_payload["previous_fingerprint"])} - {""}
    graphs_root = cache_dir / "graphs"
    for child in graphs_root.iterdir():
        if child.is_dir() and not child.name.startswith(".") and child.name not in keep:
            shutil.rmtree(child, ignore_errors=True)
    return _GraphGeneration(
        graph_path=generation_dir / "graph.json",
        fingerprint=fingerprint,
        source_fingerprint=snapshot.source_fingerprint,
        node_count=node_count,
        edge_count=edge_count,
        freshness="fresh",
        generated_at=str(generated_at),
    )


def _build_or_refresh_graph(
    project_dir: str | Path,
    resolution: GraphifyToolResolution,
    *,
    config: GraphifyBuildConfig,
    mode: GraphifyMode,
) -> _GraphGeneration | None:
    project = _project_root(project_dir)
    cache_dir = project_cache_dir(project)
    cache_dir.mkdir(parents=True, exist_ok=True)
    with _build_lock(cache_dir):
        _cleanup_staging(cache_dir)
        previous = _current_generation(cache_dir)
        build_id = uuid.uuid4().hex
        staging = cache_dir / "staging" / build_id
        source_dir = staging / "source"
        working_dir = staging / "working"
        source_dir.mkdir(parents=True, exist_ok=False)
        working_dir.mkdir(parents=True, exist_ok=False)
        try:
            _write_status(
                project,
                GraphifyStatus(
                    mode=mode.value,
                    state=GraphifyState.BUILDING.value,
                    version=GRAPHIFY_VERSION,
                    freshness="preparing_snapshot",
                ),
            )
            snapshot = create_graphify_snapshot(project, source_dir, config=config)
            if previous and previous.source_fingerprint == snapshot.source_fingerprint:
                return dataclasses.replace(previous, freshness="fresh")
            _write_status(
                project,
                GraphifyStatus(
                    mode=mode.value,
                    state=GraphifyState.BUILDING.value,
                    version=GRAPHIFY_VERSION,
                    freshness="building",
                    source_fingerprint=snapshot.source_fingerprint,
                ),
            )
            executable = resolution.executable_path
            if previous is None:
                command = [
                    executable,
                    "extract",
                    str(source_dir),
                    "--code-only",
                    "--no-cluster",
                    "--max-workers",
                    str(max(1, int(config.max_workers))),
                    "--force",
                    "--out",
                    str(working_dir),
                ]
                timeout_sec = config.initial_timeout_sec
                built_graph_path = working_dir / "graphify-out" / "graph.json"
            else:
                graphify_out = source_dir / "graphify-out"
                graphify_out.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(previous.graph_path, graphify_out / "graph.json")
                command = [
                    executable,
                    "update",
                    str(source_dir),
                    "--no-cluster",
                    "--force",
                ]
                timeout_sec = config.incremental_timeout_sec
                built_graph_path = graphify_out / "graph.json"
            _run_graphify_process(command, cwd=working_dir, timeout_sec=timeout_sec)
            return _publish_generation(
                cache_dir=cache_dir,
                snapshot=snapshot,
                built_graph_path=built_graph_path,
                previous=previous,
            )
        except Exception as exc:
            # The caller owns the mode policy. Propagating the refresh error is
            # important: AUTO may deliberately reuse the previous generation,
            # but its public status must still retain the real degradation
            # reason instead of presenting a silent cache hit.
            if isinstance(exc, GraphifyError):
                raise
            raise GraphifyBuildFailed(str(exc)) from exc
        finally:
            shutil.rmtree(staging, ignore_errors=True)


def _node_identity(node: Mapping[str, Any], index: int) -> str:
    for key in ("id", "key", "name", "label", "qualified_name", "title"):
        value = node.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return f"node:{index}"


def _node_label(node: Mapping[str, Any], index: int) -> str:
    for key in ("label", "qualified_name", "name", "title", "id"):
        value = node.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return _node_identity(node, index)


def _edge_relation(edge: Mapping[str, Any]) -> str:
    for key in ("relation", "type", "kind", "label"):
        value = edge.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return "related_to"


def _relative_graph_path(raw_path: str, *, graph_path: Path) -> str:
    text = str(raw_path or "").replace("\\", "/").strip()
    if not text:
        return ""
    source_marker = "/source/"
    if source_marker in text:
        return text.rsplit(source_marker, 1)[1].lstrip("/")
    if text.startswith("./"):
        return text[2:]
    if Path(text).is_absolute():
        # Never expose cache paths. A path that cannot be mapped is omitted.
        return ""
    if text.startswith("graphify-out/"):
        return ""
    return text


def _prompt_tokens(prompt: str) -> tuple[str, ...]:
    tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_./:-]{2,}|[\u4e00-\u9fff]{2,}", str(prompt or ""))
    normalized: list[str] = []
    for token in tokens:
        lowered = token.strip("`'\".,:;()[]{}<>").lower()
        if len(lowered) < 3 or lowered in _COMMON_QUERY_STOP_WORDS:
            continue
        if lowered not in normalized:
            normalized.append(lowered)
        if len(normalized) >= 64:
            break
    return tuple(normalized)


_PROMPT_FILE_REFERENCE_RE = re.compile(
    r"(?<![\w./-])((?:/|(?:\./|\.\./))?(?:[\w@+.-]+/)*[\w@+.-]+\.[A-Za-z0-9]{1,12}(?::\d+(?::\d+)?)?)"
)


def _normalize_project_relative_path(project: Path, value: str | Path) -> str:
    text = str(value or "").strip().strip("`'\"<>()[]{}")
    text = re.sub(r":\d+(?::\d+)?$", "", text).replace("\\", "/")
    if not text:
        return ""
    candidate = Path(text).expanduser()
    if not candidate.is_absolute() and ".." in candidate.parts:
        return ""
    try:
        resolved = candidate.resolve(strict=False) if candidate.is_absolute() else (project / candidate).resolve(strict=False)
        relative = resolved.relative_to(project).as_posix()
    except (OSError, ValueError):
        return ""
    return relative if relative not in {"", "."} else ""


def _normalize_path_seeds(project: Path, values: Sequence[str | Path] | None) -> tuple[str, ...]:
    normalized: list[str] = []
    if isinstance(values, (str, Path)):
        values = (values,)
    for value in values or ():
        relative = _normalize_project_relative_path(project, value)
        if relative and relative not in normalized:
            normalized.append(relative)
    return tuple(normalized)


def _normalize_text_seeds(values: Sequence[str] | None) -> tuple[str, ...]:
    normalized: list[str] = []
    if isinstance(values, str):
        values = (values,)
    for value in values or ():
        text = str(value or "").strip()
        if text and text not in normalized:
            normalized.append(text)
    return tuple(normalized)


def _prompt_file_references(project: Path, prompt: str) -> tuple[str, ...]:
    references: list[str] = []
    for match in _PROMPT_FILE_REFERENCE_RE.finditer(str(prompt or "")):
        relative = _normalize_project_relative_path(project, match.group(1))
        if not relative or not _is_source_path(Path(relative)):
            continue
        if relative not in references:
            references.append(relative)
    return tuple(references)


def _path_seed_score(node_path: str, seeds: Sequence[str], *, exact_score: int) -> int:
    normalized_path = str(node_path or "").lower().strip("/")
    if not normalized_path:
        return 0
    path_object = Path(normalized_path)
    best = 0
    for seed in seeds:
        normalized_seed = str(seed or "").lower().strip("/")
        if not normalized_seed:
            continue
        seed_object = Path(normalized_seed)
        if normalized_path == normalized_seed:
            candidate = exact_score
        elif normalized_path.startswith(normalized_seed + "/") or normalized_seed.startswith(normalized_path + "/"):
            candidate = exact_score - 20
        elif path_object.name == seed_object.name:
            candidate = exact_score - 30
        elif path_object.stem and path_object.stem == seed_object.stem:
            candidate = exact_score - 40
        elif normalized_seed in normalized_path:
            candidate = exact_score - 60
        else:
            candidate = 0
        best = max(best, candidate)
    return best


def _routed_module_candidates(project: Path, related_paths: Sequence[str]) -> tuple[str, ...]:
    repo_map_path = project / "docs" / "repo_map.json"
    try:
        resolved_repo_map = repo_map_path.resolve(strict=True)
        resolved_repo_map.relative_to(project)
        payload = json.loads(resolved_repo_map.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return ()
    modules = payload.get("modules", []) if isinstance(payload, Mapping) else []
    if not isinstance(modules, list):
        return ()
    normalized_related = _normalize_path_seeds(project, related_paths)
    candidates: list[str] = []
    for module in modules:
        if not isinstance(module, Mapping):
            continue
        module_id = str(module.get("id", "") or "").strip()
        owned_paths = module.get("owned_paths", [])
        if not module_id or not isinstance(owned_paths, list):
            continue
        matched_paths: list[str] = []
        for owned in owned_paths:
            if not isinstance(owned, Mapping):
                continue
            owned_path = _normalize_project_relative_path(project, str(owned.get("path", "") or ""))
            match_kind = str(owned.get("match", owned.get("type", "")) or "").strip().lower()
            if not owned_path or match_kind not in {"exact", "subtree"}:
                continue
            for related in normalized_related:
                matched = related == owned_path
                if match_kind == "subtree":
                    matched = matched or related.startswith(owned_path.rstrip("/") + "/")
                if matched and related not in matched_paths:
                    matched_paths.append(related)
        if matched_paths:
            rendered_paths = ", ".join(matched_paths[:3])
            candidates.append(f"{module_id} <- {rendered_paths} [needs_code_confirmation]")
        if len(candidates) >= 12:
            break
    return tuple(candidates)


def _build_evidence(
    *,
    project: Path,
    generation: _GraphGeneration,
    prompt: str,
    runtime_dir: Path | None,
    routed_paths: Sequence[str] = (),
    changed_files: Sequence[str] = (),
    symbols: Sequence[str] = (),
    query_seeds: Sequence[str] = (),
    prompt_file_references: Sequence[str] = (),
) -> GraphifyEvidence:
    payload, _, _ = _load_and_validate_graph(generation.graph_path)
    raw_nodes, raw_edges = _graph_lists(payload)
    nodes: list[Mapping[str, Any]] = [item for item in raw_nodes if isinstance(item, Mapping)]
    edges: list[Mapping[str, Any]] = [item for item in raw_edges if isinstance(item, Mapping)]
    tokens = _prompt_tokens(prompt)
    query_tokens = _prompt_tokens(" ".join(query_seeds))
    normalized_symbols = tuple(str(item).strip().lower() for item in symbols if str(item).strip())
    identity_to_label: dict[str, str] = {}
    scored: list[tuple[int, int, Mapping[str, Any]]] = []
    for index, node in enumerate(nodes):
        identity = _node_identity(node, index)
        label = _node_label(node, index)
        identity_to_label[identity] = label
        raw_path = _normalize_project_relative_path(
            project,
            _relative_graph_path(_node_source_path(node), graph_path=generation.graph_path),
        )
        haystack = f"{identity} {label} {raw_path}".lower()
        score = sum(4 if token in label.lower() else 2 if token in haystack else 0 for token in tokens)
        score += sum(8 if token in label.lower() else 4 if token in haystack else 0 for token in query_tokens)
        score += _path_seed_score(raw_path, routed_paths, exact_score=100)
        score += _path_seed_score(raw_path, changed_files, exact_score=120)
        score += _path_seed_score(raw_path, prompt_file_references, exact_score=110)
        lowered_identity = identity.lower()
        lowered_label = label.lower()
        for symbol in normalized_symbols:
            if symbol in {lowered_identity, lowered_label}:
                score += 120
            elif symbol in lowered_identity or symbol in lowered_label:
                score += 60
            elif symbol in haystack:
                score += 30
        degree_hint = int(node.get("degree", 0) or 0) if str(node.get("degree", 0) or "0").isdigit() else 0
        if score > 0:
            scored.append((score, degree_hint, node))
    if not scored:
        # A graph is still useful when a prompt has no exact symbol: expose a few
        # explicit high-connectivity nodes, never fabricate inferred relations.
        for index, node in enumerate(nodes):
            degree_hint = int(node.get("degree", 0) or 0) if str(node.get("degree", 0) or "0").isdigit() else 0
            scored.append((0, degree_hint, node))
    scored.sort(key=lambda item: (item[0], item[1], _node_label(item[2], 0)), reverse=True)
    selected_nodes = [item[2] for item in scored[:12]]
    selected_ids = {_node_identity(node, nodes.index(node)) for node in selected_nodes}
    extracted_nodes: list[str] = []
    related_paths: list[str] = []
    for node in selected_nodes:
        index = nodes.index(node)
        label = _node_label(node, index)
        path = _normalize_project_relative_path(
            project,
            _relative_graph_path(_node_source_path(node), graph_path=generation.graph_path),
        )
        rendered = f"{label} ({path})" if path else label
        if rendered not in extracted_nodes:
            extracted_nodes.append(rendered)
        if path and path not in related_paths:
            related_paths.append(path)
    extracted_edges: list[str] = []
    inferred: list[str] = []
    for edge in edges:
        source = _edge_end(edge, _EDGE_SOURCE_KEYS)
        target = _edge_end(edge, _EDGE_TARGET_KEYS)
        if not source or not target or (source not in selected_ids and target not in selected_ids):
            continue
        relation = _edge_relation(edge)
        source_label = identity_to_label.get(source, source)
        target_label = identity_to_label.get(target, target)
        rendered = f"{source_label} --{relation}--> {target_label}"
        confidence = str(edge.get("confidence", edge.get("provenance", "")) or "").lower()
        if "infer" in confidence or "infer" in relation.lower():
            if rendered not in inferred and len(inferred) < 3:
                inferred.append(rendered)
        elif rendered not in extracted_edges and len(extracted_edges) < 20:
            extracted_edges.append(rendered)
        if len(extracted_edges) >= 20 and len(inferred) >= 3:
            break
    routed_candidates = _routed_module_candidates(project, related_paths)
    evidence_seed_payload = json.dumps(
        {
            "prompt": prompt,
            "routed_paths": list(routed_paths),
            "changed_files": list(changed_files),
            "symbols": list(symbols),
            "query_seeds": list(query_seeds),
            "prompt_file_references": list(prompt_file_references),
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    evidence_id = hashlib.sha256(
        f"{generation.fingerprint}\0{evidence_seed_payload}".encode("utf-8")
    ).hexdigest()[:20]
    lines = [
        "[Graphify Code Graph Evidence]",
        f"- evidence_id: {evidence_id}",
        f"- graph_fingerprint: {generation.fingerprint[:16]}",
        f"- freshness: {generation.freshness}",
        "- authority: navigation evidence only; code/tests/config and AI Hermes routing remain authoritative",
        "- limitation: static extraction cannot prove dynamic import, reflection, generated code, runtime config, or cross-service behavior",
        "EXTRACTED:",
    ]
    lines.extend(f"- node: {item}" for item in extracted_nodes)
    lines.extend(f"- edge: {item}" for item in extracted_edges)
    if related_paths:
        lines.append("RELATED_PATHS:")
        lines.extend(f"- {path}" for path in related_paths[:20])
    if routed_candidates:
        lines.append("ROUTED_MODULE_CANDIDATES (needs_code_confirmation; AI Hermes routing remains authoritative):")
        lines.extend(f"- {candidate}" for candidate in routed_candidates)
    if inferred:
        lines.append("INFERRED_CANDIDATES (must confirm in code):")
        lines.extend(f"- {item}" for item in inferred[:3])
    lines.append("[End Graphify Code Graph Evidence]")
    block_text = "\n".join(lines)
    if len(block_text) > GRAPHIFY_EVIDENCE_MAX_CHARS:
        block_text = block_text[: GRAPHIFY_EVIDENCE_MAX_CHARS - 80].rstrip() + "\n- [truncated within evidence budget]\n[End Graphify Code Graph Evidence]"
    report_path = ""
    if runtime_dir is not None:
        try:
            resolved_runtime = runtime_dir.expanduser().resolve()
            resolved_runtime.relative_to(project)
            report = resolved_runtime / f"graphify_evidence_{evidence_id}.md"
            report_text = "# Graphify evidence\n\n" + block_text + "\n"
            _atomic_write_text(report, report_text)
            report_path = str(report)
        except (OSError, ValueError):
            report_path = ""
    return GraphifyEvidence(
        evidence_id=evidence_id,
        graph_fingerprint=generation.fingerprint,
        freshness=generation.freshness,
        extracted_nodes=tuple(extracted_nodes),
        extracted_edges=tuple(extracted_edges),
        inferred_candidates=tuple(inferred),
        related_paths=tuple(related_paths[:20]),
        routed_module_candidates=routed_candidates,
        report_path=report_path,
        block_text=block_text,
    )


def resolve_graphify_turn_profile(
    *,
    project_dir: str | Path,
    mode: GraphifyMode | str,
    prompt: str,
    runtime_dir: str | Path | None = None,
    config: GraphifyBuildConfig | None = None,
    refresh: bool = True,
    routed_paths: Sequence[str | Path] | None = None,
    changed_files: Sequence[str | Path] | None = None,
    symbols: Sequence[str] | None = None,
    query_seeds: Sequence[str] | None = None,
) -> GraphifyTurnProfile:
    normalized_mode = normalize_graphify_mode(mode)
    project = _project_root(project_dir)
    runtime_path = Path(runtime_dir) if runtime_dir is not None else None
    normalized_routed_paths = _normalize_path_seeds(project, routed_paths)
    normalized_changed_files = _normalize_path_seeds(project, changed_files)
    normalized_symbols = _normalize_text_seeds(symbols)
    normalized_query_seeds = _normalize_text_seeds(query_seeds)
    prompt_references = _prompt_file_references(project, prompt)
    profile_seed_fields = {
        "routed_paths": normalized_routed_paths,
        "changed_files": normalized_changed_files,
        "symbols": normalized_symbols,
        "query_seeds": normalized_query_seeds,
        "prompt_file_references": prompt_references,
    }
    if normalized_mode == GraphifyMode.OFF:
        status = GraphifyStatus(mode=normalized_mode.value, state=GraphifyState.OFF.value)
        _write_status(project, status)
        return GraphifyTurnProfile(mode=normalized_mode.value, status=status, **profile_seed_fields)
    config = config or GraphifyBuildConfig()
    generation: _GraphGeneration | None = None
    error_text = ""
    if not refresh:
        # A launch generation refreshes at most once. Later turns use the
        # immutable current generation and only recompute the bounded evidence
        # selection for their own prompt, avoiding a whole-project hash pass.
        generation = _current_generation(project_cache_dir(project))
        resolution = GraphifyToolResolution(
            version=GRAPHIFY_VERSION,
            source="cache",
            compatible=generation is not None,
            error="当前 launch generation 没有可复用的 Graphify 图" if generation is None else "",
        )
        if generation is None and normalized_mode == GraphifyMode.REQUIRED:
            status = GraphifyStatus(
                mode=normalized_mode.value,
                state=GraphifyState.FAILED.value,
                version=GRAPHIFY_VERSION,
                last_error=resolution.error,
            )
            _write_status(project, status)
            raise GraphifyUnavailable(resolution.error)
    else:
        resolution = resolve_graphify_tool()
    if generation is not None:
        pass
    elif resolution.compatible:
        try:
            generation = _build_or_refresh_graph(
                project,
                resolution,
                config=config,
                mode=normalized_mode,
            )
        except GraphifyError as exc:
            error_text = str(exc)
            if normalized_mode == GraphifyMode.REQUIRED:
                status = GraphifyStatus(
                    mode=normalized_mode.value,
                    state=GraphifyState.FAILED.value,
                    version=resolution.version,
                    last_error=error_text,
                )
                _write_status(project, status)
                raise
            generation = _current_generation(project_cache_dir(project))
            if generation is not None:
                generation = dataclasses.replace(generation, freshness="cache_fallback")
    else:
        error_text = resolution.error
        if normalized_mode == GraphifyMode.REQUIRED:
            status = GraphifyStatus(
                mode=normalized_mode.value,
                state=GraphifyState.FAILED.value,
                version=resolution.version,
                last_error=error_text,
            )
            _write_status(project, status)
            raise GraphifyUnavailable(error_text)
        generation = _current_generation(project_cache_dir(project))
        if generation is not None:
            generation = dataclasses.replace(generation, freshness="cache_fallback")
    if generation is None:
        state = GraphifyState.UNAVAILABLE.value if not resolution.compatible else GraphifyState.DEGRADED.value
        status = GraphifyStatus(
            mode=normalized_mode.value,
            state=state,
            version=resolution.version,
            last_error=error_text,
        )
        _write_status(project, status)
        return GraphifyTurnProfile(mode=normalized_mode.value, status=status, **profile_seed_fields)
    try:
        evidence = _build_evidence(
            project=project,
            generation=generation,
            prompt=prompt,
            runtime_dir=runtime_path,
            routed_paths=normalized_routed_paths,
            changed_files=normalized_changed_files,
            symbols=normalized_symbols,
            query_seeds=normalized_query_seeds,
            prompt_file_references=prompt_references,
        )
    except GraphifyError as exc:
        if normalized_mode == GraphifyMode.REQUIRED:
            raise
        status = GraphifyStatus(
            mode=normalized_mode.value,
            state=GraphifyState.DEGRADED.value,
            version=resolution.version or GRAPHIFY_VERSION,
            freshness=generation.freshness,
            generated_at=generation.generated_at,
            source_fingerprint=generation.source_fingerprint,
            node_count=generation.node_count,
            edge_count=generation.edge_count,
            last_error=str(exc),
        )
        _write_status(project, status)
        return GraphifyTurnProfile(mode=normalized_mode.value, status=status, **profile_seed_fields)
    state = GraphifyState.READY.value if generation.freshness == "fresh" else GraphifyState.STALE.value
    status = GraphifyStatus(
        mode=normalized_mode.value,
        state=state,
        version=resolution.version or GRAPHIFY_VERSION,
        freshness=generation.freshness,
        generated_at=generation.generated_at,
        source_fingerprint=generation.source_fingerprint,
        node_count=generation.node_count,
        edge_count=generation.edge_count,
        evidence_id=evidence.evidence_id,
        direct_count=len(evidence.extracted_nodes) + len(evidence.extracted_edges),
        inferred_count=len(evidence.inferred_candidates),
        report_path=evidence.report_path,
        last_error=error_text,
    )
    _write_status(project, status)
    return GraphifyTurnProfile(
        mode=normalized_mode.value,
        evidence=evidence,
        status=status,
        **profile_seed_fields,
    )


def build_graphify_evidence_block(profile: GraphifyTurnProfile, business_prompt: str) -> str:
    if not isinstance(profile, GraphifyTurnProfile) or not profile.enabled or profile.evidence is None:
        return str(business_prompt or "").strip()
    return f"{profile.evidence.block_text}\n\n{str(business_prompt or '').strip()}".strip()


def graphify_turn_context_block(profile: GraphifyTurnProfile) -> TurnContextBlock | None:
    if not isinstance(profile, GraphifyTurnProfile) or not profile.enabled or profile.evidence is None:
        return None
    return TurnContextBlock(
        source="graphify",
        content=profile.evidence.block_text,
        evidence_id=profile.evidence.evidence_id,
    )


def setup_managed_graphify() -> GraphifyToolResolution:
    uv = shutil.which("uv")
    if not uv:
        raise GraphifyUnavailable(
            "缺少 uv。请先按 https://docs.astral.sh/uv/ 安装 uv，再运行 scripts/tmux-graphify setup；"
            "系统不会执行远程 shell 安装脚本。"
        )
    tool_project = PROJECT_ROOT / "tools" / "graphify"
    lock_path = tool_project / "uv.lock"
    if not (tool_project / "pyproject.toml").is_file() or not lock_path.is_file():
        raise GraphifyUnavailable("项目内 Graphify tool manifest/uv.lock 缺失")
    data_root = graphify_data_root()
    data_root.mkdir(parents=True, exist_ok=True)
    target = data_root / GRAPHIFY_VERSION
    with _build_lock(data_root / f"setup-{GRAPHIFY_VERSION}", timeout_sec=660):
        current = _probe_tool(managed_graphify_executable(), source="managed") if target.exists() else GraphifyToolResolution()
        if current.compatible:
            _clear_tool_resolution_cache()
            return current
        staging = data_root / f".{GRAPHIFY_VERSION}.{uuid.uuid4().hex}.tmp"
        backup = data_root / f".{GRAPHIFY_VERSION}.{uuid.uuid4().hex}.bak"
        environment = _sanitized_graphify_env({"UV_PROJECT_ENVIRONMENT": str(staging)})
        try:
            try:
                _run_graphify_process(
                    [uv, "sync", "--project", str(tool_project), "--locked", "--python", "3.11", "--no-dev"],
                    cwd=tool_project,
                    timeout_sec=600,
                    environment=environment,
                )
            except GraphifyBuildFailed as exc:
                raise GraphifyUnavailable(f"managed Graphify setup failed: {exc}") from exc
            scripts_dir = "Scripts" if os.name == "nt" else "bin"
            staged_executable = staging / scripts_dir / ("graphify.exe" if os.name == "nt" else "graphify")
            staged_result = _probe_tool(staged_executable, source="managed-staging")
            if not staged_result.compatible:
                raise GraphifyUnavailable(staged_result.error or "managed Graphify contract probe failed")
            if target.exists():
                os.replace(target, backup)
            try:
                os.replace(staging, target)
            except BaseException:
                if backup.exists() and not target.exists():
                    os.replace(backup, target)
                raise
            shutil.rmtree(backup, ignore_errors=True)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
            if backup.exists() and target.exists():
                shutil.rmtree(backup, ignore_errors=True)
        _clear_tool_resolution_cache()
        result = _probe_tool(managed_graphify_executable(), source="managed")
        if not result.compatible:
            raise GraphifyUnavailable(result.error or "managed Graphify contract probe failed")
        return result


def _current_graph_or_raise(project_dir: str | Path) -> _GraphGeneration:
    generation = _current_generation(project_cache_dir(project_dir))
    if generation is None:
        raise GraphifyUnavailable("当前项目没有可用 Graphify 图；请先运行 scripts/tmux-graphify build --project <path>")
    return generation


def _sanitize_query_output(text: str, *, project: Path, graph_path: Path) -> str:
    value = str(text or "")
    cache_dir = project_cache_dir(project)
    replacements = {
        str(cache_dir): "<graph-cache>",
        str(graph_path): "<graph-cache>/graph.json",
    }
    for source, target in sorted(replacements.items(), key=lambda item: len(item[0]), reverse=True):
        value = value.replace(source, target)
    return value


def run_readonly_query(project_dir: str | Path, command: str, values: Sequence[str]) -> str:
    command_text = str(command or "").strip().lower()
    if command_text not in _QUERY_COMMANDS:
        raise GraphifyUnavailable(f"只读 Graphify wrapper 不允许命令: {command_text}")
    mode = normalize_graphify_mode(os.environ.get("TMUX_GRAPHIFY_MODE", GraphifyMode.AUTO.value), default=GraphifyMode.AUTO)
    if mode == GraphifyMode.OFF:
        raise GraphifyUnavailable("本 runner 已关闭 Graphify")
    project = _project_root(project_dir)
    generation = _current_graph_or_raise(project)
    resolution = resolve_graphify_tool()
    if not resolution.compatible:
        raise GraphifyUnavailable(resolution.error)
    args = [resolution.executable_path, command_text, *[str(value) for value in values], "--graph", str(generation.graph_path)]
    if command_text == "query" and "--budget" not in values:
        args.extend(["--budget", "1500"])
    # Query only the immutable cached graph.  Keeping cwd inside the cache also
    # prevents a future upstream CLI behavior change from implicitly walking
    # or writing to the user's target project.
    completed = _run_graphify_process(args, cwd=generation.graph_path.parent, timeout_sec=30)
    return _sanitize_query_output(completed.stdout, project=project, graph_path=generation.graph_path).strip()


def _prune_cache_directory(cache_dir: Path) -> None:
    _cleanup_staging(cache_dir, max_age_sec=0)
    current = _read_json_object(cache_dir / "current.json")
    keep = {
        str(current.get("fingerprint", "") or ""),
        str(current.get("previous_fingerprint", "") or ""),
    } - {""}
    graphs = cache_dir / "graphs"
    if graphs.is_dir():
        for child in graphs.iterdir():
            if child.is_dir() and child.name not in keep:
                shutil.rmtree(child, ignore_errors=True)


def prune_graphify_cache(project_dir: str | Path, *, all_projects: bool = False) -> None:
    if all_projects:
        root = graphify_cache_root()
        if root.is_dir():
            for child in root.iterdir():
                if child.is_dir():
                    _prune_cache_directory(child)
        return
    _prune_cache_directory(project_cache_dir(project_dir))


def _cli_project(args: argparse.Namespace) -> Path:
    value = str(getattr(args, "project", "") or os.environ.get("TMUX_GRAPHIFY_PROJECT_DIR", "") or os.getcwd()).strip()
    return _project_root(value)


def _build_cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tmux-graphify", description="Project-managed Graphify adapter")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("setup", help="install pinned Graphify into the managed environment")
    doctor = subparsers.add_parser("doctor", help="probe the pinned CLI and contract")
    doctor.add_argument("--json", action="store_true")
    status = subparsers.add_parser("status", help="show cached project graph status")
    status.add_argument("--project", default="")
    build = subparsers.add_parser("build", help="build or refresh the controlled project graph")
    build.add_argument("--project", required=True)
    build.add_argument("--mode", choices=("auto", "required"), default="required")
    query = subparsers.add_parser("query", help="read-only graph query")
    query.add_argument("question")
    query.add_argument("--project", default="")
    affected = subparsers.add_parser("affected", help="read-only reverse impact query")
    affected.add_argument("symbol")
    affected.add_argument("--project", default="")
    path = subparsers.add_parser("path", help="read-only shortest path query")
    path.add_argument("source")
    path.add_argument("target")
    path.add_argument("--project", default="")
    explain = subparsers.add_parser("explain", help="read-only node explanation")
    explain.add_argument("symbol")
    explain.add_argument("--project", default="")
    god_nodes = subparsers.add_parser("god-nodes", help="read-only hub query")
    god_nodes.add_argument("--top", type=int, default=10)
    god_nodes.add_argument("--project", default="")
    prune = subparsers.add_parser("prune", help="remove stale staging and old generations")
    prune.add_argument("--project", default="")
    prune.add_argument("--all-projects", action="store_true")
    return parser


def cli_main(argv: Sequence[str] | None = None) -> int:
    parser = _build_cli_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    command = str(args.command)
    read_only = str(os.environ.get("TMUX_GRAPHIFY_READ_ONLY", "") or "").strip().lower() in {"1", "true", "yes", "on"}
    if read_only and command not in _QUERY_COMMANDS:
        parser.error(f"agent read-only mode rejects command: {command}")
    try:
        if command == "setup":
            result = setup_managed_graphify()
            print(f"Graphify {result.version} ready ({result.source})")
            return 0
        if command == "doctor":
            result = resolve_graphify_tool()
            payload = {
                "compatible": result.compatible,
                "version": result.version,
                "source": result.source,
                "error": result.error,
            }
            if args.json:
                print(json.dumps(payload, ensure_ascii=False))
            else:
                print("Graphify doctor: " + ("ready" if result.compatible else "unavailable"))
                print(f"version: {result.version or '-'}")
                print(f"source: {result.source or '-'}")
                if result.error:
                    print(f"error: {result.error}")
            return 0 if result.compatible else 1
        if command == "status":
            payload = read_graphify_project_status(_cli_project(args)) or {
                "mode": "off", "state": "off",
            }
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0
        if command == "build":
            profile = resolve_graphify_turn_profile(
                project_dir=_cli_project(args),
                mode=args.mode,
                prompt="project graph build",
                runtime_dir=None,
            )
            print(json.dumps(profile.status.to_public_dict() if profile.status else {}, ensure_ascii=False, indent=2))
            return 0 if profile.status and profile.status.state in {"ready", "stale"} else 1
        if command in _QUERY_COMMANDS:
            project = _cli_project(args)
            values: list[str]
            if command == "query":
                values = [args.question]
            elif command in {"affected", "explain"}:
                values = [args.symbol]
            elif command == "path":
                values = [args.source, args.target]
            else:
                values = ["--top", str(max(1, int(args.top)))]
            print(run_readonly_query(project, command, values))
            return 0
        if command == "prune":
            prune_graphify_cache(_cli_project(args), all_projects=bool(args.all_projects))
            print("Graphify cache pruned")
            return 0
    except GraphifyError as exc:
        print(f"Graphify error: {exc}", file=sys.stderr)
        return 1
    parser.error(f"unsupported command: {command}")
    return 2


__all__ = [
    "GRAPHIFY_ADAPTER_VERSION",
    "GRAPHIFY_PACKAGE",
    "GRAPHIFY_VERSION",
    "GraphifyBuildConfig",
    "GraphifyBuildFailed",
    "GraphifyEvidence",
    "GraphifyMode",
    "GraphifyProjectSnapshot",
    "GraphifySchemaError",
    "GraphifySnapshotError",
    "GraphifyStatus",
    "GraphifyToolResolution",
    "GraphifyTurnProfile",
    "GraphifyUnavailable",
    "TurnContextBlock",
    "build_graphify_evidence_block",
    "cancel_graphify_processes",
    "cli_main",
    "create_graphify_snapshot",
    "graphify_cache_root",
    "graphify_data_root",
    "graphify_turn_context_block",
    "managed_graphify_executable",
    "normalize_graphify_mode",
    "project_cache_dir",
    "project_cache_key",
    "prune_graphify_cache",
    "read_graphify_project_status",
    "resolve_graphify_tool",
    "resolve_graphify_turn_profile",
    "run_readonly_query",
    "setup_managed_graphify",
]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(cli_main())
