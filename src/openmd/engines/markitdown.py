"""Document-only MarkItDown integration; no media or ZIP converter entry."""
from __future__ import annotations

import csv
from dataclasses import replace
import importlib.metadata
import importlib.util
import json
from pathlib import Path

from openmd.runtime.tempdirs import owned_temporary_directory

from .base import DocumentEngine, EngineError, EngineLimits, failure, success
from .sniff import LEGACY_FORMATS, MARKITDOWN_FORMATS, signature, validate_document


class MarkItDownEngine(DocumentEngine):
    id = 'markitdown'

    def can_handle(self, path: Path, sniff: bytes) -> bool:
        # A fixed positive allowlist, independent of detector growth. Neither
        # MarkItDown's AudioConverter nor ZipConverter can receive an input.
        return path.suffix.lower().lstrip('.') in MARKITDOWN_FORMATS and signature(path, sniff)

    def available(self):
        if importlib.util.find_spec('markitdown') is None:
            return {'ok': False, 'reason': 'Install openmd-cli[document] for MarkItDown document converters.', 'version': None}
        try:
            version = importlib.metadata.version('markitdown')
        except importlib.metadata.PackageNotFoundError:
            version = 'unknown'
        return {'ok': True, 'reason': 'MarkItDown Python package detected; format extras checked at conversion.', 'version': version}

    def convert(self, path: Path, limits: EngineLimits | None = None, *, _legacy_parser=None, title: str | None = None):
        limits = limits or EngineLimits()
        if path.suffix.lower().lstrip('.') not in MARKITDOWN_FORMATS:
            return failure(self.id, path, 'document_format_unsupported')
        if path.suffix.lower().lstrip('.') in LEGACY_FORMATS:
            # Preserve the established parser's output, native Office validation
            # and error codes. The compatibility facade delegates here.
            from openmd.adapters.pdf.backends.markitdown import MarkItDownPDFParser
            if _legacy_parser is None:
                from .worker import convert_in_worker
                from openmd.core.models import ExtractionComponent, TranscriptResult
                try:
                    validate_document(path, limits)
                    data = convert_in_worker(path, limits, mode='legacy_markitdown')
                    data['components'] = [ExtractionComponent(**c) for c in data.get('components', [])]
                    return TranscriptResult(**data)
                except (EngineError, OSError, ValueError) as exc:
                    return failure(self.id, path, getattr(exc, 'code', 'document_extraction_failed'), exc)
            parser = _legacy_parser or MarkItDownPDFParser()
            return parser._parse_legacy(str(path), title=path.stem if title is None else title)
        try:
            validate_document(path, limits)
            availability = self.available()
            if not availability['ok']:
                return failure(self.id, path, 'engine_unavailable', availability['reason'])
            coverage = None
            source = path
            with owned_temporary_directory(prefix='openmd-markitdown-') as owned_root:
                root = Path(owned_root)
                if path.suffix.lower() in {'.csv', '.tsv'}:
                    source = root / 'table.csv'
                    if path.stat().st_size > limits.max_text_bytes:
                        raise EngineError('document_size_limit')
                    with path.open(encoding='utf-8-sig', newline='') as stream, source.open('w', encoding='utf-8', newline='') as out:
                        reader = csv.reader(stream, delimiter='\t' if path.suffix.lower() == '.tsv' else ',')
                        writer = csv.writer(out)
                        header = next(reader, None)
                        if header is None:
                            raise EngineError('document_empty_text')
                        writer.writerow(header)
                        total = 0
                        for row in reader:
                            limits.remaining()
                            total += 1
                            if total <= limits.max_table_rows:
                                writer.writerow(row)
                    coverage = {'rows_total': total, 'rows_included': min(total, limits.max_table_rows),
                        'truncated': total > limits.max_table_rows, 'reason': 'table_row_limit' if total > limits.max_table_rows else None,
                        'expected_count': total, 'fetched_count': min(total, limits.max_table_rows),
                        'missing_count': max(0, total - limits.max_table_rows), 'complete': total <= limits.max_table_rows}
                text = self._convert_text(source, limits)
            limits.remaining()
            if len(text.encode('utf-8')) > limits.max_output_bytes:
                raise EngineError('document_output_limit')
            if path.suffix.lower() == '.msg' and not text.partition('## Content')[2].strip():
                raise EngineError('document_empty_text', 'MSG converter returned no message body.')
            result = success('pdf.markitdown', path, text, **({'coverage': coverage} if coverage else {}))
            if coverage and coverage['truncated'] and result.has_usable_text:
                result = replace(result, status='partial', source_notes=[*result.source_notes,
                    f"Table truncated: {coverage['rows_included']} of {coverage['rows_total']} data rows included.",
                    'document_coverage: ' + json.dumps(coverage, sort_keys=True)])
            return result
        except (EngineError, OSError, UnicodeError, csv.Error, ValueError) as exc:
            return failure(self.id, path, getattr(exc, 'code', 'document_extraction_failed'), exc)

    def _convert_text(self, path: Path, limits: EngineLimits) -> str:
        # Third party parsers are interruptible even inside a ZIP's shared total
        # deadline. The worker uses the same installed Python dependency paths.
        from .worker import convert_in_worker
        return convert_in_worker(path, limits, mode='markitdown_text')['text']


def convert_text_local(path: Path) -> str:
    """Worker entry: explicit document converters, plugins disabled."""
    suffix = path.suffix.lower().lstrip('.')
    if suffix not in MARKITDOWN_FORMATS:
        raise EngineError('document_format_unsupported')
    from markitdown import MarkItDown
    from markitdown import converters
    names = {
        'pdf': 'PdfConverter', 'docx': 'DocxConverter', 'pptx': 'PptxConverter', 'xlsx': 'XlsxConverter',
        'html': 'HtmlConverter', 'htm': 'HtmlConverter', 'epub': 'EpubConverter',
        'csv': 'CsvConverter', 'tsv': 'CsvConverter', 'ipynb': 'IpynbConverter', 'msg': 'OutlookMsgConverter',
        'json': 'PlainTextConverter', 'xml': 'PlainTextConverter',
    }
    parser = MarkItDown(enable_builtins=False, enable_plugins=False)
    parser.register_converter(getattr(converters, names[suffix])())
    converted = parser.convert(str(path))
    return (getattr(converted, 'text_content', '') or '').strip()
