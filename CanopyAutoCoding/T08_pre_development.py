# -*- encoding: utf-8 -*-
"""
@File: T08_pre_development.py
@Modify Time: 2026/4/16
@Author: Kevin-Chen
@Descriptions: 开发前期阶段状态共享工具
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from T01_tools import write_dict_to_json


PRE_DEVELOPMENT_STAGE_KEYS = (
    "需求录入",
    "需求澄清",
    "需求评审",
    "详细设计",
    "任务拆分",
)
PRE_DEVELOPMENT_STAGE_ALIASES = {
    "需求获取": "需求录入",
}


def resolve_project_root(project_dir: str | Path) -> Path:
    root = Path(project_dir).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        raise FileNotFoundError(f"项目目录不存在: {root}")
    return root


def sanitize_requirement_name(name: str) -> str:
    text = str(name or "").strip()
    text = re.sub(r"\s+", "_", text)
    text = re.sub(r'[\/\\:\*\?"<>\|]+', "_", text)
    text = re.sub(r"[^0-9A-Za-z_\-\u4e00-\u9fff]+", "_", text)
    text = re.sub(r"_+", "_", text).strip("._")
    return text or "需求"


def build_pre_development_task_record_payload() -> dict[str, dict[str, bool]]:
    return {
        "需求录入": {"需求录入": False},
        "需求澄清": {"需求澄清": False},
        "需求评审": {"需求评审": False},
        "详细设计": {"详细设计": False},
        "任务拆分": {"任务拆分": False},
    }


def build_pre_development_task_record_path(project_dir: str | Path, requirement_name: str) -> Path:
    project_root = resolve_project_root(project_dir)
    safe_name = sanitize_requirement_name(requirement_name)
    return (project_root / f"{safe_name}_开发前期.json").resolve()


def ensure_pre_development_task_record(project_dir: str | Path, requirement_name: str) -> Path:
    record_path = build_pre_development_task_record_path(project_dir, requirement_name)
    if record_path.exists():
        return record_path
    return write_dict_to_json(record_path, build_pre_development_task_record_payload())


def load_pre_development_task_record(file_path: str | Path) -> dict[str, dict[str, bool]]:
    path = Path(file_path).expanduser().resolve()
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"开发前期记录格式非法: {path}")
    normalized = build_pre_development_task_record_payload()
    for key, section in payload.items():
        canonical_key = PRE_DEVELOPMENT_STAGE_ALIASES.get(str(key), str(key))
        if canonical_key not in normalized:
            normalized[canonical_key] = section if isinstance(section, dict) else {}
            continue
        if isinstance(section, dict):
            normalized_section = normalized.get(canonical_key, {})
            for sub_key, completed in section.items():
                canonical_sub_key = PRE_DEVELOPMENT_STAGE_ALIASES.get(str(sub_key), str(sub_key))
                normalized_section[canonical_sub_key] = bool(completed)
            normalized[canonical_key] = normalized_section
    return normalized


def update_pre_development_task_status(
        project_dir: str | Path,
        requirement_name: str,
        *,
        task_key: str,
        completed: bool,
) -> Path:
    task_key = PRE_DEVELOPMENT_STAGE_ALIASES.get(task_key, task_key)
    if task_key not in PRE_DEVELOPMENT_STAGE_KEYS:
        raise ValueError(f"不支持的开发前期阶段: {task_key}")
    record_path = ensure_pre_development_task_record(project_dir, requirement_name)
    payload = load_pre_development_task_record(record_path)
    section = payload.get(task_key)
    if not isinstance(section, dict):
        section = {}
        payload[task_key] = section
    section[task_key] = bool(completed)
    record_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return record_path


def mark_requirement_intake_completed(project_dir: str | Path, requirement_name: str) -> Path:
    return update_pre_development_task_status(
        project_dir,
        requirement_name,
        task_key="需求录入",
        completed=True,
    )


def mark_requirement_clarification_completed(project_dir: str | Path, requirement_name: str) -> Path:
    return update_pre_development_task_status(
        project_dir,
        requirement_name,
        task_key="需求澄清",
        completed=True,
    )


def mark_requirement_review_completed(project_dir: str | Path, requirement_name: str) -> Path:
    return update_pre_development_task_status(
        project_dir,
        requirement_name,
        task_key="需求评审",
        completed=True,
    )


def mark_detailed_design_completed(project_dir: str | Path, requirement_name: str) -> Path:
    return update_pre_development_task_status(
        project_dir,
        requirement_name,
        task_key="详细设计",
        completed=True,
    )


def mark_task_split_completed(project_dir: str | Path, requirement_name: str) -> Path:
    return update_pre_development_task_status(
        project_dir,
        requirement_name,
        task_key="任务拆分",
        completed=True,
    )
