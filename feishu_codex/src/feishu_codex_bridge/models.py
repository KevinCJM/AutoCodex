from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class BackendKind(str, Enum):
    EXEC = "exec"
    TMUX = "tmux"


class CommandKind(str, Enum):
    HELP = "help"
    STATUS = "status"
    NEW = "new"
    RESUME = "resume"
    MODEL = "model"
    THINK = "think"
    STOP = "stop"
    PROMPT = "prompt"
    IGNORE = "ignore"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class IncomingEnvelope:
    conversation_id: str
    chat_id: str
    sender_open_id: str
    sender_user_id: str
    sender_union_id: str
    sender_type: str
    sender_tenant_key: str
    message_id: str
    message_type: str
    chat_type: str
    is_at_bot: bool
    raw_text: str
    cleaned_text: str
    is_group: bool


@dataclass(frozen=True)
class ParsedMessage:
    kind: CommandKind
    text: str = ""
    backend: BackendKind | None = None
    workdir: str = ""
    external_session_id: str = ""
    model_name: str = ""
    reasoning_effort: str = ""
    error: str = ""


@dataclass(frozen=True)
class DeliveryChunk:
    title: str
    text: str

    def to_dict(self) -> dict[str, str]:
        return {"title": self.title, "text": self.text}

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "DeliveryChunk":
        return cls(
            title=str(payload.get("title", "") or ""),
            text=str(payload.get("text", "") or ""),
        )


@dataclass
class SessionRecord:
    id: int
    conversation_id: str
    backend: str
    workdir: str
    exec_thread_id: str
    tmux_session_name: str
    model_name: str
    reasoning_effort: str
    status: str
    last_reply: str
    pending_delivery: list[DeliveryChunk] = field(default_factory=list)
    is_active: bool = True
    created_at: str = ""
    updated_at: str = ""


@dataclass
class JobRecord:
    id: int
    conversation_id: str
    job_type: str
    payload: dict[str, Any]
    status: str
    created_at: str
    updated_at: str
    error_text: str = ""
    result_summary: str = ""
