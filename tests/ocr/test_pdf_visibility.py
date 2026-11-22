"""Synthetic, nonprivate PDF visibility and Office provenance regressions."""

from __future__ import annotations

import importlib.util
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zipfile import ZipFile

from openmd.core.models import ContentItem, TranscriptResult
from openmd.ocr.config import settings
from openmd.ocr.documents import extract_pdf, probe_page
from openmd.ocr.office import recover_office


@unittest.skipUnless(importlib.util.find_spec("pymupdf"), "optional PyMuPDF is unavailable")
class PDFVisibilityTests(unittest.TestCase):
    def setUp(self):
        import pymupdf

        self.document = pymupdf.open()
        self.page = self.document.new_page(width=300, height=400)
        self.config = settings()
        self.addCleanup(self.document.close)

    def add_text(self, **kwargs):
        self.page.insert_text((20, 50), "A native page with sufficiently many readable characters.", fontsize=9, **kwargs)

    def add_image(self, rect):
        from PIL import Image, ImageDraw

        image = Image.new("RGB", (64, 64), "white")
        ImageDraw.Draw(image).text((2, 2), "Visible 42", fill="black")
        data = io.BytesIO()
        image.save(data, format="PNG")
        self.page.insert_image(rect, stream=data.getvalue(), keep_proportion=False)

    def test_native_black_text_is_reliable(self):
        self.add_text()
        probe = probe_page(self.page, self.config)
        self.assertEqual(probe["route"], "textlayer")
        self.assertIsNone(probe["textlayer_rejected_reason"])

    def test_unambiguous_native_text_skips_span_raster_ink_scan(self):
        self.add_text()
        with patch("openmd.ocr.documents._rendered_ink", side_effect=AssertionError("clear vector text was raster-scanned")):
            probe = probe_page(self.page, self.config)
        self.assertEqual(probe["route"], "textlayer")

    def test_ruled_native_table_remains_text_without_gfm_inference(self):
        self.page.insert_text((20, 35), "Ordinary paragraph remains prose.", fontsize=10)
        xs, ys = (20, 120, 220), (70, 100, 130)
        for y in ys:
            self.page.draw_line((xs[0], y), (xs[-1], y), width=0.7)
        for x in xs:
            self.page.draw_line((x, ys[0]), (x, ys[-1]), width=0.7)
        for row, values in enumerate((("Name", "Value"), ("Alpha", "12"))):
            for col, value in enumerate(values):
                self.page.insert_text((xs[col] + 5, ys[row] + 20), value, fontsize=10)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ruled-table.pdf"
            self.document.save(path)
            result = extract_pdf(ContentItem(platform="pdf", input_type="single", url=path.as_uri()),
                                 backend="pdf.textlayer", allow_ocr=False)
        self.assertIn("Ordinary paragraph remains prose.", result.text)
        self.assertIn("Name", result.text)
        self.assertIn("Value", result.text)
        self.assertIn("Alpha", result.text)
        self.assertNotIn("| Name |", result.text)
        self.assertNotIn("| Alpha |", result.text)

    def test_hidden_native_text_inside_grid_is_never_table_output(self):
        xs, ys = (20, 120, 220), (70, 100, 130)
        for y in ys:
            self.page.draw_line((xs[0], y), (xs[-1], y), width=0.7)
        for x in xs:
            self.page.draw_line((x, ys[0]), (x, ys[-1]), width=0.7)
        self.page.insert_text((25, 90), "Visible", fontsize=10)
        self.page.insert_text((125, 90), "Value", fontsize=10)
        self.page.insert_text((25, 120), "Alpha", fontsize=10)
        self.page.insert_text((125, 120), "Secret 9981", fontsize=10, render_mode=3)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "hidden-grid-text.pdf"
            self.document.save(path)
            result = extract_pdf(ContentItem(platform="pdf", input_type="single", url=path.as_uri()),
                                 backend="pdf.textlayer", allow_ocr=False)
        self.assertNotIn("Secret", result.text)
        self.assertNotIn("9981", result.text)

    def test_scan_with_dense_hidden_layer_uses_ocr_and_never_returns_hidden_text(self):
        self.add_image(self.page.rect)
        for y in range(30, 380, 16):
            self.page.insert_text((10, y), "Contradictory hidden invoice total 999 USD", fontsize=10, color=(1, 1, 1))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "hidden-layer.pdf"
            self.document.save(path)
            result = extract_pdf(ContentItem(platform="pdf", input_type="single", url=path.as_uri()), allow_ocr=False)
        self.assertEqual(result.status, "partial")
        self.assertEqual(result.metadata["pages"][0]["route"], "ocr")
        self.assertEqual(result.metadata["pages"][0]["textlayer_rejected_reason"], "image_coverage")
        self.assertNotIn("999", result.text)

    def test_image_coverage_threshold_inclusive_and_configurable(self):
        self.add_image((0, 0, 300, 200))
        # Coverage policy is independent of the stronger text/image overlap
        # rule: keep this native caption outside the image bounds.
        self.page.insert_text((20, 250), "A native caption with sufficiently many readable characters.", fontsize=9)
        probe = probe_page(self.page, self.config)
        self.assertEqual(probe["image_area_ratio"], 0.5)
        self.assertEqual(probe["textlayer_rejected_reason"], "image_coverage")
        self.assertEqual(probe_page(self.page, {**self.config, "textlayer_image_coverage": 0.6})["route"], "textlayer")

    def test_overlapping_image_bounds_are_not_double_counted(self):
        self.add_image((0, 200, 300, 300))
        self.add_image((0, 200, 300, 300))
        self.add_text()
        probe = probe_page(self.page, self.config)
        self.assertEqual(probe["image_area_ratio"], 0.25)
        self.assertEqual(probe["route"], "textlayer")

    def test_invisible_render_mode_rejected_without_raster_image(self):
        self.add_text(render_mode=3)
        probe = probe_page(self.page, self.config)
        self.assertEqual(probe["route"], "ocr")
        self.assertEqual(probe["textlayer_rejected_reason"], "invisible_render_mode")

    def test_white_and_near_white_text_rejected_on_white_background(self):
        for color in ((1, 1, 1), (0.96, 0.96, 0.96)):
            with self.subTest(color=color):
                document = self.document.__class__()
                try:
                    page = document.new_page(width=300, height=400)
                    page.insert_text((20, 50), "Low contrast synthetic native text with enough characters.", fontsize=9, color=color)
                    probe = probe_page(page, self.config)
                    self.assertEqual(probe["route"], "ocr")
                    self.assertEqual(probe["textlayer_rejected_reason"], "low_contrast_text")
                finally:
                    document.close()

    def test_near_colored_background_is_rejected(self):
        self.page.draw_rect(self.page.rect, color=None, fill=(0.3, 0.5, 0.7))
        self.add_text(color=(0.32, 0.52, 0.72))
        self.assertEqual(probe_page(self.page, self.config)["textlayer_rejected_reason"], "low_contrast_text")

    def test_white_text_on_dark_background_remains_visible(self):
        self.page.draw_rect(self.page.rect, color=None, fill=(0, 0, 0))
        self.add_text(color=(1, 1, 1))
        self.assertEqual(probe_page(self.page, self.config)["route"], "textlayer")

    def test_mixed_document_keeps_native_page_and_records_rejected_layer(self):
        self.add_text()
        second = self.document.new_page(width=300, height=400)
        second.insert_text((20, 50), "Hidden second page text is never an accepted native layer.", fontsize=9, render_mode=3)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mixed.pdf"
            self.document.save(path)
            fake = TranscriptResult(text="Visible recovered content.", status="verified", source_level="ocr", backend="ocr.chain", metadata={"pages": [{"page": 2, "route": "ocr", "status": "verified", "engine": "Ovis3", "fallback_used": False}]})
            with patch("openmd.ocr.documents.LocalOCRBackend.recognize_image", return_value=fake) as recognize:
                result = extract_pdf(ContentItem(platform="pdf", input_type="single", url=path.as_uri()), allow_ocr=True)
        self.assertEqual(result.status, "verified")
        self.assertEqual([page["route"] for page in result.metadata["pages"]], ["textlayer", "ocr"])
        self.assertEqual(result.metadata["pages"][1]["textlayer_rejected_reason"], "invisible_render_mode")
        self.assertEqual(recognize.call_count, 1)


class OfficeReceiptTests(unittest.TestCase):
    def test_recovery_records_actual_page_routes_and_engines(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "visual.docx"
            with ZipFile(path, "w") as archive:
                archive.writestr("word/media/image1.png", b"synthetic")
            item = ContentItem(platform="pdf", input_type="single", url=path.as_uri(), raw_meta={"local_path": str(path)})
            original = TranscriptResult(text="", status="failed", source_level="partial_extraction", backend="pdf.markitdown")
            recovered = TranscriptResult(text="Actual native page output.", status="verified", source_level="native_text", backend="ocr.chain", metadata={"pages": [{"page": 1, "route": "textlayer", "engine": "PyMuPDF"}]})

            def convert(command, **kwargs):
                (Path(command[-2]) / "visual.pdf").write_bytes(b"synthetic PDF")
                return SimpleNamespace(returncode=0)

            with patch("openmd.ocr.office.soffice_path", return_value="synthetic-soffice"), patch("openmd.ocr.office.owned_popen", side_effect=convert), patch("openmd.ocr.office.wait_interruptibly"), patch("openmd.ocr.documents.extract_pdf", return_value=recovered):
                result = recover_office(item, original, allow_ocr=True)
        receipt = result.metadata["office_recovery"]
        self.assertNotIn("page_backend", receipt)
        self.assertEqual(receipt["page_extractions"], [{"page": 1, "route": "textlayer", "engine": "PyMuPDF"}])
        self.assertNotIn("ocr.chain", str(receipt))
