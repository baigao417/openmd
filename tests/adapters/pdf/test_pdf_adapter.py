from __future__ import annotations

import contextlib
import io
import importlib.machinery
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from openmd.adapters.pdf.adapter import PdfDocumentAdapter
from openmd.adapters.pdf.backends.markitdown import MarkItDownPDFParser
from openmd.adapters.registry import create_default_registry
from openmd.backends.base import DiscoveryRequest
from openmd.backends.executor import SkeletonDiscoveryProvider
from openmd.backends.pdf import PDFParserProvider
from openmd.backends.registry import BackendRuntimeOptions, create_runtime_backend_provider_registry, create_runtime_discovery_provider_registry
from openmd.cli import main as cli_main
from openmd.core.detector import detect_platform
from openmd.core.package_validator import validate_package


class PdfDocumentAdapterTests(unittest.TestCase):
    def test_detector_and_registry_load_pdf_adapter_without_registry_edits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            pdf_path = Path(tmp_dir) / "sample.pdf"
            _write_fake_pdf(pdf_path)

            registry = create_default_registry()
            adapter = registry.adapter_for(str(pdf_path))

            self.assertEqual(detect_platform(str(pdf_path)), "pdf")
            self.assertIsInstance(adapter, PdfDocumentAdapter)
            self.assertEqual(adapter.id, "pdf")
            self.assertEqual(
                registry.backend_chain("pdf"),
                ("pdf.markitdown", "pdf.docling", "paddleocr.pdf"),
            )

    def test_resolves_local_pdf_file_without_copying_raw_document(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            pdf_path = Path(tmp_dir) / "sample.pdf"
            _write_fake_pdf(pdf_path)

            adapter = PdfDocumentAdapter("pdf", "pdf.mock")
            item = adapter.resolve_items(str(pdf_path))[0]

            self.assertEqual(item.platform, "pdf")
            self.assertEqual(item.source_type, "document")
            self.assertEqual(item.raw_meta["permission_scope"], "local_file")
            self.assertEqual(item.raw_meta["local_path"], str(pdf_path))
            self.assertEqual(len(item.id or ""), 12)

    def test_mock_backend_creates_valid_document_package_from_pdf(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            pdf_path = root / "sample.pdf"
            out_dir = root / "out"
            _write_fake_pdf(pdf_path)

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = cli_main(
                    [
                        "ingest",
                        str(pdf_path),
                        "--out",
                        str(out_dir),
                        "--backend",
                        "pdf.mock",
                        "--package",
                    ]
                )

            payload = json.loads(stdout.getvalue())
            package_path = Path(payload["outputs"][0])
            metadata = json.loads((package_path / "metadata.json").read_text(encoding="utf-8"))
            combined = "\n".join(
                path.read_text(encoding="utf-8")
                for path in package_path.rglob("*")
                if path.is_file() and path.suffix in {".md", ".json"}
            )

            self.assertEqual(exit_code, 0)
            self.assertTrue(payload["ok"])
            self.assertTrue(validate_package(package_path).ok)
            self.assertEqual(metadata["profile"], "document")
            self.assertEqual(metadata["source_level"], "native_text")
            self.assertEqual(list((package_path / "assets").iterdir()), [])
            self.assertIn("Mock PDF document text", combined)
            self.assertIn("local_file", combined)

    def test_directory_input_routes_to_pdf_adapter_and_batches_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            pdf_dir = root / "pdfs"
            pdf_dir.mkdir()
            _write_fake_pdf(pdf_dir / "sample-a.pdf")
            _write_fake_pdf(pdf_dir / "sample-b.pdf")
            (pdf_dir / "sample-b.pdf").write_bytes((pdf_dir / "sample-b.pdf").read_bytes() + b"% variant\n")
            (pdf_dir / "notes.txt").write_text("not a pdf", encoding="utf-8")
            out_dir = root / "out"

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = cli_main(
                    [
                        "ingest",
                        str(pdf_dir),
                        "--out",
                        str(out_dir),
                        "--backend",
                        "pdf.mock",
                        "--package",
                    ]
                )

            payload = json.loads(stdout.getvalue())
            packages = sorted(out_dir.rglob("*.openmd"))

            self.assertEqual(exit_code, 0)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["item_count"], 2)
            self.assertEqual(len(packages), 2)
            self.assertTrue(all(validate_package(package).ok for package in packages))

    def test_default_parser_backend_fails_structurally_without_real_parser(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            pdf_path = root / "sample.pdf"
            out_dir = root / "out"
            _write_fake_pdf(pdf_path)

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = cli_main(
                    [
                        "ingest",
                        str(pdf_path),
                        "--out",
                        str(out_dir),
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
            self.assertEqual(runlog_rows[-1]["error_code"], "document_backend_missing")
            self.assertEqual(runlog_rows[-1]["backend"], "paddleocr.pdf")
            self.assertEqual(list(out_dir.rglob("*.openmd")), [])

    def test_markitdown_backend_parses_local_pdf_when_optional_dependency_is_available(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            pdf_path = Path(tmp_dir) / "sample.pdf"
            _write_fake_pdf(pdf_path)
            fake_module = types.ModuleType("markitdown")
            fake_module.__spec__ = importlib.machinery.ModuleSpec("markitdown", loader=None)

            class FakeConverted:
                text_content = "Openmd PDF text\n开放文档 PDF 文本"

            class FakeMarkItDown:
                def convert(self, source: str) -> FakeConverted:
                    self.source = source
                    return FakeConverted()

            fake_module.MarkItDown = FakeMarkItDown

            with patch.dict(sys.modules, {"markitdown": fake_module}):
                result = MarkItDownPDFParser().parse(str(pdf_path), title="Sample PDF")

        self.assertTrue(result.has_usable_text)
        self.assertEqual(result.status, "verified")
        self.assertEqual(result.source_level, "native_text")
        self.assertEqual(result.backend, "pdf.markitdown")
        self.assertIn("# Sample PDF", result.text)
        self.assertIn("开放文档 PDF 文本", result.text)
        self.assertEqual(result.metadata["parser_backend"], "pdf.markitdown")


class PdfDocumentBackendGateTests(unittest.TestCase):
    def test_optional_pdf_provider_reports_missing_when_dependency_is_unavailable(self) -> None:
        with patch.object(MarkItDownPDFParser, "is_available", return_value=False):
            safe = create_runtime_backend_provider_registry("pdf", BackendRuntimeOptions())
            self.assertIsInstance(safe.provider_for("pdf.mock"), PDFParserProvider)
            self.assertIsInstance(safe.provider_for("pdf.markitdown"), PDFParserProvider)
            self.assertFalse(safe.provider_for("pdf.markitdown").health().ok)

    def test_safe_collect_skeleton_smoke_does_not_run_real_pdf_tools(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            pdf_path = root / "sample.pdf"
            _write_fake_pdf(pdf_path)

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = cli_main(
                    [
                        "collect",
                        str(pdf_path),
                        "--out",
                        str(root / "out"),
                        "--manifest-only",
                    ]
                )

            payload = json.loads(stdout.getvalue())
            runlog_rows = [
                json.loads(line)
                for line in (root / "out" / "runlog.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            request = DiscoveryRequest(input_url=str(pdf_path), platform="pdf", limit=2)
            discovery_registry = create_runtime_discovery_provider_registry("pdf", BackendRuntimeOptions())

            self.assertEqual(exit_code, 1)
            self.assertFalse(payload["ok"])
            self.assertIsInstance(discovery_registry.provider_for(request), SkeletonDiscoveryProvider)
            self.assertEqual(runlog_rows[-1]["backend"], "skeleton.discovery")
            self.assertEqual(runlog_rows[-1]["error_code"], "discovery_not_implemented")


def _write_fake_pdf(path: Path) -> None:
    path.write_bytes(
        b"%PDF-1.4\n"
        b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Count 0>>endobj\n"
        b"%%EOF\n"
    )


if __name__ == "__main__":
    unittest.main()
