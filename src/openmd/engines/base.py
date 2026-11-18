"""Document engine contract using the existing normalized extraction result.

``TranscriptResult.text`` is Markdown; components, source_level, metadata and
error_code carry structural coverage, evidence and failures without a second
output model. All engines are local and must reject media before importing a
converter. A shared deadline/budget bounds nested archive conversions.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
import time
from typing import TypedDict

from openmd.core.models import TranscriptResult
from openmd.core.secrets import sanitize_error_summary


class Availability(TypedDict):
    ok: bool
    reason: str
    version: str | None


@dataclass
class ArchiveBudget:
    files: int = 0
    bytes: int = 0


@dataclass(frozen=True)
class EngineLimits:
    timeout_s: float = 120
    max_file_bytes: int = 100 * 1024 * 1024
    max_archive_bytes: int = 500 * 1024 * 1024
    max_archive_files: int = 1000
    max_archive_depth: int = 2
    max_compression_ratio: float = 100
    max_text_bytes: int = 16 * 1024 * 1024
    max_output_bytes: int = 16 * 1024 * 1024
    max_table_rows: int = 10000
    deadline: float | None = None
    archive_depth: int = 0
    allow_ocr: bool = False
    budget: ArchiveBudget = field(default_factory=ArchiveBudget, compare=False)

    def remaining(self) -> float:
        remaining = self.timeout_s if self.deadline is None else self.deadline - time.monotonic()
        if remaining <= 0:
            raise EngineError('document_timeout', 'Document conversion exceeded its total timeout.')
        return remaining


class EngineError(ValueError):
    def __init__(self, code: str, detail: str = '') -> None:
        self.code = code
        super().__init__(detail or code)


class DocumentEngine(ABC):
    id: str

    @abstractmethod
    def can_handle(self, path: Path, sniff: bytes) -> bool:
        """Check both suffix and content signature; never launch a converter."""

    @abstractmethod
    def available(self) -> Availability:
        """Return local dependency availability, reason and installed version."""

    @abstractmethod
    def convert(self, path: Path, limits: EngineLimits | None = None) -> TranscriptResult:
        """Return Markdown/components/evidence or an explicit failure code."""


def failure(engine: str, path: Path, code: str, detail: object = '') -> TranscriptResult:
    return TranscriptResult(text='', status='failed', source_level='partial_extraction',
        backend=engine, title=path.stem, error_code=code,
        error_summary=sanitize_error_summary(detail or code),
        metadata={'profile': 'document', 'source_format': path.suffix.lower().lstrip('.')})


def success(engine: str, path: Path, text: str, **metadata) -> TranscriptResult:
    if not text.strip():
        return failure(engine, path, 'document_empty_text')
    from openmd.adapters.pdf.backends.markitdown import _with_title

    return TranscriptResult(text=_with_title(path.stem, text.strip()), status='verified',
        source_level='native_text', backend=engine, title=path.stem,
        source_notes=['Local native document text; reading order, formulas and image OCR are not certified.'],
        metadata={'profile': 'document', 'source_format': path.suffix.lower().lstrip('.'), **metadata})
