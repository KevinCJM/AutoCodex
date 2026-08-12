from __future__ import annotations

import json
import os
import hashlib
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from typing import Callable, Mapping, TypeVar


SOURCE_CHANGE_LEDGER_SCHEMA = "tmux-source-change-ledger/1"
SOURCE_CHANGE_LEDGER_FILE = "source_change_ledger.json"
LEGACY_GRAPHIFY_CHANGE_LEDGER_FILE = "graphify_change_ledger.json"


_MutationResult = TypeVar("_MutationResult")
_LEDGER_LOCKS: dict[str, threading.RLock] = {}
_LEDGER_LOCKS_GUARD = threading.Lock()


def _ledger_lock_for(path: Path) -> threading.RLock:
    """Return the process-local RMW lock for one canonical ledger path."""

    key = str(path.expanduser().resolve())
    with _LEDGER_LOCKS_GUARD:
        lock = _LEDGER_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _LEDGER_LOCKS[key] = lock
        return lock


def _locked_ledger_mutator(
    function: Callable[..., _MutationResult],
) -> Callable[..., _MutationResult]:
    """Serialize a public ledger mutator's complete read-modify-write cycle."""

    @wraps(function)
    def wrapped(ledger_path: str | Path, *args: object, **kwargs: object) -> _MutationResult:
        path = Path(ledger_path).expanduser().resolve()
        with _ledger_lock_for(path):
            return function(path, *args, **kwargs)

    return wrapped


@dataclass(frozen=True)
class SourceChangeSet:
    task_name: str
    state: str
    added: tuple[str, ...] = ()
    modified: tuple[str, ...] = ()
    deleted: tuple[str, ...] = ()
    cumulative_changed_files: tuple[str, ...] = ()
    cumulative_deleted_files: tuple[str, ...] = ()
    reason: str = ""

    @property
    def changed_files(self) -> tuple[str, ...]:
        return tuple(sorted(dict.fromkeys((*self.added, *self.modified, *self.deleted))))


def source_change_ledger_path(runtime_root: str | Path) -> Path:
    return Path(runtime_root).expanduser().resolve() / SOURCE_CHANGE_LEDGER_FILE


def source_change_ledger_exists(ledger_path: str | Path) -> bool:
    path = Path(ledger_path).expanduser().resolve()
    return path.is_file() or (
        path.name == SOURCE_CHANGE_LEDGER_FILE
        and path.with_name(LEGACY_GRAPHIFY_CHANGE_LEDGER_FILE).is_file()
    )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_ledger(path: Path) -> dict[str, object]:
    if not path.exists() and path.name == SOURCE_CHANGE_LEDGER_FILE:
        legacy_path = path.with_name(LEGACY_GRAPHIFY_CHANGE_LEDGER_FILE)
        if legacy_path.is_file():
            path = legacy_path
    if not path.exists():
        return {
            "schema": SOURCE_CHANGE_LEDGER_SCHEMA,
            "updated_at": "",
            "tasks": {},
        }
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as error:  # noqa: BLE001
        raise RuntimeError(f"源码改动账本不可读: {path}: {error}") from error
    schema = str(payload.get("schema", "") or "") if isinstance(payload, dict) else ""
    if schema not in {SOURCE_CHANGE_LEDGER_SCHEMA, "tmux-graphify-change-ledger/1"}:
        raise RuntimeError(f"源码改动账本 schema 非法: {path}")
    tasks = payload.get("tasks", {})
    if not isinstance(tasks, dict):
        raise RuntimeError(f"源码改动账本 tasks 非法: {path}")
    return payload


def _write_ledger(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    next_payload = dict(payload)
    next_payload["schema"] = SOURCE_CHANGE_LEDGER_SCHEMA
    next_payload["updated_at"] = _now_iso()
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(next_payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def _capture_manifest(
    project_dir: str | Path,
    source_config: Mapping[str, object] | None,
) -> dict[str, str]:
    """Capture a graph-engine-independent source manifest.

    The ledger deliberately does not import CodeGraph.  It records only the
    controlled source files before and after a task, so the same history remains
    useful if the navigation engine changes again.
    """

    root = Path(project_dir).expanduser().resolve()
    raw_config = dict(source_config or {})
    max_files = max(1, int(raw_config.get("max_files", 50_000) or 50_000))
    max_file_bytes = max(1, int(raw_config.get("max_file_bytes", 2 * 1024 * 1024) or 2 * 1024 * 1024))
    allowed_suffixes = {
        ".c", ".cc", ".cpp", ".cs", ".go", ".h", ".hpp", ".java", ".js", ".jsx",
        ".kt", ".kts", ".m", ".mm", ".php", ".py", ".pyi", ".rb", ".rs", ".scala",
        ".sh", ".sql", ".swift", ".ts", ".tsx", ".vue", ".yaml", ".yml", ".toml",
    }
    excluded_parts = {
        ".git", ".codegraph", ".tmux_workflow", ".development_runtime",
        ".detailed_design_runtime", ".requirements_runtime", ".pytest_cache",
        "node_modules", "venv", ".venv", "build", "dist", "coverage", "__pycache__",
    }

    candidates: list[str] = []
    if (root / ".git").exists():
        completed = subprocess.run(
            ["git", "-C", str(root), "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            check=False,
            capture_output=True,
        )
        if completed.returncode == 0:
            candidates = [item.decode("utf-8", errors="surrogateescape") for item in completed.stdout.split(b"\0") if item]
    if not candidates:
        candidates = [str(path.relative_to(root)) for path in root.rglob("*") if path.is_file()]

    manifest: dict[str, str] = {}
    for relative in sorted(dict.fromkeys(candidates)):
        if len(manifest) >= max_files:
            raise RuntimeError(f"源码文件数超过上限: {max_files}")
        rel_path = Path(relative)
        if (
            rel_path.is_absolute()
            or ".." in rel_path.parts
            or any(
                part in excluded_parts
                or (part.startswith(".") and part.endswith("_runtime"))
                or part == "task_runtime"
                for part in rel_path.parts
            )
        ):
            continue
        if rel_path.name.startswith(".env") or rel_path.suffix.lower() not in allowed_suffixes:
            continue
        path = root / rel_path
        try:
            if path.is_symlink() or not path.is_file() or path.stat().st_size > max_file_bytes:
                continue
            resolved = path.resolve()
            resolved.relative_to(root)
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except (OSError, ValueError):
            continue
        manifest[rel_path.as_posix()] = f"sha256:{digest}"
    return manifest


def _tasks(payload: dict[str, object]) -> dict[str, dict[str, object]]:
    raw = payload.setdefault("tasks", {})
    if not isinstance(raw, dict):
        raise RuntimeError("源码改动账本 tasks 非法")
    normalized: dict[str, dict[str, object]] = {}
    for key, value in raw.items():
        if isinstance(value, dict):
            normalized[str(key)] = dict(value)
    payload["tasks"] = normalized
    return normalized


def _bind_stage_generation(
    payload: dict[str, object],
    tasks: dict[str, dict[str, object]],
    *,
    stage_key: str,
    runner_id: str,
    task_key: str,
) -> None:
    """Bind baselines to one authoritative stage runner generation.

    A stage restart must never compare new work with a baseline captured by an
    older runner.  A08 deliberately keeps the active A07 deltas, while a new
    A07 generation invalidates both old A07 deltas and downstream A08 deltas.
    """

    normalized_stage = str(stage_key or "").strip().upper()
    normalized_runner = str(runner_id or "").strip()
    if not normalized_stage or not normalized_runner:
        return

    raw_generations = payload.setdefault("stage_generations", {})
    if not isinstance(raw_generations, dict):
        raise RuntimeError("源码改动账本 stage_generations 非法")
    generations = {str(key).upper(): str(value or "").strip() for key, value in raw_generations.items()}
    previous_runner = generations.get(normalized_stage, "")
    if previous_runner == normalized_runner:
        payload["stage_generations"] = generations
        return

    stale_keys: list[str] = []
    for key, task in tasks.items():
        task_stage = str(task.get("stage_key", "") or "").strip().upper()
        if task_stage == normalized_stage:
            stale_keys.append(key)
            continue
        if normalized_stage == "A07" and not task_stage:
            # Pre-generation ledgers only contained A07 task entries plus the
            # reserved A08 aggregate entry.  A new A07 runner cannot safely
            # inherit any of those unbound baselines or deltas.
            stale_keys.append(key)
            continue
        # Ledgers written before generation binding have no stage marker.  The
        # current task's baseline is unsafe to reuse even if its name matches.
        if not task_stage and key == task_key:
            stale_keys.append(key)
            continue
        if normalized_stage == "A07" and task_stage == "A08":
            stale_keys.append(key)

    for key in stale_keys:
        tasks.pop(key, None)
    if normalized_stage == "A07":
        generations.pop("A08", None)
    generations[normalized_stage] = normalized_runner
    payload["stage_generations"] = generations


def _cumulative_changed_files(tasks: Mapping[str, Mapping[str, object]]) -> tuple[str, ...]:
    values: list[str] = []
    for task in tasks.values():
        for key in ("added", "modified", "deleted"):
            raw = task.get(key, ())
            if isinstance(raw, (list, tuple)):
                values.extend(str(item) for item in raw if str(item).strip())
    return tuple(sorted(dict.fromkeys(values)))


def _cumulative_files_for_kind(
    tasks: Mapping[str, Mapping[str, object]],
    kind: str,
) -> tuple[str, ...]:
    values: list[str] = []
    for task in tasks.values():
        raw = task.get(kind, ())
        if isinstance(raw, (list, tuple)):
            values.extend(str(item) for item in raw if str(item).strip())
    return tuple(sorted(dict.fromkeys(values)))


@_locked_ledger_mutator
def ensure_source_task_baseline(
    ledger_path: str | Path,
    *,
    project_dir: str | Path,
    task_name: str,
    source_config: Mapping[str, object] | None = None,
    legacy_output_present: bool = False,
    stage_key: str = "",
    runner_id: str = "",
) -> SourceChangeSet:
    path = Path(ledger_path).expanduser().resolve()
    task_key = str(task_name or "").strip()
    if not task_key:
        raise ValueError("源码改动账本 task_name 不能为空")
    payload = _read_ledger(path)
    tasks = _tasks(payload)
    _bind_stage_generation(
        payload,
        tasks,
        stage_key=stage_key,
        runner_id=runner_id,
        task_key=task_key,
    )
    existing = tasks.get(task_key)
    if existing is not None:
        return _change_set_from_task(task_key, existing, tasks)

    task: dict[str, object] = {
        "task_name": task_key,
        "state": "unknown" if legacy_output_present else "baseline",
        "captured_at": _now_iso(),
        "baseline": {},
        "added": [],
        "modified": [],
        "deleted": [],
        "stage_key": str(stage_key or "").strip().upper(),
        "runner_id": str(runner_id or "").strip(),
        "reason": (
            "legacy_developer_output_without_pre_submit_baseline"
            if legacy_output_present
            else ""
        ),
    }
    if not legacy_output_present:
        try:
            task["baseline"] = _capture_manifest(project_dir, source_config)
        except Exception as error:  # noqa: BLE001
            task["state"] = "unknown"
            task["reason"] = f"baseline_capture_failed:{type(error).__name__}"
    tasks[task_key] = task
    payload["project_dir_key"] = _project_key(project_dir)
    _write_ledger(path, payload)
    return _change_set_from_task(task_key, task, tasks)


@_locked_ledger_mutator
def record_source_task_changes(
    ledger_path: str | Path,
    *,
    project_dir: str | Path,
    task_name: str,
    source_config: Mapping[str, object] | None = None,
    stage_key: str = "",
    runner_id: str = "",
) -> SourceChangeSet:
    path = Path(ledger_path).expanduser().resolve()
    task_key = str(task_name or "").strip()
    payload = _read_ledger(path)
    tasks = _tasks(payload)
    _bind_stage_generation(
        payload,
        tasks,
        stage_key=stage_key,
        runner_id=runner_id,
        task_key=task_key,
    )
    task = tasks.get(task_key)
    if task is None:
        task = {
            "task_name": task_key,
            "state": "unknown",
            "captured_at": _now_iso(),
            "baseline": {},
            "added": [],
            "modified": [],
            "deleted": [],
            "stage_key": str(stage_key or "").strip().upper(),
            "runner_id": str(runner_id or "").strip(),
            "reason": "missing_pre_submit_baseline",
        }
        tasks[task_key] = task
    baseline = task.get("baseline", {})
    if task.get("state") == "unknown" or not isinstance(baseline, dict):
        _write_ledger(path, payload)
        return _change_set_from_task(task_key, task, tasks)
    try:
        current = _capture_manifest(project_dir, source_config)
    except Exception as error:  # noqa: BLE001
        task["state"] = "unknown"
        task["reason"] = f"current_capture_failed:{type(error).__name__}"
        _write_ledger(path, payload)
        return _change_set_from_task(task_key, task, tasks)

    baseline_map = {str(key): str(value) for key, value in baseline.items()}
    added = sorted(set(current).difference(baseline_map))
    deleted = sorted(set(baseline_map).difference(current))
    modified = sorted(
        key
        for key in set(current).intersection(baseline_map)
        if current[key] != baseline_map[key]
    )
    task.update(
        {
            "state": "recorded",
            "observed_at": _now_iso(),
            "added": added,
            "modified": modified,
            "deleted": deleted,
            "reason": "",
        }
    )
    _write_ledger(path, payload)
    return _change_set_from_task(task_key, task, tasks)


@_locked_ledger_mutator
def mark_source_change_scope_unknown(
    ledger_path: str | Path,
    *,
    project_dir: str | Path,
    scope_name: str,
    reason: str,
) -> SourceChangeSet:
    """Persist an explicit gap without preventing later scoped baselines."""

    path = Path(ledger_path).expanduser().resolve()
    scope_key = str(scope_name or "").strip()
    if not scope_key:
        raise ValueError("源码改动账本 unknown scope_name 不能为空")
    payload = _read_ledger(path)
    tasks = _tasks(payload)
    task = tasks.setdefault(
        scope_key,
        {
            "task_name": scope_key,
            "state": "unknown",
            "captured_at": _now_iso(),
            "baseline": {},
            "added": [],
            "modified": [],
            "deleted": [],
            "reason": str(reason or "change_scope_unknown").strip(),
        },
    )
    payload["project_dir_key"] = _project_key(project_dir)
    _write_ledger(path, payload)
    return _change_set_from_task(scope_key, task, tasks)


def load_source_cumulative_changes(
    ledger_path: str | Path,
) -> SourceChangeSet:
    path = Path(ledger_path).expanduser().resolve()
    legacy_exists = (
        path.name == SOURCE_CHANGE_LEDGER_FILE
        and path.with_name(LEGACY_GRAPHIFY_CHANGE_LEDGER_FILE).is_file()
    )
    if not path.exists() and not legacy_exists:
        return SourceChangeSet(
            task_name="",
            state="unknown",
            reason="change_ledger_missing",
        )
    try:
        payload = _read_ledger(path)
        tasks = _tasks(payload)
    except Exception as error:  # noqa: BLE001
        return SourceChangeSet(
            task_name="",
            state="unknown",
            reason=f"change_ledger_invalid:{type(error).__name__}",
        )
    unknown = sorted(
        key for key, task in tasks.items() if str(task.get("state", "")) == "unknown"
    )
    return SourceChangeSet(
        task_name="",
        state="unknown" if unknown else "recorded",
        cumulative_changed_files=_cumulative_changed_files(tasks),
        cumulative_deleted_files=_cumulative_files_for_kind(tasks, "deleted"),
        reason=(f"unknown_tasks:{','.join(unknown)}" if unknown else ""),
    )


def _change_set_from_task(
    task_name: str,
    task: Mapping[str, object],
    tasks: Mapping[str, Mapping[str, object]],
) -> SourceChangeSet:
    def _items(key: str) -> tuple[str, ...]:
        raw = task.get(key, ())
        if not isinstance(raw, (list, tuple)):
            return ()
        return tuple(sorted(dict.fromkeys(str(item) for item in raw if str(item).strip())))

    return SourceChangeSet(
        task_name=task_name,
        state=str(task.get("state", "unknown") or "unknown"),
        added=_items("added"),
        modified=_items("modified"),
        deleted=_items("deleted"),
        cumulative_changed_files=_cumulative_changed_files(tasks),
        cumulative_deleted_files=_cumulative_files_for_kind(tasks, "deleted"),
        reason=str(task.get("reason", "") or ""),
    )


def _project_key(project_dir: str | Path) -> str:
    resolved = str(Path(project_dir).expanduser().resolve())
    return hashlib.sha256(resolved.encode("utf-8")).hexdigest()


__all__ = [
    "SOURCE_CHANGE_LEDGER_FILE",
    "SOURCE_CHANGE_LEDGER_SCHEMA",
    "SourceChangeSet",
    "ensure_source_task_baseline",
    "source_change_ledger_exists",
    "source_change_ledger_path",
    "load_source_cumulative_changes",
    "mark_source_change_scope_unknown",
    "record_source_task_changes",
]
