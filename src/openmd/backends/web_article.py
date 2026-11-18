"""Generic web article extraction providers.

The primary provider wraps ``trafilatura`` as an optional local library. It is
registered only after ``--enable-network-backends`` because it fetches remote
pages. Browser/session fallbacks such as OpenCLI stay behind
``--enable-browser-backends``.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable
from typing import Any

from openmd.backends.base import (
    BackendCapability,
    BackendDescriptor,
    BackendExtractRequest,
    BackendHealth,
)
from openmd.backends.executor import BackendProviderRegistry
from openmd.backends.registry import BackendRuntimeOptions
from openmd.core.models import TranscriptResult
from openmd.core.secrets import sanitize_error_summary
from openmd.runtime.interpreters import run_interruptibly


WEB_ARTICLE_TRAFILATURA_BACKEND = "web_article.trafilatura"
OPENCLI_WEB_READ_BACKEND = "opencli.web.read"
CRAWL4AI_WEB_ARTICLE_BACKEND = "crawl4ai.web_article"
DEFAULT_MIN_ARTICLE_CHARS = 40

CommandRunner = Callable[[list[str], int], tuple[int, str, str]]
TrafilaturaFetcher = Callable[[str], object]
TrafilaturaExtractor = Callable[..., str | None]
TrafilaturaMetadataExtractor = Callable[[object], object]


def register_backend_providers(
    registry: BackendProviderRegistry,
    options: BackendRuntimeOptions,
) -> None:
    """Register web article providers behind explicit runtime gates."""
    requested = options.backend_override
    from openmd.backends.discourse import DISCOURSE_BACKEND, DiscourseProvider

    if (options.enable_network_backends or options.enable_browser_backends) and requested in {None, DISCOURSE_BACKEND}:
        registry.register(DISCOURSE_BACKEND, DiscourseProvider(options))
    if options.enable_network_backends and requested in {None, WEB_ARTICLE_TRAFILATURA_BACKEND}:
        registry.register(WEB_ARTICLE_TRAFILATURA_BACKEND, WebArticleTrafilaturaProvider())

    if options.enable_browser_backends and requested in {None, OPENCLI_WEB_READ_BACKEND}:
        registry.register(OPENCLI_WEB_READ_BACKEND, OpenCliWebReadProvider())


class WebArticleTrafilaturaProvider:
    """Extract generic static articles through the optional trafilatura API."""

    name = WEB_ARTICLE_TRAFILATURA_BACKEND

    def __init__(
        self,
        *,
        fetcher: TrafilaturaFetcher | None = None,
        extractor: TrafilaturaExtractor | None = None,
        metadata_extractor: TrafilaturaMetadataExtractor | None = None,
        min_article_chars: int = DEFAULT_MIN_ARTICLE_CHARS,
    ) -> None:
        self._fetcher = fetcher
        self._extractor = extractor
        self._metadata_extractor = metadata_extractor
        self.min_article_chars = max(0, min_article_chars)

    def descriptor(self) -> BackendDescriptor:
        capability = BackendCapability(
            platforms=("web_article",),
            source_types=("article",),
            input_types=("single", "batch"),
            source_levels=("native_text", "partial_extraction"),
            requires_network=True,
            requires_credentials=False,
            optional_config=("trafilatura",),
        )
        return BackendDescriptor(
            name=self.name,
            layer="L1 specialized extractor",
            capabilities=(capability,),
            description="Fetch and extract static web articles with optional local trafilatura.",
            provider="openmd.backends.web_article",
        )

    def health(self) -> BackendHealth:
        if self._fetcher is not None and self._extractor is not None:
            return BackendHealth(ok=True, dependencies={"trafilatura": "injected"})
        try:
            import trafilatura  # type: ignore[import-not-found]  # noqa: F401
        except ModuleNotFoundError:
            return BackendHealth(
                ok=False,
                dependencies={"trafilatura": "missing"},
                error_code="backend_dependency_missing",
                error_summary="Install trafilatura or use a browser fallback backend.",
            )
        return BackendHealth(ok=True, dependencies={"trafilatura": "available"})

    def extract(self, request: BackendExtractRequest) -> TranscriptResult:
        item = request.item
        if item.platform != "web_article":
            return _failed(
                self.name,
                "unsupported_platform",
                f"trafilatura web article provider only supports web_article, got {item.platform}.",
            )

        try:
            fetcher, extractor, metadata_extractor = self._resolve_api()
        except ModuleNotFoundError:
            return _failed(
                self.name,
                "backend_dependency_missing",
                "trafilatura is not installed; install it or enable browser fallback.",
            )

        try:
            downloaded = fetcher(item.url)
        except Exception as exc:
            downloaded, fetch_error = None, exc
        else:
            fetch_error = None
        if downloaded is None and self._fetcher is None:
            # trafilatura's urllib3 client ignores system/SOCKS proxy settings;
            # stdlib urllib honors HTTP(S)_PROXY and the Windows proxy.
            try:
                downloaded = _urllib_fetch(item.url)
            except Exception as exc:  # network errors are reported below
                fetch_error = fetch_error or exc
        if downloaded is None:
            if fetch_error is not None:
                return _failed(self.name, "web_article_fetch_failed", fetch_error)
            return _failed(self.name, "web_article_fetch_empty", "trafilatura.fetch_url returned no document.")

        try:
            markdown = extractor(
                downloaded,
                output_format="markdown",
                # Metadata is read separately below; True would embed a YAML
                # block inside the body, duplicating the rendered front matter.
                with_metadata=False,
                include_images=True,
                include_links=True,
                url=item.url,
            )
        except Exception as exc:
            return _failed(self.name, "web_article_extract_failed", exc)

        text = (markdown or "").strip()
        if len("".join(text.split())) < self.min_article_chars:
            return _failed(
                self.name,
                "web_article_empty_content",
                "trafilatura returned no usable article body.",
            )

        metadata = _metadata_dict(metadata_extractor, downloaded)
        return TranscriptResult(
            text=text.rstrip() + "\n",
            status="verified",
            source_level="native_text",
            backend=self.name,
            title=_metadata_value(metadata, "title"),
            author=_metadata_value(metadata, "author"),
            published_at=_metadata_value(metadata, "date", "published", "published_at"),
            source_notes=[
                "trafilatura extracted article Markdown locally from fetched HTML.",
                "include_images=True and include_links=True were requested.",
                "No browser, cloud API, media download, or ASR action was attempted.",
            ],
            metadata={"strategy": "trafilatura_api"},
        )

    def _resolve_api(
        self,
    ) -> tuple[TrafilaturaFetcher, TrafilaturaExtractor, TrafilaturaMetadataExtractor | None]:
        if self._fetcher is not None and self._extractor is not None:
            return self._fetcher, self._extractor, self._metadata_extractor

        from trafilatura import extract, extract_metadata, fetch_url  # type: ignore[import-not-found]

        return fetch_url, extract, extract_metadata


class OpenCliWebReadProvider:
    """Browser/session fallback for generic web pages through OpenCLI."""

    name = OPENCLI_WEB_READ_BACKEND

    def __init__(self, *, runner: CommandRunner | None = None, timeout: int = 180) -> None:
        self._runner = runner or _run_command
        self.timeout = timeout

    def descriptor(self) -> BackendDescriptor:
        capability = BackendCapability(
            platforms=("web_article", "weixin"),
            source_types=("article",),
            input_types=("single", "batch"),
            source_levels=("third_party_extractor", "partial_extraction"),
            requires_network=True,
        )
        return BackendDescriptor(
            name=self.name,
            layer="L3 browser/session fallback",
            capabilities=(capability,),
            description="Use OpenCLI browser extraction and return Markdown from stdout.",
            provider="opencli.web",
            is_fallback=True,
        )

    def health(self) -> BackendHealth:
        if self._runner is not _run_command:
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
        command = [
            "opencli",
            "web",
            "read",
            "--url",
            item.url,
            "--stdout",
            "true",
            "--download-images",
            "false",
            "--window",
            "background",
            "--site-session", "ephemeral", "--keep-tab",
            "false",
            "-f",
            "md",
        ]
        exit_code, stdout, stderr = self._runner(command, self.timeout)
        if exit_code != 0:
            return _failed(self.name, f"backend_exit_{exit_code}", stderr or stdout)

        text = stdout.strip()
        if not text:
            return _failed(self.name, "empty_result", "OpenCLI web read returned no Markdown text.")

        return TranscriptResult(
            text=text.rstrip() + "\n",
            status="verified",
            source_level="third_party_extractor",
            backend=self.name,
            title=_markdown_title(text) or item.title,
            source_notes=[
                "OpenCLI web read returned Markdown from browser/session fallback.",
                "--download-images false was used; Openmd owns optional asset localization.",
                "No raw stdout/stderr artifact was persisted by the provider.",
            ],
        )


def _urllib_fetch(url: str, timeout: int = 30) -> str | bytes | None:
    import urllib.request

    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; Openmd)", "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - http(s) only, checked by input validation
        body = response.read(20 * 1024 * 1024)
        charset = response.headers.get_content_charset()
    if not body:
        return None
    # Without a declared charset, hand bytes to trafilatura (it sniffs <meta charset>, e.g. GBK).
    return body.decode(charset, errors="replace") if charset else body


def _run_command(command: list[str], timeout: int) -> tuple[int, str, str]:
    resolved = shutil.which(command[0]) or command[0]
    try:
        completed = run_interruptibly(
            [resolved, *command[1:]],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError as exc:
        return 127, "", sanitize_error_summary(exc) or "command not found"
    except subprocess.TimeoutExpired as exc:
        return (
            124,
            exc.stdout if isinstance(exc.stdout, str) else "",
            f"command timed out after {timeout}s",
        )
    return completed.returncode, completed.stdout or "", completed.stderr or ""


def _metadata_dict(
    metadata_extractor: TrafilaturaMetadataExtractor | None,
    downloaded: object,
) -> dict[str, Any]:
    if metadata_extractor is None:
        return {}
    try:
        metadata = metadata_extractor(downloaded)
    except Exception:
        return {}
    if metadata is None:
        return {}
    if isinstance(metadata, dict):
        return dict(metadata)
    as_dict = getattr(metadata, "as_dict", None)
    if callable(as_dict):
        try:
            value = as_dict()
        except Exception:
            value = None
        if isinstance(value, dict):
            return value
    result: dict[str, Any] = {}
    for key in ("title", "author", "date", "published", "published_at"):
        value = getattr(metadata, key, None)
        if value:
            result[key] = value
    return result


def _metadata_value(metadata: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = metadata.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


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
        source_notes=["web article provider returned structured failure"],
        error_code=code,
        error_summary=sanitize_error_summary(error),
    )
