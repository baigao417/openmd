"""ChatGPT official conversation trees, projected along current_node only."""
from __future__ import annotations

import json
from pathlib import Path

from openmd.core.models import ContentItem, TranscriptResult, TurnBlock
from .common import (MAX_EXPORT_BYTES, conversation_id, is_export, iso_time, read_export,
                     select_conversations, timestamp, write_import)


def _read_export(path: Path):
    rows = read_export(path, provider="chatgpt", max_bytes=MAX_EXPORT_BYTES)
    if not isinstance(rows, list) or not all(isinstance(r, dict) and isinstance(r.get("mapping"), dict) for r in rows):
        raise ValueError("chatgpt_export_invalid_structure")
    return rows


def is_chatgpt_export(value: str) -> bool:
    return is_export(value, _read_export)


def _timestamp(value) -> float | None:
    return timestamp(value, provider="chatgpt")


def _iso_time(value) -> str | None:
    return iso_time(value, provider="chatgpt")


def _content_text(content: dict) -> str:
    parts = content.get("parts")
    if not isinstance(parts, list):
        # Tool invocations often carry code/execution output in `text`.
        return content.get("text", "") if isinstance(content.get("text"), str) else ""
    projected = []
    for part in parts:
        if isinstance(part, str):
            projected.append(part)
        elif isinstance(part, dict):
            if isinstance(part.get("text"), str):
                projected.append(part["text"])
            elif isinstance(part.get("asset_pointer"), str):
                # Preserve a reference; don't resolve or download local/cloud assets.
                projected.append("[Image reference: " + part["asset_pointer"] + "]")
            else:
                projected.append(json.dumps(part, ensure_ascii=False))
    return "\n".join(projected)


def load_conversations(path, *, title: str | None = None, since=None, until=None):
    rows = _read_export(Path(path).expanduser())
    output = []
    for row in select_conversations(rows, provider="chatgpt", title_field="title", time_field="create_time",
                                     title=title, since=since, until=until):
        mapping, cursor = row["mapping"], row.get("current_node")
        if not mapping:
            continue
        if not cursor or cursor not in mapping:
            raise ValueError("chatgpt_export_current_node_missing")
        path_ids, seen = [], set()
        while cursor is not None:
            if cursor in seen or cursor not in mapping or not isinstance(mapping[cursor], dict):
                raise ValueError("chatgpt_export_broken_current_path")
            seen.add(cursor)
            path_ids.append(cursor)
            cursor = mapping[cursor].get("parent")
        turns = []
        for node_id in reversed(path_ids):
            message = mapping[node_id].get("message")
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            if not isinstance(content, dict):
                continue
            text = _content_text(content)
            if not text.strip():
                continue
            author = message.get("author") or {}
            turns.append(TurnBlock(role=str(author.get("role") or "unknown"), text=text,
                                   time=_iso_time(message.get("create_time")),
                                   message_id=str(message.get("id") or node_id)))
        if not turns:
            continue
        cid = conversation_id(row.get("id") or row.get("conversation_id"), provider="chatgpt")
        source_id = "chatgpt:" + cid
        alternative_count = sum(max(0, len(n.get("children") or []) - 1) for n in mapping.values() if isinstance(n, dict))
        item = ContentItem(platform="ai_chat", input_type="single", url="https://chatgpt.com/c/"+cid,
                           id=cid, title=str(row.get("title") or "ChatGPT conversation"),
                           source_type="conversation", raw_meta={"chat_site": "chatgpt"})
        result = TranscriptResult(text="\n\n".join(t.text for t in turns), blocks=turns,
                                  title=item.title, status="verified", source_level="manual_export",
                                  backend="chatgpt.official_export", metadata={
                                      "chat_site": "chatgpt", "source_id": source_id,
                                      "coverage": "current_branch", "current_node": row["current_node"],
                                      "alternative_branch_count": alternative_count,
                                      "turn_count": len(turns)},
                                  source_notes=["Official export; current_node ancestor path only.",
                                                f"Alternative branches: {alternative_count}; asset references are not downloaded."])
        output.append((item, result))
    return output


def cmd_import(args, value: str, *, tty: bool) -> int:
    """Local multi-conversation projection using the shared renderer and sync writer."""
    rows = load_conversations(value, title=getattr(args, "chatgpt_title", None),
                              since=getattr(args, "chatgpt_since", None), until=getattr(args, "chatgpt_until", None))
    return write_import(args, rows, provider="chatgpt", tty=tty)
