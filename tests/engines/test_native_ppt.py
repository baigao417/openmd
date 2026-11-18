import struct
import pytest
from native_fixtures import ole_container
from openmd.engines.native import NativeEngine
from openmd.engines.base import EngineLimits

def record(kind, payload=b'', container=False):
    return struct.pack('<HHI', 15 if container else 0, kind, len(payload)) + payload

@pytest.mark.parametrize('case', ['positive','broken','empty','encrypted','oversize'])
def test_ppt_native(tmp_path, case):
    if case == 'positive':
        pytest.importorskip('olefile')
    path = tmp_path / 'source.ppt'
    body = record(4000, 'Original 中文'.encode('utf-16le')) + record(4008, b'Second original')
    streams = {'PowerPoint Document': record(1006, b'' if case=='empty' else body, True)}
    if case == 'encrypted':
        streams['EncryptedSummary'] = b'ciphertext'
    path.write_bytes(b'bad' if case=='broken' else ole_container(streams))
    result = NativeEngine('ppt').convert(path, EngineLimits(max_file_bytes=1) if case=='oversize' else None)
    if case=='positive':
        assert result.has_usable_text
        assert 'Original 中文' in result.text and 'Second original' in result.text
        assert result.status == 'partial'
        assert '安装 LibreOffice 可获得完整转换' in result.source_notes
    else:
        assert result.status == 'failed'
