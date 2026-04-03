from __future__ import annotations

import json
from pathlib import Path
from itertools import count
import time
from types import SimpleNamespace

from feishu_codex_bridge.config import Settings
from feishu_codex_bridge.delivery import DeliveryService
from feishu_codex_bridge.models import DeliveryChunk
from feishu_codex_bridge.service import BridgeService
from feishu_codex_bridge.store import BridgeStore


_MESSAGE_COUNTER = count(1)


class FakeBackendManager:
    def __init__(self):
        self.prompts = []
        self.reconfigure_calls = []
        self.context_updates = []
        self.create_calls = []
        self.bind_calls = []

    def update_conversation_context(self, conversation_id, context):
        self.context_updates.append((conversation_id, context))
        return Path("/tmp/fake-feishu-context.json")

    def create_session(self, backend, workdir, conversation_id, model_name, reasoning_effort):
        self.create_calls.append((backend.value, workdir, conversation_id, model_name, reasoning_effort))
        if backend.value == "exec":
            return {"thread_id": "thread-1", "summary": "ok", "model_name": model_name, "reasoning_effort": reasoning_effort}
        return {
            "session_name": "tmux-1",
            "summary": "ok",
            "model_name": model_name,
            "reasoning_effort": reasoning_effort,
        }

    def bind_existing_session(self, backend, external_session_id, workdir, conversation_id, model_name, reasoning_effort):
        self.bind_calls.append(
            (backend.value, external_session_id, workdir, conversation_id, model_name, reasoning_effort)
        )
        if backend.value == "exec":
            return {
                "thread_id": external_session_id,
                "summary": "ok",
                "model_name": model_name,
                "reasoning_effort": reasoning_effort,
            }
        return {
            "session_name": external_session_id,
            "summary": "ok",
            "model_name": model_name,
            "reasoning_effort": reasoning_effort,
        }

    def send_prompt(self, session, prompt):
        self.prompts.append((session.backend, prompt))
        return f"reply: {prompt}"

    def reconfigure_session(self, session, model_name, reasoning_effort):
        self.reconfigure_calls.append((session.backend, model_name, reasoning_effort))
        return {
            "summary": "config updated",
            "model_name": model_name,
            "reasoning_effort": reasoning_effort,
        }

    def describe_session(self, session):
        if session.backend != "tmux":
            return {"exec_thread_id": session.exec_thread_id}
        return {
            "agent_session_id": "codex-session-1",
            "confirmed_status": "idle",
            "detected_status": "processing",
            "current_command": "codex",
            "current_path": session.workdir,
            "pane_id": "%1",
            "terminal_id": "term-1",
            "log_path": "/tmp/runtime/log.txt",
            "state_path": "/tmp/runtime/state.json",
        }


class FakeClient:
    def __init__(self):
        self.sent = []
        self.im = SimpleNamespace(v1=SimpleNamespace(message=SimpleNamespace(create=self._create)))

    def _create(self, request):
        body = request.request_body
        self.sent.append((request.receive_id_type, body.receive_id, body.msg_type, body.content))
        return SimpleNamespace(success=lambda: True, code=0, msg="ok", get_log_id=lambda: "log")


def make_event(text: str, chat_type: str = "p2p", message_id: str = "msg-1"):
    if message_id == "msg-1":
        message_id = f"msg-{next(_MESSAGE_COUNTER)}"
    return SimpleNamespace(
        event=SimpleNamespace(
            sender=SimpleNamespace(
                sender_id=SimpleNamespace(user_id="ou-user", open_id="ou-open", union_id="on-union"),
                sender_type="user",
                tenant_key="tenant",
            ),
            message=SimpleNamespace(
                message_id=message_id,
                chat_id="chat-1",
                chat_type=chat_type,
                message_type="text",
                content=json.dumps({"text": text}, ensure_ascii=False),
                mentions=[],
            ),
        )
    )


def build_settings(tmp_path: Path) -> Settings:
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    return Settings(
        app_id="id",
        app_secret="secret",
        verification_token="",
        encrypt_key="",
        runtime_dir=runtime_dir,
        db_path=tmp_path / "bridge.sqlite3",
        log_dir=log_dir,
        allow_all_senders=True,
        allowed_senders=(),
        max_workers=2,
        exec_timeout_sec=10,
        tmux_timeout_sec=20,
        text_chunk_chars=400,
        bot_open_id="",
        default_model="gpt-5.4",
        default_reasoning_effort="xhigh",
        available_models=("gpt-5.4", "gpt-5.4-mini", "gpt-5.1-codex-mini"),
        available_reasoning_efforts=("low", "medium", "high", "xhigh"),
    )


def wait_for_jobs(service: BridgeService, store: BridgeStore, conversation_id: str, timeout: float = 2.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if store.count_pending_jobs(conversation_id) == 0:
            with service._running_lock:
                if conversation_id not in service._running_conversations:
                    return
        time.sleep(0.05)
    raise AssertionError("jobs did not finish in time")


def test_service_queues_messages_and_replies(tmp_path: Path):
    resolved_tmp = str(Path("/tmp").resolve())
    settings = build_settings(tmp_path)
    store = BridgeStore(settings.db_path)
    delivery = DeliveryService(
        settings=settings,
        logger=__import__("logging").getLogger("test"),
        client=FakeClient(),
    )
    manager = FakeBackendManager()
    service = BridgeService(
        settings=settings,
        store=store,
        backend_manager=manager,
        delivery=delivery,
        logger=__import__("logging").getLogger("test"),
    )

    service.handle_event(make_event("/new exec /tmp"))
    wait_for_jobs(service, store, "chat-1")

    service.handle_event(make_event("hello"))
    wait_for_jobs(service, store, "chat-1")

    active = store.get_active_session("chat-1")
    assert active is not None
    assert active.exec_thread_id == "thread-1"
    assert active.model_name == "gpt-5.4"
    assert active.reasoning_effort == "xhigh"
    assert not active.pending_delivery
    assert manager.prompts == [("exec", "hello")]
    assert manager.create_calls == [("exec", resolved_tmp, "chat-1", "gpt-5.4", "xhigh")]
    assert manager.context_updates[0][1]["chat_id"] == "chat-1"
    assert manager.context_updates[-1][1]["cleaned_text"] == "hello"

    service.shutdown()


def test_status_flushes_pending_delivery(tmp_path: Path):
    settings = build_settings(tmp_path)
    store = BridgeStore(settings.db_path)
    client = FakeClient()
    delivery = DeliveryService(
        settings=settings,
        logger=__import__("logging").getLogger("test"),
        client=client,
    )
    manager = FakeBackendManager()
    service = BridgeService(
        settings=settings,
        store=store,
        backend_manager=manager,
        delivery=delivery,
        logger=__import__("logging").getLogger("test"),
    )
    session = store.create_session(
        conversation_id="chat-1",
        backend="exec",
        workdir="/tmp",
        exec_thread_id="thread-1",
        model_name="gpt-5.4",
        reasoning_effort="xhigh",
    )
    store.update_session(session.id, pending_delivery=[DeliveryChunk(title="t", text="cached")])

    service.handle_event(make_event("/status"))
    active = store.get_active_session("chat-1")
    assert active is not None
    assert not active.pending_delivery
    assert any("cached" in payload for _, _, _, payload in client.sent)
    service.shutdown()


def test_status_includes_tmux_runtime_details(tmp_path: Path):
    settings = build_settings(tmp_path)
    store = BridgeStore(settings.db_path)
    client = FakeClient()
    delivery = DeliveryService(
        settings=settings,
        logger=__import__("logging").getLogger("test"),
        client=client,
    )
    manager = FakeBackendManager()
    service = BridgeService(
        settings=settings,
        store=store,
        backend_manager=manager,
        delivery=delivery,
        logger=__import__("logging").getLogger("test"),
    )
    store.create_session(
        conversation_id="chat-1",
        backend="tmux",
        workdir="/tmp",
        tmux_session_name="tmux-1",
        model_name="gpt-5.4",
        reasoning_effort="xhigh",
    )

    service.handle_event(make_event("/status"))

    payloads = [payload for _, _, _, payload in client.sent]
    assert any("agent_session_id" in payload for payload in payloads)
    assert any("current_command" in payload for payload in payloads)
    service.shutdown()


def test_duplicate_message_id_is_ignored(tmp_path: Path):
    settings = build_settings(tmp_path)
    store = BridgeStore(settings.db_path)
    client = FakeClient()
    delivery = DeliveryService(
        settings=settings,
        logger=__import__("logging").getLogger("test"),
        client=client,
    )
    manager = FakeBackendManager()
    service = BridgeService(
        settings=settings,
        store=store,
        backend_manager=manager,
        delivery=delivery,
        logger=__import__("logging").getLogger("test"),
    )

    service.handle_event(make_event("/new exec /tmp", message_id="new-1"))
    wait_for_jobs(service, store, "chat-1")

    service.handle_event(make_event("hello", message_id="dup-1"))
    service.handle_event(make_event("hello", message_id="dup-1"))
    wait_for_jobs(service, store, "chat-1")

    assert manager.prompts == [("exec", "hello")]
    sent_payloads = [payload for _, _, _, payload in client.sent]
    assert sum("已入队" in payload for payload in sent_payloads) == 2
    assert sum("已排队" in payload for payload in sent_payloads) == 0

    service.shutdown()


def test_consecutive_same_prompt_is_ignored_even_with_different_message_ids(tmp_path: Path):
    settings = build_settings(tmp_path)
    store = BridgeStore(settings.db_path)
    client = FakeClient()
    delivery = DeliveryService(
        settings=settings,
        logger=__import__("logging").getLogger("test"),
        client=client,
    )
    manager = FakeBackendManager()
    service = BridgeService(
        settings=settings,
        store=store,
        backend_manager=manager,
        delivery=delivery,
        logger=__import__("logging").getLogger("test"),
    )

    service.handle_event(make_event("/new exec /tmp", message_id="new-1"))
    wait_for_jobs(service, store, "chat-1")

    service.handle_event(make_event("hello", message_id="msg-a"))
    service.handle_event(make_event("hello", message_id="msg-b"))
    wait_for_jobs(service, store, "chat-1")

    assert manager.prompts == [("exec", "hello")]
    assert store.count_pending_jobs("chat-1") == 0
    sent_payloads = [payload for _, _, _, payload in client.sent]
    assert sum("已入队" in payload for payload in sent_payloads) == 2
    assert sum("已排队" in payload for payload in sent_payloads) == 0

    service.shutdown()


def test_model_and_think_commands_update_active_session(tmp_path: Path):
    settings = build_settings(tmp_path)
    store = BridgeStore(settings.db_path)
    client = FakeClient()
    delivery = DeliveryService(
        settings=settings,
        logger=__import__("logging").getLogger("test"),
        client=client,
    )
    manager = FakeBackendManager()
    service = BridgeService(
        settings=settings,
        store=store,
        backend_manager=manager,
        delivery=delivery,
        logger=__import__("logging").getLogger("test"),
    )

    service.handle_event(make_event("/new tmux /tmp", message_id="new-1"))
    wait_for_jobs(service, store, "chat-1")

    service.handle_event(make_event("/model gpt-5.4-mini", message_id="model-1"))
    service.handle_event(make_event("/think medium", message_id="think-1"))
    wait_for_jobs(service, store, "chat-1")

    active = store.get_active_session("chat-1")
    assert active is not None
    assert active.model_name == "gpt-5.4-mini"
    assert active.reasoning_effort == "medium"
    assert manager.reconfigure_calls == [
        ("tmux", "gpt-5.4-mini", "xhigh"),
        ("tmux", "gpt-5.4-mini", "medium"),
    ]

    service.shutdown()


def test_multiline_model_and_think_commands_run_sequentially(tmp_path: Path):
    settings = build_settings(tmp_path)
    store = BridgeStore(settings.db_path)
    client = FakeClient()
    delivery = DeliveryService(
        settings=settings,
        logger=__import__("logging").getLogger("test"),
        client=client,
    )
    manager = FakeBackendManager()
    service = BridgeService(
        settings=settings,
        store=store,
        backend_manager=manager,
        delivery=delivery,
        logger=__import__("logging").getLogger("test"),
    )

    service.handle_event(make_event("/new tmux /tmp", message_id="new-1"))
    wait_for_jobs(service, store, "chat-1")

    service.handle_event(
        make_event("/model gpt-5.4-mini\n/think high", message_id="batch-1")
    )
    wait_for_jobs(service, store, "chat-1")

    active = store.get_active_session("chat-1")
    assert active is not None
    assert active.model_name == "gpt-5.4-mini"
    assert active.reasoning_effort == "high"
    assert manager.reconfigure_calls == [
        ("tmux", "gpt-5.4-mini", "xhigh"),
        ("tmux", "gpt-5.4-mini", "high"),
    ]
    sent_payloads = [payload for _, _, _, payload in client.sent]
    assert sum("处理中" in payload for payload in sent_payloads) >= 3

    service.shutdown()


def test_resume_session_passes_runtime_context(tmp_path: Path):
    resolved_tmp = str(Path("/tmp").resolve())
    settings = build_settings(tmp_path)
    store = BridgeStore(settings.db_path)
    client = FakeClient()
    delivery = DeliveryService(
        settings=settings,
        logger=__import__("logging").getLogger("test"),
        client=client,
    )
    manager = FakeBackendManager()
    service = BridgeService(
        settings=settings,
        store=store,
        backend_manager=manager,
        delivery=delivery,
        logger=__import__("logging").getLogger("test"),
    )

    service.handle_event(make_event("/resume exec thread-9 /tmp", message_id="resume-1"))
    wait_for_jobs(service, store, "chat-1")

    assert manager.bind_calls == [("exec", "thread-9", resolved_tmp, "chat-1", "gpt-5.4", "xhigh")]
    assert manager.context_updates[-1][1]["message_id"] == "resume-1"
    assert manager.context_updates[-1][1]["preferred_backend"] == "exec"

    service.shutdown()
