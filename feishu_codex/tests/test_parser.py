from __future__ import annotations

import json
from types import SimpleNamespace

from feishu_codex_bridge.models import BackendKind, CommandKind
from feishu_codex_bridge.parser import build_envelope, parse_message, parse_messages


def make_event(
    *,
    text: str,
    chat_type: str = "p2p",
    mentions: list[object] | None = None,
    message_type: str = "text",
):
    return SimpleNamespace(
        event=SimpleNamespace(
            sender=SimpleNamespace(
                sender_id=SimpleNamespace(user_id="ou-user", open_id="ou-open", union_id="on-union"),
                sender_type="user",
                tenant_key="tenant",
            ),
            message=SimpleNamespace(
                message_id="msg-1",
                chat_id="chat-1",
                chat_type=chat_type,
                message_type=message_type,
                content=json.dumps({"text": text}, ensure_ascii=False),
                mentions=mentions or [],
            ),
        )
    )


def make_mention(open_id: str):
    return SimpleNamespace(id=SimpleNamespace(open_id=open_id))


def test_parse_new_exec_command():
    envelope = build_envelope(make_event(text="/new exec /tmp"))
    parsed = parse_message(envelope)
    assert parsed.kind == CommandKind.NEW
    assert parsed.backend == BackendKind.EXEC
    assert parsed.workdir == "/tmp"


def test_parse_resume_tmux_command():
    envelope = build_envelope(make_event(text='/resume tmux sess-1 "/tmp/my dir"'))
    parsed = parse_message(envelope)
    assert parsed.kind == CommandKind.RESUME
    assert parsed.backend == BackendKind.TMUX
    assert parsed.external_session_id == "sess-1"
    assert parsed.workdir == "/tmp/my dir"


def test_parse_model_command():
    envelope = build_envelope(make_event(text="/model gpt-5.4-mini"))
    parsed = parse_message(envelope)
    assert parsed.kind == CommandKind.MODEL
    assert parsed.model_name == "gpt-5.4-mini"


def test_parse_think_command():
    envelope = build_envelope(make_event(text="/think high"))
    parsed = parse_message(envelope)
    assert parsed.kind == CommandKind.THINK
    assert parsed.reasoning_effort == "high"


def test_parse_multiline_slash_commands():
    envelope = build_envelope(make_event(text="/model gpt-5.4-mini\n/think high"))
    parsed_messages = parse_messages(envelope)
    assert [item.kind for item in parsed_messages] == [CommandKind.MODEL, CommandKind.THINK]
    assert parsed_messages[0].model_name == "gpt-5.4-mini"
    assert parsed_messages[1].reasoning_effort == "high"


def test_parse_multiline_mixed_command_and_text_is_rejected():
    envelope = build_envelope(make_event(text="/model gpt-5.4-mini\nhello"))
    parsed_messages = parse_messages(envelope)
    assert len(parsed_messages) == 1
    assert parsed_messages[0].kind == CommandKind.UNSUPPORTED
    assert "每一行都是独立的 slash 命令" in parsed_messages[0].error


def test_group_message_without_at_is_ignored():
    envelope = build_envelope(make_event(text="hello", chat_type="group"))
    parsed = parse_message(envelope)
    assert parsed.kind == CommandKind.IGNORE


def test_group_message_with_at_strips_mentions():
    envelope = build_envelope(
        make_event(
            text='<at user_id="ou-bot">MyCodex</at> /status',
            chat_type="group",
            mentions=[make_mention("ou-bot")],
        )
    )
    assert envelope.cleaned_text == "/status"
    parsed = parse_message(envelope)
    assert parsed.kind == CommandKind.STATUS


def test_non_text_message_is_rejected():
    envelope = build_envelope(make_event(text="{}", message_type="image"))
    parsed = parse_message(envelope)
    assert parsed.kind == CommandKind.UNSUPPORTED
    assert "文本消息" in parsed.error


def test_post_message_is_ignored():
    envelope = build_envelope(make_event(text="/new tmux /tmp", message_type="post"))
    parsed = parse_message(envelope)
    assert parsed.kind == CommandKind.IGNORE
