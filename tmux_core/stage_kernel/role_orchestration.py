from __future__ import annotations

from typing import Callable, Sequence, TypeVar

from tmux_core.runtime.tmux_runtime import DEFAULT_COMMAND_TIMEOUT_SEC

TMain = TypeVar("TMain")
TReviewer = TypeVar("TReviewer")
TResult = TypeVar("TResult")


def _resolve_worker(owner: object | None):
    if owner is None:
        return None
    worker = getattr(owner, "worker", None)
    return worker if worker is not None else owner


def _state_name(worker: object) -> str:
    refresh_health = getattr(worker, "refresh_health", None)
    if callable(refresh_health):
        try:
            snapshot = refresh_health(notify_on_change=False)
            state_name = str(getattr(snapshot, "agent_state", "") or "").strip().upper()
            if state_name:
                return state_name
        except Exception:
            pass
    observe = getattr(worker, "observe", None)
    get_state = getattr(worker, "get_agent_state", None)
    if callable(observe) and callable(get_state):
        try:
            observation = observe(tail_lines=120)
            state = get_state(observation)
            state_name = str(getattr(state, "value", state) or "").strip().upper()
            if state_name:
                return state_name
        except Exception:
            pass
    if not callable(get_state):
        return ""
    state = get_state()
    return str(getattr(state, "value", state) or "").strip().upper()


def _read_worker_state(worker: object) -> dict[str, object]:
    read_state = getattr(worker, "read_state", None)
    if callable(read_state):
        try:
            state = read_state()
            return state if isinstance(state, dict) else {}
        except Exception:
            return {}
    return {}


def _current_turn_completed(worker: object) -> bool:
    state = _read_worker_state(worker)
    status = str(state.get("status", getattr(worker, "status", "")) or "").strip().lower()
    result_status = str(state.get("result_status", getattr(worker, "result_status", "")) or "").strip().lower()
    runtime_status = str(
        state.get("current_task_runtime_status", getattr(worker, "current_task_runtime_status", "")) or ""
    ).strip().lower()
    return status == "succeeded" and result_status == "succeeded" and runtime_status == "done"


def _ensure_worker_ready(
    worker: object | None,
    *,
    role_label: str,
    timeout_sec: float,
    allow_completed_nonready: bool = False,
) -> None:
    if worker is None:
        return
    ensure_ready = getattr(worker, "ensure_agent_ready", None)
    if not callable(ensure_ready):
        return
    if allow_completed_nonready and _current_turn_completed(worker):
        return
    if _state_name(worker) != "READY":
        ensure_ready(timeout_sec=timeout_sec)
    if allow_completed_nonready and _current_turn_completed(worker):
        return
    if _state_name(worker) != "READY":
        raise RuntimeError(f"{role_label} 未进入 READY 状态")


def ensure_main_ready(
    main_owner: object | None,
    reviewers: Sequence[object] = (),
    *,
    main_label: str = "主工作智能体",
    reviewer_label_getter: Callable[[object, int], str] | None = None,
    timeout_sec: float = DEFAULT_COMMAND_TIMEOUT_SEC,
    allow_completed_nonready: bool = False,
) -> None:
    _ensure_worker_ready(
        _resolve_worker(main_owner),
        role_label=main_label,
        timeout_sec=timeout_sec,
        allow_completed_nonready=allow_completed_nonready,
    )
    for index, reviewer in enumerate(reviewers, start=1):
        label = reviewer_label_getter(reviewer, index) if reviewer_label_getter is not None else f"审核智能体 {index}"
        _ensure_worker_ready(
            _resolve_worker(reviewer),
            role_label=label,
            timeout_sec=timeout_sec,
            allow_completed_nonready=allow_completed_nonready,
        )


def ensure_reviewers_ready(
    main_owner: object | None,
    reviewers: Sequence[object],
    *,
    main_label: str = "主工作智能体",
    reviewer_label_getter: Callable[[object, int], str] | None = None,
    timeout_sec: float = DEFAULT_COMMAND_TIMEOUT_SEC,
    allow_completed_nonready: bool = False,
) -> None:
    ensure_main_ready(
        main_owner,
        reviewers,
        main_label=main_label,
        reviewer_label_getter=reviewer_label_getter,
        timeout_sec=timeout_sec,
        allow_completed_nonready=allow_completed_nonready,
    )


def run_main_phase(
    main_owner: TMain,
    *,
    reviewers: Sequence[object] = (),
    run_phase: Callable[[TMain], TResult],
    owner_getter: Callable[[TResult], object] | None = None,
    main_label: str = "主工作智能体",
    reviewer_label_getter: Callable[[object, int], str] | None = None,
    timeout_sec: float = DEFAULT_COMMAND_TIMEOUT_SEC,
) -> TResult:
    ensure_main_ready(
        main_owner,
        reviewers,
        main_label=main_label,
        reviewer_label_getter=reviewer_label_getter,
        timeout_sec=timeout_sec,
    )
    result = run_phase(main_owner)
    updated_owner = owner_getter(result) if owner_getter is not None else result
    ensure_main_ready(
        updated_owner,
        reviewers,
        main_label=main_label,
        reviewer_label_getter=reviewer_label_getter,
        timeout_sec=timeout_sec,
        allow_completed_nonready=True,
    )
    return result


def run_reviewer_phase(
    main_owner: object | None,
    reviewers: Sequence[TReviewer],
    *,
    run_phase: Callable[[Sequence[TReviewer]], list[TReviewer]],
    main_label: str = "主工作智能体",
    reviewer_label_getter: Callable[[object, int], str] | None = None,
    timeout_sec: float = DEFAULT_COMMAND_TIMEOUT_SEC,
) -> list[TReviewer]:
    ensure_reviewers_ready(
        main_owner,
        reviewers,
        main_label=main_label,
        reviewer_label_getter=reviewer_label_getter,
        timeout_sec=timeout_sec,
    )
    updated_reviewers = run_phase(reviewers)
    ensure_reviewers_ready(
        main_owner,
        updated_reviewers,
        main_label=main_label,
        reviewer_label_getter=reviewer_label_getter,
        timeout_sec=timeout_sec,
        allow_completed_nonready=True,
    )
    return updated_reviewers
