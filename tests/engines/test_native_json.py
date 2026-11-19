import pytest
from openmd.engines.native import NativeEngine
from openmd.engines.base import EngineLimits

@pytest.mark.parametrize('case', ['positive', 'broken', 'encrypted', 'empty', 'oversize'])
def test_native_json(tmp_path, case):
    path = tmp_path / 'source.json'
    data = '{"Original": [7, "中文"]}'
    if case == 'broken':
        data = '\x00\ufffd'
    elif case == 'encrypted':
        data = '-----BEGIN PGP MESSAGE-----\nENCRYPTED'
    elif case == 'empty':
        data = ''
    path.write_text(data, encoding='utf-8')
    result = NativeEngine('json').convert(path, EngineLimits(max_file_bytes=1) if case == 'oversize' else None)
    if case == 'positive':
        assert result.has_usable_text
        assert '"Original"' in result.text
        assert result.backend == 'openmd.json'
    else:
        assert result.status == 'failed'
