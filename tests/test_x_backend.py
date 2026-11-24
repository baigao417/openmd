"""X/Twitter runtime backend regression tests."""

from __future__ import annotations

import unittest

from openmd.backends.base import BackendExtractRequest
from openmd.backends.executor import SkeletonBackendProvider
from openmd.backends.registry import BackendRuntimeOptions, create_runtime_backend_provider_registry
from openmd.backends.x import (
    FXTWITTER_BACKEND,
    OPENCLI_TWITTER_ARTICLE_BACKEND,
    OPENCLI_TWITTER_THREAD_BACKEND,
    OPENCLI_TWITTER_TWEETS_BACKEND,
    OpenCliTwitterTextProvider,
    XPublicLayeredProvider,
    classify_twitter_failure,
)
from openmd.core.models import ContentItem, TranscriptResult


def test_table_thread_title_uses_requested_post_and_preserves_body(tmp_path):
    import json
    from openmd.core.filename import filename_base_for_item
    from openmd.core.output import write_item_result
    from openmd.core.renderer import render_markdown_document

    text = (
        "| id | author | text | url |\n| --- | --- | --- | --- |\n"
        "| 999 | reply | A reply | https://x.com/reply/status/999 |\n"
        "| 123 | @writer | 第一行\\|原文<br>下一行 | https://x.com/writer/status/123 |\n"
    )
    item = ContentItem(platform="x", input_type="single", url="https://x.com/writer/status/123", source_type="article")
    result = OpenCliTwitterTextProvider(runner=lambda *a, **k: (0, text, "")).extract(
        BackendExtractRequest(item=item, backend_chain=(OPENCLI_TWITTER_THREAD_BACKEND,))
    )
    assert result.title == "第一行|原文 下一行"
    assert result.author == "@writer"
    assert result.text == text
    document = render_markdown_document(item, result)
    assert document.frontmatter["title"] == result.title
    written = write_item_result(item, result, tmp_path)
    assert written.stem == filename_base_for_item(result.title, item_id=item.id, url=item.url)
    saved_title = next(line.removeprefix("title: ") for line in written.read_text(encoding="utf-8").splitlines() if line.startswith("title: "))
    assert json.loads(saved_title) == result.title


def test_table_thread_title_drops_trailing_short_link():
    text = (
        "| id | author | text | url |\n| --- | --- | --- | --- |\n"
        "| 123 | @writer | A clean first sentence https://t.co/abc123 | https://x.com/writer/status/123 |\n"
    )
    item = ContentItem(platform="x", input_type="single", url="https://x.com/writer/status/123", source_type="article")
    result = OpenCliTwitterTextProvider(runner=lambda *a, **k: (0, text, "")).extract(
        BackendExtractRequest(item=item, backend_chain=(OPENCLI_TWITTER_THREAD_BACKEND,))
    )
    assert result.title == "A clean first sentence"


def test_thread_without_title_has_stable_post_fallback():
    item = ContentItem(platform="x", input_type="single", url="https://x.com/i/status/123", source_type="article")
    result = OpenCliTwitterTextProvider(runner=lambda *a, **k: (0, "Plain text", "")).extract(
        BackendExtractRequest(item=item, backend_chain=(OPENCLI_TWITTER_THREAD_BACKEND,))
    )
    assert result.title == "X-123"


class XOpenCliTextProviderTests(unittest.TestCase):
    def test_fxembed_preserves_research_metadata_thread_and_quote(self) -> None:
        payload = {
            "status": {
                "url": "https://x.com/writer/status/123",
                "id": "123",
                "text": "Root post",
                "author": {"name": "Writer", "screen_name": "writer"},
                "created_at": "Mon Jun 01 04:40:25 +0000 2026",
                "views": 1200,
                "likes": 34,
                "reposts": 12,
                "replies": 5,
                "bookmarks": 7,
                "quote": {
                    "url": "https://x.com/quoted/status/456",
                    "raw_text": {"text": "Quoted body"},
                    "author": {"name": "Quoted", "screen_name": "quoted"},
                    "created_at": "Sun May 31 03:00:00 +0000 2026",
                },
            },
            "thread": [
                {
                    "url": "https://x.com/writer/status/123",
                    "id": "123",
                    "text": "Root post",
                    "author": {"name": "Writer", "screen_name": "writer"},
                    "created_at": "Mon Jun 01 04:40:25 +0000 2026",
                },
                {
                    "url": "https://x.com/writer/status/124",
                    "id": "124",
                    "text": "Continuation",
                    "author": {"name": "Writer", "screen_name": "writer"},
                    "created_at": "Mon Jun 01 04:41:25 +0000 2026",
                },
            ],
        }
        provider = XPublicLayeredProvider(fetch_json=lambda url, **kw: (200, payload))
        item = ContentItem(platform="x", input_type="single", url="https://x.com/writer/status/123", source_type="article")
        result = provider.extract(BackendExtractRequest(item=item, backend_chain=(FXTWITTER_BACKEND,)))

        assert result.status == "verified"
        assert result.metadata["author_handle"] == "@writer"
        assert result.published_at == "2026-06-01T04:40:25+00:00"
        assert result.metadata["views"] == 1200
        assert result.metadata["likes"] == 34
        assert result.metadata["reposts"] == 12
        assert result.metadata["replies"] == 5
        assert result.metadata["bookmarks"] == 7
        assert result.metadata["thread_count"] == 2
        assert result.metadata["thread_coverage"] == "author_thread"
        assert result.metadata["thread"][1] == {
            "url": "https://x.com/writer/status/124",
            "author": "Writer",
            "author_handle": "@writer",
            "published_at": "2026-06-01T04:41:25+00:00",
            "text": "Continuation",
        }
        assert result.metadata["quote"] == {
            "url": "https://x.com/quoted/status/456",
            "author": "Quoted",
            "author_handle": "@quoted",
            "published_at": "2026-05-31T03:00:00+00:00",
            "text": "Quoted body",
        }
        assert "## Thread" in result.text
        assert "> 引用推文：" in result.text

    def test_fxembed_omits_missing_counts_and_marks_partial_thread(self) -> None:
        payload = {
            "tweet": {
                "url": "https://x.com/writer/status/123",
                "text": "One post",
                "author": {"name": "Writer", "screen_name": "writer"},
                "created_at": "2026-06-01T04:40:25Z",
                "likes": 0,
            }
        }
        provider = XPublicLayeredProvider(fetch_json=lambda url, **kw: (200, payload))
        result = provider.extract(BackendExtractRequest(
            item=ContentItem(platform="x", input_type="single", url="https://x.com/writer/status/123", source_type="article"),
            backend_chain=(FXTWITTER_BACKEND,),
        ))
        assert result.status == "verified"
        assert result.metadata["thread_count"] == 1
        assert result.metadata["thread_coverage"] == "partial"
        assert result.metadata["likes"] == 0
        assert "views" not in result.metadata
        assert "reposts" not in result.metadata

    def test_x_metadata_is_rendered_as_parseable_frontmatter(self) -> None:
        from openmd.core.renderer import render_document, render_markdown_document

        result = TranscriptResult(
            text="Root post\n",
            status="verified",
            source_level="native_text",
            backend=FXTWITTER_BACKEND,
            title="Root post",
            author="Writer",
            published_at="2026-06-01T04:40:25+00:00",
            metadata={
                "author_handle": "@writer",
                "views": 1200,
                "thread_count": 2,
                "thread_coverage": "author_thread",
                "thread": [{"url": "https://x.com/writer/status/123", "text": "Root post"}],
                "quote": {"url": "https://x.com/quoted/status/456", "text": "Quoted body"},
            },
        )
        item = ContentItem(platform="x", input_type="single", url="https://x.com/writer/status/123", source_type="article")
        rendered = render_document(render_markdown_document(item, result))
        assert 'published_at: "2026-06-01T04:40:25+00:00"' in rendered
        assert 'author_handle: "@writer"' in rendered
        assert 'thread_count: 2' in rendered
        assert 'thread: [{"url": "https://x.com/writer/status/123", "text": "Root post"}]' in rendered
        assert 'quote: {"url": "https://x.com/quoted/status/456", "text": "Quoted body"}' in rendered

    def test_disable_fxembed_keeps_syndication_and_skips_hosted_apis(self) -> None:
        import os
        calls = []
        provider = XPublicLayeredProvider(fetch_json=lambda url, **kw: (calls.append(url) or (200, {"id_str": "123", "text": "x" * 194})))
        old = os.environ.get("OPENMD_X_DISABLE_FXEMBED")
        os.environ["OPENMD_X_DISABLE_FXEMBED"] = "1"
        try:
            result = provider.extract(BackendExtractRequest(
                item=ContentItem(platform="x", input_type="single", url="https://x.com/writer/status/123", source_type="article"),
                backend_chain=(FXTWITTER_BACKEND,),
            ))
        finally:
            if old is None:
                os.environ.pop("OPENMD_X_DISABLE_FXEMBED", None)
            else:
                os.environ["OPENMD_X_DISABLE_FXEMBED"] = old
        self.assertEqual(result.status, "failed")
        self.assertEqual(len(calls), 1)
        self.assertIn("syndication", result.metadata["x_public_attempts"][0])

    def test_rich_fxembed_v2_is_attempted_before_lighter_public_layers(self) -> None:
        calls = []
        provider = XPublicLayeredProvider(fetch_json=lambda url, **kw: (calls.append(kw["layer"]) or (200, {
            "status": {
                "text": "Short body",
                "author": {"name": "Writer", "screen_name": "writer"},
                "created_at": "2026-11-20T12:00:00Z",
            },
            "thread": [],
        })))
        result = provider.extract(BackendExtractRequest(
            item=ContentItem(platform="x", input_type="single", url="https://x.com/writer/status/123", source_type="article"),
            backend_chain=(FXTWITTER_BACKEND,),
        ))
        assert result.status == "verified"
        assert calls == ["fxembed_v2"]

    def test_opencli_title_does_not_use_author_plus_body_summary(self) -> None:
        provider = OpenCliTwitterTextProvider(runner=lambda *a, **k: (0, "# 作者：正文前 56 字…\n\n正文实际首句。完整文本继续。", ""))
        result = provider.extract(BackendExtractRequest(
            item=ContentItem(platform="x", input_type="single", url="https://x.com/writer/status/123", source_type="article"),
            backend_chain=(OPENCLI_TWITTER_THREAD_BACKEND,),
        ))
        self.assertEqual(result.title, "正文实际首句。完整文本继续。")

    def test_opencli_usable_stdout_survives_cleanup_exit_warning(self) -> None:
        provider = OpenCliTwitterTextProvider(
            runner=lambda *a, **k: (1, "# Clean title\n\nValid public body", "Owned-tab cleanup failed")
        )
        result = provider.extract(BackendExtractRequest(
            item=ContentItem(platform="x", input_type="single", url="https://x.com/writer/status/123", source_type="article"),
            backend_chain=(OPENCLI_TWITTER_THREAD_BACKEND,),
        ))
        self.assertEqual(result.status, "verified")
        self.assertIn("cleanup", " ".join(result.source_notes).lower())
        self.assertEqual(result.text, "# Clean title\n\nValid public body\n")

    def test_opencli_cleanup_warning_does_not_replace_original_failure(self) -> None:
        provider = OpenCliTwitterTextProvider(
            runner=lambda *a, **k: (1, "", "login required; Owned-tab cleanup failed")
        )
        result = provider.extract(BackendExtractRequest(
            item=ContentItem(platform="x", input_type="single", url="https://x.com/writer/status/123", source_type="article"),
            backend_chain=(OPENCLI_TWITTER_THREAD_BACKEND,),
        ))
        self.assertEqual(result.error_code, "x_auth_required")

    def test_fxembed_truncated_result_falls_through_to_syndication(self) -> None:
        responses = iter([
            (200, {"tweet": {"text": "x" * 194, "truncated": True, "author": {"name": "Writer", "screen_name": "writer"}}}),
            (200, {"id_str": "123", "text": "Complete long article body", "user": {"screen_name": "writer", "name": "Writer"}, "created_at": "2026-11-20T12:00:00Z", "media": {"photos": [{"url": "https://pbs.twimg.com/media/a.jpg"}]}}),
        ])
        provider = XPublicLayeredProvider(fetch_json=lambda url, **kw: next(responses))
        result = provider.extract(BackendExtractRequest(
            item=ContentItem(platform="x", input_type="single", url="https://x.com/writer/status/123", source_type="article"),
            backend_chain=(FXTWITTER_BACKEND,),
        ))
        self.assertEqual(result.status, "verified")
        self.assertIn("Complete long article body", result.text)
        self.assertIn("https://pbs.twimg.com/media/a.jpg", result.text)
        self.assertEqual(result.title, "Complete long article body")
        self.assertEqual(result.author, "Writer")

    def test_x_article_uses_article_title_and_full_blocks(self) -> None:
        payload = {"tweet": {"text": "Short preview", "article": {"title": "The Article Title", "content": {"blocks": [{"type": "header-two", "text": "Section"}, {"type": "unstyled", "text": "Full article paragraph with link." , "entityRanges": [{"offset": 28, "length": 4, "key": 0}]}, {"type": "atomic", "text": "", "entityRanges": [{"offset": 0, "length": 1, "key": 1}]}], "entityMap": {"0": {"type": "LINK", "data": {"url": "https://example.test/source"}}, "1": {"type": "MEDIA", "data": {"mediaItems": [{"mediaId": "m1"}]}}}}, "media_entities": [{"media_id": "m1", "media_info": {"original_img_url": "https://pbs.twimg.com/media/article.jpg"}}]}, "author": {"name": "Writer"}}}
        provider = XPublicLayeredProvider(fetch_json=lambda url, **kw: (200, payload))
        result = provider.extract(BackendExtractRequest(item=ContentItem(platform="x", input_type="single", url="https://x.com/writer/status/123", source_type="article"), backend_chain=(FXTWITTER_BACKEND,)))
        self.assertEqual(result.status, "verified")
        self.assertEqual(result.title, "The Article Title")
        self.assertIn("## Section\n\nFull article paragraph with [link](https://example.test/source).", result.text)
        self.assertNotIn("Short preview", result.text)
        self.assertIn("[link](https://example.test/source)", result.text)
        self.assertIn("![](https://pbs.twimg.com/media/article.jpg)", result.text)

    def test_all_public_layers_failed_falls_through_to_opencli(self) -> None:
        from openmd.backends.executor import BackendExecutor
        from openmd.core.models import TranscriptResult

        provider = XPublicLayeredProvider(fetch_json=lambda url, **kw: (503, {}))
        class BrowserFallback:
            def extract(self, request):
                return TranscriptResult(text="# Browser title\n\nReadable text", status="verified", source_level="native_text", backend=request.primary_backend)
        item = ContentItem(platform="x", input_type="single", url="https://x.com/writer/status/123", source_type="article")
        result = BackendExecutor(providers={FXTWITTER_BACKEND: provider, OPENCLI_TWITTER_THREAD_BACKEND: BrowserFallback()}).extract(item, (FXTWITTER_BACKEND, OPENCLI_TWITTER_THREAD_BACKEND))
        self.assertEqual(result.status, "verified")
        self.assertEqual(result.backend, OPENCLI_TWITTER_THREAD_BACKEND)

    def test_failed_public_layers_keep_attempts_and_actionable_next_step(self) -> None:
        from openmd.backends.executor import BackendExecutor
        provider = XPublicLayeredProvider(fetch_json=lambda url, **kw: (503, {}))
        browser = OpenCliTwitterTextProvider(runner=lambda *a, **k: (124, "", "OpenCLI command timed out after 60 seconds."))
        result = BackendExecutor(providers={FXTWITTER_BACKEND: provider, OPENCLI_TWITTER_THREAD_BACKEND: browser}).extract(
            ContentItem(platform="x", input_type="single", url="https://x.com/writer/status/123", source_type="article"),
            (FXTWITTER_BACKEND, OPENCLI_TWITTER_THREAD_BACKEND),
        )
        self.assertEqual(result.status, "failed")
        self.assertIn(FXTWITTER_BACKEND, [a["backend"] for a in result.metadata["fallback_attempts"]])
        self.assertIn("Next step:", result.error_summary or "")

    def test_public_api_failure_categories_are_preserved(self) -> None:
        cases = (
            (404, {}, "x_not_found"),
            (403, {}, "x_access_denied"),
            (429, {}, "x_rate_limited"),
            (503, {}, "x_network_failed"),
            (200, {"code": 404, "message": "Tweet not found"}, "x_not_found"),
            (200, {"code": 403, "message": "Protected post"}, "x_access_denied"),
            (200, {"code": 429, "message": "Rate limited"}, "x_rate_limited"),
        )
        for status, payload, expected in cases:
            with self.subTest(status=status, payload=payload):
                provider = XPublicLayeredProvider(fetch_json=lambda url, **kw: (status, payload))
                result = provider.extract(BackendExtractRequest(
                    item=ContentItem(platform="x", input_type="single", url="https://x.com/writer/status/123", source_type="article"),
                    backend_chain=(FXTWITTER_BACKEND,),
                ))
                self.assertEqual(result.status, "failed")
                self.assertIn(expected, result.metadata["x_public_attempts"][-1])
                self.assertIn("Next step:", result.error_summary or "")

    def test_thread_provider_uses_post_id_and_does_not_pass_query_tokens(self) -> None:
        calls: list[tuple[list[str], int]] = []

        def fake_runner(args: list[str], *, timeout: int = 120) -> tuple[int, str, str]:
            calls.append((args, timeout))
            return 0, "# Thread\n\nFirst post\n\nSecond post", ""

        provider = OpenCliTwitterTextProvider(runner=fake_runner, timeout=7)
        item = ContentItem(
            platform="x",
            input_type="single",
            url="https://x.com/openmd/status/1234567890?token=secret",
            source_type="article",
        )

        result = provider.extract(
            BackendExtractRequest(
                item=item,
                backend_chain=(OPENCLI_TWITTER_THREAD_BACKEND,),
                allow_fallback=False,
            )
        )

        self.assertTrue(result.has_usable_text)
        self.assertEqual(result.status, "verified")
        self.assertEqual(result.source_level, "native_text")
        self.assertEqual(result.backend, OPENCLI_TWITTER_THREAD_BACKEND)
        self.assertEqual(result.title, "Thread")
        self.assertEqual(result.metadata["tweet_id"], "1234567890")
        self.assertEqual(result.metadata["command"], "thread")
        self.assertEqual(
            calls,
            [
                (
                    [
                        "opencli",
                        "twitter",
                        "thread",
                        "1234567890",
                        "--limit",
                        "50",
                        "-f",
                        "md",
                        "--window",
                        "background",
                        "--site-session",
                        "ephemeral",
                        "--keep-tab",
                        "false",
                    ],
                    7,
                )
            ],
        )
        self.assertNotIn("token=secret", " ".join(calls[0][0]))
        self.assertIn("No follow/unfollow/post/list/account mutation was attempted.", result.source_notes)
        for raw_key in ("stdout", "stderr", "raw_stdout", "raw_stderr"):
            self.assertNotIn(raw_key, result.metadata)

    def test_default_thread_backend_routes_user_profile_to_tweets_command(self) -> None:
        calls: list[tuple[list[str], int]] = []

        def fake_runner(args: list[str], *, timeout: int = 120) -> tuple[int, str, str]:
            calls.append((args, timeout))
            return 0, "# @openmd tweets\n\n- first public post\n- second public post", ""

        provider = OpenCliTwitterTextProvider(runner=fake_runner, timeout=9)
        item = ContentItem(
            platform="x",
            input_type="single",
            url="https://twitter.com/openmd",
            source_type="article",
            raw_meta={"limit": 3},
        )

        result = provider.extract(
            BackendExtractRequest(
                item=item,
                backend_chain=(OPENCLI_TWITTER_THREAD_BACKEND,),
                allow_fallback=False,
            )
        )

        self.assertTrue(result.has_usable_text)
        self.assertEqual(result.backend, OPENCLI_TWITTER_TWEETS_BACKEND)
        self.assertEqual(result.metadata["username"], "openmd")
        self.assertEqual(result.metadata["limit"], 3)
        self.assertEqual(
            calls,
            [
                (
                    [
                        "opencli",
                        "twitter",
                        "tweets",
                        "openmd",
                        "--limit",
                        "3",
                        "-f",
                        "md",
                        "--window",
                        "background",
                        "--site-session",
                        "ephemeral",
                        "--keep-tab",
                        "false",
                    ],
                    9,
                )
            ],
        )

    def test_explicit_article_backend_uses_article_command(self) -> None:
        calls: list[tuple[list[str], int]] = []

        def fake_runner(args: list[str], *, timeout: int = 120) -> tuple[int, str, str]:
            calls.append((args, timeout))
            return 0, "# Long-form Article\n\nArticle body from X.", ""

        provider = OpenCliTwitterTextProvider(
            default_backend=OPENCLI_TWITTER_ARTICLE_BACKEND,
            runner=fake_runner,
            timeout=11,
        )
        item = ContentItem(
            platform="x",
            input_type="single",
            url="https://x.com/openmd/status/999999",
            source_type="article",
        )

        result = provider.extract(
            BackendExtractRequest(
                item=item,
                backend_chain=(OPENCLI_TWITTER_ARTICLE_BACKEND,),
                allow_fallback=False,
            )
        )

        self.assertTrue(result.has_usable_text)
        self.assertEqual(result.backend, OPENCLI_TWITTER_ARTICLE_BACKEND)
        self.assertEqual(result.title, "Long-form Article")
        self.assertEqual(result.metadata["command"], "article")
        self.assertEqual(
            calls,
            [
                (
                    [
                        "opencli",
                        "twitter",
                        "article",
                        "999999",
                        "-f",
                        "md",
                        "--window",
                        "background",
                        "--site-session",
                        "ephemeral",
                        "--keep-tab",
                        "false",
                    ],
                    11,
                )
            ],
        )

    def test_failure_classification_and_sanitization(self) -> None:
        self.assertEqual(
            classify_twitter_failure("login required; cookie is missing"),
            "x_auth_required",
        )
        self.assertEqual(classify_twitter_failure("429 too many requests"), "x_rate_limited")
        self.assertEqual(classify_twitter_failure("Tweet not found"), "x_not_found")
        self.assertEqual(classify_twitter_failure("protected account"), "x_access_denied")
        self.assertEqual(classify_twitter_failure("command timed out"), "opencli_twitter_timeout")

        def fake_runner(args: list[str], *, timeout: int = 120) -> tuple[int, str, str]:
            return 1, "", "login required; cookie token=SECRET"

        provider = OpenCliTwitterTextProvider(runner=fake_runner)
        result = provider.extract(
            BackendExtractRequest(
                item=ContentItem(
                    platform="x",
                    input_type="single",
                    url="https://x.com/openmd/status/123",
                    source_type="article",
                ),
                backend_chain=(OPENCLI_TWITTER_THREAD_BACKEND,),
                allow_fallback=False,
            )
        )

        self.assertFalse(result.has_usable_text)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.source_level, "partial_extraction")
        self.assertEqual(result.error_code, "x_auth_required")
        self.assertNotIn("SECRET", result.error_summary or "")
        self.assertNotIn("cookie", (result.error_summary or "").lower())
        self.assertNotIn("token", (result.error_summary or "").lower())

    def test_browser_backend_gate_registers_x_extension_providers(self) -> None:
        safe_registry = create_runtime_backend_provider_registry("x", BackendRuntimeOptions())
        gated_provider = safe_registry.provider_for(OPENCLI_TWITTER_THREAD_BACKEND)

        self.assertIsInstance(gated_provider, SkeletonBackendProvider)
        self.assertNotIsInstance(gated_provider, OpenCliTwitterTextProvider)

        browser_registry = create_runtime_backend_provider_registry(
            "x",
            BackendRuntimeOptions(enable_browser_backends=True),
        )
        thread_provider = browser_registry.provider_for(OPENCLI_TWITTER_THREAD_BACKEND)
        tweets_provider = browser_registry.provider_for(OPENCLI_TWITTER_TWEETS_BACKEND)
        article_provider = browser_registry.provider_for(OPENCLI_TWITTER_ARTICLE_BACKEND)

        self.assertIsInstance(thread_provider, OpenCliTwitterTextProvider)
        self.assertIsInstance(tweets_provider, OpenCliTwitterTextProvider)
        self.assertIsInstance(article_provider, OpenCliTwitterTextProvider)

        network_registry = create_runtime_backend_provider_registry(
            "x",
            BackendRuntimeOptions(enable_network_backends=True),
        )
        self.assertIsInstance(network_registry.provider_for(FXTWITTER_BACKEND), XPublicLayeredProvider)

    def test_requested_article_backend_still_requires_browser_gate(self) -> None:
        safe_registry = create_runtime_backend_provider_registry(
            "x",
            BackendRuntimeOptions(backend_override=OPENCLI_TWITTER_ARTICLE_BACKEND),
        )
        self.assertIsInstance(
            safe_registry.provider_for(OPENCLI_TWITTER_ARTICLE_BACKEND),
            SkeletonBackendProvider,
        )

        browser_registry = create_runtime_backend_provider_registry(
            "x",
            BackendRuntimeOptions(
                backend_override=OPENCLI_TWITTER_ARTICLE_BACKEND,
                enable_browser_backends=True,
            ),
        )
        self.assertIsInstance(
            browser_registry.provider_for(OPENCLI_TWITTER_ARTICLE_BACKEND),
            OpenCliTwitterTextProvider,
        )
        self.assertIsInstance(
            browser_registry.provider_for(OPENCLI_TWITTER_THREAD_BACKEND),
            SkeletonBackendProvider,
        )


if __name__ == "__main__":
    unittest.main()
