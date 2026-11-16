"""Input resolution only. Extraction belongs to runtime providers."""
from __future__ import annotations
from openmd.core.detector import extract_first_url
from openmd.core.models import ContentItem
from .base import StubPlatformAdapter

def _source_type_for(platform: str) -> str:
    if platform in {"bilibili", "youtube", "douyin", "tiktok", "xiaoe"}:
        return "video"
    if platform == "xiaoyuzhou":
        return "audio"
    if platform == "feishu":
        return "document"
    if platform == "xiaohongshu":
        return "mixed"
    return "article"



class OpenCliPlatformAdapter(StubPlatformAdapter):
    def __init__(self, platform_id: str, default_backend: str) -> None:
        self.id = platform_id
        self.default_backend = default_backend


    def can_handle(self, input_value: str) -> bool:
        from openmd.core.detector import detect_platform

        return detect_platform(input_value) == self.id


    def resolve_items(
        self, input_value: str, *, limit: int | None = None, all: bool = False
    ) -> list[ContentItem]:
        input_type = "collection" if limit is not None or all else "single"
        return [
            ContentItem(
                platform=self.id,
                input_type=input_type,
                url=extract_first_url(input_value),
                source_type=_source_type_for(self.id),  # type: ignore[arg-type]
                raw_meta={"limit": limit},
            )
        ]
