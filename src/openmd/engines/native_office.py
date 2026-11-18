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
