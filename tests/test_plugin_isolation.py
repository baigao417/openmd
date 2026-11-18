"""Core collection stays independent of installed, possibly unfinished plugins."""
import os
from pathlib import Path
import subprocess
import sys


def test_broken_external_plugin_is_ignored_during_collection_and_tests(tmp_path):
    suite = tmp_path / "suite"
    suite.mkdir()
    conftest = Path(__file__).with_name("conftest.py").read_text(encoding="utf-8")
    # Simulate an installed plugin before pytest_configure, without installing
    # anything or changing the controller's real entry points.
    injection = '''
from importlib import metadata
from types import SimpleNamespace
_original_entries = metadata.entry_points
def _broken_load():
    raise RuntimeError("synthetic_unfinished_plugin")
_broken_entry = SimpleNamespace(name="broken", value="broken:plugin", group="openmd.platforms", load=_broken_load)
def _installed_entries(**kwargs):
    if kwargs.get("group") == "openmd.platforms":
        return [_broken_entry]
    return _original_entries(**kwargs)
metadata.entry_points = _installed_entries
'''
    (suite / "conftest.py").write_text(injection + conftest, encoding="utf-8")
    (suite / "test_probe.py").write_text('''
import pytest
from conftest import _broken_entry
from openmd.core.plugins import load_plugins, platform_ids
from openmd.routing.loader import load_catalog

assert platform_ids() == ()
assert not any(r["source"] == "broken" for r in load_catalog().routes)

def test_core_isolated():
    assert platform_ids() == ()

def test_plugin_contract_still_detects_damage(openmd_plugin_entries):
    openmd_plugin_entries.append(_broken_entry)
    with pytest.raises(RuntimeError, match="synthetic_unfinished_plugin"):
        load_plugins()
''', encoding="utf-8")
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
    result = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(suite)],
                            cwd=suite, env=env, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "2 passed" in result.stdout


def test_core_subprocess_inherits_external_plugin_isolation():
    # A metadata monkeypatch in pytest cannot reach a fresh Python process.
    # Reproduce a CLI-style child with a corrupt installed descriptor.
    code = '''
from importlib import metadata
from types import SimpleNamespace
def broken():
    raise RuntimeError("synthetic_unfinished_plugin")
metadata.entry_points = lambda **kwargs: [SimpleNamespace(name="broken", value="broken:plugin", load=broken)]
from openmd.core.plugins import platform_ids
from openmd.routing.loader import load_catalog
assert platform_ids() == ()
assert load_catalog().routes
'''
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
    result = subprocess.run([sys.executable, "-c", code], env=env,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
