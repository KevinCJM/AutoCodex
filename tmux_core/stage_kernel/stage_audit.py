from __future__ import annotations

from collections.abc import Mapping, Sequence
import datetime as dt
import hashlib
import json
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from T12_requirements_common import sanitize_requirement_name


STAGE_AUDIT_SCHEMA_VERSION = 1
SUPPORTED_STAGE_AUDIT_STAGES = frozenset(("A03", "A04", "A05", "A06", "A07", "A08"))
SUPPORTED_STAGE_AUDIT_EVENT_SCOPES = frozenset(("stage", "task", "hitl", "review"))
_EVENT_SCOPE_BY_TYPE = {
    "stage_started": "stage",
    "before_cleanup": "stage",
    "clarification_updated": "stage",
    "stage_passed": "stage",
    "stage_failed": "stage",
    "hitl_question": "hitl",
    "hitl_answer": "hitl",
    "review_merged": "review",
    "overall_review_merged": "review",
    "feedback_written": "review",
    "change_after_review": "task",
    "change_after_overall_review": "stage",
    "task_passed": "task",
    "task_failed": "task",
}
_AMBIGUOUS_EVENT_TYPES = frozenset(("developer_output",))
_EVENT_TYPES_WITH_REVIEWER_ARRAYS = frozenset(("before_cleanup", "review_merged", "overall_review_merged"))
_LOCK_GUARD = threading.RLock()
_FILE_LOCKS: dict[str, threading.RLock] = {}


@dataclass(frozen=True)
class StageAuditRunContext:
    project_dir: Path
    requirement_name: str
    stage: str
    audit_log_path: Path
    stage_run_index: int


def _normalize_stage(stage: str) -> str:
    normalized = str(stage or "").strip().upper()
    if normalized not in SUPPORTED_STAGE_AUDIT_STAGES:
        raise ValueError(f"不支持的阶段流水编号: {stage}")
    return normalized


def _normalize_requirement_name(requirement_name: str) -> str:
    normalized = str(requirement_name or "").strip()
    if not normalized:
        raise ValueError("缺少 requirement_name，无法建立阶段流水")
    return normalized


def _normalize_event_type(event_type: str) -> str:
    normalized = str(event_type or "").strip()
    if normalized not in _EVENT_SCOPE_BY_TYPE and normalized not in _AMBIGUOUS_EVENT_TYPES:
        raise ValueError(f"不支持的阶段流水事件类型: {event_type}")
    return normalized


def _resolve_event_scope(stage: str, event_type: str, event_scope: str | None) -> str:
    if event_scope is not None:
        normalized = str(event_scope or "").strip()
        if normalized not in SUPPORTED_STAGE_AUDIT_EVENT_SCOPES:
            raise ValueError(f"不支持的阶段流水事件范围: {event_scope}")
        return normalized
    if event_type == "developer_output":
        return "stage" if stage == "A08" else "task"
    return _EVENT_SCOPE_BY_TYPE[event_type]


def _file_lock(audit_log_path: Path) -> threading.RLock:
    lock_key = str(audit_log_path)
    with _LOCK_GUARD:
        lock = _FILE_LOCKS.get(lock_key)
        if lock is None:
            lock = threading.RLock()
            _FILE_LOCKS[lock_key] = lock
        return lock


def warn_stage_audit_failure(
    *,
    stage: str = "",
    event_type: str = "",
    audit_log_path: str | Path | None = None,
    error: BaseException | str = "",
) -> None:
    try:
        path_text = str(audit_log_path or "")
        sys.stderr.write(
            "警告：阶段流水处理失败"
            f" stage={stage or ''}"
            f" event_type={event_type or ''}"
            f" audit_log_path={path_text}"
            f" error={error}\n"
        )
        sys.stderr.flush()
    except Exception:
        pass


def build_stage_audit_log_path(project_dir: str | Path, requirement_name: str, stage: str) -> Path:
    normalized_requirement = _normalize_requirement_name(requirement_name)
    normalized_stage = _normalize_stage(stage)
    project_root = Path(project_dir).expanduser().resolve()
    safe_name = sanitize_requirement_name(normalized_requirement)
    return project_root / f"{safe_name}_{normalized_stage}_流水记录.jsonl"


def _read_max_integer_field(audit_log_path: Path, field_name: str) -> int:
    if not audit_log_path.exists() or not audit_log_path.is_file():
        return 0
    max_value = 0
    for line in audit_log_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        value = payload.get(field_name)
        if isinstance(value, int) and not isinstance(value, bool) and value > max_value:
            max_value = value
    return max_value


def _json_safe(value: Any) -> Any:
    try:
        json.dumps(value, ensure_ascii=False)
        return value
    except TypeError:
        if isinstance(value, Mapping):
            return {str(key): _json_safe(item) for key, item in value.items()}
        if isinstance(value, (list, tuple, set)):
            return [_json_safe(item) for item in value]
        return str(value)


def _append_unique(items: list[Any], value: Any) -> None:
    marker = json.dumps(_json_safe(value), ensure_ascii=False, sort_keys=True)
    existing = {json.dumps(_json_safe(item), ensure_ascii=False, sort_keys=True) for item in items}
    if marker not in existing:
        items.append(value)


def _metadata_with_defaults(metadata: Mapping[str, Any] | None, *, task_name: str = "") -> dict[str, Any]:
    normalized = _json_safe(dict(metadata or {}))
    if not isinstance(normalized, dict):
        normalized = {}
    for key in ("missing_files", "read_errors"):
        value = normalized.get(key)
        if value is None or value == "":
            normalized[key] = []
        elif isinstance(value, (list, tuple, set)):
            normalized[key] = list(value)
        else:
            normalized[key] = [value]
    normalized.setdefault("trigger", "")
    normalized.setdefault("error", "")
    normalized.setdefault("task_name", str(task_name or ""))
    if normalized.get("task_name") is None:
        normalized["task_name"] = ""
    normalized["task_name"] = str(normalized.get("task_name") or task_name or "")
    for key in ("agent_names", "session_names"):
        value = normalized.get(key)
        if value is None or value == "":
            normalized[key] = []
        elif isinstance(value, (list, tuple, set)):
            normalized[key] = list(value)
        else:
            normalized[key] = [value]
    normalized.setdefault("agent_name", "")
    normalized.setdefault("session_name", "")
    normalized["agent_name"] = str(normalized.get("agent_name") or "")
    normalized["session_name"] = str(normalized.get("session_name") or "")
    return normalized


def _normalize_file_path(value: str | Path | None) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    return str(Path(text).expanduser().resolve())


def _sha256_text(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _read_text_snapshot(path_text: str, metadata: dict[str, Any]) -> str:
    if not path_text:
        return ""
    path = Path(path_text)
    if not path.exists() or not path.is_file():
        _append_unique(metadata["missing_files"], path_text)
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except Exception as error:  # noqa: BLE001
        _append_unique(metadata["read_errors"], {"path": path_text, "error": str(error)})
        return ""


def _build_fixed_snapshots(
    source_paths: Mapping[str, str | Path | None],
    metadata: dict[str, Any],
) -> tuple[dict[str, str], dict[str, Any]]:
    normalized_source_paths: dict[str, str] = {}
    snapshots: dict[str, Any] = {}
    for key, value in source_paths.items():
        normalized_path = _normalize_file_path(value)
        normalized_key = str(key)
        normalized_source_paths[normalized_key] = normalized_path
        snapshots[normalized_key] = _read_text_snapshot(normalized_path, metadata)
    return normalized_source_paths, snapshots


def _build_dynamic_snapshots(
    paths: Sequence[str | Path],
    metadata: dict[str, Any],
) -> tuple[list[str], list[dict[str, str]]]:
    normalized_paths: list[str] = []
    snapshots: list[dict[str, str]] = []
    for value in paths:
        normalized_path = _normalize_file_path(value)
        if not normalized_path:
            continue
        content = _read_text_snapshot(normalized_path, metadata)
        normalized_paths.append(normalized_path)
        snapshots.append(
            {
                "path": normalized_path,
                "content": content,
                "sha256": _sha256_text(content),
            }
        )
    return normalized_paths, snapshots


def _sequence_or_empty(paths: Sequence[str | Path] | None) -> Sequence[str | Path]:
    if paths is None:
        return ()
    if isinstance(paths, (str, Path)):
        return (paths,)
    return paths


def append_stage_audit_record(
    context: StageAuditRunContext | None,
    event_type: str,
    source_paths: Mapping[str, str | Path | None],
    metadata: Mapping[str, Any] | None = None,
    review_round_index: int | None = None,
    hitl_round_index: int | None = None,
    task_name: str = "",
    reviewer_markdown_paths: Sequence[str | Path] = (),
    reviewer_json_paths: Sequence[str | Path] = (),
    event_scope: str | None = None,
    snapshot_overrides: Mapping[str, Any] | None = None,
) -> bool:
    if context is None:
        warn_stage_audit_failure(event_type=str(event_type or ""), error="缺少 StageAuditRunContext")
        return False
    normalized_event_type = str(event_type or "").strip()
    try:
        normalized_event_type = _normalize_event_type(event_type)
        normalized_event_scope = _resolve_event_scope(context.stage, normalized_event_type, event_scope)
        normalized_task_name = str(task_name or "")
        record_metadata = _metadata_with_defaults(metadata, task_name=normalized_task_name)
        normalized_source_paths, snapshots = _build_fixed_snapshots(source_paths, record_metadata)
        for key, value in (snapshot_overrides or {}).items():
            snapshots[str(key)] = _json_safe(value)
        reviewer_markdowns, reviewer_markdown_snapshots = _build_dynamic_snapshots(
            _sequence_or_empty(reviewer_markdown_paths),
            record_metadata,
        )
        reviewer_jsons, reviewer_json_snapshots = _build_dynamic_snapshots(
            _sequence_or_empty(reviewer_json_paths),
            record_metadata,
        )
        if reviewer_markdowns or reviewer_jsons or normalized_event_type in _EVENT_TYPES_WITH_REVIEWER_ARRAYS:
            normalized_source_paths["reviewer_markdowns"] = reviewer_markdowns
            normalized_source_paths["reviewer_jsons"] = reviewer_jsons
            snapshots["reviewer_markdowns"] = reviewer_markdown_snapshots
            snapshots["reviewer_jsons"] = reviewer_json_snapshots
        lock = _file_lock(context.audit_log_path)
        with lock:
            record_index = _read_max_integer_field(context.audit_log_path, "record_index") + 1
            record = {
                "schema_version": STAGE_AUDIT_SCHEMA_VERSION,
                "record_index": record_index,
                "stage_run_index": context.stage_run_index,
                "requirement_name": context.requirement_name,
                "stage": context.stage,
                "event_type": normalized_event_type,
                "event_scope": normalized_event_scope,
                "created_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
                "review_round_index": review_round_index,
                "hitl_round_index": hitl_round_index,
                "task_name": normalized_task_name,
                "source_paths": normalized_source_paths,
                "snapshots": snapshots,
                "metadata": record_metadata,
            }
            line = json.dumps(record, ensure_ascii=False) + "\n"
            context.audit_log_path.parent.mkdir(parents=True, exist_ok=True)
            with context.audit_log_path.open("a", encoding="utf-8") as file_obj:
                file_obj.write(line)
                file_obj.flush()
        return True
    except Exception as error:  # noqa: BLE001
        warn_stage_audit_failure(
            stage=context.stage,
            event_type=normalized_event_type,
            audit_log_path=context.audit_log_path,
            error=error,
        )
        return False


def record_before_cleanup(
    context: StageAuditRunContext | None,
    source_paths: Mapping[str, str | Path | None],
    metadata: Mapping[str, Any] | None = None,
    reviewer_markdown_paths: Sequence[str | Path] = (),
    reviewer_json_paths: Sequence[str | Path] = (),
    review_round_index: int | None = None,
    hitl_round_index: int | None = None,
    task_name: str = "",
) -> bool:
    return append_stage_audit_record(
        context,
        event_type="before_cleanup",
        source_paths=source_paths,
        metadata=metadata,
        review_round_index=review_round_index,
        hitl_round_index=hitl_round_index,
        task_name=task_name,
        reviewer_markdown_paths=reviewer_markdown_paths,
        reviewer_json_paths=reviewer_json_paths,
        event_scope="stage",
    )


def begin_stage_audit_run(
    project_dir: str | Path,
    requirement_name: str,
    stage: str,
    metadata: Mapping[str, Any] | None = None,
) -> StageAuditRunContext | None:
    try:
        normalized_requirement = _normalize_requirement_name(requirement_name)
        normalized_stage = _normalize_stage(stage)
        project_root = Path(project_dir).expanduser().resolve()
        audit_log_path = build_stage_audit_log_path(project_root, normalized_requirement, normalized_stage)
        audit_log_path.parent.mkdir(parents=True, exist_ok=True)
    except Exception as error:  # noqa: BLE001
        warn_stage_audit_failure(
            stage=str(stage or "").strip(),
            event_type="stage_started",
            audit_log_path="",
            error=error,
        )
        return None

    try:
        lock = _file_lock(audit_log_path)
        with lock:
            try:
                max_stage_run_index = _read_max_integer_field(audit_log_path, "stage_run_index")
            except Exception as error:  # noqa: BLE001
                warn_stage_audit_failure(
                    stage=normalized_stage,
                    event_type="stage_started",
                    audit_log_path=audit_log_path,
                    error=error,
                )
                max_stage_run_index = 0
            context = StageAuditRunContext(
                project_dir=project_root,
                requirement_name=normalized_requirement,
                stage=normalized_stage,
                audit_log_path=audit_log_path,
                stage_run_index=max_stage_run_index + 1,
            )
            try:
                append_stage_audit_record(
                    context,
                    event_type="stage_started",
                    source_paths={},
                    metadata=metadata,
                    event_scope="stage",
                )
            except Exception as error:  # noqa: BLE001
                warn_stage_audit_failure(
                    stage=normalized_stage,
                    event_type="stage_started",
                    audit_log_path=audit_log_path,
                    error=error,
                )
            return context
    except Exception as error:  # noqa: BLE001
        warn_stage_audit_failure(
            stage=normalized_stage,
            event_type="stage_started",
            audit_log_path=audit_log_path,
            error=error,
        )
        return None


__all__ = [
    "STAGE_AUDIT_SCHEMA_VERSION",
    "SUPPORTED_STAGE_AUDIT_STAGES",
    "StageAuditRunContext",
    "append_stage_audit_record",
    "begin_stage_audit_run",
    "build_stage_audit_log_path",
    "record_before_cleanup",
    "warn_stage_audit_failure",
]
