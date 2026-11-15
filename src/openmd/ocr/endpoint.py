"""Fail closed before sending document bytes to an Ollama endpoint."""

from __future__ import annotations

import ipaddress
import re
import socket
from urllib.parse import urlsplit, urlunsplit


class EndpointError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"{code}: {message}")


def _loopback_address(value: str):
    address = ipaddress.ip_address(value)
    # Mapped IPv4 and scoped IPv6 are deliberately outside the endpoint contract.
    if "%" in value or getattr(address, "ipv4_mapped", None) is not None or not address.is_loopback:
        raise EndpointError("ocr_ollama_url_not_loopback", "Ollama must use an unscoped loopback address")
    return address


def validate_ollama_url(url: str) -> str:
    """Validate and pin a URL to a numeric loopback address.

    DNS names are accepted only when *every* returned address is loopback. The
    returned numeric URL prevents a second DNS lookup from changing the target
    between validation and the connection. Call this at each HTTP entry point;
    callers must also reject HTTP redirects rather than follow unvalidated URLs.
    """
    invalid = "ocr_ollama_url_invalid"
    if not isinstance(url, str) or any(c.isspace() or ord(c) < 32 for c in url) or "\\" in url:
        raise EndpointError(invalid, "expected HTTP(S) loopback URL without credentials")
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise EndpointError(invalid, "invalid host or port") from exc
    if parsed.scheme not in {"http", "https"} or not host or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment or "%" in host or (port is not None and port < 1):
        raise EndpointError(invalid, "expected HTTP(S) loopback URL without credentials, query or fragment")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        # OS resolvers also accept legacy numeric IPv4 forms (2130706433,
        # 0177.0.0.1, 127.1, 0x7f000001). Never pass these to the resolver.
        if ":" in host or re.fullmatch(r"(?:0[xX][0-9a-fA-F]+|[0-9]+)(?:\.(?:0[xX][0-9a-fA-F]+|[0-9]+))*\.?", host):
            raise EndpointError("ocr_ollama_url_not_loopback", "noncanonical IP address is not allowed")
        try:
            answers = socket.getaddrinfo(host, port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
        except OSError as exc:
            raise EndpointError("ocr_ollama_url_resolution_failed", "cannot establish that every Ollama address is loopback") from exc
        if not answers:
            raise EndpointError("ocr_ollama_url_resolution_failed", "Ollama hostname resolved to no addresses")
        addresses = []
        for family, _, _, _, sockaddr in answers:
            if family not in {socket.AF_INET, socket.AF_INET6}:
                raise EndpointError("ocr_ollama_url_not_loopback", "unsupported Ollama address family")
            try:
                addresses.append(_loopback_address(sockaddr[0]))
            except ValueError as exc:
                if isinstance(exc, EndpointError):
                    raise
                raise EndpointError("ocr_ollama_url_not_loopback", "invalid resolved Ollama address") from exc
        address = addresses[0]
    address = _loopback_address(str(address))
    authority = f"[{address}]" if address.version == 6 else str(address)
    if port is not None:
        authority += f":{port}"
    return urlunsplit((parsed.scheme, authority, parsed.path.rstrip("/"), "", ""))
