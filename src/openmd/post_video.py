"""Optional video step for posts whose body is text with an attached video (X for now).

Modes (``openmd go --video``):
- ``note`` (default): probe metadata only and add one line saying the post has a video.
- ``text``: temporary audio download plus local ASR; the transcript is appended to the same Markdown.
- ``file``: like ``text`` and also keep the video file next to the Markdown (assets folder).

The video step never fails the save: problems become a short note inside the Markdown.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlparse

from openmd.runtime.tempdirs import owned_mkdtemp

VIDEO_MODES = ("note", "text", "file")
MAX_VIDEOS = 4
PROBE_TIMEOUT_S = 45
DOWNLOAD_TIMEOUT_S = 600
_X_HOSTS = {"x.com", "www.x.com", "twitter.com", "www.twitter.com", "mobile.x.com", "mobile.twitter.com"}
_X_STATUS = re.compile(r"^/[^/]+/status/[0-9]+")


def applies_to(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and parsed.netloc.lower() in _X_HOSTS and bool(_X_STATUS.match(parsed.path))


def _run(command: list[str], timeout: int) -> subprocess.CompletedProcess:
    from openmd.runtime.interpreters import run_interruptibly

    return run_interruptibly(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def probe(url: str, *, run=_run) -> list[float] | None:
    """Durations (seconds) of the post's videos; [] when it has none; None when unknown."""
    if shutil.which("yt-dlp") is None:
        return None
    try:
        done = run(["yt-dlp", "--simulate", "--no-warnings", "--playlist-items", f"1:{MAX_VIDEOS}", "--print", "%(duration)s", url], PROBE_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if done.returncode != 0:
        return [] if "no video" in done.stderr.lower() else None
    durations = []
    for line in done.stdout.splitlines():
        try:
            durations.append(float(line.strip()))
        except ValueError:
            continue
    return durations


def _clock(seconds: float) -> str:
    total = int(round(seconds))
    minutes, sec = divmod(total, 60)
    return f"{minutes} 分 {sec} 秒" if minutes else f"{sec} 秒"


def _summary(durations: list[float]) -> str:
    if len(durations) == 1:
        return f"本帖含视频（约 {_clock(durations[0])}）"
    return f"本帖含 {len(durations)} 段视频（共约 {_clock(sum(durations))}）"


def _download(url: str, workdir: Path, *, keep_video: bool, run) -> list[Path]:
    template = str(workdir / "video-%(autonumber)02d.%(ext)s")
    # Progressive mp4 only: X's HLS audio-only streams stall behind proxies (verified 2026-11-08),
    # while the single-file mp4 downloads in seconds. For text, a small rendition is enough.
    command = ["yt-dlp", "--no-warnings", "--playlist-items", f"1:{MAX_VIDEOS}", "--socket-timeout", "30", "--retries", "3",
               "-f", "b[ext=mp4][protocol^=http]/b[ext=mp4]/b", "-o", template, url]
    if not keep_video:
        command[1:1] = ["-S", "res:480"]
    done = run(command, DOWNLOAD_TIMEOUT_S)
    if done.returncode != 0:
        raise RuntimeError("视频下载失败")
    files = sorted(p for p in workdir.iterdir() if p.is_file() and p.suffix.lower() in {".mp4", ".wav", ".m4a", ".webm", ".mkv"})
    if not files:
        raise RuntimeError("视频下载后没有找到文件")
    return files


def _transcribe(path: Path, transcribe) -> str:
    from openmd.core.readable import format_transcript

    result = transcribe(path)
    if not getattr(result, "has_usable_text", False):
        raise RuntimeError("语音识别没有得到文字")
    return format_transcript(result.text, source_type="video", platform="x").strip()


def video_section(url: str, mode: str, markdown_dir: Path, *, run=_run, transcribe=None) -> str | None:
    """Markdown to append for ``url``; None when the post has no video or nothing can be said."""
    if mode not in VIDEO_MODES or not applies_to(url):
        return None
    durations = probe(url, run=run)
    if not durations:
        return None
    summary = _summary(durations)
    if mode == "note":
        return f"> {summary}，未转文字。需要时用插件右键「用 Openmd 保存（含视频文字版）」或 `openmd go --video text`。\n"
    if transcribe is None:
        from openmd.runtime.asr import get_asr_backend

        transcribe = get_asr_backend("asr.auto").transcribe
    from openmd.runtime.interpreters import remove_tree_with_retry

    workdir = Path(owned_mkdtemp(prefix="openmd-x-video-"))
    try:
        try:
            files = _download(url, workdir, keep_video=mode == "file", run=run)
        except (OSError, subprocess.TimeoutExpired, RuntimeError) as exc:
            return f"> {summary}，转文字未完成：{exc if isinstance(exc, RuntimeError) else '视频下载失败'}。\n"
        parts = []
        embeds = []
        for index, path in enumerate(files, 1):
            heading = "## 视频逐字稿" if len(files) == 1 else f"## 视频逐字稿（第 {index} 段）"
            try:
                parts.append(f"{heading}\n\n{_transcribe(path, transcribe)}\n")
            except RuntimeError as exc:
                parts.append(f"{heading}\n\n> 转文字未完成：{exc}。\n")
            if mode == "file":
                assets = markdown_dir / "assets"
                assets.mkdir(exist_ok=True)
                target = assets / path.name
                shutil.move(str(path), target)
                embeds.append(f"![视频](<assets/{target.name}>)")  # <> keeps paths with spaces valid
        body = "\n".join(parts)
        if embeds:
            body = "\n\n".join(embeds) + "\n\n" + body
        return f"> {summary}；以下逐字稿由本机语音识别生成。\n\n{body}"
    finally:
        remove_tree_with_retry(workdir)


def append_video_section(markdown_path: Path, url: str, mode: str, **kwargs) -> bool:
    section = video_section(url, mode, markdown_path.parent, **kwargs)
    if not section:
        return False
    text = markdown_path.read_text(encoding="utf-8").rstrip("\n")
    body, sep, last = text.rpartition("\n")
    if sep and last.startswith("*来源"):
        # Keep the source footer last; the video section belongs to the post body.
        updated = f"{body.rstrip()}\n\n{section.rstrip()}\n\n{last}\n"
    else:
        updated = f"{text}\n\n{section.rstrip()}\n"
    markdown_path.write_text(updated, encoding="utf-8")
    return True
