"""Local ODF text, tables, slides and cached values; no macro execution."""
from zipfile import ZipFile
import mimetypes

from .base import EngineError
from .native_common import xml, name, attr


def text(node):
    result = node.text or ''
    for child in node:
        kind = name(child)
        if kind == 's':
            result += ' ' * min(max(int(attr(child, 'c', '1')), 0), 10000)
        elif kind == 'tab':
            result += '\t'
        elif kind == 'line-break':
            result += '\n'
        elif kind == 'a' and attr(child, 'href'):
            result += '[' + text(child) + '](' + attr(child, 'href') + ')'
        else:
            result += text(child)
        result += child.tail or ''
    return result


def walk(d, node):
    kind = name(node)
    if kind == 'h':
        d.add('#' * min(max(int(attr(node, 'outline-level', '1')), 1), 6) + ' ' + text(node))
    elif kind == 'p':
        d.add(text(node))
    elif kind == 'list':
        numbered = getattr(d, 'numbered_styles', set())
        ordered = attr(node, 'style-name') in numbered
        for index, item in enumerate(node, 1):
            first = True
            for child in item:
                if name(child) == 'p':
                    prefix = (str(int(attr(item, 'start-value', str(index)))) + '. ' if ordered else '- ') if first else ''
                    d.add(prefix + text(child))
                    first = False
                else:
                    walk(d, child)
    elif kind == 'table':
        rows = []
        for row in (r for r in node.iter() if name(r) == 'table-row'):
            d.limits.remaining()
            cells = []
            for c in row:
                if name(c) in {'table-cell','covered-table-cell'}:
                    value = text(c).strip() or next((attr(c, key) for key in ('value','string-value','date-value','boolean-value') if attr(c, key)), '')
                    count = int(attr(c, 'number-columns-repeated', '1'))
                    if count > d.limits.max_table_rows:
                        if value:
                            d.gap('table_column_limit')
                        count = d.limits.max_table_rows
                    cells.extend([value] * max(0, min(count, d.limits.max_table_rows-len(cells))))
                    if attr(c, 'formula'):
                        d.meta['contains_formulas'] = True
            while cells and not cells[-1]:
                cells.pop()
            repeated = int(attr(row, 'number-rows-repeated', '1'))
            if any(cells):
                room = max(0, d.limits.max_table_rows + 1-len(rows))
                rows.extend([cells] * min(repeated, room))
                if repeated > room:
                    d.gap('table_row_limit')
            elif repeated == 1 and rows and len(rows) < d.limits.max_table_rows+1:
                rows.append(cells)
        while rows and not any(rows[-1]):
            rows.pop()
        d.table(rows)
    elif kind != 'notes':
        for child in node:
            walk(d, child)


def parse(d):
    with ZipFile(d.path) as z:
        if 'META-INF/manifest.xml' in z.namelist() and any(name(n) == 'encryption-data' for n in xml(z.read('META-INF/manifest.xml')).iter()):
            raise EngineError('document_encrypted')
        root = xml(z.read('content.xml'))
        d.numbered_styles = {attr(n, 'name') for n in root.iter() if name(n) == 'list-style' and any(name(c) == 'list-level-style-number' for c in n)}
        suffix = d.path.suffix.lower()
        d.meta.update(parts_order=[], notes=0)
        if suffix == '.odt':
            walk(d, root)
        else:
            part_kind = 'page' if suffix == '.odp' else 'table'
            for index, part in enumerate((n for n in root.iter() if name(n) == part_kind), 1):
                label = attr(part, 'name') or str(index)
                d.meta['parts_order'].append(label)
                start = len(d.parts)
                d.section(label, lambda part=part: walk(d, part))
                for notes in (n for n in part.iter() if name(n) == 'notes'):
                    body = '\n\n'.join(text(n) for n in notes.iter() if name(n) == 'p')
                    if body.strip():
                        d.add('### Speaker notes\n\n' + body)
                        d.meta['notes'] += 1
                if len(d.parts) > start:
                    d.parts.insert(start, '## ' + label)
        if d.meta.get('contains_formulas'):
            d.add('含公式（显示文件中已保存的计算值；不执行公式）。')
        if 'styles.xml' in z.namelist():
            def footers():
                for node in xml(z.read('styles.xml')).iter():
                    if name(node) in {'header','footer'}:
                        walk(d, node)
            d.section('header-footer', footers)
        for part in z.namelist():
            if part.startswith('Pictures/') and not part.endswith('/'):
                d.section(part, lambda part=part: d.image(z.read(part), part, mimetypes.guess_type(part)[0] or 'application/octet-stream'))
