from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tmux_core.stage_kernel.runtime_scope_cleanup import cleanup_runtime_dirs_by_scope


class _FakeTmuxRuntime:
    existing_sessions: set[str] = set()
    unavailable_sessions: set[str] = set()
    unavailable_kills: set[str] = set()
    mismatched_sessions: set[str] = set()
    killed_sessions: list[str] = []

    def session_exists(self, session_name: str) -> bool:
        if session_name in self.unavailable_sessions:
            raise TimeoutError("tmux unavailable")
        return session_name in self.existing_sessions

    def kill_session(self, session_name: str, *, missing_ok: bool = True) -> str:
        _ = missing_ok
        if session_name in self.unavailable_kills:
            raise TimeoutError("tmux kill outcome unknown")
        self.killed_sessions.append(session_name)
        return session_name

    def session_matches_worker_state(self, session_name: str, state, state_path) -> bool:  # noqa: ANN001, ARG002
        if session_name in self.unavailable_sessions:
            raise TimeoutError("tmux unavailable")
        return session_name in self.existing_sessions and session_name not in self.mismatched_sessions


class RuntimeScopeCleanupTests(unittest.TestCase):
    def setUp(self) -> None:
        _FakeTmuxRuntime.existing_sessions = set()
        _FakeTmuxRuntime.unavailable_sessions = set()
        _FakeTmuxRuntime.unavailable_kills = set()
        _FakeTmuxRuntime.mismatched_sessions = set()
        _FakeTmuxRuntime.killed_sessions = []

    @staticmethod
    def _write_worker(root: Path, name: str, **extra: object) -> Path:
        worker_dir = root / name
        worker_dir.mkdir(parents=True, exist_ok=True)
        payload: dict[str, object] = {
            "session_name": name,
            "project_dir": str(root.parent.resolve()),
            "requirement_name": "需求A",
            "workflow_action": "stage.a06.start",
        }
        payload.update(extra)
        (worker_dir / "worker.state.json").write_text(
            json.dumps(payload, ensure_ascii=False),
            encoding="utf-8",
        )
        return worker_dir

    def _cleanup(self, runtime_root: Path, *, mode: str = "stale_only") -> tuple[str, ...]:
        with patch(
            "tmux_core.stage_kernel.runtime_scope_cleanup.TmuxRuntimeController",
            _FakeTmuxRuntime,
        ):
            return cleanup_runtime_dirs_by_scope(
                runtime_root=runtime_root,
                project_dir=runtime_root.parent,
                requirement_name="需求A",
                workflow_action="stage.a06.start",
                mode=mode,  # type: ignore[arg-type]
            )

    def test_scope_match_alone_does_not_remove_live_worker(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_root = Path(tmpdir) / ".task_split_runtime"
            worker_dir = self._write_worker(runtime_root, "live-worker", agent_state="READY")
            _FakeTmuxRuntime.existing_sessions = {"live-worker"}

            removed = self._cleanup(runtime_root)

            self.assertTrue(worker_dir.exists())
            self.assertEqual(removed, ())
            self.assertEqual(_FakeTmuxRuntime.killed_sessions, [])

    def test_stale_only_removes_orphaned_and_dead_workers(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_root = Path(tmpdir) / ".task_split_runtime"
            orphaned_dir = self._write_worker(runtime_root, "orphaned-worker", turn_state="orphaned")
            dead_dir = self._write_worker(runtime_root, "dead-worker", agent_state="DEAD")
            _FakeTmuxRuntime.existing_sessions = {"orphaned-worker"}

            removed = self._cleanup(runtime_root)

            self.assertFalse(orphaned_dir.exists())
            self.assertFalse(dead_dir.exists())
            self.assertIn(str(orphaned_dir.resolve()), removed)
            self.assertIn(str(dead_dir.resolve()), removed)

    def test_tmux_unavailable_does_not_make_worker_stale(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_root = Path(tmpdir) / ".task_split_runtime"
            worker_dir = self._write_worker(runtime_root, "unknown-worker", agent_state="READY")
            _FakeTmuxRuntime.unavailable_sessions = {"unknown-worker"}

            removed = self._cleanup(runtime_root)

            self.assertTrue(worker_dir.exists())
            self.assertEqual(removed, ())

    def test_all_removes_matching_live_worker(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_root = Path(tmpdir) / ".task_split_runtime"
            worker_dir = self._write_worker(runtime_root, "live-worker", agent_state="READY")
            _FakeTmuxRuntime.existing_sessions = {"live-worker"}

            removed = self._cleanup(runtime_root, mode="all")

            self.assertFalse(worker_dir.exists())
            self.assertIn(str(worker_dir.resolve()), removed)

    def test_all_removes_matching_prelaunch_worker(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_root = Path(tmpdir) / ".task_split_runtime"
            worker_dir = self._write_worker(
                runtime_root,
                "prelaunch-worker",
                status="starting",
                startup_phase="launching",
            )

            removed = self._cleanup(runtime_root, mode="all")

            self.assertFalse(worker_dir.exists())
            self.assertIn(str(worker_dir.resolve()), removed)

    def test_unknown_session_kill_outcome_preserves_runtime_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_root = Path(tmpdir) / ".task_split_runtime"
            worker_dir = self._write_worker(runtime_root, "orphaned-worker", turn_state="orphaned")
            _FakeTmuxRuntime.existing_sessions = {"orphaned-worker"}
            _FakeTmuxRuntime.unavailable_kills = {"orphaned-worker"}

            removed = self._cleanup(runtime_root)

            self.assertTrue(worker_dir.exists())
            self.assertNotIn(str(worker_dir.resolve()), removed)

    def test_reused_session_name_never_kills_foreign_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_root = Path(tmpdir) / ".task_split_runtime"
            worker_dir = self._write_worker(runtime_root, "shared-session", turn_state="orphaned")
            _FakeTmuxRuntime.existing_sessions = {"shared-session"}
            _FakeTmuxRuntime.mismatched_sessions = {"shared-session"}

            removed = self._cleanup(runtime_root)

            self.assertFalse(worker_dir.exists())
            self.assertIn(str(worker_dir.resolve()), removed)
            self.assertEqual(_FakeTmuxRuntime.killed_sessions, [])

    def test_unscoped_dead_worker_is_not_assumed_to_belong_to_current_requirement(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_root = Path(tmpdir) / ".task_split_runtime"
            worker_dir = runtime_root / "legacy-dead-worker"
            worker_dir.mkdir(parents=True)
            (worker_dir / "worker.state.json").write_text(
                json.dumps({"session_name": "legacy-dead-worker", "agent_state": "DEAD"}),
                encoding="utf-8",
            )

            removed = self._cleanup(runtime_root)

            self.assertTrue(worker_dir.exists())
            self.assertNotIn(str(worker_dir.resolve()), removed)


if __name__ == "__main__":
    unittest.main()
