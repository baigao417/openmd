"""Optional public video metadata, separate from transcript acquisition.

Only the explicit public-network gate permits a request. Browser permission
alone never enables these requests, and metadata failure cannot lose text.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from openmd.core.filename import short_hash
from openmd.core.models import ContentItem, TranscriptResult

from .youtube_transcript import extract_youtube_video_id


MetadataFetcher = Callable[[str, int], Any]
_YOUTUBE_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_BILIBILI_ID = re.compile(r"BV[A-Za-z0-9]{10}")
_PLACEHOLDER_TITLES = frozenset({"untitled", "unknown", "video", "audio"})
_MAX_RESPONSE_BYTES = 512 * 1024


def _value(value: object, *, limit: int = 512) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.split()).strip()
    return cleaned[:limit] if cleaned else None


def _identity(item: ContentItem, result: TranscriptResult) -> tuple[str, str | None]:
    if item.platform == "youtube":
        for candidate in (item.id, result.metadata.get("video_id"), extract_youtube_video_id(item.url)):
            if isinstance(candidate, str) and _YOUTUBE_ID.fullmatch(candidate):
                return candidate, candidate
    else:
        for candidate in (item.id, result.metadata.get("bvid"), item.url):
            match = _BILIBILI_ID.search(candidate) if isinstance(candidate, str) else None
            if match:
                return match.group(), match.group()
    return short_hash(item.url), None


def _source_title(value: object, *, identifier: str, platform: str) -> str | None:
    title = _value(value)
    if title is None or title.casefold() in _PLACEHOLDER_TITLES:
        return None
    if title.casefold() in {identifier.casefold(), f"{platform} · {identifier}".casefold()}:
        return None
    return title


def _fetch_public_json(url: str, timeout: int) -> Any:
    # These fixed public endpoints do not receive session state or credentials.
    request = Request(url, headers={"User-Agent": "Openmd/0.0", "Accept": "application/json"})
    with urlopen(request, timeout=timeout) as response:
        if not 200 <= int(getattr(response, "status", 200)) < 300:
            raise ValueError("metadata_http_error")
        raw = response.read(_MAX_RESPONSE_BYTES + 1)
    if len(raw) > _MAX_RESPONSE_BYTES:
        raise ValueError("metadata_response_too_large")
    return json.loads(raw.decode("utf-8-sig"))


def _public_metadata(platform: str, identifier: str, fetch: MetadataFetcher, timeout: int) -> dict[str, str | None]:
    if platform == "youtube":
        url = "https://www.youtube.com/oembed?" + urlencode({
            "url": f"https://www.youtube.com/watch?v={identifier}", "format": "json",
        })
        payload = fetch(url, timeout)
        if not isinstance(payload, dict):
            raise ValueError("metadata_payload_invalid")
        return {
            "title": _value(payload.get("title")),
            "author": _value(payload.get("author_name"), limit=256),
            "published_at": None,  # oEmbed does not establish a publication date.
        }

    payload = fetch("https://api.bilibili.com/x/web-interface/view?" + urlencode({"bvid": identifier}), timeout)
    if not isinstance(payload, dict) or payload.get("code") not in (0, "0"):
        raise ValueError("metadata_payload_invalid")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ValueError("metadata_payload_invalid")
    owner = data.get("owner")
    pubdate = data.get("pubdate")
    published_at = None
    if isinstance(pubdate, (int, float)) and not isinstance(pubdate, bool) and pubdate > 0:
        try:
            published_at = datetime.fromtimestamp(pubdate, timezone.utc).date().isoformat()
        except (OverflowError, OSError, ValueError):
            pass
    return {
        "title": _value(data.get("title")),
        "author": _value(owner.get("name"), limit=256) if isinstance(owner, dict) else None,
        "published_at": published_at,
    }


def enrich_video_result(
    item: ContentItem,
    result: TranscriptResult,
    *,
    network_enabled: bool = False,
    fetch_json: MetadataFetcher | None = None,
    timeout: int = 8,
) -> TranscriptResult:
    """Fill absent video title/author, preserving all extraction evidence.

    At most one bounded public request is made per call, only for a usable
    YouTube/Bilibili result missing title or author and with network enabled.
    Existing source metadata takes priority. No video title is inferred from
    transcript words. ``fetch_json`` is an injectable JSON transport for local
    verification; it obeys the same gate as the default transport.
    """
    if item.platform not in {"youtube", "bilibili"} or not result.has_usable_text or result.backend == "mock":
        return result
    identifier, fetch_identifier = _identity(item, result)
    title = _source_title(result.title, identifier=identifier, platform=item.platform) or _source_title(
        item.title, identifier=identifier, platform=item.platform,
    )
    author = _value(result.author, limit=256) or _value(item.author, limit=256)
    published_at = result.published_at or item.published_at
    status = "already_present" if title and author else "not_requested"
    source = None
    if not (title and author) and network_enabled and fetch_identifier:
        try:
            public = _public_metadata(item.platform, fetch_identifier, fetch_json or _fetch_public_json, timeout)
            title = title or _source_title(public["title"], identifier=identifier, platform=item.platform)
            author = author or public["author"]
            published_at = published_at or public["published_at"]
            status = "available" if title and author else "partial"
            source = "youtube.oembed" if item.platform == "youtube" else "bilibili.public_view"
        except Exception:
            # A good transcript remains a good transcript even when metadata
            # is blocked, times out, or has an unexpected shape. Never retain
            # upstream payloads, URLs, headers or exception text in this note.
            status = "unavailable"
    missing = [field for field, value in (("title", title), ("author", author)) if not value]
    notes = list(result.source_notes)
    if not title:
        label = "YouTube" if item.platform == "youtube" else "Bilibili"
        title = f"{label} · {identifier}"
        note = "视频标题未取得；保留已提取文字，以平台和视频 ID 命名。"
        if note not in notes:
            notes.append(note)
    elif not author:
        note = "视频作者未取得；保留已提取文字。"
        if note not in notes:
            notes.append(note)
    return replace(
        result,
        title=title,
        author=author,
        published_at=published_at,
        source_notes=notes,
        metadata={**result.metadata, "video_metadata": {
            "status": status, "source": source, "missing_fields": missing,
        }},
    )
