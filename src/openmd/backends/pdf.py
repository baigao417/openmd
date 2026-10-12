"""Runtime providers for PDF/Office inputs under the legacy PDF identity."""

from __future__ import annotations

from openmd.adapters.pdf.adapter import PdfDocumentAdapter
from openmd.adapters.pdf.backends.markitdown import MarkItDownPDFParser
from openmd.adapters.pdf.parser import MissingPDFParser, MockPDFParser
from openmd.backends.base import (
    BackendCapability,
    BackendDescriptor,
    BackendExtractRequest,
    BackendHealth,
)
from openmd.backends.executor import BackendProviderRegistry
from openmd.backends.registry import BackendRuntimeOptions
from openmd.core.models import TranscriptResult


class PDFParserProvider:
    def __init__(self, parser) -> None:
        self.parser = parser
        self.name = parser.name

    def descriptor(self) -> BackendDescriptor:
        source_level = "native_text" if isinstance(self.parser, (MockPDFParser, MarkItDownPDFParser)) else "partial_extraction"
        return BackendDescriptor(
            name=self.name,
            layer="local",
            capabilities=(
                BackendCapability(
                    platforms=("pdf",),
                    source_types=("document",),
                    input_types=("single", "batch", "collection", "generic"),
                    source_levels=(source_level,),
                    requires_network=False,
                    requires_credentials=False,
                ),
            ),
            description="Local PDF/DOCX/PPTX/XLSX document parser provider.",
            provider="openmd.pdf",
        )

    def health(self) -> BackendHealth:
        is_available = getattr(self.parser, "is_available", None)
        if callable(is_available) and not is_available():
            return BackendHealth(
                ok=False,
                error_code="document_backend_missing",
                error_summary=f"{self.name} is not installed or enabled.",
            )
        if isinstance(self.parser, MissingPDFParser):
            return BackendHealth(
                ok=False,
                error_code="document_backend_missing",
                error_summary=f"{self.name} is not installed or enabled.",
            )
        return BackendHealth(ok=True, dependencies={self.name: "ok"})

    def extract(self, request: BackendExtractRequest) -> TranscriptResult:
        adapter = PdfDocumentAdapter("pdf", request.primary_backend)
        return adapter.extract_text(
            request.item,
            backend=request.primary_backend,
            backend_chain=(request.primary_backend,),
            allow_fallback=False,
        )


def register_backend_providers(
    registry: BackendProviderRegistry,
    options: BackendRuntimeOptions,
) -> None:
    registry.register("pdf.mock", PDFParserProvider(MockPDFParser()))
    registry.register("pdf.markitdown", PDFParserProvider(MarkItDownPDFParser()))
    registry.register("pdf.docling", PDFParserProvider(MissingPDFParser("pdf.docling")))
    registry.register("paddleocr.pdf", PDFParserProvider(MissingPDFParser("paddleocr.pdf")))
