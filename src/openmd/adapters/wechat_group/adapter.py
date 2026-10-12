"""Mock/gated adapter for WeChat group conversation package inputs."""

from __future__ import annotations

import hashlib
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from openmd.adapters.base import PlatformAdapter
from openmd.adapters.high_privacy import high_privacy_blocker_result
from openmd.core.detector import detect_platform
from openmd.core.models import ContentItem, TranscriptResult


class WechatGroupAdapter(PlatformAdapter):
    """Resolve explicit WeChat group selectors without reading local sessions."""

    def __init__(self, platform_id: str = "wechat_group", default_backend: str = "opencli.wx.history") -> None:
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
        selector = parse_wechat_group_selector(input_value)
        group_name = selector["group_name"]
        since = selector.get("since")
        until = selector.get("until")
        return [
            ContentItem(
                platform=self.id,
                input_type="collection",
                url=input_value.strip(),
                id=_conversation_id(group_name, since=since, until=until),
                title=f"WeChat group: {group_name}",
                source_type="conversation",
                raw_meta={
                    "profile": "conversation",
                    "permission_scope": "user_accessible",
                    "target_kind": "wechat_group",
                    "group_name": group_name,
                    "since": since,
                    "until": until,
                    "discovery_limit": limit,
                    "allow_all": all,
                },
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
        if selected in {"mock", "wechat_group.mock"}:
            messages = _messages_from_meta(item.raw_meta)
            return TranscriptResult(
                text=render_wechat_group_markdown(messages),
                status="verified",
                source_level="native_text",
                backend=selected,
                title=item.title or "WeChat group conversation",
                source_notes=[
                    "local mock adapter smoke only; no WeChat database, session, OpenCLI, account, or decryption action was attempted",
                ],
                metadata={"profile": "conversation", "messages": messages},
            )
        return high_privacy_blocker_result(
            platform=self.id,
            backend=selected,
            target_kind=str(item.raw_meta.get("target_kind") or "wechat_group"),
            state="refresh_needed",
            error_code="refresh_needed",
            error_summary=(
                "Refresh or provide an approved scoped WeChat group export/cache before running real acquisition."
            ),
            next_action=(
                "Use an explicitly approved export/cache or rerun collection with an enabled, scoped browser/backend "
                "after refreshing local WeChat data."
            ),
            permission_scope=str(item.raw_meta.get("permission_scope") or "unknown"),
            title=item.title or "WeChat group conversation",
            source_notes=[
                "wechat group adapter resolved the explicit selector only; real acquisition is behind gated discovery/export providers",
                f"allow_fallback={allow_fallback}",
            ],
        )


def parse_wechat_group_selector(value: str) -> dict[str, str | None]:
    parsed = urlparse(value.strip())
    if parsed.scheme in {"wx", "wechat"} and parsed.netloc.lower() == "group":
        group_name = unquote(parsed.path.strip("/"))
    elif parsed.scheme in {"wx-group", "wechat-group"}:
        group_name = unquote((parsed.netloc or parsed.path.strip("/")).strip("/"))
    else:
        raise ValueError("WeChat group selector must use wx://group/<group-name>.")

    if not group_name or group_name in {"*", "all"}:
        raise ValueError("WeChat group selector must name one explicit group.")

    query = parse_qs(parsed.query)
    return {
        "group_name": group_name,
        "since": _first_query_value(query, "since"),
        "until": _first_query_value(query, "until"),
    }


def render_wechat_group_markdown(messages: list[dict[str, Any]]) -> str:
    lines = ["# WeChat group conversation", ""]
    for message in messages:
        timestamp = str(message.get("timestamp") or "unknown-time")
        speaker = str(message.get("speaker") or "unknown")
        text = str(message.get("text") or "").strip()
        if not text:
            text = "_No text content._"
        lines.append(f"- [{timestamp}] {speaker}: {text}")
    return "\n".join(lines).strip() + "\n"


def _messages_from_meta(raw_meta: dict[str, Any]) -> list[dict[str, Any]]:
    value = raw_meta.get("messages") if isinstance(raw_meta, dict) else None
    if isinstance(value, list) and value:
        return [message for message in value if isinstance(message, dict)]
    group_name = str(raw_meta.get("group_name") or "mock-group")
    return [
        {
            "id": "msg_001",
            "timestamp": "2026-06-23T10:00:00+08:00",
            "speaker": "Mock sender",
            "text": f"Mock message scoped to WeChat group {group_name}.",
            "attachments": [],
            "links": [],
            "source_map": [],
        }
    ]


def _first_query_value(query: dict[str, list[str]], key: str) -> str | None:
    values = query.get(key)
    if not values:
        return None
    value = values[0].strip()
    return value or None


def _conversation_id(group_name: str, *, since: str | None, until: str | None) -> str:
    return hashlib.sha256(f"{group_name}:{since or ''}:{until or ''}".encode("utf-8")).hexdigest()[:12]
