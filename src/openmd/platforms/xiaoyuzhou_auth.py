"""Single, locked Xiaoyuzhou credential store and QR login flow.

Secrets and QR login URLs stay in the private data directory. Public results
contain only a status and, for non-interactive callers, a PNG path.
"""
from __future__ import annotations

import contextlib
import http.cookiejar
import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

API = "https://web-api.xiaoyuzhoufm.com"
APP_API = "https://api.xiaoyuzhoufm.com"
APP_ID = "v6worU4NnWyL"
CLIENT_ID = "podcaster-platform"
WARNING = "此方法有可能导致小宇宙账号被风控或封号，建议使用丢了也不心疼的小号"
REFRESH_SKEW_MS = 5 * 60 * 1000
TOKEN_TTL_MS = 20 * 60 * 1000
QR_TTL_SECONDS = 180
DEVICE_ID = "81ADBFD6-6921-482B-9AB9-A29E7CC7BB55"
_HTTP_OPENER = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))


@dataclass(frozen=True)
class LoginResult:
    status: str
    qr_path: str | None = None
    qr_expires_at: int | None = None


class AuthError(RuntimeError):
    def __init__(self, message: str, *, http_status: int | None = None, rejected: bool = False):
        super().__init__(message)
        self.http_status = http_status
        self.rejected = rejected


class CredentialStore:
    def __init__(self, path: Path | None = None):
        if path is None:
            from openmd.runtime.managed_tools import config_directory
            path = config_directory() / "xiaoyuzhou.json"
        self.path = Path(path)
        self.lock_path = self.path.with_suffix(".lock")
        self.pending_path = self.path.with_name("xiaoyuzhou-qr-pending.json")

    @contextlib.contextmanager
    def locked(self) -> Iterator[None]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self.lock_path.open("xb") as created:
                created.write(b"0")
        except FileExistsError:
            pass
        with self.lock_path.open("r+b") as stream:
            if os.name == "nt":
                import msvcrt
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if os.name == "nt":
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def read(self) -> dict[str, Any] | None:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) and value.get("access_token") and value.get("refresh_token") else None

    def write(self, value: dict[str, Any]) -> None:
        with self.locked():
            self._write_unlocked(value)

    def _write_unlocked(self, value: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".xiaoyuzhou-", suffix=".tmp", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(value, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
        finally:
            try:
                os.unlink(name)
            except FileNotFoundError:
                pass

    def delete(self) -> None:
        with self.locked():
            self.path.unlink(missing_ok=True)


def safe_error(value: object) -> str:
    text = str(value or "")
    text = re.sub(r"https?://\S+", "[redacted URL]", text)
    text = re.sub(r"(?i)(access_token|refresh_token|authorization|cookie)\s*[:=]\s*[^\s,;]+", "[redacted]", text)
    return text[:240] or "Xiaoyuzhou authentication failed."


def _post_json(url: str, payload: dict[str, Any] | None = None, *, headers: dict[str, str] | None = None,
               form: bool = False, timeout: float = 15):
    data = (urllib.parse.urlencode(payload or {}).encode("utf-8") if form else
            json.dumps(payload or {}).encode("utf-8"))
    request_headers = {"User-Agent": "Openmd/0.2",
                       "Content-Type": "application/x-www-form-urlencoded; charset=utf-8" if form else "application/json",
                       **(headers or {})}
    request = urllib.request.Request(url, data=data, headers=request_headers, method="POST")
    try:
        return _HTTP_OPENER.open(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        raise AuthError(f"小宇宙认证服务暂不可用（HTTP {exc.code}）。", http_status=exc.code) from None
    except (OSError, TimeoutError):
        raise AuthError("小宇宙认证服务网络暂不可用。") from None


def _json_response(response: Any) -> dict[str, Any]:
    try:
        data = response.json() if hasattr(response, "json") else json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        raise AuthError("Invalid authentication response.") from None
    return data if isinstance(data, dict) else {}


def _qr_path(store: CredentialStore) -> Path:
    return store.path.parent / "xiaoyuzhou-login.png"


def _render_qr(url: str, path: Path, *, terminal: bool) -> None:
    try:
        import segno
        code = segno.make(url, error="m")
        code.save(path, scale=8)
        if terminal:
            code.terminal(compact=True)
    except Exception as exc:
        raise AuthError(safe_error(exc)) from None


def _start_qr(store: CredentialStore) -> LoginResult:
    store.path.parent.mkdir(parents=True, exist_ok=True)
    response = _post_json(
        f"{API}/v1/auth/qrcode/create", {"clientId": CLIENT_ID},
        headers={"x-midway-app-id": APP_ID, "Origin": "https://accounts.xiaoyuzhoufm.com",
                 "Referer": "https://accounts.xiaoyuzhoufm.com/",
                 "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0 Safari/537.36"})
    payload = _json_response(response)
    qr_id, url = payload.get("id"), payload.get("url")
    if not isinstance(qr_id, str) or not isinstance(url, str):
        raise AuthError("QR login could not be started.")
    path = _qr_path(store)
    _render_qr(url, path, terminal=False)
    created_at = int(time.time())
    with store.locked():
        store.pending_path.write_text(json.dumps({"id": qr_id, "url": url, "created_at": created_at}), encoding="utf-8")
    return LoginResult("waiting_for_scan", str(path.resolve()), created_at + QR_TTL_SECONDS)


def _poll_pending(store: CredentialStore, *, wait: bool, terminal: bool = False) -> LoginResult:
    try:
        pending = json.loads(store.pending_path.read_text(encoding="utf-8"))
        qr_id = pending["id"]
    except (OSError, ValueError, KeyError, TypeError):
        created = _start_qr(store)
        if not wait:
            return created
        return _poll_pending(store, wait=wait, terminal=terminal)
    try:
        qr_expires_at = int(pending.get("created_at", time.time())) + QR_TTL_SECONDS
    except (TypeError, ValueError):
        qr_expires_at = int(time.time()) + QR_TTL_SECONDS
    if terminal:
        print(WARNING, file=sys.stderr)
        try:
            import segno
            segno.make(pending["url"], error="m").terminal(compact=True)
        except Exception:
            pass
        print(f"请用小宇宙 App「发现」页右上角「扫一扫」扫码确认。二维码：{_qr_path(store)}", file=sys.stderr)
    started = time.monotonic()
    network_failures = 0
    while True:
        if time.monotonic() - started > 180:
            store.pending_path.unlink(missing_ok=True)
            return LoginResult("expired")
        try:
            response = _post_json(f"{API}/v1/auth/qrcode/login", {"id": qr_id}, timeout=10)
            network_failures = 0
            payload = _json_response(response)
            headers = {str(k).lower(): str(v) for k, v in getattr(response, "headers", {}).items()}
            headers.update({str(k).lower(): str(v) for k, v in (payload.get("headers") or {}).items()})
            status = str(payload.get("status") or payload.get("state") or "").upper()
            if status == "USED":
                access = headers.get("x-jike-access-token") or payload.get("x-jike-access-token")
                refresh = headers.get("x-jike-refresh-token") or payload.get("x-jike-refresh-token")
                if not access or not refresh:
                    raise AuthError("QR login completed without usable credentials.")
                device_id = str(uuid.uuid4()).upper()
                properties = json.dumps({"uuid": device_id, "android_id": uuid.uuid4().hex[:16], "oaid": "", "vaid": "", "aaid": ""})
                store.write({"access_token": access, "refresh_token": refresh,
                             "expires_at": int(time.time() * 1000) + TOKEN_TTL_MS,
                             "device_id": device_id, "device_properties": properties})
                store.pending_path.unlink(missing_ok=True)
                return LoginResult("logged_in")
            if status not in {"WAITTING", "WAITING", "SCANNED", ""}:
                store.pending_path.unlink(missing_ok=True)
                return LoginResult("expired")
        except urllib.error.HTTPError as exc:
            if exc.code in {401, 404, 410}:
                store.pending_path.unlink(missing_ok=True)
                return LoginResult("expired")
            network_failures += 1
            if not wait or network_failures >= 3:
                return LoginResult("network_error", str(_qr_path(store).resolve()), qr_expires_at)
        except Exception:
            network_failures += 1
            if not wait or network_failures >= 3:
                return LoginResult("network_error", str(_qr_path(store).resolve()), qr_expires_at)
        if not wait:
            return LoginResult("waiting_for_scan", str(_qr_path(store).resolve()), qr_expires_at)
        time.sleep(1)


def begin_login(store: CredentialStore | None = None, *, wait: bool = True, terminal: bool = False) -> LoginResult:
    store = store or CredentialStore()
    result = _poll_pending(store, wait=wait, terminal=terminal)
    if result.status == "expired":
        result = _start_qr(store)
        if wait:
            return _poll_pending(store, wait=True, terminal=terminal)
    return result


def _refresh_credentials(current: dict[str, Any]) -> dict[str, Any]:
    headers = {
        "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
        "User-Agent": "Xiaoyuzhou/2.98.0 (build:2908; iOS 26.2.1)",
        "x-jike-access-token": current["access_token"],
        "x-jike-refresh-token": current["refresh_token"],
        "x-jike-device-id": current.get("device_id", DEVICE_ID),
        "x-jike-device-properties": current.get("device_properties", ""),
        "OS": "ios", "OS-Version": "26.2.1", "Manufacturer": "Apple", "Model": "iPhone18,1",
        "Market": "AppStore", "App-Version": "2.98.0", "App-BuildNo": "2908",
        "BundleID": "app.podcast.cosmos", "Timezone": "UTC", "Local-Time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    response = _post_json(f"{APP_API}/app_auth_tokens.refresh", {}, headers=headers, form=True, timeout=20)
    payload = _json_response(response)
    access = payload.get("x-jike-access-token")
    refresh = payload.get("x-jike-refresh-token")
    if payload.get("success") is not True or not access or not refresh:
        raise AuthError("Token refresh was rejected.", rejected=True)
    return {**current, "access_token": access, "refresh_token": refresh,
            "expires_at": int(time.time() * 1000) + TOKEN_TTL_MS}


def ensure_fresh_credentials(store: CredentialStore | None = None) -> dict[str, Any] | None:
    store = store or CredentialStore()
    with store.locked():
        current = store.read()
        if current is None:
            return None
        expires = int(current.get("expires_at") or 0)
        if expires and expires - int(time.time() * 1000) > REFRESH_SKEW_MS:
            return current
        try:
            updated = _refresh_credentials(current)
        except AuthError as exc:
            if exc.http_status in {401, 403} or exc.rejected:
                store.path.unlink(missing_ok=True)
                return None
            return current
        except Exception:
            return current
        store._write_unlocked(updated)
        return updated


def status(store: CredentialStore | None = None) -> str:
    store = store or CredentialStore()
    current = store.read()
    if current:
        expires = int(current.get("expires_at") or 0)
        if not expires or expires > int(time.time() * 1000):
            return "已登录"
    if store.pending_path.exists():
        return "需要重新扫码"
    if current:
        return "需要重新扫码"
    return "未登录"


def import_tokens(access_token: str, refresh_token: str, *, store: CredentialStore | None = None) -> None:
    """Validate and persist extension-imported cookies through the shared store."""
    if not isinstance(access_token, str) or not access_token.strip():
        raise ValueError("小宇宙 access token 缺失。")
    if not isinstance(refresh_token, str) or not refresh_token.strip():
        raise ValueError("小宇宙 refresh token 缺失。")
    device_id = str(uuid.uuid4()).upper()
    properties = json.dumps({"uuid": device_id, "android_id": uuid.uuid4().hex[:16],
                             "oaid": "", "vaid": "", "aaid": ""})
    (store or CredentialStore()).write({
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_at": int(time.time() * 1000) + TOKEN_TTL_MS,
        "device_id": device_id,
        "device_properties": properties,
    })


def pending_qr_expiry(store: CredentialStore | None = None, qr_path: str | None = None) -> int | None:
    """Return only the pending QR expiry, never its login URL or identifier."""
    store = store or CredentialStore()
    try:
        pending = json.loads(store.pending_path.read_text(encoding="utf-8"))
        created_at = int(pending["created_at"])
    except (OSError, ValueError, KeyError, TypeError):
        try:
            return int(Path(qr_path).stat().st_mtime) + QR_TTL_SECONDS if qr_path else None
        except OSError:
            return None
    return created_at + QR_TTL_SECONDS
