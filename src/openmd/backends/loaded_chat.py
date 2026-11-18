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
from urllib.parse import urlsplit

from bs4 import BeautifulSoup, Tag

from openmd.core.content_validation import html_to_markdown
from openmd.core.login import normalize_login_result
from openmd.core.models import ContentItem, TranscriptResult, TurnBlock


SELECTORS = {
    "chatgpt": '[data-message-author-role], [data-author], [data-user-message-bubble], article[data-testid*="conversation-turn"], [data-chatgpt-search-unit-key]',
    "grok": '[data-testid="user-message"], [data-testid="assistant-message"]',
    "deepseek": '.ds-message',
    "doubao": '[data-testid="message-list"] [data-testid="union_message"]',
    "gemini": '.query-text, .model-response-text',
    "qwen": '[data-chat-question-wrap], [data-chat-answers-wrap]',
    "yuanbao": '.agent-chat__list__item--human, .agent-chat__list__item--ai',
}
HOSTS = {
    "chatgpt.com": "chatgpt", "chat.openai.com": "chatgpt", "grok.com": "grok",
    "chat.deepseek.com": "deepseek", "doubao.com": "doubao", "www.doubao.com": "doubao",
    "gemini.google.com": "gemini", "www.qianwen.com": "qwen", "qianwen.com": "qwen",
    "qianwen.aliyun.com": "qwen", "tongyi.aliyun.com": "qwen", "yuanbao.tencent.com": "yuanbao",
}
CHATGPT_USER_CONTAINERS = '[data-message-author-role="user"], [data-author="user"], [data-user-message-bubble], [data-chatgpt-search-unit-key$=":user"], [data-testid="user-message"]'


def loaded_chat_site(url: str) -> str | None:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"}:
        return None
    return HOSTS.get((parsed.hostname or "").lower())


def snapshot_item(path: Path, url: str) -> ContentItem:
    """Use detail's exact identity, with a local-only snapshot extraction route."""
    site = loaded_chat_site(url)
    metadata = {"snapshot_path": str(path), "captured_from": "browser_snapshot", "coverage": "visible_only"}
    if site:
        from openmd.adapters.ai_chat import AiChatAdapter
        from openmd.backends.ai_chat import ai_chat_backend_for_site

        item = AiChatAdapter("ai_chat", ai_chat_backend_for_site(site)).resolve_items(url)[0]
        return replace(item, raw_meta={**item.raw_meta, **metadata, "chat_site": site})
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


def _role_content(site: str, node: Tag) -> tuple[str, Tag]:
    if site == "chatgpt":
        author = node if node.has_attr("data-message-author-role") or node.has_attr("data-author") else node.select_one('[data-message-author-role], [data-author]')
        role = str((author or node).get("data-message-author-role") or (author or node).get("data-author") or "")
        if not role:
            hints = " ".join(str(node.get(key) or "") for key in ("data-chatgpt-search-unit-key", "data-testid", "aria-label"))
            role = "user" if node.has_attr("data-user-message-bubble") else ("assistant" if re.search("assistant|chatgpt", hints, re.I) else ("user" if re.search("user|you", hints, re.I) else "unknown"))
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
        hints = " ".join(str(node.get(key) or "") for key in ("class", "data-message-author-role", "data-role", "aria-label"))
        role = "user" if re.search("user|query", hints, re.I) else ("assistant" if re.search("assistant|model|response|gemini", hints, re.I) else "unknown")
        return role, node
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


def _markdown(node: Tag) -> str:
    body = BeautifulSoup(str(node), "html.parser")
    for child in list(body.select('script, style, noscript, button, .sr-only, .cdk-visually-hidden, .thinking-container, [data-prompt-link-href]')):
        if child.parent is not None:
            child.decompose()
    for child in list(body.find_all(True)):
        if child.parent is not None and not _visible(child):
            child.decompose()
    # Preserve every code byte after HTML entity decoding, including indentation
    # and blank lines. General converters otherwise strip language/whitespace.
    literals = {}
    for pre in body.find_all("pre"):
        code = pre.find("code")
        code = code if code is not None else pre
        text = code.get_text()
        classes = " ".join(code.get("class", []))
        language = re.search(r"(?:language|lang)-([\w+-]+)", classes)
        fence = "`" * max(3, max((len(m.group()) + 1 for m in re.finditer(r"`+", text)), default=3))
        marker = f"OPENMDLOADEDCODE{len(literals)}END"
        while marker in str(body):
            marker += "X"
        literals[marker] = fence + (language.group(1) if language else "") + "\n" + text + ("" if text.endswith("\n") else "\n") + fence
        pre.replace_with(body.new_string("\n\n" + marker + "\n\n"))
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


def extract_loaded_chat(html: str, item: ContentItem, *, backend: str) -> TranscriptResult:
    site = loaded_chat_site(item.url)
    soup = BeautifulSoup(html, "html.parser")
    roots = _outermost([node for node in soup.select(SELECTORS[site]) if _visible(node)])
    independent_count = len(roots)
    # Separate evidence sweep: a missing role/content selector must not certify
    # an assistant-only export when user bubbles are present in the page.
    user_count = len(_outermost([node for node in soup.select(CHATGPT_USER_CONTAINERS) if _visible(node)])) if site == "chatgpt" else None
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
    for node in roots:
        role, content = _role_content(site, node)
        text = _markdown(content)
        if role not in {"user", "assistant", "system", "tool"} or not text:
            continue
        turns.append(TurnBlock(role=role, text=text, message_id=node.get("data-message-id") or node.get("data-conv-id")))
    if not turns:
        return TranscriptResult(text="", status="failed", source_level="partial_extraction", backend=backend, error_code="ai_chat_empty", error_summary="当前页没有可读取的对话轮次；请先打开并加载会话。")
    generating = _generating(soup)
    title = soup.title.get_text(strip=True) if soup.title else None
    return TranscriptResult(
        text="\n\n".join(turn.text for turn in turns) + "\n", blocks=turns,
        status="partial" if generating else "verified", source_level="browser_rendered", backend=backend,
        title=item.title or title or f"{site} conversation",
        error_code="ai_chat_not_stable" if generating else None,
        error_summary="当前页仍在生成，请等待完成后重新读取。" if generating else None,
        metadata={"chat_site": site, "captured_from": "browser_snapshot", "coverage": "visible_branch",
                  "turn_count": len(turns), "independent_turn_count": independent_count, "independent_user_turn_count": user_count, "generating": generating,
                  "selector_source": f"OpenCLI 2.0.3 clis/{site}/detail.js",
                  "assistant_turn_count": sum(turn.role == "assistant" for turn in turns)},
        source_notes=["读取当前已加载页面；仅覆盖 DOM 中已加载的分支，未验证未加载的历史消息。"],
    )
