"""Parser for qq-chat-exporter-style JSON group exports."""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any


def parse_qq_json_export(text: str) -> list[dict[str, Any]]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError("QQ JSON export is not valid JSON") from exc

    messages: list[dict[str, Any]] = []
    for index, row in enumerate(_candidate_rows(payload), start=1):
        if not isinstance(row, dict):
            continue
        message = _parse_message_row(row, index=index)
        if message is not None:
            messages.append(message)
    return messages


def _candidate_rows(payload: object) -> Iterable[object]:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return ()
    for key in ("messages", "items", "list", "data", "records", "results"):
        value = payload.get(key)
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            nested = list(_candidate_rows(value))
            if nested:
                return nested
    return ()


def _parse_message_row(row: dict[str, Any], *, index: int) -> dict[str, Any] | None:
    text = _first_string(row, "text", "content", "message", "msg")
    speaker = _first_string(row, "speaker", "sender", "from", "author", "nickname", "name")
    timestamp = _first_string(row, "timestamp", "time", "datetime", "date", "created_at")
    attachments = _list_or_empty(row.get("attachments") or row.get("files") or row.get("images"))
    if not text and not attachments:
        return None
    return {
        "id": _first_string(row, "id", "msg_id", "message_id") or f"json_{index:04d}",
        "timestamp": timestamp or "unknown-time",
        "speaker": speaker or "unknown",
        "text": text or "",
        "attachments": attachments,
        "links": _list_or_empty(row.get("links")),
        "source_map": [],
    }


def _first_string(row: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, int):
            return str(value)
    return None


def _list_or_empty(value: object) -> list[Any]:
    return value if isinstance(value, list) else []
