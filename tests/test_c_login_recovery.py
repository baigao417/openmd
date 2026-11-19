from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from openmd import browser_host, cli, interactive
from openmd.backends.ai_chat import CHATGPT_BACKEND, OpenCliAiChatProvider
from openmd.backends.base import BackendExtractRequest
from openmd.backends.executor import BackendExecutor
from openmd.core.content_validation import validate_extraction_content
from openmd.core.models import ContentItem, TranscriptResult, TurnBlock
from openmd.core.renderer import render_markdown_document, render_conversation_blocks
from openmd.core.runlog import build_item_runlog_entry, append_runlog_entry


def item(url="https://chatgpt.com/c/test", platform="ai_chat"):
    return ContentItem(platform=platform, input_type="single", url=url, source_type="conversation" if platform == "ai_chat" else "article", raw_meta={"chat_site": "chatgpt"})


def failure(code="backend_exit_77"):
    return TranscriptResult(text="", status="failed", source_level="partial_extraction", backend=CHATGPT_BACKEND, error_code=code)


@pytest.mark.parametrize("code", ["backend_exit_77", "AUTH_REQUIRED", "ai_chat_not_logged_in", "weixin_exporter_auth_required", "session_expired"])
def test_login_contract_normalizes_known_errors_and_preserves_origin(code, tmp_path):
    source = item()
    result = validate_extraction_content(failure(code), source)
    assert result.error_code == "login_required"
    assert result.metadata["login"] == {"site": "ChatGPT", "url": "https://chatgpt.com"}
    assert result.metadata["original_error_code"] == code
    row = append_runlog_entry(tmp_path / "runlog.jsonl", build_item_runlog_entry(source, result))
    assert row["login"] == result.metadata["login"]
    assert row["original_error_code"] == code


def test_chat_exit77_normalizes_direct_provider_result():
    provider = OpenCliAiChatProvider("chatgpt", CHATGPT_BACKEND, runner=lambda *_: (77, "", ""))
    result = provider.extract(BackendExtractRequest(item=item(), backend_chain=(CHATGPT_BACKEND,)))
    assert result.error_code == "login_required"
    assert result.metadata["login"]["site"] == "ChatGPT"


@pytest.mark.parametrize("text,title", [
    ("Please sign in to continue.", "Sign in"),
    ("请登录后查看完整内容\n扫码登录\n注册账号", "登录"),
    ('<form action="/login"><input name="username"><input type="password"><button>Log in</button></form>', "Account"),
    ("Username or email address\nPassword\nForgot password?", "Sign in to GitHub"),
])
@pytest.mark.parametrize("platform", ["page_snapshot", "web_article", "weixin"])
def test_login_wall_is_rejected_with_homepage_action(text, title, platform):
    source = item("https://accounts.example.org/login?return=/secret", platform)
    result = validate_extraction_content(TranscriptResult(text=text, title=title, status="verified", source_level="third_party_extractor", backend="reader"), source)
    assert result.status == "failed" and not result.has_usable_text
    assert result.error_code == "login_required"
    assert result.metadata["login"]["url"] == "https://accounts.example.org"


@pytest.mark.parametrize("text", [
    "本文讨论登录与注册的设计。登录后查看这样的提示应解释原因，而不是误导用户。",
    "The author explains why sign in forms should use password managers. This article compares two authentication designs.",
    "```html\n<form><input type=\"password\"><button>Sign in</button></form>\n```\nThis example demonstrates a form.",
])
def test_article_mentioning_login_is_not_a_wall(text):
    result = validate_extraction_content(TranscriptResult(text=text, title="Designing accounts", status="verified", source_level="native_text", backend="reader"), item("https://example.org/article", "web_article"))
    assert result.status == "verified"


def test_snapshot_and_chat_have_distinct_machine_coverage():
    value = TranscriptResult(text="body", status="verified", source_level="third_party_extractor", backend="reader")
    assert render_markdown_document(item("https://example.org/a", "page_snapshot"), value).frontmatter["coverage"] == "visible_only"
    assert render_markdown_document(item(), value).frontmatter["coverage"] == "visible_branch"
    assert "范围：提供方声明完整" in render_conversation_blocks([TurnBlock("user", "hi")], source_url=item().url, platform="ai_chat", exported_at="now", coverage="provider_declared_complete")


def login_executor(monkeypatch):
    calls = []
    class Provider:
        def extract(self, request):
            calls.append(request.item.url)
            return failure()
    monkeypatch.setattr(cli, "_build_backend_executor", lambda *a, **kw: BackendExecutor(providers={"mock": Provider()}))
    return calls


def test_go_json_login_action_never_prompts(tmp_path, monkeypatch, capsys):
    login_executor(monkeypatch)
    monkeypatch.setattr(interactive, "is_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *_: pytest.fail("JSON must not prompt"))
    assert cli.main(["go", item().url, "--backend", "mock", "--json", "-o", str(tmp_path)]) == 1
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["error_code"] == "login_required"
    assert receipt["next_action"] == {"type": "login", "site": "ChatGPT", "url": "https://chatgpt.com"}


def test_tty_opens_login_and_retries_once(tmp_path, monkeypatch, capsys):
    calls = login_executor(monkeypatch)
    monkeypatch.setattr(interactive, "is_interactive", lambda: True)
    prompts, opened = [], []
    monkeypatch.setattr("builtins.input", lambda prompt: prompts.append(prompt) or "")
    monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url) or True)
    assert cli.main(["go", item().url, "--backend", "mock", "-o", str(tmp_path)]) == 1
    assert calls == [item().url, item().url]
    assert opened == ["https://chatgpt.com"] and len(prompts) == 2
    assert "需要登录 ChatGPT" in capsys.readouterr().err


def test_batch_pauses_same_site_and_continues_other_sites(tmp_path, monkeypatch, capsys):
    calls = login_executor(monkeypatch)
    monkeypatch.setattr(interactive, "is_interactive", lambda: False)
    inputs = tmp_path / "urls.txt"
    inputs.write_text(item().url + "\nhttps://chatgpt.com/c/second\nhttps://chat.deepseek.com/a/chat/s/third\n", encoding="utf-8")
    assert cli.main(["batch", str(inputs), "--backend", "mock", "--receipt", "compact", "--out", str(tmp_path / "out")]) != 0
    captured = capsys.readouterr()
    assert len(calls) == 2
    assert captured.err.count("需要登录 ChatGPT") == 1
    receipt = json.loads(captured.out)
    assert receipt["paused_count"] == 1 and receipt["next_action"]["type"] == "login"


def test_browser_jobs_pause_and_resume_site_without_repeat_failures(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENMD_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(browser_host, "_spawn_runner", lambda *a, **kw: None)
    batch = browser_host.start_batch([item().url, "https://chatgpt.com/c/second", "https://example.org/article"], str(tmp_path), spawn=lambda *a, **kw: None)
    calls = []
    def run(command, **kwargs):
        url = command[command.index("go") + 1]
        calls.append(url)
        if "chatgpt" in url:
            return subprocess.CompletedProcess(command, 1, json.dumps({"ok": False, "error_code": "login_required", "login": {"site": "ChatGPT", "url": "https://chatgpt.com"}}), "")
        return subprocess.CompletedProcess(command, 0, json.dumps({"ok": True, "saved_to": str(tmp_path / "ok.md")}), "")
    browser_host.run_queue(runner=run)
    assert len(calls) == 2
    statuses = [browser_host.read_status(j["id"]) for j in batch["jobs"]]
    assert [s["state"] for s in statuses] == ["failed", "paused", "done"]
    assert statuses[0]["login"]["url"] == "https://chatgpt.com"
    reply = browser_host.handle({"type": "retry_login", "id": statuses[0]["id"]})
    assert reply["ok"] and len(reply["jobs"]) == 2
    assert all(browser_host.read_status(s["id"])["state"] == "queued" for s in statuses[:2])


def test_extension_has_persistent_login_buttons_and_retry_handler():
    script = (Path(browser_host.__file__).parent / "browser_extension" / "background.js").read_text(encoding="utf-8")
    assert 'title: "去登录"' in script and 'title: "已登录，重试"' in script
    assert "notifications.onButtonClicked.addListener" in script
    assert "retry_login" in script and "chrome.tabs.create" in script


def test_declared_login_destinations_are_generated():
    from openmd.routing.loader import load_catalog
    records = {r["source_id"]: r for r in load_catalog().capability_metadata}
    for source in ("weixin", "bilibili", "xiaohongshu", "douyin", "x", "xiaoe"):
        assert records[source]["display_name"] and records[source]["login_url"].startswith("https://")
    assert records["ai_chat"]["login_sites"]["chatgpt"]["login_url"] == "https://chatgpt.com"


@pytest.mark.parametrize("metadata,summary,original", [
    ({"tool_run": {"exit_code": 77}}, "Tool failed", "tool_backend_exit"),
    ({"exit_code": 77}, "Command failed", "tool_backend_exit"),
    ({}, '{"ok": false, "error": {"code": "AUTH_REQUIRED", "message": "Sign in"}}', "AUTH_REQUIRED"),
])
def test_other_opencli_routes_share_login_contract(metadata, summary, original):
    from dataclasses import replace
    result = validate_extraction_content(replace(failure("tool_backend_exit"), metadata=metadata, error_summary=summary), item())
    assert result.error_code == "login_required"
    assert result.metadata["original_error_code"] == original


def test_retry_only_resumes_the_original_browser_batch(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENMD_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(browser_host, "_spawn_runner", lambda *a, **kw: None)
    batches = [browser_host.start_batch([item().url, "https://chatgpt.com/c/second"], str(tmp_path), spawn=lambda *a, **kw: None) for _ in range(2)]
    def run(command, **kwargs):
        return subprocess.CompletedProcess(command, 1, '{"ok": false, "error_code": "login_required"}', "")
    browser_host.run_queue(runner=run)
    reply = browser_host.handle({"type": "retry_login", "id": batches[0]["jobs"][0]["id"]})
    assert {j["id"] for j in reply["jobs"]} == {j["id"] for j in batches[0]["jobs"]}
    assert all(browser_host.read_status(j["id"])["state"] == "paused" for j in batches[1]["jobs"])


def test_declared_aliases_resolve_to_the_same_login_site():
    from openmd.core.login import login_for_url
    assert login_for_url("https://doubao.com/chat/123")["site"] == "豆包"
    assert login_for_url("https://tongyi.aliyun.com/qianwen/chat/123")["site"] == "千问"


@pytest.mark.parametrize("exit_code", [0, 1])
def test_ai_structured_auth_preserves_original_code(exit_code):
    provider = OpenCliAiChatProvider("chatgpt", CHATGPT_BACKEND, runner=lambda *_: (exit_code, '{"ok": false, "error": {"code": "AUTH_REQUIRED"}}', ""))
    result = provider.extract(BackendExtractRequest(item=item(), backend_chain=(CHATGPT_BACKEND,)))
    assert result.error_code == "login_required"
    assert result.metadata["original_error_code"] == "AUTH_REQUIRED"


def test_ai_error_prose_login_fallback_is_normalized():
    provider = OpenCliAiChatProvider("chatgpt", CHATGPT_BACKEND, runner=lambda *_: (1, "", "Please sign in to continue"))
    result = provider.extract(BackendExtractRequest(item=item(), backend_chain=(CHATGPT_BACKEND,)))
    assert result.error_code == "login_required"


def test_extension_login_notice_keeps_all_batch_groups():
    script = (Path(browser_host.__file__).parent / "browser_extension" / "background.js").read_text(encoding="utf-8")
    assert "notice.groups[job.batch || job.id]" in script
    assert "Object.entries(notice.groups)" in script
    assert "batch.loginPaused = true" in script and "!batch.loginPaused" in script


def test_short_article_with_demo_form_is_not_a_login_page():
    from openmd.core.content_validation import login_wall_reason
    html = '<article><h1>Form design</h1><p>This article explains the sign in interface. The following form is an example for designers.</p><form><input type="' + 'pass' + 'word' + '"><button>Sign in</button></form></article>'
    assert not login_wall_reason(html, "Form design")


def test_declared_web_reader_reports_login_wall_before_min_length(tmp_path):
    from openmd.routing.loader import load_catalog
    from openmd.routing.readers import read_text
    from openmd.routing.runner import ToolError, ToolRun
    cap = load_catalog().capabilities["defuddle.web.content"]
    (tmp_path / "content.json").write_text(json.dumps({"title": "Sign in", "content": "请先登录"}), encoding="utf-8")
    with pytest.raises(ToolError) as exc:
        read_text(cap, ToolRun(0, 1, "", "", {}), tmp_path)
    assert exc.value.code == "login_wall"


def test_command_provider_preserves_structured_login_failure(monkeypatch):
    from openmd.backends.registry import BackendRuntimeOptions
    from openmd.routing import providers
    from openmd.routing.loader import load_catalog
    from openmd.routing.runner import ToolRun
    catalog = load_catalog()
    cap = catalog.capabilities["defuddle.web.content"]
    monkeypatch.setattr(providers, "installed_identity", lambda _: {"tool_version_verified": True})
    monkeypatch.setattr(providers, "run_tool", lambda *args: ToolRun(1, 0.1, '{"ok": false, "error": {"code": "AUTH_REQUIRED"}}', "", {}))
    provider = providers.CommandToolProvider(catalog.cards["defuddle"], cap, BackendRuntimeOptions(enable_network_backends=True))
    result = BackendExecutor(providers={cap["id"]: provider}).extract(item("https://example.org/a", "web_article"), (cap["id"],))
    assert result.error_code == "login_required"
    assert result.metadata["original_error_code"] == "AUTH_REQUIRED"


def test_snapshot_login_retry_replaces_the_captured_dom(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENMD_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(browser_host, "_spawn_runner", lambda *a, **kw: None)
    job = browser_host.start_snapshot_job("<p>Please sign in to continue.</p>", "https://example.org/article", save_to=str(tmp_path), spawn=lambda *a, **kw: None)
    browser_host.run_queue(runner=lambda command, **kw: subprocess.CompletedProcess(command, 1, '{"ok": false, "error_code": "login_required"}', ""))
    assert Path(job["snapshot_path"]).is_file()
    missing = browser_host.handle({"type": "retry_login", "id": job["id"]})
    assert not missing["ok"] and "snapshot_refresh_required" in missing["error"]
    reply = browser_host.handle({"type": "retry_login", "id": job["id"], "snapshot": {"html": "<p>Original article after login.</p>", "url": job["url"], "title": "Article"}})
    assert reply["ok"] and [j["id"] for j in reply["jobs"]] == [job["id"]]
    assert "Original article after login" in Path(job["snapshot_path"]).read_text()


def test_cloud_service_credentials_do_not_request_a_website_login():
    result = validate_extraction_content(failure("tingwu_auth_required"), item())
    assert result.error_code == "tingwu_auth_required" and "login" not in result.metadata
