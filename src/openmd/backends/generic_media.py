"""Runtime providers for local generic media inputs."""

from __future__ import annotations

from openmd.adapters.local_audio.adapter import LocalAudioAdapter
from openmd.runtime.asr import get_asr_backend
from openmd.adapters.local_audio.asr import MissingASRBackend, MockASRBackend
from openmd.backends.base import (
    BackendCapability,
    BackendDescriptor,
    BackendExtractRequest,
    BackendHealth,
)
from openmd.backends.executor import BackendProviderRegistry
from openmd.backends.registry import BackendRuntimeOptions
from openmd.core.models import TranscriptResult


class LocalAudioASRProvider:
    def __init__(self, backend) -> None:
        self.backend = backend
        self.name = backend.name

    def descriptor(self) -> BackendDescriptor:
        source_level = "partial_extraction" if isinstance(self.backend, MissingASRBackend) else "asr"
        return BackendDescriptor(
            name=self.name,
            layer="local",
            capabilities=(
                BackendCapability(
                    platforms=("generic_media",),
                    source_types=("audio",),
                    input_types=("single", "batch", "collection", "generic"),
                    source_levels=(source_level,),
                    requires_network=False,
                    requires_credentials=False,
                ),
            ),
            description="Local audio ASR provider.",
            provider="openmd.local_audio",
        )

    def health(self) -> BackendHealth:
        is_available = getattr(self.backend, "is_available", None)
        if callable(is_available) and not is_available():
            return BackendHealth(
                ok=False,
                error_code="asr_backend_missing",
                error_summary=f"{self.name} is not installed or enabled.",
            )
        if isinstance(self.backend, MissingASRBackend):
            return BackendHealth(
                ok=False,
                error_code="asr_backend_missing",
                error_summary=f"{self.name} is not installed or enabled.",
            )
        return BackendHealth(ok=True, dependencies={self.name: "ok"})

    def extract(self, request: BackendExtractRequest) -> TranscriptResult:
        adapter = LocalAudioAdapter("generic_media", request.primary_backend)
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
    registry.register("local_audio.mock", LocalAudioASRProvider(MockASRBackend()))
    registry.register("asr.faster_whisper", LocalAudioASRProvider(get_asr_backend("asr.faster_whisper")))
    registry.register("asr.auto", LocalAudioASRProvider(get_asr_backend("asr.auto")))
    registry.register("asr.qwen3", LocalAudioASRProvider(get_asr_backend("asr.qwen3")))
    registry.register("asr.fallback", LocalAudioASRProvider(MissingASRBackend("asr.fallback")))
