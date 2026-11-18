import pytest


def test_xlsx_order_tables_and_formula_cache_gap(tmp_path):
    pytest.importorskip('openpyxl')
    from openpyxl import Workbook
    from openmd.engines.native import NativeEngine
    w = Workbook()
    s = w.active
    s.title = 'First'
    s.append(['Name', 'Value'])
    s.append(['Apple', 7])
    s.append(['Formula', '=B2*2'])
    w.create_sheet('Second').append(['Second original'])
    path = tmp_path / 'source.xlsx'
    w.save(path)
    result = NativeEngine('xlsx').convert(path)
    assert result.status == 'partial'
    assert result.text.index('First') < result.text.index('Second')
    assert '| Apple | 7 |' in result.text
    assert '含公式' in result.text
    assert 'formula_cache_missing' in result.source_notes
    assert result.metadata['parts_order'] == ['First', 'Second']


@pytest.mark.parametrize('case', ['broken', 'empty', 'oversize', 'encrypted'])
def test_xlsx_boundaries(tmp_path, case):
    from openmd.engines.native import NativeEngine
    from openmd.engines.base import EngineLimits
    path = tmp_path / 'source.xlsx'
    path.write_bytes(b'bad')
    if case == 'empty':
        pytest.importorskip('openpyxl')
        from openpyxl import Workbook
        Workbook().save(path)
    if case == 'encrypted':
        path.write_bytes(bytes.fromhex('d0cf11e0a1b11ae1') + b'EncryptedPackage')
    assert NativeEngine('xlsx').convert(path, EngineLimits(max_file_bytes=1) if case == 'oversize' else None).status == 'failed'
