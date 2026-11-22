"""Pick the local ASR model from the spoken language.

Routing follows the 2026-11-07 benchmark (quality 80%, speed 20%), recorded in
``docs/benchmarks/asr/``:

- Mandarin: Qwen3-ASR (lowest CER, fewest dropped characters) when its GPU stack is
  installed; otherwise Whisper large-v3.
- English: Whisper large-v3-turbo (best weighted score, 3.5x faster than large-v3).
- Anything else: Whisper large-v3.

The language is detected once with the English model, which is also the fastest one.
Every choice can be overridden by environment variables listed in ``execution_config``.
"""

from __future__ import annotations

import gc
import importlib.util
import os
from dataclasses import replace
from pathlib import Path
from typing import Any

from openmd.adapters.local_audio.asr import ASRBackend, MissingASRBackend
from openmd.adapters.local_audio.backends.faster_whisper import (
    FasterWhisperASRBackend,
    _prepare_windows_dll_paths,
)
from openmd.adapters.local_audio.backends.qwen3_asr import Qwen3ASRBackend
from openmd.core.models import TranscriptResult
from openmd.runtime.models import ModelNotFoundError, _runnable_whisper_paths, configured_models, model_label, offline_model_loading, require_model, resolve_model
from openmd.runtime.asr_memory import InsufficientASRMemory, check_memory, memory_warning_notes

DEFAULT_ENGLISH_MODEL = "large-v3-turbo"
DEFAULT_GENERAL_MODEL = "large-v3"


class AutoASRBackend(ASRBackend):
    name = "asr.auto"

    def __init__(self, *, qwen3: Qwen3ASRBackend | None = None) -> None:
        self.english_model = os.getenv("OPENMD_ASR_AUTO_EN_MODEL") or DEFAULT_ENGLISH_MODEL
        self.general_model = os.getenv("OPENMD_FASTER_WHISPER_MODEL") or DEFAULT_GENERAL_MODEL
        self.chinese_engine = os.getenv("OPENMD_ASR_AUTO_ZH_ENGINE") or "qwen3"
        self._qwen3 = qwen3
        self._probe_memory: dict[str, Any] = {}
        self._probe_model: Path | None = None
        self._selected_model: str | None = None

    def execution_config(self) -> dict[str, Any]:
        return {
            "english_model": model_label(self.english_model),
            "english_model_origin": resolve_model(self.english_model, family="whisper").origin,
            "general_model": model_label(self.general_model),
            "general_model_origin": resolve_model(self.general_model, family="whisper").origin,
            "chinese_engine": self.chinese_engine,
            "qwen3": (self._qwen3 or Qwen3ASRBackend()).execution_config(),
            "device": os.getenv("OPENMD_FASTER_WHISPER_DEVICE") or "cuda_if_available",
            "local_files_only": True,
        }

    def is_available(self) -> bool:
        return importlib.util.find_spec("faster_whisper") is not None

    def transcribe(self, path: Path) -> TranscriptResult:
        self._probe_memory = {}
        source_path = path.expanduser()
        if not source_path.is_file() or not self.is_available():
            return MissingASRBackend(self.name).transcribe(source_path)
        try:
            language, probability = self.detect_language(source_path)
        except InsufficientASRMemory as exc:
            from openmd.runtime.asr import failed_result

            result = failed_result(self.name, "asr_insufficient_memory", exc)
            result.metadata.update(exc.metrics)
            result.metadata["language_probe_memory"] = exc.metrics
            return result
        except ModelNotFoundError as exc:
            from openmd.runtime.asr import failed_result

            return failed_result(self.name, "asr_model_missing", exc)
        except Exception as exc:  # pragma: no cover - dependency/model safety net
            selected = self._select_whisper(self.general_model)
            result = FasterWhisperASRBackend(model_size=selected, **self._whisper_device()).transcribe(source_path)
            return self._annotate(result, None, None, f"language detection failed ({type(exc).__name__}: {exc}); used Whisper {model_label(selected)}")

        backend, reason = self.choose(language)
        self._selected_model = getattr(backend, "model_size", None)
        result = backend.transcribe(source_path)
        if result.status != "verified" and result.error_code != "asr_insufficient_memory" and isinstance(backend, Qwen3ASRBackend):
            selected = self._select_whisper(self.general_model)
            fallback = FasterWhisperASRBackend(model_size=selected, language="zh", **self._whisper_device())
            result = fallback.transcribe(source_path)
            reason += f"; Qwen3-ASR failed, fell back to Whisper {model_label(selected)}"
        return self._annotate(result, language, probability, reason)

    def _select_whisper(self, preferred: str) -> str:
        """The preferred Whisper model, or the best one actually installed.

        ``asr:baseline`` installs only large-v3-turbo, so every route that would
        name large-v3 must downgrade instead of failing with asr_model_missing.
        """
        if resolve_model(preferred, family="whisper").path is not None:
            return preferred
        models = configured_models()
        for key in ("whisper-large-v3-turbo", "whisper-large-v3"):
            if models[key].path is not None:
                return str(models[key].path)
        unknown = [path for path, _, evaluated in _runnable_whisper_paths() if not evaluated]
        if unknown:
            return str(unknown[0])
        return preferred

    def choose(self, language: str | None) -> tuple[ASRBackend, str]:
        device = self._whisper_device()
        select_whisper = self._select_whisper

        if language == "zh":
            qwen3 = self._qwen3 or Qwen3ASRBackend()
            if self.chinese_engine == "qwen3" and qwen3.is_available() and self._device()[0] == "cuda":
                return qwen3, "Mandarin: Qwen3-ASR scored the lowest CER"
            reason = "Mandarin: Qwen3-ASR needs its GPU stack, which is unavailable" if self.chinese_engine == "qwen3" else "Mandarin: Whisper requested"
            selected = select_whisper(self.general_model)
            reason += self._evaluation_note(selected)
            return FasterWhisperASRBackend(model_size=selected, language="zh", **device), reason + (f"; downgraded to available {model_label(selected)}" if selected != self.general_model else "")
        if language == "en":
            selected = select_whisper(self.english_model)
            return (
                FasterWhisperASRBackend(model_size=selected, language="en", **device),
                f"English: Whisper {model_label(selected)} selected" + self._evaluation_note(selected),
            )
        selected = select_whisper(self.general_model)
        return (
            FasterWhisperASRBackend(model_size=selected, language=language, **device),
            f"Language {language or 'unknown'}: Whisper {model_label(selected)} selected" + self._evaluation_note(selected),
        )

    def detect_language(self, path: Path) -> tuple[str | None, float | None]:
        probe_model = self.english_model if resolve_model(self.english_model, family="whisper").path else self.general_model
        if resolve_model(probe_model, family="whisper").path is None:
            unknown = [path for path, _, evaluated in _runnable_whisper_paths() if not evaluated]
            if unknown:
                probe_model = str(unknown[0])
        model_path = require_model(probe_model, family="whisper")
        self._probe_model = model_path
        largest = max((p.stat().st_size for p in model_path.glob("model.bin")), default=0)
        self._probe_memory = check_memory("language_probe", largest_shard_bytes=largest)
        _load_torch_first()
        _prepare_windows_dll_paths()
        from faster_whisper import WhisperModel

        device, compute_type = self._device()
        with offline_model_loading():
            model = WhisperModel(str(model_path), device=device, compute_type=compute_type, local_files_only=True)
        try:
            _, info = model.transcribe(str(path), vad_filter=True, beam_size=1)
            return getattr(info, "language", None), getattr(info, "language_probability", None)
        finally:
            del model
            gc.collect()
            _free_torch_cache()

    def _device(self) -> tuple[str, str]:
        device = os.getenv("OPENMD_FASTER_WHISPER_DEVICE")
        compute_type = os.getenv("OPENMD_FASTER_WHISPER_COMPUTE_TYPE")
        if device is None:
            device = "cuda" if _cuda_available() else "cpu"
        if compute_type is None:
            compute_type = "int8_float16" if device == "cuda" else "int8"
        return device, compute_type

    def _whisper_device(self) -> dict[str, str]:
        device, compute_type = self._device()
        return {"device": device, "compute_type": compute_type}

    def _annotate(self, result: TranscriptResult, language: str | None, probability: float | None, reason: str) -> TranscriptResult:
        route = {
            "detected_language": language,
            "language_probability": round(probability, 3) if probability is not None else None,
            "selected_backend": result.backend,
            "reason": reason,
            "selected_model": model_label(self._selected_model) if self._selected_model else None,
            "selected_model_evaluation": self._evaluation(self._selected_model) if self._selected_model else None,
            "language_probe_model": model_label(str(self._probe_model)) if self._probe_model else None,
            "language_probe_model_evaluation": self._evaluation(str(self._probe_model)) if self._probe_model else None,
        }
        return replace(
            result,
            source_notes=[*result.source_notes, *["Language probe: " + note for note in memory_warning_notes(self._probe_memory)],
                          f"asr.auto routed this audio: {reason}."],
            metadata={**result.metadata, "asr_route": route, "language_probe_memory": self._probe_memory},
        )

    @staticmethod
    def _evaluation(value: str | None) -> str | None:
        if not value:
            return None
        resolved = resolve_model(value, family="whisper").path
        if resolved is None:
            return "unavailable"
        target = resolved.resolve()
        for path, _, evaluated in _runnable_whisper_paths():
            if path.resolve() == target:
                return "evaluated" if evaluated else "unverified"
        return "unverified"

    @classmethod
    def _evaluation_note(cls, value: str) -> str:
        return "; 未经 Openmd 评测" if cls._evaluation(value) == "unverified" else ""


def _load_torch_first() -> None:
    """Load torch's cuDNN before CTranslate2 when both share a process.

    If CTranslate2 loads cuDNN first, Qwen3 on torch later crashes the process with
    "Could not load symbol cudnnGetLibConfig".
    """
    if importlib.util.find_spec("torch") is not None:
        import torch  # noqa: F401


def _cuda_available() -> bool:
    try:
        _load_torch_first()
        _prepare_windows_dll_paths()
        import ctranslate2
        import torch

        return torch.cuda.is_available() and ctranslate2.get_cuda_device_count() > 0
    except Exception:
        return False


def _free_torch_cache() -> None:
    if importlib.util.find_spec("torch") is None:
        return
    import torch

    if torch.cuda.is_available():
        torch.cuda.empty_cache()


def create_backend() -> AutoASRBackend:
    return AutoASRBackend()
