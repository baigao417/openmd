import pytest


def test_pptx_text_table_notes_chart_and_order(tmp_path):
    pytest.importorskip('pptx')
    from pptx import Presentation
    from pptx.util import Inches
    from pptx.chart.data import CategoryChartData
    from pptx.enum.chart import XL_CHART_TYPE
    from openmd.engines.native import NativeEngine
    p = Presentation()
    for title in ['First original', 'Second original']:
        slide = p.slides.add_slide(p.slide_layouts[5])
        slide.shapes.title.text = title
        slide.notes_slide.notes_text_frame.text = 'Speaker original ' + title
    table = p.slides[0].shapes.add_table(2, 2, Inches(1), Inches(2), Inches(4), Inches(1)).table
    table.cell(0, 0).text = 'Name'
    table.cell(0, 1).text = 'Value'
    table.cell(1, 0).text = 'Apple'
    table.cell(1, 1).text = '7'
    data = CategoryChartData()
    data.categories = ['Category original']
    data.add_series('Series original', [11])
    p.slides[1].shapes.add_chart(XL_CHART_TYPE.COLUMN_CLUSTERED, Inches(1), Inches(2), Inches(4), Inches(2), data)
    path = tmp_path / 'authored.pptx'
    p.save(path)
    result = NativeEngine('pptx').convert(path)
    assert result.status == 'verified'
    assert result.text.index('First original') < result.text.index('Second original')
    assert all(s in result.text for s in ['Speaker original', '| Apple | 7 |', 'Category original', 'Series original', '11'])
    assert result.metadata['parts_order'] == ['Slide 1', 'Slide 2']
    assert result.metadata['notes'] == 2


@pytest.mark.parametrize('case', ['broken', 'empty', 'oversize', 'encrypted'])
def test_pptx_boundaries(tmp_path, case):
    from openmd.engines.native import NativeEngine
    from openmd.engines.base import EngineLimits
    path = tmp_path / 'test.pptx'
    path.write_bytes(b'bad')
    if case == 'empty':
        pytest.importorskip('pptx')
        from pptx import Presentation
        Presentation().save(path)
    if case == 'encrypted':
        path.write_bytes(bytes.fromhex('d0cf11e0a1b11ae1') + b'EncryptedPackage')
    result = NativeEngine('pptx').convert(path, EngineLimits(max_file_bytes=1) if case == 'oversize' else None)
    assert result.status == 'failed'
