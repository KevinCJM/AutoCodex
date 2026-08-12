from __future__ import annotations

import json
import inspect
import time
import warnings
from contextlib import nullcontext, suppress
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from tmux_core.runtime.vendor_catalog import get_default_model_for_vendor
from tmux_core.runtime.ponytail import PonytailMode, normalize_ponytail_mode
from tmux_core.runtime.grill import RequirementsMode, normalize_requirements_mode
from tmux_core.runtime.codegraph import (
    CodeGraphMode,
    enable_codegraph_interactive_recovery,
    normalize_codegraph_mode,
    read_codegraph_project_preference,
)
from A01_Routing_LayerPlanning import (
    DEFAULT_MODEL_BY_VENDOR,
    normalize_effort_choice,
    normalize_model_choice,
    normalize_vendor_choice,
    prompt_effort,
    prompt_model,
    prompt_ponytail_mode,
    prompt_vendor,
)
from tmux_core.runtime.contracts import TASK_STATUS_DONE, TurnFileContract, validate_turn_file_artifact_rules, write_task_status
from tmux_core.runtime.tmux_runtime import (
    AgentRunConfig,
    CommandResult,
    TmuxBatchWorker,
    Vendor,
    WorkerStatus,
    normalize_codegraph_config,
    is_agent_ready_timeout_error,
    is_agent_startup_intervention_error,
    is_provider_auth_error,
    is_provider_runtime_error,
    is_worker_death_error,
)
from tmux_core.stage_kernel.agent_intervention import (
    AGENT_INTERVENTION_RECHECK,
    AGENT_INTERVENTION_WORKER_DEAD,
    request_worker_manual_intervention,
)
from T09_terminal_ops import (
    PROMPT_BACK_VALUE,
    PromptBackRequested,
    SingleLineSpinnerMonitor,
    TERMINAL_SPINNER_FRAMES,
    collect_multiline_input,
    message,
    prompt_metadata,
    prompt_select_option,
    prompt_positive_int as terminal_prompt_positive_int,
    prompt_with_default,
    prompt_yes_no as terminal_prompt_yes_no,
)
from T12_requirements_common import (
    DEFAULT_REQUIREMENTS_CLARIFICATION_EFFORT,
    DEFAULT_REQUIREMENTS_CLARIFICATION_VENDOR,
    DEFAULT_REQUIREMENTS_CLARIFICATION_MODEL,
    stdin_is_interactive,
)

DEFAULT_REVIEWER_COUNT = 1
MAX_REVIEWER_REPAIR_ATTEMPTS = 2
DEFAULT_STAGE_REVIEW_MAX_ROUNDS = 5
REVIEWER_CONSECUTIVE_FAILURE_RECONFIG_THRESHOLD = 2
AGENT_READY_TIMEOUT_RETRY = AGENT_INTERVENTION_RECHECK
AGENT_READY_TIMEOUT_SKIP = AGENT_INTERVENTION_WORKER_DEAD


@dataclass(frozen=True)
class ReviewAgentSelection:
    vendor: str
    model: str
    reasoning_effort: str
    proxy_url: str
    ponytail_mode: str = PonytailMode.OFF.value
    codegraph_mode: str = CodeGraphMode.OFF.value
    codegraph_config: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class InvalidAgentSelection:
    name: str
    source: str
    raw_spec: object
    error: str


@dataclass(frozen=True)
class ReviewerRuntime:
    reviewer_name: str
    selection: ReviewAgentSelection
    worker: TmuxBatchWorker
    review_md_path: Path
    review_json_path: Path
    contract: TurnFileContract
    failure_streak: int = 0
    last_failure_reason: str = ""


@dataclass(frozen=True)
class ReviewAgentHandoff:
    reviewer_key: str
    role_name: str
    role_prompt: str
    selection: ReviewAgentSelection
    worker: TmuxBatchWorker


@dataclass(frozen=True)
class StageAgentConfig:
    main: ReviewAgentSelection | None = None
    reviewers: dict[str, ReviewAgentSelection] | None = None
    reviewer_order: tuple[str, ...] = ()
    invalid_main: InvalidAgentSelection | None = None
    invalid_reviewers: dict[str, InvalidAgentSelection] = field(default_factory=dict)
    ponytail_mode: str = PonytailMode.OFF.value
    codegraph_mode: str = CodeGraphMode.OFF.value
    codegraph_config: dict[str, object] = field(default_factory=dict)

    def reviewer_selection(self, reviewer_key: str) -> ReviewAgentSelection | None:
        selections = self.reviewers or {}
        return selections.get(str(reviewer_key or "").strip())


@dataclass
class ReviewRoundPolicy:
    max_rounds: int | None
    quota_count: int = 0
    initial_review_done: bool = False

    def record_review_attempt(self) -> None:
        self.quota_count += 1
        self.initial_review_done = True

    def should_escalate_before_next_review(self) -> bool:
        return self.max_rounds is not None and self.quota_count >= self.max_rounds

    def reset_after_hitl(self) -> None:
        self.quota_count = 0


def refresh_codegraph_workers_for_checkpoint(
    workers: Sequence[object],
    *,
    prompt: str,
    turn_context: object | None = None,
) -> object | None:
    """Refresh one shared project graph, then let peers reuse that generation."""

    eligible = [
        worker
        for worker in workers
        if callable(getattr(worker, "refresh_codegraph_generation", None))
        and str(getattr(worker, "codegraph_mode", CodeGraphMode.OFF.value) or "").strip()
        != CodeGraphMode.OFF.value
    ]
    if not eligible:
        return None
    refresh = eligible[0].refresh_codegraph_generation
    try:
        parameters = inspect.signature(refresh).parameters.values()
    except (TypeError, ValueError):
        parameters = ()
    if turn_context is not None and any(
        parameter.name == "turn_context" or parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in parameters
    ):
        profile = refresh(prompt, turn_context=turn_context)
    else:
        profile = refresh(prompt)
    for worker in eligible[1:]:
        marker = getattr(worker, "mark_codegraph_generation_current", None)
        if callable(marker):
            try:
                marker_parameters = inspect.signature(marker).parameters.values()
            except (TypeError, ValueError):
                marker_parameters = ()
            if turn_context is not None and any(
                parameter.name == "turn_context"
                or parameter.kind == inspect.Parameter.VAR_KEYWORD
                for parameter in marker_parameters
            ):
                marker(turn_context=turn_context)
            else:
                marker()
    return profile


@dataclass(frozen=True)
class ReviewLimitHitlConfig:
    stage_label: str
    artifact_label: str
    primary_output_path: str | Path
    ask_human_path: str | Path
    hitl_record_path: str | Path
    merged_review_path: str | Path
    output_summary_path: str | Path
    continue_output_label: str


@dataclass(frozen=True)
class ReviewLimitHitlResult:
    owner: object
    rounds_used: int
    post_hitl_continue_completed: bool = False


def _review_worker_state(worker: TmuxBatchWorker | None) -> dict[str, object]:
    if worker is None:
        return {}
    reader = getattr(worker, "read_state", None)
    if not callable(reader):
        return {}
    try:
        state = reader()
    except Exception:
        return {}
    return dict(state) if isinstance(state, Mapping) else {}


def reviewer_outputs_satisfy_contract(reviewer: ReviewerRuntime) -> bool:
    try:
        result = reviewer.contract.validator(reviewer.contract.status_path)
        validate_turn_file_artifact_rules(reviewer.contract, result)
        return True
    except Exception:
        return False


def reviewer_artifact_signature(reviewer: ReviewerRuntime) -> tuple[object, ...]:
    signatures: list[object] = []
    for path in (reviewer.review_md_path, reviewer.review_json_path):
        resolved = Path(path).expanduser().resolve()
        if not resolved.exists():
            signatures.append(("missing", str(resolved)))
            continue
        stat = resolved.stat()
        signatures.append((str(resolved), stat.st_size, stat.st_mtime_ns))
    return tuple(signatures)


def resolve_reviewer_artifact_agent_name(reviewer: ReviewerRuntime | object) -> str:
    """Return the immutable reviewer suffix encoded in the bound JSON path."""
    review_json_path = getattr(reviewer, "review_json_path", None)
    if review_json_path is not None and str(review_json_path).strip():
        stem = Path(review_json_path).expanduser().stem
        for marker in ("_整体复核记录_", "_评审记录_"):
            if marker not in stem:
                continue
            artifact_name = stem.rsplit(marker, 1)[-1].strip()
            if artifact_name:
                return artifact_name
    worker = getattr(reviewer, "worker", None)
    session_name = str(getattr(worker, "session_name", "") or "").strip()
    reviewer_name = str(getattr(reviewer, "reviewer_name", "") or "").strip()
    return session_name or reviewer_name


def reviewer_worker_needs_terminal_success_normalization(reviewer: ReviewerRuntime) -> bool:
    state = _review_worker_state(reviewer.worker)
    if state:
        status = str(state.get("status", "") or "").strip().lower()
        result_status = str(state.get("result_status", "") or "").strip().lower()
        runtime_status = str(state.get("current_task_runtime_status", "") or "").strip().lower()
        agent_state = str(state.get("agent_state", "") or "").strip().upper()
        return not (
            status == "succeeded"
            and result_status == "succeeded"
            and runtime_status == TASK_STATUS_DONE
            and agent_state == "READY"
        )
    return True


def mark_reviewer_turn_succeeded_from_materialized_outputs(
        reviewer: ReviewerRuntime,
        *,
        label: str,
        task_name: str,
) -> None:
    if not reviewer_worker_needs_terminal_success_normalization(reviewer):
        return
    worker = reviewer.worker
    state = _review_worker_state(worker)
    task_status_path_text = str(state.get("current_task_status_path", "") or "").strip()
    if task_status_path_text:
        with suppress(Exception):
            write_task_status(Path(task_status_path_text).expanduser().resolve(), status=TASK_STATUS_DONE)
    extra = {
        "label": label,
        "result_status": "succeeded",
        "current_turn_id": str(state.get("current_turn_id", "") or reviewer.contract.turn_id),
        "current_turn_phase": str(state.get("current_turn_phase", "") or task_name),
        "current_turn_status_path": str(reviewer.review_json_path),
        "current_task_status_path": task_status_path_text,
        "current_task_runtime_status": TASK_STATUS_DONE,
        "agent_started": True,
        "agent_ready": True,
        "agent_state": "READY",
        "health_status": "alive",
        "health_note": "alive",
    }
    record_result = getattr(worker, "_record_result", None)
    if callable(record_result):
        timestamp = time.strftime("%Y-%m-%dT%H:%M:%S")
        record_result(
            CommandResult(
                label=label,
                command="",
                exit_code=0,
                raw_output=f"review artifacts materialized for {task_name}",
                clean_output=f"review artifacts materialized for {task_name}",
                started_at=timestamp,
                finished_at=timestamp,
            ),
            status=WorkerStatus.SUCCEEDED,
            note=f"done:{label}",
            extra=extra,
        )
        return
    write_state = getattr(worker, "_write_state", None)
    if callable(write_state):
        with suppress(Exception):
            write_state(WorkerStatus.SUCCEEDED, note=f"done:{label}", extra=extra)


AGENT_CONFIG_ERROR_MARKERS = (
    "不支持的推理强度",
    "不支持的厂商",
    "不支持的模型",
    "模型不能为空",
    "model 不能为空",
    "未安装",
    "没有可用模型",
    "无法选择模型",
    "unsupported vendor",
    "unsupported reasoning effort",
    "does not support normalized effort",
    "model unavailable",
    "model cannot be empty",
    "model is required",
    "not installed",
    "no available model",
    "scanned catalog",
    "vendor catalog",
)


def is_agent_config_error(error: Exception | BaseException | str) -> bool:
    text = str(error or "").strip()
    if not text:
        return False
    lowered = text.lower()
    return any(marker in text or marker in lowered for marker in AGENT_CONFIG_ERROR_MARKERS)


def describe_reviewer_failure_reason(
    error: Exception | BaseException | str,
    worker: TmuxBatchWorker | None = None,
) -> str:
    message_text = str(error or "").strip()
    lowered = message_text.lower()
    state = _review_worker_state(worker)
    health_status = str(state.get("health_status", "")).strip().lower()
    if is_agent_config_error(error):
        first_line = message_text.splitlines()[0].strip() if message_text else ""
        return first_line[:160] if first_line else "智能体模型配置不可用"
    if is_provider_auth_error(error) or worker_has_provider_auth_error(worker):
        return "模型认证已失效"
    if is_provider_runtime_error(error) or worker_has_provider_runtime_error(worker):
        return "模型服务出现临时运行错误"
    if is_agent_ready_timeout_error(error) or "shell initialization timed out" in lowered:
        return "启动超时，未能进入可输入状态"
    if "agent exited back to shell" in lowered:
        return "agent 进程已退出并返回 shell"
    if is_worker_death_error(error):
        if health_status == "missing_session":
            return "tmux 会话已丢失"
        if health_status == "pane_dead":
            return "tmux pane 已退出"
        if not getattr(worker, "session_exists", lambda: True)():
            return "tmux 会话已丢失"
        return "智能体进程已死亡或退出"
    first_line = message_text.splitlines()[0].strip() if message_text else ""
    return first_line[:160] if first_line else "未知原因"


def note_reviewer_failure(
    reviewer: ReviewerRuntime,
    *,
    reason_text: str,
) -> ReviewerRuntime:
    next_streak = max(int(getattr(reviewer, "failure_streak", 0) or 0), 0) + 1
    return replace(
        reviewer,
        failure_streak=next_streak,
        last_failure_reason=str(reason_text or "").strip(),
    )


def carry_reviewer_failure_state(
    reviewer: ReviewerRuntime,
    *,
    previous: ReviewerRuntime,
) -> ReviewerRuntime:
    return replace(
        reviewer,
        failure_streak=max(int(getattr(previous, "failure_streak", 0) or 0), 0),
        last_failure_reason=str(getattr(previous, "last_failure_reason", "") or "").strip(),
    )


def reviewer_requires_manual_model_reconfiguration(reviewer: ReviewerRuntime) -> bool:
    return int(getattr(reviewer, "failure_streak", 0) or 0) >= REVIEWER_CONSECUTIVE_FAILURE_RECONFIG_THRESHOLD


def build_reviewer_failure_reconfiguration_reason(
    reviewer: ReviewerRuntime,
    *,
    role_label: str,
    failure_reason: str,
) -> str:
    streak = max(int(getattr(reviewer, "failure_streak", 0) or 0), 0)
    return (
        f"检测到{role_label}连续 {streak} 次死亡/失败。\n"
        f"最近一次原因：{str(failure_reason or '').strip() or '未知原因'}\n"
        "需要重新选择模型后继续当前阶段。"
    )


class ReviewStageProgress:
    def __init__(self, *, initial_phase: str = "评审准备中") -> None:
        self._phase = initial_phase
        self._active = False
        self._monitor = SingleLineSpinnerMonitor(
            frame_builder=self._render_line,
            interval_sec=0.2,
        )

    def _render_line(self, tick: int) -> str:
        spinner = TERMINAL_SPINNER_FRAMES[tick % len(TERMINAL_SPINNER_FRAMES)]
        return f"{spinner} {self._phase}"

    def set_phase(self, phase: str, *, start: bool = True) -> None:
        self._phase = str(phase).strip() or "评审中"
        if start:
            self.start()

    def start(self) -> None:
        if self._active:
            return
        self._monitor.start()
        self._active = True

    def stop(self) -> None:
        if not self._active:
            return
        self._monitor.stop()
        self._active = False

    def suspended(self):
        was_active = self._active
        self.stop()
        if not was_active:
            return nullcontext()

        progress = self

        class _ResumeContext:
            def __enter__(self):
                return None

            def __exit__(self, exc_type, exc, tb):
                progress.start()
                return False

        return _ResumeContext()


_ACTIVE_REVIEW_PROGRESS: ReviewStageProgress | None = None


def resolve_review_progress(progress: ReviewStageProgress | None = None) -> ReviewStageProgress | None:
    return progress if progress is not None else _ACTIVE_REVIEW_PROGRESS


def prompt_proxy_url(default: str = "", *, role_label: str = "") -> str:
    role_text = str(role_label or "").strip()
    prompt_text = "输入代理端口或完整代理 URL（可留空）"
    if role_text:
        prompt_text = f"为 {role_text} {prompt_text}"
    return prompt_with_default(prompt_text, default, allow_empty=True)


def prompt_positive_int(
    prompt_text: str,
    default: int = 1,
    *,
    progress: ReviewStageProgress | None = None,
    allow_back: bool = False,
    stage_key: str = "",
    stage_step_index: int = 0,
) -> int:
    progress = resolve_review_progress(progress)
    with progress.suspended() if progress is not None else nullcontext():
        with prompt_metadata(
            allow_back=allow_back,
            back_value=PROMPT_BACK_VALUE,
            stage_key=stage_key,
            stage_step_index=stage_step_index,
        ):
            return terminal_prompt_positive_int(prompt_text, default)


def _parse_spec_text(value: str, *, source: str) -> dict[str, str]:
    text = str(value or "").strip()
    if not text:
        return {}
    fields: dict[str, str] = {}
    if "=" not in text and ":" in text:
        parts = [part.strip() for part in text.split(":")]
        names = ("vendor", "model", "effort", "proxy")
        for index, part in enumerate(parts[: len(names)]):
            if part:
                fields[names[index]] = part
        return fields
    for chunk in text.split(","):
        item = chunk.strip()
        if not item:
            continue
        if "=" not in item:
            raise RuntimeError(f"{source} 配置项必须是 key=value: {item}")
        key, raw = item.split("=", 1)
        key_text = key.strip().replace("-", "_").lower()
        if not key_text:
            raise RuntimeError(f"{source} 存在空 key: {item}")
        fields[key_text] = raw.strip()
    return fields


def _coerce_agent_spec_fields(spec: object, *, source: str) -> dict[str, str]:
    if spec is None:
        return {}
    if isinstance(spec, str):
        return _parse_spec_text(spec, source=source)
    if isinstance(spec, Mapping):
        return {
            str(key).strip().replace("-", "_").lower(): str(value).strip()
            for key, value in spec.items()
            if str(key).strip() and value is not None and str(value).strip()
        }
    raise RuntimeError(f"{source} 必须是字符串或对象")


def parse_agent_selection_spec(
    spec: object,
    *,
    default_name: str = "",
    default_vendor: str = DEFAULT_REQUIREMENTS_CLARIFICATION_VENDOR,
    default_model: str = "",
    default_reasoning_effort: str = DEFAULT_REQUIREMENTS_CLARIFICATION_EFFORT,
    default_ponytail_mode: str = PonytailMode.OFF.value,
    default_codegraph_mode: str = CodeGraphMode.OFF.value,
    default_codegraph_config: Mapping[str, object] | None = None,
    source: str = "agent",
) -> tuple[str, ReviewAgentSelection]:
    fields = _coerce_agent_spec_fields(spec, source=source)
    if "codegraph_mode" in fields or "codegraph_config" in fields or "codegraph" in fields:
        raise RuntimeError(
            f"{source} 不支持角色级 codegraph_mode；请在顶层或 stages.<stage> 配置。"
        )
    raw_vendor = fields.get("vendor") or default_vendor
    vendor = normalize_vendor_choice(raw_vendor)
    model_default = default_model if default_model and vendor == default_vendor else get_default_model_for_vendor(vendor)
    model = normalize_model_choice(vendor, fields.get("model") or model_default)
    effort = normalize_effort_choice(
        vendor,
        model,
        fields.get("effort") or fields.get("reasoning_effort") or default_reasoning_effort,
    )
    proxy_url = (
        fields.get("proxy_url")
        or fields.get("proxy")
        or fields.get("proxy_port")
        or fields.get("port")
        or ""
    )
    ponytail_mode = normalize_ponytail_mode(
        fields.get("ponytail_mode") or fields.get("ponytail") or default_ponytail_mode,
        default=PonytailMode.OFF,
    ).value
    codegraph_mode = normalize_codegraph_mode(
        default_codegraph_mode,
        default=CodeGraphMode.OFF,
    ).value
    name = (
        fields.get("name")
        or fields.get("key")
        or fields.get("role")
        or fields.get("reviewer")
        or default_name
    )
    return (
        str(name or "").strip(),
        ReviewAgentSelection(
            vendor=vendor,
            model=model,
            reasoning_effort=effort,
            proxy_url=str(proxy_url or "").strip(),
            ponytail_mode=ponytail_mode,
            codegraph_mode=codegraph_mode,
            codegraph_config=normalize_codegraph_config(default_codegraph_config),
        ),
    )


def _agent_spec_name(spec: object, *, default_name: str, source: str) -> str:
    try:
        fields = _coerce_agent_spec_fields(spec, source=source)
    except Exception:
        return str(default_name or "").strip()
    return str(
        fields.get("name")
        or fields.get("key")
        or fields.get("role")
        or fields.get("reviewer")
        or default_name
        or ""
    ).strip()


def _load_agent_config_payload(path_value: object) -> dict[str, Any]:
    text = str(path_value or "").strip()
    if not text:
        return {}
    path = Path(text).expanduser().resolve()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as error:  # noqa: BLE001
        raise RuntimeError(f"--agent-config 读取失败: {path}: {error}") from error
    if not isinstance(payload, dict):
        raise RuntimeError(f"--agent-config 根节点必须是 JSON 对象: {path}")
    return payload


def _config_reviewer_specs(payload: Mapping[str, Any]) -> list[object]:
    for key in ("reviewers", "reviewer_agents", "reviewer_agent"):
        value = payload.get(key)
        if value is None:
            continue
        if isinstance(value, list):
            return list(value)
        return [value]
    return []


def _stage_config_payload(payload: Mapping[str, Any], stage_key: str) -> Mapping[str, Any]:
    key = str(stage_key or "").strip()
    if not key:
        return {}
    stages = payload.get("stages")
    if not isinstance(stages, Mapping):
        return {}
    stage_payload = stages.get(key)
    if not isinstance(stage_payload, Mapping):
        return {}
    return stage_payload


def resolve_workflow_ponytail_mode(
    args: object,
    *,
    config_payload: Mapping[str, Any] | None = None,
) -> str:
    cached = str(getattr(args, "_resolved_ponytail_mode", "") or "").strip()
    if cached:
        return normalize_ponytail_mode(cached).value
    payload = dict(config_payload) if config_payload is not None else _load_agent_config_payload(getattr(args, "agent_config", ""))
    explicit = str(getattr(args, "ponytail_mode", "") or "").strip()
    configured = str(payload.get("ponytail_mode") or payload.get("ponytail") or "").strip()
    if explicit or configured:
        mode = normalize_ponytail_mode(explicit or configured).value
    elif bool(getattr(args, "yes", False)) or not stdin_is_interactive():
        mode = PonytailMode.FULL.value
    else:
        mode = prompt_ponytail_mode(PonytailMode.FULL.value)
    try:
        setattr(args, "ponytail_mode", mode)
        setattr(args, "_resolved_ponytail_mode", mode)
    except Exception:
        pass
    return mode


def resolve_workflow_codegraph_mode(
    args: object,
    *,
    config_payload: Mapping[str, Any] | None = None,
    stage_key: str = "",
) -> str:
    """Resolve the project graph policy without mutating cross-stage CLI state.

    A00 resolves every stage from the same ``argparse.Namespace``.  A
    stage-scoped value therefore must be cached per stage instead of being
    written back to ``args.codegraph_mode`` and accidentally becoming the next
    stage's CLI override.
    """

    cache_key = str(stage_key or "__workflow__").strip() or "__workflow__"
    cached_modes = getattr(args, "_resolved_codegraph_modes", None)
    if isinstance(cached_modes, Mapping) and cache_key in cached_modes:
        return normalize_codegraph_mode(cached_modes[cache_key]).value

    payload = (
        dict(config_payload)
        if config_payload is not None
        else _load_agent_config_payload(getattr(args, "agent_config", ""))
    )
    stage_payload = _stage_config_payload(payload, stage_key)
    explicit = str(getattr(args, "codegraph_mode", "") or "").strip()
    legacy_explicit = str(getattr(args, "graphify_mode", "") or "").strip()
    stage_configured = str(stage_payload.get("codegraph_mode", "") or "").strip()
    legacy_stage = str(stage_payload.get("graphify_mode", "") or "").strip()
    root_configured = str(payload.get("codegraph_mode", "") or "").strip()
    legacy_root = str(payload.get("graphify_mode", "") or "").strip()
    legacy_mode = legacy_explicit or legacy_stage or legacy_root
    if legacy_mode:
        warnings.warn(
            "graphify_mode 已废弃，本版暂时映射为 codegraph_mode；请更新 CLI/配置。",
            FutureWarning,
            stacklevel=2,
        )
    project_preference = ""
    project_dir = str(getattr(args, "project_dir", "") or "").strip()
    if project_dir and not any((explicit, stage_configured, root_configured, legacy_mode)):
        project_preference = read_codegraph_project_preference(project_dir)
    mode = normalize_codegraph_mode(
        explicit or stage_configured or root_configured or legacy_mode or project_preference or CodeGraphMode.AUTO.value,
        default=CodeGraphMode.AUTO,
    ).value
    if not bool(getattr(args, "yes", False)) and stdin_is_interactive():
        # Actual tool decisions happen immediately before the first worker
        # launch.  Keeping config resolution side-effect free avoids inserting
        # a surprise prompt into requirement/model selection flows that may not
        # create any coding agent at all.
        if project_dir:
            enable_codegraph_interactive_recovery(project_dir)
    next_cache = dict(cached_modes) if isinstance(cached_modes, Mapping) else {}
    next_cache[cache_key] = mode
    with suppress(Exception):
        setattr(args, "_resolved_codegraph_modes", next_cache)
    return mode


def configured_workflow_codegraph_mode(
    args: object,
    *,
    config_payload: Mapping[str, Any] | None = None,
    stage_key: str = "",
) -> str:
    """Return only an explicit CLI/config graph policy, without defaulting.

    A00 uses this before the interactive project directory exists.  Returning
    an empty value lets A01 resolve the selected project's persisted preference
    instead of turning the workflow default into an accidental CLI override.
    """

    payload = (
        dict(config_payload)
        if config_payload is not None
        else _load_agent_config_payload(getattr(args, "agent_config", ""))
    )
    stage_payload = _stage_config_payload(payload, stage_key)
    configured = str(
        getattr(args, "codegraph_mode", "")
        or getattr(args, "graphify_mode", "")
        or stage_payload.get("codegraph_mode", "")
        or stage_payload.get("graphify_mode", "")
        or payload.get("codegraph_mode", "")
        or payload.get("graphify_mode", "")
        or ""
    ).strip()
    if not configured:
        return ""
    return normalize_codegraph_mode(configured, default=CodeGraphMode.AUTO).value


def resolve_workflow_codegraph_config(
    args: object,
    *,
    config_payload: Mapping[str, Any] | None = None,
) -> dict[str, object]:
    payload = (
        dict(config_payload)
        if config_payload is not None
        else _load_agent_config_payload(getattr(args, "agent_config", ""))
    )
    return normalize_codegraph_config(
        payload.get("codegraph", {}) if isinstance(payload.get("codegraph", {}), Mapping) else payload.get("codegraph")
    )


def prompt_requirements_mode(default: str = RequirementsMode.STANDARD.value) -> str:
    normalized_default = normalize_requirements_mode(default).value
    candidate = prompt_select_option(
        title="选择需求澄清模式",
        options=(
            (RequirementsMode.STANDARD.value, "Standard（默认）— 使用现有需求澄清流程"),
            (RequirementsMode.GRILL.value, "Grill Me — 一次只确认一个业务决策"),
            (
                RequirementsMode.GRILL_WITH_DOCS.value,
                "Grill with Docs — 逐问澄清并维护领域词汇表/ADR 草稿",
            ),
        ),
        default_value=normalized_default,
        prompt_text="选择需求澄清模式",
    )
    return normalize_requirements_mode(candidate).value


def configured_workflow_requirements_mode(
    args: object,
    *,
    config_payload: Mapping[str, Any] | None = None,
    stage_key: str = "requirements_clarification",
) -> str:
    """Return an explicitly configured mode without prompting or applying defaults.

    The A00 workflow cannot safely ask this question until the project and
    requirement scope are known: an unfinished Grill session in that scope is
    authoritative and must be resumed without presenting a mode switch.  This
    helper preserves the public precedence rules while allowing A03 to perform
    the scope-aware interactive/default resolution.
    """

    payload = (
        dict(config_payload)
        if config_payload is not None
        else _load_agent_config_payload(getattr(args, "agent_config", ""))
    )
    stage_payload = _stage_config_payload(payload, stage_key)
    explicit = str(getattr(args, "requirements_mode", "") or "").strip()
    stage_configured = str(stage_payload.get("requirements_mode", "") or "").strip()
    root_configured = str(payload.get("requirements_mode", "") or "").strip()
    configured = explicit or stage_configured or root_configured
    if not configured:
        return ""
    return normalize_requirements_mode(configured).value


def resolve_workflow_requirements_mode(
    args: object,
    *,
    config_payload: Mapping[str, Any] | None = None,
    stage_key: str = "requirements_clarification",
) -> str:
    cached = str(getattr(args, "_resolved_requirements_mode", "") or "").strip()
    if cached:
        return normalize_requirements_mode(cached).value
    configured = configured_workflow_requirements_mode(
        args,
        config_payload=config_payload,
        stage_key=stage_key,
    )
    if configured:
        mode = normalize_requirements_mode(configured).value
        if mode != RequirementsMode.STANDARD.value and (
            bool(getattr(args, "yes", False)) or not stdin_is_interactive()
        ):
            raise RuntimeError(
                "Grill 需求澄清必须由人类逐题确认；--yes 或非交互模式仅支持 requirements_mode=standard。"
            )
    elif bool(getattr(args, "yes", False)) or not stdin_is_interactive():
        mode = RequirementsMode.STANDARD.value
    else:
        mode = prompt_requirements_mode(RequirementsMode.STANDARD.value)
    try:
        setattr(args, "requirements_mode", mode)
        setattr(args, "_resolved_requirements_mode", mode)
    except Exception:
        pass
    return mode


def inherit_legacy_handoff_ponytail_mode(args: object, mode: object) -> None:
    """Preserve an explicit legacy handoff when no new workflow policy was supplied."""
    if any(
        str(getattr(args, key, "") or "").strip()
        for key in ("ponytail_mode", "main_ponytail_mode", "agent_config")
    ):
        return
    inherited = normalize_ponytail_mode(mode or PonytailMode.OFF.value).value
    with suppress(Exception):
        setattr(args, "ponytail_mode", inherited)
        setattr(args, "_resolved_ponytail_mode", inherited)


def inherit_legacy_handoff_codegraph_mode(args: object, mode: object) -> None:
    """Keep an active legacy handoff on its persisted graph policy.

    A00 always passes an explicit mode for a new workflow.  A direct stage
    invocation that only carries an existing handoff must instead treat the
    handoff as the runner's frozen policy; otherwise the new-workflow ``auto``
    default would silently switch an old ``off`` session mid-run.
    """

    if any(
        str(getattr(args, key, "") or "").strip()
        for key in ("codegraph_mode", "agent_config")
    ):
        return
    inherited = normalize_codegraph_mode(
        mode or CodeGraphMode.OFF.value,
        default=CodeGraphMode.OFF,
    ).value
    with suppress(Exception):
        setattr(args, "codegraph_mode", inherited)
        setattr(args, "_resolved_codegraph_modes", {})


def resolve_main_ponytail_mode(
    args: object,
    *,
    agent_config: StageAgentConfig | None = None,
    stage_key: str = "",
) -> str:
    explicit = str(getattr(args, "main_ponytail_mode", "") or "").strip()
    if explicit:
        return normalize_ponytail_mode(explicit).value
    config = agent_config or resolve_stage_agent_config(args, stage_key=stage_key)
    if config.main is not None:
        return normalize_ponytail_mode(config.main.ponytail_mode, default=PonytailMode.FULL).value
    return normalize_ponytail_mode(config.ponytail_mode, default=PonytailMode.FULL).value


def resolve_stage_agent_config(
    args: object,
    *,
    stage_key: str = "",
    default_reviewer_names: Sequence[str] = (),
) -> StageAgentConfig:
    config_payload = _load_agent_config_payload(getattr(args, "agent_config", ""))
    ponytail_mode = resolve_workflow_ponytail_mode(args, config_payload=config_payload)
    codegraph_mode = resolve_workflow_codegraph_mode(
        args,
        config_payload=config_payload,
        stage_key=stage_key,
    )
    codegraph_config = resolve_workflow_codegraph_config(args, config_payload=config_payload)
    stage_payload = _stage_config_payload(config_payload, stage_key)
    main_spec = stage_payload.get("main") or stage_payload.get("main_agent") or config_payload.get("main") or config_payload.get("main_agent")
    cli_main = getattr(args, "main_agent", "")
    if str(cli_main or "").strip():
        main_spec = cli_main
    main_selection: ReviewAgentSelection | None = None
    invalid_main: InvalidAgentSelection | None = None
    if main_spec:
        try:
            _, main_selection = parse_agent_selection_spec(
                main_spec,
                default_ponytail_mode=ponytail_mode,
                default_codegraph_mode=codegraph_mode,
                default_codegraph_config=codegraph_config,
                source="main-agent",
            )
        except Exception as error:  # noqa: BLE001
            invalid_main = InvalidAgentSelection(
                name=_agent_spec_name(main_spec, default_name="main", source="main-agent"),
                source="main-agent",
                raw_spec=main_spec,
                error=str(error),
            )

    reviewer_specs: list[object] = _config_reviewer_specs(stage_payload) if stage_payload else []
    if not reviewer_specs:
        reviewer_specs = _config_reviewer_specs(config_payload)
    cli_reviewers = list(getattr(args, "reviewer_agent", []) or [])
    if cli_reviewers:
        reviewer_specs = cli_reviewers

    reviewers: dict[str, ReviewAgentSelection] = {}
    invalid_reviewers: dict[str, InvalidAgentSelection] = {}
    reviewer_order: list[str] = []
    default_names = [str(item).strip() for item in default_reviewer_names if str(item).strip()]
    for index, reviewer_spec in enumerate(reviewer_specs):
        default_name = default_names[index] if index < len(default_names) else f"R{index + 1}"
        source = f"reviewer-agent[{index + 1}]"
        reviewer_name = _agent_spec_name(reviewer_spec, default_name=default_name, source=source)
        if not reviewer_name:
            reviewer_name = default_name
        reviewer_order.append(reviewer_name)
        try:
            _, selection = parse_agent_selection_spec(
                reviewer_spec,
                default_name=default_name,
                default_ponytail_mode=ponytail_mode,
                default_codegraph_mode=codegraph_mode,
                default_codegraph_config=codegraph_config,
                source=source,
            )
        except Exception as error:  # noqa: BLE001
            invalid_reviewers[reviewer_name] = InvalidAgentSelection(
                name=reviewer_name,
                source=source,
                raw_spec=reviewer_spec,
                error=str(error),
            )
            continue
        reviewers[reviewer_name] = selection
    return StageAgentConfig(
        main=main_selection,
        reviewers=reviewers,
        reviewer_order=tuple(reviewer_order),
        invalid_main=invalid_main,
        invalid_reviewers=invalid_reviewers,
        ponytail_mode=ponytail_mode,
        codegraph_mode=codegraph_mode,
        codegraph_config=codegraph_config,
    )


def prompt_review_agent_selection(
    default_vendor: str = DEFAULT_REQUIREMENTS_CLARIFICATION_VENDOR,
    default_model: str = "",
    default_reasoning_effort: str = DEFAULT_REQUIREMENTS_CLARIFICATION_EFFORT,
    default_proxy_url: str = "",
    default_ponytail_mode: str = PonytailMode.FULL.value,
    default_codegraph_mode: str = CodeGraphMode.OFF.value,
    default_codegraph_config: Mapping[str, object] | None = None,
    *,
    role_label: str = "",
    progress: ReviewStageProgress | None = None,
    allow_back_first_step: bool = False,
    stage_key: str = "agent_selection",
) -> ReviewAgentSelection:
    progress = resolve_review_progress(progress)
    while True:
        try:
            vendor = default_vendor
            model = default_model
            reasoning_effort = default_reasoning_effort
            proxy_url = default_proxy_url
            ponytail_mode = normalize_ponytail_mode(default_ponytail_mode, default=PonytailMode.FULL).value
            codegraph_mode = normalize_codegraph_mode(
                default_codegraph_mode,
                default=CodeGraphMode.OFF,
            ).value
            step = 0
            while step < 4:
                try:
                    with progress.suspended() if progress is not None else nullcontext():
                        with prompt_metadata(
                            allow_back=allow_back_first_step if step == 0 else True,
                            back_value=PROMPT_BACK_VALUE,
                            stage_key=stage_key,
                            stage_step_index=step,
                        ):
                            if step == 0:
                                vendor = prompt_vendor(vendor or default_vendor, role_label=role_label)
                                if model:
                                    try:
                                        normalize_model_choice(vendor, model)
                                    except ValueError:
                                        model = ""
                                step = 1
                                continue
                            if step == 1:
                                preferred_model = model if model and vendor == default_vendor else get_default_model_for_vendor(vendor)
                                model = prompt_model(vendor, preferred_model, role_label=role_label)
                                step = 2
                                continue
                            if step == 2:
                                reasoning_effort = prompt_effort(vendor, model, reasoning_effort or default_reasoning_effort, role_label=role_label)
                                step = 3
                                continue
                            if step == 3:
                                proxy_url = prompt_proxy_url(proxy_url, role_label=role_label)
                                step = 4
                                continue
                except PromptBackRequested:
                    if step == 0:
                        continue
                    step -= 1
            return ReviewAgentSelection(
                vendor=vendor,
                model=model,
                reasoning_effort=reasoning_effort,
                proxy_url=proxy_url,
                ponytail_mode=ponytail_mode,
                codegraph_mode=codegraph_mode,
                codegraph_config=normalize_codegraph_config(default_codegraph_config),
            )
        except Exception as error:  # noqa: BLE001
            if not is_agent_config_error(error):
                raise
            if not stdin_is_interactive():
                raise
            role_text = str(role_label or "").strip() or "智能体"
            message(f"{role_text} 模型配置不可用: {error}\n请重新选择厂商、模型和推理强度。")


def resolve_agent_run_config_with_recovery(
    selection: ReviewAgentSelection,
    *,
    role_label: str,
    progress: ReviewStageProgress | None = None,
    reason_text: str = "",
) -> tuple[ReviewAgentSelection, AgentRunConfig]:
    progress = resolve_review_progress(progress)
    current_selection = selection
    role_text = str(role_label or "").strip() or "智能体"
    while True:
        try:
            config = AgentRunConfig(
                vendor=current_selection.vendor,
                model=current_selection.model,
                reasoning_effort=current_selection.reasoning_effort,
                proxy_url=current_selection.proxy_url,
                ponytail_mode=current_selection.ponytail_mode,
                codegraph_mode=current_selection.codegraph_mode,
                codegraph_config=current_selection.codegraph_config,
            )
            return current_selection, config
        except Exception as error:  # noqa: BLE001
            if not is_agent_config_error(error):
                raise
            prompt_is_patched = getattr(prompt_review_agent_selection, "__module__", __name__) != __name__
            if not stdin_is_interactive() and not prompt_is_patched:
                raise RuntimeError(f"{role_text} 模型配置不可用: {error}；当前环境无法交互重新选择模型。") from error
            message(
                str(reason_text or "").strip()
                or f"{role_text} 模型配置不可用: {error}\n请重新选择模型配置后继续当前阶段。"
            )
            preserved_ponytail_mode = current_selection.ponytail_mode
            preserved_codegraph_mode = current_selection.codegraph_mode
            preserved_codegraph_config = current_selection.codegraph_config
            current_selection = prompt_review_agent_selection(
                default_vendor=current_selection.vendor,
                default_model=current_selection.model,
                default_reasoning_effort=current_selection.reasoning_effort,
                default_proxy_url=current_selection.proxy_url,
                role_label=role_text,
                progress=progress,
            )
            current_selection = replace(
                current_selection,
                ponytail_mode=preserved_ponytail_mode,
                codegraph_mode=preserved_codegraph_mode,
                codegraph_config=preserved_codegraph_config,
            )
            message(render_review_agent_selection(f"{role_text} 新配置", current_selection))


def render_review_agent_selection(title: str, selection: ReviewAgentSelection) -> str:
    return "\n".join(
        [
            title,
            f"vendor: {selection.vendor}",
            f"model: {selection.model}",
            f"reasoning_effort: {selection.reasoning_effort}",
            f"proxy_url: {selection.proxy_url or '(none)'}",
            f"ponytail_mode: {selection.ponytail_mode}",
            f"codegraph_mode: {selection.codegraph_mode}",
        ]
    )


def collect_reviewer_agent_selections(
    *,
    project_dir: str | Path,
    reviewer_specs: Sequence[object],
    display_name_resolver: Callable[[str | Path, object, Sequence[str]], str],
    progress: ReviewStageProgress | None = None,
    skip_reviewer_keys: Sequence[str] = (),
    reserved_session_names: Sequence[str] = (),
    allow_back_first_prompt: bool = False,
    stage_key: str = "reviewer_selection",
    default_ponytail_mode: str = PonytailMode.FULL.value,
    default_codegraph_mode: str = CodeGraphMode.OFF.value,
    default_codegraph_config: Mapping[str, object] | None = None,
) -> dict[str, ReviewAgentSelection]:
    selections: dict[str, ReviewAgentSelection] = {}
    predicted_session_names: set[str] = {str(name).strip() for name in reserved_session_names if str(name).strip()}
    skip_keys = {str(item).strip() for item in skip_reviewer_keys if str(item).strip()}
    interactive = stdin_is_interactive()
    next_allow_back = bool(allow_back_first_prompt)
    for reviewer_spec in reviewer_specs:
        reviewer_key = str(
            getattr(reviewer_spec, "reviewer_key", "") or getattr(reviewer_spec, "role_name", "")
        ).strip()
        if not reviewer_key or reviewer_key in skip_keys:
            continue
        reviewer_display_name = display_name_resolver(project_dir, reviewer_spec, sorted(predicted_session_names))
        predicted_session_names.add(reviewer_display_name)
        if interactive:
            selection = prompt_review_agent_selection(
                DEFAULT_REQUIREMENTS_CLARIFICATION_VENDOR,
                default_model=DEFAULT_REQUIREMENTS_CLARIFICATION_MODEL,
                default_reasoning_effort=DEFAULT_REQUIREMENTS_CLARIFICATION_EFFORT,
                default_proxy_url="",
                default_codegraph_mode=default_codegraph_mode,
                default_codegraph_config=default_codegraph_config,
                role_label=reviewer_display_name,
                progress=progress,
                allow_back_first_step=next_allow_back,
                stage_key=stage_key,
            )
            selection = replace(
                selection,
                ponytail_mode=normalize_ponytail_mode(
                    default_ponytail_mode,
                    default=PonytailMode.FULL,
                ).value,
                codegraph_mode=normalize_codegraph_mode(
                    default_codegraph_mode,
                    default=CodeGraphMode.OFF,
                ).value,
                codegraph_config=normalize_codegraph_config(default_codegraph_config),
            )
            next_allow_back = False
            message(render_review_agent_selection(f"{reviewer_display_name} 配置", selection))
        else:
            selection = ReviewAgentSelection(
                vendor=DEFAULT_REQUIREMENTS_CLARIFICATION_VENDOR,
                model=DEFAULT_REQUIREMENTS_CLARIFICATION_MODEL,
                reasoning_effort=DEFAULT_REQUIREMENTS_CLARIFICATION_EFFORT,
                proxy_url="",
                ponytail_mode=normalize_ponytail_mode(
                    default_ponytail_mode,
                    default=PonytailMode.FULL,
                ).value,
                codegraph_mode=normalize_codegraph_mode(
                    default_codegraph_mode,
                    default=CodeGraphMode.OFF,
                ).value,
                codegraph_config=normalize_codegraph_config(default_codegraph_config),
            )
        selections[reviewer_key] = selection
    return selections


def prompt_yes_no_choice(
    prompt_text: str,
    default: bool = False,
    *,
    progress: ReviewStageProgress | None = None,
    preview_path: str | Path | None = None,
    preview_title: str = "",
    allow_back: bool = False,
    stage_key: str = "",
    stage_step_index: int = 0,
) -> bool:
    progress = resolve_review_progress(progress)
    with progress.suspended() if progress is not None else nullcontext():
        with prompt_metadata(
            allow_back=allow_back,
            back_value=PROMPT_BACK_VALUE,
            stage_key=stage_key,
            stage_step_index=stage_step_index,
        ):
            return terminal_prompt_yes_no(
                prompt_text,
                default,
                preview_path=preview_path,
                preview_title=preview_title,
            )


def prompt_replacement_review_agent_selection(
    *,
    reason_text: str,
    previous_selection: ReviewAgentSelection,
    force_model_change: bool,
    role_label: str,
    progress: ReviewStageProgress | None = None,
) -> ReviewAgentSelection | None:
    progress = resolve_review_progress(progress)
    message(reason_text)
    if not prompt_yes_no_choice(f"是否创建新的{role_label}继续当前阶段", True, progress=progress):
        return None
    while True:
        selection = prompt_review_agent_selection(
            default_vendor=previous_selection.vendor,
            default_model=previous_selection.model,
            default_reasoning_effort=previous_selection.reasoning_effort,
            default_proxy_url=previous_selection.proxy_url,
            default_ponytail_mode=previous_selection.ponytail_mode,
            default_codegraph_mode=previous_selection.codegraph_mode,
            default_codegraph_config=previous_selection.codegraph_config,
            role_label=role_label,
            progress=progress,
        )
        selection = replace(
            selection,
            ponytail_mode=previous_selection.ponytail_mode,
            codegraph_mode=previous_selection.codegraph_mode,
            codegraph_config=dict(previous_selection.codegraph_config),
        )
        if not force_model_change or (
            selection.vendor != previous_selection.vendor
            or selection.model != previous_selection.model
        ):
            return selection
        message("新的智能体必须切换 vendor 或 model，当前选择与旧智能体完全相同，请重新选择。")


def prompt_required_replacement_review_agent_selection(
    *,
    reason_text: str,
    previous_selection: ReviewAgentSelection,
    force_model_change: bool,
    role_label: str,
    progress: ReviewStageProgress | None = None,
) -> ReviewAgentSelection:
    progress = resolve_review_progress(progress)
    prompt_is_patched = getattr(prompt_review_agent_selection, "__module__", __name__) != __name__
    if not stdin_is_interactive() and not prompt_is_patched:
        raise RuntimeError(f"{role_label} 需要重新配置智能体，但当前环境无法交互选择厂商/模型。")
    message(reason_text)
    while True:
        selection = prompt_review_agent_selection(
            default_vendor=previous_selection.vendor,
            default_model=previous_selection.model,
            default_reasoning_effort=previous_selection.reasoning_effort,
            default_proxy_url=previous_selection.proxy_url,
            default_ponytail_mode=previous_selection.ponytail_mode,
            default_codegraph_mode=previous_selection.codegraph_mode,
            default_codegraph_config=previous_selection.codegraph_config,
            role_label=role_label,
            progress=progress,
        )
        selection = replace(
            selection,
            ponytail_mode=previous_selection.ponytail_mode,
            codegraph_mode=previous_selection.codegraph_mode,
            codegraph_config=dict(previous_selection.codegraph_config),
        )
        if not force_model_change or (
            selection.vendor != previous_selection.vendor
            or selection.model != previous_selection.model
        ):
            message(render_review_agent_selection(f"重新创建{role_label}", selection))
            return selection
        message("新的智能体必须切换 vendor 或 model，当前选择与旧智能体完全相同，请重新选择。")


def render_tmux_start_summary(role_name: str, worker: TmuxBatchWorker) -> str:
    return "\n".join(
        [
            f"{role_name} 已创建",
            f"runtime_dir: {worker.runtime_dir}",
            f"session_name: {worker.session_name}",
            "首次执行任务时会等待 READY；启动失败将进入阶段恢复逻辑。",
            "可使用以下命令进入会话:",
            f"  tmux attach -t {worker.session_name}",
        ]
    )


def worker_has_provider_auth_error(worker: TmuxBatchWorker | None) -> bool:
    if worker is None:
        return False
    try:
        state = worker.read_state()
    except Exception:
        state = {}
    health_status = str(state.get("health_status", "")).strip().lower()
    health_note = str(state.get("health_note", "")).strip().lower()
    return health_status == "provider_auth_error" or is_provider_auth_error(health_note)


def worker_has_provider_runtime_error(worker: TmuxBatchWorker | None) -> bool:
    if worker is None:
        return False
    try:
        state = worker.read_state()
    except Exception:
        state = {}
    health_status = str(state.get("health_status", "")).strip().lower()
    health_note = str(state.get("health_note", "")).strip().lower()
    last_provider_error = str(state.get("last_provider_error", "")).strip().lower()
    return (
        health_status == "provider_runtime_error"
        or is_provider_runtime_error(health_note)
        or is_provider_runtime_error(last_provider_error)
    )


def worker_has_agent_config_error(worker: TmuxBatchWorker | None) -> bool:
    if worker is None:
        return False
    try:
        state = worker.read_state()
    except Exception:
        state = {}
    health_status = str(state.get("health_status", "")).strip().lower()
    return health_status in {"agent_config_error", "config_error"} or any(
        is_agent_config_error(state.get(key, ""))
        for key in ("health_note", "note", "last_provider_error", "dispatch_reason")
    )


def is_recoverable_startup_failure(error: Exception, worker: TmuxBatchWorker | None = None) -> bool:
    message_text = str(error or "").strip().lower()
    if is_agent_startup_intervention_error(error):
        return True
    if is_agent_config_error(error) or worker_has_agent_config_error(worker):
        return True
    if is_provider_auth_error(error) or worker_has_provider_auth_error(worker):
        return True
    if is_provider_runtime_error(error) or worker_has_provider_runtime_error(worker):
        return True
    if is_agent_ready_timeout_error(error):
        return True
    if "shell initialization timed out" in message_text:
        return True
    if "agent exited back to shell while starting" in message_text:
        return True
    return False


def mark_worker_awaiting_reconfiguration(
    worker: TmuxBatchWorker | None,
    *,
    reason_text: str,
) -> None:
    if worker is None:
        return
    marker = getattr(worker, "mark_awaiting_reconfiguration", None)
    if not callable(marker):
        return
    try:
        marker(reason_text=reason_text)
    except Exception:
        return


def prompt_agent_ready_timeout_recovery(
    worker: TmuxBatchWorker | None,
    *,
    role_label: str,
    can_skip: bool,
    progress: ReviewStageProgress | None = None,
    reason_text: str = "",
    allow_recreate: bool = False,
    target_paths: Sequence[str | Path] = (),
    noninteractive_default: str | None = None,
) -> str:
    role_text = str(role_label or "").strip() or "智能体"
    session_name = str(getattr(worker, "session_name", "") or "").strip()
    reason = str(reason_text or "").strip() or (
        f"{session_name or role_text}启动超时，未能进入可输入状态。\n"
        "请先手动更换模型或处理该 AGENT，再选择恢复动作。"
    )
    return request_worker_manual_intervention(
        stage_label="智能体启动超时",
        role_label=session_name or role_text,
        worker=worker,
        reason_text=reason,
        target_paths=target_paths,
        progress=progress,
        allow_recreate=allow_recreate,
        allow_worker_dead=can_skip,
        noninteractive_default=noninteractive_default,
    )


def ensure_empty_file(file_path: str | Path) -> Path:
    target = Path(file_path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("", encoding="utf-8")
    return target


def ensure_review_artifacts(md_path: str | Path, json_path: str | Path) -> tuple[Path, Path]:
    review_md = ensure_empty_file(md_path)
    review_json = Path(json_path).expanduser().resolve()
    review_json.parent.mkdir(parents=True, exist_ok=True)
    review_json.write_text("[]", encoding="utf-8")
    return review_md, review_json


def ensure_review_artifacts_exist(md_path: str | Path, json_path: str | Path) -> tuple[Path, Path]:
    """Create missing review artifacts without truncating an earlier result."""
    review_md = Path(md_path).expanduser().resolve()
    review_json = Path(json_path).expanduser().resolve()
    review_md.parent.mkdir(parents=True, exist_ok=True)
    review_json.parent.mkdir(parents=True, exist_ok=True)
    if not review_md.exists():
        review_md.write_text("", encoding="utf-8")
    if not review_json.exists():
        review_json.write_text("[]", encoding="utf-8")
    return review_md, review_json


def collect_review_limit_hitl_response(
    question_path: str | Path,
    *,
    stage_label: str,
    hitl_round: int,
    answer_path: str | Path | None = None,
    progress: ReviewStageProgress | None = None,
) -> str:
    question_file = Path(question_path).expanduser().resolve()
    question_text = question_file.read_text(encoding="utf-8").strip() if question_file.exists() else ""
    message()
    message(f"{stage_label} 第 {hitl_round} 轮，需要人工补充信息")
    message(f"问题文档: {question_file}")
    message(question_text or "(问题文档为空)")
    if progress is not None:
        progress.set_phase(f"{stage_label} / 等待 HITL")
    with progress.suspended() if progress is not None else nullcontext():
        return collect_multiline_input(
            title=f"{stage_label} HITL 第 {hitl_round} 轮回复",
            empty_retry_message="回复不能为空，请重新输入。",
            question_path=question_file,
            answer_path=answer_path,
            is_hitl=True,
        )


def collect_auto_review_limit_hitl_response(
    question_path: str | Path,
    *,
    stage_label: str,
    hitl_round: int,
) -> str:
    question_file = Path(question_path).expanduser().resolve()
    question_text = question_file.read_text(encoding="utf-8").strip() if question_file.exists() else ""
    message()
    message(f"{stage_label} 第 {hitl_round} 轮 已由 --yes 自动回复，继续非交互流程。")
    return (
        f"{stage_label} 第 {hitl_round} 轮自动回复：当前以 --yes 非交互模式运行。"
        "请采用最保守的修正路径：严格按已有原始需求、澄清记录和评审记录补齐遗漏，"
        "删除或改写无来源依据的扩展内容，不新增需求、不跳过评审、不再等待人工输入。"
        "若问题文档提供多个方案，优先选择“原样同步/最小修正/无扩展”的方案；"
        "将本轮自动决策和假设追加到 HITL 记录，随后清空问题文档并继续当前阶段。"
        "\n\n[自动回复所依据的问题文档]\n"
        f"{question_text or '(问题文档为空)'}"
    )


def _append_review_limit_human_response(
    *,
    hitl_record_file: Path,
    stage_label: str,
    hitl_round: int,
    human_msg: str,
) -> None:
    message_text = str(human_msg or "").strip()
    if not message_text:
        return
    hitl_record_file.parent.mkdir(parents=True, exist_ok=True)
    prefix = "\n\n" if hitl_record_file.exists() and hitl_record_file.read_text(encoding="utf-8").strip() else ""
    with hitl_record_file.open("a", encoding="utf-8") as file_obj:
        file_obj.write(f"{prefix}## {stage_label} HITL 第 {hitl_round} 轮人类回复\n")
        file_obj.write(message_text)
        file_obj.write("\n")


def parse_review_max_rounds(value: object, *, source: str, default: int = DEFAULT_STAGE_REVIEW_MAX_ROUNDS) -> int | None:
    text = str(value or "").strip()
    if not text:
        return int(default)
    if text.lower() == "infinite":
        return None
    try:
        parsed = int(text)
    except Exception as error:  # noqa: BLE001
        raise RuntimeError(f"{source} 必须是正整数或 infinite") from error
    if parsed <= 0:
        raise RuntimeError(f"{source} 必须是正整数或 infinite")
    return parsed


def prompt_review_max_rounds(
    *,
    default: int = DEFAULT_STAGE_REVIEW_MAX_ROUNDS,
    progress: ReviewStageProgress | None = None,
    prompt_text: str = "输入最大审核轮次（输入 infinite 表示不设上限）",
    source: str = "最大审核轮次",
    allow_back: bool = False,
    stage_key: str = "",
    stage_step_index: int = 0,
) -> int | None:
    progress = resolve_review_progress(progress)
    with progress.suspended() if progress is not None else nullcontext():
        while True:
            with prompt_metadata(
                allow_back=allow_back,
                back_value=PROMPT_BACK_VALUE,
                stage_key=stage_key,
                stage_step_index=stage_step_index,
            ):
                value = prompt_with_default(prompt_text, str(default)).strip()
            try:
                return parse_review_max_rounds(value, source=source, default=default)
            except RuntimeError as error:
                message(str(error))


def render_review_limit_force_hitl_prompt(
    *,
    config: ReviewLimitHitlConfig,
    review_limit: int,
    review_rounds_used: int,
    hitl_record_md: str | Path,
    extra_inputs: Sequence[str | Path] = (),
) -> str:
    merged_review_md = str(Path(config.merged_review_path).expanduser().resolve())
    ask_human_md = str(Path(config.ask_human_path).expanduser().resolve())
    output_md = str(Path(config.primary_output_path).expanduser().resolve())
    hitl_record_md = str(Path(hitl_record_md).expanduser().resolve())
    feedback_md = str(Path(config.output_summary_path).expanduser().resolve())
    extra_input_list = [str(Path(item).expanduser().resolve()) for item in extra_inputs]
    input_lines = "\n".join(f"- 《{item}》" for item in [merged_review_md, output_md, hitl_record_md, *extra_input_list])
    return f"""## 任务目标
当前《{merged_review_md}》对应的评审已累计 {review_rounds_used} 轮，达到上限 {review_limit}。
你现在必须停止继续自修，改为发起一次强制 HITL，请人类给出新的决策信息。

## 必读输入
{input_lines}

## 强制执行步骤
1. 阅读并去重《{merged_review_md}》中的多轮评审意见。
2. 结合《{output_md}》与《{hitl_record_md}》，总结“为什么多轮仍未通过”。
3. 只保留必须由人类拍板的缺口，覆盖写入《{ask_human_md}》。
4. 不要继续尝试闭环该问题，不要声称已完成继续工作。

## 《{ask_human_md}》固定结构
- [多轮未通过原因]
- [仍未闭环的问题]
- [需要人类决策]
- [可选方案与影响]
- [继续工作后将修改的产物]

## 输出约束
- 允许修改：《{ask_human_md}》、可选更新《{hitl_record_md}》。
- 禁止修改：《{output_md}》与其他业务产物。
- 若《{ask_human_md}》为空，视为失败。
- 只允许返回 `HITL`。
"""


def render_review_limit_human_reply_prompt(
    *,
    config: ReviewLimitHitlConfig,
    human_msg: str,
    hitl_record_md: str | Path,
    extra_inputs: Sequence[str | Path] = (),
) -> str:
    ask_human_md = str(Path(config.ask_human_path).expanduser().resolve())
    output_md = str(Path(config.primary_output_path).expanduser().resolve())
    hitl_record_md = str(Path(hitl_record_md).expanduser().resolve())
    feedback_md = str(Path(config.output_summary_path).expanduser().resolve())
    extra_input_list = [str(Path(item).expanduser().resolve()) for item in extra_inputs]
    input_lines = "\n".join(f"- 《{item}》" for item in [output_md, hitl_record_md, *extra_input_list])
    return f"""## 任务目标
你上一轮因评审超过上限触发了 HITL。现在人类已经回复，请先同步人类信息，再继续当前阶段工作。

## 人类回复
[HUMAN MSG START]
{human_msg}
[HUMAN MSG END]

## 必读输入
{input_lines}

## 执行步骤
1. 解析人类回复，区分有效信息、噪音、冲突修订。
2. 以追加 / 拦截 / 覆写规则同步《{hitl_record_md}》。
3. 若信息仍不足，继续覆盖写入《{ask_human_md}》并返回 `HITL`。
4. 若信息足够，继续当前阶段工作，必须更新《{output_md}》。
5. 如有必要，同时更新《{feedback_md}》说明本轮处理结果。

## 输出约束
- 如果仍需人类介入：必须写《{ask_human_md}》，只返回 `HITL`。
- 如果信息已足够：必须清空《{ask_human_md}》，并完成《{config.continue_output_label}》对应产物更新，只返回 `修改完成`。
- 禁止输出其他文本。
"""


def run_review_limit_hitl_cycle(
    *,
    stage_label: str,
    ask_human_path: str | Path,
    hitl_record_path: str | Path,
    initial_turn: Callable[[], object],
    human_reply_turn: Callable[[str], object],
    human_input_provider: Callable[[Path, int], str] | None = None,
    progress: ReviewStageProgress | None = None,
    max_hitl_rounds: int = 8,
    on_hitl_question: Callable[[int, Path], None] | None = None,
    on_hitl_answer: Callable[[int, str, Path], None] | None = None,
) -> ReviewLimitHitlResult:
    ask_human_file = Path(ask_human_path).expanduser().resolve()
    hitl_record_file = Path(hitl_record_path).expanduser().resolve()
    owner = initial_turn()
    if not ask_human_file.exists() or not ask_human_file.read_text(encoding="utf-8").strip():
        raise RuntimeError(f"{stage_label} 超限后未生成有效《{ask_human_file.name}》")
    post_hitl_continue_completed = False

    def _invoke_callback(callback: Callable[..., object] | None, callback_name: str, *args: object) -> None:
        if callback is None:
            return
        try:
            callback(*args)
        except Exception as error:  # noqa: BLE001
            try:
                message(f"警告：评审超限 HITL 回调失败 callback={callback_name} error={error}")
            except Exception:
                pass

    for hitl_round in range(1, max_hitl_rounds + 1):
        question_text_before = ask_human_file.read_text(encoding="utf-8")
        if not question_text_before.strip():
            return ReviewLimitHitlResult(
                owner=owner,
                rounds_used=hitl_round - 1,
                post_hitl_continue_completed=post_hitl_continue_completed,
            )
        _invoke_callback(on_hitl_question, "on_hitl_question", hitl_round, ask_human_file)
        if human_input_provider is not None:
            human_msg = human_input_provider(ask_human_file, hitl_round)
        else:
            human_msg = collect_review_limit_hitl_response(
                ask_human_file,
                stage_label=stage_label,
                hitl_round=hitl_round,
                answer_path=hitl_record_file,
                progress=progress,
            )
        human_msg = str(human_msg or "").strip()
        if not human_msg:
            if progress is not None:
                progress.set_phase(f"{stage_label} / 等待 HITL")
            continue
        _append_review_limit_human_response(
            hitl_record_file=hitl_record_file,
            stage_label=stage_label,
            hitl_round=hitl_round,
            human_msg=human_msg,
        )
        _invoke_callback(on_hitl_answer, "on_hitl_answer", hitl_round, human_msg, hitl_record_file)
        ask_human_file.write_text("", encoding="utf-8")
        try:
            owner = human_reply_turn(human_msg)
        except Exception:
            with suppress(Exception):
                if not ask_human_file.read_text(encoding="utf-8").strip():
                    ask_human_file.write_text(question_text_before, encoding="utf-8")
            raise
        post_hitl_continue_completed = True
    raise RuntimeError(f"{stage_label} HITL 轮次超过上限: {max_hitl_rounds}")
