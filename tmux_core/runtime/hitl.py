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
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Mapping, Sequence

from tmux_core.runtime.contracts import TurnFileContract, TurnFileResult
from tmux_core.runtime.tmux_runtime import DEFAULT_COMMAND_TIMEOUT_SEC, is_worker_death_error
from T09_terminal_ops import (
    BridgeTerminalUI,
    get_terminal_ui,
    message,
    notify_runtime_state_changed,
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
    replace_dead_worker: Callable[[object, BaseException], object] | None = None,
    timeout_sec: float = DEFAULT_COMMAND_TIMEOUT_SEC,
    max_hitl_rounds: int = 8,
    fresh_completion_paths: Sequence[str | Path] = (),
    fresh_completion_start_round: int = 1,
) -> HitlLoopResult:
    output_file = Path(output_path).expanduser().resolve()
    question_file = Path(question_path).expanduser().resolve()
    record_file = Path(record_path).expanduser().resolve()
    status_file = Path(stage_status_path).expanduser().resolve()
    turns_dir = Path(turns_root).expanduser().resolve()
    turns_dir.mkdir(parents=True, exist_ok=True)
    fresh_start_round = max(int(fresh_completion_start_round), 1)

    def _replace_worker(current_worker: object, error: BaseException) -> object:
        nonlocal worker
        if replace_dead_worker is None or not is_worker_death_error(error):
            raise error
        worker = replace_dead_worker(current_worker, error)
        if on_worker_starting is not None:
            on_worker_starting(worker)
        worker.ensure_agent_ready(timeout_sec=min(timeout_sec, 60.0))
        notify_runtime_state_changed()
        if on_worker_started is not None:
            on_worker_started(worker)
        return worker

    while True:
        try:
            if on_worker_starting is not None:
                on_worker_starting(worker)
            worker.ensure_agent_ready(timeout_sec=min(timeout_sec, 60.0))
            notify_runtime_state_changed()
            if on_worker_started is not None:
                on_worker_started(worker)
            break
        except Exception as error:  # noqa: BLE001
            if replace_dead_worker is None or not is_worker_death_error(error):
                raise
            worker = replace_dead_worker(worker, error)

    human_responses: list[str] = []
    for hitl_round in range(1, max_hitl_rounds + 1):
        turn_id = f"{label_prefix}_{hitl_round}"
        turn_status_path = turns_dir / turn_id / "turn_status.json"
        turn_status_path.parent.mkdir(parents=True, exist_ok=True)
        question_file.write_text("", encoding="utf-8")
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
        effective_fresh_completion_paths = (
            tuple(fresh_completion_paths) if hitl_round >= fresh_start_round else ()
        )
        baseline_fresh_hashes = {
            str(Path(item).expanduser().resolve()): _read_optional_artifact_hash(item)
            for item in effective_fresh_completion_paths
        }
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
            fresh_completion_paths=effective_fresh_completion_paths,
            baseline_fresh_hashes=baseline_fresh_hashes,
        )
        while True:
            turn_worker = worker
            if on_agent_turn_started is not None:
                on_agent_turn_started(context, turn_worker)
            try:
                result = turn_worker.run_turn(
                    label=f"{label_prefix}_round_{hitl_round}",
                    prompt=prompt,
                    completion_contract=contract,
                    timeout_sec=timeout_sec,
                )
            except Exception as error:  # noqa: BLE001
                if replace_dead_worker is None or not is_worker_death_error(error):
                    raise
                _replace_worker(turn_worker, error)
                continue
            finally:
                if on_agent_turn_finished is not None:
                    on_agent_turn_finished(context, turn_worker)
            if not result.ok and replace_dead_worker is not None:
                error = RuntimeError(result.clean_output or f"{stage_name} 阶段执行失败")
                if is_worker_death_error(error):
                    _replace_worker(turn_worker, error)
                    continue
            break
        if not result.ok:
            raise RuntimeError(result.clean_output or f"{stage_name} 阶段执行失败")
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
