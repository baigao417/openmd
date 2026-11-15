"""Platform adapter interface and skeleton adapter implementation."""

from __future__ import annotations

from abc import ABC, abstractmethod

from openmd.core.detector import detect_platform, extract_first_url
from openmd.core.models import ContentItem, TranscriptResult


class PlatformAdapter(ABC):
    id: str

    @abstractmethod
    def can_handle(self, input_value: str) -> bool:
        raise NotImplementedError

    @abstractmethod
    def resolve_items(
        self, input_value: str, *, limit: int | None = None, all: bool = False
    ) -> list[ContentItem]:
        raise NotImplementedError

    @abstractmethod
    def extract_text(
        self,
        item: ContentItem,
        *,
        backend: str | None = None,
        backend_chain: tuple[str, ...] | None = None,
        allow_fallback: bool = True,
    ) -> TranscriptResult:
        raise NotImplementedError


class StubPlatformAdapter(PlatformAdapter):
    """Read-only adapter used until real platform backends are wired."""

    def __init__(self, platform_id: str, default_backend: str) -> None:
        self.id = platform_id
        self.default_backend = default_backend

    def can_handle(self, input_value: str) -> bool:
        return detect_platform(input_value) == self.id

    def resolve_items(
        self, input_value: str, *, limit: int | None = None, all: bool = False
    ) -> list[ContentItem]:
        input_type = "collection" if limit is not None or all else "single"
        source_type = "video" if self.id in {"bilibili", "youtube", "douyin", "tiktok"} else "article"
        if self.id in {"xiaoyuzhou"}:
            source_type = "audio"
        if self.id in {"feishu"}:
            source_type = "document"
        return [
            ContentItem(
                platform=self.id,
                input_type=input_type,
                url=extract_first_url(input_value),
                source_type=source_type,
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
        chain = backend_chain or (backend or self.default_backend,)
        if not allow_fallback:
            chain = chain[:1]
        selected_backend = chain[0] if chain else self.default_backend
        if selected_backend == "mock":
            return TranscriptResult(
                text="[00:00:00] Mock transcript for Openmd skeleton.\n",
                status="verified",
                source_level="third_party_extractor",
                backend="mock",
                source_notes=["local smoke check only"],
            )
        return TranscriptResult(
            text="",
            status="failed",
            source_level="partial_extraction",
            backend=selected_backend,
            source_notes=[
                "adapter skeleton only; real extraction not implemented",
                f"backend_chain: {' -> '.join(chain)}",
            ],
            error_code="backend_not_implemented",
            error_summary="Openmd V0 skeleton routed the item but did not run a platform extractor.",
        )
