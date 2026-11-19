from __future__ import annotations

import subprocess
import sys
from types import SimpleNamespace

import pytest

from openmd import media_tools


def test_managed_python_module_is_preferred_to_path(monkeypatch):
    monkeypatch.setattr(media_tools.importlib.util, "find_spec", lambda _name: object())
    monkeypatch.setattr(media_tools.shutil, "which", lambda _name: "C:/tools/yt-dlp.exe")

    assert media_tools.yt_dlp_command() == [sys.executable, "-m", "yt_dlp"]


def test_path_executable_is_fallback_when_managed_module_is_missing(monkeypatch):
    monkeypatch.setattr(media_tools.importlib.util, "find_spec", lambda _name: None)
    monkeypatch.setattr(media_tools.shutil, "which", lambda _name: "C:/tools/yt-dlp.exe")

    assert media_tools.yt_dlp_command() == ["C:/tools/yt-dlp.exe"]


def test_missing_yt_dlp_has_no_command(monkeypatch):
    monkeypatch.setattr(media_tools.importlib.util, "find_spec", lambda _name: None)
    monkeypatch.setattr(media_tools.shutil, "which", lambda _name: None)

    assert media_tools.yt_dlp_command() is None


@pytest.mark.parametrize(
    ("stderr", "expected"),
    [
        ("Account authentication is required", "login_required"),
        ("Unsupported URL: https://example.test/watch", "unsupported_url"),
        ("HTTP Error 403: Forbidden", "forbidden_or_outdated"),
        ("Video unavailable in your country or private video", "geo_or_private"),
        ("Unable to download webpage: connection timed out", "network"),
        ("Extractor failed for an unknown reason", "other"),
    ],
)
def test_yt_dlp_error_classes_include_next_action(stderr, expected):
    result = media_tools.classify_yt_dlp_error(stderr)

    assert result["category"] == expected
    assert result["next_action"]


def test_doctor_reports_managed_version_age_and_stale_warning(monkeypatch):
    monkeypatch.setattr(media_tools.importlib.util, "find_spec", lambda _name: object())

    def run(command, **_kwargs):
        assert command == [sys.executable, "-m", "yt_dlp", "--version"]
        return subprocess.CompletedProcess(command, 0, "2026.01.01\n", "")

    report = media_tools.yt_dlp_diagnostics(run=run, today=media_tools.date(2026, 4, 2))

    assert report["source"] == "managed"
    assert report["version"] == "2026.01.01"
    assert report["age_days"] == 91
    assert report["warning"] is True
    assert "升级" in report["next_action"]


def test_doctor_reports_missing_without_running_command(monkeypatch):
    monkeypatch.setattr(media_tools.importlib.util, "find_spec", lambda _name: None)
    monkeypatch.setattr(media_tools.shutil, "which", lambda _name: None)

    report = media_tools.yt_dlp_diagnostics(run=lambda *_a, **_kw: pytest.fail("missing tool must not run"))

    assert report["source"] == "missing"
    assert report["version"] is None
    assert report["age_days"] is None
    assert report["warning"] is True


def test_media_doctor_includes_yt_dlp_version_report(monkeypatch):
    import openmd.core.doctor as doctor
    import openmd.runtime.interpreters as interpreters
    import openmd.runtime.models as models
    import openmd.runtime.prepared_models as prepared

    runtime = SimpleNamespace(probe={"ready": False}, reason="ASR unavailable", as_dict=lambda: {"probe": {"ready": False}})
    monkeypatch.setattr(interpreters, "select_asr_runtime", lambda: runtime)
    monkeypatch.setattr(models, "configured_models", lambda: {})
    monkeypatch.setattr(prepared, "preparation_status", lambda *_args: {})
    monkeypatch.setattr(doctor.shutil, "which", lambda name: "fixture" if name == "ffmpeg" else None)
    monkeypatch.setattr(media_tools, "yt_dlp_diagnostics", lambda: {
        "source": "managed", "version": "2026.10.26",
        "age_days": 24, "warning": False, "next_action": "yt-dlp 版本可用。",
    })

    report = doctor._media_diagnostics()

    assert report["yt_dlp"]["source"] == "managed"
    assert report["yt_dlp"]["version"] == "2026.10.26"


def test_doctor_text_shows_stale_yt_dlp_upgrade_hint():
    from openmd.doctor_view import render

    view = render({"profile_diagnostics": {"yt_dlp": {
        "source": "path", "version": "2026.01.01", "age_days": 100,
        "warning": True, "next_action": "运行 openmd doctor 查看并更新 Openmd 管理的 yt-dlp。",
    }}})

    assert "yt-dlp: PATH；版本 2026.01.01；发布 100 天" in view
    assert "更新 Openmd 管理的 yt-dlp" in view
