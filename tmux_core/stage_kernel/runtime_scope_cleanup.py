from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Literal, Sequence

from tmux_core.runtime.tmux_runtime import TmuxRuntimeController, worker_state_is_prelaunch_active


def _safe_read_worker_state(state_path: Path) -> dict[str, object]:
    if not state_path.exists() or not state_path.is_file():
        return {}
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except Exception:
        payload = {}
    return payload if isinstance(payload, dict) else {}


def _classify_scope_match(
    payload: dict[str, object],
    *,
    project_dir: str,
    requirement_name: str,
    workflow_action: str,
) -> str:
    payload_project = str(payload.get("project_dir", "") or "").strip()
    payload_requirement = str(payload.get("requirement_name", "") or "").strip()
    payload_action = str(payload.get("workflow_action", "") or "").strip()
    if payload_project and payload_project != project_dir:
        return "mismatch"
    if payload_requirement and payload_requirement != requirement_name:
        return "mismatch"
    if payload_action and payload_action != workflow_action:
        return "mismatch"
    if payload_project and payload_requirement and payload_action:
        return "match"
    return "unknown"


CleanupMode = Literal["stale_only", "all"]


def _session_exists(tmux_runtime: TmuxRuntimeController, session_name: str) -> bool | None:
    if not session_name:
        return False
    try:
        return bool(tmux_runtime.session_exists(session_name))
    except Exception:
        # A tmux control-plane failure is not evidence that the session vanished.
        return None


def _session_matches_worker_state(
    tmux_runtime: TmuxRuntimeController,
    *,
    session_name: str,
    payload: dict[str, object],
    state_path: Path,
) -> bool | None:
    """Confirm destructive ownership without treating probe failure as a mismatch."""
    if not session_name:
        return False
    resolver = getattr(tmux_runtime, "session_matches_worker_state", None)
    if not callable(resolver):
        return None
    try:
        return bool(resolver(session_name, payload, state_path))
    except Exception:
        return None


def _is_legacy_stale_worker(
    payload: dict[str, object],
    *,
    session_name: str,
    tmux_runtime: TmuxRuntimeController,
) -> bool:
    if worker_state_is_prelaunch_active(payload):
        return False
    agent_state = str(payload.get("agent_state", "") or "").strip().upper()
    if agent_state == "DEAD":
        return True
    turn_state = str(payload.get("turn_state", "") or "").strip().lower()
    if turn_state == "orphaned":
        return True
    session_exists = _session_exists(tmux_runtime, session_name) if session_name else None
    if session_exists is False:
        return True
    return False


def cleanup_runtime_dirs_by_scope(
    *,
    runtime_root: str | Path,
    project_dir: str | Path,
    requirement_name: str,
    workflow_action: str,
    preserve_runtime_dirs: Sequence[str | Path] = (),
    preserve_session_names: Sequence[str] = (),
    mode: CleanupMode = "stale_only",
) -> tuple[str, ...]:
    if mode not in {"stale_only", "all"}:
        raise ValueError(f"不支持的 runtime cleanup mode: {mode}")
    root = Path(runtime_root).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        return ()

    current_project_dir = str(Path(project_dir).expanduser().resolve())
    current_requirement = str(requirement_name or "").strip()
    current_action = str(workflow_action or "").strip()
    preserve_dirs = {
        Path(item).expanduser().resolve()
        for item in preserve_runtime_dirs
        if str(item).strip()
    }
    preserve_sessions = {
        str(item).strip()
        for item in preserve_session_names
        if str(item).strip()
    }
    tmux_runtime = TmuxRuntimeController()
    removed: list[str] = []

    state_paths = sorted(root.glob("**/worker.state.json"))
    for state_path in state_paths:
        try:
            relative_parts = state_path.relative_to(root).parts
        except ValueError:
            relative_parts = state_path.parts
        if "_locks" in relative_parts:
            continue
        resolved_worker_dir = state_path.parent.expanduser().resolve()
        if resolved_worker_dir in preserve_dirs:
            continue
        if resolved_worker_dir.name == "_locks":
            continue
        payload = _safe_read_worker_state(state_path)
        if mode == "stale_only" and worker_state_is_prelaunch_active(payload):
            continue
        session_name = str(payload.get("session_name", "") or "").strip()
        if session_name and session_name in preserve_sessions:
            continue

        scope_state = _classify_scope_match(
            payload,
            project_dir=current_project_dir,
            requirement_name=current_requirement,
            workflow_action=current_action,
        )
        should_remove = False
        if scope_state == "match":
            should_remove = mode == "all" or _is_legacy_stale_worker(
                payload,
                session_name=session_name,
                tmux_runtime=tmux_runtime,
            )
        # Unscoped legacy state cannot prove project + requirement + action
        # ownership.  Preserve it for explicit/manual cleanup.
        if not should_remove:
            continue

        if session_name and session_name not in preserve_sessions:
            identity_matches = _session_matches_worker_state(
                tmux_runtime,
                session_name=session_name,
                payload=payload,
                state_path=state_path,
            )
            if identity_matches is None:
                # Neither a failed identity probe nor a runtime without an
                # ownership resolver authorizes a destructive tmux mutation.
                continue
            if identity_matches:
                try:
                    tmux_runtime.kill_session(session_name, missing_ok=True)
                except Exception:
                    # Session deletion is a mutation.  If tmux cannot confirm its
                    # outcome, retain the runtime evidence for the next explicit
                    # cleanup instead of pretending the worker was removed.
                    continue
            # A definitive identity mismatch means that the name has been
            # reused by another worker.  Remove only this stale runtime record.
        shutil.rmtree(resolved_worker_dir, ignore_errors=True)
        removed.append(str(resolved_worker_dir))

    if root.exists() and root.is_dir():
        for candidate in sorted((path for path in root.glob("**/*") if path.is_dir()), key=lambda path: len(path.parts), reverse=True):
            try:
                candidate_relative_parts = candidate.relative_to(root).parts
            except ValueError:
                candidate_relative_parts = candidate.parts
            if candidate == root or "_locks" in candidate_relative_parts:
                continue
            try:
                if not any(candidate.iterdir()):
                    candidate.rmdir()
                    removed.append(str(candidate))
            except Exception:
                continue
        if not any(root.iterdir()):
            root.rmdir()
            removed.append(str(root))
    return tuple(removed)
