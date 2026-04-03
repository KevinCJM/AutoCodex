from __future__ import annotations

import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from .backends import BackendManager
from .config import Settings
from .delivery import DeliveryService
from .models import BackendKind, CommandKind, DeliveryChunk, SessionRecord
from .parser import build_envelope, parse_messages
from .store import BridgeStore


WHITESPACE_PATTERN = re.compile(r"\s+")


def _ensure_workdir(path_text: str) -> Path:
    path = Path(path_text).expanduser().resolve()
    if not path.is_absolute():
        raise ValueError("workdir must be an absolute path")
    if not path.is_dir():
        raise ValueError(f"workdir does not exist: {path}")
    return path


def _normalize_text(text: str) -> str:
    return WHITESPACE_PATTERN.sub(" ", str(text or "")).strip()


def _normalize_reasoning_effort(value: str) -> str:
    effort = _normalize_text(value).lower()
    if effort == "max":
        return "xhigh"
    return effort


def _parsed_signature_payload(envelope: Any, parsed: Any) -> str:
    sender_key = envelope.sender_open_id or envelope.sender_user_id or envelope.sender_union_id or "-"
    if parsed.kind == CommandKind.NEW:
        payload = f"new|{parsed.backend.value}|{_normalize_text(parsed.workdir)}"
    elif parsed.kind == CommandKind.RESUME:
        payload = (
            f"resume|{parsed.backend.value}|{_normalize_text(parsed.external_session_id)}|"
            f"{_normalize_text(parsed.workdir)}"
        )
    elif parsed.kind == CommandKind.MODEL:
        payload = f"model|{_normalize_text(parsed.model_name)}"
    elif parsed.kind == CommandKind.THINK:
        payload = f"think|{_normalize_text(parsed.reasoning_effort)}"
    elif parsed.kind == CommandKind.PROMPT:
        payload = f"prompt|{_normalize_text(parsed.text)}"
    elif parsed.kind == CommandKind.UNSUPPORTED:
        payload = f"unsupported|{_normalize_text(envelope.cleaned_text)}"
    else:
        payload = f"{parsed.kind.value}|{_normalize_text(envelope.cleaned_text or parsed.text)}"
    return f"{sender_key}|{envelope.message_type}|{payload}"


def _message_signature(envelope: Any, parsed_messages: Any) -> str:
    if isinstance(parsed_messages, (list, tuple)):
        items = list(parsed_messages)
    else:
        items = [parsed_messages]
    return " || ".join(_parsed_signature_payload(envelope, item) for item in items)


def _build_runtime_context(envelope: Any, *, preferred_backend: str = "") -> dict[str, str]:
    return {
        "conversation_id": envelope.conversation_id,
        "chat_id": envelope.chat_id,
        "message_id": envelope.message_id,
        "chat_type": envelope.chat_type,
        "sender_open_id": envelope.sender_open_id,
        "sender_user_id": envelope.sender_user_id,
        "sender_union_id": envelope.sender_union_id,
        "sender_type": envelope.sender_type,
        "sender_tenant_key": envelope.sender_tenant_key,
        "cleaned_text": envelope.cleaned_text,
        "raw_text": envelope.raw_text,
        "preferred_backend": preferred_backend,
    }


class BridgeService:
    def __init__(
        self,
        *,
        settings: Settings,
        store: BridgeStore,
        backend_manager: BackendManager,
        delivery: DeliveryService,
        logger: logging.Logger,
    ):
        self.settings = settings
        self.store = store
        self.backend_manager = backend_manager
        self.delivery = delivery
        self.logger = logger
        self.executor = ThreadPoolExecutor(max_workers=settings.max_workers)
        self._running_conversations: set[str] = set()
        self._running_lock = threading.Lock()

    def shutdown(self) -> None:
        self.executor.shutdown(wait=True)
        self.store.close()

    def _resolve_session_model(self, session: SessionRecord | None) -> str:
        if session is not None and session.model_name:
            return session.model_name
        return self.settings.default_model

    def _resolve_session_reasoning(self, session: SessionRecord | None) -> str:
        if session is not None and session.reasoning_effort:
            return session.reasoning_effort
        return self.settings.default_reasoning_effort

    def _validate_model_name(self, value: str) -> str:
        model_name = _normalize_text(value)
        if not model_name:
            raise ValueError("model_id 不能为空。")
        if model_name not in self.settings.available_models:
            choices = ", ".join(f"`{item}`" for item in self.settings.available_models)
            raise ValueError(f"不支持的模型: `{model_name}`。可用模型: {choices}")
        return model_name

    def _validate_reasoning_effort(self, value: str) -> str:
        reasoning_effort = _normalize_reasoning_effort(value)
        if reasoning_effort not in self.settings.available_reasoning_efforts:
            choices = ", ".join(f"`{item}`" for item in self.settings.available_reasoning_efforts)
            raise ValueError(f"不支持的推理强度: `{value}`。可用强度: {choices}")
        return reasoning_effort

    def handle_event(self, event: Any) -> None:
        envelope = build_envelope(event, bot_open_id=self.settings.bot_open_id)
        if not envelope.conversation_id:
            self.logger.warning("ignored message without conversation_id")
            return
        if not self.store.register_processed_message(envelope.message_id, envelope.conversation_id):
            self.logger.info(
                "ignored duplicate message conversation=%s message_id=%s",
                envelope.conversation_id,
                envelope.message_id or "-",
            )
            return
        if not self.settings.sender_allowed(envelope.sender_open_id, envelope.sender_user_id):
            self.delivery.send_chunks(
                envelope.conversation_id,
                [DeliveryChunk(title="拒绝访问", text="当前发送人不在允许名单中。")],
            )
            return

        self._try_flush_pending_delivery(envelope.conversation_id)

        parsed_messages = parse_messages(envelope)
        if len(parsed_messages) == 1 and parsed_messages[0].kind == CommandKind.IGNORE:
            return
        if not self.store.register_consecutive_signature(
            envelope.conversation_id,
            _message_signature(envelope, parsed_messages),
        ):
            self.logger.info(
                "ignored consecutive duplicate conversation=%s message_id=%s text=%s",
                envelope.conversation_id,
                envelope.message_id or "-",
                _normalize_text(envelope.cleaned_text)[:120],
            )
            return
        for parsed in parsed_messages:
            self._handle_parsed_message(envelope, parsed)

    def _handle_parsed_message(self, envelope: Any, parsed: Any) -> None:
        conversation_id = envelope.conversation_id
        if parsed.kind == CommandKind.HELP:
            self.delivery.send_chunks(conversation_id, self.delivery.help_chunks())
            return
        if parsed.kind == CommandKind.STATUS:
            self.delivery.send_chunks(conversation_id, self._status_chunks(conversation_id))
            return
        if parsed.kind == CommandKind.MODEL:
            active_session = self.store.get_active_session(conversation_id)
            has_queue = self.store.count_pending_jobs(conversation_id) > 0
            if active_session is None and not has_queue:
                self.delivery.send_chunks(
                    conversation_id,
                    [DeliveryChunk(title="没有激活会话", text="请先执行 `/new ...` 或 `/resume ...`。")],
                )
                return
            try:
                model_name = self._validate_model_name(parsed.model_name)
            except ValueError as error:
                self.delivery.send_chunks(
                    conversation_id,
                    [DeliveryChunk(title="命令错误", text=str(error))],
                )
                return
            payload = {"model_name": model_name}
            queue_depth = self._enqueue_job(conversation_id, "reconfigure_session", payload)
            self.delivery.send_chunks(
                conversation_id,
                [
                    self._ack_chunk(
                        backend=active_session.backend if active_session else "pending",
                        workdir=active_session.workdir if active_session else "awaiting-session",
                        queue_depth=queue_depth,
                    )
                ],
            )
            return
        if parsed.kind == CommandKind.THINK:
            active_session = self.store.get_active_session(conversation_id)
            has_queue = self.store.count_pending_jobs(conversation_id) > 0
            if active_session is None and not has_queue:
                self.delivery.send_chunks(
                    conversation_id,
                    [DeliveryChunk(title="没有激活会话", text="请先执行 `/new ...` 或 `/resume ...`。")],
                )
                return
            try:
                reasoning_effort = self._validate_reasoning_effort(parsed.reasoning_effort)
            except ValueError as error:
                self.delivery.send_chunks(
                    conversation_id,
                    [DeliveryChunk(title="命令错误", text=str(error))],
                )
                return
            payload = {"reasoning_effort": reasoning_effort}
            queue_depth = self._enqueue_job(conversation_id, "reconfigure_session", payload)
            self.delivery.send_chunks(
                conversation_id,
                [
                    self._ack_chunk(
                        backend=active_session.backend if active_session else "pending",
                        workdir=active_session.workdir if active_session else "awaiting-session",
                        queue_depth=queue_depth,
                    )
                ],
            )
            return
        if parsed.kind == CommandKind.STOP:
            stopped = self.store.stop_active_session(conversation_id)
            if stopped is None:
                chunks = [DeliveryChunk(title="状态", text="当前没有激活会话。")]
            else:
                chunks = [
                    DeliveryChunk(
                        title="已停止",
                        text=(
                            f"已停止当前会话。\n\n"
                            f"- 后端: `{stopped.backend}`\n"
                            f"- 目录: `{stopped.workdir}`"
                        ),
                    )
                ]
            self.delivery.send_chunks(conversation_id, chunks)
            return
        if parsed.kind == CommandKind.UNSUPPORTED:
            self.delivery.send_chunks(
                conversation_id,
                [DeliveryChunk(title="命令错误", text=parsed.error or "无法解析该命令。")],
            )
            return

        if parsed.kind == CommandKind.NEW:
            try:
                workdir = str(_ensure_workdir(parsed.workdir))
            except ValueError as error:
                self.delivery.send_chunks(
                    conversation_id,
                    [DeliveryChunk(title="命令错误", text=str(error))],
                )
                return
            payload = {
                "backend": parsed.backend.value,
                "workdir": workdir,
                "model_name": self.settings.default_model,
                "reasoning_effort": self.settings.default_reasoning_effort,
                "context": _build_runtime_context(envelope, preferred_backend=parsed.backend.value),
            }
            queue_depth = self._enqueue_job(conversation_id, "new_session", payload)
            self.delivery.send_chunks(
                conversation_id,
                [self._ack_chunk(backend=parsed.backend.value, workdir=workdir, queue_depth=queue_depth)],
            )
            return

        if parsed.kind == CommandKind.RESUME:
            try:
                workdir = str(_ensure_workdir(parsed.workdir))
            except ValueError as error:
                self.delivery.send_chunks(
                    conversation_id,
                    [DeliveryChunk(title="命令错误", text=str(error))],
                )
                return
            payload = {
                "backend": parsed.backend.value,
                "workdir": workdir,
                "external_session_id": parsed.external_session_id,
                "model_name": self.settings.default_model,
                "reasoning_effort": self.settings.default_reasoning_effort,
                "context": _build_runtime_context(envelope, preferred_backend=parsed.backend.value),
            }
            queue_depth = self._enqueue_job(conversation_id, "resume_session", payload)
            self.delivery.send_chunks(
                conversation_id,
                [self._ack_chunk(backend=parsed.backend.value, workdir=workdir, queue_depth=queue_depth)],
            )
            return

        if parsed.kind == CommandKind.PROMPT:
            active_session = self.store.get_active_session(conversation_id)
            has_queue = self.store.count_pending_jobs(conversation_id) > 0
            if active_session is None and not has_queue:
                self.delivery.send_chunks(
                    conversation_id,
                    [
                        DeliveryChunk(
                            title="没有激活会话",
                            text="请先执行 `/new ...` 或 `/resume ...`。",
                        )
                    ],
                )
                return
            payload = {
                "prompt": parsed.text,
                "context": _build_runtime_context(
                    envelope,
                    preferred_backend=active_session.backend if active_session else "",
                ),
            }
            queue_depth = self._enqueue_job(conversation_id, "prompt", payload)
            self.delivery.send_chunks(
                conversation_id,
                [
                    self._ack_chunk(
                        backend=active_session.backend if active_session else "pending",
                        workdir=active_session.workdir if active_session else "awaiting-session",
                        queue_depth=queue_depth,
                    )
                ],
            )

    def _ack_chunk(self, *, backend: str, workdir: str, queue_depth: int) -> DeliveryChunk:
        if queue_depth > 1:
            text = (
                "已排队，等待当前任务完成后继续处理。\n\n"
                f"- 后端: `{backend}`\n"
                f"- 目录: `{workdir}`\n"
                f"- 队列长度: `{queue_depth}`"
            )
        else:
            text = (
                "已入队，开始处理。\n\n"
                f"- 后端: `{backend}`\n"
                f"- 目录: `{workdir}`\n"
                f"- 队列长度: `{queue_depth}`"
            )
        return DeliveryChunk(title="处理中", text=text)

    def _enqueue_job(self, conversation_id: str, job_type: str, payload: dict[str, Any]) -> int:
        self.store.enqueue_job(conversation_id=conversation_id, job_type=job_type, payload=payload)
        queue_depth = self.store.count_pending_jobs(conversation_id)
        self._ensure_conversation_worker(conversation_id)
        return queue_depth

    def _ensure_conversation_worker(self, conversation_id: str) -> None:
        with self._running_lock:
            if conversation_id in self._running_conversations:
                return
            self._running_conversations.add(conversation_id)
        self.executor.submit(self._drain_conversation, conversation_id)

    def _drain_conversation(self, conversation_id: str) -> None:
        try:
            while True:
                job = self.store.claim_next_job(conversation_id)
                if job is None:
                    return
                try:
                    self._process_job(job)
                    self.store.complete_job(job.id, summary="ok")
                except Exception as error:
                    self.logger.exception("job %s failed", job.id)
                    self.store.fail_job(job.id, str(error))
                    self.delivery.send_chunks(
                        conversation_id,
                        self.delivery.split_text("处理失败", str(error)),
                    )
                    self._store_error_for_later(conversation_id, str(error))
        finally:
            with self._running_lock:
                self._running_conversations.discard(conversation_id)
            if self.store.count_pending_jobs(conversation_id):
                self._ensure_conversation_worker(conversation_id)

    def _process_job(self, job: Any) -> None:
        if job.job_type == "new_session":
            self._handle_new_session(job.conversation_id, job.payload)
            return
        if job.job_type == "resume_session":
            self._handle_resume_session(job.conversation_id, job.payload)
            return
        if job.job_type == "reconfigure_session":
            self._handle_reconfigure_session(job.conversation_id, job.payload)
            return
        if job.job_type == "prompt":
            self._handle_prompt(job.conversation_id, job.payload)
            return
        raise RuntimeError(f"unknown job type: {job.job_type}")

    def _handle_new_session(self, conversation_id: str, payload: dict[str, Any]) -> None:
        backend = BackendKind(payload["backend"])
        workdir = str(_ensure_workdir(payload["workdir"]))
        model_name = self._validate_model_name(payload["model_name"])
        reasoning_effort = self._validate_reasoning_effort(payload["reasoning_effort"])
        context = payload.get("context") or {}
        if context:
            self.backend_manager.update_conversation_context(conversation_id, context)
        result = self.backend_manager.create_session(
            backend=backend,
            workdir=workdir,
            conversation_id=conversation_id,
            model_name=model_name,
            reasoning_effort=reasoning_effort,
        )
        if backend == BackendKind.EXEC:
            self.store.create_session(
                conversation_id=conversation_id,
                backend=backend.value,
                workdir=workdir,
                exec_thread_id=result["thread_id"],
                model_name=model_name,
                reasoning_effort=reasoning_effort,
                status="idle",
                activate=True,
            )
            self._deliver_or_store(
                conversation_id=conversation_id,
                title="会话已创建",
                text=(
                    f"已创建 `exec` 会话。\n\n"
                    f"- 目录: `{workdir}`\n"
                    f"- thread_id: `{result['thread_id']}`\n"
                    f"- model: `{model_name}`\n"
                    f"- think: `{reasoning_effort}`"
                ),
            )
            return

        self.store.create_session(
            conversation_id=conversation_id,
            backend=backend.value,
            workdir=workdir,
            tmux_session_name=result["session_name"],
            model_name=model_name,
            reasoning_effort=reasoning_effort,
            status="idle",
            activate=True,
        )
        self._deliver_or_store(
            conversation_id=conversation_id,
            title="会话已创建",
            text=(
                f"已创建 `tmux` 会话。\n\n"
                f"- 目录: `{workdir}`\n"
                f"- session_name: `{result['session_name']}`\n"
                f"- model: `{model_name}`\n"
                f"- think: `{reasoning_effort}`\n"
                f"- agent_session_id: `{result.get('agent_session_id') or '-'}`\n"
                f"- state_path: `{result.get('state_path') or '-'}`"
            ),
        )

    def _handle_resume_session(self, conversation_id: str, payload: dict[str, Any]) -> None:
        backend = BackendKind(payload["backend"])
        workdir = str(_ensure_workdir(payload["workdir"]))
        external_session_id = str(payload["external_session_id"]).strip()
        model_name = self._validate_model_name(payload["model_name"])
        reasoning_effort = self._validate_reasoning_effort(payload["reasoning_effort"])
        context = payload.get("context") or {}
        if context:
            self.backend_manager.update_conversation_context(conversation_id, context)
        result = self.backend_manager.bind_existing_session(
            backend=backend,
            external_session_id=external_session_id,
            workdir=workdir,
            conversation_id=conversation_id,
            model_name=model_name,
            reasoning_effort=reasoning_effort,
        )
        if backend == BackendKind.EXEC:
            self.store.create_session(
                conversation_id=conversation_id,
                backend=backend.value,
                workdir=workdir,
                exec_thread_id=result["thread_id"],
                model_name=model_name,
                reasoning_effort=reasoning_effort,
                status="idle",
                activate=True,
            )
            self._deliver_or_store(
                conversation_id=conversation_id,
                title="会话已恢复",
                text=(
                    f"已绑定已有 `exec` 会话。\n\n"
                    f"- 目录: `{workdir}`\n"
                    f"- thread_id: `{result['thread_id']}`\n"
                    f"- model: `{model_name}`\n"
                    f"- think: `{reasoning_effort}`"
                ),
            )
            return

        self.store.create_session(
            conversation_id=conversation_id,
            backend=backend.value,
            workdir=workdir,
            tmux_session_name=result["session_name"],
            model_name=model_name,
            reasoning_effort=reasoning_effort,
            status="idle",
            activate=True,
        )
        self._deliver_or_store(
            conversation_id=conversation_id,
            title="会话已恢复",
            text=(
                f"已绑定已有 `tmux` 会话。\n\n"
                f"- 目录: `{workdir}`\n"
                f"- session_name: `{result['session_name']}`\n"
                f"- model: `{model_name}`\n"
                f"- think: `{reasoning_effort}`\n"
                f"- agent_session_id: `{result.get('agent_session_id') or '-'}`\n"
                f"- state_path: `{result.get('state_path') or '-'}`"
            ),
        )

    def _handle_reconfigure_session(self, conversation_id: str, payload: dict[str, Any]) -> None:
        active_session = self.store.get_active_session(conversation_id)
        if active_session is None:
            self._deliver_or_store(
                conversation_id=conversation_id,
                title="没有激活会话",
                text="请先执行 `/new ...` 或 `/resume ...`。",
            )
            return

        model_name = self._validate_model_name(payload.get("model_name") or self._resolve_session_model(active_session))
        reasoning_effort = self._validate_reasoning_effort(
            payload.get("reasoning_effort") or self._resolve_session_reasoning(active_session)
        )
        session = self.store.update_session(active_session.id, status="busy")
        try:
            result = self.backend_manager.reconfigure_session(
                session=session,
                model_name=model_name,
                reasoning_effort=reasoning_effort,
            )
            updated = self.store.update_session(
                session.id,
                status="idle",
                model_name=model_name,
                reasoning_effort=reasoning_effort,
            )
            self._deliver_or_store(
                conversation_id=conversation_id,
                title="会话配置已更新",
                text=(
                    f"已更新当前 `{updated.backend}` 会话配置。\n\n"
                    f"- model: `{model_name}`\n"
                    f"- think: `{reasoning_effort}`\n"
                    f"- 说明: {result.get('summary', '配置已生效')}"
                ),
            )
        except Exception:
            self.store.update_session(session.id, status="error")
            raise

    def _handle_prompt(self, conversation_id: str, payload: dict[str, Any]) -> None:
        active_session = self.store.get_active_session(conversation_id)
        if active_session is None:
            self._deliver_or_store(
                conversation_id=conversation_id,
                title="没有激活会话",
                text="请先执行 `/new ...` 或 `/resume ...`。",
            )
            return
        context = payload.get("context") or {}
        if context:
            self.backend_manager.update_conversation_context(conversation_id, context)
        session = self.store.update_session(active_session.id, status="busy")
        try:
            reply = self.backend_manager.send_prompt(session=session, prompt=payload["prompt"])
            chunks = self.delivery.split_text("Codex", reply)
            updated = self.store.update_session(
                session.id,
                status="idle",
                last_reply=reply,
                pending_delivery=[],
            )
            sent, unsent = self.delivery.send_chunks(conversation_id, chunks)
            if not sent:
                self.store.update_session(updated.id, pending_delivery=unsent)
            self.logger.info("prompt completed for conversation=%s session=%s", conversation_id, updated.id)
        except Exception:
            self.store.update_session(session.id, status="error")
            raise

    def _deliver_or_store(
        self,
        *,
        conversation_id: str,
        title: str,
        text: str,
    ) -> None:
        session = self.store.get_active_session(conversation_id)
        if session is None:
            return
        chunks = self.delivery.split_text(title, text)
        sent, unsent = self.delivery.send_chunks(conversation_id, chunks)
        if sent:
            self.store.update_session(session.id, pending_delivery=[])
            return
        pending = list(session.pending_delivery)
        pending.extend(unsent)
        self.store.update_session(session.id, pending_delivery=pending)

    def _store_error_for_later(self, conversation_id: str, error_text: str) -> None:
        session = self.store.get_active_session(conversation_id)
        if session is None:
            return
        chunks = self.delivery.split_text("处理失败", error_text)
        pending = list(session.pending_delivery)
        pending.extend(chunks)
        self.store.update_session(session.id, pending_delivery=pending, status="error")

    def _status_chunks(self, conversation_id: str) -> list[DeliveryChunk]:
        active = self.store.get_active_session(conversation_id)
        queue_depth = self.store.count_pending_jobs(conversation_id)
        if active is None:
            text = "\n".join(
                [
                    "当前没有激活会话。",
                    "",
                    f"- 队列长度: `{queue_depth}`",
                    "- 下一步: 执行 `/new ...` 或 `/resume ...`",
                ]
            )
            return [DeliveryChunk(title="状态", text=text)]

        history = self.store.list_recent_sessions(conversation_id, limit=3)
        history_lines = []
        for item in history:
            session_ref = item.exec_thread_id or item.tmux_session_name or "-"
            history_lines.append(
                f"- `{item.backend}` | `{item.status}` | `{item.workdir}` | `{session_ref}`"
            )

        runtime_lines: list[str] = []
        runtime_info = self.backend_manager.describe_session(active)
        if runtime_info:
            runtime_lines.extend(["", "Runtime", ""])
            if runtime_info.get("runtime_error"):
                runtime_lines.append(f"- 错误: `{runtime_info['runtime_error']}`")
            else:
                ordered_keys = (
                    ("agent_session_id", "agent_session_id"),
                    ("confirmed_status", "confirmed_status"),
                    ("detected_status", "detected_status"),
                    ("current_command", "current_command"),
                    ("current_path", "current_path"),
                    ("pane_id", "pane_id"),
                    ("terminal_id", "terminal_id"),
                    ("log_path", "log_path"),
                    ("state_path", "state_path"),
                )
                for key, label in ordered_keys:
                    value = runtime_info.get(key)
                    if value:
                        runtime_lines.append(f"- {label}: `{value}`")

        text = "\n".join(
            [
                "当前会话",
                "",
                f"- 后端: `{active.backend}`",
                f"- 目录: `{active.workdir}`",
                f"- 状态: `{active.status}`",
                f"- model: `{self._resolve_session_model(active)}`",
                f"- think: `{self._resolve_session_reasoning(active)}`",
                f"- exec_thread_id: `{active.exec_thread_id or '-'}`",
                f"- tmux_session_name: `{active.tmux_session_name or '-'}`",
                f"- 队列长度: `{queue_depth}`",
                f"- 待补发消息: `{len(active.pending_delivery)}`",
                *runtime_lines,
                "",
                "最近会话",
                "",
                *(history_lines or ["- 无"]),
            ]
        )
        return [DeliveryChunk(title="状态", text=text)]

    def _try_flush_pending_delivery(self, conversation_id: str) -> None:
        session = self.store.get_active_session(conversation_id)
        if session is None or not session.pending_delivery:
            return
        sent, unsent = self.delivery.send_chunks(conversation_id, session.pending_delivery)
        if sent:
            self.store.clear_pending_delivery(session.id)
            return
        self.store.update_session(session.id, pending_delivery=unsent)
