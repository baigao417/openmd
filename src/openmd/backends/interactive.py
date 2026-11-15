"""Defer daily browser consent until execution reaches a browser candidate."""

from __future__ import annotations

from dataclasses import replace

from openmd.core.models import TranscriptResult
from openmd.routing.loader import load_catalog
from openmd.routing.providers import gates_open

from .executor import BackendProviderRegistry
from .registry import create_runtime_backend_provider_registry


class DeferredBrowserRegistry(BackendProviderRegistry):
    def __init__(self, original, platform, args, authorize):
        super().__init__()
        self.original = original
        self.platform = platform
        self.args = args
        self.authorize = authorize

    def provider_for(self, backend):
        provider = self.original.provider_for(backend)
        # The normal registry uses safe skeleton placeholders for closed gates.
        from .executor import SkeletonBackendProvider
        if provider is not None and not isinstance(provider, SkeletonBackendProvider):
            return provider
        capability = load_catalog().capabilities.get(backend, {})
        if "browser" not in capability.get("gates", []):
            return provider
        from openmd.cli import _backend_runtime_options

        possible = replace(_backend_runtime_options(self.args), enable_browser_backends=True)
        if not gates_open(capability, possible):
            return provider
        return _DeferredBrowserProvider(self, backend)


class _DeferredBrowserProvider:
    def __init__(self, registry, backend):
        self.registry = registry
        self.name = backend

    def extract(self, request):
        if self.registry.authorize(self.name):
            from openmd.cli import _backend_runtime_options

            registry = create_runtime_backend_provider_registry(
                self.registry.platform, _backend_runtime_options(self.registry.args)
            )
            provider = registry.provider_for(self.name)
            if provider is not None:
                self.registry.original = registry
                return provider.extract(request)
        return TranscriptResult(
            text="", status="failed", source_level="partial_extraction", backend=self.name,
            error_code="browser_permission_required",
            error_summary="此路线需要浏览器读取许可；可在提示中允许一次，或显式使用 --enable-browser-backends。",
        )
