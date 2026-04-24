from __future__ import annotations

import datetime as dt
import errno
import fcntl
import hashlib
import json
import os
import re
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, TextIO


LOCK_ROOT_NAME = ".canopy_stage_locks"

_SAFE_NAME_RE = re.compile(r"[^0-9A-Za-z._-]+")
_LOCK_GUARD = threading.RLock()


@dataclass
class _HeldLockEntry:
    file_obj: TextIO
    owner_thread_id: int
    owner_thread_name: str
    ref_count: int = 1


_HELD_LOCKS: dict[str, _HeldLockEntry] = {}


def normalize_requirement_scope(project_dir: str | Path, requirement_name: str) -> tuple[Path, str]:
    project_root = Path(project_dir).expanduser().resolve()
    normalized_requirement = str(requirement_name or "").strip()
    if not normalized_requirement:
        raise RuntimeError("缺少 requirement_name，无法建立并发锁")
    return project_root, normalized_requirement


def build_requirement_lock_key(project_dir: str | Path, requirement_name: str) -> str:
    project_root, normalized_requirement = normalize_requirement_scope(project_dir, requirement_name)
    return f"{project_root}::{normalized_requirement}"


def _build_lock_path(project_root: Path, requirement_name: str) -> Path:
    key = f"{project_root}::{requirement_name}".encode("utf-8")
    digest = hashlib.sha1(key).hexdigest()[:12]  # noqa: S324
    safe_name = _SAFE_NAME_RE.sub("_", requirement_name).strip("._")[:48] or "requirement"
    lock_root = project_root / LOCK_ROOT_NAME
    lock_root.mkdir(parents=True, exist_ok=True)
    return lock_root / f"{safe_name}-{digest}.lock"


def _lock_owner_payload(*, key: str, action: str) -> dict[str, str]:
    return {
        "pid": str(os.getpid()),
        "thread_id": str(threading.get_ident()),
        "thread_name": threading.current_thread().name,
        "lock_key": key,
        "action": str(action or "").strip(),
        "acquired_at": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
    }


def _write_owner_payload(file_obj: TextIO, *, key: str, action: str) -> None:
    payload = _lock_owner_payload(key=key, action=action)
    try:
        file_obj.seek(0)
        file_obj.truncate(0)
        file_obj.write(json.dumps(payload, ensure_ascii=False))
        file_obj.flush()
        os.fsync(file_obj.fileno())
    except Exception:
        pass


def _read_owner_payload(lock_path: Path) -> str:
    try:
        payload = json.loads(lock_path.read_text(encoding="utf-8"))
    except Exception:
        return ""
    if not isinstance(payload, dict):
        return ""
    pid = str(payload.get("pid", "")).strip()
    thread_id = str(payload.get("thread_id", "")).strip()
    thread_name = str(payload.get("thread_name", "")).strip()
    action = str(payload.get("action", "")).strip()
    acquired_at = str(payload.get("acquired_at", "")).strip()
    details = [
        segment
        for segment in (
            f"pid={pid}" if pid else "",
            f"thread_id={thread_id}" if thread_id else "",
            f"thread_name={thread_name}" if thread_name else "",
            f"action={action}" if action else "",
            f"acquired_at={acquired_at}" if acquired_at else "",
        )
        if segment
    ]
    return ", ".join(details)


def _release_once(lock_key: str) -> None:
    entry_to_release: _HeldLockEntry | None = None
    with _LOCK_GUARD:
        entry = _HELD_LOCKS.get(lock_key)
        if entry is None:
            return
        entry.ref_count -= 1
        if entry.ref_count > 0:
            return
        entry_to_release = _HELD_LOCKS.pop(lock_key, None)
    if entry_to_release is None:
        return
    try:
        entry_to_release.file_obj.seek(0)
        entry_to_release.file_obj.truncate(0)
        entry_to_release.file_obj.flush()
    except Exception:
        pass
    try:
        fcntl.flock(entry_to_release.file_obj.fileno(), fcntl.LOCK_UN)
    except Exception:
        pass
    try:
        entry_to_release.file_obj.close()
    except Exception:
        pass


@contextmanager
def requirement_concurrency_lock(
    project_dir: str | Path,
    requirement_name: str,
    *,
    action: str = "",
) -> Iterator[str]:
    project_root, normalized_requirement = normalize_requirement_scope(project_dir, requirement_name)
    lock_key = f"{project_root}::{normalized_requirement}"
    owner_thread_id = threading.get_ident()
    owner_thread_name = threading.current_thread().name

    with _LOCK_GUARD:
        existing = _HELD_LOCKS.get(lock_key)
        if existing is not None:
            if existing.owner_thread_id == owner_thread_id:
                existing.ref_count += 1
                reentrant = True
            else:
                holder = f"pid={os.getpid()}, thread_id={existing.owner_thread_id}"
                if existing.owner_thread_name:
                    holder = f"{holder}, thread_name={existing.owner_thread_name}"
                raise RuntimeError(f"并发冲突：同项目同需求已有运行中任务（lock_key={lock_key}; holder={holder}）")
        else:
            reentrant = False

    if reentrant:
        try:
            yield lock_key
        finally:
            _release_once(lock_key)
        return

    lock_path = _build_lock_path(project_root, normalized_requirement)
    file_obj = lock_path.open("a+", encoding="utf-8")
    acquired = False
    try:
        try:
            fcntl.flock(file_obj.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except OSError as error:
            if error.errno in {errno.EACCES, errno.EAGAIN}:
                owner_hint = _read_owner_payload(lock_path)
                hint = f"lock_key={lock_key}"
                if owner_hint:
                    hint = f"{hint}; holder={owner_hint}"
                raise RuntimeError(f"并发冲突：同项目同需求已有运行中任务（{hint}）") from error
            raise

        _write_owner_payload(file_obj, key=lock_key, action=action)
        with _LOCK_GUARD:
            _HELD_LOCKS[lock_key] = _HeldLockEntry(
                file_obj=file_obj,
                owner_thread_id=owner_thread_id,
                owner_thread_name=owner_thread_name,
                ref_count=1,
            )
        try:
            yield lock_key
        finally:
            _release_once(lock_key)
    except Exception:
        if acquired:
            try:
                fcntl.flock(file_obj.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
        try:
            file_obj.close()
        except Exception:
            pass
        raise
