"""Turn a captured HTML page into a privacy reviewed structural sample.

The sanitizer deliberately works on the DOM rather than regular expressions: the
loaded-chat parser can therefore consume the resulting sample and retain the same
element and role structure without retaining page text.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from bs4 import BeautifulSoup, NavigableString, Tag

from openmd.core.secrets import is_sensitive_key, is_sensitive_query_key


@dataclass(frozen=True)
class SanitizedSnapshot:
    html: str
    manifest: dict[str, Any]


_LATIN = "loremipsum"
_CJK = "示例文本问题回答内容"


def _replacement(text: str) -> tuple[str, int]:
    count = 0
    out: list[str] = []
    for char in text:
        if char.isspace():
            out.append(char)
        elif "0" <= char <= "9":
            out.append(str((ord(char) - 48 + count + 1) % 10))
            count += 1
        elif "\u3400" <= char <= "\u9fff":
            out.append(_CJK[count % len(_CJK)])
            count += 1
        elif char.isalpha() and char.isascii():
            base = _LATIN[count % len(_LATIN)]
            out.append(base.upper() if char.isupper() else base)
            count += 1
        else:
            out.append("·" if not char.isalnum() else char)
            count += 1
    return "".join(out), count


def _safe_url(value: str, *, source: bool = False) -> str:
    parsed = urlsplit(value)
    if not parsed.scheme or not parsed.netloc:
        return "about:blank"
    # Samples retain only the origin. This removes paths, queries and fragments,
    # including session IDs, while keeping a useful site hint for maintainers.
    return f"{parsed.scheme}://{parsed.netloc}/" if not source else f"{parsed.scheme}://{parsed.netloc}/"


def _structure_hash(soup: BeautifulSoup) -> str:
    parts: list[str] = []
    for node in soup.find_all(True):
        attrs = []
        for key in sorted(node.attrs):
            if key == "class":
                attrs.append((key, tuple(sorted(str(v) for v in node.get(key, [])))))
            elif key.startswith("data-") or key in {"role", "aria-label", "aria-hidden", "hidden"}:
                attrs.append((key, str(node.get(key))))
        parts.append(f"<{node.name}:{attrs}>")
    return hashlib.sha256("".join(parts).encode("utf-8")).hexdigest()


def sanitize_snapshot(html: str, url: str, *, captured_at: str | None = None) -> SanitizedSnapshot:
    if not isinstance(html, str) or not html.strip():
        raise ValueError("empty_snapshot_html")
    if not isinstance(url, str) or urlsplit(url).scheme not in {"http", "https"}:
        raise ValueError("invalid_snapshot_url")
    soup = BeautifulSoup(html, "html.parser")
    stats = {"text_nodes": 0, "text_characters": 0, "removed_nodes": 0, "scrubbed_attributes": 0, "sanitized_urls": 0}

    for node in list(soup.select("script, style, noscript")):
        node.decompose()
        stats["removed_nodes"] += 1
    for node in soup.select("input, textarea"):
        if node.name == "input":
            node.attrs.pop("value", None)
        else:
            node.clear()
        node.attrs.pop("value", None)

    for node in soup.find_all(True):
        for key in list(node.attrs):
            value = node.attrs.get(key)
            if is_sensitive_key(str(key)) or any(re.search(pattern, str(value), re.I) for pattern in (r"cookie", r"bearer", r"token", r"authorization", r"session")):
                if key not in {"class"} and not (str(key).startswith("data-") and str(value).lower() in {"user", "assistant", "system", "tool", "turn"}):
                    node.attrs.pop(key, None)
                    stats["scrubbed_attributes"] += 1
                    continue
            if key in {"href", "src", "action", "poster"}:
                if isinstance(value, str):
                    node.attrs[key] = _safe_url(value)
                    stats["sanitized_urls"] += 1
            elif key in {"value", "title", "alt", "aria-label"}:
                # Keep labels useful only where they are structural. Textual
                # labels are replaced below; these attrs must not leak them.
                if key == "value" or not str(value).startswith("data-"):
                    node.attrs[key] = "示例"
                    stats["scrubbed_attributes"] += 1
            elif isinstance(value, list):
                node.attrs[key] = [str(v) for v in value]

    for text_node in list(soup.find_all(string=True)):
        if not isinstance(text_node, NavigableString) or text_node.parent is None:
            continue
        if text_node.parent.name in {"script", "style", "noscript"}:
            continue
        replacement, amount = _replacement(str(text_node))
        text_node.replace_with(replacement)
        stats["text_nodes"] += 1
        stats["text_characters"] += amount

    serialized = str(soup)
    manifest = {
        "site": (urlsplit(url).hostname or "").lower(),
        "source_url": _safe_url(url, source=True),
        "captured_at": captured_at or datetime.now(timezone.utc).isoformat(),
        "structure_hash": _structure_hash(soup),
        "replacement_stats": stats,
        "sanitizer": "openmd.diagnostics.sanitize.v1",
    }
    return SanitizedSnapshot(html=serialized, manifest=manifest)
