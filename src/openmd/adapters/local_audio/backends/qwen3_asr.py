"""Local Qwen3-ASR backend.

Qwen3-ASR scored best on the Chinese benchmark but sends a whole file to the GPU at
once, which exhausts an 8 GB card on long audio. Audio is therefore cut at FunASR VAD
speech boundaries first; each line keeps the start time of its chunk. Heavy imports
happen only inside ``transcribe``.
"""

from __future__ import annotations

import gc
import importlib.util
import os
import sys
import time
from pathlib import Path
from typing import Any

from openmd.adapters.local_audio.asr import ASRBackend, MissingASRBackend
from openmd.core.models import TranscriptResult
from openmd.core.local_config import qwen3_batch_size, validate_qwen3_batch_size
from openmd.core.secrets import sanitize_error_summary
from openmd.runtime.models import ModelNotFoundError, model_label, offline_model_loading, require_model, resolve_model
from openmd.runtime.tempdirs import owned_temporary_directory
from openmd.runtime.interpreters import run_interruptibly

DEFAULT_MODEL = "Qwen/Qwen3-ASR-1.7B"
DEFAULT_VAD_MODEL = "iic/speech_fsmn_vad_zh-cn-16k-common-pytorch"
DEFAULT_DEVICE = "cuda:0"
MAX_CHUNK_MS = 25_000
SAMPLE_RATE = 16_000


class Qwen3ASRBackend(ASRBackend):
    name = "asr.qwen3"

    def __init__(
        self,
        *,
        model: str | None = None,
        vad_model: str | None = None,
        device: str | None = None,
        language: str | None = None,
        batch_size: int | None = None,
    ) -> None:
        self.model = model or os.getenv("OPENMD_QWEN3_MODEL") or DEFAULT_MODEL
        self.vad_model = vad_model or os.getenv("OPENMD_QWEN3_VAD_MODEL") or DEFAULT_VAD_MODEL
        self.device = device or os.getenv("OPENMD_QWEN3_DEVICE") or DEFAULT_DEVICE
        self.language = language or os.getenv("OPENMD_QWEN3_LANGUAGE") or None
        self.batch_size = qwen3_batch_size() if batch_size is None else validate_qwen3_batch_size(batch_size)
        self._run_metadata: dict[str, Any] = {}

    def execution_config(self) -> dict[str, Any]:
        return {
            "model": model_label(self.model),
            "model_origin": resolve_model(self.model, family="qwen3").origin,
            "vad_model": model_label(self.vad_model),
            "vad_origin": resolve_model(self.vad_model, family="vad").origin,
            "local_files_only": True,
            "device": self.device,
            "language": self.language,
            "max_chunk_ms": MAX_CHUNK_MS,
            "batch_size": self.batch_size,
        }

    def is_available(self) -> bool:
        return all(importlib.util.find_spec(name) is not None for name in ("qwen_asr", "funasr", "soundfile"))

    def transcribe(self, path: Path) -> TranscriptResult:
        self._run_metadata = {}
        source_path = path.expanduser()
        if not source_path.is_file():
            return _failed(self.name, "asr_file_not_found", f"Local audio file not found: {source_path.name}")
        if not self.is_available():
            return MissingASRBackend(self.name).transcribe(source_path)

        failure: TranscriptResult | None = None
        try:
            lines, languages, duration = self._run(source_path)
        except ModelNotFoundError as exc:
            failure = _failed(self.name, "asr_model_missing", exc)
        except Exception as exc:  # pragma: no cover - dependency/model safety net
            failure = _failed(self.name, "asr_extraction_failed", exc)
        if failure is not None:
            # The exception and its model-owning traceback have now been released.
            # This also permits asr.auto's Whisper fallback in the same process.
            gc.collect()
            torch = sys.modules.get("torch")
            if torch is not None and torch.cuda.is_available():
                torch.cuda.empty_cache()
            failure.metadata.update(self._run_metadata)
            return failure

        if not lines:
            failure = _failed(self.name, "asr_empty_text", "Qwen3-ASR returned no recognized text.")
            failure.metadata.update(self._run_metadata)
            return failure
        return TranscriptResult(
            text="\n".join(lines) + "\n",
            status="verified",
            source_level="asr",
            backend=self.name,
            source_notes=[
                "Qwen3-ASR transcribed a local audio file in VAD-bounded chunks.",
                "Each timestamp is the start of a chunk, not of a sentence.",
                "Raw audio file was referenced, not copied into the package.",
                *(["Native Qwen3-ASR batching may change individual words or punctuation through numerical differences; set OPENMD_QWEN3_BATCH_SIZE=1 for batch-1 reproducibility."] if self.batch_size > 1 else []),
            ],
            title=source_path.stem or "local audio",
            metadata={
                "profile": "media",
                "asr_backend": self.name,
                "model": model_label(self.model),
                "device": self.device,
                "language": self.language or (languages[0] if len(set(languages)) == 1 else "mixed"),
                "duration": duration,
                "segment_count": len(lines),
                **self._run_metadata,
            },
        )

    def _run(self, path: Path) -> tuple[list[str], list[str], float]:
        started = time.monotonic()
        model_path = require_model(self.model, family="qwen3")
        vad_path = require_model(self.vad_model, family="vad")
        import soundfile
        import torch
        from funasr import AutoModel
        from qwen_asr import Qwen3ASRModel

        with owned_temporary_directory(prefix="openmd-qwen3-") as tmp:
            wav = Path(tmp) / "audio.wav"
            run_interruptibly(
                ["ffmpeg", "-v", "error", "-y", "-i", str(path), "-ar", str(SAMPLE_RATE), "-ac", "1", str(wav)],
                check=True,
                capture_output=True,
            )
            audio, _ = soundfile.read(str(wav), dtype="float32")
            with offline_model_loading():
                vad = AutoModel(model=str(vad_path), device=self.device, disable_update=True)
            spans = merge_speech_spans(vad.generate(input=str(wav))[0]["value"], MAX_CHUNK_MS)
            del vad
        with offline_model_loading():
            model = Qwen3ASRModel.from_pretrained(
                str(model_path),
                dtype=torch.bfloat16,
                device_map=self.device,
                max_inference_batch_size=self.batch_size,
                max_new_tokens=512,
                local_files_only=True,
            )
        try:
            lines, languages = self._transcribe_spans(model, audio, spans, torch)
            return lines, languages, len(audio) / SAMPLE_RATE
        finally:
            del model
            gc.collect()
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
            self._run_metadata["elapsed_seconds"] = round(time.monotonic() - started, 6)

    def _transcribe_spans(
        self, model: Any, audio: Any, spans: list[list[int]], torch: Any,
    ) -> tuple[list[str], list[str]]:
        """Batch existing VAD chunks without changing their order or timestamps.

        Only a CUDA OOM retries, and only the slice not yet appended. Leaving the
        exception handler before cache cleanup releases the failed call's frames.
        """
        lines: list[str] = []
        languages: list[str] = []
        effective = self.batch_size
        histogram: dict[str, int] = {}
        oom_attempts: list[int] = []
        retries = 0
        inference_seconds = 0.0
        index = 0
        try:
            while index < len(spans):
                current = spans[index:index + effective]
                pieces = [audio[start * SAMPLE_RATE // 1000:end * SAMPLE_RATE // 1000] for start, end in current]
                # Preserve the original tuple call when inference is scalar.
                inputs = (pieces[0], SAMPLE_RATE) if effective == 1 else [(piece, SAMPLE_RATE) for piece in pieces]
                started = time.monotonic()
                retry = False
                try:
                    results = model.transcribe(audio=inputs, language=self.language)
                except torch.cuda.OutOfMemoryError:
                    oom_attempts.append(len(current))
                    if effective == 1:
                        raise
                    effective = max(1, effective // 2)
                    model.max_inference_batch_size = effective
                    retries += 1
                    retry = True
                finally:
                    inference_seconds += time.monotonic() - started
                if retry:
                    gc.collect()
                    torch.cuda.empty_cache()
                    continue
                if len(results) != len(current):
                    raise ValueError(f"qwen3_batch_cardinality_mismatch: expected {len(current)}, got {len(results)}")
                for (start, _), result in zip(current, results):
                    text = str(result.text).strip()
                    if text:
                        lines.append(f"[{format_timestamp(start)}] {text}")
                        languages.append(str(getattr(result, "language", "") or "unknown"))
                key = str(len(current))
                histogram[key] = histogram.get(key, 0) + 1
                index += len(current)
        finally:
            self._run_metadata.update({
                "batch_size_requested": self.batch_size,
                "batch_size_effective": effective,
                "batch_size_histogram": histogram,
                "batch_oom_retries": retries,
                "batch_oom_attempts": oom_attempts,
                "inference_seconds": round(inference_seconds, 6),
            })
        return lines, languages


def merge_speech_spans(spans: list[list[int]], limit_ms: int) -> list[list[int]]:
    """Join neighbouring VAD spans while the merged chunk stays within ``limit_ms``."""
    merged: list[list[int]] = []
    for start, end in spans:
        if merged and end - merged[-1][0] <= limit_ms:
            merged[-1][1] = end
        else:
            merged.append([start, end])
    return merged


def format_timestamp(milliseconds: int) -> str:
    seconds = milliseconds // 1000
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def _failed(backend: str, code: str, error: object) -> TranscriptResult:
    return TranscriptResult(
        text="",
        status="failed",
        source_level="partial_extraction",
        backend=backend,
        source_notes=["Qwen3-ASR did not produce usable text."],
        error_code=code,
        error_summary=sanitize_error_summary(error),
        metadata={"profile": "media", "asr_backend": backend},
    )


def create_backend() -> Qwen3ASRBackend:
    return Qwen3ASRBackend()
