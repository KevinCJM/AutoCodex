from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from T05_hitl_runtime import (
    HITL_STATUS_COMPLETED,
    HITL_STATUS_ERROR,
    HITL_STATUS_HITL,
    HITL_STATUS_SCHEMA_VERSION,
    HitlPromptContext,
    build_prefixed_sha256,
    build_turn_status_contract,
    collect_terminal_hitl_response,
    run_hitl_agent_loop,
    validate_hitl_status_file,
)
from T09_terminal_ops import BridgeTerminalUI


def _write_stage_status(
    status_path: Path,
    *,
    stage: str,
    turn_id: str,
    hitl_round: int,
    status: str,
    output_path: Path | None,
    question_path: Path | None,
    record_path: Path | None,
    summary: str,
) -> None:
    payload = {
        "schema_version": HITL_STATUS_SCHEMA_VERSION,
        "stage": stage,
        "turn_id": turn_id,
        "hitl_round": hitl_round,
        "status": status,
        "summary": summary,
        "output_path": str(output_path.resolve()) if output_path else "",
        "question_path": str(question_path.resolve()) if question_path else "",
        "record_path": str(record_path.resolve()) if record_path else "",
        "artifact_hashes": {},
        "written_at": "2026-04-13T12:00:00+08:00",
    }
    artifact_hashes: dict[str, str] = {}
    for candidate in (output_path, question_path, record_path):
        if candidate is None:
            continue
        artifact_hashes[str(candidate.resolve())] = build_prefixed_sha256(candidate)
    payload["artifact_hashes"] = artifact_hashes
    status_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_turn_status(
    turn_status_path: Path,
    *,
    turn_id: str,
    phase: str,
    stage_status_path: Path,
    artifact_paths: list[Path],
) -> None:
    artifacts: dict[str, str] = {"stage_status": str(stage_status_path.resolve())}
    artifact_hashes = {str(stage_status_path.resolve()): build_prefixed_sha256(stage_status_path)}
    for index, artifact_path in enumerate(artifact_paths, start=1):
        artifacts[f"artifact_{index}"] = str(artifact_path.resolve())
        artifact_hashes[str(artifact_path.resolve())] = build_prefixed_sha256(artifact_path)
    turn_status_path.parent.mkdir(parents=True, exist_ok=True)
    turn_status_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "turn_id": turn_id,
                "phase": phase,
                "status": "done",
                "artifacts": artifacts,
                "artifact_hashes": artifact_hashes,
                "written_at": "2026-04-13T12:00:00+08:00",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


class _FakeWorker:
    def __init__(self, behavior, *, runtime_dir: Path):
        self.behavior = behavior
        self.runtime_dir = runtime_dir
        self.ensure_ready_calls = 0
        self.turn_calls = 0

    def ensure_agent_ready(self, timeout_sec=60.0):  # noqa: ANN001
        self.ensure_ready_calls += 1
        return None

    def run_turn(self, *, label, prompt, completion_contract, timeout_sec):  # noqa: ANN001
        self.turn_calls += 1
        self.behavior(self, label, prompt, completion_contract, timeout_sec)
        completion_contract.validator(completion_contract.status_path)
        return type("CommandResult", (), {"ok": True, "clean_output": "", "exit_code": 0})()


class HitlRuntimeTests(unittest.TestCase):
    def test_collect_terminal_hitl_response_skips_manual_log_echo_under_bridge_ui(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            question_path = Path(tmpdir) / "question.md"
            question_path.write_text("- 缺少结算规则\n", encoding="utf-8")
            bridge_ui = BridgeTerminalUI(
                emit_event=lambda *_args, **_kwargs: None,
                request_prompt=lambda _request: {"value": "人工回复"},
            )
            with patch("T05_hitl_runtime.get_terminal_ui", return_value=bridge_ui), patch(
                "T05_hitl_runtime.message"
            ) as message_mock:
                reply = collect_terminal_hitl_response(question_path, hitl_round=1)

        self.assertEqual(reply, "人工回复")
        message_mock.assert_not_called()

    def test_validate_hitl_status_file_accepts_completed_payload(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            output_path = root / "output.md"
            status_path = root / "status.json"
            output_path.write_text("final body\n", encoding="utf-8")
            _write_stage_status(
                status_path,
                stage="demo_stage",
                turn_id="turn_1",
                hitl_round=1,
                status=HITL_STATUS_COMPLETED,
                output_path=output_path,
                question_path=None,
                record_path=None,
                summary="done",
            )
            decision = validate_hitl_status_file(
                status_path,
                expected_stage="demo_stage",
                expected_turn_id="turn_1",
                expected_hitl_round=1,
                expected_output_path=output_path,
                expected_question_path=root / "question.md",
                expected_record_path=root / "record.md",
            )
        self.assertEqual(decision.status, HITL_STATUS_COMPLETED)

    def test_build_turn_status_contract_validates_stage_artifacts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            output_path = root / "output.md"
            stage_status_path = root / "status.json"
            turn_status_path = root / "turns" / "turn_1" / "turn_status.json"
            output_path.write_text("result\n", encoding="utf-8")
            _write_stage_status(
                stage_status_path,
                stage="demo_stage",
                turn_id="turn_1",
                hitl_round=1,
                status=HITL_STATUS_COMPLETED,
                output_path=output_path,
                question_path=None,
                record_path=None,
                summary="done",
            )
            _write_turn_status(
                turn_status_path,
                turn_id="turn_1",
                phase="demo_phase",
                stage_status_path=stage_status_path,
                artifact_paths=[output_path],
            )
            contract = build_turn_status_contract(
                turn_status_path=turn_status_path,
                turn_id="turn_1",
                turn_phase="demo_phase",
                stage_status_path=stage_status_path,
            )
            result = contract.validator(turn_status_path)
        self.assertEqual(result.status_path, str(turn_status_path.resolve()))

    def test_build_turn_status_contract_materializes_missing_status_files_from_output(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            output_path = root / "output.md"
            stage_status_path = root / "status.json"
            turn_status_path = root / "turns" / "turn_1" / "turn_status.json"
            output_path.write_text("result\n", encoding="utf-8")

            contract = build_turn_status_contract(
                turn_status_path=turn_status_path,
                turn_id="turn_1",
                turn_phase="demo_phase",
                stage_status_path=stage_status_path,
                stage_name="demo_stage",
                hitl_round=1,
                output_path=output_path,
                question_path=root / "question.md",
                record_path=root / "record.md",
            )
            result = contract.validator(turn_status_path)
            decision = validate_hitl_status_file(
                stage_status_path,
                expected_stage="demo_stage",
                expected_turn_id="turn_1",
                expected_hitl_round=1,
                expected_output_path=output_path,
                expected_question_path=root / "question.md",
                expected_record_path=root / "record.md",
            )
            self.assertTrue(stage_status_path.exists())
            self.assertTrue(turn_status_path.exists())
            self.assertEqual(decision.status, HITL_STATUS_COMPLETED)
            self.assertEqual(result.status_path, str(turn_status_path.resolve()))

    def test_build_turn_status_contract_prefers_hitl_when_question_exists_and_record_file_is_empty(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            output_path = root / "output.md"
            question_path = root / "question.md"
            record_path = root / "record.md"
            stage_status_path = root / "status.json"
            turn_status_path = root / "turns" / "turn_1" / "turn_status.json"
            output_path.write_text("旧的需求澄清\n", encoding="utf-8")
            question_path.write_text("- [阻断] 需要补充平台范围\n", encoding="utf-8")
            record_path.write_text("", encoding="utf-8")

            contract = build_turn_status_contract(
                turn_status_path=turn_status_path,
                turn_id="turn_1",
                turn_phase="demo_phase",
                stage_status_path=stage_status_path,
                stage_name="demo_stage",
                hitl_round=1,
                output_path=output_path,
                question_path=question_path,
                record_path=record_path,
            )
            contract.validator(turn_status_path)
            decision = validate_hitl_status_file(
                stage_status_path,
                expected_stage="demo_stage",
                expected_turn_id="turn_1",
                expected_hitl_round=1,
                expected_output_path=output_path,
                expected_question_path=question_path,
                expected_record_path=record_path,
            )

        self.assertEqual(decision.status, HITL_STATUS_HITL)
        self.assertEqual(decision.question_path, str(question_path.resolve()))
        self.assertEqual(decision.record_path, str(record_path.resolve()))

    def test_build_turn_status_contract_can_require_fresh_completed_artifact(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            output_path = root / "output.md"
            question_path = root / "question.md"
            record_path = root / "record.md"
            stage_status_path = root / "status.json"
            turn_status_path = root / "turns" / "turn_2" / "turn_status.json"
            output_path.write_text("已有需求澄清\n", encoding="utf-8")
            question_path.write_text("", encoding="utf-8")
            record_path.write_text("- [待确认] 平台边界\n", encoding="utf-8")

            contract = build_turn_status_contract(
                turn_status_path=turn_status_path,
                turn_id="turn_2",
                turn_phase="demo_phase",
                stage_status_path=stage_status_path,
                stage_name="demo_stage",
                hitl_round=2,
                output_path=output_path,
                question_path=question_path,
                record_path=record_path,
                fresh_completion_paths=(output_path, record_path),
                baseline_fresh_hashes={
                    str(output_path.resolve()): build_prefixed_sha256(output_path),
                    str(record_path.resolve()): build_prefixed_sha256(record_path),
                },
            )
            _write_stage_status(
                stage_status_path,
                stage="demo_stage",
                turn_id="turn_2",
                hitl_round=2,
                status=HITL_STATUS_COMPLETED,
                output_path=output_path,
                question_path=None,
                record_path=record_path,
                summary="done",
            )
            _write_turn_status(
                turn_status_path,
                turn_id="turn_2",
                phase="demo_phase",
                stage_status_path=stage_status_path,
                artifact_paths=[output_path, record_path],
            )

            with self.assertRaisesRegex(ValueError, "completed 状态未生成新的阶段产物"):
                contract.validator(turn_status_path)

            output_path.write_text("更新后的需求澄清\n", encoding="utf-8")
            _write_stage_status(
                stage_status_path,
                stage="demo_stage",
                turn_id="turn_2",
                hitl_round=2,
                status=HITL_STATUS_COMPLETED,
                output_path=output_path,
                question_path=None,
                record_path=record_path,
                summary="done",
            )
            _write_turn_status(
                turn_status_path,
                turn_id="turn_2",
                phase="demo_phase",
                stage_status_path=stage_status_path,
                artifact_paths=[output_path, record_path],
            )

            result = contract.validator(turn_status_path)

        self.assertEqual(result.status_path, str(turn_status_path.resolve()))

    def test_run_hitl_agent_loop_skips_fresh_completion_requirement_before_start_round(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            output_path = root / "output.md"
            question_path = root / "question.md"
            record_path = root / "record.md"
            stage_status_path = root / "status.json"
            turns_root = root / "turns"
            output_path.write_text("已有需求澄清\n", encoding="utf-8")
            record_path.write_text("- [已确认] 平台边界\n", encoding="utf-8")

            def behavior(worker, label, prompt, completion_contract, timeout_sec):  # noqa: ANN001
                return None

            worker = _FakeWorker(behavior, runtime_dir=root / "runtime")
            result = run_hitl_agent_loop(
                worker=worker,
                stage_name="demo_stage",
                output_path=output_path,
                question_path=question_path,
                record_path=record_path,
                stage_status_path=stage_status_path,
                turns_root=turns_root,
                initial_prompt_builder=lambda context: f"initial::{context.turn_id}",
                hitl_prompt_builder=lambda human_msg, context: f"followup::{human_msg}::{context.turn_id}",
                label_prefix="demo_turn",
                turn_phase="demo_phase",
                fresh_completion_paths=(output_path, record_path),
                fresh_completion_start_round=2,
            )

        self.assertEqual(result.rounds_used, 1)
        self.assertEqual(result.decision.status, HITL_STATUS_COMPLETED)

    def test_run_hitl_agent_loop_cycles_until_completed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            output_path = root / "output.md"
            question_path = root / "question.md"
            record_path = root / "record.md"
            stage_status_path = root / "status.json"
            turns_root = root / "turns"

            def behavior(worker, label, prompt, completion_contract, timeout_sec):  # noqa: ANN001
                stage_status = stage_status_path
                if worker.turn_calls == 1:
                    question_path.write_text("- [阻断] 需要补充业务边界\n", encoding="utf-8")
                    record_path.write_text("- [待确认] 业务边界\n", encoding="utf-8")
                    _write_stage_status(
                        stage_status,
                        stage="demo_stage",
                        turn_id=completion_contract.turn_id,
                        hitl_round=1,
                        status=HITL_STATUS_HITL,
                        output_path=None,
                        question_path=question_path,
                        record_path=record_path,
                        summary="need hitl",
                    )
                    _write_turn_status(
                        completion_contract.status_path,
                        turn_id=completion_contract.turn_id,
                        phase="demo_phase",
                        stage_status_path=stage_status,
                        artifact_paths=[question_path, record_path],
                    )
                else:
                    self.assertIn("补充说明", prompt)
                    output_path.write_text("最终正文\n", encoding="utf-8")
                    record_path.write_text("- [已确认] 使用补充说明\n", encoding="utf-8")
                    _write_stage_status(
                        stage_status,
                        stage="demo_stage",
                        turn_id=completion_contract.turn_id,
                        hitl_round=2,
                        status=HITL_STATUS_COMPLETED,
                        output_path=output_path,
                        question_path=None,
                        record_path=record_path,
                        summary="done",
                    )
                    _write_turn_status(
                        completion_contract.status_path,
                        turn_id=completion_contract.turn_id,
                        phase="demo_phase",
                        stage_status_path=stage_status,
                        artifact_paths=[output_path, record_path],
                    )

            worker = _FakeWorker(behavior, runtime_dir=root / "runtime")
            lifecycle_events: list[str] = []
            result = run_hitl_agent_loop(
                worker=worker,
                stage_name="demo_stage",
                output_path=output_path,
                question_path=question_path,
                record_path=record_path,
                stage_status_path=stage_status_path,
                turns_root=turns_root,
                initial_prompt_builder=lambda context: f"initial::{context.turn_id}",
                hitl_prompt_builder=lambda human_msg, context: f"followup::{human_msg}::{context.turn_id}",
                label_prefix="demo_turn",
                turn_phase="demo_phase",
                human_input_provider=lambda path, round_index: "补充说明",
                on_agent_turn_started=lambda context, live_worker: lifecycle_events.append(f"start:{context.turn_id}"),
                on_agent_turn_finished=lambda context, live_worker: lifecycle_events.append(f"stop:{context.turn_id}"),
            )
        self.assertEqual(result.rounds_used, 2)
        self.assertEqual(result.decision.status, HITL_STATUS_COMPLETED)
        self.assertEqual(result.human_responses, ("补充说明",))
        self.assertEqual(
            lifecycle_events,
            [
                "start:demo_turn_1",
                "stop:demo_turn_1",
                "start:demo_turn_2",
                "stop:demo_turn_2",
            ],
        )

    def test_run_hitl_agent_loop_notifies_worker_starting_before_ready(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            output_path = root / "output.md"
            question_path = root / "question.md"
            record_path = root / "record.md"
            stage_status_path = root / "status.json"
            turns_root = root / "turns"

            def behavior(worker, label, prompt, completion_contract, timeout_sec):  # noqa: ANN001
                output_path.write_text("最终正文\n", encoding="utf-8")
                record_path.write_text("- [已确认] 启动完成\n", encoding="utf-8")
                _write_stage_status(
                    stage_status_path,
                    stage="demo_stage",
                    turn_id=completion_contract.turn_id,
                    hitl_round=1,
                    status=HITL_STATUS_COMPLETED,
                    output_path=output_path,
                    question_path=None,
                    record_path=record_path,
                    summary="done",
                )
                _write_turn_status(
                    completion_contract.status_path,
                    turn_id=completion_contract.turn_id,
                    phase="demo_phase",
                    stage_status_path=stage_status_path,
                    artifact_paths=[output_path, record_path],
                )

            worker = _FakeWorker(behavior, runtime_dir=root / "runtime")
            lifecycle_events: list[str] = []
            result = run_hitl_agent_loop(
                worker=worker,
                stage_name="demo_stage",
                output_path=output_path,
                question_path=question_path,
                record_path=record_path,
                stage_status_path=stage_status_path,
                turns_root=turns_root,
                initial_prompt_builder=lambda context: f"initial::{context.turn_id}",
                hitl_prompt_builder=lambda human_msg, context: f"followup::{human_msg}::{context.turn_id}",
                label_prefix="demo_turn",
                turn_phase="demo_phase",
                on_worker_starting=lambda live_worker: lifecycle_events.append("worker_starting"),
                on_worker_started=lambda live_worker: lifecycle_events.append(f"worker_started:{live_worker.ensure_ready_calls}"),
                on_agent_turn_started=lambda context, live_worker: lifecycle_events.append(f"turn_started:{context.turn_id}"),
                on_agent_turn_finished=lambda context, live_worker: lifecycle_events.append(f"turn_finished:{context.turn_id}"),
            )
        self.assertEqual(result.decision.status, HITL_STATUS_COMPLETED)
        self.assertEqual(worker.ensure_ready_calls, 1)
        self.assertEqual(
            lifecycle_events,
            [
                "worker_starting",
                "worker_started:1",
                "turn_started:demo_turn_1",
                "turn_finished:demo_turn_1",
            ],
        )

    def test_run_hitl_agent_loop_notifies_runtime_state_change_after_ready_before_worker_started(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            output_path = root / "output.md"
            question_path = root / "question.md"
            record_path = root / "record.md"
            stage_status_path = root / "status.json"
            turns_root = root / "turns"

            def behavior(worker, label, prompt, completion_contract, timeout_sec):  # noqa: ANN001
                output_path.write_text("最终正文\n", encoding="utf-8")
                record_path.write_text("- [已确认] 启动完成\n", encoding="utf-8")
                _write_stage_status(
                    stage_status_path,
                    stage="demo_stage",
                    turn_id=completion_contract.turn_id,
                    hitl_round=1,
                    status=HITL_STATUS_COMPLETED,
                    output_path=output_path,
                    question_path=None,
                    record_path=record_path,
                    summary="done",
                )
                _write_turn_status(
                    completion_contract.status_path,
                    turn_id=completion_contract.turn_id,
                    phase="demo_phase",
                    stage_status_path=stage_status_path,
                    artifact_paths=[output_path, record_path],
                )

            worker = _FakeWorker(behavior, runtime_dir=root / "runtime")
            lifecycle_events: list[str] = []
            with unittest.mock.patch("T05_hitl_runtime.notify_runtime_state_changed", side_effect=lambda: lifecycle_events.append("runtime_state_changed")):
                result = run_hitl_agent_loop(
                    worker=worker,
                    stage_name="demo_stage",
                    output_path=output_path,
                    question_path=question_path,
                    record_path=record_path,
                    stage_status_path=stage_status_path,
                    turns_root=turns_root,
                    initial_prompt_builder=lambda context: f"initial::{context.turn_id}",
                    hitl_prompt_builder=lambda human_msg, context: f"followup::{human_msg}::{context.turn_id}",
                    label_prefix="demo_turn",
                    turn_phase="demo_phase",
                    on_worker_starting=lambda live_worker: lifecycle_events.append("worker_starting"),
                    on_worker_started=lambda live_worker: lifecycle_events.append("worker_started"),
                )
        self.assertEqual(result.decision.status, HITL_STATUS_COMPLETED)
        self.assertEqual(
            lifecycle_events,
            [
                "worker_starting",
                "runtime_state_changed",
                "worker_started",
            ],
        )

    def test_run_hitl_agent_loop_retries_same_round_after_dead_worker_replacement(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            output_path = root / "output.md"
            question_path = root / "question.md"
            record_path = root / "record.md"
            stage_status_path = root / "status.json"
            turns_root = root / "turns"

            def dead_behavior(worker, label, prompt, completion_contract, timeout_sec):  # noqa: ANN001
                raise RuntimeError("tmux pane died while waiting for turn artifacts")

            def replacement_behavior(worker, label, prompt, completion_contract, timeout_sec):  # noqa: ANN001
                output_path.write_text("最终正文\n", encoding="utf-8")
                record_path.write_text("- [已确认] replacement\n", encoding="utf-8")
                question_path.write_text("", encoding="utf-8")
                completion_contract.validator(completion_contract.status_path)

            dead_worker = _FakeWorker(dead_behavior, runtime_dir=root / "runtime-dead")
            replacement_worker = _FakeWorker(replacement_behavior, runtime_dir=root / "runtime-new")
            replacement_calls: list[tuple[object, str]] = []

            result = run_hitl_agent_loop(
                worker=dead_worker,
                stage_name="demo_stage",
                output_path=output_path,
                question_path=question_path,
                record_path=record_path,
                stage_status_path=stage_status_path,
                turns_root=turns_root,
                initial_prompt_builder=lambda context: f"initial::{context.turn_id}",
                hitl_prompt_builder=lambda human_msg, context: f"followup::{human_msg}::{context.turn_id}",
                label_prefix="demo_turn",
                turn_phase="demo_phase",
                replace_dead_worker=lambda current_worker, error: replacement_calls.append((current_worker, str(error))) or replacement_worker,
            )

        self.assertEqual(result.rounds_used, 1)
        self.assertEqual(result.decision.status, HITL_STATUS_COMPLETED)
        self.assertEqual(dead_worker.turn_calls, 1)
        self.assertEqual(replacement_worker.turn_calls, 1)
        self.assertEqual(len(replacement_calls), 1)
        self.assertIs(replacement_calls[0][0], dead_worker)

    def test_run_hitl_agent_loop_retries_same_round_when_run_turn_returns_dead_output(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            output_path = root / "output.md"
            question_path = root / "question.md"
            record_path = root / "record.md"
            stage_status_path = root / "status.json"
            turns_root = root / "turns"

            class _OkFalseWorker(_FakeWorker):
                def run_turn(self, *, label, prompt, completion_contract, timeout_sec):  # noqa: ANN001
                    self.turn_calls += 1
                    self.behavior(self, label, prompt, completion_contract, timeout_sec)
                    return type(
                        "CommandResult",
                        (),
                        {
                            "ok": False,
                            "clean_output": "tmux pane died while waiting for turn artifacts",
                            "exit_code": 1,
                        },
                    )()

            def replacement_behavior(worker, label, prompt, completion_contract, timeout_sec):  # noqa: ANN001
                output_path.write_text("最终正文\n", encoding="utf-8")
                record_path.write_text("- [已确认] replacement\n", encoding="utf-8")
                question_path.write_text("", encoding="utf-8")
                completion_contract.validator(completion_contract.status_path)

            dead_worker = _OkFalseWorker(lambda *_args, **_kwargs: None, runtime_dir=root / "runtime-dead")
            replacement_worker = _FakeWorker(replacement_behavior, runtime_dir=root / "runtime-new")
            replacement_calls: list[tuple[object, str]] = []
            finished_workers: list[object] = []

            result = run_hitl_agent_loop(
                worker=dead_worker,
                stage_name="demo_stage",
                output_path=output_path,
                question_path=question_path,
                record_path=record_path,
                stage_status_path=stage_status_path,
                turns_root=turns_root,
                initial_prompt_builder=lambda context: f"initial::{context.turn_id}",
                hitl_prompt_builder=lambda human_msg, context: f"followup::{human_msg}::{context.turn_id}",
                label_prefix="demo_turn",
                turn_phase="demo_phase",
                on_agent_turn_finished=lambda context, worker: finished_workers.append(worker),
                replace_dead_worker=lambda current_worker, error: replacement_calls.append((current_worker, str(error))) or replacement_worker,
            )

        self.assertEqual(result.rounds_used, 1)
        self.assertEqual(result.decision.status, HITL_STATUS_COMPLETED)
        self.assertEqual(dead_worker.turn_calls, 1)
        self.assertEqual(replacement_worker.turn_calls, 1)
        self.assertEqual(len(replacement_calls), 1)
        self.assertIs(replacement_calls[0][0], dead_worker)
        self.assertEqual(finished_workers, [dead_worker, replacement_worker])

    def test_run_hitl_agent_loop_materializes_status_files_from_business_artifacts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            output_path = root / "output.md"
            question_path = root / "question.md"
            record_path = root / "record.md"
            stage_status_path = root / "status.json"
            turns_root = root / "turns"

            def behavior(worker, label, prompt, completion_contract, timeout_sec):  # noqa: ANN001
                if worker.turn_calls == 1:
                    question_path.write_text("- [阻断] 需要补充业务边界\n", encoding="utf-8")
                    record_path.write_text("- [待确认] 业务边界\n", encoding="utf-8")
                else:
                    self.assertIn("补充说明", prompt)
                    output_path.write_text("最终正文\n", encoding="utf-8")
                    record_path.write_text("- [已确认] 使用补充说明\n", encoding="utf-8")
                completion_contract.validator(completion_contract.status_path)

            worker = _FakeWorker(behavior, runtime_dir=root / "runtime")
            result = run_hitl_agent_loop(
                worker=worker,
                stage_name="demo_stage",
                output_path=output_path,
                question_path=question_path,
                record_path=record_path,
                stage_status_path=stage_status_path,
                turns_root=turns_root,
                initial_prompt_builder=lambda context: f"initial::{context.turn_id}",
                hitl_prompt_builder=lambda human_msg, context: f"followup::{human_msg}::{context.turn_id}",
                label_prefix="demo_turn",
                turn_phase="demo_phase",
                human_input_provider=lambda path, round_index: "补充说明",
            )
        self.assertEqual(result.rounds_used, 2)
        self.assertEqual(result.decision.status, HITL_STATUS_COMPLETED)

    def test_run_hitl_agent_loop_enters_hitl_when_question_exists_and_record_file_is_empty(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            output_path = root / "output.md"
            question_path = root / "question.md"
            record_path = root / "record.md"
            stage_status_path = root / "status.json"
            turns_root = root / "turns"
            output_path.write_text("旧的需求澄清\n", encoding="utf-8")
            human_calls: list[tuple[str, int]] = []

            def behavior(worker, label, prompt, completion_contract, timeout_sec):  # noqa: ANN001
                if worker.turn_calls == 1:
                    question_path.write_text("- [阻断] 需要补充平台范围\n", encoding="utf-8")
                    record_path.write_text("", encoding="utf-8")
                else:
                    self.assertIn("补充说明", prompt)
                    output_path.write_text("新的需求澄清\n", encoding="utf-8")
                    question_path.write_text("", encoding="utf-8")
                    record_path.write_text("", encoding="utf-8")
                completion_contract.validator(completion_contract.status_path)

            worker = _FakeWorker(behavior, runtime_dir=root / "runtime")

            def fake_human_input_provider(question_path_text, hitl_round):  # noqa: ANN001
                human_calls.append((str(question_path_text), hitl_round))
                return "补充说明"

            result = run_hitl_agent_loop(
                worker=worker,
                stage_name="demo_stage",
                output_path=output_path,
                question_path=question_path,
                record_path=record_path,
                stage_status_path=stage_status_path,
                turns_root=turns_root,
                initial_prompt_builder=lambda context: f"initial::{context.turn_id}",
                hitl_prompt_builder=lambda human_msg, context: f"followup::{human_msg}::{context.turn_id}",
                label_prefix="demo_turn",
                turn_phase="demo_phase",
                human_input_provider=fake_human_input_provider,
            )

        self.assertEqual(result.rounds_used, 2)
        self.assertEqual(result.decision.status, HITL_STATUS_COMPLETED)
        self.assertEqual(
            human_calls,
            [
                (str(question_path.resolve()), 1),
            ],
        )

    def test_run_hitl_agent_loop_recovers_when_agent_writes_completed_but_question_file_still_exists(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            output_path = root / "output.md"
            question_path = root / "question.md"
            record_path = root / "record.md"
            stage_status_path = root / "status.json"
            turns_root = root / "turns"
            output_path.write_text("旧的需求澄清\n", encoding="utf-8")
            human_calls: list[tuple[str, int]] = []

            def behavior(worker, label, prompt, completion_contract, timeout_sec):  # noqa: ANN001
                if worker.turn_calls == 1:
                    question_path.write_text("- [阻断] 需要补充平台范围\n", encoding="utf-8")
                    record_path.write_text("- [待确认] 平台范围\n", encoding="utf-8")
                    _write_stage_status(
                        stage_status_path,
                        stage="demo_stage",
                        turn_id=completion_contract.turn_id,
                        hitl_round=1,
                        status=HITL_STATUS_COMPLETED,
                        output_path=output_path,
                        question_path=None,
                        record_path=None,
                        summary="done",
                    )
                else:
                    self.assertIn("补充说明", prompt)
                    output_path.write_text("新的需求澄清\n", encoding="utf-8")
                    question_path.write_text("", encoding="utf-8")
                    record_path.write_text("", encoding="utf-8")
                completion_contract.validator(completion_contract.status_path)

            worker = _FakeWorker(behavior, runtime_dir=root / "runtime")

            def fake_human_input_provider(question_path_text, hitl_round):  # noqa: ANN001
                human_calls.append((str(question_path_text), hitl_round))
                return "补充说明"

            result = run_hitl_agent_loop(
                worker=worker,
                stage_name="demo_stage",
                output_path=output_path,
                question_path=question_path,
                record_path=record_path,
                stage_status_path=stage_status_path,
                turns_root=turns_root,
                initial_prompt_builder=lambda context: f"initial::{context.turn_id}",
                hitl_prompt_builder=lambda human_msg, context: f"followup::{human_msg}::{context.turn_id}",
                label_prefix="demo_turn",
                turn_phase="demo_phase",
                human_input_provider=fake_human_input_provider,
            )

        self.assertEqual(result.rounds_used, 2)
        self.assertEqual(result.decision.status, HITL_STATUS_COMPLETED)
        self.assertEqual(
            human_calls,
            [
                (str(question_path.resolve()), 1),
            ],
        )

    def test_run_hitl_agent_loop_recovers_when_agent_writes_invalid_completed_stage_and_turn_status(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            output_path = root / "output.md"
            question_path = root / "question.md"
            record_path = root / "record.md"
            stage_status_path = root / "status.json"
            turns_root = root / "turns"
            output_path.write_text("旧的需求澄清\n", encoding="utf-8")
            human_calls: list[tuple[str, int]] = []

            def behavior(worker, label, prompt, completion_contract, timeout_sec):  # noqa: ANN001
                if worker.turn_calls == 1:
                    question_path.write_text("- [阻断] 需要补充平台范围\n", encoding="utf-8")
                    record_path.write_text("- [待确认] 平台范围\n", encoding="utf-8")
                    _write_stage_status(
                        stage_status_path,
                        stage="demo_stage",
                        turn_id=completion_contract.turn_id,
                        hitl_round=1,
                        status=HITL_STATUS_COMPLETED,
                        output_path=output_path,
                        question_path=None,
                        record_path=None,
                        summary="done",
                    )
                    _write_turn_status(
                        completion_contract.status_path,
                        turn_id=completion_contract.turn_id,
                        phase="demo_phase",
                        stage_status_path=stage_status_path,
                        artifact_paths=[output_path],
                    )
                else:
                    self.assertIn("补充说明", prompt)
                    output_path.write_text("新的需求澄清\n", encoding="utf-8")
                    question_path.write_text("", encoding="utf-8")
                    record_path.write_text("", encoding="utf-8")
                return type("CommandResult", (), {"ok": True, "clean_output": "", "exit_code": 0})()

            worker = _FakeWorker(behavior, runtime_dir=root / "runtime")

            def fake_human_input_provider(question_path_text, hitl_round):  # noqa: ANN001
                human_calls.append((str(question_path_text), hitl_round))
                return "补充说明"

            result = run_hitl_agent_loop(
                worker=worker,
                stage_name="demo_stage",
                output_path=output_path,
                question_path=question_path,
                record_path=record_path,
                stage_status_path=stage_status_path,
                turns_root=turns_root,
                initial_prompt_builder=lambda context: f"initial::{context.turn_id}",
                hitl_prompt_builder=lambda human_msg, context: f"followup::{human_msg}::{context.turn_id}",
                label_prefix="demo_turn",
                turn_phase="demo_phase",
                human_input_provider=fake_human_input_provider,
            )

        self.assertEqual(result.rounds_used, 2)
        self.assertEqual(result.decision.status, HITL_STATUS_COMPLETED)
        self.assertEqual(
            human_calls,
            [
                (str(question_path.resolve()), 1),
            ],
        )

    def test_run_hitl_agent_loop_raises_on_error_status(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            output_path = root / "output.md"
            question_path = root / "question.md"
            record_path = root / "record.md"
            stage_status_path = root / "status.json"
            turns_root = root / "turns"

            def behavior(worker, label, prompt, completion_contract, timeout_sec):  # noqa: ANN001
                _write_stage_status(
                    stage_status_path,
                    stage="demo_stage",
                    turn_id=completion_contract.turn_id,
                    hitl_round=1,
                    status=HITL_STATUS_ERROR,
                    output_path=None,
                    question_path=None,
                    record_path=None,
                    summary="runtime failed",
                )
                _write_turn_status(
                    completion_contract.status_path,
                    turn_id=completion_contract.turn_id,
                    phase="demo_phase",
                    stage_status_path=stage_status_path,
                    artifact_paths=[],
                )

            worker = _FakeWorker(behavior, runtime_dir=root / "runtime")
            with self.assertRaisesRegex(RuntimeError, "runtime failed"):
                run_hitl_agent_loop(
                    worker=worker,
                    stage_name="demo_stage",
                    output_path=output_path,
                    question_path=question_path,
                    record_path=record_path,
                    stage_status_path=stage_status_path,
                    turns_root=turns_root,
                    initial_prompt_builder=lambda context: "initial",
                    hitl_prompt_builder=lambda human_msg, context: "followup",
                    label_prefix="demo_turn",
                    turn_phase="demo_phase",
                    human_input_provider=lambda path, round_index: "unused",
                )


if __name__ == "__main__":
    unittest.main()
