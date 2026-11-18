"""Positive conversion and missing-engine contracts for each optional format.

External tool I/O is stubbed here; real availability belongs to CLI receipts.
"""
from pathlib import Path
from types import SimpleNamespace

import pytest

from openmd.engines.base import success
from openmd.engines.markitdown import MarkItDownEngine
from openmd.engines.tools import CalibreEngine, DefuddleEngine, PandocEngine, SofficeEngine

from test_signatures import SAMPLES, _container


def source(tmp_path, suffix):
    path = tmp_path / ('source.' + suffix)
    path.write_bytes(SAMPLES[suffix])
    return path


@pytest.mark.parametrize('suffix', ['epub', 'rtf', 'odt'])
def test_pandoc_positive_preserves_chapter_order(tmp_path, monkeypatch, suffix):
    path = source(tmp_path, suffix)
    monkeypatch.setattr('openmd.engines.tools.executable', lambda tool: 'pandoc')
    def run(argv, directory, limits):
        assert '--sandbox' in argv and '--from=' + suffix in argv
        assert '--to=gfm' in argv and '--wrap=none' in argv
        return '# First chapter\n\nOriginal words.\n\n# Second chapter\n\nMore original words.\n'
    monkeypatch.setattr('openmd.engines.tools.run_command', run)
    result = PandocEngine().convert(path)
    assert result.status == 'verified'
    assert result.text.index('First chapter') < result.text.index('Second chapter')


@pytest.mark.parametrize('suffix,target', [('doc', 'docx'), ('xls', 'xlsx'), ('ppt', 'pptx'), ('rtf', 'docx'), ('odt', 'docx'), ('odp', 'pptx'), ('ods', 'xlsx')])
def test_soffice_positive_uses_private_profile_and_matching_ooxml(tmp_path, monkeypatch, suffix, target):
    path = source(tmp_path, suffix)
    monkeypatch.setattr('openmd.engines.tools.executable', lambda tool: 'soffice')
    def run(argv, directory, limits):
        assert '--headless' in argv and '--norestore' in argv
        assert argv[argv.index('--convert-to') + 1] == target
        assert '-env:UserInstallation=' + (directory / 'profile').as_uri() in argv
        security = (directory / 'profile/user/registrymodifications.xcu').read_text()
        assert 'MacroSecurityLevel' in security and 'UpdateDocMode' in security
        (directory / ('source.' + target)).write_bytes(b'OOXML test boundary')
        return ''
    monkeypatch.setattr('openmd.engines.tools.run_command', run)
    def convert(self, output, limits):
        assert output.suffix == '.' + target and output.is_file()
        return success('pdf.markitdown', output, '# Original source\n\nSource body.')
    monkeypatch.setattr(MarkItDownEngine, 'convert', convert)
    result = SofficeEngine().convert(path)
    assert result.status == 'verified'
    assert result.metadata['source_format'] == suffix
    assert result.metadata['conversion_target'] == target


@pytest.mark.parametrize('suffix', ['doc', 'xls', 'ppt', 'rtf', 'odt', 'odp', 'ods'])
def test_soffice_negative_reports_dependency_not_fake_text(tmp_path, monkeypatch, suffix):
    monkeypatch.setattr('openmd.engines.tools.executable', lambda tool: None)
    result = SofficeEngine().convert(source(tmp_path, suffix))
    assert result.error_code == 'engine_unavailable'
    assert 'LibreOffice' in result.error_summary
    assert not result.text


@pytest.mark.parametrize('suffix', ['mobi', 'azw3', 'fb2'])
def test_calibre_positive_converts_to_epub_before_next_engine(tmp_path, monkeypatch, suffix):
    path = source(tmp_path, suffix)
    monkeypatch.setattr('openmd.engines.tools.executable', lambda tool: 'ebook-convert')
    def run(argv, directory, limits):
        assert argv[1] == str(path.resolve())
        Path(argv[2]).write_bytes(_container('application/epub+zip', 'META-INF/container.xml'))
        return ''
    monkeypatch.setattr('openmd.engines.tools.run_command', run)
    def next_engine(epub, limits):
        assert epub.suffix == '.epub' and epub.is_file()
        return success('pandoc', epub, '# Original ebook\n\nSource chapter.')
    monkeypatch.setattr('openmd.engines.registry.convert_document', next_engine)
    result = CalibreEngine().convert(path)
    assert result.status == 'verified'
    assert result.metadata['source_format'] == suffix
    assert result.metadata['converted_by'] == 'calibre'


@pytest.mark.parametrize('suffix', ['csv', 'tsv', 'json', 'xml', 'ipynb', 'msg'])
def test_markitdown_positive_document_only_formats(tmp_path, monkeypatch, suffix):
    monkeypatch.setattr(MarkItDownEngine, 'available', lambda self: {'ok': True, 'reason': 'test', 'version': 'test'})
    monkeypatch.setattr(MarkItDownEngine, '_convert_text', lambda *a: '# Original document\n\n## Content\n\nOriginal source words.')
    result = MarkItDownEngine().convert(source(tmp_path, suffix))
    assert result.status == 'verified'
    assert result.metadata['source_format'] == suffix
    assert 'Original source words.' in result.text


@pytest.mark.parametrize('suffix', ['html', 'htm'])
def test_defuddle_positive_uses_library_on_local_file(tmp_path, monkeypatch, suffix):
    engine = DefuddleEngine()
    path = source(tmp_path, suffix)
    monkeypatch.setattr(engine, '_launcher', lambda: SimpleNamespace(entry_path=tmp_path / 'dist/cli.js', argv=('node',)))
    def run(argv, directory, limits):
        assert Path(argv[1]).name == 'local_html.cjs'
        assert argv[-1] == str(path.resolve())
        assert not any(arg.startswith('http') for arg in argv)
        return '{"contentMarkdown":"# Source title\\n\\nOriginal source words."}'
    monkeypatch.setattr('openmd.engines.tools.run_command', run)
    result = engine.convert(path)
    assert result.status == 'verified'
    assert result.metadata['scripts_executed'] is False
    assert result.metadata['external_resources_fetched'] is False
