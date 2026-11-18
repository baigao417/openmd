import pytest
from native_fixtures import ole_container
from openmd.engines.native import NativeEngine
from openmd.engines.base import EngineLimits

@pytest.mark.parametrize('case', ['positive','broken','empty','encrypted','oversize'])
def test_msg_native(tmp_path, case):
    if case == 'positive':
        pytest.importorskip('olefile')
    path = tmp_path / 'source.msg'
    streams = {'__substg1.0_0037001F': 'Original subject'.encode('utf-16le'), '__substg1.0_1000001F': 'Original 中文 body'.encode('utf-16le')}
    if case == 'empty':
        streams = {'__substg1.0_0037001F': 'Subject only'.encode('utf-16le')}
    if case == 'encrypted':
        streams = {'__substg1.0_001A001F': 'IPM.Note.SMIME'.encode('utf-16le'), **streams}
    path.write_bytes(b'bad' if case == 'broken' else ole_container(streams))
    result = NativeEngine('msg').convert(path, EngineLimits(max_file_bytes=1) if case == 'oversize' else None)
    if case == 'positive':
        assert result.has_usable_text
        assert 'Original 中文 body' in result.text and 'Original subject' in result.text
    else:
        assert result.status == 'failed'
