"""YouTube-only discovery and transcript backend regression tests."""

from __future__ import annotations

import json
import sys
import unittest

from openmd.backends.base import BackendExtractRequest, DiscoveryRequest
from openmd.backends.registry import BackendRuntimeOptions, create_runtime_backend_provider_registry
from openmd.backends.youtube import (
    OPENCLI_YOUTUBE_TRANSCRIPT_BACKEND,
    OpenCliYouTubeTranscriptProvider,
    classify_youtube_transcript_failure,
)
from openmd.backends.youtube_discovery import OpenCliYouTubeChannelProvider
from openmd.backends.youtube_discovery import _run_opencli
from openmd.core.models import ContentItem


class YouTubeOpenCliDiscoveryTests(unittest.TestCase):
    def test_run_opencli_timeout_returns_structured_failure_without_hanging(self) -> None:
        code, stdout, stderr = _run_opencli(
            [sys.executable, "-c", "import time; time.sleep(5)"],
            timeout=0.1,
        )

        self.assertEqual(code, 124)
        self.assertEqual(stdout, "")
        self.assertIn("timed out", stderr)

    def test_channel_discovery_uses_injected_runner_without_extracting_transcripts(self) -> None:
        calls: list[tuple[list[str], int]] = []

        def fake_runner(args: list[str], *, timeout: int = 120) -> tuple[int, str, str]:
            calls.append((args, timeout))
            return (
                0,
                json.dumps(
                    [
                        {"field": "Name", "value": "Openmd Fixture Channel"},
                        {"field": "ChannelId", "value": "UCopenmdfixture"},
                        {
                            "field": "First fixture video",
                            "value": "https://www.youtube.com/watch?v=vid123 | 1:23 | 10 views | 1 day ago",
                        },
                    ]
                ),
                "",
            )

        provider = OpenCliYouTubeChannelProvider(runner=fake_runner, timeout=5)
        result = provider.discover(
            DiscoveryRequest(
                input_url="https://www.youtube.com/@openmd/videos",
                platform="youtube",
                limit=2,
                allow_all=False,
            )
        )

        self.assertTrue(result.ok, result.error_summary)
        self.assertEqual(
            calls,
            [(["opencli", "youtube", "channel", "@openmd", "--limit", "2", "-f", "json"], 5)],
        )
        self.assertEqual(len(result.items), 1)
        self.assertEqual(result.items[0].platform, "youtube")
        self.assertEqual(result.items[0].id, "vid123")
        self.assertEqual(result.items[0].source_type, "video")
        self.assertEqual(result.items[0].raw_meta["channel_id"], "UCopenmdfixture")
        self.assertEqual(result.items[0].raw_meta["channel_title"], "Openmd Fixture Channel")


class YouTubeOpenCliTranscriptTests(unittest.TestCase):
    def test_failure_classification_covers_known_youtube_blockers(self) -> None:
        cases = {
            "EMPTY_RESULT: transcript command returned nothing": "youtube_transcript_empty_result",
            "SHORT_TRANSCRIPT: only intro text was captured": "youtube_transcript_short_result",
            "Sign in to confirm you're not a bot": "youtube_bot_check",
            "login required; cookie is needed for this video": "youtube_cookie_or_auth_required",
            "ASR required because captions are unavailable": "youtube_asr_required",
            "OpenCLI command timed out after 30 seconds.": "opencli_youtube_transcript_timeout",
        }

        for message, expected in cases.items():
            with self.subTest(message=message):
                self.assertEqual(classify_youtube_transcript_failure(message), expected)

    def test_transcript_provider_uses_direct_video_id_and_classifies_runner_failure(self) -> None:
        calls: list[tuple[list[str], int]] = []

        def fake_runner(args: list[str], *, timeout: int = 120) -> tuple[int, str, str]:
            calls.append((args, timeout))
            return 1, "", "SHORT_TRANSCRIPT: only intro text was captured"

        provider = OpenCliYouTubeTranscriptProvider(
            runner=fake_runner,
            timeout=7,
            attempts=(("grouped", "md"),),
        )
        item = ContentItem(
            platform="youtube",
            input_type="single",
            url="https://www.youtube.com/watch?v=abc123&list=PLignored",
            source_type="video",
        )

        result = provider.extract(
            BackendExtractRequest(
                item=item,
                backend_chain=(OPENCLI_YOUTUBE_TRANSCRIPT_BACKEND,),
                allow_fallback=False,
            )
        )

        self.assertFalse(result.has_usable_text)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.source_level, "partial_extraction")
        self.assertEqual(result.backend, OPENCLI_YOUTUBE_TRANSCRIPT_BACKEND)
        self.assertEqual(result.error_code, "youtube_transcript_short_result")
        self.assertEqual(result.metadata["video_id"], "abc123")
        self.assertEqual(result.metadata["attempt_count"], 1)
        self.assertEqual(
            calls,
            [(["opencli", "youtube", "transcript", "abc123", "--mode", "grouped", "-f", "md"], 7)],
        )
        self.assertNotIn("watch?v=abc123", " ".join(calls[0][0]))
        self.assertIn("No cookie export, media download, or ASR action was attempted.", result.source_notes)

    def test_transcript_provider_retries_short_result_then_accepts_json_transcript(self) -> None:
        calls: list[tuple[list[str], int]] = []
        responses = [
            (0, "too short", ""),
            (
                0,
                json.dumps(
                    {
                        "segments": [
                            {"text": "[00:00:00] hello from fixture"},
                            {"text": "[00:00:05] useful transcript body"},
                        ]
                    }
                ),
                "",
            ),
        ]

        def fake_runner(args: list[str], *, timeout: int = 120) -> tuple[int, str, str]:
            calls.append((args, timeout))
            return responses.pop(0)

        provider = OpenCliYouTubeTranscriptProvider(
            runner=fake_runner,
            timeout=9,
            attempts=(("grouped", "md"), ("grouped", "json")),
        )
        item = ContentItem(
            platform="youtube",
            input_type="single",
            url="https://www.youtube.com/watch?v=abc123&list=PLignored",
            source_type="video",
        )

        result = provider.extract(
            BackendExtractRequest(
                item=item,
                backend_chain=(OPENCLI_YOUTUBE_TRANSCRIPT_BACKEND,),
                allow_fallback=False,
            )
        )

        self.assertTrue(result.has_usable_text)
        self.assertEqual(result.status, "verified")
        self.assertEqual(result.backend, OPENCLI_YOUTUBE_TRANSCRIPT_BACKEND)
        self.assertEqual(result.metadata["video_id"], "abc123")
        self.assertEqual(result.metadata["attempt_count"], 2)
        self.assertEqual(result.metadata["mode"], "grouped")
        self.assertEqual(result.metadata["format"], "json")
        self.assertIn("[00:00:05] useful transcript body", result.text)
        self.assertEqual(
            calls,
            [
                (["opencli", "youtube", "transcript", "abc123", "--mode", "grouped", "-f", "md"], 9),
                (["opencli", "youtube", "transcript", "abc123", "--mode", "grouped", "-f", "json"], 9),
            ],
        )
        self.assertTrue(all("watch?v=abc123" not in " ".join(args) for args, _timeout in calls))
        for raw_key in ("stdout", "stderr", "raw_stdout", "raw_stderr"):
            self.assertNotIn(raw_key, result.metadata)

    def test_browser_backend_gate_registers_opencli_transcript_provider_extension(self) -> None:
        safe_registry = create_runtime_backend_provider_registry("youtube", BackendRuntimeOptions())
        gated_provider = safe_registry.provider_for(OPENCLI_YOUTUBE_TRANSCRIPT_BACKEND)
        self.assertNotIsInstance(gated_provider, OpenCliYouTubeTranscriptProvider)

        browser_registry = create_runtime_backend_provider_registry(
            "youtube",
            BackendRuntimeOptions(enable_browser_backends=True),
        )
        provider = browser_registry.provider_for(OPENCLI_YOUTUBE_TRANSCRIPT_BACKEND)

        self.assertIsInstance(provider, OpenCliYouTubeTranscriptProvider)


if __name__ == "__main__":
    unittest.main()
