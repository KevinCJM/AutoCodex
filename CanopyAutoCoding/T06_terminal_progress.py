# -*- encoding: utf-8 -*-
"""
@File: T06_terminal_progress.py
@Modify Time: 2026/4/13
@Author: Kevin-Chen
@Descriptions: 通用终端单行动画与原位刷新
"""

from __future__ import annotations

import sys
import threading
from typing import Callable, TextIO


TERMINAL_SPINNER_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")


class SingleLineSpinnerMonitor:
    def __init__(
            self,
            *,
            frame_builder: Callable[[int], str],
            stream: TextIO | None = None,
            interval_sec: float = 0.2,
    ) -> None:
        self.frame_builder = frame_builder
        self.stream = stream or sys.stdout
        self.interval_sec = interval_sec
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._tick = 0
        self._started = False
        self._last_frame = ""
        self._last_line_width = 0
        self._isatty = bool(getattr(self.stream, "isatty", lambda: False)())

    def start(self) -> None:
        if self._started or not self._isatty:
            return
        self._started = True
        self.stream.write("\x1b[?25l")
        self.stream.flush()
        self._thread = threading.Thread(target=self._run_loop, name="single-line-spinner", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if not self._started:
            return
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        if self._last_line_width:
            self.stream.write("\r")
            self.stream.write(" " * self._last_line_width)
            self.stream.write("\r")
        self.stream.write("\x1b[?25h")
        self.stream.write("\n")
        self.stream.flush()
        self._started = False
        self._thread = None
        self._stop_event = threading.Event()

    def _display_line(self, line: str) -> None:
        width = len(line)
        padding = max(0, self._last_line_width - width)
        self.stream.write("\r")
        self.stream.write(line)
        if padding:
            self.stream.write(" " * padding)
        self.stream.flush()
        self._last_frame = line
        self._last_line_width = width

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            frame = self.frame_builder(self._tick)
            self._tick += 1
            if frame != self._last_frame:
                self._display_line(frame)
            if self._stop_event.wait(self.interval_sec):
                break
