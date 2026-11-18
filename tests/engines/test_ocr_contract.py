import io
from pathlib import Path

import pytest


def test_ocr_engine_uses_document_contract_and_does_not_download(tmp_path, monkeypatch):
    from openmd.engines.base import DocumentEngine, EngineLimits
    from openmd.ocr.engine import OcrEngine
    from openmd.core.models import TranscriptResult
    from PIL import Image
    path = tmp_path / 'page.png'
    Image.new('RGB', (40, 40), 'white').save(path)
    engine = OcrEngine('ocr.chain')
    assert isinstance(engine, DocumentEngine)
    assert engine.can_handle(path, path.read_bytes()[:32])
    assert not engine.can_handle(tmp_path / 'fake.png', b'not an image')
    monkeypatch.setattr('openmd.ocr.pipeline.LocalOCRBackend.recognize_image', lambda *a, **kw: TranscriptResult(text='Original', status='verified', source_level='ocr', backend='ocr.chain'))
    monkeypatch.setattr(engine, 'available', lambda: {'ok': True, 'reason': 'test', 'version': 'test'})
    value = engine.convert(path, EngineLimits())
    assert value.source_level == 'ocr'
    assert value.metadata['ocr_blocks'][0]['text'] == 'Original'
    assert value.metadata['ocr_engine']['id'] == 'ocr.chain'


def test_office_ocr_has_provenance_and_receipt(tmp_path, monkeypatch):
    from openmd.engines.native_common import Document
    from openmd.engines.base import EngineLimits
    from openmd.core.models import TranscriptResult
    from PIL import Image
    data = io.BytesIO()
    Image.new('RGB', (20, 20)).save(data, format='PNG')
    monkeypatch.setattr('openmd.ocr.pipeline.LocalOCRBackend.recognize_image', lambda *a, **kw: TranscriptResult(text='Original image words', status='verified', source_level='ocr', backend='ocr.chain'))
    d = Document(tmp_path / 'source.docx', EngineLimits(allow_ocr=True))
    d.add('Before image')
    d.image(data.getvalue(), 'word/media/image1.png')
    d.add('After image')
    result = d.result()
    assert '来源：OCR' in result.text
    assert result.text.index('Before image') < result.text.index('Original image words') < result.text.index('After image')
    assert result.metadata['images'][0]['ocr']['backend'] == 'ocr.chain'
    assert any(c.source_level == 'ocr' for c in result.components)


def test_model_plan_never_writes_or_downloads(tmp_path):
    from openmd.runtime.recommendations import install_plan
    root = tmp_path / 'models'
    plan = install_plan('ocr:baseline', root=root)
    assert plan['plan_only'] and not plan['download_started']
    assert plan['downloads'] and all('size_bytes' in item for item in plan['downloads'])
    assert not root.exists()
    with pytest.raises(ValueError):
        install_plan('unknown', root=root)


def test_recommendation_does_not_claim_unmeasured_hardware():
    from openmd.runtime.recommendations import recommend
    result = recommend({'os': 'Linux', 'ram_gib': 64, 'gpus': [{'name': 'Untested', 'vram_mib': 24000}]})
    assert result['validation'] == '未验证'
    assert result['evidence'] == []


def test_models_parser_has_explicit_plan_command():
    from openmd.cli_parser import build_parser
    args = build_parser().parse_args(['models', 'install', 'ocr:baseline'])
    assert args.tier == 'ocr:baseline'


@pytest.mark.parametrize('suffix', ['docx', 'pptx', 'xlsx'])
def test_office_images_stay_in_their_source_section(tmp_path, monkeypatch, suffix):
    from openmd.engines.native import convert_local
    from openmd.engines.base import EngineLimits
    from openmd.core.models import TranscriptResult
    from PIL import Image
    image = tmp_path / 'words.png'
    Image.new('RGB', (20, 20), 'white').save(image)
    path = tmp_path / ('source.' + suffix)
    if suffix == 'docx':
        from zipfile import ZipFile
        pytest.importorskip('mammoth')
        with ZipFile(path, 'w') as z:
            z.writestr('[Content_Types].xml', '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="xml" ContentType="application/xml"/><Default Extension="png" ContentType="image/png"/></Types>')
            z.writestr('word/document.xml', '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture"><w:body><w:p><w:r><w:t>Before image</w:t></w:r></w:p><w:p><w:r><w:drawing><wp:inline><a:graphic><a:graphicData><pic:pic><pic:blipFill><a:blip r:embed="rId1"/></pic:blipFill></pic:pic></a:graphicData></a:graphic></wp:inline></w:drawing></w:r></w:p><w:p><w:r><w:t>After image</w:t></w:r></w:p></w:body></w:document>')
            z.writestr('word/_rels/document.xml.rels', '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/image1.png"/></Relationships>')
            z.write(image, 'word/media/image1.png')
    elif suffix == 'pptx':
        pytest.importorskip('pptx')
        from pptx import Presentation
        from pptx.util import Inches
        p = Presentation()
        first = p.slides.add_slide(p.slide_layouts[5])
        first.shapes.title.text = 'Before image'
        first.shapes.add_picture(str(image), Inches(1), Inches(2))
        second = p.slides.add_slide(p.slide_layouts[5])
        second.shapes.title.text = 'After image'
        p.save(path)
    else:
        pytest.importorskip('openpyxl')
        from openpyxl import Workbook
        from openpyxl.drawing.image import Image as ExcelImage
        book = Workbook()
        book.active['A1'] = 'Before image'
        book.active.add_image(ExcelImage(str(image)), 'B2')
        book.create_sheet('After').append(['After image'])
        book.save(path)
    monkeypatch.setattr('openmd.ocr.pipeline.LocalOCRBackend.recognize_image', lambda *a, **kw: TranscriptResult(text='Image original words', status='verified', source_level='ocr', backend='ocr.chain'))
    result = convert_local(path, EngineLimits(allow_ocr=True))
    assert result.text.index('Before image') < result.text.index('Image original words') < result.text.index('After image')
    assert result.status == 'verified'


@pytest.mark.parametrize('in_table', [False, True])
def test_docx_keeps_literal_image_marker(tmp_path, monkeypatch, in_table):
    from zipfile import ZipFile
    from openmd.engines.native import convert_local
    from openmd.engines.base import EngineLimits
    from openmd.core.models import TranscriptResult
    pytest.importorskip('mammoth')
    path = tmp_path / 'literal.docx'
    from PIL import Image
    data = io.BytesIO()
    Image.new('RGB', (10, 10)).save(data, 'PNG')
    with ZipFile(path, 'w') as z:
        z.writestr('[Content_Types].xml', '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="xml" ContentType="application/xml"/><Default Extension="png" ContentType="image/png"/></Types>')
        picture = '<w:p><w:r><w:pict><v:imagedata xmlns:v="urn:schemas-microsoft-com:vml" r:id="rId1"/></w:pict></w:r></w:p>'
        if in_table:
            picture = '<w:tbl><w:tr><w:tc><w:p><w:r><w:t>Table before</w:t></w:r></w:p>' + picture + '<w:p><w:r><w:t>Table after</w:t></w:r></w:p></w:tc></w:tr></w:tbl>'
        z.writestr('word/document.xml', '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><w:body><w:p><w:r><w:t>OPENMDIMAGE0END is literal source text.</w:t></w:r></w:p>' + picture + '</w:body></w:document>')
        z.writestr('word/_rels/document.xml.rels', '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/image1.png"/></Relationships>')
        z.writestr('word/media/image1.png', data.getvalue())
    monkeypatch.setattr('openmd.ocr.pipeline.LocalOCRBackend.recognize_image', lambda *a, **kw: TranscriptResult(text='OCR words', status='verified', source_level='ocr', backend='ocr.chain'))
    result = convert_local(path, EngineLimits(allow_ocr=True))
    assert 'OPENMDIMAGE0END is literal source text.' in result.text
    assert result.text.count('OCR words') == 1
    if in_table:
        assert result.text.index('Table before') < result.text.index('OCR words') < result.text.index('Table after')


def test_xlsx_images_resolve_workbook_relationships(tmp_path, monkeypatch):
    from zipfile import ZipFile
    from openpyxl import Workbook
    from openpyxl.drawing.image import Image as ExcelImage
    from PIL import Image
    from openmd.engines.native import convert_local
    from openmd.engines.base import EngineLimits
    from openmd.core.models import TranscriptResult
    path = tmp_path / 'renamed.xlsx'
    image = tmp_path / 'image.png'
    Image.new('RGB', (10, 10)).save(image)
    book = Workbook()
    book.active['A1'] = 'Before image'
    book.active.add_image(ExcelImage(str(image)), 'B2')
    book.create_sheet('Second')['A1'] = 'After image'
    book.save(path)
    with ZipFile(path) as source:
        parts = {name: source.read(name) for name in source.namelist()}
    parts['xl/worksheets/custom.xml'] = parts.pop('xl/worksheets/sheet1.xml')
    parts['xl/worksheets/_rels/custom.xml.rels'] = parts.pop('xl/worksheets/_rels/sheet1.xml.rels')
    parts['xl/_rels/workbook.xml.rels'] = parts['xl/_rels/workbook.xml.rels'].replace(b'worksheets/sheet1.xml', b'worksheets/custom.xml')
    parts['[Content_Types].xml'] = parts['[Content_Types].xml'].replace(b'worksheets/sheet1.xml', b'worksheets/custom.xml')
    with ZipFile(path, 'w') as target:
        for name, data in parts.items():
            target.writestr(name, data)
    monkeypatch.setattr('openmd.ocr.pipeline.LocalOCRBackend.recognize_image', lambda *a, **kw: TranscriptResult(text='OCR words', status='verified', source_level='ocr', backend='ocr.chain'))
    result = convert_local(path, EngineLimits(allow_ocr=True))
    assert result.text.index('Before image') < result.text.index('OCR words') < result.text.index('After image')


def test_unmeasured_gpu_does_not_inherit_measured_ocr_status():
    from openmd.runtime.recommendations import recommend
    value = recommend({'os': 'Windows', 'ram_gib': 15.62, 'gpus': [{'name': 'Unmeasured GPU', 'vram_mib': 8188}]})
    assert value['validation'] == value['ocr_validation'] == value['asr_validation'] == '未验证'


def test_ocr_page_budget_includes_gpu_lock_wait(monkeypatch):
    from contextlib import contextmanager
    import openmd.ocr.pipeline as pipeline
    clock = [0.0]
    monkeypatch.setattr(pipeline.time, 'monotonic', lambda: clock[0])
    @contextmanager
    def waited_lock(**kwargs):
        clock[0] = 2.0
        yield
    monkeypatch.setattr(pipeline, 'gpu_lock', waited_lock)
    seen = []
    def recognize(self, image, title, page, tile, config):
        seen.append(config['page_seconds'])
        return 'test result'
    monkeypatch.setattr(pipeline.LocalOCRBackend, '_recognize_locked', recognize)
    pipeline.LocalOCRBackend().recognize_image(None, title='test', timeout_s=3)
    assert seen == [1.0]


def test_hardware_recommendation_marks_only_the_measured_machine():
    from openmd.runtime.recommendations import recommend
    value = recommend({'os': 'Windows', 'ram_gib': 15.62, 'gpus': [{'name': 'NVIDIA GeForce RTX 4060 Laptop GPU', 'vram_mib': 8188}]})
    assert value['ocr_validation'] == '实测组件'
    assert value['asr_validation'] == '未验证'
    assert value['evidence'][0]['pages_per_language'] == 6


@pytest.mark.parametrize('allowed', [True, False])
def test_legacy_office_route_uses_positioned_image_engine(tmp_path, monkeypatch, allowed):
    from zipfile import ZipFile
    from openmd.adapters.pdf.backends.markitdown import MarkItDownPDFParser
    from openmd.backends.pdf import PDFParserProvider
    from openmd.backends.base import BackendExtractRequest
    from openmd.backends.registry import BackendRuntimeOptions
    from openmd.core.models import ContentItem, TranscriptResult
    path = tmp_path / 'source.docx'
    with ZipFile(path, 'w') as z:
        z.writestr('word/media/image1.png', b'image bytes')
    calls = []
    def convert(path, limits):
        calls.append(limits.allow_ocr)
        return TranscriptResult(text='Positioned OCR', status='verified', source_level='native_text', backend='openmd.docx')
    monkeypatch.setattr('openmd.engines.registry.convert_document', convert)
    monkeypatch.setattr('openmd.adapters.pdf.adapter.PdfDocumentAdapter.extract_text', lambda *a, **kw: TranscriptResult(text='Legacy native text', status='verified', source_level='native_text', backend='pdf.markitdown'))
    provider = PDFParserProvider(MarkItDownPDFParser(), BackendRuntimeOptions(enable_asr_backends=allowed))
    item = ContentItem(platform='pdf', input_type='single', source_type='document', url=str(path), id='test', title='source')
    value = provider.extract(BackendExtractRequest(item=item, backend_chain=('pdf.markitdown',)))
    assert calls == [allowed]
    assert value.backend == 'openmd.docx'
