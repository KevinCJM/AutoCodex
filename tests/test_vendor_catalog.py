from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from tmux_core.runtime.vendor_catalog import (
    CatalogSnapshot,
    CONFIDENCE_HIGH,
    CONFIDENCE_MEDIUM,
    ModelInventory,
    OK_SCAN_STATUS,
    REASONING_MAPPED,
    REASONING_MODEL_FAMILY_ROUTING,
    REASONING_NATIVE,
    ReasoningInventory,
    SOURCE_DYNAMIC_CLI,
    SOURCE_PACKAGE_METADATA,
    VendorInventory,
    VENDOR_ORDER,
    get_default_model_for_vendor,
    get_model_choices,
    get_vendor_inventory,
    normalize_vendor_id,
    parse_codex_models_output,
    parse_opencode_debug_config_output,
    parse_opencode_verbose_output,
    resolve_launch,
    _build_gemini_models,
)


class VendorCatalogTests(unittest.TestCase):
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
