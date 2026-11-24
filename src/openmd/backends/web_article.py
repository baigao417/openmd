"""Generic web article extraction providers.

The primary provider wraps ``trafilatura`` as an optional local library. It is
registered only after ``--enable-network-backends`` because it fetches remote
pages. Browser/session fallbacks such as OpenCLI stay behind
``--enable-browser-backends``.
"""

from __future__ import annotations

import shutil
import subprocess
import re
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from bs4 import BeautifulSoup

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
from openmd.core.input_validation import normalize_http_url
from openmd.runtime.interpreters import run_interruptibly


WEB_ARTICLE_TRAFILATURA_BACKEND = "web_article.trafilatura"
OPENCLI_WEB_READ_BACKEND = "opencli.web.read"
CRAWL4AI_WEB_ARTICLE_BACKEND = "crawl4ai.web_article"
DEFAULT_MIN_ARTICLE_CHARS = 40
DEFAULT_MIN_COVERAGE_RATIO = 0.20
# RFC 9110 calibration: the truncated Defuddle candidate was about 9 KB for an
# estimated 410 KB body (~2%), while Trafilatura returned about 466 KB. Keep
# Defuddle by default and switch only when it covers under half the estimated
# body and another usable candidate offers at least 1.5x useful text.
DEFAULT_PRIMARY_SWITCH_COVERAGE_RATIO = 0.50
DEFAULT_ALTERNATE_COVERAGE_GAIN = 1.50

CommandRunner = Callable[[list[str], int], tuple[int, str, str]]
TrafilaturaFetcher = Callable[[str], object]
TrafilaturaExtractor = Callable[..., str | None]
TrafilaturaMetadataExtractor = Callable[[object], object]


class WebFetchError(OSError):
    def __init__(self, code: str, *, http_status: int | None, attempts: list[dict[str, object]]) -> None:
        super().__init__(code)
        self.code = code
        self.http_status = http_status
        self.attempts = attempts


class _NormalizedRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return super().redirect_request(req, fp, code, msg, headers, normalize_http_url(newurl))


def _fetch_web_html(url: str, *, timeout: int = 20, sleep=time.sleep) -> tuple[str | bytes, dict[str, object]]:
    """Fetch one source HTML with bounded proxy/direct retries and safe receipts."""
    attempts: list[dict[str, object]] = []
    last_status: int | None = None
    routes = ("proxy", "direct", "proxy", "direct")
    for index, route in enumerate(routes):
        opener = (
            urllib.request.build_opener()
            if route == "proxy"
            else urllib.request.build_opener(urllib.request.ProxyHandler({}))
        )
        if isinstance(opener, urllib.request.OpenerDirector):
            opener.handlers = [
                handler for handler in opener.handlers
                if not isinstance(handler, urllib.request.HTTPRedirectHandler)
            ]
            opener.add_handler(_NormalizedRedirects())
        request = urllib.request.Request(url, headers={"User-Agent": "Openmd/1.0", "Accept-Language": "en,zh;q=0.8"})
        try:
            with opener.open(request, timeout=timeout) as response:
                status = getattr(response, "status", None)
                if type(status) is int:
                    last_status = status
                body = response.read(20 * 1024 * 1024)
                charset = response.headers.get_content_charset()
            attempts.append({"route": route, "outcome": "success", "http_status": last_status})
            if not body:
                raise WebFetchError("web_fetch_empty", http_status=last_status, attempts=attempts)
            text = body.decode(charset, errors="replace") if charset else body
            return text, {"route": route, "attempts": index + 1, "http_status": last_status, "retry_history": attempts}
        except WebFetchError:
            raise
        except urllib.error.HTTPError as exc:
            last_status = exc.code
            retryable = exc.code in {408, 500, 502, 503, 504}
            code = "web_fetch_http_error"
            attempts.append({"route": route, "outcome": code, "http_status": exc.code})
        except UnicodeEncodeError:
            retryable = False
            code = "web_fetch_url_encoding_error"
            attempts.append({"route": route, "outcome": code, "http_status": None})
        except (TimeoutError, urllib.error.URLError, ConnectionError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            retryable = True
            code = "web_fetch_timeout" if isinstance(reason, TimeoutError) else "web_fetch_connection_error"
            attempts.append({"route": route, "outcome": code, "http_status": None})
        if not retryable:
            break
        if index < len(routes) - 1:
            sleep(0.2 * (index + 1))
    raise WebFetchError(code, http_status=last_status, attempts=attempts)


def _arxiv_html_url(url: str) -> str | None:
    """Map a public arXiv abstract URL to its HTML full-text counterpart."""
    parsed = urlsplit(url)
    if parsed.hostname not in {"arxiv.org", "www.arxiv.org"}:
        return None
    match = re.fullmatch(r"/abs/(\d{4}\.\d{4,5}(?:v\d+)?)", parsed.path)
    if not match:
        return None
    return f"https://arxiv.org/html/{match.group(1)}"


def _fetch_arxiv_html(url: str) -> tuple[str | bytes, dict[str, object]]:
    """Read abs metadata first, then prefer the corresponding HTML full text."""
    abs_html, abs_receipt = _fetch_web_html(url)
    fulltext_url = _arxiv_html_url(url)
    if fulltext_url is None:
        return abs_html, abs_receipt
    abs_text = abs_html.decode("utf-8", "replace") if isinstance(abs_html, bytes) else abs_html
    abs_soup = BeautifulSoup(abs_text, "html.parser")

    def meta_value(name: str) -> str | None:
        node = abs_soup.find("meta", attrs={"name": name}) or abs_soup.find("meta", attrs={"property": name})
        value = node.get("content", "").strip() if node else ""
        return value or None

    authors = [
        node.get("content", "").strip()
        for node in abs_soup.find_all("meta", attrs={"name": "citation_author"})
        if node.get("content", "").strip()
    ]
    arxiv_metadata = {
        "title": meta_value("citation_title"),
        "authors": authors,
        "published_at": meta_value("citation_date"),
        "arxiv_id": meta_value("citation_arxiv_id"),
    }
    arxiv_metadata = {key: value for key, value in arxiv_metadata.items() if value}
    try:
        fulltext_html, fulltext_receipt = _fetch_web_html(fulltext_url)
    except WebFetchError as exc:
        receipt = {
            **abs_receipt,
            "retry_history": [*abs_receipt.get("retry_history", []), *exc.attempts],
            "arxiv_full_text": {
                "available": False,
                "error_code": exc.code,
                "http_status": exc.http_status,
            },
            "arxiv_metadata": arxiv_metadata,
        }
        return abs_html, receipt
    receipt = {
        **fulltext_receipt,
        "attempts": int(abs_receipt.get("attempts", 1)) + int(fulltext_receipt.get("attempts", 1)),
        "retry_history": [*abs_receipt.get("retry_history", []), *fulltext_receipt.get("retry_history", [])],
        "metadata_page_http_status": abs_receipt.get("http_status"),
        "arxiv_full_text": {"available": True, "http_status": fulltext_receipt.get("http_status")},
        "arxiv_metadata": arxiv_metadata,
    }
    return fulltext_html, receipt


_RFC_PATH = re.compile(r"/rfc/rfc(\d+)(?:\.html)?/?", re.I)
_RFC_MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"
_RFC_RUNNING_HEADER = re.compile(rf"\s*RFC\s+(\d+)\s{{2,}}(.+?)\s{{2,}}(?:{_RFC_MONTHS})\s+\d{{4}}\s*", re.I)


def _rfc_plain_text_url(url: str) -> str | None:
    """Any RFC Editor document page -> its official full plain-text edition."""
    parsed = urlsplit(url)
    if parsed.hostname not in {"www.rfc-editor.org", "rfc-editor.org"}:
        return None
    match = _RFC_PATH.fullmatch(parsed.path)
    return f"https://www.rfc-editor.org/rfc/rfc{int(match.group(1))}.txt" if match else None


def _rfc_title(lines: list[str]) -> str:
    """'RFC <n>: <title>' from the document head; older RFCs also carry it in running headers."""
    number = next((m.group(1) for m in (re.search(r"Request for Comments:\s*(\d+)", line, re.I) for line in lines[:40]) if m), None)
    header = next((m for m in map(_RFC_RUNNING_HEADER.fullmatch, lines) if m), None)
    number = number or (header.group(1) if header else None)
    title = None
    start = next((i for i, line in enumerate(lines[:40]) if re.search(r"Request for Comments:", line, re.I)), None)
    if start is not None:
        i = start
        while i < len(lines) and lines[i].strip():   # rest of the left/right header block
            i += 1
        while i < len(lines) and not lines[i].strip():
            i += 1
        parts = []
        while i < len(lines) and lines[i].strip() and lines[i].startswith("   "):  # centred title lines
            parts.append(lines[i].strip())
            i += 1
        title = " ".join(parts) or None
    if title is None and header:
        title = header.group(2).strip()
    if number is None:
        return title or "RFC"
    return f"RFC {int(number)}: {title}" if title else f"RFC {int(number)}"


def _format_rfc_plain_text(source: str | bytes) -> str:
    """Convert RFC Editor text while retaining headings and indented ABNF."""
    text = source.decode("utf-8", "replace") if isinstance(source, bytes) else source
    lines = text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n").replace("\f", "\n").splitlines()
    lines = [line.rstrip() for line in lines]
    # Running page headers ("RFC 9110   HTTP Semantics   June 2022") give the
    # number and short title; page footers end with "[Page N]". Both repeat on
    # every page and are dropped from the body.
    title = _rfc_title(lines)
    lines = [
        line for line in lines
        if not re.fullmatch(r".*\[Page\s+[0-9ivxlc]+\]\s*", line, re.I)
        and not _RFC_RUNNING_HEADER.fullmatch(line)
    ]

    content: list[str] = []
    for line in lines:
        if not content and not line.strip():
            continue
        content.append(line)

    heading_pattern = re.compile(r"^(\s*)(\d+(?:\.\d+)*\.)\s+(.+?)\s*$")
    appendix_pattern = re.compile(r"^\s*(Appendix\s+[A-Z](?:\.\s+.*)?)\s*$", re.I)
    abnf_start = re.compile(r"^\s*[A-Za-z][A-Za-z0-9-]*\s*=\s*\S")
    http_message_start = re.compile(r"^\s*(?:[A-Z]+\s+\S+\s+HTTP/\d(?:\.\d)?|HTTP/\d(?:\.\d)?\s+\d{3})\b")
    output = [f"# {title}", ""]
    in_abnf = False
    for line in content:
        heading = heading_pattern.match(line)
        appendix = appendix_pattern.match(line)
        if in_abnf and (not line.strip() or heading or appendix):
            output.extend(("```", ""))
            in_abnf = False
        if heading:
            level = min(6, 1 + heading.group(2).count("."))
            output.append(f"{'#' * level} {heading.group(2)} {heading.group(3)}")
        elif appendix:
            output.append(f"## {appendix.group(1)}")
        else:
            if (abnf_start.match(line) or http_message_start.match(line)) and not in_abnf:
                if output and output[-1] != "":
                    output.append("")
                output.append("```")
                in_abnf = True
            output.append(line)
    if in_abnf:
        output.extend(("```", ""))

    while output and not output[-1].strip():
        output.pop()
    return "\n".join(output) + "\n"


def _fetch_rfc_plain_text(url: str) -> tuple[str, dict[str, object]] | None:
    text_url = _rfc_plain_text_url(url)
    if text_url is None:
        return None
    try:
        source, receipt = _fetch_web_html(text_url)
    except WebFetchError:
        return None
    markdown = _format_rfc_plain_text(source)
    if markdown.startswith("# RFC\n"):  # very old RFCs lack a parsable head; keep at least the number
        markdown = f"# RFC {text_url.rsplit('rfc', 1)[1].removesuffix('.txt')}" + markdown[len("# RFC"):]
    return markdown, receipt


def estimate_html_body_chars(html: str) -> int:
    """Estimate visible article scale after removing common page chrome."""
    if not html:
        return 0
    try:
        soup = BeautifulSoup(html, "html.parser")
        for node in soup(["script", "style", "noscript", "template", "nav", "header", "footer", "aside", "form"]):
            node.decompose()
        chrome_marker = re.compile(r"(?:nav|menu|sidebar|breadcrumb|footer|header|search|toolbar|actions|related|recommend|social|share|citation|download)", re.I)
        for node in soup.find_all(True):
            if node.parent is None:
                continue
            attrs = " ".join(str(value) for key, value in node.attrs.items() if key in {"id", "class", "role", "aria-label"})
            if attrs and chrome_marker.search(attrs):
                node.decompose()
        roots = [node for node in (soup.find("article"), soup.find("main"), soup.body) if node is not None]
        text = max((node.get_text(" ", strip=True) for node in roots), key=lambda value: len(value), default=soup.get_text(" ", strip=True))
        return len("".join(text.split()))
    except Exception:
        return 0


def apply_completeness_guard(
    candidates: list[TranscriptResult],
    *,
    html: str | None = None,
    title: str | None = None,
    body_chars: int | None = None,
    min_coverage_ratio: float = DEFAULT_MIN_COVERAGE_RATIO,
) -> TranscriptResult:
    """Choose the most complete candidate and make low coverage explicit."""
    if not candidates:
        return _failed("web_article.completeness", "no_candidate", "No extraction candidate was produced.")
    snapshot_candidate = any(
        result.backend.startswith("page_snapshot.") or result.metadata.get("captured_from") == "browser_snapshot"
        for result in candidates
    )
    estimate = body_chars if body_chars is not None else estimate_html_body_chars(html or "")

    def chars(result: TranscriptResult) -> int:
        return len("".join((result.text or "").split()))

    def noise_ratio(result: TranscriptResult) -> float:
        lines = [line.strip() for line in (result.text or "").splitlines() if line.strip()]
        if not lines:
            return 1.0
        link_lines = sum(1 for line in lines if "](" in line or line.lower().startswith(("navigation", "related links", "recommended")))
        return link_lines / len(lines)

    def link_directory_chars(result: TranscriptResult) -> int:
        """Count link-only sections after a heading as page chrome, not article text.

        Extractors sometimes append recommendation/tool directories to an
        otherwise good article. A section qualifies only when it has at least
        three standalone links and no prose lines; normal paragraphs with
        inline links and prose-bearing reference sections remain untouched.
        """
        lines = (result.text or "").splitlines()
        noise_chars = 0
        section_lines: list[str] = []
        has_heading = False
        current_heading: str | None = None

        def count_if_directory(heading: str | None = None) -> int:
            populated = [line.strip() for line in section_lines if line.strip()]
            link_lines = [line for line in populated if re.fullmatch(r"(?:[-*+]\s*)?(?:\[[^\]]+\]\([^)]*\)|!\[[^\]]*\]\([^)]*\))\s*", line)]
            standalone_directory = len(link_lines) >= 3 and len(link_lines) == len(populated)
            heading_suggests_chrome = bool(
                heading
                and re.search(r"\b(?:tools?|features?|demos?|bookmark|more|related|recommended|recommenders?|explore|associated|share|popular|trending|follow)\b", heading, re.I)
                and not re.search(r"\b(?:references?|bibliograph(?:y|ies)|works cited|history|versions?)\b", heading, re.I)
            )
            markdown_links = re.findall(r"(?<!!)\[[^\]]+\]\([^)]*\)|!\[[^\]]*\]\([^)]*\)", "\n".join(populated))
            without_links = re.sub(r"(?<!!)\[([^\]]+)\]\([^)]*\)|!\[([^\]]*)\]\([^)]*\)", lambda match: match.group(1) or match.group(2) or "", "\n".join(populated))
            residual = len(re.sub(r"[\s*_#>`~|-]+", "", without_links))
            headed_link_module = heading_suggests_chrome and len(markdown_links) >= 3 and residual < 140
            if has_heading and (standalone_directory or headed_link_module):
                return len(re.sub(r"\s+", "", (heading or "") + "\n" + "\n".join(section_lines)))
            return 0

        for line in lines:
            heading = re.match(r"^\s{0,3}(#{1,6})\s+(.+?)\s*$", line)
            if heading:
                noise_chars += count_if_directory(current_heading)
                section_lines = []
                has_heading = True
                current_heading = heading.group(2)
            elif has_heading:
                section_lines.append(line)
            else:
                current_heading = None
        noise_chars += count_if_directory(current_heading)
        return noise_chars

    def useful_chars(result: TranscriptResult) -> int:
        return max(0, chars(result) - link_directory_chars(result))

    usable = [result for result in candidates if result.has_usable_text]
    ranked = sorted(
        usable or candidates,
        key=lambda result: (
            useful_chars(result) - int(chars(result) * noise_ratio(result) * 0.25),
            useful_chars(result),
            -noise_ratio(result),
            chars(result),
        ),
        reverse=True,
    )
    primary = next((result for result in usable if "defuddle" in result.backend.lower()), None)
    selected = primary or (ranked[0] if ranked else candidates[0])
    selection_reasons: dict[str, str] = {
        result.backend: "primary_defuddle_candidate" if result is primary else "primary_candidate_default"
        for result in candidates
    }
    if primary is not None and estimate:
        primary_coverage = useful_chars(primary) / estimate
        alternative = next((result for result in ranked if result is not primary), None)
        if (
            alternative is not None
            and primary_coverage < DEFAULT_PRIMARY_SWITCH_COVERAGE_RATIO
            and useful_chars(alternative) >= max(1, useful_chars(primary)) * DEFAULT_ALTERNATE_COVERAGE_GAIN
        ):
            selected = alternative
            selection_reasons[alternative.backend] = (
                "defuddle_below_50_percent_and_alternative_at_least_1_5x_useful_text"
            )
    elif primary is None and ranked:
        selected = ranked[0]
        selection_reasons[selected.backend] = "highest_ranked_candidate_without_defuddle"
    selected_chars = chars(selected)
    records = [
        {
            "engine": result.backend,
            "characters": chars(result),
            "status": result.status,
            "noise_ratio": round(noise_ratio(result), 4),
            "noise_chars": link_directory_chars(result),
            "selection_reason": "selected:" + selection_reasons.get(result.backend, "alternate_candidate") if result is selected else selection_reasons.get(result.backend, "not_selected"),
            "selected": result is selected,
        }
        for result in candidates
    ]
    coverage = {
        "body_chars": estimate or None,
        "selected_chars": selected_chars,
        "ratio": round(selected_chars / estimate, 4) if estimate else None,
        "threshold": min_coverage_ratio,
    }
    metadata = {
        **selected.metadata,
        "extraction_candidates": records,
        "coverage_estimate": coverage,
        "completeness": {
            "status": "unknown" if snapshot_candidate else "ok",
            "coverage_estimate": coverage,
            "extraction_candidates": records,
        },
    }
    result = replace(selected, metadata=metadata, title=selected.title or title)
    if estimate and selected.has_usable_text and selected_chars / estimate < min_coverage_ratio and not snapshot_candidate:
        metadata["completeness"]["status"] = "downgrade"
        return replace(
            result,
            status="partial",
            source_level="partial_extraction",
            error_code="partial_coverage",
            error_summary=f"候选正文仅覆盖估算页面正文的 {selected_chars / estimate:.1%}。",
            source_notes=[*result.source_notes, "可能只截取了部分正文，已按 partial 回执。"],
        )
    return result


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

        shared_html = item.raw_meta.get("source_html")
        if isinstance(shared_html, (str, bytes)) and shared_html:
            downloaded, fetch_error = shared_html, None
        else:
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
                result = _failed(self.name, getattr(fetch_error, "code", "web_article_fetch_failed"), "Web page could not be fetched.")
                return replace(result, metadata={"http_status": getattr(fetch_error, "http_status", None), "fetch_attempts": getattr(fetch_error, "attempts", [])})
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
