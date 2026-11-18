"""Runtime providers for PDF/Office inputs under the legacy PDF identity."""

from __future__ import annotations

from dataclasses import replace

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
    def __init__(self, parser, options: BackendRuntimeOptions | None = None) -> None:
        self.parser = parser
        self.name = parser.name
        self.options = options or BackendRuntimeOptions()

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
        from openmd.adapters.local_audio.adapter import local_path_from_input
        from openmd.ocr.documents import PDFDependencyMissing, document_failure, inspect_pdf

        source = request.item.raw_meta.get("local_path") or request.item.url
        path = local_path_from_input(source)
        probes = None
        if self.name == "pdf.markitdown" and path and path.is_file() and path.suffix.lower() == ".pdf":
            try:
                probes = inspect_pdf(path)
                if any(not page["reliable"] for page in probes):
                    return document_failure(self.name, request.item.title or path.stem, "pdf_ocr_required", "One or more pages require OCR; continue to the page router.")
            except PDFDependencyMissing as exc:
                # Native pdfminer can run in [document] without the optional
                # page/OCR dependencies. Existing OCR-capable installs keep
                # their established probe and fallback order.
                import importlib.util
                if importlib.util.find_spec('pdfminer') is None:
                    return document_failure(self.name, request.item.title or path.stem, "pymupdf_missing", str(exc))
            except (OSError, ValueError, RuntimeError):
                return document_failure(self.name, request.item.title or path.stem, "pdf_probe_failed", "PyMuPDF could not inspect the local PDF.")
        adapter = PdfDocumentAdapter("pdf", request.primary_backend)
        result = adapter.extract_text(
            request.item,
            backend=request.primary_backend,
            backend_chain=(request.primary_backend,),
            allow_fallback=False,
        )
        if probes is not None:
            result = replace(result, metadata={**result.metadata, "pages": [{**p, "status": "verified", "engine": self.name, "model": None, "digest": None, "fallback_used": False, "reason": None, "ovis_seconds": 0.0, "fallback_seconds": 0.0, "peak_vram_mb": None, "han_ratio": None, "slice_parameters": {}, "breakers": []} for p in probes]})
        if self.name == "pdf.markitdown" and path and path.is_file() and path.suffix.lower() in {".docx", ".pptx", ".xlsx"} and result.error_code not in {"document_invalid_container", "document_extraction_failed"}:
            from openmd.ocr.office import recover_office

            item = replace(request.item, raw_meta={**request.item.raw_meta, "local_path": str(path)})
            result = recover_office(item, result, allow_ocr=self.options.enable_network_backends, allow_external_resources=self.options.enable_network_backends)
        return result


class PDFTextLayerProvider:
    name = "pdf.textlayer"

    def descriptor(self):
        return BackendDescriptor(name=self.name, layer="local", provider="openmd.pdf", description="PyMuPDF native page text; incomplete PDFs continue to OCR.", capabilities=(BackendCapability(platforms=("pdf",), source_types=("document",), input_types=("single", "batch", "collection", "generic"), source_levels=("native_text",), requires_network=False),))

    def health(self):
        import importlib.util

        available = importlib.util.find_spec("pymupdf") is not None
        return BackendHealth(ok=available, error_code=None if available else "pymupdf_missing")

    def extract(self, request):
        from openmd.ocr.documents import extract_pdf

        return extract_pdf(request.item, backend=self.name)


class DocumentEngineProvider:
    """Additional local formats, after the unchanged legacy PDF/OCR chain."""

    name = "document.engines"

    def descriptor(self):
        return BackendDescriptor(name=self.name, layer="local", provider="openmd.documents",
            description="Detected-only local document engines and bounded ZIP containers.",
            capabilities=(BackendCapability(platforms=("pdf",), source_types=("document",),
                input_types=("single", "batch", "collection", "generic"),
                source_levels=("native_text", "partial_extraction"), requires_network=False),))

    def health(self):
        return BackendHealth(ok=True, warnings=("Per-format dependencies are checked at conversion.",))

    def extract(self, request):
        from openmd.adapters.local_audio.adapter import local_path_from_input
        from openmd.engines.base import failure
        from openmd.engines.registry import convert_document
        from openmd.engines.sniff import LEGACY_FORMATS
        from pathlib import Path

        source = request.item.raw_meta.get("local_path") or request.item.url
        path = local_path_from_input(source)
        if path is None:
            return failure(self.name, Path("document"), "document_local_file_required")
        from openmd.engines.native import PROMOTED
        if path.suffix.lower().lstrip(".") in LEGACY_FORMATS - PROMOTED:
            # This is an additional-format provider, not a second PDF/OCR pass.
            # An unavailable fallback preserves the previous source verdict.
            return failure(self.name, path, "backend_not_implemented", "Legacy PDF/Office uses the preceding providers.")
        return convert_document(path)


def register_backend_providers(
    registry: BackendProviderRegistry,
    options: BackendRuntimeOptions,
) -> None:
    registry.register("pdf.mock", PDFParserProvider(MockPDFParser()))
    registry.register("pdf.markitdown", PDFParserProvider(MarkItDownPDFParser(), options))
    registry.register("pdf.textlayer", PDFTextLayerProvider())
    from openmd.backends.ocr import register_ocr_providers

    register_ocr_providers(registry, options)
    registry.register("document.engines", DocumentEngineProvider())
