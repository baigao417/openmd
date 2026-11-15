"""Local audio ASR backend abstractions."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from openmd.core.models import TranscriptResult
from openmd.core.secrets import sanitize_error_summary


class ASRBackend(ABC):
    name: str

    @abstractmethod
    def transcribe(self, path: Path) -> TranscriptResult:
        raise NotImplementedError


class MockASRBackend(ASRBackend):
    name = "local_audio.mock"

    def transcribe(self, path: Path) -> TranscriptResult:
        return TranscriptResult(
            text="[00:00:00] Mock local audio transcript for Openmd package smoke.\n",
            status="verified",
            source_level="asr",
            backend=self.name,
            source_notes=[
                "local audio mock backend only",
                "raw audio file was referenced, not copied into the package",
            ],
            title=path.stem or "local audio",
            metadata={"profile": "media", "asr_backend": self.name},
        )


class MissingASRBackend(ASRBackend):
    def __init__(self, name: str) -> None:
        self.name = name

    def transcribe(self, path: Path) -> TranscriptResult:
        return TranscriptResult(
            text="",
            status="failed",
            source_level="partial_extraction",
            backend=self.name,
            source_notes=[
                "local audio ASR backend is not installed or not enabled",
                "no external ASR command was attempted",
            ],
            error_code="asr_backend_missing",
            error_summary=sanitize_error_summary(
                f"ASR backend {self.name!r} is not installed or enabled for {path.name}."
            ),
            title=path.stem or "local audio",
            metadata={"profile": "media", "asr_backend": self.name},
        )
