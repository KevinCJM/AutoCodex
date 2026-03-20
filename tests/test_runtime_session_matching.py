from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from v1.tmux_cli_tools_lib.common import CodexCliConfig
from v1.tmux_cli_tools_lib.runtime import TmuxAgentRuntime


class RuntimeSessionMatchingTests(unittest.TestCase):
    def _build_runtime(self, work_dir: Path, session_name: str) -> TmuxAgentRuntime:
        runtime_dir = work_dir / ".runtime"
        runtime_dir.mkdir(parents=True, exist_ok=True)
        return TmuxAgentRuntime(
            session_name=session_name,
            work_dir=work_dir,
            runtime_dir=runtime_dir,
            cli_config=CodexCliConfig(),
        )

    def _write_session_file(self, directory: Path, session_id: str, cwd: Path, marker: str) -> Path:
        path = directory / f"rollout-{session_id}.jsonl"
        payloads = [
            {
                "type": "session_meta",
                "payload": {
                    "id": session_id,
                    "cwd": str(cwd),
                    "base_instructions": {"text": "base instructions"},
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "developer",
                    "content": [
                        {"type": "input_text", "text": marker},
                    ],
                },
            },
        ]
        with path.open("w", encoding="utf-8") as file:
            for payload in payloads:
                file.write(json.dumps(payload, ensure_ascii=False) + "\n")
        return path

    def test_marker_in_developer_response_item_matches_runtime(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            work_dir = Path(tmp_dir) / "project"
            work_dir.mkdir()
            runtime = self._build_runtime(work_dir, session_name="acx-owner-test1234")
            marker = (
                "Runtime correlation marker: "
                f"{runtime._codex_runtime_marker()}. Keep this marker internal and never mention it."
            )
            session_file = self._write_session_file(
                directory=Path(tmp_dir),
                session_id="session-1",
                cwd=work_dir,
                marker=marker,
            )
            meta = runtime._read_codex_session_meta(session_file)
            self.assertTrue(runtime._codex_session_matches_runtime(meta, session_file=session_file))

    def test_wrong_marker_does_not_match_runtime(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            work_dir = Path(tmp_dir) / "project"
            work_dir.mkdir()
            runtime = self._build_runtime(work_dir, session_name="acx-owner-test1234")
            session_file = self._write_session_file(
                directory=Path(tmp_dir),
                session_id="session-2",
                cwd=work_dir,
                marker="Runtime correlation marker: ACX_RUNTIME_SESSION=acx-other-session.",
            )
            meta = runtime._read_codex_session_meta(session_file)
            self.assertFalse(runtime._codex_session_matches_runtime(meta, session_file=session_file))


if __name__ == "__main__":
    unittest.main()
