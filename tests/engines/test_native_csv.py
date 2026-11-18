import pytest
from openmd.engines.native import NativeEngine
from openmd.engines.base import EngineLimits

@pytest.mark.parametrize('case', ['positive', 'broken', 'encrypted', 'empty', 'oversize'])
def test_native_csv(tmp_path, case):
    path = tmp_path / 'source.csv'
    data = 'name,value\nOriginal,7\n'
    if case == 'broken':
        data = '\x00\ufffd'
    elif case == 'encrypted':
        data = '-----BEGIN PGP MESSAGE-----\nENCRYPTED'
    elif case == 'empty':
        data = ''
    path.write_text(data, encoding='utf-8')
    result = NativeEngine('csv').convert(path, EngineLimits(max_file_bytes=1) if case == 'oversize' else None)
    if case == 'positive':
        assert result.has_usable_text
        assert '| Original | 7 |' in result.text
        assert result.backend == 'openmd.csv'
    else:
        assert result.status == 'failed'
