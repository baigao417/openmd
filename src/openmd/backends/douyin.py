"""Douyin bounded runtime discovery provider.

This platform extension is intentionally narrow: it only registers a read-only,
bounded OpenCLI discovery provider when browser-context backends are explicitly
enabled. It does not download media, run ASR, resolve short links, or perform
account actions.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable
from dataclasses import replace
from typing import Any
from urllib.parse import parse_qs, urlparse

from openmd.backends.base import (
    BackendCapability,
    BackendDescriptor,
    BackendExtractRequest,
    BackendHealth,
    DiscoveryRequest,
    DiscoveryResult,
)
from openmd.backends.executor import BackendProviderRegistry, DiscoveryProviderRegistry
from openmd.backends.registry import BackendRuntimeOptions
from openmd.backends.youtube_discovery import _run_opencli
from openmd.core.detector import extract_first_url
from openmd.core.models import ContentItem, TranscriptResult
from openmd.core.secrets import sanitize_error_summary


DOUYIN_OPENCLI_USER_VIDEOS_BACKEND = "opencli.douyin.user-videos"
DOUYIN_OPENMD_CREATOR_VIDEOS_BACKEND = "opencli.openmd-douyin.creator-videos"
DOUYIN_METADATA_BACKEND = "douyin.metadata"
DEFAULT_OPENCLI_DOUYIN_LIMIT = 20
MAX_OPENCLI_DOUYIN_LIMIT = 20

_SEC_UID_RE = re.compile(r"^[A-Za-z0-9_.-]{6,}$")
_AWEME_ID_RE = re.compile(r"^\d{6,}$")
_URL_CANDIDATE_RE = re.compile(r"https?://[^\s|,)\]]+")
_SENSITIVE_DOUYIN_TERMS = (
    "raw_response",
    "raw_stdout",
    "raw_stderr",
    "play_url",
    "download_url",
    "top_comments",
    "transcript_candidates",
)

OpenCliRunner = Callable[..., tuple[int, str, str]]


def register_backend_providers(
    registry: BackendProviderRegistry,
    options: BackendRuntimeOptions,
) -> None:
    requested = options.backend_override
    if requested in {None, DOUYIN_METADATA_BACKEND}:
        registry.register(DOUYIN_METADATA_BACKEND, DouyinMetadataProvider())


def register_discovery_providers(
    registry: DiscoveryProviderRegistry,
    options: BackendRuntimeOptions,
) -> None:
    """Register Douyin OpenCLI discovery only behind the browser runtime gate."""
    requested = options.requested_discovery_provider
    if options.enable_browser_backends and requested in {None, DOUYIN_OPENCLI_USER_VIDEOS_BACKEND}:
        registry.register(
            DOUYIN_OPENCLI_USER_VIDEOS_BACKEND,
            OpenCliDouyinUserVideosProvider(),
            platforms=("douyin",),
            default_for_platforms=requested is None,
        )
    if options.enable_browser_backends and requested == DOUYIN_OPENMD_CREATOR_VIDEOS_BACKEND:
        registry.register(
            DOUYIN_OPENMD_CREATOR_VIDEOS_BACKEND,
            OpenCliOpenmdDouyinCreatorVideosProvider(),
            platforms=("douyin",),
        )


class DouyinMetadataProvider:
    """Package bounded Douyin discovery metadata without downloading media."""

    name = DOUYIN_METADATA_BACKEND

    def descriptor(self) -> BackendDescriptor:
        return BackendDescriptor(
            name=self.name,
            layer="local",
            capabilities=(
                BackendCapability(
                    platforms=("douyin",),
                    source_types=("video",),
                    input_types=("single", "collection"),
                    source_levels=("third_party_extractor",),
                    requires_network=False,
                    requires_credentials=False,
                ),
            ),
            description="Create a read-only Douyin metadata package from discovered items.",
            provider="openmd.backends.douyin",
        )

    def health(self) -> BackendHealth:
        return BackendHealth(ok=True, dependencies={self.name: "ok"})

    def extract(self, request: BackendExtractRequest) -> TranscriptResult:
        item = request.item
        if item.platform != "douyin":
            return _metadata_failure(
                "invalid_input",
                f"Douyin metadata provider only supports douyin, got {item.platform}.",
                item=item,
            )
        if not item.id and not item.title:
            return _metadata_failure(
                "douyin_metadata_missing",
                "Douyin metadata package requires a discovered video id or title.",
                item=item,
            )

        title = item.title or item.id or "Douyin video"
        lines = [
            f"# {title}",
            "",
            "## Source",
            "",
            "- Platform: douyin",
            f"- URL: {item.url}",
        ]
        if item.id:
            lines.append(f"- Video ID: {item.id}")
        if item.author:
            lines.append(f"- Author: {item.author}")
        if item.published_at:
            lines.append(f"- Published: {item.published_at}")

        safe_fields = _safe_metadata_fields(item.raw_meta)
        if safe_fields:
            lines.extend(["", "## Discovered metadata", ""])
            for key, value in safe_fields.items():
                lines.append(f"- {key}: {value}")

        lines.extend(
            [
                "",
                "## Acquisition note",
                "",
                (
                    "This package contains bounded read-only Douyin discovery metadata only. "
                    "Openmd did not download media, capture comments, run ASR, bypass anti-bot "
                    "controls, or mutate any account state."
                ),
                "",
            ]
        )

        return TranscriptResult(
            text="\n".join(lines),
            status="verified",
            source_level="third_party_extractor",
            backend=self.name,
            source_notes=[
                "Douyin package contains bounded discovery metadata only.",
                "No media download, comments capture, ASR, anti-bot bypass, or account mutation was attempted.",
            ],
            title=title,
            metadata={
                "profile": "media",
                "metadata_only": True,
                "content_id": item.id,
                "collection_url": safe_fields.get("collection_url"),
            },
        )


def _safe_metadata_fields(raw_meta: dict[str, Any]) -> dict[str, str]:
    allowed = (
        "caption_status",
        "process_status",
        "duration",
        "like_count",
        "collection_url",
        "discovery_provider",
    )
    safe: dict[str, str] = {}
    for key in allowed:
        value = raw_meta.get(key)
        if value is None or value == "":
            continue
        safe[key] = sanitize_error_summary(str(value))
    return safe


def _metadata_failure(
    code: str,
    error: object,
    *,
    item: ContentItem,
) -> TranscriptResult:
    return TranscriptResult(
        text="",
        status="failed",
        source_level="partial_extraction",
        backend=DOUYIN_METADATA_BACKEND,
        source_notes=["Douyin metadata provider returned structured failure."],
        error_code=code,
        error_summary=sanitize_error_summary(error),
        title=item.title or item.id or "Douyin video",
        metadata={"profile": "media", "content_id": item.id},
    )


def normalize_douyin_collection_url(input_value: str) -> str | None:
    """Return a safe normalized Douyin user URL or short-link URL for diagnostics."""
    sec_uid = douyin_user_target(input_value)
    if sec_uid:
        return f"https://www.douyin.com/user/{sec_uid}"

    value = extract_first_url(input_value).strip()
    parsed = _parse_url(value)
    host = parsed.netloc.lower()
    if _is_douyin_short_host(host):
        parts = _path_parts(parsed.path)
        if not parts:
            return None
        return f"https://v.douyin.com/{parts[0]}/"
    return None


def douyin_user_target(input_value: str) -> str | None:
    """Extract a Douyin ``sec_uid`` for bounded user-video discovery."""
    value = extract_first_url(input_value).strip()
    if _looks_like_sec_uid(value):
        return value

    parsed = _parse_url(value)
    host = parsed.netloc.lower()
    if not (_is_douyin_host(host) or _is_iesdouyin_host(host)):
        return None
    if _is_douyin_short_host(host):
        return None

    query = parse_qs(parsed.query)
    for key in ("sec_uid", "secUid"):
        values = query.get(key)
        if values:
            candidate = values[0].strip()
            if _looks_like_sec_uid(candidate):
                return candidate

    parts = _path_parts(parsed.path)
    for marker in ("user", "share/user"):
        marker_parts = marker.split("/")
        if parts[: len(marker_parts)] == marker_parts and len(parts) > len(marker_parts):
            candidate = parts[len(marker_parts)]
            if _looks_like_sec_uid(candidate):
                return candidate
    return None


def build_opencli_douyin_user_videos_args(sec_uid: str, *, limit: int | None = None) -> list[str]:
    """Build a bounded, read-only OpenCLI Douyin discovery command."""
    return [
        "opencli",
        "douyin",
        "user-videos",
        sec_uid,
        "--limit",
        str(_bounded_limit(limit)),
        "--with_comments",
        "false",
        "-f",
        "json",
    ]


def build_opencli_openmd_douyin_creator_videos_args(input_value: str) -> list[str]:
    """Build the project-owned full creator discovery command."""
    return [
        "opencli",
        "openmd-douyin",
        "creator-videos",
        extract_first_url(input_value).strip(),
        "--timeout",
        "1800",
        "--page_delay_ms",
        "800",
        "--request_timeout_ms",
        "30000",
        "-f",
        "json",
    ]


def classify_douyin_input_failure(input_value: str, *, platform: str = "douyin") -> str:
    """Classify unsupported Douyin/TikTok input before any browser command runs."""
    if platform == "tiktok":
        return "tiktok_not_supported"

    value = extract_first_url(input_value).strip()
    parsed = _parse_url(value)
    host = parsed.netloc.lower()
    parts = _path_parts(parsed.path)

    if _is_tiktok_host(host):
        return "tiktok_not_supported"
    if _is_douyin_short_host(host):
        return "douyin_short_link_resolution_required"
    if _is_douyin_host(host) or _is_iesdouyin_host(host):
        if parts[:1] == ["video"] or "modal_id" in parse_qs(parsed.query):
            return "douyin_single_video_not_supported_for_discovery"
        return "douyin_user_sec_uid_not_found"
    return "unsupported_platform"


def classify_douyin_discovery_failure(message: object, *, exit_code: int | None = None) -> str:
    """Map OpenCLI/browser failures to stable Douyin discovery error codes."""
    text = str(message or "").lower()
    if exit_code == 127 or "not found" in text and "opencli" in text:
        return "backend_dependency_missing"
    if "timed out" in text or "timeout" in text:
        return "opencli_douyin_user_videos_timeout"
    if any(term in text for term in ("login", "sign in", "cookie", "session", "authorization")):
        return "douyin_auth_or_browser_session_required"
    if any(term in text for term in ("captcha", "verify", "risk", "blocked", "风控", "验证")):
        return "douyin_request_blocked"
    if "sec_uid" in text or "user not found" in text or "unavailable" in text:
        return "douyin_user_not_found"
    if exit_code is not None:
        return f"backend_exit_{exit_code}"
    return "opencli_douyin_user_videos_failed"


def parse_opencli_douyin_user_videos(stdout: str) -> list[ContentItem]:
    """Parse OpenCLI ``douyin user-videos -f json`` output into safe items."""
    if not stdout.strip():
        return []
    payload = json.loads(stdout)
    rows = _json_video_rows(payload)
    items: list[ContentItem] = []
    seen: set[str] = set()
    for row in rows:
        item = _content_item_from_row(row)
        if item is None or not item.id or item.id in seen:
            continue
        seen.add(item.id)
        items.append(item)
    return items


def parse_opencli_openmd_douyin_creator_videos(stdout: str) -> list[ContentItem]:
    """Parse full creator rows while retaining private acquisition fields in memory."""
    if not stdout.strip():
        return []
    payload = json.loads(stdout)
    rows = _json_video_rows(payload)
    items: list[ContentItem] = []
    seen: set[str] = set()
    for row in rows:
        item = _content_item_from_row(row, include_transient=True)
        if item is None or not item.id or item.id in seen:
            continue
        seen.add(item.id)
        items.append(item)
    return items


class OpenCliDouyinUserVideosProvider:
    """Bounded OpenCLI fallback for public Douyin user-video discovery."""

    name = DOUYIN_OPENCLI_USER_VIDEOS_BACKEND

    def __init__(self, *, runner: OpenCliRunner | None = None, timeout: int = 120) -> None:
        self._runner = runner or _run_opencli
        self.timeout = timeout

    def discover(self, request: DiscoveryRequest) -> DiscoveryResult:
        normalized_url = normalize_douyin_collection_url(request.input_url)
        sec_uid = douyin_user_target(request.input_url)
        metadata: dict[str, Any] = {
            "platform": request.platform,
            "normalized_url": normalized_url,
        }

        if request.platform != "douyin" or not sec_uid:
            return self._failure(
                error_code=classify_douyin_input_failure(request.input_url, platform=request.platform),
                error_summary=_input_error_summary(request.input_url, platform=request.platform),
                source_notes=[
                    "OpenCLI Douyin user-videos discovery did not run because the input is unsupported.",
                    "Short links are not resolved by Openmd in this safe lane.",
                ],
                metadata=metadata,
            )

        if request.allow_all:
            return self._failure(
                error_code="douyin_discovery_all_not_supported",
                error_summary="OpenCLI Douyin user-videos discovery requires a bounded --limit; --all is not supported.",
                source_notes=["OpenCLI Douyin discovery did not run unbounded collection discovery."],
                metadata={**metadata, "sec_uid": sec_uid},
            )

        limit = _bounded_limit(request.limit)
        command = build_opencli_douyin_user_videos_args(sec_uid, limit=limit)
        metadata = {
            **metadata,
            "sec_uid": sec_uid,
            "limit": limit,
            "requires_browser_context": True,
        }

        try:
            exit_code, stdout, stderr = self._runner(command, timeout=self.timeout)
        except FileNotFoundError as exc:
            return self._failure(
                error_code="backend_dependency_missing",
                error_summary=exc,
                metadata=metadata,
            )
        except subprocess.TimeoutExpired as exc:
            return self._failure(
                error_code="opencli_douyin_user_videos_timeout",
                error_summary=exc,
                metadata=metadata,
            )
        except OSError as exc:
            return self._failure(
                error_code="backend_os_error",
                error_summary=exc,
                metadata=metadata,
            )

        if exit_code != 0:
            signal = stderr or stdout or f"OpenCLI exited with code {exit_code}."
            return self._failure(
                error_code=classify_douyin_discovery_failure(signal, exit_code=exit_code),
                error_summary=signal,
                source_notes=["OpenCLI Douyin user-videos returned a command failure."],
                metadata={**metadata, "exit_code": exit_code},
            )

        try:
            items = parse_opencli_douyin_user_videos(stdout)
        except (json.JSONDecodeError, ValueError) as exc:
            return self._failure(
                error_code="opencli_douyin_user_videos_invalid_json",
                error_summary=exc,
                source_notes=["OpenCLI Douyin user-videos stdout was not valid JSON rows."],
                metadata=metadata,
            )

        items = [
            replace(
                item,
                raw_meta={
                    **item.raw_meta,
                    "collection_url": normalized_url or request.input_url,
                    "discovery_provider": self.name,
                    "sec_uid": sec_uid,
                },
            )
            for item in items
        ]

        items = items[:limit]
        if not items:
            return self._failure(
                error_code="opencli_douyin_user_videos_empty",
                error_summary="OpenCLI Douyin user-videos returned no parseable video rows.",
                metadata=metadata,
            )

        return DiscoveryResult.success(
            provider=self.name,
            items=items,
            source_notes=[
                "OpenCLI Douyin user-videos discovery only; item extraction was not attempted.",
                "Discovery stayed bounded by --limit and requested comments=false.",
                "No media download, ASR, anti-bot bypass, or account mutation was attempted.",
                "Raw stdout/stderr, play URLs, and comments were not persisted by this provider.",
            ],
            metadata={**metadata, "item_count": len(items)},
        )

    def _failure(
        self,
        *,
        error_code: str,
        error_summary: object,
        source_notes: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> DiscoveryResult:
        return DiscoveryResult.failure(
            provider=self.name,
            error_code=error_code,
            error_summary=_safe_douyin_error(error_summary),
            source_notes=source_notes or ["OpenCLI Douyin discovery returned structured failure."],
            metadata=metadata or {},
        )


class OpenCliOpenmdDouyinCreatorVideosProvider:
    """Full read-only creator discovery through the project-owned OpenCLI plugin."""

    name = DOUYIN_OPENMD_CREATOR_VIDEOS_BACKEND

    def __init__(self, *, runner: OpenCliRunner | None = None, timeout: int = 1900) -> None:
        self._runner = runner or _run_opencli
        self.timeout = timeout

    def discover(self, request: DiscoveryRequest) -> DiscoveryResult:
        normalized_url = normalize_douyin_collection_url(request.input_url)
        metadata: dict[str, Any] = {
            "platform": request.platform,
            "normalized_url": normalized_url,
            "requires_browser_context": True,
            "full_discovery": True,
        }
        if request.platform != "douyin":
            return self._failure(
                error_code="unsupported_platform",
                error_summary="The full creator provider only supports Douyin.",
                metadata=metadata,
            )
        if not request.allow_all:
            return self._failure(
                error_code="douyin_full_discovery_requires_all",
                error_summary="The full creator provider requires the explicit --all collection contract.",
                metadata=metadata,
            )

        command = build_opencli_openmd_douyin_creator_videos_args(request.input_url)
        try:
            exit_code, stdout, stderr = self._runner(command, timeout=self.timeout)
        except FileNotFoundError as exc:
            return self._failure(
                error_code="backend_dependency_missing",
                error_summary=exc,
                metadata=metadata,
            )
        except subprocess.TimeoutExpired as exc:
            return self._failure(
                error_code="opencli_douyin_creator_videos_timeout",
                error_summary=exc,
                metadata=metadata,
            )
        except OSError as exc:
            return self._failure(
                error_code="backend_os_error",
                error_summary=exc,
                metadata=metadata,
            )

        if exit_code != 0:
            signal = stderr or stdout or f"OpenCLI exited with code {exit_code}."
            return self._failure(
                error_code=classify_douyin_discovery_failure(signal, exit_code=exit_code),
                error_summary=signal,
                metadata={**metadata, "exit_code": exit_code},
            )

        try:
            items = parse_opencli_openmd_douyin_creator_videos(stdout)
        except (json.JSONDecodeError, ValueError) as exc:
            return self._failure(
                error_code="opencli_douyin_creator_videos_invalid_json",
                error_summary=exc,
                metadata=metadata,
            )
        if not items:
            return self._failure(
                error_code="opencli_douyin_creator_videos_empty",
                error_summary="The full creator plugin returned no parseable videos.",
                metadata=metadata,
            )

        collection_url = normalized_url or request.input_url
        image_post_count = sum(item.source_type == "image_text" for item in items)
        items = [
            replace(
                item,
                raw_meta={
                    **item.raw_meta,
                    "collection_url": collection_url,
                    "discovery_provider": self.name,
                    "source_exhausted": True,
                },
            )
            for item in items
        ]
        pagination_metadata = {
            key: items[0].raw_meta.get(key)
            for key in (
                "page_count",
                "empty_page_count",
                "duplicate_page_count",
                "pagination_retry_count",
                "source_exhausted",
            )
            if items[0].raw_meta.get(key) is not None
        }
        return DiscoveryResult.success(
            provider=self.name,
            items=items,
            source_notes=[
                "The project-owned OpenCLI plugin paginated Douyin until has_more=false.",
                "Video and image-text works were classified explicitly; image posts may preserve spoken audio or creator text.",
                "Playback URLs and native transcript candidates remain transient in ContentItem.raw_meta.",
                "The persisted discovery manifest excludes transient acquisition fields.",
            ],
            metadata={
                **metadata,
                "item_count": len(items),
                "video_count": len(items) - image_post_count,
                "image_post_count": image_post_count,
                "source_exhausted": True,
                **pagination_metadata,
            },
        )

    def _failure(
        self,
        *,
        error_code: str,
        error_summary: object,
        metadata: dict[str, Any],
    ) -> DiscoveryResult:
        return DiscoveryResult.failure(
            provider=self.name,
            error_code=error_code,
            error_summary=_safe_douyin_error(error_summary),
            source_notes=["Full Douyin creator discovery returned a structured failure."],
            metadata=metadata,
        )


def _json_video_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        if _row_has_video_reference(payload):
            return [payload]
        for key in ("items", "videos", "data", "rows", "aweme_list", "awemeList"):
            value = payload.get(key)
            rows = _json_video_rows(value)
            if rows:
                return rows
    return []


def _content_item_from_row(
    row: dict[str, Any],
    *,
    include_transient: bool = False,
) -> ContentItem | None:
    normalized = {_normalize_key(key): value for key, value in row.items()}
    aweme_id = _first_present(normalized, "aweme_id", "awemeid", "video_id", "id")
    video_url = _canonical_douyin_video_url(_first_present(normalized, "url", "video_url", "share_url"))
    if video_url is None and aweme_id:
        video_url = _canonical_douyin_video_url(f"https://www.douyin.com/video/{aweme_id}")
    if aweme_id is None and video_url:
        aweme_id = _aweme_id_from_url(video_url)
    if video_url is None or aweme_id is None:
        return None

    title = _first_present(normalized, "title", "desc", "description", "caption")
    author = _first_present(normalized, "author", "nickname", "user", "user_name")
    raw_meta: dict[str, Any] = {
        "caption_status": "unknown",
        "process_status": "discovered",
    }
    media_kind = _first_present(normalized, "media_kind") or "video"
    source_type = "image_text" if media_kind == "image_text" else "video"
    raw_meta["media_kind"] = media_kind
    for key in ("aweme_type", "image_count"):
        value = row.get(key)
        if value is not None:
            raw_meta[key] = value
    duration = _first_present(normalized, "duration")
    if duration:
        raw_meta["duration"] = duration
    digg_count = _first_present(normalized, "digg_count", "like_count", "likes")
    if digg_count:
        raw_meta["like_count"] = digg_count
    if include_transient:
        play_url = _first_present(normalized, "play_url")
        if play_url:
            raw_meta["play_url"] = play_url
        transcript_candidates = row.get("transcript_candidates")
        if isinstance(transcript_candidates, list):
            raw_meta["transcript_candidates"] = [
                candidate
                for candidate in transcript_candidates
                if isinstance(candidate, dict)
            ]
        for key in (
            "page_count",
            "empty_page_count",
            "duplicate_page_count",
            "pagination_retry_count",
            "source_exhausted",
        ):
            value = row.get(key)
            if value is not None:
                raw_meta[key] = value
    published_at = _first_present(normalized, "published_at", "create_time", "date")

    return ContentItem(
        platform="douyin",
        input_type="collection",
        url=video_url,
        id=aweme_id,
        title=title,
        author=author,
        published_at=published_at,
        source_type=source_type,
        raw_meta=raw_meta,
    )


def _row_has_video_reference(row: dict[str, Any]) -> bool:
    normalized = {_normalize_key(key): value for key, value in row.items()}
    aweme_id = _first_present(normalized, "aweme_id", "awemeid", "video_id", "id")
    if aweme_id and _AWEME_ID_RE.match(aweme_id):
        return True
    return any(_canonical_douyin_video_url(str(value or "")) for value in row.values())


def _canonical_douyin_video_url(value: str | None) -> str | None:
    if not value:
        return None
    for candidate in _video_url_candidates(value):
        parsed = _parse_url(candidate)
        host = parsed.netloc.lower()
        parts = _path_parts(parsed.path)
        if (_is_douyin_host(host) or _is_iesdouyin_host(host)) and parts[:1] == ["video"] and len(parts) >= 2:
            aweme_id = parts[1]
            if _AWEME_ID_RE.match(aweme_id):
                return f"https://www.douyin.com/video/{aweme_id}"
    return None


def _aweme_id_from_url(value: str) -> str | None:
    parsed = _parse_url(value)
    parts = _path_parts(parsed.path)
    if parts[:1] == ["video"] and len(parts) >= 2 and _AWEME_ID_RE.match(parts[1]):
        return parts[1]
    return None


def _video_url_candidates(value: str) -> list[str]:
    candidates = [match.group(0) for match in _URL_CANDIDATE_RE.finditer(value)]
    if not candidates:
        candidates.append(value)
    return candidates


def _input_error_summary(input_value: str, *, platform: str) -> str:
    code = classify_douyin_input_failure(input_value, platform=platform)
    if code == "tiktok_not_supported":
        return "TikTok runtime provider is not implemented in this V0 lane."
    if code == "douyin_short_link_resolution_required":
        return "Douyin short links must be resolved to a user sec_uid/profile URL before bounded discovery can run."
    if code == "douyin_single_video_not_supported_for_discovery":
        return "Douyin single-video metadata extraction is not implemented in this safe discovery lane."
    if code == "douyin_user_sec_uid_not_found":
        return "Douyin user sec_uid was not found in the input URL."
    return "Input is not a supported Douyin user/profile URL."


def _safe_douyin_error(value: object) -> str:
    summary = sanitize_error_summary(value) or ""
    for term in _SENSITIVE_DOUYIN_TERMS:
        summary = re.sub(rf"(?i)\b{re.escape(term)}\b", "[redacted]", summary)
    return summary


def _bounded_limit(limit: int | None) -> int:
    if limit is None:
        return DEFAULT_OPENCLI_DOUYIN_LIMIT
    return max(0, min(limit, MAX_OPENCLI_DOUYIN_LIMIT))


def _looks_like_sec_uid(value: str) -> bool:
    return _SEC_UID_RE.match(value) is not None and "://" not in value and "/" not in value


def _parse_url(value: str) -> Any:
    return urlparse(value if "://" in value else f"https://{value}")


def _path_parts(path: str) -> list[str]:
    return [part for part in path.split("/") if part]


def _is_douyin_host(host: str) -> bool:
    return host == "douyin.com" or host.endswith(".douyin.com")


def _is_iesdouyin_host(host: str) -> bool:
    return host == "iesdouyin.com" or host.endswith(".iesdouyin.com")


def _is_douyin_short_host(host: str) -> bool:
    return host == "v.douyin.com"


def _is_tiktok_host(host: str) -> bool:
    return host == "tiktok.com" or host.endswith(".tiktok.com")


def _normalize_key(key: object) -> str:
    return str(key or "").strip().lower().replace(" ", "_").replace("-", "_")


def _first_present(row: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = row.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None
