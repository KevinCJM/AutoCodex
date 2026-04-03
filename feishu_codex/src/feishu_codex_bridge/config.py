from __future__ import annotations

import json
import os
import stat
import sys
from dataclasses import dataclass
from getpass import getpass
from pathlib import Path
from typing import Any, Callable


DEFAULT_MODELS = (
    "gpt-5.4",
    "gpt-5.4-mini",
    "gpt-5.1-codex-mini",
)
DEFAULT_REASONING_EFFORTS = ("low", "medium", "high", "xhigh")


class MissingConfigurationError(RuntimeError):
    pass


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _default_runtime_dir() -> Path:
    return _project_root() / ".runtime"


def _default_config_path() -> Path:
    return _project_root() / ".feishu_codex_config.json"


def _parse_bool(value: str | bool | None, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return value.strip().lower() in {"1", "true", "yes", "on", "y"}


def _parse_int(value: str | int | None, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, int):
        return value
    if not value.strip():
        return default
    return int(value)


def _parse_csv(value: str | list[str] | tuple[str, ...] | None) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (list, tuple)):
        return tuple(str(item).strip() for item in value if str(item).strip())
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _env_first(*names: str) -> str:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def _load_config_payload(config_path: Path) -> dict[str, Any]:
    if not config_path.exists():
        return {}
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError(f"invalid config payload in {config_path}")
    return payload


def _config_value(payload: dict[str, Any], key: str, *aliases: str) -> Any:
    for name in (key, *aliases):
        if name in payload:
            return payload[name]
    return None


def _masked_secret(secret: str) -> str:
    if len(secret) <= 8:
        return "*" * len(secret)
    return f"{secret[:4]}{'*' * max(len(secret) - 8, 1)}{secret[-4:]}"


def _prompt_text(
    prompt: str,
    *,
    default: str = "",
    input_fn: Callable[[str], str] = input,
) -> str:
    suffix = f" [{default}]" if default else ""
    value = input_fn(f"{prompt}{suffix}: ").strip()
    return value or default


def _prompt_secret(
    prompt: str,
    *,
    default: str = "",
    getpass_fn: Callable[[str], str] = getpass,
) -> str:
    suffix = f" [{_masked_secret(default)}]" if default else ""
    value = getpass_fn(f"{prompt}{suffix}: ").strip()
    return value or default


def _prompt_bool(
    prompt: str,
    *,
    default: bool,
    input_fn: Callable[[str], str] = input,
) -> bool:
    default_text = "Y/n" if default else "y/N"
    value = input_fn(f"{prompt} [{default_text}]: ").strip().lower()
    if not value:
        return default
    if value in {"y", "yes", "1", "true", "on"}:
        return True
    if value in {"n", "no", "0", "false", "off"}:
        return False
    raise ValueError(f"invalid boolean input: {value}")


def resolve_config_path() -> Path:
    configured = _env_first("FEISHU_CODEX_CONFIG_PATH", "DING_CODEX_CONFIG_PATH")
    if configured:
        return Path(configured).expanduser().resolve()
    return _default_config_path()


def save_config_payload(config_path: Path, payload: dict[str, Any]) -> Path:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(config_path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    return config_path


@dataclass(frozen=True)
class Settings:
    app_id: str
    app_secret: str
    verification_token: str
    encrypt_key: str
    runtime_dir: Path
    db_path: Path
    log_dir: Path
    allow_all_senders: bool
    allowed_senders: tuple[str, ...]
    max_workers: int
    exec_timeout_sec: int
    tmux_timeout_sec: int
    text_chunk_chars: int
    bot_open_id: str
    default_model: str
    default_reasoning_effort: str
    available_models: tuple[str, ...]
    available_reasoning_efforts: tuple[str, ...]
    tmux_env_file: Path | None = None
    tmux_use_system_proxy: bool = True
    config_path: Path | None = None

    @classmethod
    def from_env(cls) -> "Settings":
        config_path = resolve_config_path()
        payload = _load_config_payload(config_path)

        runtime_dir = Path(
            _env_first("FEISHU_CODEX_RUNTIME_DIR", "DING_CODEX_RUNTIME_DIR")
            or str(_config_value(payload, "runtime_dir") or _default_runtime_dir())
        ).expanduser().resolve()
        db_path = Path(
            _env_first("FEISHU_CODEX_DB_PATH", "DING_CODEX_DB_PATH")
            or str(_config_value(payload, "db_path") or (runtime_dir / "bridge.sqlite3"))
        ).expanduser().resolve()
        log_dir = Path(
            _env_first("FEISHU_CODEX_LOG_DIR", "DING_CODEX_LOG_DIR")
            or str(_config_value(payload, "log_dir") or (runtime_dir / "logs"))
        ).expanduser().resolve()
        allow_all_senders = _parse_bool(
            _env_first("FEISHU_CODEX_ALLOW_ALL_SENDERS", "DING_CODEX_ALLOW_ALL_SENDERS")
            or _config_value(payload, "allow_all_senders"),
            default=False,
        )
        allowed_senders = _parse_csv(
            _env_first("FEISHU_CODEX_ALLOWED_SENDERS", "DING_CODEX_ALLOWED_SENDERS")
            or _config_value(payload, "allowed_senders")
        )
        available_models = _parse_csv(
            _env_first("FEISHU_CODEX_AVAILABLE_MODELS", "DING_CODEX_AVAILABLE_MODELS")
            or _config_value(payload, "available_models")
        ) or DEFAULT_MODELS
        default_model = (
            _env_first("FEISHU_CODEX_DEFAULT_MODEL", "DING_CODEX_DEFAULT_MODEL")
            or str(_config_value(payload, "default_model") or available_models[0])
        )
        if default_model not in available_models:
            available_models = (default_model, *available_models)

        available_reasoning_efforts = _parse_csv(
            _env_first(
                "FEISHU_CODEX_AVAILABLE_REASONING_EFFORTS",
                "DING_CODEX_AVAILABLE_REASONING_EFFORTS",
            )
            or _config_value(payload, "available_reasoning_efforts")
        ) or DEFAULT_REASONING_EFFORTS
        default_reasoning_effort = (
            _env_first(
                "FEISHU_CODEX_DEFAULT_REASONING_EFFORT",
                "DING_CODEX_DEFAULT_REASONING_EFFORT",
            )
            or str(_config_value(payload, "default_reasoning_effort") or "xhigh")
        ).lower()
        if default_reasoning_effort == "max":
            default_reasoning_effort = "xhigh"
        if default_reasoning_effort not in available_reasoning_efforts:
            available_reasoning_efforts = (default_reasoning_effort, *available_reasoning_efforts)

        app_id = _env_first("FEISHU_APP_ID", "FEISHU_CLIENT_ID", "APP_ID") or str(
            _config_value(payload, "app_id", "client_id") or ""
        ).strip()
        app_secret = _env_first("FEISHU_APP_SECRET", "FEISHU_CLIENT_SECRET", "APP_SECRET") or str(
            _config_value(payload, "app_secret", "client_secret") or ""
        ).strip()
        if not app_id or not app_secret:
            raise MissingConfigurationError(
                "Missing Feishu credentials. Run `feishu-codex-init` or set "
                "FEISHU_APP_ID and FEISHU_APP_SECRET before starting the bridge."
            )

        runtime_dir.mkdir(parents=True, exist_ok=True)
        log_dir.mkdir(parents=True, exist_ok=True)
        db_path.parent.mkdir(parents=True, exist_ok=True)

        tmux_env_file_value = _env_first("FEISHU_CODEX_TMUX_ENV_FILE", "DING_CODEX_TMUX_ENV_FILE") or str(
            _config_value(payload, "tmux_env_file") or ""
        ).strip()

        return cls(
            app_id=app_id,
            app_secret=app_secret,
            verification_token=_env_first("FEISHU_VERIFICATION_TOKEN", "VERIFICATION_TOKEN")
            or str(_config_value(payload, "verification_token") or ""),
            encrypt_key=_env_first("FEISHU_ENCRYPT_KEY", "ENCRYPT_KEY")
            or str(_config_value(payload, "encrypt_key") or ""),
            runtime_dir=runtime_dir,
            db_path=db_path,
            log_dir=log_dir,
            allow_all_senders=allow_all_senders,
            allowed_senders=allowed_senders,
            max_workers=_parse_int(
                _env_first("FEISHU_CODEX_MAX_WORKERS", "DING_CODEX_MAX_WORKERS")
                or _config_value(payload, "max_workers"),
                default=4,
            ),
            exec_timeout_sec=_parse_int(
                _env_first("FEISHU_CODEX_EXEC_TIMEOUT_SEC", "DING_CODEX_EXEC_TIMEOUT_SEC")
                or _config_value(payload, "exec_timeout_sec"),
                default=30 * 60,
            ),
            tmux_timeout_sec=_parse_int(
                _env_first("FEISHU_CODEX_TMUX_TIMEOUT_SEC", "DING_CODEX_TMUX_TIMEOUT_SEC")
                or _config_value(payload, "tmux_timeout_sec"),
                default=4 * 60,
            ),
            text_chunk_chars=_parse_int(
                _env_first("FEISHU_CODEX_TEXT_CHUNK_CHARS", "DING_CODEX_MARKDOWN_CHUNK_CHARS")
                or _config_value(payload, "text_chunk_chars"),
                default=3000,
            ),
            bot_open_id=_env_first("FEISHU_BOT_OPEN_ID")
            or str(_config_value(payload, "bot_open_id") or ""),
            default_model=default_model,
            default_reasoning_effort=default_reasoning_effort,
            available_models=available_models,
            available_reasoning_efforts=available_reasoning_efforts,
            tmux_env_file=Path(tmux_env_file_value).expanduser().resolve() if tmux_env_file_value else None,
            tmux_use_system_proxy=_parse_bool(
                _env_first("FEISHU_CODEX_TMUX_USE_SYSTEM_PROXY", "DING_CODEX_TMUX_USE_SYSTEM_PROXY")
                or _config_value(payload, "tmux_use_system_proxy"),
                default=True,
            ),
            config_path=config_path,
        )

    def sender_allowed(self, sender_open_id: str, sender_user_id: str) -> bool:
        if self.allow_all_senders:
            return True
        normalized = {sender_open_id.strip(), sender_user_id.strip()}
        normalized.discard("")
        return bool(normalized & set(self.allowed_senders))


def run_interactive_setup(
    *,
    config_path: Path | None = None,
    input_fn: Callable[[str], str] = input,
    getpass_fn: Callable[[str], str] = getpass,
    stdout: Any = None,
) -> Path:
    out = stdout or sys.stdout
    target = (config_path or resolve_config_path()).expanduser().resolve()
    existing = _load_config_payload(target)

    print("Feishu Codex Bridge 首次配置", file=out)
    print(f"配置文件: {target}", file=out)
    print("将保存飞书应用凭据和默认运行参数。后续启动会自动读取。", file=out)
    print("", file=out)

    app_id = _prompt_text(
        "1. 飞书 App ID",
        default=str(_config_value(existing, "app_id", "client_id") or ""),
        input_fn=input_fn,
    )
    while not app_id:
        print("App ID 不能为空。", file=out)
        app_id = _prompt_text("1. 飞书 App ID", input_fn=input_fn)

    app_secret = _prompt_secret(
        "2. 飞书 App Secret",
        default=str(_config_value(existing, "app_secret", "client_secret") or ""),
        getpass_fn=getpass_fn,
    )
    while not app_secret:
        print("App Secret 不能为空。", file=out)
        app_secret = _prompt_secret("2. 飞书 App Secret", getpass_fn=getpass_fn)

    allow_all_senders = _prompt_bool(
        "3. 是否允许所有发送人直接调用机器人",
        default=_parse_bool(_config_value(existing, "allow_all_senders"), True),
        input_fn=input_fn,
    )
    allowed_senders = ""
    if not allow_all_senders:
        allowed_senders = _prompt_text(
            "4. 允许名单，多个 ID 用逗号分隔",
            default=",".join(_parse_csv(_config_value(existing, "allowed_senders"))),
            input_fn=input_fn,
        )

    default_model = _prompt_text(
        "5. 默认模型",
        default=str(_config_value(existing, "default_model") or DEFAULT_MODELS[0]),
        input_fn=input_fn,
    )
    default_reasoning_effort = _prompt_text(
        "6. 默认推理强度",
        default=str(_config_value(existing, "default_reasoning_effort") or "xhigh"),
        input_fn=input_fn,
    ).lower()
    runtime_dir = _prompt_text(
        "7. 运行目录",
        default=str(Path(str(_config_value(existing, "runtime_dir") or _default_runtime_dir())).expanduser().resolve()),
        input_fn=input_fn,
    )
    tmux_use_system_proxy = _prompt_bool(
        "8. tmux 子会话是否自动读取系统代理/VPN",
        default=_parse_bool(_config_value(existing, "tmux_use_system_proxy"), True),
        input_fn=input_fn,
    )
    tmux_env_file = _prompt_text(
        "9. 可选的 tmux 额外环境脚本路径（留空跳过）",
        default=str(_config_value(existing, "tmux_env_file") or ""),
        input_fn=input_fn,
    )
    bot_open_id = _prompt_text(
        "10. 可选的机器人 open_id（群聊严格校验 @ 机器人时使用，留空跳过）",
        default=str(_config_value(existing, "bot_open_id") or ""),
        input_fn=input_fn,
    )

    payload: dict[str, Any] = {
        "app_id": app_id,
        "app_secret": app_secret,
        "verification_token": str(_config_value(existing, "verification_token") or ""),
        "encrypt_key": str(_config_value(existing, "encrypt_key") or ""),
        "runtime_dir": str(Path(runtime_dir).expanduser().resolve()),
        "db_path": str(
            Path(str(_config_value(existing, "db_path") or (Path(runtime_dir).expanduser().resolve() / "bridge.sqlite3")))
            .expanduser()
            .resolve()
        ),
        "log_dir": str(
            Path(str(_config_value(existing, "log_dir") or (Path(runtime_dir).expanduser().resolve() / "logs")))
            .expanduser()
            .resolve()
        ),
        "allow_all_senders": allow_all_senders,
        "allowed_senders": list(_parse_csv(allowed_senders)),
        "max_workers": _parse_int(_config_value(existing, "max_workers"), 4),
        "exec_timeout_sec": _parse_int(_config_value(existing, "exec_timeout_sec"), 30 * 60),
        "tmux_timeout_sec": _parse_int(_config_value(existing, "tmux_timeout_sec"), 4 * 60),
        "text_chunk_chars": _parse_int(_config_value(existing, "text_chunk_chars"), 3000),
        "bot_open_id": bot_open_id,
        "default_model": default_model,
        "default_reasoning_effort": "xhigh" if default_reasoning_effort == "max" else default_reasoning_effort,
        "available_models": list(
            _parse_csv(_config_value(existing, "available_models")) or DEFAULT_MODELS
        ),
        "available_reasoning_efforts": list(
            _parse_csv(_config_value(existing, "available_reasoning_efforts")) or DEFAULT_REASONING_EFFORTS
        ),
        "tmux_use_system_proxy": tmux_use_system_proxy,
        "tmux_env_file": tmux_env_file.strip(),
    }
    save_config_payload(target, payload)
    print("", file=out)
    print(f"配置已保存: {target}", file=out)
    print("现在可以直接运行 `feishu-codex-bridge`。", file=out)
    return target


def load_settings_with_setup() -> Settings:
    try:
        return Settings.from_env()
    except MissingConfigurationError:
        if sys.stdin.isatty() and sys.stdout.isatty():
            run_interactive_setup()
            return Settings.from_env()
        raise
