"""Synthetic loaded-page contracts; no account or real conversation content."""
from pathlib import Path

import pytest

from openmd.backends.base import BackendExtractRequest
from openmd.backends.snapshot import SnapshotProvider, snapshot_acceleration_hint
from openmd.core.models import ContentItem
from openmd.core.completeness import check_completeness


FIXTURES = Path(__file__).parent / "fixtures" / "loaded_chat"
URLS = {
    "chatgpt": "https://chatgpt.com/c/demo",
    "grok": "https://grok.com/c/demo?rid=response",
    "deepseek": "https://chat.deepseek.com/a/chat/s/demo",
    "doubao": "https://www.doubao.com/chat/demo",
    "gemini": "https://gemini.google.com/app/demo",
    "qwen": "https://www.qianwen.com/chat/demo",
    "yuanbao": "https://yuanbao.tencent.com/chat/agent/demo",
}


def extract(site, path, url=None):
    item = ContentItem(platform="ai_chat", input_type="single", source_type="conversation",
                       url=url or URLS[site], raw_meta={"snapshot_path": str(path), "chat_site": site})
    result = SnapshotProvider(runner=lambda *_: pytest.fail("AI page reached Defuddle")).extract(
        BackendExtractRequest(item=item, backend_chain=("page_snapshot.defuddle",)))
    return check_completeness(item, result)


@pytest.mark.parametrize("site", URLS)
def test_loaded_site_turns_code_table_and_visible_coverage(site):
    result = extract(site, FIXTURES / f"{site}.html")
    assert result.status == "verified"
    assert [turn.role for turn in result.blocks] == ["user", "assistant", "user", "assistant"]
    assert result.blocks[0].text == result.blocks[2].text == "Synthetic question."
    assert '```python\n  print("<demo>")\n```' in result.blocks[1].text
    assert "| Key | Value |" in result.blocks[1].text
    assert "| alpha | beta |" in result.blocks[1].text
    assert result.metadata["coverage"] == "visible_branch"
    assert result.metadata["turn_count"] == 4
    assert result.metadata["completeness"]["status"] == "ok"
    assert result.metadata["captured_from"] == "browser_snapshot"


@pytest.mark.parametrize("site", URLS)
def test_loaded_generating_empty_and_login(site, tmp_path):
    source = (FIXTURES / f"{site}.html").read_text(encoding="utf-8")
    path = tmp_path / "page.html"
    path.write_text(source.replace("</body>", '<button aria-label="Stop generating">Stop</button></body>'), encoding="utf-8")
    result = extract(site, path)
    assert result.status == "partial" and result.error_code == "ai_chat_not_stable"
    assert result.metadata["generating"] is True
    path.write_text('<html><body><main><p>No conversation selected.</p></main></body></html>', encoding="utf-8")
    assert extract(site, path).error_code == "ai_chat_empty"
    path.write_text('<html><body><main><form action="/auth/login"><input type="password"><button>Log in</button></form></main></body></html>', encoding="utf-8")
    result = extract(site, path)
    assert result.error_code == "login_required" and not result.blocks and not result.text
    assert result.metadata["login"]["url"].startswith("https://")


def test_missing_empty_turn_is_counted_before_body_conversion(tmp_path):
    path = tmp_path / "page.html"
    path.write_text('<main><div data-testid="user-message">Synthetic question.</div><div data-testid="assistant-message"></div></main>', encoding="utf-8")
    result = extract("grok", path)
    assert result.status == "partial"
    assert result.metadata["independent_turn_count"] == 2
    assert result.metadata["completeness"]["status"] == "downgrade"


def test_hidden_duplicate_and_navigation_are_not_turns(tmp_path):
    path = tmp_path / "page.html"
    path.write_text('<nav>Private sidebar title</nav><main><article data-testid="conversation-turn-1"><div data-message-author-role="user">Synthetic question.</div></article><article data-testid="conversation-turn-2"><div data-message-author-role="assistant"><div class="markdown">Synthetic answer.</div></div></article><div hidden data-message-author-role="user">Hidden</div></main>', encoding="utf-8")
    result = extract("chatgpt", path)
    assert [turn.role for turn in result.blocks] == ["user", "assistant"]
    assert result.metadata["independent_turn_count"] == 2
    assert "Private" not in result.text and "Hidden" not in result.text


def test_loaded_hint_uses_current_page():
    for url in URLS.values():
        assert "当前" in (snapshot_acceleration_hint(url) or "")
        assert "平台路线" not in (snapshot_acceleration_hint(url) or "")


def test_legacy_gemini_has_unknown_independent_count(tmp_path):
    path = tmp_path / "page.html"
    path.write_text('<main><div class="message-user">Synthetic question.</div><div class="message-model">Synthetic answer.</div></main>', encoding="utf-8")
    result = extract("gemini", path)
    assert [turn.role for turn in result.blocks] == ["user", "assistant"]
    assert result.metadata["independent_turn_count"] is None
    assert result.metadata["completeness"]["status"] == "unknown"
