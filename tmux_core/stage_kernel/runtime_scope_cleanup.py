from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Sequence

from tmux_core.runtime.tmux_runtime import TmuxRuntimeController


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


def _session_exists(tmux_runtime: TmuxRuntimeController, session_name: str) -> bool:
    if not session_name:
        return False
    try:
        return bool(tmux_runtime.session_exists(session_name))
    except Exception:
        return False


def _is_legacy_stale_worker(
    payload: dict[str, object],
    *,
    session_name: str,
    tmux_runtime: TmuxRuntimeController,
) -> bool:
    agent_state = str(payload.get("agent_state", "") or "").strip().upper()
    if agent_state == "DEAD":
        return True
    if session_name and not _session_exists(tmux_runtime, session_name):
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
) -> tuple[str, ...]:
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
        session_name = str(payload.get("session_name", "") or "").strip()
        if session_name and session_name in preserve_sessions:
            continue

        scope_state = _classify_scope_match(
            payload,
            project_dir=current_project_dir,
            requirement_name=current_requirement,
            workflow_action=current_action,
        )
        should_remove = scope_state == "match"
        if scope_state == "unknown":
            should_remove = _is_legacy_stale_worker(
                payload,
                session_name=session_name,
                tmux_runtime=tmux_runtime,
            )
        if not should_remove:
            continue

        if session_name and session_name not in preserve_sessions:
            try:
                tmux_runtime.kill_session(session_name, missing_ok=True)
            except Exception:
                pass
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
