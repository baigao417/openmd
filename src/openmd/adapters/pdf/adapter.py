"""V1 adapter for local PDF and Office document inputs.

This lane is local and read-only by default.  It resolves PDF files or folders
into ``ContentItem`` records and leaves parsing to explicit parser backends.
Raw documents are referenced as sources and are not copied into package assets.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse
import hashlib

from openmd.adapters.base import PlatformAdapter
from openmd.adapters.local_audio.adapter import local_path_from_input
from openmd.core.detector import DOCUMENT_EXTENSIONS, extract_first_url
from openmd.core.models import ContentItem, TranscriptResult

from .backends.markitdown import MarkItDownPDFParser
from .parser import MissingPDFParser, MockPDFParser


class PdfDocumentAdapter(PlatformAdapter):
    """Resolve local PDF files, folders, and PDF URL references without side effects."""

    def __init__(self, platform_id: str = "pdf", default_backend: str = "pdf.markitdown") -> None:
        self.id = platform_id
        self.default_backend = default_backend

    def can_handle(self, input_value: str) -> bool:
        return _is_pdf_input(input_value)

    def resolve_items(
        self,
        input_value: str,
        *,
        limit: int | None = None,
        all: bool = False,
    ) -> list[ContentItem]:
        path = local_path_from_input(input_value)
        if path is not None and path.is_dir():
            files = sorted(candidate for candidate in path.rglob("*") if candidate.is_file() and _is_pdf_path(candidate))
            if limit is not None and not all:
                files = files[: max(0, limit)]
            return [_content_item_for_path(candidate, platform=self.id) for candidate in files]

        if path is not None:
            return [_content_item_for_path(path, platform=self.id)]

        target = extract_first_url(input_value)
        return [_content_item_for_reference(target, platform=self.id)]

    def extract_text(
        self,
        item: ContentItem,
        *,
        backend: str | None = None,
        backend_chain: tuple[str, ...] | None = None,
        allow_fallback: bool = True,
    ) -> TranscriptResult:
        selected = (backend_chain or (backend or self.default_backend,))[0]
        source_ref = _item_source_ref(item)
        title = item.title or _title_from_source_ref(source_ref)
        if selected == "pdf.mock":
            return MockPDFParser().parse(source_ref, title=title)
        if selected == "pdf.markitdown":
            return MarkItDownPDFParser().parse(source_ref, title=title)
        return MissingPDFParser(selected).parse(source_ref, title=title)


def _content_item_for_path(path: Path, *, platform: str) -> ContentItem:
    source_ref = path.resolve().as_uri() if path.exists() else str(path)
    content_id = _pdf_content_id(path)
    return ContentItem(
        platform=platform,
        input_type="single",
        url=source_ref,
        id=content_id,
        title=path.stem,
        source_type="document",
        raw_meta={
            "profile": "document",
            "extraction_contract_version": "document-native-v2",
            "permission_scope": "local_file",
            "local_path": str(path),
            "content_id": content_id,
        },
    )


def _content_item_for_reference(source_ref: str, *, platform: str) -> ContentItem:
    return ContentItem(
        platform=platform,
        input_type="single",
        url=source_ref,
        id=_stable_id(source_ref),
        title=_title_from_source_ref(source_ref),
        source_type="document",
        raw_meta={
            "profile": "document",
            "extraction_contract_version": "document-native-v2",
            "permission_scope": "user_accessible",
            "content_id": _stable_id(source_ref),
        },
    )


def _is_pdf_input(input_value: str) -> bool:
    path = local_path_from_input(input_value)
    if path is not None:
        if path.is_dir():
            return _directory_contains_pdf(path)
        return _is_pdf_path(path)
    parsed = urlparse(extract_first_url(input_value))
    return parsed.path.lower().endswith(DOCUMENT_EXTENSIONS)


def _is_pdf_path(path: Path) -> bool:
    return path.suffix.lower() in DOCUMENT_EXTENSIONS


def _directory_contains_pdf(path: Path) -> bool:
    try:
        return any(candidate.is_file() and _is_pdf_path(candidate) for candidate in path.rglob("*"))
    except OSError:
        return False


def _pdf_content_id(path: Path) -> str:
    if path.is_file():
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()[:12]
    return _stable_id(str(path))


def _item_source_ref(item: ContentItem) -> str:
    local_path = item.raw_meta.get("local_path") if isinstance(item.raw_meta, dict) else None
    if isinstance(local_path, str) and local_path.strip():
        return local_path
    return item.url


def _title_from_source_ref(source_ref: str) -> str:
    parsed = urlparse(source_ref)
    path = Path(parsed.path if parsed.scheme else source_ref)
    return path.stem or "PDF document"


def _stable_id(value: str) -> str:
    return hashlib.sha256(value.strip().encode("utf-8")).hexdigest()[:12]
