"""One positive and negative content signature for every added format."""
import io
from zipfile import ZipFile

import pytest

from openmd.engines.registry import engine_chain
from openmd.engines.sniff import validate_document
from openmd.engines.base import EngineLimits


def _container(mime, part):
    data = io.BytesIO()
    with ZipFile(data, 'w') as archive:
        archive.writestr('mimetype', mime)
        archive.writestr(part, '<root>Source text</root>')
    return data.getvalue()


def _mobi():
    data = bytearray(150)
    data[60:68] = b'BOOKMOBI'
    data[76:78] = (1).to_bytes(2, 'big')
    data[78:82] = (100).to_bytes(4, 'big')
    return data


SAMPLES = {
    'html': b'<!doctype html><html><body><article>Real source text</article></body></html>',
    'htm': b'<html><p>Source text</p></html>',
    'epub': _container('application/epub+zip', 'META-INF/container.xml'),
    'rtf': b'{\\rtf1\\ansi Source text}',
    'odt': _container('application/vnd.oasis.opendocument.text', 'content.xml'),
    'odp': _container('application/vnd.oasis.opendocument.presentation', 'content.xml'),
    'ods': _container('application/vnd.oasis.opendocument.spreadsheet', 'content.xml'),
    **{ext: b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1' + b'\0' * 512 for ext in ('doc', 'xls', 'ppt', 'msg')},
    'csv': b'name,value\napple,1', 'tsv': b'name\tvalue\napple\t1',
    'json': b'{"source":"text"}', 'xml': b'<?xml version="1.0"?><doc>Source text</doc>',
    'ipynb': b'{"nbformat":4,"cells":[{"cell_type":"markdown","source":["Source text"]}]}',
    'mobi': _mobi(), 'azw3': _mobi(), 'fb2': b'<FictionBook><body><section><p>Source text</p></section></body></FictionBook>',
    'zip': b'PK\x05\x06' + b'\0' * 18,
}


@pytest.mark.parametrize('suffix', SAMPLES)
def test_each_format_matches_content_and_rejects_binary_garbage(tmp_path, suffix):
    path = tmp_path / ('source.' + suffix)
    path.write_bytes(SAMPLES[suffix])
    sniff = validate_document(path, EngineLimits())
    assert any(e.can_handle(path, sniff) for e in engine_chain(suffix))
    assert not any(e.can_handle(path, b'\0\xffbroken') for e in engine_chain(suffix))


def test_format_capabilities_are_generated_from_declarations():
    from pathlib import Path
    from openmd.engines.formats import render_formats
    from openmd.routing.generate import declarations
    root = Path(__file__).parents[2]
    assert (root / 'docs/subsystems/document-formats.generated.md').read_text(encoding='utf-8') == render_formats()
    assert set(SAMPLES) <= {s for f in declarations()['document_formats'] for s in f['extensions']}


@pytest.mark.parametrize('suffix, prefix, ending', [('csv', 'field\n', ''), ('json', '{"text":"', '"}')])
def test_utf8_sniff_boundary_does_not_reject_valid_chinese(tmp_path, suffix, prefix, ending):
    path = tmp_path / ('chinese.' + suffix)
    path.write_text(prefix + '中' * 3000 + ending, encoding='utf-8')
    sniff = validate_document(path, EngineLimits())
    assert any(engine.can_handle(path, sniff) for engine in engine_chain(suffix))
