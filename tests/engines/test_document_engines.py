"""Document engine boundaries; fixtures contain only authored test content."""
import io
import json
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED

import pytest

from openmd.core.detector import detect_platform


FORMATS = 'html htm epub rtf odt odp ods doc xls ppt csv tsv json xml ipynb msg mobi azw3 fb2 zip'.split()


@pytest.mark.parametrize('suffix', FORMATS)
def test_new_local_formats_are_document_inputs(tmp_path, suffix):
    path = tmp_path / ('source.' + suffix)
    path.write_bytes(b'broken input')
    assert detect_platform(str(path)) == 'pdf'
    assert detect_platform(path.as_uri()) == 'pdf'


def test_local_html_does_not_steal_web_or_qq():
    assert detect_platform('https://example.com/article.html') == 'web_article'
    assert detect_platform('qq-group-chat.html') == 'qq_group'
    assert detect_platform('qq-export.json') == 'qq_group'


@pytest.mark.parametrize('suffix', FORMATS)
def test_corrupt_formats_are_rejected_before_tool_probe(tmp_path, suffix, monkeypatch):
    from openmd.engines.registry import convert_document, engine_chain
    path = tmp_path / ('broken.' + suffix)
    path.write_bytes(b'\x00\xffbroken input')
    for engine in engine_chain(suffix):
        monkeypatch.setattr(type(engine), 'available', lambda self: pytest.fail('invalid file probed engine'))
    result = convert_document(path)
    assert result.status == 'failed'
    assert result.error_code.startswith(('document_invalid', 'archive_invalid'))


@pytest.mark.parametrize('suffix', FORMATS)
def test_format_has_explainable_engine_chain(suffix):
    from openmd.engines.base import DocumentEngine
    from openmd.engines.registry import engine_chain
    chain = engine_chain(suffix)
    assert chain
    assert all(isinstance(engine, DocumentEngine) for engine in chain)
    assert len({engine.id for engine in chain}) == len(chain)


def test_unavailable_ebook_has_install_hint_and_trace(tmp_path, monkeypatch):
    from openmd.engines.registry import convert_document
    from openmd.engines.tools import CalibreEngine
    path = tmp_path / 'book.fb2'
    path.write_text('<FictionBook><body><section><p>Chapter text</p></section></body></FictionBook>')
    monkeypatch.setattr(CalibreEngine, 'available', lambda self: {'ok': False, 'reason': 'Install Calibre (ebook-convert).', 'version': None})
    result = convert_document(path)
    assert result.error_code == 'engine_unavailable'
    assert 'Calibre' in result.error_summary
    assert result.metadata['engine_attempts'][0]['engine'] == 'calibre'


def test_drm_checked_even_when_calibre_missing(tmp_path):
    from openmd.engines.registry import convert_document
    path = tmp_path / 'locked.mobi'
    content = bytearray(150)
    content[60:68] = b'BOOKMOBI'
    content[76:78] = (1).to_bytes(2, 'big')
    content[78:82] = (100).to_bytes(4, 'big')
    content[112:114] = (2).to_bytes(2, 'big')
    path.write_bytes(content)
    assert convert_document(path).error_code == 'ebook_drm_protected'


def test_markitdown_media_and_zip_never_reach_converter(tmp_path, monkeypatch):
    from openmd.engines.markitdown import MarkItDownEngine
    engine = MarkItDownEngine()
    monkeypatch.setattr(engine, 'available', lambda: pytest.fail('media dependency probe'))
    for suffix in ['mp3', 'wav', 'm4a', 'flac', 'mp4', 'webm', 'mkv', 'zip']:
        path = tmp_path / ('media.' + suffix)
        path.write_bytes(b'nonempty')
        assert not engine.can_handle(path, b'nonempty')
        assert engine.convert(path).error_code == 'document_format_unsupported'
    source = Path(__file__).parents[2] / 'src/openmd/engines/markitdown.py'
    assert 'enable_plugins=True' not in source.read_text()


def test_json_content_positive_and_table_coverage(tmp_path, monkeypatch):
    from openmd.engines.base import EngineLimits
    from openmd.engines.markitdown import MarkItDownEngine
    path = tmp_path / 'table.tsv'
    path.write_text('name\tvalue\n' + ''.join(f'row{i}\t{i}\n' for i in range(20)))
    # TSV is passed to the actual document-only converter as a CSV stream.
    monkeypatch.setattr(MarkItDownEngine, '_convert_text', lambda self, path, limits: 'name | value\nrow0 | 0')
    monkeypatch.setattr(MarkItDownEngine, 'available', lambda self: {'ok': True, 'reason': 'test', 'version': 'test'})
    result = MarkItDownEngine().convert(path, EngineLimits(max_table_rows=5))
    assert result.status == 'partial'
    assert result.metadata['coverage']['rows_total'] == 20
    assert result.metadata['coverage']['rows_included'] == 5


def test_registry_fallback_records_unavailable_predecessor(tmp_path, monkeypatch):
    from openmd.engines.registry import convert_document
    from openmd.engines.tools import DefuddleEngine
    from openmd.engines.markitdown import MarkItDownEngine
    from openmd.core.models import TranscriptResult
    path = tmp_path / 'article.html'
    path.write_text('<html><body><article><h1>Source title</h1><p>Source body.</p></article></body></html>')
    monkeypatch.setattr(DefuddleEngine, 'available', lambda self: {'ok': False, 'reason': 'not installed', 'version': None})
    monkeypatch.setattr(MarkItDownEngine, 'available', lambda self: {'ok': True, 'reason': 'installed', 'version': '1'})
    monkeypatch.setattr(MarkItDownEngine, 'convert', lambda *a: TranscriptResult(text='# Source title\n\nSource body.', status='verified', source_level='native_text', backend='pdf.markitdown'))
    result = convert_document(path)
    assert result.metadata['selected_engine'] == 'markitdown'
    assert [entry['outcome'] for entry in result.metadata['engine_attempts']] == ['unavailable', 'selected']


def _zip(path, members, compression=0):
    with ZipFile(path, 'w', compression=compression) as archive:
        for name, content in members:
            archive.writestr(name, content)
    return path


def test_zip_routes_every_entry_and_gates_media(tmp_path, monkeypatch):
    from dataclasses import asdict
    from openmd.engines.registry import convert_document
    from openmd.engines.markitdown import MarkItDownEngine
    from openmd.core.models import TranscriptResult
    monkeypatch.setattr(MarkItDownEngine, 'available', lambda self: {'ok': True, 'reason': 'test', 'version': None})
    monkeypatch.setattr(MarkItDownEngine, 'convert', lambda *a: TranscriptResult(text='Source body text.', status='verified', source_level='native_text', backend='pdf.markitdown'))
    monkeypatch.setattr('openmd.engines.worker.convert_in_worker', lambda *a, **kw: asdict(TranscriptResult(text='Source body text.', status='verified', source_level='native_text', backend='pdf.markitdown')))
    path = _zip(tmp_path / 'mixed.zip', [('data.csv', 'name,value\na,1'), ('audio.mp3', b'ID3'), ('video.mp4', b'ftyp'), ('other.unknown', 'unknown')])
    result = convert_document(path)
    assert result.status == 'partial'
    entries = result.metadata['archive_entries']
    assert [entry['platform'] for entry in entries] == ['pdf', 'generic_media', 'generic_media', 'web_article']
    assert [entry['error_code'] for entry in entries] == [None, 'asr_required', 'asr_required', 'archive_entry_unsupported']
    assert 'audio.mp3' in result.text and 'asr_required' in result.text
    assert len(result.components) == 4


@pytest.mark.parametrize('name', ['../escape.csv', '/abs.csv', 'C:/escape.csv', 'a/../../escape.csv', 'a\\..\\..\\escape.csv', 'a:stream.csv', 'CON.csv'])
def test_zip_rejects_path_traversal_before_extract(tmp_path, name):
    from openmd.engines.registry import convert_document
    result = convert_document(_zip(tmp_path / 'bad.zip', [(name, 'a,b\n1,2')]))
    assert result.error_code == 'archive_unsafe_path'
    assert not (tmp_path / 'escape.csv').exists()


@pytest.mark.parametrize('limit, value, code', [
    ('max_archive_files', 1, 'archive_file_limit'),
    ('max_file_bytes', 3, 'archive_file_size_limit'),
    ('max_archive_bytes', 5, 'archive_size_limit'),
    ('max_compression_ratio', 2, 'archive_compression_ratio'),
    ('timeout_s', 0, 'document_timeout'),
])
def test_zip_all_resource_limits(tmp_path, limit, value, code):
    from openmd.engines.base import EngineLimits
    from openmd.engines.registry import convert_document
    path = _zip(tmp_path / 'limit.zip', [('a.csv', 'a,b\n' * 50), ('b.csv', 'a,b\n' * 50)], ZIP_DEFLATED)
    assert convert_document(path, EngineLimits(**{limit: value})).error_code == code


def test_zip_depth_is_cumulative_and_partial(tmp_path):
    from openmd.engines.registry import convert_document
    inner = io.BytesIO()
    with ZipFile(inner, 'w') as archive:
        archive.writestr('deeper.zip', b'PK\x05\x06' + b'\x00' * 18)
    result = convert_document(_zip(tmp_path / 'outer.zip', [('inner.zip', inner.getvalue())]))
    assert result.status == 'partial'
    nested = result.metadata['archive_entries'][0]
    assert nested['entries'][0]['error_code'] == 'archive_depth_limit'


def test_container_sniff_rejects_renamed_document(tmp_path):
    from openmd.engines.registry import convert_document
    path = _zip(tmp_path / 'wrong.epub', [('data.csv', 'a,b\n1,2')])
    assert convert_document(path).error_code == 'document_invalid_container'


def test_xml_external_entities_are_rejected(tmp_path):
    from openmd.engines.registry import convert_document
    path = tmp_path / 'unsafe.xml'
    path.write_text('<!DOCTYPE x [<!ENTITY src SYSTEM "file:///secret">]><x>&src;</x>')
    assert convert_document(path).error_code == 'document_invalid_content'


@pytest.mark.parametrize('signature', [b'BOOKMOBI', b'TEXtREAd'])
def test_both_palmdoc_and_mobi_drm_are_refused(tmp_path, signature):
    from openmd.engines.registry import convert_document
    content = bytearray(150)
    content[60:68] = signature
    content[76:78] = (1).to_bytes(2, 'big')
    content[78:82] = (100).to_bytes(4, 'big')
    content[112:114] = (1).to_bytes(2, 'big')
    path = tmp_path / 'locked.mobi'
    path.write_bytes(content)
    assert convert_document(path).error_code == 'ebook_drm_protected'


def test_zip_rejects_symlink_and_duplicate_case_paths(tmp_path):
    from zipfile import ZipInfo
    from openmd.engines.registry import convert_document
    path = tmp_path / 'symlink.zip'
    info = ZipInfo('link.csv')
    info.create_system = 3
    info.external_attr = (0o120777 << 16)
    with ZipFile(path, 'w') as archive:
        archive.writestr(info, '../outside')
    assert convert_document(path).error_code == 'archive_unsafe_path'
    duplicate = _zip(tmp_path / 'duplicate.zip', [('A.csv', 'a,b\n1,2'), ('a.csv', 'a,b\n1,2')])
    assert convert_document(duplicate).error_code == 'archive_duplicate_path'


def test_nested_zip_shares_total_file_and_byte_budget(tmp_path):
    from openmd.engines.registry import convert_document
    from openmd.engines.base import EngineLimits
    inner = io.BytesIO()
    with ZipFile(inner, 'w') as archive:
        archive.writestr('data.csv', 'a,b\n1,2')
    outer = _zip(tmp_path / 'outer.zip', [('inner.zip', inner.getvalue())])
    result = convert_document(outer, EngineLimits(max_archive_files=1))
    assert result.status == 'partial'
    assert result.metadata['archive_entries'][0]['error_code'] == 'archive_file_limit'


def test_document_process_deadline_is_enforced(tmp_path):
    import sys
    import time
    from openmd.engines.base import EngineError, EngineLimits
    from openmd.engines.tools import run_command
    started = time.monotonic()
    with pytest.raises(EngineError, match='document_timeout'):
        run_command([sys.executable, '-c', 'import time; time.sleep(30)'], tmp_path, EngineLimits(timeout_s=0.1))
    assert time.monotonic() - started < 10


def test_empty_msg_scaffolding_is_not_content(tmp_path, monkeypatch):
    from openmd.engines.markitdown import MarkItDownEngine
    path = tmp_path / 'empty.msg'
    path.write_bytes(b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1' + b'\x00' * 512)
    monkeypatch.setattr(MarkItDownEngine, 'available', lambda self: {'ok': True, 'reason': 'test', 'version': None})
    monkeypatch.setattr(MarkItDownEngine, '_convert_text', lambda *a: '# Email Message\n\n## Content')
    assert MarkItDownEngine().convert(path).error_code == 'document_empty_text'


def test_legacy_markitdown_rejects_all_new_media_and_zip(tmp_path, monkeypatch):
    from openmd.core.detector import MEDIA_EXTENSIONS
    from openmd.adapters.pdf.backends.markitdown import MarkItDownPDFParser
    monkeypatch.setattr(MarkItDownPDFParser, 'is_available', lambda self: pytest.fail('media dependency probe'))
    for suffix in (*MEDIA_EXTENSIONS, '.ogg', '.avi', '.zip', '.csv', '.html', '.epub'):
        path = tmp_path / ('input' + suffix)
        path.write_bytes(b'not a document')
        assert MarkItDownPDFParser().parse(str(path), title='input').error_code == 'document_format_unsupported'


def test_media_renamed_to_pdf_is_rejected_before_dependency_probe(tmp_path, monkeypatch):
    from openmd.adapters.pdf.backends.markitdown import MarkItDownPDFParser
    path = tmp_path / 'disguised.pdf'
    path.write_bytes(b'ID3 audio content renamed as PDF')
    monkeypatch.setattr(MarkItDownPDFParser, 'is_available', lambda self: pytest.fail('renamed media dependency probe'))
    assert MarkItDownPDFParser().parse(str(path), title='source').error_code == 'document_invalid_content'


def test_document_folder_does_not_absorb_qq_exports(tmp_path):
    from openmd.adapters.pdf.adapter import PdfDocumentAdapter
    (tmp_path / 'article.html').write_text('<html><p>Original article.</p></html>')
    (tmp_path / 'qq-export.html').write_text('<html><p>QQ export.</p></html>')
    items = PdfDocumentAdapter().resolve_items(str(tmp_path))
    assert [Path(item.raw_meta['local_path']).name for item in items] == ['article.html']
