from __future__ import annotations

import inspect
import unittest
from pathlib import Path

from tmux_core.stage_kernel.shared_review import ensure_empty_file


class TuiWebProtocolNoAuditTests(unittest.TestCase):
    def test_tui_bridge_and_prompt_contracts_do_not_reference_stage_audit_protocol(self):
        root = Path(__file__).resolve().parents[1]
        searched_roots = [
            root / "tmux_core" / "bridge",
            root / "tmux_core" / "prompt_contracts",
            root / "packages" / "tui" / "src",
        ]
        prompt_files = list(root.glob("Prompt_*.py"))
        banned_tokens = (
            "stage_audit",
            "流水记录",
            "record_index",
            "stage_run_index",
            "review_round_index",
            "hitl_round_index",
        )

        offenders: list[str] = []
        for base_path in [*searched_roots, *prompt_files]:
            files = [base_path] if base_path.is_file() else [item for item in base_path.rglob("*") if item.is_file()]
            for file_path in files:
                if file_path.suffix not in {".py", ".ts", ".tsx", ".js", ".jsx"}:
                    continue
                text = file_path.read_text(encoding="utf-8")
                for token in banned_tokens:
                    if token in text:
                        offenders.append(f"{file_path.relative_to(root)}:{token}")

        self.assertEqual(offenders, [])

    def test_ensure_empty_file_signature_remains_protocol_free(self):
        signature = inspect.signature(ensure_empty_file)
        self.assertEqual(tuple(signature.parameters), ("file_path",))


if __name__ == "__main__":
    unittest.main()
