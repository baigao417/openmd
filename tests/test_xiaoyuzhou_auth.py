from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from openmd.platforms import xiaoyuzhou_auth as auth


def test_credential_store_is_single_location_and_atomic(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENMD_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    store = auth.CredentialStore()

    store.write({"access_token": "synthetic-access", "refresh_token": "synthetic-refresh"})

    assert store.path == tmp_path / "data" / "opencli" / "xiaoyuzhou.json"
    assert json.loads(store.path.read_text(encoding="utf-8"))["access_token"] == "synthetic-access"
    assert not list(store.path.parent.glob("*.tmp"))


def test_qr_flow_waits_for_used_and_never_returns_login_url(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENMD_DATA_DIR", str(tmp_path))
    replies = iter([
        {"id": "opaque-id", "url": "https://login.invalid/private-qr"},
        {"status": "WAITTING"},
        {"status": "SCANNED"},
        {"status": "USED", "headers": {
            "x-jike-access-token": "synthetic-access",
            "x-jike-refresh-token": "synthetic-refresh",
        }},
    ])

    class Response:
        headers = {}
        def __init__(self, payload): self.payload = payload
        def json(self): return self.payload

    headers_seen = []
    def fake_post(*args, **kwargs):
        headers_seen.append(kwargs.get("headers", {}))
        return Response(next(replies))
    monkeypatch.setattr(auth, "_post_json", fake_post)
    monkeypatch.setattr(auth, "_render_qr", lambda _url, path, **_kwargs: path.write_bytes(b"png"))
    monkeypatch.setattr(auth.time, "sleep", lambda _: None)
    result = auth.begin_login()
    assert result.status == "logged_in"
    assert "private-qr" not in repr(result)
    assert headers_seen[0]["Origin"] == "https://accounts.xiaoyuzhoufm.com"
    assert auth.CredentialStore().read()["refresh_token"] == "synthetic-refresh"


def test_refresh_rotation_keeps_new_refresh_token(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENMD_DATA_DIR", str(tmp_path))
    store = auth.CredentialStore()
    store.write({"access_token": "old-access", "refresh_token": "old-refresh", "expires_at": 0})

    def refresh(current):
        assert current["refresh_token"] == "old-refresh"
        return {**current, "access_token": "new-access", "refresh_token": "rotated-refresh", "expires_at": 4_000_000_000_000}

    monkeypatch.setattr(auth, "_refresh_credentials", refresh)
    assert auth.ensure_fresh_credentials(store)["refresh_token"] == "rotated-refresh"
    assert store.read()["access_token"] == "new-access"


@pytest.mark.parametrize("status, expected", [(401, None), (500, "synthetic-refresh")])
def test_refresh_http_rejection_clears_only_explicitly_rejected_credentials(tmp_path, monkeypatch, status, expected):
    monkeypatch.setenv("OPENMD_DATA_DIR", str(tmp_path))
    store = auth.CredentialStore()
    store.write({"access_token": "synthetic-access", "refresh_token": "synthetic-refresh", "expires_at": 0})
    monkeypatch.setattr(auth, "_refresh_credentials", lambda _current: (_ for _ in ()).throw(auth.AuthError("refresh unavailable", http_status=status)))
    refreshed = auth.ensure_fresh_credentials(store)
    if expected is None:
        assert refreshed is None and store.read() is None
    else:
        assert refreshed["refresh_token"] == expected and store.read()["refresh_token"] == expected


def test_store_lock_serializes_refreshes(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENMD_DATA_DIR", str(tmp_path))
    store = auth.CredentialStore()
    store.write({"access_token": "a", "refresh_token": "r", "expires_at": 0})
    calls = 0
    started = threading.Event()
    release = threading.Event()

    def refresh(current):
        nonlocal calls
        calls += 1
        started.set()
        release.wait(timeout=2)
        return {**current, "access_token": "fresh", "expires_at": 4_000_000_000_000}

    monkeypatch.setattr(auth, "_refresh_credentials", refresh)
    threads = [threading.Thread(target=auth.ensure_fresh_credentials, args=(store,)) for _ in range(2)]
    for thread in threads: thread.start()
    assert started.wait(timeout=2)
    release.set()
    for thread in threads: thread.join(timeout=3)
    assert all(not thread.is_alive() for thread in threads)
    assert calls == 1


def test_qr_failure_redacts_url_and_token_values():
    message = auth.safe_error("https://login.invalid/secret?x=1 access_token=synthetic-access")
    assert "login.invalid" not in message
    assert "synthetic-access" not in message


def test_json_post_declares_content_type(monkeypatch):
    seen = []
    class Response:
        pass
    monkeypatch.setattr(auth._HTTP_OPENER, "open", lambda request, **kwargs: seen.append(request) or Response())
    auth._post_json("https://example.invalid", {"safe": "value"})
    assert seen[0].get_header("Content-type") == "application/json"


def test_qr_request_has_browser_client_headers():
    import inspect
    source = inspect.getsource(auth._start_qr)
    assert "Chrome/142.0 Safari/537.36" in source
    assert "SECRET_" not in source and "PRIVATE_" not in source


def test_http_failures_are_bounded_and_do_not_echo_request_url(monkeypatch):
    import urllib.error

    def reject(_request, **_kwargs):
        raise urllib.error.HTTPError("https://login.invalid/sensitive", 415, "unsupported", {}, None)
    monkeypatch.setattr(auth._HTTP_OPENER, "open", reject)
    try:
        auth._post_json("https://login.invalid/sensitive", {})
    except auth.AuthError as exc:
        assert "415" in str(exc)
        assert "login.invalid" not in str(exc)
    else:
        raise AssertionError("HTTP failure should be normalized")


def test_public_routes_drop_maintainer_only_media_cards():
    from openmd.routing.loader import load_catalog

    catalog = load_catalog(include_plugins=False)
    assert "xyz-dl" not in catalog.cards and "yutto" not in catalog.cards
    assert "xyz-dl.xiaoyuzhou.media-asr" not in catalog.capabilities
    assert "yutto.bilibili.media-asr" not in catalog.capabilities
    assert not any(route["source"] == "xiaoyuzhou" and route["capability"] == "media" for route in catalog.routes)
    assert not any(route["source"] == "bilibili" and "yutto.bilibili.media-asr" in route["chain"] for route in catalog.routes)


def test_mcp_login_receipt_returns_png_path_without_qr_url():
    from openmd.mcp_jobs import receipt

    result = receipt({"status": "failed", "error_code": "login_required",
                      "qr_path": "C:/safe/opencli/xiaoyuzhou-login.png",
                      "next_action": "https://login.invalid/private-qr"})
    assert result["qr_path"].endswith("xiaoyuzhou-login.png")
    assert "xiaoyuzhou-login.png" in result["next_action"]
    assert "login.invalid" not in result["next_action"]


def test_extension_cookie_import_writes_only_shared_credential_store(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENMD_DATA_DIR", str(tmp_path / "data"))
    from openmd import browser_host

    reply = browser_host.handle({"type": "import_xiaoyuzhou", "cookies": {
        "x-jike-access-token": "synthetic-access", "x-jike-refresh-token": "synthetic-refresh"}})
    assert reply == {"ok": True}
    stored = auth.CredentialStore().read()
    assert stored["access_token"] == "synthetic-access"
    assert stored["refresh_token"] == "synthetic-refresh"


def test_doctor_reports_local_xiaoyuzhou_state_without_token_values(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENMD_DATA_DIR", str(tmp_path))
    store = auth.CredentialStore()
    assert auth.status(store) == "未登录"
    store.write({"access_token": "synthetic-access", "refresh_token": "synthetic-refresh",
                 "expires_at": 4_000_000_000_000})
    assert auth.status(store) == "已登录"
