from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


def _resolve_lark_cli_command() -> list[str]:
    configured = os.environ.get("LARK_CLI_BIN", "").strip()
    if configured:
        return [configured]
    direct = shutil.which("lark-cli")
    if direct:
        return [direct]
    opencli = shutil.which("opencli")
    if opencli:
        return [opencli, "lark-cli"]
    raise SystemExit("lark-cli 未安装，且 opencli lark-cli 不可用。")


def _context_file_from_env() -> Path:
    value = os.environ.get("FEISHU_CODEX_CONTEXT_FILE", "").strip()
    if not value:
        raise SystemExit("缺少 FEISHU_CODEX_CONTEXT_FILE。")
    return Path(value).expanduser().resolve()


def _load_context(path: Path) -> dict[str, str]:
    if not path.exists():
        raise SystemExit(f"上下文文件不存在: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _read_text(args: argparse.Namespace, *, field_name: str) -> str:
    value = getattr(args, field_name, "") or ""
    if value:
        return str(value)
    if getattr(args, "stdin", False):
        return sys.stdin.read().strip()
    raise SystemExit("缺少文本内容。请使用 --text/--markdown 或 --stdin。")


def _run(command: list[str]) -> int:
    completed = subprocess.run(command, check=False, text=True)
    return completed.returncode


def _cmd_show_context(_: argparse.Namespace) -> int:
    context = _load_context(_context_file_from_env())
    print(json.dumps(context, ensure_ascii=False, indent=2))
    return 0


def _cmd_send_text(args: argparse.Namespace) -> int:
    context = _load_context(_context_file_from_env())
    chat_id = context.get("chat_id", "").strip()
    if not chat_id:
        raise SystemExit("当前上下文没有 chat_id。")
    text = _read_text(args, field_name="text")
    command = _resolve_lark_cli_command() + ["im", "+messages-send", "--chat-id", chat_id, "--text", text]
    return _run(command)


def _cmd_reply_text(args: argparse.Namespace) -> int:
    context = _load_context(_context_file_from_env())
    message_id = context.get("message_id", "").strip()
    if not message_id:
        raise SystemExit("当前上下文没有 message_id。")
    text = _read_text(args, field_name="text")
    command = _resolve_lark_cli_command() + ["im", "+messages-reply", "--message-id", message_id, "--text", text]
    if getattr(args, "reply_in_thread", False):
        command.append("--reply-in-thread")
    return _run(command)


def _cmd_send_markdown(args: argparse.Namespace) -> int:
    context = _load_context(_context_file_from_env())
    chat_id = context.get("chat_id", "").strip()
    if not chat_id:
        raise SystemExit("当前上下文没有 chat_id。")
    markdown = _read_text(args, field_name="markdown")
    command = _resolve_lark_cli_command() + [
        "im",
        "+messages-send",
        "--chat-id",
        chat_id,
        "--markdown",
        markdown,
    ]
    return _run(command)


def _cmd_reply_markdown(args: argparse.Namespace) -> int:
    context = _load_context(_context_file_from_env())
    message_id = context.get("message_id", "").strip()
    if not message_id:
        raise SystemExit("当前上下文没有 message_id。")
    markdown = _read_text(args, field_name="markdown")
    command = _resolve_lark_cli_command() + [
        "im",
        "+messages-reply",
        "--message-id",
        message_id,
        "--markdown",
        markdown,
    ]
    if getattr(args, "reply_in_thread", False):
        command.append("--reply-in-thread")
    return _run(command)


def _cmd_docs_create(args: argparse.Namespace) -> int:
    markdown = _read_text(args, field_name="markdown")
    command = _resolve_lark_cli_command() + ["docs", "+create", "--title", args.title, "--markdown", markdown]
    if args.folder_token:
        command.extend(["--folder-token", args.folder_token])
    if args.wiki_space:
        command.extend(["--wiki-space", args.wiki_space])
    if args.wiki_node:
        command.extend(["--wiki-node", args.wiki_node])
    return _run(command)


def _cmd_docs_update(args: argparse.Namespace) -> int:
    markdown = _read_text(args, field_name="markdown")
    command = _resolve_lark_cli_command() + [
        "docs",
        "+update",
        "--doc",
        args.doc,
        "--mode",
        args.mode,
        "--markdown",
        markdown,
    ]
    if args.new_title:
        command.extend(["--new-title", args.new_title])
    if args.selection_by_title:
        command.extend(["--selection-by-title", args.selection_by_title])
    if args.selection_with_ellipsis:
        command.extend(["--selection-with-ellipsis", args.selection_with_ellipsis])
    return _run(command)


def _cmd_drive_upload(args: argparse.Namespace) -> int:
    command = _resolve_lark_cli_command() + ["drive", "+upload", "--file", args.file]
    if args.name:
        command.extend(["--name", args.name])
    if args.folder_token:
        command.extend(["--folder-token", args.folder_token])
    return _run(command)


def _add_text_flags(parser: argparse.ArgumentParser, field_name: str) -> None:
    parser.add_argument(f"--{field_name}", default="", help=f"{field_name} content")
    parser.add_argument("--stdin", action="store_true", help="Read content from stdin")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Feishu CLI helper for Feishu Codex Bridge")
    subparsers = parser.add_subparsers(dest="command", required=True)

    show_context = subparsers.add_parser("show-context", help="Print current Feishu conversation context")
    show_context.set_defaults(func=_cmd_show_context)

    send_text = subparsers.add_parser("send-text", help="Send plain text to current chat")
    _add_text_flags(send_text, "text")
    send_text.set_defaults(func=_cmd_send_text)

    reply_text = subparsers.add_parser("reply-text", help="Reply with plain text to current message")
    _add_text_flags(reply_text, "text")
    reply_text.add_argument("--reply-in-thread", action="store_true", help="Reply in thread")
    reply_text.set_defaults(func=_cmd_reply_text)

    send_markdown = subparsers.add_parser("send-markdown", help="Send markdown to current chat")
    _add_text_flags(send_markdown, "markdown")
    send_markdown.set_defaults(func=_cmd_send_markdown)

    reply_markdown = subparsers.add_parser("reply-markdown", help="Reply with markdown to current message")
    _add_text_flags(reply_markdown, "markdown")
    reply_markdown.add_argument("--reply-in-thread", action="store_true", help="Reply in thread")
    reply_markdown.set_defaults(func=_cmd_reply_markdown)

    docs_create = subparsers.add_parser("docs-create", help="Create a Feishu document")
    docs_create.add_argument("--title", required=True, help="Document title")
    docs_create.add_argument("--folder-token", default="", help="Parent folder token")
    docs_create.add_argument("--wiki-space", default="", help="Wiki space id")
    docs_create.add_argument("--wiki-node", default="", help="Wiki node token")
    _add_text_flags(docs_create, "markdown")
    docs_create.add_argument("--stdin-markdown", action="store_true", help="Read markdown from stdin")
    docs_create.set_defaults(func=_cmd_docs_create)

    docs_update = subparsers.add_parser("docs-update", help="Update a Feishu document")
    docs_update.add_argument("--doc", required=True, help="Document URL or token")
    docs_update.add_argument("--mode", required=True, help="Update mode")
    docs_update.add_argument("--new-title", default="", help="New title")
    docs_update.add_argument("--selection-by-title", default="", help="Selection by title")
    docs_update.add_argument("--selection-with-ellipsis", default="", help="Selection by content locator")
    _add_text_flags(docs_update, "markdown")
    docs_update.add_argument("--stdin-markdown", action="store_true", help="Read markdown from stdin")
    docs_update.set_defaults(func=_cmd_docs_update)

    drive_upload = subparsers.add_parser("drive-upload", help="Upload a file to Feishu Drive")
    drive_upload.add_argument("--file", required=True, help="Absolute path to local file")
    drive_upload.add_argument("--name", default="", help="Remote file name")
    drive_upload.add_argument("--folder-token", default="", help="Target folder token")
    drive_upload.set_defaults(func=_cmd_drive_upload)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if getattr(args, "stdin_markdown", False):
        setattr(args, "stdin", True)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
