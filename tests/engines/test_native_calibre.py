import pytest
from openmd.engines.tools import CalibreEngine
from openmd.engines.base import success


@pytest.mark.parametrize('suffix', ['mobi', 'azw3'])
def test_calibre_removes_generated_wrapper_and_keeps_chapters(tmp_path, monkeypatch, suffix):
    from test_signatures import SAMPLES
    source = tmp_path / ('Original book.' + suffix)
    source.write_bytes(SAMPLES[suffix])
    monkeypatch.setattr('openmd.engines.tools.executable', lambda tool: 'ebook-convert')
    def run(argv, root, limits):
        from pathlib import Path
        Path(argv[2]).write_bytes(b'epub')
        return ''
    monkeypatch.setattr('openmd.engines.tools.run_command', run)
    monkeypatch.setattr('openmd.engines.registry.convert_document', lambda output, limits: success('openmd.epub', output, '# converted\n\n## First chapter\n\nOriginal words.\n\n## Second chapter\n\nMore original words.'))
    result = CalibreEngine().convert(source)
    assert '# converted' not in result.text
    assert result.text.index('First chapter') < result.text.index('Second chapter')


def test_epub_generated_cover_and_link_toc_removed(tmp_path):
    from zipfile import ZipFile
    from structured_fixtures import authored
    from openmd.engines.native import NativeEngine
    source = authored(tmp_path / 'source.epub')
    with ZipFile(source) as z:
        files = {n:z.read(n) for n in z.namelist()}
    files['OPS/book.opf'] = b'<package><manifest><item id="cover" href="cover.xhtml"/><item id="toc" href="toc.xhtml"/><item id="a" href="first.xhtml"/><item id="b" href="second.xhtml"/></manifest><spine><itemref idref="cover"/><itemref idref="toc"/><itemref idref="a"/><itemref idref="b"/></spine><guide><reference type="cover" href="cover.xhtml"/></guide></package>'
    files['OPS/cover.xhtml'] = b'<html><body><h1>Cover junk</h1><img src="cover.png"/></body></html>'
    files['OPS/toc.xhtml'] = b'<html><body><h1>Contents</h1><ul><li><a href="first.xhtml">First</a></li><li><a href="second.xhtml">Second</a></li></ul></body></html>'
    with ZipFile(source,'w') as z:
        for n,data in files.items():
            z.writestr(n,data)
    result = NativeEngine('epub').convert(source)
    assert 'Cover junk' not in result.text and 'Contents' not in result.text
    assert result.metadata['parts_order'] == ['OPS/first.xhtml','OPS/second.xhtml']

