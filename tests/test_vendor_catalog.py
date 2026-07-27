from __future__ import annotations

import json
import os
import threading
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import tmux_core.runtime.vendor_catalog as vendor_catalog_module
from tmux_core.runtime.vendor_catalog import (
    CatalogSnapshot,
    CONFIDENCE_HIGH,
    CONFIDENCE_MEDIUM,
    DEGRADED_SCAN_STATUS,
    ModelInventory,
    OK_SCAN_STATUS,
    REASONING_MAPPED,
    REASONING_MODEL_FAMILY_ROUTING,
    REASONING_NATIVE,
    ReasoningInventory,
    SCHEMA_VERSION,
    SOURCE_CACHE_FALLBACK,
    SOURCE_CONFIG_FILE,
    SOURCE_DYNAMIC_CLI,
    SOURCE_PACKAGE_METADATA,
    VendorInventory,
    VENDOR_ORDER,
    ensure_vendor_catalog_current,
    ensure_vendor_catalogs_current,
    get_catalog_snapshot,
    get_default_model_for_vendor,
    get_model_choices,
    get_vendor_inventory,
    normalize_vendor_id,
    parse_agy_models_output,
    parse_codex_models_output,
    parse_opencode_debug_config_output,
    parse_opencode_verbose_output,
    resolve_launch,
    refresh_catalog_snapshot,
    refresh_vendor_catalog,
    reset_catalog_cache_for_tests,
    _build_agy_models,
    _build_opencode_like_config_models,
    _build_opencode_like_models,
    _build_gemini_models,
    _scan_agy_vendor,
    _scan_deveco_vendor,
    _scan_mimo_vendor,
    _scan_opencode_vendor,
    _resolved_vendor_binary_path,
)


class VendorCatalogTests(unittest.TestCase):
    def setUp(self):
        reset_catalog_cache_for_tests()

    def tearDown(self):
        reset_catalog_cache_for_tests()

    @staticmethod
    def _snapshot_at(generated_at: str) -> CatalogSnapshot:
        return CatalogSnapshot(
            schema_version=SCHEMA_VERSION,
            generated_at=generated_at,
            cache_path="/tmp/vendor_catalog.json",
            vendors=tuple(
                VendorInventory(
                    vendor_id=vendor_id,
                    installed=False,
                    scan_status="unavailable",
                    source_kind="unavailable",
                    confidence="low",
                    binary_path="",
                )
                for vendor_id in VENDOR_ORDER
            ),
        )

    def test_deveco_is_appended_without_changing_existing_vendor_order(self):
        self.assertEqual(VENDOR_ORDER, ("codex", "claude", "gemini", "opencode", "mimo", "agy", "deveco"))
        self.assertEqual(normalize_vendor_id("DevEco"), "deveco")

    def test_deveco_binary_resolution_prefers_lowercase_and_falls_back_to_uppercase(self):
        with patch("tmux_core.runtime.vendor_catalog.shutil.which", return_value="/opt/bin/deveco") as which:
            self.assertEqual(_resolved_vendor_binary_path("deveco"), "/opt/bin/deveco")
        which.assert_called_once_with("deveco")

        def uppercase_only(binary_name):  # noqa: ANN001
            return "/opt/bin/DevEco" if binary_name == "DevEco" else None

        with patch("tmux_core.runtime.vendor_catalog.shutil.which", side_effect=uppercase_only) as which:
            self.assertEqual(_resolved_vendor_binary_path("deveco"), "/opt/bin/DevEco")
        self.assertEqual([call.args[0] for call in which.call_args_list], ["deveco", "DevEco"])

    def test_parse_codex_models_output_extracts_visible_models(self):
        payload = """
[
  {
    "slug": "gpt-5.4",
    "display_name": "gpt-5.4",
    "default_reasoning_level": "medium",
    "supported_reasoning_levels": [
      {"effort": "low"},
      {"effort": "medium"},
      {"effort": "high"},
      {"effort": "xhigh"}
    ],
    "priority": 9,
    "visibility": "list"
  },
  {
    "slug": "hidden-model",
    "display_name": "hidden-model",
    "supported_reasoning_levels": [],
    "priority": 99,
    "visibility": "hidden"
  }
]
"""
        items = parse_codex_models_output(payload)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["slug"], "gpt-5.4")

    def test_gemini_models_default_to_cli_family_aliases(self):
        models = _build_gemini_models(("gemini-2.0-flash", "gemini-cli", "gemini-3-pro-preview"))

        self.assertEqual([item.model_id for item in models], ["auto", "flash", "pro"])
        self.assertTrue(all(item.synthetic for item in models))

    def test_gemini_explicit_package_models_are_opt_in(self):
        with patch.dict(os.environ, {"TMUX_GEMINI_EXPERIMENTAL_MODELS": "1"}):
            models = _build_gemini_models(("gemini-2.0-flash", "gemini-3-pro-preview"))

        self.assertEqual(
            [item.model_id for item in models],
            ["auto", "flash", "pro", "gemini-2.0-flash", "gemini-3-pro-preview"],
        )

    def test_parse_opencode_verbose_output_extracts_full_model_ids(self):
        payload = """
opencode/gpt-5-nano
{
  "id": "gpt-5-nano",
  "providerID": "opencode",
  "name": "GPT-5 Nano",
  "capabilities": {
    "reasoning": true
  },
  "variants": {
    "low": {"reasoningEffort": "low"},
    "high": {"reasoningEffort": "high"}
  }
}
kimi-code/kimi-for-coding
{
  "id": "kimi-for-coding",
  "providerID": "kimi-code",
  "name": "Kimi For Coding",
  "capabilities": {
    "reasoning": false
  },
  "variants": {}
}
"""
        items = parse_opencode_verbose_output(payload)
        self.assertEqual([item["full_model_id"] for item in items], ["opencode/gpt-5-nano", "kimi-code/kimi-for-coding"])

    def test_parse_opencode_debug_config_output_extracts_json_payload(self):
        payload = """
{
  "model": "kimi-code/kimi-for-coding",
  "provider": {
    "kimi-code": {
      "models": {
        "kimi-for-coding": {
          "name": "Kimi For Coding"
        }
      }
    }
  }
}
"""
        parsed = parse_opencode_debug_config_output(payload)
        self.assertEqual(parsed["model"], "kimi-code/kimi-for-coding")
        self.assertIn("kimi-code", parsed["provider"])

    def test_opencode_dynamic_models_exclude_config_only_entries(self):
        def fake_probe(argv, *, timeout_sec=12.0):  # noqa: ANN001, ARG001
            if argv == ["/usr/bin/opencode", "models", "--verbose"]:
                return SimpleNamespace(
                    ok=True,
                    stdout=(
                        "live/provider-model\n"
                        '{"id":"provider-model","providerID":"live","name":"Live Model",'
                        '"capabilities":{"reasoning":true},"variants":{}}'
                    ),
                )
            if argv == ["/usr/bin/opencode", "debug", "config"]:
                return SimpleNamespace(
                    ok=True,
                    stdout=json.dumps(
                        {
                            "model": "stale/removed-model",
                            "provider": {
                                "live": {"models": {"provider-model": {"name": "Live Model"}}},
                                "stale": {"models": {"removed-model": {"name": "Removed Model"}}},
                            },
                        }
                    ),
                )
            return SimpleNamespace(ok=False, stdout="")

        with patch("tmux_core.runtime.vendor_catalog._command_probe", side_effect=fake_probe):
            inventory = _scan_opencode_vendor("/usr/bin/opencode")

        self.assertEqual(inventory.model_ids(), ("live/provider-model",))
        self.assertEqual(inventory.default_model, "live/provider-model")
        self.assertIn("configured_default_unavailable", inventory.notes)
        self.assertTrue(all(model.source_kind == SOURCE_DYNAMIC_CLI for model in inventory.models))

    def test_opencode_does_not_synthesize_supported_models_from_config_when_dynamic_probe_fails(self):
        def fake_probe(argv, *, timeout_sec=12.0):  # noqa: ANN001, ARG001
            if argv == ["/usr/bin/opencode", "debug", "config"]:
                return SimpleNamespace(
                    ok=True,
                    stdout='{"model":"stale/removed-model","provider":{"stale":{"models":{"removed-model":{}}}}}',
                )
            return SimpleNamespace(ok=False, stdout="")

        with patch("tmux_core.runtime.vendor_catalog._command_probe", side_effect=fake_probe):
            inventory = _scan_opencode_vendor("/usr/bin/opencode")

        self.assertEqual(inventory.scan_status, DEGRADED_SCAN_STATUS)
        self.assertEqual(inventory.models, ())
        self.assertEqual(inventory.default_model, "")
        catalog = CatalogSnapshot(
            SCHEMA_VERSION,
            "2026-07-18T00:00:00+00:00",
            "/tmp/catalog.json",
            (inventory,),
        )
        self.assertEqual(get_default_model_for_vendor("opencode", catalog=catalog), "")

    def test_mimo_vendor_normalization_and_fallback_default(self):
        self.assertEqual("mimo", normalize_vendor_id("mimo"))
        self.assertIn("mimo", VENDOR_ORDER)
        catalog = CatalogSnapshot(
            schema_version="1.0",
            generated_at="2026-06-12T00:00:00+00:00",
            cache_path="/tmp/catalog.json",
            vendors=(
                VendorInventory(
                    vendor_id="mimo",
                    installed=True,
                    scan_status="degraded",
                    source_kind="legacy_fallback",
                    confidence="low",
                    binary_path="/usr/bin/mimo",
                    default_model="mimo/mimo-v2.5-pro",
                    models=(
                        ModelInventory(
                            vendor_id="mimo",
                            model_id="mimo/mimo-v2.5-pro",
                            display_name="mimo/mimo-v2.5-pro",
                            source_kind="legacy_fallback",
                            confidence="low",
                            reasoning=ReasoningInventory(
                                vendor_id="mimo",
                                model_id="mimo/mimo-v2.5-pro",
                                source_kind="legacy_fallback",
                                confidence="low",
                                reasoning_control_mode="implicit_default",
                                supports_reasoning=True,
                                normalized_reasoning_levels=("low", "medium", "high", "xhigh", "max"),
                            ),
                        ),
                    ),
                ),
            ),
        )

        self.assertEqual(get_default_model_for_vendor("mimo", catalog=catalog), "mimo/mimo-v2.5-pro")
        resolution = resolve_launch("mimo", "default", "max", catalog=catalog)
        self.assertEqual(resolution.resolved_model, "mimo/mimo-v2.5-pro")

    def test_agy_vendor_models_keep_cli_display_names_and_effort_suffixes(self):
        self.assertEqual("agy", normalize_vendor_id("agy"))
        self.assertIn("agy", VENDOR_ORDER)
        model_ids = parse_agy_models_output(
            """
Gemini 3.5 Flash (Medium)
Gemini 3.5 Flash (High)
Claude Sonnet 4.6 (Thinking)
"""
        )
        models = _build_agy_models(model_ids)

        self.assertEqual(
            [item.model_id for item in models],
            ["Gemini 3.5 Flash (Medium)", "Gemini 3.5 Flash (High)", "Claude Sonnet 4.6 (Thinking)"],
        )
        self.assertEqual(models[0].reasoning.normalized_reasoning_levels, ("medium",))
        self.assertEqual(models[1].reasoning.normalized_reasoning_levels, ("high",))
        self.assertEqual(models[2].reasoning.normalized_reasoning_levels, ("high",))

    def test_scan_agy_vendor_uses_agy_models_command_and_default(self):
        def fake_probe(argv, *, timeout_sec=12.0):  # noqa: ANN001
            if argv == ["agy", "models"]:
                return SimpleNamespace(
                    ok=True,
                    stdout="\n".join(
                        [
                            "Gemini 3.5 Flash (Medium)",
                            "Gemini 3.5 Flash (High)",
                            "Gemini 3.5 Flash (Low)",
                        ]
                    ),
                )
            return SimpleNamespace(ok=False, stdout="")

        with patch("tmux_core.runtime.vendor_catalog._command_probe", side_effect=fake_probe) as probe:
            inventory = _scan_agy_vendor("/usr/bin/agy")

        self.assertEqual([call.args[0] for call in probe.call_args_list], [["agy", "models"]])
        self.assertEqual(inventory.vendor_id, "agy")
        self.assertEqual(inventory.default_model, "Gemini 3.5 Flash (High)")

    def test_opencode_like_builders_write_mimo_vendor_id(self):
        items = parse_opencode_verbose_output(
            """
mimo/mimo-v2.5-pro
{
  "id": "mimo-v2.5-pro",
  "providerID": "mimo",
  "name": "MiMo V2.5 Pro",
  "capabilities": {"reasoning": true},
  "variants": {"high": {"reasoningEffort": "high"}}
}
"""
        )
        models = _build_opencode_like_models("mimo", items)

        self.assertEqual(models[0].vendor_id, "mimo")
        self.assertEqual(models[0].reasoning.vendor_id, "mimo")

        config_models = _build_opencode_like_config_models(
            "mimo",
            {
                "provider": {
                    "mimo": {
                        "models": {
                            "mimo-v2.5-pro": {
                                "name": "MiMo V2.5 Pro",
                            }
                        }
                    }
                }
            },
        )
        self.assertEqual(config_models[0].vendor_id, "mimo")
        self.assertEqual(config_models[0].reasoning.vendor_id, "mimo")

    def test_scan_mimo_vendor_uses_mimo_cli_commands(self):
        def fake_probe(argv, *, timeout_sec=12.0):  # noqa: ANN001
            if argv == ["/usr/bin/mimo", "models", "--verbose"]:
                return SimpleNamespace(
                    ok=True,
                    stdout="""
mimo/mimo-v2.5-pro
{"id":"mimo-v2.5-pro","providerID":"mimo","name":"MiMo V2.5 Pro","capabilities":{"reasoning":true},"variants":{"high":{"reasoningEffort":"high"}}}
""",
                )
            if argv == ["/usr/bin/mimo", "debug", "config"]:
                return SimpleNamespace(ok=True, stdout='{"model":"mimo/mimo-v2.5-pro","provider":{}}')
            return SimpleNamespace(ok=False, stdout="")

        with patch("tmux_core.runtime.vendor_catalog._command_probe", side_effect=fake_probe) as probe:
            inventory = _scan_mimo_vendor("/usr/bin/mimo")

        self.assertCountEqual(
            [call.args[0] for call in probe.call_args_list],
            [["/usr/bin/mimo", "models", "--verbose"], ["/usr/bin/mimo", "debug", "config"]],
        )
        self.assertEqual(inventory.vendor_id, "mimo")
        self.assertEqual(inventory.default_model, "mimo/mimo-v2.5-pro")

    def test_opencode_like_models_and_config_probes_run_concurrently(self):
        both_started = threading.Event()
        started_lock = threading.Lock()
        started_commands: list[tuple[str, ...]] = []

        def concurrent_probe(argv, *, timeout_sec=12.0):  # noqa: ANN001, ARG001
            command = tuple(argv)
            with started_lock:
                started_commands.append(command)
                if len(started_commands) >= 2:
                    both_started.set()
            if not both_started.wait(timeout=5.0):
                raise RuntimeError("OpenCode-like probes were executed serially")
            if command[-2:] == ("models", "--verbose"):
                return SimpleNamespace(
                    ok=True,
                    stdout=(
                        "mimo/mimo-v2.5-pro\n"
                        '{"id":"mimo-v2.5-pro","providerID":"mimo","name":"MiMo V2.5 Pro",'
                        '"capabilities":{"reasoning":true},"variants":{}}'
                    ),
                )
            return SimpleNamespace(ok=True, stdout='{"model":"mimo/mimo-v2.5-pro","provider":{}}')

        with patch("tmux_core.runtime.vendor_catalog._command_probe", side_effect=concurrent_probe):
            inventory = _scan_mimo_vendor("/usr/bin/mimo")

        self.assertEqual(inventory.scan_status, OK_SCAN_STATUS)
        self.assertEqual(inventory.default_model, "mimo/mimo-v2.5-pro")
        self.assertCountEqual(
            started_commands,
            [
                ("/usr/bin/mimo", "models", "--verbose"),
                ("/usr/bin/mimo", "debug", "config"),
            ],
        )

    def test_scan_deveco_uses_resolved_binary_pure_commands_and_dynamic_model(self):
        binary_path = "/opt/bin/DevEco"

        def fake_probe(argv, *, timeout_sec=12.0, env_overrides=None):  # noqa: ANN001
            self.assertEqual(env_overrides, {"DEVECO_DISABLE_AUTOUPDATE": "1"})
            if argv == [binary_path, "--pure", "models", "--verbose"]:
                return SimpleNamespace(
                    ok=True,
                    stdout='deveco/GLM-current\n{"id":"GLM-current","providerID":"deveco","name":"GLM Current","capabilities":{"reasoning":true},"variants":{}}',
                )
            if argv == [binary_path, "--pure", "debug", "config"]:
                return SimpleNamespace(ok=True, stdout='{"model":"deveco/GLM-current","provider":{}}')
            return SimpleNamespace(ok=False, stdout="")

        with patch("tmux_core.runtime.vendor_catalog._command_probe", side_effect=fake_probe) as probe:
            inventory = _scan_deveco_vendor(binary_path)

        self.assertCountEqual(
            [call.args[0] for call in probe.call_args_list],
            [
                [binary_path, "--pure", "models", "--verbose"],
                [binary_path, "--pure", "debug", "config"],
            ],
        )
        self.assertEqual(inventory.vendor_id, "deveco")
        self.assertEqual(inventory.default_model, "deveco/GLM-current")
        self.assertEqual(inventory.model_ids(), ("deveco/GLM-current",))
        catalog = CatalogSnapshot(SCHEMA_VERSION, "2026-07-11T00:00:00+00:00", "/tmp/catalog.json", (inventory,))
        resolution = resolve_launch("deveco", "default", "high", catalog=catalog)
        self.assertEqual(resolution.executable_path, binary_path)

    def test_deveco_scan_failure_has_no_synthetic_or_hardcoded_model(self):
        with patch(
            "tmux_core.runtime.vendor_catalog._command_probe",
            return_value=SimpleNamespace(ok=False, stdout=""),
        ):
            inventory = _scan_deveco_vendor("/usr/bin/deveco")

        self.assertEqual(inventory.scan_status, DEGRADED_SCAN_STATUS)
        self.assertEqual(inventory.models, ())
        self.assertEqual(inventory.default_model, "")
        catalog = CatalogSnapshot(SCHEMA_VERSION, "2026-07-11T00:00:00+00:00", "/tmp/catalog.json", (inventory,))
        self.assertEqual(get_default_model_for_vendor("deveco", catalog=catalog), "")
        with self.assertRaises(ValueError):
            resolve_launch("deveco", "default", "high", catalog=catalog)

    def test_refresh_reuses_only_real_cached_deveco_models_when_scan_degrades(self):
        cached_model = ModelInventory(
            vendor_id="deveco",
            model_id="deveco/cached-model",
            display_name="Cached Model",
            source_kind=SOURCE_DYNAMIC_CLI,
            confidence=CONFIDENCE_HIGH,
            reasoning=ReasoningInventory(
                vendor_id="deveco",
                model_id="deveco/cached-model",
                source_kind=SOURCE_DYNAMIC_CLI,
                confidence=CONFIDENCE_HIGH,
                reasoning_control_mode="implicit_default",
                supports_reasoning=True,
                normalized_reasoning_levels=("high",),
            ),
        )
        prior_inventory = VendorInventory(
            vendor_id="deveco",
            installed=True,
            scan_status=OK_SCAN_STATUS,
            source_kind=SOURCE_DYNAMIC_CLI,
            confidence=CONFIDENCE_HIGH,
            binary_path="/old/bin/deveco",
            models=(cached_model,),
            default_model=cached_model.model_id,
        )
        prior = CatalogSnapshot(SCHEMA_VERSION, "2026-07-10T00:00:00+00:00", "/tmp/old.json", (prior_inventory,))
        degraded = VendorInventory(
            vendor_id="deveco",
            installed=True,
            scan_status=DEGRADED_SCAN_STATUS,
            source_kind="legacy_fallback",
            confidence="low",
            binary_path="/new/bin/deveco",
            models=(),
            default_model="",
        )

        def binary_for(vendor_id):  # noqa: ANN001
            return "/new/bin/deveco" if vendor_id == "deveco" else ""

        with patch("tmux_core.runtime.vendor_catalog._resolved_vendor_binary_path", side_effect=binary_for), patch(
            "tmux_core.runtime.vendor_catalog._SCANNERS",
            {"deveco": lambda _path: degraded},
        ), patch("tmux_core.runtime.vendor_catalog._save_cached_snapshot"):
            refreshed = refresh_catalog_snapshot(prior_snapshot=prior)

        self.assertEqual(tuple(item.vendor_id for item in refreshed.vendors), VENDOR_ORDER)
        deveco = refreshed.vendor("deveco")
        self.assertEqual(deveco.source_kind, SOURCE_CACHE_FALLBACK)
        self.assertEqual(deveco.binary_path, "/new/bin/deveco")
        self.assertEqual(deveco.model_ids(), ("deveco/cached-model",))
        self.assertEqual(deveco.default_model, "deveco/cached-model")

    def test_refresh_scans_vendors_concurrently_but_preserves_vendor_order(self):
        concurrent_scan_started = threading.Event()
        started_lock = threading.Lock()
        started_vendors: list[str] = []

        def scanner_for(vendor_id: str):
            def scan(binary_path: str) -> VendorInventory:
                with started_lock:
                    started_vendors.append(vendor_id)
                    if len(started_vendors) >= 2:
                        concurrent_scan_started.set()
                if not concurrent_scan_started.wait(timeout=5.0):
                    raise RuntimeError("vendor scans were executed serially")
                return VendorInventory(
                    vendor_id=vendor_id,
                    installed=True,
                    scan_status=OK_SCAN_STATUS,
                    source_kind=SOURCE_DYNAMIC_CLI,
                    confidence=CONFIDENCE_HIGH,
                    binary_path=binary_path,
                    notes=("concurrent_scan",),
                )

            return scan

        scanners = {vendor_id: scanner_for(vendor_id) for vendor_id in VENDOR_ORDER}
        with patch(
            "tmux_core.runtime.vendor_catalog._resolved_vendor_binary_path",
            side_effect=lambda vendor_id: f"/opt/bin/{vendor_id}",
        ), patch("tmux_core.runtime.vendor_catalog._SCANNERS", scanners), patch(
            "tmux_core.runtime.vendor_catalog._save_cached_snapshot"
        ):
            snapshot = refresh_catalog_snapshot()

        self.assertEqual(tuple(item.vendor_id for item in snapshot.vendors), VENDOR_ORDER)
        self.assertTrue(all(item.scan_status == OK_SCAN_STATUS for item in snapshot.vendors))
        self.assertTrue(all(item.notes == ("concurrent_scan",) for item in snapshot.vendors))
        self.assertCountEqual(started_vendors, VENDOR_ORDER)

    def test_fresh_disk_cache_is_used_without_refresh_or_vendor_probe(self):
        fresh_snapshot = self._snapshot_at(datetime.now(timezone.utc).isoformat())

        with patch("tmux_core.runtime.vendor_catalog._load_cached_snapshot", return_value=fresh_snapshot), patch(
            "tmux_core.runtime.vendor_catalog.refresh_catalog_snapshot"
        ) as refresh, patch(
            "tmux_core.runtime.vendor_catalog._resolved_vendor_binary_path",
            side_effect=lambda _vendor_id: "",
        ) as resolve_binary, patch(
            "tmux_core.runtime.vendor_catalog._command_probe"
        ) as probe:
            snapshot = get_catalog_snapshot()

        self.assertIs(snapshot, fresh_snapshot)
        refresh.assert_not_called()
        self.assertEqual(resolve_binary.call_count, len(VENDOR_ORDER))
        probe.assert_not_called()

    def test_in_process_snapshot_rechecks_ttl_after_first_access(self):
        fresh_snapshot = self._snapshot_at(datetime.now(timezone.utc).isoformat())
        refreshed_snapshot = self._snapshot_at(datetime.now(timezone.utc).isoformat())

        with patch("tmux_core.runtime.vendor_catalog._load_cached_snapshot", return_value=fresh_snapshot), patch(
            "tmux_core.runtime.vendor_catalog._catalog_snapshot_is_fresh",
            side_effect=[True, False],
        ), patch(
            "tmux_core.runtime.vendor_catalog.refresh_catalog_snapshot",
            return_value=refreshed_snapshot,
        ) as refresh:
            first = get_catalog_snapshot()
            second = get_catalog_snapshot()

        self.assertIs(first, fresh_snapshot)
        self.assertIs(second, refreshed_snapshot)
        refresh.assert_called_once_with(prior_snapshot=fresh_snapshot)

    def test_refresh_vendor_catalog_scans_only_selected_vendor_and_preserves_order(self):
        old_snapshot = self._snapshot_at(datetime.now(timezone.utc).isoformat())
        refreshed_model = ModelInventory(
            vendor_id="opencode",
            model_id="live/provider-model",
            display_name="Live Model",
            source_kind=SOURCE_DYNAMIC_CLI,
            confidence=CONFIDENCE_HIGH,
            reasoning=ReasoningInventory(
                vendor_id="opencode",
                model_id="live/provider-model",
                source_kind=SOURCE_DYNAMIC_CLI,
                confidence=CONFIDENCE_HIGH,
                reasoning_control_mode="implicit_default",
                supports_reasoning=True,
                normalized_reasoning_levels=("high",),
            ),
        )
        refreshed_inventory = VendorInventory(
            vendor_id="opencode",
            installed=True,
            scan_status=OK_SCAN_STATUS,
            source_kind=SOURCE_DYNAMIC_CLI,
            confidence=CONFIDENCE_HIGH,
            binary_path="/usr/bin/opencode",
            models=(refreshed_model,),
            default_model=refreshed_model.model_id,
        )

        with patch("tmux_core.runtime.vendor_catalog.get_catalog_snapshot", return_value=old_snapshot), patch(
            "tmux_core.runtime.vendor_catalog._resolved_vendor_binary_path",
            return_value="/usr/bin/opencode",
        ) as resolve_binary, patch(
            "tmux_core.runtime.vendor_catalog._scan_vendor_inventory",
            return_value=refreshed_inventory,
        ) as scan, patch("tmux_core.runtime.vendor_catalog._save_cached_snapshot"):
            refreshed = refresh_vendor_catalog("opencode")

        self.assertEqual(tuple(item.vendor_id for item in refreshed.vendors), VENDOR_ORDER)
        self.assertEqual(refreshed.generated_at, old_snapshot.generated_at)
        self.assertEqual(refreshed.vendor("opencode").model_ids(), ("live/provider-model",))
        self.assertTrue(all(
            refreshed.vendor(vendor_id) is old_snapshot.vendor(vendor_id)
            for vendor_id in VENDOR_ORDER
            if vendor_id != "opencode"
        ))
        resolve_binary.assert_called_once_with("opencode")
        scan.assert_called_once()

    def test_public_inventory_getter_refreshes_selected_dynamic_vendor(self):
        snapshot = self._snapshot_at(datetime.now(timezone.utc).isoformat())

        with patch(
            "tmux_core.runtime.vendor_catalog.ensure_vendor_catalog_current",
            return_value=snapshot,
        ) as ensure:
            inventory = get_vendor_inventory("opencode")

        self.assertIs(inventory, snapshot.vendor("opencode"))
        ensure.assert_called_once_with("opencode")

    def test_focused_failure_does_not_overwrite_concurrent_successful_full_refresh(self):
        unavailable_snapshot = self._snapshot_at("2000-01-01T00:00:00+00:00")
        prior_inventory = VendorInventory(
            vendor_id="opencode",
            installed=True,
            scan_status=OK_SCAN_STATUS,
            source_kind=SOURCE_DYNAMIC_CLI,
            confidence=CONFIDENCE_HIGH,
            binary_path="/usr/bin/opencode",
            models=(),
            default_model="",
        )
        old_snapshot = CatalogSnapshot(
            schema_version=SCHEMA_VERSION,
            generated_at=unavailable_snapshot.generated_at,
            cache_path=unavailable_snapshot.cache_path,
            vendors=tuple(
                prior_inventory if item.vendor_id == "opencode" else item
                for item in unavailable_snapshot.vendors
            ),
        )
        successful_inventory = VendorInventory(
            vendor_id="opencode",
            installed=True,
            scan_status=OK_SCAN_STATUS,
            source_kind=SOURCE_DYNAMIC_CLI,
            confidence=CONFIDENCE_HIGH,
            binary_path="/usr/bin/opencode",
            models=(),
            default_model="",
        )
        self.assertEqual(prior_inventory, successful_inventory)
        self.assertIsNot(prior_inventory, successful_inventory)
        full_snapshot = CatalogSnapshot(
            schema_version=SCHEMA_VERSION,
            generated_at="2026-07-18T00:00:00+00:00",
            cache_path="/tmp/vendor_catalog.json",
            vendors=tuple(
                successful_inventory if item.vendor_id == "opencode" else item
                for item in old_snapshot.vendors
            ),
        )
        degraded_inventory = VendorInventory(
            vendor_id="opencode",
            installed=True,
            scan_status=DEGRADED_SCAN_STATUS,
            source_kind="none",
            confidence="low",
            binary_path="/usr/bin/opencode",
            models=(),
            default_model="",
        )

        def finish_after_full_refresh(_vendor_id, _binary_path, _prior_vendor):  # noqa: ANN001
            vendor_catalog_module._CATALOG_SNAPSHOT = full_snapshot  # noqa: SLF001
            return degraded_inventory

        with patch("tmux_core.runtime.vendor_catalog._load_cached_snapshot", return_value=old_snapshot), patch(
            "tmux_core.runtime.vendor_catalog._resolved_vendor_binary_path",
            return_value="/usr/bin/opencode",
        ), patch(
            "tmux_core.runtime.vendor_catalog._scan_vendor_inventory",
            side_effect=finish_after_full_refresh,
        ), patch("tmux_core.runtime.vendor_catalog._save_cached_snapshot"):
            refreshed = refresh_vendor_catalog("opencode")

        self.assertIs(refreshed.vendor("opencode"), successful_inventory)
        self.assertEqual(refreshed.generated_at, full_snapshot.generated_at)

    def test_selected_dynamic_vendor_refresh_avoids_stale_full_catalog_scan(self):
        stale_snapshot = self._snapshot_at("2000-01-01T00:00:00+00:00")
        refreshed_inventory = VendorInventory(
            vendor_id="deveco",
            installed=True,
            scan_status=OK_SCAN_STATUS,
            source_kind=SOURCE_DYNAMIC_CLI,
            confidence=CONFIDENCE_HIGH,
            binary_path="/usr/bin/deveco",
            models=(),
            default_model="",
        )

        with patch("tmux_core.runtime.vendor_catalog._load_cached_snapshot", return_value=stale_snapshot), patch(
            "tmux_core.runtime.vendor_catalog.refresh_catalog_snapshot",
        ) as full_refresh, patch(
            "tmux_core.runtime.vendor_catalog._resolved_vendor_binary_path",
            return_value="/usr/bin/deveco",
        ), patch(
            "tmux_core.runtime.vendor_catalog._scan_vendor_inventory",
            return_value=refreshed_inventory,
        ) as selected_scan, patch("tmux_core.runtime.vendor_catalog._save_cached_snapshot"):
            snapshot = ensure_vendor_catalog_current("deveco", max_age_sec=0)

        full_refresh.assert_not_called()
        selected_scan.assert_called_once()
        self.assertIs(snapshot.vendor("deveco"), refreshed_inventory)

    def test_selected_vendor_refresh_latch_reuses_one_probe_within_window(self):
        stale_snapshot = self._snapshot_at("2000-01-01T00:00:00+00:00")
        refreshed_inventory = VendorInventory(
            vendor_id="opencode",
            installed=True,
            scan_status=OK_SCAN_STATUS,
            source_kind=SOURCE_DYNAMIC_CLI,
            confidence=CONFIDENCE_HIGH,
            binary_path="/usr/bin/opencode",
            models=(),
            default_model="",
        )

        with patch("tmux_core.runtime.vendor_catalog._load_cached_snapshot", return_value=stale_snapshot), patch(
            "tmux_core.runtime.vendor_catalog._resolved_vendor_binary_path",
            return_value="/usr/bin/opencode",
        ), patch(
            "tmux_core.runtime.vendor_catalog._scan_vendor_inventory",
            return_value=refreshed_inventory,
        ) as selected_scan, patch("tmux_core.runtime.vendor_catalog._save_cached_snapshot"):
            first = ensure_vendor_catalog_current("opencode")
            second = ensure_vendor_catalog_current("opencode")

        self.assertIs(first, second)
        selected_scan.assert_called_once()

    def test_multiple_selected_vendor_refreshes_run_concurrently(self):
        snapshot = self._snapshot_at("2000-01-01T00:00:00+00:00")
        barrier = threading.Barrier(2, timeout=2.0)
        refreshed = {
            vendor_id: VendorInventory(
                vendor_id=vendor_id,
                installed=True,
                scan_status=OK_SCAN_STATUS,
                source_kind=SOURCE_DYNAMIC_CLI,
                confidence=CONFIDENCE_HIGH,
                binary_path=f"/usr/bin/{vendor_id}",
                models=(),
                default_model="",
            )
            for vendor_id in ("opencode", "deveco")
        }

        def scan_one(vendor_id, _binary_path, _prior_vendor):  # noqa: ANN001
            barrier.wait()
            return refreshed[vendor_id]

        with patch("tmux_core.runtime.vendor_catalog._load_cached_snapshot", return_value=snapshot), patch(
            "tmux_core.runtime.vendor_catalog._resolved_vendor_binary_path",
            side_effect=lambda vendor_id: f"/usr/bin/{vendor_id}",
        ), patch(
            "tmux_core.runtime.vendor_catalog._scan_vendor_inventory",
            side_effect=scan_one,
        ) as scan, patch("tmux_core.runtime.vendor_catalog._save_cached_snapshot"):
            merged = ensure_vendor_catalogs_current(("opencode", "deveco"), max_age_sec=0)

        self.assertIs(merged.vendor("opencode"), refreshed["opencode"])
        self.assertIs(merged.vendor("deveco"), refreshed["deveco"])
        self.assertEqual(scan.call_count, 2)

    def test_expired_disk_cache_triggers_refresh(self):
        expired_snapshot = self._snapshot_at("2000-01-01T00:00:00+00:00")
        refreshed_snapshot = self._snapshot_at(datetime.now(timezone.utc).isoformat())

        with patch("tmux_core.runtime.vendor_catalog._load_cached_snapshot", return_value=expired_snapshot), patch(
            "tmux_core.runtime.vendor_catalog.refresh_catalog_snapshot",
            return_value=refreshed_snapshot,
        ) as refresh:
            snapshot = get_catalog_snapshot()

        self.assertIs(snapshot, refreshed_snapshot)
        refresh.assert_called_once_with(prior_snapshot=expired_snapshot)

    def test_invalid_cache_timestamp_triggers_refresh(self):
        invalid_snapshot = self._snapshot_at("not-a-timestamp")
        refreshed_snapshot = self._snapshot_at(datetime.now(timezone.utc).isoformat())

        with patch("tmux_core.runtime.vendor_catalog._load_cached_snapshot", return_value=invalid_snapshot), patch(
            "tmux_core.runtime.vendor_catalog.refresh_catalog_snapshot",
            return_value=refreshed_snapshot,
        ) as refresh:
            snapshot = get_catalog_snapshot()

        self.assertIs(snapshot, refreshed_snapshot)
        refresh.assert_called_once_with(prior_snapshot=invalid_snapshot)

    def test_force_refresh_ignores_fresh_disk_cache(self):
        fresh_snapshot = self._snapshot_at(datetime.now(timezone.utc).isoformat())
        refreshed_snapshot = self._snapshot_at(datetime.now(timezone.utc).isoformat())

        with patch("tmux_core.runtime.vendor_catalog._load_cached_snapshot", return_value=fresh_snapshot), patch(
            "tmux_core.runtime.vendor_catalog.refresh_catalog_snapshot",
            return_value=refreshed_snapshot,
        ) as refresh:
            snapshot = get_catalog_snapshot(force_refresh=True)

        self.assertIs(snapshot, refreshed_snapshot)
        refresh.assert_called_once_with(prior_snapshot=fresh_snapshot)

    def test_future_cache_timestamp_triggers_refresh(self):
        future_snapshot = self._snapshot_at(
            (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
        )
        refreshed_snapshot = self._snapshot_at(datetime.now(timezone.utc).isoformat())

        with patch("tmux_core.runtime.vendor_catalog._load_cached_snapshot", return_value=future_snapshot), patch(
            "tmux_core.runtime.vendor_catalog.refresh_catalog_snapshot",
            return_value=refreshed_snapshot,
        ) as refresh:
            snapshot = get_catalog_snapshot()

        self.assertIs(snapshot, refreshed_snapshot)
        refresh.assert_called_once_with(prior_snapshot=future_snapshot)

    def test_fresh_cache_with_missing_executable_triggers_refresh(self):
        cached_vendors = list(self._snapshot_at(datetime.now(timezone.utc).isoformat()).vendors)
        cached_vendors[0] = VendorInventory(
            vendor_id="codex",
            installed=True,
            scan_status=OK_SCAN_STATUS,
            source_kind=SOURCE_DYNAMIC_CLI,
            confidence=CONFIDENCE_HIGH,
            binary_path="/definitely/missing/codex",
        )
        cached_snapshot = CatalogSnapshot(
            schema_version="1.0",
            generated_at=datetime.now(timezone.utc).isoformat(),
            cache_path="/tmp/vendor_catalog.json",
            vendors=tuple(cached_vendors),
        )
        refreshed_snapshot = self._snapshot_at(datetime.now(timezone.utc).isoformat())

        with patch("tmux_core.runtime.vendor_catalog._load_cached_snapshot", return_value=cached_snapshot), patch(
            "tmux_core.runtime.vendor_catalog.refresh_catalog_snapshot",
            return_value=refreshed_snapshot,
        ) as refresh:
            snapshot = get_catalog_snapshot()

        self.assertIs(snapshot, refreshed_snapshot)
        refresh.assert_called_once_with(prior_snapshot=cached_snapshot)

    def test_resolve_launch_maps_native_variant_prompt_and_boolean_modes(self):
        catalog = CatalogSnapshot(
            schema_version="1.0",
            generated_at="2026-04-22T00:00:00+00:00",
            cache_path="/tmp/catalog.json",
            vendors=(
                VendorInventory(
                    vendor_id="codex",
                    installed=True,
                    scan_status=OK_SCAN_STATUS,
                    source_kind=SOURCE_DYNAMIC_CLI,
                    confidence=CONFIDENCE_HIGH,
                    binary_path="/usr/bin/codex",
                    default_model="gpt-5.4",
                    models=(
                        ModelInventory(
                            vendor_id="codex",
                            model_id="gpt-5.4",
                            display_name="gpt-5.4",
                            source_kind=SOURCE_DYNAMIC_CLI,
                            confidence=CONFIDENCE_HIGH,
                            reasoning=ReasoningInventory(
                                vendor_id="codex",
                                model_id="gpt-5.4",
                                source_kind=SOURCE_DYNAMIC_CLI,
                                confidence=CONFIDENCE_HIGH,
                                reasoning_control_mode=REASONING_NATIVE,
                                supports_reasoning=True,
                                native_reasoning_levels=("low", "medium", "high", "xhigh"),
                                normalized_reasoning_levels=("low", "medium", "high", "xhigh", "max"),
                                default_normalized_effort="high",
                                default_native_level="medium",
                            ),
                        ),
                    ),
                ),
                VendorInventory(
                    vendor_id="opencode",
                    installed=True,
                    scan_status=OK_SCAN_STATUS,
                    source_kind=SOURCE_DYNAMIC_CLI,
                    confidence=CONFIDENCE_HIGH,
                    binary_path="/usr/bin/opencode",
                    default_model="opencode/gpt-5-nano",
                    models=(
                        ModelInventory(
                            vendor_id="opencode",
                            model_id="opencode/gpt-5-nano",
                            display_name="GPT-5 Nano",
                            source_kind=SOURCE_DYNAMIC_CLI,
                            confidence=CONFIDENCE_HIGH,
                            reasoning=ReasoningInventory(
                                vendor_id="opencode",
                                model_id="opencode/gpt-5-nano",
                                source_kind=SOURCE_DYNAMIC_CLI,
                                confidence=CONFIDENCE_HIGH,
                                reasoning_control_mode=REASONING_MAPPED,
                                supports_reasoning=True,
                                native_reasoning_levels=("minimal", "low", "medium", "high"),
                                normalized_reasoning_levels=("low", "medium", "high", "xhigh", "max"),
                                default_normalized_effort="high",
                                default_native_level="medium",
                            ),
                        ),
                    ),
                ),
                VendorInventory(
                    vendor_id="gemini",
                    installed=True,
                    scan_status=OK_SCAN_STATUS,
                    source_kind=SOURCE_PACKAGE_METADATA,
                    confidence=CONFIDENCE_MEDIUM,
                    binary_path="/usr/bin/gemini",
                    default_model="auto",
                    models=(
                        ModelInventory(
                            vendor_id="gemini",
                            model_id="auto",
                            display_name="auto",
                            source_kind=SOURCE_PACKAGE_METADATA,
                            confidence=CONFIDENCE_MEDIUM,
                            synthetic=True,
                            reasoning=ReasoningInventory(
                                vendor_id="gemini",
                                model_id="auto",
                                source_kind=SOURCE_PACKAGE_METADATA,
                                confidence=CONFIDENCE_MEDIUM,
                                reasoning_control_mode=REASONING_MODEL_FAMILY_ROUTING,
                                supports_reasoning=True,
                                native_reasoning_levels=(),
                                normalized_reasoning_levels=("low", "medium", "high", "xhigh", "max"),
                                default_normalized_effort="high",
                                default_native_level="",
                            ),
                        ),
                    ),
                ),
            ),
        )

        codex_resolution = resolve_launch("codex", "gpt-5.4", "max", catalog=catalog)
        self.assertEqual(codex_resolution.native_reasoning_level, "xhigh")

        opencode_resolution = resolve_launch("opencode", "opencode/gpt-5-nano", "max", catalog=catalog)
        self.assertEqual(opencode_resolution.resolved_variant, "high")

        gemini_resolution = resolve_launch("gemini", "auto", "medium", catalog=catalog)
        self.assertEqual(gemini_resolution.resolved_model, "flash")

    def test_codex_gpt5_alias_tracks_scanned_default_without_reviving_retired_model(self):
        current_model = ModelInventory(
            vendor_id="codex",
            model_id="gpt-5.5",
            display_name="GPT-5.5",
            source_kind=SOURCE_DYNAMIC_CLI,
            confidence=CONFIDENCE_HIGH,
            reasoning=ReasoningInventory(
                vendor_id="codex",
                model_id="gpt-5.5",
                source_kind=SOURCE_DYNAMIC_CLI,
                confidence=CONFIDENCE_HIGH,
                reasoning_control_mode=REASONING_NATIVE,
                supports_reasoning=True,
                native_reasoning_levels=("low", "medium", "high", "xhigh"),
                normalized_reasoning_levels=("low", "medium", "high", "xhigh", "max"),
                default_normalized_effort="high",
                default_native_level="medium",
            ),
        )
        catalog = CatalogSnapshot(
            schema_version=SCHEMA_VERSION,
            generated_at="2026-07-26T00:00:00+00:00",
            cache_path="/tmp/catalog.json",
            vendors=(
                VendorInventory(
                    vendor_id="codex",
                    installed=True,
                    scan_status=OK_SCAN_STATUS,
                    source_kind=SOURCE_DYNAMIC_CLI,
                    confidence=CONFIDENCE_HIGH,
                    binary_path="/usr/bin/codex",
                    models=(current_model,),
                    default_model=current_model.model_id,
                ),
            ),
        )

        alias_resolution = resolve_launch("codex", "gpt-5", "high", catalog=catalog)
        self.assertEqual(alias_resolution.resolved_model, "gpt-5.5")
        with self.assertRaisesRegex(ValueError, "model unavailable"):
            resolve_launch("codex", "gpt-5.4", "high", catalog=catalog)

    def test_removed_qwen_and_kimi_vendors_are_rejected(self):
        for vendor_id in ("qwen", "kimi"):
            with self.subTest(vendor=vendor_id):
                with self.assertRaises(ValueError):
                    normalize_vendor_id(vendor_id)
                with self.assertRaises(ValueError):
                    resolve_launch(vendor_id, "default", "medium")

    @unittest.skipUnless(os.environ.get("TMUX_RUN_VENDOR_DISCOVERY_SMOKE") == "1", "vendor smoke tests are opt-in")
    def test_live_vendor_catalog_smoke(self):
        installed_vendors = [vendor_id for vendor_id in VENDOR_ORDER if get_vendor_inventory(vendor_id).installed]
        self.assertEqual(installed_vendors, list(VENDOR_ORDER))
        self.assertTrue(get_default_model_for_vendor("opencode"))
        self.assertGreater(len(get_model_choices("codex")), 0)
        self.assertGreater(len(get_model_choices("opencode")), 0)
