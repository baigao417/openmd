"""Offline tests use temporary machine state, including during collection."""

import os
from importlib import metadata
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
    _isolate_plugins(config._machine_environment)


def _isolate_plugins(monkeypatch, entries=()):
    """Hide external Openmd plugins, including during collection.

    Contract tests explicitly supply synthetic entries via openmd_plugin_entries.
    Other libraries' entry points (including pytest's) remain discoverable.
    """
    original = metadata.entry_points

    def entry_points(**params):
        group = params.get("group")
        if group is not None and group.startswith("openmd."):
            return [entry for entry in entries
                    if getattr(entry, "group", "openmd.platforms") == group
                    and ("name" not in params or entry.name == params["name"])]
        discovered = original(**params)
        if group is not None:
            return discovered
        if isinstance(discovered, dict):  # Python 3.10 SelectableGroups
            return type(discovered)({key: value for key, value in discovered.items()
                                     if not key.startswith("openmd.")})
        return metadata.EntryPoints(entry for entry in discovered
                                    if not entry.group.startswith("openmd."))

    monkeypatch.setattr(metadata, "entry_points", entry_points)


@pytest.fixture
def openmd_plugin_entries(monkeypatch):
    """Explicit opt-in: load only synthetic entries owned by this test."""
    from openmd.core.plugins import load_plugins
    from openmd.routing.loader import load_catalog

    class SyntheticEntries(list):
        def append(self, entry):
            super().append(entry)
            load_plugins.cache_clear()
            load_catalog.cache_clear()

        def extend(self, entries):
            super().extend(entries)
            load_plugins.cache_clear()
            load_catalog.cache_clear()

    entries = SyntheticEntries()
    _isolate_plugins(monkeypatch, entries)
    load_plugins.cache_clear()
    load_catalog.cache_clear()
    yield entries
    load_plugins.cache_clear()
    load_catalog.cache_clear()


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


@pytest.fixture(autouse=True)
def isolated_browser_registration(monkeypatch):
    # The Windows registry is independent of HOME/OPENMD_DATA_DIR. Host tests
    # explicitly supply temporary registrations; ordinary tests must not probe
    # the developer's interpreter or extension copy.
    from openmd import browser_diagnostics

    monkeypatch.setattr(browser_diagnostics, "registered_manifests", lambda: {})
