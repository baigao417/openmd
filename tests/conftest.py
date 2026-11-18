"""Offline tests use temporary machine state, including during collection."""

import os
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest


@pytest.fixture
def pinned_ai_package(tmp_path):
    """Synthetic pinned package with the real loader ABI and a stub registry."""
    (tmp_path / 'package.json').write_text('{"type":"module","version":"2.0.3"}', encoding='utf-8')
    registry = tmp_path / 'node_modules/@jackwener/opencli'
    registry.mkdir(parents=True)
    (registry / 'package.json').write_text('{"type":"module","exports":{"./registry":"./registry.js"}}', encoding='utf-8')
    (registry / 'registry.js').write_text('export const cli=x=>x; export const Strategy={};', encoding='utf-8')
    return tmp_path


def _isolate_machine(monkeypatch, root):
    # Inherited model/interpreter/host overrides must not escape to real paths.
    for key in tuple(os.environ):
        if key.startswith("OPENMD_"):
            monkeypatch.delenv(key)
    root = Path(root).resolve()
    home = root / "home"
    home.mkdir(parents=True, exist_ok=True)
    config = root / "local.json"
    config.write_text('{"schema": "openmd.local/v1"}\n', encoding="utf-8")
    locations = {
        "HOME": home,
        "USERPROFILE": home,
        "APPDATA": home / "AppData/Roaming",
        "LOCALAPPDATA": home / "AppData/Local",
        "XDG_CONFIG_HOME": home / ".config",
        "XDG_CACHE_HOME": home / ".cache",
        "XDG_DATA_HOME": home / ".local/share",
        "OPENMD_CONFIG": config,
        "OPENMD_DATA_DIR": home / ".openmd",
        "OPENMD_RUNTIME_DIR": home / ".openmd/runtime",
        "HF_HOME": home / ".cache/huggingface",
        "HF_HUB_CACHE": home / ".cache/huggingface/hub",
        "HUGGINGFACE_HUB_CACHE": home / ".cache/huggingface/hub",
        "TRANSFORMERS_CACHE": home / ".cache/huggingface/transformers",
        "MODELSCOPE_CACHE": home / ".cache/modelscope/hub",
        "OPENCLI_CONFIG_DIR": home / ".config/opencli",
    }
    for key, value in locations.items():
        monkeypatch.setenv(key, str(value))
    monkeypatch.setenv("OPENMD_AUTO_INSTALL_TOOLS", "0")


def pytest_configure(config):
    # Guard imports and collection too; child processes inherit this environment.
    config._machine_sandbox = TemporaryDirectory(prefix="openmd-pytest-")
    config._machine_environment = pytest.MonkeyPatch()
    _isolate_machine(config._machine_environment, config._machine_sandbox.name)


def pytest_unconfigure(config):
    if hasattr(config, "_machine_environment"):
        config._machine_environment.undo()
        config._machine_sandbox.cleanup()


@pytest.fixture(autouse=True)
def isolated_machine(tmp_path_factory, monkeypatch):
    # Config tests explicitly opt in with monkeypatch.setenv and a tmp JSON file.
    # Keep state separate from output dirs whose tests assert they stay empty.
    root = tmp_path_factory.mktemp("machine")
    _isolate_machine(monkeypatch, root)
    return root
