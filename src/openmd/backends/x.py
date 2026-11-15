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
from collections.abc import Callable
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
    """Register X text providers only after explicit browser-backend opt-in."""

    if not options.enable_browser_backends:
        return

    requested = options.backend_override
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
        if exit_code != 0:
            signal = stderr or stdout or f"OpenCLI exited with code {exit_code}."
            return _failed(command_plan.backend, classify_twitter_failure(signal), signal)

        text = stdout.strip()
        if not text:
            return _failed(
                command_plan.backend,
                "x_empty_result",
                "OpenCLI twitter command returned no Markdown text.",
            )

        metadata: dict[str, Any] = {"command": command_plan.command_name}
        metadata.update(command_plan.metadata)
        return TranscriptResult(
            text=text.rstrip() + "\n",
            status="verified",
            source_level="native_text",
            backend=command_plan.backend,
            title=_markdown_title(text) or item.title,
            source_notes=[
                f"OpenCLI twitter {command_plan.command_name} returned read-only X text.",
                "Provider is registered only when --enable-browser-backends is enabled.",
                "No follow/unfollow/post/list/account mutation was attempted.",
                "No media download, raw media persistence, cookie export, proxy, twscrape, or ASR action was attempted.",
            ],
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
        "persistent",
        "--keep-tab",
        "false",
    ]


def _markdown_title(markdown: str) -> str | None:
    for line in markdown.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped.removeprefix("# ").strip() or None
    return None


def _failed(backend: str, code: str, error: object) -> TranscriptResult:
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
        error_summary=sanitize_error_summary(error),
    )
