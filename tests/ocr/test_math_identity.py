"""B-fix: a unique digit match cannot grant formula rendering identity."""

from zipfile import ZipFile

import pytest

from openmd.ocr.native_math import restore_docx_math

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
M = "http://schemas.openxmlformats.org/officeDocument/2006/math"
MC = "http://schemas.openxmlformats.org/markup-compatibility/2006"
F = ('<m:oMath><m:f><m:num><m:r><m:t>123</m:t></m:r></m:num>'
     '<m:den><m:r><m:t>45</m:t></m:r></m:den></m:f></m:oMath>')


def write_math(path, content, *, other=None, attributes=""):
    xml = (f'<w:document xmlns:w="{W}" xmlns:m="{M}" xmlns:mc="{MC}" '
           f'xmlns:x="urn:future" {attributes}><w:body>{content}</w:body></w:document>')
    with ZipFile(path, "w") as z:
        z.writestr("word/document.xml", xml)
        for name, source in (other or {}).items():
            z.writestr(name, source)


@pytest.mark.parametrize("wrapper", [
    '<w:p><w:pPr>{}</w:pPr></w:p>',
    '<w:p><w:r><w:rPr>{}</w:rPr></w:r></w:p>',
    '<w:tbl><w:tblPr>{}</w:tblPr></w:tbl>',
    '<w:p><w:customXml>{}</w:customXml></w:p>',
    '<w:p><x:extension>{}</x:extension></w:p>',
    '<w:p><w:r><w:unknown>{}</w:unknown></w:r></w:p>',
    '<w:p><w:r>{}</w:r></w:p>',
    # Textbox shape visibility/geometry has not been licensed by this subset.
    '<w:p><w:r><w:pict><v:shape xmlns:v="urn:schemas-microsoft-com:vml" '
    'style="visibility:hidden;width:0;height:0"><v:textbox><w:txbxContent>'
    '<w:p>{}</w:p></w:txbxContent></v:textbox></v:shape></w:pict></w:r></w:p>',
    '<w:p><m:oMathPara><m:oMathParaPr>{}</m:oMathParaPr></m:oMathPara></w:p>',
    # Novel bypass: valid equation inside an attribute-like subtree of another
    # otherwise permitted equation. Whole-document veto includes valid peers.
    '<w:p><m:oMath><m:rPr>{}</m:rPr></m:oMath></w:p>',
])
def test_unapproved_ancestor_vetoes_whole_document(wrapper, tmp_path):
    path = tmp_path / "property-math.docx"
    write_math(path, '<w:p>' + F + '</w:p>' + wrapper.format(F))
    restored, receipt = restore_docx_math(path, "Invoice 12345.")
    assert restored == "Invoice 12345."
    assert receipt["restored"] == 0 and receipt["disabled"]
    assert "omml_unapproved_ancestor" in receipt["disabled_reasons"]


def test_review_property_math_single_candidate_cannot_rewrite_visible_invoice(tmp_path):
    path = tmp_path / 'property-math.docx'
    write_math(path, '<w:p><w:r><w:t>Invoice 12345. Reliable native source report with '
               'ordinary visible text and source number 42.</w:t></w:r></w:p>'
               '<w:p><w:pPr>' + F + '</w:pPr></w:p>')
    text, receipt = restore_docx_math(path, 'Invoice 12345. Reliable native source report with ordinary visible text and source number 42.')
    assert text.startswith('Invoice 12345.') and '\\frac' not in text
    assert receipt['restored'] == 0 and receipt['disabled']


@pytest.mark.parametrize("part", ["word/styles.xml", "word/footnotes.xml", "customXml/item1.xml", "word/headerExtra.xml", "word/custom.data"])
def test_unapproved_part_vetoes_document_math(part, tmp_path):
    path = tmp_path / "other-part.docx"
    write_math(path, '<w:p>' + F + '</w:p>', other={part: f'<w:hdr xmlns:w="{W}" xmlns:m="{M}"><w:p>{F}</w:p></w:hdr>'})
    text, receipt = restore_docx_math(path, "Invoice 12345.")
    assert text == "Invoice 12345."
    assert "omml_unapproved_part_or_root" in receipt["disabled_reasons"]


def test_duplicate_source_part_is_ambiguous_before_candidate_collection(tmp_path):
    path = tmp_path / 'duplicate.docx'
    write_math(path, '<w:p>' + F + '</w:p>')
    with pytest.warns(UserWarning, match='Duplicate name'):
        with ZipFile(path, 'a') as z:
            z.writestr('word/document.xml', f'<w:document xmlns:w="{W}"/>')
    text, receipt = restore_docx_math(path, 'Invoice 12345.')
    assert text == 'Invoice 12345.' and receipt['disabled_reasons'] == ['duplicate_source_part']


@pytest.mark.parametrize("prefix,namespace", [("m", M), ("w", W)])
def test_ignored_namespace_on_recognized_path_is_not_permitted(prefix, namespace, tmp_path):
    path = tmp_path / "ignored.docx"
    write_math(path, '<w:p>' + F + '</w:p>', attributes=f'mc:Ignorable="{prefix}"')
    text, receipt = restore_docx_math(path, "Invoice 12345.")
    assert text == "Invoice 12345." and receipt["disabled"]


@pytest.mark.parametrize("wrapper", [
    '<w:p>{}</w:p>',
    '<w:p><m:oMathPara>{}</m:oMathPara></w:p>',
    '<w:tbl><w:tr><w:tc><w:p>{}</w:p></w:tc></w:tr></w:tbl>',
    '<w:tbl><w:tr><w:tc><w:tbl><w:tr><w:tc><w:p>{}</w:p>'
    '</w:tc></w:tr></w:tbl></w:tc></w:tr></w:tbl>',
])
def test_permitted_rendering_paths_keep_normal_restoration(wrapper, tmp_path):
    path = tmp_path / "visible.docx"
    write_math(path, wrapper.format(F))
    text, receipt = restore_docx_math(path, "12345")
    assert text == r"$\frac{123}{45}$" and receipt["restored"] == 1


@pytest.mark.parametrize("part,root", [("word/header1.xml", "hdr"), ("word/footer2.xml", "ftr")])
def test_recognized_header_footer_do_not_supply_main_body_candidates(part, root, tmp_path):
    path = tmp_path / "header.docx"
    write_math(path, '<w:p/>', other={part: f'<w:{root} xmlns:w="{W}" xmlns:m="{M}"><w:p>{F}</w:p></w:{root}>'})
    text, receipt = restore_docx_math(path, "Invoice 12345.")
    assert text == "Invoice 12345." and receipt["restored"] == 0
    assert not receipt.get("disabled")
