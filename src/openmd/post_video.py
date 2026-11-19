"""Optional media step for posts whose body is text with attached media.

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
from openmd.media_tools import YtDlpFailure, classify_yt_dlp_error, yt_dlp_command

VIDEO_MODES = ("note", "text", "file")
MAX_VIDEOS = 4
PROBE_TIMEOUT_S = 45
DOWNLOAD_TIMEOUT_S = 600
_VIDEO_TEXT_SKIP = {"xiaohongshu"}
_X_HOSTS = {"x.com", "www.x.com", "twitter.com", "www.twitter.com", "mobile.x.com", "mobile.twitter.com"}
_X_STATUS = re.compile(r"^/[^/]+/status/[0-9]+")


def _transcript_route_sources() -> frozenset[str]:
    """Derive native transcript routes from the canonical routing declarations."""
    try:
        from openmd.routing.loader import load_catalog

        sources = {
            route["source"]
            for route in load_catalog(include_plugins=False).routes
            if route.get("capability") == "transcript"
            or (
                route.get("capability") == "content"
                and any(str(backend).endswith(".transcript") for backend in route.get("chain", ()))
            )
        }
        # XHS native note extraction owns its video component, but the X group
        # explicitly asks this shared post-media step to handle its video notes.
        sources.difference_update(_VIDEO_TEXT_SKIP)
        return frozenset(sources)
    except (ImportError, KeyError, TypeError, ValueError):
        return frozenset()


def applies_to(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def should_process(url: str, mode: str = "note") -> bool:
    """Return whether the shared post-media step should inspect this URL."""
    if not applies_to(url):
        return False
    parsed = urlparse(url)
    if mode == "note":
        return parsed.netloc.lower() in _X_HOSTS and bool(_X_STATUS.match(parsed.path))
    if mode in {"text", "note"}:
        try:
            from openmd.core.detector import detect_platform

            if detect_platform(url) in _transcript_route_sources():
                return False
        except (ImportError, ValueError, TypeError):
            pass
    return True


def native_transcript_source(url: str) -> str | None:
    """Return the detected platform when the routing catalog owns transcript text."""
    try:
        from openmd.core.detector import detect_platform

        source = detect_platform(url)
    except (ImportError, ValueError, TypeError):
        return None
    return source if source in _transcript_route_sources() else None


def native_media_source(url: str) -> str | None:
    """Return the platform with a declared native media retention route."""
    try:
        from openmd.core.detector import detect_platform
        from openmd.routing.loader import load_catalog

        source = detect_platform(url)
        media_sources = {
            route["source"]
            for route in load_catalog(include_plugins=False).routes
            if route.get("capability") == "media"
        }
    except (ImportError, KeyError, TypeError, ValueError):
        return None
    return source if source in media_sources else None


def _run(command: list[str], timeout: int) -> subprocess.CompletedProcess:
    from openmd.runtime.interpreters import run_interruptibly

    return run_interruptibly(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _probe_result(url: str, *, run=_run) -> tuple[list[float] | None, dict[str, str] | None]:
    """Return duration results and a sanitized diagnostic when probing fails."""
    command = yt_dlp_command()
    if command is None:
        return None, {"category": "missing", "message": "未安装 yt-dlp", "next_action": '安装媒体 extra：`pip install "openmd-cli[media]"`。'}
    try:
        done = run(command + ["--simulate", "--no-warnings", "--playlist-items", f"1:{MAX_VIDEOS}", "--print", "%(duration)s", url], PROBE_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired):
        return None, {"category": "network", "message": "视频探测未完成", "next_action": "检查网络后重试。"}
    if done.returncode != 0:
        if "no video" in done.stderr.lower():
            return [], None
        return None, classify_yt_dlp_error(done.stderr or done.stdout)
    durations = []
    for line in done.stdout.splitlines():
        try:
            durations.append(float(line.strip()))
        except ValueError:
            continue
    return durations, None


def probe(url: str, *, run=_run) -> list[float] | None:
    """Durations (seconds) of the post's videos; [] when it has none; None when unknown."""
    return _probe_result(url, run=run)[0]


def _clock(seconds: float) -> str:
    total = int(round(seconds))
    minutes, sec = divmod(total, 60)
    return f"{minutes} 分 {sec} 秒" if minutes else f"{sec} 秒"


def _summary(durations: list[float]) -> str:
    if len(durations) == 1:
        return f"本帖含视频（约 {_clock(durations[0])}）"
    return f"本帖含 {len(durations)} 段视频（共约 {_clock(sum(durations))}）"


def _download(url: str, workdir: Path, *, keep_video: bool, run) -> list[Path]:
    command_prefix = yt_dlp_command()
    if command_prefix is None:
        raise RuntimeError("未安装 yt-dlp")
    template = str(workdir / "video-%(autonumber)02d.%(ext)s")
    # Progressive mp4 only: X's HLS audio-only streams stall behind proxies (verified 2026-11-08),
    # while the single-file mp4 downloads in seconds. For text, a small rendition is enough.
    command = command_prefix + ["--no-warnings", "--playlist-items", f"1:{MAX_VIDEOS}", "--socket-timeout", "30", "--retries", "3",
               "-f", "b[ext=mp4][protocol^=http]/b[ext=mp4]/b", "-o", template, url]
    if not keep_video:
        command[1:1] = ["-S", "res:480"]
    done = run(command, DOWNLOAD_TIMEOUT_S)
    if done.returncode != 0:
        raise YtDlpFailure(done.stderr or done.stdout)
    files = sorted(p for p in workdir.iterdir() if p.is_file() and p.suffix.lower() in {".mp4", ".wav", ".m4a", ".webm", ".mkv"})
    if not files:
        raise RuntimeError("视频下载后没有找到文件")
    return files


def _transcribe(path: Path, transcribe) -> str:
    from openmd.core.readable import format_transcript

    result = transcribe(path)
    if not getattr(result, "has_usable_text", False):
        raise RuntimeError("语音识别没有得到文字")
    return format_transcript(result.text, source_type="video", platform="generic_media").strip()


def _size_label(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{size} B"


def video_section(url: str, mode: str, markdown_dir: Path, *, run=_run, transcribe=None, transcribe_media: bool = True) -> str | None:
    """Markdown to append for ``url``; None when the post has no video or nothing can be said."""
    if mode not in VIDEO_MODES or not should_process(url, mode):
        return None
    durations, probe_error = _probe_result(url, run=run)
    if mode == "note" and durations in ([], None):
        return None
    if durations == []:
        return "> 未发现可下载的视频。\n"
    if durations is None:
        detail = probe_error or {"message": "视频处理未完成", "next_action": "确认视频可公开访问后重试。"}
        category = f" [{detail['category']}]" if detail.get("category") else ""
        return f"> 视频处理未完成{category}：{detail['message']}。下一步：{detail['next_action']}\n"
    summary = _summary(durations)
    if mode == "note":
        return f"> {summary}，未转文字。需要时用插件右键「用 Openmd 保存（含视频文字版）」或 `openmd go --video text`。\n"
    if transcribe_media and transcribe is None:
        from openmd.runtime.asr import get_asr_backend

        transcribe = get_asr_backend("asr.auto").transcribe
    from openmd.runtime.interpreters import remove_tree_with_retry

    workdir = Path(owned_mkdtemp(prefix="openmd-x-video-"))
    try:
        try:
            files = _download(url, workdir, keep_video=mode == "file", run=run)
        except (OSError, subprocess.TimeoutExpired, RuntimeError) as exc:
            diagnostic = exc.diagnostic if isinstance(exc, YtDlpFailure) else None
            reason = f"视频下载失败 [{diagnostic['category']}]：{diagnostic['message']}" if diagnostic else (exc if isinstance(exc, RuntimeError) else "视频下载失败")
            action = f"下一步：{diagnostic['next_action']}" if diagnostic else "下一步：确认页面无需登录、付费或 DRM，并检查 yt-dlp 是否支持当前媒体格式后重试。"
            if not transcribe_media:
                return f"> {summary}，媒体文件保存未完成：{reason}。{action}\n"
            return f"> {summary}，转文字未完成：{reason}。{action}\n"
        parts = []
        embeds = []
        for index, path in enumerate(files, 1):
            if transcribe_media:
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
                embeds.append(
                    f"![视频/音频](<assets/{target.name}>)（时长：{_clock(durations[min(index - 1, len(durations) - 1)])}；大小：{_size_label(target.stat().st_size)}）"
                )  # <> keeps paths with spaces valid
        body = "\n".join(parts)
        if embeds:
            body = "\n\n".join(embeds) + "\n\n" + body
        if transcribe_media:
            return f"> {summary}；以下逐字稿由本机语音识别生成。\n\n{body}"
        return f"> {summary}；已保存视频/音频文件，本站点逐字稿路线不会重复转写。\n\n{body}"
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


def _append_markdown_section(markdown_path: Path, section: str) -> None:
    text = markdown_path.read_text(encoding="utf-8").rstrip("\n")
    body, sep, last = text.rpartition("\n")
    if sep and last.startswith("*来源"):
        updated = f"{body.rstrip()}\n\n{section.rstrip()}\n\n{last}\n"
    else:
        updated = f"{text}\n\n{section.rstrip()}\n"
    markdown_path.write_text(updated, encoding="utf-8")


def append_retained_media_section(markdown_path: Path, media_dir: Path) -> bool:
    """Publish media already retained by a native route into Markdown assets."""
    if not media_dir.is_dir():
        return False
    files = sorted(path for path in media_dir.iterdir() if path.is_file())
    if not files:
        return False
    assets = markdown_path.parent / "assets"
    assets.mkdir(exist_ok=True)
    embeds: list[str] = []
    for index, path in enumerate(files, 1):
        target = assets / path.name
        if target.exists():
            target = assets / f"{path.stem}-{index}{path.suffix}"
        shutil.move(str(path), target)
        embeds.append(
            f"![视频/音频](<assets/{target.name}>)（时长：未提供；大小：{_size_label(target.stat().st_size)}）"
        )
    _append_markdown_section(
        markdown_path,
        "> 本帖视频/音频文件已由本站点下载路线保留。\n\n" + "\n\n".join(embeds),
    )
    return True


def append_media_retention_note(markdown_path: Path, reason: str) -> bool:
    """Record why a requested native media copy was not retained."""
    _append_markdown_section(markdown_path, f"> 视频/音频文件未保存：{reason}。")
    return True
