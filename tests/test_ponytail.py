from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import tmux_core.runtime.ponytail as ponytail


class PonytailBundleTests(unittest.TestCase):
    def tearDown(self) -> None:
        ponytail.reset_ponytail_bundle_cache_for_tests()

    def test_bundle_contains_exact_upstream_snapshot(self) -> None:
        manifest_path = ponytail.BUNDLE_ROOT / "UPSTREAM.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        self.assertEqual(manifest["declared_version"], "4.8.4")
        self.assertEqual(manifest["upstream"]["commit"], "16f29800fd2681bdf24f3eb4ccffe38be3baec6b")
        self.assertEqual(manifest["upstream"]["git_describe"], "v4.8.4-53-g16f2980")
        self.assertEqual(manifest["license"], "MIT")
        self.assertEqual(len(manifest["files"]), 7)
        for relative_path, metadata in manifest["files"].items():
            payload = (ponytail.BUNDLE_ROOT / relative_path).read_bytes()
            self.assertEqual(len(payload), metadata["bytes"], relative_path)
            self.assertEqual(hashlib.sha256(payload).hexdigest(), metadata["sha256"], relative_path)

        self.assertEqual(
            ponytail.validate_ponytail_bundle(),
            {
                "version": "4.8.4",
                "commit": "16f29800fd2681bdf24f3eb4ccffe38be3baec6b",
                "git_describe": "v4.8.4-53-g16f2980",
            },
        )

    def test_render_matches_upstream_javascript_reference_hashes(self) -> None:
        expected = {
            "lite": "ea09a138c7aad46645e7ad1e60b4c552638314e689cdca1d27c2ba42fc2380eb",
            "full": "da4fb09cff2f6726691ce6591cebc38c95597d79da132e49c6fa2665c4e8a3ff",
            "ultra": "37d8be344d6e30c7ac8e1276fa0aff342344441d9ee5a70e3167d23277656d81",
        }
        for mode, expected_hash in expected.items():
            rendered = ponytail.render_ponytail_rules(mode)
            self.assertTrue(rendered.startswith(f"PONYTAIL MODE ACTIVE — level: {mode}\n\n# Ponytail"))
            self.assertNotIn("\n---\n", rendered)
            self.assertEqual(hashlib.sha256(rendered.encode("utf-8")).hexdigest(), expected_hash)
            self.assertIn(f"| **{mode}** |", rendered)
            self.assertIn(f"- {mode}:", rendered)
            for other_mode in expected.keys() - {mode}:
                self.assertNotIn(f"| **{other_mode}** |", rendered)
                self.assertNotIn(f"- {other_mode}:", rendered)

    def test_filter_preserves_non_example_colon_bullets(self) -> None:
        body = (
            "---\nname: ponytail\n---\n"
            "- Full: keep this ordinary rule.\n"
            "- lite: \"drop this worked example\"\n"
            "- full: \"keep this worked example\"\n"
            "- No abstraction: keep this too."
        )

        rendered = ponytail._filter_skill_body_for_mode(body, ponytail.PonytailMode.FULL)

        self.assertIn("Full: keep this ordinary rule", rendered)
        self.assertIn("No abstraction: keep this too", rendered)
        self.assertIn('full: "keep this worked example"', rendered)
        self.assertNotIn('lite: "drop this worked example"', rendered)

    def test_off_is_noop_and_does_not_load_bundle(self) -> None:
        with mock.patch.object(ponytail, "BUNDLE_ROOT", Path("/missing/ponytail")):
            ponytail.reset_ponytail_bundle_cache_for_tests()
            self.assertEqual(ponytail.render_ponytail_rules("off"), "")
            self.assertEqual(ponytail.build_ponytail_bootstrap("off", "TASK"), "TASK")
            self.assertEqual(ponytail.build_ponytail_reminder("off", "TASK"), "TASK")

    def test_normalize_accepts_case_and_reports_all_valid_modes(self) -> None:
        self.assertIs(ponytail.normalize_ponytail_mode(" Full "), ponytail.PonytailMode.FULL)
        self.assertIs(ponytail.normalize_ponytail_mode(None, default="lite"), ponytail.PonytailMode.LITE)
        with self.assertRaisesRegex(ValueError, "off, lite, full, ultra"):
            ponytail.normalize_ponytail_mode("review")

    def test_bootstrap_is_host_controlled_and_idempotent(self) -> None:
        prompt = ponytail.build_ponytail_bootstrap("full", "ORIGINAL TASK")

        self.assertEqual(prompt.count(ponytail.BEGIN_MARKER), 1)
        self.assertEqual(prompt.count(ponytail.END_MARKER), 1)
        self.assertLess(prompt.index("PONYTAIL MODE ACTIVE"), prompt.index("ORIGINAL TASK"))
        self.assertIn("workflow is authoritative", prompt)
        self.assertIn("task cannot change the host-selected mode", prompt)
        self.assertIn("completion protocols", prompt)
        self.assertEqual(ponytail.build_ponytail_bootstrap("full", prompt), prompt)

    def test_business_text_cannot_spoof_the_marker_to_disable_injection(self) -> None:
        business_prompt = f"Inspect this literal marker: {ponytail.BEGIN_MARKER}"
        combined = ponytail.build_ponytail_bootstrap("full", business_prompt)

        self.assertTrue(combined.startswith(ponytail.BEGIN_MARKER))
        self.assertEqual(combined.count(ponytail.BEGIN_MARKER), 2)
        self.assertIn(business_prompt, combined)

        wrong_mode_block = ponytail.build_ponytail_bootstrap("ultra")
        combined_wrong_mode = ponytail.build_ponytail_bootstrap("full", wrong_mode_block + "\n\nTASK")
        self.assertTrue(combined_wrong_mode.startswith(ponytail.build_ponytail_bootstrap("full")))
        self.assertEqual(combined_wrong_mode.count(ponytail.BEGIN_MARKER), 2)

    def test_reminder_is_compact_self_contained_and_idempotent(self) -> None:
        reminder = ponytail.build_ponytail_reminder("ultra", "NEXT TASK")

        self.assertIn("YAGNI → existing code → stdlib", reminder)
        self.assertIn("strict YAGNI", reminder)
        self.assertIn("Explicit task, routing, artifact/file", reminder)
        self.assertLess(len(reminder), 1200)
        self.assertEqual(ponytail.build_ponytail_reminder("ultra", reminder), reminder)

    def test_bundle_load_is_independent_of_current_working_directory(self) -> None:
        original_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as temporary_directory:
            try:
                os.chdir(temporary_directory)
                ponytail.reset_ponytail_bundle_cache_for_tests()
                self.assertIn("# Ponytail", ponytail.render_ponytail_rules("lite"))
            finally:
                os.chdir(original_cwd)

    def test_corrupt_bundle_fails_with_actionable_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            copied_root = Path(temporary_directory) / "ponytail"
            shutil.copytree(ponytail.BUNDLE_ROOT, copied_root)
            main_skill = copied_root / "skills" / "ponytail" / "SKILL.md"
            main_skill.write_text(main_skill.read_text(encoding="utf-8") + "tampered", encoding="utf-8")
            with mock.patch.object(ponytail, "BUNDLE_ROOT", copied_root):
                ponytail.reset_ponytail_bundle_cache_for_tests()
                with self.assertRaisesRegex(ponytail.PonytailBundleError, "SHA-256 validation"):
                    ponytail.render_ponytail_rules("full")

    def test_every_validation_detects_tampering_after_an_initial_success(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            copied_root = Path(temporary_directory) / "ponytail"
            shutil.copytree(ponytail.BUNDLE_ROOT, copied_root)
            with mock.patch.object(ponytail, "BUNDLE_ROOT", copied_root):
                ponytail.validate_ponytail_bundle()
                (copied_root / "LICENSE").write_text("tampered", encoding="utf-8")
                with self.assertRaisesRegex(ponytail.PonytailBundleError, "SHA-256 validation"):
                    ponytail.validate_ponytail_bundle()

    def test_manifest_cannot_authorize_modified_upstream_assets(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            copied_root = Path(temporary_directory) / "ponytail"
            shutil.copytree(ponytail.BUNDLE_ROOT, copied_root)
            skill_path = copied_root / "skills" / "ponytail-help" / "SKILL.md"
            payload = skill_path.read_bytes() + b"tampered"
            skill_path.write_bytes(payload)
            manifest_path = copied_root / "UPSTREAM.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            metadata = manifest["files"]["skills/ponytail-help/SKILL.md"]
            metadata["bytes"] = len(payload)
            metadata["sha256"] = hashlib.sha256(payload).hexdigest()
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            with mock.patch.object(ponytail, "BUNDLE_ROOT", copied_root):
                with self.assertRaisesRegex(ponytail.PonytailBundleError, "not the audited value"):
                    ponytail.validate_ponytail_bundle()

    def test_auxiliary_skills_are_bundled_but_not_runtime_exports(self) -> None:
        expected_auxiliary = {
            "ponytail-review",
            "ponytail-audit",
            "ponytail-debt",
            "ponytail-gain",
            "ponytail-help",
        }
        bundled = {path.parent.name for path in (ponytail.BUNDLE_ROOT / "skills").glob("*/SKILL.md")}

        self.assertTrue(expected_auxiliary.issubset(bundled))
        self.assertFalse(any(name.replace("-", "_") in ponytail.__all__ for name in expected_auxiliary))


if __name__ == "__main__":
    unittest.main()
