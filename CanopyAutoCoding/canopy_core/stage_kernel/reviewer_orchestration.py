from __future__ import annotations

import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable, Sequence, TypeVar


TReviewer = TypeVar("TReviewer")


def run_parallel_reviewer_round(
    reviewers: Sequence[TReviewer],
    *,
    key_func: Callable[[TReviewer], str],
    run_turn: Callable[[TReviewer], TReviewer | None],
    error_prefix: str,
) -> list[TReviewer]:
    reviewer_list = list(reviewers)
    if not reviewer_list:
        return reviewer_list
    reviewer_index = {key_func(item): index for index, item in enumerate(reviewer_list)}
    dropped_keys: set[str] = set()
    with ThreadPoolExecutor(max_workers=max(1, len(reviewer_list))) as executor:
        future_map = {
            executor.submit(run_turn, reviewer): key_func(reviewer)
            for reviewer in reviewer_list
        }
        errors: list[str] = []
        for future in as_completed(future_map):
            reviewer_key = future_map[future]
            try:
                result = future.result()
                if result is None:
                    dropped_keys.add(reviewer_key)
                    continue
                reviewer_list[reviewer_index[reviewer_key]] = result
            except Exception as error:  # noqa: BLE001
                errors.append(f"{reviewer_key}: {error}")
        if errors:
            raise RuntimeError(error_prefix + "\n" + "\n".join(errors))
    return [reviewer for reviewer in reviewer_list if key_func(reviewer) not in dropped_keys]


def repair_reviewer_round_outputs(
    reviewers: Sequence[TReviewer],
    *,
    key_func: Callable[[TReviewer], str],
    artifact_name_func: Callable[[TReviewer], str],
    check_job: Callable[[Sequence[str]], dict[str, str]],
    run_fix_turn: Callable[[TReviewer, str, int], TReviewer | None],
    max_attempts: int,
    error_prefix: str,
    final_error: str,
) -> list[TReviewer]:
    reviewer_list = list(reviewers)
    if not reviewer_list:
        return reviewer_list
    reviewer_index = {key_func(item): index for index, item in enumerate(reviewer_list)}
    for repair_attempt in range(1, max_attempts + 1):
        prompts = check_job([artifact_name_func(item) for item in reviewer_list])
        if not prompts:
            return reviewer_list
        with ThreadPoolExecutor(max_workers=max(1, len(prompts))) as executor:
            future_map = {}
            for reviewer in reviewer_list:
                fix_prompt = prompts.get(artifact_name_func(reviewer))
                if not fix_prompt:
                    continue
                future_map[executor.submit(run_fix_turn, reviewer, fix_prompt, repair_attempt)] = key_func(reviewer)
            errors: list[str] = []
            dropped_keys: set[str] = set()
            for future in as_completed(future_map):
                reviewer_key = future_map[future]
                try:
                    result = future.result()
                    if result is None:
                        dropped_keys.add(reviewer_key)
                        continue
                    reviewer_list[reviewer_index[reviewer_key]] = result
                except Exception as error:  # noqa: BLE001
                    errors.append(f"{reviewer_key}: {error}")
            if errors:
                raise RuntimeError(error_prefix + "\n" + "\n".join(errors))
            if dropped_keys:
                reviewer_list = [item for item in reviewer_list if key_func(item) not in dropped_keys]
                reviewer_index = {key_func(item): index for index, item in enumerate(reviewer_list)}
                if not reviewer_list:
                    return reviewer_list
    if check_job([artifact_name_func(item) for item in reviewer_list]):
        raise RuntimeError(final_error)
    return reviewer_list


def shutdown_stage_workers(
    ba_handoff,
    reviewers: Sequence,
    *,
    cleanup_runtime: bool,
    preserve_ba_worker: bool = False,
    preserve_reviewer_keys: Sequence[str] = (),
    runtime_root_filter: str | Path | None = None,
) -> tuple[str, ...]:
    removed: list[str] = []
    seen_runtime_dirs: set[Path] = set()
    runtime_roots: set[Path] = set()
    preserved_reviewer_keys = {
        str(item).strip()
        for item in preserve_reviewer_keys
        if str(item).strip()
    }
    runtime_root_constraint = (
        Path(runtime_root_filter).expanduser().resolve()
        if runtime_root_filter is not None
        else None
    )
    for reviewer in reviewers:
        reviewer_key = str(getattr(reviewer, "reviewer_name", "") or "").strip()
        if reviewer_key and reviewer_key in preserved_reviewer_keys:
            continue
        reviewer_runtime_dir = Path(reviewer.worker.runtime_dir).expanduser().resolve()
        reviewer_runtime_root = Path(reviewer.worker.runtime_root).expanduser().resolve()
        if runtime_root_constraint is None or reviewer_runtime_root == runtime_root_constraint:
            if cleanup_runtime:
                try:
                    reviewer.worker.request_kill()
                except Exception:
                    pass
            seen_runtime_dirs.add(reviewer_runtime_dir)
            runtime_roots.add(reviewer_runtime_root)
    if ba_handoff is not None and not preserve_ba_worker:
        ba_runtime_dir = Path(ba_handoff.worker.runtime_dir).expanduser().resolve()
        ba_runtime_root = Path(ba_handoff.worker.runtime_root).expanduser().resolve()
        if runtime_root_constraint is None or ba_runtime_root == runtime_root_constraint:
            if cleanup_runtime:
                try:
                    ba_handoff.worker.request_kill()
                except Exception:
                    pass
            seen_runtime_dirs.add(ba_runtime_dir)
            runtime_roots.add(ba_runtime_root)
    if not cleanup_runtime:
        return ()
    for runtime_dir in seen_runtime_dirs:
        if runtime_dir.exists():
            shutil.rmtree(runtime_dir, ignore_errors=True)
            removed.append(str(runtime_dir))
    for runtime_root in runtime_roots:
        if runtime_root.exists() and runtime_root.is_dir() and not any(runtime_root.iterdir()):
            runtime_root.rmdir()
            removed.append(str(runtime_root))
    return tuple(removed)
