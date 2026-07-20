# -*- encoding: utf-8 -*-
"""
@File: T11_tui_backend.py
@Modify Time: 2026/4/16
@Author: Kevin-Chen
@Descriptions: OpenTUI stdio backend，负责桥接 Python workflow 与前端 UI
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import json
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import uuid
from dataclasses import asdict, dataclass, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence, TextIO

from tmux_core.requirements_scope import resolve_requirement_name_from_prompt_response
from tmux_core.runtime.tmux_runtime import (
    RuntimeShutdownRequested,
    TMUX_IDENTITY_REQUIREMENT_NAME_OPTION,
    TMUX_IDENTITY_RUNTIME_DIR_OPTION,
    TMUX_IDENTITY_WORKER_ID_OPTION,
    TMUX_IDENTITY_WORKFLOW_ACTION_OPTION,
    TMUX_IDENTITY_WORK_DIR_OPTION,
    TmuxBatchWorker,
    TmuxRuntimeController,
    allow_runtime_shutdown_cleanup,
    clear_runtime_shutdown_request,
    cleanup_registered_tmux_workers,
    is_agent_ready_timeout_error,
    is_agent_runtime_intervention_error,
    is_agent_startup_intervention_error,
    is_runtime_shutdown_error,
    is_worker_death_error,
    load_worker_from_state_path,
    _backend_show_option,
    request_runtime_shutdown,
    stage_runner_context,
    try_resume_worker,
    worker_state_is_prelaunch_active,
)
from tmux_core.stage_kernel.detailed_design import (
    DETAILED_DESIGN_RUNTIME_ROOT_NAME,
    build_detailed_design_paths,
    build_parser as build_a05_parser,
    run_detailed_design_stage,
)
from tmux_core.stage_kernel.development import (
    DEVELOPMENT_RUNTIME_ROOT_NAME,
    build_development_paths,
    build_parser as build_a07_parser,
    build_reviewer_artifact_paths,
    run_development_stage,
)
from tmux_core.stage_kernel.overall_review import (
    MAX_OVERALL_REVIEW_ROUNDS,
    build_overall_review_paths,
    build_overall_review_reviewer_artifact_paths,
    build_parser as build_a08_parser,
    overall_review_passed,
    run_overall_review_stage,
)
from tmux_core.stage_kernel.task_split import (
    TASK_SPLIT_RUNTIME_ROOT_NAME,
    build_parser as build_a06_parser,
    build_task_split_paths,
    run_task_split_stage,
)
from tmux_core.stage_kernel.requirements_review import (
    REQUIREMENTS_REVIEW_TASK_NAME,
    REQUIREMENTS_REVIEW_RUNTIME_ROOT_NAME,
    build_reviewer_artifact_paths as build_requirements_reviewer_artifact_paths,
    build_parser as build_a04_parser,
    build_requirements_review_paths,
    run_requirements_review_stage,
)
from tmux_core.stage_kernel.requirement_intake import (
    NOTION_RUNTIME_ROOT_NAME,
    build_notion_hitl_paths,
    build_parser as build_a02_parser,
    run_requirement_intake_stage,
)
from tmux_core.stage_kernel.requirements_clarification import (
    REQUIREMENTS_RUNTIME_ROOT_NAME,
    build_parser as build_a03_parser,
    run_requirements_clarification_stage,
)
from tmux_core.stage_kernel.routing_init import (
    build_parser as build_a01_parser,
    format_batch_summary,
    prepare_agent_run_config,
    prompt_confirmation,
    render_noop_summary,
    render_preflight_summary,
    render_requirements_stage_placeholder,
    resolve_batch_selection,
    run_routing_stage,
)
from tmux_core.workflow.entry import build_parser as build_a00_parser, main as a00_main
from B01_terminal_interaction import (
    AgentInitControlCenter,
    collect_b01_request,
    render_control_help,
)
from T03_agent_init_workflow import (
    ROUTING_RUNTIME_ROOT_NAME,
    RunStore,
    list_routing_run_manifest_paths,
    required_routing_layer_paths,
    validate_routing_layer_artifacts,
)
from T08_pre_development import (
    build_pre_development_task_record_path,
    load_pre_development_task_record,
)
from T01_tools import get_first_false_task, get_markdown_content, is_task_progress_json, normalize_review_status_payload
from T09_terminal_ops import BridgePromptRequest, BridgeTerminalUI, use_terminal_ui
from T12_requirements_common import (
    build_requirements_clarification_paths,
    list_existing_requirements,
    sanitize_requirement_name,
)
from T10_tui_protocol import (
    PROTOCOL_VERSION,
    build_event,
    build_response,
    decode_message,
    encode_message,
)
from U01_common_config import SYSTEM_PYTHON_PATH


class PromptBroker:
    def __init__(
        self,
        emit_event: Callable[[str, dict[str, Any]], None],
        *,
        on_prompt_open: Callable[[str, BridgePromptRequest], Mapping[str, Any] | None] | None = None,
        on_prompt_resolved: Callable[[str, Mapping[str, Any] | None], None] | None = None,
    ) -> None:
        self._emit_event = emit_event
        self._on_prompt_open = on_prompt_open
        self._on_prompt_resolved = on_prompt_resolved
        self._pending: dict[str, queue.Queue[dict[str, Any]]] = {}
        self._claimed_prompts: set[str] = set()
        self._lock = threading.Lock()
        self._prompt_seq = 0
        self._shutdown_reason = ""

    def _notify_prompt_resolved(self, prompt_id: str, payload: Mapping[str, Any]) -> None:
        if self._on_prompt_resolved is None:
            return
        # Scope-critical prompt state must be committed before the blocked workflow
        # receives the response and can advance to the next stage or exit.
        self._on_prompt_resolved(str(prompt_id).strip(), payload)

    def request(self, request: BridgePromptRequest) -> dict[str, Any]:
        prompt_queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
        with self._lock:
            if self._shutdown_reason:
                raise RuntimeShutdownRequested(self._shutdown_reason)
            self._prompt_seq += 1
            prompt_id = f"prompt_{threading.get_ident()}_{self._prompt_seq}"
            self._pending[prompt_id] = prompt_queue
        try:
            # Establish all server-side prompt/scope state before making the id
            # visible to a client that may respond on another thread immediately.
            open_metadata: dict[str, Any] = {}
            if self._on_prompt_open is not None:
                callback_result = self._on_prompt_open(prompt_id, request)
                if isinstance(callback_result, Mapping):
                    open_metadata = dict(callback_result)
            event_payload = {
                **request.payload,
                "id": prompt_id,
                "prompt_type": request.prompt_type,
                **open_metadata,
            }
            self._emit_event(
                "prompt.request",
                event_payload,
            )
        except BaseException:
            # Publishing failed before request() reached its blocking wait. Close
            # any state created by on_prompt_open and remove the broker entry so
            # the caller fails promptly instead of leaving a ghost prompt.
            try:
                self._notify_prompt_resolved(
                    prompt_id,
                    {"__prompt_broker_shutdown__": "prompt_publish_failed"},
                )
            except BaseException:
                pass
            with self._lock:
                self._pending.pop(prompt_id, None)
                self._claimed_prompts.discard(prompt_id)
            raise
        try:
            payload = prompt_queue.get()
            shutdown_reason = str(payload.get("__prompt_broker_shutdown__", "")).strip()
            if shutdown_reason:
                raise RuntimeShutdownRequested(shutdown_reason)
            return payload
        finally:
            with self._lock:
                self._pending.pop(prompt_id, None)
                self._claimed_prompts.discard(prompt_id)

    def resolve(self, prompt_id: str, payload: Mapping[str, Any] | None = None) -> bool:
        prompt_id_text = str(prompt_id).strip()
        with self._lock:
            prompt_queue = self._pending.get(prompt_id_text)
            if prompt_queue is not None and prompt_id_text in self._claimed_prompts:
                return False
            if prompt_queue is not None:
                self._claimed_prompts.add(prompt_id_text)
        if prompt_queue is None:
            raise KeyError(f"未找到待处理 prompt: {prompt_id}")
        resolved_payload = dict(payload or {})
        # Resolve scope/context before the blocked workflow can consume the
        # response, advance to another prompt, or leave the runner registry.
        try:
            self._notify_prompt_resolved(prompt_id_text, resolved_payload)
        except BaseException:
            # A failed scope commit must leave the prompt pending and retryable.
            # Only release our claim when this exact prompt is still registered.
            with self._lock:
                if self._pending.get(prompt_id_text) is prompt_queue:
                    self._claimed_prompts.discard(prompt_id_text)
            raise
        try:
            prompt_queue.put_nowait(resolved_payload)
        except queue.Full:
            return False
        return True

    def shutdown(self, reason: str = "TUI backend 已关闭，取消等待中的输入。") -> None:
        normalized_reason = str(reason or "").strip() or "TUI backend 已关闭，取消等待中的输入。"
        with self._lock:
            self._shutdown_reason = normalized_reason
            pending = list(self._pending.items())
        for prompt_id, prompt_queue in pending:
            payload = {"__prompt_broker_shutdown__": normalized_reason}
            try:
                prompt_queue.put_nowait(payload)
            except queue.Full:
                continue


@dataclass
class ControlSessionState:
    control_id: str
    center: AgentInitControlCenter
    final_result: Any | None = None
    transition_text: str = ""


@dataclass
class AppContext:
    project_dir: str = ""
    requirement_name: str = ""
    current_action: str = ""


@dataclass
class PendingPromptState:
    prompt_id: str
    prompt_type: str
    payload: dict[str, Any]
    created_at: str = ""
    owner_runner_id: str = ""


@dataclass
class ResolvedHitlState:
    question_path: str = ""
    question_summary: str = ""


class ShutdownPolicy(str, Enum):
    CLEANUP = "CLEANUP"


@dataclass
class RunnerExecutionState:
    runner_id: str
    action: str
    stage_seq: int
    project_dir: str
    requirement_name: str
    current_action: str = ""
    current_stage_seq: int = 0
    registered_project_dir: str = ""
    registered_requirement_name: str = ""
    generation_registered: bool = False
    terminal_source: str = ""
    terminal_at: str = ""


@dataclass
class AttentionState:
    prompt_id: str = ""
    reason: str = ""
    title: str = ""
    subtitle: str = ""
    body: str = ""
    started_at: str = ""
    last_notified_at: str = ""
    next_notify_at: str = ""
    active: bool = False
    suppressed_due_to_presence: bool = False
    suppressed_until: str = ""


@dataclass
class TuiPresenceState:
    last_seen_at: str = ""
    last_reason: str = ""
    active_until: str = ""


@dataclass
class AttentionHandle:
    state: AttentionState
    stop_event: threading.Event
    thread: threading.Thread | None = None
    logged_error: bool = False


STAGE_LABEL_BY_ACTION = {
    "control.b01.open": "路由初始化",
    "stage.a01.start": "路由初始化",
    "stage.a02.start": "需求录入",
    "stage.a03.start": "需求澄清",
    "stage.a04.start": "需求评审",
    "stage.a05.start": "详细设计",
    "stage.a06.start": "任务拆分",
    "stage.a07.start": "任务开发",
    "stage.a08.start": "复核",
}

STAGE_SNAPSHOT_BUILDERS: tuple[tuple[str, str], ...] = (
    ("routing", "_build_routing_snapshot"),
    ("requirements", "_build_requirements_snapshot"),
    ("review", "_build_review_snapshot"),
    ("design", "_build_design_snapshot"),
    ("task-split", "_build_task_split_snapshot"),
    ("development", "_build_development_snapshot"),
    ("overall-review", "_build_overall_review_snapshot"),
)

STAGE_ROUTE_BY_ACTION = {
    "control.b01.open": "routing",
    "stage.a01.start": "routing",
    "stage.a02.start": "requirements",
    "stage.a03.start": "requirements",
    "stage.a04.start": "review",
    "stage.a05.start": "design",
    "stage.a06.start": "task-split",
    "stage.a07.start": "development",
    "stage.a08.start": "overall-review",
}

WORKFLOW_STAGE_ACTION_ORDER = {
    action: index
    for index, action in enumerate(
        (
            "stage.a01.start",
            "stage.a02.start",
            "stage.a03.start",
            "stage.a04.start",
            "stage.a05.start",
            "stage.a06.start",
            "stage.a07.start",
            "stage.a08.start",
        ),
        start=1,
    )
}
STAGE_AUDIT_CODE_BY_ACTION = {
    "stage.a03.start": "A03",
    "stage.a04.start": "A04",
    "stage.a05.start": "A05",
    "stage.a06.start": "A06",
    "stage.a07.start": "A07",
    "stage.a08.start": "A08",
}
STAGE_EVENT_LOG_SUFFIX = "".join(("流", "水", "记", "录"))
STAGE_EVENT_ORDER_KEY = "_".join(("record", "index"))
RUNNER_DEDUP_ACTIONS = frozenset((*WORKFLOW_STAGE_ACTION_ORDER, "workflow.a00.start"))
REQUIREMENT_CONCURRENCY_CONFLICT_MARKER = "并发冲突：同项目同需求已有运行中任务"

LEGACY_REQUIREMENTS_RUNTIME_ROOT_NAME = ".requirements_analysis_runtime"
WORKFLOW_RECORD_ROOT_NAME = ".tmux_workflow"
WEB_FILE_PREVIEW_MAX_BYTES = 256 * 1024
STALE_MISSING_SESSION_LIVE_EVIDENCE_TTL_SEC = 300


class BridgeLogSink:
    def __init__(self, emit_event: Callable[[str, dict[str, Any]], None]) -> None:
        self._emit_event = emit_event
        self._buffer = ""
        self._lock = threading.Lock()
        self.encoding = "utf-8"
        self.errors = "strict"

    def write(self, data: object) -> int:
        text = str(data)
        if not text:
            return 0
        with self._lock:
            self._buffer += text
            while True:
                index = self._buffer.find("\n")
                if index < 0:
                    break
                chunk = self._buffer[: index + 1]
                self._buffer = self._buffer[index + 1 :]
                if chunk:
                    self._emit_event("log.append", {"text": chunk})
        return len(text)

    def flush(self) -> None:
        with self._lock:
            if not self._buffer:
                return
            chunk = self._buffer
            self._buffer = ""
        self._emit_event("log.append", {"text": chunk})

    def isatty(self) -> bool:
        return False


def _extract_hitl_round(text: object) -> int | None:
    candidate = str(text or "").strip()
    if not candidate:
        return None
    match = re.search(r"HITL\s*第\s*(\d+)\s*轮", candidate, re.IGNORECASE)
    if not match:
        return None
    try:
        value = int(match.group(1))
    except ValueError:
        return None
    return value if value > 0 else None


def _prompt_is_hitl(payload: Mapping[str, Any] | None) -> bool:
    if not isinstance(payload, Mapping):
        return False
    explicit = payload.get("is_hitl", payload.get("isHitl"))
    if explicit is not None:
        return bool(explicit)
    marker = " ".join(
        [
            str(payload.get("title", "")),
            str(payload.get("prompt_text", "")),
            str(payload.get("question_path", "")),
        ]
    ).strip()
    return "hitl" in marker.lower()


def _prompt_attach_command(payload: Mapping[str, Any] | None) -> str:
    if not isinstance(payload, Mapping):
        return ""
    raw_attach = payload.get("attach_command", payload.get("attachCommand", ""))
    if isinstance(raw_attach, Sequence) and not isinstance(raw_attach, (str, bytes, bytearray)):
        attach = " ".join(str(item) for item in raw_attach).strip()
    else:
        attach = str(raw_attach or "").strip()
    if attach:
        return attach
    session_name = str(payload.get("session_name", payload.get("sessionName", "")) or "").strip()
    return f"tmux attach -t {session_name}" if session_name else ""


def _prompt_requires_attention(prompt_type: str, payload: Mapping[str, Any] | None) -> bool:
    if not isinstance(payload, Mapping):
        payload = {}
    explicit = payload.get("requires_attention", payload.get("requiresAttention"))
    if explicit is not None:
        return bool(explicit)
    normalized_type = str(prompt_type or "").strip().lower()
    return _prompt_is_hitl(payload) or normalized_type in {"select", "confirm", "text", "multiline"}


def _prompt_attention_reason(prompt_type: str, payload: Mapping[str, Any] | None) -> str:
    if _prompt_is_hitl(payload):
        return "hitl"
    if not isinstance(payload, Mapping):
        payload = {}
    explicit = str(payload.get("attention_reason", payload.get("attentionReason", "")) or "").strip().lower()
    if explicit:
        return explicit
    normalized_type = str(prompt_type or "").strip().lower()
    return normalized_type or "prompt"


def _short_attention_text(value: object, *, fallback: str, max_chars: int = 72) -> str:
    text = " ".join(str(value or "").strip().split())
    if not text:
        return fallback
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3] + "..."


def _build_attention_body(prompt_type: str, payload: Mapping[str, Any] | None) -> str:
    if not isinstance(payload, Mapping):
        payload = {}
    if _prompt_is_hitl(payload):
        return "HITL 待处理"
    normalized_type = str(prompt_type or "").strip().lower()
    fallback = {
        "select": "请选择",
        "confirm": "请确认",
        "text": "请继续输入",
        "multiline": "请继续输入",
    }.get(normalized_type, "待处理人工输入")
    title = payload.get("title") or payload.get("prompt_text") or ""
    return _short_attention_text(title, fallback=fallback)


def _iso_now() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def _iso_after_seconds(seconds: float) -> str:
    return (dt.datetime.now().astimezone() + dt.timedelta(seconds=max(float(seconds), 0.0))).isoformat(timespec="seconds")


def _stage_record_action_fragment(action: str) -> str:
    text = str(action or "").strip() or "unknown"
    return re.sub(r"[^0-9A-Za-z._-]+", "_", text).replace(".", "_").strip("._") or "unknown"


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary_path.open("w", encoding="utf-8") as handle:
            json.dump(dict(payload), handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temporary_path.unlink()


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary_path.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temporary_path.unlink()


def _write_project_stage_failure_record(
    *,
    project_dir: str,
    requirement_name: str,
    action: str,
    error: BaseException,
    traceback_text: str,
    runner_id: str = "",
    stage_seq: int = 0,
    failure_kind: str = "runner_failure",
    orphaned_workers: Sequence[Mapping[str, Any]] = (),
) -> Path | None:
    project_text = str(project_dir or "").strip()
    if not project_text:
        return None
    try:
        project_root = Path(project_text).expanduser().resolve()
        safe_requirement = sanitize_requirement_name(requirement_name or "_global")
        record_dir = project_root / WORKFLOW_RECORD_ROOT_NAME / safe_requirement / "stages"
        record_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "status": "failed",
            "action": str(action or "").strip(),
            "project_dir": str(project_root),
            "requirement_name": str(requirement_name or "").strip(),
            "error": str(error),
            "traceback": str(traceback_text or ""),
            "runner_id": str(runner_id or "").strip(),
            "stage_seq": int(stage_seq or 0),
            "failure_kind": str(failure_kind or "runner_failure").strip() or "runner_failure",
            "orphaned_workers": [dict(item) for item in orphaned_workers],
            "updated_at": _iso_now(),
        }
        failure_path = record_dir / f"{_stage_record_action_fragment(action)}.failure.json"
        _atomic_write_json(failure_path, payload)
        latest_path = record_dir / "latest_failure.json"
        _atomic_write_json(latest_path, payload)
        return failure_path
    except Exception:
        return None


def _clear_project_stage_failure_record(
    *,
    record_dir: Path,
    action: str,
) -> None:
    action_text = str(action or "").strip()
    if not action_text:
        return
    latest_path = record_dir / "latest_failure.json"
    with contextlib.suppress(Exception):
        payload = json.loads(latest_path.read_text(encoding="utf-8"))
        if str(payload.get("action", "")).strip() == action_text:
            latest_path.unlink()


def _write_project_stage_state_record(
    *,
    project_dir: str,
    requirement_name: str,
    action: str,
    status: str,
    stage_seq: int,
    source: str,
    runner_id: str = "",
    failure_path: str = "",
    failure_kind: str = "",
    message: str = "",
    orphaned_workers: Sequence[Mapping[str, Any]] = (),
    activate_generation: bool = True,
) -> Path | None:
    project_text = str(project_dir or "").strip()
    action_text = str(action or "").strip()
    if not project_text or not action_text:
        return None
    normalized_source = str(source or "").strip() or "runtime_inference"
    if normalized_source not in {"runner_start", "runtime_inference", "runner_complete", "runner_failure"}:
        normalized_source = "runtime_inference"
    try:
        project_root = Path(project_text).expanduser().resolve()
        safe_requirement = sanitize_requirement_name(requirement_name or "_global")
        record_dir = project_root / WORKFLOW_RECORD_ROOT_NAME / safe_requirement / "stages"
        record_dir.mkdir(parents=True, exist_ok=True)
        state_path = record_dir / f"{_stage_record_action_fragment(action_text)}.state.json"
        if normalized_source == "runtime_inference":
            with contextlib.suppress(Exception):
                previous_payload = json.loads(state_path.read_text(encoding="utf-8"))
                if (
                    isinstance(previous_payload, Mapping)
                    and str(previous_payload.get("action", "")).strip() == action_text
                    and str(previous_payload.get("status", "")).strip()
                    in {"completed", "failed", "error", "superseded"}
                    and str(previous_payload.get("source", "")).strip()
                    in {"runner_complete", "runner_failure"}
                ):
                    return state_path
        payload = {
            "action": action_text,
            "status": str(status or "").strip() or "ready",
            "project_dir": str(project_root),
            "requirement_name": str(requirement_name or "").strip(),
            "stage_seq": int(stage_seq or 0),
            "runner_id": str(runner_id or "").strip(),
            "source": normalized_source,
            "updated_at": _iso_now(),
            "failure_path": str(Path(failure_path).expanduser().resolve()) if str(failure_path).strip() else "",
            "failure_kind": str(failure_kind or "").strip(),
            "message": str(message or "").strip(),
            "orphaned_workers": [dict(item) for item in orphaned_workers],
        }
        _atomic_write_json(state_path, payload)
        if activate_generation and action_text == "workflow.a00.start" and normalized_source == "runner_start":
            for stale_state_path in record_dir.glob("*.state.json"):
                if stale_state_path == state_path:
                    continue
                with contextlib.suppress(Exception):
                    stale_state_path.unlink()
            # A new root generation supersedes the active failure pointer, while
            # per-action failure records remain available as historical evidence.
            with contextlib.suppress(Exception):
                (record_dir / "latest_failure.json").unlink()
        if (
            activate_generation
            and str(status or "").strip() in {"running", "awaiting-input"}
            and normalized_source == "runner_start"
        ):
            # Any accepted runner generation supersedes the active failure
            # pointer for this scope. Keep the per-action failure file as
            # historical evidence.
            with contextlib.suppress(FileNotFoundError):
                (record_dir / "latest_failure.json").unlink()
        return state_path
    except Exception:
        return None


def _project_stage_record_dir(*, project_dir: str, requirement_name: str) -> Path:
    project_root = Path(str(project_dir or "").strip()).expanduser().resolve()
    safe_requirement = sanitize_requirement_name(requirement_name or "_global")
    return project_root / WORKFLOW_RECORD_ROOT_NAME / safe_requirement / "stages"


def _snapshot_stage_generation_files(record_dir: Path) -> dict[str, bytes]:
    if not record_dir.is_dir():
        return {}
    paths = list(record_dir.glob("*.state.json"))
    latest_failure_path = record_dir / "latest_failure.json"
    if latest_failure_path.is_file():
        paths.append(latest_failure_path)
    return {path.name: path.read_bytes() for path in paths}


def _restore_stage_generation_files(record_dir: Path, snapshot: Mapping[str, bytes]) -> None:
    current_paths = list(record_dir.glob("*.state.json")) if record_dir.is_dir() else []
    latest_failure_path = record_dir / "latest_failure.json"
    if latest_failure_path.exists():
        current_paths.append(latest_failure_path)
    for path in current_paths:
        if path.name not in snapshot:
            path.unlink()
    for name, payload in snapshot.items():
        _atomic_write_bytes(record_dir / name, bytes(payload))


def _activate_stage_generation_files(
    record_dir: Path,
    *,
    root_action: str,
    current_action: str,
) -> None:
    keep_state_names = {
        f"{_stage_record_action_fragment(root_action)}.state.json",
        f"{_stage_record_action_fragment(current_action)}.state.json",
    }
    if str(root_action or "").strip() == "workflow.a00.start":
        for state_path in record_dir.glob("*.state.json"):
            if state_path.name not in keep_state_names:
                state_path.unlink()
    with contextlib.suppress(FileNotFoundError):
        (record_dir / "latest_failure.json").unlink()


def _read_project_stage_state_record(
    *,
    project_dir: str,
    requirement_name: str,
    action: str,
) -> dict[str, Any] | None:
    project_text = str(project_dir or "").strip()
    action_text = str(action or "").strip()
    if not project_text or not action_text:
        return None
    try:
        project_root = Path(project_text).expanduser().resolve()
        safe_requirement = sanitize_requirement_name(requirement_name or "_global")
        state_path = (
            project_root
            / WORKFLOW_RECORD_ROOT_NAME
            / safe_requirement
            / "stages"
            / f"{_stage_record_action_fragment(action_text)}.state.json"
        )
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(payload, Mapping):
        return None
    if str(payload.get("action", "")).strip() != action_text:
        return None
    return dict(payload)


def _max_project_stage_seq(*, project_dir: str, requirement_name: str) -> int:
    project_text = str(project_dir or "").strip()
    if not project_text:
        return 0
    try:
        record_dir = (
            Path(project_text).expanduser().resolve()
            / WORKFLOW_RECORD_ROOT_NAME
            / sanitize_requirement_name(requirement_name or "_global")
            / "stages"
        )
    except Exception:
        return 0
    maximum = 0
    record_paths = (
        tuple(record_dir.glob("*.state.json"))
        + tuple(record_dir.glob("*.failure.json"))
        + ((record_dir / "latest_failure.json",) if record_dir.is_dir() else ())
        if record_dir.is_dir()
        else ()
    )
    for record_path in record_paths:
        payload = _safe_json_read(record_path)
        try:
            maximum = max(maximum, int(payload.get("stage_seq") or 0))
        except Exception:
            continue
    return maximum


def _stage_hitl_audit_answered_state(
    *,
    project_dir: str,
    requirement_name: str,
    action: str,
    question_path: str | Path,
) -> str:
    stage_code = STAGE_AUDIT_CODE_BY_ACTION.get(str(action or "").strip(), "")
    project_text = str(project_dir or "").strip()
    requirement_text = str(requirement_name or "").strip()
    if not stage_code or not project_text or not requirement_text:
        return ""
    try:
        project_root = Path(project_text).expanduser().resolve()
        safe_requirement = sanitize_requirement_name(requirement_text)
        audit_path = project_root / f"{safe_requirement}_{stage_code}_{STAGE_EVENT_LOG_SUFFIX}.jsonl"
    except Exception:
        return ""
    if not audit_path.exists() or not audit_path.is_file():
        return ""
    question_text = str(Path(question_path).expanduser().resolve())
    latest_question_index = 0
    latest_answer_index = 0
    try:
        lines = audit_path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return ""
    for fallback_index, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except Exception:
            continue
        if not isinstance(payload, Mapping):
            continue
        event_type = str(payload.get("event_type", "")).strip()
        try:
            entry_index = int(payload.get(STAGE_EVENT_ORDER_KEY) or fallback_index)
        except Exception:
            entry_index = fallback_index
        source_paths = payload.get("source_paths", {})
        ask_human_path = ""
        if isinstance(source_paths, Mapping):
            ask_human_path = str(source_paths.get("ask_human", "") or "").strip()
        if event_type == "hitl_question":
            if not ask_human_path:
                continue
            with contextlib.suppress(Exception):
                ask_human_path = str(Path(ask_human_path).expanduser().resolve())
            if ask_human_path == question_text and entry_index >= latest_question_index:
                latest_question_index = entry_index
                latest_answer_index = 0
        elif event_type == "hitl_answer" and latest_question_index and entry_index >= latest_question_index:
            latest_answer_index = max(latest_answer_index, entry_index)
    if latest_answer_index >= latest_question_index and latest_question_index:
        return "answered"
    if latest_question_index:
        return "unanswered"
    return ""


def _workflow_stage_order(action: str) -> int:
    return WORKFLOW_STAGE_ACTION_ORDER.get(str(action or "").strip(), 0)


def _osascript_quote(value: str) -> str:
    return str(value or "").replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ").strip()


class HumanAttentionManager:
    def __init__(
        self,
        *,
        adapter_name_provider: Callable[[], str],
        on_state_change: Callable[[], None] | None = None,
        emit_log: Callable[[str], None] | None = None,
        interval_sec: float = 60.0,
        presence_provider: Callable[[], Mapping[str, Any]] | None = None,
        presence_ttl_sec: float = 15.0,
        platform_name: str | None = None,
        osascript_path: str | None = None,
        notifier: Callable[[str, str, str], str | None] | None = None,
    ) -> None:
        self._adapter_name_provider = adapter_name_provider
        self._on_state_change = on_state_change
        self._emit_log = emit_log
        self._interval_sec = max(float(interval_sec), 0.01)
        self._presence_provider = presence_provider
        self._presence_ttl_sec = max(float(presence_ttl_sec), 0.01)
        self._platform_name = str(platform_name or sys.platform).strip().lower()
        self._osascript_path = str(osascript_path or shutil.which("osascript") or "").strip()
        self._notifier = notifier or self._display_notification
        self._lock = threading.Lock()
        self._handles: dict[str, AttentionHandle] = {}

    def _supported(self) -> bool:
        return self._platform_name == "darwin" and self._adapter_name_provider().strip().lower() == "tui" and bool(self._osascript_path)

    def start_prompt(
        self,
        *,
        prompt_id: str,
        prompt_type: str,
        payload: Mapping[str, Any] | None,
        stage_label: str,
    ) -> None:
        if not _prompt_requires_attention(prompt_type, payload):
            self.resolve_prompt(prompt_id)
            return
        if not self._supported():
            return
        prompt_id_text = str(prompt_id or "").strip()
        stage_label_text = _short_attention_text(stage_label, fallback="当前阶段", max_chars=48)
        old_handle: AttentionHandle | None = None
        handle = AttentionHandle(
            state=AttentionState(
                prompt_id=prompt_id_text,
                reason=_prompt_attention_reason(prompt_type, payload),
                title="TmuxCodingTeam 需要人工介入",
                subtitle=stage_label_text,
                body=_build_attention_body(prompt_type, payload),
                started_at=_iso_now(),
                active=True,
            ),
            stop_event=threading.Event(),
        )
        handle.thread = threading.Thread(
            target=self._run_loop,
            args=(prompt_id_text, handle.stop_event),
            name=f"human-attention-{prompt_id_text or 'prompt'}",
            daemon=True,
        )
        with self._lock:
            old_handle = self._handles.get(prompt_id_text)
            self._handles[prompt_id_text] = handle
        if old_handle is not None:
            old_handle.stop_event.set()
            if old_handle.thread is not None and old_handle.thread.is_alive():
                old_handle.thread.join(timeout=0.2)
        handle.thread.start()
        if self._on_state_change is not None:
            self._on_state_change()

    def resolve_prompt(self, prompt_id: str) -> None:
        prompt_id_text = str(prompt_id or "").strip()
        handles: list[AttentionHandle] = []
        with self._lock:
            if prompt_id_text:
                handle = self._handles.pop(prompt_id_text, None)
                if handle is not None:
                    handles.append(handle)
            else:
                handles = list(self._handles.values())
                self._handles.clear()
        if not handles:
            return
        for handle in handles:
            handle.stop_event.set()
        for handle in handles:
            if handle.thread is not None and handle.thread.is_alive() and handle.thread is not threading.current_thread():
                handle.thread.join(timeout=0.2)
        if self._on_state_change is not None:
            self._on_state_change()

    def shutdown(self) -> None:
        self.resolve_prompt("")

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            active_handles = [item for item in self._handles.values() if item.state.active]
            if not active_handles:
                return {
                    "pending": False,
                    "reason": "",
                    "title": "",
                    "body": "",
                    "started_at": "",
                    "last_notified_at": "",
                    "next_notify_at": "",
                    "suppressed_due_to_presence": False,
                    "suppressed_until": "",
                    "active": False,
                }
            alerting_handles = [item for item in active_handles if not item.state.suppressed_due_to_presence]
            latest = max((alerting_handles or active_handles), key=lambda item: item.state.started_at)
            state = AttentionState(**asdict(latest.state))
        return {
            "pending": bool(alerting_handles),
            "reason": state.reason,
            "title": state.title,
            "body": state.body,
            "started_at": state.started_at,
            "last_notified_at": state.last_notified_at,
            "next_notify_at": state.next_notify_at,
            "suppressed_due_to_presence": bool(state.suppressed_due_to_presence),
            "suppressed_until": state.suppressed_until,
            "active": bool(state.active),
        }

    def _run_loop(self, prompt_id: str, stop_event: threading.Event) -> None:
        next_wait = 0.0
        while not stop_event.wait(max(next_wait, 0.0)):
            next_wait = self._tick_prompt(prompt_id)

    def _presence_snapshot(self) -> dict[str, Any]:
        if self._presence_provider is None:
            return {"recent": False, "active_until": "", "delay_sec": 0.0}
        try:
            snapshot = dict(self._presence_provider() or {})
        except Exception:  # noqa: BLE001
            return {"recent": False, "active_until": "", "delay_sec": 0.0}
        delay = snapshot.get("delay_sec", 0.0)
        try:
            delay_sec = max(float(delay), 0.0)
        except Exception:
            delay_sec = 0.0
        return {
            "recent": bool(snapshot.get("recent", False)),
            "active_until": str(snapshot.get("active_until", "")).strip(),
            "delay_sec": delay_sec,
        }

    def _tick_prompt(self, prompt_id: str) -> float:
        with self._lock:
            handle = self._handles.get(prompt_id)
            if handle is None or not handle.state.active:
                return self._interval_sec
            title = handle.state.title
            subtitle = handle.state.subtitle
            body = handle.state.body
        presence = self._presence_snapshot()
        if presence["recent"]:
            should_emit = False
            with self._lock:
                handle = self._handles.get(prompt_id)
                if handle is None or not handle.state.active:
                    return self._interval_sec
                previous_suppressed = bool(handle.state.suppressed_due_to_presence)
                previous_until = str(handle.state.suppressed_until or "").strip()
                handle.state.suppressed_due_to_presence = True
                handle.state.suppressed_until = str(presence["active_until"]).strip()
                handle.state.next_notify_at = handle.state.suppressed_until
                if not previous_suppressed or previous_until != handle.state.suppressed_until:
                    should_emit = True
            if should_emit and self._on_state_change is not None:
                self._on_state_change()
            return max(min(float(presence["delay_sec"]), self._interval_sec), 0.05)
        error_text = self._notifier(title, subtitle, body)
        now_iso = _iso_now()
        next_iso = _iso_after_seconds(self._interval_sec)
        should_log_error = False
        should_emit = False
        with self._lock:
            handle = self._handles.get(prompt_id)
            if handle is None or not handle.state.active:
                return self._interval_sec
            if handle.state.suppressed_due_to_presence or handle.state.suppressed_until:
                should_emit = True
            handle.state.suppressed_due_to_presence = False
            handle.state.suppressed_until = ""
            handle.state.last_notified_at = now_iso
            handle.state.next_notify_at = next_iso
            if error_text and not handle.logged_error:
                handle.logged_error = True
                should_log_error = True
            if not error_text:
                handle.logged_error = False
        if should_emit and self._on_state_change is not None:
            self._on_state_change()
        if should_log_error and self._emit_log is not None:
            self._emit_log(f"macOS 通知发送失败: {error_text}\n")
        return self._interval_sec

    def _display_notification(self, title: str, subtitle: str, body: str) -> str | None:
        if not self._supported():
            return None
        command_text = f'display notification "{_osascript_quote(body)}" with title "{_osascript_quote(title)}"'
        if subtitle:
            command_text += f' subtitle "{_osascript_quote(subtitle)}"'
        command_text += ' sound name "Glass"'
        try:
            completed = subprocess.run(
                [self._osascript_path, "-e", command_text],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                timeout=5,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return "timeout"
        if completed.returncode == 0:
            return None
        return (completed.stderr or completed.stdout or f"exit_code={completed.returncode}").strip()


def _serialize(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _serialize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_serialize(item) for item in value]
    if is_dataclass(value):
        return _serialize(asdict(value))
    if isinstance(value, TmuxBatchWorker):
        return value.runtime_metadata()
    if hasattr(value, "runtime_metadata") and callable(value.runtime_metadata):
        try:
            return _serialize(value.runtime_metadata())
        except Exception:  # noqa: BLE001
            return {"repr": repr(value)}
    return {"repr": repr(value)}


def _safe_json_read(path_value: str | Path) -> dict[str, Any]:
    path = Path(path_value).expanduser().resolve()
    if not path.exists() or not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    return payload if isinstance(payload, dict) else {}


def _collect_paths(node: object) -> list[str]:
    flattened: list[str] = []
    if isinstance(node, dict):
        for value in node.values():
            flattened.extend(_collect_paths(value))
        return flattened
    if isinstance(node, (list, tuple, set)):
        for value in node:
            flattened.extend(_collect_paths(value))
        return flattened
    if node is None:
        return flattened
    text = str(node).strip()
    if text:
        flattened.append(text)
    return flattened


def _iso_from_path(path_value: str | Path) -> str:
    path = Path(path_value).expanduser().resolve()
    if not path.exists():
        return ""
    return dt.datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat(timespec="microseconds")


def _preview_text(path_value: str | Path, *, max_lines: int = 3, max_chars: int = 240) -> str:
    path = Path(path_value).expanduser().resolve()
    if not path.exists() or not path.is_file():
        return ""
    try:
        text = path.read_text(encoding="utf-8").strip()
    except Exception:  # noqa: BLE001
        return ""
    if not text:
        return ""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    preview = " | ".join(lines[:max_lines])
    if len(preview) > max_chars:
        return preview[: max_chars - 3] + "..."
    return preview


def _preview_path_text(path_value: str | Path, *, max_bytes: int = WEB_FILE_PREVIEW_MAX_BYTES) -> dict[str, Any]:
    path = Path(path_value).expanduser().resolve()
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(f"文件不存在: {path}")
    stat = path.stat()
    byte_limit = max(1, min(int(max_bytes or WEB_FILE_PREVIEW_MAX_BYTES), WEB_FILE_PREVIEW_MAX_BYTES))
    with path.open("rb") as file:
        data = file.read(byte_limit + 1)
    truncated = len(data) > byte_limit
    payload = data[:byte_limit]
    if b"\x00" in payload:
        raise ValueError(f"文件不是可预览文本: {path}")
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError(f"文件不是 UTF-8 文本: {path}") from error
    return {
        "path": str(path),
        "size": int(stat.st_size),
        "updated_at": _iso_from_path(path),
        "truncated": bool(truncated or stat.st_size > byte_limit),
        "text": text,
    }


def _build_file_snapshot(path_value: str | Path, *, label: str = "") -> dict[str, Any]:
    path = Path(path_value).expanduser().resolve()
    exists = path.exists() and path.is_file()
    return {
        "label": label or path.name,
        "path": str(path),
        "exists": exists,
        "updated_at": _iso_from_path(path) if exists else "",
        "summary": _preview_text(path) if exists else "",
    }


def _build_task_progress_snapshot(task_json_path: str | Path) -> dict[str, Any]:
    path = Path(task_json_path).expanduser().resolve()
    if not is_task_progress_json(path):
        return {
            "milestones": [],
            "current_milestone_key": "",
            "all_tasks_completed": False,
        }
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {
            "milestones": [],
            "current_milestone_key": "",
            "all_tasks_completed": False,
        }
    if not isinstance(payload, dict):
        return {
            "milestones": [],
            "current_milestone_key": "",
            "all_tasks_completed": False,
        }
    current_task_key = get_first_false_task(path)
    current_milestone_key = ""
    milestones: list[dict[str, Any]] = []
    all_tasks_completed = True
    for raw_milestone_key, raw_tasks in payload.items():
        milestone_key = str(raw_milestone_key).strip()
        if not isinstance(raw_tasks, dict):
            continue
        milestone_tasks: list[dict[str, Any]] = []
        milestone_completed = True
        for raw_task_key, raw_completed in raw_tasks.items():
            task_key = str(raw_task_key).strip()
            task_completed = bool(raw_completed)
            if not task_completed:
                all_tasks_completed = False
                milestone_completed = False
                if not current_milestone_key and task_key == current_task_key:
                    current_milestone_key = milestone_key
            milestone_tasks.append(
                {
                    "key": task_key,
                    "completed": task_completed,
                }
            )
        milestones.append(
            {
                "key": milestone_key,
                "completed": milestone_completed,
                "tasks": milestone_tasks,
            }
        )
    return {
        "milestones": milestones,
        "current_milestone_key": "" if all_tasks_completed else current_milestone_key,
        "all_tasks_completed": all_tasks_completed,
    }


def _read_turn_bundle(turn_status_path: str | Path) -> dict[str, Any]:
    payload = _safe_json_read(turn_status_path)
    artifacts = payload.get("artifacts", {}) if isinstance(payload, dict) else {}
    stage_status_path = str(artifacts.get("stage_status", "")).strip() if isinstance(artifacts, dict) else ""
    stage_payload = _safe_json_read(stage_status_path) if stage_status_path else {}
    artifact_paths = [str(Path(item).expanduser().resolve()) for item in _collect_paths(artifacts)]
    if stage_payload:
        for key in ("output_path", "question_path", "record_path"):
            candidate = str(stage_payload.get(key, "")).strip()
            if candidate:
                artifact_paths.append(str(Path(candidate).expanduser().resolve()))
    deduped: list[str] = []
    for item in artifact_paths:
        if item and item not in deduped and Path(item).exists():
            deduped.append(item)
    return {
        "artifact_paths": deduped,
        "question_path": str(stage_payload.get("question_path", artifacts.get("question", ""))).strip() if isinstance(artifacts, dict) else "",
        "answer_path": str(stage_payload.get("record_path", artifacts.get("record", ""))).strip() if isinstance(artifacts, dict) else "",
    }


def _read_task_result_bundle(result_path: str | Path) -> dict[str, Any]:
    payload = _safe_json_read(result_path)
    artifacts = payload.get("artifacts", {}) if isinstance(payload, dict) else {}
    artifact_paths = [str(Path(item).expanduser().resolve()) for item in _collect_paths(artifacts) if Path(item).exists()]
    deduped: list[str] = []
    for item in artifact_paths:
        if item and item not in deduped:
            deduped.append(item)
    return {
        "artifact_paths": deduped,
        "question_path": "",
        "answer_path": "",
    }


def _file_has_content(path_value: str | Path) -> bool:
    path = Path(path_value).expanduser().resolve()
    if not path.exists() or not path.is_file():
        return False
    try:
        return bool(path.read_text(encoding="utf-8").strip())
    except Exception:  # noqa: BLE001
        return False


_WORKER_AGENT_STATE_VALUES = {
    "DEAD",
    "STARTING",
    "READY",
    "BUSY",
}
_WORKER_RUNTIME_ACTIVE_STATUSES = {"running", "pending"}
_WORKER_RESULT_ACTIVE_STATUSES = {"running", "pending", "submitted", "submitting"}
_WORKER_RUNTIME_TERMINAL_STATUSES = {"done", "succeeded", "completed"}
_WORKER_RESULT_COMPLETED_STATUSES = {
    "done",
    "succeeded",
    "completed",
    "ready",
    "idle",
}
_WORKER_RESULT_FAILED_STATUSES = {
    "failed",
    "stale_failed",
    "error",
}
_WORKER_RESULT_TERMINAL_STATUSES = _WORKER_RESULT_COMPLETED_STATUSES | _WORKER_RESULT_FAILED_STATUSES
_RECOVERABLE_RECONFIG_HEALTH_STATUSES = {"awaiting_reconfig", "recoverable_startup_failure"}

def _is_recoverable_reconfig_snapshot(snapshot: Mapping[str, Any]) -> bool:
    if _snapshot_has_stale_ready_reconfig_marker(snapshot):
        return False
    health_status = str(snapshot.get("health_status", "")).strip().lower()
    note = str(snapshot.get("note", "")).strip().lower()
    return health_status in _RECOVERABLE_RECONFIG_HEALTH_STATUSES or note == "awaiting_reconfig"


def _recoverable_reconfig_snapshot_has_live_evidence(snapshot: Mapping[str, Any]) -> bool:
    if not _is_recoverable_reconfig_snapshot(snapshot):
        return False
    agent_state = str(snapshot.get("agent_state", "") or snapshot.get("agentState", "")).strip().upper()
    health_status = str(snapshot.get("health_status", "") or snapshot.get("healthStatus", "")).strip().lower()
    if agent_state == "DEAD" or health_status in {"dead", "missing_session", "pane_dead"}:
        return False
    if worker_state_is_prelaunch_active(snapshot):
        return True
    return bool(snapshot.get("session_exists") or snapshot.get("sessionExists"))


def _snapshot_status_triplet(snapshot: Mapping[str, Any]) -> tuple[str, str, str]:
    status = str(snapshot.get("status", "")).strip().lower()
    result_status = str(snapshot.get("result_status", "") or snapshot.get("resultStatus", "")).strip().lower()
    runtime_status = str(
        snapshot.get("current_task_runtime_status", "") or snapshot.get("currentTaskRuntimeStatus", "") or ""
    ).strip().lower()
    return status, result_status, runtime_status


def _snapshot_has_stale_ready_reconfig_marker(snapshot: Mapping[str, Any]) -> bool:
    note = str(snapshot.get("note", "") or "").strip().lower()
    health_status = str(snapshot.get("health_status", "") or snapshot.get("healthStatus", "") or "").strip().lower()
    if note != "awaiting_reconfig" and health_status not in _RECOVERABLE_RECONFIG_HEALTH_STATUSES:
        return False
    agent_state = str(snapshot.get("agent_state", "") or snapshot.get("agentState", "") or "").strip().upper()
    if agent_state != "READY":
        return False
    if health_status not in {"", "alive", *_RECOVERABLE_RECONFIG_HEALTH_STATUSES}:
        return False
    runtime_status = str(
        snapshot.get("current_task_runtime_status", "") or snapshot.get("currentTaskRuntimeStatus", "") or ""
    ).strip().lower()
    dispatch_state = str(snapshot.get("dispatch_state", "") or snapshot.get("dispatchState", "") or "").strip().lower()
    return runtime_status != "running" and dispatch_state not in {"submitting", "submitted", "running"}


def _worker_snapshot_has_terminal_status(snapshot: Mapping[str, Any]) -> bool:
    if _is_recoverable_reconfig_snapshot(snapshot) or worker_state_is_prelaunch_active(snapshot):
        return False
    status, result_status, runtime_status = _snapshot_status_triplet(snapshot)
    return (
        runtime_status in _WORKER_RUNTIME_TERMINAL_STATUSES
        or result_status in _WORKER_RESULT_TERMINAL_STATUSES
        or status in _WORKER_RESULT_TERMINAL_STATUSES
    )


def _worker_snapshot_has_completed_status(snapshot: Mapping[str, Any]) -> bool:
    if _is_recoverable_reconfig_snapshot(snapshot) or worker_state_is_prelaunch_active(snapshot):
        return False
    status, result_status, runtime_status = _snapshot_status_triplet(snapshot)
    return (
        runtime_status in _WORKER_RUNTIME_TERMINAL_STATUSES
        or result_status in _WORKER_RESULT_COMPLETED_STATUSES
        or status in _WORKER_RESULT_COMPLETED_STATUSES
    )


def _normalize_worker_agent_state(snapshot: Mapping[str, Any]) -> str:
    candidate = str(snapshot.get("agent_state", "")).strip().upper()
    if candidate in _WORKER_AGENT_STATE_VALUES:
        return candidate
    return ""


def _normalize_worker_session_state(
    *,
    session_name: str,
    session_exists: bool,
    status: str,
    agent_state: str,
    agent_started: bool = False,
    pane_id: str = "",
    workflow_stage: str = "",
    note: str = "",
    health_status: str,
    health_note: str,
) -> tuple[str, str, str]:
    normalized_agent_state = str(agent_state or "").strip().upper()
    normalized_health_status = str(health_status or "").strip()
    normalized_health_note = str(health_note or "").strip()
    normalized_status = str(status or "").strip()
    normalized_workflow_stage = str(workflow_stage or "").strip()
    normalized_note = str(note or "").strip()
    if (
        session_name
        and not session_exists
        and normalized_status in {"ready", "running", "pending"}
    ):
        if worker_state_is_prelaunch_active(
            {
                "agent_state": normalized_agent_state,
                "agent_started": agent_started,
                "health_note": normalized_health_note,
                "health_status": normalized_health_status,
                "note": normalized_note,
                "pane_id": pane_id,
                "result_status": normalized_status,
                "status": normalized_status,
                "workflow_stage": normalized_workflow_stage,
            }
        ):
            normalized_agent_state = "STARTING"
            if normalized_health_status in {"", "alive", "dead", "missing_session", "pane_dead"}:
                normalized_health_status = "unknown"
            if not normalized_health_note or normalized_health_note in {"alive", "tmux session missing", "missing_session", "pane_dead"}:
                normalized_health_note = "launch pending"
            return normalized_agent_state, normalized_health_status, normalized_health_note
        normalized_agent_state = "DEAD"
        if normalized_health_status in {"", "unknown", "alive"}:
            normalized_health_status = "dead"
        if not normalized_health_note:
            normalized_health_note = "tmux session missing"
    return normalized_agent_state, normalized_health_status, normalized_health_note


def _parse_iso_datetime(value: str) -> dt.datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    with contextlib.suppress(ValueError):
        return dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    return None


def _worker_status_sort_rank(status: object) -> int:
    normalized = str(status or "").strip()
    if normalized in {"failed", "stale_failed", "error"}:
        return 3
    if normalized in {"running", "pending"}:
        return 2
    if normalized in {"succeeded", "completed"}:
        return 1
    return 0


def _worker_snapshot_sort_key(snapshot: Mapping[str, Any]) -> tuple[float, float, int]:
    last_heartbeat = _parse_iso_datetime(str(snapshot.get("last_heartbeat_at", "")).strip())
    updated_at = _parse_iso_datetime(str(snapshot.get("updated_at", "")).strip())
    heartbeat_ts = last_heartbeat.timestamp() if last_heartbeat is not None else 0.0
    updated_ts = updated_at.timestamp() if updated_at is not None else 0.0
    return (
        heartbeat_ts,
        updated_ts,
        _worker_status_sort_rank(snapshot.get("status")),
    )


def _worker_state_revision(snapshot: Mapping[str, Any]) -> int:
    with contextlib.suppress(TypeError, ValueError):
        return max(int(snapshot.get("state_revision", 0) or 0), 0)
    return 0


def _worker_snapshot_is_newer(candidate: Mapping[str, Any], previous: Mapping[str, Any]) -> bool:
    candidate_path = str(candidate.get("state_path", "") or "").strip()
    previous_path = str(previous.get("state_path", "") or "").strip()
    if candidate_path and candidate_path == previous_path:
        candidate_revision = _worker_state_revision(candidate)
        previous_revision = _worker_state_revision(previous)
        if candidate_revision != previous_revision:
            return candidate_revision > previous_revision
    return _worker_snapshot_sort_key(candidate) > _worker_snapshot_sort_key(previous)


def _worker_snapshot_latest_timestamp(snapshot: Mapping[str, Any]) -> float | None:
    timestamps: list[float] = []
    for field in ("last_heartbeat_at", "updated_at"):
        parsed = _parse_iso_datetime(str(snapshot.get(field, "")).strip())
        if parsed is not None:
            timestamps.append(parsed.timestamp())
    if not timestamps:
        return None
    return max(timestamps)


def _worker_snapshot_before_timestamp(snapshot: Mapping[str, Any], started_at: float) -> bool:
    latest_timestamp = _worker_snapshot_latest_timestamp(snapshot)
    return latest_timestamp is not None and latest_timestamp < started_at


def _worker_snapshot_has_active_turn_evidence(snapshot: Mapping[str, Any]) -> bool:
    if worker_state_is_prelaunch_active(snapshot):
        return True
    status, result_status, runtime_status = _snapshot_status_triplet(snapshot)
    if runtime_status in _WORKER_RUNTIME_ACTIVE_STATUSES:
        return True
    if status in _WORKER_RESULT_ACTIVE_STATUSES or result_status in _WORKER_RESULT_ACTIVE_STATUSES:
        return True
    if str(snapshot.get("dispatch_state", "")).strip().lower() in {"submitting", "submitted", "running"}:
        return True
    if str(snapshot.get("current_turn_phase", "")).strip() and str(snapshot.get("turn_status_path", "")).strip():
        return True
    return False


def _worker_snapshot_has_active_contract_marker(snapshot: Mapping[str, Any]) -> bool:
    if worker_state_is_prelaunch_active(snapshot):
        return True
    _status, _result_status, runtime_status = _snapshot_status_triplet(snapshot)
    if runtime_status in _WORKER_RUNTIME_ACTIVE_STATUSES:
        return True
    if str(snapshot.get("dispatch_state", "")).strip().lower() in {"submitting", "submitted", "running"}:
        return True
    if str(snapshot.get("current_turn_phase", "")).strip() and str(snapshot.get("turn_status_path", "")).strip():
        return True
    return False


def _worker_snapshot_has_stale_ready_task_result_contract(snapshot: Mapping[str, Any]) -> bool:
    note = str(snapshot.get("note", "") or "").strip()
    if not note.startswith("still_running:"):
        return False
    agent_state = str(snapshot.get("agent_state", "") or snapshot.get("agentState", "") or "").strip().upper()
    if agent_state != "READY":
        return False
    if str(snapshot.get("dispatch_state", "") or "").strip().lower() in {"submitting", "submitted", "running"}:
        return False
    task_status_path = str(snapshot.get("current_task_status_path", "") or snapshot.get("currentTaskStatusPath", "") or "").strip()
    result_path = str(snapshot.get("current_task_result_path", "") or snapshot.get("currentTaskResultPath", "") or "").strip()
    if not task_status_path or not result_path:
        return False
    try:
        task_status_payload = _safe_json_read(task_status_path)
    except Exception:
        task_status_payload = {}
    if task_status_payload != {"status": "running"}:
        return False
    try:
        if Path(result_path).expanduser().exists():
            return False
    except Exception:
        return False
    return True


def _normalize_stage_active_contract_worker_snapshot(snapshot: Mapping[str, Any], *, action: str) -> dict[str, Any]:
    _ = action
    # Agent state describes the currently observed terminal surface. An
    # unresolved result contract stays visible through turn/runtime fields;
    # it must never rewrite an observed READY terminal to BUSY.
    return dict(snapshot)


def _worker_snapshot_is_stale_missing_session_live_noise(
    snapshot: Mapping[str, Any],
    *,
    now_ts: float | None = None,
    ttl_sec: float = STALE_MISSING_SESSION_LIVE_EVIDENCE_TTL_SEC,
) -> bool:
    if bool(snapshot.get("session_exists")):
        return False
    health_status = str(snapshot.get("health_status", "") or snapshot.get("healthStatus", "")).strip().lower()
    if health_status not in {"alive", "observe_error", "provider_auth_error"}:
        return False
    status, result_status, _runtime_status = _snapshot_status_triplet(snapshot)
    if status in _WORKER_RESULT_FAILED_STATUSES or result_status in _WORKER_RESULT_FAILED_STATUSES:
        return False
    agent_state = str(snapshot.get("agent_state", "") or snapshot.get("agentState", "")).strip().upper()
    if agent_state in {"DEAD", "STARTING"}:
        return False
    if _worker_snapshot_has_active_turn_evidence(snapshot):
        return False
    latest_timestamp = _worker_snapshot_latest_timestamp(snapshot)
    if latest_timestamp is None:
        return False
    reference_ts = time.time() if now_ts is None else float(now_ts)
    return latest_timestamp <= reference_ts and (reference_ts - latest_timestamp) > float(ttl_sec)


def _merge_worker_snapshots(*collections: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    latest_by_session: dict[str, dict[str, Any]] = {}
    anonymous: list[dict[str, Any]] = []
    for collection in collections:
        for item in collection:
            snapshot = dict(item)
            session_name = str(snapshot.get("session_name", "")).strip()
            if not session_name:
                anonymous.append(snapshot)
                continue
            previous = latest_by_session.get(session_name)
            if previous is None or _worker_snapshot_is_newer(snapshot, previous):
                latest_by_session[session_name] = snapshot
    merged = [*latest_by_session.values(), *anonymous]
    return sorted(merged, key=_worker_snapshot_sort_key, reverse=True)


def _filter_worker_snapshots(
    workers: Sequence[Mapping[str, Any]],
    *,
    allowed_worker_ids: Sequence[str] = (),
    allowed_worker_id_prefixes: Sequence[str] = (),
    allowed_session_prefixes: Sequence[str] = (),
) -> list[dict[str, Any]]:
    allowed_ids = {str(item).strip().lower() for item in allowed_worker_ids if str(item).strip()}
    allowed_prefixes = tuple(str(item).strip().lower() for item in allowed_worker_id_prefixes if str(item).strip())
    session_prefixes = tuple(str(item).strip() for item in allowed_session_prefixes if str(item).strip())
    filtered: list[dict[str, Any]] = []
    for item in workers:
        snapshot = dict(item)
        worker_id = str(snapshot.get("worker_id", "")).strip().lower()
        session_name = str(snapshot.get("session_name", "")).strip()
        matched = False
        if worker_id:
            if worker_id in allowed_ids:
                matched = True
            elif allowed_prefixes and any(worker_id.startswith(prefix) for prefix in allowed_prefixes):
                matched = True
        if not matched and session_name and session_prefixes:
            matched = any(session_name.startswith(prefix) for prefix in session_prefixes)
        if matched:
            filtered.append(snapshot)
    return filtered


def _is_unscoped_dead_worker_snapshot(snapshot: Mapping[str, Any], *, context_known: bool) -> bool:
    if not context_known:
        return False
    if any(str(snapshot.get(field, "")).strip() for field in ("project_dir", "requirement_name", "workflow_action")):
        return False
    session_exists = snapshot.get("session_exists")
    agent_state = str(snapshot.get("agent_state", "")).strip().upper()
    if session_exists is not False and agent_state != "DEAD":
        return False
    turn_status_path = str(snapshot.get("turn_status_path", "")).strip()
    if not turn_status_path:
        return False
    return not Path(turn_status_path).exists()


def _state_indicates_active_agent_execution(state: Mapping[str, Any]) -> bool:
    return (
        _normalize_worker_agent_state(state) == "BUSY"
        or str(state.get("current_task_runtime_status", "")).strip().lower() == "running"
    )


def _worker_snapshot_is_actively_running(snapshot: Mapping[str, Any]) -> bool:
    if _is_recoverable_reconfig_snapshot(snapshot):
        return _recoverable_reconfig_snapshot_has_live_evidence(snapshot)
    if _snapshot_has_stale_ready_reconfig_marker(snapshot):
        return False
    if _worker_snapshot_has_stale_ready_task_result_contract(snapshot):
        return False
    if str(snapshot.get("health_status", "")).strip().lower() == "dead":
        return False
    agent_state = str(snapshot.get("agent_state", "") or snapshot.get("agentState", "")).strip().upper()
    if agent_state == "DEAD":
        return False
    if worker_state_is_prelaunch_active(snapshot):
        return True
    status, result_status, runtime_status = _snapshot_status_triplet(snapshot)
    if _worker_snapshot_has_completed_status(snapshot):
        return False
    if _worker_snapshot_has_active_contract_marker(snapshot):
        return True
    if runtime_status in _WORKER_RUNTIME_ACTIVE_STATUSES and agent_state != "READY":
        return True
    if _worker_snapshot_has_terminal_status(snapshot):
        return False
    if agent_state == "READY":
        return False
    if result_status in _WORKER_RESULT_ACTIVE_STATUSES or status in _WORKER_RESULT_ACTIVE_STATUSES:
        return True
    return agent_state in {"BUSY", "STARTING"}


def _normalize_routing_worker_snapshot(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    # Routing turn progress and terminal health are independent lifecycles.
    return dict(snapshot)


def _read_worker_state_snapshot(
    state_path: str | Path,
    *,
    session_exists_resolver: Callable[[str], bool] | None = None,
    session_context_resolver: Callable[[str, Mapping[str, Any], str | Path], bool] | None = None,
    state_identity_resolver: Callable[[Mapping[str, Any], str | Path], Mapping[str, Any]] | None = None,
    trust_persisted_session: bool = False,
) -> dict[str, Any]:
    state = _safe_json_read(state_path)
    if not state:
        return {}
    prelaunch_active = worker_state_is_prelaunch_active(state)
    recovered_identity: Mapping[str, Any] = {}
    active_statuses = {
        str(state.get(field_name, "") or "").strip().lower()
        for field_name in ("status", "result_status")
    }
    agent_state_for_identity = str(state.get("agent_state", "") or "").strip().upper()
    health_status_for_identity = str(state.get("health_status", "") or "").strip().lower()
    terminal_dead_or_failed = (
        bool(active_statuses & {"error", "failed", "stale_failed"})
        or agent_state_for_identity == "DEAD"
        or health_status_for_identity in {"dead", "missing_session", "pane_dead"}
    )
    should_recover_identity = (
        not terminal_dead_or_failed
        and not prelaunch_active
        and (
            _state_indicates_active_agent_execution(state)
            or bool(active_statuses & {"running", "pending"})
            or bool(state.get("agent_started") or state.get("agent_ready"))
            or health_status_for_identity == "alive"
        )
    )
    if state_identity_resolver is not None and should_recover_identity and not trust_persisted_session:
        with contextlib.suppress(Exception):
            recovered_identity = state_identity_resolver(state, state_path) or {}

    def _state_or_identity(field_name: str, default: Any = "") -> Any:
        value = state.get(field_name, "")
        if str(value or "").strip():
            return value
        return recovered_identity.get(field_name, default)

    turn_bundle = _read_turn_bundle(str(state.get("current_turn_status_path", "")))
    task_bundle = _read_task_result_bundle(str(state.get("current_task_result_path", "")))
    artifact_paths: list[str] = []
    for collection in (turn_bundle.get("artifact_paths", []), task_bundle.get("artifact_paths", [])):
        for item in collection:
            if item and item not in artifact_paths:
                artifact_paths.append(item)
    session_name = str(_state_or_identity("session_name")).strip()
    session_exists = bool(recovered_identity.get("session_exists", False))
    persisted_session_exists = state.get("session_exists")
    persisted_session_known = isinstance(persisted_session_exists, bool)
    if prelaunch_active:
        session_exists = False
    elif trust_persisted_session and session_name and not terminal_dead_or_failed:
        # Runtime state writes must reach the UI without waiting for N serial
        # tmux probes. A health/lifecycle write can, however, persist an
        # authoritative false value; stale alive evidence must not revive it.
        if persisted_session_known:
            session_exists = bool(persisted_session_exists)
        else:
            session_exists = bool(state.get("agent_alive", False)) or (
                health_status_for_identity == "alive"
                and agent_state_for_identity in {"STARTING", "READY", "BUSY"}
            )
    elif session_name and session_context_resolver is not None:
        with contextlib.suppress(Exception):
            session_exists = bool(session_context_resolver(session_name, state, state_path)) or session_exists
        if (
            not session_exists
            and session_exists_resolver is not None
            and _state_indicates_active_agent_execution(state)
        ):
            with contextlib.suppress(Exception):
                session_exists = bool(session_exists_resolver(session_name))
    elif session_name and session_exists_resolver is not None:
        with contextlib.suppress(Exception):
            session_exists = bool(session_exists_resolver(session_name))
    raw_status = str(state.get("status") or "").strip()
    raw_result_status = str(state.get("result_status") or "").strip()
    status = str(raw_result_status or raw_status or "pending").strip()
    health_status = str(state.get("health_status", "unknown")).strip()
    health_note = str(state.get("health_note", "")).strip()
    note = str(state.get("note", "")).strip()
    agent_started = bool(state.get("agent_started", state.get("agent_ready", False)))
    agent_state = _normalize_worker_agent_state(state)
    agent_state, health_status, health_note = _normalize_worker_session_state(
        session_name=session_name,
        session_exists=session_exists,
        status=status,
        agent_state=agent_state,
        agent_started=agent_started,
        pane_id=str(state.get("pane_id", "")).strip(),
        workflow_stage=str(state.get("workflow_stage", "")).strip(),
        note=note,
        health_status=health_status,
        health_note=health_note,
    )
    current_task_runtime_status = str(state.get("current_task_runtime_status", "")).strip()
    state_revision = _worker_state_revision(state)
    config_payload = state.get("config", {})
    if not isinstance(config_payload, Mapping):
        config_payload = {}
    return {
        "worker_id": str(
            state.get("worker_id")
            or state.get("raw_worker_id")
            or recovered_identity.get("worker_id")
            or ""
        ).strip(),
        "session_name": session_name,
        "state_path": str(Path(state_path).expanduser().resolve()),
        "state_revision": state_revision,
        "work_dir": str(_state_or_identity("work_dir")).strip(),
        "status": status,
        "workflow_stage": str(state.get("workflow_stage", "pending")).strip(),
        "agent_state": agent_state,
        "health_status": health_status,
        "health_note": health_note,
        "retry_count": int(state.get("retry_count", 0) or 0),
        "note": note,
        "transcript_path": str(state.get("transcript_path", "")).strip(),
        "turn_status_path": str(state.get("current_turn_status_path", "")).strip(),
        "current_turn_phase": str(state.get("current_turn_phase", "")).strip(),
        "current_task_status_path": str(state.get("current_task_status_path", "")).strip(),
        "current_task_result_path": str(state.get("current_task_result_path", "")).strip(),
        "current_task_runtime_status": current_task_runtime_status,
        "dispatch_state": str(state.get("dispatch_state", "")).strip(),
        "dispatch_reason": str(state.get("dispatch_reason", "")).strip(),
        "turn_state": str(state.get("turn_state", "")).strip(),
        "orphaned_at": str(state.get("orphaned_at", "")).strip(),
        "orphaned_reason": str(state.get("orphaned_reason", "")).strip(),
        "stage_runner_id": str(state.get("stage_runner_id", "")).strip(),
        "tmux_control_status": str(state.get("tmux_control_status", "")).strip(),
        "tmux_control_error": str(state.get("tmux_control_error", "")).strip(),
        "tmux_control_unavailable_since": str(state.get("tmux_control_unavailable_since", "")).strip(),
        "question_path": str(turn_bundle.get("question_path") or "").strip(),
        "answer_path": str(turn_bundle.get("answer_path") or "").strip(),
        "artifact_paths": artifact_paths,
        "agent_started": agent_started,
        "session_exists": session_exists,
        "last_heartbeat_at": str(state.get("last_heartbeat_at", "")).strip(),
        "updated_at": str(state.get("updated_at", "")).strip(),
        "project_dir": str(
            state.get("project_dir")
            or recovered_identity.get("project_dir")
            or recovered_identity.get("work_dir")
            or ""
        ).strip(),
        "requirement_name": str(_state_or_identity("requirement_name")).strip(),
        "workflow_action": str(_state_or_identity("workflow_action")).strip(),
        "stage_seq": str(state.get("stage_seq", "")).strip(),
        "run_id": str(state.get("run_id", "")).strip(),
        "vendor": str(config_payload.get("vendor", "")).strip(),
        "model": str(config_payload.get("model", "")).strip(),
        "resolved_model": str(config_payload.get("resolved_model", "")).strip(),
        "reasoning_effort": str(config_payload.get("reasoning_effort", "")).strip(),
        "startup_blocker_kind": str(state.get("startup_blocker_kind", "")).strip(),
        "startup_blocker_requires_manual": bool(state.get("startup_blocker_requires_manual", False)),
    }


ProtocolLogSink = BridgeLogSink


class BridgeCore:
    def __init__(self) -> None:
        self._adapter_name = ""
        self._event_subscribers: list[Callable[[Mapping[str, Any]], None]] = []
        self._event_lock = threading.Lock()
        self._response_emitter: Callable[[Mapping[str, Any]], None] | None = None
        self._response_emission_lock = threading.Lock()
        self._emitted_response_ids: dict[str, None] = {}
        self._pending_prompt_lock = threading.RLock()
        self._prompt_revision = 0
        self._pending_prompt: PendingPromptState | None = None
        self._pending_prompts: dict[str, PendingPromptState] = {}
        self._last_resolved_hitl = ResolvedHitlState()
        self._prompt_broker = PromptBroker(
            self.emit_event,
            on_prompt_open=self._handle_prompt_open,
            on_prompt_resolved=self._handle_prompt_resolved,
        )
        self._protocol_log_sink = BridgeLogSink(self.emit_event)
        self._bridge_ui = BridgeTerminalUI(
            emit_event=self.emit_event,
            request_prompt=self._prompt_broker.request,
            state_change_notifier=self._handle_runtime_state_change,
            stage_change_notifier=self._handle_runtime_stage_change,
            progress_context_provider=self._current_progress_context,
        )
        self._workers: dict[str, threading.Thread] = {}
        self._worker_registry_lock = threading.RLock()
        self._running_action_keys: dict[str, str] = {}
        self._runner_executions: dict[str, RunnerExecutionState] = {}
        self._scope_generation_ledger: dict[tuple[str, str], tuple[int, str]] = {}
        self._runner_local = threading.local()
        self._controls: dict[str, ControlSessionState] = {}
        self._controls_lock = threading.Lock()
        self._shutdown_lock = threading.Lock()
        self._shutdown_started = False
        self._shutdown_tmux_cleanup_done = False
        self._shutdown_policy: ShutdownPolicy | None = None
        self._shutdown_policy_reason = ""
        self._context = AppContext()
        self._presence_ttl_sec = 15.0
        self._tui_presence_refresh_interval_sec = 0.75
        self._tui_presence = TuiPresenceState()
        self._tui_presence_expiry_monotonic = 0.0
        self._tui_presence_lock = threading.Lock()
        self._tui_presence_refresh_lock = threading.Lock()
        self._tui_presence_refresh_timer: threading.Timer | None = None
        self._display_status = "ready"
        self._display_action = ""
        self._display_stage_seq = 0
        self._display_source = ""
        self._display_runner_id = ""
        self._display_message = ""
        self._display_failure: dict[str, Any] = {}
        self._display_state_lock = threading.RLock()
        self._stage_seq_counter = 0
        self._stage_seq_lock = threading.Lock()
        self._snapshot_debounce_sec = 0.15
        self._snapshot_dirty_lock = threading.Lock()
        self._snapshot_dirty_sections: set[str] = set()
        self._snapshot_dirty_stage_routes: set[str] = set()
        self._snapshot_dirty_refresh_worker_health = False
        self._snapshot_dirty_update_display_stage = False
        self._snapshot_flush_running = False
        self._snapshot_refresh_worker_health = True
        self._snapshot_runtime_scan_cache: dict[tuple[str, bool], list[dict[str, Any]]] | None = None
        self._snapshot_debounce_timer: threading.Timer | None = None
        self._artifact_index_lock = threading.Lock()
        self._artifact_index_scope: tuple[str, str] = ("", "")
        self._artifact_index_items: list[dict[str, Any]] = []
        self._pending_prompt_display_state: dict[str, Any] | None = None
        self._routing_manifest_worker_suppressed_projects: set[str] = set()
        self._active_control_id = ""
        self._tmux_runtime = TmuxRuntimeController()
        self._attention_manager = HumanAttentionManager(
            adapter_name_provider=lambda: self._adapter_name,
            emit_log=lambda text: self.emit_event("log.append", {"text": text}),
            presence_provider=self._build_tui_presence_snapshot,
            presence_ttl_sec=self._presence_ttl_sec,
        )

    def attach_adapter(self, adapter_name: str) -> None:
        normalized = str(adapter_name or "").strip().lower()
        if not normalized:
            raise ValueError("adapter_name 不能为空")
        current = str(self._adapter_name or "").strip().lower()
        if current and current != normalized:
            raise RuntimeError(f"当前 backend 已绑定 adapter={current}，不能再挂载 {normalized}")
        self._adapter_name = normalized

    def subscribe_events(self, listener: Callable[[Mapping[str, Any]], None]) -> Callable[[], None]:
        with self._event_lock:
            self._event_subscribers.append(listener)

        def _unsubscribe() -> None:
            with self._event_lock:
                with contextlib.suppress(ValueError):
                    self._event_subscribers.remove(listener)

        return _unsubscribe

    def set_response_emitter(self, emitter: Callable[[Mapping[str, Any]], None] | None) -> None:
        self._response_emitter = emitter

    def _emit_hitl_prompt_log(self, request: BridgePromptRequest) -> None:
        question_path_text = str(request.payload.get("question_path", "")).strip()
        if not question_path_text:
            return
        question_file = Path(question_path_text).expanduser().resolve()
        if not question_file.exists() or not question_file.is_file():
            return
        try:
            question_text = question_file.read_text(encoding="utf-8").strip()
        except Exception:  # noqa: BLE001
            return
        if not question_text:
            return
        hitl_round = _extract_hitl_round(request.payload.get("title") or request.payload.get("prompt_text"))
        hitl_title = f"HITL 第 {hitl_round} 轮" if hitl_round is not None else "HITL"
        lines = [
            "",
            hitl_title,
            f"HITL 问题文档: {question_file}",
            question_text,
            "",
        ]
        payload: dict[str, Any] = {
            "text": "\n".join(lines),
            "log_kind": "hitl",
            "log_title": hitl_title,
        }
        if hitl_round is not None:
            payload["hitl_round"] = hitl_round
        self.emit_event("log.append", payload)

    def _next_prompt_revision_locked(self) -> int:
        self._prompt_revision += 1
        return self._prompt_revision

    def _current_pending_prompt_locked(self) -> PendingPromptState | None:
        current = self._pending_prompt
        if current is not None:
            registered = self._pending_prompts.get(current.prompt_id)
            if registered is current or not self._pending_prompts:
                return current
        if not self._pending_prompts:
            return None
        latest_prompt_id = next(reversed(self._pending_prompts))
        return self._pending_prompts[latest_prompt_id]

    @staticmethod
    def _build_prompt_request_payload(
        prompt: PendingPromptState,
        *,
        prompt_revision: int,
    ) -> dict[str, Any]:
        return {
            **prompt.payload,
            "id": prompt.prompt_id,
            "prompt_type": prompt.prompt_type,
            "prompt_revision": max(int(prompt_revision or 0), 0),
        }

    def _handle_prompt_open(self, prompt_id: str, request: BridgePromptRequest) -> Mapping[str, Any]:
        owner_runner_id = str(getattr(self._runner_local, "runner_id", "") or "").strip()
        if not owner_runner_id:
            with self._display_state_lock:
                owner_runner_id = str(self._display_runner_id or "").strip()
        pending = PendingPromptState(
            prompt_id=str(prompt_id).strip(),
            prompt_type=str(request.prompt_type or "").strip(),
            payload=dict(request.payload),
            created_at=_iso_now(),
            owner_runner_id=owner_runner_id,
        )
        with self._pending_prompt_lock:
            self._pending_prompts[pending.prompt_id] = pending
            self._pending_prompt = pending
            prompt_revision = self._next_prompt_revision_locked()
        self._attention_manager.start_prompt(
            prompt_id=prompt_id,
            prompt_type=request.prompt_type,
            payload=request.payload,
            stage_label=self._resolve_stage_label(
                action=self._display_action or self._context.current_action,
                project_dir=self._resolve_project_dir(),
                requirement_name=self._resolve_requirement_name(),
            ),
        )
        self._emit_hitl_prompt_log(request)
        prompt_display_state = self._pending_prompt_display_state
        self._pending_prompt_display_state = None
        if prompt_display_state:
            self._emit_display_stage_state(**prompt_display_state)
        else:
            action = str(self._display_action or self._context.current_action or "").strip()
            if action:
                self._emit_display_stage_state(
                    preferred_status="awaiting-input",
                    preferred_action=action,
                    preferred_stage_seq=int(self._display_stage_seq or 0),
                    source="runtime_inference",
                    force=True,
                )
        if _prompt_is_hitl(request.payload):
            hitl_snapshot = self._build_pending_prompt_hitl_snapshot()
            self._emit_snapshot_payload(
                "snapshot.app",
                lambda: self._build_app_snapshot(
                    runs=[],
                    control={},
                    hitl=hitl_snapshot,
                    attention=self._attention_manager.snapshot(),
                    artifacts={"items": []},
                ),
            )
            self.emit_event("snapshot.hitl", hitl_snapshot)
        stage_routes = self._stage_routes_for_action(self._display_action or self._context.current_action)
        self._schedule_flow_snapshot_update(
            sections={"app", "hitl", "prompt"},
            stage_routes=stage_routes,
        )
        return {"prompt_revision": prompt_revision}

    def _handle_prompt_resolved(self, prompt_id: str, payload: Mapping[str, Any] | None = None) -> None:
        prompt_id_text = str(prompt_id).strip()
        with self._pending_prompt_lock:
            current = self._pending_prompts.get(prompt_id_text)
            if current is None and self._pending_prompt is not None and self._pending_prompt.prompt_id == prompt_id_text:
                current = self._pending_prompt
        routing_snapshot_changed = False
        resolved_payload = dict(payload or {})
        prompt_shutdown = bool(str(resolved_payload.get("__prompt_broker_shutdown__", "")).strip())
        if current is not None and not prompt_shutdown:
            self._update_context_from_prompt_response(current, resolved_payload)
            routing_snapshot_changed = self._update_routing_manifest_suppression_from_prompt(current, resolved_payload)
            self._remember_resolved_hitl_prompt(current)
        # Do not remove pending/attention state until every scope-critical
        # callback above has succeeded. PromptBroker will release its claim and
        # allow the same response to be retried when an exception escapes.
        with self._pending_prompt_lock:
            authoritative_before = self._current_pending_prompt_locked()
            removed = self._pending_prompts.pop(prompt_id_text, None)
            removed_fallback = bool(
                self._pending_prompt is not None
                and self._pending_prompt.prompt_id == prompt_id_text
            )
            if removed_fallback:
                self._pending_prompt = None
            latest_pending = self._current_pending_prompt_locked()
            self._pending_prompt = latest_pending
            prompt_state_changed = removed is not None or removed_fallback
            prompt_revision = (
                self._next_prompt_revision_locked()
                if prompt_state_changed
                else self._prompt_revision
            )
            republish_latest = bool(
                prompt_state_changed
                and authoritative_before is not None
                and authoritative_before.prompt_id == prompt_id_text
                and latest_pending is not None
            )
        self._attention_manager.resolve_prompt(prompt_id)
        if republish_latest and latest_pending is not None:
            # A previously hidden prompt is authoritative again. Re-publish it
            # before unblocking the resolved workflow so clients cannot remain
            # on the just-resolved prompt. A reconnect can always recover from
            # snapshot.prompt if the best-effort event delivery itself fails.
            self._best_effort_emit_event(
                "prompt.request",
                self._build_prompt_request_payload(
                    latest_pending,
                    prompt_revision=prompt_revision,
                ),
            )
        if current is not None and not prompt_shutdown and not _prompt_is_hitl(current.payload):
            self._restore_active_runner_after_prompt_resolution(current)
        self._schedule_flow_snapshot_update(
            sections={"app", "hitl", "prompt"},
            stage_routes=("routing",) if routing_snapshot_changed else (),
            update_display_stage=True,
        )

    def _build_pending_prompt_attention_snapshot(self) -> dict[str, Any]:
        prompts = [
            prompt
            for prompt in self._iter_pending_prompts()
            if not _prompt_is_hitl(prompt.payload)
            and _prompt_requires_attention(prompt.prompt_type, prompt.payload)
        ]
        if not prompts:
            return {
                "pending": False,
                "reason": "",
                "title": "",
                "body": "",
                "started_at": "",
                "last_notified_at": "",
                "next_notify_at": "",
                "suppressed_due_to_presence": False,
                "suppressed_until": "",
                "active": False,
            }
        latest = prompts[-1]
        return {
            "pending": True,
            "reason": _prompt_attention_reason(latest.prompt_type, latest.payload),
            "title": str(latest.payload.get("title", "") or latest.payload.get("prompt_text", "") or "").strip(),
            "body": _build_attention_body(latest.prompt_type, latest.payload),
            "started_at": str(latest.created_at or "").strip(),
            "last_notified_at": "",
            "next_notify_at": "",
            "suppressed_due_to_presence": False,
            "suppressed_until": "",
            "active": True,
        }

    def record_tui_presence(self, reason: str, shell_focus: str) -> dict[str, Any]:
        if str(self._adapter_name or "").strip().lower() != "tui":
            return {"accepted": False, "reason": "adapter_not_tui"}
        now_mono = time.monotonic()
        active_until_iso = _iso_after_seconds(self._presence_ttl_sec)
        with self._tui_presence_lock:
            self._tui_presence = TuiPresenceState(
                last_seen_at=_iso_now(),
                last_reason=str(reason or "").strip(),
                active_until=active_until_iso,
            )
            self._tui_presence_expiry_monotonic = now_mono + self._presence_ttl_sec
        with self._tui_presence_refresh_lock:
            timer = self._tui_presence_refresh_timer
            self._tui_presence_refresh_timer = None
        if timer is not None:
            timer.cancel()
        stage_routes = self._stage_routes_for_action(self._current_snapshot_action())
        self._schedule_snapshot_update(
            sections={"app", "control"},
            stage_routes=stage_routes,
            delay_sec=0.0,
            refresh_worker_health=False,
        )
        self._arm_tui_presence_refresh_timer()
        return {
            "accepted": True,
            "active_until": active_until_iso,
        }

    def is_tui_presence_recent(self) -> bool:
        if str(self._adapter_name or "").strip().lower() != "tui":
            return False
        with self._tui_presence_lock:
            return self._tui_presence_expiry_monotonic > time.monotonic()

    def presence_expires_at(self) -> str:
        with self._tui_presence_lock:
            return str(self._tui_presence.active_until or "").strip()

    def _build_tui_presence_snapshot(self) -> dict[str, Any]:
        with self._tui_presence_lock:
            active_until = str(self._tui_presence.active_until or "").strip()
            delay_sec = max(self._tui_presence_expiry_monotonic - time.monotonic(), 0.0)
            recent = self._tui_presence_expiry_monotonic > time.monotonic()
        return {
            "recent": bool(recent and str(self._adapter_name or "").strip().lower() == "tui"),
            "active_until": active_until,
            "delay_sec": delay_sec,
        }

    @staticmethod
    def _worker_snapshot_is_live(worker: Mapping[str, Any]) -> bool:
        if _worker_snapshot_is_stale_missing_session_live_noise(worker):
            return False
        session_name = str(worker.get("session_name", "") or worker.get("sessionName", "")).strip()
        if not session_name:
            return False
        agent_state = str(worker.get("agent_state", "") or worker.get("agentState", "")).strip().upper()
        if agent_state == "DEAD":
            return False
        if agent_state == "STARTING":
            return True
        session_exists = worker.get("session_exists", worker.get("sessionExists"))
        if session_exists is not None:
            return bool(session_exists)
        health_status = str(worker.get("health_status", "") or worker.get("healthStatus", "")).strip().lower()
        return health_status in {"alive", "observe_error", "provider_auth_error"}

    def _current_snapshot_action(self) -> str:
        return str(self._display_action or self._context.current_action or "").strip()

    def _tui_presence_refresh_plan(self) -> dict[str, Any] | None:
        if str(self._adapter_name or "").strip().lower() != "tui":
            return None
        with self._shutdown_lock:
            if self._shutdown_started:
                return None
        if not self.is_tui_presence_recent():
            return None

        action = self._current_snapshot_action()
        if action and action != "idle":
            return {
                "sections": {"app", "control"},
                "stage_routes": self._stage_routes_for_action(action),
            }
        if self._iter_pending_prompts():
            return {"sections": {"app", "control"}, "stage_routes": ()}

        with contextlib.suppress(Exception), self._snapshot_worker_health_mode(False):
            if bool(self._build_hitl_snapshot().get("pending", False)):
                return {"sections": {"app", "control"}, "stage_routes": ()}

        with contextlib.suppress(Exception), self._snapshot_worker_health_mode(False):
            control_snapshot = self._build_control_snapshot_for_session(self._current_control_session())
            if any(self._worker_snapshot_is_live(worker) for worker in control_snapshot.get("workers", [])):
                return {"sections": {"app", "control"}, "stage_routes": ()}

        return None

    def _arm_tui_presence_refresh_timer(self) -> None:
        plan = self._tui_presence_refresh_plan()
        if plan is None:
            with self._tui_presence_refresh_lock:
                timer = self._tui_presence_refresh_timer
                self._tui_presence_refresh_timer = None
            if timer is not None:
                timer.cancel()
            return
        with self._tui_presence_refresh_lock:
            if self._tui_presence_refresh_timer is not None:
                return
            timer = threading.Timer(self._tui_presence_refresh_interval_sec, self._run_tui_presence_refresh_tick)
            timer.daemon = True
            self._tui_presence_refresh_timer = timer
            timer.start()

    def _run_tui_presence_refresh_tick(self) -> None:
        with self._tui_presence_refresh_lock:
            self._tui_presence_refresh_timer = None
        plan = self._tui_presence_refresh_plan()
        if plan is None:
            return
        stage_routes = tuple(plan["stage_routes"])
        self._schedule_snapshot_update(
            sections=set(plan["sections"]),
            stage_routes=stage_routes,
            delay_sec=0.0,
            refresh_worker_health=False,
        )
        self._arm_tui_presence_refresh_timer()

    @contextlib.contextmanager
    def _snapshot_worker_health_mode(self, refresh_worker_health: bool):
        previous_refresh_worker_health = self._snapshot_refresh_worker_health
        self._snapshot_refresh_worker_health = bool(refresh_worker_health)
        try:
            yield
        finally:
            self._snapshot_refresh_worker_health = previous_refresh_worker_health

    def _iter_pending_prompts(self) -> list[PendingPromptState]:
        with self._pending_prompt_lock:
            if self._pending_prompts:
                return list(self._pending_prompts.values())
            if self._pending_prompt is not None:
                return [self._pending_prompt]
            return []

    def _latest_pending_prompt(self) -> PendingPromptState | None:
        with self._pending_prompt_lock:
            return self._current_pending_prompt_locked()

    def _remember_resolved_hitl_prompt(self, prompt: PendingPromptState) -> None:
        if not _prompt_is_hitl(prompt.payload):
            self._last_resolved_hitl = ResolvedHitlState()
            return
        question_path = str(prompt.payload.get("question_path", "") or "").strip()
        question_summary = ""
        if question_path:
            question_summary = _preview_text(question_path)
        self._last_resolved_hitl = ResolvedHitlState(
            question_path=question_path,
            question_summary=question_summary,
        )

    @staticmethod
    def _prompt_stage_step_index(prompt: PendingPromptState) -> int:
        try:
            return int(prompt.payload.get("stage_step_index", -1))
        except Exception:
            return -1

    @staticmethod
    def _routing_setup_prompt(prompt: PendingPromptState) -> bool:
        payload = prompt.payload
        if str(payload.get("stage_key", "")).strip() == "routing":
            return True
        prompt_text = str(payload.get("prompt_text", "") or payload.get("title", "")).strip()
        return "AGENT初始化" in prompt_text or "routing" in prompt_text.lower()

    def _routing_manifest_workers_suppressed(self, project_dir: str) -> bool:
        project_text = str(project_dir or "").strip()
        if not project_text:
            return False
        normalized_project_dir = str(Path(project_text).expanduser().resolve())
        if normalized_project_dir in self._routing_manifest_worker_suppressed_projects:
            return True
        return any(self._routing_setup_prompt(prompt) for prompt in self._iter_pending_prompts())

    def _set_routing_manifest_worker_suppression(self, project_dir: str, *, suppressed: bool) -> None:
        project_text = str(project_dir or "").strip()
        if not project_text:
            return
        normalized_project_dir = str(Path(project_text).expanduser().resolve())
        if suppressed:
            self._routing_manifest_worker_suppressed_projects.add(normalized_project_dir)
        else:
            self._routing_manifest_worker_suppressed_projects.discard(normalized_project_dir)

    def _update_routing_manifest_suppression_from_prompt(
        self,
        prompt: PendingPromptState,
        payload: Mapping[str, Any],
    ) -> bool:
        if not self._routing_setup_prompt(prompt):
            return False
        step_index = self._prompt_stage_step_index(prompt)
        prompt_text = str(prompt.payload.get("prompt_text", "") or prompt.payload.get("title", "")).strip()
        if step_index not in {1, 7} and "AGENT初始化" not in prompt_text:
            return False
        value = str(payload.get("value", "")).strip().lower()
        if value not in {"yes", "no"}:
            return False
        project_dir = self._resolve_project_dir()
        if not project_dir:
            return False
        normalized_project_dir = str(Path(project_dir).expanduser().resolve())
        before = normalized_project_dir in self._routing_manifest_worker_suppressed_projects
        self._set_routing_manifest_worker_suppression(project_dir, suppressed=value == "no")
        after = normalized_project_dir in self._routing_manifest_worker_suppressed_projects
        return value == "no" or before != after

    def _update_routing_manifest_suppression_from_result(self, result: Any) -> None:
        skipped = bool(getattr(result, "skipped", False))
        project_dir = str(getattr(result, "project_dir", "") or "").strip()
        if not project_dir:
            return
        self._set_routing_manifest_worker_suppression(project_dir, suppressed=skipped)

    def _persist_previous_runtime_stage_exit(
        self,
        next_action: str,
        *,
        previous_action: str | None = None,
        previous_stage_seq: int | None = None,
        runner_id: str = "",
        project_dir: str | None = None,
        requirement_name: str | None = None,
        persist: bool = True,
    ) -> None:
        previous_action = str(previous_action or self._display_action or self._context.current_action or "").strip()
        if not previous_action or previous_action == next_action:
            return
        previous_index = _workflow_stage_order(previous_action)
        next_index = _workflow_stage_order(next_action)
        if previous_index <= 0 or next_index <= 0:
            return
        previous_status = str(self._display_status or "").strip().lower()
        if previous_status in {"completed", "failed", "error"}:
            return
        if not persist:
            return
        exit_status = "completed" if next_index > previous_index else "superseded"
        _write_project_stage_state_record(
            project_dir=str(project_dir or self._resolve_project_dir()),
            requirement_name=(
                self._resolve_requirement_name()
                if requirement_name is None
                else str(requirement_name or "").strip()
            ),
            action=previous_action,
            status=exit_status,
            stage_seq=int(previous_stage_seq or self._display_stage_seq or 0),
            runner_id=str(runner_id or self._display_runner_id or "").strip(),
            source="runner_complete",
            message=f"stage switched to {next_action}",
        )

    def _handle_runtime_stage_change(self, action: str) -> None:
        normalized = str(action or "").strip()
        if not normalized:
            return
        runner_id = str(getattr(self._runner_local, "runner_id", "") or self._display_runner_id or "").strip()
        with self._worker_registry_lock:
            if self._runner_generation_is_superseded_locked(runner_id):
                return
            execution = next(
                (
                    item
                    for item in self._runner_executions.values()
                    if str(getattr(item, "runner_id", "") or "").strip() == runner_id
                ),
                None,
            )
            previous_action = (
                self._execution_current_action(execution)
                if execution is not None
                else str(self._display_action or self._context.current_action or "").strip()
            )
            previous_stage_seq = (
                self._execution_current_stage_seq(execution)
                if execution is not None
                else int(self._display_stage_seq or 0)
            )
            generation_registered = bool(
                getattr(execution, "generation_registered", True)
            ) if execution is not None else True
            execution_project = str(getattr(execution, "project_dir", "") or "") if execution is not None else self._resolve_project_dir()
            execution_requirement = str(getattr(execution, "requirement_name", "") or "") if execution is not None else self._resolve_requirement_name()
            stage_seq = self._allocate_stage_seq(
                project_dir=execution_project,
                requirement_name=execution_requirement,
            )
            # Keep the superseded recheck, previous-state write, execution
            # cursor update, and global action update in one registry critical
            # section. A newer direct runner can only register afterwards and
            # therefore receives the final authoritative generation.
            if self._runner_generation_is_superseded_locked(runner_id):
                return
            self._persist_previous_runtime_stage_exit(
                normalized,
                previous_action=previous_action,
                previous_stage_seq=previous_stage_seq,
                runner_id=runner_id,
                project_dir=execution_project,
                requirement_name=execution_requirement,
                persist=generation_registered,
            )
            if execution is not None:
                execution.current_action = normalized
                execution.current_stage_seq = stage_seq
            self._set_context(action=normalized)
        accepted = self._emit_display_stage_state(
            preferred_status="running",
            preferred_action=normalized,
            preferred_stage_seq=stage_seq,
            preferred_runner_id=runner_id,
            source="runner_start",
            message="准备智能体",
            force=True,
        )
        if not accepted:
            return
        self._cleanup_stage_orphans_before_runner_start(normalized, runner_id=runner_id)
        self._schedule_flow_snapshot_update(
            sections={"app"},
            stage_routes=self._stage_routes_for_action(normalized),
        )

    def _handle_runtime_state_change(self) -> None:
        with self._display_state_lock:
            action = str(self._display_action or self._context.current_action or "").strip()
            stage_seq = int(self._display_stage_seq or 0)
            runner_id = str(self._display_runner_id or "").strip()
            display_status = str(self._display_status or "").strip()
        if action and display_status in {"running", "awaiting-input"}:
            workers = self._current_stage_workers_without_runtime_io(action)
            if any(worker_state_is_prelaunch_active(worker) for worker in workers):
                self._emit_display_stage_state(
                    preferred_status="running",
                    preferred_action=action,
                    preferred_stage_seq=stage_seq,
                    preferred_runner_id=runner_id,
                    source="runner_start",
                    message="等待 tmux",
                    force=True,
                )
        self._schedule_flow_snapshot_update(
            sections={"app", "control", "hitl"},
            stage_routes=self._stage_routes_for_action(self._display_action or self._context.current_action),
            update_display_stage=True,
            refresh_worker_health=False,
        )

    def _active_stage_runner_alive(self, action: str) -> bool:
        normalized = str(action or "").strip()
        if not normalized:
            return False
        with self._display_state_lock:
            display_action = str(self._display_action or "").strip()
            display_runner_id = str(self._display_runner_id or "").strip()
        with self._worker_registry_lock:
            active_workers = [
                (worker_key, thread, self._runner_executions.get(worker_key))
                for worker_key, thread in self._workers.items()
            ]
        for _worker_key, thread, execution in active_workers:
            try:
                if not thread.is_alive():
                    continue
                execution_action = self._execution_current_action(execution)
                execution_runner_id = str(getattr(execution, "runner_id", "") or "").strip()
                if execution_action == normalized:
                    return True
                if (
                    display_runner_id
                    and execution_runner_id == display_runner_id
                    and display_action == normalized
                    and str(getattr(execution, "action", "") or "").strip() == "workflow.a00.start"
                ):
                    return True
                if normalized in str(getattr(thread, "name", "")):
                    return True
            except Exception:
                continue
        return False

    def _runner_scope_key(self, project_dir: str, requirement_name: str) -> tuple[str, str]:
        return (
            self._normalize_action_scope_project(str(project_dir or "")),
            str(requirement_name or "").strip(),
        )

    @staticmethod
    def _execution_current_action(execution: RunnerExecutionState | Any) -> str:
        return str(
            getattr(execution, "current_action", "")
            or getattr(execution, "action", "")
            or ""
        ).strip()

    @staticmethod
    def _execution_current_stage_seq(execution: RunnerExecutionState | Any) -> int:
        return int(
            getattr(execution, "current_stage_seq", 0)
            or getattr(execution, "stage_seq", 0)
            or 0
        )

    def _runner_execution_by_id(self, runner_id: str) -> RunnerExecutionState | None:
        normalized_runner_id = str(runner_id or "").strip()
        if not normalized_runner_id:
            return None
        with self._worker_registry_lock:
            for execution in self._runner_executions.values():
                if str(getattr(execution, "runner_id", "") or "").strip() == normalized_runner_id:
                    return execution
        return None

    def _active_runner_execution_by_id(self, runner_id: str) -> RunnerExecutionState | None:
        normalized_runner_id = str(runner_id or "").strip()
        if not normalized_runner_id:
            return None
        with self._worker_registry_lock:
            for worker_key, execution in self._runner_executions.items():
                if str(getattr(execution, "runner_id", "") or "").strip() != normalized_runner_id:
                    continue
                thread = self._workers.get(worker_key)
                if thread is None or not thread.is_alive():
                    return None
                if str(getattr(execution, "terminal_source", "") or "").strip():
                    return None
                if self._runner_generation_is_superseded_locked(normalized_runner_id):
                    return None
                return execution
        return None

    def _restore_active_runner_after_prompt_resolution(self, prompt: PendingPromptState) -> bool:
        """Move a live prompt owner back to running until its next prompt opens."""
        owner_runner_id = str(prompt.owner_runner_id or "").strip()
        if not owner_runner_id or self._iter_pending_prompts():
            return False
        execution = self._active_runner_execution_by_id(owner_runner_id)
        if execution is None:
            return False
        action = self._execution_current_action(execution)
        stage_seq = self._execution_current_stage_seq(execution)
        if not action:
            return False
        with self._display_state_lock:
            display_runner_id = str(self._display_runner_id or "").strip()
            display_status = str(self._display_status or "").strip()
        if display_runner_id and display_runner_id != owner_runner_id:
            return False
        if display_status in {"completed", "failed", "error", "superseded"}:
            return False
        if self._runner_generation_has_persisted_terminal(
            execution=execution,
            action=action,
            stage_seq=stage_seq,
        ):
            return False
        # A runner-owned prompt is the active interaction gate. Other prompt/HITL
        # gates remain in _pending_prompts and were rejected above; a durable
        # file-only HITL has no live prompt-owning runner to restore here.
        return self._emit_display_stage_state(
            preferred_status="running",
            preferred_action=action,
            preferred_stage_seq=stage_seq,
            preferred_runner_id=owner_runner_id,
            source="runner_start",
            message="准备下一步配置",
            force=True,
        )

    def _active_explicit_runner_execution(self, action: str) -> RunnerExecutionState | None:
        normalized_action = str(action or "").strip()
        if not normalized_action:
            return None
        project_key = self._runner_scope_key(
            self._resolve_project_dir(),
            self._resolve_requirement_name(),
        )
        with self._worker_registry_lock:
            executions = list(self._runner_executions.values())
        for execution in executions:
            if str(getattr(execution, "terminal_source", "") or "").strip():
                continue
            execution_action = self._execution_current_action(execution)
            if execution_action != normalized_action:
                continue
            execution_key = self._runner_scope_key(
                str(getattr(execution, "project_dir", "") or ""),
                str(getattr(execution, "requirement_name", "") or ""),
            )
            if project_key[0] and execution_key[0] and execution_key[0] != project_key[0]:
                continue
            if project_key[1] and execution_key[1] and execution_key[1] != project_key[1]:
                continue
            return execution
        return None

    def _record_scope_generation(
        self,
        *,
        runner_id: str,
        stage_seq: int,
        project_dir: str,
        requirement_name: str,
    ) -> None:
        normalized_runner_id = str(runner_id or "").strip()
        scope_key = self._runner_scope_key(project_dir, requirement_name)
        if not normalized_runner_id or not scope_key[0] or int(stage_seq or 0) <= 0:
            return
        with self._worker_registry_lock:
            previous = self._scope_generation_ledger.get(scope_key)
            if previous is None or int(stage_seq) > int(previous[0]) or previous[1] == normalized_runner_id:
                self._scope_generation_ledger[scope_key] = (int(stage_seq), normalized_runner_id)

    def _runner_generation_is_superseded_locked(self, runner_id: str) -> bool:
        normalized_runner_id = str(runner_id or "").strip()
        if not normalized_runner_id:
            return False
        executions = list(self._runner_executions.values())
        current = next(
            (
                execution
                for execution in executions
                if str(getattr(execution, "runner_id", "") or "").strip() == normalized_runner_id
            ),
            None,
        )
        if current is None:
            return False
        current_seq = self._execution_current_stage_seq(current)
        current_project = str(getattr(current, "project_dir", "") or "").strip()
        current_requirement = str(getattr(current, "requirement_name", "") or "").strip()
        generation_registered = bool(getattr(current, "generation_registered", True))
        current_scope = self._runner_scope_key(current_project, current_requirement)
        if generation_registered and current_scope[0]:
            latest_generation = self._scope_generation_ledger.get(current_scope)
            if (
                latest_generation is not None
                and latest_generation[1] != normalized_runner_id
                and int(latest_generation[0]) >= current_seq
            ):
                return True
        for execution in executions:
            candidate_runner_id = str(getattr(execution, "runner_id", "") or "").strip()
            if not candidate_runner_id or candidate_runner_id == normalized_runner_id:
                continue
            if not bool(getattr(execution, "generation_registered", True)):
                continue
            if self._execution_current_stage_seq(execution) <= current_seq:
                continue
            candidate_project = str(getattr(execution, "project_dir", "") or "").strip()
            candidate_requirement = str(getattr(execution, "requirement_name", "") or "").strip()
            if not generation_registered:
                continue
            if current_project and candidate_project and current_project != candidate_project:
                continue
            if current_requirement and candidate_requirement and current_requirement != candidate_requirement:
                continue
            return True
        return False

    def _runner_generation_is_superseded(self, runner_id: str) -> bool:
        with self._worker_registry_lock:
            return self._runner_generation_is_superseded_locked(runner_id)

    def _allocate_stage_seq(
        self,
        *,
        project_dir: str | None = None,
        requirement_name: str | None = None,
    ) -> int:
        with self._stage_seq_lock:
            persisted_maximum = _max_project_stage_seq(
                project_dir=str(project_dir or self._resolve_project_dir()).strip(),
                requirement_name=(
                    self._resolve_requirement_name()
                    if requirement_name is None
                    else str(requirement_name or "").strip()
                ),
            )
            self._stage_seq_counter = max(self._stage_seq_counter, persisted_maximum) + 1
            return self._stage_seq_counter

    def _has_explicit_runner_generation(
        self,
        *,
        action: str,
        project_dir: str,
        requirement_name: str,
    ) -> bool:
        normalized_action = str(action or "").strip()
        normalized_project = self._normalize_action_scope_project(project_dir)
        normalized_requirement = str(requirement_name or "").strip()
        with self._worker_registry_lock:
            executions = [
                (worker_key, execution)
                for worker_key, execution in self._runner_executions.items()
                if worker_key in self._workers
            ]
        for _worker_key, execution in executions:
            execution_action = self._execution_current_action(execution)
            root_action = str(execution.action or "").strip()
            if execution_action != normalized_action and root_action != "workflow.a00.start":
                continue
            execution_project = self._normalize_action_scope_project(execution.project_dir)
            if normalized_project and execution_project and execution_project != normalized_project:
                continue
            execution_requirement = str(execution.requirement_name or "").strip()
            if (
                normalized_requirement
                and execution_requirement
                and execution_requirement != normalized_requirement
            ):
                continue
            return True
        return False

    def _reconcile_persisted_running_runner(self, action: str) -> bool:
        normalized_action = str(action or "").strip()
        project_dir = str(self._resolve_project_dir() or "").strip()
        requirement_name = self._resolve_requirement_name()
        if not normalized_action or not project_dir:
            return False
        persisted_state = _read_project_stage_state_record(
            project_dir=project_dir,
            requirement_name=requirement_name,
            action=normalized_action,
        )
        if not persisted_state:
            return False
        if str(persisted_state.get("source", "") or "").strip() != "runner_start":
            return False
        if str(persisted_state.get("status", "") or "").strip() != "running":
            return False
        if self._has_explicit_runner_generation(
            action=normalized_action,
            project_dir=project_dir,
            requirement_name=requirement_name,
        ):
            return False
        stage_seq = int(persisted_state.get("stage_seq") or 0)
        runner_id = str(persisted_state.get("runner_id", "") or "").strip()
        _failure_path, _orphaned_workers, accepted = self._commit_runner_failure(
            action=normalized_action,
            stage_seq=stage_seq,
            runner_id=runner_id,
            error=RuntimeShutdownRequested(
                "检测到上一个 backend 已退出，但持久化 runner 仍为 running；已收敛为 runner_interrupted。"
            ),
            traceback_text="",
            failure_kind="runner_interrupted",
        )
        return accepted

    def _current_progress_context(self) -> dict[str, Any]:
        with self._display_state_lock:
            return {
                "action": str(self._display_action or self._context.current_action or "").strip(),
                "stage_seq": int(self._display_stage_seq or 0),
                "runner_id": str(self._display_runner_id or "").strip(),
            }

    @staticmethod
    def _prompt_marker_text(prompt: PendingPromptState) -> str:
        return " ".join(
            [
                prompt.prompt_type,
                str(prompt.payload.get("title", "")),
                str(prompt.payload.get("prompt_text", "")),
            ]
        ).strip()

    @staticmethod
    def _execution_scope_ready_for_registration(execution: RunnerExecutionState) -> bool:
        project_dir = str(getattr(execution, "project_dir", "") or "").strip()
        requirement_name = str(getattr(execution, "requirement_name", "") or "").strip()
        if not project_dir:
            return False
        if str(getattr(execution, "action", "") or "").strip() == "workflow.a00.start":
            return bool(requirement_name)
        return True

    def _register_execution_generation_if_ready(
        self,
        execution: RunnerExecutionState,
    ) -> tuple[str, int, str] | None:
        if not self._execution_scope_ready_for_registration(execution):
            return None
        project_dir = self._normalize_action_scope_project(execution.project_dir)
        requirement_name = str(execution.requirement_name or "").strip()
        if (
            bool(execution.generation_registered)
            and self._normalize_action_scope_project(execution.registered_project_dir) == project_dir
            and str(execution.registered_requirement_name or "").strip() == requirement_name
        ):
            return None
        root_action = str(execution.action or "").strip()
        current_action = self._execution_current_action(execution) or root_action
        with self._worker_registry_lock:
            record_dir = _project_stage_record_dir(
                project_dir=project_dir,
                requirement_name=requirement_name,
            )
            generation_snapshot = _snapshot_stage_generation_files(record_dir)
            try:
                root_stage_seq = self._allocate_stage_seq(
                    project_dir=project_dir,
                    requirement_name=requirement_name,
                )
                root_state_path = _write_project_stage_state_record(
                    project_dir=project_dir,
                    requirement_name=requirement_name,
                    action=root_action,
                    status="running",
                    stage_seq=root_stage_seq,
                    runner_id=execution.runner_id,
                    source="runner_start",
                    message="准备智能体",
                    activate_generation=False,
                )
                if root_state_path is None:
                    raise RuntimeError(
                        f"runner generation state could not be persisted: action={root_action}; "
                        f"runner_id={execution.runner_id}"
                    )
                current_stage_seq = root_stage_seq
                if current_action != root_action:
                    current_stage_seq = self._allocate_stage_seq(
                        project_dir=project_dir,
                        requirement_name=requirement_name,
                    )
                    child_state_path = _write_project_stage_state_record(
                        project_dir=project_dir,
                        requirement_name=requirement_name,
                        action=current_action,
                        status="running",
                        stage_seq=current_stage_seq,
                        runner_id=execution.runner_id,
                        source="runner_start",
                        message="准备智能体",
                        activate_generation=False,
                    )
                    if child_state_path is None:
                        raise RuntimeError(
                            f"runner generation state could not be persisted: action={current_action}; "
                            f"runner_id={execution.runner_id}"
                        )
                _activate_stage_generation_files(
                    record_dir,
                    root_action=root_action,
                    current_action=current_action,
                )
            except BaseException:
                try:
                    _restore_stage_generation_files(record_dir, generation_snapshot)
                except BaseException as rollback_error:
                    raise RuntimeError(
                        "runner generation state rollback failed after persistence error"
                    ) from rollback_error
                raise
            execution.stage_seq = root_stage_seq
            execution.current_action = current_action
            execution.current_stage_seq = current_stage_seq
            execution.registered_project_dir = project_dir
            execution.registered_requirement_name = requirement_name
            execution.generation_registered = True
            self._record_scope_generation(
                runner_id=execution.runner_id,
                stage_seq=current_stage_seq,
                project_dir=project_dir,
                requirement_name=requirement_name,
            )
        return current_action, current_stage_seq, execution.runner_id

    def _update_runner_execution_scope_if_unset(
        self,
        *,
        project_dir: str | None = None,
        requirement_name: str | None = None,
        runner_id: str | None = None,
    ) -> bool:
        thread_runner_id = str(getattr(self._runner_local, "runner_id", "") or "").strip()
        target_runner_id = str(runner_id or thread_runner_id or "").strip()
        normalized_project = self._normalize_action_scope_project(str(project_dir or ""))
        normalized_requirement = str(requirement_name or "").strip()
        registered_transition: tuple[str, int, str] | None = None
        registered_context: tuple[str, str] | None = None
        accepted_update = False
        with self._worker_registry_lock:
            executions = list(self._runner_executions.values())
            if target_runner_id:
                executions = [
                    execution
                    for execution in executions
                    if str(getattr(execution, "runner_id", "") or "").strip() == target_runner_id
                ]
            elif len(executions) != 1:
                return False
            for execution in executions:
                if bool(getattr(execution, "generation_registered", False)):
                    frozen_project = self._normalize_action_scope_project(
                        str(
                            getattr(execution, "registered_project_dir", "")
                            or getattr(execution, "project_dir", "")
                            or ""
                        )
                    )
                    frozen_requirement = str(
                        getattr(execution, "registered_requirement_name", "")
                        or getattr(execution, "requirement_name", "")
                        or ""
                    ).strip()
                    if normalized_project and frozen_project and normalized_project != frozen_project:
                        continue
                    if (
                        normalized_requirement
                        and frozen_requirement
                        and normalized_requirement != frozen_requirement
                    ):
                        continue
                else:
                    if normalized_project:
                        execution.project_dir = normalized_project
                    if normalized_requirement:
                        execution.requirement_name = normalized_requirement
                accepted_update = True
                registered_transition = self._register_execution_generation_if_ready(execution)
                if registered_transition is not None:
                    registered_context = (
                        str(execution.project_dir or "").strip(),
                        str(execution.requirement_name or "").strip(),
                    )
        if registered_transition is not None:
            current_action, current_stage_seq, current_runner_id = registered_transition
            if registered_context is not None:
                self._set_context(
                    project_dir=registered_context[0],
                    requirement_name=registered_context[1],
                )
            self._emit_display_stage_state(
                preferred_status="running",
                preferred_action=current_action,
                preferred_stage_seq=current_stage_seq,
                preferred_runner_id=current_runner_id,
                source="runner_start",
                message="准备智能体",
                force=True,
            )
        return accepted_update

    def _update_context_from_prompt_response(
        self,
        prompt: PendingPromptState,
        payload: Mapping[str, Any],
    ) -> None:
        marker = self._prompt_marker_text(prompt)
        if not marker:
            return
        value = str(payload.get("value", "")).strip()
        if "项目工作目录" in marker and value:
            scope_accepted = self._update_runner_execution_scope_if_unset(
                project_dir=value,
                runner_id=prompt.owner_runner_id,
            )
            if prompt.owner_runner_id and not scope_accepted:
                return
            self._set_context(
                project_dir=value,
                requirement_name=None if prompt.owner_runner_id else "",
            )
            return
        requirement_name = resolve_requirement_name_from_prompt_response(
            prompt_marker=marker,
            payload=payload,
            options=prompt.payload.get("options", ()),
        )
        if requirement_name:
            scope_accepted = self._update_runner_execution_scope_if_unset(
                requirement_name=requirement_name,
                runner_id=prompt.owner_runner_id,
            )
            if prompt.owner_runner_id and not scope_accepted:
                return
            self._set_context(requirement_name=requirement_name)

    def _resolve_project_dir(self, *, runs: Sequence[Mapping[str, Any]] | None = None) -> str:
        runner_execution = getattr(self._runner_local, "execution", None)
        if runner_execution is not None:
            runner_project_dir = str(getattr(runner_execution, "project_dir", "") or "").strip()
            return self._normalize_action_scope_project(runner_project_dir)
        if self._context.project_dir:
            return self._context.project_dir
        session = self._current_control_session()
        selection = getattr(session.center, "selection", None) if session is not None else None
        selection_project_dir = str(getattr(selection, "project_dir", "") or "").strip()
        if selection_project_dir:
            return str(Path(selection_project_dir).expanduser().resolve())
        run_options = list(runs or self._list_runs())
        for item in run_options:
            project_dir = str(item.get("project_dir", "")).strip()
            if project_dir:
                return str(Path(project_dir).expanduser().resolve())
        return ""

    def _resolve_requirement_name(self) -> str:
        runner_execution = getattr(self._runner_local, "execution", None)
        if runner_execution is not None:
            return str(getattr(runner_execution, "requirement_name", "") or "").strip()
        return str(self._context.requirement_name or "").strip()

    def _resolve_routing_project_dir(self, payload: Mapping[str, Any] | None = None) -> str:
        payload_value = str((payload or {}).get("project_dir", "")).strip()
        if payload_value:
            return str(Path(payload_value).expanduser().resolve())
        runner_execution = getattr(self._runner_local, "execution", None)
        if runner_execution is not None:
            runner_project_dir = str(getattr(runner_execution, "project_dir", "") or "").strip()
            return self._normalize_action_scope_project(runner_project_dir)
        if self._context.project_dir:
            return self._context.project_dir
        session = self._current_control_session()
        selection = getattr(session.center, "selection", None) if session is not None else None
        selection_project_dir = str(getattr(selection, "project_dir", "") or "").strip()
        if selection_project_dir:
            return str(Path(selection_project_dir).expanduser().resolve())
        return ""

    def _latest_run_store(self, *, project_dir: str = "") -> RunStore | None:
        normalized_project_dir = str(Path(project_dir).expanduser().resolve()) if project_dir else ""
        if not normalized_project_dir:
            return None
        project_path = Path(normalized_project_dir)
        if not project_path.exists() or not project_path.is_dir():
            return None
        try:
            manifest_paths = list_routing_run_manifest_paths(project_dir=normalized_project_dir)
        except Exception:
            return None
        for manifest_path in manifest_paths:
            try:
                store = RunStore.load(run_id=manifest_path.parent.name, project_dir=normalized_project_dir)
            except Exception:
                continue
            return store
        return None

    def _emit_log_error(
        self,
        *,
        action: str = "",
        title: str,
        error: Exception | str,
        traceback_text: str = "",
    ) -> None:
        resolved_action = str(action or self._context.current_action or "").strip()
        lines = [title]
        if resolved_action:
            lines.append(f"action: {resolved_action}")
        error_text = str(error).strip()
        if error_text:
            lines.append(f"error: {error_text}")
        traceback_block = str(traceback_text or "").strip()
        if traceback_block:
            lines.append(traceback_block)
        self.emit_event(
            "log.append",
            {
                "text": "\n".join(lines).rstrip() + "\n",
                "log_kind": "error",
                "action": resolved_action,
            },
        )

    def _manifest_worker_snapshot(self, entry: Any) -> dict[str, Any]:
        state_path = str(getattr(entry, "state_path", "") or "").strip()
        snapshot: dict[str, Any] = {}
        if state_path:
            snapshot = self._refresh_running_worker_snapshot_if_needed(state_path)
        session_name = str(snapshot.get("session_name") or getattr(entry, "session_name", "") or "").strip()
        session_exists = bool(snapshot.get("session_exists")) if session_name else False
        status = str(snapshot.get("status") or getattr(entry, "result_status", "") or "pending").strip()
        health_status = str(snapshot.get("health_status") or getattr(entry, "health_status", "") or "unknown").strip()
        health_note = str(snapshot.get("health_note") or getattr(entry, "health_note", "") or "").strip()
        note = str(snapshot.get("note") or getattr(entry, "note", "") or "").strip()
        current_task_runtime_status = str(
            snapshot.get("current_task_runtime_status") or getattr(entry, "current_task_runtime_status", "") or ""
        ).strip()
        turn_status_path = str(
            snapshot.get("turn_status_path") or getattr(entry, "current_turn_status_path", "") or ""
        ).strip()
        agent_started = bool(snapshot.get("agent_started", getattr(entry, "agent_started", False)))
        agent_state = _normalize_worker_agent_state(
            {
                **snapshot,
                "agent_state": snapshot.get("agent_state") or getattr(entry, "agent_state", ""),
                "agent_started": agent_started,
            }
        )
        prelaunch_active = worker_state_is_prelaunch_active(
            {
                **snapshot,
                "agent_state": agent_state,
                "agent_started": agent_started,
                "health_status": health_status,
                "note": note,
                "result_status": status,
                "status": status,
                "workflow_stage": str(
                    snapshot.get("workflow_stage")
                    or getattr(entry, "workflow_stage", "")
                    or "pending"
                ).strip(),
            }
        )
        if session_name and not session_exists and not prelaunch_active:
            with contextlib.suppress(Exception):
                session_exists = bool(self._tmux_runtime.session_exists(session_name))
        agent_state, health_status, health_note = _normalize_worker_session_state(
            session_name=session_name,
            session_exists=session_exists,
            status=status,
            agent_state=agent_state,
            agent_started=agent_started,
            pane_id=str(snapshot.get("pane_id") or getattr(entry, "pane_id", "") or "").strip(),
            workflow_stage=str(snapshot.get("workflow_stage") or getattr(entry, "workflow_stage", "") or "pending").strip(),
            note=note,
            health_status=health_status,
            health_note=health_note,
        )
        return _normalize_routing_worker_snapshot({
            "session_name": session_name,
            "work_dir": str(snapshot.get("work_dir") or getattr(entry, "work_dir", "") or "").strip(),
            "status": status,
            "workflow_stage": str(snapshot.get("workflow_stage") or getattr(entry, "workflow_stage", "") or "pending").strip(),
            "agent_state": agent_state,
            "health_status": health_status,
            "health_note": health_note,
            "current_task_runtime_status": current_task_runtime_status,
            "retry_count": int(snapshot.get("retry_count") or getattr(entry, "retry_count", 0) or 0),
            "note": note,
            "transcript_path": str(snapshot.get("transcript_path") or getattr(entry, "transcript_path", "") or "").strip(),
            "turn_status_path": turn_status_path,
            "question_path": str(snapshot.get("question_path", "") or "").strip(),
            "answer_path": str(snapshot.get("answer_path", "") or "").strip(),
            "artifact_paths": list(snapshot.get("artifact_paths", [])),
            "session_exists": session_exists,
            "updated_at": str(snapshot.get("updated_at") or "").strip(),
        })

    def _infer_workflow_a00_stage_label(self, project_dir: str, requirement_name: str) -> str:
        if not project_dir:
            return "路由初始化"
        try:
            routing_paths = required_routing_layer_paths(project_dir)
        except Exception:
            return "路由初始化"
        if any(not Path(path).exists() for path in routing_paths):
            return "路由初始化"
        if not requirement_name:
            return "需求录入"
        try:
            original_requirement_path, requirements_clear_path, _, _ = build_requirements_clarification_paths(project_dir, requirement_name)
        except Exception:
            return "需求录入"
        if not _file_has_content(original_requirement_path):
            return "需求录入"
        if not _file_has_content(requirements_clear_path):
            return "需求澄清"
        try:
            review_paths = build_requirements_review_paths(
                project_dir,
                requirement_name,
                ensure_hitl_record=False,
            )
        except Exception:
            return "需求评审"
        if not _file_has_content(review_paths["merged_review_path"]):
            return "需求评审"
        try:
            record_path = build_pre_development_task_record_path(project_dir, requirement_name)
        except Exception:
            return "详细设计"
        if not record_path.exists():
            return "详细设计"
        try:
            record_payload = load_pre_development_task_record(record_path)
        except Exception:
            return "详细设计"
        if not bool(record_payload.get("详细设计", {}).get("详细设计")):
            return "详细设计"
        if not bool(record_payload.get("任务拆分", {}).get("任务拆分")):
            return "任务拆分"
        try:
            development_paths = build_development_paths(project_dir, requirement_name)
        except Exception:
            return "任务开发"
        task_json_path = development_paths["task_json_path"]
        if not is_task_progress_json(task_json_path):
            return "任务开发"
        if get_first_false_task(task_json_path) is not None:
            return "任务开发"
        try:
            overall_review_paths = build_overall_review_paths(project_dir, requirement_name)
        except Exception:
            return "复核"
        return "测试" if overall_review_passed(overall_review_paths["state_path"]) else "复核"

    def _resolve_stage_label(self, *, action: str, project_dir: str, requirement_name: str) -> str:
        normalized_action = str(action or "").strip()
        if not normalized_action or normalized_action == "idle":
            return "等待中"
        if normalized_action == "workflow.a00.start":
            return self._infer_workflow_a00_stage_label(project_dir, requirement_name)
        return STAGE_LABEL_BY_ACTION.get(normalized_action, "等待中")

    def emit_event(self, event_type: str, payload: Mapping[str, Any] | None = None) -> None:
        message = build_event(event_type, payload)
        with self._event_lock:
            listeners = tuple(self._event_subscribers)
        for listener in listeners:
            listener(message)

    def emit_response(
        self,
        request_id: str,
        *,
        ok: bool,
        payload: Mapping[str, Any] | None = None,
        error: str = "",
    ) -> None:
        emitter = self._response_emitter
        request_id_text = str(request_id or "").strip()
        if emitter is None or not request_id_text:
            return
        with self._response_emission_lock:
            if request_id_text in self._emitted_response_ids:
                return
            emitter(build_response(request_id_text, ok=ok, payload=payload, error=error))
            self._emitted_response_ids[request_id_text] = None
            while len(self._emitted_response_ids) > 4096:
                oldest_request_id = next(iter(self._emitted_response_ids))
                self._emitted_response_ids.pop(oldest_request_id, None)

    def protocol_log_sink(self) -> BridgeLogSink:
        return self._protocol_log_sink

    def _set_context(
        self,
        *,
        project_dir: str | None = None,
        requirement_name: str | None = None,
        action: str | None = None,
    ) -> None:
        previous_artifact_scope = self._current_artifact_index_scope()
        if project_dir is not None and str(project_dir).strip():
            self._context.project_dir = str(Path(project_dir).expanduser().resolve())
        if requirement_name is not None:
            self._context.requirement_name = str(requirement_name).strip()
        if action is not None and str(action).strip():
            self._context.current_action = str(action).strip()
        if self._current_artifact_index_scope() != previous_artifact_scope:
            self._reset_artifact_index_for_current_context()

    @staticmethod
    def _parse_stage_args_for_action(action: str, argv: Sequence[str]) -> argparse.Namespace | None:
        normalized = str(action or "").strip()
        parser_builders = {
            "workflow.a00.start": build_a00_parser,
            "stage.a01.start": build_a01_parser,
            "stage.a02.start": build_a02_parser,
            "stage.a03.start": build_a03_parser,
            "stage.a04.start": build_a04_parser,
            "stage.a05.start": build_a05_parser,
            "stage.a06.start": build_a06_parser,
            "stage.a07.start": build_a07_parser,
            "stage.a08.start": build_a08_parser,
        }
        builder = parser_builders.get(normalized)
        if builder is None:
            return None
        return builder().parse_args(list(argv))

    def _update_context_from_stage_args(self, action: str, argv: Sequence[str]) -> None:
        try:
            args = self._parse_stage_args_for_action(action, argv)
            if args is None:
                return
        except Exception:
            return
        self._set_context(
            project_dir=str(getattr(args, "project_dir", "") or "").strip() or None,
            requirement_name=str(getattr(args, "requirement_name", "") or "").strip(),
            action=action,
        )

    def _update_context_from_result(self, result: Any, *, action: str) -> None:
        resolved_action = action
        if action == "workflow.a00.start":
            current_action = str(self._context.current_action or "").strip()
            if current_action and current_action != action:
                resolved_action = current_action
        if isinstance(result, Mapping):
            self._update_runner_execution_scope_if_unset(
                project_dir=str(result.get("project_dir", "")).strip() or None,
                requirement_name=str(result.get("requirement_name", "")).strip() or None,
            )
            self._set_context(
                project_dir=str(result.get("project_dir", "")).strip() or None,
                requirement_name=str(result.get("requirement_name", "")).strip() or None,
                action=resolved_action,
            )
            return
        self._update_runner_execution_scope_if_unset(
            project_dir=str(getattr(result, "project_dir", "") or "").strip() or None,
            requirement_name=str(getattr(result, "requirement_name", "") or "").strip() or None,
        )
        self._set_context(
            project_dir=str(getattr(result, "project_dir", "") or "").strip() or None,
            requirement_name=str(getattr(result, "requirement_name", "") or "").strip() or None,
            action=resolved_action,
        )

    def _scan_runtime_workers(self, runtime_root: str | Path, *, refresh_health: bool | None = None) -> list[dict[str, Any]]:
        root = Path(runtime_root).expanduser().resolve()
        if not root.exists() or not root.is_dir():
            return []
        should_refresh_health = self._snapshot_refresh_worker_health if refresh_health is None else bool(refresh_health)
        cache_key = (str(root), should_refresh_health)
        scan_cache = self._snapshot_runtime_scan_cache
        if scan_cache is not None:
            cached = scan_cache.get(cache_key)
            if cached is not None:
                return [dict(item) for item in cached]
        workers: list[dict[str, Any]] = []
        for state_path in self._iter_worker_state_paths(root):
            if should_refresh_health:
                snapshot = self._refresh_running_worker_snapshot_if_needed(state_path)
            else:
                snapshot = _read_worker_state_snapshot(
                    state_path,
                    trust_persisted_session=True,
                )
            if snapshot and not _worker_snapshot_is_stale_missing_session_live_noise(snapshot):
                workers.append(snapshot)
        if scan_cache is not None:
            scan_cache[cache_key] = [dict(item) for item in workers]
        return workers

    def _scan_runtime_workers_without_runtime_io(self, runtime_root: str | Path) -> list[dict[str, Any]]:
        root = Path(runtime_root).expanduser().resolve()
        if not root.exists() or not root.is_dir():
            return []
        workers: list[dict[str, Any]] = []
        for state_path in self._iter_worker_state_paths(root):
            snapshot = _read_worker_state_snapshot(state_path, trust_persisted_session=True)
            if snapshot and not _worker_snapshot_is_stale_missing_session_live_noise(snapshot):
                workers.append(snapshot)
        return workers

    @staticmethod
    def _iter_worker_state_paths(root: Path) -> list[Path]:
        state_paths: list[Path] = []
        pending = [root]
        while pending:
            current = pending.pop()
            try:
                with os.scandir(current) as entries_iter:
                    entries = list(entries_iter)
            except OSError:
                continue
            for entry in entries:
                try:
                    if entry.name == "_locks":
                        continue
                    if entry.name == "worker.state.json" and entry.is_file(follow_symlinks=False):
                        state_paths.append(Path(entry.path))
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        pending.append(Path(entry.path))
                except OSError:
                    continue
        return sorted(state_paths)

    def _session_context_resolver(self) -> Callable[[str, Mapping[str, Any], str | Path], bool] | None:
        session_exists = getattr(self._tmux_runtime, "session_exists", None)
        if isinstance(self._tmux_runtime, TmuxRuntimeController):
            bound_func = getattr(session_exists, "__func__", None)
            if bound_func is not TmuxRuntimeController.session_exists:
                return None
        resolver = getattr(self._tmux_runtime, "session_matches_worker_state", None)
        return resolver if callable(resolver) else None

    def _state_identity_resolver(self) -> Callable[[Mapping[str, Any], str | Path], Mapping[str, Any]] | None:
        resolver = getattr(self._tmux_runtime, "worker_identity_for_runtime_dir", None)
        if not callable(resolver):
            return None

        def _resolve(_state: Mapping[str, Any], state_path: str | Path) -> Mapping[str, Any]:
            return resolver(Path(state_path).expanduser().resolve().parent)

        return _resolve

    def _refresh_running_worker_snapshot_if_needed(self, state_path: str | Path) -> dict[str, Any]:
        snapshot = _read_worker_state_snapshot(
            state_path,
            session_exists_resolver=self._tmux_runtime.session_exists,
            session_context_resolver=self._session_context_resolver(),
            state_identity_resolver=self._state_identity_resolver(),
        )
        if not snapshot:
            return {}
        if worker_state_is_prelaunch_active(snapshot):
            return snapshot
        status = str(snapshot.get("status") or snapshot.get("result_status") or "").strip()
        agent_state = str(snapshot.get("agent_state", "")).strip().upper()
        health_status = str(snapshot.get("health_status", "")).strip().lower()
        session_name = str(snapshot.get("session_name", "")).strip()
        backend = getattr(self._tmux_runtime, "backend", None)
        session_exists = bool(snapshot.get("session_exists"))
        stale_dead_with_live_session = agent_state == "DEAD" and session_exists
        should_refresh = (
            status in {"running", "pending"}
            or agent_state in {"BUSY", "STARTING"}
            or stale_dead_with_live_session
        )
        if (
            not should_refresh
            or (health_status == "dead" and not stale_dead_with_live_session)
            or not session_name
            or backend is None
        ):
            return snapshot
        worker = load_worker_from_state_path(
            state_path,
            backend=backend,
            passive_health=True,
        )
        if worker is None:
            return snapshot
        with contextlib.suppress(Exception):
            worker.refresh_health(notify_on_change=False)
            snapshot = _read_worker_state_snapshot(
                state_path,
                session_exists_resolver=self._tmux_runtime.session_exists,
                session_context_resolver=self._session_context_resolver(),
                state_identity_resolver=self._state_identity_resolver(),
            )
            if snapshot:
                return snapshot
        return snapshot

    def _list_runs(self) -> list[dict[str, Any]]:
        project_dir = self._resolve_routing_project_dir()
        if not project_dir:
            return []
        project_path = Path(project_dir).expanduser().resolve()
        if not project_path.exists() or not project_path.is_dir():
            return []
        options: list[dict[str, Any]] = []
        try:
            manifest_paths = list_routing_run_manifest_paths(project_dir=str(project_path))
        except Exception:
            return []
        for manifest_path in manifest_paths:
            try:
                store = RunStore.load(run_id=manifest_path.parent.name, project_dir=str(project_path))
            except Exception:
                continue
            workers = list(store.manifest.workers)
            options.append(
                {
                    "run_id": store.manifest.run_id,
                    "runtime_dir": store.manifest.runtime_dir,
                    "project_dir": store.manifest.project_dir,
                    "status": store.manifest.status,
                    "updated_at": store.manifest.updated_at,
                    "worker_count": len(workers),
                    "failed_count": sum(1 for item in workers if item.result_status in {"failed", "stale_failed"}),
                }
            )
        return options

    def _build_control_snapshot_for_session(self, session: ControlSessionState | None) -> dict[str, Any]:
        if session is None:
            return {
                "supported": True,
                "control_id": "",
                "run_id": "",
                "runtime_dir": "",
                "status_text": "当前没有激活的 routing run。",
                "help_text": render_control_help(),
                "workers": [],
                "done": False,
                "can_switch_runs": True,
                "final_summary": "",
                "transition_text": "",
            }
        center = session.center
        if self._snapshot_refresh_worker_health:
            center.refresh_worker_health()
        if center.all_done() and session.final_result is None:
            session.final_result = center.wait_until_complete()
            session.transition_text = center.transition_to_requirements_phase(session.final_result)
        build_workers = getattr(center, "build_worker_snapshots", None)
        worker_snapshots = build_workers() if callable(build_workers) else _serialize(getattr(center, "build_status_rows")())
        worker_snapshots = [_normalize_routing_worker_snapshot(worker) for worker in worker_snapshots]
        return {
            "supported": True,
            "control_id": session.control_id,
            "run_id": center.run_id,
            "runtime_dir": str(center.run_root),
            "status_text": center.render_status(),
            "help_text": render_control_help(),
            "workers": worker_snapshots,
            "done": session.final_result is not None,
            "can_switch_runs": center.can_switch_runs(),
            "final_summary": format_batch_summary(session.final_result) if session.final_result is not None else "",
            "transition_text": session.transition_text,
        }

    def _current_control_session(self) -> ControlSessionState | None:
        with self._controls_lock:
            if self._active_control_id and self._active_control_id in self._controls:
                return self._controls[self._active_control_id]
            if self._controls:
                latest = next(reversed(list(self._controls.keys())))
                self._active_control_id = latest
                return self._controls[latest]
        return None

    def _build_routing_snapshot(self) -> dict[str, Any]:
        project_dir = self._resolve_project_dir()
        files = []
        if project_dir:
            try:
                files = [_build_file_snapshot(path, label=path.name) for path in required_routing_layer_paths(project_dir)]
            except Exception:
                files = []
        session = self._current_control_session()
        control_snapshot = self._build_control_snapshot_for_session(session)
        workers = list(control_snapshot.get("workers", []))
        status_text = str(control_snapshot.get("status_text", "")).strip()
        done = bool(control_snapshot.get("done", False))
        if not workers and not self._routing_manifest_workers_suppressed(project_dir):
            store = self._latest_run_store(project_dir=project_dir)
            if store is not None:
                workers = [self._manifest_worker_snapshot(entry) for entry in store.manifest.workers]
                status_text = store.manifest.status or status_text
                done = store.manifest.status == "completed"
        return {
            "project_dir": project_dir,
            "files": files,
            "workers": workers,
            "status_text": status_text,
            "done": done,
        }

    def _build_requirements_snapshot(self) -> dict[str, Any]:
        project_dir = self._resolve_project_dir()
        requirement_name = self._resolve_requirement_name()
        active_action = str(self._display_action or self._context.current_action or "").strip()
        files: list[dict[str, Any]] = []
        workers: list[dict[str, Any]] = []
        if project_dir:
            if requirement_name:
                try:
                    output_path, question_path, record_path = build_notion_hitl_paths(project_dir, requirement_name)
                    _, requirements_clear_path, ask_human_path, hitl_record_path = build_requirements_clarification_paths(project_dir, requirement_name)
                    files = [
                        _build_file_snapshot(output_path, label="原始需求"),
                        _build_file_snapshot(requirements_clear_path, label="需求澄清"),
                        _build_file_snapshot(question_path, label="需求录入 HITL 问题"),
                        _build_file_snapshot(record_path, label="需求录入 HITL 记录"),
                        _build_file_snapshot(ask_human_path, label="需求澄清提问"),
                        _build_file_snapshot(hitl_record_path, label="需求澄清记录"),
                    ]
                except Exception:
                    files = []
            else:
                try:
                    files = [_build_file_snapshot(Path(project_dir) / f"{name}_原始需求.md", label=name) for name in list_existing_requirements(project_dir)]
                except Exception:
                    files = []
            if active_action == "stage.a02.start":
                workers = self._scan_requirement_intake_workers(project_dir)
            elif active_action == "stage.a03.start":
                workers = self._scan_requirement_clarification_workers(project_dir)
            else:
                workers = _merge_worker_snapshots(
                    self._scan_requirement_intake_workers(project_dir),
                    self._scan_requirement_clarification_workers(project_dir),
                )
            if active_action in {"stage.a02.start", "stage.a03.start"}:
                workers = self._filter_workers_for_current_context(workers, active_action)
        return {
            "project_dir": project_dir,
            "requirement_name": requirement_name,
            "files": files,
            "workers": workers,
        }

    def _scan_requirement_intake_workers(self, project_dir: str | Path) -> list[dict[str, Any]]:
        return self._scan_runtime_workers(Path(project_dir) / NOTION_RUNTIME_ROOT_NAME)

    def _scan_requirement_clarification_workers(self, project_dir: str | Path) -> list[dict[str, Any]]:
        return _merge_worker_snapshots(
            self._scan_runtime_workers(Path(project_dir) / REQUIREMENTS_RUNTIME_ROOT_NAME),
            self._scan_runtime_workers(Path(project_dir) / LEGACY_REQUIREMENTS_RUNTIME_ROOT_NAME),
        )

    def _scan_review_handoff_analyst_workers(self, project_dir: str | Path, requirement_name: str) -> list[dict[str, Any]]:
        project_root = Path(project_dir).expanduser().resolve()
        requirement_text = str(requirement_name or "").strip()
        handoff_workers: list[dict[str, Any]] = []
        for worker in self._scan_requirement_clarification_workers(project_root):
            worker_id = str(worker.get("worker_id", "")).strip().lower()
            session_name = str(worker.get("session_name", "")).strip()
            if worker_id != "requirements-analyst" and not (
                session_name.startswith("需求分析师-") or session_name.startswith("分析师-")
            ):
                continue
            worker_requirement = str(worker.get("requirement_name", "")).strip()
            if worker_requirement and requirement_text and worker_requirement != requirement_text:
                continue
            worker_project = str(worker.get("project_dir", "") or worker.get("work_dir", "")).strip()
            if worker_project:
                with contextlib.suppress(Exception):
                    worker_project = str(Path(worker_project).expanduser().resolve())
                if worker_project != str(project_root):
                    continue
            if not self._worker_snapshot_has_live_session(worker):
                continue
            snapshot = dict(worker)
            if self._worker_snapshot_is_requirements_review_feedback(snapshot):
                snapshot.setdefault("project_dir", str(project_root))
                if requirement_text:
                    snapshot["requirement_name"] = requirement_text
                snapshot["workflow_action"] = "stage.a04.start"
            handoff_workers.append(snapshot)
        return handoff_workers

    def _scan_current_design_workers(self, project_dir: str | Path) -> list[dict[str, Any]]:
        workers = _merge_worker_snapshots(
            self._scan_runtime_workers(Path(project_dir) / DETAILED_DESIGN_RUNTIME_ROOT_NAME),
            self._scan_runtime_workers(Path(project_dir) / REQUIREMENTS_REVIEW_RUNTIME_ROOT_NAME),
            self._scan_runtime_workers(Path(project_dir) / REQUIREMENTS_RUNTIME_ROOT_NAME),
        )
        filtered = _filter_worker_snapshots(
            workers,
            allowed_worker_ids=(
                "detailed-design-analyst",
                "requirements-review-analyst",
                "requirements-analyst",
            ),
            allowed_worker_id_prefixes=("detailed-design-review-",),
            allowed_session_prefixes=(
                "需求分析师-",
                "分析师-",
                "开发工程师-",
                "测试工程师-",
                "架构师-",
                "审核员-",
            ),
        )
        current_workers: list[dict[str, Any]] = []
        for snapshot in filtered:
            worker_id = str(snapshot.get("worker_id", "")).strip().lower()
            status = str(snapshot.get("status", "")).strip().lower()
            if worker_id == "requirements-analyst" and status not in {"running", "pending"}:
                continue
            current_workers.append(snapshot)
        return current_workers

    def _scan_current_design_status_workers(self, project_dir: str | Path) -> list[dict[str, Any]]:
        workers = self._scan_runtime_workers(Path(project_dir) / DETAILED_DESIGN_RUNTIME_ROOT_NAME)
        return _filter_worker_snapshots(
            workers,
            allowed_worker_ids=("detailed-design-analyst",),
            allowed_worker_id_prefixes=("detailed-design-review-",),
            allowed_session_prefixes=(
                "需求分析师-",
                "开发工程师-",
                "测试工程师-",
                "架构师-",
                "审核员-",
            ),
        )

    def _scan_design_handoff_analyst_workers(self, project_dir: str | Path, requirement_name: str) -> list[dict[str, Any]]:
        project_root = Path(project_dir).expanduser().resolve()
        requirement_text = str(requirement_name or "").strip()
        workers = _merge_worker_snapshots(
            self._scan_runtime_workers(project_root / REQUIREMENTS_REVIEW_RUNTIME_ROOT_NAME, refresh_health=False),
            self._scan_runtime_workers(project_root / REQUIREMENTS_RUNTIME_ROOT_NAME, refresh_health=False),
            self._scan_runtime_workers(project_root / LEGACY_REQUIREMENTS_RUNTIME_ROOT_NAME, refresh_health=False),
        )
        handoff_workers: list[dict[str, Any]] = []
        for worker in workers:
            worker_id = str(worker.get("worker_id", "")).strip().lower()
            session_name = str(worker.get("session_name", "")).strip()
            if worker_id not in {"requirements-analyst", "requirements-review-analyst"} and not (
                session_name.startswith("需求分析师-") or session_name.startswith("分析师-")
            ):
                continue
            worker_requirement = str(worker.get("requirement_name", "")).strip()
            if worker_requirement and requirement_text and worker_requirement != requirement_text:
                continue
            workflow_action = str(worker.get("workflow_action", "")).strip()
            if workflow_action and workflow_action != "stage.a05.start":
                continue
            worker_project = str(worker.get("project_dir", "") or worker.get("work_dir", "")).strip()
            if worker_project:
                with contextlib.suppress(Exception):
                    worker_project = str(Path(worker_project).expanduser().resolve())
                if worker_project != str(project_root):
                    continue
            design_markers = " ".join(
                str(worker.get(key, "")).strip()
                for key in (
                    "note",
                    "current_turn_phase",
                    "turn_status_path",
                    "current_task_runtime_status",
                )
            )
            artifact_markers = " ".join(str(item) for item in worker.get("artifact_paths", []))
            marker_text = f"{design_markers} {artifact_markers}".lower()
            if "detailed_design" not in marker_text and "detailed-design" not in marker_text and "详细设计" not in marker_text:
                continue
            has_design_scope = workflow_action == "stage.a05.start" or (
                bool(worker_requirement)
                and bool(requirement_text)
                and worker_requirement == requirement_text
            )
            has_artifact_evidence = bool(str(artifact_markers).strip())
            if (
                not has_design_scope
                and _worker_snapshot_is_actively_running(worker)
                and not has_artifact_evidence
            ):
                continue
            if not self._worker_snapshot_has_live_session(worker):
                continue
            snapshot = dict(worker)
            snapshot.setdefault("project_dir", str(project_root))
            if requirement_text and not str(snapshot.get("requirement_name", "")).strip():
                snapshot["requirement_name"] = requirement_text
            if not str(snapshot.get("workflow_action", "")).strip():
                snapshot["workflow_action"] = "stage.a05.start"
            handoff_workers.append(snapshot)
        return handoff_workers

    def _scan_current_task_split_workers(self, project_dir: str | Path) -> list[dict[str, Any]]:
        workers = _merge_worker_snapshots(
            self._scan_runtime_workers(Path(project_dir) / TASK_SPLIT_RUNTIME_ROOT_NAME),
            self._scan_runtime_workers(Path(project_dir) / DETAILED_DESIGN_RUNTIME_ROOT_NAME),
            self._scan_runtime_workers(Path(project_dir) / REQUIREMENTS_REVIEW_RUNTIME_ROOT_NAME),
        )
        current_workers: list[dict[str, Any]] = []
        for snapshot in workers:
            worker_id = str(snapshot.get("worker_id", "")).strip().lower()
            note = str(snapshot.get("note", "")).strip().lower()
            phase = str(snapshot.get("current_turn_phase", "")).strip()
            if worker_id == "task-split-analyst" or worker_id.startswith("task-split-review-"):
                current_workers.append(dict(snapshot))
                continue
            if worker_id == "requirements-review-analyst":
                if phase == "任务拆分" or "task_split" in note or "task-split" in note or "generate_task_split" in note:
                    current_workers.append(dict(snapshot))
                continue
            if worker_id == "detailed-design-analyst":
                if phase == "任务拆分" or "task_split" in note or "task-split" in note:
                    current_workers.append(dict(snapshot))
                continue
            if worker_id.startswith("detailed-design-review-"):
                if phase == "任务拆分" or "task_split" in note or "task-split" in note:
                    current_workers.append(dict(snapshot))
        return current_workers

    def _scan_current_development_workers(self, project_dir: str | Path) -> list[dict[str, Any]]:
        workers = self._scan_runtime_workers(Path(project_dir) / DEVELOPMENT_RUNTIME_ROOT_NAME)
        return _filter_worker_snapshots(
            workers,
            allowed_worker_ids=("development-developer",),
            allowed_worker_id_prefixes=("development-review-",),
            allowed_session_prefixes=(
                "开发工程师-",
            ),
        )

    @staticmethod
    def _overall_review_worker_has_artifact_evidence(
        worker: Mapping[str, Any],
        *,
        project_dir: str,
        requirement_name: str,
    ) -> bool:
        safe_requirement = sanitize_requirement_name(requirement_name)
        artifact_paths = worker.get("artifact_paths", [])
        artifact_text = ""
        if isinstance(artifact_paths, Sequence) and not isinstance(artifact_paths, (str, bytes)):
            artifact_text = " ".join(str(item) for item in artifact_paths)
        marker_text = " ".join(
            str(worker.get(key, "")).strip()
            for key in (
                "note",
                "current_turn_phase",
                "turn_status_path",
                "current_task_runtime_status",
                "workflow_stage",
                "transcript_path",
            )
        )
        marker_text = f"{marker_text} {artifact_text}".lower()
        if any(
            marker in marker_text
            for marker in (
                "overall_review",
                "overall-review",
                "整体复核",
                "复核阶段",
                f"{safe_requirement}_整体复核记录_".lower(),
                f"{safe_requirement}_整体代码复核记录_".lower(),
            )
        ):
            return True
        session_name = str(worker.get("session_name", "")).strip()
        if not session_name or not project_dir or not requirement_name:
            return False
        with contextlib.suppress(Exception):
            review_md_path, review_json_path = build_overall_review_reviewer_artifact_paths(
                project_dir,
                requirement_name,
                session_name,
            )
            return review_md_path.exists() or review_json_path.exists()
        return False

    def _scan_current_overall_review_workers(self, project_dir: str | Path) -> list[dict[str, Any]]:
        requirement_name = self._resolve_requirement_name()
        project_text = str(Path(project_dir).expanduser().resolve())
        workers = self._scan_current_development_workers(project_dir)
        scoped = self._filter_workers_for_current_context(workers, "stage.a08.start")
        current_workers: list[dict[str, Any]] = []
        for worker in scoped:
            workflow_action = str(worker.get("workflow_action", "")).strip()
            if workflow_action == "stage.a08.start":
                current_workers.append(dict(worker))
                continue
            if workflow_action:
                continue
            if self._overall_review_worker_has_artifact_evidence(
                worker,
                project_dir=project_text,
                requirement_name=requirement_name,
            ):
                current_workers.append(dict(worker))
        return current_workers

    def _build_review_snapshot(self) -> dict[str, Any]:
        project_dir = self._resolve_project_dir()
        requirement_name = self._resolve_requirement_name()
        files: list[dict[str, Any]] = []
        workers: list[dict[str, Any]] = []
        blockers: list[str] = []
        if project_dir and requirement_name:
            try:
                paths = build_requirements_review_paths(
                    project_dir,
                    requirement_name,
                    ensure_hitl_record=False,
                )
                files = [
                    _build_file_snapshot(paths["merged_review_path"], label="合并评审记录"),
                    _build_file_snapshot(paths["ba_feedback_path"], label="BA 反馈"),
                    _build_file_snapshot(paths["ask_human_path"], label="评审提问"),
                    _build_file_snapshot(paths["hitl_record_path"], label="需求澄清记录"),
                ]
                if not Path(paths["merged_review_path"]).exists():
                    blockers.append("merged_review_missing")
            except Exception:
                files = []
            workers = self._filter_workers_for_current_context(
                self._scan_runtime_workers(Path(project_dir) / REQUIREMENTS_REVIEW_RUNTIME_ROOT_NAME),
                "stage.a04.start",
            )
            workers = _merge_worker_snapshots(
                workers,
                self._scan_review_handoff_analyst_workers(project_dir, requirement_name),
            )
        return {
            "project_dir": project_dir,
            "requirement_name": requirement_name,
            "files": files,
            "workers": workers,
            "blockers": blockers,
        }

    def _build_design_snapshot(self) -> dict[str, Any]:
        project_dir = self._resolve_project_dir()
        requirement_name = self._resolve_requirement_name()
        files: list[dict[str, Any]] = []
        workers: list[dict[str, Any]] = []
        blockers: list[str] = []
        if project_dir and requirement_name:
            try:
                paths = build_detailed_design_paths(project_dir, requirement_name)
                files = [
                    _build_file_snapshot(paths["detailed_design_path"], label="详细设计"),
                    _build_file_snapshot(paths["merged_review_path"], label="合并详设评审记录"),
                    _build_file_snapshot(paths["ba_feedback_path"], label="需求分析师反馈"),
                    _build_file_snapshot(paths["ask_human_path"], label="详设提问"),
                    _build_file_snapshot(paths["hitl_record_path"], label="需求澄清记录"),
                ]
                if not Path(paths["detailed_design_path"]).exists():
                    blockers.append("detailed_design_missing")
                if not Path(paths["merged_review_path"]).exists():
                    blockers.append("merged_design_review_missing")
            except Exception:
                files = []
            workers = _merge_worker_snapshots(
                self._filter_workers_for_current_context(
                    self._scan_runtime_workers(Path(project_dir) / DETAILED_DESIGN_RUNTIME_ROOT_NAME),
                    "stage.a05.start",
                ),
                self._scan_design_handoff_analyst_workers(project_dir, requirement_name),
            )
        return {
            "project_dir": project_dir,
            "requirement_name": requirement_name,
            "files": files,
            "workers": workers,
            "blockers": blockers,
        }

    def _build_task_split_snapshot(self) -> dict[str, Any]:
        project_dir = self._resolve_project_dir()
        requirement_name = self._resolve_requirement_name()
        files: list[dict[str, Any]] = []
        workers: list[dict[str, Any]] = []
        blockers: list[str] = []
        if project_dir and requirement_name:
            try:
                paths = build_task_split_paths(project_dir, requirement_name)
                files = [
                    _build_file_snapshot(paths["task_md_path"], label="任务单"),
                    _build_file_snapshot(paths["task_json_path"], label="任务单 JSON"),
                    _build_file_snapshot(paths["merged_review_path"], label="合并任务单评审记录"),
                    _build_file_snapshot(paths["ba_feedback_path"], label="需求分析师反馈"),
                    _build_file_snapshot(paths["ask_human_path"], label="任务拆分提问"),
                    _build_file_snapshot(paths["detailed_design_path"], label="详细设计"),
                ]
                if not get_markdown_content(paths["task_md_path"]).strip():
                    blockers.append("task_split_missing")
                if not Path(paths["task_json_path"]).exists():
                    blockers.append("task_split_json_missing")
                if not Path(paths["merged_review_path"]).exists():
                    blockers.append("merged_task_split_review_missing")
            except Exception:
                files = []
            worker_collections = (
                self._scan_runtime_workers(Path(project_dir) / TASK_SPLIT_RUNTIME_ROOT_NAME),
                self._scan_runtime_workers(Path(project_dir) / DETAILED_DESIGN_RUNTIME_ROOT_NAME),
                self._scan_runtime_workers(Path(project_dir) / REQUIREMENTS_REVIEW_RUNTIME_ROOT_NAME),
            )
            workers = self._filter_workers_for_current_context(_merge_worker_snapshots(*worker_collections), "stage.a06.start")
        return {
            "project_dir": project_dir,
            "requirement_name": requirement_name,
            "files": files,
            "workers": workers,
            "blockers": blockers,
        }

    def _build_development_snapshot(self) -> dict[str, Any]:
        project_dir = self._resolve_project_dir()
        requirement_name = self._resolve_requirement_name()
        files: list[dict[str, Any]] = []
        workers: list[dict[str, Any]] = []
        blockers: list[str] = []
        task_progress_snapshot = {
            "milestones": [],
            "current_milestone_key": "",
            "all_tasks_completed": False,
        }
        if project_dir and requirement_name:
            try:
                paths = build_development_paths(project_dir, requirement_name)
                files = [
                    _build_file_snapshot(paths["task_md_path"], label="任务单"),
                    _build_file_snapshot(paths["task_json_path"], label="任务单 JSON"),
                    _build_file_snapshot(paths["ask_human_path"], label="与人类交流"),
                    _build_file_snapshot(paths["developer_output_path"], label="工程师开发内容"),
                    _build_file_snapshot(paths["merged_review_path"], label="合并代码评审记录"),
                    _build_file_snapshot(paths["detailed_design_path"], label="详细设计"),
                ]
                if not Path(paths["task_md_path"]).exists():
                    blockers.append("task_split_missing")
                if not is_task_progress_json(paths["task_json_path"]):
                    blockers.append("task_json_invalid")
                else:
                    task_progress_snapshot = _build_task_progress_snapshot(paths["task_json_path"])
                if not Path(paths["merged_review_path"]).exists():
                    blockers.append("merged_code_review_missing")
            except Exception:
                files = []
            workers = self._filter_workers_for_current_context(
                self._scan_runtime_workers(Path(project_dir) / DEVELOPMENT_RUNTIME_ROOT_NAME),
                "stage.a07.start",
            )
        return {
            "project_dir": project_dir,
            "requirement_name": requirement_name,
            "files": files,
            "workers": workers,
            "blockers": blockers,
            "milestones": task_progress_snapshot["milestones"],
            "current_milestone_key": task_progress_snapshot["current_milestone_key"],
            "all_tasks_completed": task_progress_snapshot["all_tasks_completed"],
        }

    def _build_overall_review_snapshot(self) -> dict[str, Any]:
        project_dir = self._resolve_project_dir()
        requirement_name = self._resolve_requirement_name()
        files: list[dict[str, Any]] = []
        workers: list[dict[str, Any]] = []
        blockers: list[str] = []
        if project_dir and requirement_name:
            try:
                paths = build_overall_review_paths(project_dir, requirement_name)
                files = [
                    _build_file_snapshot(paths["original_requirement_path"], label="原始需求"),
                    _build_file_snapshot(paths["requirements_clear_path"], label="需求澄清"),
                    _build_file_snapshot(paths["detailed_design_path"], label="详细设计"),
                    _build_file_snapshot(paths["task_md_path"], label="任务单"),
                    _build_file_snapshot(paths["task_json_path"], label="任务单 JSON"),
                    _build_file_snapshot(paths["developer_output_path"], label="工程师开发内容"),
                    _build_file_snapshot(paths["merged_review_path"], label="合并复核记录"),
                    _build_file_snapshot(paths["state_path"], label="复核完成状态"),
                ]
                if not get_markdown_content(paths["original_requirement_path"]).strip():
                    blockers.append("original_requirement_missing")
                if not get_markdown_content(paths["requirements_clear_path"]).strip():
                    blockers.append("requirements_clear_missing")
                if not get_markdown_content(paths["detailed_design_path"]).strip():
                    blockers.append("detailed_design_missing")
                if not Path(paths["task_md_path"]).exists():
                    blockers.append("task_split_missing")
                if not is_task_progress_json(paths["task_json_path"]):
                    blockers.append("task_json_invalid")
                elif get_first_false_task(paths["task_json_path"]) is not None:
                    blockers.append("task_json_not_all_true")
                if not overall_review_passed(paths["state_path"]):
                    blockers.append("overall_review_not_passed")
            except Exception:
                files = []
            workers = self._scan_current_overall_review_workers(project_dir)
        return {
            "project_dir": project_dir,
            "requirement_name": requirement_name,
            "files": files,
            "workers": workers,
            "blockers": blockers,
        }

    def _build_pending_prompt_hitl_snapshot(self) -> dict[str, Any]:
        for pending in reversed(self._iter_pending_prompts()):
            if not _prompt_is_hitl(pending.payload):
                continue
            payload = pending.payload
            title = str(pending.payload.get("title", "")).strip()
            prompt_text = str(pending.payload.get("prompt_text", "")).strip()
            reason_text = str(payload.get("reason_text", payload.get("reasonText", "")) or "").strip()
            raw_target_paths = payload.get("target_paths", payload.get("targetPaths", []))
            if isinstance(raw_target_paths, Sequence) and not isinstance(raw_target_paths, (str, bytes, bytearray)):
                target_paths = [str(item).strip() for item in raw_target_paths if str(item).strip()]
            else:
                target_path = str(raw_target_paths or "").strip()
                target_paths = [target_path] if target_path else []
            reason_summary = reason_text.splitlines()[0].strip() if reason_text else ""
            summary = title or prompt_text or reason_summary or "存在待处理 HITL"
            return {
                "pending": True,
                "prompt_id": pending.prompt_id,
                "prompt_type": pending.prompt_type,
                "question_path": str(payload.get("question_path", "") or "").strip(),
                "answer_path": str(payload.get("answer_path", "") or "").strip(),
                "summary": summary,
                "attach_command": _prompt_attach_command(payload),
                "recovery_kind": str(payload.get("recovery_kind", payload.get("recoveryKind", "")) or "").strip(),
                "reason_text": reason_text,
                "target_paths": target_paths,
            }
        return {
            "pending": False,
            "prompt_id": "",
            "prompt_type": "",
            "question_path": "",
            "answer_path": "",
            "summary": "",
            "attach_command": "",
            "recovery_kind": "",
            "reason_text": "",
            "target_paths": [],
        }

    def _resolved_file_hitl_has_durable_consumption(
        self,
        *,
        action: str,
        question_path: str | Path,
    ) -> bool:
        project_dir = self._resolve_project_dir()
        requirement_name = self._resolve_requirement_name()
        audit_state = _stage_hitl_audit_answered_state(
            project_dir=project_dir,
            requirement_name=requirement_name,
            action=action,
            question_path=question_path,
        )
        if audit_state == "answered":
            return True
        if audit_state == "unanswered":
            return False
        stage_state = _read_project_stage_state_record(
            project_dir=project_dir,
            requirement_name=requirement_name,
            action=action,
        )
        if (
            str(action or "").strip() == "stage.a06.start"
            and isinstance(stage_state, Mapping)
            and str(stage_state.get("status", "")).strip() == "awaiting-input"
        ):
            return False
        return True

    def _build_hitl_snapshot(self) -> dict[str, Any]:
        prompt_snapshot = self._build_pending_prompt_hitl_snapshot()
        if prompt_snapshot.get("pending", False):
            return prompt_snapshot
        active_action = str(self._display_action or self._context.current_action or "").strip()
        runtime_snapshot = self._build_runtime_worker_hitl_snapshot(active_action)
        if runtime_snapshot.get("pending", False):
            return runtime_snapshot
        project_dir = self._resolve_project_dir()
        requirement_name = self._resolve_requirement_name()
        if not project_dir or not requirement_name:
            return {"pending": False, "question_path": "", "answer_path": "", "summary": ""}
        try:
            _, notion_question_path, notion_record_path = build_notion_hitl_paths(project_dir, requirement_name)
            _, _, ask_human_path, hitl_record_path = build_requirements_clarification_paths(project_dir, requirement_name)
        except Exception:
            return {"pending": False, "question_path": "", "answer_path": "", "summary": ""}
        try:
            development_paths = build_development_paths(project_dir, requirement_name)
            development_question_path = development_paths["ask_human_path"]
            development_answer_path = development_paths["hitl_record_path"]
        except Exception:
            development_question_path = ""
            development_answer_path = ""
        active_question = ""
        answer_path = ""
        for kind in self._file_hitl_candidate_kinds_for_action(active_action):
            if kind == "development":
                question_path = Path(development_question_path) if development_question_path else None
                candidate_answer_path = Path(development_answer_path) if development_answer_path else None
            elif kind == "requirements":
                question_path = ask_human_path
                candidate_answer_path = hitl_record_path
            elif kind == "notion":
                question_path = notion_question_path
                candidate_answer_path = notion_record_path
            else:
                continue
            if question_path and question_path.exists() and _preview_text(question_path):
                active_question = question_path
                answer_path = candidate_answer_path or ""
                break
        question_summary = _preview_text(active_question) if active_question else ""
        if active_question:
            last_resolved = self._last_resolved_hitl
            if (
                str(active_question) == str(last_resolved.question_path).strip()
                and question_summary
                and question_summary == str(last_resolved.question_summary).strip()
            ):
                if self._resolved_file_hitl_has_durable_consumption(
                    action=active_action,
                    question_path=active_question,
                ):
                    return {"pending": False, "question_path": "", "answer_path": "", "summary": ""}
        return {
            "pending": bool(active_question),
            "question_path": str(active_question) if active_question else "",
            "answer_path": str(answer_path) if answer_path and Path(answer_path).exists() else "",
            "summary": question_summary,
        }

    @staticmethod
    def _file_hitl_candidate_kinds_for_action(action: str) -> tuple[str, ...]:
        normalized = str(action or "").strip()
        if not normalized or normalized == "idle":
            return ("development", "requirements", "notion")
        if normalized in {"workflow.a00.start", "control.b01.open", "stage.a01.start"}:
            return ()
        if normalized == "stage.a02.start":
            return ("notion",)
        if normalized in {"stage.a03.start", "stage.a04.start", "stage.a05.start", "stage.a06.start"}:
            return ("requirements", "notion")
        if normalized in {"stage.a07.start", "stage.a08.start"}:
            return ("development", "requirements", "notion")
        return ("development", "requirements", "notion")

    @staticmethod
    def _stage_routes_for_action(action: str) -> tuple[str, ...]:
        route = STAGE_ROUTE_BY_ACTION.get(str(action or "").strip())
        return (route,) if route else ()

    def _build_stage_snapshot_by_route(self, route: str) -> dict[str, Any]:
        normalized = str(route or "").strip()
        builder_name = dict(STAGE_SNAPSHOT_BUILDERS).get(normalized)
        if not builder_name:
            raise KeyError(f"unknown stage snapshot route: {normalized}")
        builder = getattr(self, builder_name)
        return builder()

    def _build_stage_snapshots(self, routes: Sequence[str] | None = None) -> dict[str, dict[str, Any]]:
        selected = tuple(routes or [route for route, _builder in STAGE_SNAPSHOT_BUILDERS])
        snapshots: dict[str, dict[str, Any]] = {}
        for route in selected:
            normalized = str(route or "").strip()
            if not normalized or normalized in snapshots:
                continue
            snapshots[normalized] = self._build_stage_snapshot_by_route(normalized)
        return snapshots

    def _current_artifact_index_scope(self) -> tuple[str, str]:
        return (
            str(self._context.project_dir or "").strip(),
            str(self._context.requirement_name or "").strip(),
        )

    def _reset_artifact_index_for_current_context(self) -> None:
        with self._artifact_index_lock:
            self._artifact_index_scope = self._current_artifact_index_scope()
            self._artifact_index_items = []

    def _ensure_artifact_index_scope(self) -> None:
        current_scope = self._current_artifact_index_scope()
        with self._artifact_index_lock:
            if self._artifact_index_scope != current_scope:
                self._artifact_index_scope = current_scope
                self._artifact_index_items = []

    @staticmethod
    def _artifact_items_from_candidates(candidates: Sequence[str]) -> list[dict[str, Any]]:
        unique_paths: list[str] = []
        for item in candidates:
            text = str(item).strip()
            if not text:
                continue
            path = Path(text).expanduser()
            if not path.exists():
                continue
            resolved = str(path.resolve())
            if resolved not in unique_paths:
                unique_paths.append(resolved)
        return [
            {
                "path": item,
                "updated_at": _iso_from_path(item),
                "summary": _preview_text(item),
            }
            for item in sorted(unique_paths, key=lambda candidate: Path(candidate).stat().st_mtime, reverse=True)
        ]

    def _merge_artifact_index(self, items: Sequence[Mapping[str, Any]], *, replace: bool) -> list[dict[str, Any]]:
        self._ensure_artifact_index_scope()
        merged: dict[str, dict[str, Any]] = {}
        if not replace:
            with self._artifact_index_lock:
                for item in self._artifact_index_items:
                    path_text = str(item.get("path", "")).strip()
                    if path_text and Path(path_text).exists():
                        merged[path_text] = dict(item)
        for item in items:
            path_text = str(item.get("path", "")).strip()
            if path_text and Path(path_text).exists():
                merged[path_text] = dict(item)
        ordered = sorted(
            merged.values(),
            key=lambda item: Path(str(item.get("path", ""))).stat().st_mtime,
            reverse=True,
        )
        with self._artifact_index_lock:
            self._artifact_index_items = ordered[:24]
        return ordered

    def _build_artifacts_snapshot(
        self,
        *,
        stages: Mapping[str, Mapping[str, Any]] | None = None,
        control: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        candidates: list[str] = []
        if stages is None:
            stage_snapshots = self._build_stage_snapshots()
            replace_index = True
        else:
            stage_snapshots = dict(stages)
            replace_index = set(stage_snapshots) >= {route for route, _builder in STAGE_SNAPSHOT_BUILDERS}
        control_snapshot = dict(control) if control is not None else self._build_control_snapshot_for_session(self._current_control_session())
        collections: list[list[Any]] = []
        for route, _builder in STAGE_SNAPSHOT_BUILDERS:
            snapshot = stage_snapshots.get(route)
            if snapshot is not None:
                collections.append([item.get("path", "") for item in snapshot.get("files", [])])
        collections.append([artifact for worker in control_snapshot.get("workers", []) for artifact in worker.get("artifact_paths", [])])
        for collection in collections:
            for item in collection:
                text = str(item).strip()
                if text and Path(text).exists():
                    candidates.append(text)
        items = self._merge_artifact_index(self._artifact_items_from_candidates(candidates), replace=replace_index)[:12]
        return {"items": items}

    def _build_app_snapshot(
        self,
        *,
        runs: Sequence[Mapping[str, Any]] | None = None,
        control: Mapping[str, Any] | None = None,
        hitl: Mapping[str, Any] | None = None,
        attention: Mapping[str, Any] | None = None,
        artifacts: Mapping[str, Any] | None = None,
        stage_snapshots: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        runs_list = list(runs) if runs is not None else self._list_runs()
        control_snapshot = dict(control) if control is not None else self._build_control_snapshot_for_session(self._current_control_session())
        hitl_snapshot = dict(hitl) if hitl is not None else self._build_hitl_snapshot()
        attention_snapshot = dict(attention) if attention is not None else self._attention_manager.snapshot()
        prompt_attention_snapshot = self._build_pending_prompt_attention_snapshot()
        if prompt_attention_snapshot.get("pending"):
            attention_snapshot = prompt_attention_snapshot
        artifacts_snapshot = dict(artifacts) if artifacts is not None else self._build_artifacts_snapshot(control=control_snapshot)
        project_dir = self._resolve_project_dir(runs=runs_list)
        requirement_name = self._resolve_requirement_name()
        with self._display_state_lock:
            reconcile_action = str(self._display_action or self._context.current_action or "").strip()
        self._reconcile_persisted_running_runner(reconcile_action)
        with self._display_state_lock:
            preferred_stage = self._display_action or self._context.current_action or "idle"
            preferred_status = str(self._display_status or "ready").strip() or "ready"
            preferred_stage_seq = int(self._display_stage_seq or 0)
            preferred_source = str(self._display_source or "").strip()
            preferred_runner_id = str(self._display_runner_id or "").strip()
            preferred_message = str(self._display_message or "").strip()
            display_failure = dict(self._display_failure)
        runtime_status: str | None = None
        if stage_snapshots:
            active_route = STAGE_ROUTE_BY_ACTION.get(str(preferred_stage or "").strip())
            active_snapshot = stage_snapshots.get(active_route) if active_route else None
            if isinstance(active_snapshot, Mapping):
                workers = active_snapshot.get("workers", [])
                if isinstance(workers, Sequence) and not isinstance(workers, (str, bytes)):
                    runtime_status = self._infer_runtime_stage_status_from_workers(preferred_stage, workers)
        active_stage, active_stage_status, active_stage_seq = self._derive_display_stage_state(
            preferred_action=preferred_stage,
            preferred_status=preferred_status,
            preferred_stage_seq=preferred_stage_seq,
            source=preferred_source,
            runtime_status=runtime_status,
            pending_prompt=bool(self._iter_pending_prompts()),
            pending_hitl=bool(hitl_snapshot.get("pending", False)),
        )
        active_stage = active_stage or "idle"
        stage_state = _read_project_stage_state_record(
            project_dir=project_dir,
            requirement_name=requirement_name,
            action=active_stage,
        )
        stage_state_runner_id = str((stage_state or {}).get("runner_id", "") or "").strip()
        stage_state_matches_cursor = bool(
            stage_state is not None
            and int(stage_state.get("stage_seq") or 0) == int(active_stage_seq or 0)
            and (
                not preferred_runner_id
                or not stage_state_runner_id
                or stage_state_runner_id == preferred_runner_id
            )
        )
        if stage_state_matches_cursor and stage_state is not None:
            preferred_source = str(stage_state.get("source", "") or preferred_source).strip()
            preferred_runner_id = stage_state_runner_id or preferred_runner_id
            if not preferred_message:
                preferred_message = str(stage_state.get("message", "") or "").strip()
            if active_stage_status in {"failed", "error"}:
                failure_path = str(stage_state.get("failure_path", "") or "").strip()
                persisted_failure = _safe_json_read(failure_path) if failure_path else {}
                display_failure = {
                    "kind": str(
                        persisted_failure.get("failure_kind")
                        or stage_state.get("failure_kind")
                        or "runner_failure"
                    ).strip(),
                    "message": str(persisted_failure.get("error") or stage_state.get("message") or "").strip(),
                    "path": failure_path,
                    "runner_id": str(persisted_failure.get("runner_id") or preferred_runner_id).strip(),
                    "stage_seq": int(persisted_failure.get("stage_seq") or active_stage_seq or 0),
                    "updated_at": str(persisted_failure.get("updated_at") or stage_state.get("updated_at") or "").strip(),
                    "orphaned_workers": list(
                        persisted_failure.get("orphaned_workers")
                        or stage_state.get("orphaned_workers")
                        or []
                    ),
                }
        if active_stage_status in {"failed", "error"} and (
            display_failure or preferred_source == "runner_failure"
        ):
            stage_label = self._resolve_stage_label(
                action=active_stage,
                project_dir=project_dir,
                requirement_name=requirement_name,
            )
            failure_path = str(
                display_failure.get("failure_path")
                or display_failure.get("path")
                or ""
            ).strip()
            failure_kind = str(
                display_failure.get("failure_kind")
                or display_failure.get("kind")
                or "runner_failure"
            ).strip() or "runner_failure"
            display_failure = {
                **display_failure,
                "action": active_stage,
                "status": "failed",
                "source": "runner_failure",
                "failure_path": failure_path,
                "failure_kind": failure_kind,
                "stage_label": stage_label,
                # Keep the original aliases for additive compatibility.
                "path": failure_path,
                "kind": failure_kind,
                "runner_id": str(display_failure.get("runner_id") or preferred_runner_id).strip(),
                "stage_seq": int(display_failure.get("stage_seq") or active_stage_seq or 0),
                "orphaned_workers": list(display_failure.get("orphaned_workers") or []),
            }
        else:
            display_failure = {}
        return {
            "project_dir": project_dir,
            "requirement_name": requirement_name,
            "current_action": self._context.current_action,
            "active_run_id": str(control_snapshot.get("run_id", "")).strip() or (runs_list[0]["run_id"] if runs_list else ""),
            "active_stage": active_stage,
            "active_stage_status": active_stage_status,
            "active_stage_seq": active_stage_seq,
            "active_stage_runner_id": preferred_runner_id,
            "active_stage_source": preferred_source,
            "active_stage_message": preferred_message,
            "active_stage_failure": display_failure,
            "active_stage_label": self._resolve_stage_label(
                action=active_stage,
                project_dir=project_dir,
                requirement_name=requirement_name,
            ),
            "pending_hitl": bool(hitl_snapshot.get("pending", False)),
            "pending_attention": bool(attention_snapshot.get("pending", False)),
            "pending_attention_reason": str(attention_snapshot.get("reason", "")).strip(),
            "pending_attention_since": str(attention_snapshot.get("started_at", "")).strip(),
            "recent_artifacts": artifacts_snapshot.get("items", [])[:5],
            "available_runs": runs_list[:5],
            "capabilities": {
                "structured_snapshots": True,
                "control_actions": ["attach", "detach", "restart", "retry", "kill", "resume"],
                "local_prompt_history": True,
                "collapsible_logs": True,
            },
        }

    def _current_stage_workers(self, action: str) -> list[dict[str, Any]]:
        normalized = str(action or "").strip()
        project_dir = self._resolve_project_dir()
        if normalized in {"control.b01.open", "stage.a01.start"}:
            return list(self._build_routing_snapshot().get("workers", []))
        if normalized == "stage.a02.start":
            if not project_dir:
                return []
            return self._scan_requirement_intake_workers(project_dir)
        if normalized == "stage.a03.start":
            if not project_dir:
                return []
            return self._scan_requirement_clarification_workers(project_dir)
        if normalized == "stage.a04.start":
            return list(self._build_review_snapshot().get("workers", []))
        if normalized == "stage.a05.start":
            if not project_dir:
                return []
            return self._scan_current_design_status_workers(project_dir)
        if normalized == "stage.a06.start":
            if not project_dir:
                return []
            return self._scan_current_task_split_workers(project_dir)
        if normalized == "stage.a07.start":
            if not project_dir:
                return []
            return self._scan_current_development_workers(project_dir)
        if normalized == "stage.a08.start":
            if not project_dir:
                return []
            return self._scan_current_overall_review_workers(project_dir)
        return []

    def _current_stage_workers_without_runtime_io(self, action: str) -> list[dict[str, Any]]:
        normalized = str(action or "").strip()
        project_dir = self._resolve_project_dir()
        if not project_dir:
            return []
        project_root = Path(project_dir)
        if normalized in {"control.b01.open", "stage.a01.start"}:
            return self._scan_runtime_workers_without_runtime_io(project_root / ROUTING_RUNTIME_ROOT_NAME)
        if normalized == "stage.a02.start":
            return self._scan_runtime_workers_without_runtime_io(project_root / NOTION_RUNTIME_ROOT_NAME)
        if normalized == "stage.a03.start":
            return _merge_worker_snapshots(
                self._scan_runtime_workers_without_runtime_io(project_root / REQUIREMENTS_RUNTIME_ROOT_NAME),
                self._scan_runtime_workers_without_runtime_io(project_root / LEGACY_REQUIREMENTS_RUNTIME_ROOT_NAME),
            )
        if normalized == "stage.a04.start":
            return _merge_worker_snapshots(
                self._scan_runtime_workers_without_runtime_io(project_root / REQUIREMENTS_REVIEW_RUNTIME_ROOT_NAME),
                self._scan_runtime_workers_without_runtime_io(project_root / REQUIREMENTS_RUNTIME_ROOT_NAME),
                self._scan_runtime_workers_without_runtime_io(project_root / LEGACY_REQUIREMENTS_RUNTIME_ROOT_NAME),
            )
        if normalized == "stage.a05.start":
            return _merge_worker_snapshots(
                self._scan_runtime_workers_without_runtime_io(project_root / DETAILED_DESIGN_RUNTIME_ROOT_NAME),
                self._scan_runtime_workers_without_runtime_io(project_root / REQUIREMENTS_REVIEW_RUNTIME_ROOT_NAME),
                self._scan_runtime_workers_without_runtime_io(project_root / REQUIREMENTS_RUNTIME_ROOT_NAME),
                self._scan_runtime_workers_without_runtime_io(project_root / LEGACY_REQUIREMENTS_RUNTIME_ROOT_NAME),
            )
        if normalized == "stage.a06.start":
            return _merge_worker_snapshots(
                self._scan_runtime_workers_without_runtime_io(project_root / TASK_SPLIT_RUNTIME_ROOT_NAME),
                self._scan_runtime_workers_without_runtime_io(project_root / DETAILED_DESIGN_RUNTIME_ROOT_NAME),
                self._scan_runtime_workers_without_runtime_io(project_root / REQUIREMENTS_REVIEW_RUNTIME_ROOT_NAME),
                self._scan_runtime_workers_without_runtime_io(project_root / REQUIREMENTS_RUNTIME_ROOT_NAME),
                self._scan_runtime_workers_without_runtime_io(project_root / LEGACY_REQUIREMENTS_RUNTIME_ROOT_NAME),
            )
        if normalized == "stage.a07.start":
            return self._scan_runtime_workers_without_runtime_io(project_root / DEVELOPMENT_RUNTIME_ROOT_NAME)
        if normalized == "stage.a08.start":
            return self._scan_runtime_workers_without_runtime_io(project_root / DEVELOPMENT_RUNTIME_ROOT_NAME)
        return []

    def _current_stage_worker_states_without_runtime_io(self, action: str) -> list[tuple[Path, dict[str, Any]]]:
        normalized = str(action or "").strip()
        project_dir = str(self._resolve_project_dir() or "").strip()
        if not project_dir or not normalized:
            return []
        project_root = Path(project_dir).expanduser().resolve()
        roots_by_action: dict[str, tuple[str, ...]] = {
            "control.b01.open": (ROUTING_RUNTIME_ROOT_NAME,),
            "stage.a01.start": (ROUTING_RUNTIME_ROOT_NAME,),
            "stage.a02.start": (NOTION_RUNTIME_ROOT_NAME,),
            "stage.a03.start": (REQUIREMENTS_RUNTIME_ROOT_NAME, LEGACY_REQUIREMENTS_RUNTIME_ROOT_NAME),
            "stage.a04.start": (
                REQUIREMENTS_REVIEW_RUNTIME_ROOT_NAME,
                REQUIREMENTS_RUNTIME_ROOT_NAME,
                LEGACY_REQUIREMENTS_RUNTIME_ROOT_NAME,
            ),
            "stage.a05.start": (
                DETAILED_DESIGN_RUNTIME_ROOT_NAME,
                REQUIREMENTS_REVIEW_RUNTIME_ROOT_NAME,
                REQUIREMENTS_RUNTIME_ROOT_NAME,
                LEGACY_REQUIREMENTS_RUNTIME_ROOT_NAME,
            ),
            "stage.a06.start": (
                TASK_SPLIT_RUNTIME_ROOT_NAME,
                DETAILED_DESIGN_RUNTIME_ROOT_NAME,
                REQUIREMENTS_REVIEW_RUNTIME_ROOT_NAME,
                REQUIREMENTS_RUNTIME_ROOT_NAME,
                LEGACY_REQUIREMENTS_RUNTIME_ROOT_NAME,
            ),
            "stage.a07.start": (DEVELOPMENT_RUNTIME_ROOT_NAME,),
            "stage.a08.start": (DEVELOPMENT_RUNTIME_ROOT_NAME,),
        }
        requirement_name = self._resolve_requirement_name()
        result: list[tuple[Path, dict[str, Any]]] = []
        for root_name in roots_by_action.get(normalized, ()):
            runtime_root = project_root / root_name
            if not runtime_root.is_dir():
                continue
            for state_path in self._iter_worker_state_paths(runtime_root):
                payload = _safe_json_read(state_path)
                if not payload:
                    continue
                state_action = str(payload.get("workflow_action", "") or "").strip()
                if state_action != normalized:
                    continue
                state_requirement = str(payload.get("requirement_name", "") or "").strip()
                if requirement_name and state_requirement != requirement_name:
                    continue
                state_project = str(payload.get("project_dir") or payload.get("work_dir") or "").strip()
                if state_project:
                    with contextlib.suppress(Exception):
                        if Path(state_project).expanduser().resolve() != project_root:
                            continue
                result.append((state_path, payload))
        return result

    @staticmethod
    def _worker_can_be_orphaned(worker: Mapping[str, Any]) -> bool:
        session_name = str(worker.get("session_name", "") or "").strip()
        if not session_name:
            return False
        agent_state = str(worker.get("agent_state", "") or "").strip().upper()
        health_status = str(worker.get("health_status", "") or "").strip().lower()
        return agent_state != "DEAD" and health_status != "dead"

    def _update_worker_orphan_marker(
        self,
        state_path_value: str,
        *,
        runner_id: str,
        orphaned: bool,
        reason: str = "",
        stage_action: str = "",
    ) -> bool:
        state_path_text = str(state_path_value or "").strip()
        if not state_path_text:
            return False
        state_path = Path(state_path_text).expanduser().resolve()
        payload = _safe_json_read(state_path)
        if not payload:
            return False
        payload["stage_runner_id"] = str(runner_id or "").strip()
        if orphaned:
            payload["turn_state"] = "orphaned"
            payload["orphaned_at"] = _iso_now()
            payload["orphaned_reason"] = str(reason or "runner_failure").strip() or "runner_failure"
            payload["orphaned_stage_action"] = str(stage_action or "").strip()
        else:
            payload["turn_state"] = ""
            payload["orphaned_at"] = ""
            payload["orphaned_reason"] = ""
            payload["orphaned_stage_action"] = ""
        try:
            _atomic_write_json(state_path, payload)
            return True
        except Exception:
            return False

    def _mark_stage_workers_orphaned(self, action: str, *, runner_id: str, reason: str) -> list[dict[str, str]]:
        orphaned_workers: list[dict[str, str]] = []
        seen_state_paths: set[str] = set()
        normalized_runner_id = str(runner_id or "").strip()
        for state_path_value, raw_state in self._current_stage_worker_states_without_runtime_io(action):
            state_path = str(state_path_value)
            if not state_path or state_path in seen_state_paths:
                continue
            seen_state_paths.add(state_path)
            if str(raw_state.get("stage_runner_id", "") or "").strip() != normalized_runner_id:
                continue
            if not self._worker_can_be_orphaned(raw_state):
                continue
            if not self._update_worker_orphan_marker(
                state_path,
                runner_id=runner_id,
                orphaned=True,
                reason=reason,
                stage_action=action,
            ):
                continue
            session_name = str(raw_state.get("session_name", "") or "").strip()
            worker_id = str(raw_state.get("worker_id", "") or "").strip()
            role = session_name.split("-", 1)[0] if session_name else worker_id
            orphaned_workers.append(
                {
                    "session_name": session_name,
                    "role": role,
                    "worker_id": worker_id,
                    "attach_command": f"tmux attach -t {session_name}" if session_name else "",
                }
            )
        return orphaned_workers

    def _cleanup_stage_orphans_before_runner_start(self, action: str, *, runner_id: str) -> None:
        normalized_action = str(action or "").strip()
        seen_state_paths: set[str] = set()
        for state_path_value, state_payload in self._current_stage_worker_states_without_runtime_io(action):
            state_path = str(state_path_value)
            if not state_path or state_path in seen_state_paths:
                continue
            seen_state_paths.add(state_path)
            is_orphaned = str(state_payload.get("turn_state", "") or "").strip().lower() == "orphaned"
            is_dead = (
                str(state_payload.get("agent_state", "") or "").strip().upper() == "DEAD"
                or str(state_payload.get("health_status", "") or "").strip().lower() == "dead"
            )
            if not is_orphaned and not is_dead:
                continue
            orphaned_action = str(
                state_payload.get("orphaned_stage_action")
                or state_payload.get("workflow_action")
                or ""
            ).strip()
            if orphaned_action != normalized_action:
                continue
            session_name = str(state_payload.get("session_name") or "").strip()
            if session_name:
                identity_resolver = getattr(self._tmux_runtime, "session_matches_worker_state", None)
                if not callable(identity_resolver):
                    raise RuntimeError(
                        "清理上一轮孤儿 tmux 会话前无法验证 ownership，保留运行目录并终止新 runner: "
                        f"session={session_name}; runner_id={runner_id}"
                    )
                try:
                    identity_matches = bool(identity_resolver(session_name, state_payload, state_path_value))
                except Exception as error:
                    raise RuntimeError(
                        f"清理上一轮孤儿 tmux 会话前 ownership 验证失败，保留运行目录并终止新 runner: "
                        f"session={session_name}; runner_id={runner_id}; error={error}"
                    ) from error
                if identity_matches:
                    try:
                        self._tmux_runtime.kill_session(session_name, missing_ok=True)
                    except Exception as error:
                        raise RuntimeError(
                            f"清理上一轮孤儿 tmux 会话失败，保留运行目录并终止新 runner: "
                            f"session={session_name}; runner_id={runner_id}; error={error}"
                        ) from error
            runtime_dir = Path(state_path).expanduser().resolve().parent
            project_dir = str(self._resolve_project_dir() or "").strip()
            if not project_dir:
                continue
            project_root = Path(project_dir).expanduser().resolve()
            if runtime_dir == project_root or not self._path_is_within_project(str(runtime_dir), project_root):
                continue
            try:
                shutil.rmtree(runtime_dir)
            except FileNotFoundError:
                continue

    def _filter_workers_for_current_context(self, workers: Sequence[Mapping[str, Any]], action: str) -> list[dict[str, Any]]:
        project_dir = str(self._resolve_project_dir() or "").strip()
        requirement_name = self._resolve_requirement_name()
        normalized_action = str(action or "").strip()
        filtered = [dict(worker) for worker in workers]
        context_known = bool(project_dir or requirement_name or normalized_action)
        if context_known:
            filtered = [
                worker
                for worker in filtered
                if not _is_unscoped_dead_worker_snapshot(worker, context_known=context_known)
            ]
        if project_dir:
            matched = [
                worker
                for worker in filtered
                if str(worker.get("project_dir", "")).strip() in {"", project_dir}
            ]
            if matched:
                filtered = matched
        if requirement_name:
            requirement_scope_pool = filtered
            if normalized_action:
                action_matched = [
                    worker
                    for worker in filtered
                    if str(worker.get("workflow_action", "")).strip() in {"", normalized_action}
                ]
                requirement_scope_pool = action_matched
            scoped_matches = [
                worker
                for worker in requirement_scope_pool
                if str(worker.get("requirement_name", "")).strip() == requirement_name
            ]
            if scoped_matches:
                if normalized_action == "stage.a05.start":
                    active_unscoped_design_ba = [
                        worker
                        for worker in requirement_scope_pool
                        if str(worker.get("worker_id", "")).strip().lower() == "detailed-design-analyst"
                        and not str(worker.get("requirement_name", "")).strip()
                        and _worker_snapshot_is_actively_running(worker)
                    ]
                    filtered = _merge_worker_snapshots(scoped_matches, active_unscoped_design_ba)
                else:
                    filtered = scoped_matches
            elif any(str(worker.get("requirement_name", "")).strip() for worker in requirement_scope_pool):
                filtered = []
            else:
                filtered = [
                    worker
                    for worker in requirement_scope_pool
                    if not str(worker.get("requirement_name", "")).strip()
                ]
        if normalized_action:
            matched = [
                worker
                for worker in filtered
                if str(worker.get("workflow_action", "")).strip() in {"", normalized_action}
            ]
            if matched:
                filtered = matched
            elif any(str(worker.get("workflow_action", "")).strip() for worker in filtered):
                filtered = []
        workflow_started_at = self._current_workflow_started_at()
        if workflow_started_at is not None:
            filtered = [
                worker
                for worker in filtered
                if not _worker_snapshot_before_timestamp(worker, workflow_started_at)
            ]
        if normalized_action in {"stage.a06.start", "stage.a07.start"}:
            filtered = [
                _normalize_stage_active_contract_worker_snapshot(worker, action=normalized_action)
                for worker in filtered
            ]
        return filtered

    def _current_workflow_started_at(self) -> float | None:
        project_dir = str(self._resolve_project_dir() or "").strip()
        requirement_name = self._resolve_requirement_name()
        if not project_dir or not requirement_name:
            return None
        try:
            project_root = Path(project_dir).expanduser().resolve()
            safe_requirement = sanitize_requirement_name(requirement_name)
            state_path = (
                project_root
                / WORKFLOW_RECORD_ROOT_NAME
                / safe_requirement
                / "stages"
                / "workflow_a00_start.state.json"
            )
            payload = _safe_json_read(state_path)
            if str(payload.get("source", "")).strip() != "runner_start":
                return None
            started_at = _parse_iso_datetime(str(payload.get("updated_at", "")).strip())
            return started_at.timestamp() if started_at is not None else None
        except Exception:
            return None

    def _build_runtime_worker_hitl_snapshot(self, action: str) -> dict[str, Any]:
        workers = self._filter_workers_for_current_context(self._current_stage_workers(action), action)
        for worker in sorted(workers, key=_worker_snapshot_sort_key, reverse=True):
            question_path = str(worker.get("question_path", "") or "").strip()
            answer_path = str(worker.get("answer_path", "") or "").strip()
            if not question_path:
                continue
            question_summary = _preview_text(question_path)
            if not question_summary:
                continue
            last_resolved = self._last_resolved_hitl
            if (
                question_path == str(last_resolved.question_path).strip()
                and question_summary == str(last_resolved.question_summary).strip()
            ):
                continue
            return {
                "pending": True,
                "question_path": question_path,
                "answer_path": answer_path,
                "summary": question_summary,
                "attach_command": _prompt_attach_command(worker),
            }
        return {"pending": False, "question_path": "", "answer_path": "", "summary": "", "attach_command": ""}

    @staticmethod
    def _review_json_has_task_result(path_value: str | Path, *, task_name: str) -> bool:
        path_text = str(path_value or "").strip()
        task_name_text = str(task_name or "").strip()
        if not path_text or not task_name_text:
            return False
        path = Path(path_text).expanduser()
        if not path.exists() or not path.is_file() or path.suffix.lower() != ".json":
            return False
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            normalize_review_status_payload(payload, task_name=task_name_text, source=str(path))
            return True
        except Exception:
            return False

    @staticmethod
    def _requirements_review_output_is_valid(review_json_path: str | Path, review_md_path: str | Path) -> bool:
        json_path = Path(review_json_path).expanduser()
        md_path = Path(review_md_path).expanduser()
        if not json_path.exists() or not json_path.is_file() or not md_path.exists() or not md_path.is_file():
            return False
        try:
            payload = json.loads(json_path.read_text(encoding="utf-8"))
            matched_item = normalize_review_status_payload(
                payload,
                task_name=REQUIREMENTS_REVIEW_TASK_NAME,
                source=str(json_path),
            )
            review_pass = bool(matched_item["review_pass"])
            review_md_empty = not get_markdown_content(md_path).strip()
            return (review_pass and review_md_empty) or ((not review_pass) and not review_md_empty)
        except Exception:
            return False

    @staticmethod
    def _worker_snapshot_is_requirements_review_feedback(worker: Mapping[str, Any]) -> bool:
        artifact_paths = worker.get("artifact_paths", [])
        artifact_text = ""
        if isinstance(artifact_paths, Sequence) and not isinstance(artifact_paths, (str, bytes)):
            artifact_text = " ".join(str(item) for item in artifact_paths)
        marker_text = " ".join(
            str(worker.get(key, "")).strip()
            for key in (
                "note",
                "current_turn_phase",
                "turn_status_path",
                "current_task_runtime_status",
                "workflow_stage",
            )
        )
        marker_text = f"{marker_text} {artifact_text}".lower()
        return "requirements_review_feedback" in marker_text or "requirements-review-feedback" in marker_text

    def _stage_a04_failed_reviewer_has_current_review_output(self, worker: Mapping[str, Any]) -> bool:
        worker_id = str(worker.get("worker_id", "")).strip().lower()
        if not worker_id.startswith("requirements-review-"):
            return False
        agent_state = str(worker.get("agent_state", "")).strip().upper()
        health_status = str(worker.get("health_status", "")).strip().lower()
        has_live_evidence = bool(worker.get("session_exists")) or health_status in {
            "alive",
            "observe_error",
            "provider_auth_error",
        }
        if agent_state == "DEAD" or health_status in {"dead", "missing_session", "pane_dead"} or not has_live_evidence:
            return False
        project_dir = self._resolve_project_dir()
        requirement_name = self._resolve_requirement_name()
        if not project_dir or not requirement_name:
            return False
        session_name = str(worker.get("session_name", "")).strip()
        candidates: list[tuple[Path, Path]] = []
        if session_name:
            candidates.append(build_requirements_reviewer_artifact_paths(project_dir, requirement_name, session_name))
        artifact_paths = worker.get("artifact_paths", [])
        if isinstance(artifact_paths, Sequence) and not isinstance(artifact_paths, (str, bytes)):
            json_paths = [
                Path(str(path).strip())
                for path in artifact_paths
                if str(path).strip().lower().endswith(".json")
            ]
            md_paths = [
                Path(str(path).strip())
                for path in artifact_paths
                if str(path).strip().lower().endswith(".md")
            ]
            for json_path in json_paths:
                for md_path in md_paths:
                    candidates.append((md_path, json_path))
        seen: set[tuple[str, str]] = set()
        for review_md_path, review_json_path in candidates:
            key = (str(Path(review_md_path).expanduser()), str(Path(review_json_path).expanduser()))
            if key in seen:
                continue
            seen.add(key)
            if self._requirements_review_output_is_valid(review_json_path, review_md_path):
                return True
        return False

    def _stage_a07_failed_reviewer_has_current_task_output(self, worker: Mapping[str, Any]) -> bool:
        worker_id = str(worker.get("worker_id", "")).strip()
        if not worker_id.startswith("development-review-"):
            return False
        if not self._worker_snapshot_has_live_session(worker):
            return False
        project_dir = self._resolve_project_dir()
        requirement_name = self._resolve_requirement_name()
        if not project_dir or not requirement_name:
            return False
        try:
            paths = build_development_paths(project_dir, requirement_name)
            task_json_path = paths["task_json_path"]
            if not is_task_progress_json(task_json_path):
                return False
            task_name = str(get_first_false_task(task_json_path) or "").strip()
        except Exception:
            return False
        if not task_name:
            return False
        candidates: list[str] = []
        turn_status_path = str(worker.get("turn_status_path", "")).strip()
        if turn_status_path:
            candidates.append(turn_status_path)
        artifact_paths = worker.get("artifact_paths", [])
        if isinstance(artifact_paths, Sequence) and not isinstance(artifact_paths, (str, bytes)):
            candidates.extend(str(path).strip() for path in artifact_paths if str(path).strip())
        session_name = str(worker.get("session_name", "")).strip()
        if session_name:
            with contextlib.suppress(Exception):
                _review_md_path, review_json_path = build_reviewer_artifact_paths(project_dir, requirement_name, session_name)
                candidates.append(str(review_json_path))
        seen: set[str] = set()
        for candidate in candidates:
            if not candidate or candidate in seen:
                continue
            seen.add(candidate)
            if self._review_json_has_task_result(candidate, task_name=task_name):
                return True
        return False

    def _worker_snapshot_has_failed_status(self, worker: Mapping[str, Any], *, action: str = "") -> bool:
        if _is_recoverable_reconfig_snapshot(worker):
            return False
        if self._worker_snapshot_has_live_session(worker):
            if _worker_snapshot_is_actively_running(worker):
                return False
        normalized_action = str(action or self._context.current_action or self._display_action or "").strip()
        if normalized_action == "stage.a04.start" and self._stage_a04_failed_reviewer_has_current_review_output(worker):
            return False
        if normalized_action == "stage.a07.start" and self._stage_a07_failed_reviewer_has_current_task_output(worker):
            return False
        return str(worker.get("status", "")).strip() in {"failed", "stale_failed", "error"}

    @staticmethod
    def _worker_snapshot_is_dead(worker: Mapping[str, Any]) -> bool:
        if _is_recoverable_reconfig_snapshot(worker):
            return False
        if worker_state_is_prelaunch_active(worker):
            return False
        return (
            str(worker.get("agent_state", "")).strip().upper() == "DEAD"
            or str(worker.get("health_status", "")).strip().lower() == "dead"
        )

    @staticmethod
    def _worker_snapshot_has_live_session(worker: Mapping[str, Any]) -> bool:
        return (
            bool(worker.get("session_exists"))
            and str(worker.get("agent_state", "")).strip().upper() != "DEAD"
            and str(worker.get("health_status", "")).strip().lower() != "dead"
        )

    def _stage_has_pending_contract_work(self, action: str) -> bool:
        normalized_action = str(action or "").strip()
        project_dir = self._resolve_project_dir()
        requirement_name = self._resolve_requirement_name()
        if not project_dir or not requirement_name:
            return False
        try:
            if normalized_action == "stage.a07.start":
                paths = build_development_paths(project_dir, requirement_name)
                task_json_path = paths["task_json_path"]
                return is_task_progress_json(task_json_path) and get_first_false_task(task_json_path) is not None
            if normalized_action == "stage.a08.start":
                paths = build_overall_review_paths(project_dir, requirement_name)
                return not overall_review_passed(paths["state_path"])
        except Exception:
            return False
        return False

    def _stage_has_pending_hitl(self, action: str) -> bool:
        if self._iter_pending_prompts():
            return True
        with contextlib.suppress(Exception):
            return bool(self._build_runtime_worker_hitl_snapshot(action).get("pending", False))
        return False

    def _routing_contract_is_ready(self) -> bool:
        project_dir = self._resolve_project_dir()
        if not project_dir:
            return False
        try:
            validate_routing_layer_artifacts(project_dir)
            return True
        except Exception:
            return False

    def _infer_runtime_stage_status_from_workers(self, action: str, workers: Sequence[Mapping[str, Any]]) -> str:
        normalized_action = str(action or "").strip()
        contract_gated_action = normalized_action == "stage.a08.start"
        has_pending_contract_work = self._stage_has_pending_contract_work(normalized_action)
        allow_worker_running_inference = not contract_gated_action or has_pending_contract_work
        if not workers:
            return ""
        if allow_worker_running_inference and any(_recoverable_reconfig_snapshot_has_live_evidence(worker) for worker in workers):
            return "running"
        if allow_worker_running_inference and any(_worker_snapshot_is_actively_running(worker) for worker in workers):
            return "running"
        if normalized_action == "stage.a07.start" and any(
            _worker_snapshot_has_stale_ready_task_result_contract(worker) for worker in workers
        ):
            return "failed"
        if normalized_action == "stage.a08.start" and has_pending_contract_work and self._active_stage_runner_alive(normalized_action):
            return "running"
        has_failed_workers = any(self._worker_snapshot_has_failed_status(worker, action=normalized_action) for worker in workers)
        has_dead_workers = any(self._worker_snapshot_is_dead(worker) for worker in workers)
        if has_failed_workers:
            return "failed"
        if has_dead_workers:
            if normalized_action == "stage.a01.start" and self._routing_contract_is_ready():
                return ""
            return "failed"
        if (
            normalized_action == "stage.a08.start"
            and has_pending_contract_work
            and not self._stage_has_pending_hitl(normalized_action)
            and all(_worker_snapshot_has_terminal_status(worker) for worker in workers)
        ):
            return "failed"
        return ""

    def _infer_runtime_stage_status(self, action: str) -> str:
        workers = self._filter_workers_for_current_context(self._current_stage_workers(action), action)
        return self._infer_runtime_stage_status_from_workers(action, workers)

    def _stage_has_stale_ready_task_result_contract(self, action: str) -> bool:
        normalized_action = str(action or "").strip()
        if normalized_action != "stage.a07.start":
            return False
        workers = self._filter_workers_for_current_context(self._current_stage_workers(normalized_action), normalized_action)
        return any(_worker_snapshot_has_stale_ready_task_result_contract(worker) for worker in workers)

    def _failed_stage_worker_summaries(self, action: str) -> list[str]:
        failed_workers = [
            worker
            for worker in self._filter_workers_for_current_context(self._current_stage_workers(action), action)
            if str(worker.get("status", "")).strip() in {"failed", "stale_failed", "error"}
            and not _is_recoverable_reconfig_snapshot(worker)
            and self._worker_snapshot_has_failed_status(worker, action=action)
        ]
        summaries: list[str] = []
        for worker in sorted(failed_workers, key=_worker_snapshot_sort_key, reverse=True):
            worker_name = (
                str(worker.get("session_name", "")).strip()
                or str(worker.get("worker_id", "")).strip()
                or "unknown-worker"
            )
            worker_note = (
                str(worker.get("note", "")).strip()
                or str(worker.get("status", "")).strip()
                or "failed"
            )
            summaries.append(f"{worker_name}: {worker_note}")
        return summaries

    def _validate_stage_success_before_completed(self, *, action: str, result: Any) -> None:
        normalized_action = str(action or "").strip()
        if normalized_action not in {"stage.a07.start", "stage.a08.start"}:
            return
        if isinstance(result, Mapping):
            project_dir = str(result.get("project_dir", "")).strip() or self._resolve_project_dir()
            requirement_name = str(result.get("requirement_name", "")).strip() or self._resolve_requirement_name()
        else:
            project_dir = str(getattr(result, "project_dir", "") or "").strip() or self._resolve_project_dir()
            requirement_name = str(getattr(result, "requirement_name", "") or "").strip() or self._resolve_requirement_name()
        if not project_dir or not requirement_name:
            stage_label = "任务开发阶段" if normalized_action == "stage.a07.start" else "复核阶段"
            raise RuntimeError(f"{stage_label}返回 completed，但缺少项目或需求上下文，无法校验阶段产物")
        if normalized_action == "stage.a07.start":
            paths = build_development_paths(project_dir, requirement_name)
        else:
            paths = build_overall_review_paths(project_dir, requirement_name)
        task_json_path = paths["task_json_path"]
        failed_worker_summaries = self._failed_stage_worker_summaries(normalized_action)
        failed_worker_text = ""
        if failed_worker_summaries:
            failed_worker_text = "\n当前失败智能体:\n" + "\n".join(failed_worker_summaries)
        if not is_task_progress_json(task_json_path):
            stage_label = "任务开发阶段" if normalized_action == "stage.a07.start" else "复核阶段"
            raise RuntimeError(f"{stage_label}返回 completed，但任务单 JSON 不合法或缺失: {task_json_path}{failed_worker_text}")
        next_task = get_first_false_task(task_json_path)
        if next_task is not None:
            stage_label = "任务开发阶段" if normalized_action == "stage.a07.start" else "复核阶段"
            raise RuntimeError(f"{stage_label}返回 completed，但任务单 JSON 仍存在未完成任务: {next_task}{failed_worker_text}")
        if normalized_action == "stage.a08.start" and not overall_review_passed(paths["state_path"]):
            raise RuntimeError(
                f"复核阶段返回 completed，但复核完成状态未通过: {paths['state_path']}"
                f"{failed_worker_text}"
            )

    def _load_authoritative_runner_terminal_stage_state(self, action: str) -> dict[str, Any] | None:
        action_text = str(action or "").strip()
        project_dir = self._resolve_project_dir()
        requirement_name = self._resolve_requirement_name()
        if not action_text or not project_dir or not requirement_name:
            return None
        try:
            state_path = (
                Path(project_dir).expanduser().resolve()
                / WORKFLOW_RECORD_ROOT_NAME
                / sanitize_requirement_name(requirement_name or "_global")
                / "stages"
                / f"{_stage_record_action_fragment(action_text)}.state.json"
            )
            payload = _safe_json_read(state_path)
        except Exception:
            return None
        if not isinstance(payload, Mapping):
            return None
        if str(payload.get("action", "")).strip() != action_text:
            return None
        if str(payload.get("source", "")).strip() not in {"runner_complete", "runner_failure"}:
            return None
        if str(payload.get("status", "")).strip() not in {"completed", "failed", "error", "superseded"}:
            return None
        if REQUIREMENT_CONCURRENCY_CONFLICT_MARKER in str(payload.get("message", "")):
            return None
        return dict(payload)

    def _load_runner_failure_stage_state(self, action: str) -> dict[str, Any] | None:
        payload = self._load_authoritative_runner_terminal_stage_state(action)
        if payload is None:
            return None
        if str(payload.get("source", "")).strip() != "runner_failure":
            return None
        if str(payload.get("status", "")).strip() not in {"failed", "error"}:
            return None
        return payload

    def _derive_display_stage_state(
        self,
        *,
        preferred_status: str | None = None,
        preferred_action: str | None = None,
        preferred_stage_seq: int | None = None,
        source: str | None = None,
        runtime_status: str | None = None,
        pending_prompt: bool | None = None,
        pending_hitl: bool | None = None,
    ) -> tuple[str, str, int]:
        action = str(preferred_action or self._context.current_action or self._display_action or "").strip()
        explicit_status = str(preferred_status or self._display_status or "ready").strip() or "ready"
        stage_seq = max(int(preferred_stage_seq or 0), 0) or int(self._display_stage_seq or 0)
        source_text = str(source or "").strip()
        if source_text in {"runner_start", "runner_complete", "runner_failure"}:
            return action, explicit_status, stage_seq
        live_explicit_execution = (
            self._active_explicit_runner_execution(action)
            if source_text in {"", "runtime_inference"}
            else None
        )
        authoritative_terminal = (
            None
            if live_explicit_execution is not None
            else self._load_authoritative_runner_terminal_stage_state(action)
        )
        if authoritative_terminal is not None:
            record_stage_seq = max(int(authoritative_terminal.get("stage_seq") or 0), 0)
            return (
                action,
                str(authoritative_terminal.get("status", "")).strip() or explicit_status,
                record_stage_seq or stage_seq,
            )
        if explicit_status == "awaiting-input" and self._iter_pending_prompts():
            return action, "awaiting-input", stage_seq
        inferred_runtime_status = runtime_status if runtime_status is not None else (self._infer_runtime_stage_status(action) if action else "")
        suppress_recoverable_worker_failure = (
            inferred_runtime_status == "failed"
            and self._active_stage_runner_alive(action)
            and source_text not in {"runner_failure", "runner_complete"}
            and not self._stage_has_stale_ready_task_result_contract(action)
        )
        if inferred_runtime_status == "failed" and not suppress_recoverable_worker_failure:
            return action, inferred_runtime_status, stage_seq
        live_runtime_hitl_pending = bool(self._iter_pending_prompts()) if pending_prompt is None else bool(pending_prompt)
        if not live_runtime_hitl_pending and action:
            if pending_hitl is None:
                live_runtime_hitl_pending = bool(self._build_runtime_worker_hitl_snapshot(action).get("pending", False))
            else:
                live_runtime_hitl_pending = bool(pending_hitl)
        if explicit_status in {"failed", "error"}:
            if source_text == "runner_failure" or (
                source_text in {"", "runtime_inference"}
                and str(getattr(self, "_display_source", "") or "").strip() == "runner_failure"
            ):
                return action, explicit_status, stage_seq
            if live_runtime_hitl_pending:
                return action, "awaiting-input", stage_seq
            if inferred_runtime_status == "running":
                return action, inferred_runtime_status, stage_seq
            return action, explicit_status, stage_seq
        if live_runtime_hitl_pending:
            return action, "awaiting-input", stage_seq
        prompt_pending = bool(self._iter_pending_prompts()) if pending_prompt is None else bool(pending_prompt)
        if prompt_pending:
            return action, "awaiting-input", stage_seq
        hitl_pending = bool(self._build_hitl_snapshot().get("pending", False)) if pending_hitl is None else bool(pending_hitl)
        if hitl_pending:
            return action, "awaiting-input", stage_seq
        if inferred_runtime_status == "running":
            return action, inferred_runtime_status, stage_seq
        if suppress_recoverable_worker_failure:
            return action, "running", stage_seq
        return action, explicit_status, stage_seq

    def _emit_display_stage_state(
        self,
        *,
        preferred_status: str | None = None,
        preferred_action: str | None = None,
        preferred_stage_seq: int | None = None,
        preferred_runner_id: str | None = None,
        source: str | None = None,
        failure_path: str = "",
        failure_kind: str = "",
        message: str = "",
        orphaned_workers: Sequence[Mapping[str, Any]] = (),
        emit_transition: bool = True,
        force: bool = False,
    ) -> bool:
        source_text = str(source or "runtime_inference").strip() or "runtime_inference"
        candidate_action = str(preferred_action or self._context.current_action or self._display_action or "").strip()
        live_explicit_execution = (
            self._active_explicit_runner_execution(candidate_action)
            if source_text == "runtime_inference"
            else None
        )
        runner_id = str(
            preferred_runner_id
            or getattr(self._runner_local, "runner_id", "")
            or getattr(live_explicit_execution, "runner_id", "")
            or self._display_runner_id
            or ""
        ).strip()
        if live_explicit_execution is not None:
            preferred_stage_seq = self._execution_current_stage_seq(live_explicit_execution)
        if source_text in {"runner_start", "runner_complete", "runner_failure"} and self._runner_generation_is_superseded(
            runner_id
        ):
            return False
        action, status, stage_seq = self._derive_display_stage_state(
            preferred_status=preferred_status,
            preferred_action=preferred_action,
            preferred_stage_seq=preferred_stage_seq,
            source=source_text,
        )
        # Serialize the display cursor with runner registration.  The fast
        # superseded check above is intentionally repeated after taking both
        # locks: a newer generation may be registered while the display state
        # is being derived.
        with self._display_state_lock, self._worker_registry_lock:
            if (
                source_text in {"runner_start", "runner_complete", "runner_failure"}
                and self._runner_generation_is_superseded_locked(runner_id)
            ):
                return False
            previous_action = self._display_action
            previous_status = self._display_status
            previous_stage_seq = self._display_stage_seq
            previous_runner_id = self._display_runner_id
            previous_message = self._display_message
            runner_execution = next(
                (
                    execution
                    for execution in self._runner_executions.values()
                    if str(getattr(execution, "runner_id", "") or "").strip() == runner_id
                ),
                None,
            )
            registration_pending = bool(
                source_text == "runner_start"
                and runner_execution is not None
                and not bool(getattr(runner_execution, "generation_registered", True))
                and self._execution_scope_ready_for_registration(runner_execution)
            )
            persistence_project_dir = (
                self._normalize_action_scope_project(str(runner_execution.project_dir or ""))
                if registration_pending and runner_execution is not None
                else self._resolve_project_dir()
            )
            persistence_requirement_name = (
                str(runner_execution.requirement_name or "").strip()
                if registration_pending and runner_execution is not None
                else self._resolve_requirement_name()
            )
            if stage_seq and previous_stage_seq and stage_seq < previous_stage_seq:
                return False
            persisted_state = _read_project_stage_state_record(
                project_dir=persistence_project_dir,
                requirement_name=persistence_requirement_name,
                action=action,
            )
            protect_live_runtime_inference = source_text == "runtime_inference" and live_explicit_execution is not None
            if persisted_state is not None and not protect_live_runtime_inference:
                persisted_seq = int(persisted_state.get("stage_seq") or 0)
                persisted_runner_id = str(persisted_state.get("runner_id", "") or "").strip()
                persisted_source = str(persisted_state.get("source", "") or "").strip()
                persisted_status = str(persisted_state.get("status", "") or "").strip()
                same_generation = (
                    persisted_seq == int(stage_seq or 0)
                    and persisted_runner_id
                    and runner_id
                    and persisted_runner_id == runner_id
                )
                if same_generation and not str(message or "").strip():
                    message = str(persisted_state.get("message", "") or "").strip()
                persisted_is_terminal = (
                    persisted_source in {"runner_complete", "runner_failure"}
                    and persisted_status in {"completed", "failed", "error", "superseded"}
                )
                if same_generation and persisted_is_terminal and source_text in {"runner_complete", "runner_failure"}:
                    if source_text != persisted_source or status != persisted_status:
                        return False
                    if source_text == "runner_failure":
                        persisted_failure_kind = str(persisted_state.get("failure_kind", "") or "").strip()
                        persisted_message = str(persisted_state.get("message", "") or "").strip()
                        if persisted_failure_kind and failure_kind and persisted_failure_kind != failure_kind:
                            return False
                        if persisted_message and message and persisted_message != message:
                            return False
                if (
                    source_text == "runtime_inference"
                    and persisted_source in {"runner_complete", "runner_failure"}
                    and persisted_status in {"completed", "failed", "error", "superseded"}
                ):
                    source_text = persisted_source
                    status = persisted_status
                    stage_seq = persisted_seq or stage_seq
                    runner_id = persisted_runner_id or runner_id
                    failure_path = str(failure_path or persisted_state.get("failure_path") or "").strip()
                    failure_kind = str(failure_kind or persisted_state.get("failure_kind") or "").strip()
                    message = str(message or persisted_state.get("message") or "").strip()
                    orphaned_workers = list(orphaned_workers or persisted_state.get("orphaned_workers") or [])
                if persisted_seq and stage_seq and persisted_seq > stage_seq:
                    return False
                if (
                    persisted_seq
                    and persisted_seq == stage_seq
                    and persisted_runner_id
                    and runner_id
                    and persisted_runner_id != runner_id
                ):
                    return False
            if (
                action == previous_action
                and int(stage_seq or 0) == int(previous_stage_seq or 0)
                and previous_runner_id
                and runner_id
                and runner_id != previous_runner_id
            ):
                return False
            cursor_changed = (
                action != previous_action
                or int(stage_seq or 0) != int(previous_stage_seq or 0)
                or runner_id != previous_runner_id
            )
            incoming_message = str(message or "").strip()
            accepted_message = (
                incoming_message
                if cursor_changed or incoming_message
                else str(previous_message or "").strip()
            )
            if (
                action
                and action == previous_action
                and previous_status in {"failed", "error"}
                and status in {"running", "awaiting-input"}
                and stage_seq <= previous_stage_seq
                and source_text == "runtime_inference"
            ):
                return False
            if not action and status in {"ready", "booting"} and not force:
                self._display_action = action
                self._display_status = status
                self._display_stage_seq = stage_seq
                self._display_source = source_text
                self._display_runner_id = runner_id
                self._display_message = accepted_message
                return True
            if (
                not force
                and action == previous_action
                and status == previous_status
                and stage_seq == previous_stage_seq
                and runner_id == previous_runner_id
                and accepted_message == previous_message
            ):
                return False
            failure_payload = {
                "kind": str(failure_kind or "").strip(),
                "message": accepted_message,
                "path": str(Path(failure_path).expanduser().resolve()) if str(failure_path).strip() else "",
                "runner_id": runner_id,
                "stage_seq": int(stage_seq or 0),
                "updated_at": _iso_now(),
                "orphaned_workers": [dict(item) for item in orphaned_workers],
            }
            should_persist = not protect_live_runtime_inference
            if (
                source_text in {"runner_start", "runner_complete", "runner_failure"}
                and runner_execution is not None
                and not bool(getattr(runner_execution, "generation_registered", True))
            ):
                should_persist = registration_pending
            state_record_path = None
            if should_persist:
                state_record_path = _write_project_stage_state_record(
                    project_dir=persistence_project_dir,
                    requirement_name=persistence_requirement_name,
                    action=action,
                    status=status,
                    stage_seq=stage_seq,
                    runner_id=runner_id,
                    source=source_text,
                    failure_path=failure_path,
                    failure_kind=failure_kind,
                    message=accepted_message,
                    orphaned_workers=orphaned_workers,
                )
                if action and persistence_project_dir and state_record_path is None:
                    return False
            if registration_pending and runner_execution is not None:
                runner_execution.registered_project_dir = persistence_project_dir
                runner_execution.registered_requirement_name = persistence_requirement_name
                runner_execution.generation_registered = True
            self._display_action = action
            self._display_status = status
            self._display_stage_seq = stage_seq
            self._display_source = source_text
            self._display_runner_id = runner_id
            self._display_message = accepted_message
            self._display_failure = failure_payload if status in {"failed", "error"} else {}
            if source_text in {"runner_start", "runner_complete", "runner_failure"} and should_persist:
                self._record_scope_generation(
                    runner_id=runner_id,
                    stage_seq=stage_seq,
                    project_dir=persistence_project_dir,
                    requirement_name=persistence_requirement_name,
                )
            event_payload = {
                "action": action or "idle",
                "stage_label": self._resolve_stage_label(
                    action=action,
                    project_dir=self._resolve_project_dir(),
                    requirement_name=self._resolve_requirement_name(),
                ),
                "status": status,
                "stage_seq": stage_seq,
                "runner_id": runner_id,
                "source": source_text,
                "message": accepted_message,
                "failure_path": failure_payload["path"],
                "failure_kind": str(failure_kind or "").strip(),
                "orphaned_workers": failure_payload["orphaned_workers"],
            }
        if emit_transition:
            with contextlib.suppress(Exception):
                self.emit_event("stage.changed", event_payload)
        return True

    def _emit_snapshot_payload(self, event_type: str, payload_builder: Callable[[], Mapping[str, Any]]) -> None:
        try:
            self.emit_event(event_type, dict(payload_builder()))
        except Exception as error:  # noqa: BLE001
            self._emit_log_error(
                title="snapshot emit failed",
                error=error,
                traceback_text=traceback.format_exc(),
            )

    def _emit_snapshot_update(
        self,
        *,
        include_app: bool = False,
        include_control: bool = False,
        include_hitl: bool = False,
        include_prompt: bool = False,
        include_artifacts: bool = False,
        stage_routes: Sequence[str] | None = None,
        include_all_stages: bool = False,
        refresh_worker_health: bool = True,
    ) -> None:
        previous_scan_cache = self._snapshot_runtime_scan_cache
        self._snapshot_runtime_scan_cache = {}
        selected_routes = tuple(route for route, _builder in STAGE_SNAPSHOT_BUILDERS) if include_all_stages else tuple(stage_routes or ())
        stage_snapshots: dict[str, dict[str, Any]] = {}
        control_snapshot: dict[str, Any] | None = None
        hitl_snapshot: dict[str, Any] | None = None
        attention_snapshot: dict[str, Any] | None = None
        artifacts_snapshot: dict[str, Any] | None = None
        runs: list[Mapping[str, Any]] | None = None

        try:
            with self._snapshot_worker_health_mode(refresh_worker_health):
                if selected_routes:
                    def _build_stages() -> Mapping[str, Any]:
                        nonlocal stage_snapshots
                        if not stage_snapshots:
                            stage_snapshots = self._build_stage_snapshots(selected_routes)
                        return stage_snapshots

                    try:
                        stage_snapshots = dict(_build_stages())
                    except Exception as error:  # noqa: BLE001
                        self._emit_log_error(
                            title="snapshot emit failed",
                            error=error,
                            traceback_text=traceback.format_exc(),
                        )
                        stage_snapshots = {}

                if include_control or include_app or include_artifacts:
                    try:
                        control_snapshot = self._build_control_snapshot_for_session(self._current_control_session())
                    except Exception as error:  # noqa: BLE001
                        self._emit_log_error(
                            title="snapshot emit failed",
                            error=error,
                            traceback_text=traceback.format_exc(),
                        )
                        control_snapshot = {}
                if include_hitl or include_app:
                    try:
                        hitl_snapshot = self._build_hitl_snapshot()
                    except Exception as error:  # noqa: BLE001
                        self._emit_log_error(
                            title="snapshot emit failed",
                            error=error,
                            traceback_text=traceback.format_exc(),
                        )
                        hitl_snapshot = {}
                if include_app:
                    try:
                        attention_snapshot = self._attention_manager.snapshot()
                        runs = self._list_runs()
                    except Exception as error:  # noqa: BLE001
                        self._emit_log_error(
                            title="snapshot emit failed",
                            error=error,
                            traceback_text=traceback.format_exc(),
                        )
                        attention_snapshot = {}
                        runs = []
                if include_artifacts or include_app:
                    try:
                        artifacts_snapshot = self._build_artifacts_snapshot(
                            stages=stage_snapshots,
                            control=control_snapshot or {},
                        )
                    except Exception as error:  # noqa: BLE001
                        self._emit_log_error(
                            title="snapshot emit failed",
                            error=error,
                            traceback_text=traceback.format_exc(),
                        )
                        artifacts_snapshot = {"items": []}

            if include_app:
                self._emit_snapshot_payload(
                    "snapshot.app",
                    lambda: self._build_app_snapshot(
                        runs=runs or [],
                        control=control_snapshot or {},
                        hitl=hitl_snapshot or {},
                        attention=attention_snapshot or {},
                        artifacts=artifacts_snapshot or {"items": []},
                        stage_snapshots=stage_snapshots,
                    ),
                )
            for route in selected_routes:
                snapshot = stage_snapshots.get(route)
                if snapshot is None:
                    continue
                self.emit_event("snapshot.stage", {"route": route, "snapshot": snapshot})
            if include_control:
                self.emit_event("snapshot.control", control_snapshot or {})
            if include_hitl:
                self.emit_event("snapshot.hitl", hitl_snapshot or {"pending": False})
            if include_prompt:
                self.emit_event("snapshot.prompt", self.build_prompt_snapshot())
            if include_artifacts:
                self.emit_event("snapshot.artifacts", artifacts_snapshot or {"items": []})
        finally:
            self._snapshot_runtime_scan_cache = previous_scan_cache

    def _emit_all_snapshots(self) -> None:
        self._emit_snapshot_update(
            include_app=True,
            include_control=True,
            include_hitl=True,
            include_prompt=True,
            include_artifacts=True,
            include_all_stages=True,
        )

    def _schedule_flow_snapshot_update(
        self,
        *,
        sections: set[str],
        stage_routes: Sequence[str] | None = None,
        update_display_stage: bool = False,
        refresh_worker_health: bool | None = None,
    ) -> None:
        normalized_stage_routes = tuple(str(item).strip() for item in (stage_routes or ()) if str(item).strip())
        self._schedule_snapshot_update(
            sections=sections,
            stage_routes=normalized_stage_routes,
            refresh_worker_health=(
                False
                if refresh_worker_health is None
                else bool(refresh_worker_health)
            ),
            update_display_stage=update_display_stage,
        )

    def _schedule_snapshot_update(
        self,
        *,
        sections: set[str],
        stage_routes: Sequence[str] | None = None,
        delay_sec: float | None = None,
        refresh_worker_health: bool = True,
        update_display_stage: bool = False,
    ) -> None:
        normalized_sections = {str(item).strip() for item in sections if str(item).strip()}
        normalized_routes = {str(item).strip() for item in (stage_routes or ()) if str(item).strip()}
        if not normalized_sections and not normalized_routes:
            return
        with self._snapshot_dirty_lock:
            self._snapshot_dirty_sections.update(normalized_sections)
            self._snapshot_dirty_stage_routes.update(normalized_routes)
            self._snapshot_dirty_refresh_worker_health = (
                self._snapshot_dirty_refresh_worker_health or bool(refresh_worker_health)
            )
            self._snapshot_dirty_update_display_stage = (
                self._snapshot_dirty_update_display_stage or bool(update_display_stage)
            )
            if self._snapshot_debounce_timer is not None or self._snapshot_flush_running:
                return
            timer = threading.Timer(
                max(float(self._snapshot_debounce_sec if delay_sec is None else delay_sec), 0.0),
                self._flush_dirty_snapshots,
            )
            timer.daemon = True
            self._snapshot_debounce_timer = timer
            timer.start()

    def _flush_dirty_snapshots(self) -> None:
        with self._snapshot_dirty_lock:
            if self._snapshot_flush_running:
                return
            self._snapshot_flush_running = True
            sections = set(self._snapshot_dirty_sections)
            stage_routes = set(self._snapshot_dirty_stage_routes)
            refresh_worker_health = self._snapshot_dirty_refresh_worker_health
            update_display_stage = self._snapshot_dirty_update_display_stage
            self._snapshot_dirty_sections.clear()
            self._snapshot_dirty_stage_routes.clear()
            self._snapshot_dirty_refresh_worker_health = False
            self._snapshot_dirty_update_display_stage = False
            self._snapshot_debounce_timer = None
        try:
            if update_display_stage:
                previous_refresh_worker_health = self._snapshot_refresh_worker_health
                self._snapshot_refresh_worker_health = bool(refresh_worker_health)
                try:
                    self._emit_display_stage_state()
                finally:
                    self._snapshot_refresh_worker_health = previous_refresh_worker_health
            self._emit_snapshot_update(
                include_app="app" in sections,
                include_control="control" in sections,
                include_hitl="hitl" in sections,
                include_prompt="prompt" in sections,
                include_artifacts="artifacts" in sections,
                stage_routes=tuple(sorted(stage_routes)),
                refresh_worker_health=refresh_worker_health,
            )
        finally:
            follow_up_timer: threading.Timer | None = None
            with self._snapshot_dirty_lock:
                self._snapshot_flush_running = False
                has_follow_up = bool(self._snapshot_dirty_sections or self._snapshot_dirty_stage_routes)
                if (
                    has_follow_up
                    and self._snapshot_debounce_timer is None
                    and not self._shutdown_started
                ):
                    follow_up_timer = threading.Timer(0.0, self._flush_dirty_snapshots)
                    follow_up_timer.daemon = True
                    self._snapshot_debounce_timer = follow_up_timer
            if follow_up_timer is not None:
                follow_up_timer.start()

    @staticmethod
    def _result_exit_code(result: Any) -> int:
        if isinstance(result, bool):
            return int(result)
        if isinstance(result, int):
            return result
        if isinstance(result, Mapping):
            try:
                return int(result.get("exit_code", 0))
            except Exception:
                return 0
        try:
            return int(getattr(result, "exit_code", 0))
        except Exception:
            return 0

    @staticmethod
    def _get_mapping_or_attr(source: Any, key: str, default: Any = None) -> Any:
        if isinstance(source, Mapping):
            return source.get(key, default)
        return getattr(source, key, default)

    @classmethod
    def _result_failure_summary(cls, result: Any) -> str:
        batch_result = cls._get_mapping_or_attr(result, "batch_result")
        if not batch_result:
            return ""
        results = cls._get_mapping_or_attr(batch_result, "results", ())
        if not isinstance(results, Sequence) or isinstance(results, (str, bytes)):
            return ""
        lines: list[str] = []
        for item in results:
            status = str(
                cls._get_mapping_or_attr(item, "status", "")
                or cls._get_mapping_or_attr(item, "result_status", "")
            ).strip()
            if status != "failed":
                continue
            work_dir = str(cls._get_mapping_or_attr(item, "work_dir", "")).strip() or "(unknown)"
            reason = str(
                cls._get_mapping_or_attr(item, "failure_reason", "")
                or cls._get_mapping_or_attr(item, "note", "")
                or status
            ).strip()
            if len(reason) > 500:
                reason = f"{reason[:497]}..."
            lines.append(f"- {work_dir}: {reason}")
        if not lines:
            return ""
        if len(lines) > 5:
            remaining = len(lines) - 5
            lines = lines[:5] + [f"- ... and {remaining} more failed target(s)"]
        return "failed routing targets:\n" + "\n".join(lines)

    def _resolve_terminal_stage_target(
        self,
        *,
        fallback_action: str,
        fallback_stage_seq: int,
    ) -> tuple[str, int]:
        runner_id = str(getattr(self._runner_local, "runner_id", "") or "").strip()
        execution = getattr(self._runner_local, "execution", None)
        if execution is not None:
            execution_action = self._execution_current_action(execution)
            execution_stage_seq = self._execution_current_stage_seq(execution)
            if execution_action:
                return execution_action, execution_stage_seq or int(fallback_stage_seq or 0)
        with self._display_state_lock:
            current_display_action = str(self._display_action or "").strip()
            current_display_runner_id = str(self._display_runner_id or "").strip()
            current_display_stage_seq = int(self._display_stage_seq or 0)
        if runner_id and current_display_runner_id and current_display_runner_id != runner_id:
            return str(fallback_action or "").strip(), int(fallback_stage_seq or 0)
        current_context_action = str(self._context.current_action or "").strip()
        action = current_display_action or current_context_action or str(fallback_action or "").strip()
        stage_seq = current_display_stage_seq or int(fallback_stage_seq or 0)
        return action, stage_seq

    def _raise_for_nonzero_exit_code(
        self,
        *,
        action: str,
        stage_seq: int,
        result: Any,
    ) -> None:
        exit_code = self._result_exit_code(result)
        if exit_code == 0:
            return
        final_action, _final_stage_seq = self._resolve_terminal_stage_target(
            fallback_action=action,
            fallback_stage_seq=stage_seq,
        )
        message = f"{final_action or action} exited with non-zero code: {exit_code}"
        failure_summary = self._result_failure_summary(result)
        if failure_summary:
            message = f"{message}\n{failure_summary}"
        raise RuntimeError(message)

    def _build_requirement_intake_argv(
        self,
        *,
        stage_a01_argv: Sequence[str],
        result: Any,
    ) -> list[str]:
        project_dir = ""
        if isinstance(result, Mapping):
            project_dir = str(result.get("project_dir", "")).strip()
        else:
            project_dir = str(getattr(result, "project_dir", "") or "").strip()
        try:
            parsed = build_a01_parser().parse_args(list(stage_a01_argv))
        except Exception:
            parsed = None
        if not project_dir and parsed is not None:
            project_dir = str(getattr(parsed, "project_dir", "") or "").strip()
        if not project_dir:
            project_dir = self._resolve_project_dir()
        if not project_dir:
            return []
        argv = ["--project-dir", project_dir]
        requirement_name = str(getattr(parsed, "requirement_name", "") or "").strip() if parsed is not None else ""
        reuse_existing_original_requirement = (
            bool(getattr(parsed, "reuse_existing_original_requirement", False))
            if parsed is not None
            else False
        )
        existing_requirements: tuple[str, ...] = ()
        if project_dir and not reuse_existing_original_requirement:
            try:
                existing_requirements = list_existing_requirements(project_dir)
            except Exception:
                existing_requirements = ()
        if requirement_name and (reuse_existing_original_requirement or not existing_requirements):
            argv.extend(["--requirement-name", requirement_name])
        if reuse_existing_original_requirement:
            argv.append("--reuse-existing-original-requirement")
        if parsed is not None and bool(getattr(parsed, "yes", False)):
            argv.append("--yes")
        if parsed is not None and bool(getattr(parsed, "no_tui", False)):
            argv.append("--no-tui")
        if parsed is not None and bool(getattr(parsed, "legacy_cli", False)):
            argv.append("--legacy-cli")
        return argv

    def _maybe_chain_after_stage_success(
        self,
        *,
        action: str,
        argv: Sequence[str],
        result: Any,
    ) -> None:
        if action != "stage.a01.start":
            return
        if self._result_exit_code(result) != 0:
            return
        followup_argv = self._build_requirement_intake_argv(stage_a01_argv=argv, result=result)
        if not followup_argv:
            return
        self.emit_event("log.append", {"text": "自动进入需求录入阶段\n"})
        self._run_in_thread(
            "",
            "stage.a02.start",
            lambda: run_requirement_intake_stage(followup_argv),
            argv=followup_argv,
            respond=False,
        )

    def _persist_runner_awaiting_input(
        self,
        *,
        action: str,
        stage_seq: int,
        message: str,
    ) -> bool:
        runner_id = str(getattr(self._runner_local, "runner_id", "") or "").strip()
        return self._emit_display_stage_state(
            preferred_status="awaiting-input",
            preferred_action=action,
            preferred_stage_seq=stage_seq,
            preferred_runner_id=runner_id,
            source="runner_start",
            message=message,
            force=True,
        )

    def _await_agent_ready_timeout_recovery(
        self,
        *,
        request_id: str,
        action: str,
        stage_seq: int,
        error: BaseException,
        respond: bool,
    ) -> None:
        final_action, final_stage_seq = self._resolve_terminal_stage_target(
            fallback_action=action,
            fallback_stage_seq=stage_seq,
        )
        message_text = str(error or "").strip()
        runtime_intervention = is_agent_runtime_intervention_error(error)
        startup_intervention = is_agent_startup_intervention_error(error) and not runtime_intervention
        if runtime_intervention:
            recovery_kind = "agent_runtime_intervention"
        elif startup_intervention:
            recovery_kind = "agent_startup_intervention"
        else:
            recovery_kind = "agent_ready_timeout"
        workers = self._filter_workers_for_current_context(
            self._current_stage_workers_without_runtime_io(final_action or action),
            final_action or action,
        )
        if not workers:
            workers = self._filter_workers_for_current_context(
                self._current_stage_workers(final_action or action),
                final_action or action,
            )
        worker_context = sorted(workers, key=_worker_snapshot_sort_key, reverse=True)
        current_worker = worker_context[0] if worker_context else {}
        state_path = str(getattr(error, "state_path", "") or current_worker.get("state_path", "")).strip()
        session_name = str(getattr(error, "session_name", "") or current_worker.get("session_name", "")).strip()
        role_label = session_name.split("-", 1)[0] if session_name else "当前智能体"
        attach_command = f"tmux attach -t {session_name}" if session_name else ""
        if runtime_intervention:
            title = "HITL: 智能体运行期权限介入"
            intervention_message = "检测到智能体运行期需要人工处理，已转为 HITL，系统不会标记为失败。"
            prompt_text = "请先进入原 tmux 会话处理运行期权限确认，然后重新检查。"
        elif startup_intervention:
            title = "HITL: 智能体启动需要人工介入"
            intervention_message = "检测到智能体启动需要人工处理，已转为 HITL，系统不会标记为失败。"
            prompt_text = "请先进入原 tmux 会话处理该 AGENT，然后重新检查。"
        else:
            title = "HITL: 智能体启动超时"
            intervention_message = "检测到智能体启动超时，已转为 HITL，系统不会标记为失败。"
            prompt_text = "请先进入原 tmux 会话检查该 AGENT，然后重新检查。"
        self.emit_event(
            "log.append",
            {
                "text": (
                    f"{intervention_message}\n"
                    + (f"{attach_command}\n" if attach_command else "")
                ),
                "log_kind": "warning",
                "log_title": recovery_kind,
            },
        )
        self._pending_prompt_display_state = {
            "preferred_status": "awaiting-input",
            "preferred_action": final_action or action,
            "preferred_stage_seq": final_stage_seq,
            "preferred_runner_id": str(getattr(self._runner_local, "runner_id", "") or "").strip(),
            "source": "runner_start",
            "message": message_text,
            "force": True,
        }
        self._persist_runner_awaiting_input(
            action=final_action or action,
            stage_seq=final_stage_seq,
            message=message_text,
        )
        recovered = False
        try:
            self._prompt_broker.request(
                BridgePromptRequest(
                    prompt_type="select",
                    payload={
                        "title": title,
                        "prompt_text": prompt_text,
                        "options": [
                            {
                                "value": "recheck_after_manual_intervention",
                                "label": "我已处理，重新检查",
                            }
                        ],
                        "default_value": "recheck_after_manual_intervention",
                        "is_hitl": True,
                        "recovery_kind": recovery_kind,
                        "session_name": session_name,
                        "role_label": role_label,
                        "attach_command": attach_command,
                        "state_path": state_path,
                        "can_skip": False,
                    },
                )
            )
            backend = getattr(self._tmux_runtime, "backend", None)
            recovered_worker = load_worker_from_state_path(state_path, backend=backend) if state_path else None
            if recovered_worker is not None:
                if runtime_intervention:
                    checker = getattr(recovered_worker, "runtime_intervention_is_resolved", None)
                    if callable(checker):
                        recovered = bool(checker(str(getattr(error, "blocker_kind", "") or "")))
                    else:
                        recovered = try_resume_worker(recovered_worker, timeout_sec=60.0)
                else:
                    recovered = try_resume_worker(recovered_worker, timeout_sec=60.0)
        finally:
            self._pending_prompt_display_state = None
        if recovered:
            self.emit_event(
                "log.append",
                {
                    "text": "人工处理后已确认智能体 READY；原阶段调用栈若已退出，请重新发起当前阶段。\n",
                    "log_kind": "success",
                    "log_title": "agent startup recovered",
                },
            )
        if respond and request_id:
            self.emit_response(
                request_id,
                ok=True,
                payload={
                    "awaiting_input": not recovered,
                    "recovered": recovered,
                    "recovery_kind": recovery_kind,
                    "session_name": session_name,
                    "attach_command": attach_command,
                    "message": message_text,
                },
            )

    def _manual_reconfiguration_error_pending(self, *, action: str, error: BaseException) -> bool:
        message = str(error or "").strip()
        lowered = message.lower()
        if not (
            is_worker_death_error(error)
            or "需要重新启动或重建" in message
            or "重新选择" in message
            or "awaiting_reconfig" in lowered
        ):
            return False
        if self._matching_manual_reconfiguration_prompt(action=action, session_name="") is not None:
            return True
        workers = self._filter_workers_for_current_context(
            self._current_stage_workers_without_runtime_io(action),
            action,
        )
        if any(_is_recoverable_reconfig_snapshot(worker) for worker in workers):
            return True
        return "重新选择" in message or "awaiting_reconfig" in lowered

    def _matching_manual_reconfiguration_prompt(
        self,
        *,
        action: str,
        session_name: str,
    ) -> PendingPromptState | None:
        normalized_action = str(action or "").strip()
        normalized_session = str(session_name or "").strip()
        current_runner_id = str(getattr(self._runner_local, "runner_id", "") or "").strip()
        for prompt in reversed(self._iter_pending_prompts()):
            payload = prompt.payload
            if str(payload.get("recovery_kind", "") or "").strip() != "manual_reconfiguration":
                continue
            prompt_session = str(payload.get("session_name", "") or "").strip()
            if normalized_session and prompt_session != normalized_session:
                continue
            prompt_action = str(payload.get("workflow_action", payload.get("action", "")) or "").strip()
            if prompt_action and normalized_action and prompt_action != normalized_action:
                continue
            if not normalized_session and (not prompt_action or prompt_action != normalized_action):
                continue
            if current_runner_id and prompt.owner_runner_id and prompt.owner_runner_id != current_runner_id:
                continue
            return prompt
        return None

    @staticmethod
    def _is_requirement_concurrency_conflict(error: BaseException) -> bool:
        return REQUIREMENT_CONCURRENCY_CONFLICT_MARKER in str(error or "")

    @staticmethod
    def _normalize_action_scope_project(project_dir: str) -> str:
        text = str(project_dir or "").strip()
        if not text:
            return ""
        try:
            return str(Path(text).expanduser().resolve())
        except Exception:
            return text

    def _runner_dedup_key(self, action: str, argv: Sequence[str] | None) -> str:
        normalized_action = str(action or "").strip()
        if normalized_action not in RUNNER_DEDUP_ACTIONS:
            return ""
        project_dir = ""
        requirement_name = ""
        try:
            args = self._parse_stage_args_for_action(normalized_action, list(argv or []))
        except Exception:
            args = None
        if args is not None:
            project_dir = self._normalize_action_scope_project(str(getattr(args, "project_dir", "") or ""))
            requirement_name = str(getattr(args, "requirement_name", "") or "").strip()
        return "\0".join((normalized_action, project_dir, requirement_name))

    def _runner_scope_from_argv(
        self,
        action: str,
        argv: Sequence[str] | None,
    ) -> tuple[str, str]:
        if argv is None:
            return (
                self._normalize_action_scope_project(str(self._context.project_dir or "")),
                str(self._context.requirement_name or "").strip(),
            )
        try:
            args = self._parse_stage_args_for_action(str(action or "").strip(), list(argv or []))
        except Exception:
            args = None
        if args is None:
            return (
                self._normalize_action_scope_project(str(self._context.project_dir or "")),
                str(self._context.requirement_name or "").strip(),
            )
        requested_project = self._normalize_action_scope_project(
            str(getattr(args, "project_dir", "") or self._context.project_dir or "")
        )
        requested_requirement = str(getattr(args, "requirement_name", "") or "").strip()
        return requested_project, requested_requirement

    def _assert_no_active_cross_scope_runner_locked(
        self,
        *,
        requested_project: str,
        requested_requirement: str,
        requesting_runner_id: str = "",
    ) -> None:
        normalized_requesting_runner_id = str(requesting_runner_id or "").strip()
        for worker_key, execution in self._runner_executions.items():
            thread = self._workers.get(worker_key)
            if thread is None or execution is None:
                continue
            if (
                normalized_requesting_runner_id
                and str(getattr(execution, "runner_id", "") or "").strip()
                == normalized_requesting_runner_id
            ):
                continue
            try:
                if not thread.is_alive():
                    continue
            except Exception:
                pass
            active_project = self._normalize_action_scope_project(
                str(getattr(execution, "project_dir", "") or "")
            )
            active_requirement = str(getattr(execution, "requirement_name", "") or "").strip()
            if requested_project == active_project and requested_requirement == active_requirement:
                continue
            raise RuntimeError(
                "当前 Backend 已有其他项目或需求的 runner 在执行；为避免阶段状态串写，本次启动已拒绝。"
            )

    def _assert_no_active_cross_scope_runner(self, action: str, argv: Sequence[str]) -> None:
        requested_project, requested_requirement = self._runner_scope_from_argv(action, argv)
        with self._worker_registry_lock:
            self._assert_no_active_cross_scope_runner_locked(
                requested_project=requested_project,
                requested_requirement=requested_requirement,
                requesting_runner_id=str(getattr(self._runner_local, "runner_id", "") or ""),
            )

    def _emit_duplicate_runner_response(
        self,
        *,
        request_id: str,
        action: str,
        respond: bool,
        reason: str,
        stage_seq: int = 0,
        mark_failed: bool = False,
    ) -> None:
        message_text = str(reason or "").strip() or "已有同一任务在运行，本次重复启动已忽略。"
        self.emit_event(
            "log.append",
            {
                "text": f"{message_text}\n",
                "log_kind": "warning",
                "log_title": "duplicate runner ignored",
            },
        )
        failure_record_path = None
        if mark_failed:
            failure_record_path = _write_project_stage_failure_record(
                project_dir=self._resolve_project_dir(),
                requirement_name=self._resolve_requirement_name(),
                action=action,
                error=RuntimeError(message_text),
                traceback_text="",
            )
            if failure_record_path is not None:
                self.emit_event("log.append", {"text": f"阶段失败记录: {failure_record_path}\n"})
            self.emit_event(
                "error",
                {"action": action, "message": message_text, "traceback": ""},
            )
            self._emit_display_stage_state(
                preferred_status="failed",
                preferred_action=action,
                preferred_stage_seq=stage_seq,
                source="runner_failure",
                failure_path=str(failure_record_path or ""),
                message=message_text,
                force=True,
            )
        if respond and request_id:
            self.emit_response(
                request_id,
                ok=False,
                error=message_text,
                payload={
                    "accepted": True,
                    "deferred": True,
                    "already_running": True,
                    "action": str(action or "").strip(),
                    "message": message_text,
                },
            )
        self._schedule_flow_snapshot_update(
            sections={"app", "artifacts"},
            stage_routes=self._stage_routes_for_action(action),
        )

    def _await_manual_reconfiguration_recovery(
        self,
        *,
        request_id: str,
        action: str,
        stage_seq: int,
        error: BaseException,
        respond: bool,
    ) -> None:
        message_text = str(error or "").strip()
        workers = self._filter_workers_for_current_context(
            self._current_stage_workers_without_runtime_io(action),
            action,
        )
        reconfig_workers = [worker for worker in workers if _is_recoverable_reconfig_snapshot(worker)]
        worker_context = sorted(reconfig_workers or workers, key=_worker_snapshot_sort_key, reverse=True)
        current_worker = worker_context[0] if worker_context else {}
        session_name = str(current_worker.get("session_name", "")).strip()
        worker_id = str(current_worker.get("worker_id", "")).strip()
        role_label = session_name.split("-", 1)[0] if session_name else (worker_id or "当前智能体")
        runner_id = str(getattr(self._runner_local, "runner_id", "") or "").strip()
        prompt_payload = {
            "title": "HITL: 智能体需要人工重配",
            "prompt_text": "请先手动更换模型或处理该 AGENT，然后选择继续尝试。",
            "options": [
                {
                    "value": "retry_after_manual_reconfiguration",
                    "label": "我已手动处理，继续/重新尝试",
                }
            ],
            "default_value": "retry_after_manual_reconfiguration",
            "is_hitl": True,
            "recovery_kind": "manual_reconfiguration",
            "workflow_action": str(action or "").strip(),
            "stage_runner_id": runner_id,
            "project_dir": self._resolve_project_dir(),
            "requirement_name": self._resolve_requirement_name(),
            "session_name": session_name,
            "role_label": role_label,
            "attach_command": _prompt_attach_command(current_worker),
            "error": message_text,
            "can_skip": False,
        }
        self.emit_event(
            "log.append",
            {
                "text": "检测到智能体需要人工重配，已转为等待人类输入，系统不会标记为失败。\n",
                "log_kind": "warning",
                "log_title": "manual reconfiguration required",
            },
        )
        prompt_response: Mapping[str, Any] = {}
        pending_display_state = {
            "preferred_status": "awaiting-input",
            "preferred_action": action,
            "preferred_stage_seq": stage_seq,
            "preferred_runner_id": runner_id,
            "source": "runner_start",
            "message": message_text,
            "force": True,
        }
        self._persist_runner_awaiting_input(
            action=action,
            stage_seq=stage_seq,
            message=message_text,
        )
        matching_prompt = self._matching_manual_reconfiguration_prompt(
            action=action,
            session_name=session_name,
        )
        if matching_prompt is not None:
            self._emit_display_stage_state(**pending_display_state)
        else:
            self._pending_prompt_display_state = pending_display_state
            try:
                prompt_response = self._prompt_broker.request(
                    BridgePromptRequest(
                        prompt_type="select",
                        payload=prompt_payload,
                    )
                )
            finally:
                self._pending_prompt_display_state = None
        if respond and request_id:
            self.emit_response(
                request_id,
                ok=True,
                payload={
                    "awaiting_input": True,
                    "recovery_kind": "manual_reconfiguration",
                    "message": message_text,
                    "prompt_response": dict(prompt_response),
                },
            )
        self._schedule_flow_snapshot_update(
            sections={"app", "artifacts"},
            stage_routes=self._stage_routes_for_action(action),
        )

    def _runner_transition_is_current(self, action: str, *, stage_seq: int, runner_id: str) -> bool:
        if self._runner_generation_is_superseded(runner_id):
            return False
        with self._display_state_lock:
            if self._display_stage_seq and stage_seq and stage_seq < self._display_stage_seq:
                return False
            persisted_state = _read_project_stage_state_record(
                project_dir=self._resolve_project_dir(),
                requirement_name=self._resolve_requirement_name(),
                action=action,
            )
            if persisted_state is None:
                return True
            persisted_seq = int(persisted_state.get("stage_seq") or 0)
            persisted_runner_id = str(persisted_state.get("runner_id", "") or "").strip()
            persisted_source = str(persisted_state.get("source", "") or "").strip()
            persisted_status = str(persisted_state.get("status", "") or "").strip()
            if persisted_seq and stage_seq and persisted_seq > stage_seq:
                return False
            if (
                persisted_seq == int(stage_seq or 0)
                and persisted_runner_id
                and runner_id
                and persisted_runner_id != runner_id
            ):
                return False
            if (
                persisted_seq == int(stage_seq or 0)
                and persisted_runner_id
                and runner_id
                and persisted_runner_id == runner_id
                and persisted_source in {"runner_complete", "runner_failure"}
                and persisted_status in {"completed", "failed", "error", "superseded"}
            ):
                return False
            return True

    def _commit_runner_failure(
        self,
        *,
        action: str,
        stage_seq: int,
        runner_id: str,
        error: BaseException,
        traceback_text: str,
        failure_kind: str,
    ) -> tuple[Path | None, list[dict[str, str]], bool]:
        normalized_action = str(action or "").strip()
        normalized_runner_id = str(runner_id or "").strip()
        with self._display_state_lock:
            if not self._runner_transition_is_current(
                normalized_action,
                stage_seq=stage_seq,
                runner_id=normalized_runner_id,
            ):
                return None, [], False
            self._latch_shutdown_policy(
                ShutdownPolicy.CLEANUP,
                reason=failure_kind,
            )
            failure_record_path = _write_project_stage_failure_record(
                project_dir=self._resolve_project_dir(),
                requirement_name=self._resolve_requirement_name(),
                action=normalized_action,
                error=error,
                traceback_text=traceback_text,
                runner_id=normalized_runner_id,
                stage_seq=stage_seq,
                failure_kind=failure_kind,
                orphaned_workers=(),
            )
            if self._resolve_project_dir() and failure_record_path is None:
                return None, [], False
            accepted = self._emit_display_stage_state(
                preferred_status="failed",
                preferred_action=normalized_action,
                preferred_stage_seq=stage_seq,
                preferred_runner_id=normalized_runner_id,
                source="runner_failure",
                failure_path=str(failure_record_path or ""),
                failure_kind=failure_kind,
                message=str(error),
                orphaned_workers=(),
                emit_transition=False,
                force=True,
            )
            if accepted:
                self._mark_runner_execution_terminal(
                    normalized_runner_id,
                    source="runner_failure",
                )
            orphaned_workers: list[dict[str, str]] = []
            accepted = self._emit_display_stage_state(
                preferred_status="failed",
                preferred_action=normalized_action,
                preferred_stage_seq=stage_seq,
                preferred_runner_id=normalized_runner_id,
                source="runner_failure",
                failure_path=str(failure_record_path or ""),
                failure_kind=failure_kind,
                message=str(error),
                orphaned_workers=orphaned_workers,
                emit_transition=True,
                force=True,
            ) and accepted
        return failure_record_path, orphaned_workers, accepted

    def _best_effort_emit_response(
        self,
        request_id: str,
        *,
        ok: bool,
        payload: Mapping[str, Any] | None = None,
        error: str = "",
    ) -> None:
        with contextlib.suppress(Exception):
            self.emit_response(request_id, ok=ok, payload=payload, error=error)

    def _best_effort_emit_event(self, event_type: str, payload: Mapping[str, Any] | None = None) -> None:
        with contextlib.suppress(Exception):
            self.emit_event(event_type, payload)

    def _run_in_thread(
        self,
        request_id: str,
        action: str,
        runner: Callable[[], Any],
        *,
        argv: Sequence[str] | None = None,
        respond: bool = True,
    ) -> None:
        worker_key = str(request_id).strip() or f"auto-{action}-{dt.datetime.now().timestamp()}"
        dedup_key = self._runner_dedup_key(action, argv)
        runner_id = f"runner_{uuid.uuid4().hex}"
        stage_seq = 0
        runner_execution: RunnerExecutionState | None = None

        def target() -> None:
            execution = runner_execution
            if execution is None:
                return
            self._runner_local.runner_id = runner_id
            self._runner_local.execution = execution
            try:
                with (
                    stage_runner_context(runner_id),
                    use_terminal_ui(self._bridge_ui),
                    contextlib.redirect_stdout(self._protocol_log_sink),
                ):
                    runner_start_accepted = self._emit_display_stage_state(
                        preferred_status="running",
                        preferred_action=action,
                        preferred_stage_seq=stage_seq,
                        preferred_runner_id=runner_id,
                        source="runner_start",
                        message="解析参数",
                        force=True,
                    )
                    if not runner_start_accepted:
                        self._mark_runner_execution_terminal(
                            runner_id,
                            source="runner_start_rejected",
                        )
                        if respond and request_id:
                            self._best_effort_emit_response(
                                request_id,
                                ok=False,
                                error="runner generation was superseded or its start state could not be persisted",
                            )
                        return
                    self._cleanup_stage_orphans_before_runner_start(action, runner_id=runner_id)
                    result = runner()
                if self._runner_generation_is_superseded(runner_id):
                    if respond and request_id:
                        self._best_effort_emit_response(
                            request_id,
                            ok=False,
                            error="runner generation was superseded by a newer explicit run",
                        )
                    return
                self._update_context_from_result(result, action=action)
                if action == "stage.a01.start":
                    self._update_routing_manifest_suppression_from_result(result)
                self._raise_for_nonzero_exit_code(action=action, stage_seq=stage_seq, result=result)
                self._validate_stage_success_before_completed(action=action, result=result)
                final_action, final_stage_seq = self._resolve_terminal_stage_target(
                    fallback_action=action,
                    fallback_stage_seq=stage_seq,
                )
                terminal_status = "completed"
                with contextlib.suppress(Exception):
                    if bool(self._build_hitl_snapshot().get("pending", False)):
                        terminal_status = "awaiting-input"
                terminal_transition_accepted = self._emit_display_stage_state(
                    preferred_status=terminal_status,
                    preferred_action=final_action,
                    preferred_stage_seq=final_stage_seq,
                    preferred_runner_id=runner_id,
                    source="runner_complete",
                    force=True,
                )
                if terminal_transition_accepted:
                    self._mark_runner_execution_terminal(
                        runner_id,
                        source="runner_complete",
                    )
                else:
                    return
                if respond and request_id:
                    self._best_effort_emit_response(request_id, ok=True, payload={"result": _serialize(result)})
                with contextlib.suppress(Exception):
                    self._schedule_flow_snapshot_update(
                        sections={"app", "artifacts"},
                        stage_routes=self._stage_routes_for_action(final_action),
                    )
                if argv is not None:
                    self._maybe_chain_after_stage_success(action=action, argv=argv, result=result)
            except BaseException as error:  # noqa: BLE001
                if self._runner_generation_is_superseded(runner_id):
                    self._mark_runner_execution_terminal(runner_id, source="superseded")
                    if respond and request_id:
                        self._best_effort_emit_response(
                            request_id,
                            ok=False,
                            error="runner generation was superseded by a newer explicit run",
                        )
                    return
                trace = traceback.format_exc()
                final_action, final_stage_seq = self._resolve_terminal_stage_target(
                    fallback_action=action,
                    fallback_stage_seq=stage_seq,
                )
                if not isinstance(error, Exception):
                    failure_record_path, orphaned_workers, accepted = self._commit_runner_failure(
                        action=final_action or action,
                        stage_seq=final_stage_seq,
                        runner_id=runner_id,
                        error=error,
                        traceback_text=trace,
                        failure_kind="runner_interrupted",
                    )
                    if accepted and failure_record_path is not None:
                        self._best_effort_emit_event(
                            "log.append",
                            {"text": f"阶段中断记录: {failure_record_path}\n"},
                        )
                    if respond and request_id:
                        self._best_effort_emit_response(
                            request_id,
                            ok=False,
                            error=str(error) or type(error).__name__,
                            payload={"traceback": trace},
                        )
                    if accepted:
                        self._best_effort_emit_event(
                            "error",
                            {
                                "action": final_action or action,
                                "message": str(error) or type(error).__name__,
                                "traceback": trace,
                                "runner_id": runner_id,
                                "stage_seq": final_stage_seq,
                                "source": "runner_failure",
                                "failure_path": str(failure_record_path or ""),
                                "failure_kind": "runner_interrupted",
                                "orphaned_workers": orphaned_workers,
                            },
                        )
                    return
                if is_runtime_shutdown_error(error):
                    failure_record_path, _orphaned_workers, accepted = self._commit_runner_failure(
                        action=final_action or action,
                        stage_seq=final_stage_seq,
                        runner_id=runner_id,
                        error=error,
                        traceback_text=trace,
                        failure_kind="runner_interrupted",
                    )
                    if accepted and failure_record_path is not None:
                        self._best_effort_emit_event(
                            "log.append",
                            {"text": f"阶段中断记录: {failure_record_path}\n"},
                        )
                    if accepted:
                        with contextlib.suppress(Exception):
                            self._schedule_flow_snapshot_update(
                                sections={"app", "artifacts"},
                                stage_routes=self._stage_routes_for_action(final_action or action),
                                refresh_worker_health=False,
                            )
                    return
                if is_agent_runtime_intervention_error(error):
                    self._await_agent_ready_timeout_recovery(
                        request_id=request_id,
                        action=final_action or action,
                        stage_seq=final_stage_seq,
                        error=error,
                        respond=respond,
                    )
                    self._mark_runner_execution_terminal(runner_id, source="awaiting_input")
                    return
                if is_agent_startup_intervention_error(error):
                    self._await_agent_ready_timeout_recovery(
                        request_id=request_id,
                        action=final_action or action,
                        stage_seq=final_stage_seq,
                        error=error,
                        respond=respond,
                    )
                    self._mark_runner_execution_terminal(runner_id, source="awaiting_input")
                    return
                if is_agent_ready_timeout_error(error):
                    self._await_agent_ready_timeout_recovery(
                        request_id=request_id,
                        action=final_action or action,
                        stage_seq=final_stage_seq,
                        error=error,
                        respond=respond,
                    )
                    self._mark_runner_execution_terminal(runner_id, source="awaiting_input")
                    return
                if self._is_requirement_concurrency_conflict(error):
                    self._emit_duplicate_runner_response(
                        request_id=request_id,
                        action=final_action or action,
                        respond=respond,
                        reason=f"{error}\n已检测到同项目同需求已有运行中任务，本次启动已终止。",
                        stage_seq=final_stage_seq,
                    )
                    self._mark_runner_execution_terminal(runner_id, source="concurrency_conflict")
                    return
                if self._manual_reconfiguration_error_pending(action=final_action or action, error=error):
                    self._await_manual_reconfiguration_recovery(
                        request_id=request_id,
                        action=final_action or action,
                        stage_seq=final_stage_seq,
                        error=error,
                        respond=respond,
                    )
                    self._mark_runner_execution_terminal(runner_id, source="awaiting_input")
                    return
                failure_record_path, orphaned_workers, accepted = self._commit_runner_failure(
                    action=final_action or action,
                    stage_seq=final_stage_seq,
                    runner_id=runner_id,
                    error=error,
                    traceback_text=trace,
                    failure_kind="runner_failure",
                )
                if not accepted:
                    return
                with contextlib.suppress(Exception):
                    self._emit_log_error(
                        action=final_action or action,
                        title="stage execution failed",
                        error=error,
                        traceback_text=trace,
                    )
                if failure_record_path is not None:
                    self._best_effort_emit_event("log.append", {"text": f"阶段失败记录: {failure_record_path}\n"})
                if respond and request_id:
                    self._best_effort_emit_response(
                        request_id,
                        ok=False,
                        error=str(error),
                        payload={"traceback": trace},
                    )
                self._best_effort_emit_event(
                    "error",
                    {
                        "action": final_action or action,
                        "message": str(error),
                        "traceback": trace,
                        "runner_id": runner_id,
                        "stage_seq": final_stage_seq,
                        "source": "runner_failure",
                        "failure_path": str(failure_record_path or ""),
                        "failure_kind": "runner_failure",
                        "orphaned_workers": orphaned_workers,
                    },
                )
                with contextlib.suppress(Exception):
                    self._schedule_flow_snapshot_update(
                        sections={"app", "artifacts"},
                        stage_routes=self._stage_routes_for_action(final_action or action),
                        refresh_worker_health=False,
                    )
            finally:
                if execution is not None and not str(execution.terminal_source or "").strip():
                    final_action = self._execution_current_action(execution) or action
                    final_stage_seq = self._execution_current_stage_seq(execution) or stage_seq
                    if self._runner_generation_has_persisted_terminal(
                        execution=execution,
                        action=final_action,
                        stage_seq=final_stage_seq,
                    ):
                        self._mark_runner_execution_terminal(runner_id, source="persisted_terminal")
                    elif self._runner_generation_is_superseded(runner_id):
                        self._mark_runner_execution_terminal(runner_id, source="superseded")
                    else:
                        with contextlib.suppress(BaseException):
                            self._commit_runner_failure(
                                action=final_action,
                                stage_seq=final_stage_seq,
                                runner_id=runner_id,
                                error=RuntimeShutdownRequested(
                                    "runner thread exited without an authoritative terminal state"
                                ),
                                traceback_text="",
                                failure_kind="runner_interrupted",
                            )
                self._runner_local.runner_id = ""
                self._runner_local.execution = None
                with self._worker_registry_lock:
                    self._workers.pop(worker_key, None)
                    self._runner_executions.pop(worker_key, None)
                    if dedup_key and self._running_action_keys.get(dedup_key) == worker_key:
                        self._running_action_keys.pop(dedup_key, None)

        # Runner cleanup is still attempted during shutdown, but interpreter exit must not be
        # blocked by a stuck workflow thread. Process teardown releases flock locks, and tmux
        # cleanup is already handled explicitly by backend shutdown.
        duplicate_running = False
        with self._worker_registry_lock:
            requested_project, requested_requirement = self._runner_scope_from_argv(action, argv)
            self._assert_no_active_cross_scope_runner_locked(
                requested_project=requested_project,
                requested_requirement=requested_requirement,
                requesting_runner_id=str(getattr(self._runner_local, "runner_id", "") or ""),
            )
            if dedup_key:
                existing_worker_key = self._running_action_keys.get(dedup_key, "")
                existing_worker = self._workers.get(existing_worker_key)
                if existing_worker is not None and existing_worker.is_alive():
                    duplicate_running = True
                if existing_worker_key and not duplicate_running:
                    self._running_action_keys.pop(dedup_key, None)
            if not duplicate_running:
                if str(action or "").strip() in RUNNER_DEDUP_ACTIONS:
                    self._set_context(
                        project_dir=requested_project or None,
                        requirement_name=requested_requirement,
                        action=action,
                    )
                stage_seq = self._allocate_stage_seq(
                    project_dir=requested_project,
                    requirement_name=requested_requirement,
                )
                runner_execution = RunnerExecutionState(
                    runner_id=runner_id,
                    action=str(action or "").strip(),
                    stage_seq=stage_seq,
                    project_dir=requested_project,
                    requirement_name=requested_requirement,
                    current_action=str(action or "").strip(),
                    current_stage_seq=stage_seq,
                )
                thread = threading.Thread(
                    target=target,
                    name=f"tui-backend-{action}-{request_id}",
                    daemon=True,
                )
                self._workers[worker_key] = thread
                self._runner_executions[worker_key] = runner_execution
                if dedup_key:
                    self._running_action_keys[dedup_key] = worker_key
                try:
                    thread.start()
                except Exception:
                    self._workers.pop(worker_key, None)
                    self._runner_executions.pop(worker_key, None)
                    if dedup_key and self._running_action_keys.get(dedup_key) == worker_key:
                        self._running_action_keys.pop(dedup_key, None)
                    raise
        if duplicate_running:
            self._emit_duplicate_runner_response(
                request_id=request_id,
                action=action,
                respond=respond,
                reason="已有同一任务在运行，本次重复启动已忽略。",
            )

    def _get_control_session(self, control_id: str) -> ControlSessionState:
        key = str(control_id or "").strip()
        if not key:
            raise ValueError("缺少 control_id")
        with self._controls_lock:
            session = self._controls.get(key)
        if session is None:
            raise KeyError(f"未找到 control session: {key}")
        return session

    def _set_control_session(self, session: ControlSessionState) -> None:
        with self._controls_lock:
            self._controls[session.control_id] = session
        self._active_control_id = session.control_id
        selection = getattr(session.center, "selection", None)
        self._set_context(project_dir=str(getattr(selection, "project_dir", "") or "").strip() or None, action="control.b01.open")

    def _clear_control_session(self, control_id: str) -> None:
        with self._controls_lock:
            session = self._controls.pop(control_id, None)
            if self._active_control_id == control_id:
                self._active_control_id = ""
        if session is not None:
            session.center.close()

    def _cleanup_visible_tmux_workers(self, *, reason: str) -> list[str]:
        cleaned_sessions: list[str] = []
        seen_sessions: set[str] = set()
        actions = ("control.b01.open", *WORKFLOW_STAGE_ACTION_ORDER.keys())
        for action in actions:
            try:
                workers = self._filter_workers_for_current_context(self._current_stage_workers(action), action)
            except Exception:
                continue
            for worker in workers:
                session_name = str(worker.get("session_name", "")).strip()
                if not session_name or session_name in seen_sessions:
                    continue
                seen_sessions.add(session_name)
                if not self._worker_snapshot_has_live_session(worker):
                    continue
                try:
                    cleaned_session = self._tmux_runtime.kill_session(session_name, missing_ok=True)
                except Exception:
                    continue
                if cleaned_session:
                    cleaned_sessions.append(cleaned_session)
        return sorted(set(cleaned_sessions))

    @staticmethod
    def _path_is_within_project(path_value: str, project_root: Path) -> bool:
        if not str(path_value or "").strip():
            return True
        try:
            candidate = Path(path_value).expanduser().resolve()
        except Exception:
            return False
        return candidate == project_root or project_root in candidate.parents

    def _project_runtime_roots(self, project_root: Path) -> tuple[Path, ...]:
        root_names = (
            ROUTING_RUNTIME_ROOT_NAME,
            NOTION_RUNTIME_ROOT_NAME,
            REQUIREMENTS_RUNTIME_ROOT_NAME,
            LEGACY_REQUIREMENTS_RUNTIME_ROOT_NAME,
            REQUIREMENTS_REVIEW_RUNTIME_ROOT_NAME,
            DETAILED_DESIGN_RUNTIME_ROOT_NAME,
            TASK_SPLIT_RUNTIME_ROOT_NAME,
            DEVELOPMENT_RUNTIME_ROOT_NAME,
        )
        return tuple(project_root / name for name in root_names)

    def _cleanup_project_runtime_tmux_workers(self, *, reason: str) -> list[str]:
        _ = reason
        project_dir = str(self._resolve_project_dir() or "").strip()
        if not project_dir:
            return []
        try:
            project_root = Path(project_dir).expanduser().resolve()
        except Exception:
            return []
        cleaned_sessions: list[str] = []
        seen_sessions: set[str] = set()
        for runtime_root in self._project_runtime_roots(project_root):
            if not runtime_root.exists() or not runtime_root.is_dir():
                continue
            for state_path in self._iter_worker_state_paths(runtime_root):
                payload = _safe_json_read(state_path)
                if not payload:
                    continue
                payload_project = str(payload.get("project_dir", "") or "").strip()
                payload_work_dir = str(payload.get("work_dir", "") or "").strip()
                if payload_project and not self._path_is_within_project(payload_project, project_root):
                    continue
                if payload_work_dir and not self._path_is_within_project(payload_work_dir, project_root):
                    continue
                session_name = str(payload.get("session_name", "") or "").strip()
                if not session_name or session_name in seen_sessions:
                    continue
                seen_sessions.add(session_name)
                try:
                    if not self._tmux_runtime.session_exists(session_name):
                        continue
                    cleaned_session = self._tmux_runtime.kill_session(session_name, missing_ok=True)
                except Exception:
                    continue
                if cleaned_session:
                    cleaned_sessions.append(cleaned_session)
        return sorted(set(cleaned_sessions))

    def _list_identity_tmux_sessions(self) -> list[dict[str, str]]:
        backend = getattr(self._tmux_runtime, "backend", None)
        list_sessions = getattr(self._tmux_runtime, "list_sessions", None)
        if backend is None or not callable(list_sessions):
            return []
        snapshots: list[dict[str, str]] = []
        for session_name in list_sessions():
            session_name_text = str(session_name or "").strip()
            if not session_name_text:
                continue
            runtime_dir = _backend_show_option(backend, session_name_text, TMUX_IDENTITY_RUNTIME_DIR_OPTION)
            work_dir = _backend_show_option(backend, session_name_text, TMUX_IDENTITY_WORK_DIR_OPTION)
            requirement_name = _backend_show_option(backend, session_name_text, TMUX_IDENTITY_REQUIREMENT_NAME_OPTION)
            workflow_action = _backend_show_option(backend, session_name_text, TMUX_IDENTITY_WORKFLOW_ACTION_OPTION)
            worker_id = _backend_show_option(backend, session_name_text, TMUX_IDENTITY_WORKER_ID_OPTION)
            if not any((runtime_dir, work_dir, requirement_name, workflow_action, worker_id)):
                continue
            snapshots.append(
                {
                    "session_name": session_name_text,
                    "runtime_dir": runtime_dir,
                    "work_dir": work_dir,
                    "requirement_name": requirement_name,
                    "workflow_action": workflow_action,
                    "worker_id": worker_id,
                }
            )
        return snapshots

    def _list_current_project_tmux_sessions(self) -> list[dict[str, str]]:
        project_dir = str(self._resolve_project_dir() or "").strip()
        if not project_dir:
            return []
        try:
            project_root = Path(project_dir).expanduser().resolve()
        except Exception:
            return []
        snapshots: list[dict[str, str]] = []
        for item in self._list_identity_tmux_sessions():
            runtime_dir = item["runtime_dir"]
            work_dir = item["work_dir"]
            if runtime_dir and self._path_is_within_project(runtime_dir, project_root):
                snapshots.append(item)
                continue
            if work_dir and self._path_is_within_project(work_dir, project_root):
                snapshots.append(item)
        return snapshots

    def _cleanup_current_project_tmux_sessions(
        self,
        *,
        reason: str,
        exclude_sessions: Sequence[str] = (),
    ) -> list[str]:
        _ = reason
        cleaned_sessions: list[str] = []
        seen_sessions = {str(session_name or "").strip() for session_name in exclude_sessions if str(session_name or "").strip()}
        for item in self._list_current_project_tmux_sessions():
            session_name = str(item.get("session_name", "") or "").strip()
            if not session_name or session_name in seen_sessions:
                continue
            seen_sessions.add(session_name)
            try:
                cleaned_session = self._tmux_runtime.kill_session(session_name, missing_ok=True)
            except Exception:
                continue
            if cleaned_session:
                cleaned_sessions.append(cleaned_session)
        return sorted(set(cleaned_sessions))

    def _list_foreign_project_tmux_sessions(self) -> list[dict[str, str]]:
        project_dir = str(self._resolve_project_dir() or "").strip()
        if not project_dir:
            return []
        try:
            project_root = Path(project_dir).expanduser().resolve()
        except Exception:
            return []
        snapshots: list[dict[str, str]] = []
        for item in self._list_identity_tmux_sessions():
            runtime_dir = item["runtime_dir"]
            work_dir = item["work_dir"]
            if runtime_dir and self._path_is_within_project(runtime_dir, project_root):
                continue
            if work_dir and self._path_is_within_project(work_dir, project_root):
                continue
            snapshots.append(item)
        return snapshots

    def _join_active_runner_threads(self, *, timeout_sec: float) -> None:
        deadline = time.monotonic() + max(0.0, timeout_sec)
        current_thread = threading.current_thread()
        with self._worker_registry_lock:
            threads = [thread for thread in self._workers.values() if thread is not current_thread]
        for thread in threads:
            try:
                if not thread.is_alive():
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return
                thread.join(timeout=min(remaining, 0.5))
            except Exception:
                continue

    def _mark_runner_execution_terminal(self, runner_id: str, *, source: str) -> None:
        normalized_runner_id = str(runner_id or "").strip()
        if not normalized_runner_id:
            return
        with self._worker_registry_lock:
            for execution in self._runner_executions.values():
                if str(getattr(execution, "runner_id", "") or "").strip() != normalized_runner_id:
                    continue
                execution.terminal_source = str(source or "").strip()
                execution.terminal_at = _iso_now()

    def _runner_generation_has_persisted_terminal(
        self,
        *,
        execution: RunnerExecutionState,
        action: str,
        stage_seq: int,
    ) -> bool:
        runner_id = str(getattr(execution, "runner_id", "") or "").strip()
        if not runner_id:
            return False
        persisted_state = _read_project_stage_state_record(
            project_dir=str(getattr(execution, "project_dir", "") or self._resolve_project_dir()),
            requirement_name=str(
                getattr(execution, "requirement_name", "")
                or self._context.requirement_name
                or ""
            ).strip(),
            action=action,
        )
        if not persisted_state:
            return False
        if str(persisted_state.get("runner_id", "") or "").strip() != runner_id:
            return False
        if int(persisted_state.get("stage_seq") or 0) != int(stage_seq or 0):
            return False
        source = str(persisted_state.get("source", "") or "").strip()
        status = str(persisted_state.get("status", "") or "").strip()
        return source in {"runner_failure", "runner_complete"} and status in {
            "completed",
            "failed",
            "error",
            "superseded",
        }

    def _mark_active_runners_interrupted(self, *, reason: str) -> None:
        with self._worker_registry_lock:
            active = [
                (worker_key, thread, self._runner_executions.get(worker_key))
                for worker_key, thread in self._workers.items()
            ]
        for _worker_key, thread, execution in active:
            if execution is None:
                continue
            with contextlib.suppress(Exception):
                if not thread.is_alive():
                    continue
            if str(getattr(execution, "terminal_source", "") or "").strip():
                continue
            action = self._execution_current_action(execution)
            stage_seq = self._execution_current_stage_seq(execution)
            with self._display_state_lock:
                if self._display_runner_id == execution.runner_id:
                    action = str(self._display_action or action).strip()
                    stage_seq = int(self._display_stage_seq or stage_seq)
            if self._runner_generation_has_persisted_terminal(
                execution=execution,
                action=action,
                stage_seq=stage_seq,
            ):
                self._mark_runner_execution_terminal(execution.runner_id, source="persisted_terminal")
                continue
            self._commit_runner_failure(
                action=action,
                stage_seq=stage_seq,
                runner_id=execution.runner_id,
                error=RuntimeShutdownRequested(reason),
                traceback_text="",
                failure_kind="runner_interrupted",
            )

    def _latch_shutdown_policy(self, policy: ShutdownPolicy, *, reason: str = "") -> ShutdownPolicy:
        with self._shutdown_lock:
            if self._shutdown_policy is None:
                self._shutdown_policy = policy
                self._shutdown_policy_reason = str(reason or "").strip()
            return self._shutdown_policy

    def _current_shutdown_policy(self) -> ShutdownPolicy | None:
        with self._shutdown_lock:
            return self._shutdown_policy

    def shutdown(self, *, cleanup_tmux: bool) -> list[str]:
        # ``cleanup_tmux`` is retained for source compatibility. Program shutdown
        # always owns and cleans its tmux sessions; callers can no longer opt out.
        _ = cleanup_tmux
        request_runtime_shutdown("tui_backend_shutdown")
        self._latch_shutdown_policy(ShutdownPolicy.CLEANUP, reason="tui_backend_shutdown")
        first_shutdown = False
        with self._shutdown_lock:
            if not self._shutdown_started:
                self._shutdown_started = True
                first_shutdown = True
            elif self._shutdown_tmux_cleanup_done:
                return []
        if first_shutdown:
            with self._snapshot_dirty_lock:
                timer = self._snapshot_debounce_timer
                self._snapshot_debounce_timer = None
                self._snapshot_dirty_sections.clear()
                self._snapshot_dirty_stage_routes.clear()
                self._snapshot_dirty_refresh_worker_health = False
                self._snapshot_dirty_update_display_stage = False
            if timer is not None:
                timer.cancel()
            with self._tui_presence_refresh_lock:
                presence_timer = self._tui_presence_refresh_timer
                self._tui_presence_refresh_timer = None
            if presence_timer is not None:
                presence_timer.cancel()
            self._prompt_broker.shutdown()
            self._attention_manager.shutdown()
            self._mark_active_runners_interrupted(reason="tui_backend_shutdown")
            with self._controls_lock:
                sessions = list(self._controls.values())
                self._controls.clear()
            for session in sessions:
                try:
                    session.center.close()
                except Exception:
                    continue
        cleaned_sessions: list[str] = []
        self._join_active_runner_threads(timeout_sec=3.0)
        cleaned_set: set[str] = set()
        cleanup_steps: tuple[tuple[str, Callable[[], Sequence[str]]], ...] = (
            (
                "registered workers",
                lambda: cleanup_registered_tmux_workers(reason="tui_backend_shutdown"),
            ),
            (
                "visible workers",
                lambda: self._cleanup_visible_tmux_workers(reason="tui_backend_shutdown"),
            ),
            (
                "project runtime workers",
                lambda: self._cleanup_project_runtime_tmux_workers(reason="tui_backend_shutdown"),
            ),
        )
        for label, cleanup_step in cleanup_steps:
            try:
                with allow_runtime_shutdown_cleanup():
                    cleaned_set.update(str(item) for item in cleanup_step() if str(item).strip())
            except Exception as error:  # noqa: BLE001
                self._best_effort_emit_event(
                    "log.append",
                    {"text": f"tmux 清理警告 ({label}): {error}\n"},
                )
        try:
            with allow_runtime_shutdown_cleanup():
                cleaned_set.update(
                    self._cleanup_current_project_tmux_sessions(
                        reason="tui_backend_shutdown",
                        exclude_sessions=sorted(cleaned_set),
                    )
                )
        except Exception as error:  # noqa: BLE001
            self._best_effort_emit_event(
                "log.append",
                {"text": f"tmux 清理警告 (project identity sessions): {error}\n"},
            )
        cleaned_sessions = sorted(cleaned_set)
        if cleaned_sessions:
            self._best_effort_emit_event(
                "log.append",
                {"text": f"已清理 tmux 会话: {', '.join(cleaned_sessions)}\n"},
            )
        try:
            with allow_runtime_shutdown_cleanup():
                foreign_sessions = self._list_foreign_project_tmux_sessions()
        except Exception as error:  # noqa: BLE001
            foreign_sessions = []
            self._best_effort_emit_event(
                "log.append",
                {"text": f"tmux 清理警告 (foreign session audit): {error}\n"},
            )
        if foreign_sessions:
            lines = ["检测到其他项目的存活 tmux 会话；当前退出不会清理它们:"]
            for item in foreign_sessions:
                session_name = item["session_name"]
                work_dir = item["work_dir"] or item["runtime_dir"] or "(unknown)"
                requirement_name = item["requirement_name"] or "(unknown)"
                workflow_action = item["workflow_action"] or "(unknown)"
                lines.append(
                    f"- {session_name} | {work_dir} | {requirement_name} | {workflow_action}"
                )
            self._best_effort_emit_event("log.append", {"text": "\n".join(lines) + "\n"})
        with self._shutdown_lock:
            self._shutdown_tmux_cleanup_done = True
        with contextlib.suppress(Exception):
            self._protocol_log_sink.flush()
        return cleaned_sessions

    def _snapshot_control_session(self, session: ControlSessionState) -> dict[str, Any]:
        return self._build_control_snapshot_for_session(session)

    def _open_control_session(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        control_id = str(payload.get("control_id", "")).strip()
        if control_id:
            self._active_control_id = control_id
            return self._snapshot_control_session(self._get_control_session(control_id))

        argv = self._argv_from_payload(payload)
        self._update_context_from_stage_args("stage.a01.start", argv)
        parser = build_a01_parser()
        args = parser.parse_args(argv)
        if getattr(args, "resume_run", ""):
            project_dir = str(getattr(args, "project_dir", "") or "").strip() or self._resolve_routing_project_dir(payload)
            if not project_dir:
                raise ValueError("当前只支持恢复当前项目内的 routing run；请先选择项目或传入 --project-dir。")
            center = AgentInitControlCenter.from_existing_run(
                run_id=str(args.resume_run).strip(),
                project_dir=project_dir,
                max_refine_rounds=max(int(args.max_refine_rounds or 3), 1),
            )
            center.start()
            session = ControlSessionState(control_id=center.run_id, center=center)
            self._set_control_session(session)
            return self._snapshot_control_session(session)

        request = collect_b01_request(args)
        selection = resolve_batch_selection(request)
        if not selection.should_run:
            return {
                "supported": True,
                "control_id": "",
                "run_id": "",
                "runtime_dir": "",
                "status_text": "当前项目路由层已完备，跳过路由初始化。",
                "help_text": render_control_help(),
                "workers": [],
                "done": True,
                "can_switch_runs": True,
                "final_summary": render_noop_summary(request, None, selection),
                "transition_text": render_requirements_stage_placeholder(()),
            }

        config = prepare_agent_run_config(request)

        preflight_summary = render_preflight_summary(request, config, selection)
        force_confirmation = bool(selection.project_missing_files)
        if not request.auto_confirm and not prompt_confirmation(preflight_summary, force_yes=force_confirmation):
            return {
                "supported": True,
                "control_id": "",
                "run_id": "",
                "runtime_dir": "",
                "status_text": "已取消执行。",
                "help_text": render_control_help(),
                "workers": [],
                "done": True,
                "can_switch_runs": True,
                "final_summary": "",
                "transition_text": "",
            }

        center = AgentInitControlCenter.create_new(
            selection=selection,
            config=config,
            max_refine_rounds=request.max_refine_rounds,
        )
        center.start()
        session = ControlSessionState(control_id=center.run_id, center=center)
        self._set_control_session(session)
        return self._snapshot_control_session(session)

    def _run_worker_control_action(
        self,
        *,
        control_id: str,
        argument: str,
        handler: Callable[[AgentInitControlCenter, str], Any],
        reset_done_state: bool = False,
    ) -> dict[str, Any]:
        session = self._get_control_session(control_id)
        if reset_done_state:
            session.final_result = None
            session.transition_text = ""
        handler(session.center, argument)
        return self._snapshot_control_session(session)

    def _handle_worker_attach(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        session = self._get_control_session(str(payload.get("control_id", "")))
        target = session.center.get_target(str(payload.get("argument", "")))
        if not target.session_name:
            raise RuntimeError("tmux 会话尚未创建")
        snapshot = self._snapshot_control_session(session)
        snapshot.update(
            {
                "attach_session_name": target.session_name,
                "attach_command": ["tmux", "attach", "-t", target.session_name],
                "transcript_path": target.transcript_path,
                "work_dir": target.work_dir,
            }
        )
        return snapshot

    def _handle_resume_control(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        current_control_id = str(payload.get("control_id", "")).strip()
        run_id = str(payload.get("run_id", "")).strip()
        if not run_id:
            raise ValueError("run.resume 缺少 run_id")
        project_dir = self._resolve_routing_project_dir(payload)
        if not project_dir:
            raise ValueError("run.resume 仅支持恢复当前项目内的 routing run；请先选择项目或传入 project_dir。")
        if current_control_id:
            current = self._get_control_session(current_control_id)
            if not current.center.can_switch_runs():
                raise RuntimeError("当前 run 尚未完成，不能切换到其他 run。")
            self._snapshot_control_session(current)
            if current.final_result is not None and any(
                getattr(item, "status", "") == "failed" for item in getattr(current.final_result, "results", [])
            ):
                current.center.cleanup_routing_tmux_sessions()
            self._clear_control_session(current_control_id)
        center = AgentInitControlCenter.from_existing_run(run_id=run_id, project_dir=project_dir)
        center.start()
        session = ControlSessionState(control_id=center.run_id, center=center)
        self._set_control_session(session)
        return self._snapshot_control_session(session)

    def _handle_run_list(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        project_dir = self._resolve_routing_project_dir(payload)
        if not project_dir:
            return {"runs": []}
        self._set_context(project_dir=project_dir)
        return {"runs": self._list_runs()}

    @staticmethod
    def _add_preview_path(allowed: set[str], value: object) -> None:
        text = str(value or "").strip()
        if not text:
            return
        try:
            path = Path(text).expanduser().resolve()
        except Exception:
            return
        if path.exists() and path.is_file():
            allowed.add(str(path))

    @classmethod
    def _add_preview_paths_from_worker(cls, allowed: set[str], worker: Mapping[str, Any]) -> None:
        for key in ("transcript_path", "turn_status_path", "question_path", "answer_path"):
            cls._add_preview_path(allowed, worker.get(key, ""))
        artifact_paths = worker.get("artifact_paths", [])
        if isinstance(artifact_paths, Sequence) and not isinstance(artifact_paths, (str, bytes)):
            for path in artifact_paths:
                cls._add_preview_path(allowed, path)

    @classmethod
    def _add_preview_paths_from_prompt(cls, allowed: set[str], prompt: PendingPromptState) -> None:
        for key in ("preview_path", "question_path", "answer_path"):
            cls._add_preview_path(allowed, prompt.payload.get(key, ""))

    def build_prompt_snapshot(self) -> dict[str, Any]:
        with self._pending_prompt_lock:
            prompt = self._current_pending_prompt_locked()
            prompt_revision = self._prompt_revision
        if prompt is None:
            return {
                "pending": False,
                "prompt_id": "",
                "prompt_type": "",
                "payload": {},
                "prompt_revision": prompt_revision,
            }
        return {
            "pending": True,
            "prompt_id": prompt.prompt_id,
            "prompt_type": prompt.prompt_type,
            "payload": dict(prompt.payload),
            "prompt_revision": prompt_revision,
        }

    def _allowed_file_preview_paths(
        self,
        *,
        stages: Mapping[str, Mapping[str, Any]] | None = None,
        control: Mapping[str, Any] | None = None,
        hitl: Mapping[str, Any] | None = None,
        artifacts: Mapping[str, Any] | None = None,
    ) -> set[str]:
        allowed: set[str] = set()
        stage_snapshots = dict(stages) if stages is not None else self._build_stage_snapshots()
        control_snapshot = dict(control) if control is not None else self._build_control_snapshot_for_session(self._current_control_session())
        hitl_snapshot = dict(hitl) if hitl is not None else self._build_hitl_snapshot()
        artifacts_snapshot = dict(artifacts) if artifacts is not None else self._build_artifacts_snapshot(
            stages=stage_snapshots,
            control=control_snapshot,
        )
        for prompt in self._iter_pending_prompts():
            self._add_preview_paths_from_prompt(allowed, prompt)
        for key in ("question_path", "answer_path"):
            self._add_preview_path(allowed, hitl_snapshot.get(key, ""))
        for snapshot in stage_snapshots.values():
            for file_item in snapshot.get("files", []):
                if isinstance(file_item, Mapping):
                    self._add_preview_path(allowed, file_item.get("path", ""))
            for worker in snapshot.get("workers", []):
                if isinstance(worker, Mapping):
                    self._add_preview_paths_from_worker(allowed, worker)
        for worker in control_snapshot.get("workers", []):
            if isinstance(worker, Mapping):
                self._add_preview_paths_from_worker(allowed, worker)
        for artifact in artifacts_snapshot.get("items", []):
            if isinstance(artifact, Mapping):
                self._add_preview_path(allowed, artifact.get("path", ""))
        with self._display_state_lock:
            active_failure = dict(self._display_failure)
            active_action = str(self._display_action or self._context.current_action or "").strip()
        self._add_preview_path(
            allowed,
            active_failure.get("failure_path") or active_failure.get("path") or "",
        )
        persisted_state = _read_project_stage_state_record(
            project_dir=self._resolve_project_dir(),
            requirement_name=self._resolve_requirement_name(),
            action=active_action,
        )
        if (
            isinstance(persisted_state, Mapping)
            and str(persisted_state.get("source", "") or "").strip() == "runner_failure"
        ):
            self._add_preview_path(allowed, persisted_state.get("failure_path", ""))
        return allowed

    def build_file_preview(self, path_value: str | Path, *, max_bytes: int = WEB_FILE_PREVIEW_MAX_BYTES) -> dict[str, Any]:
        requested = Path(path_value).expanduser().resolve()
        allowed = self._allowed_file_preview_paths()
        if str(requested) not in allowed:
            raise PermissionError(f"文件未在当前 Web 快照中授权预览: {requested}")
        return _preview_path_text(requested, max_bytes=max_bytes)

    def build_snapshots(self) -> dict[str, Any]:
        with self._display_state_lock:
            reconcile_action = str(self._display_action or self._context.current_action or "").strip()
        self._reconcile_persisted_running_runner(reconcile_action)
        stages = self._build_stage_snapshots()
        control = self._build_control_snapshot_for_session(self._current_control_session())
        hitl = self._build_hitl_snapshot()
        attention = self._attention_manager.snapshot()
        artifacts = self._build_artifacts_snapshot(stages=stages, control=control)
        prompt = self.build_prompt_snapshot()
        app = self._build_app_snapshot(
            runs=self._list_runs(),
            control=control,
            hitl=hitl,
            attention=attention,
            artifacts=artifacts,
        )
        return {
            "app": app,
            "stages": stages,
            "control": control,
            "hitl": hitl,
            "artifacts": artifacts,
            "prompt": prompt,
        }

    def build_bootstrap_payload(self) -> dict[str, Any]:
        return {
            "protocol_version": PROTOCOL_VERSION,
            "python_path": SYSTEM_PYTHON_PATH,
            "routes": ["home", "routing", "requirements", "review", "design", "task-split", "development", "overall-review", "control"],
            "commands": [
                "app.bootstrap",
                "app.shutdown",
                "workflow.a00.start",
                "stage.a01.start",
                "stage.a02.start",
                "stage.a03.start",
                "stage.a04.start",
                "stage.a05.start",
                "stage.a06.start",
                "stage.a07.start",
                "stage.a08.start",
                "control.b01.open",
                "worker.attach",
                "worker.detach",
                "worker.kill",
                "worker.restart",
                "worker.retry",
                "run.list",
                "run.resume",
            ],
            "capabilities": {
                "structured_snapshots": True,
                "run_resume_picker": True,
                "bridge_only_terminal_ui": str(self._adapter_name or "").strip().lower() == "tui",
                "web_file_preview": str(self._adapter_name or "").strip().lower() == "web",
                "pending_prompt_snapshot": True,
            },
            "snapshots": self.build_snapshots(),
        }

    def bootstrap(self) -> dict[str, Any]:
        return self.build_bootstrap_payload()

    def resolve_prompt(self, prompt_id: str, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        prompt_id_text = str(prompt_id or "").strip()
        if not prompt_id_text:
            raise ValueError("prompt.response 缺少 prompt_id")
        response_payload = dict(payload or {})
        accepted = self._prompt_broker.resolve(prompt_id_text, response_payload)
        return {"accepted": accepted}

    def dispatch_action(
        self,
        action: str,
        payload: Mapping[str, Any] | None = None,
        *,
        request_id: str = "",
        respond: bool = True,
    ) -> dict[str, Any]:
        normalized_action = str(action or "").strip()
        normalized_request_id = str(request_id or "").strip()
        request_payload = dict(payload or {})

        if normalized_action == "app.shutdown":
            requested_policy_text = str(request_payload.get("policy", "cleanup") or "cleanup").strip().lower()
            if requested_policy_text not in {"cleanup", "preserve_orphans"}:
                raise ValueError("app.shutdown policy 仅支持 cleanup 或 preserve_orphans")
            reason = str(request_payload.get("reason", "app.shutdown") or "app.shutdown").strip()
            # ``preserve_orphans`` remains accepted for protocol compatibility
            # with older clients, but program exit always resolves to cleanup.
            effective_policy = self._latch_shutdown_policy(ShutdownPolicy.CLEANUP, reason=reason)
            response_payload = {
                "accepted": True,
                "policy": effective_policy.value.lower(),
                "reason": self._shutdown_policy_reason or reason,
            }
            if respond and normalized_request_id:
                self.emit_response(normalized_request_id, ok=True, payload=response_payload)
            return response_payload
        if normalized_action == "app.bootstrap":
            bootstrap_payload = self.bootstrap()
            if respond and normalized_request_id:
                self.emit_response(normalized_request_id, ok=True, payload=bootstrap_payload)
            self._emit_display_stage_state()
            return bootstrap_payload
        if normalized_action == "prompt.response":
            response_payload = self.resolve_prompt(
                str(request_payload.get("prompt_id", "")).strip(),
                request_payload,
            )
            if respond and normalized_request_id:
                self.emit_response(normalized_request_id, ok=True, payload=response_payload)
            return response_payload
        if normalized_action == "ui.presence":
            result = self.record_tui_presence(
                str(request_payload.get("reason", "")).strip(),
                str(request_payload.get("shell_focus", "")).strip(),
            )
            if respond and normalized_request_id:
                self.emit_response(normalized_request_id, ok=True, payload=result)
            return result
        if normalized_action == "workflow.a00.start":
            argv = self._argv_from_payload(request_payload)
            argv = self._argv_with_payload_agent_config(argv, request_payload)
            self._run_in_thread(
                normalized_request_id if respond else "",
                normalized_action,
                lambda: a00_main(argv),
                argv=argv,
                respond=respond,
            )
            return {"accepted": True, "deferred": True}
        if normalized_action == "stage.a01.start":
            argv = self._argv_from_payload(request_payload)
            self._run_in_thread(normalized_request_id if respond else "", normalized_action, lambda: run_routing_stage(argv), argv=argv, respond=respond)
            return {"accepted": True, "deferred": True}
        if normalized_action == "stage.a02.start":
            argv = self._argv_from_payload(request_payload)
            self._run_in_thread(
                normalized_request_id if respond else "",
                normalized_action,
                lambda: run_requirement_intake_stage(argv),
                argv=argv,
                respond=respond,
            )
            return {"accepted": True, "deferred": True}
        if normalized_action == "stage.a03.start":
            argv = self._argv_from_payload(request_payload)
            preserve = bool(request_payload.get("preserve_ba_worker", False))
            self._run_in_thread(
                normalized_request_id if respond else "",
                normalized_action,
                lambda: run_requirements_clarification_stage(argv, preserve_ba_worker=preserve),
                argv=argv,
                respond=respond,
            )
            return {"accepted": True, "deferred": True}
        if normalized_action == "stage.a04.start":
            argv = self._argv_from_payload(request_payload)
            self._run_in_thread(
                normalized_request_id if respond else "",
                normalized_action,
                lambda: run_requirements_review_stage(argv),
                argv=argv,
                respond=respond,
            )
            return {"accepted": True, "deferred": True}
        if normalized_action == "stage.a05.start":
            argv = self._argv_from_payload(request_payload)
            self._run_in_thread(
                normalized_request_id if respond else "",
                normalized_action,
                lambda: run_detailed_design_stage(argv),
                argv=argv,
                respond=respond,
            )
            return {"accepted": True, "deferred": True}
        if normalized_action == "stage.a06.start":
            argv = self._argv_from_payload(request_payload)
            self._run_in_thread(
                normalized_request_id if respond else "",
                normalized_action,
                lambda: run_task_split_stage(argv),
                argv=argv,
                respond=respond,
            )
            return {"accepted": True, "deferred": True}
        if normalized_action == "stage.a07.start":
            argv = self._argv_from_payload(request_payload)
            self._run_in_thread(
                normalized_request_id if respond else "",
                normalized_action,
                lambda: run_development_stage(argv, preserve_workers=True),
                argv=argv,
                respond=respond,
            )
            return {"accepted": True, "deferred": True}
        if normalized_action == "stage.a08.start":
            argv = self._argv_with_default_a08_review_max_rounds(self._argv_from_payload(request_payload))
            self._run_in_thread(
                normalized_request_id if respond else "",
                normalized_action,
                lambda: run_overall_review_stage(argv, preserve_workers=True),
                argv=argv,
                respond=respond,
            )
            return {"accepted": True, "deferred": True}
        if normalized_action == "control.b01.open":
            if str(request_payload.get("control_id", "")).strip():
                result = self._open_control_session(request_payload)
                if respond and normalized_request_id:
                    self.emit_response(normalized_request_id, ok=True, payload=result)
                self._schedule_flow_snapshot_update(sections={"control"})
                return result
            self._run_in_thread(normalized_request_id if respond else "", normalized_action, lambda: self._open_control_session(request_payload), respond=respond)
            return {"accepted": True, "deferred": True}
        if normalized_action == "worker.attach":
            result = self._handle_worker_attach(request_payload)
            if respond and normalized_request_id:
                self.emit_response(normalized_request_id, ok=True, payload=result)
            self._schedule_flow_snapshot_update(sections={"app", "control"})
            return result
        if normalized_action == "worker.detach":
            result = self._run_worker_control_action(
                control_id=str(request_payload.get("control_id", "")).strip(),
                argument=str(request_payload.get("argument", "")).strip(),
                handler=lambda center, arg: center.detach(arg),
            )
            if respond and normalized_request_id:
                self.emit_response(normalized_request_id, ok=True, payload=result)
            self._schedule_flow_snapshot_update(sections={"app", "control"})
            return result
        if normalized_action == "worker.kill":
            result = self._run_worker_control_action(
                control_id=str(request_payload.get("control_id", "")).strip(),
                argument=str(request_payload.get("argument", "")).strip(),
                handler=lambda center, arg: center.kill_worker(arg),
            )
            if respond and normalized_request_id:
                self.emit_response(normalized_request_id, ok=True, payload=result)
            self._schedule_flow_snapshot_update(sections={"app", "control"})
            return result
        if normalized_action == "worker.restart":
            result = self._run_worker_control_action(
                control_id=str(request_payload.get("control_id", "")).strip(),
                argument=str(request_payload.get("argument", "")).strip(),
                handler=lambda center, arg: center.restart_worker(arg),
                reset_done_state=True,
            )
            if respond and normalized_request_id:
                self.emit_response(normalized_request_id, ok=True, payload=result)
            self._schedule_flow_snapshot_update(sections={"app", "control"})
            return result
        if normalized_action == "worker.retry":
            result = self._run_worker_control_action(
                control_id=str(request_payload.get("control_id", "")).strip(),
                argument=str(request_payload.get("argument", "")).strip(),
                handler=lambda center, arg: center.retry_worker(arg),
                reset_done_state=True,
            )
            if respond and normalized_request_id:
                self.emit_response(normalized_request_id, ok=True, payload=result)
            self._schedule_flow_snapshot_update(sections={"app", "control"})
            return result
        if normalized_action == "run.list":
            result = self._handle_run_list(request_payload)
            if respond and normalized_request_id:
                self.emit_response(normalized_request_id, ok=True, payload=result)
            self._schedule_flow_snapshot_update(sections={"app"})
            return result
        if normalized_action == "run.resume":
            result = self._handle_resume_control(request_payload)
            if respond and normalized_request_id:
                self.emit_response(normalized_request_id, ok=True, payload=result)
            self._schedule_flow_snapshot_update(sections={"app", "control"})
            return result
        raise ValueError(f"不支持的 action: {normalized_action}")

    def handle_action(self, action: str, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        return self.dispatch_action(action, payload, respond=False)

    @staticmethod
    def _argv_from_payload(payload: Mapping[str, Any]) -> list[str]:
        argv = payload.get("argv", [])
        if isinstance(argv, list):
            return [str(item) for item in argv]
        raise ValueError("payload.argv 必须是数组")

    @staticmethod
    def _argv_has_option(argv: Sequence[str], option: str) -> bool:
        prefix = f"{option}="
        return any(str(item).strip() == option or str(item).strip().startswith(prefix) for item in argv)

    @classmethod
    def _argv_with_default_a08_review_max_rounds(cls, argv: Sequence[str]) -> list[str]:
        normalized = [str(item) for item in argv]
        if cls._argv_has_option(normalized, "--review-max-rounds"):
            return normalized
        return [*normalized, "--review-max-rounds", str(MAX_OVERALL_REVIEW_ROUNDS)]

    @staticmethod
    def _argv_with_payload_agent_config(argv: Sequence[str], payload: Mapping[str, Any]) -> list[str]:
        agent_config = payload.get("agent_config")
        if agent_config is None:
            return list(argv)
        if not isinstance(agent_config, Mapping):
            raise ValueError("payload.agent_config 必须是对象")
        root = Path(tempfile.gettempdir()) / "tmux-web-agent-config"
        root.mkdir(parents=True, exist_ok=True)
        path = root / f"agent-config-{dt.datetime.now().strftime('%Y%m%d%H%M%S%f')}.json"
        path.write_text(json.dumps(dict(agent_config), ensure_ascii=False, indent=2), encoding="utf-8")
        return [*list(argv), "--agent-config", str(path)]

    def handle_request(self, request: Mapping[str, Any]) -> None:
        request_id = str(request.get("id", "")).strip()
        action = str(request.get("action", "")).strip()
        payload = request.get("payload", {})
        if not request_id:
            raise ValueError("request.id 不能为空")
        if not isinstance(payload, dict):
            raise ValueError("request.payload 必须是对象")
        self.dispatch_action(action, payload, request_id=request_id, respond=True)

    def serve_forever(self) -> int:
        for raw_line in self.reader:
            text = str(raw_line).strip()
            if not text:
                continue
            request_id = ""
            try:
                request = decode_message(text)
                if request.get("kind") != "request":
                    raise ValueError("stdio backend 仅接收 request 消息")
                request_id = str(request.get("id", "") or "").strip()
                self.handle_request(request)
            except Exception as error:  # noqa: BLE001
                if request_id:
                    try:
                        self.emit_response(request_id, ok=False, error=str(error))
                    except Exception:
                        pass
                self.write_message(
                    build_event(
                        "error",
                        {
                            "message": str(error),
                            "traceback": traceback.format_exc(),
                        },
                    )
                )
        return 0


class TuiBackendServer(BridgeCore):
    def __init__(self, *, reader: TextIO | None = None, writer: TextIO | None = None) -> None:
        clear_runtime_shutdown_request()
        super().__init__()
        self.reader = reader or sys.stdin
        self.writer = writer or sys.stdout
        self._write_lock = threading.Lock()
        self.attach_adapter("tui")
        self.subscribe_events(self.write_message)
        self.set_response_emitter(self.write_message)

    def write_message(self, payload: Mapping[str, Any]) -> None:
        line = encode_message(payload)
        with self._write_lock:
            self.writer.write(line)
            self.writer.flush()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="OpenTUI Python stdio backend")
    parser.add_argument("--cleanup-project-dir", default="", help="仅清理指定项目目录下的 tmux 会话")
    parser.add_argument("--cleanup-requirement-name", default="", help="cleanup-only 模式下的需求名称")
    parser.add_argument("--cleanup-action", default="", help="cleanup-only 模式下的当前 action")
    return parser


def _run_cleanup_only_backend(args: argparse.Namespace) -> int:
    project_dir = str(getattr(args, "cleanup_project_dir", "") or "").strip()
    if not project_dir:
        raise ValueError("cleanup-only 模式要求传入 --cleanup-project-dir")
    server = TuiBackendServer()
    server._set_context(  # noqa: SLF001
        project_dir=project_dir,
        requirement_name=str(getattr(args, "cleanup_requirement_name", "") or "").strip(),
        action=str(getattr(args, "cleanup_action", "") or "").strip(),
    )
    server.shutdown(cleanup_tmux=True)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if str(getattr(args, "cleanup_project_dir", "") or "").strip():
        return _run_cleanup_only_backend(args)
    server = TuiBackendServer()

    def _handle_signal(signum: int, _frame: Any) -> None:
        if signum == signal.SIGINT:
            raise KeyboardInterrupt
        raise SystemExit(128 + int(signum))

    handled_signals = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
    previous_handlers = {item: signal.getsignal(item) for item in handled_signals}
    for item in handled_signals:
        signal.signal(item, _handle_signal)
    try:
        sys.stdout = server.protocol_log_sink()
        return server.serve_forever()
    except KeyboardInterrupt:
        return 130
    finally:
        for item, previous_handler in previous_handlers.items():
            signal.signal(item, previous_handler)
        server.shutdown(cleanup_tmux=True)


if __name__ == "__main__":
    raise SystemExit(main())
