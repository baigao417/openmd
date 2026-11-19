"""Shared, read-only discovery and diagnostics for optional media tools."""

from __future__ import annotations

import importlib.util
import re
import shutil
import subprocess
import sys
from datetime import date, datetime
from typing import Any, Callable


VersionRunner = Callable[..., subprocess.CompletedProcess]


def yt_dlp_command() -> list[str] | None:
    """Return the managed module command first, then a PATH executable."""
    try:
        managed = importlib.util.find_spec("yt_dlp") is not None
    except (ImportError, ValueError):
        managed = False
    if managed:
        return [sys.executable, "-m", "yt_dlp"]
    executable = shutil.which("yt-dlp")
    return [executable] if executable else None


_ERRORS = {
    "login_required": (
        ("account authentication is required", "authentication is required", "login required", "sign in to"),
        "该站点要求登录才能下载视频",
        "可在浏览器中手动下载；Openmd 不读取浏览器 cookie。",
    ),
    "unsupported_url": (
        ("unsupported url", "no suitable extractor", "not a valid url"),
        "yt-dlp 暂不支持此页面格式",
        "确认链接指向公开的视频页面；也可在浏览器中手动下载。",
    ),
    "forbidden_or_outdated": (
        ("http error 403", "403 forbidden", "forbidden"),
        "下载站点返回 403，yt-dlp 可能已过期",
        "yt-dlp 可能已过期：在 Openmd 所在环境运行 `python -m pip install -U yt-dlp`（或重装 `openmd-cli[media]`）后重试；`openmd doctor` 可查看当前版本。",
    ),
    "geo_or_private": (
        ("private video", "video is private", "not available in your country", "geo-restricted", "geographic restriction"),
        "视频可能存在地区限制或已设为私密",
        "确认视频对当前地区公开可见；Openmd 不绕过地区或隐私限制。",
    ),
    "network": (
        ("timed out", "connection reset", "temporary failure", "unable to download webpage", "network is unreachable"),
        "网络连接暂时失败",
        "检查网络后重试。",
    ),
}


def classify_yt_dlp_error(stderr: str) -> dict[str, str]:
    """Classify diagnostic text without returning raw stderr to saved content."""
    text = (stderr or "").casefold()
    for category, (needles, message, action) in _ERRORS.items():
        if any(needle in text for needle in needles):
            return {"category": category, "message": message, "next_action": action}
    return {
        "category": "other",
        "message": "yt-dlp 未能完成视频处理",
        "next_action": "运行 `openmd doctor` 查看 yt-dlp 状态，并确认视频可公开访问。",
    }


def _release_date(version: str) -> date | None:
    match = re.search(r"(?<!\d)(20\d{2})[._-](\d{1,2})[._-](\d{1,2})(?!\d)", version)
    if not match:
        return None
    try:
        return date(*(int(part) for part in match.groups()))
    except ValueError:
        return None


def yt_dlp_diagnostics(
    *,
    run: VersionRunner = subprocess.run,
    today: date | None = None,
) -> dict[str, Any]:
    """Report the selected yt-dlp source/version without network access."""
    command = yt_dlp_command()
    if command is None:
        return {
            "source": "missing",
            "version": None,
            "age_days": None,
            "warning": True,
            "next_action": '安装媒体 extra：`pip install "openmd-cli[media]"`。',
        }
    source = "managed" if len(command) > 1 and command[1:3] == ["-m", "yt_dlp"] else "path"
    try:
        done = run(command + ["--version"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10, check=False)
        version = done.stdout.strip().splitlines()[0] if done.returncode == 0 and done.stdout.strip() else None
    except (OSError, subprocess.TimeoutExpired):
        version = None
    released = _release_date(version or "")
    age_days = ((today or datetime.now().date()) - released).days if released else None
    warning = version is None or (age_days is not None and age_days > 90)
    if version is None:
        next_action = "无法读取版本；运行 `openmd doctor` 后检查 yt-dlp 安装。"
    elif age_days is not None and age_days > 90:
        next_action = 'yt-dlp 已超过 90 天，YouTube 等站点可能拒绝下载：在 Openmd 所在环境运行 `python -m pip install -U yt-dlp`（或重装 `openmd-cli[media]`）。'
    else:
        next_action = "yt-dlp 版本可用。"
    return {
        "source": source,
        "version": version,
        "age_days": age_days,
        "warning": warning,
        "next_action": next_action,
    }


class YtDlpFailure(RuntimeError):
    def __init__(self, stderr: str):
        self.diagnostic = classify_yt_dlp_error(stderr)
        super().__init__(self.diagnostic["message"])
