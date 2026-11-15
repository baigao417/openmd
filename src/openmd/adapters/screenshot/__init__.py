"""V1 long screenshot OCR adapter factory."""

from __future__ import annotations

from openmd.adapters.base import PlatformAdapter

from .adapter import ScreenshotOcrAdapter


def create_adapter(platform_id: str, default_backend: str) -> PlatformAdapter:
    return ScreenshotOcrAdapter(platform_id, default_backend)
