"""Read an already loaded AI page. No navigation, browser API, or network.

Selectors/role rules ported from OpenCLI 2.0.3 (Apache-2.0):
clis/{chatgpt,grok,deepseek,doubao,gemini,qwen}/detail.js and utils.js;
clis/yuanbao/detail.js and shared.js. See docs/subsystems/loaded-chat.md.
Both native extension snapshots and agent HTML use this parser.
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import re
from typing import Any
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup, Tag

from openmd.core.content_validation import html_to_markdown
from openmd.core.chat_title import conversation_title
from openmd.core.login import normalize_login_result
from openmd.core.models import ContentItem, TranscriptResult, TurnBlock, normalize_turn_role


SELECTORS = {
    "chatgpt": '[data-message-author-role], [data-author], [data-user-message-bubble], [data-testid="user-message"], article[data-testid*="conversation-turn"], [data-chatgpt-search-unit-key]',
    "grok": '[data-testid="user-message"], [data-testid="assistant-message"]',
    "deepseek": '.ds-message',
    "doubao": '[data-testid="message-list"] [data-testid="union_message"]',
    "gemini": 'user-query, model-response, .query-text, .model-response-text',
    "qwen": '[data-chat-question-wrap], [data-chat-answers-wrap]',
    "yuanbao": '.agent-chat__list__item--human, .agent-chat__list__item--ai',
}
HOSTS = {
    "chatgpt.com": "chatgpt", "chat.openai.com": "chatgpt", "grok.com": "grok",
    "chat.deepseek.com": "deepseek", "doubao.com": "doubao", "www.doubao.com": "doubao",
    "gemini.google.com": "gemini", "www.qianwen.com": "qwen", "qianwen.com": "qwen",
    "qianwen.aliyun.com": "qwen", "tongyi.aliyun.com": "qwen", "yuanbao.tencent.com": "yuanbao",
}
CHATGPT_USER_CONTAINERS = '[data-message-author-role="user"], [data-author="user"], [data-user-message-bubble], [data-chatgpt-search-unit-key$=":user"], [data-content-search-unit-key$=":user"], [data-testid="user-message"], [data-conversation-role="user"]'
CHATGPT_USER_KEYS = '[data-chatgpt-search-unit-key$=":user"], [data-content-search-unit-key$=":user"]'
CHATGPT_TURN_ANCHORS = '[data-turn-key], [data-content-search-turn-key], [data-conversation-turn-id], [data-message-id], [data-turn-id], article[data-testid*="conversation-turn"]'
EMBEDDED_IMAGE_LIMIT = 8 * 1024  # UTF-8 bytes of the inline URI, before decoding.
# Exact structural signals only. Site-specific variants require live acceptance;
# prose such as "Thought for ..." is never enough to discard answer text.
THINKING_SELECTORS = {
    "chatgpt": '[data-testid="reasoning-summary"], .reasoning-summary',
    "deepseek": '.ds-markdown--think, .ds-thinking-content',
    "gemini": 'model-thoughts, .thoughts-container, .thoughts-content',
    "doubao": '[data-testid="thinking_content"], [data-testid="think_content"]',
    "qwen": '[data-chat-thinking-wrap], .qwen-thinking',
    "yuanbao": '.hyc-component-reasoner, .hyc-content-think, .agent-chat__thinking, .hyc-component-deepsearch-cot__think',
}
COMMON_THINKING_SELECTOR = '.thinking-container, [data-message-type="reasoning"], [data-testid="reasoning"], [data-testid="thinking"]'
DOUBAO_STATUS_TEXT = re.compile(
    r'^(?:这个问题我需要花点时间来处理|正在(?:思考|分析|搜索|整理|生成)|正在(?:进行)?(?:深度)?研究|'
    r'(?:已开始|开始进行)(?:深度)?研究|深度研究(?:中|已开始)|请稍候)(?:[。.!！…]|，请稍候)?$',
    re.I,
)


def loaded_chat_site(url: str) -> str | None:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"}:
        return None
    return HOSTS.get((parsed.hostname or "").lower())


def loaded_snapshot_site(url: str) -> str | None:
    """Return a platform whose currently loaded DOM has a bounded parser."""
    host = (urlsplit(url).hostname or "").lower()
    if host == "xiaohongshu.com" or host.endswith(".xiaohongshu.com") or host == "xhslink.com" or host.endswith(".xhslink.com"):
        return "xiaohongshu"
    if host == "douyin.com" or host.endswith(".douyin.com") or host == "iesdouyin.com" or host.endswith(".iesdouyin.com"):
        return "douyin"
    return None


def snapshot_item(
    path: Path,
    url: str,
    *,
    capture_meta: dict[str, Any] | None = None,
) -> ContentItem:
    """Use detail's exact identity, with a local-only snapshot extraction route."""
    site = loaded_chat_site(url)
    metadata = {"snapshot_path": str(path), "captured_from": "browser_snapshot", "coverage": "visible_only"}
    if isinstance(capture_meta, dict):
        metadata["capture"] = dict(capture_meta)
        if capture_meta.get("coverage") in {"full_page", "scrolled_full", "visible_only"}:
            metadata["coverage"] = capture_meta["coverage"]
    if site:
        from openmd.adapters.ai_chat import AiChatAdapter
        from openmd.backends.ai_chat import ai_chat_backend_for_site

        item = AiChatAdapter("ai_chat", ai_chat_backend_for_site(site)).resolve_items(url)[0]
        return replace(item, raw_meta={**item.raw_meta, **metadata, "chat_site": site})
    loaded_site = loaded_snapshot_site(url)
    if loaded_site in {"xiaohongshu", "douyin"}:
        # Resolve identity through the normal adapter without invoking a
        # provider. This keeps snapshot and interface routes on one source_id.
        from openmd.adapters.registry import create_default_registry

        item = create_default_registry().adapter_for(url).resolve_items(url)[0]
        note_match = re.search(r"/(?:explore|discovery/item|item)/([A-Za-z0-9]+)", urlsplit(url).path)
        extra = {"snapshot_site": loaded_site}
        if note_match:
            extra["snapshot_note_id"] = note_match.group(1)
        return replace(item, raw_meta={**item.raw_meta, **metadata, **extra})
    return ContentItem(platform="page_snapshot", input_type="single", url=url, source_type="article", raw_meta=metadata)


def _visible(node: Tag) -> bool:
    for parent in [node, *node.parents]:
        if not isinstance(parent, Tag):
            continue
        style = re.sub(r"\s+", "", str(parent.get("style", ""))).lower()
        if parent.has_attr("hidden") or parent.get("aria-hidden") == "true" or parent.get("data-openmd-hidden") == "true":
            return False
        if "display:none" in style or "visibility:hidden" in style:
            return False
    return True


def _outermost(nodes: list[Tag]) -> list[Tag]:
    identities = {id(node) for node in nodes}
    return [node for node in nodes if not any(id(parent) in identities for parent in node.parents)]


def _chatgpt_user_turn_count(soup: BeautifulSoup) -> int:
    """Count semantic user turns, collapsing markers from one DOM turn.

    ChatGPT currently renders one user turn with a role heading, a search-unit
    wrapper, a content-search wrapper, and a bubble.  These markers are often
    siblings, so an outermost-node count treats one turn as several.  Prefer a
    shared ``*:user`` search key; fall back to the nearest explicit turn anchor
    for older layouts.  A bare marker remains independent evidence by itself.
    """
    markers = _outermost([node for node in soup.select(CHATGPT_USER_CONTAINERS) if _visible(node)])
    groups: set[tuple[str, object]] = set()
    for node in markers:
        key: tuple[str, object] | None = None
        for ancestor in [node, *node.parents]:
            keyed = [candidate for candidate in ancestor.select(CHATGPT_USER_KEYS) if _visible(candidate)]
            if ancestor in soup.select(CHATGPT_USER_KEYS) and _visible(ancestor):
                keyed.append(ancestor)
            values = {str(candidate.get("data-chatgpt-search-unit-key") or candidate.get("data-content-search-unit-key")) for candidate in keyed}
            if len(values) == 1:
                key = ("search", next(iter(values)))
                break
        if key is None:
            for ancestor in [node, *node.parents]:
                if not isinstance(ancestor, Tag):
                    continue
                if ancestor in soup.select(CHATGPT_TURN_ANCHORS):
                    key = ("anchor", id(ancestor))
                    break
        groups.add(key or ("marker", id(node)))
    return len(groups)


def _role_content(site: str, node: Tag) -> tuple[str, Tag]:
    if site == "chatgpt":
        author = node if node.has_attr("data-message-author-role") or node.has_attr("data-author") else node.select_one('[data-message-author-role], [data-author], [data-user-message-bubble]')
        role = str((author or node).get("data-message-author-role") or (author or node).get("data-author") or "")
        if not role:
            hints = " ".join(str(node.get(key) or "") for key in ("data-chatgpt-search-unit-key", "data-testid", "aria-label"))
            role = "user" if node.has_attr("data-user-message-bubble") or (author is not None and author.has_attr("data-user-message-bubble")) else ("assistant" if re.search("assistant|chatgpt", hints, re.I) else ("user" if re.search("user|you", hints, re.I) else "unknown"))
        content = node.select_one('[data-markdown-copy="contents"], [data-markdown-text-style="assistant-message"], [data-user-message-bubble] [data-search-result-target]')
        content = content if content is not None else node.select_one('.markdown')
        return role.lower(), content if content is not None else (author if author is not None else node)
    if site == "grok":
        return ("assistant" if node.get("data-testid") == "assistant-message" else "user"), node
    if site == "deepseek":
        # The upstream role heuristic is based on the bubble's class count.
        role = str(node.get("data-message-author-role") or node.get("data-role") or "")
        return role or ("user" if len(node.get("class", [])) > 2 else "assistant"), node
    if site == "doubao":
        role = "user" if node.select_one('[data-testid="send_message"]') else ("assistant" if node.select_one('[data-testid="receive_message"]') else "unknown")
        content = node.select_one('[data-testid="message_text_content"]')
        return role, content if content is not None else node
    if site == "gemini":
        hints = node.name + " " + " ".join(str(node.get(key) or "") for key in ("class", "data-message-author-role", "data-role", "aria-label"))
        role = "user" if re.search("user|query", hints, re.I) else ("assistant" if re.search("assistant|model|response|gemini", hints, re.I) else "unknown")
        content = node.select_one('.query-text' if role == "user" else '.model-response-text')
        return role, content if content is not None else node
    if site == "qwen":
        role = "assistant" if node.has_attr("data-chat-answers-wrap") else "user"
        content = node.select_one('#qk-markdown-react, [class*="markdown"]') if role == "assistant" else None
        return role, content if content is not None else node
    role = "assistant" if "agent-chat__list__item--ai" in node.get("class", []) else "user"
    selectors = ('.hyc-content-md-done', '.hyc-content-md', '.agent-chat__speech-text', '.agent-chat__bubble__content') if role == "assistant" else ('.hyc-component-text .hyc-content-text', '.hyc-content-text', '.agent-chat__bubble__content')
    for selector in selectors:
        content = node.select_one(selector)
        if content is not None:
            return role, content
    return role, node


def _markdown(node: Tag, stats: dict[str, int] | None = None) -> str:
    stats = stats if stats is not None else {}
    body = BeautifulSoup(str(node), "html.parser")
    for child in list(body.select('script, style, noscript, button, .sr-only, .cdk-visually-hidden, [data-prompt-link-href]')):
        if child.parent is not None:
            child.decompose()
    literals = {}

    def literal(child: Tag, text: str, *, block: bool = False) -> None:
        marker = f"OPENMDLOADEDLITERAL{len(literals)}END"
        while marker in str(body):
            marker += "X"
        literals[marker] = text
        child.replace_with(body.new_string(("\n\n" if block else "") + marker + ("\n\n" if block else "")))

    # Read semantic TeX before removing accessibility duplicates. Never flatten
    # fractions/superscripts through a generic HTML-to-text converter.
    for formula in _outermost(list(body.select('.katex-display, .katex, mjx-container, math'))):
        if not _visible(formula) or formula.find_parent(["pre", "code"]) is not None:
            continue
        annotation = formula.select_one('annotation[encoding="application/x-tex"]')
        block = "katex-display" in formula.get("class", []) or formula.get("display") in {"block", "true"}
        if annotation is not None and annotation.get_text().strip():
            tex = annotation.get_text().strip()
            text = "$$\n" + tex + "\n$$" if block else "$" + tex + "$"
        else:
            math = formula if formula.name == "math" else formula.find("math")
            text = str(math) if math is not None else formula.get_text(" ", strip=True)
            stats["math_fallback_count"] = stats.get("math_fallback_count", 0) + 1
        literal(formula, text, block=block)
    for child in list(body.find_all(True)):
        if child.parent is not None and not _visible(child):
            child.decompose()
    for image in body.find_all("img"):
        source = str(image.get("src") or "")
        if not source.lower().startswith("data:"):
            continue
        if len(source.encode("utf-8")) > EMBEDDED_IMAGE_LIMIT:
            kind = source.split(";", 1)[0].split(",", 1)[0].removeprefix("data:").removeprefix("image/").upper()
            kind = "SVG" if kind == "SVG+XML" else kind
            alt = str(image.get("alt") or "").strip()
            literal(image, f"[图片：内嵌 {kind}，已省略]" + (f" {alt}" if alt else ""))
            stats["embedded_image_omitted_count"] = stats.get("embedded_image_omitted_count", 0) + 1
        else:
            width, height = str(image.get("width", "")), str(image.get("height", ""))
            tiny = width.isdigit() and height.isdigit() and int(width) <= 32 and int(height) <= 32
            decorative = image.get("role") == "presentation" or "icon" in image.get("class", [])
            if tiny or decorative:
                image.decompose()
                stats["embedded_icon_removed_count"] = stats.get("embedded_icon_removed_count", 0) + 1
    # Preserve every code byte after HTML entity decoding, including indentation
    # and blank lines. General converters otherwise strip language/whitespace.
    for pre in body.find_all("pre"):
        code = pre.find("code")
        code = code if code is not None else pre
        text = code.get_text()
        classes = " ".join(code.get("class", []))
        language = re.search(r"(?:language|lang)-([\w+-]+)", classes)
        fence = "`" * max(3, max((len(m.group()) + 1 for m in re.finditer(r"`+", text)), default=3))
        literal(pre, fence + (language.group(1) if language else "") + "\n" + text + ("" if text.endswith("\n") else "\n") + fence, block=True)
    text = html_to_markdown(str(body))
    for marker, literal in literals.items():
        text = text.replace(marker, literal)
    return text.strip()


def _generating(soup: BeautifulSoup) -> bool:
    for node in soup.select('button, [role="button"], [data-testid]'):
        if not _visible(node) or node.has_attr("disabled") or node.get("aria-disabled") == "true":
            continue
        label = " ".join(str(node.get(key) or "") for key in ("aria-label", "title", "data-testid"))
        if re.search(r"stop[- _]?(?:generat|response|stream|answer)|停止(?:生成|回答|响应|输出)|中止(?:生成|回答)", label, re.I):
            return True
        if node.name == "button" or node.get("role") == "button":
            if re.fullmatch(r"stop(?: generating| response| answering)?|停止(?:生成|回答|响应|输出)?", node.get_text(" ", strip=True), re.I):
                return True
    return False


def _turn_markdown(site: str, node: Tag, role: str, stats: dict[str, int], *, include_thinking: bool) -> str:
    if role != "assistant":
        return _markdown(_role_content(site, node)[1], stats)
    # Work at the turn root: thinking can sit beside the final markdown region.
    clone = BeautifulSoup(str(node), "html.parser").find()
    thoughts = []
    selector = COMMON_THINKING_SELECTOR + ", " + THINKING_SELECTORS.get(site, COMMON_THINKING_SELECTOR)
    for thought in _outermost(list(clone.select(selector))):
        if _visible(thought) and thought.get_text(strip=True):
            key = "thinking_included_count" if include_thinking else "thinking_omitted_count"
            stats[key] += 1
            if include_thinking:
                thoughts.append(_markdown(thought, stats))
        thought.decompose()
    answer = _markdown(_role_content(site, clone)[1], stats)
    if thoughts:
        return "#### 思考过程\n\n" + "\n\n".join(thoughts) + ("\n\n#### 最终回答\n\n" + answer if answer else "")
    return answer


def _doubao_status_text(site: str, node: Tag, role: str) -> str | None:
    """Return a short Doubao progress bubble, which is not a conversation turn.

    Doubao's loaded page can expose a progress bubble as a second
    ``union_message`` with ``receive_message`` and ``message_text_content``.
    The managed OpenCLI selectors identify the surrounding message root but do
    not distinguish this progress signal from a final answer, so match only a
    complete, known progress phrase.  Ordinary answer prose is left untouched.
    """
    if site != "doubao" or role != "assistant":
        return None
    _, content = _role_content(site, node)
    text = re.sub(r"\s+", " ", content.get_text(" ", strip=True)).strip()
    if not text or len(text) > 160:
        return None
    if DOUBAO_STATUS_TEXT.fullmatch(text):
        return text
    return None


def _prepend_thinking_sections(text: str, sections: list[str]) -> str:
    if not sections:
        return text
    joined = "\n\n".join(section for section in sections if section)
    if not joined:
        return text
    prefix = "#### 思考过程\n\n"
    if text.startswith(prefix):
        body = text[len(prefix):]
        final_marker = "\n\n#### 最终回答\n\n"
        if final_marker in body:
            thought, answer = body.split(final_marker, 1)
            return prefix + joined + "\n\n" + thought + final_marker + answer
        return prefix + joined + "\n\n" + body
    return prefix + joined + ("\n\n#### 最终回答\n\n" + text if text else "")


def _session_title(soup: BeautifulSoup, url: str) -> str | None:
    target = urlsplit(url)

    def matches_current_link(node: Tag) -> bool:
        link = urlsplit(urljoin(url, str(node.get("href"))))
        return (link.hostname, link.path.rstrip("/")) == (target.hostname, target.path.rstrip("/"))

    selectors = '[data-conversation-title], [data-testid="conversation-title"], [data-testid="chat-title"], .conversation-title, .chat-title, header h1, header h2, nav [aria-current="page"], aside [aria-current="page"], nav [aria-selected="true"], aside [aria-selected="true"]'
    for node in soup.select(selectors):
        if node.find_parent(["nav", "aside"]) is not None:
            chain = [node, *[parent for parent in node.parents if isinstance(parent, Tag)]]
            if not any(parent.get("aria-current") == "page" or parent.get("aria-selected") == "true" or (parent.name == "a" and parent.has_attr("href") and matches_current_link(parent)) for parent in chain):
                continue
        if _visible(node):
            title = str(node.get("data-conversation-title") or node.get_text(" ", strip=True))
            if title:
                return title
    for node in soup.select('nav a[href], aside a[href]'):
        if _visible(node) and matches_current_link(node):
            title = node.get_text(" ", strip=True)
            if title:
                return title
    return None


def extract_loaded_chat(html: str, item: ContentItem, *, backend: str) -> TranscriptResult:
    site = loaded_chat_site(item.url)
    soup = BeautifulSoup(html, "html.parser")
    roots = _outermost([node for node in soup.select(SELECTORS[site]) if _visible(node)])
    independent_count = len(roots)
    # Separate evidence sweep: a missing role/content selector must not certify
    # an assistant-only export when user bubbles are present in the page.
    user_count = _chatgpt_user_turn_count(soup) if site == "chatgpt" else None
    if site == "gemini" and not roots:
        # Upstream's legacy layout sweep lacks an independent turn container.
        roots = _outermost([node for node in soup.select('[data-testid*="message"], [data-test-id*="message"], [class*="message"], [class*="conversation-turn"], [class*="query-text"], [class*="response-text"]') if _visible(node)])
        independent_count = None
    login_path = re.search(r"/(?:auth|login|signin)(?:/|$)", urlsplit(item.url).path, re.I)
    login_form = any(_visible(node) for node in soup.select('input[type="password"], form[action*="login"], form[action*="signin"]'))
    login_cta = any(_visible(node) and re.fullmatch(r"log in|login|sign in|登录|立即登录|登录/注册", node.get_text(" ", strip=True), re.I) for node in soup.select('button, a, [role="button"]'))
    if login_path or login_form or (not roots and login_cta):
        return normalize_login_result(TranscriptResult(text="", status="failed", source_level="partial_extraction", backend=backend, error_code="login_required"), item)
    turns = []
    stats: dict[str, int] = {"math_fallback_count": 0, "embedded_image_omitted_count": 0, "embedded_icon_removed_count": 0, "thinking_omitted_count": 0, "thinking_included_count": 0, "status_omitted_count": 0, "status_included_count": 0}
    pending_statuses: list[str] = []
    for node in roots:
        role, content = _role_content(site, node)
        role = normalize_turn_role(role)
        if role not in {"user", "assistant", "system", "tool"}:
            continue
        status_text = _doubao_status_text(site, node, role)
        if status_text is not None:
            key = "status_included_count" if item.raw_meta.get("include_thinking") is True else "status_omitted_count"
            stats[key] += 1
            if item.raw_meta.get("include_thinking") is True:
                pending_statuses.append(status_text)
            continue
        text = _turn_markdown(site, node, role, stats, include_thinking=item.raw_meta.get("include_thinking") is True)
        if role == "assistant" and pending_statuses:
            text = _prepend_thinking_sections(text, pending_statuses)
            pending_statuses.clear()
        if not text:
            continue
        turns.append(TurnBlock(role=role, text=text, message_id=node.get("data-message-id") or node.get("data-conv-id")))
    if pending_statuses and turns and turns[-1].role == "assistant":
        turns[-1] = replace(turns[-1], text=_prepend_thinking_sections(turns[-1].text, pending_statuses))
    if not turns:
        return TranscriptResult(text="", status="failed", source_level="partial_extraction", backend=backend, error_code="ai_chat_empty", error_summary="当前页没有可读取的对话轮次；请先打开并加载会话。")
    generating = _generating(soup)
    user_unavailable = site == "chatgpt" and user_count == 0 and not any(turn.role == "user" for turn in turns) and any(turn.role == "assistant" for turn in turns)
    title = soup.title.get_text(strip=True) if soup.title else None
    return TranscriptResult(
        text="\n\n".join(turn.text for turn in turns) + "\n", blocks=turns,
        status="partial" if generating or user_unavailable else "verified", source_level="browser_rendered", backend=backend,
        title=conversation_title(site, item.title or title, turns, session_title=_session_title(soup, item.url)),
        error_code="ai_chat_not_stable" if generating else ("ai_chat_user_turn_unavailable" if user_unavailable else None),
        error_summary="当前页仍在生成，请等待完成后重新读取。" if generating else ("当前 ChatGPT 页面未发现可读取的用户轮，无法确认问答完整性。" if user_unavailable else None),
        metadata={"chat_site": site, "captured_from": "browser_snapshot",
                  "coverage": (item.raw_meta.get("capture") or {}).get("coverage") or "visible_branch",
                  **({"capture": item.raw_meta["capture"]} if isinstance(item.raw_meta.get("capture"), dict) else {}),
                  "turn_count": len(turns), "independent_turn_count": independent_count, "independent_user_turn_count": user_count, "generating": generating,
                  "conversation_user_evidence_unavailable": user_unavailable,
                  "selector_source": f"OpenCLI 2.0.3 clis/{site}/detail.js",
                  "assistant_turn_count": sum(turn.role == "assistant" for turn in turns), **stats},
        source_notes=["读取当前已加载页面；仅覆盖 DOM 中已加载的分支，未验证未加载的历史消息。",
                      *(["当前 ChatGPT 页面只发现助手轮，未发现可读取的用户轮；无法确认问答完整性。"] if user_unavailable else []),
                      *([f"公式可能失真：{stats['math_fallback_count']} 处未找到 TeX annotation，保留 MathML 或可见文本。"] if stats["math_fallback_count"] else []),
                      *([f"已省略内嵌图片：{stats['embedded_image_omitted_count']} 张；保留替代文本和图注。"] if stats["embedded_image_omitted_count"] else []),
                      *([f"已删除内嵌小图标：{stats['embedded_icon_removed_count']} 个。"] if stats["embedded_icon_removed_count"] else []),
                      *([f"已省略思考过程：{stats['thinking_omitted_count']} 段。"] if stats["thinking_omitted_count"] else []),
                      *([f"已保留思考过程：{stats['thinking_included_count']} 段。"] if stats["thinking_included_count"] else []),
                      *([f"已省略状态提示：{stats['status_omitted_count']} 段。"] if stats["status_omitted_count"] else []),
                      *([f"已保留状态提示：{stats['status_included_count']} 段。"] if stats["status_included_count"] else []),
                      "思考过程仅按明确 DOM 区块识别；未识别的结构保留原样。"],
    )
