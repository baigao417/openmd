"""YouTube collection discovery helpers and OpenCLI browser provider.

The helpers in this module are safe to import and validate without touching the
network. Data API providers and OpenCLI providers only run when explicitly
registered by the CLI, guarded by ``--enable-network-backends`` or
``--enable-browser-backends`` respectively.
"""

from __future__ import annotations

import csv
import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import replace
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request as UrlRequest, urlopen

from openmd.core.opencli_output import load_opencli_json
from openmd.core.detector import extract_first_url
from openmd.core.models import ContentItem
from openmd.core.secrets import sanitize_error_summary

from .base import DiscoveryRequest, DiscoveryResult

from openmd.runtime.interpreters import owned_popen, communicate_interruptibly, terminate_quietly as _terminate_process_tree


YOUTUBE_DISCOVERY_BACKEND = "opencli.youtube.discovery"
YOUTUBE_DATA_API_DISCOVERY_BACKEND = "youtube.data-api.discovery"
YOUTUBE_DATA_API_PLAYLIST_DISCOVERY_BACKEND = "youtube.data-api.playlist.discovery"
YOUTUBE_DATA_API_KEY_ENV = "OPENMD_YOUTUBE_API_KEY"
YOUTUBE_DATA_API_BASE_URL = "https://www.googleapis.com/youtube/v3"
YOUTUBE_COLLECTION_TABS: frozenset[str] = frozenset(
    {"videos", "shorts", "streams", "playlists", "featured"}
)
YOUTUBE_HOSTS: tuple[str, ...] = (
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
)
DEFAULT_OPENCLI_CHANNEL_LIMIT = 20
DEFAULT_OPENCLI_PLAYLIST_LIMIT = 20
DEFAULT_YOUTUBE_DATA_API_LIMIT = 20
DEFAULT_YOUTUBE_DATA_API_TIMEOUT = 30
YOUTUBE_PLAYLIST_DISCOVERY_BACKEND = "opencli.youtube.playlist.discovery"
_URL_CANDIDATE_RE = re.compile(r"https?://[^\s|,)\]]+")
_PLAYLIST_ID_RE = re.compile(r"(?:PL|UU|LL|RD)[A-Za-z0-9_-]{3,}|OLAK5uy_[A-Za-z0-9_-]+")
OPENCLI_CHANNEL_METADATA_FIELDS: frozenset[str] = frozenset(
    {
        "channelid",
        "description",
        "handle",
        "keywords",
        "name",
        "subscribers",
        "url",
        "---",
    }
)
OpenCliRunner = Callable[..., tuple[int, str, str]]
YouTubeDataApiFetcher = Callable[..., tuple[int, str]]


def normalize_youtube_collection_url(input_value: str) -> str | None:
    """Return a canonical YouTube collection URL when the input looks like one."""
    value = extract_first_url(input_value).strip()
    if not value:
        return None

    if _looks_like_playlist_id(value):
        return f"https://www.youtube.com/playlist?list={value}"

    if value.startswith("@"):
        return f"https://www.youtube.com/{value.lstrip('/')}"

    parsed = urlparse(value if "://" in value else f"https://{value}")
    host = parsed.netloc.lower()
    path_parts = [part for part in parsed.path.split("/") if part]
    query = parse_qs(parsed.query)

    if host not in YOUTUBE_HOSTS:
        return None

    if "list" in query and query["list"] and (
        path_parts[:1] in (["playlist"], ["watch"]) or host == "youtu.be"
    ):
        return f"https://www.youtube.com/playlist?list={query['list'][0]}"

    if not path_parts:
        return None

    first = path_parts[0]
    if first.startswith("@"):
        if len(path_parts) == 1:
            return f"https://www.youtube.com/{first}"
        if path_parts[1] in YOUTUBE_COLLECTION_TABS:
            return f"https://www.youtube.com/{first}/{path_parts[1]}"
        return None

    if first in {"channel", "c", "user"} and len(path_parts) >= 2:
        suffix = f"/{path_parts[2]}" if len(path_parts) >= 3 and path_parts[2] in YOUTUBE_COLLECTION_TABS else ""
        return f"https://www.youtube.com/{first}/{path_parts[1]}{suffix}"

    if first == "playlist" and "list" in query and query["list"]:
        return f"https://www.youtube.com/playlist?list={query['list'][0]}"

    return None


def is_youtube_collection_candidate(input_value: str) -> bool:
    """Detect YouTube channel/profile/videos/playlist inputs without network access."""
    return normalize_youtube_collection_url(input_value) is not None


def youtube_data_api_channel_lookup(input_value: str) -> tuple[str, str] | None:
    """Return the YouTube Data API channel lookup parameter for channel inputs."""
    normalized_url = normalize_youtube_collection_url(input_value)
    if normalized_url is None:
        return None

    parsed = urlparse(normalized_url)
    path_parts = [part for part in parsed.path.split("/") if part]
    if not path_parts or path_parts[0] == "playlist":
        return None

    first = path_parts[0]
    if first.startswith("@"):
        return "forHandle", first.lstrip("@")
    if first == "channel" and len(path_parts) >= 2:
        return "id", path_parts[1]
    if first == "user" and len(path_parts) >= 2:
        return "forUsername", path_parts[1]
    return None


def youtube_data_api_playlist_id(input_value: str) -> str | None:
    """Return a safe YouTube playlist id for playlist URL/watch URL/raw id inputs."""
    value = extract_first_url(input_value).strip()
    if _looks_like_playlist_id(value):
        return value

    normalized_url = normalize_youtube_collection_url(value)
    if normalized_url is None:
        return None

    parsed = urlparse(normalized_url)
    path_parts = [part for part in parsed.path.split("/") if part]
    if path_parts[:1] != ["playlist"]:
        return None

    playlist_ids = parse_qs(parsed.query).get("list")
    playlist_id = playlist_ids[0] if playlist_ids and playlist_ids[0] else None
    if playlist_id is None or not _looks_like_playlist_id(playlist_id):
        return None
    return playlist_id


def build_opencli_youtube_channel_args(
    input_value: str,
    *,
    limit: int | None = None,
) -> list[str] | None:
    """Build the bounded OpenCLI channel command for supported channel inputs."""
    target = opencli_youtube_channel_target(input_value)
    if target is None:
        return None
    bounded_limit = DEFAULT_OPENCLI_CHANNEL_LIMIT if limit is None else max(0, limit)
    return ["opencli", "youtube", "channel", target, "--limit", str(bounded_limit), "-f", "json"]


def opencli_youtube_channel_target(input_value: str) -> str | None:
    """Return the OpenCLI target for channel-like inputs; playlists stay unsupported."""
    normalized_url = normalize_youtube_collection_url(input_value)
    if normalized_url is None:
        return None

    parsed = urlparse(normalized_url)
    path_parts = [part for part in parsed.path.split("/") if part]
    if not path_parts or path_parts[0] == "playlist":
        return None

    if path_parts[0].startswith("@"):
        return path_parts[0]

    return normalized_url


def parse_opencli_youtube_channel_json(
    stdout: str,
    *,
    collection_url: str,
    discovery_provider: str = YOUTUBE_DISCOVERY_BACKEND,
) -> list[ContentItem]:
    """Parse OpenCLI's ``youtube channel -f json`` output into ContentItems."""
    payload = load_opencli_json(stdout)
    if not isinstance(payload, list):
        raise ValueError("OpenCLI YouTube channel output must be a JSON list.")

    channel_meta: dict[str, str] = {}
    items: list[ContentItem] = []
    for row in payload:
        if not isinstance(row, dict):
            continue
        field = str(row.get("field") or "").strip()
        value = str(row.get("value") or "").strip()
        field_key = field.lower()
        if field_key in OPENCLI_CHANNEL_METADATA_FIELDS:
            if field_key != "---" and value:
                channel_meta[field_key] = value
            continue

        video_url = _extract_watch_url(value)
        if video_url is None:
            continue

        duration, views, published_hint = _split_opencli_video_value(value, video_url)
        raw_meta: dict[str, Any] = {
            "collection_url": collection_url,
            "discovery_provider": discovery_provider,
        }
        if channel_meta.get("channelid"):
            raw_meta["channel_id"] = channel_meta["channelid"]
        if channel_meta.get("name"):
            raw_meta["channel_title"] = channel_meta["name"]
        if duration:
            raw_meta["duration"] = duration
        if views:
            raw_meta["views"] = views
        if published_hint:
            raw_meta["published_hint"] = published_hint

        items.append(
            ContentItem(
                platform="youtube",
                input_type="collection",
                url=video_url,
                id=_extract_video_id(video_url),
                title=field or None,
                author=channel_meta.get("name"),
                source_type="video",
                raw_meta=raw_meta,
            )
        )
    return items


def opencli_youtube_playlist_target(input_value: str) -> str | None:
    """Return a playlist id target for OpenCLI; channel inputs stay unsupported."""
    value = extract_first_url(input_value).strip()
    if _looks_like_playlist_id(value):
        return value

    normalized_url = normalize_youtube_collection_url(value)
    if normalized_url is None:
        return None

    parsed = urlparse(normalized_url)
    path_parts = [part for part in parsed.path.split("/") if part]
    if path_parts[:1] != ["playlist"]:
        return None

    playlist_ids = parse_qs(parsed.query).get("list")
    return playlist_ids[0] if playlist_ids and playlist_ids[0] else None


def build_opencli_youtube_playlist_args(
    input_value: str,
    *,
    limit: int | None = None,
) -> list[str] | None:
    """Build the bounded OpenCLI playlist command for playlist inputs."""
    target = opencli_youtube_playlist_target(input_value)
    if target is None:
        return None
    bounded_limit = DEFAULT_OPENCLI_PLAYLIST_LIMIT if limit is None else max(0, limit)
    return ["opencli", "youtube", "playlist", target, "--limit", str(bounded_limit), "-f", "json"]


def parse_opencli_youtube_playlist_output(
    stdout: str,
    *,
    collection_url: str,
    discovery_provider: str = YOUTUBE_PLAYLIST_DISCOVERY_BACKEND,
) -> list[ContentItem]:
    """Parse OpenCLI playlist JSON/table/CSV/Markdown output into ContentItems."""
    if not stdout.strip():
        raise ValueError("OpenCLI YouTube playlist output was empty.")

    rows = _playlist_rows_from_json(stdout)
    if rows is None:
        rows = _playlist_rows_from_markdown_table(stdout)
    if rows is None:
        rows = _playlist_rows_from_csv(stdout)
    if rows is None:
        rows = _playlist_rows_from_text(stdout)

    items: list[ContentItem] = []
    seen_ids: set[str] = set()
    for row in rows:
        item = _playlist_row_to_item(
            row,
            collection_url=collection_url,
            discovery_provider=discovery_provider,
        )
        if item is None or not item.id or item.id in seen_ids:
            continue
        seen_ids.add(item.id)
        items.append(item)
    return items


def _run_opencli(
    args: list[str],
    *,
    timeout: int = 120,
    stdin_text: str | None = None,
) -> tuple[int, str, str]:
    """Run OpenCLI without printing or persisting raw command output."""
    process: subprocess.Popen[str] | None = None
    try:
        command = [_resolve_executable(args[0]), *args[1:]]
        popen_args: list[str] | str = command
        if _is_windows_batch_file(command[0]):
            popen_args = _windows_batch_command_line(command)
        process = owned_popen(
            popen_args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=_subprocess_creation_flags(),
            start_new_session=os.name != "nt",
        )
        stdout, stderr = communicate_interruptibly(process, input=stdin_text, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        if process is not None:
            _terminate_process_tree(process)
            try:
                stdout, stderr = communicate_interruptibly(process, timeout=5)
            except subprocess.TimeoutExpired:
                stdout, stderr = exc.stdout, exc.stderr
        else:
            stdout, stderr = exc.stdout, exc.stderr
        return 124, _to_text(stdout), f"OpenCLI command timed out after {timeout} seconds."
    except OSError as exc:
        return 127, "", sanitize_error_summary(exc)
    return process.returncode or 0, stdout or "", stderr or ""


def _is_windows_batch_file(executable: str) -> bool:
    return os.name == "nt" and Path(executable).suffix.lower() in {".cmd", ".bat"}


def _windows_batch_command_line(command: list[str]) -> str:
    """Return a quoted command line for Windows batch shims.

    ``subprocess.Popen([...])`` does not quote ``&``-bearing arguments for
    ``.cmd``/``.bat`` shims on Windows, so signed URLs can be split by
    ``cmd.exe`` before OpenCLI receives them.
    """
    return " ".join(_quote_windows_batch_arg(argument) for argument in command)


def _quote_windows_batch_arg(argument: str) -> str:
    text = str(argument)
    if not text:
        return '""'
    return '"' + text.replace('"', r"\"") + '"'


def _subprocess_creation_flags() -> int:
    if os.name != "nt":
        return 0
    return getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)


def _run_youtube_data_api(
    endpoint: str,
    params: dict[str, str],
    *,
    timeout: int = DEFAULT_YOUTUBE_DATA_API_TIMEOUT,
) -> tuple[int, str]:
    """Call YouTube Data API without printing request or response bodies."""
    url = f"{YOUTUBE_DATA_API_BASE_URL}/{endpoint}?{urlencode(params)}"
    try:
        request = UrlRequest(url)
        with urlopen(request, timeout=timeout) as response:
            return response.status, _to_text(response.read())
    except HTTPError as exc:
        return exc.code, _to_text(exc.read())
    except (OSError, URLError) as exc:
        return 0, sanitize_error_summary(exc) or ""


def _resolve_executable(executable: str) -> str:
    if os.name == "nt" and not Path(executable).suffix:
        for extension in (".cmd", ".bat", ".exe"):
            resolved = shutil.which(f"{executable}{extension}")
            if resolved:
                return resolved
    return shutil.which(executable) or executable


class OpenCliYouTubeChannelProvider:
    """Opt-in OpenCLI browser-context provider for YouTube channel discovery."""

    name = YOUTUBE_DISCOVERY_BACKEND

    def __init__(
        self,
        *,
        runner: OpenCliRunner | None = None,
        timeout: int = 120,
    ) -> None:
        self._runner = runner or _run_opencli
        self.timeout = timeout

    def discover(self, request: DiscoveryRequest) -> DiscoveryResult:
        normalized_url = normalize_youtube_collection_url(request.input_url)
        if request.platform != "youtube" or normalized_url is None:
            return self._failure(
                error_code="youtube_collection_input_not_supported",
                error_summary="Input is not a supported YouTube collection/channel/playlist URL.",
                source_notes=[
                    "OpenCLI YouTube browser-context discovery did not run because the input is unsupported",
                ],
                metadata={"platform": request.platform},
            )

        if request.allow_all:
            return self._failure(
                error_code="youtube_discovery_all_not_supported",
                error_summary="OpenCLI YouTube channel discovery requires a bounded --limit; --all is not supported.",
                source_notes=["OpenCLI YouTube browser-context discovery did not run unbounded collection discovery"],
                metadata={"platform": request.platform, "normalized_url": normalized_url},
            )

        command = build_opencli_youtube_channel_args(normalized_url, limit=request.limit)
        if command is None:
            return self._failure(
                error_code="youtube_collection_input_not_supported",
                error_summary="OpenCLI YouTube channel discovery supports channel/profile URLs; playlist discovery is not implemented.",
                source_notes=["OpenCLI YouTube browser-context discovery did not run for unsupported playlist input"],
                metadata={"platform": request.platform, "normalized_url": normalized_url},
            )

        exit_code, stdout, stderr = self._runner(command, timeout=self.timeout)
        metadata = {
            "platform": request.platform,
            "normalized_url": normalized_url,
            "limit": DEFAULT_OPENCLI_CHANNEL_LIMIT if request.limit is None else max(0, request.limit),
            "requires_browser_context": True,
        }
        if exit_code != 0:
            return self._failure(
                error_code="opencli_youtube_channel_failed",
                error_summary=sanitize_error_summary(stderr or stdout or f"OpenCLI exited with code {exit_code}."),
                source_notes=["OpenCLI YouTube browser-context discovery returned a command failure"],
                metadata={**metadata, "exit_code": exit_code},
            )

        try:
            items = parse_opencli_youtube_channel_json(
                stdout,
                collection_url=normalized_url,
                discovery_provider=self.name,
            )
        except (json.JSONDecodeError, ValueError) as exc:
            return self._failure(
                error_code="opencli_youtube_channel_invalid_json",
                error_summary=sanitize_error_summary(exc),
                source_notes=["OpenCLI YouTube browser-context discovery stdout was not valid channel JSON"],
                metadata=metadata,
            )

        if not items:
            return self._failure(
                error_code="opencli_youtube_channel_empty",
                error_summary="OpenCLI YouTube channel discovery returned no video items.",
                source_notes=["OpenCLI YouTube browser-context discovery returned no parseable watch URLs"],
                metadata=metadata,
            )

        return DiscoveryResult.success(
            provider=self.name,
            items=items,
            source_notes=[
                "OpenCLI browser-context discovery resolved YouTube channel videos.",
                "Browser cookies/session remain inside the local browser context; Openmd did not export cookies.",
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
            error_summary=sanitize_error_summary(error_summary),
            source_notes=source_notes or ["OpenCLI YouTube browser-context discovery returned structured failure"],
            metadata=metadata or {},
        )


class OpenCliYouTubePlaylistProvider:
    """Opt-in OpenCLI browser-context provider for YouTube playlist discovery."""

    name = YOUTUBE_PLAYLIST_DISCOVERY_BACKEND

    def __init__(
        self,
        *,
        runner: OpenCliRunner | None = None,
        timeout: int = 120,
    ) -> None:
        self._runner = runner or _run_opencli
        self.timeout = timeout

    def discover(self, request: DiscoveryRequest) -> DiscoveryResult:
        normalized_url = normalize_youtube_collection_url(request.input_url)
        if request.platform != "youtube" or normalized_url is None:
            return self._failure(
                error_code="youtube_playlist_input_not_supported",
                error_summary="Input is not a supported YouTube playlist URL.",
                source_notes=["OpenCLI YouTube playlist discovery did not run because the input is unsupported"],
                metadata={"platform": request.platform},
            )

        if request.allow_all:
            return self._failure(
                error_code="youtube_playlist_discovery_all_not_supported",
                error_summary="OpenCLI YouTube playlist discovery requires a bounded --limit; --all is not supported.",
                source_notes=["OpenCLI YouTube playlist discovery did not run unbounded collection discovery"],
                metadata={"platform": request.platform, "normalized_url": normalized_url},
            )

        command = build_opencli_youtube_playlist_args(normalized_url, limit=request.limit)
        if command is None:
            return self._failure(
                error_code="youtube_playlist_input_not_supported",
                error_summary="OpenCLI YouTube playlist discovery supports playlist URLs only.",
                source_notes=["OpenCLI YouTube playlist discovery did not run for unsupported channel input"],
                metadata={"platform": request.platform, "normalized_url": normalized_url},
            )

        limit = DEFAULT_OPENCLI_PLAYLIST_LIMIT if request.limit is None else max(0, request.limit)
        playlist_id = opencli_youtube_playlist_target(normalized_url)
        metadata = {
            "platform": request.platform,
            "normalized_url": normalized_url,
            "playlist_id": playlist_id,
            "limit": limit,
            "requires_browser_context": True,
        }
        exit_code, stdout, stderr = self._runner(command, timeout=self.timeout)
        if exit_code != 0:
            error = stderr or stdout or f"OpenCLI exited with code {exit_code}."
            return self._failure(
                error_code=classify_youtube_playlist_discovery_failure(error),
                error_summary=sanitize_error_summary(error),
                source_notes=["OpenCLI YouTube playlist discovery returned a command failure"],
                metadata={**metadata, "exit_code": exit_code},
            )

        try:
            items = parse_opencli_youtube_playlist_output(
                stdout,
                collection_url=normalized_url,
                discovery_provider=self.name,
            )[:limit]
        except (json.JSONDecodeError, ValueError) as exc:
            return self._failure(
                error_code="opencli_youtube_playlist_invalid_output",
                error_summary=sanitize_error_summary(exc),
                source_notes=["OpenCLI YouTube playlist discovery stdout had an unexpected shape"],
                metadata=metadata,
            )

        if not items:
            return self._failure(
                error_code="opencli_youtube_playlist_empty",
                error_summary="OpenCLI YouTube playlist discovery returned no video items.",
                source_notes=["OpenCLI YouTube playlist discovery returned no parseable watch URLs"],
                metadata=metadata,
            )

        return DiscoveryResult.success(
            provider=self.name,
            items=items,
            source_notes=[
                "OpenCLI browser-context discovery resolved YouTube playlist videos.",
                "Discovery stayed bounded by --limit and did not extract transcripts.",
                "Browser cookies/session remain inside the local browser context; Openmd did not export cookies.",
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
            error_summary=sanitize_error_summary(error_summary),
            source_notes=source_notes or ["OpenCLI YouTube playlist discovery returned structured failure"],
            metadata=metadata or {},
        )


class YouTubeDataApiChannelProvider:
    """Opt-in Data API discovery with explicit exhaustion and finite budgets."""

    name = YOUTUBE_DATA_API_DISCOVERY_BACKEND
    empty_error_code = "youtube_data_api_channel_empty"

    def __init__(self, *, api_key: str | None = None,
                 fetcher: YouTubeDataApiFetcher | None = None,
                 timeout: int = DEFAULT_YOUTUBE_DATA_API_TIMEOUT,
                 max_pages: int = 100, max_requests: int = 205,
                 request_interval: float = 0.1,
                 sleeper: Callable[[float], None] = time.sleep) -> None:
        if max_pages < 1 or max_requests < 1 or request_interval < 0:
            raise ValueError("YouTube discovery budgets must be positive and interval nonnegative")
        self.api_key = api_key if api_key is not None else os.environ.get(YOUTUBE_DATA_API_KEY_ENV)
        self._fetcher = fetcher or _run_youtube_data_api
        self.timeout = timeout
        self.max_pages = max_pages
        self.max_requests = max_requests
        self.request_interval = request_interval
        self._sleep = sleeper

    def _metadata(self, request: DiscoveryRequest, normalized_url: str) -> dict[str, Any]:
        return {"platform": request.platform, "normalized_url": normalized_url,
                "limit": None if request.allow_all else DEFAULT_YOUTUBE_DATA_API_LIMIT if request.limit is None else max(0, request.limit),
                "requires_network": True, "api_key_env": YOUTUBE_DATA_API_KEY_ENV,
                "source_exhausted": False, "page_count": 0, "request_count": 0,
                "max_pages": self.max_pages, "max_requests": self.max_requests,
                "playlist_item_count": 0, "duplicate_item_count": 0,
                "resume_policy": "restart_discovery_and_reuse_verified_packages"}

    def discover(self, request: DiscoveryRequest) -> DiscoveryResult:
        normalized_url = normalize_youtube_collection_url(request.input_url)
        lookup = youtube_data_api_channel_lookup(request.input_url)
        if request.platform != "youtube" or normalized_url is None or lookup is None:
            return self._failure(error_code="youtube_data_api_input_not_supported",
                                 error_summary="Data API channel discovery requires a channel, handle or legacy user URL.")
        metadata = self._metadata(request, normalized_url)
        if not self.api_key:
            return self._failure(error_code="youtube_data_api_key_missing",
                                 error_summary=f"YouTube Data API key is required via {YOUTUBE_DATA_API_KEY_ENV}.", metadata=metadata)
        lookup_key, lookup_value = lookup
        payload, failure = self._request_json("channels", {"part": "snippet,contentDetails", lookup_key: lookup_value},
                failure_code="youtube_data_api_channel_request_failed", metadata=metadata)
        if failure is not None:
            return failure
        channel = _first_item(payload)
        if channel is None:
            return self._failure(error_code="youtube_data_api_channel_not_found", error_summary="No matching channel.", metadata=metadata)
        playlist_id = _uploads_playlist_id(channel)
        if not playlist_id:
            return self._failure(error_code="youtube_data_api_uploads_playlist_missing", error_summary="Channel has no uploads playlist.", metadata=metadata)
        metadata.update(channel_id=str(channel.get("id") or ""), channel_title=_optional_str(_as_dict(channel.get("snippet")).get("title")), uploads_playlist_id=playlist_id)
        return self._discover_playlist(playlist_id, request=request, metadata=metadata)

    def _discover_playlist(self, playlist_id: str, *, request: DiscoveryRequest, metadata: dict[str, Any]) -> DiscoveryResult:
        metadata["playlist_id"] = playlist_id
        records, failure = self._playlist_records(playlist_id, limit=metadata["limit"], metadata=metadata)
        enriched: dict[str, dict[str, Any]] = {}
        if failure is None and records:
            enriched, failure = self._video_metadata([record["video_id"] for record in records], metadata=metadata)
        items = _data_api_records_to_items(records, enriched, collection_url=metadata["normalized_url"],
                channel_id=metadata.get("channel_id", ""), channel_title=metadata.get("channel_title"),
                uploads_playlist_id=playlist_id, discovery_provider=self.name)
        metadata["item_count"] = len(items)
        if failure is not None:
            return replace(failure, items=items, metadata={**metadata, **failure.metadata, "item_count": len(items)})
        if not records and not (request.allow_all and metadata["source_exhausted"]):
            return self._failure(error_code=self.empty_error_code, error_summary="YouTube Data API returned no video items.", metadata=metadata)
        return DiscoveryResult.success(provider=self.name, items=items, metadata=dict(metadata),
            source_notes=["YouTube Data API resolved playlist video records; transcripts were not extracted.",
                          "Pagination completion is reported separately from extracted content coverage.",
                          "No browser cookies or session state were used."])

    def _playlist_records(self, playlist_id: str, *, limit: int | None, metadata: dict[str, Any]) -> tuple[list[dict[str, str]], DiscoveryResult | None]:
        records: list[dict[str, str]] = []
        next_token: str | None = None
        seen_tokens: set[str] = set()
        seen_ids: set[str] = set()
        def stop(code: str) -> tuple[list[dict[str, str]], DiscoveryResult]:
            return records, self._failure(error_code=code, error_summary="YouTube pagination stopped before safe source exhaustion.", metadata=metadata)
        while limit is None or len(records) < limit:
            if metadata["page_count"] >= self.max_pages:
                return stop("youtube_data_api_page_budget_exceeded")
            size = 50 if limit is None else min(50, limit - len(records))
            params = {"part": "snippet,contentDetails", "playlistId": playlist_id, "maxResults": str(size)}
            if next_token:
                params["pageToken"] = next_token
            payload, failure = self._request_json("playlistItems", params, failure_code="youtube_data_api_playlist_request_failed", metadata=metadata)
            if failure is not None:
                metadata["stop_reason"] = failure.error_code
                return records, replace(failure, metadata=dict(metadata))
            metadata["page_count"] += 1
            rows = payload.get("items")
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                return stop("youtube_data_api_invalid_items")
            token = payload.get("nextPageToken")
            if token is not None and (not isinstance(token, str) or not token.strip()):
                return stop("youtube_data_api_invalid_cursor")
            if not rows and token:
                return stop("youtube_data_api_empty_page_with_cursor")
            before = len(records)
            for item in rows:
                metadata["playlist_item_count"] += 1
                video_id = _playlist_video_id(item)
                if not video_id:
                    return stop("youtube_data_api_missing_video_id")
                if video_id in seen_ids:
                    metadata["duplicate_item_count"] += 1
                    continue
                seen_ids.add(video_id)
                snippet = _as_dict(item.get("snippet"))
                details = _as_dict(item.get("contentDetails"))
                records.append({"video_id": video_id, "title": _optional_str(snippet.get("title")) or "",
                    "published_at": _optional_str(snippet.get("publishedAt")) or "",
                    "video_published_at": _optional_str(details.get("videoPublishedAt")) or "",
                    "channel_title": _optional_str(snippet.get("videoOwnerChannelTitle")) or _optional_str(snippet.get("channelTitle")) or "",
                    "playlist_channel_title": _optional_str(snippet.get("channelTitle")) or ""})
                if limit is not None and len(records) >= limit:
                    break
            if rows and len(records) == before:
                return stop("youtube_data_api_repeated_page")
            if token and token in seen_tokens:
                return stop("youtube_data_api_cursor_loop")
            if not token:
                metadata.update(source_exhausted=True, stop_reason="source_exhausted")
                return records, None
            seen_tokens.add(token)
            next_token = token
        metadata["stop_reason"] = "limit_reached"
        return records, None

    def _video_metadata(self, video_ids: list[str], *, metadata: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], DiscoveryResult | None]:
        enriched: dict[str, dict[str, Any]] = {}
        for chunk in _chunks(video_ids, 50):
            payload, failure = self._request_json("videos", {"part": "snippet,contentDetails,statistics,status", "id": ",".join(chunk)},
                    failure_code="youtube_data_api_videos_request_failed", metadata=metadata)
            if failure is not None:
                return enriched, failure
            for item in _items(payload):
                video_id = _optional_str(item.get("id"))
                if video_id:
                    enriched[video_id] = item
        return enriched, None

    def _request_json(self, endpoint: str, params: dict[str, str], *, failure_code: str, metadata: dict[str, Any]) -> tuple[dict[str, Any], DiscoveryResult | None]:
        if metadata["request_count"] >= self.max_requests:
            return {}, self._failure(error_code="youtube_data_api_request_budget_exceeded", error_summary="YouTube request budget exhausted.", metadata=metadata)
        try:
            if metadata["request_count"] and self.request_interval:
                self._sleep(self.request_interval)
            metadata["request_count"] += 1
            status, body = self._fetcher(endpoint, {**params, "key": self.api_key or ""}, timeout=self.timeout)
        except (Exception, KeyboardInterrupt) as exc:
            code = "youtube_data_api_interrupted" if isinstance(exc, KeyboardInterrupt) else failure_code
            return {}, self._failure(error_code=code, error_summary="YouTube request interrupted or transport failed.", metadata=metadata)
        if status != 200:
            return {}, self._failure(error_code=self._classify_http_failure(body, status=status, default=failure_code), error_summary=_youtube_data_api_error_summary(body, status=status, api_key=self.api_key),
                                    metadata={**metadata, "endpoint": endpoint, "status_code": status})
        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, TypeError):
            payload = None
        if not isinstance(payload, dict):
            return {}, self._failure(error_code="youtube_data_api_invalid_json", error_summary="YouTube response must be a valid JSON object.", metadata=metadata)
        return payload, None

    @staticmethod
    def _classify_http_failure(body: str, *, status: int, default: str) -> str:
        return default

    def _failure(self, *, error_code: str, error_summary: object, metadata: dict[str, Any] | None = None) -> DiscoveryResult:
        return DiscoveryResult.failure(provider=self.name, error_code=error_code,
            error_summary=sanitize_error_summary(error_summary),
            source_notes=["YouTube Data API discovery returned structured failure"],
            metadata={**(metadata or {}), "stop_reason": error_code})


class YouTubeDataApiPlaylistProvider(YouTubeDataApiChannelProvider):
    """Opt-in Data API playlist discovery; shares channel uploads pagination."""

    name = YOUTUBE_DATA_API_PLAYLIST_DISCOVERY_BACKEND
    empty_error_code = "youtube_data_api_playlist_empty"

    @staticmethod
    def _classify_http_failure(body: str, *, status: int, default: str) -> str:
        return _classify_youtube_data_api_playlist_failure(body, status=status, default=default)

    def discover(self, request: DiscoveryRequest) -> DiscoveryResult:
        playlist_id = youtube_data_api_playlist_id(request.input_url)
        normalized_url = normalize_youtube_collection_url(request.input_url)
        if request.platform != "youtube" or not playlist_id or not normalized_url:
            return self._failure(error_code="youtube_data_api_playlist_input_not_supported", error_summary="Input must identify a YouTube playlist.")
        metadata = self._metadata(request, normalized_url)
        if not self.api_key:
            return self._failure(error_code="youtube_data_api_key_missing", error_summary=f"YouTube Data API key is required via {YOUTUBE_DATA_API_KEY_ENV}.", metadata=metadata)
        return self._discover_playlist(playlist_id, request=request, metadata=metadata)


def _extract_watch_url(value: str) -> str | None:
    return _canonical_watch_url_from_text(value)


def _data_api_records_to_items(
    records: list[dict[str, str]],
    video_metadata: dict[str, dict[str, Any]],
    *,
    collection_url: str,
    channel_id: str,
    channel_title: str | None,
    uploads_playlist_id: str,
    discovery_provider: str,
) -> list[ContentItem]:
    items: list[ContentItem] = []
    for record in records:
        video_id = record["video_id"]
        metadata = video_metadata.get(video_id, {})
        snippet = _as_dict(metadata.get("snippet"))
        content_details = _as_dict(metadata.get("contentDetails"))
        statistics = _as_dict(metadata.get("statistics"))
        status = _as_dict(metadata.get("status"))

        title = _optional_str(snippet.get("title")) or record.get("title") or None
        item_channel_title = channel_title or _optional_str(snippet.get("channelTitle")) or record.get("channel_title") or None
        published_at = (
            _optional_str(snippet.get("publishedAt"))
            or record.get("video_published_at")
            or record.get("published_at")
            or None
        )
        raw_meta: dict[str, Any] = {
            "collection_url": collection_url,
            "discovery_provider": discovery_provider,
            "playlist_id": uploads_playlist_id,
            "caption_status": "unknown",
            "process_status": "discovered",
        }
        if channel_id:
            raw_meta["channel_id"] = channel_id
            raw_meta["uploads_playlist_id"] = uploads_playlist_id
        if item_channel_title:
            raw_meta["channel_title"] = item_channel_title
        playlist_channel_title = record.get("playlist_channel_title")
        if playlist_channel_title:
            raw_meta["playlist_channel_title"] = playlist_channel_title
        duration = _optional_str(content_details.get("duration"))
        if duration:
            raw_meta["duration"] = duration
        view_count = _optional_str(statistics.get("viewCount"))
        if view_count:
            raw_meta["view_count"] = view_count
        privacy_status = _optional_str(status.get("privacyStatus"))
        if privacy_status:
            raw_meta["privacy_status"] = privacy_status

        items.append(
            ContentItem(
                platform="youtube",
                input_type="collection",
                url=f"https://www.youtube.com/watch?v={video_id}",
                id=video_id,
                title=title,
                author=item_channel_title,
                published_at=published_at,
                source_type="video",
                raw_meta=raw_meta,
            )
        )
    return items


def classify_youtube_playlist_discovery_failure(message: object) -> str:
    """Map known OpenCLI playlist discovery blockers to stable failure codes."""
    text = str(message or "").lower()
    if "empty_result" in text or "returned no data" in text or "no data" in text:
        return "opencli_youtube_playlist_empty"
    if "timed out" in text or "timeout" in text:
        return "opencli_youtube_playlist_timeout"
    if "confirm you're not a bot" in text or "not a bot" in text or "bot check" in text:
        return "youtube_bot_check"
    if "cookie" in text or "cookies" in text or "login required" in text or "log in" in text or "sign in" in text:
        return "youtube_cookie_or_auth_required"
    if "private" in text or "unavailable" in text or "not found" in text:
        return "youtube_playlist_not_found"
    if "blocked" in text:
        return "youtube_request_blocked"
    return "opencli_youtube_playlist_failed"


def _playlist_rows_from_json(stdout: str) -> list[dict[str, Any]] | None:
    try:
        payload = load_opencli_json(stdout)
    except json.JSONDecodeError:
        return None
    rows = _json_video_rows(payload)
    if rows is None:
        raise ValueError("OpenCLI YouTube playlist JSON output must contain video rows.")
    return rows


def _json_video_rows(payload: Any) -> list[dict[str, Any]] | None:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict) and _row_has_video_reference(row)]
    if not isinstance(payload, dict):
        return None
    if _row_has_video_reference(payload):
        return [payload]
    for key in ("items", "videos", "entries", "rows", "data", "playlist"):
        value = payload.get(key)
        rows = _json_video_rows(value)
        if rows is not None:
            return rows
    return None


def _playlist_rows_from_csv(stdout: str) -> list[dict[str, Any]] | None:
    try:
        reader = csv.DictReader(stdout.splitlines())
        rows = [dict(row) for row in reader if row]
    except csv.Error:
        return None
    if not rows or not reader.fieldnames:
        return None
    return rows if any(_row_has_video_reference(row) for row in rows) else None


def _playlist_rows_from_markdown_table(stdout: str) -> list[dict[str, Any]] | None:
    table_lines = [line.strip() for line in stdout.splitlines() if "|" in line]
    if len(table_lines) < 2:
        return None

    header = _markdown_cells(table_lines[0])
    if len(header) < 2:
        return None

    body_lines = table_lines[1:]
    if body_lines and all(set(cell) <= {"-", ":", " "} for cell in _markdown_cells(body_lines[0])):
        body_lines = body_lines[1:]

    rows: list[dict[str, Any]] = []
    for line in body_lines:
        cells = _markdown_cells(line)
        if len(cells) < len(header):
            continue
        rows.append(dict(zip(header, cells[: len(header)])))
    return rows if rows else None


def _markdown_cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _playlist_rows_from_text(stdout: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        video_url = _canonical_watch_url_from_text(line)
        if video_url is None:
            continue
        title = line.split(video_url, 1)[0].strip(" -|,\t") or None
        rows.append({"title": title, "url": video_url, "raw": line})
    return rows


def _playlist_row_to_item(
    row: dict[str, Any],
    *,
    collection_url: str,
    discovery_provider: str,
) -> ContentItem | None:
    normalized = {_normalize_key(key): value for key, value in row.items()}
    video_url = _row_video_url(normalized)
    video_id = _row_video_id(normalized, video_url=video_url)
    if video_url is None and video_id:
        video_url = f"https://www.youtube.com/watch?v={video_id}"
    if video_url is None:
        return None
    video_id = video_id or _extract_video_id(video_url)

    title = _row_title(normalized, video_url=video_url)
    author = _first_present(normalized, "author", "channel", "channel_title", "uploader", "owner")
    published_at = _first_present(normalized, "published_at", "publishedat", "date")
    raw_meta: dict[str, Any] = {
        "collection_url": collection_url,
        "discovery_provider": discovery_provider,
        "caption_status": "unknown",
        "process_status": "discovered",
    }
    playlist_id = opencli_youtube_playlist_target(collection_url)
    if playlist_id:
        raw_meta["playlist_id"] = playlist_id
    if author:
        raw_meta["channel_title"] = author
    for output_key, input_keys in {
        "duration": ("duration", "length"),
        "views": ("views", "view_count", "viewcount"),
        "published_hint": ("published_hint", "published", "age"),
        "rank": ("rank", "index", "position", "#"),
    }.items():
        value = _first_present(normalized, *input_keys)
        if value:
            raw_meta[output_key] = value

    return ContentItem(
        platform="youtube",
        input_type="collection",
        url=video_url,
        id=video_id,
        title=title,
        author=author,
        published_at=published_at,
        source_type="video",
        raw_meta=raw_meta,
    )


def _row_has_video_reference(row: dict[str, Any]) -> bool:
    values = [str(value or "") for value in row.values()]
    if any(_canonical_watch_url_from_text(value) for value in values):
        return True
    video_id = _first_present({_normalize_key(key): value for key, value in row.items()}, "video_id", "videoid")
    return bool(video_id and not str(video_id).startswith("PL"))


def _row_video_url(row: dict[str, Any]) -> str | None:
    for key in ("url", "video_url", "videourl", "link", "href", "watch_url", "watchurl"):
        value = row.get(key)
        if value:
            canonical = _canonical_watch_url_from_text(str(value))
            if canonical:
                return canonical
    for value in row.values():
        canonical = _canonical_watch_url_from_text(str(value or ""))
        if canonical:
            return canonical
    return None


def _row_video_id(row: dict[str, Any], *, video_url: str | None) -> str | None:
    url_video_id = _extract_video_id(video_url) if video_url else None
    if url_video_id:
        return url_video_id
    for key in ("video_id", "videoid"):
        value = _optional_str(row.get(key))
        if value and not value.startswith("PL"):
            return value
    return None


def _row_title(row: dict[str, Any], *, video_url: str) -> str | None:
    for key in ("title", "name", "video_title", "videotitle"):
        value = _optional_str(row.get(key))
        if value:
            return value
    field = _optional_str(row.get("field"))
    if field and not field.isdigit() and _canonical_watch_url_from_text(field) is None:
        return field
    value = _optional_str(row.get("value") or row.get("raw"))
    if value:
        cleaned = value.replace(video_url, "")
        parts = [part.strip(" -|,\t") for part in cleaned.split("|")]
        title = next((part for part in parts if part and "view" not in part.lower()), None)
        if title:
            return title
    return None


def _first_present(row: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = _optional_str(row.get(key))
        if value:
            return value
    return None


def _normalize_key(key: object) -> str:
    return str(key or "").strip().lower().replace(" ", "_").replace("-", "_")


def _canonical_watch_url_from_text(value: str) -> str | None:
    for match in _URL_CANDIDATE_RE.finditer(value):
        canonical = _canonical_watch_url(match.group(0))
        if canonical:
            return canonical
    return _canonical_watch_url(value)


def _canonical_watch_url(candidate: str) -> str | None:
    parsed = urlparse(candidate.strip())
    host = parsed.netloc.lower()
    path_parts = [part for part in parsed.path.split("/") if part]
    if host in YOUTUBE_HOSTS and path_parts[:1] == ["watch"]:
        video_ids = parse_qs(parsed.query).get("v")
        if video_ids and video_ids[0]:
            return f"https://www.youtube.com/watch?v={video_ids[0]}"
    if host == "youtu.be" and path_parts:
        return f"https://www.youtube.com/watch?v={path_parts[0]}"
    return None


def _looks_like_playlist_id(value: str) -> bool:
    return _PLAYLIST_ID_RE.fullmatch(value) is not None and "://" not in value and "/" not in value


def _classify_youtube_data_api_playlist_failure(body: str, *, status: int, default: str) -> str:
    text = body.lower()
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, dict):
        error = _as_dict(payload.get("error"))
        message = _optional_str(error.get("message")) or ""
        errors = error.get("errors")
        reasons = []
        if isinstance(errors, list):
            for item in errors:
                if isinstance(item, dict):
                    reason = _optional_str(item.get("reason"))
                    if reason:
                        reasons.append(reason)
        text = " ".join([text, message.lower(), " ".join(reasons).lower()])
    if status in {401, 403, 404} and (
        "playlistnotfound" in text
        or "not found" in text
        or "private" in text
        or "forbidden" in text
        or "permission" in text
    ):
        return "youtube_data_api_playlist_not_found"
    return default


def _youtube_data_api_error_summary(body: str, *, status: int, api_key: str | None = None) -> str:
    safe_body = body.replace(api_key, "[redacted]") if api_key else body
    try:
        payload = json.loads(safe_body)
    except json.JSONDecodeError:
        return sanitize_error_summary(f"YouTube Data API request failed with HTTP {status}.") or ""
    error = payload.get("error") if isinstance(payload, dict) else None
    message = error.get("message") if isinstance(error, dict) else None
    if message:
        return sanitize_error_summary(f"YouTube Data API request failed with HTTP {status}: {message}") or ""
    return sanitize_error_summary(f"YouTube Data API request failed with HTTP {status}.") or ""


def _uploads_playlist_id(channel: dict[str, Any]) -> str | None:
    content_details = _as_dict(channel.get("contentDetails"))
    related_playlists = _as_dict(content_details.get("relatedPlaylists"))
    return _optional_str(related_playlists.get("uploads"))


def _first_item(payload: dict[str, Any]) -> dict[str, Any] | None:
    items = _items(payload)
    return items[0] if items else None


def _items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    value = payload.get("items")
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _playlist_video_id(item: dict[str, Any]) -> str | None:
    content_details = _as_dict(item.get("contentDetails"))
    video_id = _optional_str(content_details.get("videoId"))
    if video_id:
        return video_id
    snippet = _as_dict(item.get("snippet"))
    resource_id = _as_dict(snippet.get("resourceId"))
    return _optional_str(resource_id.get("videoId"))


def _as_dict(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _chunks(values: list[str], size: int) -> list[list[str]]:
    return [values[index : index + size] for index in range(0, len(values), size)]


def _extract_video_id(video_url: str) -> str | None:
    parsed = urlparse(video_url)
    path_parts = [part for part in parsed.path.split("/") if part]
    if parsed.netloc.lower() == "youtu.be" and path_parts:
        return path_parts[0]
    video_ids = parse_qs(parsed.query).get("v")
    return video_ids[0] if video_ids and video_ids[0] else None


def _split_opencli_video_value(value: str, video_url: str) -> tuple[str | None, str | None, str | None]:
    parts = [part.strip() for part in value.split("|")]
    video_index = next((index for index, part in enumerate(parts) if video_url in part), len(parts))
    meta_parts = parts[:video_index]
    duration = meta_parts[0] if meta_parts else None
    views = next((part for part in meta_parts[1:] if "view" in part.lower()), None)
    published_hint = next((part for part in meta_parts[1:] if part != views), None)
    return duration, views, published_hint


def _to_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)
