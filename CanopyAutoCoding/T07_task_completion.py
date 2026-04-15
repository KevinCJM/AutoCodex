# -*- encoding: utf-8 -*-
"""
@File: T07_task_completion.py
@Modify Time: 2026/4/15
@Author: Kevin-Chen
@Descriptions: task completion helper for tmux agent runtime
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from T04_common_prompt import TASK_DONE_MARKER


def write_task_status(path: str | Path, *, status: str) -> Path:
    target = Path(path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target.with_suffix(target.suffix + ".tmp")
    tmp_path.write_text(json.dumps({"status": status}, ensure_ascii=False), encoding="utf-8")
    tmp_path.replace(target)
    return target


def complete_task(
        *,
        task_status_path: str | Path,
        status: str = "done",
        done_marker: str = TASK_DONE_MARKER,
        stream = None,
) -> Path:
    _ = done_marker
    _ = stream
    return write_task_status(task_status_path, status=status)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Mark tmux agent task completion")
    parser.add_argument("--task-status-path", required=True)
    parser.add_argument("--status", default="done")
    parser.add_argument("--done-marker", default=TASK_DONE_MARKER)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    complete_task(
        task_status_path=args.task_status_path,
        status=args.status,
        done_marker=args.done_marker,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
