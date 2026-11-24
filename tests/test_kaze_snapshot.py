from __future__ import annotations

import json
import re
from pathlib import Path

from openmd.backends.base import BackendExtractRequest
from openmd.backends.registry import BackendRuntimeOptions
from openmd.core.models import ContentItem
from openmd.backends.kaze_snapshot import (
    KAZE_BACKEND,
    KazeSnapshotProvider,
    _is_login_redirect,
    _same_origin,
    _tool_payload,
    _same_url,
    probe_kaze,
    register_backend_providers,
)
from openmd.backends.executor import BackendProviderRegistry


class FakeSession:
    def __init__(self, *, html=None, rows=None, final_url="https://example.org/article"):
        self.html = html or "<html><head><title>Public page</title></head><body><main><p>Visible article text with enough content to extract.</p></main></body></html>"
        self.rows = rows if rows is not None else [{"url": "https://example.org/article", "backendId": "opaque"}]
        self.final_url = final_url
        self.calls = []
        self.closed = False
        self.fingerprint = "schema-fingerprint"
        self.initialized = {"protocolVersion": "2024-11-05", "serverInfo": {"name": "browsermcp", "version": "0.0.43-embedded.0"}}

    def call(self, name, arguments, timeout):
        self.calls.append((name, arguments))
        if name == "tabs_list":
            return {"tabs": self.rows}
        if name == "claim_tab":
            return {"tabId": "borrowed-tab"}
        if name == "open_tab":
            return {"tabId": "owned-tab"}
        if name == "js":
            code = arguments["code"]
            if "const settled" in code:
                return _tool_payload({"structuredContent": {"ok": True, "value": {
                    "ready": True, "body_chars": len(self.html), "visibility_state": "visible",
                }}})
            if "captureSource" in code:
                # Match Kaze's Tab.evaluate(script, opts) contract: callback
                # arguments are supplied via opts.arg.
                if "{ arg: captureSource, timeoutMs: 60000 }" not in code:
                    raise RuntimeError("invalid_args: Tab.evaluate options expected object; dispatched:false")
                assert arguments["timeoutMs"] == 60_000
            match = re.search(r"fs\.writeFile\((\"(?:\\\\.|[^\"])*\")", code)
            if match:
                Path(json.loads(match.group(1))).write_text(self.html, encoding="utf-8")
                value = {"url": self.final_url, "title": "Public page",
                         "bytes": len(self.html.encode("utf-8"))}
            else:
                value = {"html": self.html, "url": self.final_url, "title": "Public page"}
            return _tool_payload({"structuredContent": {"ok": True, "value": value}})
        if name == "session_finish":
            return {"ok": True, "closed": [], "released": [], "failed": []}
        return {"ok": True}

    def close(self):
        self.closed = True


def test_kaze_provider_claims_matching_page_and_releases_without_closing(monkeypatch):
    session = FakeSession()
    monkeypatch.setattr("openmd.backends.kaze_snapshot._open_session", lambda: session)
    request = BackendExtractRequest(
        ContentItem("web_article", "single", "https://example.org/article", source_type="article"),
        (KAZE_BACKEND,),
    )

    result = KazeSnapshotProvider().extract(request)

    assert result.status in {"verified", "partial"}
    assert result.backend == KAZE_BACKEND
    assert "Visible article text" in result.text
    assert ("claim_tab", {"url": "https://example.org/article", "expectedUrl": "https://example.org/article"}) in session.calls
    assert ("release_tab", {"tabId": "borrowed-tab"}) in session.calls
    assert not any(name == "close_tab" for name, _ in session.calls)
    assert session.closed
    assert "https://example.org" not in repr(result.metadata)


def test_kaze_reopens_matching_tab_when_claimed_page_is_blank(monkeypatch):
    class BlankClaimedPageSession(FakeSession):
        def __init__(self):
            super().__init__()
            self.readiness_results = [
                {"ready": False, "body_chars": 0, "visibility_state": "visible"},
                {"ready": True, "body_chars": 57, "visibility_state": "visible"},
            ]

        def call(self, name, arguments, timeout):
            if name == "js" and "const settled" in arguments["code"]:
                self.calls.append((name, arguments))
                return self.readiness_results.pop(0)
            return super().call(name, arguments, timeout)

    session = BlankClaimedPageSession()
    monkeypatch.setattr("openmd.backends.kaze_snapshot._open_session", lambda: session)
    request = BackendExtractRequest(
        ContentItem("web_article", "single", "https://example.org/article", source_type="article"),
        (KAZE_BACKEND,),
    )

    result = KazeSnapshotProvider().extract(request)

    assert result.status in {"verified", "partial"}
    assert ("claim_tab", {"url": "https://example.org/article", "expectedUrl": "https://example.org/article"}) in session.calls
    assert ("release_tab", {"tabId": "borrowed-tab"}) in session.calls
    assert ("open_tab", {"url": "https://example.org/article"}) in session.calls
    assert ("close_tab", {"tabId": "owned-tab"}) in session.calls


def test_kaze_unwraps_browsermcp_value_envelope():
    value = {"url": "https://example.org/article", "title": "Public page", "bytes": 17}
    assert _tool_payload({"structuredContent": {"ok": True, "value": value}}) == value


def test_kaze_url_comparison_uses_shared_iri_normalization():
    assert _same_url("https://zh.wikipedia.org/wiki/中国", "https://zh.wikipedia.org/wiki/%E4%B8%AD%E5%9B%BD")


def test_kaze_accepts_same_origin_canonical_redirect_but_rejects_login_path(monkeypatch):
    session = FakeSession(rows=[], final_url="https://example.org/articles/1")
    monkeypatch.setattr("openmd.backends.kaze_snapshot._open_session", lambda: session)
    request = BackendExtractRequest(
        ContentItem("web_article", "single", "https://example.org/article", source_type="article"),
        (KAZE_BACKEND,),
    )
    result = KazeSnapshotProvider().extract(request)
    assert result.status in {"verified", "partial"}
    assert _same_origin("https://example.org/articles/1", "https://example.org/article")
    assert _is_login_redirect("https://example.org/login")


def test_kaze_login_redirect_is_not_success(monkeypatch):
    session = FakeSession(rows=[], final_url="https://example.org/login")
    monkeypatch.setattr("openmd.backends.kaze_snapshot._open_session", lambda: session)
    request = BackendExtractRequest(
        ContentItem("web_article", "single", "https://example.org/article", source_type="article"),
        (KAZE_BACKEND,),
    )
    result = KazeSnapshotProvider().extract(request)
    assert result.status == "failed"
    assert result.error_code == "kaze_login_required"


def test_kaze_failure_codes_have_next_actions():
    from openmd.cli_login import source_next_action

    codes = (
        "kaze_launcher_missing", "kaze_not_running", "kaze_unavailable",
        "kaze_external_agents_disabled", "kaze_unsupported_protocol", "kaze_tool_schema_changed",
        "kaze_timeout", "kaze_tab_open_failed", "kaze_page_not_ready",
        "kaze_snapshot_transfer_failed", "kaze_snapshot_too_large", "kaze_cleanup_failed",
        "kaze_page_url_mismatch", "kaze_login_required", "kaze_snapshot_failed",
    )
    assert all(source_next_action(code) for code in codes)


def test_kaze_page_url_is_read_from_page_context(monkeypatch):
    session = FakeSession(rows=[])
    monkeypatch.setattr("openmd.backends.kaze_snapshot._open_session", lambda: session)
    request = BackendExtractRequest(
        ContentItem("web_article", "single", "https://example.org/article", source_type="article"),
        (KAZE_BACKEND,),
    )

    KazeSnapshotProvider().extract(request)

    read_js = next(args["code"] for name, args in session.calls if name == "js" and "outerHTML" in args["code"])
    assert "const url = captured.url" in read_js
    assert "await t.url()" not in read_js
    assert "({html" not in read_js
    assert "node:fs/promises" in read_js
    assert "fs.writeFile" in read_js
    assert "page_not_ready" not in read_js


def test_kaze_provider_closes_only_owned_tab(monkeypatch):
    session = FakeSession(rows=[])
    monkeypatch.setattr("openmd.backends.kaze_snapshot._open_session", lambda: session)
    request = BackendExtractRequest(
        ContentItem("web_article", "single", "https://example.org/article", source_type="article"),
        (KAZE_BACKEND,),
    )

    KazeSnapshotProvider().extract(request)

    assert ("close_tab", {"tabId": "owned-tab"}) in session.calls
    assert not any(name == "release_tab" for name, _ in session.calls)


def test_kaze_login_page_is_not_returned_as_success(monkeypatch):
    session = FakeSession(html="<html><head><title>Sign in</title></head><body><form><input name='password'><button>Log in</button></form></body></html>")
    monkeypatch.setattr("openmd.backends.kaze_snapshot._open_session", lambda: session)
    request = BackendExtractRequest(
        ContentItem("web_article", "single", "https://example.org/article", source_type="article"),
        (KAZE_BACKEND,),
    )

    result = KazeSnapshotProvider().extract(request)

    assert result.status == "failed"
    assert result.error_code == "kaze_login_required"
    assert result.text == ""


def test_kaze_doctor_separates_component_versions_and_disable_skips_launch(monkeypatch, tmp_path):
    launcher = tmp_path / "kaze-mcp.cmd"
    launcher.write_text("", encoding="utf-8")
    monkeypatch.setenv("KAZE_MCP_LAUNCHER", str(launcher))
    called = []
    session = FakeSession()
    cap = probe_kaze(disabled=False, session_factory=lambda: (called.append(True), session)[1])
    report = cap.as_dict()
    assert report["launcher_available"] is True
    assert report["server_version"] == "0.0.43-embedded.0"
    assert report["kaze_version"] is None and report["bridge_version"] is None

    disabled = probe_kaze(disabled=True, session_factory=lambda: called.append(False))
    assert disabled.error_code == "kaze_disabled"
    assert disabled.as_dict()["private_session_state"] == "not_read"
    assert called == [True]


def test_doctor_includes_kaze_capability_without_reading_session(monkeypatch):
    from openmd.core import doctor

    monkeypatch.setattr(
        "openmd.backends.kaze_snapshot.probe_kaze",
        lambda: type("Capability", (), {"as_dict": lambda self: {
            "available": False, "launcher_available": False, "kaze_version": None,
            "bridge_version": None, "server_version": None, "disabled": True,
            "error_code": "kaze_disabled", "next_action": "Set OPENMD_KAZE=1.",
            "private_session_state": "not_read",
        }})(),
    )
    monkeypatch.setattr(doctor, "_module_available", lambda _: False)

    report = doctor.build_doctor_report("core")

    assert report["kaze_snapshot"]["error_code"] == "kaze_disabled"
    assert report["kaze_snapshot"]["private_session_state"] == "not_read"


def test_kaze_provider_is_registered_only_behind_browser_gate(monkeypatch):
    monkeypatch.delenv("OPENMD_KAZE", raising=False)
    disabled = BackendProviderRegistry()
    register_backend_providers(disabled, BackendRuntimeOptions())
    assert disabled.provider_for(KAZE_BACKEND) is None

    enabled = BackendProviderRegistry()
    register_backend_providers(enabled, BackendRuntimeOptions(enable_browser_backends=True))
    assert enabled.provider_for(KAZE_BACKEND) is not None

    monkeypatch.setenv("OPENMD_KAZE", "0")
    opted_out = BackendProviderRegistry()
    register_backend_providers(opted_out, BackendRuntimeOptions(enable_browser_backends=True))
    assert opted_out.provider_for(KAZE_BACKEND) is None

    monkeypatch.delenv("OPENMD_KAZE", raising=False)
    x_registry = BackendProviderRegistry()
    from openmd.backends.x import register_backend_providers as register_x

    register_x(x_registry, BackendRuntimeOptions(enable_browser_backends=True))
    assert x_registry.provider_for(KAZE_BACKEND) is not None


def test_kaze_keeps_article_heading_when_page_title_is_generic(monkeypatch):
    session = FakeSession(html=(
        "<html><head><title>Public page</title></head><body>"
        "<main><article><h1>Founder Mode</h1><p>Visible article text with enough content to extract.</p></article></main>"
        "</body></html>"
    ))
    monkeypatch.setattr("openmd.backends.kaze_snapshot._open_session", lambda: session)
    request = BackendExtractRequest(
        ContentItem("web_article", "single", "https://example.org/article", source_type="article"),
        (KAZE_BACKEND,),
    )

    result = KazeSnapshotProvider().extract(request)

    assert result.status in {"verified", "partial"}
    assert result.title == "Founder Mode"


def test_kaze_waits_for_rendered_text_to_settle_before_capture(monkeypatch):
    """SPAs paint their shell before the article request returns; capture after text stops changing."""
    session = FakeSession(rows=[])
    monkeypatch.setattr("openmd.backends.kaze_snapshot._open_session", lambda: session)
    request = BackendExtractRequest(
        ContentItem("web_article", "single", "https://example.org/article", source_type="article"),
        (KAZE_BACKEND,),
    )

    KazeSnapshotProvider().extract(request)

    scripts = [args["code"] for name, args in session.calls if name == "js"]
    settle = next(index for index, code in enumerate(scripts) if "innerText" in code)
    capture = next(index for index, code in enumerate(scripts) if "outerHTML" in code)
    assert settle < capture
    assert "stable" in scripts[settle] and "setTimeout" in scripts[settle]
    assert "performance.getEntriesByType('resource').length" not in scripts[settle]
    assert "querySelectorAll('*').length" in scripts[settle]


def test_kaze_evaluate_uses_options_arg_and_full_capture_timeout(monkeypatch):
    session = FakeSession(rows=[])
    monkeypatch.setattr("openmd.backends.kaze_snapshot._open_session", lambda: session)
    request = BackendExtractRequest(
        ContentItem("web_article", "single", "https://example.org/article", source_type="article"),
        (KAZE_BACKEND,),
    )

    result = KazeSnapshotProvider().extract(request)

    assert result.status in {"verified", "partial"}
    capture_call = next(args for name, args in session.calls if name == "js" and "captureSource" in args["code"])
    assert "{ arg: captureSource, timeoutMs: 60000 }" in capture_call["code"]
    assert capture_call["timeoutMs"] == 60_000
    assert capture_call["timeoutMs"] > 45_000
    assert "const html = captured.html; const url = captured.url;" in capture_call["code"]
    assert "await t.url()" not in capture_call["code"]


def test_kaze_invalid_evaluate_arguments_are_reported_as_script_error(monkeypatch):
    session = FakeSession(rows=[])
    original_call = session.call

    def call(name, arguments, timeout):
        if name == "js" and "captureSource" in arguments["code"]:
            raise RuntimeError("invalid_args: Tab.evaluate options expected object; dispatched:false")
        return original_call(name, arguments, timeout)

    session.call = call
    monkeypatch.setattr("openmd.backends.kaze_snapshot._open_session", lambda: session)
    request = BackendExtractRequest(
        ContentItem("web_article", "single", "https://example.org/article", source_type="article"),
        (KAZE_BACKEND,),
    )

    result = KazeSnapshotProvider().extract(request)

    assert result.status == "failed"
    assert result.error_code == "kaze_script_error"
    from openmd.cli_login import source_next_action

    assert "调用兼容问题" in source_next_action(result.error_code)


def test_kaze_capture_meta_is_forwarded_to_result(monkeypatch):
    session = FakeSession(rows=[])
    original_call = session.call

    def call(name, arguments, timeout):
        value = original_call(name, arguments, timeout)
        if name == "js" and any(token in arguments["code"] for token in ("fs.writeFile", "capture_meta")):
            # Fake BrowserMCP returns the structured capture receipt alongside the HTML.
            payload = value if isinstance(value, dict) else None
            if isinstance(payload, dict) and "bytes" in payload:
                payload["capture_meta"] = {
                    "coverage": "scrolled_full", "virtual_detected": True,
                    "stop_reason": "end_reached", "steps": 2, "elapsed_ms": 300,
                    "seen_count": 3, "new_count": 3, "duplicate_count": 0,
                    "collected_count": 3, "max_index": 2, "index_gaps": [], "declared_total": 3,
                }
        return value

    session.call = call
    monkeypatch.setattr("openmd.backends.kaze_snapshot._open_session", lambda: session)
    request = BackendExtractRequest(
        ContentItem("web_article", "single", "https://example.org/article", source_type="article"),
        (KAZE_BACKEND,),
    )
    result = KazeSnapshotProvider().extract(request)
    assert result.metadata["capture"]["coverage"] == "scrolled_full"
