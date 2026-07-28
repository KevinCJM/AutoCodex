from __future__ import annotations

import io
import json
import os
import queue
import signal
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from A02_RequirementIntake import NOTION_RUNTIME_ROOT_NAME
from A04_RequirementsReview import (
    REQUIREMENTS_REVIEW_RUNTIME_ROOT_NAME,
    REQUIREMENTS_REVIEW_TASK_NAME,
    build_requirements_review_paths,
    build_reviewer_artifact_paths as build_requirements_reviewer_artifact_paths,
)
from A05_DetailedDesign import DETAILED_DESIGN_RUNTIME_ROOT_NAME, build_detailed_design_paths
from A06_TaskSplit import TASK_SPLIT_RUNTIME_ROOT_NAME, build_task_split_paths
from A07_Development import DEVELOPMENT_RUNTIME_ROOT_NAME, build_development_paths, build_reviewer_artifact_paths
from A08_OverallReview import build_overall_review_paths
from T03_agent_init_workflow import ROUTING_RUNTIME_ROOT_NAME, build_routing_runtime_root, required_routing_layer_paths
from T08_pre_development import update_pre_development_task_status
from T12_requirements_common import build_requirements_clarification_paths
from T11_tui_backend import (
    ControlSessionState,
    HumanAttentionManager,
    PendingPromptState,
    PromptBroker,
    RunnerExecutionState,
    TuiBackendServer,
    _read_graphify_app_status,
    _write_project_stage_state_record,
    main as backend_main,
)
from T10_tui_protocol import build_request
from T09_terminal_ops import BridgePromptRequest
from tmux_core.bridge.backend import _flatten_graphify_worker_fields
from tmux_core.runtime.tmux_runtime import (
    AgentRuntimeInterventionRequired,
    AgentStartupInterventionRequired,
    clear_runtime_shutdown_request,
    get_current_stage_runner_id,
    runtime_shutdown_requested,
)
from tmux_core.runtime.hitl import build_prefixed_sha256


def _write_valid_routing_layer(project_dir: Path) -> None:
    (project_dir / "docs").mkdir(parents=True, exist_ok=True)
    (project_dir / "AGENTS.md").write_text("ok\n", encoding="utf-8")
    (project_dir / "docs" / "repo_map.json").write_text(
        json.dumps({"modules": [{"id": "M01", "name": "root"}]}, ensure_ascii=False),
        encoding="utf-8",
    )
    (project_dir / "docs" / "task_routes.json").write_text(
        json.dumps(
            {
                "routes": [
                    {
                        "id": "R01",
                        "first_read_modules": [{"kind": "module", "ref": "M01"}],
                        "pitfall_ids": [{"kind": "pitfall", "ref": "P01"}],
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (project_dir / "docs" / "pitfalls.json").write_text(
        json.dumps(
            {
                "pitfalls": [
                    {
                        "id": "P01",
                        "title": "risk",
                        "affected_modules": [{"kind": "module", "ref": "M01"}],
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _write_grill_recovery_session(
    project_dir: Path,
    requirement_name: str,
    *,
    state: str = "awaiting_answer",
    pending_answer: str = "",
    active_worker_state_path: str = "",
    question_hash: str | None = None,
) -> tuple[Path, Path]:
    question_path = project_dir / ".requirements_runtime" / "grill-question.md"
    question_path.parent.mkdir(parents=True, exist_ok=True)
    question_path.write_text(
        """## 问题
数据边界如何定义？

## 为什么需要决定
影响实现范围。

## 推荐答案
方案 B

## 回答方式
select

## 选项
- 方案 A
- 方案 B

## 已核实事实
- 已读取 AGENTS.md 和路由层
""",
        encoding="utf-8",
    )
    session_path = project_dir / ".tmux_workflow" / requirement_name / "grill" / "session.json"
    session_path.parent.mkdir(parents=True, exist_ok=True)
    session_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "session_id": "grill-session-a",
                "requirements_mode": "grill",
                "state": state,
                "question_seq": 3,
                "pending_question_path": str(question_path),
                "pending_question_hash": question_hash or build_prefixed_sha256(question_path),
                "pending_answer": pending_answer,
                "active_worker_state_path": active_worker_state_path,
                "updated_at": "2026-07-24T12:00:00+00:00",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return session_path, question_path


class _FakeTarget:
    def __init__(self, *, session_name: str = "sess-1", transcript_path: str = "/tmp/transcript.md", work_dir: str = "/tmp/demo"):
        self.session_name = session_name
        self.transcript_path = transcript_path
        self.work_dir = work_dir


class _FakeCenter:
    def __init__(self, *, run_id: str = "run_demo", done: bool = False):
        self.run_id = run_id
        self.run_root = "/tmp/runtime"
        self._done = done
        self.closed = False
        self.cleaned = False
        self.selection = SimpleNamespace(project_dir="/tmp/project")

    def refresh_worker_health(self) -> None:
        return None

    def build_status_rows(self):
        return [{"index": 1, "session_name": "sess-1", "status": "running"}]

    def build_worker_snapshots(self):
        return [
            {
                "index": 1,
                "session_name": "sess-1",
                "work_dir": "/tmp/project",
                "status": "running",
                "workflow_stage": "create_running",
                "agent_state": "READY",
                "health_status": "healthy",
                "retry_count": 0,
                "note": "running",
                "transcript_path": "/tmp/transcript.md",
                "turn_status_path": "/tmp/turn_status.json",
                "question_path": "",
                "answer_path": "",
                "artifact_paths": [],
            }
        ]

    def all_done(self) -> bool:
        return self._done

    def wait_until_complete(self):
        return type("Batch", (), {"run_id": self.run_id, "runtime_dir": "/tmp/runtime", "config": {"vendor": "codex", "model": "gpt-5.4", "reasoning_effort": "high", "proxy_url": ""}, "results": []})()

    def transition_to_requirements_phase(self, batch_result):  # noqa: ANN001
        return "进入需求录入阶段（占位）"

    def can_switch_runs(self) -> bool:
        return True

    def render_status(self) -> str:
        return "status text"

    def get_target(self, argument: str):  # noqa: ARG002
        return _FakeTarget()

    def detach(self, argument: str):  # noqa: ARG002
        return "sess-1"

    def kill_worker(self, argument: str):  # noqa: ARG002
        return "sess-1"

    def restart_worker(self, argument: str):  # noqa: ARG002
        return "sess-1"

    def retry_worker(self, argument: str):  # noqa: ARG002
        return "sess-1"

    def close(self) -> None:
        self.closed = True

    def start(self) -> None:
        return None

    def cleanup_routing_tmux_sessions(self):
        self.cleaned = True
        return ["sess-1"]


class T11TuiBackendTests(unittest.TestCase):
    def test_prompt_broker_roundtrip(self):
        events: list[tuple[str, dict[str, object]]] = []
        broker = PromptBroker(lambda event_type, payload: events.append((event_type, payload)))

        def resolve_later() -> None:
            broker.resolve(str(events[0][1]["id"]), {"value": "ok"})

        import threading

        threading.Timer(0.01, resolve_later).start()
        payload = broker.request(type("Req", (), {"prompt_type": "text", "payload": {"prompt_text": "输入"}})())
        self.assertEqual(payload["value"], "ok")
        self.assertEqual(events[0][0], "prompt.request")

    def test_prompt_broker_assigns_distinct_ids_to_sequential_prompts_on_same_thread(self):
        events: list[tuple[str, dict[str, object]]] = []
        broker = PromptBroker(lambda event_type, payload: events.append((event_type, payload)))

        def resolve_last(value: str) -> None:
            broker.resolve(str(events[-1][1]["id"]), {"value": value})

        import threading

        threading.Timer(0.01, resolve_last, args=("first",)).start()
        first = broker.request(type("Req", (), {"prompt_type": "select", "payload": {"prompt_text": "第一个"}})())
        threading.Timer(0.01, resolve_last, args=("second",)).start()
        second = broker.request(type("Req", (), {"prompt_type": "select", "payload": {"prompt_text": "第二个"}})())

        prompt_ids = [str(payload["id"]) for event_type, payload in events if event_type == "prompt.request"]
        self.assertEqual(first["value"], "first")
        self.assertEqual(second["value"], "second")
        self.assertEqual(len(prompt_ids), 2)
        self.assertNotEqual(prompt_ids[0], prompt_ids[1])

    def test_prompt_broker_registers_prompt_before_synchronous_responder_can_resolve_it(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            observed_registered: list[bool] = []
            resolve_results: list[dict[str, object]] = []

            def resolve_as_soon_as_published(message):  # noqa: ANN001
                if message.get("kind") != "event" or message.get("type") != "prompt.request":
                    return
                prompt_id = str(message["payload"]["id"])
                observed_registered.append(prompt_id in server._pending_prompts)  # noqa: SLF001
                resolve_results.append(server.resolve_prompt(prompt_id, {"value": tmpdir}))

            server.subscribe_events(resolve_as_soon_as_published)
            response = server._prompt_broker.request(  # noqa: SLF001
                BridgePromptRequest(
                    prompt_type="text",
                    payload={"prompt_text": "请输入项目工作目录"},
                )
            )

            self.assertEqual(response["value"], tmpdir)
            self.assertEqual(observed_registered, [True])
            self.assertEqual(resolve_results, [{"accepted": True}])
            self.assertEqual(server._context.project_dir, str(Path(tmpdir).resolve()))  # noqa: SLF001
            self.assertEqual(server._pending_prompts, {})  # noqa: SLF001
            self.assertIsNone(server._pending_prompt)  # noqa: SLF001

    def test_resolving_authoritative_prompt_republishes_previous_prompt_with_new_revision(self):
        server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
        events: list[dict[str, object]] = []
        events_lock = threading.Lock()
        responses: dict[str, dict[str, object]] = {}

        def record_event(message):  # noqa: ANN001
            if message.get("kind") != "event":
                return
            with events_lock:
                events.append(dict(message))

        def prompt_events() -> list[dict[str, object]]:
            with events_lock:
                return [item for item in events if item.get("type") == "prompt.request"]

        def wait_for_prompt_count(expected: int) -> list[dict[str, object]]:
            deadline = time.time() + 2.0
            while time.time() < deadline:
                current = prompt_events()
                if len(current) >= expected:
                    return current
                time.sleep(0.005)
            return prompt_events()

        server.subscribe_events(record_event)

        def request_prompt(key: str) -> None:
            responses[key] = server._prompt_broker.request(  # noqa: SLF001
                BridgePromptRequest(
                    prompt_type="select",
                    payload={"title": f"选择 {key}", "options": []},
                )
            )

        with patch.object(server, "_schedule_snapshot_update"):
            first_thread = threading.Thread(target=request_prompt, args=("first",))
            first_thread.start()
            first_events = wait_for_prompt_count(1)
            self.assertEqual(len(first_events), 1)

            second_thread = threading.Thread(target=request_prompt, args=("second",))
            second_thread.start()
            opened_events = wait_for_prompt_count(2)
            self.assertEqual(len(opened_events), 2)
            first_id = str(opened_events[0]["payload"]["id"])
            second_id = str(opened_events[1]["payload"]["id"])

            self.assertEqual(server.resolve_prompt(second_id, {"value": "second"}), {"accepted": True})
            second_thread.join(timeout=2.0)
            republished_events = wait_for_prompt_count(3)

            self.assertFalse(second_thread.is_alive())
            self.assertEqual([item["payload"]["id"] for item in republished_events], [first_id, second_id, first_id])
            revisions = [int(item["payload"]["prompt_revision"]) for item in republished_events]
            self.assertEqual(revisions, sorted(revisions))
            self.assertEqual(len(set(revisions)), 3)
            self.assertEqual(responses["second"]["value"], "second")

            prompt_snapshot = server.build_prompt_snapshot()
            self.assertTrue(prompt_snapshot["pending"])
            self.assertEqual(prompt_snapshot["prompt_id"], first_id)
            self.assertEqual(prompt_snapshot["prompt_revision"], revisions[-1])

            server._emit_snapshot_update(include_prompt=True)  # noqa: SLF001
            with events_lock:
                snapshot_events = [item for item in events if item.get("type") == "snapshot.prompt"]
            self.assertTrue(snapshot_events)
            self.assertEqual(snapshot_events[-1]["payload"], prompt_snapshot)

            self.assertEqual(server.resolve_prompt(first_id, {"value": "first"}), {"accepted": True})
            first_thread.join(timeout=2.0)

        self.assertFalse(first_thread.is_alive())
        self.assertEqual(responses["first"]["value"], "first")
        empty_snapshot = server.build_prompt_snapshot()
        self.assertFalse(empty_snapshot["pending"])
        self.assertGreater(empty_snapshot["prompt_revision"], revisions[-1])

    def test_prompt_broker_publish_failure_cleans_open_prompt_and_does_not_wait(self):
        server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())

        with patch.object(
            server._prompt_broker,  # noqa: SLF001
            "_emit_event",
            side_effect=OSError("stdout unavailable"),
        ), self.assertRaisesRegex(OSError, "stdout unavailable"):
            server._prompt_broker.request(  # noqa: SLF001
                BridgePromptRequest(prompt_type="select", payload={"prompt_text": "选择身份"})
            )

        self.assertEqual(server._prompt_broker._pending, {})  # noqa: SLF001
        self.assertEqual(server._prompt_broker._claimed_prompts, set())  # noqa: SLF001
        self.assertEqual(server._pending_prompts, {})  # noqa: SLF001
        self.assertIsNone(server._pending_prompt)  # noqa: SLF001

    def test_prompt_broker_shutdown_unblocks_pending_prompt(self):
        events: list[tuple[str, dict[str, object]]] = []
        broker = PromptBroker(lambda event_type, payload: events.append((event_type, payload)))
        started = threading.Event()
        errors: list[str] = []

        def wait_for_prompt() -> None:
            try:
                started.set()
                broker.request(type("Req", (), {"prompt_type": "text", "payload": {"prompt_text": "输入"}})())
            except RuntimeError as error:
                errors.append(str(error))

        worker = threading.Thread(target=wait_for_prompt)
        worker.start()
        self.assertTrue(started.wait(timeout=2.0))
        while not events:
            time.sleep(0.01)
        broker.shutdown("backend exiting")
        worker.join(timeout=2.0)

        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, ["backend exiting"])

    def test_serve_forever_returns_one_error_response_for_synchronous_request_failure(self):
        request = build_request(
            "prompt.response",
            {"prompt_id": "missing-prompt", "value": "identity"},
            message_id="req-sync-failure",
        )
        writer = io.StringIO()
        server = TuiBackendServer(
            reader=io.StringIO(json.dumps(request, ensure_ascii=False) + "\n"),
            writer=writer,
        )

        self.assertEqual(server.serve_forever(), 0)

        messages = [json.loads(line) for line in writer.getvalue().splitlines() if line.strip()]
        responses = [
            message
            for message in messages
            if message.get("kind") == "response" and message.get("id") == "req-sync-failure"
        ]
        error_events = [
            message
            for message in messages
            if message.get("kind") == "event" and message.get("type") == "error"
        ]
        self.assertEqual(len(responses), 1)
        self.assertFalse(responses[0]["ok"])
        self.assertIn("missing-prompt", responses[0]["error"])
        self.assertEqual(len(error_events), 1)
        self.assertIn("missing-prompt", error_events[0]["payload"]["message"])

    def test_pending_hitl_snapshot_can_be_derived_from_active_prompt(self):
        server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
        server._pending_prompt = PendingPromptState(  # noqa: SLF001
            prompt_id="prompt_1",
            prompt_type="multiline",
            payload={
                "title": "HITL 第 1 轮回复",
                "question_path": "/tmp/question.md",
                "answer_path": "/tmp/answer.md",
                "session_name": "需求分析师-地奇星",
            },
        )
        hitl = server._build_hitl_snapshot()  # noqa: SLF001
        app = server._build_app_snapshot()  # noqa: SLF001
        self.assertTrue(hitl["pending"])
        self.assertEqual(hitl["summary"], "HITL 第 1 轮回复")
        self.assertEqual(hitl["question_path"], "/tmp/question.md")
        self.assertEqual(hitl["answer_path"], "/tmp/answer.md")
        self.assertEqual(hitl["attach_command"], "tmux attach -t 需求分析师-地奇星")
        self.assertTrue(app["pending_hitl"])

    def test_pending_hitl_snapshot_prefers_explicit_hitl_flag(self):
        server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
        server._pending_prompt = PendingPromptState(  # noqa: SLF001
            prompt_id="prompt_1",
            prompt_type="multiline",
            payload={
                "title": "请回复",
                "question_path": "/tmp/question.md",
                "answer_path": "/tmp/answer.md",
                "is_hitl": True,
                "attach_command": "tmux attach -t 测试工程师-天暴星",
            },
        )
        hitl = server._build_hitl_snapshot()  # noqa: SLF001
        self.assertTrue(hitl["pending"])
        self.assertEqual(hitl["question_path"], "/tmp/question.md")
        self.assertEqual(hitl["attach_command"], "tmux attach -t 测试工程师-天暴星")

    def test_pending_hitl_snapshot_exposes_file_noncompliance_prompt_details(self):
        server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
        server._pending_prompt = PendingPromptState(  # noqa: SLF001
            prompt_id="prompt_file_fix",
            prompt_type="select",
            payload={
                "title": "HITL: 测试工程师 需要人工介入",
                "is_hitl": True,
                "recovery_kind": "file_noncompliance",
                "reason_text": "指定文件连续 2 次修复后仍不符合要求。",
                "target_paths": ["/tmp/review.md", "/tmp/review.json"],
                "attach_command": "tmux attach -t 测试工程师-参水猿",
            },
        )

        hitl = server._build_hitl_snapshot()  # noqa: SLF001

        self.assertTrue(hitl["pending"])
        self.assertEqual(hitl["prompt_id"], "prompt_file_fix")
        self.assertEqual(hitl["prompt_type"], "select")
        self.assertEqual(hitl["recovery_kind"], "file_noncompliance")
        self.assertEqual(hitl["reason_text"], "指定文件连续 2 次修复后仍不符合要求。")
        self.assertEqual(hitl["target_paths"], ["/tmp/review.md", "/tmp/review.json"])
        self.assertEqual(hitl["attach_command"], "tmux attach -t 测试工程师-参水猿")

    def test_pending_hitl_snapshot_exposes_grill_prompt_details(self):
        server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
        server._pending_prompt = PendingPromptState(  # noqa: SLF001
            prompt_id="prompt_grill_2",
            prompt_type="multiline",
            payload={
                "title": "需求澄清 · Grill 第 2 题",
                "is_hitl": True,
                "interaction_kind": "grill",
                "question_index": 2,
                "recommendation": "保持旧接口兼容",
                "reason_text": "该选择决定迁移风险。",
                "question_path": "/tmp/grill-question.md",
            },
        )

        hitl = server._build_hitl_snapshot()  # noqa: SLF001

        self.assertEqual(hitl["interaction_kind"], "grill")
        self.assertEqual(hitl["question_index"], 2)
        self.assertEqual(hitl["recommendation"], "保持旧接口兼容")
        self.assertEqual(hitl["reason_text"], "该选择决定迁移风险。")

    def test_pending_attention_snapshot_can_be_derived_from_plain_prompt(self):
        server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
        server._pending_prompt = PendingPromptState(  # noqa: SLF001
            prompt_id="prompt_1",
            prompt_type="select",
            payload={"title": "请选择复核审核智能体模型", "stage_key": "overall_review_reviewer_selection"},
            created_at="2026-05-12T21:40:00+08:00",
        )

        app = server._build_app_snapshot(  # noqa: SLF001
            runs=[],
            control={},
            hitl={"pending": False},
            attention={"pending": False},
            artifacts={"items": []},
        )

        self.assertFalse(app["pending_hitl"])
        self.assertTrue(app["pending_attention"])
        self.assertEqual(app["pending_attention_reason"], "select")
        self.assertEqual(app["pending_attention_since"], "2026-05-12T21:40:00+08:00")
        self.assertEqual(app["active_stage_status"], "awaiting-input")

    def test_task_split_active_contract_keeps_ready_agent_ready_and_app_running(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir).resolve()
            requirement_name = "需求A"
            runtime_dir = project_dir / TASK_SPLIT_RUNTIME_ROOT_NAME / requirement_name / "task-split-analyst-1"
            runtime_dir.mkdir(parents=True)
            (runtime_dir / "worker.state.json").write_text(
                json.dumps(
                    {
                        "worker_id": "task-split-analyst",
                        "session_name": "需求分析师-天机星",
                        "work_dir": str(project_dir),
                        "project_dir": str(project_dir),
                        "requirement_name": requirement_name,
                        "workflow_action": "stage.a06.start",
                        "status": "running",
                        "result_status": "running",
                        "current_task_runtime_status": "running",
                        "dispatch_state": "submitted",
                        "current_task_status_path": str(runtime_dir / "modify.json"),
                        "current_task_result_path": str(runtime_dir / "modify_result.json"),
                        "agent_state": "READY",
                        "health_status": "alive",
                        "updated_at": "2026-05-18T20:54:33+08:00",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._set_context(project_dir=str(project_dir), requirement_name=requirement_name, action="stage.a06.start")  # noqa: SLF001
            server._display_action = "stage.a06.start"  # noqa: SLF001
            server._display_status = "ready"  # noqa: SLF001
            server._display_stage_seq = 8  # noqa: SLF001
            server._tmux_runtime = SimpleNamespace(session_exists=lambda name: name == "需求分析师-天机星", backend=None)  # noqa: SLF001

            task_split = server._build_task_split_snapshot()  # noqa: SLF001
            app = server._build_app_snapshot(  # noqa: SLF001
                runs=[],
                control={},
                hitl={"pending": False},
                attention={"pending": False},
                artifacts={"items": []},
            )

        self.assertEqual(task_split["workers"][0]["agent_state"], "READY")
        self.assertEqual(task_split["workers"][0]["current_task_runtime_status"], "running")
        self.assertEqual(app["active_stage_status"], "running")

    def test_task_split_pending_prompt_overrides_active_worker_running_status(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir).resolve()
            requirement_name = "需求A"
            runtime_dir = project_dir / TASK_SPLIT_RUNTIME_ROOT_NAME / requirement_name / "task-split-analyst-1"
            runtime_dir.mkdir(parents=True)
            (runtime_dir / "worker.state.json").write_text(
                json.dumps(
                    {
                        "worker_id": "task-split-analyst",
                        "session_name": "需求分析师-天机星",
                        "project_dir": str(project_dir),
                        "requirement_name": requirement_name,
                        "workflow_action": "stage.a06.start",
                        "status": "running",
                        "result_status": "running",
                        "current_task_runtime_status": "running",
                        "dispatch_state": "submitted",
                        "agent_state": "BUSY",
                        "health_status": "alive",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._set_context(project_dir=str(project_dir), requirement_name=requirement_name, action="stage.a06.start")  # noqa: SLF001
            server._display_action = "stage.a06.start"  # noqa: SLF001
            server._display_status = "ready"  # noqa: SLF001
            server._display_stage_seq = 8  # noqa: SLF001
            server._tmux_runtime = SimpleNamespace(session_exists=lambda name: name == "需求分析师-天机星", backend=None)  # noqa: SLF001
            server._pending_prompt = PendingPromptState(  # noqa: SLF001
                prompt_id="prompt_1",
                prompt_type="select",
                payload={"title": "请选择任务拆分需求分析师模型", "stage_key": "task_split_main"},
            )

            app = server._build_app_snapshot(  # noqa: SLF001
                runs=[],
                control={},
                hitl={"pending": False},
                attention={"pending": False},
                artifacts={"items": []},
            )

        self.assertTrue(app["pending_attention"])
        self.assertEqual(app["active_stage_status"], "awaiting-input")

    def test_task_split_ready_workers_without_active_turn_do_not_infer_running(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir).resolve()
            requirement_name = "需求A"
            runtime_dir = project_dir / TASK_SPLIT_RUNTIME_ROOT_NAME / requirement_name / "task-split-review-1"
            runtime_dir.mkdir(parents=True)
            (runtime_dir / "worker.state.json").write_text(
                json.dumps(
                    {
                        "worker_id": "task-split-review-审核员",
                        "session_name": "审核员-地奇星",
                        "project_dir": str(project_dir),
                        "requirement_name": requirement_name,
                        "workflow_action": "stage.a06.start",
                        "status": "succeeded",
                        "result_status": "succeeded",
                        "current_task_runtime_status": "done",
                        "dispatch_state": "",
                        "agent_state": "READY",
                        "health_status": "alive",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._set_context(project_dir=str(project_dir), requirement_name=requirement_name, action="stage.a06.start")  # noqa: SLF001
            server._display_action = "stage.a06.start"  # noqa: SLF001
            server._display_status = "ready"  # noqa: SLF001
            server._display_stage_seq = 8  # noqa: SLF001
            server._tmux_runtime = SimpleNamespace(session_exists=lambda name: name == "审核员-地奇星", backend=None)  # noqa: SLF001

            app = server._build_app_snapshot(  # noqa: SLF001
                runs=[],
                control={},
                hitl={"pending": False},
                attention={"pending": False},
                artifacts={"items": []},
            )

        self.assertEqual(app["active_stage_status"], "ready")

    def test_human_attention_manager_repeats_until_resolved(self):
        notifications: list[tuple[str, str, str]] = []
        manager = HumanAttentionManager(
            adapter_name_provider=lambda: "tui",
            platform_name="darwin",
            osascript_path="/usr/bin/osascript",
            interval_sec=0.05,
            notifier=lambda title, subtitle, body: notifications.append((title, subtitle, body)) or None,
        )

        manager.start_prompt(
            prompt_id="prompt_1",
            prompt_type="multiline",
            payload={"title": "请回复", "is_hitl": True},
            stage_label="任务开发",
        )
        time.sleep(0.13)
        snapshot = manager.snapshot()
        manager.resolve_prompt("prompt_1")
        notification_count = len(notifications)
        time.sleep(0.08)

        self.assertGreaterEqual(notification_count, 2)
        self.assertTrue(snapshot["pending"])
        self.assertEqual(snapshot["reason"], "hitl")
        self.assertEqual(snapshot["body"], "HITL 待处理")
        self.assertTrue(all(item[1] == "任务开发" for item in notifications[:notification_count]))
        self.assertFalse(manager.snapshot()["pending"])

    def test_human_attention_manager_keeps_other_unresolved_prompts_active(self):
        notifications: list[tuple[str, str, str]] = []
        manager = HumanAttentionManager(
            adapter_name_provider=lambda: "tui",
            platform_name="darwin",
            osascript_path="/usr/bin/osascript",
            interval_sec=0.05,
            notifier=lambda title, subtitle, body: notifications.append((title, subtitle, body)) or None,
        )

        manager.start_prompt(
            prompt_id="prompt_1",
            prompt_type="multiline",
            payload={"title": "请回复", "is_hitl": True},
            stage_label="任务开发",
        )
        manager.start_prompt(
            prompt_id="prompt_2",
            prompt_type="select",
            payload={"title": "请选择 reviewer 模型"},
            stage_label="任务拆分",
        )
        time.sleep(0.08)
        manager.resolve_prompt("prompt_2")
        time.sleep(0.08)
        snapshot = manager.snapshot()
        manager.shutdown()

        self.assertTrue(snapshot["pending"])
        self.assertEqual(snapshot["reason"], "hitl")
        self.assertIn(("TmuxCodingTeam 需要人工介入", "任务拆分", "请选择 reviewer 模型"), notifications)

    def test_human_attention_manager_initial_notify_is_async(self):
        manager = HumanAttentionManager(
            adapter_name_provider=lambda: "tui",
            platform_name="darwin",
            osascript_path="/usr/bin/osascript",
            interval_sec=60,
            notifier=lambda _title, _subtitle, _body: time.sleep(0.2) or None,
        )

        started = time.perf_counter()
        manager.start_prompt(
            prompt_id="prompt_1",
            prompt_type="text",
            payload={"prompt_text": "请输入项目目录"},
            stage_label="路由初始化",
        )
        elapsed = time.perf_counter() - started
        manager.shutdown()

        self.assertLess(elapsed, 0.1)

    def test_human_attention_manager_suppresses_initial_notify_while_tui_presence_is_recent(self):
        notifications: list[tuple[str, str, str]] = []
        presence_until = [time.monotonic() + 0.06]

        def presence_provider() -> dict[str, object]:
            delay = max(presence_until[0] - time.monotonic(), 0.0)
            return {
                "recent": delay > 0,
                "active_until": "soon",
                "delay_sec": delay,
            }

        manager = HumanAttentionManager(
            adapter_name_provider=lambda: "tui",
            platform_name="darwin",
            osascript_path="/usr/bin/osascript",
            interval_sec=0.05,
            presence_provider=presence_provider,
            notifier=lambda title, subtitle, body: notifications.append((title, subtitle, body)) or None,
        )

        try:
            manager.start_prompt(
                prompt_id="prompt_1",
                prompt_type="select",
                payload={"title": "请选择 reviewer 模型"},
                stage_label="任务拆分",
            )
            deadline = time.time() + 0.5
            while time.time() < deadline and not manager.snapshot().get("suppressed_due_to_presence"):
                time.sleep(0.005)
            suppressed_snapshot = manager.snapshot()
            self.assertFalse(suppressed_snapshot["pending"])
            self.assertTrue(suppressed_snapshot["suppressed_due_to_presence"])
            self.assertEqual(notifications, [])

            deadline = time.time() + 0.5
            while time.time() < deadline and not notifications:
                time.sleep(0.005)
            self.assertGreaterEqual(len(notifications), 1)
            self.assertTrue(manager.snapshot()["pending"])
        finally:
            manager.shutdown()

    def test_human_attention_manager_skips_repeat_notify_while_tui_presence_is_recent(self):
        notifications: list[tuple[str, str, str]] = []
        presence_until = [0.0]

        def presence_provider() -> dict[str, object]:
            delay = max(presence_until[0] - time.monotonic(), 0.0)
            return {
                "recent": delay > 0,
                "active_until": "soon",
                "delay_sec": delay,
            }

        manager = HumanAttentionManager(
            adapter_name_provider=lambda: "tui",
            platform_name="darwin",
            osascript_path="/usr/bin/osascript",
            interval_sec=0.03,
            presence_provider=presence_provider,
            notifier=lambda title, subtitle, body: notifications.append((title, subtitle, body)) or None,
        )

        try:
            manager.start_prompt(
                prompt_id="prompt_1",
                prompt_type="text",
                payload={"prompt_text": "请输入项目目录"},
                stage_label="路由初始化",
            )
            deadline = time.time() + 0.5
            while time.time() < deadline and not notifications:
                time.sleep(0.005)
            self.assertEqual(len(notifications), 1)

            presence_until[0] = time.monotonic() + 0.08
            time.sleep(0.05)
            self.assertEqual(len(notifications), 1)
            self.assertTrue(manager.snapshot()["suppressed_due_to_presence"])

            deadline = time.time() + 0.5
            while time.time() < deadline and len(notifications) < 2:
                time.sleep(0.005)
            self.assertGreaterEqual(len(notifications), 2)
        finally:
            manager.shutdown()

    def test_human_attention_manager_is_disabled_when_not_tui_or_not_macos(self):
        notifications: list[tuple[str, str, str]] = []
        manager = HumanAttentionManager(
            adapter_name_provider=lambda: "web",
            platform_name="linux",
            osascript_path="/usr/bin/osascript",
            notifier=lambda title, subtitle, body: notifications.append((title, subtitle, body)) or None,
        )

        manager.start_prompt(
            prompt_id="prompt_1",
            prompt_type="select",
            payload={"title": "请选择 reviewer 模型"},
            stage_label="任务拆分",
        )

        self.assertEqual(notifications, [])
        self.assertFalse(manager.snapshot()["pending"])

    def test_ui_presence_action_refreshes_tui_presence_window(self):
        server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
        server.attach_adapter("tui")

        with patch.object(server, "_schedule_snapshot_update") as schedule_snapshot, patch.object(
            server,
            "_arm_tui_presence_refresh_timer",
        ) as arm_refresh_timer:
            result = server.dispatch_action(
                "ui.presence",
                {"reason": "keyboard", "shell_focus": "content"},
                respond=False,
            )

        self.assertTrue(result["accepted"])
        self.assertTrue(server.is_tui_presence_recent())
        self.assertTrue(server.presence_expires_at())
        schedule_snapshot.assert_called_once_with(
            sections={"app", "control"},
            stage_routes=(),
            delay_sec=0.0,
            refresh_worker_health=False,
        )
        arm_refresh_timer.assert_called_once_with()

    def test_ui_presence_action_is_ignored_for_web_adapter(self):
        server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
        server._adapter_name = "web"  # noqa: SLF001

        result = server.dispatch_action(
            "ui.presence",
            {"reason": "keyboard", "shell_focus": "content"},
            respond=False,
        )

        self.assertFalse(result["accepted"])
        self.assertFalse(server.is_tui_presence_recent())

    def test_ui_presence_action_uses_persisted_active_stage_snapshot(self):
        server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
        server.attach_adapter("tui")
        server._set_context(project_dir="/tmp/project", requirement_name="需求A", action="stage.a07.start")  # noqa: SLF001

        with patch.object(server, "_schedule_snapshot_update") as schedule_snapshot, patch.object(
            server,
            "_arm_tui_presence_refresh_timer",
        ) as arm_refresh_timer:
            result = server.dispatch_action(
                "ui.presence",
                {"reason": "focus", "shell_focus": "content"},
                respond=False,
            )

        self.assertTrue(result["accepted"])
        schedule_snapshot.assert_called_once_with(
            sections={"app", "control"},
            stage_routes=("development",),
            delay_sec=0.0,
            refresh_worker_health=False,
        )
        arm_refresh_timer.assert_called_once_with()

    def test_tui_presence_refresh_tick_stops_when_presence_is_stale(self):
        server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
        server.attach_adapter("tui")
        server._set_context(project_dir="/tmp/project", requirement_name="需求A", action="stage.a07.start")  # noqa: SLF001

        with patch.object(server, "_schedule_snapshot_update") as schedule_snapshot, patch.object(
            server,
            "_arm_tui_presence_refresh_timer",
        ) as arm_refresh_timer:
            server._run_tui_presence_refresh_tick()  # noqa: SLF001

        schedule_snapshot.assert_not_called()
        arm_refresh_timer.assert_not_called()

    def test_tui_presence_refresh_tick_uses_persisted_worker_health(self):
        server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
        server.attach_adapter("tui")
        server._set_context(project_dir="/tmp/project", requirement_name="需求A", action="stage.a07.start")  # noqa: SLF001
        server._tui_presence_expiry_monotonic = time.monotonic() + 5.0  # noqa: SLF001

        with patch.object(server, "_schedule_snapshot_update") as schedule_snapshot, patch.object(
            server,
            "_arm_tui_presence_refresh_timer",
        ) as arm_refresh_timer:
            server._run_tui_presence_refresh_tick()  # noqa: SLF001

        schedule_snapshot.assert_called_once_with(
            sections={"app", "control"},
            stage_routes=("development",),
            delay_sec=0.0,
            refresh_worker_health=False,
        )
        arm_refresh_timer.assert_called_once_with()

    def test_prompt_open_starts_attention_manager_for_current_stage(self):
        server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
        calls: list[dict[str, object]] = []
        server._attention_manager = SimpleNamespace(  # noqa: SLF001
            start_prompt=lambda **kwargs: calls.append(dict(kwargs)),
            resolve_prompt=lambda *_args, **_kwargs: None,
            snapshot=lambda: {"pending": False, "reason": "", "started_at": ""},
            shutdown=lambda: None,
        )
        server._set_context(project_dir="/tmp/project", requirement_name="需求A", action="stage.a06.start")  # noqa: SLF001

        server._handle_prompt_open(  # noqa: SLF001
            "prompt_1",
            BridgePromptRequest(
                prompt_type="select",
                payload={"title": "请选择 reviewer 模型"},
            ),
        )

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["prompt_id"], "prompt_1")
        self.assertEqual(calls[0]["stage_label"], "任务拆分")

    def test_app_snapshot_exposes_pending_attention_fields(self):
        server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
        server._attention_manager = SimpleNamespace(  # noqa: SLF001
            start_prompt=lambda **kwargs: None,
            resolve_prompt=lambda *_args, **_kwargs: None,
            snapshot=lambda: {
                "pending": True,
                "reason": "select",
                "started_at": "2026-04-23T10:00:00+08:00",
            },
            shutdown=lambda: None,
        )

        snapshot = server._build_app_snapshot()  # noqa: SLF001

        self.assertTrue(snapshot["pending_attention"])
        self.assertEqual(snapshot["pending_attention_reason"], "select")
        self.assertEqual(snapshot["pending_attention_since"], "2026-04-23T10:00:00+08:00")

    def test_app_snapshot_exposes_only_safe_project_level_graphify_status(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir).resolve()
            evidence_id = "evidence-123"
            runtime_dir = project_dir / DEVELOPMENT_RUNTIME_ROOT_NAME / "worker-a"
            runtime_dir.mkdir(parents=True)
            state_path = runtime_dir / "worker.state.json"
            state_path.write_text("{}\n", encoding="utf-8")
            report_path = runtime_dir / f"graphify_evidence_{evidence_id}.md"
            report_path.write_text("# Graphify evidence\n", encoding="utf-8")
            stages = {
                "development": {
                    "workers": [
                        {
                            "state_path": str(state_path),
                            "graphify_evidence_id": evidence_id,
                        }
                    ]
                }
            }
            fake_graphify = SimpleNamespace(
                read_graphify_project_status=lambda _project_dir: {
                    "mode": "auto",
                    "state": "ready",
                    "version": "0.9.27",
                    "freshness": "fresh",
                    "node_count": 5098,
                    "edge_count": 22091,
                    "evidence_id": evidence_id,
                    "report_path": str(report_path),
                    "query_count_stage": 4,
                    "last_query_command": "affected",
                    "last_query_at": "2026-07-27T10:11:12+08:00",
                    "last_query_status": "ok",
                    "last_query_freshness": "fresh",
                    "last_query_truncated": False,
                    "query_text": "show me /Users/example/private.py",
                    "query_result": "secret result",
                    "last_error": "cache /Users/example/.cache/graphify failed",
                    "executable_path": "/Users/example/.local/bin/graphify",
                    "cache_dir": "/Users/example/.cache/graphify",
                }
            )
            with patch.dict(sys.modules, {"tmux_core.runtime.graphify": fake_graphify}):
                server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
                server._set_context(project_dir=str(project_dir))  # noqa: SLF001
                snapshot = server._build_app_snapshot(stage_snapshots=stages)  # noqa: SLF001
                allowed = server._allowed_file_preview_paths(  # noqa: SLF001
                    stages=stages,
                    control={"workers": []},
                    hitl={},
                    artifacts={"items": []},
                )

        self.assertEqual(snapshot["graphify"]["state"], "ready")
        self.assertEqual(snapshot["graphify"]["node_count"], 5098)
        self.assertEqual(snapshot["graphify"]["report_path"], str(report_path))
        self.assertEqual(snapshot["graphify"]["query_count_stage"], 4)
        self.assertEqual(snapshot["graphify"]["last_query_command"], "affected")
        self.assertEqual(snapshot["graphify"]["last_query_freshness"], "fresh")
        self.assertFalse(snapshot["graphify"]["last_query_truncated"])
        self.assertIn("<redacted-path>", snapshot["graphify"]["last_error"])
        self.assertNotIn("executable_path", snapshot["graphify"])
        self.assertNotIn("cache_dir", snapshot["graphify"])
        self.assertNotIn("query_text", snapshot["graphify"])
        self.assertNotIn("query_result", snapshot["graphify"])
        self.assertIn(str(report_path), allowed)

    def test_graphify_report_requires_matching_known_stage_worker_runtime(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir).resolve()
            runtime_dir = project_dir / DEVELOPMENT_RUNTIME_ROOT_NAME / "worker-a"
            runtime_dir.mkdir(parents=True)
            state_path = runtime_dir / "worker.state.json"
            state_path.write_text("{}\n", encoding="utf-8")
            evidence_id = "evidence-123"
            report_path = runtime_dir / f"graphify_evidence_{evidence_id}.md"
            report_path.write_text("# Graphify evidence\n", encoding="utf-8")
            fake_graphify = SimpleNamespace(
                read_graphify_project_status=lambda _project_dir: {
                    "mode": "auto",
                    "state": "ready",
                    "evidence_id": evidence_id,
                    "report_path": str(report_path),
                }
            )
            mismatches = (
                {},
                {"development": {"workers": [{"state_path": str(state_path), "graphify_evidence_id": "other"}]}},
                {
                    "development": {
                        "workers": [
                            {
                                "state_path": str(project_dir / "worker.state.json"),
                                "graphify_evidence_id": evidence_id,
                            }
                        ]
                    }
                },
            )
            with patch.dict(sys.modules, {"tmux_core.runtime.graphify": fake_graphify}):
                for stage_snapshots in mismatches:
                    status = _read_graphify_app_status(
                        str(project_dir),
                        stage_snapshots=stage_snapshots,
                    )
                    self.assertNotIn("report_path", status)

    def test_graphify_report_outside_project_is_not_exposed(self):
        with tempfile.TemporaryDirectory() as tmpdir, tempfile.NamedTemporaryFile() as outside:
            fake_graphify = SimpleNamespace(
                read_graphify_project_status=lambda _project_dir: {
                    "mode": "auto",
                    "state": "ready",
                    "report_path": outside.name,
                    "query_count_stage": -1,
                    "last_query_command": "/Users/example/private.py",
                    "last_query_at": "private question",
                    "last_query_status": "private result",
                    "last_query_freshness": "secret",
                    "last_query_truncated": "true",
                }
            )
            with patch.dict(sys.modules, {"tmux_core.runtime.graphify": fake_graphify}):
                status = _read_graphify_app_status(tmpdir)

        self.assertEqual(status["state"], "ready")
        self.assertEqual(status["query_count_stage"], 0)
        self.assertNotIn("report_path", status)
        self.assertNotIn("last_query_command", status)
        self.assertNotIn("last_query_at", status)
        self.assertNotIn("last_query_status", status)
        self.assertNotIn("last_query_freshness", status)
        self.assertNotIn("last_query_truncated", status)

    def test_worker_snapshot_exposes_only_safe_graphify_turn_identity(self):
        flattened = _flatten_graphify_worker_fields(
            {
                "config": {
                    "graphify_mode": "auto",
                    "graphify_config": {"include": ["secret/**"]},
                },
                "graphify_evidence_id": "evidence-123",
                "graphify_fingerprint": "a" * 64,
                "graphify_freshness": "fresh",
                "graphify_cache_dir": "/Users/example/.cache/private",
            }
        )
        self.assertEqual(flattened["graphify_mode"], "auto")
        self.assertEqual(flattened["graphify_evidence_id"], "evidence-123")
        self.assertEqual(flattened["graphify_fingerprint"], "a" * 64)
        self.assertEqual(flattened["graphify_freshness"], "fresh")
        self.assertNotIn("graphify_config", flattened)
        self.assertNotIn("graphify_cache_dir", flattened)

    def test_stage_status_does_not_treat_unattached_awaiting_reconfig_worker_as_running(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            runtime_dir = project_dir / DEVELOPMENT_RUNTIME_ROOT_NAME / "worker-awaiting-reconfig"
            runtime_dir.mkdir(parents=True, exist_ok=True)
            (runtime_dir / "worker.state.json").write_text(
                json.dumps(
                    {
                        "worker_id": "development-developer",
                        "session_name": "开发工程师-天魁星",
                        "work_dir": str(project_dir),
                        "project_dir": str(project_dir),
                        "requirement_name": "需求A",
                        "workflow_action": "stage.a07.start",
                        "result_status": "failed",
                        "status": "failed",
                        "agent_state": "DEAD",
                        "health_status": "awaiting_reconfig",
                        "health_note": "需要重新选择模型",
                        "note": "awaiting_reconfig",
                        "updated_at": "2026-04-23T10:00:00+08:00",
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._set_context(project_dir=str(project_dir), requirement_name="需求A", action="stage.a07.start")  # noqa: SLF001

            status = server._infer_runtime_stage_status("stage.a07.start")  # noqa: SLF001
            failed = server._failed_stage_worker_summaries("stage.a07.start")  # noqa: SLF001

        self.assertEqual(status, "")
        self.assertEqual(failed, [])

    def test_manual_reconfiguration_error_pending_detects_awaiting_reconfig_worker(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            runtime_dir = project_dir / DEVELOPMENT_RUNTIME_ROOT_NAME / "worker-awaiting-reconfig"
            runtime_dir.mkdir(parents=True, exist_ok=True)
            (runtime_dir / "worker.state.json").write_text(
                json.dumps(
                    {
                        "worker_id": "development-developer",
                        "session_name": "开发工程师-柳土獐",
                        "work_dir": str(project_dir),
                        "project_dir": str(project_dir),
                        "requirement_name": "需求A",
                        "workflow_action": "stage.a07.start",
                        "result_status": "running",
                        "agent_state": "STARTING",
                        "health_status": "awaiting_reconfig",
                        "health_note": "需要重新选择模型",
                        "note": "awaiting_reconfig",
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._set_context(project_dir=str(project_dir), requirement_name="需求A", action="stage.a07.start")  # noqa: SLF001

            pending = server._manual_reconfiguration_error_pending(  # noqa: SLF001
                action="stage.a07.start",
                error=RuntimeError("检测到 开发工程师-柳土獐 需要重新启动或重建。原因: tmux pane missing"),
            )

        self.assertTrue(pending)

    def test_manual_reconfiguration_error_does_not_hitch_on_unrelated_pending_prompt(self):
        server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
        unrelated = PendingPromptState(
            prompt_id="prompt-unrelated",
            prompt_type="select",
            payload={
                "title": "请选择需求",
                "recovery_kind": "requirement_selection",
            },
        )
        server._pending_prompts[unrelated.prompt_id] = unrelated  # noqa: SLF001
        server._pending_prompt = unrelated  # noqa: SLF001

        with patch.object(server, "_current_stage_workers_without_runtime_io", return_value=[]):
            pending = server._manual_reconfiguration_error_pending(  # noqa: SLF001
                action="stage.a07.start",
                error=RuntimeError("tmux pane died while running"),
            )

        self.assertFalse(pending)

    def test_manual_reconfiguration_error_can_reuse_matching_action_prompt(self):
        server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
        matching = PendingPromptState(
            prompt_id="prompt-manual",
            prompt_type="select",
            payload={
                "recovery_kind": "manual_reconfiguration",
                "workflow_action": "stage.a07.start",
                "session_name": "开发工程师-天罡星",
            },
        )
        server._pending_prompts[matching.prompt_id] = matching  # noqa: SLF001
        server._pending_prompt = matching  # noqa: SLF001

        with patch.object(server, "_current_stage_workers_without_runtime_io", return_value=[]):
            pending = server._manual_reconfiguration_error_pending(  # noqa: SLF001
                action="stage.a07.start",
                error=RuntimeError("tmux pane died while running"),
            )

        self.assertTrue(pending)

    def test_manual_reconfiguration_opens_own_prompt_when_unrelated_prompt_is_pending(self):
        server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
        unrelated = PendingPromptState(
            prompt_id="prompt-unrelated",
            prompt_type="select",
            payload={"title": "请选择需求", "recovery_kind": "requirement_selection"},
        )
        server._pending_prompts[unrelated.prompt_id] = unrelated  # noqa: SLF001
        server._pending_prompt = unrelated  # noqa: SLF001
        worker = {
            "worker_id": "development-developer",
            "session_name": "开发工程师-天罡星",
            "health_status": "awaiting_reconfig",
            "note": "awaiting_reconfig",
        }

        with patch.object(
            server,
            "_current_stage_workers_without_runtime_io",
            return_value=[worker],
        ), patch.object(
            server,
            "_filter_workers_for_current_context",
            side_effect=lambda workers, _action: list(workers),
        ), patch.object(
            server,
            "_persist_runner_awaiting_input",
            return_value=True,
        ), patch.object(
            server._prompt_broker,
            "request",
            return_value={"value": "retry_after_manual_reconfiguration"},
        ) as request_prompt, patch.object(server, "_schedule_snapshot_update"):
            server._await_manual_reconfiguration_recovery(  # noqa: SLF001
                request_id="",
                action="stage.a07.start",
                stage_seq=7,
                error=RuntimeError("awaiting_reconfig"),
                respond=False,
            )

        request_prompt.assert_called_once()
        request = request_prompt.call_args.args[0]
        self.assertEqual(request.payload["recovery_kind"], "manual_reconfiguration")
        self.assertEqual(request.payload["workflow_action"], "stage.a07.start")
        self.assertEqual(request.payload["session_name"], "开发工程师-天罡星")

    def test_stage_a08_ready_worker_with_stale_reconfig_note_is_not_inferred_running(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            review_json_path = project_dir / "需求A_整体复核记录_测试工程师-地镇星.json"
            runtime_dir = project_dir / DEVELOPMENT_RUNTIME_ROOT_NAME / "worker-ready-after-reconfig"
            runtime_dir.mkdir(parents=True, exist_ok=True)
            (runtime_dir / "worker.state.json").write_text(
                json.dumps(
                    {
                        "worker_id": "development-review-测试工程师",
                        "session_name": "测试工程师-地镇星",
                        "work_dir": str(project_dir),
                        "project_dir": str(project_dir.resolve()),
                        "requirement_name": "需求A",
                        "workflow_action": "stage.a08.start",
                        "status": "running",
                        "result_status": "running",
                        "agent_state": "READY",
                        "agent_started": True,
                        "agent_alive": True,
                        "current_command": "opencode.exe",
                        "health_status": "alive",
                        "health_note": "alive",
                        "note": "awaiting_reconfig",
                        "current_turn_phase": "复核阶段",
                        "current_turn_status_path": str(review_json_path),
                        "updated_at": "2026-06-18T12:08:09+08:00",
                        "last_heartbeat_at": "2026-06-18T12:08:09+08:00",
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._tmux_runtime.session_exists = lambda _session_name: True  # noqa: SLF001
            server._tmux_runtime.backend.session_exists = lambda _session_name: True  # noqa: SLF001
            server._set_context(project_dir=str(project_dir), requirement_name="需求A", action="stage.a08.start")  # noqa: SLF001

            with patch("tmux_core.bridge.backend.load_worker_from_state_path", return_value=None):
                snapshot = server._build_overall_review_snapshot()  # noqa: SLF001
                status = server._infer_runtime_stage_status("stage.a08.start")  # noqa: SLF001

        self.assertEqual(snapshot["workers"][0]["agent_state"], "READY")
        self.assertEqual(snapshot["workers"][0]["health_status"], "alive")
        self.assertNotEqual(status, "running")

    def test_stage_a03_manual_reconfiguration_opens_hitl_prompt_instead_of_running(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            requirement_name = "需求A"
            runtime_dir = project_dir / ".requirements_clarification_runtime" / "requirements-analyst-dead"
            runtime_dir.mkdir(parents=True, exist_ok=True)
            writer = io.StringIO()
            server = TuiBackendServer(reader=io.StringIO(), writer=writer)

            def write_failed_start_state() -> None:
                (runtime_dir / "worker.state.json").write_text(
                    json.dumps(
                        {
                            "worker_id": "requirements-analyst",
                            "session_name": "分析师-地英星",
                            "pane_id": "%96",
                            "work_dir": str(project_dir),
                            "project_dir": str(project_dir),
                            "requirement_name": requirement_name,
                            "workflow_action": "stage.a03.start",
                            "status": "running",
                            "result_status": "running",
                            "agent_state": "STARTING",
                            "agent_started": False,
                            "health_status": "awaiting_reconfig",
                            "health_note": "需要重新选择模型",
                            "note": "awaiting_reconfig",
                            "updated_at": "2026-05-14T16:24:48+08:00",
                        },
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )

            def fake_requirements_stage(*_args, **_kwargs):  # noqa: ANN002, ANN003
                write_failed_start_state()
                raise RuntimeError("agent exited back to shell while starting:\nmock codex command")

            with patch("T11_tui_backend.run_requirements_clarification_stage", side_effect=fake_requirements_stage):
                server.handle_request(
                    build_request(
                        "stage.a03.start",
                        {
                            "argv": [
                                "--project-dir",
                                str(project_dir),
                                "--requirement-name",
                                requirement_name,
                            ]
                        },
                        message_id="req_manual_reconfig",
                    )
                )
                prompt_id = ""
                messages_before_resolve: list[dict[str, object]] = []
                deadline = time.time() + 2.0
                while time.time() < deadline:
                    messages = [json.loads(line) for line in writer.getvalue().splitlines() if line.strip()]
                    prompt_messages = [
                        item
                        for item in messages
                        if item.get("kind") == "event" and item.get("type") == "prompt.request"
                    ]
                    if prompt_messages:
                        prompt_id = str(prompt_messages[-1]["payload"]["id"])
                    app_snapshots = [item for item in messages if item.get("kind") == "event" and item.get("type") == "snapshot.app"]
                    if prompt_id and any(item["payload"].get("pending_hitl") for item in app_snapshots):
                        messages_before_resolve = messages
                        break
                    time.sleep(0.01)

                self.assertTrue(prompt_id)
                self.assertTrue(messages_before_resolve)
                server.handle_request(
                    build_request(
                        "prompt.response",
                        {"prompt_id": prompt_id, "value": "retry_after_manual_reconfiguration"},
                        message_id="req_manual_reconfig_prompt",
                    )
                )
                for worker in list(server._workers.values()):  # noqa: SLF001
                    worker.join(timeout=2.0)

            prompt_payload = [
                item["payload"]
                for item in messages_before_resolve
                if item.get("kind") == "event" and item.get("type") == "prompt.request"
            ][-1]
            self.assertTrue(prompt_payload["is_hitl"])
            self.assertEqual(prompt_payload["recovery_kind"], "manual_reconfiguration")
            self.assertEqual(prompt_payload["session_name"], "分析师-地英星")
            stage_events = [
                item
                for item in messages_before_resolve
                if item.get("kind") == "event" and item.get("type") == "stage.changed"
            ]
            self.assertTrue(stage_events)
            self.assertEqual(stage_events[-1]["payload"]["action"], "stage.a03.start")
            self.assertEqual(stage_events[-1]["payload"]["status"], "awaiting-input")
            app_snapshots = [item for item in messages_before_resolve if item.get("kind") == "event" and item.get("type") == "snapshot.app"]
            self.assertTrue(app_snapshots)
            self.assertTrue(app_snapshots[-1]["payload"]["pending_hitl"])
            self.assertEqual(app_snapshots[-1]["payload"]["active_stage_status"], "awaiting-input")

    def test_resolved_manual_reconfiguration_prompt_without_live_work_does_not_restore_running(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            requirement_name = "需求A"
            runtime_dir = project_dir / ".requirements_clarification_runtime" / "requirements-analyst-dead"
            runtime_dir.mkdir(parents=True, exist_ok=True)
            (runtime_dir / "worker.state.json").write_text(
                json.dumps(
                    {
                        "worker_id": "requirements-analyst",
                        "session_name": "分析师-地英星",
                        "pane_id": "%96",
                        "work_dir": str(project_dir),
                        "project_dir": str(project_dir),
                        "requirement_name": requirement_name,
                        "workflow_action": "stage.a03.start",
                        "status": "running",
                        "result_status": "running",
                        "agent_state": "STARTING",
                        "agent_started": False,
                        "health_status": "awaiting_reconfig",
                        "health_note": "需要重新选择模型",
                        "note": "awaiting_reconfig",
                        "updated_at": "2026-05-14T16:24:48+08:00",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            writer = io.StringIO()
            server = TuiBackendServer(reader=io.StringIO(), writer=writer)
            server._set_context(project_dir=str(project_dir), requirement_name=requirement_name, action="stage.a03.start")  # noqa: SLF001
            server._display_action = "stage.a03.start"  # noqa: SLF001
            server._display_status = "awaiting-input"  # noqa: SLF001
            server._display_stage_seq = 4  # noqa: SLF001
            server._pending_prompts["prompt_manual"] = PendingPromptState(  # noqa: SLF001
                prompt_id="prompt_manual",
                prompt_type="select",
                payload={
                    "title": "HITL: 智能体需要人工重配",
                    "is_hitl": True,
                    "recovery_kind": "manual_reconfiguration",
                    "session_name": "分析师-地英星",
                },
            )
            server._pending_prompt = server._pending_prompts["prompt_manual"]  # noqa: SLF001

            server._handle_prompt_resolved("prompt_manual", {"value": "retry_after_manual_reconfiguration"})  # noqa: SLF001
            server._flush_dirty_snapshots()  # noqa: SLF001
            messages = [json.loads(line) for line in writer.getvalue().splitlines() if line.strip()]

        stage_events = [
            item
            for item in messages
            if item.get("kind") == "event"
            and item.get("type") == "stage.changed"
            and item.get("payload", {}).get("action") == "stage.a03.start"
        ]
        self.assertFalse(any(item["payload"].get("status") == "running" for item in stage_events))
        app_snapshots = [item for item in messages if item.get("kind") == "event" and item.get("type") == "snapshot.app"]
        self.assertTrue(app_snapshots)
        self.assertEqual(app_snapshots[-1]["payload"]["active_stage_status"], "awaiting-input")

    def test_empty_hitl_question_file_is_not_reported_as_pending(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            _, _, ask_human_path, _ = build_requirements_clarification_paths(project_dir, "贪吃蛇")
            ask_human_path.parent.mkdir(parents=True, exist_ok=True)
            ask_human_path.write_text("\n", encoding="utf-8")
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._set_context(project_dir=str(project_dir), requirement_name="贪吃蛇", action="stage.a04.start")  # noqa: SLF001
            hitl = server._build_hitl_snapshot()  # noqa: SLF001
        self.assertFalse(hitl["pending"])
        self.assertEqual(hitl["question_path"], "")

    def test_file_driven_hitl_is_ignored_during_routing_stage(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            _, _, ask_human_path, _ = build_requirements_clarification_paths(project_dir, "贪吃蛇")
            development_paths = build_development_paths(project_dir, "贪吃蛇")
            ask_human_path.parent.mkdir(parents=True, exist_ok=True)
            ask_human_path.write_text("旧的需求澄清问题\n", encoding="utf-8")
            development_paths["ask_human_path"].parent.mkdir(parents=True, exist_ok=True)
            development_paths["ask_human_path"].write_text("旧的开发澄清问题\n", encoding="utf-8")
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._set_context(project_dir=str(project_dir), requirement_name="贪吃蛇", action="stage.a01.start")  # noqa: SLF001
            hitl = server._build_hitl_snapshot()  # noqa: SLF001

        self.assertFalse(hitl["pending"])
        self.assertEqual(hitl["question_path"], "")

    def test_requirements_file_hitl_is_ignored_during_intake_stage(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            _, _, ask_human_path, _ = build_requirements_clarification_paths(project_dir, "贪吃蛇")
            ask_human_path.parent.mkdir(parents=True, exist_ok=True)
            ask_human_path.write_text("旧的需求澄清问题\n", encoding="utf-8")
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._set_context(project_dir=str(project_dir), requirement_name="贪吃蛇", action="stage.a02.start")  # noqa: SLF001
            hitl = server._build_hitl_snapshot()  # noqa: SLF001

        self.assertFalse(hitl["pending"])
        self.assertEqual(hitl["question_path"], "")

    def test_hitl_snapshot_can_be_derived_from_current_stage_worker_question(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            requirement_name = "需求A"
            paths = build_development_paths(project_dir, requirement_name)
            for path in (
                paths["task_md_path"],
                paths["task_json_path"],
                paths["developer_output_path"],
                paths["merged_review_path"],
                paths["detailed_design_path"],
            ):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("ok\n", encoding="utf-8")
            paths["task_json_path"].write_text(json.dumps({"M1": {"M1-T1": False}}, ensure_ascii=False), encoding="utf-8")
            paths["ask_human_path"].write_text("请确认评审冲突\n", encoding="utf-8")
            runtime_dir = project_dir / DEVELOPMENT_RUNTIME_ROOT_NAME / "worker-review"
            runtime_dir.mkdir(parents=True, exist_ok=True)
            (runtime_dir / "worker.state.json").write_text(
                json.dumps(
                    {
                        "worker_id": "development-developer",
                        "session_name": "开发工程师-天魁星",
                        "work_dir": str(project_dir),
                        "project_dir": str(project_dir),
                        "requirement_name": requirement_name,
                        "workflow_action": "stage.a07.start",
                        "result_status": "running",
                        "workflow_stage": "turn_running",
                        "agent_state": "READY",
                        "health_status": "alive",
                        "question_path": str(paths["ask_human_path"]),
                        "answer_path": str(paths["hitl_record_path"]),
                        "updated_at": "2026-04-23T10:00:00+08:00",
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._set_context(project_dir=str(project_dir), requirement_name=requirement_name, action="stage.a07.start")  # noqa: SLF001
            hitl = server._build_hitl_snapshot()  # noqa: SLF001

        self.assertTrue(hitl["pending"])
        self.assertEqual(hitl["question_path"], str(paths["ask_human_path"]))

    def test_answered_hitl_question_is_not_re_reported_until_question_changes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            _, _, ask_human_path, _ = build_requirements_clarification_paths(project_dir, "贪吃蛇")
            ask_human_path.parent.mkdir(parents=True, exist_ok=True)
            ask_human_path.write_text("请补充边界条件\n", encoding="utf-8")
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._tmux_runtime.session_exists = lambda session_name: session_name == "需求分析师-天慧星"  # noqa: SLF001
            server._set_context(project_dir=str(project_dir), requirement_name="贪吃蛇", action="stage.a05.start")  # noqa: SLF001
            server._pending_prompt = PendingPromptState(  # noqa: SLF001
                prompt_id="prompt_1",
                prompt_type="multiline",
                payload={
                    "title": "HITL 第 1 轮回复",
                    "question_path": str(ask_human_path),
                    "answer_path": str(project_dir / "贪吃蛇_人机交互澄清记录.md"),
                },
            )

            server._handle_prompt_resolved("prompt_1", {"value": "这里是答复"})  # noqa: SLF001
            answered = server._build_hitl_snapshot()  # noqa: SLF001

            ask_human_path.write_text("请补充异常分支\n", encoding="utf-8")
            next_question = server._build_hitl_snapshot()  # noqa: SLF001

        self.assertFalse(answered["pending"])
        self.assertTrue(next_question["pending"])
        self.assertEqual(next_question["question_path"], str(ask_human_path))

    def test_task_split_unconsumed_file_hitl_is_not_hidden_by_last_resolved_prompt(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir).resolve()
            requirement_name = "需求A"
            paths = build_task_split_paths(project_dir, requirement_name)
            paths["ask_human_path"].write_text("请确认任务拆分粒度\n", encoding="utf-8")
            workflow_state_dir = project_dir / ".tmux_workflow" / requirement_name / "stages"
            workflow_state_dir.mkdir(parents=True)
            (workflow_state_dir / "stage_a06_start.state.json").write_text(
                json.dumps(
                    {
                        "action": "stage.a06.start",
                        "status": "awaiting-input",
                        "project_dir": str(project_dir),
                        "requirement_name": requirement_name,
                        "stage_seq": 8,
                        "source": "runtime_inference",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            (project_dir / "需求A_A06_流水记录.jsonl").write_text(
                json.dumps(
                    {
                        "record_index": 1,
                        "event_type": "hitl_question",
                        "source_paths": {"ask_human": str(paths["ask_human_path"])},
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._set_context(project_dir=str(project_dir), requirement_name=requirement_name, action="stage.a06.start")  # noqa: SLF001
            server._display_action = "stage.a06.start"  # noqa: SLF001
            server._display_status = "running"  # noqa: SLF001
            server._display_stage_seq = 8  # noqa: SLF001
            server._last_resolved_hitl = SimpleNamespace(  # noqa: SLF001
                question_path=str(paths["ask_human_path"]),
                question_summary="请确认任务拆分粒度",
            )

            hitl = server._build_hitl_snapshot()  # noqa: SLF001
            app = server._build_app_snapshot(  # noqa: SLF001
                runs=[],
                control={},
                hitl=hitl,
                attention={"pending": False},
                artifacts={"items": []},
            )

        self.assertTrue(hitl["pending"])
        self.assertEqual(hitl["question_path"], str(paths["ask_human_path"]))
        self.assertTrue(app["pending_hitl"])
        self.assertEqual(app["active_stage_status"], "awaiting-input")

    def test_task_split_consumed_file_hitl_can_be_hidden_after_answer_audit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir).resolve()
            requirement_name = "需求A"
            paths = build_task_split_paths(project_dir, requirement_name)
            paths["ask_human_path"].write_text("请确认任务拆分粒度\n", encoding="utf-8")
            workflow_state_dir = project_dir / ".tmux_workflow" / requirement_name / "stages"
            workflow_state_dir.mkdir(parents=True)
            (workflow_state_dir / "stage_a06_start.state.json").write_text(
                json.dumps(
                    {
                        "action": "stage.a06.start",
                        "status": "awaiting-input",
                        "project_dir": str(project_dir),
                        "requirement_name": requirement_name,
                        "stage_seq": 8,
                        "source": "runtime_inference",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            audit_lines = [
                {
                    "record_index": 1,
                    "event_type": "hitl_question",
                    "source_paths": {"ask_human": str(paths["ask_human_path"])},
                },
                {
                    "record_index": 2,
                    "event_type": "hitl_answer",
                    "source_paths": {"hitl_record": str(paths["hitl_record_path"])},
                },
            ]
            (project_dir / "需求A_A06_流水记录.jsonl").write_text(
                "\n".join(json.dumps(item, ensure_ascii=False) for item in audit_lines) + "\n",
                encoding="utf-8",
            )
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._set_context(project_dir=str(project_dir), requirement_name=requirement_name, action="stage.a06.start")  # noqa: SLF001
            server._display_action = "stage.a06.start"  # noqa: SLF001
            server._display_status = "running"  # noqa: SLF001
            server._display_stage_seq = 8  # noqa: SLF001
            server._last_resolved_hitl = SimpleNamespace(  # noqa: SLF001
                question_path=str(paths["ask_human_path"]),
                question_summary="请确认任务拆分粒度",
            )

            hitl = server._build_hitl_snapshot()  # noqa: SLF001

        self.assertFalse(hitl["pending"])
        self.assertEqual(hitl["question_path"], "")

    def test_review_snapshot_lists_ba_and_all_reviewers_from_runtime_root(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            paths = build_requirements_review_paths(project_dir, "贪吃蛇")
            for file_path in (
                paths["merged_review_path"],
                paths["ba_feedback_path"],
                paths["ask_human_path"],
                paths["hitl_record_path"],
            ):
                file_path.parent.mkdir(parents=True, exist_ok=True)
                file_path.write_text("", encoding="utf-8")
            runtime_root = project_dir / REQUIREMENTS_REVIEW_RUNTIME_ROOT_NAME
            session_names = [
                "评审分析师-天佑星",
                "审核器-天平星",
                "审核器-地隐星",
                "审核器-天哭星",
            ]
            for index, session_name in enumerate(session_names, start=1):
                runtime_dir = runtime_root / f"worker-{index}"
                runtime_dir.mkdir(parents=True, exist_ok=True)
                (runtime_dir / "worker.state.json").write_text(
                    json.dumps(
                        {
                            "session_name": session_name,
                            "work_dir": str(project_dir),
                            "result_status": "running",
                            "workflow_stage": "turn_running",
                            "agent_state": "BUSY",
                            "health_status": "alive",
                            "retry_count": 0,
                            "note": "",
                            "transcript_path": "",
                            "updated_at": "2026-04-17T18:00:00+08:00",
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._tmux_runtime.session_exists = lambda name: True  # noqa: SLF001
            server._set_context(project_dir=str(project_dir), requirement_name="贪吃蛇", action="stage.a04.start")  # noqa: SLF001

            review = server._build_review_snapshot()  # noqa: SLF001

        self.assertEqual([worker["session_name"] for worker in review["workers"]], session_names)

    def test_review_snapshot_does_not_materialize_hitl_record(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            original_requirement_path, requirements_clear_path, _, hitl_record_path = build_requirements_clarification_paths(project_dir, "需求A")
            original_requirement_path.write_text("原始需求正文\n", encoding="utf-8")
            requirements_clear_path.write_text("需求澄清正文\n", encoding="utf-8")
            self.assertFalse(hitl_record_path.exists())

            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._set_context(project_dir=str(project_dir), requirement_name="需求A", action="stage.a04.start")  # noqa: SLF001

            review = server._build_review_snapshot()  # noqa: SLF001

        self.assertEqual(review["requirement_name"], "需求A")
        self.assertFalse(hitl_record_path.exists())

    def test_review_snapshot_includes_reused_analyst_from_clarification_runtime(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            review_runtime_dir = project_dir / REQUIREMENTS_REVIEW_RUNTIME_ROOT_NAME / "reviewer"
            review_runtime_dir.mkdir(parents=True, exist_ok=True)
            (review_runtime_dir / "worker.state.json").write_text(
                json.dumps(
                    {
                        "worker_id": "requirements-review-r1",
                        "session_name": "审核器-天损星",
                        "work_dir": str(project_dir.resolve()),
                        "project_dir": str(project_dir.resolve()),
                        "requirement_name": "日志持久化",
                        "workflow_action": "stage.a04.start",
                        "result_status": "running",
                        "workflow_stage": "turn_running",
                        "agent_state": "BUSY",
                        "agent_started": True,
                        "health_status": "alive",
                        "current_task_runtime_status": "running",
                        "note": "turn:requirements_review_init_R1_round_1",
                        "updated_at": "2026-05-05T13:18:22",
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            clarification_runtime_dir = project_dir / ".requirements_clarification_runtime" / "ba"
            clarification_runtime_dir.mkdir(parents=True, exist_ok=True)
            (clarification_runtime_dir / "worker.state.json").write_text(
                json.dumps(
                    {
                        "worker_id": "requirements-analyst",
                        "session_name": "分析师-天闲星",
                        "work_dir": str(project_dir.resolve()),
                        "result_status": "succeeded",
                        "workflow_stage": "pending",
                        "agent_state": "READY",
                        "agent_started": True,
                        "health_status": "alive",
                        "current_task_runtime_status": "done",
                        "note": "done:requirements_clarification_round_1",
                        "updated_at": "2026-05-05T13:16:41",
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._tmux_runtime.session_exists = lambda name: name in {"审核器-天损星", "分析师-天闲星"}  # noqa: SLF001
            server._set_context(project_dir=str(project_dir), requirement_name="日志持久化", action="stage.a04.start")  # noqa: SLF001

            review = server._build_review_snapshot()  # noqa: SLF001

        self.assertEqual(
            [worker["session_name"] for worker in review["workers"]],
            ["审核器-天损星", "分析师-天闲星"],
        )

    def test_stage_a04_status_uses_review_feedback_analyst_and_ignores_valid_failed_reviewer(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            requirement_name = "独立部署"
            reviewer_session = "审核器-地会星"
            analyst_session = "分析师-心月狐"
            review_md_path, review_json_path = build_requirements_reviewer_artifact_paths(
                project_dir,
                requirement_name,
                reviewer_session,
            )
            review_md_path.write_text("缺少独立部署启动参数说明\n", encoding="utf-8")
            review_json_path.write_text(
                json.dumps(
                    [{"task_name": REQUIREMENTS_REVIEW_TASK_NAME, "review_pass": False}],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            review_runtime_dir = project_dir / REQUIREMENTS_REVIEW_RUNTIME_ROOT_NAME / "reviewer-r1"
            review_runtime_dir.mkdir(parents=True, exist_ok=True)
            (review_runtime_dir / "worker.state.json").write_text(
                json.dumps(
                    {
                        "worker_id": "requirements-review-r1",
                        "session_name": reviewer_session,
                        "work_dir": str(project_dir.resolve()),
                        "project_dir": str(project_dir.resolve()),
                        "requirement_name": requirement_name,
                        "workflow_action": "stage.a04.start",
                        "status": "failed",
                        "result_status": "failed",
                        "workflow_stage": "turn_running",
                        "agent_state": "READY",
                        "agent_started": True,
                        "health_status": "alive",
                        "note": "error:requirements_review_init_R1_round_1",
                        "updated_at": "2026-05-12T10:00:00+08:00",
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            clarification_runtime_dir = project_dir / ".requirements_clarification_runtime" / "ba-feedback"
            clarification_runtime_dir.mkdir(parents=True, exist_ok=True)
            (clarification_runtime_dir / "worker.state.json").write_text(
                json.dumps(
                    {
                        "worker_id": "requirements-analyst",
                        "session_name": analyst_session,
                        "work_dir": str(project_dir.resolve()),
                        "status": "running",
                        "result_status": "running",
                        "workflow_stage": "turn_running",
                        "agent_state": "BUSY",
                        "agent_started": True,
                        "health_status": "alive",
                        "current_task_runtime_status": "running",
                        "note": "turn:requirements_review_feedback_round_2_round_1",
                        "updated_at": "2026-05-12T10:01:00+08:00",
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._tmux_runtime.session_exists = lambda name: name == analyst_session  # noqa: SLF001
            server._tmux_runtime.backend.session_exists = lambda name: name == analyst_session  # noqa: SLF001
            server._set_context(project_dir=str(project_dir), requirement_name=requirement_name, action="stage.a04.start")  # noqa: SLF001
            with patch("tmux_core.bridge.backend.load_worker_from_state_path", return_value=None):
                workers = server._filter_workers_for_current_context(  # noqa: SLF001
                    server._current_stage_workers("stage.a04.start"),  # noqa: SLF001
                    "stage.a04.start",
                )
                reviewer_worker = next(worker for worker in workers if worker["session_name"] == reviewer_session)
                reviewer_failed = server._worker_snapshot_has_failed_status(  # noqa: SLF001
                    reviewer_worker,
                    action="stage.a04.start",
                )
                status = server._infer_runtime_stage_status("stage.a04.start")  # noqa: SLF001

        self.assertIn(analyst_session, [worker["session_name"] for worker in workers])
        self.assertFalse(reviewer_failed)
        self.assertEqual(status, "running")

    def test_design_snapshot_lists_ba_and_all_reviewers_from_runtime_root(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            paths = build_detailed_design_paths(project_dir, "贪吃蛇")
            for file_path in (
                paths["detailed_design_path"],
                paths["merged_review_path"],
                paths["ba_feedback_path"],
                paths["ask_human_path"],
                paths["hitl_record_path"],
            ):
                file_path.parent.mkdir(parents=True, exist_ok=True)
                file_path.write_text("", encoding="utf-8")
            runtime_root = project_dir / DETAILED_DESIGN_RUNTIME_ROOT_NAME
            session_names = [
                "需求分析师-天佑星",
                "开发工程师-天魁星",
                "测试工程师-天英星",
                "审核员-天机星",
            ]
            for index, session_name in enumerate(session_names, start=1):
                runtime_dir = runtime_root / f"worker-{index}"
                runtime_dir.mkdir(parents=True, exist_ok=True)
                (runtime_dir / "worker.state.json").write_text(
                    json.dumps(
                        {
                            "session_name": session_name,
                            "work_dir": str(project_dir),
                            "result_status": "running",
                            "workflow_stage": "turn_running",
                            "agent_state": "BUSY",
                            "health_status": "alive",
                            "retry_count": 0,
                            "note": "",
                            "transcript_path": "",
                            "updated_at": "2026-04-18T10:00:00+08:00",
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._tmux_runtime.session_exists = lambda name: True  # noqa: SLF001
            server._set_context(project_dir=str(project_dir), requirement_name="贪吃蛇", action="stage.a05.start")  # noqa: SLF001

            design = server._build_design_snapshot()  # noqa: SLF001

        self.assertEqual([worker["session_name"] for worker in design["workers"]], session_names)

    def test_design_snapshot_includes_reused_ba_from_previous_stage_runtime_roots(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            paths = build_detailed_design_paths(project_dir, "贪吃蛇")
            for file_path in (
                paths["detailed_design_path"],
                paths["merged_review_path"],
                paths["ba_feedback_path"],
                paths["ask_human_path"],
                paths["hitl_record_path"],
            ):
                file_path.parent.mkdir(parents=True, exist_ok=True)
                file_path.write_text("", encoding="utf-8")

            clarification_runtime_dir = project_dir / ".requirements_clarification_runtime" / "ba"
            clarification_runtime_dir.mkdir(parents=True, exist_ok=True)
            (clarification_runtime_dir / "worker.state.json").write_text(
                json.dumps(
                    {
                        "worker_id": "requirements-analyst",
                        "session_name": "需求分析师-天佑星",
                        "work_dir": str(project_dir),
                        "project_dir": str(project_dir),
                        "requirement_name": "贪吃蛇",
                        "workflow_action": "stage.a05.start",
                        "result_status": "running",
                        "workflow_stage": "turn_running",
                        "agent_state": "BUSY",
                        "health_status": "alive",
                        "retry_count": 0,
                        "note": "submitted:modify_detailed_design",
                        "transcript_path": "",
                        "updated_at": "2026-04-19T10:00:00+08:00",
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            design_runtime_dir = project_dir / DETAILED_DESIGN_RUNTIME_ROOT_NAME / "reviewer"
            design_runtime_dir.mkdir(parents=True, exist_ok=True)
            (design_runtime_dir / "worker.state.json").write_text(
                json.dumps(
                    {
                        "session_name": "开发工程师-天魁星",
                        "work_dir": str(project_dir),
                        "result_status": "running",
                        "workflow_stage": "turn_running",
                        "agent_state": "BUSY",
                        "health_status": "alive",
                        "retry_count": 0,
                        "note": "",
                        "transcript_path": "",
                        "updated_at": "2026-04-19T10:01:00+08:00",
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._tmux_runtime.session_exists = lambda name: True  # noqa: SLF001
            server._set_context(project_dir=str(project_dir), requirement_name="贪吃蛇", action="stage.a05.start")  # noqa: SLF001

            design = server._build_design_snapshot()  # noqa: SLF001

        self.assertEqual(
            [worker["session_name"] for worker in design["workers"]],
            ["开发工程师-天魁星", "需求分析师-天佑星"],
        )

    def test_design_snapshot_keeps_completed_ba_handoff_when_reviewers_are_scoped(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            paths = build_detailed_design_paths(project_dir, "日志持久化")
            for file_path in (
                paths["detailed_design_path"],
                paths["merged_review_path"],
                paths["ba_feedback_path"],
                paths["ask_human_path"],
                paths["hitl_record_path"],
            ):
                file_path.parent.mkdir(parents=True, exist_ok=True)
                file_path.write_text("", encoding="utf-8")

            clarification_runtime_dir = project_dir / ".requirements_clarification_runtime" / "ba"
            clarification_runtime_dir.mkdir(parents=True, exist_ok=True)
            (clarification_runtime_dir / "worker.state.json").write_text(
                json.dumps(
                    {
                        "worker_id": "requirements-analyst",
                        "session_name": "分析师-天闲星",
                        "work_dir": str(project_dir.resolve()),
                        "result_status": "succeeded",
                        "workflow_stage": "pending",
                        "agent_state": "READY",
                        "agent_started": True,
                        "health_status": "alive",
                        "current_task_runtime_status": "done",
                        "note": "done:generate_detailed_design",
                        "updated_at": "2026-05-05T12:01:02",
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            design_runtime_dir = project_dir / DETAILED_DESIGN_RUNTIME_ROOT_NAME / "日志持久化" / "reviewer"
            design_runtime_dir.mkdir(parents=True, exist_ok=True)
            (design_runtime_dir / "worker.state.json").write_text(
                json.dumps(
                    {
                        "worker_id": "detailed-design-review-架构师",
                        "session_name": "架构师-奎木狼",
                        "work_dir": str(project_dir.resolve()),
                        "project_dir": str(project_dir.resolve()),
                        "requirement_name": "日志持久化",
                        "workflow_action": "stage.a05.start",
                        "result_status": "succeeded",
                        "workflow_stage": "pending",
                        "agent_state": "READY",
                        "agent_started": True,
                        "health_status": "alive",
                        "current_task_runtime_status": "done",
                        "note": "done:detailed_design_review_init_架构师_round_1",
                        "updated_at": "2026-05-05T12:02:02",
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._tmux_runtime.session_exists = lambda name: name in {"分析师-天闲星", "架构师-奎木狼"}  # noqa: SLF001
            server._set_context(project_dir=str(project_dir), requirement_name="日志持久化", action="stage.a05.start")  # noqa: SLF001

            design = server._build_design_snapshot()  # noqa: SLF001

        self.assertEqual(
            [worker["session_name"] for worker in design["workers"]],
            ["架构师-奎木狼", "分析师-天闲星"],
        )

    def test_design_snapshot_ignores_unscoped_busy_ba_handoff_without_a05_artifact_evidence(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            paths = build_detailed_design_paths(project_dir, "独立部署")
            for file_path in (
                paths["detailed_design_path"],
                paths["merged_review_path"],
                paths["ba_feedback_path"],
                paths["ask_human_path"],
                paths["hitl_record_path"],
            ):
                file_path.parent.mkdir(parents=True, exist_ok=True)
                file_path.write_text("", encoding="utf-8")

            ba_runtime_dir = project_dir / ".requirements_clarification_runtime" / "requirements-analyst-stale"
            ba_runtime_dir.mkdir(parents=True, exist_ok=True)
            (ba_runtime_dir / "worker.state.json").write_text(
                json.dumps(
                    {
                        "worker_id": "requirements-analyst",
                        "session_name": "分析师-心月狐",
                        "work_dir": str(project_dir.resolve()),
                        "status": "running",
                        "result_status": "running",
                        "workflow_stage": "pending",
                        "agent_state": "BUSY",
                        "agent_started": True,
                        "health_status": "alive",
                        "current_task_runtime_status": "running",
                        "dispatch_state": "submitted",
                        "note": "submitted:modify_detailed_design",
                        "updated_at": "2026-05-12T19:59:49",
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            reviewer_runtime_dir = project_dir / DETAILED_DESIGN_RUNTIME_ROOT_NAME / "独立部署" / "reviewer"
            reviewer_runtime_dir.mkdir(parents=True, exist_ok=True)
            (reviewer_runtime_dir / "worker.state.json").write_text(
                json.dumps(
                    {
                        "worker_id": "detailed-design-review-开发工程师",
                        "session_name": "开发工程师-地遂星",
                        "work_dir": str(project_dir.resolve()),
                        "project_dir": str(project_dir.resolve()),
                        "requirement_name": "独立部署",
                        "workflow_action": "stage.a05.start",
                        "status": "succeeded",
                        "result_status": "succeeded",
                        "workflow_stage": "pending",
                        "agent_state": "READY",
                        "agent_started": True,
                        "health_status": "alive",
                        "current_task_runtime_status": "done",
                        "note": "done:detailed_design_review_again_开发工程师_round_3",
                        "updated_at": "2026-05-12T20:00:01",
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._tmux_runtime.session_exists = lambda name: name in {"分析师-心月狐", "开发工程师-地遂星"}  # noqa: SLF001
            server._set_context(project_dir=str(project_dir), requirement_name="独立部署", action="stage.a05.start")  # noqa: SLF001

            design = server._build_design_snapshot()  # noqa: SLF001

        self.assertEqual([worker["session_name"] for worker in design["workers"]], ["开发工程师-地遂星"])

    def test_design_snapshot_keeps_scoped_active_ba_handoff(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            paths = build_detailed_design_paths(project_dir, "独立部署")
            for file_path in (
                paths["detailed_design_path"],
                paths["merged_review_path"],
                paths["ba_feedback_path"],
                paths["ask_human_path"],
                paths["hitl_record_path"],
            ):
                file_path.parent.mkdir(parents=True, exist_ok=True)
                file_path.write_text("", encoding="utf-8")

            ba_runtime_dir = project_dir / ".requirements_clarification_runtime" / "requirements-analyst-active"
            ba_runtime_dir.mkdir(parents=True, exist_ok=True)
            (ba_runtime_dir / "worker.state.json").write_text(
                json.dumps(
                    {
                        "worker_id": "requirements-analyst",
                        "session_name": "分析师-心月狐",
                        "work_dir": str(project_dir.resolve()),
                        "project_dir": str(project_dir.resolve()),
                        "requirement_name": "独立部署",
                        "workflow_action": "stage.a05.start",
                        "status": "running",
                        "result_status": "running",
                        "workflow_stage": "pending",
                        "agent_state": "BUSY",
                        "agent_started": True,
                        "health_status": "alive",
                        "current_task_runtime_status": "running",
                        "note": "submitted:modify_detailed_design",
                        "updated_at": "2026-05-12T20:00:01",
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._tmux_runtime.session_exists = lambda name: name == "分析师-心月狐"  # noqa: SLF001
            server._set_context(project_dir=str(project_dir), requirement_name="独立部署", action="stage.a05.start")  # noqa: SLF001

            design = server._build_design_snapshot()  # noqa: SLF001

        self.assertEqual([worker["session_name"] for worker in design["workers"]], ["分析师-心月狐"])

    def test_stage_a05_status_is_not_polluted_by_failed_review_worker(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            review_runtime_dir = project_dir / REQUIREMENTS_REVIEW_RUNTIME_ROOT_NAME / "worker-failed"
            review_runtime_dir.mkdir(parents=True, exist_ok=True)
            (review_runtime_dir / "worker.state.json").write_text(
                json.dumps(
                    {
                        "worker_id": "requirements-review-r1",
                        "session_name": "审核器-毕月乌",
                        "work_dir": str(project_dir),
                        "result_status": "failed",
                        "workflow_stage": "pending",
                        "agent_state": "DEAD",
                        "health_status": "alive",
                        "note": "error:requirements_review_round_2",
                        "updated_at": "2026-04-20T14:06:58+08:00",
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            design_runtime_dir = project_dir / DETAILED_DESIGN_RUNTIME_ROOT_NAME / "worker-running"
            design_runtime_dir.mkdir(parents=True, exist_ok=True)
            (design_runtime_dir / "worker.state.json").write_text(
                json.dumps(
                    {
                        "worker_id": "detailed-design-analyst",
                        "session_name": "需求分析师-天慧星",
                        "work_dir": str(project_dir),
                        "result_status": "running",
                        "workflow_stage": "pending",
                        "agent_state": "BUSY",
                        "health_status": "alive",
                        "note": "turn:detailed_design_feedback_round_2",
                        "updated_at": "2026-04-20T14:05:58+08:00",
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._tmux_runtime.session_exists = lambda session_name: session_name == "需求分析师-天慧星"  # noqa: SLF001
            server._tmux_runtime.backend.session_exists = lambda session_name: session_name == "需求分析师-天慧星"  # noqa: SLF001
            server._set_context(project_dir=str(project_dir), requirement_name="贪吃蛇", action="stage.a05.start")  # noqa: SLF001
            with patch("tmux_core.bridge.backend.load_worker_from_state_path", return_value=None):
                status = server._infer_runtime_stage_status("stage.a05.start")  # noqa: SLF001

        self.assertEqual(status, "running")

    def test_stage_a05_status_ignores_previous_stage_failed_workers_before_design_runtime_exists(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            review_runtime_dir = project_dir / REQUIREMENTS_REVIEW_RUNTIME_ROOT_NAME / "worker-failed"
            review_runtime_dir.mkdir(parents=True, exist_ok=True)
            (review_runtime_dir / "worker.state.json").write_text(
                json.dumps(
                    {
                        "worker_id": "requirements-review-analyst",
                        "session_name": "需求分析师-天慧星",
                        "work_dir": str(project_dir),
                        "result_status": "failed",
                        "workflow_stage": "pending",
                        "agent_state": "DEAD",
                        "health_status": "alive",
                        "note": "error:requirements_review_feedback_round_2",
                        "updated_at": "2026-04-22T17:01:00+08:00",
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            clarification_runtime_dir = project_dir / ".requirements_clarification_runtime" / "worker-failed"
            clarification_runtime_dir.mkdir(parents=True, exist_ok=True)
            (clarification_runtime_dir / "worker.state.json").write_text(
                json.dumps(
                    {
                        "worker_id": "requirements-analyst",
                        "session_name": "分析师-天富星",
                        "work_dir": str(project_dir),
                        "result_status": "failed",
                        "workflow_stage": "pending",
                        "agent_state": "DEAD",
                        "health_status": "alive",
                        "note": "error:requirements_clarification_round_1",
                        "updated_at": "2026-04-22T17:02:00+08:00",
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._set_context(project_dir=str(project_dir), requirement_name="贪吃蛇", action="stage.a05.start")  # noqa: SLF001

            workers = server._current_stage_workers("stage.a05.start")  # noqa: SLF001
            status = server._infer_runtime_stage_status("stage.a05.start")  # noqa: SLF001

        self.assertEqual(workers, [])
        self.assertEqual(status, "")

    def test_stage_a05_status_ignores_running_nested_clarification_analyst_for_status_inference(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            clarification_runtime_dir = project_dir / ".requirements_clarification_runtime" / "worker-running"
            clarification_runtime_dir.mkdir(parents=True, exist_ok=True)
            (clarification_runtime_dir / "worker.state.json").write_text(
                json.dumps(
                    {
                        "worker_id": "requirements-analyst",
                        "session_name": "分析师-天慧星",
                        "work_dir": str(project_dir),
                        "result_status": "running",
                        "workflow_stage": "turn_running",
                        "agent_state": "BUSY",
                        "health_status": "alive",
                        "note": "turn:requirements_clarification_round_1",
                        "updated_at": "2026-04-20T14:05:58+08:00",
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._tmux_runtime.session_exists = lambda session_name: session_name == "分析师-天慧星"  # noqa: SLF001
            server._tmux_runtime.backend.session_exists = lambda session_name: session_name == "分析师-天慧星"  # noqa: SLF001
            server._set_context(project_dir=str(project_dir), requirement_name="贪吃蛇", action="stage.a05.start")  # noqa: SLF001
            with patch("tmux_core.bridge.backend.load_worker_from_state_path", return_value=None):
                status = server._infer_runtime_stage_status("stage.a05.start")  # noqa: SLF001

        self.assertEqual(status, "")

    def test_stage_a05_status_treats_dead_workers_as_failed_not_running(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            design_runtime_dir = project_dir / DETAILED_DESIGN_RUNTIME_ROOT_NAME / "worker-dead"
            design_runtime_dir.mkdir(parents=True, exist_ok=True)
            (design_runtime_dir / "worker.state.json").write_text(
                json.dumps(
                    {
                        "worker_id": "detailed-design-reviewer",
                        "session_name": "审核员-地异星",
                        "work_dir": str(project_dir),
                        "status": "running",
                        "result_status": "running",
                        "workflow_stage": "turn_running",
                        "agent_state": "DEAD",
                        "health_status": "dead",
                        "note": "tmux session missing",
                        "updated_at": "2026-04-22T10:05:58+08:00",
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._set_context(project_dir=str(project_dir), requirement_name="贪吃蛇", action="stage.a05.start")  # noqa: SLF001

            status = server._infer_runtime_stage_status("stage.a05.start")  # noqa: SLF001

        self.assertEqual(status, "failed")

    def test_task_split_snapshot_lists_ba_and_all_reviewers_from_runtime_roots(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            paths = build_task_split_paths(project_dir, "贪吃蛇")
            for file_path in (
                paths["task_md_path"],
                paths["task_json_path"],
                paths["merged_review_path"],
                paths["ba_feedback_path"],
                paths["ask_human_path"],
                paths["detailed_design_path"],
            ):
                file_path.parent.mkdir(parents=True, exist_ok=True)
                file_path.write_text("ok\n", encoding="utf-8")
            runtime_root = project_dir / TASK_SPLIT_RUNTIME_ROOT_NAME
            session_names = [
                "需求分析师-天佑星",
                "开发工程师-天魁星",
                "测试工程师-天英星",
            ]
            for index, session_name in enumerate(session_names, start=1):
                runtime_dir = runtime_root / f"worker-{index}"
                runtime_dir.mkdir(parents=True, exist_ok=True)
                (runtime_dir / "worker.state.json").write_text(
                    json.dumps(
                        {
                            "session_name": session_name,
                            "work_dir": str(project_dir),
                            "result_status": "running",
                            "workflow_stage": "turn_running",
                            "agent_state": "BUSY",
                            "health_status": "alive",
                            "retry_count": 0,
                            "note": "",
                            "transcript_path": "",
                            "updated_at": "2026-04-20T10:00:00+08:00",
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._tmux_runtime.session_exists = lambda name: True  # noqa: SLF001
            server._set_context(project_dir=str(project_dir), requirement_name="贪吃蛇", action="stage.a06.start")  # noqa: SLF001

            task_split = server._build_task_split_snapshot()  # noqa: SLF001

        self.assertEqual([worker["session_name"] for worker in task_split["workers"]], session_names)
        self.assertTrue(any(item["label"] == "任务单" for item in task_split["files"]))
        self.assertTrue(any(item["label"] == "任务单 JSON" for item in task_split["files"]))

    def test_development_snapshot_lists_workers_and_stage_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            paths = build_development_paths(project_dir, "贪吃蛇")
            for file_path, content in (
                (paths["task_md_path"], "任务单\n"),
                (
                    paths["task_json_path"],
                    json.dumps({"M1": {"M1-T1": True}, "M2": {"M2-T1": False, "M2-T2": True}}, ensure_ascii=False, indent=2),
                ),
                (paths["ask_human_path"], ""),
                (paths["developer_output_path"], "开发内容\n"),
                (paths["merged_review_path"], "评审记录\n"),
                (paths["detailed_design_path"], "详细设计\n"),
            ):
                file_path.parent.mkdir(parents=True, exist_ok=True)
                file_path.write_text(content, encoding="utf-8")
            runtime_root = project_dir / DEVELOPMENT_RUNTIME_ROOT_NAME
            session_names = [
                "开发工程师-天魁星",
                "测试工程师-天英星",
            ]
            for index, session_name in enumerate(session_names, start=1):
                runtime_dir = runtime_root / f"worker-{index}"
                runtime_dir.mkdir(parents=True, exist_ok=True)
                (runtime_dir / "worker.state.json").write_text(
                    json.dumps(
                        {
                            "worker_id": "development-developer" if index == 1 else "development-review-测试工程师",
                            "session_name": session_name,
                            "work_dir": str(project_dir),
                            "result_status": "running",
                            "workflow_stage": "turn_running",
                            "agent_state": "BUSY",
                            "health_status": "alive",
                            "retry_count": 0,
                            "note": "",
                            "transcript_path": "",
                            "updated_at": "2026-04-21T10:00:00+08:00",
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._tmux_runtime.session_exists = lambda name: True  # noqa: SLF001
            server._set_context(project_dir=str(project_dir), requirement_name="贪吃蛇", action="stage.a07.start")  # noqa: SLF001

            development = server._build_development_snapshot()  # noqa: SLF001

        self.assertEqual([worker["session_name"] for worker in development["workers"]], session_names)
        self.assertTrue(any(item["label"] == "任务单" for item in development["files"]))
        self.assertTrue(any(item["label"] == "与人类交流" for item in development["files"]))
        self.assertEqual(development["current_milestone_key"], "M2")
        self.assertFalse(development["all_tasks_completed"])
        self.assertEqual([item["key"] for item in development["milestones"]], ["M1", "M2"])
        self.assertEqual(
            development["milestones"][1]["tasks"],
            [
                {"key": "M2-T1", "completed": False},
                {"key": "M2-T2", "completed": True},
            ],
        )

    def test_build_development_snapshot_omits_milestones_when_task_json_invalid(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            paths = build_development_paths(project_dir, "贪吃蛇")
            for file_path, content in (
                (paths["task_md_path"], "任务单\n"),
                (paths["task_json_path"], json.dumps({"M1": {"M1-T1": "invalid"}}, ensure_ascii=False, indent=2)),
                (paths["detailed_design_path"], "详细设计\n"),
            ):
                file_path.parent.mkdir(parents=True, exist_ok=True)
                file_path.write_text(content, encoding="utf-8")
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._tmux_runtime.session_exists = lambda session_name: session_name == "开发工程师-天魁星"  # noqa: SLF001
            server._set_context(project_dir=str(project_dir), requirement_name="贪吃蛇", action="stage.a07.start")  # noqa: SLF001

            development = server._build_development_snapshot()  # noqa: SLF001

        self.assertIn("task_json_invalid", development["blockers"])
        self.assertEqual(development["milestones"], [])
        self.assertEqual(development["current_milestone_key"], "")
        self.assertFalse(development["all_tasks_completed"])

    def test_overall_review_snapshot_lists_workers_files_and_blockers(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            paths = build_overall_review_paths(project_dir, "贪吃蛇")
            for file_path, content in (
                (paths["original_requirement_path"], "原始需求\n"),
                (paths["requirements_clear_path"], "需求澄清\n"),
                (paths["task_md_path"], "任务单\n"),
                (paths["task_json_path"], json.dumps({"M1": {"M1-T1": True}}, ensure_ascii=False, indent=2)),
                (paths["developer_output_path"], "工程师开发内容\n"),
                (paths["merged_review_path"], "合并复核记录\n"),
                (paths["detailed_design_path"], "详细设计\n"),
            ):
                file_path.parent.mkdir(parents=True, exist_ok=True)
                file_path.write_text(content, encoding="utf-8")
            runtime_root = project_dir / DEVELOPMENT_RUNTIME_ROOT_NAME
            session_names = [
                "开发工程师-天魁星",
                "测试工程师-天英星",
            ]
            for index, session_name in enumerate(session_names, start=1):
                runtime_dir = runtime_root / f"worker-a08-{index}"
                runtime_dir.mkdir(parents=True, exist_ok=True)
                (runtime_dir / "worker.state.json").write_text(
                    json.dumps(
                        {
                            "worker_id": "development-developer" if index == 1 else "development-review-测试工程师",
                            "session_name": session_name,
                            "work_dir": str(project_dir),
                            "result_status": "running",
                            "workflow_stage": "turn_running",
                            "workflow_action": "stage.a08.start",
                            "agent_state": "BUSY",
                            "health_status": "alive",
                            "retry_count": 0,
                            "note": "",
                            "transcript_path": "",
                            "updated_at": "2026-04-23T10:00:00+08:00",
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._tmux_runtime.session_exists = lambda name: True  # noqa: SLF001
            server._set_context(project_dir=str(project_dir), requirement_name="贪吃蛇", action="stage.a08.start")  # noqa: SLF001

            overall_review = server._build_overall_review_snapshot()  # noqa: SLF001

        self.assertEqual([worker["session_name"] for worker in overall_review["workers"]], session_names)
        self.assertTrue(any(item["label"] == "任务单 JSON" for item in overall_review["files"]))
        self.assertTrue(any(item["label"] == "复核完成状态" for item in overall_review["files"]))
        self.assertEqual(overall_review["blockers"], ["overall_review_not_passed"])

    def test_hitl_snapshot_prefers_development_question_when_a07_is_active(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            paths = build_development_paths(project_dir, "贪吃蛇")
            paths["ask_human_path"].parent.mkdir(parents=True, exist_ok=True)
            paths["ask_human_path"].write_text("请确认数据库字段映射\n", encoding="utf-8")
            paths["hitl_record_path"].write_text("历史回复\n", encoding="utf-8")
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._tmux_runtime.session_exists = lambda session_name: session_name == "开发工程师-天魁星"  # noqa: SLF001
            server._set_context(project_dir=str(project_dir), requirement_name="贪吃蛇", action="stage.a07.start")  # noqa: SLF001

            hitl = server._build_hitl_snapshot()  # noqa: SLF001

        self.assertTrue(hitl["pending"])
        self.assertEqual(hitl["question_path"], str(paths["ask_human_path"]))

    def test_hitl_snapshot_prefers_development_question_even_without_current_action(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            paths = build_development_paths(project_dir, "贪吃蛇")
            paths["ask_human_path"].parent.mkdir(parents=True, exist_ok=True)
            paths["ask_human_path"].write_text("请确认任务边界\n", encoding="utf-8")
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._set_context(project_dir=str(project_dir), requirement_name="贪吃蛇", action="idle")  # noqa: SLF001

            hitl = server._build_hitl_snapshot()  # noqa: SLF001

        self.assertTrue(hitl["pending"])
        self.assertEqual(hitl["question_path"], str(paths["ask_human_path"]))

    def test_stage_a07_status_prefers_running_worker_over_failed_worker(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            runtime_root = project_dir / DEVELOPMENT_RUNTIME_ROOT_NAME
            for worker_name, status, updated_at in (
                ("worker-failed", "failed", "2026-04-21T09:00:00+08:00"),
                ("worker-running", "running", "2026-04-21T10:00:00+08:00"),
            ):
                runtime_dir = runtime_root / worker_name
                runtime_dir.mkdir(parents=True, exist_ok=True)
                (runtime_dir / "worker.state.json").write_text(
                    json.dumps(
                        {
                            "worker_id": "development-review-架构师" if "failed" in worker_name else "development-developer",
                            "session_name": "架构师-天机星" if "failed" in worker_name else "开发工程师-天魁星",
                            "work_dir": str(project_dir),
                            "result_status": status,
                            "workflow_stage": "turn_running",
                            "agent_state": "BUSY",
                            "health_status": "alive",
                            "note": "",
                            "updated_at": updated_at,
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._tmux_runtime.session_exists = lambda session_name: session_name == "开发工程师-天魁星"  # noqa: SLF001
            server._tmux_runtime.backend.session_exists = lambda session_name: session_name == "开发工程师-天魁星"  # noqa: SLF001
            server._set_context(project_dir=str(project_dir), requirement_name="贪吃蛇", action="stage.a07.start")  # noqa: SLF001
            with patch("tmux_core.bridge.backend.load_worker_from_state_path", return_value=None):
                status = server._infer_runtime_stage_status("stage.a07.start")  # noqa: SLF001

        self.assertEqual(status, "running")

    def test_stage_a07_status_reports_failed_when_any_current_worker_failed_and_none_running(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            runtime_root = project_dir / DEVELOPMENT_RUNTIME_ROOT_NAME
            for worker_name, session_name, status, updated_at in (
                ("worker-failed", "需求分析师-虚日鼠", "failed", "2026-04-21T09:00:00+08:00"),
                ("worker-succeeded", "开发工程师-天魁星", "succeeded", "2026-04-21T10:00:00+08:00"),
            ):
                runtime_dir = runtime_root / worker_name
                runtime_dir.mkdir(parents=True, exist_ok=True)
                (runtime_dir / "worker.state.json").write_text(
                    json.dumps(
                        {
                            "worker_id": "development-review-需求分析师" if "failed" in worker_name else "development-developer",
                            "session_name": session_name,
                            "work_dir": str(project_dir),
                            "result_status": status,
                            "workflow_stage": "pending",
                            "agent_state": "DEAD" if status == "failed" else "READY",
                            "health_status": "alive",
                            "note": "error:development_reviewer_init_需求分析师" if status == "failed" else "done:development_developer_init",
                            "updated_at": updated_at,
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._set_context(project_dir=str(project_dir), requirement_name="贪吃蛇", action="stage.a07.start")  # noqa: SLF001

            status = server._infer_runtime_stage_status("stage.a07.start")  # noqa: SLF001

        self.assertEqual(status, "failed")

    def test_stage_a07_runtime_state_change_ready_workers_do_not_recover_running_when_tasks_remain(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            paths = build_development_paths(project_dir, "需求A")
            paths["task_json_path"].parent.mkdir(parents=True, exist_ok=True)
            paths["task_json_path"].write_text(
                json.dumps({"M1": {"M1-T1": False}}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            runtime_root = project_dir / DEVELOPMENT_RUNTIME_ROOT_NAME
            for worker_name, worker_id, session_name in (
                ("worker-developer", "development-developer", "开发工程师-天魁星"),
                ("worker-reviewer", "development-review-审核员", "审核员-天伤星"),
            ):
                runtime_dir = runtime_root / worker_name
                runtime_dir.mkdir(parents=True, exist_ok=True)
                (runtime_dir / "worker.state.json").write_text(
                    json.dumps(
                        {
                            "worker_id": worker_id,
                            "session_name": session_name,
                            "work_dir": str(project_dir),
                            "project_dir": str(project_dir.resolve()),
                            "requirement_name": "需求A",
                            "workflow_action": "stage.a07.start",
                            "result_status": "succeeded",
                            "agent_state": "READY",
                            "agent_started": True,
                            "agent_alive": True,
                            "current_command": "codex",
                            "health_status": "alive",
                            "updated_at": "2026-04-24T12:00:00+08:00",
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
            writer = io.StringIO()
            server = TuiBackendServer(reader=io.StringIO(), writer=writer)
            server._tmux_runtime.session_exists = lambda _session_name: True  # noqa: SLF001
            server._tmux_runtime.backend.session_exists = lambda _session_name: True  # noqa: SLF001
            server._set_context(project_dir=str(project_dir), requirement_name="需求A", action="stage.a07.start")  # noqa: SLF001
            server._display_action = "stage.a07.start"  # noqa: SLF001
            server._display_status = "failed"  # noqa: SLF001
            server._display_stage_seq = 7  # noqa: SLF001
            server._stage_seq_counter = 7  # noqa: SLF001

            with patch("tmux_core.bridge.backend.load_worker_from_state_path", return_value=None):
                server._bridge_ui.notify_runtime_state_changed()  # noqa: SLF001
                server._flush_dirty_snapshots()  # noqa: SLF001
                messages = [json.loads(line) for line in writer.getvalue().splitlines() if line.strip()]

            stage_events = [item for item in messages if item.get("kind") == "event" and item.get("type") == "stage.changed"]
            state_path = project_dir / ".tmux_workflow" / "需求A" / "stages" / "stage_a07_start.state.json"

        self.assertFalse(stage_events)
        self.assertFalse(state_path.exists())

    def test_stage_a07_failed_live_reviewer_with_current_review_output_does_not_fail_stage(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            paths = build_development_paths(project_dir, "需求A")
            paths["task_json_path"].parent.mkdir(parents=True, exist_ok=True)
            paths["task_json_path"].write_text(
                json.dumps({"M1": {"M1-T1": False}}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            _review_md_path, review_json_path = build_reviewer_artifact_paths(project_dir, "需求A", "测试工程师-天寿星")
            review_json_path.write_text(
                json.dumps([{"task_name": "M1-T1", "review_pass": True}], ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            runtime_dir = project_dir / DEVELOPMENT_RUNTIME_ROOT_NAME / "worker-reviewer"
            runtime_dir.mkdir(parents=True, exist_ok=True)
            (runtime_dir / "worker.state.json").write_text(
                json.dumps(
                    {
                        "worker_id": "development-review-测试工程师",
                        "session_name": "测试工程师-天寿星",
                        "work_dir": str(project_dir),
                        "project_dir": str(project_dir.resolve()),
                        "requirement_name": "需求A",
                        "workflow_action": "stage.a07.start",
                        "status": "failed",
                        "result_status": "failed",
                        "agent_state": "READY",
                        "agent_started": True,
                        "health_status": "alive",
                        "current_turn_status_path": str(review_json_path),
                        "note": "error:development_review_init_M1-T1_测试工程师_round_1_repair_1",
                        "updated_at": "2026-04-24T12:00:00+08:00",
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._tmux_runtime.session_exists = lambda _session_name: True  # noqa: SLF001
            server._tmux_runtime.backend.session_exists = lambda _session_name: True  # noqa: SLF001
            server._set_context(project_dir=str(project_dir), requirement_name="需求A", action="stage.a07.start")  # noqa: SLF001

            with patch("tmux_core.bridge.backend.load_worker_from_state_path", return_value=None):
                status = server._infer_runtime_stage_status("stage.a07.start")  # noqa: SLF001
                failed_summaries = server._failed_stage_worker_summaries("stage.a07.start")  # noqa: SLF001

        self.assertEqual(status, "")
        self.assertEqual(failed_summaries, [])

    def test_stage_a07_busy_worker_with_stale_failed_status_keeps_stage_running(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            paths = build_development_paths(project_dir, "需求A")
            paths["task_json_path"].parent.mkdir(parents=True, exist_ok=True)
            paths["task_json_path"].write_text(
                json.dumps({"M1": {"M1-T1": False}}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            runtime_root = project_dir / DEVELOPMENT_RUNTIME_ROOT_NAME
            for worker_name, payload in (
                (
                    "worker-developer",
                    {
                        "worker_id": "development-developer",
                        "session_name": "开发工程师-地威星",
                        "status": "failed",
                        "result_status": "failed",
                        "agent_state": "BUSY",
                        "current_task_runtime_status": "running",
                        "health_status": "alive",
                        "workflow_action": "stage.a07.start",
                        "requirement_name": "需求A",
                        "project_dir": str(project_dir.resolve()),
                    },
                ),
                (
                    "worker-reviewer",
                    {
                        "worker_id": "development-review-审核员",
                        "session_name": "审核员-轸水蚓",
                        "status": "failed",
                        "result_status": "failed",
                        "agent_state": "READY",
                        "current_task_runtime_status": "",
                        "health_status": "alive",
                        "workflow_action": "stage.a07.start",
                        "requirement_name": "需求A",
                        "project_dir": str(project_dir.resolve()),
                    },
                ),
            ):
                runtime_dir = runtime_root / worker_name
                runtime_dir.mkdir(parents=True, exist_ok=True)
                (runtime_dir / "worker.state.json").write_text(
                    json.dumps(
                        {
                            "work_dir": str(project_dir),
                            "agent_started": True,
                            "updated_at": "2026-05-12T10:00:00+08:00",
                            **payload,
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._tmux_runtime.session_exists = lambda _session_name: True  # noqa: SLF001
            server._tmux_runtime.backend.session_exists = lambda _session_name: True  # noqa: SLF001
            server._set_context(project_dir=str(project_dir), requirement_name="需求A", action="stage.a07.start")  # noqa: SLF001

            with patch("tmux_core.bridge.backend.load_worker_from_state_path", return_value=None):
                status = server._infer_runtime_stage_status("stage.a07.start")  # noqa: SLF001
                failed_summaries = server._failed_stage_worker_summaries("stage.a07.start")  # noqa: SLF001

        self.assertEqual(status, "running")
        self.assertEqual(failed_summaries, ["审核员-轸水蚓: failed"])

    def test_stage_a07_ready_workers_with_pending_tasks_are_not_inferred_running(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            paths = build_development_paths(project_dir, "需求A")
            paths["task_json_path"].parent.mkdir(parents=True, exist_ok=True)
            paths["task_json_path"].write_text(
                json.dumps({"M5": {"M5-T11": False}}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            runtime_root = project_dir / DEVELOPMENT_RUNTIME_ROOT_NAME
            for worker_name, worker_id, session_name in (
                ("worker-developer", "development-developer", "开发工程师-地雄星"),
                ("worker-reviewer", "development-review-审核员", "审核员-翼火蛇"),
            ):
                runtime_dir = runtime_root / worker_name
                runtime_dir.mkdir(parents=True, exist_ok=True)
                (runtime_dir / "worker.state.json").write_text(
                    json.dumps(
                        {
                            "worker_id": worker_id,
                            "session_name": session_name,
                            "work_dir": str(project_dir),
                            "project_dir": str(project_dir),
                            "requirement_name": "需求A",
                            "workflow_action": "stage.a07.start",
                            "status": "ready",
                            "result_status": "ready",
                            "workflow_stage": "pending",
                            "agent_state": "READY",
                            "agent_started": True,
                            "health_status": "alive",
                            "health_note": "alive",
                            "current_task_runtime_status": "",
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._tmux_runtime.session_exists = lambda _session_name: True  # noqa: SLF001
            server._set_context(project_dir=str(project_dir), requirement_name="需求A", action="stage.a07.start")  # noqa: SLF001

            with patch("tmux_core.bridge.backend.load_worker_from_state_path", return_value=None):
                status = server._infer_runtime_stage_status("stage.a07.start")  # noqa: SLF001

        self.assertEqual(status, "")

    def test_stage_a07_ready_worker_with_submitted_task_contract_stays_ready(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            paths = build_development_paths(project_dir, "需求A")
            paths["task_json_path"].parent.mkdir(parents=True, exist_ok=True)
            paths["task_json_path"].write_text(
                json.dumps({"M5": {"M5-T1": False}}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            runtime_dir = project_dir / DEVELOPMENT_RUNTIME_ROOT_NAME / "worker-developer"
            runtime_dir.mkdir(parents=True, exist_ok=True)
            task_runtime_dir = runtime_dir / "task_runtime"
            task_runtime_dir.mkdir(parents=True, exist_ok=True)
            (runtime_dir / "worker.state.json").write_text(
                json.dumps(
                    {
                        "worker_id": "development-developer",
                        "session_name": "开发工程师-地雄星",
                        "work_dir": str(project_dir),
                        "project_dir": str(project_dir),
                        "requirement_name": "需求A",
                        "workflow_action": "stage.a07.start",
                        "status": "running",
                        "result_status": "running",
                        "workflow_stage": "pending",
                        "agent_state": "READY",
                        "agent_started": True,
                        "health_status": "alive",
                        "health_note": "alive",
                        "current_task_runtime_status": "running",
                        "current_task_status_path": str(task_runtime_dir / "developer_attempt_1.json"),
                        "current_task_result_path": str(task_runtime_dir / "developer_attempt_1_result.json"),
                        "dispatch_state": "submitted",
                        "note": "submitted:development_start_M5-T1",
                        "updated_at": "2026-05-14T17:24:41+08:00",
                        "last_heartbeat_at": "2026-05-14T17:24:41+08:00",
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._tmux_runtime = SimpleNamespace(session_exists=lambda _session_name: True, backend=None)  # noqa: SLF001
            server._set_context(project_dir=str(project_dir), requirement_name="需求A", action="stage.a07.start")  # noqa: SLF001

            with patch("tmux_core.bridge.backend.load_worker_from_state_path", return_value=None):
                snapshot = server._build_development_snapshot()  # noqa: SLF001
                status = server._infer_runtime_stage_status("stage.a07.start")  # noqa: SLF001

        self.assertEqual(snapshot["workers"][0]["agent_state"], "READY")
        self.assertEqual(snapshot["workers"][0]["current_task_runtime_status"], "running")
        self.assertEqual(snapshot["workers"][0]["dispatch_state"], "submitted")
        self.assertEqual(status, "running")

    def test_stage_a07_stale_ready_missing_task_result_is_not_displayed_as_running(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            paths = build_development_paths(project_dir, "需求A")
            paths["task_json_path"].parent.mkdir(parents=True, exist_ok=True)
            paths["task_json_path"].write_text(
                json.dumps({"M6": {"M6-T1": False}}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            runtime_dir = project_dir / DEVELOPMENT_RUNTIME_ROOT_NAME / "worker-developer"
            runtime_dir.mkdir(parents=True, exist_ok=True)
            task_runtime_dir = runtime_dir / "task_runtime"
            task_runtime_dir.mkdir(parents=True, exist_ok=True)
            task_status_path = task_runtime_dir / "developer_attempt_1.json"
            task_status_path.write_text('{"status": "running"}', encoding="utf-8")
            task_result_path = task_runtime_dir / "developer_attempt_1_result.json"
            (runtime_dir / "worker.state.json").write_text(
                json.dumps(
                    {
                        "worker_id": "development-developer",
                        "session_name": "开发工程师-地英星",
                        "work_dir": str(project_dir),
                        "project_dir": str(project_dir),
                        "requirement_name": "需求A",
                        "workflow_action": "stage.a07.start",
                        "status": "ready",
                        "result_status": "ready",
                        "workflow_stage": "pending",
                        "agent_state": "READY",
                        "agent_started": True,
                        "health_status": "alive",
                        "current_task_runtime_status": "",
                        "current_task_status_path": str(task_status_path),
                        "current_task_result_path": str(task_result_path),
                        "dispatch_state": "",
                        "note": "still_running:development_start_M6-T1",
                        "updated_at": "2026-05-19T13:15:54+08:00",
                        "last_heartbeat_at": "2026-05-19T13:15:54+08:00",
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._tmux_runtime.session_exists = lambda _session_name: True  # noqa: SLF001
            server._tmux_runtime.backend.session_exists = lambda _session_name: True  # noqa: SLF001
            server._set_context(project_dir=str(project_dir), requirement_name="需求A", action="stage.a07.start")  # noqa: SLF001
            server._display_action = "stage.a07.start"  # noqa: SLF001
            server._display_status = "running"  # noqa: SLF001
            server._display_source = "runtime_inference"  # noqa: SLF001
            server._display_stage_seq = 8  # noqa: SLF001

            with patch("tmux_core.bridge.backend.load_worker_from_state_path", return_value=None):
                snapshot = server._build_development_snapshot()  # noqa: SLF001
                status = server._infer_runtime_stage_status("stage.a07.start")  # noqa: SLF001
                app = server._build_app_snapshot(  # noqa: SLF001
                    runs=[],
                    control={"workers": [], "run_id": ""},
                    hitl={"pending": False},
                    attention={"pending": False},
                    artifacts={"items": []},
                    stage_snapshots={"development": snapshot},
                )

        self.assertEqual(snapshot["workers"][0]["agent_state"], "READY")
        self.assertEqual(snapshot["workers"][0]["note"], "still_running:development_start_M6-T1")
        self.assertEqual(status, "failed")
        self.assertEqual(app["active_stage_status"], "failed")

    def test_stage_a07_runtime_state_change_keeps_failed_when_tasks_remain_but_session_is_gone(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            paths = build_development_paths(project_dir, "需求A")
            paths["task_json_path"].parent.mkdir(parents=True, exist_ok=True)
            paths["task_json_path"].write_text(
                json.dumps({"M1": {"M1-T1": False}}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            runtime_dir = project_dir / DEVELOPMENT_RUNTIME_ROOT_NAME / "worker-reviewer"
            runtime_dir.mkdir(parents=True, exist_ok=True)
            (runtime_dir / "worker.state.json").write_text(
                json.dumps(
                    {
                        "worker_id": "development-review-审核员",
                        "session_name": "审核员-天伤星",
                        "work_dir": str(project_dir),
                        "project_dir": str(project_dir.resolve()),
                        "requirement_name": "需求A",
                        "workflow_action": "stage.a07.start",
                        "result_status": "succeeded",
                        "agent_state": "READY",
                        "agent_started": True,
                        "agent_alive": True,
                        "current_command": "codex",
                        "health_status": "alive",
                        "updated_at": "2026-04-24T12:00:00+08:00",
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            writer = io.StringIO()
            server = TuiBackendServer(reader=io.StringIO(), writer=writer)
            server._tmux_runtime.session_exists = lambda _session_name: False  # noqa: SLF001
            server._tmux_runtime.backend.session_exists = lambda _session_name: False  # noqa: SLF001
            server._set_context(project_dir=str(project_dir), requirement_name="需求A", action="stage.a07.start")  # noqa: SLF001
            server._display_action = "stage.a07.start"  # noqa: SLF001
            server._display_status = "failed"  # noqa: SLF001
            server._display_stage_seq = 7  # noqa: SLF001
            server._stage_seq_counter = 7  # noqa: SLF001

            with patch("tmux_core.bridge.backend.load_worker_from_state_path", return_value=None):
                server._bridge_ui.notify_runtime_state_changed()  # noqa: SLF001
                messages = [json.loads(line) for line in writer.getvalue().splitlines() if line.strip()]
                action, status, _stage_seq = server._derive_display_stage_state(  # noqa: SLF001
                    preferred_status="failed",
                    preferred_action="stage.a07.start",
                )

        stage_events = [item for item in messages if item.get("kind") == "event" and item.get("type") == "stage.changed"]
        self.assertEqual(action, "stage.a07.start")
        self.assertEqual(status, "failed")
        self.assertFalse(
            any(
                item.get("payload", {}).get("action") == "stage.a07.start"
                and item.get("payload", {}).get("status") == "running"
                for item in stage_events
            )
        )

    def test_stage_a08_status_does_not_treat_succeeded_reviewers_as_running_when_developer_dead(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            runtime_root = project_dir / DEVELOPMENT_RUNTIME_ROOT_NAME
            for worker_name, session_name, worker_id, status, agent_state, health_status in (
                ("worker-developer", "开发工程师-地默星", "development-developer", "running", "DEAD", "dead"),
                ("worker-reviewer", "审核员-地阖星", "development-review-审核员", "succeeded", "READY", "alive"),
            ):
                runtime_dir = runtime_root / worker_name
                runtime_dir.mkdir(parents=True, exist_ok=True)
                (runtime_dir / "worker.state.json").write_text(
                    json.dumps(
                        {
                            "worker_id": worker_id,
                            "session_name": session_name,
                            "work_dir": str(project_dir),
                            "project_dir": str(project_dir.resolve()),
                            "requirement_name": "需求A",
                            "workflow_action": "stage.a08.start",
                            "result_status": status,
                            "agent_state": agent_state,
                            "health_status": health_status,
                            "updated_at": "2026-04-24T12:00:00+08:00",
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._tmux_runtime = SimpleNamespace(session_exists=lambda name: False)  # noqa: SLF001
            server._set_context(project_dir=str(project_dir), requirement_name="需求A", action="stage.a08.start")  # noqa: SLF001

            status = server._infer_runtime_stage_status("stage.a08.start")  # noqa: SLF001

        self.assertEqual(status, "failed")

    def test_stage_a08_status_stays_failed_when_review_state_not_passed_and_workers_terminal(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            runtime_root = project_dir / DEVELOPMENT_RUNTIME_ROOT_NAME
            runtime_dir = runtime_root / "worker-reviewer"
            runtime_dir.mkdir(parents=True, exist_ok=True)
            (runtime_dir / "worker.state.json").write_text(
                json.dumps(
                    {
                        "worker_id": "development-review-审核员",
                        "session_name": "审核员-天伤星",
                        "work_dir": str(project_dir),
                        "project_dir": str(project_dir.resolve()),
                        "requirement_name": "需求A",
                        "workflow_action": "stage.a08.start",
                        "result_status": "succeeded",
                        "agent_state": "READY",
                        "agent_started": True,
                        "agent_alive": True,
                        "current_command": "codex",
                        "health_status": "alive",
                        "updated_at": "2026-04-24T12:00:00+08:00",
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._tmux_runtime.session_exists = lambda _session_name: True  # noqa: SLF001
            server._tmux_runtime.backend.session_exists = lambda _session_name: True  # noqa: SLF001
            server._set_context(project_dir=str(project_dir), requirement_name="需求A", action="stage.a08.start")  # noqa: SLF001
            server._display_status = "failed"  # noqa: SLF001

            with patch("tmux_core.bridge.backend.load_worker_from_state_path", return_value=None):
                action, status, _stage_seq = server._derive_display_stage_state(  # noqa: SLF001
                    preferred_status="failed",
                    preferred_action="stage.a08.start",
                )

        self.assertEqual(action, "stage.a08.start")
        self.assertEqual(status, "failed")

    def test_stage_a08_completed_state_is_not_overridden_by_residual_live_worker(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            paths = build_overall_review_paths(project_dir, "需求A")
            paths["task_json_path"].parent.mkdir(parents=True, exist_ok=True)
            paths["task_json_path"].write_text(
                json.dumps({"M1": {"M1-T1": True}}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            paths["state_path"].write_text(
                json.dumps({"passed": True}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            runtime_dir = project_dir / DEVELOPMENT_RUNTIME_ROOT_NAME / "worker-reviewer"
            runtime_dir.mkdir(parents=True, exist_ok=True)
            (runtime_dir / "worker.state.json").write_text(
                json.dumps(
                    {
                        "worker_id": "development-review-审核员",
                        "session_name": "审核员-地阖星",
                        "work_dir": str(project_dir),
                        "project_dir": str(project_dir.resolve()),
                        "requirement_name": "需求A",
                        "workflow_action": "stage.a08.start",
                        "result_status": "succeeded",
                        "agent_state": "READY",
                        "agent_started": True,
                        "agent_alive": True,
                        "current_command": "codex",
                        "health_status": "alive",
                        "updated_at": "2026-04-24T12:00:00+08:00",
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._tmux_runtime.session_exists = lambda _session_name: True  # noqa: SLF001
            server._tmux_runtime.backend.session_exists = lambda _session_name: True  # noqa: SLF001
            server._set_context(project_dir=str(project_dir), requirement_name="需求A", action="stage.a08.start")  # noqa: SLF001
            server._display_status = "completed"  # noqa: SLF001

            with patch("tmux_core.bridge.backend.load_worker_from_state_path", return_value=None):
                runtime_status = server._infer_runtime_stage_status("stage.a08.start")  # noqa: SLF001
                action, status, _stage_seq = server._derive_display_stage_state(  # noqa: SLF001
                    preferred_status="completed",
                    preferred_action="stage.a08.start",
                )

        self.assertEqual(runtime_status, "")
        self.assertEqual(action, "stage.a08.start")
        self.assertEqual(status, "completed")

    def test_stage_a08_terminal_stale_busy_workers_fail_pending_contract_instead_of_running(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            paths = build_overall_review_paths(project_dir, "需求A")
            paths["task_json_path"].parent.mkdir(parents=True, exist_ok=True)
            paths["task_json_path"].write_text(
                json.dumps({"M1": {"M1-T1": True}}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            paths["state_path"].write_text(
                json.dumps({"passed": False}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            runtime_root = project_dir / DEVELOPMENT_RUNTIME_ROOT_NAME
            for worker_name, session_name, agent_state in (
                ("worker-analyst", "需求分析师-地英星", "BUSY"),
                ("worker-reviewer", "测试工程师-天慧星", "READY"),
            ):
                runtime_dir = runtime_root / worker_name
                runtime_dir.mkdir(parents=True, exist_ok=True)
                (runtime_dir / "worker.state.json").write_text(
                    json.dumps(
                        {
                            "worker_id": "development-review-需求分析师" if "analyst" in worker_name else "development-review-测试工程师",
                            "session_name": session_name,
                            "work_dir": str(project_dir),
                            "project_dir": str(project_dir.resolve()),
                            "requirement_name": "需求A",
                            "workflow_action": "stage.a08.start",
                            "status": "succeeded",
                            "result_status": "succeeded",
                            "workflow_stage": "pending",
                            "agent_state": agent_state,
                            "agent_started": True,
                            "agent_alive": True,
                            "current_task_runtime_status": "done",
                            "health_status": "alive",
                            "updated_at": "2026-05-12T15:14:25+08:00",
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._tmux_runtime.session_exists = lambda _session_name: True  # noqa: SLF001
            server._tmux_runtime.backend.session_exists = lambda _session_name: True  # noqa: SLF001
            server._set_context(project_dir=str(project_dir), requirement_name="需求A", action="stage.a08.start")  # noqa: SLF001

            with patch("tmux_core.bridge.backend.load_worker_from_state_path", return_value=None):
                workers = server._current_stage_workers("stage.a08.start")  # noqa: SLF001
                runtime_status = server._infer_runtime_stage_status("stage.a08.start")  # noqa: SLF001

        self.assertEqual(runtime_status, "failed")
        self.assertEqual(workers[0]["agent_state"], "BUSY")

    def test_stage_a08_running_worker_still_keeps_pending_contract_running(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            paths = build_overall_review_paths(project_dir, "需求A")
            paths["task_json_path"].parent.mkdir(parents=True, exist_ok=True)
            paths["task_json_path"].write_text(
                json.dumps({"M1": {"M1-T1": True}}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            paths["state_path"].write_text(
                json.dumps({"passed": False}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            runtime_dir = project_dir / DEVELOPMENT_RUNTIME_ROOT_NAME / "worker-reviewer"
            runtime_dir.mkdir(parents=True, exist_ok=True)
            (runtime_dir / "worker.state.json").write_text(
                json.dumps(
                    {
                        "worker_id": "development-review-测试工程师",
                        "session_name": "测试工程师-天慧星",
                        "work_dir": str(project_dir),
                        "project_dir": str(project_dir.resolve()),
                        "requirement_name": "需求A",
                        "workflow_action": "stage.a08.start",
                        "status": "running",
                        "result_status": "running",
                        "workflow_stage": "turn_running",
                        "agent_state": "BUSY",
                        "agent_started": True,
                        "current_task_runtime_status": "running",
                        "health_status": "alive",
                        "updated_at": "2026-05-12T15:14:25+08:00",
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._tmux_runtime.session_exists = lambda _session_name: True  # noqa: SLF001
            server._tmux_runtime.backend.session_exists = lambda _session_name: True  # noqa: SLF001
            server._set_context(project_dir=str(project_dir), requirement_name="需求A", action="stage.a08.start")  # noqa: SLF001

            with patch("tmux_core.bridge.backend.load_worker_from_state_path", return_value=None):
                runtime_status = server._infer_runtime_stage_status("stage.a08.start")  # noqa: SLF001

        self.assertEqual(runtime_status, "running")

    def test_failed_stage_worker_summaries_filter_to_current_requirement_and_action(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            runtime_root = project_dir / DEVELOPMENT_RUNTIME_ROOT_NAME
            for worker_name, requirement_name, workflow_action, session_name, note in (
                ("worker-current", "需求A", "stage.a07.start", "需求分析师-虚日鼠", "error:reqA"),
                ("worker-other-requirement", "需求B", "stage.a07.start", "审核员-地阖星", "error:reqB"),
                ("worker-other-action", "需求A", "stage.a08.start", "架构师-鬼金羊", "error:a08"),
            ):
                runtime_dir = runtime_root / worker_name
                runtime_dir.mkdir(parents=True, exist_ok=True)
                (runtime_dir / "worker.state.json").write_text(
                    json.dumps(
                        {
                            "worker_id": "development-review-测试",
                            "session_name": session_name,
                            "work_dir": str(project_dir),
                            "project_dir": str(project_dir.resolve()),
                            "requirement_name": requirement_name,
                            "workflow_action": workflow_action,
                            "result_status": "failed",
                            "health_status": "alive",
                            "note": note,
                            "updated_at": "2026-04-24T12:00:00+08:00",
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._set_context(project_dir=str(project_dir), requirement_name="需求A", action="stage.a07.start")  # noqa: SLF001

            summaries = server._failed_stage_worker_summaries("stage.a07.start")  # noqa: SLF001

        self.assertEqual(summaries, ["需求分析师-虚日鼠: error:reqA"])

    def test_stage_a07_status_treats_session_created_worker_as_running(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            paths = build_development_paths(project_dir, "需求A")
            paths["task_json_path"].write_text(
                json.dumps({"M1": {"M1-T1": False}}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            runtime_dir = project_dir / DEVELOPMENT_RUNTIME_ROOT_NAME / "worker-starting"
            runtime_dir.mkdir(parents=True, exist_ok=True)
            (runtime_dir / "worker.state.json").write_text(
                json.dumps(
                    {
                        "worker_id": "development-developer",
                        "session_name": "开发工程师-地默星",
                        "work_dir": str(project_dir),
                        "result_status": "ready",
                        "workflow_stage": "pending",
                        "current_command": "zsh",
                        "agent_state": "STARTING",
                        "agent_started": False,
                        "health_status": "alive",
                        "note": "session_created",
                        "updated_at": "2026-04-22T10:00:00+08:00",
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._tmux_runtime.session_exists = lambda session_name: session_name == "开发工程师-地默星"  # noqa: SLF001
            server._set_context(project_dir=str(project_dir), requirement_name="需求A", action="stage.a07.start")  # noqa: SLF001

            with patch("tmux_core.bridge.backend.load_worker_from_state_path", return_value=None):
                snapshot = server._build_development_snapshot()  # noqa: SLF001
                status = server._infer_runtime_stage_status("stage.a07.start")  # noqa: SLF001

        self.assertEqual(snapshot["workers"][0]["agent_state"], "STARTING")
        self.assertEqual(status, "running")

    def test_stage_a07_session_created_worker_with_missing_session_stays_starting(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            paths = build_development_paths(project_dir, "需求A")
            paths["task_json_path"].write_text(
                json.dumps({"M1": {"M1-T1": False}}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            runtime_dir = project_dir / DEVELOPMENT_RUNTIME_ROOT_NAME / "worker-starting"
            runtime_dir.mkdir(parents=True, exist_ok=True)
            (runtime_dir / "worker.state.json").write_text(
                json.dumps(
                    {
                        "worker_id": "development-review-测试工程师",
                        "session_name": "测试工程师-天暴星",
                        "work_dir": str(project_dir),
                        "project_dir": str(project_dir),
                        "requirement_name": "需求A",
                        "workflow_action": "stage.a07.start",
                        "status": "ready",
                        "result_status": "running",
                        "workflow_stage": "pending",
                        "current_task_runtime_status": "running",
                        "pane_id": "%7",
                        "agent_state": "STARTING",
                        "agent_started": False,
                        "health_status": "alive",
                        "health_note": "alive",
                        "note": "session_created",
                        "updated_at": "2026-04-22T10:00:00+08:00",
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._tmux_runtime.session_exists = lambda _session_name: False  # noqa: SLF001
            server._set_context(project_dir=str(project_dir), requirement_name="需求A", action="stage.a07.start")  # noqa: SLF001

            with patch("tmux_core.bridge.backend.load_worker_from_state_path", return_value=None):
                snapshot = server._build_development_snapshot()  # noqa: SLF001
                status = server._infer_runtime_stage_status("stage.a07.start")  # noqa: SLF001

        self.assertEqual(snapshot["workers"][0]["agent_state"], "STARTING")
        self.assertEqual(snapshot["workers"][0]["health_status"], "unknown")
        self.assertEqual(snapshot["workers"][0]["health_note"], "launch pending")
        self.assertEqual(status, "running")

    def test_stage_a07_metadata_only_worker_without_pane_is_starting_not_dead(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            paths = build_development_paths(project_dir, "需求A")
            paths["task_json_path"].write_text(
                json.dumps({"M1": {"M1-T1": False}}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            runtime_dir = project_dir / DEVELOPMENT_RUNTIME_ROOT_NAME / "worker-starting-no-pane"
            runtime_dir.mkdir(parents=True, exist_ok=True)
            (runtime_dir / "worker.state.json").write_text(
                json.dumps(
                    {
                        "worker_id": "development-developer",
                        "session_name": "开发工程师-柳土獐",
                        "work_dir": str(project_dir),
                        "project_dir": str(project_dir),
                        "requirement_name": "需求A",
                        "workflow_action": "stage.a07.start",
                        "status": "running",
                        "result_status": "running",
                        "workflow_stage": "pending",
                        "pane_id": "",
                        "agent_state": "STARTING",
                        "agent_started": False,
                        "health_status": "unknown",
                        "health_note": "launch pending",
                        "note": "awaiting_reconfig",
                        "updated_at": "2026-04-22T10:00:00+08:00",
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._tmux_runtime.session_exists = lambda _session_name: False  # noqa: SLF001
            server._set_context(project_dir=str(project_dir), requirement_name="需求A", action="stage.a07.start")  # noqa: SLF001

            with patch("tmux_core.bridge.backend.load_worker_from_state_path", return_value=None):
                snapshot = server._build_development_snapshot()  # noqa: SLF001
                status = server._infer_runtime_stage_status("stage.a07.start")  # noqa: SLF001

        self.assertEqual(snapshot["workers"][0]["agent_state"], "STARTING")
        self.assertEqual(snapshot["workers"][0]["health_status"], "unknown")
        self.assertEqual(status, "running")

    def test_stage_snapshots_treat_prelaunch_dead_workers_as_starting_across_review_design_split_and_overall(self):
        cases = (
            ("stage.a04.start", REQUIREMENTS_REVIEW_RUNTIME_ROOT_NAME, "requirements-review-analyst", "_build_review_snapshot"),
            ("stage.a05.start", DETAILED_DESIGN_RUNTIME_ROOT_NAME, "detailed-design-analyst", "_build_design_snapshot"),
            ("stage.a06.start", TASK_SPLIT_RUNTIME_ROOT_NAME, "task-split-analyst", "_build_task_split_snapshot"),
            ("stage.a08.start", DEVELOPMENT_RUNTIME_ROOT_NAME, "development-review-审核员", "_build_overall_review_snapshot"),
        )
        for action, runtime_root_name, worker_id, builder_name in cases:
            with self.subTest(action=action), tempfile.TemporaryDirectory() as tmpdir:
                project_dir = Path(tmpdir)
                runtime_dir = project_dir / runtime_root_name / "worker-prelaunch"
                runtime_dir.mkdir(parents=True, exist_ok=True)
                (runtime_dir / "worker.state.json").write_text(
                    json.dumps(
                        {
                            "worker_id": worker_id,
                            "session_name": "预启动-虚日鼠",
                            "work_dir": str(project_dir),
                            "project_dir": str(project_dir),
                            "requirement_name": "需求A",
                            "workflow_action": action,
                            "status": "running",
                            "result_status": "running",
                            "workflow_stage": "pending",
                            "pane_id": "",
                            "agent_state": "DEAD",
                            "agent_started": False,
                            "health_status": "missing_session",
                            "health_note": "missing_session",
                            "note": "turn:init",
                            "updated_at": "2026-04-22T10:00:00+08:00",
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
                server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
                server._tmux_runtime.session_exists = lambda _session_name: False  # noqa: SLF001
                server._set_context(project_dir=str(project_dir), requirement_name="需求A", action=action)  # noqa: SLF001

                with patch("tmux_core.bridge.backend.load_worker_from_state_path", return_value=None):
                    snapshot = getattr(server, builder_name)()
                    status = server._infer_runtime_stage_status(action)  # noqa: SLF001

            self.assertEqual(snapshot["workers"][0]["agent_state"], "STARTING")
            self.assertEqual(snapshot["workers"][0]["health_status"], "unknown")
            self.assertNotEqual(status, "failed")

    def test_stage_a06_status_prefers_running_task_split_worker_over_failed_reviewers(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            runtime_root = project_dir / DETAILED_DESIGN_RUNTIME_ROOT_NAME

            failed_runtime_dir = runtime_root / "worker-failed"
            failed_runtime_dir.mkdir(parents=True, exist_ok=True)
            (failed_runtime_dir / "worker.state.json").write_text(
                json.dumps(
                    {
                        "worker_id": "detailed-design-review-测试工程师",
                        "session_name": "测试工程师-井木犴",
                        "work_dir": str(project_dir),
                        "result_status": "failed",
                        "workflow_stage": "pending",
                        "agent_state": "DEAD",
                        "health_status": "alive",
                        "note": "error:task_split_review_init_测试工程师_round_1",
                        "current_turn_phase": "任务拆分",
                        "updated_at": "2026-04-21T12:25:20+08:00",
                        "last_heartbeat_at": "2026-04-21T12:27:31+08:00",
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            running_runtime_dir = runtime_root / "worker-running"
            running_runtime_dir.mkdir(parents=True, exist_ok=True)
            (running_runtime_dir / "worker.state.json").write_text(
                json.dumps(
                    {
                        "worker_id": "detailed-design-review-架构师",
                        "session_name": "架构师-昴日鸡",
                        "work_dir": str(project_dir),
                        "result_status": "running",
                        "workflow_stage": "turn_running",
                        "agent_state": "BUSY",
                        "health_status": "alive",
                        "note": "turn:task_split_review_init_架构师_round_1",
                        "current_turn_phase": "任务拆分",
                        "updated_at": "2026-04-21T13:25:49+08:00",
                        "last_heartbeat_at": "2026-04-21T13:25:49+08:00",
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._tmux_runtime.session_exists = lambda session_name: session_name == "架构师-昴日鸡"  # noqa: SLF001
            server._tmux_runtime.backend.session_exists = lambda session_name: session_name == "架构师-昴日鸡"  # noqa: SLF001
            server._set_context(project_dir=str(project_dir), requirement_name="贪吃蛇", action="stage.a06.start")  # noqa: SLF001
            with patch("tmux_core.bridge.backend.load_worker_from_state_path", return_value=None):
                status = server._infer_runtime_stage_status("stage.a06.start")  # noqa: SLF001

        self.assertEqual(status, "running")

    def test_task_split_snapshot_filters_workers_from_previous_workflow_start(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            record_dir = project_dir / ".tmux_workflow" / "需求A" / "stages"
            record_dir.mkdir(parents=True)
            (record_dir / "workflow_a00_start.state.json").write_text(
                json.dumps(
                    {
                        "action": "workflow.a00.start",
                        "status": "running",
                        "project_dir": str(project_dir),
                        "requirement_name": "需求A",
                        "stage_seq": 1,
                        "source": "runner_start",
                        "updated_at": "2026-05-03T09:00:00+08:00",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            runtime_root = project_dir / DETAILED_DESIGN_RUNTIME_ROOT_NAME
            older_root = runtime_root / "worker-old"
            newer_root = runtime_root / "worker-new"
            older_root.mkdir(parents=True)
            newer_root.mkdir(parents=True)
            common = {
                "worker_id": "detailed-design-review-测试工程师",
                "work_dir": str(project_dir),
                "project_dir": str(project_dir),
                "requirement_name": "需求A",
                "workflow_action": "stage.a06.start",
                "result_status": "succeeded",
                "workflow_stage": "pending",
                "agent_state": "READY",
                "health_status": "alive",
            }
            (older_root / "worker.state.json").write_text(
                json.dumps(
                    {
                        **common,
                        "session_name": "测试工程师-旧",
                        "note": "done:task_split_review_again_round_2",
                        "updated_at": "2026-05-02T21:27:03+08:00",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            (newer_root / "worker.state.json").write_text(
                json.dumps(
                    {
                        **common,
                        "session_name": "测试工程师-新",
                        "result_status": "running",
                        "workflow_stage": "turn_running",
                        "agent_state": "BUSY",
                        "note": "turn:task_split_review_again_round_2",
                        "updated_at": "2026-05-03T09:27:03+08:00",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._tmux_runtime = SimpleNamespace(session_exists=lambda name: True)  # noqa: SLF001
            server._set_context(project_dir=str(project_dir), requirement_name="需求A", action="stage.a06.start")  # noqa: SLF001

            snapshot = server._build_task_split_snapshot()  # noqa: SLF001
            status = server._infer_runtime_stage_status("stage.a06.start")  # noqa: SLF001

        self.assertEqual([worker["session_name"] for worker in snapshot["workers"]], ["测试工程师-新"])
        self.assertEqual(status, "running")

    def test_stage_a06_status_ignores_failed_design_reviewer_outside_task_split_phase(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            runtime_root = project_dir / DETAILED_DESIGN_RUNTIME_ROOT_NAME / "worker-failed"
            runtime_root.mkdir(parents=True, exist_ok=True)
            (runtime_root / "worker.state.json").write_text(
                json.dumps(
                    {
                        "worker_id": "detailed-design-review-架构师",
                        "session_name": "架构师-箕水豹",
                        "work_dir": str(project_dir),
                        "result_status": "failed",
                        "workflow_stage": "pending",
                        "agent_state": "DEAD",
                        "health_status": "alive",
                        "note": "timeout:detailed_design_review_again_架构师_round_4",
                        "current_turn_phase": "详细设计",
                        "updated_at": "2026-04-21T12:55:37+08:00",
                        "last_heartbeat_at": "2026-04-21T12:55:37+08:00",
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._set_context(project_dir=str(project_dir), requirement_name="贪吃蛇", action="stage.a06.start")  # noqa: SLF001

            workers = server._current_stage_workers("stage.a06.start")  # noqa: SLF001
            status = server._infer_runtime_stage_status("stage.a06.start")  # noqa: SLF001

        self.assertEqual(workers, [])
        self.assertEqual(status, "")

    def test_stage_snapshots_exclude_dead_unscoped_workers_when_context_known(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)

            design_runtime_dir = project_dir / DETAILED_DESIGN_RUNTIME_ROOT_NAME / "worker-stale-design"
            design_runtime_dir.mkdir(parents=True, exist_ok=True)
            (design_runtime_dir / "worker.state.json").write_text(
                json.dumps(
                    {
                        "worker_id": "detailed-design-review-审核员",
                        "session_name": "审核员-天暴星",
                        "work_dir": str(project_dir),
                        "result_status": "running",
                        "workflow_stage": "turn_running",
                        "agent_state": "READY",
                        "health_status": "alive",
                        "note": "turn:detailed_design_review_init_审核员_round_1",
                        "current_turn_status_path": str(project_dir / "missing_design_review.json"),
                        "updated_at": "2026-04-22T22:58:03+08:00",
                        "last_heartbeat_at": "2026-04-22T22:58:03+08:00",
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            task_split_runtime_dir = project_dir / TASK_SPLIT_RUNTIME_ROOT_NAME / "worker-stale-task-split"
            task_split_runtime_dir.mkdir(parents=True, exist_ok=True)
            (task_split_runtime_dir / "worker.state.json").write_text(
                json.dumps(
                    {
                        "worker_id": "task-split-review-审核员",
                        "session_name": "审核员-天暴星",
                        "work_dir": str(project_dir),
                        "result_status": "running",
                        "workflow_stage": "turn_running",
                        "agent_state": "READY",
                        "health_status": "alive",
                        "note": "task_split_review_init_审核员_round_1",
                        "current_turn_phase": "任务拆分",
                        "current_turn_status_path": str(project_dir / "missing_task_split_review.json"),
                        "updated_at": "2026-04-22T22:58:04+08:00",
                        "last_heartbeat_at": "2026-04-22T22:58:04+08:00",
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._tmux_runtime = SimpleNamespace(session_exists=lambda name: False)  # noqa: SLF001
            server._set_context(project_dir=str(project_dir), requirement_name="贪吃蛇", action="stage.a06.start")  # noqa: SLF001

            design = server._build_design_snapshot()  # noqa: SLF001
            task_split = server._build_task_split_snapshot()  # noqa: SLF001

        self.assertEqual(design["workers"], [])
        self.assertEqual(task_split["workers"], [])

    def test_stage_snapshots_prefer_scoped_workers_when_requirement_is_known(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            design_runtime_dir = project_dir / DETAILED_DESIGN_RUNTIME_ROOT_NAME / "worker-live-design"
            design_runtime_dir.mkdir(parents=True, exist_ok=True)
            (design_runtime_dir / "worker.state.json").write_text(
                json.dumps(
                    {
                        "worker_id": "detailed-design-review-审核员",
                        "session_name": "审核员-天暴星",
                        "work_dir": str(project_dir),
                        "result_status": "running",
                        "workflow_stage": "turn_running",
                        "agent_state": "READY",
                        "health_status": "alive",
                        "note": "turn:detailed_design_review_init_审核员_round_1",
                        "current_turn_status_path": str(project_dir / "missing_design_review.json"),
                        "updated_at": "2026-04-22T22:58:03+08:00",
                        "last_heartbeat_at": "2026-04-22T22:58:03+08:00",
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            scoped_runtime_dir = project_dir / DETAILED_DESIGN_RUNTIME_ROOT_NAME / "worker-scoped-design"
            scoped_runtime_dir.mkdir(parents=True, exist_ok=True)
            (scoped_runtime_dir / "worker.state.json").write_text(
                json.dumps(
                    {
                        "worker_id": "detailed-design-review-审核员",
                        "session_name": "审核员-天寿星",
                        "work_dir": str(project_dir),
                        "project_dir": str(project_dir.resolve()),
                        "requirement_name": "贪吃蛇",
                        "workflow_action": "stage.a05.start",
                        "result_status": "running",
                        "workflow_stage": "turn_running",
                        "agent_state": "READY",
                        "health_status": "alive",
                        "note": "turn:detailed_design_review_init_审核员_round_1",
                        "updated_at": "2026-04-22T22:58:04+08:00",
                        "last_heartbeat_at": "2026-04-22T22:58:04+08:00",
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._tmux_runtime = SimpleNamespace(session_exists=lambda name: name in {"审核员-天暴星", "审核员-天寿星"})  # noqa: SLF001
            server._set_context(project_dir=str(project_dir), requirement_name="贪吃蛇", action="stage.a05.start")  # noqa: SLF001

            design = server._build_design_snapshot()  # noqa: SLF001

        self.assertEqual([worker["session_name"] for worker in design["workers"]], ["审核员-天寿星"])

    def test_design_snapshot_keeps_active_unscoped_ba_with_scoped_reviewers(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir).resolve()
            requirement_name = "贪吃蛇"
            ba_runtime_dir = project_dir / DETAILED_DESIGN_RUNTIME_ROOT_NAME / requirement_name / "detailed-design-analyst-1"
            ba_runtime_dir.mkdir(parents=True)
            (ba_runtime_dir / "worker.state.json").write_text(
                json.dumps(
                    {
                        "worker_id": "detailed-design-analyst",
                        "session_name": "需求分析师-地暴星",
                        "work_dir": str(project_dir),
                        "project_dir": str(project_dir),
                        "requirement_name": "",
                        "workflow_action": "stage.a05.start",
                        "status": "running",
                        "result_status": "running",
                        "current_task_runtime_status": "running",
                        "dispatch_state": "submitted",
                        "agent_state": "READY",
                        "health_status": "alive",
                        "note": "submitted:modify_detailed_design",
                        "updated_at": "2026-04-22T22:58:05+08:00",
                        "last_heartbeat_at": "2026-04-22T22:58:05+08:00",
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            reviewer_runtime_dir = project_dir / DETAILED_DESIGN_RUNTIME_ROOT_NAME / requirement_name / "reviewer-1"
            reviewer_runtime_dir.mkdir(parents=True)
            (reviewer_runtime_dir / "worker.state.json").write_text(
                json.dumps(
                    {
                        "worker_id": "detailed-design-review-审核员",
                        "session_name": "审核员-天退星",
                        "work_dir": str(project_dir),
                        "project_dir": str(project_dir),
                        "requirement_name": requirement_name,
                        "workflow_action": "stage.a05.start",
                        "status": "ready",
                        "result_status": "ready",
                        "agent_state": "READY",
                        "health_status": "alive",
                        "updated_at": "2026-04-22T22:58:04+08:00",
                        "last_heartbeat_at": "2026-04-22T22:58:04+08:00",
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._tmux_runtime = SimpleNamespace(session_exists=lambda name: name in {"需求分析师-地暴星", "审核员-天退星"})  # noqa: SLF001
            server._set_context(project_dir=str(project_dir), requirement_name=requirement_name, action="stage.a05.start")  # noqa: SLF001

            design = server._build_design_snapshot()  # noqa: SLF001

        sessions = {worker["session_name"] for worker in design["workers"]}
        self.assertEqual(sessions, {"需求分析师-地暴星", "审核员-天退星"})

    def test_worker_context_filter_separates_requirements_in_same_project(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = str(Path(tmpdir).resolve())
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._set_context(project_dir=project_dir, requirement_name="需求A", action="stage.a07.start")  # noqa: SLF001

            workers = server._filter_workers_for_current_context(  # noqa: SLF001
                [
                    {
                        "session_name": "开发工程师-A",
                        "project_dir": project_dir,
                        "requirement_name": "需求A",
                        "workflow_action": "stage.a07.start",
                    },
                    {
                        "session_name": "开发工程师-B",
                        "project_dir": project_dir,
                        "requirement_name": "需求B",
                        "workflow_action": "stage.a07.start",
                    },
                    {
                        "session_name": "开发工程师-未标记需求",
                        "project_dir": project_dir,
                        "workflow_action": "stage.a07.start",
                    },
                ],
                "stage.a07.start",
            )

        self.assertEqual([worker["session_name"] for worker in workers], ["开发工程师-A"])

    def test_worker_context_filter_does_not_fallback_to_unscoped_when_other_requirement_is_scoped(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = str(Path(tmpdir).resolve())
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._set_context(project_dir=project_dir, requirement_name="需求C", action="stage.a07.start")  # noqa: SLF001

            workers = server._filter_workers_for_current_context(  # noqa: SLF001
                [
                    {
                        "session_name": "开发工程师-B",
                        "project_dir": project_dir,
                        "requirement_name": "需求B",
                        "workflow_action": "stage.a07.start",
                    },
                    {
                        "session_name": "开发工程师-未标记需求",
                        "project_dir": project_dir,
                        "workflow_action": "stage.a07.start",
                    },
                ],
                "stage.a07.start",
            )

        self.assertEqual(workers, [])

    def test_worker_context_filter_does_not_leak_same_requirement_from_other_action(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = str(Path(tmpdir).resolve())
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._set_context(project_dir=project_dir, requirement_name="需求A", action="stage.a07.start")  # noqa: SLF001

            workers = server._filter_workers_for_current_context(  # noqa: SLF001
                [
                    {
                        "session_name": "开发工程师-A-复核",
                        "project_dir": project_dir,
                        "requirement_name": "需求A",
                        "workflow_action": "stage.a08.start",
                    }
                ],
                "stage.a07.start",
            )

        self.assertEqual(workers, [])

    def test_worker_context_filter_keeps_legacy_unscoped_worker_when_no_scoped_metadata_exists(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = str(Path(tmpdir).resolve())
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._set_context(project_dir=project_dir, requirement_name="需求A", action="stage.a07.start")  # noqa: SLF001

            workers = server._filter_workers_for_current_context(  # noqa: SLF001
                [
                    {
                        "session_name": "开发工程师-旧运行态",
                        "project_dir": project_dir,
                    }
                ],
                "stage.a07.start",
            )

        self.assertEqual([worker["session_name"] for worker in workers], ["开发工程师-旧运行态"])

    def test_hitl_prompt_open_emits_question_into_log(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            question_path = Path(tmpdir) / "question.md"
            question_path.write_text("- [阻断] 需要补充碰撞规则\n", encoding="utf-8")
            writer = io.StringIO()
            server = TuiBackendServer(reader=io.StringIO(), writer=writer)

            server._handle_prompt_open(  # noqa: SLF001
                "prompt_hitl",
                BridgePromptRequest(
                    prompt_type="multiline",
                    payload={
                        "title": "HITL 第 1 轮回复",
                        "question_path": str(question_path),
                        "answer_path": str(Path(tmpdir) / "answer.md"),
                    },
                ),
            )

            messages = [json.loads(line) for line in writer.getvalue().splitlines() if line.strip()]
            log_events = [item for item in messages if item.get("kind") == "event" and item.get("type") == "log.append"]
        self.assertTrue(log_events)
        self.assertTrue(any("HITL 问题文档" in str(item["payload"].get("text", "")) for item in log_events))
        self.assertTrue(any("需要补充碰撞规则" in str(item["payload"].get("text", "")) for item in log_events))
        self.assertTrue(any(item["payload"].get("log_kind") == "hitl" for item in log_events))
        self.assertTrue(any(item["payload"].get("hitl_round") == 1 for item in log_events))
        self.assertTrue(any(item["payload"].get("log_title") == "HITL 第 1 轮" for item in log_events))

    def test_prompt_response_backfills_project_dir_into_app_snapshot(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            writer = io.StringIO()
            server = TuiBackendServer(reader=io.StringIO(), writer=writer)
            server._pending_prompt = PendingPromptState(  # noqa: SLF001
                prompt_id="prompt_project",
                prompt_type="text",
                payload={"prompt_text": "输入项目工作目录"},
            )
            server._prompt_broker._pending["prompt_project"] = queue.Queue(maxsize=1)  # noqa: SLF001
            server.handle_request(
                build_request(
                    "prompt.response",
                    {"prompt_id": "prompt_project", "value": tmpdir},
                    message_id="req_project",
                )
            )
            app = server._build_app_snapshot()  # noqa: SLF001
        self.assertEqual(app["project_dir"], str(Path(tmpdir).resolve()))

    def test_project_dir_prompt_clears_stale_requirement_name(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._set_context(project_dir="/tmp/old-project", requirement_name="需求A", action="stage.a04.start")  # noqa: SLF001
            server._pending_prompt = PendingPromptState(  # noqa: SLF001
                prompt_id="prompt_project",
                prompt_type="text",
                payload={"prompt_text": "输入项目工作目录"},
            )
            server._prompt_broker._pending["prompt_project"] = queue.Queue(maxsize=1)  # noqa: SLF001
            server.handle_request(
                build_request(
                    "prompt.response",
                    {"prompt_id": "prompt_project", "value": tmpdir},
                    message_id="req_project",
                )
            )
            app = server._build_app_snapshot()  # noqa: SLF001
        self.assertEqual(app["project_dir"], str(Path(tmpdir).resolve()))
        self.assertEqual(app["requirement_name"], "")

    def test_prompt_response_backfills_requirement_name_from_text_and_select_prompts(self):
        server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
        server._pending_prompt = PendingPromptState(  # noqa: SLF001
            prompt_id="prompt_requirement",
            prompt_type="text",
            payload={"prompt_text": "输入需求名称"},
        )
        server._prompt_broker._pending["prompt_requirement"] = queue.Queue(maxsize=1)  # noqa: SLF001
        server.handle_request(
            build_request(
                "prompt.response",
                {"prompt_id": "prompt_requirement", "value": "贪吃蛇"},
                message_id="req_requirement",
            )
        )
        self.assertEqual(server._build_app_snapshot()["requirement_name"], "贪吃蛇")  # noqa: SLF001

        server._pending_prompt = PendingPromptState(  # noqa: SLF001
            prompt_id="prompt_existing_requirement",
            prompt_type="select",
            payload={
                "prompt_text": "选择已有需求或创建新需求",
                "options": [
                    {"value": "需求A", "label": "需求A"},
                    {"value": "__create_new__", "label": "创建新需求"},
                ],
            },
        )
        server._prompt_broker._pending["prompt_existing_requirement"] = queue.Queue(maxsize=1)  # noqa: SLF001
        server.handle_request(
            build_request(
                "prompt.response",
                {"prompt_id": "prompt_existing_requirement", "value": "需求A"},
                message_id="req_existing_requirement",
            )
        )
        self.assertEqual(server._build_app_snapshot()["requirement_name"], "需求A")  # noqa: SLF001

    def test_prompt_response_ignores_invalid_existing_requirement_placeholder_value(self):
        server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
        server._pending_prompt = PendingPromptState(  # noqa: SLF001
            prompt_id="prompt_existing_requirement",
            prompt_type="select",
            payload={
                "prompt_text": "选择已有需求或创建新需求",
                "options": [
                    {"value": "需求A", "label": "需求A"},
                    {"value": "__create_new__", "label": "创建新需求"},
                ],
            },
        )
        server._prompt_broker._pending["prompt_existing_requirement"] = queue.Queue(maxsize=1)  # noqa: SLF001
        server.handle_request(
            build_request(
                "prompt.response",
                {"prompt_id": "prompt_existing_requirement", "value": "现有需求"},
                message_id="req_existing_requirement_invalid",
            )
        )
        self.assertEqual(server._build_app_snapshot()["requirement_name"], "")  # noqa: SLF001

    def test_handle_bootstrap_writes_response(self):
        writer = io.StringIO()
        server = TuiBackendServer(reader=io.StringIO(), writer=writer)
        server.handle_request(build_request("app.bootstrap", {}, message_id="req_1"))
        messages = [json.loads(line) for line in writer.getvalue().splitlines() if line.strip()]
        self.assertEqual(messages[0]["kind"], "response")
        self.assertEqual(messages[0]["id"], "req_1")
        self.assertIn("routes", messages[0]["payload"])
        self.assertIn("design", messages[0]["payload"]["routes"])
        self.assertIn("task-split", messages[0]["payload"]["routes"])
        self.assertIn("development", messages[0]["payload"]["routes"])
        self.assertIn("overall-review", messages[0]["payload"]["routes"])
        self.assertIn("stage.a08.start", messages[0]["payload"]["commands"])
        self.assertIn("capabilities", messages[0]["payload"])
        self.assertIn("snapshots", messages[0]["payload"])
        self.assertEqual(len(messages), 1)

    def test_stage_a05_start_runs_in_background(self):
        writer = io.StringIO()
        server = TuiBackendServer(reader=io.StringIO(), writer=writer)
        with patch("T11_tui_backend.run_detailed_design_stage", return_value=SimpleNamespace(project_dir="/tmp/project", requirement_name="需求A", passed=True)):
            server.handle_request(
                build_request(
                    "stage.a05.start",
                    {"argv": ["--project-dir", "/tmp/project", "--requirement-name", "需求A"]},
                    message_id="req_a05",
                )
            )
            for worker in list(server._workers.values()):  # noqa: SLF001
                worker.join(timeout=2.0)
            server._flush_dirty_snapshots()  # noqa: SLF001
        messages = [json.loads(line) for line in writer.getvalue().splitlines() if line.strip()]
        self.assertTrue(any(item.get("kind") == "response" and item.get("id") == "req_a05" for item in messages))

    def test_stage_a05_start_logs_error_when_stage_fails(self):
        writer = io.StringIO()
        server = TuiBackendServer(reader=io.StringIO(), writer=writer)
        with patch("T11_tui_backend.run_detailed_design_stage", side_effect=RuntimeError("design failed")):
            server.handle_request(
                build_request(
                    "stage.a05.start",
                    {"argv": ["--project-dir", "/tmp/project", "--requirement-name", "需求A"]},
                    message_id="req_a05_failed",
                )
            )
            for worker in list(server._workers.values()):  # noqa: SLF001
                worker.join(timeout=2.0)
            server._flush_dirty_snapshots()  # noqa: SLF001
        messages = [json.loads(line) for line in writer.getvalue().splitlines() if line.strip()]
        log_events = [item for item in messages if item.get("kind") == "event" and item.get("type") == "log.append"]
        self.assertTrue(any("design failed" in str(item.get("payload", {}).get("text", "")) for item in log_events))
        responses = [item for item in messages if item.get("kind") == "response" and item.get("id") == "req_a05_failed"]
        self.assertTrue(responses)
        self.assertFalse(responses[-1]["ok"])
        stage_events = [item for item in messages if item.get("kind") == "event" and item.get("type") == "stage.changed"]
        self.assertTrue(stage_events)
        self.assertEqual(stage_events[-1]["payload"]["status"], "failed")
        self.assertGreater(int(stage_events[-1]["payload"]["stage_seq"]), 0)

    def test_bridge_progress_events_follow_current_stage_sequence(self):
        writer = io.StringIO()
        server = TuiBackendServer(reader=io.StringIO(), writer=writer)

        server._bridge_ui.notify_stage_action_changed("stage.a05.start")  # noqa: SLF001
        monitor = server._bridge_ui.create_progress_monitor(frame_builder=lambda _tick: "running")  # noqa: SLF001
        monitor.start()
        monitor.stop()

        messages = [json.loads(line) for line in writer.getvalue().splitlines() if line.strip()]
        stage_events = [item for item in messages if item.get("kind") == "event" and item.get("type") == "stage.changed"]
        progress_events = [item for item in messages if item.get("kind") == "event" and str(item.get("type", "")).startswith("progress.")]
        self.assertTrue(stage_events)
        self.assertTrue(progress_events)
        self.assertEqual(stage_events[-1]["payload"]["stage_label"], "详细设计")
        stage_seq = int(stage_events[-1]["payload"]["stage_seq"])
        self.assertGreater(stage_seq, 0)
        self.assertTrue(all(item["payload"]["action"] == "stage.a05.start" for item in progress_events))
        self.assertTrue(all(int(item["payload"]["stage_seq"]) == stage_seq for item in progress_events))

    def test_progress_context_includes_authoritative_runner_id(self):
        server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
        server._display_action = "stage.a06.start"  # noqa: SLF001
        server._display_stage_seq = 8  # noqa: SLF001
        server._display_runner_id = "runner-progress"  # noqa: SLF001

        self.assertEqual(  # noqa: SLF001
            server._current_progress_context(),
            {
                "action": "stage.a06.start",
                "stage_seq": 8,
                "runner_id": "runner-progress",
            },
        )

    def test_stage_a05_start_success_survives_snapshot_emit_failure(self):
        writer = io.StringIO()
        server = TuiBackendServer(reader=io.StringIO(), writer=writer)
        with patch("T11_tui_backend.run_detailed_design_stage", return_value=SimpleNamespace(project_dir="/tmp/project", requirement_name="需求A", passed=True)), patch.object(
            server,
            "_build_app_snapshot",
            side_effect=RuntimeError("snapshot broken"),
        ):
            server.handle_request(
                build_request(
                    "stage.a05.start",
                    {"argv": ["--project-dir", "/tmp/project", "--requirement-name", "需求A"]},
                    message_id="req_a05_snapshot",
                )
            )
            for worker in list(server._workers.values()):  # noqa: SLF001
                worker.join(timeout=2.0)
            server._flush_dirty_snapshots()  # noqa: SLF001
        messages = [json.loads(line) for line in writer.getvalue().splitlines() if line.strip()]
        responses = [item for item in messages if item.get("kind") == "response" and item.get("id") == "req_a05_snapshot"]
        self.assertTrue(responses)
        self.assertTrue(responses[-1]["ok"])
        stage_events = [item for item in messages if item.get("kind") == "event" and item.get("type") == "stage.changed"]
        self.assertEqual(stage_events[-1]["payload"]["status"], "completed")
        log_events = [item for item in messages if item.get("kind") == "event" and item.get("type") == "log.append"]
        self.assertTrue(any("snapshot broken" in str(item.get("payload", {}).get("text", "")) for item in log_events))

    def test_stage_a06_start_runs_in_background(self):
        writer = io.StringIO()
        server = TuiBackendServer(reader=io.StringIO(), writer=writer)
        with patch("T11_tui_backend.run_task_split_stage", return_value=SimpleNamespace(project_dir="/tmp/project", requirement_name="需求A", passed=True)):
            server.handle_request(
                build_request(
                    "stage.a06.start",
                    {"argv": ["--project-dir", "/tmp/project", "--requirement-name", "需求A"]},
                    message_id="req_a06",
                )
            )
            for worker in list(server._workers.values()):  # noqa: SLF001
                worker.join(timeout=2.0)
        messages = [json.loads(line) for line in writer.getvalue().splitlines() if line.strip()]
        self.assertTrue(any(item.get("kind") == "response" and item.get("id") == "req_a06" for item in messages))

    def test_stage_a07_start_runs_in_background(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            paths = build_development_paths(project_dir, "需求A")
            paths["task_json_path"].write_text(
                json.dumps({"M1": {"M1-T1": True}}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            writer = io.StringIO()
            server = TuiBackendServer(reader=io.StringIO(), writer=writer)

            def fake_run_development_stage(argv, preserve_workers=False):  # noqa: ANN001
                self.assertTrue(preserve_workers)
                return SimpleNamespace(project_dir=str(project_dir), requirement_name="需求A", completed=True)

            with patch("T11_tui_backend.run_development_stage", side_effect=fake_run_development_stage):
                server.handle_request(
                    build_request(
                        "stage.a07.start",
                        {"argv": ["--project-dir", str(project_dir), "--requirement-name", "需求A"]},
                        message_id="req_a07",
                    )
                )
                for worker in list(server._workers.values()):  # noqa: SLF001
                    worker.join(timeout=2.0)
        messages = [json.loads(line) for line in writer.getvalue().splitlines() if line.strip()]
        self.assertTrue(any(item.get("kind") == "response" and item.get("id") == "req_a07" and item.get("ok") for item in messages))

    def test_stage_a08_start_runs_in_background(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            paths = build_overall_review_paths(project_dir, "需求A")
            for file_path, content in (
                (paths["original_requirement_path"], "原始需求\n"),
                (paths["requirements_clear_path"], "需求澄清\n"),
                (paths["task_md_path"], "任务单\n"),
                (paths["task_json_path"], json.dumps({"M1": {"M1-T1": True}}, ensure_ascii=False, indent=2)),
                (paths["detailed_design_path"], "详细设计\n"),
                (paths["state_path"], json.dumps({"passed": True}, ensure_ascii=False, indent=2)),
            ):
                file_path.parent.mkdir(parents=True, exist_ok=True)
                file_path.write_text(content, encoding="utf-8")
            writer = io.StringIO()
            server = TuiBackendServer(reader=io.StringIO(), writer=writer)

            def fake_run_overall_review_stage(argv, preserve_workers=False):  # noqa: ANN001
                self.assertTrue(preserve_workers)
                return SimpleNamespace(project_dir=str(project_dir), requirement_name="需求A", completed=True)

            with patch("T11_tui_backend.run_overall_review_stage", side_effect=fake_run_overall_review_stage):
                server.handle_request(
                    build_request(
                        "stage.a08.start",
                        {"argv": ["--project-dir", str(project_dir), "--requirement-name", "需求A"]},
                        message_id="req_a08",
                    )
                )
                for worker in list(server._workers.values()):  # noqa: SLF001
                    worker.join(timeout=2.0)
        messages = [json.loads(line) for line in writer.getvalue().splitlines() if line.strip()]
        self.assertTrue(any(item.get("kind") == "response" and item.get("id") == "req_a08" and item.get("ok") for item in messages))

    def test_stage_a08_start_rejects_completed_result_when_review_state_not_passed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            paths = build_overall_review_paths(project_dir, "需求A")
            paths["task_json_path"].write_text(
                json.dumps({"M1": {"M1-T1": True}}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            writer = io.StringIO()
            server = TuiBackendServer(reader=io.StringIO(), writer=writer)
            with patch("T11_tui_backend.run_overall_review_stage", return_value=SimpleNamespace(project_dir=str(project_dir), requirement_name="需求A", completed=True)):
                server.handle_request(
                    build_request(
                        "stage.a08.start",
                        {"argv": ["--project-dir", str(project_dir), "--requirement-name", "需求A"]},
                        message_id="req_a08_invalid_completed",
                    )
                )
                for worker in list(server._workers.values()):  # noqa: SLF001
                    worker.join(timeout=2.0)
            messages = [json.loads(line) for line in writer.getvalue().splitlines() if line.strip()]
            failure_path = project_dir / ".tmux_workflow" / "需求A" / "stages" / "stage_a08_start.failure.json"
            failure_exists = failure_path.exists()
            failure_payload = json.loads(failure_path.read_text(encoding="utf-8")) if failure_exists else {}
            state_path = project_dir / ".tmux_workflow" / "需求A" / "stages" / "stage_a08_start.state.json"
            state_exists = state_path.exists()
            state_payload = json.loads(state_path.read_text(encoding="utf-8")) if state_exists else {}

        responses = [item for item in messages if item.get("kind") == "response" and item.get("id") == "req_a08_invalid_completed"]
        self.assertTrue(responses)
        self.assertFalse(responses[-1]["ok"])
        self.assertIn("复核阶段返回 completed，但复核完成状态未通过", responses[-1]["error"])
        self.assertTrue(failure_exists)
        self.assertEqual(failure_payload["action"], "stage.a08.start")
        self.assertIn("复核完成状态未通过", failure_payload["error"])
        self.assertTrue(state_exists)
        self.assertEqual(state_payload["action"], "stage.a08.start")
        self.assertEqual(state_payload["status"], "failed")
        self.assertEqual(state_payload["source"], "runner_failure")
        self.assertEqual(state_payload["failure_path"], str(failure_path.resolve()))
        self.assertIn("复核完成状态未通过", state_payload["message"])

    def test_stage_a07_runtime_failure_overrides_stale_completed_display_state(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            runtime_root = project_dir / DEVELOPMENT_RUNTIME_ROOT_NAME / "worker-failed"
            runtime_root.mkdir(parents=True, exist_ok=True)
            (runtime_root / "worker.state.json").write_text(
                json.dumps(
                    {
                        "worker_id": "development-review-需求分析师",
                        "session_name": "需求分析师-虚日鼠",
                        "work_dir": str(project_dir),
                        "result_status": "failed",
                        "workflow_stage": "pending",
                        "agent_state": "DEAD",
                        "health_status": "alive",
                        "note": "error:development_reviewer_init_需求分析师",
                        "updated_at": "2026-04-21T16:52:24+08:00",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            writer = io.StringIO()
            server = TuiBackendServer(reader=io.StringIO(), writer=writer)
            server._set_context(project_dir=str(project_dir), requirement_name="需求A", action="stage.a07.start")  # noqa: SLF001
            server._display_status = "completed"  # noqa: SLF001

            server._bridge_ui.notify_runtime_state_changed()  # noqa: SLF001
            server._flush_dirty_snapshots()  # noqa: SLF001
            messages = [json.loads(line) for line in writer.getvalue().splitlines() if line.strip()]

        stage_events = [item for item in messages if item.get("kind") == "event" and item.get("type") == "stage.changed"]
        self.assertTrue(stage_events)
        self.assertEqual(stage_events[-1]["payload"]["action"], "stage.a07.start")
        self.assertEqual(stage_events[-1]["payload"]["status"], "failed")

    def test_stage_a07_runner_failure_state_overrides_ready_workers_on_bootstrap(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            state_dir = project_dir / ".tmux_workflow" / "需求A" / "stages"
            state_dir.mkdir(parents=True, exist_ok=True)
            (state_dir / "stage_a07_start.state.json").write_text(
                json.dumps(
                    {
                        "action": "stage.a07.start",
                        "status": "failed",
                        "project_dir": str(project_dir.resolve()),
                        "requirement_name": "需求A",
                        "stage_seq": 7,
                        "source": "runner_failure",
                        "updated_at": "2026-05-14T00:00:00+08:00",
                        "message": "internal task result materialization missing",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            runtime_root = project_dir / DEVELOPMENT_RUNTIME_ROOT_NAME / "worker-ready"
            runtime_root.mkdir(parents=True, exist_ok=True)
            (runtime_root / "worker.state.json").write_text(
                json.dumps(
                    {
                        "worker_id": "development-developer",
                        "session_name": "开发工程师-昴日鸡",
                        "work_dir": str(project_dir),
                        "project_dir": str(project_dir.resolve()),
                        "requirement_name": "需求A",
                        "workflow_action": "stage.a07.start",
                        "status": "running",
                        "result_status": "",
                        "workflow_stage": "pending",
                        "agent_state": "READY",
                        "health_status": "awaiting_reconfig",
                        "current_task_runtime_status": "running",
                        "updated_at": "2026-05-14T00:00:01+08:00",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._set_context(project_dir=str(project_dir), requirement_name="需求A", action="stage.a07.start")  # noqa: SLF001
            server._display_action = "stage.a07.start"  # noqa: SLF001
            server._display_status = "running"  # noqa: SLF001
            server._display_stage_seq = 7  # noqa: SLF001

            action, status, stage_seq = server._derive_display_stage_state(  # noqa: SLF001
                preferred_status="running",
                preferred_action="stage.a07.start",
                preferred_stage_seq=7,
                source="runtime_inference",
            )
            bootstrap = server.build_bootstrap_payload()

        self.assertEqual(action, "stage.a07.start")
        self.assertEqual(status, "failed")
        self.assertEqual(stage_seq, 7)
        self.assertEqual(bootstrap["snapshots"]["app"]["active_stage"], "stage.a07.start")
        self.assertEqual(bootstrap["snapshots"]["app"]["active_stage_status"], "failed")

    def test_stage_a07_new_runner_start_can_override_previous_runner_failure_state(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            state_dir = project_dir / ".tmux_workflow" / "需求A" / "stages"
            state_dir.mkdir(parents=True, exist_ok=True)
            (state_dir / "stage_a07_start.state.json").write_text(
                json.dumps(
                    {
                        "action": "stage.a07.start",
                        "status": "failed",
                        "project_dir": str(project_dir.resolve()),
                        "requirement_name": "需求A",
                        "stage_seq": 7,
                        "source": "runner_failure",
                        "updated_at": "2026-05-14T00:00:00+08:00",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._set_context(project_dir=str(project_dir), requirement_name="需求A", action="stage.a07.start")  # noqa: SLF001

            action, status, stage_seq = server._derive_display_stage_state(  # noqa: SLF001
                preferred_status="running",
                preferred_action="stage.a07.start",
                preferred_stage_seq=8,
                source="runner_start",
            )

        self.assertEqual(action, "stage.a07.start")
        self.assertEqual(status, "running")
        self.assertEqual(stage_seq, 8)

    def test_stage_a07_runner_failure_is_not_overridden_by_newer_runtime_inference(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            state_dir = project_dir / ".tmux_workflow" / "需求A" / "stages"
            state_dir.mkdir(parents=True, exist_ok=True)
            (state_dir / "stage_a07_start.state.json").write_text(
                json.dumps(
                    {
                        "action": "stage.a07.start",
                        "status": "failed",
                        "project_dir": str(project_dir.resolve()),
                        "requirement_name": "需求A",
                        "stage_seq": 7,
                        "source": "runner_failure",
                        "updated_at": "2026-05-19T17:36:40+08:00",
                        "message": "开发工程师执行失败",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._set_context(project_dir=str(project_dir), requirement_name="需求A", action="stage.a07.start")  # noqa: SLF001

            action, status, stage_seq = server._derive_display_stage_state(  # noqa: SLF001
                preferred_status="running",
                preferred_action="stage.a07.start",
                preferred_stage_seq=8,
                source="runtime_inference",
                runtime_status="running",
            )

        self.assertEqual(action, "stage.a07.start")
        self.assertEqual(status, "failed")
        self.assertEqual(stage_seq, 7)

    def test_stage_a07_recoverable_worker_failure_does_not_flash_failed_while_runner_alive(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            paths = build_development_paths(project_dir, "需求A")
            paths["task_json_path"].write_text(
                json.dumps({"M1": {"M1-T1": False}}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            runtime_root = project_dir / DEVELOPMENT_RUNTIME_ROOT_NAME / "worker-failed"
            runtime_root.mkdir(parents=True, exist_ok=True)
            (runtime_root / "worker.state.json").write_text(
                json.dumps(
                    {
                        "worker_id": "development-review-需求分析师",
                        "session_name": "需求分析师-虚日鼠",
                        "work_dir": str(project_dir),
                        "project_dir": str(project_dir.resolve()),
                        "requirement_name": "需求A",
                        "workflow_action": "stage.a07.start",
                        "result_status": "failed",
                        "status": "failed",
                        "workflow_stage": "pending",
                        "agent_state": "DEAD",
                        "health_status": "alive",
                        "note": "error:development_review_init_M1-T1_R1_round_1",
                        "updated_at": "2026-04-21T16:52:24+08:00",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            writer = io.StringIO()
            server = TuiBackendServer(reader=io.StringIO(), writer=writer)
            server._set_context(project_dir=str(project_dir), requirement_name="需求A", action="stage.a07.start")  # noqa: SLF001
            server._display_action = "stage.a07.start"  # noqa: SLF001
            server._display_status = "running"  # noqa: SLF001
            server._display_stage_seq = 7  # noqa: SLF001
            server._workers["req_a07"] = SimpleNamespace(  # noqa: SLF001
                name="tui-backend-stage.a07.start-req_a07",
                is_alive=lambda: True,
            )

            with patch("tmux_core.bridge.backend.load_worker_from_state_path", return_value=None):
                action, status, _stage_seq = server._derive_display_stage_state()  # noqa: SLF001
                server._bridge_ui.notify_runtime_state_changed()  # noqa: SLF001
                messages = [json.loads(line) for line in writer.getvalue().splitlines() if line.strip()]

        stage_events = [item for item in messages if item.get("kind") == "event" and item.get("type") == "stage.changed"]
        self.assertEqual(action, "stage.a07.start")
        self.assertEqual(status, "running")
        self.assertFalse(
            any(
                item.get("payload", {}).get("action") == "stage.a07.start"
                and item.get("payload", {}).get("status") == "failed"
                for item in stage_events
            )
        )

    def test_stage_a07_runtime_state_change_does_not_emit_failed_for_prelaunch_worker(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            paths = build_development_paths(project_dir, "需求A")
            paths["task_json_path"].write_text(
                json.dumps({"M1": {"M1-T1": False}}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            runtime_dir = project_dir / DEVELOPMENT_RUNTIME_ROOT_NAME / "worker-starting"
            runtime_dir.mkdir(parents=True, exist_ok=True)
            (runtime_dir / "worker.state.json").write_text(
                json.dumps(
                    {
                        "worker_id": "development-developer",
                        "session_name": "开发工程师-地默星",
                        "work_dir": str(project_dir),
                        "result_status": "ready",
                        "workflow_stage": "pending",
                        "current_command": "zsh",
                        "agent_state": "STARTING",
                        "agent_started": False,
                        "health_status": "alive",
                        "note": "session_created",
                        "updated_at": "2026-04-22T16:52:24+08:00",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            writer = io.StringIO()
            server = TuiBackendServer(reader=io.StringIO(), writer=writer)
            server._set_context(project_dir=str(project_dir), requirement_name="需求A", action="stage.a07.start")  # noqa: SLF001
            server._display_status = "running"  # noqa: SLF001
            server._tmux_runtime.session_exists = lambda session_name: session_name == "开发工程师-地默星"  # noqa: SLF001

            with patch("tmux_core.bridge.backend.load_worker_from_state_path", return_value=None):
                server._bridge_ui.notify_runtime_state_changed()  # noqa: SLF001
                messages = [json.loads(line) for line in writer.getvalue().splitlines() if line.strip()]

        stage_events = [item for item in messages if item.get("kind") == "event" and item.get("type") == "stage.changed"]
        self.assertFalse(
            any(
                item.get("payload", {}).get("action") == "stage.a07.start"
                and item.get("payload", {}).get("status") == "failed"
                for item in stage_events
            )
        )

    def test_refresh_running_worker_snapshot_rechecks_busy_session_even_after_succeeded_status(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            runtime_dir = project_dir / DEVELOPMENT_RUNTIME_ROOT_NAME / "worker-reviewer"
            runtime_dir.mkdir(parents=True, exist_ok=True)
            state_path = runtime_dir / "worker.state.json"
            state_path.write_text(
                json.dumps(
                    {
                        "worker_id": "development-review-审核员",
                        "session_name": "审核员-天伤星",
                        "work_dir": str(project_dir),
                        "result_status": "succeeded",
                        "status": "ready",
                        "agent_state": "BUSY",
                        "agent_started": True,
                        "agent_alive": True,
                        "current_command": "codex",
                        "health_status": "alive",
                        "updated_at": "2026-04-24T12:00:00+08:00",
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            class _FakeWorker:
                def refresh_health(self, *, notify_on_change: bool = False) -> None:  # noqa: ARG002
                    state_path.write_text(
                        json.dumps(
                            {
                                "worker_id": "development-review-审核员",
                                "session_name": "审核员-天伤星",
                                "work_dir": str(project_dir),
                                "result_status": "succeeded",
                                "status": "ready",
                                "agent_state": "READY",
                                "agent_started": True,
                                "agent_alive": True,
                                "current_command": "codex",
                                "health_status": "alive",
                                "updated_at": "2026-04-24T12:01:00+08:00",
                            },
                            ensure_ascii=False,
                            indent=2,
                        ),
                        encoding="utf-8",
                    )

            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._tmux_runtime.session_exists = lambda session_name: session_name == "审核员-天伤星"  # noqa: SLF001
            server._tmux_runtime.backend.session_exists = lambda session_name: session_name == "审核员-天伤星"  # noqa: SLF001

            with patch("tmux_core.bridge.backend.load_worker_from_state_path", return_value=_FakeWorker()):
                snapshot = server._refresh_running_worker_snapshot_if_needed(state_path)  # noqa: SLF001

        self.assertEqual(snapshot["agent_state"], "READY")

    def test_refresh_running_worker_snapshot_loads_active_worker_in_passive_health_mode(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            state_path = project_dir / "worker.state.json"
            state_path.write_text(
                json.dumps(
                    {
                        "worker_id": "development-review-测试工程师",
                        "session_name": "测试工程师-天伤星",
                        "work_dir": str(project_dir),
                        "result_status": "running",
                        "status": "running",
                        "agent_state": "BUSY",
                        "agent_started": True,
                        "agent_alive": True,
                        "current_command": "deveco",
                        "health_status": "alive",
                        "updated_at": "2026-04-24T12:00:00+08:00",
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            class _FakeWorker:
                def refresh_health(self, *, notify_on_change: bool = False) -> None:  # noqa: ARG002
                    return None

            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._tmux_runtime.session_exists = lambda session_name: session_name == "测试工程师-天伤星"  # noqa: SLF001
            server._tmux_runtime.backend.session_exists = lambda session_name: session_name == "测试工程师-天伤星"  # noqa: SLF001

            with patch(
                "tmux_core.bridge.backend.load_worker_from_state_path",
                return_value=_FakeWorker(),
            ) as load_worker:
                server._refresh_running_worker_snapshot_if_needed(state_path)  # noqa: SLF001

        load_worker.assert_called_once_with(
            state_path,
            backend=server._tmux_runtime.backend,  # noqa: SLF001
            passive_health=True,
        )

    def test_stage_a07_start_rejects_completed_result_when_task_json_still_has_false_and_logs_failed_worker(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            paths = build_development_paths(project_dir, "需求A")
            paths["task_json_path"].write_text(
                json.dumps({"M1": {"M1-T1": False}}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            runtime_dir = project_dir / DEVELOPMENT_RUNTIME_ROOT_NAME / "worker-failed"
            runtime_dir.mkdir(parents=True, exist_ok=True)
            (runtime_dir / "worker.state.json").write_text(
                json.dumps(
                    {
                        "worker_id": "development-review-需求分析师",
                        "session_name": "需求分析师-虚日鼠",
                        "work_dir": str(project_dir),
                        "result_status": "failed",
                        "workflow_stage": "pending",
                        "agent_state": "DEAD",
                        "health_status": "alive",
                        "note": "error:development_reviewer_init_需求分析师",
                        "updated_at": "2026-04-21T16:52:24+08:00",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            writer = io.StringIO()
            server = TuiBackendServer(reader=io.StringIO(), writer=writer)
            with patch("tmux_core.bridge.backend.run_development_stage", return_value=SimpleNamespace(project_dir=str(project_dir), requirement_name="需求A", completed=True)):
                server.handle_request(
                    build_request(
                        "stage.a07.start",
                        {"argv": ["--project-dir", str(project_dir), "--requirement-name", "需求A"]},
                        message_id="req_a07_invalid_completed",
                    )
                )
                for worker in list(server._workers.values()):  # noqa: SLF001
                    worker.join(timeout=5.0)
            messages = [json.loads(line) for line in writer.getvalue().splitlines() if line.strip()]
            failure_path = project_dir / ".tmux_workflow" / "需求A" / "stages" / "stage_a07_start.failure.json"
            failure_exists = failure_path.exists()
            failure_payload = json.loads(failure_path.read_text(encoding="utf-8")) if failure_exists else {}
            state_path = project_dir / ".tmux_workflow" / "需求A" / "stages" / "stage_a07_start.state.json"
            state_exists = state_path.exists()
            state_payload = json.loads(state_path.read_text(encoding="utf-8")) if state_exists else {}

        responses = [item for item in messages if item.get("kind") == "response" and item.get("id") == "req_a07_invalid_completed"]
        self.assertTrue(responses)
        self.assertFalse(responses[-1]["ok"])
        self.assertIn("任务开发阶段返回 completed，但任务单 JSON 仍存在未完成任务: M1-T1", responses[-1]["error"])
        self.assertIn("需求分析师-虚日鼠: error:development_reviewer_init_需求分析师", responses[-1]["error"])
        self.assertTrue(failure_exists)
        self.assertEqual(failure_payload["action"], "stage.a07.start")
        self.assertIn("任务单 JSON 仍存在未完成任务", failure_payload["error"])
        self.assertTrue(state_exists)
        self.assertEqual(state_payload["action"], "stage.a07.start")
        self.assertEqual(state_payload["status"], "failed")
        self.assertEqual(state_payload["source"], "runner_failure")
        self.assertEqual(state_payload["failure_path"], str(failure_path.resolve()))
        self.assertIn("任务单 JSON 仍存在未完成任务", state_payload["message"])
        log_texts = [
            str(item.get("payload", {}).get("text", ""))
            for item in messages
            if item.get("kind") == "event" and item.get("type") == "log.append"
        ]
        self.assertTrue(any("需求分析师-虚日鼠: error:development_reviewer_init_需求分析师" in text for text in log_texts))
        stage_events = [item for item in messages if item.get("kind") == "event" and item.get("type") == "stage.changed"]
        self.assertTrue(stage_events)
        self.assertEqual(stage_events[-1]["payload"]["status"], "failed")

    def test_stage_a07_workers_scan_requirement_scoped_runtime_subdirectories(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            runtime_dir = project_dir / DEVELOPMENT_RUNTIME_ROOT_NAME / "需求A" / "development-developer-aaaa"
            runtime_dir.mkdir(parents=True, exist_ok=True)
            (runtime_dir / "worker.state.json").write_text(
                json.dumps(
                    {
                        "worker_id": "development-developer",
                        "session_name": "开发工程师-天速星",
                        "work_dir": str(project_dir),
                        "status": "ready",
                        "agent_state": "READY",
                        "health_status": "alive",
                        "project_dir": str(project_dir.resolve()),
                        "requirement_name": "需求A",
                        "workflow_action": "stage.a07.start",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._set_context(project_dir=str(project_dir), requirement_name="需求A", action="stage.a07.start")  # noqa: SLF001

            workers = server._scan_current_development_workers(str(project_dir))  # noqa: SLF001

        self.assertEqual(len(workers), 1)
        self.assertEqual(workers[0]["session_name"], "开发工程师-天速星")

    def test_stage_a07_workers_scan_includes_custom_reviewer_roles(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            runtime_dir = project_dir / DEVELOPMENT_RUNTIME_ROOT_NAME / "需求A" / "development-review-abcd"
            runtime_dir.mkdir(parents=True, exist_ok=True)
            (runtime_dir / "worker.state.json").write_text(
                json.dumps(
                    {
                        "worker_id": "development-review-量化资产配置专家",
                        "session_name": "量化资产配置专家-地暗星",
                        "work_dir": str(project_dir),
                        "status": "running",
                        "agent_state": "STARTING",
                        "health_status": "alive",
                        "project_dir": str(project_dir.resolve()),
                        "requirement_name": "需求A",
                        "workflow_action": "stage.a07.start",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._set_context(project_dir=str(project_dir), requirement_name="需求A", action="stage.a07.start")  # noqa: SLF001

            workers = server._scan_current_development_workers(str(project_dir))  # noqa: SLF001

        self.assertEqual(len(workers), 1)
        self.assertEqual(workers[0]["session_name"], "量化资产配置专家-地暗星")

    def test_prompt_broker_ignores_duplicate_resolution_when_queue_is_full(self):
        broker = PromptBroker(lambda *_args, **_kwargs: None)
        prompt_queue: queue.Queue[dict[str, object]] = queue.Queue(maxsize=1)
        prompt_queue.put({"value": "first"})
        broker._pending["prompt_1"] = prompt_queue  # noqa: SLF001
        accepted = broker.resolve("prompt_1", {"value": "second"})
        self.assertFalse(accepted)
        self.assertEqual(prompt_queue.get_nowait()["value"], "first")

    def test_prompt_broker_atomically_claims_one_of_two_concurrent_responses(self):
        callback_values: list[str] = []
        broker = PromptBroker(
            lambda *_args, **_kwargs: None,
            on_prompt_resolved=lambda _prompt_id, payload: callback_values.append(
                str((payload or {}).get("value", ""))
            ),
        )
        prompt_queue: queue.Queue[dict[str, object]] = queue.Queue(maxsize=1)
        broker._pending["prompt_1"] = prompt_queue  # noqa: SLF001
        start_barrier = threading.Barrier(3)
        errors: list[BaseException] = []
        results: list[bool] = []

        def resolve(value: str) -> None:
            start_barrier.wait(timeout=1.0)
            try:
                results.append(broker.resolve("prompt_1", {"value": value}))
            except BaseException as error:  # noqa: BLE001
                errors.append(error)

        first = threading.Thread(target=resolve, args=("需求A",))
        second = threading.Thread(target=resolve, args=("需求B",))
        first.start()
        second.start()
        start_barrier.wait(timeout=1.0)
        first.join(timeout=2.0)
        second.join(timeout=2.0)
        delivered = str(prompt_queue.get_nowait()["value"])

        self.assertEqual(errors, [])
        self.assertEqual(sorted(results), [False, True])
        self.assertEqual(callback_values, [delivered])
        self.assertIn(delivered, {"需求A", "需求B"})

    def test_prompt_broker_commits_resolved_callback_before_unblocking_waiter(self):
        prompt_opened = threading.Event()
        callback_entered = threading.Event()
        release_callback = threading.Event()
        received: queue.Queue[dict[str, object]] = queue.Queue(maxsize=1)

        def on_prompt_resolved(_prompt_id, _payload):  # noqa: ANN001
            callback_entered.set()
            release_callback.wait(timeout=2.0)

        broker = PromptBroker(
            lambda *_args, **_kwargs: prompt_opened.set(),
            on_prompt_resolved=on_prompt_resolved,
        )

        def request_prompt() -> None:
            received.put(
                broker.request(
                    BridgePromptRequest(
                        prompt_type="select",
                        payload={"title": "HITL", "default_value": "recheck"},
                    )
                )
            )

        requester = threading.Thread(target=request_prompt)
        requester.start()
        self.assertTrue(prompt_opened.wait(timeout=1.0))
        prompt_id = next(iter(broker._pending))  # noqa: SLF001
        resolver = threading.Thread(target=lambda: broker.resolve(prompt_id, {"value": "recheck"}))
        resolver.start()
        try:
            self.assertTrue(callback_entered.wait(timeout=1.0))
            with self.assertRaises(queue.Empty):
                received.get(timeout=0.05)
        finally:
            release_callback.set()
            requester.join(timeout=2.0)
            resolver.join(timeout=2.0)
        self.assertEqual(received.get(timeout=1.0)["value"], "recheck")

    def test_prompt_broker_claimed_response_returns_false_then_callback_failure_allows_retry(self):
        callback_attempts: list[str] = []
        callback_entered = threading.Event()
        release_callback = threading.Event()
        first_errors: list[BaseException] = []

        def commit_scope(_prompt_id, payload):  # noqa: ANN001
            value = str(payload.get("value", ""))
            callback_attempts.append(value)
            if value == "first":
                callback_entered.set()
                release_callback.wait(timeout=2.0)
                raise OSError("state write failed")

        broker = PromptBroker(
            lambda *_args, **_kwargs: None,
            on_prompt_resolved=commit_scope,
        )
        prompt_queue: queue.Queue[dict[str, object]] = queue.Queue(maxsize=1)
        broker._pending["prompt-retry"] = prompt_queue  # noqa: SLF001

        def resolve_first() -> None:
            try:
                broker.resolve("prompt-retry", {"value": "first"})
            except BaseException as error:  # noqa: BLE001
                first_errors.append(error)

        first = threading.Thread(target=resolve_first)
        first.start()
        self.assertTrue(callback_entered.wait(timeout=1.0))
        self.assertFalse(broker.resolve("prompt-retry", {"value": "second"}))
        release_callback.set()
        first.join(timeout=2.0)

        self.assertFalse(first.is_alive())
        self.assertEqual(len(first_errors), 1)
        self.assertIsInstance(first_errors[0], OSError)
        self.assertIn("state write failed", str(first_errors[0]))
        self.assertIn("prompt-retry", broker._pending)  # noqa: SLF001
        self.assertNotIn("prompt-retry", broker._claimed_prompts)  # noqa: SLF001
        self.assertTrue(prompt_queue.empty())

        self.assertTrue(broker.resolve("prompt-retry", {"value": "third"}))

        self.assertEqual(prompt_queue.get_nowait()["value"], "third")
        self.assertEqual(callback_attempts, ["first", "third"])

    def test_bridge_prompt_response_reports_claimed_resolution_as_not_accepted(self):
        server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
        server._prompt_broker._pending["prompt-claimed"] = queue.Queue(maxsize=1)  # noqa: SLF001
        server._prompt_broker._claimed_prompts.add("prompt-claimed")  # noqa: SLF001

        self.assertEqual(
            server.resolve_prompt("prompt-claimed", {"value": "duplicate"}),
            {"accepted": False},
        )

    def test_grill_prompt_exposes_cursor_and_rejects_stale_or_duplicate_responses(self):
        server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
        pending = PendingPromptState(
            prompt_id="prompt-grill",
            prompt_type="select",
            payload={
                "interaction_kind": "grill",
                "question_index": 7,
                "title": "选择边界",
            },
            owner_runner_id="runner-a03",
            question_seq=7,
        )
        prompt_queue: queue.Queue[dict[str, object]] = queue.Queue(maxsize=1)
        server._pending_prompts[pending.prompt_id] = pending  # noqa: SLF001
        server._pending_prompt = pending  # noqa: SLF001
        server._prompt_broker._pending[pending.prompt_id] = prompt_queue  # noqa: SLF001

        snapshot = server.build_prompt_snapshot()
        request_payload = server._build_prompt_request_payload(pending, prompt_revision=3)  # noqa: SLF001
        self.assertEqual(snapshot["owner_runner_id"], "runner-a03")
        self.assertEqual(snapshot["question_seq"], 7)
        self.assertEqual(request_payload["owner_runner_id"], "runner-a03")
        self.assertEqual(request_payload["question_seq"], 7)

        self.assertEqual(
            server.resolve_prompt("prompt-grill", {"value": "A"}),
            {"accepted": False},
        )
        self.assertEqual(
            server.resolve_prompt(
                "prompt-grill",
                {"value": "A", "runner_id": "runner-old", "question_seq": 7},
            ),
            {"accepted": False},
        )
        self.assertEqual(
            server.resolve_prompt(
                "prompt-grill",
                {"value": "A", "runner_id": "runner-a03", "question_seq": 6},
            ),
            {"accepted": False},
        )
        self.assertTrue(prompt_queue.empty())

        self.assertEqual(
            server.resolve_prompt(
                "prompt-grill",
                {"value": "A", "runner_id": "runner-a03", "question_seq": 7},
            ),
            {"accepted": True},
        )
        self.assertEqual(prompt_queue.get_nowait()["value"], "A")
        self.assertEqual(
            server.resolve_prompt(
                "prompt-grill",
                {"value": "A", "runner_id": "runner-a03", "question_seq": 7},
            ),
            {"accepted": False},
        )

    def test_grill_prompt_open_returns_owner_cursor_for_initial_request_event(self):
        server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
        server._runner_local.runner_id = "runner-a03"  # noqa: SLF001
        request = BridgePromptRequest(
            prompt_type="select",
            payload={"interaction_kind": "grill", "question_index": 4, "title": "选择边界"},
        )
        with patch.object(server._attention_manager, "start_prompt"), patch.object(  # noqa: SLF001
            server,
            "_emit_hitl_prompt_log",
        ), patch.object(server, "_emit_display_stage_state"), patch.object(
            server,
            "_schedule_flow_snapshot_update",
        ):
            metadata = server._handle_prompt_open("prompt-grill-open", request)  # noqa: SLF001

        self.assertEqual(metadata["owner_runner_id"], "runner-a03")
        self.assertEqual(metadata["question_seq"], 4)
        self.assertEqual(server._pending_prompts["prompt-grill-open"].question_seq, 4)  # noqa: SLF001

    def test_grill_prompt_open_without_registered_runner_uses_prompt_scoped_cursor(self):
        server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
        request = BridgePromptRequest(
            prompt_type="select",
            payload={"interaction_kind": "grill", "question_index": 2, "title": "选择边界"},
        )
        with patch.object(server._attention_manager, "start_prompt"), patch.object(  # noqa: SLF001
            server,
            "_emit_hitl_prompt_log",
        ), patch.object(server, "_emit_display_stage_state"), patch.object(
            server,
            "_schedule_flow_snapshot_update",
        ):
            metadata = server._handle_prompt_open("prompt-grill-unowned", request)  # noqa: SLF001

        self.assertEqual(metadata["owner_runner_id"], "prompt-owner:prompt-grill-unowned")
        self.assertEqual(metadata["question_seq"], 2)
        self.assertEqual(
            server._pending_prompts["prompt-grill-unowned"].owner_runner_id,  # noqa: SLF001
            "prompt-owner:prompt-grill-unowned",
        )

    def test_grill_awaiting_answer_recovers_as_submittable_snapshot_without_live_runner(self):
        with tempfile.TemporaryDirectory() as tmp:
            project_dir = Path(tmp).resolve()
            requirement_name = "需求A"
            session_path, question_path = _write_grill_recovery_session(project_dir, requirement_name)
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._set_context(  # noqa: SLF001
                project_dir=str(project_dir),
                requirement_name=requirement_name,
                action="stage.a03.start",
            )
            server._display_action = "stage.a03.start"  # noqa: SLF001

            snapshot = server.build_prompt_snapshot()

            self.assertTrue(snapshot["pending"])
            self.assertEqual(snapshot["prompt_type"], "multiline")
            self.assertEqual(snapshot["question_seq"], 3)
            self.assertEqual(snapshot["owner_runner_id"], "grill-session:grill-session-a")
            self.assertEqual(snapshot["grill_session_id"], "grill-session-a")
            self.assertEqual(snapshot["grill_question_hash"], build_prefixed_sha256(question_path))
            self.assertTrue(snapshot["payload"]["can_submit"])
            self.assertFalse(snapshot["payload"]["recovery_pending"])
            self.assertTrue(snapshot["payload"]["synthetic_recovery"])
            self.assertNotIn(snapshot["prompt_id"], server._pending_prompts)  # noqa: SLF001
            self.assertEqual(server._prompt_broker.pending_prompt_ids(), ())  # noqa: SLF001
            self.assertIn("安全写入 Grill 会话", snapshot["payload"]["recovery_message"])
            self.assertEqual(snapshot["payload"]["recommendation"], "方案 B")
            self.assertEqual(snapshot["payload"]["reason_text"], "影响实现范围。")
            self.assertEqual(snapshot["payload"]["answer_options"], ["方案 A", "方案 B"])
            self.assertEqual(snapshot["payload"]["default_value"], "方案 B")
            self.assertEqual(server.build_file_preview(question_path)["path"], str(question_path))
            repeated = server.build_prompt_snapshot()
            self.assertEqual(repeated["prompt_revision"], snapshot["prompt_revision"])
            response_payload = {
                "prompt_id": snapshot["prompt_id"],
                "value": "采用业务日边界",
                "runner_id": snapshot["owner_runner_id"],
                "question_seq": 3,
                "grill_session_id": snapshot["grill_session_id"],
                "grill_question_hash": snapshot["grill_question_hash"],
            }
            with patch.object(server, "_schedule_flow_snapshot_update") as refresh:
                response = server.resolve_prompt(snapshot["prompt_id"], response_payload)
            self.assertEqual(response, {"accepted": True})
            refresh.assert_called_once()
            session_payload = json.loads(session_path.read_text(encoding="utf-8"))
            self.assertEqual(session_payload["state"], "answer_pending")
            self.assertEqual(session_payload["pending_answer"], "采用业务日边界")
            self.assertEqual(session_payload["pending_question_path"], "")
            self.assertEqual(session_payload["pending_question_hash"], "")
            self.assertEqual(session_payload["accepted_answers"][-1]["answer"], "采用业务日边界")
            cleared = server.build_prompt_snapshot()
            self.assertFalse(cleared["pending"])
            self.assertGreater(cleared["prompt_revision"], snapshot["prompt_revision"])
            accepted_revision = cleared["prompt_revision"]
            with patch.object(server, "_schedule_flow_snapshot_update"):
                duplicate = server.resolve_prompt(snapshot["prompt_id"], response_payload)
            self.assertEqual(duplicate, {"accepted": False})
            self.assertEqual(server.build_prompt_snapshot()["prompt_revision"], accepted_revision)

    def test_grill_recovery_does_not_reopen_consumed_or_invalid_question(self):
        cases = (
            {"state": "turn_in_progress"},
            {"pending_answer": "方案 B"},
            {"question_hash": "sha256:invalid"},
        )
        for index, overrides in enumerate(cases, start=1):
            with self.subTest(case=index), tempfile.TemporaryDirectory() as tmp:
                project_dir = Path(tmp).resolve()
                requirement_name = "需求A"
                _write_grill_recovery_session(project_dir, requirement_name, **overrides)
                server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
                server._set_context(  # noqa: SLF001
                    project_dir=str(project_dir),
                    requirement_name=requirement_name,
                    action="stage.a03.start",
                )
                server._display_action = "stage.a03.start"  # noqa: SLF001

                self.assertFalse(server.build_prompt_snapshot()["pending"])

    def test_synthetic_grill_recovery_rejects_every_stale_cursor_component(self):
        with tempfile.TemporaryDirectory() as tmp:
            project_dir = Path(tmp).resolve()
            requirement_name = "需求A"
            session_path, _question_path = _write_grill_recovery_session(project_dir, requirement_name)
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._set_context(  # noqa: SLF001
                project_dir=str(project_dir),
                requirement_name=requirement_name,
                action="stage.a03.start",
            )
            server._display_action = "stage.a03.start"  # noqa: SLF001
            snapshot = server.build_prompt_snapshot()
            baseline_revision = snapshot["prompt_revision"]
            base_response = {
                "prompt_id": snapshot["prompt_id"],
                "value": "方案 B",
                "runner_id": snapshot["owner_runner_id"],
                "question_seq": snapshot["question_seq"],
                "grill_session_id": snapshot["grill_session_id"],
                "grill_question_hash": snapshot["grill_question_hash"],
            }
            cases = (
                {"prompt_id": "grill_recovery_stale"},
                {"runner_id": "grill-session:stale"},
                {"question_seq": 2},
                {"grill_session_id": "stale-session"},
                {"grill_question_hash": "sha256:stale"},
                {"value": "   "},
            )
            for overrides in cases:
                with self.subTest(overrides=overrides), patch.object(
                    server,
                    "_schedule_flow_snapshot_update",
                ):
                    response = server.resolve_prompt(
                        snapshot["prompt_id"],
                        {**base_response, **overrides},
                    )
                self.assertEqual(response, {"accepted": False})
                persisted = json.loads(session_path.read_text(encoding="utf-8"))
                self.assertEqual(persisted["state"], "awaiting_answer")
                self.assertEqual(persisted["pending_answer"], "")
                self.assertEqual(server.build_prompt_snapshot()["prompt_revision"], baseline_revision)

    def test_synthetic_grill_recovery_rejects_broker_runner_and_lock_races(self):
        with tempfile.TemporaryDirectory() as tmp:
            project_dir = Path(tmp).resolve()
            requirement_name = "需求A"
            session_path, _question_path = _write_grill_recovery_session(project_dir, requirement_name)
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._set_context(  # noqa: SLF001
                project_dir=str(project_dir),
                requirement_name=requirement_name,
                action="stage.a03.start",
            )
            server._display_action = "stage.a03.start"  # noqa: SLF001
            snapshot = server.build_prompt_snapshot()
            response_payload = {
                "prompt_id": snapshot["prompt_id"],
                "value": "方案 B",
                "runner_id": snapshot["owner_runner_id"],
                "question_seq": snapshot["question_seq"],
                "grill_session_id": snapshot["grill_session_id"],
                "grill_question_hash": snapshot["grill_question_hash"],
            }

            server._prompt_broker._pending["prompt_other"] = queue.Queue(maxsize=1)  # noqa: SLF001
            with patch.object(server, "_schedule_flow_snapshot_update") as refresh:
                self.assertEqual(
                    server.resolve_prompt(snapshot["prompt_id"], response_payload),
                    {"accepted": False},
                )
            refresh.assert_called_once()
            server._prompt_broker._pending.pop("prompt_other", None)  # noqa: SLF001

            worker_key = "late-a03-runner"
            server._workers[worker_key] = threading.current_thread()  # noqa: SLF001
            server._runner_executions[worker_key] = RunnerExecutionState(  # noqa: SLF001
                runner_id="runner-late",
                action="stage.a03.start",
                stage_seq=4,
                project_dir=str(project_dir),
                requirement_name=requirement_name,
                current_action="stage.a03.start",
                current_stage_seq=4,
            )
            with patch.object(server, "_schedule_flow_snapshot_update") as refresh:
                self.assertEqual(
                    server.resolve_prompt(snapshot["prompt_id"], response_payload),
                    {"accepted": False},
                )
            refresh.assert_called_once()
            server._workers.pop(worker_key, None)  # noqa: SLF001
            server._runner_executions.pop(worker_key, None)  # noqa: SLF001

            with patch(
                "tmux_core.bridge.backend.requirement_concurrency_lock",
                side_effect=RuntimeError("并发冲突：同项目同需求已有运行中任务"),
            ), patch.object(server, "_schedule_flow_snapshot_update") as refresh:
                self.assertEqual(
                    server.resolve_prompt(snapshot["prompt_id"], response_payload),
                    {"accepted": False},
                )
            refresh.assert_called_once()
            persisted = json.loads(session_path.read_text(encoding="utf-8"))
            self.assertEqual(persisted["state"], "awaiting_answer")
            self.assertEqual(persisted["pending_answer"], "")

    def test_synthetic_grill_recovery_maps_legacy_select_token_before_persisting(self):
        with tempfile.TemporaryDirectory() as tmp:
            project_dir = Path(tmp).resolve()
            requirement_name = "需求A"
            session_path, _question_path = _write_grill_recovery_session(project_dir, requirement_name)
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._set_context(  # noqa: SLF001
                project_dir=str(project_dir),
                requirement_name=requirement_name,
                action="stage.a03.start",
            )
            server._display_action = "stage.a03.start"  # noqa: SLF001
            snapshot = server.build_prompt_snapshot()
            with patch.object(server, "_schedule_flow_snapshot_update"):
                response = server.resolve_prompt(
                    snapshot["prompt_id"],
                    {
                        "prompt_id": snapshot["prompt_id"],
                        "value": "option_2",
                        "runner_id": snapshot["owner_runner_id"],
                        "question_seq": snapshot["question_seq"],
                        "grill_session_id": snapshot["grill_session_id"],
                        "grill_question_hash": snapshot["grill_question_hash"],
                    },
                )
            self.assertEqual(response, {"accepted": True})
            persisted = json.loads(session_path.read_text(encoding="utf-8"))
            self.assertEqual(persisted["pending_answer"], "方案 B")
            self.assertEqual(persisted["accepted_answers"][-1]["answer"], "方案 B")

    def test_grill_recovery_rebinds_only_live_runner_broker_queue(self):
        with tempfile.TemporaryDirectory() as tmp:
            project_dir = Path(tmp).resolve()
            requirement_name = "需求A"
            worker_state_path = project_dir / ".requirements_runtime" / "worker.state.json"
            worker_state_path.parent.mkdir(parents=True, exist_ok=True)
            worker_state_path.write_text(
                json.dumps(
                    {
                        "project_dir": str(project_dir),
                        "requirement_name": requirement_name,
                        "workflow_action": "stage.a03.start",
                        "stage_runner_id": "runner-a03-live",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            _write_grill_recovery_session(
                project_dir,
                requirement_name,
                active_worker_state_path=str(worker_state_path),
            )
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._set_context(  # noqa: SLF001
                project_dir=str(project_dir),
                requirement_name=requirement_name,
                action="stage.a03.start",
            )
            server._display_action = "stage.a03.start"  # noqa: SLF001
            worker_key = "runner-worker"
            server._workers[worker_key] = threading.current_thread()  # noqa: SLF001
            server._runner_executions[worker_key] = RunnerExecutionState(  # noqa: SLF001
                runner_id="runner-a03-live",
                action="stage.a03.start",
                stage_seq=4,
                project_dir=str(project_dir),
                requirement_name=requirement_name,
                current_action="stage.a03.start",
                current_stage_seq=4,
            )
            broker_prompt_id = f"prompt_{threading.current_thread().ident}_1"
            prompt_queue: queue.Queue[dict[str, object]] = queue.Queue(maxsize=1)
            server._prompt_broker._pending[broker_prompt_id] = prompt_queue  # noqa: SLF001

            snapshot = server.build_prompt_snapshot()

            self.assertEqual(snapshot["prompt_id"], broker_prompt_id)
            self.assertEqual(snapshot["owner_runner_id"], "runner-a03-live")
            self.assertTrue(snapshot["payload"]["can_submit"])
            self.assertIs(server._pending_prompts[broker_prompt_id], server._pending_prompt)  # noqa: SLF001
            with patch.object(server, "_schedule_flow_snapshot_update"), patch.object(
                server,
                "_restore_active_runner_after_prompt_resolution",
            ):
                response = server.resolve_prompt(
                    broker_prompt_id,
                    {
                        "value": "option_2",
                        "runner_id": "runner-a03-live",
                        "question_seq": 3,
                    },
                )
            self.assertEqual(response, {"accepted": True})
            self.assertEqual(prompt_queue.get_nowait()["value"], "option_2")

    def test_non_grill_prompt_response_remains_compatible_with_prompt_id_only(self):
        server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
        pending = PendingPromptState(
            prompt_id="prompt-standard",
            prompt_type="text",
            payload={"title": "标准输入"},
        )
        prompt_queue: queue.Queue[dict[str, object]] = queue.Queue(maxsize=1)
        server._pending_prompts[pending.prompt_id] = pending  # noqa: SLF001
        server._pending_prompt = pending  # noqa: SLF001
        server._prompt_broker._pending[pending.prompt_id] = prompt_queue  # noqa: SLF001

        self.assertEqual(
            server.resolve_prompt("prompt-standard", {"value": "legacy"}),
            {"accepted": True},
        )
        self.assertEqual(prompt_queue.get_nowait()["value"], "legacy")

    def test_prompt_resolved_schedules_lightweight_snapshot_after_unblocking(self):
        server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
        server._pending_prompts["prompt_1"] = PendingPromptState(  # noqa: SLF001
            prompt_id="prompt_1",
            prompt_type="select",
            payload={"title": "HITL: 架构师 需要人工介入", "is_hitl": True},
        )
        server._pending_prompt = server._pending_prompts["prompt_1"]  # noqa: SLF001

        with patch.object(server, "_emit_snapshot_update") as emit_snapshot, patch.object(
            server,
            "_schedule_snapshot_update",
        ) as schedule_snapshot, patch.object(
            server,
            "_restore_active_runner_after_prompt_resolution",
        ) as restore_runner:
            server._handle_prompt_resolved("prompt_1", {"value": "recheck_after_manual_intervention"})  # noqa: SLF001

        emit_snapshot.assert_not_called()
        restore_runner.assert_not_called()
        schedule_snapshot.assert_called_once()
        self.assertEqual(schedule_snapshot.call_args.kwargs["sections"], {"app", "hitl", "prompt"})
        self.assertFalse(schedule_snapshot.call_args.kwargs["refresh_worker_health"])

    def test_prompt_open_schedules_lightweight_snapshot_without_sync_emit(self):
        server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
        request = BridgePromptRequest(
            prompt_type="select",
            payload={"title": "HITL: 架构师 需要人工介入", "is_hitl": True},
        )

        with patch.object(server, "_emit_snapshot_update", side_effect=AssertionError("sync snapshot should not run")) as emit_snapshot, patch.object(
            server,
            "_schedule_snapshot_update",
        ) as schedule_snapshot:
            server._handle_prompt_open("prompt_1", request)  # noqa: SLF001

        emit_snapshot.assert_not_called()
        schedule_snapshot.assert_called_once()
        self.assertEqual(schedule_snapshot.call_args.kwargs["sections"], {"app", "hitl", "prompt"})
        self.assertFalse(schedule_snapshot.call_args.kwargs["refresh_worker_health"])

    def test_plain_prompt_open_marks_stage_awaiting_input(self):
        server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
        server._set_context(project_dir="/tmp/demo", requirement_name="需求A", action="stage.a08.start")  # noqa: SLF001
        server._display_action = "stage.a08.start"  # noqa: SLF001
        server._display_status = "running"  # noqa: SLF001
        server._display_stage_seq = 7  # noqa: SLF001
        events: list[tuple[str, dict[str, object]]] = []
        request = BridgePromptRequest(
            prompt_type="text",
            payload={"prompt_text": "输入最大审核轮次", "default": "5"},
        )

        with patch.object(server, "emit_event", side_effect=lambda event_type, payload: events.append((event_type, payload))), patch(
            "T11_tui_backend._write_project_stage_state_record",
        ) as write_stage_state, patch.object(server, "_schedule_snapshot_update") as schedule_snapshot:
            server._handle_prompt_open("prompt_plain", request)  # noqa: SLF001

        stage_events = [payload for event_type, payload in events if event_type == "stage.changed"]
        self.assertTrue(stage_events)
        self.assertEqual(stage_events[-1]["action"], "stage.a08.start")
        self.assertEqual(stage_events[-1]["status"], "awaiting-input")
        write_stage_state.assert_called()
        self.assertEqual(write_stage_state.call_args.kwargs["status"], "awaiting-input")
        schedule_snapshot.assert_called_once()

    def test_prompt_resolved_refreshes_display_stage(self):
        server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
        server._pending_prompts["prompt_plain"] = PendingPromptState(  # noqa: SLF001
            prompt_id="prompt_plain",
            prompt_type="text",
            payload={"prompt_text": "输入最大审核轮次", "default": "5"},
        )
        server._pending_prompt = server._pending_prompts["prompt_plain"]  # noqa: SLF001

        with patch.object(server, "_schedule_snapshot_update") as schedule_snapshot:
            server._handle_prompt_resolved("prompt_plain", {"value": "5"})  # noqa: SLF001

        schedule_snapshot.assert_called_once()
        self.assertEqual(schedule_snapshot.call_args.kwargs["sections"], {"app", "hitl", "prompt"})
        self.assertTrue(schedule_snapshot.call_args.kwargs["update_display_stage"])

    def test_prompt_resolved_restores_live_owner_runner_to_running(self):
        server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
        runner_started = threading.Event()
        release_runner = threading.Event()

        def active_runner() -> None:
            runner_started.set()
            release_runner.wait(timeout=2.0)

        runner_thread = threading.Thread(target=active_runner, daemon=True)
        runner_thread.start()
        self.assertTrue(runner_started.wait(timeout=1.0))
        server._workers["owner"] = runner_thread  # noqa: SLF001
        server._runner_executions["owner"] = RunnerExecutionState(  # noqa: SLF001
            runner_id="runner-owner",
            action="workflow.a00.start",
            stage_seq=1,
            project_dir="",
            requirement_name="",
            current_action="stage.a05.start",
            current_stage_seq=9,
        )
        server._display_action = "stage.a05.start"  # noqa: SLF001
        server._display_status = "awaiting-input"  # noqa: SLF001
        server._display_stage_seq = 9  # noqa: SLF001
        server._display_runner_id = "runner-owner"  # noqa: SLF001
        server._display_source = "runtime_inference"  # noqa: SLF001
        server._pending_prompts["prompt_identity"] = PendingPromptState(  # noqa: SLF001
            prompt_id="prompt_identity",
            prompt_type="select",
            payload={"title": "选择审核智能体角色"},
            owner_runner_id="runner-owner",
        )
        server._pending_prompt = server._pending_prompts["prompt_identity"]  # noqa: SLF001

        try:
            with patch.object(server, "_schedule_snapshot_update"):
                server._handle_prompt_resolved("prompt_identity", {"value": "测试工程师"})  # noqa: SLF001

            self.assertEqual(server._display_status, "running")  # noqa: SLF001
            self.assertEqual(server._display_action, "stage.a05.start")  # noqa: SLF001
            self.assertEqual(server._display_stage_seq, 9)  # noqa: SLF001
            self.assertEqual(server._display_runner_id, "runner-owner")  # noqa: SLF001
            self.assertEqual(server._display_source, "runner_start")  # noqa: SLF001
            self.assertEqual(server._display_message, "准备下一步配置")  # noqa: SLF001
            self.assertIsNone(server._pending_prompt)  # noqa: SLF001
        finally:
            release_runner.set()
            runner_thread.join(timeout=1.0)

    def test_prompt_resolved_keeps_awaiting_when_another_hitl_prompt_is_pending(self):
        server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
        runner_started = threading.Event()
        release_runner = threading.Event()

        def active_runner() -> None:
            runner_started.set()
            release_runner.wait(timeout=2.0)

        runner_thread = threading.Thread(target=active_runner, daemon=True)
        runner_thread.start()
        self.assertTrue(runner_started.wait(timeout=1.0))
        server._workers["owner"] = runner_thread  # noqa: SLF001
        server._runner_executions["owner"] = RunnerExecutionState(  # noqa: SLF001
            runner_id="runner-owner",
            action="stage.a05.start",
            stage_seq=9,
            project_dir="",
            requirement_name="",
            current_action="stage.a05.start",
            current_stage_seq=9,
        )
        server._display_action = "stage.a05.start"  # noqa: SLF001
        server._display_status = "awaiting-input"  # noqa: SLF001
        server._display_stage_seq = 9  # noqa: SLF001
        server._display_runner_id = "runner-owner"  # noqa: SLF001
        current = PendingPromptState(
            prompt_id="prompt_identity",
            prompt_type="select",
            payload={"title": "选择审核智能体角色"},
            owner_runner_id="runner-owner",
        )
        hitl = PendingPromptState(
            prompt_id="prompt_hitl",
            prompt_type="select",
            payload={"title": "HITL", "is_hitl": True},
            owner_runner_id="runner-owner",
        )
        server._pending_prompts[current.prompt_id] = current  # noqa: SLF001
        server._pending_prompts[hitl.prompt_id] = hitl  # noqa: SLF001
        server._pending_prompt = current  # noqa: SLF001

        try:
            with patch.object(server, "_schedule_snapshot_update"):
                server._handle_prompt_resolved(current.prompt_id, {"value": "测试工程师"})  # noqa: SLF001

            self.assertEqual(server._display_status, "awaiting-input")  # noqa: SLF001
            self.assertIs(server._pending_prompt, hitl)  # noqa: SLF001
            self.assertEqual(list(server._pending_prompts), [hitl.prompt_id])  # noqa: SLF001
        finally:
            release_runner.set()
            runner_thread.join(timeout=1.0)

    def test_stage_a08_start_injects_default_review_max_rounds_for_tui(self):
        writer = io.StringIO()
        server = TuiBackendServer(reader=io.StringIO(), writer=writer)
        started = threading.Event()
        release = threading.Event()
        captured_argv: list[list[str]] = []

        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)

            def fake_run(argv, *, preserve_workers=False):  # noqa: ANN001
                self.assertTrue(preserve_workers)
                captured_argv.append(list(argv))
                started.set()
                release.wait(timeout=2.0)
                return {"project_dir": str(project_dir), "requirement_name": "需求A"}

            with patch("T11_tui_backend.run_overall_review_stage", side_effect=fake_run), patch.object(
                server,
                "_validate_stage_success_before_completed",
            ):
                server.handle_request(
                    build_request(
                        "stage.a08.start",
                        {"argv": ["--project-dir", str(project_dir), "--requirement-name", "需求A"]},
                        message_id="req_a08_default_rounds",
                    )
                )
                self.assertTrue(started.wait(timeout=2.0))
                release.set()
                for worker in list(server._workers.values()):  # noqa: SLF001
                    worker.join(timeout=2.0)

        self.assertTrue(captured_argv)
        option_index = captured_argv[0].index("--review-max-rounds")
        self.assertEqual(captured_argv[0][option_index + 1], "5")

    def test_stage_a08_start_preserves_explicit_review_max_rounds(self):
        writer = io.StringIO()
        server = TuiBackendServer(reader=io.StringIO(), writer=writer)
        started = threading.Event()
        release = threading.Event()
        captured_argv: list[list[str]] = []

        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)

            def fake_run(argv, *, preserve_workers=False):  # noqa: ANN001
                self.assertTrue(preserve_workers)
                captured_argv.append(list(argv))
                started.set()
                release.wait(timeout=2.0)
                return {"project_dir": str(project_dir), "requirement_name": "需求A"}

            with patch("T11_tui_backend.run_overall_review_stage", side_effect=fake_run), patch.object(
                server,
                "_validate_stage_success_before_completed",
            ):
                server.handle_request(
                    build_request(
                        "stage.a08.start",
                        {
                            "argv": [
                                "--project-dir",
                                str(project_dir),
                                "--requirement-name",
                                "需求A",
                                "--review-max-rounds",
                                "infinite",
                            ]
                        },
                        message_id="req_a08_explicit_rounds",
                    )
                )
                self.assertTrue(started.wait(timeout=2.0))
                release.set()
                for worker in list(server._workers.values()):  # noqa: SLF001
                    worker.join(timeout=2.0)

        self.assertTrue(captured_argv)
        self.assertEqual(captured_argv[0].count("--review-max-rounds"), 1)
        option_index = captured_argv[0].index("--review-max-rounds")
        self.assertEqual(captured_argv[0][option_index + 1], "infinite")

    def test_workflow_a00_start_runs_in_background(self):
        writer = io.StringIO()
        server = TuiBackendServer(reader=io.StringIO(), writer=writer)
        release = threading.Event()

        def delayed_main(_argv):  # noqa: ANN001
            release.wait(timeout=2.0)
            return 0

        with patch("T11_tui_backend.a00_main", side_effect=delayed_main):
            server.handle_request(build_request("workflow.a00.start", {"argv": []}, message_id="req_2"))
            workers = list(server._workers.values())  # noqa: SLF001
            try:
                self.assertTrue(workers)
                self.assertTrue(workers[0].daemon)
            finally:
                release.set()
                for worker in workers:
                    worker.join(timeout=2.0)
        messages = [json.loads(line) for line in writer.getvalue().splitlines() if line.strip()]
        self.assertTrue(any(item.get("kind") == "event" and item.get("type") == "stage.changed" for item in messages))
        self.assertTrue(any(item.get("kind") == "response" and item.get("id") == "req_2" for item in messages))

    def test_workflow_a00_start_deduplicates_while_runner_active(self):
        writer = io.StringIO()
        server = TuiBackendServer(reader=io.StringIO(), writer=writer)
        started = threading.Event()
        release = threading.Event()
        calls: list[list[str]] = []

        def delayed_main(argv):  # noqa: ANN001
            calls.append(list(argv))
            started.set()
            release.wait(timeout=2.0)
            return 0

        with patch("T11_tui_backend.a00_main", side_effect=delayed_main):
            server.handle_request(build_request("workflow.a00.start", {"argv": []}, message_id="req_1"))
            self.assertTrue(started.wait(timeout=2.0))
            server.handle_request(build_request("workflow.a00.start", {"argv": []}, message_id="req_2"))
            release.set()
            for worker in list(server._workers.values()):  # noqa: SLF001
                worker.join(timeout=2.0)

        self.assertEqual(len(calls), 1)
        messages = [json.loads(line) for line in writer.getvalue().splitlines() if line.strip()]
        duplicate_responses = [item for item in messages if item.get("kind") == "response" and item.get("id") == "req_2"]
        self.assertTrue(duplicate_responses)
        self.assertFalse(duplicate_responses[-1]["ok"])
        self.assertIn("已有同一任务在运行", duplicate_responses[-1]["error"])
        self.assertTrue(duplicate_responses[-1]["payload"]["already_running"])
        self.assertFalse(any(item.get("kind") == "event" and item.get("type") == "error" for item in messages))

    def test_workflow_a00_start_nonzero_exit_marks_failed(self):
        writer = io.StringIO()
        server = TuiBackendServer(reader=io.StringIO(), writer=writer)
        with patch("T11_tui_backend.a00_main", return_value=1):
            server.handle_request(build_request("workflow.a00.start", {"argv": []}, message_id="req_nonzero"))
            for worker in list(server._workers.values()):  # noqa: SLF001
                worker.join(timeout=2.0)

        messages = [json.loads(line) for line in writer.getvalue().splitlines() if line.strip()]
        responses = [item for item in messages if item.get("kind") == "response" and item.get("id") == "req_nonzero"]
        self.assertTrue(responses)
        self.assertFalse(responses[-1]["ok"])
        self.assertIn("workflow.a00.start exited with non-zero code: 1", responses[-1]["error"])
        stage_events = [item for item in messages if item.get("kind") == "event" and item.get("type") == "stage.changed"]
        self.assertTrue(stage_events)
        self.assertEqual(stage_events[-1]["payload"]["action"], "workflow.a00.start")
        self.assertEqual(stage_events[-1]["payload"]["status"], "failed")

    def test_runner_system_exit_is_persisted_as_interrupted_terminal(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir).resolve()
            writer = io.StringIO()
            server = TuiBackendServer(reader=io.StringIO(), writer=writer)

            def exit_runner() -> int:
                raise SystemExit(2)

            server._run_in_thread(  # noqa: SLF001
                "req-system-exit",
                "stage.a06.start",
                exit_runner,
                argv=["--project-dir", str(project_dir), "--requirement-name", "需求A"],
                respond=True,
            )
            for worker in list(server._workers.values()):  # noqa: SLF001
                worker.join(timeout=2.0)

            record_dir = project_dir / ".tmux_workflow" / "需求A" / "stages"
            state = json.loads((record_dir / "stage_a06_start.state.json").read_text(encoding="utf-8"))
            failure = json.loads((record_dir / "stage_a06_start.failure.json").read_text(encoding="utf-8"))
            messages = [json.loads(line) for line in writer.getvalue().splitlines() if line.strip()]

        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["source"], "runner_failure")
        self.assertEqual(state["failure_kind"], "runner_interrupted")
        self.assertEqual(failure["failure_kind"], "runner_interrupted")
        self.assertEqual(failure["error"], "2")
        self.assertFalse(server._runner_executions)  # noqa: SLF001
        responses = [item for item in messages if item.get("kind") == "response"]
        self.assertTrue(responses)
        self.assertFalse(responses[-1]["ok"])

    def test_workflow_a00_ready_timeout_enters_hitl_without_failed_response(self):
        writer = io.StringIO()
        server = TuiBackendServer(reader=io.StringIO(), writer=writer)

        with patch("T11_tui_backend.a00_main", side_effect=RuntimeError("Timed out waiting for agent ready.\nmock screen")):
            server.handle_request(build_request("workflow.a00.start", {"argv": []}, message_id="req_ready_timeout"))
            prompt_id = ""
            prompt_messages: list[dict[str, object]] = []
            messages_before_resolve: list[dict[str, object]] = []
            deadline = time.time() + 2.0
            while time.time() < deadline:
                messages = [json.loads(line) for line in writer.getvalue().splitlines() if line.strip()]
                prompt_messages = [
                    item
                    for item in messages
                    if item.get("kind") == "event" and item.get("type") == "prompt.request"
                ]
                if prompt_messages:
                    prompt_id = str(prompt_messages[-1]["payload"]["id"])
                app_snapshots = [item for item in messages if item.get("kind") == "event" and item.get("type") == "snapshot.app"]
                if prompt_id and any(item["payload"].get("pending_hitl") for item in app_snapshots):
                    messages_before_resolve = messages
                    break
                time.sleep(0.01)

            self.assertTrue(prompt_id)
            self.assertTrue(messages_before_resolve)
            server.handle_request(
                build_request(
                    "prompt.response",
                    {"prompt_id": prompt_id, "value": "retry_after_manual_model_change"},
                    message_id="req_ready_timeout_prompt",
                )
            )
            for worker in list(server._workers.values()):  # noqa: SLF001
                worker.join(timeout=2.0)

        messages = [json.loads(line) for line in writer.getvalue().splitlines() if line.strip()]
        prompt_payload = prompt_messages[-1]["payload"]
        self.assertTrue(prompt_payload["is_hitl"])
        self.assertEqual(prompt_payload["recovery_kind"], "agent_ready_timeout")
        self.assertFalse(prompt_payload["can_skip"])
        stage_events = [item for item in messages_before_resolve if item.get("kind") == "event" and item.get("type") == "stage.changed"]
        self.assertTrue(any(item["payload"]["status"] == "awaiting-input" for item in stage_events))
        app_snapshots = [item for item in messages_before_resolve if item.get("kind") == "event" and item.get("type") == "snapshot.app"]
        self.assertTrue(any(item["payload"].get("pending_hitl") for item in app_snapshots))
        self.assertEqual(len(app_snapshots), 1)
        hitl_snapshots = [item for item in messages_before_resolve if item.get("kind") == "event" and item.get("type") == "snapshot.hitl"]
        self.assertEqual(len(hitl_snapshots), 1)
        responses = [item for item in messages if item.get("kind") == "response" and item.get("id") == "req_ready_timeout"]
        self.assertTrue(responses)
        self.assertTrue(responses[-1]["ok"])
        self.assertNotIn("请求失败", str(responses[-1]))
        self.assertFalse(any(item.get("kind") == "event" and item.get("type") == "error" for item in messages))
        self.assertFalse(
            any(
                item.get("kind") == "event"
                and item.get("type") == "stage.changed"
                and item.get("payload", {}).get("status") == "failed"
                for item in messages
            )
        )

    def test_ready_timeout_persists_authoritative_awaiting_input_after_runner_join(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir).resolve()
            writer = io.StringIO()
            server = TuiBackendServer(reader=io.StringIO(), writer=writer)

            def timeout_runner() -> int:
                raise RuntimeError("Timed out waiting for agent ready.\nmock screen")

            server._run_in_thread(  # noqa: SLF001
                "req-scoped-timeout",
                "stage.a06.start",
                timeout_runner,
                argv=["--project-dir", str(project_dir), "--requirement-name", "需求A"],
                respond=True,
            )
            prompt_id = ""
            deadline = time.time() + 2.0
            while time.time() < deadline:
                messages = [json.loads(line) for line in writer.getvalue().splitlines() if line.strip()]
                prompts = [
                    item
                    for item in messages
                    if item.get("kind") == "event" and item.get("type") == "prompt.request"
                ]
                if prompts:
                    prompt_id = str(prompts[-1]["payload"]["id"])
                    break
                time.sleep(0.01)
            self.assertTrue(prompt_id)
            server.resolve_prompt(prompt_id, {"value": "recheck_after_manual_intervention"})
            for worker in list(server._workers.values()):  # noqa: SLF001
                worker.join(timeout=2.0)

            record_dir = project_dir / ".tmux_workflow" / "需求A" / "stages"
            state_path = record_dir / "stage_a06_start.state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            failure_exists = (record_dir / "stage_a06_start.failure.json").exists()
            with patch.object(server, "_infer_runtime_stage_status", return_value=""):
                app = server._build_app_snapshot(  # noqa: SLF001
                    runs=[],
                    control={},
                    hitl={"pending": False},
                    attention={"pending": False},
                    artifacts={"items": []},
                    stage_snapshots={},
                )

        self.assertEqual(state["status"], "awaiting-input")
        self.assertEqual(state["source"], "runner_start")
        self.assertFalse(failure_exists)
        self.assertEqual(app["active_stage_status"], "awaiting-input")
        self.assertEqual(app["active_stage_failure"], {})

    def test_workflow_startup_intervention_uses_real_session_and_state_for_recovery(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = str((Path(tmpdir) / "deveco-worker.state.json").resolve())
            session_name = "DevEcoCode-启动介入"
            startup_error = AgentStartupInterventionRequired(
                blocker_kind="deveco_login",
                session_name=session_name,
                state_path=state_path,
                message="DevEco login requires manual intervention",
            )
            recovered_worker = object()
            writer = io.StringIO()
            server = TuiBackendServer(reader=io.StringIO(), writer=writer)
            expected_backend = server._tmux_runtime.backend  # noqa: SLF001

            with patch("T11_tui_backend.a00_main", side_effect=startup_error), patch(
                "T11_tui_backend.load_worker_from_state_path",
                return_value=recovered_worker,
            ) as load_worker, patch(
                "T11_tui_backend.try_resume_worker",
                return_value=True,
            ) as resume_worker:
                server.handle_request(
                    build_request("workflow.a00.start", {"argv": []}, message_id="req_startup_intervention")
                )
                prompt_id = ""
                prompt_messages: list[dict[str, object]] = []
                deadline = time.time() + 2.0
                while time.time() < deadline:
                    messages = [json.loads(line) for line in writer.getvalue().splitlines() if line.strip()]
                    prompt_messages = [
                        item
                        for item in messages
                        if item.get("kind") == "event" and item.get("type") == "prompt.request"
                    ]
                    if prompt_messages:
                        prompt_id = str(prompt_messages[-1]["payload"]["id"])
                        break
                    time.sleep(0.01)

                self.assertTrue(prompt_id)
                server.handle_request(
                    build_request(
                        "prompt.response",
                        {"prompt_id": prompt_id, "value": "recheck_after_manual_intervention"},
                        message_id="req_startup_intervention_prompt",
                    )
                )
                for worker_thread in list(server._workers.values()):  # noqa: SLF001
                    worker_thread.join(timeout=2.0)

            messages = [json.loads(line) for line in writer.getvalue().splitlines() if line.strip()]
            prompt_payload = prompt_messages[-1]["payload"]
            self.assertEqual(prompt_payload["recovery_kind"], "agent_startup_intervention")
            self.assertEqual(prompt_payload["session_name"], session_name)
            self.assertEqual(prompt_payload["attach_command"], f"tmux attach -t {session_name}")
            self.assertEqual(prompt_payload["state_path"], state_path)
            self.assertFalse(prompt_payload["can_skip"])
            load_worker.assert_called_once_with(state_path, backend=expected_backend)
            resume_worker.assert_called_once_with(recovered_worker, timeout_sec=60.0)
            responses = [
                item
                for item in messages
                if item.get("kind") == "response" and item.get("id") == "req_startup_intervention"
            ]
            self.assertTrue(responses)
            self.assertTrue(responses[-1]["ok"])
            self.assertEqual(
                responses[-1]["payload"],
                {
                    "awaiting_input": False,
                    "recovered": True,
                    "recovery_kind": "agent_startup_intervention",
                    "session_name": session_name,
                    "attach_command": f"tmux attach -t {session_name}",
                    "message": "DevEco login requires manual intervention",
                },
            )
            self.assertFalse(
                any(
                    item.get("kind") == "event"
                    and item.get("type") == "stage.changed"
                    and item.get("payload", {}).get("status") == "failed"
                    for item in messages
                )
            )
            self.assertFalse(any(item.get("kind") == "event" and item.get("type") == "error" for item in messages))

    def test_runtime_intervention_subclass_uses_runtime_recovery_prompt(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = str((Path(tmpdir) / "runtime-worker.state.json").resolve())
            error = AgentRuntimeInterventionRequired(
                blocker_kind="codex_approval",
                session_name="开发工程师-运行期介入",
                state_path=state_path,
                message="Agent runtime requires manual intervention",
            )
            checked_blockers: list[str] = []
            recovered_worker = SimpleNamespace(
                runtime_intervention_is_resolved=lambda blocker_kind: checked_blockers.append(blocker_kind) or True
            )
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())

            with patch.object(
                server,
                "_current_stage_workers_without_runtime_io",
                return_value=[],
            ), patch.object(
                server,
                "_current_stage_workers",
                return_value=[],
            ), patch.object(
                server,
                "_persist_runner_awaiting_input",
                return_value=True,
            ), patch.object(
                server._prompt_broker,
                "request",
                return_value={"value": "recheck_after_manual_intervention"},
            ) as request_prompt, patch(
                "T11_tui_backend.load_worker_from_state_path",
                return_value=recovered_worker,
            ), patch(
                "T11_tui_backend.try_resume_worker",
            ) as resume_worker:
                server._await_agent_ready_timeout_recovery(  # noqa: SLF001
                    request_id="",
                    action="stage.a07.start",
                    stage_seq=7,
                    error=error,
                    respond=False,
                )

        request_prompt.assert_called_once()
        prompt = request_prompt.call_args.args[0]
        self.assertEqual(prompt.payload["recovery_kind"], "agent_runtime_intervention")
        self.assertEqual(prompt.payload["title"], "HITL: 智能体运行期权限介入")
        self.assertIn("运行期权限确认", prompt.payload["prompt_text"])
        self.assertEqual(checked_blockers, ["codex_approval"])
        resume_worker.assert_not_called()

    def test_opencode_question_intervention_prompts_human_to_answer_in_tmux(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = str((Path(tmpdir) / "runtime-worker.state.json").resolve())
            error = AgentRuntimeInterventionRequired(
                blocker_kind="opencode_question",
                session_name="开发工程师-地杰星",
                state_path=state_path,
                message="Agent runtime requires manual intervention",
            )
            recovered_worker = SimpleNamespace(runtime_intervention_is_resolved=lambda _blocker_kind: True)
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())

            with patch.object(
                server,
                "_current_stage_workers_without_runtime_io",
                return_value=[],
            ), patch.object(
                server,
                "_current_stage_workers",
                return_value=[],
            ), patch.object(
                server,
                "_persist_runner_awaiting_input",
                return_value=True,
            ), patch.object(
                server._prompt_broker,
                "request",
                return_value={"value": "recheck_after_manual_intervention"},
            ) as request_prompt, patch(
                "T11_tui_backend.load_worker_from_state_path",
                return_value=recovered_worker,
            ), patch("T11_tui_backend.try_resume_worker"):
                server._await_agent_ready_timeout_recovery(  # noqa: SLF001
                    request_id="",
                    action="stage.a05.start",
                    stage_seq=24,
                    error=error,
                    respond=False,
                )

        request_prompt.assert_called_once()
        prompt = request_prompt.call_args.args[0]
        self.assertEqual(prompt.payload["recovery_kind"], "agent_runtime_intervention")
        self.assertEqual(prompt.payload["title"], "HITL: 智能体运行期问题待回答")
        self.assertIn("进入原 tmux 会话回答智能体的问题", prompt.payload["prompt_text"])
        self.assertEqual(prompt.payload["attach_command"], "tmux attach -t 开发工程师-地杰星")

    def test_long_running_task_intervention_explains_no_prompt_replay(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = str((Path(tmpdir) / "runtime-worker.state.json").resolve())
            error = AgentRuntimeInterventionRequired(
                blocker_kind="long_running_task_result",
                session_name="开发工程师-天满星",
                state_path=state_path,
                message="Agent runtime requires manual intervention",
            )
            recovered_worker = SimpleNamespace(runtime_intervention_is_resolved=lambda _blocker_kind: True)
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())

            with patch.object(
                server,
                "_current_stage_workers_without_runtime_io",
                return_value=[],
            ), patch.object(
                server,
                "_current_stage_workers",
                return_value=[],
            ), patch.object(
                server,
                "_persist_runner_awaiting_input",
                return_value=True,
            ), patch.object(
                server._prompt_broker,
                "request",
                return_value={"value": "recheck_after_manual_intervention"},
            ) as request_prompt, patch(
                "T11_tui_backend.load_worker_from_state_path",
                return_value=recovered_worker,
            ), patch("T11_tui_backend.try_resume_worker"):
                server._await_agent_ready_timeout_recovery(  # noqa: SLF001
                    request_id="",
                    action="stage.a07.start",
                    stage_seq=48,
                    error=error,
                    respond=False,
                )

        request_prompt.assert_called_once()
        prompt = request_prompt.call_args.args[0]
        self.assertEqual(prompt.payload["title"], "HITL: 智能体任务长时间运行")
        self.assertIn("不会重发提示词", prompt.payload["prompt_text"])
        self.assertEqual(prompt.payload["attach_command"], "tmux attach -t 开发工程师-天满星")

    def test_codex_hook_trust_intervention_prompts_human_without_auto_trusting(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = str((Path(tmpdir) / "runtime-worker.state.json").resolve())
            error = AgentRuntimeInterventionRequired(
                blocker_kind="codex_hook_trust",
                session_name="需求分析师-氐土貉",
                state_path=state_path,
                message="Agent runtime requires manual intervention",
            )
            checked_blockers: list[str] = []
            recovered_worker = SimpleNamespace(
                runtime_intervention_is_resolved=lambda blocker_kind: checked_blockers.append(blocker_kind) or True
            )
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())

            with patch.object(
                server,
                "_current_stage_workers_without_runtime_io",
                return_value=[],
            ), patch.object(
                server,
                "_current_stage_workers",
                return_value=[],
            ), patch.object(
                server,
                "_persist_runner_awaiting_input",
                return_value=True,
            ), patch.object(
                server._prompt_broker,
                "request",
                return_value={"value": "recheck_after_manual_intervention"},
            ) as request_prompt, patch(
                "T11_tui_backend.load_worker_from_state_path",
                return_value=recovered_worker,
            ), patch("T11_tui_backend.try_resume_worker") as resume_worker:
                server._await_agent_ready_timeout_recovery(  # noqa: SLF001
                    request_id="",
                    action="stage.a05.start",
                    stage_seq=29,
                    error=error,
                    respond=False,
                )

        request_prompt.assert_called_once()
        prompt = request_prompt.call_args.args[0]
        self.assertEqual(prompt.payload["recovery_kind"], "agent_runtime_intervention")
        self.assertEqual(prompt.payload["title"], "HITL: Codex Hook 待确认")
        self.assertIn("系统不会自动信任", prompt.payload["prompt_text"])
        self.assertEqual(prompt.payload["attach_command"], "tmux attach -t 需求分析师-氐土貉")
        self.assertEqual(checked_blockers, ["codex_hook_trust"])
        resume_worker.assert_not_called()

    def test_prompt_shutdown_marks_runner_interrupted_without_generic_error(self):
        clear_runtime_shutdown_request()
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                project_dir = Path(tmpdir)
                writer = io.StringIO()
                server = TuiBackendServer(reader=io.StringIO(), writer=writer)

                def fake_runner(argv, preserve_workers=True):  # noqa: ANN001
                    _ = argv
                    _ = preserve_workers
                    from T09_terminal_ops import prompt_select_option

                    return prompt_select_option(
                        title="HITL: 开发工程师 需要人工介入",
                        options=(("recheck", "我已进入 tmux/修正文件，重新检查"),),
                        default_value="recheck",
                        prompt_text="请选择恢复方式",
                        is_hitl=True,
                    )

                with patch("T11_tui_backend.run_development_stage", side_effect=fake_runner):
                    server.handle_request(
                        build_request(
                            "stage.a07.start",
                            {"argv": ["--project-dir", str(project_dir), "--requirement-name", "需求A"]},
                            message_id="req_prompt_shutdown",
                        )
                    )
                    deadline = time.time() + 2.0
                    while time.time() < deadline:
                        messages = [json.loads(line) for line in writer.getvalue().splitlines() if line.strip()]
                        if any(item.get("kind") == "event" and item.get("type") == "prompt.request" for item in messages):
                            break
                        time.sleep(0.01)
                    with patch("T11_tui_backend.cleanup_registered_tmux_workers", return_value=[]), patch.object(
                        server, "_cleanup_visible_tmux_workers", return_value=[]
                    ), patch.object(server, "_cleanup_project_runtime_tmux_workers", return_value=[]), patch.object(
                        server, "_cleanup_current_project_tmux_sessions", return_value=[]
                    ), patch.object(server, "_list_foreign_project_tmux_sessions", return_value=[]):
                        server.shutdown(cleanup_tmux=False)
                    for worker in list(server._workers.values()):  # noqa: SLF001
                        worker.join(timeout=2.0)

                messages = [json.loads(line) for line in writer.getvalue().splitlines() if line.strip()]
                failure_path = (
                    project_dir
                    / ".tmux_workflow"
                    / "需求A"
                    / "stages"
                    / "stage_a07_start.failure.json"
                )
                failure_exists = failure_path.exists()
                failure_payload = json.loads(failure_path.read_text(encoding="utf-8")) if failure_exists else {}
                state_path = failure_path.with_name("stage_a07_start.state.json")
                state_payload = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}

            self.assertTrue(failure_exists)
            self.assertEqual(failure_payload["failure_kind"], "runner_interrupted")
            self.assertEqual(state_payload["status"], "failed")
            self.assertEqual(state_payload["source"], "runner_failure")
            self.assertEqual(state_payload["failure_kind"], "runner_interrupted")
            self.assertFalse(any(item.get("kind") == "event" and item.get("type") == "error" for item in messages))
            self.assertTrue(
                any(
                    item.get("kind") == "event"
                    and item.get("type") == "stage.changed"
                    and item.get("payload", {}).get("status") == "failed"
                    and item.get("payload", {}).get("failure_kind") == "runner_interrupted"
                    for item in messages
                )
            )
        finally:
            clear_runtime_shutdown_request()

    def test_workflow_a00_failure_uses_current_stage_action_and_seq(self):
        writer = io.StringIO()
        server = TuiBackendServer(reader=io.StringIO(), writer=writer)

        def fake_runner(argv):  # noqa: ANN001
            server._handle_runtime_stage_change("stage.a05.start")  # noqa: SLF001
            return 1

        with patch("T11_tui_backend.a00_main", side_effect=fake_runner):
            server.handle_request(build_request("workflow.a00.start", {"argv": []}, message_id="req_stage_fail"))
            for worker in list(server._workers.values()):  # noqa: SLF001
                worker.join(timeout=2.0)

        messages = [json.loads(line) for line in writer.getvalue().splitlines() if line.strip()]
        stage_events = [item for item in messages if item.get("kind") == "event" and item.get("type") == "stage.changed"]
        review_events = [item for item in stage_events if item.get("payload", {}).get("action") == "stage.a05.start"]
        self.assertGreaterEqual(len(review_events), 2)
        self.assertEqual(review_events[-1]["payload"]["status"], "failed")
        self.assertEqual(review_events[-1]["payload"]["stage_seq"], review_events[0]["payload"]["stage_seq"])

    def test_runner_stage_changed_messages_cover_parse_prepare_and_tmux_wait(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir).resolve()
            writer = io.StringIO()
            server = TuiBackendServer(reader=io.StringIO(), writer=writer)

            def root_runner() -> int:
                server._handle_runtime_stage_change("stage.a05.start")  # noqa: SLF001
                server._handle_runtime_state_change()  # noqa: SLF001
                return 0

            prelaunch_worker = {
                "worker_id": "design-prelaunch",
                "session_name": "需求分析师-待启动",
                "workflow_action": "stage.a05.start",
                "workflow_stage": "pending",
                "status": "pending",
                "result_status": "pending",
                "agent_state": "STARTING",
                "agent_started": False,
                "health_status": "unknown",
                "note": "worker_prepared",
            }
            with patch.object(server, "_schedule_flow_snapshot_update"), patch.object(
                server,
                "_current_stage_workers_without_runtime_io",
                return_value=[prelaunch_worker],
            ):
                server._run_in_thread(  # noqa: SLF001
                    "req-stage-messages",
                    "workflow.a00.start",
                    root_runner,
                    argv=[
                        "--project-dir",
                        str(project_dir),
                        "--requirement-name",
                        "需求A",
                    ],
                    respond=False,
                )
                for worker in list(server._workers.values()):  # noqa: SLF001
                    worker.join(timeout=2.0)

            bootstrap = server.build_bootstrap_payload()

            events = [
                item["payload"]
                for item in (
                    json.loads(line)
                    for line in writer.getvalue().splitlines()
                    if line.strip()
                )
                if item.get("kind") == "event" and item.get("type") == "stage.changed"
            ]

        self.assertTrue(
            any(item["action"] == "workflow.a00.start" and item["message"] == "解析参数" for item in events)
        )
        self.assertTrue(
            any(item["action"] == "stage.a05.start" and item["message"] == "准备智能体" for item in events)
        )
        self.assertTrue(
            any(item["action"] == "stage.a05.start" and item["message"] == "等待 tmux" for item in events)
        )
        self.assertEqual(
            bootstrap["snapshots"]["app"]["active_stage_message"],
            "等待 tmux",
        )

    def test_stage_message_is_bound_to_accepted_runner_cursor_and_stale_snapshot_cannot_restore_it(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir).resolve()
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._set_context(  # noqa: SLF001
                project_dir=str(project_dir),
                requirement_name="需求A",
                action="stage.a05.start",
            )

            self.assertTrue(
                server._emit_display_stage_state(  # noqa: SLF001
                    preferred_status="running",
                    preferred_action="stage.a05.start",
                    preferred_stage_seq=10,
                    preferred_runner_id="runner-current",
                    source="runner_start",
                    message="准备智能体",
                    force=True,
                )
            )
            self.assertTrue(
                server._emit_display_stage_state(  # noqa: SLF001
                    preferred_status="running",
                    preferred_action="stage.a05.start",
                    preferred_stage_seq=10,
                    preferred_runner_id="runner-current",
                    source="runtime_inference",
                    force=True,
                )
            )
            self.assertEqual(server._display_message, "准备智能体")  # noqa: SLF001

            self.assertTrue(
                server._emit_display_stage_state(  # noqa: SLF001
                    preferred_status="running",
                    preferred_action="stage.a06.start",
                    preferred_stage_seq=11,
                    preferred_runner_id="runner-current",
                    source="runner_start",
                    force=True,
                )
            )
            self.assertEqual(server._display_message, "")  # noqa: SLF001
            self.assertTrue(
                server._emit_display_stage_state(  # noqa: SLF001
                    preferred_status="running",
                    preferred_action="stage.a06.start",
                    preferred_stage_seq=11,
                    preferred_runner_id="runner-current",
                    source="runner_start",
                    message="等待 tmux",
                )
            )
            self.assertTrue(
                server._emit_display_stage_state(  # noqa: SLF001
                    preferred_status="running",
                    preferred_action="stage.a06.start",
                    preferred_stage_seq=11,
                    preferred_runner_id="runner-current",
                    source="runtime_inference",
                    force=True,
                )
            )
            self.assertEqual(server._display_message, "等待 tmux")  # noqa: SLF001
            self.assertFalse(
                server._emit_display_stage_state(  # noqa: SLF001
                    preferred_status="running",
                    preferred_action="stage.a06.start",
                    preferred_stage_seq=11,
                    preferred_runner_id="runner-old",
                    source="runtime_inference",
                    message="旧快照消息",
                    force=True,
                )
            )
            self.assertEqual(server._display_message, "等待 tmux")  # noqa: SLF001

            self.assertTrue(
                server._emit_display_stage_state(  # noqa: SLF001
                    preferred_status="running",
                    preferred_action="stage.a06.start",
                    preferred_stage_seq=12,
                    preferred_runner_id="runner-next",
                    source="runner_start",
                    force=True,
                )
            )
            self.assertEqual(server._display_message, "")  # noqa: SLF001
            stale_state_path = (
                project_dir
                / ".tmux_workflow"
                / "需求A"
                / "stages"
                / "stage_a06_start.state.json"
            )
            stale_state_path.write_text(
                json.dumps(
                    {
                        "action": "stage.a06.start",
                        "status": "completed",
                        "stage_seq": 11,
                        "runner_id": "runner-current",
                        "source": "runner_complete",
                        "message": "旧快照消息",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            app_snapshot = server._build_app_snapshot(  # noqa: SLF001
                runs=[],
                control={"workers": []},
                hitl={"pending": False},
                attention={"pending": False},
                artifacts={"items": []},
                stage_snapshots={},
            )

        self.assertEqual(app_snapshot["active_stage_runner_id"], "runner-next")
        self.assertEqual(app_snapshot["active_stage_seq"], 12)
        self.assertEqual(app_snapshot["active_stage_message"], "")

    def test_a00_child_stage_persists_runner_start_before_orphan_cleanup_failure(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir).resolve()
            runtime_dir = project_dir / ".requirements_clarification_runtime" / "analyst"
            runtime_dir.mkdir(parents=True)
            (runtime_dir / "worker.state.json").write_text(
                json.dumps(
                    {
                        "worker_id": "requirements-analyst",
                        "session_name": "需求分析师-天哭星",
                        "project_dir": str(project_dir),
                        "requirement_name": "需求A",
                        "workflow_action": "stage.a04.start",
                        "agent_state": "READY",
                        "health_status": "alive",
                        "turn_state": "orphaned",
                        "orphaned_stage_action": "stage.a04.start",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            writer = io.StringIO()
            server = TuiBackendServer(reader=io.StringIO(), writer=writer)
            server._set_context(project_dir=str(project_dir), requirement_name="需求A", action="workflow.a00.start")  # noqa: SLF001

            def fail_kill(_session_name, *, missing_ok=True):  # noqa: ANN001, ARG001
                raise TimeoutError("tmux mutation timed out")

            server._tmux_runtime = SimpleNamespace(  # noqa: SLF001
                session_matches_worker_state=lambda *_args: True,
                kill_session=fail_kill,
            )

            def root_runner():
                server._handle_runtime_stage_change("stage.a04.start")  # noqa: SLF001

            with patch.object(server, "_schedule_flow_snapshot_update"):
                server._run_in_thread(  # noqa: SLF001
                    "req-a00-cleanup-failure",
                    "workflow.a00.start",
                    root_runner,
                    respond=False,
                )
                for thread in list(server._workers.values()):  # noqa: SLF001
                    thread.join(timeout=2.0)

            record_dir = project_dir / ".tmux_workflow" / "需求A" / "stages"
            failure_path = record_dir / "stage_a04_start.failure.json"
            failure = json.loads(failure_path.read_text(encoding="utf-8"))
            state = json.loads((record_dir / "stage_a04_start.state.json").read_text(encoding="utf-8"))
            messages = [json.loads(line) for line in writer.getvalue().splitlines() if line.strip()]

        child_events = [
            item["payload"]
            for item in messages
            if item.get("kind") == "event"
            and item.get("type") == "stage.changed"
            and item.get("payload", {}).get("action") == "stage.a04.start"
        ]
        self.assertGreaterEqual(len(child_events), 2)
        self.assertEqual(child_events[0]["source"], "runner_start")
        self.assertEqual(child_events[-1]["source"], "runner_failure")
        self.assertEqual(child_events[-1]["stage_seq"], child_events[0]["stage_seq"])
        self.assertEqual(failure["action"], "stage.a04.start")
        self.assertEqual(failure["stage_seq"], child_events[0]["stage_seq"])
        self.assertEqual(failure["runner_id"], child_events[0]["runner_id"])
        self.assertIn("保留运行目录并终止新 runner", failure["error"])
        self.assertEqual(state["source"], "runner_failure")
        self.assertEqual(state["stage_seq"], failure["stage_seq"])

    def test_requirement_concurrency_conflict_is_reported_as_error(self):
        writer = io.StringIO()
        server = TuiBackendServer(reader=io.StringIO(), writer=writer)

        def fake_runner(_argv):  # noqa: ANN001
            server._handle_runtime_stage_change("stage.a02.start")  # noqa: SLF001
            raise RuntimeError(
                "并发冲突：同项目同需求已有运行中任务（lock_key=/tmp/project::需求A; "
                "holder=pid=1, thread_name=tui-backend-workflow.a00.start-req_1）"
            )

        with patch("T11_tui_backend.a00_main", side_effect=fake_runner):
            server.handle_request(build_request("workflow.a00.start", {"argv": []}, message_id="req_conflict"))
            for worker in list(server._workers.values()):  # noqa: SLF001
                worker.join(timeout=2.0)

        messages = [json.loads(line) for line in writer.getvalue().splitlines() if line.strip()]
        responses = [item for item in messages if item.get("kind") == "response" and item.get("id") == "req_conflict"]
        self.assertTrue(responses)
        self.assertFalse(responses[-1]["ok"])
        self.assertIn("并发冲突", responses[-1]["error"])
        self.assertTrue(responses[-1]["payload"]["already_running"])
        self.assertFalse(
            any(
                item.get("kind") == "event"
                and item.get("type") == "stage.changed"
                and item.get("payload", {}).get("status") == "failed"
                for item in messages
            )
        )

    def test_persisted_requirement_concurrency_conflict_does_not_reopen_as_stage_failure(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            record_dir = project_dir / ".tmux_workflow" / "需求A" / "stages"
            record_dir.mkdir(parents=True)
            state_path = record_dir / "stage_a02_start.state.json"
            state_path.write_text(
                json.dumps(
                    {
                        "action": "stage.a02.start",
                        "status": "failed",
                        "project_dir": str(project_dir),
                        "requirement_name": "需求A",
                        "stage_seq": 3,
                        "source": "runner_failure",
                        "message": "并发冲突：同项目同需求已有运行中任务（lock_key=/tmp/project::需求A）",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._set_context(project_dir=str(project_dir), requirement_name="需求A", action="stage.a02.start")  # noqa: SLF001

            failure_state = server._load_runner_failure_stage_state("stage.a02.start")  # noqa: SLF001
            app = server._build_app_snapshot(stage_snapshots={"requirements": {"workers": []}})  # noqa: SLF001

        self.assertIsNone(failure_state)
        self.assertNotEqual(app["active_stage_status"], "failed")

    def test_backend_main_cleans_tmux_on_normal_eof(self):
        shutdown_calls: list[bool] = []

        class _FakeServer:
            def protocol_log_sink(self):
                return io.StringIO()

            def serve_forever(self):
                return 0

            def shutdown(self, *, cleanup_tmux: bool):
                shutdown_calls.append(cleanup_tmux)
                return []

        original_stdout = sys.stdout
        try:
            with patch("T11_tui_backend.TuiBackendServer", return_value=_FakeServer()), patch(
                "T11_tui_backend.signal.getsignal",
                return_value=signal.SIG_DFL,
            ), patch("T11_tui_backend.signal.signal"):
                exit_code = backend_main([])
        finally:
            sys.stdout = original_stdout

        self.assertEqual(exit_code, 0)
        self.assertEqual(shutdown_calls, [True])

    def test_backend_main_cleans_tmux_when_serve_forever_raises(self):
        shutdown_calls: list[bool] = []

        class _FakeServer:
            def protocol_log_sink(self):
                return io.StringIO()

            def serve_forever(self):
                raise RuntimeError("backend loop crashed")

            def shutdown(self, *, cleanup_tmux: bool):
                shutdown_calls.append(cleanup_tmux)
                return []

        original_stdout = sys.stdout
        try:
            with patch("T11_tui_backend.TuiBackendServer", return_value=_FakeServer()), patch(
                "T11_tui_backend.signal.getsignal",
                return_value=signal.SIG_DFL,
            ), patch("T11_tui_backend.signal.signal"):
                with self.assertRaisesRegex(RuntimeError, "backend loop crashed"):
                    backend_main([])
        finally:
            sys.stdout = original_stdout

        self.assertEqual(shutdown_calls, [True])

    def test_backend_main_cleans_tmux_on_sigterm(self):
        shutdown_calls: list[bool] = []
        handlers: dict[int, object] = {}

        class _FakeServer:
            def protocol_log_sink(self):
                return io.StringIO()

            def serve_forever(self):
                handler = handlers[signal.SIGTERM]
                handler(signal.SIGTERM, None)
                return 0

            def shutdown(self, *, cleanup_tmux: bool):
                shutdown_calls.append(cleanup_tmux)
                return []

        def fake_signal(signum, handler):  # noqa: ANN001
            handlers[int(signum)] = handler

        original_stdout = sys.stdout
        try:
            with patch("T11_tui_backend.TuiBackendServer", return_value=_FakeServer()), patch(
                "T11_tui_backend.signal.getsignal",
                return_value=signal.SIG_DFL,
            ), patch("T11_tui_backend.signal.signal", side_effect=fake_signal):
                with self.assertRaises(SystemExit) as context:
                    backend_main([])
        finally:
            sys.stdout = original_stdout

        self.assertEqual(context.exception.code, 128 + int(signal.SIGTERM))
        self.assertEqual(shutdown_calls, [True])

    def test_backend_main_signal_handler_does_not_shutdown_inline(self):
        shutdown_calls: list[bool] = []
        inline_shutdown_counts: list[int] = []
        handlers: dict[int, object] = {}

        class _FakeServer:
            def protocol_log_sink(self):
                return io.StringIO()

            def serve_forever(self):
                try:
                    handler = handlers[signal.SIGTERM]
                    handler(signal.SIGTERM, None)
                except SystemExit:
                    inline_shutdown_counts.append(len(shutdown_calls))
                    raise
                return 0

            def shutdown(self, *, cleanup_tmux: bool):
                shutdown_calls.append(cleanup_tmux)
                return []

        def fake_signal(signum, handler):  # noqa: ANN001
            handlers[int(signum)] = handler

        original_stdout = sys.stdout
        try:
            with patch("T11_tui_backend.TuiBackendServer", return_value=_FakeServer()), patch(
                "T11_tui_backend.signal.getsignal",
                return_value=signal.SIG_DFL,
            ), patch("T11_tui_backend.signal.signal", side_effect=fake_signal):
                with self.assertRaises(SystemExit):
                    backend_main([])
        finally:
            sys.stdout = original_stdout

        self.assertEqual(inline_shutdown_counts, [0])
        self.assertEqual(shutdown_calls, [True])

    def test_backend_main_cleanup_only_scopes_context_and_runs_shutdown(self):
        shutdown_calls: list[bool] = []
        contexts: list[dict[str, str | None]] = []

        class _FakeServer:
            def _set_context(self, *, project_dir=None, requirement_name=None, action=None):  # noqa: ANN001
                contexts.append(
                    {
                        "project_dir": project_dir,
                        "requirement_name": requirement_name,
                        "action": action,
                    }
                )

            def shutdown(self, *, cleanup_tmux: bool):
                shutdown_calls.append(cleanup_tmux)
                return []

        with patch("T11_tui_backend.TuiBackendServer", return_value=_FakeServer()):
            exit_code = backend_main(
                [
                    "--cleanup-project-dir",
                    "/tmp/drl-pm",
                    "--cleanup-requirement-name",
                    "强化学习资产配置",
                    "--cleanup-action",
                    "stage.a07.start",
                ]
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            contexts,
            [
                {
                    "project_dir": "/tmp/drl-pm",
                    "requirement_name": "强化学习资产配置",
                    "action": "stage.a07.start",
                }
            ],
        )
        self.assertEqual(shutdown_calls, [True])

    def test_build_requirement_intake_argv_prompts_selection_when_original_requirement_exists(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "贪吃蛇_原始需求.md").write_text("已有原始需求\n", encoding="utf-8")
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())

            argv = server._build_requirement_intake_argv(  # noqa: SLF001
                stage_a01_argv=["--project-dir", tmpdir, "--requirement-name", "贪吃蛇", "--yes"],
                result=SimpleNamespace(project_dir=tmpdir, exit_code=0),
            )

        self.assertEqual(argv[:2], ["--project-dir", tmpdir])
        self.assertNotIn("--requirement-name", argv)
        self.assertNotIn("贪吃蛇", argv)
        self.assertIn("--yes", argv)

    def test_build_requirement_intake_argv_preserves_explicit_reuse_existing_requirement(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "贪吃蛇_原始需求.md").write_text("已有原始需求\n", encoding="utf-8")
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())

            argv = server._build_requirement_intake_argv(  # noqa: SLF001
                stage_a01_argv=[
                    "--project-dir",
                    tmpdir,
                    "--requirement-name",
                    "贪吃蛇",
                    "--reuse-existing-original-requirement",
                    "--yes",
                ],
                result=SimpleNamespace(project_dir=tmpdir, exit_code=0),
            )

        self.assertIn("--requirement-name", argv)
        self.assertIn("贪吃蛇", argv)
        self.assertIn("--reuse-existing-original-requirement", argv)
        self.assertIn("--yes", argv)

    def test_build_requirement_intake_argv_keeps_requirement_name_without_existing_requirement(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())

            argv = server._build_requirement_intake_argv(  # noqa: SLF001
                stage_a01_argv=["--project-dir", tmpdir, "--requirement-name", "贪吃蛇", "--yes"],
                result=SimpleNamespace(project_dir=tmpdir, exit_code=0),
            )

        self.assertIn("--requirement-name", argv)
        self.assertIn("贪吃蛇", argv)
        self.assertIn("--yes", argv)

    def test_stage_a01_start_auto_chains_to_requirement_intake(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            writer = io.StringIO()
            server = TuiBackendServer(reader=io.StringIO(), writer=writer)
            intake_calls: list[list[str]] = []

            with patch(
                "T11_tui_backend.run_routing_stage",
                return_value=SimpleNamespace(project_dir=tmpdir, exit_code=0),
            ), patch(
                "T11_tui_backend.run_requirement_intake_stage",
                side_effect=lambda argv: intake_calls.append(list(argv)) or SimpleNamespace(
                    project_dir=tmpdir,
                    requirement_name="贪吃蛇",
                ),
            ):
                server.handle_request(
                    build_request(
                        "stage.a01.start",
                        {"argv": ["--project-dir", tmpdir, "--requirement-name", "贪吃蛇", "--reuse-existing-original-requirement", "--yes"]},
                        message_id="req_a01",
                    )
                )
                for _ in range(10):
                    workers = list(server._workers.values())  # noqa: SLF001
                    if not workers:
                        break
                    for worker in workers:
                        worker.join(timeout=2.0)

            self.assertEqual(len(intake_calls), 1)
            self.assertEqual(intake_calls[0][:2], ["--project-dir", tmpdir])
            self.assertIn("--requirement-name", intake_calls[0])
            self.assertIn("贪吃蛇", intake_calls[0])
            self.assertIn("--reuse-existing-original-requirement", intake_calls[0])
            self.assertIn("--yes", intake_calls[0])
            messages = [json.loads(line) for line in writer.getvalue().splitlines() if line.strip()]
            self.assertTrue(any(item.get("kind") == "event" and item.get("type") == "log.append" and "自动进入需求录入阶段" in item.get("payload", {}).get("text", "") for item in messages))

    def test_stage_a01_start_auto_chain_prompts_a02_selection_when_original_requirement_exists(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "贪吃蛇_原始需求.md").write_text("已有原始需求\n", encoding="utf-8")
            writer = io.StringIO()
            server = TuiBackendServer(reader=io.StringIO(), writer=writer)
            intake_calls: list[list[str]] = []

            with patch(
                "T11_tui_backend.run_routing_stage",
                return_value=SimpleNamespace(project_dir=tmpdir, exit_code=0),
            ), patch(
                "T11_tui_backend.run_requirement_intake_stage",
                side_effect=lambda argv: intake_calls.append(list(argv)) or SimpleNamespace(
                    project_dir=tmpdir,
                    requirement_name="贪吃蛇",
                ),
            ):
                server.handle_request(
                    build_request(
                        "stage.a01.start",
                        {"argv": ["--project-dir", tmpdir, "--requirement-name", "贪吃蛇", "--yes"]},
                        message_id="req_a01_existing_original",
                    )
                )
                for _ in range(10):
                    workers = list(server._workers.values())  # noqa: SLF001
                    if not workers:
                        break
                    for worker in workers:
                        worker.join(timeout=2.0)

        self.assertEqual(len(intake_calls), 1)
        self.assertEqual(intake_calls[0][:2], ["--project-dir", tmpdir])
        self.assertNotIn("--requirement-name", intake_calls[0])
        self.assertNotIn("贪吃蛇", intake_calls[0])
        self.assertIn("--yes", intake_calls[0])

    def test_stage_a01_start_does_not_chain_when_routing_failed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            writer = io.StringIO()
            server = TuiBackendServer(reader=io.StringIO(), writer=writer)

            with patch(
                "T11_tui_backend.run_routing_stage",
                return_value=SimpleNamespace(project_dir=tmpdir, exit_code=1),
            ), patch(
                "T11_tui_backend.run_requirement_intake_stage",
                side_effect=AssertionError("routing failed 时不应自动进入需求录入"),
            ):
                server.handle_request(
                    build_request(
                        "stage.a01.start",
                        {"argv": ["--project-dir", tmpdir]},
                        message_id="req_a01_failed",
                    )
                )
                for worker in list(server._workers.values()):  # noqa: SLF001
                    worker.join(timeout=2.0)

    def test_workflow_start_clears_stale_stage_state_records(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            record_dir = project_dir / ".tmux_workflow" / "需求A" / "stages"
            record_dir.mkdir(parents=True)
            stale_path = record_dir / "stage_a07_start.state.json"
            stale_path.write_text(
                json.dumps(
                    {
                        "action": "stage.a07.start",
                        "status": "awaiting-input",
                        "project_dir": str(project_dir),
                        "requirement_name": "需求A",
                        "stage_seq": 8,
                        "source": "runtime_inference",
                        "updated_at": "2026-04-01T10:00:00+08:00",
                        "failure_path": "",
                        "message": "",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._set_context(project_dir=str(project_dir), requirement_name="需求A", action="workflow.a00.start")  # noqa: SLF001

            server._emit_display_stage_state(  # noqa: SLF001
                preferred_status="running",
                preferred_action="workflow.a00.start",
                preferred_stage_seq=1,
                source="runner_start",
                force=True,
            )

            workflow_path = record_dir / "workflow_a00_start.state.json"
            stale_exists = stale_path.exists()
            workflow_exists = workflow_path.exists()

        self.assertFalse(stale_exists)
        self.assertTrue(workflow_exists)

    def test_new_runner_start_clears_active_failure_pointer_but_preserves_history(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            record_dir = project_dir / ".tmux_workflow" / "需求A" / "stages"
            record_dir.mkdir(parents=True)
            failure_payload = {
                "action": "stage.a06.start",
                "status": "failed",
                "project_dir": str(project_dir),
                "requirement_name": "需求A",
                "error": "old failure",
            }
            failure_path = record_dir / "stage_a06_start.failure.json"
            latest_path = record_dir / "latest_failure.json"
            failure_path.write_text(json.dumps(failure_payload, ensure_ascii=False), encoding="utf-8")
            latest_path.write_text(json.dumps(failure_payload, ensure_ascii=False), encoding="utf-8")
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._set_context(project_dir=str(project_dir), requirement_name="需求A", action="stage.a07.start")  # noqa: SLF001

            server._emit_display_stage_state(  # noqa: SLF001
                preferred_status="running",
                preferred_action="stage.a07.start",
                preferred_stage_seq=12,
                preferred_runner_id="runner-new",
                source="runner_start",
                force=True,
            )
            failure_exists = failure_path.exists()
            latest_exists = latest_path.exists()

        self.assertTrue(failure_exists)
        self.assertFalse(latest_exists)

    def test_runtime_inference_never_deletes_authoritative_failure_record(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            record_dir = project_dir / ".tmux_workflow" / "需求A" / "stages"
            record_dir.mkdir(parents=True)
            failure_payload = {
                "action": "stage.a07.start",
                "status": "failed",
                "project_dir": str(project_dir),
                "requirement_name": "需求A",
                "error": "runner crashed",
            }
            failure_path = record_dir / "stage_a07_start.failure.json"
            latest_path = record_dir / "latest_failure.json"
            failure_path.write_text(json.dumps(failure_payload, ensure_ascii=False), encoding="utf-8")
            latest_path.write_text(json.dumps(failure_payload, ensure_ascii=False), encoding="utf-8")
            state_path = record_dir / "stage_a07_start.state.json"
            state_path.write_text(
                json.dumps(
                    {
                        "action": "stage.a07.start",
                        "status": "failed",
                        "source": "runner_failure",
                        "stage_seq": 7,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._set_context(project_dir=str(project_dir), requirement_name="需求A", action="stage.a07.start")  # noqa: SLF001

            server._emit_display_stage_state(  # noqa: SLF001
                preferred_status="running",
                preferred_action="stage.a07.start",
                preferred_stage_seq=7,
                source="runtime_inference",
                force=True,
            )
            persisted_state = json.loads(state_path.read_text(encoding="utf-8"))
            failure_exists = failure_path.exists()
            latest_exists = latest_path.exists()

        self.assertTrue(failure_exists)
        self.assertTrue(latest_exists)
        self.assertEqual(persisted_state["status"], "failed")
        self.assertEqual(persisted_state["source"], "runner_failure")

    def test_runtime_inference_cannot_override_authoritative_runner_complete(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir).resolve()
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._set_context(  # noqa: SLF001
                project_dir=str(project_dir),
                requirement_name="需求A",
                action="stage.a06.start",
            )
            self.assertTrue(
                server._emit_display_stage_state(  # noqa: SLF001
                    preferred_status="completed",
                    preferred_action="stage.a06.start",
                    preferred_stage_seq=7,
                    preferred_runner_id="runner-complete",
                    source="runner_complete",
                    force=True,
                )
            )

            with patch.object(server, "_infer_runtime_stage_status", return_value="failed"):
                accepted = server._emit_display_stage_state(  # noqa: SLF001
                    preferred_status="running",
                    preferred_action="stage.a06.start",
                    preferred_stage_seq=7,
                    source="runtime_inference",
                    force=True,
                )
            state_path = (
                project_dir
                / ".tmux_workflow"
                / "需求A"
                / "stages"
                / "stage_a06_start.state.json"
            )
            persisted_state = json.loads(state_path.read_text(encoding="utf-8"))

        self.assertTrue(accepted)
        self.assertEqual(persisted_state["status"], "completed")
        self.assertEqual(persisted_state["source"], "runner_complete")
        self.assertEqual(server._display_status, "completed")  # noqa: SLF001
        self.assertEqual(server._display_source, "runner_complete")  # noqa: SLF001

    def test_runner_failure_is_persisted_without_orphaning_when_event_emit_breaks(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._set_context(project_dir=str(project_dir), requirement_name="需求A", action="stage.a06.start")  # noqa: SLF001
            record_dir = project_dir / ".tmux_workflow" / "需求A" / "stages"
            with patch.object(server, "_infer_runtime_stage_status", side_effect=AssertionError("runner failure must not infer tmux")), patch.object(
                server,
                "_mark_stage_workers_orphaned",
                side_effect=AssertionError("terminal failure must not preserve workers"),
            ), patch.object(server, "emit_event", side_effect=BrokenPipeError("frontend exited")):
                failure_path, orphaned_workers, accepted = server._commit_runner_failure(  # noqa: SLF001
                    action="stage.a06.start",
                    stage_seq=4,
                    runner_id="runner-old",
                    error=RuntimeError("boom"),
                    traceback_text="trace",
                    failure_kind="runner_failure",
                )

            failure = json.loads(Path(failure_path).read_text(encoding="utf-8"))
            state = json.loads((record_dir / "stage_a06_start.state.json").read_text(encoding="utf-8"))

        self.assertTrue(accepted)
        self.assertEqual(orphaned_workers, [])
        self.assertEqual(failure["orphaned_workers"], [])
        self.assertEqual(state["orphaned_workers"], [])
        self.assertEqual(state["source"], "runner_failure")

    def test_shutdown_does_not_overwrite_same_generation_runner_failure_as_interrupted(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir).resolve()
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._set_context(project_dir=str(project_dir), requirement_name="需求A", action="stage.a06.start")  # noqa: SLF001
            runner_started = threading.Event()
            release_runner = threading.Event()

            def active_runner():
                runner_started.set()
                release_runner.wait(timeout=2.0)

            thread = threading.Thread(target=active_runner, name="tui-backend-stage.a06.start", daemon=True)
            thread.start()
            self.assertTrue(runner_started.wait(timeout=1.0))
            execution = SimpleNamespace(
                runner_id="runner-failed",
                action="stage.a06.start",
                stage_seq=4,
                project_dir=str(project_dir),
                requirement_name="需求A",
                terminal_source="",
                terminal_at="",
            )
            server._workers["runner"] = thread  # noqa: SLF001
            server._runner_executions["runner"] = execution  # noqa: SLF001

            try:
                failure_path, _orphaned_workers, accepted = server._commit_runner_failure(  # noqa: SLF001
                    action="stage.a06.start",
                    stage_seq=4,
                    runner_id="runner-failed",
                    error=RuntimeError("original runner failure"),
                    traceback_text="trace",
                    failure_kind="runner_failure",
                )
                self.assertTrue(accepted)
                self.assertEqual(execution.terminal_source, "runner_failure")
                # Force the persisted-generation guard to cover the SIGTERM race even if
                # the in-memory terminal marker has not become visible yet.
                execution.terminal_source = ""
                with patch.object(
                    server,
                    "_commit_runner_failure",
                    side_effect=AssertionError("terminal generation must not be rewritten"),
                ):
                    server._mark_active_runners_interrupted(reason="tui_backend_shutdown")  # noqa: SLF001
                failure = json.loads(Path(failure_path).read_text(encoding="utf-8"))
                state = json.loads(
                    Path(failure_path).with_name("stage_a06_start.state.json").read_text(encoding="utf-8")
                )
            finally:
                release_runner.set()
                thread.join(timeout=1.0)

        self.assertEqual(failure["failure_kind"], "runner_failure")
        self.assertEqual(failure["error"], "original runner failure")
        self.assertEqual(state["failure_kind"], "runner_failure")
        self.assertEqual(state["message"], "original runner failure")

    def test_runner_failure_keeps_worker_state_for_cleanup_and_exposes_authoritative_app_failure(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir).resolve()
            runtime_dir = project_dir / TASK_SPLIT_RUNTIME_ROOT_NAME / "需求A" / "analyst"
            runtime_dir.mkdir(parents=True)
            worker_state_path = runtime_dir / "worker.state.json"
            worker_state_path.write_text(
                json.dumps(
                    {
                        "worker_id": "task-split-analyst",
                        "session_name": "需求分析师-天哭星",
                        "project_dir": str(project_dir),
                        "requirement_name": "需求A",
                        "workflow_action": "stage.a06.start",
                        "stage_runner_id": "runner-a06",
                        "agent_state": "READY",
                        "health_status": "alive",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._set_context(project_dir=str(project_dir), requirement_name="需求A", action="stage.a06.start")  # noqa: SLF001

            failure_path, orphaned_workers, accepted = server._commit_runner_failure(  # noqa: SLF001
                action="stage.a06.start",
                stage_seq=9,
                runner_id="runner-a06",
                error=RuntimeError("task split runner crashed"),
                traceback_text="trace",
                failure_kind="runner_failure",
            )
            app = server._build_app_snapshot(  # noqa: SLF001
                runs=[],
                control={},
                hitl={"pending": False},
                attention={"pending": False},
                artifacts={"items": []},
            )
            worker_state = json.loads(worker_state_path.read_text(encoding="utf-8"))

        self.assertTrue(accepted)
        self.assertNotEqual(worker_state.get("turn_state"), "orphaned")
        self.assertEqual(worker_state["stage_runner_id"], "runner-a06")
        self.assertEqual(orphaned_workers, [])
        active_failure = app["active_stage_failure"]
        self.assertEqual(active_failure["action"], "stage.a06.start")
        self.assertEqual(active_failure["status"], "failed")
        self.assertEqual(active_failure["source"], "runner_failure")
        self.assertEqual(active_failure["failure_kind"], "runner_failure")
        self.assertEqual(active_failure["failure_path"], str(Path(failure_path).resolve()))
        self.assertEqual(active_failure["stage_label"], "任务拆分")

    def test_runner_failure_shutdown_cleans_owned_tmux_and_keeps_failure_record(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir).resolve()
            runtime_root = project_dir / TASK_SPLIT_RUNTIME_ROOT_NAME / "需求A"
            owned_runtime = runtime_root / "owned"
            foreign_runtime = runtime_root / "foreign"
            owned_runtime.mkdir(parents=True)
            foreign_runtime.mkdir(parents=True)
            (owned_runtime / "worker.state.json").write_text(
                json.dumps(
                    {
                        "worker_id": "owned-worker",
                        "session_name": "需求分析师-天哭星",
                        "project_dir": str(project_dir),
                        "requirement_name": "需求A",
                        "workflow_action": "stage.a06.start",
                        "stage_runner_id": "runner-a06",
                        "agent_state": "READY",
                        "health_status": "alive",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            (foreign_runtime / "worker.state.json").write_text(
                json.dumps(
                    {
                        "worker_id": "foreign-worker",
                        "session_name": "其他项目-不应清理",
                        "project_dir": str(project_dir.parent / "other-project"),
                        "requirement_name": "需求B",
                        "workflow_action": "stage.a06.start",
                        "agent_state": "READY",
                        "health_status": "alive",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._set_context(project_dir=str(project_dir), requirement_name="需求A", action="stage.a06.start")  # noqa: SLF001
            failure_path, orphaned_workers, accepted = server._commit_runner_failure(  # noqa: SLF001
                action="stage.a06.start",
                stage_seq=9,
                runner_id="runner-a06",
                error=RuntimeError("task split runner crashed"),
                traceback_text="trace",
                failure_kind="runner_failure",
            )
            killed: list[str] = []
            server._tmux_runtime = SimpleNamespace(  # noqa: SLF001
                session_exists=lambda _session_name: True,
                kill_session=lambda session_name, *, missing_ok=True: killed.append(session_name) or session_name,
            )
            with patch("T11_tui_backend.cleanup_registered_tmux_workers", return_value=[]), patch.object(
                server, "_cleanup_visible_tmux_workers", return_value=[]
            ), patch.object(server, "_cleanup_current_project_tmux_sessions", return_value=[]), patch.object(
                server, "_list_foreign_project_tmux_sessions", return_value=[]
            ):
                cleaned = server.shutdown(cleanup_tmux=True)
            failure = json.loads(Path(failure_path).read_text(encoding="utf-8"))

        self.assertTrue(accepted)
        self.assertEqual(orphaned_workers, [])
        self.assertEqual(failure["orphaned_workers"], [])
        self.assertEqual(killed, ["需求分析师-天哭星"])
        self.assertEqual(cleaned, ["需求分析师-天哭星"])

    def test_runner_failure_does_not_orphan_a03_to_a06_handoff_workers(self):
        scenarios = (
            ("stage.a03.start", ".requirements_analysis_runtime"),
            ("stage.a04.start", ".requirements_clarification_runtime"),
            ("stage.a05.start", REQUIREMENTS_REVIEW_RUNTIME_ROOT_NAME),
            ("stage.a06.start", DETAILED_DESIGN_RUNTIME_ROOT_NAME),
        )
        for action, root_name in scenarios:
            with self.subTest(action=action, root_name=root_name), tempfile.TemporaryDirectory() as tmpdir:
                project_dir = Path(tmpdir).resolve()
                runtime_dir = project_dir / root_name / "需求A" / "handoff"
                runtime_dir.mkdir(parents=True)
                state_path = runtime_dir / "worker.state.json"
                state_path.write_text(
                    json.dumps(
                        {
                            "worker_id": "cross-stage-handoff",
                            "session_name": f"需求分析师-{action}",
                            "project_dir": str(project_dir),
                            "requirement_name": "需求A",
                            "workflow_action": action,
                            "stage_runner_id": f"runner-{action}",
                            "agent_state": "READY",
                            "health_status": "alive",
                        },
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
                server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
                server._set_context(project_dir=str(project_dir), requirement_name="需求A", action=action)  # noqa: SLF001

                _failure_path, orphaned_workers, accepted = server._commit_runner_failure(  # noqa: SLF001
                    action=action,
                    stage_seq=1,
                    runner_id=f"runner-{action}",
                    error=RuntimeError("runner crashed"),
                    traceback_text="trace",
                    failure_kind="runner_failure",
                )
                worker_state = json.loads(state_path.read_text(encoding="utf-8"))

                self.assertTrue(accepted)
                self.assertNotEqual(worker_state.get("turn_state"), "orphaned")
                self.assertEqual(orphaned_workers, [])

    def test_runner_failure_does_not_mutate_worker_turn_state_across_generations(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir).resolve()

            def write_worker(directory: str, runner_id: str, session_name: str) -> Path:
                runtime_dir = project_dir / TASK_SPLIT_RUNTIME_ROOT_NAME / "需求A" / directory
                runtime_dir.mkdir(parents=True)
                state_path = runtime_dir / "worker.state.json"
                state_path.write_text(
                    json.dumps(
                        {
                            "worker_id": directory,
                            "session_name": session_name,
                            "project_dir": str(project_dir),
                            "requirement_name": "需求A",
                            "workflow_action": "stage.a06.start",
                            "stage_runner_id": runner_id,
                            "agent_state": "READY",
                            "health_status": "alive",
                            "turn_state": "waiting_result",
                        },
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
                return state_path

            current_path = write_worker("current", "runner-current", "需求分析师-本代")
            old_path = write_worker("old", "runner-old", "需求分析师-旧代")
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._set_context(  # noqa: SLF001
                project_dir=str(project_dir),
                requirement_name="需求A",
                action="stage.a06.start",
            )

            _failure_path, orphaned_workers, accepted = server._commit_runner_failure(  # noqa: SLF001
                action="stage.a06.start",
                stage_seq=2,
                runner_id="runner-current",
                error=RuntimeError("current generation failed"),
                traceback_text="trace",
                failure_kind="runner_failure",
            )
            current_state = json.loads(current_path.read_text(encoding="utf-8"))
            old_state = json.loads(old_path.read_text(encoding="utf-8"))

        self.assertTrue(accepted)
        self.assertEqual(current_state["turn_state"], "waiting_result")
        self.assertEqual(old_state["turn_state"], "waiting_result")
        self.assertEqual(orphaned_workers, [])

    def test_older_runner_failure_cannot_overwrite_new_generation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._set_context(project_dir=str(project_dir), requirement_name="需求A", action="stage.a07.start")  # noqa: SLF001
            server._emit_display_stage_state(  # noqa: SLF001
                preferred_status="running",
                preferred_action="stage.a07.start",
                preferred_stage_seq=8,
                preferred_runner_id="runner-new",
                source="runner_start",
                force=True,
            )

            failure_path, _orphaned_workers, accepted = server._commit_runner_failure(  # noqa: SLF001
                action="stage.a07.start",
                stage_seq=7,
                runner_id="runner-old",
                error=RuntimeError("late failure"),
                traceback_text="trace",
                failure_kind="runner_failure",
            )
            state_path = project_dir / ".tmux_workflow" / "需求A" / "stages" / "stage_a07_start.state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))

        self.assertFalse(accepted)
        self.assertIsNone(failure_path)
        self.assertEqual(state["status"], "running")
        self.assertEqual(state["runner_id"], "runner-new")
        self.assertEqual(state["stage_seq"], 8)

    def test_same_runner_failure_cannot_be_overwritten_by_late_complete(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir).resolve()
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._set_context(  # noqa: SLF001
                project_dir=str(project_dir),
                requirement_name="需求A",
                action="stage.a06.start",
            )
            failure_path, _workers, accepted = server._commit_runner_failure(  # noqa: SLF001
                action="stage.a06.start",
                stage_seq=1,
                runner_id="runner-same",
                error=RuntimeError("original failure"),
                traceback_text="trace",
                failure_kind="runner_failure",
            )
            late_complete = server._emit_display_stage_state(  # noqa: SLF001
                preferred_status="completed",
                preferred_action="stage.a06.start",
                preferred_stage_seq=1,
                preferred_runner_id="runner-same",
                source="runner_complete",
                force=True,
            )
            state = json.loads(Path(failure_path).with_name("stage_a06_start.state.json").read_text(encoding="utf-8"))

        self.assertTrue(accepted)
        self.assertFalse(late_complete)
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["source"], "runner_failure")
        self.assertEqual(state["message"], "original failure")

    def test_same_runner_complete_cannot_be_overwritten_by_late_interruption(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir).resolve()
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._set_context(  # noqa: SLF001
                project_dir=str(project_dir),
                requirement_name="需求A",
                action="stage.a06.start",
            )
            self.assertTrue(
                server._emit_display_stage_state(  # noqa: SLF001
                    preferred_status="completed",
                    preferred_action="stage.a06.start",
                    preferred_stage_seq=1,
                    preferred_runner_id="runner-same",
                    source="runner_complete",
                    force=True,
                )
            )
            failure_path, orphaned_workers, accepted = server._commit_runner_failure(  # noqa: SLF001
                action="stage.a06.start",
                stage_seq=1,
                runner_id="runner-same",
                error=RuntimeError("late shutdown"),
                traceback_text="trace",
                failure_kind="runner_interrupted",
            )
            state_path = (
                project_dir / ".tmux_workflow" / "需求A" / "stages" / "stage_a06_start.state.json"
            )
            state = json.loads(state_path.read_text(encoding="utf-8"))

        self.assertFalse(accepted)
        self.assertIsNone(failure_path)
        self.assertEqual(orphaned_workers, [])
        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["source"], "runner_complete")

    def test_superseded_runner_runtime_stage_change_is_rejected_before_allocating_sequence(self):
        server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
        server._set_context(project_dir="/tmp/project", requirement_name="需求A", action="stage.a06.start")  # noqa: SLF001
        server._display_action = "stage.a06.start"  # noqa: SLF001
        server._display_status = "running"  # noqa: SLF001
        server._display_stage_seq = 12  # noqa: SLF001
        server._display_runner_id = "runner-new"  # noqa: SLF001
        server._runner_local.runner_id = "runner-old"  # noqa: SLF001
        server._runner_executions = {  # noqa: SLF001
            "old": SimpleNamespace(
                runner_id="runner-old",
                stage_seq=11,
                project_dir="/tmp/project",
                requirement_name="需求A",
            ),
            "new": SimpleNamespace(
                runner_id="runner-new",
                stage_seq=12,
                project_dir="/tmp/project",
                requirement_name="需求A",
            ),
        }

        with patch.object(server, "_allocate_stage_seq", side_effect=AssertionError("late event must be rejected")):
            server._handle_runtime_stage_change("stage.a07.start")  # noqa: SLF001

        self.assertEqual(server._context.current_action, "stage.a06.start")  # noqa: SLF001
        self.assertEqual(server._display_runner_id, "runner-new")  # noqa: SLF001

    def test_superseded_runner_stays_rejected_after_new_runner_leaves_registry(self):
        server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
        server._set_context(project_dir="/tmp/project", requirement_name="需求A", action="stage.a06.start")  # noqa: SLF001
        server._display_action = "stage.a06.start"  # noqa: SLF001
        server._display_status = "completed"  # noqa: SLF001
        server._display_stage_seq = 12  # noqa: SLF001
        server._display_runner_id = "runner-new"  # noqa: SLF001
        server._runner_local.runner_id = "runner-old"  # noqa: SLF001
        server._runner_executions = {  # noqa: SLF001
            "old": SimpleNamespace(
                runner_id="runner-old",
                stage_seq=11,
                project_dir="/tmp/project",
                requirement_name="需求A",
            )
        }
        server._record_scope_generation(  # noqa: SLF001
            runner_id="runner-new",
            stage_seq=12,
            project_dir="/tmp/project",
            requirement_name="需求A",
        )

        with patch.object(server, "_allocate_stage_seq", side_effect=AssertionError("old runner must stay superseded")):
            server._handle_runtime_stage_change("stage.a07.start")  # noqa: SLF001

        self.assertEqual(server._context.current_action, "stage.a06.start")  # noqa: SLF001
        self.assertEqual(server._display_runner_id, "runner-new")  # noqa: SLF001

    def test_direct_runner_registers_generation_only_after_start_state_is_persisted(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = str(Path(tmpdir).resolve())
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            old_execution = RunnerExecutionState(
                runner_id="runner-old",
                action="stage.a06.start",
                stage_seq=1,
                project_dir=project_dir,
                requirement_name="需求A",
                current_action="stage.a06.start",
                current_stage_seq=1,
                registered_project_dir=project_dir,
                registered_requirement_name="需求A",
                generation_registered=True,
            )
            server._workers["old"] = SimpleNamespace(is_alive=lambda: True)  # noqa: SLF001
            server._runner_executions["old"] = old_execution  # noqa: SLF001
            server._stage_seq_counter = 1  # noqa: SLF001
            server._record_scope_generation(  # noqa: SLF001
                runner_id="runner-old",
                stage_seq=1,
                project_dir=project_dir,
                requirement_name="需求A",
            )
            runner_started = threading.Event()
            release_runner = threading.Event()
            before_write: list[tuple[bool, tuple[int, str], bool]] = []
            after_write: list[tuple[bool, tuple[int, str], bool]] = []
            original_write = _write_project_stage_state_record

            def observed_write(**kwargs):  # noqa: ANN003
                if kwargs.get("source") == "runner_start" and kwargs.get("status") == "running":
                    with server._worker_registry_lock:  # noqa: SLF001
                        current = next(
                            item
                            for item in server._runner_executions.values()  # noqa: SLF001
                            if item.runner_id != "runner-old"
                        )
                        before_write.append(
                            (
                                current.generation_registered,
                                server._scope_generation_ledger[(project_dir, "需求A")],  # noqa: SLF001
                                server._runner_generation_is_superseded_locked("runner-old"),  # noqa: SLF001
                            )
                        )
                return original_write(**kwargs)

            def runner() -> int:
                with server._worker_registry_lock:  # noqa: SLF001
                    current = next(
                        item
                        for item in server._runner_executions.values()  # noqa: SLF001
                        if item.runner_id != "runner-old"
                    )
                    after_write.append(
                        (
                            current.generation_registered,
                            server._scope_generation_ledger[(project_dir, "需求A")],  # noqa: SLF001
                            server._runner_generation_is_superseded_locked("runner-old"),  # noqa: SLF001
                        )
                    )
                runner_started.set()
                release_runner.wait(timeout=2.0)
                return 0

            with patch(
                "T11_tui_backend._write_project_stage_state_record",
                side_effect=observed_write,
            ), patch.object(server, "_cleanup_stage_orphans_before_runner_start"), patch.object(
                server,
                "_schedule_flow_snapshot_update",
            ):
                server._run_in_thread(  # noqa: SLF001
                    "req-direct-register",
                    "stage.a06.start",
                    runner,
                    argv=["--project-dir", project_dir, "--requirement-name", "需求A"],
                    respond=False,
                )
                self.assertTrue(runner_started.wait(timeout=1.0))
                active_threads = [
                    thread
                    for key, thread in server._workers.items()  # noqa: SLF001
                    if key != "old"
                ]
                release_runner.set()
                for thread in active_threads:
                    thread.join(timeout=2.0)

        self.assertEqual(before_write, [(False, (1, "runner-old"), False)])
        self.assertEqual(len(after_write), 1)
        self.assertTrue(after_write[0][0])
        self.assertEqual(after_write[0][1][0], 2)
        self.assertNotEqual(after_write[0][1][1], "runner-old")
        self.assertTrue(after_write[0][2])

    def test_direct_runner_start_write_failure_does_not_supersede_or_create_phantom_failure(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = str(Path(tmpdir).resolve())
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            old_execution = RunnerExecutionState(
                runner_id="runner-old",
                action="stage.a06.start",
                stage_seq=1,
                project_dir=project_dir,
                requirement_name="需求A",
                current_action="stage.a06.start",
                current_stage_seq=1,
                registered_project_dir=project_dir,
                registered_requirement_name="需求A",
                generation_registered=True,
            )
            server._workers["old"] = SimpleNamespace(is_alive=lambda: True)  # noqa: SLF001
            server._runner_executions["old"] = old_execution  # noqa: SLF001
            server._stage_seq_counter = 1  # noqa: SLF001
            server._record_scope_generation(  # noqa: SLF001
                runner_id="runner-old",
                stage_seq=1,
                project_dir=project_dir,
                requirement_name="需求A",
            )
            write_entered = threading.Event()
            release_write = threading.Event()
            runner_called = threading.Event()
            observed_registration: list[bool] = []

            def fail_start_write(**_kwargs):  # noqa: ANN003
                with server._worker_registry_lock:  # noqa: SLF001
                    current = next(
                        item
                        for item in server._runner_executions.values()  # noqa: SLF001
                        if item.runner_id != "runner-old"
                    )
                    observed_registration.append(current.generation_registered)
                write_entered.set()
                release_write.wait(timeout=2.0)
                return None

            with patch(
                "T11_tui_backend._write_project_stage_state_record",
                side_effect=fail_start_write,
            ), patch.object(server, "_commit_runner_failure") as commit_failure:
                server._run_in_thread(  # noqa: SLF001
                    "req-direct-write-failure",
                    "stage.a06.start",
                    lambda: runner_called.set(),
                    argv=["--project-dir", project_dir, "--requirement-name", "需求A"],
                    respond=False,
                )
                self.assertTrue(write_entered.wait(timeout=1.0))
                self.assertFalse(server._runner_generation_is_superseded("runner-old"))  # noqa: SLF001
                active_threads = [
                    thread
                    for key, thread in server._workers.items()  # noqa: SLF001
                    if key != "old"
                ]
                release_write.set()
                for thread in active_threads:
                    thread.join(timeout=2.0)

                commit_failure.assert_not_called()

            self.assertEqual(observed_registration, [False])
            self.assertFalse(runner_called.is_set())
            self.assertEqual(
                server._scope_generation_ledger[(project_dir, "需求A")],  # noqa: SLF001
                (1, "runner-old"),
            )
            self.assertFalse(server._runner_generation_is_superseded("runner-old"))  # noqa: SLF001
            self.assertFalse((Path(project_dir) / ".tmux_workflow").exists())

    def test_unscoped_a00_scope_binding_rebases_generation_and_persists_current_child(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir).resolve()
            record_dir = project_dir / ".tmux_workflow" / "需求A" / "stages"
            record_dir.mkdir(parents=True)
            old_failure = record_dir / "stage_a04_start.failure.json"
            old_payload = {
                "action": "stage.a04.start",
                "status": "failed",
                "stage_seq": 9,
                "runner_id": "runner-old",
                "source": "runner_failure",
                "failure_kind": "runner_failure",
            }
            (record_dir / "stage_a04_start.state.json").write_text(
                json.dumps(old_payload, ensure_ascii=False),
                encoding="utf-8",
            )
            old_failure.write_text(json.dumps(old_payload, ensure_ascii=False), encoding="utf-8")
            (record_dir / "latest_failure.json").write_text(
                json.dumps(old_payload, ensure_ascii=False),
                encoding="utf-8",
            )
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            child_started = threading.Event()
            release_runner = threading.Event()

            def root_runner() -> int:
                server._handle_runtime_stage_change("stage.a05.start")  # noqa: SLF001
                child_started.set()
                release_runner.wait(timeout=2.0)
                return 0

            server._run_in_thread(  # noqa: SLF001
                "req-unscoped-a00",
                "workflow.a00.start",
                root_runner,
                argv=[],
                respond=False,
            )
            self.assertTrue(child_started.wait(timeout=1.0))
            with server._worker_registry_lock:  # noqa: SLF001
                execution = next(iter(server._runner_executions.values()))  # noqa: SLF001
            self.assertFalse(execution.generation_registered)
            runner_id = execution.runner_id

            server._update_context_from_prompt_response(  # noqa: SLF001
                PendingPromptState(
                    prompt_id="project",
                    prompt_type="text",
                    payload={"title": "项目工作目录"},
                    owner_runner_id=runner_id,
                ),
                {"value": str(project_dir)},
            )
            self.assertFalse(execution.generation_registered)
            server._update_context_from_prompt_response(  # noqa: SLF001
                PendingPromptState(
                    prompt_id="requirement",
                    prompt_type="text",
                    payload={"title": "需求名称"},
                    owner_runner_id=runner_id,
                ),
                {"value": "需求A"},
            )

            root_state = json.loads(
                (record_dir / "workflow_a00_start.state.json").read_text(encoding="utf-8")
            )
            child_state = json.loads(
                (record_dir / "stage_a05_start.state.json").read_text(encoding="utf-8")
            )
            try:
                self.assertTrue(execution.generation_registered)
                self.assertEqual(execution.current_action, "stage.a05.start")
                self.assertEqual(root_state["runner_id"], runner_id)
                self.assertEqual(child_state["runner_id"], runner_id)
                self.assertGreater(root_state["stage_seq"], 9)
                self.assertGreater(child_state["stage_seq"], root_state["stage_seq"])
                self.assertEqual(execution.current_stage_seq, child_state["stage_seq"])
                self.assertFalse((record_dir / "stage_a04_start.state.json").exists())
                self.assertTrue(old_failure.exists())
                self.assertFalse((record_dir / "latest_failure.json").exists())
                self.assertEqual(server._display_action, "stage.a05.start")  # noqa: SLF001
                self.assertEqual(server._display_runner_id, runner_id)  # noqa: SLF001
            finally:
                release_runner.set()
                for thread in list(server._workers.values()):  # noqa: SLF001
                    thread.join(timeout=2.0)

    def test_scope_registration_write_failure_does_not_release_prompt_and_can_retry(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir).resolve()
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            execution = RunnerExecutionState(
                runner_id="runner-scope-retry",
                action="workflow.a00.start",
                stage_seq=1,
                project_dir=str(project_dir),
                requirement_name="",
                current_action="workflow.a00.start",
                current_stage_seq=1,
            )
            server._workers["scope-retry"] = SimpleNamespace(is_alive=lambda: True)  # noqa: SLF001
            server._runner_executions["scope-retry"] = execution  # noqa: SLF001
            server._set_context(project_dir=str(project_dir), action="workflow.a00.start")  # noqa: SLF001
            prompt = PendingPromptState(
                prompt_id="prompt-scope-retry",
                prompt_type="text",
                payload={"title": "需求名称"},
                owner_runner_id=execution.runner_id,
            )
            prompt_queue: queue.Queue[dict[str, object]] = queue.Queue(maxsize=1)
            server._pending_prompts[prompt.prompt_id] = prompt  # noqa: SLF001
            server._pending_prompt = prompt  # noqa: SLF001
            server._prompt_broker._pending[prompt.prompt_id] = prompt_queue  # noqa: SLF001

            with patch.object(server, "_schedule_flow_snapshot_update"), patch(
                "T11_tui_backend._write_project_stage_state_record",
                return_value=None,
            ):
                with self.assertRaisesRegex(RuntimeError, "could not be persisted"):
                    server._prompt_broker.resolve(prompt.prompt_id, {"value": "需求A"})  # noqa: SLF001

            self.assertFalse(execution.generation_registered)
            self.assertEqual(server._context.requirement_name, "")  # noqa: SLF001
            self.assertIn(prompt.prompt_id, server._pending_prompts)  # noqa: SLF001
            self.assertIn(prompt.prompt_id, server._prompt_broker._pending)  # noqa: SLF001
            self.assertNotIn(prompt.prompt_id, server._prompt_broker._claimed_prompts)  # noqa: SLF001
            self.assertTrue(prompt_queue.empty())

            with patch.object(server, "_schedule_flow_snapshot_update"):
                server._prompt_broker.resolve(prompt.prompt_id, {"value": "需求A"})  # noqa: SLF001

            self.assertEqual(prompt_queue.get_nowait()["value"], "需求A")
            self.assertTrue(execution.generation_registered)
            self.assertEqual(server._context.requirement_name, "需求A")  # noqa: SLF001
            self.assertNotIn(prompt.prompt_id, server._pending_prompts)  # noqa: SLF001
            state_path = (
                project_dir
                / ".tmux_workflow"
                / "需求A"
                / "stages"
                / "workflow_a00_start.state.json"
            )
            self.assertEqual(
                json.loads(state_path.read_text(encoding="utf-8"))["runner_id"],
                execution.runner_id,
            )

    def test_a00_root_and_child_scope_registration_rolls_back_then_prompt_retry_succeeds(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir).resolve()
            record_dir = project_dir / ".tmux_workflow" / "需求A" / "stages"
            record_dir.mkdir(parents=True)
            root_path = record_dir / "workflow_a00_start.state.json"
            old_child_path = record_dir / "stage_a04_start.state.json"
            latest_failure_path = record_dir / "latest_failure.json"
            root_path.write_text(
                json.dumps(
                    {
                        "action": "workflow.a00.start",
                        "status": "failed",
                        "stage_seq": 8,
                        "runner_id": "runner-old",
                        "source": "runner_failure",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            old_child_path.write_text(
                json.dumps(
                    {
                        "action": "stage.a04.start",
                        "status": "failed",
                        "stage_seq": 9,
                        "runner_id": "runner-old",
                        "source": "runner_failure",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            latest_failure_path.write_text(
                json.dumps({"action": "stage.a04.start", "runner_id": "runner-old"}),
                encoding="utf-8",
            )
            original_files = {
                path.name: path.read_bytes()
                for path in (root_path, old_child_path, latest_failure_path)
            }
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            execution = RunnerExecutionState(
                runner_id="runner-a00-transaction",
                action="workflow.a00.start",
                stage_seq=1,
                project_dir=str(project_dir),
                requirement_name="",
                current_action="stage.a05.start",
                current_stage_seq=2,
            )
            server._workers["a00-transaction"] = SimpleNamespace(is_alive=lambda: True)  # noqa: SLF001
            server._runner_executions["a00-transaction"] = execution  # noqa: SLF001
            server._set_context(project_dir=str(project_dir), action="stage.a05.start")  # noqa: SLF001
            prompt = PendingPromptState(
                prompt_id="prompt-a00-transaction",
                prompt_type="text",
                payload={"title": "需求名称"},
                owner_runner_id=execution.runner_id,
            )
            prompt_queue: queue.Queue[dict[str, object]] = queue.Queue(maxsize=1)
            server._pending_prompts[prompt.prompt_id] = prompt  # noqa: SLF001
            server._pending_prompt = prompt  # noqa: SLF001
            server._prompt_broker._pending[prompt.prompt_id] = prompt_queue  # noqa: SLF001
            original_write = _write_project_stage_state_record

            def fail_after_child_write(**kwargs):  # noqa: ANN003
                state_path = original_write(**kwargs)
                if kwargs.get("action") == "stage.a05.start":
                    return None
                return state_path

            with patch.object(server, "_schedule_flow_snapshot_update"), patch(
                "T11_tui_backend._write_project_stage_state_record",
                side_effect=fail_after_child_write,
            ):
                with self.assertRaisesRegex(RuntimeError, "action=stage.a05.start"):
                    server._prompt_broker.resolve(prompt.prompt_id, {"value": "需求A"})  # noqa: SLF001

            self.assertFalse(execution.generation_registered)
            self.assertEqual(server._context.requirement_name, "")  # noqa: SLF001
            self.assertIn(prompt.prompt_id, server._pending_prompts)  # noqa: SLF001
            self.assertNotIn(prompt.prompt_id, server._prompt_broker._claimed_prompts)  # noqa: SLF001
            self.assertFalse((record_dir / "stage_a05_start.state.json").exists())
            for name, payload in original_files.items():
                self.assertEqual((record_dir / name).read_bytes(), payload)

            with patch.object(server, "_schedule_flow_snapshot_update"):
                accepted = server._prompt_broker.resolve(prompt.prompt_id, {"value": "需求A"})  # noqa: SLF001

            self.assertTrue(accepted)
            self.assertEqual(prompt_queue.get_nowait()["value"], "需求A")
            self.assertTrue(execution.generation_registered)
            self.assertEqual(server._context.requirement_name, "需求A")  # noqa: SLF001
            self.assertTrue(root_path.is_file())
            self.assertTrue((record_dir / "stage_a05_start.state.json").is_file())
            self.assertFalse(old_child_path.exists())
            self.assertFalse(latest_failure_path.exists())

    def test_late_project_scope_callback_keeps_resolved_requirement_and_runner_owner(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir).resolve()
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            execution = RunnerExecutionState(
                runner_id="runner-owned-prompt",
                action="workflow.a00.start",
                stage_seq=1,
                project_dir="",
                requirement_name="需求A",
                current_action="stage.a05.start",
                current_stage_seq=2,
            )
            server._workers["owned"] = SimpleNamespace(is_alive=lambda: True)  # noqa: SLF001
            server._runner_executions["owned"] = execution  # noqa: SLF001
            server._set_context(requirement_name="需求A", action="stage.a05.start")  # noqa: SLF001

            server._update_context_from_prompt_response(  # noqa: SLF001
                PendingPromptState(
                    prompt_id="late-project",
                    prompt_type="text",
                    payload={"title": "项目工作目录"},
                    owner_runner_id=execution.runner_id,
                ),
                {"value": str(project_dir)},
            )

            child_state = json.loads(
                (
                    project_dir
                    / ".tmux_workflow"
                    / "需求A"
                    / "stages"
                    / "stage_a05_start.state.json"
                ).read_text(encoding="utf-8")
            )

        self.assertEqual(server._context.requirement_name, "需求A")  # noqa: SLF001
        self.assertEqual(execution.requirement_name, "需求A")
        self.assertTrue(execution.generation_registered)
        self.assertEqual(child_state["runner_id"], execution.runner_id)

    def test_unregistered_a00_owner_can_replace_project_after_back_then_scope_freezes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_a = (Path(tmpdir) / "project-a").resolve()
            project_b = (Path(tmpdir) / "project-b").resolve()
            project_a.mkdir()
            project_b.mkdir()
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            execution = RunnerExecutionState(
                runner_id="runner-back-project",
                action="workflow.a00.start",
                stage_seq=1,
                project_dir="",
                requirement_name="",
                current_action="workflow.a00.start",
                current_stage_seq=1,
            )
            server._workers["owned"] = SimpleNamespace(is_alive=lambda: True)  # noqa: SLF001
            server._runner_executions["owned"] = execution  # noqa: SLF001
            server._set_context(action="workflow.a00.start")  # noqa: SLF001

            def project_prompt(prompt_id: str) -> PendingPromptState:
                return PendingPromptState(
                    prompt_id=prompt_id,
                    prompt_type="text",
                    payload={"title": "项目工作目录"},
                    owner_runner_id=execution.runner_id,
                )

            server._update_context_from_prompt_response(  # noqa: SLF001
                project_prompt("project-a"),
                {"value": str(project_a)},
            )
            self.assertEqual(execution.project_dir, str(project_a))
            self.assertFalse(execution.generation_registered)

            # The interactive workflow went Back and selected another project.
            server._update_context_from_prompt_response(  # noqa: SLF001
                project_prompt("project-b"),
                {"value": str(project_b)},
            )
            self.assertEqual(execution.project_dir, str(project_b))
            self.assertEqual(server._context.project_dir, str(project_b))  # noqa: SLF001
            self.assertFalse(execution.generation_registered)

            server._update_context_from_prompt_response(  # noqa: SLF001
                PendingPromptState(
                    prompt_id="requirement-b",
                    prompt_type="text",
                    payload={"title": "需求名称"},
                    owner_runner_id=execution.runner_id,
                ),
                {"value": "需求B"},
            )
            state_path = (
                project_b
                / ".tmux_workflow"
                / "需求B"
                / "stages"
                / "workflow_a00_start.state.json"
            )
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertTrue(execution.generation_registered)
            self.assertEqual(execution.registered_project_dir, str(project_b))
            self.assertEqual(execution.registered_requirement_name, "需求B")
            self.assertEqual(state["runner_id"], execution.runner_id)
            self.assertFalse((project_a / ".tmux_workflow").exists())

            # Once persisted, late/cross-scope prompt callbacks are rejected.
            server._update_context_from_prompt_response(  # noqa: SLF001
                project_prompt("late-project-a"),
                {"value": str(project_a)},
            )

        self.assertEqual(execution.project_dir, str(project_b))
        self.assertEqual(server._context.project_dir, str(project_b))  # noqa: SLF001

    def test_live_explicit_runner_is_not_replaced_by_old_runtime_inference_terminal(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir).resolve()
            record_dir = project_dir / ".tmux_workflow" / "需求A" / "stages"
            record_dir.mkdir(parents=True)
            state_path = record_dir / "stage_a05_start.state.json"
            state_path.write_text(
                json.dumps(
                    {
                        "action": "stage.a05.start",
                        "status": "failed",
                        "stage_seq": 9,
                        "runner_id": "runner-old",
                        "source": "runner_failure",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._set_context(  # noqa: SLF001
                project_dir=str(project_dir),
                requirement_name="需求A",
                action="stage.a05.start",
            )
            server._workers["current"] = SimpleNamespace(is_alive=lambda: True)  # noqa: SLF001
            server._runner_executions["current"] = SimpleNamespace(  # noqa: SLF001
                runner_id="runner-current",
                action="workflow.a00.start",
                stage_seq=10,
                current_action="stage.a05.start",
                current_stage_seq=10,
                project_dir=str(project_dir),
                requirement_name="需求A",
                generation_registered=True,
                terminal_source="",
            )
            server._display_action = "stage.a05.start"  # noqa: SLF001
            server._display_status = "running"  # noqa: SLF001
            server._display_stage_seq = 10  # noqa: SLF001
            server._display_runner_id = "runner-current"  # noqa: SLF001

            with patch.object(server, "_infer_runtime_stage_status", return_value="running"):
                accepted = server._emit_display_stage_state(  # noqa: SLF001
                    preferred_status="running",
                    preferred_action="stage.a05.start",
                    preferred_stage_seq=9,
                    source="runtime_inference",
                    force=True,
                )

            persisted = json.loads(state_path.read_text(encoding="utf-8"))

        self.assertTrue(accepted)
        self.assertEqual(server._display_runner_id, "runner-current")  # noqa: SLF001
        self.assertEqual(server._display_stage_seq, 10)  # noqa: SLF001
        self.assertEqual(server._display_status, "running")  # noqa: SLF001
        self.assertEqual(persisted["runner_id"], "runner-old")
        self.assertEqual(persisted["source"], "runner_failure")

    def test_partial_scope_bind_race_keeps_live_runner_owner_for_100_inferences(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir).resolve()
            record_dir = project_dir / ".tmux_workflow" / "需求A" / "stages"
            record_dir.mkdir(parents=True)
            (record_dir / "stage_a05_start.state.json").write_text(
                json.dumps(
                    {
                        "action": "stage.a05.start",
                        "status": "failed",
                        "stage_seq": 9,
                        "runner_id": "runner-old",
                        "source": "runner_failure",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            execution = SimpleNamespace(
                runner_id="runner-current",
                action="workflow.a00.start",
                stage_seq=10,
                current_action="stage.a05.start",
                current_stage_seq=10,
                project_dir=str(project_dir),
                requirement_name="",
                generation_registered=False,
                terminal_source="",
            )
            server._workers["current"] = SimpleNamespace(is_alive=lambda: True)  # noqa: SLF001
            server._runner_executions["current"] = execution  # noqa: SLF001

            observed: list[tuple[bool, str, int, str]] = []
            for _iteration in range(100):
                execution.requirement_name = ""
                server._set_context(  # noqa: SLF001
                    project_dir=str(project_dir),
                    requirement_name="",
                    action="stage.a05.start",
                )
                server._display_action = "stage.a05.start"  # noqa: SLF001
                server._display_status = "running"  # noqa: SLF001
                server._display_stage_seq = 10  # noqa: SLF001
                server._display_runner_id = "runner-current"  # noqa: SLF001
                race_barrier = threading.Barrier(2)

                def bind_scope() -> None:
                    server._set_context(requirement_name="需求A")  # noqa: SLF001
                    race_barrier.wait(timeout=1.0)
                    race_barrier.wait(timeout=1.0)
                    execution.requirement_name = "需求A"

                def infer_runtime() -> None:
                    race_barrier.wait(timeout=1.0)
                    accepted = server._emit_display_stage_state(  # noqa: SLF001
                        preferred_status="running",
                        preferred_action="stage.a05.start",
                        preferred_stage_seq=9,
                        source="runtime_inference",
                        force=True,
                    )
                    observed.append(
                        (
                            accepted,
                            server._display_runner_id,  # noqa: SLF001
                            server._display_stage_seq,  # noqa: SLF001
                            server._display_status,  # noqa: SLF001
                        )
                    )
                    race_barrier.wait(timeout=1.0)

                bind_thread = threading.Thread(target=bind_scope)
                infer_thread = threading.Thread(target=infer_runtime)
                bind_thread.start()
                infer_thread.start()
                bind_thread.join(timeout=2.0)
                infer_thread.join(timeout=2.0)
                self.assertFalse(bind_thread.is_alive())
                self.assertFalse(infer_thread.is_alive())

        self.assertEqual(len(observed), 100)
        self.assertTrue(all(item == (True, "runner-current", 10, "running") for item in observed))

    def test_a00_stage_sequence_allocation_cannot_overtake_explicit_runner_registration(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = str(Path(tmpdir).resolve())
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._set_context(  # noqa: SLF001
                project_dir=project_dir,
                requirement_name="需求A",
                action="stage.a06.start",
            )
            server._display_action = "stage.a06.start"  # noqa: SLF001
            server._display_status = "running"  # noqa: SLF001
            server._display_stage_seq = 1  # noqa: SLF001
            server._display_runner_id = "runner-old"  # noqa: SLF001
            old_execution = SimpleNamespace(
                runner_id="runner-old",
                action="workflow.a00.start",
                stage_seq=1,
                project_dir=project_dir,
                requirement_name="需求A",
            )
            server._workers["old"] = SimpleNamespace(is_alive=lambda: True)  # noqa: SLF001
            server._runner_executions["old"] = old_execution  # noqa: SLF001
            allocation_entered = threading.Event()
            release_allocation = threading.Event()
            new_runner_started = threading.Event()
            release_new_runner = threading.Event()
            allocation_count = 0
            allocation_lock = threading.Lock()

            def controlled_allocate(**_kwargs):  # noqa: ANN003
                nonlocal allocation_count
                with allocation_lock:
                    allocation_count += 1
                    current = allocation_count
                if current == 1:
                    allocation_entered.set()
                    release_allocation.wait(timeout=2.0)
                return current + 1

            def transition() -> None:
                server._runner_local.runner_id = "runner-old"  # noqa: SLF001
                server._runner_local.execution = old_execution  # noqa: SLF001
                try:
                    server._handle_runtime_stage_change("stage.a07.start")  # noqa: SLF001
                finally:
                    server._runner_local.runner_id = ""  # noqa: SLF001
                    server._runner_local.execution = None  # noqa: SLF001

            def explicit_runner() -> int:
                new_runner_started.set()
                release_new_runner.wait(timeout=2.0)
                return 0

            argv = ["--project-dir", project_dir, "--requirement-name", "需求A"]
            with patch.object(server, "_allocate_stage_seq", side_effect=controlled_allocate):
                transition_thread = threading.Thread(target=transition)
                transition_thread.start()
                self.assertTrue(allocation_entered.wait(timeout=1.0))
                launch_thread = threading.Thread(
                    target=lambda: server._run_in_thread(  # noqa: SLF001
                        "req-new",
                        "stage.a06.start",
                        explicit_runner,
                        argv=argv,
                        respond=False,
                    )
                )
                launch_thread.start()
                self.assertFalse(new_runner_started.wait(timeout=0.05))
                release_allocation.set()
                transition_thread.join(timeout=2.0)
                launch_thread.join(timeout=2.0)
                self.assertTrue(new_runner_started.wait(timeout=1.0))
                self.assertEqual(allocation_count, 2)
                self.assertNotEqual(server._display_runner_id, "runner-old")  # noqa: SLF001
                self.assertEqual(server._display_stage_seq, 3)  # noqa: SLF001
                release_new_runner.set()
                for worker_key, thread in list(server._workers.items()):  # noqa: SLF001
                    if worker_key != "old":
                        thread.join(timeout=2.0)

    def test_previous_stage_write_cannot_race_new_runner_registration(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = str(Path(tmpdir).resolve())
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._set_context(  # noqa: SLF001
                project_dir=project_dir,
                requirement_name="需求A",
                action="stage.a06.start",
            )
            server._display_action = "stage.a06.start"  # noqa: SLF001
            server._display_status = "running"  # noqa: SLF001
            server._display_stage_seq = 1  # noqa: SLF001
            server._display_runner_id = "runner-old"  # noqa: SLF001
            old_execution = RunnerExecutionState(
                runner_id="runner-old",
                action="workflow.a00.start",
                stage_seq=1,
                project_dir=project_dir,
                requirement_name="需求A",
                current_action="stage.a06.start",
                current_stage_seq=1,
                registered_project_dir=project_dir,
                registered_requirement_name="需求A",
                generation_registered=True,
            )
            server._workers["old"] = SimpleNamespace(is_alive=lambda: True)  # noqa: SLF001
            server._runner_executions["old"] = old_execution  # noqa: SLF001
            previous_write_entered = threading.Event()
            release_previous_write = threading.Event()
            launch_attempted = threading.Event()
            new_registration_allocated = threading.Event()
            new_runner_started = threading.Event()
            release_new_runner = threading.Event()
            transition_errors: list[BaseException] = []
            original_persist_previous = server._persist_previous_runtime_stage_exit  # noqa: SLF001
            original_allocate_stage_seq = server._allocate_stage_seq  # noqa: SLF001

            def blocked_persist_previous(*args, **kwargs):  # noqa: ANN002, ANN003
                previous_write_entered.set()
                release_previous_write.wait(timeout=2.0)
                return original_persist_previous(*args, **kwargs)

            def transition() -> None:
                server._runner_local.runner_id = "runner-old"  # noqa: SLF001
                server._runner_local.execution = old_execution  # noqa: SLF001
                try:
                    server._handle_runtime_stage_change("stage.a07.start")  # noqa: SLF001
                except BaseException as error:  # noqa: BLE001
                    transition_errors.append(error)
                finally:
                    server._runner_local.runner_id = ""  # noqa: SLF001
                    server._runner_local.execution = None  # noqa: SLF001

            def observed_allocate_stage_seq(**kwargs):  # noqa: ANN003
                if threading.current_thread().name == "new-runner-registration":
                    new_registration_allocated.set()
                return original_allocate_stage_seq(**kwargs)

            def explicit_runner() -> int:
                new_runner_started.set()
                release_new_runner.wait(timeout=2.0)
                return 0

            def launch_new_runner() -> None:
                launch_attempted.set()
                server._run_in_thread(  # noqa: SLF001
                    "req-new-after-previous-write",
                    "stage.a06.start",
                    explicit_runner,
                    argv=argv,
                    respond=False,
                )

            argv = ["--project-dir", project_dir, "--requirement-name", "需求A"]
            with patch.object(
                server,
                "_persist_previous_runtime_stage_exit",
                side_effect=blocked_persist_previous,
            ), patch.object(
                server,
                "_cleanup_stage_orphans_before_runner_start",
            ), patch.object(
                server,
                "_schedule_flow_snapshot_update",
            ), patch.object(
                server,
                "_allocate_stage_seq",
                side_effect=observed_allocate_stage_seq,
            ):
                transition_thread = threading.Thread(target=transition)
                transition_thread.start()
                self.assertTrue(previous_write_entered.wait(timeout=1.0))
                launch_thread = threading.Thread(
                    target=launch_new_runner,
                    name="new-runner-registration",
                )
                launch_thread.start()
                self.assertTrue(launch_attempted.wait(timeout=1.0))
                self.assertFalse(new_registration_allocated.wait(timeout=0.1))
                self.assertFalse(new_runner_started.is_set())

                release_previous_write.set()
                transition_thread.join(timeout=2.0)
                launch_thread.join(timeout=2.0)
                self.assertTrue(new_registration_allocated.wait(timeout=1.0))
                self.assertTrue(new_runner_started.wait(timeout=1.0))

                state_path = (
                    Path(project_dir)
                    / ".tmux_workflow"
                    / "需求A"
                    / "stages"
                    / "stage_a06_start.state.json"
                )
                persisted = json.loads(state_path.read_text(encoding="utf-8"))
                self.assertEqual(transition_errors, [])
                self.assertNotEqual(persisted["runner_id"], "runner-old")
                self.assertEqual(persisted["status"], "running")
                self.assertEqual(server._context.current_action, "stage.a06.start")  # noqa: SLF001
                self.assertNotEqual(server._display_runner_id, "runner-old")  # noqa: SLF001

                release_new_runner.set()
                for worker_key, thread in list(server._workers.items()):  # noqa: SLF001
                    if worker_key != "old":
                        thread.join(timeout=2.0)

    def test_late_old_runner_display_transition_cannot_overwrite_new_generation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = str(Path(tmpdir).resolve())
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._set_context(  # noqa: SLF001
                project_dir=project_dir,
                requirement_name="需求A",
                action="stage.a04.start",
            )
            old_execution = RunnerExecutionState(
                runner_id="runner-old",
                action="stage.a04.start",
                stage_seq=5,
                project_dir=project_dir,
                requirement_name="需求A",
                current_action="stage.a04.start",
                current_stage_seq=5,
                registered_project_dir=project_dir,
                registered_requirement_name="需求A",
                generation_registered=True,
            )
            server._workers["old"] = SimpleNamespace(is_alive=lambda: True)  # noqa: SLF001
            server._runner_executions["old"] = old_execution  # noqa: SLF001
            old_derive_entered = threading.Event()
            release_old_derive = threading.Event()
            old_results: list[bool] = []
            original_derive = server._derive_display_stage_state  # noqa: SLF001

            def block_old_after_fast_generation_check(*args, **kwargs):  # noqa: ANN002, ANN003
                result = original_derive(*args, **kwargs)
                if threading.current_thread().name == "late-old-display":
                    old_derive_entered.set()
                    release_old_derive.wait(timeout=2.0)
                return result

            def emit_old() -> None:
                old_results.append(
                    server._emit_display_stage_state(  # noqa: SLF001
                        preferred_status="running",
                        preferred_action="stage.a04.start",
                        preferred_stage_seq=5,
                        preferred_runner_id="runner-old",
                        source="runner_start",
                        force=True,
                    )
                )

            with patch.object(
                server,
                "_derive_display_stage_state",
                side_effect=block_old_after_fast_generation_check,
            ):
                old_thread = threading.Thread(target=emit_old, name="late-old-display")
                old_thread.start()
                self.assertTrue(old_derive_entered.wait(timeout=1.0))

                new_execution = RunnerExecutionState(
                    runner_id="runner-new",
                    action="stage.a05.start",
                    stage_seq=6,
                    project_dir=project_dir,
                    requirement_name="需求A",
                    current_action="stage.a05.start",
                    current_stage_seq=6,
                    registered_project_dir=project_dir,
                    registered_requirement_name="需求A",
                    generation_registered=True,
                )
                with server._worker_registry_lock:  # noqa: SLF001
                    server._workers["new"] = SimpleNamespace(is_alive=lambda: True)  # noqa: SLF001
                    server._runner_executions["new"] = new_execution  # noqa: SLF001
                    server._record_scope_generation(  # noqa: SLF001
                        runner_id="runner-new",
                        stage_seq=6,
                        project_dir=project_dir,
                        requirement_name="需求A",
                    )
                new_accepted = server._emit_display_stage_state(  # noqa: SLF001
                    preferred_status="running",
                    preferred_action="stage.a05.start",
                    preferred_stage_seq=6,
                    preferred_runner_id="runner-new",
                    source="runner_start",
                    force=True,
                )
                release_old_derive.set()
                old_thread.join(timeout=2.0)

            self.assertFalse(old_thread.is_alive())
            self.assertTrue(new_accepted)
            self.assertEqual(old_results, [False])
            self.assertEqual(server._display_action, "stage.a05.start")  # noqa: SLF001
            self.assertEqual(server._display_stage_seq, 6)  # noqa: SLF001
            self.assertEqual(server._display_runner_id, "runner-new")  # noqa: SLF001

    def test_cross_scope_start_is_rejected_before_global_context_changes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_a = Path(tmpdir, "project-a").resolve()
            project_b = Path(tmpdir, "project-b").resolve()
            project_a.mkdir()
            project_b.mkdir()
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._set_context(  # noqa: SLF001
                project_dir=str(project_a),
                requirement_name="需求A",
                action="stage.a06.start",
            )
            started = threading.Event()
            release = threading.Event()

            def active_runner():
                started.set()
                release.wait(timeout=2.0)
                return 0

            argv_a = ["--project-dir", str(project_a), "--requirement-name", "需求A"]
            server._run_in_thread("req-a", "stage.a06.start", active_runner, argv=argv_a, respond=False)  # noqa: SLF001
            self.assertTrue(started.wait(timeout=1.0))
            argv_b = ["--project-dir", str(project_b), "--requirement-name", "需求B"]
            try:
                with patch("T11_tui_backend.run_task_split_stage") as run_project_b:
                    with self.assertRaisesRegex(RuntimeError, "避免阶段状态串写"):
                        server.dispatch_action(
                            "stage.a06.start",
                            {"argv": argv_b},
                            request_id="req-b",
                            respond=False,
                        )
                    run_project_b.assert_not_called()
                self.assertEqual(server._context.project_dir, str(project_a))  # noqa: SLF001
                self.assertEqual(server._context.requirement_name, "需求A")  # noqa: SLF001
            finally:
                release.set()
                for thread in list(server._workers.values()):  # noqa: SLF001
                    thread.join(timeout=2.0)

    def test_concurrent_cross_scope_starts_atomically_register_only_one_runner(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_a = Path(tmpdir, "project-a").resolve()
            project_b = Path(tmpdir, "project-b").resolve()
            project_a.mkdir()
            project_b.mkdir()
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            start_barrier = threading.Barrier(3)
            release_runner = threading.Event()
            outcomes: list[tuple[str, str]] = []
            outcomes_lock = threading.Lock()

            def launch(label: str, project_dir: Path, requirement_name: str) -> None:
                argv = [
                    "--project-dir",
                    str(project_dir),
                    "--requirement-name",
                    requirement_name,
                ]
                def waiting_runner() -> int:
                    release_runner.wait(timeout=2.0)
                    return 0

                start_barrier.wait(timeout=2.0)
                try:
                    server._run_in_thread(  # noqa: SLF001
                        f"req-{label}",
                        "stage.a06.start",
                        waiting_runner,
                        argv=argv,
                        respond=False,
                    )
                except RuntimeError as error:
                    outcome = (label, str(error))
                else:
                    outcome = (label, "accepted")
                with outcomes_lock:
                    outcomes.append(outcome)

            launch_a = threading.Thread(target=launch, args=("a", project_a, "需求A"))
            launch_b = threading.Thread(target=launch, args=("b", project_b, "需求B"))
            launch_a.start()
            launch_b.start()
            start_barrier.wait(timeout=2.0)
            launch_a.join(timeout=2.0)
            launch_b.join(timeout=2.0)
            try:
                accepted = [label for label, result in outcomes if result == "accepted"]
                rejected = [result for _label, result in outcomes if result != "accepted"]
                self.assertEqual(len(accepted), 1)
                self.assertEqual(len(rejected), 1)
                self.assertIn("避免阶段状态串写", rejected[0])
                with server._worker_registry_lock:  # noqa: SLF001
                    executions = list(server._runner_executions.values())  # noqa: SLF001
                self.assertEqual(len(executions), 1)
                execution = executions[0]
                expected_project = project_a if accepted[0] == "a" else project_b
                expected_requirement = "需求A" if accepted[0] == "a" else "需求B"
                self.assertEqual(execution.project_dir, str(expected_project))
                self.assertEqual(execution.requirement_name, expected_requirement)
                self.assertEqual(server._context.project_dir, str(expected_project))  # noqa: SLF001
                self.assertEqual(server._context.requirement_name, expected_requirement)  # noqa: SLF001
            finally:
                release_runner.set()
                for thread in list(server._workers.values()):  # noqa: SLF001
                    thread.join(timeout=2.0)

    def test_runner_failure_persists_to_execution_scope_after_global_context_changes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_a = Path(tmpdir, "project-a").resolve()
            project_b = Path(tmpdir, "project-b").resolve()
            project_a.mkdir()
            project_b.mkdir()
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            runner_started = threading.Event()
            release_runner = threading.Event()

            def failing_runner() -> int:
                runner_started.set()
                release_runner.wait(timeout=2.0)
                raise RuntimeError("execution scoped failure")

            argv = ["--project-dir", str(project_a), "--requirement-name", "需求A"]
            server._run_in_thread(  # noqa: SLF001
                "req-scoped-failure",
                "stage.a06.start",
                failing_runner,
                argv=argv,
                respond=False,
            )
            self.assertTrue(runner_started.wait(timeout=1.0))
            server._set_context(  # noqa: SLF001
                project_dir=str(project_b),
                requirement_name="需求B",
                action="control.b01.open",
            )
            release_runner.set()
            for thread in list(server._workers.values()):  # noqa: SLF001
                thread.join(timeout=2.0)

            failure_a = (
                project_a
                / ".tmux_workflow"
                / "需求A"
                / "stages"
                / "stage_a06_start.failure.json"
            )
            failure_b = (
                project_b
                / ".tmux_workflow"
                / "需求B"
                / "stages"
                / "stage_a06_start.failure.json"
            )
            self.assertTrue(failure_a.is_file())
            self.assertFalse(failure_b.exists())
            failure_payload = json.loads(failure_a.read_text(encoding="utf-8"))
            self.assertEqual(failure_payload["requirement_name"], "需求A")
            self.assertEqual(failure_payload["project_dir"], str(project_a))

    def test_a00_state_cleanup_happens_only_after_new_generation_is_persisted(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir).resolve()
            record_dir = project_dir / ".tmux_workflow" / "需求A" / "stages"
            record_dir.mkdir(parents=True)
            child_state = record_dir / "stage_a06_start.state.json"
            child_state.write_text('{"source":"runner_failure","status":"failed"}', encoding="utf-8")
            latest_failure = record_dir / "latest_failure.json"
            latest_failure.write_text('{"action":"workflow.a00.start"}', encoding="utf-8")

            with patch("T11_tui_backend._atomic_write_json", side_effect=OSError("disk full")):
                result = _write_project_stage_state_record(
                    project_dir=str(project_dir),
                    requirement_name="需求A",
                    action="workflow.a00.start",
                    status="running",
                    stage_seq=2,
                    runner_id="runner-new",
                    source="runner_start",
                )

            self.assertIsNone(result)
            self.assertTrue(child_state.exists())
            self.assertTrue(latest_failure.exists())

    def test_stage_sequence_continues_from_persisted_maximum_after_restart(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            record_dir = project_dir / ".tmux_workflow" / "需求A" / "stages"
            record_dir.mkdir(parents=True)
            (record_dir / "stage_a06_start.state.json").write_text(
                json.dumps({"action": "stage.a06.start", "stage_seq": 13}, ensure_ascii=False),
                encoding="utf-8",
            )
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._set_context(project_dir=str(project_dir), requirement_name="需求A", action="stage.a07.start")  # noqa: SLF001
            next_sequence = server._allocate_stage_seq()  # noqa: SLF001

        self.assertEqual(next_sequence, 14)

    def test_snapshot_reconciles_unowned_persisted_running_runner_as_interrupted(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir).resolve()
            record_dir = project_dir / ".tmux_workflow" / "需求A" / "stages"
            record_dir.mkdir(parents=True)
            state_path = record_dir / "stage_a06_start.state.json"
            state_path.write_text(
                json.dumps(
                    {
                        "action": "stage.a06.start",
                        "status": "running",
                        "project_dir": str(project_dir),
                        "requirement_name": "需求A",
                        "stage_seq": 5,
                        "runner_id": "runner-from-dead-backend",
                        "source": "runner_start",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._set_context(project_dir=str(project_dir), requirement_name="需求A", action="stage.a06.start")  # noqa: SLF001

            app = server._build_app_snapshot(  # noqa: SLF001
                runs=[],
                control={},
                hitl={"pending": False},
                attention={"pending": False},
                artifacts={"items": []},
            )
            reconciled_state = json.loads(state_path.read_text(encoding="utf-8"))

        self.assertEqual(reconciled_state["status"], "failed")
        self.assertEqual(reconciled_state["source"], "runner_failure")
        self.assertEqual(reconciled_state["failure_kind"], "runner_interrupted")
        self.assertEqual(reconciled_state["runner_id"], "runner-from-dead-backend")
        self.assertEqual(app["active_stage_status"], "failed")
        self.assertEqual(app["active_stage_failure"]["failure_kind"], "runner_interrupted")

    def test_explicit_new_generation_prevents_restart_reconciliation_of_old_running_state(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir).resolve()
            record_dir = project_dir / ".tmux_workflow" / "需求A" / "stages"
            record_dir.mkdir(parents=True)
            state_path = record_dir / "stage_a06_start.state.json"
            state_path.write_text(
                json.dumps(
                    {
                        "action": "stage.a06.start",
                        "status": "running",
                        "project_dir": str(project_dir),
                        "requirement_name": "需求A",
                        "stage_seq": 5,
                        "runner_id": "runner-old",
                        "source": "runner_start",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._set_context(project_dir=str(project_dir), requirement_name="需求A", action="stage.a06.start")  # noqa: SLF001
            server._workers["new"] = object()  # type: ignore[assignment]  # noqa: SLF001
            server._runner_executions["new"] = SimpleNamespace(  # noqa: SLF001
                runner_id="runner-new",
                action="stage.a06.start",
                stage_seq=6,
                project_dir=str(project_dir),
                requirement_name="需求A",
            )

            reconciled = server._reconcile_persisted_running_runner("stage.a06.start")  # noqa: SLF001
            persisted = json.loads(state_path.read_text(encoding="utf-8"))

        self.assertFalse(reconciled)
        self.assertEqual(persisted["status"], "running")
        self.assertEqual(persisted["runner_id"], "runner-old")

    def test_reconciled_interruption_keeps_cleanup_policy_during_explicit_rerun(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir).resolve()
            record_dir = project_dir / ".tmux_workflow" / "需求A" / "stages"
            record_dir.mkdir(parents=True)
            (record_dir / "stage_a06_start.state.json").write_text(
                json.dumps(
                    {
                        "action": "stage.a06.start",
                        "status": "running",
                        "project_dir": str(project_dir),
                        "requirement_name": "需求A",
                        "stage_seq": 5,
                        "runner_id": "runner-old",
                        "source": "runner_start",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._set_context(project_dir=str(project_dir), requirement_name="需求A", action="stage.a06.start")  # noqa: SLF001
            self.assertTrue(server._reconcile_persisted_running_runner("stage.a06.start"))  # noqa: SLF001
            self.assertEqual(server._current_shutdown_policy().value, "CLEANUP")  # noqa: SLF001
            runner_started = threading.Event()
            release_runner = threading.Event()

            def successful_rerun():
                runner_started.set()
                release_runner.wait(timeout=2.0)
                return 0

            server._run_in_thread("req-rerun", "stage.a06.start", successful_rerun, respond=False)  # noqa: SLF001
            self.assertTrue(runner_started.wait(timeout=2.0))
            policy_during_rerun = server._current_shutdown_policy()  # noqa: SLF001
            release_runner.set()
            for thread in list(server._workers.values()):  # noqa: SLF001
                thread.join(timeout=2.0)
            with patch("T11_tui_backend.cleanup_registered_tmux_workers", return_value=["sess-cleaned"]), patch.object(
                server,
                "_cleanup_visible_tmux_workers",
                return_value=[],
            ), patch.object(
                server,
                "_cleanup_project_runtime_tmux_workers",
                return_value=[],
            ), patch.object(
                server,
                "_cleanup_current_project_tmux_sessions",
                return_value=[],
            ), patch.object(server, "_list_foreign_project_tmux_sessions", return_value=[]):
                cleaned = server.shutdown(cleanup_tmux=True)

        self.assertEqual(policy_during_rerun.value, "CLEANUP")
        self.assertEqual(cleaned, ["sess-cleaned"])

    def test_successful_explicit_rerun_cannot_disable_failure_cleanup_policy(self):
        clear_runtime_shutdown_request()
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                project_dir = Path(tmpdir).resolve()
                runtime_dir = project_dir / TASK_SPLIT_RUNTIME_ROOT_NAME / "需求A" / "analyst"
                runtime_dir.mkdir(parents=True)
                state_path = runtime_dir / "worker.state.json"
                state_path.write_text(
                    json.dumps(
                        {
                            "worker_id": "task-split-analyst",
                            "session_name": "需求分析师-天哭星",
                            "project_dir": str(project_dir),
                            "requirement_name": "需求A",
                            "workflow_action": "stage.a06.start",
                            "stage_runner_id": "runner-old",
                            "agent_state": "READY",
                            "health_status": "alive",
                        },
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
                server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
                server._set_context(project_dir=str(project_dir), requirement_name="需求A", action="stage.a06.start")  # noqa: SLF001
                _failure_path, orphaned_workers, accepted = server._commit_runner_failure(  # noqa: SLF001
                    action="stage.a06.start",
                    stage_seq=1,
                    runner_id="runner-old",
                    error=RuntimeError("ordinary failure"),
                    traceback_text="trace",
                    failure_kind="runner_failure",
                )
                self.assertTrue(accepted)
                self.assertEqual(orphaned_workers, [])
                self.assertEqual(server._current_shutdown_policy().value, "CLEANUP")  # noqa: SLF001

                killed: list[str] = []
                server._tmux_runtime = SimpleNamespace(  # noqa: SLF001
                    session_matches_worker_state=lambda *_args: True,
                    kill_session=lambda session_name, *, missing_ok=True: killed.append(session_name) or session_name,
                )
                observed_policy: list[object] = []
                runner_called = threading.Event()

                def successful_rerun():
                    observed_policy.append(server._current_shutdown_policy())  # noqa: SLF001
                    runner_called.set()
                    return 0

                with patch.object(server, "_schedule_flow_snapshot_update"):
                    server._run_in_thread(  # noqa: SLF001
                        "req-ordinary-rerun",
                        "stage.a06.start",
                        successful_rerun,
                        respond=False,
                    )
                    for thread in list(server._workers.values()):  # noqa: SLF001
                        thread.join(timeout=2.0)

                with patch("T11_tui_backend.cleanup_registered_tmux_workers", return_value=["sess-cleaned"]), patch.object(
                    server,
                    "_cleanup_visible_tmux_workers",
                    return_value=[],
                ), patch.object(
                    server,
                    "_cleanup_project_runtime_tmux_workers",
                    return_value=[],
                ), patch.object(
                    server,
                    "_cleanup_current_project_tmux_sessions",
                    return_value=[],
                ), patch.object(server, "_list_foreign_project_tmux_sessions", return_value=[]):
                    cleaned = server.shutdown(cleanup_tmux=True)

                runtime_exists = runtime_dir.exists()

            self.assertTrue(runner_called.is_set())
            self.assertEqual(killed, [])
            self.assertTrue(runtime_exists)
            self.assertEqual([policy.value for policy in observed_policy], ["CLEANUP"])
            self.assertEqual(cleaned, ["sess-cleaned"])
        finally:
            clear_runtime_shutdown_request()

    def test_new_runner_cleanup_removes_only_matching_orphan_or_dead_runtime(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir).resolve()
            runtime_root = project_dir / TASK_SPLIT_RUNTIME_ROOT_NAME / "需求A"

            def write_worker(name: str, **extra: object) -> Path:
                worker_dir = runtime_root / name
                worker_dir.mkdir(parents=True)
                payload: dict[str, object] = {
                    "worker_id": name,
                    "session_name": f"session-{name}",
                    "project_dir": str(project_dir),
                    "requirement_name": "需求A",
                    "workflow_action": "stage.a06.start",
                    "agent_state": "READY",
                    "health_status": "alive",
                }
                payload.update(extra)
                (worker_dir / "worker.state.json").write_text(
                    json.dumps(payload, ensure_ascii=False),
                    encoding="utf-8",
                )
                return worker_dir

            orphan_dir = write_worker("orphan", turn_state="orphaned", orphaned_stage_action="stage.a06.start")
            dead_dir = write_worker("dead", agent_state="DEAD")
            live_dir = write_worker("live")
            killed: list[str] = []
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._set_context(project_dir=str(project_dir), requirement_name="需求A", action="stage.a06.start")  # noqa: SLF001
            server._tmux_runtime = SimpleNamespace(  # noqa: SLF001
                session_matches_worker_state=lambda *_args: True,
                kill_session=lambda session_name, *, missing_ok=True: killed.append(session_name) or session_name,
            )

            server._cleanup_stage_orphans_before_runner_start("stage.a06.start", runner_id="runner-new")  # noqa: SLF001
            orphan_exists = orphan_dir.exists()
            dead_exists = dead_dir.exists()
            live_exists = live_dir.exists()

        self.assertEqual(sorted(killed), ["session-dead", "session-orphan"])
        self.assertFalse(orphan_exists)
        self.assertFalse(dead_exists)
        self.assertTrue(live_exists)

    def test_new_runner_cleanup_never_kills_reused_foreign_session_name(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir).resolve()
            runtime_dir = project_dir / TASK_SPLIT_RUNTIME_ROOT_NAME / "需求A" / "orphan"
            runtime_dir.mkdir(parents=True)
            (runtime_dir / "worker.state.json").write_text(
                json.dumps(
                    {
                        "worker_id": "task-split-analyst",
                        "session_name": "shared-session",
                        "project_dir": str(project_dir),
                        "requirement_name": "需求A",
                        "workflow_action": "stage.a06.start",
                        "agent_state": "READY",
                        "health_status": "alive",
                        "turn_state": "orphaned",
                        "orphaned_stage_action": "stage.a06.start",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            killed: list[str] = []
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._set_context(project_dir=str(project_dir), requirement_name="需求A", action="stage.a06.start")  # noqa: SLF001
            server._tmux_runtime = SimpleNamespace(  # noqa: SLF001
                session_matches_worker_state=lambda *_args: False,
                kill_session=lambda session_name, *, missing_ok=True: killed.append(session_name) or session_name,
            )

            server._cleanup_stage_orphans_before_runner_start("stage.a06.start", runner_id="runner-new")  # noqa: SLF001

            runtime_exists = runtime_dir.exists()

        self.assertFalse(runtime_exists)
        self.assertEqual(killed, [])

    def test_tmux_cleanup_timeout_preserves_orphan_runtime_and_fails_new_runner(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir).resolve()
            runtime_dir = project_dir / TASK_SPLIT_RUNTIME_ROOT_NAME / "需求A" / "orphan"
            runtime_dir.mkdir(parents=True)
            (runtime_dir / "worker.state.json").write_text(
                json.dumps(
                    {
                        "worker_id": "task-split-analyst",
                        "session_name": "需求分析师-天哭星",
                        "project_dir": str(project_dir),
                        "requirement_name": "需求A",
                        "workflow_action": "stage.a06.start",
                        "agent_state": "READY",
                        "health_status": "alive",
                        "turn_state": "orphaned",
                        "orphaned_stage_action": "stage.a06.start",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._set_context(project_dir=str(project_dir), requirement_name="需求A", action="stage.a06.start")  # noqa: SLF001

            def fail_kill(_session_name, *, missing_ok=True):  # noqa: ANN001, ARG001
                raise TimeoutError("tmux mutation timed out")

            server._tmux_runtime = SimpleNamespace(  # noqa: SLF001
                session_matches_worker_state=lambda *_args: True,
                kill_session=fail_kill,
            )
            runner_called = threading.Event()
            with patch.object(server, "_schedule_flow_snapshot_update"):
                server._run_in_thread(  # noqa: SLF001
                    "req-cleanup-timeout",
                    "stage.a06.start",
                    lambda: runner_called.set(),
                    respond=False,
                )
                for thread in list(server._workers.values()):  # noqa: SLF001
                    thread.join(timeout=2.0)
            failure_path = project_dir / ".tmux_workflow" / "需求A" / "stages" / "stage_a06_start.failure.json"
            failure = json.loads(failure_path.read_text(encoding="utf-8"))
            stage_state = json.loads(
                failure_path.with_name("stage_a06_start.state.json").read_text(encoding="utf-8")
            )
            runtime_exists = runtime_dir.exists()

        self.assertFalse(runner_called.is_set())
        self.assertTrue(runtime_exists)
        self.assertIn("保留运行目录并终止新 runner", failure["error"])
        self.assertEqual(failure["failure_kind"], "runner_failure")
        self.assertTrue(failure["runner_id"].startswith("runner_"))
        self.assertEqual(stage_state["runner_id"], failure["runner_id"])
        self.assertEqual(stage_state["stage_seq"], failure["stage_seq"])
        self.assertEqual(stage_state["source"], "runner_failure")
        self.assertEqual(server._current_shutdown_policy().value, "CLEANUP")  # noqa: SLF001

    def test_worker_snapshot_exposes_tmux_control_diagnostics(self):
        from tmux_core.bridge.backend import _read_worker_state_snapshot

        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "worker.state.json"
            state_path.write_text(
                json.dumps(
                    {
                        "worker_id": "worker-1",
                        "tmux_control_status": "unavailable",
                        "tmux_control_error": "list-panes timeout",
                        "tmux_control_unavailable_since": "2026-07-14T10:00:00+08:00",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            snapshot = _read_worker_state_snapshot(state_path)

        self.assertEqual(snapshot["tmux_control_status"], "unavailable")
        self.assertEqual(snapshot["tmux_control_error"], "list-panes timeout")
        self.assertEqual(snapshot["tmux_control_unavailable_since"], "2026-07-14T10:00:00+08:00")

    def test_worker_snapshot_flattens_optional_ponytail_state_without_polluting_legacy_workers(self):
        from tmux_core.bridge.backend import _read_worker_state_snapshot

        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "worker.state.json"
            state_path.write_text(
                json.dumps(
                    {
                        "worker_id": "worker-1",
                        "config": {"ponytail_mode": "full"},
                        "ponytail_policy": {
                            "bundle_version": "4.8.4",
                            "delivery": "runtime_prompt",
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            ponytail_snapshot = _read_worker_state_snapshot(state_path)
            state_path.write_text(json.dumps({"worker_id": "legacy-worker"}), encoding="utf-8")
            legacy_snapshot = _read_worker_state_snapshot(state_path)

        self.assertEqual(ponytail_snapshot["ponytail_mode"], "full")
        self.assertEqual(ponytail_snapshot["ponytail_bundle_version"], "4.8.4")
        self.assertEqual(ponytail_snapshot["ponytail_delivery"], "runtime_prompt")
        self.assertNotIn("ponytail_mode", legacy_snapshot)
        self.assertNotIn("ponytail_bundle_version", legacy_snapshot)
        self.assertNotIn("ponytail_delivery", legacy_snapshot)

    def test_worker_snapshot_flattens_optional_grill_state_without_polluting_legacy_workers(self):
        from tmux_core.bridge.backend import _read_worker_state_snapshot

        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "worker.state.json"
            state_path.write_text(
                json.dumps(
                    {
                        "worker_id": "requirements-analyst",
                        "config": {"requirements_mode": "grill-with-docs"},
                        "grill_policy": {
                            "bundle_commit": "ed37663cc5fbef691ddfecd080dff42f7e7e350d",
                            "delivery": "runtime_prompt",
                            "question_seq": 4,
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            grill_snapshot = _read_worker_state_snapshot(state_path)
            state_path.write_text(json.dumps({"worker_id": "legacy-worker"}), encoding="utf-8")
            legacy_snapshot = _read_worker_state_snapshot(state_path)

        self.assertEqual(grill_snapshot["requirements_mode"], "grill-with-docs")
        self.assertEqual(grill_snapshot["grill_bundle_commit"], "ed37663cc5fbef691ddfecd080dff42f7e7e350d")
        self.assertEqual(grill_snapshot["grill_delivery"], "runtime_prompt")
        self.assertEqual(grill_snapshot["grill_question_seq"], 4)
        self.assertNotIn("requirements_mode", legacy_snapshot)
        self.assertNotIn("grill_bundle_commit", legacy_snapshot)
        self.assertNotIn("grill_delivery", legacy_snapshot)
        self.assertNotIn("grill_question_seq", legacy_snapshot)

    def test_ready_agent_health_does_not_erase_running_turn_contract(self):
        from tmux_core.bridge.backend import _read_worker_state_snapshot

        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "worker.state.json"
            state_path.write_text(
                json.dumps(
                    {
                        "worker_id": "worker-1",
                        "session_name": "需求分析师-天哭星",
                        "status": "ready",
                        "result_status": "running",
                        "current_task_runtime_status": "running",
                        "agent_state": "READY",
                        "agent_started": True,
                        "health_status": "alive",
                        "note": "agent_ready",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            snapshot = _read_worker_state_snapshot(
                state_path,
                session_exists_resolver=lambda _session_name: True,
            )

        self.assertEqual(snapshot["agent_state"], "READY")
        self.assertEqual(snapshot["status"], "running")
        self.assertEqual(snapshot["current_task_runtime_status"], "running")

    def test_completed_turn_does_not_override_observed_busy_agent_state(self):
        from tmux_core.bridge.backend import _read_worker_state_snapshot

        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "worker.state.json"
            state_path.write_text(
                json.dumps(
                    {
                        "worker_id": "worker-1",
                        "session_name": "架构师-地强星",
                        "status": "succeeded",
                        "result_status": "succeeded",
                        "current_task_runtime_status": "done",
                        "turn_state": "succeeded",
                        "agent_state": "BUSY",
                        "agent_alive": True,
                        "agent_started": True,
                        "health_status": "alive",
                        "state_revision": 17,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            snapshot = _read_worker_state_snapshot(
                state_path,
                trust_persisted_session=True,
            )

        self.assertEqual(snapshot["agent_state"], "BUSY")
        self.assertEqual(snapshot["turn_state"], "succeeded")
        self.assertEqual(snapshot["current_task_runtime_status"], "done")
        self.assertEqual(snapshot["state_revision"], 17)
        self.assertTrue(snapshot["session_exists"])

    def test_persisted_missing_session_overrides_stale_alive_busy_snapshot(self):
        from tmux_core.bridge.backend import _read_worker_state_snapshot

        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "worker.state.json"
            state_path.write_text(
                json.dumps(
                    {
                        "worker_id": "development-review-审核员",
                        "session_name": "审核员-天异星",
                        "status": "running",
                        "result_status": "running",
                        "current_task_runtime_status": "running",
                        "turn_state": "failed",
                        "agent_state": "BUSY",
                        "agent_alive": True,
                        "agent_started": True,
                        "session_exists": False,
                        "health_status": "alive",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            snapshot = _read_worker_state_snapshot(
                state_path,
                trust_persisted_session=True,
            )

        self.assertFalse(snapshot["session_exists"])
        self.assertEqual(snapshot["agent_state"], "DEAD")
        self.assertEqual(snapshot["health_status"], "dead")
        self.assertEqual(snapshot["current_task_runtime_status"], "running")

    def test_runtime_stage_change_marks_previous_forward_stage_completed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            record_dir = project_dir / ".tmux_workflow" / "需求A" / "stages"
            writer = io.StringIO()
            server = TuiBackendServer(reader=io.StringIO(), writer=writer)
            server._set_context(project_dir=str(project_dir), requirement_name="需求A", action="workflow.a00.start")  # noqa: SLF001

            server._handle_runtime_stage_change("stage.a03.start")  # noqa: SLF001
            server._handle_runtime_stage_change("stage.a04.start")  # noqa: SLF001

            previous_payload = json.loads((record_dir / "stage_a03_start.state.json").read_text(encoding="utf-8"))
            current_payload = json.loads((record_dir / "stage_a04_start.state.json").read_text(encoding="utf-8"))

        self.assertEqual(previous_payload["action"], "stage.a03.start")
        self.assertEqual(previous_payload["status"], "completed")
        self.assertEqual(previous_payload["source"], "runner_complete")
        self.assertIn("stage.a04.start", previous_payload["message"])
        self.assertEqual(current_payload["action"], "stage.a04.start")
        self.assertEqual(current_payload["status"], "running")

    def test_a00_root_runner_is_alive_for_current_child_stage_runtime_inference(self):
        server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
        runner_started = threading.Event()
        release_runner = threading.Event()

        def root_runner():
            runner_started.set()
            release_runner.wait(timeout=2.0)

        thread = threading.Thread(
            target=root_runner,
            name="tui-backend-workflow.a00.start-root",
            daemon=True,
        )
        thread.start()
        self.assertTrue(runner_started.wait(timeout=1.0))
        server._workers["root"] = thread  # noqa: SLF001
        server._runner_executions["root"] = SimpleNamespace(  # noqa: SLF001
            runner_id="runner-root",
            action="workflow.a00.start",
            stage_seq=1,
            project_dir="/tmp/project",
            requirement_name="需求A",
            terminal_source="",
        )
        server._display_action = "stage.a06.start"  # noqa: SLF001
        server._display_status = "running"  # noqa: SLF001
        server._display_stage_seq = 6  # noqa: SLF001
        server._display_runner_id = "runner-root"  # noqa: SLF001

        try:
            action, status, stage_seq = server._derive_display_stage_state(  # noqa: SLF001
                preferred_status="running",
                preferred_action="stage.a06.start",
                preferred_stage_seq=6,
                source="runtime_inference",
                runtime_status="failed",
                pending_prompt=False,
                pending_hitl=False,
            )
        finally:
            release_runner.set()
            thread.join(timeout=1.0)

        self.assertEqual(action, "stage.a06.start")
        self.assertEqual(status, "running")
        self.assertEqual(stage_seq, 6)

    def test_runtime_stage_change_marks_previous_backward_stage_superseded(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            record_dir = project_dir / ".tmux_workflow" / "需求A" / "stages"
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._set_context(project_dir=str(project_dir), requirement_name="需求A", action="workflow.a00.start")  # noqa: SLF001

            server._handle_runtime_stage_change("stage.a05.start")  # noqa: SLF001
            server._handle_runtime_stage_change("stage.a04.start")  # noqa: SLF001

            previous_payload = json.loads((record_dir / "stage_a05_start.state.json").read_text(encoding="utf-8"))
            current_payload = json.loads((record_dir / "stage_a04_start.state.json").read_text(encoding="utf-8"))

        self.assertEqual(previous_payload["action"], "stage.a05.start")
        self.assertEqual(previous_payload["status"], "superseded")
        self.assertEqual(previous_payload["source"], "runner_complete")
        self.assertIn("stage.a04.start", previous_payload["message"])
        self.assertEqual(current_payload["action"], "stage.a04.start")
        self.assertEqual(current_payload["status"], "running")

    def test_nonzero_routing_error_includes_failed_target_details(self):
        server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
        result = SimpleNamespace(
            exit_code=1,
            batch_result=SimpleNamespace(
                results=[
                    SimpleNamespace(work_dir="/tmp/project", status="passed"),
                    SimpleNamespace(
                        work_dir="/tmp/project/core",
                        status="failed",
                        failure_reason="create_command_failed: prompt timeout",
                    ),
                ]
            ),
        )

        with self.assertRaises(RuntimeError) as context:
            server._raise_for_nonzero_exit_code(action="stage.a01.start", stage_seq=1, result=result)  # noqa: SLF001

        message = str(context.exception)
        self.assertIn("stage.a01.start exited with non-zero code: 1", message)
        self.assertIn("failed routing targets", message)
        self.assertIn("/tmp/project/core", message)
        self.assertIn("create_command_failed: prompt timeout", message)

    def test_workflow_status_downgrades_to_awaiting_input_when_runner_leaves_file_driven_hitl(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            requirement_name = "贪吃蛇"
            _, _, ask_human_path, _ = build_requirements_clarification_paths(project_dir, requirement_name)
            writer = io.StringIO()
            server = TuiBackendServer(reader=io.StringIO(), writer=writer)

            def fake_runner(argv):  # noqa: ANN001
                server._handle_runtime_stage_change("stage.a04.start")  # noqa: SLF001
                ask_human_path.parent.mkdir(parents=True, exist_ok=True)
                ask_human_path.write_text("请补充碰撞规则。", encoding="utf-8")
                return SimpleNamespace(project_dir=str(project_dir), requirement_name=requirement_name)

            with patch("T11_tui_backend.a00_main", side_effect=fake_runner):
                server.handle_request(
                    build_request(
                        "workflow.a00.start",
                        {"argv": ["--project-dir", str(project_dir), "--requirement-name", requirement_name]},
                        message_id="req_hitl",
                    )
                )
                for worker in list(server._workers.values()):  # noqa: SLF001
                    worker.join(timeout=2.0)
                server._flush_dirty_snapshots()  # noqa: SLF001

        messages = [json.loads(line) for line in writer.getvalue().splitlines() if line.strip()]
        stage_events = [item for item in messages if item.get("kind") == "event" and item.get("type") == "stage.changed"]
        self.assertTrue(stage_events)
        self.assertEqual(stage_events[-1]["payload"]["action"], "stage.a04.start")
        self.assertEqual(stage_events[-1]["payload"]["status"], "awaiting-input")
        app_events = [item for item in messages if item.get("kind") == "event" and item.get("type") == "snapshot.app"]
        self.assertTrue(app_events)
        self.assertTrue(app_events[-1]["payload"]["pending_hitl"])

    def test_failed_status_is_not_masked_by_file_detected_hitl(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            _, _, ask_human_path, _ = build_requirements_clarification_paths(project_dir, "贪吃蛇")
            ask_human_path.parent.mkdir(parents=True, exist_ok=True)
            ask_human_path.write_text("请补充碰撞规则。", encoding="utf-8")
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._set_context(project_dir=str(project_dir), requirement_name="贪吃蛇", action="stage.a04.start")  # noqa: SLF001

            action, status, _stage_seq = server._derive_display_stage_state(  # noqa: SLF001
                preferred_status="failed",
                preferred_action="stage.a04.start",
            )

        self.assertEqual(action, "stage.a04.start")
        self.assertEqual(status, "failed")

    def test_control_open_returns_snapshot_for_existing_session(self):
        writer = io.StringIO()
        server = TuiBackendServer(reader=io.StringIO(), writer=writer)
        server._controls["run_demo"] = ControlSessionState(control_id="run_demo", center=_FakeCenter())  # noqa: SLF001
        server.handle_request(build_request("control.b01.open", {"control_id": "run_demo"}, message_id="req_3"))
        server._flush_dirty_snapshots()  # noqa: SLF001
        messages = [json.loads(line) for line in writer.getvalue().splitlines() if line.strip()]
        payload = messages[0]["payload"]
        self.assertTrue(payload["supported"])
        self.assertEqual(payload["control_id"], "run_demo")
        self.assertEqual(payload["status_text"], "status text")
        self.assertEqual(payload["workers"][0]["session_name"], "sess-1")
        event_types = [item.get("type") for item in messages[1:] if item.get("kind") == "event"]
        self.assertEqual(event_types, ["snapshot.control"])

    def test_control_open_skip_does_not_prepare_unused_agent_config(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            _write_valid_routing_layer(project_dir)
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            with patch(
                "T11_tui_backend.prepare_agent_run_config",
                side_effect=AssertionError("skip must not resolve an unused agent config"),
            ):
                snapshot = server._open_control_session(  # noqa: SLF001
                    {
                        "argv": [
                            "--project-dir",
                            str(project_dir),
                            "--run-init",
                            "no",
                            "--yes",
                        ]
                    }
                )

        self.assertTrue(snapshot["done"])
        self.assertEqual(snapshot["workers"], [])
        self.assertIn("跳过路由初始化", snapshot["status_text"])

    def test_worker_attach_returns_tmux_attach_command(self):
        writer = io.StringIO()
        server = TuiBackendServer(reader=io.StringIO(), writer=writer)
        server._controls["run_demo"] = ControlSessionState(control_id="run_demo", center=_FakeCenter())  # noqa: SLF001
        server.handle_request(
            build_request(
                "worker.attach",
                {"control_id": "run_demo", "argument": "1"},
                message_id="req_4",
            )
        )
        messages = [json.loads(line) for line in writer.getvalue().splitlines() if line.strip()]
        payload = messages[0]["payload"]
        self.assertEqual(payload["attach_command"], ["tmux", "attach", "-t", "sess-1"])
        self.assertEqual(payload["work_dir"], "/tmp/demo")

    def test_run_resume_replaces_previous_control_session(self):
        writer = io.StringIO()
        server = TuiBackendServer(reader=io.StringIO(), writer=writer)
        old_center = _FakeCenter(run_id="run_old", done=True)
        server._controls["run_old"] = ControlSessionState(control_id="run_old", center=old_center)  # noqa: SLF001
        with patch("T11_tui_backend.AgentInitControlCenter.from_existing_run", return_value=_FakeCenter(run_id="run_new")):
            server.handle_request(
                build_request(
                    "run.resume",
                    {"control_id": "run_old", "run_id": "run_new"},
                    message_id="req_5",
                )
            )
        messages = [json.loads(line) for line in writer.getvalue().splitlines() if line.strip()]
        payload = messages[0]["payload"]
        self.assertTrue(old_center.closed)
        self.assertEqual(payload["control_id"], "run_new")

    def test_run_resume_cleans_failed_run_sessions_before_switch(self):
        writer = io.StringIO()
        server = TuiBackendServer(reader=io.StringIO(), writer=writer)
        failed_center = _FakeCenter(run_id="run_failed", done=True)
        failed_batch = type(
            "Batch",
            (),
            {
                "run_id": "run_failed",
                "runtime_dir": "/tmp/runtime",
                "config": {"vendor": "codex", "model": "gpt-5.4", "reasoning_effort": "high", "proxy_url": ""},
                "results": [type("Item", (), {"status": "failed", "work_dir": "/tmp/demo", "failure_reason": "", "last_audit_summary": ""})()],
            },
        )()
        server._controls["run_failed"] = ControlSessionState(
            control_id="run_failed",
            center=failed_center,
            final_result=failed_batch,
        )  # noqa: SLF001
        with patch("T11_tui_backend.AgentInitControlCenter.from_existing_run", return_value=_FakeCenter(run_id="run_new")):
            server.handle_request(
                build_request(
                    "run.resume",
                    {"control_id": "run_failed", "run_id": "run_new"},
                    message_id="req_7",
                )
            )
        self.assertTrue(failed_center.cleaned)

    def test_worker_retry_clears_completed_snapshot_state(self):
        writer = io.StringIO()
        server = TuiBackendServer(reader=io.StringIO(), writer=writer)
        session = ControlSessionState(
            control_id="run_demo",
            center=_FakeCenter(done=False),
            final_result=object(),
            transition_text="old transition",
        )
        server._controls["run_demo"] = session  # noqa: SLF001
        server.handle_request(
            build_request(
                "worker.retry",
                {"control_id": "run_demo", "argument": "1"},
                message_id="req_6",
            )
        )
        messages = [json.loads(line) for line in writer.getvalue().splitlines() if line.strip()]
        payload = messages[0]["payload"]
        self.assertFalse(payload["done"])
        self.assertEqual(payload["transition_text"], "")

    def test_protocol_log_sink_converts_stdout_noise_into_log_event(self):
        writer = io.StringIO()
        server = TuiBackendServer(reader=io.StringIO(), writer=writer)
        with redirect_stdout(server.protocol_log_sink()):
            print("警告：文件不存在 -> /tmp/demo")
        messages = [json.loads(line) for line in writer.getvalue().splitlines() if line.strip()]
        self.assertTrue(any(item.get("kind") == "event" and item.get("type") == "log.append" for item in messages))
        payloads = [item.get("payload", {}) for item in messages if item.get("type") == "log.append"]
        self.assertTrue(any("警告：文件不存在" in str(payload.get("text", "")) for payload in payloads))

    def test_shutdown_closes_controls_and_cleans_tmux_when_requested(self):
        writer = io.StringIO()
        server = TuiBackendServer(reader=io.StringIO(), writer=writer)
        center = _FakeCenter(run_id="run_demo", done=False)
        server._controls["run_demo"] = ControlSessionState(control_id="run_demo", center=center)  # noqa: SLF001
        with patch("T11_tui_backend.cleanup_registered_tmux_workers", return_value=["sess-1"]) as cleanup:
            cleaned = server.shutdown(cleanup_tmux=True)
        self.assertEqual(cleaned, ["sess-1"])
        self.assertTrue(center.closed)
        cleanup.assert_called_once_with(reason="tui_backend_shutdown")

    def test_shutdown_requests_runtime_shutdown_and_new_server_clears_it(self):
        clear_runtime_shutdown_request()
        try:
            writer = io.StringIO()
            server = TuiBackendServer(reader=io.StringIO(), writer=writer)
            self.assertFalse(runtime_shutdown_requested())
            with patch("T11_tui_backend.cleanup_registered_tmux_workers", return_value=[]), patch.object(
                server, "_cleanup_visible_tmux_workers", return_value=[]
            ), patch.object(server, "_cleanup_project_runtime_tmux_workers", return_value=[]), patch.object(
                server, "_cleanup_current_project_tmux_sessions", return_value=[]
            ), patch.object(server, "_list_foreign_project_tmux_sessions", return_value=[]):
                server.shutdown(cleanup_tmux=False)
            self.assertTrue(runtime_shutdown_requested())

            TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            self.assertFalse(runtime_shutdown_requested())
        finally:
            clear_runtime_shutdown_request()

    def test_shutdown_cleanup_cannot_be_disabled_by_legacy_false_flag(self):
        writer = io.StringIO()
        server = TuiBackendServer(reader=io.StringIO(), writer=writer)
        with patch("T11_tui_backend.cleanup_registered_tmux_workers", return_value=["sess-now"]) as cleanup, patch.object(
            server,
            "_cleanup_visible_tmux_workers",
            return_value=[],
        ), patch.object(
            server,
            "_cleanup_project_runtime_tmux_workers",
            return_value=[],
        ), patch.object(server, "_cleanup_current_project_tmux_sessions", return_value=[]), patch.object(
            server, "_list_foreign_project_tmux_sessions", return_value=[]
        ):
            first_cleaned = server.shutdown(cleanup_tmux=False)
            second_cleaned = server.shutdown(cleanup_tmux=True)
        self.assertEqual(first_cleaned, ["sess-now"])
        self.assertEqual(second_cleaned, [])
        cleanup.assert_called_once_with(reason="tui_backend_shutdown")

    def test_shutdown_continues_other_tmux_cleanup_steps_after_probe_error(self):
        server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
        with patch(
            "T11_tui_backend.cleanup_registered_tmux_workers",
            side_effect=RuntimeError("registry probe failed"),
        ), patch.object(
            server, "_cleanup_visible_tmux_workers", return_value=["visible-session"]
        ), patch.object(
            server,
            "_cleanup_project_runtime_tmux_workers",
            side_effect=RuntimeError("runtime probe failed"),
        ), patch.object(
            server, "_cleanup_current_project_tmux_sessions", return_value=["identity-session"]
        ), patch.object(
            server,
            "_list_foreign_project_tmux_sessions",
            side_effect=RuntimeError("foreign audit failed"),
        ):
            cleaned = server.shutdown(cleanup_tmux=True)

        self.assertEqual(cleaned, ["identity-session", "visible-session"])

    def test_legacy_preserve_orphans_request_is_normalized_to_cleanup(self):
        writer = io.StringIO()
        server = TuiBackendServer(reader=io.StringIO(), writer=writer)
        result = server.dispatch_action(  # noqa: SLF001
            "app.shutdown",
            {"policy": "preserve_orphans", "reason": "runner_failure"},
            request_id="req-shutdown",
        )
        with patch("T11_tui_backend.cleanup_registered_tmux_workers", return_value=["sess-cleaned"]) as cleanup, patch.object(
            server, "_cleanup_visible_tmux_workers", return_value=[]
        ), patch.object(server, "_cleanup_project_runtime_tmux_workers", return_value=[]), patch.object(
            server, "_cleanup_current_project_tmux_sessions", return_value=[]
        ), patch.object(server, "_list_foreign_project_tmux_sessions", return_value=[]):
            cleaned = server.shutdown(cleanup_tmux=True)

        self.assertEqual(result["policy"], "cleanup")
        self.assertEqual(result["reason"], "runner_failure")
        self.assertEqual(cleaned, ["sess-cleaned"])
        cleanup.assert_called_once_with(reason="tui_backend_shutdown")
        messages = [json.loads(line) for line in writer.getvalue().splitlines() if line.strip()]
        responses = [item for item in messages if item.get("kind") == "response" and item.get("id") == "req-shutdown"]
        self.assertTrue(responses)
        self.assertTrue(responses[-1]["ok"])

    def test_legacy_preserve_request_cannot_downgrade_runner_failure_cleanup(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir).resolve()
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._set_context(project_dir=str(project_dir), requirement_name="需求A", action="stage.a06.start")  # noqa: SLF001
            _failure_path, _orphaned_workers, accepted = server._commit_runner_failure(  # noqa: SLF001
                action="stage.a06.start",
                stage_seq=1,
                runner_id="runner-old",
                error=RuntimeError("ordinary failure"),
                traceback_text="trace",
                failure_kind="runner_failure",
            )
            self.assertTrue(accepted)
            server.dispatch_action(  # noqa: SLF001
                "app.shutdown",
                {"policy": "preserve_orphans", "reason": "runner_failure"},
                respond=False,
            )
            server._emit_display_stage_state(  # noqa: SLF001
                preferred_status="running",
                preferred_action="stage.a06.start",
                preferred_stage_seq=2,
                preferred_runner_id="runner-new",
                source="runner_start",
                force=True,
            )

            with patch("T11_tui_backend.cleanup_registered_tmux_workers", return_value=["sess-cleaned"]), patch.object(
                server, "_cleanup_visible_tmux_workers", return_value=[]
            ), patch.object(server, "_cleanup_project_runtime_tmux_workers", return_value=[]), patch.object(
                server, "_cleanup_current_project_tmux_sessions", return_value=[]
            ), patch.object(server, "_list_foreign_project_tmux_sessions", return_value=[]):
                cleaned = server.shutdown(cleanup_tmux=True)

        self.assertEqual(server._current_shutdown_policy().value, "CLEANUP")  # noqa: SLF001
        self.assertEqual(cleaned, ["sess-cleaned"])

    def test_shutdown_waits_for_runner_threads_before_tmux_cleanup(self):
        clear_runtime_shutdown_request()
        try:
            writer = io.StringIO()
            server = TuiBackendServer(reader=io.StringIO(), writer=writer)
            runner_stopped = threading.Event()
            cleanup_observed_runner_stopped: list[bool] = []

            def runner():
                while not runtime_shutdown_requested():
                    time.sleep(0.01)
                runner_stopped.set()

            thread = threading.Thread(target=runner, name="tui-backend-unit-runner")
            thread.start()
            server._workers["unit"] = thread  # noqa: SLF001

            def fake_cleanup(*, reason):  # noqa: ANN001
                cleanup_observed_runner_stopped.append(runner_stopped.is_set())
                return []

            with patch("T11_tui_backend.cleanup_registered_tmux_workers", side_effect=fake_cleanup), patch.object(
                server,
                "_cleanup_visible_tmux_workers",
                return_value=[],
            ), patch.object(
                server,
                "_cleanup_project_runtime_tmux_workers",
                return_value=[],
            ):
                server.shutdown(cleanup_tmux=True)

            thread.join(timeout=1.0)
            self.assertTrue(runner_stopped.is_set())
            self.assertEqual(cleanup_observed_runner_stopped, [True])
        finally:
            clear_runtime_shutdown_request()

    def test_run_in_thread_creates_daemon_runner_thread(self):
        writer = io.StringIO()
        server = TuiBackendServer(reader=io.StringIO(), writer=writer)
        runner_started = threading.Event()
        release_runner = threading.Event()
        observed_runner_ids: list[str] = []

        def runner():
            observed_runner_ids.append(get_current_stage_runner_id())
            runner_started.set()
            release_runner.wait(timeout=1.0)
            return 0

        server._run_in_thread("req_daemon", "workflow.a00.start", runner, respond=False)  # noqa: SLF001
        worker_threads = list(server._workers.values())  # noqa: SLF001
        self.assertEqual(len(worker_threads), 1)
        thread = worker_threads[0]
        self.assertTrue(runner_started.wait(timeout=1.0))
        self.assertTrue(thread.daemon)
        self.assertEqual(len(observed_runner_ids), 1)
        self.assertTrue(observed_runner_ids[0].startswith("runner_"))
        release_runner.set()
        thread.join(timeout=1.0)

    def test_shutdown_cleans_visible_unregistered_tmux_workers_when_requested(self):
        writer = io.StringIO()
        server = TuiBackendServer(reader=io.StringIO(), writer=writer)
        server._set_context(project_dir="/tmp/project", requirement_name="需求A", action="stage.a07.start")  # noqa: SLF001
        killed_sessions: list[tuple[str, bool]] = []
        server._tmux_runtime = SimpleNamespace(  # noqa: SLF001
            kill_session=lambda session_name, *, missing_ok=True: killed_sessions.append((session_name, missing_ok)) or session_name,
        )
        visible_worker = {
            "worker_id": "development-review-测试工程师",
            "session_name": "测试工程师-天寿星",
            "project_dir": "/tmp/project",
            "requirement_name": "需求A",
            "workflow_action": "stage.a07.start",
            "status": "failed",
            "agent_state": "READY",
            "health_status": "alive",
            "session_exists": True,
        }

        def fake_current_stage_workers(action):  # noqa: ANN001
            return [visible_worker] if action == "stage.a07.start" else []

        with patch("T11_tui_backend.cleanup_registered_tmux_workers", return_value=["registered-sess"]), patch.object(
            server,
            "_current_stage_workers",
            side_effect=fake_current_stage_workers,
        ):
            cleaned = server.shutdown(cleanup_tmux=True)

        self.assertEqual(killed_sessions, [("测试工程师-天寿星", True)])
        self.assertEqual(cleaned, ["registered-sess", "测试工程师-天寿星"])

    def test_shutdown_cleans_current_project_runtime_state_workers_regardless_status(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            project_dir = Path(tmp_dir) / "project"
            other_project_dir = Path(tmp_dir) / "other-project"
            project_dir.mkdir()
            other_project_dir.mkdir()
            current_worker_root = project_dir / DEVELOPMENT_RUNTIME_ROOT_NAME / "M1-T1" / "dev-worker"
            current_worker_root.mkdir(parents=True)
            (current_worker_root / "worker.state.json").write_text(
                json.dumps(
                    {
                        "session_name": "开发工程师-地雄星",
                        "project_dir": str(project_dir),
                        "work_dir": str(project_dir / "src"),
                        "status": "succeeded",
                        "result_status": "succeeded",
                        "agent_state": "READY",
                        "health_status": "alive",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            routing_worker_root = project_dir / ROUTING_RUNTIME_ROOT_NAME / "run_1" / "router"
            routing_worker_root.mkdir(parents=True)
            (routing_worker_root / "worker.state.json").write_text(
                json.dumps(
                    {
                        "session_name": "路由器-天异星",
                        "project_dir": str(project_dir),
                        "work_dir": str(project_dir),
                        "status": "completed",
                        "agent_state": "READY",
                        "health_status": "alive",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            foreign_worker_root = project_dir / DEVELOPMENT_RUNTIME_ROOT_NAME / "foreign"
            foreign_worker_root.mkdir(parents=True)
            (foreign_worker_root / "worker.state.json").write_text(
                json.dumps(
                    {
                        "session_name": "其他项目-不应清理",
                        "project_dir": str(other_project_dir),
                        "work_dir": str(other_project_dir / "src"),
                        "status": "succeeded",
                        "agent_state": "READY",
                        "health_status": "alive",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            writer = io.StringIO()
            server = TuiBackendServer(reader=io.StringIO(), writer=writer)
            server._set_context(project_dir=str(project_dir), requirement_name="需求A", action="stage.a07.start")  # noqa: SLF001
            killed_sessions: list[tuple[str, bool]] = []
            live_sessions = {"开发工程师-地雄星", "路由器-天异星", "其他项目-不应清理"}
            server._tmux_runtime = SimpleNamespace(  # noqa: SLF001
                session_exists=lambda session_name: session_name in live_sessions,
                kill_session=lambda session_name, *, missing_ok=True: killed_sessions.append((session_name, missing_ok)) or session_name,
            )

            with patch("T11_tui_backend.cleanup_registered_tmux_workers", return_value=[]), patch.object(
                server,
                "_cleanup_visible_tmux_workers",
                return_value=[],
            ):
                cleaned = server.shutdown(cleanup_tmux=True)

        self.assertEqual(killed_sessions, [("路由器-天异星", True), ("开发工程师-地雄星", True)])
        self.assertEqual(cleaned, ["开发工程师-地雄星", "路由器-天异星"])

    def test_shutdown_cleans_current_project_tmux_identity_sessions_without_runtime_state(self):
        writer = io.StringIO()
        server = TuiBackendServer(reader=io.StringIO(), writer=writer)
        server._set_context(project_dir="/tmp/drl-pm", requirement_name="强化学习资产配置", action="stage.a07.start")  # noqa: SLF001

        options = {
            ("审核员-翼火蛇", "@tmux_runtime_dir"): "/tmp/drl-pm/.development_runtime/强化学习资产配置/development-review-1",
            ("审核员-翼火蛇", "@tmux_work_dir"): "/tmp/drl-pm",
            ("审核员-翼火蛇", "@tmux_requirement_name"): "强化学习资产配置",
            ("审核员-翼火蛇", "@tmux_workflow_action"): "stage.a07.start",
            ("审核员-翼火蛇", "@tmux_worker_id"): "development-review-审核员",
            ("开发工程师-地雄星", "@tmux_runtime_dir"): "/tmp/drl-pm/.development_runtime/强化学习资产配置/development-developer-1",
            ("开发工程师-地雄星", "@tmux_work_dir"): "/tmp/drl-pm",
            ("开发工程师-地雄星", "@tmux_requirement_name"): "强化学习资产配置",
            ("开发工程师-地雄星", "@tmux_workflow_action"): "stage.a07.start",
            ("开发工程师-地雄星", "@tmux_worker_id"): "development-developer",
            ("架构师-亢金龙", "@tmux_runtime_dir"): "/tmp/drl-pm/.development_runtime/强化学习资产配置/development-review-2",
            ("架构师-亢金龙", "@tmux_work_dir"): "/tmp/drl-pm",
            ("架构师-亢金龙", "@tmux_requirement_name"): "强化学习资产配置",
            ("架构师-亢金龙", "@tmux_workflow_action"): "stage.a07.start",
            ("架构师-亢金龙", "@tmux_worker_id"): "development-review-架构师",
            ("其他项目-不应清理", "@tmux_runtime_dir"): "/tmp/other-project/.development_runtime/需求B/development-review-9",
            ("其他项目-不应清理", "@tmux_work_dir"): "/tmp/other-project",
            ("其他项目-不应清理", "@tmux_requirement_name"): "需求B",
            ("其他项目-不应清理", "@tmux_workflow_action"): "stage.a07.start",
            ("其他项目-不应清理", "@tmux_worker_id"): "development-review-其他",
        }
        killed_sessions: list[tuple[str, bool]] = []
        server._tmux_runtime = SimpleNamespace(  # noqa: SLF001
            list_sessions=lambda: ["审核员-翼火蛇", "开发工程师-地雄星", "架构师-亢金龙", "其他项目-不应清理"],
            backend=SimpleNamespace(
                show_option=lambda target, option_name: options.get((target, option_name), ""),
            ),
            kill_session=lambda session_name, *, missing_ok=True: killed_sessions.append((session_name, missing_ok)) or session_name,
        )

        with patch("T11_tui_backend.cleanup_registered_tmux_workers", return_value=[]), patch.object(
            server,
            "_cleanup_visible_tmux_workers",
            return_value=[],
        ), patch.object(
            server,
            "_cleanup_project_runtime_tmux_workers",
            return_value=[],
        ):
            cleaned = server.shutdown(cleanup_tmux=True)

        self.assertEqual(
            killed_sessions,
            [("审核员-翼火蛇", True), ("开发工程师-地雄星", True), ("架构师-亢金龙", True)],
        )
        self.assertEqual(cleaned, ["审核员-翼火蛇", "开发工程师-地雄星", "架构师-亢金龙"])

    def test_shutdown_reports_foreign_project_tmux_sessions(self):
        writer = io.StringIO()
        server = TuiBackendServer(reader=io.StringIO(), writer=writer)
        server._set_context(project_dir="/tmp/canopy-api-v3", requirement_name="需求A", action="stage.a07.start")  # noqa: SLF001

        options = {
            ("开发工程师-地雄星", "@tmux_runtime_dir"): "/tmp/canopy-api-v3/.development_runtime/需求A/development-developer-1",
            ("开发工程师-地雄星", "@tmux_work_dir"): "/tmp/canopy-api-v3",
            ("开发工程师-地雄星", "@tmux_requirement_name"): "需求A",
            ("开发工程师-地雄星", "@tmux_workflow_action"): "stage.a07.start",
            ("开发工程师-地雄星", "@tmux_worker_id"): "development-developer",
            ("审核员-翼火蛇", "@tmux_runtime_dir"): "/tmp/drl-pm/.development_runtime/强化学习资产配置/development-review-1",
            ("审核员-翼火蛇", "@tmux_work_dir"): "/tmp/drl-pm",
            ("审核员-翼火蛇", "@tmux_requirement_name"): "强化学习资产配置",
            ("审核员-翼火蛇", "@tmux_workflow_action"): "stage.a07.start",
            ("审核员-翼火蛇", "@tmux_worker_id"): "development-review-审核员",
        }
        server._tmux_runtime = SimpleNamespace(  # noqa: SLF001
            list_sessions=lambda: ["开发工程师-地雄星", "审核员-翼火蛇", "plain-shell"],
            backend=SimpleNamespace(
                show_option=lambda target, option_name: options.get((target, option_name), ""),
            ),
        )

        with patch("T11_tui_backend.cleanup_registered_tmux_workers", return_value=[]), patch.object(
            server,
            "_cleanup_visible_tmux_workers",
            return_value=[],
        ), patch.object(
            server,
            "_cleanup_project_runtime_tmux_workers",
            return_value=[],
        ):
            server.shutdown(cleanup_tmux=True)

        messages = [json.loads(line) for line in writer.getvalue().splitlines() if line.strip()]
        payloads = [item.get("payload", {}) for item in messages if item.get("type") == "log.append"]
        foreign_logs = [str(payload.get("text", "")) for payload in payloads if "当前退出不会清理它们" in str(payload.get("text", ""))]
        self.assertEqual(len(foreign_logs), 1)
        self.assertIn("审核员-翼火蛇", foreign_logs[0])
        self.assertIn("/tmp/drl-pm", foreign_logs[0])
        self.assertNotIn("开发工程师-地雄星", foreign_logs[0])

    def test_run_list_reads_existing_manifests(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir) / "project"
            project_dir.mkdir(parents=True)
            runtime_root = build_routing_runtime_root(project_dir)
            run_root = runtime_root / "run_demo"
            run_root.mkdir(parents=True)
            (run_root / "manifest.json").write_text(
                json.dumps(
                    {
                        "manifest_version": 1,
                        "run_id": "run_demo",
                        "runtime_dir": str(run_root),
                        "project_dir": str(project_dir),
                        "selection": {"project_dir": str(project_dir), "selected_dirs": [], "skipped_dirs": [], "forced_dirs": [], "project_missing_files": []},
                        "config": {"vendor": "codex", "model": "gpt-5.4", "reasoning_effort": "high", "proxy_url": ""},
                        "status": "completed",
                        "created_at": "2026-04-16T10:00:00",
                        "updated_at": "2026-04-16T10:00:00",
                        "workers": [],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            writer = io.StringIO()
            server = TuiBackendServer(reader=io.StringIO(), writer=writer)
            server._set_context(project_dir=str(project_dir), action="stage.a01.start")  # noqa: SLF001
            server.handle_request(build_request("run.list", {}, message_id="req_8"))
        messages = [json.loads(line) for line in writer.getvalue().splitlines() if line.strip()]
        payload = messages[0]["payload"]
        self.assertEqual(payload["runs"][0]["run_id"], "run_demo")

    def test_run_list_returns_empty_without_project_context(self):
        writer = io.StringIO()
        server = TuiBackendServer(reader=io.StringIO(), writer=writer)
        server.handle_request(build_request("run.list", {}, message_id="req_8_empty"))
        messages = [json.loads(line) for line in writer.getvalue().splitlines() if line.strip()]
        self.assertEqual(messages[0]["payload"]["runs"], [])

    def test_run_resume_requires_project_context_or_payload_project_dir(self):
        writer = io.StringIO()
        server = TuiBackendServer(reader=io.StringIO(), writer=writer)
        with self.assertRaisesRegex(ValueError, "当前项目内的 routing run"):
            server.handle_request(
                build_request(
                    "run.resume",
                    {"run_id": "run_demo"},
                    message_id="req_resume_missing_project",
                )
            )

    def test_workflow_a00_stage_label_follows_file_state_progression(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._set_context(project_dir=str(project_dir), requirement_name="贪吃蛇", action="workflow.a00.start")  # noqa: SLF001

            self.assertEqual(server._build_app_snapshot()["active_stage_label"], "路由初始化")  # noqa: SLF001

            for file_path in required_routing_layer_paths(project_dir):
                file_path.parent.mkdir(parents=True, exist_ok=True)
                file_path.write_text("ok", encoding="utf-8")
            self.assertEqual(server._build_app_snapshot()["active_stage_label"], "需求录入")  # noqa: SLF001

            original_requirement_path, requirements_clear_path, _, _ = build_requirements_clarification_paths(project_dir, "贪吃蛇")
            original_requirement_path.write_text("原始需求", encoding="utf-8")
            self.assertEqual(server._build_app_snapshot()["active_stage_label"], "需求澄清")  # noqa: SLF001

            requirements_clear_path.write_text("需求澄清", encoding="utf-8")
            self.assertEqual(server._build_app_snapshot()["active_stage_label"], "需求评审")  # noqa: SLF001
            self.assertFalse((project_dir / "贪吃蛇_人机交互澄清记录.md").exists())

            review_paths = build_requirements_review_paths(project_dir, "贪吃蛇")
            review_paths["merged_review_path"].write_text("评审完成", encoding="utf-8")
            self.assertEqual(server._build_app_snapshot()["active_stage_label"], "详细设计")  # noqa: SLF001

            update_pre_development_task_status(project_dir, "贪吃蛇", task_key="详细设计", completed=True)
            self.assertEqual(server._build_app_snapshot()["active_stage_label"], "任务拆分")  # noqa: SLF001

            update_pre_development_task_status(project_dir, "贪吃蛇", task_key="任务拆分", completed=True)
            self.assertEqual(server._build_app_snapshot()["active_stage_label"], "任务开发")  # noqa: SLF001

            development_paths = build_development_paths(project_dir, "贪吃蛇")
            development_paths["task_json_path"].write_text(
                json.dumps({"M1": {"M1-T1": True}}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            self.assertEqual(server._build_app_snapshot()["active_stage_label"], "复核")  # noqa: SLF001

            overall_review_paths = build_overall_review_paths(project_dir, "贪吃蛇")
            overall_review_paths["state_path"].write_text(
                json.dumps({"passed": True}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            self.assertEqual(server._build_app_snapshot()["active_stage_label"], "测试")  # noqa: SLF001

    def test_app_snapshot_prefers_display_action_for_active_stage_label(self):
        server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
        server._display_action = "stage.a07.start"  # noqa: SLF001
        server._context.current_action = ""  # noqa: SLF001

        app = server._build_app_snapshot()  # noqa: SLF001

        self.assertEqual(app["active_stage"], "stage.a07.start")
        self.assertEqual(app["active_stage_label"], "任务开发")

    def test_runtime_scanned_worker_snapshots_include_session_exists(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_root = Path(tmpdir)
            worker_root = runtime_root / "worker-1"
            worker_root.mkdir(parents=True)
            state_path = worker_root / "worker.state.json"
            state_path.write_text(
                json.dumps(
                    {
                        "session_name": "sess-runtime",
                        "work_dir": "/tmp/project",
                        "status": "running",
                        "workflow_stage": "create_running",
                        "agent_state": "READY",
                        "health_status": "alive",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._tmux_runtime = SimpleNamespace(session_exists=lambda name: name == "sess-runtime")  # noqa: SLF001
            workers = server._scan_runtime_workers(runtime_root)  # noqa: SLF001
        self.assertEqual(workers[0]["session_name"], "sess-runtime")
        self.assertTrue(workers[0]["session_exists"])

    def test_runtime_worker_lightweight_scan_skips_health_refresh(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_root = Path(tmpdir)
            worker_root = runtime_root / "worker-1"
            worker_root.mkdir(parents=True)
            (worker_root / "worker.state.json").write_text(
                json.dumps(
                    {
                        "session_name": "sess-runtime",
                        "work_dir": "/tmp/project",
                        "status": "running",
                        "agent_state": "BUSY",
                        "agent_alive": True,
                        "health_status": "alive",
                        "state_revision": 4,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._tmux_runtime = SimpleNamespace(  # noqa: SLF001
                session_exists=lambda _name: (_ for _ in ()).throw(
                    AssertionError("persisted-only scan must not query tmux")
                ),
                backend=object(),
            )
            with patch("T11_tui_backend.load_worker_from_state_path") as load_worker:
                workers = server._scan_runtime_workers(runtime_root, refresh_health=False)  # noqa: SLF001

        load_worker.assert_not_called()
        self.assertEqual(workers[0]["session_name"], "sess-runtime")
        self.assertTrue(workers[0]["session_exists"])
        self.assertEqual(workers[0]["state_revision"], 4)

    def test_runtime_worker_scan_tolerates_concurrently_deleted_directory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_root = Path(tmpdir)
            live_root = runtime_root / "worker-live"
            disappearing_root = runtime_root / "worker-disappearing"
            live_root.mkdir(parents=True)
            disappearing_root.mkdir(parents=True)
            (live_root / "worker.state.json").write_text(
                json.dumps(
                    {
                        "session_name": "sess-live",
                        "work_dir": "/tmp/project",
                        "status": "running",
                        "agent_state": "BUSY",
                        "health_status": "alive",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            real_scandir = os.scandir

            def flaky_scandir(path):  # noqa: ANN001
                if Path(path) == disappearing_root:
                    raise FileNotFoundError(str(path))
                return real_scandir(path)

            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._tmux_runtime = SimpleNamespace(session_exists=lambda name: name == "sess-live", backend=object())  # noqa: SLF001
            with patch("tmux_core.bridge.backend.os.scandir", side_effect=flaky_scandir), patch(
                "T11_tui_backend.load_worker_from_state_path"
            ) as load_worker:
                workers = server._scan_runtime_workers(runtime_root, refresh_health=False)  # noqa: SLF001

        load_worker.assert_not_called()
        self.assertEqual([worker["session_name"] for worker in workers], ["sess-live"])

    def test_worker_merge_uses_revision_before_same_second_status_rank(self):
        from tmux_core.bridge.backend import _merge_worker_snapshots

        state_path = "/tmp/runtime/worker.state.json"
        merged = _merge_worker_snapshots(
            [
                {
                    "session_name": "开发工程师-天罡星",
                    "state_path": state_path,
                    "state_revision": 9,
                    "agent_state": "BUSY",
                    "status": "running",
                    "updated_at": "2026-07-15T15:00:00",
                }
            ],
            [
                {
                    "session_name": "开发工程师-天罡星",
                    "state_path": state_path,
                    "state_revision": 10,
                    "agent_state": "READY",
                    "status": "succeeded",
                    "updated_at": "2026-07-15T15:00:00",
                }
            ],
        )

        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["state_revision"], 10)
        self.assertEqual(merged[0]["agent_state"], "READY")

    def test_handle_runtime_state_change_schedules_persisted_snapshot_without_stage_inference(self):
        server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
        server._display_action = "stage.a07.start"  # noqa: SLF001
        with patch.object(server, "_infer_runtime_stage_status", side_effect=AssertionError("stage inference should be async")) as infer_status, patch.object(
            server,
            "_schedule_snapshot_update",
        ) as schedule_snapshot:
            server._handle_runtime_state_change()  # noqa: SLF001

        schedule_snapshot.assert_called_once()
        infer_status.assert_not_called()
        self.assertFalse(schedule_snapshot.call_args.kwargs["refresh_worker_health"])

    def test_stage_action_change_schedules_stage_health_refresh_without_sync_emit(self):
        server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
        with patch.object(server, "_emit_snapshot_update", side_effect=AssertionError("sync snapshot should not run")) as emit_snapshot, patch.object(
            server,
            "_schedule_snapshot_update",
        ) as schedule_snapshot, patch.object(server, "_infer_runtime_stage_status", return_value=""), patch.object(
            server,
            "_build_hitl_snapshot",
            return_value={"pending": False},
        ):
            server._handle_runtime_stage_change("stage.a07.start")  # noqa: SLF001

        emit_snapshot.assert_not_called()
        schedule_snapshot.assert_called_once()
        self.assertEqual(schedule_snapshot.call_args.kwargs["sections"], {"app"})
        self.assertFalse(schedule_snapshot.call_args.kwargs["refresh_worker_health"])

    def test_runner_completion_schedules_snapshot_without_waiting_for_heavy_emit(self):
        writer = io.StringIO()
        server = TuiBackendServer(reader=io.StringIO(), writer=writer)

        with patch.object(server, "_emit_snapshot_update", side_effect=AssertionError("sync snapshot should not run")) as emit_snapshot, patch.object(
            server,
            "_schedule_snapshot_update",
        ) as schedule_snapshot, patch.object(server, "_maybe_chain_after_stage_success") as chain_after_success, patch.object(
            server,
            "_infer_runtime_stage_status",
            return_value="",
        ), patch.object(server, "_build_hitl_snapshot", return_value={"pending": False}):
            server._run_in_thread("req_flow", "workflow.a00.start", lambda: 0, argv=[], respond=True)  # noqa: SLF001
            for worker in list(server._workers.values()):  # noqa: SLF001
                worker.join(timeout=2.0)

        emit_snapshot.assert_not_called()
        self.assertTrue(schedule_snapshot.called)
        self.assertTrue(any(not call.kwargs.get("refresh_worker_health", True) for call in schedule_snapshot.call_args_list))
        chain_after_success.assert_called_once()

    def test_runner_failure_schedules_snapshot_without_sync_emit(self):
        writer = io.StringIO()
        server = TuiBackendServer(reader=io.StringIO(), writer=writer)

        with patch.object(server, "_emit_snapshot_update", side_effect=AssertionError("sync snapshot should not run")) as emit_snapshot, patch.object(
            server,
            "_schedule_snapshot_update",
        ) as schedule_snapshot, patch.object(server, "_infer_runtime_stage_status", return_value=""), patch.object(
            server,
            "_build_hitl_snapshot",
            return_value={"pending": False},
        ):
            server._run_in_thread("req_fail", "workflow.a00.start", lambda: (_ for _ in ()).throw(RuntimeError("boom")), respond=True)  # noqa: SLF001
            for worker in list(server._workers.values()):  # noqa: SLF001
                worker.join(timeout=2.0)

        emit_snapshot.assert_not_called()
        self.assertTrue(schedule_snapshot.called)
        self.assertTrue(any(not call.kwargs.get("refresh_worker_health", True) for call in schedule_snapshot.call_args_list))

    def test_runtime_triggered_lightweight_snapshot_includes_dispatch_state(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            requirement_name = "需求A"
            worker_root = project_dir / DEVELOPMENT_RUNTIME_ROOT_NAME / requirement_name / "worker-1"
            worker_root.mkdir(parents=True)
            (worker_root / "worker.state.json").write_text(
                json.dumps(
                    {
                        "worker_id": "development-review-测试工程师",
                        "session_name": "测试工程师-天慧星",
                        "work_dir": str(project_dir),
                        "project_dir": str(project_dir),
                        "requirement_name": requirement_name,
                        "workflow_action": "stage.a07.start",
                        "status": "running",
                        "agent_state": "READY",
                        "health_status": "alive",
                        "dispatch_state": "submitting",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._tmux_runtime = SimpleNamespace(session_exists=lambda name: name == "测试工程师-天慧星", backend=object())  # noqa: SLF001
            server._context.project_dir = str(project_dir)  # noqa: SLF001
            server._context.requirement_name = requirement_name  # noqa: SLF001
            emitted: list[tuple[str, dict[str, object]]] = []
            with patch.object(server, "emit_event", side_effect=lambda event, payload: emitted.append((event, payload))), patch(
                "T11_tui_backend.load_worker_from_state_path"
            ) as load_worker:
                server._emit_snapshot_update(  # noqa: SLF001
                    stage_routes=("development",),
                    refresh_worker_health=False,
                )

        load_worker.assert_not_called()
        stage_events = [payload for event, payload in emitted if event == "snapshot.stage"]
        self.assertEqual(stage_events[0]["route"], "development")
        workers = stage_events[0]["snapshot"]["workers"]  # type: ignore[index]
        self.assertEqual(workers[0]["dispatch_state"], "submitting")

    def test_flow_snapshot_flush_does_not_refresh_control_worker_health(self):
        class RaisingRefreshCenter(_FakeCenter):
            def refresh_worker_health(self) -> None:
                raise AssertionError("flow snapshot should not refresh worker health")

        writer = io.StringIO()
        server = TuiBackendServer(reader=io.StringIO(), writer=writer)
        server._controls["run_demo"] = ControlSessionState(control_id="run_demo", center=RaisingRefreshCenter())  # noqa: SLF001

        server._schedule_flow_snapshot_update(sections={"app", "control"})  # noqa: SLF001
        server._flush_dirty_snapshots()  # noqa: SLF001

        messages = [json.loads(line) for line in writer.getvalue().splitlines() if line.strip()]
        event_types = [item.get("type") for item in messages if item.get("kind") == "event"]
        self.assertIn("snapshot.app", event_types)
        self.assertIn("snapshot.control", event_types)

    def test_development_snapshot_recovers_sparse_health_state_from_tmux_identity(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            requirement_name = "需求A"
            runtime_dir = project_dir / DEVELOPMENT_RUNTIME_ROOT_NAME / requirement_name / "development-review-abcd1234"
            runtime_dir.mkdir(parents=True)
            (runtime_dir / "worker.state.json").write_text(
                json.dumps(
                    {
                        "agent_alive": True,
                        "agent_started": True,
                        "agent_ready": True,
                        "agent_state": "READY",
                        "health_status": "alive",
                        "health_note": "alive",
                        "updated_at": "2026-05-09T15:40:17",
                        "last_heartbeat_at": "2026-05-09T15:40:17",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            class FakeTmuxRuntime:
                backend = None

                def session_exists(self, name):
                    return name == "测试工程师-天慧星"

                def session_matches_worker_state(self, name, state, state_path):  # noqa: ARG002
                    return name == "测试工程师-天慧星"

                def worker_identity_for_runtime_dir(self, current_runtime_dir):
                    if Path(current_runtime_dir).resolve() != runtime_dir.resolve():
                        return {}
                    return {
                        "session_name": "测试工程师-天慧星",
                        "session_exists": True,
                        "worker_id": "development-review-测试工程师",
                        "work_dir": str(project_dir),
                        "project_dir": str(project_dir),
                        "requirement_name": requirement_name,
                        "workflow_action": "stage.a07.start",
                    }

            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._tmux_runtime = FakeTmuxRuntime()  # noqa: SLF001
            server._context.project_dir = str(project_dir)  # noqa: SLF001
            server._context.requirement_name = requirement_name  # noqa: SLF001

            snapshot = server._build_development_snapshot()  # noqa: SLF001

        self.assertEqual(len(snapshot["workers"]), 1)
        self.assertEqual(snapshot["workers"][0]["session_name"], "测试工程师-天慧星")
        self.assertEqual(snapshot["workers"][0]["worker_id"], "development-review-测试工程师")
        self.assertEqual(snapshot["workers"][0]["requirement_name"], requirement_name)
        self.assertEqual(snapshot["workers"][0]["workflow_action"], "stage.a07.start")
        self.assertTrue(snapshot["workers"][0]["session_exists"])

    def test_overall_review_snapshot_filters_stale_sparse_alive_state_without_session(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            requirement_name = "需求A"
            paths = build_overall_review_paths(project_dir, requirement_name)
            paths["task_json_path"].parent.mkdir(parents=True, exist_ok=True)
            paths["task_json_path"].write_text(json.dumps({"M1": {"M1-T1": True}}, ensure_ascii=False), encoding="utf-8")
            paths["state_path"].write_text(json.dumps({"passed": False}, ensure_ascii=False), encoding="utf-8")
            runtime_dir = project_dir / DEVELOPMENT_RUNTIME_ROOT_NAME / requirement_name / "development-review-stale"
            runtime_dir.mkdir(parents=True)
            (runtime_dir / "worker.state.json").write_text(
                json.dumps(
                    {
                        "agent_alive": True,
                        "agent_started": True,
                        "agent_ready": True,
                        "agent_state": "READY",
                        "health_status": "alive",
                        "health_note": "alive",
                        "updated_at": "2000-01-01T00:00:00+00:00",
                        "last_heartbeat_at": "2000-01-01T00:00:00+00:00",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._tmux_runtime = SimpleNamespace(session_exists=lambda _name: False, backend=None)  # noqa: SLF001
            server._set_context(project_dir=str(project_dir), requirement_name=requirement_name, action="stage.a08.start")  # noqa: SLF001

            snapshot = server._build_overall_review_snapshot()  # noqa: SLF001

        self.assertEqual(snapshot["workers"], [])

    def test_overall_review_snapshot_keeps_fresh_missing_session_probe_for_current_a08_worker(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            requirement_name = "需求A"
            paths = build_overall_review_paths(project_dir, requirement_name)
            paths["task_json_path"].parent.mkdir(parents=True, exist_ok=True)
            paths["task_json_path"].write_text(json.dumps({"M1": {"M1-T1": True}}, ensure_ascii=False), encoding="utf-8")
            paths["state_path"].write_text(json.dumps({"passed": False}, ensure_ascii=False), encoding="utf-8")
            runtime_dir = project_dir / DEVELOPMENT_RUNTIME_ROOT_NAME / requirement_name / "development-review-a08"
            runtime_dir.mkdir(parents=True)
            (runtime_dir / "worker.state.json").write_text(
                json.dumps(
                    {
                        "worker_id": "development-review-测试工程师",
                        "session_name": "测试工程师-天慧星",
                        "work_dir": str(project_dir),
                        "project_dir": str(project_dir.resolve()),
                        "requirement_name": requirement_name,
                        "workflow_action": "stage.a08.start",
                        "status": "succeeded",
                        "result_status": "succeeded",
                        "agent_state": "READY",
                        "health_status": "alive",
                        "updated_at": "2999-01-01T00:00:00+00:00",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._tmux_runtime = SimpleNamespace(session_exists=lambda _name: False, backend=None)  # noqa: SLF001
            server._set_context(project_dir=str(project_dir), requirement_name=requirement_name, action="stage.a08.start")  # noqa: SLF001

            snapshot = server._build_overall_review_snapshot()  # noqa: SLF001

        self.assertEqual([worker["session_name"] for worker in snapshot["workers"]], ["测试工程师-天慧星"])

    def test_runtime_scanned_running_worker_snapshots_mark_dead_when_session_is_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_root = Path(tmpdir)
            worker_root = runtime_root / "worker-1"
            worker_root.mkdir(parents=True)
            state_path = worker_root / "worker.state.json"
            state_path.write_text(
                json.dumps(
                    {
                        "session_name": "sess-missing",
                        "work_dir": "/tmp/project",
                        "status": "running",
                        "workflow_stage": "turn_running",
                        "agent_state": "READY",
                        "health_status": "alive",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._tmux_runtime = SimpleNamespace(session_exists=lambda name: False)  # noqa: SLF001
            workers = server._scan_runtime_workers(runtime_root)  # noqa: SLF001
        self.assertFalse(workers[0]["session_exists"])
        self.assertEqual(workers[0]["agent_state"], "DEAD")

    def test_runtime_scanned_worker_snapshot_rejects_tmux_session_from_other_context(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_root = Path(tmpdir)
            project_dir = runtime_root / "project-a"
            worker_root = runtime_root / "worker-1"
            worker_root.mkdir(parents=True)
            state_path = worker_root / "worker.state.json"
            state_path.write_text(
                json.dumps(
                    {
                        "session_name": "shared-session",
                        "work_dir": str(project_dir),
                        "project_dir": str(project_dir),
                        "requirement_name": "需求A",
                        "workflow_action": "stage.a08.start",
                        "status": "running",
                        "workflow_stage": "turn_running",
                        "agent_state": "READY",
                        "health_status": "alive",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            class FakeTmuxRuntime:
                backend = None

                def session_exists(self, name):
                    return name == "shared-session"

                def session_matches_worker_state(self, name, state, state_path):
                    return False

            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._tmux_runtime = FakeTmuxRuntime()  # noqa: SLF001
            workers = server._scan_runtime_workers(runtime_root)  # noqa: SLF001

        self.assertFalse(workers[0]["session_exists"])
        self.assertEqual(workers[0]["agent_state"], "DEAD")

    def test_runtime_scanned_busy_worker_keeps_live_session_when_context_mismatch(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_root = Path(tmpdir)
            project_dir = runtime_root / "project-a"
            worker_root = runtime_root / "worker-1"
            worker_root.mkdir(parents=True)
            state_path = worker_root / "worker.state.json"
            state_path.write_text(
                json.dumps(
                    {
                        "session_name": "shared-session",
                        "work_dir": str(project_dir),
                        "project_dir": str(project_dir),
                        "requirement_name": "需求A",
                        "workflow_action": "stage.a08.start",
                        "status": "running",
                        "result_status": "running",
                        "workflow_stage": "turn_running",
                        "agent_state": "BUSY",
                        "current_task_runtime_status": "running",
                        "health_status": "alive",
                        "health_note": "alive",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            class FakeTmuxRuntime:
                backend = None

                def session_exists(self, name):
                    return name == "shared-session"

                def session_matches_worker_state(self, name, state, state_path):
                    return False

            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._tmux_runtime = FakeTmuxRuntime()  # noqa: SLF001
            workers = server._scan_runtime_workers(runtime_root)  # noqa: SLF001

        self.assertTrue(workers[0]["session_exists"])
        self.assertEqual(workers[0]["agent_state"], "BUSY")
        self.assertEqual(workers[0]["health_status"], "alive")

    def test_runtime_scanned_busy_worker_still_marks_dead_when_session_is_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_root = Path(tmpdir)
            project_dir = runtime_root / "project-a"
            worker_root = runtime_root / "worker-1"
            worker_root.mkdir(parents=True)
            state_path = worker_root / "worker.state.json"
            state_path.write_text(
                json.dumps(
                    {
                        "session_name": "shared-session",
                        "work_dir": str(project_dir),
                        "project_dir": str(project_dir),
                        "requirement_name": "需求A",
                        "workflow_action": "stage.a08.start",
                        "status": "running",
                        "result_status": "running",
                        "workflow_stage": "turn_running",
                        "agent_state": "BUSY",
                        "current_task_runtime_status": "running",
                        "health_status": "alive",
                        "health_note": "alive",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            class FakeTmuxRuntime:
                backend = None

                def session_exists(self, name):
                    return False

                def session_matches_worker_state(self, name, state, state_path):
                    return False

            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._tmux_runtime = FakeTmuxRuntime()  # noqa: SLF001
            workers = server._scan_runtime_workers(runtime_root)  # noqa: SLF001

        self.assertFalse(workers[0]["session_exists"])
        self.assertEqual(workers[0]["agent_state"], "DEAD")
        self.assertEqual(workers[0]["health_status"], "dead")

    def test_runtime_scanned_running_worker_snapshots_refresh_health_via_tmux_runtime(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_root = Path(tmpdir)
            worker_root = runtime_root / "worker-1"
            worker_root.mkdir(parents=True)
            state_path = worker_root / "worker.state.json"
            state_path.write_text(
                json.dumps(
                    {
                        "worker_id": "requirements-review-r3",
                        "session_name": "sess-runtime",
                        "pane_id": "%1",
                        "work_dir": str(runtime_root),
                        "status": "running",
                        "workflow_stage": "turn_running",
                        "agent_state": "READY",
                        "health_status": "alive",
                        "health_note": "alive",
                        "config": {
                            "vendor": "gemini",
                            "model": "pro",
                            "resolved_model": "pro",
                            "reasoning_effort": "high",
                            "proxy_url": "",
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            class _RefreshedWorker:
                def refresh_health(self, **kwargs) -> None:
                    self.kwargs = kwargs
                    payload = json.loads(state_path.read_text(encoding="utf-8"))
                    payload["agent_state"] = "BUSY"
                    payload["health_note"] = "alive"
                    payload["last_heartbeat_at"] = "2026-04-20T16:00:00+08:00"
                    state_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._tmux_runtime = SimpleNamespace(  # noqa: SLF001
                session_exists=lambda name: name == "sess-runtime",
                backend=object(),
            )
            with patch("tmux_core.bridge.backend.load_worker_from_state_path", return_value=_RefreshedWorker()):
                workers = server._scan_runtime_workers(runtime_root)  # noqa: SLF001

        self.assertEqual(workers[0]["agent_state"], "BUSY")
        self.assertEqual(workers[0]["health_note"], "alive")
        self.assertEqual(workers[0]["vendor"], "gemini")
        self.assertEqual(workers[0]["model"], "pro")
        self.assertEqual(workers[0]["resolved_model"], "pro")
        self.assertEqual(workers[0]["reasoning_effort"], "high")

    def test_development_snapshot_refreshes_starting_gemini_reviewer_to_ready(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            requirement_name = "强化学习资产配置"
            paths = build_development_paths(project_dir, requirement_name)
            for file_path in (
                paths["task_md_path"],
                paths["task_json_path"],
                paths["developer_output_path"],
                paths["merged_review_path"],
                paths["detailed_design_path"],
            ):
                file_path.parent.mkdir(parents=True, exist_ok=True)
                file_path.write_text("ok\n", encoding="utf-8")
            paths["task_json_path"].write_text(json.dumps({"M1": {"M1-T1": False}}, ensure_ascii=False), encoding="utf-8")

            worker_root = project_dir / DEVELOPMENT_RUNTIME_ROOT_NAME / requirement_name / "reviewer-ba"
            worker_root.mkdir(parents=True)
            state_path = worker_root / "worker.state.json"
            state_path.write_text(
                json.dumps(
                    {
                        "worker_id": "development-review-需求分析师",
                        "session_name": "需求分析师-地英星",
                        "pane_id": "%1",
                        "work_dir": str(project_dir),
                        "project_dir": str(project_dir),
                        "requirement_name": requirement_name,
                        "workflow_action": "stage.a07.start",
                        "status": "running",
                        "result_status": "running",
                        "workflow_stage": "turn_running",
                        "agent_state": "STARTING",
                        "agent_started": False,
                        "health_status": "alive",
                        "health_note": "alive",
                        "config": {
                            "vendor": "gemini",
                            "model": "flash",
                            "resolved_model": "flash",
                            "reasoning_effort": "high",
                            "proxy_url": "",
                        },
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            class _RefreshedGeminiWorker:
                def refresh_health(self, **kwargs) -> None:  # noqa: ANN003
                    payload = json.loads(state_path.read_text(encoding="utf-8"))
                    payload["agent_state"] = "READY"
                    payload["agent_started"] = True
                    payload["agent_ready"] = True
                    payload["health_status"] = "alive"
                    payload["health_note"] = "alive"
                    payload["pane_title"] = "◇ Ready"
                    payload["last_heartbeat_at"] = "2026-05-11T12:00:00+08:00"
                    state_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._set_context(project_dir=str(project_dir), requirement_name=requirement_name, action="stage.a07.start")  # noqa: SLF001
            server._tmux_runtime = SimpleNamespace(  # noqa: SLF001
                session_exists=lambda name: name == "需求分析师-地英星",
                backend=object(),
            )
            with patch("tmux_core.bridge.backend.load_worker_from_state_path", return_value=_RefreshedGeminiWorker()):
                development = server._build_development_snapshot()  # noqa: SLF001

        self.assertEqual([worker["session_name"] for worker in development["workers"]], ["需求分析师-地英星"])
        self.assertEqual(development["workers"][0]["agent_state"], "READY")
        self.assertTrue(development["workers"][0]["agent_started"])
        self.assertEqual(development["workers"][0]["vendor"], "gemini")

    def test_design_snapshot_refreshes_stale_agy_reviewer_to_ready(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            requirement_name = "BrinsonRL"
            worker_root = project_dir / DETAILED_DESIGN_RUNTIME_ROOT_NAME / requirement_name / "detailed-design-review-agy"
            worker_root.mkdir(parents=True)
            state_path = worker_root / "worker.state.json"
            state_path.write_text(
                json.dumps(
                    {
                        "worker_id": "detailed-design-review-开发工程师",
                        "session_name": "开发工程师-天速星",
                        "pane_id": "%1",
                        "work_dir": str(project_dir),
                        "project_dir": str(project_dir),
                        "requirement_name": requirement_name,
                        "workflow_action": "stage.a05.start",
                        "status": "ready",
                        "result_status": "ready",
                        "workflow_stage": "pending",
                        "agent_state": "STARTING",
                        "agent_started": True,
                        "health_status": "alive",
                        "health_note": "alive",
                        "current_command": "agy",
                        "config": {
                            "vendor": "agy",
                            "model": "GPT-OSS 120B (Medium)",
                            "resolved_model": "GPT-OSS 120B (Medium)",
                            "reasoning_effort": "medium",
                            "proxy_url": "",
                        },
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            class _RefreshedAgyWorker:
                def refresh_health(self, **kwargs) -> None:  # noqa: ANN003
                    payload = json.loads(state_path.read_text(encoding="utf-8"))
                    payload["agent_state"] = "READY"
                    payload["agent_started"] = True
                    payload["agent_ready"] = True
                    payload["health_status"] = "alive"
                    payload["health_note"] = "alive"
                    payload["last_heartbeat_at"] = "2026-06-18T16:00:00+08:00"
                    state_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._set_context(project_dir=str(project_dir), requirement_name=requirement_name, action="stage.a05.start")  # noqa: SLF001
            server._tmux_runtime = SimpleNamespace(  # noqa: SLF001
                session_exists=lambda name: name == "开发工程师-天速星",
                backend=object(),
            )
            with patch("tmux_core.bridge.backend.load_worker_from_state_path", return_value=_RefreshedAgyWorker()):
                design = server._build_design_snapshot()  # noqa: SLF001

        self.assertEqual([worker["session_name"] for worker in design["workers"]], ["开发工程师-天速星"])
        self.assertEqual(design["workers"][0]["agent_state"], "READY")
        self.assertTrue(design["workers"][0]["agent_started"])
        self.assertEqual(design["workers"][0]["vendor"], "agy")

    def test_runtime_scanned_stale_dead_worker_refreshes_when_session_is_live(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_root = Path(tmpdir)
            worker_root = runtime_root / "worker-1"
            worker_root.mkdir(parents=True)
            state_path = worker_root / "worker.state.json"
            state_path.write_text(
                json.dumps(
                    {
                        "worker_id": "development-developer",
                        "session_name": "sess-runtime",
                        "pane_id": "%1",
                        "work_dir": str(runtime_root),
                        "status": "failed",
                        "result_status": "failed",
                        "workflow_stage": "turn_running",
                        "agent_state": "DEAD",
                        "health_status": "dead",
                        "health_note": "generic turn error",
                        "config": {
                            "vendor": "codex",
                            "model": "gpt-5.4",
                            "reasoning_effort": "high",
                            "proxy_url": "",
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            class _RefreshedWorker:
                def refresh_health(self, **kwargs) -> None:  # noqa: ANN003
                    payload = json.loads(state_path.read_text(encoding="utf-8"))
                    payload["agent_state"] = "READY"
                    payload["health_status"] = "alive"
                    payload["health_note"] = "alive"
                    payload["last_heartbeat_at"] = "2026-04-20T16:00:00+08:00"
                    state_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._tmux_runtime = SimpleNamespace(  # noqa: SLF001
                session_exists=lambda name: name == "sess-runtime",
                backend=object(),
            )
            with patch("tmux_core.bridge.backend.load_worker_from_state_path", return_value=_RefreshedWorker()):
                workers = server._scan_runtime_workers(runtime_root)  # noqa: SLF001

        self.assertTrue(workers[0]["session_exists"])
        self.assertEqual(workers[0]["agent_state"], "READY")
        self.assertEqual(workers[0]["health_status"], "alive")

    def test_runtime_scan_does_not_infer_busy_from_current_command_when_agent_state_is_invalid(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_root = Path(tmpdir)
            worker_root = runtime_root / "worker-1"
            worker_root.mkdir(parents=True)
            (worker_root / "worker.state.json").write_text(
                json.dumps(
                    {
                        "worker_id": "requirements-review-r3",
                        "session_name": "sess-runtime",
                        "pane_id": "%1",
                        "work_dir": str(runtime_root),
                        "status": "running",
                        "workflow_stage": "turn_running",
                        "agent_state": "not-a-runtime-state",
                        "agent_alive": True,
                        "agent_started": True,
                        "current_command": "codex",
                        "health_status": "alive",
                        "health_note": "alive",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._tmux_runtime = SimpleNamespace(  # noqa: SLF001
                session_exists=lambda name: name == "sess-runtime",
                backend=None,
            )

            workers = server._scan_runtime_workers(runtime_root)  # noqa: SLF001

        self.assertEqual(workers[0]["agent_state"], "")

    def test_runtime_scan_refresh_disables_reentrant_runtime_notifications(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_root = Path(tmpdir)
            worker_root = runtime_root / "worker-1"
            worker_root.mkdir(parents=True)
            state_path = worker_root / "worker.state.json"
            state_path.write_text(
                json.dumps(
                    {
                        "worker_id": "requirements-review-r3",
                        "session_name": "sess-runtime",
                        "pane_id": "%1",
                        "work_dir": str(runtime_root),
                        "status": "running",
                        "workflow_stage": "turn_running",
                        "health_status": "alive",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            class _RefreshedWorker:
                def __init__(self) -> None:
                    self.kwargs = {}

                def refresh_health(self, **kwargs) -> None:
                    self.kwargs = kwargs

            worker = _RefreshedWorker()
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._tmux_runtime = SimpleNamespace(  # noqa: SLF001
                session_exists=lambda name: name == "sess-runtime",
                backend=object(),
            )
            with patch("tmux_core.bridge.backend.load_worker_from_state_path", return_value=worker):
                server._scan_runtime_workers(runtime_root)  # noqa: SLF001

        self.assertEqual(worker.kwargs, {"notify_on_change": False})

    def test_runtime_scanned_worker_snapshots_keep_alive_health_note_for_ready_agent(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_root = Path(tmpdir)
            worker_root = runtime_root / "worker-1"
            worker_root.mkdir(parents=True)
            state_path = worker_root / "worker.state.json"
            state_path.write_text(
                json.dumps(
                    {
                        "session_name": "sess-reviewer",
                        "work_dir": "/tmp/project",
                        "result_status": "succeeded",
                        "workflow_stage": "turn_done",
                        "agent_state": "READY",
                        "health_status": "alive",
                        "health_note": "alive",
                        "updated_at": "2026-04-17T10:00:00+08:00",
                        "last_heartbeat_at": "2026-04-17T10:00:02+08:00",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._tmux_runtime = SimpleNamespace(session_exists=lambda name: name == "sess-reviewer")  # noqa: SLF001
            workers = server._scan_runtime_workers(runtime_root)  # noqa: SLF001
        self.assertEqual(workers[0]["agent_state"], "READY")
        self.assertEqual(workers[0]["health_note"], "alive")

    def test_runtime_scanned_worker_snapshots_keep_running_ready_agent_ready(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_root = Path(tmpdir)
            worker_root = runtime_root / "worker-1"
            worker_root.mkdir(parents=True)
            state_path = worker_root / "worker.state.json"
            state_path.write_text(
                json.dumps(
                    {
                        "session_name": "sess-reviewer",
                        "work_dir": "/tmp/project",
                        "result_status": "running",
                        "workflow_stage": "turn_running",
                        "agent_state": "READY",
                        "agent_started": True,
                        "agent_alive": True,
                        "current_command": "gemini",
                        "current_task_runtime_status": "running",
                        "health_status": "alive",
                        "health_note": "alive",
                        "updated_at": "2026-04-17T10:00:00+08:00",
                        "last_heartbeat_at": "2026-04-17T10:00:02+08:00",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._tmux_runtime = SimpleNamespace(session_exists=lambda name: name == "sess-reviewer", backend=None)  # noqa: SLF001
            workers = server._scan_runtime_workers(runtime_root)  # noqa: SLF001
        self.assertEqual(workers[0]["agent_state"], "READY")
        self.assertEqual(workers[0]["health_note"], "alive")

    def test_runtime_scanned_worker_snapshot_preserves_running_turn_when_agent_ready(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_root = Path(tmpdir)
            worker_root = runtime_root / "worker-1"
            worker_root.mkdir(parents=True)
            state_path = worker_root / "worker.state.json"
            state_path.write_text(
                json.dumps(
                    {
                        "session_name": "sess-reviewer",
                        "work_dir": "/tmp/project",
                        "status": "ready",
                        "result_status": "running",
                        "workflow_stage": "pending",
                        "agent_state": "READY",
                        "agent_started": True,
                        "agent_alive": True,
                        "current_command": "gemini",
                        "current_task_runtime_status": "running",
                        "health_status": "alive",
                        "health_note": "alive",
                        "note": "agent_ready",
                        "updated_at": "2026-04-17T10:00:00+08:00",
                        "last_heartbeat_at": "2026-04-17T10:00:02+08:00",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._tmux_runtime = SimpleNamespace(session_exists=lambda name: name == "sess-reviewer", backend=None)  # noqa: SLF001
            workers = server._scan_runtime_workers(runtime_root)  # noqa: SLF001
        self.assertEqual(workers[0]["status"], "running")
        self.assertEqual(workers[0]["agent_state"], "READY")
        self.assertEqual(workers[0]["current_task_runtime_status"], "running")

    def test_runtime_scanned_worker_snapshots_prefer_fresher_alive_health_note_while_turn_is_running(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_root = Path(tmpdir)
            worker_root = runtime_root / "worker-1"
            worker_root.mkdir(parents=True)
            state_path = worker_root / "worker.state.json"
            state_path.write_text(
                json.dumps(
                    {
                        "session_name": "sess-active",
                        "work_dir": "/tmp/project",
                        "result_status": "running",
                        "workflow_stage": "turn_running",
                        "agent_state": "BUSY",
                        "health_status": "alive",
                        "health_note": "alive",
                        "updated_at": "2026-04-17T10:00:00+08:00",
                        "last_heartbeat_at": "2026-04-17T10:00:03+08:00",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._tmux_runtime = SimpleNamespace(session_exists=lambda name: name == "sess-active")  # noqa: SLF001
            workers = server._scan_runtime_workers(runtime_root)  # noqa: SLF001
        self.assertEqual(workers[0]["agent_state"], "BUSY")
        self.assertEqual(workers[0]["health_note"], "alive")

    def test_runtime_scanned_worker_snapshots_keep_newer_alive_health_note_while_turn_is_running(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_root = Path(tmpdir)
            worker_root = runtime_root / "worker-1"
            worker_root.mkdir(parents=True)
            state_path = worker_root / "worker.state.json"
            state_path.write_text(
                json.dumps(
                    {
                        "session_name": "sess-active",
                        "work_dir": "/tmp/project",
                        "result_status": "running",
                        "workflow_stage": "turn_running",
                        "agent_state": "BUSY",
                        "health_status": "alive",
                        "health_note": "alive",
                        "updated_at": "2026-04-17T10:00:03+08:00",
                        "last_heartbeat_at": "2026-04-17T10:00:00+08:00",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._tmux_runtime = SimpleNamespace(session_exists=lambda name: name == "sess-active")  # noqa: SLF001
            workers = server._scan_runtime_workers(runtime_root)  # noqa: SLF001
        self.assertEqual(workers[0]["agent_state"], "BUSY")
        self.assertEqual(workers[0]["health_note"], "alive")

    def test_requirements_snapshot_prefers_latest_failed_worker_when_session_name_reused(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            runtime_root = project_dir / ".requirements_clarification_runtime"
            older_root = runtime_root / "worker-old"
            newer_root = runtime_root / "worker-new"
            older_root.mkdir(parents=True)
            newer_root.mkdir(parents=True)
            (older_root / "worker.state.json").write_text(
                json.dumps(
                    {
                        "session_name": "分析师-参水猿",
                        "work_dir": str(project_dir),
                        "result_status": "succeeded",
                        "workflow_stage": "pending",
                        "agent_state": "READY",
                        "health_status": "alive",
                        "note": "done:requirements_clarification_round_2",
                        "updated_at": "2026-04-20T14:04:58+08:00",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            (newer_root / "worker.state.json").write_text(
                json.dumps(
                    {
                        "session_name": "分析师-参水猿",
                        "work_dir": str(project_dir),
                        "result_status": "failed",
                        "workflow_stage": "pending",
                        "agent_state": "DEAD",
                        "health_status": "alive",
                        "note": "error:requirements_clarification_round_3",
                        "updated_at": "2026-04-20T14:06:58+08:00",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._set_context(project_dir=str(project_dir), requirement_name="基金数据生成器", action="stage.a03.start")  # noqa: SLF001

            snapshot = server._build_requirements_snapshot()  # noqa: SLF001

        self.assertEqual(len(snapshot["workers"]), 1)
        self.assertEqual(snapshot["workers"][0]["session_name"], "分析师-参水猿")
        self.assertEqual(snapshot["workers"][0]["status"], "failed")
        self.assertEqual(snapshot["workers"][0]["note"], "error:requirements_clarification_round_3")

    def test_runtime_state_change_surfaces_failed_requirements_worker(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            runtime_root = project_dir / ".requirements_clarification_runtime" / "worker-latest"
            runtime_root.mkdir(parents=True)
            (runtime_root / "worker.state.json").write_text(
                json.dumps(
                    {
                        "session_name": "分析师-参水猿",
                        "work_dir": str(project_dir),
                        "result_status": "failed",
                        "workflow_stage": "pending",
                        "agent_state": "DEAD",
                        "health_status": "alive",
                        "note": "error:requirements_clarification_round_3",
                        "updated_at": "2026-04-20T14:06:58+08:00",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            writer = io.StringIO()
            server = TuiBackendServer(reader=io.StringIO(), writer=writer)
            server._set_context(project_dir=str(project_dir), requirement_name="基金数据生成器", action="stage.a03.start")  # noqa: SLF001
            server._display_status = "completed"  # noqa: SLF001

            server._bridge_ui.notify_runtime_state_changed()  # noqa: SLF001
            server._flush_dirty_snapshots()  # noqa: SLF001
            messages = [json.loads(line) for line in writer.getvalue().splitlines() if line.strip()]

        stage_events = [item for item in messages if item.get("kind") == "event" and item.get("type") == "stage.changed"]
        self.assertTrue(stage_events)
        self.assertEqual(stage_events[-1]["payload"]["action"], "stage.a03.start")
        self.assertEqual(stage_events[-1]["payload"]["status"], "failed")

    def test_stage_a03_status_is_not_polluted_by_failed_requirement_intake_worker(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            runtime_root = project_dir / NOTION_RUNTIME_ROOT_NAME / "worker-failed"
            runtime_root.mkdir(parents=True)
            (runtime_root / "worker.state.json").write_text(
                json.dumps(
                    {
                        "session_name": "录入-危月燕",
                        "work_dir": str(project_dir),
                        "result_status": "failed",
                        "workflow_stage": "pending",
                        "agent_state": "DEAD",
                        "health_status": "alive",
                        "note": "error:notion_round_1",
                        "updated_at": "2026-04-20T14:06:58+08:00",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._set_context(project_dir=str(project_dir), requirement_name="基金数据生成器", action="stage.a03.start")  # noqa: SLF001

            a02_status = server._infer_runtime_stage_status("stage.a02.start")  # noqa: SLF001
            a03_status = server._infer_runtime_stage_status("stage.a03.start")  # noqa: SLF001
            snapshot = server._build_requirements_snapshot()  # noqa: SLF001

        self.assertEqual(a02_status, "failed")
        self.assertEqual(a03_status, "")
        self.assertEqual(snapshot["workers"], [])

    def test_stage_a02_snapshot_and_app_status_are_not_polluted_by_failed_clarification_worker(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            runtime_root = project_dir / ".requirements_clarification_runtime" / "worker-failed"
            runtime_root.mkdir(parents=True)
            (runtime_root / "worker.state.json").write_text(
                json.dumps(
                    {
                        "session_name": "分析师-井木犴",
                        "work_dir": str(project_dir),
                        "status": "running",
                        "workflow_stage": "requirements_clarification",
                        "agent_state": "DEAD",
                        "health_status": "missing_session",
                        "note": "awaiting_reconfig",
                        "updated_at": "2026-04-20T14:06:58+08:00",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            writer = io.StringIO()
            server = TuiBackendServer(reader=io.StringIO(), writer=writer)
            server._set_context(project_dir=str(project_dir), requirement_name="基金数据生成器", action="stage.a02.start")  # noqa: SLF001

            snapshot = server._build_requirements_snapshot()  # noqa: SLF001
            app = server._build_app_snapshot(stage_snapshots={"requirements": snapshot})  # noqa: SLF001
            a02_status = server._infer_runtime_stage_status("stage.a02.start")  # noqa: SLF001

        self.assertEqual(snapshot["workers"], [])
        self.assertEqual(a02_status, "")
        self.assertNotEqual(app["active_stage_status"], "failed")

    def test_routing_snapshot_uses_latest_run_workers_without_active_control_session(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir) / "project"
            project_dir.mkdir(parents=True)
            runtime_root = build_routing_runtime_root(project_dir)
            for file_path in required_routing_layer_paths(project_dir):
                file_path.parent.mkdir(parents=True, exist_ok=True)
                file_path.write_text("ok", encoding="utf-8")

            run_root = runtime_root / "run_demo"
            run_root.mkdir(parents=True)
            (run_root / "manifest.json").write_text(
                json.dumps(
                    {
                        "manifest_version": 1,
                        "run_id": "run_demo",
                        "runtime_dir": str(run_root),
                        "project_dir": str(project_dir),
                        "selection": {"project_dir": str(project_dir), "selected_dirs": [], "skipped_dirs": [], "forced_dirs": [], "project_missing_files": []},
                        "config": {"vendor": "codex", "model": "gpt-5.4", "reasoning_effort": "high", "proxy_url": ""},
                        "status": "running",
                        "created_at": "2026-04-16T10:00:00",
                        "updated_at": "2026-04-16T10:00:00",
                        "workers": [
                            {
                                "work_dir": str(project_dir),
                                "session_name": "sess-routing",
                                "workflow_stage": "create_running",
                                "result_status": "running",
                                "agent_state": "READY",
                                "health_status": "alive",
                                "current_task_runtime_status": "running",
                                "current_turn_status_path": str(run_root / "turn_status.json"),
                                "state_path": "",
                                "transcript_path": "",
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._set_context(project_dir=str(project_dir), action="workflow.a00.start")  # noqa: SLF001
            server._tmux_runtime = SimpleNamespace(session_exists=lambda name: name == "sess-routing")  # noqa: SLF001
            snapshot = server._build_routing_snapshot()  # noqa: SLF001
        self.assertEqual(snapshot["workers"][0]["session_name"], "sess-routing")
        self.assertTrue(snapshot["workers"][0]["session_exists"])
        self.assertEqual(snapshot["workers"][0]["agent_state"], "READY")
        self.assertEqual(snapshot["workers"][0]["current_task_runtime_status"], "running")

    def test_manifest_backed_prelaunch_routing_worker_with_missing_session_stays_starting(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir) / "project"
            project_dir.mkdir(parents=True)
            runtime_root = build_routing_runtime_root(project_dir)
            for file_path in required_routing_layer_paths(project_dir):
                file_path.parent.mkdir(parents=True, exist_ok=True)
                file_path.write_text("ok", encoding="utf-8")

            run_root = runtime_root / "run_demo"
            run_root.mkdir(parents=True)
            worker_state_path = run_root / "worker.state.json"
            worker_state_path.write_text(
                json.dumps(
                    {
                        "worker_id": "routing-prelaunch",
                        "work_dir": str(project_dir),
                        "session_name": "sess-routing-prelaunch",
                        "workflow_stage": "pending",
                        "result_status": "pending",
                        "status": "pending",
                        "agent_state": "STARTING",
                        "agent_alive": False,
                        "agent_started": False,
                        "health_status": "unknown",
                        "note": "worker_prepared",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            (run_root / "manifest.json").write_text(
                json.dumps(
                    {
                        "manifest_version": 1,
                        "run_id": "run_demo",
                        "runtime_dir": str(run_root),
                        "project_dir": str(project_dir),
                        "selection": {"project_dir": str(project_dir), "selected_dirs": [], "skipped_dirs": [], "forced_dirs": [], "project_missing_files": []},
                        "config": {"vendor": "codex", "model": "gpt-5.4", "reasoning_effort": "high", "proxy_url": ""},
                        "status": "running",
                        "created_at": "2026-04-16T10:00:00",
                        "updated_at": "2026-04-16T10:00:00",
                        "workers": [
                            {
                                "work_dir": str(project_dir),
                                "session_name": "sess-routing-prelaunch",
                                "workflow_stage": "pending",
                                "result_status": "pending",
                                "agent_state": "STARTING",
                                "agent_alive": False,
                                "agent_started": False,
                                "health_status": "unknown",
                                "state_path": str(worker_state_path),
                                "transcript_path": "",
                                "note": "worker_prepared",
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._set_context(project_dir=str(project_dir), action="stage.a01.start")  # noqa: SLF001
            def unexpected_tmux_probe(*_args, **_kwargs):  # noqa: ANN002, ANN003
                raise AssertionError("prelaunch snapshot must not probe tmux")

            server._tmux_runtime = SimpleNamespace(  # noqa: SLF001
                backend=object(),
                session_exists=unexpected_tmux_probe,
                session_matches_worker_state=unexpected_tmux_probe,
                worker_identity_for_runtime_dir=unexpected_tmux_probe,
            )

            with patch(
                "T11_tui_backend.load_worker_from_state_path",
                side_effect=AssertionError("prelaunch snapshot must not load runtime worker"),
            ):
                snapshot = server._build_routing_snapshot()  # noqa: SLF001
            status = server._infer_runtime_stage_status("stage.a01.start")  # noqa: SLF001

        self.assertFalse(snapshot["workers"][0]["session_exists"])
        self.assertEqual(snapshot["workers"][0]["agent_state"], "STARTING")
        self.assertEqual(snapshot["workers"][0]["health_status"], "unknown")
        self.assertEqual(status, "running")

    def test_manifest_backed_active_prelaunch_routing_worker_with_missing_session_stays_starting(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir) / "project"
            project_dir.mkdir(parents=True)
            runtime_root = build_routing_runtime_root(project_dir)
            for file_path in required_routing_layer_paths(project_dir):
                file_path.parent.mkdir(parents=True, exist_ok=True)
                file_path.write_text("ok", encoding="utf-8")

            run_root = runtime_root / "run_demo"
            run_root.mkdir(parents=True)
            worker_state_path = run_root / "worker.state.json"
            worker_state_path.write_text(
                json.dumps(
                    {
                        "worker_id": "routing-prelaunch-active",
                        "work_dir": str(project_dir),
                        "session_name": "sess-routing-launching",
                        "workflow_stage": "create_running",
                        "result_status": "running",
                        "status": "pending",
                        "agent_state": "DEAD",
                        "agent_alive": False,
                        "agent_started": False,
                        "health_status": "dead",
                        "health_note": "missing_session",
                        "note": "create_routing_layer",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            (run_root / "manifest.json").write_text(
                json.dumps(
                    {
                        "manifest_version": 1,
                        "run_id": "run_demo",
                        "runtime_dir": str(run_root),
                        "project_dir": str(project_dir),
                        "selection": {"project_dir": str(project_dir), "selected_dirs": [], "skipped_dirs": [], "forced_dirs": [], "project_missing_files": []},
                        "config": {"vendor": "codex", "model": "gpt-5.4", "reasoning_effort": "high", "proxy_url": ""},
                        "status": "running",
                        "created_at": "2026-04-16T10:00:00",
                        "updated_at": "2026-04-16T10:00:00",
                        "workers": [
                            {
                                "work_dir": str(project_dir),
                                "session_name": "sess-routing-launching",
                                "workflow_stage": "create_running",
                                "result_status": "running",
                                "agent_state": "DEAD",
                                "agent_alive": False,
                                "agent_started": False,
                                "health_status": "dead",
                                "health_note": "missing_session",
                                "state_path": str(worker_state_path),
                                "transcript_path": "",
                                "note": "create_routing_layer",
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._set_context(project_dir=str(project_dir), action="stage.a01.start")  # noqa: SLF001
            def unexpected_tmux_probe(*_args, **_kwargs):  # noqa: ANN002, ANN003
                raise AssertionError("active prelaunch snapshot must not probe tmux")

            server._tmux_runtime = SimpleNamespace(  # noqa: SLF001
                backend=object(),
                session_exists=unexpected_tmux_probe,
                session_matches_worker_state=unexpected_tmux_probe,
                worker_identity_for_runtime_dir=unexpected_tmux_probe,
            )

            with patch(
                "T11_tui_backend.load_worker_from_state_path",
                side_effect=AssertionError("active prelaunch snapshot must not load runtime worker"),
            ):
                snapshot = server._build_routing_snapshot()  # noqa: SLF001
            status = server._infer_runtime_stage_status("stage.a01.start")  # noqa: SLF001

        self.assertFalse(snapshot["workers"][0]["session_exists"])
        self.assertEqual(snapshot["workers"][0]["workflow_stage"], "create_running")
        self.assertEqual(snapshot["workers"][0]["agent_state"], "STARTING")
        self.assertEqual(snapshot["workers"][0]["health_status"], "unknown")
        self.assertEqual(snapshot["workers"][0]["health_note"], "launch pending")
        self.assertEqual(status, "running")

    def test_manifest_backed_busy_routing_worker_marks_stage_running(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir) / "project"
            project_dir.mkdir(parents=True)
            runtime_root = build_routing_runtime_root(project_dir)
            for file_path in required_routing_layer_paths(project_dir):
                file_path.parent.mkdir(parents=True, exist_ok=True)
                file_path.write_text("ok", encoding="utf-8")

            run_root = runtime_root / "run_demo"
            run_root.mkdir(parents=True)
            (run_root / "manifest.json").write_text(
                json.dumps(
                    {
                        "manifest_version": 1,
                        "run_id": "run_demo",
                        "runtime_dir": str(run_root),
                        "project_dir": str(project_dir),
                        "selection": {"project_dir": str(project_dir), "selected_dirs": [], "skipped_dirs": [], "forced_dirs": [], "project_missing_files": []},
                        "config": {"vendor": "codex", "model": "gpt-5.4", "reasoning_effort": "high", "proxy_url": ""},
                        "status": "running",
                        "created_at": "2026-04-16T10:00:00",
                        "updated_at": "2026-04-16T10:00:00",
                        "workers": [
                            {
                                "work_dir": str(project_dir),
                                "session_name": "sess-routing-busy",
                                "workflow_stage": "create_running",
                                "result_status": "running",
                                "agent_state": "BUSY",
                                "agent_alive": True,
                                "agent_started": True,
                                "health_status": "alive",
                                "state_path": "",
                                "transcript_path": "",
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._set_context(project_dir=str(project_dir), action="stage.a01.start")  # noqa: SLF001
            server._tmux_runtime = SimpleNamespace(session_exists=lambda name: name == "sess-routing-busy")  # noqa: SLF001

            status = server._infer_runtime_stage_status("stage.a01.start")  # noqa: SLF001

        self.assertEqual(status, "running")

    def test_completed_routing_contract_ignores_manifest_only_missing_session(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir) / "project"
            project_dir.mkdir(parents=True)
            runtime_root = build_routing_runtime_root(project_dir)
            _write_valid_routing_layer(project_dir)

            run_root = runtime_root / "run_demo"
            run_root.mkdir(parents=True)
            (run_root / "manifest.json").write_text(
                json.dumps(
                    {
                        "manifest_version": 1,
                        "run_id": "run_demo",
                        "runtime_dir": str(run_root),
                        "project_dir": str(project_dir),
                        "selection": {"project_dir": str(project_dir), "selected_dirs": [], "skipped_dirs": [], "forced_dirs": [], "project_missing_files": []},
                        "config": {"vendor": "codex", "model": "gpt-5.4", "reasoning_effort": "high", "proxy_url": ""},
                        "status": "running",
                        "created_at": "2026-04-16T10:00:00",
                        "updated_at": "2026-04-16T10:00:00",
                        "workers": [
                            {
                                "work_dir": str(project_dir),
                                "session_name": "sess-routing-cleaned",
                                "workflow_stage": "create_running",
                                "result_status": "running",
                                "agent_state": "READY",
                                "health_status": "alive",
                                "state_path": "",
                                "transcript_path": "",
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._set_context(project_dir=str(project_dir), action="stage.a01.start")  # noqa: SLF001
            server._tmux_runtime = SimpleNamespace(session_exists=lambda name: False)  # noqa: SLF001

            status = server._infer_runtime_stage_status("stage.a01.start")  # noqa: SLF001
            action, display_status, _stage_seq = server._derive_display_stage_state(  # noqa: SLF001
                preferred_status="completed",
                preferred_action="stage.a01.start",
            )

        self.assertEqual(status, "")
        self.assertEqual(action, "stage.a01.start")
        self.assertEqual(display_status, "completed")

    def test_invalid_nonempty_routing_files_are_not_contract_ready(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir) / "project"
            project_dir.mkdir(parents=True)
            for file_path in required_routing_layer_paths(project_dir):
                file_path.parent.mkdir(parents=True, exist_ok=True)
                file_path.write_text("ok", encoding="utf-8")

            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._set_context(project_dir=str(project_dir), action="stage.a01.start")  # noqa: SLF001

            ready = server._routing_contract_is_ready()  # noqa: SLF001

        self.assertFalse(ready)

    def test_object_ref_routing_files_are_contract_ready(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir) / "project"
            project_dir.mkdir(parents=True)
            _write_valid_routing_layer(project_dir)

            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._set_context(project_dir=str(project_dir), action="stage.a01.start")  # noqa: SLF001

            ready = server._routing_contract_is_ready()  # noqa: SLF001

        self.assertTrue(ready)

    def test_manifest_backed_running_worker_snapshot_marks_dead_when_session_is_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir) / "project"
            project_dir.mkdir(parents=True)
            runtime_root = build_routing_runtime_root(project_dir)
            for file_path in required_routing_layer_paths(project_dir):
                file_path.parent.mkdir(parents=True, exist_ok=True)
                file_path.write_text("ok", encoding="utf-8")

            run_root = runtime_root / "run_demo"
            run_root.mkdir(parents=True)
            (run_root / "manifest.json").write_text(
                json.dumps(
                    {
                        "manifest_version": 1,
                        "run_id": "run_demo",
                        "runtime_dir": str(run_root),
                        "project_dir": str(project_dir),
                        "selection": {"project_dir": str(project_dir), "selected_dirs": [], "skipped_dirs": [], "forced_dirs": [], "project_missing_files": []},
                        "config": {"vendor": "codex", "model": "gpt-5.4", "reasoning_effort": "high", "proxy_url": ""},
                        "status": "running",
                        "created_at": "2026-04-16T10:00:00",
                        "updated_at": "2026-04-16T10:00:00",
                        "workers": [
                            {
                                "work_dir": str(project_dir),
                                "session_name": "sess-routing-dead",
                                "workflow_stage": "create_running",
                                "result_status": "running",
                                "agent_state": "READY",
                                "health_status": "alive",
                                "state_path": "",
                                "transcript_path": "",
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._set_context(project_dir=str(project_dir), action="workflow.a00.start")  # noqa: SLF001
            server._tmux_runtime = SimpleNamespace(session_exists=lambda name: False)  # noqa: SLF001

            snapshot = server._build_routing_snapshot()  # noqa: SLF001

        self.assertEqual(snapshot["workers"][0]["session_name"], "sess-routing-dead")
        self.assertFalse(snapshot["workers"][0]["session_exists"])
        self.assertEqual(snapshot["workers"][0]["agent_state"], "DEAD")
        self.assertEqual(snapshot["workers"][0]["health_status"], "dead")
        self.assertEqual(snapshot["workers"][0]["health_note"], "tmux session missing")

    def test_routing_setup_prompt_suppresses_manifest_only_dead_workers(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir) / "project"
            project_dir.mkdir(parents=True)
            runtime_root = build_routing_runtime_root(project_dir)
            for file_path in required_routing_layer_paths(project_dir):
                file_path.parent.mkdir(parents=True, exist_ok=True)
                file_path.write_text("ok", encoding="utf-8")

            run_root = runtime_root / "run_demo"
            run_root.mkdir(parents=True)
            (run_root / "manifest.json").write_text(
                json.dumps(
                    {
                        "manifest_version": 1,
                        "run_id": "run_demo",
                        "runtime_dir": str(run_root),
                        "project_dir": str(project_dir),
                        "selection": {"project_dir": str(project_dir), "selected_dirs": [], "skipped_dirs": [], "forced_dirs": [], "project_missing_files": []},
                        "config": {"vendor": "codex", "model": "gpt-5.4", "reasoning_effort": "high", "proxy_url": ""},
                        "status": "running",
                        "created_at": "2026-04-16T10:00:00",
                        "updated_at": "2026-04-16T10:00:00",
                        "workers": [
                            {
                                "work_dir": str(project_dir),
                                "session_name": "sess-routing-dead",
                                "workflow_stage": "create_running",
                                "result_status": "running",
                                "agent_state": "READY",
                                "health_status": "alive",
                                "state_path": "",
                                "transcript_path": "",
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._set_context(project_dir=str(project_dir), action="stage.a01.start")  # noqa: SLF001
            server._tmux_runtime = SimpleNamespace(session_exists=lambda name: False)  # noqa: SLF001
            server._pending_prompt = PendingPromptState(  # noqa: SLF001
                prompt_id="prompt-routing",
                prompt_type="select",
                payload={
                    "stage_key": "routing",
                    "stage_step_index": 1,
                    "prompt_text": "是否执行 AGENT初始化",
                    "options": [{"value": "yes", "label": "yes"}, {"value": "no", "label": "no"}],
                },
            )

            snapshot = server._build_routing_snapshot()  # noqa: SLF001

        self.assertEqual(snapshot["workers"], [])

    def test_routing_skip_prompt_resolution_clears_manifest_only_workers(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir) / "project"
            project_dir.mkdir(parents=True)
            runtime_root = build_routing_runtime_root(project_dir)
            for file_path in required_routing_layer_paths(project_dir):
                file_path.parent.mkdir(parents=True, exist_ok=True)
                file_path.write_text("ok", encoding="utf-8")

            run_root = runtime_root / "run_demo"
            run_root.mkdir(parents=True)
            (run_root / "manifest.json").write_text(
                json.dumps(
                    {
                        "manifest_version": 1,
                        "run_id": "run_demo",
                        "runtime_dir": str(run_root),
                        "project_dir": str(project_dir),
                        "selection": {"project_dir": str(project_dir), "selected_dirs": [], "skipped_dirs": [], "forced_dirs": [], "project_missing_files": []},
                        "config": {"vendor": "codex", "model": "gpt-5.4", "reasoning_effort": "high", "proxy_url": ""},
                        "status": "running",
                        "created_at": "2026-04-16T10:00:00",
                        "updated_at": "2026-04-16T10:00:00",
                        "workers": [
                            {
                                "work_dir": str(project_dir),
                                "session_name": "sess-routing-dead",
                                "workflow_stage": "create_running",
                                "result_status": "running",
                                "agent_state": "READY",
                                "health_status": "alive",
                                "state_path": "",
                                "transcript_path": "",
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            writer = io.StringIO()
            server = TuiBackendServer(reader=io.StringIO(), writer=writer)
            server._set_context(project_dir=str(project_dir), action="stage.a01.start")  # noqa: SLF001
            server._tmux_runtime = SimpleNamespace(session_exists=lambda name: False)  # noqa: SLF001
            server._pending_prompts["prompt-routing"] = PendingPromptState(  # noqa: SLF001
                prompt_id="prompt-routing",
                prompt_type="select",
                payload={
                    "stage_key": "routing",
                    "stage_step_index": 1,
                    "prompt_text": "是否执行 AGENT初始化",
                    "options": [{"value": "yes", "label": "yes"}, {"value": "no", "label": "no"}],
                },
            )
            server._pending_prompt = server._pending_prompts["prompt-routing"]  # noqa: SLF001

            server._handle_prompt_resolved("prompt-routing", {"value": "no"})  # noqa: SLF001
            server._flush_dirty_snapshots()  # noqa: SLF001
            snapshot = server._build_routing_snapshot()  # noqa: SLF001
            events = [json.loads(line) for line in writer.getvalue().splitlines() if line.strip()]

        self.assertEqual(snapshot["workers"], [])
        self.assertIsNone(server._pending_prompt)  # noqa: SLF001
        self.assertEqual(server._pending_prompts, {})  # noqa: SLF001
        self.assertTrue(
            any(
                item.get("kind") == "event"
                and item.get("type") == "snapshot.stage"
                and item.get("payload", {}).get("route") == "routing"
                and item.get("payload", {}).get("snapshot", {}).get("workers") == []
                for item in events
            )
        )

    def test_bridge_ui_runtime_state_change_notifier_debounces_app_and_current_stage_snapshots(self):
        writer = io.StringIO()
        server = TuiBackendServer(reader=io.StringIO(), writer=writer)
        server._set_context(action="stage.a03.start")  # noqa: SLF001
        server._bridge_ui.notify_runtime_state_changed()  # noqa: SLF001
        server._flush_dirty_snapshots()  # noqa: SLF001
        messages = [json.loads(line) for line in writer.getvalue().splitlines() if line.strip()]
        event_types = [item.get("type") for item in messages if item.get("kind") == "event"]
        self.assertIn("snapshot.app", event_types)
        self.assertIn("snapshot.stage", event_types)
        self.assertIn("snapshot.control", event_types)

    def test_bridge_ui_runtime_state_change_refreshes_requirements_worker_snapshot(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            runtime_root = project_dir / ".requirements_analysis_runtime" / "requirements-analyst-demo"
            runtime_root.mkdir(parents=True)
            (runtime_root / "worker.state.json").write_text(
                json.dumps(
                    {
                        "session_name": "sess-requirements",
                        "work_dir": str(project_dir),
                        "status": "running",
                        "workflow_stage": "requirements_analysis",
                        "agent_state": "READY",
                        "health_status": "alive",
                        "retry_count": 0,
                        "note": "requirements_analysis_round_1",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            writer = io.StringIO()
            server = TuiBackendServer(reader=io.StringIO(), writer=writer)
            server._set_context(project_dir=str(project_dir), requirement_name="贪吃蛇", action="stage.a03.start")  # noqa: SLF001
            server._tmux_runtime = SimpleNamespace(session_exists=lambda session_name: session_name == "sess-requirements")  # noqa: SLF001
            server._bridge_ui.notify_runtime_state_changed()  # noqa: SLF001
            server._flush_dirty_snapshots()  # noqa: SLF001
            messages = [json.loads(line) for line in writer.getvalue().splitlines() if line.strip()]
        requirement_snapshots = [
            item["payload"]["snapshot"]
            for item in messages
            if item.get("kind") == "event"
            and item.get("type") == "snapshot.stage"
            and item.get("payload", {}).get("route") == "requirements"
        ]
        self.assertTrue(requirement_snapshots)
        latest_snapshot = requirement_snapshots[-1]
        self.assertEqual(latest_snapshot["workers"][0]["session_name"], "sess-requirements")
        self.assertTrue(latest_snapshot["workers"][0]["session_exists"])

    def test_runtime_state_change_emits_hitl_snapshot_when_worker_question_is_pending(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            requirement_name = "需求A"
            paths = build_development_paths(project_dir, requirement_name)
            for path in (
                paths["task_md_path"],
                paths["task_json_path"],
                paths["developer_output_path"],
                paths["merged_review_path"],
                paths["detailed_design_path"],
            ):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("ok\n", encoding="utf-8")
            paths["task_json_path"].write_text(json.dumps({"M1": {"M1-T1": False}}, ensure_ascii=False), encoding="utf-8")
            paths["ask_human_path"].write_text("请确认评审冲突\n", encoding="utf-8")
            runtime_dir = project_dir / DEVELOPMENT_RUNTIME_ROOT_NAME / "worker-hitl"
            runtime_dir.mkdir(parents=True, exist_ok=True)
            (runtime_dir / "worker.state.json").write_text(
                json.dumps(
                    {
                        "worker_id": "development-developer",
                        "session_name": "开发工程师-天魁星",
                        "work_dir": str(project_dir),
                        "project_dir": str(project_dir),
                        "requirement_name": requirement_name,
                        "workflow_action": "stage.a07.start",
                        "result_status": "running",
                        "workflow_stage": "turn_running",
                        "agent_state": "READY",
                        "health_status": "alive",
                        "question_path": str(paths["ask_human_path"]),
                        "answer_path": str(paths["hitl_record_path"]),
                        "updated_at": "2026-04-23T10:00:00+08:00",
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            writer = io.StringIO()
            server = TuiBackendServer(reader=io.StringIO(), writer=writer)
            server._set_context(project_dir=str(project_dir), requirement_name=requirement_name, action="stage.a07.start")  # noqa: SLF001
            server._bridge_ui.notify_runtime_state_changed()  # noqa: SLF001
            server._flush_dirty_snapshots()  # noqa: SLF001
            messages = [json.loads(line) for line in writer.getvalue().splitlines() if line.strip()]

        hitl_events = [
            item["payload"]
            for item in messages
            if item.get("kind") == "event" and item.get("type") == "snapshot.hitl"
        ]
        self.assertTrue(hitl_events)
        self.assertTrue(hitl_events[-1]["pending"])
        self.assertEqual(hitl_events[-1]["question_path"], str(paths["ask_human_path"]))

    def test_app_recent_artifacts_use_cache_and_current_stage_when_stage_update_is_partial(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            routing_file = root / "routing.md"
            development_file = root / "development.md"
            routing_file.write_text("routing\n", encoding="utf-8")
            development_file.write_text("development\n", encoding="utf-8")
            files_by_route = {
                "routing": [{"path": str(routing_file)}],
                "development": [{"path": str(development_file)}],
            }
            built_routes: list[str] = []

            def fake_stage_snapshot(route: str) -> dict[str, object]:
                built_routes.append(route)
                return {"files": files_by_route.get(route, []), "workers": []}

            writer = io.StringIO()
            server = TuiBackendServer(reader=io.StringIO(), writer=writer)
            with patch.object(server, "_build_stage_snapshot_by_route", side_effect=fake_stage_snapshot):
                server._emit_snapshot_update(include_app=True, include_all_stages=True)  # noqa: SLF001
                built_routes.clear()
                server._emit_snapshot_update(include_app=True, stage_routes=("development",))  # noqa: SLF001
            messages = [json.loads(line) for line in writer.getvalue().splitlines() if line.strip()]

        app_events = [
            item["payload"]
            for item in messages
            if item.get("kind") == "event" and item.get("type") == "snapshot.app"
        ]
        artifact_paths = {item["path"] for item in app_events[-1]["recent_artifacts"]}
        self.assertIn(str(routing_file.resolve()), artifact_paths)
        self.assertIn(str(development_file.resolve()), artifact_paths)
        self.assertEqual(built_routes, ["development"])

    def test_artifact_item_builder_filters_empty_and_missing_candidates(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            existing = Path(tmpdir) / "artifact.md"
            existing.write_text("artifact\n", encoding="utf-8")
            missing = Path(tmpdir) / "missing.md"

            items = TuiBackendServer._artifact_items_from_candidates(["", str(missing), str(existing)])  # noqa: SLF001

        self.assertEqual([item["path"] for item in items], [str(existing.resolve())])

    def test_artifact_index_is_scoped_by_project_and_requirement(self):
        with tempfile.TemporaryDirectory() as project_a, tempfile.TemporaryDirectory() as project_b:
            old_file = Path(project_a) / "old.md"
            new_file = Path(project_b) / "new.md"
            old_file.write_text("old\n", encoding="utf-8")
            new_file.write_text("new\n", encoding="utf-8")
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())

            server._set_context(project_dir=project_a, requirement_name="req-a")  # noqa: SLF001
            seeded = server._build_artifacts_snapshot(  # noqa: SLF001
                stages={"development": {"files": [{"path": str(old_file)}]}},
                control={"workers": []},
            )
            self.assertEqual([item["path"] for item in seeded["items"]], [str(old_file.resolve())])

            server._set_context(project_dir=project_b, requirement_name="req-b")  # noqa: SLF001
            current = server._build_artifacts_snapshot(  # noqa: SLF001
                stages={"development": {"files": [{"path": str(new_file)}]}},
                control={"workers": []},
            )
            partial = server._build_artifacts_snapshot(stages={}, control={"workers": []})  # noqa: SLF001

        self.assertEqual([item["path"] for item in current["items"]], [str(new_file.resolve())])
        self.assertEqual([item["path"] for item in partial["items"]], [str(new_file.resolve())])

    def test_snapshot_stage_registry_rejects_unknown_and_dedupes_routes(self):
        server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
        with self.assertRaises(KeyError):
            server._build_stage_snapshot_by_route("missing")  # noqa: SLF001

        with patch.object(server, "_build_routing_snapshot", return_value={"files": [], "workers": []}) as builder:
            snapshots = server._build_stage_snapshots(["routing", "routing"])  # noqa: SLF001

        self.assertEqual(list(snapshots), ["routing"])
        builder.assert_called_once()

    def test_snapshot_update_reuses_runtime_scan_results_within_same_emit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            requirement_name = "需求A"
            runtime_dir = project_dir / DEVELOPMENT_RUNTIME_ROOT_NAME / requirement_name / "development-review-abcd"
            runtime_dir.mkdir(parents=True, exist_ok=True)
            (runtime_dir / "worker.state.json").write_text(
                json.dumps(
                    {
                        "worker_id": "development-review-测试工程师",
                        "session_name": "测试工程师-地耗星",
                        "work_dir": str(project_dir),
                        "status": "running",
                        "agent_state": "BUSY",
                        "health_status": "alive",
                        "question_path": "",
                        "project_dir": str(project_dir.resolve()),
                        "requirement_name": requirement_name,
                        "workflow_action": "stage.a07.start",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
            server._set_context(project_dir=str(project_dir), requirement_name=requirement_name, action="stage.a07.start")  # noqa: SLF001

            original_iter = server._iter_worker_state_paths  # noqa: SLF001
            with patch.object(server, "_iter_worker_state_paths", wraps=original_iter) as iter_paths:
                server._emit_snapshot_update(  # noqa: SLF001
                    include_app=True,
                    include_hitl=True,
                    stage_routes=("development",),
                    refresh_worker_health=False,
                )

        iter_paths.assert_called_once()

    def test_snapshot_update_logs_builder_failures_and_continues_with_fallbacks(self):
        writer = io.StringIO()
        server = TuiBackendServer(reader=io.StringIO(), writer=writer)
        with patch.object(server, "_build_stage_snapshots", side_effect=RuntimeError("stage boom")), patch.object(
            server,
            "_build_control_snapshot_for_session",
            side_effect=RuntimeError("control boom"),
        ), patch.object(server, "_build_hitl_snapshot", side_effect=RuntimeError("hitl boom")), patch.object(
            server._attention_manager,
            "snapshot",
            side_effect=RuntimeError("attention boom"),
        ), patch.object(server, "_build_artifacts_snapshot", side_effect=RuntimeError("artifacts boom")):
            server._emit_snapshot_update(  # noqa: SLF001
                include_app=True,
                include_control=True,
                include_hitl=True,
                include_artifacts=True,
                stage_routes=("routing",),
            )

        messages = [json.loads(line) for line in writer.getvalue().splitlines() if line.strip()]
        log_text = "\n".join(str(item.get("payload", {}).get("text", "")) for item in messages if item.get("type") == "log.append")
        self.assertIn("stage boom", log_text)
        self.assertIn("control boom", log_text)
        self.assertIn("hitl boom", log_text)
        self.assertIn("attention boom", log_text)
        self.assertIn("artifacts boom", log_text)
        self.assertTrue(any(item.get("type") == "snapshot.app" for item in messages))

    def test_snapshot_dirty_scheduler_noops_empty_updates_and_shutdown_cancels_timer(self):
        server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
        server._schedule_snapshot_update(sections=set(), stage_routes=())  # noqa: SLF001
        self.assertIsNone(server._snapshot_debounce_timer)  # noqa: SLF001

        server._schedule_snapshot_update(sections={"app"}, delay_sec=10.0)  # noqa: SLF001
        self.assertIsNotNone(server._snapshot_debounce_timer)  # noqa: SLF001
        server._schedule_snapshot_update(sections={"hitl"}, delay_sec=10.0)  # noqa: SLF001
        self.assertEqual(server._snapshot_dirty_sections, {"app", "hitl"})  # noqa: SLF001
        server.attach_adapter("tui")
        server._set_context(project_dir="/tmp/project", requirement_name="需求A", action="stage.a07.start")  # noqa: SLF001
        server.record_tui_presence("keyboard", "content")
        self.assertIsNotNone(server._tui_presence_refresh_timer)  # noqa: SLF001
        with patch("T11_tui_backend.cleanup_registered_tmux_workers", return_value=[]), patch.object(
            server, "_cleanup_visible_tmux_workers", return_value=[]
        ), patch.object(server, "_cleanup_project_runtime_tmux_workers", return_value=[]), patch.object(
            server, "_cleanup_current_project_tmux_sessions", return_value=[]
        ), patch.object(server, "_list_foreign_project_tmux_sessions", return_value=[]):
            server.shutdown(cleanup_tmux=False)
        self.assertIsNone(server._snapshot_debounce_timer)  # noqa: SLF001
        self.assertIsNone(server._tui_presence_refresh_timer)  # noqa: SLF001

    def test_snapshot_dirty_scheduler_is_single_flight_and_coalesces_follow_up(self):
        server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
        first_started = threading.Event()
        release_first = threading.Event()
        second_finished = threading.Event()
        call_lock = threading.Lock()
        calls: list[dict[str, object]] = []
        active_calls = 0
        max_active_calls = 0

        def slow_snapshot_emit(**kwargs):  # noqa: ANN003
            nonlocal active_calls, max_active_calls
            with call_lock:
                calls.append(dict(kwargs))
                call_index = len(calls)
                active_calls += 1
                max_active_calls = max(max_active_calls, active_calls)
            try:
                if call_index == 1:
                    first_started.set()
                    release_first.wait(timeout=2.0)
            finally:
                with call_lock:
                    active_calls -= 1
                if call_index == 2:
                    second_finished.set()

        with patch.object(server, "_emit_snapshot_update", side_effect=slow_snapshot_emit):
            server._schedule_snapshot_update(  # noqa: SLF001
                sections={"app"},
                delay_sec=0.0,
                refresh_worker_health=False,
            )
            self.assertTrue(first_started.wait(timeout=1.0))
            for _index in range(20):
                server._schedule_snapshot_update(  # noqa: SLF001
                    sections={"control", "hitl"},
                    stage_routes=("detailed-design",),
                    delay_sec=0.0,
                    refresh_worker_health=False,
                )
            release_first.set()
            self.assertTrue(second_finished.wait(timeout=2.0))
            deadline = time.time() + 1.0
            while time.time() < deadline:
                with server._snapshot_dirty_lock:  # noqa: SLF001
                    idle = not server._snapshot_flush_running and server._snapshot_debounce_timer is None  # noqa: SLF001
                if idle:
                    break
                time.sleep(0.005)

        self.assertEqual(len(calls), 2)
        self.assertEqual(max_active_calls, 1)
        self.assertTrue(calls[0]["include_app"])
        self.assertTrue(calls[1]["include_control"])
        self.assertTrue(calls[1]["include_hitl"])
        self.assertEqual(calls[1]["stage_routes"], ("detailed-design",))
        self.assertFalse(server._snapshot_flush_running)  # noqa: SLF001

    def test_snapshot_dirty_scheduler_releases_single_flight_latch_after_error(self):
        server = TuiBackendServer(reader=io.StringIO(), writer=io.StringIO())
        server._snapshot_dirty_sections.add("app")  # noqa: SLF001

        with patch.object(server, "_emit_snapshot_update", side_effect=RuntimeError("snapshot boom")):
            with self.assertRaisesRegex(RuntimeError, "snapshot boom"):
                server._flush_dirty_snapshots()  # noqa: SLF001

        self.assertFalse(server._snapshot_flush_running)  # noqa: SLF001
        self.assertIsNone(server._snapshot_debounce_timer)  # noqa: SLF001

        server._snapshot_dirty_sections.add("hitl")  # noqa: SLF001
        with patch.object(server, "_emit_snapshot_update") as emit_snapshot:
            server._flush_dirty_snapshots()  # noqa: SLF001

        emit_snapshot.assert_called_once()
        self.assertFalse(server._snapshot_flush_running)  # noqa: SLF001

    def test_worker_control_actions_emit_control_and_app_snapshots_only(self):
        for action in ("worker.detach", "worker.kill", "worker.restart", "worker.retry"):
            writer = io.StringIO()
            server = TuiBackendServer(reader=io.StringIO(), writer=writer)
            server._controls["run_demo"] = ControlSessionState(control_id="run_demo", center=_FakeCenter())  # noqa: SLF001
            server.handle_request(build_request(action, {"control_id": "run_demo", "argument": "1"}, message_id=f"req_{action}"))
            server._flush_dirty_snapshots()  # noqa: SLF001
            messages = [json.loads(line) for line in writer.getvalue().splitlines() if line.strip()]
            event_types = [item.get("type") for item in messages if item.get("kind") == "event"]
            self.assertIn("snapshot.app", event_types)
            self.assertIn("snapshot.control", event_types)
            self.assertNotIn("snapshot.stage", event_types)

    def test_emit_all_snapshots_uses_single_full_bundle_path(self):
        writer = io.StringIO()
        server = TuiBackendServer(reader=io.StringIO(), writer=writer)
        server._emit_all_snapshots()  # noqa: SLF001
        messages = [json.loads(line) for line in writer.getvalue().splitlines() if line.strip()]
        event_types = [item.get("type") for item in messages if item.get("kind") == "event"]
        self.assertIn("snapshot.app", event_types)
        self.assertIn("snapshot.control", event_types)
        self.assertIn("snapshot.hitl", event_types)
        self.assertIn("snapshot.prompt", event_types)
        self.assertIn("snapshot.artifacts", event_types)


if __name__ == "__main__":
    unittest.main()
