from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tmux_core.stage_kernel.death_orchestration import (
    drop_dead_reviewers,
    replace_dead_main,
    run_main_phase_with_death_handling,
    run_reviewer_phase_with_death_handling,
)
from tmux_core.stage_kernel.agent_intervention import (
    AGENT_INTERVENTION_RECHECK,
    AGENT_INTERVENTION_RECREATE,
    AGENT_INTERVENTION_WORKER_DEAD,
    request_file_noncompliance_intervention,
    request_worker_manual_intervention,
)
from tmux_core.stage_kernel.role_orchestration import (
    WorkerReadyCheckFailed,
    ensure_main_ready,
    run_main_phase,
    run_reviewer_phase,
)
from tmux_core.runtime.tmux_runtime import AgentStartupInterventionRequired


class _FakeWorker:
    def __init__(self, state: str) -> None:
        self.state = state
        self.ensure_calls = 0

    def get_agent_state(self):
        return self.state

    def ensure_agent_ready(self, timeout_sec: float = 0.0) -> None:
        _ = timeout_sec
        self.ensure_calls += 1
        self.state = "READY"


class _HealthAwareWorker(_FakeWorker):
    def __init__(self, state: str, *, health_state: str) -> None:
        super().__init__(state)
        self.health_state = health_state

    def refresh_health(self, notify_on_change: bool = True):  # noqa: ARG002
        return SimpleNamespace(agent_state=self.health_state)

    def observe(self, tail_lines: int = 120):  # noqa: ARG002
        return SimpleNamespace()


class _ManualMarkerWorker:
    session_name = "测试工程师-参水猿"

    def __init__(self) -> None:
        self.marked_reconfig = False

    def read_state(self):
        return {"status": "ready", "agent_state": "READY", "health_status": "alive"}

    def mark_awaiting_reconfiguration(self, *, reason_text: str) -> None:  # noqa: ARG002
        self.marked_reconfig = True


class _LaggingRefreshWorker(_FakeWorker):
    def __init__(self, states: list[str]) -> None:
        super().__init__(states[0])
        self.states = list(states)
        self.refresh_calls = 0

    def refresh_health(self, notify_on_change: bool = True):  # noqa: ARG002
        self.refresh_calls += 1
        if self.states:
            self.state = self.states.pop(0)
        return SimpleNamespace(agent_state=self.state)

    def read_state(self):
        return {"health_status": "alive", "agent_state": self.state}

    def ensure_agent_ready(self, timeout_sec: float = 0.0) -> None:
        _ = timeout_sec
        self.ensure_calls += 1


class _CompletedBusyWorker(_FakeWorker):
    def __init__(self) -> None:
        super().__init__("READY")
        self.completed = False

    def read_state(self):
        if not self.completed:
            return {}
        return {
            "status": "succeeded",
            "result_status": "succeeded",
            "current_task_runtime_status": "done",
        }

    def ensure_agent_ready(self, timeout_sec: float = 0.0) -> None:
        _ = timeout_sec
        self.ensure_calls += 1
        self.state = "READY"


class _PrelaunchMissingSessionWorker(_FakeWorker):
    def __init__(self) -> None:
        super().__init__("STARTING")

    def read_state(self):
        return {
            "status": "running",
            "result_status": "running",
            "agent_state": "DEAD",
            "agent_started": False,
            "pane_id": "",
            "health_status": "missing_session",
            "workflow_stage": "pending",
        }


class _ActiveFailedBusyWorker(_FakeWorker):
    def __init__(self) -> None:
        super().__init__("BUSY")

    def read_state(self):
        return {
            "status": "failed",
            "result_status": "failed",
            "agent_state": self.state,
            "health_status": "alive",
            "terminal_recently_changed": True,
        }


class _DeathAwareFakeWorker:
    def __init__(self, state: str, *, launched: bool) -> None:
        self.state = state
        self._launched = launched
        self.ensure_calls = 0
        self.agent_started = launched
        self.pane_id = "pane_1" if launched else ""
        self.state_path = "/tmp/nonexistent-worker.state.json"

    def get_agent_state(self):
        return SimpleNamespace(value=self.state)

    def has_ever_launched(self) -> bool:
        return self._launched

    def ensure_agent_ready(self, timeout_sec: float = 0.0) -> None:
        _ = timeout_sec
        self.ensure_calls += 1
        self._launched = True
        self.agent_started = True
        self.pane_id = "pane_1"
        self.state = "READY"


class _ReadyDeathWorker(_DeathAwareFakeWorker):
    def __init__(self, *, session_name: str) -> None:
        super().__init__("STARTING", launched=True)
        self.session_name = session_name

    def ensure_agent_ready(self, timeout_sec: float = 0.0) -> None:
        _ = timeout_sec
        self.ensure_calls += 1
        raise RuntimeError(
            f"检测到 {self.session_name} 需要重新启动或重建，但系统不会自动执行。\n"
            f"原因: tmux pane missing"
        )


class _StartupInterventionWorker(_DeathAwareFakeWorker):
    def __init__(self) -> None:
        super().__init__("STARTING", launched=True)
        self.session_name = "开发工程师-启动介入"
        self.startup_error = AgentStartupInterventionRequired(
            blocker_kind="deveco_login",
            session_name=self.session_name,
            state_path=self.state_path,
            message="DevEco login requires manual intervention",
        )

    def ensure_agent_ready(self, timeout_sec: float = 0.0) -> None:
        _ = timeout_sec
        self.ensure_calls += 1
        raise self.startup_error


class RoleOrchestrationTests(unittest.TestCase):
    def test_manual_intervention_noninteractive_uses_explicit_default_without_prompt(self):
        with mock.patch(
            "tmux_core.stage_kernel.agent_intervention.terminal_ui_is_interactive",
            return_value=False,
        ), mock.patch(
            "tmux_core.stage_kernel.agent_intervention.prompt_select_option",
        ) as prompt:
            decision = request_worker_manual_intervention(
                stage_label="任务开发",
                role_label="开发工程师",
                worker=None,
                reason_text="tmux pane exited",
                allow_recreate=True,
                noninteractive_default=AGENT_INTERVENTION_RECREATE,
            )

        self.assertEqual(decision, AGENT_INTERVENTION_RECREATE)
        prompt.assert_not_called()

    def test_manual_intervention_noninteractive_without_default_fails_clearly(self):
        with mock.patch(
            "tmux_core.stage_kernel.agent_intervention.terminal_ui_is_interactive",
            return_value=False,
        ), mock.patch(
            "tmux_core.stage_kernel.agent_intervention.prompt_select_option",
        ) as prompt:
            with self.assertRaisesRegex(RuntimeError, "当前环境不可交互"):
                request_worker_manual_intervention(
                    stage_label="任务开发",
                    role_label="开发工程师",
                    worker=None,
                    reason_text="tmux pane exited",
                    allow_recreate=True,
                )

        prompt.assert_not_called()

    def test_manual_intervention_interactive_still_prompts(self):
        with mock.patch(
            "tmux_core.stage_kernel.agent_intervention.terminal_ui_is_interactive",
            return_value=True,
        ), mock.patch(
            "tmux_core.stage_kernel.agent_intervention.prompt_select_option",
            return_value=AGENT_INTERVENTION_RECHECK,
        ) as prompt:
            decision = request_worker_manual_intervention(
                stage_label="任务开发",
                role_label="开发工程师",
                worker=None,
                reason_text="tmux pane exited",
                allow_recreate=True,
                noninteractive_default=AGENT_INTERVENTION_RECREATE,
            )

        self.assertEqual(decision, AGENT_INTERVENTION_RECHECK)
        prompt.assert_called_once()

    def test_file_noncompliance_intervention_does_not_mark_ready_worker_as_reconfiguring(self):
        worker = _ManualMarkerWorker()
        with mock.patch(
            "tmux_core.stage_kernel.agent_intervention.terminal_ui_is_interactive",
            return_value=True,
        ), mock.patch(
            "tmux_core.stage_kernel.agent_intervention.prompt_select_option",
            return_value=AGENT_INTERVENTION_RECHECK,
        ) as prompt:
            decision = request_file_noncompliance_intervention(
                stage_label="详细设计",
                role_label="测试工程师",
                worker=worker,
                reason_text="review_pass=false 但评审 markdown 为空",
                attempts_used=2,
                target_paths=("/tmp/review.md", "/tmp/review.json"),
                allow_recreate=True,
            )

        self.assertEqual(decision, AGENT_INTERVENTION_RECHECK)
        self.assertFalse(worker.marked_reconfig)
        extra_payload = prompt.call_args.kwargs["extra_payload"]
        self.assertEqual(extra_payload["recovery_kind"], "file_noncompliance")
        self.assertEqual(extra_payload["session_name"], "测试工程师-参水猿")
        self.assertEqual(
            extra_payload["target_paths"],
            [str(Path("/tmp/review.md").resolve()), str(Path("/tmp/review.json").resolve())],
        )

    def test_ensure_main_ready_prefers_refresh_health_state(self):
        main = SimpleNamespace(worker=_HealthAwareWorker("BUSY", health_state="READY"))

        ensure_main_ready(main)

        self.assertEqual(main.worker.ensure_calls, 0)

    def test_ensure_main_ready_recovers_non_ready_main_and_reviewers(self):
        main = SimpleNamespace(worker=_FakeWorker("BUSY"))
        reviewers = [
            SimpleNamespace(worker=_FakeWorker("STARTING"), reviewer_name="R1"),
            SimpleNamespace(worker=_FakeWorker("DEAD"), reviewer_name="R2"),
        ]

        ensure_main_ready(
            main,
            reviewers,
            reviewer_label_getter=lambda reviewer, index: reviewer.reviewer_name or f"R{index}",
        )

        self.assertEqual(main.worker.state, "READY")
        self.assertEqual(main.worker.ensure_calls, 1)
        self.assertEqual([item.worker.state for item in reviewers], ["READY", "READY"])
        self.assertEqual([item.worker.ensure_calls for item in reviewers], [1, 1])

    def test_ensure_main_ready_allows_prelaunch_missing_session_to_start(self):
        worker = _PrelaunchMissingSessionWorker()
        main = SimpleNamespace(worker=worker)

        ensure_main_ready(main)

        self.assertEqual(worker.ensure_calls, 1)
        self.assertEqual(worker.state, "READY")

    def test_ensure_main_ready_waits_for_active_failed_busy_worker(self):
        worker = _ActiveFailedBusyWorker()
        main = SimpleNamespace(worker=worker)

        ensure_main_ready(main, timeout_sec=3.0)

        self.assertEqual(worker.ensure_calls, 1)
        self.assertEqual(worker.state, "READY")

    def test_run_main_phase_waits_before_and_after_owner_turn(self):
        main = SimpleNamespace(worker=_FakeWorker("READY"))
        reviewer = SimpleNamespace(worker=_FakeWorker("READY"), reviewer_name="审计员")
        observed: list[str] = []

        def _run_phase(owner):  # noqa: ANN001
            observed.append("run_main_phase")
            owner.worker.state = "BUSY"
            return owner

        updated = run_main_phase(
            main,
            reviewers=[reviewer],
            run_phase=_run_phase,
            main_label="主工作智能体",
            reviewer_label_getter=lambda item, index: item.reviewer_name or f"R{index}",
        )

        self.assertIs(updated, main)
        self.assertEqual(observed, ["run_main_phase"])
        self.assertEqual(main.worker.state, "READY")
        self.assertEqual(main.worker.ensure_calls, 1)
        self.assertEqual(reviewer.worker.ensure_calls, 0)

    def test_ensure_main_ready_rechecks_with_refresh_health_after_ensure(self):
        worker = _HealthAwareWorker("BUSY", health_state="BUSY")
        main = SimpleNamespace(worker=worker)

        def _ensure_agent_ready(timeout_sec: float = 0.0) -> None:
            _ = timeout_sec
            worker.ensure_calls += 1
            worker.state = "BUSY"
            worker.health_state = "READY"

        worker.ensure_agent_ready = _ensure_agent_ready

        ensure_main_ready(main)

        self.assertEqual(worker.ensure_calls, 1)

    def test_ensure_main_ready_waits_for_ready_stabilization_after_ensure(self):
        worker = _LaggingRefreshWorker(["BUSY", "BUSY", "READY"])
        main = SimpleNamespace(worker=worker)

        with mock.patch("tmux_core.stage_kernel.role_orchestration.time.sleep", return_value=None):
            ensure_main_ready(main)

        self.assertEqual(worker.ensure_calls, 1)
        self.assertEqual(worker.state, "READY")
        self.assertEqual(worker.refresh_calls, 3)

    def test_ensure_main_ready_allows_startup_busy_past_short_grace(self):
        worker = _LaggingRefreshWorker(["BUSY", "BUSY", "BUSY", "BUSY", "READY"])
        main = SimpleNamespace(worker=worker)

        with mock.patch(
            "tmux_core.stage_kernel.role_orchestration.time.monotonic",
            side_effect=[0.0, 0.0, 11.0],
        ), mock.patch("tmux_core.stage_kernel.role_orchestration.time.sleep", return_value=None):
            ensure_main_ready(main)

        self.assertEqual(worker.ensure_calls, 1)
        self.assertEqual(worker.state, "READY")

    def test_ensure_main_ready_fails_after_ready_stabilization_grace_expires(self):
        worker = _LaggingRefreshWorker(["BUSY", "BUSY", "BUSY"])
        main = SimpleNamespace(worker=worker)

        with mock.patch(
            "tmux_core.stage_kernel.role_orchestration.time.monotonic",
            side_effect=[0.0, 0.0, 61.0],
        ), mock.patch("tmux_core.stage_kernel.role_orchestration.time.sleep", return_value=None):
            with self.assertRaisesRegex(WorkerReadyCheckFailed, "开发工程师 未进入 READY 状态"):
                ensure_main_ready(main, main_label="开发工程师")

        self.assertEqual(worker.ensure_calls, 1)
        self.assertEqual(worker.state, "BUSY")

    def test_run_reviewer_phase_waits_before_and_after_reviewer_round(self):
        main = SimpleNamespace(worker=_FakeWorker("READY"))
        reviewers = [
            SimpleNamespace(worker=_FakeWorker("BUSY"), reviewer_name="R1"),
            SimpleNamespace(worker=_FakeWorker("READY"), reviewer_name="R2"),
        ]
        observed: list[str] = []

        def _run_phase(active_reviewers):  # noqa: ANN001
            observed.append(",".join(item.reviewer_name for item in active_reviewers))
            active_reviewers[0].worker.state = "BUSY"
            return list(active_reviewers)

        updated = run_reviewer_phase(
            main,
            reviewers,
            run_phase=_run_phase,
            main_label="主工作智能体",
            reviewer_label_getter=lambda item, index: item.reviewer_name or f"R{index}",
        )

        self.assertEqual(observed, ["R1,R2"])
        self.assertEqual([item.worker.state for item in updated], ["READY", "READY"])
        self.assertEqual([item.worker.ensure_calls for item in updated], [2, 0])

    def test_run_reviewer_phase_allows_completed_busy_reviewer_after_round(self):
        main = SimpleNamespace(worker=_FakeWorker("READY"))
        reviewer_worker = _CompletedBusyWorker()
        reviewers = [SimpleNamespace(worker=reviewer_worker, reviewer_name="R1")]

        def _run_phase(active_reviewers):  # noqa: ANN001
            reviewer_worker.completed = True
            reviewer_worker.state = "BUSY"
            return list(active_reviewers)

        updated = run_reviewer_phase(
            main,
            reviewers,
            run_phase=_run_phase,
            main_label="主工作智能体",
            reviewer_label_getter=lambda item, index: item.reviewer_name or f"R{index}",
        )

        self.assertEqual([item.reviewer_name for item in updated], ["R1"])
        self.assertEqual(reviewer_worker.ensure_calls, 0)
        self.assertEqual(reviewer_worker.state, "BUSY")

    def test_drop_dead_reviewers_keeps_fresh_dead_workers_until_launch(self):
        fresh = SimpleNamespace(worker=_DeathAwareFakeWorker("DEAD", launched=False), reviewer_name="fresh")
        launched = SimpleNamespace(worker=_DeathAwareFakeWorker("DEAD", launched=True), reviewer_name="launched")
        alive = SimpleNamespace(worker=_DeathAwareFakeWorker("READY", launched=True), reviewer_name="alive")
        notices: list[str] = []

        with mock.patch(
            "tmux_core.stage_kernel.death_orchestration.request_worker_manual_intervention",
            return_value=AGENT_INTERVENTION_WORKER_DEAD,
        ) as prompt:
            survivors = drop_dead_reviewers(
                [fresh, launched, alive],
                reviewer_label_getter=lambda reviewer, _index: reviewer.reviewer_name,
                notify=notices.append,
            )

        self.assertEqual([item.reviewer_name for item in survivors], ["fresh", "alive"])
        self.assertEqual(notices, ["launched 已按死亡处理，后续将忽略该审核智能体。"])
        prompt.assert_called_once()

    def test_replace_dead_main_keeps_fresh_dead_owner_until_launch(self):
        fresh_main = SimpleNamespace(worker=_DeathAwareFakeWorker("DEAD", launched=False))
        launched_main = SimpleNamespace(worker=_DeathAwareFakeWorker("DEAD", launched=True))
        replacement = SimpleNamespace(worker=_DeathAwareFakeWorker("READY", launched=True))
        replace_calls: list[str] = []

        def _replace(_owner):  # noqa: ANN001
            replace_calls.append("called")
            return replacement

        self.assertIs(replace_dead_main(fresh_main, replace_owner=_replace), fresh_main)
        self.assertEqual(replace_calls, [])
        self.assertIs(replace_dead_main(launched_main, replace_owner=_replace), replacement)
        self.assertEqual(replace_calls, ["called"])

    def test_run_main_phase_with_death_handling_replaces_main_when_ready_detects_missing_pane(self):
        main = SimpleNamespace(worker=_ReadyDeathWorker(session_name="开发工程师-柳土獐"))
        replacement = SimpleNamespace(worker=_DeathAwareFakeWorker("READY", launched=True))
        replace_calls: list[object] = []

        def _replace(owner):  # noqa: ANN001
            replace_calls.append(owner)
            return replacement

        with mock.patch(
            "tmux_core.stage_kernel.death_orchestration.request_worker_manual_intervention",
            return_value=AGENT_INTERVENTION_RECREATE,
        ) as prompt:
            result, reviewers, current_main = run_main_phase_with_death_handling(
                main,
                reviewers=(),
                run_phase=lambda owner: owner,
                replace_dead_main_owner=_replace,
                main_label="开发工程师",
            )

        self.assertIs(result, replacement)
        self.assertEqual(reviewers, [])
        self.assertIs(current_main, replacement)
        self.assertEqual(replace_calls, [main])
        self.assertEqual(main.worker.ensure_calls, 1)
        prompt.assert_called_once()

    def test_startup_intervention_uses_dedicated_recovery_without_wrapping_or_recreating_main(self):
        worker = _StartupInterventionWorker()
        main = SimpleNamespace(worker=worker)
        replace_main = mock.Mock(side_effect=AssertionError("startup intervention must not recreate worker"))

        def recover_startup(live_worker, *, error, stage_label, role_label):  # noqa: ANN001
            self.assertIs(live_worker, worker)
            self.assertIs(error, worker.startup_error)
            self.assertEqual(stage_label, "阶段调度")
            self.assertEqual(role_label, "开发工程师")
            live_worker.state = "READY"

        with mock.patch(
            "tmux_core.stage_kernel.role_orchestration.wait_for_worker_startup_intervention",
            side_effect=recover_startup,
        ) as recover:
            result, reviewers, current_main = run_main_phase_with_death_handling(
                main,
                reviewers=(),
                run_phase=lambda owner: owner,
                replace_dead_main_owner=replace_main,
                main_label="开发工程师",
            )

        self.assertIs(result, main)
        self.assertEqual(reviewers, [])
        self.assertIs(current_main, main)
        self.assertEqual(worker.ensure_calls, 1)
        recover.assert_called_once()
        replace_main.assert_not_called()

    def test_run_main_phase_noninteractive_recreates_main_without_prompt(self):
        main = SimpleNamespace(worker=_ReadyDeathWorker(session_name="开发工程师-柳土獐"))
        replacement = SimpleNamespace(worker=_DeathAwareFakeWorker("READY", launched=True))
        replace_calls: list[object] = []

        with mock.patch(
            "tmux_core.stage_kernel.agent_intervention.terminal_ui_is_interactive",
            return_value=False,
        ), mock.patch(
            "tmux_core.stage_kernel.agent_intervention.prompt_select_option",
        ) as prompt:
            result, reviewers, current_main = run_main_phase_with_death_handling(
                main,
                reviewers=(),
                run_phase=lambda owner: owner,
                replace_dead_main_owner=lambda owner: replace_calls.append(owner) or replacement,
                main_label="开发工程师",
            )

        self.assertIs(result, replacement)
        self.assertEqual(reviewers, [])
        self.assertIs(current_main, replacement)
        self.assertEqual(replace_calls, [main])
        prompt.assert_not_called()

    def test_run_main_phase_with_death_handling_rechecks_ready_failure(self):
        main = SimpleNamespace(worker=_LaggingRefreshWorker(["BUSY", "BUSY", "BUSY", "BUSY", "READY"]))
        replace_calls: list[object] = []

        with mock.patch(
            "tmux_core.stage_kernel.role_orchestration.time.monotonic",
            side_effect=[0.0, 0.0, 61.0],
        ), mock.patch("tmux_core.stage_kernel.role_orchestration.time.sleep", return_value=None), mock.patch(
            "tmux_core.stage_kernel.death_orchestration.request_worker_manual_intervention",
            return_value=AGENT_INTERVENTION_RECHECK,
        ) as prompt:
            result, reviewers, current_main = run_main_phase_with_death_handling(
                main,
                reviewers=(),
                run_phase=lambda owner: owner,
                replace_dead_main_owner=lambda owner: replace_calls.append(owner) or owner,
                main_label="开发工程师",
            )

        self.assertIs(result, main)
        self.assertEqual(reviewers, [])
        self.assertIs(current_main, main)
        self.assertEqual(replace_calls, [])
        prompt.assert_called_once()

    def test_run_main_phase_with_death_handling_ignores_reviewer_ready_death(self):
        main = SimpleNamespace(worker=_DeathAwareFakeWorker("READY", launched=True))
        reviewer = SimpleNamespace(worker=_ReadyDeathWorker(session_name="审核员-地巧星"), reviewer_name="审核员-地巧星")
        replace_calls: list[object] = []

        result, reviewers, current_main = run_main_phase_with_death_handling(
            main,
            reviewers=[reviewer],
            run_phase=lambda owner: owner,
            replace_dead_main_owner=lambda owner: replace_calls.append(owner) or owner,
            main_label="开发工程师",
            reviewer_label_getter=lambda item, _index: item.reviewer_name,
        )

        self.assertIs(result, main)
        self.assertEqual(reviewers, [reviewer])
        self.assertIs(current_main, main)
        self.assertEqual(replace_calls, [])
        self.assertEqual(reviewer.worker.ensure_calls, 0)

    def test_run_main_phase_with_death_handling_drops_dead_reviewer_without_blocking_busy_reviewer(self):
        main = SimpleNamespace(worker=_DeathAwareFakeWorker("READY", launched=True))
        dead = SimpleNamespace(worker=_DeathAwareFakeWorker("DEAD", launched=True), reviewer_name="dead")
        busy = SimpleNamespace(worker=_DeathAwareFakeWorker("BUSY", launched=True), reviewer_name="busy")
        notices: list[str] = []

        with mock.patch(
            "tmux_core.stage_kernel.death_orchestration.request_worker_manual_intervention",
            return_value=AGENT_INTERVENTION_WORKER_DEAD,
        ) as prompt:
            result, reviewers, current_main = run_main_phase_with_death_handling(
                main,
                reviewers=[dead, busy],
                run_phase=lambda owner: owner,
                replace_dead_main_owner=lambda owner: owner,
                reviewer_label_getter=lambda item, _index: item.reviewer_name,
                notify=notices.append,
            )

        self.assertIs(result, main)
        self.assertIs(current_main, main)
        self.assertEqual(reviewers, [busy])
        self.assertEqual(busy.worker.ensure_calls, 0)
        self.assertEqual(notices, ["dead 已按死亡处理，后续将忽略该审核智能体。"])
        prompt.assert_called_once()

    def test_run_reviewer_phase_with_death_handling_replaces_dead_reviewer(self):
        main = SimpleNamespace(worker=_DeathAwareFakeWorker("READY", launched=True))
        dead_reviewer = SimpleNamespace(worker=_DeathAwareFakeWorker("DEAD", launched=True), reviewer_name="dead")
        replacement = SimpleNamespace(worker=_DeathAwareFakeWorker("READY", launched=True), reviewer_name="replacement")

        with mock.patch(
            "tmux_core.stage_kernel.death_orchestration.request_worker_manual_intervention",
            return_value=AGENT_INTERVENTION_RECREATE,
        ) as prompt:
            updated, current_main = run_reviewer_phase_with_death_handling(
                main,
                [dead_reviewer],
                run_phase=lambda reviewers: list(reviewers),
                replace_dead_main_owner=lambda owner: owner,
                replace_dead_reviewer=lambda reviewer, _index: replacement if reviewer is dead_reviewer else reviewer,
                reviewer_label_getter=lambda reviewer, _index: reviewer.reviewer_name,
            )

        self.assertIs(current_main, main)
        self.assertEqual([item.reviewer_name for item in updated], ["replacement"])
        prompt.assert_called_once()

    def test_run_reviewer_phase_with_death_handling_does_not_block_on_stale_reviewer(self):
        main = SimpleNamespace(worker=_DeathAwareFakeWorker("READY", launched=True))
        reviewer = SimpleNamespace(worker=_ActiveFailedBusyWorker(), reviewer_name="测试工程师")

        updated, current_main = run_reviewer_phase_with_death_handling(
            main,
            [reviewer],
            run_phase=lambda reviewers: list(reviewers),
            replace_dead_main_owner=lambda owner: owner,
            reviewer_label_getter=lambda item, _index: item.reviewer_name,
        )

        self.assertIs(current_main, main)
        self.assertEqual(updated, [reviewer])

    def test_run_reviewer_phase_with_death_handling_drops_reviewer_after_manual_dead_choice(self):
        main = SimpleNamespace(worker=_DeathAwareFakeWorker("READY", launched=True))
        reviewer = SimpleNamespace(worker=_LaggingRefreshWorker(["BUSY", "BUSY", "BUSY"]), reviewer_name="测试工程师")
        notices: list[str] = []

        with mock.patch(
            "tmux_core.stage_kernel.role_orchestration.time.monotonic",
            side_effect=[0.0, 0.0, 61.0],
        ), mock.patch("tmux_core.stage_kernel.role_orchestration.time.sleep", return_value=None), mock.patch(
            "tmux_core.stage_kernel.death_orchestration.request_worker_manual_intervention",
            return_value=AGENT_INTERVENTION_WORKER_DEAD,
        ) as prompt:
            updated, current_main = run_reviewer_phase_with_death_handling(
                main,
                [reviewer],
                run_phase=lambda reviewers: list(reviewers),
                replace_dead_main_owner=lambda owner: owner,
                reviewer_label_getter=lambda item, _index: item.reviewer_name,
                notify=notices.append,
            )

        self.assertIs(current_main, main)
        self.assertEqual(updated, [])
        self.assertEqual(notices, ["测试工程师 已按死亡处理，后续将忽略该审核智能体。"])
        prompt.assert_called_once()

    def test_run_reviewer_phase_noninteractive_drops_unready_reviewer_without_prompt(self):
        main = SimpleNamespace(worker=_DeathAwareFakeWorker("READY", launched=True))
        reviewer = SimpleNamespace(worker=_LaggingRefreshWorker(["BUSY", "BUSY", "BUSY"]), reviewer_name="测试工程师")
        notices: list[str] = []

        with mock.patch(
            "tmux_core.stage_kernel.role_orchestration.time.monotonic",
            side_effect=[0.0, 0.0, 61.0],
        ), mock.patch("tmux_core.stage_kernel.role_orchestration.time.sleep", return_value=None), mock.patch(
            "tmux_core.stage_kernel.agent_intervention.terminal_ui_is_interactive",
            return_value=False,
        ), mock.patch(
            "tmux_core.stage_kernel.agent_intervention.prompt_select_option",
        ) as prompt:
            updated, current_main = run_reviewer_phase_with_death_handling(
                main,
                [reviewer],
                run_phase=lambda reviewers: list(reviewers),
                replace_dead_main_owner=lambda owner: owner,
                reviewer_label_getter=lambda item, _index: item.reviewer_name,
                notify=notices.append,
            )

        self.assertIs(current_main, main)
        self.assertEqual(updated, [])
        self.assertEqual(notices, ["测试工程师 已按死亡处理，后续将忽略该审核智能体。"])
        prompt.assert_not_called()

    def test_run_reviewer_phase_with_death_handling_recreates_reviewer_after_manual_choice(self):
        main = SimpleNamespace(worker=_DeathAwareFakeWorker("READY", launched=True))
        reviewer = SimpleNamespace(worker=_ReadyDeathWorker(session_name="审核员-地巧星"), reviewer_name="审核员-地巧星")
        replacement = SimpleNamespace(worker=_DeathAwareFakeWorker("READY", launched=True), reviewer_name="审核员-新")
        replace_calls: list[object] = []

        with mock.patch(
            "tmux_core.stage_kernel.death_orchestration.request_worker_manual_intervention",
            return_value=AGENT_INTERVENTION_RECREATE,
        ) as prompt:
            updated, current_main = run_reviewer_phase_with_death_handling(
                main,
                [reviewer],
                run_phase=lambda reviewers: list(reviewers),
                replace_dead_main_owner=lambda owner: owner,
                replace_dead_reviewer=lambda item, _index: replace_calls.append(item) or replacement,
                reviewer_label_getter=lambda item, _index: item.reviewer_name,
            )

        self.assertIs(current_main, main)
        self.assertEqual(updated, [replacement])
        self.assertEqual(replace_calls, [reviewer])
        prompt.assert_called_once()


if __name__ == "__main__":
    unittest.main()
