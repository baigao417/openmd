"""Regression tests for the Qwen3 local ASR backend; real quality comes from real audio runs."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from openmd.adapters.local_audio.backends.qwen3_asr import (
    MAX_CHUNK_MS,
    Qwen3ASRBackend,
    format_timestamp,
    merge_speech_spans,
)
from openmd.backends.generic_media import LocalAudioASRProvider
from openmd.backends.registry import (
    BackendRuntimeOptions,
    create_runtime_backend_provider_registry,
)


class Qwen3ASRBackendTests(unittest.TestCase):
    def test_merge_keeps_every_chunk_within_the_limit(self) -> None:
        spans = [[0, 10_000], [11_000, 20_000], [21_000, 30_000], [31_000, 32_000]]
        self.assertEqual(merge_speech_spans(spans, 25_000), [[0, 20_000], [21_000, 32_000]])

    def test_single_long_span_is_kept_whole(self) -> None:
        self.assertEqual(merge_speech_spans([[0, MAX_CHUNK_MS * 2]], MAX_CHUNK_MS), [[0, MAX_CHUNK_MS * 2]])

    def test_timestamp_format(self) -> None:
        self.assertEqual(format_timestamp(8_616_120), "02:23:36")

    def test_transcribe_reports_timed_lines_and_detected_language(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            audio = Path(tmp) / "speech.wav"
            audio.write_bytes(b"audio")
            backend = Qwen3ASRBackend(model="fixture", device="cpu")
            lines = ["[00:00:00] hello", "[00:00:30] world"]
            with (
                patch.object(Qwen3ASRBackend, "is_available", return_value=True),
                patch.object(Qwen3ASRBackend, "_run", return_value=(lines, ["English", "English"], 42.0)),
            ):
                result = backend.transcribe(audio)

        self.assertEqual(result.status, "verified")
        self.assertEqual(result.source_level, "asr")
        self.assertEqual(result.text, "[00:00:00] hello\n[00:00:30] world\n")
        self.assertEqual(result.metadata["language"], "English")
        self.assertEqual(result.metadata["segment_count"], 2)

    def test_empty_recognition_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            audio = Path(tmp) / "speech.wav"
            audio.write_bytes(b"audio")
            with (
                patch.object(Qwen3ASRBackend, "is_available", return_value=True),
                patch.object(Qwen3ASRBackend, "_run", return_value=([], [], 1.0)),
            ):
                result = Qwen3ASRBackend().transcribe(audio)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.error_code, "asr_empty_text")

    def test_missing_dependency_fails_safely(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            audio = Path(tmp) / "speech.wav"
            audio.write_bytes(b"audio")
            with patch("importlib.util.find_spec", return_value=None):
                result = Qwen3ASRBackend().transcribe(audio)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.error_code, "asr_backend_missing")

    def test_backend_is_registered_for_generic_media(self) -> None:
        registry = create_runtime_backend_provider_registry("generic_media", BackendRuntimeOptions())
        self.assertIsInstance(registry.provider_for("asr.qwen3"), LocalAudioASRProvider)


if __name__ == "__main__":
    unittest.main()
