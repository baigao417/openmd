"""Category regressions for review3 T1/T2, using nonprivate PDFs only."""

import importlib.util
from pathlib import Path
from unittest.mock import patch

import pytest

from openmd.core.models import ContentItem
from openmd.ocr.config import settings
from openmd.ocr.documents import _clip_operator, _office_pdf_origin, extract_pdf, inspect_pdf, probe_page

pytestmark = pytest.mark.skipif(not importlib.util.find_spec("pymupdf"), reason="optional PyMuPDF")


def document():
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page(width=595, height=400)
    page.insert_text((30, 40), "Reliable native report: retained invoice number 42. Source stays unchanged.")
    return doc, page


@pytest.mark.parametrize("mode", [0, 1, 2])
def test_normal_outline_and_pseudo_bold_remain_native_without_models(mode, tmp_path):
    import pymupdf
    with pymupdf.open() as doc:
        page = doc.new_page()
        text = "Normal printed document with bold outlined letters and source number 42."
        page.insert_text((50, 80), text, fontsize=12, render_mode=mode)
        path = tmp_path / f"fill-stroke-{mode}.pdf"
        doc.save(path)
    with patch("openmd.ocr.documents.LocalOCRBackend.recognize_image") as inference:
        result = extract_pdf(ContentItem(platform="pdf", input_type="single", url=path.as_uri()))
    inference.assert_not_called()
    assert result.status == "verified" and result.source_level == "native_text"
    assert text in result.text
    assert result.metadata["pages"][0]["route"] == "textlayer"


@pytest.mark.parametrize("same_text", [True, False])
def test_separate_fill_then_stroke_at_identical_coordinates_is_still_occlusion(same_text):
    import pymupdf
    with pymupdf.open() as doc:
        page = doc.new_page()
        text = "Normal printed document with source number 42."
        page.insert_text((50, 80), text, render_mode=0)
        page.insert_text((50, 80), text if same_text else text.replace("42", "99"), render_mode=1)
        probe = probe_page(page, settings())
        assert probe["route"] == "ocr"
        assert "overlapping_paint" in probe["textlayer_rejected_reasons"]


def test_same_paint_identity_inside_form_and_rotated_page_preserves_clip_gate():
    import pymupdf
    with pymupdf.open() as source, pymupdf.open() as target:
        p = source.new_page()
        p.insert_text((50, 80), "Outlined Form source report with document number 42.", render_mode=2)
        page = target.new_page()
        page.show_pdf_page(page.rect, source, 0)
        page.set_rotation(90)
        from openmd.ocr.documents import _text_paint_groups
        assert _text_paint_groups(page, page.get_texttrace()) == {}
        # Forms have a BBox clip: grouping cannot override the T1 clip gate.
        assert probe_page(page, settings())["route"] == "ocr"
        assert page.rotation == 90


def test_new_bypass_stroke_draw_after_real_combined_paint_is_not_same_operation():
    import pymupdf
    with pymupdf.open() as doc:
        page = doc.new_page()
        text = "Normal visible document and source invoice 12345."
        page.insert_text((50, 80), text, render_mode=2)
        page.insert_text((50, 80), text, render_mode=1)
        probe = probe_page(page, settings())
        assert probe["route"] == "ocr" and "overlapping_paint" in probe["textlayer_rejected_reasons"]


@pytest.mark.parametrize('mode', [0, 1, 2])
def test_multiple_source_text_shows_in_one_buffer_do_not_gain_same_paint_waiver(mode, tmp_path):
    import pymupdf
    with pymupdf.open() as doc:
        page = doc.new_page()
        page.insert_text((50, 80), 'Normal visible source document invoice 12345.', render_mode=mode)
        xref = page.get_contents()[0]
        raw = doc.xref_stream(xref).replace(
            b'ET', b'1 0 0 1 50 762 Tm [(Normal visible source document invoice 99999.)] TJ\nET')
        doc.update_stream(xref, raw)
        # The interpreter batches both text-show operators into one buffer;
        # Tr2 even has a genuine same-object fill/stroke pair. Still reject.
        traces = page.get_texttrace()
        assert len(traces) == (2 if mode == 2 else 1)
        probe = probe_page(page, settings())
        assert probe['route'] == 'ocr' and 'overlapping_paint' in probe['textlayer_rejected_reasons']
        path = tmp_path / 'buffered-overlay.pdf'
        doc.save(path)
    result = extract_pdf(ContentItem(platform='pdf', input_type='single', url=path.as_uri()))
    assert result.status == 'partial' and '12345' in result.text and '99999' in result.text


def test_single_later_character_show_in_shared_combined_buffer_is_unproven():
    import pymupdf
    with pymupdf.open() as doc:
        page = doc.new_page()
        page.insert_text((50,80), 'Normal visible source invoice 12345.', render_mode=2)
        # Repaint just the final glyph with a slight offset, inside the same BT.
        last = page.get_texttrace()[0]['chars'][-1]
        xref = page.get_contents()[0]
        stream = doc.xref_stream(xref).replace(b'ET', f'1 0 0 1 {last[2][0]+0.1} 762 Tm [(X)] Tj\nET'.encode())
        doc.update_stream(xref, stream)
        assert len(page.get_texttrace()) == 2
        probe = probe_page(page, settings())
        assert probe['route'] == 'ocr' and 'overlapping_paint' in probe['textlayer_rejected_reasons']


def test_normal_adjacent_kerning_in_one_combined_show_is_native():
    import pymupdf
    with pymupdf.open() as doc:
        page = doc.new_page()
        page.insert_text((50,80), 'Normal visible source invoice 12345.', render_mode=2)
        xref = page.get_contents()[0]
        stream = doc.xref_stream(xref)
        # One TJ operation with an ordinary adjacent-glyph kerning adjustment.
        start, end = stream.index(b'['), stream.index(b']')
        stream = stream[:start] + b'[(Normal) 20 ( visible source invoice 12345.)]' + stream[end+1:]
        doc.update_stream(xref, stream)
        assert probe_page(page, settings())['route'] == 'textlayer'


def test_fill_stroke_with_text_clip_keeps_t1_whole_page_rejection():
    import pymupdf
    with pymupdf.open() as doc:
        page = doc.new_page()
        page.insert_text((50, 80), "Normal visible document and source invoice 12345.", render_mode=6)
        from openmd.ocr.documents import _text_paint_groups
        assert len(_text_paint_groups(page, page.get_texttrace())) == 2
        probe = probe_page(page, settings())
        assert probe["route"] == "ocr" and "clip_visibility_unproven" in probe["textlayer_rejected_reasons"]


@pytest.mark.parametrize("clip", [b"100 270 140 2 re W n", b"100 270 m 240 270 l 240 271 l 100 271 l h W* n"])
def test_t1_clip_residual_ink_never_proves_visible_characters(clip, tmp_path):
    doc, page = document()
    try:
        page.insert_text((100, 130), "HIDDEN 999", fontsize=22)
        xref = page.get_contents()[-1]
        doc.update_stream(xref, b"q\n" + clip + b"\n" + doc.xref_stream(xref) + b"\nQ")
        path = tmp_path / "clipped.pdf"
        doc.save(path)
        probe = inspect_pdf(path)[0]
        assert "clip_hides_text" in probe["hard_reasons"]
        assert "999" not in probe["filtered_text"]
        result = extract_pdf(ContentItem(platform="pdf", input_type="single", url=path.as_uri()))
        # Text clipped out of view is hidden content: it must never reach the output.
        assert "999" not in result.text and "HIDDEN" not in result.text
        assert "clip_hides_text" in result.metadata["pages"][0]["hard_reasons"]
        assert result.metadata["pages"][0]["hidden_text_removed_chars"] >= 9
    finally:
        doc.close()


def vector_label(page, kind):
    shape = page.new_shape()
    points = ([(100, 124), (136.7, 124), (136.7, 127), (100, 124)] if kind == "triangle"
              else [(100, 124), (136.7, 124), (136.7, 125), (100, 125), (100, 124)])
    if kind == "rectangle":
        shape.draw_rect((100, 124, 137, 126))
    else:
        shape.draw_polyline(points)
    shape.finish(color=None, fill=(0, 0, 0), closePath=True)
    shape.commit()
    page.insert_text((100, 130), "999", fontsize=22, render_mode=3)


@pytest.mark.parametrize("kind", ["underline", "triangle", "rectangle"])
def test_t2_any_user_vector_shape_never_waives_invisible_mode(kind, tmp_path):
    doc, page = document()
    try:
        vector_label(page, kind)
        # Self-reported converter provenance is never an authority.
        doc.set_metadata({"producer": "LibreOffice", "creator": "Openmd"})
        path = tmp_path / "office.pdf"
        doc.save(path)
        item = ContentItem(platform="pdf", input_type="single", url=path.as_uri(),
                           raw_meta={"office_generated": True, "source_format": "docx", "local_path": str(path)})
        with patch("openmd.ocr.documents.LocalOCRBackend.recognize_image") as inference:
            result = extract_pdf(item)
        inference.assert_not_called()
        assert result.status == "partial"
        assert "999" not in result.text
        probe = result.metadata["pages"][0]
        assert probe["vector_alternative_text_chars"] == 0
        assert probe["textlayer_origin"] == "user_pdf"
        assert "invisible_render_mode" in probe["textlayer_rejected_reasons"]
    finally:
        doc.close()


def test_t2_conversion_origin_is_bound_to_both_path_and_current_bytes(tmp_path):
    doc, page = document()
    try:
        vector_label(page, "underline")
        path = tmp_path / "converted.pdf"
        doc.save(path)
        origin = _office_pdf_origin(path, Path("source.docx"))
        # Reusing a valid proof for another file cannot waive visibility checks.
        other = tmp_path / "other.pdf"
        other.write_bytes(path.read_bytes())
        for target in (other, path):
            if target == path:
                with path.open("ab") as file:
                    file.write(b"\n% changed since conversion\n")
            item = ContentItem(platform="pdf", input_type="single", url=target.as_uri())
            result = extract_pdf(item, _office_origin=origin)
            assert result.status == "partial"
            assert result.metadata["pages"][0]["textlayer_origin"] == "user_pdf"
        assert _office_pdf_origin(other, Path("source.xlsx")) is None
    finally:
        doc.close()


@pytest.mark.parametrize("opacity", [1.0, 0.02])
def test_any_later_overlapping_paint_even_thin_or_translucent_forces_ocr(opacity):
    doc, page = document()
    try:
        box = page.get_texttrace()[0]["chars"][3][3]
        # Unlike the review3 full cover, cover only a narrow glyph strip.
        page.draw_rect((box[0], box[1], box[0] + 0.25, box[3]), color=None, fill=(1, 1, 1), fill_opacity=opacity)
        probe = probe_page(page, settings())
        assert probe["route"] == "ocr"
        assert "overlapping_paint" in probe["textlayer_rejected_reasons"]
    finally:
        doc.close()


def test_annotation_overlay_cannot_borrow_underlying_visible_ink():
    doc, page = document()
    try:
        annot = page.add_rect_annot(page.get_texttrace()[0]["bbox"])
        annot.set_colors(fill=(1, 1, 1))
        annot.update()
        probe = probe_page(page, settings())
        assert probe["route"] == "ocr"
        assert "text_visibility_unproven" in probe["textlayer_rejected_reasons"]
    finally:
        doc.close()


def test_later_text_paint_is_also_an_occluding_object():
    doc, page = document()
    try:
        page.insert_text((30, 40), "OVERWRITE", color=(1, 1, 1))
        probe = probe_page(page, settings())
        assert probe["route"] == "ocr"
        assert "overlapping_paint" in probe["textlayer_rejected_reasons"]
    finally:
        doc.close()


def test_visibility_cannot_be_proved_when_graphics_probe_fails():
    doc, page = document()
    try:
        with patch.object(page, "get_drawings", side_effect=RuntimeError("unreadable graphics")):
            probe = probe_page(page, settings())
        assert probe["route"] == "ocr"
        assert "visibility_probe_failed" in probe["textlayer_rejected_reasons"]
    finally:
        doc.close()


def test_converter_ligature_continuation_requires_office_origin_not_borrowed_ink():
    doc, page = document()
    try:
        spans = page.get_texttrace()
        original = spans[0]["chars"]
        first = original[0]
        zero = (original[1][0], -1, first[2], (first[3][0], first[3][1], first[3][0], first[3][3]))
        spans[0]["chars"] = (first, zero, *original[2:])
        with patch.object(page, "get_texttrace", return_value=spans):
            untrusted = probe_page(page, settings())
            trusted = probe_page(page, settings(), _office_generated=True)
        assert untrusted["route"] == "textlayer"
        assert untrusted["hidden_text_removed_chars"] == 1
        assert "out_of_page_or_zero_width" in untrusted["hard_reasons"]
        assert trusted["route"] == "textlayer"
        assert trusted["office_glyph_alternative_text_chars"] == 1
        assert untrusted["office_glyph_alternative_text_chars"] == 0
    finally:
        doc.close()


@pytest.mark.parametrize("mode", [4, 5, 6, 7])
def test_text_rendering_clip_is_rejected_even_when_drawings_device_omits_it(mode):
    doc, page = document()
    try:
        page.insert_text((100, 130), "___", fontsize=22, render_mode=mode)
        xref = page.get_contents()[-1]
        clip = doc.xref_stream(xref)
        page.insert_text((100, 130), "HIDDEN 999", fontsize=22)
        target = page.get_contents()[-1]
        doc.update_stream(xref, clip.replace(b"\nQ", b"") + b"\n0 Tr\n" + doc.xref_stream(target) + b"\nQ")
        doc.update_stream(target, b"")
        probe = probe_page(page, settings())
        assert probe["route"] == "ocr"
        assert probe["content_clip_operator"]
        assert "clip_visibility_unproven" in probe["textlayer_rejected_reasons"]
    finally:
        doc.close()


@pytest.mark.parametrize("data,expected", [
    (b"4% comment\nTr", True), (b"+4.000 Tr", True), (b"q 1 2 3 4 re W* n", True),
    (b"(W 7 Tr \\( x \\) (nested)) Tj 0 Tr", False),
    (b"<572037205472> Tj /W /Tr 3 Tr", False),
    (b"% W 7 Tr\n0 Tr", False), (b"BI /W 1 /H 1 ID binary", True),
    (b"4Tr", True), (b"(unclosed string", True), (b"future-unknown-operator", True),
])
def test_clip_lexing_respects_pdf_objects_and_fails_closed_for_inline_payload(data, expected):
    assert _clip_operator(data) is expected
