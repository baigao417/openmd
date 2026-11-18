"""ChatGPT official conversation trees, projected along current_node only."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import zipfile

from openmd.core.models import ContentItem, TranscriptResult, TurnBlock

MAX_EXPORT_BYTES = 100 * 1024 * 1024


def _read_export(path: Path):
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            members = [i for i in archive.infolist() if i.filename == "conversations.json"]
            if len(members) != 1 or members[0].file_size > MAX_EXPORT_BYTES:
                raise ValueError("chatgpt_export_missing_or_oversized_conversations")
            # Never unpack user-controlled paths, or read unrelated account files.
            with archive.open(members[0]) as stream:
                data = stream.read(MAX_EXPORT_BYTES + 1)
    else:
        with path.open("rb") as stream:
            data = stream.read(MAX_EXPORT_BYTES + 1)
    if len(data) > MAX_EXPORT_BYTES:
        raise ValueError("chatgpt_export_too_large")
    try:
        rows = json.loads(data.decode("utf-8-sig"))
    except (ValueError, UnicodeError) as exc:
        raise ValueError("chatgpt_export_invalid_json") from exc
    if not isinstance(rows, list) or not all(isinstance(r, dict) and isinstance(r.get("mapping"), dict) for r in rows):
        raise ValueError("chatgpt_export_invalid_structure")
    return rows


def is_chatgpt_export(value: str) -> bool:
    path = Path(value).expanduser()
    if path.suffix.lower() not in {".zip", ".json"} or not path.is_file():
        return False
    try:
        _read_export(path)
        return True
    except (OSError, ValueError, zipfile.BadZipFile):
        return False


def _timestamp(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        try:
            stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            return stamp.replace(tzinfo=timezone.utc).timestamp() if stamp.tzinfo is None else stamp.timestamp()
        except (ValueError, TypeError, OverflowError) as exc:
            raise ValueError("chatgpt_export_invalid_time") from exc


def _iso_time(value) -> str | None:
    stamp = _timestamp(value)
    if stamp is None:
        return None
    try:
        return datetime.fromtimestamp(stamp, timezone.utc).isoformat()
    except (ValueError, OverflowError, OSError) as exc:
        raise ValueError("chatgpt_export_invalid_time") from exc


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
    lower, upper = _timestamp(since), _timestamp(until)
    if lower is not None and upper is not None and lower > upper:
        raise ValueError("chatgpt_export_invalid_time_range")
    output = []
    for row in rows:
        if title and title.casefold() not in str(row.get("title") or "").casefold():
            continue
        stamp = _timestamp(row.get("create_time"))
        if (lower is not None and (stamp is None or stamp < lower)) or (upper is not None and (stamp is None or stamp > upper)):
            continue
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
        cid = str(row.get("id") or row.get("conversation_id") or "")
        if not cid or any(c in cid for c in "/\\?#\r\n"):
            raise ValueError("chatgpt_export_invalid_conversation_id")
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
    from openmd.core.output import write_item_result

    rows = load_conversations(value, title=getattr(args, "chatgpt_title", None),
                              since=getattr(args, "chatgpt_since", None), until=getattr(args, "chatgpt_until", None))
    destination = args.save_to or args.out
    if not destination and tty:
        destination = input("保存目录：").strip()
    if not destination:
        raise ValueError("chatgpt_export_output_directory_required")
    outputs, conversations = [], []
    for item, result in rows:
        sync = {}
        target = write_item_result(item, result, destination, raw=True, sync_receipt=sync)
        outputs.append(str(target))
        conversations.append({**result.metadata, "sync": sync, "path": str(target)})
    receipt = {"ok": True, "command": "go", "source_level": "manual_export", "status": "verified",
               "saved_to": str(Path(destination).resolve()), "outputs": outputs, "conversations": conversations}
    if getattr(args, "json", False):
        print(json.dumps(receipt, ensure_ascii=True))
    else:
        print(f"已保存 {len(outputs)} 份对话：{Path(destination).resolve()}")
    return 0
