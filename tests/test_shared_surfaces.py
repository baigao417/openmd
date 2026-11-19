"""Executable single-owner checks for semantic merge conflicts."""
from __future__ import annotations

import json
from pathlib import Path

from shared_surface_scanner import format_violations, scan_shared_surfaces


ROOT = Path(__file__).parents[1]
SOURCE = ROOT / "src" / "openmd"
MANIFEST = SOURCE / "contracts" / "shared_surfaces.json"


def test_declared_shared_surfaces_have_no_second_implementation():
    violations = scan_shared_surfaces(SOURCE, MANIFEST)
    assert not violations, format_violations(violations)


def test_manifest_has_the_required_first_batch():
    names = {entry["name"] for entry in json.loads(MANIFEST.read_text(encoding="utf-8"))["surfaces"]}
    assert names == {
        "extension_identity_hash",
        "opencli_patch_registration",
        "user_preference_write",
        "process_startup",
        "completeness_summary",
        "ai_chat_title_cleaning",
    }


def test_scanner_catches_second_extension_hash_implementation(tmp_path):
    (tmp_path / "duplicate.py").write_text(
        "import hashlib\n\ndef duplicate_extension_identity(data):\n    extension_manifest = 'manifest.json'\n    return hashlib.sha256(data).hexdigest()\n",
        encoding="utf-8",
    )
    violations = scan_shared_surfaces(tmp_path, MANIFEST)
    assert any(item.surface == "extension_identity_hash" and item.evidence == "ast:hashlib.sha256" for item in violations)


def test_scanner_catches_owner_outside_preference_write(tmp_path):
    (tmp_path / "duplicate.py").write_text(
        "from pathlib import Path\nPath('preferences.json').write_text('{}')\n",
        encoding="utf-8",
    )
    violations = scan_shared_surfaces(tmp_path, MANIFEST)
    assert any(item.surface == "user_preference_write" for item in violations)
