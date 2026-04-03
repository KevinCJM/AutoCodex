from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from feishu_codex_bridge.config import MissingConfigurationError
from feishu_codex_bridge.config import Settings
from feishu_codex_bridge.config import run_interactive_setup


def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "FEISHU_CODEX_CONFIG_PATH",
        "FEISHU_APP_ID",
        "FEISHU_CLIENT_ID",
        "APP_ID",
        "FEISHU_APP_SECRET",
        "FEISHU_CLIENT_SECRET",
        "APP_SECRET",
        "FEISHU_CODEX_RUNTIME_DIR",
        "FEISHU_CODEX_DEFAULT_MODEL",
        "FEISHU_CODEX_DEFAULT_REASONING_EFFORT",
    ):
        monkeypatch.delenv(name, raising=False)


def test_run_interactive_setup_saves_config_and_loads_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    config_path = tmp_path / "feishu-config.json"
    monkeypatch.setenv("FEISHU_CODEX_CONFIG_PATH", str(config_path))
    _clear_env(monkeypatch)
    monkeypatch.setenv("FEISHU_CODEX_CONFIG_PATH", str(config_path))

    answers = iter(
        [
            "cli_test_app",
            "y",
            "gpt-5.4-mini",
            "high",
            str(tmp_path / "runtime"),
            "y",
            "",
            "",
        ]
    )
    stdout = io.StringIO()

    run_interactive_setup(
        input_fn=lambda _: next(answers),
        getpass_fn=lambda _: "secret-value",
        stdout=stdout,
    )

    payload = json.loads(config_path.read_text(encoding="utf-8"))
    assert payload["app_id"] == "cli_test_app"
    assert payload["app_secret"] == "secret-value"
    assert payload["default_model"] == "gpt-5.4-mini"
    assert payload["default_reasoning_effort"] == "high"

    settings = Settings.from_env()
    assert settings.app_id == "cli_test_app"
    assert settings.app_secret == "secret-value"
    assert settings.default_model == "gpt-5.4-mini"
    assert settings.default_reasoning_effort == "high"
    assert settings.runtime_dir == (tmp_path / "runtime").resolve()


def test_settings_from_env_prefers_env_over_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    config_path = tmp_path / "feishu-config.json"
    config_path.write_text(
        json.dumps(
            {
                "app_id": "cli_from_file",
                "app_secret": "secret_from_file",
                "default_model": "gpt-5.4",
                "default_reasoning_effort": "xhigh",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    _clear_env(monkeypatch)
    monkeypatch.setenv("FEISHU_CODEX_CONFIG_PATH", str(config_path))
    monkeypatch.setenv("FEISHU_APP_ID", "cli_from_env")
    monkeypatch.setenv("FEISHU_APP_SECRET", "secret_from_env")
    monkeypatch.setenv("FEISHU_CODEX_DEFAULT_MODEL", "gpt-5.4-mini")

    settings = Settings.from_env()

    assert settings.app_id == "cli_from_env"
    assert settings.app_secret == "secret_from_env"
    assert settings.default_model == "gpt-5.4-mini"
    assert settings.default_reasoning_effort == "xhigh"


def test_settings_from_env_raises_when_missing_credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    config_path = tmp_path / "missing.json"
    _clear_env(monkeypatch)
    monkeypatch.setenv("FEISHU_CODEX_CONFIG_PATH", str(config_path))

    with pytest.raises(MissingConfigurationError):
        Settings.from_env()
