from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from canopy_core.prompt_contracts.spec import (
    ACCESS_READ,
    ACCESS_READ_WRITE,
    ACCESS_WRITE,
    CHANGE_NONE,
    FileSpec,
    OutcomeSpec,
    ResolvedPromptSpec,
    get_prompt_spec,
    render_prompt_contract_appendix,
    resolve_prompt_files,
)
from canopy_core.runtime.contracts import (
    TaskResultContract,
    TurnFileContract,
    snapshot_file_fingerprint,
)
from canopy_core.runtime.tmux_runtime import DEFAULT_COMMAND_TIMEOUT_SEC, TmuxBatchWorker
from canopy_core.stage_kernel.turn_output_goals import (
    CompletionTurnGoal,
    OutcomeGoal,
    RepairPromptContext,
    TaskTurnGoal,
    build_default_completion_repair_prompt,
    build_default_task_repair_prompt,
    run_completion_turn_with_repair,
    run_task_result_turn_with_repair,
)


@dataclass(frozen=True)
class PromptTurnBuild:
    prompt: str
    resolved: ResolvedPromptSpec
    task_result_contract: TaskResultContract | None = None
    completion_contract: TurnFileContract | None = None
    task_turn_goal: TaskTurnGoal | None = None
    completion_turn_goal: CompletionTurnGoal | None = None


def _is_output_file(file_spec: FileSpec) -> bool:
    return file_spec.access in {ACCESS_WRITE, ACCESS_READ_WRITE} or file_spec.change != CHANGE_NONE


def _artifact_rule(alias: str, file_spec: FileSpec, path: Path) -> dict[str, object]:
    return {
        "alias": alias,
        "access": file_spec.access,
        "change": file_spec.change,
        "cleanup": file_spec.cleanup,
        "special": file_spec.special,
        "meaning": file_spec.meaning,
        "baseline": snapshot_file_fingerprint(path),
    }


def _build_outcome_artifacts(resolved: ResolvedPromptSpec) -> dict[str, dict[str, tuple[str, ...]]]:
    outcome_artifacts: dict[str, dict[str, tuple[str, ...]]] = {}
    for outcome in resolved.spec.outcomes.values():
        status = str(outcome.status).strip()
        if not status:
            continue
        existing = outcome_artifacts.setdefault(status, {"requires": (), "optional": (), "forbids": ()})
        outcome_artifacts[status] = {
            "requires": tuple(dict.fromkeys(existing["requires"] + outcome.requires)),
            "optional": tuple(dict.fromkeys(existing["optional"] + outcome.optional)),
            "forbids": tuple(dict.fromkeys(existing["forbids"] + outcome.forbids)),
        }
    return outcome_artifacts


def _build_artifact_maps(
    resolved: ResolvedPromptSpec,
) -> tuple[dict[str, Path], dict[str, Path], dict[str, object], dict[str, dict[str, tuple[str, ...]]]]:
    required: dict[str, Path] = {}
    optional: dict[str, Path] = {}
    rules: dict[str, object] = {}
    outcomes = tuple(resolved.spec.outcomes.values())
    single_outcome_requires = set(outcomes[0].requires) if len(outcomes) == 1 else set()
    for alias, path in resolved.files.items():
        file_spec = resolved.spec.files[alias]
        if not _is_output_file(file_spec):
            optional[alias] = path
            continue
        if alias in single_outcome_requires:
            required[alias] = path
        else:
            optional[alias] = path
        rules[alias] = _artifact_rule(alias, file_spec, path)
    return required, optional, rules, _build_outcome_artifacts(resolved)


def build_task_result_contract_from_prompt(
    resolved: ResolvedPromptSpec,
    *,
    turn_status_path: str | Path | None = None,
    stage_status_path: str | Path | None = None,
    stage_name: str = "",
) -> TaskResultContract:
    required, optional, rules, outcome_artifacts = _build_artifact_maps(resolved)
    statuses = tuple(dict.fromkeys(outcome.status for outcome in resolved.spec.outcomes.values()))
    return TaskResultContract(
        turn_id=resolved.spec.mode,
        phase=resolved.spec.mode,
        task_kind=resolved.spec.mode,
        mode=resolved.spec.mode,
        expected_statuses=statuses,
        stage_name=stage_name or resolved.spec.stage,
        turn_status_path=turn_status_path,
        stage_status_path=stage_status_path,
        required_artifacts=required,
        optional_artifacts=optional,
        artifact_rules=rules,
        outcome_artifacts=outcome_artifacts,
    )


def build_completion_contract_from_prompt(
    resolved: ResolvedPromptSpec,
    *,
    status_path: str | Path,
    validator: Callable[[Path], Any],
    kind: str = "",
    quiet_window_sec: float = 1.0,
) -> TurnFileContract:
    tracked: dict[str, Path] = {}
    rules: dict[str, object] = {}
    for alias, path in resolved.files.items():
        file_spec = resolved.spec.files[alias]
        if not _is_output_file(file_spec):
            continue
        tracked[alias] = path
        rules[alias] = _artifact_rule(alias, file_spec, path)
    return TurnFileContract(
        turn_id=resolved.spec.mode,
        phase=resolved.spec.stage,
        status_path=Path(status_path).expanduser().resolve(),
        validator=validator,
        quiet_window_sec=quiet_window_sec,
        kind=kind,
        tracked_artifacts=tracked,
        artifact_rules=rules,
        outcome_artifacts=_build_outcome_artifacts(resolved),
    )


def _outcome_goal(outcome: OutcomeSpec) -> OutcomeGoal:
    return OutcomeGoal(
        status=outcome.status,
        required_aliases=outcome.requires,
        optional_aliases=outcome.optional,
        forbidden_aliases=outcome.forbids,
        description=outcome.special,
    )


def _build_spec_task_repair_prompt(context: RepairPromptContext) -> str:
    return (
        build_default_task_repair_prompt(context)
        + "\n\n本轮 contract 来自 @agent_prompt 元数据；只修正缺失、未变化或 forbidden 的文件 alias。"
    )


def _build_spec_completion_repair_prompt(context: RepairPromptContext) -> str:
    return (
        build_default_completion_repair_prompt(context)
        + "\n\n本轮 contract 来自 @agent_prompt 元数据；只修正缺失、未变化或 forbidden 的评审文件 alias。"
    )


def build_task_turn_goal_from_prompt(resolved: ResolvedPromptSpec) -> TaskTurnGoal:
    return TaskTurnGoal(
        goal_id=resolved.spec.mode,
        outcomes={name: _outcome_goal(outcome) for name, outcome in resolved.spec.outcomes.items()},
        repair_prompt_builder=_build_spec_task_repair_prompt,
    )


def build_completion_turn_goal_from_prompt(resolved: ResolvedPromptSpec) -> CompletionTurnGoal:
    return CompletionTurnGoal(
        goal_id=resolved.spec.mode,
        outcomes={name: _outcome_goal(outcome) for name, outcome in resolved.spec.outcomes.items()},
        repair_prompt_builder=_build_spec_completion_repair_prompt,
    )


def build_prompt_text(prompt_fn: Callable[..., str], *args: Any, **kwargs: Any) -> tuple[str, ResolvedPromptSpec]:
    resolved = resolve_prompt_files(prompt_fn, *args, **kwargs)
    prompt_text = prompt_fn(*args, **kwargs)
    if not isinstance(prompt_text, str) or not prompt_text.strip():
        raise RuntimeError(f"{getattr(prompt_fn, '__name__', 'prompt')} did not return prompt text")
    if resolved.spec.prompt_appendix:
        prompt_text = prompt_text.rstrip() + "\n" + render_prompt_contract_appendix(resolved)
    return prompt_text, resolved


def build_prompt_task_turn(
    prompt_fn: Callable[..., str],
    *args: Any,
    stage_name: str = "",
    turn_status_path: str | Path | None = None,
    stage_status_path: str | Path | None = None,
    **kwargs: Any,
) -> PromptTurnBuild:
    prompt, resolved = build_prompt_text(prompt_fn, *args, **kwargs)
    contract = build_task_result_contract_from_prompt(
        resolved,
        turn_status_path=turn_status_path,
        stage_status_path=stage_status_path,
        stage_name=stage_name,
    )
    return PromptTurnBuild(
        prompt=prompt,
        resolved=resolved,
        task_result_contract=contract,
        task_turn_goal=build_task_turn_goal_from_prompt(resolved),
    )


def build_prompt_completion_turn(
    prompt_fn: Callable[..., str],
    *args: Any,
    status_path: str | Path,
    validator: Callable[[Path], Any],
    kind: str = "",
    quiet_window_sec: float = 1.0,
    **kwargs: Any,
) -> PromptTurnBuild:
    prompt, resolved = build_prompt_text(prompt_fn, *args, **kwargs)
    contract = build_completion_contract_from_prompt(
        resolved,
        status_path=status_path,
        validator=validator,
        kind=kind,
        quiet_window_sec=quiet_window_sec,
    )
    return PromptTurnBuild(
        prompt=prompt,
        resolved=resolved,
        completion_contract=contract,
        completion_turn_goal=build_completion_turn_goal_from_prompt(resolved),
    )


def _parse_result_payload(clean_output: str) -> dict[str, object]:
    payload = json.loads(clean_output)
    if not isinstance(payload, dict):
        raise RuntimeError("prompt turn result must be a JSON object")
    return payload


def _has_reviewer_outcomes(spec: Any) -> bool:
    statuses = {outcome.status for outcome in spec.outcomes.values()}
    return bool(statuses.intersection({"review_pass", "review_fail"}))


def run_prompt_turn(
    *,
    worker: TmuxBatchWorker,
    prompt_fn: Callable[..., str],
    args: tuple[Any, ...] = (),
    kwargs: dict[str, Any] | None = None,
    selected_intent: str = "",
    label: str = "",
    timeout_sec: float = DEFAULT_COMMAND_TIMEOUT_SEC,
    stage_label: str = "",
    role_label: str = "",
    task_name: str = "",
    requirement_name: str = "",
) -> dict[str, object]:
    call_kwargs = dict(kwargs or {})
    spec = get_prompt_spec(prompt_fn)
    if spec is None:
        raise ValueError(f"{getattr(prompt_fn, '__name__', 'prompt')} is missing @agent_prompt metadata")
    if selected_intent and spec.intent != selected_intent:
        raise ValueError(f"{spec.prompt_id} intent mismatch: expected {selected_intent}, got {spec.intent}")
    if _has_reviewer_outcomes(spec):
        raise ValueError(f"{spec.prompt_id} is a reviewer prompt; use run_prompt_completion_turn with an explicit validator")
    built = build_prompt_task_turn(prompt_fn, *args, stage_name=stage_label, **call_kwargs)
    if built.task_result_contract is None or built.task_turn_goal is None:
        raise AssertionError("prompt task turn builder did not create a task result contract")
    return run_task_result_turn_with_repair(
        worker=worker,
        label=label or spec.mode,
        prompt=built.prompt,
        result_contract=built.task_result_contract,
        parse_result_payload=_parse_result_payload,
        turn_goal=built.task_turn_goal,
        timeout_sec=timeout_sec,
        stage_label=stage_label or spec.stage,
        role_label=role_label or spec.role,
        task_name=task_name,
        requirement_name=requirement_name,
    )


def run_prompt_completion_turn(
    *,
    worker: TmuxBatchWorker,
    prompt_fn: Callable[..., str],
    status_path: str | Path,
    validator: Callable[[Path], Any],
    args: tuple[Any, ...] = (),
    kwargs: dict[str, Any] | None = None,
    selected_intent: str = "",
    label: str = "",
    kind: str = "review_round",
    quiet_window_sec: float = 1.0,
    timeout_sec: float = DEFAULT_COMMAND_TIMEOUT_SEC,
    stage_label: str = "",
    role_label: str = "",
    task_name: str = "",
    requirement_name: str = "",
) -> None:
    call_kwargs = dict(kwargs or {})
    spec = get_prompt_spec(prompt_fn)
    if spec is None:
        raise ValueError(f"{getattr(prompt_fn, '__name__', 'prompt')} is missing @agent_prompt metadata")
    if selected_intent and spec.intent != selected_intent:
        raise ValueError(f"{spec.prompt_id} intent mismatch: expected {selected_intent}, got {spec.intent}")
    if not _has_reviewer_outcomes(spec):
        raise ValueError(f"{spec.prompt_id} is not a reviewer prompt; use run_prompt_turn for task result prompts")
    built = build_prompt_completion_turn(
        prompt_fn,
        *args,
        status_path=status_path,
        validator=validator,
        kind=kind,
        quiet_window_sec=quiet_window_sec,
        **call_kwargs,
    )
    if built.completion_contract is None or built.completion_turn_goal is None:
        raise AssertionError("prompt completion turn builder did not create a completion contract")
    run_completion_turn_with_repair(
        worker=worker,
        label=label or spec.mode,
        prompt=built.prompt,
        completion_contract=built.completion_contract,
        turn_goal=built.completion_turn_goal,
        timeout_sec=timeout_sec,
        stage_label=stage_label or spec.stage,
        role_label=role_label or spec.role,
        task_name=task_name,
        requirement_name=requirement_name,
    )


__all__ = [
    "PromptTurnBuild",
    "build_completion_contract_from_prompt",
    "build_completion_turn_goal_from_prompt",
    "build_prompt_completion_turn",
    "build_prompt_task_turn",
    "build_prompt_text",
    "build_task_result_contract_from_prompt",
    "build_task_turn_goal_from_prompt",
    "run_prompt_completion_turn",
    "run_prompt_turn",
]
