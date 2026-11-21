"""Douyin transcript provider with native text, transient ASR, and OCR fallback."""

from __future__ import annotations

import difflib
import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any, Protocol

from openmd.backends.base import BackendCapability, BackendDescriptor, BackendExtractRequest, BackendHealth, ItemIdentityResolution
from openmd.backends.douyin_detail import DouyinDetailError, DouyinDetailProvider
from openmd.backends.executor import BackendProviderRegistry
from openmd.backends.registry import BackendRuntimeOptions
from openmd.core.models import ContentItem, TranscriptResult
from openmd.core.secrets import sanitize_error_summary
from openmd.runtime.tempdirs import owned_temporary_directory
from openmd.runtime.ffmpeg_tools import ffmpeg_executable
from openmd.runtime.interpreters import run_interruptibly


DOUYIN_TRANSCRIPT_BACKEND = "douyin.transcript"
DOUYIN_NATIVE_TRANSCRIPT_BACKEND = "douyin.native-transcript"
DOUYIN_POST_TEXT_BACKEND = "douyin.post-text"
DOUYIN_ASR_BACKEND = "siliconflow.sensevoice"
DOUYIN_OCR_BACKEND = "rapidocr.douyin"
SILICONFLOW_URL = "https://api.siliconflow.cn/v1/audio/transcriptions"
SILICONFLOW_MODEL = "FunAudioLLM/SenseVoiceSmall"

CommandRunner = Callable[[list[str], int], tuple[int, str, str]]
OcrExtractor = Callable[[ContentItem], str]

_TIMESTAMP_PREFIX_RE = re.compile(r"^\[?\d{1,2}:\d{2}(?::\d{2})?\]?\s*")
_CHINESE_RE = re.compile(r"[\u4e00-\u9fff]")
_TEXT_TOKEN_RE = re.compile(r"[\u4e00-\u9fffA-Za-z0-9]")
_SPACE_RE = re.compile(r"\s+")


class AsrClient(Protocol):
    name: str

    def is_available(self) -> bool:
        ...

    def transcribe(self, audio_path: Path, *, timeout: int) -> str:
        ...


def register_backend_providers(
    registry: BackendProviderRegistry,
    options: BackendRuntimeOptions,
) -> None:
    requested = options.backend_override
    if requested in {None, DOUYIN_TRANSCRIPT_BACKEND}:
        registry.register(DOUYIN_TRANSCRIPT_BACKEND, DouyinTranscriptProvider(options))
    if requested == DOUYIN_ASR_BACKEND and all((options.enable_browser_backends, options.enable_media_download_backends, options.enable_asr_backends, options.enable_cloud_backends)):
        registry.register(DOUYIN_ASR_BACKEND, DouyinCloudTranscriptProvider(options, asr_client=SiliconFlowASRClient()))


class SiliconFlowASRClient:
    name = DOUYIN_ASR_BACKEND

    def is_available(self) -> bool:
        return bool(os.getenv("SILICONFLOW_API_KEY"))

    def transcribe(self, audio_path: Path, *, timeout: int) -> str:
        api_key = os.getenv("SILICONFLOW_API_KEY")
        if not api_key:
            raise RuntimeError("SILICONFLOW_API_KEY is not set")
        boundary = f"openmd-{uuid.uuid4().hex}"
        body = _multipart_body(
            boundary,
            fields={"model": SILICONFLOW_MODEL},
            file_field="file",
            file_path=audio_path,
            content_type="audio/mpeg",
        )
        request = urllib.request.Request(
            SILICONFLOW_URL,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed HTTPS endpoint.
                payload = json.loads(response.read().decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"SiliconFlow ASR HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"SiliconFlow ASR request failed: {sanitize_error_summary(exc)}") from exc
        text = _asr_text_from_payload(payload)
        if not _has_meaningful_text(text):
            raise RuntimeError("SiliconFlow ASR returned no meaningful text")
        return text


class DouyinTranscriptProvider:
    """Own the native -> transient ASR -> hard-subtitle OCR ladder."""

    name = DOUYIN_TRANSCRIPT_BACKEND

    def __init__(
        self,
        options: BackendRuntimeOptions,
        *,
        runner: CommandRunner | None = None,
        asr_client: AsrClient | None = None,
        ocr_extractor: OcrExtractor | None = None,
        detail_provider: DouyinDetailProvider | None = None,
        asr_backend: Any | None = None,
        ffmpeg_timeout: int = 600,
        asr_timeout: int = 300,
    ) -> None:
        self._options = options
        self._runner = runner or _run_command
        self._asr_client = asr_client
        self._asr_backend = asr_backend
        self._detail_provider = detail_provider or DouyinDetailProvider()
        self._resolved_items: dict[str, ContentItem] = {}
        self._ocr_extractor_override = ocr_extractor
        self._ffmpeg_timeout = ffmpeg_timeout
        self._asr_timeout = asr_timeout
        self._ocr_engine: Any | None = None

    def descriptor(self) -> BackendDescriptor:
        return BackendDescriptor(
            name=self.name,
            layer="transcript",
            capabilities=(
                BackendCapability(
                    platforms=("douyin",),
                    source_types=("video", "image_text"),
                    input_types=("single", "collection"),
                    source_levels=("native_text", "platform_transcript", "media_asr", "ocr", "partial_extraction"),
                    supports_batch=True,
                    supports_collection=True,
                    requires_network=True,
                    requires_credentials=False,
                    optional_config=(
                        "enable_media_download_backends",
                        "enable_asr_backends",
                        "enable_cloud_backends",
                        "SILICONFLOW_API_KEY",
                    ),
                ),
            ),
            description="Douyin bounded detail, native captions, then temporary audio + local asr.auto; optional explicit cloud/OCR.",
            provider="openmd.backends.douyin_providers",
        )

    def health(self) -> BackendHealth:
        return BackendHealth(
            ok=True,
            dependencies={"ffmpeg": "ok" if ffmpeg_executable() else "missing", "asr.auto": "local_runtime_lazy"},
            warnings=("Detail needs browser permission; local audio ASR needs media and ASR permissions.",),
        )

    def resolve_item_identity(self, item: ContentItem) -> ItemIdentityResolution:
        if not self._options.enable_browser_backends:
            return ItemIdentityResolution.failure(item, provider=self.name, error_code="douyin_detail_browser_not_enabled", error_summary="Video share link resolution requires the browser backend gate.")
        try:
            resolved = self._detail_provider.resolve(item)
            self._resolved_items[resolved.url] = resolved
            return ItemIdentityResolution.success(resolved, provider=self._detail_provider.name)
        except DouyinDetailError as exc:
            return ItemIdentityResolution.failure(item, provider=self._detail_provider.name, error_code=exc.code, error_summary=str(exc))

    def extract(self, request: BackendExtractRequest) -> TranscriptResult:
        item = request.item
        if item.platform != "douyin":
            return _failure(item, "invalid_input", "Douyin transcript provider only supports Douyin.")
        item = self._resolved_items.pop(item.url, item)
        metadata_detail = None
        metadata_attempts = 0
        metadata_errors: list[str] = []
        if (
            item.input_type == "collection" and self._options.enable_browser_backends
            and not item.raw_meta.get("detail_resolved")
            and not item.raw_meta.get("metadata_detail_attempted")
            and _missing_metadata_fields(item.title, item.author, item.published_at, item.id)
            and (item.raw_meta.get("play_url") or _native_transcript(item) or _image_post_author_text(item)[0])
        ):
            # Enrichment allows at most two bounded reads. Do not replace media,
            # native captions, or image author text with a different source.
            for _ in range(2):
                metadata_attempts += 1
                try:
                    metadata_detail = self._detail_provider.resolve(item)
                    item = replace(item, title=item.title if item.title and item.title != item.id else metadata_detail.title,
                                   author=item.author or metadata_detail.author,
                                   published_at=item.published_at or metadata_detail.published_at)
                except DouyinDetailError as exc:
                    metadata_errors.append(exc.code)
                if not _missing_metadata_fields(item.title, item.author, item.published_at, item.id):
                    break
            item = replace(item, raw_meta={**item.raw_meta, "metadata_detail_attempted": True})
        result = self._extract_content(item, metadata_detail=metadata_detail)
        if item.input_type == "collection":
            missing = _missing_metadata_fields(result.title, result.author, result.published_at, item.id)
            if missing:
                warning = "Douyin metadata incomplete: missing " + ", ".join(missing) + f"; bounded metadata detail attempts: {metadata_attempts}."
                result = replace(result, source_notes=[*result.source_notes, warning], metadata={
                    **result.metadata,
                    "metadata_warnings": [warning],
                    "metadata_enrichment": {"detail_attempts": metadata_attempts, "missing_fields": missing, "detail_error_codes": metadata_errors},
                })
        return result

    def _extract_content(self, item: ContentItem, *, metadata_detail: ContentItem | None = None) -> TranscriptResult:
        native = _native_transcript(item)
        author_text, author_text_source = _image_post_author_text(item)
        needs_image_detail = item.source_type == "image_text" and not author_text.strip() and not item.raw_meta.get("detail_resolved")
        if needs_image_detail or (item.source_type != "image_text" and not native and not item.raw_meta.get("play_url")):
            if not self._options.enable_browser_backends:
                if item.source_type == "image_text":
                    return _failure(item, "douyin_image_post_text_unavailable", "Image-post author text is unavailable; detail acquisition requires --enable-browser-backends.", failure_class="user_resolvable")
                return _failure(item, "douyin_detail_browser_not_enabled", "Video detail acquisition requires --enable-browser-backends.", failure_class="user_resolvable")
            try:
                item = self._detail_provider.resolve(item)
            except DouyinDetailError as exc:
                return _failure(item, exc.code, str(exc), failure_class="source_unavailable")
            native = _native_transcript(item)
            author_text, author_text_source = _image_post_author_text(item)
        if item.source_type == "image_text":
            if not author_text.strip():
                return _failure(item, "douyin_image_post_text_unavailable", "This image post returned no author text; display titles and IDs are not used as body text.", failure_class="source_unavailable")
            return TranscriptResult(
                text=author_text, status="verified", source_level="native_text", backend=DOUYIN_POST_TEXT_BACKEND,
                title=item.title, author=item.author, published_at=item.published_at,
                source_notes=["Image-post author text retained verbatim; no media, speech recognition, or OCR was used."],
                metadata={"profile": "media", "content_id": item.id, "transcript_method": "image_post_text", "media_kind": "image_text", "content_kind": "author_text", "author_text_source": author_text_source, "scope": "image_post_author_text"},
            )
        if native:
            return TranscriptResult(
                text=native, status="verified", source_level="platform_transcript", backend=DOUYIN_NATIVE_TRANSCRIPT_BACKEND,
                source_notes=["Native caption text retained in source order, including repeated speech and real timestamps.", "Untimed captions remain untimed; no artificial zero timestamps were added."],
                title=item.title, author=item.author, published_at=item.published_at,
                metadata={"profile": "media", "content_id": item.id, "transcript_method": "native", "scope": "video_captions", "coverage": {"duration_seconds": _safe_duration(item.raw_meta.get("duration")), "timing": "source_provided_or_untimed"}},
            )
        if not (self._options.enable_media_download_backends and self._options.enable_asr_backends):
            return _failure(item, "douyin_media_asr_not_enabled", "No native captions were returned; use --enable-media-download --enable-asr.", failure_class="user_resolvable")
        if self._asr_client is not None and not self._options.enable_cloud_backends:
            return _failure(item, "douyin_cloud_backends_not_enabled", "Explicit cloud ASR requires --enable-cloud-backends before audio preparation.", failure_class="user_resolvable")
        play_url = str(item.raw_meta.get("play_url") or "").strip()
        if not play_url:
            return _failure(item, "douyin_play_url_unavailable", "Video detail returned no transient playback URL.", failure_class="source_unavailable")
        from openmd.runtime.asr import get_asr_backend

        asr = self._asr_client or self._asr_backend or get_asr_backend("asr.auto")
        try:
            available = asr.is_available()
        except Exception:
            available = False
        if not available:
            return _failure(item, "douyin_asr_unavailable", "The selected ASR runtime or existing local models are unavailable; no model was downloaded.", failure_class="user_resolvable")
        with owned_temporary_directory(prefix="openmd-douyin-transcript-") as temp_dir:
            audio_path = Path(temp_dir) / "audio.mp3"
            try:
                self._extract_audio(play_url, audio_path)
            except Exception:
                if metadata_detail is not None:
                    # Reuse the already-read detail on an expired discovery URL;
                    # no second metadata read or repeated successful download.
                    return self.extract(BackendExtractRequest(metadata_detail, (self.name,), allow_fallback=False))
                if self._options.enable_browser_backends and not item.raw_meta.get("detail_resolved") and not item.raw_meta.get("metadata_detail_attempted"):
                    try:
                        refreshed = self._detail_provider.resolve(item)
                    except DouyinDetailError as exc:
                        return _failure(item, exc.code, str(exc), failure_class="runtime_transient")
                    # The detail marker bounds refresh to one attempt. Native
                    # captions newly exposed by detail still take precedence.
                    return self.extract(BackendExtractRequest(refreshed, (self.name,), allow_fallback=False))
                # Downloader diagnostics can include the complete signed URL.
                # Persist a stable reason only, never raw stdout/stderr/argv.
                return _failure(item, "douyin_media_url_unavailable", "Temporary audio acquisition failed: media URL may have expired or contain no accessible audio.", failure_class="runtime_transient")
            try:
                if self._asr_client is not None:
                    transcript = _format_transcript(asr.transcribe(audio_path, timeout=self._asr_timeout))
                    asr_result = TranscriptResult(text=transcript, status="verified", source_level="media_asr", backend=asr.name, metadata={"asr_model": SILICONFLOW_MODEL})
                else:
                    asr_result = asr.transcribe(audio_path)
            except Exception as exc:
                no_speech_error = _asr_indicates_no_speech(str(exc)) or "no speech" in str(exc).lower()
                asr_result = TranscriptResult(text="", status="failed", source_level="partial_extraction", backend=getattr(asr, "name", "asr.auto"), error_code="asr_no_speech" if no_speech_error else "asr_runtime_error")
            if asr_result.has_usable_text:
                return replace(asr_result, source_level="media_asr", title=item.title, author=item.author, published_at=item.published_at,
                    source_notes=[*asr_result.source_notes, "Temporary mono audio was transcribed and removed; playback URLs were not retained."],
                    metadata={**asr_result.metadata, "profile": "media", "content_id": item.id, "transcript_method": "asr", "media_retention": "none", "scope": "video_audio", "coverage": {"duration_seconds": _safe_duration(item.raw_meta.get("duration")), "audio_selection": "full_audio"}})
            no_speech = not asr_result.text.strip() and asr_result.error_code in {None, "asr_empty", "asr_empty_text", "asr_no_speech", "no_speech", "empty_transcript"}
            code = "douyin_no_speech" if no_speech else "douyin_asr_unavailable" if asr_result.error_code in {"asr_runtime_unavailable", "asr_dependency_missing", "asr_model_missing"} else "douyin_asr_failed"
            if self._ocr_extractor_override is None and item.raw_meta.get("enable_ocr") is not True:
                return _failure(item, code, "No usable speech transcript was produced; ASR detail code: " + str(asr_result.error_code or "empty_transcript"), failure_class="source_unavailable" if no_speech else "runtime_transient")
            try:
                ocr_text = self._extract_ocr(item)
            except Exception:
                return _failure(item, "douyin_ocr_failed", "ASR produced no transcript and sampled hard-subtitle OCR failed.", failure_class="source_unavailable")
            if not _has_meaningful_text(ocr_text):
                return _failure(item, "douyin_no_speech_or_hard_subtitle", "No usable speech or sampled hard-subtitle text was found.", failure_class="source_unavailable")
            return TranscriptResult(text=ocr_text, status="partial", source_level="ocr", backend=DOUYIN_OCR_BACKEND,
                title=item.title, author=item.author, published_at=item.published_at,
                source_notes=["Sampled visible subtitles only; this is not a complete spoken transcript."],
                metadata={"profile": "media", "content_id": item.id, "transcript_method": "ocr", "media_retention": "none", "scope": "sampled_hard_subtitles", "coverage": {"sample_interval_seconds": 2, "max_seconds": 300, "sampled_seconds": min(_safe_duration(item.raw_meta.get("duration")) or 60, 300), "crop": "lower_52_percent", "complete": False}})

    def _extract_audio(self, play_url: str, output_path: Path) -> None:
        ffmpeg = ffmpeg_executable()
        if not ffmpeg:
            raise RuntimeError("ffmpeg is not available")
        command = [
            ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-headers",
            "User-Agent: Mozilla/5.0\r\nReferer: https://www.douyin.com/\r\n",
            "-i",
            play_url,
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-b:a",
            "48k",
            str(output_path),
        ]
        exit_code, _stdout, stderr = self._runner(command, self._ffmpeg_timeout)
        if exit_code != 0 or not output_path.is_file() or output_path.stat().st_size == 0:
            raise RuntimeError(f"ffmpeg audio extraction failed: {sanitize_error_summary(stderr)}")

    def _extract_ocr(self, item: ContentItem) -> str:
        if self._ocr_extractor_override is not None:
            return self._ocr_extractor_override(item)
        from rapidocr_onnxruntime import RapidOCR

        if self._ocr_engine is None:
            self._ocr_engine = RapidOCR()
        play_url = str(item.raw_meta.get("play_url") or "").strip()
        duration = _safe_duration(item.raw_meta.get("duration"))
        with owned_temporary_directory(prefix="openmd-douyin-ocr-") as temp_dir:
            frames_dir = Path(temp_dir)
            pattern = frames_dir / "frame_%05d.jpg"
            ffmpeg = ffmpeg_executable()
            if not ffmpeg:
                raise RuntimeError("ffmpeg is not available")
            command = [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-headers",
                "User-Agent: Mozilla/5.0\r\nReferer: https://www.douyin.com/\r\n",
                "-t",
                str(max(1, min(duration or 60, 300))),
                "-i",
                play_url,
                "-vf",
                "fps=0.5,crop=iw:ih*0.52:0:ih*0.45",
                "-q:v",
                "3",
                str(pattern),
            ]
            exit_code, _stdout, stderr = self._runner(command, self._ffmpeg_timeout)
            if exit_code != 0:
                raise RuntimeError(f"ffmpeg frame extraction failed: {sanitize_error_summary(stderr)}")
            segments: list[tuple[int, str]] = []
            last = ""
            for index, frame in enumerate(sorted(frames_dir.glob("frame_*.jpg"))):
                text = _ocr_frame(self._ocr_engine, frame)
                normalized = _SPACE_RE.sub("", text)
                if not normalized:
                    continue
                similarity = difflib.SequenceMatcher(None, normalized, last).ratio() if last else 0.0
                if similarity < 0.82:
                    segments.append((index * 2, text))
                    last = normalized
        return "\n".join(f"[{seconds // 60:02d}:{seconds % 60:02d}] {text}" for seconds, text in segments)


class DouyinCloudTranscriptProvider(DouyinTranscriptProvider):
    """Cloud is reachable only through explicit backend selection and gates."""

    name = DOUYIN_ASR_BACKEND


def _missing_metadata_fields(title: str | None, author: str | None, published_at: str | None, content_id: str | None) -> list[str]:
    return [key for key, value in (("title", title if title != content_id else None), ("author", author), ("published_at", published_at)) if not value or not value.strip()]


def _image_post_author_text(item: ContentItem) -> tuple[str, str]:
    if item.source_type != "image_text":
        return "", ""
    if "author_text" in item.raw_meta:
        text = item.raw_meta["author_text"]
        source = "detail_desc" if item.raw_meta.get("author_text_source") == "detail_desc" else "explicit_author_text"
        return (text if isinstance(text, str) else ""), source
    if item.raw_meta.get("detail_resolved"):
        return "", ""
    # Existing discovery stores the image description in title. Preserve that
    # contract for already classified image posts, including short author text.
    # Once detail has been read, only its actual description can supply a body.
    title = item.title or ""
    candidate = title.strip()
    placeholders = {"untitled", "无标题", "抖音视频", "抖音作品", "douyin video", "douyin post"}
    if not candidate or candidate == item.id or candidate.lower() in placeholders or re.fullmatch(r"\d{6,20}", candidate):
        return "", ""
    if candidate == item.url or re.fullmatch(r"(?i)(?:douyin[ -]*(?:video|post)|抖音(?:视频|作品))\s*[:：#-]?\s*\d{6,20}", candidate):
        return "", ""
    return title, "legacy_image_title"


def _run_command(command: list[str], timeout: int) -> tuple[int, str, str]:
    completed = run_interruptibly(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )
    return completed.returncode, completed.stdout, completed.stderr


def _native_transcript(item: ContentItem) -> str:
    # Choose one native source; never concatenate alternative caption tracks.
    for key in ("transcript_candidates", "subtitles", "caption_info", "captionInfo"):
        candidates = item.raw_meta.get(key)
        if not candidates:
            continue
        lines: list[str] = []
        def visit(candidate: object) -> None:
            if isinstance(candidate, list):
                for segment in candidate:
                    visit(segment)
                return
            if isinstance(candidate, str):
                # Caption download locators are not caption bodies. Keep them
                # transient and let audio ASR supply the requested transcript.
                if re.fullmatch(r"https?://\S+", candidate.strip(), re.IGNORECASE):
                    return
                lines.extend(line.strip() for line in candidate.replace("\r\n", "\n").splitlines() if line.strip())
                return
            if not isinstance(candidate, dict):
                return
            for nested in ("segments", "sentences", "captions", "subtitles", "items", "list"):
                if isinstance(candidate.get(nested), list):
                    visit(candidate[nested])
                    return
            text = str(candidate.get("text") or candidate.get("content") or candidate.get("utterance") or "").strip()
            if not text or not _TEXT_TOKEN_RE.search(text):
                return
            start = candidate.get("start", candidate.get("start_time"))
            if candidate.get("start_ms") is not None:
                try:
                    start = float(candidate["start_ms"]) / 1000
                except (ValueError, TypeError):
                    start = None
            anchor = ""
            if start is not None and not _TIMESTAMP_PREFIX_RE.match(text):
                try:
                    seconds = max(0, int(float(start)))
                    anchor = f"[{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}] "
                except (ValueError, TypeError, OverflowError):
                    pass
            lines.append(anchor + text)
        visit(candidates)
        if lines:
            return "\n".join(lines) + "\n"
    return ""


def _format_transcript(text: str) -> str:
    # Cloud text has no reliable timestamps; preserve wording without inventing anchors.
    cleaned = re.sub(r"<\|[^|]+?\|>", "", text)
    return cleaned.replace("\r\n", "\n").replace("\r", "\n").strip() + ("\n" if cleaned.strip() else "")


def _has_meaningful_text(value: str) -> bool:
    tokens = _TEXT_TOKEN_RE.findall(value or "")
    return len(tokens) >= 2


def _asr_indicates_no_speech(error_summary: str | None) -> bool:
    value = str(error_summary or "").lower()
    return "no meaningful text" in value or "no meaningful speech" in value


def _safe_duration(value: object) -> int:
    try:
        return max(0, int(float(str(value or 0).rstrip("s"))))
    except (TypeError, ValueError):
        return 0


def _ocr_frame(ocr: Any, image_path: Path) -> str:
    result, _elapsed = ocr(str(image_path))
    if not result:
        return ""
    items: list[tuple[int, float, str]] = []
    for box, text, score in result:
        if float(score) < 0.55:
            continue
        cleaned = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9%，。！？、：；（）《》“”+&./:_()' -]", "", str(text or ""))
        cleaned = _SPACE_RE.sub(" ", cleaned).strip()
        if not cleaned or not (_CHINESE_RE.search(cleaned) or re.search(r"[A-Za-z]{2,}", cleaned)):
            continue
        xs = [float(point[0]) for point in box]
        ys = [float(point[1]) for point in box]
        items.append((int(round(min(ys) / 28)), min(xs), cleaned))
    items.sort(key=lambda item: (item[0], item[1]))
    return "".join(text for _line, _x, text in items)


def _failure(
    item: ContentItem,
    code: str,
    summary: object,
    *,
    failure_class: str = "code_resolvable",
    notes: list[str] | None = None,
) -> TranscriptResult:
    return TranscriptResult(
        text="",
        status="failed",
        source_level="partial_extraction",
        backend=DOUYIN_TRANSCRIPT_BACKEND,
        source_notes=["Douyin transcript provider returned a structured failure.", *(notes or [])],
        error_code=code,
        error_summary=sanitize_error_summary(summary),
        title=item.title,
        author=item.author,
        published_at=item.published_at,
        metadata={
            "profile": "media",
            "content_id": item.id,
            "failure_class": failure_class,
            "media_retention": "none",
        },
    )


def _multipart_body(
    boundary: str,
    *,
    fields: dict[str, str],
    file_field: str,
    file_path: Path,
    content_type: str,
) -> bytes:
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.extend(
            [
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
                value.encode("utf-8"),
                b"\r\n",
            ]
        )
    chunks.extend(
        [
            f"--{boundary}\r\n".encode(),
            (
                f'Content-Disposition: form-data; name="{file_field}"; filename="{file_path.name}"\r\n'
                f"Content-Type: {content_type}\r\n\r\n"
            ).encode(),
            file_path.read_bytes(),
            b"\r\n",
            f"--{boundary}--\r\n".encode(),
        ]
    )
    return b"".join(chunks)


def _asr_text_from_payload(payload: object) -> str:
    if not isinstance(payload, dict):
        return ""
    text = str(payload.get("text") or payload.get("transcription") or "").strip()
    data = payload.get("data")
    if not text and isinstance(data, dict):
        text = str(data.get("text") or "").strip()
    segments = payload.get("segments")
    if not text and isinstance(segments, list):
        text = "\n".join(
            str(segment.get("text") or "").strip()
            for segment in segments
            if isinstance(segment, dict) and str(segment.get("text") or "").strip()
        )
    return text
