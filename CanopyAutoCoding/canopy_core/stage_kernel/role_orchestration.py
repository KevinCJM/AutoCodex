from __future__ import annotations

from typing import Callable, Sequence, TypeVar

from canopy_core.runtime.tmux_runtime import DEFAULT_COMMAND_TIMEOUT_SEC

TMain = TypeVar("TMain")
TReviewer = TypeVar("TReviewer")
TResult = TypeVar("TResult")


def _resolve_worker(owner: object | None):
    if owner is None:
        return None
    worker = getattr(owner, "worker", None)
    return worker if worker is not None else owner


def _state_name(worker: object) -> str:
    get_state = getattr(worker, "get_agent_state", None)
    if not callable(get_state):
        return "READY"
    state = get_state()
    return str(getattr(state, "value", state) or "").strip().upper() or "READY"


def _ensure_worker_ready(worker: object | None, *, role_label: str, timeout_sec: float) -> None:
    if worker is None:
        return
    ensure_ready = getattr(worker, "ensure_agent_ready", None)
    if not callable(ensure_ready):
        return
    if _state_name(worker) != "READY":
        ensure_ready(timeout_sec=timeout_sec)
    if _state_name(worker) != "READY":
        raise RuntimeError(f"{role_label} 未进入 READY 状态")


def ensure_main_ready(
    main_owner: object | None,
    reviewers: Sequence[object] = (),
    *,
    main_label: str = "主工作智能体",
    reviewer_label_getter: Callable[[object, int], str] | None = None,
    timeout_sec: float = DEFAULT_COMMAND_TIMEOUT_SEC,
) -> None:
    _ensure_worker_ready(_resolve_worker(main_owner), role_label=main_label, timeout_sec=timeout_sec)
    for index, reviewer in enumerate(reviewers, start=1):
        label = reviewer_label_getter(reviewer, index) if reviewer_label_getter is not None else f"审核智能体 {index}"
        _ensure_worker_ready(_resolve_worker(reviewer), role_label=label, timeout_sec=timeout_sec)


def ensure_reviewers_ready(
    main_owner: object | None,
    reviewers: Sequence[object],
    *,
    main_label: str = "主工作智能体",
    reviewer_label_getter: Callable[[object, int], str] | None = None,
    timeout_sec: float = DEFAULT_COMMAND_TIMEOUT_SEC,
) -> None:
    ensure_main_ready(
        main_owner,
        reviewers,
        main_label=main_label,
        reviewer_label_getter=reviewer_label_getter,
        timeout_sec=timeout_sec,
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
    )
    return updated_reviewers
