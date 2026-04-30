from __future__ import annotations

import functools
import inspect
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

ACCESS_READ = "read"
ACCESS_WRITE = "write"
ACCESS_READ_WRITE = "read_write"

CHANGE_NONE = "none"
CHANGE_MAY_CHANGE = "may_change"
CHANGE_MUST_EXIST_NONEMPTY = "must_exist_nonempty"
CHANGE_MUST_CHANGE = "must_change"
CHANGE_MUST_NOT_CHANGE = "must_not_change"

CLEANUP_NONE = "none"
CLEANUP_SYSTEM_BEFORE_TURN = "system_before_turn"
CLEANUP_SYSTEM_AFTER_HITL_RESOLVED = "system_after_hitl_resolved"
CLEANUP_SYSTEM_BEFORE_STAGE_OR_RETRY = "system_before_stage_or_retry"
CLEANUP_AGENT_INSIDE_FILE = "agent_inside_file"

SPECIAL_NONE = "none"
SPECIAL_OPEN_HITL = "open_hitl"
SPECIAL_REVIEW_PASS = "review_pass"
SPECIAL_REVIEW_FAIL = "review_fail"
SPECIAL_STAGE_ARTIFACT = "stage_artifact"

_VALID_ACCESS = {ACCESS_READ, ACCESS_WRITE, ACCESS_READ_WRITE}
_VALID_CHANGE = {
    CHANGE_NONE,
    CHANGE_MAY_CHANGE,
    CHANGE_MUST_EXIST_NONEMPTY,
    CHANGE_MUST_CHANGE,
    CHANGE_MUST_NOT_CHANGE,
}
_VALID_CLEANUP = {
    CLEANUP_NONE,
    CLEANUP_SYSTEM_BEFORE_TURN,
    CLEANUP_SYSTEM_AFTER_HITL_RESOLVED,
    CLEANUP_SYSTEM_BEFORE_STAGE_OR_RETRY,
    CLEANUP_AGENT_INSIDE_FILE,
}
_VALID_SPECIAL = {
    SPECIAL_NONE,
    SPECIAL_OPEN_HITL,
    SPECIAL_REVIEW_PASS,
    SPECIAL_REVIEW_FAIL,
    SPECIAL_STAGE_ARTIFACT,
}


@dataclass(frozen=True)
class FileSpec:
    path_arg: str
    access: str = ACCESS_READ
    change: str = CHANGE_NONE
    meaning: str = ""
    cleanup: str = CLEANUP_NONE
    special: str = SPECIAL_NONE

    def __post_init__(self) -> None:
        if not str(self.path_arg).strip():
            raise ValueError("FileSpec.path_arg is required")
        if self.access not in _VALID_ACCESS:
            raise ValueError(f"invalid file access: {self.access}")
        if self.change not in _VALID_CHANGE:
            raise ValueError(f"invalid file change policy: {self.change}")
        if self.cleanup not in _VALID_CLEANUP:
            raise ValueError(f"invalid file cleanup policy: {self.cleanup}")
        if self.special not in _VALID_SPECIAL:
            raise ValueError(f"invalid file special policy: {self.special}")


@dataclass(frozen=True)
class OutcomeSpec:
    status: str
    requires: tuple[str, ...] = ()
    optional: tuple[str, ...] = ()
    forbids: tuple[str, ...] = ()
    special: str = SPECIAL_NONE

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", str(self.status).strip())
        object.__setattr__(self, "requires", tuple(str(item).strip() for item in self.requires if str(item).strip()))
        object.__setattr__(self, "optional", tuple(str(item).strip() for item in self.optional if str(item).strip()))
        object.__setattr__(self, "forbids", tuple(str(item).strip() for item in self.forbids if str(item).strip()))
        if not self.status:
            raise ValueError("OutcomeSpec.status is required")
        if self.special not in _VALID_SPECIAL:
            raise ValueError(f"invalid outcome special policy: {self.special}")


@dataclass(frozen=True)
class PromptSpec:
    prompt_id: str
    stage: str
    role: str
    intent: str
    files: dict[str, FileSpec] = field(default_factory=dict)
    outcomes: dict[str, OutcomeSpec] = field(default_factory=dict)
    mode: str = ""
    prompt_appendix: bool = True

    def __post_init__(self) -> None:
        for field_name in ("prompt_id", "stage", "role", "intent"):
            if not str(getattr(self, field_name)).strip():
                raise ValueError(f"PromptSpec.{field_name} is required")
        object.__setattr__(self, "mode", str(self.mode or self.prompt_id).strip())
        file_aliases = set(self.files)
        for outcome_name, outcome in self.outcomes.items():
            if not isinstance(outcome, OutcomeSpec):
                raise TypeError(f"outcome {outcome_name} must be OutcomeSpec")
            referenced = set(outcome.requires) | set(outcome.optional) | set(outcome.forbids)
            unknown = sorted(referenced - file_aliases)
            if unknown:
                raise ValueError(f"outcome {outcome_name} references unknown file aliases: {', '.join(unknown)}")


@dataclass(frozen=True)
class ResolvedPromptSpec:
    spec: PromptSpec
    files: dict[str, Path]


def agent_prompt(
    *,
    prompt_id: str,
    stage: str,
    role: str,
    intent: str,
    files: dict[str, FileSpec] | None = None,
    outcomes: dict[str, OutcomeSpec] | None = None,
    mode: str = "",
    prompt_appendix: bool = True,
) -> Callable[[Callable[..., str]], Callable[..., str]]:
    spec = PromptSpec(
        prompt_id=prompt_id,
        stage=stage,
        role=role,
        intent=intent,
        files=files or {},
        outcomes=outcomes or {},
        mode=mode,
        prompt_appendix=prompt_appendix,
    )

    def decorator(fn: Callable[..., str]) -> Callable[..., str]:
        setattr(fn, "__tmux_prompt_spec__", spec)
        return fn

    return decorator


def prompt_helper(*, no_turn: bool = True) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        setattr(fn, "__tmux_prompt_helper__", bool(no_turn))
        return fn

    return decorator


def copy_prompt_metadata(source: Callable[..., Any], target: Callable[..., Any]) -> Callable[..., Any]:
    spec = get_prompt_spec(source)
    if spec is not None:
        setattr(target, "__tmux_prompt_spec__", spec)
    if is_prompt_helper(source):
        setattr(target, "__tmux_prompt_helper__", True)
    return target


def wraps_prompt(source: Callable[..., Any]) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorator(target: Callable[..., Any]) -> Callable[..., Any]:
        wrapped = functools.wraps(source)(target)
        return copy_prompt_metadata(source, wrapped)

    return decorator


def get_prompt_spec(fn: Callable[..., Any]) -> PromptSpec | None:
    spec = getattr(fn, "__tmux_prompt_spec__", None)
    return spec if isinstance(spec, PromptSpec) else None


def require_prompt_spec(fn: Callable[..., Any]) -> PromptSpec:
    spec = get_prompt_spec(fn)
    if spec is None:
        raise ValueError(f"{getattr(fn, '__name__', fn)!r} is missing @agent_prompt metadata")
    return spec


def is_prompt_helper(fn: Callable[..., Any]) -> bool:
    return bool(getattr(fn, "__tmux_prompt_helper__", False))


def bind_prompt_arguments(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> inspect.BoundArguments:
    signature = inspect.signature(fn)
    bound = signature.bind_partial(*args, **kwargs)
    bound.apply_defaults()
    return bound


def resolve_prompt_files(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> ResolvedPromptSpec:
    spec = require_prompt_spec(fn)
    bound = bind_prompt_arguments(fn, *args, **kwargs)
    resolved: dict[str, Path] = {}
    missing: list[str] = []
    for alias, file_spec in spec.files.items():
        if file_spec.path_arg not in bound.arguments:
            missing.append(f"{alias}:{file_spec.path_arg}")
            continue
        raw_path = bound.arguments[file_spec.path_arg]
        if raw_path is None or not str(raw_path).strip():
            missing.append(f"{alias}:{file_spec.path_arg}")
            continue
        resolved[alias] = Path(str(raw_path)).expanduser().resolve()
    if missing:
        raise ValueError(f"{spec.prompt_id} missing prompt path args: {', '.join(missing)}")
    return ResolvedPromptSpec(spec=spec, files=resolved)


def render_prompt_contract_appendix(resolved: ResolvedPromptSpec) -> str:
    spec = resolved.spec
    lines = [
        "",
        "---",
        "## 本轮文件契约 (Machine Contract)",
        f"- prompt_id: `{spec.prompt_id}`",
        f"- stage: `{spec.stage}`",
        f"- role: `{spec.role}`",
        f"- intent: `{spec.intent}`",
    ]
    if resolved.files:
        lines.append("- files:")
        for alias, path in sorted(resolved.files.items()):
            file_spec = spec.files[alias]
            detail = [
                f"access={file_spec.access}",
                f"change={file_spec.change}",
                f"cleanup={file_spec.cleanup}",
            ]
            if file_spec.special != SPECIAL_NONE:
                detail.append(f"special={file_spec.special}")
            meaning = f" ; {file_spec.meaning}" if file_spec.meaning else ""
            lines.append(f"  - `{alias}` -> `{path}` ({', '.join(detail)}){meaning}")
    if spec.outcomes:
        lines.append("- outcomes:")
        for outcome_name, outcome in sorted(spec.outcomes.items()):
            parts = [f"status={outcome.status}"]
            if outcome.requires:
                parts.append("requires=" + ",".join(outcome.requires))
            if outcome.optional:
                parts.append("optional=" + ",".join(outcome.optional))
            if outcome.forbids:
                parts.append("forbids=" + ",".join(outcome.forbids))
            if outcome.special != SPECIAL_NONE:
                parts.append(f"special={outcome.special}")
            lines.append(f"  - `{outcome_name}`: {'; '.join(parts)}")
    lines.extend(
        [
            "- Change policies are outcome-scoped: enforce `change` only for aliases listed in the selected outcome.",
            "- Aliases in `forbids` must be absent or empty for the selected outcome.",
            "- stdout is not the completion truth.",
            "- Follow the file contract exactly before returning.",
        ]
    )
    return "\n".join(lines)


__all__ = [
    "ACCESS_READ",
    "ACCESS_READ_WRITE",
    "ACCESS_WRITE",
    "CHANGE_MAY_CHANGE",
    "CHANGE_MUST_CHANGE",
    "CHANGE_MUST_EXIST_NONEMPTY",
    "CHANGE_MUST_NOT_CHANGE",
    "CHANGE_NONE",
    "CLEANUP_AGENT_INSIDE_FILE",
    "CLEANUP_NONE",
    "CLEANUP_SYSTEM_AFTER_HITL_RESOLVED",
    "CLEANUP_SYSTEM_BEFORE_STAGE_OR_RETRY",
    "CLEANUP_SYSTEM_BEFORE_TURN",
    "SPECIAL_NONE",
    "SPECIAL_OPEN_HITL",
    "SPECIAL_REVIEW_FAIL",
    "SPECIAL_REVIEW_PASS",
    "SPECIAL_STAGE_ARTIFACT",
    "FileSpec",
    "OutcomeSpec",
    "PromptSpec",
    "ResolvedPromptSpec",
    "agent_prompt",
    "bind_prompt_arguments",
    "copy_prompt_metadata",
    "get_prompt_spec",
    "is_prompt_helper",
    "prompt_helper",
    "render_prompt_contract_appendix",
    "require_prompt_spec",
    "resolve_prompt_files",
    "wraps_prompt",
]
