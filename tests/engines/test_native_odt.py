import pytest
from structured_fixtures import authored
from openmd.engines.native import NativeEngine
from openmd.engines.base import EngineLimits


def test_odt_numbered_list_keeps_numbers_and_continuation(tmp_path):
    from zipfile import ZipFile
    path = authored(tmp_path / 'source.odt')
    with ZipFile(path) as z:
        files = {n: z.read(n) for n in z.namelist()}
    data = files['content.xml'].decode().replace('<office:body>', '<text:list-style text:name="Numbered"><text:list-level-style-number/></text:list-style><office:body>')
    data = data.replace('</office:text>', '<text:list text:style-name="Numbered"><text:list-item><text:p>First item</text:p><text:p>Continuation original</text:p></text:list-item><text:list-item><text:p>Second item</text:p></text:list-item></text:list></office:text>')
    files['content.xml'] = data.encode()
    with ZipFile(path, 'w') as z:
        for n, data in files.items():
            z.writestr(n, data)
    result = NativeEngine('odt').convert(path)
    assert '1. First item' in result.text and '2. Second item' in result.text
    assert '- Continuation' not in result.text

@pytest.mark.parametrize('case', ['positive', 'broken', 'encrypted', 'empty', 'oversize'])
def test_native_odt(tmp_path, case):
    path = authored(tmp_path / 'source.odt', empty=case=='empty', encrypted=case=='encrypted')
    if case == 'broken':
        path.write_bytes(b'\x00\xffbroken')
    result = NativeEngine('odt').convert(path, EngineLimits(max_file_bytes=1) if case=='oversize' else None)
    if case == 'positive':
        assert result.has_usable_text
        assert 'Original' in result.text and '中文' in result.text
        if 'Second original' in result.text:
            assert result.text.index('Original') < result.text.index('Second original')
        if 'odt' in {'html','htm','rtf','ods'}:
            assert result.metadata['tables']
        if 'odt' == 'odp':
            assert 'Speaker original' in result.text
    else:
        assert result.status == 'failed'
