# -*- encoding: utf-8 -*-
"""
@File: A02_RequirementsAnalysis.py
@Modify Time: 2026/4/13
@Author: Kevin-Chen
@Descriptions: 需求获取与需求澄清阶段
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence
from xml.etree import ElementTree

from A01_Routing_LayerPlanning import (
    DEFAULT_MODEL_BY_VENDOR,
    normalize_effort_choice,
    normalize_model_choice,
    normalize_vendor_choice,
    prompt_effort,
    prompt_model,
    prompt_vendor,
)
from Prompt_02_RequirementsAnalysis import (
    NOTION_STATUS_ERROR,
    NOTION_STATUS_HITL,
    NOTION_STATUS_OK,
    NOTION_STATUS_SCHEMA_VERSION,
    REQUIREMENTS_STATUS_OK,
    REQUIREMENTS_STATUS_SCHEMA_VERSION,
    fintech_ba,
    get_notion_requirement,
    hitl_bck,
    requirements_understand,
    resume_requirements_understand,
)
from T02_tmux_agents import (
    DEFAULT_COMMAND_TIMEOUT_SEC,
    AgentRunConfig,
    TmuxBatchWorker,
    cleanup_registered_tmux_workers,
    is_agent_ready_timeout_error,
    is_provider_auth_error,
    is_worker_death_error,
    try_resume_worker,
)
from T01_tools import get_markdown_content
from T03_agent_init_workflow import resolve_existing_directory
from T05_hitl_runtime import HitlPromptContext, build_prefixed_sha256, run_hitl_agent_loop, validate_hitl_status_file
from T09_terminal_ops import (
    SingleLineSpinnerMonitor,
    TERMINAL_SPINNER_FRAMES,
    collect_multiline_input,
    message,
    maybe_launch_tui,
    prompt_select_option,
    terminal_ui_is_interactive,
    prompt_with_default as terminal_prompt_with_default,
    prompt_yes_no as terminal_prompt_yes_no,
)
from T08_pre_development import (
    build_pre_development_task_record_path as shared_build_pre_development_task_record_path,
    build_pre_development_task_record_payload as shared_build_pre_development_task_record_payload,
    ensure_pre_development_task_record as shared_ensure_pre_development_task_record,
    mark_requirement_clarification_completed as shared_mark_requirement_clarification_completed,
    mark_requirement_intake_completed as shared_mark_requirement_intake_completed,
)


INPUT_TYPE_CHOICES = ("text", "file", "notion")
DEFAULT_REQUIREMENTS_ANALYSIS_VENDOR = "codex"
DEFAULT_NOTION_MODEL = "gpt-5.4-mini"
DEFAULT_NOTION_EFFORT = "high"
DEFAULT_REQUIREMENTS_ANALYSIS_MODEL = "gpt-5.4"
DEFAULT_REQUIREMENTS_ANALYSIS_EFFORT = "high"
PLACEHOLDER_NEXT_STEP = "下一步进入需求评审阶段（待接入）"
NOTION_TURN_PHASE = "requirements_notion_read"
NOTION_RUNTIME_ROOT_NAME = ".requirements_analysis_runtime"
NOTION_STAGE_NAME = "requirements_notion_intake"
REQUIREMENTS_ANALYSIS_TURN_PHASE = "requirements_analysis"
REQUIREMENTS_ANALYSIS_STAGE_NAME = "requirements_analysis"


def build_pre_development_task_record_payload() -> dict[str, dict[str, bool]]:
    return shared_build_pre_development_task_record_payload()


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
class RequirementNameSelection:
    requirement_name: str
    reuse_existing_original_requirement: bool = False


@dataclass(frozen=True)
class InputReadResult:
    content: str
    cleanup_paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class RequirementsAnalystHandoff:
    worker: TmuxBatchWorker
    vendor: str
    model: str
    reasoning_effort: str
    proxy_url: str


@dataclass(frozen=True)
class RequirementsStageResult:
    project_dir: str
    requirement_name: str
    requirements_clear_path: str
    cleanup_paths: tuple[str, ...] = ()
    ba_handoff: RequirementsAnalystHandoff | None = None


RequirementsAnalysisResult = RequirementsStageResult


@dataclass(frozen=True)
class RequirementsAnalysisAgentSelection:
    vendor: str
    model: str
    reasoning_effort: str
    proxy_url: str


class NotionInputRetryRequired(RuntimeError):
    def __init__(self, message: str, *, cleanup_paths: Sequence[str | Path] = ()) -> None:
        super().__init__(message)
        self.cleanup_paths = tuple(str(Path(path).expanduser().resolve()) for path in cleanup_paths)


class RequirementInputRetryRequired(RuntimeError):
    def __init__(self, message: str, *, cleanup_paths: Sequence[str | Path] = ()) -> None:
        super().__init__(message)
        self.cleanup_paths = tuple(str(Path(path).expanduser().resolve()) for path in cleanup_paths)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="需求获取与需求澄清阶段")
    parser.add_argument("--project-dir", help="项目目录")
    parser.add_argument("--requirement-name", help="需求名称")
    parser.add_argument("--input-type", choices=INPUT_TYPE_CHOICES, help="输入方式: text|file|notion")
    parser.add_argument("--input-value", default="", help="输入值")
    parser.add_argument("--vendor", help="需求澄清阶段厂商: codex|claude|gemini|qwen|kimi")
    parser.add_argument("--model", help="需求澄清阶段模型名称")
    parser.add_argument("--effort", help="需求澄清阶段推理强度")
    parser.add_argument("--proxy-url", default="", help="需求澄清阶段代理端口或完整代理 URL")
    parser.add_argument("--overwrite", action="store_true", help="允许覆盖已存在的原始需求文件")
    parser.add_argument("--yes", action="store_true", help="跳过非覆盖类确认")
    parser.add_argument("--no-tui", action="store_true", help="显式禁用 OpenTUI")
    parser.add_argument("--legacy-cli", action="store_true", help="使用旧版 Python CLI，不跳转 OpenTUI")
    return parser


def prompt_with_default(prompt_text: str, default: str = "", allow_empty: bool = False) -> str:
    return terminal_prompt_with_default(prompt_text, default, allow_empty)


def prompt_proxy_url(default: str = "") -> str:
    return prompt_with_default("代理端口或完整代理 URL，可留空", default, allow_empty=True)


def prompt_project_dir(default: str = "") -> str:
    while True:
        candidate = prompt_with_default("项目工作目录", default)
        try:
            return str(resolve_existing_directory(candidate))
        except Exception as error:  # noqa: BLE001
            message(f"目录无效: {error}")


def prompt_requirement_name(default: str = "") -> str:
    while True:
        name = prompt_with_default("需求名称", default)
        if sanitize_requirement_name(name):
            return name
        message("需求名称不能全部由非法字符组成，请重试。")


def list_existing_requirements(project_dir: str | Path) -> tuple[str, ...]:
    project_root = resolve_existing_directory(project_dir)
    existing: list[str] = []
    for file_path in sorted(project_root.glob("*_原始需求.md")):
        if not file_path.is_file():
            continue
        if not file_path.read_text(encoding="utf-8").strip():
            continue
        requirement_name = file_path.name.removesuffix("_原始需求.md").strip()
        if requirement_name:
            existing.append(requirement_name)
    return tuple(existing)


def prompt_requirement_name_selection(project_dir: str | Path, default: str = "") -> RequirementNameSelection:
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
        options=[*[(item, item) for item in existing], ("__create_new__", "创建新需求")],
        default_value="__create_new__",
        prompt_text="选择已有需求或创建新需求",
    )
    if selected == "__create_new__":
        return RequirementNameSelection(
            requirement_name=prompt_requirement_name(default),
            reuse_existing_original_requirement=False,
        )
    return RequirementNameSelection(
        requirement_name=selected,
        reuse_existing_original_requirement=True,
    )


def prompt_requirement_name_with_existing(project_dir: str | Path, default: str = "") -> str:
    return prompt_requirement_name_selection(project_dir, default).requirement_name


def prompt_input_type(default: str = "text") -> str:
    candidate = prompt_select_option(
        title="可选输入方式:",
        options=[(item, item) for item in INPUT_TYPE_CHOICES],
        default_value=normalize_input_type(default) or INPUT_TYPE_CHOICES[0],
        prompt_text="选择输入方式",
    )
    return normalize_input_type(candidate)


def prompt_yes_no(prompt_text: str, default: bool = False) -> bool:
    return terminal_prompt_yes_no(prompt_text, default)


def stdin_is_interactive() -> bool:
    return terminal_ui_is_interactive()


def normalize_input_type(value: str | None) -> str:
    text = str(value or "").strip().lower()
    if text in INPUT_TYPE_CHOICES:
        return text
    if text.isdigit():
        index = int(text)
        if 1 <= index <= len(INPUT_TYPE_CHOICES):
            return INPUT_TYPE_CHOICES[index - 1]
    return ""


def sanitize_requirement_name(name: str) -> str:
    text = str(name or "").strip()
    text = re.sub(r"\s+", "_", text)
    text = re.sub(r'[\/\\:\*\?"<>\|]+', "_", text)
    text = re.sub(r"[^0-9A-Za-z_\-\u4e00-\u9fff]+", "_", text)
    text = re.sub(r"_+", "_", text).strip("._")
    return text or "需求"


def build_output_path(project_dir: str | Path, requirement_name: str) -> Path:
    root = resolve_existing_directory(project_dir)
    safe_name = sanitize_requirement_name(requirement_name)
    return root / f"{safe_name}_原始需求.md"


def build_pre_development_task_record_path(project_dir: str | Path, requirement_name: str) -> Path:
    return shared_build_pre_development_task_record_path(project_dir, requirement_name)


def ensure_pre_development_task_record(project_dir: str | Path, requirement_name: str) -> Path:
    return shared_ensure_pre_development_task_record(project_dir, requirement_name)


def mark_requirement_intake_completed(project_dir: str | Path, requirement_name: str) -> Path:
    return shared_mark_requirement_intake_completed(project_dir, requirement_name)


def mark_requirement_clarification_completed(project_dir: str | Path, requirement_name: str) -> Path:
    return shared_mark_requirement_clarification_completed(project_dir, requirement_name)


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


def cleanup_runtime_paths(paths: Sequence[str | Path]) -> tuple[str, ...]:
    removed: list[str] = []
    unique_paths: list[Path] = []
    for item in paths:
        path = Path(item).expanduser().resolve()
        if path not in unique_paths:
            unique_paths.append(path)
    for path in unique_paths:
        if not path.exists():
            continue
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
        removed.append(str(path))
    return tuple(removed)


def cleanup_notion_runtime_paths(runtime_dir: str | Path, runtime_root: str | Path) -> tuple[str, ...]:
    removed = list(cleanup_runtime_paths([runtime_dir]))
    root = Path(runtime_root).expanduser().resolve()
    if root.exists() and root.is_dir() and not any(root.iterdir()):
        root.rmdir()
        removed.append(str(root))
    return tuple(removed)


def cleanup_stage_runtime_paths(runtime_dir: str | Path, runtime_root: str | Path) -> tuple[str, ...]:
    return cleanup_notion_runtime_paths(runtime_dir, runtime_root)


def build_notion_hitl_paths(project_dir: str | Path, requirement_name: str) -> tuple[Path, Path, Path]:
    project_root = resolve_existing_directory(project_dir)
    safe_name = sanitize_requirement_name(requirement_name)
    question_path = project_root / f"{safe_name}_需求录入_HITL问题.md"
    record_path = project_root / f"{safe_name}_需求录入_HITL记录.md"
    output_path = build_output_path(project_root, requirement_name)
    return output_path, question_path, record_path


def build_requirements_analysis_paths(project_dir: str | Path, requirement_name: str) -> tuple[Path, Path, Path, Path]:
    project_root = resolve_existing_directory(project_dir)
    safe_name = sanitize_requirement_name(requirement_name)
    original_requirement_path = build_output_path(project_root, requirement_name)
    requirements_clear_path = project_root / f"{safe_name}_需求澄清.md"
    ask_human_path = project_root / f"{safe_name}_与人类交流.md"
    hitl_record_path = project_root / f"{safe_name}_人机交互澄清记录.md"
    return original_requirement_path, requirements_clear_path, ask_human_path, hitl_record_path


def build_legacy_requirements_hitl_record_path(project_dir: str | Path, requirement_name: str) -> Path:
    project_root = resolve_existing_directory(project_dir)
    safe_name = sanitize_requirement_name(requirement_name)
    return project_root / f"{safe_name}人机交互澄清记录.md"


def ensure_requirements_hitl_record_file(project_dir: str | Path, requirement_name: str) -> Path:
    _, _, _, hitl_record_path = build_requirements_analysis_paths(project_dir, requirement_name)
    legacy_path = build_legacy_requirements_hitl_record_path(project_dir, requirement_name)
    hitl_record_path.parent.mkdir(parents=True, exist_ok=True)
    if hitl_record_path.exists():
        return hitl_record_path.resolve()
    if legacy_path.exists():
        legacy_path.replace(hitl_record_path)
        return hitl_record_path.resolve()
    hitl_record_path.write_text("", encoding="utf-8")
    return hitl_record_path.resolve()


def has_existing_requirements_clarification(project_dir: str | Path, requirement_name: str) -> bool:
    _, requirements_clear_path, _, _ = build_requirements_analysis_paths(project_dir, requirement_name)
    return bool(get_markdown_content(requirements_clear_path).strip())


def should_reuse_existing_requirements_clarification(
        project_dir: str | Path,
        requirement_name: str,
        *,
        overwrite: bool,
        interactive: bool,
) -> bool:
    _, requirements_clear_path, _, _ = build_requirements_analysis_paths(project_dir, requirement_name)
    if not get_markdown_content(requirements_clear_path).strip():
        return False
    if not interactive:
        return not overwrite
    message(f"检测项目内已有需求澄清: {requirements_clear_path.name}")
    return prompt_yes_no("是否直接复用已有的需求澄清并跳入下一阶段", True)


def reuse_existing_requirements_clarification(
        project_dir: str | Path,
        requirement_name: str,
) -> RequirementsStageResult:
    _, requirements_clear_path, _, _ = build_requirements_analysis_paths(project_dir, requirement_name)
    if not get_markdown_content(requirements_clear_path).strip():
        raise RuntimeError(f"缺少可复用的需求澄清文档: {requirements_clear_path}")
    ensure_requirements_hitl_record_file(project_dir, requirement_name)
    return RequirementsStageResult(
        project_dir=str(resolve_existing_directory(project_dir)),
        requirement_name=requirement_name,
        requirements_clear_path=str(requirements_clear_path.resolve()),
    )


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
            "Notion 需求获取失败，请先处理以下问题，然后重新选择需求录入方式：",
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
        input_value = prompt_with_default("本地文件路径", default_value, allow_empty=False)
    elif input_type == "notion":
        default_value = request.input_value if request.input_type == "notion" else ""
        input_value = prompt_with_default("Notion 页面链接", default_value, allow_empty=False)
    return RequirementIntakeRequest(
        project_dir=request.project_dir,
        requirement_name=request.requirement_name,
        input_type=input_type,
        input_value=input_value,
        overwrite=request.overwrite,
        auto_confirm=request.auto_confirm,
    )


def reprompt_request_after_notion_failure(request: RequirementIntakeRequest) -> RequirementIntakeRequest:
    return reprompt_request_for_input_source(request)


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
    provider_phase = str(state.get("provider_phase", "unknown")).strip() or "unknown"
    health_status = str(state.get("health_status", "unknown")).strip() or "unknown"
    note = str(state.get("note", "")).strip() or workflow_stage
    status = str(state.get("status", "running")).strip() or "running"
    spinner = TERMINAL_SPINNER_FRAMES[tick % len(TERMINAL_SPINNER_FRAMES)]
    return (
        f"{spinner} Notion需求提取中"
        f" | {requirement_name}:{status}/{provider_phase}"
        f" | health={health_status}"
        f" | {note}"
    )


def render_agent_boot_progress_line(*, tick: int) -> str:
    spinner = TERMINAL_SPINNER_FRAMES[tick % len(TERMINAL_SPINNER_FRAMES)]
    return f"{spinner} 智能体启动中..."


def render_requirements_analysis_tmux_start_summary(worker: TmuxBatchWorker) -> str:
    return "\n".join(
        [
            "需求澄清智能体已启动",
            f"runtime_dir: {worker.runtime_dir}",
            f"session_name: {worker.session_name}",
            "可使用以下命令进入会话:",
            f"  tmux attach -t {worker.session_name}",
        ]
    )


def render_requirements_analysis_progress_line(
        *,
        worker: TmuxBatchWorker,
        requirement_name: str,
        tick: int,
) -> str:
    try:
        state = worker.read_state()
    except Exception:  # noqa: BLE001
        state = {}
    workflow_stage = str(
        state.get("workflow_stage")
        or state.get("current_turn_phase")
        or "starting"
    ).strip() or "starting"
    provider_phase = str(state.get("provider_phase", "unknown")).strip() or "unknown"
    health_status = str(state.get("health_status", "unknown")).strip() or "unknown"
    note = str(state.get("note", "")).strip() or workflow_stage
    status = str(state.get("status", "running")).strip() or "running"
    spinner = TERMINAL_SPINNER_FRAMES[tick % len(TERMINAL_SPINNER_FRAMES)]
    return (
        f"{spinner} 需求澄清中"
        f" | {requirement_name}:{status}/{provider_phase}"
        f" | health={health_status}"
        f" | {note}"
    )


def collect_requirements_analysis_agent_selection(args: argparse.Namespace) -> RequirementsAnalysisAgentSelection:
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

    return RequirementsAnalysisAgentSelection(
        vendor=vendor,
        model=model,
        reasoning_effort=reasoning_effort,
        proxy_url=proxy_url,
    )


def render_requirements_analysis_stage_start(selection: RequirementsAnalysisAgentSelection) -> str:
    return "\n".join(
        [
            "进入需求澄清阶段（需求分析师）",
            f"vendor: {selection.vendor}",
            f"model: {selection.model}",
            f"reasoning_effort: {selection.reasoning_effort}",
            f"proxy_url: {selection.proxy_url or '(none)'}",
        ]
    )


def prompt_recreate_requirements_analysis_agent(
        *,
        reason_text: str,
        requirement_name: str,
        current_vendor: str,
        current_model: str,
        current_reasoning_effort: str,
        current_proxy_url: str,
        force_model_change: bool,
) -> RequirementsAnalysisAgentSelection | None:
    if not stdin_is_interactive():
        return None
    message(reason_text)
    if not prompt_yes_no("是否创建新的需求分析师继续当前阶段", True):
        return None
    while True:
        vendor = prompt_vendor(current_vendor)
        model = prompt_model(vendor, current_model if vendor == current_vendor else DEFAULT_MODEL_BY_VENDOR[vendor])
        reasoning_effort = prompt_effort(vendor, model, current_reasoning_effort)
        proxy_url = prompt_proxy_url(current_proxy_url)
        if (not force_model_change) or vendor != current_vendor or model != current_model:
            selection = RequirementsAnalysisAgentSelection(
                vendor=vendor,
                model=model,
                reasoning_effort=reasoning_effort,
                proxy_url=proxy_url,
            )
            message(render_requirements_analysis_stage_start(selection))
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


def run_notion_reader(project_dir: str | Path, notion_url: str, requirement_name: str) -> InputReadResult:
    project_root = resolve_existing_directory(project_dir)
    runtime_root = project_root / NOTION_RUNTIME_ROOT_NAME
    output_path, question_path, record_path = build_notion_hitl_paths(project_root, requirement_name)
    worker = TmuxBatchWorker(
        worker_id="requirements-notion-reader",
        work_dir=project_root,
        config=AgentRunConfig(
            vendor="codex",
            model=DEFAULT_NOTION_MODEL,
            reasoning_effort=DEFAULT_NOTION_EFFORT,
        ),
        runtime_root=runtime_root,
    )
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
                                cleanup_paths=(
                                    record_path,
                                    runtime_dir,
                                    runtime_root,
                                ),
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
                    cleanup_paths=(
                        record_path,
                        runtime_dir,
                        runtime_root,
                    ),
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
) -> RequirementsStageResult:
    project_root = resolve_existing_directory(project_dir)
    runtime_root = project_root / NOTION_RUNTIME_ROOT_NAME
    original_requirement_path, requirements_clear_path, ask_human_path, hitl_record_path = (
        build_requirements_analysis_paths(project_root, requirement_name)
    )
    hitl_record_path = ensure_requirements_hitl_record_file(project_root, requirement_name)
    if not original_requirement_path.exists() or not original_requirement_path.read_text(encoding="utf-8").strip():
        raise RuntimeError(f"缺少原始需求文档: {original_requirement_path}")

    current_vendor = vendor
    current_model = model
    current_reasoning_effort = reasoning_effort
    current_proxy_url = proxy_url
    current_resume_existing = bool(resume_existing)
    keep_worker_alive = False
    worker: TmuxBatchWorker | None = None
    runtime_dir = runtime_root

    progress_monitor = SingleLineSpinnerMonitor(
        frame_builder=lambda tick: render_requirements_analysis_progress_line(
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
        message(render_requirements_analysis_tmux_start_summary(live_worker))

    try:
        while True:
            worker = TmuxBatchWorker(
                worker_id="requirements-analyst",
                work_dir=project_root,
                config=AgentRunConfig(
                    vendor=current_vendor,
                    model=current_model,
                    reasoning_effort=current_reasoning_effort,
                    proxy_url=current_proxy_url,
                ),
                runtime_root=runtime_root,
            )
            runtime_dir = worker.runtime_dir
            stage_status_path = runtime_dir / "requirements_analysis_status.json"
            turns_root = runtime_dir / "turns"

            def initial_prompt_builder(context: HitlPromptContext) -> str:
                prompt_builder = resume_requirements_understand if current_resume_existing else requirements_understand
                return prompt_builder(
                    fintech_ba,
                    original_requirement_md=str(original_requirement_path.resolve()),
                    requirements_clear_md=str(Path(context.output_path).resolve()),
                    ask_human_md=str(Path(context.question_path).resolve()),
                    hitl_record_md=str(Path(context.record_path).resolve()),
                )

            def hitl_prompt_builder(human_msg: str, context: HitlPromptContext) -> str:
                return hitl_bck(
                    human_msg,
                    original_requirement_md=str(original_requirement_path.resolve()),
                    hitl_record_md=str(Path(context.record_path).resolve()),
                    requirements_clear_md=str(Path(context.output_path).resolve()),
                    ask_human_md=str(Path(context.question_path).resolve()),
                )

            try:
                loop_result = run_hitl_agent_loop(
                    worker=worker,
                    stage_name=REQUIREMENTS_ANALYSIS_STAGE_NAME,
                    output_path=requirements_clear_path,
                    question_path=ask_human_path,
                    record_path=hitl_record_path,
                    stage_status_path=stage_status_path,
                    turns_root=turns_root,
                    initial_prompt_builder=initial_prompt_builder,
                    hitl_prompt_builder=hitl_prompt_builder,
                    label_prefix="requirements_analysis",
                    turn_phase=REQUIREMENTS_ANALYSIS_TURN_PHASE,
                    on_worker_starting=lambda live_worker: start_boot_progress(),
                    on_worker_started=handle_worker_started,
                    on_agent_turn_started=lambda context, live_worker: start_progress(),
                    on_agent_turn_finished=lambda context, live_worker: stop_progress(),
                    timeout_sec=DEFAULT_COMMAND_TIMEOUT_SEC,
                )
                if str(loop_result.decision.payload.get("status", "")).strip() != REQUIREMENTS_STATUS_OK:
                    raise RuntimeError(loop_result.decision.summary or "需求澄清未完成闭环")
                if not requirements_clear_path.exists():
                    raise RuntimeError("需求澄清未生成需求澄清文档")
                if not requirements_clear_path.read_text(encoding="utf-8").strip():
                    raise RuntimeError("需求澄清文档为空")
                handoff = None
                cleanup_paths: tuple[str, ...] = (
                    str(ask_human_path.resolve()),
                )
                if preserve_ba_worker:
                    keep_worker_alive = True
                    handoff = RequirementsAnalystHandoff(
                        worker=worker,
                        vendor=worker.config.vendor.value,
                        model=worker.config.model,
                        reasoning_effort=worker.config.reasoning_effort,
                        proxy_url=worker.config.proxy_url,
                    )
                else:
                    cleanup_paths = (
                        str(ask_human_path.resolve()),
                        str(Path(runtime_dir).expanduser().resolve()),
                        str(Path(runtime_root).expanduser().resolve()),
                    )
                return RequirementsStageResult(
                    project_dir=str(project_root),
                    requirement_name=requirement_name,
                    requirements_clear_path=str(requirements_clear_path.resolve()),
                    cleanup_paths=cleanup_paths,
                    ba_handoff=handoff,
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
                        except Exception:
                            pass
                    selection = prompt_recreate_requirements_analysis_agent(
                        reason_text=f"检测到需求分析师仍在 agent 界面，但模型认证已失效: {requirement_name}\n需要更换模型后继续当前阶段。",
                        requirement_name=requirement_name,
                        current_vendor=current_vendor,
                        current_model=current_model,
                        current_reasoning_effort=current_reasoning_effort,
                        current_proxy_url=current_proxy_url,
                        force_model_change=True,
                    )
                    if selection is not None:
                        current_vendor = selection.vendor
                        current_model = selection.model
                        current_reasoning_effort = selection.reasoning_effort
                        current_proxy_url = selection.proxy_url
                        current_resume_existing = (
                            current_resume_existing
                            or bool(get_markdown_content(requirements_clear_path).strip())
                        )
                        keep_worker_alive = False
                        continue
                    raise RuntimeError("需求分析师认证已失效，且用户未更换模型") from error
                if ready_timeout_error:
                    if not keep_worker_alive:
                        try:
                            worker.request_kill()
                        except Exception:
                            pass
                    selection = prompt_recreate_requirements_analysis_agent(
                        reason_text=f"需求分析师启动超时，未能进入可输入状态: {requirement_name}\n请重新选择模型后继续当前阶段。",
                        requirement_name=requirement_name,
                        current_vendor=current_vendor,
                        current_model=current_model,
                        current_reasoning_effort=current_reasoning_effort,
                        current_proxy_url=current_proxy_url,
                        force_model_change=True,
                    )
                    if selection is not None:
                        current_vendor = selection.vendor
                        current_model = selection.model
                        current_reasoning_effort = selection.reasoning_effort
                        current_proxy_url = selection.proxy_url
                        current_resume_existing = (
                            current_resume_existing
                            or bool(get_markdown_content(requirements_clear_path).strip())
                        )
                        keep_worker_alive = False
                        continue
                    raise RuntimeError("需求分析师启动超时，且用户未更换模型") from error
                if is_worker_death_error(error):
                    resumed = try_resume_worker(worker, timeout_sec=60.0)
                    if resumed:
                        try:
                            worker.request_kill()
                        except Exception:
                            pass
                        current_resume_existing = (
                            current_resume_existing
                            or bool(get_markdown_content(requirements_clear_path).strip())
                        )
                        keep_worker_alive = False
                        continue
                    if not keep_worker_alive:
                        try:
                            worker.request_kill()
                        except Exception:
                            pass
                    selection = prompt_recreate_requirements_analysis_agent(
                        reason_text=f"检测到需求分析师已死亡，且 resume 失败: {requirement_name}\n需要更换模型后继续当前阶段。",
                        requirement_name=requirement_name,
                        current_vendor=current_vendor,
                        current_model=current_model,
                        current_reasoning_effort=current_reasoning_effort,
                        current_proxy_url=current_proxy_url,
                        force_model_change=True,
                    )
                    if selection is not None:
                        current_vendor = selection.vendor
                        current_model = selection.model
                        current_reasoning_effort = selection.reasoning_effort
                        current_proxy_url = selection.proxy_url
                        current_resume_existing = (
                            current_resume_existing
                            or bool(get_markdown_content(requirements_clear_path).strip())
                        )
                        keep_worker_alive = False
                        continue
                raise
    finally:
        stop_progress()
        stop_boot_progress()
        if worker is not None and not keep_worker_alive:
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
        return InputReadResult(
            content=ensure_non_empty_content(result.content),
            cleanup_paths=result.cleanup_paths,
        )
    raise ValueError(f"不支持的输入方式: {request.input_type}")


def write_requirement_file(output_path: str | Path, content: str, *, overwrite: bool) -> Path:
    path = Path(output_path)
    if path.exists() and not overwrite:
        raise FileExistsError(f"文件已存在: {path}")
    path.write_text(str(content).strip() + "\n", encoding="utf-8")
    return path


def collect_request(args: argparse.Namespace) -> RequirementIntakeRequest:
    project_dir = (
        str(resolve_existing_directory(args.project_dir))
        if args.project_dir
        else prompt_project_dir("")
    )
    reuse_existing_original_requirement = False
    if args.requirement_name:
        requirement_name = str(args.requirement_name).strip()
    else:
        selection = prompt_requirement_name_selection(project_dir, "")
        requirement_name = selection.requirement_name
        reuse_existing_original_requirement = selection.reuse_existing_original_requirement

    input_type = ""
    input_value = ""
    if not reuse_existing_original_requirement:
        input_type = args.input_type or prompt_input_type("text")

        input_value = str(args.input_value or "").strip()
        if input_type == "file" and not input_value:
            input_value = prompt_with_default("本地文件路径", "", allow_empty=False)
        elif input_type == "notion" and not input_value:
            input_value = prompt_with_default("Notion 页面链接", "", allow_empty=False)
        elif input_type == "text" and input_value:
            input_value = str(args.input_value)

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


def run_requirements_stage(
        argv: Sequence[str] | None = None,
        *,
        preserve_ba_worker: bool = False,
) -> RequirementsStageResult:
    parser = build_parser()
    args = parser.parse_args(argv)
    request = collect_request(args)
    ensure_pre_development_task_record(request.project_dir, request.requirement_name)
    output_path = build_output_path(request.project_dir, request.requirement_name)

    if not request.reuse_existing_original_requirement and output_path.exists() and not request.overwrite:
        if request.auto_confirm:
            raise RuntimeError(f"目标文件已存在，未指定 --overwrite: {output_path}")
        if not maybe_confirm_overwrite(output_path, overwrite=False):
            raise RuntimeError("已取消写入。")

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

    message("进入需求澄清阶段")
    has_existing_clarification = has_existing_requirements_clarification(
        request.project_dir,
        request.requirement_name,
    )
    should_direct_reuse_requirements_clarification = False
    if has_existing_clarification:
        should_direct_reuse_requirements_clarification = should_reuse_existing_requirements_clarification(
            request.project_dir,
            request.requirement_name,
            overwrite=request.overwrite,
            interactive=stdin_is_interactive(),
        )
    if has_existing_clarification and should_direct_reuse_requirements_clarification:
        message("复用已有的需求澄清，直接进入下一阶段")
        analysis_result = reuse_existing_requirements_clarification(
            request.project_dir,
            request.requirement_name,
        )
    elif has_existing_clarification:
        message("不直接复用已有需求澄清，将启动需求分析师基于现有澄清继续核验")
        analysis_selection = collect_requirements_analysis_agent_selection(args)
        message(render_requirements_analysis_stage_start(analysis_selection))
        analysis_result = run_requirements_analysis(
            request.project_dir,
            request.requirement_name,
            vendor=analysis_selection.vendor,
            model=analysis_selection.model,
            reasoning_effort=analysis_selection.reasoning_effort,
            proxy_url=analysis_selection.proxy_url,
            resume_existing=True,
            preserve_ba_worker=preserve_ba_worker,
        )
    else:
        message("执行摘要: 未检测到可复用的需求澄清，需要启动需求分析师智能体执行需求澄清；请为需求分析师选择厂商、模型、推理强度、代理端口。")
        analysis_selection = collect_requirements_analysis_agent_selection(args)
        message(render_requirements_analysis_stage_start(analysis_selection))
        analysis_result = run_requirements_analysis(
            request.project_dir,
            request.requirement_name,
            vendor=analysis_selection.vendor,
            model=analysis_selection.model,
            reasoning_effort=analysis_selection.reasoning_effort,
            proxy_url=analysis_selection.proxy_url,
            resume_existing=False,
            preserve_ba_worker=preserve_ba_worker,
        )
    mark_requirement_clarification_completed(request.project_dir, request.requirement_name)
    cleanup_paths = analysis_result.cleanup_paths
    if cleanup_paths:
        cleanup_runtime_paths(cleanup_paths)
        cleanup_paths = ()
    return RequirementsStageResult(
        project_dir=str(resolve_existing_directory(request.project_dir)),
        requirement_name=request.requirement_name,
        requirements_clear_path=analysis_result.requirements_clear_path,
        cleanup_paths=cleanup_paths,
        ba_handoff=getattr(analysis_result, "ba_handoff", None),
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
