"""Routing tests for asr.auto; recognition quality comes from real audio runs."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from openmd.adapters.local_audio.backends.auto import AutoASRBackend
from openmd.adapters.local_audio.backends.faster_whisper import FasterWhisperASRBackend
from openmd.adapters.local_audio.backends.qwen3_asr import Qwen3ASRBackend
from openmd.backends.generic_media import LocalAudioASRProvider
from openmd.backends.registry import BackendRuntimeOptions, create_runtime_backend_provider_registry
from openmd.core.models import TranscriptResult


def _verified(backend: str) -> TranscriptResult:
    return TranscriptResult(text="[00:00:00] text\n", status="verified", source_level="asr", backend=backend)


class FakeQwen(Qwen3ASRBackend):
    def __init__(self, available: bool = True, result: TranscriptResult | None = None) -> None:
        super().__init__(model="fixture")
        self.available = available
        self.result = result or _verified("asr.qwen3")

    def is_available(self) -> bool:
        return self.available

    def transcribe(self, path: Path) -> TranscriptResult:
        return self.result


class AutoASRRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        env = patch.dict("os.environ", {"OPENMD_FASTER_WHISPER_DEVICE": "cuda"}, clear=False)
        env.start()
        self.addCleanup(env.stop)

    def test_mandarin_uses_qwen3_on_gpu(self) -> None:
        backend, reason = AutoASRBackend(qwen3=FakeQwen()).choose("zh")
        self.assertIsInstance(backend, Qwen3ASRBackend)
        self.assertIn("Qwen3", reason)

    def test_mandarin_without_qwen3_stack_uses_whisper(self) -> None:
        backend, reason = AutoASRBackend(qwen3=FakeQwen(available=False)).choose("zh")
        self.assertIsInstance(backend, FasterWhisperASRBackend)
        self.assertEqual((backend.model_size, backend.language), ("large-v3", "zh"))
        self.assertIn("unavailable", reason)

    def test_mandarin_on_cpu_uses_whisper(self) -> None:
        with patch.dict("os.environ", {"OPENMD_FASTER_WHISPER_DEVICE": "cpu"}):
            backend, _ = AutoASRBackend(qwen3=FakeQwen()).choose("zh")
        self.assertIsInstance(backend, FasterWhisperASRBackend)

    def test_english_uses_turbo_and_other_languages_use_large_v3(self) -> None:
        english, _ = AutoASRBackend().choose("en")
        japanese, _ = AutoASRBackend().choose("ja")
        self.assertEqual((english.model_size, english.language), ("large-v3-turbo", "en"))
        self.assertEqual((japanese.model_size, japanese.language), ("large-v3", "ja"))

    def test_models_follow_environment_overrides(self) -> None:
        with patch.dict("os.environ", {"OPENMD_ASR_AUTO_EN_MODEL": "local/turbo", "OPENMD_ASR_AUTO_ZH_ENGINE": "whisper"}):
            auto = AutoASRBackend(qwen3=FakeQwen())
            english, _ = auto.choose("en")
            mandarin, _ = auto.choose("zh")
        self.assertEqual(english.model_size, "local/turbo")
        self.assertIsInstance(mandarin, FasterWhisperASRBackend)

    def test_route_is_recorded_and_failed_qwen3_falls_back(self) -> None:
        failed = TranscriptResult(text="", status="failed", source_level="partial_extraction", backend="asr.qwen3")
        with tempfile.TemporaryDirectory() as tmp:
            audio = Path(tmp) / "a.wav"
            audio.write_bytes(b"audio")
            auto = AutoASRBackend(qwen3=FakeQwen(result=failed))
            with (
                patch.object(AutoASRBackend, "is_available", return_value=True),
                patch.object(AutoASRBackend, "detect_language", return_value=("zh", 0.99)),
                patch.object(FasterWhisperASRBackend, "transcribe", return_value=_verified("asr.faster_whisper")),
            ):
                result = auto.transcribe(audio)
        self.assertEqual(result.status, "verified")
        route = result.metadata["asr_route"]
        self.assertEqual((route["detected_language"], route["selected_backend"]), ("zh", "asr.faster_whisper"))
        self.assertIn("fell back", route["reason"])
        self.assertTrue(any(note.startswith("asr.auto routed") for note in result.source_notes))

    def test_local_media_under_asr_gate_defaults_to_auto(self) -> None:
        from argparse import Namespace

        from openmd.cli import _requested_backend

        self.assertEqual(_requested_backend("generic_media", Namespace(backend=None, enable_asr=True)), "asr.auto")
        self.assertIsNone(_requested_backend("generic_media", Namespace(backend=None, enable_asr=False)))
        self.assertIsNone(_requested_backend("bilibili", Namespace(backend=None, enable_asr=True)))
        self.assertEqual(_requested_backend("generic_media", Namespace(backend="asr.qwen3", enable_asr=True)), "asr.qwen3")

    def test_registered_for_generic_media(self) -> None:
        registry = create_runtime_backend_provider_registry("generic_media", BackendRuntimeOptions())
        self.assertIsInstance(registry.provider_for("asr.auto"), LocalAudioASRProvider)


if __name__ == "__main__":
    unittest.main()
