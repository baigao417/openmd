"""Runtime providers for long screenshot OCR inputs."""

from __future__ import annotations

from openmd.adapters.screenshot.adapter import ScreenshotOcrAdapter
from openmd.adapters.screenshot.backends.rapidocr import RapidOCRBackend
from openmd.adapters.screenshot.ocr import MissingOCRBackend, MockOCRBackend
from openmd.backends.base import (
    BackendCapability,
    BackendDescriptor,
    BackendExtractRequest,
    BackendHealth,
)
from openmd.backends.executor import BackendProviderRegistry
from openmd.backends.registry import BackendRuntimeOptions
from openmd.core.models import TranscriptResult


class ScreenshotOCRProvider:
    def __init__(self, backend) -> None:
        self.backend = backend
        self.name = backend.name

    def descriptor(self) -> BackendDescriptor:
        source_level = "ocr" if isinstance(self.backend, MockOCRBackend) else "partial_extraction"
        return BackendDescriptor(
            name=self.name,
            layer="local",
            capabilities=(
                BackendCapability(
                    platforms=("screenshot",),
                    source_types=("image_text",),
                    input_types=("single", "batch", "collection", "generic"),
                    source_levels=(source_level,),
                    requires_network=False,
                    requires_credentials=False,
                ),
            ),
            description="Long screenshot OCR provider.",
            provider="openmd.screenshot",
        )

    def health(self) -> BackendHealth:
        is_available = getattr(self.backend, "is_available", None)
        if callable(is_available) and not is_available():
            return BackendHealth(
                ok=False,
                error_code="ocr_backend_missing",
                error_summary=f"{self.name} is not installed or enabled.",
            )
        if isinstance(self.backend, MissingOCRBackend):
            return BackendHealth(
                ok=False,
                error_code="ocr_backend_missing",
                error_summary=f"{self.name} is not installed or enabled.",
            )
        return BackendHealth(ok=True, dependencies={self.name: "ok"})

    def extract(self, request: BackendExtractRequest) -> TranscriptResult:
        adapter = ScreenshotOcrAdapter("screenshot", request.primary_backend)
        return adapter.extract_text(
            request.item,
            backend=request.primary_backend,
            backend_chain=(request.primary_backend,),
            allow_fallback=False,
        )


def register_backend_providers(
    registry: BackendProviderRegistry,
    options: BackendRuntimeOptions,
) -> None:
    from openmd.backends.ocr import register_ocr_providers

    register_ocr_providers(registry, options)
    registry.register("screenshot.mock", ScreenshotOCRProvider(MockOCRBackend()))
    registry.register("ocr.rapidocr", ScreenshotOCRProvider(RapidOCRBackend()))
    registry.register("ocr.paddle", ScreenshotOCRProvider(MissingOCRBackend("ocr.paddle")))
    registry.register("ocr.tesseract", ScreenshotOCRProvider(MissingOCRBackend("ocr.tesseract")))
