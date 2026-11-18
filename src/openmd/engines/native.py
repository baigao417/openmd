"""Openmd-owned document glue. Optional parsers run in an owned local worker."""
from __future__ import annotations

from dataclasses import replace
import importlib.metadata
import importlib.util
from pathlib import Path

from openmd.core.models import ExtractionComponent, TranscriptResult
from .base import DocumentEngine, EngineError, EngineLimits, failure
from .sniff import signature, validate_document


DEPENDENCIES = {'docx': ('mammoth', 'mammoth'), 'pptx': ('pptx', 'python-pptx')}
# Promotion is explicit and follows a saved per-format benchmark.
PROMOTED = {'docx', 'pptx'}


class NativeEngine(DocumentEngine):
    def __init__(self, source_format: str):
        self.format = source_format.lower().lstrip('.')
        self.id = 'openmd.' + self.format

    def can_handle(self, path: Path, sniff: bytes) -> bool:
        return path.suffix.lower() == '.' + self.format and signature(path, sniff)

    def available(self):
        dependency = DEPENDENCIES.get(self.format)
        if dependency and importlib.util.find_spec(dependency[0]) is None:
            return {'ok': False, 'reason': 'python -m pip install "openmd-cli[document]" (missing ' + dependency[1] + ')', 'version': None}
        version = None
        if dependency:
            try:
                version = importlib.metadata.version(dependency[1])
            except importlib.metadata.PackageNotFoundError:
                pass
        return {'ok': True, 'reason': 'Openmd local document parser; no downloads or cloud plugins.', 'version': version or 'builtin'}

    def convert(self, path: Path, limits: EngineLimits | None = None) -> TranscriptResult:
        limits = limits or EngineLimits()
        try:
            validate_document(path, limits)
            with path.open('rb') as stream:
                sniff = stream.read(4096)
            if not self.can_handle(path, sniff):
                raise EngineError('document_format_unsupported')
            available = self.available()
            if not available['ok']:
                return failure(self.id, path, 'engine_unavailable', available['reason'])
            from .worker import convert_in_worker
            value = convert_in_worker(path, limits, mode='native')
            value['components'] = [ExtractionComponent(**c) for c in value.get('components', [])]
            return TranscriptResult(**value)
        except (EngineError, OSError, ValueError) as exc:
            return failure(self.id, path, getattr(exc, 'code', 'document_extraction_failed'), exc)


def convert_local(path: Path, limits: EngineLimits):
    from .native_common import Document
    from .native_office import parse_docx, parse_pptx
    document = Document(path, limits)
    {'docx': parse_docx, 'pptx': parse_pptx}[path.suffix.lower().lstrip('.')](document)
    return document.result()
