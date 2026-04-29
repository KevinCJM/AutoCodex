# -*- encoding: utf-8 -*-
"""
@File: A02_RequirementsAnalysis.py
@Modify Time: 2026/4/17
@Author: Kevin-Chen
@Descriptions: 兼容层，已拆分为 A02_RequirementIntake / A03_RequirementsClarification
"""

from __future__ import annotations

from contextlib import contextmanager
import A02_RequirementIntake as intake_module
import A03_RequirementsClarification as clarification_module
from types import SimpleNamespace
from pathlib import Path
from typing import Sequence

from canopy_core.runtime.vendor_catalog import get_default_model_for_vendor
from A02_RequirementIntake import (
    INPUT_TYPE_CHOICES,
    DEFAULT_NOTION_EFFORT,
    DEFAULT_NOTION_MODEL,
    NOTION_RUNTIME_ROOT_NAME,
    NOTION_STAGE_NAME,
    NOTION_TURN_PHASE,
    InputReadResult,
    NotionInputRetryRequired,
    RequirementInputRetryRequired,
    RequirementIntakeRequest,
    RequirementIntakeStageResult,
    build_parser as build_intake_parser,
    build_notion_followup_prompt,
    build_notion_hitl_paths,
    build_notion_retry_message,
    build_output_path,
    cleanup_notion_runtime_paths,
    cleanup_runtime_paths,
    collect_request as collect_intake_request,
    collect_text_input_interactive,
    collect_text_input_noninteractive,
    ensure_non_empty_content,
    extract_text_from_docx,
    extract_text_from_local_file,
    extract_text_from_markdown_or_text,
    extract_text_from_pdf,
    format_notion_failure_message,
    load_json_object,
    main as intake_main,
    maybe_confirm_overwrite,
    normalize_input_type,
    prompt_input_type,
    read_input_content,
    render_agent_boot_progress_line,
    render_notion_progress_line,
    render_notion_tmux_start_summary,
    reprompt_request_for_input_source,
    resolve_input_file_path,
    run_notion_reader,
    run_requirement_intake_stage,
    validate_notion_status,
    write_requirement_file,
)
from A03_RequirementsClarification import (
    DEFAULT_MODEL_BY_VENDOR,
    RequirementsAnalysisAgentSelection,
    RequirementsAnalysisResult,
    RequirementsAnalystHandoff,
    REQUIREMENTS_CLARIFICATION_STAGE_NAME,
    REQUIREMENTS_CLARIFICATION_TURN_PHASE,
    RequirementsClarificationAgentSelection,
    RequirementsClarificationStageResult,
    RequirementsStageResult,
    collect_requirements_clarification_agent_selection,
    collect_request as collect_clarification_request,
    has_existing_requirements_clarification,
    load_json_object as load_clarification_json_object,
    normalize_effort_choice,
    normalize_model_choice,
    normalize_vendor_choice,
    prompt_effort,
    prompt_model,
    prompt_proxy_url,
    prompt_recreate_requirements_clarification_agent,
    prompt_vendor,
    prompt_yes_no,
    render_requirements_clarification_progress_line,
    render_requirements_clarification_tmux_start_summary,
    render_requirements_clarification_stage_start,
    reuse_existing_requirements_clarification,
    run_requirements_clarification,
    run_requirements_clarification_stage,
    should_reuse_existing_requirements_clarification,
    stdin_is_interactive,
    worker_has_provider_auth_error,
)
from T08_pre_development import (
    build_pre_development_task_record_path,
    build_pre_development_task_record_payload,
    ensure_pre_development_task_record,
    mark_requirement_clarification_completed,
    mark_requirement_intake_completed,
)
from T09_terminal_ops import SingleLineSpinnerMonitor, clear_pending_tty_input, maybe_launch_tui, message
from T02_tmux_agents import TmuxBatchWorker
from T05_hitl_runtime import build_prefixed_sha256
from canopy_core.stage_kernel.shared_review import is_agent_config_error
from T12_requirements_common import (
    DEFAULT_REQUIREMENTS_ANALYSIS_EFFORT,
    DEFAULT_REQUIREMENTS_ANALYSIS_MODEL,
    DEFAULT_REQUIREMENTS_ANALYSIS_VENDOR,
    RequirementNameSelection,
    build_legacy_requirements_hitl_record_path,
    build_requirements_analysis_paths,
    build_requirements_clarification_paths,
    ensure_requirements_hitl_record_file,
    list_existing_requirements,
    prompt_project_dir,
    prompt_requirement_name,
    prompt_requirement_name_selection,
    prompt_requirement_name_with_existing,
    prompt_with_default,
    resolve_existing_directory,
    sanitize_requirement_name,
)


REQUIREMENTS_ANALYSIS_STAGE_NAME = REQUIREMENTS_CLARIFICATION_STAGE_NAME
REQUIREMENTS_ANALYSIS_TURN_PHASE = REQUIREMENTS_CLARIFICATION_TURN_PHASE
collect_requirements_analysis_agent_selection = collect_requirements_clarification_agent_selection
render_requirements_analysis_progress_line = render_requirements_clarification_progress_line
render_requirements_analysis_tmux_start_summary = render_requirements_clarification_tmux_start_summary
cleanup_stage_runtime_paths = cleanup_runtime_paths
collect_request = collect_intake_request
build_parser = build_intake_parser


_INTAKE_BOOLEAN_FLAGS = {
    "--overwrite",
    "--reuse-existing-original-requirement",
    "--yes",
    "--no-tui",
    "--legacy-cli",
}
_INTAKE_VALUE_FLAGS = {
    "--project-dir",
    "--requirement-name",
    "--input-type",
    "--input-value",
}


def _extract_passthrough_option(argv: Sequence[str], flag: str) -> str:
    try:
        index = list(argv).index(flag)
    except ValueError:
        return ""
    if index + 1 >= len(argv):
        return ""
    return str(argv[index + 1]).strip()


def _build_intake_argv(argv: Sequence[str]) -> list[str]:
    raw = list(argv)
    filtered: list[str] = []
    index = 0
    while index < len(raw):
        token = raw[index]
        if token in _INTAKE_BOOLEAN_FLAGS:
            filtered.append(token)
            index += 1
            continue
        if token in _INTAKE_VALUE_FLAGS:
            filtered.append(token)
            if index + 1 < len(raw):
                filtered.append(raw[index + 1])
            index += 2
            continue
        index += 1
    return filtered


@contextmanager
def _patched_requirements_runtime_symbols():
    replacements = {
        "TmuxBatchWorker": TmuxBatchWorker,
        "SingleLineSpinnerMonitor": SingleLineSpinnerMonitor,
        "prompt_yes_no": prompt_yes_no,
        "stdin_is_interactive": stdin_is_interactive,
        "prompt_vendor": prompt_vendor,
        "prompt_model": prompt_model,
        "prompt_effort": prompt_effort,
        "prompt_proxy_url": prompt_proxy_url,
    }
    originals: list[tuple[object, str, object]] = []
    try:
        for module in (intake_module, clarification_module):
            for name, value in replacements.items():
                if hasattr(module, name):
                    originals.append((module, name, getattr(module, name)))
                    setattr(module, name, value)
        yield
    finally:
        for module, name, value in reversed(originals):
            setattr(module, name, value)


def run_requirement_intake_stage(argv: Sequence[str] | None = None) -> RequirementIntakeStageResult:
    with _patched_requirements_runtime_symbols():
        return intake_module.run_requirement_intake_stage(argv)


def run_notion_reader(project_dir: str | Path, notion_url: str, requirement_name: str) -> InputReadResult:
    with _patched_requirements_runtime_symbols():
        return intake_module.run_notion_reader(project_dir, notion_url, requirement_name)


def run_requirements_analysis(
        project_dir: str | Path,
        requirement_name: str,
        *,
        vendor: str = DEFAULT_REQUIREMENTS_ANALYSIS_VENDOR,
        model: str = DEFAULT_REQUIREMENTS_ANALYSIS_MODEL,
        reasoning_effort: str = DEFAULT_REQUIREMENTS_ANALYSIS_EFFORT,
        proxy_url: str = "",
        resume_existing: bool = False,
        preserve_ba_worker: bool = False,
) -> RequirementsClarificationStageResult:
    with _patched_requirements_runtime_symbols():
        return clarification_module.run_requirements_clarification(
            project_dir,
            requirement_name,
            vendor=vendor,
            model=model,
            reasoning_effort=reasoning_effort,
            proxy_url=proxy_url,
            resume_existing=resume_existing,
            preserve_ba_worker=preserve_ba_worker,
        )


def collect_requirements_analysis_agent_selection(args) -> RequirementsClarificationAgentSelection:
    interactive = stdin_is_interactive()
    vendor_value = str(getattr(args, "vendor", "") or "").strip()
    proxy_url = str(getattr(args, "proxy_url", "") or "").strip()
    try:
        if vendor_value:
            vendor = normalize_vendor_choice(vendor_value)
        elif interactive:
            vendor = prompt_vendor(DEFAULT_REQUIREMENTS_ANALYSIS_VENDOR)
        else:
            raise RuntimeError("需求澄清阶段需要选择厂商；非交互模式请传入 --vendor、--model、--effort。")

        model_value = str(getattr(args, "model", "") or "").strip()
        if model_value:
            model = normalize_model_choice(vendor, model_value)
        elif interactive:
            model = prompt_model(vendor, get_default_model_for_vendor(vendor))
        else:
            raise RuntimeError("需求澄清阶段需要选择模型；非交互模式请传入 --vendor、--model、--effort。")

        effort_value = str(getattr(args, "effort", "") or "").strip()
        if effort_value:
            reasoning_effort = normalize_effort_choice(vendor, model, effort_value)
        elif interactive:
            reasoning_effort = prompt_effort(vendor, model, DEFAULT_REQUIREMENTS_ANALYSIS_EFFORT)
        else:
            raise RuntimeError("需求澄清阶段需要选择推理强度；非交互模式请传入 --vendor、--model、--effort。")

        if interactive:
            proxy_url = prompt_proxy_url(proxy_url)
    except Exception as error:  # noqa: BLE001
        if not interactive or not is_agent_config_error(error):
            raise
        message(f"需求分析师模型配置不可用: {error}\n请重新选择厂商、模型和推理强度。")
        vendor = prompt_vendor(DEFAULT_REQUIREMENTS_ANALYSIS_VENDOR)
        model = prompt_model(vendor, get_default_model_for_vendor(vendor))
        reasoning_effort = prompt_effort(vendor, model, DEFAULT_REQUIREMENTS_ANALYSIS_EFFORT)
        proxy_url = prompt_proxy_url(proxy_url)

    return RequirementsClarificationAgentSelection(
        vendor=vendor,
        model=model,
        reasoning_effort=reasoning_effort,
        proxy_url=proxy_url,
    )


def run_requirements_stage(
        argv: Sequence[str] | None = None,
        *,
        preserve_ba_worker: bool = False,
) -> RequirementsClarificationStageResult:
    intake_result = run_requirement_intake_stage(argv)
    clear_pending_tty_input()
    message("进入需求澄清阶段")
    clarification_args = [
        "--project-dir",
        intake_result.project_dir,
        "--requirement-name",
        intake_result.requirement_name,
    ]
    if argv:
        passthrough = list(argv)
        for flag in ("--vendor", "--model", "--effort", "--proxy-url", "--yes", "--overwrite"):
            if flag in passthrough:
                index = passthrough.index(flag)
                clarification_args.append(flag)
                if flag not in {"--yes", "--overwrite"} and index + 1 < len(passthrough):
                    clarification_args.append(passthrough[index + 1])
    return run_requirements_clarification_stage(
        clarification_args,
        preserve_ba_worker=preserve_ba_worker,
    )


def main(argv: Sequence[str] | None = None) -> int:
    redirected, launch = maybe_launch_tui(argv, route="requirements", action="stage.a02.start")
    if redirected:
        return int(launch)
    try:
        raw_args = list(launch)
        intake_result = run_requirement_intake_stage(_build_intake_argv(raw_args))
        clear_pending_tty_input()
        message("进入需求澄清阶段")
        project_dir = intake_result.project_dir
        requirement_name = intake_result.requirement_name
        selection_args = SimpleNamespace(
            vendor=_extract_passthrough_option(raw_args, "--vendor"),
            model=_extract_passthrough_option(raw_args, "--model"),
            effort=_extract_passthrough_option(raw_args, "--effort"),
            proxy_url=_extract_passthrough_option(raw_args, "--proxy-url"),
            overwrite="--overwrite" in raw_args,
        )
        if has_existing_requirements_clarification(project_dir, requirement_name):
            if should_reuse_existing_requirements_clarification(
                    project_dir,
                    requirement_name,
                    overwrite=bool(selection_args.overwrite),
                    interactive=stdin_is_interactive(),
            ):
                message("复用已有的需求澄清，直接进入需求评审阶段")
                result = reuse_existing_requirements_clarification(project_dir, requirement_name)
            else:
                message("不直接复用已有需求澄清，将启动需求分析师基于现有澄清继续核验")
                selection = collect_requirements_analysis_agent_selection(selection_args)
                message(render_requirements_clarification_stage_start(selection))
                result = run_requirements_analysis(
                    project_dir,
                    requirement_name,
                    vendor=selection.vendor,
                    model=selection.model,
                    reasoning_effort=selection.reasoning_effort,
                    proxy_url=selection.proxy_url,
                    resume_existing=True,
                    preserve_ba_worker=False,
                )
        else:
            message("执行摘要: 未检测到可复用的需求澄清，需要启动需求分析师智能体执行需求澄清；请为需求分析师选择厂商、模型、推理强度、代理端口。")
            selection = collect_requirements_analysis_agent_selection(selection_args)
            message(render_requirements_clarification_stage_start(selection))
            result = run_requirements_analysis(
                project_dir,
                requirement_name,
                vendor=selection.vendor,
                model=selection.model,
                reasoning_effort=selection.reasoning_effort,
                proxy_url=selection.proxy_url,
                resume_existing=False,
                preserve_ba_worker=False,
            )
        mark_requirement_clarification_completed(project_dir, requirement_name)
        if result.cleanup_paths:
            cleanup_runtime_paths(result.cleanup_paths)
            result = RequirementsClarificationStageResult(
                project_dir=result.project_dir,
                requirement_name=result.requirement_name,
                requirements_clear_path=result.requirements_clear_path,
                cleanup_paths=(),
                ba_handoff=result.ba_handoff,
            )
    except Exception as error:  # noqa: BLE001
        message(error)
        return 1
    message("需求澄清完成")
    message(result.requirements_clear_path)
    return 0
