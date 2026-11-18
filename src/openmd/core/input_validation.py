"""Daily-input classification and bounded, best-effort public URL preflight."""

from __future__ import annotations

import queue
import socket
import threading
from urllib.parse import urlparse
import urllib.error
import urllib.request

from .detector import _local_path_for_detection


INPUT_HINT = "请粘贴 http(s) 链接或本地文件路径"


def valid_daily_input(value: str) -> bool:
    try:
        parsed = urlparse(value)
        if parsed.scheme == "wechat":
            return bool(parsed.netloc or parsed.path.strip("/")) and not any(ch.isspace() for ch in value)
        if parsed.scheme in {"http", "https"}:
            return bool(parsed.hostname) and not any(ch.isspace() for ch in value)
        path = _local_path_for_detection(value)
        return path is not None and path.is_file()
    except (OSError, ValueError):
        return False


def _probe_url(url: str) -> str | None:
    parsed = urlparse(url)
    host = (parsed.hostname or "").rstrip(".").lower()
    # RFC-reserved invalid names stay invalid even under a fake-DNS proxy.
    if host == "invalid" or host.endswith(".invalid"):
        return "域名无法访问"
    proxies = urllib.request.getproxies()
    # A configured proxy may resolve the origin remotely. Local DNS must not
    # veto that route or consume its short preflight budget.
    if not (proxies.get(parsed.scheme) or proxies.get("all")):
        try:
            socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            if exc.errno in {socket.EAI_NONAME, getattr(socket, "EAI_NODATA", socket.EAI_NONAME)}:
                return "域名无法访问"
            return None
        except (OSError, ValueError):
            return None
    # urllib supplies HTTP(S)_PROXY/NO_PROXY handling, but ALL_PROXY needs an
    # explicit scheme fallback. Do not persist proxy URLs or credentials.
    if proxies.get("all"):
        proxies.setdefault("http", proxies["all"])
        proxies.setdefault("https", proxies["all"])
    opener = urllib.request.build_opener(urllib.request.ProxyHandler(proxies))
    # A proxied TLS round trip regularly takes 2-4s here, so a 2s HEAD followed
    # by a 2s GET timed out before either answered. Spend the budget on one
    # HEAD; fall back to GET only when the server answered but rejected HEAD.
    for method, timeout in (("HEAD", 4.0), ("GET", 2.0)):
        try:
            request = urllib.request.Request(url, method=method, headers={"User-Agent": "Openmd/0.0.1"})
            with opener.open(request, timeout=timeout):
                return None
        except urllib.error.HTTPError as exc:
            code = exc.code
            exc.close()
            if code in {400, 404, 410, 414, 422}:
                label = "页面不存在" if code in {404, 410} else "页面无法访问"
                return f"{label}（{code}）"
        except (OSError, ValueError):
            return None
    return None


def network_preflight(url: str, *, timeout: float = 4.8) -> str | None:
    """Stop only on a positive DNS/not-found signal, never on uncertainty.

    DNS resolution itself has no portable socket timeout. Keep its worker a
    daemon and bound the entire wait so a slow resolver cannot stall the CLI.
    No response body or credentials are read or persisted.
    """
    results: queue.Queue[str | None] = queue.Queue(maxsize=1)

    def probe() -> None:
        try:
            results.put(_probe_url(url))
        except Exception:
            results.put(None)

    threading.Thread(target=probe, name="openmd-url-preflight", daemon=True).start()
    try:
        return results.get(timeout=timeout)
    except queue.Empty:
        return None
