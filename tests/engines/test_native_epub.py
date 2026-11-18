import pytest
from structured_fixtures import authored
from openmd.engines.native import NativeEngine
from openmd.engines.base import EngineLimits


def test_document_tables_keep_inline_links(tmp_path):
    from openmd.engines.native_common import Document, html_markdown
    d = Document(tmp_path / 'source.epub', EngineLimits())
    value = html_markdown(d, '<table><tr><td><a href="chapter.xhtml">Original link</a> <code>code</code></td></tr></table>')
    assert '[Original link](chapter.xhtml)' in value
    assert '`code`' in value

@pytest.mark.parametrize('case', ['positive', 'broken', 'encrypted', 'empty', 'oversize'])
def test_native_epub(tmp_path, case):
    path = authored(tmp_path / 'source.epub', empty=case=='empty', encrypted=case=='encrypted')
    if case == 'broken':
        path.write_bytes(b'\x00\xffbroken')
    result = NativeEngine('epub').convert(path, EngineLimits(max_file_bytes=1) if case=='oversize' else None)
    if case == 'positive':
        assert result.has_usable_text
        assert 'Original' in result.text and '中文' in result.text
        if 'Second original' in result.text:
            assert result.text.index('Original') < result.text.index('Second original')
        if 'epub' in {'html','htm','rtf','ods'}:
            assert result.metadata['tables']
        if 'epub' == 'odp':
            assert 'Speaker original' in result.text
    else:
        assert result.status == 'failed'
