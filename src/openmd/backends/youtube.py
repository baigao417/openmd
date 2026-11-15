"""YouTube-specific opt-in backend providers.

This extension module is loaded by ``openmd.backends.registry`` using the
platform naming convention. Providers here stay behind explicit runtime gates;
importing the module does not touch the network, browser sessions, cookies, or
media files.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable, Iterable
from typing import Any

from openmd.core.opencli_output import load_opencli_json
from openmd.core.models import ContentItem, TranscriptResult
from openmd.core.secrets import sanitize_error_summary

from .base import BackendCapability, BackendDescriptor, BackendExtractRequest, BackendHealth
from .youtube_discovery import _run_opencli
from .youtube_transcript import extract_youtube_video_id


OPENCLI_YOUTUBE_TRANSCRIPT_BACKEND = "opencli.youtube.transcript"
DEFAULT_TRANSCRIPT_ATTEMPTS: tuple[tuple[str, str], ...] = (
    ("grouped", "md"),
    ("grouped", "json"),
    ("raw", "md"),
    ("raw", "json"),
)
DEFAULT_MIN_TRANSCRIPT_CHARS = 20

OpenCliRunner = Callable[..., tuple[int, str, str]]


def register_backend_providers(registry: Any, options: Any) -> None:
    """Register YouTube extraction providers under explicit runtime gates."""
    if getattr(options, "enable_browser_backends", False):
        registry.register(OPENCLI_YOUTUBE_TRANSCRIPT_BACKEND, OpenCliYouTubeTranscriptProvider())


def build_opencli_youtube_transcript_args(video_id: str, *, mode: str, output_format: str) -> list[str]:
    """Build a direct-video-id OpenCLI transcript command."""
    return [
        "opencli",
        "youtube",
        "transcript",
        video_id,
        "--mode",
        mode,
        "-f",
        output_format,
    ]


def classify_youtube_transcript_failure(message: object) -> str:
    """Map known YouTube transcript blockers to stable failure codes."""
    text = str(message or "").lower()
    if "short_transcript" in text or "short transcript" in text:
        return "youtube_transcript_short_result"
    if "empty_result" in text or "empty result" in text or "returned no transcript" in text:
        return "youtube_transcript_empty_result"
    if "confirm you're not a bot" in text or "not a bot" in text or "bot check" in text:
        return "youtube_bot_check"
    if "cookie" in text or "cookies" in text or "login required" in text or "sign in" in text:
        return "youtube_cookie_or_auth_required"
    if "asr" in text or "audio transcription" in text or "download audio" in text:
        return "youtube_asr_required"
    if "timed out" in text or "timeout" in text:
        return "opencli_youtube_transcript_timeout"
    if "caption" in text and ("disabled" in text or "unavailable" in text or "not found" in text):
        return "youtube_transcript_not_found"
    if "blocked" in text:
        return "youtube_request_blocked"
    return "opencli_youtube_transcript_failed"


class OpenCliYouTubeTranscriptProvider:
    """Fetch existing YouTube captions through OpenCLI using direct video ids."""

    name = OPENCLI_YOUTUBE_TRANSCRIPT_BACKEND

    def __init__(
        self,
        *,
        runner: OpenCliRunner | None = None,
        timeout: int = 120,
        attempts: tuple[tuple[str, str], ...] = DEFAULT_TRANSCRIPT_ATTEMPTS,
        min_transcript_chars: int = DEFAULT_MIN_TRANSCRIPT_CHARS,
    ) -> None:
        self._runner = runner or _run_opencli
        self.timeout = timeout
        self.attempts = attempts
        self.min_transcript_chars = max(0, min_transcript_chars)

    def descriptor(self) -> BackendDescriptor:
        capability = BackendCapability(
            platforms=("youtube",),
            source_types=("video",),
            input_types=("single", "collection"),
            source_levels=("native_caption", "auto_caption", "third_party_extractor"),
            requires_network=True,
            requires_credentials=False,
            optional_config=("mode", "format", "timeout"),
        )
        return BackendDescriptor(
            name=self.name,
            layer="L2 browser/session extractor",
            capabilities=(capability,),
            description="Fetch existing YouTube captions through OpenCLI without media download or ASR.",
            provider="opencli.youtube",
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
        if item.platform != "youtube":
            return _failed(
                item,
                "unsupported_platform",
                f"OpenCLI YouTube transcript only supports youtube, got {item.platform}.",
            )

        video_ids = _video_id_candidates(item)
        if not video_ids:
            return _failed(item, "invalid_input", "Could not extract a YouTube video id.")

        last_error: object = "OpenCLI YouTube transcript returned no usable text."
        last_code = "opencli_youtube_transcript_failed"
        attempted: list[dict[str, object]] = []

        for video_id in video_ids:
            for mode, output_format in self.attempts:
                args = build_opencli_youtube_transcript_args(
                    video_id,
                    mode=mode,
                    output_format=output_format,
                )
                exit_code, stdout, stderr = self._runner(args, timeout=self.timeout)
                attempted.append(
                    {
                        "video_id": video_id,
                        "mode": mode,
                        "format": output_format,
                        "exit_code": exit_code,
                    }
                )

                signal = stderr or stdout
                if exit_code != 0:
                    last_error = signal or f"OpenCLI exited with code {exit_code}."
                    last_code = classify_youtube_transcript_failure(last_error)
                    if _is_hard_blocker(last_code):
                        return _failed(item, last_code, last_error, video_id=video_id, attempted=attempted)
                    continue

                text = _extract_transcript_text(stdout, output_format=output_format)
                if _is_usable_text(text, self.min_transcript_chars):
                    return TranscriptResult(
                        text=text.rstrip() + "\n",
                        status="verified",
                        source_level="third_party_extractor",
                        backend=self.name,
                        source_notes=[
                            "OpenCLI fetched existing YouTube transcript text.",
                            "Direct video-id command was used; no URL pre-navigation retry was needed.",
                            "No cookie export, media download, or ASR action was attempted.",
                            f"mode: {mode}",
                            f"format: {output_format}",
                        ],
                        metadata={
                            "video_id": video_id,
                            "mode": mode,
                            "format": output_format,
                            "attempt_count": len(attempted),
                        },
                    )

                last_error = stdout or "EMPTY_RESULT: OpenCLI returned no transcript text."
                last_code = (
                    "youtube_transcript_short_result"
                    if text.strip()
                    else classify_youtube_transcript_failure(last_error)
                )

        return _failed(item, last_code, last_error, video_id=video_ids[-1], attempted=attempted)


def _video_id_candidates(item: ContentItem) -> tuple[str, ...]:
    candidates = [extract_youtube_video_id(item.url)]
    if item.id:
        candidates.append(extract_youtube_video_id(item.id) or item.id)

    deduped: list[str] = []
    for candidate in candidates:
        if candidate and candidate not in deduped:
            deduped.append(candidate)
    return tuple(deduped)


def _extract_transcript_text(stdout: str, *, output_format: str) -> str:
    if output_format == "json":
        try:
            payload = load_opencli_json(stdout)
        except json.JSONDecodeError:
            return stdout.strip()
        return _text_from_json_payload(payload).strip()
    return stdout.strip()


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


def _is_usable_text(text: str, min_chars: int) -> bool:
    return len("".join(text.split())) >= min_chars


def _is_hard_blocker(code: str) -> bool:
    return code in {"youtube_bot_check", "youtube_cookie_or_auth_required", "youtube_asr_required"}


def _failed(
    item: ContentItem,
    code: str,
    error: object,
    *,
    video_id: str | None = None,
    attempted: Iterable[dict[str, object]] = (),
) -> TranscriptResult:
    metadata: dict[str, object] = {"platform": item.platform}
    if video_id:
        metadata["video_id"] = video_id
    attempted_list = list(attempted)
    if attempted_list:
        metadata["attempted"] = attempted_list
        metadata["attempt_count"] = len(attempted_list)
    return TranscriptResult(
        text="",
        status="failed",
        source_level="partial_extraction",
        backend=OPENCLI_YOUTUBE_TRANSCRIPT_BACKEND,
        source_notes=[
            "OpenCLI YouTube transcript returned structured failure.",
            "No cookie export, media download, or ASR action was attempted.",
        ],
        error_code=code,
        error_summary=sanitize_error_summary(error),
        metadata=metadata,
    )
