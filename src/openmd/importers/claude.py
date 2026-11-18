"""Offline Claude export projection; no browser, API or asset access."""
from __future__ import annotations

from collections import Counter
from pathlib import Path

from openmd.core.models import ContentItem, TranscriptResult, TurnBlock
from openmd.core.sync_manifest import source_id_for
from .common import (conversation_id, is_export, iso_time, read_export,
                     select_conversations, write_import)

_ROW_FIELDS = {"uuid", "name", "created_at", "updated_at", "chat_messages"}
_MESSAGE_FIELDS = {"uuid", "sender", "text", "content", "created_at", "updated_at", "attachments", "files"}
_BLOCK_FIELDS = {"type", "text", "thinking", "signature", "id", "name", "input", "content", "tool_use_id", "is_error"}
_ASSET_FIELDS = {"file_name", "file_type", "file_kind", "name", "type", "mime_type"}
_NON_TEXT = {"tool_use", "tool_result", "thinking"}


def _read_export(path: Path):
    rows = read_export(path, provider="claude")
    if not isinstance(rows, list) or not all(
        isinstance(row, dict) and isinstance(row.get("uuid"), str)
        and isinstance(row.get("chat_messages"), list) and "mapping" not in row
        for row in rows
    ):
        raise ValueError("claude_export_invalid_structure")
    for row in rows:
        conversation_id(row["uuid"], provider="claude")
        if not all(isinstance(message, dict) for message in row["chat_messages"]):
            raise ValueError("claude_export_invalid_messages")
    return rows


def is_claude_export(value: str) -> bool:
    # [] has no identifying fields, so it must not claim another export's input.
    def identifiable(path):
        if not _read_export(path):
            raise ValueError("claude_export_ambiguous_empty_list")
    return is_export(value, identifiable)


def _unknown_fields(value: dict, known: set[str]) -> int:
    return len(value.keys() - known)


def _label(value, fallback: str) -> str:
    # Metadata labels occupy one line; never serialize nested content or URLs.
    return value.replace("\r", " ").replace("\n", " ") if isinstance(value, str) else fallback


def _message_text(message: dict, counts: Counter) -> tuple[str, int]:
    content = message.get("content")
    unknown = _unknown_fields(message, _MESSAGE_FIELDS)
    parts = []
    has_text = False
    previous_was_text = False
    if content is not None:
        if not isinstance(content, list):
            raise ValueError("claude_export_invalid_content")
        for block in content:
            if isinstance(block, dict):
                unknown += _unknown_fields(block, _BLOCK_FIELDS)
            if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
                if previous_was_text:
                    parts[-1] += block["text"]
                else:
                    parts.append(block["text"])
                has_text = True
                previous_was_text = True
            else:
                kind = block.get("type") if isinstance(block, dict) else None
                kind = kind if isinstance(kind, str) and kind in _NON_TEXT else "unknown"
                counts[kind] += 1
                parts.append(f"[Content block: {kind}]")
                previous_was_text = False
    # Text-only exports and messages with metadata-only content use the text field.
    if not has_text and isinstance(message.get("text"), str) and message["text"]:
        parts.insert(0, message["text"])
    text = "\n".join(parts)
    assets = []
    for field, kind in (("attachments", "attachment"), ("files", "file")):
        values = message.get(field)
        if values is None:
            continue
        if not isinstance(values, list):
            raise ValueError("claude_export_invalid_assets")
        for value in values:
            counts[kind] += 1
            if isinstance(value, dict):
                unknown += _unknown_fields(value, _ASSET_FIELDS)
                name = _label(value.get("file_name") or value.get("name"), "unnamed")
                media_type = _label(value.get("file_type") or value.get("mime_type") or value.get("file_kind") or value.get("type"), "unknown")
            else:
                name, media_type = "unnamed", "unknown"
            assets.append(f"[{kind.capitalize()}: {name} ({media_type})]")
    if assets:
        text += ("\n" if text else "") + "\n".join(assets)
    return text, unknown


def load_conversations(path, *, title: str | None = None, since=None, until=None):
    rows = _read_export(Path(path).expanduser())
    output = []
    for row in select_conversations(rows, provider="claude", title_field="name", time_field="created_at",
                                     title=title, since=since, until=until):
        turns, counts = [], Counter()
        unknown = _unknown_fields(row, _ROW_FIELDS)
        unknown_senders = 0
        for message in row["chat_messages"]:
            text, extra = _message_text(message, counts)
            unknown += extra
            sender = message.get("sender")
            role = {"human": "user", "assistant": "assistant"}.get(sender, "unknown") if isinstance(sender, str) else "unknown"
            unknown_senders += int(role == "unknown")
            turns.append(TurnBlock(role=role, text=text,
                                   time=iso_time(message.get("created_at"), provider="claude"),
                                   message_id=message.get("uuid") if isinstance(message.get("uuid"), str) else None))
        cid = conversation_id(row["uuid"], provider="claude")
        item = ContentItem(platform="ai_chat", input_type="single", url="https://claude.ai/chat/" + cid,
                           id=cid, title=str(row.get("name") or "Claude conversation"),
                           source_type="conversation", raw_meta={"chat_site": "claude"})
        result = TranscriptResult(text="\n\n".join(turn.text for turn in turns), blocks=turns,
                                  title=item.title, status="partial" if counts or unknown_senders or not turns else "verified",
                                  source_level="manual_export", backend="claude.official_export",
                                  metadata={"chat_site": "claude", "source_id": source_id_for(item),
                                            "coverage": "exported_messages", "turn_count": len(turns),
                                            "omission_counts": dict(counts), "unknown_field_count": unknown,
                                            "unknown_sender_count": unknown_senders,
                                            "created_at": iso_time(row.get("created_at"), provider="claude"),
                                            "updated_at": iso_time(row.get("updated_at"), provider="claude")},
                                  source_notes=["Official export; chat_messages in file order; no branch completeness inferred.",
                                                "Non-text blocks and assets are placeholders only; no asset contents are read.",
                                                "Placeholder counts: " + str(dict(counts)),
                                                f"Unknown fields: {unknown}; unknown senders: {unknown_senders}.",
                                                f"Exported turns: {len(turns)}; zero turns means an empty exported conversation."])
        output.append((item, result))
    return output


def cmd_import(args, value: str, *, tty: bool) -> int:
    rows = load_conversations(value, title=getattr(args, "claude_title", None),
                              since=getattr(args, "claude_since", None), until=getattr(args, "claude_until", None))
    return write_import(args, rows, provider="claude", tty=tty)
