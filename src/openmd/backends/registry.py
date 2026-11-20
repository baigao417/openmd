"""Runtime backend provider registration helpers.

This module centralizes core provider wiring so platform lanes can add
platform-specific modules without repeatedly editing the CLI or central adapter
registry. Real network/browser providers still need explicit CLI opt-in.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from .executor import (
    BackendProviderRegistry,
    DiscoveryProviderRegistry,
    MockDiscoveryProvider,
    create_safe_backend_provider_registry,
    create_safe_discovery_provider_registry,
)
from .youtube_discovery import (
    OpenCliYouTubeChannelProvider,
    OpenCliYouTubePlaylistProvider,
    YouTubeDataApiChannelProvider,
    YouTubeDataApiPlaylistProvider,
    YOUTUBE_DATA_API_DISCOVERY_BACKEND,
    YOUTUBE_DATA_API_PLAYLIST_DISCOVERY_BACKEND,
    YOUTUBE_DISCOVERY_BACKEND,
    YOUTUBE_PLAYLIST_DISCOVERY_BACKEND,
)
from .youtube_transcript import YOUTUBE_TRANSCRIPT_BACKEND, YouTubeTranscriptApiProvider


@dataclass(frozen=True)
class BackendRuntimeOptions:
    """Safe runtime gates shared by extraction and discovery provider setup."""

    backend_override: str | None = None
    ocr_engine: str = "auto"
    requested_discovery_provider: str | None = None
    enable_network_backends: bool = False
    enable_browser_backends: bool = False
    enable_media_download_backends: bool = False
    enable_asr_backends: bool = False
    enable_cloud_backends: bool = False
    enable_local_export_backends: bool = False
    keep_media_artifacts: bool = False
    media_artifact_dir: str | None = None


ExtensionModuleLoader = Callable[[str], Iterable[object]]


DISCOVERY_BACKEND_ALIASES: dict[str, str] = {
    "api": YOUTUBE_DATA_API_DISCOVERY_BACKEND,
    "data-api": YOUTUBE_DATA_API_DISCOVERY_BACKEND,
    "data-api-playlist": YOUTUBE_DATA_API_PLAYLIST_DISCOVERY_BACKEND,
    "mock": "mock.discovery",
    "skeleton": "skeleton.discovery",
    "opencli": YOUTUBE_DISCOVERY_BACKEND,
    "opencli-playlist": YOUTUBE_PLAYLIST_DISCOVERY_BACKEND,
    "playlist": YOUTUBE_PLAYLIST_DISCOVERY_BACKEND,
    "youtube-data-api": YOUTUBE_DATA_API_DISCOVERY_BACKEND,
    "youtube-data-api-playlist": YOUTUBE_DATA_API_PLAYLIST_DISCOVERY_BACKEND,
    "youtube-playlist": YOUTUBE_PLAYLIST_DISCOVERY_BACKEND,
    "wechat-article-exporter-api": "wechat-article-exporter.http",
    "wechat-article-exporter-http": "wechat-article-exporter.http",
    "weixin-exporter-api": "wechat-article-exporter.http",
    "weixin-exporter-http": "wechat-article-exporter.http",
    "weixin-http": "wechat-article-exporter.http",
    "openmd-douyin": "opencli.openmd-douyin.creator-videos",
    "openmd-douyin-creator-videos": "opencli.openmd-douyin.creator-videos",
}


def normalize_discovery_backend(value: str | None) -> str | None:
    if value is None:
        return None
    return DISCOVERY_BACKEND_ALIASES.get(value, value)


def create_runtime_backend_provider_registry(
    platform: str,
    options: BackendRuntimeOptions,
    *,
    extension_module_loader: ExtensionModuleLoader | None = None,
) -> BackendProviderRegistry:
    registry = create_safe_backend_provider_registry()
    _register_builtin_backend_providers(registry, platform, options)
    _register_extension_backend_providers(
        registry,
        platform,
        options,
        extension_module_loader=extension_module_loader,
    )
    from openmd.routing.providers import register_card_providers

    register_card_providers(registry, platform, options)
    return registry


def create_runtime_discovery_provider_registry(
    platform: str,
    options: BackendRuntimeOptions,
    *,
    extension_module_loader: ExtensionModuleLoader | None = None,
) -> DiscoveryProviderRegistry:
    registry = create_safe_discovery_provider_registry()
    requested_provider = normalize_discovery_backend(options.requested_discovery_provider)
    if requested_provider is None and options.backend_override == "mock":
        registry.register(
            "mock.discovery",
            MockDiscoveryProvider(),
            platforms=(platform,),
        )

    should_register_network_provider = (
        options.enable_network_backends
        and platform == "youtube"
        and (requested_provider is None or requested_provider == YOUTUBE_DATA_API_DISCOVERY_BACKEND)
    )
    if should_register_network_provider:
        registry.register(
            YOUTUBE_DATA_API_DISCOVERY_BACKEND,
            YouTubeDataApiChannelProvider(),
            platforms=("youtube",),
            default_for_platforms=requested_provider is None,
        )

    should_register_network_playlist_provider = (
        options.enable_network_backends
        and platform == "youtube"
        and (requested_provider is None or requested_provider == YOUTUBE_DATA_API_PLAYLIST_DISCOVERY_BACKEND)
    )
    if should_register_network_playlist_provider:
        registry.register(
            YOUTUBE_DATA_API_PLAYLIST_DISCOVERY_BACKEND,
            YouTubeDataApiPlaylistProvider(),
            platforms=("youtube",),
            default_for_platforms=requested_provider == YOUTUBE_DATA_API_PLAYLIST_DISCOVERY_BACKEND,
        )

    should_register_browser_channel_provider = (
        options.enable_browser_backends
        and platform == "youtube"
        and (requested_provider is None or requested_provider == YOUTUBE_DISCOVERY_BACKEND)
    )
    if should_register_browser_channel_provider:
        registry.register(
            YOUTUBE_DISCOVERY_BACKEND,
            OpenCliYouTubeChannelProvider(),
            platforms=("youtube",),
            default_for_platforms=requested_provider is None and not should_register_network_provider,
        )

    should_register_browser_playlist_provider = (
        options.enable_browser_backends
        and platform == "youtube"
        and (requested_provider is None or requested_provider == YOUTUBE_PLAYLIST_DISCOVERY_BACKEND)
    )
    if should_register_browser_playlist_provider:
        registry.register(
            YOUTUBE_PLAYLIST_DISCOVERY_BACKEND,
            OpenCliYouTubePlaylistProvider(),
            platforms=("youtube",),
            default_for_platforms=requested_provider == YOUTUBE_PLAYLIST_DISCOVERY_BACKEND,
        )

    _register_extension_discovery_providers(
        registry,
        platform,
        options,
        extension_module_loader=extension_module_loader,
    )
    return registry


def _register_builtin_backend_providers(
    registry: BackendProviderRegistry,
    platform: str,
    options: BackendRuntimeOptions,
) -> None:
    if options.enable_network_backends and platform == "youtube":
        registry.register(YOUTUBE_TRANSCRIPT_BACKEND, YouTubeTranscriptApiProvider())


def _register_extension_backend_providers(
    registry: BackendProviderRegistry,
    platform: str,
    options: BackendRuntimeOptions,
    *,
    extension_module_loader: ExtensionModuleLoader | None = None,
) -> None:
    for module in _load_backend_extension_modules(platform, extension_module_loader):
        registrar = getattr(module, "register_backend_providers", None)
        if registrar is not None:
            if not callable(registrar):
                raise TypeError("register_backend_providers must be callable")
            registrar(registry, options)


def _register_extension_discovery_providers(
    registry: DiscoveryProviderRegistry,
    platform: str,
    options: BackendRuntimeOptions,
    *,
    extension_module_loader: ExtensionModuleLoader | None = None,
) -> None:
    for module in _load_backend_extension_modules(platform, extension_module_loader):
        registrar = getattr(module, "register_discovery_providers", None)
        if registrar is not None:
            if not callable(registrar):
                raise TypeError("register_discovery_providers must be callable")
            registrar(registry, options)


def _load_backend_extension_modules(
    platform: str,
    extension_module_loader: ExtensionModuleLoader | None = None,
) -> tuple[object, ...]:
    if extension_module_loader is not None:
        return tuple(extension_module_loader(platform))

    from openmd.core.plugins import backend_module_names

    declared = backend_module_names(platform)
    if declared is not None:
        modules = tuple(importlib.import_module(name) for name in declared)
        for module in modules:
            if not any(callable(getattr(module, callback, None)) for callback in ("register_backend_providers", "register_discovery_providers")):
                raise TypeError(f"plugin_provider_registrar_missing: {module.__name__}")
        return modules

    modules: list[object] = []
    for module_name in _backend_extension_module_names(platform):
        try:
            modules.append(importlib.import_module(module_name))
        except ModuleNotFoundError as exc:
            if exc.name == module_name:
                continue
            raise
    return tuple(modules)


def _backend_extension_module_names(platform: str) -> tuple[str, str]:
    if platform == "page_snapshot":
        return ("openmd.backends.snapshot",)
    return (f"openmd.backends.{platform}", f"openmd.backends.{platform}_providers")
