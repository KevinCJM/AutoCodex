# -*- encoding: utf-8 -*-
"""
@File: A00_main.py
@Modify Time: 2026/4/13
@Author: Kevin-Chen
@Descriptions: 自动化开发流程总入口
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Sequence

from A01_Routing_LayerPlanning import prompt_project_dir
from A01_Routing_LayerPlanning import run_routing_stage as routing_stage_main
from A02_RequirementIntake import run_requirement_intake_stage
from A03_RequirementsClarification import run_requirements_clarification_stage
from A04_RequirementsReview import run_requirements_review_stage
from T02_tmux_agents import cleanup_registered_tmux_workers
from T08_pre_development import (
    build_pre_development_task_record_path as shared_build_pre_development_task_record_path,
    build_pre_development_task_record_payload as shared_build_pre_development_task_record_payload,
    ensure_pre_development_task_record as shared_ensure_pre_development_task_record,
)
from T09_terminal_ops import clear_pending_tty_input
from T09_terminal_ops import maybe_launch_tui, message, notify_stage_action_changed


UNIMPLEMENTED_STAGES = (
    "详细设计阶段（占位）",
    "任务拆分阶段（占位）",
    "任务开发阶段（占位）",
    "测试阶段（占位）",
    "复合阶段（占位）",
)


def build_pre_development_task_record_payload() -> dict[str, dict[str, bool]]:
    return shared_build_pre_development_task_record_payload()


def build_pre_development_task_record_path(
        project_dir: str | Path,
        *,
        requirement_name: str,
) -> Path:
    return shared_build_pre_development_task_record_path(project_dir, requirement_name)


def ensure_pre_development_task_record(
        project_dir: str | Path,
        *,
        requirement_name: str,
) -> Path:
    return shared_ensure_pre_development_task_record(project_dir, requirement_name)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="A00 总调度入口：串联自动化开发各阶段")
    parser.add_argument("--project-dir", help="项目工作目录")
    parser.add_argument("--requirement-name", help="需求名称")
    parser.add_argument("--yes", action="store_true", help="传递给当前已实现阶段，跳过非关键确认")
    parser.add_argument("--no-tui", action="store_true", help="显式禁用 OpenTUI")
    parser.add_argument("--legacy-cli", action="store_true", help="使用旧版 Python CLI，不跳转 OpenTUI")
    return parser


def build_stage_args(project_dir: str, *, auto_confirm: bool, requirement_name: str = "") -> list[str]:
    args = ["--project-dir", project_dir]
    if str(requirement_name).strip():
        args.extend(["--requirement-name", str(requirement_name).strip()])
    if auto_confirm:
        args.append("--yes")
    return args


def render_remaining_stage_placeholders() -> str:
    lines = ["A00 总调度已完成当前已实现阶段。", "后续阶段状态:"]
    for stage in UNIMPLEMENTED_STAGES:
        lines.append(f"- {stage}")
    return "\n".join(lines)


def run_stage(stage_name: str, stage_main, argv: Sequence[str]) -> int:
    message(f"\n===== {stage_name} =====")
    return int(stage_main(list(argv)))


def main(argv: Sequence[str] | None = None) -> int:
    redirected, launch = maybe_launch_tui(argv, route="home", action="workflow.a00.start")
    if redirected:
        return int(launch)
    argv = list(launch)
    parser = build_parser()
    args = parser.parse_args(argv)
    project_dir = str(args.project_dir).strip() if args.project_dir else prompt_project_dir("")
    requirement_name = str(getattr(args, "requirement_name", "") or "").strip()
    stage_args = build_stage_args(
        project_dir,
        auto_confirm=bool(args.yes),
        requirement_name=requirement_name,
    )

    message("\n===== AGENT初始化阶段 =====")
    notify_stage_action_changed("stage.a01.start")
    routing_result = routing_stage_main(stage_args)
    routing_exit_code = int(getattr(routing_result, "exit_code", routing_result))
    if routing_exit_code != 0:
        return routing_exit_code

    try:
        if requirement_name:
            ensure_pre_development_task_record(project_dir, requirement_name=requirement_name)
    except Exception as error:  # noqa: BLE001
        message(f"创建开发前期任务记录失败: {error}")
        return 1

    clear_pending_tty_input()
    message("\n===== 需求录入阶段 =====")
    notify_stage_action_changed("stage.a02.start")
    try:
        intake_result = run_requirement_intake_stage(stage_args)
    except Exception as error:  # noqa: BLE001
        message(error)
        return 1

    clear_pending_tty_input()
    clarification_stage_args = build_stage_args(
        project_dir,
        auto_confirm=bool(args.yes),
        requirement_name=intake_result.requirement_name,
    )
    message("\n===== 需求澄清阶段 =====")
    notify_stage_action_changed("stage.a03.start")
    try:
        requirements_result = run_requirements_clarification_stage(
            clarification_stage_args,
            preserve_ba_worker=True,
        )
    except Exception as error:  # noqa: BLE001
        message(error)
        return 1

    clear_pending_tty_input()
    review_stage_args = build_stage_args(
        project_dir,
        auto_confirm=bool(args.yes),
        requirement_name=requirements_result.requirement_name,
    )
    message("\n===== 需求评审阶段 =====")
    notify_stage_action_changed("stage.a04.start")
    try:
        run_requirements_review_stage(review_stage_args, ba_handoff=requirements_result.ba_handoff)
    except Exception as error:  # noqa: BLE001
        message(error)
        return 1
    message()
    message(render_remaining_stage_placeholders())
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        cleaned_sessions = cleanup_registered_tmux_workers(reason="keyboard_interrupt")
        if cleaned_sessions:
            message(f"\n已清理 tmux 会话: {', '.join(cleaned_sessions)}")
        raise SystemExit(130)
