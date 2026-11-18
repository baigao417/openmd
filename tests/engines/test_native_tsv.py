import pytest
from openmd.engines.native import NativeEngine
from openmd.engines.base import EngineLimits

@pytest.mark.parametrize('case', ['positive', 'broken', 'encrypted', 'empty', 'oversize'])
def test_native_tsv(tmp_path, case):
    path = tmp_path / 'source.tsv'
    data = 'name\tvalue\nOriginal\t7\n'
    if case == 'broken':
        data = '\x00\ufffd'
    elif case == 'encrypted':
        data = '-----BEGIN PGP MESSAGE-----\nENCRYPTED'
    elif case == 'empty':
        data = ''
    path.write_text(data, encoding='utf-8')
    result = NativeEngine('tsv').convert(path, EngineLimits(max_file_bytes=1) if case == 'oversize' else None)
    if case == 'positive':
        assert result.has_usable_text
        assert '| Original | 7 |' in result.text
        assert result.backend == 'openmd.tsv'
    else:
        assert result.status == 'failed'
