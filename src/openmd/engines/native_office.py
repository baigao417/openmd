"""Office glue built on mammoth and the public OOXML document structure."""
from __future__ import annotations

import mimetypes
from zipfile import ZipFile
from .native_common import xml, name, html_markdown


def embedded_images(d, z):
    for part in z.namelist():
        if '/media/' in part and not part.endswith('/'):
            d.section('image:' + part, lambda part=part: d.image(z.read(part), part, mimetypes.guess_type(part)[0] or 'application/octet-stream'))


def parse_docx(d):
    import mammoth
    with ZipFile(d.path) as z:
        root = xml(z.read('word/document.xml'))
        # Mammoth keeps semantic styles, lists, hyperlinks, footnotes and tables;
        # images are extracted separately without its default data conversion.
        def ignore_image(image):
            return {'src': '', 'alt': ''}
        with d.path.open('rb') as stream:
            value = mammoth.convert_to_html(stream, convert_image=mammoth.images.img_element(ignore_image))
        d.add(html_markdown(d, value.value))
        # Header/footer text isn't included by mammoth. Preserve it explicitly.
        for part in z.namelist():
            if part.startswith(('word/header', 'word/footer')) and part.endswith('.xml'):
                def include(part=part):
                    d.add('\n\n'.join(''.join(n.itertext()) for n in xml(z.read(part)).iter() if name(n) == 't'))
                d.section(part, include)
        d.meta['parser_warnings'] = [m.type for m in value.messages]
        if any(m.type == 'warning' for m in value.messages):
            d.gap('docx_parser_warning')
        embedded_images(d, z)


def parse_pptx(d):
    from pptx import Presentation
    presentation = Presentation(d.path)
    d.meta.update(parts_order=[], notes=0)

    def shapes(items):
        for shape in items:
            if shape.has_text_frame:
                d.add(shape.text_frame.text)
            if shape.has_table:
                d.table([[c.text for c in r.cells] for r in shape.table.rows])
            if shape.has_chart:
                chart = shape.chart
                if chart.has_title:
                    d.add(chart.chart_title.text_frame.text)
                for plot in chart.plots:
                    series = list(plot.series)
                    categories = [c.label for c in plot.categories]
                    values = [list(s.values) for s in series]
                    d.table([['Category', *[s.name for s in series]], *[[label, *[v[i] if i < len(v) else '' for v in values]] for i, label in enumerate(categories)]])
            if hasattr(shape, 'shapes'):
                shapes(shape.shapes)

    for index, slide in enumerate(presentation.slides, 1):
        label = f'Slide {index}'
        d.meta['parts_order'].append(label)
        start = len(d.parts)
        d.section(label, lambda slide=slide: shapes(slide.shapes))
        if slide.has_notes_slide:
            def notes(slide=slide):
                frame = slide.notes_slide.notes_text_frame
                if frame is not None and frame.text.strip():
                    d.add('### Speaker notes\n\n' + frame.text)
                    d.meta['notes'] += 1
            d.section(label + ':notes', notes)
        if len(d.parts) > start:
            d.parts.insert(start, '## ' + label)
    with ZipFile(d.path) as z:
        embedded_images(d, z)


def parse_xlsx(d):
    from openpyxl import load_workbook
    values = load_workbook(d.path, read_only=True, data_only=True, keep_links=False)
    formulas = load_workbook(d.path, read_only=True, data_only=False, keep_links=False)
    d.meta.update(parts_order=values.sheetnames, formula_count=0)
    try:
        for sheet in values:
            def include(sheet=sheet):
                rows = []
                formula_count = missing = 0
                for index, (cells, expressions) in enumerate(zip(sheet.iter_rows(), formulas[sheet.title].iter_rows())):
                    d.limits.remaining()
                    if index > d.limits.max_table_rows:
                        d.gap('table_row_limit', sheet.title + ':rows')
                        break
                    row = []
                    for c, f in zip(cells, expressions):
                        value = c.value
                        if f.data_type == 'f':
                            formula_count += 1
                            if value is None:
                                missing += 1
                                value = '[Formula cached value unavailable]'
                        row.append(value)
                    rows.append(row)
                if not any(any(v is not None for v in row) for row in rows):
                    return
                d.add('## ' + sheet.title)
                d.table(rows)
                if formula_count:
                    d.add('含公式（显示文件中已保存的计算值；不执行公式）。')
                    d.meta['formula_count'] += formula_count
                if missing:
                    d.gap('formula_cache_missing', sheet.title + ':formulas')
            d.section(sheet.title, include)
    finally:
        values.close()
        formulas.close()
    with ZipFile(d.path) as z:
        embedded_images(d, z)


def parse_xls(d):
    import xlrd
    book = xlrd.open_workbook(d.path, on_demand=True)
    d.meta['parts_order'] = book.sheet_names()
    try:
        for sheet in book.sheets():
            def include(sheet=sheet):
                rows = [sheet.row_values(i) for i in range(min(sheet.nrows, d.limits.max_table_rows + 1))]
                if not any(any(str(v).strip() for v in row) for row in rows):
                    return
                d.add('## ' + sheet.name)
                d.table(rows)
                if sheet.nrows > len(rows):
                    d.gap('table_row_limit', sheet.name + ':rows')
            d.section(sheet.name, include)
    finally:
        book.release_resources()
