# -*- encoding: utf-8 -*-
"""
@File: Prompt_02_RequirementIntake.py
@Modify Time: 2026/4/17
@Author: Kevin-Chen
@Descriptions: 需求录入阶段提示词
"""

from __future__ import annotations

from pathlib import Path


NOTION_STATUS_SCHEMA_VERSION = "1.0"
NOTION_STATUS_OK = "completed"
NOTION_STATUS_HITL = "hitl"
NOTION_STATUS_ERROR = "error"
NOTION_SKILL_PATH = Path("/Users/chenjunming/.codex/skills/notion-api-token-ops/SKILL.md")
NOTION_RUNNER_PATH = Path("/Users/chenjunming/.codex/skills/notion-api-token-ops/scripts/notion_api_token_run.sh")


def get_notion_requirement(
        notion_url,
        original_requirement_md='name_原始需求.md',
        ask_human_md='name_与人类交流.md'):
    get_notion_requirement_prompt = f"""## TASK
$notion-api-token-ops
完整读取指定的 Notion 文档. 目标 URL: {notion_url}
如果 $notion-api-token-ops 技能失败. 改用 notion MCP 尝试.

## if 成功读取指定 URL 的 Notion 内容:
1. 将 Notion 内容输出为本地文件《{original_requirement_md}》, 不做外链文档的下沉获取.
2. 在《{ask_human_md}》中覆盖写入空数据
3. 返回 `完成`

## else:
1. 在《{ask_human_md}》中覆盖写入失败原因
```md
- 以 bullet 格式写明Notion文档信息获取失败的原因
```
2. 在《{original_requirement_md}》中覆盖写入空数据
3. 返回 `失败`

## 要求
- 只能返回 "完成", 禁止返回其他文字
- 禁止修改源代码, 禁止修改除了《{ask_human_md}》和《{original_requirement_md}》之外的文档;
- 如果返回 `完成` 那么《{ask_human_md}》是一个空文件,《{original_requirement_md}》是非空文件;
- 如果返回 `失败` 那么《{ask_human_md}》是一个非空文件,《{original_requirement_md}》是空文件."""
    return get_notion_requirement_prompt
