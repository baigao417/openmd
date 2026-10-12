"""Markdown rendering for normalized Openmd extraction results."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

from .models import ContentItem, MarkdownDocument, TranscriptResult
from .secrets import sanitize_error_summary, sanitize_url_text


FRONTMATTER_FIELDS: tuple[str, ...] = (
    "platform",
    "url",
    "title",
    "author",
    "published_at",
    "captured_at",
    "source_type",
    "status",
    "source_level",
    "backend",
)

_PLAIN_SCALAR_RE = re.compile(r"^[A-Za-z0-9_.:/@+\- ]+$")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _frontmatter_value(value: Any) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value).replace("\r", " ").replace("\n", " ")
    if _PLAIN_SCALAR_RE.match(text) and text.strip() == text:
        return text
    return json.dumps(text, ensure_ascii=False)


def build_source_notes(result: TranscriptResult) -> list[str]:
    notes = [f"backend: {result.backend}", f"source_level: {result.source_level}"]
    for note in result.source_notes:
        if note:
            notes.append(f"notes: {note}")
    if result.error_code:
        notes.append(f"error_code: {result.error_code}")
    if result.error_summary:
        summary = sanitize_error_summary(result.error_summary)
        if summary:
            notes.append(f"error_summary: {summary}")
    return notes


def render_markdown_document(
    item: ContentItem,
    result: TranscriptResult,
    *,
    captured_at: str | None = None,
) -> MarkdownDocument:
    title = result.title or item.title or item.id or "untitled"
    frontmatter = {
        "platform": item.platform,
        "url": sanitize_url_text(item.url),
        "title": title,
        "author": result.author or item.author,
        "published_at": result.published_at or item.published_at,
        "captured_at": captured_at or utc_now_iso(),
        "source_type": item.source_type,
        "status": result.status,
        "source_level": result.source_level,
        "backend": result.backend,
    }
    return MarkdownDocument(
        frontmatter=frontmatter,
        title=title,
        content=result.text,
        source_notes=build_source_notes(result),
    )


def render_document(document: MarkdownDocument) -> str:
    lines = ["---"]
    for field in FRONTMATTER_FIELDS:
        lines.append(f"{field}: {_frontmatter_value(document.frontmatter.get(field))}")
    lines.extend(
        [
            "---",
            "",
            f"# {document.title}",
            "",
            "## Transcript / Content",
            "",
            document.content,
            "",
            "## Source Notes",
            "",
        ]
    )
    if document.source_notes:
        lines.extend(f"- {note}" for note in document.source_notes)
    else:
        lines.append("- notes: none")
    return "\n".join(lines).rstrip() + "\n"
