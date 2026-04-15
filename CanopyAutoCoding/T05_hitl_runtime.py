# -*- encoding: utf-8 -*-
"""
@File: T05_hitl_runtime.py
@Modify Time: 2026/4/13
@Author: Kevin-Chen
@Descriptions: 通用 HITL 文档协议与 tmux agent 循环运行时
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from T02_tmux_agents import DEFAULT_COMMAND_TIMEOUT_SEC, TurnFileContract, TurnFileResult


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


def build_turn_status_contract(
    *,
    turn_status_path: str | Path,
    turn_id: str,
    turn_phase: str,
    stage_status_path: str | Path,
) -> TurnFileContract:
    expected_stage_status = str(Path(stage_status_path).expanduser().resolve())

    def validator(path: Path) -> TurnFileResult:
        status_path = Path(path).expanduser().resolve()
        if not status_path.exists():
            raise FileNotFoundError(f"缺少 turn_status.json: {status_path}")
        payload = json.loads(status_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("turn_status.json 必须是 JSON 对象")
        if str(payload.get("schema_version", "")).strip() != TURN_STATUS_SCHEMA_VERSION:
            raise ValueError("turn_status.json schema_version 非法")
        if str(payload.get("turn_id", "")).strip() != turn_id:
            raise ValueError("turn_status.json turn_id 非法")
        if str(payload.get("phase", "")).strip() != turn_phase:
            raise ValueError("turn_status.json phase 非法")
        if str(payload.get("status", "")).strip().lower() != "done":
            raise ValueError("turn_status.json status 非法")
        parse_iso_timestamp(payload.get("written_at", ""))
        artifacts = payload.get("artifacts", {})
        if not isinstance(artifacts, dict):
            raise ValueError("turn_status.json artifacts 必须是对象")
        stage_status_in_payload = str(artifacts.get("stage_status", "")).strip()
        if stage_status_in_payload != expected_stage_status:
            raise ValueError("turn_status.json stage_status 非法")
        artifact_paths = _collect_artifact_paths(artifacts)
        artifact_hashes = payload.get("artifact_hashes", {})
        if not isinstance(artifact_hashes, dict):
            raise ValueError("turn_status.json artifact_hashes 必须是对象")
        validated_hashes: dict[str, str] = {}
        for artifact_path_text in artifact_paths:
            artifact_path = Path(artifact_path_text).expanduser().resolve()
            if not artifact_path.exists() or not artifact_path.is_file():
                raise FileNotFoundError(f"turn_status.json 引用的文件不存在: {artifact_path}")
            expected_hash = str(artifact_hashes.get(str(artifact_path), "")).strip()
            actual_hash = build_prefixed_sha256(artifact_path)
            if expected_hash != actual_hash:
                raise ValueError(f"turn_status.json artifact_hashes 不匹配: {artifact_path}")
            validated_hashes[str(artifact_path)] = actual_hash
        return TurnFileResult(
            status_path=str(status_path),
            payload=payload,
            artifact_paths={f"artifact_{index}": item for index, item in enumerate(artifact_paths, start=1)},
            artifact_hashes=validated_hashes,
            validated_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        )

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
    print()
    print(f"HITL 第 {hitl_round} 轮，需要人工补充信息")
    print(f"问题文档: {question_file}")
    print(question_text or "(问题文档为空)")
    print("请输入你的回复，单独一行输入 EOF 结束:")
    while True:
        lines: list[str] = []
        while True:
            line = input()
            if line == "EOF":
                break
            lines.append(line)
        text = "\n".join(lines).strip()
        if text:
            return text
        print("回复不能为空，请重新输入，单独一行输入 EOF 结束:")


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
    on_worker_started: Callable[[object], None] | None = None,
    on_agent_turn_started: Callable[[HitlPromptContext, object], None] | None = None,
    on_agent_turn_finished: Callable[[HitlPromptContext, object], None] | None = None,
    timeout_sec: float = DEFAULT_COMMAND_TIMEOUT_SEC,
    max_hitl_rounds: int = 8,
) -> HitlLoopResult:
    output_file = Path(output_path).expanduser().resolve()
    question_file = Path(question_path).expanduser().resolve()
    record_file = Path(record_path).expanduser().resolve()
    status_file = Path(stage_status_path).expanduser().resolve()
    turns_dir = Path(turns_root).expanduser().resolve()
    turns_dir.mkdir(parents=True, exist_ok=True)

    worker.ensure_agent_ready(timeout_sec=min(timeout_sec, 60.0))
    if on_worker_started is not None:
        on_worker_started(worker)

    human_responses: list[str] = []
    for hitl_round in range(1, max_hitl_rounds + 1):
        turn_id = f"{label_prefix}_{hitl_round}"
        turn_status_path = turns_dir / turn_id / "turn_status.json"
        turn_status_path.parent.mkdir(parents=True, exist_ok=True)
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
        prompt = (
            initial_prompt_builder(context)
            if hitl_round == 1
            else hitl_prompt_builder(human_responses[-1], context)
        )
        contract = build_turn_status_contract(
            turn_status_path=turn_status_path,
            turn_id=turn_id,
            turn_phase=turn_phase,
            stage_status_path=status_file,
        )
        if on_agent_turn_started is not None:
            on_agent_turn_started(context, worker)
        try:
            result = worker.run_turn(
                label=f"{label_prefix}_round_{hitl_round}",
                prompt=prompt,
                completion_contract=contract,
                timeout_sec=timeout_sec,
            )
        finally:
            if on_agent_turn_finished is not None:
                on_agent_turn_finished(context, worker)
        if not result.ok:
            raise RuntimeError(result.clean_output or f"{stage_name} 阶段执行失败")
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
            return HitlLoopResult(
                decision=decision,
                rounds_used=hitl_round,
                human_responses=tuple(human_responses),
            )
        if decision.status == HITL_STATUS_ERROR:
            raise RuntimeError(decision.summary or f"{stage_name} 状态文件返回 error")
        if hitl_round >= max_hitl_rounds:
            raise RuntimeError(f"{stage_name} HITL 轮次超过上限: {max_hitl_rounds}")
        try:
            human_message = human_input_provider(decision.question_path, hitl_round=hitl_round)
        except TypeError as error:
            if "unexpected keyword argument" not in str(error):
                raise
            human_message = human_input_provider(decision.question_path, hitl_round)
        human_history_path = turns_dir / turn_id / f"human_response_round_{hitl_round}.md"
        human_history_path.write_text(human_message.strip() + "\n", encoding="utf-8")
        human_responses.append(human_message)
    raise RuntimeError(f"{stage_name} HITL 轮次超过上限: {max_hitl_rounds}")
