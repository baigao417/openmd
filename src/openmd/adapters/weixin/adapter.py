"""V1 adapter for WeChat Official Account article inputs.

The adapter is intentionally conservative: it resolves single article URLs and
author/account collection selectors into ``ContentItem`` records, but real
network/browser acquisition remains behind the backend/discovery gates.
"""

from __future__ import annotations

import hashlib
from urllib.parse import parse_qs, urlparse

from openmd.adapters.base import PlatformAdapter
from openmd.backends.weixin import normalize_weixin_url
from openmd.core.detector import detect_platform, extract_first_url
from openmd.core.models import ContentItem, TranscriptResult


WEIXIN_SELECTOR_SCHEMES: frozenset[str] = frozenset(
    {"weixin", "wechat", "wechat-official", "wx-official"}
)


class WeixinOfficialAccountAdapter(PlatformAdapter):
    """Resolve Weixin article and account selectors without side effects."""

    def __init__(self, platform_id: str = "weixin", default_backend: str = "weixin.article.local") -> None:
        self.id = platform_id
        self.default_backend = default_backend

    def can_handle(self, input_value: str) -> bool:
        return _is_weixin_article_url(input_value) or _is_weixin_collection_selector(input_value)

    def resolve_items(
        self,
        input_value: str,
        *,
        limit: int | None = None,
        all: bool = False,
    ) -> list[ContentItem]:
        normalized = normalize_weixin_url(extract_first_url(input_value))
        if _is_weixin_article_url(normalized):
            return [_article_item(normalized, platform=self.id)]

        return [
            ContentItem(
                platform=self.id,
                input_type="collection",
                url=input_value.strip(),
                id=_stable_id(input_value),
                source_type="article",
                raw_meta={
                    "profile": "article",
                    "permission_scope": "user_accessible",
                    "collection_selector": input_value.strip(),
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
        if selected in {"mock", "weixin.mock"}:
            return TranscriptResult(
                text="# Mock Weixin article\n\nMock official account article content.\n",
                status="verified",
                source_level="native_text",
                backend=selected,
                title=item.title or "Mock Weixin article",
                source_notes=["local mock adapter smoke only; no network/browser/account action was attempted"],
            )
        return TranscriptResult(
            text="",
            status="failed",
            source_level="partial_extraction",
            backend=selected,
            source_notes=[
                "weixin adapter resolved the item only; real extraction is handled by gated backend providers",
                f"allow_fallback={allow_fallback}",
            ],
            error_code="backend_requires_runtime_gate",
            error_summary="Enable the appropriate Openmd backend gate before running real Weixin extraction.",
        )


def _article_item(url: str, *, platform: str) -> ContentItem:
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    title = query.get("title", [None])[0]
    return ContentItem(
        platform=platform,
        input_type="single",
        url=url,
        id=_weixin_article_id(url),
        title=title,
        source_type="article",
        raw_meta={
            "profile": "article",
            "permission_scope": "user_accessible",
            "content_id": _weixin_article_id(url),
        },
    )


def _is_weixin_article_url(input_value: str) -> bool:
    normalized = normalize_weixin_url(extract_first_url(input_value))
    parsed = urlparse(normalized)
    return (
        parsed.scheme in {"http", "https"}
        and parsed.netloc.lower() == "mp.weixin.qq.com"
        and (parsed.path == "/s" or parsed.path.startswith("/s/"))
    )


def _is_weixin_collection_selector(input_value: str) -> bool:
    value = input_value.strip()
    parsed = urlparse(value)
    if parsed.scheme in WEIXIN_SELECTOR_SCHEMES:
        return bool(parsed.netloc or parsed.path)
    return detect_platform(value) == "weixin"


def _weixin_article_id(url: str) -> str:
    normalized = normalize_weixin_url(url)
    parsed = urlparse(normalized)
    if parsed.path.startswith("/s/"):
        slug = parsed.path.removeprefix("/s/").strip("/")
        if slug:
            return slug
    query = parse_qs(parsed.query)
    biz = query.get("__biz", [""])[0]
    mid = query.get("mid", [""])[0]
    idx = query.get("idx", [""])[0]
    if biz and mid and idx:
        return f"{biz}-{mid}-{idx}"
    return _stable_id(normalized)


def _stable_id(value: str) -> str:
    return hashlib.sha256(value.strip().encode("utf-8")).hexdigest()[:12]
