"""Xiaoe adapters discover media/content; core owns media download and ASR."""

from __future__ import annotations

import json
import re
import time
from dataclasses import replace
from urllib.parse import urlsplit
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openmd.core.content_validation import validate_extraction_content
from openmd.core.opencli_output import load_opencli_json
from openmd.core.models import ContentItem, TranscriptResult
from openmd.core.secrets import sanitize_error_summary, sanitize_asr_execution

from .base import (
    BackendCapability,
    BackendDescriptor,
    BackendExtractRequest,
    BackendHealth,
    DiscoveryRequest,
    DiscoveryResult,
)
from .executor import DiscoveryProviderRegistry
from openmd.runtime.opencli_adapters import run_xiaoe as _run_xiaoe
from openmd.runtime.media import download_audio

from openmd.runtime.tempdirs import owned_temporary_directory


OPENCLI_XIAOE_TRANSCRIPT_BACKEND = "opencli.xiaoe.transcript"
OPENCLI_XIAOE_CONTENT_BACKEND = "opencli.xiaoe.content"
OPENCLI_XIAOE_CATALOG_DISCOVERY_BACKEND = "opencli.xiaoe.catalog"
XIAOE_TRANSCRIPT_TIMEOUT = 60 * 60
XIAOE_CONTENT_TIMEOUT = 300
XIAOE_CATALOG_TIMEOUT = 300

XIAOE_RESOURCE_TYPE_LABELS: dict[int, str] = {
    1: "图文",
    2: "直播",
    3: "音频",
    4: "视频",
    6: "专栏",
    8: "大专栏",
}
XIAOE_CONTAINER_RESOURCE_TYPES = frozenset({6, 8})
XIAOE_TEXT_RESOURCE_TYPES = frozenset({1})
XIAOE_AUDIO_RESOURCE_TYPES = frozenset({3})
XIAOE_VIDEO_RESOURCE_TYPES = frozenset({2, 4})

XIAOE_ERROR_CODE_ALIASES: dict[str, str] = {
    "argumenterror": "invalid_argument",
    "invalidargument": "invalid_argument",
    "emptyresulterror": "empty_result",
    "emptyresult": "empty_result",
    "emptycontent": "empty_content",
    "commandexecutionerror": "command_execution_failed",
    "commandexecutionfailed": "command_execution_failed",
    "authrequired": "session_auth_required",
    "authenticationrequired": "session_auth_required",
    "sessionexpired": "session_expired",
    "permissiondenied": "permission_denied",
    "notfound": "source_not_found",
    "resourcenotfound": "source_not_found",
}

_XIAOE_ERROR_NAME_RE = re.compile(
    r"\b(?:ArgumentError|EmptyResultError|CommandExecutionError|"
    r"AuthRequired|SessionExpired|PermissionDenied|NotFound)\b",
    re.IGNORECASE,
)

OpenCliRunner = Callable[..., tuple[int, str, str]]


@dataclass(frozen=True)
class XiaoeContentPayload:
    """The small, normalized subset accepted from ``xiaoe content -f json``."""

    title: str | None
    content: str
    content_length: int
    image_count: int
    content_selector: str | None = None


def register_backend_providers(registry: Any, options: Any) -> None:
    """Register Xiaoe extraction providers under the explicit browser gate."""
    if not getattr(options, "enable_browser_backends", False):
        return

    requested = getattr(options, "backend_override", None)
    if requested in {None, OPENCLI_XIAOE_TRANSCRIPT_BACKEND}:
        registry.register(
            OPENCLI_XIAOE_TRANSCRIPT_BACKEND,
            OpenCliXiaoeTranscriptProvider(
                allow_local_asr=bool(getattr(options, "enable_asr_backends", False)),
                allow_media_download=bool(getattr(options, "enable_media_download_backends", False)),
            ),
        )
    if requested in {None, OPENCLI_XIAOE_CONTENT_BACKEND}:
        registry.register(OPENCLI_XIAOE_CONTENT_BACKEND, OpenCliXiaoeContentProvider())


def register_discovery_providers(
    registry: DiscoveryProviderRegistry,
    options: Any,
) -> None:
    """Register Xiaoe catalog discovery under the explicit browser gate."""
    if not getattr(options, "enable_browser_backends", False):
        return

    requested = getattr(options, "requested_discovery_provider", None)
    if requested in {None, OPENCLI_XIAOE_CATALOG_DISCOVERY_BACKEND}:
        registry.register(
            OPENCLI_XIAOE_CATALOG_DISCOVERY_BACKEND,
            OpenCliXiaoeCatalogProvider(),
            platforms=("xiaoe",),
            default_for_platforms=requested is None,
        )


def build_opencli_xiaoe_play_url_args(url: str) -> list[str]:
    return ["opencli", "xiaoe", "play-url", url, "-f", "json", "--window", "background", "--site-session", "persistent", "--keep-tab", "false"]


def build_opencli_xiaoe_catalog_args(url: str) -> list[str]:
    """Build the bounded read-only OpenCLI Xiaoe catalog command."""
    return [
        "opencli",
        "xiaoe",
        "catalog",
        url,
        "-f",
        "json",
        "--window",
        "background",
        "--site-session",
        "persistent",
        "--keep-tab",
        "false",
    ]


def build_opencli_xiaoe_content_args(url: str) -> list[str]:
    """Build the OpenCLI Xiaoe text/content fallback command."""
    return [
        "opencli",
        "xiaoe",
        "content",
        url,
        "-f",
        "json",
        "--window",
        "background",
        "--site-session",
        "persistent",
        "--keep-tab",
        "false",
    ]


class OpenCliXiaoeTranscriptProvider:
    """Adapter media discovery -> core download -> explicitly gated local ASR."""

    name = OPENCLI_XIAOE_TRANSCRIPT_BACKEND

    def __init__(self, *, runner: OpenCliRunner | None = None, timeout: int = XIAOE_TRANSCRIPT_TIMEOUT,
                 allow_local_asr: bool = False, allow_media_download: bool = False,
                 local_asr: Any | None = None, downloader: Any | None = None) -> None:
        self._runner = runner or _run_xiaoe
        self.timeout = timeout
        self.allow_local_asr = allow_local_asr
        self.allow_media_download = allow_media_download
        self._local_asr = local_asr
        self._downloader = downloader or download_audio

    def descriptor(self) -> BackendDescriptor:
        return BackendDescriptor(name=self.name, layer="media ASR", provider="opencli.xiaoe",
            description="Xiaoe media discovery only; Openmd owns download and local ASR. Openmd does not own or store credentials. Cloud services are never selected automatically.",
            capabilities=(BackendCapability(platforms=("xiaoe",), source_types=("video", "audio"), input_types=("single",), source_levels=("media_asr",), requires_network=True, requires_credentials=False,
                optional_config=("opencli", "browser_session", "user_accessible_session", "timeout")),))

    def health(self) -> BackendHealth:
        return _opencli_health(self._runner)

    def extract(self, request: BackendExtractRequest) -> TranscriptResult:
        item = request.item
        failure = _validate_xiaoe_item(item, self.name)
        if failure is not None:
            return failure
        if not (self.allow_local_asr and self.allow_media_download):
            return _failed(self.name, item, "media_asr_not_enabled", "Enable media download and local ASR explicitly; no cloud service was invoked.")
        stages: dict[str, float] = {}
        start = time.monotonic()
        code, stdout, stderr = self._runner(build_opencli_xiaoe_play_url_args(item.url), timeout=min(self.timeout, 120))
        stages["media_discovery"] = round(time.monotonic() - start, 3)
        if code:
            return _backend_failure(self.name, item, code, stderr or stdout)
        try:
            payload = load_opencli_json(stdout)
            rows = payload if isinstance(payload, list) else payload.get("data", payload.get("rows", []))
            if isinstance(rows, dict):
                rows = rows.get("rows", [])
            record = rows[0] if isinstance(rows, list) and len(rows) == 1 else {}
            expected = re.search(r"[val]_[a-f0-9]+", item.url)
            if expected and record.get("resource_id") != expected.group():
                return _failed(self.name, item, "media_identity_mismatch", "Media discovery did not identify the requested resource.")
            locator = record.get("m3u8_url", "")
            if not isinstance(locator, str) or not locator.startswith(("https://", "http://")):
                return _failed(self.name, item, "media_not_found", "No media found for this resource; check access in the browser.")
            backend = self._local_asr
            if backend is None:
                from openmd.runtime.asr import get_asr_backend
                backend = get_asr_backend("asr.auto")
            with owned_temporary_directory(prefix="openmd-xiaoe-") as directory:
                audio = Path(directory) / "audio.mp3"
                start = time.monotonic()
                origin = urlsplit(item.url)
                self._downloader(locator, audio, referer=f"{origin.scheme}://{origin.netloc}/", timeout=min(self.timeout, 600))
                stages["download_audio"] = round(time.monotonic() - start, 3)
                start = time.monotonic()
                result = backend.transcribe(audio)
                stages["asr"] = round(time.monotonic() - start, 3)
            return validate_extraction_content(replace(result, backend=self.name, source_level="media_asr" if result.has_usable_text else result.source_level, title=record.get("title") or item.title,
                source_notes=[*result.source_notes, "Openmd downloaded temporary audio and used local ASR; no cloud service was invoked."],
                metadata={**sanitize_asr_execution(result.metadata), "asr_backend": result.backend, "transcript_provider": result.backend, "stage_seconds": stages,
                          "media_method": record.get("method") if record.get("method") in {"api_direct", "alive_lookback_api", "perf_api", "vue_search"} else "unknown",
                          "navigation_seconds": record.get("navigation_seconds") if isinstance(record.get("navigation_seconds"), (int, float)) else None}))
        except (ValueError, TypeError, KeyError, IndexError, RuntimeError, OSError) as exc:
            return _failed(self.name, item, "media_pipeline_failed", f"Xiaoe media pipeline failed: {type(exc).__name__}", metadata={"stage_seconds": stages})


class OpenCliXiaoeCatalogProvider:
    """Discover Xiaoe course catalog entries through OpenCLI."""

    name = OPENCLI_XIAOE_CATALOG_DISCOVERY_BACKEND

    def __init__(
        self,
        *,
        runner: OpenCliRunner | None = None,
        timeout: int = XIAOE_CATALOG_TIMEOUT,
    ) -> None:
        self._runner = runner or _run_xiaoe
        self.timeout = timeout

    def discover(self, request: DiscoveryRequest) -> DiscoveryResult:
        if request.platform != "xiaoe":
            return DiscoveryResult.failure(
                provider=self.name,
                error_code="unsupported_platform",
                error_summary=f"Xiaoe catalog discovery only supports xiaoe, got {request.platform}.",
            )

        args = build_opencli_xiaoe_catalog_args(request.input_url)
        exit_code, stdout, stderr = self._runner(args, timeout=self.timeout)
        if exit_code != 0:
            error_code, error_summary = _map_xiaoe_error(stderr or stdout, exit_code)
            return DiscoveryResult.failure(
                provider=self.name,
                error_code=error_code,
                error_summary=error_summary,
                source_notes=[
                    "OpenCLI xiaoe catalog failed before item extraction.",
                    "Raw browser/session state and command streams were not persisted by Openmd.",
                ],
                metadata={"exit_code": exit_code},
            )

        items = parse_xiaoe_catalog_items(stdout)
        if request.limit is not None and not request.allow_all:
            items = items[: max(0, request.limit)]
        if not items:
            return DiscoveryResult.failure(
                provider=self.name,
                error_code="empty_discovery_result",
                error_summary="OpenCLI xiaoe catalog returned no catalog items.",
            )

        return DiscoveryResult.success(
            provider=self.name,
            items=items,
            source_notes=[
                "OpenCLI xiaoe catalog returned manifest items.",
                "Catalog discovery does not run transcript extraction, batch-transcript, media download, or ASR.",
            ],
            metadata={"normalized_url": request.input_url},
        )


class OpenCliXiaoeContentProvider:
    """Fetch Xiaoe page/article text through OpenCLI content as fallback."""

    name = OPENCLI_XIAOE_CONTENT_BACKEND

    def __init__(
        self,
        *,
        runner: OpenCliRunner | None = None,
        timeout: int = XIAOE_CONTENT_TIMEOUT,
    ) -> None:
        self._runner = runner or _run_xiaoe
        self.timeout = timeout

    def descriptor(self) -> BackendDescriptor:
        capability = BackendCapability(
            platforms=("xiaoe",),
            source_types=("article", "image_text", "mixed", "video"),
            input_types=("single",),
            source_levels=("native_text",),
            requires_network=True,
            requires_credentials=False,
            optional_config=(
                "opencli",
                "browser_session",
                "user_accessible_session",
                "timeout",
            ),
        )
        return BackendDescriptor(
            name=self.name,
            layer="L2 browser/session extractor",
            capabilities=(capability,),
            description=(
                "Fetch Xiaoe graphic/text content through OpenCLI content fallback. "
                "Openmd does not own or store credentials; a user-accessible browser "
                "session may still be required."
            ),
            provider="opencli.xiaoe",
            is_fallback=True,
        )

    def health(self) -> BackendHealth:
        return _opencli_health(self._runner)

    def extract(self, request: BackendExtractRequest) -> TranscriptResult:
        item = request.item
        platform_failure = _validate_xiaoe_item(item, self.name)
        if platform_failure is not None:
            return platform_failure

        args = build_opencli_xiaoe_content_args(item.url)
        exit_code, stdout, stderr = self._runner(args, timeout=self.timeout)
        if exit_code != 0:
            return _backend_failure(self.name, item, exit_code, stderr or stdout)
        if not stdout.strip():
            return _failed(
                self.name,
                item,
                "empty_result",
                "OpenCLI xiaoe content returned empty stdout.",
            )

        content_payload = parse_xiaoe_content_result(stdout)
        if content_payload is None:
            typed_error = _extract_xiaoe_error_details(stdout)
            if typed_error is not None:
                error_code, error_summary = _map_xiaoe_error(typed_error, None)
                return _failed(
                    self.name,
                    item,
                    error_code,
                    error_summary,
                )
            return _failed(
                self.name,
                item,
                "invalid_content_response",
                "OpenCLI xiaoe content did not return the expected JSON content record.",
            )

        if not content_payload.content.strip():
            return _failed(
                self.name,
                item,
                "empty_content",
                "OpenCLI xiaoe content returned an empty content field.",
                metadata={
                    "content_length": content_payload.content_length,
                    "image_count": content_payload.image_count,
                },
            )

        content_length_mismatch = (
            content_payload.content_length > 0
            and content_payload.content_length != _javascript_string_length(content_payload.content)
        )
        source_notes = [
            "OpenCLI xiaoe content extraction output was normalized from JSON.",
            "Only title, content, content_length, image_count, and content_selector were retained.",
            "Fallback path only; no batch-transcript, media download, ASR, or permission bypass was attempted.",
        ]
        if content_length_mismatch:
            source_notes.append(
                "Declared content_length differs from the returned content length; result is marked partial."
            )

        result = TranscriptResult(
            text=content_payload.content,
            status="partial" if content_length_mismatch else "verified",
            source_level="native_text",
            backend=self.name,
            title=content_payload.title,
            source_notes=source_notes,
            metadata={
                "content_length": content_payload.content_length,
                "image_count": content_payload.image_count,
                "content_selector": content_payload.content_selector,
            },
        )
        return validate_extraction_content(result)


def _opencli_health(runner: OpenCliRunner) -> BackendHealth:
    if runner is not _run_xiaoe:
        return BackendHealth(ok=True, dependencies={"opencli": "injected"})
    from openmd.runtime.managed_tools import managed_package_root
    if not (managed_package_root() / "package.json").is_file():
        return BackendHealth(
            ok=False,
            dependencies={"opencli": "missing"},
            error_code="backend_dependency_missing",
            error_summary="opencli executable is not available on PATH.",
        )
    return BackendHealth(ok=True, dependencies={"opencli": "available"})


def _validate_xiaoe_item(item: ContentItem, backend: str) -> TranscriptResult | None:
    platform_failure = _require_xiaoe_item(item, backend)
    if platform_failure is not None:
        return platform_failure
    if not item.url.strip():
        resource_id = item.id or item.raw_meta.get("resource_id")
        return _failed(
            backend,
            item,
            "resource_url_unavailable",
            (
                "Xiaoe catalog item has no canonical URL and cannot be sent to an "
                f"extractor (resource_id={resource_id or 'unknown'})."
            ),
            metadata={"resource_id": resource_id},
        )
    if item.raw_meta.get("container") is True:
        return _failed(
            backend,
            item,
            "container_not_extractable",
            "Xiaoe column/container records must be discovered before extracting lessons.",
            metadata={"resource_id": item.id or item.raw_meta.get("resource_id")},
        )
    return None


def _require_xiaoe_item(item: ContentItem, backend: str) -> TranscriptResult | None:
    if item.platform == "xiaoe":
        return None
    return _failed(
        backend,
        item,
        "unsupported_platform",
        f"OpenCLI Xiaoe backend only supports xiaoe, got {item.platform}.",
    )


def parse_xiaoe_catalog_items(stdout: str) -> list[ContentItem]:
    records = _catalog_records_from_stdout(stdout)
    items: list[ContentItem] = []
    for record in records:
        item = _catalog_item_from_record(record)
        if item is not None:
            items.append(item)
    return items


def _catalog_records_from_stdout(stdout: str) -> list[dict[str, Any]]:
    import json

    try:
        payload = load_opencli_json(stdout.strip())
    except json.JSONDecodeError:
        return []
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("items", "catalog", "data", "rows", "list"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            nested = _catalog_records_from_payload(value)
            if nested:
                return nested
    return []


def _catalog_records_from_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("items", "catalog", "data", "rows", "list"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            nested = _catalog_records_from_payload(value)
            if nested:
                return nested
    return []


def _catalog_item_from_record(record: dict[str, Any]) -> ContentItem | None:
    url = _first_text(record, "url", "link", "href", "canonical_url") or ""
    resource_id = _first_text(
        record,
        "resource_id",
        "resourceId",
        "chapter_id",
        "chapterId",
        "id",
        "rid",
    )
    if not url and not resource_id:
        return None
    raw_resource_type = _first_value(
        record,
        "resource_type",
        "resourceType",
        "chapter_type",
        "chapterType",
        "type",
    )
    resource_label = _first_text(record, "resource_label", "resourceLabel", "label")
    resource_kind, source_type, is_container = _classify_xiaoe_resource_type(
        raw_resource_type
    )
    resource_label = resource_label or _xiaoe_resource_type_label(raw_resource_type)
    title = _first_text(record, "title", "name")
    return ContentItem(
        platform="xiaoe",
        input_type="collection",
        url=url,
        id=resource_id,
        title=title,
        source_type=source_type,
        raw_meta={
            "profile": (
                "media"
                if source_type in {"video", "audio"}
                else "document"
                if source_type == "image_text"
                else "article"
            ),
            "chapter": _first_text(record, "chapter", "ch"),
            "no": _first_text(record, "no", "index"),
            "status": _first_text(record, "status"),
            "resource_id": resource_id,
            "resource_type": raw_resource_type,
            "resource_label": resource_label or None,
            "resource_kind": resource_kind,
            "container": is_container,
            "canonical_url": url or None,
            "canonical_url_available": bool(url),
            "catalog_type": resource_label or None,
            "caption_status": "unknown",
            "discovery_provider": OPENCLI_XIAOE_CATALOG_DISCOVERY_BACKEND,
        },
    )


def _first_value(record: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = record.get(key)
        if value is None:
            continue
        if isinstance(value, str):
            if value.strip():
                return value.strip()
            continue
        return value
    return None


def _first_text(record: dict[str, Any], *keys: str) -> str | None:
    value = _first_value(record, *keys)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _resource_type_number(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _xiaoe_resource_type_label(value: Any) -> str | None:
    resource_type_number = _resource_type_number(value)
    if resource_type_number in XIAOE_RESOURCE_TYPE_LABELS:
        return XIAOE_RESOURCE_TYPE_LABELS[resource_type_number]
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _classify_xiaoe_resource_type(value: Any) -> tuple[str, str, bool]:
    resource_type_number = _resource_type_number(value)
    if resource_type_number in XIAOE_CONTAINER_RESOURCE_TYPES:
        return "container", "mixed", True
    if resource_type_number in XIAOE_TEXT_RESOURCE_TYPES:
        return "image_text", "image_text", False
    if resource_type_number in XIAOE_AUDIO_RESOURCE_TYPES:
        return "audio", "audio", False
    if resource_type_number in XIAOE_VIDEO_RESOURCE_TYPES:
        return "live" if resource_type_number == 2 else "video", "video", False

    normalized = str(value or "").strip().lower()
    if normalized in {"图文", "article", "text", "graphic", "image_text"}:
        return "image_text", "image_text", False
    if normalized in {"直播", "live", "live_stream", "livestream"}:
        return "live", "video", False
    if normalized in {"音频", "audio", "podcast"}:
        return "audio", "audio", False
    if normalized in {"视频", "video", "vod"}:
        return "video", "video", False
    if normalized in {"专栏", "大专栏", "column", "series", "container"}:
        return "container", "mixed", True
    return "unknown", "mixed", False


def parse_xiaoe_content_result(stdout: str) -> XiaoeContentPayload | None:
    """Extract only the normalized content fields from an OpenCLI JSON response."""
    try:
        payload = load_opencli_json(stdout.strip())
    except (TypeError, ValueError):
        return None

    record = _find_xiaoe_content_record(payload)
    if record is None:
        return None
    content = record.get("content")
    if not isinstance(content, str):
        return None
    return XiaoeContentPayload(
        title=_first_text(record, "title", "name"),
        content=content,
        content_length=_non_negative_int(record.get("content_length"), len(content)),
        image_count=_non_negative_int(record.get("image_count"), 0),
        content_selector=_first_text(record, "content_selector"),
    )


def _find_xiaoe_content_record(payload: Any) -> dict[str, Any] | None:
    if isinstance(payload, dict):
        if "content" in payload or "content_length" in payload or "image_count" in payload:
            return payload
        for key in ("data", "result", "items", "rows", "list"):
            nested = _find_xiaoe_content_record(payload.get(key))
            if nested is not None:
                return nested
    elif isinstance(payload, list):
        for value in payload:
            nested = _find_xiaoe_content_record(value)
            if nested is not None:
                return nested
    return None


def _non_negative_int(value: Any, fallback: int) -> int:
    if isinstance(value, bool):
        return fallback
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        return fallback
    return normalized if normalized >= 0 else fallback


def _javascript_string_length(value: str) -> int:
    """Match JavaScript's UTF-16 ``String.length`` for content metadata."""
    return len(value.encode("utf-16-le")) // 2


def _extract_xiaoe_error_details(value: Any) -> tuple[str | None, str | None] | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        payload = json.loads(text)
    except (TypeError, ValueError):
        payload = None

    candidates: list[dict[str, Any]] = []
    if isinstance(payload, dict):
        candidates.append(payload)
        for key in ("error", "failure", "data", "result"):
            nested = payload.get(key)
            if isinstance(nested, dict):
                candidates.append(nested)
    for candidate in candidates:
        error_code = _first_text(
            candidate,
            "error_code",
            "errorCode",
            "code",
            "name",
            "type",
        )
        error_message = _first_text(
            candidate,
            "error_summary",
            "errorSummary",
            "message",
            "detail",
        )
        if error_code or error_message:
            return error_code, error_message

    match = _XIAOE_ERROR_NAME_RE.search(text)
    if match:
        return match.group(0), text
    return None


def _map_xiaoe_error(
    error: tuple[str | None, str | None] | str,
    exit_code: int | None,
) -> tuple[str, str]:
    if isinstance(error, tuple):
        raw_code, raw_summary = error
    else:
        details = _extract_xiaoe_error_details(error)
        raw_code, raw_summary = details or (None, str(error))
    normalized_code = re.sub(r"[^a-z0-9]+", "", str(raw_code or "").lower())
    mapped_code = XIAOE_ERROR_CODE_ALIASES.get(normalized_code)
    if mapped_code is None:
        mapped_code = str(raw_code or "").strip().lower().replace(" ", "_")
    if not mapped_code:
        mapped_code = f"backend_exit_{exit_code}" if exit_code is not None else "backend_failed"
    summary = sanitize_error_summary(
        raw_summary or f"OpenCLI xiaoe command failed with code {mapped_code}."
    ) or "OpenCLI xiaoe command failed."
    return mapped_code, summary


def _backend_failure(
    backend: str,
    item: ContentItem,
    exit_code: int,
    error: object,
) -> TranscriptResult:
    code, summary = _map_xiaoe_error(error, exit_code)
    if exit_code == 127 and code.startswith("backend_exit_"):
        code = "backend_dependency_missing"
    return _failed(
        backend,
        item,
        code,
        summary or f"OpenCLI command exited with code {exit_code}.",
        metadata={"exit_code": exit_code},
        notes=["backend command failed without persisting raw stdout/stderr."],
    )


def _failed(
    backend: str,
    item: ContentItem,
    code: str,
    error: object,
    *,
    metadata: dict[str, object] | None = None,
    notes: list[str] | None = None,
) -> TranscriptResult:
    return TranscriptResult(
        text="",
        status="failed",
        source_level="partial_extraction",
        backend=backend,
        source_notes=["Xiaoe runtime provider returned structured failure.", *(notes or [])],
        error_code=code,
        error_summary=sanitize_error_summary(error),
        metadata={"platform": item.platform, **dict(metadata or {})},
    )
