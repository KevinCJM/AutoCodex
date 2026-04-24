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

from canopy_core.runtime.tmux_runtime import cleanup_registered_tmux_workers
from canopy_core.stage_kernel.requirement_intake import run_requirement_intake_stage
from canopy_core.stage_kernel.requirements_clarification import run_requirements_clarification_stage
from canopy_core.stage_kernel.detailed_design import run_detailed_design_stage
from canopy_core.stage_kernel.overall_review import run_overall_review_stage
from canopy_core.stage_kernel.requirements_review import run_requirements_review_stage
from canopy_core.stage_kernel.routing_init import prompt_project_dir
from canopy_core.stage_kernel.routing_init import run_routing_stage as routing_stage_main
from canopy_core.stage_kernel.development import cleanup_stale_development_runtime_state, run_development_stage
from canopy_core.stage_kernel.requirement_concurrency import requirement_concurrency_lock
from canopy_core.stage_kernel.task_split import run_task_split_stage
from T08_pre_development import (
    build_pre_development_task_record_path as shared_build_pre_development_task_record_path,
    build_pre_development_task_record_payload as shared_build_pre_development_task_record_payload,
    ensure_pre_development_task_record as shared_ensure_pre_development_task_record,
)
from T09_terminal_ops import clear_pending_tty_input
from T09_terminal_ops import BridgeTerminalUI, get_terminal_ui, maybe_launch_tui, message, notify_stage_action_changed


UNIMPLEMENTED_STAGES = (
    "测试阶段（功能测试 + 全面回归，占位）",
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
    parser.add_argument("--requirements-review-max-rounds", default="", help="需求评审最多重试几轮；传 infinite 表示不设上限")
    parser.add_argument("--detailed-design-review-max-rounds", default="", help="详细设计评审最多重试几轮；传 infinite 表示不设上限")
    parser.add_argument("--task-split-review-max-rounds", default="", help="任务拆分评审最多重试几轮；传 infinite 表示不设上限")
    parser.add_argument("--development-review-max-rounds", default="", help="任务开发评审最多重试几轮；传 infinite 表示不设上限")
    parser.add_argument("--yes", action="store_true", help="传递给当前已实现阶段，跳过非关键确认")
    parser.add_argument("--no-tui", action="store_true", help="显式禁用 OpenTUI")
    parser.add_argument("--legacy-cli", action="store_true", help="使用旧版 Python CLI，不跳转 OpenTUI")
    return parser


def build_stage_args(
        project_dir: str,
        *,
        auto_confirm: bool,
        requirement_name: str = "",
        review_max_rounds: str = "",
) -> list[str]:
    args = ["--project-dir", project_dir]
    if str(requirement_name).strip():
        args.extend(["--requirement-name", str(requirement_name).strip()])
    if str(review_max_rounds).strip():
        args.extend(["--review-max-rounds", str(review_max_rounds).strip()])
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


def _bridge_terminal_active() -> bool:
    return isinstance(get_terminal_ui(), BridgeTerminalUI)


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
        if _bridge_terminal_active():
            raise RuntimeError(f"创建开发前期任务记录失败: {error}") from error
        message(f"创建开发前期任务记录失败: {error}")
        return 1

    clear_pending_tty_input()
    message("\n===== 需求录入阶段 =====")
    notify_stage_action_changed("stage.a02.start")
    try:
        intake_result = run_requirement_intake_stage(stage_args)
    except Exception as error:  # noqa: BLE001
        if _bridge_terminal_active():
            raise
        message(error)
        return 1

    workflow_requirement_name = str(getattr(intake_result, "requirement_name", "") or "").strip()
    lock_context = requirement_concurrency_lock(
        project_dir,
        workflow_requirement_name,
        action="workflow.a00.start",
    )
    lock_context.__enter__()
    try:
        clear_pending_tty_input()
        clarification_stage_args = build_stage_args(
            project_dir,
            auto_confirm=bool(args.yes),
            requirement_name=workflow_requirement_name,
        )
        message("\n===== 需求澄清阶段 =====")
        notify_stage_action_changed("stage.a03.start")
        try:
            requirements_result = run_requirements_clarification_stage(
                clarification_stage_args,
                preserve_ba_worker=True,
            )
        except Exception as error:  # noqa: BLE001
            if _bridge_terminal_active():
                raise
            message(error)
            return 1

        clear_pending_tty_input()
        review_stage_args = build_stage_args(
            project_dir,
            auto_confirm=bool(args.yes),
            requirement_name=requirements_result.requirement_name,
            review_max_rounds=str(args.requirements_review_max_rounds or "").strip(),
        )
        message("\n===== 需求评审阶段 =====")
        notify_stage_action_changed("stage.a04.start")
        try:
            review_result = run_requirements_review_stage(
                review_stage_args,
                ba_handoff=requirements_result.ba_handoff,
                preserve_ba_worker=True,
            )
        except Exception as error:  # noqa: BLE001
            if _bridge_terminal_active():
                raise
            message(error)
            return 1

        clear_pending_tty_input()
        design_stage_args = build_stage_args(
            project_dir,
            auto_confirm=bool(args.yes),
            requirement_name=review_result.requirement_name,
            review_max_rounds=str(args.detailed_design_review_max_rounds or "").strip(),
        )
        message("\n===== 详细设计阶段 =====")
        notify_stage_action_changed("stage.a05.start")
        try:
            design_result = run_detailed_design_stage(
                design_stage_args,
                ba_handoff=review_result.ba_handoff,
                preserve_workers=True,
            )
        except Exception as error:  # noqa: BLE001
            if _bridge_terminal_active():
                raise
            message(error)
            return 1

        clear_pending_tty_input()
        task_split_stage_args = build_stage_args(
            project_dir,
            auto_confirm=bool(args.yes),
            requirement_name=design_result.requirement_name,
            review_max_rounds=str(args.task_split_review_max_rounds or "").strip(),
        )
        message("\n===== 任务拆分阶段 =====")
        notify_stage_action_changed("stage.a06.start")
        try:
            task_split_result = run_task_split_stage(
                task_split_stage_args,
                ba_handoff=design_result.ba_handoff,
                reviewer_handoff=design_result.reviewer_handoff,
            )
        except Exception as error:  # noqa: BLE001
            if _bridge_terminal_active():
                raise
            message(error)
            return 1

        clear_pending_tty_input()
        development_stage_args = build_stage_args(
            project_dir,
            auto_confirm=bool(args.yes),
            requirement_name=task_split_result.requirement_name,
            review_max_rounds=str(args.development_review_max_rounds or "").strip(),
        )
        message("\n===== 任务开发阶段 =====")
        cleanup_stale_development_runtime_state(project_dir, task_split_result.requirement_name)
        notify_stage_action_changed("stage.a07.start")
        try:
            development_result = run_development_stage(
                development_stage_args,
                preserve_workers=True,
            )
        except Exception as error:  # noqa: BLE001
            if _bridge_terminal_active():
                raise
            message(error)
            return 1

        clear_pending_tty_input()
        overall_review_stage_args = build_stage_args(
            project_dir,
            auto_confirm=bool(args.yes),
            requirement_name=task_split_result.requirement_name,
        )
        message("\n===== 复核阶段 =====")
        notify_stage_action_changed("stage.a08.start")
        try:
            run_overall_review_stage(
                overall_review_stage_args,
                developer_handoff=development_result.developer_handoff,
                reviewer_handoff=development_result.reviewer_handoff,
            )
        except Exception as error:  # noqa: BLE001
            if _bridge_terminal_active():
                raise
            message(error)
            return 1
        message()
        message(render_remaining_stage_placeholders())
        return 0
    finally:
        lock_context.__exit__(None, None, None)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        cleaned_sessions = cleanup_registered_tmux_workers(reason="keyboard_interrupt")
        if cleaned_sessions:
            message(f"\n已清理 tmux 会话: {', '.join(cleaned_sessions)}")
        raise SystemExit(130)
