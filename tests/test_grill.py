from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path
from unittest import mock

import pytest

import tmux_core.runtime.grill as grill


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def test_bundle_manifest_covers_exact_audited_files() -> None:
    manifest = json.loads((grill.BUNDLE_ROOT / "UPSTREAM.json").read_text(encoding="utf-8"))
    assert manifest["upstream"]["commit"] == grill.BUNDLE_COMMIT
    assert manifest["license"] == "MIT"
    assert set(manifest["files"]) == {
        "LICENSE",
        "skills/grill-me/SKILL.md",
        "skills/grill-with-docs/SKILL.md",
        "skills/grilling/SKILL.md",
        "skills/domain-modeling/SKILL.md",
        "skills/domain-modeling/CONTEXT-FORMAT.md",
        "skills/domain-modeling/ADR-FORMAT.md",
    }
    for relative_path, metadata in manifest["files"].items():
        payload = (grill.BUNDLE_ROOT / relative_path).read_bytes()
        assert len(payload) == metadata["bytes"]
        assert _sha256(payload) == metadata["sha256"]
    assert grill.validate_grill_bundle()["commit"] == grill.BUNDLE_COMMIT


def test_modes_normalize_and_profile_validates_sequence() -> None:
    assert grill.REQUIREMENTS_MODE_CHOICES == ("standard", "grill", "grill-with-docs")
    assert grill.normalize_requirements_mode(" Grill_With_Docs ") is grill.RequirementsMode.GRILL_WITH_DOCS
    assert grill.GrillTurnProfile("grill", 2).enabled is True
    assert grill.GrillTurnProfile("standard").enabled is False
    with pytest.raises(ValueError, match="expected one of"):
        grill.normalize_requirements_mode("automatic")
    with pytest.raises(ValueError, match="non-negative"):
        grill.GrillTurnProfile("grill", -1)


def test_standard_mode_never_loads_bundle() -> None:
    with mock.patch.object(grill, "BUNDLE_ROOT", Path("/missing/grill-bundle")):
        assert grill.render_grill_rules("standard") == ""
        assert grill.build_grill_bootstrap("standard", "TASK") == "TASK"
        assert grill.build_grill_reminder("standard", "TASK") == "TASK"


def test_rendered_rules_are_self_contained_and_remove_host_frontmatter() -> None:
    plain = grill.render_grill_rules("grill")
    docs = grill.render_grill_rules("grill-with-docs")

    assert plain.startswith("GRILL REQUIREMENTS MODE ACTIVE")
    assert "name: grilling" not in plain
    assert "/grilling" not in plain
    assert "Upstream domain-modeling rules" not in plain
    assert "Upstream domain-modeling rules" in docs
    assert "Embedded CONTEXT.md format" in docs
    assert "Embedded ADR format" in docs
    assert "name: domain-modeling" not in docs
    assert "./CONTEXT-FORMAT.md" not in docs
    assert "./ADR-FORMAT.md" not in docs


def test_bootstrap_and_reminder_are_idempotent_and_host_controlled() -> None:
    profile = grill.GrillTurnProfile("grill-with-docs", 4)
    bootstrap = grill.build_grill_bootstrap(profile, "BUSINESS TASK")
    assert bootstrap.count(grill.BEGIN_MARKER) == 1
    assert "question_seq: 4" in bootstrap
    assert "Ask exactly one decision question per turn" in bootstrap
    assert "never publish or choose a project path yourself" in bootstrap
    assert bootstrap.index(grill.BEGIN_MARKER) < bootstrap.index("BUSINESS TASK")
    assert grill.build_grill_bootstrap(profile, bootstrap) == bootstrap

    reminder = grill.build_grill_reminder(grill.GrillTurnProfile("grill", 5), "NEXT")
    assert reminder.count(grill.BEGIN_MARKER) == 1
    assert "GRILL PROFILE REMINDER" in reminder
    assert "recommended answer" in reminder
    assert grill.build_grill_reminder(grill.GrillTurnProfile("grill", 5), reminder) == reminder


def test_bundle_can_load_from_any_current_working_directory(tmp_path: Path) -> None:
    previous = Path.cwd()
    try:
        os.chdir(tmp_path)
        assert "Upstream grilling rules" in grill.render_grill_rules("grill")
    finally:
        os.chdir(previous)


def test_tampered_bundle_is_rejected(tmp_path: Path) -> None:
    copied_root = tmp_path / "mattpocock-skills"
    shutil.copytree(grill.BUNDLE_ROOT, copied_root)
    skill_path = copied_root / "skills" / "grilling" / "SKILL.md"
    skill_path.write_text(skill_path.read_text(encoding="utf-8") + "\nchanged\n", encoding="utf-8")
    with mock.patch.object(grill, "BUNDLE_ROOT", copied_root):
        with pytest.raises(grill.GrillBundleError, match="SHA-256 validation"):
            grill.render_grill_rules("grill")


def test_manifest_metadata_cannot_redefine_the_audited_bundle(tmp_path: Path) -> None:
    copied_root = tmp_path / "mattpocock-skills"
    shutil.copytree(grill.BUNDLE_ROOT, copied_root)
    manifest_path = copied_root / "UPSTREAM.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"]["skills/grilling/SKILL.md"]["sha256"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with mock.patch.object(grill, "BUNDLE_ROOT", copied_root):
        with pytest.raises(grill.GrillBundleError, match="not the audited value"):
            grill.validate_grill_bundle()
