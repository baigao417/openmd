"""Ordered format chains and receipts explaining every engine decision."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import time

from .base import EngineError, EngineLimits, failure
from .markitdown import MarkItDownEngine
from .sniff import validate_document
from .tools import CalibreEngine, DefuddleEngine, PandocEngine, SofficeEngine


def engine_chain(source_format: str):
    from .archive import ZipEngine
    suffix = source_format.lower().lstrip('.')
    if suffix in {'html', 'htm'}:
        return [DefuddleEngine(), MarkItDownEngine()]
    if suffix == 'epub':
        return [PandocEngine(), MarkItDownEngine()]
    if suffix in {'rtf', 'odt', 'odp', 'ods'}:
        return [PandocEngine(), SofficeEngine()]
    if suffix in {'doc', 'xls', 'ppt'}:
        return [SofficeEngine()]
    if suffix in {'mobi', 'azw3', 'fb2'}:
        return [CalibreEngine()]
    if suffix == 'zip':
        return [ZipEngine()]
    if suffix in {'pdf', 'docx', 'pptx', 'xlsx', 'csv', 'tsv', 'json', 'xml', 'ipynb', 'msg'}:
        return [MarkItDownEngine()]
    return []


def convert_document(path: Path, limits: EngineLimits | None = None):
    path = Path(path)
    limits = limits or EngineLimits()
    if limits.deadline is None:
        limits = replace(limits, deadline=time.monotonic() + limits.timeout_s)
    attempts = []
    result = None
    chain = engine_chain(path.suffix)
    if not chain:
        return failure('document.engines', path, 'document_format_unsupported')
    try:
        sniff = validate_document(path, limits)
        for engine in chain:
            limits.remaining()
            if not engine.can_handle(path, sniff):
                attempts.append({'engine': engine.id, 'outcome': 'unsupported', 'reason': 'Suffix/signature not handled.'})
                continue
            availability = engine.available()
            limits.remaining()
            attempt = {'engine': engine.id, **availability}
            if not availability['ok']:
                attempts.append({**attempt, 'outcome': 'unavailable'})
                continue
            result = engine.convert(path, limits)
            attempts.append({**attempt, 'outcome': 'selected' if result.has_usable_text else 'failed', 'error_code': result.error_code})
            if result.has_usable_text or result.error_code in {'ebook_drm_protected', 'document_timeout'}:
                break
        if result is None:
            result = failure('document.engines', path, 'engine_unavailable', ' '.join(a.get('reason', '') for a in attempts))
    except (EngineError, OSError, ValueError) as exc:
        result = failure('document.engines', path, getattr(exc, 'code', 'document_invalid_content'), exc)
    selected = next((a['engine'] for a in reversed(attempts) if a['outcome'] == 'selected'), None)
    return replace(result, metadata={**result.metadata, 'selected_engine': selected,
        'engine': selected.replace('.', '_') if selected else None,
        'engine_selection_reason': 'First available matching engine producing usable native text.' if selected else 'No engine produced usable text.',
        'engine_attempts': attempts}, source_notes=[*result.source_notes,
        'document_engine_selected: ' + (selected or 'none') + '; first matching available engine producing native text.',
        *[f"document_engine_attempt: {a['engine']}={a['outcome']}; version={a.get('version') or 'unknown'}; {a.get('reason', '')}" for a in attempts]])
