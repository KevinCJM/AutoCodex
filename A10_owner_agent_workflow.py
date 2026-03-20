# -*- encoding: utf-8 -*-
"""
@File: A10_owner_agent_workflow.py
@Modify Time: 2026/3/20
@Author: Kevin-Chen
@Descriptions: owner + analyst/tester/auditor 四阶段工作流
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from B00_agent_config import (
    AGENT_SKILLS_DICT,
    BOOTSTRAP_CONTEXT_TOKEN,
    BOOTSTRAP_INIT_TOKEN,
    BOOTSTRAP_ROLE_TOKEN,
    DEVELOPMENT_DONE_TOKEN,
    HUMAN_QUESTION_TRIGGER,
    MAX_HUMAN_QA_ROUND,
    OWNER_AGENT_INIT_PROMPT,
    OWNER_AGENT_NAME,
    OWNER_STAGE_DONE_TOKEN,
    REQUIREMENT_CLARIFICATION_MD,
    REVIEWER_AGENT_NAMES,
    REVIEWER_INIT_PROMPTS,
    REVIEW_PASS_TOKEN,
    REVIEW_VERDICT_ASK_HUMAN,
    REVIEW_VERDICT_BLOCKED,
    REVIEW_VERDICT_PASS,
    REVIEW_VERDICT_REVISE,
    REVIEW_VERDICT_TOKENS,
    TASK_DOC_STRUCTURE_PROMPT,
    TASK_EXECUTION_PROMPT,
    common_init_prompt_1,
    common_init_prompt_2,
    delivery_report_md,
    design_md,
    design_trace_json,
    format_agent_skills,
    get_agent_runtime_info,
    requirement_spec_md,
    requirement_str,
    run_agent,
    task_md,
    task_run_report_dir,
    task_schedule_json,
    test_plan_md,
    today_str,
    workflow_event_jsonl,
    workflow_state_json,
    working_path,
)
from B02_log_tools import Colors, log_message

print_lock = threading.Lock()
WORK_DIR = Path(working_path).expanduser().resolve()
RUN_ID = datetime.now().strftime("%Y%m%d%H%M%S")
WORKFLOW_STATE_PATH = WORK_DIR / workflow_state_json
WORKFLOW_EVENT_PATH = WORK_DIR / workflow_event_jsonl
RUN_REPORTS_DIR = WORK_DIR / task_run_report_dir
LAST_CHECKPOINT_ID = None
AGENT_SESSION_ID_DICT = {}

MAX_STAGE_REVIEW_ROUNDS = 5
TASK_STATUS_DONE = {"done", "completed"}
TASK_CHECKED_RE_TEMPLATE = r"^\s*[-*]\s+\[(?P<checked>[xX])\]\s+{task_id}\b"

PHASE_TITLES = {
    "requirement_specification": "需求指定",
    "detailed_design": "详细设计",
    "task_planning": "任务规划",
    "development_testing": "开发与测试",
}

REVIEWER_STAGE_FOCUS = {
    "requirement_specification": {
        "analyst": "重点审查范围是否清晰、需求是否可验证、未决问题是否清零或显式阻塞。",
        "tester": "重点审查验收标准、边界场景、非功能约束和测试可执行性是否足够明确。",
        "auditor": "重点审查约束、风险、失败路径、澄清闭环与阶段推进条件是否明确。",
    },
    "detailed_design": {
        "analyst": "重点审查需求到设计的可追踪性、接口行为与用户场景是否一致、有无越界设计。",
        "tester": "重点审查设计是否给出可测接口、验证点、回归范围和失败场景。",
        "auditor": "重点审查架构边界、状态一致性、异常处理、可观测性与恢复策略。",
    },
    "task_planning": {
        "analyst": "重点审查任务是否完整覆盖设计、粒度是否足够小、依赖顺序是否合理。",
        "tester": "重点审查每个任务单是否具备明确验证命令、测试范围和回归要求。",
        "auditor": "重点审查 Markdown 与 JSON 计划是否一致、机器调度真值源是否可靠、是否存在重复/遗漏任务。",
    },
    "development_testing": {
        "analyst": "重点审查当前改动是否严格落在当前 task_id，是否引入未批准范围或改变既定用户行为。",
        "tester": "重点审查当前改动是否有对应测试、回归覆盖和明确的验证证据，是否存在漏测。",
        "auditor": "重点审查当前改动的架构一致性、状态更新、运行记录、日志与交付物是否同步。",
    },
}


def _now_iso():
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _agent_log_file(agent_name):
    return str(WORK_DIR / f"agent_{agent_name}_{today_str}.log")


def _abs_path(file_name):
    path = Path(file_name)
    if path.is_absolute():
        return path
    return WORK_DIR / path


def _ensure_parent(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)


def _append_workflow_event(payload):
    _ensure_parent(WORKFLOW_EVENT_PATH)
    with WORKFLOW_EVENT_PATH.open("a", encoding="utf-8") as file:
        file.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _prompt_sha(prompt):
    return hashlib.sha256(str(prompt or "").encode("utf-8")).hexdigest()


def _artifact_sha(artifact_paths, include_git_snapshot=False):
    hasher = hashlib.sha256()
    for artifact_path in sorted({str(path) for path in artifact_paths}):
        path = Path(artifact_path)
        hasher.update(str(path).encode("utf-8"))
        if path.exists():
            hasher.update(path.read_bytes())
        else:
            hasher.update(b"__MISSING__")
    if include_git_snapshot:
        for cmd in (
                ["git", "status", "--porcelain=v1", "-uall"],
                ["git", "diff", "--no-ext-diff", "--binary", "HEAD", "--"],
        ):
            result = subprocess.run(
                cmd,
                cwd=str(WORK_DIR),
                capture_output=True,
                text=True,
                check=False,
            )
            hasher.update(result.stdout.encode("utf-8"))
            hasher.update(result.stderr.encode("utf-8"))
    return hasher.hexdigest()


def _sync_agent_session_id(agent_name, session_id=""):
    runtime_info = get_agent_runtime_info(agent_name)
    runtime_session_id = str(
        runtime_info.get("agent_session_id")
        or runtime_info.get("codex_session_id")
        or ""
    ).strip()
    tracked_session_id = runtime_session_id or str(session_id or "").strip() or AGENT_SESSION_ID_DICT.get(agent_name, "")
    if tracked_session_id:
        AGENT_SESSION_ID_DICT[agent_name] = tracked_session_id
    return tracked_session_id, runtime_info


def _run_agent_turn(
        agent_name,
        log_file_path,
        prompt,
        init_yn=True,
        session_id=None,
        required_token=None,
        reply_validator=None,
):
    msg, latest_session_id = run_agent(
        agent_name,
        log_file_path,
        prompt,
        init_yn=init_yn,
        session_id=session_id,
        required_token=required_token,
        reply_validator=reply_validator,
    )
    tracked_session_id, _ = _sync_agent_session_id(agent_name, latest_session_id)
    return msg, tracked_session_id


def _runtime_payload():
    payload = {}
    for agent_name, session_id in AGENT_SESSION_ID_DICT.items():
        tracked_session_id, runtime_info = _sync_agent_session_id(agent_name, session_id)
        payload[agent_name] = {
            "agent_session_id": tracked_session_id,
            "tmux_session": runtime_info.get("session_name", ""),
            "pane_id": runtime_info.get("pane_id", ""),
            "codex_session_id": runtime_info.get("agent_session_id", ""),
            "state_path": runtime_info.get("state_path", ""),
            "log_path": runtime_info.get("log_path", ""),
            "raw_log_path": runtime_info.get("raw_log_path", ""),
            "confirmed_status": runtime_info.get("confirmed_status", ""),
            "detected_status": runtime_info.get("detected_status", ""),
            "updated_at": runtime_info.get("updated_at", ""),
        }
    return payload


def record_checkpoint(
        *,
        note,
        phase_id,
        phase_round=0,
        owner_revision=0,
        reviewer_id="",
        task_id="",
        artifact_refs=None,
        artifact_sha="",
        verdict="",
        next_action="",
        waiting_human=False,
        loop_guard_reason="",
        prompt="",
):
    global LAST_CHECKPOINT_ID
    checkpoint_id = uuid.uuid4().hex[:12]
    payload = {
        "run_id": RUN_ID,
        "checkpoint_id": checkpoint_id,
        "parent_checkpoint_id": LAST_CHECKPOINT_ID,
        "phase_id": phase_id,
        "phase_title": PHASE_TITLES.get(phase_id, phase_id),
        "phase_round": phase_round,
        "owner_id": OWNER_AGENT_NAME,
        "owner_revision": owner_revision,
        "reviewer_id": reviewer_id,
        "task_id": task_id,
        "artifact_ref": [str(path) for path in (artifact_refs or [])],
        "artifact_sha": artifact_sha,
        "worktree": str(WORK_DIR),
        "prompt_sha": _prompt_sha(prompt) if prompt else "",
        "verdict": verdict,
        "next_action": next_action,
        "resume_count": 0,
        "loop_guard_reason": loop_guard_reason,
        "waiting_human": waiting_human,
        "note": note,
        "created_at": _now_iso(),
        "agents": _runtime_payload(),
    }
    _ensure_parent(WORKFLOW_STATE_PATH)
    WORKFLOW_STATE_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    _append_workflow_event(payload)
    LAST_CHECKPOINT_ID = checkpoint_id
    return payload


def prepare_agent_prompt(agent_name, agent_prompt):
    skills_prefix = format_agent_skills(agent_name, AGENT_SKILLS_DICT)
    return f"{skills_prefix}\n{agent_prompt}".strip()


def _with_bootstrap_token(prompt, token):
    return f"{str(prompt or '').strip()}\n\n完成后只回复 `{token}`，不要补充其它内容。".strip()


def ask_human(question, log_file_path):
    question = str(question or "").strip()
    if not question:
        raise ValueError("ask_human 问题不能为空")

    with print_lock:
        log_message(
            log_file_path=log_file_path,
            message=f"[ask_human] 问题:\n{question}",
            color=Colors.CYAN,
        )

    print("\n" + "=" * 100)
    print("[需要人类确认] 以下问题请你回答:")
    print(question)
    print("=" * 100)

    try:
        answer = input("请输入你的回答: ").strip()
    except EOFError:
        answer = "人类未提供输入（EOF）。"
    except KeyboardInterrupt:
        answer = "人类中断了输入（KeyboardInterrupt）。"

    if not answer:
        answer = "人类未提供有效回答（空输入）。"

    with print_lock:
        log_message(
            log_file_path=log_file_path,
            message=f"[ask_human] 回答:\n{answer}",
            color=Colors.MAGENTA,
        )
    return answer


def extract_human_question(text):
    content = str(text or "").strip()
    if not content:
        return None
    matched = re.search(rf"{re.escape(HUMAN_QUESTION_TRIGGER)}\s*(.+)", content, flags=re.S)
    if matched:
        question = matched.group(1).strip()
        if question:
            return question
    return None


def append_clarification_record(question, human_answer, owner_reply, log_file_path):
    clarification_path = _abs_path(REQUIREMENT_CLARIFICATION_MD)
    _ensure_parent(clarification_path)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    section = f"""
## 需求澄清记录 {timestamp}

- owner 问题: {question}
- 人类回答: {human_answer}
- owner 处理结果:
{owner_reply}

"""
    with clarification_path.open("a", encoding="utf-8") as file:
        file.write(section)

    with print_lock:
        log_message(
            log_file_path=log_file_path,
            message=f"[clarification] 已将问答追加到需求澄清文档: {clarification_path}",
            color=Colors.GREEN,
        )


def boot_agent(agent_name, custom_prompt):
    log_file_path = _agent_log_file(agent_name)
    _, session_id = _run_agent_turn(
        agent_name,
        log_file_path,
        _with_bootstrap_token(common_init_prompt_1, BOOTSTRAP_INIT_TOKEN),
        init_yn=True,
        session_id=None,
        required_token=BOOTSTRAP_INIT_TOKEN,
    )
    if agent_name == OWNER_AGENT_NAME:
        _, session_id = _run_agent_turn(
            agent_name,
            log_file_path,
            _with_bootstrap_token(common_init_prompt_2, BOOTSTRAP_CONTEXT_TOKEN),
            init_yn=False,
            session_id=session_id,
            required_token=BOOTSTRAP_CONTEXT_TOKEN,
        )
    _, session_id = _run_agent_turn(
        agent_name,
        log_file_path,
        _with_bootstrap_token(prepare_agent_prompt(agent_name, custom_prompt), BOOTSTRAP_ROLE_TOKEN),
        init_yn=False,
        session_id=session_id,
        required_token=BOOTSTRAP_ROLE_TOKEN,
    )
    return agent_name, session_id


def initialize_agents():
    prompts = {OWNER_AGENT_NAME: OWNER_AGENT_INIT_PROMPT}
    prompts.update(REVIEWER_INIT_PROMPTS)
    with ThreadPoolExecutor(max_workers=len(prompts)) as executor:
        futures = [executor.submit(boot_agent, agent_name, prompts[agent_name]) for agent_name in prompts]
        for future in as_completed(futures):
            agent_name, session_id = future.result()
            AGENT_SESSION_ID_DICT[agent_name] = session_id


def run_owner_prompt(prompt, phase_id, phase_round, owner_revision, task_id="", allow_human=False):
    owner_log = _agent_log_file(OWNER_AGENT_NAME)
    owner_msg, owner_session_id = _run_agent_turn(
        OWNER_AGENT_NAME,
        owner_log,
        prompt,
        init_yn=False,
        session_id=AGENT_SESSION_ID_DICT[OWNER_AGENT_NAME],
    )
    AGENT_SESSION_ID_DICT[OWNER_AGENT_NAME] = owner_session_id
    if not allow_human:
        return owner_msg

    msg = owner_msg
    for _ in range(MAX_HUMAN_QA_ROUND):
        question = extract_human_question(msg)
        if not question:
            return msg
        record_checkpoint(
            note="waiting_human",
            phase_id=phase_id,
            phase_round=phase_round,
            owner_revision=owner_revision,
            task_id=task_id,
            waiting_human=True,
            next_action="wait_human",
            prompt=msg,
        )
        human_answer = ask_human(f"{OWNER_AGENT_NAME} 提问: {question}", owner_log)
        followup_prompt = f"""
你刚刚使用触发词 {HUMAN_QUESTION_TRIGGER} 向人类提出了以下问题:
{question}

人类输入如下:
{human_answer}

请你基于这条人类输入继续推进当前阶段。
要求:
1) 将新的澄清结论同步回 `{_abs_path(REQUIREMENT_CLARIFICATION_MD)}`;
2) 如仍需澄清，一次只允许再提出一个关键问题，并继续使用 `{HUMAN_QUESTION_TRIGGER}`;
3) 如果问题已经澄清完成，继续更新当前阶段产物并返回简要摘要。
"""
        msg, owner_session_id = _run_agent_turn(
            OWNER_AGENT_NAME,
            owner_log,
            followup_prompt,
            init_yn=False,
            session_id=AGENT_SESSION_ID_DICT[OWNER_AGENT_NAME],
        )
        AGENT_SESSION_ID_DICT[OWNER_AGENT_NAME] = owner_session_id
        append_clarification_record(question, human_answer, msg, owner_log)

    raise RuntimeError(f"owner 连续提问超过 {MAX_HUMAN_QA_ROUND} 轮，已停止。")


def _ensure_artifacts_ready(required_paths):
    error = _artifacts_readiness_error(required_paths)
    if error:
        raise RuntimeError(error)


def _artifacts_readiness_error(required_paths):
    missing = []
    empty = []
    for artifact_path in required_paths:
        path = Path(artifact_path)
        if not path.exists():
            missing.append(str(path))
            continue
        if path.is_file() and path.stat().st_size == 0:
            empty.append(str(path))
    if missing or empty:
        return f"阶段产物未准备好。缺失: {missing}; 空文件: {empty}"
    return ""


def _normalize_completion_line(line):
    text = str(line or "").strip()
    return re.sub(r"^(?:[-*•]\s*)?", "", text).strip()


def _has_completion_token(owner_summary, completion_token):
    if not completion_token:
        return True

    lines = [line.strip() for line in str(owner_summary or "").splitlines() if line.strip()]
    if not lines:
        return False

    for line in reversed(lines):
        normalized = _normalize_completion_line(line)
        if normalized == completion_token:
            return True
        if normalized.endswith(completion_token):
            prefix = normalized[:-len(completion_token)].rstrip()
            if not prefix:
                return True
            if prefix[-1] in "。.!?！？；;：:)]）】》」』”’\"'":
                return True
        break
    return False


def _owner_completion_issues(owner_summary, required_artifacts, completion_token):
    issues = []
    if completion_token and not _has_completion_token(owner_summary, completion_token):
        issues.append(f"回复缺少完成标记 `{completion_token}`。")
    artifact_error = _artifacts_readiness_error(required_artifacts)
    if artifact_error:
        issues.append(artifact_error)
    return issues


def build_owner_completion_retry_prompt(
        *,
        phase_id,
        required_artifacts,
        prior_summary,
        completion_issues,
        completion_token,
        task_id="",
):
    title = PHASE_TITLES.get(phase_id, phase_id)
    artifact_lines = "\n".join(f"- `{path}`" for path in required_artifacts)
    issue_lines = "\n".join(f"- {issue}" for issue in completion_issues)
    task_scope = f"当前 task_id 仍然是 `{task_id}`，禁止切换到别的任务单。" if task_id else "不要切换到下一阶段。"
    return f"""你上一轮针对阶段 `{title}` 的回复还不能结束本轮工作。

上一轮摘要:
{prior_summary}

当前阻塞:
{issue_lines}

请继续完成并满足以下条件后再结束本轮:
1) 实际更新并落盘以下文件:
{artifact_lines}
2) 只有在上述文件已经落盘且本轮工作真正完成后，回复中才允许包含 `{completion_token}`；
3) `{completion_token}` 必须放在回复最后一个非空行，单独一行输出，不要夹在句子中；
4) 先给出简短摘要和剩余风险，再在最后一个非空行单独输出 `{completion_token}`；
5) {task_scope}
"""


def _format_review_contract(phase_id, review_round, artifact_sha, task_id):
    task_id = task_id or "NONE"
    return f"""最终输出必须严格遵循以下格式，禁止额外说明:
<verdict token>
phase_id: {phase_id}
owner_id: {OWNER_AGENT_NAME}
task_id: {task_id}
artifact_sha: {artifact_sha}
review_round: {review_round}
issues_count: <数字>
summary: <一句话总结>
issues:
1. <问题1>
2. <问题2>

其中:
- verdict token 只能是以下四个之一:
  - {REVIEW_VERDICT_PASS}
  - {REVIEW_VERDICT_REVISE}
  - {REVIEW_VERDICT_BLOCKED}
  - {REVIEW_VERDICT_ASK_HUMAN}
- 如果 verdict 是 {REVIEW_VERDICT_PASS}，issues_count 必须为 0，issues 写“无”即可。
- 如果 verdict 不是 {REVIEW_VERDICT_PASS}，issues_count 必须大于 0，并列出具体问题。
"""


def parse_reviewer_response(reviewer_name, text, expected_phase_id, expected_review_round, expected_artifact_sha, expected_task_id):
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    if not lines:
        return {
            "reviewer": reviewer_name,
            "verdict": REVIEW_VERDICT_REVISE,
            "valid": False,
            "raw": text,
            "problems": [f"{reviewer_name} 未返回任何内容"],
        }

    verdict = lines[0]
    if verdict not in REVIEW_VERDICT_TOKENS:
        return {
            "reviewer": reviewer_name,
            "verdict": REVIEW_VERDICT_REVISE,
            "valid": False,
            "raw": text,
            "problems": [f"{reviewer_name} 未按协议输出 verdict token"],
        }

    fields = {}
    for line in lines[1:]:
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        fields[key.strip().lower()] = value.strip()

    problems = []
    if fields.get("phase_id") != expected_phase_id:
        problems.append(f"phase_id 不匹配: {fields.get('phase_id')!r}")
    if fields.get("owner_id") != OWNER_AGENT_NAME:
        problems.append(f"owner_id 不匹配: {fields.get('owner_id')!r}")
    if fields.get("task_id", "NONE") != (expected_task_id or "NONE"):
        problems.append(f"task_id 不匹配: {fields.get('task_id')!r}")
    if fields.get("artifact_sha") != expected_artifact_sha:
        problems.append(f"artifact_sha 不匹配: {fields.get('artifact_sha')!r}")
    if fields.get("review_round") != str(expected_review_round):
        problems.append(f"review_round 不匹配: {fields.get('review_round')!r}")

    return {
        "reviewer": reviewer_name,
        "verdict": verdict if not problems else REVIEW_VERDICT_REVISE,
        "valid": not problems,
        "raw": text,
        "summary": fields.get("summary", ""),
        "issues_count": fields.get("issues_count", ""),
        "problems": problems,
    }


def reviewer_reply_is_complete(text, expected_phase_id, expected_review_round, expected_artifact_sha, expected_task_id):
    parsed = parse_reviewer_response(
        reviewer_name="reviewer",
        text=text,
        expected_phase_id=expected_phase_id,
        expected_review_round=expected_review_round,
        expected_artifact_sha=expected_artifact_sha,
        expected_task_id=expected_task_id,
    )
    if not parsed["valid"]:
        return False

    summary = str(parsed.get("summary", "") or "").strip()
    issues_count_text = str(parsed.get("issues_count", "") or "").strip()
    if not summary or not issues_count_text.isdigit():
        return False

    issues_count = int(issues_count_text)
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    issues_index = next((index for index, line in enumerate(lines) if line.lower().startswith("issues:")), -1)
    if issues_index < 0:
        return False

    issue_payload = []
    inline_issue_text = lines[issues_index].split(":", 1)[1].strip()
    if inline_issue_text:
        issue_payload.append(inline_issue_text)
    issue_payload.extend(lines[issues_index + 1:])
    numbered_issues = [line for line in issue_payload if re.match(r"^\d+\.\s+\S+", line)]

    if parsed["verdict"] == REVIEW_VERDICT_PASS:
        return issues_count == 0 and any(
            item == "无" or re.match(r"^1\.\s+无$", item)
            for item in issue_payload
        )

    return issues_count > 0 and len(numbered_issues) >= min(issues_count, 1)


def format_review_feedback(parsed_results):
    parts = []
    for result in parsed_results:
        parts.append(f"### {result['reviewer']}\n{result['raw']}")
    return "\n\n".join(parts)


def build_reviewer_prompt(reviewer_name, phase_id, review_round, artifact_paths, artifact_sha, owner_summary, task_id="", extra_context=""):
    title = PHASE_TITLES.get(phase_id, phase_id)
    focus = REVIEWER_STAGE_FOCUS[phase_id][reviewer_name]
    artifact_lines = "\n".join(f"- {path}" for path in artifact_paths)
    return f"""当前阶段: {title} ({phase_id})
review_round: {review_round}
artifact_sha: {artifact_sha}
task_id: {task_id or "NONE"}

owner 刚刚提交了本轮产物，摘要如下:
{owner_summary}

你需要审核的文件:
{artifact_lines}

附加上下文:
{extra_context or "无"}

请自行阅读这些文件与当前代码，然后只从 {reviewer_name} 的角度审查。
重点关注:
{focus}

{_format_review_contract(phase_id, review_round, artifact_sha, task_id)}
"""


def run_reviewer_round(phase_id, review_round, artifact_paths, owner_summary, task_id="", extra_context="", include_git_snapshot=False):
    artifact_sha = _artifact_sha(artifact_paths, include_git_snapshot=include_git_snapshot)
    record_checkpoint(
        note="owner_candidate_ready",
        phase_id=phase_id,
        phase_round=review_round,
        task_id=task_id,
        artifact_refs=artifact_paths,
        artifact_sha=artifact_sha,
        next_action="review",
    )

    results = []
    with ThreadPoolExecutor(max_workers=len(REVIEWER_AGENT_NAMES)) as executor:
        future_map = {}
        for reviewer_name in REVIEWER_AGENT_NAMES:
            prompt = build_reviewer_prompt(
                reviewer_name=reviewer_name,
                phase_id=phase_id,
                review_round=review_round,
                artifact_paths=artifact_paths,
                artifact_sha=artifact_sha,
                owner_summary=owner_summary,
                task_id=task_id,
                extra_context=extra_context,
            )
            future = executor.submit(
                _run_agent_turn,
                reviewer_name,
                _agent_log_file(reviewer_name),
                prompt,
                False,
                AGENT_SESSION_ID_DICT[reviewer_name],
                "[[ACX_VERDICT:",
                lambda text, phase_id=phase_id, review_round=review_round, artifact_sha=artifact_sha, task_id=task_id: reviewer_reply_is_complete(
                    text=text,
                    expected_phase_id=phase_id,
                    expected_review_round=review_round,
                    expected_artifact_sha=artifact_sha,
                    expected_task_id=task_id,
                ),
            )
            future_map[future] = reviewer_name

        for future in as_completed(future_map):
            reviewer_name = future_map[future]
            raw_reply, reviewer_session_id = future.result()
            AGENT_SESSION_ID_DICT[reviewer_name] = reviewer_session_id
            parsed = parse_reviewer_response(
                reviewer_name=reviewer_name,
                text=raw_reply,
                expected_phase_id=phase_id,
                expected_review_round=review_round,
                expected_artifact_sha=artifact_sha,
                expected_task_id=task_id,
            )
            results.append(parsed)
            record_checkpoint(
                note="reviewer_verdict_received",
                phase_id=phase_id,
                phase_round=review_round,
                reviewer_id=reviewer_name,
                task_id=task_id,
                artifact_refs=artifact_paths,
                artifact_sha=artifact_sha,
                verdict=parsed["verdict"],
                next_action="aggregate_review",
                prompt=raw_reply,
            )

    results.sort(key=lambda item: item["reviewer"])
    verdicts = [result["verdict"] for result in results]
    if results and all(verdict == REVIEW_VERDICT_PASS for verdict in verdicts):
        status = "approved"
    elif any(verdict == REVIEW_VERDICT_ASK_HUMAN for verdict in verdicts):
        status = "ask_human"
    elif any(verdict == REVIEW_VERDICT_BLOCKED for verdict in verdicts):
        status = "blocked"
    else:
        status = "revise"

    return {
        "status": status,
        "artifact_sha": artifact_sha,
        "results": results,
        "feedback": format_review_feedback(results),
    }


def build_requirement_prompt():
    return f"""当前进入步骤 1/4: 需求指定。
原始需求如下:
{requirement_str}

请先阅读当前代码与已有文档，然后完成以下工作:
1) 编写并更新 `{_abs_path(requirement_spec_md)}`;
2) 文档中必须显式给出 requirement_id（例如 R1、R2）、背景与目标、范围内、范围外、关键场景、功能要求、非功能约束、风险、验收标准;
3) 如果你发现关键信息缺失，可以使用 `{HUMAN_QUESTION_TRIGGER}` 一次只提一个关键问题;
4) 如果存在人类澄清，必须同步维护 `{_abs_path(REQUIREMENT_CLARIFICATION_MD)}`;
5) 只有在 `{_abs_path(requirement_spec_md)}` 与 `{_abs_path(REQUIREMENT_CLARIFICATION_MD)}` 已按需落盘后，才允许在回复中包含 `{OWNER_STAGE_DONE_TOKEN}`;
6) 先返回简短摘要，说明本轮明确了哪些 requirement_id、仍有哪些风险；
7) `{OWNER_STAGE_DONE_TOKEN}` 必须放在回复最后一个非空行，单独一行输出，不要夹在句子中。
"""


def build_design_prompt():
    return f"""当前进入步骤 2/4: 详细设计。
请基于以下输入:
- `{_abs_path(requirement_spec_md)}`
- `{_abs_path(REQUIREMENT_CLARIFICATION_MD)}`

完成以下输出:
1) 更新 `{_abs_path(design_md)}`;
2) 更新 `{_abs_path(design_trace_json)}`;

要求:
1) `02_design.md` 必须覆盖架构方案、涉及模块/文件、关键数据流或调用流、状态与异常处理、回滚/兼容性、日志与可观测性、测试策略;
2) `02_design_trace.json` 必须至少包含 `requirement_id -> design_section -> impacted_modules` 的映射;
3) 设计不得越过需求边界，不得引入未批准范围;
4) 只有在 `{_abs_path(design_md)}` 与 `{_abs_path(design_trace_json)}` 已落盘后，才允许在回复中包含 `{OWNER_STAGE_DONE_TOKEN}`;
5) 先返回本轮设计摘要、关键设计决策和剩余风险；
6) `{OWNER_STAGE_DONE_TOKEN}` 必须放在回复最后一个非空行，单独一行输出，不要夹在句子中。
"""


def build_task_planning_prompt():
    return f"""当前进入步骤 3/4: 任务规划。
请基于以下输入:
- `{_abs_path(requirement_spec_md)}`
- `{_abs_path(REQUIREMENT_CLARIFICATION_MD)}`
- `{_abs_path(design_md)}`
- `{_abs_path(design_trace_json)}`

完成以下输出:
1) 更新 `{_abs_path(task_md)}`
2) 更新 `{_abs_path(task_schedule_json)}`
3) 更新 `{_abs_path(test_plan_md)}`

要求:
{TASK_DOC_STRUCTURE_PROMPT}

额外要求:
1) `{task_schedule_json}` 是后续开发阶段唯一机器调度真值源;
2) `{test_plan_md}` 必须形成“任务单 -> 测试策略/验证命令/回归说明”的对照表;
3) 只有在 `{_abs_path(task_md)}`、`{_abs_path(task_schedule_json)}`、`{_abs_path(test_plan_md)}` 都已落盘后，才允许在回复中包含 `{OWNER_STAGE_DONE_TOKEN}`;
4) 先返回本轮任务规划摘要，说明里程碑数量、任务单数量和关键验证策略；
5) `{OWNER_STAGE_DONE_TOKEN}` 必须放在回复最后一个非空行，单独一行输出，不要夹在句子中。
"""


def build_revision_prompt(phase_id, review_round, artifact_paths, review_feedback):
    title = PHASE_TITLES.get(phase_id, phase_id)
    artifact_lines = "\n".join(f"- {path}" for path in artifact_paths)
    return f"""你提交的阶段 `{title}` 未通过 reviewer gate。
请阅读以下 reviewer 反馈，逐项修复并更新对应产物:

{review_feedback}

必须更新的文件:
{artifact_lines}

要求:
1) 不要回避 reviewer 指出的问题;
2) 如果确实需要新的人工澄清，可以使用 `{HUMAN_QUESTION_TRIGGER}` 一次只提出一个关键问题;
3) 只有在上述文件已经更新落盘后，回复中才允许包含 `{OWNER_STAGE_DONE_TOKEN}`;
4) 先返回本轮修复摘要，并明确说明还剩哪些风险（如无则写“无”）；
5) `{OWNER_STAGE_DONE_TOKEN}` 必须放在回复最后一个非空行，单独一行输出，不要夹在句子中。

当前 review_round 为 {review_round}，这是一个 revision round，不要切换到下一阶段。
"""


def execute_document_stage(phase_id, build_initial_prompt, required_artifacts, optional_artifacts=None, allow_human=False):
    title = PHASE_TITLES[phase_id]
    phase_round = 1
    owner_revision = 0
    prompt = build_initial_prompt()
    record_checkpoint(
        note="phase_assigned",
        phase_id=phase_id,
        phase_round=phase_round,
        next_action="owner_work",
        prompt=prompt,
    )
    owner_summary = run_owner_prompt(
        prompt=prompt,
        phase_id=phase_id,
        phase_round=phase_round,
        owner_revision=owner_revision,
        allow_human=allow_human,
    )

    while True:
        completion_issues = _owner_completion_issues(
            owner_summary=owner_summary,
            required_artifacts=required_artifacts,
            completion_token=OWNER_STAGE_DONE_TOKEN,
        )
        if completion_issues:
            if phase_round >= MAX_STAGE_REVIEW_ROUNDS:
                raise RuntimeError(f"{title} 连续 {MAX_STAGE_REVIEW_ROUNDS} 轮仍未完成有效落盘。")

            owner_revision += 1
            phase_round += 1
            prompt = build_owner_completion_retry_prompt(
                phase_id=phase_id,
                required_artifacts=required_artifacts,
                prior_summary=owner_summary,
                completion_issues=completion_issues,
                completion_token=OWNER_STAGE_DONE_TOKEN,
            )
            record_checkpoint(
                note="owner_completion_retry",
                phase_id=phase_id,
                phase_round=phase_round,
                owner_revision=owner_revision,
                artifact_refs=required_artifacts,
                next_action="owner_work",
                prompt=prompt,
            )
            owner_summary = run_owner_prompt(
                prompt=prompt,
                phase_id=phase_id,
                phase_round=phase_round,
                owner_revision=owner_revision,
                allow_human=allow_human,
            )
            continue

        artifact_paths = list(required_artifacts)
        for artifact in optional_artifacts or []:
            if Path(artifact).exists():
                artifact_paths.append(artifact)
        review_result = run_reviewer_round(
            phase_id=phase_id,
            review_round=phase_round,
            artifact_paths=artifact_paths,
            owner_summary=owner_summary,
            include_git_snapshot=False,
        )
        if review_result["status"] == "approved":
            record_checkpoint(
                note="phase_closed",
                phase_id=phase_id,
                phase_round=phase_round,
                artifact_refs=artifact_paths,
                artifact_sha=review_result["artifact_sha"],
                verdict=REVIEW_PASS_TOKEN,
                next_action="next_phase",
            )
            return owner_summary

        if phase_round >= MAX_STAGE_REVIEW_ROUNDS:
            raise RuntimeError(f"{title} 连续 {MAX_STAGE_REVIEW_ROUNDS} 轮未通过 reviewer gate。")

        owner_revision += 1
        phase_round += 1
        prompt = build_revision_prompt(
            phase_id=phase_id,
            review_round=phase_round,
            artifact_paths=artifact_paths,
            review_feedback=review_result["feedback"],
        )
        record_checkpoint(
            note="owner_revision_ready",
            phase_id=phase_id,
            phase_round=phase_round,
            owner_revision=owner_revision,
            artifact_refs=artifact_paths,
            artifact_sha=review_result["artifact_sha"],
            next_action=review_result["status"],
            prompt=prompt,
        )
        owner_summary = run_owner_prompt(
            prompt=prompt,
            phase_id=phase_id,
            phase_round=phase_round,
            owner_revision=owner_revision,
            allow_human=allow_human or review_result["status"] == "ask_human",
        )


def load_task_schedule():
    schedule_path = _abs_path(task_schedule_json)
    if not schedule_path.exists():
        raise RuntimeError(f"任务计划 JSON 不存在: {schedule_path}")
    return json.loads(schedule_path.read_text(encoding="utf-8"))


def find_next_unfinished_task():
    schedule = load_task_schedule()
    for milestone in schedule.get("milestones", []):
        for task in milestone.get("tasks", []):
            status = str(task.get("status", "todo")).strip().lower()
            if status in TASK_STATUS_DONE:
                continue
            return {
                "milestone_id": str(milestone.get("milestone_id", "")).strip(),
                "milestone_title": str(milestone.get("title", "")).strip(),
                "task_id": str(task.get("task_id", "")).strip(),
                "title": str(task.get("title", "")).strip(),
                "objective": str(task.get("objective", "")).strip(),
                "files": task.get("files", []),
                "done_criteria": task.get("done_criteria", []),
                "verification": task.get("verification", []),
            }
    return None


def is_task_done(task_id):
    schedule = load_task_schedule()
    for milestone in schedule.get("milestones", []):
        for task in milestone.get("tasks", []):
            if str(task.get("task_id", "")).strip() == task_id:
                return str(task.get("status", "todo")).strip().lower() in TASK_STATUS_DONE
    return False


def is_task_checked_in_markdown(task_id):
    markdown_path = _abs_path(task_md)
    if not markdown_path.exists():
        return False
    pattern = re.compile(TASK_CHECKED_RE_TEMPLATE.format(task_id=re.escape(task_id)))
    return any(pattern.search(line) for line in markdown_path.read_text(encoding="utf-8").splitlines())


def _development_post_review_sync_issues(task_id):
    issues = []
    if not is_task_done(task_id):
        issues.append(f"`{task_schedule_json}` 中 task_id={task_id} 仍未标记 done。")
    if not is_task_checked_in_markdown(task_id):
        issues.append(f"`{task_md}` 中 task_id={task_id} 仍未勾选。")
    return issues


def task_run_report_path(task_id):
    return RUN_REPORTS_DIR / f"run_{task_id}.json"


def build_development_prompt(task_info):
    run_report = task_run_report_path(task_info["task_id"])
    _ensure_parent(run_report)
    return f"""当前进入步骤 4/4: 开发与测试。
请只处理以下任务单，不要跨任务单越界开发:
- milestone_id: {task_info['milestone_id']}
- milestone_title: {task_info['milestone_title']}
- task_id: {task_info['task_id']}
- task_title: {task_info['title']}
- objective: {task_info['objective']}
- files: {task_info['files']}
- done_criteria: {task_info['done_criteria']}
- verification: {task_info['verification']}

请先阅读:
- `{_abs_path(requirement_spec_md)}`
- `{_abs_path(REQUIREMENT_CLARIFICATION_MD)}`
- `{_abs_path(design_md)}`
- `{_abs_path(design_trace_json)}`
- `{_abs_path(task_md)}`
- `{_abs_path(task_schedule_json)}`
- `{_abs_path(test_plan_md)}`

然后完成:
1) 仅实现当前 task_id 对应代码与必要测试;
2) 运行与当前 task_id 相关的验证和测试;
3) 更新 `{_abs_path(task_md)}` 与 `{_abs_path(task_schedule_json)}` 中当前任务单状态;
4) 更新 `{_abs_path(test_plan_md)}` 的实际测试结果;
5) 更新 `{_abs_path(delivery_report_md)}` 的交付摘要;
6) 写入 `{run_report}`，至少包含 task_id、changed_files、commands、results、risks、updated_at;
7) 只有在以上改动和文档都已落盘后，回复中才允许包含 `{OWNER_STAGE_DONE_TOKEN}`;
8) `{OWNER_STAGE_DONE_TOKEN}` 必须放在回复最后一个非空行，单独一行输出，不要夹在句子中。

完成后返回:
1) task_id
2) 主要修改文件
3) 执行的测试/验证
4) 结果与剩余风险
"""


def build_development_revision_prompt(task_info, review_feedback):
    run_report = task_run_report_path(task_info["task_id"])
    return f"""当前任务 `{task_info['task_id']}` 未通过 reviewer gate。
请阅读以下反馈，仅修复当前任务单，不要切换到下一个任务单:

{review_feedback}

修复后必须同步更新:
- `{_abs_path(task_md)}`
- `{_abs_path(task_schedule_json)}`
- `{_abs_path(test_plan_md)}`
- `{_abs_path(delivery_report_md)}`
- `{run_report}`

然后重新执行必要的测试和验证；只有在以上文件都更新落盘后，回复中才允许包含 `{OWNER_STAGE_DONE_TOKEN}`。
请先返回本轮修复摘要，再在最后一个非空行单独输出 `{OWNER_STAGE_DONE_TOKEN}`。
"""


def build_delivery_closeout_prompt():
    return f"""所有任务单都已开发完成，请进行开发与测试阶段总复核。
请完成以下工作:
1) 再次检查 `{_abs_path(task_schedule_json)}` 中是否所有 task.status 都为 done;
2) 再次检查 `{_abs_path(task_md)}` 中是否所有任务单都已勾选;
3) 完整更新 `{_abs_path(delivery_report_md)}`，写清已完成任务、主要改动模块、测试覆盖、回归结论、剩余风险;
4) 如有必要，同步修正 `{_abs_path(test_plan_md)}`。

完成后先返回总复核摘要；如果确实已经全部完成，请在最后一个非空行单独输出 `{DEVELOPMENT_DONE_TOKEN}`。
"""


def execute_development_stage():
    phase_id = "development_testing"
    task_retry_guard = {}

    while True:
        task_info = find_next_unfinished_task()
        if not task_info:
            break

        task_id = task_info["task_id"]
        task_retry_guard[task_id] = task_retry_guard.get(task_id, 0) + 1
        if task_retry_guard[task_id] > MAX_STAGE_REVIEW_ROUNDS:
            raise RuntimeError(f"任务 {task_id} 连续返工超过 {MAX_STAGE_REVIEW_ROUNDS} 次。")

        phase_round = 1
        task_prompt = build_development_prompt(task_info)
        record_checkpoint(
            note="phase_assigned",
            phase_id=phase_id,
            phase_round=phase_round,
            task_id=task_id,
            next_action="owner_work",
            prompt=task_prompt,
        )
        owner_summary = run_owner_prompt(
            prompt=task_prompt,
            phase_id=phase_id,
            phase_round=phase_round,
            owner_revision=0,
            task_id=task_id,
            allow_human=True,
        )

        while True:
            required_artifacts = [
                _abs_path(task_md),
                _abs_path(task_schedule_json),
                _abs_path(test_plan_md),
                _abs_path(delivery_report_md),
                task_run_report_path(task_id),
            ]
            completion_issues = _owner_completion_issues(
                owner_summary=owner_summary,
                required_artifacts=required_artifacts,
                completion_token=OWNER_STAGE_DONE_TOKEN,
            )
            if completion_issues:
                if phase_round >= MAX_STAGE_REVIEW_ROUNDS:
                    raise RuntimeError(f"任务 {task_id} 连续 {MAX_STAGE_REVIEW_ROUNDS} 轮仍未完成有效落盘。")

                phase_round += 1
                completion_prompt = build_owner_completion_retry_prompt(
                    phase_id=phase_id,
                    required_artifacts=required_artifacts,
                    prior_summary=owner_summary,
                    completion_issues=completion_issues,
                    completion_token=OWNER_STAGE_DONE_TOKEN,
                    task_id=task_id,
                )
                record_checkpoint(
                    note="owner_completion_retry",
                    phase_id=phase_id,
                    phase_round=phase_round,
                    task_id=task_id,
                    artifact_refs=required_artifacts,
                    next_action="owner_work",
                    prompt=completion_prompt,
                )
                owner_summary = run_owner_prompt(
                    prompt=completion_prompt,
                    phase_id=phase_id,
                    phase_round=phase_round,
                    owner_revision=phase_round - 1,
                    task_id=task_id,
                    allow_human=True,
                )
                continue

            artifact_paths = required_artifacts + [
                _abs_path(requirement_spec_md),
                _abs_path(design_md),
                _abs_path(design_trace_json),
            ]
            review_result = run_reviewer_round(
                phase_id=phase_id,
                review_round=phase_round,
                artifact_paths=artifact_paths,
                owner_summary=owner_summary,
                task_id=task_id,
                extra_context=f"当前 reviewer 只审 task_id={task_id} 的本轮实现与测试结果。",
                include_git_snapshot=True,
            )
            if review_result["status"] == "approved":
                sync_issues = _development_post_review_sync_issues(task_id)
                if sync_issues:
                    if phase_round >= MAX_STAGE_REVIEW_ROUNDS:
                        raise RuntimeError(f"任务 {task_id} 连续 {MAX_STAGE_REVIEW_ROUNDS} 轮仍未完成状态同步。")
                    phase_round += 1
                    completion_prompt = build_owner_completion_retry_prompt(
                        phase_id=phase_id,
                        required_artifacts=required_artifacts,
                        prior_summary=owner_summary,
                        completion_issues=sync_issues,
                        completion_token=OWNER_STAGE_DONE_TOKEN,
                        task_id=task_id,
                    )
                    record_checkpoint(
                        note="owner_completion_retry",
                        phase_id=phase_id,
                        phase_round=phase_round,
                        task_id=task_id,
                        artifact_refs=artifact_paths,
                        artifact_sha=review_result["artifact_sha"],
                        next_action="owner_work",
                        prompt=completion_prompt,
                    )
                    owner_summary = run_owner_prompt(
                        prompt=completion_prompt,
                        phase_id=phase_id,
                        phase_round=phase_round,
                        owner_revision=phase_round - 1,
                        task_id=task_id,
                        allow_human=True,
                    )
                    continue
                record_checkpoint(
                    note="phase_closed",
                    phase_id=phase_id,
                    phase_round=phase_round,
                    task_id=task_id,
                    artifact_refs=artifact_paths,
                    artifact_sha=review_result["artifact_sha"],
                    verdict=REVIEW_PASS_TOKEN,
                    next_action="next_task",
                )
                break

            if phase_round >= MAX_STAGE_REVIEW_ROUNDS:
                raise RuntimeError(f"任务 {task_id} 连续 {MAX_STAGE_REVIEW_ROUNDS} 轮未通过 reviewer gate。")

            phase_round += 1
            revision_prompt = build_development_revision_prompt(task_info, review_result["feedback"])
            record_checkpoint(
                note="owner_revision_ready",
                phase_id=phase_id,
                phase_round=phase_round,
                task_id=task_id,
                artifact_refs=artifact_paths,
                artifact_sha=review_result["artifact_sha"],
                next_action=review_result["status"],
                prompt=revision_prompt,
            )
            owner_summary = run_owner_prompt(
                prompt=revision_prompt,
                phase_id=phase_id,
                phase_round=phase_round,
                owner_revision=phase_round - 1,
                task_id=task_id,
                allow_human=True,
            )

    phase_round = 1
    closeout_prompt = build_delivery_closeout_prompt()
    record_checkpoint(
        note="phase_assigned",
        phase_id=phase_id,
        phase_round=phase_round,
        task_id="ALL",
        next_action="owner_work",
        prompt=closeout_prompt,
    )
    owner_summary = run_owner_prompt(
        prompt=closeout_prompt,
        phase_id=phase_id,
        phase_round=phase_round,
        owner_revision=0,
        task_id="ALL",
        allow_human=True,
    )
    if not _has_completion_token(owner_summary, DEVELOPMENT_DONE_TOKEN):
        raise RuntimeError("owner 未在总复核回复中声明开发完成标记。")

    required_artifacts = [
        _abs_path(task_md),
        _abs_path(task_schedule_json),
        _abs_path(test_plan_md),
        _abs_path(delivery_report_md),
    ]
    while True:
        _ensure_artifacts_ready(required_artifacts)
        review_result = run_reviewer_round(
            phase_id=phase_id,
            review_round=phase_round,
            artifact_paths=required_artifacts,
            owner_summary=owner_summary,
            task_id="ALL",
            extra_context="当前 reviewer 正在对开发与测试阶段做终审，请关注整体交付完整性和遗漏项。",
            include_git_snapshot=True,
        )
        if review_result["status"] == "approved":
            record_checkpoint(
                note="phase_closed",
                phase_id=phase_id,
                phase_round=phase_round,
                task_id="ALL",
                artifact_refs=required_artifacts,
                artifact_sha=review_result["artifact_sha"],
                verdict=REVIEW_PASS_TOKEN,
                next_action="workflow_completed",
            )
            return

        if phase_round >= MAX_STAGE_REVIEW_ROUNDS:
            raise RuntimeError("开发与测试阶段总复核连续未通过 reviewer gate。")

        phase_round += 1
        revision_prompt = build_revision_prompt(
            phase_id=phase_id,
            review_round=phase_round,
            artifact_paths=required_artifacts,
            review_feedback=review_result["feedback"],
        )
        owner_summary = run_owner_prompt(
            prompt=revision_prompt,
            phase_id=phase_id,
            phase_round=phase_round,
            owner_revision=phase_round - 1,
            task_id="ALL",
            allow_human=True,
        )


def main():
    RUN_REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    record_checkpoint(
        note="workflow_bootstrap",
        phase_id="bootstrap",
        next_action="initialize_agents",
    )
    try:
        initialize_agents()
        record_checkpoint(
            note="agents_ready",
            phase_id="bootstrap",
            next_action="requirement_specification",
        )

        execute_document_stage(
            phase_id="requirement_specification",
            build_initial_prompt=build_requirement_prompt,
            required_artifacts=[_abs_path(requirement_spec_md)],
            optional_artifacts=[_abs_path(REQUIREMENT_CLARIFICATION_MD)],
            allow_human=True,
        )
        execute_document_stage(
            phase_id="detailed_design",
            build_initial_prompt=build_design_prompt,
            required_artifacts=[_abs_path(design_md), _abs_path(design_trace_json)],
            optional_artifacts=[_abs_path(requirement_spec_md), _abs_path(REQUIREMENT_CLARIFICATION_MD)],
            allow_human=True,
        )
        execute_document_stage(
            phase_id="task_planning",
            build_initial_prompt=build_task_planning_prompt,
            required_artifacts=[_abs_path(task_md), _abs_path(task_schedule_json), _abs_path(test_plan_md)],
            optional_artifacts=[
                _abs_path(requirement_spec_md),
                _abs_path(REQUIREMENT_CLARIFICATION_MD),
                _abs_path(design_md),
                _abs_path(design_trace_json),
            ],
            allow_human=True,
        )
        execute_development_stage()
        record_checkpoint(
            note="workflow_completed",
            phase_id="development_testing",
            next_action="done",
        )
    except Exception as error:
        record_checkpoint(
            note="workflow_failed",
            phase_id="failed",
            loop_guard_reason=str(error),
            next_action="manual_intervention",
        )
        raise


if __name__ == "__main__":
    main()
