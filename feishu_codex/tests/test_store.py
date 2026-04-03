from pathlib import Path

from feishu_codex_bridge.models import DeliveryChunk
from feishu_codex_bridge.store import BridgeStore


def test_store_session_and_pending_delivery(tmp_path: Path):
    store = BridgeStore(tmp_path / "bridge.sqlite3")
    session = store.create_session(
        conversation_id="cid-1",
        backend="exec",
        workdir="/tmp",
        exec_thread_id="thread-1",
    )
    updated = store.update_session(
        session.id,
        pending_delivery=[DeliveryChunk(title="t", text="body")],
        last_reply="answer",
        status="idle",
    )
    active = store.get_active_session("cid-1")
    assert active is not None
    assert active.exec_thread_id == "thread-1"
    assert active.last_reply == "answer"
    assert updated.pending_delivery[0].text == "body"


def test_store_job_queue(tmp_path: Path):
    store = BridgeStore(tmp_path / "bridge.sqlite3")
    store.enqueue_job("cid-1", "prompt", {"prompt": "hello"})
    store.enqueue_job("cid-1", "prompt", {"prompt": "world"})
    first = store.claim_next_job("cid-1")
    second = store.claim_next_job("cid-1")
    assert first is not None
    assert second is not None
    assert first.payload["prompt"] == "hello"
    assert second.payload["prompt"] == "world"
    store.complete_job(first.id, summary="ok")
    store.fail_job(second.id, error_text="boom")


def test_store_register_processed_message_is_idempotent(tmp_path: Path):
    store = BridgeStore(tmp_path / "bridge.sqlite3")
    assert store.register_processed_message("msg-1", "cid-1") is True
    assert store.register_processed_message("msg-1", "cid-1") is False


def test_store_register_consecutive_signature_blocks_repeated_message(tmp_path: Path):
    store = BridgeStore(tmp_path / "bridge.sqlite3")
    assert store.register_consecutive_signature("cid-1", "user-a|text|hello") is True
    assert store.register_consecutive_signature("cid-1", "user-a|text|hello") is False
    assert store.register_consecutive_signature("cid-1", "user-a|text|world") is True
    assert store.register_consecutive_signature("cid-1", "user-a|text|hello") is True
