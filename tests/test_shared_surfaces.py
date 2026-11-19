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


def test_manifest_has_the_required_first_batch_and_behavior_basis():
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    entries = {entry["name"]: entry for entry in manifest["surfaces"]}
    assert set(entries) == {
        "extension_identity_hash",
        "opencli_patch_registration",
        "user_preference_write",
        "process_startup",
        "completeness_summary",
        "ai_chat_title_cleaning",
        "safe_receipt_shape",
    }
    assert entries["completeness_summary"]["owner"] == "core/completeness.py"
    assert all(entry.get("detector") and entry.get("detection_basis") for entry in entries.values())


def test_scanner_catches_historical_behavior_shapes(tmp_path):
    cases = {
        "hash_duplicate.py": """
import hashlib
def _extension_hashes(root):
    result = {}
    for path in root.rglob('*'):
        if path.is_file():
            result[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result
""",
        "patch_duplicate.py": """
PATCHES = [('loader edit', 'before source', 'after source', 1)]
def apply(source):
    return source.replace(PATCHES[0][1], PATCHES[0][2])
""",
        "preference_duplicate.py": """
from pathlib import Path
def write_preferences(value):
    Path('preferences.json').write_text(value, encoding='utf-8')
""",
        "process_duplicate.py": """
import subprocess as sp
def launch(command):
    return sp.Popen(command)
""",
        "completeness_duplicate.py": """
def summarize(checks):
    return 'downgrade' if any(check.status == 'downgrade' for check in checks) else ('ok' if all(check.status in {'ok', 'unknown'} for check in checks) else 'unknown')
""",
        "title_duplicate.py": r"""
import re
def clean(title):
    return re.sub(r'\s*[-–—|·]\s*(?:豆包|Google Gemini)\s*$', '', title).strip()
""",
        "unsafe_job_receipt.py": """
def persist_result(status, result):
    status[\"result\"] = {key: result.get(key) for key in (\"status\", \"source_level\", \"completeness\", \"error_code\", \"next_action\") if result.get(key) is not None}
""",
        "unsafe_job_error.py": """
def persist_error(status, result):
    status[\"receipt\"][\"error\"] = result[\"error\"]
""",
    }
    for name, source in cases.items():
        (tmp_path / name).write_text(source, encoding="utf-8")
    violations = scan_shared_surfaces(tmp_path, MANIFEST)
    found = {item.surface for item in violations}
    assert found == {
        "extension_identity_hash",
        "opencli_patch_registration",
        "user_preference_write",
        "process_startup",
        "completeness_summary",
        "ai_chat_title_cleaning",
        "safe_receipt_shape",
    }, format_violations(violations)


def test_owner_calls_are_not_second_implementations(tmp_path):
    (tmp_path / "caller.py").write_text(
        """
from pathlib import Path
from openmd.browser_extension_sync import extension_file_hashes
from openmd.runtime.opencli_patches import BRIDGE_PATCHES
from openmd.core.preferences import save_preferences
from openmd.runtime.interpreters import owned_popen
from openmd.core.completeness import check_completeness
from openmd.core.chat_title import clean_chat_title
from openmd.mcp_jobs import receipt

def use_owners(item, result, command):
    extension_file_hashes(Path('.'))
    len(BRIDGE_PATCHES)
    save_preferences()
    owned_popen(command)
    check_completeness(item, result)
    return clean_chat_title('gemini', 'title')

def persist_safe(status, result):
    status['result'] = receipt(result)
""",
        encoding="utf-8",
    )
    assert scan_shared_surfaces(tmp_path, MANIFEST) == []


def test_collection_coverage_statistics_are_not_completeness_surface(tmp_path):
    (tmp_path / "coverage.py").write_text(
        """
def as_dict(results):
    completeness_counts = {'ok': len(results)}
    return {'completeness_counts': completeness_counts}
""",
        encoding="utf-8",
    )
    assert scan_shared_surfaces(tmp_path, MANIFEST) == []
