"""Xiaoe runtime backend provider regression tests with fake OpenCLI runners."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from openmd.backends.base import BackendExtractRequest, DiscoveryRequest
from openmd.backends.executor import (
    BackendExecutor,
    BackendProviderRegistry,
    SkeletonBackendProvider,
)
from openmd.backends.registry import (
    BackendRuntimeOptions,
    create_runtime_backend_provider_registry,
    create_runtime_discovery_provider_registry,
)
from openmd.backends.xiaoe import (
    OPENCLI_XIAOE_CATALOG_DISCOVERY_BACKEND,
    OPENCLI_XIAOE_CONTENT_BACKEND,
    OPENCLI_XIAOE_TRANSCRIPT_BACKEND,
    OpenCliXiaoeCatalogProvider,
    OpenCliXiaoeContentProvider,
    OpenCliXiaoeTranscriptProvider,
    build_opencli_xiaoe_catalog_args,
    build_opencli_xiaoe_content_args,
    build_opencli_xiaoe_transcript_args,
    parse_xiaoe_content_result,
    parse_xiaoe_catalog_items,
)
from openmd.core.models import ContentItem
from openmd.core.manifest import discovery_manifest_row


SENSITIVE_ERROR_TERMS = ("cookie", "token", "authorization", "header", "stdout", "stderr")


def _xiaoe_item(url: str = "https://demo.xet.citv.cn/p/course/video/example") -> ContentItem:
    return ContentItem(
        platform="xiaoe",
        input_type="single",
        url=url,
        source_type="video",
    )


def _request(item: ContentItem, backend: str) -> BackendExtractRequest:
    return BackendExtractRequest(item=item, backend_chain=(backend,), allow_fallback=False)


class XiaoeRuntimeGateTests(unittest.TestCase):
    def test_browser_backend_gate_registers_opencli_xiaoe_providers(self) -> None:
        safe_registry = create_runtime_backend_provider_registry("xiaoe", BackendRuntimeOptions())

        transcript_provider = safe_registry.provider_for(OPENCLI_XIAOE_TRANSCRIPT_BACKEND)
        content_provider = safe_registry.provider_for(OPENCLI_XIAOE_CONTENT_BACKEND)

        self.assertIsInstance(transcript_provider, SkeletonBackendProvider)
        self.assertIsInstance(content_provider, SkeletonBackendProvider)
        self.assertNotIsInstance(transcript_provider, OpenCliXiaoeTranscriptProvider)
        self.assertNotIsInstance(content_provider, OpenCliXiaoeContentProvider)

        browser_registry = create_runtime_backend_provider_registry(
            "xiaoe",
            BackendRuntimeOptions(enable_browser_backends=True),
        )

        self.assertIsInstance(
            browser_registry.provider_for(OPENCLI_XIAOE_TRANSCRIPT_BACKEND),
            OpenCliXiaoeTranscriptProvider,
        )
        self.assertIsInstance(
            browser_registry.provider_for(OPENCLI_XIAOE_CONTENT_BACKEND),
            OpenCliXiaoeContentProvider,
        )

    def test_browser_backend_gate_registers_xiaoe_catalog_discovery_provider(self) -> None:
        request = DiscoveryRequest(
            input_url="https://demo.xet.citv.cn/p/course/example",
            platform="xiaoe",
            limit=2,
            allow_all=False,
        )
        safe_registry = create_runtime_discovery_provider_registry(
            "xiaoe",
            BackendRuntimeOptions(
                requested_discovery_provider=OPENCLI_XIAOE_CATALOG_DISCOVERY_BACKEND,
            ),
        )
        safe_provider = safe_registry.provider_for(
            request,
            requested_provider=OPENCLI_XIAOE_CATALOG_DISCOVERY_BACKEND,
        )

        browser_registry = create_runtime_discovery_provider_registry(
            "xiaoe",
            BackendRuntimeOptions(
                requested_discovery_provider=OPENCLI_XIAOE_CATALOG_DISCOVERY_BACKEND,
                enable_browser_backends=True,
            ),
        )
        browser_provider = browser_registry.provider_for(
            request,
            requested_provider=OPENCLI_XIAOE_CATALOG_DISCOVERY_BACKEND,
        )

        self.assertEqual(getattr(safe_provider, "name"), "skeleton.discovery")
        self.assertIsInstance(browser_provider, OpenCliXiaoeCatalogProvider)


class XiaoeCatalogDiscoveryProviderTests(unittest.TestCase):
    def test_contract_fixture_preserves_resource_types_and_maps_source_types(self) -> None:
        fixture_path = Path(__file__).parent / "fixtures" / "xiaoe" / "normalized-contract.json"
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))

        items = parse_xiaoe_catalog_items(json.dumps(fixture["catalog"], ensure_ascii=False))

        self.assertEqual(len(items), 7)
        self.assertEqual(items[0].source_type, "image_text")
        self.assertEqual(items[0].raw_meta["resource_type"], 1)
        self.assertEqual(items[0].raw_meta["resource_label"], "图文")
        self.assertEqual(items[1].source_type, "audio")
        self.assertEqual(items[2].source_type, "video")
        self.assertEqual(items[3].raw_meta["resource_kind"], "live")
        self.assertEqual(items[4].source_type, "mixed")
        self.assertTrue(items[4].raw_meta["container"])
        self.assertEqual(items[5].source_type, "mixed")
        self.assertEqual(items[5].raw_meta["resource_type"], 99)
        self.assertEqual(items[5].raw_meta["resource_label"], "99")
        self.assertEqual(items[6].url, "")
        self.assertFalse(items[6].raw_meta["canonical_url_available"])

    def test_parse_catalog_json_builds_media_content_items(self) -> None:
        items = parse_xiaoe_catalog_items(
            """
            [
              {
                "chapter": "chapter 1",
                "no": "1",
                "title": "Lesson 1",
                "type": "video",
                "resource_id": "res1",
                "url": "https://demo.xet.citv.cn/p/course/video/res1",
                "status": "available"
              }
            ]
            """
        )

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].platform, "xiaoe")
        self.assertEqual(items[0].input_type, "collection")
        self.assertEqual(items[0].source_type, "video")
        self.assertEqual(items[0].id, "res1")
        self.assertEqual(items[0].title, "Lesson 1")
        self.assertEqual(items[0].raw_meta["chapter"], "chapter 1")

    def test_catalog_item_without_url_returns_structured_extraction_failure(self) -> None:
        item = parse_xiaoe_catalog_items(
            '[{"title":"Unavailable","resource_type":4,"resource_id":"v_missing"}]'
        )[0]

        result = OpenCliXiaoeTranscriptProvider(runner=lambda *args, **kwargs: (99, "", "")).extract(
            _request(item, OPENCLI_XIAOE_TRANSCRIPT_BACKEND)
        )

        self.assertFalse(result.has_usable_text)
        self.assertEqual(result.error_code, "resource_url_unavailable")

    def test_container_item_is_not_sent_to_lesson_extractor(self) -> None:
        item = parse_xiaoe_catalog_items(
            '[{"title":"Column","resource_type":6,"resource_id":"c_column","url":"https://demo.h5.xet.citv.cn/p/course/ecourse/c_column"}]'
        )[0]

        result = OpenCliXiaoeContentProvider(runner=lambda *args, **kwargs: (99, "", "")).extract(
            _request(item, OPENCLI_XIAOE_CONTENT_BACKEND)
        )

        self.assertFalse(result.has_usable_text)
        self.assertEqual(result.error_code, "container_not_extractable")

    def test_manifest_retains_xiaoe_resource_metadata_without_raw_command_fields(self) -> None:
        item = parse_xiaoe_catalog_items(
            '[{"title":"Video","resource_type":4,"resource_id":"v1","url":"https://demo.h5.xet.citv.cn/v1/course/video/v1?token=secret&lang=zh"}]'
        )[0]

        row = discovery_manifest_row(
            item,
            collection_url="https://demo.h5.xet.citv.cn/p/course/example",
            provider=OPENCLI_XIAOE_CATALOG_DISCOVERY_BACKEND,
        )

        self.assertEqual(row["resource_id"], "v1")
        self.assertEqual(row["resource_type"], 4)
        self.assertEqual(row["resource_label"], "视频")
        self.assertTrue(row["canonical_url_available"])
        self.assertNotIn("token=secret", row["content_url"])
        self.assertNotIn("stdout", row)

    def test_provider_success_runs_opencli_catalog_and_limits_items(self) -> None:
        calls: list[tuple[list[str], int]] = []

        def fake_runner(args: list[str], *, timeout: int = 120) -> tuple[int, str, str]:
            calls.append((args, timeout))
            return (
                0,
                """
                [
                  {"title": "Lesson 1", "type": "video", "resource_id": "res1", "url": "https://demo.xet.citv.cn/p/course/video/res1"},
                  {"title": "Lesson 2", "type": "audio", "resource_id": "res2", "url": "https://demo.xet.citv.cn/p/course/audio/res2"}
                ]
                """,
                "",
            )

        provider = OpenCliXiaoeCatalogProvider(runner=fake_runner, timeout=31)
        result = provider.discover(
            DiscoveryRequest(
                input_url="https://demo.xet.citv.cn/p/course/example",
                platform="xiaoe",
                limit=1,
                allow_all=False,
            )
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.provider, OPENCLI_XIAOE_CATALOG_DISCOVERY_BACKEND)
        self.assertEqual(len(result.items), 1)
        self.assertEqual(result.items[0].id, "res1")
        self.assertEqual(
            calls,
            [(build_opencli_xiaoe_catalog_args("https://demo.xet.citv.cn/p/course/example"), 31)],
        )
        self.assertIn("--keep-tab", calls[0][0])
        self.assertNotIn("batch-transcript", calls[0][0])

    def test_catalog_failure_summary_is_sanitized(self) -> None:
        def fake_runner(args: list[str], *, timeout: int = 120) -> tuple[int, str, str]:
            return 7, "stdout token=secret", "authorization header cookie token"

        provider = OpenCliXiaoeCatalogProvider(runner=fake_runner)
        result = provider.discover(
            DiscoveryRequest(
                input_url="https://demo.xet.citv.cn/p/course/example",
                platform="xiaoe",
                limit=2,
                allow_all=False,
            )
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "backend_exit_7")
        text = (result.error_summary or "").lower()
        for term in SENSITIVE_ERROR_TERMS:
            self.assertNotIn(term, text)


class XiaoeTranscriptProviderTests(unittest.TestCase):
    def test_transcript_command_reads_temp_markdown_and_cleans_output_dir(self) -> None:
        calls: list[tuple[list[str], int]] = []
        output_dirs: list[Path] = []

        def fake_runner(args: list[str], *, timeout: int = 120) -> tuple[int, str, str]:
            calls.append((args, timeout))
            output_dir = Path(args[args.index("--out") + 1])
            output_dirs.append(output_dir)
            output_dir.mkdir(parents=True, exist_ok=True)
            (output_dir / "lesson.md").write_text(
                "# Xiaoe lesson\n\n[00:00:01] transcript text\n",
                encoding="utf-8",
            )
            return 0, '{"outputPath":"redacted"}', ""

        provider = OpenCliXiaoeTranscriptProvider(runner=fake_runner, timeout=17)

        result = provider.extract(_request(_xiaoe_item(), OPENCLI_XIAOE_TRANSCRIPT_BACKEND))

        self.assertTrue(result.has_usable_text)
        self.assertEqual(result.status, "verified")
        self.assertEqual(result.source_level, "third_party_extractor")
        self.assertEqual(result.backend, OPENCLI_XIAOE_TRANSCRIPT_BACKEND)
        self.assertEqual(result.title, "Xiaoe lesson")
        self.assertIn("[00:00:01] transcript text", result.text)
        self.assertEqual(len(output_dirs), 1)
        self.assertFalse(output_dirs[0].exists())
        self.assertEqual(calls[0][1], 17)
        self.assertEqual(calls[0][0][:4], ["opencli", "xiaoe", "transcript", _xiaoe_item().url])
        self.assertIn("--keep-artifacts", calls[0][0])
        self.assertEqual(calls[0][0][calls[0][0].index("--keep-artifacts") + 1], "false")
        self.assertNotIn("batch-transcript", calls[0][0])
        self.assertNotIn("download", calls[0][0])
        self.assertEqual(
            calls[0][0],
            build_opencli_xiaoe_transcript_args(_xiaoe_item().url, output_dir=str(output_dirs[0])),
        )
        self.assertIn(
            "No batch-transcript, full-course crawl, media download, ASR, or permission bypass was attempted.",
            result.source_notes,
        )

    def test_transcript_failure_summary_is_sanitized_without_raw_stream_metadata(self) -> None:
        def fake_runner(args: list[str], *, timeout: int = 120) -> tuple[int, str, str]:
            return 23, "stdout token=secret", "authorization header cookie token xsec_token"

        result = OpenCliXiaoeTranscriptProvider(runner=fake_runner).extract(
            _request(_xiaoe_item(), OPENCLI_XIAOE_TRANSCRIPT_BACKEND)
        )

        self.assertFalse(result.has_usable_text)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.error_code, "backend_exit_23")
        self.assertEqual(result.metadata, {"platform": "xiaoe", "exit_code": 23})
        self._assert_no_sensitive_error_terms(result.error_summary)

    def test_typed_opencli_error_is_mapped_to_openmd_error_code(self) -> None:
        def fake_runner(args: list[str], *, timeout: int = 120) -> tuple[int, str, str]:
            return (
                1,
                '{"error":{"name":"SessionExpired","message":"browser session expired"}}',
                "",
            )

        result = OpenCliXiaoeTranscriptProvider(runner=fake_runner).extract(
            _request(_xiaoe_item(), OPENCLI_XIAOE_TRANSCRIPT_BACKEND)
        )

        self.assertEqual(result.error_code, "session_expired")
        self.assertIn("browser session expired", result.error_summary or "")

    def test_descriptor_separates_credentials_from_user_accessible_session(self) -> None:
        capability = OpenCliXiaoeTranscriptProvider(runner=lambda *args, **kwargs: (0, "", "")).descriptor().capabilities[0]

        self.assertFalse(capability.requires_credentials)
        self.assertIn("user_accessible_session", capability.optional_config)
        self.assertIn("does not own or store credentials", OpenCliXiaoeTranscriptProvider().descriptor().description)

    def _assert_no_sensitive_error_terms(self, value: str | None) -> None:
        text = (value or "").lower()
        for term in SENSITIVE_ERROR_TERMS:
            self.assertNotIn(term, text)


class XiaoeContentFallbackTests(unittest.TestCase):
    def test_content_fallback_command_shape_and_success_result(self) -> None:
        calls: list[tuple[list[str], int]] = []

        def fake_runner(args: list[str], *, timeout: int = 120) -> tuple[int, str, str]:
            calls.append((args, timeout))
            return (
                0,
                json.dumps(
                    {
                        "data": [
                            {
                                "title": "Xiaoe text page",
                                "content": "fallback content",
                                "content_length": 16,
                                "image_count": 2,
                                "content_selector": ".rich-text-wrap",
                                "ignored": "must not be retained",
                            }
                        ],
                        "ignored": "must not be retained",
                    }
                ),
                "",
            )

        provider = OpenCliXiaoeContentProvider(runner=fake_runner, timeout=29)

        result = provider.extract(_request(_xiaoe_item(), OPENCLI_XIAOE_CONTENT_BACKEND))

        self.assertTrue(result.has_usable_text)
        self.assertEqual(result.status, "verified")
        self.assertEqual(result.source_level, "native_text")
        self.assertEqual(result.backend, OPENCLI_XIAOE_CONTENT_BACKEND)
        self.assertIn("fallback content", result.text)
        self.assertEqual(result.title, "Xiaoe text page")
        self.assertEqual(result.metadata["content_length"], 16)
        self.assertEqual(result.metadata["image_count"], 2)
        self.assertNotIn("must not be retained", result.text)
        self.assertEqual(calls, [(build_opencli_xiaoe_content_args(_xiaoe_item().url), 29)])
        self.assertIn("--keep-tab", calls[0][0])
        self.assertNotIn("batch-transcript", calls[0][0])

    def test_backend_executor_uses_registered_content_fallback_after_transcript_failure(self) -> None:
        calls: list[list[str]] = []

        def fake_runner(args: list[str], *, timeout: int = 120) -> tuple[int, str, str]:
            calls.append(args)
            if args[:3] == ["opencli", "xiaoe", "transcript"]:
                return 9, "", "transcript unavailable"
            if args[:3] == ["opencli", "xiaoe", "content"]:
                return 0, '{"title":"Fallback","content":"fallback text","content_length":13,"image_count":0}', ""
            return 99, "", "unexpected command"

        registry = BackendProviderRegistry(allow_skeleton_fallback=False)
        registry.register(
            OPENCLI_XIAOE_TRANSCRIPT_BACKEND,
            OpenCliXiaoeTranscriptProvider(runner=fake_runner),
        )
        registry.register(
            OPENCLI_XIAOE_CONTENT_BACKEND,
            OpenCliXiaoeContentProvider(runner=fake_runner),
        )
        executor = BackendExecutor(provider_registry=registry)

        result = executor.extract(
            _xiaoe_item(),
            (OPENCLI_XIAOE_TRANSCRIPT_BACKEND, OPENCLI_XIAOE_CONTENT_BACKEND),
        )

        self.assertTrue(result.has_usable_text)
        self.assertEqual(result.backend, OPENCLI_XIAOE_CONTENT_BACKEND)
        self.assertEqual([call[:3] for call in calls], [["opencli", "xiaoe", "transcript"], ["opencli", "xiaoe", "content"]])
        self.assertIn(
            "fallback_attempt: opencli.xiaoe.transcript failed with backend_exit_9",
            result.source_notes,
        )

    def test_content_parser_discards_json_envelope(self) -> None:
        payload = parse_xiaoe_content_result(
            '{"data":[{"title":"Title","content":"Text","content_length":4,"image_count":1,"extra":"ignored"}],"extra":"ignored"}'
        )

        self.assertIsNotNone(payload)
        self.assertEqual(payload.title, "Title")
        self.assertEqual(payload.content, "Text")
        self.assertEqual(payload.content_length, 4)
        self.assertEqual(payload.image_count, 1)

    def test_invalid_content_response_is_not_written_as_markdown_body(self) -> None:
        provider = OpenCliXiaoeContentProvider(
            runner=lambda *args, **kwargs: (0, "not json content", "")
        )

        result = provider.extract(_request(_xiaoe_item(), OPENCLI_XIAOE_CONTENT_BACKEND))

        self.assertFalse(result.has_usable_text)
        self.assertEqual(result.error_code, "invalid_content_response")
        self.assertEqual(result.text, "")


if __name__ == "__main__":
    unittest.main()
