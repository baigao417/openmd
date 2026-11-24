"""Managed tools can move without relocating preferences or browser sessions."""

import json
from pathlib import Path

import pytest

from openmd.runtime import managed_tools


def test_managed_root_uses_local_tool_location(tmp_path, monkeypatch):
    location = tmp_path / "separate runtime" / "opencli"
    monkeypatch.setattr(managed_tools, "load_local_config", lambda: {"tools": {"opencli": {"root": str(location)}}}, raising=False)
    assert managed_tools.managed_root() == location.resolve()
    assert managed_tools.managed_package_root() == location.resolve() / "node_modules" / "@jackwener" / "opencli"
    assert not location.exists()  # Resolution is read only.


def test_managed_root_preserves_default_data_location(tmp_path, monkeypatch):
    monkeypatch.setattr(managed_tools, "load_local_config", lambda: {}, raising=False)
    monkeypatch.setattr(managed_tools, "data_directory", lambda: tmp_path)
    assert managed_tools.managed_root() == tmp_path / "tools" / "opencli"


def test_defuddle_first_use_installs_pinned_package_to_managed_tools(tmp_path, monkeypatch):
    import subprocess

    node = tmp_path / "node"
    node.mkdir()
    (node / "node.exe").touch()
    npm = node / "npm-cli.js"
    npm.touch()
    monkeypatch.setattr(managed_tools, "node_dir", lambda: node)
    monkeypatch.setattr(managed_tools, "_npm_cli", lambda _node: npm)
    monkeypatch.setattr(managed_tools, "data_directory", lambda: tmp_path / "data")
    monkeypatch.setenv("OPENMD_AUTO_INSTALL_TOOLS", "1")
    monkeypatch.setenv("OPENMD_NPM_REGISTRY", "https://registry.example.invalid")
    calls = []

    def install(command, **kwargs):
        calls.append(command)
        prefix = Path(command[command.index("--prefix") + 1])
        package = prefix / "node_modules" / "defuddle"
        package.mkdir(parents=True)
        (package / "package.json").write_text(json.dumps({"version": "0.19.6"}), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(managed_tools, "run_interruptibly", install)
    assert managed_tools.ensure_defuddle(lambda _message: None) == "managed"
    assert calls[0][calls[0].index("--registry") + 1] == "https://registry.example.invalid"
    assert "defuddle@0.19.6" in calls[0]
    assert (tmp_path / "data" / "tools" / "defuddle" / "node_modules" / "defuddle" / "package.json").is_file()


def test_defuddle_auto_install_disabled_and_failure_fall_back(tmp_path, monkeypatch):
    import subprocess

    monkeypatch.setattr(managed_tools, "data_directory", lambda: tmp_path)
    monkeypatch.setenv("OPENMD_AUTO_INSTALL_TOOLS", "0")
    monkeypatch.setattr(managed_tools, "run_interruptibly", lambda *_a, **_k: pytest.fail("install must be disabled"))
    assert managed_tools.ensure_defuddle(lambda _message: None) == "missing"
    node = tmp_path / "node"
    node.mkdir()
    (node / "node.exe").touch()
    monkeypatch.setattr(managed_tools, "node_dir", lambda: node)
    monkeypatch.setattr(managed_tools, "_npm_cli", lambda _node: tmp_path / "npm-cli.js")
    monkeypatch.setenv("OPENMD_AUTO_INSTALL_TOOLS", "1")
    monkeypatch.setattr(managed_tools, "run_interruptibly", lambda command, **_kwargs: subprocess.CompletedProcess(command, 1, "", "network failure"))
    assert managed_tools.ensure_defuddle(lambda _message: None) == "missing"


@pytest.mark.parametrize("setting", [None, "folder", {"root": ""}, {"root": 42}, {"other": "folder"}])
def test_managed_root_rejects_invalid_explicit_location(setting, monkeypatch):
    monkeypatch.setattr(managed_tools, "load_local_config", lambda: {"tools": {"opencli": setting}}, raising=False)
    with pytest.raises(ValueError, match="managed_opencli_location_invalid"):
        managed_tools.managed_root()


def test_doctor_reports_invalid_managed_location(tmp_path, monkeypatch, capsys):
    from openmd.cli import main

    config = tmp_path / "positions.json"
    config.write_text(json.dumps({"schema": "openmd.local/v1", "tools": {"opencli": {"root": ""}}}), encoding="utf-8")
    monkeypatch.setenv("OPENMD_CONFIG", str(config))
    assert main(["doctor", "--profile", "core", "--json"]) == 1
    output = capsys.readouterr()
    result = json.loads(output.out)
    assert result["ok"] is False
    assert result["profile_diagnostics"]["error_code"] == "local_configuration_invalid"
    assert "Traceback" not in output.err


def _install_fake_managed_defuddle(tmp_path, monkeypatch):
    """Lay out the real npm prefix shape: <root>/node_modules/defuddle/dist."""
    import os

    data = tmp_path / "data"
    package = data / "tools" / "defuddle" / "node_modules" / "defuddle"
    (package / "dist").mkdir(parents=True)
    (package / "dist" / "cli.js").touch()
    (package / "dist" / "node.js").touch()
    (package / "package.json").write_text(json.dumps({"version": managed_tools.DEFUDDLE_VERSION}), encoding="utf-8")
    folder = tmp_path / "node" if os.name == "nt" else tmp_path / "node" / "bin"
    folder.mkdir(parents=True)
    (folder / ("node.exe" if os.name == "nt" else "node")).touch()
    monkeypatch.setattr(managed_tools, "data_directory", lambda: data)
    monkeypatch.setattr(managed_tools, "node_dir", lambda: folder)
    monkeypatch.delenv("OPENMD_TOOL_DEFUDDLE_ROOT", raising=False)
    monkeypatch.delenv("OPENMD_TOOL_DEFUDDLE_NODE", raising=False)
    return package.parent


def test_snapshot_uses_managed_defuddle_without_local_config(tmp_path, monkeypatch):
    """Browser-host and fresh installs have no local config; snapshots must still use Defuddle."""
    import openmd.backends.snapshot as snapshot_backend

    modules = _install_fake_managed_defuddle(tmp_path, monkeypatch)
    monkeypatch.setattr(snapshot_backend, "load_local_config", lambda: {})

    spec = snapshot_backend._managed_defuddle_spec()

    assert spec is not None
    _node, root, module, _options = spec
    assert Path(root) == modules
    assert (Path(root) / module).is_file()


def test_url_route_uses_managed_defuddle_without_local_config(tmp_path, monkeypatch):
    import openmd.routing.runner as runner

    modules = _install_fake_managed_defuddle(tmp_path, monkeypatch)
    monkeypatch.setattr(runner, "load_local_config", lambda: {})
    card = json.loads((Path(runner.__file__).parent / "cards" / "defuddle.json").read_text(encoding="utf-8"))

    launcher = runner.resolve_launcher(card)

    assert launcher.origin == "managed"
    assert launcher.entry_path == (modules / "defuddle" / "dist" / "cli.js").resolve()
