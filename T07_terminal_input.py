# -*- encoding: utf-8 -*-
"""
@File: T07_terminal_input.py
@Modify Time: 2026/4/16
@Author: Kevin-Chen
@Descriptions: 统一的人类终端输入组件
"""

from __future__ import annotations

import os
import re
import select
import shutil
import subprocess
import sys
import tempfile
import termios
import tty
import unicodedata
from pathlib import Path
from typing import Sequence

try:
    import readline  # type: ignore
except Exception:  # noqa: BLE001
    readline = None


KEY_SUBMIT = "submit"
KEY_NEWLINE = "newline"
KEY_LEFT = "left"
KEY_RIGHT = "right"
KEY_UP = "up"
KEY_DOWN = "down"
KEY_HOME = "home"
KEY_END = "end"
KEY_BACKSPACE = "backspace"
KEY_DELETE = "delete"
KEY_INTERRUPT = "interrupt"
KEY_UNKNOWN = "unknown"
KEY_TEXT = "text"

SHIFT_ENTER_SEQUENCES = {
    b"\x1b[13;2u",
    b"\x1b[27;2;13~",
    b"\x1b[13;2~",
}


SelectionOption = tuple[str, str]
MULTILINE_MAX_VISIBLE_LINES = 5
INLINE_EDITOR_RESERVED_LINES = 7
INLINE_SELECT_MAX_VISIBLE_OPTIONS = 5
INLINE_SELECT_RESERVED_LINES = 8


def _supports_real_tty() -> bool:
    stdin = sys.stdin
    stdout = sys.stdout
    return bool(
        getattr(stdin, "isatty", lambda: False)()
        and getattr(stdout, "isatty", lambda: False)()
        and hasattr(stdin, "fileno")
        and hasattr(stdout, "fileno")
    )


def _supports_ansi_multiline_input() -> bool:
    term_value = str(os.environ.get("TERM", "")).strip().lower()
    return _supports_real_tty() and term_value not in {"", "dumb"}


def _readline_input(prompt_text: str, default: str = "") -> str:
    if readline is None or not _supports_real_tty():
        return input(prompt_text)

    startup_hook = None
    if default:
        def startup_hook() -> None:
            readline.insert_text(default)
            try:
                readline.redisplay()
            except Exception:  # noqa: BLE001
                pass

    get_startup_hook = getattr(readline, "get_startup_hook", None)
    set_startup_hook = getattr(readline, "set_startup_hook", None)
    if not callable(set_startup_hook):
        return input(prompt_text)

    previous_hook = get_startup_hook() if callable(get_startup_hook) else None
    try:
        set_startup_hook(startup_hook)
        return input(prompt_text)
    finally:
        set_startup_hook(previous_hook)


def prompt_with_default(prompt_text: str, default: str = "", allow_empty: bool = False) -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        value = _readline_input(f"{prompt_text}{suffix}: ", default).strip()
        if value:
            return value
        if default:
            return default
        if allow_empty:
            return ""
        print("输入不能为空，请重试。")


def _normalize_selection_options(options: Sequence[str | SelectionOption]) -> list[SelectionOption]:
    normalized: list[SelectionOption] = []
    for item in options:
        if isinstance(item, tuple):
            value, label = item
        else:
            value = str(item)
            label = str(item)
        normalized.append((str(value), str(label)))
    if not normalized:
        raise ValueError("选项不能为空")
    return normalized


def _selection_default_index(options: Sequence[SelectionOption], default_value: str | None = None) -> int:
    if default_value is None:
        return 0
    default_text = str(default_value)
    for index, (value, _) in enumerate(options):
        if value == default_text:
            return index
    return 0


def _render_select_frame(
    *,
    title: str,
    options: Sequence[SelectionOption],
    selected_index: int,
    footer: str,
) -> None:
    width, _ = shutil.get_terminal_size((100, 30))
    content_width = max(20, width - 1)
    display_lines = min(max(1, len(options)), INLINE_SELECT_MAX_VISIBLE_OPTIONS)
    start_index = max(0, min(selected_index - display_lines + 1, max(0, len(options) - display_lines)))
    visible_options = options[start_index:start_index + display_lines]

    rendered_lines: list[str] = [
        _truncate_plain_line(title, content_width),
        _truncate_plain_line("↑/↓ 选择 | Enter 确认 | 数字快捷选择 | Ctrl+C 取消", content_width),
    ]
    for offset in range(INLINE_SELECT_MAX_VISIBLE_OPTIONS):
        if offset < len(visible_options):
            actual_index = start_index + offset
            _, label = visible_options[offset]
            prefix = "›" if actual_index == selected_index else " "
            rendered_lines.append(_truncate_plain_line(f"{prefix} {actual_index + 1}. {label}", content_width))
        else:
            rendered_lines.append(" " * content_width)
    rendered_lines.append(_truncate_plain_line(footer or "", content_width))
    _render_inline_region(rendered_lines, reserved_lines=INLINE_SELECT_RESERVED_LINES)


def _collect_select_input_ansi(
    *,
    title: str,
    options: Sequence[SelectionOption],
    default_index: int = 0,
) -> str:
    fd = sys.stdin.fileno()
    original_settings = termios.tcgetattr(fd)
    selected_index = max(0, min(default_index, len(options) - 1))
    footer = ""
    try:
        tty.setraw(fd)
        _begin_inline_region(INLINE_SELECT_RESERVED_LINES)
        while True:
            _render_select_frame(
                title=title,
                options=options,
                selected_index=selected_index,
                footer=footer,
            )
            footer = ""
            key_name, key_value = _decode_key_bytes(_read_key_bytes(fd))
            if key_name == KEY_SUBMIT:
                return options[selected_index][0]
            if key_name == KEY_UP:
                selected_index = max(0, selected_index - 1)
                continue
            if key_name == KEY_DOWN:
                selected_index = min(len(options) - 1, selected_index + 1)
                continue
            if key_name == KEY_HOME:
                selected_index = 0
                continue
            if key_name == KEY_END:
                selected_index = len(options) - 1
                continue
            if key_name == KEY_INTERRUPT:
                raise KeyboardInterrupt
            if key_name == KEY_TEXT and key_value.isdigit():
                candidate_index = int(key_value) - 1
                if 0 <= candidate_index < len(options):
                    selected_index = candidate_index
                else:
                    footer = f"不支持的序号: {key_value}"
                continue
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, original_settings)
        _finish_inline_region(INLINE_SELECT_RESERVED_LINES)


def prompt_select_option(
    *,
    title: str,
    options: Sequence[str | SelectionOption],
    default_value: str | None = None,
    prompt_text: str = "请选择",
) -> str:
    normalized = _normalize_selection_options(options)
    default_index = _selection_default_index(normalized, default_value)
    if _supports_ansi_multiline_input():
        try:
            return _collect_select_input_ansi(
                title=title,
                options=normalized,
                default_index=default_index,
            )
        except Exception:  # noqa: BLE001
            pass

    print(title)
    for index, (_, label) in enumerate(normalized, start=1):
        print(f"  {index}. {label}")
    default_prompt = str(default_index + 1)
    while True:
        candidate = prompt_with_default(prompt_text, default_prompt).strip()
        if candidate.isdigit():
            index = int(candidate)
            if 1 <= index <= len(normalized):
                return normalized[index - 1][0]
        for value, label in normalized:
            if candidate == value or candidate == label:
                return value
        print(f"不支持的选择: {candidate}")


def _preferred_editor() -> list[str]:
    editor = str(os.environ.get("TMUX_EDITOR") or os.environ.get("EDITOR") or "").strip()
    if editor:
        return [editor]
    for candidate in ("nano", "vim", "vi"):
        resolved = shutil.which(candidate)
        if resolved:
            return [resolved]
    return []


def _render_editor_help(editor_cmd: list[str]) -> str:
    executable = Path(editor_cmd[0]).name.lower()
    if executable == "nano":
        return "已打开终端编辑器（nano）。保存: Ctrl+O，确认回车，退出: Ctrl+X。"
    if executable in {"vim", "vi"}:
        return "已打开终端编辑器（vim/vi）。保存退出可用 :wq。"
    return f"已打开终端编辑器（{editor_cmd[0]}）。保存并退出后继续。"


def _split_lines(text: str) -> list[str]:
    lines = str(text or "").split("\n")
    return lines if lines else [""]


def _join_lines(lines: list[str]) -> str:
    return "\n".join(lines)


def _char_display_width(char: str) -> int:
    if not char:
        return 0
    if unicodedata.combining(char):
        return 0
    if unicodedata.east_asian_width(char) in {"W", "F"}:
        return 2
    return 1


def _text_display_width(text: str) -> int:
    return sum(_char_display_width(char) for char in str(text or ""))


def _display_width_until(text: str, logical_col: int) -> int:
    width = 0
    for index, char in enumerate(str(text or "")):
        if index >= logical_col:
            break
        width += _char_display_width(char)
    return width


def _slice_text_by_display_width(text: str, start_col: int, max_width: int) -> str:
    if max_width <= 0:
        return ""
    visible: list[str] = []
    current_width = 0
    consumed_width = 0
    for char in str(text or ""):
        char_width = _char_display_width(char)
        next_width = current_width + char_width
        if next_width <= start_col:
            current_width = next_width
            continue
        if consumed_width + char_width > max_width:
            break
        visible.append(char)
        consumed_width += char_width
        current_width = next_width
    result = "".join(visible)
    padding = max(0, max_width - _text_display_width(result))
    return result + (" " * padding)


def _truncate_plain_line(text: str, max_width: int) -> str:
    clipped = _slice_text_by_display_width(text, 0, max_width)
    width = _text_display_width(clipped)
    if width < max_width:
        clipped += " " * (max_width - width)
    return clipped


def _horizontal_view_offset(line: str, logical_col: int, viewport_width: int) -> int:
    if viewport_width <= 0:
        return 0
    cursor_width = _display_width_until(line, logical_col)
    total_width = _text_display_width(line)
    if total_width <= viewport_width or cursor_width < viewport_width:
        return 0
    return max(0, cursor_width - viewport_width + 1)


def _insert_text(lines: list[str], row: int, col: int, text: str) -> tuple[int, int]:
    current = lines[row]
    lines[row] = current[:col] + text + current[col:]
    return row, col + len(text)


def _insert_newline(lines: list[str], row: int, col: int) -> tuple[int, int]:
    current = lines[row]
    lines[row] = current[:col]
    lines.insert(row + 1, current[col:])
    return row + 1, 0


def _backspace(lines: list[str], row: int, col: int) -> tuple[int, int]:
    if col > 0:
        current = lines[row]
        lines[row] = current[:col - 1] + current[col:]
        return row, col - 1
    if row == 0:
        return row, col
    previous = lines[row - 1]
    current = lines[row]
    new_col = len(previous)
    lines[row - 1] = previous + current
    del lines[row]
    return row - 1, new_col


def _delete_forward(lines: list[str], row: int, col: int) -> tuple[int, int]:
    current = lines[row]
    if col < len(current):
        lines[row] = current[:col] + current[col + 1:]
        return row, col
    if row >= len(lines) - 1:
        return row, col
    lines[row] = current + lines[row + 1]
    del lines[row + 1]
    return row, col


def _move_left(lines: list[str], row: int, col: int) -> tuple[int, int]:
    if col > 0:
        return row, col - 1
    if row == 0:
        return row, col
    return row - 1, len(lines[row - 1])


def _move_right(lines: list[str], row: int, col: int) -> tuple[int, int]:
    if col < len(lines[row]):
        return row, col + 1
    if row >= len(lines) - 1:
        return row, col
    return row + 1, 0


def _move_up(lines: list[str], row: int, col: int) -> tuple[int, int]:
    if row == 0:
        return row, col
    return row - 1, min(col, len(lines[row - 1]))


def _move_down(lines: list[str], row: int, col: int) -> tuple[int, int]:
    if row >= len(lines) - 1:
        return row, col
    return row + 1, min(col, len(lines[row + 1]))


def _decode_key_bytes(key_bytes: bytes) -> tuple[str, str]:
    if not key_bytes:
        return KEY_UNKNOWN, ""
    if key_bytes == b"\r":
        return KEY_SUBMIT, ""
    if key_bytes == b"\n":
        return KEY_NEWLINE, ""
    if key_bytes in SHIFT_ENTER_SEQUENCES:
        return KEY_NEWLINE, ""
    if key_bytes in {b"\x7f", b"\x08"}:
        return KEY_BACKSPACE, ""
    if key_bytes == b"\x03":
        return KEY_INTERRUPT, ""
    if key_bytes in {b"\x1b[H", b"\x1bOH", b"\x01"}:
        return KEY_HOME, ""
    if key_bytes in {b"\x1b[F", b"\x1bOF", b"\x05"}:
        return KEY_END, ""
    if key_bytes == b"\x1b[3~":
        return KEY_DELETE, ""
    if key_bytes.startswith(b"\x1b"):
        try:
            escape_text = key_bytes.decode("ascii", errors="ignore")
        except Exception:  # noqa: BLE001
            escape_text = ""
        if re.fullmatch(r"\x1b\[[0-9;]*A", escape_text) or re.fullmatch(r"\x1bO[A-Z]", escape_text) and escape_text.endswith("A"):
            return KEY_UP, ""
        if re.fullmatch(r"\x1b\[[0-9;]*B", escape_text) or re.fullmatch(r"\x1bO[A-Z]", escape_text) and escape_text.endswith("B"):
            return KEY_DOWN, ""
        if re.fullmatch(r"\x1b\[[0-9;]*C", escape_text) or re.fullmatch(r"\x1bO[A-Z]", escape_text) and escape_text.endswith("C"):
            return KEY_RIGHT, ""
        if re.fullmatch(r"\x1b\[[0-9;]*D", escape_text) or re.fullmatch(r"\x1bO[A-Z]", escape_text) and escape_text.endswith("D"):
            return KEY_LEFT, ""
        if re.fullmatch(r"\x1b\[[0-9;]*H", escape_text) or escape_text.endswith("H"):
            return KEY_HOME, ""
        if re.fullmatch(r"\x1b\[[0-9;]*F", escape_text) or escape_text.endswith("F"):
            return KEY_END, ""
    try:
        text = key_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return KEY_UNKNOWN, ""
    if text and text.isprintable():
        return KEY_TEXT, text
    return KEY_UNKNOWN, text


def _read_more_escape_bytes(fd: int, *, timeout_sec: float = 0.08) -> bytes:
    chunks: list[bytes] = []
    while True:
        ready, _, _ = select.select([fd], [], [], timeout_sec)
        if not ready:
            break
        chunk = os.read(fd, 16)
        if not chunk:
            break
        chunks.append(chunk)
    return b"".join(chunks)


def _utf8_expected_length(first_byte: int) -> int:
    if first_byte & 0b11110000 == 0b11110000:
        return 4
    if first_byte & 0b11100000 == 0b11100000:
        return 3
    if first_byte & 0b11000000 == 0b11000000:
        return 2
    return 1


def _read_key_bytes(fd: int) -> bytes:
    first = os.read(fd, 1)
    if not first:
        return b""
    if first == b"\x1b":
        return first + _read_more_escape_bytes(fd)
    first_value = first[0]
    expected_length = _utf8_expected_length(first_value)
    if expected_length <= 1:
        return first
    remainder = b""
    while len(remainder) < expected_length - 1:
        remainder += os.read(fd, expected_length - 1 - len(remainder))
    return first + remainder


def _render_multiline_frame(
    *,
    title: str,
    lines: list[str],
    row: int,
    col: int,
    footer: str,
) -> None:
    width, _ = shutil.get_terminal_size((100, 30))
    content_width = max(20, width - 1)
    display_lines = min(max(1, len(lines)), MULTILINE_MAX_VISIBLE_LINES)
    start_row = max(0, row - display_lines + 1)
    visible_lines = lines[start_row:start_row + display_lines]
    horizontal_offset = _horizontal_view_offset(lines[row], col, content_width)

    rendered_lines: list[str] = [
        _truncate_plain_line(title, content_width),
        _truncate_plain_line("Enter 提交 | Shift+Enter 换行(若终端支持) | Ctrl+J 换行保底 | Ctrl+C 取消", content_width),
    ]
    for index in range(MULTILINE_MAX_VISIBLE_LINES):
        if index < len(visible_lines):
            rendered_lines.append(_slice_text_by_display_width(visible_lines[index], horizontal_offset, content_width))
        else:
            rendered_lines.append(" " * content_width)
    rendered_lines.append(_truncate_plain_line(footer or "", content_width))

    cursor_row = 3 + (row - start_row)
    cursor_col = _display_width_until(lines[row], col) - horizontal_offset + 1
    cursor_col = max(1, min(cursor_col, content_width))
    _render_inline_region(
        rendered_lines,
        reserved_lines=INLINE_EDITOR_RESERVED_LINES,
        cursor_row=cursor_row,
        cursor_col=cursor_col,
    )


def _collect_multiline_input_ansi(
    *,
    title: str,
    empty_retry_message: str,
    initial_text: str = "",
) -> str:
    fd = sys.stdin.fileno()
    original_settings = termios.tcgetattr(fd)
    lines = _split_lines(initial_text)
    row = len(lines) - 1
    col = len(lines[row])
    footer = ""
    try:
        tty.setraw(fd)
        _begin_inline_region(INLINE_EDITOR_RESERVED_LINES)
        while True:
            _render_multiline_frame(
                title=title,
                lines=lines,
                row=row,
                col=col,
                footer=footer,
            )
            footer = ""
            key_name, key_value = _decode_key_bytes(_read_key_bytes(fd))
            if key_name == KEY_SUBMIT:
                text = _join_lines(lines).strip()
                if text:
                    return text
                footer = empty_retry_message
                continue
            if key_name == KEY_NEWLINE:
                row, col = _insert_newline(lines, row, col)
                continue
            if key_name == KEY_LEFT:
                row, col = _move_left(lines, row, col)
                continue
            if key_name == KEY_RIGHT:
                row, col = _move_right(lines, row, col)
                continue
            if key_name == KEY_UP:
                row, col = _move_up(lines, row, col)
                continue
            if key_name == KEY_DOWN:
                row, col = _move_down(lines, row, col)
                continue
            if key_name == KEY_HOME:
                col = 0
                continue
            if key_name == KEY_END:
                col = len(lines[row])
                continue
            if key_name == KEY_BACKSPACE:
                row, col = _backspace(lines, row, col)
                continue
            if key_name == KEY_DELETE:
                row, col = _delete_forward(lines, row, col)
                continue
            if key_name == KEY_INTERRUPT:
                raise KeyboardInterrupt
            if key_name == KEY_TEXT:
                row, col = _insert_text(lines, row, col, key_value)
                continue
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, original_settings)
        _finish_inline_region(INLINE_EDITOR_RESERVED_LINES)


def _begin_inline_region(reserved_lines: int) -> None:
    if reserved_lines <= 0:
        return
    sys.stdout.write("\r\n" * reserved_lines)
    sys.stdout.write(f"\x1b[{reserved_lines}A\r")
    sys.stdout.write("\x1b[s")
    sys.stdout.write("\x1b[?25h")
    sys.stdout.flush()


def _render_inline_region(
    rendered_lines: Sequence[str],
    *,
    reserved_lines: int,
    cursor_row: int | None = None,
    cursor_col: int | None = None,
) -> None:
    sys.stdout.write("\x1b[u")
    for index in range(reserved_lines):
        line = rendered_lines[index] if index < len(rendered_lines) else ""
        sys.stdout.write("\x1b[2K")
        sys.stdout.write(line)
        if index < reserved_lines - 1:
            sys.stdout.write("\r\n")
    sys.stdout.write("\x1b[u")
    if cursor_row is None or cursor_col is None:
        if reserved_lines > 1:
            sys.stdout.write(f"\x1b[{reserved_lines - 1}B")
        sys.stdout.write("\r")
    else:
        if cursor_row > 1:
            sys.stdout.write(f"\x1b[{cursor_row - 1}B")
        if cursor_col > 1:
            sys.stdout.write(f"\x1b[{cursor_col - 1}C")
    sys.stdout.flush()


def _finish_inline_region(reserved_lines: int) -> None:
    if reserved_lines <= 0:
        return
    sys.stdout.write("\x1b[u")
    if reserved_lines > 1:
        sys.stdout.write(f"\x1b[{reserved_lines - 1}B")
    sys.stdout.write("\r")
    sys.stdout.write("\x1b[?25h")
    sys.stdout.flush()


def collect_multiline_input(
    *,
    title: str,
    empty_retry_message: str,
    initial_text: str = "",
) -> str:
    if _supports_ansi_multiline_input():
        try:
            return _collect_multiline_input_ansi(
                title=title,
                empty_retry_message=empty_retry_message,
                initial_text=initial_text,
            )
        except Exception:  # noqa: BLE001
            pass

    editor_cmd = _preferred_editor() if _supports_real_tty() else []
    if editor_cmd:
        while True:
            temp_path = Path(tempfile.mkstemp(prefix="tmux_input_", suffix=".md")[1]).resolve()
            try:
                temp_path.write_text(str(initial_text or ""), encoding="utf-8")
                print(title)
                print(_render_editor_help(editor_cmd))
                result = subprocess.run([*editor_cmd, str(temp_path)], check=False)
                if result.returncode != 0:
                    raise RuntimeError(f"终端编辑器退出码异常: {result.returncode}")
                text = temp_path.read_text(encoding="utf-8").strip()
                if text:
                    return text
                print(empty_retry_message)
            finally:
                if temp_path.exists():
                    temp_path.unlink()

    print(f"{title}，单独一行输入 EOF 结束:")
    while True:
        lines: list[str] = []
        while True:
            line = input()
            if line == "EOF":
                break
            lines.append(line)
        text = "\n".join(lines).strip()
        if text:
            return text
        print(empty_retry_message)
