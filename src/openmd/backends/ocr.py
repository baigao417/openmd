"""OCR providers shared by screenshot and document route chains."""

from __future__ import annotations

from openmd.backends.base import BackendCapability, BackendDescriptor, BackendHealth
from openmd.ocr.pipeline import LocalOCRBackend


class OCRRouterProvider:
    def __init__(self, name: str, options) -> None:
        self.name, self.options = name, options

    def descriptor(self):
        return BackendDescriptor(name=self.name, layer="local", provider="openmd.ocr", description="Serial local Ollama OCR with monitored fallback and page receipts.", capabilities=(BackendCapability(platforms=("screenshot", "pdf"), source_types=("image_text", "document"), input_types=("single", "batch", "collection", "generic"), source_levels=("ocr",), requires_network=True),))

    def health(self):
        if not self.options.enable_network_backends:
            return BackendHealth(ok=False, error_code="ocr_permission_required", error_summary="Enable network backends explicitly to call the configured Ollama API.")
        from openmd.ocr.config import settings
        from openmd.ocr.ollama import probe_ollama

        report = probe_ollama(settings())
        key = "minicpm_model" if self.name == "ocr.minicpm" else "ovis_model"
        ready = bool(report["models"].get(key, {}).get("available"))
        return BackendHealth(ok=ready, dependencies={"ollama": str(report.get("version"))}, error_code=None if ready else report.get("error_code"))

    def extract(self, request):
        if request.item.platform == "pdf":
            from openmd.ocr.documents import extract_pdf

            return extract_pdf(request.item, backend=self.name, allow_ocr=self.options.enable_network_backends)
        backend = LocalOCRBackend(self.name)
        if not self.options.enable_network_backends:
            return backend.failed("ocr_permission_required", request.item.title or "screenshot", "Use --enable-network-backends to call the configured Ollama API.")
        source = request.item.raw_meta.get("local_path") or request.item.url
        return backend.recognize(source, title=request.item.title or "screenshot")


def register_ocr_providers(registry, options):
    for name in ("ocr.ovis3", "ocr.minicpm", "ocr.chain"):
        registry.register(name, OCRRouterProvider(name, options))
