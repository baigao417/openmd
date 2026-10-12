"""Douyin bounded runtime discovery provider regression tests."""

from __future__ import annotations

import json
import unittest
import argparse
import tempfile
from pathlib import Path

from openmd.backends.base import DiscoveryRequest
from openmd.backends.douyin import (
    DOUYIN_METADATA_BACKEND,
    DOUYIN_OPENMD_CREATOR_VIDEOS_BACKEND,
    DOUYIN_OPENCLI_USER_VIDEOS_BACKEND,
    DouyinMetadataProvider,
    OpenCliOpenmdDouyinCreatorVideosProvider,
    OpenCliDouyinUserVideosProvider,
    build_opencli_openmd_douyin_creator_videos_args,
    build_opencli_douyin_user_videos_args,
    classify_douyin_input_failure,
    douyin_user_target,
    normalize_douyin_collection_url,
)
from openmd.backends.douyin_providers import DOUYIN_TRANSCRIPT_BACKEND
from openmd.cli import (
    CliResult,
    _allow_scoped_all_collect,
    _select_collect_discovery_backend,
    _write_douyin_coverage_summary,
)
from openmd.core.manifest import discovery_manifest_row
from openmd.backends.base import BackendExtractRequest
from openmd.backends.registry import BackendRuntimeOptions, create_runtime_discovery_provider_registry
from openmd.backends.registry import create_runtime_backend_provider_registry
from openmd.core.models import ContentItem


SEC_UID = "MS4wLjABAAAAfixture_123-abc"


def _douyin_request(
    url: str = f"https://www.douyin.com/user/{SEC_UID}?modal_id=999&token=secret",
    *,
    limit: int | None = 20,
    allow_all: bool = False,
    platform: str = "douyin",
) -> DiscoveryRequest:
    return DiscoveryRequest(
        input_url=url,
        platform=platform,
        limit=limit,
        allow_all=allow_all,
    )


class DouyinInputNormalizationTests(unittest.TestCase):
    def test_user_url_normalization_strips_query_and_extracts_sec_uid(self) -> None:
        url = f"https://www.douyin.com/user/{SEC_UID}?modal_id=999&token=secret"

        self.assertEqual(douyin_user_target(url), SEC_UID)
        self.assertEqual(normalize_douyin_collection_url(url), f"https://www.douyin.com/user/{SEC_UID}")

    def test_short_link_and_unsupported_inputs_are_classified_before_runner(self) -> None:
        short_url = "复制打开抖音 https://v.douyin.com/iFixture/?token=secret"

        self.assertEqual(normalize_douyin_collection_url(short_url), "https://v.douyin.com/iFixture/")
        self.assertEqual(classify_douyin_input_failure(short_url), "douyin_short_link_resolution_required")
        self.assertEqual(
            classify_douyin_input_failure("https://www.douyin.com/video/7123456789012345678"),
            "douyin_single_video_not_supported_for_discovery",
        )
        self.assertEqual(
            classify_douyin_input_failure("https://www.tiktok.com/@openmd/video/7123456789012345678"),
            "tiktok_not_supported",
        )

    def test_short_link_discovery_fails_without_running_opencli(self) -> None:
        calls: list[list[str]] = []

        def fake_runner(args: list[str], *, timeout: int = 120) -> tuple[int, str, str]:
            calls.append(args)
            return 0, "[]", ""

        provider = OpenCliDouyinUserVideosProvider(runner=fake_runner)
        result = provider.discover(_douyin_request("https://v.douyin.com/iFixture/?token=secret"))

        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "douyin_short_link_resolution_required")
        self.assertEqual(result.metadata["normalized_url"], "https://v.douyin.com/iFixture/")
        self.assertEqual(calls, [])


class DouyinOpenCliDiscoveryTests(unittest.TestCase):
    def test_bounded_user_videos_command_shape_and_safe_json_parsing(self) -> None:
        calls: list[tuple[list[str], int]] = []

        def fake_runner(args: list[str], *, timeout: int = 120) -> tuple[int, str, str]:
            calls.append((args, timeout))
            return (
                0,
                json.dumps(
                    {
                        "data": [
                            {
                                "aweme_id": "7123456789012345678",
                                "title": "Fixture Douyin video",
                                "duration": "12s",
                                "digg_count": "88",
                                "play_url": "https://example.com/raw-media.mp4?token=secret",
                                "top_comments": [{"text": "raw comment should not persist"}],
                            }
                        ]
                    }
                ),
                "",
            )

        provider = OpenCliDouyinUserVideosProvider(runner=fake_runner, timeout=7)
        result = provider.discover(_douyin_request(limit=99))

        self.assertTrue(result.ok, result.error_summary)
        self.assertEqual(
            calls,
            [
                (
                    [
                        "opencli",
                        "douyin",
                        "user-videos",
                        SEC_UID,
                        "--limit",
                        "20",
                        "--with_comments",
                        "false",
                        "-f",
                        "json",
                    ],
                    7,
                )
            ],
        )
        self.assertEqual(build_opencli_douyin_user_videos_args(SEC_UID, limit=99), calls[0][0])
        self.assertEqual(result.metadata["limit"], 20)
        self.assertEqual(result.metadata["requires_browser_context"], True)
        self.assertEqual(len(result.items), 1)
        item = result.items[0]
        self.assertEqual(item.platform, "douyin")
        self.assertEqual(item.input_type, "collection")
        self.assertEqual(item.id, "7123456789012345678")
        self.assertEqual(item.url, "https://www.douyin.com/video/7123456789012345678")
        self.assertEqual(item.title, "Fixture Douyin video")
        self.assertEqual(item.raw_meta["like_count"], "88")
        self.assertEqual(item.raw_meta["collection_url"], f"https://www.douyin.com/user/{SEC_UID}")
        self.assertNotIn("play_url", item.raw_meta)
        self.assertNotIn("top_comments", item.raw_meta)
        self.assertIn("No media download, ASR, anti-bot bypass, or account mutation was attempted.", result.source_notes)

    def test_provider_gate_requires_enable_browser_backends(self) -> None:
        request = _douyin_request()
        safe_registry = create_runtime_discovery_provider_registry(
            "douyin",
            BackendRuntimeOptions(requested_discovery_provider=DOUYIN_OPENCLI_USER_VIDEOS_BACKEND),
        )
        safe_provider = safe_registry.provider_for(
            request,
            requested_provider=DOUYIN_OPENCLI_USER_VIDEOS_BACKEND,
        )

        self.assertIsNotNone(safe_provider)
        self.assertEqual(getattr(safe_provider, "name"), "skeleton.discovery")
        self.assertNotIsInstance(safe_provider, OpenCliDouyinUserVideosProvider)

        browser_registry = create_runtime_discovery_provider_registry(
            "douyin",
            BackendRuntimeOptions(
                requested_discovery_provider=DOUYIN_OPENCLI_USER_VIDEOS_BACKEND,
                enable_browser_backends=True,
            ),
        )
        browser_provider = browser_registry.provider_for(
            request,
            requested_provider=DOUYIN_OPENCLI_USER_VIDEOS_BACKEND,
        )

        self.assertIsInstance(browser_provider, OpenCliDouyinUserVideosProvider)

    def test_opencli_failure_summary_is_sanitized_and_does_not_persist_raw_streams(self) -> None:
        def fake_runner(args: list[str], *, timeout: int = 120) -> tuple[int, str, str]:
            return (
                23,
                "stdout cookie token access_token=secret raw_stdout play_url top_comments",
                "authorization header raw_response xsec_token Cookie token",
            )

        provider = OpenCliDouyinUserVideosProvider(runner=fake_runner)
        result = provider.discover(_douyin_request())

        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "douyin_auth_or_browser_session_required")
        self.assertEqual(result.metadata["exit_code"], 23)
        summary = (result.error_summary or "").lower()
        for term in (
            "cookie",
            "token",
            "authorization",
            "header",
            "raw_response",
            "raw_stdout",
            "play_url",
            "top_comments",
            "xsec_token",
        ):
            self.assertNotIn(term, summary)
        self.assertNotIn("stdout", result.metadata)
        self.assertNotIn("stderr", result.metadata)

    def test_full_creator_provider_preserves_transient_fields_only_in_memory(self) -> None:
        rows = [
            {
                "aweme_id": str(7123456789012345600 + index),
                "title": f"Video {index}",
                "author": "Creator",
                "duration": 12,
                "digg_count": index,
                "aweme_type": 0,
                "image_count": 0,
                "media_kind": "video",
                "play_url": f"https://media.example/{index}?token=secret",
                "transcript_candidates": [{"source": "video_text", "text": f"line {index}"}],
                "page_count": 4,
                "empty_page_count": 1,
                "duplicate_page_count": 0,
                "pagination_retry_count": 2,
                "source_exhausted": True,
            }
            for index in range(45)
        ]
        rows.append(
            {
                "aweme_id": "7999999999999999999",
                "title": "Image post",
                "author": "Creator",
                "duration": 0,
                "aweme_type": 68,
                "image_count": 1,
                "media_kind": "image_text",
                "play_url": "https://media.example/audio?token=secret",
                "page_count": 4,
                "empty_page_count": 1,
                "duplicate_page_count": 0,
                "pagination_retry_count": 2,
                "source_exhausted": True,
            }
        )
        calls: list[tuple[list[str], int]] = []

        def fake_runner(args: list[str], *, timeout: int = 120) -> tuple[int, str, str]:
            calls.append((args, timeout))
            return 0, json.dumps(rows), ""

        provider = OpenCliOpenmdDouyinCreatorVideosProvider(runner=fake_runner, timeout=99)
        request = _douyin_request("https://v.douyin.com/iFixture/", limit=None, allow_all=True)
        result = provider.discover(request)

        self.assertTrue(result.ok, result.error_summary)
        self.assertEqual(len(result.items), 46)
        self.assertEqual(len({item.id for item in result.items}), 46)
        self.assertTrue(result.metadata["source_exhausted"])
        self.assertEqual(result.metadata["video_count"], 45)
        self.assertEqual(result.metadata["image_post_count"], 1)
        self.assertEqual(result.metadata["page_count"], 4)
        self.assertEqual(
            calls,
            [(build_opencli_openmd_douyin_creator_videos_args(request.input_url), 99)],
        )
        item = result.items[0]
        self.assertIn("play_url", item.raw_meta)
        self.assertIn("transcript_candidates", item.raw_meta)
        manifest = discovery_manifest_row(
            item,
            collection_url=request.input_url,
            provider=result.provider,
            limit=None,
        )
        self.assertNotIn("play_url", manifest)
        self.assertNotIn("transcript_candidates", manifest)
        self.assertNotIn("token=secret", json.dumps(manifest))
        image_item = result.items[-1]
        self.assertEqual(image_item.source_type, "image_text")
        image_manifest = discovery_manifest_row(
            image_item,
            collection_url=request.input_url,
            provider=result.provider,
            limit=None,
        )
        self.assertEqual(image_manifest["media_kind"], "image_text")
        self.assertEqual(image_manifest["image_count"], 1)
        self.assertNotIn("play_url", image_manifest)

    def test_full_creator_provider_and_all_cli_are_browser_gated(self) -> None:
        request = _douyin_request(limit=None, allow_all=True)
        safe_registry = create_runtime_discovery_provider_registry(
            "douyin",
            BackendRuntimeOptions(requested_discovery_provider=DOUYIN_OPENMD_CREATOR_VIDEOS_BACKEND),
        )
        browser_registry = create_runtime_discovery_provider_registry(
            "douyin",
            BackendRuntimeOptions(
                requested_discovery_provider=DOUYIN_OPENMD_CREATOR_VIDEOS_BACKEND,
                enable_browser_backends=True,
            ),
        )
        self.assertNotIsInstance(
            safe_registry.provider_for(request, requested_provider=DOUYIN_OPENMD_CREATOR_VIDEOS_BACKEND),
            OpenCliOpenmdDouyinCreatorVideosProvider,
        )
        self.assertIsInstance(
            browser_registry.provider_for(request, requested_provider=DOUYIN_OPENMD_CREATOR_VIDEOS_BACKEND),
            OpenCliOpenmdDouyinCreatorVideosProvider,
        )

        args = argparse.Namespace(
            all=True,
            enable_browser_backends=True,
            discovery_backend=None,
        )
        self.assertTrue(_allow_scoped_all_collect(request.input_url, args))
        _select_collect_discovery_backend(request.input_url, args)
        self.assertEqual(args.discovery_backend, DOUYIN_OPENMD_CREATOR_VIDEOS_BACKEND)
        self.assertEqual(args.backend, DOUYIN_TRANSCRIPT_BACKEND)

    def test_full_creator_coverage_summary_preserves_resume_and_pagination_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            out = Path(tmp_dir)
            manifest = out / "manifests" / "douyin.jsonl"
            manifest.parent.mkdir(parents=True)
            manifest.write_text("{}\n", encoding="utf-8")
            args = argparse.Namespace(
                out=str(out),
                url="https://v.douyin.com/iFixture/",
                discovery_backend=DOUYIN_OPENMD_CREATOR_VIDEOS_BACKEND,
                backend=DOUYIN_TRANSCRIPT_BACKEND,
                runlog=None,
            )
            result = CliResult(
                ok=False,
                command="collect",
                item_count=45,
                success_count=44,
                failed_count=1,
                skipped_count=40,
                source_level_counts={"media_asr": 43, "ocr": 1},
                failure_code_counts={"douyin_transcript_unavailable": 1},
            )

            coverage_path = _write_douyin_coverage_summary(
                args=args,
                result=result,
                manifest_path=manifest,
                discovered_count=45,
                source_exhausted=True,
                discovery_metadata={
                    "page_count": 4,
                    "empty_page_count": 1,
                    "duplicate_page_count": 0,
                    "pagination_retry_count": 2,
                    "video_count": 43,
                    "image_post_count": 2,
                },
                start_index=0,
                max_items=0,
            )

            payload = json.loads(coverage_path.read_text(encoding="utf-8"))
            self.assertTrue(payload["source_exhausted"])
            self.assertTrue(payload["complete_run"])
            self.assertEqual(payload["discovered"], 45)
            self.assertEqual(payload["success"], 44)
            self.assertEqual(payload["failed"], 1)
            self.assertEqual(payload["skipped_existing"], 40)
            self.assertEqual(payload["remaining_unattempted"], 0)
            self.assertEqual(payload["page_count"], 4)
            self.assertEqual(payload["empty_page_count"], 1)
            self.assertEqual(payload["pagination_retry_count"], 2)
            self.assertEqual(payload["video_count"], 43)
            self.assertEqual(payload["image_post_count"], 2)
            self.assertEqual(payload["source_level_counts"], {"asr": 43, "ocr": 1})
            self.assertEqual(
                payload["failure_code_counts"],
                {"douyin_transcript_unavailable": 1},
            )
            self.assertNotIn("play_url", coverage_path.read_text(encoding="utf-8"))


class DouyinMetadataProviderTests(unittest.TestCase):
    def test_metadata_backend_is_registered_without_browser_gate(self) -> None:
        registry = create_runtime_backend_provider_registry("douyin", BackendRuntimeOptions())

        self.assertIsInstance(registry.provider_for(DOUYIN_METADATA_BACKEND), DouyinMetadataProvider)

    def test_metadata_provider_packages_discovered_item_without_raw_media_fields(self) -> None:
        item = ContentItem(
            platform="douyin",
            input_type="collection",
            url="https://www.douyin.com/video/7123456789012345678",
            id="7123456789012345678",
            title="Fixture Douyin video",
            author="Fixture author",
            published_at="2026-06-24",
            source_type="video",
            raw_meta={
                "duration": "12",
                "like_count": "88",
                "collection_url": f"https://www.douyin.com/user/{SEC_UID}",
                "discovery_provider": DOUYIN_OPENCLI_USER_VIDEOS_BACKEND,
                "play_url": "https://example.com/raw.mp4?token=secret",
                "top_comments": "raw comments should not persist",
            },
        )

        result = DouyinMetadataProvider().extract(
            BackendExtractRequest(item=item, backend_chain=(DOUYIN_METADATA_BACKEND,), allow_fallback=False)
        )

        self.assertTrue(result.has_usable_text)
        self.assertEqual(result.backend, DOUYIN_METADATA_BACKEND)
        self.assertEqual(result.source_level, "third_party_extractor")
        self.assertIn("Fixture Douyin video", result.text)
        self.assertIn("metadata only", result.text)
        self.assertNotIn("play_url", result.text)
        self.assertNotIn("top_comments", result.text)
        self.assertNotIn("token=secret", result.text)


if __name__ == "__main__":
    unittest.main()
