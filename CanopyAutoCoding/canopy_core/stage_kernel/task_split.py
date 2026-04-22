# -*- encoding: utf-8 -*-
"""
@File: A06_TaskSplit.py
@Modify Time: 2026/4/20
@Author: Kevin-Chen
@Descriptions: 任务拆分阶段
"""

from __future__ import annotations

import argparse
import contextlib
import json
import shutil
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from A01_Routing_LayerPlanning import (
    DEFAULT_MODEL_BY_VENDOR,
    normalize_effort_choice,
    normalize_model_choice,
    normalize_vendor_choice,
)
from canopy_core.prompt_contracts.common import check_reviewer_job
from canopy_core.prompt_contracts.task_split import (
    again_review_task,
    create_task_split_ba,
    modify_task,
    re_task_md_to_json,
    review_task,
    task_md_to_json,
    task_split,
)
from canopy_core.runtime.contracts import TaskResultContract, TurnFileContract, TurnFileResult
from canopy_core.runtime.hitl import build_prefixed_sha256
from canopy_core.runtime.tmux_runtime import (
    DEFAULT_COMMAND_TIMEOUT_SEC,
    AgentRunConfig,
    TmuxBatchWorker,
    TmuxRuntimeController,
    Vendor,
    build_session_name,
    cleanup_registered_tmux_workers,
    is_agent_ready_timeout_error,
    is_provider_auth_error,
    is_turn_artifact_contract_error,
    is_worker_death_error,
    list_registered_tmux_workers,
)
from canopy_core.stage_kernel.detailed_design import (
    DetailedDesignReviewerSpec,
    build_detailed_design_paths,
    collect_ba_agent_selection,
    run_detailed_design_stage,
    resolve_reviewer_specs as resolve_design_reviewer_specs,
)
from canopy_core.stage_kernel.reviewer_orchestration import (
    repair_reviewer_round_outputs,
    run_parallel_reviewer_round,
    shutdown_stage_workers,
)
from canopy_core.stage_kernel.death_orchestration import (
    ensure_active_reviewers,
    run_main_phase_with_death_handling,
    run_reviewer_phase_with_death_handling,
)
from canopy_core.stage_kernel.shared_review import (
    MAX_REVIEWER_REPAIR_ATTEMPTS,
    ReviewAgentHandoff,
    ReviewAgentSelection,
    ReviewStageProgress,
    ReviewerRuntime,
    ensure_empty_file,
    prompt_yes_no_choice,
    prompt_replacement_review_agent_selection,
    prompt_review_agent_selection,
    render_review_agent_selection,
    render_tmux_start_summary,
    worker_has_provider_auth_error,
)
from T01_tools import get_markdown_content, is_file_empty, is_standard_task_initial_json, is_task_progress_json, task_done
from T08_pre_development import (
    build_pre_development_task_record_path,
    ensure_pre_development_task_record,
    mark_task_split_completed,
    update_pre_development_task_status,
)
from T09_terminal_ops import maybe_launch_tui, message
from T12_requirements_common import (
    DEFAULT_REQUIREMENTS_CLARIFICATION_EFFORT,
    DEFAULT_REQUIREMENTS_CLARIFICATION_MODEL,
    DEFAULT_REQUIREMENTS_CLARIFICATION_VENDOR,
    RequirementsAnalystHandoff,
    build_requirements_clarification_paths,
    prompt_project_dir,
    prompt_requirement_name_selection,
    sanitize_requirement_name,
    stdin_is_interactive,
)


TASK_SPLIT_TASK_NAME = "任务拆分"
TASK_SPLIT_RUNTIME_ROOT_NAME = ".task_split_runtime"
MAX_TASK_SPLIT_REVIEW_ROUNDS = 5
MAX_TASK_SPLIT_JSON_REPAIR_ATTEMPTS = 2
PLACEHOLDER_NEXT_STEP = "下一步进入任务开发阶段（待接入）"
TASK_SPLIT_BA_ROLE_DESC = "你是任务拆分阶段的需求分析师，负责将详细设计转换为可执行任务单，并在评审后对任务单做最小化修订。"

TaskSplitReviewerSpec = DetailedDesignReviewerSpec


@dataclass(frozen=True)
class TaskSplitStageResult:
    project_dir: str
    requirement_name: str
    task_md_path: str
    task_json_path: str
    merged_review_path: str
    passed: bool
    cleanup_paths: tuple[str, ...] = ()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="任务拆分阶段")
    parser.add_argument("--project-dir", help="项目目录")
    parser.add_argument("--requirement-name", help="需求名称")
    parser.add_argument("--vendor", help="需求分析师厂商: codex|claude|gemini|qwen|kimi")
    parser.add_argument("--model", help="需求分析师模型名称")
    parser.add_argument("--effort", help="需求分析师推理强度")
    parser.add_argument("--proxy-url", default="", help="需求分析师代理端口或完整代理 URL")
    parser.add_argument("--reviewer-role", action="append", default=[], help="重复传入以覆盖任务拆分评审角色列表")
    parser.add_argument("--reviewer-role-prompt", action="append", default=[], help="重复传入以覆盖对应角色提示词")
    parser.add_argument("--yes", action="store_true", help="跳过非关键确认")
    parser.add_argument("--no-tui", action="store_true", help="显式禁用 OpenTUI")
    parser.add_argument("--legacy-cli", action="store_true", help="使用旧版 Python CLI，不跳转 OpenTUI")
    return parser


def build_task_split_paths(project_dir: str | Path, requirement_name: str) -> dict[str, Path]:
    project_root = Path(project_dir).expanduser().resolve()
    safe_name = sanitize_requirement_name(requirement_name)
    original_requirement_path, requirements_clear_path, ask_human_path, hitl_record_path = (
        build_requirements_clarification_paths(project_root, requirement_name)
    )
    return {
        "project_root": project_root,
        "original_requirement_path": original_requirement_path,
        "requirements_clear_path": requirements_clear_path,
        "ask_human_path": ask_human_path,
        "hitl_record_path": hitl_record_path,
        "pre_development_path": build_pre_development_task_record_path(project_root, requirement_name),
        "detailed_design_path": project_root / f"{safe_name}_详细设计.md",
        "task_md_path": project_root / f"{safe_name}_任务单.md",
        "task_json_path": project_root / f"{safe_name}_任务单.json",
        "merged_review_path": project_root / f"{safe_name}_任务单评审记录.md",
        "ba_feedback_path": project_root / f"{safe_name}_需求分析师反馈.md",
    }


def build_reviewer_artifact_paths(project_dir: str | Path, requirement_name: str, reviewer_name: str) -> tuple[Path, Path]:
    project_root = Path(project_dir).expanduser().resolve()
    safe_name = sanitize_requirement_name(requirement_name)
    artifact_agent_name = sanitize_requirement_name(reviewer_name)
    review_md_path = project_root / f"{safe_name}_任务单评审记录_{artifact_agent_name}.md"
    review_json_path = project_root / f"{safe_name}_评审记录_{artifact_agent_name}.json"
    return review_md_path, review_json_path


def build_task_split_reviewer_worker_id(role_name: str) -> str:
    return f"task-split-review-{str(role_name).strip()}"


def cleanup_existing_task_split_artifacts(paths: dict[str, Path], requirement_name: str) -> tuple[str, ...]:
    project_root = paths["project_root"]
    safe_name = sanitize_requirement_name(requirement_name)
    removed: list[str] = []
    for pattern in (
        f"{safe_name}_评审记录_*.json",
        f"{safe_name}_任务单评审记录_*.md",
    ):
        for candidate in project_root.glob(pattern):
            if candidate.is_file():
                candidate.unlink()
                removed.append(str(candidate.resolve()))
    for candidate in (
        paths["task_md_path"],
        paths["task_json_path"],
        paths["merged_review_path"],
        paths["ba_feedback_path"],
        paths["ask_human_path"],
    ):
        if candidate.exists() and candidate.is_file():
            candidate.write_text("", encoding="utf-8")
            removed.append(str(candidate.resolve()))
    return tuple(dict.fromkeys(removed))


def cleanup_stale_task_split_runtime_state(project_dir: str | Path) -> tuple[str, ...]:
    runtime_root = Path(project_dir).expanduser().resolve() / TASK_SPLIT_RUNTIME_ROOT_NAME
    if not runtime_root.exists() or not runtime_root.is_dir():
        return ()
    tmux_runtime = TmuxRuntimeController()
    removed: list[str] = []
    for worker_dir in sorted(path for path in runtime_root.iterdir() if path.is_dir()):
        state_path = worker_dir / "worker.state.json"
        session_name = ""
        if state_path.exists():
            try:
                payload = json.loads(state_path.read_text(encoding="utf-8"))
            except Exception:
                payload = {}
            if isinstance(payload, dict):
                session_name = str(payload.get("session_name", "")).strip()
        if session_name:
            try:
                tmux_runtime.kill_session(session_name, missing_ok=True)
            except Exception:
                pass
        shutil.rmtree(worker_dir, ignore_errors=True)
        removed.append(str(worker_dir.resolve()))
    if runtime_root.exists() and runtime_root.is_dir() and not any(runtime_root.iterdir()):
        runtime_root.rmdir()
        removed.append(str(runtime_root.resolve()))
    return tuple(removed)


def _predict_worker_display_name(
    *,
    project_dir: str | Path,
    worker_id: str,
    occupied_session_names: Sequence[str] = (),
) -> str:
    occupied = {str(name).strip() for name in occupied_session_names if str(name).strip()}
    for worker in list_registered_tmux_workers():
        session_name = str(getattr(worker, "session_name", "") or "").strip()
        if session_name:
            occupied.add(session_name)
    return build_session_name(
        worker_id,
        Path(project_dir).expanduser().resolve(),
        Vendor.CODEX,
        occupied_session_names=sorted(occupied),
    )


def _task_split_ba_display_name(
    *,
    project_dir: str | Path,
    handoff: RequirementsAnalystHandoff | None = None,
) -> str:
    session_name = str(getattr(getattr(handoff, "worker", None), "session_name", "") or "").strip()
    if session_name:
        return session_name
    return _predict_worker_display_name(project_dir=project_dir, worker_id="task-split-analyst")


def _reviewer_artifact_agent_name(reviewer: ReviewerRuntime) -> str:
    worker = getattr(reviewer, "worker", None)
    session_name = str(getattr(worker, "session_name", "") or "").strip()
    reviewer_name = str(getattr(reviewer, "reviewer_name", "") or "").strip()
    return session_name or reviewer_name


def _reviewer_spec_identity(reviewer_spec: TaskSplitReviewerSpec) -> str:
    return str(reviewer_spec.reviewer_key or reviewer_spec.role_name).strip()


def _reviewer_default_selection() -> ReviewAgentSelection:
    return ReviewAgentSelection(
        vendor=DEFAULT_REQUIREMENTS_CLARIFICATION_VENDOR,
        model=DEFAULT_REQUIREMENTS_CLARIFICATION_MODEL,
        reasoning_effort=DEFAULT_REQUIREMENTS_CLARIFICATION_EFFORT,
        proxy_url="",
    )


def _is_live_ba_handoff(handoff: RequirementsAnalystHandoff | None) -> bool:
    if handoff is None:
        return False
    get_state = getattr(handoff.worker, "get_agent_state", None)
    if callable(get_state):
        with contextlib.suppress(Exception):
            state = get_state()
            state_name = str(getattr(state, "value", state) or "").strip().upper()
            if state_name == "DEAD":
                return False
    session_name = str(getattr(handoff.worker, "session_name", "") or "").strip()
    if not session_name:
        return False
    session_exists = getattr(handoff.worker, "session_exists", None)
    if callable(session_exists):
        try:
            return bool(session_exists())
        except Exception:
            return False
    return True


def _is_live_reviewer_handoff(handoff: ReviewAgentHandoff) -> bool:
    get_state = getattr(handoff.worker, "get_agent_state", None)
    if callable(get_state):
        with contextlib.suppress(Exception):
            state = get_state()
            state_name = str(getattr(state, "value", state) or "").strip().upper()
            if state_name == "DEAD":
                return False
    session_name = str(getattr(handoff.worker, "session_name", "") or "").strip()
    if not session_name:
        return False
    session_exists = getattr(handoff.worker, "session_exists", None)
    if callable(session_exists):
        try:
            return bool(session_exists())
        except Exception:
            return False
    return True


def build_task_split_stage_argv(args: argparse.Namespace, *, project_dir: str, requirement_name: str) -> list[str]:
    argv = ["--project-dir", project_dir, "--requirement-name", requirement_name]
    if getattr(args, "yes", False):
        argv.append("--yes")
    if getattr(args, "no_tui", False):
        argv.append("--no-tui")
    if getattr(args, "legacy_cli", False):
        argv.append("--legacy-cli")
    interactive = stdin_is_interactive()
    if not interactive:
        vendor = normalize_vendor_choice(str(getattr(args, "vendor", "") or "").strip() or DEFAULT_REQUIREMENTS_CLARIFICATION_VENDOR)
        model = normalize_model_choice(vendor, str(getattr(args, "model", "") or "").strip() or DEFAULT_MODEL_BY_VENDOR[vendor])
        effort = normalize_effort_choice(vendor, model, str(getattr(args, "effort", "") or "").strip() or DEFAULT_REQUIREMENTS_CLARIFICATION_EFFORT)
        argv.extend(["--vendor", vendor, "--model", model, "--effort", effort])
        proxy_url = str(getattr(args, "proxy_url", "") or "").strip()
        if proxy_url:
            argv.extend(["--proxy-url", proxy_url])
    return argv


def ensure_task_split_inputs(
    args: argparse.Namespace,
    *,
    project_dir: str,
    requirement_name: str,
    ba_handoff: RequirementsAnalystHandoff | None,
    reviewer_handoff: Sequence[ReviewAgentHandoff],
) -> tuple[dict[str, Path], RequirementsAnalystHandoff | None, tuple[ReviewAgentHandoff, ...]]:
    paths = build_task_split_paths(project_dir, requirement_name)
    active_ba_handoff = ba_handoff
    active_reviewer_handoff = tuple(reviewer_handoff)
    if not get_markdown_content(paths["detailed_design_path"]).strip():
        message(f"缺少详细设计文档，先自动执行详细设计阶段: {paths['detailed_design_path'].name}")
        design_result = run_detailed_design_stage(
            build_task_split_stage_argv(args, project_dir=project_dir, requirement_name=requirement_name),
            ba_handoff=ba_handoff,
            preserve_workers=True,
        )
        active_ba_handoff = design_result.ba_handoff
        active_reviewer_handoff = tuple(design_result.reviewer_handoff)
    paths = build_task_split_paths(project_dir, requirement_name)
    if not get_markdown_content(paths["original_requirement_path"]).strip():
        raise RuntimeError(f"缺少原始需求文档: {paths['original_requirement_path']}")
    if not get_markdown_content(paths["requirements_clear_path"]).strip():
        raise RuntimeError(f"缺少需求澄清文档: {paths['requirements_clear_path']}")
    if not get_markdown_content(paths["detailed_design_path"]).strip():
        raise RuntimeError(f"缺少详细设计文档: {paths['detailed_design_path']}")
    ensure_pre_development_task_record(project_dir, requirement_name)
    return paths, active_ba_handoff, active_reviewer_handoff


def should_skip_existing_task_split(
    args: argparse.Namespace,
    *,
    paths: dict[str, Path],
    progress: ReviewStageProgress | None = None,
) -> bool:
    if bool(getattr(args, "yes", False)) or not stdin_is_interactive():
        return False
    task_md_text = get_markdown_content(paths["task_md_path"]).strip()
    if not task_md_text or not is_task_progress_json(paths["task_json_path"]):
        return False
    if progress is not None:
        progress.set_phase("任务拆分 / 跳过确认")
    return prompt_yes_no_choice(
        f"检测到已存在《{paths['task_md_path'].name}》与《{paths['task_json_path'].name}》，是否跳过任务拆分阶段",
        False,
        progress=progress,
        preview_path=paths["task_md_path"],
        preview_title="现有任务单",
    )


def create_task_split_ba_handoff(
    *,
    project_dir: str | Path,
    selection: ReviewAgentSelection,
) -> RequirementsAnalystHandoff:
    project_root = Path(project_dir).expanduser().resolve()
    worker = TmuxBatchWorker(
        worker_id="task-split-analyst",
        work_dir=project_root,
        config=AgentRunConfig(
            vendor=selection.vendor,
            model=selection.model,
            reasoning_effort=selection.reasoning_effort,
            proxy_url=selection.proxy_url,
        ),
        runtime_root=project_root / TASK_SPLIT_RUNTIME_ROOT_NAME,
    )
    message(render_tmux_start_summary(str(worker.session_name).strip() or "需求分析师", worker))
    return RequirementsAnalystHandoff(
        worker=worker,
        vendor=selection.vendor,
        model=selection.model,
        reasoning_effort=selection.reasoning_effort,
        proxy_url=selection.proxy_url,
    )


def prepare_task_split_ba_handoff(
    args: argparse.Namespace,
    *,
    project_dir: str | Path,
    ba_handoff: RequirementsAnalystHandoff | None,
) -> tuple[RequirementsAnalystHandoff, bool]:
    if _is_live_ba_handoff(ba_handoff):
        message("复用上一阶段的需求分析师继续生成任务单")
        return ba_handoff, False
    role_label = _task_split_ba_display_name(project_dir=project_dir)
    selection = collect_ba_agent_selection(args, role_label=role_label)
    message(render_review_agent_selection("进入任务拆分阶段（需求分析师）", selection))
    return create_task_split_ba_handoff(project_dir=project_dir, selection=selection), True


def build_task_split_init_prompt(paths: dict[str, Path], *, role_desc: str = TASK_SPLIT_BA_ROLE_DESC) -> str:
    return create_task_split_ba(
        role_desc,
        original_requirement_md=str(paths["original_requirement_path"].resolve()),
        requirements_clear_md=str(paths["requirements_clear_path"].resolve()),
        hitl_record_md=str(paths["hitl_record_path"].resolve()),
        detail_design_md=str(paths["detailed_design_path"].resolve()),
    )


def build_task_split_prompt(paths: dict[str, Path]) -> str:
    return task_split(
        task_md=str(paths["task_md_path"].resolve()),
        detail_design_md=str(paths["detailed_design_path"].resolve()),
        hitl_record_md=str(paths["hitl_record_path"].resolve()),
        original_requirement_md=str(paths["original_requirement_path"].resolve()),
        requirements_clear_md=str(paths["requirements_clear_path"].resolve()),
    )


def build_ba_init_result_contract(paths: dict[str, Path]) -> TaskResultContract:
    return TaskResultContract(
        turn_id="a06_ba_init",
        phase="a06_ba_init",
        task_kind="a06_ba_init",
        mode="a06_ba_init",
        expected_statuses=("ready",),
        stage_name=TASK_SPLIT_TASK_NAME,
        optional_artifacts={
            "original_requirement": paths["original_requirement_path"],
            "requirements_clear": paths["requirements_clear_path"],
            "hitl_record": paths["hitl_record_path"],
            "detailed_design": paths["detailed_design_path"],
        },
        terminal_status_tokens={"ready": ("完成",)},
        terminal_status_summaries={"ready": "任务拆分阶段智能体已完成初始化"},
    )


def build_task_split_generate_result_contract(paths: dict[str, Path]) -> TaskResultContract:
    return TaskResultContract(
        turn_id="a06_task_split_generate",
        phase="a06_task_split_generate",
        task_kind="a06_task_split_generate",
        mode="a06_task_split_generate",
        expected_statuses=("completed",),
        stage_name=TASK_SPLIT_TASK_NAME,
        required_artifacts={"task_md": paths["task_md_path"]},
        optional_artifacts={
            "original_requirement": paths["original_requirement_path"],
            "requirements_clear": paths["requirements_clear_path"],
            "hitl_record": paths["hitl_record_path"],
            "detailed_design": paths["detailed_design_path"],
        },
        terminal_status_tokens={"completed": ("完成",)},
        terminal_status_summaries={"completed": "需求分析师已生成任务单文档"},
    )


def build_task_split_feedback_result_contract(paths: dict[str, Path]) -> TaskResultContract:
    return TaskResultContract(
        turn_id="a06_task_split_feedback",
        phase="a06_task_split_feedback",
        task_kind="a06_task_split_feedback",
        mode="a06_task_split_feedback",
        expected_statuses=("hitl", "completed"),
        stage_name=TASK_SPLIT_TASK_NAME,
        optional_artifacts={
            "ask_human": paths["ask_human_path"],
            "ba_feedback": paths["ba_feedback_path"],
            "task_md": paths["task_md_path"],
            "hitl_record": paths["hitl_record_path"],
        },
    )


def build_task_split_json_result_contract(paths: dict[str, Path]) -> TaskResultContract:
    return TaskResultContract(
        turn_id="a06_task_split_json_generate",
        phase="a06_task_split_json_generate",
        task_kind="a06_task_split_json_generate",
        mode="a06_task_split_json_generate",
        expected_statuses=("completed",),
        stage_name=TASK_SPLIT_TASK_NAME,
        required_artifacts={"task_json": paths["task_json_path"]},
        optional_artifacts={"task_md": paths["task_md_path"]},
        terminal_status_tokens={"completed": ("完成",)},
        terminal_status_summaries={"completed": "需求分析师已生成任务单 JSON"},
    )


def _parse_result_payload(clean_output: str) -> dict[str, object]:
    try:
        payload = json.loads(clean_output)
    except Exception as error:  # noqa: BLE001
        raise RuntimeError(f"未识别到结构化结果 JSON: {clean_output!r}") from error
    if not isinstance(payload, dict):
        raise RuntimeError("结构化结果必须是 JSON 对象")
    return payload


def _run_ba_turn(
    handoff: RequirementsAnalystHandoff,
    *,
    label: str,
    prompt: str,
    result_contract: TaskResultContract,
) -> dict[str, object]:
    result = handoff.worker.run_turn(
        label=label,
        prompt=prompt,
        result_contract=result_contract,
        timeout_sec=DEFAULT_COMMAND_TIMEOUT_SEC,
    )
    if not result.ok:
        raise RuntimeError(result.clean_output or f"{label} 执行失败")
    return _parse_result_payload(result.clean_output)


def recreate_task_split_ba_handoff(
    *,
    project_dir: str | Path,
    previous_handoff: RequirementsAnalystHandoff,
    progress: ReviewStageProgress | None = None,
) -> RequirementsAnalystHandoff | None:
    if not stdin_is_interactive():
        return None
    ba_display_name = _task_split_ba_display_name(project_dir=project_dir, handoff=previous_handoff)
    selection = prompt_replacement_review_agent_selection(
        reason_text=f"检测到{ba_display_name}不可继续使用，需要重建需求分析师后继续当前阶段。",
        previous_selection=ReviewAgentSelection(
            previous_handoff.vendor,
            previous_handoff.model,
            previous_handoff.reasoning_effort,
            previous_handoff.proxy_url,
        ),
        force_model_change=True,
        role_label=ba_display_name,
        progress=progress,
    )
    if selection is None:
        return None
    return create_task_split_ba_handoff(project_dir=project_dir, selection=selection)


def run_ba_turn_with_recovery(
    handoff: RequirementsAnalystHandoff,
    *,
    project_dir: str | Path,
    label: str,
    prompt: str,
    result_contract: TaskResultContract,
    initialize_on_replacement: bool,
    paths: dict[str, Path],
    init_role_desc: str = TASK_SPLIT_BA_ROLE_DESC,
    progress: ReviewStageProgress | None = None,
) -> tuple[RequirementsAnalystHandoff, dict[str, object]]:
    current_handoff = handoff
    needs_initialize = False
    while True:
        try:
            if needs_initialize:
                _run_ba_turn(
                    current_handoff,
                    label=f"{label}_reinit",
                    prompt=build_task_split_init_prompt(paths, role_desc=init_role_desc),
                    result_contract=build_ba_init_result_contract(paths),
                )
                needs_initialize = False
            payload = _run_ba_turn(
                current_handoff,
                label=label,
                prompt=prompt,
                result_contract=result_contract,
            )
            return current_handoff, payload
        except Exception as error:  # noqa: BLE001
            ba_display_name = _task_split_ba_display_name(project_dir=project_dir, handoff=current_handoff)
            auth_error = is_provider_auth_error(error) or worker_has_provider_auth_error(current_handoff.worker)
            ready_timeout_error = is_agent_ready_timeout_error(error)
            if auth_error or ready_timeout_error:
                replacement = recreate_task_split_ba_handoff(
                    project_dir=project_dir,
                    previous_handoff=current_handoff,
                    progress=progress,
                )
                if replacement is None:
                    raise RuntimeError(f"{ba_display_name} 无法继续，且未能重建需求分析师") from error
                current_handoff = replacement
                needs_initialize = initialize_on_replacement
                continue
            if is_worker_death_error(error):
                replacement = recreate_task_split_ba_handoff(
                    project_dir=project_dir,
                    previous_handoff=current_handoff,
                    progress=progress,
                )
                if replacement is None:
                    raise RuntimeError(f"{ba_display_name} 已死亡，且未能重建需求分析师") from error
                current_handoff = replacement
                needs_initialize = initialize_on_replacement
                continue
            raise


def generate_task_split_document(
    handoff: RequirementsAnalystHandoff,
    *,
    project_dir: str | Path,
    paths: dict[str, Path],
    initialize_first: bool,
    progress: ReviewStageProgress | None = None,
) -> RequirementsAnalystHandoff:
    current_handoff = handoff
    if initialize_first:
        current_handoff, _ = run_ba_turn_with_recovery(
            current_handoff,
            project_dir=project_dir,
            label="task_split_ba_init",
            prompt=build_task_split_init_prompt(paths),
            result_contract=build_ba_init_result_contract(paths),
            initialize_on_replacement=False,
            paths=paths,
            progress=progress,
        )
    if progress is not None:
        progress.set_phase("任务拆分 / 生成中")
    current_handoff, _ = run_ba_turn_with_recovery(
        current_handoff,
        project_dir=project_dir,
        label="generate_task_split",
        prompt=build_task_split_prompt(paths),
        result_contract=build_task_split_generate_result_contract(paths),
        initialize_on_replacement=True,
        paths=paths,
        progress=progress,
    )
    if not get_markdown_content(paths["task_md_path"]).strip():
        raise RuntimeError("任务单为空，未生成有效《任务单.md》")
    return current_handoff


def build_reviewer_completion_contract(
    *,
    reviewer_name: str,
    review_md_path: Path,
    review_json_path: Path,
) -> TurnFileContract:
    def validator(status_path: Path) -> TurnFileResult:
        if not status_path.exists():
            raise FileNotFoundError(f"缺少审核 JSON 文件: {status_path}")
        payload = json.loads(status_path.read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            raise ValueError(f"审核 JSON 必须是 list: {status_path}")
        matched_item: dict[str, object] | None = None
        for item in payload:
            if not isinstance(item, dict):
                continue
            if str(item.get("task_name", "")).strip() == TASK_SPLIT_TASK_NAME:
                matched_item = item
                break
        if matched_item is None:
            raise ValueError(f"{status_path.name} 缺少 {TASK_SPLIT_TASK_NAME} 状态项")
        review_pass = matched_item.get("review_pass")
        if not isinstance(review_pass, bool):
            raise ValueError(f"{status_path.name} 中 {TASK_SPLIT_TASK_NAME}.review_pass 必须是 bool")
        review_md_empty = is_file_empty(review_md_path)
        if review_pass and not review_md_empty:
            raise ValueError(f"{reviewer_name} 已审核通过，但 {review_md_path.name} 不为空")
        if (not review_pass) and review_md_empty:
            raise ValueError(f"{reviewer_name} 未通过，但 {review_md_path.name} 为空")
        artifact_paths = {
            "review_md": str(review_md_path.resolve()),
            "review_json": str(review_json_path.resolve()),
        }
        artifact_hashes = {
            str(review_md_path.resolve()): build_prefixed_sha256(review_md_path),
            str(review_json_path.resolve()): build_prefixed_sha256(review_json_path),
        }
        return TurnFileResult(
            status_path=str(status_path.resolve()),
            payload={"task_name": TASK_SPLIT_TASK_NAME, "review_pass": review_pass},
            artifact_paths=artifact_paths,
            artifact_hashes=artifact_hashes,
            validated_at=str(status_path.stat().st_mtime),
        )

    return TurnFileContract(
        turn_id=f"task_split_review_{reviewer_name}",
        phase=TASK_SPLIT_TASK_NAME,
        status_path=review_json_path,
        validator=validator,
    )


def _reviewer_has_materialized_outputs(reviewer: ReviewerRuntime) -> bool:
    try:
        payload = json.loads(reviewer.review_json_path.read_text(encoding="utf-8"))
    except Exception:
        payload = None
    if isinstance(payload, list):
        for item in payload:
            if not isinstance(item, dict):
                continue
            if str(item.get("task_name", "")).strip() != TASK_SPLIT_TASK_NAME:
                continue
            if isinstance(item.get("review_pass"), bool):
                return True
    return not is_file_empty(reviewer.review_md_path)


def _reviewer_artifact_signature(reviewer: ReviewerRuntime) -> tuple[object, ...]:
    signatures: list[object] = []
    for path in (reviewer.review_md_path, reviewer.review_json_path):
        if not path.exists():
            signatures.append(("missing", str(path.resolve())))
            continue
        stat = path.stat()
        signatures.append((str(path.resolve()), stat.st_size, stat.st_mtime_ns))
    return tuple(signatures)


def create_reviewer_runtime(
    *,
    project_dir: str | Path,
    requirement_name: str,
    reviewer_spec: TaskSplitReviewerSpec,
    selection: ReviewAgentSelection,
) -> ReviewerRuntime:
    reviewer_identity = _reviewer_spec_identity(reviewer_spec)
    runtime_root = Path(project_dir).expanduser().resolve() / TASK_SPLIT_RUNTIME_ROOT_NAME
    worker = TmuxBatchWorker(
        worker_id=build_task_split_reviewer_worker_id(reviewer_spec.role_name),
        work_dir=Path(project_dir).expanduser().resolve(),
        config=AgentRunConfig(
            vendor=selection.vendor,
            model=selection.model,
            reasoning_effort=selection.reasoning_effort,
            proxy_url=selection.proxy_url,
        ),
        runtime_root=runtime_root,
    )
    review_md_path, review_json_path = build_reviewer_artifact_paths(
        project_dir,
        requirement_name,
        str(worker.session_name).strip() or reviewer_spec.role_name,
    )
    ensure_empty_file(review_md_path)
    message(render_tmux_start_summary(str(worker.session_name).strip() or reviewer_spec.role_name, worker))
    return ReviewerRuntime(
        reviewer_name=reviewer_identity,
        selection=selection,
        worker=worker,
        review_md_path=review_md_path,
        review_json_path=review_json_path,
        contract=build_reviewer_completion_contract(
            reviewer_name=reviewer_identity,
            review_md_path=review_md_path,
            review_json_path=review_json_path,
        ),
    )


def bind_reviewer_runtime_from_handoff(
    *,
    project_dir: str | Path,
    requirement_name: str,
    handoff: ReviewAgentHandoff,
) -> ReviewerRuntime:
    reviewer_name = str(getattr(handoff.worker, "session_name", "") or "").strip() or handoff.role_name
    review_md_path, review_json_path = build_reviewer_artifact_paths(project_dir, requirement_name, reviewer_name)
    ensure_empty_file(review_md_path)
    return ReviewerRuntime(
        reviewer_name=handoff.reviewer_key,
        selection=handoff.selection,
        worker=handoff.worker,
        review_md_path=review_md_path,
        review_json_path=review_json_path,
        contract=build_reviewer_completion_contract(
            reviewer_name=handoff.reviewer_key,
            review_md_path=review_md_path,
            review_json_path=review_json_path,
        ),
    )


def resolve_reviewer_specs(
    args: argparse.Namespace,
    *,
    reviewer_handoff: Sequence[ReviewAgentHandoff],
    progress: ReviewStageProgress | None = None,
) -> list[TaskSplitReviewerSpec]:
    if reviewer_handoff:
        return [
            TaskSplitReviewerSpec(
                role_name=item.role_name,
                role_prompt=item.role_prompt,
                reviewer_key=item.reviewer_key,
            )
            for item in reviewer_handoff
        ]
    return list(resolve_design_reviewer_specs(args, progress=progress))


def build_reviewer_workers(
    args: argparse.Namespace,
    *,
    project_dir: str | Path,
    requirement_name: str,
    reviewer_specs: Sequence[TaskSplitReviewerSpec],
    reviewer_handoff: Sequence[ReviewAgentHandoff],
    progress: ReviewStageProgress | None = None,
) -> tuple[list[ReviewerRuntime], list[ReviewerRuntime]]:
    if progress is not None:
        progress.set_phase("任务拆分 / 启动审核器")
    reviewers: list[ReviewerRuntime] = []
    newly_created_reviewers: list[ReviewerRuntime] = []
    predicted_session_names: set[str] = set()
    interactive = stdin_is_interactive()
    live_handoffs_by_key = {
        item.reviewer_key: item
        for item in reviewer_handoff
        if _is_live_reviewer_handoff(item)
    }
    if live_handoffs_by_key:
        message("复用仍存活的详细设计审核智能体继续审核任务单")
    if reviewer_handoff and len(live_handoffs_by_key) != len(reviewer_handoff):
        message("部分详细设计审核智能体已失效，仅重建失效的任务拆分审核智能体")
    for reviewer_spec in reviewer_specs:
        reviewer_key = _reviewer_spec_identity(reviewer_spec)
        live_handoff = live_handoffs_by_key.get(reviewer_key)
        if live_handoff is not None:
            reviewers.append(
                bind_reviewer_runtime_from_handoff(
                    project_dir=project_dir,
                    requirement_name=requirement_name,
                    handoff=live_handoff,
                )
            )
            continue
        reviewer_display_name = _predict_worker_display_name(
            project_dir=project_dir,
            worker_id=build_task_split_reviewer_worker_id(reviewer_spec.role_name),
            occupied_session_names=sorted(predicted_session_names),
        )
        predicted_session_names.add(reviewer_display_name)
        if interactive:
            selection = prompt_review_agent_selection(
                DEFAULT_REQUIREMENTS_CLARIFICATION_VENDOR,
                default_model=DEFAULT_REQUIREMENTS_CLARIFICATION_MODEL,
                default_reasoning_effort=DEFAULT_REQUIREMENTS_CLARIFICATION_EFFORT,
                default_proxy_url="",
                role_label=reviewer_display_name,
                progress=progress,
            )
            message(render_review_agent_selection(f"{reviewer_display_name} 配置", selection))
        else:
            selection = _reviewer_default_selection()
        reviewer = create_reviewer_runtime(
            project_dir=project_dir,
            requirement_name=requirement_name,
            reviewer_spec=reviewer_spec,
            selection=selection,
        )
        reviewers.append(reviewer)
        newly_created_reviewers.append(reviewer)
    return reviewers, newly_created_reviewers


def _run_reviewer_result_turn(
    reviewer: ReviewerRuntime,
    *,
    label: str,
    prompt: str,
    result_contract: TaskResultContract,
) -> ReviewerRuntime | None:
    while True:
        try:
            result = reviewer.worker.run_turn(
                label=label,
                prompt=prompt,
                result_contract=result_contract,
                timeout_sec=DEFAULT_COMMAND_TIMEOUT_SEC,
            )
            if not result.ok:
                raise RuntimeError(result.clean_output or f"{reviewer.reviewer_name} 执行失败")
            return reviewer
        except Exception as error:  # noqa: BLE001
            if is_worker_death_error(error):
                message(f"{_reviewer_artifact_agent_name(reviewer)} 已死亡，当前阶段将忽略该审核智能体。")
                return None
            raise RuntimeError(f"{_reviewer_artifact_agent_name(reviewer)} 初始化失败") from error


def _run_reviewer_turn_with_resume(
    reviewer: ReviewerRuntime,
    *,
    label: str,
    prompt: str,
) -> ReviewerRuntime | None:
    while True:
        baseline_signature = _reviewer_artifact_signature(reviewer)
        try:
            result = reviewer.worker.run_turn(
                label=label,
                prompt=prompt,
                completion_contract=reviewer.contract,
                timeout_sec=DEFAULT_COMMAND_TIMEOUT_SEC,
            )
            if not result.ok:
                raise RuntimeError(result.clean_output or f"{reviewer.reviewer_name} 执行失败")
            return reviewer
        except Exception as error:  # noqa: BLE001
            if is_turn_artifact_contract_error(error):
                return reviewer
            if (
                _reviewer_has_materialized_outputs(reviewer)
                and _reviewer_artifact_signature(reviewer) != baseline_signature
            ):
                return reviewer
            reviewer_display_name = _reviewer_artifact_agent_name(reviewer)
            auth_error = is_provider_auth_error(error) or worker_has_provider_auth_error(reviewer.worker)
            ready_timeout_error = is_agent_ready_timeout_error(error)
            if is_worker_death_error(error):
                message(f"{reviewer_display_name} 已死亡，当前阶段将忽略该审核智能体。")
                return None
            if auth_error or ready_timeout_error:
                raise RuntimeError(f"{reviewer_display_name} 无法继续，自动恢复失败") from error
            raise


def initialize_task_split_workers(
    ba_handoff: RequirementsAnalystHandoff,
    *,
    project_dir: str | Path,
    paths: dict[str, Path],
    initialize_ba: bool,
    reviewers: Sequence[ReviewerRuntime],
    reviewer_specs_by_name: dict[str, TaskSplitReviewerSpec],
    initialize_reviewers: bool,
    progress: ReviewStageProgress | None = None,
) -> tuple[RequirementsAnalystHandoff, list[ReviewerRuntime]]:
    current_handoff = ba_handoff
    reviewer_list = list(reviewers)
    if not initialize_ba and not initialize_reviewers:
        return current_handoff, reviewer_list
    if progress is not None:
        progress.set_phase("任务拆分 / 初始化中")
    if initialize_ba:
        current_handoff, _ = run_ba_turn_with_recovery(
            current_handoff,
            project_dir=project_dir,
            label="task_split_ba_init",
            prompt=build_task_split_init_prompt(paths),
            result_contract=build_ba_init_result_contract(paths),
            initialize_on_replacement=False,
            paths=paths,
            progress=progress,
        )
    if initialize_reviewers:
        initialized_reviewers: list[ReviewerRuntime] = []
        for reviewer in reviewer_list:
            reviewer_spec = reviewer_specs_by_name[reviewer.reviewer_name]
            initialized = _run_reviewer_result_turn(
                reviewer,
                label=f"task_split_reviewer_init_{sanitize_requirement_name(reviewer.reviewer_name)}",
                prompt=build_task_split_init_prompt(paths, role_desc=reviewer_spec.role_prompt),
                result_contract=build_ba_init_result_contract(paths),
            )
            if initialized is not None:
                initialized_reviewers.append(initialized)
        reviewer_list = initialized_reviewers
    return current_handoff, reviewer_list


def _run_parallel_reviewers(
    reviewers: Sequence[ReviewerRuntime],
    *,
    reviewer_specs_by_name: dict[str, TaskSplitReviewerSpec],
    round_index: int,
    prompt_builder,
    label_prefix: str,
    progress: ReviewStageProgress | None = None,
) -> list[ReviewerRuntime]:
    if progress is not None:
        progress.set_phase(f"任务拆分评审第 {round_index} 轮")
    return run_parallel_reviewer_round(
        reviewers,
        key_func=lambda reviewer: reviewer.reviewer_name,
        run_turn=lambda reviewer: _run_reviewer_turn_with_resume(
            reviewer,
            label=f"{label_prefix}_{sanitize_requirement_name(reviewer.reviewer_name)}_round_{round_index}",
            prompt=prompt_builder(reviewer, reviewer_specs_by_name[reviewer.reviewer_name]),
        ),
        error_prefix="任务拆分审核智能体执行失败:",
    )


def repair_reviewer_outputs(
    reviewers: Sequence[ReviewerRuntime],
    *,
    reviewer_specs_by_name: dict[str, TaskSplitReviewerSpec],
    project_dir: str | Path,
    requirement_name: str,
    round_index: int,
) -> list[ReviewerRuntime]:
    json_pattern = f"{sanitize_requirement_name(requirement_name)}_评审记录_*.json"
    md_pattern = f"{sanitize_requirement_name(requirement_name)}_任务单评审记录_*.md"
    return repair_reviewer_round_outputs(
        reviewers,
        key_func=lambda reviewer: reviewer.reviewer_name,
        artifact_name_func=_reviewer_artifact_agent_name,
        check_job=lambda reviewer_names: check_reviewer_job(
            reviewer_names,
            directory=project_dir,
            task_name=TASK_SPLIT_TASK_NAME,
            json_pattern=json_pattern,
            md_pattern=md_pattern,
        ),
        run_fix_turn=lambda reviewer, fix_prompt, repair_attempt: _run_reviewer_turn_with_resume(
            reviewer,
            label=f"task_split_review_fix_{sanitize_requirement_name(reviewer.reviewer_name)}_round_{round_index}_attempt_{repair_attempt}",
            prompt=fix_prompt,
        ),
        max_attempts=MAX_REVIEWER_REPAIR_ATTEMPTS,
        error_prefix="任务拆分审核智能体修复输出失败:",
        final_error="任务拆分审核智能体多次修复后仍未按协议更新文档",
    )


def _read_required_task_split_ba_feedback(paths: dict[str, Path]) -> str:
    ba_feedback = get_markdown_content(paths["ba_feedback_path"]).strip()
    if ba_feedback:
        return ba_feedback
    raise RuntimeError(f"任务拆分评审未通过，但 {paths['ba_feedback_path'].name} 为空")


def _active_reviewer_files(reviewers: Sequence[ReviewerRuntime]) -> tuple[list[str], list[str]]:
    json_files = [str(reviewer.review_json_path.resolve()) for reviewer in reviewers]
    md_files = [str(reviewer.review_md_path.resolve()) for reviewer in reviewers]
    return json_files, md_files


def _replace_dead_task_split_ba(
    handoff: RequirementsAnalystHandoff,
    *,
    project_dir: str | Path,
    progress: ReviewStageProgress | None = None,
) -> RequirementsAnalystHandoff:
    replacement = recreate_task_split_ba_handoff(
        project_dir=project_dir,
        previous_handoff=handoff,
        progress=progress,
    )
    if replacement is None:
        ba_display_name = _task_split_ba_display_name(project_dir=project_dir, handoff=handoff)
        raise RuntimeError(f"{ba_display_name} 已死亡，且未能重建需求分析师")
    return replacement


def run_ba_modify_loop(
    handoff: RequirementsAnalystHandoff,
    *,
    project_dir: str | Path,
    paths: dict[str, Path],
    review_msg: str,
    progress: ReviewStageProgress | None = None,
) -> RequirementsAnalystHandoff:
    current_handoff = handoff
    ensure_empty_file(paths["ask_human_path"])
    ensure_empty_file(paths["ba_feedback_path"])
    if progress is not None:
        progress.set_phase("任务拆分 / 按评审修订")
    current_handoff, _ = run_ba_turn_with_recovery(
        current_handoff,
        project_dir=project_dir,
        label="modify_task_split",
        prompt=modify_task(
            review_msg,
            task_md=str(paths["task_md_path"].resolve()),
            ask_human_md=str(paths["ask_human_path"].resolve()),
            what_just_change=str(paths["ba_feedback_path"].resolve()),
        ),
        result_contract=build_task_split_feedback_result_contract(paths),
        initialize_on_replacement=True,
        paths=paths,
        progress=progress,
    )
    if get_markdown_content(paths["ask_human_path"]).strip():
        raise RuntimeError("A06 当前不支持 HITL，请先完成详细设计或调整任务单输入")
    _read_required_task_split_ba_feedback(paths)
    return current_handoff


def generate_task_split_json(
    handoff: RequirementsAnalystHandoff,
    *,
    project_dir: str | Path,
    paths: dict[str, Path],
    progress: ReviewStageProgress | None = None,
) -> RequirementsAnalystHandoff:
    current_handoff = handoff
    ensure_empty_file(paths["task_json_path"])
    if progress is not None:
        progress.set_phase("任务拆分 / 生成任务单 JSON")
    prompts = [
        task_md_to_json(
            task_md=str(paths["task_md_path"].resolve()),
            task_json=str(paths["task_json_path"].resolve()),
        ),
        re_task_md_to_json(
            task_md=str(paths["task_md_path"].resolve()),
            task_json=str(paths["task_json_path"].resolve()),
        ),
    ]
    last_error: Exception | None = None
    for attempt in range(1, MAX_TASK_SPLIT_JSON_REPAIR_ATTEMPTS + 1):
        ensure_empty_file(paths["task_json_path"])
        prompt = prompts[0] if attempt == 1 else prompts[1]
        try:
            current_handoff, _ = run_ba_turn_with_recovery(
                current_handoff,
                project_dir=project_dir,
                label=f"generate_task_split_json_attempt_{attempt}",
                prompt=prompt,
                result_contract=build_task_split_json_result_contract(paths),
                initialize_on_replacement=True,
                paths=paths,
                progress=progress,
            )
        except Exception as error:  # noqa: BLE001
            last_error = error
            continue
        if is_standard_task_initial_json(paths["task_json_path"]):
            return current_handoff
        last_error = RuntimeError("任务单 JSON 未通过结构校验")
    raise RuntimeError("任务单 JSON 生成失败") from last_error


def _shutdown_workers(
    ba_handoff: RequirementsAnalystHandoff | None,
    reviewers: Sequence[ReviewerRuntime],
    *,
    cleanup_runtime: bool,
) -> tuple[str, ...]:
    return shutdown_stage_workers(
        ba_handoff,
        reviewers,
        cleanup_runtime=cleanup_runtime,
    )


def run_task_split_stage(
    argv: Sequence[str] | None = None,
    *,
    ba_handoff: RequirementsAnalystHandoff | None = None,
    reviewer_handoff: Sequence[ReviewAgentHandoff] | None = None,
) -> TaskSplitStageResult:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.project_dir:
        project_dir = str(Path(args.project_dir).expanduser().resolve())
    else:
        project_dir = prompt_project_dir("")
    if args.requirement_name:
        requirement_name = str(args.requirement_name).strip()
    else:
        requirement_name = prompt_requirement_name_selection(project_dir, "").requirement_name

    progress = ReviewStageProgress(initial_phase="任务拆分准备中")
    active_ba_handoff = ba_handoff
    active_reviewer_handoff = tuple(reviewer_handoff or ())
    reviewer_workers: list[ReviewerRuntime] = []
    cleanup_paths: tuple[str, ...] = ()
    try:
        paths, active_ba_handoff, active_reviewer_handoff = ensure_task_split_inputs(
            args,
            project_dir=project_dir,
            requirement_name=requirement_name,
            ba_handoff=active_ba_handoff,
            reviewer_handoff=active_reviewer_handoff,
        )
        if should_skip_existing_task_split(args, paths=paths, progress=progress):
            message(f"检测到已存在《{paths['task_md_path'].name}》与《{paths['task_json_path'].name}》")
            message("用户选择跳过任务拆分阶段")
            update_pre_development_task_status(project_dir, requirement_name, task_key="任务拆分", completed=True)
            message("已将任务拆分标记为完成，继续后续阶段")
            return TaskSplitStageResult(
                project_dir=project_dir,
                requirement_name=requirement_name,
                task_md_path=str(paths["task_md_path"].resolve()),
                task_json_path=str(paths["task_json_path"].resolve()),
                merged_review_path=str(paths["merged_review_path"].resolve()),
                passed=True,
                cleanup_paths=(),
        )
        update_pre_development_task_status(project_dir, requirement_name, task_key="任务拆分", completed=False)
        cleanup_stale_task_split_runtime_state(project_dir)
        cleanup_existing_task_split_artifacts(paths, requirement_name)
        reviewer_label_getter = lambda reviewer, index: _reviewer_artifact_agent_name(reviewer) or f"任务拆分审核智能体 {index}"  # noqa: E731
        active_ba_handoff, created_new_ba = prepare_task_split_ba_handoff(
            args,
            project_dir=project_dir,
            ba_handoff=active_ba_handoff,
        )
        reviewer_specs = resolve_reviewer_specs(
            args,
            reviewer_handoff=active_reviewer_handoff,
            progress=progress,
        )
        reviewer_specs_by_name = {_reviewer_spec_identity(item): item for item in reviewer_specs}
        if created_new_ba:
            _, reviewer_workers, active_ba_handoff = run_main_phase_with_death_handling(
                active_ba_handoff,
                reviewers=(),
                run_phase=lambda handoff: initialize_task_split_workers(
                    handoff,
                    project_dir=project_dir,
                    paths=paths,
                    initialize_ba=True,
                    reviewers=(),
                    reviewer_specs_by_name=reviewer_specs_by_name,
                    initialize_reviewers=False,
                    progress=progress,
                )[0],
                replace_dead_main_owner=lambda owner: _replace_dead_task_split_ba(
                    owner,
                    project_dir=project_dir,
                    progress=progress,
                ),
                main_label="任务拆分需求分析师",
                reviewer_label_getter=reviewer_label_getter,
                notify=message,
            )
        reviewer_workers: list[ReviewerRuntime] = []
        new_reviewer_workers: list[ReviewerRuntime] = []
        _, reviewer_workers, active_ba_handoff = run_main_phase_with_death_handling(
            active_ba_handoff,
            reviewers=reviewer_workers,
            run_phase=lambda handoff: generate_task_split_document(
                handoff,
                project_dir=project_dir,
                paths=paths,
                initialize_first=False,
                progress=progress,
            ),
            replace_dead_main_owner=lambda owner: _replace_dead_task_split_ba(
                owner,
                project_dir=project_dir,
                progress=progress,
            ),
            main_label="任务拆分需求分析师",
            reviewer_label_getter=reviewer_label_getter,
            notify=message,
        )

        def initial_prompt_builder(reviewer: ReviewerRuntime, reviewer_spec: TaskSplitReviewerSpec) -> str:
            return review_task(
                reviewer_spec.role_prompt,
                task_name=TASK_SPLIT_TASK_NAME,
                original_requirement_md=str(paths["original_requirement_path"].resolve()),
                requirements_clear_md=str(paths["requirements_clear_path"].resolve()),
                hitl_record_md=str(paths["hitl_record_path"].resolve()),
                task_md=str(paths["task_md_path"].resolve()),
                detail_design_md=str(paths["detailed_design_path"].resolve()),
                task_review_md=str(reviewer.review_md_path.resolve()),
                task_review_json=str(reviewer.review_json_path.resolve()),
            )

        for round_index in range(1, MAX_TASK_SPLIT_REVIEW_ROUNDS + 1):
            if round_index == 1:
                reviewer_workers, new_reviewer_workers = build_reviewer_workers(
                    args,
                    project_dir=project_dir,
                    requirement_name=requirement_name,
                    reviewer_specs=reviewer_specs,
                    reviewer_handoff=active_reviewer_handoff,
                    progress=progress,
                )
                if new_reviewer_workers:
                    created_reviewer_keys = {item.reviewer_name for item in new_reviewer_workers}
                    new_reviewer_workers, active_ba_handoff = run_reviewer_phase_with_death_handling(
                        active_ba_handoff,
                        new_reviewer_workers,
                        run_phase=lambda active_reviewers: initialize_task_split_workers(
                            active_ba_handoff,
                            project_dir=project_dir,
                            paths=paths,
                            initialize_ba=False,
                            reviewers=active_reviewers,
                            reviewer_specs_by_name=reviewer_specs_by_name,
                            initialize_reviewers=True,
                            progress=progress,
                        )[1],
                        replace_dead_main_owner=lambda owner: _replace_dead_task_split_ba(
                            owner,
                            project_dir=project_dir,
                            progress=progress,
                        ),
                        main_label="任务拆分需求分析师",
                        reviewer_label_getter=reviewer_label_getter,
                        notify=message,
                    )
                    reviewer_workers = [item for item in reviewer_workers if item.reviewer_name not in created_reviewer_keys]
                    reviewer_workers.extend(new_reviewer_workers)
                reviewer_workers, active_ba_handoff = run_reviewer_phase_with_death_handling(
                    active_ba_handoff,
                    reviewer_workers,
                    run_phase=lambda active_reviewers: _run_parallel_reviewers(
                        active_reviewers,
                        reviewer_specs_by_name=reviewer_specs_by_name,
                        round_index=round_index,
                        prompt_builder=initial_prompt_builder,
                        label_prefix="task_split_review_init",
                        progress=progress,
                    ),
                    replace_dead_main_owner=lambda owner: _replace_dead_task_split_ba(
                        owner,
                        project_dir=project_dir,
                        progress=progress,
                    ),
                    main_label="任务拆分需求分析师",
                    reviewer_label_getter=reviewer_label_getter,
                    notify=message,
                )
                reviewer_workers, active_ba_handoff = run_reviewer_phase_with_death_handling(
                    active_ba_handoff,
                    reviewer_workers,
                    run_phase=lambda active_reviewers: repair_reviewer_outputs(
                        active_reviewers,
                        reviewer_specs_by_name=reviewer_specs_by_name,
                        project_dir=project_dir,
                        requirement_name=requirement_name,
                        round_index=round_index,
                    ),
                    replace_dead_main_owner=lambda owner: _replace_dead_task_split_ba(
                        owner,
                        project_dir=project_dir,
                        progress=progress,
                    ),
                    main_label="任务拆分需求分析师",
                    reviewer_label_getter=reviewer_label_getter,
                    notify=message,
                )
            else:
                review_msg = get_markdown_content(paths["merged_review_path"]).strip()
                if not review_msg:
                    raise RuntimeError("任务拆分评审未通过，但合并后的任务单评审记录为空")
                _, reviewer_workers, active_ba_handoff = run_main_phase_with_death_handling(
                    active_ba_handoff,
                    reviewers=reviewer_workers,
                    run_phase=lambda handoff: run_ba_modify_loop(
                        handoff,
                        project_dir=project_dir,
                        paths=paths,
                        review_msg=review_msg,
                        progress=progress,
                    ),
                    replace_dead_main_owner=lambda owner: _replace_dead_task_split_ba(
                        owner,
                        project_dir=project_dir,
                        progress=progress,
                    ),
                    main_label="任务拆分需求分析师",
                    reviewer_label_getter=reviewer_label_getter,
                    notify=message,
                )
                ba_reply = _read_required_task_split_ba_feedback(paths)

                def again_prompt_builder(reviewer: ReviewerRuntime, reviewer_spec: TaskSplitReviewerSpec) -> str:
                    del reviewer_spec
                    return again_review_task(
                        ba_reply,
                        task_name=TASK_SPLIT_TASK_NAME,
                        task_md=str(paths["task_md_path"].resolve()),
                        task_review_md=str(reviewer.review_md_path.resolve()),
                        task_review_json=str(reviewer.review_json_path.resolve()),
                    )

                reviewer_workers, active_ba_handoff = run_reviewer_phase_with_death_handling(
                    active_ba_handoff,
                    reviewer_workers,
                    run_phase=lambda active_reviewers: _run_parallel_reviewers(
                        active_reviewers,
                        reviewer_specs_by_name=reviewer_specs_by_name,
                        round_index=round_index,
                        prompt_builder=again_prompt_builder,
                        label_prefix="task_split_review_again",
                        progress=progress,
                    ),
                    replace_dead_main_owner=lambda owner: _replace_dead_task_split_ba(
                        owner,
                        project_dir=project_dir,
                        progress=progress,
                    ),
                    main_label="任务拆分需求分析师",
                    reviewer_label_getter=reviewer_label_getter,
                    notify=message,
                )
                reviewer_workers, active_ba_handoff = run_reviewer_phase_with_death_handling(
                    active_ba_handoff,
                    reviewer_workers,
                    run_phase=lambda active_reviewers: repair_reviewer_outputs(
                        active_reviewers,
                        reviewer_specs_by_name=reviewer_specs_by_name,
                        project_dir=project_dir,
                        requirement_name=requirement_name,
                        round_index=round_index,
                    ),
                    replace_dead_main_owner=lambda owner: _replace_dead_task_split_ba(
                        owner,
                        project_dir=project_dir,
                        progress=progress,
                    ),
                    main_label="任务拆分需求分析师",
                    reviewer_label_getter=reviewer_label_getter,
                    notify=message,
                )

            ensure_active_reviewers(reviewer_workers, stage_label="任务拆分")
            review_json_files, review_md_files = _active_reviewer_files(reviewer_workers)
            passed = task_done(
                directory=project_dir,
                file_path=paths["pre_development_path"],
                task_name=TASK_SPLIT_TASK_NAME,
                json_pattern=f"{sanitize_requirement_name(requirement_name)}_评审记录_*.json",
                md_pattern=f"{sanitize_requirement_name(requirement_name)}_任务单评审记录_*.md",
                md_output_name=paths["merged_review_path"].name,
                json_files=review_json_files,
                md_files=review_md_files,
            )
            if passed:
                try:
                    _, reviewer_workers, active_ba_handoff = run_main_phase_with_death_handling(
                        active_ba_handoff,
                        reviewers=reviewer_workers,
                        run_phase=lambda handoff: generate_task_split_json(
                            handoff,
                            project_dir=project_dir,
                            paths=paths,
                            progress=progress,
                        ),
                        replace_dead_main_owner=lambda owner: _replace_dead_task_split_ba(
                            owner,
                            project_dir=project_dir,
                            progress=progress,
                        ),
                        main_label="任务拆分需求分析师",
                        reviewer_label_getter=reviewer_label_getter,
                        notify=message,
                    )
                except Exception:
                    update_pre_development_task_status(project_dir, requirement_name, task_key="任务拆分", completed=False)
                    raise
                mark_task_split_completed(project_dir, requirement_name)
                cleanup_paths = _shutdown_workers(
                    active_ba_handoff,
                    reviewer_workers,
                    cleanup_runtime=True,
                )
                return TaskSplitStageResult(
                    project_dir=project_dir,
                    requirement_name=requirement_name,
                    task_md_path=str(paths["task_md_path"].resolve()),
                    task_json_path=str(paths["task_json_path"].resolve()),
                    merged_review_path=str(paths["merged_review_path"].resolve()),
                    passed=True,
                    cleanup_paths=cleanup_paths,
                )

        raise RuntimeError(f"任务拆分评审超过最大轮数 {MAX_TASK_SPLIT_REVIEW_ROUNDS}，仍未全部通过")
    except Exception:
        _shutdown_workers(
            active_ba_handoff,
            reviewer_workers,
            cleanup_runtime=False,
        )
        raise
    finally:
        progress.stop()


def main(argv: Sequence[str] | None = None) -> int:
    redirected, launch = maybe_launch_tui(argv, route="task-split", action="stage.a06.start")
    if redirected:
        return int(launch)
    try:
        result = run_task_split_stage(list(launch))
    except Exception as error:  # noqa: BLE001
        message(error)
        return 1

    message("任务拆分完成")
    message(result.task_md_path)
    message(result.task_json_path)
    message(PLACEHOLDER_NEXT_STEP)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        cleaned_sessions = cleanup_registered_tmux_workers(reason="keyboard_interrupt")
        if cleaned_sessions:
            message(f"\n已清理 tmux 会话: {', '.join(cleaned_sessions)}")
        raise SystemExit(130)
