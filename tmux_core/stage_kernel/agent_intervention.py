from __future__ import annotations

import inspect
from contextlib import nullcontext
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from T09_terminal_ops import message, prompt_select_option, terminal_ui_is_interactive
from tmux_core.runtime.tmux_runtime import (
    AgentInterventionRequired,
    AgentRuntimeInterventionRequired,
    AgentStartupInterventionRequired,
    try_resume_worker,
)

AGENT_INTERVENTION_RECHECK = "recheck_after_manual_intervention"
AGENT_INTERVENTION_RECREATE = "recreate_after_manual_intervention"
AGENT_INTERVENTION_WORKER_DEAD = "worker_dead_after_manual_intervention"


class AgentInterventionActionSelected(RuntimeError):
    """Propagate an already selected HITL action without opening another prompt."""

    def __init__(
        self,
        *,
        decision: str,
        recovery_kind: str,
        reason_text: str,
        attempts_used: int = 0,
        target_paths: Sequence[str | Path] = (),
    ) -> None:
        self.decision = str(decision or "").strip()
        self.recovery_kind = str(recovery_kind or "").strip()
        self.reason_text = str(reason_text or "").strip()
        self.attempts_used = max(0, int(attempts_used or 0))
        self.target_paths = _normalize_target_paths(target_paths)
        super().__init__(
            f"HITL action already selected: recovery_kind={self.recovery_kind} "
            f"decision={self.decision} reason={self.reason_text}"
        )


def _normalize_target_paths(target_paths: Sequence[str | Path]) -> tuple[str, ...]:
    normalized: list[str] = []
    seen: set[str] = set()
    for item in target_paths:
        if not str(item or "").strip():
            continue
        path_text = str(Path(item).expanduser().resolve())
        if path_text in seen:
            continue
        seen.add(path_text)
        normalized.append(path_text)
    return tuple(normalized)


def _session_name(worker: object | None) -> str:
    return str(getattr(worker, "session_name", "") or "").strip()


def _worker_state(worker: object | None) -> str:
    if worker is None:
        return "unknown"
    read_state = getattr(worker, "read_state", None)
    if callable(read_state):
        try:
            state = read_state()
        except Exception:
            state = {}
        if isinstance(state, Mapping):
            agent_state = str(state.get("agent_state", "") or "").strip()
            status = str(state.get("status", "") or "").strip()
            health_status = str(state.get("health_status", "") or "").strip()
            summary = "/".join(item for item in (status, agent_state, health_status) if item)
            if summary:
                return summary
    get_agent_state = getattr(worker, "get_agent_state", None)
    if callable(get_agent_state):
        try:
            state = get_agent_state()
            return str(getattr(state, "value", state) or "").strip() or "unknown"
        except Exception:
            return "unknown"
    return "unknown"


def _attach_command(worker: object | None) -> str:
    session = _session_name(worker)
    return f"tmux attach -t {session}" if session else ""


def _mark_awaiting_manual(worker: object | None, *, reason_text: str) -> None:
    marker = getattr(worker, "mark_awaiting_reconfiguration", None)
    if not callable(marker):
        return
    try:
        marker(reason_text=reason_text)
    except Exception:
        return


def render_worker_intervention_summary(
    *,
    stage_label: str,
    role_label: str,
    worker: object | None,
    reason_text: str,
    target_paths: Sequence[str | Path] = (),
) -> str:
    lines = [
        f"{stage_label or '当前阶段'} 需要人工介入",
        f"角色: {role_label or '智能体'}",
        f"状态: {_worker_state(worker)}",
    ]
    session = _session_name(worker)
    if session:
        lines.append(f"会话: {session}")
    attach = _attach_command(worker)
    if attach:
        lines.append(f"进入会话: {attach}")
    reason = str(reason_text or "").strip()
    if reason:
        lines.append(f"原因: {reason}")
    paths = list(_normalize_target_paths(target_paths))
    if paths:
        lines.append("需要检查的文件:")
        lines.extend(f"- {item}" for item in paths)
    return "\n".join(lines)


def request_worker_manual_intervention(
    *,
    stage_label: str,
    role_label: str,
    worker: object | None,
    reason_text: str,
    target_paths: Sequence[str | Path] = (),
    progress: object | None = None,
    allow_recreate: bool = False,
    allow_worker_dead: bool = True,
    noninteractive_default: str | None = None,
    recovery_kind: str = "agent_manual_intervention",
    mark_worker_state: bool = True,
) -> str:
    role_text = str(role_label or "").strip() or "智能体"
    stage_text = str(stage_label or "").strip() or "当前阶段"
    normalized_target_paths = _normalize_target_paths(target_paths)
    summary = render_worker_intervention_summary(
        stage_label=stage_text,
        role_label=role_text,
        worker=worker,
        reason_text=reason_text,
        target_paths=normalized_target_paths,
    )
    if mark_worker_state:
        _mark_awaiting_manual(worker, reason_text=summary)
    message(summary)
    set_phase = getattr(progress, "set_phase", None)
    if callable(set_phase):
        set_phase(f"{stage_text} / 等待人工介入 | {role_text}")
    suspended = getattr(progress, "suspended", None)
    context = suspended() if callable(suspended) else nullcontext()
    options: list[tuple[str, str]] = [
        (AGENT_INTERVENTION_RECHECK, "我已进入 tmux/修正文件，重新检查"),
    ]
    if allow_recreate:
        options.append((AGENT_INTERVENTION_RECREATE, "重新创建该智能体"))
    if allow_worker_dead:
        options.append((AGENT_INTERVENTION_WORKER_DEAD, "智能体已死亡或已关闭，按死亡处理"))
    option_values = {value for value, _ in options}
    if not terminal_ui_is_interactive():
        default_value = str(noninteractive_default or "").strip()
        if default_value and default_value in option_values:
            message(f"{stage_text} / {role_text}: 非交互环境，自动选择恢复动作: {default_value}")
            return default_value
        raise RuntimeError(f"需要人工介入但当前环境不可交互:\n{summary}")
    with context:
        return prompt_select_option(
            title=f"HITL: {role_text} 需要人工介入",
            options=tuple(options),
            default_value=AGENT_INTERVENTION_RECHECK,
            prompt_text="请选择恢复方式",
            is_hitl=True,
            extra_payload={
                "recovery_kind": str(recovery_kind or "").strip() or "agent_manual_intervention",
                "stage_label": stage_text,
                "role_label": role_text,
                "session_name": _session_name(worker),
                "worker_state": _worker_state(worker),
                "attach_command": _attach_command(worker),
                "target_paths": list(normalized_target_paths),
                "reason_text": str(reason_text or "").strip(),
            },
        )


def run_worker_turn_with_startup_recovery(
    worker: object,
    *,
    run_turn_kwargs: Mapping[str, object],
    stage_label: str,
    role_label: str,
    on_intervention: Callable[[AgentStartupInterventionRequired], None] | None = None,
) -> Any:
    """Keep startup HITL inside the current stage stack and reuse the same live worker."""
    effective_run_turn_kwargs = dict(run_turn_kwargs)
    run_turn = getattr(worker, "run_turn")
    try:
        run_turn_parameters = inspect.signature(run_turn).parameters
    except (TypeError, ValueError):
        run_turn_parameters = {}
    # Only pass the optional callback when the callable explicitly declares it.
    # A generic **kwargs wrapper may delegate to a legacy worker that rejects
    # the argument, turning a recoverable startup intervention into failure.
    if "runtime_intervention_handler" in run_turn_parameters:
        effective_run_turn_kwargs.setdefault(
            "runtime_intervention_handler",
            lambda current_worker, error: wait_for_worker_runtime_intervention(
                current_worker,
                error=error,
                stage_label=stage_label,
                role_label=role_label,
            ),
        )
    while True:
        try:
            return run_turn(**effective_run_turn_kwargs)
        except AgentStartupInterventionRequired as error:
            if on_intervention is not None:
                on_intervention(error)
            wait_for_worker_startup_intervention(
                worker,
                error=error,
                stage_label=stage_label,
                role_label=role_label,
            )


def wait_for_worker_runtime_intervention(
    worker: object,
    *,
    error: AgentRuntimeInterventionRequired,
    stage_label: str,
    role_label: str,
) -> None:
    current_reason = str(error)
    while True:
        decision = request_worker_manual_intervention(
            stage_label=stage_label or "智能体运行",
            role_label=role_label or _session_name(worker) or "智能体",
            worker=worker,
            reason_text=current_reason,
            allow_recreate=False,
            allow_worker_dead=True,
            recovery_kind="agent_runtime_intervention",
            mark_worker_state=False,
        )
        if decision == AGENT_INTERVENTION_WORKER_DEAD:
            raise RuntimeError(f"tmux pane died during runtime intervention: {current_reason}")
        if decision != AGENT_INTERVENTION_RECHECK:
            continue
        resolved = getattr(worker, "runtime_intervention_is_resolved", None)
        if callable(resolved) and bool(resolved(error.blocker_kind)):
            return
        current_reason = (
            "人工处理后智能体的交互页面仍然可见；"
            "请继续在原 tmux 会话完成回答、授权或拒绝。"
        )


def wait_for_worker_startup_intervention(
    worker: object,
    *,
    error: AgentInterventionRequired,
    stage_label: str,
    role_label: str,
) -> None:
    if isinstance(error, AgentRuntimeInterventionRequired):
        wait_for_worker_runtime_intervention(
            worker,
            error=error,
            stage_label=stage_label,
            role_label=role_label,
        )
        return
    current_reason = str(error)
    while True:
        decision = request_worker_manual_intervention(
            stage_label=stage_label or "智能体启动",
            role_label=role_label or _session_name(worker) or "智能体",
            worker=worker,
            reason_text=current_reason,
            allow_recreate=False,
            allow_worker_dead=True,
            recovery_kind="agent_startup_intervention",
            mark_worker_state=False,
        )
        if decision == AGENT_INTERVENTION_WORKER_DEAD:
            raise RuntimeError(f"tmux pane died during startup intervention: {current_reason}")
        if decision != AGENT_INTERVENTION_RECHECK:
            continue
        if try_resume_worker(worker, timeout_sec=60.0):
            return
        current_reason = (
            "人工处理后智能体仍未进入 READY；请继续在原 tmux 会话完成登录、协议或启动页面。"
        )


def request_file_noncompliance_intervention(
    *,
    stage_label: str,
    role_label: str,
    worker: object | None,
    reason_text: str,
    attempts_used: int,
    target_paths: Sequence[str | Path] = (),
    progress: object | None = None,
    allow_recreate: bool = False,
    noninteractive_default: str | None = None,
) -> str:
    reason = (
        f"指定文件连续 {attempts_used} 次修复后仍不符合要求。\n"
        f"{str(reason_text or '').strip()}"
    ).strip()
    return request_worker_manual_intervention(
        stage_label=stage_label,
        role_label=role_label,
        worker=worker,
        reason_text=reason,
        target_paths=target_paths,
        progress=progress,
        allow_recreate=allow_recreate,
        noninteractive_default=noninteractive_default,
        recovery_kind="file_noncompliance",
        mark_worker_state=False,
    )
