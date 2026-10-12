"""Xiaoyuzhou runtime backend providers.

This module is loaded by ``openmd.backends.registry`` through the platform
extension naming convention. Providers here stay behind ``--enable-browser-
backends`` because both OpenCLI and Podwise rely on local browser/session or
authenticated CLI state. Importing the module does not read credentials, run
OpenCLI, run Podwise, download media, or submit paid processing jobs.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

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
from openmd.core.models import ContentItem, TranscriptResult
from openmd.core.secrets import sanitize_error_summary


OPENCLI_XIAOYUZHOU_TRANSCRIPT_BACKEND = "opencli.xiaoyuzhou.transcript"
OPENCLI_XIAOYUZHOU_EPISODES_DISCOVERY_BACKEND = "opencli.xiaoyuzhou.podcast-episodes"
PODWISE_TRANSCRIPT_BACKEND = "podwise.transcript"
OPENCLI_WEB_READ_BACKEND = "opencli.web.read"

XIAOYUZHOU_CREDENTIALS_PATH = Path.home() / ".opencli" / "xiaoyuzhou.json"
XIAOYUZHOU_ANDROID_DEVICE_PROPERTY_KEYS = frozenset(
    {"uuid", "android_id", "oaid", "vaid", "aaid"}
)
XIAOYUZHOU_IOS_DEVICE_PROPERTY_KEYS = frozenset({"idfa", "idfv"})
XIAOYUZHOU_IOS_REQUEST_METADATA_GROUPS = (
    ("user-agent",),
    ("os",),
    ("os-version",),
    ("manufacturer",),
    ("model",),
    ("market",),
    ("app-version",),
    ("app-buildno",),
    ("bundleid", "applicationid"),
)

PODWISE_EPISODE_RE = re.compile(r"https://podwise\.ai/dashboard/episodes/\d+")
DEFAULT_OPENCLI_TIMEOUT_SECONDS = 180
DEFAULT_PODWISE_SEARCH_TIMEOUT_SECONDS = 120
DEFAULT_PODWISE_GET_TIMEOUT_SECONDS = 180
DEFAULT_MIN_TRANSCRIPT_CHARS = 1

CommandRunner = Callable[..., tuple[int, str, str]]


@dataclass(frozen=True)
class CredentialShapeCheck:
    ok: bool
    code: str
    summary: str


def register_backend_providers(
    registry: BackendProviderRegistry,
    options: BackendRuntimeOptions,
) -> None:
    """Register Xiaoyuzhou extraction providers behind explicit runtime gates."""
    requested = options.backend_override
    if not options.enable_browser_backends:
        return

    if requested in {None, OPENCLI_XIAOYUZHOU_TRANSCRIPT_BACKEND}:
        registry.register(
            OPENCLI_XIAOYUZHOU_TRANSCRIPT_BACKEND,
            OpenCliXiaoyuzhouTranscriptProvider(),
        )
    if requested in {None, PODWISE_TRANSCRIPT_BACKEND}:
        registry.register(PODWISE_TRANSCRIPT_BACKEND, PodwiseTranscriptProvider())


def register_discovery_providers(
    registry: DiscoveryProviderRegistry,
    options: BackendRuntimeOptions,
) -> None:
    """Register podcast episode discovery behind explicit browser/backend gates."""
    requested = options.requested_discovery_provider
    if not options.enable_browser_backends:
        return
    if requested in {None, OPENCLI_XIAOYUZHOU_EPISODES_DISCOVERY_BACKEND}:
        registry.register(
            OPENCLI_XIAOYUZHOU_EPISODES_DISCOVERY_BACKEND,
            OpenCliXiaoyuzhouPodcastEpisodesProvider(),
            platforms=("xiaoyuzhou",),
            default_for_platforms=requested is None,
        )


def build_opencli_xiaoyuzhou_transcript_args(episode_id: str, *, output_dir: str) -> list[str]:
    return [
        "opencli",
        "xiaoyuzhou",
        "transcript",
        episode_id,
        "--output",
        output_dir,
        "--json",
        "false",
        "--text",
        "true",
        "-f",
        "json",
    ]


def build_opencli_xiaoyuzhou_podcast_episodes_args(
    podcast_id: str,
    *,
    limit: int | None,
) -> list[str]:
    command = [
        "opencli",
        "xiaoyuzhou",
        "podcast-episodes",
        podcast_id,
        "-f",
        "json",
    ]
    if limit is not None:
        command.extend(["--limit", str(max(0, limit))])
    return command


def build_opencli_web_read_args(url: str) -> list[str]:
    return ["opencli", "web", "read", "--url", url, "--stdout"]


def build_podwise_search_args(title: str) -> list[str]:
    return ["podwise", "search", "episode", title, "--limit", "5"]


def build_podwise_get_transcript_args(podwise_episode_url: str) -> list[str]:
    return ["podwise", "get", "transcript", podwise_episode_url]


class OpenCliXiaoyuzhouTranscriptProvider:
    """Fetch existing Xiaoyuzhou transcripts through OpenCLI credentials."""

    name = OPENCLI_XIAOYUZHOU_TRANSCRIPT_BACKEND

    def __init__(
        self,
        *,
        runner: CommandRunner | None = None,
        credential_path: Path | None = None,
        timeout: int = DEFAULT_OPENCLI_TIMEOUT_SECONDS,
        min_transcript_chars: int = DEFAULT_MIN_TRANSCRIPT_CHARS,
    ) -> None:
        self._runner = runner or _run_command
        self.credential_path = credential_path or XIAOYUZHOU_CREDENTIALS_PATH
        self.timeout = timeout
        self.min_transcript_chars = max(0, min_transcript_chars)

    def descriptor(self) -> BackendDescriptor:
        capability = BackendCapability(
            platforms=("xiaoyuzhou",),
            source_types=("audio",),
            input_types=("single", "batch", "collection"),
            source_levels=("native_caption", "third_party_extractor", "partial_extraction"),
            requires_network=True,
            requires_credentials=True,
            optional_config=("opencli", "~/.opencli/xiaoyuzhou.json"),
        )
        return BackendDescriptor(
            name=self.name,
            layer="L3 browser/session fallback",
            capabilities=(capability,),
            description="Fetch Xiaoyuzhou transcript text through OpenCLI using local credentials.",
            provider="opencli.xiaoyuzhou",
        )

    def health(self) -> BackendHealth:
        if self._runner is not _run_command:
            return BackendHealth(ok=True, dependencies={"opencli": "injected"})
        if not _executable_available("opencli"):
            return BackendHealth(
                ok=False,
                dependencies={"opencli": "missing"},
                error_code="backend_dependency_missing",
                error_summary="opencli executable is not available on PATH.",
            )
        return BackendHealth(ok=True, dependencies={"opencli": "available"})

    def extract(self, request: BackendExtractRequest) -> TranscriptResult:
        item = request.item
        if item.platform != "xiaoyuzhou":
            return _failed(
                self.name,
                "unsupported_platform",
                f"Xiaoyuzhou provider only supports xiaoyuzhou, got {item.platform}.",
            )

        credential_check = check_xiaoyuzhou_credentials(self.credential_path)
        if not credential_check.ok:
            return _failed(
                self.name,
                credential_check.code,
                credential_check.summary,
                notes=[
                    "Skipped OpenCLI xiaoyuzhou transcript before making an API request.",
                    "No credential values or raw command streams were persisted by Openmd.",
                ],
            )

        episode_id = extract_xiaoyuzhou_episode_id(item)
        if not episode_id:
            return _failed(self.name, "invalid_input", "Could not extract a Xiaoyuzhou episode id.")

        with tempfile.TemporaryDirectory(prefix="openmd-xiaoyuzhou-") as tmp_dir:
            command = build_opencli_xiaoyuzhou_transcript_args(episode_id, output_dir=tmp_dir)
            exit_code, stdout, stderr = self._runner(command, timeout=self.timeout)
            if exit_code != 0:
                return _failed(
                    self.name,
                    classify_opencli_xiaoyuzhou_failure(exit_code, stderr or stdout),
                    stderr or stdout or f"OpenCLI exited with code {exit_code}.",
                    metadata={"exit_code": exit_code, "episode_id": episode_id},
                )

            text = _read_opencli_transcript_text(Path(tmp_dir), stdout).strip()
            if len("".join(text.split())) < self.min_transcript_chars:
                return _failed(
                    self.name,
                    "empty_result",
                    "OpenCLI xiaoyuzhou transcript produced no usable text.",
                    metadata={"episode_id": episode_id},
                )

            return TranscriptResult(
                text=text.rstrip() + "\n",
                status="verified",
                source_level="native_caption",
                backend=self.name,
                title=item.title,
                author=item.author,
                published_at=item.published_at,
                source_notes=[
                    "OpenCLI xiaoyuzhou transcript returned existing transcript text.",
                    "Credential shape was checked locally before the OpenCLI request.",
                    "Generated text was read from a temporary directory; raw command streams were not persisted.",
                    "No media download, ASR, or Podwise paid processing action was attempted.",
                ],
                metadata={"episode_id": episode_id, "strategy": "opencli_transcript"},
            )


class PodwiseTranscriptProvider:
    """Fetch already-processed Podwise transcripts for Xiaoyuzhou episodes."""

    name = PODWISE_TRANSCRIPT_BACKEND

    def __init__(
        self,
        *,
        podwise_runner: CommandRunner | None = None,
        opencli_runner: CommandRunner | None = None,
        search_timeout: int = DEFAULT_PODWISE_SEARCH_TIMEOUT_SECONDS,
        get_timeout: int = DEFAULT_PODWISE_GET_TIMEOUT_SECONDS,
        web_timeout: int = DEFAULT_OPENCLI_TIMEOUT_SECONDS,
        min_transcript_chars: int = DEFAULT_MIN_TRANSCRIPT_CHARS,
    ) -> None:
        self._podwise_runner = podwise_runner or _run_command
        self._opencli_runner = opencli_runner or _run_command
        self.search_timeout = search_timeout
        self.get_timeout = get_timeout
        self.web_timeout = web_timeout
        self.min_transcript_chars = max(0, min_transcript_chars)

    def descriptor(self) -> BackendDescriptor:
        capability = BackendCapability(
            platforms=("xiaoyuzhou",),
            source_types=("audio",),
            input_types=("single", "batch", "collection"),
            source_levels=("third_party_extractor", "partial_extraction"),
            requires_network=True,
            requires_credentials=True,
            optional_config=("podwise", "opencli web read for title lookup"),
        )
        return BackendDescriptor(
            name=self.name,
            layer="L3 third-party transcript fallback",
            capabilities=(capability,),
            description="Fetch already-processed Podwise transcripts; never submits Podwise processing jobs.",
            provider="podwise",
            is_fallback=True,
        )

    def health(self) -> BackendHealth:
        if self._podwise_runner is not _run_command or self._opencli_runner is not _run_command:
            return BackendHealth(ok=True, dependencies={"podwise": "injected", "opencli": "injected"})
        dependencies = {
            "podwise": "available" if _executable_available("podwise") else "missing",
            "opencli": "available" if _executable_available("opencli") else "missing",
        }
        ok = dependencies["podwise"] == "available"
        return BackendHealth(
            ok=ok,
            dependencies=dependencies,
            error_code=None if ok else "backend_dependency_missing",
            error_summary=None if ok else "podwise executable is not available on PATH.",
        )

    def extract(self, request: BackendExtractRequest) -> TranscriptResult:
        item = request.item
        if item.platform != "xiaoyuzhou":
            return _failed(
                self.name,
                "unsupported_platform",
                f"Podwise Xiaoyuzhou fallback only supports xiaoyuzhou, got {item.platform}.",
            )

        podwise_episode_url = extract_podwise_episode_url(item.url)
        notes: list[str] = []
        if podwise_episode_url is None:
            lookup_result = self._find_podwise_episode_url(item.url)
            if isinstance(lookup_result, TranscriptResult):
                return lookup_result
            podwise_episode_url = lookup_result
            notes.append("Podwise episode URL was resolved through OpenCLI web read plus Podwise search.")
        else:
            notes.append("Input already contained a Podwise episode URL.")

        command = build_podwise_get_transcript_args(podwise_episode_url)
        exit_code, stdout, stderr = self._podwise_runner(command, timeout=self.get_timeout)
        if exit_code != 0:
            if _is_podwise_not_processed(stderr or stdout):
                return _failed(
                    self.name,
                    "podwise_episode_not_processed",
                    (
                        "Podwise has not processed this episode yet. Openmd does not run "
                        "`podwise process` automatically because it can consume account credits."
                    ),
                    notes=["Only Podwise search/get are allowed in this fallback path."],
                    metadata={"podwise_episode_url": podwise_episode_url},
                )
            return _failed(
                self.name,
                f"backend_exit_{exit_code}",
                stderr or stdout or f"Podwise exited with code {exit_code}.",
                metadata={"exit_code": exit_code, "podwise_episode_url": podwise_episode_url},
            )

        text = stdout.strip()
        if len("".join(text.split())) < self.min_transcript_chars:
            return _failed(
                self.name,
                "empty_result",
                "Podwise transcript command returned no usable text.",
                metadata={"podwise_episode_url": podwise_episode_url},
            )

        return TranscriptResult(
            text=text.rstrip() + "\n",
            status="verified",
            source_level="third_party_extractor",
            backend=self.name,
            title=item.title,
            author=item.author,
            published_at=item.published_at,
            source_notes=[
                *notes,
                "Podwise transcript fallback was used for Xiaoyuzhou.",
                "Only Podwise search/get were used; Podwise paid processing was not run.",
                "Raw command streams were not persisted by Openmd.",
            ],
            metadata={
                "podwise_episode_url": podwise_episode_url,
                "strategy": "podwise_already_processed_transcript",
            },
        )

    def _find_podwise_episode_url(self, xiaoyuzhou_url: str) -> str | TranscriptResult:
        with tempfile.TemporaryDirectory(prefix="openmd-xiaoyuzhou-web-") as tmp_dir:
            exit_code, stdout, stderr = self._opencli_runner(
                build_opencli_web_read_args(xiaoyuzhou_url),
                timeout=self.web_timeout,
                cwd=tmp_dir,
            )
            if exit_code != 0:
                return _failed(
                    self.name,
                    "podwise_lookup_web_read_failed",
                    stderr or stdout or f"OpenCLI web read exited with code {exit_code}.",
                    metadata={"exit_code": exit_code},
                )
            title = (
                extract_markdown_title(stdout)
                or extract_table_title(stdout)
                or extract_first_markdown_title(Path(tmp_dir))
            )

        if not title:
            return _failed(
                self.name,
                "podwise_lookup_failed",
                "Could not derive a Podwise search title from the Xiaoyuzhou page.",
            )

        exit_code, stdout, stderr = self._podwise_runner(
            build_podwise_search_args(title),
            timeout=self.search_timeout,
        )
        if exit_code != 0:
            return _failed(
                self.name,
                f"podwise_search_exit_{exit_code}",
                stderr or stdout or f"Podwise search exited with code {exit_code}.",
                metadata={"exit_code": exit_code},
            )
        podwise_episode_url = extract_podwise_episode_url(stdout)
        if not podwise_episode_url:
            return _failed(
                self.name,
                "podwise_episode_not_found",
                f"Podwise search did not return an episode URL for the derived title: {title}",
            )
        return podwise_episode_url


class OpenCliXiaoyuzhouPodcastEpisodesProvider:
    """Discover Xiaoyuzhou podcast episodes through OpenCLI."""

    name = OPENCLI_XIAOYUZHOU_EPISODES_DISCOVERY_BACKEND

    def __init__(
        self,
        *,
        runner: CommandRunner | None = None,
        timeout: int = DEFAULT_OPENCLI_TIMEOUT_SECONDS,
    ) -> None:
        self._runner = runner or _run_command
        self.timeout = timeout

    def discover(self, request: DiscoveryRequest) -> DiscoveryResult:
        if request.platform != "xiaoyuzhou":
            return DiscoveryResult.failure(
                provider=self.name,
                error_code="unsupported_platform",
                error_summary=f"Xiaoyuzhou discovery only supports xiaoyuzhou, got {request.platform}.",
            )

        podcast_id = extract_xiaoyuzhou_podcast_id(request.input_url)
        if not podcast_id:
            return DiscoveryResult.failure(
                provider=self.name,
                error_code="invalid_input",
                error_summary="Could not extract a Xiaoyuzhou podcast id.",
                metadata={"platform": request.platform},
            )

        command = build_opencli_xiaoyuzhou_podcast_episodes_args(
            podcast_id,
            limit=request.limit,
        )
        exit_code, stdout, stderr = self._runner(command, timeout=self.timeout)
        if exit_code != 0:
            return DiscoveryResult.failure(
                provider=self.name,
                error_code=f"backend_exit_{exit_code}",
                error_summary=_sanitize_error(stderr or stdout or f"OpenCLI exited with code {exit_code}.")
                or "OpenCLI xiaoyuzhou podcast-episodes failed.",
                source_notes=[
                    "OpenCLI xiaoyuzhou podcast-episodes failed before item extraction.",
                    "Raw command streams were not persisted by Openmd.",
                ],
                metadata={"exit_code": exit_code, "podcast_id": podcast_id},
            )

        items = parse_xiaoyuzhou_episode_items(stdout, podcast_id=podcast_id)
        if request.limit is not None and not request.allow_all:
            items = items[: max(0, request.limit)]
        if not items:
            return DiscoveryResult.failure(
                provider=self.name,
                error_code="empty_discovery_result",
                error_summary="OpenCLI xiaoyuzhou podcast-episodes returned no episodes.",
                metadata={"podcast_id": podcast_id},
            )

        return DiscoveryResult.success(
            provider=self.name,
            items=items,
            source_notes=[
                "OpenCLI xiaoyuzhou podcast-episodes returned episode manifest items.",
                "Manifest discovery does not download media, run ASR, or fetch transcripts.",
            ],
            metadata={
                "podcast_id": podcast_id,
                "normalized_url": f"https://www.xiaoyuzhoufm.com/podcast/{podcast_id}",
            },
        )


def check_xiaoyuzhou_credentials(path: Path = XIAOYUZHOU_CREDENTIALS_PATH) -> CredentialShapeCheck:
    """Validate a local Xiaoyuzhou credential profile without exposing values."""
    if not path.exists():
        return CredentialShapeCheck(
            False,
            "xiaoyuzhou_credentials_missing",
            "Missing Xiaoyuzhou credential file at ~/.opencli/xiaoyuzhou.json.",
        )

    try:
        credentials = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return CredentialShapeCheck(
            False,
            "xiaoyuzhou_credentials_invalid_json",
            "Xiaoyuzhou credential file is not valid JSON.",
        )

    for field in ("access_token", "refresh_token", "device_id"):
        value = credentials.get(field)
        if not isinstance(value, str) or not value.strip():
            return CredentialShapeCheck(
                False,
                "xiaoyuzhou_credentials_incomplete",
                "Xiaoyuzhou credential profile is missing required authentication/device fields.",
            )

    device_id = credentials["device_id"].strip()
    device_properties = credentials.get("device_properties")
    if not isinstance(device_properties, str) or not device_properties.strip():
        return CredentialShapeCheck(
            False,
            "xiaoyuzhou_device_properties_missing",
            (
                "Xiaoyuzhou device_properties is required; capture the real "
                "device properties value and ensure its uuid matches device_id."
            ),
        )

    try:
        parsed_properties = json.loads(device_properties)
    except json.JSONDecodeError:
        return CredentialShapeCheck(
            False,
            "xiaoyuzhou_device_properties_invalid_json",
            "Xiaoyuzhou device_properties must be a JSON string.",
        )

    if not isinstance(parsed_properties, dict):
        return CredentialShapeCheck(
            False,
            "xiaoyuzhou_device_properties_invalid_json",
            "Xiaoyuzhou device_properties must decode to a JSON object.",
        )

    property_keys = set(parsed_properties.keys())
    if XIAOYUZHOU_ANDROID_DEVICE_PROPERTY_KEYS <= property_keys:
        if parsed_properties.get("uuid") != device_id:
            return CredentialShapeCheck(
                False,
                "xiaoyuzhou_device_mismatch",
                "Xiaoyuzhou Android device_properties.uuid must match device_id.",
            )
        return CredentialShapeCheck(
            True,
            "ok",
            "Xiaoyuzhou credential profile passed local shape checks.",
        )

    if XIAOYUZHOU_IOS_DEVICE_PROPERTY_KEYS <= property_keys:
        request_metadata = credentials.get("request_headers") or credentials.get("headers")
        if not isinstance(request_metadata, dict) or not request_metadata:
            return CredentialShapeCheck(
                True,
                "ok",
                (
                    "Xiaoyuzhou credential profile passed local shape checks "
                    "for an OpenCLI-compatible iOS device profile."
                ),
            )

        missing_groups = [
            group
            for group in XIAOYUZHOU_IOS_REQUEST_METADATA_GROUPS
            if _metadata_lookup(request_metadata, group) is None
        ]
        if missing_groups:
            return CredentialShapeCheck(
                False,
                "xiaoyuzhou_request_metadata_incomplete",
                "Xiaoyuzhou iOS profile is missing required same-device request metadata.",
            )

        metadata_device_id = _metadata_lookup(request_metadata, ("x-jike-device-id",))
        if metadata_device_id is not None and metadata_device_id != device_id:
            return CredentialShapeCheck(
                False,
                "xiaoyuzhou_device_mismatch",
                "Xiaoyuzhou same-device request metadata must match device_id.",
            )

        return CredentialShapeCheck(
            True,
            "ok",
            "Xiaoyuzhou credential profile passed local shape checks.",
        )

    android_missing = sorted(XIAOYUZHOU_ANDROID_DEVICE_PROPERTY_KEYS - property_keys)
    ios_missing = sorted(XIAOYUZHOU_IOS_DEVICE_PROPERTY_KEYS - property_keys)
    if android_missing and ios_missing:
        return CredentialShapeCheck(
            False,
            "xiaoyuzhou_device_properties_incomplete",
            (
                "Xiaoyuzhou device_properties must look like either Android "
                f"({', '.join(sorted(XIAOYUZHOU_ANDROID_DEVICE_PROPERTY_KEYS))}) "
                f"or iOS ({', '.join(sorted(XIAOYUZHOU_IOS_DEVICE_PROPERTY_KEYS))})."
            ),
        )

    return CredentialShapeCheck(
        True,
        "ok",
        "Xiaoyuzhou credential profile passed local shape checks.",
    )


def extract_xiaoyuzhou_episode_id(item: ContentItem) -> str | None:
    if item.id:
        return item.id.strip()
    parsed = urlparse(item.url)
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) >= 2 and parts[0] == "episode":
        return parts[1]
    if parts:
        return parts[-1]
    return None


def extract_xiaoyuzhou_podcast_id(input_value: str) -> str | None:
    value = input_value.strip()
    if not value:
        return None
    parsed = urlparse(value if "://" in value else f"https://www.xiaoyuzhoufm.com/podcast/{value}")
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) >= 2 and parts[0] == "podcast":
        return parts[1]
    if parts and parsed.netloc.endswith("xiaoyuzhoufm.com"):
        return parts[-1]
    if "://" not in value and "/" not in value:
        return value
    return None


def parse_xiaoyuzhou_episode_items(stdout: str, *, podcast_id: str) -> list[ContentItem]:
    try:
        payload = json.loads(stdout.strip())
    except json.JSONDecodeError:
        return []
    records = _episode_records_from_payload(payload)
    items: list[ContentItem] = []
    for record in records:
        item = _episode_item_from_record(record, podcast_id=podcast_id)
        if item is not None:
            items.append(item)
    return items


def _episode_records_from_payload(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("episodes", "items", "data", "list", "rows"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            nested = _episode_records_from_payload(value)
            if nested:
                return nested
    return []


def _episode_item_from_record(record: dict[str, Any], *, podcast_id: str) -> ContentItem | None:
    episode_id = _first_text(record, "eid", "episode_id", "id")
    url = _first_text(record, "url", "episode_url", "link")
    if not episode_id and url:
        episode_id = extract_xiaoyuzhou_episode_id(
            ContentItem(platform="xiaoyuzhou", input_type="single", url=url)
        )
    if not episode_id:
        return None
    episode_url = url or f"https://www.xiaoyuzhoufm.com/episode/{episode_id}"
    return ContentItem(
        platform="xiaoyuzhou",
        input_type="collection",
        url=episode_url,
        id=episode_id,
        title=_first_text(record, "title", "name"),
        author=_first_text(record, "podcast_title", "author"),
        published_at=_first_text(record, "published_at", "date", "pub_date"),
        source_type="audio",
        raw_meta={
            "profile": "media",
            "podcast_id": podcast_id,
            "duration": _first_text(record, "duration"),
            "plays": _first_text(record, "plays", "play_count"),
            "caption_status": "unknown",
            "discovery_provider": OPENCLI_XIAOYUZHOU_EPISODES_DISCOVERY_BACKEND,
        },
    )


def _first_text(record: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = record.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def extract_podwise_episode_url(text: str) -> str | None:
    match = PODWISE_EPISODE_RE.search(text)
    return match.group(0) if match else None


def extract_markdown_title(markdown: str) -> str | None:
    for line in markdown.splitlines():
        text = line.strip()
        if text.startswith("# "):
            return text.removeprefix("# ").strip() or None
    return None


def extract_table_title(stdout: str) -> str | None:
    for line in stdout.splitlines():
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if not cells:
            continue
        title = cells[0]
        if title and title.lower() != "title" and set(title) != {"-"}:
            return title
    return None


def extract_first_markdown_title(directory: Path) -> str | None:
    for markdown_file in sorted(directory.rglob("*.md")):
        title = extract_markdown_title(
            markdown_file.read_text(encoding="utf-8", errors="replace")
        )
        if title:
            return title
    return None


def classify_opencli_xiaoyuzhou_failure(exit_code: int, message: object) -> str:
    text = str(message or "").lower()
    if exit_code == 78 and "xiaoyuzhou" in text and "credential" in text:
        return "xiaoyuzhou_credentials_unavailable"
    if "timed out" in text or "timeout" in text:
        return "opencli_xiaoyuzhou_transcript_timeout"
    if "credential" in text or "login required" in text or "unauthorized" in text:
        return "xiaoyuzhou_credentials_unavailable"
    return f"backend_exit_{exit_code}"


def _run_command(
    command: list[str],
    *,
    timeout: int = 120,
    cwd: str | Path | None = None,
) -> tuple[int, str, str]:
    try:
        completed = subprocess.run(
            [_resolve_executable(command[0]), *command[1:]],
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired as exc:
        return 124, _to_text(exc.stdout), f"command timed out after {timeout} seconds."
    except OSError as exc:
        return 127, "", _sanitize_error(exc) or ""
    return completed.returncode, completed.stdout or "", completed.stderr or ""


def _resolve_executable(executable: str) -> str:
    if os.name == "nt" and not Path(executable).suffix:
        for extension in (".cmd", ".bat", ".exe"):
            resolved = shutil.which(f"{executable}{extension}")
            if resolved:
                return resolved
    return shutil.which(executable) or executable


def _executable_available(executable: str) -> bool:
    if shutil.which(executable):
        return True
    if os.name != "nt":
        return False
    return any(shutil.which(f"{executable}{extension}") for extension in (".cmd", ".bat", ".exe"))


def _read_opencli_transcript_text(directory: Path, stdout: str) -> str:
    text_files = sorted(directory.rglob("transcript.txt"))
    if text_files:
        return text_files[0].read_text(encoding="utf-8", errors="replace")
    return _extract_text_from_stdout(stdout)


def _extract_text_from_stdout(stdout: str) -> str:
    stripped = stdout.strip()
    if not stripped:
        return ""
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        return stripped
    return _text_from_json_payload(payload).strip()


def _text_from_json_payload(payload: Any) -> str:
    if isinstance(payload, str):
        return payload
    if isinstance(payload, list):
        lines = [_text_from_json_payload(item).strip() for item in payload]
        return "\n".join(line for line in lines if line)
    if not isinstance(payload, dict):
        return ""

    for key in ("text", "transcript", "markdown", "content"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value

    for key in ("segments", "snippets", "items", "lines"):
        value = payload.get(key)
        if isinstance(value, list):
            lines = [_text_from_json_payload(item).strip() for item in value]
            return "\n".join(line for line in lines if line)
    return ""


def _metadata_lookup(metadata: object, names: tuple[str, ...]) -> str | None:
    if not isinstance(metadata, dict):
        return None
    normalized = {str(key).lower(): value for key, value in metadata.items()}
    for name in names:
        value = normalized.get(name.lower())
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _is_podwise_not_processed(message: object) -> bool:
    text = str(message or "").lower()
    return (
        "not been processed yet" in text
        or "not processed" in text
        or "process this episode" in text
    )


def _failed(
    backend: str,
    code: str,
    error: object,
    *,
    notes: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
) -> TranscriptResult:
    return TranscriptResult(
        text="",
        status="failed",
        source_level="partial_extraction",
        backend=backend,
        source_notes=["backend returned structured failure", *(notes or [])],
        error_code=code,
        error_summary=_sanitize_error(error),
        metadata=metadata or {},
    )


def _sanitize_error(value: object, *, max_length: int = 240) -> str | None:
    if value is None:
        return None
    text = str(value).replace("\r", " ").replace("\n", " ")
    text = re.sub(r"(?i)\bauthorization\b\s+(?:bearer\s+)?[^,;\s]+", "[redacted]", text)
    text = re.sub(r"(?i)\bbearer\s+[^,;\s]+", "[redacted]", text)
    text = re.sub(
        r"(?i)\b[A-Za-z0-9_-]*(?:token|authorization|cookie|headers?)[A-Za-z0-9_-]*\b"
        r"\s*[:=]\s*[^,;\s]+",
        "[redacted]",
        text,
    )
    text = re.sub(
        r"(?i)\b(?:raw|full)[-_ ]?(?:transcript|subtitle|html|response|api_response|media)\b"
        r"\s*[:=]?\s*[^,;\s]*",
        "[redacted]",
        text,
    )
    sanitized = sanitize_error_summary(text, max_length=max_length) or ""
    sanitized = re.sub(
        r"(?i)\b[A-Za-z0-9_-]*(?:token|authorization|cookie|headers?)[A-Za-z0-9_-]*\b",
        "[redacted]",
        sanitized,
    )
    sanitized = re.sub(
        r"(?i)\b(?:raw|full)[-_ ]?(?:transcript|subtitle|html|response|api_response|media)\b",
        "[redacted]",
        sanitized,
    )
    return sanitized


def _to_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)
