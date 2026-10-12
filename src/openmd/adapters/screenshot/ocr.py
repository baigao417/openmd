"""Screenshot OCR backend abstractions."""

from __future__ import annotations

from abc import ABC, abstractmethod
import re

from openmd.core.models import TranscriptResult
from openmd.core.secrets import sanitize_error_summary


class OCRBackend(ABC):
    name: str

    @abstractmethod
    def recognize(self, source_ref: str, *, title: str) -> TranscriptResult:
        raise NotImplementedError


class MockOCRBackend(OCRBackend):
    name = "screenshot.mock"

    def recognize(self, source_ref: str, *, title: str) -> TranscriptResult:
        text = f"# {title}\n\nMock OCR text from a long screenshot for Openmd package smoke.\n"
        return TranscriptResult(
            text=text,
            status="verified",
            source_level="ocr",
            backend=self.name,
            source_notes=[
                "screenshot OCR mock backend only",
                "raw screenshot file was referenced, not copied into the package",
            ],
            title=title or "screenshot",
            metadata={
                "profile": classify_ocr_profile(text),
                "ocr_backend": self.name,
                "source_ref": source_ref,
            },
        )


class MissingOCRBackend(OCRBackend):
    def __init__(self, name: str) -> None:
        self.name = name

    def recognize(self, source_ref: str, *, title: str) -> TranscriptResult:
        return TranscriptResult(
            text="",
            status="failed",
            source_level="partial_extraction",
            backend=self.name,
            source_notes=[
                "OCR backend is not installed or not enabled",
                "no external OCR command was attempted",
            ],
            error_code="ocr_backend_missing",
            error_summary=sanitize_error_summary(
                f"OCR backend {self.name!r} is not installed or enabled."
            ),
            title=title or "screenshot",
            metadata={"profile": "document", "ocr_backend": self.name, "source_ref": source_ref},
        )


def classify_ocr_profile(text: str) -> str:
    """Return the likely V1 package profile for OCR text.

    The first mock/gated lane keeps package output document-first. This helper
    records the intended routing seam for future layout-aware OCR without
    changing package writer contracts in this lane.
    """

    if _looks_like_chat_log(text):
        return "conversation"
    return "document"


def _looks_like_chat_log(text: str) -> bool:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) < 2:
        return False
    chat_hint = re.compile(r"(^|\s)(\d{1,2}:\d{2}|上午|下午|AM|PM)(\s|$).{0,40}[:：]")
    return sum(1 for line in lines if chat_hint.search(line)) >= 2
