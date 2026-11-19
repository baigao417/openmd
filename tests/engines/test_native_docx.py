from pathlib import Path
from zipfile import ZipFile

import pytest


def document(path, body):
    with ZipFile(path, 'w') as z:
        z.writestr('[Content_Types].xml', '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="xml" ContentType="application/xml"/></Types>')
        z.writestr('word/document.xml', '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>' + body + '</w:body></w:document>')
    return path


def test_docx_native_paragraph_and_table(tmp_path):
    pytest.importorskip('mammoth')
    from openmd.engines.native import NativeEngine
    source = document(tmp_path / 'authored.docx', '<w:p><w:r><w:t>Original 中文</w:t></w:r></w:p><w:tbl><w:tr><w:tc><w:p><w:r><w:t>Cell one</w:t></w:r></w:p></w:tc></w:tr></w:tbl>')
    result = NativeEngine('docx').convert(source)
    assert result.status == 'verified'
    assert 'Original 中文' in result.text and '| Cell one |' in result.text
    assert result.metadata['tables'][0]['rows'] == 1


@pytest.mark.parametrize('case', ['broken', 'empty', 'oversize', 'encrypted'])
def test_docx_native_rejects_invalid(tmp_path, case):
    from openmd.engines.native import NativeEngine
    from openmd.engines.base import EngineLimits
    source = tmp_path / 'source.docx'
    source.write_bytes(b'broken' if case != 'encrypted' else bytes.fromhex('d0cf11e0a1b11ae1') + b'EncryptedPackage')
    if case == 'empty':
        document(source, '')
    result = NativeEngine('docx').convert(source, EngineLimits(max_file_bytes=1) if case == 'oversize' else None)
    assert result.status == 'failed'
    assert result.error_code in {'document_invalid_content', 'document_invalid_container', 'document_empty_text', 'document_size_limit', 'document_encrypted', 'engine_unavailable'}


def test_embedded_images_keep_original_bytes_and_honest_gap(tmp_path):
    import base64
    import hashlib
    from openmd.engines.native_common import Document
    from openmd.engines.base import EngineLimits
    original = b'original image bytes'
    d = Document(tmp_path / 'source.docx', EngineLimits())
    d.add('Original body')
    d.image(original, 'word/media/image1.png')
    result = d.result()
    assert result.status == 'partial'
    assert base64.b64encode(original).decode() in result.text
    assert result.metadata['images'][0]['sha256'] == hashlib.sha256(original).hexdigest()
    assert result.components[-1].error_code == 'embedded_image_ocr_unavailable'


def test_embedded_images_use_local_ocr_only_when_enabled(tmp_path, monkeypatch):
    import io
    pytest.importorskip('PIL')
    from PIL import Image
    from openmd.engines.native_common import Document
    from openmd.engines.base import EngineLimits, success
    data = io.BytesIO()
    Image.new('RGB', (8, 8)).save(data, format='PNG')
    monkeypatch.setattr('openmd.ocr.pipeline.LocalOCRBackend.recognize_image', lambda self, image, **kw: success('ocr.test', tmp_path / 'image.png', 'Image original words'))
    d = Document(tmp_path / 'source.docx', EngineLimits(allow_ocr=True))
    d.add('Original body')
    d.image(data.getvalue(), 'word/media/image1.png')
    assert 'Image original words' in d.result().text
    assert d.result().status == 'verified'


def test_individual_bad_part_keeps_readable_document(tmp_path):
    from openmd.engines.native_common import Document
    from openmd.engines.base import EngineLimits
    d = Document(tmp_path / 'source.docx', EngineLimits())
    d.section('first', lambda: d.add('Readable original words'))
    d.section('broken', lambda: int('bad'))
    assert d.result().status == 'partial'
    assert 'Readable original words' in d.result().text
    assert d.result().components[1].error_code == 'document_part_failed'
