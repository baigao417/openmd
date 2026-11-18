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
        # Embedded images are handled from their original package bytes.
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
        if any(i['sha256'] == digest for i in self.images):
            return
        self.images.append({'part': label, 'sha256': digest, 'bytes': len(data), 'mime': mime})
        # A data URI keeps original bytes available in both flat and package
        # output, with no temporary path or external image fetch after exit.
        self.add(f'![Embedded image {len(self.images)}](data:{mime};base64,' + base64.b64encode(data).decode('ascii') + ')')
        if self.limits.allow_ocr:
            try:
                from PIL import Image
                from openmd.ocr.pipeline import LocalOCRBackend
                with Image.open(io.BytesIO(data)) as image:
                    value = LocalOCRBackend().recognize_image(image.convert('RGB'), title=label)
                self.components.extend(value.normalized_components)
                if value.has_usable_text:
                    self.add(value.text)
                    if value.status != 'verified':
                        self.gap('embedded_image_ocr_partial')
                    return
            except (ImportError, OSError, ValueError):
                pass
        self.gap('embedded_image_ocr_unavailable', 'image-' + str(len(self.images)))

    def result(self):
        text = '\n\n'.join(self.parts)
        result = success(self.id, self.path, text, tables=self.tables, images=self.images, gaps=self.gaps, **self.meta)
        if not result.has_usable_text:
            return result
        return replace(result, status='partial' if self.gaps else 'verified', components=self.components,
            source_notes=[*result.source_notes, *self.gaps], metadata={**result.metadata, 'terminal_partial': bool(self.gaps)})
