"""Shared discovery and duration probing for FFmpeg tools."""
from __future__ import annotations

import math
import re
import shutil
import subprocess
from pathlib import Path


def _bundled_ffmpeg() -> str | None:
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except (ImportError, OSError, RuntimeError):
        return None


def ffmpeg_executable() -> str | None:
    """Prefer a system FFmpeg, then the executable bundled by imageio-ffmpeg."""
    return shutil.which("ffmpeg") or _bundled_ffmpeg()


def ffmpeg_source() -> str:
    if shutil.which("ffmpeg"):
        return "system"
    return "bundled" if _bundled_ffmpeg() else "missing"


def ffmpeg_location_args() -> list[str]:
    executable = ffmpeg_executable()
    return ["--ffmpeg-location", str(Path(executable).parent)] if executable else []


def media_duration(path: str | Path) -> float | None:
    """Read duration using system ffprobe, falling back to FFmpeg's banner."""
    path = str(path)
    ffprobe = shutil.which("ffprobe")
    try:
        if ffprobe:
            done = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", path], capture_output=True,
                text=True, timeout=30, check=False)
            if done.returncode == 0:
                value = float(done.stdout.strip())
                if math.isfinite(value) and value >= 0:
                    return value
        executable = ffmpeg_executable()
        if not executable:
            return None
        done = subprocess.run([executable, "-hide_banner", "-i", path], capture_output=True,
            text=True, timeout=30, check=False)
        match = re.search(r"Duration:\s*(\d{2}):(\d{2}):(\d{2}(?:\.\d+)?)", done.stderr or "")
        if not match:
            return None
        hours, minutes, seconds = match.groups()
        value = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
        return value if math.isfinite(value) else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
