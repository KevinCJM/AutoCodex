# -*- encoding: utf-8 -*-
"""
@File: T10_tui_protocol.py
@Modify Time: 2026/4/16
@Author: Kevin-Chen
@Descriptions: OpenTUI <-> Python backend NDJSON 协议
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Mapping


PROTOCOL_VERSION = "1.0"


def build_request(action: str, payload: Mapping[str, Any] | None = None, *, message_id: str | None = None) -> dict[str, Any]:
    return {
        "kind": "request",
        "id": message_id or uuid.uuid4().hex,
        "version": PROTOCOL_VERSION,
        "action": str(action).strip(),
        "payload": dict(payload or {}),
    }


def build_response(
    request_id: str,
    *,
    ok: bool,
    payload: Mapping[str, Any] | None = None,
    error: str = "",
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "kind": "response",
        "id": str(request_id).strip(),
        "version": PROTOCOL_VERSION,
        "ok": bool(ok),
        "payload": dict(payload or {}),
    }
    if error:
        data["error"] = str(error)
    return data


def build_event(event_type: str, payload: Mapping[str, Any] | None = None, *, message_id: str | None = None) -> dict[str, Any]:
    return {
        "kind": "event",
        "id": message_id or uuid.uuid4().hex,
        "version": PROTOCOL_VERSION,
        "type": str(event_type).strip(),
        "payload": dict(payload or {}),
    }


def encode_message(message: Mapping[str, Any]) -> str:
    return json.dumps(dict(message), ensure_ascii=False) + "\n"


def decode_message(raw: str) -> dict[str, Any]:
    text = str(raw).strip()
    if not text:
        raise ValueError("协议消息不能为空")
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise ValueError("协议消息必须是对象")
    kind = str(payload.get("kind", "")).strip()
    if kind not in {"request", "response", "event"}:
        raise ValueError(f"不支持的协议消息 kind: {kind}")
    if str(payload.get("version", PROTOCOL_VERSION)).strip() != PROTOCOL_VERSION:
        raise ValueError(f"协议版本不匹配: {payload.get('version')}")
    return payload


__all__ = [
    "PROTOCOL_VERSION",
    "build_event",
    "build_request",
    "build_response",
    "decode_message",
    "encode_message",
]
