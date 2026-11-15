"""Validated bundled cards and their read-only runtime projections."""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from functools import lru_cache
from importlib.resources import files
from pathlib import Path
from typing import Any

from openmd.core.local_config import load_local_config, local_config_path
from openmd.core.models import SOURCE_LEVEL_VALUES

GATES = frozenset({"network", "browser", "media_download", "asr", "cloud", "account", "local_compute", "local_export"})
READERS = frozenset({"markdown_file", "json_stdout", "json_file", "subtitle_dir", "media_dir"})


@dataclass(frozen=True)
class Catalog:
    cards: dict[str, dict[str, Any]]
    capabilities: dict[str, dict[str, Any]]
    routes: tuple[dict[str, Any], ...]
    version: str
    capability_metadata: tuple[dict[str, Any], ...]


def validate_catalog(cards: list[dict[str, Any]], route_data: dict[str, Any]) -> Catalog:
    if route_data.get("schema") != "openmd.routes/v1" or not route_data.get("version"):
        raise ValueError("routes_schema_invalid")
    tools: dict[str, dict[str, Any]] = {}
    capabilities: dict[str, dict[str, Any]] = {}
    for card in cards:
        tool_id = card.get("id")
        if card.get("schema") != "openmd.tool-card/v1" or not isinstance(tool_id, str) or not tool_id or tool_id in tools:
            raise ValueError("tool_card_schema_invalid_or_duplicate")
        if card.get("kind") not in {"command", "python_entry", "python_script", "builtin"}:
            raise ValueError(f"tool_card_kind_invalid: {tool_id}")
        if not isinstance(card.get("capabilities"), list) or not card["capabilities"]:
            raise ValueError(f"tool_card_capabilities_missing: {tool_id}")
        tools[tool_id] = card
        for cap in card["capabilities"]:
            cap_id = cap.get("id")
            if not isinstance(cap_id, str) or not cap_id or cap_id in capabilities or not cap.get("sources"):
                raise ValueError("tool_capability_invalid_or_duplicate")
            if not set(cap.get("gates", [])) <= GATES:
                raise ValueError(f"tool_gate_invalid: {cap_id}")
            if card["kind"] == "builtin":
                if cap.get("provider") != cap_id or "argv" in cap:
                    raise ValueError(f"builtin_card_invalid: {cap_id}")
            else:
                if set(cap.get("gates", [])) & {"local_compute", "local_export"}:
                    raise ValueError(f"tool_gate_invalid: {cap_id}")
                if not card.get("version") or not card.get("license") or not card.get("installation") or not card.get("evidence"):
                    raise ValueError(f"tool_evidence_or_identity_missing: {tool_id}")
                if not isinstance(cap.get("argv"), list) or not all(isinstance(x, str) for x in cap["argv"]):
                    raise ValueError(f"tool_argv_invalid: {cap_id}")
                if cap.get("reader", {}).get("type") not in READERS or cap.get("source_level") not in SOURCE_LEVEL_VALUES:
                    raise ValueError(f"tool_output_contract_invalid: {cap_id}")
                if not 0 < cap.get("timeout_s", 0) <= 3600:
                    raise ValueError(f"tool_timeout_invalid: {cap_id}")
                if cap.get("reader", {}).get("type") == "media_dir":
                    if cap.get("then") != "asr.auto" or not {"media_download", "asr"} <= set(cap.get("gates", [])):
                        raise ValueError(f"tool_media_requires_asr: {cap_id}")
            capabilities[cap_id] = {**cap, "card_id": tool_id}
    seen: set[tuple[str, str]] = set()
    routes = route_data.get("routes")
    if not isinstance(routes, list):
        raise ValueError("routes_missing")
    for route in routes:
        key = (route.get("source"), route.get("capability"))
        chain = route.get("chain")
        if not all(isinstance(x, str) and x for x in key) or key in seen or not isinstance(chain, list) or not chain:
            raise ValueError("route_invalid_or_duplicate")
        seen.add(key)
        if len(chain) != len(set(chain)):
            raise ValueError("route_backend_duplicate")
        for backend in chain:
            if backend not in capabilities or route["source"] not in capabilities[backend]["sources"]:
                raise ValueError(f"route_backend_unknown: {backend}")
    return Catalog(tools, capabilities, tuple(routes), route_data["version"], tuple(route_data.get("capabilities", [])))


@lru_cache(maxsize=1)
def load_catalog() -> Catalog:
    resource = files("openmd.routing")
    cards = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(resource.joinpath("cards").iterdir(), key=lambda p: p.name) if p.name.endswith(".json")]
    return validate_catalog(cards, json.loads(resource.joinpath("routes.json").read_text(encoding="utf-8")))


def overlay_file() -> Path:
    config = load_local_config()
    if config.get("route_overlay"):
        return Path(config["route_overlay"]).expanduser().resolve()
    location = local_config_path()
    return (location.parent if location else Path.home() / ".openmd") / "route-overrides.json"


def effective_routes(source: str | None = None) -> list[dict[str, Any]]:
    routes = copy.deepcopy(list(load_catalog().routes))
    path = overlay_file()
    if path.is_file():
        from .lifecycle import apply_overlay

        routes = apply_overlay(routes, path)
    for route in routes:
        route.setdefault("overlay_applied", False)
    return [r for r in routes if source is None or r["source"] == source]


def default_backend_chains(*, effective: bool = True) -> dict[str, tuple[str, ...]]:
    result: dict[str, list[str]] = {}
    priority = {"transcript": 0, "content": 0, "media": 1}
    routes = effective_routes() if effective else load_catalog().routes
    for route in sorted(routes, key=lambda r: priority.get(r["capability"], 2)):
        chain = result.setdefault(route["source"], [])
        chain.extend(backend for backend in route["chain"] if backend not in chain)
    return {source: tuple(chain) for source, chain in result.items()}


def route_view(source: str | None = None, *, effective: bool = True) -> dict[str, Any]:
    catalog = load_catalog()
    routes = effective_routes(source) if effective else [{**r, "overlay_applied": False} for r in catalog.routes if source is None or r["source"] == source]
    backend_ids = {b for r in routes for b in r["chain"]}
    tool_ids = {catalog.capabilities[b]["card_id"] for b in backend_ids}
    return {"schema": "openmd.route-view/v1", "version": catalog.version, "overlay_applied": any(r["overlay_applied"] for r in routes), "routes": routes, "tools": [{"id": c["id"], "kind": c["kind"], "version": c.get("version"), "license": c.get("license")} for c in catalog.cards.values() if c["id"] in tool_ids]}


def route_trace(source: str, backend: str | None) -> dict[str, Any]:
    """Safe provenance fields shared by providers, receipts and runlogs."""
    catalog = load_catalog()
    capability = catalog.capabilities.get(backend or "", {})
    return {
        "card_id": capability.get("card_id"),
        "route_version": catalog.version,
        "overlay_applied": any(route["overlay_applied"] for route in effective_routes(source)),
    }
