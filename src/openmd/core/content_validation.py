"""Shared checks that distinguish requested source content from metadata/shells."""

from __future__ import annotations

import html
import re
from dataclasses import replace

from .models import ContentItem, TranscriptResult
from .login import normalize_login_result


_ERROR_HEADING = re.compile(
    r"^(?:this site can[’']t (?:be reached|provide a secure connection)|"
    r"this site cannot (?:be reached|provide a secure connection)|"
    r"your connection is not private|your connection is not secure|"
    r"the connection was reset|the connection has timed out|"
    r"secure connection failed|server not found|"
    r"无法访问此网站|此网站无法提供安全连接|您的连接不是私密连接|"
    r"该网页无法正常运作)[.!。！]?$",
    re.IGNORECASE,
)
_ERROR_CODE = re.compile(r"^(?:net::)?(ERR_[A-Z0-9_]+)$")
_HTML_TAG = re.compile(r"</?[a-zA-Z][^>]*>")


def _code_spans(text: str) -> list[tuple[int, int]]:
    """Find literal fenced/inline code, including unclosed fenced blocks."""
    spans: list[tuple[int, int]] = []
    fence: str | None = None
    start = offset = 0
    for line in text.splitlines(keepends=True):
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if marker and fence is None:
            fence = marker[1]
            start = offset
        elif fence is not None:
            if marker and marker[1][0] == fence[0] and len(marker[1]) >= len(fence) and not line[marker.end():].strip():
                spans.append((start, offset + len(line)))
                fence = None
        else:
            spans.extend((offset + match.start(), offset + match.end()) for match in re.finditer(r"(?<!`)(`+)(?!`)[^\n]*?(?<!`)\1(?!`)", line))
        offset += len(line)
    if fence is not None:
        spans.append((start, len(text)))
    return spans


def _outside_code(text: str) -> str:
    for start, end in reversed(_code_spans(text)):
        text = text[:start] + "\n" * text[start:end].count("\n") + text[end:]
    return text


def html_tag_residue(text: str) -> tuple[int, int]:
    """Count HTML-tag lines and tags, excluding fenced and inline code."""
    outside = _outside_code(text)
    matches = [line for line in outside.splitlines() if _HTML_TAG.search(line)]
    return len(matches), sum(len(_HTML_TAG.findall(line)) for line in matches)


def html_to_markdown(text: str) -> str:
    # Protect literal code before asking the existing optional converter to
    # normalize HTML embedded in an otherwise Markdown document.
    protected: dict[str, str] = {}

    value = text
    for start, end in reversed(_code_spans(text)):
        key = f"OPENMDCODE{len(protected)}PLACEHOLDER"
        while key in text:
            key += "X"
        protected[key] = text[start:end]
        value = value[:start] + key + value[end:]
    try:
        import markdownify  # type: ignore[import-not-found]
        from bs4 import BeautifulSoup  # type: ignore[import-not-found]

        soup = BeautifulSoup(value, "html.parser")
        for hidden in soup.find_all(["script", "style", "noscript"]):
            hidden.decompose()
        for table in soup.find_all("table"):
            # Legacy articles use nested/one-cell tables for page layout.
            # Converting these to pipe tables collapses the article into one
            # enormous cell. Unwrap their own structure, retaining data tables.
            own = [node for node in table.find_all(["thead", "tbody", "tfoot", "tr", "td", "th"]) if node.find_parent("table") is table]
            cells = [node for node in own if node.name in {"td", "th"}]
            layout = (
                table.get("role") == "presentation"
                or (not table.find("th") and (table.find("table") is not None or len(cells) <= 1 or table.has_attr("width")))
            )
            if layout:
                for node in own:
                    if node.name in {"tr", "td"}:
                        node.insert_before(soup.new_string("\n\n"))
                        node.insert_after(soup.new_string("\n\n"))
                    node.unwrap()
                table.unwrap()

        converted = markdownify.markdownify(
            str(soup), heading_style="ATX", bullets="-",
            escape_asterisks=False, escape_underscores=False, escape_misc=False,
        )
    except ModuleNotFoundError:
        # The existing Discourse converter is a stdlib-only fallback. Its
        # unsupported/residual markup still goes through the same partial gate.
        from openmd.backends.discourse import _CookedMarkdown

        # The fallback understands data tables. Remove presentation wrappers
        # when no header cell identifies a real table.
        if re.search(r"<table\b", value, re.I) and not re.search(r"<th\b", value, re.I):
            value = re.sub(r"</?(?:table|thead|tbody|tfoot|tr|td)\b[^>]*>", "\n\n", value, flags=re.I)
        parser = _CookedMarkdown("")
        parser.feed(value)
        parser.close()
        converted = parser.markdown()
    for key, literal in protected.items():
        converted = converted.replace(key, literal)
    return converted.strip()


def _normalize_web_body(result: TranscriptResult, item: ContentItem | None) -> TranscriptResult:
    if item is None or item.platform not in {"web_article", "weixin"}:
        return result
    before_lines, _ = html_tag_residue(result.text)
    if before_lines:
        try:
            converted = html_to_markdown(result.text)
        except Exception:
            # Keep the original body for inspection but never bless an HTML
            # dump if an optional converter cannot handle it.
            converted = result.text
        after_lines, after_tags = html_tag_residue(converted)
        metadata = {**result.metadata, "html_tag_residue_lines": after_lines, "html_tag_residue_tags": after_tags}
        notes = [*result.source_notes, "Shared web content validation converted HTML to Markdown before output."]
        if after_lines:
            return replace(
                result,
                text=converted.rstrip() + "\n",
                status="partial",
                error_code="html_tag_residue",
                error_summary=f"HTML tag residue remains after conversion ({after_lines} lines).",
                source_notes=[*notes, "HTML readability gate kept status partial."],
                metadata=metadata,
                components=[replace(component, status="partial") if component.status == "verified" else component for component in result.components],
            )
        return replace(result, text=converted.rstrip() + "\n", source_notes=notes, metadata=metadata)
    return replace(result, metadata={**result.metadata, "html_tag_residue_lines": 0, "html_tag_residue_tags": 0})


def browser_error_page_reason(text: str, title: str | None = None) -> str | None:
    """Recognize browser diagnostics without rejecting articles quoting a code.

    Error headings are standalone lines near the beginning of browser screens.
    A bare error code is accepted as a failure only for a short diagnostic
    screen, so an ordinary technical article mentioning ERR_* remains text.
    """
    plain = html.unescape(re.sub(r"<[^>]+>", "\n", text))
    visible = _outside_code(plain)
    headings = [line.strip().strip("#*` \t").lower().rstrip(".! …") for line in visible.splitlines() if line.strip()][:8]
    page_title = str(title or "").strip().lower().rstrip(".! …")
    challenge_heading = re.compile(r"^(?:just a moment|checking your browser(?: before accessing.*)?|attention required(?:[\s!|\-–—:]+cloudflare)?|verification successful|verify (?:you are|that you are) human|performing security verification)$")
    # Titles and leading diagnostic headings identify the page itself. Ignore
    # code examples and ordinary articles that merely discuss these phrases.
    if challenge_heading.fullmatch(page_title) or any(
        challenge_heading.fullmatch(heading) or re.match(r"verification successful\.?\s+waiting for .+ to respond", heading)
        for heading in headings[:4]
    ):
        return "browser_challenge_page"
    zhihu_error = re.compile(r"你似乎来到了没有知识存在的荒原|4\s*秒后自动跳转至知乎首页|page error", re.I)
    if page_title in {"你似乎来到了没有知识存在的荒原", "nowsecure.nl"} or any(zhihu_error.search(line) for line in headings[:8]):
        return "browser_error_page" if "荒原" in " ".join(headings[:8]) or "page error" in " ".join(headings[:8]).lower() else "browser_challenge_page"
    if "nowsecure.nl" in page_title and any("nodriver" in line.lower() for line in headings[:6]):
        return "browser_challenge_page"
    if page_title in {"not found", "page not found", "404 not found"} or ("page not found" in headings[:5] and len(plain) < 1200):
        return "browser_network_error_page"
    if page_title == "example domain" and "this domain is for use in documentation examples" in visible.lower():
        return "browser_network_error_page"
    lines = [line.strip().strip("#*` ") for line in plain.splitlines() if line.strip()]
    headings = [*(lines[:8]), str(title or "").strip()]
    codes = [match.group(1) for line in lines[:12] if (match := _ERROR_CODE.fullmatch(line))]
    screen_hint = any(
        line.lower() in {"reload", "重新加载", "try running windows network diagnostics."}
        for line in lines
    )
    if len(plain) < 3000 and any(_ERROR_HEADING.fullmatch(line) for line in headings):
        if codes or screen_hint or len(lines) == 1:
            return "browser_network_error_page"
    if codes and len(plain) < 3000:
        if len(lines) == 1 or screen_hint:
            return codes[0]
    return None


_WEIXIN_ERROR_LINES = re.compile(
    r"(?:parameter error|参数错误|该内容已被发布者删除|此内容因违规无法查看|此内容被多人投诉.*|链接已过期|该公众号已迁移.*|"
    r"该账号已.*(?:注销|冻结|封禁).*|内容不存在)"
)
_WEIXIN_CHALLENGE_LINES = re.compile(r"(?:环境异常|当前环境异常.*完成验证后即可继续访问.*)")
_XIAOE_LOGIN_TITLE = re.compile(r"(?:验证码|扫码|手机)?登录(?:小鹅通|课程|账号)?", re.I)
_XIAOE_CHALLENGE = re.compile(r"(?:拖动.{0,30}拼图|拼图.{0,30}验证|滑块.{0,20}验证|完成验证后(?:继续|访问))", re.I)


def xiaoe_login_challenge_reason(text: str, title: str | None = None) -> bool:
    """Recognize Xiaoe's short login/challenge shells without matching course prose."""
    visible = html.unescape(re.sub(r"<[^>]+>", "\n", _outside_code(text)))
    compact = re.sub(r"\s+", "", visible)
    if len(compact) > 1400:
        return False
    title_auth = bool(_XIAOE_LOGIN_TITLE.fullmatch(str(title or "").strip()))
    login_words = bool(re.search(r"(?:验证码登录|扫码登录|请先登录|请登录|登录后查看)", compact, re.I))
    challenge = bool(_XIAOE_CHALLENGE.search(compact))
    return challenge and (title_auth or login_words)


def weixin_error_page_reason(text: str) -> str | None:
    """Recognize mp.weixin.qq.com status screens (deleted/expired/invalid/verify).

    These screens are short and lead with a standalone status line; an article
    that merely discusses such a phrase has body text around it.
    """
    visible = _outside_code(text)
    lines = [line.strip().strip("#*` ").rstrip("。.!！") for line in visible.splitlines() if line.strip()]
    # A status screen shows the phrase as its own short line near the top; an
    # article that quotes or explains the phrase keeps it inside sentences.
    if any(line in {"参数错误", "内容不存在", "该内容已被发布者删除"} for line in lines[:8]) and len(re.sub(r"\s+", "", visible)) < 1200:
        return "error"
    if not lines or len(re.sub(r"\s+", "", text)) > 600:
        return None
    for line in lines[:3]:
        if _WEIXIN_CHALLENGE_LINES.fullmatch(line):
            return "challenge"
        if _WEIXIN_ERROR_LINES.fullmatch(line.lower() if line.isascii() else line):
            return "error"
    return None


# Single-line only: a stray "[" in prose must not swallow paragraphs up to a
# later "](...)".
_MD_IMAGE = re.compile(r"!\[[^\]\n]*\]\([^)\n]*\)")
_MD_LINK = re.compile(r"\[([^\]\n]*)\]\([^)\n]*\)")


def navigation_shell_reason(text: str) -> str | None:
    """Recognize a generic web extraction that kept only site chrome.

    Soft-404 and index pages extract as images, spacer GIFs and a row of
    links. Both signals are required: almost no prose outside links and
    headings, and a structure dominated by links/images. A short genuine post
    has prose; a link-list article has many links but also has prose.
    """
    visible = _outside_code(text)
    images = len(_MD_IMAGE.findall(visible))
    without_images = _MD_IMAGE.sub("", visible)
    links = len(_MD_LINK.findall(without_images))
    prose_lines = []
    for line in _MD_LINK.sub("", without_images).splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or re.fullmatch(r"[-|:\s*_>]+", stripped):
            continue
        prose_lines.append(stripped)
    # Bare URLs (e.g. an extractor's own "原文链接: <url>" line) are not prose.
    # Join with newlines so a URL cannot run into the next CJK line.
    prose = re.sub(r"https?://\S+|[\s|*_>#\-:：©]+", "", "\n".join(prose_lines))
    if images + links >= 3 and len(prose) < 60:
        return "navigation_shell"
    return None


_LOGIN_PROMPT = re.compile(
    r"^(?:(?:please\s+)?(?:sign[ -]?in|log[ -]?in|register|sign[ -]?up)"
    r"(?:\s+(?:to|with|using|for|or|your|an?)[^.!?。]{0,90})?[.!。！]?|"
    r"(?:请先?|请)?(?:扫码|扫描二维码)?登录(?:后(?:查看|阅读|继续|访问)(?:完整|全部)?(?:内容|正文|文章)?|以继续|账号|帐号)?[。！]?|"
    r"(?:注册|创建)(?:账号|帐号|账户)|登录\s*[|/或]\s*注册)$", re.I,
)
_LOGIN_UI = re.compile(
    r"^(?:username(?: or email address)?|email(?: address)?|password|forgot (?:your )?password\??|"
    r"remember me|keep me (?:signed|logged) in|create (?:an? )?(?:account|passkey)|"
    r"(?:continue|sign in|log in) with .{1,40}|(?:new to .{1,40}|don't have an account)\??|"
    r"用户名|密码|手机号|验证码|忘记密码|记住我|扫码登录|短信登录|登录即表示.{0,80})[：:]?$", re.I,
)


def login_wall_reason(text: str, title: str | None = None) -> bool:
    """Reject short account screens; references in prose/code remain articles."""
    value = _outside_code(text)
    if re.search(r"<form\b", value, re.I):
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(value, "html.parser")
        for hidden in soup.find_all(["script", "style", "noscript", "nav", "footer"]):
            hidden.decompose()
        visible = soup.get_text("\n", strip=True)
        for form in soup.find_all("form"):
            password = form.find("input", attrs={"type": re.compile(r"^password$", re.I)})
            if password is not None:
                # Require the form to dominate; short articles can also embed
                # real demonstration controls outside fenced code.
                form_text = form.get_text(" ", strip=True)
                outside = visible.replace(form.get_text("\n", strip=True), "")
                if len(re.sub(r"\s+", "", outside)) < 180 and len(visible) < 2200 and len(form_text) >= len(visible) * 0.6:
                    return True
        value = visible
    plain = html.unescape(re.sub(r"<[^>]+>", "\n", value))
    plain = _MD_LINK.sub(r"\1", _MD_IMAGE.sub("", plain))
    lines = [re.sub(r"^[#>*\-\s]+|[*\s]+$", "", line).strip() for line in plain.splitlines() if line.strip()]
    if not lines or len(plain) > 2200:
        return False
    prompts = sum(bool(_LOGIN_PROMPT.fullmatch(line)) for line in lines)
    ui = sum(bool(_LOGIN_UI.fullmatch(line)) for line in lines)
    title_auth = bool(re.fullmatch(r"(?:sign[ -]?in|log[ -]?in|login|register|sign[ -]?up)(?:\s+(?:to|[|–—-])\s*.{1,60})?|登录(?:[|\s·-].{0,40})?|注册", str(title or "").strip(), re.I))
    if any(len(line) > 180 for line in lines):
        return False
    return bool((prompts and (prompts + ui) >= len(lines) * 0.6) or (title_auth and ui >= 2))


def validate_extraction_content(
    result: TranscriptResult, item: ContentItem | None = None, *, target: str = "content"
) -> TranscriptResult:
    """Validate content before the executor decides whether to stop fallback.

    Engine providers supply structural evidence, rather than relying on a
    minimum length or filtering ordinary words from legitimate short posts.
    Explicit metadata requests are separate from the default content target.
    """
    if result.status not in {"verified", "partial"}:
        return normalize_login_result(result, item)
    if item is not None and item.platform == "xiaoe" and xiaoe_login_challenge_reason(result.text, result.title):
        rejected = _reject_content(
            result,
            "login_required",
            "请先在浏览器登录小鹅通、打开课程页，再点 Openmd 扩展。",
            failure_class="user_resolvable",
        )
        normalized = normalize_login_result(rejected, item)
        return replace(normalized, error_summary="请先在浏览器登录小鹅通、打开课程页，再点 Openmd 扩展。")
    if item is not None and item.source_type in {"article", "mixed"} and item.platform not in {"pdf", "office", "screenshot", "ai_chat"} and login_wall_reason(result.text, result.title):
        rejected = _reject_content(result, "login_wall", "网页返回登录墙，未取得目标正文。", failure_class="user_resolvable")
        return normalize_login_result(rejected, item)
    result = _normalize_web_body(result, item)
    reason = browser_error_page_reason(result.text, result.title)
    if reason is not None:
        challenge = reason == "browser_challenge_page"
        return _reject_content(
            result, "browser_challenge_page" if challenge else "browser_error_page",
            "被站点验证页拦截，未取得目标正文。" if challenge else f"Browser returned a network/error page instead of source content ({reason}).",
            failure_class="user_resolvable" if challenge else "runtime_transient",
        )
    if item is not None and item.platform == "weixin" and (weixin := weixin_error_page_reason(result.text)):
        challenge = weixin == "challenge"
        return _reject_content(
            result, "browser_challenge_page" if challenge else "weixin_error_page",
            "公众号要求完成环境验证，未取得正文；在浏览器里打开链接完成验证后重试。" if challenge
            else "公众号返回了错误页，没有正文；链接可能失效、已删除或已过期。",
            failure_class="user_resolvable" if challenge else "source_unavailable",
        )
    if item is not None:
        target = str(item.raw_meta.get("extraction_target") or target)
    if target == "metadata":
        return result
    metadata = result.metadata
    if metadata.get("metadata_only") is True or metadata.get("content_kind") in {"metadata", "description"}:
        return _reject_content(result, "content_metadata_only", "The backend produced metadata rather than the requested source content.")
    if metadata.get("page_shell") is True or metadata.get("content_kind") in {"shell", "navigation"}:
        return _reject_content(result, "content_shell", "The backend produced a page shell rather than the requested source content.")
    if item is not None and item.platform in {"web_article", "page_snapshot"} and navigation_shell_reason(result.text):
        return _reject_content(result, "content_navigation_shell", "页面只有导航或图片，没有正文；链接可能不存在或需要登录。")
    if not result.text.strip():
        # OCR document routes intentionally preserve terminal gaps for review.
        if item is not None and item.platform in {"screenshot", "pdf", "office"} and metadata.get("terminal_partial") is True and result.status == "partial":
            return result
        return _reject_content(result, "content_empty", "The backend did not produce source content.")
    engine = metadata.get("engine") or metadata.get("site_engine")
    if engine is None and item is not None:
        engine = item.raw_meta.get("engine") or item.raw_meta.get("site_engine")
    if str(engine or "").lower() == "discourse":
        post_id = metadata.get("post_id")
        target_post_id = metadata.get("target_post_id")
        if not post_id or not target_post_id or str(post_id) != str(target_post_id) or metadata.get("post_body_present") is not True:
            return _reject_content(result, "discourse_post_body_missing", "The detected Discourse result lacks the requested post ID and body.")
    # Detail may identify a /video/ URL as an image post after the original
    # item was resolved. Accept that author's text only with provider evidence;
    # an explicit transcript request still requires actual transcript sources.
    image_post_body = (
        item is not None and item.platform == "douyin"
        and result.backend == "douyin.post-text"
        and result.source_level == "native_text"
        and metadata.get("media_kind") == "image_text"
        and metadata.get("transcript_method") == "image_post_text"
        and metadata.get("content_kind") == "author_text"
    )
    if target == "transcript" or (item is not None and item.platform == "douyin" and item.source_type == "video" and not image_post_body):
        if result.source_level not in {"native_caption", "auto_caption", "platform_transcript", "asr", "media_asr", "live_capture_asr", "ocr"}:
            return _reject_content(result, "transcript_not_produced", "The backend did not produce captions, ASR, or identified OCR text for the video.")
    return result


def _reject_content(
    result: TranscriptResult, code: str, summary: str, *, failure_class: str = "source_unavailable"
) -> TranscriptResult:
    metadata = {key: value for key, value in result.metadata.items() if key != "terminal_partial"}
    return replace(
        result,
        text="",
        status="failed",
        source_level="partial_extraction",
        components=[],
        error_code=code,
        error_summary=summary,
        source_notes=[*result.source_notes, "Unusable extraction body was rejected before output."],
        metadata={**metadata, "failure_class": failure_class},
    )
