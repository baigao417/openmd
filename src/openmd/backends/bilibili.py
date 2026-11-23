"""Bilibili subtitle extraction and collection discovery providers.

The providers in this module keep Bilibili work isolated from shared registries:
``openmd.backends.registry`` discovers this file by platform naming convention.
Real network/browser work is only registered when the matching CLI runtime gate
is enabled by the caller.
"""

from __future__ import annotations

from contextlib import contextmanager
import csv
import io
import json
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

from openmd.core.opencli_output import load_opencli_json
from openmd.runtime.asr import get_asr_backend
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
from openmd.backends.bilibili_wbi import (
    BilibiliWbiSigner,
    WbiSigningError,
    bilibili_cookie_header_for_url,
    bilibili_headers,
)
from openmd.core.detector import extract_first_url
from openmd.core.models import ContentItem, SourceLevel, TranscriptResult
from openmd.core.secrets import sanitize_error_summary
from openmd.media_tools import classify_yt_dlp_error, yt_dlp_command
from openmd.runtime.tempdirs import owned_temporary_directory
from openmd.runtime.interpreters import run_interruptibly


BILIBILI_HARVEST_BACKEND = "bilibili-harvest.core"
BILIBILI_OPENCLI_SUBTITLE_BACKEND = "opencli.bilibili.subtitle"
_OPENCLI_EMPTY_SUBTITLE_ATTEMPTS = 3
BILIBILI_YTDLP_MEDIA_BACKEND = "yt-dlp.bilibili"
BILIBILI_ASR_FALLBACK_BACKEND = "asr.fallback"
BILIBILI_NATIVE_DISCOVERY_BACKEND = "bilibili-harvest.discovery"
BILIBILI_OPENCLI_DISCOVERY_BACKEND = "opencli.bilibili.user-videos"

# Historical backend IDs retain "bilibili-harvest", but the current Openmd
# implementation is a public Bilibili API wrapper, not the Bilibili-Harvest
# local app/browser-extension pipeline.

_BVID_RE = re.compile(r"(BV[0-9A-Za-z]+)")
_SPACE_UID_RE = re.compile(r"(?:space\.bilibili\.com/|^)(\d{3,})")
_AI_CAPTION_HINT_RE = re.compile(
    "ai|auto|automatic|auto[_ -]?caption|"
    "\u81ea\u52a8|\u667a\u80fd\u5b57\u5e55|"
    "ai\u5b57\u5e55|ai\u751f\u6210",
    re.IGNORECASE,
)
_NATIVE_CAPTION_HINT_RE = re.compile(
    "native|manual|author|uploaded|"
    "\u5b98\u65b9\u5b57\u5e55|\u4eba\u5de5|\u4f5c\u8005|up\u4e3b",
    re.IGNORECASE,
)

CommandRunner = Callable[[list[str], int], tuple[int, str, str]]
HttpFetcher = Callable[[str, dict[str, str], int], tuple[int, str]]


@dataclass(frozen=True)
class _SubtitleSegment:
    start: float
    end: float | None
    content: str


@dataclass(frozen=True)
class _MediaWorkspace:
    path: Path
    retention: str


def register_backend_providers(
    registry: BackendProviderRegistry,
    options: BackendRuntimeOptions,
) -> None:
    """Register Bilibili extraction providers behind explicit runtime gates."""
    requested = options.backend_override
    if options.enable_network_backends and requested in {None, BILIBILI_HARVEST_BACKEND}:
        registry.register(BILIBILI_HARVEST_BACKEND, BilibiliNativeSubtitleProvider())

    if options.enable_browser_backends and requested in {None, BILIBILI_OPENCLI_SUBTITLE_BACKEND}:
        registry.register(
            BILIBILI_OPENCLI_SUBTITLE_BACKEND,
            OpenCliBilibiliSubtitleProvider(),
        )

    if requested in {None, BILIBILI_YTDLP_MEDIA_BACKEND}:
        registry.register(
            BILIBILI_YTDLP_MEDIA_BACKEND,
            BilibiliMediaDownloadGateProvider(options),
        )

    if requested in {None, BILIBILI_ASR_FALLBACK_BACKEND}:
        registry.register(
            BILIBILI_ASR_FALLBACK_BACKEND,
            BilibiliMediaAsrFallbackProvider(options),
        )


def register_discovery_providers(
    registry: DiscoveryProviderRegistry,
    options: BackendRuntimeOptions,
) -> None:
    """Register Bilibili discovery providers without touching shared registries."""
    requested = options.requested_discovery_provider
    native_requested = requested in {None, BILIBILI_NATIVE_DISCOVERY_BACKEND}
    opencli_requested = requested in {None, BILIBILI_OPENCLI_DISCOVERY_BACKEND}

    native_registered = False
    if options.enable_network_backends and native_requested:
        registry.register(
            BILIBILI_NATIVE_DISCOVERY_BACKEND,
            BilibiliNativeUserVideosProvider(),
            platforms=("bilibili",),
            default_for_platforms=requested is None,
        )
        native_registered = True

    if options.enable_browser_backends and opencli_requested:
        registry.register(
            BILIBILI_OPENCLI_DISCOVERY_BACKEND,
            OpenCliBilibiliUserVideosProvider(),
            platforms=("bilibili",),
            default_for_platforms=requested is None and not native_registered,
        )


class BilibiliNativeSubtitleProvider:
    """Wrap public Bilibili subtitle APIs; not the local/browser Harvest pipeline."""

    name = BILIBILI_HARVEST_BACKEND

    def __init__(
        self,
        *,
        fetcher: HttpFetcher | None = None,
        timeout: int = 30,
        wbi_signer: BilibiliWbiSigner | None = None,
        max_ai_retries: int = 3,
        ai_retry_interval: float = 0.0,
    ) -> None:
        self._fetcher = fetcher or _default_fetch
        self._timeout = timeout
        self._wbi_signer = wbi_signer or BilibiliWbiSigner(
            fetcher=self._fetcher,
            timeout=timeout,
        )
        self._max_ai_retries = max(1, max_ai_retries)
        self._ai_retry_interval = max(0.0, ai_retry_interval)

    def descriptor(self) -> BackendDescriptor:
        capability = BackendCapability(
            platforms=("bilibili",),
            source_types=("video",),
            input_types=("single",),
            source_levels=("native_caption", "auto_caption", "partial_extraction"),
            requires_network=True,
        )
        return BackendDescriptor(
            name=self.name,
            layer="platform_native",
            capabilities=(capability,),
            description=(
                "Bilibili public API subtitle wrapper; not the Bilibili-Harvest "
                "local/browser pipeline, no media download or ASR."
            ),
            provider="openmd.backends.bilibili",
        )

    def health(self) -> BackendHealth:
        return BackendHealth(ok=True, dependencies={"network": "enabled by caller"})

    def extract(self, request: BackendExtractRequest) -> TranscriptResult:
        item = request.item
        bvid = extract_bvid(item.url)
        if bvid is None:
            return _failed_result(self.name, "invalid_input", "Bilibili BVID not found.")

        try:
            view = _fetch_json(
                self._fetcher,
                "https://api.bilibili.com/x/web-interface/view",
                {"bvid": bvid},
                self._timeout,
            )
            view_data = _bilibili_api_data(view)
        except (HTTPError, URLError, OSError, ValueError, json.JSONDecodeError) as exc:
            return _failed_result(self.name, "bilibili_view_fetch_failed", exc)

        aid = str(view_data.get("aid") or "").strip()
        cid = str(view_data.get("cid") or "").strip()
        if not cid:
            pages = view_data.get("pages")
            if isinstance(pages, list) and pages:
                cid = str((pages[0] or {}).get("cid") or "").strip()
        if not cid:
            return _failed_result(
                self.name,
                "bilibili_cid_missing",
                "Bilibili view metadata did not include a cid.",
                metadata={"bvid": bvid, "aid": aid},
            )

        subtitles: list[dict[str, Any]] = []
        endpoint_used = "none"
        player_warnings: list[str] = []
        for endpoint, url, params in _player_subtitle_attempts(aid=aid, cid=cid, bvid=bvid):
            request_params = params
            if endpoint == "wbi_v2":
                try:
                    request_params = self._wbi_signer.sign_params(params, timeout=self._timeout)
                except (HTTPError, URLError, OSError, ValueError, WbiSigningError) as exc:
                    player_warnings.append(
                        f"{endpoint}_signing: {sanitize_error_summary(exc)}; unsigned request attempted"
                    )

            for retry_index in range(self._max_ai_retries):
                try:
                    player = _fetch_json(self._fetcher, url, request_params, self._timeout)
                    player_data = _bilibili_api_data(player)
                except (HTTPError, URLError, OSError, ValueError, json.JSONDecodeError) as exc:
                    player_warnings.append(f"{endpoint}: {sanitize_error_summary(exc)}")
                    break

                subtitles = _subtitle_entries_from_player(player_data)
                if subtitles:
                    endpoint_used = endpoint
                    break
                if retry_index + 1 < self._max_ai_retries and self._ai_retry_interval:
                    time.sleep(self._ai_retry_interval)
            if subtitles:
                break
            player_warnings.append(f"{endpoint}: no subtitle tracks")

        if not subtitles:
            return _failed_result(
                self.name,
                "bilibili_subtitle_missing",
                "Bilibili metadata did not expose native or AI subtitles.",
                title=_safe_str(view_data.get("title")),
                author=_owner_name(view_data),
                published_at=_format_unix_timestamp(view_data.get("pubdate")),
                metadata={"bvid": bvid, "aid": aid, "cid": cid, "player_warnings": player_warnings},
                notes=[
                    (
                        "Openmd's current bilibili-harvest.core backend uses public "
                        "Bilibili APIs only; it does not invoke the Bilibili-Harvest "
                        "local app/browser-extension pipeline."
                    ),
                    *_warning_notes(player_warnings),
                ],
            )

        subtitle = _choose_subtitle(subtitles)
        subtitle_track_type = _subtitle_track_type(subtitle)
        subtitle_url = _normalize_subtitle_url(_safe_str(subtitle.get("subtitle_url")))
        ai_rescue_note: str | None = None
        if not subtitle_url and subtitle_track_type == "ai":
            try:
                subtitle_url = _resolve_ai_subtitle_url(
                    self._fetcher,
                    aid=aid,
                    cid=cid,
                    timeout=self._timeout,
                )
            except (HTTPError, URLError, OSError, ValueError, json.JSONDecodeError) as exc:
                player_warnings.append(f"ai_subtitle_search_stat: {sanitize_error_summary(exc)}")
            if subtitle_url:
                ai_rescue_note = "AI subtitle URL was recovered via x/player/v2/ai/subtitle/search/stat."

        if not subtitle_url:
            return _failed_result(
                self.name,
                "bilibili_subtitle_url_missing",
                "Selected Bilibili subtitle did not include a subtitle_url.",
                metadata={"bvid": bvid, "aid": aid, "cid": cid, "track_type": subtitle_track_type},
                notes=[
                    "AI subtitle URL rescue via x/player/v2/ai/subtitle/search/stat was attempted when applicable.",
                    *_warning_notes(player_warnings),
                ],
            )

        try:
            subtitle_payload = _fetch_json(self._fetcher, subtitle_url, {}, self._timeout)
            segments = _segments_from_subtitle_payload(subtitle_payload)
        except (HTTPError, URLError, OSError, ValueError, json.JSONDecodeError) as exc:
            return _failed_result(self.name, "bilibili_subtitle_fetch_failed", exc)

        if not segments:
            return _failed_result(
                self.name,
                "bilibili_subtitle_empty",
                "Bilibili subtitle payload did not contain usable subtitle lines.",
                metadata={"bvid": bvid, "aid": aid, "cid": cid},
            )

        source_level = _source_level_from_subtitle_meta(subtitle)
        source_notes = [
            "Bilibili public API subtitle discovery tried signed x/player/wbi/v2 before x/player/v2 when aid was available.",
            f"subtitle_endpoint_used={endpoint_used}",
            f"async_ai_subtitle_retries={self._max_ai_retries}",
            "Bilibili danmaku/XML subtitle tracks were filtered before track selection.",
            _source_level_note(source_level),
            "This backend does not invoke the Bilibili-Harvest local app/browser-extension pipeline.",
            "No media, raw subtitle JSON, raw response, cookies, headers, or ASR artifacts were persisted.",
        ]
        if ai_rescue_note:
            source_notes.append(ai_rescue_note)
        source_notes.extend(_warning_notes(player_warnings))
        return TranscriptResult(
            text=_format_segments(segments),
            status="verified",
            source_level=source_level,
            backend=self.name,
            source_notes=source_notes,
            title=_safe_str(view_data.get("title")),
            author=_owner_name(view_data),
            published_at=_format_unix_timestamp(view_data.get("pubdate")),
            metadata={
                "bvid": bvid,
                "aid": aid,
                "cid": cid,
                "caption_language": _safe_str(subtitle.get("lan") or subtitle.get("lan_doc")),
                "caption_track_type": subtitle_track_type,
                "subtitle_endpoint_used": endpoint_used,
                "segment_count": len(segments),
            },
        )


class OpenCliBilibiliSubtitleProvider:
    """Controlled OpenCLI fallback for Bilibili subtitle extraction."""

    name = BILIBILI_OPENCLI_SUBTITLE_BACKEND

    def __init__(
        self,
        *,
        runner: CommandRunner | None = None,
        timeout: int = 120,
    ) -> None:
        self._runner = runner or _run_command
        self._timeout = timeout

    def descriptor(self) -> BackendDescriptor:
        capability = BackendCapability(
            platforms=("bilibili",),
            source_types=("video",),
            input_types=("single",),
            source_levels=("native_caption", "auto_caption", "partial_extraction"),
            requires_network=True,
        )
        return BackendDescriptor(
            name=self.name,
            layer="browser_cli_fallback",
            capabilities=(capability,),
            description="OpenCLI bilibili subtitle fallback; registered only with browser backend opt-in.",
            provider="opencli",
            is_fallback=True,
        )

    def health(self) -> BackendHealth:
        ok = shutil.which("opencli") is not None
        return BackendHealth(
            ok=ok,
            dependencies={"opencli": "ok" if ok else "missing"},
            warnings=() if ok else ("opencli executable was not found on PATH",),
        )

    def extract(self, request: BackendExtractRequest) -> TranscriptResult:
        item = request.item
        bvid = extract_bvid(item.url)
        if bvid is None:
            return _failed_result(self.name, "invalid_input", "Bilibili BVID not found.")

        try:
            # B站's player API returns a varying subtitle list for the same logged-in
            # request (2026-11-23: 1,1,0,0 / 1,12,1,1 on consecutive calls), so an
            # empty answer (OpenCLI EMPTY_RESULT, exit 66) is asked again, bounded.
            for attempt in range(_OPENCLI_EMPTY_SUBTITLE_ATTEMPTS):
                exit_code, stdout, stderr = self._runner(
                    ["opencli", "bilibili", "subtitle", bvid, "-f", "json"],
                    self._timeout,
                )
                if exit_code != 66 or attempt + 1 == _OPENCLI_EMPTY_SUBTITLE_ATTEMPTS:
                    break
                time.sleep(1.0)
        except FileNotFoundError as exc:
            return _failed_result(self.name, "backend_not_found", exc)
        except subprocess.TimeoutExpired as exc:
            return _failed_result(self.name, "backend_timeout", exc)
        except OSError as exc:
            return _failed_result(self.name, "backend_os_error", exc)

        if exit_code != 0:
            return _failed_result(
                self.name,
                f"backend_exit_{exit_code}",
                stderr or "OpenCLI bilibili subtitle failed.",
            )

        segments = parse_bilibili_subtitle_segments(stdout)
        if not segments:
            return _failed_result(
                self.name,
                "empty_result",
                "OpenCLI bilibili subtitle returned no parseable subtitle rows.",
            )

        source_level = _source_level_from_text(stdout, default="auto_caption")
        return TranscriptResult(
            text=_format_segments(segments),
            status="verified",
            source_level=source_level,
            backend=self.name,
            source_notes=[
                "OpenCLI bilibili subtitle output was parsed and normalized in memory.",
                _source_level_note(source_level),
                "No raw stdout/stderr, raw subtitle rows, media, cookies, or headers were persisted.",
            ],
            metadata={"bvid": bvid, "segment_count": len(segments)},
        )


class BilibiliMediaDownloadGateProvider:
    """Document the media-download stage without performing hidden extraction."""

    name = BILIBILI_YTDLP_MEDIA_BACKEND

    def __init__(self, options: BackendRuntimeOptions) -> None:
        self._options = options

    def descriptor(self) -> BackendDescriptor:
        capability = BackendCapability(
            platforms=("bilibili",),
            source_types=("video",),
            input_types=("single",),
            source_levels=("partial_extraction",),
            requires_network=True,
            optional_config=("enable_media_download_backends", "enable_asr_backends"),
        )
        return BackendDescriptor(
            name=self.name,
            layer="media_download_gate",
            capabilities=(capability,),
            description=(
                "Bilibili media download gate. The text-producing fallback is "
                "asr.fallback and requires explicit media-download plus ASR opt-in."
            ),
            provider="openmd.backends.bilibili",
            is_fallback=True,
        )

    def health(self) -> BackendHealth:
        if not self._options.enable_media_download_backends:
            return BackendHealth(ok=True, warnings=("media download fallback is not enabled",))
        ok = yt_dlp_command() is not None
        return BackendHealth(
            ok=ok,
            dependencies={"yt-dlp": "ok" if ok else "missing"},
            error_code=None if ok else "media_download_backend_missing",
            error_summary=None if ok else "yt-dlp executable was not found on PATH.",
        )

    def extract(self, request: BackendExtractRequest) -> TranscriptResult:
        item = request.item
        bvid = extract_bvid(item.url)
        return _failed_result(
            self.name,
            "bilibili_media_download_stage_requires_asr",
            "Media download is an intermediate stage; rerun with --enable-media-download --enable-asr to attempt ASR fallback.",
            title=item.title,
            author=item.author,
            published_at=item.published_at,
            metadata={
                "bvid": bvid,
                "failure_class": "user_resolvable",
                "media_download_enabled": self._options.enable_media_download_backends,
                "asr_enabled": self._options.enable_asr_backends,
            },
            notes=[
                "No media download or ASR action was attempted by the standalone media-download stage.",
                "Use the final asr.fallback provider for approved temporary media download plus local ASR.",
            ],
        )


class BilibiliMediaAsrFallbackProvider:
    """Explicit temporary-media + local-ASR fallback for no-subtitle Bilibili items."""

    name = BILIBILI_ASR_FALLBACK_BACKEND

    def __init__(
        self,
        options: BackendRuntimeOptions,
        *,
        runner: CommandRunner | None = None,
        asr_backend: object | None = None,
        timeout: int = 1800,
    ) -> None:
        self._options = options
        self._runner = runner or _run_command
        self._asr_backend = asr_backend or get_asr_backend("asr.auto")
        self._timeout = timeout

    def descriptor(self) -> BackendDescriptor:
        capability = BackendCapability(
            platforms=("bilibili",),
            source_types=("video",),
            input_types=("single",),
            source_levels=("media_asr", "partial_extraction"),
            requires_network=True,
            optional_config=("enable_media_download_backends", "enable_asr_backends"),
        )
        return BackendDescriptor(
            name=self.name,
            layer="media_asr_fallback",
            capabilities=(capability,),
            description=(
                "Bilibili no-subtitle fallback via temporary yt-dlp media download "
                "and local ASR; registered safely but runs only with explicit gates."
            ),
            provider="openmd.backends.bilibili",
            is_fallback=True,
        )

    def health(self) -> BackendHealth:
        if not (self._options.enable_media_download_backends and self._options.enable_asr_backends):
            return BackendHealth(ok=True, warnings=("Bilibili media ASR fallback is not enabled",))
        yt_dlp_ok = yt_dlp_command() is not None
        is_available = getattr(self._asr_backend, "is_available", None)
        asr_ok = bool(is_available()) if callable(is_available) else True
        dependencies = {
            "yt-dlp": "ok" if yt_dlp_ok else "missing",
            getattr(self._asr_backend, "name", "asr_backend"): "ok" if asr_ok else "missing",
        }
        return BackendHealth(
            ok=yt_dlp_ok and asr_ok,
            dependencies=dependencies,
            error_code=None if yt_dlp_ok and asr_ok else "bilibili_media_asr_dependency_missing",
            error_summary=None if yt_dlp_ok and asr_ok else "yt-dlp or the configured ASR backend is missing.",
        )

    def extract(self, request: BackendExtractRequest) -> TranscriptResult:
        item = request.item
        bvid = extract_bvid(item.url)
        if bvid is None:
            return _failed_result(self.name, "invalid_input", "Bilibili BVID not found.")

        missing_flags = _missing_media_asr_flags(self._options)
        if missing_flags:
            return _failed_result(
                self.name,
                "bilibili_media_asr_not_enabled",
                "Bilibili no-subtitle ASR fallback requires explicit --enable-media-download and --enable-asr.",
                title=item.title,
                author=item.author,
                published_at=item.published_at,
                metadata={
                    "bvid": bvid,
                    "failure_class": "user_resolvable",
                    "missing_flags": missing_flags,
                    "media_retention": "none",
                },
                notes=[
                    "No media download or ASR action was attempted because the explicit fallback gates are closed.",
                    "Raw media is not retained by default; add --keep-media only for approved debugging.",
                ],
            )

        with _media_workspace(self._options) as workspace:
            media_path = self._download_media(item, bvid, workspace.path)
            if isinstance(media_path, TranscriptResult):
                return media_path

            transcribe = getattr(self._asr_backend, "transcribe", None)
            if not callable(transcribe):
                return _failed_result(
                    self.name,
                    "asr_backend_missing",
                    "Configured ASR backend does not expose transcribe(path).",
                    metadata={"bvid": bvid, "failure_class": "user_resolvable"},
                )
            asr_result = transcribe(media_path)
            if not isinstance(asr_result, TranscriptResult) or not asr_result.has_usable_text:
                return _failed_result(
                    self.name,
                    getattr(asr_result, "error_code", None) or "bilibili_media_asr_failed",
                    getattr(asr_result, "error_summary", None) or "Bilibili media ASR produced no usable text.",
                    title=item.title or getattr(asr_result, "title", None),
                    author=item.author,
                    published_at=item.published_at,
                    metadata={
                        "bvid": bvid,
                        "failure_class": "user_resolvable",
                        "asr_backend": getattr(self._asr_backend, "name", "unknown"),
                        "media_retention": workspace.retention,
                    },
                    notes=[
                        "Temporary media download was attempted under explicit operator gates.",
                        *_source_notes_from(asr_result),
                    ],
                )

            metadata = dict(asr_result.metadata)
            if workspace.retention != "kept":
                metadata.pop("source_ref", None)
            metadata.update(
                {
                    "profile": "media",
                    "bvid": bvid,
                    "media_download_backend": BILIBILI_YTDLP_MEDIA_BACKEND,
                    "asr_backend": getattr(self._asr_backend, "name", asr_result.backend),
                    "media_retention": workspace.retention,
                    "raw_media_retained": workspace.retention == "kept",
                }
            )
            return replace(
                asr_result,
                source_level="media_asr",
                backend=getattr(self._asr_backend, "name", asr_result.backend),
                title=item.title or asr_result.title,
                author=item.author or asr_result.author,
                published_at=item.published_at or asr_result.published_at,
                source_notes=[
                    "Bilibili no-subtitle fallback used explicit temporary media download plus local ASR.",
                    "Raw media was retained only when --keep-media was set.",
                    *asr_result.source_notes,
                ],
                metadata=metadata,
            )

    def _download_media(
        self,
        item: ContentItem,
        bvid: str,
        media_dir: Path,
    ) -> Path | TranscriptResult:
        command_prefix = yt_dlp_command()
        if command_prefix is None:
            return _failed_result(
                self.name,
                "media_download_backend_missing",
                'yt-dlp is missing; install the "openmd-cli[media]" extra.',
                metadata={"bvid": bvid, "failure_class": "user_resolvable"},
            )
        output_template = media_dir / f"{bvid}.%(ext)s"
        from openmd.runtime.ffmpeg_tools import ffmpeg_location_args
        command = command_prefix + ffmpeg_location_args() + [
            "--no-playlist",
            "--extract-audio",
            "--audio-format",
            "wav",
            "--output",
            str(output_template),
            item.url,
        ]
        try:
            exit_code, stdout, stderr = self._runner(command, self._timeout)
        except FileNotFoundError as exc:
            return _failed_result(
                self.name,
                "media_download_backend_missing",
                exc,
                metadata={"bvid": bvid, "failure_class": "user_resolvable"},
            )
        except subprocess.TimeoutExpired as exc:
            return _failed_result(
                self.name,
                "media_download_timeout",
                exc,
                metadata={"bvid": bvid, "failure_class": "runtime_transient"},
            )
        except OSError as exc:
            return _failed_result(
                self.name,
                "media_download_os_error",
                exc,
                metadata={"bvid": bvid, "failure_class": "runtime_transient"},
            )

        if exit_code != 0:
            diagnostic = classify_yt_dlp_error(stderr or stdout)
            return _failed_result(
                self.name,
                f"media_download_{diagnostic['category']}",
                f"{diagnostic['message']} Next: {diagnostic['next_action']}",
                metadata={
                    "bvid": bvid,
                    "failure_class": "user_resolvable" if diagnostic["category"] != "network" else "runtime_transient",
                    "next_action": diagnostic["next_action"],
                },
            )

        media_path = _find_downloaded_media_file(media_dir, bvid)
        if media_path is None:
            return _failed_result(
                self.name,
                "media_download_empty",
                "yt-dlp completed but no media file was found in the temporary workspace.",
                metadata={"bvid": bvid, "failure_class": "runtime_transient"},
            )
        return media_path


class BilibiliNativeUserVideosProvider:
    """Discover public Bilibili user videos without extracting item subtitles."""

    name = BILIBILI_NATIVE_DISCOVERY_BACKEND

    def __init__(
        self,
        *,
        fetcher: HttpFetcher | None = None,
        timeout: int = 30,
        wbi_signer: BilibiliWbiSigner | None = None,
    ) -> None:
        self._fetcher = fetcher or _default_fetch
        self._timeout = timeout
        self._wbi_signer = wbi_signer or BilibiliWbiSigner(
            fetcher=self._fetcher,
            timeout=timeout,
        )

    def discover(self, request: DiscoveryRequest) -> DiscoveryResult:
        uid = extract_bilibili_uid(request.input_url)
        if uid is None:
            return DiscoveryResult.failure(
                provider=self.name,
                error_code="invalid_input",
                error_summary="Bilibili space UID not found for collection discovery.",
            )

        limit = _bounded_limit(request.limit)
        params = {"mid": uid, "pn": "1", "ps": str(limit), "order": "pubdate"}
        warnings: list[str] = []
        endpoint_used = "x/space/arc/search"
        videos: list[dict[str, Any]] = []

        try:
            signed_params = self._wbi_signer.sign_params(params, timeout=self._timeout)
            signed_payload = _fetch_json(
                self._fetcher,
                "https://api.bilibili.com/x/space/wbi/arc/search",
                signed_params,
                self._timeout,
            )
            signed_data = _bilibili_api_data(signed_payload)
            videos = _video_entries_from_space_payload(signed_data)
            endpoint_used = "x/space/wbi/arc/search"
        except (HTTPError, URLError, OSError, ValueError, WbiSigningError, json.JSONDecodeError) as exc:
            warnings.append(f"x/space/wbi/arc/search: {sanitize_error_summary(exc)}")

        if not videos:
            if endpoint_used == "x/space/wbi/arc/search":
                warnings.append("x/space/wbi/arc/search: no usable video rows")
            try:
                payload = _fetch_json(
                    self._fetcher,
                    "https://api.bilibili.com/x/space/arc/search",
                    params,
                    self._timeout,
                )
                data = _bilibili_api_data(payload)
                videos = _video_entries_from_space_payload(data)
                endpoint_used = "x/space/arc/search"
            except (HTTPError, URLError, OSError, ValueError, json.JSONDecodeError) as exc:
                warnings.append(f"x/space/arc/search: {sanitize_error_summary(exc)}")

        if not videos:
            return DiscoveryResult.failure(
                provider=self.name,
                error_code="bilibili_discovery_fetch_failed",
                error_summary=sanitize_error_summary("; ".join(warnings) or "Bilibili discovery returned no usable rows."),
                metadata={"uid": uid, "limit": limit, "endpoint_used": endpoint_used},
            )

        items = [_content_item_from_video_entry(entry, request, uid) for entry in videos]
        items = [item for item in items if item.url][:limit]
        if not items:
            return DiscoveryResult.failure(
                provider=self.name,
                error_code="empty_result",
                error_summary="Bilibili user videos discovery returned no usable video rows.",
                metadata={"uid": uid, "limit": limit},
            )

        return DiscoveryResult.success(
            provider=self.name,
            items=items,
            source_notes=[
                "Bilibili public user videos API discovery only; item extraction was not attempted.",
                f"discovery_endpoint_used={endpoint_used}",
                "Discovery metadata was normalized without persisting raw API responses.",
                (
                    "Bilibili UP discovery tries signed x/space/wbi/arc/search before "
                    "falling back to public x/space/arc/search; callers can use the "
                    "OpenCLI fallback when native public discovery is unavailable."
                ),
                *_warning_notes(warnings),
            ],
            metadata={
                "uid": uid,
                "limit": limit,
                "discovery_endpoint_used": endpoint_used,
                "normalized_url": f"https://space.bilibili.com/{uid}",
                "stability_note": "Tries signed x/space/wbi/arc/search first, then public x/space/arc/search fallback.",
            },
        )


class OpenCliBilibiliUserVideosProvider:
    """Controlled OpenCLI fallback for Bilibili UP video discovery."""

    name = BILIBILI_OPENCLI_DISCOVERY_BACKEND

    def __init__(
        self,
        *,
        runner: CommandRunner | None = None,
        timeout: int = 120,
    ) -> None:
        self._runner = runner or _run_command
        self._timeout = timeout

    def discover(self, request: DiscoveryRequest) -> DiscoveryResult:
        uid = extract_bilibili_uid(request.input_url)
        if uid is None:
            return DiscoveryResult.failure(
                provider=self.name,
                error_code="invalid_input",
                error_summary="Bilibili space UID not found for OpenCLI discovery.",
            )

        limit = _bounded_limit(request.limit)
        try:
            exit_code, stdout, stderr = self._runner(
                ["opencli", "bilibili", "user-videos", uid, "--limit", str(limit)],
                self._timeout,
            )
        except FileNotFoundError as exc:
            return DiscoveryResult.failure(
                provider=self.name,
                error_code="backend_not_found",
                error_summary=sanitize_error_summary(exc),
            )
        except subprocess.TimeoutExpired as exc:
            return DiscoveryResult.failure(
                provider=self.name,
                error_code="backend_timeout",
                error_summary=sanitize_error_summary(exc),
            )
        except OSError as exc:
            return DiscoveryResult.failure(
                provider=self.name,
                error_code="backend_os_error",
                error_summary=sanitize_error_summary(exc),
            )

        if exit_code != 0:
            return DiscoveryResult.failure(
                provider=self.name,
                error_code=f"backend_exit_{exit_code}",
                error_summary=sanitize_error_summary(stderr or "OpenCLI bilibili user-videos failed."),
            )

        rows = parse_opencli_bilibili_user_videos(stdout)
        items = [_content_item_from_opencli_row(row, request, uid) for row in rows]
        items = [item for item in items if item.url][:limit]
        if not items:
            return DiscoveryResult.failure(
                provider=self.name,
                error_code="empty_result",
                error_summary="OpenCLI bilibili user-videos returned no parseable video rows.",
                metadata={"uid": uid, "limit": limit},
            )

        return DiscoveryResult.success(
            provider=self.name,
            items=items,
            source_notes=[
                "OpenCLI bilibili user-videos discovery only; item extraction was not attempted.",
                "Raw stdout/stderr was parsed in memory and not persisted.",
            ],
            metadata={"uid": uid, "limit": limit, "normalized_url": f"https://space.bilibili.com/{uid}"},
        )


def extract_bvid(value: str) -> str | None:
    match = _BVID_RE.search(extract_first_url(value)) or _BVID_RE.search(value)
    return match.group(1) if match else None


def extract_bilibili_uid(value: str) -> str | None:
    url = extract_first_url(value)
    parsed = urlparse(url if "://" in url else f"https://{url}")
    host_path = f"{parsed.netloc}{parsed.path}"
    match = _SPACE_UID_RE.search(host_path)
    if match:
        return match.group(1)
    if value.strip().isdigit():
        return value.strip()
    return None


def parse_bilibili_subtitle_segments(text: str) -> list[_SubtitleSegment]:
    rows = _parse_json_subtitle_rows(text) or _parse_tabular_rows(text)
    return _segments_from_rows(rows)


def parse_opencli_bilibili_user_videos(text: str) -> list[dict[str, str]]:
    rows = _parse_json_subtitle_rows(text) or _parse_tabular_rows(text)
    return [row for row in rows if _row_lookup(row, "url") or extract_bvid(str(row))]


def _missing_media_asr_flags(options: BackendRuntimeOptions) -> list[str]:
    missing: list[str] = []
    if not options.enable_media_download_backends:
        missing.append("--enable-media-download")
    if not options.enable_asr_backends:
        missing.append("--enable-asr")
    return missing


@contextmanager
def _media_workspace(options: BackendRuntimeOptions):
    if options.keep_media_artifacts:
        media_dir = Path(options.media_artifact_dir or "out/raw-media")
        media_dir.mkdir(parents=True, exist_ok=True)
        yield _MediaWorkspace(path=media_dir, retention="kept")
        return

    with owned_temporary_directory(prefix="openmd-bilibili-asr-") as tmp_dir:
        yield _MediaWorkspace(path=Path(tmp_dir), retention="deleted")


def _find_downloaded_media_file(media_dir: Path, bvid: str) -> Path | None:
    suffix_rank = {
        ".wav": 0,
        ".m4a": 1,
        ".mp3": 2,
        ".opus": 3,
        ".aac": 4,
        ".flac": 5,
        ".mp4": 6,
    }
    candidates = [path for path in media_dir.iterdir() if path.is_file()]
    if not candidates:
        return None
    matched = [path for path in candidates if bvid in path.name] or candidates
    return sorted(
        matched,
        key=lambda path: (suffix_rank.get(path.suffix.lower(), 100), path.name),
    )[0]


def _source_notes_from(value: object) -> list[str]:
    if isinstance(value, TranscriptResult):
        return list(value.source_notes)
    return []


def _default_fetch(url: str, params: dict[str, str], timeout: int) -> tuple[int, str]:
    query = urlencode(params)
    full_url = f"{url}?{query}" if query else url
    request = Request(
        full_url,
        headers=bilibili_headers(cookie_header=bilibili_cookie_header_for_url(url)),
    )
    with urlopen(request, timeout=timeout) as response:
        raw = response.read().decode("utf-8", errors="replace")
        return int(getattr(response, "status", 200)), raw


def _run_command(command: list[str], timeout: int) -> tuple[int, str, str]:
    resolved = shutil.which(command[0]) or command[0]
    completed = run_interruptibly(
        [resolved, *command[1:]],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        encoding="utf-8",
        errors="replace",
    )
    return completed.returncode, completed.stdout or "", completed.stderr or ""


def _fetch_json(
    fetcher: HttpFetcher,
    url: str,
    params: dict[str, str],
    timeout: int,
) -> Any:
    status, body = fetcher(url, params, timeout)
    if status < 200 or status >= 300:
        raise ValueError(f"Bilibili request failed with HTTP {status}.")
    return json.loads(body)


def _bilibili_api_data(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("Bilibili API payload was not a JSON object.")
    code = payload.get("code", 0)
    if code not in {0, "0", None}:
        message = _safe_str(payload.get("message") or payload.get("msg") or code)
        raise ValueError(f"Bilibili API returned code {code}: {message}")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ValueError("Bilibili API payload did not include an object data field.")
    return data


def _subtitle_entries_from_player(player_data: dict[str, Any]) -> list[dict[str, Any]]:
    subtitle = player_data.get("subtitle")
    if not isinstance(subtitle, dict):
        return []
    entries = subtitle.get("subtitles")
    if not isinstance(entries, list):
        return []
    return [
        entry
        for entry in entries
        if isinstance(entry, dict) and not _is_danmaku_subtitle_entry(entry)
    ]


def _choose_subtitle(entries: list[dict[str, Any]]) -> dict[str, Any]:
    dedup: dict[str, dict[str, Any]] = {}
    for entry in entries:
        lang = _normalize_lang(_safe_str(entry.get("lan") or entry.get("lan_doc")))
        key = lang.lower()
        existing = dedup.get(key)
        if existing is None or (
            _subtitle_track_type(existing) == "ai" and _subtitle_track_type(entry) == "uploader"
        ):
            dedup[key] = entry

    candidates = list(dedup.values()) or entries
    for preferred in ("zh-CN", "zh-Hans", "zh", "ai-zh"):
        for entry in candidates:
            if _normalize_lang(_safe_str(entry.get("lan") or entry.get("lan_doc"))).lower() == preferred.lower():
                return entry

    zh_like = [
        entry
        for entry in candidates
        if _normalize_lang(_safe_str(entry.get("lan") or entry.get("lan_doc"))).lower().startswith(("zh", "ai-zh"))
    ]
    if zh_like:
        return sorted(zh_like, key=_subtitle_sort_key)[0]
    return sorted(candidates, key=_subtitle_sort_key)[0]


def _normalize_subtitle_url(value: str | None) -> str | None:
    if not value:
        return None
    if value.startswith("//"):
        return f"https:{value}"
    if value.startswith("http://") or value.startswith("https://"):
        return value
    if value.startswith("/"):
        return f"https://api.bilibili.com{value}"
    return None


def _player_subtitle_attempts(
    *,
    aid: str,
    cid: str,
    bvid: str,
) -> list[tuple[str, str, dict[str, str]]]:
    attempts: list[tuple[str, str, dict[str, str]]] = []
    if aid:
        attempts.append(
            (
                "wbi_v2",
                "https://api.bilibili.com/x/player/wbi/v2",
                {"aid": aid, "cid": cid},
            )
        )
    attempts.append(
        (
            "v2",
            "https://api.bilibili.com/x/player/v2",
            {"cid": cid, "bvid": bvid},
        )
    )
    return attempts


def _normalize_lang(raw_lang: str) -> str:
    raw = raw_lang.strip()
    if not raw:
        return "unknown"
    lower = raw.lower()
    mapping = {
        "zh-cn": "zh-CN",
        "zh-hans": "zh-Hans",
        "zh-hant": "zh-Hant",
        "zh-tw": "zh-Hant",
        "zh-hk": "zh-Hant",
        "zh": "zh",
        "ai-zh": "ai-zh",
    }
    if lower in mapping:
        return mapping[lower]
    if lower.startswith("ai-zh"):
        return "ai-zh"
    if lower.startswith("zh-cn"):
        return "zh-CN"
    if lower.startswith("zh-hans"):
        return "zh-Hans"
    if lower.startswith("zh"):
        return "zh"
    return raw


def _is_danmaku_subtitle_entry(entry: dict[str, Any]) -> bool:
    raw_lang = _safe_str(entry.get("lan") or entry.get("lan_doc")).lower()
    if raw_lang == "danmaku":
        return True
    subtitle_url = _normalize_subtitle_url(_safe_str(entry.get("subtitle_url")))
    if not subtitle_url:
        return False
    return urlparse(subtitle_url).path.lower().endswith(".xml")


def _subtitle_track_type(entry: dict[str, Any]) -> str:
    raw_lang = _safe_str(entry.get("lan"))
    values = " ".join(_safe_str(value) for value in entry.values())
    ai_status = entry.get("ai_status")
    if (
        raw_lang.lower().startswith("ai-")
        or bool(entry.get("ai_type"))
        or ai_status not in {None, 0, "0", False}
        or _AI_CAPTION_HINT_RE.search(values)
    ):
        return "ai"
    return "uploader"


def _subtitle_sort_key(entry: dict[str, Any]) -> tuple[int, str]:
    source_rank = 0 if _subtitle_track_type(entry) == "uploader" else 1
    return source_rank, _normalize_lang(_safe_str(entry.get("lan") or entry.get("lan_doc"))).lower()


def _resolve_ai_subtitle_url(
    fetcher: HttpFetcher,
    *,
    aid: str,
    cid: str,
    timeout: int,
) -> str | None:
    if not aid or not cid:
        return None
    payload = _fetch_json(
        fetcher,
        "https://api.bilibili.com/x/player/v2/ai/subtitle/search/stat",
        {"aid": aid, "cid": cid},
        timeout,
    )
    data = _bilibili_api_data(payload)
    return _normalize_subtitle_url(_safe_str(data.get("subtitle_url")))


def _segments_from_subtitle_payload(payload: Any) -> list[_SubtitleSegment]:
    if isinstance(payload, dict):
        body = payload.get("body") or payload.get("segments") or payload.get("data")
    else:
        body = payload
    if not isinstance(body, list):
        return []
    return _segments_from_rows([row for row in body if isinstance(row, dict)])


def _parse_json_subtitle_rows(text: str) -> list[dict[str, Any]]:
    try:
        payload = load_opencli_json(text)
    except json.JSONDecodeError:
        return []
    if isinstance(payload, dict):
        body = payload.get("body") or payload.get("segments") or payload.get("data")
        if isinstance(body, list):
            return [row for row in body if isinstance(row, dict)]
        return [payload]
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    return []


def _parse_tabular_rows(text: str) -> list[dict[str, str]]:
    return _parse_console_or_markdown_table(text) or _parse_csv_rows(text)


def _parse_console_or_markdown_table(text: str) -> list[dict[str, str]]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    header: list[str] | None = None
    rows: list[dict[str, str]] = []
    delimiter = "\u2502" if any("\u2502" in line for line in lines) else "|"

    for line in lines:
        if delimiter not in line or set(line) <= {
            delimiter,
            "-",
            "+",
            "\u2500",
            "\u250c",
            "\u2510",
            "\u2514",
            "\u2518",
            "\u252c",
            "\u2534",
            "\u253c",
            " ",
        }:
            continue
        cells = [cell.strip().strip("'\"") for cell in line.strip(delimiter).split(delimiter)]
        cells = [cell for cell in cells if cell or delimiter == "|"]
        if not cells:
            continue
        normalized = [_normalize_header(cell) for cell in cells]
        if any(cell in {"from", "to", "content", "title", "url"} for cell in normalized):
            header = normalized
            continue
        if header is None or len(cells) < len(header):
            continue
        row = {header[index]: cells[index].strip() for index in range(len(header))}
        if any(value for value in row.values()) and set("".join(row.values())) != {"-"}:
            rows.append(row)
    return rows


def _parse_csv_rows(text: str) -> list[dict[str, str]]:
    try:
        reader = csv.DictReader(io.StringIO(text))
        if not reader.fieldnames:
            return []
        fieldnames = [_normalize_header(field or "") for field in reader.fieldnames]
        rows: list[dict[str, str]] = []
        for raw_row in reader:
            row = {
                fieldnames[index]: (raw_row.get(reader.fieldnames[index] or "") or "").strip()
                for index in range(len(fieldnames))
            }
            if any(row.values()):
                rows.append(row)
        return rows
    except csv.Error:
        return []


def _segments_from_rows(rows: list[dict[str, Any]]) -> list[_SubtitleSegment]:
    segments: list[_SubtitleSegment] = []
    for row in rows:
        start = _coerce_seconds(_row_lookup(row, "from", "start", "begin"))
        end = _coerce_seconds(_row_lookup(row, "to", "end"))
        content = _safe_str(_row_lookup(row, "content", "text", "body"))
        if start is None or not content:
            continue
        segments.append(_SubtitleSegment(start=start, end=end, content=content))
    return segments


def _format_segments(segments: list[_SubtitleSegment]) -> str:
    return "".join(f"[{_format_timestamp(segment.start)}] {segment.content.strip()}\n" for segment in segments)


def _format_timestamp(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def _coerce_seconds(value: object) -> float | None:
    text = _safe_str(value)
    if not text:
        return None
    text = text.strip().strip("s\u79d2")
    if ":" in text:
        parts = text.split(":")
        try:
            numbers = [float(part) for part in parts]
        except ValueError:
            return None
        if len(numbers) == 3:
            return numbers[0] * 3600 + numbers[1] * 60 + numbers[2]
        if len(numbers) == 2:
            return numbers[0] * 60 + numbers[1]
    try:
        return float(text)
    except ValueError:
        return None


def _source_level_from_subtitle_meta(meta: dict[str, Any]) -> SourceLevel:
    if _subtitle_track_type(meta) == "ai":
        return "auto_caption"
    values = " ".join(_safe_str(value) for value in meta.values())
    if _NATIVE_CAPTION_HINT_RE.search(values):
        return "native_caption"
    return "native_caption"


def _source_level_from_text(text: str, *, default: SourceLevel) -> SourceLevel:
    if _AI_CAPTION_HINT_RE.search(text):
        return "auto_caption"
    if _NATIVE_CAPTION_HINT_RE.search(text):
        return "native_caption"
    return default


def _source_level_note(source_level: SourceLevel) -> str:
    if source_level == "auto_caption":
        return "source_level=auto_caption: Bilibili metadata/output indicated AI or automatic captions, or OpenCLI did not expose manual-caption metadata."
    if source_level == "native_caption":
        return "source_level=native_caption: Bilibili metadata/output indicated native/manual captions."
    return "source_level=partial_extraction: no usable caption text was produced."


def _video_entries_from_space_payload(data: dict[str, Any]) -> list[dict[str, Any]]:
    listing = data.get("list")
    if isinstance(listing, dict) and isinstance(listing.get("vlist"), list):
        return [entry for entry in listing["vlist"] if isinstance(entry, dict)]
    if isinstance(data.get("vlist"), list):
        return [entry for entry in data["vlist"] if isinstance(entry, dict)]
    return []


def _content_item_from_video_entry(
    entry: dict[str, Any],
    request: DiscoveryRequest,
    uid: str,
) -> ContentItem:
    bvid = _safe_str(entry.get("bvid")) or extract_bvid(_safe_str(entry.get("arcurl")))
    url = _safe_str(entry.get("arcurl")) or (f"https://www.bilibili.com/video/{bvid}/" if bvid else "")
    return ContentItem(
        platform="bilibili",
        input_type="single",
        url=url,
        id=bvid,
        title=_safe_str(entry.get("title")),
        author=_safe_str(entry.get("author")) or None,
        published_at=_format_unix_timestamp(entry.get("created")),
        source_type="video",
        raw_meta={
            "collection_url": request.input_url,
            "collection_uid": uid,
            "discovery_limit": request.limit,
            "discovery_provider": BILIBILI_NATIVE_DISCOVERY_BACKEND,
        },
    )


def _content_item_from_opencli_row(
    row: dict[str, str],
    request: DiscoveryRequest,
    uid: str,
) -> ContentItem:
    row_text = " ".join(row.values())
    url = _row_lookup(row, "url") or _safe_str(re.search(r"https?://\S+", row_text).group(0) if re.search(r"https?://\S+", row_text) else "")
    bvid = extract_bvid(url or row_text)
    if not url and bvid:
        url = f"https://www.bilibili.com/video/{bvid}/"
    raw_meta = {
        "collection_url": request.input_url,
        "collection_uid": uid,
        "discovery_limit": request.limit,
        "discovery_provider": BILIBILI_OPENCLI_DISCOVERY_BACKEND,
    }
    for key in ("rank", "plays", "likes"):
        value = _row_lookup(row, key)
        if value:
            raw_meta[key] = value
    return ContentItem(
        platform="bilibili",
        input_type="single",
        url=url,
        id=bvid,
        title=_row_lookup(row, "title") or None,
        published_at=_row_lookup(row, "date", "published_at") or None,
        source_type="video",
        raw_meta=raw_meta,
    )


def _bounded_limit(limit: int | None) -> int:
    if limit is None:
        return 20
    return max(0, min(limit, 50))


def _normalize_header(value: str) -> str:
    normalized = value.strip().lower().replace(" ", "_").replace("-", "_")
    aliases = {
        "(index)": "index",
        "index": "index",
        "from": "from",
        "start": "start",
        "to": "to",
        "end": "end",
        "content": "content",
        "text": "text",
        "rank": "rank",
        "title": "title",
        "plays": "plays",
        "likes": "likes",
        "date": "date",
        "url": "url",
    }
    return aliases.get(normalized, normalized)


def _row_lookup(row: dict[str, Any], *keys: str) -> str:
    normalized = {_normalize_header(str(key)): value for key, value in row.items()}
    for key in keys:
        value = normalized.get(_normalize_header(key))
        text = _safe_str(value)
        if text:
            return text
    return ""


def _owner_name(view_data: dict[str, Any]) -> str | None:
    owner = view_data.get("owner")
    if isinstance(owner, dict):
        return _safe_str(owner.get("name")) or None
    return None


def _format_unix_timestamp(value: object) -> str | None:
    try:
        seconds = int(str(value))
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(seconds, tz=timezone.utc).date().isoformat()


def _safe_str(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _warning_notes(warnings: list[str]) -> list[str]:
    return [f"Bilibili subtitle discovery warning: {warning}" for warning in warnings]


def _failed_result(
    backend: str,
    code: str,
    error: object,
    *,
    title: str | None = None,
    author: str | None = None,
    published_at: str | None = None,
    metadata: dict[str, Any] | None = None,
    notes: list[str] | None = None,
) -> TranscriptResult:
    return TranscriptResult(
        text="",
        status="failed",
        source_level="partial_extraction",
        backend=backend,
        source_notes=[
            "Bilibili provider returned structured failure.",
            _source_level_note("partial_extraction"),
            *(notes or []),
        ],
        error_code=code,
        error_summary=sanitize_error_summary(error),
        title=title,
        author=author,
        published_at=published_at,
        metadata=metadata or {},
    )
