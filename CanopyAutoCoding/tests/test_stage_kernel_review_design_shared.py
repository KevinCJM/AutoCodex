from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from canopy_core.stage_kernel import detailed_design, requirements_review, reviewer_orchestration, shared_review


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class StageKernelSharedTests(unittest.TestCase):
    def test_a04_a05_share_review_support_types(self):
        self.assertIs(requirements_review.ReviewAgentSelection, shared_review.ReviewAgentSelection)
        self.assertIs(detailed_design.ReviewAgentSelection, shared_review.ReviewAgentSelection)
        self.assertIs(requirements_review.ReviewerRuntime, shared_review.ReviewerRuntime)
        self.assertIs(detailed_design.ReviewerRuntime, shared_review.ReviewerRuntime)
        self.assertTrue(issubclass(requirements_review.ReviewStageProgress, shared_review.ReviewStageProgress))
        self.assertIs(detailed_design.ReviewStageProgress, shared_review.ReviewStageProgress)
        self.assertIs(requirements_review.ensure_empty_file, shared_review.ensure_empty_file)
        self.assertIs(detailed_design.ensure_empty_file, shared_review.ensure_empty_file)
        self.assertIs(requirements_review.worker_has_provider_auth_error, shared_review.worker_has_provider_auth_error)
        self.assertIs(detailed_design.worker_has_provider_auth_error, shared_review.worker_has_provider_auth_error)

    def test_a04_a05_delegate_reviewer_orchestration_to_shared_kernel(self):
        review_source = (PROJECT_ROOT / "canopy_core/stage_kernel/requirements_review.py").read_text(encoding="utf-8")
        design_source = (PROJECT_ROOT / "canopy_core/stage_kernel/detailed_design.py").read_text(encoding="utf-8")

        for source in (review_source, design_source):
            self.assertIn("run_parallel_reviewer_round(", source)
            self.assertIn("repair_reviewer_round_outputs(", source)
            self.assertIn("shutdown_stage_workers(", source)

        self.assertTrue(hasattr(reviewer_orchestration, "run_parallel_reviewer_round"))
        self.assertTrue(hasattr(reviewer_orchestration, "repair_reviewer_round_outputs"))
        self.assertTrue(hasattr(reviewer_orchestration, "shutdown_stage_workers"))

    def test_parse_review_max_rounds_supports_default_and_infinite(self):
        self.assertEqual(shared_review.parse_review_max_rounds("", source="--review-max-rounds"), 5)
        self.assertIsNone(shared_review.parse_review_max_rounds("infinite", source="--review-max-rounds"))

    def test_prompt_review_max_rounds_retries_until_valid(self):
        with patch.object(shared_review, "prompt_with_default", side_effect=["abc", "infinite"]) as prompt_mock, patch.object(
            shared_review,
            "message",
        ) as message_mock:
            value = shared_review.prompt_review_max_rounds()

        self.assertIsNone(value)
        self.assertEqual(prompt_mock.call_args_list[0].args[0], "输入最大审核轮次（输入 infinite 表示不设上限）")
        self.assertTrue(any("必须是正整数或 infinite" in str(call.args[0]) for call in message_mock.call_args_list if call.args))

    def test_review_round_policy_resets_quota_without_resetting_initial_flag(self):
        policy = shared_review.ReviewRoundPolicy(max_rounds=2)

        policy.record_review_attempt()
        policy.record_review_attempt()
        self.assertTrue(policy.initial_review_done)
        self.assertTrue(policy.should_escalate_before_next_review())

        policy.reset_after_hitl()
        self.assertEqual(policy.quota_count, 0)
        self.assertTrue(policy.initial_review_done)
        self.assertFalse(policy.should_escalate_before_next_review())

    def test_shell_initialization_timeout_is_recoverable_startup_failure(self):
        error = RuntimeError("Shell initialization timed out.\nzsh prompt")
        self.assertTrue(shared_review.is_recoverable_startup_failure(error))
