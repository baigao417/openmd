"""Discourse extraction contract using synthetic engine payloads only."""

import json
from urllib.parse import parse_qs, urlparse

import pytest

from openmd.backends.base import BackendExtractRequest
from openmd.backends.discourse import BrowserJsonLease, DiscourseProvider, _post_body, discourse_html_evidence, topic_target
from openmd.backends.executor import BackendExecutor
from openmd.backends.registry import BackendRuntimeOptions, create_runtime_backend_provider_registry
from openmd.core.models import ContentItem, TranscriptResult


NETWORK = BackendRuntimeOptions(enable_network_backends=True)
BROWSER = BackendRuntimeOptions(enable_browser_backends=True)
URL = "https://forum.example/community/t/a-topic/123"


def post(post_id=101, number=1, raw="合法短帖", **fields):
    return {"id": post_id, "post_number": number, "raw": raw, "username": "author", "created_at": "2026-11-10T00:00:00Z", **fields}


def payload(posts=None, stream=None):
    posts = [post()] if posts is None else posts
    return {"id": 123, "title": "标题", "post_stream": {"posts": posts, "stream": stream or [entry["id"] for entry in posts]}, "suggested_topics": [{"title": "推荐话题，不是正文"}]}


def extract(provider, url=URL, **raw_meta):
    return provider.extract(BackendExtractRequest(ContentItem(platform="web_article", input_type="single", url=url, raw_meta=raw_meta), (provider.name,)))


@pytest.mark.parametrize("url,expected", [
    ("https://forum.example/t/123", ("", 123, 1)),
    ("https://forum.example/t/123/7", ("", 123, 7)),
    ("https://forum.example/t/topic/123", ("", 123, 1)),
    ("https://forum.example/t/topic/123/7", ("", 123, 7)),
    ("https://forum.example/community/t/topic/123/7", ("/community", 123, 7)),
])
def test_url_keeps_topic_floor_and_subdirectory_separate(url, expected):
    target = topic_target(url)
    assert (target.prefix, target.topic_id, target.post_number) == expected
    assert target.origin == "https://forum.example"


def test_topic_path_alone_is_not_engine_detection():
    provider = DiscourseProvider(NETWORK, fetcher=lambda url: (200, "<article>ordinary /t/ article</article>"))
    value = extract(provider)
    assert value.error_code == "discourse_not_detected"
    assert "engine" not in value.metadata


@pytest.mark.parametrize("html", [
    '<meta name="generator" content="Discourse 3.5">',
    '<meta content="Discourse" name="generator">',
    '<div id="discourse-application"></div><script id="data-preloaded"></script>',
])
def test_engine_html_markers_are_structural(html):
    assert discourse_html_evidence(html)
    assert not discourse_html_evidence("An article mentioning Discourse")
    assert not discourse_html_evidence('<div id="discourse-application"></div>')


def test_public_json_default_is_exact_main_raw_without_navigation():
    original = "回复\n\n**分享**。\n\n```py\nprint('原文')\n```"
    provider = DiscourseProvider(NETWORK, fetcher=lambda url: (200, payload([post(raw=original), post(102, 2, "第二楼")])) )
    value = extract(provider)
    assert value.status == "verified"
    assert value.text == original + "\n"
    assert "推荐话题" not in value.text and "第二楼" not in value.text
    assert value.metadata["post_id"] == value.metadata["target_post_id"] == 101
    assert value.metadata["scope"] == "main_post"


def test_floor_url_selects_requested_post_and_json_floor_endpoint():
    calls = []
    def fetch(url):
        calls.append(url)
        return 200, payload([post(), post(107, 7, "目标七楼")])
    value = extract(DiscourseProvider(NETWORK, fetcher=fetch), URL + "/7")
    assert value.text == "目标七楼\n"
    assert calls == ["https://forum.example/community/t/123/7.json?include_raw=true"]
    assert value.metadata["post_id"] == 107


def test_cooked_markdown_preserves_rich_content_and_absolute_links():
    cooked = '<p>第一段 <strong>加粗</strong> <a href="/topic">链接</a></p><p><img src="uploads/image.png" alt="图"></p><pre><code class="language-python">a = 1\n\nprint(a)\n</code></pre><blockquote><p>引用一</p><p>引用二</p></blockquote><table><thead><tr><th>甲</th><th>乙</th></tr></thead><tbody><tr><td>一</td><td>二</td></tr></tbody></table>'
    value = extract(DiscourseProvider(NETWORK, fetcher=lambda url: (200, payload([post(raw="", cooked=cooked)]))))
    assert value.status == "verified"
    for wanted in ["**加粗**", "[链接](https://forum.example/topic)", "![图](https://forum.example/community/uploads/image.png)", "```python\na = 1\n\nprint(a)\n", "> 引用一", "> 引用二", "| 甲 | 乙 |", "| --- | --- |", "| 一 | 二 |"]:
        assert wanted in value.text


def test_raw_relative_image_and_link_are_absolutized():
    body = "[链接](/t/other/1) ![图](uploads/a.png)"
    converted = _post_body(post(raw=body), "https://forum.example/community/")
    assert converted == "[链接](https://forum.example/t/other/1) ![图](https://forum.example/community/uploads/a.png)"


def test_raw_code_examples_stay_literal_and_upload_images_resolve():
    body = "```md\n[示例](/a)\n```\n`[示例](/b)`\n    [缩进](/c)\n![图](upload://hash.png)"
    converted = _post_body(post(raw=body), "https://forum.example/community/")
    assert "[示例](/a)" in converted and "`[示例](/b)`" in converted and "    [缩进](/c)" in converted
    assert "![图](https://forum.example/community/uploads/short-url/hash.png)" in converted


def test_public_rate_limit_does_not_switch_to_browser():
    class NeverBrowser:
        engine = False
        def read(self, path):
            pytest.fail("429 must be respected")
        def close(self):
            pass
    options = BackendRuntimeOptions(enable_network_backends=True, enable_browser_backends=True)
    def fetch(url):
        return (429, None) if ".json" in url else (200, '<meta name="generator" content="Discourse">')
    value = extract(DiscourseProvider(options, fetcher=fetch, browser_factory=lambda url: NeverBrowser()))
    assert value.error_code == "discourse_rate_limited"


def test_empty_engine_shell_is_failed_without_word_blacklist():
    provider = DiscourseProvider(NETWORK, fetcher=lambda url: (200, payload([post(raw="", cooked="")])) )
    value = extract(provider)
    assert value.error_code == "discourse_post_body_missing"
    assert value.metadata["engine"] == "discourse"
    assert not value.has_usable_text


@pytest.mark.parametrize("status,code", [(401, "discourse_auth_required"), (403, "discourse_permission_denied"), (429, "discourse_rate_limited")])
def test_access_failure_is_not_a_successful_article(status, code):
    def fetch(url):
        return (status, {"errors": ["not accessible"]}) if ".json" in url else (200, '<meta name="generator" content="Discourse">')
    value = extract(DiscourseProvider(NETWORK, fetcher=fetch))
    assert value.status == "failed" and value.error_code == code
    assert value.metadata["engine"] == "discourse"


def test_public_denied_then_browser_same_origin_json_and_cleanup():
    class Browser:
        engine = True
        closed = False
        paths = []
        def read(self, path):
            self.paths.append(path)
            return 200, payload()
        def close(self):
            self.closed = True
    lease = Browser()
    options = BackendRuntimeOptions(enable_network_backends=True, enable_browser_backends=True)
    provider = DiscourseProvider(options, fetcher=lambda url: (403, None), browser_factory=lambda url: lease)
    value = extract(provider)
    assert value.status == "verified" and value.metadata["strategy"] == "browser_same_origin"
    assert lease.closed


def test_browser_bridge_reads_json_only_in_origin_and_releases_session():
    calls = []
    def runner(args, *, timeout):
        calls.append(args)
        if args[1:3] == ["browser", "eval"]:
            return 0, json.dumps({"ok": True, "data": {"status": 200, "engine": True, "payload": payload()}}), ""
        return 0, "{}", ""
    lease = BrowserJsonLease(URL, runner=runner)
    assert lease.read("/community/t/123.json")[0] == 200
    lease.close()
    assert [args[1:3] for args in calls] == [["browser", "open"], ["browser", "eval"], ["browser", "eval"], ["browser", "close"]]
    script = calls[2][3]
    assert "location.origin !==" in script and "credentials: 'same-origin'" in script
    assert "cookie" not in script.lower()
    assert all("-f" not in args for args in calls)
    assert calls[0][calls[0].index("--session") + 1] == calls[-1][calls[-1].index("--session") + 1]
    assert all("\n" not in args[3] for args in calls if args[1:3] == ["browser", "eval"])


def test_positive_dom_engine_survives_later_browser_json_failure():
    calls = []
    def runner(args, *, timeout):
        calls.append(args)
        if args[1:3] != ["browser", "eval"]:
            return 0, "{}", ""
        if "return {engine};" in args[3]:
            return 0, json.dumps({"ok": True, "data": {"engine": True}}), ""
        return 1, "", "read failed"
    lease = BrowserJsonLease(URL, runner=runner)
    value = extract(DiscourseProvider(BROWSER, browser_factory=lambda url: lease))
    assert value.status == "failed"
    assert value.metadata["engine"] == "discourse"
    assert calls[-1][1:3] == ["browser", "close"]


def test_default_browser_runner_uses_existing_resolved_command_wrapper(monkeypatch):
    from openmd.backends import web_article
    calls = []
    def command(args, timeout):
        calls.append(args)
        if args[1:3] == ["browser", "eval"]:
            return 0, json.dumps({"ok": True, "data": {"status": 200, "engine": True, "payload": payload()}}), ""
        return 0, "{}", ""
    monkeypatch.setattr(web_article, "_run_command", command)
    lease = BrowserJsonLease(URL)
    assert lease.read("/community/t/123.json")[0] == 200
    lease.close()
    assert len(calls) == 4


def test_all_posts_fetches_stream_chunks_deduplicates_and_sorts():
    all_entries = [post(index, index - 99, f"正文{index}") for index in range(100, 108)]
    calls = []
    def fetch(url):
        calls.append(url)
        if "/posts.json?" not in url:
            return 200, payload(all_entries[:2], list(range(100, 108)))
        ids = [int(value) for value in parse_qs(urlparse(url).query)["post_ids[]"]]
        return 200, payload([entry for entry in reversed(all_entries) if entry["id"] in ids] * 2)
    value = extract(DiscourseProvider(NETWORK, fetcher=fetch, scope="all_posts", chunk_size=2))
    assert value.status == "verified"
    assert value.metadata["coverage"] == {"expected_count": 8, "fetched_count": 8, "missing_count": 0, "initial_count": 2, "request_count": 4, "missing_post_ids": [], "complete": True}
    assert len(calls) == 4
    assert value.text.count("正文107") == 1
    assert value.text.index("正文100") < value.text.index("正文107")


def test_all_scope_records_missing_ids_and_partial_status():
    def fetch(url):
        return (200, payload([post()], [101, 102])) if "/posts.json" not in url else (200, payload([]))
    value = extract(DiscourseProvider(NETWORK, fetcher=fetch), discourse_scope="all_posts")
    assert value.status == "partial"
    assert value.metadata["coverage"]["missing_post_ids"] == [102]
    assert value.metadata["coverage"]["complete"] is False


def test_known_engine_failure_cannot_be_replaced_by_generic_shell():
    discourse = DiscourseProvider(NETWORK, fetcher=lambda url: (200, payload([post(raw="")])))
    class Generic:
        def extract(self, request):
            return TranscriptResult(text="# Forum\n\nSuggested topics", status="verified", source_level="native_text", backend="generic")
    executor = BackendExecutor(providers={discourse.name: discourse, "generic": Generic()})
    value = executor.extract(ContentItem(platform="web_article", input_type="single", url=URL), (discourse.name, "generic"))
    assert value.status == "failed"
    assert value.error_code == "discourse_post_body_missing"


def test_provider_registration_has_no_network_or_browser_without_gates():
    assert not isinstance(create_runtime_backend_provider_registry("web_article", BackendRuntimeOptions()).provider_for("web_article.discourse"), DiscourseProvider)
    assert isinstance(create_runtime_backend_provider_registry("web_article", NETWORK).provider_for("web_article.discourse"), DiscourseProvider)
    assert isinstance(create_runtime_backend_provider_registry("web_article", BROWSER).provider_for("web_article.discourse"), DiscourseProvider)
