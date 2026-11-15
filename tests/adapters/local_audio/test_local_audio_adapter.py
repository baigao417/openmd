from __future__ import annotations

import contextlib
import io
import importlib.machinery
import json
import os
import sys
import tempfile
import types
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from openmd.adapters.local_audio.adapter import LocalAudioAdapter
from openmd.adapters.local_audio.backends.faster_whisper import FasterWhisperASRBackend, _normalize_chinese_script
from openmd.adapters.registry import create_default_registry
from openmd.backends.generic_media import LocalAudioASRProvider
from openmd.backends.registry import BackendRuntimeOptions, create_runtime_backend_provider_registry
from openmd.cli import main as cli_main
from openmd.core.package_validator import validate_package


class LocalAudioAdapterTests(unittest.TestCase):
    def test_resolves_local_audio_file_without_copying_raw_media(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            audio_path = Path(tmp_dir) / "sample.wav"
            _write_silence_wav(audio_path)

            adapter = LocalAudioAdapter("generic_media", "local_audio.mock")
            item = adapter.resolve_items(audio_path.as_uri())[0]

            self.assertEqual(item.platform, "generic_media")
            self.assertEqual(item.source_type, "audio")
            self.assertEqual(item.raw_meta["permission_scope"], "local_file")
            self.assertEqual(item.raw_meta["local_path"], str(audio_path))
            self.assertEqual(len(item.id or ""), 12)

    def test_mock_backend_creates_valid_media_package_from_file_uri(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            audio_path = root / "sample.wav"
            out_dir = root / "out"
            _write_silence_wav(audio_path)

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = cli_main(
                    [
                        "ingest",
                        audio_path.as_uri(),
                        "--out",
                        str(out_dir),
                        "--backend",
                        "local_audio.mock",
                        "--package",
                    ]
                )

            payload = json.loads(stdout.getvalue())
            package_path = Path(payload["outputs"][0])

            self.assertEqual(exit_code, 0)
            self.assertTrue(payload["ok"])
            self.assertTrue(validate_package(package_path).ok)
            self.assertTrue((package_path / "transcript.md").exists())
            self.assertEqual(list((package_path / "assets").iterdir()), [])
            combined = "\n".join(
                path.read_text(encoding="utf-8")
                for path in package_path.rglob("*")
                if path.is_file() and path.suffix in {".md", ".json"}
            )
            self.assertIn("Mock local audio transcript", combined)
            self.assertIn("local_file", combined)

    def test_default_asr_fallback_fails_structurally_without_real_asr(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            audio_path = root / "sample.wav"
            out_dir = root / "out"
            _write_silence_wav(audio_path)

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = cli_main(
                    [
                        "ingest",
                        audio_path.as_uri(),
                        "--out",
                        str(out_dir),
                        "--backend",
                        "asr.fallback",
                        "--package",
                    ]
                )

            payload = json.loads(stdout.getvalue())
            runlog_rows = [
                json.loads(line)
                for line in (out_dir / "runlog.jsonl").read_text(encoding="utf-8").splitlines()
            ]

            self.assertEqual(exit_code, 1)
            self.assertFalse(payload["ok"])
            self.assertEqual(runlog_rows[-1]["error_code"], "asr_backend_missing")
            self.assertEqual(list(out_dir.rglob("*.openmd")), [])

    def test_faster_whisper_backend_transcribes_local_file_when_optional_dependency_is_available(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            audio_path = Path(tmp_dir) / "speech.wav"
            _write_silence_wav(audio_path)

            fake_module = types.ModuleType("faster_whisper")
            fake_module.__spec__ = importlib.machinery.ModuleSpec("faster_whisper", loader=None)

            class FakeSegment:
                def __init__(self, start: float, end: float, text: str) -> None:
                    self.start = start
                    self.end = end
                    self.text = text

            class FakeInfo:
                language = "zh"
                duration = 2.0

            class FakeWhisperModel:
                def __init__(self, model_size: str, *, device: str, compute_type: str, local_files_only: bool) -> None:
                    assert local_files_only is True
                    self.model_size = model_size
                    self.device = device
                    self.compute_type = compute_type

                def transcribe(self, source: str, **kwargs):
                    self.source = source
                    self.kwargs = kwargs
                    return (
                        [
                            FakeSegment(0.0, 1.2, "Openmd local audio"),
                            FakeSegment(1.2, 2.0, "开放文档本地音频"),
                        ],
                        FakeInfo(),
                    )

            fake_module.WhisperModel = FakeWhisperModel

            with (
                patch.dict(sys.modules, {"faster_whisper": fake_module}),
                patch.dict(os.environ, {}, clear=True),
                patch("openmd.adapters.local_audio.backends.faster_whisper.require_model", return_value=audio_path.parent),
            ):
                result = FasterWhisperASRBackend(
                    device="cpu",
                    compute_type="int8",
                    language="zh",
                ).transcribe(audio_path)

        self.assertTrue(result.has_usable_text)
        self.assertEqual(result.status, "verified")
        self.assertEqual(result.source_level, "asr")
        self.assertEqual(result.backend, "asr.faster_whisper")
        self.assertIn("[00:00:00-00:00:01] Openmd local audio", result.text)
        self.assertIn("开放文档本地音频", result.text)
        self.assertEqual(result.metadata["asr_backend"], "asr.faster_whisper")
        self.assertEqual(result.metadata["model_size"], "large-v3")
        self.assertEqual(result.metadata["language"], "zh")
        self.assertEqual(result.metadata["segment_count"], 2)
        self.assertEqual(result.metadata["beam_size"], 5)
        self.assertEqual(result.metadata["quality_profile"], "general_v1")

    def test_faster_whisper_backend_missing_dependency_fails_safely(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            audio_path = Path(tmp_dir) / "speech.wav"
            _write_silence_wav(audio_path)

            with patch("importlib.util.find_spec", return_value=None):
                result = FasterWhisperASRBackend().transcribe(audio_path)

        self.assertFalse(result.has_usable_text)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.error_code, "asr_backend_missing")
        self.assertEqual(result.backend, "asr.faster_whisper")

    def test_generic_media_keeps_asr_behind_explicit_backend_gate(self) -> None:
        registry = create_default_registry()
        safe = create_runtime_backend_provider_registry("generic_media", BackendRuntimeOptions())
        provider = safe.provider_for("asr.faster_whisper")
        health = provider.health()

        self.assertEqual(registry.backend_chain("generic_media"), ("asr.fallback",))
        self.assertIsInstance(provider, LocalAudioASRProvider)
        if health.ok:
            self.assertEqual(health.dependencies["asr.faster_whisper"], "ok")
        else:
            self.assertEqual(health.error_code, "asr_backend_missing")


class ChineseScriptTests(unittest.TestCase):
    def _fake_opencc(self):
        module = types.ModuleType("opencc")

        class OpenCC:
            def __init__(self, config: str) -> None:
                self.config = config

            def convert(self, text: str) -> str:
                return text.replace("這", "这")

        module.OpenCC = OpenCC
        return module

    def test_mandarin_is_converted_and_noted(self) -> None:
        with patch.dict(sys.modules, {"opencc": self._fake_opencc()}):
            text, note, script = _normalize_chinese_script("這是", "zh", "simplified")
        self.assertEqual((text, script), ("这是", "simplified"))
        self.assertIn("OpenCC", note)

    def test_other_languages_and_opt_out_are_untouched(self) -> None:
        with patch.dict(sys.modules, {"opencc": self._fake_opencc()}):
            for language, script in (("en", "simplified"), ("yue", "simplified"), ("zh", "original")):
                self.assertEqual(_normalize_chinese_script("這是", language, script), ("這是", None, None))

    def test_missing_opencc_keeps_text_and_says_so(self) -> None:
        with patch.dict(sys.modules, {"opencc": None}):
            text, note, script = _normalize_chinese_script("這是", "zh", "simplified")
        self.assertEqual((text, script), ("這是", "original"))
        self.assertIn("not installed", note)


def _write_silence_wav(path: Path) -> None:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(8000)
        handle.writeframes(b"\x00\x00" * 800)
