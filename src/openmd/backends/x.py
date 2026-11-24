"""X/Twitter-specific opt-in runtime backend providers.

This module is loaded by ``openmd.backends.registry`` through the platform
extension naming convention. It wraps read-only OpenCLI ``twitter`` commands
behind ``--enable-browser-backends`` and intentionally excludes media download,
ASR, account mutation, cookies, twscrape, proxies, and raw stdout/stderr
persistence.
"""

from __future__ import annotations

import re
import shutil
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlparse

from openmd.backends.base import (
    BackendCapability,
    BackendDescriptor,
    BackendExtractRequest,
    BackendHealth,
)
from openmd.backends.executor import BackendProviderRegistry
from openmd.backends.registry import BackendRuntimeOptions
from openmd.core.models import ContentItem, TranscriptResult
from openmd.core.secrets import sanitize_error_summary, sanitize_url

from .youtube_discovery import _run_opencli


OPENCLI_TWITTER_THREAD_BACKEND = "opencli.twitter.thread"
OPENCLI_TWITTER_TWEETS_BACKEND = "opencli.twitter.tweets"
OPENCLI_TWITTER_ARTICLE_BACKEND = "opencli.twitter.article"
FXTWITTER_BACKEND = "openmd.x.public"
X_BROWSER_BACKEND = "openmd.x.logged_in_browser"

DEFAULT_THREAD_LIMIT = 50
DEFAULT_TWEETS_LIMIT = 20
MAX_X_TEXT_LIMIT = 100

OpenCliRunner = Callable[..., tuple[int, str, str]]

X_STATUS_RE = re.compile(r"/status(?:es)?/(\d+)")
USERNAME_RE = re.compile(r"@?([A-Za-z0-9_]{1,15})$")
RESERVED_X_PATHS = frozenset(
    {
        "compose",
        "explore",
        "home",
        "i",
        "intent",
        "messages",
        "notifications",
        "search",
        "settings",
        "share",
    }
)


def register_backend_providers(
    registry: BackendProviderRegistry,
    options: BackendRuntimeOptions,
) -> None:
    """Register anonymous network providers and gated browser fallback."""
    requested = options.backend_override
    if options.enable_network_backends and requested in {None, FXTWITTER_BACKEND}:
        registry.register(FXTWITTER_BACKEND, XPublicLayeredProvider())
    if not options.enable_browser_backends:
        return
    if requested in {None, X_BROWSER_BACKEND}:
        from openmd.backends.x_browser import XLoggedInBrowserProvider

        registry.register(X_BROWSER_BACKEND, XLoggedInBrowserProvider())
    if requested in {None, OPENCLI_TWITTER_THREAD_BACKEND}:
        registry.register(
            OPENCLI_TWITTER_THREAD_BACKEND,
            OpenCliTwitterTextProvider(default_backend=OPENCLI_TWITTER_THREAD_BACKEND),
        )
    if requested in {None, OPENCLI_TWITTER_TWEETS_BACKEND}:
        registry.register(
            OPENCLI_TWITTER_TWEETS_BACKEND,
            OpenCliTwitterTextProvider(default_backend=OPENCLI_TWITTER_TWEETS_BACKEND),
        )
    if requested in {None, OPENCLI_TWITTER_ARTICLE_BACKEND}:
        registry.register(
            OPENCLI_TWITTER_ARTICLE_BACKEND,
            OpenCliTwitterTextProvider(default_backend=OPENCLI_TWITTER_ARTICLE_BACKEND),
        )
    from openmd.backends.kaze_snapshot import register_backend_providers as register_kaze_snapshot

    register_kaze_snapshot(registry, options)


def build_opencli_twitter_thread_args(tweet_id: str, *, limit: int) -> list[str]:
    return [
        "opencli",
        "twitter",
        "thread",
        tweet_id,
        "--limit",
        str(_bounded_limit(limit, default=DEFAULT_THREAD_LIMIT)),
        "-f",
        "md",
        *_browser_options(),
    ]


def build_opencli_twitter_tweets_args(username: str, *, limit: int) -> list[str]:
    return [
        "opencli",
        "twitter",
        "tweets",
        username,
        "--limit",
        str(_bounded_limit(limit, default=DEFAULT_TWEETS_LIMIT)),
        "-f",
        "md",
        *_browser_options(),
    ]


def build_opencli_twitter_article_args(tweet_ref: str) -> list[str]:
    return [
        "opencli",
        "twitter",
        "article",
        tweet_ref,
        "-f",
        "md",
        *_browser_options(),
    ]


def classify_twitter_failure(message: object) -> str:
    """Map known X/OpenCLI read blockers to stable failure codes."""

    text = str(message or "").lower()
    if "timed out" in text or "timeout" in text:
        return "opencli_twitter_timeout"
    if (
        "login" in text
        or "sign in" in text
        or "auth" in text
        or "cookie" in text
        or "not logged in" in text
    ):
        return "x_auth_required"
    if "rate limit" in text or "too many requests" in text or "429" in text:
        return "x_rate_limited"
    if "not found" in text or "does not exist" in text or "doesn't exist" in text or "404" in text:
        return "x_not_found"
    if "protected" in text or "private" in text or "forbidden" in text or "not authorized" in text:
        return "x_access_denied"
    if "empty" in text or "no tweets" in text or "no content" in text:
        return "x_empty_result"
    return "opencli_twitter_failed"


class OpenCliTwitterTextProvider:
    """Read X/Twitter post, thread, article, or user text through OpenCLI."""

    def __init__(
        self,
        *,
        default_backend: str = OPENCLI_TWITTER_THREAD_BACKEND,
        runner: OpenCliRunner | None = None,
        timeout: int = 180,
    ) -> None:
        self.name = default_backend
        self._runner = runner or _run_opencli
        self.timeout = timeout

    def descriptor(self) -> BackendDescriptor:
        capability = BackendCapability(
            platforms=("x",),
            source_types=("article", "mixed"),
            input_types=("single", "batch", "collection"),
            source_levels=("native_text", "partial_extraction"),
            requires_network=True,
            requires_credentials=False,
            optional_config=("limit", "browser_session"),
        )
        return BackendDescriptor(
            name=self.name,
            layer="L3 browser/session fallback",
            capabilities=(capability,),
            description="Read-only X/Twitter text extraction through OpenCLI twitter commands.",
            provider="opencli.twitter",
        )

    def health(self) -> BackendHealth:
        if self._runner is not _run_opencli:
            return BackendHealth(ok=True, dependencies={"opencli": "injected"})
        if shutil.which("opencli") is None and shutil.which("opencli.cmd") is None:
            return BackendHealth(
                ok=False,
                dependencies={"opencli": "missing"},
                error_code="backend_dependency_missing",
                error_summary="opencli executable is not available on PATH.",
            )
        return BackendHealth(ok=True, dependencies={"opencli": "available"})

    def extract(self, request: BackendExtractRequest) -> TranscriptResult:
        item = request.item
        requested_backend = request.primary_backend
        if item.platform != "x":
            return _failed(
                requested_backend,
                "unsupported_platform",
                f"OpenCLI twitter provider only supports x, got {item.platform}.",
            )

        command_plan = _command_plan_for(item, requested_backend)
        if command_plan.error_code is not None:
            return _failed(requested_backend, command_plan.error_code, command_plan.error_summary)

        exit_code, stdout, stderr = self._runner(command_plan.args, timeout=self.timeout)
        text = stdout.strip()
        cleanup_warning = _is_cleanup_only_warning(stderr)
        if exit_code != 0 and not (text and cleanup_warning):
            signal = _prefer_original_error(stderr, stdout) or f"OpenCLI exited with code {exit_code}."
            return _failed(command_plan.backend, classify_twitter_failure(signal), signal)

        if not text:
            return _failed(
                command_plan.backend,
                "x_empty_result",
                "OpenCLI twitter command returned no Markdown text.",
            )

        metadata: dict[str, Any] = {"command": command_plan.command_name}
        metadata.update(command_plan.metadata)
        table_title, table_author = _table_post_title(text, tweet_id=metadata.get("tweet_id"))
        fallback = f"X-{metadata['tweet_id']}" if metadata.get("tweet_id") else f"X-@{_x_username_candidate(item) or 'posts'}"
        notes = [
            f"OpenCLI twitter {command_plan.command_name} returned read-only X text.",
            "Provider is registered only when --enable-browser-backends is enabled.",
            "No follow/unfollow/post/list/account mutation was attempted.",
            "No media download, raw media persistence, cookie export, proxy, twscrape, or ASR action was attempted.",
        ]
        if stderr.strip():
            notes.append("OpenCLI returned a non-fatal diagnostic after usable text was received: " + sanitize_error_summary(stderr))
        return TranscriptResult(
            text=text.rstrip() + "\n",
            status="verified",
            source_level="native_text",
            backend=command_plan.backend,
            title=_clean_x_title(_markdown_title(text), text) or _clean_x_title(item.title, text) or _clean_x_title(table_title, text) or fallback,
            author=table_author or item.author,
            source_notes=notes,
            metadata=metadata,
        )


class _CommandPlan:
    def __init__(
        self,
        *,
        backend: str,
        command_name: str,
        args: list[str],
        metadata: dict[str, Any] | None = None,
        error_code: str | None = None,
        error_summary: str | None = None,
    ) -> None:
        self.backend = backend
        self.command_name = command_name
        self.args = args
        self.metadata = metadata or {}
        self.error_code = error_code
        self.error_summary = error_summary


def _command_plan_for(item: ContentItem, requested_backend: str) -> _CommandPlan:
    if requested_backend == OPENCLI_TWITTER_ARTICLE_BACKEND:
        tweet_ref = _tweet_ref_for_article(item)
        if not tweet_ref:
            return _error_plan("invalid_input", "X article tweet ID or URL not found.")
        tweet_id = _extract_x_post_id(tweet_ref)
        metadata: dict[str, Any] = {}
        if tweet_id:
            metadata["tweet_id"] = tweet_id
        return _CommandPlan(
            backend=OPENCLI_TWITTER_ARTICLE_BACKEND,
            command_name="article",
            args=build_opencli_twitter_article_args(tweet_id or tweet_ref),
            metadata=metadata,
        )

    if requested_backend == OPENCLI_TWITTER_TWEETS_BACKEND:
        username = _x_username_candidate(item)
        if not username:
            return _error_plan("invalid_input", "X username not found for tweets extraction.")
        limit = _item_limit(item, default=DEFAULT_TWEETS_LIMIT)
        return _CommandPlan(
            backend=OPENCLI_TWITTER_TWEETS_BACKEND,
            command_name="tweets",
            args=build_opencli_twitter_tweets_args(username, limit=limit),
            metadata={"username": username, "limit": _bounded_limit(limit, default=DEFAULT_TWEETS_LIMIT)},
        )

    tweet_id = _tweet_id_candidate(item)
    if tweet_id:
        limit = _item_limit(item, default=DEFAULT_THREAD_LIMIT)
        return _CommandPlan(
            backend=OPENCLI_TWITTER_THREAD_BACKEND,
            command_name="thread",
            args=build_opencli_twitter_thread_args(tweet_id, limit=limit),
            metadata={"tweet_id": tweet_id, "limit": _bounded_limit(limit, default=DEFAULT_THREAD_LIMIT)},
        )

    username = _x_username_candidate(item)
    if username:
        limit = _item_limit(item, default=DEFAULT_TWEETS_LIMIT)
        return _CommandPlan(
            backend=OPENCLI_TWITTER_TWEETS_BACKEND,
            command_name="tweets",
            args=build_opencli_twitter_tweets_args(username, limit=limit),
            metadata={"username": username, "limit": _bounded_limit(limit, default=DEFAULT_TWEETS_LIMIT)},
        )

    return _error_plan("invalid_input", "X post ID, article URL, or username not found.")


def _error_plan(code: str, summary: str) -> _CommandPlan:
    return _CommandPlan(
        backend=OPENCLI_TWITTER_THREAD_BACKEND,
        command_name="none",
        args=[],
        error_code=code,
        error_summary=summary,
    )


def _tweet_id_candidate(item: ContentItem) -> str | None:
    for value in (item.id, item.url):
        if not value:
            continue
        if value.isdigit():
            return value
        tweet_id = _extract_x_post_id(value)
        if tweet_id:
            return tweet_id
    return None


def _tweet_ref_for_article(item: ContentItem) -> str | None:
    tweet_id = _tweet_id_candidate(item)
    if tweet_id:
        return tweet_id
    parsed = urlparse(item.url)
    if parsed.scheme and parsed.netloc and _is_x_host(parsed.netloc):
        return sanitize_url(item.url)
    return None


def _extract_x_post_id(value: str) -> str | None:
    parsed = urlparse(value)
    match = X_STATUS_RE.search(parsed.path)
    return match.group(1) if match else None


def _x_username_candidate(item: ContentItem) -> str | None:
    for value in (item.id, item.author, item.url):
        username = _extract_x_username(str(value or ""))
        if username:
            return username
    return None


def _extract_x_username(value: str) -> str | None:
    parsed = urlparse(value)
    if parsed.scheme and parsed.netloc:
        if not _is_x_host(parsed.netloc):
            return None
        parts = [part for part in parsed.path.split("/") if part]
        if not parts:
            return None
        candidate = parts[0].removeprefix("@")
    else:
        candidate = value.strip().lstrip("@").split("/")[0]

    if candidate.lower() in RESERVED_X_PATHS:
        return None
    match = USERNAME_RE.fullmatch(candidate)
    return match.group(1) if match else None


def _is_x_host(host: str) -> bool:
    normalized = host.lower().split(":")[0]
    return normalized == "x.com" or normalized.endswith(".x.com") or normalized == "twitter.com" or normalized.endswith(".twitter.com")


def _item_limit(item: ContentItem, *, default: int) -> int:
    value = item.raw_meta.get("limit", default)
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _bounded_limit(value: int, *, default: int) -> int:
    if value <= 0:
        return default
    return min(value, MAX_X_TEXT_LIMIT)


def _browser_options() -> list[str]:
    return [
        "--window",
        "background",
        "--site-session",
        "ephemeral",
        "--keep-tab",
        "false",
    ]


def _markdown_title(markdown: str) -> str | None:
    for line in markdown.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped.removeprefix("# ").strip() or None
    return None


def _clean_x_title(candidate: str | None, markdown: str) -> str | None:
    value = " ".join(str(candidate or "").split())
    if not value:
        return None
    value = re.sub(r"\s+(?:https?://|http\s+t\.co)\S*.*$", "", value, flags=re.IGNORECASE).strip()
    if re.search(r"(?:作者\s*[:：]|正文前\s*\d+\s*字|前\s*\d+\s*字|正文摘要)", value, re.IGNORECASE):
        value = ""
    if value:
        return value[:120]
    for line in markdown.splitlines():
        value = re.sub(r"<br\s*/?>|\\n", " ", line, flags=re.IGNORECASE).strip(" #|>-*")
        if value and not re.search(r"\b(?:id|author|text|url)\b|(?:作者\s*[:：]|正文前\s*\d+\s*字|前\s*\d+\s*字|正文摘要)", value, re.IGNORECASE):
            return value[:120]
    return None


def _table_post_title(markdown: str, *, tweet_id: str | None) -> tuple[str | None, str | None]:
    """Read metadata from the Markdown table without changing its raw body."""
    headers: list[str] = []
    for line in markdown.splitlines():
        if not line.lstrip().startswith("|"):
            continue
        cells = [cell.strip().replace(r"\|", "|") for cell in re.split(r"(?<!\\)\|", line.strip().strip("|"))]
        if "author" in cells and "text" in cells:
            headers = cells
            continue
        if not headers or len(cells) < len(headers):
            continue
        row = dict(zip(headers, cells))
        if tweet_id and row.get("id") != tweet_id:
            continue
        body = re.sub(r"<br\s*/?>|\\n", " ", row["text"], flags=re.IGNORECASE)
        body = " ".join(body.split())
        if not body or re.fullmatch(r"[-: ]+", body):
            continue
        author = row["author"] or None
        opening = body[:120] + ("…" if len(body) > 120 else "")
        opening = re.sub(r"\s+(?:https?://|http\s+t\.co)\S*.*$", "", opening, flags=re.IGNORECASE).strip()
        return opening, author
    return None, None


def _failed(backend: str, code: str, error: object) -> TranscriptResult:
    summary = sanitize_error_summary(error)
    next_action = _twitter_next_action(code)
    if next_action and next_action not in summary:
        summary = f"{summary} Next step: {next_action}"
    return TranscriptResult(
        text="",
        status="failed",
        source_level="partial_extraction",
        backend=backend,
        source_notes=[
            "OpenCLI twitter provider returned structured failure.",
            "No follow/unfollow/post/list/account mutation, media download, proxy, twscrape, or ASR action was attempted.",
        ],
        error_code=code,
        error_summary=summary,
        metadata={"next_action": next_action} if next_action else {},
    )


def _twitter_next_action(code: str) -> str | None:
    if code in {"x_not_found", "x_source_failed"}:
        return "Check that the public post URL is correct and still available."
    if code in {"x_access_denied", "x_auth_required"}:
        return "Only public posts are supported; open the URL in a browser to check whether X grants access. Openmd will not bypass private-post or account permissions."
    if code == "x_rate_limited":
        return "Wait briefly, then retry once."
    if code in {"x_network_failed", "x_empty_result", "x_truncated", "opencli_twitter_timeout", "opencli_twitter_failed"}:
        return "Retry with network access; if public APIs remain unavailable, enable the browser backend and open the public post in X."
    return None


class XPublicLayeredProvider:
    """Anonymous public X extraction: rich FxEmbed v2, then lighter fallbacks."""

    def __init__(self, *, fetch_json=None, opencli_runner: OpenCliRunner | None = None, timeout: float = 7.0) -> None:
        self.name = FXTWITTER_BACKEND
        self._fetch_json = fetch_json or _fetch_x_json
        self._opencli_runner = opencli_runner
        self.timeout = timeout

    def descriptor(self) -> BackendDescriptor:
        return BackendDescriptor(
            name=self.name,
            layer="L1-L3 anonymous public X APIs",
            capabilities=(BackendCapability(platforms=("x",), source_types=("article", "mixed"), input_types=("single",), source_levels=("native_text", "third_party_extractor", "partial_extraction"), requires_network=True, requires_credentials=False),),
            description="Extract public X post text using anonymous public endpoints with bounded fallback.",
            provider="openmd.x.public",
        )

    def health(self) -> BackendHealth:
        return BackendHealth(ok=True, dependencies={"network": "available"})

    def extract(self, request: BackendExtractRequest) -> TranscriptResult:
        item = request.item
        tweet_id = _tweet_id_candidate(item)
        username = _x_username_candidate(item) or "i"
        if item.platform != "x" or not tweet_id:
            return _failed(self.name, "invalid_input", "A public X status URL is required.")
        attempts: list[str] = []
        urls = []
        if os.environ.get("OPENMD_X_DISABLE_FXEMBED") != "1":
            urls.extend([
                ("fxembed_v2", f"https://api.fxtwitter.com/2/conversation/{tweet_id}"),
                ("fxembed_v1", f"https://api.fxtwitter.com/{urllib.parse.quote(username)}/status/{tweet_id}"),
            ])
        urls.append(("syndication", _syndication_url(tweet_id)))
        for layer, url in urls:
            try:
                status, payload = self._fetch_json(url, timeout=self.timeout, layer=layer)
            except Exception as exc:
                status, payload = 0, {"error": _safe_exception_category(exc)}
            code = _x_http_error(status, payload)
            if code:
                attempts.append(f"{layer}:{code}")
                continue
            parsed = _parse_x_payload(payload, tweet_id=tweet_id, source=layer)
            if parsed is None:
                attempts.append(f"{layer}:truncated_or_empty")
                continue
            return _x_result(item, tweet_id, parsed, layer, attempts)
        failed = _failed(self.name, attempts[-1].split(":", 1)[-1] if attempts else "x_network_failed", "; ".join(attempts))
        return TranscriptResult(
            text=failed.text,
            status=failed.status,
            source_level=failed.source_level,
            backend=failed.backend,
            source_notes=[*failed.source_notes, "If the browser fallback is enabled, OpenCLI may try the current public page; private posts and account permissions remain inaccessible."],
            error_code=failed.error_code,
            error_summary=failed.error_summary,
            metadata={"x_public_attempts": attempts},
        )


def _fetch_x_json(url: str, *, timeout: float, layer: str) -> tuple[int, object]:
    headers = {"User-Agent": "Openmd/0.1 (+https://github.com/openmd)"}
    req = urllib.request.Request(url, headers=headers)
    for attempt in range(2):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, {}
        except (OSError, TimeoutError):
            if attempt:
                raise
    return 0, {}


def _syndication_url(tweet_id: str) -> str:
    token = _base36_float(__import__("math").pi * (int(tweet_id) / 1_000_000))
    return f"https://cdn.syndication.twimg.com/tweet-result?id={tweet_id}&lang=en&token={urllib.parse.quote(token)}"


def _base36_float(value: float) -> str:
    whole = int(value)
    fraction = value - whole
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    result = ""
    while whole:
        whole, remainder = divmod(whole, 36)
        result = digits[remainder] + result
    result = result or "0"
    for _ in range(12):
        fraction *= 36
        digit = int(fraction)
        result += digits[digit]
        fraction -= digit
        if fraction < 1e-10:
            break
    return result


def _parse_x_payload(payload: object, *, tweet_id: str, source: str) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    tweet = payload.get("status") if isinstance(payload.get("status"), dict) else payload.get("tweet") if isinstance(payload.get("tweet"), dict) else payload
    legacy = tweet.get("legacy") if isinstance(tweet.get("legacy"), dict) else tweet
    note = tweet.get("note_tweet") or legacy.get("note_tweet")
    if note and not (isinstance(note, dict) and isinstance(note.get("text"), str) and note.get("text")):
        return None
    text = ""
    article = tweet.get("article") if isinstance(tweet.get("article"), dict) else {}
    article_content = article.get("content") if isinstance(article.get("content"), dict) else {}
    article_blocks = article_content.get("blocks") if isinstance(article_content.get("blocks"), list) else []
    article_text = _render_article_blocks({**article_content, "_article": article})
    article_text_field = article.get("plain_text") or article.get("text")
    candidates = (article_text_field,) if article else (note.get("text") if isinstance(note, dict) else None, tweet.get("text"), legacy.get("full_text"), legacy.get("text"))
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            text = candidate.strip()
            break
    if article_text:
        text = article_text
    elif article and not text:
        # A title and preview are not the full article body.
        return None
    if not text or legacy.get("truncated") is True or tweet.get("truncated") is True:
        return None
    if source == "syndication" and len(text) >= 180 and not (article or isinstance(note, dict)):
        return None
    user = tweet.get("user") if isinstance(tweet.get("user"), dict) else legacy.get("user") if isinstance(legacy.get("user"), dict) else tweet.get("author") if isinstance(tweet.get("author"), dict) else {}
    author = user.get("name") or user.get("screen_name") or user.get("username")
    author_handle = _x_handle(user)
    created = tweet.get("created_at") or legacy.get("created_at")
    title = article.get("title") or (text.splitlines()[0][:120])
    media = tweet.get("media") if isinstance(tweet.get("media"), dict) else legacy.get("extended_entities") if isinstance(legacy.get("extended_entities"), dict) else {}
    photos = media.get("photos") or tweet.get("mediaDetails") or media.get("media", [])
    images = [str(p.get("url") or p.get("media_url_https") or "") for p in photos if isinstance(p, dict)]
    article_media = article.get("media_entities") if isinstance(article.get("media_entities"), list) else []
    cover = article.get("cover_media") if isinstance(article.get("cover_media"), dict) else {}
    cover_info = cover.get("media_info") if isinstance(cover.get("media_info"), dict) else {}
    if cover_info.get("original_img_url"):
        images.append(str(cover_info["original_img_url"]))
    for entity in article_media:
        info = entity.get("media_info") if isinstance(entity, dict) else None
        if isinstance(info, dict):
            url = info.get("original_img_url") or info.get("media_url_https")
            if url:
                images.append(str(url))
    quote = tweet.get("quote") or tweet.get("quoted_tweet") or legacy.get("quoted_status")
    quote_record = _x_post_record(quote) if isinstance(quote, dict) else None
    if isinstance(quote, dict):
        quoted = quote_record.get("text") if quote_record else None
        if quoted:
            text += "\n\n> 引用推文：\n> " + str(quoted).replace("\n", "\n> ")
    raw_thread = payload.get("thread") if isinstance(payload, dict) else None
    if not isinstance(raw_thread, list):
        raw_thread = tweet.get("thread") if isinstance(tweet.get("thread"), list) else []
    thread = [_x_post_record(row) for row in raw_thread if isinstance(row, dict)]
    thread = [row for row in thread if row]
    if not thread:
        root_record = _x_post_record(tweet, fallback_url=f"https://x.com/{author_handle.lstrip('@') if author_handle else 'i'}/status/{tweet_id}")
        thread = [root_record] if root_record else []
    thread_handles = [row.get("author_handle") for row in thread]
    thread_coverage = "author_thread" if len(thread) > 1 and author_handle and all(handle == author_handle for handle in thread_handles) else "partial"
    stats = tweet.get("stats") if isinstance(tweet.get("stats"), dict) else {}
    counts = {
        key: _first_count(tweet.get(source_key), legacy.get(source_key), stats.get(source_key))
        for key, source_key in {
            "views": "views",
            "likes": "likes",
            "reposts": "reposts",
            "replies": "replies",
            "bookmarks": "bookmarks",
        }.items()
    }
    if counts["reposts"] is None:
        counts["reposts"] = _first_count(tweet.get("retweets"), legacy.get("retweets"), stats.get("retweets"))
    return {
        "text": text,
        "title": str(title),
        "author": str(author or "") or None,
        "author_handle": author_handle,
        "published_at": _normalize_x_timestamp(created),
        "images": images,
        "article": bool(article),
        "tweet": tweet,
        "quote": quote_record,
        "thread": thread,
        "thread_count": len(thread),
        "thread_coverage": thread_coverage,
        **counts,
    }


def _first_count(*values: object) -> int | float | None:
    for value in values:
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            return value
    return None


def _x_handle(user: object) -> str | None:
    if not isinstance(user, dict):
        return None
    value = user.get("screen_name") or user.get("username") or user.get("handle")
    if not isinstance(value, str) or not value.strip():
        return None
    return "@" + value.strip().lstrip("@")


def _normalize_x_timestamp(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(text)
        except (TypeError, ValueError, IndexError, OverflowError):
            return text
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _x_post_record(value: object, *, fallback_url: str | None = None) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    legacy = value.get("legacy") if isinstance(value.get("legacy"), dict) else {}
    user = value.get("author") if isinstance(value.get("author"), dict) else value.get("user") if isinstance(value.get("user"), dict) else legacy.get("user")
    user = user if isinstance(user, dict) else {}
    text_value = value.get("text") or value.get("full_text") or legacy.get("full_text") or legacy.get("text")
    if isinstance(value.get("raw_text"), dict):
        text_value = text_value or value["raw_text"].get("text")
    text = str(text_value).strip() if isinstance(text_value, str) else ""
    handle = _x_handle(user)
    author = user.get("name") or user.get("screen_name") or user.get("username")
    post_id = value.get("id") or value.get("id_str") or legacy.get("id_str")
    url = value.get("url") or value.get("tweet_url")
    if not isinstance(url, str) or not url.strip():
        if post_id and handle:
            url = f"https://x.com/{handle.lstrip('@')}/status/{post_id}"
        else:
            url = fallback_url
    record = {
        "url": str(url) if isinstance(url, str) and url else None,
        "author": str(author) if author else None,
        "author_handle": handle,
        "published_at": _normalize_x_timestamp(value.get("created_at") or value.get("published_at") or legacy.get("created_at")),
        "text": text or None,
    }
    return {key: item for key, item in record.items() if item not in (None, "")}


def _render_article_blocks(content: dict[str, Any]) -> str:
    blocks = content.get("blocks") if isinstance(content.get("blocks"), list) else []
    entity_map = content.get("entityMap") or {}
    if isinstance(entity_map, list):
        entity_map = {str(item["key"]): item.get("value") for item in entity_map if isinstance(item, dict) and item.get("key") is not None}
    if not isinstance(entity_map, dict):
        entity_map = {}
    rendered: list[str] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        text = block.get("text")
        kind = str(block.get("type") or "").lower()
        if kind == "atomic":
            for entity_range in block.get("entityRanges", []):
                key = str(entity_range.get("key")) if isinstance(entity_range, dict) else ""
                entity = entity_map.get(key)
                if not isinstance(entity, dict):
                    continue
                entity_type = str(entity.get("type") or "").upper()
                data = entity.get("data") if isinstance(entity.get("data"), dict) else {}
                if entity_type == "MARKDOWN":
                    markdown = data.get("markdown")
                    if isinstance(markdown, str) and markdown.strip():
                        match = re.search(r"```(?:[a-zA-Z0-9_-]*)?\n(.*?)```", markdown, re.DOTALL)
                        rendered.append(match.group(1).strip("\n") if match else markdown.strip())
                elif entity_type == "MEDIA":
                    for media_item in data.get("mediaItems", []):
                        if not isinstance(media_item, dict):
                            continue
                        media_id = str(media_item.get("mediaId") or "")
                        url = _article_media_url(media_id, content)
                        if url:
                            rendered.append(f"![]({url})")
            continue
        if not isinstance(text, str) or not text.strip():
            continue
        text = _article_inline_links(text, block, entity_map)
        prefix = {"header-one": "# ", "header-two": "## ", "header-three": "### ", "blockquote": "> ", "unordered-list-item": "- ", "ordered-list-item": "1. "}.get(kind, "")
        rendered.append(prefix + text.strip())
    return "\n\n".join(rendered)


def _article_media_url(media_id: str, content: dict[str, Any]) -> str | None:
    article = content.get("_article") if isinstance(content.get("_article"), dict) else {}
    media_rows = list(article.get("media_entities", []))
    cover = article.get("cover_media")
    if isinstance(cover, dict):
        media_rows.append(cover)
    for row in media_rows:
        if not isinstance(row, dict) or str(row.get("media_id") or "") != media_id:
            continue
        info = row.get("media_info") if isinstance(row.get("media_info"), dict) else {}
        url = info.get("original_img_url")
        if isinstance(url, str) and url.startswith("https://"):
            return url
    return None


def _article_inline_links(text: str, block: dict[str, Any], entity_map: dict[str, Any]) -> str:
    ranges = block.get("entityRanges") if isinstance(block.get("entityRanges"), list) else []
    for entity_range in sorted((r for r in ranges if isinstance(r, dict)), key=lambda r: int(r.get("offset") or 0), reverse=True):
        entity = entity_map.get(str(entity_range.get("key")))
        if not isinstance(entity, dict) or str(entity.get("type") or "").upper() != "LINK":
            continue
        data = entity.get("data") if isinstance(entity.get("data"), dict) else {}
        url = data.get("url")
        offset, length = int(entity_range.get("offset") or 0), int(entity_range.get("length") or 0)
        if isinstance(url, str) and url.startswith(("https://", "http://")) and length and offset + length <= len(text):
            label = text[offset:offset + length]
            text = text[:offset] + f"[{label}]({url})" + text[offset + length:]
    return text


def _x_result(item: ContentItem, tweet_id: str, data: dict[str, Any], source: str, attempts: list[str]) -> TranscriptResult:
    canonical = f"https://x.com/{_x_username_candidate(item) or 'i'}/status/{tweet_id}"
    body = data["text"]
    thread = data.get("thread") if isinstance(data.get("thread"), list) else []
    if len(thread) > 1:
        sections = []
        for post in thread[1:]:
            if not isinstance(post, dict) or not post.get("text"):
                continue
            label = post.get("author_handle") or post.get("author") or "帖文"
            timestamp = f" · {post['published_at']}" if post.get("published_at") else ""
            sections.append(f"### {label}{timestamp}\n\n{post['text']}")
        if sections:
            body += "\n\n## Thread\n\n" + "\n\n".join(sections)
    appendix = [f"\n\n原文链接：{canonical}"]
    appendix.extend(f"\n图片链接：{url}" for url in data["images"])
    text = body + "".join(appendix) + "\n"
    title = _clean_x_title(str(data["title"] or body.splitlines()[0][:120]), body) or body.splitlines()[0][:120].strip()
    metadata = {
        "tweet_id": tweet_id,
        "api_layer": source,
        "fallback_attempts": list(attempts),
        "image_urls": data["images"],
        "quote_present": bool(data["quote"]),
        "thread": data["thread"],
        "thread_count": data["thread_count"],
        "thread_coverage": data["thread_coverage"],
        "completeness": "full",
    }
    for key in ("author_handle", "views", "likes", "reposts", "replies", "bookmarks", "quote"):
        if data.get(key) is not None:
            metadata[key] = data[key]
    return TranscriptResult(text=text, status="verified", source_level="native_text" if source == "syndication" else "third_party_extractor", backend=FXTWITTER_BACKEND, title=title, author=data["author"], published_at=data["published_at"], metadata=metadata, source_notes=["Only public X content is requested; private posts and account permissions are not bypassed.", f"Public extraction layer: {source}."])


def _x_http_error(status: int, payload: object) -> str | None:
    if isinstance(payload, dict):
        message = str(payload.get("message") or payload.get("error") or "").lower()
        if "protected" in message or "private" in message or "not authorized" in message:
            return "x_access_denied"
        provider_status = payload.get("code")
        if isinstance(provider_status, int) and provider_status >= 400:
            status = provider_status
    if status == 404:
        return "x_not_found"
    if status in {401, 403}:
        return "x_access_denied"
    if status == 429:
        return "x_rate_limited"
    if status >= 500 or status == 0:
        return "x_network_failed"
    if status >= 400:
        return "x_source_failed"
    return None


def _safe_exception_category(exc: Exception) -> str:
    return "network_timeout" if isinstance(exc, (TimeoutError,)) or "timed out" in str(exc).lower() else "network_error"


def _prefer_original_error(stderr: str, stdout: str) -> str:
    combined = "\n".join(part for part in (stderr, stdout) if part)
    lines = [line for line in combined.splitlines() if "cleanup failed" not in line.lower() and "cannot close a tab" not in line.lower()]
    return "\n".join(lines).strip() or combined.strip()


def _is_cleanup_only_warning(stderr: str) -> bool:
    lines = [line.strip().lower() for line in stderr.splitlines() if line.strip()]
    return bool(lines) and all(
        line.startswith(("cleanup failed", "owned-tab cleanup", "owned tab cleanup", "cannot close a tab"))
        for line in lines
    )
