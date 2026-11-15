"""Source-backed native math restoration never guesses from a flattened PDF."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zipfile import ZipFile

from openmd.core.models import ContentItem, TranscriptResult
from openmd.ocr.native_math import restore_docx_math
from openmd.ocr.office import recover_office


MATH = "http://schemas.openxmlformats.org/officeDocument/2006/math"
WORD = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
MC = "http://schemas.openxmlformats.org/markup-compatibility/2006"


def run(text):
    return "<m:r><m:t>" + text + "</m:t></m:r>"


def component(name, text):
    return "<m:" + name + ">" + run(text) + "</m:" + name + ">"


SUM = ('<m:nary><m:naryPr><m:chr m:val="∑"/></m:naryPr>'
       + component("sub", "t=1") + component("sup", "n")
       + '<m:e><m:f><m:num><m:sSub>' + component("e", "CF") + component("sub", "t")
       + '</m:sSub></m:num><m:den><m:sSup>' + component("e", "(1+r)") + component("sup", "t")
       + '</m:sSup></m:den></m:f></m:e></m:nary>')
FORMULA = run("NPV=") + SUM + run("−C0=0")
FLAT = "NPV =∑\nt =1\nn\nCFt\n(1+r )\nt −C0=0"
MARKUP = "$NPV=∑_{t=1}^{n}\\frac{CF_{t}}{(1+r)^{t}}−C0=0$"


class NativeMathTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "source.docx"

    def package(self, formula=FORMULA, repeats=1):
        with ZipFile(self.path, "w") as archive:
            archive.writestr("word/document.xml", f'<w:document xmlns:w="{WORD}" xmlns:m="{MATH}"><w:body><w:p>'
                             + ("<m:oMath>" + formula + "</m:oMath>") * repeats + "</w:p></w:body></w:document>")

    def test_unique_formula_restores_source_structure_and_preserves_native_typo_numbers(self):
        self.package()
        before, after = "Native reciept 001.20\n", "\nOriginal 12.04%"
        text, receipt = restore_docx_math(self.path, before + FLAT + after)
        self.assertEqual(text, before + MARKUP + after)
        self.assertEqual(receipt["restored"], 1)
        self.assertNotIn("NPV", str(receipt))

    def test_symbol_difference_never_authorizes_replacement(self):
        self.package()
        for text in (FLAT.replace("+r", "-r"), FLAT.replace("CFt", "CF*t"), FLAT.replace("C0", "C9")):
            with self.subTest(text=text):
                restored, receipt = restore_docx_math(self.path, text)
                self.assertEqual(restored, text)
                self.assertEqual(receipt["restored"], 0)

    def test_ambiguous_native_or_source_formulas_remain_unchanged(self):
        self.package()
        duplicate = FLAT + "\n" + FLAT
        self.assertEqual(restore_docx_math(self.path, duplicate)[0], duplicate)
        self.package(repeats=2)
        self.assertEqual(restore_docx_math(self.path, FLAT)[0], FLAT)

    def test_unsupported_structure_or_literal_markup_never_flattened(self):
        for xml in ('<m:rad>' + component("e", "1234") + '</m:rad>', run("x_y=1234"),
                    '<m:f><m:fPr><m:type m:val="noBar"/></m:fPr>' + component("num", "1234") + component("den", "5") + '</m:f>'):
            with self.subTest(xml=xml):
                self.package(xml)
                text, receipt = restore_docx_math(self.path, "12345 x_y=1234")
                self.assertEqual(text, "12345 x_y=1234")
                self.assertEqual(receipt["unsupported"], 1)

    def test_scripts_preserve_original_component_characters(self):
        self.package('<m:sSubSup>' + component("e", "xyz") + component("sub", "0") + component("sup", "2") + '</m:sSubSup>')
        self.assertEqual(restore_docx_math(self.path, "xyz 0 2")[0], "$xyz_{0}^{2}$")

    def test_invalid_package_keeps_native_text(self):
        self.path.write_bytes(b"invalid")
        text, receipt = restore_docx_math(self.path, FLAT)
        self.assertEqual(text, FLAT)
        self.assertTrue(receipt["source_unavailable"])

    def test_review3_unselected_alternate_formula_cannot_replace_invoice_digits(self):
        fraction = '<m:f>' + component("num", "123") + component("den", "45") + '</m:f>'
        xml = (f'<w:document xmlns:w="{WORD}" xmlns:m="{MATH}" xmlns:mc="{MC}" xmlns:future="urn:future">'
               '<w:body><w:p><w:r><w:t>Invoice 12345.</w:t></w:r></w:p>'
               '<mc:AlternateContent><mc:Choice Requires="future"><m:oMath>' + fraction + '</m:oMath>'
               '</mc:Choice><mc:Fallback><w:p><w:r><w:t>Fallback</w:t></w:r></w:p></mc:Fallback>'
               '</mc:AlternateContent></w:body></w:document>')
        with ZipFile(self.path, "w") as archive:
            archive.writestr("word/document.xml", xml)
        text, receipt = restore_docx_math(self.path, "Invoice 12345.")
        self.assertEqual(text, "Invoice 12345.")
        self.assertEqual(receipt["restored"], 0)
        self.assertTrue(receipt["disabled"])
        self.assertEqual(receipt["disabled_reasons"], ["alternate_content"])

    def test_review3_deleted_formula_and_all_revision_classes_disable_whole_document(self):
        fraction = '<m:f>' + component("num", "123") + component("den", "45") + '</m:f>'
        for namespace in (WORD, "http://purl.oclc.org/ooxml/wordprocessingml/main"):
            for revision in ("del", "ins", "moveFrom", "moveTo", "moveFromRangeStart", "moveToRangeEnd", "rPrChange"):
                with self.subTest(namespace=namespace, revision=revision):
                    xml = (f'<w:document xmlns:w="{namespace}" xmlns:m="{MATH}"><w:p><w:{revision}>'
                           '<m:oMath>' + fraction + f'</m:oMath></w:{revision}></w:p></w:document>')
                    with ZipFile(self.path, "w") as archive:
                        archive.writestr("word/document.xml", xml)
                    text, receipt = restore_docx_math(self.path, "Invoice 12345.")
                    self.assertEqual(text, "Invoice 12345.")
                    self.assertEqual(receipt["disabled_reasons"], ["revision_markup"])

    def test_new_variant_ambiguity_in_header_disables_unrelated_body_math(self):
        for marker, reason in ((f'<mc:AlternateContent xmlns:mc="{MC}"/>', "alternate_content"),
                               ('<w:ins/>', "revision_markup"),
                               ('<w:fldSimple w:instr="PAGE"/>', "field_code"),
                               ('<w:instrText>PAGE</w:instrText>', "field_code"),
                               ('<w:delInstrText>PAGE</w:delInstrText>', "field_code"),
                               ('<w:fldChar w:fldCharType="begin"/>', "field_code"),
                               ('<w:vanish/>', "hidden_content"),
                               ('<w:webHidden/>', "hidden_content"),
                               ('<w:altChunk/>', "dynamic_content"),
                               ('<w:subDoc/>', "dynamic_content"),
                               ('<w:dataBinding/>', "dynamic_content")):
            with self.subTest(marker=marker):
                self.package()
                with ZipFile(self.path, "a") as archive:
                    archive.writestr("word/header1.xml", f'<w:hdr xmlns:w="{WORD}">{marker}</w:hdr>')
                text, receipt = restore_docx_math(self.path, FLAT)
                self.assertEqual(text, FLAT)
                self.assertEqual(receipt["restored"], 0)
                self.assertEqual(receipt["disabled_reasons"], [reason])

    def test_review3_cross_paragraph_match_and_new_page_break_variant_are_rejected(self):
        self.package('<m:f>' + component("num", "123") + component("den", "45") + '</m:f>')
        for text in ("123\n\n45", "123\r\n \t\r\n45", "123\f45"):
            with self.subTest(text=text):
                result, receipt = restore_docx_math(self.path, text)
                self.assertEqual(result, text)
                self.assertEqual(receipt["restored"], 0)
                self.assertEqual(receipt["paragraph_boundary_rejections"], 1)

    def test_office_only_restores_proven_native_pages_and_serializes_provenance(self):
        self.package()
        item = ContentItem(platform="pdf", input_type="single", url=self.path.as_uri(), raw_meta={"local_path": str(self.path)})
        original = TranscriptResult(text="", status="failed", source_level="partial_extraction", backend="pdf.markitdown")
        for route, status, expected in (("textlayer", "verified", MARKUP), ("ocr", "verified", FLAT), ("textlayer", "partial", FLAT)):
            with self.subTest(route=route, status=status):
                extracted = TranscriptResult(text=FLAT, status=status, source_level="native_text", backend="pdf.textlayer", metadata={"pages": [{"page": 1, "route": route, "status": status, "engine": "PyMuPDF"}]})

                def convert(command, **kwargs):
                    (Path(command[-2]) / "source.pdf").write_bytes(b"synthetic")
                    return SimpleNamespace(returncode=0)

                with patch("openmd.ocr.office.soffice_path", return_value="synthetic"), patch("openmd.ocr.office.owned_popen", side_effect=convert), patch("openmd.ocr.office.wait_interruptibly"), patch("openmd.ocr.documents.extract_pdf", return_value=extracted):
                    result = recover_office(item, original, allow_ocr=False)
                self.assertEqual(result.text, expected)
                if expected == MARKUP:
                    self.assertTrue(any("DOCX OMML" in note for note in result.source_notes))
                    from openmd.core.runlog import build_item_runlog_entry
                    from openmd.core.package_writer import build_package_metadata
                    for metadata in (build_item_runlog_entry(item, result)["document_metadata"], build_package_metadata(item, result, created_at="now", updated_at="now")["document_metadata"]):
                        self.assertEqual(metadata["office_recovery"]["native_formula_restoration"]["restored"], 1)
                else:
                    self.assertNotIn("native_formula_restoration", result.metadata["office_recovery"])

    def test_office_empty_recovery_keeps_page_rejection_reasons_in_receipt(self):
        self.package()
        item = ContentItem(platform="pdf", input_type="single", url=self.path.as_uri(), raw_meta={"local_path": str(self.path)})
        original = TranscriptResult(text="", status="failed", source_level="partial_extraction", backend="pdf.markitdown")
        page = {"page": 1, "route": "ocr", "status": "partial", "textlayer_rejected_reasons": ["text_visibility_unproven"]}
        extracted = TranscriptResult(text="", status="partial", source_level="ocr", backend="ocr.chain", error_code="pdf_partial", metadata={"pages": [page]})

        def convert(command, **kwargs):
            (Path(command[-2]) / "source.pdf").write_bytes(b"synthetic PDF")
            return SimpleNamespace(returncode=0)

        with patch("openmd.ocr.office.soffice_path", return_value="synthetic"), patch("openmd.ocr.office.owned_popen", side_effect=convert), patch("openmd.ocr.office.wait_interruptibly"), patch("openmd.ocr.documents.extract_pdf", return_value=extracted):
            result = recover_office(item, original, allow_ocr=False)
        self.assertEqual(result.metadata["pages"], [page])
        self.assertEqual(result.status, "partial")
