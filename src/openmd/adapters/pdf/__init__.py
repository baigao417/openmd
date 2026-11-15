"""V1 PDF document adapter factory."""

from __future__ import annotations

from openmd.adapters.base import PlatformAdapter

from .adapter import PdfDocumentAdapter


def create_adapter(platform_id: str, default_backend: str) -> PlatformAdapter:
    return PdfDocumentAdapter(platform_id, default_backend)
