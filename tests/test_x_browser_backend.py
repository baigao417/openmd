from __future__ import annotations

import json

from openmd.backends.base import BackendExtractRequest
from openmd.backends.x_browser import (
    X_BROWSER_BACKEND,
    XLoggedInBrowserProvider,
)
from openmd.core.models import ContentItem


def _request(url: str = "https://x.com/writer/status/1234567890") -> BackendExtractRequest:
    return BackendExtractRequest(
        item=ContentItem(platform="x", input_type="single", url=url, source_type="article"),
        backend_chain=(X_BROWSER_BACKEND,),
    )


def test_logged_in_browser_provider_reads_one_x_tab_without_exporting_secrets():
    calls = []

    def runner(args, *, timeout):
        calls.append((args, timeout))
        return 0, json.dumps({"ok": True, "data": [{"result": {
            "ok": True,
            "text": "A complete note body.",
            "legacy_text": "A short prefix.",
            "title": "A complete note body.",
            "author": "writer",
            "created_at": "2026-11-22T12:00:00Z",
            "images": [],
        }}]}), ""

    result = XLoggedInBrowserProvider(runner=runner).extract(_request())

    assert result.status == "verified"
    assert result.source_level == "browser_rendered"
    assert result.metadata["api_layer"] == "logged_in_browser_x_api"
    assert result.backend == X_BROWSER_BACKEND
    assert "A complete note body." in result.text
    assert "secret" not in json.dumps(result.metadata).lower()
    assert len(calls) == 1
    assert calls[0][0][:5] == ["opencli", "browser", "chrome", "call", "openmd.x.read"]
    assert json.loads(calls[0][0][5]) == ["1234567890"]
    assert all(timeout <= 8 for _, timeout in calls)


def test_logged_in_browser_provider_fails_fast_without_an_x_tab():
    calls = []

    def runner(args, *, timeout):
        calls.append(args)
        return 0, json.dumps({"ok": True, "data": {"error_code": "x_browser_no_tab", "next_action": "Open x.com in Chrome."}}), ""

    result = XLoggedInBrowserProvider(runner=runner).extract(_request())

    assert result.status == "failed"
    assert result.error_code == "x_browser_no_tab"
    assert result.metadata["next_action"]
    assert len(calls) == 1


def test_logged_in_browser_provider_accepts_multiline_bridge_json_envelope():
    def runner(args, *, timeout):
        return 0, '{\n  "ok": true,\n  "data": {"id": "command", "ok": true, "data": [{"result": {"ok": true, "text": "Readable body."}}]}}\n', ""

    result = XLoggedInBrowserProvider(runner=runner).extract(_request())

    assert result.status == "verified"
    assert "Readable body." in result.text


def test_logged_in_browser_provider_turns_permission_failure_into_actionable_result():
    def runner(args, *, timeout):
        return 0, json.dumps({"ok": True, "data": [{"result": {
            "ok": False,
            "error_code": "x_browser_permission_denied",
            "next_action": "Reload the Openmd extension and retry.",
        }}]}), ""

    result = XLoggedInBrowserProvider(runner=runner).extract(_request())

    assert result.status == "failed"
    assert result.error_code == "x_browser_permission_denied"
    assert result.metadata["next_action"]
    assert "Next step:" in (result.error_summary or "")


def test_logged_in_browser_provider_rejects_invalid_status_urls_without_browser_calls():
    calls = []
    result = XLoggedInBrowserProvider(runner=lambda *a, **k: calls.append(a)).extract(
        _request("https://x.com/home")
    )

    assert result.status == "failed"
    assert result.error_code == "invalid_input"
    assert calls == []


def test_x_backend_chain_places_logged_in_api_after_anonymous_and_before_opencli():
    from openmd.backends.registry import BackendRuntimeOptions, create_runtime_backend_provider_registry
    from openmd.backends.x import FXTWITTER_BACKEND, OPENCLI_TWITTER_THREAD_BACKEND

    registry = create_runtime_backend_provider_registry(
        "x",
        BackendRuntimeOptions(enable_network_backends=True, enable_browser_backends=True),
    )

    assert isinstance(registry.provider_for(X_BROWSER_BACKEND), XLoggedInBrowserProvider)
    assert registry.provider_for(FXTWITTER_BACKEND) is not None
    assert registry.provider_for(OPENCLI_TWITTER_THREAD_BACKEND) is not None


def test_opencli_bridge_exposes_only_the_restricted_x_main_world_handler():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    extension = root / "src" / "openmd" / "browser_extension"
    main_world = (extension / "x_browser_main.js").read_text(encoding="utf-8")
    bridge = (extension / "vendor" / "opencli-bridge" / "background.js").read_text(encoding="utf-8")

    assert "export async function openmdXReadPage" in main_world
    assert "document.cookie" in main_world
    extension_background = (extension / "background.js").read_text(encoding="utf-8")
    assert 'import { openmdXReadPage } from "../../x_browser_main.js";' in bridge
    assert "chrome.scripting.executeScript" in bridge
    assert 'world:"MAIN"' in bridge
    assert "func:openmdXReadPage" in bridge
    assert "func: globalThis.__OPENMD_X_READ__" not in bridge
