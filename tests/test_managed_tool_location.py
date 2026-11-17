"""Managed tools can move without relocating preferences or browser sessions."""

import json

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
