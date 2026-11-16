"""Core-owned transient media to audio conversion. No locators in failures."""
from pathlib import Path
import shutil
import subprocess
from urllib.parse import urlsplit

from .interpreters import run_interruptibly


def download_audio(url: str, target: Path, *, referer: str, timeout: int = 600) -> None:
    parsed = urlsplit(url)
    if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("invalid_media_locator")
    executable = shutil.which("ffmpeg")
    if not executable:
        raise RuntimeError("ffmpeg_missing")
    # The URL is transient argv only; ffmpeg's diagnostic stream may echo signed
    # locators, so never propagate it to a result, exception, log or artifact.
    command = [executable, "-nostdin", "-y", "-hide_banner", "-loglevel", "error",
               "-protocol_whitelist", "http,https,tcp,tls,crypto,httpproxy", "-rw_timeout", "30000000", "-headers", f"User-Agent: Mozilla/5.0\r\nReferer: {referer}\r\n",
               "-i", url, "-vn", "-ac", "1", "-ar", "16000", "-b:a", "48k", str(target)]
    try:
        result = run_interruptibly(command, capture_output=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        raise RuntimeError("media_download_failed") from None
    if result.returncode or not target.is_file() or target.stat().st_size == 0:
        raise RuntimeError("media_download_failed")
