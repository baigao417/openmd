"""V1 adapter for Bilibili video/audio-like package inputs."""

from __future__ import annotations

import hashlib

from openmd.adapters.base import PlatformAdapter
from openmd.backends.bilibili import extract_bilibili_uid, extract_bvid
from openmd.core.detector import detect_platform, extract_first_url
from openmd.core.models import ContentItem, TranscriptResult


class BilibiliAdapter(PlatformAdapter):
    """Resolve Bilibili BV and UP/collection selectors without side effects."""

    def __init__(self, platform_id: str = "bilibili", default_backend: str = "bilibili-harvest.core") -> None:
        self.id = platform_id
        self.default_backend = default_backend

    def can_handle(self, input_value: str) -> bool:
        return detect_platform(input_value) == "bilibili"

    def resolve_items(
        self,
        input_value: str,
        *,
        limit: int | None = None,
        all: bool = False,
    ) -> list[ContentItem]:
        target = extract_first_url(input_value)
        bvid = extract_bvid(target)
        if bvid is not None:
            return [
                ContentItem(
                    platform=self.id,
                    input_type="single",
                    url=_bilibili_video_url(bvid),
                    id=bvid,
                    source_type="video",
                    raw_meta={
                        "profile": "media",
                        "permission_scope": "user_accessible",
                        "content_id": bvid,
                    },
                )
            ]

        uid = extract_bilibili_uid(target)
        return [
            ContentItem(
                platform=self.id,
                input_type="collection",
                url=f"https://space.bilibili.com/{uid}" if uid else target,
                id=f"space-{uid}" if uid else _stable_id(target),
                source_type="video",
                raw_meta={
                    "profile": "media",
                    "permission_scope": "user_accessible",
                    "collection_selector": input_value.strip(),
                    "up_uid": uid,
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
        if selected in {"mock", "bilibili.mock"}:
            return TranscriptResult(
                text="[00:00:00] Mock Bilibili transcript for Openmd package smoke.\n",
                status="verified",
                source_level="third_party_extractor",
                backend=selected,
                title=item.title or item.id or "Mock Bilibili item",
                source_notes=["local mock adapter smoke only; no network/browser/OpenCLI/media action was attempted"],
            )
        return TranscriptResult(
            text="",
            status="failed",
            source_level="partial_extraction",
            backend=selected,
            source_notes=[
                "bilibili adapter resolved the item only; real extraction is handled by gated backend providers",
                f"allow_fallback={allow_fallback}",
            ],
            error_code="backend_requires_runtime_gate",
            error_summary="Enable the appropriate Openmd backend gate before running real Bilibili extraction.",
        )


def _bilibili_video_url(bvid: str) -> str:
    return f"https://www.bilibili.com/video/{bvid}/"


def _stable_id(value: str) -> str:
    return hashlib.sha256(value.strip().encode("utf-8")).hexdigest()[:12]
