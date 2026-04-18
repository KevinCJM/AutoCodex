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
import queue
import signal
import sys
import threading
import traceback
from dataclasses import asdict, dataclass, is_dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence, TextIO

from A00_main import build_parser as build_a00_parser, main as a00_main
from A01_Routing_LayerPlanning import (
    build_parser as build_a01_parser,
    format_batch_summary,
    prepare_batch_request,
    prompt_confirmation,
    render_noop_summary,
    render_preflight_summary,
    render_requirements_stage_placeholder,
)
from A01_Routing_LayerPlanning import run_routing_stage
from A02_RequirementIntake import (
    NOTION_RUNTIME_ROOT_NAME,
    build_notion_hitl_paths,
    build_parser as build_a02_parser,
    run_requirement_intake_stage,
)
from A03_RequirementsClarification import (
    REQUIREMENTS_RUNTIME_ROOT_NAME,
    build_parser as build_a03_parser,
    run_requirements_clarification_stage,
)
from A04_RequirementsReview import (
    REQUIREMENTS_REVIEW_RUNTIME_ROOT_NAME,
    build_parser as build_a04_parser,
    build_requirements_review_paths,
    run_requirements_review_stage,
)
from B01_terminal_interaction import (
    AgentInitControlCenter,
    collect_b01_request,
    render_control_help,
)
from T02_tmux_agents import (
    DEFAULT_RUNTIME_ROOT,
    TmuxBatchWorker,
    TmuxRuntimeController,
    cleanup_registered_tmux_workers,
)
from T03_agent_init_workflow import RunStore, required_routing_layer_paths
from T08_pre_development import (
    build_pre_development_task_record_path,
    load_pre_development_task_record,
)
from T09_terminal_ops import BridgePromptRequest, BridgeTerminalUI, use_terminal_ui
from T12_requirements_common import (
    build_requirements_clarification_paths,
    list_existing_requirements,
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
        on_prompt_open: Callable[[str, BridgePromptRequest], None] | None = None,
        on_prompt_resolved: Callable[[str, Mapping[str, Any] | None], None] | None = None,
    ) -> None:
        self._emit_event = emit_event
        self._on_prompt_open = on_prompt_open
        self._on_prompt_resolved = on_prompt_resolved
        self._pending: dict[str, queue.Queue[dict[str, Any]]] = {}
        self._lock = threading.Lock()

    def request(self, request: BridgePromptRequest) -> dict[str, Any]:
        prompt_id = f"prompt_{threading.get_ident()}_{len(self._pending) + 1}"
        prompt_queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
        with self._lock:
            self._pending[prompt_id] = prompt_queue
        self._emit_event(
            "prompt.request",
            {
                "id": prompt_id,
                "prompt_type": request.prompt_type,
                **request.payload,
            },
        )
        if self._on_prompt_open is not None:
            self._on_prompt_open(prompt_id, request)
        try:
            return prompt_queue.get()
        finally:
            with self._lock:
                self._pending.pop(prompt_id, None)

    def resolve(self, prompt_id: str, payload: Mapping[str, Any] | None = None) -> None:
        with self._lock:
            prompt_queue = self._pending.get(str(prompt_id).strip())
        if prompt_queue is None:
            raise KeyError(f"未找到待处理 prompt: {prompt_id}")
        if self._on_prompt_resolved is not None:
            self._on_prompt_resolved(str(prompt_id).strip(), payload)
        try:
            prompt_queue.put_nowait(dict(payload or {}))
        except queue.Full:
            return


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


STAGE_LABEL_BY_ACTION = {
    "control.b01.open": "路由初始化",
    "stage.a01.start": "路由初始化",
    "stage.a02.start": "需求录入",
    "stage.a03.start": "需求澄清",
    "stage.a04.start": "需求评审",
}

LEGACY_REQUIREMENTS_RUNTIME_ROOT_NAME = ".requirements_analysis_runtime"


class ProtocolLogSink:
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
    return dt.datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat(timespec="seconds")


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


_WORKER_PHASE_VALUES = {
    "shell",
    "booting",
    "auth_prompt",
    "update_prompt",
    "waiting_input",
    "idle_ready",
    "processing",
    "completed_response",
    "recovering",
    "error",
    "unknown",
}


def _normalize_worker_provider_phase(
    *,
    status: str,
    provider_phase: str,
    health_status: str,
    health_note: str,
    updated_at: str = "",
    last_heartbeat_at: str = "",
) -> str:
    normalized_provider_phase = str(provider_phase or "").strip() or "unknown"
    normalized_health_note = str(health_note or "").strip()
    normalized_status = str(status or "").strip()
    if normalized_health_note in _WORKER_PHASE_VALUES:
        if normalized_status in {"succeeded", "completed"}:
            return normalized_health_note
        if normalized_health_note == normalized_provider_phase:
            return normalized_provider_phase
        provider_updated_at = _parse_iso_datetime(updated_at)
        health_observed_at = _parse_iso_datetime(last_heartbeat_at)
        if health_observed_at is not None and provider_updated_at is None:
            return normalized_health_note
        if (
            health_observed_at is not None
            and provider_updated_at is not None
            and health_observed_at > provider_updated_at
        ):
            return normalized_health_note
    return normalized_provider_phase


def _parse_iso_datetime(value: str) -> dt.datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    with contextlib.suppress(ValueError):
        return dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    return None


def _read_worker_state_snapshot(
    state_path: str | Path,
    *,
    session_exists_resolver: Callable[[str], bool] | None = None,
) -> dict[str, Any]:
    state = _safe_json_read(state_path)
    if not state:
        return {}
    turn_bundle = _read_turn_bundle(str(state.get("current_turn_status_path", "")))
    task_bundle = _read_task_result_bundle(str(state.get("current_task_result_path", "")))
    artifact_paths: list[str] = []
    for collection in (turn_bundle.get("artifact_paths", []), task_bundle.get("artifact_paths", [])):
        for item in collection:
            if item and item not in artifact_paths:
                artifact_paths.append(item)
    session_name = str(state.get("session_name", "")).strip()
    session_exists = False
    if session_name and session_exists_resolver is not None:
        with contextlib.suppress(Exception):
            session_exists = bool(session_exists_resolver(session_name))
    status = str(state.get("result_status") or state.get("status") or "pending").strip()
    health_status = str(state.get("health_status", "unknown")).strip()
    health_note = str(state.get("health_note", "")).strip()
    return {
        "session_name": session_name,
        "work_dir": str(state.get("work_dir", "")).strip(),
        "status": status,
        "workflow_stage": str(state.get("workflow_stage", "pending")).strip(),
        "provider_phase": _normalize_worker_provider_phase(
            status=status,
            provider_phase=str(state.get("provider_phase", "unknown")).strip(),
            health_status=health_status,
            health_note=health_note,
            updated_at=str(state.get("updated_at", "")).strip(),
            last_heartbeat_at=str(state.get("last_heartbeat_at", "")).strip(),
        ),
        "health_status": health_status,
        "health_note": health_note,
        "retry_count": int(state.get("retry_count", 0) or 0),
        "note": str(state.get("note", "")).strip(),
        "transcript_path": str(state.get("transcript_path", "")).strip(),
        "turn_status_path": str(state.get("current_turn_status_path", "")).strip(),
        "question_path": str(turn_bundle.get("question_path") or "").strip(),
        "answer_path": str(turn_bundle.get("answer_path") or "").strip(),
        "artifact_paths": artifact_paths,
        "session_exists": session_exists,
        "last_heartbeat_at": str(state.get("last_heartbeat_at", "")).strip(),
        "updated_at": str(state.get("updated_at", "")).strip(),
    }


class TuiBackendServer:
    def __init__(self, *, reader: TextIO | None = None, writer: TextIO | None = None) -> None:
        self.reader = reader or sys.stdin
        self.writer = writer or sys.stdout
        self._write_lock = threading.Lock()
        self._pending_prompt: PendingPromptState | None = None
        self._prompt_broker = PromptBroker(
            self.emit_event,
            on_prompt_open=self._handle_prompt_open,
            on_prompt_resolved=self._handle_prompt_resolved,
        )
        self._protocol_log_sink = ProtocolLogSink(self.emit_event)
        self._bridge_ui = BridgeTerminalUI(
            emit_event=self.emit_event,
            request_prompt=self._prompt_broker.request,
            state_change_notifier=self._handle_runtime_state_change,
            stage_change_notifier=self._handle_runtime_stage_change,
        )
        self._workers: dict[str, threading.Thread] = {}
        self._controls: dict[str, ControlSessionState] = {}
        self._controls_lock = threading.Lock()
        self._shutdown_lock = threading.Lock()
        self._shutdown_started = False
        self._context = AppContext()
        self._display_status = "ready"
        self._display_action = ""
        self._active_control_id = ""
        self._tmux_runtime = TmuxRuntimeController()

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
        lines = [
            "",
            f"HITL 问题文档: {question_file}",
            question_text,
            "",
        ]
        self.emit_event("log.append", {"text": "\n".join(lines)})

    def _handle_prompt_open(self, prompt_id: str, request: BridgePromptRequest) -> None:
        self._pending_prompt = PendingPromptState(
            prompt_id=str(prompt_id).strip(),
            prompt_type=str(request.prompt_type or "").strip(),
            payload=dict(request.payload),
        )
        self._emit_hitl_prompt_log(request)
        self._emit_all_snapshots()

    def _handle_prompt_resolved(self, prompt_id: str, payload: Mapping[str, Any] | None = None) -> None:
        current = self._pending_prompt
        if current is not None and current.prompt_id == str(prompt_id).strip():
            self._update_context_from_prompt_response(current, payload or {})
            self._pending_prompt = None

    def _handle_runtime_stage_change(self, action: str) -> None:
        normalized = str(action or "").strip()
        if not normalized:
            return
        self._set_context(action=normalized)
        self._emit_display_stage_state(
            preferred_status="running",
            preferred_action=normalized,
            force=True,
        )
        self._emit_all_snapshots()

    def _handle_runtime_state_change(self) -> None:
        self._emit_display_stage_state()
        self._emit_all_snapshots()

    @staticmethod
    def _prompt_marker_text(prompt: PendingPromptState) -> str:
        return " ".join(
            [
                prompt.prompt_type,
                str(prompt.payload.get("title", "")),
                str(prompt.payload.get("prompt_text", "")),
            ]
        ).strip()

    def _update_context_from_prompt_response(
        self,
        prompt: PendingPromptState,
        payload: Mapping[str, Any],
    ) -> None:
        marker = self._prompt_marker_text(prompt)
        value = str(payload.get("value", "")).strip()
        if not marker:
            return
        if "项目工作目录" in marker and value:
            self._set_context(project_dir=value)
            return
        if "需求名称" in marker and value:
            self._set_context(requirement_name=value)
            return
        if "选择已有需求或创建新需求" in marker and value and value != "__create_new__":
            self._set_context(requirement_name=value)

    def _resolve_project_dir(self, *, runs: Sequence[Mapping[str, Any]] | None = None) -> str:
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

    def _latest_run_store(self, *, project_dir: str = "") -> RunStore | None:
        runtime_root = Path(DEFAULT_RUNTIME_ROOT).expanduser().resolve()
        if not runtime_root.exists() or not runtime_root.is_dir():
            return None
        normalized_project_dir = str(Path(project_dir).expanduser().resolve()) if project_dir else ""
        manifest_paths = sorted(runtime_root.glob("run_*/manifest.json"), key=lambda item: item.stat().st_mtime, reverse=True)
        for manifest_path in manifest_paths:
            try:
                store = RunStore.load(run_id=manifest_path.parent.name, runtime_root=runtime_root)
            except Exception:
                continue
            manifest_project_dir = str(store.manifest.project_dir or "").strip()
            if normalized_project_dir and manifest_project_dir:
                try:
                    if str(Path(manifest_project_dir).expanduser().resolve()) != normalized_project_dir:
                        continue
                except Exception:
                    continue
            return store
        return None

    def _manifest_worker_snapshot(self, entry: Any) -> dict[str, Any]:
        state_path = str(getattr(entry, "state_path", "") or "").strip()
        snapshot: dict[str, Any] = {}
        if state_path:
            snapshot = _read_worker_state_snapshot(
                state_path,
                session_exists_resolver=self._tmux_runtime.session_exists,
            )
        session_name = str(snapshot.get("session_name") or getattr(entry, "session_name", "") or "").strip()
        session_exists = bool(snapshot.get("session_exists")) if session_name else False
        if session_name and not session_exists:
            with contextlib.suppress(Exception):
                session_exists = bool(self._tmux_runtime.session_exists(session_name))
        status = str(snapshot.get("status") or getattr(entry, "result_status", "") or "pending").strip()
        health_status = str(snapshot.get("health_status") or getattr(entry, "health_status", "") or "unknown").strip()
        health_note = str(snapshot.get("health_note") or getattr(entry, "health_note", "") or "").strip()
        return {
            "session_name": session_name,
            "work_dir": str(snapshot.get("work_dir") or getattr(entry, "work_dir", "") or "").strip(),
            "status": status,
            "workflow_stage": str(snapshot.get("workflow_stage") or getattr(entry, "workflow_stage", "") or "pending").strip(),
            "provider_phase": _normalize_worker_provider_phase(
                status=status,
                provider_phase=str(snapshot.get("provider_phase") or getattr(entry, "provider_phase", "") or "unknown").strip(),
                health_status=health_status,
                health_note=health_note,
                updated_at=str(snapshot.get("updated_at") or "").strip(),
                last_heartbeat_at=str(snapshot.get("last_heartbeat_at") or "").strip(),
            ),
            "health_status": health_status,
            "health_note": health_note,
            "retry_count": int(snapshot.get("retry_count") or getattr(entry, "retry_count", 0) or 0),
            "note": str(snapshot.get("note") or getattr(entry, "note", "") or "").strip(),
            "transcript_path": str(snapshot.get("transcript_path") or getattr(entry, "transcript_path", "") or "").strip(),
            "turn_status_path": str(snapshot.get("turn_status_path", "") or "").strip(),
            "question_path": str(snapshot.get("question_path", "") or "").strip(),
            "answer_path": str(snapshot.get("answer_path", "") or "").strip(),
            "artifact_paths": list(snapshot.get("artifact_paths", [])),
            "session_exists": session_exists,
            "updated_at": str(snapshot.get("updated_at") or "").strip(),
        }

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
            review_paths = build_requirements_review_paths(project_dir, requirement_name)
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
        return "任务开发"

    def _resolve_stage_label(self, *, action: str, project_dir: str, requirement_name: str) -> str:
        normalized_action = str(action or "").strip()
        if not normalized_action or normalized_action == "idle":
            return "等待中"
        if normalized_action == "workflow.a00.start":
            return self._infer_workflow_a00_stage_label(project_dir, requirement_name)
        return STAGE_LABEL_BY_ACTION.get(normalized_action, "等待中")

    def write_message(self, payload: Mapping[str, Any]) -> None:
        line = encode_message(payload)
        with self._write_lock:
            self.writer.write(line)
            self.writer.flush()

    def emit_event(self, event_type: str, payload: Mapping[str, Any] | None = None) -> None:
        self.write_message(build_event(event_type, payload))

    def emit_response(
        self,
        request_id: str,
        *,
        ok: bool,
        payload: Mapping[str, Any] | None = None,
        error: str = "",
    ) -> None:
        self.write_message(build_response(request_id, ok=ok, payload=payload, error=error))

    def protocol_log_sink(self) -> ProtocolLogSink:
        return self._protocol_log_sink

    def _set_context(
        self,
        *,
        project_dir: str | None = None,
        requirement_name: str | None = None,
        action: str | None = None,
    ) -> None:
        if project_dir is not None and str(project_dir).strip():
            self._context.project_dir = str(Path(project_dir).expanduser().resolve())
        if requirement_name is not None and str(requirement_name).strip():
            self._context.requirement_name = str(requirement_name).strip()
        if action is not None and str(action).strip():
            self._context.current_action = str(action).strip()

    def _update_context_from_stage_args(self, action: str, argv: Sequence[str]) -> None:
        try:
            if action == "workflow.a00.start":
                args = build_a00_parser().parse_args(list(argv))
            elif action == "stage.a01.start":
                args = build_a01_parser().parse_args(list(argv))
            elif action == "stage.a02.start":
                args = build_a02_parser().parse_args(list(argv))
            elif action == "stage.a03.start":
                args = build_a03_parser().parse_args(list(argv))
            elif action == "stage.a04.start":
                args = build_a04_parser().parse_args(list(argv))
            else:
                return
        except Exception:
            return
        self._set_context(
            project_dir=str(getattr(args, "project_dir", "") or "").strip() or None,
            requirement_name=str(getattr(args, "requirement_name", "") or "").strip() or None,
            action=action,
        )

    def _update_context_from_result(self, result: Any, *, action: str) -> None:
        resolved_action = action
        if action == "workflow.a00.start":
            current_action = str(self._context.current_action or "").strip()
            if current_action and current_action != action:
                resolved_action = current_action
        if isinstance(result, Mapping):
            self._set_context(
                project_dir=str(result.get("project_dir", "")).strip() or None,
                requirement_name=str(result.get("requirement_name", "")).strip() or None,
                action=resolved_action,
            )
            return
        self._set_context(
            project_dir=str(getattr(result, "project_dir", "") or "").strip() or None,
            requirement_name=str(getattr(result, "requirement_name", "") or "").strip() or None,
            action=resolved_action,
        )

    def _scan_runtime_workers(self, runtime_root: str | Path) -> list[dict[str, Any]]:
        root = Path(runtime_root).expanduser().resolve()
        if not root.exists() or not root.is_dir():
            return []
        workers: list[dict[str, Any]] = []
        for state_path in sorted(root.glob("*/worker.state.json")):
            snapshot = _read_worker_state_snapshot(
                state_path,
                session_exists_resolver=self._tmux_runtime.session_exists,
            )
            if snapshot:
                workers.append(snapshot)
        return workers

    def _list_runs(self) -> list[dict[str, Any]]:
        root = Path(DEFAULT_RUNTIME_ROOT).expanduser().resolve()
        if not root.exists() or not root.is_dir():
            return []
        options: list[dict[str, Any]] = []
        for manifest_path in sorted(root.glob("run_*/manifest.json"), key=lambda item: item.stat().st_mtime, reverse=True):
            try:
                store = RunStore.load(run_id=manifest_path.parent.name, runtime_root=root)
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
        center.refresh_worker_health()
        if center.all_done() and session.final_result is None:
            session.final_result = center.wait_until_complete()
            session.transition_text = center.transition_to_requirements_phase(session.final_result)
        build_workers = getattr(center, "build_worker_snapshots", None)
        worker_snapshots = build_workers() if callable(build_workers) else _serialize(getattr(center, "build_status_rows")())
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
        if not workers:
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
        requirement_name = str(self._context.requirement_name or "").strip()
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
            for collection in (
                self._scan_runtime_workers(Path(project_dir) / NOTION_RUNTIME_ROOT_NAME),
                self._scan_runtime_workers(Path(project_dir) / REQUIREMENTS_RUNTIME_ROOT_NAME),
                self._scan_runtime_workers(Path(project_dir) / LEGACY_REQUIREMENTS_RUNTIME_ROOT_NAME),
            ):
                for worker in collection:
                    session_name = str(worker.get("session_name", "")).strip()
                    if not session_name or all(str(item.get("session_name", "")).strip() != session_name for item in workers):
                        workers.append(worker)
        return {
            "project_dir": project_dir,
            "requirement_name": requirement_name,
            "files": files,
            "workers": workers,
        }

    def _build_review_snapshot(self) -> dict[str, Any]:
        project_dir = self._resolve_project_dir()
        requirement_name = str(self._context.requirement_name or "").strip()
        files: list[dict[str, Any]] = []
        workers: list[dict[str, Any]] = []
        blockers: list[str] = []
        if project_dir and requirement_name:
            try:
                paths = build_requirements_review_paths(project_dir, requirement_name)
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
            workers = self._scan_runtime_workers(Path(project_dir) / REQUIREMENTS_REVIEW_RUNTIME_ROOT_NAME)
        return {
            "project_dir": project_dir,
            "requirement_name": requirement_name,
            "files": files,
            "workers": workers,
            "blockers": blockers,
        }

    def _build_pending_prompt_hitl_snapshot(self) -> dict[str, Any]:
        pending = self._pending_prompt
        if pending is None:
            return {"pending": False, "question_path": "", "answer_path": "", "summary": ""}
        combined = " ".join(
            [
                pending.prompt_type,
                str(pending.payload.get("title", "")),
                str(pending.payload.get("prompt_text", "")),
            ]
        ).strip()
        if "hitl" not in combined.lower():
            return {"pending": False, "question_path": "", "answer_path": "", "summary": ""}
        title = str(pending.payload.get("title", "")).strip()
        prompt_text = str(pending.payload.get("prompt_text", "")).strip()
        summary = title or prompt_text or "存在待处理 HITL"
        return {
            "pending": True,
            "question_path": str(pending.payload.get("question_path", "") or "").strip(),
            "answer_path": str(pending.payload.get("answer_path", "") or "").strip(),
            "summary": summary,
        }

    def _build_hitl_snapshot(self) -> dict[str, Any]:
        prompt_snapshot = self._build_pending_prompt_hitl_snapshot()
        if prompt_snapshot.get("pending", False):
            return prompt_snapshot
        project_dir = self._resolve_project_dir()
        requirement_name = str(self._context.requirement_name or "").strip()
        if not project_dir or not requirement_name:
            return {"pending": False, "question_path": "", "answer_path": "", "summary": ""}
        try:
            _, notion_question_path, notion_record_path = build_notion_hitl_paths(project_dir, requirement_name)
            _, _, ask_human_path, hitl_record_path = build_requirements_clarification_paths(project_dir, requirement_name)
        except Exception:
            return {"pending": False, "question_path": "", "answer_path": "", "summary": ""}
        active_question = ask_human_path if ask_human_path.exists() and _preview_text(ask_human_path) else ""
        if not active_question:
            active_question = notion_question_path if notion_question_path.exists() and _preview_text(notion_question_path) else ""
        answer_path = hitl_record_path if hitl_record_path.exists() else notion_record_path
        return {
            "pending": bool(active_question),
            "question_path": str(active_question) if active_question else "",
            "answer_path": str(answer_path) if answer_path and Path(answer_path).exists() else "",
            "summary": _preview_text(active_question) if active_question else "",
        }

    def _build_artifacts_snapshot(self) -> dict[str, Any]:
        candidates: list[str] = []
        routing = self._build_routing_snapshot()
        requirements = self._build_requirements_snapshot()
        review = self._build_review_snapshot()
        control = self._build_control_snapshot_for_session(self._current_control_session())
        for collection in (
            [item.get("path", "") for item in routing.get("files", [])],
            [item.get("path", "") for item in requirements.get("files", [])],
            [item.get("path", "") for item in review.get("files", [])],
            [artifact for worker in control.get("workers", []) for artifact in worker.get("artifact_paths", [])],
        ):
            for item in collection:
                text = str(item).strip()
                if text and Path(text).exists() and text not in candidates:
                    candidates.append(text)
        items = [
            {
                "path": str(Path(item).expanduser().resolve()),
                "updated_at": _iso_from_path(item),
                "summary": _preview_text(item),
            }
            for item in sorted(candidates, key=lambda candidate: Path(candidate).stat().st_mtime, reverse=True)[:12]
        ]
        return {"items": items}

    def _build_app_snapshot(self) -> dict[str, Any]:
        runs = self._list_runs()
        control = self._build_control_snapshot_for_session(self._current_control_session())
        hitl = self._build_hitl_snapshot()
        artifacts = self._build_artifacts_snapshot()
        project_dir = self._resolve_project_dir(runs=runs)
        requirement_name = str(self._context.requirement_name or "").strip()
        active_stage = self._context.current_action or "idle"
        return {
            "project_dir": project_dir,
            "requirement_name": requirement_name,
            "current_action": self._context.current_action,
            "active_run_id": str(control.get("run_id", "")).strip() or (runs[0]["run_id"] if runs else ""),
            "active_stage": active_stage,
            "active_stage_label": self._resolve_stage_label(
                action=active_stage,
                project_dir=project_dir,
                requirement_name=requirement_name,
            ),
            "pending_hitl": bool(hitl.get("pending", False)),
            "recent_artifacts": artifacts.get("items", [])[:5],
            "available_runs": runs[:5],
            "capabilities": {
                "structured_snapshots": True,
                "control_actions": ["attach", "detach", "restart", "retry", "kill", "resume"],
                "local_prompt_history": True,
                "collapsible_logs": True,
            },
        }

    def _derive_display_stage_state(
        self,
        *,
        preferred_status: str | None = None,
        preferred_action: str | None = None,
    ) -> tuple[str, str]:
        action = str(preferred_action or self._context.current_action or self._display_action or "").strip()
        explicit_status = str(preferred_status or self._display_status or "ready").strip() or "ready"
        if explicit_status in {"failed", "error"}:
            return action, explicit_status
        if self._pending_prompt is not None:
            return action, "awaiting-input"
        if bool(self._build_hitl_snapshot().get("pending", False)):
            return action, "awaiting-input"
        return action, explicit_status

    def _emit_display_stage_state(
        self,
        *,
        preferred_status: str | None = None,
        preferred_action: str | None = None,
        force: bool = False,
    ) -> None:
        previous_action = self._display_action
        previous_status = self._display_status
        action, status = self._derive_display_stage_state(
            preferred_status=preferred_status,
            preferred_action=preferred_action,
        )
        if not action and status in {"ready", "booting"} and not force:
            self._display_action = action
            self._display_status = status
            return
        if not force and action == previous_action and status == previous_status:
            return
        self._display_action = action
        self._display_status = status
        self.emit_event("stage.changed", {"action": action or "idle", "status": status})

    def _emit_all_snapshots(self) -> None:
        self.emit_event("snapshot.app", self._build_app_snapshot())
        self.emit_event("snapshot.stage", {"route": "routing", "snapshot": self._build_routing_snapshot()})
        self.emit_event("snapshot.stage", {"route": "requirements", "snapshot": self._build_requirements_snapshot()})
        self.emit_event("snapshot.stage", {"route": "review", "snapshot": self._build_review_snapshot()})
        self.emit_event("snapshot.control", self._build_control_snapshot_for_session(self._current_control_session()))
        self.emit_event("snapshot.hitl", self._build_hitl_snapshot())
        self.emit_event("snapshot.artifacts", self._build_artifacts_snapshot())

    @staticmethod
    def _result_exit_code(result: Any) -> int:
        if isinstance(result, Mapping):
            try:
                return int(result.get("exit_code", 0))
            except Exception:
                return 0
        try:
            return int(getattr(result, "exit_code", 0))
        except Exception:
            return 0

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
            project_dir = str(self._context.project_dir or "").strip()
        if not project_dir:
            return []
        argv = ["--project-dir", project_dir]
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
        self._update_context_from_stage_args("stage.a02.start", followup_argv)
        self._run_in_thread(
            "",
            "stage.a02.start",
            lambda: run_requirement_intake_stage(followup_argv),
            argv=followup_argv,
            respond=False,
        )

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

        def target() -> None:
            try:
                with use_terminal_ui(self._bridge_ui), contextlib.redirect_stdout(self._protocol_log_sink):
                    self._emit_display_stage_state(
                        preferred_status="running",
                        preferred_action=action,
                        force=True,
                    )
                    result = runner()
                self._update_context_from_result(result, action=action)
                if respond and request_id:
                    self.emit_response(request_id, ok=True, payload={"result": _serialize(result)})
                self._emit_display_stage_state(preferred_status="completed", force=True)
                self._emit_all_snapshots()
                if argv is not None:
                    self._maybe_chain_after_stage_success(action=action, argv=argv, result=result)
            except Exception as error:  # noqa: BLE001
                if respond and request_id:
                    self.emit_response(
                        request_id,
                        ok=False,
                        error=str(error),
                        payload={"traceback": traceback.format_exc()},
                    )
                self.emit_event(
                    "error",
                    {"action": action, "message": str(error), "traceback": traceback.format_exc()},
                )
                self._emit_display_stage_state(
                    preferred_status="failed",
                    preferred_action=action,
                    force=True,
                )
                self._emit_all_snapshots()
            finally:
                self._workers.pop(worker_key, None)

        thread = threading.Thread(target=target, name=f"tui-backend-{action}-{request_id}", daemon=True)
        self._workers[worker_key] = thread
        thread.start()

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

    def shutdown(self, *, cleanup_tmux: bool) -> list[str]:
        with self._shutdown_lock:
            if self._shutdown_started:
                return []
            self._shutdown_started = True
        with self._controls_lock:
            sessions = list(self._controls.values())
            self._controls.clear()
        for session in sessions:
            try:
                session.center.close()
            except Exception:
                continue
        cleaned_sessions: list[str] = []
        if cleanup_tmux:
            cleaned_sessions = cleanup_registered_tmux_workers(reason="tui_backend_shutdown")
            if cleaned_sessions:
                self.emit_event(
                    "log.append",
                    {"text": f"已清理 tmux 会话: {', '.join(cleaned_sessions)}\n"},
                )
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
            center = AgentInitControlCenter.from_existing_run(
                run_id=str(args.resume_run).strip(),
                max_refine_rounds=max(int(args.max_refine_rounds or 3), 1),
            )
            center.start()
            session = ControlSessionState(control_id=center.run_id, center=center)
            self._set_control_session(session)
            return self._snapshot_control_session(session)

        request = collect_b01_request(args)
        config, selection = prepare_batch_request(request)
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
                "final_summary": render_noop_summary(request, config, selection),
                "transition_text": render_requirements_stage_placeholder(()),
            }

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
        center = AgentInitControlCenter.from_existing_run(run_id=run_id)
        center.start()
        session = ControlSessionState(control_id=center.run_id, center=center)
        self._set_control_session(session)
        return self._snapshot_control_session(session)

    def _handle_run_list(self) -> dict[str, Any]:
        return {"runs": self._list_runs()}

    def _handle_bootstrap(self, request_id: str) -> None:
        self.emit_response(
            request_id,
            ok=True,
            payload={
                "protocol_version": PROTOCOL_VERSION,
                "python_path": SYSTEM_PYTHON_PATH,
                "routes": ["home", "routing", "requirements", "review", "control"],
                "commands": [
                    "app.bootstrap",
                    "workflow.a00.start",
                    "stage.a01.start",
                    "stage.a02.start",
                    "stage.a03.start",
                    "stage.a04.start",
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
                    "bridge_only_terminal_ui": True,
                },
                "snapshots": {
                    "app": self._build_app_snapshot(),
                    "stages": {
                        "routing": self._build_routing_snapshot(),
                        "requirements": self._build_requirements_snapshot(),
                        "review": self._build_review_snapshot(),
                    },
                    "control": self._build_control_snapshot_for_session(self._current_control_session()),
                    "hitl": self._build_hitl_snapshot(),
                    "artifacts": self._build_artifacts_snapshot(),
                },
            },
        )
        self._emit_display_stage_state()
        self._emit_all_snapshots()

    @staticmethod
    def _argv_from_payload(payload: Mapping[str, Any]) -> list[str]:
        argv = payload.get("argv", [])
        if isinstance(argv, list):
            return [str(item) for item in argv]
        raise ValueError("payload.argv 必须是数组")

    def handle_request(self, request: Mapping[str, Any]) -> None:
        request_id = str(request.get("id", "")).strip()
        action = str(request.get("action", "")).strip()
        payload = request.get("payload", {})
        if not request_id:
            raise ValueError("request.id 不能为空")
        if not isinstance(payload, dict):
            raise ValueError("request.payload 必须是对象")

        if action == "app.bootstrap":
            self._handle_bootstrap(request_id)
            return
        if action == "prompt.response":
            prompt_id = str(payload.get("prompt_id", "")).strip()
            if not prompt_id:
                raise ValueError("prompt.response 缺少 prompt_id")
            self._prompt_broker.resolve(prompt_id, payload)
            self.emit_response(request_id, ok=True, payload={"accepted": True})
            self._emit_all_snapshots()
            return
        if action == "workflow.a00.start":
            argv = self._argv_from_payload(payload)
            self._update_context_from_stage_args(action, argv)
            self._run_in_thread(request_id, action, lambda: a00_main(argv))
            return
        if action == "stage.a01.start":
            argv = self._argv_from_payload(payload)
            self._update_context_from_stage_args(action, argv)
            self._run_in_thread(request_id, action, lambda: run_routing_stage(argv), argv=argv)
            return
        if action == "stage.a02.start":
            argv = self._argv_from_payload(payload)
            self._update_context_from_stage_args(action, argv)
            self._run_in_thread(request_id, action, lambda: run_requirement_intake_stage(argv))
            return
        if action == "stage.a03.start":
            argv = self._argv_from_payload(payload)
            self._update_context_from_stage_args(action, argv)
            preserve = bool(payload.get("preserve_ba_worker", False))
            self._run_in_thread(request_id, action, lambda: run_requirements_clarification_stage(argv, preserve_ba_worker=preserve))
            return
        if action == "stage.a04.start":
            argv = self._argv_from_payload(payload)
            self._update_context_from_stage_args(action, argv)
            self._run_in_thread(request_id, action, lambda: run_requirements_review_stage(argv))
            return
        if action == "control.b01.open":
            if str(payload.get("control_id", "")).strip():
                result = self._open_control_session(payload)
                self.emit_response(request_id, ok=True, payload=result)
                self._emit_all_snapshots()
            else:
                self._run_in_thread(request_id, action, lambda: self._open_control_session(payload))
            return
        if action == "worker.attach":
            self.emit_response(request_id, ok=True, payload=self._handle_worker_attach(payload))
            self._emit_all_snapshots()
            return
        if action == "worker.detach":
            control_id = str(payload.get("control_id", "")).strip()
            argument = str(payload.get("argument", "")).strip()
            self.emit_response(
                request_id,
                ok=True,
                payload=self._run_worker_control_action(
                    control_id=control_id,
                    argument=argument,
                    handler=lambda center, arg: center.detach(arg),
                ),
            )
            self._emit_all_snapshots()
            return
        if action == "worker.kill":
            control_id = str(payload.get("control_id", "")).strip()
            argument = str(payload.get("argument", "")).strip()
            self.emit_response(
                request_id,
                ok=True,
                payload=self._run_worker_control_action(
                    control_id=control_id,
                    argument=argument,
                    handler=lambda center, arg: center.kill_worker(arg),
                ),
            )
            self._emit_all_snapshots()
            return
        if action == "worker.restart":
            control_id = str(payload.get("control_id", "")).strip()
            argument = str(payload.get("argument", "")).strip()
            self.emit_response(
                request_id,
                ok=True,
                payload=self._run_worker_control_action(
                    control_id=control_id,
                    argument=argument,
                    handler=lambda center, arg: center.restart_worker(arg),
                    reset_done_state=True,
                ),
            )
            self._emit_all_snapshots()
            return
        if action == "worker.retry":
            control_id = str(payload.get("control_id", "")).strip()
            argument = str(payload.get("argument", "")).strip()
            self.emit_response(
                request_id,
                ok=True,
                payload=self._run_worker_control_action(
                    control_id=control_id,
                    argument=argument,
                    handler=lambda center, arg: center.retry_worker(arg),
                    reset_done_state=True,
                ),
            )
            self._emit_all_snapshots()
            return
        if action == "run.list":
            self.emit_response(request_id, ok=True, payload=self._handle_run_list())
            self._emit_all_snapshots()
            return
        if action == "run.resume":
            self.emit_response(request_id, ok=True, payload=self._handle_resume_control(payload))
            self._emit_all_snapshots()
            return
        raise ValueError(f"不支持的 action: {action}")

    def serve_forever(self) -> int:
        for raw_line in self.reader:
            text = str(raw_line).strip()
            if not text:
                continue
            try:
                request = decode_message(text)
                if request.get("kind") != "request":
                    raise ValueError("stdio backend 仅接收 request 消息")
                self.handle_request(request)
            except Exception as error:  # noqa: BLE001
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


def build_parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(description="OpenTUI Python stdio backend")


def main(argv: Sequence[str] | None = None) -> int:
    build_parser().parse_args(argv)
    server = TuiBackendServer()
    interrupted = False

    def _handle_signal(signum: int, _frame: Any) -> None:
        nonlocal interrupted
        interrupted = True
        server.shutdown(cleanup_tmux=True)
        if signum == signal.SIGINT:
            raise KeyboardInterrupt
        raise SystemExit(128 + int(signum))

    previous_sigint = signal.getsignal(signal.SIGINT)
    previous_sigterm = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)
    try:
        sys.stdout = server.protocol_log_sink()
        return server.serve_forever()
    except KeyboardInterrupt:
        interrupted = True
        return 130
    finally:
        signal.signal(signal.SIGINT, previous_sigint)
        signal.signal(signal.SIGTERM, previous_sigterm)
        server.shutdown(cleanup_tmux=True)


if __name__ == "__main__":
    raise SystemExit(main())
