"""pdfminer native text candidate; established PDF/OCR provider order stays."""
from pdfminer.high_level import extract_pages
from pdfminer.layout import LTTextContainer, LTImage, LTFigure
from pdfminer.pdfdocument import PDFDocument
from pdfminer.pdfparser import PDFParser

from .base import EngineError


def parse(d):
    with d.path.open('rb') as source:
        document = PDFDocument(PDFParser(source))
        if document.encryption:
            raise EngineError('document_encrypted')
    pages = []
    for number, page in enumerate(extract_pages(str(d.path)), 1):
        pages.append(number)
        start = len(d.parts)

        def include(page=page):
            image_count = 0

            def walk(nodes):
                nonlocal image_count
                for node in nodes:
                    d.limits.remaining()
                    if isinstance(node, LTTextContainer):
                        d.add(node.get_text())
                    elif isinstance(node, LTImage):
                        image_count += 1
                    elif isinstance(node, LTFigure):
                        walk(node)
            walk(page)
            if image_count:
                d.gap('pdf_images_require_ocr', 'page-' + str(number))
        d.section('page-' + str(number), include)
        if len(d.parts) > start:
            d.parts.insert(start, '## Page ' + str(number))
    d.meta['parts_order'] = pages

