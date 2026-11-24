"""Resolve supported Zhihu item links without network or browser side effects."""
from __future__ import annotations

from urllib.parse import parse_qs, unquote, urlparse

from openmd.adapters.base import PlatformAdapter
from openmd.core.detector import detect_platform, extract_first_url
from openmd.core.models import ContentItem, TranscriptResult


def normalize_zhihu_url(value: str) -> str | None:
    url = extract_first_url(str(value or "").strip())
    parsed = urlparse(url if "://" in url else "https://" + url)
    if parsed.hostname and parsed.hostname.lower() == "link.zhihu.com":
        target = parse_qs(parsed.query).get("target", [""])[0]
        if not target:
            return None
        url = unquote(target)
        parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or not (host == "zhihu.com" or host.endswith(".zhihu.com")):
        return None
    parts = [part for part in parsed.path.split("/") if part]
    canonical = ""
    if len(parts) == 2 and parts[0] == "question" and parts[1].isdigit():
        canonical = f"https://www.zhihu.com/question/{parts[1]}"
    elif len(parts) == 4 and parts[0] == "question" and parts[1].isdigit() and parts[2] == "answer" and parts[3].isdigit():
        canonical = f"https://www.zhihu.com/question/{parts[1]}/answer/{parts[3]}"
    elif host == "zhuanlan.zhihu.com" and len(parts) == 2 and parts[0] == "p" and parts[1].isdigit():
        canonical = f"https://zhuanlan.zhihu.com/p/{parts[1]}"
    elif len(parts) == 2 and parts[0] in {"pin", "collection"} and parts[1].isdigit():
        canonical = f"https://www.zhihu.com/{parts[0]}/{parts[1]}"
    return canonical or None


class ZhihuAdapter(PlatformAdapter):
    id = "zhihu"

    def __init__(self, platform_id: str = "zhihu", default_backend: str = "opencli.zhihu.read") -> None:
        self.id, self.default_backend = platform_id, default_backend

    def can_handle(self, input_value: str) -> bool:
        return detect_platform(input_value) == self.id

    def resolve_items(self, input_value: str, *, limit: int | None = None, all: bool = False) -> list[ContentItem]:
        url = normalize_zhihu_url(input_value)
        if not url:
            return []
        path = urlparse(url).path
        kind = "answer" if "/answer/" in path else path.split("/")[1]
        source_type = "article"
        return [ContentItem(platform=self.id, input_type="single", url=url, id=path.rstrip("/").split("/")[-1],
                            source_type=source_type, raw_meta={"zhihu_kind": kind, "all_answers": True,
                                                               "answer_limit": 1000, "time_limit_seconds": 300})]

    def extract_text(self, item: ContentItem, *, backend: str | None = None, backend_chain: tuple[str, ...] | None = None,
                     allow_fallback: bool = True) -> TranscriptResult:
        selected = (backend_chain or (backend or self.default_backend,))[0]
        return TranscriptResult(text="", status="failed", source_level="partial_extraction", backend=selected,
                                error_code="backend_requires_runtime_gate",
                                error_summary="Enable the browser backend gate before reading a Zhihu page.")


def create_adapter(platform_id: str, default_backend: str) -> PlatformAdapter:
    return ZhihuAdapter(platform_id, default_backend)
