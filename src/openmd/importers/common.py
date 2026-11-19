"""Bounded, offline official-export reading, selection and artifact writing."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import zipfile

MAX_EXPORT_BYTES = 100 * 1024 * 1024


def read_export(path: Path, *, provider: str, max_bytes: int | None = None):
    limit = MAX_EXPORT_BYTES if max_bytes is None else max_bytes
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            members = [i for i in archive.infolist() if i.filename == "conversations.json"]
            if len(members) != 1 or members[0].file_size > limit:
                raise ValueError(f"{provider}_export_missing_or_oversized_conversations")
            # Only this exact root member is read; paths are never extracted or resolved.
            try:
                with archive.open(members[0]) as stream:
                    data = stream.read(limit + 1)
            except (zipfile.BadZipFile, RuntimeError, NotImplementedError) as exc:
                raise ValueError(f"{provider}_export_invalid_archive") from exc
    else:
        with path.open("rb") as stream:
            data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f"{provider}_export_too_large")
    try:
        return json.loads(data.decode("utf-8-sig"))
    except (ValueError, UnicodeError) as exc:
        raise ValueError(f"{provider}_export_invalid_json") from exc


def is_export(value: str, reader) -> bool:
    path = Path(value).expanduser()
    if path.suffix.lower() not in {".zip", ".json"} or not path.is_file():
        return False
    try:
        reader(path)
        return True
    except (OSError, ValueError, zipfile.BadZipFile):
        return False


def timestamp(value, *, provider: str) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        try:
            stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            return stamp.replace(tzinfo=timezone.utc).timestamp() if stamp.tzinfo is None else stamp.timestamp()
        except (ValueError, TypeError, OverflowError) as exc:
            raise ValueError(f"{provider}_export_invalid_time") from exc


def iso_time(value, *, provider: str) -> str | None:
    stamp = timestamp(value, provider=provider)
    if stamp is None:
        return None
    try:
        return datetime.fromtimestamp(stamp, timezone.utc).isoformat()
    except (ValueError, OverflowError, OSError) as exc:
        raise ValueError(f"{provider}_export_invalid_time") from exc


def select_conversations(rows, *, provider: str, title_field: str, time_field: str,
                         title: str | None = None, since=None, until=None):
    lower, upper = timestamp(since, provider=provider), timestamp(until, provider=provider)
    if lower is not None and upper is not None and lower > upper:
        raise ValueError(f"{provider}_export_invalid_time_range")
    for row in rows:
        if title and title.casefold() not in str(row.get(title_field) or "").casefold():
            continue
        stamp = timestamp(row.get(time_field), provider=provider)
        if (lower is not None and (stamp is None or stamp < lower)) or (upper is not None and (stamp is None or stamp > upper)):
            continue
        yield row


def conversation_id(value, *, provider: str) -> str:
    cid = str(value or "")
    if not cid or any(c in cid for c in "/\\?#\r\n"):
        raise ValueError(f"{provider}_export_invalid_conversation_id")
    return cid


def write_import(args, rows, *, provider: str, tty: bool) -> int:
    from openmd.core.output import write_item_result

    destination = args.save_to or args.out
    if not destination and tty:
        destination = input("保存目录：").strip()
    if not destination:
        raise ValueError(f"{provider}_export_output_directory_required")
    outputs, conversations = [], []
    status = "verified"
    for item, result in rows:
        sync = {}
        target = write_item_result(item, result, destination, raw=True, sync_receipt=sync)
        outputs.append(str(target))
        conversations.append({**result.metadata, "sync": sync, "path": str(target)})
        if result.status != "verified":
            status = "partial"
    receipt = {"ok": True, "command": "go", "source_level": "manual_export", "status": status,
               "saved_to": str(Path(destination).resolve()), "outputs": outputs, "conversations": conversations}
    if getattr(args, "json", False):
        print(json.dumps(receipt, ensure_ascii=True))
    else:
        print(f"已保存 {len(outputs)} 份对话：{Path(destination).resolve()}")
    return 0
