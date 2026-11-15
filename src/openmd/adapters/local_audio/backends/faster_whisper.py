"""Optional faster-whisper backend for local audio files.

The dependency is imported lazily so normal Openmd imports, mock lanes, and
safe skeleton discovery never download models or run ASR.  This backend only
transcribes user-local files; URL media download/extraction remains a separate
explicit media lane.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from typing import Any

from openmd.adapters.local_audio.asr import ASRBackend, MissingASRBackend
from openmd.core.models import TranscriptResult
from openmd.core.secrets import sanitize_error_summary
from openmd.runtime.models import ModelNotFoundError, model_label, offline_model_loading, require_model, resolve_model


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
# Keep handles alive: closing or collecting them removes Windows DLL directories.
_DLL_DIRECTORY_HANDLES: dict[str, Any] = {}


def _prepare_windows_dll_paths() -> None:
    """Expose optional pip-installed NVIDIA runtimes before loading CTranslate2."""
    if sys.platform != "win32":
        return
    spec = importlib.util.find_spec("faster_whisper")
    if spec is None or spec.origin is None:
        return
    nvidia_root = Path(spec.origin).resolve().parent.parent / "nvidia"
    directories = sorted(path.resolve() for path in nvidia_root.glob("*/bin") if path.is_dir())
    # A CUDA torch install ships cuBLAS/cuDNN in torch/lib; reuse them without importing torch.
    torch_lib = nvidia_root.parent / "torch" / "lib"
    if torch_lib.is_dir():
        directories.append(torch_lib.resolve())
    existing = {
        os.path.normcase(os.path.abspath(path))
        for path in os.environ.get("PATH", "").split(os.pathsep)
        if path
    }
    additions: list[str] = []
    for directory in directories:
        value = str(directory)
        key = os.path.normcase(value)
        if key not in _DLL_DIRECTORY_HANDLES:
            _DLL_DIRECTORY_HANDLES[key] = os.add_dll_directory(value)
        if key not in existing:
            additions.append(value)
            existing.add(key)
    if additions:
        os.environ["PATH"] = os.pathsep.join([*additions, os.environ.get("PATH", "")])


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
        self.chinese_script = os.getenv("OPENMD_ASR_CHINESE_SCRIPT", "simplified")

    def execution_config(self) -> dict[str, Any]:
        """Effective result settings; construction never imports or downloads a model."""
        return {
            "profile": self.profile,
            "model": model_label(self.model_size),
            "model_origin": resolve_model(self.model_size, family="whisper").origin,
            "local_files_only": True,
            "language": self.language,
            "device": self.device,
            "compute_type": self.compute_type,
            "beam_size": self.beam_size,
            **FIXED_DECODE_OPTIONS,
            "vad_min_silence_ms": self.vad_min_silence_ms,
            "initial_prompt": self.initial_prompt,
            "hotwords": self.hotwords,
            "condition_on_previous_text": self.condition_on_previous_text,
            "chinese_script": self.chinese_script,
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
            model_path = require_model(self.model_size, family="whisper")
            _prepare_windows_dll_paths()
            from faster_whisper import WhisperModel

            with offline_model_loading():
                model = WhisperModel(
                    str(model_path),
                    device=self.device,
                    compute_type=self.compute_type,
                    local_files_only=True,
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
        except ModelNotFoundError as exc:
            return _failed_result(self.name, source_path, "asr_model_missing", exc)
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

        detected_language = getattr(info, "language", None) or self.language
        text, script_note, applied_script = _normalize_chinese_script(text, detected_language, self.chinese_script)

        return TranscriptResult(
            text=f"{text}\n",
            status="verified",
            source_level="asr",
            backend=self.name,
            source_notes=[
                "faster-whisper transcribed a local audio file.",
                "Raw audio file was referenced, not copied into the package.",
                *([script_note] if script_note else []),
            ],
            title=source_path.stem or "local audio",
            metadata={
                "profile": "media",
                "asr_backend": self.name,
                "chinese_script": applied_script,
                "model_size": model_label(self.model_size),
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


def _normalize_chinese_script(text: str, language: str | None, script: str) -> tuple[str, str | None, str | None]:
    """Whisper mixes Traditional into Mandarin output even with a Simplified prompt.

    Only the character form changes; wording is untouched. Cantonese keeps its own form.
    """
    if language != "zh" or script != "simplified":
        return text, None, None
    try:
        from opencc import OpenCC
    except ImportError:
        return text, "OpenCC is not installed, so Traditional characters were left unconverted.", "original"
    return OpenCC("t2s").convert(text), "Traditional characters were converted to Simplified with OpenCC; wording is unchanged.", "simplified"


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
        metadata={"profile": "media", "asr_backend": backend, "source_ref": path.name},
    )
