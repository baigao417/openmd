"""Mock/gated adapter for QQ group export package inputs."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from openmd.adapters.base import PlatformAdapter
from openmd.adapters.high_privacy import high_privacy_blocker_result
from openmd.adapters.qq_group.parsers.html import parse_qq_html_export
from openmd.adapters.qq_group.parsers.json import parse_qq_json_export
from openmd.core.detector import detect_platform
from openmd.core.models import ContentItem, TranscriptResult


class QQGroupAdapter(PlatformAdapter):
    """Resolve QQ group exports without database access or decryption."""

    def __init__(self, platform_id: str = "qq_group", default_backend: str = "qq_group.export") -> None:
        self.id = platform_id
        self.default_backend = default_backend

    def can_handle(self, input_value: str) -> bool:
        return detect_platform(input_value) == self.id

    def resolve_items(
        self,
        input_value: str,
        *,
        limit: int | None = None,
        all: bool = False,
    ) -> list[ContentItem]:
        value = input_value.strip()
        if _is_selector(value):
            group_name = _group_from_selector(value)
            raw_meta: dict[str, Any] = {
                "profile": "conversation",
                "permission_scope": "user_accessible",
                "target_kind": "qq_group",
                "group_name": group_name,
                "discovery_limit": limit,
                "allow_all": all,
            }
            title = f"QQ group: {group_name}"
            identity = _stable_hash(group_name)
        else:
            path = _local_path_from_input(value)
            group_name = _group_name_from_path(path)
            raw_meta = {
                "profile": "conversation",
                "permission_scope": "user_export",
                "target_kind": "qq_group_export",
                "group_name": group_name,
                "local_path": str(path),
                "export_format": path.suffix.lower().lstrip("."),
                "discovery_limit": limit,
                "allow_all": all,
            }
            title = f"QQ group export: {group_name}"
            identity = _stable_hash(str(path.resolve()) if path.exists() else str(path))

        return [
            ContentItem(
                platform=self.id,
                input_type="collection",
                url=value,
                id=identity,
                title=title,
                source_type="conversation",
                raw_meta=raw_meta,
            )
        ]

    def extract_text(
        self,
        item: ContentItem,
        *,
        backend: str | None = None,
        backend_chain: tuple[str, ...] | None = None,
        allow_fallback: bool = True,
    ) -> TranscriptResult:
        selected = (backend_chain or (backend or self.default_backend,))[0]
        if selected in {"mock", "qq_group.mock"}:
            messages = _messages_from_item(item)
            return TranscriptResult(
                text=render_qq_group_markdown(messages),
                status="verified",
                source_level="manual_export",
                backend=selected,
                title=item.title or "QQ group conversation",
                source_notes=[
                    "local mock adapter smoke only; no QQ database, session, DB key extraction, or decryption action was attempted",
                ],
                metadata={"profile": "conversation", "messages": messages},
            )
        if selected == "qq_group.export":
            messages, blocker = _messages_from_export_item(item)
            if blocker is not None:
                return high_privacy_blocker_result(
                    platform=self.id,
                    backend=selected,
                    target_kind=str(item.raw_meta.get("target_kind") or "qq_group_export"),
                    state=blocker["state"],
                    error_code=blocker["error_code"],
                    error_summary=blocker["error_summary"],
                    next_action=blocker["next_action"],
                    permission_scope=str(item.raw_meta.get("permission_scope") or "unknown"),
                    title=item.title or "QQ group conversation",
                    source_notes=[
                        "qq group adapter resolved the explicit selector/export only; no QQ database, DB key extraction, or decryption action was attempted",
                        f"allow_fallback={allow_fallback}",
                    ],
                )
            return TranscriptResult(
                text=render_qq_group_markdown(messages),
                status="verified",
                source_level="manual_export",
                backend=selected,
                title=item.title or "QQ group conversation",
                source_notes=[
                    "parsed an explicitly provided QQ group export file",
                    "no QQ database, DB key extraction, session, or decryption action was attempted",
                ],
                metadata={"profile": "conversation", "messages": messages},
            )
        return high_privacy_blocker_result(
            platform=self.id,
            backend=selected,
            target_kind=str(item.raw_meta.get("target_kind") or "qq_group"),
            state="export_required",
            error_code="export_required",
            error_summary="Provide an approved QQ group export file before running real parsing.",
            next_action="Export the scoped QQ group conversation to JSON, HTML, or TXT and pass that local file to Openmd.",
            permission_scope=str(item.raw_meta.get("permission_scope") or "unknown"),
            title=item.title or "QQ group conversation",
            source_notes=[
                "qq group adapter resolved the export/selector only; no QQ database, DB key extraction, or decryption action was attempted",
                f"allow_fallback={allow_fallback}",
            ],
        )


def parse_qq_export_file(path: Path) -> list[dict[str, Any]]:
    suffix = path.suffix.lower()
    text = path.read_text(encoding="utf-8")
    if suffix == ".json":
        return parse_qq_json_export(text)
    if suffix == ".html":
        return parse_qq_html_export(text)
    if suffix == ".txt":
        return parse_qq_txt_export(text)
    raise ValueError(f"unsupported QQ export suffix: {suffix}")


def parse_qq_txt_export(text: str) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    pattern = re.compile(r"^\[?(?P<time>[^\]\s][^\]]{5,40})\]?\s+(?P<speaker>[^:：]+)[:：]\s*(?P<text>.*)$")
    for index, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        match = pattern.match(stripped)
        if match:
            messages.append(
                {
                    "id": f"txt_{index:04d}",
                    "timestamp": match.group("time").strip(),
                    "speaker": match.group("speaker").strip(),
                    "text": match.group("text").strip(),
                    "attachments": [],
                    "links": [],
                    "source_map": [],
                }
            )
    return messages


def render_qq_group_markdown(messages: list[dict[str, Any]]) -> str:
    lines = ["# QQ group conversation", ""]
    for message in messages:
        timestamp = str(message.get("timestamp") or "unknown-time")
        speaker = str(message.get("speaker") or "unknown")
        text = str(message.get("text") or "").strip() or "_No text content._"
        lines.append(f"- [{timestamp}] {speaker}: {text}")
    return "\n".join(lines).strip() + "\n"


def _messages_from_item(item: ContentItem) -> list[dict[str, Any]]:
    raw_meta = item.raw_meta if isinstance(item.raw_meta, dict) else {}
    value = raw_meta.get("messages")
    if isinstance(value, list) and value:
        return [message for message in value if isinstance(message, dict)]

    local_path = raw_meta.get("local_path")
    if isinstance(local_path, str):
        path = Path(local_path)
        if path.exists():
            messages = parse_qq_export_file(path)
            if messages:
                return messages

    group_name = str(raw_meta.get("group_name") or "mock-group")
    return [
        {
            "id": "qq_msg_001",
            "timestamp": "2026-06-23T14:00:00+08:00",
            "speaker": "Mock QQ sender",
            "text": f"Mock message scoped to QQ group {group_name}.",
            "attachments": [],
            "links": [],
            "source_map": [],
        }
    ]


def _messages_from_export_item(item: ContentItem) -> tuple[list[dict[str, Any]], dict[str, str] | None]:
    raw_meta = item.raw_meta if isinstance(item.raw_meta, dict) else {}
    local_path = raw_meta.get("local_path")
    if not isinstance(local_path, str) or not local_path.strip():
        return [], {
            "state": "export_required",
            "error_code": "export_required",
            "error_summary": "A scoped QQ group export file is required.",
            "next_action": "Export the scoped QQ group conversation to JSON, HTML, or TXT and pass that local file to Openmd.",
        }
    path = Path(local_path)
    if not path.exists():
        return [], {
            "state": "export_required",
            "error_code": "export_required",
            "error_summary": "The configured QQ group export file does not exist.",
            "next_action": "Provide an existing scoped QQ group JSON, HTML, or TXT export file.",
        }
    try:
        messages = parse_qq_export_file(path)
    except ValueError as exc:
        return [], {
            "state": "export_required",
            "error_code": "export_format_unsupported",
            "error_summary": str(exc),
            "next_action": "Use a supported scoped QQ export format: JSON, HTML, or TXT.",
        }
    if not messages:
        return [], {
            "state": "no_delta",
            "error_code": "no_delta",
            "error_summary": "The QQ group export file contained no parseable messages.",
            "next_action": "Confirm the export scope/date range and provide a non-empty scoped export.",
        }
    return messages, None


def _is_selector(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"qq", "qq-group", "qq_export", "qq-export"}


def _group_from_selector(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme == "qq" and parsed.netloc.lower() == "group":
        group_name = parsed.path.strip("/")
    else:
        group_name = parsed.netloc or parsed.path.strip("/")
    group_name = unquote(group_name)
    if not group_name or group_name in {"*", "all"}:
        raise ValueError("QQ group selector must name one explicit group.")
    return group_name


def _local_path_from_input(value: str) -> Path:
    parsed = urlparse(value)
    if parsed.scheme == "file":
        return Path(unquote(parsed.path))
    return Path(value)


def _group_name_from_path(path: Path) -> str:
    stem = path.name
    for suffix in (".json", ".html", ".txt"):
        if stem.lower().endswith(suffix):
            stem = stem[: -len(suffix)]
    return stem or "qq-group-export"


def _stable_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
