"""Xiaohongshu adapter declaring the multimodal extraction contract before runtime skip checks."""

from __future__ import annotations

import hashlib
import re
from urllib.parse import urlparse

from openmd.adapters.base import PlatformAdapter
from openmd.backends.xiaohongshu import has_required_signed_query, normalize_xiaohongshu_url, xiaohongshu_user_id
from openmd.backends.xiaohongshu_providers import XIAOHONGSHU_COMPOSITE_BACKEND
from openmd.core.detector import detect_platform, extract_first_url
from openmd.core.models import ContentItem, ExtractionComponent, TranscriptResult
from openmd.core.package_contract import XIAOHONGSHU_EXTRACTION_CONTRACT_VERSION
from openmd.core.secrets import sanitize_url


_NOTE_ID_RE = re.compile(r"^[0-9a-f]{24}$", re.I)
_ALL_COMPONENTS = ["native_text", "image_ocr", "video_transcript"]


class XiaohongshuAdapter(PlatformAdapter):
    def __init__(self, platform_id: str = "xiaohongshu", default_backend: str = XIAOHONGSHU_COMPOSITE_BACKEND) -> None:
        self.id = platform_id
        self.default_backend = default_backend

    def can_handle(self, input_value: str) -> bool:
        return detect_platform(input_value) == "xiaohongshu"

    def resolve_items(
        self,
        input_value: str,
        *,
        limit: int | None = None,
        all: bool = False,
    ) -> list[ContentItem]:
        target = normalize_xiaohongshu_url(extract_first_url(input_value))
        note_id = _note_id_from_url(target)
        is_short_note = _is_xhslink_url(target)
        if note_id or is_short_note:
            content_id = note_id or _stable_id(target)
            profile_id = _profile_id_from_note_url(target)
            raw_meta: dict[str, object] = {
                "permission_scope": "user_accessible",
                "content_id": content_id,
                "media_kind": "mixed",
                "extraction_contract_version": XIAOHONGSHU_EXTRACTION_CONTRACT_VERSION,
                "required_components": list(_ALL_COMPONENTS),
            }
            if profile_id:
                raw_meta["profile_id"] = profile_id
            if has_required_signed_query(target):
                raw_meta["runtime_note_url"] = target
            if is_short_note:
                raw_meta["runtime_selector"] = target
                raw_meta["identity_unresolved"] = True
            return [
                ContentItem(
                    platform=self.id,
                    input_type="single",
                    url=(
                        f"https://www.xiaohongshu.com/discovery/item/{note_id}"
                        if note_id
                        else sanitize_url(target)
                    ),
                    id=content_id,
                    source_type="mixed",
                    raw_meta=raw_meta,
                )
            ]
        profile_id = xiaohongshu_user_id(target)
        return [
            ContentItem(
                platform=self.id,
                input_type="collection",
                url=sanitize_url(target),
                id=f"user-{profile_id}" if profile_id else _stable_id(target),
                source_type="mixed",
                raw_meta={
                    "permission_scope": "user_accessible",
                    "profile_id": profile_id,
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
        if selected == "mock":
            return TranscriptResult(
                text="## Native Note Text\n\nMock Xiaohongshu note.\n\n## Image OCR\n\n_Not applicable._\n\n## Video Transcript\n\n_Not applicable._\n",
                status="verified",
                source_level="native_text",
                backend="mock",
                title=item.title or item.id or "Mock Xiaohongshu note",
                extraction_contract_version=XIAOHONGSHU_EXTRACTION_CONTRACT_VERSION,
                components=[
                    ExtractionComponent(
                        "native_text", "native_text", "verified", "native_text", "mock",
                        text_ref="index.md#native-note-text",
                    ),
                    ExtractionComponent(
                        "image_ocr", "image_ocr", "not_applicable", "partial_extraction", "mock",
                        text_ref="index.md#image-ocr",
                    ),
                    ExtractionComponent(
                        "video_transcript", "video_transcript", "not_applicable", "partial_extraction", "mock",
                        text_ref="index.md#video-transcript",
                    ),
                ],
            )
        return TranscriptResult(
            text="",
            status="failed",
            source_level="partial_extraction",
            backend=selected,
            error_code="backend_requires_runtime_gate",
            error_summary="Enable the browser backend gate before running Xiaohongshu extraction.",
            source_notes=[f"allow_fallback={allow_fallback}"],
            extraction_contract_version=XIAOHONGSHU_EXTRACTION_CONTRACT_VERSION,
        )


def create_adapter(platform_id: str, _default_backend: str) -> PlatformAdapter:
    return XiaohongshuAdapter(platform_id, XIAOHONGSHU_COMPOSITE_BACKEND)


def _note_id_from_url(value: str) -> str | None:
    parsed = urlparse(value)
    parts = [part for part in parsed.path.split("/") if part]
    candidate = parts[-1] if parts else ""
    return candidate.lower() if _NOTE_ID_RE.fullmatch(candidate) else None


def _profile_id_from_note_url(value: str) -> str | None:
    parsed = urlparse(value)
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) >= 4 and parts[:2] == ["user", "profile"]:
        return parts[2]
    return None


def _is_xhslink_url(value: str) -> bool:
    host = urlparse(value).netloc.lower()
    return host == "xhslink.com" or host.endswith(".xhslink.com")


def _stable_id(value: str) -> str:
    return hashlib.sha256(value.strip().encode("utf-8")).hexdigest()[:12]
