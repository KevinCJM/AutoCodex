from pathlib import Path

from feishu_codex_bridge.backends import BRIDGE_INIT_PROMPT, build_tmux_bridge_instructions
from feishu_codex_bridge.backends import BackendManager
from feishu_codex_bridge.config import Settings
from feishu_codex_bridge.models import BackendKind, SessionRecord


class FakeRuntime:
    def __init__(self):
        self.prompts = []
        self.ready_calls = []
        self.resume_calls = []
        self.kill_calls = 0
        self.agent_session_id = "codex-session-1"

    def ask(self, prompt: str, timeout_sec: float = 0.0) -> str:
        self.prompts.append((prompt, timeout_sec))
        return "tmux reply"

    def ensure_codex_ready(self, recreate_session: bool = False):
        self.ready_calls.append(recreate_session)
        return None

    def resume_codex_session(self, **kwargs):
        self.resume_calls.append(kwargs)
        return None

    def kill_session(self):
        self.kill_calls += 1
        return None

    def get_runtime_metadata(self):
        return {
            "backend": "codex",
            "terminal_id": "term-1",
            "session_name": "tmux-1",
            "pane_id": "%1",
            "agent_session_id": "codex-session-1",
            "work_dir": "/tmp",
            "log_path": "/tmp/runtime/log.txt",
            "raw_log_path": "/tmp/runtime/raw.log",
            "state_path": "/tmp/runtime/state.json",
        }

    def get_snapshot(self, tail_lines: int = 0):
        return type(
            "Snapshot",
            (),
            {
                "detected_status": type("Status", (), {"value": "processing"})(),
                "confirmed_status": type("Status", (), {"value": "idle"})(),
                "current_command": "codex",
                "current_path": "/tmp",
                "pane_dead": False,
            },
        )()


def build_settings(tmp_path: Path) -> Settings:
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    return Settings(
        app_id="id",
        app_secret="secret",
        verification_token="",
        encrypt_key="",
        runtime_dir=runtime_dir,
        db_path=tmp_path / "bridge.sqlite3",
        log_dir=log_dir,
        allow_all_senders=True,
        allowed_senders=(),
        max_workers=2,
        exec_timeout_sec=10,
        tmux_timeout_sec=20,
        text_chunk_chars=1200,
        bot_open_id="",
        default_model="gpt-5.4",
        default_reasoning_effort="xhigh",
        available_models=("gpt-5.4", "gpt-5.4-mini", "gpt-5.1-codex-mini"),
        available_reasoning_efforts=("low", "medium", "high", "xhigh"),
    )


def test_exec_backend_uses_init_and_resume(monkeypatch, tmp_path: Path):
    settings = build_settings(tmp_path)
    manager = BackendManager(settings=settings, logger=__import__("logging").getLogger("test"))
    manager.exec_backend.lark_cli_executable = "/usr/local/bin/lark-cli"

    def fake_init(prompt, folder_path=None, timeout=0, **kwargs):
        assert folder_path == "/tmp"
        assert BRIDGE_INIT_PROMPT in prompt
        assert "Feishu CLI tool access:" in prompt
        assert "lark_cli_helper.py" in prompt
        return [], "ready", "thread-123"

    def fake_resume(thread_id, folder_path, prompt, timeout=0, **kwargs):
        assert thread_id == "thread-123"
        assert folder_path == "/tmp"
        assert "hello" in prompt
        assert "reply-text" in prompt
        return [], "exec reply", thread_id

    monkeypatch.setattr("feishu_codex_bridge.backends.init_codex", fake_init)
    monkeypatch.setattr("feishu_codex_bridge.backends.resume_codex", fake_resume)

    created = manager.create_session(BackendKind.EXEC, "/tmp", "cid", "gpt-5.4", "xhigh")
    assert created["thread_id"] == "thread-123"

    session = SessionRecord(
        id=1,
        conversation_id="cid",
        backend="exec",
        workdir="/tmp",
        exec_thread_id="thread-123",
        tmux_session_name="",
        model_name="gpt-5.4",
        reasoning_effort="xhigh",
        status="idle",
        last_reply="",
    )
    reply = manager.send_prompt(session, "hello")
    assert reply == "exec reply"


def test_tmux_backend_uses_runtime(monkeypatch, tmp_path: Path):
    settings = build_settings(tmp_path)
    manager = BackendManager(settings=settings, logger=__import__("logging").getLogger("test"))
    manager.tmux_backend.lark_cli_executable = "/usr/local/bin/lark-cli"
    runtime = FakeRuntime()
    created_kwargs = {}

    def fake_runtime_factory(**kwargs):
        created_kwargs.update(kwargs)
        return runtime

    monkeypatch.setattr("feishu_codex_bridge.backends.TmuxAgentRuntime", fake_runtime_factory)
    monkeypatch.setattr(
        "feishu_codex_bridge.backends.TmuxBackend._detect_system_proxy_env",
        lambda self: {
            "ALL_PROXY": "socks5://127.0.0.1:10010",
            "HTTPS_PROXY": "http://127.0.0.1:10900",
            "HTTP_PROXY": "http://127.0.0.1:10900",
        },
    )

    created = manager.create_session(BackendKind.TMUX, "/tmp", "cid", "gpt-5.4", "xhigh")
    session_name = created["session_name"]
    assert runtime.ready_calls == [True]
    assert created["agent_session_id"] == "codex-session-1"
    assert Path(created_kwargs["runtime_dir"]) == settings.runtime_dir / "tmux"
    assert session_name in created_kwargs["cli_config"].developer_instructions
    expected_context = manager.tmux_backend.context_file_for("cid")
    assert created_kwargs["cli_config"].developer_instructions == build_tmux_bridge_instructions(
        session_name,
        helper_script=manager.tmux_backend.helper_script,
        context_file=expected_context,
        lark_cli_executable="/usr/local/bin/lark-cli",
    )
    assert any("FEISHU_CODEX_CONTEXT_FILE" in hook and str(expected_context) in hook for hook in created_kwargs["prelaunch_hooks"])
    assert any("LARK_CLI_BIN=/usr/local/bin/lark-cli" in hook for hook in created_kwargs["prelaunch_hooks"])
    assert any("ALL_PROXY=socks5://127.0.0.1:10010" in hook for hook in created_kwargs["prelaunch_hooks"])
    assert created_kwargs["cli_config"].model == "gpt-5.4"
    assert created_kwargs["cli_config"].reasoning_effort == "xhigh"

    session = SessionRecord(
        id=1,
        conversation_id="cid",
        backend="tmux",
        workdir="/tmp",
        exec_thread_id="",
        tmux_session_name=session_name,
        model_name="gpt-5.4",
        reasoning_effort="xhigh",
        status="idle",
        last_reply="",
    )
    reply = manager.send_prompt(session, "hello")
    assert reply == "tmux reply"
    assert runtime.prompts[0][0] == "hello"

    resume_runtime = FakeRuntime()
    monkeypatch.setattr("feishu_codex_bridge.backends.TmuxAgentRuntime", lambda **kwargs: resume_runtime)
    manager_after_restart = BackendManager(settings=settings, logger=__import__("logging").getLogger("test"))
    manager_after_restart.bind_existing_session(
        BackendKind.TMUX,
        session_name,
        "/tmp",
        "cid",
        "gpt-5.4",
        "xhigh",
    )
    assert resume_runtime.resume_calls[0]["attach_if_running"] is False


def test_tmux_backend_describe_session_returns_runtime_metadata(monkeypatch, tmp_path: Path):
    settings = build_settings(tmp_path)
    manager = BackendManager(settings=settings, logger=__import__("logging").getLogger("test"))
    runtime = FakeRuntime()

    monkeypatch.setattr("feishu_codex_bridge.backends.TmuxAgentRuntime", lambda **kwargs: runtime)

    session = SessionRecord(
        id=1,
        conversation_id="cid",
        backend="tmux",
        workdir="/tmp",
        exec_thread_id="",
        tmux_session_name="tmux-1",
        model_name="gpt-5.4",
        reasoning_effort="xhigh",
        status="idle",
        last_reply="",
    )
    info = manager.describe_session(session)
    assert info["agent_session_id"] == "codex-session-1"
    assert info["confirmed_status"] == "idle"
    assert info["current_command"] == "codex"


def test_tmux_backend_builds_env_file_hook(tmp_path: Path):
    settings = build_settings(tmp_path)
    env_file = tmp_path / "tmux-proxy.env"
    env_file.write_text("export ALL_PROXY=socks5://127.0.0.1:10010\n", encoding="utf-8")
    settings = Settings(
        **{**settings.__dict__, "tmux_env_file": env_file}
    )
    backend = manager = BackendManager(
        settings=settings,
        logger=__import__("logging").getLogger("test"),
    ).tmux_backend

    hooks = backend._build_prelaunch_hooks()
    assert any("source" in hook and str(env_file) in hook for hook in hooks)


def test_tmux_backend_reconfigure_restarts_and_resumes(monkeypatch, tmp_path: Path):
    settings = build_settings(tmp_path)
    manager = BackendManager(settings=settings, logger=__import__("logging").getLogger("test"))
    current_runtime = FakeRuntime()
    built_runtimes = []

    def fake_runtime_factory(**kwargs):
        runtime = FakeRuntime()
        runtime.agent_session_id = current_runtime.agent_session_id
        built_runtimes.append((kwargs, runtime))
        return runtime

    monkeypatch.setattr("feishu_codex_bridge.backends.TmuxAgentRuntime", fake_runtime_factory)

    session = SessionRecord(
        id=1,
        conversation_id="cid",
        backend="tmux",
        workdir="/tmp",
        exec_thread_id="",
        tmux_session_name="tmux-1",
        model_name="gpt-5.4",
        reasoning_effort="xhigh",
        status="idle",
        last_reply="",
    )
    manager.tmux_backend._runtimes["tmux-1"] = current_runtime

    result = manager.reconfigure_session(session, "gpt-5.4-mini", "medium")

    assert built_runtimes[-1][0]["cli_config"].model == "gpt-5.4-mini"
    assert built_runtimes[-1][0]["cli_config"].reasoning_effort == "medium"
    assert built_runtimes[-1][1].kill_calls == 1
    assert built_runtimes[-1][1].resume_calls[0]["attach_if_running"] is False
    assert result["model_name"] == "gpt-5.4-mini"
    assert result["reasoning_effort"] == "medium"


def test_backend_manager_writes_context_file(tmp_path: Path):
    settings = build_settings(tmp_path)
    manager = BackendManager(settings=settings, logger=__import__("logging").getLogger("test"))

    context_file = manager.update_conversation_context(
        "chat-1",
        {
            "chat_id": "chat-1",
            "message_id": "msg-1",
            "cleaned_text": "hello",
            "preferred_backend": "tmux",
        },
    )

    assert context_file.exists()
    payload = context_file.read_text(encoding="utf-8")
    assert '"chat_id": "chat-1"' in payload
    assert '"message_id": "msg-1"' in payload
