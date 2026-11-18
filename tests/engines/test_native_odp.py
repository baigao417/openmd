import pytest
from structured_fixtures import authored
from openmd.engines.native import NativeEngine
from openmd.engines.base import EngineLimits

@pytest.mark.parametrize('case', ['positive', 'broken', 'encrypted', 'empty', 'oversize'])
def test_native_odp(tmp_path, case):
    path = authored(tmp_path / 'source.odp', empty=case=='empty', encrypted=case=='encrypted')
    if case == 'broken':
        path.write_bytes(b'\x00\xffbroken')
    result = NativeEngine('odp').convert(path, EngineLimits(max_file_bytes=1) if case=='oversize' else None)
    if case == 'positive':
        assert result.has_usable_text
        assert 'Original' in result.text and '中文' in result.text
        if 'Second original' in result.text:
            assert result.text.index('Original') < result.text.index('Second original')
        if 'odp' in {'html','htm','rtf','ods'}:
            assert result.metadata['tables']
        if 'odp' == 'odp':
            assert 'Speaker original' in result.text
    else:
        assert result.status == 'failed'
