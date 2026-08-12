from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import subprocess
import tarfile
import threading
import time
import urllib.error
import zipfile
from pathlib import Path
from unittest import mock

import pytest

from tmux_core.runtime import codegraph
from tmux_core.runtime.codegraph import (
    CODEGRAPH_MAX_HINT_CHARS,
    CODEGRAPH_MAX_REMINDER_CHARS,
    CODEGRAPH_VERSION,
    CodeGraphConfig,
    CodeGraphContractError,
    CodeGraphMode,
    CodeGraphQueryIntent,
    CodeGraphState,
    CodeGraphToolResolution,
    CodeGraphTurnContext,
    CodeGraphUnavailable,
    build_codegraph_hint_block,
    cli_main,
    execute_readonly_query,
    inspect_codegraph_project,
    initialize_codegraph_project,
    managed_codegraph_executable,
    normalize_codegraph_config,
    normalize_codegraph_mode,
    read_codegraph_project_status,
    read_codegraph_project_preference,
    resolve_codegraph_tool,
    resolve_codegraph_turn_profile,
    run_readonly_query,
    sync_codegraph_project,
    write_codegraph_project_preference,
)


def _tool() -> CodeGraphToolResolution:
    return CodeGraphToolResolution(
        executable_path="/tmp/codegraph",
        version=CODEGRAPH_VERSION,
        source="test",
        compatible=True,
    )


def _status_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "initialized": True,
        "version": CODEGRAPH_VERSION,
        "lastIndexed": "2026-08-10T00:00:00Z",
        "fileCount": 12,
        "nodeCount": 34,
        "edgeCount": 56,
        "pendingChanges": {"added": 0, "modified": 0, "removed": 0},
        "index": {"state": "complete", "pendingRefs": 0},
    }
    payload.update(overrides)
    return payload


def _status_process(payload: object, *, returncode: int = 0) -> tuple[int, str, str, bool]:
    return returncode, json.dumps(payload), "", False


def test_mode_and_config_contract() -> None:
    assert normalize_codegraph_mode("AUTO") is CodeGraphMode.AUTO
    assert normalize_codegraph_mode("") is CodeGraphMode.OFF
    with pytest.raises(ValueError, match="合法值"):
        normalize_codegraph_mode("legacy")
    assert normalize_codegraph_config(None) == {
        "max_files": 6,
        "max_output_chars": 12000,
        "init_timeout_sec": 300.0,
        "sync_timeout_sec": 60.0,
    }
    with pytest.raises(ValueError, match="未知 CodeGraph"):
        normalize_codegraph_config({"max_workers": 2})
    with pytest.raises(ValueError, match="1..20"):
        CodeGraphConfig(max_files=21)


def test_config_values_are_normalized_and_invalid_numeric_types_rejected() -> None:
    config = CodeGraphConfig(
        max_files="7",  # type: ignore[arg-type]
        max_output_chars="16000",  # type: ignore[arg-type]
        init_timeout_sec="12.5",  # type: ignore[arg-type]
        sync_timeout_sec=3,
    )
    assert config.max_files == 7 and type(config.max_files) is int
    assert config.max_output_chars == 16_000 and type(config.max_output_chars) is int
    assert config.init_timeout_sec == 12.5 and type(config.init_timeout_sec) is float
    assert config.sync_timeout_sec == 3.0 and type(config.sync_timeout_sec) is float
    with pytest.raises(ValueError, match="max_files.*整数"):
        CodeGraphConfig(max_files=1.5)
    with pytest.raises(ValueError, match="有限正数"):
        CodeGraphConfig(sync_timeout_sec=float("nan"))
    with pytest.raises(ValueError, match="整数"):
        CodeGraphConfig(max_output_chars=True)


def test_managed_path_is_versioned_and_platform_scoped(tmp_path: Path) -> None:
    with mock.patch.dict(os.environ, {"XDG_DATA_HOME": str(tmp_path)}):
        path = managed_codegraph_executable()
    assert f"codegraph/{CODEGRAPH_VERSION}" in path.as_posix()
    assert path.name in {"codegraph", "codegraph.exe"}


def test_relative_xdg_data_home_falls_back_to_absolute_user_data_root() -> None:
    with mock.patch.dict(os.environ, {"XDG_DATA_HOME": "relative-data-root"}):
        path = managed_codegraph_executable()
    assert path.is_absolute()
    assert "relative-data-root" not in path.parts


def test_managed_process_environment_disables_daemon_updates_and_tracking() -> None:
    with mock.patch.dict(
        os.environ,
        {
            "CODEGRAPH_DIR": "/tmp/hostile-index",
            "CODEGRAPH_TELEMETRY": "1",
            "CODEGRAPH_NO_UPDATE_CHECK": "0",
        },
        clear=False,
    ):
        environment = codegraph._sanitized_env(
            {
                "CODEGRAPH_DIR": "../override",
                "CODEGRAPH_TELEMETRY": "1",
                "CODEGRAPH_NO_DAEMON": "0",
                "CODEGRAPH_NO_UPDATE_CHECK": "0",
                "CODEGRAPH_NO_PROMPT_HOOK": "0",
                "DO_NOT_TRACK": "0",
            }
        )
    assert environment["CODEGRAPH_DIR"] == ".codegraph"
    assert environment["CODEGRAPH_TELEMETRY"] == "0"
    assert environment["CODEGRAPH_NO_DAEMON"] == "1"
    assert environment["CODEGRAPH_NO_UPDATE_CHECK"] == "1"
    assert environment["CODEGRAPH_NO_PROMPT_HOOK"] == "1"
    assert environment["DO_NOT_TRACK"] == "1"


def test_explicit_tool_is_absolute_exact_and_fail_closed(tmp_path: Path) -> None:
    fallback = tmp_path / "fallback-codegraph"
    fallback.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fallback.chmod(fallback.stat().st_mode | stat.S_IXUSR)
    with mock.patch.dict(os.environ, {"TMUX_CODEGRAPH_EXECUTABLE": "relative"}, clear=False):
        resolution = resolve_codegraph_tool(force_refresh=True)
    assert not resolution.compatible
    assert "绝对路径" in resolution.error

    explicit = tmp_path / "codegraph"
    explicit.write_text("#!/bin/sh\nif [ \"$1\" = \"--version\" ]; then echo 1.4.0; else echo init sync status explore; fi\n", encoding="utf-8")
    explicit.chmod(explicit.stat().st_mode | stat.S_IXUSR)
    with mock.patch.dict(os.environ, {"TMUX_CODEGRAPH_EXECUTABLE": str(explicit), "PATH": str(tmp_path)}, clear=False):
        resolution = resolve_codegraph_tool(force_refresh=True)
    assert not resolution.compatible
    assert resolution.source == "env"
    assert "1.4.0" in resolution.error


def test_path_tool_requires_command_contract(tmp_path: Path) -> None:
    executable = tmp_path / "codegraph"
    executable.write_text(
        "#!/bin/sh\nif [ \"$1\" = \"--version\" ]; then echo 1.5.0; else echo status explore; fi\n",
        encoding="utf-8",
    )
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    with mock.patch.dict(os.environ, {"PATH": str(tmp_path), "TMUX_CODEGRAPH_EXECUTABLE": ""}, clear=False):
        resolution = resolve_codegraph_tool(force_refresh=True)
    assert not resolution.compatible


def test_tool_cache_reprobes_after_atomic_executable_replacement(tmp_path: Path) -> None:
    executable = tmp_path / "codegraph"

    def write_version(path: Path, version: str) -> None:
        path.write_text(
            f'#!/bin/sh\nif [ "$1" = "--version" ]; then echo {version}; else echo init sync status explore; fi\n',
            encoding="utf-8",
        )
        path.chmod(0o755)

    write_version(executable, CODEGRAPH_VERSION)
    with mock.patch.dict(
        os.environ,
        {"TMUX_CODEGRAPH_EXECUTABLE": str(executable)},
        clear=False,
    ):
        assert resolve_codegraph_tool(force_refresh=True).compatible
        replacement = tmp_path / "replacement"
        write_version(replacement, "1.6.0")
        os.replace(replacement, executable)
        resolution = resolve_codegraph_tool()
    assert not resolution.compatible
    assert "1.6.0" in resolution.error


def test_off_status_never_probes_tool(tmp_path: Path) -> None:
    with mock.patch.object(codegraph, "resolve_codegraph_tool", side_effect=AssertionError("must not probe")):
        status = inspect_codegraph_project(tmp_path, mode="off")
    assert status.state == CodeGraphState.OFF.value
    assert not status.initialized


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (_status_payload(), "ready"),
        (_status_payload(pendingChanges={"added": 1, "modified": 2, "removed": 0}), "stale"),
        (_status_payload(index={"state": "partial", "pendingRefs": 0}), "degraded"),
        (_status_payload(index={"state": "complete", "pendingRefs": 2}), "degraded"),
        ({"initialized": False, "version": CODEGRAPH_VERSION}, "unavailable"),
    ],
)
def test_status_adapter_maps_protocol(tmp_path: Path, payload: dict[str, object], expected: str) -> None:
    payload = dict(payload)
    payload["projectPath"] = str(tmp_path)
    with (
        mock.patch.object(codegraph, "resolve_codegraph_tool", return_value=_tool()),
        mock.patch.object(codegraph, "_run_process_separate", return_value=_status_process(payload)),
    ):
        status = inspect_codegraph_project(tmp_path, mode="auto")
    assert status.state == expected
    assert status.version == CODEGRAPH_VERSION
    assert status.to_public_dict()["pending_changes"]["added"] >= 0


def test_status_rejects_worktree_mismatch_and_bad_json(tmp_path: Path) -> None:
    payload = _status_payload(
        projectPath=str(tmp_path / "other"),
        worktreeMismatch=None,
    )
    with (
        mock.patch.object(codegraph, "resolve_codegraph_tool", return_value=_tool()),
        mock.patch.object(codegraph, "_run_process_separate", return_value=_status_process(payload)),
    ):
        mismatch = inspect_codegraph_project(tmp_path, mode="auto")
    assert mismatch.state == "degraded"
    assert mismatch.worktree_mismatch

    bad = (0, "not-json", "", False)
    with (
        mock.patch.object(codegraph, "resolve_codegraph_tool", return_value=_tool()),
        mock.patch.object(codegraph, "_run_process_separate", return_value=bad),
    ):
        failed = inspect_codegraph_project(tmp_path, mode="auto")
    assert failed.state == "failed"
    assert "/" not in failed.last_error


@pytest.mark.parametrize(
    ("override", "reason"),
    [
        ({"projectPath": ""}, "projectPath"),
        ({"projectPath": "\x00invalid"}, "projectPath"),
        ({"projectPath": "/different/project"}, "projectPath"),
        ({"version": ""}, "版本"),
        ({"version": "1.4.0"}, "版本"),
    ],
)
@pytest.mark.parametrize(
    ("mode", "expected_state"),
    [("auto", "degraded"), ("required", "failed")],
)
def test_initialized_status_requires_exact_project_and_version(
    tmp_path: Path,
    override: dict[str, object],
    reason: str,
    mode: str,
    expected_state: str,
) -> None:
    payload = _status_payload(projectPath=str(tmp_path))
    payload.update(override)
    with (
        mock.patch.object(codegraph, "resolve_codegraph_tool", return_value=_tool()),
        mock.patch.object(codegraph, "_run_process_separate", return_value=_status_process(payload)),
    ):
        status = inspect_codegraph_project(tmp_path, mode=mode)
    assert status.state == expected_state
    assert reason in status.last_error


def test_status_requires_explicit_complete_state_and_strict_booleans(tmp_path: Path) -> None:
    unknown = _status_payload(index={})
    unknown["projectPath"] = str(tmp_path)
    with (
        mock.patch.object(codegraph, "resolve_codegraph_tool", return_value=_tool()),
        mock.patch.object(codegraph, "_run_process_separate", return_value=_status_process(unknown)),
    ):
        status = inspect_codegraph_project(tmp_path, mode="auto")
    assert status.state == "degraded"
    assert "index state" in status.last_error

    invalid = _status_payload(initialized="false")
    invalid["projectPath"] = str(tmp_path)
    with (
        mock.patch.object(codegraph, "resolve_codegraph_tool", return_value=_tool()),
        mock.patch.object(codegraph, "_run_process_separate", return_value=_status_process(invalid)),
    ):
        status = inspect_codegraph_project(tmp_path, mode="auto")
    assert status.state == "failed"
    assert "必须是布尔值" in status.last_error


def test_status_worktree_mismatch_accepts_only_null_or_object() -> None:
    matched = _status_payload(worktreeMismatch=None)
    mismatched = _status_payload(worktreeMismatch={"worktreeRoot": "/a", "indexRoot": "/b"})
    assert codegraph._status_from_payload(matched, mode=CodeGraphMode.AUTO).worktree_mismatch is False
    status = codegraph._status_from_payload(mismatched, mode=CodeGraphMode.AUTO)
    assert status.worktree_mismatch is True and status.state == "degraded"
    with pytest.raises(CodeGraphContractError, match="null 或对象"):
        codegraph._status_from_payload(
            _status_payload(worktreeMismatch=True),
            mode=CodeGraphMode.AUTO,
        )


def test_missing_tool_is_unavailable(tmp_path: Path) -> None:
    missing = CodeGraphToolResolution(error="not installed")
    with mock.patch.object(codegraph, "resolve_codegraph_tool", return_value=missing):
        status = inspect_codegraph_project(tmp_path, mode="auto")
    assert status.state == "unavailable"
    assert status.last_error == "not installed"


def test_symlinked_project_index_fails_closed_without_codegraph_probe(tmp_path: Path) -> None:
    external = tmp_path.parent / f"{tmp_path.name}-external-index"
    external.mkdir()
    try:
        (tmp_path / ".codegraph").symlink_to(external, target_is_directory=True)
        with (
            mock.patch.object(
                codegraph,
                "resolve_codegraph_tool",
                side_effect=AssertionError("must not probe tool"),
            ),
            mock.patch.object(
                codegraph,
                "_run_process_separate",
                side_effect=AssertionError("must not run status"),
            ),
        ):
            auto = inspect_codegraph_project(tmp_path, mode="auto")
            required = inspect_codegraph_project(tmp_path, mode="required")
        assert auto.state == "degraded" and auto.worktree_mismatch
        assert required.state == "failed" and required.worktree_mismatch

        with mock.patch.object(
            codegraph,
            "_run_process",
            side_effect=AssertionError("must not init or sync"),
        ):
            assert initialize_codegraph_project(tmp_path, mode="auto").worktree_mismatch
            assert sync_codegraph_project(tmp_path, mode="auto").worktree_mismatch
            with pytest.raises(CodeGraphContractError, match="符号链接"):
                initialize_codegraph_project(tmp_path, mode="required")
            with pytest.raises(CodeGraphContractError, match="符号链接"):
                sync_codegraph_project(tmp_path, mode="required")
    finally:
        external.rmdir()


@pytest.mark.parametrize("name", ["codegraph.db", "codegraph.db-wal", "codegraph.db-shm"])
def test_symlinked_index_database_member_fails_closed_before_cli(tmp_path: Path, name: str) -> None:
    index_dir = tmp_path / ".codegraph"
    index_dir.mkdir()
    external = tmp_path.parent / f"{tmp_path.name}-{name.replace('.', '-')}-external"
    external.write_text("not a project index", encoding="utf-8")
    try:
        (index_dir / name).symlink_to(external)
        with (
            mock.patch.object(
                codegraph,
                "resolve_codegraph_tool",
                side_effect=AssertionError("must not probe tool"),
            ),
            mock.patch.object(
                codegraph,
                "_run_process_separate",
                side_effect=AssertionError("must not run status"),
            ),
        ):
            auto = inspect_codegraph_project(tmp_path, mode="auto")
            required = inspect_codegraph_project(tmp_path, mode="required")
        assert auto.state == "degraded" and auto.worktree_mismatch
        assert required.state == "failed" and required.worktree_mismatch
        assert name in auto.last_error

        with mock.patch.object(
            codegraph,
            "_run_process",
            side_effect=AssertionError("must not init, sync, or query"),
        ):
            assert initialize_codegraph_project(tmp_path, mode="auto").state == "degraded"
            assert sync_codegraph_project(tmp_path, mode="auto").state == "degraded"
            with pytest.raises(CodeGraphUnavailable):
                execute_readonly_query(tmp_path, "explore", ["callers"])
    finally:
        external.unlink()


def test_project_status_is_authoritative_over_newer_legacy_status(tmp_path: Path) -> None:
    workflow_root = tmp_path / ".tmux_workflow"
    current_path = workflow_root / "codegraph.status.json"
    legacy_path = workflow_root / "old-requirement" / "codegraph.status.json"
    legacy_path.parent.mkdir(parents=True)
    (tmp_path / ".codegraph").mkdir()
    current_path.write_text(
        json.dumps({"mode": "auto", "state": "ready", "initialized": True}),
        encoding="utf-8",
    )
    legacy_path.write_text(
        json.dumps({"mode": "auto", "state": "failed", "initialized": False}),
        encoding="utf-8",
    )
    current_mtime = current_path.stat().st_mtime
    os.utime(legacy_path, (current_mtime + 10, current_mtime + 10))

    status = read_codegraph_project_status(tmp_path)

    assert status is not None
    assert status["state"] == "ready"
    assert status["initialized"] is True


def test_persisted_ready_status_degrades_when_project_index_is_missing(tmp_path: Path) -> None:
    workflow_root = tmp_path / ".tmux_workflow"
    workflow_root.mkdir()
    (workflow_root / "codegraph.status.json").write_text(
        json.dumps(
            {
                "mode": "auto",
                "state": "ready",
                "initialized": True,
                "freshness": "fresh",
                "node_count": 99,
            }
        ),
        encoding="utf-8",
    )

    status = read_codegraph_project_status(tmp_path)

    assert status is not None
    assert status["state"] == "unavailable"
    assert status["initialized"] is False
    assert status["freshness"] == "unknown"
    assert "索引目录不存在" in str(status["last_error"])


def test_persisted_ready_status_rejects_symlinked_cross_worktree_index(tmp_path: Path) -> None:
    workflow_root = tmp_path / ".tmux_workflow"
    workflow_root.mkdir()
    external_index = tmp_path.parent / f"{tmp_path.name}-external-codegraph"
    external_index.mkdir()
    try:
        (tmp_path / ".codegraph").symlink_to(external_index, target_is_directory=True)
        (workflow_root / "codegraph.status.json").write_text(
            json.dumps({"mode": "auto", "state": "ready", "initialized": True}),
            encoding="utf-8",
        )

        status = read_codegraph_project_status(tmp_path)

        assert status is not None
        assert status["state"] == "unavailable"
        assert status["initialized"] is False
    finally:
        external_index.rmdir()


def test_sync_runs_only_codegraph_and_required_failure_is_fatal(tmp_path: Path) -> None:
    before = codegraph.CodeGraphProjectStatus(mode="required", state="stale", initialized=True, freshness="stale")
    after = codegraph.CodeGraphProjectStatus(mode="required", state="ready", initialized=True, freshness="fresh")
    calls: list[list[str]] = []

    def run(argv: list[str], **_: object) -> tuple[int, str, bool]:
        calls.append(argv)
        return 0, "ok", False

    with (
        mock.patch.object(codegraph, "inspect_codegraph_project", side_effect=[before, after]),
        mock.patch.object(codegraph, "resolve_codegraph_tool", return_value=_tool()),
        mock.patch.object(codegraph, "_run_process", side_effect=run),
    ):
        status = sync_codegraph_project(tmp_path, mode="required")
    assert status.state == "ready"
    assert calls == [["/tmp/codegraph", "sync", str(tmp_path.resolve())]]
    assert not any(any(name in part.lower() for name in ("codex", "claude", "deveco")) for part in calls[0])

    with (
        mock.patch.object(codegraph, "inspect_codegraph_project", return_value=before),
        mock.patch.object(codegraph, "resolve_codegraph_tool", return_value=_tool()),
        mock.patch.object(codegraph, "_run_process", return_value=(2, "boom", False)),
    ):
        with pytest.raises(codegraph.CodeGraphSyncFailed):
            sync_codegraph_project(tmp_path, mode="required")
    persisted_failure = read_codegraph_project_status(tmp_path)
    assert persisted_failure and persisted_failure["state"] == "failed"

    stale_after = codegraph.CodeGraphProjectStatus(
        mode="required", state="stale", initialized=True, freshness="stale"
    )
    with (
        mock.patch.object(codegraph, "inspect_codegraph_project", side_effect=[before, stale_after]),
        mock.patch.object(codegraph, "resolve_codegraph_tool", return_value=_tool()),
        mock.patch.object(codegraph, "_run_process", return_value=(0, "ok", False)),
    ):
        with pytest.raises(codegraph.CodeGraphSyncFailed, match="stale"):
            sync_codegraph_project(tmp_path, mode="required")


def test_ready_sync_is_deduplicated_after_project_lock(tmp_path: Path) -> None:
    ready = codegraph.CodeGraphProjectStatus(
        mode="auto", state="ready", initialized=True, freshness="fresh"
    )
    with (
        mock.patch.object(codegraph, "inspect_codegraph_project", return_value=ready),
        mock.patch.object(codegraph, "_run_process", side_effect=AssertionError("must not sync")),
    ):
        assert sync_codegraph_project(tmp_path, mode="auto") is ready


def test_parallel_syncs_share_one_project_operation(tmp_path: Path) -> None:
    stale = codegraph.CodeGraphProjectStatus(
        mode="auto", state="stale", initialized=True, freshness="stale"
    )
    ready = codegraph.CodeGraphProjectStatus(
        mode="auto", state="ready", initialized=True, freshness="fresh"
    )
    synchronized = threading.Event()
    calls: list[int] = []

    def inspect(*_: object, **__: object) -> codegraph.CodeGraphProjectStatus:
        return ready if synchronized.is_set() else stale

    def run(*_: object, **__: object) -> tuple[int, str, bool]:
        calls.append(1)
        time.sleep(0.05)
        synchronized.set()
        return 0, "ok", False

    results: list[codegraph.CodeGraphProjectStatus] = []
    with (
        mock.patch.object(codegraph, "inspect_codegraph_project", side_effect=inspect),
        mock.patch.object(codegraph, "resolve_codegraph_tool", return_value=_tool()),
        mock.patch.object(codegraph, "_run_process", side_effect=run),
    ):
        threads = [
            threading.Thread(target=lambda: results.append(sync_codegraph_project(tmp_path, mode="auto")))
            for _ in range(2)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2)
    assert len(results) == 2
    assert calls == [1]
    assert all(result.state == "ready" for result in results)

def test_auto_sync_failure_is_degraded_without_rethrow(tmp_path: Path) -> None:
    stale = codegraph.CodeGraphProjectStatus(
        mode="auto", state="stale", initialized=True, freshness="stale"
    )
    with (
        mock.patch.object(codegraph, "inspect_codegraph_project", side_effect=[stale, stale]),
        mock.patch.object(codegraph, "resolve_codegraph_tool", return_value=_tool()),
        mock.patch.object(codegraph, "_run_process", return_value=(2, "boom", False)),
    ):
        status = sync_codegraph_project(tmp_path, mode="auto")
    assert status.state == "degraded"
    assert status.initialized
    assert "保留当前索引" in status.last_error
    assert read_codegraph_project_status(tmp_path)["state"] == "degraded"


@pytest.mark.parametrize("failure", [subprocess.TimeoutExpired(["codegraph"], 1), OSError("no process")])
def test_sync_process_failures_persist_terminal_state(tmp_path: Path, failure: BaseException) -> None:
    stale = codegraph.CodeGraphProjectStatus(
        mode="auto", state="stale", initialized=True, freshness="stale"
    )
    with (
        mock.patch.object(codegraph, "inspect_codegraph_project", return_value=stale),
        mock.patch.object(codegraph, "resolve_codegraph_tool", return_value=_tool()),
        mock.patch.object(codegraph, "_run_process", side_effect=failure),
    ):
        auto = sync_codegraph_project(tmp_path, mode="auto")
    assert auto.state == "degraded"
    assert read_codegraph_project_status(tmp_path)["state"] == "degraded"

    with (
        mock.patch.object(codegraph, "inspect_codegraph_project", return_value=stale),
        mock.patch.object(codegraph, "resolve_codegraph_tool", return_value=_tool()),
        mock.patch.object(codegraph, "_run_process", side_effect=failure),
    ):
        with pytest.raises(codegraph.CodeGraphSyncFailed):
            sync_codegraph_project(tmp_path, mode="required")
    persisted = read_codegraph_project_status(tmp_path)
    assert persisted and persisted["state"] == "failed" and persisted["mode"] == "required"


def test_init_process_failure_persists_mode_specific_terminal_state(tmp_path: Path) -> None:
    unavailable = codegraph.CodeGraphProjectStatus(
        mode="auto", state="unavailable", initialized=False, freshness="unknown"
    )
    with (
        mock.patch.object(codegraph, "inspect_codegraph_project", return_value=unavailable),
        mock.patch.object(codegraph, "resolve_codegraph_tool", return_value=_tool()),
        mock.patch.object(codegraph, "_run_process", side_effect=OSError("spawn failed")),
    ):
        status = initialize_codegraph_project(tmp_path, mode="auto")
    assert status.state == "degraded"
    assert read_codegraph_project_status(tmp_path)["state"] == "degraded"

    with (
        mock.patch.object(codegraph, "inspect_codegraph_project", return_value=unavailable),
        mock.patch.object(codegraph, "resolve_codegraph_tool", return_value=_tool()),
        mock.patch.object(codegraph, "_run_process", side_effect=OSError("spawn failed")),
    ):
        with pytest.raises(codegraph.CodeGraphSyncFailed):
            initialize_codegraph_project(tmp_path, mode="required")
    assert read_codegraph_project_status(tmp_path)["state"] == "failed"


def test_init_nonzero_exit_persists_auto_degraded(tmp_path: Path) -> None:
    unavailable = codegraph.CodeGraphProjectStatus(
        mode="auto", state="unavailable", initialized=False, freshness="unknown"
    )
    with (
        mock.patch.object(codegraph, "inspect_codegraph_project", return_value=unavailable),
        mock.patch.object(codegraph, "resolve_codegraph_tool", return_value=_tool()),
        mock.patch.object(codegraph, "_run_process", return_value=(9, "bad init", False)),
    ):
        status = initialize_codegraph_project(tmp_path, mode="auto")
    assert status.state == "degraded"
    persisted = read_codegraph_project_status(tmp_path)
    assert persisted and persisted["state"] == "degraded" and "bad init" in persisted["last_error"]


@pytest.mark.parametrize("empty_local_index", [False, True])
def test_init_allows_child_local_index_when_status_found_only_ancestor(
    tmp_path: Path,
    empty_local_index: bool,
) -> None:
    if empty_local_index:
        (tmp_path / ".codegraph").mkdir()
    ancestor = codegraph.CodeGraphProjectStatus(
        mode="auto",
        state="degraded",
        initialized=True,
        freshness="unknown",
        worktree_mismatch=True,
        last_error="projectPath mismatch",
    )
    ready = codegraph.CodeGraphProjectStatus(
        mode="auto",
        state="ready",
        initialized=True,
        freshness="fresh",
        version=CODEGRAPH_VERSION,
    )
    with (
        mock.patch.object(codegraph, "inspect_codegraph_project", side_effect=[ancestor, ready]),
        mock.patch.object(codegraph, "resolve_codegraph_tool", return_value=_tool()),
        mock.patch.object(codegraph, "_run_process", return_value=(0, "ok", False)) as run,
    ):
        status = initialize_codegraph_project(tmp_path, mode="auto")
    assert status is ready
    run.assert_called_once()


def test_init_missing_tool_is_auto_fail_soft_and_required_persists_failed(tmp_path: Path) -> None:
    missing = CodeGraphToolResolution(error="missing CodeGraph")
    unavailable_auto = codegraph.CodeGraphProjectStatus(
        mode="auto",
        state="unavailable",
        initialized=False,
        last_error="missing CodeGraph",
    )
    with (
        mock.patch.object(codegraph, "inspect_codegraph_project", return_value=unavailable_auto),
        mock.patch.object(codegraph, "resolve_codegraph_tool", return_value=missing),
        mock.patch.object(codegraph, "_run_process", side_effect=AssertionError("must not init")),
    ):
        status = initialize_codegraph_project(tmp_path, mode="auto")
    assert status.state == "unavailable"
    persisted = read_codegraph_project_status(tmp_path)
    assert persisted and persisted["state"] == "unavailable"

    unavailable_required = codegraph.CodeGraphProjectStatus(
        mode="required",
        state="unavailable",
        initialized=False,
        last_error="missing CodeGraph",
    )
    with (
        mock.patch.object(codegraph, "inspect_codegraph_project", return_value=unavailable_required),
        mock.patch.object(codegraph, "resolve_codegraph_tool", return_value=missing),
    ):
        with pytest.raises(CodeGraphUnavailable, match="missing CodeGraph"):
            initialize_codegraph_project(tmp_path, mode="required")
    persisted = read_codegraph_project_status(tmp_path)
    assert persisted and persisted["state"] == "failed" and persisted["mode"] == "required"


def test_auto_uninitialized_does_not_run_init_or_sync(tmp_path: Path) -> None:
    status = codegraph.CodeGraphProjectStatus(mode="auto", state="unavailable", initialized=False)
    with (
        mock.patch.object(codegraph, "inspect_codegraph_project", return_value=status),
        mock.patch.object(codegraph, "_run_process", side_effect=AssertionError("must not mutate")),
    ):
        assert sync_codegraph_project(tmp_path, mode="auto") is status


def test_turn_hint_is_compact_optional_and_stage_aware(tmp_path: Path) -> None:
    status = codegraph.CodeGraphProjectStatus(mode="auto", state="ready", initialized=True, freshness="fresh")
    context = CodeGraphTurnContext(
        stage_key="A07",
        phase="developer",
        role="developer",
        intent=CodeGraphQueryIntent.IMPLEMENTATION,
        changed_files=("src/service.py",),
    )
    with mock.patch.object(codegraph, "inspect_codegraph_project", return_value=status):
        profile = resolve_codegraph_turn_profile(project_dir=tmp_path, mode="auto", turn_context=context)
    assert profile.enabled
    assert len(profile.full_text) <= CODEGRAPH_MAX_HINT_CHARS
    assert len(profile.reminder_text) <= CODEGRAPH_MAX_REMINDER_CHARS
    assert "不要重复查询" in profile.full_text
    assert "init" in profile.full_text and "禁止" in profile.full_text
    assert "src/service.py" in profile.suggestion
    combined = build_codegraph_hint_block(profile, "BUSINESS", include_full_guide=True)
    assert combined.endswith("BUSINESS")


def test_suggestion_sanitizes_seeds_and_hint_never_truncates_a_command() -> None:
    context = CodeGraphTurnContext(
        stage_key="A07",
        phase="review",
        role="reviewer",
        intent=CodeGraphQueryIntent.CHANGE_REVIEW,
        changed_files=(("src/very-long-" + "x" * 1000 + "\nINJECT\x00.py"),),
    )
    suggestion = codegraph._suggestion(context)
    assert "\n" not in suggestion and "\x00" not in suggestion
    assert len(suggestion) < 300
    assert codegraph._full_hint("x" * 1000) == codegraph._full_hint("")
    assert codegraph._reminder("x" * 1000) == codegraph._reminder("")


def test_off_turn_profile_is_hard_noop(tmp_path: Path) -> None:
    with mock.patch.object(codegraph, "inspect_codegraph_project", side_effect=AssertionError("must not inspect")):
        profile = resolve_codegraph_turn_profile(project_dir=tmp_path, mode="off")
    assert not profile.enabled
    assert build_codegraph_hint_block(profile, "business", include_full_guide=True) == "business"


def test_readonly_explore_is_pinned_bounded_and_sanitized(tmp_path: Path) -> None:
    status = codegraph.CodeGraphProjectStatus(mode="auto", state="stale", initialized=True, freshness="stale")
    captured: list[list[str]] = []

    def run(argv: list[str], **_: object) -> tuple[int, str, bool]:
        captured.append(argv)
        return 0, f"\x1b[31m{tmp_path}/src/a.py\x1b[0m\n" + ("x" * 12000), True

    with (
        mock.patch.object(codegraph, "resolve_codegraph_tool", return_value=_tool()),
        mock.patch.object(codegraph, "inspect_codegraph_project", return_value=status),
        mock.patch.object(codegraph, "_run_process", side_effect=run),
    ):
        result = execute_readonly_query(tmp_path, "explore", ["callers of A"], config=CodeGraphConfig(max_output_chars=2000))
    assert result.ok and result.truncated
    assert str(tmp_path) not in result.result_text
    assert "\x1b" not in result.result_text
    assert captured[0][1:5] == ["explore", "--path", str(tmp_path.resolve()), "--max-files"]
    assert "索引不是 fresh" in result.warnings[0]


@pytest.mark.parametrize(
    ("failure", "error_kind"),
    [
        (OSError("spawn failed"), "spawn_failed"),
        (subprocess.SubprocessError("query failed"), "query_failed"),
        (subprocess.TimeoutExpired(["codegraph"], 1), "timeout"),
    ],
)
def test_readonly_query_process_errors_return_bounded_failure(
    tmp_path: Path,
    failure: BaseException,
    error_kind: str,
) -> None:
    status = codegraph.CodeGraphProjectStatus(
        mode="auto", state="ready", initialized=True, freshness="fresh"
    )
    with (
        mock.patch.object(codegraph, "resolve_codegraph_tool", return_value=_tool()),
        mock.patch.object(codegraph, "inspect_codegraph_project", return_value=status),
        mock.patch.object(codegraph, "_run_process", side_effect=failure),
    ):
        result = execute_readonly_query(tmp_path, "explore", ["callers of A"])
    assert not result.ok
    assert result.error_kind == error_kind
    assert result.result_text == ""


def test_query_output_redacts_windows_and_unc_paths(tmp_path: Path) -> None:
    cleaned = codegraph._sanitize_query_output(
        '"C:\\Users\\alice smith\\secret.py" '
        '"\\\\server\\share\\private folder\\index.db" '
        "file:///Users/alice/private%20file.py safe.py",
        project=tmp_path,
    )
    assert "alice smith" not in cleaned
    assert "private folder" not in cleaned
    assert "file:" not in cleaned
    assert cleaned.count("<redacted-path>") == 3
    assert "safe.py" in cleaned

    error = codegraph._sanitize_error(
        'failed at "C:\\Users\\alice smith\\secret.py" and file:///tmp/private.db'
    )
    assert "alice smith" not in error and "file:" not in error


def test_readonly_rejects_writes_option_injection_and_cross_project(tmp_path: Path) -> None:
    with pytest.raises(CodeGraphUnavailable, match="只读"):
        execute_readonly_query(tmp_path, "sync", [])
    status = codegraph.CodeGraphProjectStatus(mode="auto", state="ready", initialized=True, freshness="fresh")
    with (
        mock.patch.object(codegraph, "resolve_codegraph_tool", return_value=_tool()),
        mock.patch.object(codegraph, "inspect_codegraph_project", return_value=status),
    ):
        with pytest.raises(CodeGraphContractError, match="查询文本"):
            execute_readonly_query(tmp_path, "explore", ["--path /tmp/other"])

    other = tmp_path / "other"
    other.mkdir()
    with mock.patch.dict(
        os.environ,
        {"TMUX_CODEGRAPH_PROJECT_DIR": str(tmp_path), "TMUX_CODEGRAPH_READ_ONLY": "1"},
        clear=False,
    ):
        assert cli_main(["--project", str(other), "status"]) == 2


@pytest.mark.parametrize(
    "status",
    [
        codegraph.CodeGraphProjectStatus(
            mode="auto",
            state="degraded",
            initialized=True,
            worktree_mismatch=True,
            last_error="wrong worktree",
        ),
        codegraph.CodeGraphProjectStatus(
            mode="auto",
            state="degraded",
            initialized=True,
            index_state="partial",
            last_error="partial index",
        ),
    ],
)
def test_readonly_query_rejects_unsafe_index_without_running_tool(
    tmp_path: Path,
    status: codegraph.CodeGraphProjectStatus,
) -> None:
    with (
        mock.patch.object(codegraph, "resolve_codegraph_tool", return_value=_tool()),
        mock.patch.object(codegraph, "inspect_codegraph_project", return_value=status),
        mock.patch.object(
            codegraph,
            "_run_process",
            side_effect=AssertionError("must not query unsafe index"),
        ),
    ):
        with pytest.raises(CodeGraphUnavailable):
            execute_readonly_query(tmp_path, "explore", ["callers"])


def test_manual_cli_project_is_not_pinned_by_worker_project_env(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    other = tmp_path / "other"
    other.mkdir()
    status = codegraph.CodeGraphProjectStatus(mode="auto", state="ready", initialized=True)
    with (
        mock.patch.dict(
            os.environ,
            {"TMUX_CODEGRAPH_PROJECT_DIR": str(tmp_path), "TMUX_CODEGRAPH_READ_ONLY": "0"},
            clear=False,
        ),
        mock.patch.object(codegraph, "inspect_codegraph_project", return_value=status) as inspect,
    ):
        assert cli_main(["--project", str(other), "status"]) == 0
    assert capsys.readouterr().out
    assert inspect.call_args.args[0] == other.resolve()


def test_readonly_cli_refuses_maintenance_in_worker_environment(tmp_path: Path) -> None:
    with mock.patch.dict(
        os.environ,
        {"TMUX_CODEGRAPH_PROJECT_DIR": str(tmp_path), "TMUX_CODEGRAPH_READ_ONLY": "1"},
        clear=False,
    ):
        with pytest.raises(SystemExit):
            cli_main(["sync"])


def test_text_and_json_query_envelopes_do_not_store_query(tmp_path: Path) -> None:
    result = codegraph.CodeGraphQueryResult(
        ok=True,
        query_id="qid",
        command="explore",
        freshness="fresh",
        truncated=False,
        duration_ms=1,
        result_text="answer",
    )
    with mock.patch.object(codegraph, "execute_readonly_query", return_value=result):
        text = run_readonly_query(tmp_path, "explore", ["secret question"])
        payload = json.loads(run_readonly_query(tmp_path, "explore", ["secret question"], output_format="json"))
    assert "secret question" not in text
    assert "secret question" not in json.dumps(payload)
    assert payload["schema"] == "tmux-codegraph-query-result/1"


def test_project_preference_is_atomic_and_scoped(tmp_path: Path) -> None:
    assert read_codegraph_project_preference(tmp_path) == ""
    write_codegraph_project_preference(tmp_path, "off")
    assert read_codegraph_project_preference(tmp_path) == "off"
    payload = json.loads((tmp_path / ".tmux_workflow" / "codegraph.preference.json").read_text(encoding="utf-8"))
    assert payload == {"schema": "tmux-codegraph-preference/1", "mode": "off"}


def test_bridge_status_reader_uses_persisted_state_without_cli_probe(tmp_path: Path) -> None:
    status_path = tmp_path / ".tmux_workflow" / "codegraph.status.json"
    status_path.parent.mkdir()
    status_path.write_text(
        json.dumps({"mode": "off", "state": "off", "initialized": False}),
        encoding="utf-8",
    )
    with mock.patch.object(codegraph, "inspect_codegraph_project", side_effect=AssertionError("must not probe")):
        payload = read_codegraph_project_status(tmp_path)
    assert payload == {"mode": "off", "state": "off", "initialized": False}


def test_process_timeout_terminates_only_owned_process() -> None:
    started = time.monotonic()
    with pytest.raises(Exception):
        codegraph._run_process(
            ["/bin/sh", "-c", "sleep 5"],
            timeout_sec=0.1,
            max_output_chars=1000,
        )
    assert time.monotonic() - started < 3
    assert not codegraph._ACTIVE_PROCESSES


@pytest.mark.parametrize("runner_name", ["_run_process", "_run_process_separate"])
def test_owned_process_never_inherits_parent_stdin(runner_name: str) -> None:
    captured: dict[str, object] = {}

    class FakeProcess:
        pid = 12345
        returncode = 0
        stdout = io.StringIO("")
        stderr = io.StringIO("")

        def wait(self, **_: object) -> int:
            return 0

        def poll(self) -> int:
            return 0

    def popen(*_: object, **kwargs: object) -> FakeProcess:
        captured.update(kwargs)
        return FakeProcess()

    with mock.patch.object(codegraph.subprocess, "Popen", side_effect=popen):
        getattr(codegraph, runner_name)(["codegraph", "status"], timeout_sec=1)
    assert captured["stdin"] is subprocess.DEVNULL


@pytest.mark.parametrize(
    ("runner_name", "error"),
    [
        ("_run_process", KeyboardInterrupt()),
        ("_run_process_separate", SystemExit("stop")),
    ],
)
def test_owned_process_is_terminated_before_unregister_on_base_exception(
    runner_name: str,
    error: BaseException,
) -> None:
    class FakeProcess:
        pid = 12345
        returncode = None
        stdout = io.StringIO("")
        stderr = io.StringIO("")

        def wait(self, **_: object) -> int:
            raise error

        def poll(self) -> None:
            return None

    process = FakeProcess()
    runner = getattr(codegraph, runner_name)
    with (
        mock.patch.object(codegraph.subprocess, "Popen", return_value=process),
        mock.patch.object(codegraph, "_terminate_process_group") as terminate,
    ):
        with pytest.raises(type(error)):
            runner(["codegraph", "status"], timeout_sec=1, max_output_chars=1000)
    terminate.assert_called_once_with(process)
    assert process not in codegraph._ACTIVE_PROCESSES


def test_separate_output_process_is_owned_and_cancellable() -> None:
    result: list[tuple[int, str, str, bool]] = []

    def run() -> None:
        result.append(
            codegraph._run_process_separate(
                ["/bin/sh", "-c", "printf '{\"started\":true}'; sleep 5"],
                timeout_sec=10,
                max_output_chars=1000,
            )
        )

    thread = threading.Thread(target=run)
    thread.start()
    deadline = time.monotonic() + 2
    while not codegraph._ACTIVE_PROCESSES and time.monotonic() < deadline:
        time.sleep(0.01)
    assert codegraph._ACTIVE_PROCESSES
    codegraph.cancel_codegraph_processes()
    thread.join(timeout=3)
    assert not thread.is_alive()
    assert result and result[0][0] != 0
    assert not codegraph._ACTIVE_PROCESSES
    # Backend and legacy A00 finally paths may both perform best-effort cleanup.
    codegraph.cancel_codegraph_processes()
    assert not codegraph._ACTIVE_PROCESSES


def test_recovery_guard_is_scoped_per_project(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    entered = threading.Event()
    release = threading.Event()

    def hold_first() -> None:
        with codegraph.codegraph_required_recovery_guard(first):
            entered.set()
            release.wait(timeout=2)

    thread = threading.Thread(target=hold_first)
    thread.start()
    assert entered.wait(timeout=1)
    started = time.monotonic()
    with codegraph.codegraph_required_recovery_guard(second):
        pass
    assert time.monotonic() - started < 0.5
    release.set()
    thread.join(timeout=2)
    assert not thread.is_alive()


def test_managed_setup_rechecks_existing_tool_before_download() -> None:
    with (
        mock.patch.object(codegraph, "resolve_codegraph_tool", return_value=_tool()) as resolve,
        mock.patch.object(codegraph, "_manifest", side_effect=AssertionError("must not download")),
    ):
        result = codegraph.setup_managed_codegraph()
    assert result.compatible
    resolve.assert_called_once_with(force_refresh=True)


def test_managed_setup_wraps_download_failure_and_publishes_nothing(tmp_path: Path) -> None:
    target = "darwin-arm64"
    manifest = {
        "assets": {
            target: {
                "url": "https://github.com/colbymchenry/codegraph/releases/download/v1.5.0/codegraph.zip",
                "sha256": "0" * 64,
            }
        }
    }
    unavailable = CodeGraphToolResolution(error="missing")
    with (
        mock.patch.dict(os.environ, {"XDG_DATA_HOME": str(tmp_path)}),
        mock.patch.object(codegraph, "resolve_codegraph_tool", return_value=unavailable),
        mock.patch.object(codegraph, "_platform_target", return_value=target),
        mock.patch.object(codegraph, "_manifest", return_value=manifest),
        mock.patch.object(
            codegraph.urllib.request,
            "urlopen",
            side_effect=urllib.error.URLError("offline"),
        ),
    ):
        with pytest.raises(CodeGraphUnavailable, match="托管安装失败"):
            codegraph.setup_managed_codegraph()
    destination = tmp_path / "tmux_coding_team" / "tools" / "codegraph" / CODEGRAPH_VERSION / target
    assert not destination.exists()
    assert not list(destination.parent.glob(".*.staging"))


def test_managed_setup_reuses_compatible_concurrent_publish(tmp_path: Path) -> None:
    target = "darwin-arm64"
    archive_bytes = b"fixed release archive"
    expected = hashlib.sha256(archive_bytes).hexdigest()
    manifest = {
        "assets": {
            target: {
                "url": "https://github.com/colbymchenry/codegraph/releases/download/v1.5.0/codegraph.zip",
                "sha256": expected,
            }
        }
    }
    destination = tmp_path / "tmux_coding_team" / "tools" / "codegraph" / CODEGRAPH_VERSION / target
    executable = destination / "bin" / "codegraph"
    concurrent = CodeGraphToolResolution(
        executable_path=str(executable),
        version=CODEGRAPH_VERSION,
        source="managed",
        compatible=True,
    )
    original_move = codegraph.shutil.move

    def extract(_: Path, extraction: Path) -> None:
        package = extraction / "package" / "bin"
        package.mkdir(parents=True)
        (package / "codegraph").write_bytes(b"our staging binary")

    def race_move(source: str, staging: str) -> str:
        result = original_move(source, staging)
        executable.parent.mkdir(parents=True)
        executable.write_bytes(b"concurrent compatible binary")
        executable.chmod(0o755)
        return result

    with (
        mock.patch.dict(os.environ, {"XDG_DATA_HOME": str(tmp_path)}),
        mock.patch.object(codegraph, "_platform_target", return_value=target),
        mock.patch.object(codegraph, "_manifest", return_value=manifest),
        mock.patch.object(codegraph.urllib.request, "urlopen", return_value=io.BytesIO(archive_bytes)),
        mock.patch.object(codegraph, "_extract_release_archive", side_effect=extract),
        mock.patch.object(codegraph.shutil, "move", side_effect=race_move),
        mock.patch.object(
            codegraph,
            "resolve_codegraph_tool",
            side_effect=[CodeGraphToolResolution(error="missing"), concurrent],
        ) as resolve,
    ):
        result = codegraph.setup_managed_codegraph()
    assert result is concurrent
    assert executable.read_bytes() == b"concurrent compatible binary"
    assert resolve.call_count == 2
    assert not list(destination.parent.glob(".*.staging"))


def test_release_extract_rejects_zip_symlink_and_tar_special_member(tmp_path: Path) -> None:
    destination = tmp_path / "destination"
    destination.mkdir()
    zip_path = tmp_path / "release.zip"
    link = zipfile.ZipInfo("codegraph-link")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(zip_path, "w") as bundle:
        bundle.writestr(link, "target")
    with pytest.raises(CodeGraphContractError, match="特殊成员"):
        codegraph._extract_release_archive(zip_path, destination)

    tar_path = tmp_path / "release.tar.gz"
    special = tarfile.TarInfo("codegraph-fifo")
    special.type = tarfile.FIFOTYPE
    with tarfile.open(tar_path, "w:gz") as bundle:
        bundle.addfile(special)
    with pytest.raises(CodeGraphContractError, match="特殊成员"):
        codegraph._extract_release_archive(tar_path, destination)


def test_release_extract_rejects_zip_and_tar_bomb_metadata(tmp_path: Path) -> None:
    destination = tmp_path / "destination"
    destination.mkdir()
    zip_path = tmp_path / "bomb.zip"
    with zipfile.ZipFile(zip_path, "w") as bundle:
        bundle.writestr("oversized.bin", b"x")
    with zipfile.ZipFile(zip_path) as bundle:
        member = bundle.infolist()[0]
        member.file_size = codegraph.CODEGRAPH_MAX_ARCHIVE_MEMBER_BYTES + 1
        with mock.patch.object(bundle, "infolist", return_value=[member]):
            with pytest.raises(CodeGraphContractError, match="单成员"):
                codegraph._validate_archive_sizes([member.file_size])

    tar_path = tmp_path / "bomb.tar.gz"
    oversized = tarfile.TarInfo("oversized.bin")
    oversized.size = codegraph.CODEGRAPH_MAX_ARCHIVE_MEMBER_BYTES + 1
    with mock.patch.object(tarfile, "open") as open_tar:
        open_tar.return_value.__enter__.return_value.getmembers.return_value = [oversized]
        with pytest.raises(CodeGraphContractError, match="单成员"):
            codegraph._extract_release_archive(tar_path, destination)

    with pytest.raises(CodeGraphContractError, match="解压总量"):
        codegraph._validate_archive_sizes(
            [codegraph.CODEGRAPH_MAX_ARCHIVE_MEMBER_BYTES]
            * (codegraph.CODEGRAPH_MAX_EXTRACTED_BYTES // codegraph.CODEGRAPH_MAX_ARCHIVE_MEMBER_BYTES + 1)
        )


def test_extract_permissions_are_normalized(tmp_path: Path) -> None:
    archive = tmp_path / "release.zip"
    executable = zipfile.ZipInfo("bin/codegraph")
    executable.create_system = 3
    executable.external_attr = (stat.S_IFREG | 0o777) << 16
    regular = zipfile.ZipInfo("README.txt")
    regular.create_system = 3
    regular.external_attr = (stat.S_IFREG | 0o777) << 16
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr(executable, b"binary")
        bundle.writestr(regular, b"readme")
    destination = tmp_path / "destination"
    destination.mkdir()
    codegraph._extract_release_archive(archive, destination)
    assert stat.S_IMODE((destination / "bin" / "codegraph").stat().st_mode) == 0o755
    assert stat.S_IMODE((destination / "README.txt").stat().st_mode) == 0o644


def test_limited_copy_rejects_oversized_stream_without_full_write() -> None:
    source = io.BytesIO(b"x" * 11)
    output = io.BytesIO()
    with pytest.raises(CodeGraphContractError, match="大小上限"):
        codegraph._copy_limited(source, output, limit=10, label="下载包")
    assert len(output.getvalue()) <= 10


def test_release_manifest_is_fixed_and_complete() -> None:
    payload = codegraph._manifest()
    assert payload["version"] == "1.5.0"
    assets = payload["assets"]
    assert set(assets) == {"darwin-arm64", "darwin-x64", "linux-arm64", "linux-x64", "win32-arm64", "win32-x64"}
    assert all(len(item["sha256"]) == 64 for item in assets.values())
