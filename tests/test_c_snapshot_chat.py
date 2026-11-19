from __future__ import annotations

import json
from pathlib import Path

from openmd.backends.ai_chat import (
    CHATGPT_BACKEND,
    DEEPSEEK_BACKEND,
    OpenCliAiChatProvider,
    build_opencli_detail_args,
    conversation_id_for_url,
    parse_ai_chat_output,
)
from openmd.backends.base import BackendExtractRequest
from openmd.backends.snapshot import SnapshotProvider
import openmd.backends.snapshot as snapshot_backend
from openmd.browser_host import handle
from openmd.core.content_validation import validate_extraction_content
from openmd.core.detector import detect_platform
from openmd.core.models import ContentItem, TableBlock, TranscriptResult, TurnBlock
from openmd.core.renderer import render_conversation_blocks, render_document, render_markdown_document
from openmd.core.package_writer import write_item_package
from openmd.core.package_validator import validate_package


def test_ai_chat_detector_and_opencli_ids():
    assert detect_platform("https://chatgpt.com/c/abc123") == "ai_chat"
    assert detect_platform("https://chat.deepseek.com/a/chat/s/749e6bbd-6a45-4440-beaa-ae5238bf06d8") == "ai_chat"
    assert conversation_id_for_url("chatgpt", "https://chatgpt.com/c/abc123") == "abc123"
    assert conversation_id_for_url("deepseek", "https://chat.deepseek.com/a/chat/s/749e6bbd-6a45-4440-beaa-ae5238bf06d8").startswith("749e6bbd")
    assert build_opencli_detail_args("chatgpt", "abc123")[-2:] == ["--markdown", "true"]


def test_ai_chat_parser_preserves_code_and_rejects_empty_or_generating():
    payload = {"messages": [
        {"role": "user", "id": "m1", "content": "show code"},
        {"role": "assistant", "id": "m2", "content": "```python\nprint(1)\n```"},
    ]}
    turns, meta = parse_ai_chat_output(payload, site="chatgpt", source_url="https://chatgpt.com/c/x")
    assert [turn.role for turn in turns] == ["user", "assistant"]
    assert "```python" in turns[1].text and meta["turn_count"] == 2
    try:
        parse_ai_chat_output({"messages": [], "status": "complete"}, site="chatgpt", source_url="https://chatgpt.com/c/x")
    except ValueError as exc:
        assert str(exc) == "平台返回了空会话，未取得任何轮次。"
    else:
        raise AssertionError("empty chat should be rejected")
    try:
        parse_ai_chat_output({"messages": [], "status": "generating"}, site="chatgpt", source_url="https://chatgpt.com/c/x")
    except ValueError as exc:
        assert str(exc) == "对话仍在生成中，请等待生成完成后重试。"
    else:
        raise AssertionError("generating chat should be rejected")


def test_ai_chat_coverage_is_machine_enum_and_auth_text_does_not_scan正文():
    turns, meta = parse_ai_chat_output(
        {"messages": [{"role": "assistant", "content": "The author says sign in is optional."}]},
        site="chatgpt",
        source_url="https://chatgpt.com/c/x",
    )
    assert turns[0].text.startswith("The author")
    assert meta["coverage"] == "visible_branch"


def test_ai_chat_structured_auth_code_and_empty_result_are_distinct():
    import pytest
    with pytest.raises(ValueError, match="未登录"):
        parse_ai_chat_output({"ok": False, "error": {"code": "AUTH_REQUIRED", "message": "author"}}, site="chatgpt", source_url="https://chatgpt.com/c/x")
    with pytest.raises(ValueError, match="空会话"):
        parse_ai_chat_output({"ok": False, "error": {"code": "EMPTY_RESULT", "message": "no rows"}}, site="chatgpt", source_url="https://chatgpt.com/c/x")


def test_ai_chat_provider_uses_site_specific_detail_command():
    calls = []
    provider = OpenCliAiChatProvider("deepseek", DEEPSEEK_BACKEND, runner=lambda command, timeout: (calls.append(command) or (0, json.dumps({"messages": [{"role": "user", "text": "hi"}]}), "")))
    item = ContentItem(platform="ai_chat", input_type="single", url="https://chat.deepseek.com/a/chat/s/abc", source_type="conversation", raw_meta={"chat_site": "deepseek"})
    result = provider.extract(BackendExtractRequest(item=item, backend_chain=(DEEPSEEK_BACKEND,)))
    assert result.status == "verified" and result.blocks[0].role == "user"
    assert calls[0][:4] == ["opencli", "deepseek", "detail", "abc"]


def test_conversation_renderer_and_package_blocks(tmp_path):
    blocks = [TurnBlock("user", "question", time="10:00", message_id="u1"), TurnBlock("assistant", "answer\n\n```js\n1\n```", message_id="a1"), TableBlock([["a", "b"], ["1", "2"]], merged_cells=[{"row": 0, "col": 0}])]
    rendered = render_conversation_blocks(blocks, source_url="https://chatgpt.com/c/x", platform="chatgpt", exported_at="2026-11-18T00:00:00+00:00")
    assert "### user · 10:00" in rendered and "```js" in rendered and "轮次数：2" in rendered
    item = ContentItem(platform="ai_chat", input_type="single", url="https://chatgpt.com/c/x", source_type="conversation", raw_meta={"chat_site": "chatgpt"})
    result = TranscriptResult(text="question\nanswer", status="verified", source_level="browser_rendered", backend=CHATGPT_BACKEND, blocks=blocks, metadata={"coverage": "visible_branch"})
    written = write_item_package(item, result, tmp_path)
    assert (written.package_path / "messages.json").is_file()
    assert validate_package(written.package_path).ok


def test_snapshot_backend_fixtures_and_validation(tmp_path):
    article = tmp_path / "article.html"
    article.write_text("<html><head><title>Article</title></head><body><main><h1>Article</h1><p>Visible body text.</p></main></body></html>", encoding="utf-8")
    item = ContentItem(platform="page_snapshot", input_type="single", url="https://example.org/a", source_type="article", raw_meta={"snapshot_path": str(article)})
    result = SnapshotProvider().extract(BackendExtractRequest(item=item, backend_chain=("page_snapshot.defuddle",)))
    assert result.status == "verified" and result.metadata["coverage"] == "visible_only"
    document = render_markdown_document(item, result)
    text = render_document(document)
    assert 'captured_from: "browser_snapshot"' in text and 'coverage: "visible_only"' in text
    wall = tmp_path / "wall.html"
    wall.write_text("<html><head><title>Just a moment</title></head><body>Checking your browser</body></html>", encoding="utf-8")
    wall_item = ContentItem(platform="page_snapshot", input_type="single", url="https://example.org/pay", source_type="article", raw_meta={"snapshot_path": str(wall)})
    wall_result = SnapshotProvider().extract(BackendExtractRequest(item=wall_item, backend_chain=("page_snapshot.defuddle",)))
    rejected = validate_extraction_content(wall_result, wall_item)
    assert rejected.status == "failed" or rejected.error_code == "browser_challenge_page"


def test_snapshot_managed_defuddle_and_explicit_fallback(monkeypatch, tmp_path):
    article = tmp_path / "article.html"
    article.write_text("<html><head><title>Article</title></head><body><main><p>Managed body text.</p></main></body></html>", encoding="utf-8")
    item = ContentItem(platform="page_snapshot", input_type="single", url="https://example.org/a", source_type="article", raw_meta={"snapshot_path": str(article)})
    monkeypatch.setattr(snapshot_backend, "_run_managed_defuddle", lambda *args, **kwargs: "# Managed\n\nManaged body text.")
    managed = SnapshotProvider().extract(BackendExtractRequest(item=item, backend_chain=("page_snapshot.defuddle",)))
    assert managed.source_level == "third_party_extractor"
    assert managed.metadata["coverage"] == "visible_only"
    monkeypatch.setattr(snapshot_backend, "_run_managed_defuddle", lambda *args, **kwargs: None)
    fallback = SnapshotProvider().extract(BackendExtractRequest(item=item, backend_chain=("page_snapshot.defuddle",)))
    assert fallback.source_level == "partial_extraction"
    assert any("未使用 Defuddle，正文抽取为简易回退" in note for note in fallback.source_notes)


def test_xiaohongshu_loaded_snapshot_preserves_note_metadata_and_assets(tmp_path):
    from openmd.backends.loaded_chat import snapshot_item
    from openmd.core.sync_manifest import source_id_for
    from openmd.adapters.registry import create_default_registry

    url = "https://www.xiaohongshu.com/explore/6ad1a387000000000b007478"
    html = """
    <html><head><title>页面标题</title></head><body>
      <article data-testid="note-detail">
        <h1 data-testid="note-title">页面标题</h1>
        <div data-testid="user-name">公开作者</div>
        <time datetime="2026-11-19T09:30:00+08:00"></time>
        <div data-testid="note-content"><p>这是笔记正文。</p><a class="tag">#效率</a><a class="tag">#工作流</a></div>
        <img src="https://sns-img-bd.xhscdn.com/abc.jpg?xsec_token=secret" />
        <img data-src="https://sns-img-bd.xhscdn.com/def.jpg?foo=bar" />
        <video><source src="https://sns-video-bd.xhscdn.com/video.mp4?xsec_token=secret" /></video>
      </article>
    </body></html>
    """
    page = tmp_path / "xhs.html"
    page.write_text(html, encoding="utf-8")
    loaded = snapshot_item(page, url)
    detail = create_default_registry().adapter_for(url).resolve_items(url)[0]
    assert loaded.platform == "xiaohongshu" and loaded.source_type == "mixed"
    assert source_id_for(loaded) == source_id_for(detail)
    result = SnapshotProvider().extract(BackendExtractRequest(item=loaded, backend_chain=("page_snapshot.defuddle",)))
    assert result.status == "verified"
    assert result.title == "页面标题" and result.author == "公开作者"
    assert result.published_at == "2026-11-19T09:30:00+08:00"
    assert "这是笔记正文" in result.text and "#效率" in result.text
    assert result.metadata["images"] == ["https://sns-img-bd.xhscdn.com/abc.jpg", "https://sns-img-bd.xhscdn.com/def.jpg"]
    assert result.metadata["video_url"] == "https://sns-video-bd.xhscdn.com/video.mp4"


def test_xiaohongshu_snapshot_rejects_login_wall(tmp_path):
    from openmd.backends.loaded_chat import snapshot_item

    url = "https://www.xiaohongshu.com/explore/fixture"
    page = tmp_path / "login.html"
    page.write_text('<html><body><button>登录</button></body></html>', encoding="utf-8")
    item = snapshot_item(page, url)
    result = SnapshotProvider().extract(BackendExtractRequest(item=item, backend_chain=("page_snapshot.defuddle",)))
    assert result.error_code == "login_required"


def test_ordinary_snapshot_with_login_button_and_full_body_is_verified(tmp_path):
    from openmd.backends.loaded_chat import snapshot_item

    page = tmp_path / "article.html"
    page.write_text("""<html><head><title>完整文章</title></head><body>
      <header><button>登录</button></header>
      <main><article><p>这是一段完整且可保存的文章正文。</p></article></main>
    </body></html>""", encoding="utf-8")
    item = snapshot_item(page, "https://example.org/article")
    result = SnapshotProvider().extract(BackendExtractRequest(item=item, backend_chain=("page_snapshot.defuddle",)))
    assert result.status == "verified"
    assert "完整且可保存" in result.text


def test_xiaohongshu_snapshot_with_login_button_and_full_note_is_verified(tmp_path):
    from openmd.backends.loaded_chat import snapshot_item

    page = tmp_path / "note.html"
    page.write_text("""<html><body>
      <header><button>登录</button></header>
      <article data-testid="note-detail">
        <h1 data-testid="note-title">一篇完整笔记</h1>
        <div data-testid="note-content"><p>这里是完整的笔记正文。</p></div>
      </article>
    </body></html>""", encoding="utf-8")
    item = snapshot_item(page, "https://www.xiaohongshu.com/explore/fixture")
    result = SnapshotProvider().extract(BackendExtractRequest(item=item, backend_chain=("page_snapshot.defuddle",)))
    assert result.status == "verified"
    assert result.title == "一篇完整笔记"
    assert "完整的笔记正文" in result.text


def test_douyin_snapshot_is_rejected_as_no_transcript(tmp_path):
    from openmd.backends.loaded_chat import snapshot_item

    page = tmp_path / "douyin.html"
    page.write_text("<html><body><main>视频标题和页面文案，不是逐字稿</main></body></html>", encoding="utf-8")
    item = snapshot_item(page, "https://www.douyin.com/video/123456")
    result = SnapshotProvider().extract(BackendExtractRequest(item=item, backend_chain=("page_snapshot.defuddle",)))
    assert result.status == "failed"
    assert result.error_code == "douyin_snapshot_no_transcript"
    assert result.text == ""


def test_xiaohongshu_snapshot_rejects_loaded_recommendation_feed(tmp_path):
    from openmd.backends.loaded_chat import snapshot_item

    url = "https://www.xiaohongshu.com/explore/fixture123"
    page = tmp_path / "feed.html"
    page.write_text("""
    <html><body><main>
      <a href="/explore/other1">一</a><a href="/explore/other2">二</a>
      <a href="/explore/other3">三</a><a href="/explore/other4">四</a>
    </main></body></html>
    """, encoding="utf-8")
    item = snapshot_item(page, url)
    result = SnapshotProvider().extract(BackendExtractRequest(item=item, backend_chain=("page_snapshot.defuddle",)))
    assert result.error_code == "xiaohongshu_loaded_page_mismatch"


def test_snapshot_host_rejects_bad_and_oversized_messages(tmp_path):
    bad = handle({"type": "save_snapshot", "html": "<p>x</p>", "url": "file:///x", "save_to": str(tmp_path)})
    assert not bad["ok"]
    huge = handle({"type": "save_snapshot", "html": "x" * (50 * 1024 * 1024 + 1), "url": "https://example.org", "save_to": str(tmp_path)})
    assert not huge["ok"] and "snapshot_too_large" in huge["error"]


def test_snapshot_host_queues_private_temp_file_and_command(tmp_path, monkeypatch):
    from openmd import browser_host
    monkeypatch.setenv("OPENMD_DATA_DIR", str(tmp_path / "data"))
    job = browser_host.start_snapshot_job("<html><body>snapshot</body></html>", "https://example.org/private", "Private", str(tmp_path), spawn=lambda *args, **kwargs: None)
    assert job["snapshot"] is True and Path(job["snapshot_path"]).is_file()
