from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from tmux_core.runtime.graphify import (
    GRAPHIFY_EVIDENCE_MAX_CHARS,
    GRAPHIFY_FULL_GUIDE_MARKER,
    GRAPHIFY_QUERY_MAX_CHARS,
    GRAPHIFY_VERSION,
    GraphifyBuildConfig,
    GraphifyBuildFailed,
    GraphifyMode,
    GraphifyQueryIntent,
    GraphifyQuerySuggestion,
    GraphifySchemaError,
    GraphifySnapshotError,
    GraphifyTurnContext,
    GraphifyToolResolution,
    GraphifyUnavailable,
    GraphifyUsagePolicy,
    assess_graphify_freshness,
    assess_graphify_turn_usage,
    build_graphify_evidence_block,
    capture_graphify_source_manifest,
    cli_main,
    create_graphify_snapshot,
    execute_readonly_query,
    managed_graphify_executable,
    normalize_graphify_mode,
    project_cache_dir,
    publish_graphify_pending_status,
    read_graphify_project_status,
    register_graphify_turn_usage,
    resolve_graphify_tool,
    resolve_graphify_turn_profile,
    run_readonly_query,
    setup_managed_graphify,
)


FAKE_GRAPHIFY = r'''#!/usr/bin/env python3
import json
import os
import pathlib
import sys
import time

args = sys.argv[1:]
if args == ["--version"]:
    print("graphify " + os.environ.get("FAKE_GRAPHIFY_VERSION", "0.9.27"))
    raise SystemExit(0)
if args == ["--help"]:
    print("extract update query affected path explain god-nodes")
    raise SystemExit(0)
if any(os.environ.get(key) for key in (
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "CLOUDSDK_AUTH_ACCESS_TOKEN",
    "VERTEX_TOKEN",
)):
    print("model credential leaked", file=sys.stderr)
    raise SystemExit(91)
log_path = os.environ.get("FAKE_GRAPHIFY_LOG")
if log_path:
    with open(log_path, "a", encoding="utf-8") as handle:
        handle.write(" ".join(args) + "\n")
if os.environ.get("FAKE_GRAPHIFY_SLEEP"):
    time.sleep(float(os.environ["FAKE_GRAPHIFY_SLEEP"]))
if os.environ.get("FAKE_GRAPHIFY_FAIL"):
    print("requested fake failure", file=sys.stderr)
    raise SystemExit(9)

command = args[0]
if command in {"extract", "update"}:
    source = pathlib.Path(args[1]).resolve()
    if command == "extract" and "--out" in args:
        output = pathlib.Path(args[args.index("--out") + 1]).resolve()
    else:
        output = source
    graph_dir = output / "graphify-out"
    graph_dir.mkdir(parents=True, exist_ok=True)
    files = sorted(path for path in source.rglob("*") if path.is_file() and "graphify-out" not in path.parts)
    nodes = []
    for index, path in enumerate(files):
        nodes.append({"id": "n" + str(index), "label": path.stem, "file_path": str(path), "start_line": index + 10})
    edges = []
    if len(nodes) > 1:
        edges.append({"source": nodes[0]["id"], "target": nodes[1]["id"], "relation": "calls"})
    edge_mode = os.environ.get("FAKE_GRAPHIFY_EDGE_MODE", "")
    if edge_mode == "non_object":
        edges = ["broken-edge"]
    elif edge_mode == "no_edges":
        edges = []
    elif edge_mode == "missing_target":
        edges = [{"source": nodes[0]["id"]}]
    elif edge_mode == "unknown_target":
        edges = [{"source": nodes[0]["id"], "target": "missing-node"}]
    elif edge_mode == "compatible_aliases":
        edges = [{"from": {"id": nodes[0]["id"]}, "to": {"key": nodes[1]["id"]}}]
    edge_collection = "links" if edge_mode == "compatible_aliases" else "edges"
    graph = {"nodes": nodes, edge_collection: edges, "directed": True}
    (graph_dir / "graph.json").write_text(json.dumps(graph), encoding="utf-8")
    print("ok")
    raise SystemExit(0)
if command in {"query", "affected", "path", "explain", "god-nodes"}:
    graph_path = args[args.index("--graph") + 1]
    output = os.environ.get("FAKE_GRAPHIFY_QUERY_OUTPUT", command + " graph=" + graph_path)
    if os.environ.get("FAKE_GRAPHIFY_QUERY_STDERR"):
        print(output, file=sys.stderr)
        raise SystemExit(int(os.environ.get("FAKE_GRAPHIFY_QUERY_EXIT", "7")))
    print(output)
    raise SystemExit(0)
print("unsupported", file=sys.stderr)
raise SystemExit(2)
'''
REAL_GRAPHIFY_EXECUTABLE = str(os.environ.get("REAL_GRAPHIFY_EXECUTABLE", "") or "").strip()


class GraphifyRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.cache_home = self.root / "cache"
        self.data_home = self.root / "data"
        self.fake = self.root / "graphify"
        self.fake.write_text(FAKE_GRAPHIFY, encoding="utf-8")
        self.fake.chmod(0o755)
        self.log_path = self.root / "graphify.log"
        self.environment = mock.patch.dict(
            os.environ,
            {
                "XDG_CACHE_HOME": str(self.cache_home),
                "XDG_DATA_HOME": str(self.data_home),
                "TMUX_GRAPHIFY_EXECUTABLE": str(self.fake),
                "FAKE_GRAPHIFY_LOG": str(self.log_path),
                "OPENAI_API_KEY": "must-not-reach-child",
                "ANTHROPIC_API_KEY": "must-not-reach-child",
                "GOOGLE_APPLICATION_CREDENTIALS": "/secret/google.json",
                "CLOUDSDK_AUTH_ACCESS_TOKEN": "must-not-reach-child",
                "VERTEX_TOKEN": "must-not-reach-child",
            },
            clear=False,
        )
        self.environment.start()

    def tearDown(self) -> None:
        self.environment.stop()
        self.temp_dir.cleanup()

    def _write_sources(self) -> None:
        (self.project / "src").mkdir(exist_ok=True)
        (self.project / "src" / "alpha.py").write_text(
            "from .beta import beta\ndef alpha(): return beta()\n", encoding="utf-8"
        )
        (self.project / "src" / "beta.py").write_text("def beta(): return 1\n", encoding="utf-8")

    def _profile(self, *, mode: str = "required", prompt: str = "change alpha"):
        runtime = self.project / ".development_runtime" / "demo"
        return resolve_graphify_turn_profile(
            project_dir=self.project,
            mode=mode,
            prompt=prompt,
            runtime_dir=runtime,
        )

    def test_mode_normalization_and_invalid_value(self) -> None:
        self.assertEqual(normalize_graphify_mode(" AUTO "), GraphifyMode.AUTO)
        self.assertEqual(normalize_graphify_mode("", default=GraphifyMode.REQUIRED), GraphifyMode.REQUIRED)
        with self.assertRaisesRegex(ValueError, "off, auto, required"):
            normalize_graphify_mode("sometimes")

    def test_explicit_tool_requires_absolute_exact_version_and_contract(self) -> None:
        result = resolve_graphify_tool()
        self.assertTrue(result.compatible)
        self.assertEqual(result.version, GRAPHIFY_VERSION)
        self.assertEqual(result.source, "environment")
        with mock.patch.dict(os.environ, {"TMUX_GRAPHIFY_EXECUTABLE": "graphify"}, clear=False):
            invalid = resolve_graphify_tool()
        self.assertFalse(invalid.compatible)
        self.assertIn("绝对路径", invalid.error)
        with mock.patch.dict(os.environ, {"FAKE_GRAPHIFY_VERSION": "0.9.25"}, clear=False):
            mismatch = resolve_graphify_tool()
        self.assertFalse(mismatch.compatible)
        self.assertIn("required 0.9.27", mismatch.error)

    def test_managed_setup_builds_directly_at_final_target_without_moving_venv(self) -> None:
        target = managed_graphify_executable().parent.parent
        target.mkdir(parents=True)
        (target / "old-marker").write_text("old", encoding="utf-8")
        observed_environment: dict[str, str] = {}

        def install(_args, *, cwd, timeout_sec, environment):
            del cwd, timeout_sec
            observed_environment.update(environment)
            executable = managed_graphify_executable()
            executable.parent.mkdir(parents=True, exist_ok=True)
            executable.write_text("#!/final-target/bin/python\n", encoding="utf-8")
            executable.chmod(0o755)
            return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

        incompatible = GraphifyToolResolution(source="managed", error="broken shebang")
        compatible = GraphifyToolResolution(
            executable_path=str(managed_graphify_executable()),
            version=GRAPHIFY_VERSION,
            source="managed",
            compatible=True,
        )
        real_replace = os.replace
        with mock.patch("tmux_core.runtime.graphify.shutil.which", return_value="/usr/bin/uv"), mock.patch(
            "tmux_core.runtime.graphify._run_graphify_process", side_effect=install,
        ), mock.patch(
            "tmux_core.runtime.graphify._probe_tool", side_effect=(incompatible, compatible),
        ), mock.patch("tmux_core.runtime.graphify.os.replace", wraps=real_replace) as replace:
            result = setup_managed_graphify()
        self.assertTrue(result.compatible)
        self.assertEqual(observed_environment["UV_PROJECT_ENVIRONMENT"], str(target))
        self.assertTrue(managed_graphify_executable().is_file())
        self.assertFalse((target / "old-marker").exists())
        self.assertEqual(len(replace.call_args_list), 1)
        self.assertEqual(Path(replace.call_args.args[0]), target)
        self.assertNotEqual(Path(replace.call_args.args[1]), target)

    def test_managed_setup_failure_restores_previous_target(self) -> None:
        target = managed_graphify_executable().parent.parent
        target.mkdir(parents=True)
        marker = target / "old-marker"
        marker.write_text("keep", encoding="utf-8")
        observed_environment: dict[str, str] = {}

        def fail_install(_args, *, cwd, timeout_sec, environment):
            del cwd, timeout_sec
            observed_environment.update(environment)
            managed_graphify_executable().parent.mkdir(parents=True, exist_ok=True)
            managed_graphify_executable().write_text("broken new venv", encoding="utf-8")
            raise GraphifyBuildFailed("uv failed")

        with mock.patch("tmux_core.runtime.graphify.shutil.which", return_value="/usr/bin/uv"), mock.patch(
            "tmux_core.runtime.graphify._run_graphify_process", side_effect=fail_install,
        ), mock.patch(
            "tmux_core.runtime.graphify._probe_tool",
            return_value=GraphifyToolResolution(source="managed", error="broken shebang"),
        ):
            with self.assertRaisesRegex(GraphifyUnavailable, "uv failed"):
                setup_managed_graphify()
        self.assertEqual(observed_environment["UV_PROJECT_ENVIRONMENT"], str(target))
        self.assertEqual(marker.read_text(encoding="utf-8"), "keep")
        self.assertFalse(any(target.parent.glob(f".{GRAPHIFY_VERSION}.*.bak")))

    def test_off_does_not_probe_or_scan(self) -> None:
        with mock.patch("tmux_core.runtime.graphify.resolve_graphify_tool", side_effect=AssertionError("must not probe")):
            profile = resolve_graphify_turn_profile(
                project_dir=self.project,
                mode="off",
                prompt="ignored",
            )
        self.assertFalse(profile.enabled)
        self.assertEqual(profile.status.state, "off")
        self.assertFalse(self.log_path.exists())

    def test_safe_snapshot_excludes_secrets_runtime_dependencies_and_symlinks(self) -> None:
        self._write_sources()
        (self.project / ".env.local").write_text("TOKEN=x\n", encoding="utf-8")
        (self.project / "private.key").write_text("secret\n", encoding="utf-8")
        runtime = self.project / ".development_runtime"
        runtime.mkdir()
        (runtime / "leak.py").write_text("secret = 1\n", encoding="utf-8")
        node_modules = self.project / "node_modules" / "pkg"
        node_modules.mkdir(parents=True)
        (node_modules / "index.js").write_text("secret\n", encoding="utf-8")
        outside = self.root / "outside.py"
        outside.write_text("outside = 1\n", encoding="utf-8")
        (self.project / "linked.py").symlink_to(outside)
        destination = self.root / "snapshot" / "source"
        snapshot = create_graphify_snapshot(self.project, destination)
        manifest = json.loads(Path(snapshot.manifest_path).read_text(encoding="utf-8"))
        paths = {item["path"] for item in manifest["files"]}
        self.assertEqual(paths, {"src/alpha.py", "src/beta.py"})
        self.assertFalse((destination / "linked.py").exists())

    def test_non_git_snapshot_excludes_generated_tmp_and_binary_source_lookalikes(self) -> None:
        self._write_sources()
        for directory in (".tmp", "tmp", "generated"):
            candidate = self.project / directory
            candidate.mkdir()
            (candidate / "should_not_scan.py").write_text("SECRET = 1\n", encoding="utf-8")
        (self.project / "binary.py").write_bytes(b"def looks_like_source():\0binary")
        destination = self.root / "snapshot-generated" / "source"
        snapshot = create_graphify_snapshot(self.project, destination)
        manifest = json.loads(Path(snapshot.manifest_path).read_text(encoding="utf-8"))
        paths = {item["path"] for item in manifest["files"]}
        self.assertEqual(paths, {"src/alpha.py", "src/beta.py"})

    def test_snapshot_limits_fail_closed_without_truncating(self) -> None:
        self._write_sources()
        with self.assertRaisesRegex(GraphifySnapshotError, "文件数上限"):
            create_graphify_snapshot(
                self.project,
                self.root / "limited" / "source",
                config=GraphifyBuildConfig(max_files=1),
            )

    def test_build_cache_incremental_evidence_and_sanitized_environment(self) -> None:
        self._write_sources()
        first = self._profile()
        self.assertTrue(first.enabled)
        self.assertEqual(first.status.state, "ready")
        self.assertIn("Graphify Code Graph Evidence", first.evidence.block_text)
        self.assertIn("alpha", first.evidence.block_text)
        self.assertIn("GRAPHIFY_USAGE_POLICY", first.evidence.block_text)
        self.assertIn("evidence_reading: required", first.evidence.block_text)
        self.assertIn("query_requirement: optional", first.evidence.block_text)
        self.assertIn('"$TMUX_GRAPHIFY_CMD" query "<question>"', first.evidence.block_text)
        self.assertIn('"$TMUX_GRAPHIFY_CMD" affected "<file-or-symbol>"', first.evidence.block_text)
        self.assertIn('"$TMUX_GRAPHIFY_CMD" path "<source>" "<target>"', first.evidence.block_text)
        self.assertIn('"$TMUX_GRAPHIFY_CMD" explain "<symbol>"', first.evidence.block_text)
        self.assertIn('"$TMUX_GRAPHIFY_CMD" god-nodes', first.evidence.block_text)
        self.assertIn("otherwise query only when the evidence is insufficient", first.evidence.block_text)
        self.assertIn("verify them in AGENTS.md (when present), source, tests, and config", first.evidence.block_text)
        self.assertIn("The wrapper is read-only", first.evidence.block_text)
        self.assertNotIn(str(self.cache_home), first.evidence.block_text)
        self.assertTrue(Path(first.evidence.report_path).is_file())
        calls_after_first = self.log_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(sum(line.startswith("extract ") for line in calls_after_first), 1)

        second = self._profile(prompt="inspect beta")
        self.assertTrue(second.enabled)
        calls_after_second = self.log_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(sum(line.startswith("extract ") for line in calls_after_second), 1)
        self.assertEqual(sum(line.startswith("update ") for line in calls_after_second), 0)

        (self.project / "src" / "beta.py").write_text("def beta(): return 2\n", encoding="utf-8")
        third = self._profile(prompt="inspect beta")
        self.assertTrue(third.enabled)
        calls_after_third = self.log_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(sum(line.startswith("update ") for line in calls_after_third), 1)
        self.assertNotEqual(first.evidence.graph_fingerprint, third.evidence.graph_fingerprint)
        status = read_graphify_project_status(self.project)
        self.assertEqual(status["evidence_id"], third.evidence.evidence_id)

    def test_route_change_symbol_query_and_prompt_file_seeds_rank_graph_evidence(self) -> None:
        self._write_sources()
        source_dir = self.project / "src"
        for index in range(15):
            (source_dir / f"zz_noise_{index:02d}.py").write_text(
                f"NOISE_{index} = {index}\n",
                encoding="utf-8",
            )
        target_paths = (
            "src/a_routed.py",
            "src/b_changed.py",
            "src/c_symbol.py",
            "src/d_prompt.py",
            "src/e_query_target.py",
        )
        for relative in target_paths:
            target = self.project / relative
            target.write_text(f"def {target.stem}(): return True\n", encoding="utf-8")
        docs = self.project / "docs"
        docs.mkdir()
        (docs / "repo_map.json").write_text("not runtime input", encoding="utf-8")

        baseline = self._profile(prompt="generic request")
        for relative in target_paths:
            self.assertNotIn(relative, baseline.evidence.related_paths)

        outside = self.root / "outside.py"
        outside.write_text("OUTSIDE = True\n", encoding="utf-8")
        seeded = resolve_graphify_turn_profile(
            project_dir=self.project,
            mode="required",
            prompt="generic request; inspect src/d_prompt.py:12",
            runtime_dir=self.project / ".development_runtime" / "demo",
            refresh=False,
            routed_paths=("src/a_routed.py", outside),
            changed_files=(self.project / "src" / "b_changed.py",),
            symbols=("c_symbol",),
            query_seeds=("e_query_target",),
        )

        self.assertEqual(seeded.routed_paths, ("src/a_routed.py",))
        self.assertEqual(seeded.changed_files, ("src/b_changed.py",))
        self.assertEqual(seeded.symbols, ("c_symbol",))
        self.assertEqual(seeded.query_seeds, ("e_query_target",))
        self.assertEqual(seeded.prompt_file_references, ("src/d_prompt.py",))
        for relative in target_paths:
            self.assertIn(relative, seeded.evidence.related_paths)
        self.assertNotEqual(baseline.evidence.evidence_id, seeded.evidence.evidence_id)
        self.assertEqual(seeded.evidence.routed_module_candidates, ())
        self.assertNotIn("ROUTED_MODULE_CANDIDATES", seeded.evidence.block_text)
        self.assertIn("AI Hermes routing remains authoritative", seeded.evidence.block_text)
        self.assertNotIn(str(outside), seeded.evidence.block_text)

    def test_non_refresh_turn_reuses_generation_without_tool_probe_or_scan(self) -> None:
        self._write_sources()
        first = self._profile(prompt="alpha")
        with mock.patch(
            "tmux_core.runtime.graphify.resolve_graphify_tool",
            side_effect=AssertionError("later turn must not probe"),
        ), mock.patch(
            "tmux_core.runtime.graphify.create_graphify_snapshot",
            side_effect=AssertionError("later turn must not rescan"),
        ):
            later = resolve_graphify_turn_profile(
                project_dir=self.project,
                mode="required",
                prompt="beta",
                runtime_dir=self.project / ".development_runtime" / "demo",
                refresh=False,
            )
        self.assertTrue(later.enabled)
        self.assertEqual(first.evidence.graph_fingerprint, later.evidence.graph_fingerprint)

    def test_auto_reuses_previous_graph_and_records_refresh_error(self) -> None:
        self._write_sources()
        first = self._profile()
        (self.project / "src" / "beta.py").write_text("def beta(): return 3\n", encoding="utf-8")
        with mock.patch.dict(os.environ, {"FAKE_GRAPHIFY_FAIL": "1"}, clear=False):
            fallback = self._profile(mode="auto")
        self.assertTrue(fallback.enabled)
        self.assertEqual(fallback.status.state, "stale")
        self.assertEqual(fallback.evidence.graph_fingerprint, first.evidence.graph_fingerprint)
        self.assertIn("requested fake failure", fallback.status.last_error)

    def test_edge_schema_rejects_non_objects_missing_and_unknown_endpoints(self) -> None:
        self._write_sources()
        for edge_mode, error_pattern in (
            ("non_object", r"edge\[0\].*object"),
            ("missing_target", r"edge\[0\].*source/target"),
            ("unknown_target", r"edge\[0\].*未知节点"),
        ):
            with self.subTest(edge_mode=edge_mode), mock.patch.dict(
                os.environ,
                {"FAKE_GRAPHIFY_EDGE_MODE": edge_mode},
                clear=False,
            ):
                with self.assertRaisesRegex(GraphifySchemaError, error_pattern):
                    self._profile(mode="required")
                self.assertFalse((project_cache_dir(self.project) / "current.json").exists())

        with mock.patch.dict(
            os.environ,
            {"FAKE_GRAPHIFY_EDGE_MODE": "missing_target"},
            clear=False,
        ):
            degraded = self._profile(mode="auto")
        self.assertFalse(degraded.enabled)
        self.assertEqual(degraded.status.state, "degraded")

    def test_links_collection_and_compatible_endpoint_aliases_are_accepted(self) -> None:
        self._write_sources()
        with mock.patch.dict(
            os.environ,
            {"FAKE_GRAPHIFY_EDGE_MODE": "compatible_aliases"},
            clear=False,
        ):
            profile = self._profile(mode="required")
        self.assertTrue(profile.enabled)
        self.assertEqual(profile.status.edge_count, 1)

    def test_auto_schema_failure_preserves_previous_generation(self) -> None:
        self._write_sources()
        first = self._profile(prompt="alpha")
        (self.project / "src" / "beta.py").write_text("def beta(): return 7\n", encoding="utf-8")
        with mock.patch.dict(
            os.environ,
            {"FAKE_GRAPHIFY_EDGE_MODE": "unknown_target"},
            clear=False,
        ):
            fallback = self._profile(mode="auto", prompt="beta")
        current = json.loads(
            (project_cache_dir(self.project) / "current.json").read_text(encoding="utf-8")
        )
        self.assertTrue(fallback.enabled)
        self.assertEqual(fallback.status.state, "stale")
        self.assertEqual(fallback.evidence.graph_fingerprint, first.evidence.graph_fingerprint)
        self.assertEqual(current["fingerprint"], first.evidence.graph_fingerprint)
        self.assertIn("未知节点", fallback.status.last_error)

    def test_cached_generation_hash_corruption_falls_back_to_previous(self) -> None:
        self._write_sources()
        first = self._profile(prompt="alpha")
        (self.project / "src" / "beta.py").write_text("def beta(): return 9\n", encoding="utf-8")
        second = self._profile(prompt="beta")
        self.assertNotEqual(first.evidence.graph_fingerprint, second.evidence.graph_fingerprint)
        current_graph = (
            project_cache_dir(self.project)
            / "graphs"
            / second.evidence.graph_fingerprint
            / "graph.json"
        )
        current_graph.write_text('{"nodes": [], "edges": []}', encoding="utf-8")
        recovered = resolve_graphify_turn_profile(
            project_dir=self.project,
            mode="required",
            prompt="alpha",
            refresh=False,
        )
        self.assertTrue(recovered.enabled)
        self.assertEqual(recovered.evidence.graph_fingerprint, first.evidence.graph_fingerprint)
        self.assertEqual(recovered.evidence.freshness, "cache_fallback")

    def test_required_tool_failure_stops_but_auto_degrades(self) -> None:
        self._write_sources()
        with mock.patch.dict(os.environ, {"FAKE_GRAPHIFY_VERSION": "0.9.25"}, clear=False):
            with self.assertRaises(GraphifyUnavailable):
                self._profile(mode="required")
            auto = self._profile(mode="auto")
        self.assertFalse(auto.enabled)
        self.assertEqual(auto.status.state, "unavailable")

    def test_timeout_terminates_build_and_does_not_publish_generation(self) -> None:
        self._write_sources()
        with mock.patch.dict(os.environ, {"FAKE_GRAPHIFY_SLEEP": "2"}, clear=False):
            with self.assertRaisesRegex(Exception, "timed out"):
                resolve_graphify_turn_profile(
                    project_dir=self.project,
                    mode="required",
                    prompt="alpha",
                    config=GraphifyBuildConfig(initial_timeout_sec=0.1),
                )
        self.assertFalse((project_cache_dir(self.project) / "current.json").exists())

    def test_concurrent_builds_share_the_same_generation(self) -> None:
        self._write_sources()
        profiles = []
        errors = []

        def run() -> None:
            try:
                profiles.append(self._profile(prompt="alpha"))
            except Exception as exc:  # pragma: no cover - assertion reports it.
                errors.append(exc)

        threads = [threading.Thread(target=run), threading.Thread(target=run)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertFalse(errors)
        self.assertEqual(len(profiles), 2)
        self.assertEqual(profiles[0].evidence.graph_fingerprint, profiles[1].evidence.graph_fingerprint)
        calls = self.log_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(sum(line.startswith("extract ") for line in calls), 1)

    def test_separate_processes_share_the_file_lock(self) -> None:
        self._write_sources()
        repository = Path(__file__).resolve().parents[1]
        program = (
            "from tmux_core.runtime.graphify import resolve_graphify_turn_profile; "
            "import sys; "
            "p=resolve_graphify_turn_profile(project_dir=sys.argv[1], mode='required', prompt='alpha'); "
            "assert p.enabled"
        )
        processes = [
            subprocess.Popen(
                [sys.executable, "-c", program, str(self.project)],
                cwd=str(repository),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=dict(os.environ),
            )
            for _ in range(2)
        ]
        results = [process.communicate(timeout=20) for process in processes]
        self.assertEqual(
            [(process.returncode, stderr) for process, (_stdout, stderr) in zip(processes, results)],
            [(0, ""), (0, "")],
        )
        calls = self.log_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(sum(line.startswith("extract ") for line in calls), 1)

    def test_readonly_query_uses_current_graph_and_hides_cache_path(self) -> None:
        self._write_sources()
        self._profile()
        output = run_readonly_query(self.project, "query", ["alpha"])
        self.assertIn("query graph=<graph-cache>/graph.json", output)
        self.assertNotIn(str(project_cache_dir(self.project)), output)
        with self.assertRaises(GraphifyUnavailable):
            run_readonly_query(self.project, "extract", ["."])

    def test_readonly_query_rejects_option_injection_and_invalid_arity(self) -> None:
        self._write_sources()
        self._profile()
        invalid = (
            ("query", ["--"]),
            ("affected", ["--save-result"]),
            ("explain", ["--reflect"]),
            ("path", ["alpha", "--graph"]),
            ("query", []),
            ("path", ["alpha"]),
            ("god-nodes", ["--graph", "1"]),
            ("god-nodes", ["--top", "0"]),
        )
        for command, values in invalid:
            with self.subTest(command=command, values=values):
                with self.assertRaises(GraphifyUnavailable):
                    execute_readonly_query(self.project, command, values)
        self.assertTrue(execute_readonly_query(self.project, "god-nodes", ["--top", "3"]).ok)

    def test_readonly_query_runs_from_cache_not_target_project(self) -> None:
        self._write_sources()
        profile = self._profile()
        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="ok", stderr="")
        with mock.patch(
            "tmux_core.runtime.graphify._run_bounded_graphify_query",
            return_value=(completed, False),
        ) as runner:
            output = run_readonly_query(self.project, "query", ["alpha"])
        self.assertIn("[TMUX_GRAPHIFY_QUERY]", output)
        self.assertIn("\n[RESULT]\nok\n[END_TMUX_GRAPHIFY_QUERY]", output)
        expected = (
            project_cache_dir(self.project)
            / "graphs"
            / profile.evidence.graph_fingerprint
        )
        self.assertEqual(runner.call_args.kwargs["cwd"], expected)

    def test_graphify_build_config_rejects_parallel_workers(self) -> None:
        with self.assertRaisesRegex(ValueError, "max_workers=1"):
            GraphifyBuildConfig(max_workers=2)

    def test_agent_readonly_cli_rejects_maintenance_commands(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"TMUX_GRAPHIFY_READ_ONLY": "1", "TMUX_GRAPHIFY_PROJECT_DIR": str(self.project)},
            clear=False,
        ):
            with self.assertRaises(SystemExit) as caught:
                cli_main(["build", "--project", str(self.project)])
        self.assertEqual(caught.exception.code, 2)

    def test_build_does_not_change_target_git_status(self) -> None:
        self._write_sources()
        subprocess.run(["git", "init", "-q", str(self.project)], check=True)
        subprocess.run(["git", "-C", str(self.project), "add", "src"], check=True)
        before = subprocess.run(
            ["git", "-C", str(self.project), "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        profile = resolve_graphify_turn_profile(
            project_dir=self.project,
            mode="required",
            prompt="alpha",
            runtime_dir=None,
        )
        after = subprocess.run(
            ["git", "-C", str(self.project), "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        self.assertTrue(profile.enabled)
        self.assertEqual(after, before)

    def test_turn_context_generates_bounded_deterministic_query_suggestions(self) -> None:
        self._write_sources()
        context = GraphifyTurnContext(
            stage_key="A07",
            phase="development_review",
            role="reviewer",
            intent=GraphifyQueryIntent.CHANGE_REVIEW,
            requirement_name="Safe review",
            task_name="M1-T1",
            routed_paths=("src/alpha.py",),
            changed_files=("src/beta.py", "src/alpha.py"),
            symbols=("alpha",),
        )
        profile = resolve_graphify_turn_profile(
            project_dir=self.project,
            mode="required",
            prompt="review the change",
            runtime_dir=self.project / ".development_runtime" / "context",
            turn_context=context,
        )
        self.assertEqual(profile.turn_context, context)
        self.assertGreaterEqual(len(profile.suggestions), 1)
        self.assertLessEqual(len(profile.suggestions), 3)
        self.assertEqual(profile.suggestions[0].command, "affected")
        self.assertEqual(profile.suggestions[0].values, ("src/alpha.py",))
        self.assertIn('"$TMUX_GRAPHIFY_CMD" affected src/alpha.py', profile.evidence.block_text)
        self.assertIn("src/alpha.py:L10", profile.evidence.block_text)
        self.assertIn("[EXTRACTED] edge:", profile.evidence.block_text)
        self.assertLessEqual(len(profile.evidence.block_text), GRAPHIFY_EVIDENCE_MAX_CHARS)
        self.assertTrue(profile.evidence.block_text.endswith("[End Graphify Code Graph Evidence]"))
        full = build_graphify_evidence_block(profile, "business", include_full_guide=True)
        compact = build_graphify_evidence_block(profile, "business", include_full_guide=False)
        self.assertIn(GRAPHIFY_FULL_GUIDE_MARKER, full)
        self.assertNotIn(GRAPHIFY_FULL_GUIDE_MARKER, compact)
        self.assertIn("REQUIRED_QUERY", compact)

    def test_usage_policy_requires_concrete_queries_only_for_matching_conditions(self) -> None:
        self._write_sources()
        routing_profile = resolve_graphify_turn_profile(
            project_dir=self.project,
            mode="required",
            prompt="create routing",
            runtime_dir=self.project / ".routing_init_runtime" / "usage",
            turn_context=GraphifyTurnContext(
                stage_key="A01",
                phase="routing_create",
                role="routing-initializer",
                intent=GraphifyQueryIntent.ROUTING_DISCOVERY,
            ),
        )
        self.assertEqual(routing_profile.usage_policy.query_requirement, "required")
        self.assertEqual(
            routing_profile.usage_policy.recommended_command,
            '"$TMUX_GRAPHIFY_CMD" god-nodes --top 10',
        )
        self.assertTrue(publish_graphify_pending_status(
            self.project,
            "required",
            runner_id="routing-runner",
            stage_key="A01",
            session_generation="routing-session",
        ))
        self.assertTrue(register_graphify_turn_usage(
            self.project,
            "required",
            runner_id="routing-runner",
            stage_key="A01",
            session_generation="routing-session",
            turn_id="routing-turn-1",
            policy=routing_profile.usage_policy,
            delivery_confirmed=True,
        ))
        with mock.patch.dict(
            os.environ,
            {"TMUX_GRAPHIFY_SESSION_GENERATION": "routing-session"},
            clear=False,
        ):
            self.assertTrue(execute_readonly_query(
                self.project,
                "god-nodes",
                ["--top", "10"],
            ).ok)
        later_routing_profile = resolve_graphify_turn_profile(
            project_dir=self.project,
            mode="required",
            prompt="continue routing",
            runtime_dir=self.project / ".routing_init_runtime" / "usage-later",
            refresh=False,
            turn_context=GraphifyTurnContext(
                stage_key="A01",
                phase="routing_create",
                role="routing-initializer",
                intent=GraphifyQueryIntent.ROUTING_DISCOVERY,
            ),
            usage_session_generation="routing-session",
        )
        self.assertEqual(later_routing_profile.usage_policy.query_requirement, "optional")
        clarification_profile = resolve_graphify_turn_profile(
            project_dir=self.project,
            mode="required",
            prompt="clarify a missing symbol",
            runtime_dir=self.project / ".requirement_clarification_runtime" / "usage",
            refresh=False,
            turn_context=GraphifyTurnContext(
                stage_key="A03",
                phase="requirements_clarification",
                role="requirements-analyst",
                intent=GraphifyQueryIntent.CODE_FACT_DISCOVERY,
                symbols=("missing_symbol",),
            ),
        )
        self.assertEqual(clarification_profile.usage_policy.query_requirement, "required")
        self.assertIn("explain missing_symbol", clarification_profile.usage_policy.recommended_command)
        implementation_profile = resolve_graphify_turn_profile(
            project_dir=self.project,
            mode="required",
            prompt="implement alpha",
            runtime_dir=self.project / ".development_runtime" / "usage",
            refresh=False,
            turn_context=GraphifyTurnContext(
                stage_key="A07",
                phase="development",
                role="developer",
                intent=GraphifyQueryIntent.IMPLEMENTATION,
                routed_paths=("src/alpha.py",),
                symbols=("alpha",),
            ),
        )
        self.assertEqual(implementation_profile.usage_policy.query_requirement, "optional")
        for profile in (routing_profile, clarification_profile, implementation_profile):
            self.assertIn("evidence_reading: required", profile.evidence.block_text)

    def test_usage_receipt_requires_exact_session_turn_evidence_and_fingerprint(self) -> None:
        self._write_sources()
        profile = resolve_graphify_turn_profile(
            project_dir=self.project,
            mode="required",
            prompt="review alpha",
            runtime_dir=self.project / ".development_runtime" / "receipt",
            turn_context=GraphifyTurnContext(
                stage_key="A07",
                phase="development_review",
                role="reviewer",
                intent=GraphifyQueryIntent.CHANGE_REVIEW,
                changed_files=("src/alpha.py",),
            ),
        )
        policy = profile.usage_policy
        self.assertTrue(publish_graphify_pending_status(
            self.project,
            "required",
            runner_id="runner-current",
            stage_key="A07",
            session_generation="session-current",
        ))
        self.assertTrue(register_graphify_turn_usage(
            self.project,
            "required",
            runner_id="runner-current",
            stage_key="A07",
            session_generation="session-current",
            turn_id="turn-current",
            policy=policy,
            delivery_confirmed=True,
        ))
        with mock.patch.dict(
            os.environ,
            {"TMUX_GRAPHIFY_SESSION_GENERATION": "session-current"},
            clear=False,
        ):
            result = execute_readonly_query(self.project, "affected", ["src/alpha.py"])
        self.assertTrue(result.ok)
        receipt_path = self.project / ".development_runtime" / "receipt" / "usage.json"
        current = assess_graphify_turn_usage(
            self.project,
            policy,
            session_generation="session-current",
            turn_id="turn-current",
            delivery_confirmed=True,
            receipt_path=receipt_path,
        )
        self.assertEqual(current.query_status, "satisfied")
        self.assertEqual(json.loads(receipt_path.read_text(encoding="utf-8"))["schema"], "tmux-graphify-usage/1")
        self.assertTrue(register_graphify_turn_usage(
            self.project,
            "required",
            runner_id="runner-current",
            stage_key="A07",
            session_generation="session-current",
            turn_id="turn-wrong-command",
            policy=policy,
            delivery_confirmed=True,
        ))
        with mock.patch.dict(
            os.environ,
            {"TMUX_GRAPHIFY_SESSION_GENERATION": "session-current"},
            clear=False,
        ):
            self.assertTrue(execute_readonly_query(self.project, "explain", ["alpha"]).ok)
        wrong_command = assess_graphify_turn_usage(
            self.project,
            policy,
            session_generation="session-current",
            turn_id="turn-wrong-command",
            delivery_confirmed=True,
        )
        self.assertEqual(wrong_command.query_status, "missing")
        old_turn = assess_graphify_turn_usage(
            self.project,
            policy,
            session_generation="session-current",
            turn_id="turn-old",
            delivery_confirmed=True,
        )
        self.assertEqual(old_turn.query_status, "missing")
        old_graph = assess_graphify_turn_usage(
            self.project,
            GraphifyUsagePolicy(
                evidence_required=True,
                query_requirement="required",
                evidence_id=policy.evidence_id,
                graph_fingerprint="0" * 64,
                freshness="fresh",
            ),
            session_generation="session-current",
            turn_id="turn-current",
            delivery_confirmed=True,
        )
        self.assertEqual(old_graph.query_status, "missing")

    def test_cross_module_design_without_extracted_edge_requires_query(self) -> None:
        self._write_sources()
        with mock.patch.dict(os.environ, {"FAKE_GRAPHIFY_EDGE_MODE": "no_edges"}, clear=False):
            profile = resolve_graphify_turn_profile(
                project_dir=self.project,
                mode="required",
                prompt="design alpha and beta",
                runtime_dir=self.project / ".detailed_design_runtime" / "usage",
                turn_context=GraphifyTurnContext(
                    stage_key="A05",
                    phase="detailed_design",
                    role="requirements-analyst",
                    intent=GraphifyQueryIntent.ARCHITECTURE_BOUNDARY,
                    routed_paths=("src/alpha.py", "src/beta.py"),
                ),
            )
        self.assertEqual(profile.usage_policy.query_requirement, "required")
        self.assertIn("缺少 EXTRACTED", profile.usage_policy.requirement_reason)

    def test_deleted_path_uses_previous_generation_as_ambiguous_navigation_only(self) -> None:
        self._write_sources()
        first = self._profile(prompt="alpha calls beta")
        (self.project / "src" / "beta.py").unlink()
        context = GraphifyTurnContext(
            stage_key="A07",
            phase="a07_developer_to_reviewer_checkpoint",
            role="development_reviewer",
            intent=GraphifyQueryIntent.CHANGE_REVIEW,
            requirement_name="Delete beta",
            task_name="M1-T1",
            changed_files=("src/beta.py",),
            deleted_files=("src/beta.py",),
        )
        refreshed = resolve_graphify_turn_profile(
            project_dir=self.project,
            mode="required",
            prompt="review deleted beta callers",
            runtime_dir=self.project / ".development_runtime" / "deleted",
            turn_context=context,
        )

        self.assertNotEqual(first.evidence.graph_fingerprint, refreshed.evidence.graph_fingerprint)
        self.assertEqual(refreshed.deleted_files, ("src/beta.py",))
        self.assertTrue(refreshed.evidence.old_generation_candidates)
        self.assertEqual(
            refreshed.evidence.old_generation_fingerprint,
            first.evidence.graph_fingerprint,
        )
        old_candidate = refreshed.evidence.old_generation_candidates[0]
        self.assertIn("[OLD_GENERATION][AMBIGUOUS]", old_candidate)
        self.assertIn("alpha", old_candidate)
        self.assertIn("beta", old_candidate)
        self.assertIn("OLD_GENERATION_CANDIDATES", refreshed.evidence.block_text)
        self.assertNotIn(str(project_cache_dir(self.project)), refreshed.evidence.block_text)
        self.assertEqual(refreshed.suggestions[0].generation_scope, "previous")
        self.assertEqual(
            refreshed.suggestions[0].shell_command,
            '"$TMUX_GRAPHIFY_CMD" affected src/beta.py --previous',
        )

        previous_result = execute_readonly_query(
            self.project,
            "affected",
            ["src/beta.py"],
            generation_scope="previous",
        )
        self.assertTrue(previous_result.ok)
        self.assertEqual(previous_result.graph_fingerprint, first.evidence.graph_fingerprint)
        self.assertEqual(previous_result.freshness, "stale")
        self.assertEqual(previous_result.generation_scope, "previous")
        self.assertIn("OLD_GENERATION", previous_result.warnings[0])
        self.assertEqual(previous_result.to_public_dict()["graph"]["generation_scope"], "previous")
        self.assertEqual(read_graphify_project_status(self.project)["state"], "ready")
        upstream_call = self.log_path.read_text(encoding="utf-8").splitlines()[-1]
        self.assertTrue(upstream_call.startswith("affected src/beta.py "))
        self.assertNotIn("--previous", upstream_call)

        with self.assertRaisesRegex(GraphifyUnavailable, "仍存在"):
            execute_readonly_query(
                self.project,
                "affected",
                ["src/alpha.py"],
                generation_scope="previous",
            )
        with self.assertRaisesRegex(GraphifyUnavailable, "相对路径"):
            execute_readonly_query(
                self.project,
                "affected",
                [str(self.project / "src" / "beta.py")],
                generation_scope="previous",
            )
        with self.assertRaisesRegex(GraphifyUnavailable, "manifest 删除校验"):
            execute_readonly_query(
                self.project,
                "affected",
                ["src/never-existed.py"],
                generation_scope="previous",
            )
        with self.assertRaisesRegex(GraphifyUnavailable, "仅支持 affected"):
            execute_readonly_query(
                self.project,
                "query",
                ["beta"],
                generation_scope="previous",
            )
        with mock.patch("builtins.print"):
            self.assertEqual(
                cli_main([
                    "affected",
                    "src/beta.py",
                    "--previous",
                    "--project",
                    str(self.project),
                    "--format",
                    "json",
                ]),
                0,
            )

    def test_evidence_truncation_preserves_authority_limit_verification_and_end(self) -> None:
        source_dir = self.project / "src"
        source_dir.mkdir()
        for index in range(24):
            name = f"symbol_{index:02d}_" + ("x" * 180) + ".py"
            (source_dir / name).write_text(f"VALUE_{index} = {index}\n", encoding="utf-8")
        profile = self._profile(prompt="unmatched broad request")
        block = profile.evidence.block_text
        self.assertLessEqual(len(block), GRAPHIFY_EVIDENCE_MAX_CHARS)
        self.assertIn("authority: navigation evidence only", block)
        self.assertIn("limitation: static extraction cannot prove", block)
        self.assertIn("VERIFY_BEFORE_ACTING:", block)
        self.assertIn("verify them in AGENTS.md", block)
        self.assertTrue(block.endswith("[End Graphify Code Graph Evidence]"))
        self.assertIn("[truncated within evidence budget]", block)

    def test_query_suggestion_shell_command_quotes_shell_expansion(self) -> None:
        suggestion = GraphifyQuerySuggestion(
            command="query",
            values=('$(touch /tmp/should-not-run) `id` "quoted"',),
            purpose="test",
        )
        rendered = suggestion.shell_command
        self.assertTrue(rendered.startswith('"$TMUX_GRAPHIFY_CMD" query '))
        self.assertIn("'$(touch /tmp/should-not-run) `id` \"quoted\"'", rendered)

    def test_freshness_guard_allows_old_graph_with_explicit_warning(self) -> None:
        self._write_sources()
        profile = self._profile()
        fresh = assess_graphify_freshness(self.project)
        self.assertEqual(fresh.state, "fresh")
        (self.project / "src" / "beta.py").write_text("def beta(): return 99\n", encoding="utf-8")
        stale = execute_readonly_query(self.project, "affected", ["beta"])
        self.assertTrue(stale.ok)
        self.assertEqual(stale.freshness, "stale")
        self.assertIn("src/beta.py", stale.warnings[0])
        current = json.loads(
            (project_cache_dir(self.project) / "current.json").read_text(encoding="utf-8")
        )
        self.assertEqual(current["fingerprint"], profile.evidence.graph_fingerprint)
        self.assertEqual(current["freshness"], "stale")
        status = read_graphify_project_status(self.project)
        self.assertEqual(status["state"], "stale")
        self.assertEqual(status["query_count_stage"], 1)
        (self.project / "src" / "beta.py").write_text("def beta(): return 1\n", encoding="utf-8")
        restored = execute_readonly_query(self.project, "affected", ["beta"])
        self.assertEqual(restored.freshness, "fresh")
        self.assertEqual(read_graphify_project_status(self.project)["query_count_stage"], 2)
        self._profile(prompt="checkpoint")
        self.assertEqual(read_graphify_project_status(self.project)["query_count_stage"], 2)

    def test_clean_git_change_manifest_uses_blob_ids_without_reading_source(self) -> None:
        self._write_sources()
        subprocess.run(["git", "init", "-q", str(self.project)], check=True)
        subprocess.run(["git", "-C", str(self.project), "add", "src"], check=True)
        with mock.patch(
            "tmux_core.runtime.graphify._read_snapshot_source",
            side_effect=AssertionError("clean Git source must not be read"),
        ):
            manifest = capture_graphify_source_manifest(self.project)
        self.assertEqual(set(manifest), {"src/alpha.py", "src/beta.py"})
        self.assertTrue(all(value.startswith("git:") for value in manifest.values()))

    def test_validation_stamp_makes_warm_query_skip_full_graph_validation(self) -> None:
        self._write_sources()
        profile = self._profile()
        validation = (
            project_cache_dir(self.project)
            / "graphs"
            / profile.evidence.graph_fingerprint
            / "validation.json"
        )
        self.assertTrue(validation.is_file())
        with mock.patch(
            "tmux_core.runtime.graphify._load_and_validate_graph",
            side_effect=AssertionError("warm query must use validation stamp"),
        ), mock.patch(
            "tmux_core.runtime.graphify._hash_file",
            side_effect=AssertionError("warm query must not hash cached metadata"),
        ):
            result = execute_readonly_query(self.project, "query", ["alpha"])
        self.assertTrue(result.ok)

    def test_query_output_is_bounded_sanitized_and_audited_without_payload(self) -> None:
        self._write_sources()
        self._profile()
        publish_graphify_pending_status(
            self.project,
            "auto",
            runner_id="runner-current",
            stage_key="A05",
            session_generation="session-current",
        )
        raw = (
            "\x1b[31m/private/secret.py C:\\secret\\file.py "
            "file:///private/uri-secret.py "
            "vscode://file/Users/name/vscode-secret.py "
            "\\\\server\\share\\unc-secret.py "
            "//server/share/forward-secret.py "
            "https://example.com/a\n"
            + ("x" * 7000)
        )
        with mock.patch.dict(
            os.environ,
            {
                "FAKE_GRAPHIFY_QUERY_OUTPUT": raw,
                "TMUX_GRAPHIFY_RUNNER_ID": "stale-child-runner",
                "TMUX_GRAPHIFY_SESSION_GENERATION": "session-current",
            },
            clear=False,
        ):
            result = execute_readonly_query(self.project, "query", ["sensitive question"])
        self.assertTrue(result.ok)
        self.assertTrue(result.truncated)
        self.assertLessEqual(len(result.result_text), GRAPHIFY_QUERY_MAX_CHARS)
        self.assertNotIn("/private/secret.py", result.result_text)
        self.assertNotIn("C:\\secret", result.result_text)
        self.assertNotIn("uri-secret.py", result.result_text)
        self.assertNotIn("vscode-secret.py", result.result_text)
        self.assertNotIn("unc-secret.py", result.result_text)
        self.assertNotIn("forward-secret.py", result.result_text)
        self.assertNotIn("\x1b", result.result_text)
        self.assertIn("https://example.com/a", result.result_text)
        control_output = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="ok\x00control", stderr="",
        )
        with mock.patch(
            "tmux_core.runtime.graphify._run_bounded_graphify_query",
            return_value=(control_output, False),
        ):
            control_result = execute_readonly_query(self.project, "query", ["control"])
        self.assertNotIn("\x00", control_result.result_text)
        audit_path = project_cache_dir(self.project) / "query-audit.jsonl"
        audit_text = audit_path.read_text(encoding="utf-8")
        self.assertNotIn("sensitive question", audit_text)
        self.assertNotIn("secret.py", audit_text)
        self.assertNotIn("result_text", audit_text)
        audit_payload = json.loads(audit_text.splitlines()[0])
        self.assertEqual(audit_payload["runner_id"], "runner-current")
        self.assertEqual(audit_payload["stage_key"], "A05")
        self.assertTrue(audit_payload["scope_match"])
        with mock.patch.dict(
            os.environ,
            {
                "FAKE_GRAPHIFY_QUERY_OUTPUT": "file:///private/json-secret.py",
                "TMUX_GRAPHIFY_SESSION_GENERATION": "session-current",
            },
            clear=False,
        ):
            json_payload = json.loads(
                run_readonly_query(
                    self.project,
                    "query",
                    ["json output"],
                    output_format="json",
                )
            )
        self.assertNotIn("json-secret.py", json_payload["result_text"])

    def test_failed_stderr_truncation_and_json_envelope(self) -> None:
        self._write_sources()
        self._profile()
        with mock.patch.dict(
            os.environ,
            {
                "FAKE_GRAPHIFY_QUERY_OUTPUT": "failure " + ("z" * 7000),
                "FAKE_GRAPHIFY_QUERY_STDERR": "1",
            },
            clear=False,
        ):
            result = execute_readonly_query(self.project, "explain", ["alpha"])
        self.assertFalse(result.ok)
        self.assertTrue(result.truncated)
        payload = json.loads(run_readonly_query(self.project, "query", ["alpha"], output_format="json"))
        self.assertEqual(payload["schema"], "tmux-graphify-query-result/1")
        self.assertIn("graph", payload)

    def test_readonly_cli_locks_project_and_returns_query_exit_status(self) -> None:
        self._write_sources()
        self._profile()
        outside = self.root / "outside-project"
        outside.mkdir()
        with mock.patch.dict(
            os.environ,
            {
                "TMUX_GRAPHIFY_READ_ONLY": "1",
                "TMUX_GRAPHIFY_PROJECT_DIR": str(self.project),
                "TMUX_GRAPHIFY_MODE": "required",
            },
            clear=False,
        ):
            self.assertEqual(
                cli_main(["query", "alpha", "--project", str(outside), "--format", "json"]),
                1,
            )
            self.assertEqual(
                cli_main(["query", "alpha", "--project", str(self.project), "--format", "json"]),
                0,
            )

    @unittest.skipUnless(REAL_GRAPHIFY_EXECUTABLE, "set REAL_GRAPHIFY_EXECUTABLE for the managed canary")
    def test_real_graphify_code_only_incremental_and_readonly_queries(self) -> None:
        self._write_sources()
        (self.project / "src" / "caller.ts").write_text(
            "export function caller() { return alpha(); }\n", encoding="utf-8"
        )
        (self.project / ".gitignore").write_text(".development_runtime/\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q", str(self.project)], check=True)
        subprocess.run(["git", "-C", str(self.project), "add", ".gitignore", "src"], check=True)
        status_before_build = subprocess.run(
            ["git", "-C", str(self.project), "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        with mock.patch.dict(
            os.environ,
            {"TMUX_GRAPHIFY_EXECUTABLE": REAL_GRAPHIFY_EXECUTABLE},
            clear=False,
        ):
            first = resolve_graphify_turn_profile(
                project_dir=self.project,
                mode="required",
                prompt="alpha callers beta",
                runtime_dir=self.project / ".development_runtime" / "real-canary",
            )
            self.assertTrue(first.enabled)
            self.assertGreater(first.status.node_count, 0)
            self.assertTrue(run_readonly_query(self.project, "query", ["alpha"]))
            self.assertTrue(run_readonly_query(self.project, "affected", ["alpha"]))
            self.assertTrue(run_readonly_query(self.project, "path", ["alpha", "beta"]))
            status_after_build = subprocess.run(
                ["git", "-C", str(self.project), "status", "--porcelain"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout
            self.assertEqual(status_after_build, status_before_build)
            (self.project / "src" / "alpha.py").write_text("def alpha(): return 42\n", encoding="utf-8")
            (self.project / "src" / "added.py").write_text("def added(): return 2\n", encoding="utf-8")
            (self.project / "src" / "beta.py").unlink()
            stale = execute_readonly_query(self.project, "affected", ["alpha"])
            self.assertTrue(stale.ok)
            self.assertEqual(stale.freshness, "stale")
            status_before_refresh = subprocess.run(
                ["git", "-C", str(self.project), "status", "--porcelain"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout
            refreshed = resolve_graphify_turn_profile(
                project_dir=self.project,
                mode="required",
                prompt="alpha added",
                runtime_dir=self.project / ".development_runtime" / "real-canary",
                turn_context=GraphifyTurnContext(
                    stage_key="A07",
                    phase="a07_developer_to_reviewer_checkpoint",
                    role="development_reviewer",
                    intent=GraphifyQueryIntent.CHANGE_REVIEW,
                    changed_files=("src/alpha.py", "src/added.py", "src/beta.py"),
                    deleted_files=("src/beta.py",),
                ),
            )
            previous_deleted = execute_readonly_query(
                self.project,
                "affected",
                ["src/beta.py"],
                generation_scope="previous",
            )
            status_after_refresh = subprocess.run(
                ["git", "-C", str(self.project), "status", "--porcelain"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout
        self.assertTrue(refreshed.enabled)
        self.assertNotEqual(first.evidence.graph_fingerprint, refreshed.evidence.graph_fingerprint)
        self.assertEqual(refreshed.status.freshness, "fresh")
        self.assertTrue(refreshed.evidence.old_generation_candidates)
        self.assertEqual(previous_deleted.graph_fingerprint, first.evidence.graph_fingerprint)
        self.assertEqual(previous_deleted.generation_scope, "previous")
        self.assertEqual(previous_deleted.freshness, "stale")
        self.assertIn("OLD_GENERATION", previous_deleted.warnings[0])
        self.assertEqual(status_after_refresh, status_before_refresh)


if __name__ == "__main__":
    unittest.main()
