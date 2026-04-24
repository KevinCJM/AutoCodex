from __future__ import annotations

import io
import unittest
from unittest.mock import patch

from T07_terminal_input import (
    KEY_BACKSPACE,
    KEY_DELETE,
    KEY_DOWN,
    KEY_END,
    KEY_HOME,
    KEY_INTERRUPT,
    KEY_LEFT,
    KEY_NEWLINE,
    KEY_RIGHT,
    KEY_SUBMIT,
    KEY_TEXT,
    KEY_UP,
    _backspace,
    _begin_inline_region,
    _decode_key_bytes,
    _delete_forward,
    _render_inline_region,
    _insert_newline,
    _insert_text,
    _join_lines,
    _move_down,
    _move_left,
    _move_right,
    _move_up,
    _readline_input,
    _split_lines,
    prompt_select_option,
)


class TerminalInputTests(unittest.TestCase):
    def test_prompt_select_option_falls_back_to_numeric_choice(self):
        with patch("T07_terminal_input._supports_ansi_multiline_input", return_value=False), patch(
            "builtins.input",
            return_value="2",
        ):
            value = prompt_select_option(
                title="可选厂商:",
                options=(("codex", "codex"), ("gemini", "gemini")),
                default_value="codex",
                prompt_text="选择厂商",
            )
        self.assertEqual(value, "gemini")

    def test_readline_input_tolerates_missing_get_startup_hook(self):
        class _FakeReadline:
            def __init__(self):
                self.hooks = []

            def insert_text(self, text):
                return None

            def redisplay(self):
                return None

            def set_startup_hook(self, hook):
                self.hooks.append(hook)

        fake_readline = _FakeReadline()
        with patch("T07_terminal_input.readline", fake_readline), patch(
            "T07_terminal_input._supports_real_tty",
            return_value=True,
        ), patch("builtins.input", return_value="1"):
            value = _readline_input("选择", "2")
        self.assertEqual(value, "1")
        self.assertEqual(len(fake_readline.hooks), 2)
        self.assertIsNone(fake_readline.hooks[-1])

    def test_decode_key_bytes_maps_common_control_keys(self):
        self.assertEqual(_decode_key_bytes(b"\r"), (KEY_SUBMIT, ""))
        self.assertEqual(_decode_key_bytes(b"\n"), (KEY_NEWLINE, ""))
        self.assertEqual(_decode_key_bytes(b"\x1b[13;2u"), (KEY_NEWLINE, ""))
        self.assertEqual(_decode_key_bytes(b"\x1b[D"), (KEY_LEFT, ""))
        self.assertEqual(_decode_key_bytes(b"\x1b[C"), (KEY_RIGHT, ""))
        self.assertEqual(_decode_key_bytes(b"\x1b[A"), (KEY_UP, ""))
        self.assertEqual(_decode_key_bytes(b"\x1b[B"), (KEY_DOWN, ""))
        self.assertEqual(_decode_key_bytes(b"\x1b[1;2A"), (KEY_UP, ""))
        self.assertEqual(_decode_key_bytes(b"\x1b[1;2B"), (KEY_DOWN, ""))
        self.assertEqual(_decode_key_bytes(b"\x1bOA"), (KEY_UP, ""))
        self.assertEqual(_decode_key_bytes(b"\x1bOB"), (KEY_DOWN, ""))
        self.assertEqual(_decode_key_bytes(b"\x1b[H"), (KEY_HOME, ""))
        self.assertEqual(_decode_key_bytes(b"\x1b[F"), (KEY_END, ""))
        self.assertEqual(_decode_key_bytes(b"\x7f"), (KEY_BACKSPACE, ""))
        self.assertEqual(_decode_key_bytes(b"\x1b[3~"), (KEY_DELETE, ""))
        self.assertEqual(_decode_key_bytes(b"\x03"), (KEY_INTERRUPT, ""))
        self.assertEqual(_decode_key_bytes("中".encode("utf-8")), (KEY_TEXT, "中"))

    def test_text_buffer_edit_operations(self):
        lines = _split_lines("abc")
        row, col = 0, 3
        row, col = _insert_text(lines, row, col, "d")
        self.assertEqual((_join_lines(lines), row, col), ("abcd", 0, 4))

        row, col = _insert_newline(lines, row, col)
        self.assertEqual((_join_lines(lines), row, col), ("abcd\n", 1, 0))

        row, col = _insert_text(lines, row, col, "xy")
        self.assertEqual(_join_lines(lines), "abcd\nxy")

        row, col = _move_up(lines, row, col)
        self.assertEqual((row, col), (0, 2))
        row, col = _move_down(lines, row, col)
        self.assertEqual((row, col), (1, 2))

        row, col = _move_left(lines, row, col)
        self.assertEqual((row, col), (1, 1))
        row, col = _move_right(lines, row, col)
        self.assertEqual((row, col), (1, 2))

        row, col = _backspace(lines, row, col)
        self.assertEqual((_join_lines(lines), row, col), ("abcd\nx", 1, 1))

        row, col = _delete_forward(lines, row, col)
        self.assertEqual((_join_lines(lines), row, col), ("abcd\nx", 1, 1))

    def test_backspace_at_line_start_merges_lines(self):
        lines = ["abc", "def"]
        row, col = _backspace(lines, 1, 0)
        self.assertEqual((_join_lines(lines), row, col), ("abcdef", 0, 3))

    def test_begin_inline_region_uses_crlf_for_reserved_lines(self):
        stdout = io.StringIO()
        with patch("sys.stdout", stdout):
            _begin_inline_region(3)
        output = stdout.getvalue()
        self.assertIn("\r\n\r\n\r\n", output)
        self.assertIn("\x1b[3A\r", output)

    def test_render_inline_region_uses_crlf_between_lines(self):
        stdout = io.StringIO()
        with patch("sys.stdout", stdout):
            _render_inline_region(
                ["line1", "line2", "line3"],
                reserved_lines=3,
                cursor_row=1,
                cursor_col=1,
            )
        output = stdout.getvalue()
        self.assertIn("line1\r\n", output)
        self.assertIn("line2\r\n", output)


if __name__ == "__main__":
    unittest.main()
