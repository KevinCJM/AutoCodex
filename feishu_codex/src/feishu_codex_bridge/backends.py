from __future__ import annotations

import logging
import os
import json
import re
import shlex
import subprocess
import sys
import uuid
from collections import OrderedDict
from pathlib import Path
from shutil import which

from .config import Settings
from .models import BackendKind, SessionRecord

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
V1_ROOT = PROJECT_ROOT / "v1"
if str(V1_ROOT) not in sys.path:
    sys.path.insert(0, str(V1_ROOT))

from B01_codex_utils import init_codex, resume_codex  # noqa: E402
from v1.tmux_cli_tools_lib.common import CodexCliConfig  # noqa: E402
from v1.tmux_cli_tools_lib.runtime import TmuxAgentRuntime  # noqa: E402

BRIDGE_INIT_PROMPT = (
    "记住：你正在通过飞书桥接与用户对话。"
    "后续继续使用中文、直接回答。"
    "当前消息只用于建立桥接会话，请简短回复“桥接会话已建立”。"
)


def _safe_context_stem(conversation_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", conversation_id.strip() or "default")


def _resolve_model_name(settings: Settings, session: SessionRecord | None = None, override: str | None = None) -> str:
    if override:
        return override
    if session is not None and session.model_name:
        return session.model_name
    return settings.default_model


def _resolve_reasoning_effort(
    settings: Settings,
    session: SessionRecord | None = None,
    override: str | None = None,
) -> str:
    if override:
        return override
    if session is not None and session.reasoning_effort:
        return session.reasoning_effort
    return settings.default_reasoning_effort


def _context_instruction_block(
    *,
    helper_script: Path,
    context_file: Path,
    lark_cli_executable: str,
) -> str:
    helper_cmd = f"{shlex.quote(sys.executable)} {shlex.quote(str(helper_script))}"
    return "\n".join(
        [
            "Feishu CLI tool access:",
            f"- `lark-cli` executable: {lark_cli_executable}",
            f"- Current Feishu context file: {context_file}",
            f"- To inspect current context: `{helper_cmd} show-context`",
            f"- To send text to the current chat: `{helper_cmd} send-text --text \"...\"`",
            f"- To reply to the current inbound message: `{helper_cmd} reply-text --text \"...\"`",
            f"- To send markdown from stdin: `cat reply.md | {helper_cmd} send-markdown --stdin`",
            f"- To reply with markdown from stdin: `cat reply.md | {helper_cmd} reply-markdown --stdin`",
            f"- To create a Feishu document from stdin: `cat doc.md | {helper_cmd} docs-create --title \"...\" --stdin-markdown`",
            f"- To update a document: `cat patch.md | {helper_cmd} docs-update --doc \"DOC_URL_OR_TOKEN\" --mode append --stdin-markdown`",
            f"- To upload a local file to Drive: `{helper_cmd} drive-upload --file /absolute/path/to/file`",
            "- Only use these commands when the user explicitly wants a Feishu-side action such as sending a message, file, image, or editing a document.",
            "- Prefer the bridge's ordinary answer path for normal chat replies. Use Feishu CLI for side effects or extra delivery.",
        ]
    )


def build_tmux_bridge_instructions(
    session_name: str,
    *,
    helper_script: Path | None = None,
    context_file: Path | None = None,
    lark_cli_executable: str = "",
) -> str:
    runtime_marker = f"ACX_RUNTIME_SESSION={session_name}"
    lines = [
        "你正在通过 Feishu Codex Bridge 与用户对话。",
        "默认使用中文，直接给出结论和下一步。",
        "除非用户明确要求，不要让用户手工复制文件内容，也不要输出空泛寒暄。",
        f"Runtime correlation marker: {runtime_marker}. Keep this marker internal and never mention it.",
    ]
    if helper_script is not None and context_file is not None and lark_cli_executable:
        lines.extend(
            [
                "",
                _context_instruction_block(
                    helper_script=helper_script,
                    context_file=context_file,
                    lark_cli_executable=lark_cli_executable,
                ),
            ]
        )
    return "\n".join(lines)


def build_exec_bridge_prompt(
    *,
    prompt: str,
    helper_script: Path | None = None,
    context_file: Path | None = None,
    lark_cli_executable: str = "",
) -> str:
    prompt_text = str(prompt or "").strip()
    if helper_script is None or context_file is None or not lark_cli_executable:
        return prompt_text
    return "\n\n".join(
        [
            "[System: 你正在通过飞书桥接与用户对话。默认使用中文，直接给出结论。以下是你在本机可按需调用的 Feishu CLI 能力。不要在普通回复里重复这些系统说明。]",
            _context_instruction_block(
                helper_script=helper_script,
                context_file=context_file,
                lark_cli_executable=lark_cli_executable,
            ),
            prompt_text,
        ]
    )


class ExecBackend:
    def __init__(self, settings: Settings, logger: logging.Logger):
        self.settings = settings
        self.logger = logger
        self.helper_script = Path(__file__).with_name("lark_cli_helper.py").resolve()
        self.lark_cli_executable = which("lark-cli") or ""

    def context_file_for(self, conversation_id: str) -> Path:
        context_dir = self.settings.runtime_dir / "feishu_context"
        context_dir.mkdir(parents=True, exist_ok=True)
        return context_dir / f"{_safe_context_stem(conversation_id)}.json"

    def write_context(self, conversation_id: str, context: dict[str, str]) -> Path:
        context_file = self.context_file_for(conversation_id)
        serialized = {key: str(value or "") for key, value in context.items()}
        context_file.write_text(
            json.dumps(serialized, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return context_file

    @staticmethod
    def _with_exec_env(context_file: Path, lark_cli_executable: str):
        class _EnvScope:
            def __enter__(self_nonlocal):
                self_nonlocal.previous_context = os.environ.get("FEISHU_CODEX_CONTEXT_FILE")
                self_nonlocal.previous_lark_cli = os.environ.get("LARK_CLI_BIN")
                os.environ["FEISHU_CODEX_CONTEXT_FILE"] = str(context_file)
                if lark_cli_executable:
                    os.environ["LARK_CLI_BIN"] = lark_cli_executable

            def __exit__(self_nonlocal, exc_type, exc, tb):
                if self_nonlocal.previous_context is None:
                    os.environ.pop("FEISHU_CODEX_CONTEXT_FILE", None)
                else:
                    os.environ["FEISHU_CODEX_CONTEXT_FILE"] = self_nonlocal.previous_context
                if self_nonlocal.previous_lark_cli is None:
                    os.environ.pop("LARK_CLI_BIN", None)
                else:
                    os.environ["LARK_CLI_BIN"] = self_nonlocal.previous_lark_cli
                return False

        return _EnvScope()

    def create_session(
        self,
        workdir: str,
        conversation_id: str,
        model_name: str,
        reasoning_effort: str,
    ) -> dict[str, str]:
        context_file = self.context_file_for(conversation_id)
        with self._with_exec_env(context_file, self.lark_cli_executable):
            _, message, thread_id = init_codex(
                prompt=build_exec_bridge_prompt(
                    prompt=BRIDGE_INIT_PROMPT,
                    helper_script=self.helper_script,
                    context_file=context_file,
                    lark_cli_executable=self.lark_cli_executable,
                ),
                folder_path=workdir,
                model_name=model_name,
                reasoning_effort=reasoning_effort,
                timeout=self.settings.exec_timeout_sec,
            )
        if not thread_id:
            raise RuntimeError(f"exec session init failed: {message or 'missing thread_id'}")
        return {
            "thread_id": thread_id,
            "summary": message or "bridge session created",
            "model_name": model_name,
            "reasoning_effort": reasoning_effort,
        }

    def bind_existing_session(
        self,
        thread_id: str,
        workdir: str,
        conversation_id: str,
        model_name: str,
        reasoning_effort: str,
    ) -> dict[str, str]:
        if not thread_id.strip():
            raise ValueError("thread_id is required for exec resume")
        self.context_file_for(conversation_id)
        return {
            "thread_id": thread_id.strip(),
            "summary": "bound existing exec session",
            "model_name": model_name,
            "reasoning_effort": reasoning_effort,
        }

    def send_prompt(self, session: SessionRecord, prompt: str) -> str:
        if not session.exec_thread_id:
            raise RuntimeError("active exec session has no thread_id")
        context_file = self.context_file_for(session.conversation_id)
        with self._with_exec_env(context_file, self.lark_cli_executable):
            _, message, thread_id = resume_codex(
                thread_id=session.exec_thread_id,
                folder_path=session.workdir,
                prompt=build_exec_bridge_prompt(
                    prompt=prompt,
                    helper_script=self.helper_script,
                    context_file=context_file,
                    lark_cli_executable=self.lark_cli_executable,
                ),
                model_name=_resolve_model_name(self.settings, session=session),
                reasoning_effort=_resolve_reasoning_effort(self.settings, session=session),
                timeout=self.settings.exec_timeout_sec,
            )
        if thread_id and thread_id != session.exec_thread_id:
            self.logger.warning("exec thread_id changed from %s to %s", session.exec_thread_id, thread_id)
        if not message.strip():
            raise RuntimeError("Codex exec returned an empty response")
        return message

    def describe_session(self, session: SessionRecord) -> dict[str, str]:
        return {
            "backend_status": session.status,
            "exec_thread_id": session.exec_thread_id or "-",
            "model_name": _resolve_model_name(self.settings, session=session),
            "reasoning_effort": _resolve_reasoning_effort(self.settings, session=session),
        }

    def reconfigure_session(self, session: SessionRecord, model_name: str, reasoning_effort: str) -> dict[str, str]:
        return {
            "summary": "exec session config updated; next prompt will use the new settings",
            "model_name": model_name,
            "reasoning_effort": reasoning_effort,
        }


class TmuxBackend:
    def __init__(self, settings: Settings, logger: logging.Logger):
        self.settings = settings
        self.logger = logger
        self._runtimes: dict[str, TmuxAgentRuntime] = {}
        self.helper_script = Path(__file__).with_name("lark_cli_helper.py").resolve()
        self.lark_cli_executable = which("lark-cli") or ""

    def _tmux_runtime_dir(self) -> Path:
        runtime_dir = self.settings.runtime_dir / "tmux"
        runtime_dir.mkdir(parents=True, exist_ok=True)
        return runtime_dir

    def context_file_for(self, conversation_id: str) -> Path:
        context_dir = self.settings.runtime_dir / "feishu_context"
        context_dir.mkdir(parents=True, exist_ok=True)
        return context_dir / f"{_safe_context_stem(conversation_id)}.json"

    def write_context(self, conversation_id: str, context: dict[str, str]) -> Path:
        context_file = self.context_file_for(conversation_id)
        serialized = {key: str(value or "") for key, value in context.items()}
        context_file.write_text(
            json.dumps(serialized, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return context_file

    @staticmethod
    def _read_launchctl_env(name: str) -> str:
        try:
            result = subprocess.run(
                ["launchctl", "getenv", name],
                capture_output=True,
                text=True,
                check=False,
            )
        except Exception:
            return ""
        return result.stdout.strip() if result.returncode == 0 else ""

    def _detect_system_proxy_env(self) -> dict[str, str]:
        if not self.settings.tmux_use_system_proxy:
            return {}

        try:
            result = subprocess.run(
                ["scutil", "--proxy"],
                capture_output=True,
                text=True,
                check=False,
            )
        except Exception as error:
            self.logger.warning("failed to read macOS proxy settings: %s", error)
            return {}

        if result.returncode != 0 or not result.stdout.strip():
            return {}

        entries: dict[str, str] = {}
        for line in result.stdout.splitlines():
            match = re.match(r"^\s*([A-Za-z0-9]+)\s*:\s*(.*?)\s*$", line)
            if match:
                entries[match.group(1)] = match.group(2)

        exports: "OrderedDict[str, str]" = OrderedDict()
        if entries.get("SOCKSEnable") == "1" and entries.get("SOCKSProxy") and entries.get("SOCKSPort"):
            proxy = f"socks5://{entries['SOCKSProxy']}:{entries['SOCKSPort']}"
            exports["ALL_PROXY"] = proxy
            exports["all_proxy"] = proxy
        if entries.get("HTTPEnable") == "1" and entries.get("HTTPProxy") and entries.get("HTTPPort"):
            proxy = f"http://{entries['HTTPProxy']}:{entries['HTTPPort']}"
            exports["HTTP_PROXY"] = proxy
            exports["http_proxy"] = proxy
        if entries.get("HTTPSEnable") == "1" and entries.get("HTTPSProxy") and entries.get("HTTPSPort"):
            proxy = f"http://{entries['HTTPSProxy']}:{entries['HTTPSPort']}"
            exports["HTTPS_PROXY"] = proxy
            exports["https_proxy"] = proxy

        no_proxy = (
            self._read_launchctl_env("NO_PROXY")
            or self._read_launchctl_env("no_proxy")
            or "localhost,127.0.0.1"
        )
        if exports:
            exports["NO_PROXY"] = no_proxy
            exports["no_proxy"] = no_proxy
        return dict(exports)

    def _build_prelaunch_hooks(self, *, context_file: Path | None = None) -> tuple[str, ...]:
        hooks: list[str] = []
        if context_file is not None:
            hooks.append(f"export FEISHU_CODEX_CONTEXT_FILE={shlex.quote(str(context_file))}")
        if self.lark_cli_executable:
            hooks.append(f"export LARK_CLI_BIN={shlex.quote(self.lark_cli_executable)}")
        proxy_env = self._detect_system_proxy_env()
        if proxy_env:
            export_line = " ".join(
                f"export {name}={shlex.quote(value)};"
                for name, value in proxy_env.items()
                if value
            )
            hooks.append(export_line.rstrip(";"))
        env_file = self.settings.tmux_env_file
        if env_file is not None:
            hooks.append(f"test -f {shlex.quote(str(env_file))} && source {shlex.quote(str(env_file))}")
        return tuple(hooks)

    def _build_runtime(
        self,
        session_name: str,
        conversation_id: str,
        workdir: str,
        model_name: str,
        reasoning_effort: str,
    ) -> TmuxAgentRuntime:
        context_file = self.context_file_for(conversation_id)
        return TmuxAgentRuntime(
            session_name=session_name,
            work_dir=Path(workdir),
            runtime_dir=self._tmux_runtime_dir(),
            cli_config=CodexCliConfig(
                model=model_name,
                reasoning_effort=reasoning_effort,
                developer_instructions=build_tmux_bridge_instructions(
                    session_name,
                    helper_script=self.helper_script,
                    context_file=context_file,
                    lark_cli_executable=self.lark_cli_executable,
                ),
            ),
            prelaunch_hooks=self._build_prelaunch_hooks(context_file=context_file),
        )

    def _runtime_info(self, runtime: TmuxAgentRuntime, tail_lines: int = 220) -> dict[str, str]:
        info: dict[str, str] = {
            key: str(value)
            for key, value in runtime.get_runtime_metadata().items()
        }
        snapshot = runtime.get_snapshot(tail_lines=tail_lines)
        info.update(
            {
                "detected_status": snapshot.detected_status.value,
                "confirmed_status": snapshot.confirmed_status.value,
                "current_command": snapshot.current_command,
                "current_path": snapshot.current_path,
                "pane_dead": str(snapshot.pane_dead).lower(),
            }
        )
        return info

    def _runtime_for(self, session: SessionRecord) -> TmuxAgentRuntime:
        session_name = session.tmux_session_name
        runtime = self._runtimes.get(session_name)
        if runtime is not None:
            return runtime
        runtime = self._build_runtime(
            session_name=session_name,
            conversation_id=session.conversation_id,
            workdir=session.workdir,
            model_name=_resolve_model_name(self.settings, session=session),
            reasoning_effort=_resolve_reasoning_effort(self.settings, session=session),
        )
        runtime.resume_codex_session(
            prompt=None,
            attach_if_running=False,
            attach_after_resume=False,
            timeout_sec=self.settings.tmux_timeout_sec,
        )
        self._runtimes[session_name] = runtime
        return runtime

    def create_session(
        self,
        workdir: str,
        conversation_id: str,
        model_name: str,
        reasoning_effort: str,
    ) -> dict[str, str]:
        session_name = f"feishu-codex-{conversation_id[:12]}-{uuid.uuid4().hex[:8]}"
        runtime = self._build_runtime(
            session_name=session_name,
            conversation_id=conversation_id,
            workdir=workdir,
            model_name=model_name,
            reasoning_effort=reasoning_effort,
        )
        runtime.ensure_codex_ready(recreate_session=True)
        self._runtimes[session_name] = runtime
        info = self._runtime_info(runtime)
        return {
            "session_name": session_name,
            "summary": "tmux session created",
            "model_name": model_name,
            "reasoning_effort": reasoning_effort,
            "agent_session_id": info.get("agent_session_id", ""),
            "state_path": info.get("state_path", ""),
            "log_path": info.get("log_path", ""),
        }

    def bind_existing_session(
        self,
        session_name: str,
        workdir: str,
        conversation_id: str,
        model_name: str,
        reasoning_effort: str,
    ) -> dict[str, str]:
        runtime = self._build_runtime(
            session_name=session_name,
            conversation_id=conversation_id,
            workdir=workdir,
            model_name=model_name,
            reasoning_effort=reasoning_effort,
        )
        runtime.resume_codex_session(
            prompt=None,
            attach_if_running=False,
            attach_after_resume=False,
            timeout_sec=self.settings.tmux_timeout_sec,
        )
        self._runtimes[session_name] = runtime
        info = self._runtime_info(runtime)
        return {
            "session_name": session_name,
            "summary": "bound existing tmux session",
            "model_name": model_name,
            "reasoning_effort": reasoning_effort,
            "agent_session_id": info.get("agent_session_id", ""),
            "state_path": info.get("state_path", ""),
            "log_path": info.get("log_path", ""),
        }

    def send_prompt(self, session: SessionRecord, prompt: str) -> str:
        if not session.tmux_session_name:
            raise RuntimeError("active tmux session has no session_name")
        runtime = self._runtime_for(session)
        reply = runtime.ask(prompt=prompt, timeout_sec=self.settings.tmux_timeout_sec)
        if not reply.strip():
            raise RuntimeError("tmux Codex returned an empty response")
        return reply

    def describe_session(self, session: SessionRecord) -> dict[str, str]:
        if not session.tmux_session_name:
            return {}
        try:
            runtime = self._runtime_for(session)
            info = self._runtime_info(runtime)
            info["model_name"] = _resolve_model_name(self.settings, session=session)
            info["reasoning_effort"] = _resolve_reasoning_effort(self.settings, session=session)
            return info
        except Exception as error:
            self.logger.exception("failed to describe tmux session %s", session.tmux_session_name)
            return {"runtime_error": str(error)}

    def reconfigure_session(self, session: SessionRecord, model_name: str, reasoning_effort: str) -> dict[str, str]:
        if not session.tmux_session_name:
            raise RuntimeError("active tmux session has no session_name")

        runtime = self._runtimes.get(session.tmux_session_name)
        if runtime is None:
            runtime = self._build_runtime(
                session_name=session.tmux_session_name,
                conversation_id=session.conversation_id,
                workdir=session.workdir,
                model_name=_resolve_model_name(self.settings, session=session),
                reasoning_effort=_resolve_reasoning_effort(self.settings, session=session),
            )

        if not runtime.agent_session_id:
            runtime._refresh_agent_session_id()  # noqa: SLF001
        if not runtime.agent_session_id:
            raise RuntimeError("tmux session has no recorded agent_session_id; cannot reconfigure")

        reconfigured = self._build_runtime(
            session_name=session.tmux_session_name,
            conversation_id=session.conversation_id,
            workdir=session.workdir,
            model_name=model_name,
            reasoning_effort=reasoning_effort,
        )
        reconfigured.agent_session_id = runtime.agent_session_id
        reconfigured.kill_session()
        reconfigured.resume_codex_session(
            prompt=None,
            attach_if_running=False,
            attach_after_resume=False,
            timeout_sec=self.settings.tmux_timeout_sec,
        )
        self._runtimes[session.tmux_session_name] = reconfigured
        info = self._runtime_info(reconfigured)
        info.update(
            {
                "summary": "tmux session restarted and resumed with the new configuration",
                "model_name": model_name,
                "reasoning_effort": reasoning_effort,
            }
        )
        return info


class BackendManager:
    def __init__(self, settings: Settings, logger: logging.Logger):
        self.exec_backend = ExecBackend(settings=settings, logger=logger)
        self.tmux_backend = TmuxBackend(settings=settings, logger=logger)

    def update_conversation_context(self, conversation_id: str, context: dict[str, str]) -> Path:
        backend = self.tmux_backend if context.get("preferred_backend") == BackendKind.TMUX.value else self.exec_backend
        return backend.write_context(conversation_id, context)

    def create_session(
        self,
        backend: BackendKind,
        workdir: str,
        conversation_id: str,
        model_name: str,
        reasoning_effort: str,
    ) -> dict[str, str]:
        if backend == BackendKind.EXEC:
            return self.exec_backend.create_session(
                workdir=workdir,
                conversation_id=conversation_id,
                model_name=model_name,
                reasoning_effort=reasoning_effort,
            )
        return self.tmux_backend.create_session(
            workdir=workdir,
            conversation_id=conversation_id,
            model_name=model_name,
            reasoning_effort=reasoning_effort,
        )

    def bind_existing_session(
        self,
        backend: BackendKind,
        external_session_id: str,
        workdir: str,
        conversation_id: str,
        model_name: str,
        reasoning_effort: str,
    ) -> dict[str, str]:
        if backend == BackendKind.EXEC:
            return self.exec_backend.bind_existing_session(
                thread_id=external_session_id,
                workdir=workdir,
                conversation_id=conversation_id,
                model_name=model_name,
                reasoning_effort=reasoning_effort,
            )
        return self.tmux_backend.bind_existing_session(
            session_name=external_session_id,
            workdir=workdir,
            conversation_id=conversation_id,
            model_name=model_name,
            reasoning_effort=reasoning_effort,
        )

    def send_prompt(self, session: SessionRecord, prompt: str) -> str:
        backend = BackendKind(session.backend)
        if backend == BackendKind.EXEC:
            return self.exec_backend.send_prompt(session=session, prompt=prompt)
        return self.tmux_backend.send_prompt(session=session, prompt=prompt)

    def describe_session(self, session: SessionRecord) -> dict[str, str]:
        backend = BackendKind(session.backend)
        if backend == BackendKind.EXEC:
            return self.exec_backend.describe_session(session=session)
        return self.tmux_backend.describe_session(session=session)

    def reconfigure_session(
        self,
        session: SessionRecord,
        model_name: str,
        reasoning_effort: str,
    ) -> dict[str, str]:
        backend = BackendKind(session.backend)
        if backend == BackendKind.EXEC:
            return self.exec_backend.reconfigure_session(
                session=session,
                model_name=model_name,
                reasoning_effort=reasoning_effort,
            )
        return self.tmux_backend.reconfigure_session(
            session=session,
            model_name=model_name,
            reasoning_effort=reasoning_effort,
        )
