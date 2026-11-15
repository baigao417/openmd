"""Generated nonprivate covers, partial scans and vector formula text."""

from __future__ import annotations

import importlib.util
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from openmd.core.models import ContentItem
from openmd.ocr.config import settings
from openmd.ocr.documents import _office_pdf_origin, extract_pdf, probe_page


@unittest.skipUnless(importlib.util.find_spec("pymupdf"), "optional PyMuPDF is unavailable")
class RenderConsistencyTests(unittest.TestCase):
    def setUp(self):
        import pymupdf

        self.doc = pymupdf.open()
        self.page = self.doc.new_page(width=400, height=500)
        self.addCleanup(self.doc.close)

    def native(self):
        self.page.insert_text((20, 300), "A normal native paragraph retains original typo reciept 12.04 and 486.72.", fontsize=9)

    def image(self, rect):
        from PIL import Image, ImageDraw

        image = Image.new("RGB", (400, 150), "white")
        ImageDraw.Draw(image).text((20, 50), "Visible total 42 USD", fill="black")
        data = io.BytesIO()
        image.save(data, format="PNG")
        self.page.insert_image(rect, stream=data.getvalue(), keep_proportion=False)

    def probe(self):
        return probe_page(self.page, settings())

    def test_later_white_cover_is_rejected(self):
        self.native()
        self.page.insert_text((20, 55), "Contradictory hidden invoice total 999 USD", fontsize=9)
        self.page.draw_rect((10, 30, 390, 70), color=None, fill=(1, 1, 1))
        probe = self.probe()
        self.assertEqual(probe["route"], "ocr")
        self.assertIn("rendered_text_obscured", probe["textlayer_rejected_reasons"])

    def test_later_colored_cover_is_rejected(self):
        self.native()
        self.page.insert_text((20, 55), "Contradictory hidden invoice total 999 USD", fontsize=9)
        self.page.draw_rect((10, 30, 390, 70), color=None, fill=(0.15, 0.4, 0.7))
        self.assertIn("rendered_text_obscured", self.probe()["textlayer_rejected_reasons"])

    def test_partial_scan_with_hidden_layer_rejected_below_half_coverage(self):
        self.native()
        self.image((0, 80, 400, 230))
        self.page.insert_text((20, 130), "Contradictory hidden invoice total 999 USD", fontsize=9)
        probe = self.probe()
        self.assertEqual(probe["image_area_ratio"], 0.3)
        self.assertEqual(probe["route"], "ocr")
        self.assertIn("text_image_overlap", probe["textlayer_rejected_reasons"])

    def test_partial_scan_with_covered_hidden_layer_rejected(self):
        self.page.insert_text((20, 55), "Contradictory hidden invoice total 999 USD", fontsize=9)
        self.page.draw_rect((10, 30, 390, 70), color=None, fill=(1, 1, 1))
        self.image((0, 100, 400, 250))
        probe = self.probe()
        self.assertEqual(probe["image_area_ratio"], 0.3)
        self.assertEqual(probe["route"], "ocr")
        self.assertIn("rendered_text_obscured", probe["textlayer_rejected_reasons"])

    def test_native_figure_and_nonoverlapping_caption_remain_native(self):
        self.native()
        self.image((0, 80, 400, 230))
        self.page.insert_text((20, 260), "Figure 1. This caption sits below the locally embedded figure.", fontsize=9)
        self.assertEqual(self.probe()["route"], "textlayer")

    def test_native_nonbreaking_spaces_do_not_require_rendered_ink(self):
        self.page.insert_text((20, 55), "Native\u00a0text\u00a0retains\u00a0original\u00a0reciept\u00a012.04\u00a0and\u00a0486.72.", fontsize=9)
        # Base-14 fonts normalize NBSP to ASCII; imitate a converter's Unicode
        # texttrace using the same real rendered empty space bboxes.
        spans = self.page.get_texttrace()
        for span in spans:
            span["chars"] = tuple((160, *char[1:]) if char[0] == 32 else char for char in span["chars"])
        self.assertTrue(any(char[0] == 160 for span in spans for char in span["chars"]))
        with patch.object(self.page, "get_texttrace", return_value=spans):
            probe = self.probe()
        self.assertEqual(probe["route"], "textlayer")
        self.assertEqual(probe["render_obscured_text_chars"], 0)

    def vector_equal(self, *, text="=", rect=False, cover=False):
        # Exactly the LibreOffice provenance: a filled outline, immediately
        # followed by a short invisible extraction span over the same glyph.
        if rect:
            self.page.draw_rect((80.4, 173, 86.4, 176), color=None, fill=(0, 0, 0))
        else:
            shape = self.page.new_shape()
            for y in (173, 175.5):
                shape.draw_polyline([(80.4, y), (86.4, y), (86.4, y + 0.5), (80.4, y + 0.5), (80.4, y)])
            shape.finish(color=None, fill=(0, 0, 0))
            shape.commit()
        self.page.insert_text((80, 180), text, fontsize=12, render_mode=3)
        if cover:
            self.page.draw_rect((75, 160, 100, 190), color=None, fill=(1, 1, 1))

    def test_vector_glyph_alternative_requires_current_office_conversion_origin(self):
        self.native()
        self.vector_equal()
        probe = self.probe()
        self.assertEqual(probe["route"], "ocr")
        self.assertEqual(probe["invisible_text_chars"], 1)
        self.assertEqual(probe["vector_alternative_text_chars"], 0)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "formula.pdf"
            self.doc.save(path)
            item = ContentItem(platform="pdf", input_type="single", url=path.as_uri())
            origin = _office_pdf_origin(path, Path(tmp) / "formula.docx")
            with patch("openmd.ocr.documents.LocalOCRBackend.recognize_image") as infer:
                result = extract_pdf(item, allow_ocr=True, _office_origin=origin)
        self.assertEqual(result.status, "verified")
        self.assertIn("reciept 12.04 and 486.72", result.text)
        self.assertIn("=", result.text)
        self.assertEqual(result.metadata["pages"][0]["vector_alternative_text_chars"], 1)
        infer.assert_not_called()

    def test_rectangle_is_not_vector_glyph_proof(self):
        self.native()
        self.vector_equal(rect=True)
        self.assertIn("invisible_render_mode", self.probe()["textlayer_rejected_reasons"])

    def test_occluded_vector_glyph_is_not_accepted(self):
        self.native()
        self.vector_equal(cover=True)
        self.assertIn("invisible_render_mode", self.probe()["textlayer_rejected_reasons"])

    def test_long_invisible_span_cannot_borrow_vector_glyph(self):
        self.native()
        self.vector_equal(text="9999")
        self.assertIn("invisible_render_mode", self.probe()["textlayer_rejected_reasons"])

    def test_partially_covered_span_cannot_borrow_other_characters_ink(self):
        self.native()
        self.page.insert_text((20, 55), "Invoice 999 original amount", fontsize=9)
        span = self.page.get_texttrace()[-1]
        digit_boxes = [char[3] for char in span["chars"] if char[0] == ord("9")]
        self.page.draw_rect((digit_boxes[0][0] - 0.2, digit_boxes[0][1] - 0.2, digit_boxes[-1][2] + 0.2, digit_boxes[-1][3] + 0.2), color=None, fill=(1, 1, 1))
        self.assertIn("rendered_text_obscured", self.probe()["textlayer_rejected_reasons"])
