# -*- encoding: utf-8 -*-
"""
@File: T07_task_completion.py
@Modify Time: 2026/4/16
@Author: Kevin-Chen
@Descriptions: manifest-driven task completion helper for tmux agent runtime
"""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from T04_common_prompt import TASK_DONE_MARKER

TASK_RESULT_SCHEMA_VERSION = "1.0"
TASK_MANIFEST_SCHEMA_VERSION = "1.0"
TASK_STATUS_RUNNING = "running"
TASK_STATUS_DONE = "done"
TASK_RESULT_READY = "ready"
TASK_RESULT_COMPLETED = "completed"
TASK_RESULT_HITL = "hitl"
TASK_RESULT_ERROR = "error"
TASK_RESULT_REVIEW_PASS = "review_pass"
TASK_RESULT_REVIEW_FAIL = "review_fail"


@dataclass(frozen=True)
class TaskCompletionResult:
    status: str
    summary: str
    artifacts: dict[str, str]
    artifact_hashes: dict[str, str]


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _resolve_path(path: str | Path | None) -> Path | None:
    if path is None:
        return None
    text = str(path).strip()
    if not text:
        return None
    return Path(text).expanduser().resolve()


def _read_json_object(path: str | Path) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON 文件必须是对象: {path}")
    return payload


def _write_json_atomic(path: str | Path, payload: dict[str, Any]) -> Path:
    target = Path(path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target.with_suffix(target.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(target)
    return target


def _build_prefixed_sha256(path: str | Path) -> str:
    target = Path(path).expanduser().resolve()
    digest = hashlib.sha256()
    with target.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _read_text(path: str | Path | None) -> str:
    target = _resolve_path(path)
    if target is None or (not target.exists()) or (not target.is_file()):
        return ""
    return target.read_text(encoding="utf-8").strip()


def _is_non_empty_file(path: str | Path | None) -> bool:
    target = _resolve_path(path)
    return bool(target and target.exists() and target.is_file() and target.read_text(encoding="utf-8").strip())


def _normalize_artifacts(raw: Any) -> dict[str, str]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("manifest.required_artifacts / optional_artifacts 必须是对象")
    artifacts: dict[str, str] = {}
    for key, value in raw.items():
        alias = str(key).strip()
        path_text = str(value or "").strip()
        if alias and path_text:
            artifacts[alias] = str(Path(path_text).expanduser().resolve())
    return artifacts


def _collect_result_artifacts(
        *,
        required_artifacts: dict[str, str],
        optional_artifacts: dict[str, str],
) -> tuple[dict[str, str], dict[str, str]]:
    artifacts: dict[str, str] = {}
    artifact_hashes: dict[str, str] = {}
    for alias, path_text in {**required_artifacts, **optional_artifacts}.items():
        target = _resolve_path(path_text)
        if target is None or (not target.exists()) or (not target.is_file()):
            continue
        if not target.read_text(encoding="utf-8").strip():
            continue
        resolved = str(target.resolve())
        artifacts[alias] = resolved
        artifact_hashes[resolved] = _build_prefixed_sha256(target)
    return artifacts, artifact_hashes


def write_task_status(path: str | Path, *, status: str) -> Path:
    return _write_json_atomic(path, {"status": status})


def _load_manifest(path: str | Path) -> dict[str, Any]:
    manifest = _read_json_object(path)
    if str(manifest.get("schema_version", "")).strip() != TASK_MANIFEST_SCHEMA_VERSION:
        raise ValueError("manifest.json schema_version 非法")
    for field in ("turn_id", "phase", "task_kind", "mode", "task_status_path", "result_path"):
        if not str(manifest.get(field, "")).strip():
            raise ValueError(f"manifest.json 缺少字段: {field}")
    manifest["task_status_path"] = str(Path(str(manifest["task_status_path"])).expanduser().resolve())
    manifest["result_path"] = str(Path(str(manifest["result_path"])).expanduser().resolve())
    manifest["turn_status_path"] = str(_resolve_path(manifest.get("turn_status_path")) or "")
    manifest["stage_status_path"] = str(_resolve_path(manifest.get("stage_status_path")) or "")
    manifest["required_artifacts"] = _normalize_artifacts(manifest.get("required_artifacts"))
    manifest["optional_artifacts"] = _normalize_artifacts(manifest.get("optional_artifacts"))
    if not isinstance(manifest.get("artifact_rules", {}), dict):
        raise ValueError("manifest.json artifact_rules 必须是对象")
    if not isinstance(manifest.get("retry_policy", {}), dict):
        raise ValueError("manifest.json retry_policy 必须是对象")
    if not isinstance(manifest.get("resume_policy", {}), dict):
        raise ValueError("manifest.json resume_policy 必须是对象")
    return manifest


def _build_result_payload(manifest: dict[str, Any], result: TaskCompletionResult) -> dict[str, Any]:
    return {
        "schema_version": TASK_RESULT_SCHEMA_VERSION,
        "turn_id": str(manifest["turn_id"]),
        "phase": str(manifest["phase"]),
        "task_kind": str(manifest["task_kind"]),
        "status": result.status,
        "summary": result.summary,
        "artifacts": result.artifacts,
        "artifact_hashes": result.artifact_hashes,
        "written_at": _now_iso(),
    }


def _resolve_mode_result(manifest: dict[str, Any]) -> TaskCompletionResult:
    mode = str(manifest["mode"]).strip()
    required_artifacts = dict(manifest["required_artifacts"])
    optional_artifacts = dict(manifest["optional_artifacts"])
    all_artifacts = {**required_artifacts, **optional_artifacts}
    artifacts, artifact_hashes = _collect_result_artifacts(
        required_artifacts=required_artifacts,
        optional_artifacts=optional_artifacts,
    )

    if mode == "a03_ba_resume":
        return TaskCompletionResult(
            status=TASK_RESULT_READY,
            summary="需求分析师已进入需求评审准备态",
            artifacts=artifacts,
            artifact_hashes=artifact_hashes,
        )

    if mode == "a03_reviewer_round":
        review_json = _resolve_path(all_artifacts.get("review_json"))
        review_md = _resolve_path(all_artifacts.get("review_md"))
        if review_json is None or review_md is None:
            raise ValueError("a03_reviewer_round 缺少 review_json/review_md")
        payload = json.loads(review_json.read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            raise ValueError("审核 JSON 必须是 list")
        matched_item: dict[str, Any] | None = None
        for item in payload:
            if isinstance(item, dict) and str(item.get("task_name", "")).strip() == "需求评审":
                matched_item = item
                break
        if matched_item is None:
            raise ValueError("审核 JSON 缺少 需求评审 状态项")
        review_pass = matched_item.get("review_pass")
        if not isinstance(review_pass, bool):
            raise ValueError("review_pass 必须是 bool")
        review_md_empty = not review_md.read_text(encoding="utf-8").strip()
        if review_pass and not review_md_empty:
            raise ValueError("审核通过时 reviewer md 必须为空")
        if (not review_pass) and review_md_empty:
            raise ValueError("审核未通过时 reviewer md 必须非空")
        status = TASK_RESULT_REVIEW_PASS if review_pass else TASK_RESULT_REVIEW_FAIL
        summary = "审核器已完成需求评审" if review_pass else "审核器已写入未通过评审意见"
        return TaskCompletionResult(
            status=status,
            summary=summary,
            artifacts=artifacts,
            artifact_hashes=artifact_hashes,
        )

    if mode == "a03_ba_feedback":
        ask_human_text = _read_text(all_artifacts.get("ask_human"))
        ba_feedback_text = _read_text(all_artifacts.get("ba_feedback"))
        if ask_human_text:
            return TaskCompletionResult(
                status=TASK_RESULT_HITL,
                summary="需求分析师需要继续向人类澄清",
                artifacts=artifacts,
                artifact_hashes=artifact_hashes,
            )
        if ba_feedback_text:
            return TaskCompletionResult(
                status=TASK_RESULT_COMPLETED,
                summary="需求分析师已完成评审反馈修订",
                artifacts=artifacts,
                artifact_hashes=artifact_hashes,
            )
        raise ValueError("需求分析师反馈未生成 HITL 问题或修改反馈")

    if mode == "a03_human_feedback":
        ask_human_text = _read_text(all_artifacts.get("ask_human"))
        if ask_human_text:
            return TaskCompletionResult(
                status=TASK_RESULT_COMPLETED,
                summary="需求分析师已完成对人类反馈的处理",
                artifacts=artifacts,
                artifact_hashes=artifact_hashes,
            )
        raise ValueError("需求分析师未写入《与人类交流.md》")

    if mode in {"a02_requirements_analysis", "a03_requirements_clarification"}:
        requirements_clear_text = _read_text(all_artifacts.get("requirements_clear"))
        ask_human_text = _read_text(all_artifacts.get("ask_human"))
        hitl_record_text = _read_text(all_artifacts.get("hitl_record"))
        if requirements_clear_text:
            return TaskCompletionResult(
                status=TASK_RESULT_COMPLETED,
                summary="需求澄清已完成",
                artifacts=artifacts,
                artifact_hashes=artifact_hashes,
            )
        if ask_human_text and hitl_record_text:
            return TaskCompletionResult(
                status=TASK_RESULT_HITL,
                summary="需求澄清需要继续 HITL",
                artifacts=artifacts,
                artifact_hashes=artifact_hashes,
            )
        raise ValueError("需求澄清未生成有效结果文件")

    if mode in {"a02_notion_intake", "a02_requirement_intake"}:
        original_requirement_text = _read_text(all_artifacts.get("original_requirement"))
        ask_human_text = _read_text(all_artifacts.get("ask_human"))
        hitl_record_text = _read_text(all_artifacts.get("hitl_record"))
        if original_requirement_text:
            return TaskCompletionResult(
                status=TASK_RESULT_COMPLETED,
                summary="Notion 需求录入完成",
                artifacts=artifacts,
                artifact_hashes=artifact_hashes,
            )
        if ask_human_text and hitl_record_text:
            return TaskCompletionResult(
                status=TASK_RESULT_HITL,
                summary="Notion 需求录入需要继续 HITL",
                artifacts=artifacts,
                artifact_hashes=artifact_hashes,
            )
        if ask_human_text:
            return TaskCompletionResult(
                status=TASK_RESULT_ERROR,
                summary="Notion 需求录入失败",
                artifacts=artifacts,
                artifact_hashes=artifact_hashes,
            )
        raise ValueError("Notion 需求录入未生成有效结果文件")

    raise ValueError(f"不支持的 manifest mode: {mode}")


def complete_task(
        *,
        task_status_path: str | Path | None = None,
        status: str = TASK_STATUS_DONE,
        done_marker: str = TASK_DONE_MARKER,
        stream=None,
        manifest_path: str | Path | None = None,
) -> Path:
    _ = done_marker
    _ = stream
    if manifest_path is not None:
        if status != TASK_STATUS_DONE:
            raise ValueError("manifest 模式只允许将 task_status 标记为 done")
        manifest = _load_manifest(manifest_path)
        result = _resolve_mode_result(manifest)
        _write_json_atomic(manifest["result_path"], _build_result_payload(manifest, result))
        return write_task_status(manifest["task_status_path"], status=TASK_STATUS_DONE)
    if task_status_path is None:
        raise ValueError("task_status_path 和 manifest_path 不能同时为空")
    return write_task_status(task_status_path, status=status)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Mark tmux agent task completion")
    parser.add_argument("--task-status-path")
    parser.add_argument("--manifest-path")
    parser.add_argument("--status", default=TASK_STATUS_DONE)
    parser.add_argument("--done-marker", default=TASK_DONE_MARKER)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if not args.manifest_path and not args.task_status_path:
        raise SystemExit("必须提供 --manifest-path 或 --task-status-path")
    complete_task(
        task_status_path=args.task_status_path,
        status=args.status,
        done_marker=args.done_marker,
        manifest_path=args.manifest_path,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
