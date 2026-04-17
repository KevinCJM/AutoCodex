# -*- encoding: utf-8 -*-
"""
@File: A03_RequirementsReview.py
@Modify Time: 2026/4/16
@Author: Kevin-Chen
@Descriptions: 需求评审阶段
"""

from __future__ import annotations

import argparse
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from A01_Routing_LayerPlanning import (
    DEFAULT_MODEL_BY_VENDOR,
    normalize_effort_choice,
    normalize_model_choice,
    normalize_vendor_choice,
    prompt_effort,
    prompt_model,
    prompt_vendor,
)
from A02_RequirementsAnalysis import (
    DEFAULT_REQUIREMENTS_ANALYSIS_EFFORT,
    DEFAULT_REQUIREMENTS_ANALYSIS_MODEL,
    DEFAULT_REQUIREMENTS_ANALYSIS_VENDOR,
    NOTION_RUNTIME_ROOT_NAME,
    RequirementsAnalystHandoff,
    build_pre_development_task_record_path,
    build_requirements_analysis_paths,
    ensure_requirements_hitl_record_file,
    prompt_project_dir,
    prompt_requirement_name_selection,
    sanitize_requirement_name,
)
from Prompt_03_RequirementsReview import (
    human_feed_bck,
    requirements_review_init,
    requirements_review_reply,
    resume_ba,
    review_feedback,
)
from T01_tools import (
    check_task_exists,
    create_empty_json_files,
    get_markdown_content,
    is_file_empty,
    task_done,
)
from T02_tmux_agents import (
    DEFAULT_COMMAND_TIMEOUT_SEC,
    AgentRunConfig,
    TaskResultContract,
    TmuxBatchWorker,
    TurnFileContract,
    TurnFileResult,
    cleanup_registered_tmux_workers,
    is_agent_ready_timeout_error,
    is_provider_auth_error,
    is_worker_death_error,
    try_resume_worker,
)
from T04_common_prompt import check_reviewer_job
from T05_hitl_runtime import build_prefixed_sha256
from T09_terminal_ops import (
    collect_multiline_input,
    message,
    maybe_launch_tui,
    prompt_positive_int as terminal_prompt_positive_int,
    prompt_with_default,
    prompt_yes_no as terminal_prompt_yes_no,
)
from T08_pre_development import (
    ensure_pre_development_task_record,
    update_pre_development_task_status,
)


REQUIREMENTS_REVIEW_TASK_NAME = "需求评审"
REQUIREMENTS_REVIEW_RUNTIME_ROOT_NAME = ".requirements_review_runtime"
DEFAULT_REVIEWER_COUNT = 1
MAX_REVIEW_ROUNDS = 5
MAX_REVIEWER_REPAIR_ATTEMPTS = 2


@dataclass(frozen=True)
class ReviewAgentSelection:
    vendor: str
    model: str
    reasoning_effort: str
    proxy_url: str


@dataclass(frozen=True)
class ReviewerRuntime:
    reviewer_name: str
    selection: ReviewAgentSelection
    worker: TmuxBatchWorker
    review_md_path: Path
    review_json_path: Path
    contract: TurnFileContract


@dataclass(frozen=True)
class RequirementsReviewStageResult:
    project_dir: str
    requirement_name: str
    merged_review_path: str
    rounds_used: int
    passed: bool
    cleanup_paths: tuple[str, ...] = ()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="需求评审阶段")
    parser.add_argument("--project-dir", help="项目目录")
    parser.add_argument("--requirement-name", help="需求名称")
    parser.add_argument("--yes", action="store_true", help="跳过非关键确认")
    parser.add_argument("--no-tui", action="store_true", help="显式禁用 OpenTUI")
    parser.add_argument("--legacy-cli", action="store_true", help="使用旧版 Python CLI，不跳转 OpenTUI")
    return parser


def prompt_proxy_url(default: str = "") -> str:
    return prompt_with_default("代理端口或完整代理 URL，可留空", default, allow_empty=True)


def prompt_positive_int(prompt_text: str, default: int = 1) -> int:
    return terminal_prompt_positive_int(prompt_text, default)


def prompt_review_agent_selection(
        default_vendor: str = DEFAULT_REQUIREMENTS_ANALYSIS_VENDOR,
        default_model: str = "",
        default_reasoning_effort: str = DEFAULT_REQUIREMENTS_ANALYSIS_EFFORT,
        default_proxy_url: str = "",
) -> ReviewAgentSelection:
    vendor = prompt_vendor(default_vendor)
    preferred_model = default_model if default_model and vendor == default_vendor else DEFAULT_MODEL_BY_VENDOR[vendor]
    model = prompt_model(vendor, preferred_model)
    reasoning_effort = prompt_effort(vendor, model, default_reasoning_effort)
    proxy_url = prompt_proxy_url(default_proxy_url)
    return ReviewAgentSelection(
        vendor=vendor,
        model=model,
        reasoning_effort=reasoning_effort,
        proxy_url=proxy_url,
    )


def render_review_agent_selection(title: str, selection: ReviewAgentSelection) -> str:
    return "\n".join(
        [
            title,
            f"vendor: {selection.vendor}",
            f"model: {selection.model}",
            f"reasoning_effort: {selection.reasoning_effort}",
            f"proxy_url: {selection.proxy_url or '(none)'}",
        ]
    )


def prompt_yes_no_choice(prompt_text: str, default: bool = False) -> bool:
    return terminal_prompt_yes_no(prompt_text, default)


def prompt_replacement_review_agent_selection(
        *,
        reason_text: str,
        previous_selection: ReviewAgentSelection,
        force_model_change: bool,
        role_label: str,
) -> ReviewAgentSelection | None:
    message(reason_text)
    if not prompt_yes_no_choice(f"是否创建新的{role_label}继续当前阶段", True):
        return None
    while True:
        selection = prompt_review_agent_selection(
            default_vendor=previous_selection.vendor,
            default_model=previous_selection.model,
            default_reasoning_effort=previous_selection.reasoning_effort,
            default_proxy_url=previous_selection.proxy_url,
        )
        if (
                not force_model_change
                or selection.vendor != previous_selection.vendor
                or selection.model != previous_selection.model
        ):
            message(render_review_agent_selection(f"重新创建{role_label}", selection))
            return selection
        message("需要更换模型，请选择与当前不同的厂商或模型。")


def build_requirements_review_paths(project_dir: str | Path, requirement_name: str) -> dict[str, Path]:
    project_root = Path(project_dir).expanduser().resolve()
    safe_name = sanitize_requirement_name(requirement_name)
    original_requirement_path, requirements_clear_path, ask_human_path, hitl_record_path = (
        build_requirements_analysis_paths(project_root, requirement_name)
    )
    return {
        "project_root": project_root,
        "original_requirement_path": original_requirement_path,
        "requirements_clear_path": requirements_clear_path,
        "ask_human_path": ask_human_path,
        "hitl_record_path": ensure_requirements_hitl_record_file(project_root, requirement_name),
        "pre_development_path": build_pre_development_task_record_path(project_root, requirement_name),
        "merged_review_path": project_root / f"{safe_name}_需求评审记录.md",
        "ba_feedback_path": project_root / f"{safe_name}_需求分析师反馈.md",
    }


def build_reviewer_artifact_paths(project_dir: str | Path, requirement_name: str, reviewer_name: str) -> tuple[Path, Path]:
    project_root = Path(project_dir).expanduser().resolve()
    safe_name = sanitize_requirement_name(requirement_name)
    review_md_path = project_root / f"{safe_name}_需求评审记录_{reviewer_name}.md"
    review_json_path = project_root / f"{safe_name}_评审记录_{reviewer_name}.json"
    return review_md_path, review_json_path


def create_reviewer_runtime(
        *,
        project_dir: str | Path,
        requirement_name: str,
        reviewer_name: str,
        selection: ReviewAgentSelection,
) -> ReviewerRuntime:
    runtime_root = Path(project_dir).expanduser().resolve() / REQUIREMENTS_REVIEW_RUNTIME_ROOT_NAME
    review_md_path, review_json_path = build_reviewer_artifact_paths(project_dir, requirement_name, reviewer_name)
    ensure_empty_file(review_md_path)
    worker = TmuxBatchWorker(
        worker_id=f"requirements-review-{reviewer_name.lower()}",
        work_dir=Path(project_dir).expanduser().resolve(),
        config=AgentRunConfig(
            vendor=selection.vendor,
            model=selection.model,
            reasoning_effort=selection.reasoning_effort,
            proxy_url=selection.proxy_url,
        ),
        runtime_root=runtime_root,
    )
    message(render_tmux_start_summary(f"审核器 {reviewer_name}", worker))
    return ReviewerRuntime(
        reviewer_name=reviewer_name,
        selection=selection,
        worker=worker,
        review_md_path=review_md_path,
        review_json_path=review_json_path,
        contract=build_reviewer_completion_contract(
            requirement_name=requirement_name,
            reviewer_name=reviewer_name,
            review_md_path=review_md_path,
            review_json_path=review_json_path,
        ),
    )


def cleanup_existing_review_artifacts(paths: dict[str, Path], requirement_name: str) -> tuple[str, ...]:
    project_root = paths["project_root"]
    safe_name = sanitize_requirement_name(requirement_name)
    removed: list[str] = []
    for pattern in (
            f"{safe_name}_评审记录_*.json",
            f"{safe_name}_需求评审记录_*.md",
    ):
        for candidate in project_root.glob(pattern):
            if candidate.is_file():
                candidate.unlink()
                removed.append(str(candidate.resolve()))
    for candidate in (
            paths["merged_review_path"],
            paths["ba_feedback_path"],
    ):
        if candidate.exists() and candidate.is_file():
            candidate.unlink()
            removed.append(str(candidate.resolve()))
    return tuple(removed)


def worker_has_provider_auth_error(worker: TmuxBatchWorker | None) -> bool:
    if worker is None:
        return False
    try:
        state = worker.read_state()
    except Exception:
        state = {}
    health_status = str(state.get("health_status", "")).strip().lower()
    health_note = str(state.get("health_note", "")).strip().lower()
    return health_status == "provider_auth_error" or is_provider_auth_error(health_note)


def ensure_empty_file(file_path: str | Path) -> Path:
    target = Path(file_path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("", encoding="utf-8")
    return target


def build_reviewer_completion_contract(
        *,
        requirement_name: str,
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
            if str(item.get("task_name", "")).strip() == REQUIREMENTS_REVIEW_TASK_NAME:
                matched_item = item
                break
        if matched_item is None:
            raise ValueError(f"{status_path.name} 缺少 {REQUIREMENTS_REVIEW_TASK_NAME} 状态项")
        review_pass = matched_item.get("review_pass")
        if not isinstance(review_pass, bool):
            raise ValueError(f"{status_path.name} 中 {REQUIREMENTS_REVIEW_TASK_NAME}.review_pass 必须是 bool")
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
            payload={"task_name": REQUIREMENTS_REVIEW_TASK_NAME, "review_pass": review_pass},
            artifact_paths=artifact_paths,
            artifact_hashes=artifact_hashes,
            validated_at=str(status_path.stat().st_mtime),
        )

    return TurnFileContract(
        turn_id=f"requirements_review_{reviewer_name}",
        phase=REQUIREMENTS_REVIEW_TASK_NAME,
        status_path=review_json_path,
        validator=validator,
    )


def build_ba_resume_result_contract(paths: dict[str, Path]) -> TaskResultContract:
    return TaskResultContract(
        turn_id="requirements_review_ba_resume",
        phase="requirements_review_ba_resume",
        task_kind="a03_ba_resume",
        mode="a03_ba_resume",
        expected_statuses=("ready",),
        stage_name=REQUIREMENTS_REVIEW_TASK_NAME,
        optional_artifacts={
            "original_requirement": paths["original_requirement_path"],
            "requirements_clear": paths["requirements_clear_path"],
            "hitl_record": paths["hitl_record_path"],
        },
    )


def build_ba_human_feedback_result_contract(paths: dict[str, Path]) -> TaskResultContract:
    return TaskResultContract(
        turn_id="requirements_review_human_feedback",
        phase="requirements_review_human_feedback",
        task_kind="a03_human_feedback",
        mode="a03_human_feedback",
        expected_statuses=("completed",),
        stage_name=REQUIREMENTS_REVIEW_TASK_NAME,
        required_artifacts={
            "ask_human": paths["ask_human_path"],
        },
        optional_artifacts={
            "requirements_clear": paths["requirements_clear_path"],
            "hitl_record": paths["hitl_record_path"],
        },
    )


def build_ba_review_feedback_result_contract(paths: dict[str, Path]) -> TaskResultContract:
    return TaskResultContract(
        turn_id="requirements_review_feedback",
        phase="requirements_review_feedback",
        task_kind="a03_ba_feedback",
        mode="a03_ba_feedback",
        expected_statuses=("hitl", "completed"),
        stage_name=REQUIREMENTS_REVIEW_TASK_NAME,
        optional_artifacts={
            "ask_human": paths["ask_human_path"],
            "ba_feedback": paths["ba_feedback_path"],
            "requirements_clear": paths["requirements_clear_path"],
            "hitl_record": paths["hitl_record_path"],
        },
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


def _run_reviewer_turn(
        reviewer: ReviewerRuntime,
        *,
        label: str,
        prompt: str,
) -> None:
    result = reviewer.worker.run_turn(
        label=label,
        prompt=prompt,
        completion_contract=reviewer.contract,
        timeout_sec=DEFAULT_COMMAND_TIMEOUT_SEC,
    )
    if not result.ok:
        raise RuntimeError(result.clean_output or f"{reviewer.reviewer_name} 执行失败")


def run_ba_turn_with_recreation(
        handoff: RequirementsAnalystHandoff,
        *,
        project_dir: str | Path,
        label: str,
        prompt: str,
        result_contract: TaskResultContract,
) -> tuple[RequirementsAnalystHandoff, dict[str, object]]:
    current_handoff = handoff
    while True:
        try:
            payload = _run_ba_turn(
                current_handoff,
                label=label,
                prompt=prompt,
                result_contract=result_contract,
            )
            return current_handoff, payload
        except Exception as error:  # noqa: BLE001
            auth_error = is_provider_auth_error(error) or worker_has_provider_auth_error(current_handoff.worker)
            ready_timeout_error = is_agent_ready_timeout_error(error)
            if auth_error:
                selection = prompt_replacement_review_agent_selection(
                    reason_text="检测到需求分析师仍在 agent 界面，但模型认证已失效。\n需要更换模型后继续当前阶段。",
                    previous_selection=ReviewAgentSelection(
                        current_handoff.vendor,
                        current_handoff.model,
                        current_handoff.reasoning_effort,
                        current_handoff.proxy_url,
                    ),
                    force_model_change=True,
                    role_label="需求分析师",
                )
                if selection is None:
                    raise RuntimeError("需求分析师认证已失效，且用户未更换模型") from error
                current_handoff = RequirementsAnalystHandoff(
                    worker=TmuxBatchWorker(
                        worker_id="requirements-review-analyst",
                        work_dir=Path(project_dir).expanduser().resolve(),
                        config=AgentRunConfig(
                            vendor=selection.vendor,
                            model=selection.model,
                            reasoning_effort=selection.reasoning_effort,
                            proxy_url=selection.proxy_url,
                        ),
                        runtime_root=Path(project_dir).expanduser().resolve() / REQUIREMENTS_REVIEW_RUNTIME_ROOT_NAME,
                    ),
                    vendor=selection.vendor,
                    model=selection.model,
                    reasoning_effort=selection.reasoning_effort,
                    proxy_url=selection.proxy_url,
                )
                message(render_tmux_start_summary("需求分析师", current_handoff.worker))
                continue
            if ready_timeout_error:
                selection = prompt_replacement_review_agent_selection(
                    reason_text="需求分析师启动超时，未能进入可输入状态。\n需要更换模型后继续当前阶段。",
                    previous_selection=ReviewAgentSelection(
                        current_handoff.vendor,
                        current_handoff.model,
                        current_handoff.reasoning_effort,
                        current_handoff.proxy_url,
                    ),
                    force_model_change=True,
                    role_label="需求分析师",
                )
                if selection is None:
                    raise RuntimeError("需求分析师启动超时，且用户未更换模型") from error
                current_handoff = RequirementsAnalystHandoff(
                    worker=TmuxBatchWorker(
                        worker_id="requirements-review-analyst",
                        work_dir=Path(project_dir).expanduser().resolve(),
                        config=AgentRunConfig(
                            vendor=selection.vendor,
                            model=selection.model,
                            reasoning_effort=selection.reasoning_effort,
                            proxy_url=selection.proxy_url,
                        ),
                        runtime_root=Path(project_dir).expanduser().resolve() / REQUIREMENTS_REVIEW_RUNTIME_ROOT_NAME,
                    ),
                    vendor=selection.vendor,
                    model=selection.model,
                    reasoning_effort=selection.reasoning_effort,
                    proxy_url=selection.proxy_url,
                )
                message(render_tmux_start_summary("需求分析师", current_handoff.worker))
                continue
            if is_worker_death_error(error):
                if try_resume_worker(current_handoff.worker, timeout_sec=60.0):
                    continue
                replacement = recreate_ba_handoff(
                    project_dir=project_dir,
                    previous_handoff=current_handoff,
                )
                if replacement is None:
                    raise RuntimeError("需求分析师已死亡，且用户未创建新的需求分析师") from error
                current_handoff = replacement
                continue
            raise


def run_reviewer_turn_with_recreation(
        reviewer: ReviewerRuntime,
        *,
        project_dir: str | Path,
        requirement_name: str,
        label: str,
        prompt: str,
) -> ReviewerRuntime:
    current_reviewer = reviewer
    while True:
        try:
            _run_reviewer_turn(
                current_reviewer,
                label=label,
                prompt=prompt,
            )
            return current_reviewer
        except Exception as error:  # noqa: BLE001
            auth_error = is_provider_auth_error(error) or worker_has_provider_auth_error(current_reviewer.worker)
            ready_timeout_error = is_agent_ready_timeout_error(error)
            if auth_error:
                selection = prompt_replacement_review_agent_selection(
                    reason_text=f"检测到审核器 {current_reviewer.reviewer_name} 仍在 agent 界面，但模型认证已失效。\n需要更换模型后继续当前阶段。",
                    previous_selection=current_reviewer.selection,
                    force_model_change=True,
                    role_label=f"审核器 {current_reviewer.reviewer_name}",
                )
                if selection is None:
                    raise RuntimeError(f"审核器 {current_reviewer.reviewer_name} 认证已失效，且用户未更换模型") from error
                current_reviewer = create_reviewer_runtime(
                    project_dir=project_dir,
                    requirement_name=requirement_name,
                    reviewer_name=current_reviewer.reviewer_name,
                    selection=selection,
                )
                continue
            if ready_timeout_error:
                selection = prompt_replacement_review_agent_selection(
                    reason_text=f"审核器 {current_reviewer.reviewer_name} 启动超时，未能进入可输入状态。\n需要更换模型后继续当前阶段。",
                    previous_selection=current_reviewer.selection,
                    force_model_change=True,
                    role_label=f"审核器 {current_reviewer.reviewer_name}",
                )
                if selection is None:
                    raise RuntimeError(f"审核器 {current_reviewer.reviewer_name} 启动超时，且用户未更换模型") from error
                current_reviewer = create_reviewer_runtime(
                    project_dir=project_dir,
                    requirement_name=requirement_name,
                    reviewer_name=current_reviewer.reviewer_name,
                    selection=selection,
                )
                continue
            if is_worker_death_error(error):
                if try_resume_worker(current_reviewer.worker, timeout_sec=60.0):
                    continue
                replacement = recreate_reviewer_runtime(
                    project_dir=project_dir,
                    requirement_name=requirement_name,
                    reviewer=current_reviewer,
                )
                if replacement is None:
                    raise RuntimeError(f"审核器 {current_reviewer.reviewer_name} 已死亡，且用户未创建新的审核器") from error
                current_reviewer = replacement
                continue
            raise


def render_tmux_start_summary(role_name: str, worker: TmuxBatchWorker) -> str:
    return "\n".join(
        [
            f"{role_name} 已启动",
            f"runtime_dir: {worker.runtime_dir}",
            f"session_name: {worker.session_name}",
            "可使用以下命令进入会话:",
            f"  tmux attach -t {worker.session_name}",
        ]
    )


def ensure_review_stage_inputs(paths: dict[str, Path], requirement_name: str) -> None:
    if not get_markdown_content(paths["original_requirement_path"]).strip():
        raise RuntimeError(f"缺少原始需求文档: {paths['original_requirement_path']}")
    if not get_markdown_content(paths["requirements_clear_path"]).strip():
        raise RuntimeError(f"缺少需求澄清文档: {paths['requirements_clear_path']}")
    ensure_pre_development_task_record(paths["project_root"], requirement_name)


def prepare_ba_handoff(
        *,
        project_dir: str | Path,
        requirement_name: str,
        ba_handoff: RequirementsAnalystHandoff | None,
        paths: dict[str, Path],
) -> tuple[RequirementsAnalystHandoff, tuple[str, ...]]:
    if ba_handoff is not None:
        return ba_handoff, ()

    message("直接进入需求评审阶段，当前没有可复用的需求分析师，将新建需求分析师")
    selection = prompt_review_agent_selection(DEFAULT_REQUIREMENTS_ANALYSIS_VENDOR)
    message(render_review_agent_selection("进入需求评审阶段（需求分析师）", selection))
    worker = TmuxBatchWorker(
        worker_id="requirements-review-analyst",
        work_dir=Path(project_dir).expanduser().resolve(),
        config=AgentRunConfig(
            vendor=selection.vendor,
            model=selection.model,
            reasoning_effort=selection.reasoning_effort,
            proxy_url=selection.proxy_url,
        ),
        runtime_root=Path(project_dir).expanduser().resolve() / REQUIREMENTS_REVIEW_RUNTIME_ROOT_NAME,
    )
    message(render_tmux_start_summary("需求分析师", worker))
    handoff = RequirementsAnalystHandoff(
        worker=worker,
        vendor=selection.vendor,
        model=selection.model,
        reasoning_effort=selection.reasoning_effort,
        proxy_url=selection.proxy_url,
    )
    handoff, payload = run_ba_turn_with_recreation(
        handoff,
        project_dir=project_dir,
        label="resume_requirements_review_ba",
        prompt=resume_ba(
            original_requirement_md=str(paths["original_requirement_path"].resolve()),
            requirements_clear_md=str(paths["requirements_clear_path"].resolve()),
            hitl_record_md=str(paths["hitl_record_path"].resolve()),
        ),
        result_contract=build_ba_resume_result_contract(paths),
    )
    if str(payload.get("status", "")).strip() != "ready":
        raise RuntimeError("需求分析师未按要求进入需求评审准备态")
    return (
        handoff,
        (str(handoff.worker.runtime_dir), str((Path(project_dir).expanduser().resolve() / REQUIREMENTS_REVIEW_RUNTIME_ROOT_NAME).resolve())),
    )


def recreate_ba_handoff(
        *,
        project_dir: str | Path,
        previous_handoff: RequirementsAnalystHandoff,
) -> RequirementsAnalystHandoff | None:
    selection = prompt_replacement_review_agent_selection(
        reason_text="检测到需求分析师已死亡，且 resume 失败。\n需要更换模型后继续当前阶段。",
        previous_selection=ReviewAgentSelection(
            previous_handoff.vendor,
            previous_handoff.model,
            previous_handoff.reasoning_effort,
            previous_handoff.proxy_url,
        ),
        force_model_change=True,
        role_label="需求分析师",
    )
    if selection is None:
        return None
    worker = TmuxBatchWorker(
        worker_id="requirements-review-analyst",
        work_dir=Path(project_dir).expanduser().resolve(),
        config=AgentRunConfig(
            vendor=selection.vendor,
            model=selection.model,
            reasoning_effort=selection.reasoning_effort,
            proxy_url=selection.proxy_url,
        ),
        runtime_root=Path(project_dir).expanduser().resolve() / REQUIREMENTS_REVIEW_RUNTIME_ROOT_NAME,
    )
    message(render_tmux_start_summary("需求分析师", worker))
    return RequirementsAnalystHandoff(
        worker=worker,
        vendor=selection.vendor,
        model=selection.model,
        reasoning_effort=selection.reasoning_effort,
        proxy_url=selection.proxy_url,
    )


def recreate_reviewer_runtime(
        *,
        project_dir: str | Path,
        requirement_name: str,
        reviewer: ReviewerRuntime,
) -> ReviewerRuntime | None:
    selection = prompt_replacement_review_agent_selection(
        reason_text=f"检测到审核器 {reviewer.reviewer_name} 已死亡，且 resume 失败。\n需要更换模型后继续当前阶段。",
        previous_selection=reviewer.selection,
        force_model_change=True,
        role_label=f"审核器 {reviewer.reviewer_name}",
    )
    if selection is None:
        return None
    return create_reviewer_runtime(
        project_dir=project_dir,
        requirement_name=requirement_name,
        reviewer_name=reviewer.reviewer_name,
        selection=selection,
    )


def run_human_check_loop(
        *,
        handoff: RequirementsAnalystHandoff,
        paths: dict[str, Path],
) -> RequirementsAnalystHandoff:
    message("进入需求评审阶段")
    message(f"请先阅读需求澄清文档: {paths['requirements_clear_path']}")
    current_handoff = handoff
    while True:
        if not prompt_yes_no_choice("是否向需求分析师提出建议或问题", False):
            return current_handoff
        human_msg = collect_multiline_input(
            title="请输入给需求分析师的问题或建议",
            empty_retry_message="内容不能为空，请重新输入。",
        )
        ensure_empty_file(paths["ask_human_path"])
        current_handoff, payload = run_ba_turn_with_recreation(
            current_handoff,
            project_dir=paths["project_root"],
            label="requirements_review_human_feedback",
            prompt=human_feed_bck(
                human_msg,
                original_requirement_md=str(paths["original_requirement_path"].resolve()),
                ask_human_md=str(paths["ask_human_path"].resolve()),
                requirements_clear_md=str(paths["requirements_clear_path"].resolve()),
                hitl_record_md=str(paths["hitl_record_path"].resolve()),
            ),
            result_contract=build_ba_human_feedback_result_contract(paths),
        )
        if str(payload.get("status", "")).strip() != "completed":
            raise RuntimeError("需求分析师未正确处理人类反馈")
        response = get_markdown_content(paths["ask_human_path"]).strip()
        if not response:
            raise RuntimeError("需求分析师未写入《与人类交流.md》")
        message("需求分析师回复:")
        message(response)


def build_reviewer_workers(
        *,
        project_dir: str | Path,
        requirement_name: str,
) -> list[ReviewerRuntime]:
    reviewer_count = prompt_positive_int("请输入审核器数量", DEFAULT_REVIEWER_COUNT)
    reviewer_names = [f"R{index}" for index in range(1, reviewer_count + 1)]
    create_empty_json_files(
        directory=project_dir,
        name_list=reviewer_names,
        pattern=f"{sanitize_requirement_name(requirement_name)}_评审记录_*.json",
    )
    reviewers: list[ReviewerRuntime] = []
    for reviewer_name in reviewer_names:
        message(f"配置审核器 {reviewer_name}")
        selection = prompt_review_agent_selection(DEFAULT_REQUIREMENTS_ANALYSIS_VENDOR)
        message(render_review_agent_selection(f"审核器 {reviewer_name} 配置", selection))
        reviewers.append(
            create_reviewer_runtime(
                project_dir=project_dir,
                requirement_name=requirement_name,
                reviewer_name=reviewer_name,
                selection=selection,
            )
        )
    return reviewers


def _run_parallel_reviewers(
        reviewers: Sequence[ReviewerRuntime],
        *,
        project_dir: str | Path,
        requirement_name: str,
        round_index: int,
        prompt_builder: Callable[[ReviewerRuntime], str],
        label_prefix: str,
) -> list[ReviewerRuntime]:
    reviewer_list = list(reviewers)
    reviewer_index = {item.reviewer_name: index for index, item in enumerate(reviewer_list)}
    with ThreadPoolExecutor(max_workers=max(1, len(reviewers))) as executor:
        future_map = {
            executor.submit(
                run_reviewer_turn_with_recreation,
                reviewer,
                project_dir=project_dir,
                requirement_name=requirement_name,
                label=f"{label_prefix}_{reviewer.reviewer_name}_round_{round_index}",
                prompt=prompt_builder(reviewer),
            ): reviewer.reviewer_name
            for reviewer in reviewer_list
        }
        errors: list[str] = []
        for future in as_completed(future_map):
            reviewer_name = future_map[future]
            try:
                reviewer_list[reviewer_index[reviewer_name]] = future.result()
            except Exception as error:  # noqa: BLE001
                errors.append(f"{reviewer_name}: {error}")
        if errors:
            raise RuntimeError("审核器执行失败:\n" + "\n".join(errors))
    return reviewer_list


def repair_reviewer_outputs(
        reviewers: Sequence[ReviewerRuntime],
        *,
        project_dir: str | Path,
        requirement_name: str,
        round_index: int,
) -> list[ReviewerRuntime]:
    reviewer_list = list(reviewers)
    reviewer_index = {item.reviewer_name: index for index, item in enumerate(reviewer_list)}
    json_pattern = f"{sanitize_requirement_name(requirement_name)}_评审记录_*.json"
    md_pattern = f"{sanitize_requirement_name(requirement_name)}_需求评审记录_*.md"
    for repair_attempt in range(1, MAX_REVIEWER_REPAIR_ATTEMPTS + 1):
        prompts = check_reviewer_job(
            [item.reviewer_name for item in reviewer_list],
            directory=project_dir,
            task_name=REQUIREMENTS_REVIEW_TASK_NAME,
            json_pattern=json_pattern,
            md_pattern=md_pattern,
        )
        if not prompts:
            return reviewer_list
        with ThreadPoolExecutor(max_workers=max(1, len(prompts))) as executor:
            future_map = {}
            for reviewer in reviewer_list:
                fix_prompt = prompts.get(reviewer.reviewer_name)
                if not fix_prompt:
                    continue
                future_map[
                    executor.submit(
                        run_reviewer_turn_with_recreation,
                        reviewer,
                        project_dir=project_dir,
                        requirement_name=requirement_name,
                        label=f"requirements_review_fix_{reviewer.reviewer_name}_round_{round_index}_attempt_{repair_attempt}",
                        prompt=fix_prompt,
                    )
                ] = reviewer.reviewer_name
            errors: list[str] = []
            for future in as_completed(future_map):
                reviewer_name = future_map[future]
                try:
                    reviewer_list[reviewer_index[reviewer_name]] = future.result()
                except Exception as error:  # noqa: BLE001
                    errors.append(f"{reviewer_name}: {error}")
            if errors:
                raise RuntimeError("审核器修复输出失败:\n" + "\n".join(errors))
    remaining = check_reviewer_job(
        [item.reviewer_name for item in reviewer_list],
        directory=project_dir,
        task_name=REQUIREMENTS_REVIEW_TASK_NAME,
        json_pattern=json_pattern,
        md_pattern=md_pattern,
    )
    if remaining:
        raise RuntimeError("审核器多次修复后仍未按协议更新文档")
    return reviewer_list


def _run_review_feedback_loop(
        *,
        handoff: RequirementsAnalystHandoff,
        reviewers: Sequence[ReviewerRuntime],
        paths: dict[str, Path],
        requirement_name: str,
        round_index: int,
) -> tuple[RequirementsAnalystHandoff, list[ReviewerRuntime]]:
    review_msg = get_markdown_content(paths["merged_review_path"]).strip()
    if not review_msg:
        raise RuntimeError("评审未通过，但合并后的需求评审记录为空")
    ensure_empty_file(paths["ask_human_path"])
    ensure_empty_file(paths["ba_feedback_path"])
    current_handoff, payload = run_ba_turn_with_recreation(
        handoff,
        project_dir=paths["project_root"],
        label=f"requirements_review_feedback_round_{round_index}",
        prompt=review_feedback(
            review_msg,
            original_requirement_md=str(paths["original_requirement_path"].resolve()),
            ask_human_md=str(paths["ask_human_path"].resolve()),
            hitl_record_md=str(paths["hitl_record_path"].resolve()),
            requirements_clear_md=str(paths["requirements_clear_path"].resolve()),
            what_just_change=str(paths["ba_feedback_path"].resolve()),
        ),
        result_contract=build_ba_review_feedback_result_contract(paths),
    )
    if str(payload.get("status", "")).strip() == "hitl":
        question_text = get_markdown_content(paths["ask_human_path"]).strip()
        if not question_text:
            raise RuntimeError("需求分析师返回 HITL，但未写入《与人类交流.md》")
        message("需求分析师向人类发起提问:")
        message(question_text)
        human_msg = collect_multiline_input(
            title="请输入给需求分析师的补充答复",
            empty_retry_message="内容不能为空，请重新输入。",
        )
        ensure_empty_file(paths["ask_human_path"])
        current_handoff, complete_payload = run_ba_turn_with_recreation(
            current_handoff,
            project_dir=paths["project_root"],
            label=f"requirements_review_human_reply_round_{round_index}",
            prompt=human_feed_bck(
                human_msg,
                original_requirement_md=str(paths["original_requirement_path"].resolve()),
                ask_human_md=str(paths["ask_human_path"].resolve()),
                requirements_clear_md=str(paths["requirements_clear_path"].resolve()),
                hitl_record_md=str(paths["hitl_record_path"].resolve()),
            ),
            result_contract=build_ba_human_feedback_result_contract(paths),
        )
        if str(complete_payload.get("status", "")).strip() != "completed":
            raise RuntimeError("需求分析师未正确处理人类补充答复")
        ba_reply = get_markdown_content(paths["ask_human_path"]).strip()
        if not ba_reply:
            raise RuntimeError("需求分析师未写入 HITL 回复内容")
        message("需求分析师回复:")
        message(ba_reply)
    else:
        ba_reply = get_markdown_content(paths["ba_feedback_path"]).strip()
        if not ba_reply:
            raise RuntimeError("需求分析师已完成评审反馈处理，但未写入《需求分析师反馈.md》")

    def prompt_builder(reviewer: ReviewerRuntime) -> str:
        return requirements_review_reply(
            ba_reply,
            REQUIREMENTS_REVIEW_TASK_NAME,
            requirement_review_md=str(reviewer.review_md_path.resolve()),
            requirement_review_json=str(reviewer.review_json_path.resolve()),
            requirements_clear_md=str(paths["requirements_clear_path"].resolve()),
        )

    reviewer_list = _run_parallel_reviewers(
        reviewers,
        project_dir=paths["project_root"],
        requirement_name=requirement_name,
        round_index=round_index,
        prompt_builder=prompt_builder,
        label_prefix="requirements_review_reply",
    )
    reviewer_list = repair_reviewer_outputs(
        reviewer_list,
        project_dir=paths["project_root"],
        requirement_name=requirement_name,
        round_index=round_index,
    )
    return current_handoff, reviewer_list


def _shutdown_workers(
        ba_handoff: RequirementsAnalystHandoff | None,
        reviewers: Sequence[ReviewerRuntime],
        *,
        cleanup_runtime: bool,
) -> tuple[str, ...]:
    removed: list[str] = []
    seen_runtime_dirs: set[Path] = set()
    runtime_roots: set[Path] = set()
    for reviewer in reviewers:
        try:
            reviewer.worker.request_kill()
        except Exception:
            pass
        seen_runtime_dirs.add(Path(reviewer.worker.runtime_dir).expanduser().resolve())
        runtime_roots.add(Path(reviewer.worker.runtime_root).expanduser().resolve())
    if ba_handoff is not None:
        try:
            ba_handoff.worker.request_kill()
        except Exception:
            pass
        seen_runtime_dirs.add(Path(ba_handoff.worker.runtime_dir).expanduser().resolve())
        runtime_roots.add(Path(ba_handoff.worker.runtime_root).expanduser().resolve())
    if not cleanup_runtime:
        return ()
    for runtime_dir in seen_runtime_dirs:
        if runtime_dir.exists():
            import shutil
            shutil.rmtree(runtime_dir, ignore_errors=True)
            removed.append(str(runtime_dir))
    for root in runtime_roots:
        if root.exists() and root.is_dir() and not any(root.iterdir()):
            root.rmdir()
            removed.append(str(root))
    return tuple(removed)


def run_requirements_review_stage(
        argv: Sequence[str] | None = None,
        *,
        ba_handoff: RequirementsAnalystHandoff | None = None,
) -> RequirementsReviewStageResult:
    parser = build_parser()
    args = parser.parse_args(argv)
    project_dir = str(Path(args.project_dir).expanduser().resolve()) if args.project_dir else prompt_project_dir("")
    if args.requirement_name:
        requirement_name = str(args.requirement_name).strip()
    else:
        requirement_name = prompt_requirement_name_selection(project_dir, "").requirement_name

    paths = build_requirements_review_paths(project_dir, requirement_name)
    ensure_review_stage_inputs(paths, requirement_name)
    ensure_pre_development_task_record(project_dir, requirement_name)
    update_pre_development_task_status(project_dir, requirement_name, task_key="需求评审", completed=False)

    active_ba_handoff: RequirementsAnalystHandoff | None = None
    reviewer_workers: list[ReviewerRuntime] = []
    cleanup_paths: tuple[str, ...] = ()
    try:
        active_ba_handoff, _ = prepare_ba_handoff(
            project_dir=project_dir,
            requirement_name=requirement_name,
            ba_handoff=ba_handoff,
            paths=paths,
        )
        active_ba_handoff = run_human_check_loop(handoff=active_ba_handoff, paths=paths)
        cleanup_existing_review_artifacts(paths, requirement_name)
        reviewer_workers = build_reviewer_workers(project_dir=project_dir, requirement_name=requirement_name)

        def initial_prompt_builder(reviewer: ReviewerRuntime) -> str:
            return requirements_review_init(
                original_requirement_md=str(paths["original_requirement_path"].resolve()),
                hitl_record_md=str(paths["hitl_record_path"].resolve()),
                requirements_clear_md=str(paths["requirements_clear_path"].resolve()),
                requirement_review_md=str(reviewer.review_md_path.resolve()),
                requirement_review_json=str(reviewer.review_json_path.resolve()),
            )

        for round_index in range(1, MAX_REVIEW_ROUNDS + 1):
            if round_index == 1:
                reviewer_workers = _run_parallel_reviewers(
                    reviewer_workers,
                    project_dir=project_dir,
                    requirement_name=requirement_name,
                    round_index=round_index,
                    prompt_builder=initial_prompt_builder,
                    label_prefix="requirements_review_init",
                )
                reviewer_workers = repair_reviewer_outputs(
                    reviewer_workers,
                    project_dir=project_dir,
                    requirement_name=requirement_name,
                    round_index=round_index,
                )
            else:
                active_ba_handoff, reviewer_workers = _run_review_feedback_loop(
                    handoff=active_ba_handoff,
                    reviewers=reviewer_workers,
                    paths=paths,
                    requirement_name=requirement_name,
                    round_index=round_index,
                )

            passed = task_done(
                directory=project_dir,
                file_path=paths["pre_development_path"],
                task_name=REQUIREMENTS_REVIEW_TASK_NAME,
                json_pattern=f"{sanitize_requirement_name(requirement_name)}_评审记录_*.json",
                md_pattern=f"{sanitize_requirement_name(requirement_name)}_需求评审记录_*.md",
                md_output_name=paths["merged_review_path"].name,
            )
            if passed:
                cleanup_paths = _shutdown_workers(active_ba_handoff, reviewer_workers, cleanup_runtime=True)
                return RequirementsReviewStageResult(
                    project_dir=project_dir,
                    requirement_name=requirement_name,
                    merged_review_path=str(paths["merged_review_path"].resolve()),
                    rounds_used=round_index,
                    passed=True,
                    cleanup_paths=cleanup_paths,
                )

        raise RuntimeError(f"需求评审超过最大轮数 {MAX_REVIEW_ROUNDS}，仍未全部通过")
    except Exception:
        _shutdown_workers(active_ba_handoff, reviewer_workers, cleanup_runtime=False)
        raise


def main(argv: Sequence[str] | None = None) -> int:
    redirected, launch = maybe_launch_tui(argv, route="review", action="stage.a03.start")
    if redirected:
        return int(launch)
    try:
        result = run_requirements_review_stage(list(launch))
    except Exception as error:  # noqa: BLE001
        message(error)
        return 1
    message("需求评审完成")
    message(result.merged_review_path)
    message("下一步进入详细设计阶段（待接入）")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        cleaned_sessions = cleanup_registered_tmux_workers(reason="keyboard_interrupt")
        if cleaned_sessions:
            message(f"\n已清理 tmux 会话: {', '.join(cleaned_sessions)}")
        raise SystemExit(130)
