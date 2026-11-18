import pytest
from structured_fixtures import authored
from openmd.engines.native import NativeEngine
from openmd.engines.base import EngineLimits


@pytest.mark.parametrize('source,expected', [
    (r'{\rtf1\ansi \uc1\u-10179?\u-8704?}', '😀'),
    (r'{\rtf1\ansi\ansicpg936 \'d6\'d0\'ce\'c4}', '中文'),
    (r'{\rtf1\ansi \{Original\}\\text}', '{Original}\\text'),
])
def test_rtf_unicode_and_literals(tmp_path, source, expected):
    path = tmp_path / 'source.rtf'
    path.write_text(source)
    assert expected in NativeEngine('rtf').convert(path).text

@pytest.mark.parametrize('case', ['positive', 'broken', 'encrypted', 'empty', 'oversize'])
def test_native_rtf(tmp_path, case):
    path = authored(tmp_path / 'source.rtf', empty=case=='empty', encrypted=case=='encrypted')
    if case == 'broken':
        path.write_bytes(b'\x00\xffbroken')
    result = NativeEngine('rtf').convert(path, EngineLimits(max_file_bytes=1) if case=='oversize' else None)
    if case == 'positive':
        assert result.has_usable_text
        assert 'Original' in result.text and '中文' in result.text
        if 'Second original' in result.text:
            assert result.text.index('Original') < result.text.index('Second original')
        if 'rtf' in {'html','htm','rtf','ods'}:
            assert result.metadata['tables']
        if 'rtf' == 'odp':
            assert 'Speaker original' in result.text
    else:
        assert result.status == 'failed'
