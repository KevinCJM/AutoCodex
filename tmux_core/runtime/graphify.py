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
import shlex
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
GRAPHIFY_QUERY_MAX_CHARS = 6_000
GRAPHIFY_GUIDE_VERSION = "2"
GRAPHIFY_FULL_GUIDE_MARKER = f"[Graphify Full Usage Guide v{GRAPHIFY_GUIDE_VERSION}]"
GRAPHIFY_QUERY_RESULT_SCHEMA = "tmux-graphify-query-result/1"
GRAPHIFY_QUERY_AUDIT_MAX_BYTES = 1024 * 1024
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


class GraphifyQueryIntent(str, enum.Enum):
    ROUTING_DISCOVERY = "routing_discovery"
    CODE_FACT_DISCOVERY = "code_fact_discovery"
    REQUIREMENT_IMPACT = "requirement_impact"
    ARCHITECTURE_BOUNDARY = "architecture_boundary"
    TASK_DEPENDENCY = "task_dependency"
    IMPLEMENTATION = "implementation"
    CHANGE_REVIEW = "change_review"
    WHOLE_CHANGE_REVIEW = "whole_change_review"


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
class GraphifyTurnContext:
    stage_key: str
    phase: str
    role: str
    intent: GraphifyQueryIntent | str
    requirement_name: str = ""
    task_name: str = ""
    routed_paths: tuple[str, ...] = ()
    changed_files: tuple[str, ...] = ()
    deleted_files: tuple[str, ...] = ()
    symbols: tuple[str, ...] = ()
    query_seeds: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        try:
            intent = self.intent if isinstance(self.intent, GraphifyQueryIntent) else GraphifyQueryIntent(str(self.intent))
        except ValueError as exc:
            allowed = ", ".join(item.value for item in GraphifyQueryIntent)
            raise ValueError(f"非法 Graphify query intent: {self.intent!r}; 合法值: {allowed}") from exc
        object.__setattr__(self, "intent", intent)
        for field_name in (
            "stage_key", "phase", "role", "requirement_name", "task_name",
        ):
            object.__setattr__(self, field_name, str(getattr(self, field_name) or "").strip())
        for field_name in ("routed_paths", "changed_files", "deleted_files", "symbols", "query_seeds"):
            values = getattr(self, field_name)
            if isinstance(values, (str, Path)):
                values = (str(values),)
            object.__setattr__(
                self,
                field_name,
                tuple(dict.fromkeys(str(value).strip() for value in (values or ()) if str(value).strip())),
            )


@dataclasses.dataclass(frozen=True)
class GraphifyQuerySuggestion:
    command: str
    values: tuple[str, ...]
    purpose: str
    generation_scope: str = "current"

    def __post_init__(self) -> None:
        scope = str(self.generation_scope or "current").strip().lower()
        if scope not in {"current", "previous"}:
            raise ValueError(f"非法 Graphify query generation scope: {scope!r}")
        if scope == "previous" and self.command != "affected":
            raise ValueError("Graphify previous generation 仅支持 affected 查询")
        object.__setattr__(self, "generation_scope", scope)

    @property
    def shell_command(self) -> str:
        rendered = " ".join(shlex.quote(str(value)) for value in self.values)
        suffix = " --previous" if self.generation_scope == "previous" else ""
        return f'"$TMUX_GRAPHIFY_CMD" {self.command}' + (f" {rendered}" if rendered else "") + suffix


@dataclasses.dataclass(frozen=True)
class GraphifyFreshnessAssessment:
    state: str
    graph_fingerprint: str
    changed_paths: tuple[str, ...]
    checked_at: str
    reason: str = ""


@dataclasses.dataclass(frozen=True)
class GraphifyQueryResult:
    ok: bool
    query_id: str
    command: str
    graph_fingerprint: str
    freshness: str
    warnings: tuple[str, ...]
    truncated: bool
    duration_ms: int
    result_text: str
    error_kind: str = ""
    generation_scope: str = "current"

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "schema": GRAPHIFY_QUERY_RESULT_SCHEMA,
            "ok": self.ok,
            "query_id": self.query_id,
            "command": self.command,
            "graph": {
                "version": GRAPHIFY_VERSION,
                "fingerprint": self.graph_fingerprint,
                "freshness": self.freshness,
                "generation_scope": self.generation_scope,
            },
            "warnings": list(self.warnings),
            "truncated": self.truncated,
            "duration_ms": self.duration_ms,
            "result_text": self.result_text,
            "error_kind": self.error_kind,
        }

    def to_text(self) -> str:
        lines = [
            "[TMUX_GRAPHIFY_QUERY]",
            f"schema: {GRAPHIFY_QUERY_RESULT_SCHEMA}",
            f"query_id: {self.query_id}",
            f"command: {self.command}",
            f"graph_fingerprint: {self.graph_fingerprint}",
            f"freshness: {self.freshness}",
            f"generation_scope: {self.generation_scope}",
            f"ok: {str(self.ok).lower()}",
            f"truncated: {str(self.truncated).lower()}",
            f"duration_ms: {self.duration_ms}",
        ]
        lines.extend(f"warning: {warning}" for warning in self.warnings)
        if self.error_kind:
            lines.append(f"error_kind: {self.error_kind}")
        lines.append("[RESULT]")
        if self.result_text:
            lines.append(self.result_text)
        lines.append("[END_TMUX_GRAPHIFY_QUERY]")
        return "\n".join(lines)


@dataclasses.dataclass(frozen=True)
class _GraphifyQueryScope:
    active: bool = False
    accepted: bool = True
    runner_id: str = ""
    stage_key: str = ""
    scope_key: str = ""


@dataclasses.dataclass(frozen=True)
class GraphifyEvidence:
    evidence_id: str
    graph_fingerprint: str
    freshness: str
    extracted_nodes: tuple[str, ...] = ()
    extracted_edges: tuple[str, ...] = ()
    ambiguous_candidates: tuple[str, ...] = ()
    inferred_candidates: tuple[str, ...] = ()
    old_generation_candidates: tuple[str, ...] = ()
    old_generation_fingerprint: str = ""
    related_paths: tuple[str, ...] = ()
    routed_module_candidates: tuple[str, ...] = ()
    report_path: str = ""
    block_text: str = ""
    compact_block_text: str = ""
    suggestions: tuple[GraphifyQuerySuggestion, ...] = ()


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
    query_count_stage: int = 0
    last_query_command: str = ""
    last_query_at: str = ""
    last_query_status: str = ""
    last_query_freshness: str = ""
    last_query_truncated: bool = False

    def to_public_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True)
class GraphifyTurnProfile:
    mode: str = GraphifyMode.OFF.value
    evidence: GraphifyEvidence | None = None
    status: GraphifyStatus | None = None
    routed_paths: tuple[str, ...] = ()
    changed_files: tuple[str, ...] = ()
    deleted_files: tuple[str, ...] = ()
    symbols: tuple[str, ...] = ()
    query_seeds: tuple[str, ...] = ()
    prompt_file_references: tuple[str, ...] = ()
    turn_context: GraphifyTurnContext | None = None
    suggestions: tuple[GraphifyQuerySuggestion, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "mode",
            normalize_graphify_mode(self.mode, default=GraphifyMode.OFF).value,
        )
        for field_name in (
            "routed_paths",
            "changed_files",
            "deleted_files",
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


def _runner_query_scope_path(project_dir: str | Path) -> Path:
    return project_cache_dir(project_dir) / "runner-query-scope.json"


def _normalize_query_scope_component(value: object, *, max_length: int = 128) -> str:
    return re.sub(r"[^A-Za-z0-9_.:-]", "", str(value or "").strip())[:max_length]


def _normalize_graphify_stage_key(value: object) -> str:
    normalized = str(value or "").strip()
    match = re.search(r"(?i)(?:^|[._-])(a(?:0[0-9]|1[0-9]))(?:$|[._-])", normalized)
    if match:
        return match.group(1).upper()
    if re.fullmatch(r"(?i)a(?:0[0-9]|1[0-9])", normalized):
        return normalized.upper()
    return _normalize_query_scope_component(normalized)


def _runner_query_scope_key(
    runner_id: str,
    mode: GraphifyMode,
    stage_key: str = "",
) -> str:
    material = (
        f"{_normalize_query_scope_component(runner_id)}\0"
        f"{_normalize_graphify_stage_key(stage_key)}\0{mode.value}"
    ).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def _read_runner_query_scope(project_dir: str | Path) -> dict[str, Any]:
    scope_path = _runner_query_scope_path(project_dir)
    if not scope_path.is_file():
        return {}
    try:
        payload = json.loads(scope_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


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
    *,
    runner_id: str = "",
    stage_key: str = "",
    session_generation: str = "",
) -> bool:
    normalized = normalize_graphify_mode(mode, default=GraphifyMode.AUTO)
    if normalized == GraphifyMode.OFF:
        return publish_graphify_off_status(project_dir)
    project = _project_root(project_dir)
    cache_dir = project_cache_dir(project)
    normalized_runner_id = _normalize_query_scope_component(runner_id)
    normalized_stage_key = _normalize_graphify_stage_key(stage_key)
    normalized_session_generation = _normalize_query_scope_component(
        session_generation,
        max_length=64,
    )
    try:
        with _metadata_lock(cache_dir):
            previous = read_graphify_project_status(project) or {}
            scope_path = _runner_query_scope_path(project)
            scope_payload = _read_runner_query_scope(project)

            if normalized_runner_id and normalized_stage_key:
                scope_key = _runner_query_scope_key(
                    normalized_runner_id,
                    normalized,
                    normalized_stage_key,
                )
                if (
                    str(scope_payload.get("scope_key", "") or "") == scope_key
                    and previous
                ):
                    # Multiple workers in one runner share one project graph.
                    # Their constructors must not regress Ready to Building or
                    # erase the runner's already-recorded query aggregate.
                    generations = {
                        _normalize_query_scope_component(value, max_length=64)
                        for value in scope_payload.get("session_generations", ())
                        if _normalize_query_scope_component(value, max_length=64)
                    }
                    if (
                        normalized_session_generation
                        and normalized_session_generation not in generations
                    ):
                        generations.add(normalized_session_generation)
                        _atomic_write_json(
                            scope_path,
                            {
                                **scope_payload,
                                "schema": "tmux-graphify-query-scope/2",
                                "runner_id": normalized_runner_id,
                                "stage_key": normalized_stage_key,
                                "mode": normalized.value,
                                "session_generations": sorted(generations),
                                "updated_at": _now_iso(),
                            },
                        )
                    return True
                aggregate: dict[str, Any] = {}
            else:
                # Legacy callers have no generation identity. Preserve query
                # observations because clearing them cannot be scoped safely.
                scope_key = ""
                aggregate = _query_aggregate_fields(project, reset=False)

            _write_status(
                project,
                GraphifyStatus(
                    mode=normalized.value,
                    state=GraphifyState.BUILDING.value,
                    version=str(previous.get("version", "") or ""),
                    freshness="pending_refresh",
                    **aggregate,
                ),
            )
            if normalized_runner_id and normalized_stage_key:
                _atomic_write_json(
                    scope_path,
                    {
                        "schema": "tmux-graphify-query-scope/2",
                        "scope_key": scope_key,
                        "runner_id": normalized_runner_id,
                        "stage_key": normalized_stage_key,
                        "mode": normalized.value,
                        "session_generations": (
                            [normalized_session_generation]
                            if normalized_session_generation
                            else []
                        ),
                        "updated_at": _now_iso(),
                    },
                )
    except (GraphifyError, OSError):
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


def _query_aggregate_fields(project_dir: str | Path, *, reset: bool) -> dict[str, Any]:
    if reset:
        return {}
    previous = read_graphify_project_status(project_dir) or {}
    names = {
        "query_count_stage",
        "last_query_command",
        "last_query_at",
        "last_query_status",
        "last_query_freshness",
        "last_query_truncated",
    }
    return {name: previous[name] for name in names if name in previous}


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


def _run_bounded_graphify_query(
    args: Sequence[str],
    *,
    cwd: Path,
    timeout_sec: float,
    max_chars: int = GRAPHIFY_QUERY_MAX_CHARS,
) -> tuple[subprocess.CompletedProcess[str], bool]:
    try:
        process = subprocess.Popen(
            [str(value) for value in args],
            cwd=str(cwd),
            env=_sanitized_graphify_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
            start_new_session=(os.name != "nt"),
        )
    except OSError as exc:
        raise GraphifyBuildFailed(f"Graphify process launch failed: {exc}") from exc
    _register_process(process)
    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []
    lengths = {"stdout": 0, "stderr": 0}
    truncated = {"stdout": False, "stderr": False}

    def drain(stream: Any, chunks: list[str], key: str) -> None:
        try:
            while True:
                chunk = stream.read(4096)
                if not chunk:
                    break
                remaining = max_chars - lengths[key]
                if remaining > 0:
                    kept = chunk[:remaining]
                    chunks.append(kept)
                    lengths[key] += len(kept)
                if len(chunk) > max(remaining, 0):
                    truncated[key] = True
        finally:
            with contextlib.suppress(Exception):
                stream.close()

    stdout_thread = threading.Thread(target=drain, args=(process.stdout, stdout_chunks, "stdout"), daemon=True)
    stderr_thread = threading.Thread(target=drain, args=(process.stderr, stderr_chunks, "stderr"), daemon=True)
    stdout_thread.start()
    stderr_thread.start()
    try:
        try:
            process.wait(timeout=max(float(timeout_sec), 0.1))
        except subprocess.TimeoutExpired as exc:
            _terminate_process_group(process)
            raise GraphifyBuildFailed(f"Graphify query timed out after {timeout_sec:g}s") from exc
        except BaseException:
            _terminate_process_group(process)
            raise
    finally:
        stdout_thread.join(timeout=3)
        stderr_thread.join(timeout=3)
        _unregister_process(process)
    return (
        subprocess.CompletedProcess(
            args=list(args),
            returncode=int(process.returncode or 0),
            stdout="".join(stdout_chunks),
            stderr="".join(stderr_chunks),
        ),
        bool(truncated["stdout"] or truncated["stderr"]),
    )


def _project_lock(project_key: str) -> threading.RLock:
    with _PROJECT_LOCKS_GUARD:
        return _PROJECT_LOCKS.setdefault(project_key, threading.RLock())


@contextlib.contextmanager
def _graph_lock(
    cache_dir: Path,
    *,
    exclusive: bool,
    timeout_sec: float = 180.0,
) -> Iterator[None]:
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
                        operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
                        fcntl.flock(handle.fileno(), operation | fcntl.LOCK_NB)
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


@contextlib.contextmanager
def _build_lock(cache_dir: Path, *, timeout_sec: float = 180.0) -> Iterator[None]:
    with _graph_lock(cache_dir, exclusive=True, timeout_sec=timeout_sec):
        yield


@contextlib.contextmanager
def _query_lock(cache_dir: Path, *, timeout_sec: float = 35.0) -> Iterator[None]:
    with _graph_lock(cache_dir, exclusive=False, timeout_sec=timeout_sec):
        yield


@contextlib.contextmanager
def _metadata_lock(cache_dir: Path) -> Iterator[None]:
    lock = _project_lock(cache_dir.name + ":metadata")
    with lock:
        cache_dir.mkdir(parents=True, exist_ok=True)
        handle = (cache_dir / "metadata.lock").open("a+", encoding="utf-8")
        try:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
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


def _source_stat_hint(path: Path) -> dict[str, int]:
    details = os.stat(path, follow_symlinks=False)
    return {
        "size": int(details.st_size),
        "mtime_ns": int(details.st_mtime_ns),
        "ctime_ns": int(details.st_ctime_ns),
        "dev": int(details.st_dev),
        "ino": int(details.st_ino),
    }


def _capture_graphify_source_records(
    project: Path,
    config: GraphifyBuildConfig,
    *,
    destination: Path | None = None,
) -> tuple[list[dict[str, Any]], int]:
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
        blob_id = clean_blob_ids.get(relative.as_posix(), "")
        if destination is None and blob_id:
            details = _source_stat_hint(source)
            size = int(details["size"])
            if size > config.max_file_bytes:
                raise GraphifySnapshotError(
                    f"Graphify 源文件超过 {config.max_file_bytes} bytes 上限: "
                    f"{relative.as_posix()} ({size})"
                )
            total_bytes += size
            if total_bytes > config.max_total_bytes:
                raise GraphifySnapshotError(
                    f"Graphify 输入超过总量上限 {config.max_total_bytes} bytes；不会静默截断"
                )
            if len(records) + 1 > config.max_files:
                raise GraphifySnapshotError(
                    f"Graphify 输入超过文件数上限 {config.max_files}；不会静默截断"
                )
            records.append({
                "path": relative.as_posix(),
                "size": size,
                "content_id": f"git:{blob_id}",
                "stat": details,
            })
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
        record = {
            "path": relative.as_posix(),
            "size": size,
            "content_id": _git_blob_content_id(blob_id, content),
            "stat": _source_stat_hint(source),
        }
        records.append(record)
        if destination is not None:
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
    return records, total_bytes


def capture_graphify_source_manifest(
    project_dir: str | Path,
    *,
    config: GraphifyBuildConfig | None = None,
) -> dict[str, str]:
    """Capture the controlled source content IDs without building a graph."""

    project = _project_root(project_dir)
    records, _ = _capture_graphify_source_records(project, config or GraphifyBuildConfig())
    return {
        str(record["path"]): str(record["content_id"])
        for record in records
    }


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
    records, total_bytes = _capture_graphify_source_records(
        project,
        config,
        destination=destination_path,
    )
    fingerprint_records = [
        {key: record[key] for key in ("path", "size", "content_id")}
        for record in records
    ]
    fingerprint_payload = {
        "graphify_version": GRAPHIFY_VERSION,
        "adapter_version": GRAPHIFY_ADAPTER_VERSION,
        "schema": GRAPHIFY_GRAPH_SCHEMA,
        "include": list(config.include),
        "exclude": list(config.exclude),
        "files": fingerprint_records,
    }
    fingerprint = hashlib.sha256(
        json.dumps(fingerprint_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    manifest_path = destination_path.parent / "source-manifest.json"
    _atomic_write_json(
        manifest_path,
        {
            **fingerprint_payload,
            "files": records,
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


def _manifest_config(manifest: Mapping[str, Any]) -> GraphifyBuildConfig:
    include = manifest.get("include", [])
    exclude = manifest.get("exclude", [])
    return GraphifyBuildConfig(
        include=tuple(str(value) for value in include) if isinstance(include, list) else (),
        exclude=tuple(str(value) for value in exclude) if isinstance(exclude, list) else (),
    )


def _eligible_source_paths(project: Path, config: GraphifyBuildConfig) -> tuple[list[str], bool]:
    candidates = _git_source_candidates(project)
    is_git = candidates is not None
    if candidates is None:
        candidates = _filesystem_source_candidates(project)
    eligible: list[str] = []
    for candidate in sorted(set(candidates)):
        relative = Path(candidate)
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or _is_sensitive_path(relative)
            or not _is_source_path(relative)
            or not _matches_scan_policy(relative, config)
        ):
            continue
        source = project / relative
        try:
            resolved = source.resolve(strict=True)
            resolved.relative_to(project)
        except (OSError, ValueError):
            continue
        if _has_symlink_component(project, source) or not resolved.is_file():
            continue
        eligible.append(relative.as_posix())
    return eligible, is_git


def _content_matches_manifest(
    path: Path,
    expected_content_id: str,
    *,
    clean_blob_id: str = "",
) -> bool:
    expected = str(expected_content_id or "").strip().lower()
    clean_blob = str(clean_blob_id or "").strip().lower()
    if clean_blob and expected == f"git:{clean_blob}":
        return True
    content = path.read_bytes()
    if expected.startswith("sha256:"):
        return hashlib.sha256(content).hexdigest() == expected.removeprefix("sha256:")
    if expected.startswith("git:"):
        return _git_blob_content_id(expected.removeprefix("git:"), content) == expected
    return False


def assess_graphify_freshness(
    project_dir: str | Path,
    generation: _GraphGeneration | None = None,
) -> GraphifyFreshnessAssessment:
    project = _project_root(project_dir)
    checked_at = _now_iso()
    try:
        current_generation = generation or _current_graph_or_raise(project)
        manifest = _read_json_object(current_generation.graph_path.parent / "source-manifest.json")
        records = manifest.get("files", [])
        if not isinstance(records, list):
            raise GraphifySnapshotError("source manifest files must be a list")
        record_by_path = {
            str(record.get("path", "") or ""): record
            for record in records
            if isinstance(record, Mapping) and str(record.get("path", "") or "")
        }
        config = _manifest_config(manifest)
        current_paths, is_git = _eligible_source_paths(project, config)
        changed = set(record_by_path).symmetric_difference(current_paths)
        clean_blob_ids = _git_clean_blob_ids(project) if is_git else {}
        for relative in sorted(set(record_by_path).intersection(current_paths)):
            if relative in changed:
                continue
            record = record_by_path[relative]
            source = project / relative
            try:
                if os.stat(source, follow_symlinks=False).st_size > config.max_file_bytes:
                    changed.add(relative)
                    continue
            except OSError:
                changed.add(relative)
                continue
            if is_git:
                if not _content_matches_manifest(
                    source,
                    str(record.get("content_id", "") or ""),
                    clean_blob_id=clean_blob_ids.get(relative, ""),
                ):
                    changed.add(relative)
                continue
            stat_hint = record.get("stat")
            if isinstance(stat_hint, Mapping):
                try:
                    current_hint = _source_stat_hint(source)
                    keys = ("size", "mtime_ns", "ctime_ns", "dev", "ino")
                    if all(int(stat_hint.get(key, -1)) == current_hint[key] for key in keys):
                        continue
                except (OSError, TypeError, ValueError):
                    pass
            if not _content_matches_manifest(source, str(record.get("content_id", "") or "")):
                changed.add(relative)
        state = "stale" if changed else "fresh"
        reason = f"{len(changed)} controlled source path(s) changed" if changed else "controlled source matches graph manifest"
        return GraphifyFreshnessAssessment(
            state=state,
            graph_fingerprint=current_generation.fingerprint,
            changed_paths=tuple(sorted(changed)[:10]),
            checked_at=checked_at,
            reason=reason,
        )
    except Exception as exc:
        fingerprint = generation.fingerprint if generation is not None else ""
        return GraphifyFreshnessAssessment(
            state="unknown",
            graph_fingerprint=fingerprint,
            changed_paths=(),
            checked_at=checked_at,
            reason="freshness check failed: " + " ".join(str(exc).split())[:300],
        )


def _publish_freshness_assessment(
    project: Path,
    assessment: GraphifyFreshnessAssessment,
) -> None:
    cache_dir = project_cache_dir(project)
    with _metadata_lock(cache_dir):
        current_path = cache_dir / "current.json"
        current = _read_json_object(current_path)
        if str(current.get("fingerprint", "") or "") != assessment.graph_fingerprint:
            return
        current["freshness"] = assessment.state
        current["freshness_checked_at"] = assessment.checked_at
        current["freshness_reason"] = assessment.reason
        _atomic_write_json(current_path, current)
        public = read_graphify_project_status(project)
        if not public or str(public.get("mode", "") or "") == GraphifyMode.OFF.value:
            return
        allowed = {field.name for field in dataclasses.fields(GraphifyStatus)}
        payload = {key: value for key, value in public.items() if key in allowed}
        payload["freshness"] = assessment.state
        if assessment.state == "fresh":
            payload["state"] = GraphifyState.READY.value
        elif assessment.state == "unknown":
            payload["state"] = GraphifyState.DEGRADED.value
        else:
            payload["state"] = GraphifyState.STALE.value
        _write_status(project, GraphifyStatus(**payload))


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


def _validation_file_record(path: Path, *, include_hash: bool = True) -> dict[str, Any]:
    details = path.stat()
    record: dict[str, Any] = {
        "size": int(details.st_size),
        "mtime_ns": int(details.st_mtime_ns),
    }
    if include_hash:
        record["sha256"] = _hash_file(path)
    return record


def _validation_stamp_is_current(
    generation_dir: Path,
    *,
    metadata: Mapping[str, Any],
    manifest: Mapping[str, Any],
) -> bool:
    validation = _read_json_object(generation_dir / "validation.json")
    if (
        str(validation.get("schema", "") or "") != GRAPHIFY_GRAPH_SCHEMA
        or str(validation.get("graphify_version", "") or "") != GRAPHIFY_VERSION
        or str(validation.get("adapter_version", "") or "") != GRAPHIFY_ADAPTER_VERSION
        or str(validation.get("source_fingerprint", "") or "")
        != str(metadata.get("source_fingerprint", "") or "")
    ):
        return False
    files = validation.get("files", {})
    if not isinstance(files, Mapping):
        return False
    for name in ("graph.json", "source-manifest.json", "metadata.json"):
        expected = files.get(name)
        if not isinstance(expected, Mapping):
            return False
        try:
            details = (generation_dir / name).stat()
        except OSError:
            return False
        if (
            int(expected.get("size", -1) or -1) != int(details.st_size)
            or int(expected.get("mtime_ns", -1) or -1) != int(details.st_mtime_ns)
        ):
            return False
    if str(files["graph.json"].get("sha256", "") or "") != str(metadata.get("graph_sha256", "") or ""):
        return False
    if str(files["source-manifest.json"].get("sha256", "") or "") != str(metadata.get("manifest_sha256", "") or ""):
        return False
    if str(manifest.get("source_fingerprint", "") or "") != str(metadata.get("source_fingerprint", "") or ""):
        return False
    return True


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
    if _validation_stamp_is_current(
        generation_dir,
        metadata=metadata,
        manifest=manifest,
    ):
        try:
            node_count = int(metadata.get("node_count", 0) or 0)
            edge_count = int(metadata.get("edge_count", 0) or 0)
        except (TypeError, ValueError):
            return None
        if node_count < 0 or edge_count < 0:
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


def _previous_generation(cache_dir: Path) -> _GraphGeneration | None:
    """Return only the immutable generation immediately before current.

    Unlike ``_current_generation`` this helper never treats the previous graph
    as a transparent cache fallback.  Callers use it only for explicitly
    labelled, navigation-only evidence about files deleted by the latest
    checkpoint.
    """

    current = _read_json_object(cache_dir / "current.json")
    if (
        str(current.get("version", "") or "") != GRAPHIFY_VERSION
        or str(current.get("schema", "") or "") != GRAPHIFY_GRAPH_SCHEMA
    ):
        return None
    fingerprint = str(current.get("fingerprint", "") or "").strip()
    previous_fingerprint = str(current.get("previous_fingerprint", "") or "").strip()
    if not previous_fingerprint or previous_fingerprint == fingerprint:
        return None
    return _validate_cached_generation(
        cache_dir,
        previous_fingerprint,
        freshness="old_generation",
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
        metadata_payload = _read_json_object(temporary_dir / "metadata.json")
        _atomic_write_json(
            temporary_dir / "validation.json",
            {
                "schema": GRAPHIFY_GRAPH_SCHEMA,
                "graphify_version": GRAPHIFY_VERSION,
                "adapter_version": GRAPHIFY_ADAPTER_VERSION,
                "source_fingerprint": snapshot.source_fingerprint,
                "validated_at": _now_iso(),
                "files": {
                    name: _validation_file_record(temporary_dir / name)
                    for name in ("graph.json", "source-manifest.json", "metadata.json")
                },
                "node_count": int(metadata_payload.get("node_count", 0) or 0),
                "edge_count": int(metadata_payload.get("edge_count", 0) or 0),
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
                current_payload = _read_json_object(cache_dir / "current.json")
                if str(current_payload.get("fingerprint", "") or "") == previous.fingerprint:
                    current_payload["freshness"] = "fresh"
                    current_payload["freshness_checked_at"] = _now_iso()
                    current_payload["freshness_reason"] = "checkpoint source matches current graph"
                    _atomic_write_json(cache_dir / "current.json", current_payload)
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


def _node_line_number(node: Mapping[str, Any]) -> int | None:
    containers = [node]
    metadata = node.get("metadata")
    if isinstance(metadata, Mapping):
        containers.append(metadata)
    location = node.get("source_location")
    if isinstance(location, Mapping):
        containers.append(location)
    for container in containers:
        for key in ("start_line", "line_start", "line", "lineno", "line_number"):
            value = container.get(key)
            try:
                number = int(value)
            except (TypeError, ValueError):
                continue
            if number > 0:
                return number
    return None


def _edge_relation(edge: Mapping[str, Any]) -> str:
    for key in ("relation", "type", "kind", "label"):
        value = edge.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return "related_to"


def _edge_evidence_kind(edge: Mapping[str, Any]) -> str:
    values = " ".join(
        str(edge.get(key, "") or "")
        for key in ("confidence", "provenance", "evidence", "status", "kind")
    ).lower()
    relation = _edge_relation(edge).lower()
    if "ambig" in values or "ambig" in relation:
        return "AMBIGUOUS"
    if "infer" in values or "infer" in relation:
        return "INFERRED"
    return "EXTRACTED"


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


def _old_generation_deleted_candidates(
    *,
    project: Path,
    generation: _GraphGeneration | None,
    deleted_files: Sequence[str],
) -> tuple[str, ...]:
    """Extract bounded old incoming-edge candidates for deleted source files.

    Every returned item is deliberately labelled OLD_GENERATION/AMBIGUOUS.
    The previous graph can only guide inspection; it cannot establish a fact
    about the current source tree.
    """

    if generation is None or not deleted_files:
        return ()
    payload, _, _ = _load_and_validate_graph(generation.graph_path)
    raw_nodes, raw_edges = _graph_lists(payload)
    nodes = [item for item in raw_nodes if isinstance(item, Mapping)]
    deleted = {
        normalized
        for value in deleted_files
        if (normalized := _normalize_project_relative_path(project, value))
    }
    if not deleted:
        return ()

    reference_to_node: dict[str, tuple[Mapping[str, Any], int]] = {}
    deleted_references: set[str] = set()
    deleted_nodes: list[tuple[Mapping[str, Any], int]] = []
    for index, node in enumerate(nodes):
        path = _normalize_project_relative_path(
            project,
            _relative_graph_path(_node_source_path(node), graph_path=generation.graph_path),
        )
        references = _node_references(node)
        for reference in references:
            reference_to_node[reference] = (node, index)
        if path in deleted:
            deleted_nodes.append((node, index))
            deleted_references.update(references)
    if not deleted_references:
        return ()

    def render_node(reference: str) -> str:
        item = reference_to_node.get(reference)
        if item is None:
            return reference
        node, index = item
        label = _node_label(node, index)
        path = _normalize_project_relative_path(
            project,
            _relative_graph_path(_node_source_path(node), graph_path=generation.graph_path),
        )
        line = _node_line_number(node)
        location = f"{path}:L{line}" if path and line else path
        return f"{label} ({location})" if location else label

    candidates: list[str] = []
    for edge in raw_edges:
        if not isinstance(edge, Mapping):
            continue
        source = _edge_end(edge, _EDGE_SOURCE_KEYS)
        target = _edge_end(edge, _EDGE_TARGET_KEYS)
        if not source or target not in deleted_references or source in deleted_references:
            continue
        rendered = (
            "[OLD_GENERATION][AMBIGUOUS] possible old caller: "
            f"{render_node(source)} --{_edge_relation(edge)}--> {render_node(target)}"
        )
        if rendered not in candidates:
            candidates.append(rendered)
        if len(candidates) >= 3:
            break
    if candidates:
        return tuple(candidates)

    # Some parsers expose a deleted node but no resolvable incoming edge.  Keep
    # that bounded navigation clue explicit instead of fabricating a caller.
    for node, index in deleted_nodes[:3]:
        references = _node_references(node)
        reference = references[0] if references else _node_identity(node, index)
        candidates.append(
            "[OLD_GENERATION][AMBIGUOUS] deleted node without resolved old caller: "
            + render_node(reference)
        )
    return tuple(candidates)


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


def _safe_query_value(value: str, *, max_chars: int = 160) -> str:
    text = " ".join(str(value or "").replace("`", "").split())
    return text[:max_chars].strip()


def _query_suggestions(
    *,
    context: GraphifyTurnContext | None,
    routed_paths: Sequence[str],
    changed_files: Sequence[str],
    deleted_files: Sequence[str],
    previous_deleted_files: Sequence[str],
    symbols: Sequence[str],
    query_seeds: Sequence[str],
    prompt_file_references: Sequence[str],
    path_degrees: Mapping[str, int] | None = None,
) -> tuple[GraphifyQuerySuggestion, ...]:
    intent = context.intent if context is not None else GraphifyQueryIntent.CODE_FACT_DISCOVERY
    degrees = path_degrees or {}

    def ordered_paths(values: Sequence[str]) -> tuple[str, ...]:
        unique = {
            safe
            for value in values
            if (safe := _safe_query_value(value))
        }
        return tuple(sorted(unique, key=lambda path: (-int(degrees.get(path, 0) or 0), path)))

    deleted_ordered = ordered_paths(deleted_files)
    previous_deleted_ordered = ordered_paths(previous_deleted_files)
    changed_ordered = tuple(
        path for path in ordered_paths(changed_files) if path not in deleted_ordered
    )
    routed_ordered = tuple(path for path in ordered_paths(routed_paths) if path not in changed_ordered)
    prompt_ordered = tuple(
        path
        for path in ordered_paths(prompt_file_references)
        if path not in changed_ordered and path not in routed_ordered
    )
    paths = (*changed_ordered, *routed_ordered, *prompt_ordered)
    symbol_values = tuple(
        dict.fromkeys(_safe_query_value(value) for value in symbols if _safe_query_value(value))
    )
    broad_seeds: list[str] = []
    if context is not None:
        broad_seeds.extend((context.requirement_name, context.task_name))
    broad_seeds.extend(query_seeds)
    broad = " ".join(
        dict.fromkeys(_safe_query_value(value, max_chars=80) for value in broad_seeds if _safe_query_value(value, max_chars=80))
    )[:240].strip()
    candidates: list[GraphifyQuerySuggestion] = []

    def add(
        command: str,
        values: Sequence[str],
        purpose: str,
        *,
        generation_scope: str = "current",
    ) -> None:
        normalized = tuple(_safe_query_value(value) for value in values if _safe_query_value(value))
        if command != "god-nodes" and not normalized:
            return
        suggestion = GraphifyQuerySuggestion(
            command=command,
            values=normalized,
            purpose=purpose,
            generation_scope=generation_scope,
        )
        if suggestion not in candidates:
            candidates.append(suggestion)

    impact_intents = {
        GraphifyQueryIntent.REQUIREMENT_IMPACT,
        GraphifyQueryIntent.TASK_DEPENDENCY,
        GraphifyQueryIntent.CHANGE_REVIEW,
        GraphifyQueryIntent.WHOLE_CHANGE_REVIEW,
    }
    if intent in impact_intents and previous_deleted_ordered:
        add(
            "affected",
            (previous_deleted_ordered[0],),
            "从上一代不可变图检查已删除路径的旧调用者候选；结果不是当前代码事实",
            generation_scope="previous",
        )
    if intent in impact_intents and paths:
        add("affected", (paths[0],), "检查当前变更或目标路径的静态影响候选")
    if intent == GraphifyQueryIntent.IMPLEMENTATION and symbol_values:
        add("explain", (symbol_values[0],), "定位当前实现符号及其直接关系")
    if intent == GraphifyQueryIntent.CODE_FACT_DISCOVERY and symbol_values:
        add("explain", (symbol_values[0],), "在询问人类前核对可从代码确认的事实")
    path_seeds = (*symbol_values, *paths)
    if intent in {
        GraphifyQueryIntent.CODE_FACT_DISCOVERY,
        GraphifyQueryIntent.REQUIREMENT_IMPACT,
        GraphifyQueryIntent.ARCHITECTURE_BOUNDARY,
        GraphifyQueryIntent.TASK_DEPENDENCY,
        GraphifyQueryIntent.IMPLEMENTATION,
        GraphifyQueryIntent.CHANGE_REVIEW,
        GraphifyQueryIntent.WHOLE_CHANGE_REVIEW,
    } and len(path_seeds) >= 2:
        add("path", path_seeds[:2], "检查两个明确节点之间的静态关系路径")
    if broad:
        add("query", (broad,), "补充当前需求或任务的相关代码候选")
    if intent in {GraphifyQueryIntent.ROUTING_DISCOVERY, GraphifyQueryIntent.ARCHITECTURE_BOUNDARY}:
        add("god-nodes", (), "发现高连接节点，仅作为扩大代码阅读范围的候选")
    if not candidates and paths:
        add("affected", (paths[0],), "检查目标路径的静态影响候选")
    if not candidates and symbol_values:
        add("explain", (symbol_values[0],), "查看目标符号的静态关系")
    return tuple(candidates[:3])


def _render_bounded_evidence(
    sections: Sequence[tuple[str, Sequence[str]]],
    *,
    required_tail: Sequence[str],
) -> str:
    end = "[End Graphify Code Graph Evidence]"
    truncation = "- [truncated within evidence budget]"
    output: list[str] = []
    truncated = False
    for heading, lines in sections:
        section_lines = ([heading] if heading else []) + list(lines)
        for line in section_lines:
            candidate = "\n".join((*output, line, *required_tail, end))
            if len(candidate) > GRAPHIFY_EVIDENCE_MAX_CHARS:
                truncated = True
                break
            output.append(line)
        if truncated:
            break
    if truncated:
        while output and len("\n".join((*output, truncation, *required_tail, end))) > GRAPHIFY_EVIDENCE_MAX_CHARS:
            output.pop()
        output.append(truncation)
    output.extend(required_tail)
    output.append(end)
    return "\n".join(output)


def _build_evidence(
    *,
    project: Path,
    generation: _GraphGeneration,
    previous_generation: _GraphGeneration | None,
    prompt: str,
    runtime_dir: Path | None,
    routed_paths: Sequence[str] = (),
    changed_files: Sequence[str] = (),
    deleted_files: Sequence[str] = (),
    symbols: Sequence[str] = (),
    query_seeds: Sequence[str] = (),
    prompt_file_references: Sequence[str] = (),
    turn_context: GraphifyTurnContext | None = None,
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
        for reference in _node_references(node):
            identity_to_label[reference] = label
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
    selected_ids = {
        reference
        for node in selected_nodes
        for reference in _node_references(node)
    }
    extracted_nodes: list[str] = []
    related_paths: list[str] = []
    for node in selected_nodes:
        index = nodes.index(node)
        label = _node_label(node, index)
        path = _normalize_project_relative_path(
            project,
            _relative_graph_path(_node_source_path(node), graph_path=generation.graph_path),
        )
        line = _node_line_number(node)
        rendered_path = f"{path}:L{line}" if path and line else path
        rendered = f"{label} ({rendered_path})" if rendered_path else label
        if rendered not in extracted_nodes:
            extracted_nodes.append(rendered)
        if path and path not in related_paths:
            related_paths.append(path)
    extracted_edges: list[str] = []
    ambiguous: list[str] = []
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
        evidence_kind = _edge_evidence_kind(edge)
        if evidence_kind == "INFERRED":
            if rendered not in inferred and len(inferred) < 3:
                inferred.append(rendered)
        elif evidence_kind == "AMBIGUOUS":
            if rendered not in ambiguous and len(ambiguous) < 3:
                ambiguous.append(rendered)
        elif rendered not in extracted_edges and len(extracted_edges) < 20:
            extracted_edges.append(rendered)
        if len(extracted_edges) >= 20 and len(ambiguous) >= 3 and len(inferred) >= 3:
            break
    reference_degrees: dict[str, int] = {}
    for edge in edges:
        source = _edge_end(edge, _EDGE_SOURCE_KEYS)
        target = _edge_end(edge, _EDGE_TARGET_KEYS)
        if source:
            reference_degrees[source] = reference_degrees.get(source, 0) + 1
        if target:
            reference_degrees[target] = reference_degrees.get(target, 0) + 1
    path_degrees: dict[str, int] = {}
    for index, node in enumerate(nodes):
        path = _normalize_project_relative_path(
            project,
            _relative_graph_path(_node_source_path(node), graph_path=generation.graph_path),
        )
        if not path:
            continue
        explicit_degree = int(node.get("degree", 0) or 0) if str(node.get("degree", 0) or "0").isdigit() else 0
        observed_degree = max((reference_degrees.get(reference, 0) for reference in _node_references(node)), default=0)
        path_degrees[path] = max(path_degrees.get(path, 0), explicit_degree, observed_degree)
    current_manifest_paths = _manifest_path_set(
        generation.graph_path.parent / "source-manifest.json"
    )
    previous_manifest_paths = (
        _manifest_path_set(previous_generation.graph_path.parent / "source-manifest.json")
        if previous_generation is not None
        else set()
    )
    previous_deleted_files = tuple(
        path
        for path in deleted_files
        if path in previous_manifest_paths and path not in current_manifest_paths
    )
    suggestions = _query_suggestions(
        context=turn_context,
        routed_paths=routed_paths,
        changed_files=changed_files,
        deleted_files=deleted_files,
        previous_deleted_files=previous_deleted_files,
        symbols=symbols,
        query_seeds=query_seeds,
        prompt_file_references=prompt_file_references,
        path_degrees=path_degrees,
    )
    old_generation_candidates = _old_generation_deleted_candidates(
        project=project,
        generation=previous_generation,
        deleted_files=previous_deleted_files,
    )
    context_payload = {}
    if turn_context is not None:
        context_payload = {
            "stage_key": turn_context.stage_key,
            "phase": turn_context.phase,
            "role": turn_context.role,
            "intent": turn_context.intent.value,
            "requirement_name": turn_context.requirement_name,
            "task_name": turn_context.task_name,
        }
    evidence_seed_payload = json.dumps(
        {
            "prompt": prompt,
            "routed_paths": list(routed_paths),
            "changed_files": list(changed_files),
            "deleted_files": list(deleted_files),
            "symbols": list(symbols),
            "query_seeds": list(query_seeds),
            "prompt_file_references": list(prompt_file_references),
            "turn_context": context_payload,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    previous_fingerprint = previous_generation.fingerprint if previous_generation is not None else ""
    evidence_id = hashlib.sha256(
        f"{generation.fingerprint}\0{previous_fingerprint}\0{evidence_seed_payload}".encode("utf-8")
    ).hexdigest()[:20]
    intent_text = turn_context.intent.value if turn_context is not None else "code_fact_discovery"
    header = [
        f"- evidence_id: {evidence_id}",
        f"- graph_fingerprint: {generation.fingerprint[:16]}",
        f"- freshness: {generation.freshness}",
        f"- query_intent: {intent_text}",
        "- authority: navigation evidence only; code/tests/config and AI Hermes routing remains authoritative",
        "- limitation: static extraction cannot prove dynamic import, reflection, generated code, runtime config, or cross-service behavior",
    ]
    guide = [
        GRAPHIFY_FULL_GUIDE_MARKER,
        "- Query only when the injected evidence is insufficient; do not query on every turn.",
        '- `"$TMUX_GRAPHIFY_CMD" query "<question>"`',
        '- `"$TMUX_GRAPHIFY_CMD" affected "<file-or-symbol>"`',
        '- For a ledger-confirmed deleted path only: `"$TMUX_GRAPHIFY_CMD" affected "<deleted-file>" --previous`',
        '- `"$TMUX_GRAPHIFY_CMD" path "<source>" "<target>"`',
        '- `"$TMUX_GRAPHIFY_CMD" explain "<symbol>"`',
        '- `"$TMUX_GRAPHIFY_CMD" god-nodes`',
        "- Query output reports fresh/stale/unknown; stale or unknown results are old navigation evidence and require source verification.",
        "- The wrapper is read-only and rejects setup, build, update, prune, install, hooks, watch, serve, global, and write operations.",
    ]
    recommendation_lines = [
        f"- `{suggestion.shell_command}` — {suggestion.purpose}"
        for suggestion in suggestions
    ] or ["- No concrete query is recommended for this turn."]
    old_generation_sections: list[tuple[str, Sequence[str]]] = []
    if old_generation_candidates:
        old_generation_sections.append((
            "OLD_GENERATION_CANDIDATES (previous immutable graph; current code confirmation required):",
            [
                f"- old_graph_fingerprint: {previous_fingerprint[:16]}",
                *[f"- {item}" for item in old_generation_candidates],
            ],
        ))
    full_sections: list[tuple[str, Sequence[str]]] = [
        ("[Graphify Code Graph Evidence]", header),
        ("FURTHER_QUERY (optional; read-only):", guide),
        ("RECOMMENDED_QUERIES (optional; max 3):", recommendation_lines),
        *old_generation_sections,
        ("EXTRACTED:", [
            *[f"- [EXTRACTED] node: {item}" for item in extracted_nodes],
            *[f"- [EXTRACTED] edge: {item}" for item in extracted_edges],
        ]),
    ]
    compact_sections: list[tuple[str, Sequence[str]]] = [
        ("[Graphify Code Graph Evidence]", header),
        ("GRAPHIFY_REMINDER:", [
            "- Optional read-only wrapper is available; query only when current evidence is insufficient.",
            "- Verify every graph result against AGENTS.md, source, tests, and config.",
        ]),
        ("RECOMMENDED_QUERIES (optional; max 3):", recommendation_lines),
        *old_generation_sections,
        ("EXTRACTED:", [
            *[f"- [EXTRACTED] node: {item}" for item in extracted_nodes],
            *[f"- [EXTRACTED] edge: {item}" for item in extracted_edges],
        ]),
    ]
    optional_sections: list[tuple[str, Sequence[str]]] = []
    if related_paths:
        optional_sections.append(("RELATED_PATHS:", [f"- {path}" for path in related_paths[:20]]))
    if ambiguous:
        optional_sections.append((
            "AMBIGUOUS_CANDIDATES (must confirm in code):",
            [f"- [AMBIGUOUS] {item}" for item in ambiguous[:3]],
        ))
    if inferred:
        optional_sections.append((
            "INFERRED_CANDIDATES (must confirm in code):",
            [f"- [INFERRED] {item}" for item in inferred[:3]],
        ))
    verification_tail = [
        "VERIFY_BEFORE_ACTING:",
        "- Treat graph results as navigation candidates; verify them in AGENTS.md (when present), source, tests, and config before acting.",
    ]
    block_text = _render_bounded_evidence(
        [*full_sections, *optional_sections],
        required_tail=verification_tail,
    )
    compact_block_text = _render_bounded_evidence(
        [*compact_sections, *optional_sections],
        required_tail=verification_tail,
    )
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
        ambiguous_candidates=tuple(ambiguous),
        inferred_candidates=tuple(inferred),
        old_generation_candidates=old_generation_candidates,
        old_generation_fingerprint=previous_fingerprint,
        related_paths=tuple(related_paths[:20]),
        routed_module_candidates=(),
        report_path=report_path,
        block_text=block_text,
        compact_block_text=compact_block_text,
        suggestions=suggestions,
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
    deleted_files: Sequence[str | Path] | None = None,
    symbols: Sequence[str] | None = None,
    query_seeds: Sequence[str] | None = None,
    turn_context: GraphifyTurnContext | None = None,
) -> GraphifyTurnProfile:
    normalized_mode = normalize_graphify_mode(mode)
    project = _project_root(project_dir)
    # Runner generation registration owns the single query-count reset.
    # Refreshing a graph checkpoint in that same runner must retain the
    # aggregate instead of making every worker appear to start a new stage.
    query_aggregate_fields = _query_aggregate_fields(project, reset=False)
    runtime_path = Path(runtime_dir) if runtime_dir is not None else None
    context_routed_paths = turn_context.routed_paths if turn_context is not None else ()
    context_changed_files = turn_context.changed_files if turn_context is not None else ()
    context_deleted_files = turn_context.deleted_files if turn_context is not None else ()
    context_symbols = turn_context.symbols if turn_context is not None else ()
    context_query_seeds = turn_context.query_seeds if turn_context is not None else ()
    explicit_routed = (routed_paths,) if isinstance(routed_paths, (str, Path)) else tuple(routed_paths or ())
    explicit_changed = (changed_files,) if isinstance(changed_files, (str, Path)) else tuple(changed_files or ())
    explicit_deleted = (deleted_files,) if isinstance(deleted_files, (str, Path)) else tuple(deleted_files or ())
    explicit_symbols = (symbols,) if isinstance(symbols, str) else tuple(symbols or ())
    explicit_query_seeds = (query_seeds,) if isinstance(query_seeds, str) else tuple(query_seeds or ())
    normalized_routed_paths = _normalize_path_seeds(project, (*context_routed_paths, *explicit_routed))
    normalized_changed_files = _normalize_path_seeds(project, (*context_changed_files, *explicit_changed))
    normalized_deleted_files = _normalize_path_seeds(project, (*context_deleted_files, *explicit_deleted))
    normalized_symbols = _normalize_text_seeds((*context_symbols, *explicit_symbols))
    normalized_query_seeds = _normalize_text_seeds((*context_query_seeds, *explicit_query_seeds))
    prompt_references = _prompt_file_references(project, prompt)
    profile_seed_fields = {
        "routed_paths": normalized_routed_paths,
        "changed_files": normalized_changed_files,
        "deleted_files": normalized_deleted_files,
        "symbols": normalized_symbols,
        "query_seeds": normalized_query_seeds,
        "prompt_file_references": prompt_references,
        "turn_context": turn_context,
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
                **query_aggregate_fields,
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
                    **query_aggregate_fields,
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
                **query_aggregate_fields,
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
            **query_aggregate_fields,
        )
        _write_status(project, status)
        return GraphifyTurnProfile(mode=normalized_mode.value, status=status, **profile_seed_fields)
    try:
        previous_generation = (
            _previous_generation(project_cache_dir(project))
            if normalized_deleted_files
            else None
        )
        if previous_generation is not None and previous_generation.fingerprint == generation.fingerprint:
            previous_generation = None
        evidence = _build_evidence(
            project=project,
            generation=generation,
            previous_generation=previous_generation,
            prompt=prompt,
            runtime_dir=runtime_path,
            routed_paths=normalized_routed_paths,
            changed_files=normalized_changed_files,
            deleted_files=normalized_deleted_files,
            symbols=normalized_symbols,
            query_seeds=normalized_query_seeds,
            prompt_file_references=prompt_references,
            turn_context=turn_context,
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
            **query_aggregate_fields,
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
        **query_aggregate_fields,
    )
    _write_status(project, status)
    return GraphifyTurnProfile(
        mode=normalized_mode.value,
        evidence=evidence,
        status=status,
        suggestions=evidence.suggestions,
        **profile_seed_fields,
    )


def build_graphify_evidence_block(
    profile: GraphifyTurnProfile,
    business_prompt: str,
    *,
    include_full_guide: bool = True,
) -> str:
    if not isinstance(profile, GraphifyTurnProfile) or not profile.enabled or profile.evidence is None:
        return str(business_prompt or "").strip()
    evidence_text = (
        profile.evidence.block_text
        if include_full_guide
        else profile.evidence.compact_block_text or profile.evidence.block_text
    )
    return f"{evidence_text}\n\n{str(business_prompt or '').strip()}".strip()


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
        backup = data_root / f".{GRAPHIFY_VERSION}.{uuid.uuid4().hex}.bak"
        environment = _sanitized_graphify_env({"UV_PROJECT_ENVIRONMENT": str(target)})
        installed_result = GraphifyToolResolution()
        try:
            if target.exists():
                os.replace(target, backup)
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
            installed_executable = target / scripts_dir / ("graphify.exe" if os.name == "nt" else "graphify")
            installed_result = _probe_tool(installed_executable, source="managed")
            if not installed_result.compatible:
                raise GraphifyUnavailable(installed_result.error or "managed Graphify contract probe failed")
        except BaseException:
            shutil.rmtree(target, ignore_errors=True)
            if backup.exists():
                os.replace(backup, target)
            raise
        else:
            shutil.rmtree(backup, ignore_errors=True)
        _clear_tool_resolution_cache()
        return installed_result


def _current_graph_or_raise(project_dir: str | Path) -> _GraphGeneration:
    generation = _current_generation(project_cache_dir(project_dir))
    if generation is None:
        raise GraphifyUnavailable("当前项目没有可用 Graphify 图；请先运行 scripts/tmux-graphify build --project <path>")
    return generation


def _previous_graph_for_deleted_target(
    project: Path,
    target: str,
) -> tuple[_GraphGeneration, str]:
    """Resolve the previous graph only for a path deleted since that graph."""

    cache_dir = project_cache_dir(project)
    current_payload = _read_json_object(cache_dir / "current.json")
    current_fingerprint = str(current_payload.get("fingerprint", "") or "").strip()
    current = _validate_cached_generation(
        cache_dir,
        current_fingerprint,
        freshness=str(current_payload.get("freshness", "fresh") or "fresh"),
    )
    previous = _previous_generation(cache_dir)
    if current is None or previous is None:
        raise GraphifyUnavailable("当前项目没有可查询的上一代不可变 Graphify 图")
    raw_target = str(target or "").strip()
    raw_path = Path(raw_target).expanduser()
    if raw_path.is_absolute() or ".." in raw_path.parts:
        raise GraphifyUnavailable("--previous affected 只接受项目内已删除源码相对路径")
    relative = _normalize_project_relative_path(project, raw_target)
    if not relative or not _is_source_path(Path(relative)):
        raise GraphifyUnavailable("--previous affected 只接受项目内已删除源码相对路径")
    if (project / relative).exists():
        raise GraphifyUnavailable("--previous affected 拒绝仍存在于当前项目的源码路径")
    current_paths = _manifest_path_set(current.graph_path.parent / "source-manifest.json")
    previous_paths = _manifest_path_set(previous.graph_path.parent / "source-manifest.json")
    if relative in current_paths or relative not in previous_paths:
        raise GraphifyUnavailable("目标路径没有通过 current/previous manifest 删除校验")
    return previous, relative


def _sanitize_query_output(text: str, *, project: Path, graph_path: Path) -> str:
    value = str(text or "")
    value = re.sub(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])", "", value)
    # URI and network-path forms must be removed before ordinary absolute
    # paths. Otherwise a leading scheme/UNC prefix can shield the embedded
    # absolute path from the generic path expressions below.
    value = re.sub(
        r"(?i)\b(?:file:(?://+)?|vscode://file/)[^\s\"'`<>]+",
        "<redacted-uri>",
        value,
    )
    value = re.sub(
        r"(?<![:/\\\w])(?:\\\\(?:\?\\(?:UNC\\)?|\.\\)?[^\\\s\"'`<>]+"
        r"(?:\\[^\\\s\"'`<>]+)+|//[^/\s\"'`<>]+/[^\s\"'`<>]+)",
        "<redacted-path>",
        value,
    )
    cache_dir = project_cache_dir(project)
    replacements = {
        str(graph_path): "__TMUX_GRAPHIFY_GRAPH_PATH__",
        str(cache_dir): "__TMUX_GRAPHIFY_CACHE_PATH__",
        str(project): "__TMUX_GRAPHIFY_PROJECT_PATH__",
    }
    for source, target in sorted(replacements.items(), key=lambda item: len(item[0]), reverse=True):
        value = value.replace(source, target)
    value = re.sub(r"(?<![:/\w])/(?!/)[^\s\"'`<>]+", "<redacted-path>", value)
    value = re.sub(r"(?<![\w])(?:[A-Za-z]:[\\/]|~[/\\])[^\s\"'`<>]+", "<redacted-path>", value)
    value = value.replace("__TMUX_GRAPHIFY_GRAPH_PATH__", "<graph-cache>/graph.json")
    value = value.replace("__TMUX_GRAPHIFY_CACHE_PATH__", "<graph-cache>")
    value = value.replace("__TMUX_GRAPHIFY_PROJECT_PATH__", ".")
    value = "".join(
        character
        for character in value
        if character in {"\n", "\r", "\t"} or ord(character) >= 32
    )
    return value


def _query_warnings(assessment: GraphifyFreshnessAssessment) -> tuple[str, ...]:
    if assessment.state == "fresh":
        return ()
    if assessment.state == "stale":
        paths = ", ".join(assessment.changed_paths)
        suffix = f" Changed paths: {paths}." if paths else ""
        return (
            "Graph is older than current controlled source and is navigation-only."
            + suffix
            + " Verify results in source, tests, and config.",
        )
    return (
        "Graph freshness could not be confirmed; treat results as old navigation evidence and verify in source.",
    )


def _resolve_query_scope(project: Path) -> _GraphifyQueryScope:
    scope_payload = _read_runner_query_scope(project)
    scope_key = str(scope_payload.get("scope_key", "") or "").strip()
    runner_id = _normalize_query_scope_component(scope_payload.get("runner_id", ""))
    stage_key = _normalize_graphify_stage_key(scope_payload.get("stage_key", ""))
    if scope_key and runner_id and stage_key:
        mode = normalize_graphify_mode(
            scope_payload.get("mode", GraphifyMode.AUTO.value),
            default=GraphifyMode.AUTO,
        )
        expected_scope_key = _runner_query_scope_key(runner_id, mode, stage_key)
        generation = _normalize_query_scope_component(
            os.environ.get("TMUX_GRAPHIFY_SESSION_GENERATION", ""),
            max_length=64,
        )
        generations = {
            _normalize_query_scope_component(value, max_length=64)
            for value in scope_payload.get("session_generations", ())
            if _normalize_query_scope_component(value, max_length=64)
        }
        return _GraphifyQueryScope(
            active=True,
            accepted=bool(
                scope_key == expected_scope_key
                and generation
                and generation in generations
            ),
            runner_id=runner_id,
            stage_key=stage_key,
            scope_key=scope_key,
        )
    # Compatibility for manual/legacy queries created before scope v2. They
    # remain queryable, but cannot impersonate a stage key.
    return _GraphifyQueryScope(
        runner_id=_normalize_query_scope_component(
            os.environ.get("TMUX_GRAPHIFY_RUNNER_ID", "")
        ),
    )


def _rotate_and_append_query_audit(
    project: Path,
    result: GraphifyQueryResult,
    query_scope: _GraphifyQueryScope,
) -> None:
    cache_dir = project_cache_dir(project)
    cache_dir.mkdir(parents=True, exist_ok=True)
    audit_path = cache_dir / "query-audit.jsonl"
    lock_path = cache_dir / "query-audit.lock"
    handle = lock_path.open("a+", encoding="utf-8")
    try:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        with contextlib.suppress(OSError):
            if audit_path.stat().st_size >= GRAPHIFY_QUERY_AUDIT_MAX_BYTES:
                os.replace(audit_path, audit_path.with_suffix(".jsonl.1"))
        payload = {
            "query_id": result.query_id,
            "runner_id": query_scope.runner_id,
            "stage_key": query_scope.stage_key,
            "scope_match": query_scope.accepted,
            "command": result.command,
            "generation_scope": result.generation_scope,
            "fingerprint": result.graph_fingerprint,
            "freshness": result.freshness,
            "duration_ms": result.duration_ms,
            "ok": result.ok,
            "truncated": result.truncated,
            "timestamp": _now_iso(),
            "error_kind": result.error_kind,
        }
        with audit_path.open("a", encoding="utf-8") as audit:
            audit.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
    finally:
        if fcntl is not None:
            with contextlib.suppress(OSError):
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _publish_query_result(
    project: Path,
    result: GraphifyQueryResult,
    query_scope: _GraphifyQueryScope | None = None,
) -> None:
    if query_scope is None:
        query_scope = _resolve_query_scope(project)
    cache_dir = project_cache_dir(project)
    with _metadata_lock(cache_dir):
        active_scope_key = str(
            _read_runner_query_scope(project).get("scope_key", "") or ""
        )
        if query_scope.active and (
            not query_scope.accepted
            or query_scope.scope_key != active_scope_key
        ):
            # A query from an unregistered session or a scope superseded while
            # the query was running remains a valid local result, but must not
            # contaminate the active stage aggregate.
            return
        if not query_scope.active and active_scope_key:
            return
        previous = read_graphify_project_status(project) or {}
        allowed = {field.name for field in dataclasses.fields(GraphifyStatus)}
        payload = {key: value for key, value in previous.items() if key in allowed}
        payload.setdefault("mode", normalize_graphify_mode(
            os.environ.get("TMUX_GRAPHIFY_MODE", GraphifyMode.AUTO.value),
            default=GraphifyMode.AUTO,
        ).value)
        payload.setdefault("state", GraphifyState.READY.value)
        payload.setdefault("version", GRAPHIFY_VERSION)
        if result.generation_scope != "previous":
            payload["freshness"] = result.freshness
            if result.freshness == "fresh":
                payload["state"] = GraphifyState.READY.value
            elif result.freshness in {"stale", "cache_fallback"}:
                payload["state"] = GraphifyState.STALE.value
            elif result.freshness == "unknown":
                payload["state"] = GraphifyState.DEGRADED.value
        payload["query_count_stage"] = max(0, int(previous.get("query_count_stage", 0) or 0)) + 1
        payload["last_query_command"] = result.command
        payload["last_query_at"] = _now_iso()
        payload["last_query_status"] = "ok" if result.ok else result.error_kind or "error"
        payload["last_query_freshness"] = result.freshness
        payload["last_query_truncated"] = result.truncated
        _write_status(project, GraphifyStatus(**payload))


def _validated_readonly_query_values(command: str, values: Sequence[str]) -> tuple[str, ...]:
    normalized = tuple(str(value) for value in values)
    expected_arity = {"query": 1, "affected": 1, "explain": 1, "path": 2}
    if command == "god-nodes":
        if len(normalized) != 2 or normalized[0] != "--top":
            raise GraphifyUnavailable("god-nodes 只允许 wrapper 生成的 --top <positive-int>")
        try:
            top = int(normalized[1])
        except ValueError as exc:
            raise GraphifyUnavailable("god-nodes --top 必须是正整数") from exc
        if top <= 0 or str(top) != normalized[1].strip():
            raise GraphifyUnavailable("god-nodes --top 必须是正整数")
        return ("--top", str(top))
    if command not in expected_arity or len(normalized) != expected_arity[command]:
        raise GraphifyUnavailable(
            f"只读 Graphify 命令 {command} 参数数量非法: expected {expected_arity.get(command, 0)}, got {len(normalized)}"
        )
    for value in normalized:
        if not value.strip():
            raise GraphifyUnavailable(f"只读 Graphify 命令 {command} 不接受空参数")
        if value.lstrip().startswith("-"):
            raise GraphifyUnavailable(f"只读 Graphify 命令 {command} 拒绝 option-like 位置参数")
    return normalized


def execute_readonly_query(
    project_dir: str | Path,
    command: str,
    values: Sequence[str],
    *,
    generation_scope: str = "current",
) -> GraphifyQueryResult:
    command_text = str(command or "").strip().lower()
    if command_text not in _QUERY_COMMANDS:
        raise GraphifyUnavailable(f"只读 Graphify wrapper 不允许命令: {command_text}")
    validated_values = _validated_readonly_query_values(command_text, values)
    normalized_generation_scope = str(generation_scope or "current").strip().lower()
    if normalized_generation_scope not in {"current", "previous"}:
        raise GraphifyUnavailable(f"非法 Graphify query generation scope: {normalized_generation_scope!r}")
    if normalized_generation_scope == "previous" and command_text != "affected":
        raise GraphifyUnavailable("Graphify previous generation 仅支持 affected 查询")
    mode = normalize_graphify_mode(os.environ.get("TMUX_GRAPHIFY_MODE", GraphifyMode.AUTO.value), default=GraphifyMode.AUTO)
    if mode == GraphifyMode.OFF:
        raise GraphifyUnavailable("本 runner 已关闭 Graphify")
    project = _project_root(project_dir)
    cache_dir = project_cache_dir(project)
    query_scope = _resolve_query_scope(project)
    query_id = uuid.uuid4().hex
    started = time.monotonic()
    with _query_lock(cache_dir):
        query_values = validated_values
        if normalized_generation_scope == "previous":
            generation, deleted_path = _previous_graph_for_deleted_target(
                project,
                validated_values[0],
            )
            query_values = (deleted_path,)
            assessment = GraphifyFreshnessAssessment(
                state="stale",
                graph_fingerprint=generation.fingerprint,
                changed_paths=(deleted_path,),
                checked_at=_now_iso(),
                reason="explicit previous-generation query for a manifest-confirmed deleted path",
            )
        else:
            generation = _current_graph_or_raise(project)
            assessment = assess_graphify_freshness(project, generation)
            with contextlib.suppress(OSError):
                _publish_freshness_assessment(project, assessment)
        resolution = resolve_graphify_tool()
        completed: subprocess.CompletedProcess[str] | None = None
        truncated = False
        error_kind = ""
        if resolution.compatible:
            args = [
                resolution.executable_path,
                command_text,
                *query_values,
                "--graph",
                str(generation.graph_path),
            ]
            if command_text == "query":
                args.extend(["--budget", "1500"])
            try:
                completed, truncated = _run_bounded_graphify_query(
                    args,
                    cwd=generation.graph_path.parent,
                    timeout_sec=30,
                )
            except GraphifyBuildFailed as exc:
                error_kind = "timeout" if "timed out" in str(exc).lower() else "error"
                completed = subprocess.CompletedProcess(
                    args=args,
                    returncode=1,
                    stdout="",
                    stderr=str(exc),
                )
        else:
            error_kind = "unavailable"
            completed = subprocess.CompletedProcess(
                args=[],
                returncode=1,
                stdout="",
                stderr=resolution.error,
            )
        assert completed is not None
        ok = completed.returncode == 0
        if not ok and not error_kind:
            error_kind = "error"
        raw_output = completed.stdout if ok else completed.stderr or completed.stdout
        sanitized = _sanitize_query_output(
            raw_output,
            project=project,
            graph_path=generation.graph_path,
        ).strip()
        if len(sanitized) > GRAPHIFY_QUERY_MAX_CHARS:
            sanitized = sanitized[:GRAPHIFY_QUERY_MAX_CHARS].rstrip()
            truncated = True
        if truncated:
            marker = "\n[wrapper output truncated]"
            sanitized = sanitized[: GRAPHIFY_QUERY_MAX_CHARS - len(marker)].rstrip() + marker
        effective_freshness = (
            "stale"
            if normalized_generation_scope == "previous"
            else "cache_fallback"
            if generation.freshness == "cache_fallback" and assessment.state == "fresh"
            else assessment.state
        )
        warnings = list(_query_warnings(assessment))
        if normalized_generation_scope == "previous":
            warnings.insert(
                0,
                "OLD_GENERATION: result comes from the previous immutable graph for a deleted path; it is ambiguous navigation evidence, never a current source fact.",
            )
        if effective_freshness == "cache_fallback":
            warnings.insert(
                0,
                "Current graph generation failed validation; this query uses the previous valid cache and requires source verification.",
            )
        result = GraphifyQueryResult(
            ok=ok,
            query_id=query_id,
            command=command_text,
            graph_fingerprint=generation.fingerprint,
            freshness=effective_freshness,
            warnings=tuple(warnings),
            truncated=truncated,
            duration_ms=max(0, int((time.monotonic() - started) * 1000)),
            result_text=sanitized,
            error_kind=error_kind,
            generation_scope=normalized_generation_scope,
        )
        with contextlib.suppress(OSError):
            _rotate_and_append_query_audit(project, result, query_scope)
        with contextlib.suppress(OSError, ValueError):
            _publish_query_result(project, result, query_scope)
        return result


def run_readonly_query(
    project_dir: str | Path,
    command: str,
    values: Sequence[str],
    *,
    output_format: str = "text",
    generation_scope: str = "current",
) -> str:
    result = execute_readonly_query(
        project_dir,
        command,
        values,
        generation_scope=generation_scope,
    )
    normalized_format = str(output_format or "text").strip().lower()
    if normalized_format == "json":
        return json.dumps(result.to_public_dict(), ensure_ascii=False)
    if normalized_format != "text":
        raise ValueError(f"unsupported Graphify query output format: {output_format}")
    return result.to_text()


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
    requested = str(getattr(args, "project", "") or "").strip()
    worker_project = str(os.environ.get("TMUX_GRAPHIFY_PROJECT_DIR", "") or "").strip()
    read_only = str(os.environ.get("TMUX_GRAPHIFY_READ_ONLY", "") or "").strip().lower() in {"1", "true", "yes", "on"}
    if read_only:
        if not worker_project:
            raise GraphifyUnavailable("agent read-only mode requires TMUX_GRAPHIFY_PROJECT_DIR")
        configured = _project_root(worker_project)
        if requested and _project_root(requested) != configured:
            raise GraphifyUnavailable("agent read-only mode rejects --project outside TMUX_GRAPHIFY_PROJECT_DIR")
        return configured
    return _project_root(requested or worker_project or os.getcwd())


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
    query.add_argument("--format", choices=("text", "json"), default="text")
    affected = subparsers.add_parser("affected", help="read-only reverse impact query")
    affected.add_argument("symbol")
    affected.add_argument(
        "--previous",
        action="store_true",
        help="query the previous immutable graph for a manifest-confirmed deleted path",
    )
    affected.add_argument("--project", default="")
    affected.add_argument("--format", choices=("text", "json"), default="text")
    path = subparsers.add_parser("path", help="read-only shortest path query")
    path.add_argument("source")
    path.add_argument("target")
    path.add_argument("--project", default="")
    path.add_argument("--format", choices=("text", "json"), default="text")
    explain = subparsers.add_parser("explain", help="read-only node explanation")
    explain.add_argument("symbol")
    explain.add_argument("--project", default="")
    explain.add_argument("--format", choices=("text", "json"), default="text")
    god_nodes = subparsers.add_parser("god-nodes", help="read-only hub query")
    god_nodes.add_argument("--top", type=int, default=10)
    god_nodes.add_argument("--project", default="")
    god_nodes.add_argument("--format", choices=("text", "json"), default="text")
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
            result = execute_readonly_query(
                project,
                command,
                values,
                generation_scope=(
                    "previous"
                    if command == "affected" and bool(getattr(args, "previous", False))
                    else "current"
                ),
            )
            if str(args.format) == "json":
                print(json.dumps(result.to_public_dict(), ensure_ascii=False))
            else:
                print(result.to_text())
            return 0 if result.ok else 1
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
    "GRAPHIFY_FULL_GUIDE_MARKER",
    "GRAPHIFY_GUIDE_VERSION",
    "GRAPHIFY_PACKAGE",
    "GRAPHIFY_QUERY_RESULT_SCHEMA",
    "GRAPHIFY_VERSION",
    "GraphifyFreshnessAssessment",
    "GraphifyBuildConfig",
    "GraphifyBuildFailed",
    "GraphifyEvidence",
    "GraphifyMode",
    "GraphifyProjectSnapshot",
    "GraphifyQueryIntent",
    "GraphifyQueryResult",
    "GraphifyQuerySuggestion",
    "GraphifySchemaError",
    "GraphifySnapshotError",
    "GraphifyStatus",
    "GraphifyToolResolution",
    "GraphifyTurnProfile",
    "GraphifyTurnContext",
    "GraphifyUnavailable",
    "TurnContextBlock",
    "assess_graphify_freshness",
    "build_graphify_evidence_block",
    "cancel_graphify_processes",
    "capture_graphify_source_manifest",
    "cli_main",
    "create_graphify_snapshot",
    "execute_readonly_query",
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
