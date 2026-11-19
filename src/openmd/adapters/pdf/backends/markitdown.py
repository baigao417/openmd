"""Optional local MarkItDown PDF/Office backend (legacy PDF backend name).

The dependency is imported lazily so normal Openmd imports, mock lanes, and
safe skeleton discovery never execute the external parser. The backend only
parses local files or ``file://`` references; remote PDF fetching is deliberately
out of scope for this local document lane.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from typing import Any
from zipfile import BadZipFile, ZipFile

from openmd.adapters.local_audio.adapter import local_path_from_input
from openmd.adapters.pdf.parser import MissingPDFParser, PDFParser
from openmd.core.models import TranscriptResult
from openmd.core.secrets import sanitize_error_summary


class MarkItDownPDFParser(PDFParser):
    name = "pdf.markitdown"

    def is_available(self) -> bool:
        return importlib.util.find_spec("markitdown") is not None or importlib.util.find_spec("pdfminer") is not None

    def parse(self, source_ref: str, *, title: str) -> TranscriptResult:
        from openmd.engines.markitdown import MarkItDownEngine

        path = _local_existing_path(source_ref)
        if path is not None and path.suffix.lower() == '.pdf' and importlib.util.find_spec('markitdown') is None:
            from openmd.engines.native import NativeEngine
            return NativeEngine('pdf').convert(path)
        if path is None or path.suffix.lower() not in {".pdf", ".docx", ".pptx", ".xlsx"}:
            return self._parse_legacy(source_ref, title=title)
        return MarkItDownEngine().convert(path, _legacy_parser=self, title=title)

    def _parse_legacy(self, source_ref: str, *, title: str) -> TranscriptResult:
        """Compatibility implementation owned by the document engine facade."""
        source_path = _local_existing_path(source_ref)
        if source_path is None:
            return _failed_result(
                self.name,
                source_ref,
                title,
                "document_local_file_required",
                "MarkItDown document parsing requires a local file path or file:// reference.",
            )

        # Detector additions must never enable MarkItDown media/ZIP fallbacks.
        if source_path.suffix.lower() not in {".pdf", ".docx", ".pptx", ".xlsx"}:
            return _failed_result(
                self.name, str(source_path), title, "document_format_unsupported",
                "Supported local document formats are PDF, DOCX, PPTX and XLSX.",
            )

        source_format = source_path.suffix.lower().lstrip(".")
        if source_format == "pdf":
            with source_path.open("rb") as source:
                if b"%PDF-" not in source.read(1024):
                    return _failed_result(self.name, str(source_path), title, "document_invalid_content", "The input does not have a PDF signature.")
        office_parts = {"docx": "word/document.xml", "pptx": "ppt/presentation.xml", "xlsx": "xl/workbook.xml"}
        if source_format in office_parts:
            try:
                with ZipFile(source_path) as archive:
                    if not {"[Content_Types].xml", office_parts[source_format]}.issubset(archive.namelist()):
                        raise BadZipFile("required Office parts are missing")
            except (BadZipFile, OSError):
                return _failed_result(
                    self.name, str(source_path), title, "document_invalid_container",
                    "The Office input is not a valid OOXML document container.",
                )

        if not self.is_available():
            return MissingPDFParser(self.name).parse(str(source_path), title=title)

        try:
            from markitdown import MarkItDown

            if source_format in office_parts:
                from markitdown.converters import DocxConverter, PptxConverter, XlsxConverter

                # Do not let a broken Office file fall back to plain text or a
                # ZIP file listing and become a falsely verified document.
                converter = {"docx": DocxConverter, "pptx": PptxConverter, "xlsx": XlsxConverter}[source_format]
                parser = MarkItDown(enable_builtins=False, enable_plugins=False)
                parser.register_converter(converter())
            else:
                from markitdown.converters import PdfConverter

                parser = MarkItDown(enable_builtins=False, enable_plugins=False)
                parser.register_converter(PdfConverter())
            converted: Any = parser.convert(str(source_path))
            text = (getattr(converted, "text_content", None) or "").strip()
        except Exception as exc:  # pragma: no cover - dependency-specific safety net
            return _failed_result(
                self.name,
                str(source_path),
                title,
                "document_extraction_failed",
                exc,
            )

        if not _has_document_text(text, source_format):
            return _failed_result(
                self.name,
                str(source_path),
                title,
                "document_empty_text",
                "MarkItDown returned no text for the document.",
            )

        document_title = title or source_path.stem or "PDF document"
        return TranscriptResult(
            text=_with_title(document_title, text),
            status="verified",
            source_level="native_text",
            backend=self.name,
            source_notes=[
                f"MarkItDown parsed local {source_format.upper()} text.",
                "Raw document file was referenced, not copied into the package.",
                "Native text extraction does not certify reading order, tables, formulas or image OCR quality.",
            ],
            title=document_title,
            metadata={
                "profile": "document",
                "parser_backend": self.name,
                "source_ref": str(source_path),
                "page_mapping": "not_available",
                "source_format": source_format,
            },
        )


def _has_document_text(text: str, source_format: str) -> bool:
    """Ignore converter scaffolding without removing actual source headings."""
    if source_format not in {"pptx", "xlsx"}:
        return bool(text.strip())
    visible = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    if source_format == "xlsx":
        # Sheet headings and pandas' unnamed-column labels are generated even
        # for empty workbooks, so they cannot establish source body coverage.
        visible = re.sub(r"^## .*?$", "", visible, flags=re.MULTILINE)
        visible = re.sub(r"Unnamed: \d+(?:_level_\d+)?", "", visible)
        visible = re.sub(r"[|\s:\-]", "", visible)
    return bool(visible.strip())


def _local_existing_path(source_ref: str) -> Path | None:
    path = local_path_from_input(source_ref)
    if path is None:
        return None
    return path if path.is_file() else None


def _with_title(title: str, text: str) -> str:
    if text.lstrip().startswith("#"):
        return text if text.endswith("\n") else f"{text}\n"
    return f"# {title}\n\n{text}\n"


def _failed_result(
    backend: str,
    source_ref: str,
    title: str,
    code: str,
    error: object,
) -> TranscriptResult:
    return TranscriptResult(
        text="",
        status="failed",
        source_level="partial_extraction",
        backend=backend,
        source_notes=[
            "Document parser backend did not produce usable text.",
            "Raw document file was referenced, not copied into the package.",
        ],
        error_code=code,
        error_summary=sanitize_error_summary(error),
        title=title or "PDF document",
        metadata={"profile": "document", "parser_backend": backend, "source_ref": source_ref},
    )
