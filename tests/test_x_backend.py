"""X/Twitter runtime backend regression tests."""

from __future__ import annotations

import unittest

from openmd.backends.base import BackendExtractRequest
from openmd.backends.executor import SkeletonBackendProvider
from openmd.backends.registry import BackendRuntimeOptions, create_runtime_backend_provider_registry
from openmd.backends.x import (
    OPENCLI_TWITTER_ARTICLE_BACKEND,
    OPENCLI_TWITTER_THREAD_BACKEND,
    OPENCLI_TWITTER_TWEETS_BACKEND,
    OpenCliTwitterTextProvider,
    classify_twitter_failure,
)
from openmd.core.models import ContentItem


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
    assert result.title == "@writer：第一行|原文 下一行"
    assert result.author == "@writer"
    assert result.text == text
    document = render_markdown_document(item, result)
    assert document.frontmatter["title"] == result.title
    written = write_item_result(item, result, tmp_path)
    assert written.stem == filename_base_for_item(result.title, item_id=item.id, url=item.url)
    saved_title = next(line.removeprefix("title: ") for line in written.read_text(encoding="utf-8").splitlines() if line.startswith("title: "))
    assert json.loads(saved_title) == result.title


def test_thread_without_title_has_stable_post_fallback():
    item = ContentItem(platform="x", input_type="single", url="https://x.com/i/status/123", source_type="article")
    result = OpenCliTwitterTextProvider(runner=lambda *a, **k: (0, "Plain text", "")).extract(
        BackendExtractRequest(item=item, backend_chain=(OPENCLI_TWITTER_THREAD_BACKEND,))
    )
    assert result.title == "X-123"


class XOpenCliTextProviderTests(unittest.TestCase):
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
                        "persistent",
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
                        "persistent",
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
                        "persistent",
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
