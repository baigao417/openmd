"""Read declared artifacts; command logs and metadata never count as source text."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from openmd.core.content_validation import validate_extraction_content
from openmd.core.models import TranscriptResult

from .runner import ToolError, ToolRun

MEDIA_EXTENSIONS = frozenset({".m4a", ".mp3", ".aac", ".opus", ".wav", ".mp4", ".webm", ".ogg", ".flac"})


def field_value(data: Any, path: str | list[str]) -> str | None:
    if isinstance(path, list):
        return next((value for key in path if (value := field_value(data, key))), None)
    current = data
    for key in path.split("."):
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return current if isinstance(current, str) and current.strip() else None


def contained_file(workdir: Path, relative: str) -> Path:
    path = (workdir / relative).resolve()
    if not path.is_relative_to(workdir.resolve()) or not path.is_file():
        raise ToolError("tool_artifact_missing")
    return path


def subtitle_text(text: str) -> str:
    lines: list[str] = []
    timestamp: str | None = None
    for line in text.splitlines():
        match = re.match(r"\s*((?:\d{2}:)?\d{2}:\d{2})[,.]\d+\s*-->", line)
        if match:
            timestamp = match.group(1)
            if timestamp.count(":") == 1:
                timestamp = "00:" + timestamp
        elif not line.strip() or line.strip().isdigit() or line.strip() == "WEBVTT":
            continue
        elif timestamp:
            value = re.sub(r"<[^>]+>", "", line).strip()
            if value:
                lines.append(f"[{timestamp}] {value}")
    return "\n".join(lines)


def read_media(workdir: Path) -> Path:
    media = [p for p in workdir.rglob("*") if p.is_file() and p.suffix.lower() in MEDIA_EXTENSIONS and p.resolve().is_relative_to(workdir.resolve()) and p.stat().st_size > 0]
    if not media:
        raise ToolError("tool_media_missing", "The tool returned no media; metadata and command logs are insufficient.")
    return max(media, key=lambda p: p.stat().st_size)


def read_text(capability: dict[str, Any], run: ToolRun, workdir: Path) -> TranscriptResult:
    reader = capability["reader"]
    title = author = published_at = None
    kind = reader["type"]
    if kind == "markdown_file":
        path = contained_file(workdir, reader["path"])
        if path.stat().st_size > 16 * 1024 * 1024:
            raise ToolError("tool_output_too_large")
        text = path.read_text(encoding="utf-8-sig")
        heading = re.search(r"^#\s+(.+)$", text, re.M)
        title = heading.group(1).strip() if heading else None
    elif kind in {"json_stdout", "json_file"}:
        if kind == "json_file":
            path = contained_file(workdir, reader["path"])
            if path.stat().st_size > 16 * 1024 * 1024:
                raise ToolError("tool_output_too_large")
            raw_json = path.read_text(encoding="utf-8-sig")
        else:
            raw_json = run.stdout
        try:
            data = json.loads(raw_json)
        except ValueError as exc:
            raise ToolError("tool_json_invalid") from exc
        if not isinstance(data, dict) or data.get("error") or data.get("success") is False:
            raise ToolError("tool_upstream_failed", "Tool JSON contains an upstream error.")
        fields = reader["fields"]
        text = field_value(data, fields["text"]) or ""
        title = field_value(data, fields.get("title", "title"))
        author = field_value(data, fields.get("author", "author"))
        published_at = field_value(data, fields.get("published_at", "published_at"))
        if not title and text and reader.get("title_from_first_line", False):
            title = text.splitlines()[0][:120]
    elif kind == "subtitle_dir":
        paths = sorted(p for p in workdir.rglob("*") if p.suffix.lower() in {".srt", ".vtt"} and p.is_file() and p.resolve().is_relative_to(workdir.resolve()))
        if not paths:
            raise ToolError("tool_subtitle_missing")
        text = subtitle_text(paths[0].read_text(encoding="utf-8-sig"))
    else:
        raise ToolError("tool_reader_invalid")
    text = text.strip()
    if len(text) < capability.get("validation", {}).get("min_chars", 1):
        raise ToolError("tool_content_empty", "Tool returned insufficient source text.")
    if len(text) < 3000 and re.search(r"(?im)^\s*(?:#\s*)?(?:just a moment[.!…]*|verify you are human|请完成验证|请先登录|访问过于频繁|安全验证)\s*$", text):
        raise ToolError("tool_challenge_page", "A challenge/login page is not source content.")
    if re.match(r"\s*(?:<!doctype html|<html)", text, re.I):
        raise ToolError("tool_html_instead_of_markdown")
    result = validate_extraction_content(TranscriptResult(text=text + "\n", status="verified", source_level=capability["source_level"], backend=capability["id"], title=title, author=author, published_at=published_at, source_notes=["Original source text read from the tool's declared artifact.", *([capability["coverage_note"]] if capability.get("coverage_note") else [])]))
    return result
