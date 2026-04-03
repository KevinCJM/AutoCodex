from __future__ import annotations

import sys
from pathlib import Path


AUTO_CODEX_ROOT = Path(__file__).resolve().parents[2]
if str(AUTO_CODEX_ROOT) not in sys.path:
    sys.path.insert(0, str(AUTO_CODEX_ROOT))

from v1.tmux_cli_tools_lib.common import SessionSnapshot, TerminalStatus
from v1.tmux_cli_tools_lib.runtime import TmuxAgentRuntime


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def build_snapshot(timestamp: float, status: TerminalStatus, raw_output: str) -> SessionSnapshot:
    return SessionSnapshot(
        timestamp=timestamp,
        detected_status=status,
        confirmed_status=status,
        current_command="codex",
        current_path="/tmp",
        pane_dead=False,
        raw_output=raw_output,
        clean_output=raw_output,
    )


def test_wait_for_final_reply_waits_for_stable_completed_output(monkeypatch, tmp_path: Path):
    runtime = TmuxAgentRuntime(
        session_name="tmux-test",
        work_dir=tmp_path,
        runtime_dir=tmp_path / "runtime",
    )
    clock = FakeClock()
    snapshots = iter(
        [
            build_snapshot(0.0, TerminalStatus.PROCESSING, "processing"),
            build_snapshot(0.5, TerminalStatus.COMPLETED, "partial"),
            build_snapshot(1.0, TerminalStatus.COMPLETED, "full"),
            build_snapshot(1.5, TerminalStatus.COMPLETED, "full"),
            build_snapshot(2.0, TerminalStatus.COMPLETED, "full"),
        ]
    )
    raw_sizes = iter([100, 180, 180, 180, 180])

    monkeypatch.setattr("v1.tmux_cli_tools_lib.runtime.time.monotonic", clock.monotonic)
    monkeypatch.setattr("v1.tmux_cli_tools_lib.runtime.time.sleep", clock.sleep)
    monkeypatch.setattr(runtime, "take_snapshot", lambda tail_lines=500: next(snapshots))
    monkeypatch.setattr(runtime, "_read_raw_log_size", lambda: next(raw_sizes))
    monkeypatch.setattr(
        runtime,
        "_extract_reply_if_ready",
        lambda output: {
            "partial": "半截回复",
            "full": "完整回复",
        }.get(output, ""),
    )
    monkeypatch.setattr(runtime, "_append_clean_log_snapshot", lambda *args, **kwargs: None)
    monkeypatch.setattr(runtime, "_write_state_file", lambda *args, **kwargs: None)

    final_snapshot = runtime.wait_for_final_reply(timeout_sec=5.0)

    assert final_snapshot.raw_output == "full"
    assert final_snapshot.timestamp == 2.0
