# -*- encoding: utf-8 -*-
"""
@File: T12_requirements_common.py
@Modify Time: 2026/4/17
@Author: Kevin-Chen
@Descriptions: 需求录入/澄清/评审共享工具
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from canopy_core.requirements_scope import (
    CREATE_NEW_REQUIREMENT_SELECTION_VALUE,
)
from T03_agent_init_workflow import resolve_existing_directory
from T09_terminal_ops import prompt_metadata, prompt_select_option, terminal_ui_is_interactive
from T09_terminal_ops import prompt_with_default as terminal_prompt_with_default


DEFAULT_REQUIREMENTS_CLARIFICATION_VENDOR = "codex"
DEFAULT_REQUIREMENTS_CLARIFICATION_MODEL = "gpt-5.4"
DEFAULT_REQUIREMENTS_CLARIFICATION_EFFORT = "high"

DEFAULT_REQUIREMENTS_ANALYSIS_VENDOR = DEFAULT_REQUIREMENTS_CLARIFICATION_VENDOR
DEFAULT_REQUIREMENTS_ANALYSIS_MODEL = DEFAULT_REQUIREMENTS_CLARIFICATION_MODEL
DEFAULT_REQUIREMENTS_ANALYSIS_EFFORT = DEFAULT_REQUIREMENTS_CLARIFICATION_EFFORT


@dataclass(frozen=True)
class RequirementNameSelection:
    requirement_name: str
    reuse_existing_original_requirement: bool = False


@dataclass(frozen=True)
class RequirementsAnalystHandoff:
    worker: object
    vendor: str
    model: str
    reasoning_effort: str
    proxy_url: str


def prompt_with_default(prompt_text: str, default: str = "", allow_empty: bool = False) -> str:
    return terminal_prompt_with_default(prompt_text, default, allow_empty)


def stdin_is_interactive() -> bool:
    return terminal_ui_is_interactive()


def sanitize_requirement_name(name: str) -> str:
    text = str(name or "").strip()
    text = re.sub(r"\s+", "_", text)
    text = re.sub(r'[\/\\:\*\?"<>\|]+', "_", text)
    text = re.sub(r"[^0-9A-Za-z_\-\u4e00-\u9fff]+", "_", text)
    text = re.sub(r"_+", "_", text).strip("._")
    return text or "需求"


def prompt_project_dir(default: str = "") -> str:
    last_error = ""
    while True:
        with prompt_metadata(error_message=last_error):
            candidate = str(prompt_with_default("输入项目工作目录", default)).strip()
        default = candidate
        try:
            if not Path(candidate).expanduser().is_absolute():
                raise ValueError("请输入绝对路径")
            return str(resolve_existing_directory(candidate))
        except Exception as error:  # noqa: BLE001
            from T09_terminal_ops import message
            last_error = f"目录无效: {error}"
            message(last_error)


def prompt_requirement_name(default: str = "") -> str:
    while True:
        name = prompt_with_default("输入需求名称", default)
        if sanitize_requirement_name(name):
            return name
        from T09_terminal_ops import message
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
        options=[*[(item, item) for item in existing], (CREATE_NEW_REQUIREMENT_SELECTION_VALUE, "创建新需求")],
        default_value=CREATE_NEW_REQUIREMENT_SELECTION_VALUE,
        prompt_text="选择已有需求或创建新需求",
    )
    if selected == CREATE_NEW_REQUIREMENT_SELECTION_VALUE:
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


def build_output_path(project_dir: str | Path, requirement_name: str) -> Path:
    root = resolve_existing_directory(project_dir)
    safe_name = sanitize_requirement_name(requirement_name)
    return root / f"{safe_name}_原始需求.md"


def build_requirements_clarification_paths(project_dir: str | Path, requirement_name: str) -> tuple[Path, Path, Path, Path]:
    project_root = resolve_existing_directory(project_dir)
    safe_name = sanitize_requirement_name(requirement_name)
    original_requirement_path = build_output_path(project_root, requirement_name)
    requirements_clear_path = project_root / f"{safe_name}_需求澄清.md"
    ask_human_path = project_root / f"{safe_name}_与人类交流.md"
    hitl_record_path = project_root / f"{safe_name}_人机交互澄清记录.md"
    return original_requirement_path, requirements_clear_path, ask_human_path, hitl_record_path


def build_requirements_analysis_paths(project_dir: str | Path, requirement_name: str) -> tuple[Path, Path, Path, Path]:
    return build_requirements_clarification_paths(project_dir, requirement_name)


def build_legacy_requirements_hitl_record_path(project_dir: str | Path, requirement_name: str) -> Path:
    project_root = resolve_existing_directory(project_dir)
    safe_name = sanitize_requirement_name(requirement_name)
    return project_root / f"{safe_name}人机交互澄清记录.md"


def clear_requirements_human_exchange_file(project_dir: str | Path, requirement_name: str) -> Path:
    _, _, ask_human_path, _ = build_requirements_clarification_paths(project_dir, requirement_name)
    if ask_human_path.exists():
        ask_human_path.write_text("", encoding="utf-8")
    return ask_human_path.resolve()


def ensure_requirements_hitl_record_file(project_dir: str | Path, requirement_name: str) -> Path:
    _, _, _, hitl_record_path = build_requirements_clarification_paths(project_dir, requirement_name)
    legacy_path = build_legacy_requirements_hitl_record_path(project_dir, requirement_name)
    hitl_record_path.parent.mkdir(parents=True, exist_ok=True)
    if hitl_record_path.exists():
        return hitl_record_path.resolve()
    if legacy_path.exists():
        legacy_path.replace(hitl_record_path)
        return hitl_record_path.resolve()
    hitl_record_path.write_text("", encoding="utf-8")
    return hitl_record_path.resolve()


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


def cleanup_runtime_root_if_empty(runtime_root: str | Path) -> tuple[str, ...]:
    root = Path(runtime_root).expanduser().resolve()
    if root.exists() and root.is_dir() and not any(root.iterdir()):
        root.rmdir()
        return (str(root),)
    return ()
