"""V1 adapter for Douyin author/video package inputs."""

from __future__ import annotations

import hashlib
from urllib.parse import parse_qs, urlparse

from openmd.adapters.base import PlatformAdapter
from openmd.backends.douyin import douyin_user_target, normalize_douyin_collection_url
from openmd.backends.douyin_detail import douyin_video_id
from openmd.core.detector import detect_platform, extract_first_url
from openmd.core.models import ContentItem, TranscriptResult


class DouyinAdapter(PlatformAdapter):
    """Resolve Douyin single video and author selectors without side effects."""

    def __init__(self, platform_id: str = "douyin", default_backend: str = "douyin.transcript") -> None:
        self.id = platform_id
        self.default_backend = default_backend

    def can_handle(self, input_value: str) -> bool:
        return detect_platform(input_value) == "douyin"

    def resolve_items(
        self,
        input_value: str,
        *,
        limit: int | None = None,
        all: bool = False,
    ) -> list[ContentItem]:
        target = extract_first_url(input_value)
        aweme_id = douyin_video_id(target)
        if aweme_id is not None:
            return [
                ContentItem(
                    platform=self.id,
                    input_type="single",
                    url=f"https://www.douyin.com/video/{aweme_id}",
                    id=aweme_id,
                    source_type="video",
                    raw_meta={
                        "profile": "media",
                        "permission_scope": "user_accessible",
                        "content_id": aweme_id,
                    },
                )
            ]

        if (urlparse(target).hostname or "").lower() == "v.douyin.com":
            return [ContentItem(platform=self.id, input_type="single", url=target,
                                source_type="video", raw_meta={"profile": "media", "identity_unresolved": True})]

        sec_uid = douyin_user_target(target)
        normalized = normalize_douyin_collection_url(target) or target
        return [
            ContentItem(
                platform=self.id,
                input_type="collection",
                url=normalized,
                id=f"user-{sec_uid}" if sec_uid else _stable_id(target),
                source_type="video",
                raw_meta={
                    "profile": "media",
                    "permission_scope": "user_accessible",
                    "collection_selector": input_value.strip(),
                    "sec_uid": sec_uid,
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
        if selected in {"mock", "douyin.mock"}:
            return TranscriptResult(
                text="[00:00:00] Mock Douyin transcript for Openmd package smoke.\n",
                status="verified",
                source_level="platform_transcript",
                backend=selected,
                title=item.title or item.id or "Mock Douyin item",
                source_notes=["local mock adapter smoke only; no sidecar/OpenCLI/browser/account/media/ASR action was attempted"],
            )
        return TranscriptResult(
            text="",
            status="failed",
            source_level="partial_extraction",
            backend=selected,
            source_notes=[
                "douyin adapter resolved the item only; real acquisition is handled by gated discovery/backend providers",
                f"allow_fallback={allow_fallback}",
            ],
            error_code="backend_requires_runtime_gate",
            error_summary="Enable the appropriate Openmd backend gate before running real Douyin acquisition.",
        )


def _douyin_aweme_id(value: str) -> str | None:
    parsed = urlparse(value if "://" in value else f"https://{value}")
    parts = [part for part in parsed.path.split("/") if part]
    if parts[:1] == ["video"] and len(parts) >= 2 and parts[1].isdigit():
        return parts[1]
    query = parse_qs(parsed.query)
    modal = query.get("modal_id", [""])[0].strip()
    return modal if modal.isdigit() else None


def _stable_id(value: str) -> str:
    return hashlib.sha256(value.strip().encode("utf-8")).hexdigest()[:12]
