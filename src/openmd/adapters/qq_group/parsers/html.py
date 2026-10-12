"""Small parser for user-provided QQ HTML group exports."""

from __future__ import annotations

import re
from html import unescape
from typing import Any


_MESSAGE_RE = re.compile(
    r"<(?P<tag>div|li)[^>]+class=[\"'][^\"']*(?:message|msg)[^\"']*[\"'][^>]*>(?P<body>.*?)</(?P=tag)>",
    re.IGNORECASE | re.DOTALL,
)


def parse_qq_html_export(text: str) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    blocks = [match.group("body") for match in _MESSAGE_RE.finditer(text)]
    if not blocks:
        blocks = [line for line in text.splitlines() if "sender" in line or "content" in line]

    for index, block in enumerate(blocks, start=1):
        message = _parse_message_block(block, index=index)
        if message is not None:
            messages.append(message)
    return messages


def _parse_message_block(block: str, *, index: int) -> dict[str, Any] | None:
    timestamp = _class_text(block, "time") or _class_text(block, "timestamp")
    speaker = _class_text(block, "sender") or _class_text(block, "nickname") or _class_text(block, "name")
    content = _class_text(block, "content") or _class_text(block, "text") or _strip_tags(block)
    if not content:
        return None
    return {
        "id": f"html_{index:04d}",
        "timestamp": timestamp or "unknown-time",
        "speaker": speaker or "unknown",
        "text": content,
        "attachments": [],
        "links": [],
        "source_map": [],
    }


def _class_text(block: str, class_name: str) -> str | None:
    pattern = re.compile(
        rf"<[^>]+class=[\"'][^\"']*{re.escape(class_name)}[^\"']*[\"'][^>]*>(.*?)</[^>]+>",
        re.IGNORECASE | re.DOTALL,
    )
    match = pattern.search(block)
    if not match:
        return None
    return _strip_tags(match.group(1))


def _strip_tags(value: str) -> str:
    text = re.sub(r"<[^>]+>", " ", value)
    text = unescape(text)
    return " ".join(text.split())
