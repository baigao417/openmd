from __future__ import annotations

import subprocess
from pathlib import Path

from openmd.runtime import ffmpeg_tools


def test_ffmpeg_executable_prefers_path(monkeypatch):
    monkeypatch.setattr(ffmpeg_tools.shutil, "which", lambda name: "system-ffmpeg" if name == "ffmpeg" else None)
    monkeypatch.setattr(ffmpeg_tools, "_bundled_ffmpeg", lambda: "bundled-ffmpeg")
    assert ffmpeg_tools.ffmpeg_executable() == "system-ffmpeg"


def test_ffmpeg_executable_falls_back_to_imageio(monkeypatch):
    monkeypatch.setattr(ffmpeg_tools.shutil, "which", lambda _name: None)
    monkeypatch.setattr(ffmpeg_tools, "_bundled_ffmpeg", lambda: "bundled-ffmpeg")
    assert ffmpeg_tools.ffmpeg_executable() == "bundled-ffmpeg"


def test_ffmpeg_executable_returns_none_when_missing(monkeypatch):
    monkeypatch.setattr(ffmpeg_tools.shutil, "which", lambda _name: None)
    monkeypatch.setattr(ffmpeg_tools, "_bundled_ffmpeg", lambda: None)
    assert ffmpeg_tools.ffmpeg_executable() is None


def test_media_duration_uses_ffprobe(monkeypatch, tmp_path):
    monkeypatch.setattr(ffmpeg_tools.shutil, "which", lambda name: "system-ffprobe" if name == "ffprobe" else None)
    monkeypatch.setattr(ffmpeg_tools, "ffmpeg_executable", lambda: "ffmpeg")
    monkeypatch.setattr(ffmpeg_tools.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a[0], 0, "5.25\n", ""))
    assert ffmpeg_tools.media_duration(tmp_path / "audio.wav") == 5.25


def test_media_duration_parses_ffmpeg_duration(monkeypatch, tmp_path):
    monkeypatch.setattr(ffmpeg_tools.shutil, "which", lambda _name: None)
    monkeypatch.setattr(ffmpeg_tools, "ffmpeg_executable", lambda: "bundled-ffmpeg")
    monkeypatch.setattr(ffmpeg_tools.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a[0], 1, "", "Duration: 00:00:05.25, start: 0.0"))
    assert ffmpeg_tools.media_duration(tmp_path / "audio.wav") == 5.25


def test_media_duration_returns_none_for_unsupported_format(monkeypatch, tmp_path):
    monkeypatch.setattr(ffmpeg_tools.shutil, "which", lambda _name: None)
    monkeypatch.setattr(ffmpeg_tools, "ffmpeg_executable", lambda: "bundled-ffmpeg")
    monkeypatch.setattr(ffmpeg_tools.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a[0], 1, "", "Unknown input format"))
    assert ffmpeg_tools.media_duration(tmp_path / "audio.unknown") is None
