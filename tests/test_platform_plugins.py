"""Third-party plugin contract, without a maintainer package or live network."""
from __future__ import annotations

import json
import sys
from dataclasses import replace
from types import ModuleType, SimpleNamespace

import pytest

from openmd.core import plugins
from openmd.routing.loader import load_catalog


@pytest.fixture
def installed(monkeypatch, tmp_path):
    package = tmp_path / "sample_platform_resources"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    routes = {"schema": "openmd.plugin-routes/v1", "routes": [
        {"source": "sample_feed", "capability": "content", "chain": ["sample.content"]}],
        "capabilities": [{"source_id": "sample_feed", "state": "partial"}]}
    cards = {"schema": "openmd.tool-card/v1", "id": "sample-tool", "kind": "builtin", "capabilities": [
        {"id": "sample.content", "provider": "sample.content", "sources": ["sample_feed"], "gates": ["browser"]}]}
    (package / "routes.json").write_text(json.dumps(routes), encoding="utf-8")
    (package / "card.json").write_text(json.dumps(cards), encoding="utf-8")
    descriptor = plugins.PlatformPlugin(
        PLATFORM_IDS=("sample_feed",), detect_platform=lambda value: "sample_feed" if value.startswith("sample:") else None,
        adapter_modules={"sample_feed": "sample_platform.adapter"},
        backend_modules={"sample_feed": ("sample_platform.backend",)},
        collection_sources=frozenset({"sample_feed"}),
        collection_bindings=lambda: {"sample_feed": ("sample.content", ("browser",))},
        resource_package=package.name, card_resources=("card.json",),
    )
    entries = []

    def entry(plugin=descriptor, name="sample"):
        return SimpleNamespace(name=name, value="sample_platform:plugin", load=lambda: plugin)

    monkeypatch.setattr(plugins.metadata, "entry_points", lambda *, group: entries if group == "openmd.platforms" else [])
    plugins.load_plugins.cache_clear()
    load_catalog.cache_clear()
    yield SimpleNamespace(plugin=descriptor, entries=entries, entry=entry, package=package, routes=routes, cards=cards)
    plugins.load_plugins.cache_clear()
    load_catalog.cache_clear()
    sys.modules.pop(package.name, None)


def test_no_plugins_keeps_core_platforms_and_routes(installed):
    from openmd.core.detector import detect_platform
    assert plugins.platform_ids() == ()
    assert plugins.collection_bindings() == {}
    assert plugins.backend_module_names("bilibili") is None
    assert detect_platform("https://www.bilibili.com/video/BV123/") == "bilibili"
    assert "sample_feed" not in {route["source"] for route in load_catalog().routes}


def test_descriptor_drives_detection_adapter_provider_and_catalog(installed, monkeypatch):
    from openmd.adapters.base import StubPlatformAdapter
    from openmd.adapters.registry import create_default_registry
    from openmd.backends.executor import MockDiscoveryProvider, SkeletonDiscoveryProvider
    from openmd.backends.base import DiscoveryRequest
    from openmd.backends.registry import BackendRuntimeOptions, create_runtime_discovery_provider_registry
    from openmd.core.detector import detect_platform
    from openmd.core.planner import _inspect_input
    adapter = ModuleType("sample_platform.adapter")
    adapter.create_adapter = lambda platform, backend: StubPlatformAdapter(platform, backend)
    backend = ModuleType("sample_platform.backend")

    def register(registry, options):
        if options.enable_browser_backends:
            registry.register("sample.content", MockDiscoveryProvider(), platforms=("sample_feed",), default_for_platforms=True)

    backend.register_discovery_providers = register
    monkeypatch.setitem(sys.modules, adapter.__name__, adapter)
    monkeypatch.setitem(sys.modules, backend.__name__, backend)
    installed.entries.append(installed.entry())
    assert detect_platform("sample://feed/one") == "sample_feed"
    assert _inspect_input("sample://feed/one")[:2] == ("sample_feed", "collection")
    assert plugins.collection_bindings() == {"sample_feed": ("sample.content", ("browser",))}
    assert create_default_registry().get_adapter("sample_feed").id == "sample_feed"
    catalog = load_catalog()
    assert catalog.capabilities["sample.content"]["card_id"] == "sample-tool"
    assert any(r["source"] == "sample_feed" for r in catalog.routes)
    assert not any(r["source"] == "sample_feed" for r in load_catalog(include_plugins=False).routes)
    disabled = create_runtime_discovery_provider_registry("sample_feed", BackendRuntimeOptions())
    enabled = create_runtime_discovery_provider_registry("sample_feed", BackendRuntimeOptions(enable_browser_backends=True))
    request = DiscoveryRequest(platform="sample_feed", input_url="sample://feed/one")
    assert isinstance(disabled.provider_for(request, requested_provider="sample.content"), SkeletonDiscoveryProvider)
    assert isinstance(enabled.provider_for(request, requested_provider="sample.content"), MockDiscoveryProvider)


@pytest.mark.parametrize("platform", ["bilibili", "sample_feed"])
def test_duplicate_platform_rejected(installed, platform):
    if platform == "sample_feed":
        installed.entries.append(installed.entry(name="first"))
    installed.entries.append(installed.entry(replace(installed.plugin, PLATFORM_IDS=(platform,)), name="second"))
    with pytest.raises(ValueError, match="plugin_platform_invalid_or_duplicate"):
        plugins.load_plugins()


def test_duplicate_entry_point_rejected(installed):
    installed.entries.extend([installed.entry(), installed.entry()])
    with pytest.raises(ValueError, match="plugin_entry_point_duplicate"):
        plugins.load_plugins()


@pytest.mark.parametrize("change,error", [
    ({"adapter_modules": {}}, "module_mapping_incomplete"),
    ({"backend_modules": {}}, "module_mapping_incomplete"),
    ({"collection_bindings": lambda: {}}, "collection_bindings_incomplete"),
    ({"detect_platform": lambda value: "unregistered"}, "detector_returned_undeclared"),
])
def test_incomplete_or_lying_descriptor_rejected(installed, change, error):
    installed.entries.append(installed.entry(replace(installed.plugin, **change)))
    with pytest.raises(ValueError, match=error):
        plugins.detect_platform("sample://one")


@pytest.mark.parametrize("kind,error", [
    ("card", "tool_card_schema_invalid_or_duplicate"),
    ("capability", "tool_capability_invalid_or_duplicate"),
    ("route", "route_invalid_or_duplicate"),
    ("metadata", "capability_metadata_invalid_or_duplicate"),
])
def test_routing_conflicts_fail_closed(installed, kind, error):
    installed.entries.append(installed.entry())
    if kind == "card":
        installed.cards["id"] = "builtin"
    elif kind == "capability":
        installed.cards["capabilities"].append({"id": "web_article.trafilatura", "provider": "web_article.trafilatura", "sources": ["sample_feed"], "gates": ["browser"]})
    elif kind == "route":
        installed.routes["routes"].append(installed.routes["routes"][0])
    else:
        installed.routes["capabilities"].append(installed.routes["capabilities"][0])
    (installed.package / "routes.json").write_text(json.dumps(installed.routes), encoding="utf-8")
    (installed.package / "card.json").write_text(json.dumps(installed.cards), encoding="utf-8")
    with pytest.raises(ValueError, match=error):
        load_catalog()


def test_missing_declared_provider_module_does_not_fall_back_to_core_namespace(installed):
    from openmd.backends.registry import _load_backend_extension_modules
    installed.entries.append(installed.entry())
    with pytest.raises(ModuleNotFoundError):
        _load_backend_extension_modules("sample_feed")
