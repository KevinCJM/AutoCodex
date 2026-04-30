from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Sequence


CREATE_NEW_REQUIREMENT_SELECTION_VALUE = "__create_new__"


def build_requirement_scope_lock_prompt(*artifact_paths: str | Path) -> str:
    normalized: list[str] = []
    for item in artifact_paths:
        text = str(item or "").strip()
        if text and text not in normalized:
            normalized.append(text)
    if not normalized:
        return ""
    bullet_lines = "\n".join(f"- 《{item}》" for item in normalized)
    return f"""## Requirement Scope Lock
- 当前任务的唯一需求范围仅限以下文件：
{bullet_lines}
- 如果工作目录中还存在其他前缀的 sibling 文件（例如其他 `{chr(123)}需求名{chr(125)}_*.md` / `{chr(123)}需求名{chr(125)}_*.json`，包括不属于本轮的 `_开发前期.json`），一律视为历史或旁路上下文，禁止作为当前任务依据，除非本提示词显式引用。
- 本提示词里显式给出的文件路径优先级高于路由层 docs 中的 `first_read_files` / `then_check_files`。"""


def _prompt_option_values(options: Sequence[object]) -> set[str]:
    allowed_values: set[str] = set()
    for item in options:
        if not isinstance(item, Mapping):
            continue
        value = str(item.get("value", "")).strip()
        if value:
            allowed_values.add(value)
    return allowed_values


def resolve_requirement_name_from_prompt_response(
    *,
    prompt_marker: str,
    payload: Mapping[str, Any],
    options: Sequence[object] = (),
) -> str | None:
    marker = str(prompt_marker or "").strip()
    value = str(payload.get("value", "")).strip()
    if not marker or not value:
        return None
    if "需求名称" in marker:
        return value
    if "选择已有需求或创建新需求" not in marker:
        return None
    if value == CREATE_NEW_REQUIREMENT_SELECTION_VALUE:
        return None
    return value if value in _prompt_option_values(options) else None
