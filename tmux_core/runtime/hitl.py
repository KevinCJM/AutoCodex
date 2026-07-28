# -*- encoding: utf-8 -*-
"""
@File: T05_hitl_runtime.py
@Modify Time: 2026/4/13
@Author: Kevin-Chen
@Descriptions: 通用 HITL 文档协议与 tmux agent 循环运行时
"""

from __future__ import annotations

import hashlib
import inspect
import json
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Mapping, Sequence

from tmux_core.runtime.contracts import TurnFileContract, TurnFileResult
from tmux_core.runtime.tmux_runtime import (
    AgentInterventionRequired,
    AgentStartupInterventionRequired,
    DEFAULT_COMMAND_TIMEOUT_SEC,
    is_turn_artifact_contract_error,
    is_worker_death_error,
)
from T09_terminal_ops import (
    BridgeTerminalUI,
    get_terminal_ui,
    message,
    notify_runtime_state_changed,
    prompt_metadata,
)


HITL_STATUS_SCHEMA_VERSION = "1.0"
HITL_STATUS_COMPLETED = "completed"
HITL_STATUS_HITL = "hitl"
HITL_STATUS_ERROR = "error"
HITL_ALLOWED_STATUSES = {
    HITL_STATUS_COMPLETED,
    HITL_STATUS_HITL,
    HITL_STATUS_ERROR,
}
TURN_STATUS_SCHEMA_VERSION = "1.0"
DEFAULT_HITL_CONTRACT_REPAIR_ATTEMPTS = 2
GRILL_SESSION_SCHEMA_VERSION = "1.0"
GRILL_STANDARD_MODE = "standard"
GRILL_MODE = "grill"
GRILL_WITH_DOCS_MODE = "grill-with-docs"
GRILL_ALLOWED_MODES = {GRILL_STANDARD_MODE, GRILL_MODE, GRILL_WITH_DOCS_MODE}
GRILL_QUESTION_BUDGET = 20
GRILL_DOCUMENT_DRAFT_SCHEMA_VERSION = "1.0"
GRILL_DOCUMENT_PUBLISH_LOCK_SCOPE = "__grill_domain_document_publish__"
GRILL_DOCUMENT_REFERENCES_BEGIN = "<!-- GRILL-DOMAIN-DOCUMENT-REFERENCES:BEGIN -->"
GRILL_DOCUMENT_REFERENCES_END = "<!-- GRILL-DOMAIN-DOCUMENT-REFERENCES:END -->"


class GrillSessionError(RuntimeError):
    """Raised when a persisted Grill session or its controlled documents are invalid."""


class GrillSessionAborted(GrillSessionError):
    """Raised when the human explicitly aborts the Grill session."""


class GrillDocumentConflict(GrillSessionError):
    """Raised instead of overwriting a domain document changed during the interview."""


class GrillContextTargetRequired(GrillSessionError):
    def __init__(self, targets: Sequence[Path]) -> None:
        self.targets = tuple(Path(item).expanduser().resolve() for item in targets)
        super().__init__("CONTEXT-MAP.md 包含多个上下文，需要人类选择发布目标")


@dataclass(frozen=True)
class GrillQuestion:
    question: str
    why_it_matters: str
    recommended_answer: str
    answer_kind: str
    options: tuple[str, ...] = ()
    verified_facts: tuple[str, ...] = ()


@dataclass(frozen=True)
class GrillControlDecision:
    action: str
    message: str = ""


@dataclass(frozen=True)
class GrillSessionHeader:
    requirements_mode: str
    state: str
    active_worker_state_path: str = ""
    active_runtime_dir: str = ""
    active_session_name: str = ""
    active_pane_id: str = ""
    turn_status_path: str = ""
    turn_stage_status_path: str = ""


@dataclass
class GrillSessionState:
    session_id: str
    requirements_mode: str
    state: str = "active"
    turn_seq: int = 0
    question_seq: int = 0
    budget_blocks_approved: int = 1
    force_finalize: bool = False
    pending_question_path: str = ""
    pending_question_hash: str = ""
    pending_answer: str = ""
    accepted_answers: list[dict[str, object]] = field(default_factory=list)
    candidate_turn_id: str = ""
    candidate_round: int = 0
    final_artifact_hashes: dict[str, str] = field(default_factory=dict)
    final_confirmation: dict[str, object] = field(default_factory=dict)
    domain_draft_path: str = ""
    context_map_exists: bool | None = None
    context_map_hash: str = ""
    context_target_snapshot: list[str] = field(default_factory=list)
    context_preimage_hashes: dict[str, str] = field(default_factory=dict)
    selected_context_target: str = ""
    published_paths: list[str] = field(default_factory=list)
    publish_intent: dict[str, object] = field(default_factory=dict)
    active_worker_state_path: str = ""
    active_runtime_dir: str = ""
    active_session_name: str = ""
    active_pane_id: str = ""
    active_worker_generation: str = ""
    turn_id: str = ""
    turn_label: str = ""
    turn_status_path: str = ""
    turn_stage_status_path: str = ""
    turn_submission_cursor: str = ""
    turn_worker_state_revision: int = 0
    turn_prompt_kind: str = ""
    turn_prompt_text: str = ""
    turn_prompt_hash: str = ""
    turn_contract_repair_attempts: int = 0
    turn_manual_intervention_used: bool = False
    turn_fresh_baseline_hashes: dict[str, str] = field(default_factory=dict)
    updated_at: str = ""

    def to_payload(self) -> dict[str, object]:
        payload = asdict(self)
        payload["schema_version"] = GRILL_SESSION_SCHEMA_VERSION
        payload["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
        return payload


@dataclass(frozen=True)
class GrillDocumentDraft:
    context_markdown: str
    adrs: tuple[dict[str, object], ...]


@dataclass(frozen=True)
class HitlPromptContext:
    stage_name: str
    hitl_round: int
    turn_id: str
    turn_phase: str
    output_path: str
    question_path: str
    record_path: str
    stage_status_path: str
    turn_status_path: str


@dataclass(frozen=True)
class HitlStatusDecision:
    stage: str
    turn_id: str
    hitl_round: int
    status: str
    summary: str
    output_path: str
    question_path: str
    record_path: str
    status_path: str
    payload: dict[str, object]
    artifact_hashes: dict[str, str]
    written_at: str


@dataclass(frozen=True)
class HitlLoopResult:
    decision: HitlStatusDecision
    rounds_used: int
    human_responses: tuple[str, ...]


@dataclass(frozen=True)
class HitlLoopPaths:
    output_path: Path
    question_path: Path
    record_path: Path
    stage_status_path: Path
    turns_root: Path


def sha256_file(file_path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(file_path).open("rb") as file:
        for chunk in iter(lambda: file.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_prefixed_sha256(file_path: str | Path) -> str:
    return f"sha256:{sha256_file(file_path)}"


def parse_iso_timestamp(text: str) -> str:
    value = str(text or "").strip()
    if not value:
        raise ValueError("written_at 不能为空")
    datetime.fromisoformat(value.replace("Z", "+00:00"))
    return value


def _collect_artifact_paths(node: object) -> list[str]:
    flattened: list[str] = []
    if isinstance(node, dict):
        for value in node.values():
            flattened.extend(_collect_artifact_paths(value))
        return flattened
    if isinstance(node, (list, tuple, set)):
        for value in node:
            flattened.extend(_collect_artifact_paths(value))
        return flattened
    if node is None:
        return flattened
    text = str(node).strip()
    if text:
        flattened.append(text)
    return flattened


def _write_json_atomic(path: str | Path, payload: dict[str, object]) -> Path:
    target_path = Path(path).expanduser().resolve()
    target_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = target_path.with_name(f".{target_path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temp_path.replace(target_path)
    finally:
        if temp_path.exists():
            temp_path.unlink()
    return target_path


def _write_text_atomic(path: str | Path, text: str) -> Path:
    target_path = Path(path).expanduser().resolve()
    target_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = target_path.with_name(f".{target_path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temp_path.write_text(str(text), encoding="utf-8")
        temp_path.replace(target_path)
    finally:
        if temp_path.exists():
            temp_path.unlink()
    return target_path


def normalize_requirements_mode_value(value: object) -> str:
    normalized = str(value or GRILL_STANDARD_MODE).strip().lower().replace("_", "-")
    if normalized not in GRILL_ALLOWED_MODES:
        choices = ", ".join(sorted(GRILL_ALLOWED_MODES))
        raise ValueError(f"requirements_mode 非法: {value!r}; 可选值: {choices}")
    return normalized


def read_grill_session_header(session_path: str | Path) -> GrillSessionHeader | None:
    """Read only the mode/lifecycle header needed before interactive mode selection."""

    path = Path(session_path).expanduser().resolve()
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise GrillSessionError("Grill session.json 必须是 JSON 对象")
    if str(payload.get("schema_version", "")).strip() != GRILL_SESSION_SCHEMA_VERSION:
        raise GrillSessionError("Grill session.json schema_version 非法")
    state = str(payload.get("state", "active") or "active").strip()
    if not state:
        raise GrillSessionError("Grill session.json state 不能为空")
    return GrillSessionHeader(
        requirements_mode=normalize_requirements_mode_value(payload.get("requirements_mode")),
        state=state,
        active_worker_state_path=str(payload.get("active_worker_state_path", "") or "").strip(),
        active_runtime_dir=str(payload.get("active_runtime_dir", "") or "").strip(),
        active_session_name=str(payload.get("active_session_name", "") or "").strip(),
        active_pane_id=str(payload.get("active_pane_id", "") or "").strip(),
        turn_status_path=str(payload.get("turn_status_path", "") or "").strip(),
        turn_stage_status_path=str(
            payload.get("turn_stage_status_path", "") or ""
        ).strip(),
    )


def load_grill_session_state(
    session_path: str | Path,
    *,
    requirements_mode: str,
    domain_draft_path: str | Path | None = None,
) -> GrillSessionState:
    mode = normalize_requirements_mode_value(requirements_mode)
    path = Path(session_path).expanduser().resolve()
    if not path.exists():
        return GrillSessionState(
            session_id=uuid.uuid4().hex,
            requirements_mode=mode,
            domain_draft_path=(
                str(Path(domain_draft_path).expanduser().resolve()) if domain_draft_path else ""
            ),
        )
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise GrillSessionError("Grill session.json 必须是 JSON 对象")
    if str(payload.get("schema_version", "")).strip() != GRILL_SESSION_SCHEMA_VERSION:
        raise GrillSessionError("Grill session.json schema_version 非法")
    persisted_mode = normalize_requirements_mode_value(payload.get("requirements_mode"))
    if persisted_mode != mode:
        raise GrillSessionError(
            f"活跃 Grill 会话禁止切换模式: persisted={persisted_mode}, requested={mode}"
        )
    session_id = str(payload.get("session_id", "")).strip()
    if not session_id:
        raise GrillSessionError("Grill session.json 缺少 session_id")
    accepted_answers = payload.get("accepted_answers", [])
    final_hashes = payload.get("final_artifact_hashes", {})
    final_confirmation = payload.get("final_confirmation", {})
    context_hashes = payload.get("context_preimage_hashes", {})
    context_target_snapshot = payload.get("context_target_snapshot", [])
    published_paths = payload.get("published_paths", [])
    publish_intent = payload.get("publish_intent", {})
    turn_fresh_baseline_hashes = payload.get("turn_fresh_baseline_hashes", {})
    if not isinstance(accepted_answers, list):
        raise GrillSessionError("Grill session.json accepted_answers 必须是数组")
    if not isinstance(final_hashes, dict) or not isinstance(context_hashes, dict):
        raise GrillSessionError("Grill session.json 哈希字段必须是对象")
    if not isinstance(final_confirmation, dict):
        raise GrillSessionError("Grill session.json final_confirmation 必须是对象")
    raw_context_map_exists = payload.get("context_map_exists")
    if raw_context_map_exists is not None and not isinstance(raw_context_map_exists, bool):
        raise GrillSessionError("Grill session.json context_map_exists 必须是布尔值或 null")
    if not isinstance(context_target_snapshot, list):
        raise GrillSessionError("Grill session.json context_target_snapshot 必须是数组")
    if not isinstance(published_paths, list):
        raise GrillSessionError("Grill session.json published_paths 必须是数组")
    if not isinstance(publish_intent, dict):
        raise GrillSessionError("Grill session.json publish_intent 必须是对象")
    if not isinstance(turn_fresh_baseline_hashes, dict):
        raise GrillSessionError("Grill session.json turn_fresh_baseline_hashes 必须是对象")
    state = GrillSessionState(
        session_id=session_id,
        requirements_mode=persisted_mode,
        state=str(payload.get("state", "active") or "active").strip(),
        turn_seq=max(int(payload.get("turn_seq", 0)), 0),
        question_seq=max(int(payload.get("question_seq", 0)), 0),
        budget_blocks_approved=max(int(payload.get("budget_blocks_approved", 1)), 1),
        force_finalize=bool(payload.get("force_finalize", False)),
        pending_question_path=str(payload.get("pending_question_path", "") or "").strip(),
        pending_question_hash=str(payload.get("pending_question_hash", "") or "").strip(),
        pending_answer=str(payload.get("pending_answer", "") or "").strip(),
        accepted_answers=[dict(item) for item in accepted_answers if isinstance(item, dict)],
        candidate_turn_id=str(payload.get("candidate_turn_id", "") or "").strip(),
        candidate_round=max(int(payload.get("candidate_round", 0)), 0),
        final_artifact_hashes={str(key): str(value) for key, value in final_hashes.items()},
        final_confirmation=dict(final_confirmation),
        domain_draft_path=str(
            payload.get("domain_draft_path", "")
            or (str(Path(domain_draft_path).expanduser().resolve()) if domain_draft_path else "")
        ).strip(),
        context_map_exists=raw_context_map_exists,
        context_map_hash=str(payload.get("context_map_hash", "") or "").strip(),
        context_target_snapshot=[
            str(item).strip() for item in context_target_snapshot if str(item).strip()
        ],
        context_preimage_hashes={str(key): str(value) for key, value in context_hashes.items()},
        selected_context_target=str(payload.get("selected_context_target", "") or "").strip(),
        published_paths=[str(item) for item in published_paths if str(item).strip()],
        publish_intent=dict(publish_intent),
        active_worker_state_path=str(
            payload.get("active_worker_state_path", "") or ""
        ).strip(),
        active_runtime_dir=str(payload.get("active_runtime_dir", "") or "").strip(),
        active_session_name=str(payload.get("active_session_name", "") or "").strip(),
        active_pane_id=str(payload.get("active_pane_id", "") or "").strip(),
        active_worker_generation=str(
            payload.get("active_worker_generation", "") or ""
        ).strip(),
        turn_id=str(payload.get("turn_id", "") or "").strip(),
        turn_label=str(payload.get("turn_label", "") or "").strip(),
        turn_status_path=str(payload.get("turn_status_path", "") or "").strip(),
        turn_stage_status_path=str(
            payload.get("turn_stage_status_path", "") or ""
        ).strip(),
        turn_submission_cursor=str(
            payload.get("turn_submission_cursor", "") or ""
        ).strip(),
        turn_worker_state_revision=max(
            int(payload.get("turn_worker_state_revision", 0) or 0),
            0,
        ),
        turn_prompt_kind=str(payload.get("turn_prompt_kind", "") or "").strip(),
        turn_prompt_text=str(payload.get("turn_prompt_text", "") or ""),
        turn_prompt_hash=str(payload.get("turn_prompt_hash", "") or "").strip(),
        turn_contract_repair_attempts=max(
            int(payload.get("turn_contract_repair_attempts", 0) or 0),
            0,
        ),
        turn_manual_intervention_used=bool(
            payload.get("turn_manual_intervention_used", False)
        ),
        turn_fresh_baseline_hashes={
            str(key): str(value)
            for key, value in turn_fresh_baseline_hashes.items()
        },
        updated_at=str(payload.get("updated_at", "") or "").strip(),
    )
    if state.pending_question_path:
        question_path = Path(state.pending_question_path).expanduser().resolve()
        if not question_path.exists() or not question_path.is_file():
            raise GrillSessionError(f"Grill 待回答问题文件不存在: {question_path}")
        if state.pending_question_hash != build_prefixed_sha256(question_path):
            raise GrillSessionError("Grill 待回答问题文件已变化，拒绝恢复旧问题")
    return state


def save_grill_session_state(session_path: str | Path, state: GrillSessionState) -> Path:
    state.requirements_mode = normalize_requirements_mode_value(state.requirements_mode)
    return _write_json_atomic(session_path, state.to_payload())


_GRILL_HEADING_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)


def _grill_markdown_sections(text: str) -> dict[str, str]:
    matches = list(_GRILL_HEADING_RE.finditer(str(text or "")))
    sections: dict[str, str] = {}
    for index, match in enumerate(matches):
        key = match.group(1).strip()
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        if key in sections:
            raise GrillSessionError(f"Grill 问题文档包含重复章节: {key}")
        sections[key] = text[start:end].strip()
    return sections


def validate_grill_question_file(question_path: str | Path) -> GrillQuestion:
    path = Path(question_path).expanduser().resolve()
    text = _read_non_empty_text(path)
    if not text:
        raise GrillSessionError("Grill 问题文档为空")
    sections = _grill_markdown_sections(text)
    aliases = {
        "question": ("问题", "Question"),
        "why": ("为什么需要决定", "Why It Matters"),
        "recommendation": ("推荐答案", "Recommended Answer"),
        "kind": ("回答方式", "Answer Kind"),
        "options": ("选项", "Options"),
        "facts": ("已核实事实", "Verified Facts"),
    }

    def _section(name: str, *, required: bool = True) -> str:
        for alias in aliases[name]:
            if alias in sections:
                value = sections[alias].strip()
                if value or not required:
                    return value
        if required:
            raise GrillSessionError(f"Grill 问题文档缺少章节: {aliases[name][0]}")
        return ""

    def _has_section(name: str) -> bool:
        return any(alias in sections for alias in aliases[name])

    question = _section("question")
    why = _section("why")
    recommendation = _section("recommendation")
    kind = _section("kind").splitlines()[0].strip().lower().replace("_", "-")
    if kind not in {"select", "multiline"}:
        raise GrillSessionError("Grill 回答方式必须是 select 或 multiline")
    question_lines = [line.strip() for line in question.splitlines() if line.strip()]
    if len(question_lines) != 1 or question_lines[0].startswith(("- ", "* ", "1. ")):
        raise GrillSessionError("Grill 每轮必须只包含一个独立问题")
    if question.count("?") + question.count("？") > 1:
        raise GrillSessionError("Grill 每轮不得合并多个问题")

    if not _has_section("options"):
        raise GrillSessionError("Grill 问题文档缺少章节: 选项")
    option_text = _section("options", required=False)
    options = tuple(
        line.lstrip("-* ").strip()
        for line in option_text.splitlines()
        if line.strip().startswith(("-", "*")) and line.lstrip("-* ").strip()
    )
    if options and not 2 <= len(options) <= 4:
        raise GrillSessionError("Grill select 问题必须提供 2 到 4 个选项")
    if options and kind != "select":
        raise GrillSessionError("Grill 存在明确选项时回答方式必须是 select")
    if kind == "select" and not 2 <= len(options) <= 4:
        raise GrillSessionError("Grill select 问题必须提供 2 到 4 个选项")
    if kind == "select" and recommendation not in options:
        raise GrillSessionError("Grill select 的推荐答案必须精确匹配其中一个选项")
    if not _has_section("facts"):
        raise GrillSessionError("Grill 问题文档缺少章节: 已核实事实")
    fact_text = _section("facts")
    verified_facts = tuple(
        line.lstrip("-* ").strip()
        for line in fact_text.splitlines()
        if line.strip().startswith(("-", "*")) and line.lstrip("-* ").strip()
    )
    if not verified_facts:
        verified_facts = tuple(line.strip() for line in fact_text.splitlines() if line.strip())
    return GrillQuestion(
        question=question_lines[0],
        why_it_matters=why,
        recommended_answer=recommendation,
        answer_kind=kind,
        options=options,
        verified_facts=verified_facts,
    )


def collect_grill_hitl_response(
    question_path: str | Path,
    *,
    hitl_round: int,
    question_index: int,
) -> str:
    question_file = Path(question_path).expanduser().resolve()
    question = validate_grill_question_file(question_file)
    ui = get_terminal_ui()
    metadata = {
        "interaction_kind": "grill",
        "question_index": question_index,
        "question_id": f"grill:{build_prefixed_sha256(question_file).split(':', 1)[-1][:16]}:{question_index}",
        "recommendation": question.recommended_answer,
        "reason_text": question.why_it_matters,
        "recovery_kind": "grill_decision",
    }
    if not isinstance(ui, BridgeTerminalUI):
        message()
        message(f"Grill 第 {question_index} 题")
        message(f"问题: {question.question}")
        message(f"推荐答案: {question.recommended_answer}")
        message(f"为什么需要决定: {question.why_it_matters}")
    if question.answer_kind == "select":
        options = tuple((f"option_{index}", item) for index, item in enumerate(question.options, start=1))
        options += (("custom", "自定义回答"),)
        default_value = next(
            value for value, label in options if label == question.recommended_answer
        )
        value = ui.prompt_select(
            title=f"Grill 第 {question_index} 题：{question.question}",
            options=options,
            default_value=default_value,
            prompt_text="请选择回答",
            preview_path=question_file,
            preview_title=f"Grill 第 {question_index} 题",
            is_hitl=True,
            extra_payload=metadata,
        )
        if value != "custom":
            selected = dict(options)[value]
            return selected
    with prompt_metadata(**metadata):
        return ui.prompt_multiline(
            title=f"Grill 第 {question_index} 题回复",
            empty_retry_message="回复不能为空，请重新输入。",
            question_path=question_file,
            is_hitl=True,
        )


def collect_grill_final_confirmation(
    output_path: str | Path,
    *,
    question_index: int,
) -> GrillControlDecision:
    output_file = Path(output_path).expanduser().resolve()
    ui = get_terminal_ui()
    preview_text = output_file.read_text(encoding="utf-8") if output_file.exists() else ""
    includes_documents = "## 待发布领域文档" in preview_text
    value = ui.prompt_select(
        title=(
            "Grill 已形成共享理解及领域文档发布候选，请由人类确认"
            if includes_documents
            else "Grill 已形成共享理解候选，请由人类确认"
        ),
        options=(
            (
                "confirm",
                "确认共享理解并发布所示 CONTEXT/ADR"
                if includes_documents
                else "确认共享理解并继续",
            ),
            ("continue", "继续追问"),
            ("revise", "修改已有答案"),
            ("abort", "终止本阶段"),
        ),
        default_value="confirm",
        prompt_text="请选择",
        preview_path=output_file,
        preview_title="需求澄清候选",
        is_hitl=True,
        extra_payload={
            "interaction_kind": "grill",
            "question_index": question_index,
            "recommendation": (
                "确认共享理解并发布所示 CONTEXT/ADR"
                if includes_documents
                else "确认共享理解并继续"
            ),
            "reason_text": (
                "预览同时包含需求澄清候选和全部待发布领域文档；只有人类确认后才会发布并完成阶段。"
                if includes_documents
                else "只有人类确认后，需求澄清阶段才允许完成。"
            ),
            "recovery_kind": "grill_decision",
        },
    )
    if value == "revise":
        with prompt_metadata(
            interaction_kind="grill",
            question_index=question_index,
            recovery_kind="grill_decision",
        ):
            revision = ui.prompt_multiline(
                title="请说明需要修改的已有答案",
                empty_retry_message="修改说明不能为空。",
                question_path=output_file,
                is_hitl=True,
            )
        return GrillControlDecision("revise", revision.strip())
    return GrillControlDecision(value)


def build_grill_confirmation_preview(
    *,
    output_path: str | Path,
    preview_path: str | Path,
    domain_draft_path: str | Path | None = None,
) -> Path:
    output_file = Path(output_path).expanduser().resolve()
    output_text = _read_non_empty_text(output_file)
    if not output_text:
        raise GrillSessionError("Grill 最终确认缺少需求澄清候选")
    sections = ["# Grill 最终确认预览", "## 需求澄清候选", output_text]
    if domain_draft_path:
        draft = validate_grill_document_draft(domain_draft_path)
        if draft.context_markdown or draft.adrs:
            sections.extend(["## 待发布领域文档"])
            if draft.context_markdown:
                sections.extend(["### CONTEXT.md", draft.context_markdown])
            for index, adr in enumerate(draft.adrs, start=1):
                sections.extend(
                    [
                        f"### ADR 候选 {index}: {adr['title']}",
                        str(adr["markdown"]),
                    ]
                )
    return _write_text_atomic(preview_path, "\n\n".join(sections).rstrip() + "\n")


def collect_grill_budget_decision(*, question_index: int) -> GrillControlDecision:
    ui = get_terminal_ui()
    value = ui.prompt_select(
        title=f"Grill 已完成 {question_index} 个问题，本预算块已用完",
        options=(
            ("continue", "继续下一组问题"),
            ("finalize", "带明确未决项进入最终确认"),
            ("abort", "终止本阶段"),
        ),
        default_value="continue",
        prompt_text="请选择",
        is_hitl=True,
        extra_payload={
            "interaction_kind": "grill",
            "question_index": question_index,
            "recommendation": "继续下一组问题",
            "reason_text": "问题预算只控制交互节奏，不会自动判定阶段失败。",
            "recovery_kind": "grill_decision",
        },
    )
    return GrillControlDecision(value)


def collect_grill_context_target(targets: Sequence[Path]) -> str:
    normalized = tuple(Path(item).expanduser().resolve() for item in targets)
    if not normalized:
        raise GrillSessionError("没有可选的 CONTEXT.md 发布目标")
    ui = get_terminal_ui()
    return ui.prompt_select(
        title="Grill with Docs 需要选择领域上下文",
        options=tuple((str(path), str(path)) for path in normalized),
        default_value=str(normalized[0]),
        prompt_text="请选择 CONTEXT.md 发布目标",
        is_hitl=True,
        extra_payload={
            "interaction_kind": "grill",
            "recommendation": str(normalized[0]),
            "reason_text": "CONTEXT-MAP.md 声明了多个领域上下文，系统不能替人类猜测归属。",
            "recovery_kind": "grill_decision",
        },
    )


def collect_grill_document_conflict_decision(error_text: str) -> GrillControlDecision:
    ui = get_terminal_ui()
    value = ui.prompt_select(
        title="Grill with Docs 发布发生并发冲突",
        options=(
            ("rebuild", "重新读取并合并草稿"),
            ("skip", "跳过领域文档发布"),
            ("abort", "终止本阶段"),
        ),
        default_value="rebuild",
        prompt_text="请选择",
        is_hitl=True,
        extra_payload={
            "interaction_kind": "grill",
            "recommendation": "重新读取并合并草稿",
            "reason_text": str(error_text).strip(),
            "recovery_kind": "grill_decision",
        },
    )
    return GrillControlDecision(value)


def _resolve_project_owned_publish_path(
    project_dir: str | Path,
    candidate: str | Path,
    *,
    label: str,
) -> Path:
    """Resolve symlinks before any document read/write and enforce project ownership."""

    project_root = Path(project_dir).expanduser().resolve()
    raw_path = Path(candidate).expanduser()
    if not raw_path.is_absolute():
        raw_path = project_root / raw_path
    resolved = raw_path.resolve()
    try:
        resolved.relative_to(project_root)
    except ValueError as error:
        raise GrillSessionError(f"{label} 路径越界或通过符号链接指向项目外: {raw_path}") from error
    return resolved


def _grill_context_map_state(project_dir: str | Path) -> tuple[bool, str, Path | None]:
    project_root = Path(project_dir).expanduser().resolve()
    lexical_path = project_root / "CONTEXT-MAP.md"
    if not lexical_path.exists() and not lexical_path.is_symlink():
        return False, "", None
    resolved = _resolve_project_owned_publish_path(
        project_root,
        lexical_path,
        label="CONTEXT-MAP.md",
    )
    if not resolved.exists() or not resolved.is_file():
        raise GrillSessionError(f"CONTEXT-MAP.md 不是可读取文件: {resolved}")
    return True, build_prefixed_sha256(resolved), resolved


def resolve_grill_context_targets(project_dir: str | Path) -> tuple[Path, ...]:
    project_root = Path(project_dir).expanduser().resolve()
    map_exists, _map_hash, context_map = _grill_context_map_state(project_root)
    if not map_exists:
        return (
            _resolve_project_owned_publish_path(
                project_root,
                project_root / "CONTEXT.md",
                label="CONTEXT.md",
            ),
        )
    assert context_map is not None
    text = context_map.read_text(encoding="utf-8")
    links = re.findall(r"\[[^\]]+\]\(([^)]+)\)", text)
    targets: list[Path] = []
    for raw_link in links:
        link = raw_link.strip().split("#", 1)[0].strip().strip("<>")
        path_part = link.split("?", 1)[0].strip()
        looks_like_context = Path(path_part).name == "CONTEXT.md" if path_part else False
        if not path_part:
            continue
        if "://" in path_part or path_part.startswith(("/", "~")):
            if looks_like_context:
                raise GrillSessionError(f"CONTEXT-MAP.md 包含非项目内 CONTEXT.md: {raw_link}")
            continue
        candidate = _resolve_project_owned_publish_path(
            project_root,
            project_root / path_part,
            label="CONTEXT-MAP.md 引用",
        )
        if candidate.name != "CONTEXT.md":
            continue
        if candidate not in targets:
            targets.append(candidate)
    if not targets:
        raise GrillSessionError("CONTEXT-MAP.md 未引用任何项目内 CONTEXT.md")
    return tuple(targets)


def capture_grill_context_snapshot(project_dir: str | Path) -> dict[str, object]:
    project_root = Path(project_dir).expanduser().resolve()
    map_exists, map_hash, _context_map = _grill_context_map_state(project_root)
    targets = resolve_grill_context_targets(project_root)
    target_hashes = {
        str(path): build_prefixed_sha256(path) if path.exists() and path.is_file() else ""
        for path in targets
    }
    return {
        "map_exists": map_exists,
        "map_hash": map_hash,
        "targets": [str(path) for path in targets],
        "target_hashes": target_hashes,
    }


def capture_grill_context_preimages(project_dir: str | Path) -> dict[str, str]:
    snapshot = capture_grill_context_snapshot(project_dir)
    return dict(snapshot["target_hashes"])  # type: ignore[arg-type]


def apply_grill_context_snapshot(
    state: GrillSessionState,
    project_dir: str | Path,
) -> dict[str, object]:
    snapshot = capture_grill_context_snapshot(project_dir)
    state.context_map_exists = bool(snapshot["map_exists"])
    state.context_map_hash = str(snapshot["map_hash"])
    state.context_target_snapshot = [str(item) for item in snapshot["targets"]]  # type: ignore[union-attr]
    state.context_preimage_hashes = {
        str(key): str(value)
        for key, value in dict(snapshot["target_hashes"]).items()  # type: ignore[arg-type]
    }
    return snapshot


def grill_context_snapshot_complete(state: GrillSessionState) -> bool:
    if state.context_map_exists is None or not state.context_target_snapshot:
        return False
    if state.context_map_exists and not state.context_map_hash:
        return False
    if not state.context_map_exists and state.context_map_hash:
        return False
    return set(state.context_target_snapshot) == set(state.context_preimage_hashes)


_CONTEXT_TERM_RE = re.compile(r"^\*\*([^*:：\n]+)\*\*\s*[:：]\s*(.*)$")
_CONTEXT_AVOID_RE = re.compile(r"^_(?:Avoid|避免|不使用)_\s*[:：]\s*(.+)$", re.IGNORECASE)
_CONTEXT_LANGUAGE_HEADINGS = {"language", "语言", "领域语言", "术语"}
_CONTEXT_IMPLEMENTATION_RE = re.compile(
    r"(?i)(?:\b(?:api|endpoint|modules?|files?|class|function|method|route|router|"
    r"implementation|source\s+code|tests?|configs?|configuration|deployment|"
    r"kubernetes|docker|pods?|database|sql|orm|framework|library|http|grpc|"
    r"python|javascript|typescript|golang|rust|redis|postgres(?:ql)?|mysql)\b|"
    r"(?:^|[\s`(])(?:src|tests?|docs|config)/|"
    r"\.(?:py|js|ts|tsx|jsx|java|go|rs|json|ya?ml)(?:\b|`)|"
    r"实现|代码|文件|模块|接口|路由|端点|类名|函数|方法|测试|配置|部署|"
    r"数据库|数据表|字段|容器|服务端|客户端|框架|依赖)"
)


def _context_sentence_count(text: str) -> int:
    return len(
        [
            item
            for item in re.findall(r"[^.!?。！？]+(?:[.!?。！？]+|$)", str(text).strip())
            if item.strip(" .!?。！？\t\r\n")
        ]
    )


def _validate_grill_context_markdown(context_markdown: str) -> None:
    """Validate the upstream glossary format with a positive, line-oriented grammar."""

    text = str(context_markdown or "").replace("\r\n", "\n").strip()
    if not text:
        return
    if "```" in text or "`" in text or re.search(r"\[[^\]]+\]\([^)]+\)", text):
        raise GrillSessionError("CONTEXT 草稿只能使用纯领域词汇表格式，不能包含代码或链接")
    if _CONTEXT_IMPLEMENTATION_RE.search(text):
        raise GrillSessionError("CONTEXT 草稿正文包含实现、文件、模块、API 或路由事实")

    lines = text.splitlines()
    nonempty_indexes = [index for index, line in enumerate(lines) if line.strip()]
    if not nonempty_indexes or not re.fullmatch(r"#\s+[^#].*", lines[nonempty_indexes[0]].strip()):
        raise GrillSessionError("CONTEXT 草稿必须以唯一的一级上下文标题开始")
    if sum(1 for line in lines if re.match(r"^#\s+", line.strip())) != 1:
        raise GrillSessionError("CONTEXT 草稿只能包含一个一级上下文标题")

    language_indexes = [
        index
        for index, line in enumerate(lines)
        if re.fullmatch(r"##\s+(.+?)\s*", line.strip())
        and re.fullmatch(r"##\s+(.+?)\s*", line.strip()).group(1).strip().lower()  # type: ignore[union-attr]
        in _CONTEXT_LANGUAGE_HEADINGS
    ]
    if len(language_indexes) != 1:
        raise GrillSessionError("CONTEXT 草稿必须且只能包含一个 ## Language（或中文等价标题）")
    language_index = language_indexes[0]
    title_index = nonempty_indexes[0]
    description_lines = [line.strip() for line in lines[title_index + 1 : language_index] if line.strip()]
    if any(line.startswith("#") or re.match(r"^(?:[-*+]|>|\d+[.)])\s+", line) for line in description_lines):
        raise GrillSessionError("CONTEXT 上下文说明必须是普通的一到两句文本")
    description = " ".join(description_lines)
    if description and not 1 <= _context_sentence_count(description) <= 2:
        raise GrillSessionError("CONTEXT 上下文说明最多两句话")

    seen_terms: set[str] = set()
    current_term = ""
    current_definition: list[str] = []
    current_avoid = ""

    def flush_term() -> None:
        nonlocal current_term, current_definition, current_avoid
        if not current_term:
            return
        definition = " ".join(item.strip() for item in current_definition if item.strip()).strip()
        if not definition:
            raise GrillSessionError(f"CONTEXT 术语缺少定义: {current_term}")
        if not 1 <= _context_sentence_count(definition) <= 2:
            raise GrillSessionError(f"CONTEXT 术语定义必须是一到两句话: {current_term}")
        normalized_term = current_term.casefold()
        if normalized_term in seen_terms:
            raise GrillSessionError(f"CONTEXT 术语重复: {current_term}")
        seen_terms.add(normalized_term)
        current_term = ""
        current_definition = []
        current_avoid = ""

    for raw_line in lines[language_index + 1 :]:
        line = raw_line.strip()
        if not line:
            continue
        if re.fullmatch(r"###\s+[^#].*", line):
            flush_term()
            continue
        term_match = _CONTEXT_TERM_RE.fullmatch(line)
        if term_match:
            flush_term()
            current_term = term_match.group(1).strip()
            inline_definition = term_match.group(2).strip()
            if not current_term:
                raise GrillSessionError("CONTEXT 术语名称不能为空")
            if inline_definition:
                current_definition.append(inline_definition)
            continue
        avoid_match = _CONTEXT_AVOID_RE.fullmatch(line)
        if avoid_match:
            if not current_term or current_avoid or not avoid_match.group(1).strip():
                raise GrillSessionError("CONTEXT _Avoid_ 必须紧跟一个已定义术语且只能出现一次")
            current_avoid = avoid_match.group(1).strip()
            continue
        if line.startswith("#") or re.match(r"^(?:[-*+]|>|\d+[.)])\s+", line) or "|" in line:
            raise GrillSessionError("CONTEXT Language 章节只允许分组标题和术语定义")
        if not current_term or current_avoid:
            raise GrillSessionError("CONTEXT Language 章节包含未归属到术语的散文")
        current_definition.append(line)
    flush_term()
    if not seen_terms:
        raise GrillSessionError("CONTEXT 草稿至少需要一个领域术语")


def validate_grill_document_draft(draft_path: str | Path) -> GrillDocumentDraft:
    path = Path(draft_path).expanduser().resolve()
    if not path.exists():
        return GrillDocumentDraft(context_markdown="", adrs=())
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise GrillSessionError("domain_drafts.json 必须是 JSON 对象")
    if str(payload.get("schema_version", "")).strip() != GRILL_DOCUMENT_DRAFT_SCHEMA_VERSION:
        raise GrillSessionError("domain_drafts.json schema_version 非法")
    context_markdown = str(payload.get("context_markdown", "") or "").strip()
    if context_markdown:
        _validate_grill_context_markdown(context_markdown)
    raw_adrs = payload.get("adrs", [])
    if not isinstance(raw_adrs, list):
        raise GrillSessionError("domain_drafts.json adrs 必须是数组")
    adrs: list[dict[str, object]] = []
    seen_slugs: set[str] = set()
    for index, item in enumerate(raw_adrs, start=1):
        if not isinstance(item, dict):
            raise GrillSessionError(f"ADR 草稿 {index} 必须是对象")
        if not all(bool(item.get(key, False)) for key in ("hard_to_reverse", "surprising", "real_tradeoff")):
            raise GrillSessionError(f"ADR 草稿 {index} 不满足三项创建条件")
        title = str(item.get("title", "") or "").strip()
        markdown = str(item.get("markdown", "") or "").strip()
        slug = re.sub(r"[^0-9a-z-]+", "-", str(item.get("slug", title) or "").strip().lower())
        slug = re.sub(r"-+", "-", slug).strip("-")
        if not title or not markdown or not slug:
            raise GrillSessionError(f"ADR 草稿 {index} 缺少 title、slug 或 markdown")
        if not markdown.startswith("# "):
            raise GrillSessionError(f"ADR 草稿 {index} 必须以一级标题开始")
        if slug in seen_slugs:
            raise GrillSessionError(f"ADR 草稿 slug 重复: {slug}")
        seen_slugs.add(slug)
        adrs.append({**item, "title": title, "slug": slug, "markdown": markdown})
    return GrillDocumentDraft(context_markdown=context_markdown, adrs=tuple(adrs))


def _prefixed_text_sha256(text: str) -> str:
    return f"sha256:{hashlib.sha256(str(text).encode('utf-8')).hexdigest()}"


def _optional_file_hash(path: Path) -> str:
    return build_prefixed_sha256(path) if path.exists() and path.is_file() else ""


def _validate_grill_context_snapshot_for_publish(
    *,
    project_root: Path,
    context_map_exists: bool | None,
    context_map_hash: str,
    context_target_snapshot: Sequence[str | Path],
    context_preimage_hashes: Mapping[str, str],
) -> tuple[Path, ...]:
    if context_map_exists is None or not context_target_snapshot:
        raise GrillDocumentConflict(
            "旧 Grill 会话缺少 CONTEXT-MAP/目标启动快照；必须重新读取并再次确认后才能发布"
        )
    snapshot_targets = tuple(
        _resolve_project_owned_publish_path(project_root, item, label="CONTEXT 启动目标")
        for item in context_target_snapshot
    )
    if len(set(snapshot_targets)) != len(snapshot_targets):
        raise GrillSessionError("CONTEXT 启动目标快照包含重复路径")
    if set(str(item) for item in snapshot_targets) != set(str(key) for key in context_preimage_hashes):
        raise GrillDocumentConflict("CONTEXT 目标快照与启动前哈希集合不一致；必须重新确认")

    current = capture_grill_context_snapshot(project_root)
    current_targets = tuple(Path(item).expanduser().resolve() for item in current["targets"])  # type: ignore[union-attr]
    if bool(current["map_exists"]) != context_map_exists:
        raise GrillDocumentConflict("CONTEXT-MAP.md 的存在状态在访谈期间发生变化")
    if str(current["map_hash"]) != str(context_map_hash or ""):
        raise GrillDocumentConflict("CONTEXT-MAP.md 在访谈期间发生变化，拒绝沿用旧发布目标")
    if set(current_targets) != set(snapshot_targets):
        raise GrillDocumentConflict("CONTEXT-MAP.md 发布目标集合在访谈期间发生变化")
    return current_targets


def _validated_publish_intent_files(
    *,
    project_root: Path,
    intent: Mapping[str, object],
    draft: GrillDocumentDraft,
    draft_hash: str,
    target: Path,
) -> list[dict[str, str]]:
    if str(intent.get("schema_version", "")).strip() != "1.0":
        raise GrillSessionError("Grill publish_intent schema_version 非法")
    if str(intent.get("draft_hash", "")).strip() != draft_hash:
        raise GrillDocumentConflict("领域文档草稿已变化，不能沿用既有发布事务")
    intent_target = _resolve_project_owned_publish_path(
        project_root,
        str(intent.get("context_target", "") or ""),
        label="publish_intent CONTEXT 目标",
    )
    if intent_target != target:
        raise GrillDocumentConflict("publish_intent CONTEXT 目标与当前人工选择不一致")
    raw_files = intent.get("files", [])
    if not isinstance(raw_files, list) or not raw_files:
        raise GrillSessionError("Grill publish_intent 缺少固定发布文件计划")

    expected_contents: list[tuple[str, str, str]] = []
    if draft.context_markdown:
        expected_contents.append(("context", "", draft.context_markdown.rstrip() + "\n"))
    for item in draft.adrs:
        expected_contents.append(("adr", str(item["slug"]), str(item["markdown"]).strip() + "\n"))
    if len(raw_files) != len(expected_contents):
        raise GrillDocumentConflict("publish_intent 文件数量与已确认草稿不一致")

    normalized: list[dict[str, str]] = []
    seen_paths: set[str] = set()
    expected_adr_dir = _resolve_project_owned_publish_path(
        project_root,
        target.parent / "docs" / "adr",
        label="publish_intent ADR 目录",
    )
    for index, (raw_item, expected) in enumerate(zip(raw_files, expected_contents), start=1):
        if not isinstance(raw_item, Mapping):
            raise GrillSessionError(f"publish_intent 文件计划 {index} 必须是对象")
        expected_kind, expected_slug, expected_content = expected
        kind = str(raw_item.get("kind", "") or "").strip()
        path = _resolve_project_owned_publish_path(
            project_root,
            str(raw_item.get("path", "") or ""),
            label=f"publish_intent 文件 {index}",
        )
        path_text = str(path)
        if path_text in seen_paths:
            raise GrillSessionError("publish_intent 包含重复发布路径")
        seen_paths.add(path_text)
        if kind != expected_kind:
            raise GrillDocumentConflict("publish_intent 文件类型与已确认草稿不一致")
        if kind == "context" and path != target:
            raise GrillDocumentConflict("publish_intent CONTEXT 文件路径非法")
        if kind == "adr":
            expected_name = re.compile(rf"^\d{{4}}-{re.escape(expected_slug)}\.md$")
            if path.parent != expected_adr_dir or not expected_name.fullmatch(path.name):
                raise GrillDocumentConflict("publish_intent ADR 路径与已确认目录或 slug 不一致")
        preimage_hash = str(raw_item.get("preimage_hash", "") or "").strip()
        if preimage_hash and not re.fullmatch(r"sha256:[0-9a-f]{64}", preimage_hash):
            raise GrillSessionError("publish_intent preimage_hash 非法")
        expected_content_hash = _prefixed_text_sha256(expected_content)
        if str(raw_item.get("content_hash", "") or "").strip() != expected_content_hash:
            raise GrillDocumentConflict("publish_intent 内容哈希与已确认草稿不一致")
        normalized.append(
            {
                "kind": kind,
                "path": path_text,
                "preimage_hash": preimage_hash,
                "content_hash": expected_content_hash,
                "content": expected_content,
            }
        )
    return normalized


def publish_grill_document_draft(
    *,
    project_dir: str | Path,
    draft_path: str | Path,
    context_preimage_hashes: Mapping[str, str],
    context_target: str | Path | None = None,
    context_map_exists: bool | None = None,
    context_map_hash: str = "",
    context_target_snapshot: Sequence[str | Path] = (),
    publish_intent: dict[str, object] | None = None,
    persist_publish_intent: Callable[[dict[str, object]], None] | None = None,
) -> tuple[str, ...]:
    """Publish confirmed domain docs through an idempotent, durable file plan."""

    project_root = Path(project_dir).expanduser().resolve()
    draft_file = Path(draft_path).expanduser().resolve()
    draft = validate_grill_document_draft(draft_file)
    if not draft.context_markdown and not draft.adrs:
        return ()
    targets = _validate_grill_context_snapshot_for_publish(
        project_root=project_root,
        context_map_exists=context_map_exists,
        context_map_hash=context_map_hash,
        context_target_snapshot=context_target_snapshot,
        context_preimage_hashes=context_preimage_hashes,
    )
    if context_target is None:
        if len(targets) != 1:
            raise GrillContextTargetRequired(targets)
        target = targets[0]
    else:
        target = _resolve_project_owned_publish_path(
            project_root,
            context_target,
            label="选择的 CONTEXT.md",
        )
        if target not in targets:
            raise GrillDocumentConflict(
                f"CONTEXT-MAP.md 在访谈期间发生变化，原发布目标已不再允许: {target}"
            )

    intent_holder = publish_intent if publish_intent is not None else {}

    def persist_intent(payload: dict[str, object]) -> None:
        intent_holder.clear()
        intent_holder.update(payload)
        if persist_publish_intent is not None:
            persist_publish_intent(dict(payload))

    # This fixed project-wide scope serializes ADR number allocation across
    # different requirements. The A03 requirement lock alone is insufficient.
    from tmux_core.stage_kernel.requirement_concurrency import requirement_concurrency_lock

    try:
        publish_lock = requirement_concurrency_lock(
            project_root,
            GRILL_DOCUMENT_PUBLISH_LOCK_SCOPE,
            action="grill.domain_documents.publish",
        )
        publish_lock.__enter__()
    except RuntimeError as error:
        raise GrillDocumentConflict(f"领域文档发布锁冲突: {error}") from error
    try:
        # Recheck the map and all symlinks while holding the allocation lock.
        targets = _validate_grill_context_snapshot_for_publish(
            project_root=project_root,
            context_map_exists=context_map_exists,
            context_map_hash=context_map_hash,
            context_target_snapshot=context_target_snapshot,
            context_preimage_hashes=context_preimage_hashes,
        )
        if target not in targets:
            raise GrillDocumentConflict(f"当前 CONTEXT-MAP.md 不再允许目标: {target}")
        draft_hash = build_prefixed_sha256(draft_file)

        if intent_holder:
            plan = _validated_publish_intent_files(
                project_root=project_root,
                intent=intent_holder,
                draft=draft,
                draft_hash=draft_hash,
                target=target,
            )
            intent = dict(intent_holder)
        else:
            expected_hash = str(context_preimage_hashes.get(str(target), ""))
            current_hash = _optional_file_hash(target)
            if current_hash != expected_hash:
                raise GrillDocumentConflict(f"CONTEXT.md 在访谈期间发生变化，拒绝覆盖: {target}")

            adr_dir = _resolve_project_owned_publish_path(
                project_root,
                target.parent / "docs" / "adr",
                label="ADR 发布目录",
            )
            if adr_dir.exists() and not adr_dir.is_dir():
                raise GrillDocumentConflict(f"ADR 发布目录不是目录: {adr_dir}")
            existing_numbers: list[int] = []
            if adr_dir.exists():
                for candidate in adr_dir.glob("[0-9][0-9][0-9][0-9]-*.md"):
                    try:
                        existing_numbers.append(int(candidate.name[:4]))
                    except ValueError:
                        continue
            next_number = max(existing_numbers, default=0) + 1
            plan: list[dict[str, str]] = []
            if draft.context_markdown:
                content = draft.context_markdown.rstrip() + "\n"
                plan.append(
                    {
                        "kind": "context",
                        "path": str(target),
                        "preimage_hash": expected_hash,
                        "content_hash": _prefixed_text_sha256(content),
                        "content": content,
                    }
                )
            for offset, item in enumerate(draft.adrs):
                target_path = _resolve_project_owned_publish_path(
                    project_root,
                    adr_dir / f"{next_number + offset:04d}-{item['slug']}.md",
                    label="ADR 发布文件",
                )
                if target_path.exists() or target_path.is_symlink():
                    raise GrillDocumentConflict(f"ADR 目标已存在，拒绝覆盖: {target_path}")
                content = str(item["markdown"]).strip() + "\n"
                plan.append(
                    {
                        "kind": "adr",
                        "path": str(target_path),
                        "preimage_hash": "",
                        "content_hash": _prefixed_text_sha256(content),
                        "content": content,
                    }
                )
            intent = {
                "schema_version": "1.0",
                "intent_id": uuid.uuid4().hex,
                "state": "prepared",
                "draft_hash": draft_hash,
                "context_target": str(target),
                "files": [
                    {key: value for key, value in item.items() if key != "content"}
                    for item in plan
                ],
                "applied_paths": [],
                "published_paths": [],
            }
            # The durable plan must exist before the first project document changes.
            persist_intent(intent)

        intent_state = str(intent.get("state", "") or "").strip()
        if intent_state not in {"prepared", "committing", "committed"}:
            raise GrillSessionError(f"publish_intent state 非法: {intent_state!r}")
        applied_paths = [str(item) for item in intent.get("applied_paths", []) if str(item).strip()]
        if intent_state != "committed":
            intent["state"] = "committing"
            persist_intent(intent)

        for item in plan:
            planned_path = Path(item["path"]).expanduser().resolve()
            checked_path = _resolve_project_owned_publish_path(
                project_root,
                planned_path,
                label="发布事务文件",
            )
            if checked_path != planned_path:
                raise GrillDocumentConflict(f"发布事务路径在执行期间发生符号链接漂移: {planned_path}")
            current_hash = _optional_file_hash(checked_path)
            if current_hash != item["content_hash"]:
                if current_hash != item["preimage_hash"]:
                    raise GrillDocumentConflict(f"发布事务目标发生并发变化，拒绝覆盖: {checked_path}")
                _write_text_atomic(checked_path, item["content"])
                if _optional_file_hash(checked_path) != item["content_hash"]:
                    raise GrillSessionError(f"领域文档原子写入后哈希不匹配: {checked_path}")
            if str(checked_path) not in applied_paths:
                applied_paths.append(str(checked_path))
                intent["applied_paths"] = list(applied_paths)
                persist_intent(intent)

        published = [item["path"] for item in plan]
        intent["state"] = "committed"
        intent["published_paths"] = list(published)
        persist_intent(intent)
        return tuple(published)
    finally:
        publish_lock.__exit__(None, None, None)


def _grill_reference_marker_pattern() -> re.Pattern[str]:
    return re.compile(
        re.escape(GRILL_DOCUMENT_REFERENCES_BEGIN)
        + r".*?"
        + re.escape(GRILL_DOCUMENT_REFERENCES_END),
        re.DOTALL,
    )


def _build_grill_reference_block(relative_paths: Sequence[str]) -> str:
    links = "\n".join(f"- [{Path(item).name}]({item})" for item in relative_paths)
    return "\n".join(
        (
            GRILL_DOCUMENT_REFERENCES_BEGIN,
            "## 领域文档引用",
            "以下 CONTEXT/ADR 已经人类最终确认，并由系统发布：",
            links,
            GRILL_DOCUMENT_REFERENCES_END,
        )
    )


def _grill_reference_free_text(text: str) -> str:
    return _grill_reference_marker_pattern().sub("", str(text), count=1).rstrip()


def update_grill_document_references(
    *,
    requirements_path: str | Path,
    project_dir: str | Path,
    published_paths: Sequence[str | Path],
) -> tuple[str, ...]:
    """Atomically add an idempotent, system-owned reference block to A03 output."""

    requirements_file = Path(requirements_path).expanduser().resolve()
    project_root = Path(project_dir).expanduser().resolve()
    if not requirements_file.exists() or not requirements_file.is_file():
        raise GrillSessionError(f"需求澄清候选不存在: {requirements_file}")
    relative_paths: list[str] = []
    for path_value in published_paths:
        published = Path(path_value).expanduser().resolve()
        try:
            relative = published.relative_to(project_root)
        except ValueError as error:
            raise GrillSessionError(f"领域文档发布路径越界: {published}") from error
        if not published.exists() or not published.is_file():
            raise GrillSessionError(f"领域文档发布结果不存在: {published}")
        relative_text = relative.as_posix()
        if relative_text not in relative_paths:
            relative_paths.append(relative_text)
    if not relative_paths:
        return ()

    reference_block = _build_grill_reference_block(relative_paths)
    original = requirements_file.read_text(encoding="utf-8")
    marker_pattern = _grill_reference_marker_pattern()
    if len(marker_pattern.findall(original)) > 1:
        raise GrillSessionError("需求澄清候选包含重复的系统领域文档引用块")
    if marker_pattern.search(original):
        updated = marker_pattern.sub(reference_block, original, count=1)
    else:
        updated = original.rstrip() + "\n\n" + reference_block + "\n"
    if updated != original:
        _write_text_atomic(requirements_file, updated)
    return tuple(relative_paths)


def _read_non_empty_text(file_path: str | Path) -> str:
    path = Path(file_path).expanduser().resolve()
    if not path.exists() or not path.is_file():
        return ""
    return path.read_text(encoding="utf-8").strip()


def _build_hitl_artifact_hashes(paths: list[str]) -> dict[str, str]:
    artifact_hashes: dict[str, str] = {}
    for item in paths:
        resolved = Path(item).expanduser().resolve()
        if not resolved.exists() or not resolved.is_file():
            raise FileNotFoundError(f"HITL 状态文件引用的文档不存在: {resolved}")
        artifact_hashes[str(resolved)] = build_prefixed_sha256(resolved)
    return artifact_hashes


def _read_optional_artifact_hash(path_value: str | Path) -> str:
    path = Path(path_value).expanduser().resolve()
    if not path.exists() or not path.is_file():
        return ""
    if not path.read_text(encoding="utf-8").strip():
        return ""
    return build_prefixed_sha256(path)


def _infer_hitl_status(
    *,
    stage_name: str,
    turn_id: str,
    hitl_round: int,
    output_path: str | Path,
    question_path: str | Path,
    record_path: str | Path,
) -> dict[str, object]:
    output_file = Path(output_path).expanduser().resolve()
    question_file = Path(question_path).expanduser().resolve()
    record_file = Path(record_path).expanduser().resolve()

    output_text = _read_non_empty_text(output_file)
    question_text = _read_non_empty_text(question_file)
    record_text = _read_non_empty_text(record_file)

    if stage_name == "requirements_notion_intake":
        if output_text:
            referenced_paths = [str(output_file)]
            if record_text:
                referenced_paths.append(str(record_file))
            return {
                "schema_version": HITL_STATUS_SCHEMA_VERSION,
                "stage": stage_name,
                "turn_id": turn_id,
                "hitl_round": hitl_round,
                "status": HITL_STATUS_COMPLETED,
                "summary": "done",
                "output_path": str(output_file),
                "question_path": "",
                "record_path": str(record_file) if record_text else "",
                "artifact_hashes": _build_hitl_artifact_hashes(referenced_paths),
                "written_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            }
        if question_text:
            if record_text:
                return {
                    "schema_version": HITL_STATUS_SCHEMA_VERSION,
                    "stage": stage_name,
                    "turn_id": turn_id,
                    "hitl_round": hitl_round,
                    "status": HITL_STATUS_HITL,
                    "summary": "need hitl",
                    "output_path": "",
                    "question_path": str(question_file),
                    "record_path": str(record_file),
                    "artifact_hashes": _build_hitl_artifact_hashes([str(question_file), str(record_file)]),
                    "written_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                }
            referenced_paths = [str(question_file)]
            return {
                "schema_version": HITL_STATUS_SCHEMA_VERSION,
                "stage": stage_name,
                "turn_id": turn_id,
                "hitl_round": hitl_round,
                "status": HITL_STATUS_ERROR,
                "summary": question_text.splitlines()[0].strip() or "notion_read_failed",
                "output_path": "",
                "question_path": str(question_file),
                "record_path": "",
                "artifact_hashes": _build_hitl_artifact_hashes(referenced_paths),
                "written_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            }
        raise FileNotFoundError("尚未观察到可判定的 Notion 需求读取产物")

    record_exists = record_file.exists() and record_file.is_file()

    if question_text and record_exists:
        return {
            "schema_version": HITL_STATUS_SCHEMA_VERSION,
            "stage": stage_name,
            "turn_id": turn_id,
            "hitl_round": hitl_round,
            "status": HITL_STATUS_HITL,
            "summary": "need hitl",
            "output_path": "",
            "question_path": str(question_file),
            "record_path": str(record_file),
            "artifact_hashes": _build_hitl_artifact_hashes([str(question_file), str(record_file)]),
            "written_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }

    if output_text:
        referenced_paths = [str(output_file)]
        if record_text:
            referenced_paths.append(str(record_file))
        return {
            "schema_version": HITL_STATUS_SCHEMA_VERSION,
            "stage": stage_name,
            "turn_id": turn_id,
            "hitl_round": hitl_round,
            "status": HITL_STATUS_COMPLETED,
            "summary": "done",
            "output_path": str(output_file),
            "question_path": "",
            "record_path": str(record_file) if record_text else "",
            "artifact_hashes": _build_hitl_artifact_hashes(referenced_paths),
            "written_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }

    if question_text and not record_exists:
        raise FileNotFoundError(f"HITL 提问文件已生成，但缺少记录文件: {record_file}")

    raise FileNotFoundError(f"尚未观察到可判定的阶段产物: {stage_name}")


def _materialize_hitl_status_file(
    status_path: str | Path,
    *,
    stage_name: str,
    turn_id: str,
    hitl_round: int,
    output_path: str | Path,
    question_path: str | Path,
    record_path: str | Path,
) -> Path:
    payload = _infer_hitl_status(
        stage_name=stage_name,
        turn_id=turn_id,
        hitl_round=hitl_round,
        output_path=output_path,
        question_path=question_path,
        record_path=record_path,
    )
    return _write_json_atomic(status_path, payload)


def _build_turn_status_payload(
    *,
    turn_id: str,
    phase: str,
    stage_status_path: str | Path,
    output_path: str | Path,
    question_path: str | Path,
    record_path: str | Path,
) -> dict[str, object]:
    stage_status_file = Path(stage_status_path).expanduser().resolve()
    output_file = Path(output_path).expanduser().resolve()
    question_file = Path(question_path).expanduser().resolve()
    record_file = Path(record_path).expanduser().resolve()

    artifacts: dict[str, str] = {"stage_status": str(stage_status_file)}
    artifact_hashes = {str(stage_status_file): build_prefixed_sha256(stage_status_file)}
    for key, file_path in (
        ("output", output_file),
        ("question", question_file),
        ("record", record_file),
    ):
        if not file_path.exists() or not file_path.is_file():
            continue
        if not file_path.read_text(encoding="utf-8").strip():
            continue
        artifacts[key] = str(file_path)
        artifact_hashes[str(file_path)] = build_prefixed_sha256(file_path)
    return {
        "schema_version": TURN_STATUS_SCHEMA_VERSION,
        "turn_id": turn_id,
        "phase": phase,
        "status": "done",
        "artifacts": artifacts,
        "artifact_hashes": artifact_hashes,
        "written_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }


def _materialize_turn_status_file(
    status_path: str | Path,
    *,
    turn_id: str,
    phase: str,
    stage_status_path: str | Path,
    output_path: str | Path,
    question_path: str | Path,
    record_path: str | Path,
) -> Path:
    payload = _build_turn_status_payload(
        turn_id=turn_id,
        phase=phase,
        stage_status_path=stage_status_path,
        output_path=output_path,
        question_path=question_path,
        record_path=record_path,
    )
    return _write_json_atomic(status_path, payload)


def build_hitl_contract_repair_prompt(
    *,
    context: HitlPromptContext,
    error_text: str,
) -> str:
    lines = [
        "上一轮已经结束，但 HITL 文件契约未通过校验，需要补齐或修正本轮允许的产物。",
        f"阶段: {context.stage_name}",
        f"本轮: {context.turn_id}",
        "允许修改的文件:",
        f"- output: {context.output_path}",
        f"- question: {context.question_path}",
        f"- record: {context.record_path}",
    ]
    cleaned_error = str(error_text or "").strip()
    if cleaned_error:
        lines.append(f"上次校验错误: {cleaned_error[:1200]}")
    lines.extend(
        [
            "根据你上一轮真实意图只选择一种结果:",
            "- 若信息足够: 写入非空 output，清空 question，可按需更新 record。",
            "- 若仍需人类补充: 写入非空 question，并确保 record 文件存在且同步当前事实。",
            "不要修改其他文件，不要重新发起无关分析；补齐后仍按原输出协议返回。",
        ]
    )
    return "\n".join(lines)


def build_turn_status_contract(
    *,
    turn_status_path: str | Path,
    turn_id: str,
    turn_phase: str,
    stage_status_path: str | Path,
    stage_name: str | None = None,
    hitl_round: int | None = None,
    output_path: str | Path | None = None,
    question_path: str | Path | None = None,
    record_path: str | Path | None = None,
    fresh_completion_paths: Sequence[str | Path] = (),
    baseline_fresh_hashes: Mapping[str, str] | None = None,
) -> TurnFileContract:
    expected_stage_status = str(Path(stage_status_path).expanduser().resolve())
    tracked_fresh_paths = [Path(item).expanduser().resolve() for item in fresh_completion_paths]
    expected_fresh_hashes = {
        str(Path(path_text).expanduser().resolve()): str(hash_text).strip()
        for path_text, hash_text in (baseline_fresh_hashes or {}).items()
    }

    def _validate_fresh_completion(decision: HitlStatusDecision) -> None:
        if decision.status != HITL_STATUS_COMPLETED or not tracked_fresh_paths:
            return
        for tracked_path in tracked_fresh_paths:
            current_hash = _read_optional_artifact_hash(tracked_path)
            baseline_hash = expected_fresh_hashes.get(str(tracked_path), "")
            if current_hash != baseline_hash:
                return
        tracked_names = ", ".join(path.name for path in tracked_fresh_paths)
        raise ValueError(f"completed 状态未生成新的阶段产物: {tracked_names}")

    def validator(path: Path) -> TurnFileResult:
        status_path = Path(path).expanduser().resolve()
        validation_error: Exception | None = None
        turn_result: TurnFileResult | None = None
        if not status_path.exists():
            validation_error = FileNotFoundError(f"缺少 turn_status.json: {status_path}")
        else:
            payload = json.loads(status_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                validation_error = ValueError("turn_status.json 必须是 JSON 对象")
            else:
                if str(payload.get("schema_version", "")).strip() != TURN_STATUS_SCHEMA_VERSION:
                    validation_error = ValueError("turn_status.json schema_version 非法")
                elif str(payload.get("turn_id", "")).strip() != turn_id:
                    validation_error = ValueError("turn_status.json turn_id 非法")
                elif str(payload.get("phase", "")).strip() != turn_phase:
                    validation_error = ValueError("turn_status.json phase 非法")
                elif str(payload.get("status", "")).strip().lower() != "done":
                    validation_error = ValueError("turn_status.json status 非法")
                else:
                    parse_iso_timestamp(payload.get("written_at", ""))
                    artifacts = payload.get("artifacts", {})
                    if not isinstance(artifacts, dict):
                        validation_error = ValueError("turn_status.json artifacts 必须是对象")
                    else:
                        stage_status_in_payload = str(artifacts.get("stage_status", "")).strip()
                        if stage_status_in_payload != expected_stage_status:
                            validation_error = ValueError("turn_status.json stage_status 非法")
                        else:
                            artifact_paths = _collect_artifact_paths(artifacts)
                            artifact_hashes = payload.get("artifact_hashes", {})
                            if not isinstance(artifact_hashes, dict):
                                validation_error = ValueError("turn_status.json artifact_hashes 必须是对象")
                            else:
                                validated_hashes: dict[str, str] = {}
                                for artifact_path_text in artifact_paths:
                                    artifact_path = Path(artifact_path_text).expanduser().resolve()
                                    if not artifact_path.exists() or not artifact_path.is_file():
                                        validation_error = FileNotFoundError(
                                            f"turn_status.json 引用的文件不存在: {artifact_path}"
                                        )
                                        break
                                    expected_hash = str(artifact_hashes.get(str(artifact_path), "")).strip()
                                    actual_hash = build_prefixed_sha256(artifact_path)
                                    if expected_hash != actual_hash:
                                        validation_error = ValueError(
                                            f"turn_status.json artifact_hashes 不匹配: {artifact_path}"
                                        )
                                        break
                                    validated_hashes[str(artifact_path)] = actual_hash
                                if validation_error is None:
                                    turn_result = TurnFileResult(
                                        status_path=str(status_path),
                                        payload=payload,
                                        artifact_paths={
                                            f"artifact_{index}": item
                                            for index, item in enumerate(artifact_paths, start=1)
                                        },
                                        artifact_hashes=validated_hashes,
                                        validated_at=datetime.now().astimezone().isoformat(timespec="seconds"),
                                    )
        if not all((stage_name, hitl_round is not None, output_path, question_path, record_path)):
            if turn_result is not None:
                return turn_result
            raise validation_error or FileNotFoundError(f"缺少 turn_status.json: {status_path}")

        stage_decision: HitlStatusDecision | None = None
        stage_validation_error: Exception | None = None
        try:
            stage_decision = validate_hitl_status_file(
                stage_status_path,
                expected_stage=str(stage_name),
                expected_turn_id=turn_id,
                expected_hitl_round=int(hitl_round),
                expected_output_path=output_path,
                expected_question_path=question_path,
                expected_record_path=record_path,
            )
        except Exception as error:  # noqa: BLE001
            stage_validation_error = error

        if stage_decision is not None:
            _validate_fresh_completion(stage_decision)

        if turn_result is not None and stage_decision is not None:
            return turn_result

        if stage_decision is None:
            try:
                materialized_stage_status_path = _materialize_hitl_status_file(
                    stage_status_path,
                    stage_name=str(stage_name),
                    turn_id=turn_id,
                    hitl_round=int(hitl_round),
                    output_path=output_path,
                    question_path=question_path,
                    record_path=record_path,
                )
                stage_decision = validate_hitl_status_file(
                    materialized_stage_status_path,
                    expected_stage=str(stage_name),
                    expected_turn_id=turn_id,
                    expected_hitl_round=int(hitl_round),
                    expected_output_path=output_path,
                    expected_question_path=question_path,
                    expected_record_path=record_path,
                )
            except Exception:
                if validation_error is not None:
                    raise validation_error
                if stage_validation_error is not None:
                    raise stage_validation_error
                raise

        materialized_turn_status_path = _materialize_turn_status_file(
            status_path,
            turn_id=turn_id,
            phase=turn_phase,
            stage_status_path=stage_decision.status_path,
            output_path=output_path,
            question_path=question_path,
            record_path=record_path,
        )
        return validator(materialized_turn_status_path)

    return TurnFileContract(
        turn_id=turn_id,
        phase=turn_phase,
        status_path=Path(turn_status_path).expanduser().resolve(),
        validator=validator,
        quiet_window_sec=1.0,
    )


def validate_hitl_status_file(
    status_path: str | Path,
    *,
    expected_stage: str,
    expected_turn_id: str,
    expected_hitl_round: int,
    expected_output_path: str | Path,
    expected_question_path: str | Path,
    expected_record_path: str | Path,
) -> HitlStatusDecision:
    resolved_status_path = Path(status_path).expanduser().resolve()
    if not resolved_status_path.exists():
        raise FileNotFoundError(f"缺少 HITL 状态文件: {resolved_status_path}")
    payload = json.loads(resolved_status_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("HITL 状态文件必须是 JSON 对象")
    if str(payload.get("schema_version", "")).strip() != HITL_STATUS_SCHEMA_VERSION:
        raise ValueError("HITL 状态文件 schema_version 非法")
    stage = str(payload.get("stage", "")).strip()
    if stage != expected_stage:
        raise ValueError(f"HITL 状态文件 stage 非法: {stage!r}")
    turn_id = str(payload.get("turn_id", "")).strip()
    if turn_id != expected_turn_id:
        raise ValueError(f"HITL 状态文件 turn_id 非法: {turn_id!r}")
    hitl_round = int(payload.get("hitl_round", -1))
    if hitl_round != expected_hitl_round:
        raise ValueError(f"HITL 状态文件 hitl_round 非法: {hitl_round!r}")
    status = str(payload.get("status", "")).strip().lower()
    if status not in HITL_ALLOWED_STATUSES:
        raise ValueError(f"HITL 状态文件 status 非法: {status!r}")
    summary = str(payload.get("summary", "")).strip()
    written_at = parse_iso_timestamp(payload.get("written_at", ""))

    output_path_text = str(payload.get("output_path", "")).strip()
    question_path_text = str(payload.get("question_path", "")).strip()
    record_path_text = str(payload.get("record_path", "")).strip()
    expected_output_text = str(Path(expected_output_path).expanduser().resolve())
    expected_question_text = str(Path(expected_question_path).expanduser().resolve())
    expected_record_text = str(Path(expected_record_path).expanduser().resolve())

    if output_path_text and output_path_text != expected_output_text:
        raise ValueError("HITL 状态文件 output_path 非法")
    if question_path_text and question_path_text != expected_question_text:
        raise ValueError("HITL 状态文件 question_path 非法")
    if record_path_text and record_path_text != expected_record_text:
        raise ValueError("HITL 状态文件 record_path 非法")

    artifact_hashes = payload.get("artifact_hashes", {})
    if not isinstance(artifact_hashes, dict):
        raise ValueError("HITL 状态文件 artifact_hashes 必须是对象")

    referenced_paths = [item for item in (output_path_text, question_path_text, record_path_text) if item]
    for referenced in referenced_paths:
        file_path = Path(referenced).expanduser().resolve()
        if not file_path.exists() or not file_path.is_file():
            raise FileNotFoundError(f"HITL 状态文件引用的文档不存在: {file_path}")
        expected_hash = str(artifact_hashes.get(str(file_path), "")).strip()
        actual_hash = build_prefixed_sha256(file_path)
        if expected_hash != actual_hash:
            raise ValueError(f"HITL 状态文件 artifact_hashes 不匹配: {file_path}")

    if status == HITL_STATUS_COMPLETED:
        if output_path_text != expected_output_text:
            raise ValueError("completed 状态必须指向最终输出文档")
        if not Path(output_path_text).read_text(encoding="utf-8").strip():
            raise ValueError("completed 状态的最终输出文档不能为空")
        if question_path_text:
            raise ValueError("completed 状态不应声明提问文档")
        expected_question_file = Path(expected_question_text)
        if expected_question_file.exists() and expected_question_file.read_text(encoding="utf-8").strip():
            raise ValueError("completed 状态下提问文档必须为空")
    if status == HITL_STATUS_HITL:
        if question_path_text != expected_question_text:
            raise ValueError("hitl 状态必须指向提问文档")
        if record_path_text != expected_record_text:
            raise ValueError("hitl 状态必须指向 HITL 记录文档")
        if not Path(question_path_text).read_text(encoding="utf-8").strip():
            raise ValueError("hitl 提问文档不能为空")
    return HitlStatusDecision(
        stage=stage,
        turn_id=turn_id,
        hitl_round=hitl_round,
        status=status,
        summary=summary,
        output_path=output_path_text,
        question_path=question_path_text,
        record_path=record_path_text,
        status_path=str(resolved_status_path),
        payload=payload,
        artifact_hashes={str(key): str(value) for key, value in artifact_hashes.items()},
        written_at=written_at,
    )


def collect_terminal_hitl_response(question_path: str | Path, *, hitl_round: int) -> str:
    question_file = Path(question_path).expanduser().resolve()
    question_text = question_file.read_text(encoding="utf-8").strip()
    ui = get_terminal_ui()
    if not isinstance(ui, BridgeTerminalUI):
        message()
        message(f"HITL 第 {hitl_round} 轮，需要人工补充信息")
        message(f"问题文档: {question_file}")
        message(question_text or "(问题文档为空)")
    return ui.prompt_multiline(
        title=f"HITL 第 {hitl_round} 轮回复",
        empty_retry_message="回复不能为空，请重新输入。",
        question_path=question_file,
        is_hitl=True,
    )


def _invoke_optional_hitl_callback(
    callback: Callable[..., object] | None,
    callback_name: str,
    *args: object,
) -> None:
    if callback is None:
        return
    try:
        callback(*args)
    except Exception as error:  # noqa: BLE001
        try:
            message(f"警告：HITL 回调失败 callback={callback_name} error={error}")
        except Exception:
            pass


def build_grill_contract_repair_prompt(
    *,
    context: HitlPromptContext,
    error_text: str,
) -> str:
    return "\n".join(
        [
            "上一轮已经结束，但 Grill 逐问结构未通过校验。只修正本轮允许的 HITL 文件。",
            f"问题文件: {context.question_path}",
            f"记录文件: {context.record_path}",
            f"校验错误: {str(error_text).strip()[:1200]}",
            "问题文件必须恰好包含以下二级标题:",
            "## 问题",
            "## 为什么需要决定",
            "## 推荐答案",
            "## 回答方式",
            "## 选项",
            "## 已核实事实",
            "每轮只写一个问题；回答方式只能是 select 或 multiline。",
            "select 必须给出 2 到 4 个 Markdown 列表选项；multiline 的选项章节可以为空。",
            "不要重新发起无关分析，修正后仍按原输出协议返回。",
        ]
    )


def run_hitl_agent_loop(
    *,
    worker,
    stage_name: str,
    output_path: str | Path,
    question_path: str | Path,
    record_path: str | Path,
    stage_status_path: str | Path,
    turns_root: str | Path,
    initial_prompt_builder: Callable[[HitlPromptContext], str],
    hitl_prompt_builder: Callable[[str, HitlPromptContext], str],
    label_prefix: str,
    turn_phase: str,
    human_input_provider: Callable[[str | Path, int], str] = collect_terminal_hitl_response,
    on_worker_starting: Callable[[object], None] | None = None,
    on_worker_started: Callable[[object], None] | None = None,
    on_agent_turn_started: Callable[[HitlPromptContext, object], None] | None = None,
    on_agent_turn_finished: Callable[[HitlPromptContext, object], None] | None = None,
    on_before_question_clear: Callable[[HitlPromptContext], None] | None = None,
    on_hitl_question: Callable[[HitlPromptContext, HitlStatusDecision], None] | None = None,
    on_hitl_answer: Callable[[HitlPromptContext, str, Path], None] | None = None,
    replace_dead_worker: Callable[[object, BaseException], object] | None = None,
    timeout_sec: float = DEFAULT_COMMAND_TIMEOUT_SEC,
    max_hitl_rounds: int = 8,
    max_contract_repair_attempts: int = DEFAULT_HITL_CONTRACT_REPAIR_ATTEMPTS,
    fresh_completion_paths: Sequence[str | Path] = (),
    fresh_completion_start_round: int = 1,
    startup_intervention_handler: Callable[[object, AgentInterventionRequired], None] | None = None,
    requirements_mode: str = GRILL_STANDARD_MODE,
    grill_session_path: str | Path | None = None,
    grill_domain_draft_path: str | Path | None = None,
    grill_project_dir: str | Path | None = None,
    grill_turn_profile_factory: Callable[[int], object] | None = None,
    graphify_context_factory: Callable[[HitlPromptContext], object] | None = None,
    grill_confirmation_provider: Callable[..., GrillControlDecision | str] = collect_grill_final_confirmation,
    grill_budget_provider: Callable[..., GrillControlDecision | str] = collect_grill_budget_decision,
    grill_context_target_provider: Callable[[Sequence[Path]], str] = collect_grill_context_target,
    grill_document_conflict_provider: Callable[[str], GrillControlDecision | str] = collect_grill_document_conflict_decision,
    grill_contract_intervention_handler: Callable[[object, GrillSessionError, HitlPromptContext, int], object] | None = None,
) -> HitlLoopResult:
    output_file = Path(output_path).expanduser().resolve()
    question_file = Path(question_path).expanduser().resolve()
    record_file = Path(record_path).expanduser().resolve()
    status_file = Path(stage_status_path).expanduser().resolve()
    turns_dir = Path(turns_root).expanduser().resolve()
    turns_dir.mkdir(parents=True, exist_ok=True)
    fresh_start_round = max(int(fresh_completion_start_round), 1)
    normalized_requirements_mode = normalize_requirements_mode_value(requirements_mode)
    grill_enabled = normalized_requirements_mode != GRILL_STANDARD_MODE
    session_file: Path | None = None
    grill_state: GrillSessionState | None = None
    if grill_enabled:
        if grill_session_path is None:
            raise GrillSessionError("Grill 模式必须提供 grill_session_path")
        session_file = Path(grill_session_path).expanduser().resolve()
        session_preexisted = session_file.exists()
        grill_state = load_grill_session_state(
            session_file,
            requirements_mode=normalized_requirements_mode,
            domain_draft_path=grill_domain_draft_path,
        )
        if normalized_requirements_mode == GRILL_WITH_DOCS_MODE:
            if grill_project_dir is None:
                raise GrillSessionError("grill-with-docs 模式必须提供 grill_project_dir")
            # Only a genuinely new interview may capture its launch snapshot
            # implicitly.  A legacy session missing the map/target fields must
            # reach the publish conflict flow and be rebuilt/reconfirmed;
            # silently filling those fields would bind an old confirmation to
            # a target the human never previewed.
            if not session_preexisted:
                apply_grill_context_snapshot(grill_state, grill_project_dir)
                save_grill_session_state(session_file, grill_state)

    def _read_worker_cursor(current_worker: object) -> dict[str, object]:
        read_state = getattr(current_worker, "read_state", None)
        if not callable(read_state):
            return {}
        try:
            payload = read_state()
        except Exception:  # noqa: BLE001
            return {}
        return dict(payload) if isinstance(payload, Mapping) else {}

    def _bind_grill_worker(current_worker: object) -> tuple[bool, bool]:
        """Persist the owned runtime pointer before any agent mutation occurs."""
        if grill_state is None or session_file is None:
            return False, False
        state_path_value = str(getattr(current_worker, "state_path", "") or "").strip()
        runtime_dir_value = str(getattr(current_worker, "runtime_dir", "") or "").strip()
        session_name_value = str(getattr(current_worker, "session_name", "") or "").strip()
        pane_id_value = str(getattr(current_worker, "pane_id", "") or "").strip()
        if not state_path_value:
            return False, not bool(grill_state.active_worker_state_path)
        resolved_state_path = str(Path(state_path_value).expanduser().resolve())
        resolved_runtime_dir = (
            str(Path(runtime_dir_value).expanduser().resolve())
            if runtime_dir_value
            else str(Path(resolved_state_path).parent)
        )
        previous_state_path = str(grill_state.active_worker_state_path or "").strip()
        replaced = bool(previous_state_path and previous_state_path != resolved_state_path)
        legacy_unbound = bool(
            grill_state.state == "turn_in_progress" and not previous_state_path
        )
        worker_payload = _read_worker_cursor(current_worker)
        grill_policy = worker_payload.get("grill_policy", {})
        generation = ""
        if isinstance(grill_policy, Mapping):
            generation = str(grill_policy.get("session_generation", "") or "").strip()
        grill_state.active_worker_state_path = resolved_state_path
        grill_state.active_runtime_dir = resolved_runtime_dir
        grill_state.active_session_name = (
            session_name_value
            or str(worker_payload.get("session_name", "") or "").strip()
        )
        grill_state.active_pane_id = (
            pane_id_value
            or str(worker_payload.get("pane_id", "") or "").strip()
        )
        grill_state.active_worker_generation = generation
        save_grill_session_state(session_file, grill_state)
        return replaced, legacy_unbound

    def _sync_grill_turn_cursor(current_worker: object) -> None:
        if grill_state is None or session_file is None:
            return
        current_state_path = str(getattr(current_worker, "state_path", "") or "").strip()
        if current_state_path and grill_state.active_worker_state_path:
            if (
                str(Path(current_state_path).expanduser().resolve())
                != str(Path(grill_state.active_worker_state_path).expanduser().resolve())
            ):
                # A dead worker's finally block may run after its replacement
                # has already been bound. Never let that stale cursor reclaim
                # the logical turn or overwrite the replacement pane pointer.
                return
        payload = _read_worker_cursor(current_worker)
        if not payload:
            return
        dispatch_state = str(payload.get("dispatch_state", "") or "").strip().lower()
        turn_state = str(payload.get("turn_state", "") or "").strip().lower()
        grill_state.turn_submission_cursor = dispatch_state or turn_state
        try:
            grill_state.turn_worker_state_revision = max(
                int(payload.get("state_revision", 0) or 0),
                0,
            )
        except (TypeError, ValueError):
            grill_state.turn_worker_state_revision = 0
        grill_state.active_session_name = str(
            payload.get("session_name", grill_state.active_session_name) or ""
        ).strip()
        grill_state.active_pane_id = str(
            payload.get("pane_id", grill_state.active_pane_id) or ""
        ).strip()
        save_grill_session_state(session_file, grill_state)

    def _persist_grill_turn_prompt(prompt_text: str, *, prompt_kind: str) -> None:
        if grill_state is None or session_file is None:
            return
        grill_state.turn_prompt_kind = str(prompt_kind or "").strip()
        grill_state.turn_prompt_text = str(prompt_text)
        grill_state.turn_prompt_hash = hashlib.sha256(
            grill_state.turn_prompt_text.encode("utf-8")
        ).hexdigest()
        if "repair" in grill_state.turn_prompt_kind:
            grill_state.turn_submission_cursor = "repair_not_started"
        save_grill_session_state(session_file, grill_state)

    grill_worker_recreated = False
    grill_legacy_unbound_turn = False
    if grill_state is not None:
        grill_worker_recreated, grill_legacy_unbound_turn = _bind_grill_worker(worker)
        if grill_worker_recreated and grill_state.state == "turn_in_progress":
            # The prior pane is confirmed dead by the A03 owner before it
            # constructs a replacement. The replacement has not received this
            # logical turn yet, so replay is safe after checking the old
            # contract once.
            grill_state.turn_submission_cursor = "not_started"
            grill_state.turn_worker_state_revision = 0
            save_grill_session_state(session_file, grill_state)

    def _normalize_control(value: GrillControlDecision | str) -> GrillControlDecision:
        if isinstance(value, GrillControlDecision):
            return value
        return GrillControlDecision(str(value or "").strip().lower())

    def _call_confirmation_provider() -> GrillControlDecision:
        assert grill_state is not None and session_file is not None
        preview_path = build_grill_confirmation_preview(
            output_path=output_file,
            preview_path=session_file.parent / "confirmation_preview.md",
            domain_draft_path=(
                grill_state.domain_draft_path
                if normalized_requirements_mode == GRILL_WITH_DOCS_MODE
                else None
            ),
        )
        try:
            raw = grill_confirmation_provider(preview_path, question_index=grill_state.question_seq)
        except TypeError as error:
            if "unexpected keyword argument" not in str(error):
                raise
            raw = grill_confirmation_provider(preview_path, grill_state.question_seq)  # type: ignore[misc]
        decision = _normalize_control(raw)
        if decision.action not in {"confirm", "continue", "revise", "abort"}:
            raise GrillSessionError(f"Grill 最终确认选择非法: {decision.action!r}")
        if decision.action == "revise" and not decision.message.strip():
            raise GrillSessionError("Grill 修改已有答案时必须提供修改说明")
        return decision

    def _call_budget_provider() -> GrillControlDecision:
        try:
            raw = grill_budget_provider(question_index=grill_state.question_seq)  # type: ignore[union-attr]
        except TypeError as error:
            if "unexpected keyword argument" not in str(error):
                raise
            raw = grill_budget_provider(grill_state.question_seq)  # type: ignore[misc,union-attr]
        decision = _normalize_control(raw)
        if decision.action not in {"continue", "finalize", "abort"}:
            raise GrillSessionError(f"Grill 问题预算选择非法: {decision.action!r}")
        return decision

    def _handle_budget_decision(answer: str) -> str:
        assert grill_state is not None and session_file is not None
        budget = _call_budget_provider()
        if budget.action == "abort":
            grill_state.state = "aborted"
            save_grill_session_state(session_file, grill_state)
            raise GrillSessionAborted("人类已在 Grill 问题预算门禁终止阶段")
        if budget.action == "continue":
            grill_state.budget_blocks_approved += 1
            grill_state.force_finalize = False
        else:
            grill_state.force_finalize = True
            answer = (
                answer
                + "\n\n人类要求停止扩展问题树；请把所有未决项明确写入需求澄清候选并进入最终确认，禁止静默假设。"
            )
        grill_state.pending_answer = answer
        grill_state.state = "answer_pending"
        grill_state.pending_question_path = ""
        grill_state.pending_question_hash = ""
        save_grill_session_state(session_file, grill_state)
        return answer

    def _validate_candidate_artifacts() -> None:
        assert grill_state is not None
        for path_text, expected_hash in grill_state.final_artifact_hashes.items():
            artifact = Path(path_text).expanduser().resolve()
            if not artifact.exists() or build_prefixed_sha256(artifact) != expected_hash:
                raise GrillSessionError(f"Grill 最终确认候选在恢复期间发生变化: {artifact}")

    def _persist_final_confirmation(candidate: HitlStatusDecision) -> None:
        assert grill_state is not None and session_file is not None
        output_base_hash = _prefixed_text_sha256(
            _grill_reference_free_text(output_file.read_text(encoding="utf-8"))
        )
        grill_state.final_confirmation = {
            "schema_version": "1.0",
            "status": "confirmed",
            "candidate_turn_id": candidate.turn_id,
            "candidate_round": candidate.hitl_round,
            "artifact_hashes": dict(grill_state.final_artifact_hashes),
            "output_base_hash": output_base_hash,
            "confirmed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        # This transition is the durable boundary: once it is visible, restart
        # resumes publication and must never ask the final-confirmation question
        # again for the same candidate hashes.
        grill_state.state = "publishing"
        save_grill_session_state(session_file, grill_state)

    def _validate_persisted_final_confirmation() -> None:
        assert grill_state is not None
        confirmation = grill_state.final_confirmation
        if str(confirmation.get("schema_version", "") or "") != "1.0":
            raise GrillSessionError("Grill publishing 状态缺少有效 final_confirmation")
        if str(confirmation.get("status", "") or "") != "confirmed":
            raise GrillSessionError("Grill final_confirmation 状态非法")
        if str(confirmation.get("candidate_turn_id", "") or "") != grill_state.candidate_turn_id:
            raise GrillSessionError("Grill final_confirmation turn_id 与候选不一致")
        try:
            confirmed_round = int(confirmation.get("candidate_round", 0) or 0)
        except (TypeError, ValueError) as error:
            raise GrillSessionError("Grill final_confirmation candidate_round 非法") from error
        if confirmed_round != grill_state.candidate_round:
            raise GrillSessionError("Grill final_confirmation round 与候选不一致")
        raw_hashes = confirmation.get("artifact_hashes", {})
        if not isinstance(raw_hashes, Mapping):
            raise GrillSessionError("Grill final_confirmation artifact_hashes 非法")
        confirmed_hashes = {str(key): str(value) for key, value in raw_hashes.items()}
        if confirmed_hashes != grill_state.final_artifact_hashes:
            raise GrillSessionError("Grill final_confirmation 未绑定当前候选哈希")

        for path_text, expected_hash in confirmed_hashes.items():
            artifact = Path(path_text).expanduser().resolve()
            if not artifact.exists() or not artifact.is_file():
                raise GrillSessionError(f"Grill 最终确认候选在发布恢复期间丢失: {artifact}")
            actual_hash = build_prefixed_sha256(artifact)
            if actual_hash == expected_hash:
                continue
            if artifact != output_file:
                raise GrillSessionError(f"Grill 最终确认候选在发布恢复期间发生变化: {artifact}")

            # The only allowed post-confirmation output mutation is the exact
            # system-owned reference block from a committed publish_intent.
            intent = grill_state.publish_intent
            if str(intent.get("state", "") or "") != "committed":
                raise GrillSessionError(f"Grill 最终确认候选在发布恢复期间发生变化: {artifact}")
            raw_published = intent.get("published_paths", [])
            if not isinstance(raw_published, list) or not raw_published:
                raise GrillSessionError("已提交的 publish_intent 缺少 published_paths")
            relative_paths: list[str] = []
            for published_text in raw_published:
                published = _resolve_project_owned_publish_path(
                    grill_project_dir,  # type: ignore[arg-type]
                    str(published_text),
                    label="publish_intent 已发布文件",
                )
                if not published.exists() or not published.is_file():
                    raise GrillSessionError(f"publish_intent 已发布文件不存在: {published}")
                relative = published.relative_to(Path(grill_project_dir).expanduser().resolve())  # type: ignore[arg-type]
                relative_paths.append(relative.as_posix())
            current_text = output_file.read_text(encoding="utf-8")
            matches = _grill_reference_marker_pattern().findall(current_text)
            expected_block = _build_grill_reference_block(relative_paths)
            if matches != [expected_block]:
                raise GrillSessionError("需求澄清候选包含未经确认的领域文档引用变更")
            output_base_hash = str(confirmation.get("output_base_hash", "") or "")
            if _prefixed_text_sha256(_grill_reference_free_text(current_text)) != output_base_hash:
                raise GrillSessionError("需求澄清候选在系统引用块之外发生变化")

    def _publishing_candidate_identity() -> HitlStatusDecision:
        assert grill_state is not None
        return HitlStatusDecision(
            stage=stage_name,
            turn_id=grill_state.candidate_turn_id,
            hitl_round=grill_state.candidate_round,
            status=HITL_STATUS_COMPLETED,
            summary="Grill candidate already confirmed; resuming publication",
            output_path=str(output_file),
            question_path="",
            record_path=str(record_file),
            status_path=str(status_file),
            payload={},
            artifact_hashes=dict(grill_state.final_artifact_hashes),
            written_at=str(grill_state.final_confirmation.get("confirmed_at", "") or ""),
        )

    def _load_candidate_decision() -> HitlStatusDecision:
        assert grill_state is not None
        if not grill_state.candidate_turn_id or grill_state.candidate_round < 1:
            raise GrillSessionError("Grill 最终确认候选缺少 turn 标识")
        _validate_candidate_artifacts()
        return validate_hitl_status_file(
            status_file,
            expected_stage=stage_name,
            expected_turn_id=grill_state.candidate_turn_id,
            expected_hitl_round=grill_state.candidate_round,
            expected_output_path=output_file,
            expected_question_path=question_file,
            expected_record_path=record_file,
        )

    def _publish_confirmed_documents() -> tuple[str, ...]:
        assert grill_state is not None and session_file is not None
        if normalized_requirements_mode != GRILL_WITH_DOCS_MODE:
            return ()
        if not grill_state.domain_draft_path:
            return ()
        draft = validate_grill_document_draft(grill_state.domain_draft_path)
        if not draft.context_markdown and not draft.adrs:
            return ()
        targets = tuple(
            Path(item).expanduser().resolve()
            for item in grill_state.context_target_snapshot
        )
        selected = grill_state.selected_context_target
        if not selected and len(targets) > 1:
            selected = str(grill_context_target_provider(targets)).strip()
            grill_state.selected_context_target = selected
            save_grill_session_state(session_file, grill_state)

        def persist_publish_intent(intent: dict[str, object]) -> None:
            # Persist every transaction state transition before returning to
            # the publisher.  A crash after a file rename can then reconcile
            # its content hash against the same fixed paths on restart.
            grill_state.publish_intent = dict(intent)
            save_grill_session_state(session_file, grill_state)

        published = publish_grill_document_draft(
            project_dir=grill_project_dir,  # type: ignore[arg-type]
            draft_path=grill_state.domain_draft_path,
            context_preimage_hashes=grill_state.context_preimage_hashes,
            context_target=selected or None,
            context_map_exists=grill_state.context_map_exists,
            context_map_hash=grill_state.context_map_hash,
            context_target_snapshot=grill_state.context_target_snapshot,
            publish_intent=grill_state.publish_intent,
            persist_publish_intent=persist_publish_intent,
        )
        grill_state.published_paths = list(published)
        save_grill_session_state(session_file, grill_state)
        return published

    def _refresh_candidate_after_publish(
        candidate: HitlStatusDecision,
        published: Sequence[str | Path],
    ) -> HitlStatusDecision:
        assert grill_state is not None and session_file is not None
        relative_paths = update_grill_document_references(
            requirements_path=output_file,
            project_dir=grill_project_dir,  # type: ignore[arg-type]
            published_paths=published,
        )
        payload = json.loads(status_file.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise GrillSessionError("Grill stage status 必须是 JSON 对象")
        artifact_hashes = payload.get("artifact_hashes", {})
        if not isinstance(artifact_hashes, dict):
            raise GrillSessionError("Grill stage status artifact_hashes 必须是对象")
        artifact_hashes[str(output_file)] = build_prefixed_sha256(output_file)
        payload["artifact_hashes"] = artifact_hashes
        grill_payload = payload.get("grill", {})
        if not isinstance(grill_payload, dict):
            grill_payload = {}
        grill_payload["published_paths"] = list(relative_paths)
        payload["grill"] = grill_payload
        payload["written_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
        _write_json_atomic(status_file, payload)

        turn_status_path = turns_dir / candidate.turn_id / "turn_status.json"
        _materialize_turn_status_file(
            turn_status_path,
            turn_id=candidate.turn_id,
            phase=turn_phase,
            stage_status_path=status_file,
            output_path=output_file,
            question_path=question_file,
            record_path=record_file,
        )
        refreshed = validate_hitl_status_file(
            status_file,
            expected_stage=stage_name,
            expected_turn_id=candidate.turn_id,
            expected_hitl_round=candidate.hitl_round,
            expected_output_path=output_file,
            expected_question_path=question_file,
            expected_record_path=record_file,
        )
        tracked_paths = {
            Path(path_text).expanduser().resolve()
            for path_text in grill_state.final_artifact_hashes
        }
        tracked_paths.update(Path(item).expanduser().resolve() for item in published)
        tracked_paths.add(output_file)
        grill_state.final_artifact_hashes = {
            str(path): build_prefixed_sha256(path)
            for path in sorted(tracked_paths, key=str)
            if path.exists() and path.is_file()
        }
        grill_state.published_paths = [str(Path(item).expanduser().resolve()) for item in published]
        # Persist the refreshed contracts and terminal state together.  A crash
        # before this save remains recoverable as ``publishing``; a crash after
        # it is already a complete, self-consistent session.
        grill_state.state = "confirmed"
        save_grill_session_state(session_file, grill_state)
        return refreshed

    def _handle_candidate_confirmation(
        candidate: HitlStatusDecision,
        *,
        resume_publishing: bool = False,
    ) -> tuple[HitlLoopResult | None, str]:
        assert grill_state is not None and session_file is not None
        if resume_publishing:
            _validate_persisted_final_confirmation()
        else:
            control = _call_confirmation_provider()
            if control.action == "abort":
                grill_state.state = "aborted"
                save_grill_session_state(session_file, grill_state)
                raise GrillSessionAborted("人类已终止 Grill 需求澄清阶段")
            if control.action in {"continue", "revise"}:
                instruction = (
                    "人类尚未确认共享理解。继续逐问，每轮只提出一个仍影响实现的决策问题。"
                    if control.action == "continue"
                    else f"人类要求修改已有答案：\n{control.message.strip()}\n请校正记录并继续逐问。"
                )
                grill_state.state = "answer_pending"
                grill_state.pending_answer = instruction
                if control.action == "revise":
                    grill_state.accepted_answers.append(
                        {
                            "kind": "revision",
                            "question_seq": grill_state.question_seq,
                            "answer": control.message.strip(),
                            "answered_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                        }
                    )
                grill_state.candidate_turn_id = ""
                grill_state.candidate_round = 0
                grill_state.final_artifact_hashes = {}
                grill_state.final_confirmation = {}
                grill_state.publish_intent = {}
                grill_state.force_finalize = False
                save_grill_session_state(session_file, grill_state)
                return None, instruction
            # The preview can remain open while files change. Never publish
            # content that was not included in the human-confirmed preview.
            _validate_candidate_artifacts()
            _persist_final_confirmation(candidate)
        try:
            published = _publish_confirmed_documents()
        except GrillDocumentConflict as error:
            conflict_control = _normalize_control(grill_document_conflict_provider(str(error)))
            publish_started = bool(
                grill_state.publish_intent
                and (
                    str(grill_state.publish_intent.get("state", ""))
                    in {"committing", "committed"}
                    or bool(grill_state.publish_intent.get("applied_paths", []))
                )
            )
            if publish_started and conflict_control.action in {"skip", "rebuild"}:
                raise GrillSessionError(
                    "领域文档发布事务已经开始，不能丢弃固定路径计划；"
                    "请先恢复同一事务或终止并人工检查已发布文件"
                ) from error
            if conflict_control.action == "abort":
                grill_state.state = "aborted"
                save_grill_session_state(session_file, grill_state)
                raise GrillSessionAborted("人类已终止 Grill with Docs 发布") from error
            if conflict_control.action == "skip":
                grill_state.publish_intent = {}
                published = ()
            elif conflict_control.action == "rebuild":
                apply_grill_context_snapshot(grill_state, grill_project_dir)  # type: ignore[arg-type]
                instruction = (
                    f"领域文档发布目标发生并发变化：{error}。"
                    "请重新读取 CONTEXT/CONTEXT-MAP，合并最新事实并更新受控 domain_drafts.json。"
                )
                grill_state.state = "answer_pending"
                grill_state.pending_answer = instruction
                grill_state.candidate_turn_id = ""
                grill_state.candidate_round = 0
                grill_state.final_artifact_hashes = {}
                grill_state.final_confirmation = {}
                grill_state.force_finalize = False
                grill_state.selected_context_target = ""
                grill_state.published_paths = []
                grill_state.publish_intent = {}
                save_grill_session_state(session_file, grill_state)
                return None, instruction
            else:
                raise GrillSessionError(f"Grill 文档冲突选择非法: {conflict_control.action!r}") from error
        if published:
            candidate = _refresh_candidate_after_publish(candidate, published)
        else:
            grill_state.published_paths = []
            if resume_publishing:
                candidate = _load_candidate_decision()
        grill_state.state = "confirmed"
        grill_state.pending_answer = ""
        grill_state.force_finalize = False
        save_grill_session_state(session_file, grill_state)
        return HitlLoopResult(
            decision=candidate,
            rounds_used=grill_state.turn_seq,
            human_responses=tuple(
                str(item.get("answer", "")) for item in grill_state.accepted_answers
            ),
        ), ""

    def _persist_question_and_collect_answer(decision: HitlStatusDecision) -> str:
        assert grill_state is not None and session_file is not None
        question = validate_grill_question_file(decision.question_path)
        grill_state.question_seq += 1
        grill_state.state = "awaiting_answer"
        grill_state.pending_question_path = str(Path(decision.question_path).expanduser().resolve())
        grill_state.pending_question_hash = build_prefixed_sha256(decision.question_path)
        grill_state.pending_answer = ""
        save_grill_session_state(session_file, grill_state)
        if human_input_provider is collect_terminal_hitl_response:
            answer = collect_grill_hitl_response(
                decision.question_path,
                hitl_round=decision.hitl_round,
                question_index=grill_state.question_seq,
            )
        else:
            try:
                answer = human_input_provider(decision.question_path, hitl_round=decision.hitl_round)
            except TypeError as error:
                if "unexpected keyword argument" not in str(error):
                    raise
                answer = human_input_provider(decision.question_path, decision.hitl_round)
        answer = str(answer or "").strip()
        if not answer:
            raise GrillSessionError("Grill 人类回复不能为空")
        grill_state.accepted_answers.append(
            {
                "question_seq": grill_state.question_seq,
                "question": question.question,
                "answer": answer,
                "question_hash": grill_state.pending_question_hash,
                "answered_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            }
        )
        grill_state.pending_answer = answer
        grill_state.state = "answer_pending"
        grill_state.pending_question_path = ""
        grill_state.pending_question_hash = ""
        save_grill_session_state(session_file, grill_state)
        if grill_state.question_seq >= grill_state.budget_blocks_approved * GRILL_QUESTION_BUDGET:
            grill_state.state = "awaiting_budget"
            save_grill_session_state(session_file, grill_state)
            return _handle_budget_decision(answer)
        return grill_state.pending_answer

    def _annotate_grill_decision(
        decision: HitlStatusDecision,
        question: GrillQuestion | None,
    ) -> HitlStatusDecision:
        assert grill_state is not None
        payload = dict(decision.payload)
        grill_payload: dict[str, object] = {
            "session_id": grill_state.session_id,
            "question_seq": grill_state.question_seq + (1 if question is not None else 0),
            "state": "question" if question is not None else "ready_for_confirmation",
            "question": question.question if question is not None else "",
            "why_it_matters": question.why_it_matters if question is not None else "",
            "recommended_answer": question.recommended_answer if question is not None else "",
            "answer_kind": question.answer_kind if question is not None else "",
            "options": list(question.options) if question is not None else [],
            "verified_facts": list(question.verified_facts) if question is not None else [],
        }
        payload["grill"] = grill_payload
        _write_json_atomic(status_file, payload)
        _materialize_turn_status_file(
            context.turn_status_path,
            turn_id=context.turn_id,
            phase=context.turn_phase,
            stage_status_path=status_file,
            output_path=output_file,
            question_path=question_file,
            record_path=record_file,
        )
        return validate_hitl_status_file(
            status_file,
            expected_stage=stage_name,
            expected_turn_id=context.turn_id,
            expected_hitl_round=context.hitl_round,
            expected_output_path=output_file,
            expected_question_path=question_file,
            expected_record_path=record_file,
        )

    def _replace_worker(current_worker: object, error: BaseException) -> object:
        nonlocal worker, grill_worker_recreated, grill_legacy_unbound_turn
        if replace_dead_worker is None or not is_worker_death_error(error):
            raise error
        worker = replace_dead_worker(current_worker, error)
        replaced, legacy_unbound = _bind_grill_worker(worker)
        grill_worker_recreated = grill_worker_recreated or replaced
        grill_legacy_unbound_turn = grill_legacy_unbound_turn or legacy_unbound
        if grill_state is not None and session_file is not None:
            grill_state.turn_submission_cursor = "not_started"
            grill_state.turn_worker_state_revision = 0
            save_grill_session_state(session_file, grill_state)
        if on_worker_starting is not None:
            on_worker_starting(worker)
        if grill_state is None:
            worker.ensure_agent_ready(timeout_sec=min(timeout_sec, 60.0))
            notify_runtime_state_changed()
            if on_worker_started is not None:
                on_worker_started(worker)
        return worker

    # A recovered question is shown again from the persisted file without invoking the agent.
    recovered_human_message = ""
    if grill_state is not None:
        if grill_state.state == "confirmed":
            candidate = _load_candidate_decision()
            return HitlLoopResult(
                decision=candidate,
                rounds_used=grill_state.turn_seq,
                human_responses=tuple(
                    str(item.get("answer", "")) for item in grill_state.accepted_answers
                ),
            )
        if grill_state.state == "publishing":
            recovered_result, recovered_human_message = _handle_candidate_confirmation(
                _publishing_candidate_identity(),
                resume_publishing=True,
            )
            if recovered_result is not None:
                return recovered_result
        elif grill_state.state == "awaiting_confirmation":
            recovered_result, recovered_human_message = _handle_candidate_confirmation(
                _load_candidate_decision()
            )
            if recovered_result is not None:
                return recovered_result
        elif grill_state.state == "awaiting_answer" and grill_state.pending_question_path:
            recovered_decision = validate_hitl_status_file(
                status_file,
                expected_stage=stage_name,
                expected_turn_id=grill_state.candidate_turn_id or f"{label_prefix}_{grill_state.turn_seq}",
                expected_hitl_round=grill_state.candidate_round or grill_state.turn_seq,
                expected_output_path=output_file,
                expected_question_path=question_file,
                expected_record_path=record_file,
            )
            # Do not increment question_seq for a question already counted before shutdown.
            question = validate_grill_question_file(recovered_decision.question_path)
            if human_input_provider is collect_terminal_hitl_response:
                recovered_human_message = collect_grill_hitl_response(
                    recovered_decision.question_path,
                    hitl_round=recovered_decision.hitl_round,
                    question_index=grill_state.question_seq,
                )
            else:
                try:
                    recovered_human_message = human_input_provider(
                        recovered_decision.question_path,
                        hitl_round=recovered_decision.hitl_round,
                    )
                except TypeError as error:
                    if "unexpected keyword argument" not in str(error):
                        raise
                    recovered_human_message = human_input_provider(
                        recovered_decision.question_path,
                        recovered_decision.hitl_round,
                    )
            recovered_human_message = str(recovered_human_message or "").strip()
            if not recovered_human_message:
                raise GrillSessionError("Grill 人类回复不能为空")
            grill_state.accepted_answers.append(
                {
                    "question_seq": grill_state.question_seq,
                    "question": question.question,
                    "answer": recovered_human_message,
                    "question_hash": grill_state.pending_question_hash,
                    "answered_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                }
            )
            grill_state.pending_answer = recovered_human_message
            grill_state.state = "answer_pending"
            grill_state.pending_question_path = ""
            grill_state.pending_question_hash = ""
            save_grill_session_state(session_file, grill_state)
            if grill_state.question_seq >= grill_state.budget_blocks_approved * GRILL_QUESTION_BUDGET:
                grill_state.state = "awaiting_budget"
                save_grill_session_state(session_file, grill_state)
                recovered_human_message = _handle_budget_decision(recovered_human_message)
        elif grill_state.state == "awaiting_budget" and grill_state.pending_answer:
            recovered_human_message = _handle_budget_decision(grill_state.pending_answer)
        elif grill_state.pending_answer:
            recovered_human_message = grill_state.pending_answer

    skip_ready_for_persisted_turn = bool(
        grill_state is not None
        and grill_state.state == "turn_in_progress"
    )
    while not skip_ready_for_persisted_turn:
        try:
            if on_worker_starting is not None:
                on_worker_starting(worker)
            worker.ensure_agent_ready(timeout_sec=min(timeout_sec, 60.0))
            notify_runtime_state_changed()
            if on_worker_started is not None:
                on_worker_started(worker)
            break
        except Exception as error:  # noqa: BLE001
            if isinstance(error, AgentInterventionRequired) and startup_intervention_handler is not None:
                startup_intervention_handler(worker, error)
                continue
            if replace_dead_worker is None or not is_worker_death_error(error):
                raise
            worker = _replace_worker(worker, error)
    if skip_ready_for_persisted_turn:
        notify_runtime_state_changed()
        if on_worker_started is not None:
            on_worker_started(worker)

    human_responses: list[str] = (
        [str(item.get("answer", "")) for item in grill_state.accepted_answers]
        if grill_state is not None
        else []
    )
    next_human_message = recovered_human_message
    standard_round = 0
    while True:
        resuming_grill_turn = bool(
            grill_state is not None and grill_state.state == "turn_in_progress"
        )
        if grill_state is None:
            standard_round += 1
            if standard_round > max_hitl_rounds:
                raise RuntimeError(f"{stage_name} HITL 轮次超过上限: {max_hitl_rounds}")
            hitl_round = standard_round
            initial_turn = hitl_round == 1
        else:
            initial_turn = (
                grill_state.turn_seq == 0
                or (
                    resuming_grill_turn
                    and grill_state.turn_seq == 1
                    and grill_state.question_seq == 0
                    and not grill_state.accepted_answers
                    and not grill_state.pending_answer
                )
            )
            if grill_state.state == "turn_in_progress":
                hitl_round = grill_state.turn_seq
            else:
                grill_state.turn_seq += 1
                hitl_round = grill_state.turn_seq
                grill_state.state = "turn_in_progress"
        turn_id = f"{label_prefix}_{hitl_round}"
        turn_label = f"{label_prefix}_round_{hitl_round}"
        turn_status_path = turns_dir / turn_id / "turn_status.json"
        turn_status_path.parent.mkdir(parents=True, exist_ok=True)
        if grill_state is not None:
            if resuming_grill_turn:
                if grill_state.turn_id and grill_state.turn_id != turn_id:
                    raise GrillSessionError(
                        "Grill 持久化 turn 游标与当前轮次不一致: "
                        f"persisted={grill_state.turn_id}, expected={turn_id}"
                    )
                if grill_state.turn_label and grill_state.turn_label != turn_label:
                    raise GrillSessionError(
                        "Grill 持久化 turn label 与当前轮次不一致: "
                        f"persisted={grill_state.turn_label}, expected={turn_label}"
                    )
                if (
                    grill_state.turn_status_path
                    and str(Path(grill_state.turn_status_path).expanduser().resolve())
                    != str(turn_status_path.resolve())
                ):
                    raise GrillSessionError(
                        "Grill 恢复路径未指向原 turn contract: "
                        f"persisted={grill_state.turn_status_path}, current={turn_status_path}"
                    )
                if (
                    grill_state.turn_stage_status_path
                    and str(Path(grill_state.turn_stage_status_path).expanduser().resolve())
                    != str(status_file)
                ):
                    raise GrillSessionError(
                        "Grill 恢复路径未指向原 stage contract: "
                        f"persisted={grill_state.turn_stage_status_path}, current={status_file}"
                    )
            grill_state.turn_id = turn_id
            grill_state.turn_label = turn_label
            grill_state.turn_status_path = str(turn_status_path.resolve())
            grill_state.turn_stage_status_path = str(status_file)
            if not resuming_grill_turn:
                grill_state.turn_submission_cursor = "not_started"
                grill_state.turn_worker_state_revision = 0
                grill_state.turn_contract_repair_attempts = 0
                grill_state.turn_manual_intervention_used = False
                grill_state.turn_fresh_baseline_hashes = {}
            save_grill_session_state(session_file, grill_state)
        context = HitlPromptContext(
            stage_name=stage_name,
            hitl_round=hitl_round,
            turn_id=turn_id,
            turn_phase=turn_phase,
            output_path=str(output_file),
            question_path=str(question_file),
            record_path=str(record_file),
            stage_status_path=str(status_file),
            turn_status_path=str(turn_status_path),
        )
        question_cleared_for_turn = False

        def _clear_question_for_turn() -> None:
            nonlocal question_cleared_for_turn
            if question_cleared_for_turn:
                return
            _invoke_optional_hitl_callback(on_before_question_clear, "on_before_question_clear", context)
            question_file.write_text("", encoding="utf-8")
            question_cleared_for_turn = True

        # A resumed turn must inspect its persisted contract before mutating
        # any output file. In particular, a replacement worker may arrive
        # after the old worker wrote a valid HITL question and status.
        if not resuming_grill_turn:
            _clear_question_for_turn()
        prompt_kind = "initial" if initial_turn else "hitl_answer"
        prompt = (
            initial_prompt_builder(context)
            if initial_turn
            else hitl_prompt_builder(
                next_human_message or (human_responses[-1] if human_responses else "请继续逐问"),
                context,
            )
        )
        if grill_state is not None:
            if resuming_grill_turn and grill_state.turn_prompt_text:
                actual_prompt_hash = hashlib.sha256(
                    grill_state.turn_prompt_text.encode("utf-8")
                ).hexdigest()
                if grill_state.turn_prompt_hash != actual_prompt_hash:
                    raise GrillSessionError("Grill 持久化 turn prompt 哈希不匹配")
                prompt = grill_state.turn_prompt_text
                prompt_kind = grill_state.turn_prompt_kind or prompt_kind
            elif not resuming_grill_turn or grill_state.turn_submission_cursor == "not_started":
                _persist_grill_turn_prompt(prompt, prompt_kind=prompt_kind)
        # Freeze one Graphify profile for the whole logical HITL turn. Any
        # contract-repair prompt below must reuse the same graph generation and
        # evidence instead of silently widening its source facts.
        graphify_profile: object | None = None
        if graphify_context_factory is not None:
            prepare_graphify = getattr(worker, "prepare_graphify_turn_profile", None)
            if callable(prepare_graphify):
                graphify_context = graphify_context_factory(context)
                try:
                    parameters = inspect.signature(prepare_graphify).parameters.values()
                except (TypeError, ValueError):
                    parameters = ()
                if any(
                    parameter.name == "turn_context" or parameter.kind == inspect.Parameter.VAR_KEYWORD
                    for parameter in parameters
                ):
                    graphify_profile = prepare_graphify(
                        prompt,
                        turn_context=graphify_context,
                    )
                else:
                    graphify_profile = prepare_graphify(prompt)
        effective_fresh_completion_paths = list(
            tuple(fresh_completion_paths) if hitl_round >= fresh_start_round else ()
        )
        if grill_state is not None and output_file not in {
            Path(item).expanduser().resolve() for item in effective_fresh_completion_paths
        }:
            # A pre-existing requirements document is not proof that the
            # interrupted Grill prompt completed. Completed candidates must be
            # fresh for this logical turn; HITL questions remain unaffected.
            effective_fresh_completion_paths.append(output_file)
        if (
            grill_state is not None
            and resuming_grill_turn
            and grill_state.turn_fresh_baseline_hashes
        ):
            baseline_fresh_hashes = dict(grill_state.turn_fresh_baseline_hashes)
        else:
            if (
                grill_state is not None
                and resuming_grill_turn
                and grill_state.turn_submission_cursor
                not in {"not_started", "repair_not_started"}
            ):
                raise GrillSessionError(
                    "Grill turn_in_progress 缺少提交前 artifact baseline；无法安全判断旧合同，拒绝重发"
                )
            baseline_fresh_hashes = {
                str(Path(item).expanduser().resolve()): _read_optional_artifact_hash(item)
                for item in effective_fresh_completion_paths
            }
            if grill_state is not None:
                grill_state.turn_fresh_baseline_hashes = dict(baseline_fresh_hashes)
                save_grill_session_state(session_file, grill_state)
        contract = build_turn_status_contract(
            turn_status_path=turn_status_path,
            turn_id=turn_id,
            turn_phase=turn_phase,
            stage_status_path=status_file,
            stage_name=stage_name,
            hitl_round=hitl_round,
            output_path=output_file,
            question_path=question_file,
            record_path=record_file,
            fresh_completion_paths=tuple(effective_fresh_completion_paths),
            baseline_fresh_hashes=baseline_fresh_hashes,
        )
        contract_repair_attempts = (
            grill_state.turn_contract_repair_attempts
            if grill_state is not None and resuming_grill_turn
            else 0
        )
        grill_manual_intervention_used = bool(
            grill_state is not None
            and resuming_grill_turn
            and grill_state.turn_manual_intervention_used
        )
        decision: HitlStatusDecision | None = None
        resume_this_submission = resuming_grill_turn
        while True:
            turn_worker = worker
            if on_agent_turn_started is not None:
                on_agent_turn_started(context, turn_worker)
            try:
                turn_kwargs: dict[str, object] = {
                    "label": turn_label,
                    "prompt": prompt,
                    "completion_contract": contract,
                    "timeout_sec": timeout_sec,
                }
                if startup_intervention_handler is not None:
                    with_runtime_handler = False
                    try:
                        parameters = inspect.signature(turn_worker.run_turn).parameters.values()
                    except (TypeError, ValueError):
                        parameters = ()
                    with_runtime_handler = any(
                        parameter.name == "runtime_intervention_handler"
                        for parameter in parameters
                    )
                    if with_runtime_handler:
                        turn_kwargs["runtime_intervention_handler"] = startup_intervention_handler
                grill_profile: object | None = None
                if grill_state is not None and grill_turn_profile_factory is not None:
                    grill_profile = grill_turn_profile_factory(grill_state.question_seq)
                    try:
                        parameters = inspect.signature(turn_worker.run_turn).parameters.values()
                    except (TypeError, ValueError):
                        parameters = ()
                    supports_grill_profile = any(
                        parameter.name == "grill_profile" or parameter.kind == inspect.Parameter.VAR_KEYWORD
                        for parameter in parameters
                    )
                    if supports_grill_profile:
                        turn_kwargs["grill_profile"] = grill_profile
                if graphify_profile is not None:
                    try:
                        parameters = inspect.signature(turn_worker.run_turn).parameters.values()
                    except (TypeError, ValueError):
                        parameters = ()
                    if any(
                        parameter.name == "graphify_profile" or parameter.kind == inspect.Parameter.VAR_KEYWORD
                        for parameter in parameters
                    ):
                        turn_kwargs["graphify_profile"] = graphify_profile
                if resume_this_submission:
                    if grill_legacy_unbound_turn:
                        raise GrillSessionError(
                            "Grill turn_in_progress 缺少原 worker 游标；无法证明提示词是否已提交，拒绝重发"
                        )
                    resume_turn = getattr(turn_worker, "resume_completion_turn", None)
                    if not callable(resume_turn):
                        raise GrillSessionError(
                            "Grill turn_in_progress 无法使用安全恢复接口；拒绝重复提交提示词"
                        )
                    result = resume_turn(
                        label=turn_label,
                        completion_contract=contract,
                        timeout_sec=timeout_sec,
                        submission_cursor=(
                            grill_state.turn_submission_cursor
                            if grill_state is not None
                            else ""
                        ),
                        stage_status_path=status_file,
                        grill_profile=grill_profile,
                        graphify_profile=graphify_profile,
                        runtime_intervention_handler=startup_intervention_handler,
                    )
                    resume_this_submission = False
                    if result is None:
                        # ``None`` is the resume API's proof that this logical
                        # turn never crossed the mutation boundary. Only now is
                        # it safe to clear a stale prior question and submit.
                        _clear_question_for_turn()
                        result = turn_worker.run_turn(**turn_kwargs)
                else:
                    result = turn_worker.run_turn(**turn_kwargs)
            except Exception as error:  # noqa: BLE001
                if isinstance(error, AgentStartupInterventionRequired) and startup_intervention_handler is not None:
                    startup_intervention_handler(turn_worker, error)
                    continue
                if replace_dead_worker is None or not is_worker_death_error(error):
                    raise
                _replace_worker(turn_worker, error)
                # A replacement Grill pane must first validate the original
                # persisted contract. Only a confirmed-missing contract may be
                # replayed into the new launch generation.
                resume_this_submission = grill_state is not None
                continue
            finally:
                _sync_grill_turn_cursor(turn_worker)
                if on_agent_turn_finished is not None:
                    on_agent_turn_finished(context, turn_worker)
            if not result.ok and replace_dead_worker is not None:
                error = RuntimeError(result.clean_output or f"{stage_name} 阶段执行失败")
                if is_worker_death_error(error):
                    _replace_worker(turn_worker, error)
                    continue
            if (
                not result.ok
                and is_turn_artifact_contract_error(result.clean_output)
                and contract_repair_attempts < max_contract_repair_attempts
            ):
                contract_repair_attempts += 1
                if grill_state is not None and session_file is not None:
                    grill_state.turn_contract_repair_attempts = contract_repair_attempts
                    save_grill_session_state(session_file, grill_state)
                prompt = build_hitl_contract_repair_prompt(
                    context=context,
                    error_text=result.clean_output,
                )
                _persist_grill_turn_prompt(prompt, prompt_kind="contract_repair")
                continue
            if not result.ok:
                break
            try:
                contract.validator(contract.status_path)
                decision = validate_hitl_status_file(
                    status_file,
                    expected_stage=stage_name,
                    expected_turn_id=turn_id,
                    expected_hitl_round=hitl_round,
                    expected_output_path=output_file,
                    expected_question_path=question_file,
                    expected_record_path=record_file,
                )
                if grill_state is not None and decision.status == HITL_STATUS_HITL:
                    if grill_state.force_finalize:
                        raise GrillSessionError(
                            "人类已要求结束当前 20 题预算块；本轮必须形成待确认候选，不能继续提问"
                        )
                    question = validate_grill_question_file(decision.question_path)
                    decision = _annotate_grill_decision(decision, question)
                elif grill_state is not None and decision.status == HITL_STATUS_COMPLETED:
                    if not _read_non_empty_text(record_file):
                        raise GrillSessionError(
                            "Grill 完成候选必须包含非空的人机交互记录"
                        )
                    if (
                        normalized_requirements_mode == GRILL_WITH_DOCS_MODE
                        and grill_state.domain_draft_path
                    ):
                        validate_grill_document_draft(grill_state.domain_draft_path)
                    decision = _annotate_grill_decision(decision, None)
            except GrillSessionError as error:
                if contract_repair_attempts >= max_contract_repair_attempts:
                    if grill_contract_intervention_handler is None or grill_manual_intervention_used:
                        raise
                    grill_manual_intervention_used = True
                    grill_state.turn_manual_intervention_used = True
                    save_grill_session_state(session_file, grill_state)
                    action = grill_contract_intervention_handler(
                        turn_worker,
                        error,
                        context,
                        contract_repair_attempts,
                    )
                    if "worker_dead" in str(action or ""):
                        death_error = RuntimeError(
                            f"tmux pane died during Grill contract intervention: {error}"
                        )
                        if replace_dead_worker is None:
                            raise death_error from error
                        _replace_worker(turn_worker, death_error)
                        # The one-time manual-intervention latch remains set.
                        # Recheck the old contract on the replacement before a
                        # single repair replay; never open a second HITL prompt.
                        resume_this_submission = grill_state is not None
                        continue
                    try:
                        contract.validator(contract.status_path)
                        decision = validate_hitl_status_file(
                            status_file,
                            expected_stage=stage_name,
                            expected_turn_id=turn_id,
                            expected_hitl_round=hitl_round,
                            expected_output_path=output_file,
                            expected_question_path=question_file,
                            expected_record_path=record_file,
                        )
                        if decision.status == HITL_STATUS_HITL:
                            if grill_state.force_finalize:
                                raise GrillSessionError(
                                    "人类已要求结束当前 20 题预算块；本轮必须形成待确认候选，不能继续提问"
                                )
                            question = validate_grill_question_file(decision.question_path)
                            decision = _annotate_grill_decision(decision, question)
                        elif decision.status == HITL_STATUS_COMPLETED:
                            if not _read_non_empty_text(record_file):
                                raise GrillSessionError(
                                    "Grill 完成候选必须包含非空的人机交互记录"
                                )
                            if (
                                normalized_requirements_mode == GRILL_WITH_DOCS_MODE
                                and grill_state.domain_draft_path
                            ):
                                validate_grill_document_draft(grill_state.domain_draft_path)
                            decision = _annotate_grill_decision(decision, None)
                    except Exception as post_intervention_error:  # noqa: BLE001
                        raise GrillSessionError(
                            "Grill 逐问结构在一次人工介入后仍不符合要求"
                        ) from post_intervention_error
                    break
                contract_repair_attempts += 1
                grill_state.turn_contract_repair_attempts = contract_repair_attempts
                save_grill_session_state(session_file, grill_state)
                prompt = build_grill_contract_repair_prompt(context=context, error_text=str(error))
                _persist_grill_turn_prompt(prompt, prompt_kind="grill_contract_repair")
                continue
            break
        if not result.ok:
            raise RuntimeError(result.clean_output or f"{stage_name} 阶段执行失败")
        if decision is None:
            contract.validator(contract.status_path)
            decision = validate_hitl_status_file(
                status_file,
                expected_stage=stage_name,
                expected_turn_id=turn_id,
                expected_hitl_round=hitl_round,
                expected_output_path=output_file,
                expected_question_path=question_file,
                expected_record_path=record_file,
            )
        if decision.status == HITL_STATUS_COMPLETED:
            if grill_state is not None:
                grill_state.state = "awaiting_confirmation"
                grill_state.pending_answer = ""
                grill_state.pending_question_path = ""
                grill_state.pending_question_hash = ""
                grill_state.force_finalize = False
                grill_state.candidate_turn_id = turn_id
                grill_state.candidate_round = hitl_round
                grill_state.final_confirmation = {}
                grill_state.publish_intent = {}
                if not _read_non_empty_text(record_file):
                    raise GrillSessionError(
                        "Grill 完成候选必须包含非空的人机交互记录"
                    )
                candidate_paths = [output_file, record_file]
                if grill_state.domain_draft_path:
                    draft_path = Path(grill_state.domain_draft_path).expanduser().resolve()
                    if draft_path.exists() and draft_path.is_file():
                        validate_grill_document_draft(draft_path)
                        candidate_paths.append(draft_path)
                grill_state.final_artifact_hashes = {
                    str(path): build_prefixed_sha256(path) for path in candidate_paths
                }
                save_grill_session_state(session_file, grill_state)
                confirmed_result, next_human_message = _handle_candidate_confirmation(decision)
                if confirmed_result is not None:
                    return confirmed_result
                continue
            return HitlLoopResult(
                decision=decision,
                rounds_used=hitl_round,
                human_responses=tuple(human_responses),
            )
        if decision.status == HITL_STATUS_ERROR:
            raise RuntimeError(decision.summary or f"{stage_name} 状态文件返回 error")
        if grill_state is None and hitl_round >= max_hitl_rounds:
            raise RuntimeError(f"{stage_name} HITL 轮次超过上限: {max_hitl_rounds}")
        _invoke_optional_hitl_callback(on_hitl_question, "on_hitl_question", context, decision)
        if grill_state is not None:
            grill_state.candidate_turn_id = turn_id
            grill_state.candidate_round = hitl_round
            human_message = _persist_question_and_collect_answer(decision)
        else:
            try:
                human_message = human_input_provider(decision.question_path, hitl_round=hitl_round)
            except TypeError as error:
                if "unexpected keyword argument" not in str(error):
                    raise
                human_message = human_input_provider(decision.question_path, hitl_round)
        human_history_path = turns_dir / turn_id / f"human_response_round_{hitl_round}.md"
        human_history_path.write_text(human_message.strip() + "\n", encoding="utf-8")
        _invoke_optional_hitl_callback(
            on_hitl_answer,
            "on_hitl_answer",
            context,
            human_message,
            human_history_path,
        )
        human_responses.append(human_message)
        next_human_message = human_message
