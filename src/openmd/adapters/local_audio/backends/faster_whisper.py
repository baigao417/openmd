"""Optional faster-whisper backend for local audio files.

The dependency is imported lazily so normal Openmd imports, mock lanes, and
safe skeleton discovery never download models or run ASR.  This backend only
transcribes user-local files; URL media download/extraction remains a separate
explicit media lane.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from typing import Any

from openmd.adapters.local_audio.asr import ASRBackend, MissingASRBackend
from openmd.core.models import TranscriptResult
from openmd.core.secrets import sanitize_error_summary


DEFAULT_MODEL_SIZE = "large-v3"
DEFAULT_DEVICE = "cpu"
DEFAULT_COMPUTE_TYPE = "int8"
DEFAULT_LANGUAGE = None
DEFAULT_BEAM_SIZE = 5
DEFAULT_VAD_MIN_SILENCE_MS = 700
DEFAULT_INITIAL_PROMPT = (
    "这是中文播客或访谈，请保留中文专有名词、英文缩写、数字和人名。"
    "常见词包括：搞钱女孩、大漂亮爱吃肉、梨涡小雨、Leiya、刘六六、Shelly、"
    "小红书、LinkedIn、ETF、SOP、AI、自媒体、创业、投资理财。"
)
DEFAULT_HOTWORDS = (
    "搞钱女孩 大漂亮爱吃肉 梨涡小雨 Leiya 刘六六 Shelly "
    "小红书 LinkedIn ETF SOP AI 自媒体 创业 投资理财"
)
# Decode settings shared by the transcribe call and the execution fingerprint.
FIXED_DECODE_OPTIONS: dict[str, Any] = {"best_of": 5, "temperature": 0.0, "vad_filter": True}


class FasterWhisperASRBackend(ASRBackend):
    name = "asr.faster_whisper"

    def __init__(
        self,
        *,
        model_size: str | None = None,
        device: str | None = None,
        compute_type: str | None = None,
        language: str | None = None,
        beam_size: int = DEFAULT_BEAM_SIZE,
        vad_min_silence_ms: int = DEFAULT_VAD_MIN_SILENCE_MS,
        initial_prompt: str | None = None,
        hotwords: str | None = None,
        condition_on_previous_text: bool = False,
        profile: str | None = None,
    ) -> None:
        self.profile = profile or os.getenv("OPENMD_FASTER_WHISPER_PROFILE") or "general_v1"
        if self.profile not in {"general_v1", "zh_podcast_v2"}:
            raise ValueError("Unknown faster-whisper profile; use general_v1 or zh_podcast_v2")
        podcast = self.profile == "zh_podcast_v2"
        self.model_size = model_size or os.getenv("OPENMD_FASTER_WHISPER_MODEL") or DEFAULT_MODEL_SIZE
        self.device = device or os.getenv("OPENMD_FASTER_WHISPER_DEVICE") or DEFAULT_DEVICE
        self.compute_type = (
            compute_type
            or os.getenv("OPENMD_FASTER_WHISPER_COMPUTE_TYPE")
            or DEFAULT_COMPUTE_TYPE
        )
        selected_language = language if language is not None else os.getenv(
            "OPENMD_FASTER_WHISPER_LANGUAGE", "zh" if podcast else ""
        )
        self.language = selected_language or DEFAULT_LANGUAGE
        self.beam_size = beam_size
        self.vad_min_silence_ms = vad_min_silence_ms
        self.initial_prompt = initial_prompt if initial_prompt is not None else os.getenv(
            "OPENMD_FASTER_WHISPER_INITIAL_PROMPT", DEFAULT_INITIAL_PROMPT if podcast else ""
        )
        self.hotwords = hotwords if hotwords is not None else os.getenv(
            "OPENMD_FASTER_WHISPER_HOTWORDS", DEFAULT_HOTWORDS if podcast else ""
        )
        self.condition_on_previous_text = condition_on_previous_text

    def execution_config(self) -> dict[str, Any]:
        """Effective result settings; construction never imports or downloads a model."""
        return {
            "profile": self.profile,
            "model": self.model_size,
            "language": self.language,
            "device": self.device,
            "compute_type": self.compute_type,
            "beam_size": self.beam_size,
            **FIXED_DECODE_OPTIONS,
            "vad_min_silence_ms": self.vad_min_silence_ms,
            "initial_prompt": self.initial_prompt,
            "hotwords": self.hotwords,
            "condition_on_previous_text": self.condition_on_previous_text,
        }

    def is_available(self) -> bool:
        return importlib.util.find_spec("faster_whisper") is not None

    def transcribe(self, path: Path) -> TranscriptResult:
        source_path = path.expanduser()
        if not source_path.is_file():
            return _failed_result(
                self.name,
                source_path,
                "asr_local_file_required",
                "faster-whisper ASR requires a local audio file path.",
            )

        if not self.is_available():
            return MissingASRBackend(self.name).transcribe(source_path)

        try:
            from faster_whisper import WhisperModel

            model = WhisperModel(
                self.model_size,
                device=self.device,
                compute_type=self.compute_type,
            )
            kwargs: dict[str, Any] = {
                "beam_size": self.beam_size,
                **FIXED_DECODE_OPTIONS,
                "vad_parameters": {"min_silence_duration_ms": self.vad_min_silence_ms},
                "condition_on_previous_text": self.condition_on_previous_text,
                "initial_prompt": self.initial_prompt or None,
                "hotwords": self.hotwords or None,
            }
            if self.language:
                kwargs["language"] = self.language
            segments, info = model.transcribe(str(source_path), **kwargs)
            lines = _format_segments(segments)
        except Exception as exc:  # pragma: no cover - dependency/model safety net
            return _failed_result(self.name, source_path, "asr_extraction_failed", exc)

        text = "\n".join(lines).strip()
        if not text:
            return _failed_result(
                self.name,
                source_path,
                "asr_empty_text",
                "faster-whisper returned no recognized text.",
            )

        return TranscriptResult(
            text=f"{text}\n",
            status="verified",
            source_level="asr",
            backend=self.name,
            source_notes=[
                "faster-whisper transcribed a local audio file.",
                "Raw audio file was referenced, not copied into the package.",
            ],
            title=source_path.stem or "local audio",
            metadata={
                "profile": "media",
                "asr_backend": self.name,
                "model_size": self.model_size,
                "device": self.device,
                "compute_type": self.compute_type,
                "language": getattr(info, "language", None) or self.language,
                "duration": getattr(info, "duration", None),
                "segment_count": len(lines),
                "beam_size": self.beam_size,
                "vad_min_silence_ms": self.vad_min_silence_ms,
                "condition_on_previous_text": self.condition_on_previous_text,
                "quality_profile": self.profile,
            },
        )


def create_backend() -> FasterWhisperASRBackend:
    return FasterWhisperASRBackend()


def _format_segments(segments: Any) -> list[str]:
    lines: list[str] = []
    for segment in segments:
        text = str(getattr(segment, "text", "")).strip()
        if not text:
            continue
        start = getattr(segment, "start", None)
        end = getattr(segment, "end", None)
        if isinstance(start, (int, float)) and isinstance(end, (int, float)):
            prefix = f"[{_timestamp(float(start))}-{_timestamp(float(end))}]"
        elif isinstance(start, (int, float)):
            prefix = f"[{_timestamp(float(start))}]"
        else:
            prefix = "[00:00:00]"
        lines.append(f"{prefix} {text}")
    return lines


def _timestamp(seconds: float) -> str:
    value = max(0, int(seconds))
    hours, remainder = divmod(value, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def _failed_result(
    backend: str,
    path: Path,
    code: str,
    error: object,
) -> TranscriptResult:
    return TranscriptResult(
        text="",
        status="failed",
        source_level="partial_extraction",
        backend=backend,
        source_notes=[
            "local audio ASR backend did not produce usable text.",
            "Raw audio file was referenced, not copied into the package.",
        ],
        error_code=code,
        error_summary=sanitize_error_summary(error),
        title=path.stem or "local audio",
        metadata={"profile": "media", "asr_backend": backend, "source_ref": str(path)},
    )
