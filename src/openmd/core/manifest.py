"""Discovery manifest writer for collection workflows."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .filename import safe_filename, short_hash
from .models import ContentItem
from .secrets import sanitize_for_logging, sanitize_url


def default_manifest_path(
    out_dir: str | Path,
    *,
    platform: str,
    collection_url: str,
) -> Path:
    """Return a stable per-collection JSONL manifest path."""
    slug = _collection_slug(collection_url) or short_hash(collection_url)
    filename = safe_filename(
        f"{platform}_{slug}-{short_hash(collection_url)}",
        fallback=f"{platform}_{short_hash(collection_url)}",
        max_length=120,
    )
    return Path(out_dir) / "manifests" / f"{filename}.jsonl"


def write_discovery_manifest(
    *,
    out_dir: str | Path,
    platform: str,
    collection_url: str,
    provider: str,
    items: list[ContentItem],
    limit: int | None = None,
) -> Path:
    """Append newly discovered items to a per-collection manifest.

    The manifest uses generic ``content_id``/``content_url`` fields while
    retaining the legacy video aliases. Existing keys are not appended again,
    so repeated smoke runs stay resumable without growing duplicate rows.
    """
    manifest_path = default_manifest_path(
        out_dir,
        platform=platform,
        collection_url=collection_url,
    )
    manifest_path.parent.mkdir(parents=True, exist_ok=True)

    existing_keys = _read_existing_manifest_keys(manifest_path)
    with manifest_path.open("a", encoding="utf-8", newline="\n") as file:
        for item in items:
            row = discovery_manifest_row(
                item,
                collection_url=collection_url,
                provider=provider,
                limit=limit,
            )
            key = _manifest_key(row)
            if key in existing_keys:
                continue
            existing_keys.add(key)
            safe_row = sanitize_for_logging(row)
            file.write(json.dumps(safe_row, ensure_ascii=False, sort_keys=True) + "\n")
    return manifest_path


def discovery_manifest_row(
    item: ContentItem,
    *,
    collection_url: str,
    provider: str,
    limit: int | None = None,
) -> dict[str, Any]:
    """Build one normalized discovery manifest row."""
    raw_meta = item.raw_meta
    return {
        "platform": item.platform,
        "collection_url": sanitize_url(collection_url),
        "playlist_id": raw_meta.get("playlist_id"),
        "channel_id": raw_meta.get("channel_id"),
        "channel_title": raw_meta.get("channel_title") or item.author,
        "content_id": item.id,
        "content_url": sanitize_url(item.url),
        "video_id": item.id,
        "video_url": sanitize_url(item.url),
        "resource_id": raw_meta.get("resource_id") or item.id,
        "resource_type": raw_meta.get("resource_type"),
        "resource_label": raw_meta.get("resource_label"),
        "resource_kind": raw_meta.get("resource_kind"),
        "container": raw_meta.get("container", False),
        "canonical_url_available": raw_meta.get(
            "canonical_url_available", bool(item.url)
        ),
        "title": item.title,
        "author": item.author,
        "published_at": item.published_at,
        "published_hint": raw_meta.get("published_hint"),
        "duration": raw_meta.get("duration"),
        "view_count": raw_meta.get("view_count") or raw_meta.get("views"),
        "caption_status": raw_meta.get("caption_status") or "unknown",
        "process_status": raw_meta.get("process_status") or "discovered",
        "discovery_only": True,
        "source_type": item.source_type,
        "media_kind": raw_meta.get("media_kind"),
        "aweme_type": raw_meta.get("aweme_type"),
        "image_count": raw_meta.get("image_count"),
        "discovery_provider": provider,
        "discovery_limit": limit,
        "source_exhausted": raw_meta.get("source_exhausted"),
        "page_count": raw_meta.get("page_count"),
        "empty_page_count": raw_meta.get("empty_page_count"),
        "duplicate_page_count": raw_meta.get("duplicate_page_count"),
        "pagination_retry_count": raw_meta.get("pagination_retry_count"),
    }


def _collection_slug(collection_url: str) -> str | None:
    parsed = urlparse(collection_url if "://" in collection_url else f"https://{collection_url}")
    path_parts = [part for part in parsed.path.split("/") if part]
    if path_parts:
        if path_parts[0].startswith("@"):
            return path_parts[0].lstrip("@")
        if len(path_parts) >= 2 and path_parts[0] in {"channel", "c", "user"}:
            return path_parts[1]
    return parsed.netloc or None


def _read_existing_manifest_keys(path: Path) -> set[str]:
    if not path.exists():
        return set()
    keys: set[str] = set()
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            keys.add(_manifest_key(row))
    return keys


def _manifest_key(row: dict[str, Any]) -> str:
    return str(
        row.get("content_id")
        or row.get("video_id")
        or row.get("content_url")
        or row.get("video_url")
        or row.get("title")
        or ""
    )
