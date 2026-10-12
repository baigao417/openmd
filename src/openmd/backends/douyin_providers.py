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
from pathlib import Path
from typing import Any, Protocol

from openmd.backends.base import BackendCapability, BackendDescriptor, BackendExtractRequest, BackendHealth
from openmd.backends.executor import BackendProviderRegistry
from openmd.backends.registry import BackendRuntimeOptions
from openmd.core.models import ContentItem, TranscriptResult
from openmd.core.secrets import sanitize_error_summary


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
        ffmpeg_timeout: int = 600,
        asr_timeout: int = 300,
    ) -> None:
        self._options = options
        self._runner = runner or _run_command
        self._asr_client = asr_client or SiliconFlowASRClient()
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
                    source_levels=("platform_transcript", "media_asr", "ocr", "partial_extraction"),
                    supports_batch=True,
                    supports_collection=True,
                    requires_network=True,
                    requires_credentials=True,
                    optional_config=(
                        "enable_media_download_backends",
                        "enable_asr_backends",
                        "enable_cloud_backends",
                        "SILICONFLOW_API_KEY",
                    ),
                ),
            ),
            description="Douyin native transcript first, then transient ffmpeg audio + ASR, then OCR.",
            provider="openmd.backends.douyin_providers",
        )

    def health(self) -> BackendHealth:
        ffmpeg_ok = shutil.which("ffmpeg") is not None
        asr_ok = self._asr_client.is_available()
        warnings: list[str] = []
        if not (
            self._options.enable_media_download_backends
            and self._options.enable_asr_backends
            and self._options.enable_cloud_backends
        ):
            warnings.append("Douyin media ASR fallback gates are closed; native transcript fields still work.")
        return BackendHealth(
            ok=True,
            dependencies={
                "ffmpeg": "ok" if ffmpeg_ok else "missing",
                self._asr_client.name: "ok" if asr_ok else "missing",
            },
            warnings=tuple(warnings),
        )

    def extract(self, request: BackendExtractRequest) -> TranscriptResult:
        item = request.item
        if item.platform != "douyin":
            return _failure(item, "invalid_input", "Douyin transcript provider only supports Douyin.")

        native = _native_transcript(item)
        if native:
            return TranscriptResult(
                text=native,
                status="verified",
                source_level="platform_transcript",
                backend=DOUYIN_NATIVE_TRANSCRIPT_BACKEND,
                source_notes=[
                    "Douyin native transcript/video-text candidates were normalized in memory.",
                    "No playback URL, raw response, cookie, token, header, or media artifact was persisted.",
                ],
                title=item.title,
                author=item.author,
                published_at=item.published_at,
                metadata={"profile": "media", "content_id": item.id, "transcript_method": "native"},
            )

        if not (self._options.enable_media_download_backends and self._options.enable_asr_backends):
            return _failure(
                item,
                "douyin_media_asr_not_enabled",
                "No native transcript was available; rerun with --enable-media-download --enable-asr.",
                failure_class="user_resolvable",
            )
        if not self._options.enable_cloud_backends:
            return _failure(
                item,
                "douyin_cloud_backends_not_enabled",
                "No native transcript was available; rerun with --enable-cloud-backends before cloud ASR media preparation.",
                failure_class="user_resolvable",
            )
        play_url = str(item.raw_meta.get("play_url") or "").strip()
        if not play_url:
            return _failure(
                item,
                "douyin_play_url_unavailable",
                "The discovery provider did not expose a usable transient playback URL.",
                failure_class="source_unavailable",
            )

        asr_error: str | None = None
        with tempfile.TemporaryDirectory(prefix="openmd-douyin-transcript-") as temp_dir:
            audio_path = Path(temp_dir) / f"{item.id or 'douyin'}.mp3"
            try:
                self._extract_audio(play_url, audio_path)
                asr_text = self._asr_client.transcribe(audio_path, timeout=self._asr_timeout)
                transcript = _format_transcript(asr_text)
                if not _has_meaningful_text(transcript):
                    raise RuntimeError("ASR transcript contained no meaningful text")
                return TranscriptResult(
                    text=transcript,
                    status="verified",
                    source_level="media_asr",
                    backend=self._asr_client.name,
                    source_notes=[
                        "No native Douyin transcript was available.",
                        "ffmpeg extracted temporary mono audio and SiliconFlow ASR produced the transcript.",
                        "Temporary audio was deleted and playback URLs were not persisted.",
                    ],
                    title=item.title,
                    author=item.author,
                    published_at=item.published_at,
                    metadata={
                        "profile": "media",
                        "content_id": item.id,
                        "transcript_method": "asr",
                        "asr_model": SILICONFLOW_MODEL,
                        "media_retention": "none",
                    },
                )
            except Exception as exc:  # noqa: BLE001 - OCR is the approved fallback for ASR/no-speech failures.
                asr_error = sanitize_error_summary(exc)

            if item.source_type == "image_text" and _asr_indicates_no_speech(asr_error):
                post_text = str(item.title or "").strip()
                if _has_meaningful_text(post_text):
                    return TranscriptResult(
                        text=post_text + "\n",
                        status="verified",
                        source_level="native_text",
                        backend=DOUYIN_POST_TEXT_BACKEND,
                        source_notes=[
                            f"The image post audio contained no usable speech: {asr_error}",
                            "The creator-authored post description was preserved as the raw content fallback.",
                            "No playback URL, image URL, raw response, or media artifact was persisted.",
                        ],
                        title=item.title,
                        author=item.author,
                        published_at=item.published_at,
                        metadata={
                            "profile": "document",
                            "content_id": item.id,
                            "transcript_method": "image_post_text",
                            "media_retention": "none",
                        },
                    )

            try:
                ocr_text = self._extract_ocr(item)
            except Exception as exc:  # noqa: BLE001 - return one structured terminal failure.
                return _failure(
                    item,
                    "douyin_transcript_unavailable",
                    f"ASR failed ({asr_error}); OCR fallback failed ({sanitize_error_summary(exc)}).",
                    failure_class="source_unavailable",
                    notes=["Temporary audio/frames were deleted after the failed fallback ladder."],
                )
            if not _has_meaningful_text(ocr_text):
                return _failure(
                    item,
                    "douyin_no_speech_or_hard_subtitle",
                    f"ASR failed ({asr_error}); OCR found no meaningful hard-subtitle text.",
                    failure_class="source_unavailable",
                )
            return TranscriptResult(
                text=ocr_text,
                status="verified",
                source_level="ocr",
                backend=DOUYIN_OCR_BACKEND,
                source_notes=[
                    f"ASR did not produce usable speech text: {asr_error}",
                    "RapidOCR recovered visible hard-subtitle text from temporary sampled frames.",
                    "Temporary frames and playback URLs were not persisted.",
                ],
                title=item.title,
                author=item.author,
                published_at=item.published_at,
                metadata={
                    "profile": "media",
                    "content_id": item.id,
                    "transcript_method": "ocr",
                    "media_retention": "none",
                },
            )

    def _extract_audio(self, play_url: str, output_path: Path) -> None:
        ffmpeg = shutil.which("ffmpeg")
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
        with tempfile.TemporaryDirectory(prefix="openmd-douyin-ocr-") as temp_dir:
            frames_dir = Path(temp_dir)
            pattern = frames_dir / "frame_%05d.jpg"
            ffmpeg = shutil.which("ffmpeg")
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


def _run_command(command: list[str], timeout: int) -> tuple[int, str, str]:
    completed = subprocess.run(
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
    candidates = item.raw_meta.get("transcript_candidates")
    if not isinstance(candidates, list):
        return ""
    lines: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        text = str(candidate.get("text") or "").strip()
        for raw_line in text.replace("\r\n", "\n").replace("\r", "\n").splitlines():
            line = _TIMESTAMP_PREFIX_RE.sub("", raw_line).strip()
            if not _has_meaningful_text(line) or line in seen:
                continue
            seen.add(line)
            lines.append(f"[00:00:00] {line}")
    return "\n".join(lines).strip() + ("\n" if lines else "")


def _format_transcript(text: str) -> str:
    cleaned = re.sub(r"<\|[^|]+?\|>", "", text)
    cleaned = cleaned.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not cleaned:
        return ""
    parts = [part.strip() for part in cleaned.splitlines() if part.strip()]
    if len(parts) == 1:
        parts = [part.strip() for part in re.split(r"(?<=[。！？!?])\s*", parts[0]) if part.strip()]
    return "\n".join(f"[00:00:00] {part}" for part in parts).strip() + "\n"


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
