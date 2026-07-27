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
    GRAPHIFY_VERSION,
    GraphifyBuildConfig,
    GraphifyMode,
    GraphifySchemaError,
    GraphifySnapshotError,
    GraphifyUnavailable,
    cli_main,
    create_graphify_snapshot,
    normalize_graphify_mode,
    project_cache_dir,
    read_graphify_project_status,
    resolve_graphify_tool,
    resolve_graphify_turn_profile,
    run_readonly_query,
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
        nodes.append({"id": "n" + str(index), "label": path.stem, "file_path": str(path)})
    edges = []
    if len(nodes) > 1:
        edges.append({"source": nodes[0]["id"], "target": nodes[1]["id"], "relation": "calls"})
    edge_mode = os.environ.get("FAKE_GRAPHIFY_EDGE_MODE", "")
    if edge_mode == "non_object":
        edges = ["broken-edge"]
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
    print(command + " graph=" + graph_path)
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
        (docs / "repo_map.json").write_text(
            json.dumps(
                {
                    "modules": [
                        {
                            "id": "M_EXACT_ROUTED",
                            "owned_paths": [
                                {"type": "literal", "match": "exact", "path": "src/a_routed.py"}
                            ],
                        },
                        {
                            "id": "M_SOURCE_TREE",
                            "owned_paths": [
                                {"type": "subtree", "match": "subtree", "path": "src"}
                            ],
                        },
                    ]
                }
            ),
            encoding="utf-8",
        )

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
        self.assertIn(
            "M_EXACT_ROUTED <- src/a_routed.py [needs_code_confirmation]",
            seeded.evidence.routed_module_candidates,
        )
        self.assertTrue(
            any(candidate.startswith("M_SOURCE_TREE <- ") for candidate in seeded.evidence.routed_module_candidates)
        )
        self.assertIn("ROUTED_MODULE_CANDIDATES (needs_code_confirmation", seeded.evidence.block_text)
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

    def test_readonly_query_runs_from_cache_not_target_project(self) -> None:
        self._write_sources()
        profile = self._profile()
        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="ok", stderr="")
        with mock.patch("tmux_core.runtime.graphify._run_graphify_process", return_value=completed) as runner:
            self.assertEqual(run_readonly_query(self.project, "query", ["alpha"]), "ok")
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
            )
            status_after_refresh = subprocess.run(
                ["git", "-C", str(self.project), "status", "--porcelain"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout
        self.assertTrue(refreshed.enabled)
        self.assertNotEqual(first.evidence.graph_fingerprint, refreshed.evidence.graph_fingerprint)
        self.assertEqual(status_after_refresh, status_before_refresh)


if __name__ == "__main__":
    unittest.main()
