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
