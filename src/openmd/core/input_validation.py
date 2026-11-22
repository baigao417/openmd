"""Daily-input classification and bounded, best-effort public URL preflight."""

from __future__ import annotations

import queue
import re
import socket
import threading
import urllib.error
import urllib.request
from urllib.parse import quote, urlparse, urlsplit, urlunsplit

from .detector import _local_path_for_detection


INPUT_HINT = "请粘贴 http(s) 链接或本地文件路径"

_URI_SAFE = ":/?#[]@!$&'()*+,;=-._~"
_VALID_PERCENT = re.compile(r"%[0-9A-Fa-f]{2}")


def _quote_iri_component(value: str, safe: str) -> str:
    """UTF-8 encode IRI characters while preserving valid URI escapes."""
    output: list[str] = []
    start = 0
    for match in _VALID_PERCENT.finditer(value):
        output.append(quote(value[start:match.start()], safe=safe))
        output.append(match.group(0))
        start = match.end()
    output.append(quote(value[start:], safe=safe))
    return "".join(output)


def normalize_http_url(value: str) -> str:
    """Convert an HTTP(S) IRI to an ASCII URI without changing other inputs."""
    if not isinstance(value, str) or not value.lower().startswith(("http://", "https://")):
        return value
    parsed = urlsplit(value)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return value

    netloc = parsed.netloc
    userinfo, separator, hostport = netloc.rpartition("@")
    prefix = _quote_iri_component(userinfo, safe="!$&'()*+,;=:") + separator if separator else ""
    host = parsed.hostname
    if ":" in host:  # IPv6 literals are already ASCII and need brackets restored.
        host_text = f"[{host}]"
    else:
        try:
            host_text = host.encode("idna").decode("ascii")
        except UnicodeError:
            return value
    try:
        parsed.port  # Validate malformed/out-of-range ports without rewriting their spelling.
    except ValueError:
        return value
    if hostport.startswith("["):
        closing = hostport.find("]")
        port = hostport[closing + 1:]
    else:
        _, separator, port_text = hostport.rpartition(":")
        port = f":{port_text}" if separator else ""
    authority = prefix + host_text + port
    path = _quote_iri_component(parsed.path, safe=_URI_SAFE)
    query = _quote_iri_component(parsed.query, safe=_URI_SAFE)
    fragment = _quote_iri_component(parsed.fragment, safe=_URI_SAFE)
    normalized = urlunsplit((parsed.scheme.lower(), authority, path, query, fragment))
    before_fragment, fragment_separator, _raw_fragment = value.partition("#")
    if "?" in before_fragment and not parsed.query:
        normalized += "?"
    if fragment_separator and not parsed.fragment:
        normalized += "#"
    return normalized


def valid_daily_input(value: str) -> bool:
    try:
        value = normalize_http_url(value)
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
            # Some sites (Douyin) answer HEAD with 404 for pages that GET serves
            # with 200; a HEAD rejection is only confirmed by the GET.
            if method == "HEAD":
                continue
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
    url = normalize_http_url(url)
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
