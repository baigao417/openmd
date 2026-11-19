"""Weixin/WeChat Official Account article providers.

``weixin.article.local`` is an HTTP-first local parser for public article
HTML. Browser/session fallbacks are registered separately and only after the
caller enables browser backends.
"""

from __future__ import annotations

import html as html_mod
import json
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlparse, urlunparse
from urllib.request import Request, urlopen

from openmd.core.opencli_output import load_opencli_json
from openmd.backends.base import (
    BackendCapability,
    BackendDescriptor,
    BackendExtractRequest,
    BackendHealth,
)
from openmd.backends.executor import BackendProviderRegistry, DiscoveryProviderRegistry
from openmd.backends.registry import BackendRuntimeOptions
from openmd.backends.web_article import OPENCLI_WEB_READ_BACKEND, OpenCliWebReadProvider
from openmd.core.models import TranscriptResult
from openmd.core.secrets import sanitize_error_summary
from openmd.runtime.tempdirs import owned_temporary_directory
from openmd.runtime.interpreters import run_interruptibly


WEIXIN_LOCAL_BACKEND = "weixin.article.local"
WECHAT_ARTICLE_EXPORTER_DOWNLOAD_BACKEND = "wechat-article-exporter.download"
OPENCLI_WEIXIN_DOWNLOAD_BACKEND = "opencli.weixin.download"
DEFAULT_MIN_WEIXIN_CHARS = 20
DEFAULT_WECHAT_ARTICLE_EXPORTER_BASE_URL = "http://127.0.0.1:3000"
WECHAT_ARTICLE_EXPORTER_BASE_URL_ENV = "OPENMD_WECHAT_ARTICLE_EXPORTER_BASE_URL"

HtmlFetcher = Callable[[str, int], str]
CommandRunner = Callable[[list[str], int], tuple[int, str, str]]
TextFetcher = Callable[[str, int], tuple[int, str]]


def register_backend_providers(
    registry: BackendProviderRegistry,
    options: BackendRuntimeOptions,
) -> None:
    """Register Weixin extraction providers behind explicit runtime gates."""
    requested = options.backend_override
    if options.enable_network_backends and requested in {None, WEIXIN_LOCAL_BACKEND}:
        registry.register(WEIXIN_LOCAL_BACKEND, WeixinArticleLocalProvider())

    if options.enable_browser_backends and requested in {None, WECHAT_ARTICLE_EXPORTER_DOWNLOAD_BACKEND}:
        registry.register(WECHAT_ARTICLE_EXPORTER_DOWNLOAD_BACKEND, WechatArticleExporterDownloadProvider())

    if options.enable_browser_backends and requested in {None, OPENCLI_WEIXIN_DOWNLOAD_BACKEND}:
        registry.register(OPENCLI_WEIXIN_DOWNLOAD_BACKEND, OpenCliWeixinDownloadProvider())
    if options.enable_browser_backends and requested in {None, OPENCLI_WEB_READ_BACKEND}:
        registry.register(OPENCLI_WEB_READ_BACKEND, OpenCliWebReadProvider())


def register_discovery_providers(
    registry: DiscoveryProviderRegistry,
    options: BackendRuntimeOptions,
) -> None:
    """Register Weixin account/article discovery behind explicit browser gates."""
    if not options.enable_browser_backends:
        return
    requested = options.requested_discovery_provider
    from openmd.adapters.weixin.discovery import (
        WECHAT_ARTICLE_EXPORTER_DISCOVERY_BACKEND,
        WECHAT_ARTICLE_EXPORTER_HTTP_DISCOVERY_BACKEND,
        WechatArticleExporterDiscoveryProvider,
        WechatArticleExporterHttpDiscoveryProvider,
    )
    requested = _normalize_weixin_discovery_backend(requested)

    if requested in {None, WECHAT_ARTICLE_EXPORTER_DISCOVERY_BACKEND}:
        registry.register(
            WECHAT_ARTICLE_EXPORTER_DISCOVERY_BACKEND,
            WechatArticleExporterDiscoveryProvider(),
            platforms=("weixin",),
            default_for_platforms=False,
        )
    if requested in {None, WECHAT_ARTICLE_EXPORTER_HTTP_DISCOVERY_BACKEND}:
        registry.register(
            WECHAT_ARTICLE_EXPORTER_HTTP_DISCOVERY_BACKEND,
            WechatArticleExporterHttpDiscoveryProvider(),
            platforms=("weixin",),
            default_for_platforms=requested is None,
        )


def _normalize_weixin_discovery_backend(value: str | None) -> str | None:
    if value in {
        "wechat-article-exporter-api",
        "wechat-article-exporter-http",
        "weixin-exporter-api",
        "weixin-exporter-http",
        "weixin-http",
    }:
        return "wechat-article-exporter.http"
    return value


class WeixinArticleLocalProvider:
    """HTTP-first parser for public ``mp.weixin.qq.com/s`` articles."""

    name = WEIXIN_LOCAL_BACKEND

    def __init__(
        self,
        *,
        fetcher: HtmlFetcher | None = None,
        timeout: int = 30,
        min_article_chars: int = DEFAULT_MIN_WEIXIN_CHARS,
    ) -> None:
        self._fetcher = fetcher or _fetch_html
        self.timeout = timeout
        self.min_article_chars = max(0, min_article_chars)

    def descriptor(self) -> BackendDescriptor:
        capability = BackendCapability(
            platforms=("weixin",),
            source_types=("article",),
            input_types=("single", "batch"),
            source_levels=("native_text", "partial_extraction"),
            requires_network=True,
            requires_credentials=False,
            optional_config=("beautifulsoup4", "markdownify"),
        )
        return BackendDescriptor(
            name=self.name,
            layer="L1 specialized extractor",
            capabilities=(capability,),
            description="HTTP-first Weixin article HTML parser; no browser, cloud API, media, or ASR.",
            provider="openmd.backends.weixin",
        )

    def health(self) -> BackendHealth:
        try:
            import bs4  # type: ignore[import-not-found]  # noqa: F401
        except ModuleNotFoundError:
            return BackendHealth(
                ok=False,
                dependencies={"beautifulsoup4": "missing"},
                error_code="backend_dependency_missing",
                error_summary="Install beautifulsoup4 or use a browser fallback backend.",
            )
        return BackendHealth(ok=True, dependencies={"beautifulsoup4": "available"})

    def extract(self, request: BackendExtractRequest) -> TranscriptResult:
        item = request.item
        if item.platform != "weixin":
            return _failed(
                self.name,
                "unsupported_platform",
                f"Weixin article provider only supports weixin, got {item.platform}.",
            )

        url = normalize_weixin_url(item.url)
        if not _is_weixin_article_url(url):
            return _failed(self.name, "invalid_input", "Expected an mp.weixin.qq.com article URL.")

        try:
            from bs4 import BeautifulSoup  # type: ignore[import-not-found]
        except ModuleNotFoundError:
            return _failed(
                self.name,
                "backend_dependency_missing",
                "beautifulsoup4 is not installed; install it or enable browser fallback.",
            )

        try:
            html = self._fetcher(url, self.timeout)
        except Exception as exc:
            return _failed(self.name, "weixin_fetch_failed", exc)

        soup = BeautifulSoup(html, "html.parser")
        content_el = soup.select_one("#js_content")
        if content_el is None:
            from openmd.core.content_validation import weixin_error_page_reason

            for script in soup(["script", "style"]):
                script.decompose()
            verdict = weixin_error_page_reason(soup.get_text("\n"))
            if verdict == "error":
                return _failed(self.name, "weixin_error_page", "公众号返回了错误页，没有正文；链接可能失效、已删除或已过期。")
            if verdict == "challenge":
                return _failed(self.name, "browser_challenge_page", "公众号要求完成环境验证，未取得正文。")
            return _failed(
                self.name,
                "weixin_source_empty",
                "HTTP fetch did not expose #js_content; classify as source-empty unless a gated fallback recovers content.",
            )

        metadata = _extract_metadata(soup, html)
        content_html, code_blocks = _process_content(soup)
        markdown = _html_to_markdown(content_html, code_blocks).strip()
        if len("".join(markdown.split())) < self.min_article_chars:
            return _failed(self.name, "weixin_source_empty", "Parsed Weixin article body was empty.")

        return TranscriptResult(
            text=markdown.rstrip() + "\n",
            status="verified",
            source_level="native_text",
            backend=self.name,
            title=metadata.get("title") or item.title,
            author=metadata.get("author") or item.author,
            published_at=metadata.get("published_at") or item.published_at,
            source_notes=[
                "Weixin article HTML was fetched over HTTP and parsed locally.",
                "#js_content was converted to Markdown; data-src image URLs were preserved.",
                "Weixin code-snippet blocks were converted to fenced code blocks where detected.",
                "No browser, cloud API, media download, or ASR action was attempted.",
            ],
            metadata={"strategy": "http_first", "normalized_url": url},
        )


class OpenCliWeixinDownloadProvider:
    """Browser/session fallback for Weixin articles through OpenCLI."""

    name = OPENCLI_WEIXIN_DOWNLOAD_BACKEND

    def __init__(self, *, runner: CommandRunner | None = None, timeout: int = 180) -> None:
        self._runner = runner or _run_command
        self.timeout = timeout

    def descriptor(self) -> BackendDescriptor:
        capability = BackendCapability(
            platforms=("weixin",),
            source_types=("article",),
            input_types=("single", "batch"),
            source_levels=("third_party_extractor", "partial_extraction"),
            requires_network=True,
        )
        return BackendDescriptor(
            name=self.name,
            layer="L3 browser/session fallback",
            capabilities=(capability,),
            description="Use OpenCLI weixin download and read generated Markdown from a temp directory.",
            provider="opencli.weixin",
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
        with owned_temporary_directory(prefix="openmd-weixin-") as tmp_dir:
            command = [
                "opencli",
                "weixin",
                "download",
                "--url",
                item.url,
                "--output",
                tmp_dir,
                "--download-images",
                "false",
                "-f",
                "json",
                "--window",
                "background",
                "--site-session", "ephemeral", "--keep-tab",
                "false",
            ]
            exit_code, stdout, stderr = self._runner(command, self.timeout)
            if exit_code != 0:
                return _failed(self.name, f"backend_exit_{exit_code}", stderr or stdout)

            markdown_files = sorted(Path(tmp_dir).rglob("*.md"))
            if not markdown_files:
                return _failed(self.name, "empty_result", "OpenCLI weixin download produced no Markdown file.")

            text = markdown_files[0].read_text(encoding="utf-8", errors="replace").strip()
            if not text:
                return _failed(self.name, "empty_result", "OpenCLI weixin download produced empty Markdown.")

            metadata = _opencli_stdout_metadata(stdout)
            return TranscriptResult(
                text=text.rstrip() + "\n",
                status="verified",
                source_level="third_party_extractor",
                backend=self.name,
                title=metadata.get("title") or _markdown_title(text) or item.title,
                author=metadata.get("author") or item.author,
                published_at=metadata.get("publish_time") or item.published_at,
                source_notes=[
                    "OpenCLI weixin download returned Markdown via browser/session fallback.",
                    "--download-images false was used; Openmd owns optional asset localization.",
                    "Generated Markdown was read from a temporary directory and raw stdout/stderr was not persisted.",
                ],
                )


class WechatArticleExporterDownloadProvider:
    """Read article Markdown through a running ``wechat-article-exporter`` sidecar."""

    name = WECHAT_ARTICLE_EXPORTER_DOWNLOAD_BACKEND

    def __init__(
        self,
        *,
        base_url: str | None = None,
        fetch_text: TextFetcher | None = None,
        timeout: int = 60,
        min_article_chars: int = DEFAULT_MIN_WEIXIN_CHARS,
    ) -> None:
        import os

        self.base_url = _normalize_exporter_base_url(
            base_url or os.environ.get(WECHAT_ARTICLE_EXPORTER_BASE_URL_ENV) or DEFAULT_WECHAT_ARTICLE_EXPORTER_BASE_URL
        )
        self._fetch_text = fetch_text or _fetch_text
        self.timeout = timeout
        self.min_article_chars = max(0, min_article_chars)

    def descriptor(self) -> BackendDescriptor:
        capability = BackendCapability(
            platforms=("weixin",),
            source_types=("article",),
            input_types=("single", "batch"),
            source_levels=("third_party_extractor", "partial_extraction"),
            requires_network=True,
            requires_credentials=False,
        )
        return BackendDescriptor(
            name=self.name,
            layer="L3 local sidecar extractor",
            capabilities=(capability,),
            description="Use a running wechat-article-exporter public download API to return Markdown.",
            provider="wechat-article-exporter",
            is_fallback=True,
        )

    def health(self) -> BackendHealth:
        return BackendHealth(
            ok=True,
            dependencies={"wechat-article-exporter": "configured"},
            warnings=("sidecar availability is checked at extraction time",),
        )

    def extract(self, request: BackendExtractRequest) -> TranscriptResult:
        item = request.item
        if item.platform != "weixin":
            return _failed(
                self.name,
                "unsupported_platform",
                f"WechatArticleExporterDownloadProvider only supports weixin, got {item.platform}.",
            )
        url = normalize_weixin_url(item.url)
        if not _is_weixin_article_url(url):
            return _failed(self.name, "invalid_input", "Expected an mp.weixin.qq.com article URL.")

        endpoint = _build_exporter_download_url(self.base_url, url)
        status, text = self._fetch_text(endpoint, self.timeout)
        if status == 204:
            return _failed(
                self.name,
                "weixin_source_empty",
                "wechat-article-exporter returned HTTP 204 with no article content.",
            )
        if status < 200 or status >= 300:
            return _failed(self.name, _exporter_http_error_code(status), text)
        markdown = _clean_exporter_markdown(text)
        if _looks_like_exporter_empty_shell(markdown):
            return _failed(
                self.name,
                "weixin_source_empty",
                "wechat-article-exporter returned a fixed empty Markdown shell.",
            )
        if len("".join(markdown.split())) < self.min_article_chars:
            return _failed(self.name, "weixin_source_empty", "wechat-article-exporter returned empty Markdown.")
        return TranscriptResult(
            text=markdown.rstrip() + "\n",
            status="verified",
            source_level="third_party_extractor",
            backend=self.name,
            title=_markdown_title(markdown) or item.title,
            author=item.author,
            published_at=item.published_at,
            source_notes=[
                "wechat-article-exporter public download API returned Markdown.",
                "Openmd called a read-only endpoint on an already-running sidecar.",
                "No browser/login/service lifecycle action or credential import was attempted by Openmd.",
            ],
            metadata={"strategy": "wechat_article_exporter_download", "sidecar_base_url": self.base_url},
        )


def normalize_weixin_url(raw: str) -> str:
    value = str(raw or "").strip()
    if not value:
        return value
    if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
        value = value[1:-1].strip()
    if value.startswith("<") and value.endswith(">"):
        value = value[1:-1].strip()
    value = re.sub(r"\\+([:/&?=#%])", r"\1", value)
    value = html_mod.unescape(value)
    if value.startswith("mp.weixin.qq.com/") or value.startswith("//mp.weixin.qq.com/"):
        value = "https://" + value.lstrip("/")
    parsed = urlparse(value)
    if parsed.scheme in {"http", "https"} and (parsed.hostname or "").lower() == "mp.weixin.qq.com":
        value = urlunparse(("https", "mp.weixin.qq.com", parsed.path, parsed.params, parsed.query, parsed.fragment))
    return value


def _is_weixin_article_url(url: str) -> bool:
    parsed = urlparse(url)
    return (
        parsed.scheme in {"http", "https"}
        and parsed.netloc.lower() == "mp.weixin.qq.com"
        and (parsed.path == "/s" or parsed.path.startswith("/s/"))
    )


def _fetch_html(url: str, timeout: int) -> str:
    request = Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Referer": "https://mp.weixin.qq.com/",
        },
    )
    with urlopen(request, timeout=timeout) as response:
        content = response.read()
        encoding = response.headers.get_content_charset() or "utf-8"
    return content.decode(encoding, errors="replace")


def _extract_metadata(soup: object, raw_html: str) -> dict[str, str]:
    title_el = soup.select_one("#activity-name")  # type: ignore[attr-defined]
    author_el = soup.select_one("#js_name")  # type: ignore[attr-defined]
    return {
        "title": title_el.get_text(strip=True) if title_el else "",
        "author": author_el.get_text(strip=True) if author_el else "",
        "published_at": _extract_publish_time(raw_html),
    }


def _extract_publish_time(raw_html: str) -> str:
    patterns = (
        r"create_time\s*:\s*JsDecode\('([^']+)'\)",
        r"create_time\s*:\s*'(\d+)'",
        r'create_time\s*[:=]\s*["\']?(\d+)["\']?',
    )
    for pattern in patterns:
        match = re.search(pattern, raw_html)
        if not match:
            continue
        value = match.group(1)
        if value.isdigit():
            return _format_timestamp(int(value))
        return value
    return ""


def _format_timestamp(timestamp: int) -> str:
    china_tz = timezone(timedelta(hours=8))
    return datetime.fromtimestamp(timestamp, tz=china_tz).strftime("%Y-%m-%d %H:%M:%S")


def _process_content(soup: object) -> tuple[str, list[dict[str, str]]]:
    content_el = soup.select_one("#js_content")  # type: ignore[attr-defined]
    if content_el is None:
        return "", []

    for img in content_el.find_all("img"):
        data_src = img.get("data-src") or img.get("data-original") or img.get("data-backsrc")
        if data_src:
            img["src"] = _absolute_image_url(str(data_src))

    code_blocks: list[dict[str, str]] = []
    for element in content_el.select(".code-snippet__fix"):
        for line_index in element.select(".code-snippet__line-index"):
            line_index.decompose()

        pre = element.select_one("pre[data-lang]")
        language = str(pre.get("data-lang", "")) if pre else ""
        lines: list[str] = []
        for code_tag in element.find_all("code"):
            text = code_tag.get_text()
            if re.match(r"^[ce]?ounter\(line", text):
                continue
            lines.append(text)
        if not lines:
            lines.append(element.get_text("\n"))

        placeholder = f"OPENMD-CODEBLOCK-PLACEHOLDER-{len(code_blocks)}"
        code_blocks.append({"language": language, "code": "\n".join(lines).strip("\n")})
        replacement = soup.new_tag("p")  # type: ignore[attr-defined]
        replacement.string = placeholder
        element.replace_with(replacement)

    for selector in ("script", "style", ".qr_code_pc", ".reward_area", ".rich_media_tool"):
        for tag in content_el.select(selector):
            tag.decompose()

    return str(content_el), code_blocks


def _html_to_markdown(content_html: str, code_blocks: list[dict[str, str]]) -> str:
    try:
        import markdownify  # type: ignore[import-not-found]

        markdown = markdownify.markdownify(
            content_html,
            heading_style="ATX",
            bullets="-",
            convert=[
                "p",
                "h1",
                "h2",
                "h3",
                "h4",
                "h5",
                "h6",
                "strong",
                "em",
                "a",
                "img",
                "ul",
                "ol",
                "li",
                "blockquote",
                "br",
                "hr",
                "table",
                "thead",
                "tbody",
                "tr",
                "th",
                "td",
                "pre",
                "code",
            ],
        )
    except ModuleNotFoundError:
        markdown = _simple_html_to_markdown(content_html)

    for index, block in enumerate(code_blocks):
        placeholder = f"OPENMD-CODEBLOCK-PLACEHOLDER-{index}"
        fenced = f"\n```{block.get('language', '')}\n{block.get('code', '')}\n```\n"
        markdown = markdown.replace(placeholder, fenced)
    markdown = markdown.replace("\u00a0", " ")
    markdown = re.sub(r"\n{4,}", "\n\n\n", markdown)
    markdown = re.sub(r"[ \t]+$", "", markdown, flags=re.MULTILINE)
    return markdown


def _simple_html_to_markdown(content_html: str) -> str:
    from bs4 import BeautifulSoup  # type: ignore[import-not-found]

    soup = BeautifulSoup(content_html, "html.parser")
    for img in soup.find_all("img"):
        src = img.get("src")
        if src:
            img.replace_with(soup.new_string(f"\n![{img.get('alt', '')}]({src})\n"))
    for link in soup.find_all("a"):
        href = link.get("href")
        text = link.get_text(strip=True)
        if href and text:
            link.replace_with(soup.new_string(f"[{text}]({href})"))
    lines = [line.strip() for line in soup.get_text("\n").splitlines()]
    return "\n\n".join(line for line in lines if line)


def _absolute_image_url(url: str) -> str:
    if url.startswith("//"):
        return f"https:{url}"
    return url


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


def _normalize_exporter_base_url(value: str) -> str:
    parsed = urlparse(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return DEFAULT_WECHAT_ARTICLE_EXPORTER_BASE_URL
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", "", ""))


def _build_exporter_download_url(base_url: str, article_url: str) -> str:
    query = urlencode({"url": article_url, "format": "markdown"}, quote_via=quote)
    return f"{base_url}/api/public/v1/download?{query}"


def _fetch_text(url: str, timeout: int) -> tuple[int, str]:
    request = Request(url)
    try:
        with urlopen(request, timeout=timeout) as response:
            text = response.read().decode(response.headers.get_content_charset() or "utf-8", errors="replace")
            return response.status, text
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return exc.code, body
    except URLError as exc:
        return 599, sanitize_error_summary(exc) or "wechat-article-exporter sidecar is unreachable."


def _exporter_http_error_code(status: int) -> str:
    if status == 404:
        return "sidecar_endpoint_not_found"
    if status == 599:
        return "sidecar_unreachable"
    return f"sidecar_http_{status}"


def _clean_exporter_markdown(markdown: str) -> str:
    """Remove Weixin/exporter chrome noise while preserving article order."""
    lines = markdown.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    lines = _drop_leading_css_lines(lines)
    lines = [line for line in lines if not _is_weixin_reader_chrome_line(line)]
    cleaned = "\n".join(lines).strip()
    cleaned = re.sub(r"\n{4,}", "\n\n\n", cleaned)
    return cleaned


def _drop_leading_css_lines(lines: list[str]) -> list[str]:
    start = 0
    while start < len(lines) and not lines[start].strip():
        start += 1
    if start >= len(lines):
        return []
    first = lines[start].strip()
    if not _looks_like_css_noise(first):
        return lines[start:]

    end = start
    while end < len(lines):
        stripped = lines[end].strip()
        if not stripped:
            end += 1
            break
        if not _looks_like_css_noise(stripped):
            break
        end += 1
    return lines[end:]


def _looks_like_css_noise(line: str) -> bool:
    stripped = line.strip()
    if len(stripped) < 12:
        return False
    return "{" in stripped and "}" in stripped and any(
        token in stripped
        for token in (
            "max-width",
            "margin:",
            "display:",
            "height:",
            "width:",
            "#js_",
            "#js\\_",
            ".wx_",
            ".wx\\_",
        )
    )


def _is_weixin_reader_chrome_line(line: str) -> bool:
    stripped = line.strip()
    return stripped in {
        "在小说阅读器读本章",
        "去阅读",
        "在小说阅读器中沉浸阅读",
    }


def _looks_like_exporter_empty_shell(markdown: str) -> bool:
    compact = "".join(markdown.split())
    if not compact:
        return True
    if len(markdown.encode("utf-8", errors="ignore")) <= 420 and not re.search(r"[\u4e00-\u9fffA-Za-z0-9]{20}", compact):
        return True
    lower = markdown.lower()
    return "cgidatanew" in lower and "# article" not in lower and "## " not in lower


def _opencli_stdout_metadata(stdout: str) -> dict[str, str]:
    try:
        payload = load_opencli_json(stdout)
    except json.JSONDecodeError:
        return {}
    if isinstance(payload, list) and payload and isinstance(payload[0], dict):
        return {key: str(value) for key, value in payload[0].items() if value is not None}
    if isinstance(payload, dict):
        return {key: str(value) for key, value in payload.items() if value is not None}
    return {}


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
        source_notes=["weixin article provider returned structured failure"],
        error_code=code,
        error_summary=sanitize_error_summary(error),
        metadata={"failure_class": _weixin_failure_class(code)},
    )


def _weixin_failure_class(code: str) -> str:
    normalized = code.lower()
    if "auth" in normalized or "permission" in normalized:
        return "user_resolvable"
    if "source_empty" in normalized or "empty_result" in normalized or "not_found" in normalized:
        return "source_unavailable"
    if "unreachable" in normalized or "http_5" in normalized or "fetch_failed" in normalized or "backend_exit" in normalized:
        return "runtime_transient"
    return "code_resolvable"
