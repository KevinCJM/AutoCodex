from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import DeliveryChunk, JobRecord, SessionRecord


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class BridgeStore:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._conn:
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._initialize()

    def _initialize(self) -> None:
        with self._conn:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id TEXT NOT NULL,
                    backend TEXT NOT NULL,
                    workdir TEXT NOT NULL,
                    exec_thread_id TEXT NOT NULL DEFAULT '',
                    tmux_session_name TEXT NOT NULL DEFAULT '',
                    model_name TEXT NOT NULL DEFAULT '',
                    reasoning_effort TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    last_reply TEXT NOT NULL DEFAULT '',
                    pending_delivery TEXT NOT NULL DEFAULT '[]',
                    is_active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            self._ensure_column("sessions", "model_name", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column("sessions", "reasoning_effort", "TEXT NOT NULL DEFAULT ''")
            self._conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_sessions_conversation_active
                ON sessions (conversation_id, is_active, updated_at)
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id TEXT NOT NULL,
                    job_type TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    error_text TEXT NOT NULL DEFAULT '',
                    result_summary TEXT NOT NULL DEFAULT ''
                )
                """
            )
            self._conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_jobs_conversation_status
                ON jobs (conversation_id, status, id)
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS processed_messages (
                    message_id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_processed_messages_conversation
                ON processed_messages (conversation_id, created_at)
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS conversation_dedup_state (
                    conversation_id TEXT PRIMARY KEY,
                    signature TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )

    def _ensure_column(self, table_name: str, column_name: str, column_ddl: str) -> None:
        columns = {
            str(row["name"])
            for row in self._conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        }
        if column_name in columns:
            return
        self._conn.execute(
            f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_ddl}"
        )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def mark_running_jobs_failed_on_startup(self) -> None:
        now = _utc_now()
        with self._lock, self._conn:
            self._conn.execute(
                """
                UPDATE jobs
                SET status = 'failed', updated_at = ?, error_text = 'bridge restarted while job was running'
                WHERE status = 'running'
                """,
                (now,),
            )

    def _row_to_session(self, row: sqlite3.Row | None) -> SessionRecord | None:
        if row is None:
            return None
        pending_payload = json.loads(row["pending_delivery"] or "[]")
        pending_delivery = [DeliveryChunk.from_dict(item) for item in pending_payload]
        return SessionRecord(
            id=int(row["id"]),
            conversation_id=str(row["conversation_id"]),
            backend=str(row["backend"]),
            workdir=str(row["workdir"]),
            exec_thread_id=str(row["exec_thread_id"]),
            tmux_session_name=str(row["tmux_session_name"]),
            model_name=str(row["model_name"]),
            reasoning_effort=str(row["reasoning_effort"]),
            status=str(row["status"]),
            last_reply=str(row["last_reply"]),
            pending_delivery=pending_delivery,
            is_active=bool(row["is_active"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    def _row_to_job(self, row: sqlite3.Row | None) -> JobRecord | None:
        if row is None:
            return None
        return JobRecord(
            id=int(row["id"]),
            conversation_id=str(row["conversation_id"]),
            job_type=str(row["job_type"]),
            payload=json.loads(row["payload"]),
            status=str(row["status"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            error_text=str(row["error_text"]),
            result_summary=str(row["result_summary"]),
        )

    def create_session(
        self,
        *,
        conversation_id: str,
        backend: str,
        workdir: str,
        exec_thread_id: str = "",
        tmux_session_name: str = "",
        model_name: str = "",
        reasoning_effort: str = "",
        status: str = "idle",
        activate: bool = True,
    ) -> SessionRecord:
        now = _utc_now()
        with self._lock, self._conn:
            if activate:
                self._conn.execute(
                    """
                    UPDATE sessions
                    SET is_active = 0, updated_at = ?, status = CASE WHEN status = 'busy' THEN 'idle' ELSE status END
                    WHERE conversation_id = ? AND is_active = 1
                    """,
                    (now, conversation_id),
                )
            cursor = self._conn.execute(
                """
                INSERT INTO sessions (
                    conversation_id, backend, workdir, exec_thread_id, tmux_session_name,
                    model_name, reasoning_effort, status, last_reply, pending_delivery, is_active, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, '', '[]', ?, ?, ?)
                """,
                (
                    conversation_id,
                    backend,
                    workdir,
                    exec_thread_id,
                    tmux_session_name,
                    model_name,
                    reasoning_effort,
                    status,
                    1 if activate else 0,
                    now,
                    now,
                ),
            )
            row = self._conn.execute(
                "SELECT * FROM sessions WHERE id = ?",
                (cursor.lastrowid,),
            ).fetchone()
        return self._row_to_session(row)  # type: ignore[return-value]

    def get_active_session(self, conversation_id: str) -> SessionRecord | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT * FROM sessions
                WHERE conversation_id = ? AND is_active = 1
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (conversation_id,),
            ).fetchone()
        return self._row_to_session(row)

    def list_recent_sessions(self, conversation_id: str, limit: int = 5) -> list[SessionRecord]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM sessions
                WHERE conversation_id = ?
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (conversation_id, limit),
            ).fetchall()
        return [session for row in rows if (session := self._row_to_session(row)) is not None]

    def update_session(
        self,
        session_id: int,
        *,
        status: str | None = None,
        last_reply: str | None = None,
        pending_delivery: list[DeliveryChunk] | None = None,
        exec_thread_id: str | None = None,
        tmux_session_name: str | None = None,
        model_name: str | None = None,
        reasoning_effort: str | None = None,
        is_active: bool | None = None,
    ) -> SessionRecord:
        now = _utc_now()
        assignments: list[str] = ["updated_at = ?"]
        values: list[Any] = [now]
        if status is not None:
            assignments.append("status = ?")
            values.append(status)
        if last_reply is not None:
            assignments.append("last_reply = ?")
            values.append(last_reply)
        if pending_delivery is not None:
            assignments.append("pending_delivery = ?")
            values.append(json.dumps([item.to_dict() for item in pending_delivery], ensure_ascii=False))
        if exec_thread_id is not None:
            assignments.append("exec_thread_id = ?")
            values.append(exec_thread_id)
        if tmux_session_name is not None:
            assignments.append("tmux_session_name = ?")
            values.append(tmux_session_name)
        if model_name is not None:
            assignments.append("model_name = ?")
            values.append(model_name)
        if reasoning_effort is not None:
            assignments.append("reasoning_effort = ?")
            values.append(reasoning_effort)
        if is_active is not None:
            assignments.append("is_active = ?")
            values.append(1 if is_active else 0)
        values.append(session_id)

        with self._lock, self._conn:
            self._conn.execute(
                f"UPDATE sessions SET {', '.join(assignments)} WHERE id = ?",
                values,
            )
            row = self._conn.execute(
                "SELECT * FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
        session = self._row_to_session(row)
        if session is None:
            raise RuntimeError(f"session not found after update: {session_id}")
        return session

    def clear_pending_delivery(self, session_id: int) -> SessionRecord:
        return self.update_session(session_id, pending_delivery=[])

    def stop_active_session(self, conversation_id: str) -> SessionRecord | None:
        session = self.get_active_session(conversation_id)
        if session is None:
            return None
        self.cancel_pending_jobs(conversation_id)
        return self.update_session(session.id, status="stopped", is_active=False)

    def enqueue_job(self, conversation_id: str, job_type: str, payload: dict[str, Any]) -> JobRecord:
        now = _utc_now()
        with self._lock, self._conn:
            cursor = self._conn.execute(
                """
                INSERT INTO jobs (
                    conversation_id, job_type, payload, status, created_at, updated_at, error_text, result_summary
                ) VALUES (?, ?, ?, 'pending', ?, ?, '', '')
                """,
                (conversation_id, job_type, json.dumps(payload, ensure_ascii=False), now, now),
            )
            row = self._conn.execute(
                "SELECT * FROM jobs WHERE id = ?",
                (cursor.lastrowid,),
            ).fetchone()
        return self._row_to_job(row)  # type: ignore[return-value]

    def count_pending_jobs(self, conversation_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT COUNT(1) AS total
                FROM jobs
                WHERE conversation_id = ? AND status IN ('pending', 'running')
                """,
                (conversation_id,),
            ).fetchone()
        return int(row["total"]) if row else 0

    def claim_next_job(self, conversation_id: str) -> JobRecord | None:
        now = _utc_now()
        with self._lock, self._conn:
            row = self._conn.execute(
                """
                SELECT * FROM jobs
                WHERE conversation_id = ? AND status = 'pending'
                ORDER BY id ASC
                LIMIT 1
                """,
                (conversation_id,),
            ).fetchone()
            if row is None:
                return None
            self._conn.execute(
                """
                UPDATE jobs
                SET status = 'running', updated_at = ?
                WHERE id = ?
                """,
                (now, row["id"]),
            )
            updated = self._conn.execute(
                "SELECT * FROM jobs WHERE id = ?",
                (row["id"],),
            ).fetchone()
        return self._row_to_job(updated)

    def complete_job(self, job_id: int, summary: str = "") -> None:
        now = _utc_now()
        with self._lock, self._conn:
            self._conn.execute(
                """
                UPDATE jobs
                SET status = 'completed', updated_at = ?, result_summary = ?
                WHERE id = ?
                """,
                (now, summary, job_id),
            )

    def fail_job(self, job_id: int, error_text: str) -> None:
        now = _utc_now()
        with self._lock, self._conn:
            self._conn.execute(
                """
                UPDATE jobs
                SET status = 'failed', updated_at = ?, error_text = ?
                WHERE id = ?
                """,
                (now, error_text, job_id),
            )

    def cancel_pending_jobs(self, conversation_id: str) -> None:
        now = _utc_now()
        with self._lock, self._conn:
            self._conn.execute(
                """
                UPDATE jobs
                SET status = 'cancelled', updated_at = ?, error_text = 'cancelled by /stop'
                WHERE conversation_id = ? AND status = 'pending'
                """,
                (now, conversation_id),
            )

    def register_processed_message(self, message_id: str, conversation_id: str) -> bool:
        message_id = str(message_id or "").strip()
        if not message_id:
            return True
        now = _utc_now()
        with self._lock, self._conn:
            cursor = self._conn.execute(
                """
                INSERT OR IGNORE INTO processed_messages (
                    message_id, conversation_id, created_at
                ) VALUES (?, ?, ?)
                """,
                (message_id, conversation_id, now),
            )
        return cursor.rowcount > 0

    def register_consecutive_signature(
        self,
        conversation_id: str,
        signature: str,
        *,
        dedupe_window_sec: float = 300.0,
    ) -> bool:
        signature = str(signature or "").strip()
        if not signature:
            return True

        now = datetime.now(timezone.utc)
        now_text = now.isoformat()
        with self._lock, self._conn:
            row = self._conn.execute(
                """
                SELECT signature, updated_at
                FROM conversation_dedup_state
                WHERE conversation_id = ?
                """,
                (conversation_id,),
            ).fetchone()

            if row is not None:
                previous_signature = str(row["signature"] or "")
                updated_at_text = str(row["updated_at"] or "")
                try:
                    updated_at = datetime.fromisoformat(updated_at_text)
                except ValueError:
                    updated_at = None

                if (
                    previous_signature == signature
                    and updated_at is not None
                    and (now - updated_at).total_seconds() <= float(dedupe_window_sec)
                ):
                    self._conn.execute(
                        """
                        UPDATE conversation_dedup_state
                        SET updated_at = ?
                        WHERE conversation_id = ?
                        """,
                        (now_text, conversation_id),
                    )
                    return False

            self._conn.execute(
                """
                INSERT INTO conversation_dedup_state (
                    conversation_id, signature, updated_at
                ) VALUES (?, ?, ?)
                ON CONFLICT(conversation_id) DO UPDATE SET
                    signature = excluded.signature,
                    updated_at = excluded.updated_at
                """,
                (conversation_id, signature, now_text),
            )
        return True
