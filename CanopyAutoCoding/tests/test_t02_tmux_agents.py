from __future__ import annotations

import io
import json
import subprocess
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path

from canopy_core.runtime.vendor_catalog import get_default_model_for_vendor, get_model_choices
from T02_tmux_agents import (
    AgentRuntimeState,
    AgentRunConfig,
    GeminiOutputDetector,
    OpenCodeOutputDetector,
    QwenOutputDetector,
    CodexOutputDetector,
    ClaudeOutputDetector,
    KimiOutputDetector,
    LaunchCoordinator,
    ProviderPhase,
    WrapperState,
    WorkerObservation,
    WorkerHealthSnapshot,
    WorkerStatus,
    TIMEOUT_EXIT_CODE,
    TaskResultContract,
    TmuxBackend,
    TmuxBatchWorker,
    TurnFileContract,
    TurnFileResult,
    TmuxRuntimeController,
    Vendor,
    cleanup_registered_tmux_workers,
    build_prompt_header,
    build_proxy_env,
    build_reasoning_note,
    build_session_name,
    extract_final_protocol_token,
    load_worker_from_state_path,
    normalize_proxy_url,
    read_text_tail,
    TERMINAL_ACTIVITY_IDLE_WINDOW_SEC,
)
from canopy_core.runtime.contracts import finalize_task_result, write_task_status


class TmuxAgentsTests(unittest.TestCase):
    def test_normalize_proxy_url_accepts_port_or_url(self):
        self.assertEqual("http://127.0.0.1:7890", normalize_proxy_url("7890"))
        self.assertEqual("http://127.0.0.1:10809", normalize_proxy_url(10809))
        self.assertEqual("http://127.0.0.1:8899", normalize_proxy_url("127.0.0.1:8899"))
        self.assertEqual("https://proxy.example.com:8443", normalize_proxy_url("https://proxy.example.com:8443"))

    def test_build_proxy_env_populates_all_expected_keys(self):
        env = build_proxy_env("http://127.0.0.1:7890")
        self.assertEqual("http://127.0.0.1:7890", env["HTTP_PROXY"])
        self.assertEqual("socks5h://127.0.0.1:7890", env["all_proxy"])

    def test_normalize_proxy_url_accepts_explicit_socks_urls(self):
        self.assertEqual("socks5h://127.0.0.1:10900", normalize_proxy_url("socks5h://127.0.0.1:10900"))

    def test_prompt_header_contains_reasoning_note(self):
        header = build_prompt_header(Vendor.QWEN, "qwen3-coder", "xhigh")
        self.assertIn("vendor: qwen", header)
        self.assertIn("qwen_prompt_hint=true", header)
        self.assertIn("tmux_interactive_conversation", header)

    def test_gemini_ready_detection_requires_input_box_marker(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            worker = TmuxBatchWorker(
                worker_id="gemini-worker",
                work_dir=tmp_dir,
                config=AgentRunConfig(vendor="gemini", model="auto"),
                runtime_root=Path(tmp_dir) / "runtime",
            )
            not_ready_visible = """
Gemini CLI v0.37.1
Waiting for authentication... (Press Esc or Ctrl+C to cancel)
"""
            ready_visible = """
? for shortcuts
YOLO Ctrl+Y
*   Type your message or @path/to/file
workspace (/directory)
"""
            self.assertFalse(worker._visible_indicates_agent_ready(not_ready_visible))
            self.assertTrue(worker._visible_indicates_agent_ready(ready_visible))

    def test_qwen_ready_detection_requires_input_box_marker(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            worker = TmuxBatchWorker(
                worker_id="qwen-worker",
                work_dir=tmp_dir,
                config=AgentRunConfig(vendor="qwen", model="qwen3-coder"),
                runtime_root=Path(tmp_dir) / "runtime",
            )
            not_ready_visible = """
Qwen OAuth | coder-model (/model to change)
~/Desktop/KevinGit/PyFinance/MetricsFactory
"""
            ready_visible = """
────────────────────────────────────────────────────────────────────────────────
*   输入您的消息或 @ 文件路径
────────────────────────────────────────────────────────────────────────────────
  YOLO 模式 (shift + tab 切换)
"""
            self.assertFalse(worker._visible_indicates_agent_ready(not_ready_visible))
            self.assertTrue(worker._visible_indicates_agent_ready(ready_visible))

    def test_codex_ready_detection_rejects_prompt_marker_during_mcp_boot(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            worker = TmuxBatchWorker(
                worker_id="codex-worker",
                work_dir=tmp_dir,
                config=AgentRunConfig(vendor="codex", model="gpt-5.4-mini"),
                runtime_root=Path(tmp_dir) / "runtime",
            )
            not_ready_visible = """
• Starting MCP servers (0/2): ossinsight, playwright (0s • esc to interrupt)
› Find and fix a bug in @filename
"""
            ready_visible = """
› Find and fix a bug in @filename
  gpt-5.4-mini high · ~/Desktop/KevinGit/My_C_Tools
"""
            self.assertTrue(worker._visible_indicates_agent_starting(not_ready_visible))
            self.assertFalse(worker._visible_indicates_agent_ready(not_ready_visible))
            self.assertTrue(worker._visible_indicates_agent_ready(ready_visible))

    def test_codex_ready_detection_accepts_input_box_with_update_banner(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            worker = TmuxBatchWorker(
                worker_id="codex-worker",
                work_dir=tmp_dir,
                config=AgentRunConfig(vendor="codex", model="gpt-5.4"),
                runtime_root=Path(tmp_dir) / "runtime",
            )
            visible = """
╭─────────────────────────────────────────────────╮
│ ✨ Update available! 0.120.0 -> 0.121.0         │
╰─────────────────────────────────────────────────╯

› Run /review on my current changes
  gpt-5.4 high · ~/Desktop/KevinGit/My_C_Tools
"""
            self.assertFalse(worker._visible_indicates_agent_starting(visible))
            self.assertTrue(worker._visible_indicates_agent_ready(visible))

    def test_gemini_ready_detection_rejects_trust_prompt(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            worker = TmuxBatchWorker(
                worker_id="gemini-worker",
                work_dir=tmp_dir,
                config=AgentRunConfig(vendor="gemini", model="flash"),
                runtime_root=Path(tmp_dir) / "runtime",
            )
            trust_visible = """
Do you trust the files in this folder?
1. Trust folder
2. Trust parent folder
3. Don't trust
"""
            self.assertFalse(worker._visible_indicates_agent_ready(trust_visible))

    def test_kimi_ready_detection_rejects_login_required_prompt(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            worker = TmuxBatchWorker(
                worker_id="kimi-worker",
                work_dir=tmp_dir,
                config=AgentRunConfig(vendor="kimi", model="kimi-k2-turbo"),
                runtime_root=Path(tmp_dir) / "runtime",
            )
            login_required_visible = """
Model: not set, send /login to login
LLM not set, send "/login" to login
> 
"""
            self.assertFalse(worker._visible_indicates_agent_ready(login_required_visible))

    def test_opencode_ready_detection_accepts_input_prompt_and_completed_footer(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            worker = TmuxBatchWorker(
                worker_id="opencode-worker",
                work_dir=tmp_dir,
                config=AgentRunConfig(vendor="opencode", model="default"),
                runtime_root=Path(tmp_dir) / "runtime",
            )
            booting_visible = """
Performing one time database migration...
Database migration complete.
"""
            waiting_visible = """
Ask anything... "Fix a TODO in the codebase"
tab agents  ctrl+p commands
"""
            completed_visible = """
Created hello.txt with the content hi in /private/tmp/opencode-permtest.NOSf72/.

10.4K  ctrl+p commands
"""
            busy_visible = """
Thinking: The file has been created successfully.
■■■⬝⬝⬝⬝⬝  esc interrupt                         tab agents  ctrl+p commands
"""
            self.assertTrue(worker._visible_indicates_agent_starting(booting_visible))
            self.assertFalse(worker._visible_indicates_agent_ready(booting_visible))
            self.assertTrue(worker._visible_indicates_agent_ready(waiting_visible))
            self.assertTrue(worker._visible_indicates_agent_ready(completed_visible))
            self.assertFalse(worker._visible_indicates_agent_ready(busy_visible))

    def test_codex_title_detection_accepts_work_dir_basename(self):
        class CodexStateWorker(TmuxBatchWorker):
            def target_exists(self, target=None):
                return True

        with tempfile.TemporaryDirectory(prefix="canopy-api-v3-") as tmp_dir:
            work_dir = Path(tmp_dir) / "canopy-api-v3"
            work_dir.mkdir()
            worker = CodexStateWorker(
                worker_id="codex-title-worker",
                work_dir=work_dir,
                config=AgentRunConfig(vendor="codex", model="gpt-5.4-mini"),
                runtime_root=Path(tmp_dir) / "runtime",
            )
            worker.pane_id = "%1"
            ready_observation = WorkerObservation(
                visible_text="› Continue with the current task",
                raw_log_delta="",
                raw_log_tail="› Continue with the current task",
                current_command="node",
                current_path=str(work_dir),
                pane_dead=False,
                session_exists=True,
                log_mtime=0.0,
                observed_at="2026-04-21T00:00:00",
                pane_title="canopy-api-v3",
            )
            busy_observation = WorkerObservation(
                visible_text="› Continue with the current task",
                raw_log_delta="",
                raw_log_tail="› Continue with the current task",
                current_command="node",
                current_path=str(work_dir),
                pane_dead=False,
                session_exists=True,
                log_mtime=0.0,
                observed_at="2026-04-21T00:00:01",
                pane_title="⠋ canopy-api-v3",
            )
            shell_observation = WorkerObservation(
                visible_text="",
                raw_log_delta="",
                raw_log_tail="",
                current_command="zsh",
                current_path=str(work_dir),
                pane_dead=False,
                session_exists=True,
                log_mtime=0.0,
                observed_at="2026-04-21T00:00:02",
                pane_title="canopy-api-v3",
            )

            self.assertTrue(worker._title_indicates_ready("canopy-api-v3"))
            self.assertTrue(worker._title_indicates_busy("⠋ canopy-api-v3"))

            worker.agent_started = False
            self.assertEqual(worker.get_agent_state(ready_observation), AgentRuntimeState.STARTING)

            worker.agent_started = True
            self.assertEqual(worker.get_agent_readiness(ready_observation), AgentRuntimeState.READY.value)
            self.assertEqual(worker.get_agent_state(ready_observation), AgentRuntimeState.READY)
            self.assertEqual(worker.get_agent_readiness(busy_observation), AgentRuntimeState.BUSY.value)
            self.assertEqual(worker.get_agent_state(busy_observation), AgentRuntimeState.BUSY)
            self.assertEqual(worker.get_agent_state(shell_observation), AgentRuntimeState.DEAD)

    def test_wrapper_state_maps_not_ready_and_ready(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            worker = TmuxBatchWorker(
                worker_id="wrapper-state-worker",
                work_dir=tmp_dir,
                config=AgentRunConfig(vendor="codex", model="gpt-5.4-mini"),
                runtime_root=Path(tmp_dir) / "runtime",
            )
            worker.provider_phase = ProviderPhase.BOOTING
            worker.agent_ready = False
            self.assertEqual(
                worker._infer_wrapper_state(
                    current_command="codex",
                    visible_text="Starting MCP servers (0/2): ossinsight, playwright",
                ),
                WrapperState.NOT_READY,
            )
            worker.provider_phase = ProviderPhase.PROCESSING
            worker.agent_ready = True
            self.assertEqual(
                worker._infer_wrapper_state(
                    current_command="codex",
                    visible_text="Thinking...",
                ),
                WrapperState.NOT_READY,
            )
            worker.provider_phase = ProviderPhase.WAITING_INPUT
            worker.agent_ready = True
            worker.agent_started = True
            worker.last_pane_title = "AutoCodex"
            self.assertEqual(
                worker._infer_wrapper_state(
                    current_command="codex",
                    visible_text="› Find and fix a bug in @filename",
                ),
                WrapperState.READY,
            )

    def test_wrapper_state_keeps_pre_ready_processing_in_not_ready(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            worker = TmuxBatchWorker(
                worker_id="pre-ready-processing-worker",
                work_dir=tmp_dir,
                config=AgentRunConfig(vendor="codex", model="gpt-5.4-mini"),
                runtime_root=Path(tmp_dir) / "runtime",
            )
            worker.provider_phase = ProviderPhase.PROCESSING
            worker.agent_ready = False
            self.assertEqual(
                worker._infer_wrapper_state(
                    current_command="codex",
                    visible_text="",
                ),
                WrapperState.NOT_READY,
            )

    def test_wrapper_state_keeps_codex_and_gemini_startup_pages_in_not_ready(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            codex_worker = TmuxBatchWorker(
                worker_id="codex-wrapper-state-worker",
                work_dir=tmp_dir,
                config=AgentRunConfig(vendor="codex", model="gpt-5.4-mini"),
                runtime_root=Path(tmp_dir) / "runtime-codex",
            )
            codex_worker.provider_phase = ProviderPhase.WAITING_INPUT
            codex_worker.agent_ready = False
            self.assertEqual(
                codex_worker._infer_wrapper_state(
                    current_command="codex",
                    visible_text="• Starting MCP servers (0/2): ossinsight, playwright\n› Find and fix a bug in @filename",
                ),
                WrapperState.NOT_READY,
            )

            gemini_worker = TmuxBatchWorker(
                worker_id="gemini-wrapper-state-worker",
                work_dir=tmp_dir,
                config=AgentRunConfig(vendor="gemini", model="flash"),
                runtime_root=Path(tmp_dir) / "runtime-gemini",
            )
            gemini_worker.provider_phase = ProviderPhase.AUTH_PROMPT
            gemini_worker.agent_ready = True
            self.assertEqual(
                gemini_worker._infer_wrapper_state(
                    current_command="gemini",
                    visible_text="Waiting for authentication... (Press Esc or Ctrl+C to cancel)",
                ),
                WrapperState.NOT_READY,
            )

    def test_wrapper_state_uses_recent_terminal_hash_changes_as_not_ready_signal(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            worker = TmuxBatchWorker(
                worker_id="activity-worker",
                work_dir=tmp_dir,
                config=AgentRunConfig(vendor="claude", model="haiku"),
                runtime_root=Path(tmp_dir) / "runtime",
            )
            worker.provider_phase = ProviderPhase.WAITING_INPUT
            worker.agent_ready = True
            worker._update_terminal_activity("frame-1", observed_at="2026-04-15T00:00:00")
            self.assertEqual(
                worker._infer_wrapper_state(
                    current_command="claude",
                    visible_text="❯",
                ),
                WrapperState.NOT_READY,
            )

    def test_wrapper_state_returns_ready_after_terminal_hash_stabilizes(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            worker = TmuxBatchWorker(
                worker_id="stable-activity-worker",
                work_dir=tmp_dir,
                config=AgentRunConfig(vendor="claude", model="haiku"),
                runtime_root=Path(tmp_dir) / "runtime",
            )
            worker.provider_phase = ProviderPhase.WAITING_INPUT
            worker.agent_ready = True
            worker.agent_started = True
            worker.last_pane_title = "✳ Claude Code"
            worker._update_terminal_activity("frame-1", observed_at="2026-04-15T00:00:00")
            worker._last_terminal_change_monotonic = time.monotonic() - TERMINAL_ACTIVITY_IDLE_WINDOW_SEC - 0.1
            worker.terminal_recently_changed = False
            self.assertEqual(
                worker._infer_wrapper_state(
                    current_command="claude",
                    visible_text="❯",
                ),
                WrapperState.READY,
            )

    def test_gemini_output_detector_ignores_footer_and_keeps_protocol_tokens(self):
        output = """
✦ Completed the routing layer generation.

[[ACX_TURN:demo1234:DONE]]
[[ROUTING_CREATE:DONE]]

? for shortcuts
YOLO Ctrl+Y
▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀
*   Type your message or @path/to/file
▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄
workspace (/directory)                                                     branch                          sandbox                                             /model
~/Desktop/KevinGit/PyFinance/ReturnClassification                          main                            no sandbox                          gemini-3-flash-preview
"""
        message = GeminiOutputDetector().extract_last_message(output)
        self.assertIn("[[ACX_TURN:demo1234:DONE]]", message)
        self.assertIn("[[ROUTING_CREATE:DONE]]", message)
        self.assertNotIn("Type your message or @path/to/file", message)

    def test_reasoning_note_for_kimi_uses_thinking_toggle(self):
        self.assertIn("kimi_thinking=off", build_reasoning_note(Vendor.KIMI, "low"))
        self.assertIn("kimi_thinking=on", build_reasoning_note(Vendor.KIMI, "high"))

    def test_provider_detectors_classify_vendor_prompts(self):
        gemini_phase = GeminiOutputDetector().classify_phase(
            WorkerObservation(
                visible_text="Waiting for authentication... (Press Esc or Ctrl+C to cancel)",
                raw_log_delta="",
                raw_log_tail="Waiting for authentication... (Press Esc or Ctrl+C to cancel)",
                current_command="gemini",
                current_path="/tmp/project",
                pane_dead=False,
                session_exists=True,
                log_mtime=0.0,
                observed_at="2026-04-12T00:00:00",
            )
        )
        self.assertEqual(gemini_phase, ProviderPhase.AUTH_PROMPT)

        gemini_trust_phase = GeminiOutputDetector().classify_phase(
            WorkerObservation(
                visible_text="Do you trust the files in this folder?\n1. Trust folder\n2. Trust parent folder\n3. Don't trust",
                raw_log_delta="",
                raw_log_tail="Do you trust the files in this folder?\n1. Trust folder\n2. Trust parent folder\n3. Don't trust",
                current_command="gemini",
                current_path="/tmp/project",
                pane_dead=False,
                session_exists=True,
                log_mtime=0.0,
                observed_at="2026-04-12T00:00:00",
            )
        )
        self.assertEqual(gemini_trust_phase, ProviderPhase.AUTH_PROMPT)

        gemini_ready_over_stale_auth_phase = GeminiOutputDetector().classify_phase(
            WorkerObservation(
                visible_text="*   Type your message or @path/to/file\nworkspace (/directory)",
                raw_log_delta="",
                raw_log_tail="Waiting for authentication... (Press Esc or Ctrl+C to cancel)\n*   Type your message or @path/to/file\nworkspace (/directory)",
                current_command="gemini",
                current_path="/tmp/project",
                pane_dead=False,
                session_exists=True,
                log_mtime=0.0,
                observed_at="2026-04-12T00:00:00",
            )
        )
        self.assertEqual(gemini_ready_over_stale_auth_phase, ProviderPhase.WAITING_INPUT)

        gemini_processing_over_stale_auth_phase = GeminiOutputDetector().classify_phase(
            WorkerObservation(
                visible_text="Working… (My_C_Tools)",
                raw_log_delta="",
                raw_log_tail="Waiting for authentication... (Press Esc or Ctrl+C to cancel)\nWorking… (My_C_Tools)",
                current_command="gemini",
                current_path="/tmp/project",
                pane_dead=False,
                session_exists=True,
                log_mtime=0.0,
                observed_at="2026-04-12T00:00:00",
            )
        )
        self.assertEqual(gemini_processing_over_stale_auth_phase, ProviderPhase.PROCESSING)

        codex_phase = CodexOutputDetector().classify_phase(
            WorkerObservation(
                visible_text="Update available!\nUpdate now\nSkip until next version\nPress enter to continue",
                raw_log_delta="",
                raw_log_tail="Update available!\nUpdate now\nSkip until next version\nPress enter to continue",
                current_command="codex",
                current_path="/tmp/project",
                pane_dead=False,
                session_exists=True,
                log_mtime=0.0,
                observed_at="2026-04-12T00:00:00",
            )
        )
        self.assertEqual(codex_phase, ProviderPhase.UPDATE_PROMPT)

        codex_waiting_phase = CodexOutputDetector().classify_phase(
            WorkerObservation(
                visible_text="› Find and fix a bug in @filename\n  gpt-5.4-mini high · ~/project",
                raw_log_delta="",
                raw_log_tail="› Find and fix a bug in @filename\n  gpt-5.4-mini high · ~/project",
                current_command="codex",
                current_path="/tmp/project",
                pane_dead=False,
                session_exists=True,
                log_mtime=0.0,
                observed_at="2026-04-12T00:00:00",
            )
        )
        self.assertEqual(codex_waiting_phase, ProviderPhase.WAITING_INPUT)

        codex_waiting_with_update_banner_phase = CodexOutputDetector().classify_phase(
            WorkerObservation(
                visible_text="Update available!\n› Run /review on my current changes\n  gpt-5.4 high · ~/project",
                raw_log_delta="",
                raw_log_tail="Update available!\n› Run /review on my current changes\n  gpt-5.4 high · ~/project",
                current_command="codex",
                current_path="/tmp/project",
                pane_dead=False,
                session_exists=True,
                log_mtime=0.0,
                observed_at="2026-04-12T00:00:00",
            )
        )
        self.assertEqual(codex_waiting_with_update_banner_phase, ProviderPhase.WAITING_INPUT)

        codex_booting_phase = CodexOutputDetector().classify_phase(
            WorkerObservation(
                visible_text="• Starting MCP servers (0/2): ossinsight, playwright (0s • esc to interrupt)\n› Find and fix a bug in @filename",
                raw_log_delta="",
                raw_log_tail="• Starting MCP servers (0/2): ossinsight, playwright (0s • esc to interrupt)\n› Find and fix a bug in @filename",
                current_command="codex",
                current_path="/tmp/project",
                pane_dead=False,
                session_exists=True,
                log_mtime=0.0,
                observed_at="2026-04-12T00:00:00",
            )
        )
        self.assertEqual(codex_booting_phase, ProviderPhase.BOOTING)

        codex_processing_phase = CodexOutputDetector().classify_phase(
            WorkerObservation(
                visible_text="• Identifying C++ files and tests (25s • esc to interrupt)\n› Run /review on my current changes",
                raw_log_delta="",
                raw_log_tail="• Identifying C++ files and tests (25s • esc to interrupt)\n› Run /review on my current changes",
                current_command="codex",
                current_path="/tmp/project",
                pane_dead=False,
                session_exists=True,
                log_mtime=0.0,
                observed_at="2026-04-12T00:00:00",
            )
        )
        self.assertEqual(codex_processing_phase, ProviderPhase.PROCESSING)

        claude_phase = ClaudeOutputDetector().classify_phase(
            WorkerObservation(
                visible_text="❯",
                raw_log_delta="",
                raw_log_tail="❯",
                current_command="claude",
                current_path="/tmp/project",
                pane_dead=False,
                session_exists=True,
                log_mtime=0.0,
                observed_at="2026-04-12T00:00:00",
            )
        )
        self.assertEqual(claude_phase, ProviderPhase.WAITING_INPUT)

        kimi_auth_phase = KimiOutputDetector().classify_phase(
            WorkerObservation(
                visible_text='Model: not set, send /login to login\nLLM not set, send "/login" to login\n>',
                raw_log_delta="",
                raw_log_tail='Model: not set, send /login to login\nLLM not set, send "/login" to login\n>',
                current_command="kimi",
                current_path="/tmp/project",
                pane_dead=False,
                session_exists=True,
                log_mtime=0.0,
                observed_at="2026-04-12T00:00:00",
            )
        )
        self.assertEqual(kimi_auth_phase, ProviderPhase.AUTH_PROMPT)

        kimi_update_phase = KimiOutputDetector().classify_phase(
            WorkerObservation(
                visible_text="kimi-cli update available\n[Enter] Upgrade now\n[q] Not now, remind me next time\n[s] Skip reminders for version 1.34.0",
                raw_log_delta="",
                raw_log_tail="kimi-cli update available\n[Enter] Upgrade now\n[q] Not now, remind me next time\n[s] Skip reminders for version 1.34.0",
                current_command="kimi",
                current_path="/tmp/project",
                pane_dead=False,
                session_exists=True,
                log_mtime=0.0,
                observed_at="2026-04-12T00:00:00",
            )
        )
        self.assertEqual(kimi_update_phase, ProviderPhase.UPDATE_PROMPT)

        qwen_phase = QwenOutputDetector().classify_phase(
            WorkerObservation(
                visible_text="输入您的消息或 @ 文件路径\nYOLO 模式 (shift + tab 切换)",
                raw_log_delta="",
                raw_log_tail="输入您的消息或 @ 文件路径\nYOLO 模式 (shift + tab 切换)",
                current_command="node",
                current_path="/tmp/project",
                pane_dead=False,
                session_exists=True,
                log_mtime=0.0,
                observed_at="2026-04-12T00:00:00",
            )
        )
        self.assertEqual(qwen_phase, ProviderPhase.WAITING_INPUT)

    def test_build_launch_command_variants_include_expected_flags(self):
        work_dir = Path("/tmp/project")

        codex_cmd = AgentRunConfig(vendor=Vendor.CODEX, model="gpt-5.4", reasoning_effort="xhigh").build_launch_command(work_dir)
        self.assertIn("codex --model", codex_cmd)
        self.assertIn("--cd /tmp/project", codex_cmd)

        claude_cmd = AgentRunConfig(vendor=Vendor.CLAUDE, model="sonnet", reasoning_effort="max").build_launch_command(work_dir)
        self.assertIn("claude --model", claude_cmd)
        self.assertIn("--effort max", claude_cmd)

        gemini_cmd = AgentRunConfig(vendor=Vendor.GEMINI, model="auto", reasoning_effort="medium").build_launch_command(work_dir)
        self.assertIn("gemini --model flash", gemini_cmd)
        self.assertIn("--model flash", gemini_cmd)

        qwen_cmd = AgentRunConfig(
            vendor=Vendor.QWEN,
            model="qwen3-coder",
            reasoning_effort="high",
            proxy_url="7890",
        ).build_launch_command(work_dir)
        self.assertIn("qwen --model qwen3-coder", qwen_cmd)
        self.assertIn("--proxy http://127.0.0.1:7890", qwen_cmd)

        kimi_cmd = AgentRunConfig(vendor=Vendor.KIMI, model="kimi-k2", reasoning_effort="low").build_launch_command(work_dir)
        self.assertIn("kimi --work-dir /tmp/project", kimi_cmd)
        self.assertIn("--no-thinking", kimi_cmd)

        opencode_default_cmd = AgentRunConfig(vendor=Vendor.OPENCODE, model="default").build_launch_command(work_dir)
        self.assertIn("opencode /tmp/project --pure", opencode_default_cmd)
        self.assertIn(f"--model {get_default_model_for_vendor('opencode')}", opencode_default_cmd)

        mapped_opencode_model = next(
            item.model_id
            for item in get_model_choices("opencode")
            if item.reasoning.reasoning_control_mode == "mapped"
        )
        opencode_model_cmd = AgentRunConfig(
            vendor=Vendor.OPENCODE,
            model=mapped_opencode_model,
            reasoning_effort="max",
        ).build_launch_command(work_dir)
        self.assertIn(f"opencode /tmp/project --pure --model {mapped_opencode_model}", opencode_model_cmd)
        self.assertIn("--variant", opencode_model_cmd)

    def test_build_session_name_is_stable_and_bounded(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            work_dir = Path(tmp_dir)
            left = build_session_name("owner-worker", work_dir, Vendor.CODEX)
            right = build_session_name("owner-worker", work_dir, Vendor.CODEX)
            self.assertEqual(left, right)
            self.assertTrue(left.startswith("执行者-"))
            self.assertNotRegex(left, r"[\s/\\\\]")
            self.assertLessEqual(len(left), 60)

    def test_build_session_name_advances_when_preferred_name_is_occupied(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            work_dir = Path(tmp_dir)
            preferred = build_session_name("requirements-analyst", work_dir, Vendor.CODEX)
            fallback = build_session_name(
                "requirements-analyst",
                work_dir,
                Vendor.CODEX,
                occupied_session_names=[preferred],
            )
            self.assertTrue(preferred.startswith("分析师-"))
            self.assertTrue(fallback.startswith("分析师-"))
            self.assertNotEqual(preferred, fallback)

    def test_build_session_name_maps_routing_and_reviewer_roles(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            work_dir = Path(tmp_dir) / "repo-map"
            work_dir.mkdir()
            routing_session = build_session_name(work_dir.name, work_dir, Vendor.CODEX)
            review_ba_session = build_session_name("requirements-review-analyst", work_dir, Vendor.CODEX)
            reviewer_session = build_session_name("requirements-review-r2", work_dir, Vendor.CODEX)
            design_ba_session = build_session_name("detailed-design-analyst", work_dir, Vendor.CODEX)
            design_reviewer_session = build_session_name("detailed-design-review-开发工程师", work_dir, Vendor.CODEX)
            task_split_ba_session = build_session_name("task-split-analyst", work_dir, Vendor.CODEX)
            task_split_reviewer_session = build_session_name("task-split-review-开发工程师", work_dir, Vendor.CODEX)
            development_worker_session = build_session_name("development-developer", work_dir, Vendor.CODEX)
            development_reviewer_session = build_session_name("development-review-测试工程师", work_dir, Vendor.CODEX)
            self.assertTrue(routing_session.startswith("路由器-"))
            self.assertTrue(review_ba_session.startswith("需求分析师-"))
            self.assertTrue(reviewer_session.startswith("审核器-"))
            self.assertTrue(design_ba_session.startswith("需求分析师-"))
            self.assertTrue(design_reviewer_session.startswith("开发工程师-"))
            self.assertTrue(task_split_ba_session.startswith("需求分析师-"))
            self.assertTrue(task_split_reviewer_session.startswith("开发工程师-"))
            self.assertTrue(development_worker_session.startswith("开发工程师-"))
            self.assertTrue(development_reviewer_session.startswith("测试工程师-"))
            self.assertNotIn("-R2-", reviewer_session)

    def test_detailed_design_reviewer_worker_preserves_raw_worker_id_for_session_mapping(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            worker = TmuxBatchWorker(
                worker_id="detailed-design-review-开发工程师",
                work_dir=tmp_dir,
                config=AgentRunConfig(vendor="codex", model="gpt-5.4-mini"),
                runtime_root=Path(tmp_dir) / "runtime",
            )

        self.assertEqual(worker.worker_id, "detailed-design-review-开发工程师")
        self.assertTrue(worker.runtime_dir.name.startswith("detailed-design-review-"))
        self.assertTrue(worker.session_name.startswith("开发工程师-"))

    def test_load_worker_from_state_path_restores_detailed_design_reviewer_role_mapping(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            worker = TmuxBatchWorker(
                worker_id="detailed-design-review-开发工程师",
                work_dir=tmp_dir,
                config=AgentRunConfig(vendor="codex", model="gpt-5.4-mini"),
                runtime_root=Path(tmp_dir) / "runtime",
            )
            worker._write_state(WorkerStatus.READY, note="saved")  # noqa: SLF001
            restored = load_worker_from_state_path(worker.state_path)

        self.assertIsNotNone(restored)
        self.assertEqual(restored.worker_id, "detailed-design-review-开发工程师")
        self.assertTrue(restored.session_name.startswith("开发工程师-"))

    def test_task_split_reviewer_worker_preserves_raw_worker_id_for_session_mapping(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            worker = TmuxBatchWorker(
                worker_id="task-split-review-开发工程师",
                work_dir=tmp_dir,
                config=AgentRunConfig(vendor="codex", model="gpt-5.4-mini"),
                runtime_root=Path(tmp_dir) / "runtime",
            )

        self.assertEqual(worker.worker_id, "task-split-review-开发工程师")
        self.assertTrue(worker.runtime_dir.name.startswith("task-split-review-"))
        self.assertTrue(worker.session_name.startswith("开发工程师-"))

    def test_load_worker_from_state_path_restores_task_split_reviewer_role_mapping(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            worker = TmuxBatchWorker(
                worker_id="task-split-review-开发工程师",
                work_dir=tmp_dir,
                config=AgentRunConfig(vendor="codex", model="gpt-5.4-mini"),
                runtime_root=Path(tmp_dir) / "runtime",
            )
            worker._write_state(WorkerStatus.READY, note="saved")  # noqa: SLF001
            restored = load_worker_from_state_path(worker.state_path)

        self.assertIsNotNone(restored)
        self.assertEqual(restored.worker_id, "task-split-review-开发工程师")
        self.assertTrue(restored.session_name.startswith("开发工程师-"))

    def test_development_worker_preserves_raw_worker_id_for_session_mapping(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            worker = TmuxBatchWorker(
                worker_id="development-developer",
                work_dir=tmp_dir,
                config=AgentRunConfig(vendor="codex", model="gpt-5.4-mini"),
                runtime_root=Path(tmp_dir) / "runtime",
            )

        self.assertEqual(worker.worker_id, "development-developer")
        self.assertTrue(worker.runtime_dir.name.startswith("development-developer"))
        self.assertTrue(worker.session_name.startswith("开发工程师-"))

    def test_development_reviewer_worker_preserves_raw_worker_id_for_session_mapping(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            worker = TmuxBatchWorker(
                worker_id="development-review-测试工程师",
                work_dir=tmp_dir,
                config=AgentRunConfig(vendor="codex", model="gpt-5.4-mini"),
                runtime_root=Path(tmp_dir) / "runtime",
            )

        self.assertEqual(worker.worker_id, "development-review-测试工程师")
        self.assertTrue(worker.runtime_dir.name.startswith("development-review-"))
        self.assertTrue(worker.session_name.startswith("测试工程师-"))

    def test_load_worker_from_state_path_restores_development_reviewer_role_mapping(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            worker = TmuxBatchWorker(
                worker_id="development-review-测试工程师",
                work_dir=tmp_dir,
                config=AgentRunConfig(vendor="codex", model="gpt-5.4-mini"),
                runtime_root=Path(tmp_dir) / "runtime",
            )
            worker._write_state(WorkerStatus.READY, note="saved")  # noqa: SLF001
            restored = load_worker_from_state_path(worker.state_path)

        self.assertIsNotNone(restored)
        self.assertEqual(restored.worker_id, "development-review-测试工程师")
        self.assertTrue(restored.session_name.startswith("测试工程师-"))

    def test_write_state_notifies_runtime_state_change(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            worker = TmuxBatchWorker(
                worker_id="reviewer-worker",
                work_dir=tmp_dir,
                config=AgentRunConfig(vendor="codex", model="gpt-5.4-mini"),
                runtime_root=Path(tmp_dir) / "runtime",
            )
            with mock.patch("T02_tmux_agents._notify_runtime_state_changed_best_effort") as notifier:
                worker._write_state(WorkerStatus.RUNNING, note="test")  # noqa: SLF001
        notifier.assert_called_once_with()

    def test_build_session_name_maps_routing_role_for_non_ascii_work_dir(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            work_dir = Path(tmp_dir) / "测试项目"
            work_dir.mkdir()
            routing_session = build_session_name("测试项目", work_dir, Vendor.CODEX)
            self.assertTrue(routing_session.startswith("路由器-"))

    def test_extract_final_protocol_token_requires_final_line(self):
        self.assertEqual(
            extract_final_protocol_token("line\n[[ROUTING_CREATE:DONE]]", ["[[ROUTING_CREATE:DONE]]"]),
            "[[ROUTING_CREATE:DONE]]",
        )
        self.assertEqual(
            extract_final_protocol_token("line\n⏺ [[ROUTING_CREATE:DONE]]", ["[[ROUTING_CREATE:DONE]]"]),
            "[[ROUTING_CREATE:DONE]]",
        )
        self.assertEqual(
            extract_final_protocol_token("line\n✦ [[ROUTING_CREATE:DONE]]", ["[[ROUTING_CREATE:DONE]]"]),
            "[[ROUTING_CREATE:DONE]]",
        )
        self.assertEqual(
            extract_final_protocol_token("line\n[[ROUTING_CREATE:DONE]]\nextra", ["[[ROUTING_CREATE:DONE]]"]),
            "",
        )
        self.assertEqual(
            extract_final_protocol_token(
                "line\n[[ROUTING_CREATE:DONE]]\n✢ Improvising… (38s · ↓ 1.4k tokens)\n❯",
                ["[[ROUTING_CREATE:DONE]]"],
            ),
            "[[ROUTING_CREATE:DONE]]",
        )
        self.assertEqual(
            extract_final_protocol_token(
                "line\n[[ROUTING_AUDIT:WRITTEN]]\n*   输入您的消息或 @ 文件路径",
                ["[[ROUTING_AUDIT:WRITTEN]]"],
            ),
            "[[ROUTING_AUDIT:WRITTEN]]",
        )

    def test_run_turn_timeout_records_failed_state(self):
        class TimeoutWorker(TmuxBatchWorker):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.state_notes = []

            def _write_state(self, status, *, note, extra=None):
                self.state_notes.append((status.value, note, extra or {}))

            def _append_transcript(self, title, body):
                return None

            def ensure_agent_ready(self, timeout_sec=60.0):
                self.pane_id = "%1"
                self.agent_ready = True
                return None

            def target_exists(self, target=None):
                return True

            def capture_visible(self, tail_lines=500):
                return "fake-visible"

            def _send_text(self, text, enter_count=None):
                return None

            def _wait_for_turn_reply(self, **kwargs):
                raise TimeoutError("timed out")

        with tempfile.TemporaryDirectory() as tmp_dir:
            worker = TimeoutWorker(
                worker_id="calc-worker",
                work_dir=tmp_dir,
                config=AgentRunConfig(vendor="codex", model="gpt-5"),
                runtime_root=Path(tmp_dir) / "runtime",
            )
            result = worker.run_turn(label="timeout_case", prompt="hello", required_tokens=["[[DONE]]"], timeout_sec=0.05)
            self.assertFalse(result.ok)
            self.assertEqual(worker.results[-1].exit_code, TIMEOUT_EXIT_CODE)
            self.assertEqual(worker.state_notes[-1][0], "failed")
            self.assertTrue(worker.state_notes[-1][1].startswith("timeout:"))

    def test_run_turn_restores_running_state_after_agent_ready(self):
        class SubmitStateWorker(TmuxBatchWorker):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.state_notes = []

            def _write_state(self, status, *, note, extra=None):
                self.state_notes.append((status.value, note, extra or {}))

            def _append_transcript(self, title, body):
                return None

            def ensure_agent_ready(self, timeout_sec=60.0):
                self.pane_id = "%1"
                self.agent_ready = True
                self._write_state(type("Status", (), {"value": "ready"})(), note="agent_ready", extra={})

            def target_exists(self, target=None):
                return True

            def capture_visible(self, tail_lines=500):
                return "fake-visible"

            def _send_text(self, text, enter_count=None):
                assert self.state_notes[-1][0] == "running"
                assert self.state_notes[-1][1].startswith("turn:")
                assert self.current_task_status_path
                payload = json.loads(Path(self.current_task_status_path).read_text(encoding="utf-8"))
                assert payload == {"status": "running"}
                assert self.current_task_status_path not in text

            def _wait_for_turn_reply(self, **kwargs):
                return "ok\n[[DONE]]"

        with tempfile.TemporaryDirectory() as tmp_dir:
            worker = SubmitStateWorker(
                worker_id="calc-worker",
                work_dir=tmp_dir,
                config=AgentRunConfig(vendor="codex", model="gpt-5"),
                runtime_root=Path(tmp_dir) / "runtime",
            )
            result = worker.run_turn(label="submit_case", prompt="hello", required_tokens=["[[DONE]]"], timeout_sec=0.05)
            self.assertTrue(result.ok)
            running_notes = [item for item in worker.state_notes if item[0] == "running"]
            self.assertGreaterEqual(len(running_notes), 2)

    def test_run_turn_marks_wrapper_ready_after_success(self):
        class SubmitStateWorker(TmuxBatchWorker):
            def _append_transcript(self, title, body):
                return None

            def ensure_agent_ready(self, timeout_sec=60.0):
                self.pane_id = "%1"
                self.agent_ready = True
                self.agent_started = True
                self.provider_phase = ProviderPhase.WAITING_INPUT
                self.last_pane_title = "AutoCodex"
                self.current_command = "codex"
                self.current_path = str(self.work_dir)

            def target_exists(self, target=None):
                return True

            def capture_visible(self, tail_lines=500):
                return "fake-visible"

            def observe(self, *, tail_lines=500, tail_bytes=24000):
                self.provider_phase = ProviderPhase.WAITING_INPUT
                return WorkerObservation(
                    visible_text="❯",
                    raw_log_delta="",
                    raw_log_tail="❯",
                    current_command="codex",
                    current_path=str(self.work_dir),
                    pane_dead=False,
                    session_exists=True,
                    log_mtime=0.0,
                    observed_at="2026-04-15T00:00:00",
                    pane_title="AutoCodex",
                )

            def _send_text(self, text, enter_count=None):
                write_task_status(self.current_task_status_path, status="done")

            def _wait_for_turn_reply(self, **kwargs):
                return "ok\n[[DONE]]"

        with tempfile.TemporaryDirectory() as tmp_dir:
            worker = SubmitStateWorker(
                worker_id="success-ready-worker",
                work_dir=tmp_dir,
                config=AgentRunConfig(vendor="codex", model="gpt-5"),
                runtime_root=Path(tmp_dir) / "runtime",
            )
            result = worker.run_turn(label="submit_case", prompt="hello", required_tokens=["[[DONE]]"], timeout_sec=0.05)
            self.assertTrue(result.ok)
            self.assertEqual(worker.wrapper_state, WrapperState.READY)

    def test_wait_for_turn_artifacts_returns_after_stable_file_validation(self):
        class FileContractWorker(TmuxBatchWorker):
            def target_exists(self, target=None):
                return True

            def observe(self, *, tail_lines=500, tail_bytes=24000):
                return WorkerObservation(
                    visible_text="processing",
                    raw_log_delta="delta",
                    raw_log_tail="tail",
                    current_command="codex",
                    current_path=str(self.work_dir),
                    pane_dead=False,
                    session_exists=True,
                    log_mtime=0.0,
                    observed_at="2026-04-12T00:00:00",
                )

            def capture_visible(self, tail_lines=500):
                return "visible"

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            artifact_path = root / "artifact.txt"
            artifact_path.write_text("artifact-body", encoding="utf-8")
            status_path = root / "turn_status.json"
            status_path.write_text(json.dumps({"ok": True}), encoding="utf-8")

            def validator(path: Path) -> TurnFileResult:
                payload = json.loads(path.read_text(encoding="utf-8"))
                if not payload.get("ok"):
                    raise ValueError("not ready")
                return TurnFileResult(
                    status_path=str(path),
                    payload=payload,
                    artifact_paths={"artifact.txt": str(artifact_path)},
                    artifact_hashes={"artifact.txt": "sha256:test"},
                    validated_at="2026-04-12T00:00:00",
                )

            worker = FileContractWorker(
                worker_id="file-contract-worker",
                work_dir=tmp_dir,
                config=AgentRunConfig(vendor="codex", model="gpt-5.4-mini"),
                runtime_root=root / "runtime",
            )
            result = worker.wait_for_turn_artifacts(
                contract=TurnFileContract(
                    turn_id="create_routing_layer_1",
                    phase="routing_layer_create",
                    status_path=status_path,
                    validator=validator,
                    quiet_window_sec=1.0,
                ),
                timeout_sec=2.0,
            )
            self.assertEqual(Path(result.status_path).resolve(), status_path.resolve())
            self.assertEqual(Path(result.artifact_paths["artifact.txt"]).resolve(), artifact_path.resolve())

    def test_wait_for_turn_artifacts_ignores_transient_target_exists_false_after_observe(self):
        class FileContractWorker(TmuxBatchWorker):
            def target_exists(self, target=None):
                return False

            def observe(self, *, tail_lines=500, tail_bytes=24000):
                return WorkerObservation(
                    visible_text="processing",
                    raw_log_delta="delta",
                    raw_log_tail="tail",
                    current_command="codex",
                    current_path=str(self.work_dir),
                    pane_dead=False,
                    session_exists=True,
                    log_mtime=0.0,
                    observed_at="2026-04-12T00:00:00",
                )

            def capture_visible(self, tail_lines=500):
                return "visible"

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            artifact_path = root / "artifact.txt"
            artifact_path.write_text("artifact-body", encoding="utf-8")
            status_path = root / "turn_status.json"
            status_path.write_text(json.dumps({"ok": True}), encoding="utf-8")

            def validator(path: Path) -> TurnFileResult:
                payload = json.loads(path.read_text(encoding="utf-8"))
                return TurnFileResult(
                    status_path=str(path),
                    payload=payload,
                    artifact_paths={"artifact.txt": str(artifact_path)},
                    artifact_hashes={"artifact.txt": "sha256:test"},
                    validated_at="2026-04-12T00:00:00",
                )

            worker = FileContractWorker(
                worker_id="transient-target-worker",
                work_dir=tmp_dir,
                config=AgentRunConfig(vendor="codex", model="gpt-5.4-mini"),
                runtime_root=root / "runtime",
            )
            result = worker.wait_for_turn_artifacts(
                contract=TurnFileContract(
                    turn_id="audit_routing_layer_2",
                    phase="routing_layer_audit",
                    status_path=status_path,
                    validator=validator,
                    quiet_window_sec=1.0,
                ),
                timeout_sec=2.0,
            )
            self.assertEqual(Path(result.status_path).resolve(), status_path.resolve())

    def test_wait_for_turn_artifacts_requires_done_status(self):
        class FileContractWorker(TmuxBatchWorker):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.observe_count = 0

            def target_exists(self, target=None):
                return True

            def observe(self, *, tail_lines=500, tail_bytes=24000):
                self.observe_count += 1
                if self.observe_count == 1:
                    task_status_path.write_text('{"status": "running"}', encoding="utf-8")
                else:
                    task_status_path.write_text('{"status": "done"}', encoding="utf-8")
                return WorkerObservation(
                    visible_text="processing",
                    raw_log_delta="delta",
                    raw_log_tail="tail",
                    current_command="codex",
                    current_path=str(self.work_dir),
                    pane_dead=False,
                    session_exists=True,
                    log_mtime=0.0,
                    observed_at="2026-04-12T00:00:00",
                )

            def capture_visible(self, tail_lines=500):
                return "visible"

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            artifact_path = root / "artifact.txt"
            artifact_path.write_text("artifact-body", encoding="utf-8")
            status_path = root / "turn_status.json"
            status_path.write_text(json.dumps({"ok": True}), encoding="utf-8")
            task_status_path = root / "task_runtime.json"

            def validator(path: Path) -> TurnFileResult:
                payload = json.loads(path.read_text(encoding="utf-8"))
                return TurnFileResult(
                    status_path=str(path),
                    payload=payload,
                    artifact_paths={"artifact.txt": str(artifact_path)},
                    artifact_hashes={"artifact.txt": "sha256:test"},
                    validated_at="2026-04-12T00:00:00",
                )

            worker = FileContractWorker(
                worker_id="file-contract-signal-worker",
                work_dir=tmp_dir,
                config=AgentRunConfig(vendor="codex", model="gpt-5.4-mini"),
                runtime_root=root / "runtime",
            )
            result = worker.wait_for_turn_artifacts(
                contract=TurnFileContract(
                    turn_id="create_routing_layer_1",
                    phase="routing_layer_create",
                    status_path=status_path,
                    validator=validator,
                    quiet_window_sec=1.0,
                ),
                task_status_path=task_status_path,
                timeout_sec=2.0,
            )
            self.assertEqual(Path(result.status_path).resolve(), status_path.resolve())
            self.assertGreaterEqual(worker.observe_count, 2)

    def test_wait_for_turn_artifacts_accepts_stable_ready_files_without_helper_done(self):
        class FileContractReadyWorker(TmuxBatchWorker):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.observe_count = 0

            def target_exists(self, target=None):
                return True

            def observe(self, *, tail_lines=500, tail_bytes=24000):
                self.observe_count += 1
                self.provider_phase = ProviderPhase.WAITING_INPUT
                return WorkerObservation(
                    visible_text="› Continue working in @filename",
                    raw_log_delta="" if self.observe_count == 1 else "delta",
                    raw_log_tail="› Continue working in @filename",
                    current_command="codex",
                    current_path=str(self.work_dir),
                    pane_dead=False,
                    session_exists=True,
                    log_mtime=0.0,
                    observed_at="2026-04-17T00:00:00",
                    pane_title="AutoCodex",
                )

            def capture_visible(self, tail_lines=500):
                return "› Continue working in @filename"

            def _maybe_send_task_completion_nudge(self, **kwargs):
                return False

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            artifact_path = root / "artifact.txt"
            artifact_path.write_text("artifact-body", encoding="utf-8")
            status_path = root / "turn_status.json"
            status_path.write_text(json.dumps({"ok": True}), encoding="utf-8")
            task_status_path = root / "task_runtime.json"
            task_status_path.write_text('{"status": "running"}', encoding="utf-8")

            def validator(path: Path) -> TurnFileResult:
                payload = json.loads(path.read_text(encoding="utf-8"))
                return TurnFileResult(
                    status_path=str(path),
                    payload=payload,
                    artifact_paths={"artifact.txt": str(artifact_path)},
                    artifact_hashes={"artifact.txt": "sha256:test"},
                    validated_at="2026-04-17T00:00:00",
                )

            worker = FileContractReadyWorker(
                worker_id="file-contract-ready-worker",
                work_dir=tmp_dir,
                config=AgentRunConfig(vendor="codex", model="gpt-5.4-mini"),
                runtime_root=root / "runtime",
            )
            worker.pane_id = "%1"
            worker.agent_started = True
            result = worker.wait_for_turn_artifacts(
                contract=TurnFileContract(
                    turn_id="requirements_review_r1",
                    phase="需求评审",
                    status_path=status_path,
                    validator=validator,
                    quiet_window_sec=0.2,
                ),
                task_status_path=task_status_path,
                timeout_sec=2.0,
            )

            self.assertEqual(Path(result.status_path).resolve(), status_path.resolve())
            self.assertEqual(json.loads(task_status_path.read_text(encoding="utf-8")), {"status": "done"})
            self.assertEqual(worker.current_task_runtime_status, "done")
            self.assertGreaterEqual(worker.observe_count, 2)

    def test_wait_for_turn_artifacts_raises_contract_violation_after_done_with_invalid_files(self):
        class InvalidFileContractWorker(TmuxBatchWorker):
            def target_exists(self, target=None):
                return True

            def observe(self, *, tail_lines=500, tail_bytes=24000):
                return WorkerObservation(
                    visible_text="› Continue working in @filename",
                    raw_log_delta="",
                    raw_log_tail="› Continue working in @filename",
                    current_command="codex",
                    current_path=str(self.work_dir),
                    pane_dead=False,
                    session_exists=True,
                    log_mtime=0.0,
                    observed_at="2026-04-20T00:00:00",
                )

            def capture_visible(self, tail_lines=500):
                return "› Continue working in @filename"

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            status_path = root / "turn_status.json"
            status_path.write_text(json.dumps({"task_name": "需求评审", "review_pass": False}), encoding="utf-8")
            task_status_path = root / "task_runtime.json"
            task_status_path.write_text('{"status": "done"}', encoding="utf-8")

            def validator(path: Path) -> TurnFileResult:
                raise ValueError("审核器未通过，但评审 markdown 为空")

            worker = InvalidFileContractWorker(
                worker_id="invalid-file-contract-worker",
                work_dir=tmp_dir,
                config=AgentRunConfig(vendor="codex", model="gpt-5.4-mini"),
                runtime_root=root / "runtime",
            )
            with mock.patch("canopy_core.runtime.tmux_runtime.TURN_ARTIFACT_POST_DONE_GRACE_SEC", 0.0):
                with self.assertRaisesRegex(RuntimeError, "turn artifacts contract violation after task completion"):
                    worker.wait_for_turn_artifacts(
                        contract=TurnFileContract(
                            turn_id="requirements_review_r2",
                            phase="需求评审",
                            status_path=status_path,
                            validator=validator,
                            quiet_window_sec=0.0,
                        ),
                        task_status_path=task_status_path,
                        timeout_sec=2.0,
                    )

    def test_wait_for_turn_artifacts_allows_late_files_after_done_before_grace_expires(self):
        class LateArtifactWorker(TmuxBatchWorker):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.observe_count = 0

            def target_exists(self, target=None):
                return True

            def observe(self, *, tail_lines=500, tail_bytes=24000):
                self.observe_count += 1
                if self.observe_count >= 3:
                    status_path.write_text(json.dumps({"ok": True}), encoding="utf-8")
                return WorkerObservation(
                    visible_text="processing",
                    raw_log_delta="delta",
                    raw_log_tail="tail",
                    current_command="node",
                    current_path=str(self.work_dir),
                    pane_dead=False,
                    session_exists=True,
                    log_mtime=0.0,
                    observed_at="2026-04-20T00:00:00",
                )

            def capture_visible(self, tail_lines=500):
                return "visible"

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            artifact_path = root / "artifact.txt"
            artifact_path.write_text("artifact-body", encoding="utf-8")
            status_path = root / "turn_status.json"
            status_path.write_text(json.dumps({"ok": False}), encoding="utf-8")
            task_status_path = root / "task_runtime.json"
            task_status_path.write_text('{"status": "done"}', encoding="utf-8")

            def validator(path: Path) -> TurnFileResult:
                payload = json.loads(path.read_text(encoding="utf-8"))
                if not payload.get("ok"):
                    raise ValueError("not ready")
                return TurnFileResult(
                    status_path=str(path),
                    payload=payload,
                    artifact_paths={"artifact.txt": str(artifact_path)},
                    artifact_hashes={"artifact.txt": "sha256:test"},
                    validated_at="2026-04-20T00:00:00",
                )

            worker = LateArtifactWorker(
                worker_id="late-artifact-worker",
                work_dir=tmp_dir,
                config=AgentRunConfig(vendor="codex", model="gpt-5.4-mini"),
                runtime_root=root / "runtime",
            )
            with mock.patch("canopy_core.runtime.tmux_runtime.TURN_ARTIFACT_POST_DONE_GRACE_SEC", 2.0):
                result = worker.wait_for_turn_artifacts(
                    contract=TurnFileContract(
                        turn_id="task_split_review_r1",
                        phase="任务拆分",
                        status_path=status_path,
                        validator=validator,
                        quiet_window_sec=0.0,
                    ),
                    task_status_path=task_status_path,
                    timeout_sec=3.0,
                )

        self.assertEqual(Path(result.status_path).resolve(), status_path.resolve())
        self.assertGreaterEqual(worker.observe_count, 3)

    def test_wait_for_turn_artifacts_keeps_waiting_when_shell_returns_after_done(self):
        class ShellAfterDoneWorker(TmuxBatchWorker):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.observe_count = 0

            def target_exists(self, target=None):
                return True

            def observe(self, *, tail_lines=500, tail_bytes=24000):
                self.observe_count += 1
                if self.observe_count >= 3:
                    status_path.write_text(json.dumps({"ok": True}), encoding="utf-8")
                return WorkerObservation(
                    visible_text="❯",
                    raw_log_delta="delta",
                    raw_log_tail="tail",
                    current_command="zsh",
                    current_path=str(self.work_dir),
                    pane_dead=False,
                    session_exists=True,
                    log_mtime=0.0,
                    observed_at="2026-04-20T00:00:00",
                )

            def capture_visible(self, tail_lines=500):
                return "❯"

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            artifact_path = root / "artifact.txt"
            artifact_path.write_text("artifact-body", encoding="utf-8")
            status_path = root / "turn_status.json"
            status_path.write_text(json.dumps({"ok": False}), encoding="utf-8")
            task_status_path = root / "task_runtime.json"
            task_status_path.write_text('{"status": "done"}', encoding="utf-8")

            def validator(path: Path) -> TurnFileResult:
                payload = json.loads(path.read_text(encoding="utf-8"))
                if not payload.get("ok"):
                    raise ValueError("not ready")
                return TurnFileResult(
                    status_path=str(path),
                    payload=payload,
                    artifact_paths={"artifact.txt": str(artifact_path)},
                    artifact_hashes={"artifact.txt": "sha256:test"},
                    validated_at="2026-04-20T00:00:00",
                )

            worker = ShellAfterDoneWorker(
                worker_id="shell-after-done-worker",
                work_dir=tmp_dir,
                config=AgentRunConfig(vendor="codex", model="gpt-5.4-mini"),
                runtime_root=root / "runtime",
            )
            with mock.patch("canopy_core.runtime.tmux_runtime.TURN_ARTIFACT_POST_DONE_GRACE_SEC", 2.0):
                result = worker.wait_for_turn_artifacts(
                    contract=TurnFileContract(
                        turn_id="requirements_review_r3",
                        phase="需求评审",
                        status_path=status_path,
                        validator=validator,
                        quiet_window_sec=0.0,
                    ),
                    task_status_path=task_status_path,
                    timeout_sec=3.0,
                )

        self.assertEqual(Path(result.status_path).resolve(), status_path.resolve())
        self.assertGreaterEqual(worker.observe_count, 3)






    def test_observe_tolerates_tmux_display_message_race(self):
        class RaceBackend(TmuxBackend):
            def has_session(self, session_name: str) -> bool:
                return True

            def target_exists(self, target_name: str) -> bool:
                return True

            def capture_visible(self, target: str, *, tail_lines: int = 500) -> str:
                return "› ready"

            def display_message(self, target: str, expression: str) -> str:
                raise subprocess.CalledProcessError(1, ["tmux", "display-message"])

            def tail_raw_log(
                    self,
                    raw_log_path: str | Path,
                    *,
                    last_offset: int = 0,
                    tail_bytes: int = 24000,
            ) -> tuple[str, str, int, float]:
                return "", "", last_offset, 0.0

        with tempfile.TemporaryDirectory() as tmp_dir:
            worker = TmuxBatchWorker(
                worker_id="observe-race-worker",
                work_dir=tmp_dir,
                config=AgentRunConfig(vendor="codex", model="gpt-5.4-mini"),
                runtime_root=Path(tmp_dir) / "runtime",
                existing_session_name="demo-session",
                existing_pane_id="%1",
                backend=RaceBackend(),
            )
            observation = worker.observe()
            self.assertTrue(observation.session_exists)
            self.assertEqual("", observation.current_command)
            self.assertEqual("› ready", observation.visible_text)
            self.assertEqual("", observation.pane_title)

    def test_run_turn_with_completion_contract_uses_file_protocol_not_stdout_tokens(self):
        class CompletionContractWorker(TmuxBatchWorker):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.sent_prompts = []

            def _append_transcript(self, title, body):
                return None

            def ensure_agent_ready(self, timeout_sec=60.0):
                self.pane_id = "%1"
                self.agent_ready = True
                self.provider_phase = ProviderPhase.WAITING_INPUT
                self.current_command = "claude"
                self.current_path = str(self.work_dir)

            def target_exists(self, target=None):
                return True

            def capture_visible(self, tail_lines=500):
                return "visible"

            def observe(self, *, tail_lines=500, tail_bytes=24000):
                return WorkerObservation(
                    visible_text="processing",
                    raw_log_delta="delta" if self.sent_prompts else "",
                    raw_log_tail="processing",
                    current_command="claude",
                    current_path=str(self.work_dir),
                    pane_dead=False,
                    session_exists=True,
                    log_mtime=0.0,
                    observed_at="2026-04-12T00:00:00",
                )

            def _send_text(self, text, enter_count=None):
                self.sent_prompts.append(text)
                artifact_path = self.work_dir / "artifact.json"
                artifact_path.write_text('{"ready": true}', encoding="utf-8")
                status_payload = {
                    "schema_version": "1.0",
                    "turn_id": "audit_routing_layer_1",
                    "phase": "routing_layer_audit",
                    "status": "done",
                    "written_at": "2026-04-12T00:00:00+08:00",
                }
                contract_path.write_text(json.dumps(status_payload, ensure_ascii=False), encoding="utf-8")
                write_task_status(self.current_task_status_path, status="done")

            def _wait_for_turn_reply(self, **kwargs):
                raise AssertionError("completion_contract path should not use stdout reply waiting")

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            contract_path = root / "turn_status.json"
            artifact_path = root / "artifact.json"

            def validator(path: Path) -> TurnFileResult:
                payload = json.loads(path.read_text(encoding="utf-8"))
                return TurnFileResult(
                    status_path=str(path),
                    payload=payload,
                    artifact_paths={"artifact.json": str(artifact_path)},
                    artifact_hashes={"artifact.json": "sha256:test"},
                    validated_at="2026-04-12T00:00:01",
                )

            worker = CompletionContractWorker(
                worker_id="completion-worker",
                work_dir=tmp_dir,
                config=AgentRunConfig(vendor="claude", model="haiku"),
                runtime_root=root / "runtime",
            )
            result = worker.run_turn(
                label="audit_routing_layer_1",
                prompt="write audit files only",
                completion_contract=TurnFileContract(
                    turn_id="audit_routing_layer_1",
                    phase="routing_layer_audit",
                    status_path=contract_path,
                    validator=validator,
                    quiet_window_sec=1.0,
                ),
                timeout_sec=2.0,
            )
            self.assertTrue(result.ok)
            self.assertEqual(len(worker.sent_prompts), 1)
            self.assertIn("write audit files only", worker.sent_prompts[0])
            self.assertNotIn(worker.current_task_status_path, worker.sent_prompts[0])
            parsed = json.loads(result.clean_output)
            self.assertEqual(Path(parsed["status_path"]).resolve(), contract_path.resolve())
            self.assertEqual(Path(parsed["artifact_paths"]["artifact.json"]).resolve(), artifact_path.resolve())


    def test_validate_task_result_file_requires_required_artifacts_and_hashes(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            worker = TmuxBatchWorker(
                worker_id="validate-result-worker",
                work_dir=tmp_dir,
                config=AgentRunConfig(vendor="codex", model="gpt-5.4-mini"),
                runtime_root=root / "runtime",
            )
            ask_human = root / "ask_human.md"
            ask_human.write_text("问题\n", encoding="utf-8")
            result_path = root / "result.json"
            result_path.write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "turn_id": "requirements_review_human_feedback",
                        "phase": "requirements_review_human_feedback",
                        "task_kind": "a03_human_feedback",
                        "status": "completed",
                        "summary": "ok",
                        "artifacts": {},
                        "artifact_hashes": {},
                        "written_at": "2026-04-16T00:00:00+00:00",
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            contract = TaskResultContract(
                turn_id="requirements_review_human_feedback",
                phase="requirements_review_human_feedback",
                task_kind="a03_human_feedback",
                mode="a03_human_feedback",
                expected_statuses=("completed",),
                required_artifacts={"ask_human": ask_human},
            )
            with self.assertRaises(ValueError):
                worker._validate_task_result_file(contract=contract, result_path=result_path)

    def test_wait_for_task_result_requires_done_task_status_before_success(self):
        class WaitResultWorker(TmuxBatchWorker):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.track_calls = 0

            def observe(self, *, tail_lines=500, tail_bytes=24000):
                return WorkerObservation(
                    visible_text="› ready",
                    raw_log_delta="delta",
                    raw_log_tail="› ready",
                    current_command="codex",
                    current_path=str(self.work_dir),
                    pane_dead=False,
                    session_exists=True,
                    log_mtime=0.0,
                    observed_at="2026-04-16T00:00:00",
                )

            def _maybe_send_task_completion_nudge(self, **kwargs):
                return False

            def _track_task_completion_signal(self, *, task_status_path, status_done_seen):
                self.track_calls += 1
                return self.track_calls >= 2

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            ask_human = root / "ask_human.md"
            ask_human.write_text("答复\n", encoding="utf-8")
            result_path = root / "result.json"
            worker = WaitResultWorker(
                worker_id="wait-result-worker",
                work_dir=tmp_dir,
                config=AgentRunConfig(vendor="codex", model="gpt-5.4-mini"),
                runtime_root=root / "runtime",
            )
            result_path.write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "turn_id": "requirements_review_human_feedback",
                        "phase": "requirements_review_human_feedback",
                        "task_kind": "a03_human_feedback",
                        "status": "completed",
                        "summary": "ok",
                        "artifacts": {
                            "ask_human": str(ask_human.resolve()),
                        },
                        "artifact_hashes": {
                            str(ask_human.resolve()): "sha256:" + __import__("hashlib").sha256(
                                ask_human.read_bytes()
                            ).hexdigest(),
                        },
                        "written_at": "2026-04-16T00:00:00+00:00",
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            contract = TaskResultContract(
                turn_id="requirements_review_human_feedback",
                phase="requirements_review_human_feedback",
                task_kind="a03_human_feedback",
                mode="a03_human_feedback",
                expected_statuses=("completed",),
                required_artifacts={"ask_human": ask_human},
            )
            result = worker.wait_for_task_result(
                contract=contract,
                task_status_path=root / "task_status.json",
                result_path=result_path,
                timeout_sec=2.0,
            )
            self.assertEqual(result.payload["status"], "completed")
            self.assertGreaterEqual(worker.track_calls, 2)

    def test_wait_for_task_result_finalizes_from_contract_when_ready_without_terminal_token(self):
        class ContractReadyWorker(TmuxBatchWorker):
            def target_exists(self, target=None):
                return True

            def observe(self, *, tail_lines=500, tail_bytes=24000):
                return WorkerObservation(
                    visible_text="› Continue working in @filename",
                    raw_log_delta="",
                    raw_log_tail="",
                    current_command="codex",
                    current_path=str(self.work_dir),
                    pane_dead=False,
                    session_exists=True,
                    log_mtime=0.0,
                    observed_at="2026-04-16T00:00:03",
                    pane_title="AutoCodex",
                )

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            requirements_clear = root / "需求澄清.md"
            requirements_clear.write_text("已澄清\n", encoding="utf-8")
            task_status_path = root / "task_status.json"
            task_status_path.write_text('{"status": "running"}', encoding="utf-8")
            result_path = root / "result.json"
            worker = ContractReadyWorker(
                worker_id="contract-ready-result-worker",
                work_dir=tmp_dir,
                config=AgentRunConfig(vendor="codex", model="gpt-5.4-mini"),
                runtime_root=root / "runtime",
            )
            worker.pane_id = "%1"
            worker.agent_started = True
            contract = TaskResultContract(
                turn_id="requirements_review_ba_resume",
                phase="requirements_review_ba_resume",
                task_kind="a03_ba_resume",
                mode="a03_ba_resume",
                expected_statuses=("ready",),
                optional_artifacts={"requirements_clear": requirements_clear},
            )

            result = worker.wait_for_task_result(
                contract=contract,
                task_status_path=task_status_path,
                result_path=result_path,
                timeout_sec=1.0,
            )

            self.assertEqual(result.payload["status"], "ready")
            self.assertEqual(json.loads(task_status_path.read_text(encoding="utf-8")), {"status": "done"})
            self.assertTrue(result_path.exists())

    def test_wait_for_task_result_materializes_ready_result_from_terminal_reply_without_helper(self):
        class ReadyReplyWorker(TmuxBatchWorker):
            def target_exists(self, target=None):
                return True

            def observe(self, *, tail_lines=500, tail_bytes=24000):
                return WorkerObservation(
                    visible_text="\n".join(
                        [
                            "• 准备完毕",
                            "",
                            "",
                            "› Use /skills to list available skills",
                            "",
                            "  gpt-5.4-mini low · ~/Desktop/my_test",
                        ]
                    ),
                    raw_log_delta="",
                    raw_log_tail="• 准备完毕",
                    current_command="codex",
                    current_path=str(self.work_dir),
                    pane_dead=False,
                    session_exists=True,
                    log_mtime=0.0,
                    observed_at="2026-04-16T00:00:03",
                    pane_title="AutoCodex",
                )

            def _maybe_send_task_completion_nudge(self, **kwargs):
                return False

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            requirements_clear = root / "需求澄清.md"
            requirements_clear.write_text("已澄清\n", encoding="utf-8")
            task_status_path = root / "task_status.json"
            task_status_path.write_text('{"status": "running"}', encoding="utf-8")
            result_path = root / "result.json"
            worker = ReadyReplyWorker(
                worker_id="ready-result-worker",
                work_dir=tmp_dir,
                config=AgentRunConfig(vendor="codex", model="gpt-5.4-mini"),
                runtime_root=root / "runtime",
            )
            worker.pane_id = "%1"
            worker.agent_started = True
            contract = TaskResultContract(
                turn_id="requirements_review_ba_resume",
                phase="requirements_review_ba_resume",
                task_kind="a03_ba_resume",
                mode="a03_ba_resume",
                expected_statuses=("ready",),
                optional_artifacts={"requirements_clear": requirements_clear},
                terminal_status_tokens={"ready": ("准备完毕",)},
                terminal_status_summaries={"ready": "需求分析师已进入需求评审准备态"},
            )
            result = worker.wait_for_task_result(
                contract=contract,
                task_status_path=task_status_path,
                result_path=result_path,
                timeout_sec=1.0,
            )
            self.assertEqual(result.payload["status"], "ready")
            self.assertEqual(result.payload["summary"], "需求分析师已进入需求评审准备态")
            self.assertEqual(json.loads(task_status_path.read_text(encoding="utf-8")), {"status": "done"})
            self.assertEqual(
                result.payload["artifacts"]["requirements_clear"],
                str(requirements_clear.resolve()),
            )

    def test_wait_for_task_result_materializes_terminal_result_even_when_terminal_recently_changed(self):
        class NoisyReadyReplyWorker(TmuxBatchWorker):
            def target_exists(self, target=None):
                return True

            def observe(self, *, tail_lines=500, tail_bytes=24000):
                self.provider_phase = ProviderPhase.WAITING_INPUT
                self.terminal_recently_changed = True
                return WorkerObservation(
                    visible_text="\n".join(
                        [
                            "• 完成",
                            "",
                            "",
                            "› Run /review on my current changes",
                            "",
                            "  gpt-5.4-mini low · ~/Desktop/my_test",
                        ]
                    ),
                    raw_log_delta="",
                    raw_log_tail="• 完成",
                    current_command="codex",
                    current_path=str(self.work_dir),
                    pane_dead=False,
                    session_exists=True,
                    log_mtime=0.0,
                    observed_at="2026-04-19T00:00:03",
                    pane_title="AutoCodex",
                )

            def _maybe_send_task_completion_nudge(self, **kwargs):
                return False

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            requirements_clear = root / "需求澄清.md"
            requirements_clear.write_text("已澄清\n", encoding="utf-8")
            task_status_path = root / "task_status.json"
            task_status_path.write_text('{"status": "running"}', encoding="utf-8")
            result_path = root / "result.json"
            worker = NoisyReadyReplyWorker(
                worker_id="noisy-ready-result-worker",
                work_dir=tmp_dir,
                config=AgentRunConfig(vendor="codex", model="gpt-5.4-mini"),
                runtime_root=root / "runtime",
            )
            worker.pane_id = "%1"
            worker.agent_started = True
            contract = TaskResultContract(
                turn_id="a05_ba_init",
                phase="a05_ba_init",
                task_kind="a05_ba_init",
                mode="a05_ba_init",
                expected_statuses=("ready",),
                optional_artifacts={"requirements_clear": requirements_clear},
                terminal_status_tokens={"ready": ("完成",)},
                terminal_status_summaries={"ready": "需求分析师已完成详细设计初始化"},
            )
            result = worker.wait_for_task_result(
                contract=contract,
                task_status_path=task_status_path,
                result_path=result_path,
                timeout_sec=1.0,
            )
            self.assertEqual(result.payload["status"], "ready")
            self.assertEqual(result.payload["summary"], "需求分析师已完成详细设计初始化")
            self.assertEqual(json.loads(task_status_path.read_text(encoding="utf-8")), {"status": "done"})
            self.assertEqual(
                result.payload["artifacts"]["requirements_clear"],
                str(requirements_clear.resolve()),
            )

    def test_wait_for_task_result_ignores_stale_terminal_token_from_previous_turn(self):
        class StaleDoneReplyWorker(TmuxBatchWorker):
            def observe(self, *, tail_lines=500, tail_bytes=24000):
                return WorkerObservation(
                    visible_text="\n".join(
                        [
                            "• 完成",
                            "",
                            "",
                            "› Explain this codebase",
                            "",
                            "  gpt-5.4-mini low · ~/Desktop/my_test",
                        ]
                    ),
                    raw_log_delta="",
                    raw_log_tail="",
                    current_command="codex",
                    current_path=str(self.work_dir),
                    pane_dead=False,
                    session_exists=True,
                    log_mtime=0.0,
                    observed_at="2026-04-20T00:00:03",
                    pane_title="AutoCodex",
                )

            def _maybe_send_task_completion_nudge(self, **kwargs):
                return False

            def capture_visible(self, tail_lines=500):
                return "• 完成"

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            detailed_design = root / "详细设计.md"
            task_status_path = root / "task_status.json"
            task_status_path.write_text('{"status": "running"}', encoding="utf-8")
            result_path = root / "result.json"
            worker = StaleDoneReplyWorker(
                worker_id="stale-done-result-worker",
                work_dir=tmp_dir,
                config=AgentRunConfig(vendor="codex", model="gpt-5.4-mini"),
                runtime_root=root / "runtime",
            )
            contract = TaskResultContract(
                turn_id="a05_detailed_design_generate",
                phase="a05_detailed_design_generate",
                task_kind="a05_detailed_design_generate",
                mode="a05_detailed_design_generate",
                expected_statuses=("completed",),
                required_artifacts={"detailed_design": detailed_design},
                terminal_status_tokens={"completed": ("完成",)},
                terminal_status_summaries={"completed": "需求分析师已生成详细设计文档"},
            )
            with self.assertRaises(TimeoutError):
                worker.wait_for_task_result(
                    contract=contract,
                    task_status_path=task_status_path,
                    result_path=result_path,
                    timeout_sec=0.1,
                    baseline_visible="\n".join(
                        [
                            "• 完成",
                            "",
                            "",
                            "› Explain this codebase",
                            "",
                            "  gpt-5.4-mini low · ~/Desktop/my_test",
                        ]
                    ),
                )

    def test_wait_for_task_result_accepts_repeated_terminal_token_when_delta_is_fresh(self):
        class FreshDoneReplyWorker(TmuxBatchWorker):
            def target_exists(self, target=None):
                return True

            def observe(self, *, tail_lines=500, tail_bytes=24000):
                return WorkerObservation(
                    visible_text="\n".join(
                        [
                            "• 完成",
                            "",
                            "",
                            "› Explain this codebase",
                            "",
                            "  gpt-5.4-mini low · ~/Desktop/my_test",
                        ]
                    ),
                    raw_log_delta="• 完成",
                    raw_log_tail="• 完成",
                    current_command="codex",
                    current_path=str(self.work_dir),
                    pane_dead=False,
                    session_exists=True,
                    log_mtime=0.0,
                    observed_at="2026-04-20T00:00:03",
                    pane_title="AutoCodex",
                )

            def _maybe_send_task_completion_nudge(self, **kwargs):
                return False

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            detailed_design = root / "详细设计.md"
            detailed_design.write_text("设计正文\n", encoding="utf-8")
            task_status_path = root / "task_status.json"
            task_status_path.write_text('{"status": "running"}', encoding="utf-8")
            result_path = root / "result.json"
            worker = FreshDoneReplyWorker(
                worker_id="fresh-done-result-worker",
                work_dir=tmp_dir,
                config=AgentRunConfig(vendor="codex", model="gpt-5.4-mini"),
                runtime_root=root / "runtime",
            )
            worker.pane_id = "%1"
            worker.agent_started = True
            contract = TaskResultContract(
                turn_id="a05_detailed_design_generate",
                phase="a05_detailed_design_generate",
                task_kind="a05_detailed_design_generate",
                mode="a05_detailed_design_generate",
                expected_statuses=("completed",),
                required_artifacts={"detailed_design": detailed_design},
                terminal_status_tokens={"completed": ("完成",)},
                terminal_status_summaries={"completed": "需求分析师已生成详细设计文档"},
            )
            result = worker.wait_for_task_result(
                contract=contract,
                task_status_path=task_status_path,
                result_path=result_path,
                timeout_sec=1.0,
                baseline_visible="\n".join(
                    [
                        "• 完成",
                        "",
                        "",
                        "› Explain this codebase",
                        "",
                        "  gpt-5.4-mini low · ~/Desktop/my_test",
                    ]
                ),
            )
            self.assertEqual(result.payload["status"], "completed")
            self.assertEqual(json.loads(task_status_path.read_text(encoding="utf-8")), {"status": "done"})

    def test_ensure_agent_ready_does_not_reuse_processing_phase(self):
        class ProcessingWorker(TmuxBatchWorker):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.wait_called = 0

            def target_exists(self, target=None):
                return True

            def pane_dead(self):
                return False

            def pane_current_command(self):
                return "codex"

            def _wait_for_agent_ready(self, timeout_sec=60.0):
                self.wait_called += 1
                self.agent_ready = True
                self.provider_phase = ProviderPhase.WAITING_INPUT

        with tempfile.TemporaryDirectory() as tmp_dir:
            worker = ProcessingWorker(
                worker_id="processing-worker",
                work_dir=tmp_dir,
                config=AgentRunConfig(vendor="codex", model="gpt-5"),
                runtime_root=Path(tmp_dir) / "runtime",
            )
            worker.pane_id = "%1"
            worker.agent_ready = True
            worker.provider_phase = ProviderPhase.PROCESSING
            worker.ensure_agent_ready(timeout_sec=0.1)
            self.assertEqual(worker.wait_called, 1)

    def test_wait_for_agent_ready_accepts_codex_work_dir_title(self):
        class ReadyTitleWorker(TmuxBatchWorker):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.observe_count = 0

            def target_exists(self, target=None):
                return True

            def observe(self, *, tail_lines=500, tail_bytes=24000):
                self.observe_count += 1
                pane_title = "⠋ canopy-api-v3" if self.observe_count == 1 else "canopy-api-v3"
                return WorkerObservation(
                    visible_text="› Write tests for @filename",
                    raw_log_delta="",
                    raw_log_tail="› Write tests for @filename",
                    current_command="node",
                    current_path=str(self.work_dir),
                    pane_dead=False,
                    session_exists=True,
                    log_mtime=0.0,
                    observed_at=f"2026-04-21T00:00:0{self.observe_count}",
                    pane_title=pane_title,
                )

            def capture_visible(self, tail_lines=500):
                return "› Write tests for @filename"

        with tempfile.TemporaryDirectory(prefix="codex-ready-") as tmp_dir:
            work_dir = Path(tmp_dir) / "canopy-api-v3"
            work_dir.mkdir()
            worker = ReadyTitleWorker(
                worker_id="ready-title-worker",
                work_dir=work_dir,
                config=AgentRunConfig(vendor="codex", model="gpt-5.4-mini"),
                runtime_root=Path(tmp_dir) / "runtime",
            )
            worker.pane_id = "%1"

            with mock.patch("canopy_core.runtime.tmux_runtime.time.sleep", return_value=None):
                worker._wait_for_agent_ready(timeout_sec=1.0)

            self.assertTrue(worker.agent_started)
            self.assertTrue(worker.agent_ready)
            self.assertEqual(worker.wrapper_state, WrapperState.READY)
            self.assertEqual(worker.last_pane_title, "canopy-api-v3")
            self.assertEqual(worker.observe_count, 3)


    def test_codex_boot_prompt_handler_debounces_repeated_enter(self):
        class CodexBootWorker(TmuxBatchWorker):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.keys: list[str] = []

            def send_special_key(self, key: str) -> None:
                self.keys.append(key)

        with tempfile.TemporaryDirectory() as tmp_dir:
            worker = CodexBootWorker(
                worker_id="boot-worker",
                work_dir=tmp_dir,
                config=AgentRunConfig(vendor="codex", model="gpt-5.4-mini"),
                runtime_root=Path(tmp_dir) / "runtime",
            )
            trust_prompt = """
Do you trust the contents of this directory?
› 1. Yes, continue
2. No, quit
Press enter to continue
"""
            self.assertTrue(worker._maybe_handle_codex_boot_prompt(trust_prompt))
            self.assertFalse(worker._maybe_handle_codex_boot_prompt(trust_prompt))
            self.assertEqual(worker.keys, ["Enter"])

    def test_gemini_boot_prompt_handler_accepts_trust_folder_prompt(self):
        class GeminiBootWorker(TmuxBatchWorker):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.keys: list[str] = []

            def send_special_key(self, key: str) -> None:
                self.keys.append(key)

        with tempfile.TemporaryDirectory() as tmp_dir:
            worker = GeminiBootWorker(
                worker_id="boot-worker",
                work_dir=tmp_dir,
                config=AgentRunConfig(vendor="gemini", model="flash"),
                runtime_root=Path(tmp_dir) / "runtime",
            )
            trust_prompt = """
Do you trust the files in this folder?
1. Trust folder
2. Trust parent folder
3. Don't trust
"""
            self.assertTrue(worker._maybe_handle_gemini_boot_prompt(trust_prompt))
            self.assertFalse(worker._maybe_handle_gemini_boot_prompt(trust_prompt))
            self.assertEqual(worker.keys, ["Enter"])

    def test_kimi_boot_prompt_handler_skips_update_prompt(self):
        class KimiBootWorker(TmuxBatchWorker):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.keys: list[str] = []

            def send_special_key(self, key: str) -> None:
                self.keys.append(key)

        with tempfile.TemporaryDirectory() as tmp_dir:
            worker = KimiBootWorker(
                worker_id="boot-worker",
                work_dir=tmp_dir,
                config=AgentRunConfig(vendor="kimi", model="kimi-k2-turbo"),
                runtime_root=Path(tmp_dir) / "runtime",
            )
            update_prompt = """
kimi-cli update available
[Enter] Upgrade now
[q] Not now, remind me next time
[s] Skip reminders for version 1.34.0
"""
            self.assertTrue(worker._maybe_handle_kimi_boot_prompt(update_prompt))
            self.assertFalse(worker._maybe_handle_kimi_boot_prompt(update_prompt))
            self.assertEqual(worker.keys, ["q"])

    def test_wait_for_turn_reply_does_not_require_full_pane_stability_after_token(self):
        class DynamicPaneWorker(TmuxBatchWorker):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.visible_count = 0

            def target_exists(self, target=None):
                return True

            def observe(self, *, tail_lines=500, tail_bytes=24000):
                self.visible_count += 1
                return WorkerObservation(
                    visible_text=f"frame-{self.visible_count}",
                    raw_log_delta="done\n[[ACX_TURN:test1234:DONE]]\n[[ROUTING_CREATE:DONE]]",
                    raw_log_tail="done\n[[ACX_TURN:test1234:DONE]]\n[[ROUTING_CREATE:DONE]]",
                    current_command="gemini",
                    current_path=str(self.work_dir),
                    pane_dead=False,
                    session_exists=True,
                    log_mtime=0.0,
                    observed_at="2026-04-12T00:00:03",
                )

        with tempfile.TemporaryDirectory() as tmp_dir:
            worker = DynamicPaneWorker(
                worker_id="gemini-worker",
                work_dir=tmp_dir,
                config=AgentRunConfig(vendor="gemini", model="flash"),
                runtime_root=Path(tmp_dir) / "runtime",
            )
            reply = worker._wait_for_turn_reply(
                baseline_reply="",
                baseline_visible="baseline",
                turn_token="[[ACX_TURN:test1234:DONE]]",
                required_tokens=["[[ROUTING_CREATE:DONE]]"],
                timeout_sec=0.2,
            )
            self.assertIn("[[ROUTING_CREATE:DONE]]", reply)

    def test_wait_for_turn_reply_requires_done_status(self):
        class DynamicPaneWorker(TmuxBatchWorker):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.observe_count = 0

            def target_exists(self, target=None):
                return True

            def observe(self, *, tail_lines=500, tail_bytes=24000):
                self.observe_count += 1
                if self.observe_count == 1:
                    task_status_path.write_text('{"status": "running"}', encoding="utf-8")
                else:
                    task_status_path.write_text('{"status": "done"}', encoding="utf-8")
                return WorkerObservation(
                    visible_text=f"frame-{self.observe_count}",
                    raw_log_delta="delta",
                    raw_log_tail="answer\n[[ACX_TURN:test1234:DONE]]\n[[ROUTING_CREATE:DONE]]",
                    current_command="gemini",
                    current_path=str(self.work_dir),
                    pane_dead=False,
                    session_exists=True,
                    log_mtime=0.0,
                    observed_at="2026-04-12T00:00:03",
                )

        with tempfile.TemporaryDirectory() as tmp_dir:
            task_status_path = Path(tmp_dir) / "task_runtime.json"
            worker = DynamicPaneWorker(
                worker_id="gemini-worker",
                work_dir=tmp_dir,
                config=AgentRunConfig(vendor="gemini", model="flash"),
                runtime_root=Path(tmp_dir) / "runtime",
            )
            reply = worker._wait_for_turn_reply(
                baseline_reply="",
                baseline_visible="baseline",
                turn_token="[[ACX_TURN:test1234:DONE]]",
                required_tokens=["[[ROUTING_CREATE:DONE]]"],
                task_status_path=task_status_path,
                timeout_sec=1.0,
            )
            self.assertIn("[[ROUTING_CREATE:DONE]]", reply)
            self.assertGreaterEqual(worker.observe_count, 2)

    def test_wait_for_turn_reply_accepts_required_token_after_done_without_turn_token(self):
        class ReadyWorker(TmuxBatchWorker):
            def observe(self, *, tail_lines=500, tail_bytes=24000):
                return WorkerObservation(
                    visible_text="\n".join(
                        [
                            "• 准备完毕",
                            "",
                            "",
                            "› Use /skills to list available skills",
                            "",
                            "  gpt-5.4 high fast · ~/Desktop/KevinGit/My_C_Tools",
                        ]
                    ),
                    raw_log_delta="",
                    raw_log_tail="helper finished",
                    current_command="codex",
                    current_path=str(self.work_dir),
                    pane_dead=False,
                    session_exists=True,
                    log_mtime=0.0,
                    observed_at="2026-04-16T00:00:03",
                )

        with tempfile.TemporaryDirectory() as tmp_dir:
            task_status_path = Path(tmp_dir) / "task_runtime.json"
            task_status_path.write_text('{"status": "done"}', encoding="utf-8")
            worker = ReadyWorker(
                worker_id="codex-worker",
                work_dir=tmp_dir,
                config=AgentRunConfig(vendor="codex", model="gpt-5.4"),
                runtime_root=Path(tmp_dir) / "runtime",
            )
            reply = worker._wait_for_turn_reply(
                baseline_reply="",
                baseline_visible="baseline",
                turn_token="[[ACX_TURN:test1234:DONE]]",
                required_tokens=["准备完毕"],
                task_status_path=task_status_path,
                timeout_sec=0.2,
            )
            self.assertEqual(reply, "准备完毕")

    def test_extract_reply_from_observation_normalizes_pass_audit_to_token_only(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            worker = TmuxBatchWorker(
                worker_id="gemini-worker",
                work_dir=tmp_dir,
                config=AgentRunConfig(vendor="gemini", model="flash"),
                runtime_root=Path(tmp_dir) / "runtime",
            )
            observation = WorkerObservation(
                visible_text="",
                raw_log_delta="",
                raw_log_tail="\n".join(
                    [
                        "⠼ Thinking... (esc to cancel, 14s)   ? for shortcuts",
                        "*   Type your message or @path/to/file",
                        "~/Desktop/KevinGit/PyFinance main no sandbox gemini-3-flash-preview",
                        "[[ACX_TURN:f565aae0:DONE]]",
                        "[[ROUTING_AUDIT:PASS]]",
                    ]
                ),
                current_command="gemini",
                current_path=str(Path(tmp_dir)),
                pane_dead=False,
                session_exists=True,
                log_mtime=0.0,
                observed_at="2026-04-12T00:00:00",
            )
            reply = worker._extract_reply_from_observation(
                observation,
                turn_token="[[ACX_TURN:f565aae0:DONE]]",
                required_tokens=["[[ROUTING_AUDIT:PASS]]", "[[ROUTING_AUDIT:REVISE]]"],
            )
            self.assertEqual(reply, "[[ROUTING_AUDIT:PASS]]")

    def test_extract_reply_from_observation_preserves_unexpected_pass_content_for_validation(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            worker = TmuxBatchWorker(
                worker_id="gemini-worker",
                work_dir=tmp_dir,
                config=AgentRunConfig(vendor="gemini", model="flash"),
                runtime_root=Path(tmp_dir) / "runtime",
            )
            observation = WorkerObservation(
                visible_text="",
                raw_log_delta="",
                raw_log_tail="\n".join(
                    [
                        "summary: mostly usable, but here is extra prose",
                        "[[ACX_TURN:f565aae0:DONE]]",
                        "[[ROUTING_AUDIT:PASS]]",
                    ]
                ),
                current_command="gemini",
                current_path=str(Path(tmp_dir)),
                pane_dead=False,
                session_exists=True,
                log_mtime=0.0,
                observed_at="2026-04-12T00:00:00",
            )
            reply = worker._extract_reply_from_observation(
                observation,
                turn_token="[[ACX_TURN:f565aae0:DONE]]",
                required_tokens=["[[ROUTING_AUDIT:PASS]]", "[[ROUTING_AUDIT:REVISE]]"],
            )
            self.assertIn("summary: mostly usable", reply)
            self.assertTrue(reply.endswith("[[ROUTING_AUDIT:PASS]]"))

    def test_extract_reply_from_observation_preserves_unexpected_pass_content_after_turn_token(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            worker = TmuxBatchWorker(
                worker_id="gemini-worker",
                work_dir=tmp_dir,
                config=AgentRunConfig(vendor="gemini", model="flash"),
                runtime_root=Path(tmp_dir) / "runtime",
            )
            observation = WorkerObservation(
                visible_text="",
                raw_log_delta="",
                raw_log_tail="\n".join(
                    [
                        "[[ACX_TURN:f565aae0:DONE]]",
                        "summary: extra prose after turn token",
                        "[[ROUTING_AUDIT:PASS]]",
                    ]
                ),
                current_command="gemini",
                current_path=str(Path(tmp_dir)),
                pane_dead=False,
                session_exists=True,
                log_mtime=0.0,
                observed_at="2026-04-12T00:00:00",
            )
            reply = worker._extract_reply_from_observation(
                observation,
                turn_token="[[ACX_TURN:f565aae0:DONE]]",
                required_tokens=["[[ROUTING_AUDIT:PASS]]", "[[ROUTING_AUDIT:REVISE]]"],
            )
            self.assertIn("summary: extra prose after turn token", reply)
            self.assertTrue(reply.endswith("[[ROUTING_AUDIT:PASS]]"))

    def test_extract_reply_from_observation_uses_last_audit_token_in_current_turn(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            worker = TmuxBatchWorker(
                worker_id="gemini-worker",
                work_dir=tmp_dir,
                config=AgentRunConfig(vendor="gemini", model="flash"),
                runtime_root=Path(tmp_dir) / "runtime",
            )
            observation = WorkerObservation(
                visible_text="",
                raw_log_delta="",
                raw_log_tail="\n".join(
                    [
                        "- verdict: usable_but_drift_prone",
                        "- recommendation: minor_structural_fixes",
                        "[[ACX_TURN:f565aae0:DONE]]",
                        "[[ROUTING_AUDIT:PASS]]",
                        "- finding: severity=high | title=late correction | problem=late | impact=late | evidence=docs/x | direction=use final token",
                        "[[ROUTING_AUDIT:REVISE]]",
                    ]
                ),
                current_command="gemini",
                current_path=str(Path(tmp_dir)),
                pane_dead=False,
                session_exists=True,
                log_mtime=0.0,
                observed_at="2026-04-12T00:00:00",
            )
            reply = worker._extract_reply_from_observation(
                observation,
                turn_token="[[ACX_TURN:f565aae0:DONE]]",
                required_tokens=["[[ROUTING_AUDIT:PASS]]", "[[ROUTING_AUDIT:REVISE]]"],
            )
            self.assertIn("- verdict: usable_but_drift_prone", reply)
            self.assertIn("- finding: severity=high | title=late correction", reply)
            self.assertTrue(reply.endswith("[[ROUTING_AUDIT:REVISE]]"))

    def test_extract_reply_from_observation_excludes_prompt_example_bullets_far_before_turn(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            worker = TmuxBatchWorker(
                worker_id="gemini-worker",
                work_dir=tmp_dir,
                config=AgentRunConfig(vendor="gemini", model="flash"),
                runtime_root=Path(tmp_dir) / "runtime",
            )
            filler = [f"filler line {index}" for index in range(170)]
            observation = WorkerObservation(
                visible_text="",
                raw_log_delta="",
                raw_log_tail="\n".join(
                    [
                        "- finding: severity=critical|high|medium|low | title=<short> | problem=<short> | impact=<short> | evidence=<path[:line]|...> | direction=<short structural fix>",
                        "- duplication: topic=<short> | owner=<file> | overlaps=<file1,file2,...> | value=necessary|wasteful | note=<short>",
                        *filler,
                        "- verdict: usable_but_drift_prone",
                        "- missing: area=docs_pitfalls | effect=missing risk registry | direction=restore file",
                        "- recommendation: minor_structural_fixes",
                        "[[ACX_TURN:test1234:DONE]]",
                        "[[ROUTING_AUDIT:REVISE]]",
                        "────────────────────────────────────────────────────────────────────────────────",
                    ]
                ),
                current_command="gemini",
                current_path=str(Path(tmp_dir)),
                pane_dead=False,
                session_exists=True,
                log_mtime=0.0,
                observed_at="2026-04-12T00:00:00",
            )
            reply = worker._extract_reply_from_observation(
                observation,
                turn_token="[[ACX_TURN:test1234:DONE]]",
                required_tokens=["[[ROUTING_AUDIT:PASS]]", "[[ROUTING_AUDIT:REVISE]]"],
            )
            self.assertIn("- verdict: usable_but_drift_prone", reply)
            self.assertIn("- missing: area=docs_pitfalls", reply)
            self.assertNotIn("severity=critical|high|medium|low | title=<short>", reply)
            self.assertNotIn("topic=<short> | owner=<file>", reply)
            self.assertTrue(reply.endswith("[[ROUTING_AUDIT:REVISE]]"))

    def test_extract_reply_from_observation_keeps_only_final_revise_audit_body(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            worker = TmuxBatchWorker(
                worker_id="gemini-worker",
                work_dir=tmp_dir,
                config=AgentRunConfig(vendor="gemini", model="flash"),
                runtime_root=Path(tmp_dir) / "runtime",
            )
            observation = WorkerObservation(
                visible_text="",
                raw_log_delta="",
                raw_log_tail="\n".join(
                    [
                        "⠼ Thinking... (esc to cancel, 14s)   ? for shortcuts",
                        "- verdict: usable_but_drift_prone",
                        "6. - missing",
                        "* - missing: area=escalation_conditions | effect=Agents may not know when to stop | direction=Add stop_and_verify_when.",
                        "- recommendation: minor_structural_fixes",
                        "- top_priority: Add escalation rule",
                        "[[ACX_TURN:f565aae0:DONE]]",
                        "[[ROUTING_AUDIT:REVISE]]",
                    ]
                ),
                current_command="gemini",
                current_path=str(Path(tmp_dir)),
                pane_dead=False,
                session_exists=True,
                log_mtime=0.0,
                observed_at="2026-04-12T00:00:00",
            )
            reply = worker._extract_reply_from_observation(
                observation,
                turn_token="[[ACX_TURN:f565aae0:DONE]]",
                required_tokens=["[[ROUTING_AUDIT:PASS]]", "[[ROUTING_AUDIT:REVISE]]"],
            )
            self.assertIn("- verdict: usable_but_drift_prone", reply)
            self.assertIn("- missing: area=escalation_conditions", reply)
            self.assertIn("- recommendation: minor_structural_fixes", reply)
            self.assertIn("[[ROUTING_AUDIT:REVISE]]", reply)
            self.assertNotIn("Thinking...", reply)

    def test_extract_reply_from_observation_keeps_revise_body_after_turn_token(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            worker = TmuxBatchWorker(
                worker_id="gemini-worker",
                work_dir=tmp_dir,
                config=AgentRunConfig(vendor="gemini", model="flash"),
                runtime_root=Path(tmp_dir) / "runtime",
            )
            observation = WorkerObservation(
                visible_text="",
                raw_log_delta="",
                raw_log_tail="\n".join(
                    [
                        "- verdict: usable_but_drift_prone",
                        "[[ACX_TURN:f565aae0:DONE]]",
                        "- missing: area=escalation_conditions | effect=Agents may not know when to stop | direction=Add stop_and_verify_when.",
                        "- recommendation: minor_structural_fixes",
                        "[[ROUTING_AUDIT:REVISE]]",
                    ]
                ),
                current_command="gemini",
                current_path=str(Path(tmp_dir)),
                pane_dead=False,
                session_exists=True,
                log_mtime=0.0,
                observed_at="2026-04-12T00:00:00",
            )
            reply = worker._extract_reply_from_observation(
                observation,
                turn_token="[[ACX_TURN:f565aae0:DONE]]",
                required_tokens=["[[ROUTING_AUDIT:PASS]]", "[[ROUTING_AUDIT:REVISE]]"],
            )
            self.assertIn("- verdict: usable_but_drift_prone", reply)
            self.assertIn("- missing: area=escalation_conditions", reply)
            self.assertIn("- recommendation: minor_structural_fixes", reply)
            self.assertTrue(reply.endswith("[[ROUTING_AUDIT:REVISE]]"))

    def test_extract_reply_from_observation_ignores_claude_footer_noise_after_revise_token(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            worker = TmuxBatchWorker(
                worker_id="claude-worker",
                work_dir=tmp_dir,
                config=AgentRunConfig(vendor="claude", model="haiku"),
                runtime_root=Path(tmp_dir) / "runtime",
            )
            filler = [f"filler line {index}" for index in range(120)]
            observation = WorkerObservation(
                visible_text="",
                raw_log_delta="",
                raw_log_tail="\n".join(
                    [
                        "- finding: severity=critical|high|medium|low | title=<short> | problem=<short> | impact=<short> | evidence=<path[:line]|...> | direction=<short structural fix>",
                        *filler,
                        "- top_priority: <short>",
                        "- finding: severity=medium | title=Missing pitfalls registry | problem=docs/pitfalls.json is absent | impact=pitfalls become unresolvable | evidence=docs/pitfalls.json | direction=restore docs/pitfalls.json",
                        "- missing: area=Pitfall definitions source file | effect=P01-P05 unresolved | direction=Create docs/pitfalls.json",
                        "- recommendation: minor_structural_fixes",
                        "- top_priority: Restore docs/pitfalls.json",
                        "[[ACX_TURN:testclaude:DONE]]",
                        "[[ROUTING_AUDIT:REVISE]]",
                        "✳ Metamorphosing… (1m 44s · ↓ 1.0k tokens)",
                        "❯",
                        "⏵⏵bypasspermissionson ·escto✗Auto-updatefailed·Tryclaudedocto…",
                        "(·oo·)Crumpet",
                        "Metamorphosing…",
                        "✻ Baked for 1m 44s",
                        "✻ Churned for 1m 9s",
                        "✗ Auto-update faild · Tryclaude doctor or np…",
                        "· Precipitating… (43s · ↓ 1.5k tokens)",
                        "Precipitating…4",
                        "· Hashing… (46s · ↓ 1.2k tokens)",
                        "Hashing…",
                        "✽ Moseying… (38s · ↓ 1.3k tokens)",
                        "✢ Improvising… (38s · ↓ 1.4k tokens)",
                        "✻ Cogitated for 44s",
                        "✻ Sautéed for 46s",
                        "✻ Brewed for 39s",
                    ]
                ),
                current_command="claude",
                current_path=str(Path(tmp_dir)),
                pane_dead=False,
                session_exists=True,
                log_mtime=0.0,
                observed_at="2026-04-12T00:00:00",
            )
            reply = worker._extract_reply_from_observation(
                observation,
                turn_token="[[ACX_TURN:testclaude:DONE]]",
                required_tokens=["[[ROUTING_AUDIT:PASS]]", "[[ROUTING_AUDIT:REVISE]]"],
            )
            self.assertIn("- finding: severity=medium | title=Missing pitfalls registry", reply)
            self.assertIn("- missing: area=Pitfall definitions source file", reply)
            self.assertIn("- recommendation: minor_structural_fixes", reply)
            self.assertIn("- top_priority: Restore docs/pitfalls.json", reply)
            self.assertNotIn("title=<short>", reply)
            self.assertNotIn("<short>", reply)
            self.assertNotIn("Metamorphosing", reply)
            self.assertNotIn("Precipitating", reply)
            self.assertNotIn("Hashing", reply)
            self.assertNotIn("Moseying", reply)
            self.assertNotIn("Improvising", reply)
            self.assertNotIn("Cogitated", reply)
            self.assertNotIn("Sautéed", reply)
            self.assertNotIn("Brewed", reply)
            self.assertNotIn("bypasspermissionson", reply)
            self.assertTrue(reply.endswith("[[ROUTING_AUDIT:REVISE]]"))

    def test_extract_reply_from_observation_accepts_prefixed_turn_token_for_written_audit(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            worker = TmuxBatchWorker(
                worker_id="claude-worker",
                work_dir=tmp_dir,
                config=AgentRunConfig(vendor="claude", model="haiku"),
                runtime_root=Path(tmp_dir) / "runtime",
            )
            observation = WorkerObservation(
                visible_text="\n".join(
                    [
                        "⏺ Write(audit.json)",
                        "⎿ Wrote 7 lines to audit.json",
                        "⏺ [[ACX_TURN:testclaude:DONE]]",
                        "  [[ROUTING_AUDIT:WRITTEN]]",
                        "✻ Churned for 1m 24s",
                        "❯",
                    ]
                ),
                raw_log_delta="",
                raw_log_tail="",
                current_command="claude",
                current_path=str(Path(tmp_dir)),
                pane_dead=False,
                session_exists=True,
                log_mtime=0.0,
                observed_at="2026-04-12T00:00:00",
            )
            reply = worker._extract_reply_from_observation(
                observation,
                turn_token="[[ACX_TURN:testclaude:DONE]]",
                required_tokens=["[[ROUTING_AUDIT:WRITTEN]]"],
            )
            self.assertEqual(reply, "[[ROUTING_AUDIT:WRITTEN]]")

    def test_extract_reply_from_observation_truncates_claude_footer_after_turn_token(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            worker = TmuxBatchWorker(
                worker_id="claude-worker",
                work_dir=tmp_dir,
                config=AgentRunConfig(vendor="claude", model="haiku"),
                runtime_root=Path(tmp_dir) / "runtime",
            )
            observation = WorkerObservation(
                visible_text="\n".join(
                    [
                        "❯ Reply with exactly `OK` on one line.",
                        "",
                        "Turn completion protocol:",
                        "- Output exactly `[[ACX_TURN:testclaude:DONE]]` on its own line before the final workflow token.",
                        "- If no workflow token is required, output `[[ACX_TURN:testclaude:DONE]]` as the final line.",
                        "",
                        "⏺ OK",
                        "",
                        "  [[ACX_TURN:testclaude:DONE]]",
                        "",
                        "────────────────────────────────────────────────────────────────────────────────",
                        "❯",
                        "⏵⏵ bypass permissions on     ✗ Auto-update failed · Try claude doctor or npm …",
                        "(·oo·) Crumpet",
                    ]
                ),
                raw_log_delta="",
                raw_log_tail="",
                current_command="claude",
                current_path=str(Path(tmp_dir)),
                pane_dead=False,
                session_exists=True,
                log_mtime=0.0,
                observed_at="2026-04-12T00:00:00",
            )
            reply = worker._extract_reply_from_observation(
                observation,
                turn_token="[[ACX_TURN:testclaude:DONE]]",
                required_tokens=[],
            )
            self.assertIn("OK", reply)
            self.assertNotIn("Crumpet", reply)
            self.assertNotIn("Auto-update failed", reply)

    def test_codex_extract_last_message_truncates_idle_prompt_footer(self):
        detector = CodexOutputDetector()
        output = "\n".join(
            [
                "• 准备完毕",
                "",
                "",
                "› Use /skills to list available skills",
                "",
                "  gpt-5.4 high fast · ~/Desktop/KevinGit/My_C_Tools",
            ]
        )
        self.assertEqual(detector.extract_last_message(output), "• 准备完毕")

    def test_extract_reply_from_observation_accepts_symbol_prefixed_turn_token_for_written_audit(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            worker = TmuxBatchWorker(
                worker_id="gemini-worker",
                work_dir=tmp_dir,
                config=AgentRunConfig(vendor="gemini", model="flash"),
                runtime_root=Path(tmp_dir) / "runtime",
            )
            observation = WorkerObservation(
                visible_text="\n".join(
                    [
                        "│ ✓  WriteFile Writing to audit.json",
                        "✦ [[ACX_TURN:testgemini:DONE]]",
                        "  [[ROUTING_AUDIT:WRITTEN]]",
                        "? for shortcuts",
                        "*   Type your message or @path/to/file",
                    ]
                ),
                raw_log_delta="",
                raw_log_tail="",
                current_command="gemini",
                current_path=str(Path(tmp_dir)),
                pane_dead=False,
                session_exists=True,
                log_mtime=0.0,
                observed_at="2026-04-12T00:00:00",
            )
            reply = worker._extract_reply_from_observation(
                observation,
                turn_token="[[ACX_TURN:testgemini:DONE]]",
                required_tokens=["[[ROUTING_AUDIT:WRITTEN]]"],
            )
            self.assertEqual(reply, "[[ROUTING_AUDIT:WRITTEN]]")

    def test_extract_reply_from_observation_strips_qwen_tail_noise_after_written_token(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            worker = TmuxBatchWorker(
                worker_id="qwen-worker",
                work_dir=tmp_dir,
                config=AgentRunConfig(vendor="qwen", model="qwen3-coder"),
                runtime_root=Path(tmp_dir) / "runtime",
            )
            observation = WorkerObservation(
                visible_text="",
                raw_log_delta="",
                raw_log_tail="\n".join(
                    [
                        "✦ [[ACX_TURN:testqwen:DONE]]",
                        "[[ROUTING_AUDIT:WRITTEN]]",
                        "⠋ 正在向服务器投喂咖啡... (1m 3s · ↓ 1.7k tokens · esc to cancel)",
                        "*   输入您的消息或 @ 文件路径",
                    ]
                ),
                current_command="node",
                current_path=str(Path(tmp_dir)),
                pane_dead=False,
                session_exists=True,
                log_mtime=0.0,
                observed_at="2026-04-12T00:00:00",
            )
            reply = worker._extract_reply_from_observation(
                observation,
                turn_token="[[ACX_TURN:testqwen:DONE]]",
                required_tokens=["[[ROUTING_AUDIT:WRITTEN]]"],
            )
            self.assertEqual(reply, "[[ROUTING_AUDIT:WRITTEN]]")

    def test_extract_reply_from_observation_preserves_trailing_content_after_final_audit_token(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            worker = TmuxBatchWorker(
                worker_id="gemini-worker",
                work_dir=tmp_dir,
                config=AgentRunConfig(vendor="gemini", model="flash"),
                runtime_root=Path(tmp_dir) / "runtime",
            )
            observation = WorkerObservation(
                visible_text="",
                raw_log_delta="",
                raw_log_tail="\n".join(
                    [
                        "[[ACX_TURN:f565aae0:DONE]]",
                        "[[ROUTING_AUDIT:PASS]]",
                        "summary: trailing prose after final token",
                    ]
                ),
                current_command="gemini",
                current_path=str(Path(tmp_dir)),
                pane_dead=False,
                session_exists=True,
                log_mtime=0.0,
                observed_at="2026-04-12T00:00:00",
            )
            reply = worker._extract_reply_from_observation(
                observation,
                turn_token="[[ACX_TURN:f565aae0:DONE]]",
                required_tokens=["[[ROUTING_AUDIT:PASS]]", "[[ROUTING_AUDIT:REVISE]]"],
            )
            self.assertIn("[[ROUTING_AUDIT:PASS]]", reply)
            self.assertTrue(reply.endswith("summary: trailing prose after final token"))

    def test_extract_reply_from_observation_ignores_stale_previous_audit_turns(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            worker = TmuxBatchWorker(
                worker_id="gemini-worker",
                work_dir=tmp_dir,
                config=AgentRunConfig(vendor="gemini", model="flash"),
                runtime_root=Path(tmp_dir) / "runtime",
            )
            observation = WorkerObservation(
                visible_text="",
                raw_log_delta="",
                raw_log_tail="\n".join(
                    [
                        "- verdict: weak",
                        "- finding: severity=high | title=old finding | problem=stale | impact=stale | evidence=docs/a | direction=ignore",
                        "[[ACX_TURN:oldturn:DONE]]",
                        "[[ROUTING_AUDIT:REVISE]]",
                        "- verdict: usable_but_drift_prone",
                        "- finding: severity=medium | title=current finding | problem=current | impact=current | evidence=docs/b | direction=fix current",
                        "- recommendation: minor_structural_fixes",
                        "[[ACX_TURN:newturn:DONE]]",
                        "[[ROUTING_AUDIT:REVISE]]",
                    ]
                ),
                current_command="gemini",
                current_path=str(Path(tmp_dir)),
                pane_dead=False,
                session_exists=True,
                log_mtime=0.0,
                observed_at="2026-04-12T00:00:00",
            )
            reply = worker._extract_reply_from_observation(
                observation,
                turn_token="[[ACX_TURN:newturn:DONE]]",
                required_tokens=["[[ROUTING_AUDIT:PASS]]", "[[ROUTING_AUDIT:REVISE]]"],
            )
            self.assertIn("current finding", reply)
            self.assertNotIn("old finding", reply)

    def test_extract_reply_from_observation_does_not_fallback_to_generic_for_partial_audit(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            worker = TmuxBatchWorker(
                worker_id="gemini-worker",
                work_dir=tmp_dir,
                config=AgentRunConfig(vendor="gemini", model="flash"),
                runtime_root=Path(tmp_dir) / "runtime",
            )
            observation = WorkerObservation(
                visible_text="\n".join(
                    [
                        "assistant:",
                        "⠼ Thinking... (esc to cancel, 14s)",
                        "[[ACX_TURN:newturn:DONE]]",
                        "[[ROUTING_AUDIT:REVISE]]",
                    ]
                ),
                raw_log_delta="",
                raw_log_tail="\n".join(
                    [
                        "⠼ Thinking... (esc to cancel, 14s)",
                        "[[ACX_TURN:newturn:DONE]]",
                        "[[ROUTING_AUDIT:REVISE]]",
                    ]
                ),
                current_command="gemini",
                current_path=str(Path(tmp_dir)),
                pane_dead=False,
                session_exists=True,
                log_mtime=0.0,
                observed_at="2026-04-12T00:00:00",
            )
            reply = worker._extract_reply_from_observation(
                observation,
                turn_token="[[ACX_TURN:newturn:DONE]]",
                required_tokens=["[[ROUTING_AUDIT:PASS]]", "[[ROUTING_AUDIT:REVISE]]"],
            )
            self.assertEqual(reply, "")

    def test_tmux_backend_tail_raw_log_returns_delta_and_tail(self):
        backend = TmuxBackend()
        with tempfile.TemporaryDirectory() as tmp_dir:
            raw_log_path = Path(tmp_dir) / "worker.raw.log"
            raw_log_path.write_text("alpha\nbeta\n", encoding="utf-8")
            delta, tail, next_offset, _ = backend.tail_raw_log(raw_log_path, last_offset=0, tail_bytes=1024)
            self.assertIn("alpha", delta)
            self.assertIn("beta", tail)
            raw_log_path.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
            delta2, tail2, next_offset2, _ = backend.tail_raw_log(raw_log_path, last_offset=next_offset, tail_bytes=1024)
            self.assertEqual(delta2, "gamma\n")
            self.assertIn("gamma", tail2)
            self.assertGreater(next_offset2, next_offset)

    def test_read_text_tail_handles_missing_and_tail_only(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "demo.txt"
            self.assertEqual(read_text_tail(path), "(文件不存在)")
            path.write_text("a\nb\nc\n", encoding="utf-8")
            self.assertEqual(read_text_tail(path, max_lines=2), "b\nc")

    def test_runtime_controller_uses_backend_for_session_ops(self):
        calls: list[tuple[str, str]] = []

        class FakeBackend:
            def has_session(self, session_name):
                calls.append(("has", session_name))
                return session_name == "demo"

            def attach_session(self, session_name):
                calls.append(("attach", session_name))

            def detach_session(self, session_name):
                calls.append(("detach", session_name))

            def kill_session(self, session_name):
                calls.append(("kill", session_name))

            def list_sessions(self):
                calls.append(("list", ""))
                return ["demo", "other"]

        controller = TmuxRuntimeController(FakeBackend())
        self.assertTrue(controller.session_exists("demo"))
        self.assertEqual(controller.list_sessions(), ["demo", "other"])
        controller.attach_session("demo")
        controller.detach_session("demo")
        controller.kill_session("demo")
        self.assertIn(("attach", "demo"), calls)
        self.assertIn(("detach", "demo"), calls)
        self.assertIn(("kill", "demo"), calls)
        self.assertIn(("list", ""), calls)

    def test_worker_restart_and_kill_are_runtime_level_ops(self):
        class FakeBackend:
            def has_session(self, session_name):
                return True

            def kill_session(self, session_name):
                self.last_killed = session_name

            def run(self, *args, **kwargs):
                raise AssertionError("unexpected tmux run")

        with tempfile.TemporaryDirectory() as tmp_dir:
            backend = FakeBackend()
            worker = TmuxBatchWorker(
                worker_id="ops-worker",
                work_dir=tmp_dir,
                config=AgentRunConfig(vendor="gemini", model="flash"),
                runtime_root=Path(tmp_dir) / "runtime",
                backend=backend,
            )
            worker.agent_ready = True
            session_name = worker.request_restart()
            self.assertEqual(session_name, worker.session_name)
            self.assertFalse(worker.agent_ready)
            self.assertTrue(worker.recoverable)
            self.assertEqual(worker.provider_phase, ProviderPhase.RECOVERING)
            session_name = worker.request_kill()
            self.assertEqual(session_name, worker.session_name)
            self.assertFalse(worker.recoverable)
            self.assertEqual(worker.provider_phase, ProviderPhase.ERROR)

    def test_cleanup_registered_tmux_workers_kills_live_sessions(self):
        class FakeBackend:
            def __init__(self):
                self.live_sessions = {"session-a", "session-b"}
                self.killed: list[str] = []

            def has_session(self, session_name):
                return session_name in self.live_sessions

            def kill_session(self, session_name):
                self.killed.append(session_name)
                self.live_sessions.discard(session_name)

            def run(self, *args, **kwargs):
                raise AssertionError("unexpected tmux run")

        with tempfile.TemporaryDirectory() as tmp_dir:
            backend = FakeBackend()
            worker_a = TmuxBatchWorker(
                worker_id="cleanup-a",
                work_dir=tmp_dir,
                config=AgentRunConfig(vendor="codex", model="gpt-5.4"),
                runtime_root=Path(tmp_dir) / "runtime-a",
                backend=backend,
                existing_session_name="session-a",
            )
            worker_b = TmuxBatchWorker(
                worker_id="cleanup-b",
                work_dir=tmp_dir,
                config=AgentRunConfig(vendor="gemini", model="flash"),
                runtime_root=Path(tmp_dir) / "runtime-b",
                backend=backend,
                existing_session_name="session-b",
            )
            cleaned = cleanup_registered_tmux_workers(reason="unit_test")
            self.assertEqual(sorted(cleaned), ["session-a", "session-b"])
            self.assertEqual(sorted(backend.killed), ["session-a", "session-b"])
            self.assertFalse(worker_a.recoverable)
            self.assertFalse(worker_b.recoverable)

    def test_refresh_health_can_auto_relaunch(self):
        class RelaunchWorker(TmuxBatchWorker):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.ready_calls = 0

            def session_exists(self):
                return False if self.ready_calls == 0 else True

            def ensure_agent_ready(self, timeout_sec=60.0):
                self.ready_calls += 1
                self.agent_ready = True
                self.provider_phase = ProviderPhase.WAITING_INPUT
                self.last_heartbeat_at = "2026-04-12T00:00:00"

        with tempfile.TemporaryDirectory() as tmp_dir:
            worker = RelaunchWorker(
                worker_id="relaunch-worker",
                work_dir=tmp_dir,
                config=AgentRunConfig(vendor="gemini", model="flash"),
                runtime_root=Path(tmp_dir) / "runtime",
            )
            snapshot = worker.refresh_health(auto_relaunch=True, relaunch_timeout_sec=0.1)
            self.assertIsInstance(snapshot, WorkerHealthSnapshot)
            self.assertEqual(snapshot.health_status, "auto_relaunched")
            self.assertEqual(snapshot.provider_phase, ProviderPhase.WAITING_INPUT.value)
            self.assertEqual(worker.ready_calls, 1)

    def test_refresh_health_auto_relaunch_short_circuits_for_unsupported_vendor(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            worker = TmuxBatchWorker(
                worker_id="unsupported-health-worker",
                work_dir=tmp_dir,
                config=AgentRunConfig(vendor="qwen", model="qwen3-coder"),
                runtime_root=Path(tmp_dir) / "runtime",
            )
            snapshot = worker.refresh_health(auto_relaunch=True, relaunch_timeout_sec=0.1)
            self.assertEqual(snapshot.health_status, "unsupported_vendor")
            self.assertEqual(snapshot.health_note, "qwen")

    def test_refresh_health_auto_relaunch_keeps_opencode_supported(self):
        class RelaunchWorker(TmuxBatchWorker):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.ready_calls = 0

            def session_exists(self):
                return False if self.ready_calls == 0 else True

            def ensure_agent_ready(self, timeout_sec=60.0):
                self.ready_calls += 1
                self.agent_ready = True
                self.agent_started = True
                self.provider_phase = ProviderPhase.IDLE_READY
                self.last_heartbeat_at = "2026-04-22T00:00:00"

        with tempfile.TemporaryDirectory() as tmp_dir:
            worker = RelaunchWorker(
                worker_id="opencode-health-worker",
                work_dir=tmp_dir,
                config=AgentRunConfig(vendor="opencode", model="default"),
                runtime_root=Path(tmp_dir) / "runtime",
            )
            snapshot = worker.refresh_health(auto_relaunch=True, relaunch_timeout_sec=0.1)
            self.assertEqual(snapshot.health_status, "auto_relaunched")
            self.assertEqual(snapshot.provider_phase, ProviderPhase.IDLE_READY.value)
            self.assertEqual(worker.ready_calls, 1)

    def test_opencode_output_detector_distinguishes_booting_waiting_processing_and_idle_ready(self):
        detector = OpenCodeOutputDetector()
        booting_phase = detector.classify_phase(
            WorkerObservation(
                visible_text="Performing one time database migration...\nDatabase migration complete.",
                raw_log_delta="",
                raw_log_tail="Performing one time database migration...\nDatabase migration complete.",
                current_command="node",
                current_path="/tmp/project",
                pane_dead=False,
                session_exists=True,
                log_mtime=0.0,
                observed_at="2026-04-22T00:00:00",
                pane_title="OpenCode",
            )
        )
        waiting_phase = detector.classify_phase(
            WorkerObservation(
                visible_text='Ask anything... "Fix a TODO in the codebase"\ntab agents  ctrl+p commands',
                raw_log_delta="",
                raw_log_tail='Ask anything... "Fix a TODO in the codebase"\ntab agents  ctrl+p commands',
                current_command="node",
                current_path="/tmp/project",
                pane_dead=False,
                session_exists=True,
                log_mtime=0.0,
                observed_at="2026-04-22T00:00:01",
                pane_title="OpenCode",
            )
        )
        processing_phase = detector.classify_phase(
            WorkerObservation(
                visible_text="Thinking: The user wants me to reply with exactly OK.\n■■■⬝⬝⬝⬝⬝  esc interrupt",
                raw_log_delta="",
                raw_log_tail="Thinking: The user wants me to reply with exactly OK.\n■■■⬝⬝⬝⬝⬝  esc interrupt",
                current_command="node",
                current_path="/tmp/project",
                pane_dead=False,
                session_exists=True,
                log_mtime=0.0,
                observed_at="2026-04-22T00:00:02",
                pane_title="OpenCode",
            )
        )
        idle_ready_phase = detector.classify_phase(
            WorkerObservation(
                visible_text="OK\n\n10.9K  ctrl+p commands",
                raw_log_delta="",
                raw_log_tail="Thinking: The user wants me to reply with exactly OK.\nOK\n\n10.9K  ctrl+p commands",
                current_command="node",
                current_path="/tmp/project",
                pane_dead=False,
                session_exists=True,
                log_mtime=0.0,
                observed_at="2026-04-22T00:00:03",
                pane_title="OpenCode",
            )
        )
        self.assertEqual(booting_phase, ProviderPhase.BOOTING)
        self.assertEqual(waiting_phase, ProviderPhase.WAITING_INPUT)
        self.assertEqual(processing_phase, ProviderPhase.PROCESSING)
        self.assertEqual(idle_ready_phase, ProviderPhase.IDLE_READY)

    def test_wait_for_agent_ready_supports_opencode_visible_ready_without_title_ready(self):
        class OpenCodeReadyWorker(TmuxBatchWorker):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.observe_calls = 0

            def observe(self, tail_lines=220):
                self.observe_calls += 1
                return WorkerObservation(
                    visible_text='Ask anything... "Fix a TODO in the codebase"\ntab agents  ctrl+p commands',
                    raw_log_delta="",
                    raw_log_tail='Ask anything... "Fix a TODO in the codebase"\ntab agents  ctrl+p commands',
                    current_command="node",
                    current_path=str(self.work_dir),
                    pane_dead=False,
                    session_exists=True,
                    log_mtime=0.0,
                    observed_at="2026-04-22T00:00:00",
                    pane_title="OpenCode",
                )

        with tempfile.TemporaryDirectory() as tmp_dir, mock.patch("canopy_core.runtime.tmux_runtime.time.sleep", return_value=None):
            worker = OpenCodeReadyWorker(
                worker_id="opencode-ready-worker",
                work_dir=tmp_dir,
                config=AgentRunConfig(vendor="opencode", model="default"),
                runtime_root=Path(tmp_dir) / "runtime",
            )
            worker._wait_for_agent_ready(timeout_sec=1.0)
            self.assertTrue(worker.agent_ready)
            self.assertTrue(worker.agent_started)
            self.assertEqual(worker.wrapper_state, WrapperState.READY)
            self.assertEqual(worker.current_command, "node")
            self.assertGreaterEqual(worker.observe_calls, 2)

    def test_worker_does_not_restore_provider_phase_from_state_file(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            runtime_root = Path(tmp_dir) / "runtime"
            worker = TmuxBatchWorker(
                worker_id="phase-state-worker",
                work_dir=tmp_dir,
                config=AgentRunConfig(vendor="codex", model="gpt-5.4-mini"),
                runtime_root=runtime_root,
            )
            worker.state_path.write_text(
                json.dumps(
                    {
                        "session_name": worker.session_name,
                        "pane_id": "%1",
                        "provider_phase": "waiting_input",
                        "agent_started": True,
                        "current_command": "codex",
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            restored = TmuxBatchWorker(
                worker_id="phase-state-worker",
                work_dir=tmp_dir,
                config=AgentRunConfig(vendor="codex", model="gpt-5.4-mini"),
                existing_runtime_dir=worker.runtime_dir,
                existing_session_name=worker.session_name,
                existing_pane_id="%1",
                runtime_root=runtime_root,
            )

            self.assertEqual(restored.provider_phase, ProviderPhase.UNKNOWN)
            self.assertTrue(restored.agent_started)

    def test_passive_health_refresh_updates_state_without_consuming_raw_log(self):
        class PassiveHealthWorker(TmuxBatchWorker):
            def session_exists(self):
                return True

            def target_exists(self, target=None):
                return True

            def capture_visible(self, tail_lines=500):
                return """
› Continue with the current task
  gpt-5.4 high · ~/Desktop/KevinGit/My_C_Tools
"""

            def pane_current_command(self):
                return "codex"

            def pane_current_path(self):
                return str(self.work_dir)

            def pane_dead(self):
                return False

            def tail_raw_log(self, *, tail_bytes=24000):
                raise AssertionError("passive health refresh should not consume raw log")

        with tempfile.TemporaryDirectory() as tmp_dir:
            worker = PassiveHealthWorker(
                worker_id="passive-health-worker",
                work_dir=tmp_dir,
                config=AgentRunConfig(vendor="codex", model="gpt-5.4"),
                runtime_root=Path(tmp_dir) / "runtime",
            )
            worker.pane_id = "%1"
            worker.last_log_offset = 123
            worker._write_state(WorkerStatus.READY, note="seed")

            snapshot = worker.refresh_health()
            state = worker.read_state()

            self.assertEqual(snapshot.health_status, "alive")
            self.assertEqual(snapshot.health_note, ProviderPhase.WAITING_INPUT.value)
            self.assertEqual(state["health_status"], "alive")
            self.assertEqual(state["health_note"], ProviderPhase.WAITING_INPUT.value)
            self.assertEqual(state["current_command"], "codex")
            self.assertEqual(state["current_path"], str(worker.work_dir))
            self.assertEqual(worker.last_log_offset, 123)

    def test_passive_health_refresh_marks_provider_auth_error_from_visible_text(self):
        class PassiveAuthErrorWorker(TmuxBatchWorker):
            def session_exists(self):
                return True

            def target_exists(self, target=None):
                return True

            def capture_visible(self, tail_lines=500):
                return """
╭────────────────────────────────────────────╮
│ >_ OpenAI Codex (v0.120.0)                 │
╰────────────────────────────────────────────╯

[API Error: 401 invalid access token or token expired]
"""

            def pane_current_command(self):
                return "codex"

            def pane_current_path(self):
                return str(self.work_dir)

            def pane_dead(self):
                return False

        with tempfile.TemporaryDirectory() as tmp_dir:
            worker = PassiveAuthErrorWorker(
                worker_id="passive-auth-worker",
                work_dir=tmp_dir,
                config=AgentRunConfig(vendor="codex", model="gpt-5.4"),
                runtime_root=Path(tmp_dir) / "runtime",
            )
            worker.pane_id = "%1"
            worker._write_state(WorkerStatus.READY, note="seed")

            snapshot = worker.refresh_health()
            state = worker.read_state()

            self.assertEqual(snapshot.health_status, "provider_auth_error")
            self.assertEqual(snapshot.health_note, "provider_auth_error")
            self.assertEqual(state["health_status"], "provider_auth_error")
            self.assertEqual(state["health_note"], "provider_auth_error")

    def test_provider_phase_debounce_requires_repeated_ready_observation(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            worker = TmuxBatchWorker(
                worker_id="gemini-worker",
                work_dir=tmp_dir,
                config=AgentRunConfig(vendor="gemini", model="flash"),
                runtime_root=Path(tmp_dir) / "runtime",
            )
            phase1 = worker._debounce_provider_phase(ProviderPhase.WAITING_INPUT)
            time.sleep(0.55)
            phase2 = worker._debounce_provider_phase(ProviderPhase.WAITING_INPUT)
            self.assertEqual(phase1, ProviderPhase.UNKNOWN)
            self.assertEqual(phase2, ProviderPhase.WAITING_INPUT)

    def test_launch_coordinator_backoff_doubles_on_failure_and_resets_on_success(self):
        LaunchCoordinator._stagger_by_vendor.clear()
        self.assertEqual(LaunchCoordinator.current_stagger(Vendor.GEMINI), 2.0)
        LaunchCoordinator.record_launch_result(Vendor.GEMINI, success=False)
        self.assertEqual(LaunchCoordinator.current_stagger(Vendor.GEMINI), 4.0)
        LaunchCoordinator.record_launch_result(Vendor.GEMINI, success=False)
        self.assertEqual(LaunchCoordinator.current_stagger(Vendor.GEMINI), 8.0)
        LaunchCoordinator.record_launch_result(Vendor.GEMINI, success=True)
        self.assertEqual(LaunchCoordinator.current_stagger(Vendor.GEMINI), 2.0)

    def test_run_turn_retries_once_after_timeout(self):
        class RetryOnceWorker(TmuxBatchWorker):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.wait_calls = 0
                self.ready_calls = 0

            def _write_state(self, status, *, note, extra=None):
                return None

            def _append_transcript(self, title, body):
                return None

            def ensure_agent_ready(self, timeout_sec=60.0):
                self.pane_id = "%1"
                self.agent_ready = True
                self.ready_calls += 1

            def observe(self, *, tail_lines=500, tail_bytes=24000):
                return WorkerObservation(
                    visible_text="visible",
                    raw_log_delta="",
                    raw_log_tail="",
                    current_command="codex",
                    current_path=str(self.work_dir),
                    pane_dead=False,
                    session_exists=True,
                    log_mtime=0.0,
                    observed_at="2026-04-12T00:00:00",
                )

            def target_exists(self, target=None):
                return True

            def _send_text(self, text, enter_count=None):
                return None

            def _wait_for_turn_reply(self, **kwargs):
                self.wait_calls += 1
                if self.wait_calls == 1:
                    raise TimeoutError("timed out once")
                return "ok\n[[DONE]]"

        with tempfile.TemporaryDirectory() as tmp_dir:
            worker = RetryOnceWorker(
                worker_id="retry-worker",
                work_dir=tmp_dir,
                config=AgentRunConfig(vendor="codex", model="gpt-5"),
                runtime_root=Path(tmp_dir) / "runtime",
            )
            result = worker.run_turn(label="retry_case", prompt="hello", required_tokens=["[[DONE]]"], timeout_sec=0.05)
            self.assertTrue(result.ok)
            self.assertEqual(worker.wait_calls, 2)
            self.assertGreaterEqual(worker.ready_calls, 2)

    def test_build_turn_prompt_does_not_embed_runtime_context_header(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            worker = TmuxBatchWorker(
                worker_id="plain-prompt-worker",
                work_dir=tmp_dir,
                config=AgentRunConfig(vendor="gemini", model="flash"),
                runtime_root=Path(tmp_dir) / "runtime",
            )
            task_status_path = Path(tmp_dir) / "task_runtime.json"
            submitted = worker._build_turn_prompt(
                "hello world",
                "[[ACX_TURN:test:DONE]]",
                ["[[DONE]]"],
                task_status_path=task_status_path,
                complete_task_command="/tmp/complete_task --status done",
                include_turn_protocol=True,
            )
            self.assertIn("hello world", submitted)
            self.assertNotIn("[Agent Runtime Context]", submitted)
            self.assertNotIn(str(task_status_path.resolve()), submitted)
            self.assertNotIn("complete_task", submitted)




    def test_worker_session_names_are_reserved_across_local_initialization(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            runtime_root = Path(tmp_dir) / "runtime"
            worker_a = TmuxBatchWorker(
                worker_id="requirements-analyst",
                work_dir=tmp_dir,
                config=AgentRunConfig(vendor="codex", model="gpt-5.4-mini"),
                runtime_root=runtime_root,
            )
            worker_b = TmuxBatchWorker(
                worker_id="requirements-analyst",
                work_dir=tmp_dir,
                config=AgentRunConfig(vendor="codex", model="gpt-5.4-mini"),
                runtime_root=runtime_root,
            )
            self.assertNotEqual(worker_a.session_name, worker_b.session_name)

    def test_session_name_reservation_is_released_after_session_creation(self):
        class FakeBackend:
            def __init__(self):
                self.live_sessions: set[str] = set()

            def list_sessions(self):
                return list(self.live_sessions)

            def has_session(self, session_name):
                return session_name in self.live_sessions

            def create_session(self, session_name, work_dir, command):
                self.live_sessions.add(session_name)
                return "%1"

        with tempfile.TemporaryDirectory() as tmp_dir:
            backend = FakeBackend()
            worker = TmuxBatchWorker(
                worker_id="requirements-analyst",
                work_dir=tmp_dir,
                config=AgentRunConfig(vendor="codex", model="gpt-5.4-mini"),
                runtime_root=Path(tmp_dir) / "runtime",
                backend=backend,
            )
            self.assertTrue(worker._session_name_reserved)
            worker._tmux = lambda *args, **kwargs: subprocess.CompletedProcess(["tmux"], 0, "", "")
            worker._start_pipe_logging = lambda: None
            worker._ensure_health_supervisor_started = lambda: None
            worker._refresh_health_state_nonintrusive = lambda: None
            worker._log_event = lambda *args, **kwargs: None
            worker._write_state = lambda *args, **kwargs: None
            worker.create_session()
            self.assertFalse(worker._session_name_reserved)

    def test_session_name_reservation_is_released_when_session_creation_fails(self):
        class FakeBackend:
            def list_sessions(self):
                return []

            def has_session(self, session_name):
                return False

            def create_session(self, session_name, work_dir, command):
                raise RuntimeError("tmux create failed")

        with tempfile.TemporaryDirectory() as tmp_dir:
            worker = TmuxBatchWorker(
                worker_id="requirements-analyst",
                work_dir=tmp_dir,
                config=AgentRunConfig(vendor="codex", model="gpt-5.4-mini"),
                runtime_root=Path(tmp_dir) / "runtime",
                backend=FakeBackend(),
            )
            self.assertTrue(worker._session_name_reserved)
            with self.assertRaisesRegex(RuntimeError, "tmux create failed"):
                worker.create_session()
            self.assertFalse(worker._session_name_reserved)



if __name__ == "__main__":
    unittest.main()
