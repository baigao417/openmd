"""Quality regressions use synthetic text only; no live browser or accounts."""
from pathlib import Path

import pytest

from openmd.backends import loaded_chat
from openmd.core.completeness import check_completeness


URLS = {
    "chatgpt": "https://chatgpt.com/c/demo",
    "grok": "https://grok.com/c/demo",
    "deepseek": "https://chat.deepseek.com/a/chat/s/demo",
    "doubao": "https://www.doubao.com/chat/demo",
    "gemini": "https://gemini.google.com/app/demo",
    "qwen": "https://www.qianwen.com/chat/demo",
    "yuanbao": "https://yuanbao.tencent.com/chat/agent/demo",
}


def extract(html, site="chatgpt", **options):
    item = loaded_chat.snapshot_item(Path("synthetic.html"), URLS[site])
    item.raw_meta.update(options)
    return check_completeness(item, loaded_chat.extract_loaded_chat(html, item, backend="page_snapshot.defuddle"))


@pytest.mark.parametrize("attribute", ['data-author="user"', 'data-user-message-bubble', 'data-chatgpt-search-unit-key="turn:user"'])
def test_chatgpt_user_layouts(attribute):
    result = extract(f'<main><div {attribute}>Synthetic question.</div><div data-message-author-role="assistant">Synthetic answer.</div></main>')
    assert [turn.role for turn in result.blocks] == ["user", "assistant"]
    assert result.metadata["independent_user_turn_count"] == 1
    assert result.status == "verified"


def test_independent_user_signal_catches_a_parser_miss(monkeypatch):
    original = loaded_chat._role_content
    monkeypatch.setattr(loaded_chat, "_role_content", lambda site, node: ("unknown", node) if node.has_attr("data-user-message-bubble") else original(site, node))
    result = extract('<main><div data-user-message-bubble>Synthetic question.</div><div data-message-author-role="assistant">Synthetic answer.</div></main>')
    assert result.status == "partial"
    assert result.metadata["completeness"]["status"] == "downgrade"
    assert any(check["reason"] == "conversation_user_turns_missing" for check in result.metadata["completeness"]["checks"])


def test_chatgpt_nested_hidden_user_is_not_double_counted():
    result = extract('<main><article data-testid="conversation-turn-1"><div data-author="user"><div data-user-message-bubble>Synthetic question.</div></div></article><div hidden data-user-message-bubble>Hidden</div><div data-message-author-role="assistant">Synthetic answer.</div></main>')
    assert len(result.blocks) == 2
    assert result.metadata["independent_user_turn_count"] == 1
