from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path

from A01_Routing_LayerPlanning import (
    DEFAULT_MODEL_BY_VENDOR,
    prompt_effort,
    prompt_model,
    prompt_vendor,
)
from canopy_core.runtime.contracts import TurnFileContract
from canopy_core.runtime.tmux_runtime import TmuxBatchWorker, Vendor, is_provider_auth_error
from T09_terminal_ops import (
    SingleLineSpinnerMonitor,
    TERMINAL_SPINNER_FRAMES,
    message,
    prompt_positive_int as terminal_prompt_positive_int,
    prompt_with_default,
    prompt_yes_no as terminal_prompt_yes_no,
)
from T12_requirements_common import (
    DEFAULT_REQUIREMENTS_CLARIFICATION_EFFORT,
    DEFAULT_REQUIREMENTS_CLARIFICATION_VENDOR,
)

DEFAULT_REVIEWER_COUNT = 1
MAX_REVIEWER_REPAIR_ATTEMPTS = 2


@dataclass(frozen=True)
class ReviewAgentSelection:
    vendor: str
    model: str
    reasoning_effort: str
    proxy_url: str


@dataclass(frozen=True)
class ReviewerRuntime:
    reviewer_name: str
    selection: ReviewAgentSelection
    worker: TmuxBatchWorker
    review_md_path: Path
    review_json_path: Path
    contract: TurnFileContract


@dataclass(frozen=True)
class ReviewAgentHandoff:
    reviewer_key: str
    role_name: str
    role_prompt: str
    selection: ReviewAgentSelection
    worker: TmuxBatchWorker


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


def prompt_positive_int(prompt_text: str, default: int = 1, *, progress: ReviewStageProgress | None = None) -> int:
    progress = resolve_review_progress(progress)
    with progress.suspended() if progress is not None else nullcontext():
        return terminal_prompt_positive_int(prompt_text, default)


def prompt_review_agent_selection(
    default_vendor: str = DEFAULT_REQUIREMENTS_CLARIFICATION_VENDOR,
    default_model: str = "",
    default_reasoning_effort: str = DEFAULT_REQUIREMENTS_CLARIFICATION_EFFORT,
    default_proxy_url: str = "",
    *,
    role_label: str = "",
    progress: ReviewStageProgress | None = None,
) -> ReviewAgentSelection:
    progress = resolve_review_progress(progress)
    with progress.suspended() if progress is not None else nullcontext():
        vendor = prompt_vendor(default_vendor, role_label=role_label)
        preferred_model = default_model if default_model and vendor == default_vendor else DEFAULT_MODEL_BY_VENDOR[vendor]
        model = prompt_model(vendor, preferred_model, role_label=role_label)
        reasoning_effort = prompt_effort(vendor, model, default_reasoning_effort, role_label=role_label)
        proxy_url = prompt_proxy_url(default_proxy_url, role_label=role_label)
    return ReviewAgentSelection(
        vendor=vendor,
        model=model,
        reasoning_effort=reasoning_effort,
        proxy_url=proxy_url,
    )


def render_review_agent_selection(title: str, selection: ReviewAgentSelection) -> str:
    return "\n".join(
        [
            title,
            f"vendor: {selection.vendor}",
            f"model: {selection.model}",
            f"reasoning_effort: {selection.reasoning_effort}",
            f"proxy_url: {selection.proxy_url or '(none)'}",
        ]
    )


def prompt_yes_no_choice(
    prompt_text: str,
    default: bool = False,
    *,
    progress: ReviewStageProgress | None = None,
    preview_path: str | Path | None = None,
    preview_title: str = "",
) -> bool:
    progress = resolve_review_progress(progress)
    with progress.suspended() if progress is not None else nullcontext():
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
            role_label=role_label,
            progress=progress,
        )
        if not force_model_change or (
            selection.vendor != previous_selection.vendor
            or selection.model != previous_selection.model
        ):
            return selection
        message("新的智能体必须切换 vendor 或 model，当前选择与旧智能体完全相同，请重新选择。")


def render_tmux_start_summary(role_name: str, worker: TmuxBatchWorker) -> str:
    return "\n".join(
        [
            f"{role_name} 已创建",
            f"runtime_dir: {worker.runtime_dir}",
            f"session_name: {worker.session_name}",
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


def ensure_empty_file(file_path: str | Path) -> Path:
    target = Path(file_path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("", encoding="utf-8")
    return target
