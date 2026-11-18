import pytest
from structured_fixtures import authored
from openmd.engines.native import NativeEngine
from openmd.engines.base import EngineLimits


def test_fb2_no_empty_or_setext_heading_or_escaped_linebreak(tmp_path):
    path = tmp_path / 'source.fb2'
    path.write_text('<FictionBook><body><title><p/></title><section><title><p>Original heading</p></title><p>First line\nsecond line</p><p><strong/></p></section></body></FictionBook>')
    result = NativeEngine('fb2').convert(path)
    assert result.text.lstrip().startswith('## Original heading')
    assert '\\' + '\n' not in result.text
    assert '\n**\n' not in result.text
    assert '===' not in result.text

@pytest.mark.parametrize('case', ['positive', 'broken', 'encrypted', 'empty', 'oversize'])
def test_native_fb2(tmp_path, case):
    path = authored(tmp_path / 'source.fb2', empty=case=='empty', encrypted=case=='encrypted')
    if case == 'broken':
        path.write_bytes(b'\x00\xffbroken')
    result = NativeEngine('fb2').convert(path, EngineLimits(max_file_bytes=1) if case=='oversize' else None)
    if case == 'positive':
        assert result.has_usable_text
        assert 'Original' in result.text and '中文' in result.text
        if 'Second original' in result.text:
            assert result.text.index('Original') < result.text.index('Second original')
        if 'fb2' in {'html','htm','rtf','ods'}:
            assert result.metadata['tables']
        if 'fb2' == 'odp':
            assert 'Speaker original' in result.text
    else:
        assert result.status == 'failed'
