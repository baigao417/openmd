import pytest
from openmd.engines.base import EngineLimits
from openmd.engines.tools import CalibreEngine
from test_signatures import SAMPLES

@pytest.mark.parametrize('suffix',['mobi','azw3'])
@pytest.mark.parametrize('case',['broken','empty','encrypted','oversize'])
def test_ebook_boundaries(tmp_path,suffix,case):
    path = tmp_path / ('source.'+suffix)
    data = bytearray(SAMPLES[suffix])
    if case == 'encrypted':
        data[112:114] = (2).to_bytes(2,'big')
    if case == 'broken':
        data = b'bad'
    if case == 'empty':
        data = b''
    path.write_bytes(data)
    result = CalibreEngine().convert(path, EngineLimits(max_file_bytes=1) if case=='oversize' else None)
    assert result.status == 'failed'
    if case == 'encrypted':
        assert result.error_code == 'ebook_drm_protected'

