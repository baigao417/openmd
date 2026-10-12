"""PDF parser backend abstractions."""

from __future__ import annotations

from abc import ABC, abstractmethod

from openmd.core.models import TranscriptResult
from openmd.core.secrets import sanitize_error_summary


class PDFParser(ABC):
    name: str

    @abstractmethod
    def parse(self, source_ref: str, *, title: str) -> TranscriptResult:
        raise NotImplementedError


class MockPDFParser(PDFParser):
    name = "pdf.mock"

    def parse(self, source_ref: str, *, title: str) -> TranscriptResult:
        return TranscriptResult(
            text=f"# {title}\n\nPage 1: Mock PDF document text for Openmd package smoke.\n",
            status="verified",
            source_level="native_text",
            backend=self.name,
            source_notes=[
                "PDF mock backend only",
                "raw PDF file was referenced, not copied into the package",
            ],
            title=title or "PDF document",
            metadata={"profile": "document", "parser_backend": self.name, "source_ref": source_ref},
        )

class MissingPDFParser(PDFParser):
    def __init__(self, name: str) -> None:
        self.name = name

    def parse(self, source_ref: str, *, title: str) -> TranscriptResult:
        return TranscriptResult(
            text="",
            status="failed",
            source_level="partial_extraction",
            backend=self.name,
            source_notes=[
                "PDF parser backend is not installed or not enabled",
                "no external PDF parser command was attempted",
            ],
            error_code="document_backend_missing",
            error_summary=sanitize_error_summary(
                f"PDF parser backend {self.name!r} is not installed or enabled."
            ),
            title=title or "PDF document",
            metadata={"profile": "document", "parser_backend": self.name, "source_ref": source_ref},
        )
