"""Adapter registry and default backend preferences."""

from __future__ import annotations

import importlib
from dataclasses import dataclass

from collections.abc import Callable, Iterable

from openmd.core.detector import detect_platform
from openmd.core import plugins

from .base import PlatformAdapter, StubPlatformAdapter
from .opencli import OpenCliPlatformAdapter


AdapterFactory = Callable[[str, str], PlatformAdapter]
AdapterFactoryLoader = Callable[[str], AdapterFactory | None]


OPENCLI_ADAPTER_PLATFORMS: frozenset[str] = frozenset(
    {
        "bilibili",
        "youtube",
        "weixin",
        "xiaoyuzhou",
        "xiaohongshu",
        "xiaoe",
        "x",
        "ai_chat",
    }
)


from openmd.routing.loader import default_backend_chains



@dataclass(frozen=True)
class BackendSelection:
    platform: str
    primary: str
    fallbacks: tuple[str, ...]

    @property
    def chain(self) -> tuple[str, ...]:
        return (self.primary, *self.fallbacks)


class AdapterRegistry:
    def __init__(self) -> None:
        self._adapters: dict[str, PlatformAdapter] = {}
        self._backends: dict[str, tuple[str, ...]] = default_backend_chains()

    def register(
        self,
        adapter: PlatformAdapter,
        *,
        backends: Iterable[str] | None = None,
    ) -> None:
        self._adapters[adapter.id] = adapter
        if backends is not None:
            self._backends[adapter.id] = tuple(backends)

    def get_adapter(self, platform: str) -> PlatformAdapter:
        try:
            return self._adapters[platform]
        except KeyError as exc:
            raise KeyError(f"no adapter registered for platform: {platform}") from exc

    def adapter_for(self, input_value: str) -> PlatformAdapter:
        return self.get_adapter(detect_platform(input_value))

    def backend_chain(
        self,
        platform: str,
        requested: str | None = None,
        *,
        allow_fallback: bool = True,
    ) -> tuple[str, ...]:
        if requested:
            return (requested,)
        chain = self._backends.get(platform, ("mock",))
        if allow_fallback:
            return chain
        return chain[:1]

    def select_backend(
        self,
        platform: str,
        requested: str | None = None,
        *,
        allow_fallback: bool = True,
    ) -> BackendSelection:
        chain = self.backend_chain(platform, requested, allow_fallback=allow_fallback)
        primary = chain[0]
        fallbacks = chain[1:] if allow_fallback else ()
        return BackendSelection(platform=platform, primary=primary, fallbacks=fallbacks)


def create_default_registry(
    *,
    adapter_factory_loader: AdapterFactoryLoader | None = None,
) -> AdapterRegistry:
    registry = AdapterRegistry()
    load_factory = adapter_factory_loader or load_adapter_factory
    for platform_id in (*plugins.BUILTIN_PLATFORM_IDS, *plugins.platform_ids()):
        backend = registry.backend_chain(platform_id)[0]
        registry.register(_create_platform_adapter(platform_id, backend, load_factory))
    return registry


def _create_platform_adapter(
    platform_id: str,
    default_backend: str,
    load_factory: AdapterFactoryLoader,
) -> PlatformAdapter:
    factory = load_factory(platform_id)
    if factory is not None:
        adapter = factory(platform_id, default_backend)
        if adapter.id != platform_id:
            raise ValueError(
                f"adapter factory for {platform_id} returned adapter id {adapter.id!r}"
            )
        return adapter
    if platform_id in OPENCLI_ADAPTER_PLATFORMS:
        return OpenCliPlatformAdapter(platform_id, default_backend)
    return StubPlatformAdapter(platform_id, default_backend)


def load_adapter_factory(platform_id: str) -> AdapterFactory | None:
    """Load an optional platform adapter factory by naming convention.

    Platform lanes can add ``openmd.adapters.<platform_id>`` with a
    ``create_adapter(platform_id, default_backend)`` function. The core registry
    will pick it up without platform lanes editing this shared file.
    """
    plugin_module = plugins.adapter_module(platform_id)
    module_name = plugin_module or f"openmd.adapters.{platform_id}"
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        if exc.name == module_name and plugin_module is None:
            return None
        raise
    factory = getattr(module, "create_adapter", None)
    if factory is None:
        if plugin_module is not None:
            raise TypeError(f"{module_name}.create_adapter is required")
        return None
    if not callable(factory):
        raise TypeError(f"{module_name}.create_adapter must be callable")
    return factory
