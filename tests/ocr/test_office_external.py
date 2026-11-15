"""Synthetic Office external-resource guards; no remote requests or models."""

from __future__ import annotations

import re
import struct
import tempfile
import threading
import unittest
import zlib
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from xml.etree import ElementTree as ET
from zipfile import ZIP_DEFLATED, ZipFile

from openmd.core.models import ContentItem, TranscriptResult
from openmd.ocr.office import inspect_external_resources, recover_office


REL = "http://schemas.openxmlformats.org/package/2006/relationships"
OFFICE_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"
WORD = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def package(path, parts):
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        for name, content in parts.items():
            archive.writestr(name, content)


def relation(kind, target="http://127.0.0.1:9/not-requested", mode="External", prefix=""):
    tag = prefix + ":" if prefix else ""
    namespace = f'xmlns:{prefix}' if prefix else "xmlns"
    return f'<{tag}Relationships {namespace}="{REL}"><{tag}Relationship Id="rId9" Type="{OFFICE_REL}{kind}" Target="{target}" TargetMode="{mode}"/></{tag}Relationships>'


def text_hyperlink(word=WORD, relationships=OFFICE_REL.rstrip("/"), extra=""):
    return f'<w:document xmlns:w="{word}" xmlns:r="{relationships}"><w:hyperlink r:id="rId9"><w:r><w:rPr><w:rStyle w:val="Hyperlink"/></w:rPr><w:t>Click text</w:t></w:r></w:hyperlink>{extra}</w:document>'


def run_recovery(path):
    item = ContentItem(platform="pdf", input_type="single", url=path.as_uri(), raw_meta={"local_path": str(path)})
    original = TranscriptResult(text="Native typo teh 001.20", status="verified", source_level="native_text", backend="pdf.markitdown")
    with patch("openmd.ocr.office.soffice_path", return_value="synthetic-soffice"), patch("openmd.ocr.office.owned_popen") as convert:
        # Empty-result/sparse-object detection is orthogonal to the resource guard.
        with patch("openmd.ocr.office.suspected_loss", return_value={"suspected": True, "reason": "visual_objects_sparse_text"}):
            result = recover_office(item, original, allow_ocr=False)
    return result, convert


def embedded_office_parts(suffix):
    """Small self-contained OPC packages with a real inline picture.

    Build the Office XML directly so this regression never depends on optional
    python-docx/python-pptx/openpyxl/Pillow fixture generators.
    """
    a = "http://schemas.openxmlformats.org/drawingml/2006/main"
    r = OFFICE_REL.rstrip("/")
    picture = '<a:blip r:embed="rIdImage"/><a:stretch><a:fillRect/></a:stretch>'
    geometry = '<a:xfrm><a:off x="0" y="0"/><a:ext cx="914400" cy="914400"/></a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom>'
    group = '<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr/>'
    if suffix == "docx":
        main, image_part, image_rels = "word/document.xml", "word/media/image1.png", "word/_rels/document.xml.rels"
        overrides = {main: "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"}
        drawing = f'<w:drawing><wp:inline distT="0" distB="0" distL="0" distR="0"><wp:extent cx="914400" cy="914400"/><wp:docPr id="1" name="Embedded picture"/><a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture"><pic:pic><pic:nvPicPr><pic:cNvPr id="1" name="image1.png"/><pic:cNvPicPr/></pic:nvPicPr><pic:blipFill>{picture}</pic:blipFill><pic:spPr>{geometry}</pic:spPr></pic:pic></a:graphicData></a:graphic></wp:inline></w:drawing>'
        content = {main: f'<w:document xmlns:w="{WORD}" xmlns:r="{r}" xmlns:a="{a}" xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture"><w:body><w:p><w:r><w:t>Native text with linked picture.</w:t></w:r></w:p><w:p><w:r>{drawing}</w:r></w:p><w:sectPr/></w:body></w:document>'}
        image_target = "media/image1.png"
    elif suffix == "pptx":
        p = "http://schemas.openxmlformats.org/presentationml/2006/main"
        main, image_part, image_rels = "ppt/presentation.xml", "ppt/media/image1.png", "ppt/slides/_rels/slide1.xml.rels"
        overrides = {main: "application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml", "ppt/slides/slide1.xml": "application/vnd.openxmlformats-officedocument.presentationml.slide+xml"}
        shape = f'<p:pic><p:nvPicPr><p:cNvPr id="2" name="Embedded picture"/><p:cNvPicPr/><p:nvPr/></p:nvPicPr><p:blipFill>{picture}</p:blipFill><p:spPr>{geometry}</p:spPr></p:pic>'
        content = {
            main: f'<p:presentation xmlns:p="{p}" xmlns:r="{r}"><p:sldIdLst><p:sldId id="256" r:id="rIdSlide"/></p:sldIdLst><p:sldSz cx="9144000" cy="6858000"/><p:notesSz cx="6858000" cy="9144000"/></p:presentation>',
            "ppt/_rels/presentation.xml.rels": f'<Relationships xmlns="{REL}"><Relationship Id="rIdSlide" Type="{OFFICE_REL}slide" Target="slides/slide1.xml"/></Relationships>',
            "ppt/slides/slide1.xml": f'<p:sld xmlns:p="{p}" xmlns:r="{r}" xmlns:a="{a}"><p:cSld><p:spTree>{group}{shape}</p:spTree></p:cSld></p:sld>',
        }
        image_target = "../media/image1.png"
    else:
        s = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
        xdr = "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"
        main, image_part, image_rels = "xl/workbook.xml", "xl/media/image1.png", "xl/drawings/_rels/drawing1.xml.rels"
        overrides = {main: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml", "xl/worksheets/sheet1.xml": "application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml", "xl/drawings/drawing1.xml": "application/vnd.openxmlformats-officedocument.drawing+xml"}
        shape = f'<xdr:pic><xdr:nvPicPr><xdr:cNvPr id="1" name="Embedded picture"/><xdr:cNvPicPr/></xdr:nvPicPr><xdr:blipFill>{picture}</xdr:blipFill><xdr:spPr>{geometry}</xdr:spPr></xdr:pic>'
        content = {
            main: f'<workbook xmlns="{s}" xmlns:r="{r}"><sheets><sheet name="Sheet1" sheetId="1" r:id="rIdSheet"/></sheets></workbook>',
            "xl/_rels/workbook.xml.rels": f'<Relationships xmlns="{REL}"><Relationship Id="rIdSheet" Type="{OFFICE_REL}worksheet" Target="worksheets/sheet1.xml"/></Relationships>',
            "xl/worksheets/sheet1.xml": f'<worksheet xmlns="{s}" xmlns:r="{r}"><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>Native text</t></is></c></row></sheetData><drawing r:id="rIdDrawing"/></worksheet>',
            "xl/worksheets/_rels/sheet1.xml.rels": f'<Relationships xmlns="{REL}"><Relationship Id="rIdDrawing" Type="{OFFICE_REL}drawing" Target="../drawings/drawing1.xml"/></Relationships>',
            "xl/drawings/drawing1.xml": f'<xdr:wsDr xmlns:xdr="{xdr}" xmlns:a="{a}" xmlns:r="{r}"><xdr:oneCellAnchor><xdr:from><xdr:col>1</xdr:col><xdr:colOff>0</xdr:colOff><xdr:row>1</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:from><xdr:ext cx="914400" cy="914400"/>{shape}<xdr:clientData/></xdr:oneCellAnchor></xdr:wsDr>',
        }
        image_target = "../media/image1.png"
    content[image_rels] = f'<Relationships xmlns="{REL}"><Relationship Id="rIdImage" Type="{OFFICE_REL}image" Target="{image_target}"/></Relationships>'
    content["_rels/.rels"] = f'<Relationships xmlns="{REL}"><Relationship Id="rIdOffice" Type="{OFFICE_REL}officeDocument" Target="{main}"/></Relationships>'
    content["[Content_Types].xml"] = '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Default Extension="png" ContentType="image/png"/>' + "".join(f'<Override PartName="/{name}" ContentType="{kind}"/>' for name, kind in overrides.items()) + '</Types>'
    def png_chunk(kind, payload):
        return struct.pack("!I", len(payload)) + kind + payload + struct.pack("!I", zlib.crc32(kind + payload))

    content[image_part] = b"\x89PNG\r\n\x1a\n" + png_chunk(b"IHDR", struct.pack("!IIBBBBB", 1, 1, 8, 2, 0, 0, 0)) + png_chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00")) + png_chunk(b"IEND", b"")
    return content


class OfficeResourceGuardTests(unittest.TestCase):
    def test_every_external_nonhyperlink_relation_is_blocked_in_every_format(self):
        types = ("image", "attachedTemplate", "oleObject", "ole", "frame", "subDocument", "externalLink", "unknownFutureKind")
        for suffix, folder in (("docx", "word"), ("pptx", "ppt"), ("xlsx", "xl")):
            for kind in types:
                with self.subTest(suffix=suffix, kind=kind), tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp) / ("external." + suffix)
                    package(path, {f"{folder}/nested/_rels/object.xml.rels": relation(kind, prefix="p")})
                    result, convert = run_recovery(path)
                    self.assertEqual(result.status, "partial")
                    self.assertEqual(result.error_code, "office_external_resource_blocked")
                    self.assertEqual(result.text, "Native typo teh 001.20")
                    self.assertTrue(result.metadata["terminal_partial"])
                    receipt = result.metadata["office_recovery"]["external_resources"]
                    self.assertEqual(receipt["relationship_types"], [OFFICE_REL + kind])
                    self.assertEqual(receipt["target_hosts"], ["127.0.0.1"])
                    self.assertFalse(result.metadata["office_external_resources_fetched"])
                    self.assertNotIn("not-requested", str(receipt))
                    convert.assert_not_called()

    def test_relationship_namespace_encoding_and_mode_variations(self):
        with tempfile.TemporaryDirectory() as tmp:
            for encoding in ("utf-8", "utf-16"):
                with self.subTest(encoding=encoding):
                    path = Path(tmp) / "external.docx"
                    xml = '<?xml version="1.0" encoding="' + encoding + '"?>' + relation("image", mode=" external ", prefix="rel")
                    package(path, {"custom/_rels/not-main.xml.rels": xml.encode(encoding)})
                    self.assertTrue(inspect_external_resources(path)["blocked"])

    def test_hyperlink_exemption_requires_text_only_reference_and_supports_strict_ooxml(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "hyperlink.docx"
            for prefix, word in ((OFFICE_REL, WORD), ("http://purl.oclc.org/ooxml/officeDocument/relationships/", "http://purl.oclc.org/ooxml/wordprocessingml/main")):
                package(path, {"word/_rels/document.xml.rels": relation("hyperlink").replace(OFFICE_REL, prefix),
                               "word/document.xml": text_hyperlink(word, prefix.rstrip("/"))})
                report = inspect_external_resources(path)
                self.assertFalse(report["blocked"])
                self.assertEqual(report["hyperlinks"], 1)
            package(path, {"_rels/.rels": relation("hyperlink")})
            self.assertTrue(inspect_external_resources(path)["blocked"])

    def test_simple_and_split_complex_word_fields_are_blocked(self):
        for command in ("INCLUDEPICTURE", "INCLUDETEXT", "LINK"):
            simple = f'<w:fldSimple w:instr=" {command} &quot;https://127.0.0.1:9/remote?a=1&amp;b=2&quot;"/>'
            pieces = (command[:4], command[4:] + ' "ht', 'tp://127.0.0.1:9/remote"')
            complex_field = '<w:r><w:fldChar w:fldCharType="begin"/></w:r>' + "".join(f'<w:r><w:instrText xml:space="preserve">{part}</w:instrText></w:r>' for part in pieces) + '<w:r><w:fldChar w:fldCharType="separate"/></w:r><w:r><w:t>display</w:t></w:r><w:r><w:fldChar w:fldCharType="end"/></w:r>'
            for field in (simple, complex_field):
                with self.subTest(command=command, split=field == complex_field), tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp) / "fields.docx"
                    xml = f'<?xml version="1.0" encoding="utf-16"?><w:hdr xmlns:w="{WORD}"><w:p>{field}</w:p></w:hdr>'
                    package(path, {"word/header2.xml": xml.encode("utf-16")})
                    result, convert = run_recovery(path)
                    self.assertEqual(result.error_code, "office_external_resource_blocked")
                    self.assertEqual(result.metadata["office_recovery"]["external_resources"]["field_types"], [command])
                    convert.assert_not_called()

    def test_split_incomplete_instruction_and_network_share_are_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "fields.docx"
            xml = f'<q:document xmlns:q="{WORD}"><q:instrText>INCLUDE</q:instrText><q:instrText>TEXT "\\\\server\\share\\file.docx"</q:instrText></q:document>'
            package(path, {"word/document.xml": xml})
            self.assertEqual(inspect_external_resources(path)["field_types"], ["INCLUDETEXT"])

    def test_native_local_word_field_is_not_an_external_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "local.docx"
            xml = f'<w:document xmlns:w="{WORD}"><w:fldSimple w:instr="INCLUDETEXT &quot;C:\\local\\source.docx&quot;"/></w:document>'
            package(path, {"word/document.xml": xml})
            self.assertFalse(inspect_external_resources(path)["blocked"])

    def test_malformed_relationship_xml_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "broken.docx"
            package(path, {"word/_rels/document.xml.rels": b"<Relationships"})
            result, convert = run_recovery(path)
            self.assertEqual(result.error_code, "office_external_resource_check_failed")
            self.assertEqual(result.status, "partial")
            convert.assert_not_called()

    def test_hyperlinks_and_embedded_content_continue_conversion(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "embedded.docx"
            package(path, {"word/_rels/document.xml.rels": relation("hyperlink"), "word/document.xml": text_hyperlink(), "word/media/image1.png": b"synthetic embedded image"})
            item = ContentItem(platform="pdf", input_type="single", url=path.as_uri(), raw_meta={"local_path": str(path)})
            original = TranscriptResult(text="", status="failed", source_level="partial_extraction", backend="pdf.markitdown")
            recovered = TranscriptResult(text="Native typo teh 001.20", status="verified", source_level="native_text", backend="pdf.textlayer", metadata={"pages": [{"page": 1, "route": "textlayer", "engine": "PyMuPDF"}]})

            def convert(command, **kwargs):
                (Path(command[-2]) / "embedded.pdf").write_bytes(b"synthetic PDF")
                return SimpleNamespace(returncode=0)

            with patch("openmd.ocr.office.soffice_path", return_value="synthetic-soffice"), patch("openmd.ocr.office.owned_popen", side_effect=convert) as spawned, patch("openmd.ocr.office.wait_interruptibly"), patch("openmd.ocr.documents.extract_pdf", return_value=recovered):
                result = recover_office(item, original, allow_ocr=False)
            spawned.assert_called_once()
            self.assertEqual(result.status, "verified")
            self.assertEqual(result.text, recovered.text)

    def test_explicit_network_opt_in_allows_external_conversion_and_redacts_targets(self):
        for part, xml in (
            ("word/_rels/document.xml.rels", relation("image", target="https://user:password@resource.example:8443/picture.png?secret=value&amp;x=2")),
            ("word/document.xml", f'<w:document xmlns:w="{WORD}"><w:fldSimple w:instr="INCLUDETEXT &quot;https://user:password@resource.example/private?secret=value&quot;"/></w:document>'),
        ):
            with self.subTest(part=part), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "external.docx"
                package(path, {part: xml})
                item = ContentItem(platform="pdf", input_type="single", url=path.as_uri(), raw_meta={"local_path": str(path)})
                original = TranscriptResult(text="", status="failed", source_level="partial_extraction", backend="pdf.markitdown")
                recovered = TranscriptResult(text="Native teh 001.20 retained", status="verified", source_level="native_text", backend="pdf.textlayer", metadata={"pages": [{"page": 1, "route": "textlayer", "engine": "PyMuPDF"}]})

                def convert(command, **kwargs):
                    (Path(command[-2]) / "external.pdf").write_bytes(b"synthetic PDF")
                    return SimpleNamespace(returncode=0)

                with patch("openmd.ocr.office.soffice_path", return_value="synthetic-soffice"), patch("openmd.ocr.office.owned_popen", side_effect=convert) as spawned, patch("openmd.ocr.office.wait_interruptibly"), patch("openmd.ocr.documents.extract_pdf", return_value=recovered):
                    result = recover_office(item, original, allow_ocr=True, allow_external_resources=True)
                spawned.assert_called_once()
                self.assertTrue(result.metadata["office_external_resources_fetched"])
                resources = result.metadata["office_recovery"]["external_resources"]
                self.assertEqual(resources["target_hosts"], ["resource.example"])
                self.assertTrue(resources["relationship_types"] or resources["field_types"])
                for private in ("password", "secret", "value", "picture.png", "private?", "8443", "user:"):
                    self.assertNotIn(private, str(result.metadata))
                self.assertTrue(any("depends on external resources" in note for note in result.source_notes))
                from openmd.core.runlog import build_item_runlog_entry
                from openmd.core.package_writer import build_package_metadata
                for metadata in (
                    build_item_runlog_entry(item, result)["document_metadata"],
                    build_package_metadata(item, result, created_at="2026-11-09T00:00:00Z", updated_at="2026-11-09T00:00:00Z")["document_metadata"],
                ):
                    self.assertTrue(metadata["office_external_resources_fetched"])
                    self.assertEqual(metadata["office_recovery"]["external_resources"]["target_hosts"], ["resource.example"])

    def test_ocr_permission_alone_does_not_grant_office_resource_access(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "external.docx"
            package(path, {"_rels/.rels": relation("attachedTemplate")})
            item = ContentItem(platform="pdf", input_type="single", url=path.as_uri(), raw_meta={"local_path": str(path)})
            original = TranscriptResult(text="", status="failed", source_level="partial_extraction", backend="pdf.markitdown")
            with patch("openmd.ocr.office.owned_popen") as convert:
                result = recover_office(item, original, allow_ocr=True)
            convert.assert_not_called()
            self.assertEqual(result.error_code, "office_external_resource_blocked")

    def test_external_conversion_failure_retains_fetched_flag_and_provenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "external.docx"
            package(path, {"_rels/.rels": relation("image")})
            item = ContentItem(platform="pdf", input_type="single", url=path.as_uri(), raw_meta={"local_path": str(path)})
            original = TranscriptResult(text="", status="failed", source_level="partial_extraction", backend="pdf.markitdown")
            with patch("openmd.ocr.office.soffice_path", return_value="synthetic-soffice"), patch("openmd.ocr.office.owned_popen", return_value=SimpleNamespace(returncode=1)), patch("openmd.ocr.office.wait_interruptibly"):
                result = recover_office(item, original, allow_ocr=False, allow_external_resources=True)
            self.assertEqual(result.error_code, "libreoffice_conversion_failed")
            self.assertTrue(result.metadata["office_external_resources_fetched"])
            self.assertEqual(result.metadata["office_recovery"]["external_resources"]["target_hosts"], ["127.0.0.1"])


class ValidOfficeCanaryTests(unittest.TestCase):
    def test_valid_docx_pptx_xlsx_linked_images_make_zero_canary_requests(self):
        """Generate complete local OPC packages, then link the picture out."""
        hits = []

        class Canary(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                hits.append(self.path)
                self.send_response(200)
                self.end_headers()

        server = HTTPServer(("127.0.0.1", 0), Canary)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                directory = Path(tmp)
                for suffix in ("docx", "pptx", "xlsx"):
                    with self.subTest(suffix=suffix):
                        source_path = directory / ("base." + suffix)
                        package(source_path, embedded_office_parts(suffix))
                        self.assertFalse(inspect_external_resources(source_path)["blocked"])
                        target = directory / ("linked." + suffix)
                        with ZipFile(source_path) as source, ZipFile(target, "w", ZIP_DEFLATED) as output:
                            for info in source.infolist():
                                data = source.read(info)
                                if info.filename.endswith(".rels"):
                                    root = ET.fromstring(data)
                                    for node in root:
                                        if node.get("Type", "").endswith("/image"):
                                            node.set("TargetMode", "External")
                                            node.set("Target", f"http://127.0.0.1:{server.server_port}/{suffix}-canary.png")
                                    data = ET.tostring(root, encoding="utf-8", xml_declaration=True)
                                elif info.filename.endswith(".xml"):
                                    # Preserve the producer's namespace declarations,
                                    # including mc:Ignorable prefixes, verbatim.
                                    data = re.sub(rb"\br:embed=", b"r:link=", data)
                                output.writestr(info, data)
                        result, convert = run_recovery(target)
                        self.assertEqual(result.error_code, "office_external_resource_blocked")
                        convert.assert_not_called()
                        self.assertEqual(hits, [])
        finally:
            server.shutdown()
            thread.join()
            server.server_close()


class OfficeReferenceCategoryTests(unittest.TestCase):
    """T3 review3 reproductions plus independent reference-use bypasses."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "source.docx"

    def test_review3_linked_picture_cannot_borrow_hyperlink_type(self):
        parts = embedded_office_parts("docx")
        rels = "word/_rels/document.xml.rels"
        parts[rels] = parts[rels].replace(OFFICE_REL + "image", OFFICE_REL + "hyperlink").replace('Target="media/image1.png"', 'TargetMode="External" Target="http://127.0.0.1:9/review3.png"')
        parts["word/document.xml"] = parts["word/document.xml"].replace("r:embed=", "r:link=")
        package(self.path, parts)
        result, convert = run_recovery(self.path)
        self.assertEqual(result.error_code, "office_external_resource_blocked")
        self.assertEqual(result.metadata["office_recovery"]["external_resources"]["hyperlinks"], 0)
        self.assertEqual(result.metadata["office_recovery"]["external_resources"]["target_hosts"], ["127.0.0.1"])
        convert.assert_not_called()

    def test_new_variant_shared_text_link_and_style_or_object_reference_is_blocked(self):
        # A valid text link cannot bless the same ID used by any other element,
        # including resource references with a misleading relationship Type.
        for extra in ('<w:object r:id="rId9"/>', '<w:style r:link="rId9"/>', '<w:r unknown="rId9"/>'):
            with self.subTest(extra=extra):
                package(self.path, {"word/document.xml": text_hyperlink(extra=extra),
                                    "word/_rels/document.xml.rels": relation("hyperlink")})
                self.assertTrue(inspect_external_resources(self.path)["blocked"])

    def test_picture_inside_hyperlink_is_not_pure_text_even_without_another_id(self):
        xml = text_hyperlink().replace('<w:t>Click text</w:t>', '<w:drawing/>')
        package(self.path, {"word/document.xml": xml, "word/_rels/document.xml.rels": relation("hyperlink")})
        self.assertTrue(inspect_external_resources(self.path)["blocked"])

    def test_resource_references_in_nonstandard_source_parts_are_checked(self):
        for part in ("word/shapes/picture.vml", "custom/resource.bin"):
            with self.subTest(part=part):
                source = Path(part)
                rels = source.parent.as_posix() + "/_rels/" + source.name + ".rels"
                package(self.path, {part: f'<object xmlns:r="{OFFICE_REL.rstrip("/")}" r:id="rId9"/>', rels: relation("hyperlink")})
                self.assertTrue(inspect_external_resources(self.path)["blocked"])

    def test_missing_source_missing_id_and_duplicate_ids_never_receive_exemption(self):
        for xml in (relation("hyperlink").replace('Id="rId9"', 'Id=""'),
                    relation("hyperlink").replace('</Relationships>', '<Relationship Id="rId9" Type="other" Target="embedded.bin"/></Relationships>')):
            with self.subTest(xml=xml):
                package(self.path, {"word/document.xml": text_hyperlink(), "word/_rels/document.xml.rels": xml})
                self.assertTrue(inspect_external_resources(self.path)["blocked"])
        package(self.path, {"word/_rels/document.xml.rels": relation("hyperlink")})
        self.assertTrue(inspect_external_resources(self.path)["blocked"])

    def test_review3_nested_field_and_single_backslash_paths_fail_closed(self):
        nested = ('<w:fldChar w:fldCharType="begin"/><w:instrText>INCLUDEPICTURE </w:instrText>'
                  '<w:fldChar w:fldCharType="begin"/><w:instrText>QUOTE "http://127.0.0.1:9/nested.png"</w:instrText>'
                  '<w:fldChar w:fldCharType="end"/><w:fldChar w:fldCharType="end"/>')
        single_slash = r'<w:fldSimple w:instr="INCLUDEPICTURE &quot;\127.0.0.1\share\image.png&quot;"/>'
        for field in (nested, single_slash):
            with self.subTest(field=field):
                package(self.path, {"word/document.xml": f'<w:document xmlns:w="{WORD}">{field}</w:document>'})
                result, convert = run_recovery(self.path)
                self.assertEqual(result.error_code, "office_external_resource_blocked")
                self.assertEqual(result.metadata["office_recovery"]["external_resources"]["field_types"], ["INCLUDEPICTURE"])
                convert.assert_not_called()

    def test_dynamic_relative_or_incomplete_resource_fields_are_not_treated_as_local(self):
        for instruction in ("INCLUDEPICTURE", 'INCLUDETEXT "../remote.docx"', 'INCLUDEPICTURE { IF 1 = 1 "remote" "local" }'):
            with self.subTest(instruction=instruction):
                field = ET.Element("{" + WORD + "}fldSimple", {"{" + WORD + "}instr": instruction})
                package(self.path, {"word/document.xml": ET.tostring(field)})
                self.assertTrue(inspect_external_resources(self.path)["blocked"])
