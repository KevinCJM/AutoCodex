from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from tmux_core.stage_kernel import source_change_ledger as ledger_module
from tmux_core.stage_kernel.source_change_ledger import (
    SOURCE_CHANGE_LEDGER_SCHEMA,
    ensure_source_task_baseline,
    load_source_cumulative_changes,
    mark_source_change_scope_unknown,
    record_source_task_changes,
    source_change_ledger_exists,
)


class SourceChangeLedgerTests(unittest.TestCase):
    def test_public_mutators_serialize_same_ledger_read_modify_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            ledger = root / "source_change_ledger.json"
            workers = 8
            start = threading.Barrier(workers)
            tracker_lock = threading.Lock()
            active_reads = 0
            max_active_reads = 0
            original_read = ledger_module._read_ledger  # noqa: SLF001

            def delayed_read(path: Path) -> dict[str, object]:
                nonlocal active_reads, max_active_reads
                with tracker_lock:
                    active_reads += 1
                    max_active_reads = max(max_active_reads, active_reads)
                try:
                    time.sleep(0.02)
                    return original_read(path)
                finally:
                    with tracker_lock:
                        active_reads -= 1

            def write_scope(index: int) -> None:
                start.wait()
                mark_source_change_scope_unknown(
                    ledger,
                    project_dir=root,
                    scope_name=f"scope-{index}",
                    reason="concurrent_test",
                )

            with patch.object(ledger_module, "_read_ledger", side_effect=delayed_read):
                with ThreadPoolExecutor(max_workers=workers) as executor:
                    list(executor.map(write_scope, range(workers)))

            payload = json.loads(ledger.read_text(encoding="utf-8"))
            self.assertEqual(max_active_reads, 1)
            self.assertEqual(set(payload["tasks"]), {f"scope-{index}" for index in range(workers)})

    def test_legacy_graphify_ledger_is_read_once_and_new_writes_use_generic_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            legacy = root / "graphify_change_ledger.json"
            current = root / "source_change_ledger.json"
            legacy.write_text(
                json.dumps({
                    "schema": "tmux-graphify-change-ledger/1",
                    "tasks": {
                        "M1-T1": {
                            "task_name": "M1-T1",
                            "state": "recorded",
                            "baseline": {"src/a.py": "sha256:before"},
                            "added": [],
                            "modified": ["src/a.py"],
                            "deleted": [],
                        }
                    },
                }),
                encoding="utf-8",
            )

            loaded = load_source_cumulative_changes(current)
            self.assertTrue(source_change_ledger_exists(current))
            self.assertEqual(loaded.cumulative_changed_files, ("src/a.py",))
            with patch(
                "tmux_core.stage_kernel.source_change_ledger._capture_manifest",
                return_value={"src/a.py": "sha256:before"},
            ):
                ensure_source_task_baseline(current, project_dir=root, task_name="M2-T1")
            self.assertTrue(current.is_file())
            self.assertEqual(
                json.loads(current.read_text(encoding="utf-8"))["schema"],
                SOURCE_CHANGE_LEDGER_SCHEMA,
            )

    def test_manifest_excludes_codegraph_and_all_stage_runtime_trees(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            (root / "src").mkdir()
            (root / "src" / "service.py").write_text("VALUE = 1\n", encoding="utf-8")
            (root / ".custom_stage_runtime").mkdir()
            (root / ".custom_stage_runtime" / "generated.py").write_text("VALUE = 2\n", encoding="utf-8")
            (root / ".codegraph").mkdir()
            (root / ".codegraph" / "generated.py").write_text("VALUE = 3\n", encoding="utf-8")

            manifest = ledger_module._capture_manifest(root, None)  # noqa: SLF001
            self.assertEqual(tuple(manifest), ("src/service.py",))

    def test_same_runner_resume_reuses_existing_task_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            ledger = root / "source_change_ledger.json"
            with patch(
                "tmux_core.stage_kernel.source_change_ledger._capture_manifest",
                side_effect=[
                    {"src/a.py": "sha256:before"},
                    {"src/a.py": "sha256:after"},
                ],
            ) as capture:
                first = ensure_source_task_baseline(
                    ledger,
                    project_dir=root,
                    task_name="M1-T1",
                    stage_key="A07",
                    runner_id="runner-a07-1",
                )
                resumed = ensure_source_task_baseline(
                    ledger,
                    project_dir=root,
                    task_name="M1-T1",
                    stage_key="A07",
                    runner_id="runner-a07-1",
                )
                changes = record_source_task_changes(
                    ledger,
                    project_dir=root,
                    task_name="M1-T1",
                    stage_key="A07",
                    runner_id="runner-a07-1",
                )

            self.assertEqual(first.state, "baseline")
            self.assertEqual(resumed.state, "baseline")
            self.assertEqual(changes.modified, ("src/a.py",))
            self.assertEqual(capture.call_count, 2)
            payload = json.loads(ledger.read_text(encoding="utf-8"))
            self.assertEqual(payload["stage_generations"]["A07"], "runner-a07-1")
            self.assertEqual(payload["tasks"]["M1-T1"]["runner_id"], "runner-a07-1")

    def test_new_runner_rebuilds_same_task_baseline_without_old_delta(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            ledger = root / "source_change_ledger.json"
            with patch(
                "tmux_core.stage_kernel.source_change_ledger._capture_manifest",
                side_effect=[
                    {"src/old.py": "sha256:before"},
                    {"src/old.py": "sha256:after"},
                    {"src/old.py": "sha256:after"},
                    {
                        "src/old.py": "sha256:after",
                        "src/new.py": "sha256:new",
                    },
                ],
            ) as capture:
                ensure_source_task_baseline(
                    ledger,
                    project_dir=root,
                    task_name="M1-T1",
                    stage_key="A07",
                    runner_id="runner-a07-old",
                )
                old_changes = record_source_task_changes(
                    ledger,
                    project_dir=root,
                    task_name="M1-T1",
                    stage_key="A07",
                    runner_id="runner-a07-old",
                )
                new_baseline = ensure_source_task_baseline(
                    ledger,
                    project_dir=root,
                    task_name="M1-T1",
                    stage_key="A07",
                    runner_id="runner-a07-new",
                )
                new_changes = record_source_task_changes(
                    ledger,
                    project_dir=root,
                    task_name="M1-T1",
                    stage_key="A07",
                    runner_id="runner-a07-new",
                )

            self.assertEqual(old_changes.modified, ("src/old.py",))
            self.assertEqual(new_baseline.state, "baseline")
            self.assertEqual(new_changes.added, ("src/new.py",))
            self.assertEqual(new_changes.modified, ())
            self.assertEqual(new_changes.cumulative_changed_files, ("src/new.py",))
            self.assertEqual(capture.call_count, 4)
            payload = json.loads(ledger.read_text(encoding="utf-8"))
            self.assertEqual(payload["stage_generations"]["A07"], "runner-a07-new")
            self.assertEqual(payload["tasks"]["M1-T1"]["runner_id"], "runner-a07-new")

    def test_new_a08_runner_rebuilds_baseline_but_keeps_active_a07_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            ledger = root / "source_change_ledger.json"
            manifests = [
                {"src/a07.py": "sha256:before"},
                {"src/a07.py": "sha256:after"},
                {"src/a07.py": "sha256:after"},
                {
                    "src/a07.py": "sha256:after",
                    "src/old_a08.py": "sha256:old-run",
                },
                {
                    "src/a07.py": "sha256:after",
                    "src/old_a08.py": "sha256:old-run",
                },
                {
                    "src/a07.py": "sha256:after",
                    "src/old_a08.py": "sha256:old-run",
                    "src/new_a08.py": "sha256:new-run",
                },
            ]
            with patch(
                "tmux_core.stage_kernel.source_change_ledger._capture_manifest",
                side_effect=manifests,
            ):
                ensure_source_task_baseline(
                    ledger,
                    project_dir=root,
                    task_name="M1-T1",
                    stage_key="A07",
                    runner_id="runner-a07",
                )
                record_source_task_changes(
                    ledger,
                    project_dir=root,
                    task_name="M1-T1",
                    stage_key="A07",
                    runner_id="runner-a07",
                )
                ensure_source_task_baseline(
                    ledger,
                    project_dir=root,
                    task_name="__A08_overall_review__",
                    stage_key="A08",
                    runner_id="runner-a08-old",
                )
                record_source_task_changes(
                    ledger,
                    project_dir=root,
                    task_name="__A08_overall_review__",
                    stage_key="A08",
                    runner_id="runner-a08-old",
                )
                ensure_source_task_baseline(
                    ledger,
                    project_dir=root,
                    task_name="__A08_overall_review__",
                    stage_key="A08",
                    runner_id="runner-a08-new",
                )
                changes = record_source_task_changes(
                    ledger,
                    project_dir=root,
                    task_name="__A08_overall_review__",
                    stage_key="A08",
                    runner_id="runner-a08-new",
                )

            self.assertEqual(changes.added, ("src/new_a08.py",))
            self.assertEqual(
                changes.cumulative_changed_files,
                ("src/a07.py", "src/new_a08.py"),
            )
            payload = json.loads(ledger.read_text(encoding="utf-8"))
            self.assertEqual(payload["stage_generations"]["A07"], "runner-a07")
            self.assertEqual(payload["stage_generations"]["A08"], "runner-a08-new")
            self.assertEqual(
                payload["tasks"]["__A08_overall_review__"]["runner_id"],
                "runner-a08-new",
            )

    def test_task_delta_uses_pre_submit_manifest_and_accumulates_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            ledger = root / "runtime" / "source_change_ledger.json"
            manifests = [
                {
                    "src/preexisting_dirty.py": "sha256:dirty-before",
                    "src/modified.py": "sha256:before",
                    "src/deleted.py": "sha256:before",
                },
                {
                    "src/preexisting_dirty.py": "sha256:dirty-before",
                    "src/modified.py": "sha256:after",
                    "src/added.py": "sha256:after",
                },
            ]
            with patch(
                "tmux_core.stage_kernel.source_change_ledger._capture_manifest",
                side_effect=manifests,
            ):
                baseline = ensure_source_task_baseline(
                    ledger,
                    project_dir=root,
                    task_name="M1-T1",
                )
                changes = record_source_task_changes(
                    ledger,
                    project_dir=root,
                    task_name="M1-T1",
                )

            self.assertEqual(baseline.state, "baseline")
            self.assertEqual(changes.added, ("src/added.py",))
            self.assertEqual(changes.modified, ("src/modified.py",))
            self.assertEqual(changes.deleted, ("src/deleted.py",))
            self.assertNotIn("src/preexisting_dirty.py", changes.changed_files)
            self.assertEqual(changes.cumulative_changed_files, changes.changed_files)
            self.assertEqual(changes.cumulative_deleted_files, ("src/deleted.py",))
            payload = json.loads(ledger.read_text(encoding="utf-8"))
            self.assertEqual(payload["schema"], SOURCE_CHANGE_LEDGER_SCHEMA)

    def test_legacy_output_without_baseline_stays_unknown_and_is_not_guessed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            ledger = root / "source_change_ledger.json"
            with patch(
                "tmux_core.stage_kernel.source_change_ledger._capture_manifest"
            ) as capture:
                result = ensure_source_task_baseline(
                    ledger,
                    project_dir=root,
                    task_name="M2-T1",
                    legacy_output_present=True,
                )
                recorded = record_source_task_changes(
                    ledger,
                    project_dir=root,
                    task_name="M2-T1",
                )
            capture.assert_not_called()
            self.assertEqual(result.state, "unknown")
            self.assertEqual(recorded.state, "unknown")
            self.assertEqual(recorded.changed_files, ())
            self.assertIn("legacy_developer_output", recorded.reason)

    def test_unknown_prior_scope_survives_later_known_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            ledger = root / "source_change_ledger.json"
            mark_source_change_scope_unknown(
                ledger,
                project_dir=root,
                scope_name="__legacy_A07_scope__",
                reason="a07_change_ledger_missing",
            )
            with patch(
                "tmux_core.stage_kernel.source_change_ledger._capture_manifest",
                side_effect=[{"src/a.py": "sha256:1"}, {"src/a.py": "sha256:2"}],
            ):
                ensure_source_task_baseline(
                    ledger,
                    project_dir=root,
                    task_name="__A08_overall_review__",
                )
                record_source_task_changes(
                    ledger,
                    project_dir=root,
                    task_name="__A08_overall_review__",
                )
            cumulative = load_source_cumulative_changes(ledger)
            self.assertEqual(cumulative.state, "unknown")
            self.assertEqual(cumulative.cumulative_changed_files, ("src/a.py",))
            self.assertIn("__legacy_A07_scope__", cumulative.reason)


if __name__ == "__main__":
    unittest.main()
