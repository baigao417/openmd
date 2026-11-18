"""Installed platform declarations, discovered through ``openmd.platforms``.

Plugins are trusted installed Python packages. Discovery validates declarations;
it never constructs providers, reads sessions, or enables execution gates.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from importlib import metadata
from importlib.resources import files
from typing import Callable, Mapping


BUILTIN_PLATFORM_IDS = (
    "bilibili", "youtube", "weixin", "qq_group", "xiaoyuzhou", "douyin",
    "tiktok", "xiaohongshu", "xiaoe", "x", "feishu", "web_article",
    "generic_media", "pdf", "screenshot", "page_snapshot", "ai_chat",
)


@dataclass(frozen=True)
class PlatformPlugin:
    """Version 1 entry-point value. Module names live in the plugin's namespace.

    Adapter modules expose create_adapter(id, default_backend). Backend modules
    expose the existing register_backend_providers/register_discovery_providers
    callbacks and must obey BackendRuntimeOptions gates.
    """

    PLATFORM_IDS: tuple[str, ...]
    detect_platform: Callable[[str], str | None]
    adapter_modules: Mapping[str, str]
    backend_modules: Mapping[str, tuple[str, ...]]
    collection_sources: frozenset[str]
    collection_bindings: Callable[[], dict[str, tuple[str, tuple[str, ...]]]]
    resource_package: str
    routes_resource: str = "routes.json"
    card_resources: tuple[str, ...] = ("cards/builtin.json",)


@lru_cache(maxsize=1)
def load_plugins() -> tuple[PlatformPlugin, ...]:
    plugins: list[PlatformPlugin] = []
    seen = set(BUILTIN_PLATFORM_IDS)
    names: set[str] = set()
    for entry in sorted(metadata.entry_points(group="openmd.platforms"), key=lambda e: (e.name, e.value)):
        if entry.name in names:
            raise ValueError(f"plugin_entry_point_duplicate: {entry.name}")
        names.add(entry.name)
        plugin = entry.load()
        if not isinstance(plugin, PlatformPlugin):
            raise TypeError(f"plugin_descriptor_invalid: {entry.name}")
        ids = plugin.PLATFORM_IDS
        if not ids or any(not isinstance(p, str) or not p or p in seen for p in ids) or len(ids) != len(set(ids)):
            raise ValueError(f"plugin_platform_invalid_or_duplicate: {entry.name}")
        if not callable(plugin.detect_platform) or not callable(plugin.collection_bindings):
            raise TypeError(f"plugin_callback_invalid: {entry.name}")
        if set(plugin.adapter_modules) != set(ids) or set(plugin.backend_modules) != set(ids):
            raise ValueError(f"plugin_module_mapping_incomplete: {entry.name}")
        if any(not isinstance(m, str) or not m for m in plugin.adapter_modules.values()):
            raise ValueError(f"plugin_adapter_module_invalid: {entry.name}")
        if any(not isinstance(ms, tuple) or any(not isinstance(m, str) or not m for m in ms) for ms in plugin.backend_modules.values()):
            raise ValueError(f"plugin_backend_modules_invalid: {entry.name}")
        if not plugin.collection_sources <= set(ids):
            raise ValueError(f"plugin_collection_source_unknown: {entry.name}")
        bindings = plugin.collection_bindings()
        if set(bindings) != set(plugin.collection_sources):
            raise ValueError(f"plugin_collection_bindings_incomplete: {entry.name}")
        for provider, gates in bindings.values():
            if not isinstance(provider, str) or not provider or not isinstance(gates, tuple) or not set(gates) <= {
                "network", "browser", "media_download", "asr", "cloud", "account", "local_compute", "local_export",
            }:
                raise ValueError(f"plugin_collection_binding_invalid: {entry.name}")
        if not plugin.resource_package or not plugin.routes_resource or not plugin.card_resources:
            raise ValueError(f"plugin_resources_missing: {entry.name}")
        seen.update(ids)
        plugins.append(plugin)
    return tuple(plugins)


def platform_ids() -> tuple[str, ...]:
    return tuple(p for plugin in load_plugins() for p in plugin.PLATFORM_IDS)


def detect_platform(value: str) -> str | None:
    matches: list[str] = []
    for plugin in load_plugins():
        result = plugin.detect_platform(value)
        if result is not None:
            if result not in plugin.PLATFORM_IDS:
                raise ValueError("plugin_detector_returned_undeclared_platform")
            matches.append(result)
    if len(matches) > 1:
        raise ValueError("plugin_detector_ambiguous")
    return matches[0] if matches else None


def collection_sources() -> frozenset[str]:
    return frozenset(source for plugin in load_plugins() for source in plugin.collection_sources)


def collection_bindings() -> dict[str, tuple[str, tuple[str, ...]]]:
    return {source: binding for plugin in load_plugins() for source, binding in plugin.collection_bindings().items()}


def adapter_module(platform: str) -> str | None:
    for plugin in load_plugins():
        if platform in plugin.adapter_modules:
            return plugin.adapter_modules[platform]
    return None


def backend_module_names(platform: str) -> tuple[str, ...] | None:
    for plugin in load_plugins():
        if platform in plugin.backend_modules:
            return plugin.backend_modules[platform]
    return None


def routing_resources() -> tuple[tuple[PlatformPlugin, dict, list[dict]], ...]:
    """Read explicit packaged resources; never search the core filesystem."""
    import json

    resources = []
    for plugin in load_plugins():
        root = files(plugin.resource_package)
        routes = json.loads(root.joinpath(plugin.routes_resource).read_text(encoding="utf-8"))
        if routes.get("schema") != "openmd.plugin-routes/v1":
            raise ValueError("plugin_routes_schema_invalid")
        if any(r.get("source") not in plugin.PLATFORM_IDS for r in routes.get("routes", [])):
            raise ValueError("plugin_route_source_undeclared")
        cards = [json.loads(root.joinpath(p).read_text(encoding="utf-8")) for p in plugin.card_resources]
        if any(cap.get("source_id") not in plugin.PLATFORM_IDS for cap in routes.get("capabilities", [])):
            raise ValueError("plugin_capability_source_undeclared")
        resources.append((plugin, routes, cards))
    return tuple(resources)
