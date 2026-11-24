"""Xiaohongshu OpenCLI runtime providers.

The providers in this module are loaded by ``openmd.backends.registry`` through
the platform extension naming convention. They stay inert unless the caller
explicitly enables browser backends, because OpenCLI may use a local browser
session to read signed Xiaohongshu pages.
"""

from __future__ import annotations

import json
import re
import shutil
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urlparse

from openmd.core.opencli_output import load_opencli_json
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
from openmd.core.package_contract import XIAOHONGSHU_EXTRACTION_CONTRACT_VERSION
from openmd.core.secrets import sanitize_error_summary, sanitize_url, sanitize_url_text


OPENCLI_XIAOHONGSHU_NOTE_BACKEND = "opencli.xiaohongshu.note"
OPENCLI_XIAOHONGSHU_USER_DISCOVERY_BACKEND = "opencli.xiaohongshu.user"
OPENCLI_OPENMD_XIAOHONGSHU_CREATOR_NOTES_BACKEND = "opencli.openmd-xiaohongshu.creator-notes"
DEFAULT_XIAOHONGSHU_USER_LIMIT = 20
MAX_XIAOHONGSHU_USER_LIMIT = 50
MIN_NOTE_CHARS = 1

_SIGNED_QUERY_KEY = "xsec" + "_token"
_URL_CANDIDATE_RE = re.compile(r"https?://[^\s|,)\]\"']+")
_NOTE_PATH_MARKERS = ("/explore/", "/discovery/item/", "/item/")
_USER_PATH_MARKER = "/user/profile/"

OpenCliRunner = Callable[..., tuple[int, str, str]]
OpenCliInputRunner = Callable[..., tuple[int, str, str]]


@dataclass(frozen=True)
class ParsedXiaohongshuNote:
    text: str
    title: str | None = None
    author: str | None = None
    published_at: str | None = None
    note_id: str | None = None


def register_backend_providers(
    registry: BackendProviderRegistry,
    options: BackendRuntimeOptions,
) -> None:
    """Register Xiaohongshu extraction only behind the browser backend gate."""
    requested = options.backend_override
    if options.enable_browser_backends and requested in {None, OPENCLI_XIAOHONGSHU_NOTE_BACKEND}:
        registry.register(OPENCLI_XIAOHONGSHU_NOTE_BACKEND, OpenCliXiaohongshuNoteProvider())
    from openmd.backends.kaze_snapshot import register_backend_providers as register_kaze_snapshot

    register_kaze_snapshot(registry, options)


def register_discovery_providers(
    registry: DiscoveryProviderRegistry,
    options: BackendRuntimeOptions,
) -> None:
    """Register bounded Xiaohongshu user discovery only behind the browser gate."""
    requested = options.requested_discovery_provider
    if options.enable_browser_backends and requested in {None, OPENCLI_XIAOHONGSHU_USER_DISCOVERY_BACKEND}:
        registry.register(
            OPENCLI_XIAOHONGSHU_USER_DISCOVERY_BACKEND,
            OpenCliXiaohongshuUserDiscoveryProvider(),
            platforms=("xiaohongshu",),
            default_for_platforms=requested is None,
        )
    if options.enable_browser_backends and requested == OPENCLI_OPENMD_XIAOHONGSHU_CREATOR_NOTES_BACKEND:
        registry.register(
            OPENCLI_OPENMD_XIAOHONGSHU_CREATOR_NOTES_BACKEND,
            OpenCliOpenmdXiaohongshuCreatorNotesProvider(),
            platforms=("xiaohongshu",),
            default_for_platforms=False,
        )


def build_opencli_xiaohongshu_note_args(input_url: str) -> list[str]:
    return ["opencli", "xiaohongshu", "note", input_url, "-f", "json"]


def build_opencli_xiaohongshu_user_args(input_url: str, *, limit: int | None = None) -> list[str]:
    bounded_limit = bounded_xiaohongshu_user_limit(limit)
    return [
        "opencli",
        "xiaohongshu",
        "user",
        input_url,
        "--limit",
        str(bounded_limit),
        "-f",
        "json",
    ]


def build_opencli_openmd_xiaohongshu_creator_notes_args(
    input_value: str,
    *,
    selector_stdin: bool = False,
) -> list[str]:
    """Build the project-owned full profile discovery command with a query-free target."""
    user_id = xiaohongshu_user_id(input_value)
    target = (
        f"https://www.xiaohongshu.com/user/profile/{user_id}"
        if user_id
        else sanitize_url(normalize_xiaohongshu_url(input_value))
    )
    command = [
        "opencli",
        "openmd-xiaohongshu",
        "creator-notes",
        "stdin-selector" if selector_stdin else target,
        "--timeout",
        "1800",
        "--scroll_delay_ms",
        "1500",
        "--stable_rounds",
        "4",
        "-f",
        "json",
    ]
    if selector_stdin:
        command.extend(
            [
                "--selector_stdin",
                "true",
                "--transient_note_urls",
                "true",
            ]
        )
    return command


def bounded_xiaohongshu_user_limit(limit: int | None) -> int:
    if limit is None:
        return DEFAULT_XIAOHONGSHU_USER_LIMIT
    return min(MAX_XIAOHONGSHU_USER_LIMIT, max(0, int(limit)))


def normalize_xiaohongshu_url(input_value: str) -> str:
    value = extract_first_url(str(input_value or "")).strip()
    if value.startswith("xiaohongshu.com/") or value.startswith("www.xiaohongshu.com/"):
        value = f"https://{value}"
    return value


def is_xiaohongshu_user_url(input_value: str) -> bool:
    parsed = urlparse(normalize_xiaohongshu_url(input_value))
    return _host_is_xiaohongshu(parsed.netloc) and _USER_PATH_MARKER in parsed.path


def xiaohongshu_user_id(input_value: str) -> str | None:
    parsed = urlparse(normalize_xiaohongshu_url(input_value))
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) == 3 and parts[:2] == ["user", "profile"]:
        return parts[2]
    value = str(input_value or "").strip()
    if re.fullmatch(r"[A-Za-z0-9]{8,64}", value):
        return value
    return None


def is_xiaohongshu_note_url(input_value: str) -> bool:
    parsed = urlparse(normalize_xiaohongshu_url(input_value))
    if not _host_is_xiaohongshu(parsed.netloc):
        return False
    if parsed.netloc.lower().endswith("xhslink.com"):
        return True
    return any(marker in parsed.path for marker in _NOTE_PATH_MARKERS)


def has_required_signed_query(input_value: str) -> bool:
    parsed = urlparse(normalize_xiaohongshu_url(input_value))
    return any(key.lower() == _SIGNED_QUERY_KEY and value.strip() for key, value in parse_qsl(parsed.query))


class OpenCliXiaohongshuNoteProvider:
    """Extract one Xiaohongshu note through OpenCLI without persisting raw output."""

    name = OPENCLI_XIAOHONGSHU_NOTE_BACKEND

    def __init__(
        self,
        *,
        runner: OpenCliRunner | None = None,
        timeout: int = 60,
        min_note_chars: int = MIN_NOTE_CHARS,
    ) -> None:
        self._runner = runner or _run_opencli
        self.timeout = timeout
        self.min_note_chars = max(0, min_note_chars)

    def descriptor(self) -> BackendDescriptor:
        capability = BackendCapability(
            platforms=("xiaohongshu",),
            source_types=("mixed",),
            input_types=("single", "collection"),
            source_levels=("native_text", "third_party_extractor", "partial_extraction"),
            requires_network=True,
            requires_credentials=False,
        )
        return BackendDescriptor(
            name=self.name,
            layer="L3 browser/session fallback",
            capabilities=(capability,),
            description="Use OpenCLI Xiaohongshu note extraction for signed public note URLs.",
            provider="opencli.xiaohongshu",
        )

    def health(self) -> BackendHealth:
        if self._runner is not _run_opencli:
            return BackendHealth(ok=True, dependencies={"opencli": "injected"})
        if shutil.which("opencli") is None and shutil.which("opencli.cmd") is None:
            return BackendHealth(
                ok=False,
                dependencies={"opencli": "missing"},
                error_code="backend_dependency_missing",
                error_summary="opencli executable is not available on PATH.",
            )
        return BackendHealth(ok=True, dependencies={"opencli": "available"})

    def extract(self, request: BackendExtractRequest) -> TranscriptResult:
        item = request.item
        url = normalize_xiaohongshu_url(item.url)
        runtime_url = str(item.raw_meta.get("runtime_note_url") or "").strip()
        command_url = runtime_url if has_required_signed_query(runtime_url) else url
        metadata = {"platform": item.platform, "normalized_url": sanitize_url(url)}

        if item.platform != "xiaohongshu":
            return _failed(
                self.name,
                "unsupported_platform",
                f"Xiaohongshu note provider only supports xiaohongshu, got {item.platform}.",
                metadata=metadata,
            )
        if is_xiaohongshu_user_url(url):
            return _failed(
                self.name,
                "xiaohongshu_user_requires_collect",
                "Use collect with browser backends for bounded Xiaohongshu user discovery.",
                metadata=metadata,
            )
        if not is_xiaohongshu_note_url(url):
            return _failed(
                self.name,
                "invalid_input",
                "Expected a Xiaohongshu note URL.",
                metadata=metadata,
            )
        if not has_required_signed_query(command_url):
            return _failed(
                self.name,
                "xiaohongshu_signed_url_required",
                "缺少笔记分享参数。请在浏览器里打开这条笔记再点扩展，或从分享按钮复制完整链接。",
                source_notes=[
                    "OpenCLI Xiaohongshu note extraction was not run because the signed URL shape was missing.",
                ],
                metadata=metadata,
            )

        command = build_opencli_xiaohongshu_note_args(command_url)
        exit_code, stdout, stderr = self._runner(command, timeout=self.timeout)
        if exit_code != 0:
            if exit_code == 124:
                # The bridge owns tab leases by Openmd controller PID. End only
                # this controller's lease so a killed CLI cannot leave its
                # background note tab behind; user tabs are never selected.
                try:
                    from openmd.runtime.services import finish_openmd_bridge
                    finish_openmd_bridge()
                except (ImportError, OSError, RuntimeError):
                    pass
            return _failed(
                self.name,
                classify_xiaohongshu_failure(stderr or stdout),
                stderr or stdout or f"OpenCLI exited with code {exit_code}.",
                source_notes=["OpenCLI Xiaohongshu note extraction returned a command failure."],
                metadata={**metadata, "exit_code": exit_code},
            )

        try:
            parsed = parse_opencli_xiaohongshu_note_output(stdout)
        except (json.JSONDecodeError, ValueError) as exc:
            return _failed(
                self.name,
                "opencli_xiaohongshu_note_invalid_output",
                exc,
                source_notes=["OpenCLI Xiaohongshu note stdout had an unexpected shape."],
                metadata=metadata,
            )

        if len("".join(parsed.text.split())) < self.min_note_chars:
            return _failed(
                self.name,
                "opencli_xiaohongshu_note_empty",
                "OpenCLI Xiaohongshu note extraction returned no usable text.",
                metadata=metadata,
            )

        result_metadata: dict[str, object] = {
            **metadata,
            "requires_browser_context": True,
        }
        if parsed.note_id:
            result_metadata["note_id"] = parsed.note_id
        return TranscriptResult(
            text=parsed.text.rstrip() + "\n",
            status="verified",
            source_level="native_text",
            backend=self.name,
            title=parsed.title or item.title,
            author=parsed.author or item.author,
            published_at=parsed.published_at or item.published_at,
            source_notes=[
                "OpenCLI Xiaohongshu note extraction returned normalized note text.",
                "Raw stdout/stderr and browser session data were not persisted.",
            ],
            metadata=result_metadata,
        )


class OpenCliXiaohongshuUserDiscoveryProvider:
    """Route bounded profiles to official OpenCLI and explicit all to the project plugin."""

    name = OPENCLI_XIAOHONGSHU_USER_DISCOVERY_BACKEND

    def __init__(
        self,
        *,
        runner: OpenCliRunner | None = None,
        timeout: int = 120,
        full_timeout: int = 1900,
    ) -> None:
        self._runner = runner or _run_opencli
        self.timeout = timeout
        self.full_timeout = full_timeout

    def discover(self, request: DiscoveryRequest) -> DiscoveryResult:
        url = normalize_xiaohongshu_url(request.input_url)
        metadata: dict[str, object] = {
            "platform": request.platform,
            "normalized_url": sanitize_url(url),
            "limit": bounded_xiaohongshu_user_limit(request.limit),
            "requires_browser_context": True,
        }

        if request.platform != "xiaohongshu" or not xiaohongshu_user_id(url):
            return _discovery_failure(
                self.name,
                "xiaohongshu_user_input_not_supported",
                "Input is not a supported Xiaohongshu user profile URL or user id.",
                source_notes=["OpenCLI Xiaohongshu user discovery did not run because the input is unsupported."],
                metadata=metadata,
            )
        if request.allow_all:
            return OpenCliOpenmdXiaohongshuCreatorNotesProvider(
                runner=self._runner,
                timeout=self.full_timeout,
            ).discover(request)
        if metadata["limit"] <= 0:
            return _discovery_failure(
                self.name,
                "xiaohongshu_user_limit_required",
                "Xiaohongshu user discovery requires a positive bounded limit.",
                metadata=metadata,
            )

        command = build_opencli_xiaohongshu_user_args(url, limit=request.limit)
        exit_code, stdout, stderr = self._runner(command, timeout=self.timeout)
        if exit_code != 0:
            return _discovery_failure(
                self.name,
                classify_xiaohongshu_failure(stderr or stdout),
                stderr or stdout or f"OpenCLI exited with code {exit_code}.",
                source_notes=["OpenCLI Xiaohongshu user discovery returned a command failure."],
                metadata={**metadata, "exit_code": exit_code},
            )

        try:
            items = parse_opencli_xiaohongshu_user_output(
                stdout,
                collection_url=url,
                discovery_provider=self.name,
                include_transient=True,
            )[: int(metadata["limit"])]
        except (json.JSONDecodeError, ValueError) as exc:
            return _discovery_failure(
                self.name,
                "opencli_xiaohongshu_user_invalid_output",
                exc,
                source_notes=["OpenCLI Xiaohongshu user discovery stdout had an unexpected shape."],
                metadata=metadata,
            )

        if not items:
            return _discovery_failure(
                self.name,
                "opencli_xiaohongshu_user_empty",
                "OpenCLI Xiaohongshu user discovery returned no note items.",
                source_notes=["OpenCLI Xiaohongshu user discovery returned no parseable note URLs."],
                metadata=metadata,
            )

        return DiscoveryResult.success(
            provider=self.name,
            items=items,
            source_notes=[
                "OpenCLI browser-context discovery resolved Xiaohongshu user notes.",
                "Discovery stayed bounded by --limit and did not run an unbounded user crawl.",
                "Raw stdout/stderr and browser session data were not persisted.",
            ],
            metadata={**metadata, "item_count": len(items)},
        )


class OpenCliOpenmdXiaohongshuCreatorNotesProvider:
    """Full read-only creator discovery through the project-owned OpenCLI plugin."""

    name = OPENCLI_OPENMD_XIAOHONGSHU_CREATOR_NOTES_BACKEND

    def __init__(
        self,
        *,
        runner: OpenCliRunner | None = None,
        input_runner: OpenCliInputRunner | None = None,
        timeout: int = 1900,
    ) -> None:
        self._runner = runner or _run_opencli
        self._input_runner = input_runner
        self.timeout = timeout

    def discover(self, request: DiscoveryRequest) -> DiscoveryResult:
        url = normalize_xiaohongshu_url(request.input_url)
        canonical_profile = sanitize_url(url)
        metadata: dict[str, object] = {
            "platform": request.platform,
            "normalized_url": canonical_profile,
            "requires_browser_context": True,
            "full_discovery": True,
        }
        if request.platform != "xiaohongshu" or not xiaohongshu_user_id(url):
            return _discovery_failure(
                self.name,
                "xiaohongshu_user_input_not_supported",
                "Input is not a supported Xiaohongshu creator profile URL or user id.",
                metadata=metadata,
            )
        if not request.allow_all:
            return _discovery_failure(
                self.name,
                "xiaohongshu_full_discovery_requires_all",
                "The project-owned Xiaohongshu creator provider requires explicit --all.",
                metadata=metadata,
            )

        runtime_selector = url if has_required_signed_query(url) else None
        command = build_opencli_openmd_xiaohongshu_creator_notes_args(
            url,
            selector_stdin=runtime_selector is not None,
        )
        if runtime_selector is not None:
            input_runner = self._input_runner or (_run_opencli if self._runner is _run_opencli else None)
            if input_runner is not None:
                exit_code, stdout, stderr = input_runner(
                    command,
                    timeout=self.timeout,
                    stdin_text=runtime_selector,
                )
            else:
                exit_code, stdout, stderr = self._runner(command, timeout=self.timeout)
        else:
            exit_code, stdout, stderr = self._runner(command, timeout=self.timeout)
        if exit_code != 0:
            return _discovery_failure(
                self.name,
                classify_xiaohongshu_failure(stderr or stdout),
                stderr or stdout or f"OpenCLI exited with code {exit_code}.",
                source_notes=["Project-owned Xiaohongshu full discovery returned a command failure."],
                metadata={**metadata, "exit_code": exit_code},
            )

        try:
            items = parse_opencli_xiaohongshu_user_output(
                stdout,
                collection_url=canonical_profile,
                discovery_provider=self.name,
                include_transient=True,
                include_runtime_note_url=True,
            )
        except (json.JSONDecodeError, ValueError) as exc:
            return _discovery_failure(
                self.name,
                "opencli_xiaohongshu_creator_notes_invalid_output",
                exc,
                metadata=metadata,
            )
        if not items:
            return _discovery_failure(
                self.name,
                "opencli_xiaohongshu_creator_notes_empty",
                "The project-owned Xiaohongshu creator plugin returned no parseable notes.",
                metadata=metadata,
            )
        if runtime_selector is not None:
            for item in items:
                if not item.raw_meta.get("runtime_note_url"):
                    item.raw_meta["runtime_profile_selector"] = runtime_selector

        pagination_metadata = {
            key: items[0].raw_meta.get(key)
            for key in (
                "page_count",
                "scroll_count",
                "stable_round_count",
                "empty_snapshot_count",
                "duplicate_snapshot_count",
                "anomaly_count",
                "source_exhausted",
                "explicit_exhaustion",
                "exhaustion_reason",
                "store_source",
            )
            if items[0].raw_meta.get(key) is not None
        }
        if (
            pagination_metadata.get("source_exhausted") is not True
            or pagination_metadata.get("explicit_exhaustion") is not True
        ):
            return _discovery_failure(
                self.name,
                "xiaohongshu_source_not_explicitly_exhausted",
                "The project-owned Xiaohongshu plugin returned rows without an explicit platform exhaustion signal.",
                metadata={**metadata, **pagination_metadata},
            )

        return DiscoveryResult.success(
            provider=self.name,
            items=items,
            source_notes=[
                "The project-owned OpenCLI plugin read the authenticated Xiaohongshu profile store while scrolling.",
                "Discovery stopped only after an explicit platform end marker; stable no-growth rounds are retryable and do not prove exhaustion.",
                "Canonical note ids remain the durable identity; current per-note selectors cross only captured child stdout in memory.",
                "Transient note selectors are removed by manifest/runlog/package allowlists before persistence.",
                "Raw browser stores, cookies, headers, signed values, and command streams were not persisted.",
            ],
            metadata={
                **metadata,
                "item_count": len(items),
                **pagination_metadata,
            },
        )


def parse_opencli_xiaohongshu_note_output(stdout: str) -> ParsedXiaohongshuNote:
    text = str(stdout or "").strip()
    if not text:
        raise ValueError("OpenCLI Xiaohongshu note output was empty.")

    try:
        payload = load_opencli_json(text)
    except json.JSONDecodeError:
        return ParsedXiaohongshuNote(text=sanitize_url_text(text))

    note = _first_note_dict(payload)
    if note is None:
        raise ValueError("OpenCLI Xiaohongshu note output did not contain a note object.")

    title = _first_text(note, ("title", "note_title", "display_title", "name"))
    body = _first_text(
        note,
        (
            "markdown",
            "content",
            "desc",
            "description",
            "text",
            "note",
        ),
    )
    if body is None:
        body = _first_text(_nested_dict(note, ("data", "note_info", "note_card")) or {}, ("content", "desc", "text"))
    if body is None:
        body = ""

    tags = _string_list(note.get("tags") or note.get("hashtags"))
    text_parts: list[str] = []
    if title:
        text_parts.append(f"# {title.strip()}")
    if body.strip():
        text_parts.append(sanitize_url_text(body.strip()))
    if tags:
        text_parts.append("Tags: " + ", ".join(tags))

    return ParsedXiaohongshuNote(
        text="\n\n".join(part for part in text_parts if part).strip(),
        title=title,
        author=_extract_author(note),
        published_at=_first_text(note, ("published_at", "publish_time", "created_at", "time")),
        note_id=_first_text(note, ("id", "note_id", "noteId")),
    )


def parse_opencli_xiaohongshu_user_output(
    stdout: str,
    *,
    collection_url: str,
    discovery_provider: str = OPENCLI_XIAOHONGSHU_USER_DISCOVERY_BACKEND,
    include_transient: bool = False,
    include_runtime_note_url: bool = True,
) -> list[ContentItem]:
    text = str(stdout or "").strip()
    if not text:
        raise ValueError("OpenCLI Xiaohongshu user output was empty.")

    payload = load_opencli_json(text)
    rows = _candidate_rows(payload)
    items: list[ContentItem] = []
    seen: set[str] = set()
    for row in rows:
        runtime_note_url = _extract_note_url(row)
        note_id = _first_text(row, ("id", "note_id", "noteId")) or (
            _note_id_from_url(runtime_note_url) if runtime_note_url else None
        )
        note_url = _canonical_xiaohongshu_note_url(note_id)
        if note_url is None or note_id is None or note_id in seen:
            continue
        seen.add(note_id)
        title = _first_text(row, ("title", "note_title", "display_title", "field", "name"))
        media_kind = _xiaohongshu_media_kind(row)
        raw_meta: dict[str, Any] = {
            "collection_url": sanitize_url(collection_url),
            "discovery_provider": discovery_provider,
            "media_kind": media_kind,
            "process_status": "discovered",
            "extraction_contract_version": XIAOHONGSHU_EXTRACTION_CONTRACT_VERSION,
            "required_components": _xiaohongshu_required_components(media_kind),
        }
        if include_transient and include_runtime_note_url:
            raw_runtime_url = row.get("runtime_note_url")
            signed_candidate = (
                raw_runtime_url.strip()
                if isinstance(raw_runtime_url, str) and raw_runtime_url.strip()
                else runtime_note_url
            )
            if signed_candidate and has_required_signed_query(signed_candidate):
                raw_meta["runtime_note_url"] = signed_candidate
        if include_transient:
            for key in (
                "page_count",
                "scroll_count",
                "stable_round_count",
                "empty_snapshot_count",
                "duplicate_snapshot_count",
                "anomaly_count",
                "source_exhausted",
                "explicit_exhaustion",
                "exhaustion_reason",
                "store_source",
            ):
                value = row.get(key)
                if value is not None:
                    raw_meta[key] = value
        published_hint = _first_text(row, ("published_hint", "published_at", "publish_time", "created_at", "time"))
        if published_hint:
            raw_meta["published_hint"] = published_hint
        items.append(
            ContentItem(
                platform="xiaohongshu",
                input_type="collection",
                url=note_url,
                id=note_id,
                title=title,
                author=_extract_author(row),
                published_at=published_hint,
                source_type=media_kind,
                raw_meta=raw_meta,
            )
        )
    return items


def classify_xiaohongshu_failure(message: object) -> str:
    text = str(message or "").lower()
    if "source_not_explicitly_exhausted" in text:
        return "xiaohongshu_source_not_explicitly_exhausted"
    if "timed out" in text or "timeout" in text:
        return "opencli_xiaohongshu_timeout"
    if "login" in text or "cookie" in text or "sign in" in text or "auth_challenge" in text:
        return "xiaohongshu_browser_session_required"
    if any(term in text for term in ("captcha", "verify", "risk", "blocked", "风控", "验证")):
        return "xiaohongshu_request_blocked"
    if "not found" in text or "404" in text:
        return "xiaohongshu_not_found"
    if "signed" in text or _SIGNED_QUERY_KEY in text:
        return "xiaohongshu_signed_url_required"
    return "opencli_xiaohongshu_failed"


def _host_is_xiaohongshu(host: str) -> bool:
    normalized = host.lower()
    return (
        normalized == "xiaohongshu.com"
        or normalized.endswith(".xiaohongshu.com")
        or normalized == "xhslink.com"
        or normalized.endswith(".xhslink.com")
    )


def _first_note_dict(payload: object) -> dict[str, Any] | None:
    if isinstance(payload, dict):
        row_dict = _field_rows_to_dict(payload)
        if row_dict is not None:
            return row_dict
        for key in ("data", "note", "note_info", "note_card", "result"):
            value = payload.get(key)
            if isinstance(value, dict):
                nested = _first_note_dict(value)
                if nested is not None:
                    return nested
        return payload
    if isinstance(payload, list):
        row_dict = _field_rows_to_dict(payload)
        if row_dict is not None:
            return row_dict
        for item in payload:
            if isinstance(item, dict):
                return item
    return None


def _field_rows_to_dict(payload: object) -> dict[str, Any] | None:
    if isinstance(payload, dict):
        payload = payload.get("rows") or payload.get("items")
    if not isinstance(payload, list) or not payload:
        return None
    flattened: dict[str, Any] = {}
    saw_field_row = False
    for row in payload:
        if not isinstance(row, dict):
            return None
        field = row.get("field")
        value = row.get("value")
        if field is None:
            return None
        saw_field_row = True
        key = str(field).strip().lower().replace(" ", "_")
        flattened[key] = value
    return flattened if saw_field_row else None


def _candidate_rows(payload: object) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if not isinstance(payload, dict):
        raise ValueError("OpenCLI Xiaohongshu user output must be JSON object or list.")

    for key in ("items", "notes", "list", "results"):
        value = payload.get(key)
        if isinstance(value, list):
            return [row for row in value if isinstance(row, dict)]

    data = payload.get("data")
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if isinstance(data, dict):
        return _candidate_rows(data)
    row_dict = _field_rows_to_dict(payload)
    if row_dict is not None:
        return [row_dict]
    return [payload]


def _extract_note_url(row: dict[str, Any]) -> str | None:
    for key in ("runtime_note_url", "url", "note_url", "share_url", "link", "href", "value"):
        value = row.get(key)
        if isinstance(value, str):
            candidate = _first_xiaohongshu_url(value)
            if candidate and is_xiaohongshu_note_url(candidate):
                return candidate
    for value in row.values():
        if isinstance(value, str):
            candidate = _first_xiaohongshu_url(value)
            if candidate and is_xiaohongshu_note_url(candidate):
                return candidate
    return None


def _first_xiaohongshu_url(text: str) -> str | None:
    for match in _URL_CANDIDATE_RE.finditer(text):
        candidate = match.group(0).rstrip(".,，。；;！!)）]")
        if _host_is_xiaohongshu(urlparse(candidate).netloc):
            return candidate
    return None


def _first_text(mapping: dict[str, Any], keys: Iterable[str]) -> str | None:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, str) and value.strip():
            return sanitize_url_text(value.strip())
        if value is not None and not isinstance(value, (dict, list, tuple)):
            text = str(value).strip()
            if text:
                return sanitize_url_text(text)
    return None


def _nested_dict(mapping: dict[str, Any], keys: Iterable[str]) -> dict[str, Any] | None:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, dict):
            return value
    return None


def _extract_author(mapping: dict[str, Any]) -> str | None:
    author = _first_text(mapping, ("author", "nickname", "user_name", "username"))
    if author:
        return author
    user = mapping.get("user") or mapping.get("author_info") or mapping.get("user_info")
    if isinstance(user, dict):
        return _first_text(user, ("nickname", "name", "username"))
    return None


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    tags: list[str] = []
    for item in value:
        if isinstance(item, str) and item.strip():
            tags.append(item.strip())
        elif isinstance(item, dict):
            tag = _first_text(item, ("name", "tag", "text"))
            if tag:
                tags.append(tag)
    return tags


def _note_id_from_url(input_value: str) -> str | None:
    parsed = urlparse(input_value)
    parts = [part for part in parsed.path.split("/") if part]
    return parts[-1] if parts else None


def _canonical_xiaohongshu_note_url(note_id: str | None) -> str | None:
    if not note_id or not re.fullmatch(r"[0-9a-fA-F]{24}", note_id):
        return None
    return f"https://www.xiaohongshu.com/discovery/item/{note_id.lower()}"


def _xiaohongshu_media_kind(row: dict[str, Any]) -> str:
    value = _first_text(row, ("media_kind", "type", "note_type", "noteType")) or ""
    normalized = value.lower().replace("-", "_")
    if "video" in normalized:
        return "video"
    if any(term in normalized for term in ("image", "normal", "graphic", "note")):
        return "image_text"
    return "mixed"


def _xiaohongshu_required_components(media_kind: str) -> list[str]:
    if media_kind == "video":
        return ["native_text", "video_transcript"]
    if media_kind == "image_text":
        return ["native_text", "image_ocr"]
    return ["native_text"]


def _failed(
    backend: str,
    code: str,
    error: object,
    *,
    source_notes: list[str] | None = None,
    metadata: dict[str, object] | None = None,
) -> TranscriptResult:
    return TranscriptResult(
        text="",
        status="failed",
        source_level="partial_extraction",
        backend=backend,
        source_notes=source_notes or ["OpenCLI Xiaohongshu provider returned structured failure."],
        error_code=code,
        error_summary=sanitize_error_summary(error),
        metadata=metadata or {},
    )


def _discovery_failure(
    provider: str,
    code: str,
    error: object,
    *,
    source_notes: list[str] | None = None,
    metadata: dict[str, object] | None = None,
) -> DiscoveryResult:
    return DiscoveryResult.failure(
        provider=provider,
        error_code=code,
        error_summary=sanitize_error_summary(error),
        source_notes=source_notes or ["OpenCLI Xiaohongshu discovery returned structured failure."],
        metadata=metadata or {},
    )
