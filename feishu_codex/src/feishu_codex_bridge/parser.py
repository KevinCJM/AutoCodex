from __future__ import annotations

import json
import re
import shlex
from typing import Any

from .models import BackendKind, CommandKind, IncomingEnvelope, ParsedMessage

AT_TAG_PATTERN = re.compile(r"<at\b[^>]*>.*?</at>", re.IGNORECASE | re.DOTALL)


def _load_text_content(content: str) -> str:
    payload = (content or "").strip()
    if not payload:
        return ""
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        return payload
    if isinstance(parsed, dict):
        return str(parsed.get("text", "") or "").strip()
    return payload


def _strip_mentions(text: str) -> str:
    cleaned = AT_TAG_PATTERN.sub("", text)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip()


def _mentioned_open_ids(message: Any) -> tuple[str, ...]:
    mentions = getattr(message, "mentions", None) or []
    values: list[str] = []
    for mention in mentions:
        user_id = getattr(getattr(mention, "id", None), "open_id", None)
        if user_id:
            values.append(str(user_id))
    return tuple(values)


def build_envelope(event: Any, *, bot_open_id: str = "") -> IncomingEnvelope:
    sender = getattr(getattr(event, "event", None), "sender", None)
    message = getattr(getattr(event, "event", None), "message", None)
    sender_id = getattr(sender, "sender_id", None)
    raw_text = _load_text_content(str(getattr(message, "content", "") or ""))
    is_group = str(getattr(message, "chat_type", "") or "") == "group"
    mention_open_ids = _mentioned_open_ids(message)
    has_at_tag = bool(AT_TAG_PATTERN.search(raw_text))
    if bot_open_id:
        is_at_bot = bot_open_id in mention_open_ids or f'user_id="{bot_open_id}"' in raw_text
    else:
        is_at_bot = bool(mention_open_ids) or has_at_tag
    cleaned_text = _strip_mentions(raw_text) if is_group else raw_text
    return IncomingEnvelope(
        conversation_id=str(getattr(message, "chat_id", "") or ""),
        chat_id=str(getattr(message, "chat_id", "") or ""),
        sender_open_id=str(getattr(sender_id, "open_id", "") or ""),
        sender_user_id=str(getattr(sender_id, "user_id", "") or ""),
        sender_union_id=str(getattr(sender_id, "union_id", "") or ""),
        sender_type=str(getattr(sender, "sender_type", "") or ""),
        sender_tenant_key=str(getattr(sender, "tenant_key", "") or ""),
        message_id=str(getattr(message, "message_id", "") or ""),
        message_type=str(getattr(message, "message_type", "") or ""),
        chat_type=str(getattr(message, "chat_type", "") or ""),
        is_at_bot=is_at_bot,
        raw_text=raw_text,
        cleaned_text=cleaned_text,
        is_group=is_group,
    )


def _parse_single_message_text(text: str) -> ParsedMessage:
    try:
        parts = shlex.split(text)
    except ValueError as error:
        return ParsedMessage(kind=CommandKind.UNSUPPORTED, error=str(error))

    if not parts:
        return ParsedMessage(kind=CommandKind.HELP)

    command = parts[0].lower()
    if command == "/help":
        return ParsedMessage(kind=CommandKind.HELP)
    if command == "/status":
        return ParsedMessage(kind=CommandKind.STATUS)
    if command == "/model":
        if len(parts) != 2:
            return ParsedMessage(
                kind=CommandKind.UNSUPPORTED,
                error="Usage: /model <model_id>",
            )
        return ParsedMessage(kind=CommandKind.MODEL, model_name=parts[1].strip())
    if command == "/think":
        if len(parts) != 2:
            return ParsedMessage(
                kind=CommandKind.UNSUPPORTED,
                error="Usage: /think <low|medium|high|xhigh>",
            )
        return ParsedMessage(kind=CommandKind.THINK, reasoning_effort=parts[1].strip().lower())
    if command == "/stop":
        return ParsedMessage(kind=CommandKind.STOP)
    if command == "/new":
        if len(parts) != 3 or parts[1] not in {BackendKind.EXEC.value, BackendKind.TMUX.value}:
            return ParsedMessage(
                kind=CommandKind.UNSUPPORTED,
                error="Usage: /new exec <absolute_workdir> or /new tmux <absolute_workdir>",
            )
        return ParsedMessage(
            kind=CommandKind.NEW,
            backend=BackendKind(parts[1]),
            workdir=parts[2],
        )
    if command == "/resume":
        if len(parts) != 4 or parts[1] not in {BackendKind.EXEC.value, BackendKind.TMUX.value}:
            return ParsedMessage(
                kind=CommandKind.UNSUPPORTED,
                error=(
                    "Usage: /resume exec <thread_id> <absolute_workdir> "
                    "or /resume tmux <tmux_session_name> <absolute_workdir>"
                ),
            )
        return ParsedMessage(
            kind=CommandKind.RESUME,
            backend=BackendKind(parts[1]),
            external_session_id=parts[2],
            workdir=parts[3],
        )
    return ParsedMessage(
        kind=CommandKind.UNSUPPORTED,
        error=f"Unsupported command: {parts[0]}",
    )


def parse_messages(envelope: IncomingEnvelope) -> tuple[ParsedMessage, ...]:
    if envelope.is_group and not envelope.is_at_bot:
        return (ParsedMessage(kind=CommandKind.IGNORE),)

    if envelope.message_type == "post":
        return (ParsedMessage(kind=CommandKind.IGNORE),)

    if envelope.message_type and envelope.message_type != "text":
        return (
            ParsedMessage(
                kind=CommandKind.UNSUPPORTED,
                error=f"暂不支持 `{envelope.message_type}` 消息，只支持文本消息。",
            ),
        )

    text = envelope.cleaned_text.strip()
    if not text:
        return (ParsedMessage(kind=CommandKind.HELP),)

    if not text.startswith("/"):
        return (ParsedMessage(kind=CommandKind.PROMPT, text=text),)

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) <= 1:
        return (_parse_single_message_text(text),)
    if any(not line.startswith("/") for line in lines):
        return (
            ParsedMessage(
                kind=CommandKind.UNSUPPORTED,
                error="多行消息若以 `/` 开头，必须每一行都是独立的 slash 命令。",
            ),
        )

    parsed_messages: list[ParsedMessage] = []
    for line in lines:
        parsed = _parse_single_message_text(line)
        if parsed.kind == CommandKind.UNSUPPORTED:
            return (parsed,)
        parsed_messages.append(parsed)
    return tuple(parsed_messages)


def parse_message(envelope: IncomingEnvelope) -> ParsedMessage:
    return parse_messages(envelope)[0]
