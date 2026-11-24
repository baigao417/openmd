"""Current page snapshot backend.

Snapshots are local HTML captured by the extension. Defuddle is used when
available; a small stdlib fallback keeps offline CLI fixtures testable without
silently claiming broader page coverage.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlsplit, urlunsplit

from bs4 import BeautifulSoup, Tag

from openmd.backends.base import BackendCapability, BackendDescriptor, BackendExtractRequest, BackendHealth
from openmd.backends.executor import BackendProviderRegistry
from openmd.backends.registry import BackendRuntimeOptions
from openmd.core.content_validation import html_to_markdown, login_wall_reason
from openmd.core.local_config import load_local_config
from openmd.core.login import normalize_login_result
from openmd.core.models import TranscriptResult
from openmd.backends.web_article import apply_completeness_guard, estimate_html_body_chars
from openmd.runtime.interpreters import run_interruptibly


SNAPSHOT_BACKEND = "page_snapshot.defuddle"
MAX_SNAPSHOT_BYTES = 50 * 1024 * 1024
_TITLE_COUNTER_PREFIX = re.compile(
    r"^\(\s*(?:\d+\s*封私信(?:\s*/\s*|\s+)\d+\s*条消息|\d+\s*封私信|\d+\s*条消息|\d+)\s*\)\s*"
)
_TITLE_SITE_SUFFIX = re.compile(r"\s+-\s+知乎\s*$", re.IGNORECASE)


def register_backend_providers(registry: BackendProviderRegistry, options: BackendRuntimeOptions) -> None:
    if options.backend_override in {None, SNAPSHOT_BACKEND}:
        registry.register(SNAPSHOT_BACKEND, SnapshotProvider())


class SnapshotProvider:
    name = SNAPSHOT_BACKEND

    def __init__(self, *, runner=None, max_bytes: int = MAX_SNAPSHOT_BYTES) -> None:
        self.runner = runner
        self.max_bytes = max_bytes

    def descriptor(self) -> BackendDescriptor:
        return BackendDescriptor(
            name=self.name,
            layer="L1 local snapshot extractor",
            capabilities=(BackendCapability(
                platforms=("page_snapshot", "ai_chat"), source_types=("article", "conversation"), input_types=("single",),
                source_levels=("browser_rendered", "third_party_extractor", "partial_extraction"),
                requires_network=False,
            ),),
            description="Read a loaded AI conversation or extract an article snapshot through Defuddle.",
            provider="openmd.backends.snapshot",
        )

    def health(self) -> BackendHealth:
        if self.runner is not None:
            return BackendHealth(ok=True, dependencies={"defuddle": "injected"})
        return BackendHealth(ok=True, dependencies={"defuddle": "managed" if _managed_defuddle_available() else "fallback"})

    def extract(self, request: BackendExtractRequest) -> TranscriptResult:
        item = request.item
        path_value = item.raw_meta.get("snapshot_path")
        if not isinstance(path_value, str) or not path_value:
            return _failed("snapshot_missing_path", "page_snapshot 输入缺少本地快照路径。")
        path = Path(path_value)
        try:
            if not path.is_file():
                return _failed("snapshot_missing_file", "当前页快照文件不存在。")
            if path.stat().st_size > self.max_bytes:
                return _failed("snapshot_too_large", "当前页快照超过 50 MB 上限。")
            html = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return _failed("snapshot_read_failed", "当前页快照无法读取。")
        from openmd.backends.loaded_chat import extract_loaded_chat, loaded_chat_site

        if loaded_chat_site(item.url):
            return extract_loaded_chat(html, item, backend=self.name)
        from openmd.backends.loaded_chat import loaded_snapshot_site

        site = loaded_snapshot_site(item.url)
        if site == "xiaohongshu":
            title = item.title or _html_title(html) or "小红书笔记"
            soup = BeautifulSoup(html, "html.parser")
            has_note_title = soup.select_one("[data-testid='note-title'], .title, h1") is not None
            has_note_body = soup.select_one("[data-testid='note-content'], .note-content, .desc") is not None
            if _snapshot_login_wall(html) and not (has_note_title or has_note_body):
                return normalize_login_result(_failed("login_wall", "网页返回登录墙，未取得目标正文。"), item)
            return _extract_xiaohongshu_snapshot(html, item, path)
        if site == "douyin":
            return _failed("douyin_snapshot_no_transcript", "抖音视频快照不提供逐字稿，请改走媒体转写路线。")
        page_title = clean_page_title(item.title or _html_title(html) or "当前页快照")
        if login_wall_reason(html, page_title):
            return normalize_login_result(_failed("login_wall", "网页返回登录墙，未取得目标正文。"), item)
        title = _article_heading_title(html, page_title)
        markdown, used_managed = self._html_to_markdown(path, html, item.url)
        text = (markdown or "").strip()
        if text.startswith("# ") and text.splitlines()[0].strip().removeprefix("# ").strip() == title.strip():
            text = "\n".join(text.splitlines()[1:]).lstrip()
        if not text:
            return _failed("snapshot_empty", "当前页快照没有可保存的正文。")
        notes = ["当前页快照仅覆盖浏览器捕获的可见 DOM，不包含 cookie 或 storage。"]
        if not used_managed:
            notes.append("未使用 Defuddle，正文抽取为简易回退")
        hint = snapshot_acceleration_hint(item.url)
        if hint:
            notes.append(hint)
        result = TranscriptResult(
            text=text.rstrip() + "\n",
            status="verified",
            source_level="third_party_extractor" if used_managed else "partial_extraction",
            backend=self.name,
            title=title,
            source_notes=notes,
            metadata={"captured_from": "browser_snapshot", "coverage": "visible_only", "snapshot_path": str(path)},
        )
        if item.platform == "page_snapshot":
            body_chars = estimate_html_body_chars(html)
            return apply_completeness_guard([result], body_chars=body_chars, title=title)
        return result

    def _html_to_markdown(self, path: Path, html: str, url: str) -> tuple[str, bool]:
        managed_markdown = ""
        if self.runner is not None:
            result = self.runner(path, html)
            if isinstance(result, dict):
                managed_markdown = str(result.get("contentMarkdown") or result.get("content") or "")
            else:
                managed_markdown = str(result or "")
        else:
            managed_markdown = _run_managed_defuddle(path, html, url) or ""
        try:
            fallback_markdown = html_to_markdown(html)
        except Exception:
            fallback_markdown = re.sub(r"<[^>]+>", "\n", html)
        if not managed_markdown:
            return fallback_markdown, False
        # Defuddle is usually the better article extractor, but it can discard
        # tables retained by the HTML converter. Compare the same snapshot and
        # keep the candidate with more Markdown table rows; ties favor Defuddle.
        if _markdown_table_row_count(fallback_markdown) > _markdown_table_row_count(managed_markdown):
            return fallback_markdown, False
        return managed_markdown, True


def _markdown_table_row_count(markdown: str) -> int:
    return sum(
        1 for line in markdown.splitlines()
        if line.strip().startswith("|") and line.count("|") >= 3
    )


def _defuddle_card_path() -> Path:
    return Path(__file__).resolve().parents[1] / "routing" / "cards" / "defuddle.json"


def _managed_defuddle_spec() -> tuple[str, str, str, dict[str, Any]] | None:
    try:
        card = json.loads(_defuddle_card_path().read_text(encoding="utf-8"))
        api = card["launcher"]["node_api"]
        module = str(api["module"])
        options = dict(api.get("options") or {})
        try:
            spec = load_local_config().get("tools", {}).get("defuddle") or {}
        except ValueError:
            spec = {}
        node = str(spec.get("node") or "") if isinstance(spec, dict) else ""
        root = str(spec.get("root") or "") if isinstance(spec, dict) else ""
        if node and root and Path(node).is_file() and Path(root).is_dir():
            return node, root, module, options
        # No configured location (fresh installs, the browser host's own
        # working directory): use the pinned copy Openmd manages itself.
        from openmd.runtime.managed_tools import ensure_defuddle, managed_defuddle_modules, node_dir

        if ensure_defuddle() != "managed":
            return None
        bundled = node_dir()
        if bundled is None:
            return None
        return str(bundled / ("node.exe" if os.name == "nt" else "node")), str(managed_defuddle_modules()), module, options
    except (OSError, TypeError, ValueError, KeyError):
        return None


def _managed_defuddle_available() -> bool:
    return _managed_defuddle_spec() is not None


def _run_managed_defuddle(path: Path, html: str, url: str) -> str | None:
    spec = _managed_defuddle_spec()
    if spec is None:
        return None
    node, root, module, options = spec
    script = r"""
const path = require('path');
const fs = require('fs');
const { Defuddle } = require(path.join(process.argv[1], process.argv[2]));
const { parseHTML } = require(path.join(process.argv[1], 'linkedom'));
(async () => {
  const html = fs.readFileSync(process.argv[3], 'utf8');
  const { document } = parseHTML(html);
  const result = await Defuddle(document, process.argv[4], { ...JSON.parse(process.argv[5]), markdown: true });
  process.stdout.write(JSON.stringify({ contentMarkdown: result.contentMarkdown || '', content: result.content || '' }));
})().catch((error) => { process.stderr.write(String(error && error.stack || error)); process.exit(1); });
"""
    try:
        completed = run_interruptibly(
            [node, "-e", script, root, module, str(path), url, json.dumps(options)],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
            encoding="utf-8",
            errors="replace",
        )
        if completed.returncode != 0:
            return None
        payload = json.loads(completed.stdout or "{}")
        if isinstance(payload, dict):
            return str(payload.get("contentMarkdown") or payload.get("content") or "") or None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    return None


def snapshot_acceleration_hint(url: str) -> str | None:
    from openmd.backends.loaded_chat import loaded_chat_site

    if loaded_chat_site(url):
        return "AI 对话优先读取当前已加载页面；范围仅限已加载分支。"
    host = (urlparse(url).hostname or "").lower()
    if host == "mp.weixin.qq.com" or host.endswith(".weixin.qq.com"):
        return "该 URL 有公众号平台路线，可以用平台路线完整抓取。"
    if host.endswith("xiaohongshu.com") or host.endswith("xhslink.com"):
        return "该 URL 有小红书平台路线，可以用平台路线完整抓取。"
    return None


def _html_title(html: str) -> str | None:
    match = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    if not match:
        return None
    value = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", match.group(1))).strip()
    return clean_page_title(value) or None


def clean_page_title(value: str | None) -> str:
    """Remove browser chrome noise from a page title before rendering or naming files."""
    title = re.sub(r"\s+", " ", str(value or "")).strip()
    title = _TITLE_COUNTER_PREFIX.sub("", title)
    title = _TITLE_SITE_SUFFIX.sub("", title).strip()
    return title


def _article_heading_title(html: str, page_title: str) -> str:
    """Prefer the article's sole <h1> when the tab title does not name it.

    Single-page readers keep one app-wide document title for every article;
    a specific title that already contains the heading is kept unchanged.
    """
    soup = BeautifulSoup(html, "html.parser")
    headings: list[str] = []
    # Readers often render the heading beside, not inside, <article>; use the
    # narrowest scope that has any <h1>, and only when it has exactly one.
    for scope in (soup.select_one("article"), soup.select_one("main, [role='main']"), soup.body or soup):
        if scope is None:
            continue
        headings = [re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip() for node in scope.find_all("h1")]
        headings = [value for value in headings if value]
        if headings:
            break
    page_title = clean_page_title(page_title)
    if len(headings) != 1 or len(headings[0]) > 300:
        return page_title
    heading = clean_page_title(headings[0])
    return page_title if heading.casefold() in page_title.casefold() else heading


def _failed(code: str, summary: str) -> TranscriptResult:
    return TranscriptResult(text="", status="failed", source_level="partial_extraction", backend=SNAPSHOT_BACKEND, error_code=code, error_summary=summary, source_notes=["browser snapshot provider returned a structured failure."])


def _snapshot_login_wall(html: str) -> bool:
    soup = BeautifulSoup(html, "html.parser")
    for node in soup.select("button, a, [role='button']"):
        if node.get_text(" ", strip=True).lower() in {"登录", "登录/注册", "log in", "login", "sign in"}:
            return True
    return False


def _first_snapshot_text(soup: BeautifulSoup, selectors: tuple[str, ...]) -> str | None:
    for selector in selectors:
        for node in soup.select(selector):
            if not isinstance(node, Tag) or node.get("aria-hidden") == "true":
                continue
            value = node.get("content") or node.get("datetime") or node.get_text(" ", strip=True)
            if value and str(value).strip():
                return str(value).strip()
    return None


def _public_asset_url(value: object) -> str | None:
    raw = str(value or "").strip()
    if not raw or raw.startswith(("data:", "blob:")):
        return None
    if raw.startswith("//"):
        raw = "https:" + raw
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    # Snapshot metadata keeps a stable public locator only. Signed/query
    # material is never persisted and no asset is downloaded here.
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _snapshot_assets(soup: BeautifulSoup, *, kind: str) -> list[str]:
    values: list[str] = []
    selectors = "img[src], img[data-src], img[data-original]" if kind == "image" else "video[src], video[data-src], video source[src], video source[data-src]"
    for node in soup.select(selectors):
        candidate = node.get("src") or node.get("data-src") or node.get("data-original")
        url = _public_asset_url(candidate)
        if url and url not in values:
            values.append(url)
    return values


# Parts of an open note that are not the note: comments, interaction bars,
# follow/share controls, the comment box and the feed behind a modal.
_XHS_NOISE = ", ".join((
    "script", "style", "noscript", ".comments-el", ".comments-container", ".comment-item",
    ".interact-container", ".engage-bar", ".interactions", ".input-box", ".note-follow",
    ".follow-button", ".share-wrapper", "button", "section.note-item", ".feeds-container",
))


def _xhs_chrome_asset(url: str) -> bool:
    """Avatars and site decoration are page chrome, not the note's media."""
    host = urlsplit(url).netloc.lower()
    return host.startswith("sns-avatar") or host.startswith("picasso-static") or host.startswith("fe-static")


def _extract_xiaohongshu_snapshot(html: str, item, path: Path) -> TranscriptResult:
    soup = BeautifulSoup(html, "html.parser")
    expected_id = str(item.raw_meta.get("snapshot_note_id") or "").strip()
    note_root = soup.select_one("[data-testid='note-detail'], #noteContainer, .note-detail")
    actual_node = note_root if note_root is not None and note_root.get("note-id") else soup.select_one("[note-id]")
    actual_id = str(actual_node.get("note-id") if actual_node is not None else "").strip()
    if expected_id and actual_id and expected_id != actual_id:
        return _failed("xiaohongshu_loaded_page_mismatch", "当前已加载页面不是请求的小红书笔记。请返回目标笔记后重新保存。")
    if note_root is None and expected_id:
        note_root = next((article for article in soup.select("article") if expected_id in str(article)), None)
    if note_root is None:
        linked_ids = set(re.findall(r"/(?:explore|discovery/item|item)/([A-Za-z0-9]+)", html))
        if expected_id and linked_ids and expected_id not in linked_ids:
            return _failed("xiaohongshu_loaded_page_mismatch", "当前已加载页面不是请求的小红书笔记。请返回目标笔记后重新保存。")
        if len(linked_ids) >= 3:
            return _failed("xiaohongshu_loaded_page_mismatch", "当前已加载页面是小红书推荐流，不是目标笔记。请返回目标笔记后重新保存。")
    canonical = soup.select_one("link[rel='canonical'][href], meta[property='og:url'][content]")
    canonical_id = re.search(r"/(?:explore|discovery/item|item)/([A-Za-z0-9]+)", str(canonical.get("href") or canonical.get("content"))) if canonical else None
    if expected_id and canonical_id and canonical_id.group(1) != expected_id:
        return _failed("xiaohongshu_loaded_page_mismatch", "当前已加载页面不是请求的小红书笔记。请返回目标笔记后重新保存。")
    # Everything below reads only the open note. A note opened from the feed is a
    # modal over feed cards that carry their own .title/.author, and the modal
    # holds comments with avatars, dates and like counts (round10c real sample).
    root = BeautifulSoup(str(note_root or soup.select_one("main") or soup.body or soup), "html.parser")
    for node in list(root.select(_XHS_NOISE)):
        node.decompose()
    title = _first_snapshot_text(root, (
        "#detail-title", "[data-testid='note-title']", ".note-content .title", ".title", "h1",
    )) or _first_snapshot_text(soup, ("meta[property='og:title']", "title")) or item.title or "小红书笔记"
    author = _first_snapshot_text(root, (
        "[data-testid='user-name']", "[data-testid='author-name']", ".author-wrapper .username",
        ".author .username", ".author-name", ".user-name", ".author",
    )) or item.author
    published_at = _first_snapshot_text(root, (
        "time[datetime]", "[data-publish-time]", "[data-published-at]", ".publish-time",
        ".note-content .date", ".bottom-container .date", ".date",
    )) or item.published_at
    media = root.select_one(".media-container, [data-testid='note-media'], .swiper") or root
    images = [url for url in _snapshot_assets(media, kind="image") if not _xhs_chrome_asset(url)]
    videos = _snapshot_assets(media, kind="video")
    content = root.select_one("#detail-desc, [data-testid='note-content'], .note-content .desc")
    body_source = content if content is not None else root
    tags: list[str] = []
    for node in body_source.select(".tag, #hash-tag, [data-testid*='tag'], a[href*='hashtag']"):
        value = node.get_text(" ", strip=True)
        if value and value not in tags:
            tags.append(value if value.startswith("#") else "#" + value)
    for value in body_source.get_text(" ", strip=True).split():
        if value.startswith("#") and value not in tags:
            tags.append(value.rstrip("，。！？.,"))

    body = BeautifulSoup(str(body_source), "html.parser")
    for node in list(body.select("script, style, noscript, img, video, source, .tag, #hash-tag, [data-testid*='tag'], a[href*='hashtag'], #detail-title, [data-testid='note-title'], [data-testid='user-name'], [data-testid='author-name'], .author-wrapper, .author, time, .publish-time, .date, .media-container, .swiper")):
        node.decompose()
    text = html_to_markdown(str(body)).strip()
    if text.startswith("# ") and text.splitlines()[0].strip().removeprefix("# ").strip() == title.strip():
        text = "\n".join(text.splitlines()[1:]).lstrip()
    sections = [text] if text else []
    if tags:
        sections.extend(["## 话题", "", *[f"- {tag}" for tag in tags]])
    if images:
        sections.extend(["## 图片", "", *[f"- {index}: {url}" for index, url in enumerate(images, start=1)]])
    if videos:
        sections.extend(["## 视频", "", *[f"- {index}: {url}" for index, url in enumerate(videos, start=1)]])
    if not sections:
        return _failed("snapshot_empty", "当前小红书笔记快照没有可保存的正文。")
    media_kind = "mixed" if videos and images else "video" if videos else "image_text" if images else "text"
    notes = [
        "小红书快照只读取用户当前已加载页面的可见 DOM，不发起站点请求。",
        "图片和视频仅保留去除查询参数后的公开地址，不下载媒体；图片 OCR 与视频转写未自动启用。",
    ]
    return TranscriptResult(
        text="\n\n".join(sections).rstrip() + "\n",
        status="verified" if text else "partial",
        source_level="browser_rendered",
        backend=SNAPSHOT_BACKEND,
        title=title,
        author=author,
        published_at=published_at,
        source_notes=notes,
        metadata={
            "captured_from": "browser_snapshot",
            "coverage": "visible_only",
            "snapshot_path": str(path),
            "snapshot_site": "xiaohongshu",
            "media_kind": media_kind,
            "images": images,
            "image_count": len(images),
            "video_urls": videos,
            "video_url": videos[0] if videos else None,
            "ocr_available": bool(images),
            "transcription_available": bool(videos),
        },
    )
