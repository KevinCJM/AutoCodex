# -*- encoding: utf-8 -*-
"""
@File: A02_RequirementsAnalysis.py
@Modify Time: 2026/4/17
@Author: Kevin-Chen
@Descriptions: 兼容层，已拆分为 A02_RequirementIntake / A03_RequirementsClarification
"""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

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
from T09_terminal_ops import clear_pending_tty_input, maybe_launch_tui, message
from T05_hitl_runtime import build_prefixed_sha256
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
run_requirements_analysis = run_requirements_clarification
cleanup_stage_runtime_paths = cleanup_runtime_paths
collect_request = collect_intake_request
build_parser = build_intake_parser


def collect_requirements_analysis_agent_selection(args) -> RequirementsClarificationAgentSelection:
    interactive = stdin_is_interactive()
    vendor_value = str(getattr(args, "vendor", "") or "").strip()
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
        model = prompt_model(vendor, DEFAULT_MODEL_BY_VENDOR[vendor])
    else:
        raise RuntimeError("需求澄清阶段需要选择模型；非交互模式请传入 --vendor、--model、--effort。")

    effort_value = str(getattr(args, "effort", "") or "").strip()
    if effort_value:
        reasoning_effort = normalize_effort_choice(vendor, model, effort_value)
    elif interactive:
        reasoning_effort = prompt_effort(vendor, model, DEFAULT_REQUIREMENTS_ANALYSIS_EFFORT)
    else:
        raise RuntimeError("需求澄清阶段需要选择推理强度；非交互模式请传入 --vendor、--model、--effort。")

    proxy_url = str(getattr(args, "proxy_url", "") or "").strip()
    if interactive:
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
        result = run_requirements_stage(list(launch), preserve_ba_worker=False)
    except Exception as error:  # noqa: BLE001
        message(error)
        return 1
    message("需求澄清完成")
    message(result.requirements_clear_path)
    return 0
