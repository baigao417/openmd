"""Openmd text data readers; no execution, plugins, or network calls."""
import csv
import json
from .base import EngineError

FORMATS = {'csv'}


def parse_table(d):
    total = 0
    rows = []
    with d.path.open(encoding='utf-8-sig', newline='') as stream:
        reader = csv.reader(stream, delimiter='\t' if d.path.suffix.lower() == '.tsv' else ',', strict=True)
        header = next(reader, None)
        if header is None:
            return
        rows.append(header)
        for row in reader:
            d.limits.remaining()
            total += 1
            if total <= d.limits.max_table_rows:
                rows.append(row)
    d.table(rows)
    included = min(total, d.limits.max_table_rows)
    coverage = {'rows_total': total, 'rows_included': included, 'truncated': total > included,
        'reason': 'table_row_limit' if total > included else None, 'expected_count': total,
        'fetched_count': included, 'missing_count': total-included, 'complete': total == included}
    d.meta['coverage'] = coverage
    if total > included:
        d.gap('table_row_limit')


def parse(d):
    suffix = d.path.suffix.lower().lstrip('.')
    if suffix in {'csv', 'tsv'}:
        return parse_table(d)
    return globals()['parse_' + suffix](d)

FORMATS.add('tsv')


def fenced(text, language=''):
    import re
    fence = '`' * max(3, max((len(m.group())+1 for m in re.finditer(r'`+', text)), default=3))
    return fence + language + '\n' + text + '\n' + fence


def parse_json(d):
    text = d.path.read_text(encoding='utf-8-sig')
    value = json.loads(text)
    # Preserve exact source lexemes (large integers, number exponents, key order).
    d.add(fenced(text, 'json'))

FORMATS.add('json')
