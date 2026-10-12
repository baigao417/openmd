"""Discovery wrappers for WeChat Official Account article batches.

These wrappers parse sanitized article-list data from an explicitly enabled
local/private ``wechat-article-exporter`` style sidecar. They never start a
service, open a browser, log in, import cookies, or persist raw exporter/API
output by themselves.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlparse, urlunparse
from urllib.request import Request as UrlRequest
from urllib.request import urlopen

from openmd.backends.base import DiscoveryRequest, DiscoveryResult
from openmd.backends.weixin import normalize_weixin_url
from openmd.core.models import ContentItem
from openmd.core.secrets import sanitize_error_summary


WECHAT_ARTICLE_EXPORTER_DISCOVERY_BACKEND = "wechat-article-exporter"
WECHAT_ARTICLE_EXPORTER_HTTP_DISCOVERY_BACKEND = "wechat-article-exporter.http"
DEFAULT_EXPORTER_TIMEOUT_SECONDS = 180
DEFAULT_EXPORTER_BASE_URL = "http://127.0.0.1:3000"
EXPORTER_BASE_URL_ENV = "OPENMD_WECHAT_ARTICLE_EXPORTER_BASE_URL"
EXPORTER_AUTH_KEY_ENV = "OPENMD_WECHAT_ARTICLE_EXPORTER_AUTH_KEY"
WEIXIN_SELECTOR_SCHEMES: frozenset[str] = frozenset(
    {"weixin", "wechat", "wechat-official", "wx-official"}
)
CommandRunner = Callable[[list[str], int], tuple[int, str, str]]
HttpJsonFetcher = Callable[[str, dict[str, str], int], tuple[int, str]]


@dataclass(frozen=True)
class ParsedWeixinArticle:
    url: str
    title: str | None = None
    author: str | None = None
    published_at: str | None = None
    article_id: str | None = None


class WechatArticleExporterDiscoveryProvider:
    """Parse article list JSON from an explicitly enabled local exporter."""

    name = WECHAT_ARTICLE_EXPORTER_DISCOVERY_BACKEND

    def __init__(
        self,
        *,
        runner: CommandRunner | None = None,
        timeout: int = DEFAULT_EXPORTER_TIMEOUT_SECONDS,
    ) -> None:
        self._runner = runner or _run_command
        self.timeout = timeout

    def discover(self, request: DiscoveryRequest) -> DiscoveryResult:
        command = build_wechat_article_exporter_args(
            request.input_url,
            limit=request.limit,
        )
        exit_code, stdout, stderr = self._runner(command, self.timeout)
        if exit_code != 0:
            return DiscoveryResult.failure(
                provider=self.name,
                error_code=_exit_error_code(exit_code),
                error_summary=sanitize_error_summary(stderr or stdout)
                or "wechat-article-exporter discovery failed.",
                source_notes=["exporter command failed without persisting raw stdout/stderr"],
                metadata={"platform": request.platform, "limit": request.limit},
            )

        try:
            parsed = parse_wechat_article_exporter_output(stdout)
        except ValueError as exc:
            return DiscoveryResult.failure(
                provider=self.name,
                error_code="invalid_discovery_output",
                error_summary=sanitize_error_summary(exc),
                source_notes=["exporter stdout was invalid JSON and was not persisted"],
                metadata={"platform": request.platform, "limit": request.limit},
            )
        if request.limit is not None and not request.allow_all:
            parsed = parsed[: max(0, request.limit)]
        if not parsed:
            return DiscoveryResult.failure(
                provider=self.name,
                error_code="empty_result",
                error_summary="wechat-article-exporter returned no article URLs.",
                source_notes=["exporter stdout was parsed in memory and was not persisted"],
                metadata={"platform": request.platform, "limit": request.limit},
            )

        return DiscoveryResult.success(
            provider=self.name,
            items=[_item_from_article(article, request, provider=self.name) for article in parsed],
            source_notes=[
                "wechat-article-exporter JSON was parsed in memory.",
                "No browser/login/service lifecycle action was attempted by Openmd.",
            ],
            metadata={
                "platform": request.platform,
                "limit": request.limit,
                "normalized_url": request.input_url,
            },
        )


class WechatArticleExporterHttpDiscoveryProvider:
    """Discover account articles through a running ``wechat-article-exporter`` API.

    The provider only calls read-only public API endpoints on an already-running
    sidecar. The sidecar owns any login/session/cookie state; Openmd receives
    sanitized account/article JSON and never logs or persists auth headers.
    """

    name = WECHAT_ARTICLE_EXPORTER_HTTP_DISCOVERY_BACKEND

    def __init__(
        self,
        *,
        base_url: str | None = None,
        auth_key: str | None = None,
        fetch_json: HttpJsonFetcher | None = None,
        timeout: int = DEFAULT_EXPORTER_TIMEOUT_SECONDS,
        page_size: int = 20,
    ) -> None:
        self.base_url = _normalize_base_url(base_url or os.environ.get(EXPORTER_BASE_URL_ENV) or DEFAULT_EXPORTER_BASE_URL)
        self.auth_key = auth_key if auth_key is not None else os.environ.get(EXPORTER_AUTH_KEY_ENV)
        self._fetch_json = fetch_json or _fetch_json
        self.timeout = timeout
        self.page_size = min(20, max(1, page_size))

    def discover(self, request: DiscoveryRequest) -> DiscoveryResult:
        if not self.auth_key:
            return DiscoveryResult.failure(
                provider=self.name,
                error_code="auth_key_required",
                error_summary=(
                    "Set OPENMD_WECHAT_ARTICLE_EXPORTER_AUTH_KEY after logging in to "
                    "the local wechat-article-exporter sidecar."
                ),
                source_notes=[
                    "Openmd did not attempt login, cookie import, or browser automation.",
                    "The sidecar must already be running and authenticated by the operator.",
                ],
                metadata={"platform": request.platform, "limit": request.limit, "sidecar_base_url": self.base_url},
            )

        fakeid, account_metadata, account_error = self._resolve_fakeid(request.input_url)
        if account_error is not None:
            return account_error
        if not fakeid:
            return DiscoveryResult.failure(
                provider=self.name,
                error_code="account_not_found",
                error_summary="wechat-article-exporter could not resolve an account fakeid for the selector.",
                source_notes=["No raw account lookup response was persisted."],
                metadata={"platform": request.platform, "limit": request.limit, "sidecar_base_url": self.base_url},
            )

        articles: list[ParsedWeixinArticle] = []
        begin = 0
        target_count = request.limit if request.limit is not None and not request.allow_all else None
        while True:
            size = self.page_size if target_count is None else min(self.page_size, max(0, target_count - len(articles)))
            if size <= 0:
                break
            payload, error = self._get_json(
                "/api/public/v1/article",
                {"fakeid": fakeid, "begin": str(begin), "size": str(size)},
            )
            if error is not None:
                return error

            parsed = parse_wechat_article_exporter_payload(payload)
            if not parsed:
                break
            articles.extend(parsed)
            if target_count is not None and len(articles) >= target_count:
                break
            if len(parsed) < size:
                break
            begin += size

        if target_count is not None:
            articles = articles[: max(0, target_count)]
        if not articles:
            return DiscoveryResult.failure(
                provider=self.name,
                error_code="empty_result",
                error_summary="wechat-article-exporter returned no article URLs.",
                source_notes=["Article-list JSON was parsed in memory and was not persisted."],
                metadata={
                    "platform": request.platform,
                    "limit": request.limit,
                    "sidecar_base_url": self.base_url,
                    **account_metadata,
                },
            )

        return DiscoveryResult.success(
            provider=self.name,
            items=[_item_from_article(article, request, provider=self.name) for article in articles],
            source_notes=[
                "wechat-article-exporter HTTP API was parsed in memory.",
                "Openmd called read-only account/article endpoints on an already-running sidecar.",
                "Openmd did not start/stop the sidecar, log in, import cookies, or persist auth headers.",
            ],
            metadata={
                "platform": request.platform,
                "limit": request.limit,
                "normalized_url": request.input_url,
                "sidecar_base_url": self.base_url,
                **account_metadata,
            },
        )

    def _resolve_fakeid(self, selector: str) -> tuple[str | None, dict[str, Any], DiscoveryResult | None]:
        parsed = urlparse(selector.strip())
        if parsed.scheme in WEIXIN_SELECTOR_SCHEMES and parsed.netloc in {"fakeid", "biz"}:
            fakeid = parsed.path.strip("/") or parsed.query.strip()
            return fakeid or None, {"account_selector_type": parsed.netloc}, None
        if parsed.scheme in WEIXIN_SELECTOR_SCHEMES and parsed.netloc == "account":
            keyword = parsed.path.strip("/") or parsed.query.strip()
            return self._search_account(keyword)
        if _looks_like_weixin_article(normalize_weixin_url(selector)):
            payload, error = self._get_json("/api/public/v1/accountbyurl", {"url": normalize_weixin_url(selector)})
            if error is not None:
                return None, {}, error
            return _fakeid_from_account_payload(payload), {"account_selector_type": "article_url"}, None
        if parsed.scheme in WEIXIN_SELECTOR_SCHEMES:
            keyword = parsed.netloc if parsed.netloc not in {"author", "official", "oa"} else parsed.path.strip("/")
            if not keyword and parsed.netloc:
                keyword = parsed.netloc
            return self._search_account(keyword)
        return self._search_account(selector.strip())

    def _search_account(self, keyword: str) -> tuple[str | None, dict[str, Any], DiscoveryResult | None]:
        if not keyword:
            return None, {}, DiscoveryResult.failure(
                provider=self.name,
                error_code="account_selector_empty",
                error_summary="Weixin account selector is empty.",
                metadata={"sidecar_base_url": self.base_url},
            )
        payload, error = self._get_json("/api/public/v1/account", {"keyword": keyword, "begin": "0", "size": "5"})
        if error is not None:
            return None, {}, error
        account = _best_account(payload, keyword)
        if account is None:
            return None, {"account_keyword": keyword}, None
        fakeid = _string_field(account, "fakeid")
        metadata = {
            "account_keyword": keyword,
            "account_nickname": _string_field(account, "nickname"),
            "account_alias": _string_field(account, "alias"),
        }
        return fakeid, metadata, None

    def _get_json(self, path: str, query: dict[str, str]) -> tuple[object | None, DiscoveryResult | None]:
        url = _build_url(self.base_url, path, query)
        status, body = self._fetch_json(url, {"X-Auth-Key": self.auth_key or ""}, self.timeout)
        if status < 200 or status >= 300:
            return None, DiscoveryResult.failure(
                provider=self.name,
                error_code=_http_error_code(status),
                error_summary=sanitize_error_summary(body) or f"wechat-article-exporter HTTP {status}",
                source_notes=["HTTP error body was sanitized and not persisted."],
                metadata={"sidecar_base_url": self.base_url},
            )
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            return None, DiscoveryResult.failure(
                provider=self.name,
                error_code="invalid_discovery_output",
                error_summary="wechat-article-exporter did not return valid JSON.",
                source_notes=["Invalid response body was not persisted."],
                metadata={"sidecar_base_url": self.base_url},
            )
        base_resp = payload.get("base_resp") if isinstance(payload, dict) else None
        if isinstance(base_resp, dict) and int(base_resp.get("ret") or 0) != 0:
            return None, DiscoveryResult.failure(
                provider=self.name,
                error_code=_api_error_code(base_resp),
                error_summary=sanitize_error_summary(base_resp.get("err_msg")) or "wechat-article-exporter API error.",
                source_notes=["API error payload was sanitized and not persisted."],
                metadata={"sidecar_base_url": self.base_url},
            )
        return payload, None


def build_wechat_article_exporter_args(selector: str, *, limit: int | None) -> list[str]:
    command = [
        "wechat-article-exporter",
        "articles",
        "--selector",
        selector,
        "--format",
        "json",
    ]
    if limit is not None:
        command.extend(["--limit", str(max(0, limit))])
    return command


def parse_wechat_article_exporter_output(stdout: str) -> list[ParsedWeixinArticle]:
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise ValueError("wechat-article-exporter did not return valid JSON") from exc

    return parse_wechat_article_exporter_payload(payload)


def parse_wechat_article_exporter_payload(payload: object) -> list[ParsedWeixinArticle]:
    rows = list(_candidate_rows(payload))
    articles: list[ParsedWeixinArticle] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        article = _parse_article_row(row)
        if article is not None:
            articles.append(article)
    return articles


def _candidate_rows(payload: object) -> Iterable[object]:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return ()
    for key in ("articles", "items", "list", "data", "records", "results"):
        value = payload.get(key)
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            nested = _candidate_rows(value)
            nested_list = list(nested)
            if nested_list:
                return nested_list
    return ()


def _parse_article_row(row: dict[str, Any]) -> ParsedWeixinArticle | None:
    url = _first_string(row, "url", "link", "article_url", "articleUrl", "content_url", "contentUrl")
    if not url:
        return None
    normalized = normalize_weixin_url(url)
    if not _looks_like_weixin_article(normalized):
        return None
    return ParsedWeixinArticle(
        url=normalized,
        title=_first_string(row, "title", "name"),
        author=_first_string(row, "author", "account", "nickname", "author_name", "authorName"),
        published_at=_first_string(row, "published_at", "publish_time", "publishTime", "date", "time"),
        article_id=_first_string(row, "id", "article_id", "articleId", "msgid", "mid"),
    )


def _first_string(row: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, int):
            return str(value)
    return None


def _string_field(row: dict[str, Any], key: str) -> str | None:
    value = row.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, int):
        return str(value)
    return None


def _best_account(payload: object, keyword: str) -> dict[str, Any] | None:
    candidates = [row for row in _candidate_rows(payload) if isinstance(row, dict)]
    if not candidates:
        return None
    normalized_keyword = keyword.strip().lower()
    for row in candidates:
        for key in ("nickname", "alias"):
            value = _string_field(row, key)
            if value and value.strip().lower() == normalized_keyword:
                return row
    return candidates[0]


def _fakeid_from_account_payload(payload: object) -> str | None:
    account = _best_account(payload, "")
    if account is None:
        return None
    return _string_field(account, "fakeid")


def _looks_like_weixin_article(url: str) -> bool:
    parsed = urlparse(url)
    return (
        parsed.scheme in {"http", "https"}
        and parsed.netloc.lower() == "mp.weixin.qq.com"
        and (parsed.path == "/s" or parsed.path.startswith("/s/"))
    )


def _normalize_base_url(value: str) -> str:
    parsed = urlparse(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return DEFAULT_EXPORTER_BASE_URL
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", "", ""))


def _build_url(base_url: str, path: str, query: dict[str, str]) -> str:
    safe_path = "/" + path.strip("/")
    return f"{base_url}{safe_path}?{urlencode(query, quote_via=quote)}"


def _fetch_json(url: str, headers: dict[str, str], timeout: int) -> tuple[int, str]:
    request = UrlRequest(url, headers={key: value for key, value in headers.items() if value})
    try:
        with urlopen(request, timeout=timeout) as response:
            body = response.read().decode(response.headers.get_content_charset() or "utf-8", errors="replace")
            return response.status, body
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return exc.code, body
    except URLError as exc:
        return 599, sanitize_error_summary(exc) or "wechat-article-exporter sidecar is unreachable."


def _http_error_code(status: int) -> str:
    if status == 401 or status == 403:
        return "auth_required"
    if status == 404:
        return "sidecar_endpoint_not_found"
    if status == 599:
        return "sidecar_unreachable"
    return f"sidecar_http_{status}"


def _api_error_code(base_resp: dict[str, Any]) -> str:
    ret = int(base_resp.get("ret") or -1)
    if ret in {-1, 200003}:
        return "auth_required"
    return f"api_ret_{ret}"


def _item_from_article(
    article: ParsedWeixinArticle,
    request: DiscoveryRequest,
    *,
    provider: str,
) -> ContentItem:
    return ContentItem(
        platform="weixin",
        input_type="single",
        url=article.url,
        id=article.article_id,
        title=article.title,
        author=article.author,
        published_at=article.published_at,
        source_type="article",
        raw_meta={
            "profile": "article",
            "permission_scope": "user_accessible",
            "collection_url": request.input_url,
            "discovery_provider": provider,
            "discovery_limit": request.limit,
        },
    )


def _run_command(command: list[str], timeout: int) -> tuple[int, str, str]:
    resolved = shutil.which(command[0]) or command[0]
    try:
        completed = subprocess.run(
            [resolved, *command[1:]],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError:
        return 127, "", "wechat-article-exporter executable is not available on PATH."
    except subprocess.TimeoutExpired:
        return 124, "", f"wechat-article-exporter timed out after {timeout}s."
    return completed.returncode, completed.stdout or "", completed.stderr or ""


def _exit_error_code(exit_code: int) -> str:
    if exit_code == 127:
        return "discovery_tool_missing"
    if exit_code == 124:
        return "discovery_timeout"
    return f"discovery_exit_{exit_code}"
