import json
from pathlib import Path

import pytest

from openmd.cli import main


FIXTURE = Path(__file__).parent / "fixtures" / "loaded_chat" / "grok.html"


def test_agent_snapshot_uses_local_parser_identity_sync_and_conflict(tmp_path, capsys, monkeypatch):
    from openmd.backends.ai_chat import OpenCliAiChatProvider
    monkeypatch.setattr(OpenCliAiChatProvider, "extract", lambda *_: pytest.fail("snapshot navigated through URL detail"))
    out = tmp_path / "saved"
    command = ["go", str(FIXTURE), "--as-snapshot", "--url", "https://grok.com/c/demo?rid=response", "-o", str(out), "--json"]
    assert main(command) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["status"] == "verified" and first["completeness"]["status"] == "ok"
    assert first["sync"]["operation"] == "created"
    artifact = Path(first["saved_to"])
    text = artifact.read_text(encoding="utf-8")
    assert text.count("### user\n") == 2 and text.count("### assistant\n") == 2
    assert '  print("<demo>")' in text
    assert 'captured_from: "browser_snapshot"' in text
    command[command.index("https://grok.com/c/demo?rid=response")] = "https://grok.com/c/demo"
    assert main(command) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["sync"]["operation"] == "unchanged"
    assert artifact.read_text(encoding="utf-8") == text
    artifact.write_text(text + "\nSynthetic user edit.\n", encoding="utf-8")
    assert main(command) == 0
    conflict = json.loads(capsys.readouterr().out)
    assert conflict["sync"]["operation"] == "conflict_kept_user_edit"
    assert artifact.read_text(encoding="utf-8").endswith("Synthetic user edit.\n")


@pytest.mark.parametrize("url", ["https://chatgpt.com/c/demo", "https://grok.com/c/demo?rid=response", "https://chat.deepseek.com/a/chat/s/demo", "https://www.doubao.com/chat/demo", "https://gemini.google.com/app/demo", "https://www.qianwen.com/chat/demo", "https://yuanbao.tencent.com/chat/agent/demo"])
def test_loaded_and_detail_identity_match(url, tmp_path):
    from openmd.backends.loaded_chat import snapshot_item
    from openmd.adapters.registry import create_default_registry
    from openmd.core.sync_manifest import source_id_for
    registry = create_default_registry()
    detail = registry.adapter_for(url).resolve_items(url)[0]
    loaded = snapshot_item(tmp_path / "page.html", url)
    assert loaded.platform == detail.platform == "ai_chat"
    assert loaded.source_type == "conversation"
    assert source_id_for(loaded) == source_id_for(detail)
    assert loaded.url == detail.url


def test_agent_login_snapshot_has_shared_recovery_receipt(tmp_path, capsys):
    page = tmp_path / "page.html"
    page.write_text('<html><body><button>Log in</button></body></html>', encoding="utf-8")
    assert main(["go", str(page), "--as-snapshot", "--url", "https://chatgpt.com/auth/login", "-o", str(tmp_path / "out"), "--json"]) == 1
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["error_code"] == "login_required"
    assert receipt["next_action"]["type"] == "login"
    assert not list((tmp_path / "out").glob("*.md"))
