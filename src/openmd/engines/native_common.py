"""Bounded Markdown, section coverage and original embedded image handling."""
from __future__ import annotations

import base64
from dataclasses import replace
import hashlib
import io
from pathlib import Path
import re
from xml.etree import ElementTree as ET

from openmd.core.models import ExtractionComponent
from .base import EngineError, success


def xml(data: bytes):
    if re.search(br'<!\s*(DOCTYPE|ENTITY)', data, re.I):
        raise EngineError('document_invalid_content', 'DTD and entities are disabled.')
    return ET.fromstring(data)


def name(node):
    return node.tag.rsplit('}', 1)[-1]


def attr(node, key, default=''):
    return next((v for k, v in node.attrib.items() if k.rsplit('}', 1)[-1] == key), default)


def cell(value):
    return str('' if value is None else value).replace('\\', '\\\\').replace('|', '\\|').replace('\r', '').replace('\n', '<br>')


def html_markdown(d, text):
    """Reuse the existing HTML cleaner, preserving document data tables."""
    from bs4 import BeautifulSoup
    from openmd.core.content_validation import html_to_markdown
    soup = BeautifulSoup(text, 'html.parser')
    replacements = {}
    for index, table in enumerate(list(soup.find_all('table'))):
        if table.parent is None:
            continue
        rows = [[html_to_markdown(''.join(str(n) for n in c.contents)) for c in r.find_all(['td', 'th'], recursive=False)] for r in table.find_all('tr')]
        start = len(d.parts)
        d.table(rows)
        markdown = '\n\n'.join(d.parts[start:])
        del d.parts[start:]
        key = f'OPENMDDOCUMENTTABLE{index}END'
        replacements[key] = markdown
        table.replace_with(soup.new_string('\n\n' + key + '\n\n'))
    for image in soup.find_all('img'):
        token = image.get('alt', '')
        if token in getattr(d, 'image_slots', {}):
            image.replace_with(soup.new_string('\n\n' + token + '\n\n'))
        else:
            image.decompose()
    value = html_to_markdown(str(soup))
    for key, replacement in replacements.items():
        value = value.replace(key, replacement)
    return value


class Document:
    def __init__(self, path, limits):
        self.path, self.limits = path, limits
        self.id = 'openmd.' + path.suffix[1:].lower()
        self.parts, self.components, self.tables, self.images, self.gaps = [], [], [], [], []
        self.bytes = 0
        self.meta = {}

    def add(self, value):
        self.limits.remaining()
        self.bytes += len(value.encode('utf-8'))
        if self.bytes > self.limits.max_output_bytes:
            raise EngineError('document_output_limit')
        if value.strip():
            self.parts.append(value.strip())

    def table(self, rows):
        rows = list(rows)
        if not rows or not any(any(v is not None and str(v).strip() for v in r) for r in rows):
            return
        width = max(map(len, rows))
        total = len(rows)
        rows = rows[:self.limits.max_table_rows + 1]
        self.tables.append({'rows': len(rows), 'columns': width, 'rows_total': total})
        if len(rows) < total:
            self.gap('table_row_limit')
        lines = ['| ' + ' | '.join(cell(v) for v in [*row, *([''] * (width-len(row)))]) + ' |' for row in rows]
        lines.insert(1, '| ' + ' | '.join(['---'] * width) + ' |')
        self.add('\n'.join(lines))

    def gap(self, code, part=None):
        self.gaps.append(code)
        self.components.append(ExtractionComponent(id=part or f'gap-{len(self.gaps)}', kind='document', status='partial', source_level='native_text', backend=self.id, error_code=code, warnings=[code]))

    def section(self, identifier, callback):
        try:
            self.limits.remaining()
            callback()
            self.components.append(ExtractionComponent(id=identifier, kind='document', status='verified', source_level='native_text', backend=self.id))
        except EngineError:
            raise
        except Exception:
            # A damaged part must not erase independently readable parts.
            self.gap('document_part_failed', identifier)

    def image(self, data, label, mime='image/png'):
        digest = hashlib.sha256(data).hexdigest()
        previous = next((i for i in self.images if i['sha256'] == digest), None)
        if previous is not None:
            self.add(f'![Embedded image reference](data:{mime};base64,' + base64.b64encode(data).decode('ascii') + ')')
            self.add('来源：OCR\n\n' + previous['_ocr_text'] if previous.get('_ocr_text') else '[图片文字缺口：OCR 未配置、未授权或不可用]')
            self.meta.setdefault('image_occurrences', []).append({'part': label, 'sha256': digest, 'ocr_reused': bool(previous.get('_ocr_text'))})
            return
        self.images.append({'part': label, 'sha256': digest, 'bytes': len(data), 'mime': mime})
        # A data URI keeps original bytes available in both flat and package
        # output, with no temporary path or external image fetch after exit.
        self.add(f'![Embedded image {len(self.images)}](data:{mime};base64,' + base64.b64encode(data).decode('ascii') + ')')
        if self.limits.allow_ocr:
            try:
                from PIL import Image
                from openmd.ocr.engine import OcrEngine
                with Image.open(io.BytesIO(data)) as image:
                    engine = OcrEngine()
                    if engine.id == 'ocr.rapidocr':
                        # The configured OCR environment may differ from the document worker.
                        from openmd.runtime.tempdirs import owned_temporary_directory
                        with owned_temporary_directory(prefix='openmd-office-ocr-') as root:
                            source = Path(root) / 'embedded.png'
                            image.convert('RGB').save(source)
                            value = engine.convert(source, self.limits)
                    else:
                        value = engine.recognize_image(image.convert('RGB'), title=label, timeout_s=self.limits.remaining())
                self.images[-1]['ocr'] = {'status': value.status, 'backend': value.backend, 'error_code': value.error_code, 'evidence': value.metadata}
                self.components.extend(value.normalized_components)
                if value.has_usable_text:
                    self.images[-1]['_ocr_text'] = value.text
                    self.add('来源：OCR\n\n' + value.text)
                    if value.status != 'verified':
                        self.gap('embedded_image_ocr_partial')
                    return
            except (ImportError, OSError, ValueError, TimeoutError):
                pass
        self.images[-1].setdefault('ocr', {'status': 'unavailable', 'error_code': 'embedded_image_ocr_unavailable'})
        self.add('[图片文字缺口：OCR 未配置、未授权或不可用]')
        self.gap('embedded_image_ocr_unavailable', 'image-' + str(len(self.images)))

    def result(self):
        text = '\n\n'.join(self.parts)
        images = [{key: value for key, value in image.items() if key != '_ocr_text'} for image in self.images]
        result = success(self.id, self.path, text, tables=self.tables, images=images, gaps=self.gaps, **self.meta)
        if not result.has_usable_text:
            return result
        return replace(result, status='partial' if self.gaps else 'verified', components=self.components,
            source_notes=[*result.source_notes, *self.gaps], metadata={**result.metadata, 'terminal_partial': bool(self.gaps)})
