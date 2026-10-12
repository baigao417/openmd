"""Adapter registry and default backend preferences."""

from __future__ import annotations

import importlib
from dataclasses import dataclass

from collections.abc import Callable, Iterable

from openmd.core.detector import PLATFORM_IDS, detect_platform

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
    }
)


DEFAULT_BACKENDS: dict[str, tuple[str, ...]] = {
    "bilibili": (
        "bilibili-harvest.core",
        "opencli.bilibili.subtitle",
        "yt-dlp.bilibili",
        "asr.fallback",
    ),
    "youtube": (
        "youtube-transcript-api",
        "opencli.youtube.transcript",
        "yt-dlp.youtube",
        "asr.fallback",
    ),
    "weixin": (
        "weixin.article.local",
        "wechat-article-exporter.download",
        "opencli.weixin.download",
        "opencli.web.read",
    ),
    "wechat_group": ("opencli.wx.history",),
    "wechat_moments": ("opencli.wx.sns-feed",),
    "qq_group": ("qq_group.export",),
    "xiaoyuzhou": (
        "opencli.xiaoyuzhou.transcript",
        "podwise.transcript",
        "opencli.web.read",
    ),
    "douyin": ("douyin.metadata",),
    "tiktok": ("opencli.tiktok.user",),
    "xiaohongshu": ("xiaohongshu.composite",),
    "xiaoe": ("opencli.xiaoe.transcript", "opencli.xiaoe.content"),
    "x": ("opencli.twitter.thread",),
    "web_article": (
        "web_article.trafilatura",
        "opencli.web.read",
    ),
    "feishu": (
        "lark-cli.docs.fetch",
        "lark-cli.drive.export-markdown",
        "lark-cli.markdown.fetch",
        "manual.markdown-export",
    ),
    "generic_media": ("asr.fallback",),
    "pdf": ("pdf.markitdown", "pdf.docling", "paddleocr.pdf"),
    "screenshot": ("ocr.rapidocr", "ocr.paddle", "ocr.tesseract"),
}


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
        self._backends: dict[str, tuple[str, ...]] = dict(DEFAULT_BACKENDS)

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
    for platform_id in PLATFORM_IDS:
        backend = DEFAULT_BACKENDS.get(platform_id, ("mock",))[0]
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
    module_name = f"openmd.adapters.{platform_id}"
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        if exc.name == module_name:
            return None
        raise
    factory = getattr(module, "create_adapter", None)
    if factory is None:
        return None
    if not callable(factory):
        raise TypeError(f"{module_name}.create_adapter must be callable")
    return factory
