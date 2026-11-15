"""Offline metadata enrichment checks, with no live transport."""

import unittest
from unittest.mock import patch

from openmd.backends.video_metadata import enrich_video_result
from openmd.core.models import ContentItem, TranscriptResult
from openmd.core.renderer import render_document, render_markdown_document


class VideoMetadataTests(unittest.TestCase):
    def setUp(self):
        self.item = ContentItem(
            platform="youtube", input_type="single", url="https://www.youtube.com/watch?v=AIBfnG1it0s",
            id="AIBfnG1it0s", source_type="video",
        )
        self.result = TranscriptResult(
            text="[00:00:00] Original words\n[00:01:00] unchanged",
            title="untitled", status="verified", source_level="native_caption", backend="fixture",
            metadata={"existing": "evidence"},
        )

    def test_youtube_oembed_does_not_invent_a_publication_date(self):
        with patch("openmd.backends.video_metadata._fetch_public_json", side_effect=AssertionError("live network")):
            calls = []

            def fetch(url, timeout):
                calls.append((url, timeout))
                return {"title": "Source video title", "author_name": "Source author"}

            enriched = enrich_video_result(self.item, self.result, network_enabled=True, fetch_json=fetch)
        self.assertEqual(len(calls), 1)
        self.assertEqual(enriched.title, "Source video title")
        self.assertEqual(enriched.author, "Source author")
        self.assertIsNone(enriched.published_at)
        self.assertEqual(enriched.text, self.result.text)
        self.assertEqual(enriched.metadata["existing"], "evidence")
        self.assertEqual(enriched.metadata["video_metadata"]["status"], "available")
        document = render_markdown_document(self.item, enriched)
        for raw in (False, True):
            self.assertNotIn("published:", render_document(document, raw=raw))

    def test_existing_publication_date_survives_enrichment(self):
        item = ContentItem(
            platform="youtube", input_type="single", url=self.item.url,
            published_at="2026-11-05T12:00:00Z", source_type="video",
        )
        enriched = enrich_video_result(item, self.result, network_enabled=True,
                                      fetch_json=lambda *_: {"title": "Video", "author_name": "Author"})
        self.assertEqual(enriched.published_at, item.published_at)
        self.assertIn('published: "2026-11-05"', render_document(render_markdown_document(item, enriched)))

    def test_network_gate_mock_and_failed_results_make_no_requests(self):
        with patch("openmd.backends.video_metadata._fetch_public_json", side_effect=AssertionError("live network")) as live:
            def unexpected(*_):
                self.fail("transport should not be called")

            enriched = enrich_video_result(self.item, self.result, fetch_json=unexpected)
            self.assertEqual(enriched.metadata["video_metadata"]["status"], "not_requested")
            self.assertEqual(enriched.text, self.result.text)
            for backend, status in (("mock", "verified"), ("fixture", "failed")):
                result = TranscriptResult(text=self.result.text, status=status,
                                          source_level="native_caption", backend=backend)
                self.assertIs(enrich_video_result(self.item, result, network_enabled=True,
                                                 fetch_json=unexpected), result)
            live.assert_not_called()

    def test_metadata_failure_preserves_text_and_omits_missing_date(self):
        def unavailable(*_):
            raise TimeoutError("injected metadata failure")

        enriched = enrich_video_result(self.item, self.result, network_enabled=True, fetch_json=unavailable)
        self.assertEqual(enriched.text, self.result.text)
        self.assertEqual(enriched.status, self.result.status)
        self.assertEqual(enriched.source_level, self.result.source_level)
        self.assertEqual(enriched.metadata["video_metadata"]["status"], "unavailable")
        rendered = render_document(render_markdown_document(self.item, enriched))
        self.assertNotIn("published:", rendered)
        self.assertIn("视频标题未取得", rendered)
