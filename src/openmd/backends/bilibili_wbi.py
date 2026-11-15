"""Bilibili WBI signing helpers for Openmd backends.

This module ports only the request-signing core needed by Openmd's public API
wrappers. It does not read browser profiles, local app tokens, or cookie stores.

WBI core reference: Bilibili-Harvest, Copyright (c) 2023 Lanbin07, MIT.
See THIRD_PARTY_NOTICES.md and LICENSES/Bilibili-Harvest-MIT.txt.
"""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
import re
import threading
import time
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping
from urllib.parse import urlencode, urlparse


BILIBILI_COOKIE_HEADER_ENV = "OPENMD_BILIBILI_COOKIE_HEADER"
BILIBILI_COOKIE_FILE_ENV = "OPENMD_BILIBILI_COOKIE_FILE"
BILIBILI_NAV_URL = "https://api.bilibili.com/x/web-interface/nav"
BILIBILI_WEB_REFERER = "https://www.bilibili.com/"
BILIBILI_WBI_KEY_TTL_SECONDS = 12 * 60 * 60

MIXIN_KEY_ENC_TAB: tuple[int, ...] = (
    46,
    47,
    18,
    2,
    53,
    8,
    23,
    32,
    15,
    50,
    10,
    31,
    58,
    3,
    45,
    35,
    27,
    43,
    5,
    49,
    33,
    9,
    42,
    19,
    29,
    28,
    14,
    39,
    12,
    38,
    41,
    13,
    37,
    48,
    7,
    16,
    24,
    55,
    40,
    61,
    26,
    17,
    0,
    1,
    60,
    51,
    30,
    4,
    22,
    25,
    54,
    21,
    56,
    59,
    6,
    63,
    57,
    62,
    11,
    36,
    20,
    34,
    44,
    52,
)

HttpFetcher = Callable[[str, dict[str, str], int], tuple[int, str]]
TimeProvider = Callable[[], float]

_INVALID_WBI_VALUE = re.compile(r"[!'()*]")


class WbiSigningError(RuntimeError):
    """Raised when WBI keys cannot be fetched or parsed."""


def bilibili_cookie_header(environ: Mapping[str, str] | None = None) -> str | None:
    """Read an explicit user-owned header file, or the legacy environment value."""
    config = os.environ if environ is None else environ
    cookie_path = config.get(BILIBILI_COOKIE_FILE_ENV, "").strip()
    if cookie_path:
        try:
            value = Path(cookie_path).read_text(encoding="utf-8-sig").strip()
        except (OSError, UnicodeError):
            raise WbiSigningError("The user-provided cookie header file is unreadable.") from None
        if "\r" in value or "\n" in value:
            raise WbiSigningError("The user-provided cookie header must be a single line.")
        return value or None
    value = config.get(BILIBILI_COOKIE_HEADER_ENV, "").strip()
    return value or None


def bilibili_cookie_header_for_url(
    url: str,
    environ: Mapping[str, str] | None = None,
) -> str | None:
    """Return env cookie only for Bilibili-owned API/page hosts."""
    host = (urlparse(url).hostname or "").lower()
    if host == "bilibili.com" or host.endswith(".bilibili.com"):
        return bilibili_cookie_header(environ)
    return None


def bilibili_headers(
    *,
    cookie_header: str | None = None,
    referer: str = BILIBILI_WEB_REFERER,
) -> dict[str, str]:
    """Build browser-like Bilibili headers without logging their contents."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Referer": referer,
        "Origin": "https://www.bilibili.com",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "X-Wbi-UA": "Win32.Chrome.109.0.0.0",
    }
    if cookie_header:
        headers["Cookie"] = cookie_header.strip()
    return headers


class BilibiliWbiSigner:
    """Fetch, cache, and apply Bilibili WBI keys for signed endpoints."""

    def __init__(
        self,
        *,
        fetcher: HttpFetcher,
        timeout: int = 30,
        ttl_seconds: int = BILIBILI_WBI_KEY_TTL_SECONDS,
        time_provider: TimeProvider | None = None,
    ) -> None:
        self._fetcher = fetcher
        self._timeout = timeout
        self._ttl_seconds = ttl_seconds
        self._time_provider = time_provider or time.time
        self._lock = threading.Lock()
        self._cached_img_key = ""
        self._cached_sub_key = ""
        self._cached_at = 0.0

    def get_keys(
        self,
        *,
        force_refresh: bool = False,
        timeout: int | None = None,
    ) -> tuple[str, str]:
        now = self._time_provider()
        with self._lock:
            if not force_refresh and self._is_cache_valid(now):
                return self._cached_img_key, self._cached_sub_key

            img_key, sub_key = self._refresh_keys(timeout=timeout or self._timeout)
            self._cached_img_key = img_key
            self._cached_sub_key = sub_key
            self._cached_at = now
            return img_key, sub_key

    def sign_params(
        self,
        params: Mapping[str, Any] | None,
        *,
        force_refresh: bool = False,
        timeout: int | None = None,
    ) -> dict[str, str]:
        img_key, sub_key = self.get_keys(force_refresh=force_refresh, timeout=timeout)
        return sign_wbi_params_with_keys(
            params,
            img_key=img_key,
            sub_key=sub_key,
            wts=int(self._time_provider()),
        )

    def _is_cache_valid(self, now: float) -> bool:
        if not self._cached_img_key or not self._cached_sub_key:
            return False
        return now - self._cached_at <= self._ttl_seconds

    def _refresh_keys(self, *, timeout: int) -> tuple[str, str]:
        status, body = self._fetcher(BILIBILI_NAV_URL, {}, timeout)
        if status < 200 or status >= 300:
            raise WbiSigningError(f"Bilibili WBI nav request failed with HTTP {status}.")

        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            raise WbiSigningError("Bilibili WBI nav response was not valid JSON.") from exc
        if not isinstance(payload, dict):
            raise WbiSigningError("Bilibili WBI nav response was not a JSON object.")

        code = payload.get("code", 0)
        if code not in {0, "0", None}:
            raise WbiSigningError(f"Bilibili WBI nav returned code {code}.")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise WbiSigningError("Bilibili WBI nav response did not include data.")
        wbi_img = data.get("wbi_img")
        if not isinstance(wbi_img, dict):
            raise WbiSigningError("Bilibili WBI nav response did not include wbi_img.")

        img_key = _key_from_asset_url(str(wbi_img.get("img_url") or ""))
        sub_key = _key_from_asset_url(str(wbi_img.get("sub_url") or ""))
        if not img_key or not sub_key:
            raise WbiSigningError("Bilibili WBI nav response did not include usable keys.")
        return img_key, sub_key


def sign_wbi_params_with_keys(
    params: Mapping[str, Any] | None,
    *,
    img_key: str,
    sub_key: str,
    wts: int | str | None = None,
) -> dict[str, str]:
    """Sign params with already-known WBI keys; useful for deterministic tests."""
    signed = {
        str(key): _normalize_wbi_value(value)
        for key, value in (params or {}).items()
        if value is not None
    }
    signed["wts"] = str(int(time.time() if wts is None else wts))
    query = urlencode(sorted(signed.items()), safe="")
    signed["w_rid"] = hashlib.md5((query + _mixin_key(img_key, sub_key)).encode("utf-8")).hexdigest()
    return signed


def _mixin_key(img_key: str, sub_key: str) -> str:
    source = f"{img_key}{sub_key}"
    mixed = "".join(source[index] for index in MIXIN_KEY_ENC_TAB if index < len(source))
    return mixed[:32]


def _normalize_wbi_value(value: Any) -> str:
    return _INVALID_WBI_VALUE.sub("", str(value))


def _key_from_asset_url(value: str) -> str:
    parsed = urlparse(value.strip())
    filename = posixpath.basename(parsed.path) or PurePosixPath(value).name
    if "." in filename:
        return filename.rsplit(".", 1)[0]
    return filename
