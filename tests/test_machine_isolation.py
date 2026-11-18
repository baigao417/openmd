"""The default test environment must not consume developer runtime state."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from openmd.core.local_config import load_local_config, local_config_path
from openmd.core.preferences import data_directory
from openmd.runtime.managed_tools import managed_root
from openmd.runtime.prepared_models import prepared_root


def test_default_config_is_empty_and_explicit():
    # Read only: reproducing this leak must never attempt a real tool install.
    assert "OPENMD_CONFIG" in os.environ
    assert local_config_path() == Path(os.environ["OPENMD_CONFIG"]).resolve()
    assert load_local_config() == {"schema": "openmd.local/v1"}


def test_default_runtime_paths_belong_to_test_sandbox(isolated_machine):
    root = isolated_machine.resolve()
    for path in (Path.home(), data_directory(), managed_root(), prepared_root(),
                 *(Path(os.environ[key]) for key in (
                     "OPENMD_RUNTIME_DIR", "HF_HOME", "HF_HUB_CACHE",
                     "MODELSCOPE_CACHE", "OPENCLI_CONFIG_DIR"))):
        assert path.resolve().is_relative_to(root)


def test_sandbox_does_not_populate_output_directory(tmp_path):
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("use_checkout", [True, False])
def test_config_discovery_requires_explicit_temporary_opt_in(tmp_path, monkeypatch, use_checkout):
    from openmd.core import local_config

    checkout = tmp_path / "checkout"
    candidate = (checkout / "out/local-config.json" if use_checkout
                 else Path.home() / ".openmd/local.json")
    candidate.parent.mkdir(parents=True)
    value = {"schema": "openmd.local/v1", "tools": {
        "opencli": {"root": str(tmp_path / "selected-tools")}}}
    candidate.write_text(json.dumps(value), encoding="utf-8")
    monkeypatch.setattr(local_config, "checkout_root", lambda: checkout)
    # Even a discoverable config is ignored until this test opts into discovery.
    assert load_local_config() == {"schema": "openmd.local/v1"}
    monkeypatch.delenv("OPENMD_CONFIG")
    assert local_config_path() == candidate
    assert load_local_config() == value


def test_suite_overrides_inherited_machine_locations_before_collection(tmp_path):
    suite = tmp_path / "suite"
    suite.mkdir()
    shutil.copyfile(Path(__file__).with_name("conftest.py"), suite / "conftest.py")
    protected = tmp_path / "developer-runtime"
    protected.mkdir()
    config = protected / "local.json"
    config.write_text(json.dumps({"schema": "openmd.local/v1", "tools": {
        "opencli": {"root": str(protected / "tools")}}}), encoding="utf-8")
    before = config.stat().st_mtime_ns, config.read_bytes()
    # These checks execute both while collecting and in separate test sandboxes.
    (suite / "test_probe.py").write_text('''
import os
from pathlib import Path
from openmd.core.local_config import load_local_config
from openmd.core.preferences import data_directory
from openmd.runtime.managed_tools import managed_root

assert load_local_config() == {"schema": "openmd.local/v1"}
assert os.environ["OPENMD_AUTO_INSTALL_TOOLS"] == "0"
assert "OPENMD_PREPARED_MODEL_ROOT" not in os.environ

def test_first(isolated_machine):
    assert Path.home().is_relative_to(isolated_machine)
    assert managed_root().is_relative_to(isolated_machine)
    os.environ["OPENMD_CONFIG"] = "absent.json"

def test_second(isolated_machine):
    assert load_local_config() == {"schema": "openmd.local/v1"}
    assert data_directory().is_relative_to(isolated_machine)
''', encoding="utf-8")
    env = {**os.environ, **dict.fromkeys((
        "HOME", "USERPROFILE", "OPENMD_DATA_DIR", "OPENMD_RUNTIME_DIR",
        "OPENMD_PREPARED_MODEL_ROOT", "HF_HOME", "HF_HUB_CACHE",
        "MODELSCOPE_CACHE"), str(protected)),
        "OPENMD_CONFIG": str(config), "OPENMD_AUTO_INSTALL_TOOLS": "1",
        # The copied conftest belongs to this checkout, not an editable install
        # that may point at a different branch.
        "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
    result = subprocess.run([sys.executable, "-m", "pytest", "-q",
                             "-p", "no:cacheprovider", str(suite)],
                            cwd=suite, env=env, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "2 passed" in result.stdout
    assert (config.stat().st_mtime_ns, config.read_bytes()) == before
    assert list(protected.iterdir()) == [config]
