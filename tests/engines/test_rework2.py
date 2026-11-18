from pathlib import Path

from openmd.engines.tools import SofficeEngine


def test_soffice_availability_uses_headless_private_profile(monkeypatch):
    monkeypatch.setattr("openmd.engines.tools.executable", lambda tool: "soffice")
    profiles = []

    def run(argv, directory, limits):
        assert "--headless" in argv
        assert "--version" in argv
        assert "--norestore" in argv
        assert "--nologo" in argv
        profile = directory / "profile"
        assert "-env:UserInstallation=" + profile.as_uri() in argv
        profiles.append(profile)
        return "LibreOffice test-version\n"

    monkeypatch.setattr("openmd.engines.tools.run_command", run)
    for _ in range(2):
        result = SofficeEngine().available()
        assert result["ok"]
        assert result["version"] == "LibreOffice test-version"
    assert profiles[0] != profiles[1]
    assert all(not path.exists() for path in profiles)


def test_soffice_unavailable_does_not_start_a_process(monkeypatch):
    monkeypatch.setattr("openmd.engines.tools.executable", lambda tool: None)

    def unexpected(*args):
        raise AssertionError("Missing executable must not start a process")

    monkeypatch.setattr("openmd.engines.tools.run_command", unexpected)
    assert not SofficeEngine().available()["ok"]


def test_windows_soffice_uses_sibling_console_launcher(tmp_path, monkeypatch):
    import os
    import pytest
    if os.name != 'nt':
        pytest.skip('Windows LibreOffice console launcher')
    from openmd.engines.tools import executable
    gui = tmp_path / 'soffice.exe'
    console = gui.with_suffix('.com')
    gui.touch()
    console.touch()
    monkeypatch.setattr('openmd.ocr.office.soffice_path', lambda: str(gui))
    assert executable('soffice') == str(console)
