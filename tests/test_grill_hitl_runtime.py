from __future__ import annotations

import json
import hashlib
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from T05_hitl_runtime import (
    GRILL_DOCUMENT_PUBLISH_LOCK_SCOPE,
    GRILL_QUESTION_BUDGET,
    GrillDocumentConflict,
    GrillControlDecision,
    GrillSessionAborted,
    GrillSessionError,
    GrillSessionState,
    build_turn_status_contract,
    build_grill_confirmation_preview,
    build_prefixed_sha256,
    capture_grill_context_snapshot,
    collect_grill_hitl_response,
    load_grill_session_state,
    publish_grill_document_draft,
    run_hitl_agent_loop,
    save_grill_session_state,
    update_grill_document_references,
    validate_grill_document_draft,
    validate_grill_question_file,
)
from T02_tmux_agents import TmuxMutationOutcomeUnknown
from T09_terminal_ops import use_terminal_ui
from tmux_core.runtime import hitl as hitl_runtime


def _question_markdown(index: int = 1, *, recommendation: str = "方案 B") -> str:
    return f"""# Grill Question

## 问题
第 {index} 个业务边界采用哪个方案？

## 为什么需要决定
它会改变验收口径。

## 推荐答案
{recommendation}

## 回答方式
select

## 选项
- 方案 A
- 方案 B

## 已核实事实
- 代码当前没有默认值。
"""


def _publish_snapshot_kwargs(project_dir: Path) -> dict[str, object]:
    snapshot = capture_grill_context_snapshot(project_dir)
    return {
        "context_preimage_hashes": snapshot["target_hashes"],
        "context_map_exists": snapshot["map_exists"],
        "context_map_hash": snapshot["map_hash"],
        "context_target_snapshot": snapshot["targets"],
    }


class _CaptureUI:
    def __init__(self, *, selection: str = "option_2") -> None:
        self.selection = selection
        self.select_calls: list[dict[str, object]] = []

    def message(self, *_args, **_kwargs):  # noqa: ANN002, ANN003
        return None

    def prompt_select(self, **kwargs):  # noqa: ANN003
        self.select_calls.append(kwargs)
        return self.selection

    def prompt_multiline(self, **_kwargs):  # noqa: ANN003
        return "自定义答案"

    def clear_pending_tty_input(self):
        return None

    def notify_runtime_state_changed(self):
        return None

    def notify_stage_action_changed(self, _action):  # noqa: ANN001
        return None

    def create_progress_monitor(self, **_kwargs):  # noqa: ANN003
        raise AssertionError("unused")

    def attach_external_process(self, *_args, **_kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("unused")


class _GrillWorker:
    def __init__(self, root: Path, behavior) -> None:  # noqa: ANN001
        self.runtime_dir = root / "runtime"
        self.behavior = behavior
        self.turn_calls = 0
        self.profiles: list[object] = []

    def ensure_agent_ready(self, timeout_sec=60.0):  # noqa: ANN001
        _ = timeout_sec

    def run_turn(self, *, prompt, completion_contract, grill_profile=None, **_kwargs):  # noqa: ANN003
        self.turn_calls += 1
        self.profiles.append(grill_profile)
        self.behavior(self, prompt, completion_contract)
        completion_contract.validator(completion_contract.status_path)
        return type("Result", (), {"ok": True, "clean_output": "", "exit_code": 0})()


class _CrashResumeWorker(_GrillWorker):
    def __init__(self, root: Path, behavior, *, cursor: dict[str, object]) -> None:  # noqa: ANN001
        super().__init__(root, behavior)
        self.state_path = self.runtime_dir / "worker.state.json"
        self.session_name = str(cursor.get("session_name", "requirements-agent"))
        self.pane_id = str(cursor.get("pane_id", "%1"))
        self.cursor = dict(cursor)
        self.resume_calls = 0
        self.ready_calls = 0

    def read_state(self):
        return dict(self.cursor)

    def ensure_agent_ready(self, timeout_sec=60.0):  # noqa: ANN001
        self.ready_calls += 1
        raise AssertionError("turn_in_progress recovery must inspect its contract before READY")

    def resume_completion_turn(self, **_kwargs):  # noqa: ANN003
        self.resume_calls += 1
        return None


class _BusyAfterResultWorker(_GrillWorker):
    def __init__(self, root: Path, behavior) -> None:  # noqa: ANN001
        super().__init__(root, behavior)
        self.state_path = self.runtime_dir / "worker.state.json"
        self.session_name = "requirements-agent"
        self.ready_calls = 0
        self.cursor: dict[str, object] = {
            "session_name": self.session_name,
            "agent_state": "READY",
            "turn_state": "idle",
            "current_task_runtime_status": "done",
            "state_revision": 1,
        }

    def read_state(self):
        return dict(self.cursor)

    def ensure_agent_ready(self, timeout_sec=60.0):  # noqa: ANN001
        _ = timeout_sec
        self.ready_calls += 1
        self.cursor.update(
            {
                "agent_state": "READY",
                "turn_state": "succeeded",
                "current_task_runtime_status": "done",
                "state_revision": int(self.cursor.get("state_revision", 0)) + 1,
            }
        )

    def run_turn(self, *, completion_contract, **kwargs):  # noqa: ANN003
        result = super().run_turn(completion_contract=completion_contract, **kwargs)
        self.cursor.update(
            {
                "current_turn_id": completion_contract.turn_id,
                "agent_state": "BUSY",
                "turn_state": "succeeded",
                "current_task_runtime_status": "done",
                "state_revision": int(self.cursor.get("state_revision", 0)) + 1,
            }
        )
        return result


class GrillHitlRuntimeTests(unittest.TestCase):
    def test_select_question_requires_recommendation_to_match_and_uses_it_as_default(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            question_path = Path(tmpdir) / "question.md"
            question_path.write_text(_question_markdown(), encoding="utf-8")
            question = validate_grill_question_file(question_path)
            self.assertEqual(question.recommended_answer, "方案 B")

            ui = _CaptureUI()
            with use_terminal_ui(ui):
                answer = collect_grill_hitl_response(
                    question_path,
                    hitl_round=1,
                    question_index=1,
                )

            self.assertEqual(answer, "方案 B")
            self.assertEqual(ui.select_calls[0]["default_value"], "option_2")
            self.assertEqual(ui.select_calls[0]["extra_payload"]["recommendation"], "方案 B")

            question_path.write_text(
                _question_markdown(recommendation="不存在的方案"),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(GrillSessionError, "精确匹配"):
                validate_grill_question_file(question_path)

    def test_question_requires_options_and_non_empty_verified_facts_sections(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            question_path = Path(tmpdir) / "question.md"
            missing_options = _question_markdown().replace("## 选项\n", "")
            question_path.write_text(missing_options, encoding="utf-8")
            with self.assertRaisesRegex(GrillSessionError, "缺少章节: 选项"):
                validate_grill_question_file(question_path)

            missing_facts = _question_markdown().split("## 已核实事实", 1)[0]
            question_path.write_text(missing_facts, encoding="utf-8")
            with self.assertRaisesRegex(GrillSessionError, "缺少章节: 已核实事实"):
                validate_grill_question_file(question_path)

            empty_facts = _question_markdown().replace(
                "## 已核实事实\n- 代码当前没有默认值。",
                "## 已核实事实\n",
            )
            question_path.write_text(empty_facts, encoding="utf-8")
            with self.assertRaisesRegex(GrillSessionError, "已核实事实"):
                validate_grill_question_file(question_path)

            multiline = _question_markdown().replace("select", "multiline").replace(
                "- 方案 A\n- 方案 B",
                "",
            )
            question_path.write_text(multiline, encoding="utf-8")
            question = validate_grill_question_file(question_path)
            self.assertEqual(question.answer_kind, "multiline")
            self.assertEqual(question.options, ())

    def test_grill_loop_persists_question_and_requires_final_confirmation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            output_path = root / "output.md"
            question_path = root / "question.md"
            record_path = root / "record.md"
            status_path = root / "status.json"
            session_path = root / "session.json"

            def behavior(worker, prompt, completion_contract):  # noqa: ANN001
                if worker.turn_calls == 1:
                    question_path.write_text(_question_markdown(), encoding="utf-8")
                    record_path.write_text("- 待确认边界\n", encoding="utf-8")
                else:
                    self.assertIn("方案 B", prompt)
                    output_path.write_text("# 需求澄清\n\n已采用方案 B。\n", encoding="utf-8")
                    question_path.write_text("", encoding="utf-8")
                    record_path.write_text("- 已确认采用方案 B\n", encoding="utf-8")

            worker = _GrillWorker(root, behavior)
            result = run_hitl_agent_loop(
                worker=worker,
                stage_name="requirements_clarification",
                output_path=output_path,
                question_path=question_path,
                record_path=record_path,
                stage_status_path=status_path,
                turns_root=root / "turns",
                initial_prompt_builder=lambda _context: "initial",
                hitl_prompt_builder=lambda answer, _context: f"answer::{answer}",
                label_prefix="requirements_clarification",
                turn_phase="requirements_clarification",
                human_input_provider=lambda _path, _round: "方案 B",
                requirements_mode="grill",
                grill_session_path=session_path,
                grill_turn_profile_factory=lambda question_seq: {"question_seq": question_seq},
                grill_confirmation_provider=lambda _path, _round: "confirm",
            )

            state = load_grill_session_state(session_path, requirements_mode="grill")
            self.assertEqual(result.decision.status, "completed")
            self.assertEqual(result.decision.payload["grill"]["state"], "ready_for_confirmation")
            self.assertEqual(state.state, "confirmed")
            self.assertEqual(state.question_seq, 1)
            self.assertEqual(state.accepted_answers[0]["answer"], "方案 B")
            self.assertEqual(state.pending_question_path, "")
            self.assertEqual(state.pending_question_hash, "")
            self.assertEqual(worker.turn_calls, 2)
            self.assertEqual(worker.profiles, [{"question_seq": 0}, {"question_seq": 1}])

    def test_business_hitl_waits_for_owner_ready_before_collecting_answer(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            output_path = root / "output.md"
            question_path = root / "question.md"
            record_path = root / "record.md"
            session_path = root / "session.json"
            observed_states: list[tuple[str, str]] = []

            def behavior(worker, _prompt, _contract):  # noqa: ANN001
                if worker.turn_calls == 1:
                    question_path.write_text(_question_markdown(), encoding="utf-8")
                    record_path.write_text("- 待确认边界\n", encoding="utf-8")
                else:
                    output_path.write_text("# 完成\n", encoding="utf-8")
                    question_path.write_text("", encoding="utf-8")
                    record_path.write_text("- 已确认\n", encoding="utf-8")

            worker = _BusyAfterResultWorker(root, behavior)

            def collect_answer(_path, _round):  # noqa: ANN001
                session = load_grill_session_state(session_path, requirements_mode="grill")
                observed_states.append((str(worker.read_state()["agent_state"]), session.state))
                return "方案 B"

            result = run_hitl_agent_loop(
                worker=worker,
                stage_name="requirements_clarification",
                output_path=output_path,
                question_path=question_path,
                record_path=record_path,
                stage_status_path=root / "status.json",
                turns_root=root / "turns",
                initial_prompt_builder=lambda _context: "initial",
                hitl_prompt_builder=lambda answer, _context: f"answer::{answer}",
                label_prefix="requirements_clarification",
                turn_phase="requirements_clarification",
                human_input_provider=collect_answer,
                requirements_mode="grill",
                grill_session_path=session_path,
                grill_confirmation_provider=lambda _path, _round: "confirm",
            )

            self.assertEqual(result.decision.status, "completed")
            self.assertEqual(observed_states, [("READY", "awaiting_answer")])
            self.assertGreaterEqual(worker.ready_calls, 2)

    def test_final_confirmation_revision_is_preserved_in_session_audit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            output_path = root / "output.md"
            question_path = root / "question.md"
            record_path = root / "record.md"
            confirmations = iter(
                (GrillControlDecision("revise", "把金额边界改为闭区间"), "confirm")
            )

            def behavior(_worker, prompt, _contract):  # noqa: ANN001
                if "把金额边界改为闭区间" in prompt:
                    output_path.write_text("# 修订候选\n", encoding="utf-8")
                else:
                    output_path.write_text("# 初始候选\n", encoding="utf-8")
                question_path.write_text("", encoding="utf-8")
                record_path.write_text("- 已形成共享理解\n", encoding="utf-8")

            worker = _GrillWorker(root, behavior)
            run_hitl_agent_loop(
                worker=worker,
                stage_name="requirements_clarification",
                output_path=output_path,
                question_path=question_path,
                record_path=record_path,
                stage_status_path=root / "status.json",
                turns_root=root / "turns",
                initial_prompt_builder=lambda _context: "initial",
                hitl_prompt_builder=lambda answer, _context: answer,
                label_prefix="requirements_clarification",
                turn_phase="requirements_clarification",
                requirements_mode="grill",
                grill_session_path=root / "session.json",
                grill_confirmation_provider=lambda _path, _round: next(confirmations),
            )

            state = load_grill_session_state(root / "session.json", requirements_mode="grill")
            revision = next(item for item in state.accepted_answers if item.get("kind") == "revision")
            self.assertEqual(revision["answer"], "把金额边界改为闭区间")

    def test_recovered_pending_question_is_answered_without_repeating_agent_question_turn(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            output_path = root / "output.md"
            question_path = root / "question.md"
            record_path = root / "record.md"
            status_path = root / "status.json"
            session_path = root / "session.json"
            question_path.write_text(_question_markdown(), encoding="utf-8")
            record_path.write_text("- 待确认\n", encoding="utf-8")

            # Materialize the persisted HITL status through a one-turn interrupted run.
            state = GrillSessionState(
                session_id="session-1",
                requirements_mode="grill",
                state="awaiting_answer",
                turn_seq=1,
                question_seq=1,
                pending_question_path=str(question_path.resolve()),
                pending_question_hash=build_prefixed_sha256(question_path),
                candidate_turn_id="requirements_clarification_1",
                candidate_round=1,
            )
            save_grill_session_state(session_path, state)
            status_payload = {
                "schema_version": "1.0",
                "stage": "requirements_clarification",
                "turn_id": "requirements_clarification_1",
                "hitl_round": 1,
                "status": "hitl",
                "summary": "need hitl",
                "output_path": "",
                "question_path": str(question_path.resolve()),
                "record_path": str(record_path.resolve()),
                "artifact_hashes": {
                    str(question_path.resolve()): build_prefixed_sha256(question_path),
                    str(record_path.resolve()): build_prefixed_sha256(record_path),
                },
                "written_at": "2026-07-24T12:00:00+08:00",
            }
            status_path.write_text(json.dumps(status_payload), encoding="utf-8")

            def behavior(_worker, prompt, _contract):  # noqa: ANN001
                self.assertIn("恢复后的答案", prompt)
                output_path.write_text("# 完成\n", encoding="utf-8")
                question_path.write_text("", encoding="utf-8")

            worker = _GrillWorker(root, behavior)
            result = run_hitl_agent_loop(
                worker=worker,
                stage_name="requirements_clarification",
                output_path=output_path,
                question_path=question_path,
                record_path=record_path,
                stage_status_path=status_path,
                turns_root=root / "turns",
                initial_prompt_builder=lambda _context: "must-not-repeat",
                hitl_prompt_builder=lambda answer, _context: f"answer::{answer}",
                label_prefix="requirements_clarification",
                turn_phase="requirements_clarification",
                human_input_provider=lambda _path, _round: "恢复后的答案",
                requirements_mode="grill",
                grill_session_path=session_path,
                grill_confirmation_provider=lambda _path, _round: "confirm",
            )

            self.assertEqual(result.decision.status, "completed")
            self.assertEqual(worker.turn_calls, 1)

    def test_not_started_first_turn_recovery_uses_initial_prompt_once(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            runtime = root / "runtime"
            turns = runtime / "turns"
            turn_path = turns / "requirements_clarification_1" / "turn_status.json"
            stage_path = runtime / "requirements_clarification_status.json"
            question_path = root / "question.md"
            record_path = root / "record.md"
            output_path = root / "output.md"
            session_path = root / "session.json"
            question_path.write_text("STALE QUESTION\n", encoding="utf-8")
            worker = _CrashResumeWorker(
                root,
                lambda _worker, prompt, _contract: (
                    self.assertEqual(prompt, "INITIAL-FIRST-TURN"),
                    self.assertEqual(question_path.read_text(encoding="utf-8"), ""),
                    output_path.write_text("# 完成\n", encoding="utf-8"),
                    question_path.write_text("", encoding="utf-8"),
                    record_path.write_text("- 已形成共享理解\n", encoding="utf-8"),
                ),
                cursor={
                    "current_turn_id": "requirements_clarification_1",
                    "current_turn_status_path": str(turn_path.resolve()),
                    "turn_state": "preparing",
                    "dispatch_state": "preparing",
                    "state_revision": 3,
                    "session_name": "requirements-agent",
                    "pane_id": "%1",
                },
            )
            save_grill_session_state(
                session_path,
                GrillSessionState(
                    session_id="first-turn-crash",
                    requirements_mode="grill",
                    state="turn_in_progress",
                    turn_seq=1,
                    active_worker_state_path=str(worker.state_path.resolve()),
                    active_runtime_dir=str(worker.runtime_dir.resolve()),
                    active_session_name=worker.session_name,
                    active_pane_id=worker.pane_id,
                    turn_id="requirements_clarification_1",
                    turn_label="requirements_clarification_round_1",
                    turn_status_path=str(turn_path.resolve()),
                    turn_stage_status_path=str(stage_path.resolve()),
                    turn_submission_cursor="not_started",
                ),
            )

            result = run_hitl_agent_loop(
                worker=worker,
                stage_name="requirements_clarification",
                output_path=output_path,
                question_path=question_path,
                record_path=record_path,
                stage_status_path=stage_path,
                turns_root=turns,
                initial_prompt_builder=lambda _context: "INITIAL-FIRST-TURN",
                hitl_prompt_builder=lambda answer, _context: f"WRONG::{answer}",
                label_prefix="requirements_clarification",
                turn_phase="requirements_clarification",
                requirements_mode="grill",
                grill_session_path=session_path,
                grill_confirmation_provider=lambda _path, _round: "confirm",
            )

            self.assertEqual(result.decision.status, "completed")
            self.assertEqual(worker.resume_calls, 1)
            self.assertEqual(worker.turn_calls, 1)
            self.assertEqual(worker.ready_calls, 0)

    def test_dead_worker_replacement_validates_old_contract_without_resending(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            old_runtime = root / "old-runtime"
            old_turns = old_runtime / "turns"
            old_turn_path = old_turns / "requirements_clarification_1" / "turn_status.json"
            old_stage_path = old_runtime / "requirements_clarification_status.json"
            output_path = root / "output.md"
            question_path = root / "question.md"
            record_path = root / "record.md"
            output_path.write_text("# 原 worker 已完成\n", encoding="utf-8")
            question_path.write_text("", encoding="utf-8")
            record_path.write_text("- 已形成共享理解\n", encoding="utf-8")
            contract = build_turn_status_contract(
                turn_status_path=old_turn_path,
                turn_id="requirements_clarification_1",
                turn_phase="requirements_clarification",
                stage_status_path=old_stage_path,
                stage_name="requirements_clarification",
                hitl_round=1,
                output_path=output_path,
                question_path=question_path,
                record_path=record_path,
            )
            contract.validator(contract.status_path)

            new_worker = _CrashResumeWorker(
                root,
                lambda *_args: self.fail("completed old contract must not be submitted again"),
                cursor={
                    "turn_state": "idle",
                    "dispatch_state": "",
                    "state_revision": 1,
                    "session_name": "replacement-agent",
                    "pane_id": "%2",
                },
            )

            def resume_old_contract(**kwargs):  # noqa: ANN003
                new_worker.resume_calls += 1
                self.assertEqual(kwargs["completion_contract"].status_path, old_turn_path.resolve())
                kwargs["completion_contract"].validator(old_turn_path)
                return SimpleNamespace(ok=True, clean_output="", exit_code=0)

            new_worker.resume_completion_turn = resume_old_contract  # type: ignore[method-assign]
            session_path = root / "session.json"
            save_grill_session_state(
                session_path,
                GrillSessionState(
                    session_id="dead-after-contract",
                    requirements_mode="grill",
                    state="turn_in_progress",
                    turn_seq=1,
                    active_worker_state_path=str((old_runtime / "worker.state.json").resolve()),
                    active_runtime_dir=str(old_runtime.resolve()),
                    active_session_name="dead-agent",
                    active_pane_id="%1",
                    turn_id="requirements_clarification_1",
                    turn_label="requirements_clarification_round_1",
                    turn_status_path=str(old_turn_path.resolve()),
                    turn_stage_status_path=str(old_stage_path.resolve()),
                    turn_submission_cursor="submitted",
                    turn_fresh_baseline_hashes={str(output_path.resolve()): ""},
                ),
            )

            result = run_hitl_agent_loop(
                worker=new_worker,
                stage_name="requirements_clarification",
                output_path=output_path,
                question_path=question_path,
                record_path=record_path,
                stage_status_path=old_stage_path,
                turns_root=old_turns,
                initial_prompt_builder=lambda _context: "must-not-send",
                hitl_prompt_builder=lambda _answer, _context: "must-not-send",
                label_prefix="requirements_clarification",
                turn_phase="requirements_clarification",
                requirements_mode="grill",
                grill_session_path=session_path,
                grill_confirmation_provider=lambda _path, _round: "confirm",
            )

            self.assertEqual(result.decision.status, "completed")
            self.assertEqual(new_worker.resume_calls, 1)
            self.assertEqual(new_worker.turn_calls, 0)
            self.assertEqual(new_worker.ready_calls, 0)

    def test_replacement_preserves_completed_hitl_question_before_human_display(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            old_runtime = root / "old-runtime"
            old_turns = old_runtime / "turns"
            old_turn_path = old_turns / "requirements_clarification_1" / "turn_status.json"
            old_stage_path = old_runtime / "requirements_clarification_status.json"
            output_path = root / "output.md"
            question_path = root / "question.md"
            record_path = root / "record.md"
            original_question = _question_markdown()
            question_path.write_text(original_question, encoding="utf-8")
            record_path.write_text("# 人机记录\n", encoding="utf-8")
            contract = build_turn_status_contract(
                turn_status_path=old_turn_path,
                turn_id="requirements_clarification_1",
                turn_phase="requirements_clarification",
                stage_status_path=old_stage_path,
                stage_name="requirements_clarification",
                hitl_round=1,
                output_path=output_path,
                question_path=question_path,
                record_path=record_path,
            )
            contract.validator(contract.status_path)
            new_worker = _CrashResumeWorker(
                root / "replacement",
                lambda *_args: self.fail("completed HITL contract must not be replayed"),
                cursor={
                    "turn_state": "idle",
                    "dispatch_state": "",
                    "state_revision": 1,
                    "session_name": "replacement-agent",
                    "pane_id": "%2",
                },
            )

            def resume_old_contract(**kwargs):  # noqa: ANN003
                new_worker.resume_calls += 1
                kwargs["completion_contract"].validator(old_turn_path)
                return SimpleNamespace(ok=True, clean_output="", exit_code=0)

            new_worker.resume_completion_turn = resume_old_contract  # type: ignore[method-assign]
            session_path = root / "session.json"
            save_grill_session_state(
                session_path,
                GrillSessionState(
                    session_id="dead-hitl-contract",
                    requirements_mode="grill",
                    state="turn_in_progress",
                    turn_seq=1,
                    active_worker_state_path=str((old_runtime / "worker.state.json").resolve()),
                    active_runtime_dir=str(old_runtime.resolve()),
                    turn_id="requirements_clarification_1",
                    turn_label="requirements_clarification_round_1",
                    turn_status_path=str(old_turn_path.resolve()),
                    turn_stage_status_path=str(old_stage_path.resolve()),
                    turn_submission_cursor="submitted",
                    turn_fresh_baseline_hashes={str(output_path.resolve()): ""},
                ),
            )

            class QuestionDisplayed(RuntimeError):
                pass

            display_calls = 0

            def display_once(path, _round):  # noqa: ANN001
                nonlocal display_calls
                display_calls += 1
                self.assertEqual(Path(path).read_text(encoding="utf-8"), original_question)
                raise QuestionDisplayed

            with self.assertRaises(QuestionDisplayed):
                run_hitl_agent_loop(
                    worker=new_worker,
                    stage_name="requirements_clarification",
                    output_path=output_path,
                    question_path=question_path,
                    record_path=record_path,
                    stage_status_path=old_stage_path,
                    turns_root=old_turns,
                    initial_prompt_builder=lambda _context: "must-not-send",
                    hitl_prompt_builder=lambda _answer, _context: "must-not-send",
                    label_prefix="requirements_clarification",
                    turn_phase="requirements_clarification",
                    human_input_provider=display_once,
                    requirements_mode="grill",
                    grill_session_path=session_path,
                )

            self.assertEqual(display_calls, 1)
            self.assertEqual(new_worker.resume_calls, 1)
            self.assertEqual(new_worker.turn_calls, 0)
            self.assertEqual(question_path.read_text(encoding="utf-8"), original_question)

    def test_dead_worker_finally_cannot_overwrite_replacement_cursor(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            turns = root / "turns"
            turn_path = turns / "requirements_clarification_1" / "turn_status.json"
            stage_path = root / "requirements_clarification_status.json"
            output_path = root / "output.md"
            question_path = root / "question.md"
            record_path = root / "record.md"
            old_worker = _CrashResumeWorker(
                root / "old",
                lambda *_args: self.fail("dead worker must not receive a replay"),
                cursor={
                    "current_turn_id": "requirements_clarification_1",
                    "current_turn_status_path": str(turn_path.resolve()),
                    "turn_state": "waiting_result",
                    "dispatch_state": "submitted",
                    "state_revision": 8,
                    "session_name": "dead-agent",
                    "pane_id": "%1",
                },
            )
            old_worker.ensure_agent_ready = mock.Mock()  # type: ignore[method-assign]
            old_worker.resume_completion_turn = mock.Mock(  # type: ignore[method-assign]
                side_effect=RuntimeError("tmux pane died while waiting for turn artifacts")
            )
            new_worker = _CrashResumeWorker(
                root / "new",
                lambda _worker, _prompt, _contract: (
                    output_path.write_text("# 新 worker 完成\n", encoding="utf-8"),
                    question_path.write_text("", encoding="utf-8"),
                    record_path.write_text("- 已形成共享理解\n", encoding="utf-8"),
                ),
                cursor={
                    "turn_state": "preparing",
                    "dispatch_state": "preparing",
                    "state_revision": 1,
                    "session_name": "replacement-agent",
                    "pane_id": "%2",
                },
            )
            session_path = root / "session.json"
            save_grill_session_state(
                session_path,
                GrillSessionState(
                    session_id="replace-cursor",
                    requirements_mode="grill",
                    state="turn_in_progress",
                    turn_seq=1,
                    active_worker_state_path=str(old_worker.state_path.resolve()),
                    active_runtime_dir=str(old_worker.runtime_dir.resolve()),
                    active_session_name=old_worker.session_name,
                    active_pane_id=old_worker.pane_id,
                    turn_id="requirements_clarification_1",
                    turn_label="requirements_clarification_round_1",
                    turn_status_path=str(turn_path.resolve()),
                    turn_stage_status_path=str(stage_path.resolve()),
                    turn_submission_cursor="submitted",
                    turn_fresh_baseline_hashes={str(output_path.resolve()): ""},
                ),
            )

            result = run_hitl_agent_loop(
                worker=old_worker,
                stage_name="requirements_clarification",
                output_path=output_path,
                question_path=question_path,
                record_path=record_path,
                stage_status_path=stage_path,
                turns_root=turns,
                initial_prompt_builder=lambda _context: "RECOVER-ONCE",
                hitl_prompt_builder=lambda _answer, _context: "RECOVER-ONCE",
                label_prefix="requirements_clarification",
                turn_phase="requirements_clarification",
                replace_dead_worker=lambda _worker, _error: new_worker,
                requirements_mode="grill",
                grill_session_path=session_path,
                grill_confirmation_provider=lambda _path, _round: "confirm",
            )

            restored = load_grill_session_state(
                session_path,
                requirements_mode="grill",
            )
            self.assertEqual(result.decision.status, "completed")
            self.assertEqual(old_worker.resume_calls, 0)
            self.assertEqual(new_worker.resume_calls, 1)
            self.assertEqual(new_worker.turn_calls, 1)
            self.assertEqual(
                restored.active_worker_state_path,
                str(new_worker.state_path.resolve()),
            )
            self.assertEqual(restored.active_session_name, "replacement-agent")
            self.assertEqual(restored.active_pane_id, "%2")

    def test_unknown_submission_recovery_never_calls_run_turn(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            turns = root / "runtime" / "turns"
            turn_path = turns / "requirements_clarification_1" / "turn_status.json"
            stage_path = root / "runtime" / "requirements_clarification_status.json"
            worker = _CrashResumeWorker(
                root,
                lambda *_args: self.fail("unknown submission must never be replayed"),
                cursor={
                    "current_turn_id": "requirements_clarification_1",
                    "current_turn_status_path": str(turn_path.resolve()),
                    "turn_state": "submission_unknown",
                    "dispatch_state": "submission_unknown",
                    "state_revision": 9,
                },
            )
            worker.resume_completion_turn = mock.Mock(  # type: ignore[method-assign]
                side_effect=TmuxMutationOutcomeUnknown(
                    operation="resume uncertain Grill prompt submission",
                    error="cannot prove submission",
                )
            )
            session_path = root / "session.json"
            save_grill_session_state(
                session_path,
                GrillSessionState(
                    session_id="unknown-submit",
                    requirements_mode="grill",
                    state="turn_in_progress",
                    turn_seq=1,
                    active_worker_state_path=str(worker.state_path.resolve()),
                    turn_id="requirements_clarification_1",
                    turn_label="requirements_clarification_round_1",
                    turn_status_path=str(turn_path.resolve()),
                    turn_stage_status_path=str(stage_path.resolve()),
                    turn_submission_cursor="submission_unknown",
                    turn_fresh_baseline_hashes={
                        str((root / "output.md").resolve()): ""
                    },
                ),
            )

            with self.assertRaises(TmuxMutationOutcomeUnknown):
                run_hitl_agent_loop(
                    worker=worker,
                    stage_name="requirements_clarification",
                    output_path=root / "output.md",
                    question_path=root / "question.md",
                    record_path=root / "record.md",
                    stage_status_path=stage_path,
                    turns_root=turns,
                    initial_prompt_builder=lambda _context: "must-not-send",
                    hitl_prompt_builder=lambda _answer, _context: "must-not-send",
                    label_prefix="requirements_clarification",
                    turn_phase="requirements_clarification",
                    requirements_mode="grill",
                    grill_session_path=session_path,
                )

            self.assertEqual(worker.turn_calls, 0)
            self.assertEqual(worker.ready_calls, 0)

    def test_answer_pending_survives_question_clear_without_stale_hash_failure(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            output_path = root / "output.md"
            question_path = root / "question.md"
            record_path = root / "record.md"
            session_path = root / "session.json"

            def behavior(worker, _prompt, _contract):  # noqa: ANN001
                if worker.turn_calls == 1:
                    question_path.write_text(_question_markdown(), encoding="utf-8")
                    record_path.write_text("- 待确认\n", encoding="utf-8")
                    return
                self.assertEqual(question_path.read_text(encoding="utf-8"), "")
                raise RuntimeError("simulated crash after question clear")

            worker = _GrillWorker(root, behavior)
            with self.assertRaisesRegex(RuntimeError, "simulated crash"):
                run_hitl_agent_loop(
                    worker=worker,
                    stage_name="requirements_clarification",
                    output_path=output_path,
                    question_path=question_path,
                    record_path=record_path,
                    stage_status_path=root / "status.json",
                    turns_root=root / "turns",
                    initial_prompt_builder=lambda _context: "initial",
                    hitl_prompt_builder=lambda answer, _context: answer,
                    label_prefix="requirements_clarification",
                    turn_phase="requirements_clarification",
                    human_input_provider=lambda _path, _round: "方案 B",
                    requirements_mode="grill",
                    grill_session_path=session_path,
                )

            state = load_grill_session_state(session_path, requirements_mode="grill")
            self.assertEqual(state.state, "turn_in_progress")
            self.assertEqual(state.pending_question_path, "")
            self.assertEqual(state.pending_question_hash, "")
            self.assertEqual(state.pending_answer, "方案 B")

    def test_semantic_repairs_exhausted_use_one_manual_intervention(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            output_path = root / "output.md"
            question_path = root / "question.md"
            record_path = root / "record.md"
            intervention_calls: list[int] = []

            def behavior(worker, _prompt, _contract):  # noqa: ANN001
                if worker.turn_calls <= 3:
                    question_path.write_text("## 问题\n缺少其他章节？\n", encoding="utf-8")
                    record_path.write_text("- invalid\n", encoding="utf-8")
                else:
                    output_path.write_text("# 已确认候选\n", encoding="utf-8")
                    question_path.write_text("", encoding="utf-8")

            def intervene(_worker, _error, _context, attempts):  # noqa: ANN001
                intervention_calls.append(attempts)
                question_path.write_text(_question_markdown(), encoding="utf-8")
                return "recheck_after_manual_intervention"

            worker = _GrillWorker(root, behavior)
            result = run_hitl_agent_loop(
                worker=worker,
                stage_name="requirements_clarification",
                output_path=output_path,
                question_path=question_path,
                record_path=record_path,
                stage_status_path=root / "status.json",
                turns_root=root / "turns",
                initial_prompt_builder=lambda _context: "initial",
                hitl_prompt_builder=lambda answer, _context: answer,
                label_prefix="requirements_clarification",
                turn_phase="requirements_clarification",
                human_input_provider=lambda _path, _round: "方案 B",
                requirements_mode="grill",
                grill_session_path=root / "session.json",
                grill_contract_intervention_handler=intervene,
                grill_confirmation_provider=lambda _path, _round: "confirm",
            )

            self.assertEqual(result.decision.status, "completed")
            self.assertEqual(intervention_calls, [2])
            self.assertEqual(worker.turn_calls, 4)

    def test_worker_dead_manual_intervention_recreates_once_without_second_hitl(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            output_path = root / "output.md"
            question_path = root / "question.md"
            record_path = root / "record.md"
            intervention_calls: list[int] = []
            replacements: list[object] = []

            def invalid_behavior(_worker, _prompt, _contract):  # noqa: ANN001
                question_path.write_text("## 问题\n缺少其他章节？\n", encoding="utf-8")
                record_path.write_text("- invalid\n", encoding="utf-8")

            old_worker = _CrashResumeWorker(
                root / "old",
                invalid_behavior,
                cursor={
                    "turn_state": "preparing",
                    "dispatch_state": "preparing",
                    "state_revision": 1,
                    "session_name": "old-agent",
                    "pane_id": "%1",
                },
            )
            old_worker.ensure_agent_ready = mock.Mock()  # type: ignore[method-assign]
            new_worker = _CrashResumeWorker(
                root / "new",
                lambda _worker, _prompt, _contract: (
                    output_path.write_text("# 已确认候选\n", encoding="utf-8"),
                    question_path.write_text("", encoding="utf-8"),
                ),
                cursor={
                    "turn_state": "preparing",
                    "dispatch_state": "preparing",
                    "state_revision": 1,
                    "session_name": "new-agent",
                    "pane_id": "%2",
                },
            )

            def intervene(_worker, _error, _context, attempts):  # noqa: ANN001
                intervention_calls.append(attempts)
                return "worker_dead"

            def replace(_worker, _error):  # noqa: ANN001
                replacements.append(new_worker)
                return new_worker

            session_path = root / "session.json"
            result = run_hitl_agent_loop(
                worker=old_worker,
                stage_name="requirements_clarification",
                output_path=output_path,
                question_path=question_path,
                record_path=record_path,
                stage_status_path=root / "status.json",
                turns_root=root / "turns",
                initial_prompt_builder=lambda _context: "initial",
                hitl_prompt_builder=lambda answer, _context: answer,
                label_prefix="requirements_clarification",
                turn_phase="requirements_clarification",
                replace_dead_worker=replace,
                requirements_mode="grill",
                grill_session_path=session_path,
                grill_contract_intervention_handler=intervene,
                grill_confirmation_provider=lambda _path, _round: "confirm",
            )

            state = load_grill_session_state(session_path, requirements_mode="grill")
            self.assertEqual(result.decision.status, "completed")
            self.assertEqual(intervention_calls, [2])
            self.assertEqual(len(replacements), 1)
            self.assertEqual(old_worker.turn_calls, 3)
            self.assertEqual(new_worker.resume_calls, 1)
            self.assertEqual(new_worker.turn_calls, 1)
            self.assertTrue(state.turn_manual_intervention_used)

    def test_repair_budget_and_manual_intervention_latch_survive_restart(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            turns = root / "runtime" / "turns"
            turn_path = turns / "requirements_clarification_1" / "turn_status.json"
            stage_path = root / "runtime" / "requirements_clarification_status.json"
            question_path = root / "question.md"
            record_path = root / "record.md"
            output_path = root / "output.md"
            intervention_calls: list[int] = []

            def behavior(worker, _prompt, _contract):  # noqa: ANN001
                question_path.write_text("## 问题\n缺少结构？\n", encoding="utf-8")
                record_path.write_text("- invalid\n", encoding="utf-8")

            worker = _CrashResumeWorker(
                root,
                behavior,
                cursor={
                    "current_turn_id": "requirements_clarification_1",
                    "current_turn_status_path": str(turn_path.resolve()),
                    "turn_state": "failed",
                    "dispatch_state": "",
                    "state_revision": 12,
                },
            )

            def intervene(_worker, _error, _context, attempts):  # noqa: ANN001
                intervention_calls.append(attempts)
                raise RuntimeError("simulated crash while unified intervention is pending")

            session_path = root / "session.json"
            repair_prompt = "persisted repair prompt"
            save_grill_session_state(
                session_path,
                GrillSessionState(
                    session_id="repair-resume",
                    requirements_mode="grill",
                    state="turn_in_progress",
                    turn_seq=1,
                    active_worker_state_path=str(worker.state_path.resolve()),
                    turn_id="requirements_clarification_1",
                    turn_label="requirements_clarification_round_1",
                    turn_status_path=str(turn_path.resolve()),
                    turn_stage_status_path=str(stage_path.resolve()),
                    turn_submission_cursor="repair_not_started",
                    turn_prompt_kind="grill_contract_repair",
                    turn_prompt_text=repair_prompt,
                    turn_prompt_hash=hashlib.sha256(
                        repair_prompt.encode("utf-8")
                    ).hexdigest(),
                    turn_contract_repair_attempts=2,
                    turn_manual_intervention_used=False,
                ),
            )

            common_kwargs = {
                "worker": worker,
                "stage_name": "requirements_clarification",
                "output_path": output_path,
                "question_path": question_path,
                "record_path": record_path,
                "stage_status_path": stage_path,
                "turns_root": turns,
                "initial_prompt_builder": lambda _context: "wrong initial",
                "hitl_prompt_builder": lambda answer, _context: answer,
                "label_prefix": "requirements_clarification",
                "turn_phase": "requirements_clarification",
                "human_input_provider": lambda _path, _round: "方案 B",
                "requirements_mode": "grill",
                "grill_session_path": session_path,
                "grill_confirmation_provider": lambda _path, _round: "confirm",
            }
            with self.assertRaisesRegex(RuntimeError, "simulated crash"):
                run_hitl_agent_loop(
                    **common_kwargs,
                    grill_contract_intervention_handler=intervene,
                )

            self.assertEqual(intervention_calls, [2])
            persisted = load_grill_session_state(session_path, requirements_mode="grill")
            self.assertTrue(persisted.turn_manual_intervention_used)
            with self.assertRaises(GrillSessionError):
                run_hitl_agent_loop(
                    **common_kwargs,
                    grill_contract_intervention_handler=lambda *_args: self.fail(
                        "unified intervention must not be created twice"
                    ),
                )
            self.assertEqual(intervention_calls, [2])

    def test_question_budget_prompts_at_twenty_and_does_not_fail_stage(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            output_path = root / "output.md"
            question_path = root / "question.md"
            record_path = root / "record.md"
            budget_calls: list[int] = []

            def behavior(worker, _prompt, _contract):  # noqa: ANN001
                if worker.turn_calls <= GRILL_QUESTION_BUDGET:
                    question_path.write_text(_question_markdown(worker.turn_calls), encoding="utf-8")
                    record_path.write_text(f"- question {worker.turn_calls}\n", encoding="utf-8")
                else:
                    output_path.write_text("# 候选\n", encoding="utf-8")
                    question_path.write_text("", encoding="utf-8")

            worker = _GrillWorker(root, behavior)
            result = run_hitl_agent_loop(
                worker=worker,
                stage_name="requirements_clarification",
                output_path=output_path,
                question_path=question_path,
                record_path=record_path,
                stage_status_path=root / "status.json",
                turns_root=root / "turns",
                initial_prompt_builder=lambda _context: "initial",
                hitl_prompt_builder=lambda answer, _context: answer,
                label_prefix="requirements_clarification",
                turn_phase="requirements_clarification",
                human_input_provider=lambda _path, _round: "方案 B",
                requirements_mode="grill",
                grill_session_path=root / "session.json",
                grill_budget_provider=lambda question_index: (
                    budget_calls.append(question_index) or "continue"
                ),
                grill_confirmation_provider=lambda _path, _round: "confirm",
            )

            self.assertEqual(result.decision.status, "completed")
            self.assertEqual(budget_calls, [GRILL_QUESTION_BUDGET])
            self.assertEqual(worker.turn_calls, GRILL_QUESTION_BUDGET + 1)

    def test_finalize_budget_forces_next_turn_to_form_candidate(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            question_path = root / "question.md"
            record_path = root / "record.md"
            session_path = root / "session.json"
            state = GrillSessionState(
                session_id="session-finalize",
                requirements_mode="grill",
                state="answer_pending",
                turn_seq=20,
                question_seq=20,
                pending_answer="最后一个答案",
                force_finalize=True,
            )
            save_grill_session_state(session_path, state)

            def behavior(_worker, _prompt, _contract):  # noqa: ANN001
                question_path.write_text(_question_markdown(21), encoding="utf-8")
                record_path.write_text("- 不应继续提问\n", encoding="utf-8")

            worker = _GrillWorker(root, behavior)
            with self.assertRaisesRegex(GrillSessionError, "必须形成待确认候选"):
                run_hitl_agent_loop(
                    worker=worker,
                    stage_name="requirements_clarification",
                    output_path=root / "output.md",
                    question_path=question_path,
                    record_path=record_path,
                    stage_status_path=root / "status.json",
                    turns_root=root / "turns",
                    initial_prompt_builder=lambda _context: "initial",
                    hitl_prompt_builder=lambda answer, _context: answer,
                    label_prefix="requirements_clarification",
                    turn_phase="requirements_clarification",
                    requirements_mode="grill",
                    grill_session_path=session_path,
                )

            self.assertEqual(worker.turn_calls, 3)
            persisted = load_grill_session_state(session_path, requirements_mode="grill")
            self.assertTrue(persisted.force_finalize)

    def test_domain_documents_are_previewed_then_published_under_project_lock(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            draft_path = root / "domain_drafts.json"
            output_path = root / "requirements.md"
            preview_path = root / "preview.md"
            output_path.write_text("# 需求澄清\n", encoding="utf-8")
            draft_path.write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "context_markdown": "# Demo\n\n## Language\n\n**Order**:\nA business order.\n",
                        "adrs": [
                            {
                                "title": "Use events",
                                "slug": "use-events",
                                "markdown": "# Use events\n\nUse events because ordering is asynchronous.",
                                "hard_to_reverse": True,
                                "surprising": True,
                                "real_tradeoff": True,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            preview = build_grill_confirmation_preview(
                output_path=output_path,
                preview_path=preview_path,
                domain_draft_path=draft_path,
            )
            self.assertIn("## 待发布领域文档", preview.read_text(encoding="utf-8"))
            self.assertIn("Use events", preview.read_text(encoding="utf-8"))

            lock_scopes: list[str] = []

            class _Lock:
                def __enter__(self):
                    return "lock"

                def __exit__(self, *_args):  # noqa: ANN002
                    return None

            def fake_lock(_project, scope, *, action):  # noqa: ANN001
                self.assertEqual(action, "grill.domain_documents.publish")
                lock_scopes.append(scope)
                return _Lock()

            with mock.patch(
                "tmux_core.stage_kernel.requirement_concurrency.requirement_concurrency_lock",
                side_effect=fake_lock,
            ):
                published = publish_grill_document_draft(
                    project_dir=root,
                    draft_path=draft_path,
                    **_publish_snapshot_kwargs(root),
                )

            self.assertEqual(lock_scopes, [GRILL_DOCUMENT_PUBLISH_LOCK_SCOPE])
            self.assertEqual(Path(published[0]).name, "CONTEXT.md")
            self.assertEqual(Path(published[1]).name, "0001-use-events.md")

    def test_context_body_rejects_implementation_and_route_facts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            draft = Path(tmpdir) / "domain_drafts.json"
            for forbidden in (
                "**Order**: Implemented by billing.py.",
                "**Order**: Exposed through the API route.",
                "**订单**: 由结算模块实现。",
            ):
                with self.subTest(forbidden=forbidden):
                    draft.write_text(
                        json.dumps(
                            {
                                "schema_version": "1.0",
                                "context_markdown": f"# Demo\n\n## Language\n\n{forbidden}\n",
                                "adrs": [],
                            }
                        ),
                        encoding="utf-8",
                    )
                    with self.assertRaisesRegex(GrillSessionError, "正文包含"):
                        validate_grill_document_draft(draft)

    def test_context_schema_accepts_upstream_and_chinese_glossaries(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            draft = Path(tmpdir) / "domain_drafts.json"
            accepted_contexts = (
                (
                    "# Ordering\n\nThe vocabulary used when a customer places an order.\n\n"
                    "## Language\n\n**Order**:\nA customer's request to purchase goods.\n"
                    "_Avoid_: Purchase, transaction\n\n### Payment\n\n"
                    "**Invoice**: A request for payment after delivery.\n"
                ),
                (
                    "# 结算领域\n\n本上下文描述资金交割时使用的统一业务语言。\n\n"
                    "## 领域语言\n\n### 日期口径\n\n"
                    "**结算日**：完成资金交割的业务日期。\n"
                    "_避免_：到账日、记账日\n"
                ),
            )
            for context in accepted_contexts:
                with self.subTest(context=context.splitlines()[0]):
                    draft.write_text(
                        json.dumps(
                            {
                                "schema_version": "1.0",
                                "context_markdown": context,
                                "adrs": [],
                            },
                            ensure_ascii=False,
                        ),
                        encoding="utf-8",
                    )
                    self.assertTrue(validate_grill_document_draft(draft).context_markdown)

    def test_context_schema_rejects_freeform_or_implementation_content(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            draft = Path(tmpdir) / "domain_drafts.json"
            invalid_contexts = (
                "# Demo\n\n## Language\n\nThis is loose prose.\n",
                "# Demo\n\n## Language\n\n**Order**: A business order.\n\n## Deployment\nProduction.\n",
                "# Demo\n\n## Language\n\n**Order**: See [details](docs/order.md).\n",
                "# Demo\n\n## Language\n\n**Order**: One. Two. Three.\n",
                "# Demo\n\n## Language\n\n**Order**: One.\n**order**: Two.\n",
                "# Demo\n\n## Language\n\n**Order**:\n",
                "# Demo\n\n## Language\n\n_Avoid_: Purchase.\n",
            )
            for context in invalid_contexts:
                with self.subTest(context=context):
                    draft.write_text(
                        json.dumps(
                            {
                                "schema_version": "1.0",
                                "context_markdown": context,
                                "adrs": [],
                            }
                        ),
                        encoding="utf-8",
                    )
                    with self.assertRaises(GrillSessionError):
                        validate_grill_document_draft(draft)

    def test_context_map_target_snapshot_rejects_map_drift(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "a").mkdir()
            (root / "b").mkdir()
            (root / "CONTEXT-MAP.md").write_text(
                "# Context Map\n\n- [A](a/CONTEXT.md)\n",
                encoding="utf-8",
            )
            snapshot_kwargs = _publish_snapshot_kwargs(root)
            draft = root / "domain_drafts.json"
            draft.write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "context_markdown": "# A\n\n## Language\n\n**Order**: A business order.\n",
                        "adrs": [],
                    }
                ),
                encoding="utf-8",
            )
            (root / "CONTEXT-MAP.md").write_text(
                "# Context Map\n\n- [B](b/CONTEXT.md)\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(GrillDocumentConflict, "CONTEXT-MAP"):
                publish_grill_document_draft(
                    project_dir=root,
                    draft_path=draft,
                    **snapshot_kwargs,
                )
            self.assertFalse((root / "a" / "CONTEXT.md").exists())
            self.assertFalse((root / "b" / "CONTEXT.md").exists())

    def test_publish_paths_reject_symlinks_that_escape_project(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            outside = base / "outside"
            outside.mkdir()

            root_context = base / "root-context"
            root_context.mkdir()
            (root_context / "CONTEXT.md").symlink_to(outside / "CONTEXT.md")
            with self.assertRaisesRegex(GrillSessionError, "项目外"):
                capture_grill_context_snapshot(root_context)

            root_map = base / "root-map"
            root_map.mkdir()
            external_map = outside / "CONTEXT-MAP.md"
            external_map.write_text("- [External](CONTEXT.md)\n", encoding="utf-8")
            (root_map / "CONTEXT-MAP.md").symlink_to(external_map)
            with self.assertRaisesRegex(GrillSessionError, "项目外"):
                capture_grill_context_snapshot(root_map)

            root_adr = base / "root-adr"
            root_adr.mkdir()
            snapshot_kwargs = _publish_snapshot_kwargs(root_adr)
            (root_adr / "docs").mkdir()
            (root_adr / "docs" / "adr").symlink_to(outside)
            draft = root_adr / "domain_drafts.json"
            draft.write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "context_markdown": "",
                        "adrs": [
                            {
                                "title": "Boundary",
                                "slug": "boundary",
                                "markdown": "# Boundary\n\nChoose one durable boundary.",
                                "hard_to_reverse": True,
                                "surprising": True,
                                "real_tradeoff": True,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(GrillSessionError, "项目外"):
                publish_grill_document_draft(
                    project_dir=root_adr,
                    draft_path=draft,
                    **snapshot_kwargs,
                )
            self.assertEqual(tuple(outside.glob("*.md")), (external_map,))

    def test_publish_intent_recovers_partial_adr_commit_without_renumbering(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            draft = root / "domain_drafts.json"
            draft.write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "context_markdown": "",
                        "adrs": [
                            {
                                "title": "First",
                                "slug": "first",
                                "markdown": "# First\n\nFirst durable decision.",
                                "hard_to_reverse": True,
                                "surprising": True,
                                "real_tradeoff": True,
                            },
                            {
                                "title": "Second",
                                "slug": "second",
                                "markdown": "# Second\n\nSecond durable decision.",
                                "hard_to_reverse": True,
                                "surprising": True,
                                "real_tradeoff": True,
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            snapshot_kwargs = _publish_snapshot_kwargs(root)
            intent: dict[str, object] = {}
            persisted: list[dict[str, object]] = []
            session_path = root / "session.json"
            save_grill_session_state(
                session_path,
                GrillSessionState(
                    session_id="partial-publish",
                    requirements_mode="grill-with-docs",
                    domain_draft_path=str(draft),
                ),
            )

            def persist_to_session(value: dict[str, object]) -> None:
                persisted.append(dict(value))
                state = load_grill_session_state(
                    session_path,
                    requirements_mode="grill-with-docs",
                    domain_draft_path=draft,
                )
                state.publish_intent = dict(value)
                save_grill_session_state(session_path, state)

            original_write = hitl_runtime._write_text_atomic
            write_calls = 0

            def fail_second_write(path, text):  # noqa: ANN001
                nonlocal write_calls
                write_calls += 1
                if write_calls == 2:
                    raise OSError("simulated second rename failure")
                return original_write(path, text)

            with mock.patch.object(hitl_runtime, "_write_text_atomic", side_effect=fail_second_write):
                with self.assertRaisesRegex(OSError, "second rename"):
                    publish_grill_document_draft(
                        project_dir=root,
                        draft_path=draft,
                        publish_intent=intent,
                        persist_publish_intent=persist_to_session,
                        **snapshot_kwargs,
                    )

            self.assertEqual(intent["state"], "committing")
            self.assertTrue(str(intent["files"][0]["path"]).endswith("0001-first.md"))  # type: ignore[index]
            recovered_intent = load_grill_session_state(
                session_path,
                requirements_mode="grill-with-docs",
                domain_draft_path=draft,
            ).publish_intent
            self.assertEqual(recovered_intent, intent)
            published = publish_grill_document_draft(
                project_dir=root,
                draft_path=draft,
                publish_intent=recovered_intent,
                persist_publish_intent=persist_to_session,
                **snapshot_kwargs,
            )
            self.assertEqual([Path(item).name for item in published], ["0001-first.md", "0002-second.md"])
            self.assertEqual(recovered_intent["state"], "committed")
            self.assertEqual(
                sorted(path.name for path in (root / "docs" / "adr").glob("*.md")),
                ["0001-first.md", "0002-second.md"],
            )

    def test_publish_intent_recovers_after_write_return_crash(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            draft = root / "domain_drafts.json"
            draft.write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "context_markdown": "# Demo\n\n## Language\n\n**Order**: A business order.\n",
                        "adrs": [],
                    }
                ),
                encoding="utf-8",
            )
            snapshot_kwargs = _publish_snapshot_kwargs(root)
            intent: dict[str, object] = {}
            committed_crash = False

            def crash_after_commit(value):  # noqa: ANN001
                nonlocal committed_crash
                if value.get("state") == "committed" and not committed_crash:
                    committed_crash = True
                    raise OSError("simulated return-path crash")

            with self.assertRaisesRegex(OSError, "return-path"):
                publish_grill_document_draft(
                    project_dir=root,
                    draft_path=draft,
                    publish_intent=intent,
                    persist_publish_intent=crash_after_commit,
                    **snapshot_kwargs,
                )
            self.assertEqual(intent["state"], "committed")
            context_hash = build_prefixed_sha256(root / "CONTEXT.md")

            published = publish_grill_document_draft(
                project_dir=root,
                draft_path=draft,
                publish_intent=intent,
                **snapshot_kwargs,
            )
            self.assertEqual(published, (str((root / "CONTEXT.md").resolve()),))
            self.assertEqual(build_prefixed_sha256(root / "CONTEXT.md"), context_hash)

    def test_confirmed_docs_add_idempotent_references_and_refresh_all_hashes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            output_path = root / "requirements.md"
            question_path = root / "question.md"
            record_path = root / "record.md"
            status_path = root / "status.json"
            session_path = root / "session.json"
            draft_path = root / "domain_drafts.json"
            draft_path.write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "context_markdown": "# Demo\n\n## Language\n\n**Order**: A business request.\n",
                        "adrs": [
                            {
                                "title": "Use events",
                                "slug": "use-events",
                                "markdown": "# Use events\n\nChoose asynchronous events.",
                                "hard_to_reverse": True,
                                "surprising": True,
                                "real_tradeoff": True,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            def behavior(_worker, _prompt, _contract):  # noqa: ANN001
                output_path.write_text("# 需求澄清\n\n已形成共享理解。\n", encoding="utf-8")
                question_path.write_text("", encoding="utf-8")
                record_path.write_text("- completed\n", encoding="utf-8")

            worker = _GrillWorker(root, behavior)
            result = run_hitl_agent_loop(
                worker=worker,
                stage_name="requirements_clarification",
                output_path=output_path,
                question_path=question_path,
                record_path=record_path,
                stage_status_path=status_path,
                turns_root=root / "turns",
                initial_prompt_builder=lambda _context: "initial",
                hitl_prompt_builder=lambda answer, _context: answer,
                label_prefix="requirements_clarification",
                turn_phase="requirements_clarification",
                requirements_mode="grill-with-docs",
                grill_session_path=session_path,
                grill_domain_draft_path=draft_path,
                grill_project_dir=root,
                grill_confirmation_provider=lambda _path, _round: "confirm",
            )

            output_text = output_path.read_text(encoding="utf-8")
            self.assertIn("[CONTEXT.md](CONTEXT.md)", output_text)
            self.assertIn("[0001-use-events.md](docs/adr/0001-use-events.md)", output_text)
            self.assertEqual(output_text.count("GRILL-DOMAIN-DOCUMENT-REFERENCES:BEGIN"), 1)
            state = load_grill_session_state(
                session_path,
                requirements_mode="grill-with-docs",
                domain_draft_path=draft_path,
            )
            self.assertEqual(state.state, "confirmed")
            self.assertEqual(
                state.final_artifact_hashes[str(output_path.resolve())],
                build_prefixed_sha256(output_path),
            )
            self.assertEqual(
                state.final_artifact_hashes[str(record_path.resolve())],
                build_prefixed_sha256(record_path),
            )
            self.assertTrue(all(Path(item).exists() for item in state.published_paths))
            stage_payload = json.loads(status_path.read_text(encoding="utf-8"))
            self.assertEqual(
                stage_payload["artifact_hashes"][str(output_path.resolve())],
                build_prefixed_sha256(output_path),
            )
            turn_path = root / "turns" / result.decision.turn_id / "turn_status.json"
            turn_payload = json.loads(turn_path.read_text(encoding="utf-8"))
            self.assertEqual(
                turn_payload["artifact_hashes"][str(output_path.resolve())],
                build_prefixed_sha256(output_path),
            )
            self.assertEqual(
                turn_payload["artifact_hashes"][str(status_path.resolve())],
                build_prefixed_sha256(status_path),
            )

            update_grill_document_references(
                requirements_path=output_path,
                project_dir=root,
                published_paths=state.published_paths,
            )
            self.assertEqual(
                output_path.read_text(encoding="utf-8").count(
                    "GRILL-DOMAIN-DOCUMENT-REFERENCES:BEGIN"
                ),
                1,
            )

    def test_final_confirmation_survives_publish_crash_without_second_prompt(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            output_path = root / "requirements.md"
            question_path = root / "question.md"
            record_path = root / "record.md"
            session_path = root / "session.json"
            draft_path = root / "domain_drafts.json"
            draft_path.write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "context_markdown": "# Demo\n\n## Language\n\n**Order**: A business order.\n",
                        "adrs": [],
                    }
                ),
                encoding="utf-8",
            )
            confirmation_calls = 0

            def behavior(_worker, _prompt, _contract):  # noqa: ANN001
                output_path.write_text("# 已确认候选\n", encoding="utf-8")
                question_path.write_text("", encoding="utf-8")
                record_path.write_text("- 已形成共享理解\n", encoding="utf-8")

            def confirm(*_args, **_kwargs):  # noqa: ANN002, ANN003
                nonlocal confirmation_calls
                confirmation_calls += 1
                return "confirm"

            common_kwargs = {
                "stage_name": "requirements_clarification",
                "output_path": output_path,
                "question_path": question_path,
                "record_path": record_path,
                "stage_status_path": root / "status.json",
                "turns_root": root / "turns",
                "initial_prompt_builder": lambda _context: "initial",
                "hitl_prompt_builder": lambda answer, _context: answer,
                "label_prefix": "requirements_clarification",
                "turn_phase": "requirements_clarification",
                "requirements_mode": "grill-with-docs",
                "grill_session_path": session_path,
                "grill_domain_draft_path": draft_path,
                "grill_project_dir": root,
            }
            with mock.patch.object(
                hitl_runtime,
                "publish_grill_document_draft",
                side_effect=OSError("simulated publish crash"),
            ), self.assertRaisesRegex(OSError, "publish crash"):
                run_hitl_agent_loop(
                    worker=_GrillWorker(root, behavior),
                    grill_confirmation_provider=confirm,
                    **common_kwargs,
                )

            interrupted = load_grill_session_state(
                session_path,
                requirements_mode="grill-with-docs",
                domain_draft_path=draft_path,
            )
            self.assertEqual(interrupted.state, "publishing")
            self.assertEqual(interrupted.final_confirmation["status"], "confirmed")
            self.assertEqual(confirmation_calls, 1)

            recovery_worker = _GrillWorker(
                root / "recovery",
                lambda *_args: self.fail("publishing recovery must not run a new agent turn"),
            )
            result = run_hitl_agent_loop(
                worker=recovery_worker,
                grill_confirmation_provider=lambda *_args, **_kwargs: self.fail(
                    "durable confirmation must not be requested twice"
                ),
                **common_kwargs,
            )
            self.assertEqual(result.decision.status, "completed")
            self.assertEqual(recovery_worker.turn_calls, 0)
            self.assertEqual(confirmation_calls, 1)
            self.assertEqual(
                load_grill_session_state(
                    session_path,
                    requirements_mode="grill-with-docs",
                    domain_draft_path=draft_path,
                ).state,
                "confirmed",
            )

    def test_publishing_recovery_accepts_only_system_reference_write(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            output_path = root / "requirements.md"
            question_path = root / "question.md"
            record_path = root / "record.md"
            session_path = root / "session.json"
            draft_path = root / "domain_drafts.json"
            draft_path.write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "context_markdown": "# Demo\n\n## Language\n\n**Order**: A business order.\n",
                        "adrs": [],
                    }
                ),
                encoding="utf-8",
            )

            def behavior(_worker, _prompt, _contract):  # noqa: ANN001
                output_path.write_text("# 已确认候选\n", encoding="utf-8")
                question_path.write_text("", encoding="utf-8")
                record_path.write_text("- 已形成共享理解\n", encoding="utf-8")

            original_update = hitl_runtime.update_grill_document_references

            def write_reference_then_crash(**kwargs):  # noqa: ANN003
                original_update(**kwargs)
                raise OSError("simulated refresh crash")

            common_kwargs = {
                "stage_name": "requirements_clarification",
                "output_path": output_path,
                "question_path": question_path,
                "record_path": record_path,
                "stage_status_path": root / "status.json",
                "turns_root": root / "turns",
                "initial_prompt_builder": lambda _context: "initial",
                "hitl_prompt_builder": lambda answer, _context: answer,
                "label_prefix": "requirements_clarification",
                "turn_phase": "requirements_clarification",
                "requirements_mode": "grill-with-docs",
                "grill_session_path": session_path,
                "grill_domain_draft_path": draft_path,
                "grill_project_dir": root,
            }
            with mock.patch.object(
                hitl_runtime,
                "update_grill_document_references",
                side_effect=write_reference_then_crash,
            ), self.assertRaisesRegex(OSError, "refresh crash"):
                run_hitl_agent_loop(
                    worker=_GrillWorker(root, behavior),
                    grill_confirmation_provider=lambda *_args, **_kwargs: "confirm",
                    **common_kwargs,
                )

            interrupted = load_grill_session_state(
                session_path,
                requirements_mode="grill-with-docs",
                domain_draft_path=draft_path,
            )
            self.assertEqual(interrupted.state, "publishing")
            self.assertEqual(interrupted.publish_intent["state"], "committed")
            self.assertIn("GRILL-DOMAIN-DOCUMENT-REFERENCES:BEGIN", output_path.read_text(encoding="utf-8"))

            recovery_worker = _GrillWorker(
                root / "recovery",
                lambda *_args: self.fail("reference recovery must not run a new agent turn"),
            )
            result = run_hitl_agent_loop(
                worker=recovery_worker,
                grill_confirmation_provider=lambda *_args, **_kwargs: self.fail(
                    "publishing recovery must not repeat final confirmation"
                ),
                **common_kwargs,
            )
            self.assertEqual(result.decision.status, "completed")
            self.assertEqual(recovery_worker.turn_calls, 0)
            self.assertEqual(
                output_path.read_text(encoding="utf-8").count(
                    "GRILL-DOMAIN-DOCUMENT-REFERENCES:BEGIN"
                ),
                1,
            )

    def test_confirmation_rechecks_preview_hash_before_publish(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            output_path = root / "requirements.md"
            question_path = root / "question.md"
            record_path = root / "record.md"
            draft_path = root / "domain_drafts.json"
            draft_payload = {
                "schema_version": "1.0",
                "context_markdown": "# Demo\n\n## Language\n\n**Order**: A business request.\n",
                "adrs": [],
            }
            draft_path.write_text(json.dumps(draft_payload), encoding="utf-8")

            def behavior(_worker, _prompt, _contract):  # noqa: ANN001
                output_path.write_text("# 候选\n", encoding="utf-8")
                question_path.write_text("", encoding="utf-8")
                record_path.write_text("- 已形成共享理解\n", encoding="utf-8")

            def mutate_after_preview(_preview, _round):  # noqa: ANN001
                draft_payload["context_markdown"] = (
                    "# Demo\n\n## Language\n\n**Order**: A changed business request.\n"
                )
                draft_path.write_text(json.dumps(draft_payload), encoding="utf-8")
                return "confirm"

            with self.assertRaisesRegex(GrillSessionError, "候选.*发生变化"):
                run_hitl_agent_loop(
                    worker=_GrillWorker(root, behavior),
                    stage_name="requirements_clarification",
                    output_path=output_path,
                    question_path=question_path,
                    record_path=record_path,
                    stage_status_path=root / "status.json",
                    turns_root=root / "turns",
                    initial_prompt_builder=lambda _context: "initial",
                    hitl_prompt_builder=lambda answer, _context: answer,
                    label_prefix="requirements_clarification",
                    turn_phase="requirements_clarification",
                    requirements_mode="grill-with-docs",
                    grill_session_path=root / "session.json",
                    grill_domain_draft_path=draft_path,
                    grill_project_dir=root,
                    grill_confirmation_provider=mutate_after_preview,
                )
            self.assertFalse((root / "CONTEXT.md").exists())

    def test_legacy_docs_session_must_rebuild_and_reconfirm_before_publish(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            output_path = root / "requirements.md"
            question_path = root / "question.md"
            record_path = root / "record.md"
            session_path = root / "session.json"
            draft_path = root / "domain_drafts.json"
            draft_path.write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "context_markdown": "# Demo\n\n## Language\n\n**Order**: A business order.\n",
                        "adrs": [],
                    }
                ),
                encoding="utf-8",
            )
            # This emulates a pre-snapshot session: loading it must not silently
            # invent target provenance for the old human confirmation.
            save_grill_session_state(
                session_path,
                GrillSessionState(
                    session_id="legacy-docs",
                    requirements_mode="grill-with-docs",
                    domain_draft_path=str(draft_path),
                ),
            )
            confirmations: list[int] = []
            conflicts: list[str] = []

            def behavior(worker, _prompt, _contract):  # noqa: ANN001
                output_path.write_text(f"# 候选 {worker.turn_calls}\n", encoding="utf-8")
                question_path.write_text("", encoding="utf-8")
                record_path.write_text(f"- 候选 {worker.turn_calls}\n", encoding="utf-8")

            result = run_hitl_agent_loop(
                worker=_GrillWorker(root, behavior),
                stage_name="requirements_clarification",
                output_path=output_path,
                question_path=question_path,
                record_path=record_path,
                stage_status_path=root / "status.json",
                turns_root=root / "turns",
                initial_prompt_builder=lambda _context: "initial",
                hitl_prompt_builder=lambda answer, _context: answer,
                label_prefix="requirements_clarification",
                turn_phase="requirements_clarification",
                requirements_mode="grill-with-docs",
                grill_session_path=session_path,
                grill_domain_draft_path=draft_path,
                grill_project_dir=root,
                grill_confirmation_provider=lambda _path, question_index: (
                    confirmations.append(question_index) or "confirm"
                ),
                grill_document_conflict_provider=lambda reason: (
                    conflicts.append(reason) or "rebuild"
                ),
            )

            self.assertEqual(result.decision.status, "completed")
            self.assertEqual(len(confirmations), 2)
            self.assertEqual(len(conflicts), 1)
            self.assertIn("缺少 CONTEXT-MAP/目标启动快照", conflicts[0])
            state = load_grill_session_state(
                session_path,
                requirements_mode="grill-with-docs",
                domain_draft_path=draft_path,
            )
            self.assertIsNotNone(state.context_map_exists)
            self.assertEqual(state.state, "confirmed")
            self.assertTrue((root / "CONTEXT.md").exists())

    def test_completed_grill_candidate_requires_nonempty_human_record(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            output_path = root / "output.md"
            question_path = root / "question.md"
            confirmation_calls = 0

            def behavior(_worker, _prompt, _contract):  # noqa: ANN001
                output_path.write_text("# 候选\n", encoding="utf-8")
                question_path.write_text("", encoding="utf-8")

            def confirm(*_args, **_kwargs):  # noqa: ANN002, ANN003
                nonlocal confirmation_calls
                confirmation_calls += 1
                return "confirm"

            with self.assertRaisesRegex(GrillSessionError, "非空的人机交互记录"):
                run_hitl_agent_loop(
                    worker=_GrillWorker(root, behavior),
                    stage_name="requirements_clarification",
                    output_path=output_path,
                    question_path=question_path,
                    record_path=root / "record.md",
                    stage_status_path=root / "status.json",
                    turns_root=root / "turns",
                    initial_prompt_builder=lambda _context: "initial",
                    hitl_prompt_builder=lambda answer, _context: answer,
                    label_prefix="requirements_clarification",
                    turn_phase="requirements_clarification",
                    requirements_mode="grill",
                    grill_session_path=root / "session.json",
                    grill_confirmation_provider=confirm,
                    max_contract_repair_attempts=0,
                )
            self.assertEqual(confirmation_calls, 0)

    def test_confirmation_hash_binds_human_record(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            output_path = root / "output.md"
            question_path = root / "question.md"
            record_path = root / "record.md"

            def behavior(_worker, _prompt, _contract):  # noqa: ANN001
                output_path.write_text("# 候选\n", encoding="utf-8")
                question_path.write_text("", encoding="utf-8")
                record_path.write_text("- 原始确认记录\n", encoding="utf-8")

            def mutate_record(_preview, _round):  # noqa: ANN001
                record_path.write_text("- 未经确认的变更\n", encoding="utf-8")
                return "confirm"

            with self.assertRaisesRegex(GrillSessionError, "候选.*发生变化"):
                run_hitl_agent_loop(
                    worker=_GrillWorker(root, behavior),
                    stage_name="requirements_clarification",
                    output_path=output_path,
                    question_path=question_path,
                    record_path=record_path,
                    stage_status_path=root / "status.json",
                    turns_root=root / "turns",
                    initial_prompt_builder=lambda _context: "initial",
                    hitl_prompt_builder=lambda answer, _context: answer,
                    label_prefix="requirements_clarification",
                    turn_phase="requirements_clarification",
                    requirements_mode="grill",
                    grill_session_path=root / "session.json",
                    grill_confirmation_provider=mutate_record,
                )

    def test_domain_publish_lock_conflict_never_writes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            draft_path = root / "domain_drafts.json"
            draft_path.write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "context_markdown": "# Demo\n\n## Language\n\n**Order**:\nA business order.\n",
                        "adrs": [],
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch(
                "tmux_core.stage_kernel.requirement_concurrency.requirement_concurrency_lock",
                side_effect=RuntimeError("busy"),
            ), self.assertRaises(GrillDocumentConflict):
                publish_grill_document_draft(
                    project_dir=root,
                    draft_path=draft_path,
                    **_publish_snapshot_kwargs(root),
                )
            self.assertFalse((root / "CONTEXT.md").exists())

    def test_project_publish_lock_prevents_concurrent_adr_number_allocation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            draft_path = root / "domain_drafts.json"
            draft_path.write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "context_markdown": "",
                        "adrs": [
                            {
                                "title": "Concurrent decision",
                                "slug": "concurrent-decision",
                                "markdown": "# Concurrent decision\n\nChoose one durable boundary.",
                                "hard_to_reverse": True,
                                "surprising": True,
                                "real_tradeoff": True,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            entered_write = threading.Event()
            release_write = threading.Event()
            publisher_errors: list[BaseException] = []
            original_write = hitl_runtime._write_text_atomic

            def blocking_write(path, text):  # noqa: ANN001
                if threading.current_thread().name == "grill-publisher-1":
                    entered_write.set()
                    if not release_write.wait(timeout=5):
                        raise TimeoutError("test did not release first publisher")
                return original_write(path, text)

            def first_publish() -> None:
                try:
                    publish_grill_document_draft(
                        project_dir=root,
                        draft_path=draft_path,
                        **_publish_snapshot_kwargs(root),
                    )
                except BaseException as error:  # noqa: BLE001
                    publisher_errors.append(error)

            with mock.patch.object(hitl_runtime, "_write_text_atomic", side_effect=blocking_write):
                publisher = threading.Thread(target=first_publish, name="grill-publisher-1")
                publisher.start()
                self.assertTrue(entered_write.wait(timeout=5))
                try:
                    with self.assertRaises(GrillDocumentConflict):
                        publish_grill_document_draft(
                            project_dir=root,
                            draft_path=draft_path,
                            **_publish_snapshot_kwargs(root),
                        )
                finally:
                    release_write.set()
                    publisher.join(timeout=5)

            self.assertFalse(publisher.is_alive())
            self.assertEqual(publisher_errors, [])
            self.assertEqual(
                [item.name for item in (root / "docs" / "adr").glob("*.md")],
                ["0001-concurrent-decision.md"],
            )

    def test_rejecting_final_docs_confirmation_does_not_publish(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            output_path = root / "output.md"
            question_path = root / "question.md"
            record_path = root / "record.md"
            draft_path = root / "domain_drafts.json"
            draft_path.write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "context_markdown": "# Demo\n\n## Language\n\n**Order**:\nA business order.\n",
                        "adrs": [],
                    }
                ),
                encoding="utf-8",
            )

            def behavior(_worker, _prompt, _contract):  # noqa: ANN001
                output_path.write_text("# 候选\n", encoding="utf-8")
                question_path.write_text("", encoding="utf-8")
                record_path.write_text("- 已形成共享理解\n", encoding="utf-8")

            worker = _GrillWorker(root, behavior)
            with self.assertRaises(GrillSessionAborted):
                run_hitl_agent_loop(
                    worker=worker,
                    stage_name="requirements_clarification",
                    output_path=output_path,
                    question_path=question_path,
                    record_path=record_path,
                    stage_status_path=root / "status.json",
                    turns_root=root / "turns",
                    initial_prompt_builder=lambda _context: "initial",
                    hitl_prompt_builder=lambda answer, _context: answer,
                    label_prefix="requirements_clarification",
                    turn_phase="requirements_clarification",
                    requirements_mode="grill-with-docs",
                    grill_session_path=root / "session.json",
                    grill_domain_draft_path=draft_path,
                    grill_project_dir=root,
                    grill_confirmation_provider=lambda preview, _round: (
                        self.assertIn(
                            "## 待发布领域文档",
                            Path(preview).read_text(encoding="utf-8"),
                        )
                        or "abort"
                    ),
                )
            self.assertFalse((root / "CONTEXT.md").exists())


if __name__ == "__main__":
    unittest.main()
