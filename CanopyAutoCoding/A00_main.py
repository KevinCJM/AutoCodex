# -*- encoding: utf-8 -*-
"""
@File: A00_main.py
@Modify Time: 2026/4/13
@Author: Kevin-Chen
@Descriptions: 自动化开发流程总入口
"""

from __future__ import annotations

import argparse
import sys
from typing import Sequence

from A01_Routing_LayerPlanning import prompt_project_dir
from A01_Routing_LayerPlanning import main as routing_stage_main
from A02_RequirementsAnalysis import main as requirements_stage_main


UNIMPLEMENTED_STAGES = (
    "需求评审阶段（占位）",
    "详细设计阶段（占位）",
    "任务拆分阶段（占位）",
    "任务开发阶段（占位）",
    "测试阶段（占位）",
    "复合阶段（占位）",
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="A00 总调度入口：串联自动化开发各阶段")
    parser.add_argument("--project-dir", help="项目工作目录")
    parser.add_argument("--yes", action="store_true", help="传递给当前已实现阶段，跳过非关键确认")
    return parser


def build_stage_args(project_dir: str, *, auto_confirm: bool) -> list[str]:
    args = ["--project-dir", project_dir]
    if auto_confirm:
        args.append("--yes")
    return args


def render_remaining_stage_placeholders() -> str:
    lines = ["A00 总调度已完成当前已实现阶段。", "后续阶段状态:"]
    for stage in UNIMPLEMENTED_STAGES:
        lines.append(f"- {stage}")
    return "\n".join(lines)


def run_stage(stage_name: str, stage_main, argv: Sequence[str]) -> int:
    print(f"\n===== {stage_name} =====")
    return int(stage_main(list(argv)))


def clear_pending_tty_input() -> None:
    stdin = sys.stdin
    if not getattr(stdin, "isatty", lambda: False)():
        return
    try:
        import termios

        termios.tcflush(stdin.fileno(), termios.TCIFLUSH)
    except Exception:
        return


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    project_dir = str(args.project_dir).strip() if args.project_dir else prompt_project_dir("")
    stage_args = build_stage_args(project_dir, auto_confirm=bool(args.yes))

    routing_exit = run_stage("AGENT初始化阶段", routing_stage_main, stage_args)
    if routing_exit != 0:
        return routing_exit

    clear_pending_tty_input()
    requirements_exit = run_stage("需求分析阶段 / 需求录入", requirements_stage_main, stage_args)
    if requirements_exit != 0:
        return requirements_exit

    print()
    print(render_remaining_stage_placeholders())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
