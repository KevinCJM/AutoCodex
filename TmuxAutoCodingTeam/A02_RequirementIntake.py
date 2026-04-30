# -*- encoding: utf-8 -*-
"""
@File: A02_RequirementIntake.py
@Modify Time: 2026/4/17
@Author: Kevin-Chen
@Descriptions: 需求录入阶段
"""

from __future__ import annotations

import argparse
import json
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence
from xml.etree import ElementTree

from tmux_core.runtime.vendor_catalog import get_default_model_for_vendor
from tmux_core.requirements_scope import CREATE_NEW_REQUIREMENT_SELECTION_VALUE
from Prompt_02_RequirementIntake import (
    NOTION_STATUS_ERROR,
    NOTION_STATUS_HITL,
    NOTION_STATUS_OK,
    NOTION_STATUS_SCHEMA_VERSION,
    get_notion_requirement,
)
from A01_Routing_LayerPlanning import DEFAULT_MODEL_BY_VENDOR, prompt_effort, prompt_model, prompt_vendor
from T01_tools import get_markdown_content
from T02_tmux_agents import (
    DEFAULT_COMMAND_TIMEOUT_SEC,
    AgentRunConfig,
    TmuxBatchWorker,
    cleanup_registered_tmux_workers,
)
from T05_hitl_runtime import HitlPromptContext, run_hitl_agent_loop, validate_hitl_status_file
from T08_pre_development import ensure_pre_development_task_record, mark_requirement_intake_completed
from T09_terminal_ops import (
    PROMPT_BACK_VALUE,
    PromptBackRequested,
    SingleLineSpinnerMonitor,
    TERMINAL_SPINNER_FRAMES,
    collect_multiline_input,
    maybe_launch_tui,
    message,
    prompt_metadata,
    prompt_select_option,
    prompt_yes_no as terminal_prompt_yes_no,
)
from T12_requirements_common import (
    RequirementNameSelection,
    build_output_path,
    clear_requirements_human_exchange_file,
    cleanup_runtime_paths,
    cleanup_runtime_root_if_empty,
    list_existing_requirements,
    prompt_project_dir,
    prompt_requirement_name,
    prompt_with_default,
    resolve_existing_directory,
    sanitize_requirement_name,
    stdin_is_interactive,
)


INPUT_TYPE_CHOICES = ("text", "file", "notion")
DEFAULT_NOTION_MODEL = "gpt-5.4-mini"
DEFAULT_NOTION_EFFORT = "high"
NOTION_TURN_PHASE = "requirements_intake_notion"
NOTION_RUNTIME_ROOT_NAME = ".requirements_intake_runtime"
NOTION_STAGE_NAME = "requirements_notion_intake"
PLACEHOLDER_NEXT_STEP = "下一步进入需求澄清阶段（待接入）"


@dataclass(frozen=True)
class RequirementIntakeRequest:
    project_dir: str
    requirement_name: str
    input_type: str
    input_value: str
    overwrite: bool
    auto_confirm: bool
    reuse_existing_original_requirement: bool = False


@dataclass(frozen=True)
class InputReadResult:
    content: str
    cleanup_paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class RequirementIntakeStageResult:
    project_dir: str
    requirement_name: str
    original_requirement_path: str
    cleanup_paths: tuple[str, ...] = ()


class NotionInputRetryRequired(RuntimeError):
    def __init__(self, message: str, *, cleanup_paths: Sequence[str | Path] = ()) -> None:
        super().__init__(message)
        self.cleanup_paths = tuple(str(Path(path).expanduser().resolve()) for path in cleanup_paths)


class RequirementInputRetryRequired(RuntimeError):
    def __init__(self, message: str, *, cleanup_paths: Sequence[str | Path] = ()) -> None:
        super().__init__(message)
        self.cleanup_paths = tuple(str(Path(path).expanduser().resolve()) for path in cleanup_paths)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="需求录入阶段")
    parser.add_argument("--project-dir", help="项目目录")
    parser.add_argument("--requirement-name", help="需求名称")
    parser.add_argument("--input-type", choices=INPUT_TYPE_CHOICES, help="输入方式: text|file|notion")
    parser.add_argument("--input-value", default="", help="输入值")
    parser.add_argument("--overwrite", action="store_true", help="允许覆盖已存在的原始需求文件")
    parser.add_argument("--reuse-existing-original-requirement", action="store_true", help="复用已存在的原始需求文件")
    parser.add_argument("--allow-previous-stage-back", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--yes", action="store_true", help="跳过非覆盖类确认")
    parser.add_argument("--no-tui", action="store_true", help="显式禁用 OpenTUI")
    parser.add_argument("--legacy-cli", action="store_true", help="使用旧版 Python CLI，不跳转 OpenTUI")
    return parser


def prompt_input_type(default: str = "text") -> str:
    candidate = prompt_select_option(
        title="选择输入方式",
        options=[(item, item) for item in INPUT_TYPE_CHOICES],
        default_value=normalize_input_type(default) or INPUT_TYPE_CHOICES[0],
        prompt_text="选择输入方式",
    )
    return normalize_input_type(candidate)


def prompt_yes_no(prompt_text: str, default: bool = False) -> bool:
    return terminal_prompt_yes_no(prompt_text, default)


def _requirement_prompt_step(step_index: int, *, allow_back: bool):
    return prompt_metadata(
        allow_back=allow_back,
        back_value=PROMPT_BACK_VALUE,
        stage_key="requirement_intake",
        stage_step_index=step_index,
    )


def normalize_input_type(value: str | None) -> str:
    text = str(value or "").strip().lower()
    if text in INPUT_TYPE_CHOICES:
        return text
    if text.isdigit():
        index = int(text)
        if 1 <= index <= len(INPUT_TYPE_CHOICES):
            return INPUT_TYPE_CHOICES[index - 1]
    return ""


def collect_text_input_interactive() -> str:
    return collect_multiline_input(
        title="请输入原始需求正文",
        empty_retry_message="原始需求内容不能为空，请重新输入。",
    )


def collect_text_input_noninteractive(stdin: Iterable[str]) -> str:
    return "".join(stdin).strip()


def resolve_input_file_path(project_dir: str | Path, input_value: str) -> Path:
    candidate = Path(str(input_value or "").strip()).expanduser()
    if not candidate.is_absolute():
        candidate = resolve_existing_directory(project_dir) / candidate
    if not candidate.exists():
        raise FileNotFoundError(f"输入文件不存在: {candidate}")
    if not candidate.is_file():
        raise IsADirectoryError(f"输入路径不是文件: {candidate}")
    return candidate.resolve()


def extract_text_from_markdown_or_text(file_path: str | Path) -> str:
    return Path(file_path).read_text(encoding="utf-8").strip()


def extract_text_from_pdf(file_path: str | Path) -> str:
    try:
        from pypdf import PdfReader
    except Exception as error:  # noqa: BLE001
        raise RuntimeError("当前环境缺少 pypdf，无法读取 PDF 文件") from error

    reader = PdfReader(str(file_path))
    parts: list[str] = []
    for page in reader.pages:
        text = (page.extract_text() or "").strip()
        if text:
            parts.append(text)
    return "\n\n".join(parts).strip()


def extract_text_from_docx(file_path: str | Path) -> str:
    path = Path(file_path)
    with zipfile.ZipFile(path, "r") as archive:
        try:
            xml_text = archive.read("word/document.xml")
        except KeyError as error:
            raise RuntimeError(f"DOCX 文件缺少 word/document.xml: {path}") from error
    root = ElementTree.fromstring(xml_text)
    namespaces = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    lines: list[str] = []
    for paragraph in root.findall(".//w:p", namespaces):
        chunks = []
        for node in paragraph.iter():
            tag = node.tag.rsplit("}", 1)[-1] if "}" in node.tag else node.tag
            if tag == "t":
                chunks.append(node.text or "")
            elif tag == "tab":
                chunks.append("\t")
        text = "".join(chunks).strip()
        if text:
            lines.append(text)
    return "\n".join(lines).strip()


def extract_text_from_local_file(project_dir: str | Path, input_value: str) -> str:
    file_path = resolve_input_file_path(project_dir, input_value)
    suffix = file_path.suffix.lower()
    if suffix in {".md", ".txt"}:
        return extract_text_from_markdown_or_text(file_path)
    if suffix == ".pdf":
        return extract_text_from_pdf(file_path)
    if suffix == ".docx":
        return extract_text_from_docx(file_path)
    raise ValueError(f"暂不支持的文件类型: {file_path.suffix or '(无扩展名)'}")


def build_notion_hitl_paths(project_dir: str | Path, requirement_name: str) -> tuple[Path, Path, Path]:
    project_root = resolve_existing_directory(project_dir)
    safe_name = sanitize_requirement_name(requirement_name)
    output_path = build_output_path(project_root, requirement_name)
    question_path = project_root / f"{safe_name}_需求录入_HITL问题.md"
    record_path = project_root / f"{safe_name}_需求录入_HITL记录.md"
    return output_path, question_path, record_path


def load_json_object(file_path: str | Path) -> dict[str, object]:
    payload = json.loads(Path(file_path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("JSON 文件必须是对象")
    return payload


def validate_notion_status(
    status_path: str | Path,
    *,
    turn_id: str,
    hitl_round: int,
    output_path: str | Path,
    question_path: str | Path,
    record_path: str | Path,
):
    return validate_hitl_status_file(
        status_path,
        expected_stage=NOTION_STAGE_NAME,
        expected_turn_id=turn_id,
        expected_hitl_round=hitl_round,
        expected_output_path=output_path,
        expected_question_path=question_path,
        expected_record_path=record_path,
    )


def format_notion_failure_message(status_payload: dict[str, object]) -> str:
    error = str(status_payload.get("error", "")).strip() or "未知错误"
    next_step = str(status_payload.get("next_step", "")).strip()
    verification = str(status_payload.get("verification_command", "")).strip()
    lines = [f"Notion 读取失败: {error}"]
    if next_step:
        lines.append(f"下一步: {next_step}")
    if verification:
        lines.append(f"验证命令: {verification}")
    return "\n".join(lines)


def build_notion_followup_prompt(
        human_msg: str,
        notion_url: str,
        *,
        original_requirement_md: str,
        ask_human_md: str,
        hitl_record_md: str,
) -> str:
    base_prompt = get_notion_requirement(
        notion_url,
        original_requirement_md=original_requirement_md,
        ask_human_md=ask_human_md,
    )
    return f"""## Follow-up Context
你正在继续执行同一 Notion 需求读取任务。

## Human Feedback
{human_msg}

## Record File
本轮还必须同步更新记录文件：《{hitl_record_md}》

## Follow-up Requirements
- 如果人类反馈要求补充读取子页面、关联页面、遗漏段落或特定补充范围，必须把这些补充读取要求纳入本轮处理。
- 如果本轮成功补充读取，除了完成基础读取任务外，还要把本轮已确认事实写入《{hitl_record_md}》。
- 如果本轮仍然失败或信息不足，除了写《{ask_human_md}》之外，还要把当前已确认事实、冲突点、待确认范围写入《{hitl_record_md}》。
- 禁止修改源代码，禁止修改除了《{original_requirement_md}》/《{ask_human_md}》/《{hitl_record_md}》之外的文档。

---

{base_prompt}"""

def cleanup_notion_runtime_paths(runtime_dir: str | Path, runtime_root: str | Path) -> tuple[str, ...]:
    removed = list(cleanup_runtime_paths([runtime_dir]))
    removed.extend(cleanup_runtime_root_if_empty(runtime_root))
    return tuple(removed)


def build_notion_retry_message(
        question_path: str | Path,
        *,
        output_path: str | Path | None = None,
) -> str:
    question_text = get_markdown_content(question_path).strip()
    output_text = get_markdown_content(output_path).strip() if output_path else ""
    if not question_text or output_text:
        return ""
    return "\n".join(
        [
            "Notion 需求录入失败，请先处理以下问题，然后重新选择需求录入方式：",
            "",
            question_text,
        ]
    ).strip()


def reprompt_request_for_input_source(request: RequirementIntakeRequest) -> RequirementIntakeRequest:
    message("请重新选择需求录入方式")
    input_type = prompt_input_type("text")
    input_value = ""
    if input_type == "file":
        default_value = request.input_value if request.input_type == "file" else ""
        input_value = prompt_with_default("输入本地文件路径", default_value, allow_empty=False)
    elif input_type == "notion":
        default_value = request.input_value if request.input_type == "notion" else ""
        input_value = prompt_with_default("输入 Notion 页面链接", default_value, allow_empty=False)
    return RequirementIntakeRequest(
        project_dir=request.project_dir,
        requirement_name=request.requirement_name,
        input_type=input_type,
        input_value=input_value,
        overwrite=request.overwrite,
        auto_confirm=request.auto_confirm,
    )


def render_notion_tmux_start_summary(worker: TmuxBatchWorker) -> str:
    return "\n".join(
        [
            "Notion 临时智能体已启动",
            f"runtime_dir: {worker.runtime_dir}",
            f"session_name: {worker.session_name}",
            "可使用以下命令进入会话:",
            f"  tmux attach -t {worker.session_name}",
        ]
    )


def render_notion_progress_line(*, worker: TmuxBatchWorker, requirement_name: str, tick: int) -> str:
    try:
        state = worker.read_state()
    except Exception:  # noqa: BLE001
        state = {}
    workflow_stage = str(
        state.get("workflow_stage")
        or state.get("current_turn_phase")
        or "starting"
    ).strip() or "starting"
    agent_state = str(state.get("agent_state", "")).strip().upper()
    if agent_state not in {"DEAD", "STARTING", "READY", "BUSY"}:
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
        f"{spinner} Notion需求录入中"
        f" | {requirement_name}:{status}/{agent_state}"
        f" | health={health_status}"
        f" | {note}"
    )


def render_agent_boot_progress_line(*, tick: int) -> str:
    spinner = TERMINAL_SPINNER_FRAMES[tick % len(TERMINAL_SPINNER_FRAMES)]
    return f"{spinner} 智能体启动中..."


def prompt_recreate_notion_reader_selection(
    *,
    current_vendor: str,
    current_model: str,
    current_reasoning_effort: str,
) -> tuple[str, str, str]:
    if not stdin_is_interactive():
        raise RuntimeError("Notion 需求录入智能体已死亡，且当前无法交互式重选模型。")
    while True:
        vendor = prompt_vendor(current_vendor, role_label="Notion 临时智能体")
        model = prompt_model(
            vendor,
            current_model if vendor == current_vendor else get_default_model_for_vendor(vendor),
            role_label="Notion 临时智能体",
        )
        reasoning_effort = prompt_effort(
            vendor,
            model,
            current_reasoning_effort,
            role_label="Notion 临时智能体",
        )
        if vendor != current_vendor or model != current_model:
            return vendor, model, reasoning_effort
        message("新的智能体必须切换 vendor 或 model。")


def run_notion_reader(project_dir: str | Path, notion_url: str, requirement_name: str) -> InputReadResult:
    project_root = resolve_existing_directory(project_dir)
    runtime_root = project_root / NOTION_RUNTIME_ROOT_NAME
    output_path, question_path, record_path = build_notion_hitl_paths(project_root, requirement_name)
    current_vendor = "codex"
    current_model = DEFAULT_NOTION_MODEL
    current_reasoning_effort = DEFAULT_NOTION_EFFORT

    def create_worker() -> TmuxBatchWorker:
        return TmuxBatchWorker(
            worker_id="requirements-notion-reader",
            work_dir=project_root,
            config=AgentRunConfig(
                vendor=current_vendor,
                model=current_model,
                reasoning_effort=current_reasoning_effort,
            ),
            runtime_root=runtime_root,
        )

    worker = create_worker()
    runtime_dir = worker.runtime_dir
    stage_status_path = runtime_dir / "notion_status.json"
    turns_root = runtime_dir / "turns"
    progress_monitor = SingleLineSpinnerMonitor(
        frame_builder=lambda tick: render_notion_progress_line(
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
        boot_progress_monitor.stop()
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
        progress_monitor.stop()
        progress_active = False

    def handle_worker_started(live_worker: TmuxBatchWorker) -> None:
        stop_boot_progress()
        message(render_notion_tmux_start_summary(live_worker))

    def replace_dead_worker(current_worker: TmuxBatchWorker, error: BaseException) -> TmuxBatchWorker:
        nonlocal worker, current_vendor, current_model, current_reasoning_effort
        try:
            current_worker.request_kill()
        except Exception:
            pass
        current_vendor, current_model, current_reasoning_effort = prompt_recreate_notion_reader_selection(
            current_vendor=current_vendor,
            current_model=current_model,
            current_reasoning_effort=current_reasoning_effort,
        )
        message(f"Notion 临时智能体已死亡，准备重建: {error}")
        worker = create_worker()
        return worker

    def initial_prompt_builder(context: HitlPromptContext) -> str:
        return get_notion_requirement(
            notion_url,
            original_requirement_md=str(Path(context.output_path).resolve()),
            ask_human_md=str(Path(context.question_path).resolve()),
        )

    def hitl_prompt_builder(human_msg: str, context: HitlPromptContext) -> str:
        return build_notion_followup_prompt(
            human_msg,
            notion_url,
            original_requirement_md=str(Path(context.output_path).resolve()),
            ask_human_md=str(Path(context.question_path).resolve()),
            hitl_record_md=str(Path(context.record_path).resolve()),
        )

    try:
        try:
            loop_result = run_hitl_agent_loop(
                worker=worker,
                stage_name=NOTION_STAGE_NAME,
                output_path=output_path,
                question_path=question_path,
                record_path=record_path,
                stage_status_path=stage_status_path,
                turns_root=turns_root,
                initial_prompt_builder=initial_prompt_builder,
                hitl_prompt_builder=hitl_prompt_builder,
                label_prefix="read_notion_requirement",
                turn_phase=NOTION_TURN_PHASE,
                on_worker_starting=lambda live_worker: start_boot_progress(),
                on_worker_started=handle_worker_started,
                on_agent_turn_started=lambda context, live_worker: start_progress(),
                on_agent_turn_finished=lambda context, live_worker: stop_progress(),
                replace_dead_worker=replace_dead_worker,
                timeout_sec=DEFAULT_COMMAND_TIMEOUT_SEC,
            )
        except RuntimeError as error:
            if stage_status_path.exists():
                try:
                    payload = load_json_object(stage_status_path)
                except Exception:  # noqa: BLE001
                    pass
                else:
                    if (
                        str(payload.get("schema_version", "")).strip() == NOTION_STATUS_SCHEMA_VERSION
                        and str(payload.get("stage", "")).strip() == NOTION_STAGE_NAME
                        and str(payload.get("status", "")).strip() == NOTION_STATUS_ERROR
                    ):
                        fallback_message = format_notion_failure_message(payload)
                        retry_message = build_notion_retry_message(question_path, output_path=output_path)
                        if retry_message:
                            raise NotionInputRetryRequired(
                                retry_message,
                                cleanup_paths=(record_path, runtime_dir, runtime_root),
                            ) from error
                        raise RuntimeError(fallback_message) from error
            raise
        if not output_path.exists():
            raise RuntimeError("Notion 读取未生成正文文件")
        content = output_path.read_text(encoding="utf-8").strip()
        if not content:
            raise RuntimeError("Notion 页面正文为空")
        stage_payload = loop_result.decision.payload
        if str(stage_payload.get("status", "")).strip() != NOTION_STATUS_OK:
            fallback_message = format_notion_failure_message(stage_payload)
            retry_message = build_notion_retry_message(question_path, output_path=output_path)
            if retry_message:
                raise NotionInputRetryRequired(
                    retry_message,
                    cleanup_paths=(record_path, runtime_dir, runtime_root),
                )
            raise RuntimeError(fallback_message)
        return InputReadResult(
            content=content,
            cleanup_paths=(
                str(question_path.resolve()),
                str(record_path.resolve()),
                str(Path(runtime_dir).expanduser().resolve()),
                str(Path(runtime_root).expanduser().resolve()),
            ),
        )
    finally:
        stop_progress()
        stop_boot_progress()
        try:
            worker.request_kill()
        except Exception:
            pass


def ensure_non_empty_content(content: str) -> str:
    text = str(content or "").strip()
    if not text:
        raise RuntimeError("原始需求内容为空，未生成输出文件")
    return text


def read_input_content(request: RequirementIntakeRequest) -> InputReadResult:
    if request.input_type == "text":
        try:
            if request.input_value:
                return InputReadResult(content=ensure_non_empty_content(request.input_value))
            if not stdin_is_interactive():
                return InputReadResult(content=ensure_non_empty_content(collect_text_input_noninteractive(sys.stdin)))
            return InputReadResult(content=ensure_non_empty_content(collect_text_input_interactive()))
        except Exception as error:  # noqa: BLE001
            if stdin_is_interactive():
                raise RequirementInputRetryRequired(str(error)) from error
            raise
    if request.input_type == "file":
        try:
            return InputReadResult(
                content=ensure_non_empty_content(extract_text_from_local_file(request.project_dir, request.input_value))
            )
        except Exception as error:  # noqa: BLE001
            if stdin_is_interactive():
                raise RequirementInputRetryRequired(str(error)) from error
            raise
    if request.input_type == "notion":
        result = run_notion_reader(request.project_dir, request.input_value, request.requirement_name)
        return InputReadResult(content=ensure_non_empty_content(result.content), cleanup_paths=result.cleanup_paths)
    raise ValueError(f"不支持的输入方式: {request.input_type}")


def write_requirement_file(output_path: str | Path, content: str, *, overwrite: bool) -> Path:
    path = Path(output_path)
    if path.exists() and not overwrite:
        raise FileExistsError(f"文件已存在: {path}")
    path.write_text(str(content).strip() + "\n", encoding="utf-8")
    return path


def _prompt_requirement_selection(project_dir: str | Path, default: str = "") -> RequirementNameSelection:
    existing = list_existing_requirements(project_dir)
    if not existing:
        return RequirementNameSelection(
            requirement_name=prompt_requirement_name(default),
            reuse_existing_original_requirement=False,
        )
    selected = prompt_select_option(
        title="\n".join(
            [
                f"检测项目内已有需求: {', '.join(existing)}",
                "可选需求:",
            ]
        ),
        options=[*[(item, item) for item in existing], (CREATE_NEW_REQUIREMENT_SELECTION_VALUE, "创建新需求")],
        default_value=CREATE_NEW_REQUIREMENT_SELECTION_VALUE,
        prompt_text="选择已有需求或创建新需求",
    )
    if selected == CREATE_NEW_REQUIREMENT_SELECTION_VALUE:
        return RequirementNameSelection(
            requirement_name="",
            reuse_existing_original_requirement=False,
        )
    return RequirementNameSelection(
        requirement_name=selected,
        reuse_existing_original_requirement=True,
    )


def collect_request(args: argparse.Namespace) -> RequirementIntakeRequest:
    project_dir = str(args.project_dir or "").strip()
    requirement_name = str(args.requirement_name or "").strip()
    reuse_existing_original_requirement = bool(args.reuse_existing_original_requirement and requirement_name)
    input_type = normalize_input_type(args.input_type) if args.input_type else ""
    input_value = str(args.input_value or "").strip()
    requirement_name_step = 1
    require_new_requirement_name = False
    allow_previous_stage_back = bool(getattr(args, "allow_previous_stage_back", False))

    first_prompt_step = 0 if not project_dir else (1 if not requirement_name else 3)
    step = first_prompt_step
    while step < 5:
        try:
            if step == 0:
                with _requirement_prompt_step(0, allow_back=False):
                    project_dir = prompt_project_dir(project_dir)
                step = 1
                continue
            if step == 1:
                project_dir = str(resolve_existing_directory(project_dir))
                with _requirement_prompt_step(1, allow_back=(step > first_prompt_step) or (step == first_prompt_step and allow_previous_stage_back)):
                    selection = _prompt_requirement_selection(project_dir, requirement_name)
                reuse_existing_original_requirement = selection.reuse_existing_original_requirement
                if selection.requirement_name:
                    requirement_name = selection.requirement_name
                    require_new_requirement_name = False
                    requirement_name_step = 1
                    step = 5 if reuse_existing_original_requirement else 3
                    continue
                require_new_requirement_name = True
                requirement_name_step = 2
                step = 2
                continue
            if step == 2:
                with _requirement_prompt_step(2, allow_back=step > first_prompt_step):
                    requirement_name = prompt_requirement_name(requirement_name)
                reuse_existing_original_requirement = False
                step = 3
                continue
            if step == 3:
                if reuse_existing_original_requirement:
                    step = 5
                    continue
                if not input_type:
                    with _requirement_prompt_step(3, allow_back=step > first_prompt_step):
                        input_type = prompt_input_type("text")
                else:
                    input_type = normalize_input_type(input_type)
                step = 4
                continue
            if step == 4:
                if input_type == "file" and not input_value:
                    with _requirement_prompt_step(4, allow_back=step > first_prompt_step):
                        input_value = prompt_with_default("输入本地文件路径", "", allow_empty=False)
                elif input_type == "notion" and not input_value:
                    with _requirement_prompt_step(4, allow_back=step > first_prompt_step):
                        input_value = prompt_with_default("输入 Notion 页面链接", "", allow_empty=False)
                elif input_type == "text" and input_value:
                    input_value = str(args.input_value)
                step = 5
                continue
        except PromptBackRequested:
            if step == first_prompt_step:
                if allow_previous_stage_back:
                    raise
                continue
            if step == 3:
                step = requirement_name_step if require_new_requirement_name else 1
            else:
                step = max(first_prompt_step, step - 1)

    project_dir = str(resolve_existing_directory(project_dir))
    clear_requirements_human_exchange_file(project_dir, requirement_name)
    return RequirementIntakeRequest(
        project_dir=project_dir,
        requirement_name=requirement_name,
        input_type=input_type,
        input_value=input_value,
        overwrite=bool(args.overwrite),
        auto_confirm=bool(args.yes),
        reuse_existing_original_requirement=reuse_existing_original_requirement,
    )


def maybe_confirm_overwrite(path: str | Path, *, overwrite: bool) -> bool:
    target = Path(path)
    if not target.exists():
        return overwrite
    if overwrite:
        return True
    return prompt_yes_no(f"文件已存在，是否覆盖 {target.name}", False)


def _reprompt_args_after_overwrite_declined(
    args: argparse.Namespace,
    *,
    project_dir: str | Path,
) -> argparse.Namespace:
    next_args = argparse.Namespace(**vars(args))
    next_args.project_dir = str(project_dir)
    next_args.requirement_name = ""
    next_args.reuse_existing_original_requirement = False
    next_args.input_type = ""
    next_args.input_value = ""
    next_args.overwrite = False
    next_args.yes = False
    return next_args


def run_requirement_intake_stage(argv: Sequence[str] | None = None) -> RequirementIntakeStageResult:
    parser = build_parser()
    args = parser.parse_args(argv)
    while True:
        request = collect_request(args)
        output_path = build_output_path(request.project_dir, request.requirement_name)

        if not request.reuse_existing_original_requirement and output_path.exists() and not request.overwrite:
            if request.auto_confirm:
                raise RuntimeError(f"目标文件已存在，未指定 --overwrite: {output_path}")
            try:
                with _requirement_prompt_step(5, allow_back=True):
                    should_overwrite = maybe_confirm_overwrite(output_path, overwrite=False)
            except PromptBackRequested:
                args = _reprompt_args_after_overwrite_declined(args, project_dir=request.project_dir)
                continue
            if not should_overwrite:
                message("已取消覆盖，请重新选择需求名称或复用已有需求。")
                args = _reprompt_args_after_overwrite_declined(args, project_dir=request.project_dir)
                continue
        break
    ensure_pre_development_task_record(request.project_dir, request.requirement_name)

    if request.reuse_existing_original_requirement:
        if not get_markdown_content(output_path).strip():
            raise RuntimeError(f"已选择复用已有原始需求，但文件不存在或为空: {output_path}")
        mark_requirement_intake_completed(request.project_dir, request.requirement_name)
        message("复用已有原始需求")
        message(output_path)
    else:
        while True:
            try:
                input_result = read_input_content(request)
                break
            except (NotionInputRetryRequired, RequirementInputRetryRequired) as retry:
                message(str(retry))
                if retry.cleanup_paths:
                    cleanup_runtime_paths(retry.cleanup_paths)
                if not stdin_is_interactive():
                    raise
                request = reprompt_request_for_input_source(request)
        write_requirement_file(output_path, input_result.content, overwrite=True)
        mark_requirement_intake_completed(request.project_dir, request.requirement_name)
        if input_result.cleanup_paths:
            cleanup_runtime_paths(input_result.cleanup_paths)
        message("需求录入完成")
        message(output_path)

    return RequirementIntakeStageResult(
        project_dir=str(resolve_existing_directory(request.project_dir)),
        requirement_name=request.requirement_name,
        original_requirement_path=str(output_path.resolve()),
    )


def main(argv: Sequence[str] | None = None) -> int:
    redirected, launch = maybe_launch_tui(argv, route="requirements", action="stage.a02.start")
    if redirected:
        return int(launch)
    try:
        result = run_requirement_intake_stage(list(launch))
    except Exception as error:  # noqa: BLE001
        message(error)
        return 1

    message(result.original_requirement_path)
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
