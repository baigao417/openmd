"""youtube-transcript-api backend provider.

This provider is intentionally opt-in because it performs network requests when
enabled. The default CLI keeps using skeleton providers unless the operator
explicitly enables real network backends.
"""

from __future__ import annotations

import importlib.util
from collections.abc import Callable, Iterable
from typing import Any
from urllib.parse import parse_qs, urlparse

from openmd.core.models import ContentItem, TranscriptResult
from openmd.core.secrets import sanitize_error_summary

from .base import BackendCapability, BackendDescriptor, BackendExtractRequest, BackendHealth


YOUTUBE_TRANSCRIPT_BACKEND = "youtube-transcript-api"
DEFAULT_LANGUAGES: tuple[str, ...] = ("zh-Hans", "zh-CN", "zh", "en")


def extract_youtube_video_id(input_value: str) -> str | None:
    """Extract a YouTube video id from common URL forms or a raw id."""
    value = input_value.strip()
    if not value:
        return None
    if "://" not in value and "/" not in value and "?" not in value:
        return value

    parsed = urlparse(value if "://" in value else f"https://{value}")
    host = parsed.netloc.lower()
    path_parts = [part for part in parsed.path.split("/") if part]

    if host.endswith("youtu.be") and path_parts:
        return path_parts[0]

    query_video_id = parse_qs(parsed.query).get("v")
    if query_video_id and query_video_id[0]:
        return query_video_id[0]

    if path_parts and path_parts[0] in {"embed", "shorts", "live"} and len(path_parts) >= 2:
        return path_parts[1]

    return None


class YouTubeTranscriptApiProvider:
    """Fetch YouTube-native transcripts through youtube-transcript-api."""

    name = YOUTUBE_TRANSCRIPT_BACKEND

    def __init__(
        self,
        *,
        languages: tuple[str, ...] = DEFAULT_LANGUAGES,
        api_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.languages = languages
        self._api_factory = api_factory

    def descriptor(self) -> BackendDescriptor:
        capability = BackendCapability(
            platforms=("youtube",),
            source_types=("video",),
            input_types=("single",),
            source_levels=("native_caption", "auto_caption"),
            requires_network=True,
            requires_credentials=False,
            optional_config=("languages",),
        )
        return BackendDescriptor(
            name=self.name,
            layer="L1 specialized extractor",
            capabilities=(capability,),
            description="Fetch existing YouTube manual or generated transcripts.",
            provider="youtube-transcript-api",
        )

    def health(self) -> BackendHealth:
        if self._api_factory is not None:
            return BackendHealth(ok=True, dependencies={"youtube-transcript-api": "injected"})
        if importlib.util.find_spec("youtube_transcript_api") is None:
            return BackendHealth(
                ok=False,
                dependencies={"youtube-transcript-api": "missing"},
                error_code="backend_dependency_missing",
                error_summary="youtube-transcript-api is not installed.",
            )
        return BackendHealth(ok=True, dependencies={"youtube-transcript-api": "available"})

    def extract(self, request: BackendExtractRequest) -> TranscriptResult:
        item = request.item
        if item.platform != "youtube":
            return _failed(
                item,
                "unsupported_platform",
                f"youtube-transcript-api only supports youtube, got {item.platform}.",
            )

        video_id = item.id or extract_youtube_video_id(item.url)
        if not video_id:
            return _failed(item, "invalid_input", "Could not extract a YouTube video id.")

        try:
            api = self._create_api()
            fetched_transcript = api.fetch(video_id, languages=list(self.languages))
        except ImportError as exc:
            return _failed(item, "backend_dependency_missing", exc, video_id=video_id)
        except Exception as exc:
            return _failed(item, _error_code_for_exception(exc), exc, video_id=video_id)

        text = _format_transcript(fetched_transcript)
        if not text.strip():
            return _failed(item, "empty_result", "youtube-transcript-api returned no transcript text.", video_id=video_id)

        is_generated = bool(getattr(fetched_transcript, "is_generated", False))
        language = getattr(fetched_transcript, "language", None)
        language_code = getattr(fetched_transcript, "language_code", None)
        source_level = "auto_caption" if is_generated else "native_caption"
        notes = [
            "youtube-transcript-api fetched an existing YouTube transcript.",
            f"language_code: {language_code or 'unknown'}",
            "caption_type: generated" if is_generated else "caption_type: manual_or_official",
        ]
        return TranscriptResult(
            text=text,
            status="verified",
            source_level=source_level,
            backend=self.name,
            source_notes=notes,
            metadata={
                "video_id": video_id,
                "language": language,
                "language_code": language_code,
                "is_generated": is_generated,
            },
        )

    def _create_api(self) -> Any:
        if self._api_factory is not None:
            return self._api_factory()
        try:
            from youtube_transcript_api import YouTubeTranscriptApi
        except ImportError as exc:
            raise ImportError("youtube-transcript-api is not installed.") from exc
        return YouTubeTranscriptApi()


def _format_transcript(fetched_transcript: Iterable[Any]) -> str:
    lines: list[str] = []
    for snippet in fetched_transcript:
        text = str(_snippet_value(snippet, "text", "")).replace("\r", " ").replace("\n", " ").strip()
        if not text:
            continue
        start = _snippet_value(snippet, "start", 0.0)
        lines.append(f"[{_format_timestamp(start)}] {text}")
    return "\n".join(lines) + ("\n" if lines else "")


def _snippet_value(snippet: Any, key: str, default: Any) -> Any:
    if isinstance(snippet, dict):
        return snippet.get(key, default)
    return getattr(snippet, key, default)


def _format_timestamp(start_seconds: Any) -> str:
    try:
        total_seconds = max(0, int(float(start_seconds)))
    except (TypeError, ValueError):
        total_seconds = 0
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def _error_code_for_exception(exc: Exception) -> str:
    name = exc.__class__.__name__.lower()
    if "requestblocked" in name:
        return "youtube_request_blocked"
    if "ipblocked" in name:
        return "youtube_ip_blocked"
    if "transcriptsdisabled" in name:
        return "youtube_transcripts_disabled"
    if "notranscriptfound" in name:
        return "youtube_transcript_not_found"
    if "videounavailable" in name:
        return "youtube_video_unavailable"
    return "youtube_transcript_api_error"


def _failed(
    item: ContentItem,
    code: str,
    error: object,
    *,
    video_id: str | None = None,
) -> TranscriptResult:
    metadata = {"platform": item.platform}
    if video_id:
        metadata["video_id"] = video_id
    return TranscriptResult(
        text="",
        status="failed",
        source_level="partial_extraction",
        backend=YOUTUBE_TRANSCRIPT_BACKEND,
        source_notes=["youtube-transcript-api returned structured failure"],
        error_code=code,
        error_summary=sanitize_error_summary(error),
        metadata=metadata,
    )
