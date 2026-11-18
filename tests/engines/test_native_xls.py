import pytest
from native_fixtures import ole_container, biff_workbook


def test_xls_native_saved_values(tmp_path):
    pytest.importorskip('xlrd')
    from openmd.engines.native import NativeEngine
    path = tmp_path / 'source.xls'
    path.write_bytes(ole_container({'Workbook': biff_workbook()}))
    result = NativeEngine('xls').convert(path)
    assert result.status == 'verified'
    assert 'Original' in result.text and '7' in result.text and '11' in result.text
    assert result.metadata['parts_order'] == ['Original']


@pytest.mark.parametrize('case', ['broken', 'empty', 'oversize', 'encrypted'])
def test_xls_boundaries(tmp_path, case):
    from openmd.engines.native import NativeEngine
    from openmd.engines.base import EngineLimits
    data = b'bad'
    if case in {'empty', 'encrypted'}:
        data = ole_container({'Workbook': (b'\x2f\x00\x02\x00\x01\x00' if case == 'encrypted' else b'\0'*16)})
    path = tmp_path / 'source.xls'
    path.write_bytes(data)
    assert NativeEngine('xls').convert(path, EngineLimits(max_file_bytes=1) if case == 'oversize' else None).status == 'failed'
