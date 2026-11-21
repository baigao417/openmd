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
        {"id": "opaque-id", "url": "synthetic-qr-payload"},
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
    assert "synthetic-qr-payload" not in repr(result)
    assert headers_seen[0]["Origin"] == "https://accounts.xiaoyuzhoufm.com"
    assert auth.CredentialStore().read()["refresh_token"] == "synthetic-refresh"


def test_noninteractive_qr_result_has_absolute_path_and_expiry(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENMD_DATA_DIR", str(tmp_path))
    class Response:
        headers = {}
        def json(self): return {"id": "opaque-id", "url": "synthetic-qr-payload"}
    monkeypatch.setattr(auth, "_post_json", lambda *args, **kwargs: Response())
    monkeypatch.setattr(auth, "_render_qr", lambda _url, path, **_kwargs: path.write_bytes(b"png"))
    result = auth.begin_login(wait=False)
    assert Path(result.qr_path).is_absolute()
    assert result.qr_expires_at >= int(auth.time.time()) + auth.QR_TTL_SECONDS - 1


def test_pending_qr_expiry_exposes_only_timestamp(tmp_path):
    store = auth.CredentialStore(tmp_path / "opencli" / "xiaoyuzhou.json")
    store.pending_path.parent.mkdir(parents=True, exist_ok=True)
    store.pending_path.write_text(json.dumps({"created_at": 1_700_000_000}), encoding="utf-8")
    assert auth.pending_qr_expiry(store) == 1_700_000_000 + auth.QR_TTL_SECONDS


def test_pending_qr_expiry_falls_back_to_qr_png_creation_time(tmp_path):
    path = tmp_path / "xiaoyuzhou-login.png"
    path.write_bytes(b"png")
    expiry = auth.pending_qr_expiry(auth.CredentialStore(tmp_path / "missing.json"), str(path))
    assert expiry == int(path.stat().st_mtime) + auth.QR_TTL_SECONDS


def test_xiaoyuzhou_backend_reads_qr_expiry_from_its_credential_store(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from openmd.backends.xiaoyuzhou import OpenCliXiaoyuzhouTranscriptProvider

    store = auth.CredentialStore(tmp_path / "xiaoyuzhou.json")
    store.pending_path.parent.mkdir(parents=True, exist_ok=True)
    store.pending_path.write_text(json.dumps({"created_at": 1_700_000_000}), encoding="utf-8")
    monkeypatch.setattr(auth, "ensure_fresh_credentials", lambda _store: None)
    monkeypatch.setattr(auth, "begin_login", lambda *_args, **_kwargs: auth.LoginResult("waiting_for_scan", str(tmp_path / "qr.png")))
    monkeypatch.setattr("openmd.interactive.is_interactive", lambda: False)
    provider = OpenCliXiaoyuzhouTranscriptProvider(credential_path=store.path)
    result = provider.extract(SimpleNamespace(item=SimpleNamespace(platform="xiaoyuzhou")))
    assert result.metadata["qr_expires_at"] == 1_700_000_000 + auth.QR_TTL_SECONDS


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
                      "next_action": "synthetic-qr-payload"})
    assert result["qr_path"].endswith("xiaoyuzhou-login.png")
    assert "xiaoyuzhou-login.png" in result["next_action"]
    assert "login.invalid" not in result["next_action"]


def test_mcp_login_receipt_preserves_structured_qr_expiry():
    from openmd.mcp_jobs import receipt

    path = str(Path("C:/safe/opencli/xiaoyuzhou-login.png").resolve())
    result = receipt({"status": "failed", "error_code": "login_required",
                      "qr_path": path, "qr_expires_at": 1_800_000_000,
                      "qr_http_status": 415})
    assert result["qr_path"] == path
    assert result["qr_expires_at"] == 1_800_000_000
    assert result["qr_http_status"] == 415


def test_mcp_login_receipt_recovers_qr_expiry_from_png(tmp_path):
    from openmd.mcp_jobs import receipt

    path = tmp_path / "xiaoyuzhou-login.png"
    path.write_bytes(b"png")
    result = receipt({"status": "failed", "error_code": "login_required", "qr_path": str(path)})
    assert result["qr_expires_at"] == int(path.stat().st_mtime) + auth.QR_TTL_SECONDS


def test_cli_json_login_recovery_carries_absolute_qr_path_and_expiry():
    from types import SimpleNamespace
    from openmd.cli_login import recovery_fields

    args = SimpleNamespace(_xiaoyuzhou_qr_path=str(Path("qr.png").resolve()),
                           _xiaoyuzhou_qr_expires_at=1_800_000_000,
                           _xiaoyuzhou_qr_http_status=415)
    result = recovery_fields(args)
    assert result["qr_path"] == args._xiaoyuzhou_qr_path
    assert result["qr_expires_at"] == 1_800_000_000
    assert result["qr_http_status"] == 415
    assert args._xiaoyuzhou_qr_path in result["next_action"]


def test_extract_with_login_copies_qr_fields_to_cli_recovery_state():
    from types import SimpleNamespace
    from openmd.cli_login import extract_with_login, recovery_fields

    path = str(Path("qr.png").resolve())
    raw = SimpleNamespace(error_code="login_required", metadata={
        "qr_path": path, "qr_expires_at": 1_800_000_000, "qr_http_status": None})
    executor = SimpleNamespace(extract=lambda *_args, **_kwargs: raw)
    args = SimpleNamespace(fallback=True)
    result = extract_with_login(executor, SimpleNamespace(platform="xiaoyuzhou"), [], args)
    assert result is raw
    fields = recovery_fields(args)
    assert fields["qr_path"] == path
    assert fields["qr_expires_at"] == 1_800_000_000


def test_extract_with_login_recovers_expiry_from_shared_pending_state(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from openmd.cli_login import extract_with_login, recovery_fields

    monkeypatch.setenv("OPENMD_DATA_DIR", str(tmp_path / "data"))
    store = auth.CredentialStore()
    store.pending_path.parent.mkdir(parents=True)
    store.pending_path.write_text(json.dumps({"created_at": 1_700_000_000}), encoding="utf-8")
    raw = SimpleNamespace(error_code="login_required", metadata={"qr_path": str(tmp_path / "qr.png")})
    executor = SimpleNamespace(extract=lambda *_args, **_kwargs: raw)
    args = SimpleNamespace(fallback=True)
    extract_with_login(executor, SimpleNamespace(platform="xiaoyuzhou"), [], args)
    assert recovery_fields(args)["qr_expires_at"] == 1_700_000_000 + auth.QR_TTL_SECONDS


def test_compact_cli_json_receipt_includes_qr_fields(tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace
    from openmd import cli
    from openmd.cli_receipts import _emit_result

    path = str((tmp_path / "xiaoyuzhou-login.png").resolve())
    class FakeRun:
        has_items = True
        def finalize(self, **_kwargs): return {"status": "blocked"}, 1
    args = SimpleNamespace(_compact_receipt_run=FakeRun(), _compact_inputs_registered=True,
                           _compact_pending_identities={}, _compact_requires_fresh_stdin=False)
    result = cli.CliResult(ok=False, command="go", error_code="login_required",
                           next_action=f"请用小宇宙 App 扫码：{path}", qr_path=path,
                           qr_expires_at=None)
    Path(path).write_bytes(b"png")
    monkeypatch.setattr(cli, "_sync_compact_rows", lambda _args: None)
    assert _emit_result(args, result) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["qr_path"] == path
    assert payload["qr_expires_at"] == int(Path(path).stat().st_mtime) + auth.QR_TTL_SECONDS


def test_default_go_json_receipt_includes_qr_expiry(tmp_path):
    from types import SimpleNamespace
    from openmd.interactive import _go_receipt_fields

    path = tmp_path / "xiaoyuzhou-login.png"
    path.write_bytes(b"png")
    result = SimpleNamespace(qr_path=str(path.resolve()), qr_expires_at=None,
                             qr_http_status=None, status="failed", source_level=None,
                             completeness={}, sync={}, login=None, next_action="请扫码",
                             error_code="login_required")
    fields = _go_receipt_fields(result, None, ok=False)
    assert fields["qr_path"] == str(path.resolve())
    assert fields["qr_expires_at"] == int(path.stat().st_mtime) + auth.QR_TTL_SECONDS


def test_cookie_import_uses_shared_auth_import_function(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENMD_DATA_DIR", str(tmp_path / "data"))
    from openmd import browser_host

    calls = []
    def import_tokens(access, refresh, *, store=None):
        calls.append((access, refresh, store))
    monkeypatch.setattr(auth, "import_tokens", import_tokens, raising=False)
    reply = browser_host.handle({"type": "import_xiaoyuzhou", "cookies": {
        "x-jike-access-token": "synthetic-access", "x-jike-refresh-token": "synthetic-refresh"}})
    assert reply == {"ok": True}
    assert len(calls) == 1
    assert calls[0][:2] == ("synthetic-access", "synthetic-refresh")


def test_import_tokens_builds_validated_shared_credential_shape(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENMD_DATA_DIR", str(tmp_path / "data"))
    store = auth.CredentialStore()
    auth.import_tokens("synthetic-access", "synthetic-refresh", store=store)
    saved = store.read()
    assert saved["access_token"] == "synthetic-access"
    assert saved["refresh_token"] == "synthetic-refresh"
    assert saved["expires_at"] > 0
    properties = json.loads(saved["device_properties"])
    assert properties["uuid"] == saved["device_id"]
    assert {"android_id", "oaid", "vaid", "aaid"} <= properties.keys()


@pytest.mark.parametrize("access, refresh", [("", "r"), ("a", "")])
def test_import_tokens_rejects_missing_cookie_values(access, refresh):
    with pytest.raises(ValueError):
        auth.import_tokens(access, refresh)


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
