from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

import T02_tmux_agents as runtime_module
from T02_tmux_agents import (
    AgentRunConfig,
    AgentRuntimeInterventionRequired,
    AgentRuntimeState,
    AgentStartupInterventionRequired,
    CommandResult,
    TaskResultContract,
    TmuxBackend,
    TmuxBatchWorker,
    TmuxControlUnavailable,
    TmuxMutationOutcomeUnknown,
    TmuxProbeStatus,
    TurnFileContract,
    TurnFileResult,
    TurnState,
    WorkerStatus,
    RuntimeShutdownRequested,
    allow_runtime_shutdown_cleanup,
    assess_worker_resume,
    clear_runtime_shutdown_request,
    cleanup_registered_tmux_workers,
    get_current_stage_runner_id,
    raise_if_runtime_shutdown_requested,
    request_runtime_shutdown,
    stage_runner_context,
)


class TmuxRuntimeResilienceTests(unittest.TestCase):
    def setUp(self) -> None:
        clear_runtime_shutdown_request()

    def test_cleanup_context_bypasses_shutdown_only_for_current_context(self):
        request_runtime_shutdown("unit-test")
        with self.assertRaises(RuntimeShutdownRequested):
            raise_if_runtime_shutdown_requested("outside cleanup")

        with allow_runtime_shutdown_cleanup():
            raise_if_runtime_shutdown_requested("inside cleanup")
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(raise_if_runtime_shutdown_requested, "runner thread")
                with self.assertRaises(RuntimeShutdownRequested):
                    future.result()

        with self.assertRaises(RuntimeShutdownRequested):
            raise_if_runtime_shutdown_requested("after cleanup")

    def test_registered_worker_cleanup_runs_after_shutdown_request(self):
        class FakeWorker:
            session_name = "owned-session"

            def session_exists(self):
                raise_if_runtime_shutdown_requested("cleanup session probe")
                return True

            def request_kill(self):
                raise_if_runtime_shutdown_requested("cleanup kill")
                return self.session_name

            def _log_event(self, *_args, **_kwargs):
                return None

        request_runtime_shutdown("unit-test")
        with mock.patch.object(runtime_module, "list_registered_tmux_workers", return_value=[FakeWorker()]):
            cleaned = cleanup_registered_tmux_workers(reason="unit-test")

        self.assertEqual(cleaned, ["owned-session"])

    def test_read_probe_recovers_and_publishes_control_state(self):
        backend = TmuxBackend()
        events: list[dict[str, object]] = []
        backend.add_control_state_listener(lambda payload: events.append(dict(payload)))
        timeout = subprocess.TimeoutExpired(["tmux", "list-panes"], 0.01)
        success = subprocess.CompletedProcess(["tmux", "list-panes"], 0, stdout="%1\n", stderr="")
        with mock.patch.object(backend, "run", side_effect=[timeout, timeout, success]), mock.patch.object(
            runtime_module.time, "sleep", return_value=None
        ):
            probe = backend.probe_target_exists("%1")

        self.assertEqual(probe.status, TmuxProbeStatus.PRESENT)
        self.assertEqual(probe.attempts, 3)
        self.assertEqual([event["tmux_control_status"] for event in events], ["unavailable", "available"])

    def test_probe_recovery_budget_is_local_when_another_probe_succeeds(self):
        class FakeClock:
            def __init__(self) -> None:
                self.value = 100.0

            def monotonic(self) -> float:
                return self.value

            def sleep(self, seconds: float) -> None:
                self.value += max(float(seconds), 0.0)

        backend = TmuxBackend()
        clock = FakeClock()
        slow_attempts = 0
        fast_attempts = 0

        def fake_run(*args, **kwargs):  # noqa: ANN003
            nonlocal slow_attempts, fast_attempts
            del kwargs
            if args[0] == "fast-probe":
                fast_attempts += 1
                return subprocess.CompletedProcess(["tmux", *args], 0, stdout="ok\n", stderr="")
            slow_attempts += 1
            if slow_attempts > 6:
                raise AssertionError("another probe success must not reset this probe's recovery deadline")
            clock.value += 1.0  # The first blocking tmux command is part of the outage.
            if slow_attempts > 1:
                backend._probe_readonly("fast-probe", "fast-probe")  # noqa: SLF001
            raise subprocess.TimeoutExpired(["tmux", *args], 1.0)

        with mock.patch.object(backend, "run", side_effect=fake_run), mock.patch.object(
            runtime_module.time, "monotonic", side_effect=clock.monotonic
        ), mock.patch.object(runtime_module.time, "sleep", side_effect=clock.sleep):
            with self.assertRaises(TmuxControlUnavailable) as raised:
                backend._probe_readonly(  # noqa: SLF001
                    "slow-probe",
                    "slow-probe",
                    recovery_timeout_sec=3.0,
                )
            frozen_business_time = backend.business_monotonic()

        self.assertGreaterEqual(raised.exception.elapsed_sec, 3.0)
        self.assertLess(raised.exception.elapsed_sec, 4.5)
        self.assertEqual(slow_attempts, 3)
        self.assertEqual(fast_attempts, 2)
        self.assertAlmostEqual(frozen_business_time, 100.0)
        self.assertEqual(backend.control_state()["tmux_control_status"], "unavailable")

    def test_read_probe_caps_each_command_timeout_to_remaining_recovery_budget(self):
        class FakeClock:
            def __init__(self) -> None:
                self.value = 100.0

            def monotonic(self) -> float:
                return self.value

            def sleep(self, seconds: float) -> None:
                self.value += max(float(seconds), 0.0)

        backend = TmuxBackend()
        clock = FakeClock()
        command_timeouts: list[float] = []

        def fake_run(*args, **kwargs):  # noqa: ANN003
            command_timeout = float(kwargs["timeout_sec"])
            command_timeouts.append(command_timeout)
            clock.value += 4.5 if len(command_timeouts) == 1 else command_timeout
            raise subprocess.TimeoutExpired(["tmux", *args], command_timeout)

        with mock.patch.object(backend, "run", side_effect=fake_run), mock.patch.object(
            runtime_module.time, "monotonic", side_effect=clock.monotonic
        ), mock.patch.object(runtime_module.time, "sleep", side_effect=clock.sleep):
            with self.assertRaises(TmuxControlUnavailable):
                backend._probe_readonly(  # noqa: SLF001
                    "bounded-probe",
                    "list-panes",
                    recovery_timeout_sec=5.0,
                    timeout_sec=10.0,
                )

        self.assertEqual(len(command_timeouts), 2)
        self.assertLessEqual(command_timeouts[0], 5.0)
        self.assertLessEqual(command_timeouts[1], 0.5)
        self.assertLessEqual(clock.value, 105.0)

    def test_retroactive_first_timeout_does_not_double_count_closed_outage(self):
        clock = mock.Mock()
        clock.value = 100.0
        clock.monotonic.side_effect = lambda: clock.value
        backend = TmuxBackend()

        def fake_run(*args, **kwargs):  # noqa: ANN003
            del kwargs
            if args[0] == "fast-probe":
                return subprocess.CompletedProcess(["tmux", *args], 0, stdout="ok\n", stderr="")
            clock.value = 103.0
            backend._probe_readonly("fast-probe", "fast-probe")  # noqa: SLF001
            clock.value = 105.0
            raise subprocess.TimeoutExpired(["tmux", *args], 4.0)

        with mock.patch.object(runtime_module.time, "monotonic", side_effect=clock.monotonic):
            backend._mark_control_unavailable("prior outage")  # noqa: SLF001
            clock.value = 101.0
            with mock.patch.object(backend, "run", side_effect=fake_run):
                with self.assertRaises(TmuxControlUnavailable):
                    backend._probe_readonly(  # noqa: SLF001
                        "slow-probe",
                        "slow-probe",
                        recovery_timeout_sec=0.0,
                    )
            frozen_business_time = backend.business_monotonic()

        # 100-103 was already closed by the fast probe; the slow probe may
        # retroactively add only 103-105, not its overlapping 101-105 interval.
        self.assertAlmostEqual(backend._control_unavailable_total_sec, 3.0)  # noqa: SLF001
        self.assertAlmostEqual(frozen_business_time, 100.0)

    def test_single_missing_is_not_dead_and_two_missing_are_definitive(self):
        backend = TmuxBackend()
        missing = subprocess.CompletedProcess(
            ["tmux", "list-panes"], 1, stdout="", stderr="can't find pane: %1"
        )
        present = subprocess.CompletedProcess(["tmux", "list-panes"], 0, stdout="%1\n", stderr="")
        with mock.patch.object(backend, "run", side_effect=[missing, present]), mock.patch.object(
            runtime_module.time, "sleep", return_value=None
        ):
            self.assertEqual(backend.probe_target_exists("%1").status, TmuxProbeStatus.PRESENT)
        with mock.patch.object(backend, "run", side_effect=[missing, missing]), mock.patch.object(
            runtime_module.time, "sleep", return_value=None
        ):
            self.assertEqual(backend.probe_target_exists("%1").status, TmuxProbeStatus.MISSING)

    def test_session_missing_requires_pane_cross_confirmation_before_dead(self):
        class CrossConfirmWorker(TmuxBatchWorker):
            session_present = False
            pane_present = True
            pane_is_dead = False
            target_error: Exception | None = None
            pane_belongs_to_session = True
            pane_identity_matches = True

            def session_exists(self) -> bool:
                return self.session_present

            def target_exists(self, target=None):  # noqa: ANN001, ARG002
                if self.target_error is not None:
                    raise self.target_error
                return self.pane_present

            def pane_dead(self) -> bool:
                return self.pane_is_dead

            def pane_current_command(self) -> str:
                return "codex"

            def pane_current_path(self) -> str:
                return str(self.work_dir)

            def pane_title(self) -> str:
                return "TmuxCodingTeam"

            def _pane_belongs_to_expected_session(self) -> bool:
                return self.pane_belongs_to_session

            def _pane_matches_worker_identity(self) -> bool:
                return self.pane_belongs_to_session and self.pane_identity_matches

        with tempfile.TemporaryDirectory() as tmp_dir, mock.patch.object(
            TmuxBackend, "list_sessions", return_value=[]
        ):
            worker = CrossConfirmWorker(
                worker_id="cross-confirm-worker",
                work_dir=tmp_dir,
                config=AgentRunConfig(vendor="codex", model="gpt-5.4-mini"),
                runtime_root=Path(tmp_dir) / "runtime",
            )
            worker.pane_id = "%1"

            observation = worker._capture_lightweight_observation()  # noqa: SLF001
            self.assertTrue(observation.session_exists)
            self.assertFalse(observation.pane_dead)

            worker.pane_identity_matches = False
            identity_mismatch = worker._capture_lightweight_observation()  # noqa: SLF001
            self.assertFalse(identity_mismatch.session_exists)

            worker.pane_identity_matches = True

            worker.pane_is_dead = True
            pane_dead = worker._capture_lightweight_observation()  # noqa: SLF001
            self.assertTrue(pane_dead.session_exists)
            self.assertTrue(pane_dead.pane_dead)

            worker.pane_is_dead = False
            worker.pane_present = False
            missing = worker._capture_lightweight_observation()  # noqa: SLF001
            self.assertFalse(missing.session_exists)

            worker.target_error = TmuxControlUnavailable(
                operation="list-panes",
                error="timeout",
                elapsed_sec=60.0,
                attempts=4,
            )
            with self.assertRaises(TmuxControlUnavailable):
                worker._capture_lightweight_observation()  # noqa: SLF001

    def test_persistent_control_failure_raises_typed_error(self):
        backend = TmuxBackend()
        timeout = subprocess.TimeoutExpired(["tmux", "has-session"], 0.01)
        with mock.patch.object(backend, "run", side_effect=timeout):
            with self.assertRaises(TmuxControlUnavailable) as raised:
                backend._probe_readonly(  # noqa: SLF001
                    "has-session",
                    "has-session",
                    "-t",
                    "demo",
                    recovery_timeout_sec=0.0,
                )
        self.assertIn("has-session", str(raised.exception))
        self.assertEqual(backend.control_state()["tmux_control_status"], "unavailable")

    def test_unknown_nonzero_read_failure_is_unavailable_not_missing(self):
        backend = TmuxBackend()
        unknown = subprocess.CompletedProcess(
            ["tmux", "list-panes"], 2, stdout="", stderr="protocol error from tmux server"
        )
        with mock.patch.object(backend, "run", return_value=unknown):
            with self.assertRaises(TmuxControlUnavailable):
                backend._probe_readonly(  # noqa: SLF001
                    "list-panes",
                    "list-panes",
                    "-t",
                    "%1",
                    recovery_timeout_sec=0.0,
                    confirm_missing=True,
                )

    def test_load_buffer_retries_same_buffer_but_paste_timeout_is_unknown(self):
        backend = TmuxBackend()
        timeout = subprocess.TimeoutExpired(["tmux", "load-buffer"], 0.01)
        ok = subprocess.CompletedProcess(["tmux"], 0, stdout="", stderr="")
        calls: list[tuple[str, ...]] = []

        def fake_run(*args, **kwargs):  # noqa: ANN003
            calls.append(tuple(str(arg) for arg in args))
            if len(calls) == 1:
                raise timeout
            if args[0] == "paste-buffer":
                raise subprocess.TimeoutExpired(["tmux", "paste-buffer"], 0.01)
            return ok

        with mock.patch.object(backend, "run", side_effect=fake_run), mock.patch("subprocess.run"):
            with self.assertRaises(TmuxMutationOutcomeUnknown):
                backend.send_text("%1", "hello", submit_count=1)
        self.assertEqual(calls[0][0], "load-buffer")
        self.assertEqual(calls[1][0], "load-buffer")
        self.assertEqual(calls[0][2], calls[1][2])
        self.assertEqual(sum(call[0] == "paste-buffer" for call in calls), 1)

    def test_delete_buffer_cleanup_is_bounded_and_cannot_replace_submission_error(self):
        backend = TmuxBackend()
        paste_timeout = subprocess.TimeoutExpired(["tmux", "paste-buffer"], 0.01)
        cleanup_timeout = subprocess.TimeoutExpired(["tmux", "delete-buffer"], 2.0)

        def fake_run(*args, **kwargs):  # noqa: ANN003
            if args[0] == "paste-buffer":
                raise paste_timeout
            return subprocess.CompletedProcess(["tmux", *args], 0, stdout="", stderr="")

        with mock.patch.object(backend, "run", side_effect=fake_run), mock.patch(
            "subprocess.run",
            side_effect=cleanup_timeout,
        ) as cleanup:
            with self.assertRaises(TmuxMutationOutcomeUnknown) as raised:
                backend.send_text("%1", "hello", submit_count=1)

        self.assertEqual(raised.exception.operation, "paste-buffer")
        cleanup.assert_called_once()
        self.assertEqual(cleanup.call_args.kwargs["timeout"], runtime_module.TMUX_DELETE_BUFFER_TIMEOUT_SEC)

    def test_capture_pane_uses_short_recovery_budget(self):
        backend = TmuxBackend()
        probe_result = runtime_module.TmuxProbeResult(
            status=TmuxProbeStatus.PRESENT,
            value="visible pane",
        )
        with mock.patch.object(backend, "_probe_readonly", return_value=probe_result) as probe:  # noqa: SLF001
            visible = backend.capture_visible("%1", tail_lines=120)

        self.assertEqual(visible, "visible pane")
        self.assertEqual(
            probe.call_args.kwargs["recovery_timeout_sec"],
            runtime_module.TMUX_CAPTURE_RECOVERY_TIMEOUT_SEC,
        )
        self.assertLess(
            probe.call_args.kwargs["recovery_timeout_sec"],
            runtime_module.TMUX_CONTROL_RECOVERY_TIMEOUT_SEC,
        )

    def test_business_deadline_preserves_remaining_budget_after_control_pause(self):
        backend = TmuxBackend()
        with tempfile.TemporaryDirectory() as tmp_dir:
            worker = TmuxBatchWorker(
                worker_id="deadline-worker",
                work_dir=tmp_dir,
                config=AgentRunConfig(vendor="codex", model="gpt-5.4-mini"),
                runtime_root=Path(tmp_dir) / "runtime",
                backend=backend,
            )
            with mock.patch.object(runtime_module.time, "monotonic", side_effect=[100.0, 105.5]):
                deadline = worker._business_monotonic() + 1.0  # noqa: SLF001
                backend._control_unavailable_total_sec = 5.0  # noqa: SLF001
                remaining = deadline - worker._business_monotonic()  # noqa: SLF001
        self.assertAlmostEqual(remaining, 0.5)

    def test_active_global_outage_freezes_business_clock_and_is_counted_once(self):
        backend = TmuxBackend()
        with mock.patch.object(runtime_module.time, "monotonic", return_value=100.0):
            backend._mark_control_unavailable("timeout")  # noqa: SLF001

        with mock.patch.object(runtime_module.time, "monotonic", return_value=105.0):
            self.assertEqual(backend.business_monotonic(), 100.0)
            self.assertEqual(backend.control_unavailable_total_sec, 5.0)

        with mock.patch.object(runtime_module.time, "monotonic", return_value=110.0):
            with ThreadPoolExecutor(max_workers=8) as pool:
                recovery_durations = list(
                    pool.map(lambda _: backend._mark_control_available(), range(8))  # noqa: SLF001
                )

        self.assertEqual(sum(recovery_durations), 10.0)
        self.assertEqual(backend.control_unavailable_total_sec, 10.0)
        self.assertEqual(backend.control_state()["tmux_control_status"], "available")

    def test_task_done_cannot_override_pane_dead_health(self):
        with tempfile.TemporaryDirectory() as tmp_dir, mock.patch.object(
            TmuxBackend, "list_sessions", return_value=[]
        ):
            worker = TmuxBatchWorker(
                worker_id="done-but-dead-worker",
                work_dir=tmp_dir,
                config=AgentRunConfig(vendor="codex", model="gpt-5.4-mini"),
                runtime_root=Path(tmp_dir) / "runtime",
            )
            worker.pane_id = "%1"
            worker.agent_started = True
            worker.agent_state = AgentRuntimeState.READY
            worker.current_task_runtime_status = "done"
            snapshot = worker._build_passive_health_snapshot(  # noqa: SLF001
                runtime_module.WorkerObservation(
                    visible_text="ready",
                    raw_log_delta="",
                    raw_log_tail="ready",
                    current_command="codex",
                    current_path=tmp_dir,
                    pane_dead=True,
                    session_exists=True,
                    log_mtime=0.0,
                    observed_at="2026-07-14T00:00:00",
                    pane_title="TmuxCodingTeam",
                )
            )

        self.assertEqual(snapshot.agent_state, AgentRuntimeState.DEAD.value)
        self.assertEqual(snapshot.health_status, "pane_dead")

    def test_task_done_does_not_override_observed_busy_health(self):
        with tempfile.TemporaryDirectory() as tmp_dir, mock.patch.object(
            TmuxBackend, "list_sessions", return_value=[]
        ):
            worker = TmuxBatchWorker(
                worker_id="done-but-busy-worker",
                work_dir=tmp_dir,
                config=AgentRunConfig(vendor="codex", model="gpt-5.4-mini"),
                runtime_root=Path(tmp_dir) / "runtime",
            )
            worker.pane_id = "%1"
            worker.agent_started = True
            worker.agent_state = AgentRuntimeState.BUSY
            worker.current_task_runtime_status = "done"
            snapshot = worker._build_passive_health_snapshot(  # noqa: SLF001
                runtime_module.WorkerObservation(
                    visible_text="Working",
                    raw_log_delta="",
                    raw_log_tail="",
                    current_command="codex",
                    current_path=tmp_dir,
                    pane_dead=False,
                    session_exists=True,
                    log_mtime=0.0,
                    observed_at="2026-07-15T15:00:00",
                    pane_title="⠋ TmuxCodingTeam",
                )
            )

        self.assertEqual(snapshot.agent_state, AgentRuntimeState.BUSY.value)
        self.assertEqual(worker.current_task_runtime_status, "done")

    def test_completed_turn_health_refresh_persists_current_busy_state_and_revision(self):
        with tempfile.TemporaryDirectory() as tmp_dir, mock.patch.object(
            TmuxBackend, "list_sessions", return_value=[]
        ):
            worker = TmuxBatchWorker(
                worker_id="completed-refresh-busy-worker",
                work_dir=tmp_dir,
                config=AgentRunConfig(vendor="codex", model="gpt-5.4-mini"),
                runtime_root=Path(tmp_dir) / "runtime",
            )
            worker.pane_id = "%1"
            worker.agent_started = True
            worker.agent_state = AgentRuntimeState.READY
            worker.current_task_runtime_status = "done"
            worker.state_path.write_text(
                json.dumps(
                    {
                        "worker_id": worker.worker_id,
                        "session_name": worker.session_name,
                        "pane_id": worker.pane_id,
                        "work_dir": str(worker.work_dir),
                        "status": "succeeded",
                        "result_status": "succeeded",
                        "turn_state": TurnState.SUCCEEDED.value,
                        "current_task_runtime_status": "done",
                        "agent_state": AgentRuntimeState.READY.value,
                        "agent_started": True,
                        "agent_alive": True,
                        "health_status": "alive",
                        "state_revision": 3,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            observation = runtime_module.WorkerObservation(
                visible_text="esc interrupt",
                raw_log_delta="",
                raw_log_tail="",
                current_command="codex",
                current_path=tmp_dir,
                pane_dead=False,
                session_exists=True,
                log_mtime=0.0,
                observed_at="2026-07-15T15:00:01",
                pane_title="⠋ TmuxCodingTeam",
            )
            busy_snapshot = runtime_module.WorkerHealthSnapshot(
                session_exists=True,
                health_status="alive",
                health_note="alive",
                last_heartbeat_at=observation.observed_at,
                last_log_offset=0,
                current_command="codex",
                current_path=tmp_dir,
                pane_id=worker.pane_id,
                session_name=worker.session_name,
                agent_state=AgentRuntimeState.BUSY.value,
                pane_title=observation.pane_title,
            )
            with mock.patch.object(worker, "_capture_passive_observation", return_value=observation), mock.patch.object(
                worker,
                "_build_passive_health_snapshot",
                return_value=busy_snapshot,
            ), mock.patch.object(worker, "is_agent_alive", return_value=True):
                refreshed = worker._refresh_health_state_nonintrusive(notify_on_change=False)  # noqa: SLF001
            state = worker.read_state()

        self.assertEqual(refreshed.agent_state, AgentRuntimeState.BUSY.value)
        self.assertEqual(state["agent_state"], AgentRuntimeState.BUSY.value)
        self.assertFalse(state["agent_ready"])
        self.assertEqual(state["status"], "succeeded")
        self.assertEqual(state["turn_state"], TurnState.SUCCEEDED.value)
        self.assertEqual(state["current_task_runtime_status"], "done")
        self.assertEqual(state["state_revision"], 4)

    def test_stage_runner_context_owns_new_and_reused_worker(self):
        with tempfile.TemporaryDirectory() as tmp_dir, mock.patch.object(
            TmuxBackend, "list_sessions", return_value=[]
        ):
            root = Path(tmp_dir)
            with stage_runner_context("runner-current"):
                self.assertEqual(get_current_stage_runner_id(), "runner-current")
                worker = TmuxBatchWorker(
                    worker_id="context-owned-worker",
                    work_dir=root,
                    config=AgentRunConfig(vendor="codex", model="gpt-5.4-mini"),
                    runtime_root=root / "runtime",
                )
            self.assertEqual(get_current_stage_runner_id(), "")
            self.assertEqual(worker.stage_runner_id, "runner-current")
            self.assertEqual(worker.runtime_metadata()["stage_runner_id"], "runner-current")

            reused_dir = root / "runtime" / "reused-old"
            reused_dir.mkdir(parents=True)
            (reused_dir / "worker.state.json").write_text(
                json.dumps(
                    {
                        "session_name": "old-session",
                        "pane_id": "%9",
                        "stage_runner_id": "runner-old",
                        "turn_state": TurnState.ORPHANED.value,
                        "orphaned_at": "2026-07-14T00:00:00",
                        "orphaned_reason": "runner-old failed",
                    }
                ),
                encoding="utf-8",
            )
            with stage_runner_context("runner-new"):
                reused = TmuxBatchWorker(
                    worker_id="context-reused-worker",
                    work_dir=root,
                    config=AgentRunConfig(vendor="codex", model="gpt-5.4-mini"),
                    runtime_root=root / "runtime",
                    existing_runtime_dir=reused_dir,
                    existing_session_name="old-session",
                    existing_pane_id="%9",
                )
            self.assertEqual(reused.stage_runner_id, "runner-new")
            self.assertEqual(reused.runtime_metadata()["stage_runner_id"], "runner-new")
            self.assertEqual(reused.turn_state, TurnState.IDLE)
            reused.turn_state = TurnState.PREPARING
            reused._write_state(  # noqa: SLF001
                WorkerStatus.RUNNING,
                note="turn:new-runner",
                extra={
                    "turn_state": TurnState.PREPARING.value,
                    "agent_alive": True,
                    "agent_state": AgentRuntimeState.STARTING.value,
                },
            )
            reused_state = reused.read_state()
            self.assertEqual(reused_state["turn_state"], TurnState.PREPARING.value)
            self.assertEqual(reused_state["orphaned_at"], "")
            self.assertEqual(reused_state["orphaned_reason"], "")

    def test_set_runtime_metadata_refreshes_runner_from_current_context(self):
        with tempfile.TemporaryDirectory() as tmp_dir, mock.patch.object(
            TmuxBackend, "list_sessions", return_value=[]
        ):
            worker = TmuxBatchWorker(
                worker_id="redirected-runner-worker",
                work_dir=tmp_dir,
                config=AgentRunConfig(vendor="codex", model="gpt-5.4-mini"),
                runtime_root=Path(tmp_dir) / "runtime",
                runtime_metadata={"stage_runner_id": "runner-old"},
            )
            worker._write_session_created_state_fast()  # noqa: SLF001
            orphaned = worker.read_state()
            orphaned.update(
                {
                    "turn_state": TurnState.ORPHANED.value,
                    "orphaned_at": "2026-07-14T00:00:00",
                    "orphaned_reason": "runner-old failed",
                    "orphaned_stage_action": "stage.a06.start",
                }
            )
            worker.state_path.write_text(json.dumps(orphaned), encoding="utf-8")
            worker.turn_state = TurnState.ORPHANED
            worker.orphaned_at = "2026-07-14T00:00:00"
            worker.orphaned_reason = "runner-old failed"
            with stage_runner_context("runner-redirected"):
                worker.set_runtime_metadata(workflow_action="overall_review")
            state = worker.read_state()

        self.assertEqual(worker.stage_runner_id, "runner-redirected")
        self.assertEqual(state["stage_runner_id"], "runner-redirected")
        self.assertEqual(state["workflow_action"], "overall_review")
        self.assertEqual(state["turn_state"], TurnState.IDLE.value)
        self.assertEqual(state["orphaned_at"], "")
        self.assertEqual(state["orphaned_reason"], "")
        self.assertEqual(worker.turn_state, TurnState.IDLE)

    def test_late_worker_result_cannot_clear_same_runner_orphan_marker(self):
        with tempfile.TemporaryDirectory() as tmp_dir, mock.patch.object(
            TmuxBackend, "list_sessions", return_value=[]
        ):
            worker = TmuxBatchWorker(
                worker_id="late-orphan-writer",
                work_dir=tmp_dir,
                config=AgentRunConfig(vendor="codex", model="gpt-5.4-mini"),
                runtime_root=Path(tmp_dir) / "runtime",
                runtime_metadata={"stage_runner_id": "runner-1"},
            )
            worker.agent_state = AgentRuntimeState.READY
            worker.agent_started = True
            worker.session_exists = lambda: True
            worker._write_session_created_state_fast()  # noqa: SLF001
            orphaned = worker.read_state()
            orphaned.update(
                {
                    "turn_state": "orphaned",
                    "stage_runner_id": "runner-1",
                    "orphaned_at": "2026-07-14T00:00:00",
                    "orphaned_reason": "runner_failure",
                    "orphaned_stage_action": "stage.a07.start",
                }
            )
            worker.state_path.write_text(json.dumps(orphaned), encoding="utf-8")
            worker.turn_state = TurnState.WAITING_RESULT

            worker._record_result(  # noqa: SLF001
                CommandResult(
                    label="late result",
                    command="prompt",
                    exit_code=0,
                    raw_output="done",
                    clean_output="done",
                    started_at="2026-07-14T00:00:00",
                    finished_at="2026-07-14T00:00:01",
                ),
                status=WorkerStatus.SUCCEEDED,
                note="done:late result",
            )
            state = worker.read_state()
            worker._write_session_created_state_fast()  # noqa: SLF001
            session_created_state = worker.read_state()

        self.assertEqual(state["turn_state"], "orphaned")
        self.assertEqual(state["orphaned_at"], "2026-07-14T00:00:00")
        self.assertEqual(state["orphaned_reason"], "runner_failure")
        self.assertEqual(state["orphaned_stage_action"], "stage.a07.start")
        self.assertEqual(worker.turn_state, TurnState.ORPHANED)
        self.assertEqual(session_created_state["turn_state"], "orphaned")
        self.assertEqual(session_created_state["orphaned_reason"], "runner_failure")

    def test_submission_unknown_accepts_valid_completed_file_contracts(self):
        class ContractProofWorker(TmuxBatchWorker):
            def __init__(self, *, proof_kind: str, contract_path: Path, artifact_path: Path, **kwargs):  # noqa: ANN003
                self.proof_kind = proof_kind
                self.contract_path = contract_path
                self.artifact_path = artifact_path
                self.send_attempts = 0
                super().__init__(**kwargs)

            def _append_transcript(self, title, body):  # noqa: ANN001, ARG002
                return None

            def ensure_agent_ready(self, timeout_sec=60.0):  # noqa: ARG002
                self.pane_id = "%1"
                self.agent_ready = True
                self.agent_started = True
                self.agent_state = AgentRuntimeState.READY
                self.current_command = "codex"
                self.current_path = str(self.work_dir)

            def session_exists(self):
                return True

            def target_exists(self, target=None):  # noqa: ANN001, ARG002
                return True

            def observe(self, *, tail_lines=500, tail_bytes=24000):  # noqa: ARG002
                return runtime_module.WorkerObservation(
                    visible_text="ready",
                    raw_log_delta="",
                    raw_log_tail="ready",
                    current_command="codex",
                    current_path=str(self.work_dir),
                    pane_dead=False,
                    session_exists=True,
                    log_mtime=0.0,
                    observed_at="2026-07-14T00:00:00",
                    pane_title="TmuxCodingTeam",
                )

            def _send_text(self, text, enter_count=None):  # noqa: ANN001, ARG002
                self.send_attempts += 1
                if self.proof_kind == "completion":
                    self.artifact_path.write_text('{"ready": true}', encoding="utf-8")
                    self.contract_path.write_text(
                        json.dumps(
                            {
                                "schema_version": "1.0",
                                "turn_id": "contract-proof",
                                "phase": "contract-proof",
                                "status": "done",
                            }
                        ),
                        encoding="utf-8",
                    )
                else:
                    Path(self.current_task_result_path).write_text(
                        json.dumps(
                            {
                                "schema_version": "1.0",
                                "turn_id": "contract-proof",
                                "phase": "contract-proof",
                                "task_kind": "contract-proof",
                                "status": "completed",
                                "summary": "done",
                                "artifacts": {},
                                "artifact_hashes": {},
                            }
                        ),
                        encoding="utf-8",
                    )
                raise TmuxMutationOutcomeUnknown(operation="paste-buffer", error="timeout")

            def _wait_for_prompt_submission(self, **kwargs):  # noqa: ANN003
                raise TimeoutError("no BUSY or prompt echo")

        for proof_kind in ("completion", "result"):
            with self.subTest(proof_kind=proof_kind), tempfile.TemporaryDirectory() as tmp_dir, mock.patch.object(
                TmuxBackend, "list_sessions", return_value=[]
            ):
                root = Path(tmp_dir)
                contract_path = root / "contract.json"
                artifact_path = root / "artifact.json"

                def validator(path: Path) -> TurnFileResult:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                    return TurnFileResult(
                        status_path=str(path),
                        payload=payload,
                        artifact_paths={"artifact": str(artifact_path)},
                        artifact_hashes={"artifact": "sha256:test"},
                        validated_at="2026-07-14T00:00:00",
                    )

                worker = ContractProofWorker(
                    proof_kind=proof_kind,
                    contract_path=contract_path,
                    artifact_path=artifact_path,
                    worker_id=f"contract-proof-{proof_kind}",
                    work_dir=root,
                    config=AgentRunConfig(vendor="codex", model="gpt-5.4-mini"),
                    runtime_root=root / "runtime",
                )
                kwargs = {}
                if proof_kind == "completion":
                    kwargs["completion_contract"] = TurnFileContract(
                        turn_id="contract-proof",
                        phase="contract-proof",
                        status_path=contract_path,
                        validator=validator,
                        quiet_window_sec=0.0,
                    )
                else:
                    kwargs["result_contract"] = TaskResultContract(
                        turn_id="contract-proof",
                        phase="contract-proof",
                        task_kind="contract-proof",
                        mode="contract-proof",
                        expected_statuses=("completed",),
                    )

                result = worker.run_turn(
                    label="contract-proof",
                    prompt="do the work",
                    timeout_sec=2.0,
                    prompt_submit_timeout_sec=0.1,
                    **kwargs,
                )

                self.assertTrue(result.ok)
                self.assertEqual(worker.send_attempts, 1)
                self.assertEqual(worker.read_state()["turn_state"], TurnState.SUCCEEDED.value)

    def test_typed_transport_failure_record_does_not_probe_tmux_again(self):
        class NoProbeWorker(TmuxBatchWorker):
            def __init__(self, **kwargs):  # noqa: ANN003
                self.health_probe_calls = 0
                super().__init__(**kwargs)

            def is_agent_alive(self, *args, **kwargs):  # noqa: ANN002, ANN003
                self.health_probe_calls += 1
                raise AssertionError("failure persistence must not probe agent liveness")

            def get_agent_state(self, *args, **kwargs):  # noqa: ANN002, ANN003
                self.health_probe_calls += 1
                raise AssertionError("failure persistence must not probe agent state")

        errors = (
            TmuxControlUnavailable(
                operation="list-panes",
                error="timeout",
                elapsed_sec=60.0,
                attempts=4,
            ),
            TmuxMutationOutcomeUnknown(
                operation="paste-buffer",
                error=subprocess.TimeoutExpired(["tmux", "paste-buffer"], 10.0),
            ),
        )
        for index, error in enumerate(errors):
            with self.subTest(error=type(error).__name__), tempfile.TemporaryDirectory() as tmp_dir:
                worker = NoProbeWorker(
                    worker_id=f"no-probe-worker-{index}",
                    work_dir=tmp_dir,
                    config=AgentRunConfig(vendor="codex", model="gpt-5.4-mini"),
                    runtime_root=Path(tmp_dir) / "runtime",
                )
                worker.agent_state = AgentRuntimeState.READY
                worker.agent_ready = True
                worker.agent_started = True
                worker._write_session_created_state_fast()  # noqa: SLF001
                prior = worker.read_state()
                prior.update({"agent_alive": True, "agent_ready": True, "agent_state": "READY"})
                worker.state_path.write_text(json.dumps(prior), encoding="utf-8")
                error_extra = worker._turn_failure_runtime_state_extra(str(error), error=error)  # noqa: SLF001
                worker._record_result(  # noqa: SLF001
                    CommandResult(
                        label="transport failure",
                        command="prompt",
                        exit_code=1,
                        raw_output=str(error),
                        clean_output=str(error),
                        started_at="2026-07-14T00:00:00",
                        finished_at="2026-07-14T00:00:01",
                    ),
                    status=WorkerStatus.FAILED,
                    note="transport_failure",
                    extra=error_extra,
                )
                failed = worker.read_state()

            self.assertEqual(worker.health_probe_calls, 0)
            self.assertEqual(failed["status"], WorkerStatus.FAILED.value)
            self.assertTrue(failed["agent_alive"])
            self.assertEqual(failed["agent_state"], AgentRuntimeState.READY.value)

    def test_done_signal_with_missing_or_invalid_result_contract_keeps_turn_failed(self):
        class FixedHealthWorker(TmuxBatchWorker):
            def is_agent_alive(self, *args, **kwargs):  # noqa: ANN002, ANN003
                return True

            def get_agent_state(self, *args, **kwargs):  # noqa: ANN002, ANN003
                return AgentRuntimeState.READY

        for contract_case in ("missing", "invalid"):
            with self.subTest(contract_case=contract_case), tempfile.TemporaryDirectory() as tmp_dir:
                result_path = Path(tmp_dir) / "task.result.json"
                if contract_case == "invalid":
                    result_path.write_text("{invalid-json", encoding="utf-8")
                status_path = Path(tmp_dir) / "task.status.json"
                status_path.write_text('{"status":"done"}', encoding="utf-8")
                worker = FixedHealthWorker(
                    worker_id=f"failed-contract-{contract_case}",
                    work_dir=tmp_dir,
                    config=AgentRunConfig(vendor="codex", model="gpt-5.4-mini"),
                    runtime_root=Path(tmp_dir) / "runtime",
                )
                worker.current_task_runtime_status = "done"
                worker._record_result(  # noqa: SLF001
                    CommandResult(
                        label="invalid result contract",
                        command="prompt",
                        exit_code=1,
                        raw_output="result contract validation failed",
                        clean_output="result contract validation failed",
                        started_at="2026-07-14T00:00:00",
                        finished_at="2026-07-14T00:00:01",
                    ),
                    status=WorkerStatus.FAILED,
                    note="result_contract_failed",
                    extra={
                        "agent_alive": True,
                        "agent_state": AgentRuntimeState.READY.value,
                        "result_status": WorkerStatus.FAILED.value,
                        "current_task_status_path": str(status_path),
                        "current_task_result_path": str(result_path),
                        "current_task_runtime_status": "done",
                    },
                )
                failed = worker.read_state()

            self.assertEqual(failed["status"], WorkerStatus.FAILED.value)
            self.assertEqual(failed["result_status"], WorkerStatus.FAILED.value)
            self.assertEqual(failed["current_task_runtime_status"], "done")
            self.assertEqual(failed["turn_state"], TurnState.FAILED.value)

    def test_mark_orphaned_preserves_agent_health_and_session(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            worker = TmuxBatchWorker(
                worker_id="orphan-worker",
                work_dir=tmp_dir,
                config=AgentRunConfig(vendor="codex", model="gpt-5.4-mini"),
                runtime_root=Path(tmp_dir) / "runtime",
            )
            worker.pane_id = "%9"
            worker.agent_state = AgentRuntimeState.READY
            live_session = worker.session_name
            worker._write_session_created_state_fast()  # noqa: SLF001
            state = worker.read_state()
            state.update({"agent_state": "READY", "agent_alive": True})
            worker.state_path.write_text(json.dumps(state), encoding="utf-8")
            worker.mark_orphaned("runner failed", "runner-1")
            orphaned = worker.read_state()
        self.assertEqual(orphaned["turn_state"], TurnState.ORPHANED.value)
        self.assertEqual(orphaned["agent_state"], "READY")
        self.assertTrue(orphaned["agent_alive"])
        self.assertEqual(orphaned["session_name"], live_session)
        self.assertEqual(orphaned["stage_runner_id"], "runner-1")

    def test_richer_resume_assessment_does_not_clear_unresolved_turn(self):
        class PendingWorker:
            def read_state(self):
                return {
                    "agent_state": "READY",
                    "agent_alive": True,
                    "turn_state": "waiting_result",
                    "current_task_runtime_status": "running",
                }

        assessment = assess_worker_resume(PendingWorker(), timeout_sec=0.0)
        self.assertFalse(assessment.resumable)
        self.assertEqual(assessment.reason, "turn_unresolved")
        self.assertEqual(assessment.agent_state, "READY")

    def test_orphaned_worker_is_never_auto_resumable(self):
        class OrphanedWorker:
            def read_state(self):
                return {
                    "agent_state": "READY",
                    "agent_alive": True,
                    "turn_state": TurnState.ORPHANED.value,
                    "stage_runner_id": "runner-failed",
                }

            def session_exists(self):
                raise AssertionError("orphaned worker must fail closed before tmux probing")

        assessment = assess_worker_resume(OrphanedWorker(), timeout_sec=60.0)

        self.assertFalse(assessment.resumable)
        self.assertEqual(assessment.reason, "worker_orphaned")
        self.assertEqual(assessment.turn_state, TurnState.ORPHANED.value)

    def test_resume_assessment_propagates_tmux_probe_uncertainty(self):
        errors = (
            TmuxControlUnavailable(
                operation="has-session",
                error="timeout",
                elapsed_sec=60.0,
                attempts=4,
            ),
            TmuxMutationOutcomeUnknown(
                operation="probe-fixture",
                error="outcome unknown",
            ),
        )

        for error in errors:
            with self.subTest(error=type(error).__name__):
                class UncertainWorker:
                    def read_state(self):
                        return {}

                    def session_exists(self):
                        raise error

                with self.assertRaises(type(error)):
                    assess_worker_resume(UncertainWorker(), timeout_sec=0.0)

    def test_done_status_without_validated_result_remains_unresolved(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            status_path = Path(tmp_dir) / "task.status.json"
            status_path.write_text('{"status":"done"}', encoding="utf-8")

            class PendingWorker:
                def read_state(self):
                    return {
                        "agent_state": "READY",
                        "agent_alive": True,
                        # Legacy snapshots may not have turn_state yet; a done
                        # status signal still cannot stand in for result validation.
                        "turn_state": "",
                        "current_task_runtime_status": "running",
                        "current_task_status_path": str(status_path),
                        "current_task_result_path": str(Path(tmp_dir) / "missing.result.json"),
                    }

            assessment = assess_worker_resume(PendingWorker(), timeout_sec=0.0)
        self.assertFalse(assessment.resumable)
        self.assertEqual(assessment.reason, "turn_unresolved")

    def test_failure_diagnostic_cannot_replace_primary_error_or_skip_result_record(self):
        class DiagnosticFailureWorker(TmuxBatchWorker):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.record_calls = 0
                self.primary_failed = False

            def ensure_agent_ready(self, timeout_sec=60.0):  # noqa: ANN001, ARG002
                self.pane_id = "%1"
                self.agent_started = True
                self.agent_ready = True
                self.agent_state = AgentRuntimeState.READY
                self.current_command = "codex"

            def _ensure_agent_ready_for_turn_start(self, **kwargs):  # noqa: ANN003
                self.ensure_agent_ready(timeout_sec=float(kwargs.get("timeout_sec", 0.0)))

            def session_exists(self):
                return True

            def observe(self, *, tail_lines=500, tail_bytes=24000):  # noqa: ARG002
                return runtime_module.WorkerObservation(
                    visible_text="ready",
                    raw_log_delta="",
                    raw_log_tail="ready",
                    current_command="codex",
                    current_path=str(self.work_dir),
                    pane_dead=False,
                    session_exists=True,
                    log_mtime=0.0,
                    observed_at="2026-07-14T00:00:00",
                    pane_title="",
                )

            def _send_text(self, text, enter_count=None):  # noqa: ANN001, ARG002
                self.primary_failed = True
                raise RuntimeError("primary turn failure")

            def target_exists(self, target=None):  # noqa: ANN001, ARG002
                if self.primary_failed:
                    raise RuntimeError("secondary diagnostic failure")
                return True

            def _turn_failure_runtime_state_extra(self, clean_output, *, error=None):  # noqa: ANN001, ARG002
                return {}

            def _record_result(self, *args, **kwargs):  # noqa: ANN002, ANN003
                self.record_calls += 1
                return super()._record_result(*args, **kwargs)

        with tempfile.TemporaryDirectory() as tmp_dir:
            worker = DiagnosticFailureWorker(
                worker_id="diagnostic-failure-worker",
                work_dir=tmp_dir,
                config=AgentRunConfig(vendor="codex", model="gpt-5.4-mini"),
                runtime_root=Path(tmp_dir) / "runtime",
            )

            result = worker.run_turn(label="diagnostic_failure", prompt="hello", timeout_sec=0.1)
            state = worker.read_state()

        self.assertFalse(result.ok)
        self.assertIn("primary turn failure", result.clean_output)
        self.assertIn("diagnostic unavailable: secondary diagnostic failure", result.clean_output)
        self.assertEqual(worker.record_calls, 1)
        self.assertEqual(state["turn_state"], TurnState.FAILED.value)

    def test_run_turn_records_and_reraises_typed_runtime_failures(self):
        failures = (
            TmuxControlUnavailable(
                operation="list-panes",
                error="timeout",
                elapsed_sec=60.0,
                attempts=4,
            ),
            TmuxMutationOutcomeUnknown(operation="paste-buffer", error="timeout"),
        )

        class TypedFailureWorker(TmuxBatchWorker):
            failure: Exception

            def is_agent_alive(self, observation=None):  # noqa: ANN001, ARG002
                return True

            def get_agent_state(self, observation=None, *, task_running_override=None):  # noqa: ANN001, ARG002
                return AgentRuntimeState.READY

            def _ensure_agent_ready_for_turn_start(self, **kwargs):  # noqa: ANN003
                raise self.failure

        for index, failure in enumerate(failures):
            with self.subTest(error=type(failure).__name__), tempfile.TemporaryDirectory() as tmp_dir:
                worker = TypedFailureWorker(
                    worker_id=f"typed-failure-{index}",
                    work_dir=tmp_dir,
                    config=AgentRunConfig(vendor="codex", model="gpt-5.4-mini"),
                    runtime_root=Path(tmp_dir) / "runtime",
                )
                worker.failure = failure

                with self.assertRaises(type(failure)) as raised:
                    worker.run_turn(label="typed_failure", prompt="hello", timeout_sec=0.1)

                self.assertIs(raised.exception, failure)
                self.assertEqual(len(worker.results), 1)
                self.assertEqual(worker.read_state()["turn_state"], TurnState.FAILED.value)

    def test_run_turn_reraises_startup_intervention_without_poisoning_results(self):
        class StartupInterventionWorker(TmuxBatchWorker):
            def is_agent_alive(self, observation=None):  # noqa: ANN001, ARG002
                return True

            def get_agent_state(self, observation=None, *, task_running_override=None):  # noqa: ANN001, ARG002
                return AgentRuntimeState.STARTING

            def _ensure_agent_ready_for_turn_start(self, **kwargs):  # noqa: ANN003
                raise AgentStartupInterventionRequired(
                    blocker_kind="deveco_login",
                    session_name=self.session_name,
                    state_path=str(self.state_path),
                    message="login required",
                )

        with tempfile.TemporaryDirectory() as tmp_dir:
            worker = StartupInterventionWorker(
                worker_id="startup-intervention",
                work_dir=tmp_dir,
                config=AgentRunConfig(vendor="codex", model="gpt-5.4-mini"),
                runtime_root=Path(tmp_dir) / "runtime",
            )

            with self.assertRaises(AgentStartupInterventionRequired):
                worker.run_turn(label="startup_intervention", prompt="hello", timeout_sec=0.1)

            state = worker.read_state()
        self.assertEqual(worker.results, [])
        self.assertEqual(state["status"], WorkerStatus.RUNNING.value)
        self.assertEqual(state["result_status"], "running")
        self.assertEqual(state["startup_blocker_kind"], "deveco_login")

    def test_runtime_intervention_is_distinct_from_startup_and_preserves_submitted_turn(self):
        class FakeCodexConfig:
            vendor = runtime_module.Vendor.CODEX
            model = "gpt-test"

            def build_launch_command(self, _work_dir):  # noqa: ANN001
                return "codex"

            def to_summary(self):
                return {"vendor": "codex", "model": self.model}

            def expected_current_commands(self):
                return ("codex", "node")

        class RuntimeInterventionWorker(TmuxBatchWorker):
            def __init__(self, **kwargs):  # noqa: ANN003
                self.send_attempts = 0
                super().__init__(**kwargs)

            def is_agent_alive(self, observation=None):  # noqa: ANN001, ARG002
                return True

            def get_agent_state(self, observation=None, *, task_running_override=None):  # noqa: ANN001, ARG002
                return self.agent_state

            def _ensure_agent_ready_for_turn_start(self, **kwargs):  # noqa: ANN003
                self.pane_id = "%1"
                self.agent_started = True
                self.agent_ready = True
                self.agent_state = AgentRuntimeState.READY
                self.current_command = "codex"
                self.current_path = str(self.work_dir)

            def observe(self, *, tail_lines=500, tail_bytes=24000):  # noqa: ARG002
                return runtime_module.WorkerObservation(
                    visible_text="ready",
                    raw_log_delta="",
                    raw_log_tail="ready",
                    current_command="codex",
                    current_path=str(self.work_dir),
                    pane_dead=False,
                    session_exists=True,
                    log_mtime=0.0,
                    observed_at="2026-07-18T00:00:00",
                    pane_title="TmuxCodingTeam",
                )

            def _send_text(self, text, enter_count=None):  # noqa: ANN001, ARG002
                self.send_attempts += 1

            def _wait_for_turn_reply(self, **kwargs):  # noqa: ANN003
                raise AgentRuntimeInterventionRequired(
                    blocker_kind="opencode_permission",
                    session_name=self.session_name,
                    state_path=str(self.state_path),
                    message="Agent runtime requires manual intervention",
                )

        with tempfile.TemporaryDirectory() as tmp_dir, mock.patch.object(
            TmuxBackend, "list_sessions", return_value=[]
        ):
            worker = RuntimeInterventionWorker(
                worker_id="runtime-intervention-after-submit",
                work_dir=tmp_dir,
                config=FakeCodexConfig(),
                runtime_root=Path(tmp_dir) / "runtime",
            )

            with self.assertRaises(AgentRuntimeInterventionRequired) as raised:
                worker.run_turn(label="runtime_intervention", prompt="hello", timeout_sec=0.1)

            state = worker.read_state()

        self.assertNotIsInstance(raised.exception, AgentStartupInterventionRequired)
        self.assertEqual(worker.send_attempts, 1)
        self.assertEqual(worker.results, [])
        self.assertEqual(state["status"], WorkerStatus.RUNNING.value)
        self.assertEqual(state["turn_state"], TurnState.WAITING_RESULT.value)
        self.assertEqual(state["dispatch_state"], "submitted")
        self.assertEqual(state["startup_blocker_kind"], "opencode_permission")

    def test_speculative_timeout_finalization_does_not_swallow_runtime_intervention(self):
        worker = object.__new__(TmuxBatchWorker)
        runtime_error = AgentRuntimeInterventionRequired(
            blocker_kind="opencode_permission",
            session_name="opencode-timeout-finalize",
            state_path="/tmp/opencode-timeout-finalize.state.json",
            message="Agent runtime requires manual intervention",
        )
        worker.wait_for_task_result = mock.Mock(side_effect=runtime_error)
        contract = mock.Mock()
        contract.phase = "timeout-finalize"

        with self.assertRaises(AgentRuntimeInterventionRequired) as raised:
            worker._try_finalize_task_result_after_prompt_timeout(  # noqa: SLF001
                contract=contract,
                task_status_path=None,
                result_path=Path("/tmp/opencode-timeout-finalize.result.json"),
                baseline_visible="",
                baseline_raw_log_tail="",
                prompt_submission_observed=True,
            )

        self.assertIs(raised.exception, runtime_error)

    def test_runtime_permission_handler_freezes_business_timeout_without_resending_prompt(self):
        class FakeOpenCodeConfig:
            vendor = runtime_module.Vendor.OPENCODE
            model = "test/model"

            def build_launch_command(self, _work_dir):  # noqa: ANN001
                return "opencode"

            def to_summary(self):
                return {"vendor": "opencode", "model": self.model}

            def expected_current_commands(self):
                return ("opencode", "node")

        class FakeClock:
            def __init__(self):
                self.value = 100.0

            def monotonic(self):
                return self.value

        class PermissionWorker(TmuxBatchWorker):
            def __init__(self, **kwargs):  # noqa: ANN003
                self.send_attempts = 0
                self.pause_delta = -1.0
                super().__init__(**kwargs)

            def is_agent_alive(self, observation=None):  # noqa: ANN001, ARG002
                return True

            def get_agent_state(self, observation=None, *, task_running_override=None):  # noqa: ANN001, ARG002
                if observation is None:
                    return self.agent_state
                return self.detector.classify_agent_state(observation)

            def _ensure_agent_ready_for_turn_start(self, **kwargs):  # noqa: ANN003
                self.pane_id = "%1"
                self.agent_started = True
                self.agent_ready = True
                self.agent_state = AgentRuntimeState.READY
                self.current_command = "node"
                self.current_path = str(self.work_dir)

            def observe(self, *, tail_lines=500, tail_bytes=24000):  # noqa: ARG002
                return runtime_module.WorkerObservation(
                    visible_text="Ask anything...\nctrl+p commands",
                    raw_log_delta="",
                    raw_log_tail="Ask anything...\nctrl+p commands",
                    current_command="node",
                    current_path=str(self.work_dir),
                    pane_dead=False,
                    session_exists=True,
                    log_mtime=0.0,
                    observed_at="2026-07-18T00:00:00",
                    pane_title="OpenCode",
                )

            def _send_text(self, text, enter_count=None):  # noqa: ANN001, ARG002
                self.send_attempts += 1

            def _wait_for_turn_reply(self, **kwargs):  # noqa: ANN003
                permission = runtime_module.WorkerObservation(
                    visible_text="Permission required\nAllow once\nAllow always\nReject",
                    raw_log_delta="",
                    raw_log_tail="old ready output only",
                    current_command="node",
                    current_path=str(self.work_dir),
                    pane_dead=False,
                    session_exists=True,
                    log_mtime=0.0,
                    observed_at="2026-07-18T00:00:01",
                    pane_title="OpenCode",
                )
                before = self._business_monotonic()
                self._handle_runtime_intervention_if_needed(permission, context="等待智能体回复")
                self.pause_delta = self._business_monotonic() - before
                self.agent_state = AgentRuntimeState.READY
                self.agent_ready = True
                return "done"

        clock = FakeClock()
        handled: list[AgentRuntimeInterventionRequired] = []

        def handle_runtime(_worker, error):  # noqa: ANN001
            handled.append(error)
            clock.value += 120.0

        with tempfile.TemporaryDirectory() as tmp_dir, mock.patch.object(
            TmuxBackend, "list_sessions", return_value=[]
        ), mock.patch.object(runtime_module.time, "monotonic", side_effect=clock.monotonic):
            worker = PermissionWorker(
                worker_id="runtime-permission-timeout-pause",
                work_dir=tmp_dir,
                config=FakeOpenCodeConfig(),
                runtime_root=Path(tmp_dir) / "runtime",
            )
            result = worker.run_turn(
                label="permission_pause",
                prompt="hello",
                timeout_sec=1.0,
                runtime_intervention_handler=handle_runtime,
            )
            state = worker.read_state()

        self.assertTrue(result.ok)
        self.assertEqual(worker.send_attempts, 1)
        self.assertEqual(len(handled), 1)
        self.assertEqual(handled[0].blocker_kind, "opencode_permission")
        self.assertAlmostEqual(worker.pause_delta, 0.0)
        self.assertEqual(state["startup_blocker_kind"], "")

    def test_runtime_intervention_handler_is_scoped_to_one_turn(self):
        worker = object.__new__(TmuxBatchWorker)
        worker._runtime_intervention_handler = None  # noqa: SLF001
        handlers_seen: list[object] = []

        def fake_run_turn_impl(**_kwargs):  # noqa: ANN003
            handlers_seen.append(worker._runtime_intervention_handler)  # noqa: SLF001
            return mock.sentinel.command_result

        worker._run_turn_impl = mock.Mock(side_effect=fake_run_turn_impl)  # type: ignore[method-assign]  # noqa: SLF001
        first_handler = mock.sentinel.first_runtime_handler

        first_result = worker.run_turn(
            label="first",
            prompt="first prompt",
            runtime_intervention_handler=first_handler,
        )
        second_result = worker.run_turn(label="second", prompt="second prompt")

        self.assertIs(first_result, mock.sentinel.command_result)
        self.assertIs(second_result, mock.sentinel.command_result)
        self.assertEqual(handlers_seen, [first_handler, None])
        self.assertIsNone(worker._runtime_intervention_handler)  # noqa: SLF001

        worker._run_turn_impl = mock.Mock(side_effect=RuntimeError("turn failed"))  # type: ignore[method-assign]  # noqa: SLF001
        with self.assertRaisesRegex(RuntimeError, "turn failed"):
            worker.run_turn(
                label="failing",
                prompt="failing prompt",
                runtime_intervention_handler=mock.sentinel.failing_runtime_handler,
            )
        self.assertIsNone(worker._runtime_intervention_handler)  # noqa: SLF001

    def test_opencode_permission_intervention_uses_current_visible_surface_only(self):
        detector = runtime_module.OpenCodeOutputDetector()
        permission_surface = "Permission required\nAllow once\nAllow always\nReject"
        ready_surface = "Ask anything...\nctrl+p commands"

        def observation(*, visible_text: str, raw_log_tail: str):
            return runtime_module.WorkerObservation(
                visible_text=visible_text,
                raw_log_delta="",
                raw_log_tail=raw_log_tail,
                current_command="node",
                current_path="/tmp/project",
                pane_dead=False,
                session_exists=True,
                log_mtime=0.0,
                observed_at="2026-07-18T00:00:00",
                pane_title="OpenCode",
            )

        active_permission = observation(
            visible_text=permission_surface,
            raw_log_tail=ready_surface,
        )
        stale_permission = observation(
            visible_text=ready_surface,
            raw_log_tail=permission_surface,
        )
        worker = object.__new__(TmuxBatchWorker)
        worker.config = mock.Mock(vendor=runtime_module.Vendor.OPENCODE)
        worker.session_name = "审核员-权限确认"
        worker.state_path = Path("/tmp/opencode-permission.state.json")

        error = worker._runtime_permission_intervention(  # noqa: SLF001
            active_permission,
            context="等待结果",
        )

        self.assertIsInstance(error, AgentRuntimeInterventionRequired)
        self.assertEqual(error.blocker_kind, "opencode_permission")
        self.assertIsNone(
            worker._runtime_permission_intervention(stale_permission, context="等待结果")  # noqa: SLF001
        )
        self.assertEqual(detector.classify_agent_state(active_permission), AgentRuntimeState.STARTING)
        self.assertEqual(detector.classify_agent_state(stale_permission), AgentRuntimeState.READY)


if __name__ == "__main__":
    unittest.main()
