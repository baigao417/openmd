import json
from pathlib import Path

from bs4 import BeautifulSoup

from openmd import browser_host
from openmd.cli import main
from openmd.diagnostics.feedback import build_feedback_package
from openmd.diagnostics.sanitize import sanitize_snapshot
from openmd.adapters.ai_chat import AiChatAdapter
from openmd.backends.ai_chat import ai_chat_backend_for_site
from openmd.backends.loaded_chat import extract_loaded_chat


SENTINELS = "真实用户姓名 alpha@example.com session-token-123"


def _html(site="chatgpt.com"):
    return f'''<html><head><title>{SENTINELS}</title><script>const x="{SENTINELS}"</script></head>
    <body><main class="conversation-turn" data-message-author-role="user" data-role="turn">
    <div class="markdown">{SENTINELS}</div><a href="https://{site}/chat?id=secret">{SENTINELS}</a>
    <input value="{SENTINELS}"><pre><code class="language-python">{SENTINELS}</code></pre></main>
    <main class="conversation-turn" data-message-author-role="assistant" data-role="turn"><div class="markdown">回答</div></main></body></html>'''


def test_sanitize_preserves_structure_and_removes_sentinels():
    result = sanitize_snapshot(_html(), "https://chatgpt.com/share/session-token-123?q=private")
    assert SENTINELS not in result.html
    assert "session-token-123" not in result.html
    assert "data-message-author-role=\"user\"" in result.html
    assert result.manifest["site"] == "chatgpt.com"
    assert result.manifest["replacement_stats"]["text_nodes"] > 0
    assert "secret" not in result.html


def test_sanitize_attribute_whitelist_removes_all_identifier_sentinels():
    html = f'''<html><head><meta property="og:title" content="{SENTINELS}"></head><body>
    <div id="{SENTINELS}" data-message-id="{SENTINELS}" data-content="{SENTINELS}" data-text="{SENTINELS}"
      datetime="{SENTINELS}" srcset="{SENTINELS}" style="background:url(https://x/{SENTINELS})"
      data-message-author-role="user" class="turn"><span>{SENTINELS}</span></div></body></html>'''
    result = sanitize_snapshot(html, "https://example.org/c/session-token")
    assert SENTINELS not in result.html
    assert "data-message-author-role=\"user\"" in result.html
    assert "data-content" not in result.html and "data-text" not in result.html


def test_sanitized_snapshot_roundtrips_loaded_chat_roles():
    sanitized = sanitize_snapshot(_html(), "https://chatgpt.com/c/synthetic")
    item = AiChatAdapter("ai_chat", ai_chat_backend_for_site("chatgpt")).resolve_items("https://chatgpt.com/c/synthetic")[0]
    parsed = extract_loaded_chat(sanitized.html, item, backend="test")
    assert [block.role for block in parsed.blocks] == ["user", "assistant"]


def test_seven_site_fixtures_roundtrip_structure():
    fixtures = Path(__file__).parent / "fixtures" / "loaded_chat"
    urls = {"chatgpt": "https://chatgpt.com/c/synthetic", "grok": "https://grok.com/", "deepseek": "https://chat.deepseek.com/", "doubao": "https://www.doubao.com/chat/", "gemini": "https://gemini.google.com/app", "qwen": "https://www.qianwen.com/", "yuanbao": "https://yuanbao.tencent.com/chat/"}
    for site, url in urls.items():
        html = (fixtures / f"{site}.html").read_text(encoding="utf-8")
        original = extract_loaded_chat(html, AiChatAdapter("ai_chat", ai_chat_backend_for_site(site)).resolve_items(url)[0], backend="test")
        safe = sanitize_snapshot(html, url)
        roundtrip = extract_loaded_chat(safe.html, AiChatAdapter("ai_chat", ai_chat_backend_for_site(site)).resolve_items(url)[0], backend="test")
        assert len(roundtrip.blocks) == len(original.blocks), site
        assert [b.role for b in roundtrip.blocks] == [b.role for b in original.blocks], site


def test_feedback_package_contains_only_reviewable_files(tmp_path):
    source = tmp_path / "page.html"
    source.write_text(_html(), encoding="utf-8")
    out = tmp_path / "package"
    result = build_feedback_package(source, url="https://chatgpt.com/c/synthetic", output=out,
                                    status={"state": "failed", "error_code": "synthetic_failure"})
    assert set(result["files"]) == {"structure.html", "manifest.json", "receipt.json", "doctor.json", "README.txt"}
    assert SENTINELS not in (out / "structure.html").read_text(encoding="utf-8")
    assert json.loads((out / "receipt.json").read_text(encoding="utf-8"))["error_code"] == "synthetic_failure"


def test_feedback_cli_prints_review_instruction(tmp_path, capsys):
    source = tmp_path / "page.html"
    source.write_text(_html(), encoding="utf-8")
    assert main(["report", "feedback", str(source), "--url", "https://example.org/synthetic", "-o", str(tmp_path / "out")]) == 0
    assert "请检查后再附加" in capsys.readouterr().out


def test_link_feedback_package_contains_only_bounded_receipts(tmp_path):
    source_url = "https://www.douyin.com/video/123?signature=private&foo=bar"
    status = {
        "id": "a" * 32,
        "state": "failed",
        "url": source_url,
        "feedback_opt_in": True,
        "error_code": "route_failed",
        "error": "原始错误正文 https://www.douyin.com/video/123?signature=private",
        "result": {"status": "failed", "source_level": "platform", "error_code": "route_failed", "next_action": "重新保存"},
    }
    from openmd.diagnostics.feedback import build_feedback_package
    out = tmp_path / "package"
    result = build_feedback_package("job-id", output=out, status=status)
    assert set(result["files"]) == {"manifest.json", "receipt.json"}
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    receipt = json.loads((out / "receipt.json").read_text(encoding="utf-8"))
    assert manifest["page_structure"] == "not_captured"
    assert manifest["reason"] == "link_route"
    assert manifest["route"] == "link_route"
    assert manifest["platform"] == "douyin"
    assert "signature" not in json.dumps([manifest, receipt], ensure_ascii=False)
    assert "原始错误正文" in receipt["error_summary"]
    assert len(receipt["error_summary"]) <= 240
    assert not (out / "structure.html").exists()


def test_link_feedback_report_uses_receipt_only_and_real_actions(tmp_path):
    job = browser_host.start_job(
        "https://www.bilibili.com/video/BV1abc?signature=private",
        str(tmp_path),
        feedback_opt_in=True,
        spawn=lambda *a, **k: None,
    )
    status = browser_host.read_status(job["id"])
    status.update(state="failed", error_code="route_failed", error="失败摘要")
    browser_host._write_status(browser_host.jobs_root() / job["id"], status)
    reply = browser_host.handle({"type": "feedback_report", "id": job["id"], "save_to": str(tmp_path)})
    assert reply["ok"]
    output = Path(reply["report"]["output"])
    assert set(reply["report"]["files"]) == {"manifest.json", "receipt.json"}
    assert not (output / "structure.html").exists()
    assert json.loads((output / "receipt.json").read_text(encoding="utf-8"))["error_code"] == "route_failed"

    missing = browser_host.handle({"type": "feedback_report", "id": "f" * 32, "save_to": str(tmp_path)})
    assert missing["error_code"] == "job_not_found"
    assert "任务" in missing["next_action"]


def test_link_feedback_report_requires_opt_in(tmp_path):
    job = browser_host.start_job("https://example.org/article", str(tmp_path), spawn=lambda *a, **k: None)
    reply = browser_host.handle({"type": "feedback_report", "id": job["id"], "save_to": str(tmp_path)})
    assert reply["ok"] is False
    assert reply["error_code"] == "feedback_not_available"
    assert "开启" in reply["next_action"]
