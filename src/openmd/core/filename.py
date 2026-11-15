"""Windows-safe title filename helpers."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path


INVALID_FILENAME_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
WHITESPACE_RE = re.compile(r"\s+")


def short_hash(value: str | None, *, length: int = 8) -> str:
    seed = value or "openmd"
    return hashlib.sha1(seed.encode("utf-8", errors="ignore")).hexdigest()[:length]


def safe_filename(value: str | None, *, fallback: str = "untitled", max_length: int = 120) -> str:
    text = value or fallback
    text = INVALID_FILENAME_CHARS_RE.sub(" ", text)
    text = WHITESPACE_RE.sub(" ", text).strip(" .")
    if not text:
        text = fallback
    if len(text) > max_length:
        text = text[:max_length].rstrip(" .")
    return text or "untitled"


def filename_base_for_item(
    title: str | None,
    *,
    item_id: str | None = None,
    url: str | None = None,
    max_length: int = 120,
) -> str:
    fallback = item_id or short_hash(url)
    return safe_filename(title, fallback=fallback, max_length=max_length)


def unique_markdown_path(
    directory: Path,
    filename_base: str,
    *,
    collision_key: str | None = None,
    overwrite: bool = False,
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    base = safe_filename(filename_base)
    candidate = directory / f"{base}.md"
    if overwrite or not candidate.exists():
        return candidate

    suffix = short_hash(collision_key or base)
    candidate = directory / f"{base}-{suffix}.md"
    if not candidate.exists():
        return candidate

    counter = 2
    while True:
        candidate = directory / f"{base}-{suffix}-{counter}.md"
        if not candidate.exists():
            return candidate
        counter += 1
