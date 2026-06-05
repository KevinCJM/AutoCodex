from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from T12_requirements_common import list_existing_requirements
from tmux_core.requirements_scope import (
    CREATE_NEW_REQUIREMENT_SELECTION_VALUE,
    build_requirement_scope_lock_prompt,
    resolve_requirement_name_from_prompt_response,
)


class T12RequirementsCommonTests(unittest.TestCase):
    def test_list_existing_requirements_skips_appledouble_sidecar_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "需求A_原始需求.md").write_text("正文A\n", encoding="utf-8")
            (root / "._需求A_原始需求.md").write_bytes(b"\x00\x05\x16\x07\x00\x02\x00\x00\xb0")

            result = list_existing_requirements(root)

        self.assertEqual(result, ("需求A",))

    def test_list_existing_requirements_skips_non_utf8_candidates(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "需求A_原始需求.md").write_text("正文A\n", encoding="utf-8")
            (root / "坏编码_原始需求.md").write_bytes(b"\xb0not utf-8")

            result = list_existing_requirements(root)

        self.assertEqual(result, ("需求A",))

    def test_build_requirement_scope_lock_prompt_deduplicates_artifacts(self):
        prompt = build_requirement_scope_lock_prompt(
            "/tmp/基金数据生成器_原始需求.md",
            "/tmp/基金数据生成器_原始需求.md",
            "/tmp/基金数据生成器_需求澄清.md",
        )

        self.assertIn("当前任务的唯一需求范围仅限以下文件", prompt)
        self.assertEqual(prompt.count("/tmp/基金数据生成器_原始需求.md"), 1)
        self.assertIn("其他前缀的 sibling 文件", prompt)

    def test_resolve_requirement_name_from_text_prompt_response(self):
        requirement_name = resolve_requirement_name_from_prompt_response(
            prompt_marker="text 输入需求名称",
            payload={"value": "基金数据生成器"},
        )

        self.assertEqual(requirement_name, "基金数据生成器")

    def test_resolve_requirement_name_from_select_prompt_response_accepts_only_real_options(self):
        options = [
            {"value": "基金数据生成器", "label": "基金数据生成器"},
            {"value": CREATE_NEW_REQUIREMENT_SELECTION_VALUE, "label": "创建新需求"},
        ]

        accepted = resolve_requirement_name_from_prompt_response(
            prompt_marker="select 选择已有需求或创建新需求",
            payload={"value": "基金数据生成器"},
            options=options,
        )
        placeholder = resolve_requirement_name_from_prompt_response(
            prompt_marker="select 选择已有需求或创建新需求",
            payload={"value": "现有需求"},
            options=options,
        )
        create_new = resolve_requirement_name_from_prompt_response(
            prompt_marker="select 选择已有需求或创建新需求",
            payload={"value": CREATE_NEW_REQUIREMENT_SELECTION_VALUE},
            options=options,
        )

        self.assertEqual(accepted, "基金数据生成器")
        self.assertIsNone(placeholder)
        self.assertIsNone(create_new)
