"""Mock/gated adapter for WeChat Moments person-feed package inputs."""

from __future__ import annotations

import hashlib
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from openmd.adapters.base import PlatformAdapter
from openmd.adapters.high_privacy import high_privacy_blocker_result
from openmd.core.detector import detect_platform
from openmd.core.models import ContentItem, TranscriptResult


class WechatMomentsAdapter(PlatformAdapter):
    """Resolve explicit Moments person selectors without reading local sessions."""

    def __init__(
        self,
        platform_id: str = "wechat_moments",
        default_backend: str = "opencli.wx.sns-feed",
    ) -> None:
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
        selector = parse_wechat_moments_selector(input_value)
        person = selector["person"]
        since = selector.get("since")
        until = selector.get("until")
        return [
            ContentItem(
                platform=self.id,
                input_type="collection",
                url=input_value.strip(),
                id=_feed_id(person, since=since, until=until),
                title=f"WeChat Moments: {person}",
                source_type="conversation",
                raw_meta={
                    "profile": "conversation",
                    "permission_scope": "user_accessible",
                    "target_kind": "wechat_moments_person",
                    "person": person,
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
        if selected in {"mock", "wechat_moments.mock"}:
            posts = _posts_from_meta(item.raw_meta)
            return TranscriptResult(
                text=render_wechat_moments_markdown(posts),
                status="verified",
                source_level="native_text",
                backend=selected,
                title=item.title or "WeChat Moments feed",
                source_notes=[
                    "local mock adapter smoke only; no WeChat database, session, OpenCLI, account, or decryption action was attempted",
                ],
                metadata={"profile": "conversation", "messages": posts},
            )
        return high_privacy_blocker_result(
            platform=self.id,
            backend=selected,
            target_kind=str(item.raw_meta.get("target_kind") or "wechat_moments_person"),
            state="refresh_needed",
            error_code="refresh_needed",
            error_summary=(
                "Refresh or provide an approved scoped WeChat Moments export/cache before running real acquisition."
            ),
            next_action=(
                "Use an explicitly approved export/cache or rerun collection with an enabled, scoped browser/backend "
                "after refreshing local WeChat data."
            ),
            permission_scope=str(item.raw_meta.get("permission_scope") or "unknown"),
            title=item.title or "WeChat Moments feed",
            source_notes=[
                "wechat moments adapter resolved the explicit person selector only; real acquisition is behind gated discovery/export providers",
                f"allow_fallback={allow_fallback}",
            ],
        )


def parse_wechat_moments_selector(value: str) -> dict[str, str | None]:
    parsed = urlparse(value.strip())
    if parsed.scheme in {"wx", "wechat"} and parsed.netloc.lower() in {"moments", "sns"}:
        person = unquote(parsed.path.strip("/"))
    elif parsed.scheme in {"wx-moments", "wechat-moments"}:
        person = unquote((parsed.netloc or parsed.path.strip("/")).strip("/"))
    else:
        raise ValueError("WeChat Moments selector must use wx://moments/<person>.")

    if not person or person in {"*", "all", "friends", "timeline"}:
        raise ValueError("WeChat Moments selector must name one explicit person.")

    query = parse_qs(parsed.query)
    return {
        "person": person,
        "since": _first_query_value(query, "since"),
        "until": _first_query_value(query, "until"),
    }


def render_wechat_moments_markdown(posts: list[dict[str, Any]]) -> str:
    lines = ["# WeChat Moments feed", ""]
    for post in posts:
        timestamp = str(post.get("timestamp") or "unknown-time")
        speaker = str(post.get("speaker") or post.get("author") or "unknown")
        text = str(post.get("text") or "").strip()
        if not text:
            text = "_No text content._"
        lines.append(f"- [{timestamp}] {speaker}: {text}")
        media = post.get("attachments")
        if isinstance(media, list) and media:
            lines.append(f"  - Attachments: {len(media)} referenced item(s)")
    return "\n".join(lines).strip() + "\n"


def _posts_from_meta(raw_meta: dict[str, Any]) -> list[dict[str, Any]]:
    value = raw_meta.get("messages") if isinstance(raw_meta, dict) else None
    if isinstance(value, list) and value:
        return [post for post in value if isinstance(post, dict)]
    person = str(raw_meta.get("person") or "mock-person")
    return [
        {
            "id": "moment_001",
            "timestamp": "2026-06-23T12:00:00+08:00",
            "speaker": person,
            "text": "Mock Moments post scoped to one person.",
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


def _feed_id(person: str, *, since: str | None, until: str | None) -> str:
    return hashlib.sha256(f"{person}:{since or ''}:{until or ''}".encode("utf-8")).hexdigest()[:12]
