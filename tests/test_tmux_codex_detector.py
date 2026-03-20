from __future__ import annotations

import unittest

from v1.tmux_cli_tools_lib.common import TerminalStatus
from v1.tmux_cli_tools_lib.detectors import CodexOutputDetector


class CodexDetectorTests(unittest.TestCase):
    def test_trust_prompt_detection_ignores_stale_scrollback(self):
        detector = CodexOutputDetector()
        lines = [
            "> You are in /tmp/project",
            "Do you trust the contents of this directory?",
            "› 1. Yes, continue",
            "  2. No, quit",
            "Press enter to continue",
        ]
        lines.extend(f"warning line {index}" for index in range(40))
        lines.extend(
            [
                "› Improve documentation in @filename",
                "",
                "gpt-5.4 xhigh · 100% left · ~/tmp/project",
            ]
        )

        self.assertFalse(detector.has_trust_prompt("\n".join(lines)))

    def test_extract_last_message_requires_assistant_marker_after_multiline_user_prompt(self):
        detector = CodexOutputDetector()
        output = "\n".join(
            [
                "› $Business Analyst",
                "  你现在是 reviewer analyst。",
                "  你的输出必须以 verdict token 开头。",
                "",
                "gpt-5.4 high · 68% left · ~/tmp/project",
            ]
        )

        with self.assertRaisesRegex(ValueError, "assistant marker"):
            detector.extract_last_message(output)

    def test_extract_last_message_strips_footer_and_returns_reply(self):
        detector = CodexOutputDetector()
        output = "\n".join(
            [
                "› 请记住这些约束",
                "  1) 使用中文",
                "",
                "• 已记录，后续按这些约束执行：",
                "",
                "gpt-5.4 xhigh · 57% left · ~/tmp/project",
            ]
        )

        self.assertEqual(detector.extract_last_message(output), "已记录，后续按这些约束执行：")

    def test_extract_last_message_skips_progress_blocks(self):
        detector = CodexOutputDetector()
        output = "\n".join(
            [
                "› 请说明当前状态",
                "",
                "• Inspecting runtime functionality (2m 32s • esc to interrupt)",
                "",
                "• 当前主执行链路已经确认。",
                "",
                "gpt-5.4 high · 88% left · ~/tmp/project",
            ]
        )

        self.assertEqual(detector.extract_last_message(output), "当前主执行链路已经确认。")

    def test_extract_last_message_keeps_numbered_issue_lines_after_blank_line(self):
        detector = CodexOutputDetector()
        output = "\n".join(
            [
                "› 请输出 reviewer verdict",
                "",
                "• Explored",
                "  └ Read 01_requirement_spec.md",
                "",
                "• [[ACX_VERDICT:REVISE]]",
                "  phase_id: requirement_specification",
                "  owner_id: owner",
                "  task_id: NONE",
                "  artifact_sha: abc123",
                "  review_round: 1",
                "  issues_count: 2",
                "  summary: 仍有两个关键问题。",
                "  issues:",
                "",
                "  1. 需要补充食物不能刷在蛇身上的要求。",
                "  2. 需要补充跨浏览器验证步骤。",
                "",
                "› Implement {feature}",
                "",
                "gpt-5.1-codex-mini low · 94% left · ~/tmp/project",
            ]
        )

        extracted = detector.extract_last_message(output)
        self.assertIn("[[ACX_VERDICT:REVISE]]", extracted)
        self.assertIn("1. 需要补充食物不能刷在蛇身上的要求。", extracted)
        self.assertIn("2. 需要补充跨浏览器验证步骤。", extracted)

    def test_extract_last_message_ignores_queued_message_banner(self):
        detector = CodexOutputDetector()
        output = "\n".join(
            [
                "› 请总结本轮完成情况",
                "",
                "• 已完成需求文档更新，并补充 reviewer 关注的验证细节。",
                "",
                "Messages to be submitted after next tool call (press esc to interrupt and send",
                "immediately)",
                "  ↳ 你上一轮针对阶段 `任务规划` 的回复还不能结束本轮工作。",
                "",
                "› Find and fix a bug in @filename",
                "",
                "gpt-5.1-codex-mini low · 88% left · ~/tmp/project",
            ]
        )

        self.assertEqual(
            detector.extract_last_message(output),
            "已完成需求文档更新，并补充 reviewer 关注的验证细节。",
        )

    def test_model_selection_prompt_detection_returns_waiting_user_answer(self):
        detector = CodexOutputDetector()
        output = "\n".join(
            [
                "  Introducing GPT-5.4",
                "",
                "  Choose how you'd like Codex to proceed.",
                "",
                "› 1. Try new model",
                "  2. Use existing model",
                "",
                "  Use ↑/↓ to move, press enter to confirm",
                "",
                "gpt-5.4 medium · 100% left · ~/tmp/project",
            ]
        )

        self.assertTrue(detector.has_model_selection_prompt(output))
        self.assertEqual(detector.detect_status(output), TerminalStatus.WAITING_USER_ANSWER)

    def test_update_prompt_detection_returns_waiting_user_answer(self):
        detector = CodexOutputDetector()
        output = "\n".join(
            [
                "✨ Update available! 0.115.0 -> 0.116.0",
                "",
                "Release notes: https://github.com/openai/codex/releases/latest",
                "",
                "› 1. Update now (runs `npm install -g @openai/codex`)",
                "  2. Skip",
                "  3. Skip until next version",
                "",
                "Press enter to continue",
            ]
        )

        self.assertTrue(detector.has_update_prompt(output))
        self.assertEqual(detector.detect_status(output), TerminalStatus.WAITING_USER_ANSWER)


if __name__ == "__main__":
    unittest.main()
