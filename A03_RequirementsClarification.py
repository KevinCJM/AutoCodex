# -*- encoding: utf-8 -*-
"""
@File: A03_RequirementsClarification.py
@Modify Time: 2026/4/17
@Author: Kevin-Chen
@Descriptions: 需求澄清阶段
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from dataclasses import dataclass, field
from pathlib import Path
import uuid
from typing import Callable, Sequence

from tmux_core.runtime.vendor_catalog import get_default_model_for_vendor
from A01_Routing_LayerPlanning import (
    DEFAULT_MODEL_BY_VENDOR,
    normalize_effort_choice,
    normalize_model_choice,
    normalize_vendor_choice,
    prompt_effort,
    prompt_model,
    prompt_vendor,
)
from Prompt_03_RequirementsClarification import (
    REQUIREMENTS_STATUS_OK,
    REQUIREMENTS_STATUS_SCHEMA_VERSION,
    fintech_ba,
    hitl_bck,
    requirements_understand,
    resume_requirements_understand,
)
from T01_tools import get_markdown_content
from T02_tmux_agents import (
    DEFAULT_COMMAND_TIMEOUT_SEC,
    AgentRunConfig,
    TmuxBatchWorker,
    TmuxControlUnavailable,
    TmuxMutationOutcomeUnknown,
    cleanup_registered_tmux_workers,
    is_agent_ready_timeout_error,
    is_provider_auth_error,
    is_runtime_shutdown_error,
    is_worker_death_error,
    load_worker_from_state_path,
    worker_state_is_prelaunch_active,
)
from T05_hitl_runtime import (
    GrillSessionError,
    HitlPromptContext,
    read_grill_session_header,
    run_hitl_agent_loop,
)
from tmux_core.runtime.grill import (
    REQUIREMENTS_MODE_CHOICES,
    GrillTurnProfile,
    RequirementsMode,
    normalize_requirements_mode,
)
from tmux_core.runtime.graphify import GraphifyMode
from tmux_core.stage_kernel.agent_intervention import (
    request_file_noncompliance_intervention,
    wait_for_worker_startup_intervention,
)
from tmux_core.stage_kernel.shared_review import (
    is_agent_config_error,
    resolve_main_ponytail_mode,
    resolve_workflow_graphify_mode,
    resolve_workflow_graphify_config,
    resolve_workflow_requirements_mode,
)
from tmux_core.stage_kernel.requirement_concurrency import requirement_concurrency_lock
from tmux_core.stage_kernel.stage_audit import (
    StageAuditRunContext,
    append_stage_audit_record,
    begin_stage_audit_run,
    record_before_cleanup,
)
from T08_pre_development import mark_requirement_clarification_completed
from T09_terminal_ops import (
    PROMPT_BACK_VALUE,
    PromptBackRequested,
    SingleLineSpinnerMonitor,
    TERMINAL_SPINNER_FRAMES,
    maybe_launch_tui,
    message,
    prompt_metadata,
    terminal_ui_is_interactive,
    prompt_yes_no as terminal_prompt_yes_no,
)
from T12_requirements_common import (
    DEFAULT_REQUIREMENTS_CLARIFICATION_EFFORT,
    DEFAULT_REQUIREMENTS_CLARIFICATION_MODEL,
    DEFAULT_REQUIREMENTS_CLARIFICATION_VENDOR,
    RequirementsAnalystHandoff,
    build_requirements_clarification_paths,
    clear_requirements_human_exchange_file,
    cleanup_runtime_paths,
    cleanup_runtime_root_if_empty,
    ensure_requirements_hitl_record_file,
    prompt_project_dir,
    prompt_requirement_name_selection,
    prompt_with_default,
    resolve_existing_directory,
    sanitize_requirement_name,
)


REQUIREMENTS_CLARIFICATION_TURN_PHASE = "requirements_clarification"
REQUIREMENTS_CLARIFICATION_STAGE_NAME = "requirements_clarification"
REQUIREMENTS_RUNTIME_ROOT_NAME = ".requirements_clarification_runtime"
PLACEHOLDER_NEXT_STEP = "下一步进入需求评审阶段（待接入）"
AUTO_HITL_RESPONSE_TEXT = """自动澄清（--yes）：
本轮为非交互全流程测试，不再等待人工补充。请基于原始需求、已有澄清记录和验收示例做最小、保守、可测试的默认决策；把所有默认假设写入澄清记录和需求澄清文档，并继续完成需求澄清。除非原始需求完全无法实现，否则不要再次发起 HITL。"""


def build_requirements_grill_paths(
    project_dir: str | Path,
    requirement_name: str,
) -> tuple[Path, Path, Path]:
    project_root = Path(project_dir).expanduser().resolve()
    safe_requirement = sanitize_requirement_name(requirement_name)
    grill_root = project_root / ".tmux_workflow" / safe_requirement / "grill"
    return grill_root, grill_root / "session.json", grill_root / "domain_drafts.json"


def build_requirements_grill_host_contract(
    *,
    requirements_mode: str,
    domain_draft_path: str | Path,
) -> str:
    mode = normalize_requirements_mode(requirements_mode)
    if mode is RequirementsMode.STANDARD:
        return ""
    lines = [
        "## A03 Grill Host Contract（高优先级）",
        "- 本轮只能提出一个需要人类决定的问题。能从 AGENTS.md、路由文件、代码、测试或配置查到的事实必须自行核实。",
        "- 进入 HITL 时，问题文档必须严格使用以下二级标题：`问题`、`为什么需要决定`、`推荐答案`、`回答方式`、`选项`、`已核实事实`。",
        "- `问题`只能有一行、一个问题；`回答方式`只能写 `select` 或 `multiline`。",
        "- 有 2 到 4 个明确选项时使用 `select`，每个选项使用一条 Markdown 列表；否则使用 `multiline` 并让选项章节为空。",
        "- 信息充分时写入的需求澄清只是待人类确认的候选。系统完成最终人工确认前，不得实施需求，也不得声称阶段已经获得共享理解。",
        "- 本 Host Contract 扩展原业务文件白名单；除此处明确列出的文件外，原文件合同继续生效。",
    ]
    if mode is RequirementsMode.GRILL_WITH_DOCS:
        draft_path = Path(domain_draft_path).expanduser().resolve()
        lines.extend(
            [
                f"- 领域文档只能写入受控运行时草稿：`{draft_path}`；禁止直接修改项目中的 CONTEXT.md、CONTEXT-MAP.md 或 docs/adr。",
                "- 该草稿是 JSON 对象：`schema_version` 固定为 `1.0`；`context_markdown` 为领域词汇表 Markdown；`adrs` 为 ADR 候选数组。",
                "- 每个 ADR 候选必须包含 `title`、`slug`、`markdown`，并把 `hard_to_reverse`、`surprising`、`real_tradeoff` 三项明确写为 true；任一条件不满足就不要加入。",
                "- CONTEXT 只能保存领域术语、定义、同义词和边界，禁止实现、文件、模块、API 或路由事实。",
                "- 没有形成任何领域术语或 ADR 时，可以不创建草稿文件。",
            ]
        )
    return "\n".join(lines)


@dataclass(frozen=True)
class RequirementsClarificationStageResult:
    project_dir: str
    requirement_name: str
    requirements_clear_path: str
    cleanup_paths: tuple[str, ...] = ()
    ba_handoff: RequirementsAnalystHandoff | None = None
    requirements_mode: str = RequirementsMode.STANDARD.value


RequirementsStageResult = RequirementsClarificationStageResult
RequirementsAnalysisResult = RequirementsClarificationStageResult


@dataclass(frozen=True)
class RequirementsClarificationAgentSelection:
    vendor: str
    model: str
    reasoning_effort: str
    proxy_url: str
    ponytail_mode: str = "off"
    requirements_mode: str = RequirementsMode.STANDARD.value
    graphify_mode: str = GraphifyMode.OFF.value
    graphify_config: dict[str, object] = field(default_factory=dict)


RequirementsAnalysisAgentSelection = RequirementsClarificationAgentSelection


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="需求澄清阶段")
    parser.add_argument("--project-dir", help="项目目录")
    parser.add_argument("--requirement-name", help="需求名称")
    parser.add_argument("--allow-previous-stage-back", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--vendor", help="需求澄清阶段厂商: codex|claude|gemini|opencode|mimo|agy|deveco")
    parser.add_argument("--model", help="需求澄清阶段模型名称")
    parser.add_argument("--effort", help="需求澄清阶段推理强度")
    parser.add_argument("--proxy-url", default="", help="需求澄清阶段代理端口或完整代理 URL")
    parser.add_argument("--ponytail-mode", choices=("off", "lite", "full", "ultra"), default="", help="Ponytail 模式")
    parser.add_argument("--graphify-mode", choices=("off", "auto", "required"), default="", help="Graphify 模式")
    parser.add_argument("--main-ponytail-mode", choices=("off", "lite", "full", "ultra"), default="", help=argparse.SUPPRESS)
    parser.add_argument("--agent-config", default="", help="智能体配置 JSON")
    parser.add_argument(
        "--requirements-mode",
        choices=REQUIREMENTS_MODE_CHOICES,
        default="",
        help="需求澄清策略: standard|grill|grill-with-docs",
    )
    parser.add_argument("--overwrite", action="store_true", help="存在需求澄清时不直接复用，而是重新执行核验")
    parser.add_argument("--yes", action="store_true", help="跳过非关键确认")
    parser.add_argument("--no-tui", action="store_true", help="显式禁用 OpenTUI")
    parser.add_argument("--legacy-cli", action="store_true", help="使用旧版 Python CLI，不跳转 OpenTUI")
    return parser


def prompt_proxy_url(default: str = "") -> str:
    return prompt_with_default("输入代理端口或完整代理 URL（可留空）", default, allow_empty=True)


def _clarification_prompt_step(step_index: int, *, allow_back: bool):
    return prompt_metadata(
        allow_back=allow_back,
        back_value=PROMPT_BACK_VALUE,
        stage_key="requirements_clarification",
        stage_step_index=step_index,
    )


def prompt_yes_no(prompt_text: str, default: bool = False) -> bool:
    return terminal_prompt_yes_no(prompt_text, default)


def stdin_is_interactive() -> bool:
    return terminal_ui_is_interactive()


def collect_auto_requirements_hitl_response(question_path: str | Path, hitl_round: int = 0) -> str:
    question_file = Path(question_path).expanduser().resolve()
    question_text = get_markdown_content(question_file).strip()
    round_label = f"第 {hitl_round} 轮" if hitl_round else "本轮"
    message(f"A03 HITL {round_label} 已由 --yes 自动回复，继续非交互流程。")
    if question_text:
        return f"{AUTO_HITL_RESPONSE_TEXT}\n\n原问题文档摘要：\n{question_text}"
    return AUTO_HITL_RESPONSE_TEXT


def has_existing_requirements_clarification(project_dir: str | Path, requirement_name: str) -> bool:
    _, requirements_clear_path, _, _ = build_requirements_clarification_paths(project_dir, requirement_name)
    return bool(get_markdown_content(requirements_clear_path).strip())


def should_reuse_existing_requirements_clarification(
        project_dir: str | Path,
        requirement_name: str,
        *,
        overwrite: bool,
        interactive: bool,
        allow_back: bool = False,
) -> bool:
    _, requirements_clear_path, _, _ = build_requirements_clarification_paths(project_dir, requirement_name)
    if not get_markdown_content(requirements_clear_path).strip():
        return False
    if not interactive:
        return not overwrite
    message(f"检测项目内已有需求澄清: {requirements_clear_path.name}")
    with _clarification_prompt_step(0, allow_back=allow_back):
        return prompt_yes_no("是否直接复用已有的需求澄清并跳入需求评审阶段", True)


def reuse_existing_requirements_clarification(
    project_dir: str | Path,
    requirement_name: str,
    *,
    requirements_mode: str = RequirementsMode.STANDARD.value,
) -> RequirementsClarificationStageResult:
    _, requirements_clear_path, _, _ = build_requirements_clarification_paths(project_dir, requirement_name)
    if not get_markdown_content(requirements_clear_path).strip():
        raise RuntimeError(f"缺少可复用的需求澄清文档: {requirements_clear_path}")
    ensure_requirements_hitl_record_file(project_dir, requirement_name)
    return RequirementsClarificationStageResult(
        project_dir=str(resolve_existing_directory(project_dir)),
        requirement_name=requirement_name,
        requirements_clear_path=str(requirements_clear_path.resolve()),
        requirements_mode=normalize_requirements_mode(requirements_mode).value,
    )


def archive_terminal_requirements_grill_session(
    project_dir: str | Path,
    requirement_name: str,
) -> Path | None:
    """Atomically retire a completed/aborted interview before an explicit rerun."""

    grill_root, grill_session_path, _ = build_requirements_grill_paths(
        project_dir,
        requirement_name,
    )
    header = read_grill_session_header(grill_session_path)
    if header is None:
        return None
    if header.state not in {"confirmed", "aborted"}:
        raise GrillSessionError(
            f"活跃 Grill 会话不能归档后重跑: state={header.state}"
        )
    archive_root = grill_root.parent / "grill_archive"
    archive_root.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%f%z")
    archive_path = archive_root / f"{timestamp}-{uuid.uuid4().hex[:8]}"
    grill_root.replace(archive_path)
    return archive_path.resolve()


def render_agent_boot_progress_line(*, tick: int) -> str:
    spinner = TERMINAL_SPINNER_FRAMES[tick % len(TERMINAL_SPINNER_FRAMES)]
    return f"{spinner} 智能体启动中..."


def render_requirements_clarification_tmux_start_summary(worker: TmuxBatchWorker) -> str:
    return "\n".join(
        [
            "需求澄清智能体已启动",
            f"runtime_dir: {worker.runtime_dir}",
            f"session_name: {worker.session_name}",
            "可使用以下命令进入会话:",
            f"  tmux attach -t {worker.session_name}",
        ]
    )


def render_requirements_clarification_progress_line(*, worker: TmuxBatchWorker, requirement_name: str, tick: int) -> str:
    try:
        state = worker.read_state()
    except Exception:  # noqa: BLE001
        state = {}
    workflow_stage = str(state.get("workflow_stage") or state.get("current_turn_phase") or "starting").strip() or "starting"
    agent_state = str(state.get("agent_state", "")).strip().upper()
    if worker_state_is_prelaunch_active(state):
        agent_state = "STARTING"
    elif agent_state not in {"DEAD", "STARTING", "READY", "BUSY"}:
        provider_phase = str(state.get("provider_phase", "")).strip().lower()
        wrapper_state = str(state.get("wrapper_state", "")).strip().upper()
        if wrapper_state == "READY" or provider_phase in {"waiting_input", "idle_ready", "completed_response"}:
            agent_state = "READY"
        elif provider_phase:
            agent_state = "BUSY"
        else:
            agent_state = "DEAD"
    health_status = str(state.get("health_status", "unknown")).strip() or "unknown"
    note = str(state.get("note", "")).strip() or workflow_stage
    status = str(state.get("status", "running")).strip() or "running"
    spinner = TERMINAL_SPINNER_FRAMES[tick % len(TERMINAL_SPINNER_FRAMES)]
    return (
        f"{spinner} 需求澄清中"
        f" | {requirement_name}:{status}/{agent_state}"
        f" | health={health_status}"
        f" | {note}"
    )


def preserve_worker_after_unhandled_stage_failure(
        worker: TmuxBatchWorker | None,
        error: BaseException,
) -> bool:
    """Keep a live worker inspectable until the runner records and marks the failure."""
    if worker is None or isinstance(error, PromptBackRequested):
        return False
    if is_runtime_shutdown_error(error) or is_worker_death_error(error):
        return False
    return True


def collect_requirements_clarification_agent_selection(args: argparse.Namespace) -> RequirementsClarificationAgentSelection:
    interactive = stdin_is_interactive()
    auto_confirm = bool(getattr(args, "yes", False))
    vendor_value = str(getattr(args, "vendor", "") or "").strip()
    proxy_url = str(getattr(args, "proxy_url", "") or "").strip()
    allow_previous_stage_back = bool(getattr(args, "allow_previous_stage_back", False))
    ponytail_mode = resolve_main_ponytail_mode(args, stage_key="requirements_clarification")
    requirements_mode = resolve_workflow_requirements_mode(
        args,
        stage_key="requirements_clarification",
    )
    graphify_mode = resolve_workflow_graphify_mode(
        args,
        stage_key="requirements_clarification",
    )
    graphify_config = resolve_workflow_graphify_config(args)
    try:
        model_value = str(getattr(args, "model", "") or "").strip()
        effort_value = str(getattr(args, "effort", "") or "").strip()
        if interactive:
            if auto_confirm and vendor_value and model_value and effort_value:
                vendor = normalize_vendor_choice(vendor_value)
                model = normalize_model_choice(vendor, model_value)
                reasoning_effort = normalize_effort_choice(vendor, model, effort_value)
            else:
                vendor = normalize_vendor_choice(vendor_value) if vendor_value else DEFAULT_REQUIREMENTS_CLARIFICATION_VENDOR
                model = model_value
                reasoning_effort = effort_value or DEFAULT_REQUIREMENTS_CLARIFICATION_EFFORT
                first_prompt_step = 0 if not vendor_value else (1 if not model_value else (2 if not effort_value else 3))
                step = first_prompt_step
                while step < 4:
                    try:
                        if step == 0:
                            with _clarification_prompt_step(
                                0,
                                allow_back=(step > first_prompt_step) or (step == first_prompt_step and allow_previous_stage_back),
                            ):
                                vendor = prompt_vendor(vendor)
                            if model:
                                try:
                                    normalize_model_choice(vendor, model)
                                except ValueError:
                                    model = ""
                            step = 1
                            continue
                        if step == 1:
                            if model_value and step < first_prompt_step:
                                model = normalize_model_choice(vendor, model_value)
                            else:
                                with _clarification_prompt_step(
                                    1,
                                    allow_back=(step > first_prompt_step) or (step == first_prompt_step and allow_previous_stage_back),
                                ):
                                    model = prompt_model(vendor, model or get_default_model_for_vendor(vendor))
                            step = 2
                            continue
                        if step == 2:
                            if effort_value and step < first_prompt_step:
                                reasoning_effort = normalize_effort_choice(vendor, model, effort_value)
                            else:
                                with _clarification_prompt_step(
                                    2,
                                    allow_back=(step > first_prompt_step) or (step == first_prompt_step and allow_previous_stage_back),
                                ):
                                    reasoning_effort = prompt_effort(vendor, model, reasoning_effort or DEFAULT_REQUIREMENTS_CLARIFICATION_EFFORT)
                            step = 3
                            continue
                        if step == 3:
                            with _clarification_prompt_step(
                                3,
                                allow_back=(step > first_prompt_step) or (step == first_prompt_step and allow_previous_stage_back),
                            ):
                                proxy_url = prompt_proxy_url(proxy_url)
                            step = 4
                            continue
                    except PromptBackRequested:
                        if step == first_prompt_step:
                            if allow_previous_stage_back:
                                raise
                            continue
                        step = max(first_prompt_step, step - 1)
        else:
            if not vendor_value:
                raise RuntimeError("需求澄清阶段需要选择厂商；非交互模式请传入 --vendor、--model、--effort。")
            vendor = normalize_vendor_choice(vendor_value)
            if not model_value:
                raise RuntimeError("需求澄清阶段需要选择模型；非交互模式请传入 --vendor、--model、--effort。")
            model = normalize_model_choice(vendor, model_value)
            if not effort_value:
                raise RuntimeError("需求澄清阶段需要选择推理强度；非交互模式请传入 --vendor、--model、--effort。")
            reasoning_effort = normalize_effort_choice(vendor, model, effort_value)
    except Exception as error:  # noqa: BLE001
        if not interactive or not is_agent_config_error(error):
            raise
        message(f"需求分析师模型配置不可用: {error}\n请重新选择厂商、模型和推理强度。")
        vendor = prompt_vendor(DEFAULT_REQUIREMENTS_CLARIFICATION_VENDOR)
        model = prompt_model(vendor, get_default_model_for_vendor(vendor))
        reasoning_effort = prompt_effort(vendor, model, DEFAULT_REQUIREMENTS_CLARIFICATION_EFFORT)
        proxy_url = prompt_proxy_url(proxy_url)

    vendor = normalize_vendor_choice(vendor)
    model = normalize_model_choice(vendor, model)
    reasoning_effort = normalize_effort_choice(vendor, model, reasoning_effort)
    return RequirementsClarificationAgentSelection(
        vendor=vendor,
        model=model,
        reasoning_effort=reasoning_effort,
        proxy_url=proxy_url,
        ponytail_mode=ponytail_mode,
        requirements_mode=requirements_mode,
        graphify_mode=graphify_mode,
        graphify_config=graphify_config,
    )


def render_requirements_clarification_stage_start(selection: RequirementsClarificationAgentSelection) -> str:
    return "\n".join(
        [
            "进入需求澄清阶段（需求分析师）",
            f"vendor: {selection.vendor}",
            f"model: {selection.model}",
            f"reasoning_effort: {selection.reasoning_effort}",
            f"proxy_url: {selection.proxy_url or '(none)'}",
            f"ponytail_mode: {getattr(selection, 'ponytail_mode', 'off') or 'off'}",
            f"requirements_mode: {getattr(selection, 'requirements_mode', RequirementsMode.STANDARD.value) or RequirementsMode.STANDARD.value}",
            f"graphify_mode: {getattr(selection, 'graphify_mode', GraphifyMode.OFF.value) or GraphifyMode.OFF.value}",
        ]
    )


def prompt_recreate_requirements_clarification_agent(
        *,
        reason_text: str,
        requirement_name: str,
        current_vendor: str,
        current_model: str,
        current_reasoning_effort: str,
        current_proxy_url: str,
        current_ponytail_mode: str,
        current_requirements_mode: str,
        current_graphify_mode: str,
        current_graphify_config: dict[str, object],
        force_model_change: bool,
) -> RequirementsClarificationAgentSelection | None:
    if not stdin_is_interactive():
        return None
    message(reason_text)
    if not prompt_yes_no("是否创建新的需求分析师继续当前阶段", True):
        return None
    while True:
        vendor = prompt_vendor(current_vendor)
        model = prompt_model(vendor, current_model if vendor == current_vendor else get_default_model_for_vendor(vendor))
        reasoning_effort = prompt_effort(vendor, model, current_reasoning_effort)
        proxy_url = prompt_proxy_url(current_proxy_url)
        if (not force_model_change) or vendor != current_vendor or model != current_model:
            selection = RequirementsClarificationAgentSelection(
                vendor=vendor,
                model=model,
                reasoning_effort=reasoning_effort,
                proxy_url=proxy_url,
                ponytail_mode=current_ponytail_mode,
                requirements_mode=current_requirements_mode,
                graphify_mode=current_graphify_mode,
                graphify_config=dict(current_graphify_config),
            )
            message(render_requirements_clarification_stage_start(selection))
            return selection
        message("需要更换模型，请选择与当前不同的厂商或模型。")


def load_json_object(file_path: str | Path) -> dict[str, object]:
    payload = json.loads(Path(file_path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("JSON 文件必须是对象")
    return payload


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


def _resolve_owned_grill_worker_state_path(
        runtime_root: str | Path,
        state_path: str | Path,
) -> Path:
    root = Path(runtime_root).expanduser().resolve()
    candidate = Path(state_path).expanduser().resolve()
    try:
        candidate.relative_to(root)
    except ValueError as error:
        raise GrillSessionError(f"Grill worker state 路径越界: {candidate}") from error
    if candidate.name != "worker.state.json":
        raise GrillSessionError(f"Grill worker state 文件名非法: {candidate}")
    return candidate


def _resolve_owned_grill_runtime_path(
        runtime_root: str | Path,
        path: str | Path,
) -> Path:
    root = Path(runtime_root).expanduser().resolve()
    candidate = Path(path).expanduser().resolve()
    try:
        candidate.relative_to(root)
    except ValueError as error:
        raise GrillSessionError(f"Grill runtime 路径越界: {candidate}") from error
    return candidate


def load_persisted_requirements_grill_selection(
        *,
        project_dir: str | Path,
        runtime_root: str | Path,
        state_path: str | Path,
        requirements_mode: str,
) -> RequirementsClarificationAgentSelection | None:
    """Recover the frozen A03 agent choice without probing tmux or prompting.

    An unfinished Grill interview owns its vendor/model configuration just as
    it owns its requirements mode.  Reading the existing worker state lets a
    dead pane be recreated with the same choice and prevents a backend restart
    from reopening the vendor/model dialogs before the pending question.
    """

    state_text = str(state_path or "").strip()
    if not state_text:
        return None
    owned_state_path = _resolve_owned_grill_worker_state_path(runtime_root, state_text)
    if not owned_state_path.exists() or not owned_state_path.is_file():
        return None
    payload = load_json_object(owned_state_path)
    work_dir_text = str(payload.get("work_dir", "") or "").strip()
    if work_dir_text and Path(work_dir_text).expanduser().resolve() != Path(project_dir).expanduser().resolve():
        raise GrillSessionError(
            f"Grill worker 项目目录不匹配: {Path(work_dir_text).expanduser().resolve()}"
        )
    config = payload.get("config", {})
    if not isinstance(config, dict):
        raise GrillSessionError("Grill worker state 的 config 必须是对象")
    vendor = str(config.get("vendor", "") or "").strip()
    model = str(config.get("model", "") or "").strip()
    effort = str(config.get("reasoning_effort", "") or "").strip()
    if not vendor or not model or not effort:
        # Legacy/minimal state cannot freeze an agent choice.  Fall back to
        # the normal selector rather than pretending its absent mode is an
        # authoritative Standard worker attached to a Grill session.
        return None
    persisted_mode = normalize_requirements_mode(
        config.get("requirements_mode", RequirementsMode.STANDARD.value)
    ).value
    expected_mode = normalize_requirements_mode(requirements_mode).value
    if persisted_mode != expected_mode:
        raise GrillSessionError(
            "Grill worker 模式与会话不匹配: "
            f"worker={persisted_mode}, session={expected_mode}"
        )
    return RequirementsClarificationAgentSelection(
        vendor=vendor,
        model=model,
        reasoning_effort=effort,
        proxy_url=str(config.get("proxy_url", "") or "").strip(),
        ponytail_mode=str(config.get("ponytail_mode", "off") or "off").strip(),
        requirements_mode=expected_mode,
        graphify_mode=str(config.get("graphify_mode", GraphifyMode.OFF.value) or GraphifyMode.OFF.value).strip(),
        graphify_config=(dict(config.get("graphify_config", {})) if isinstance(config.get("graphify_config", {}), dict) else {}),
    )


def restore_live_requirements_grill_worker(
        *,
        project_dir: str | Path,
        runtime_root: str | Path,
        state_path: str | Path,
        requirements_mode: str,
) -> TmuxBatchWorker | None:
    """Restore the exact owned worker; never create or submit from this probe."""
    owned_state_path = _resolve_owned_grill_worker_state_path(runtime_root, state_path)
    if not owned_state_path.exists():
        return None
    restored = load_worker_from_state_path(owned_state_path)
    if restored is None:
        return None
    if restored.work_dir != Path(project_dir).expanduser().resolve():
        raise GrillSessionError(
            f"Grill worker 项目目录不匹配: {restored.work_dir}"
        )
    persisted_mode = normalize_requirements_mode(
        getattr(restored.config, "requirements_mode", RequirementsMode.STANDARD.value)
    ).value
    if persisted_mode != normalize_requirements_mode(requirements_mode).value:
        raise GrillSessionError(
            "Grill worker 模式与会话不匹配: "
            f"worker={persisted_mode}, session={requirements_mode}"
        )
    if not restored.session_exists():
        return None
    if not restored.pane_id or not restored.target_exists(restored.pane_id):
        return None
    if restored.pane_dead():
        return None
    return restored


def run_requirements_clarification(
        project_dir: str | Path,
        requirement_name: str,
        *,
        vendor: str = DEFAULT_REQUIREMENTS_CLARIFICATION_VENDOR,
        model: str = DEFAULT_REQUIREMENTS_CLARIFICATION_MODEL,
        reasoning_effort: str = DEFAULT_REQUIREMENTS_CLARIFICATION_EFFORT,
        proxy_url: str = "",
        ponytail_mode: str = "off",
        requirements_mode: str = RequirementsMode.STANDARD.value,
        graphify_mode: str = GraphifyMode.OFF.value,
        graphify_config: dict[str, object] | None = None,
        resume_existing: bool = False,
        preserve_ba_worker: bool = False,
        human_input_provider: Callable[..., str] | None = None,
        audit_context: StageAuditRunContext | None = None,
) -> RequirementsClarificationStageResult:
    project_root = resolve_existing_directory(project_dir)
    runtime_root = project_root / REQUIREMENTS_RUNTIME_ROOT_NAME
    original_requirement_path, requirements_clear_path, ask_human_path, hitl_record_path = (
        build_requirements_clarification_paths(project_root, requirement_name)
    )
    hitl_record_path = ensure_requirements_hitl_record_file(project_root, requirement_name)
    _, grill_session_path, grill_domain_draft_path = build_requirements_grill_paths(
        project_root,
        requirement_name,
    )
    if not original_requirement_path.exists() or not original_requirement_path.read_text(encoding="utf-8").strip():
        raise RuntimeError(f"缺少原始需求文档: {original_requirement_path}")

    current_vendor = vendor
    current_model = model
    current_reasoning_effort = reasoning_effort
    current_proxy_url = proxy_url
    current_ponytail_mode = ponytail_mode
    current_requirements_mode = normalize_requirements_mode(requirements_mode).value
    current_graphify_mode = graphify_mode
    current_graphify_config = dict(graphify_config or {})
    existing_grill_header = read_grill_session_header(grill_session_path)
    if (
        existing_grill_header is not None
        and existing_grill_header.state not in {"confirmed", "aborted"}
        and existing_grill_header.requirements_mode != current_requirements_mode
    ):
        raise GrillSessionError(
            "活跃 Grill 会话禁止中途切换模式: "
            f"persisted={existing_grill_header.requirements_mode}, "
            f"requested={current_requirements_mode}"
        )
    resumable_worker_state_path = (
        existing_grill_header.active_worker_state_path
        if existing_grill_header is not None
        and existing_grill_header.state not in {"confirmed", "aborted"}
        else ""
    )
    persisted_grill_selection = load_persisted_requirements_grill_selection(
        project_dir=project_root,
        runtime_root=runtime_root,
        state_path=resumable_worker_state_path,
        requirements_mode=current_requirements_mode,
    )
    if persisted_grill_selection is not None:
        current_vendor = persisted_grill_selection.vendor
        current_model = persisted_grill_selection.model
        current_reasoning_effort = persisted_grill_selection.reasoning_effort
        current_proxy_url = persisted_grill_selection.proxy_url
        current_ponytail_mode = persisted_grill_selection.ponytail_mode
        current_graphify_mode = persisted_grill_selection.graphify_mode
        current_graphify_config = persisted_grill_selection.graphify_config
    current_resume_existing = bool(resume_existing)
    keep_worker_alive = False
    worker: TmuxBatchWorker | None = None
    runtime_dir = runtime_root
    owned_runtime_dirs: set[Path] = set()

    progress_monitor = SingleLineSpinnerMonitor(
        frame_builder=lambda tick: render_requirements_clarification_progress_line(
            worker=worker,
            requirement_name=requirement_name,
            tick=tick,
        ),
        interval_sec=0.2,
    )
    boot_progress_monitor = SingleLineSpinnerMonitor(
        frame_builder=lambda tick: render_agent_boot_progress_line(tick=tick),
        interval_sec=0.2,
    )
    boot_progress_active = False
    progress_active = False
    stage_completed = False

    def start_boot_progress() -> None:
        nonlocal boot_progress_active
        if boot_progress_active:
            return
        boot_progress_monitor.start()
        boot_progress_active = True

    def stop_boot_progress() -> None:
        nonlocal boot_progress_active
        if not boot_progress_active:
            return
        try:
            boot_progress_monitor.stop()
        except Exception:
            # Progress transport is diagnostic only and must not replace the stage failure.
            pass
        finally:
            boot_progress_active = False

    def start_progress() -> None:
        nonlocal progress_active
        if progress_active:
            return
        stop_boot_progress()
        progress_monitor.start()
        progress_active = True

    def stop_progress() -> None:
        nonlocal progress_active
        if not progress_active:
            return
        try:
            progress_monitor.stop()
        except Exception:
            # Progress transport is diagnostic only and must not replace the stage failure.
            pass
        finally:
            progress_active = False

    def handle_worker_started(live_worker: TmuxBatchWorker) -> None:
        stop_boot_progress()
        message(render_requirements_clarification_tmux_start_summary(live_worker))

    try:
        while True:
            try:
                if worker is None and resumable_worker_state_path:
                    worker = restore_live_requirements_grill_worker(
                        project_dir=project_root,
                        runtime_root=runtime_root,
                        state_path=resumable_worker_state_path,
                        requirements_mode=current_requirements_mode,
                    )
                    resumable_worker_state_path = ""
                    if worker is not None:
                        current_vendor = worker.config.vendor.value
                        current_model = worker.config.model
                        current_reasoning_effort = worker.config.reasoning_effort
                        current_proxy_url = worker.config.proxy_url
                        current_ponytail_mode = worker.config.ponytail_mode
                        current_graphify_mode = worker.config.graphify_mode
                        current_graphify_config = dict(worker.config.graphify_config)
                        message(f"恢复现有需求分析师会话: tmux attach -t {worker.session_name}")
                if worker is None:
                    worker = TmuxBatchWorker(
                        worker_id="requirements-analyst",
                        work_dir=project_root,
                        config=AgentRunConfig(
                            vendor=current_vendor,
                            model=current_model,
                            reasoning_effort=current_reasoning_effort,
                            proxy_url=current_proxy_url,
                            ponytail_mode=current_ponytail_mode,
                            requirements_mode=current_requirements_mode,
                            graphify_mode=current_graphify_mode,
                            graphify_config=current_graphify_config,
                        ),
                        runtime_root=runtime_root,
                    )
                set_runtime_metadata = getattr(worker, "set_runtime_metadata", None)
                if callable(set_runtime_metadata):
                    set_runtime_metadata(
                        project_dir=str(project_root),
                        requirement_name=requirement_name,
                        workflow_action="stage.a03.start",
                    )
            except Exception as error:  # noqa: BLE001
                stop_progress()
                stop_boot_progress()
                if not is_agent_config_error(error):
                    raise
                selection = prompt_recreate_requirements_clarification_agent(
                    reason_text=f"需求分析师模型配置不可用: {error}\n请重新选择模型后继续当前阶段。",
                    requirement_name=requirement_name,
                    current_vendor=current_vendor,
                    current_model=current_model,
                    current_reasoning_effort=current_reasoning_effort,
                    current_proxy_url=current_proxy_url,
                    current_ponytail_mode=current_ponytail_mode,
                    current_requirements_mode=current_requirements_mode,
                    current_graphify_mode=current_graphify_mode,
                    current_graphify_config=current_graphify_config,
                    force_model_change=False,
                )
                if selection is not None:
                    current_vendor = selection.vendor
                    current_model = selection.model
                    current_reasoning_effort = selection.reasoning_effort
                    current_proxy_url = selection.proxy_url
                    current_ponytail_mode = selection.ponytail_mode
                    current_requirements_mode = selection.requirements_mode
                    current_graphify_mode = selection.graphify_mode
                    current_graphify_config = dict(selection.graphify_config)
                    current_resume_existing = current_resume_existing or bool(get_markdown_content(requirements_clear_path).strip())
                    keep_worker_alive = False
                    worker = None
                    continue
                raise RuntimeError("需求分析师模型配置不可用，且用户未重新选择模型") from error
            runtime_dir = worker.runtime_dir
            owned_runtime_dirs.add(Path(runtime_dir).expanduser().resolve())
            stage_status_path = runtime_dir / "requirements_clarification_status.json"
            turns_root = runtime_dir / "turns"
            latest_grill_header = read_grill_session_header(grill_session_path)
            if (
                latest_grill_header is not None
                and latest_grill_header.state == "turn_in_progress"
                and latest_grill_header.turn_status_path
            ):
                persisted_turn_status = _resolve_owned_grill_runtime_path(
                    runtime_root,
                    latest_grill_header.turn_status_path,
                )
                turns_root = persisted_turn_status.parent.parent
                persisted_stage_status = latest_grill_header.turn_stage_status_path
                if not persisted_stage_status and latest_grill_header.active_runtime_dir:
                    persisted_stage_status = str(
                        Path(latest_grill_header.active_runtime_dir)
                        / "requirements_clarification_status.json"
                    )
                if persisted_stage_status:
                    stage_status_path = _resolve_owned_grill_runtime_path(
                        runtime_root,
                        persisted_stage_status,
                    )
            grill_host_contract = build_requirements_grill_host_contract(
                requirements_mode=current_requirements_mode,
                domain_draft_path=grill_domain_draft_path,
            )

            def with_grill_host_contract(prompt: str) -> str:
                if not grill_host_contract:
                    return prompt
                return f"{prompt.rstrip()}\n\n{grill_host_contract}"

            def initial_prompt_builder(context: HitlPromptContext) -> str:
                prompt_builder = resume_requirements_understand if current_resume_existing else requirements_understand
                return with_grill_host_contract(
                    prompt_builder(
                        fintech_ba,
                        original_requirement_md=str(original_requirement_path.resolve()),
                        requirements_clear_md=str(Path(context.output_path).resolve()),
                        ask_human_md=str(Path(context.question_path).resolve()),
                        hitl_record_md=str(Path(context.record_path).resolve()),
                    )
                )

            def hitl_prompt_builder(human_msg: str, context: HitlPromptContext) -> str:
                return with_grill_host_contract(
                    hitl_bck(
                        human_msg,
                        original_requirement_md=str(original_requirement_path.resolve()),
                        hitl_record_md=str(Path(context.record_path).resolve()),
                        requirements_clear_md=str(Path(context.output_path).resolve()),
                        ask_human_md=str(Path(context.question_path).resolve()),
                    )
                )

            def audit_before_question_clear(context: HitlPromptContext) -> None:
                record_before_cleanup(
                    audit_context,
                    {"ask_human": context.question_path},
                    metadata={"trigger": "hitl_question_clear"},
                    hitl_round_index=context.hitl_round,
                )

            def audit_hitl_question(context: HitlPromptContext, _decision: object) -> None:
                append_stage_audit_record(
                    audit_context,
                    event_type="hitl_question",
                    source_paths={"ask_human": context.question_path},
                    hitl_round_index=context.hitl_round,
                )

            def audit_hitl_answer(context: HitlPromptContext, human_msg: str, human_history_path: Path) -> None:
                source_paths: dict[str, str | Path | None] = {
                    "human_answer": "",
                    "hitl_record": context.record_path,
                }
                if human_history_path:
                    source_paths["runtime_human_answer"] = human_history_path
                append_stage_audit_record(
                    audit_context,
                    event_type="hitl_answer",
                    source_paths=source_paths,
                    metadata={
                        "human_answer_source": "runtime_payload",
                        "runtime_human_answer": str(Path(human_history_path).expanduser().resolve()) if human_history_path else "",
                    },
                    hitl_round_index=context.hitl_round,
                    snapshot_overrides={"human_answer": human_msg},
                )

            try:
                hitl_loop_kwargs: dict[str, object] = {}
                if human_input_provider is not None:
                    hitl_loop_kwargs["human_input_provider"] = human_input_provider

                def replace_dead_grill_worker(
                    dead_worker: object,
                    _error: BaseException,
                ) -> TmuxBatchWorker:
                    nonlocal worker, runtime_dir
                    dead_config = getattr(dead_worker, "config", None)
                    if not isinstance(dead_config, AgentRunConfig):
                        raise GrillSessionError(
                            "死亡 Grill worker 缺少可复用的 AgentRunConfig，拒绝猜测模型配置"
                        )
                    replacement = TmuxBatchWorker(
                        worker_id="requirements-analyst",
                        work_dir=project_root,
                        config=dead_config,
                        runtime_root=runtime_root,
                    )
                    set_runtime_metadata = getattr(replacement, "set_runtime_metadata", None)
                    if callable(set_runtime_metadata):
                        set_runtime_metadata(
                            project_dir=str(project_root),
                            requirement_name=requirement_name,
                            workflow_action="stage.a03.start",
                        )
                    worker = replacement
                    runtime_dir = replacement.runtime_dir
                    owned_runtime_dirs.add(Path(runtime_dir).expanduser().resolve())
                    message(
                        "需求分析师会话已死亡，按原厂商、模型和模式自动重建；"
                        "系统会先复检上一轮文件合同，确认未完成后才投递一次。"
                    )
                    return replacement

                if current_requirements_mode != RequirementsMode.STANDARD.value:
                    hitl_loop_kwargs.update(
                        {
                            "requirements_mode": current_requirements_mode,
                            "grill_session_path": grill_session_path,
                            "grill_domain_draft_path": grill_domain_draft_path,
                            "grill_project_dir": project_root,
                            "grill_turn_profile_factory": lambda question_seq: GrillTurnProfile(
                                mode=current_requirements_mode,
                                question_seq=question_seq,
                            ),
                            "replace_dead_worker": replace_dead_grill_worker,
                        }
                    )
                def handle_startup_intervention(live_worker: object, error: object) -> None:
                    stop_progress()
                    stop_boot_progress()
                    wait_for_worker_startup_intervention(
                        live_worker,
                        error=error,
                        stage_label=REQUIREMENTS_CLARIFICATION_STAGE_NAME,
                        role_label=str(getattr(live_worker, "session_name", "") or "需求分析师"),
                    )

                def handle_grill_contract_intervention(
                    live_worker: object,
                    error: GrillSessionError,
                    context: HitlPromptContext,
                    attempts_used: int,
                ) -> str:
                    stop_progress()
                    stop_boot_progress()
                    target_paths: list[str | Path] = [context.question_path, context.record_path]
                    if current_requirements_mode == RequirementsMode.GRILL_WITH_DOCS.value:
                        target_paths.append(grill_domain_draft_path)
                    return request_file_noncompliance_intervention(
                        stage_label=REQUIREMENTS_CLARIFICATION_STAGE_NAME,
                        role_label=str(getattr(live_worker, "session_name", "") or "需求分析师"),
                        worker=live_worker,
                        reason_text=str(error),
                        attempts_used=attempts_used,
                        target_paths=target_paths,
                        allow_recreate=False,
                    )

                if current_requirements_mode != RequirementsMode.STANDARD.value:
                    hitl_loop_kwargs["grill_contract_intervention_handler"] = (
                        handle_grill_contract_intervention
                    )
                loop_result = run_hitl_agent_loop(
                    worker=worker,
                    stage_name=REQUIREMENTS_CLARIFICATION_STAGE_NAME,
                    output_path=requirements_clear_path,
                    question_path=ask_human_path,
                    record_path=hitl_record_path,
                    stage_status_path=stage_status_path,
                    turns_root=turns_root,
                    initial_prompt_builder=initial_prompt_builder,
                    hitl_prompt_builder=hitl_prompt_builder,
                    label_prefix="requirements_clarification",
                    turn_phase=REQUIREMENTS_CLARIFICATION_TURN_PHASE,
                    on_worker_starting=lambda live_worker: start_boot_progress(),
                    on_worker_started=handle_worker_started,
                    on_agent_turn_started=lambda context, live_worker: start_progress(),
                    on_agent_turn_finished=lambda context, live_worker: stop_progress(),
                    on_before_question_clear=audit_before_question_clear,
                    on_hitl_question=audit_hitl_question,
                    on_hitl_answer=audit_hitl_answer,
                    startup_intervention_handler=handle_startup_intervention,
                    timeout_sec=DEFAULT_COMMAND_TIMEOUT_SEC,
                    **hitl_loop_kwargs,
                )
                if str(loop_result.decision.payload.get("status", "")).strip() != REQUIREMENTS_STATUS_OK:
                    raise RuntimeError(loop_result.decision.summary or "需求澄清未完成闭环")
                if not requirements_clear_path.exists():
                    raise RuntimeError("需求澄清未生成需求澄清文档")
                if not requirements_clear_path.read_text(encoding="utf-8").strip():
                    raise RuntimeError("需求澄清文档为空")
                handoff = None
                cleanup_paths: tuple[str, ...] = (str(ask_human_path.resolve()),)
                if preserve_ba_worker and current_requirements_mode == RequirementsMode.STANDARD.value:
                    keep_worker_alive = True
                    handoff = RequirementsAnalystHandoff(
                        worker=worker,
                        vendor=worker.config.vendor.value,
                        model=worker.config.model,
                        reasoning_effort=worker.config.reasoning_effort,
                        proxy_url=worker.config.proxy_url,
                        ponytail_mode=worker.config.ponytail_mode,
                        graphify_mode=worker.config.graphify_mode,
                        graphify_config=dict(worker.config.graphify_config),
                    )
                else:
                    cleanup_paths = (
                        str(ask_human_path.resolve()),
                        *tuple(str(path) for path in sorted(owned_runtime_dirs)),
                    )
                append_stage_audit_record(
                    audit_context,
                    event_type="clarification_updated",
                    source_paths={"requirements_clear": requirements_clear_path},
                )
                stage_completed = True
                return RequirementsClarificationStageResult(
                    project_dir=str(project_root),
                    requirement_name=requirement_name,
                    requirements_clear_path=str(requirements_clear_path.resolve()),
                    cleanup_paths=cleanup_paths,
                    ba_handoff=handoff,
                    requirements_mode=current_requirements_mode,
                )
            except Exception as error:  # noqa: BLE001
                stop_progress()
                stop_boot_progress()
                auth_error = is_provider_auth_error(error) or worker_has_provider_auth_error(worker)
                ready_timeout_error = is_agent_ready_timeout_error(error)
                if auth_error:
                    if not keep_worker_alive:
                        try:
                            worker.request_kill()
                        except (TmuxControlUnavailable, TmuxMutationOutcomeUnknown):
                            raise
                    selection = prompt_recreate_requirements_clarification_agent(
                        reason_text=f"检测到需求分析师仍在 agent 界面，但模型认证已失效: {requirement_name}\n需要更换模型后继续当前阶段。",
                        requirement_name=requirement_name,
                        current_vendor=current_vendor,
                        current_model=current_model,
                        current_reasoning_effort=current_reasoning_effort,
                        current_proxy_url=current_proxy_url,
                        current_ponytail_mode=current_ponytail_mode,
                        current_requirements_mode=current_requirements_mode,
                        current_graphify_mode=current_graphify_mode,
                        current_graphify_config=current_graphify_config,
                        force_model_change=True,
                    )
                    if selection is not None:
                        current_vendor = selection.vendor
                        current_model = selection.model
                        current_reasoning_effort = selection.reasoning_effort
                        current_proxy_url = selection.proxy_url
                        current_ponytail_mode = selection.ponytail_mode
                        current_requirements_mode = selection.requirements_mode
                        current_graphify_mode = selection.graphify_mode
                        current_graphify_config = dict(selection.graphify_config)
                        current_resume_existing = current_resume_existing or bool(get_markdown_content(requirements_clear_path).strip())
                        keep_worker_alive = False
                        worker = None
                        continue
                    raise RuntimeError("需求分析师认证已失效，且用户未更换模型") from error
                if ready_timeout_error:
                    if not keep_worker_alive:
                        try:
                            worker.request_kill()
                        except (TmuxControlUnavailable, TmuxMutationOutcomeUnknown):
                            raise
                    selection = prompt_recreate_requirements_clarification_agent(
                        reason_text=f"需求分析师启动超时，未能进入可输入状态: {requirement_name}\n请重新选择模型后继续当前阶段。",
                        requirement_name=requirement_name,
                        current_vendor=current_vendor,
                        current_model=current_model,
                        current_reasoning_effort=current_reasoning_effort,
                        current_proxy_url=current_proxy_url,
                        current_ponytail_mode=current_ponytail_mode,
                        current_requirements_mode=current_requirements_mode,
                        current_graphify_mode=current_graphify_mode,
                        current_graphify_config=current_graphify_config,
                        force_model_change=True,
                    )
                    if selection is not None:
                        current_vendor = selection.vendor
                        current_model = selection.model
                        current_reasoning_effort = selection.reasoning_effort
                        current_proxy_url = selection.proxy_url
                        current_ponytail_mode = selection.ponytail_mode
                        current_requirements_mode = selection.requirements_mode
                        current_graphify_mode = selection.graphify_mode
                        current_graphify_config = dict(selection.graphify_config)
                        current_resume_existing = current_resume_existing or bool(get_markdown_content(requirements_clear_path).strip())
                        keep_worker_alive = False
                        worker = None
                        continue
                    raise RuntimeError("需求分析师启动超时，且用户未更换模型") from error
                if is_worker_death_error(error):
                    if not keep_worker_alive:
                        try:
                            worker.request_kill()
                        except (TmuxControlUnavailable, TmuxMutationOutcomeUnknown):
                            raise
                    selection = prompt_recreate_requirements_clarification_agent(
                        reason_text=f"检测到需求分析师已死亡: {requirement_name}\n需要更换模型后继续当前阶段。",
                        requirement_name=requirement_name,
                        current_vendor=current_vendor,
                        current_model=current_model,
                        current_reasoning_effort=current_reasoning_effort,
                        current_proxy_url=current_proxy_url,
                        current_ponytail_mode=current_ponytail_mode,
                        current_requirements_mode=current_requirements_mode,
                        current_graphify_mode=current_graphify_mode,
                        current_graphify_config=current_graphify_config,
                        force_model_change=True,
                    )
                    if selection is not None:
                        current_vendor = selection.vendor
                        current_model = selection.model
                        current_reasoning_effort = selection.reasoning_effort
                        current_proxy_url = selection.proxy_url
                        current_ponytail_mode = selection.ponytail_mode
                        current_requirements_mode = selection.requirements_mode
                        current_graphify_mode = selection.graphify_mode
                        current_graphify_config = dict(selection.graphify_config)
                        current_resume_existing = current_resume_existing or bool(get_markdown_content(requirements_clear_path).strip())
                        keep_worker_alive = False
                        worker = None
                        continue
                keep_worker_alive = preserve_worker_after_unhandled_stage_failure(worker, error)
                raise
    finally:
        stop_progress()
        stop_boot_progress()
        if worker is not None and not keep_worker_alive:
            try:
                worker.request_kill()
            except Exception:
                if stage_completed:
                    raise


def collect_request(args: argparse.Namespace) -> tuple[str, str]:
    project_dir = (
        str(resolve_existing_directory(args.project_dir))
        if args.project_dir
        else prompt_project_dir("")
    )
    if args.requirement_name:
        requirement_name = str(args.requirement_name).strip()
    else:
        requirement_name = prompt_requirement_name_selection(project_dir, "").requirement_name
    return project_dir, requirement_name


def run_requirements_clarification_stage(
        argv: Sequence[str] | None = None,
        *,
        preserve_ba_worker: bool = False,
) -> RequirementsClarificationStageResult:
    parser = build_parser()
    args = parser.parse_args(argv)
    project_dir, requirement_name = collect_request(args)
    _, grill_session_path, _ = build_requirements_grill_paths(project_dir, requirement_name)
    grill_session_header = read_grill_session_header(grill_session_path)
    active_grill_session = (
        grill_session_header
        if grill_session_header is not None
        and grill_session_header.state not in {"confirmed", "aborted"}
        else None
    )
    persisted_active_selection: RequirementsClarificationAgentSelection | None = None
    if active_grill_session is not None:
        if bool(getattr(args, "yes", False)) or not stdin_is_interactive():
            raise RuntimeError(
                "活跃 Grill 会话需要人类继续逐题确认；--yes 或非交互模式不能恢复该会话。"
            )
        explicit_mode = str(getattr(args, "requirements_mode", "") or "").strip()
        if (
            explicit_mode
            and normalize_requirements_mode(explicit_mode).value
            != active_grill_session.requirements_mode
        ):
            raise GrillSessionError(
                "活跃 Grill 会话禁止中途切换模式: "
                f"persisted={active_grill_session.requirements_mode}, requested={explicit_mode}"
            )
        requirements_mode = active_grill_session.requirements_mode
        args.requirements_mode = requirements_mode
        persisted_active_selection = load_persisted_requirements_grill_selection(
            project_dir=project_dir,
            runtime_root=Path(project_dir).expanduser().resolve() / REQUIREMENTS_RUNTIME_ROOT_NAME,
            state_path=active_grill_session.active_worker_state_path,
            requirements_mode=requirements_mode,
        )
        message(f"恢复现有 Grill 会话: {requirements_mode}")
    else:
        with _clarification_prompt_step(
            0,
            allow_back=bool(getattr(args, "allow_previous_stage_back", False)),
        ):
            requirements_mode = resolve_workflow_requirements_mode(
                args,
                stage_key="requirements_clarification",
            )
        # The downstream agent selection resolver sees an explicit value and
        # therefore cannot ask the same workflow-level question a second time.
        args.requirements_mode = requirements_mode
    human_input_provider = collect_auto_requirements_hitl_response if bool(args.yes) else None
    lock_context = requirement_concurrency_lock(
        project_dir,
        requirement_name,
        action="stage.a03.start",
    )
    lock_context.__enter__()
    audit_context: StageAuditRunContext | None = None
    try:
        audit_context = begin_stage_audit_run(
            project_dir,
            requirement_name,
            "A03",
            metadata={
                "trigger": "run_requirements_clarification_stage",
                "argv": list(argv or []),
                "args": vars(args),
            },
        )
        _, requirements_clear_path, ask_human_path, hitl_record_path = build_requirements_clarification_paths(
            project_dir,
            requirement_name,
        )
        record_before_cleanup(
            audit_context,
            {"ask_human": ask_human_path},
            metadata={"trigger": "clear_requirements_human_exchange_file"},
        )
        if requirements_mode == RequirementsMode.STANDARD.value or not grill_session_path.exists():
            clear_requirements_human_exchange_file(project_dir, requirement_name)
        if has_existing_requirements_clarification(project_dir, requirement_name):
            grill_reuse_confirmed = (
                requirements_mode == RequirementsMode.STANDARD.value
                or (
                    grill_session_header is not None
                    and grill_session_header.requirements_mode == requirements_mode
                    and grill_session_header.state == "confirmed"
                )
            )
            if grill_reuse_confirmed and should_reuse_existing_requirements_clarification(
                    project_dir,
                    requirement_name,
                    overwrite=bool(args.overwrite),
                    interactive=stdin_is_interactive(),
                    allow_back=bool(getattr(args, "allow_previous_stage_back", False)),
            ):
                # A confirmed Grill session is intentionally kept only when
                # this workflow is still using that same Grill policy.  A
                # Standard reuse must retire it, otherwise a later A04
                # ambiguity would incorrectly reopen an interview that the
                # human explicitly disabled for this run.
                if (
                    requirements_mode == RequirementsMode.STANDARD.value
                    and grill_session_header is not None
                    and grill_session_header.state in {"confirmed", "aborted"}
                ):
                    archived_path = archive_terminal_requirements_grill_session(
                        project_dir,
                        requirement_name,
                    )
                    grill_session_header = None
                    message(f"已归档上一轮 Grill 会话，按 Standard 复用需求澄清: {archived_path}")
                message("复用已有的需求澄清，直接进入需求评审阶段")
                result = reuse_existing_requirements_clarification(
                    project_dir,
                    requirement_name,
                    requirements_mode=requirements_mode,
                )
                append_stage_audit_record(
                    audit_context,
                    event_type="clarification_updated",
                    source_paths={"requirements_clear": requirements_clear_path},
                )
                mark_requirement_clarification_completed(project_dir, requirement_name)
                append_stage_audit_record(
                    audit_context,
                    event_type="stage_passed",
                    source_paths={
                        "requirements_clear": requirements_clear_path,
                        "hitl_record": hitl_record_path,
                    },
                )
                return result
            if (
                grill_session_header is not None
                and grill_session_header.state in {"confirmed", "aborted"}
            ):
                archived_path = archive_terminal_requirements_grill_session(
                    project_dir,
                    requirement_name,
                )
                grill_session_header = None
                message(f"已归档上一轮 Grill 会话，开始显式重跑: {archived_path}")
            elif not grill_reuse_confirmed:
                message("检测到尚未最终确认的 Grill 会话；忽略已有澄清候选并恢复逐问流程")
            message("不直接复用已有需求澄清，将启动需求分析师基于现有澄清继续核验")
            selection = (
                persisted_active_selection
                or collect_requirements_clarification_agent_selection(args)
            )
            message(render_requirements_clarification_stage_start(selection))
            result = run_requirements_clarification(
                project_dir,
                requirement_name,
                vendor=selection.vendor,
                model=selection.model,
                reasoning_effort=selection.reasoning_effort,
                proxy_url=selection.proxy_url,
                ponytail_mode=getattr(selection, "ponytail_mode", "off") or "off",
                requirements_mode=(
                    getattr(selection, "requirements_mode", requirements_mode)
                    or requirements_mode
                ),
                graphify_mode=getattr(selection, "graphify_mode", GraphifyMode.OFF.value) or GraphifyMode.OFF.value,
                graphify_config=getattr(selection, "graphify_config", {}) or {},
                resume_existing=True,
                preserve_ba_worker=preserve_ba_worker,
                human_input_provider=human_input_provider,
                audit_context=audit_context,
            )
        else:
            if (
                grill_session_header is not None
                and grill_session_header.state in {"confirmed", "aborted"}
            ):
                archived_path = archive_terminal_requirements_grill_session(
                    project_dir,
                    requirement_name,
                )
                grill_session_header = None
                message(f"已归档上一轮 Grill 会话，开始新的需求澄清: {archived_path}")
            message("执行摘要: 未检测到可复用的需求澄清，需要启动需求分析师智能体执行需求澄清；请为需求分析师选择厂商、模型、推理强度、代理端口。")
            selection = (
                persisted_active_selection
                or collect_requirements_clarification_agent_selection(args)
            )
            message(render_requirements_clarification_stage_start(selection))
            result = run_requirements_clarification(
                project_dir,
                requirement_name,
                vendor=selection.vendor,
                model=selection.model,
                reasoning_effort=selection.reasoning_effort,
                proxy_url=selection.proxy_url,
                ponytail_mode=getattr(selection, "ponytail_mode", "off") or "off",
                requirements_mode=(
                    getattr(selection, "requirements_mode", requirements_mode)
                    or requirements_mode
                ),
                graphify_mode=getattr(selection, "graphify_mode", GraphifyMode.OFF.value) or GraphifyMode.OFF.value,
                graphify_config=getattr(selection, "graphify_config", {}) or {},
                resume_existing=False,
                preserve_ba_worker=preserve_ba_worker,
                human_input_provider=human_input_provider,
                audit_context=audit_context,
            )
        mark_requirement_clarification_completed(project_dir, requirement_name)
        append_stage_audit_record(
            audit_context,
            event_type="stage_passed",
            source_paths={
                "requirements_clear": requirements_clear_path,
                "hitl_record": hitl_record_path,
            },
        )
        cleanup_paths = result.cleanup_paths
        if cleanup_paths:
            cleanup_runtime_paths(cleanup_paths)
            cleanup_runtime_root_if_empty(
                Path(project_dir).expanduser().resolve() / REQUIREMENTS_RUNTIME_ROOT_NAME
            )
            cleanup_paths = ()
        return RequirementsClarificationStageResult(
            project_dir=result.project_dir,
            requirement_name=result.requirement_name,
            requirements_clear_path=result.requirements_clear_path,
            cleanup_paths=cleanup_paths,
            ba_handoff=result.ba_handoff,
            requirements_mode=result.requirements_mode,
        )
    except Exception as error:  # noqa: BLE001
        append_stage_audit_record(
            audit_context,
            event_type="stage_failed",
            source_paths={
                "requirements_clear": requirements_clear_path if "requirements_clear_path" in locals() else "",
                "hitl_record": hitl_record_path if "hitl_record_path" in locals() else "",
            },
            metadata={"error": str(error)},
        )
        raise
    finally:
        lock_context.__exit__(None, None, None)


def main(argv: Sequence[str] | None = None) -> int:
    redirected, launch = maybe_launch_tui(argv, route="requirements", action="stage.a03.start")
    if redirected:
        return int(launch)
    try:
        result = run_requirements_clarification_stage(list(launch), preserve_ba_worker=False)
    except Exception as error:  # noqa: BLE001
        message(error)
        return 1

    message("需求澄清完成")
    message(result.requirements_clear_path)
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
