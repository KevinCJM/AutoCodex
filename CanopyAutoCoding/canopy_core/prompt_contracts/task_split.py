from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO

from canopy_core.prompt_contracts.spec import wraps_prompt
from Prompt_06_TaskSplit import (
    again_review_task,
    create_task_split_ba,
    modify_task,
    re_task_md_to_json as _re_task_md_to_json,
    review_task,
    task_md_to_json as _task_md_to_json,
    task_split,
)


def _normalize_prompt_output(builder, *args, **kwargs) -> str:  # noqa: ANN001
    capture = StringIO()
    with redirect_stdout(capture):
        prompt = builder(*args, **kwargs)
    if isinstance(prompt, str) and prompt.strip():
        return prompt
    printed = capture.getvalue().strip()
    if printed:
        return printed
    raise RuntimeError(f"{getattr(builder, '__name__', 'task_split_prompt')} 未生成有效提示词")


@wraps_prompt(_task_md_to_json)
def task_md_to_json(*args, **kwargs) -> str:  # noqa: ANN002, ANN003
    return _normalize_prompt_output(_task_md_to_json, *args, **kwargs)


@wraps_prompt(_re_task_md_to_json)
def re_task_md_to_json(*args, **kwargs) -> str:  # noqa: ANN002, ANN003
    return _normalize_prompt_output(_re_task_md_to_json, *args, **kwargs)

__all__ = [
    "again_review_task",
    "create_task_split_ba",
    "modify_task",
    "re_task_md_to_json",
    "review_task",
    "task_md_to_json",
    "task_split",
]
