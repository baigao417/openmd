from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import openmd.cli as cli_module
import openmd.backends.registry as backend_registry_module
from openmd.adapters import opencli as opencli_adapters
from openmd.adapters.base import StubPlatformAdapter
from openmd.adapters.opencli import OpenCliPlatformAdapter
from openmd.adapters.registry import BackendSelection, create_default_registry
from openmd.backends.base import BackendExtractRequest, DiscoveryRequest, DiscoveryResult
from openmd.backends.bilibili import (
    BILIBILI_ASR_FALLBACK_BACKEND,
    BILIBILI_HARVEST_BACKEND,
    BILIBILI_NATIVE_DISCOVERY_BACKEND,
    BILIBILI_OPENCLI_DISCOVERY_BACKEND,
    BILIBILI_OPENCLI_SUBTITLE_BACKEND,
    BILIBILI_YTDLP_MEDIA_BACKEND,
    BilibiliMediaAsrFallbackProvider,
    BilibiliNativeSubtitleProvider,
    BilibiliNativeUserVideosProvider,
    OpenCliBilibiliSubtitleProvider,
    OpenCliBilibiliUserVideosProvider,
)
from openmd.backends.bilibili_wbi import (
    BILIBILI_COOKIE_HEADER_ENV,
    BILIBILI_NAV_URL,
    BilibiliWbiSigner,
    bilibili_cookie_header,
    bilibili_cookie_header_for_url,
    bilibili_headers,
    sign_wbi_params_with_keys,
)
from openmd.backends.executor import (
    BackendExecutor,
    DiscoveryExecutor,
    MockDiscoveryProvider,
    create_safe_backend_provider_registry,
    create_safe_discovery_provider_registry,
)
from openmd.backends.opencli import OpenCliBackend
from openmd.backends.registry import (
    BackendRuntimeOptions,
    create_runtime_backend_provider_registry,
    create_runtime_discovery_provider_registry,
    normalize_discovery_backend,
)
from openmd.backends.web_article import (
    OPENCLI_WEB_READ_BACKEND,
    WEB_ARTICLE_TRAFILATURA_BACKEND,
    OpenCliWebReadProvider,
    WebArticleTrafilaturaProvider,
)
from openmd.backends.weixin import (
    OPENCLI_WEIXIN_DOWNLOAD_BACKEND,
    WECHAT_ARTICLE_EXPORTER_DOWNLOAD_BACKEND,
    WEIXIN_LOCAL_BACKEND,
    OpenCliWeixinDownloadProvider,
    WeixinArticleLocalProvider,
    normalize_weixin_url,
)
from openmd.backends.youtube_discovery import (
    OpenCliYouTubeChannelProvider,
    OpenCliYouTubePlaylistProvider,
    YouTubeDataApiChannelProvider,
    YouTubeDataApiPlaylistProvider,
    YOUTUBE_DATA_API_DISCOVERY_BACKEND,
    YOUTUBE_DATA_API_PLAYLIST_DISCOVERY_BACKEND,
    YOUTUBE_DATA_API_KEY_ENV,
    YOUTUBE_DISCOVERY_BACKEND,
    YOUTUBE_PLAYLIST_DISCOVERY_BACKEND,
    build_opencli_youtube_channel_args,
    build_opencli_youtube_playlist_args,
    classify_youtube_playlist_discovery_failure,
    is_youtube_collection_candidate,
    normalize_youtube_collection_url,
    opencli_youtube_playlist_target,
    parse_opencli_youtube_channel_json,
    parse_opencli_youtube_playlist_output,
    youtube_data_api_channel_lookup,
    youtube_data_api_playlist_id,
)
from openmd.backends.youtube_transcript import (
    YouTubeTranscriptApiProvider,
    extract_youtube_video_id,
)
from openmd.cli import main as cli_main
from openmd.core.detector import PLATFORM_IDS, detect_platform
from openmd.core.manifest import (
    default_manifest_path,
    discovery_manifest_row,
    write_discovery_manifest,
)
from openmd.core.models import ContentItem, TranscriptResult
from openmd.core.output import localize_markdown_images, write_item_result
from openmd.core.renderer import render_markdown_document
from openmd.core.runlog import append_runlog_entry, build_item_runlog_entry, build_runlog_entry
from openmd.core.secrets import sanitize_url_text


def _sample_opencli_channel_json() -> str:
    return json.dumps(
        [
            {"field": "channelId", "value": "UC_synthetic_fixture_channel"},
            {"field": "name", "value": "ExampleChannel"},
            {"field": "---", "value": "--- Recent Videos ---"},
            {
                "field": "Synthetic channel video one",
                "value": "2:39 | 5.3K views | 11 hours ago | https://www.youtube.com/watch?v=demoVideo01",
            },
            {
                "field": "Synthetic channel video two",
                "value": "4:20 | 12K views | 1 day ago | https://www.youtube.com/watch?v=o3Example",
            },
        ]
    )


def _sample_youtube_data_api_fetcher(
    calls: list[tuple[str, dict[str, str], int]],
) -> object:
    def fake_fetcher(
        endpoint: str,
        params: dict[str, str],
        *,
        timeout: int = 30,
    ) -> tuple[int, str]:
        calls.append((endpoint, params, timeout))
        if endpoint == "channels":
            return 200, json.dumps(
                {
                    "items": [
                        {
                            "id": "UC123",
                            "snippet": {"title": "Example Channel"},
                            "contentDetails": {"relatedPlaylists": {"uploads": "UU123"}},
                        }
                    ]
                }
            )
        if endpoint == "playlistItems":
            return 200, json.dumps(
                {
                    "items": [
                        {
                            "snippet": {
                                "title": "First playlist title",
                                "publishedAt": "2026-01-01T00:00:00Z",
                            },
                            "contentDetails": {"videoId": "vid1"},
                        },
                        {
                            "snippet": {
                                "title": "Second playlist title",
                                "publishedAt": "2026-01-02T00:00:00Z",
                            },
                            "contentDetails": {"videoId": "vid2"},
                        },
                    ]
                }
            )
        if endpoint == "videos":
            return 200, json.dumps(
                {
                    "items": [
                        {
                            "id": "vid1",
                            "snippet": {
                                "title": "First enriched title",
                                "publishedAt": "2026-01-01T00:00:00Z",
                            },
                            "contentDetails": {"duration": "PT1M2S"},
                            "statistics": {"viewCount": "100"},
                            "status": {"privacyStatus": "public"},
                        },
                        {
                            "id": "vid2",
                            "snippet": {
                                "title": "Second enriched title",
                                "publishedAt": "2026-01-02T00:00:00Z",
                            },
                            "contentDetails": {"duration": "PT3M4S"},
                            "statistics": {"viewCount": "200"},
                            "status": {"privacyStatus": "public"},
                        },
                    ]
                }
            )
        return 404, json.dumps({"error": {"message": "not found"}})

    return fake_fetcher


def _sample_youtube_playlist_data_api_fetcher(
    calls: list[tuple[str, dict[str, str], int]],
) -> object:
    def fake_fetcher(
        endpoint: str,
        params: dict[str, str],
        *,
        timeout: int = 30,
    ) -> tuple[int, str]:
        calls.append((endpoint, params, timeout))
        if endpoint == "playlistItems" and "pageToken" not in params:
            return 200, json.dumps(
                {
                    "nextPageToken": "NEXT",
                    "items": [
                        {
                            "snippet": {
                                "title": "Playlist first title",
                                "publishedAt": "2026-01-01T00:00:00Z",
                                "channelTitle": "Playlist Owner",
                                "videoOwnerChannelTitle": "Video Owner",
                            },
                            "contentDetails": {
                                "videoId": "plvid1",
                                "videoPublishedAt": "2025-12-31T00:00:00Z",
                            },
                        }
                    ],
                }
            )
        if endpoint == "playlistItems" and params.get("pageToken") == "NEXT":
            return 200, json.dumps(
                {
                    "items": [
                        {
                            "snippet": {
                                "title": "Playlist second title",
                                "publishedAt": "2026-01-02T00:00:00Z",
                                "channelTitle": "Playlist Owner",
                            },
                            "contentDetails": {"videoId": "plvid2"},
                        }
                    ]
                }
            )
        if endpoint == "videos":
            return 200, json.dumps(
                {
                    "items": [
                        {
                            "id": "plvid1",
                            "snippet": {
                                "title": "Video first enriched title",
                                "publishedAt": "2025-12-31T00:00:00Z",
                                "channelTitle": "Video Owner",
                            },
                            "contentDetails": {"duration": "PT10S"},
                            "statistics": {"viewCount": "10"},
                            "status": {"privacyStatus": "public"},
                        },
                        {
                            "id": "plvid2",
                            "snippet": {
                                "title": "Video second enriched title",
                                "publishedAt": "2026-01-02T00:00:00Z",
                                "channelTitle": "Second Owner",
                            },
                        },
                    ]
                }
            )
        return 404, json.dumps({"error": {"message": "not found"}})

    return fake_fetcher


class CliInputTests(unittest.TestCase):
    def test_stdin_input_helpers_keep_selectors_memory_only(self) -> None:
        with patch.object(sys, "stdin", io.StringIO("# comment\nhttps://example.test/item\n")):
            self.assertEqual(cli_module._read_single_input("-"), "https://example.test/item")
        with patch.object(sys, "stdin", io.StringIO("https://example.test/a\nhttps://example.test/b\n")):
            self.assertEqual(
                cli_module._read_urls_file("-"),
                ["https://example.test/a", "https://example.test/b"],
            )

    def test_single_stdin_rejects_zero_or_multiple_inputs(self) -> None:
        for payload in ("", "https://example.test/a\nhttps://example.test/b\n"):
            with self.subTest(payload=payload), patch.object(sys, "stdin", io.StringIO(payload)):
                with self.assertRaisesRegex(ValueError, "exactly one"):
                    cli_module._read_single_input("-")


class DetectorTests(unittest.TestCase):
    def test_detects_v0_platforms(self) -> None:
        cases = {
            "https://www.bilibili.com/video/BV1xx411c7mD": "bilibili",
            "BV1xx411c7mD": "bilibili",
            "https://youtu.be/example": "youtube",
            "https://mp.weixin.qq.com/s/example": "weixin",
            "https://www.xiaoyuzhoufm.com/episode/example": "xiaoyuzhou",
            "https://www.douyin.com/video/example": "douyin",
            "4.33 example https://v.douyin.com/60MDLsGxFyw/ copied text": "douyin",
            "https://www.tiktok.com/@user/video/example": "tiktok",
            "https://www.xiaohongshu.com/explore/example": "xiaohongshu",
            "https://demo.xet.citv.cn/p/example": "xiaoe",
            "https://appm1dwjnky5483.h5.xet.pomoho.com/p/course/video/example": "xiaoe",
            "https://x.com/example/status/1": "x",
            "https://twitter.com/example/status/1": "x",
            "https://example.feishu.cn/docx/example": "feishu",
            "https://example.com/article": "web_article",
            "https://cdn.example.com/video.mp4": "generic_media",
            str(Path("C:/fixture/audio/openmd-sample.wav")): "generic_media",
            "file:///C:/fixture/audio/openmd-sample.wav": "generic_media",
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                self.assertEqual(detect_platform(url), expected)


class RendererOutputTests(unittest.TestCase):
    def test_writes_schema_markdown_and_avoids_duplicate_overwrite(self) -> None:
        item = ContentItem(
            platform="youtube",
            input_type="single",
            url="https://youtu.be/example",
            title="Sample: Video?",
            author="tester",
            source_type="video",
        )
        result = TranscriptResult(
            text="[00:00:01] raw transcript line\n[00:00:02] second line",
            status="verified",
            source_level="native_caption",
            backend="mock",
            source_notes=["timestamp preservation smoke test"],
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            first = write_item_result(item, result, tmp_dir)
            second = write_item_result(item, result, tmp_dir)

            self.assertNotEqual(first, second)
            markdown = first.read_text(encoding="utf-8")
            self.assertIn("platform: youtube", markdown)
            self.assertIn("url: https://youtu.be/example", markdown)
            self.assertIn("title: \"Sample: Video?\"", markdown)
            self.assertIn("status: verified", markdown)
            self.assertIn("source_level: native_caption", markdown)
            self.assertIn("## Transcript / Content", markdown)
            self.assertIn("[00:00:01] raw transcript line", markdown)
            self.assertIn("## Source Notes", markdown)


class RunlogTests(unittest.TestCase):
    def test_runlog_is_jsonl_and_sanitized(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            runlog = Path(tmp_dir) / "runlog.jsonl"
            success = build_runlog_entry(
                input_url="https://example.com/ok",
                platform="web_article",
                backend="mock",
                status="verified",
                source_type="article",
                source_level="native_text",
                output_path=Path(tmp_dir) / "ok.md",
            )
            failure = build_runlog_entry(
                input_url="https://example.com/fail",
                platform="web_article",
                backend="mock",
                status="failed",
                source_type="article",
                source_level="partial_extraction",
                error_code="mock_failure",
                error_summary="cookie=secret token=secret authorization: bearer secret headers: x <html>raw</html> raw transcript: long text",
            )
            failure["raw_transcript"] = "should not be logged"

            append_runlog_entry(runlog, success)
            append_runlog_entry(runlog, failure)

            lines = runlog.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 2)
            parsed = [json.loads(line) for line in lines]
            self.assertEqual(parsed[0]["status"], "verified")
            self.assertEqual(parsed[1]["status"], "failed")

            content = runlog.read_text(encoding="utf-8").lower()
            for forbidden in (
                "cookie",
                "token",
                "authorization",
                "bearer",
                "headers",
                "<html",
                "raw transcript",
                "should not be logged",
            ):
                self.assertNotIn(forbidden, content)

    def test_runlog_strips_sensitive_query_params_from_urls(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            runlog = Path(tmp_dir) / "runlog.jsonl"
            entry = build_runlog_entry(
                input_url=(
                    "https://www.xiaohongshu.com/explore/abc?source=webshare"
                    "&xsec_token=secret&access_token=secret"
                ),
                platform="xiaohongshu",
                backend="mock",
                status="verified",
                source_type="mixed",
                source_level="native_text",
                output_path=Path(tmp_dir) / "ok.md",
            )

            append_runlog_entry(runlog, entry)

            content = runlog.read_text(encoding="utf-8")
            self.assertIn("source=webshare", content)
            self.assertNotIn("xsec_token", content)
            self.assertNotIn("access_token", content)
            self.assertNotIn("secret", content)


class SanitizerTests(unittest.TestCase):
    def test_sanitize_url_text_strips_sensitive_query_params(self) -> None:
        value = (
            "分享 https://www.xiaohongshu.com/discovery/item/abc?source=webshare"
            "&xsec_token=secret&xsec_source=pc_share&refresh_token=secret"
        )

        sanitized = sanitize_url_text(value)

        self.assertIn("source=webshare", sanitized)
        self.assertIn("xsec_source=pc_share", sanitized)
        self.assertNotIn("xsec_token", sanitized)
        self.assertNotIn("refresh_token", sanitized)
        self.assertNotIn("secret", sanitized)

    def test_renderer_frontmatter_strips_sensitive_query_params(self) -> None:
        item = ContentItem(
            platform="xiaohongshu",
            input_type="single",
            url=(
                "https://www.xiaohongshu.com/explore/abc?source=webshare"
                "&xsec_token=secret"
            ),
            title="XHS note",
            source_type="mixed",
        )
        result = TranscriptResult(
            text="raw note text",
            status="verified",
            source_level="native_text",
            backend="mock",
        )

        document = render_markdown_document(item, result)

        self.assertIn("source=webshare", document.frontmatter["url"])
        self.assertNotIn("xsec_token", document.frontmatter["url"])
        self.assertNotIn("secret", document.frontmatter["url"])


class ManifestTests(unittest.TestCase):
    def test_discovery_manifest_row_uses_video_id_and_safe_defaults(self) -> None:
        item = ContentItem(
            platform="youtube",
            input_type="collection",
            url="https://www.youtube.com/watch?v=abc123&access_token=secret",
            id="abc123",
            title="Example video",
            author="Example channel",
            source_type="video",
            raw_meta={
                "channel_id": "UC123",
                "channel_title": "Example channel",
                "duration": "1:23",
                "views": "10K views",
                "published_hint": "1 day ago",
            },
        )

        row = discovery_manifest_row(
            item,
            collection_url="https://www.youtube.com/@example/videos?access_token=secret&view=1",
            provider="fake.discovery",
            limit=20,
        )
        self.assertEqual(row["content_id"], row["video_id"])
        self.assertEqual(row["content_url"], row["video_url"])

        self.assertEqual(row["video_id"], "abc123")
        self.assertEqual(row["video_url"], "https://www.youtube.com/watch?v=abc123")
        self.assertEqual(row["channel_id"], "UC123")
        self.assertEqual(row["channel_title"], "Example channel")
        self.assertEqual(row["caption_status"], "unknown")
        self.assertEqual(row["process_status"], "discovered")
        self.assertEqual(row["discovery_provider"], "fake.discovery")
        self.assertEqual(row["discovery_limit"], 20)
        self.assertNotIn("access_token", row["collection_url"])
        self.assertNotIn("secret", row["collection_url"])

    def test_write_discovery_manifest_jsonl_deduplicates_by_video_id(self) -> None:
        items = parse_opencli_youtube_channel_json(
            _sample_opencli_channel_json(),
            collection_url="https://www.youtube.com/@ExampleChannel/videos",
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            first_path = write_discovery_manifest(
                out_dir=tmp_dir,
                platform="youtube",
                collection_url="https://www.youtube.com/@ExampleChannel/videos",
                provider=YOUTUBE_DISCOVERY_BACKEND,
                items=items,
                limit=20,
            )
            second_path = write_discovery_manifest(
                out_dir=tmp_dir,
                platform="youtube",
                collection_url="https://www.youtube.com/@ExampleChannel/videos",
                provider=YOUTUBE_DISCOVERY_BACKEND,
                items=items,
                limit=20,
            )

            self.assertEqual(first_path, second_path)
            self.assertEqual(first_path, default_manifest_path(
                tmp_dir,
                platform="youtube",
                collection_url="https://www.youtube.com/@ExampleChannel/videos",
            ))
            rows = [
                json.loads(line)
                for line in first_path.read_text(encoding="utf-8").splitlines()
            ]

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["video_id"], "demoVideo01")
        self.assertEqual(rows[0]["caption_status"], "unknown")
        self.assertEqual(rows[0]["process_status"], "discovered")
        self.assertEqual(rows[0]["discovery_provider"], YOUTUBE_DISCOVERY_BACKEND)

    def test_discovery_manifest_deduplicates_legacy_and_generic_identity_fields(self) -> None:
        item = ContentItem(
            platform="example",
            input_type="collection",
            url="https://example.com/content/1",
            id="content-1",
            title="Content 1",
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = default_manifest_path(
                tmp_dir,
                platform="example",
                collection_url="https://example.com/user/1",
            )
            path.parent.mkdir(parents=True)
            path.write_text(
                json.dumps(
                    {
                        "platform": "example",
                        "video_id": "content-1",
                        "video_url": "https://example.com/content/1",
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            write_discovery_manifest(
                out_dir=tmp_dir,
                platform="example",
                collection_url="https://example.com/user/1",
                provider="fake.discovery",
                items=[item],
            )
            rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["video_id"], "content-1")


class BilibiliProviderTests(unittest.TestCase):
    def test_wbi_signing_generates_wts_and_w_rid(self) -> None:
        signed = sign_wbi_params_with_keys(
            {
                "cid": 123,
                "aid": 456,
                "skip": None,
                "spm_id_from": "333.788.home!()* test",
                "empty": "",
            },
            img_key="7cd084941338484aae1ad9425b84077c",
            sub_key="4932caff0ff746eab6f01bf08b70ac45",
            wts=1700000000,
        )

        self.assertEqual(signed["wts"], "1700000000")
        self.assertEqual(signed["w_rid"], "57947ed0fc78857e0e67f6f51dc82190")
        self.assertEqual(len(signed["w_rid"]), 32)
        self.assertEqual(signed["spm_id_from"], "333.788.home test")
        self.assertNotIn("skip", signed)

    def test_wbi_key_cache_reuses_nav_within_ttl(self) -> None:
        calls: list[str] = []
        now = [1700000000.0]

        def fake_fetcher(url: str, params: dict[str, str], timeout: int) -> tuple[int, str]:
            calls.append(url)
            self.assertEqual(params, {})
            self.assertEqual(timeout, 10)
            return 200, json.dumps(
                {
                    "code": 0,
                    "data": {
                        "wbi_img": {
                            "img_url": "https://i0.hdslb.com/bfs/wbi/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.png",
                            "sub_url": "https://i0.hdslb.com/bfs/wbi/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb.png",
                        }
                    },
                }
            )

        signer = BilibiliWbiSigner(fetcher=fake_fetcher, timeout=10, time_provider=lambda: now[0])

        self.assertEqual(signer.get_keys(), ("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"))
        now[0] += 60
        self.assertEqual(signer.get_keys(), ("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"))
        self.assertEqual(calls, [BILIBILI_NAV_URL])

    def test_bilibili_cookie_header_is_env_only_and_not_in_failure_summary(self) -> None:
        self.assertEqual(
            bilibili_cookie_header({BILIBILI_COOKIE_HEADER_ENV: "env-placeholder"}),
            "env-placeholder",
        )
        self.assertEqual(
            bilibili_cookie_header_for_url(
                "https://api.bilibili.com/x/web-interface/view",
                {BILIBILI_COOKIE_HEADER_ENV: "env-placeholder"},
            ),
            "env-placeholder",
        )
        self.assertIsNone(
            bilibili_cookie_header_for_url(
                "https://subtitle.example.com/sample.json",
                {BILIBILI_COOKIE_HEADER_ENV: "env-placeholder"},
            )
        )
        headers = bilibili_headers(cookie_header="env-placeholder")
        self.assertIn("Referer", headers)
        self.assertIn("Origin", headers)
        self.assertIn("Accept", headers)
        self.assertIn("X-Wbi-UA", headers)

        def fake_fetcher(url: str, params: dict[str, str], timeout: int) -> tuple[int, str]:
            raise ValueError(
                "cookie=value-placeholder token=value-placeholder headers={'Cookie': 'value'} raw api response"
            )

        provider = BilibiliNativeSubtitleProvider(fetcher=fake_fetcher)
        item = ContentItem(
            platform="bilibili",
            input_type="single",
            url="https://www.bilibili.com/video/BV1secret123/",
            source_type="video",
        )

        result = provider.extract(
            BackendExtractRequest(
                item=item,
                backend_chain=(BILIBILI_HARVEST_BACKEND,),
                allow_fallback=False,
            )
        )

        self.assertFalse(result.has_usable_text)
        summary = result.error_summary or ""
        self.assertNotIn("value-placeholder", summary)
        self.assertNotIn("cookie", summary.lower())
        self.assertNotIn("headers", summary.lower())

    def test_native_subtitle_provider_signs_wbi_success(self) -> None:
        calls: list[tuple[str, dict[str, str]]] = []

        def fake_fetcher(url: str, params: dict[str, str], timeout: int) -> tuple[int, str]:
            calls.append((url, params))
            if url == BILIBILI_NAV_URL:
                return 200, json.dumps(
                    {
                        "code": 0,
                        "data": {
                            "wbi_img": {
                                "img_url": "https://i0.hdslb.com/bfs/wbi/7cd084941338484aae1ad9425b84077c.png",
                                "sub_url": "https://i0.hdslb.com/bfs/wbi/4932caff0ff746eab6f01bf08b70ac45.png",
                            }
                        },
                    }
                )
            if url.endswith("/x/web-interface/view"):
                return 200, json.dumps({"code": 0, "data": {"aid": 456, "cid": 123}})
            if url.endswith("/x/player/wbi/v2"):
                self.assertEqual(params["aid"], "456")
                self.assertEqual(params["cid"], "123")
                self.assertIn("wts", params)
                self.assertIn("w_rid", params)
                return 200, json.dumps(
                    {
                        "code": 0,
                        "data": {
                            "subtitle": {
                                "subtitles": [
                                    {"lan": "zh-CN", "subtitle_url": "//subtitle.example.com/signed.json"}
                                ]
                            }
                        },
                    }
                )
            if url == "https://subtitle.example.com/signed.json":
                return 200, json.dumps({"body": [{"from": 0, "to": 1, "content": "signed"}]})
            return 404, "{}"

        signer = BilibiliWbiSigner(fetcher=fake_fetcher, time_provider=lambda: 1700000000.0)
        provider = BilibiliNativeSubtitleProvider(fetcher=fake_fetcher, wbi_signer=signer)
        item = ContentItem(
            platform="bilibili",
            input_type="single",
            url="https://www.bilibili.com/video/BV1signed123/",
            source_type="video",
        )

        result = provider.extract(
            BackendExtractRequest(
                item=item,
                backend_chain=(BILIBILI_HARVEST_BACKEND,),
                allow_fallback=False,
            )
        )

        self.assertTrue(result.has_usable_text)
        self.assertEqual(result.metadata["subtitle_endpoint_used"], "wbi_v2")
        self.assertTrue(any(url.endswith("/x/player/wbi/v2") for url, _params in calls))

    def test_native_subtitle_provider_marks_ai_caption_without_persisting_raw(self) -> None:
        calls: list[tuple[str, dict[str, str], int]] = []

        def fake_fetcher(url: str, params: dict[str, str], timeout: int) -> tuple[int, str]:
            calls.append((url, params, timeout))
            if url.endswith("/x/web-interface/view"):
                return 200, json.dumps(
                    {
                        "code": 0,
                        "data": {
                            "cid": 123,
                            "title": "Bilibili sample",
                            "pubdate": 1767139200,
                            "owner": {"name": "Sample UP"},
                        },
                    }
                )
            if url.endswith("/x/player/v2"):
                return 200, json.dumps(
                    {
                        "code": 0,
                        "data": {
                            "subtitle": {
                                "subtitles": [
                                    {
                                        "lan": "zh-CN",
                                        "lan_doc": "AI字幕",
                                        "ai_status": 2,
                                        "subtitle_url": "//subtitle.example.com/sample.json",
                                    }
                                ]
                            }
                        },
                    }
                )
            if url == "https://subtitle.example.com/sample.json":
                return 200, json.dumps(
                    {
                        "body": [
                            {"from": 0.0, "to": 1.0, "content": "第一句"},
                            {"from": 62.4, "to": 64.0, "content": "第二句"},
                        ]
                    }
                )
            return 404, "{}"

        provider = BilibiliNativeSubtitleProvider(fetcher=fake_fetcher)
        item = ContentItem(
            platform="bilibili",
            input_type="single",
            url="https://www.bilibili.com/video/BV1abc123456/",
            source_type="video",
        )

        result = provider.extract(
            BackendExtractRequest(
                item=item,
                backend_chain=(BILIBILI_HARVEST_BACKEND,),
                allow_fallback=False,
            )
        )

        self.assertTrue(result.has_usable_text)
        self.assertEqual(result.backend, BILIBILI_HARVEST_BACKEND)
        self.assertEqual(result.source_level, "auto_caption")
        self.assertEqual(result.title, "Bilibili sample")
        self.assertEqual(result.author, "Sample UP")
        self.assertIn("[00:00:00] 第一句", result.text)
        self.assertIn("[00:01:02] 第二句", result.text)
        self.assertEqual(result.metadata["segment_count"], 2)
        self.assertIn("No media", " ".join(result.source_notes))
        self.assertEqual(calls[0][1], {"bvid": "BV1abc123456"})

    def test_native_subtitle_provider_tries_wbi_then_v2_and_filters_danmaku(self) -> None:
        calls: list[str] = []

        def fake_fetcher(url: str, params: dict[str, str], timeout: int) -> tuple[int, str]:
            calls.append(url)
            if url.endswith("/x/web-interface/view"):
                return 200, json.dumps(
                    {"code": 0, "data": {"aid": 456, "cid": 123, "title": "Sample"}}
                )
            if url.endswith("/x/player/wbi/v2"):
                return 200, json.dumps(
                    {
                        "code": 0,
                        "data": {
                            "subtitle": {
                                "subtitles": [
                                    {"lan": "danmaku", "subtitle_url": "//comment.example.com/danmaku.xml"},
                                    {"lan": "zh-CN", "subtitle_url": "//comment.example.com/subtitle.xml"},
                                ]
                            }
                        },
                    }
                )
            if url.endswith("/x/player/v2"):
                return 200, json.dumps(
                    {
                        "code": 0,
                        "data": {
                            "subtitle": {
                                "subtitles": [
                                    {"lan": "zh-CN", "subtitle_url": "//subtitle.example.com/manual.json"}
                                ]
                            }
                        },
                    }
                )
            if url == "https://subtitle.example.com/manual.json":
                return 200, json.dumps({"body": [{"from": 0, "to": 1, "content": "manual"}]})
            return 404, "{}"

        provider = BilibiliNativeSubtitleProvider(fetcher=fake_fetcher)
        item = ContentItem(
            platform="bilibili",
            input_type="single",
            url="https://www.bilibili.com/video/BV1wbi123456/",
            source_type="video",
        )

        result = provider.extract(
            BackendExtractRequest(
                item=item,
                backend_chain=(BILIBILI_HARVEST_BACKEND,),
                allow_fallback=False,
            )
        )

        self.assertTrue(result.has_usable_text)
        self.assertEqual(result.metadata["subtitle_endpoint_used"], "v2")
        self.assertEqual(result.source_level, "native_caption")
        self.assertIn("[00:00:00] manual", result.text)
        wbi_index = next(index for index, url in enumerate(calls) if url.endswith("/x/player/wbi/v2"))
        v2_index = next(index for index, url in enumerate(calls) if url.endswith("/x/player/v2"))
        self.assertLess(wbi_index, v2_index)
        self.assertIn("Bilibili danmaku/XML subtitle tracks were filtered", " ".join(result.source_notes))

    def test_native_subtitle_provider_prefers_uploader_over_ai_same_language(self) -> None:
        fetched_subtitle_urls: list[str] = []

        def fake_fetcher(url: str, params: dict[str, str], timeout: int) -> tuple[int, str]:
            if url.endswith("/x/web-interface/view"):
                return 200, json.dumps({"code": 0, "data": {"aid": 456, "cid": 123}})
            if url.endswith("/x/player/wbi/v2"):
                return 200, json.dumps(
                    {
                        "code": 0,
                        "data": {
                            "subtitle": {
                                "subtitles": [
                                    {
                                        "lan": "zh-CN",
                                        "lan_doc": "AI字幕",
                                        "ai_status": 1,
                                        "subtitle_url": "//subtitle.example.com/ai.json",
                                    },
                                    {
                                        "lan": "zh-CN",
                                        "lan_doc": "中文",
                                        "subtitle_url": "//subtitle.example.com/manual.json",
                                    },
                                ]
                            }
                        },
                    }
                )
            if url.startswith("https://subtitle.example.com/"):
                fetched_subtitle_urls.append(url)
                return 200, json.dumps({"body": [{"from": 0, "to": 1, "content": "chosen"}]})
            return 404, "{}"

        provider = BilibiliNativeSubtitleProvider(fetcher=fake_fetcher)
        item = ContentItem(
            platform="bilibili",
            input_type="single",
            url="https://www.bilibili.com/video/BV1manual123/",
            source_type="video",
        )

        result = provider.extract(
            BackendExtractRequest(
                item=item,
                backend_chain=(BILIBILI_HARVEST_BACKEND,),
                allow_fallback=False,
            )
        )

        self.assertTrue(result.has_usable_text)
        self.assertEqual(fetched_subtitle_urls, ["https://subtitle.example.com/manual.json"])
        self.assertEqual(result.metadata["caption_track_type"], "uploader")
        self.assertEqual(result.source_level, "native_caption")

    def test_native_subtitle_provider_recovers_empty_ai_url_with_search_stat(self) -> None:
        calls: list[str] = []

        def fake_fetcher(url: str, params: dict[str, str], timeout: int) -> tuple[int, str]:
            calls.append(url)
            if url.endswith("/x/web-interface/view"):
                return 200, json.dumps({"code": 0, "data": {"aid": 456, "cid": 123}})
            if url.endswith("/x/player/wbi/v2"):
                return 200, json.dumps(
                    {
                        "code": 0,
                        "data": {
                            "subtitle": {
                                "subtitles": [
                                    {"lan": "ai-zh", "ai_status": 1, "subtitle_url": ""}
                                ]
                            }
                        },
                    }
                )
            if url.endswith("/x/player/v2/ai/subtitle/search/stat"):
                return 200, json.dumps(
                    {"code": 0, "data": {"subtitle_url": "//subtitle.example.com/rescued.json"}}
                )
            if url == "https://subtitle.example.com/rescued.json":
                return 200, json.dumps({"body": [{"from": 0, "to": 1, "content": "rescued"}]})
            return 404, "{}"

        provider = BilibiliNativeSubtitleProvider(fetcher=fake_fetcher)
        item = ContentItem(
            platform="bilibili",
            input_type="single",
            url="https://www.bilibili.com/video/BV1rescue123/",
            source_type="video",
        )

        result = provider.extract(
            BackendExtractRequest(
                item=item,
                backend_chain=(BILIBILI_HARVEST_BACKEND,),
                allow_fallback=False,
            )
        )

        self.assertTrue(result.has_usable_text)
        self.assertEqual(result.source_level, "auto_caption")
        self.assertIn("[00:00:00] rescued", result.text)
        self.assertTrue(any(url.endswith("/x/player/v2/ai/subtitle/search/stat") for url in calls))
        self.assertIn("AI subtitle URL was recovered", " ".join(result.source_notes))

    def test_native_subtitle_provider_retries_async_ai_subtitle_discovery(self) -> None:
        wbi_attempts = 0

        def fake_fetcher(url: str, params: dict[str, str], timeout: int) -> tuple[int, str]:
            nonlocal wbi_attempts
            if url == BILIBILI_NAV_URL:
                return 200, json.dumps(
                    {
                        "code": 0,
                        "data": {
                            "wbi_img": {
                                "img_url": "https://i0.hdslb.com/bfs/wbi/7cd084941338484aae1ad9425b84077c.png",
                                "sub_url": "https://i0.hdslb.com/bfs/wbi/4932caff0ff746eab6f01bf08b70ac45.png",
                            }
                        },
                    }
                )
            if url.endswith("/x/web-interface/view"):
                return 200, json.dumps({"code": 0, "data": {"aid": 456, "cid": 123}})
            if url.endswith("/x/player/wbi/v2"):
                wbi_attempts += 1
                if wbi_attempts < 3:
                    return 200, json.dumps({"code": 0, "data": {"subtitle": {"subtitles": []}}})
                return 200, json.dumps(
                    {
                        "code": 0,
                        "data": {
                            "subtitle": {
                                "subtitles": [
                                    {
                                        "lan": "ai-zh",
                                        "ai_status": 1,
                                        "subtitle_url": "//subtitle.example.com/retry.json",
                                    }
                                ]
                            }
                        },
                    }
                )
            if url == "https://subtitle.example.com/retry.json":
                return 200, json.dumps({"body": [{"from": 0, "to": 1, "content": "retry"}]})
            return 404, "{}"

        signer = BilibiliWbiSigner(fetcher=fake_fetcher, time_provider=lambda: 1700000000.0)
        provider = BilibiliNativeSubtitleProvider(
            fetcher=fake_fetcher,
            wbi_signer=signer,
            max_ai_retries=3,
        )
        item = ContentItem(
            platform="bilibili",
            input_type="single",
            url="https://www.bilibili.com/video/BV1retry1234/",
            source_type="video",
        )

        result = provider.extract(
            BackendExtractRequest(
                item=item,
                backend_chain=(BILIBILI_HARVEST_BACKEND,),
                allow_fallback=False,
            )
        )

        self.assertTrue(result.has_usable_text)
        self.assertEqual(wbi_attempts, 3)
        self.assertEqual(result.metadata["subtitle_endpoint_used"], "wbi_v2")
        self.assertIn("async_ai_subtitle_retries=3", result.source_notes)

    def test_native_subtitle_provider_returns_structured_no_subtitle_failure(self) -> None:
        def fake_fetcher(url: str, params: dict[str, str], timeout: int) -> tuple[int, str]:
            if url == BILIBILI_NAV_URL:
                return 500, "{}"
            if url.endswith("/x/web-interface/view"):
                return 200, json.dumps({"code": 0, "data": {"aid": 456, "cid": 123}})
            if url.endswith("/x/player/wbi/v2") or url.endswith("/x/player/v2"):
                return 200, json.dumps({"code": 0, "data": {"subtitle": {"subtitles": []}}})
            return 404, "{}"

        provider = BilibiliNativeSubtitleProvider(fetcher=fake_fetcher, max_ai_retries=1)
        item = ContentItem(
            platform="bilibili",
            input_type="single",
            url="https://www.bilibili.com/video/BV1nosub1234/",
            source_type="video",
        )

        result = provider.extract(
            BackendExtractRequest(
                item=item,
                backend_chain=(BILIBILI_HARVEST_BACKEND,),
                allow_fallback=False,
            )
        )

        self.assertFalse(result.has_usable_text)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.error_code, "bilibili_subtitle_missing")
        self.assertIn("public", " ".join(result.source_notes).lower())

    def test_bilibili_asr_fallback_requires_explicit_media_and_asr_flags(self) -> None:
        def forbidden_runner(command: list[str], timeout: int) -> tuple[int, str, str]:
            raise AssertionError(f"unexpected command: {command}")

        provider = BilibiliMediaAsrFallbackProvider(
            BackendRuntimeOptions(),
            runner=forbidden_runner,
        )
        item = ContentItem(
            platform="bilibili",
            input_type="single",
            url="https://www.bilibili.com/video/BV1nosub1234/",
            source_type="video",
        )

        result = provider.extract(
            BackendExtractRequest(
                item=item,
                backend_chain=(BILIBILI_ASR_FALLBACK_BACKEND,),
                allow_fallback=False,
            )
        )

        self.assertFalse(result.has_usable_text)
        self.assertEqual(result.error_code, "bilibili_media_asr_not_enabled")
        self.assertEqual(result.metadata["failure_class"], "user_resolvable")
        self.assertEqual(
            result.metadata["missing_flags"],
            ["--enable-media-download", "--enable-asr"],
        )
        self.assertIn("No media download or ASR action was attempted", " ".join(result.source_notes))

    def test_bilibili_media_asr_fallback_downloads_temp_media_and_does_not_persist_path_by_default(
        self,
    ) -> None:
        seen_commands: list[list[str]] = []

        def fake_runner(command: list[str], timeout: int) -> tuple[int, str, str]:
            seen_commands.append(command)
            output_template = command[command.index("--output") + 1]
            media_path = Path(output_template.replace("%(ext)s", "wav"))
            media_path.write_bytes(b"fake wav")
            return 0, "", ""

        class FakeASRBackend:
            name = "asr.fake"

            def __init__(self) -> None:
                self.seen_path: Path | None = None

            def transcribe(self, path: Path):
                self.seen_path = path
                return TranscriptResult(
                    text="[00:00:00] fallback transcript\n",
                    status="verified",
                    source_level="asr",
                    backend=self.name,
                    source_notes=["fake local ASR"],
                    metadata={"source_ref": str(path), "asr_backend": self.name},
                )

        fake_asr = FakeASRBackend()
        provider = BilibiliMediaAsrFallbackProvider(
            BackendRuntimeOptions(
                enable_media_download_backends=True,
                enable_asr_backends=True,
            ),
            runner=fake_runner,
            asr_backend=fake_asr,
        )
        item = ContentItem(
            platform="bilibili",
            input_type="single",
            url="https://www.bilibili.com/video/BV1nosub1234/",
            title="No subtitle sample",
            source_type="video",
        )

        result = provider.extract(
            BackendExtractRequest(
                item=item,
                backend_chain=(BILIBILI_ASR_FALLBACK_BACKEND,),
                allow_fallback=False,
            )
        )

        self.assertTrue(result.has_usable_text)
        self.assertEqual(result.source_level, "media_asr")
        self.assertEqual(result.backend, "asr.fake")
        self.assertEqual(result.title, "No subtitle sample")
        self.assertEqual(result.metadata["bvid"], "BV1nosub1234")
        self.assertEqual(result.metadata["media_download_backend"], BILIBILI_YTDLP_MEDIA_BACKEND)
        self.assertEqual(result.metadata["media_retention"], "deleted")
        self.assertFalse(result.metadata["raw_media_retained"])
        self.assertNotIn("source_ref", result.metadata)
        self.assertIsNotNone(fake_asr.seen_path)
        self.assertFalse(fake_asr.seen_path.exists())
        self.assertEqual(seen_commands[0][0], "yt-dlp")
        self.assertIn("--extract-audio", seen_commands[0])

    def test_opencli_subtitle_provider_normalizes_table_output_as_auto_caption(self) -> None:
        calls: list[tuple[list[str], int]] = []

        def fake_runner(command: list[str], timeout: int) -> tuple[int, str, str]:
            calls.append((command, timeout))
            return (
                0,
                json.dumps(
                    [
                        {"from": 0, "to": 1, "content": "hello"},
                        {"from": 65, "to": 67, "content": "world"},
                    ]
                ),
                "raw stderr should not be used on success",
            )

        provider = OpenCliBilibiliSubtitleProvider(runner=fake_runner)
        item = ContentItem(
            platform="bilibili",
            input_type="single",
            url="https://www.bilibili.com/video/BV1opencli123/",
            source_type="video",
        )

        result = provider.extract(
            BackendExtractRequest(
                item=item,
                backend_chain=(BILIBILI_OPENCLI_SUBTITLE_BACKEND,),
                allow_fallback=False,
            )
        )

        self.assertTrue(result.has_usable_text)
        self.assertEqual(result.backend, BILIBILI_OPENCLI_SUBTITLE_BACKEND)
        self.assertEqual(result.source_level, "auto_caption")
        self.assertEqual(result.text, "[00:00:00] hello\n[00:01:05] world\n")
        self.assertEqual(
            calls[0][0],
            ["opencli", "bilibili", "subtitle", "BV1opencli123", "-f", "json"],
        )
        self.assertIn("No raw stdout/stderr", " ".join(result.source_notes))

    def test_native_user_videos_discovery_does_not_extract_items(self) -> None:
        calls: list[tuple[str, dict[str, str]]] = []

        def fake_fetcher(url: str, params: dict[str, str], timeout: int) -> tuple[int, str]:
            calls.append((url, params))
            if url == BILIBILI_NAV_URL:
                return 200, json.dumps(
                    {
                        "code": 0,
                        "data": {
                            "wbi_img": {
                                "img_url": "https://i0.hdslb.com/bfs/wbi/7cd084941338484aae1ad9425b84077c.png",
                                "sub_url": "https://i0.hdslb.com/bfs/wbi/4932caff0ff746eab6f01bf08b70ac45.png",
                            }
                        },
                    }
                )
            self.assertTrue(url.endswith("/x/space/wbi/arc/search"))
            self.assertEqual(params["mid"], "441831884")
            self.assertEqual(params["ps"], "1")
            self.assertIn("wts", params)
            self.assertIn("w_rid", params)
            return 200, json.dumps(
                {
                    "code": 0,
                    "data": {
                        "list": {
                            "vlist": [
                                {
                                    "bvid": "BV1discover123",
                                    "title": "Discovered video",
                                    "author": "Sample UP",
                                    "created": 1767139200,
                                    "arcurl": "https://www.bilibili.com/video/BV1discover123/",
                                },
                                {
                                    "bvid": "BV1ignored1234",
                                    "title": "Ignored by limit",
                                },
                            ]
                        }
                    },
                }
            )

        provider = BilibiliNativeUserVideosProvider(fetcher=fake_fetcher)
        result = provider.discover(
            DiscoveryRequest(
                input_url="https://space.bilibili.com/441831884",
                platform="bilibili",
                limit=1,
            )
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.provider, BILIBILI_NATIVE_DISCOVERY_BACKEND)
        self.assertEqual(len(result.items), 1)
        self.assertEqual(result.items[0].id, "BV1discover123")
        self.assertEqual(result.items[0].input_type, "single")
        self.assertEqual(result.items[0].raw_meta["discovery_provider"], BILIBILI_NATIVE_DISCOVERY_BACKEND)
        self.assertIn("extraction was not attempted", " ".join(result.source_notes))
        self.assertEqual(result.metadata["discovery_endpoint_used"], "x/space/wbi/arc/search")
        self.assertIn("signed x/space/wbi/arc/search", result.metadata["stability_note"])
        self.assertTrue(any(url.endswith("/x/space/wbi/arc/search") for url, _params in calls))

    def test_native_user_videos_discovery_falls_back_to_public_endpoint(self) -> None:
        calls: list[str] = []

        def fake_fetcher(url: str, params: dict[str, str], timeout: int) -> tuple[int, str]:
            calls.append(url)
            if url == BILIBILI_NAV_URL:
                return 500, "{}"
            if url.endswith("/x/space/arc/search"):
                self.assertEqual(params["mid"], "441831884")
                return 200, json.dumps(
                    {
                        "code": 0,
                        "data": {
                            "list": {
                                "vlist": [
                                    {
                                        "bvid": "BV1fallback123",
                                        "title": "Fallback video",
                                        "arcurl": "https://www.bilibili.com/video/BV1fallback123/",
                                    }
                                ]
                            }
                        },
                    }
                )
            return 404, "{}"

        provider = BilibiliNativeUserVideosProvider(fetcher=fake_fetcher)
        result = provider.discover(
            DiscoveryRequest(
                input_url="https://space.bilibili.com/441831884",
                platform="bilibili",
                limit=1,
            )
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.items[0].id, "BV1fallback123")
        self.assertEqual(result.metadata["discovery_endpoint_used"], "x/space/arc/search")
        self.assertTrue(any(url.endswith("/x/space/arc/search") for url in calls))

    def test_opencli_user_videos_discovery_parses_limited_rows(self) -> None:
        calls: list[tuple[list[str], int]] = []

        def fake_runner(command: list[str], timeout: int) -> tuple[int, str, str]:
            calls.append((command, timeout))
            return (
                0,
                (
                    "rank,title,plays,likes,date,url\n"
                    "1,First video,100,2,2026-01-01,https://www.bilibili.com/video/BV1first12345/\n"
                    "2,Second video,50,1,2026-01-02,https://www.bilibili.com/video/BV1second1234/\n"
                ),
                "",
            )

        provider = OpenCliBilibiliUserVideosProvider(runner=fake_runner)
        result = provider.discover(
            DiscoveryRequest(
                input_url="https://space.bilibili.com/441831884",
                platform="bilibili",
                limit=1,
            )
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.provider, BILIBILI_OPENCLI_DISCOVERY_BACKEND)
        self.assertEqual(len(result.items), 1)
        self.assertEqual(result.items[0].id, "BV1first12345")
        self.assertEqual(result.items[0].title, "First video")
        self.assertEqual(calls[0][0], ["opencli", "bilibili", "user-videos", "441831884", "--limit", "1"])

    def test_runtime_registers_bilibili_providers_only_when_enabled(self) -> None:
        safe_backend_registry = create_runtime_backend_provider_registry(
            "bilibili",
            BackendRuntimeOptions(),
        )
        self.assertEqual(
            safe_backend_registry.provider_for(BILIBILI_HARVEST_BACKEND).descriptor().provider,
            "openmd.skeleton",
        )
        self.assertEqual(
            safe_backend_registry.provider_for(BILIBILI_ASR_FALLBACK_BACKEND).descriptor().provider,
            "openmd.backends.bilibili",
        )
        self.assertEqual(
            safe_backend_registry.provider_for(BILIBILI_YTDLP_MEDIA_BACKEND).descriptor().provider,
            "openmd.backends.bilibili",
        )

        network_backend_registry = create_runtime_backend_provider_registry(
            "bilibili",
            BackendRuntimeOptions(enable_network_backends=True),
        )
        network_descriptor = network_backend_registry.provider_for(
            BILIBILI_HARVEST_BACKEND
        ).descriptor()
        self.assertEqual(network_descriptor.provider, "openmd.backends.bilibili")
        self.assertIn("public API", network_descriptor.description)
        self.assertIn(
            "not the Bilibili-Harvest local/browser pipeline",
            network_descriptor.description,
        )

        browser_backend_registry = create_runtime_backend_provider_registry(
            "bilibili",
            BackendRuntimeOptions(enable_browser_backends=True),
        )
        self.assertEqual(
            browser_backend_registry.provider_for(BILIBILI_OPENCLI_SUBTITLE_BACKEND).descriptor().provider,
            "opencli",
        )

    def test_manifest_only_collect_skips_bilibili_item_extraction(self) -> None:
        items = [
            ContentItem(
                platform="bilibili",
                input_type="single",
                url="https://www.bilibili.com/video/BV1manifest123/",
                id="BV1manifest123",
                title="Manifest only video",
                source_type="video",
            )
        ]
        test_case = self

        class FakeDiscoveryExecutor:
            def discover(self, request: DiscoveryRequest) -> DiscoveryResult:
                test_case.assertEqual(request.platform, "bilibili")
                return DiscoveryResult.success(
                    provider=BILIBILI_NATIVE_DISCOVERY_BACKEND,
                    items=items,
                    metadata={"normalized_url": "https://space.bilibili.com/441831884"},
                )

        def fail_process_items(*_: object, **__: object) -> object:
            raise AssertionError("manifest-only collect must not extract discovered items")

        old_build_discovery_executor = cli_module._build_discovery_executor
        old_process_items = cli_module._process_items
        try:
            cli_module._build_discovery_executor = lambda *_args, **_kwargs: FakeDiscoveryExecutor()
            cli_module._process_items = fail_process_items
            with tempfile.TemporaryDirectory() as tmp_dir:
                with contextlib.redirect_stdout(io.StringIO()) as stdout:
                    exit_code = cli_main(
                        [
                            "collect",
                            "https://space.bilibili.com/441831884",
                            "--out",
                            tmp_dir,
                            "--limit",
                            "1",
                            "--manifest-only",
                        ]
                    )
                output = json.loads(stdout.getvalue())
                manifest_path = Path(output["outputs"][0])
                rows = [
                    json.loads(line)
                    for line in manifest_path.read_text(encoding="utf-8").splitlines()
                ]
        finally:
            cli_module._build_discovery_executor = old_build_discovery_executor
            cli_module._process_items = old_process_items

        self.assertEqual(exit_code, 0)
        self.assertEqual(output["item_count"], 1)
        self.assertEqual(rows[0]["video_id"], "BV1manifest123")
        self.assertEqual(rows[0]["discovery_provider"], BILIBILI_NATIVE_DISCOVERY_BACKEND)

    def test_manifest_only_collect_skips_youtube_playlist_extraction(self) -> None:
        playlist_url = "https://www.youtube.com/watch?v=ignored&list=PLmanifest123"
        items = [
            ContentItem(
                platform="youtube",
                input_type="collection",
                url="https://www.youtube.com/watch?v=ytmanifest1",
                id="ytmanifest1",
                title="Manifest only YouTube video",
                source_type="video",
                raw_meta={"playlist_id": "PLmanifest123"},
            )
        ]
        test_case = self

        class FakeDiscoveryExecutor:
            def discover(self, request: DiscoveryRequest) -> DiscoveryResult:
                test_case.assertEqual(request.platform, "youtube")
                test_case.assertEqual(request.limit, 1)
                return DiscoveryResult.success(
                    provider=YOUTUBE_PLAYLIST_DISCOVERY_BACKEND,
                    items=items,
                    metadata={"normalized_url": "https://www.youtube.com/playlist?list=PLmanifest123"},
                )

        def fake_build_discovery_executor(args: object, *, platform: str) -> FakeDiscoveryExecutor:
            test_case.assertEqual(platform, "youtube")
            test_case.assertEqual(getattr(args, "discovery_backend"), "youtube-playlist")
            return FakeDiscoveryExecutor()

        def fail_process_items(*_: object, **__: object) -> object:
            raise AssertionError("manifest-only collect must not extract discovered items")

        old_build_discovery_executor = cli_module._build_discovery_executor
        old_process_items = cli_module._process_items
        try:
            cli_module._build_discovery_executor = fake_build_discovery_executor
            cli_module._process_items = fail_process_items
            with tempfile.TemporaryDirectory() as tmp_dir:
                with contextlib.redirect_stdout(io.StringIO()) as stdout:
                    exit_code = cli_main(
                        [
                            "collect",
                            playlist_url,
                            "--out",
                            tmp_dir,
                            "--limit",
                            "1",
                            "--manifest-only",
                            "--enable-browser-backends",
                        ]
                    )
                output = json.loads(stdout.getvalue())
                manifest_path = Path(output["outputs"][0])
                rows = [
                    json.loads(line)
                    for line in manifest_path.read_text(encoding="utf-8").splitlines()
                ]
        finally:
            cli_module._build_discovery_executor = old_build_discovery_executor
            cli_module._process_items = old_process_items

        self.assertEqual(exit_code, 0)
        self.assertEqual(output["item_count"], 1)
        self.assertEqual(rows[0]["video_id"], "ytmanifest1")
        self.assertEqual(rows[0]["discovery_provider"], YOUTUBE_PLAYLIST_DISCOVERY_BACKEND)

    def test_manifest_only_collect_skips_youtube_data_api_playlist_extraction(self) -> None:
        playlist_url = "https://www.youtube.com/watch?v=ignored&list=PLmanifest123"
        items = [
            ContentItem(
                platform="youtube",
                input_type="collection",
                url="https://www.youtube.com/watch?v=ytmanifest1",
                id="ytmanifest1",
                title="Manifest only YouTube Data API video",
                source_type="video",
                raw_meta={"playlist_id": "PLmanifest123", "channel_title": "Example channel"},
            )
        ]
        test_case = self

        class FakeDiscoveryExecutor:
            def discover(self, request: DiscoveryRequest) -> DiscoveryResult:
                test_case.assertEqual(request.platform, "youtube")
                test_case.assertEqual(request.limit, 1)
                return DiscoveryResult.success(
                    provider=YOUTUBE_DATA_API_PLAYLIST_DISCOVERY_BACKEND,
                    items=items,
                    metadata={"normalized_url": "https://www.youtube.com/playlist?list=PLmanifest123"},
                )

        def fake_build_discovery_executor(args: object, *, platform: str) -> FakeDiscoveryExecutor:
            test_case.assertEqual(platform, "youtube")
            test_case.assertEqual(getattr(args, "discovery_backend"), YOUTUBE_DATA_API_PLAYLIST_DISCOVERY_BACKEND)
            return FakeDiscoveryExecutor()

        def fail_process_items(*_: object, **__: object) -> object:
            raise AssertionError("manifest-only collect must not extract discovered items")

        old_build_discovery_executor = cli_module._build_discovery_executor
        old_process_items = cli_module._process_items
        try:
            cli_module._build_discovery_executor = fake_build_discovery_executor
            cli_module._process_items = fail_process_items
            with tempfile.TemporaryDirectory() as tmp_dir:
                with contextlib.redirect_stdout(io.StringIO()) as stdout:
                    exit_code = cli_main(
                        [
                            "collect",
                            playlist_url,
                            "--out",
                            tmp_dir,
                            "--limit",
                            "1",
                            "--manifest-only",
                            "--enable-network-backends",
                        ]
                    )
                output = json.loads(stdout.getvalue())
                manifest_path = Path(output["outputs"][0])
                rows = [
                    json.loads(line)
                    for line in manifest_path.read_text(encoding="utf-8").splitlines()
                ]
        finally:
            cli_module._build_discovery_executor = old_build_discovery_executor
            cli_module._process_items = old_process_items

        self.assertEqual(exit_code, 0)
        self.assertEqual(output["item_count"], 1)
        self.assertEqual(rows[0]["video_id"], "ytmanifest1")
        self.assertEqual(rows[0]["playlist_id"], "PLmanifest123")
        self.assertEqual(rows[0]["channel_title"], "Example channel")
        self.assertEqual(rows[0]["discovery_provider"], YOUTUBE_DATA_API_PLAYLIST_DISCOVERY_BACKEND)


class BackendExecutorTests(unittest.TestCase):
    def test_executor_falls_back_after_structured_failure(self) -> None:
        item = ContentItem(
            platform="youtube",
            input_type="single",
            url="https://youtu.be/example",
            source_type="video",
        )
        calls: list[tuple[str, tuple[str, ...], bool]] = []

        class RecordingProvider:
            def __init__(self, *, should_succeed: bool) -> None:
                self.should_succeed = should_succeed

            def extract(self, request: BackendExtractRequest) -> TranscriptResult:
                backend = request.primary_backend
                calls.append((backend, request.backend_chain, request.allow_fallback))
                if self.should_succeed:
                    return TranscriptResult(
                        text="fallback text",
                        status="verified",
                        source_level="third_party_extractor",
                        backend=backend,
                    )
                return TranscriptResult(
                    text="",
                    status="failed",
                    source_level="partial_extraction",
                    backend=backend,
                    error_code="primary_failed",
                    error_summary="primary provider returned structured failure",
                )

        executor = BackendExecutor(
            providers={
                "primary.backend": RecordingProvider(should_succeed=False),
                "fallback.backend": RecordingProvider(should_succeed=True),
            }
        )

        result = executor.extract(item, ("primary.backend", "fallback.backend"))

        self.assertTrue(result.has_usable_text)
        self.assertEqual(result.backend, "fallback.backend")
        self.assertEqual(
            calls,
            [
                ("primary.backend", ("primary.backend",), False),
                ("fallback.backend", ("fallback.backend",), False),
            ],
        )
        self.assertIn("backend_chain: primary.backend -> fallback.backend", result.source_notes)
        self.assertIn(
            "fallback_attempt: primary.backend failed with primary_failed",
            result.source_notes,
        )

    def test_executor_no_fallback_uses_only_primary(self) -> None:
        item = ContentItem(
            platform="youtube",
            input_type="single",
            url="https://youtu.be/example",
            source_type="video",
        )
        calls: list[str] = []

        class FailingProvider:
            def extract(self, request: BackendExtractRequest) -> TranscriptResult:
                backend = request.primary_backend
                calls.append(backend)
                return TranscriptResult(
                    text="",
                    status="failed",
                    source_level="partial_extraction",
                    backend=backend,
                    error_code="failed",
                )

        executor = BackendExecutor(
            providers={
                "primary.backend": FailingProvider(),
                "fallback.backend": FailingProvider(),
            }
        )

        result = executor.extract(
            item,
            ("primary.backend", "fallback.backend"),
            allow_fallback=False,
        )

        self.assertFalse(result.has_usable_text)
        self.assertEqual(result.backend, "primary.backend")
        self.assertEqual(calls, ["primary.backend"])
        self.assertIn("backend_chain: primary.backend", result.source_notes)

    def test_safe_registry_returns_skeleton_without_calling_default_provider(self) -> None:
        item = ContentItem(
            platform="youtube",
            input_type="single",
            url="https://youtu.be/example",
            source_type="video",
        )

        class RaisingProvider:
            def extract(self, request: BackendExtractRequest) -> TranscriptResult:
                raise AssertionError("default provider should not be used in safe skeleton mode")

        executor = BackendExecutor(
            provider_registry=create_safe_backend_provider_registry(),
            default_provider=RaisingProvider(),
        )

        result = executor.extract(item, ("youtube-transcript-api",), allow_fallback=False)

        self.assertFalse(result.has_usable_text)
        self.assertEqual(result.backend, "youtube-transcript-api")
        self.assertEqual(result.error_code, "backend_not_implemented")
        self.assertIn(
            "no external command/network/account/asr/ocr action was attempted",
            result.source_notes,
        )


class YouTubeTranscriptProviderTests(unittest.TestCase):
    def test_extracts_youtube_video_ids_from_common_urls(self) -> None:
        cases = {
            "https://www.youtube.com/watch?v=abc123": "abc123",
            "https://youtu.be/abc123?si=share": "abc123",
            "https://www.youtube.com/embed/abc123": "abc123",
            "https://www.youtube.com/shorts/abc123": "abc123",
            "abc123": "abc123",
        }

        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(extract_youtube_video_id(value), expected)

    def test_provider_formats_transcript_from_injected_api_without_network(self) -> None:
        class FakeFetchedTranscript(list):
            language = "English"
            language_code = "en"
            is_generated = False

        class FakeApi:
            def __init__(self) -> None:
                self.calls: list[tuple[str, list[str]]] = []

            def fetch(self, video_id: str, *, languages: list[str]) -> FakeFetchedTranscript:
                self.calls.append((video_id, languages))
                return FakeFetchedTranscript(
                    [
                        {"text": "hello", "start": 0.0, "duration": 1.0},
                        {"text": "world", "start": 61.2, "duration": 2.0},
                    ]
                )

        fake_api = FakeApi()
        provider = YouTubeTranscriptApiProvider(
            languages=("en",),
            api_factory=lambda: fake_api,
        )
        item = ContentItem(
            platform="youtube",
            input_type="single",
            url="https://www.youtube.com/watch?v=abc123",
            source_type="video",
        )

        result = provider.extract(
            BackendExtractRequest(
                item=item,
                backend_chain=("youtube-transcript-api",),
                allow_fallback=False,
            )
        )

        self.assertTrue(result.has_usable_text)
        self.assertEqual(result.backend, "youtube-transcript-api")
        self.assertEqual(result.source_level, "native_caption")
        self.assertIn("[00:00:00] hello", result.text)
        self.assertIn("[00:01:01] world", result.text)
        self.assertEqual(fake_api.calls, [("abc123", ["en"])])
        self.assertEqual(result.metadata["language_code"], "en")

    def test_provider_normalizes_blocked_request_failure(self) -> None:
        class RequestBlocked(Exception):
            pass

        class FakeApi:
            def fetch(self, video_id: str, *, languages: list[str]) -> object:
                raise RequestBlocked("request blocked without secrets")

        provider = YouTubeTranscriptApiProvider(api_factory=FakeApi)
        item = ContentItem(
            platform="youtube",
            input_type="single",
            url="https://youtu.be/abc123",
            source_type="video",
        )

        result = provider.extract(
            BackendExtractRequest(
                item=item,
                backend_chain=("youtube-transcript-api",),
                allow_fallback=False,
            )
        )

        self.assertFalse(result.has_usable_text)
        self.assertEqual(result.error_code, "youtube_request_blocked")
        self.assertEqual(result.metadata["video_id"], "abc123")


class DiscoveryTests(unittest.TestCase):
    def test_youtube_collection_candidates_are_recognized_without_network(self) -> None:
        cases = {
            "@openmd": "https://www.youtube.com/@openmd",
            "https://www.youtube.com/@openmd/videos": "https://www.youtube.com/@openmd/videos",
            "youtube.com/channel/UC123/videos": "https://www.youtube.com/channel/UC123/videos",
            "https://www.youtube.com/playlist?list=PL123&si=share": "https://www.youtube.com/playlist?list=PL123",
            "https://www.youtube.com/watch?v=abc123&list=PL123": "https://www.youtube.com/playlist?list=PL123",
            "https://youtu.be/abc123?list=PL123": "https://www.youtube.com/playlist?list=PL123",
        }

        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertTrue(is_youtube_collection_candidate(value))
                self.assertEqual(normalize_youtube_collection_url(value), expected)

        self.assertFalse(is_youtube_collection_candidate("https://www.youtube.com/watch?v=abc123"))

    def test_discovery_skeleton_returns_structured_failure(self) -> None:
        executor = DiscoveryExecutor(
            provider_registry=create_safe_discovery_provider_registry(),
        )

        result = executor.discover(
            DiscoveryRequest(
                input_url="https://www.youtube.com/@openmd/videos",
                platform="youtube",
                limit=20,
                allow_all=False,
            )
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.provider, "skeleton.discovery")
        self.assertEqual(result.error_code, "discovery_not_implemented")
        self.assertEqual(result.items, [])
        self.assertIn(
            "no external command/network/browser/account/session/yt-dlp/asr/ocr action was attempted",
            result.source_notes,
        )

    def test_mock_discovery_provider_returns_multiple_video_items(self) -> None:
        provider = MockDiscoveryProvider(default_count=3)

        result = provider.discover(
            DiscoveryRequest(
                input_url="https://www.youtube.com/@openmd/videos",
                platform="youtube",
                limit=2,
                allow_all=False,
            )
        )

        self.assertTrue(result.ok)
        self.assertEqual(len(result.items), 2)
        self.assertEqual([item.source_type for item in result.items], ["video", "video"])
        self.assertEqual([item.platform for item in result.items], ["youtube", "youtube"])
        self.assertEqual(result.items[0].raw_meta["discovery_limit"], 2)

    def test_youtube_data_api_channel_lookup_supports_handles_and_channel_ids(self) -> None:
        self.assertEqual(youtube_data_api_channel_lookup("@ExampleChannel"), ("forHandle", "ExampleChannel"))
        self.assertEqual(
            youtube_data_api_channel_lookup("https://www.youtube.com/@ExampleChannel/videos"),
            ("forHandle", "ExampleChannel"),
        )
        self.assertEqual(
            youtube_data_api_channel_lookup("https://www.youtube.com/channel/UC123/videos"),
            ("id", "UC123"),
        )
        self.assertIsNone(
            youtube_data_api_channel_lookup("https://www.youtube.com/playlist?list=PL123")
        )

    def test_youtube_data_api_discovery_uses_injected_fetcher_successfully(self) -> None:
        calls: list[tuple[str, dict[str, str], int]] = []
        provider = YouTubeDataApiChannelProvider(
            api_key="fake-key",
            fetcher=_sample_youtube_data_api_fetcher(calls),
            timeout=9,
        )

        result = provider.discover(
            DiscoveryRequest(
                input_url="https://www.youtube.com/@ExampleChannel/videos",
                platform="youtube",
                limit=2,
                allow_all=False,
            )
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.provider, YOUTUBE_DATA_API_DISCOVERY_BACKEND)
        self.assertEqual([item.id for item in result.items], ["vid1", "vid2"])
        self.assertEqual(result.items[0].url, "https://www.youtube.com/watch?v=vid1")
        self.assertEqual(result.items[0].title, "First enriched title")
        self.assertEqual(result.items[0].published_at, "2026-01-01T00:00:00Z")
        self.assertEqual(result.items[0].raw_meta["channel_id"], "UC123")
        self.assertEqual(result.items[0].raw_meta["channel_title"], "Example Channel")
        self.assertEqual(result.items[0].raw_meta["duration"], "PT1M2S")
        self.assertEqual(result.items[0].raw_meta["view_count"], "100")
        self.assertEqual(result.items[0].raw_meta["privacy_status"], "public")
        self.assertEqual(calls[0][0], "channels")
        self.assertEqual(calls[0][1]["forHandle"], "ExampleChannel")
        self.assertEqual(calls[1][0], "playlistItems")
        self.assertEqual(calls[1][1]["playlistId"], "UU123")
        self.assertEqual(calls[1][1]["maxResults"], "2")
        self.assertEqual(calls[2][0], "videos")
        self.assertEqual(calls[2][1]["id"], "vid1,vid2")
        self.assertTrue(all(call[1]["key"] == "fake-key" for call in calls))
        self.assertTrue(all(call[2] == 9 for call in calls))

    def test_youtube_data_api_discovery_requires_api_key_without_fetcher_call(self) -> None:
        calls: list[tuple[str, dict[str, str], int]] = []
        provider = YouTubeDataApiChannelProvider(
            api_key="",
            fetcher=_sample_youtube_data_api_fetcher(calls),
        )

        result = provider.discover(
            DiscoveryRequest(
                input_url="https://www.youtube.com/@ExampleChannel/videos",
                platform="youtube",
                limit=2,
                allow_all=False,
            )
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "youtube_data_api_key_missing")
        self.assertIn(YOUTUBE_DATA_API_KEY_ENV, result.error_summary or "")
        self.assertEqual(calls, [])

    def test_youtube_data_api_discovery_invalid_json_returns_failure(self) -> None:
        def fake_fetcher(
            endpoint: str,
            params: dict[str, str],
            *,
            timeout: int = 30,
        ) -> tuple[int, str]:
            return 200, "not-json"

        provider = YouTubeDataApiChannelProvider(api_key="fake-key", fetcher=fake_fetcher)

        result = provider.discover(
            DiscoveryRequest(
                input_url="https://www.youtube.com/@ExampleChannel/videos",
                platform="youtube",
                limit=2,
                allow_all=False,
            )
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "youtube_data_api_invalid_json")

    def test_youtube_data_api_discovery_http_failure_is_sanitized(self) -> None:
        def fake_fetcher(
            endpoint: str,
            params: dict[str, str],
            *,
            timeout: int = 30,
        ) -> tuple[int, str]:
            return 403, json.dumps({"error": {"message": "quota failed token=secret cookie=secret key=fake-key"}})

        provider = YouTubeDataApiChannelProvider(api_key="fake-key", fetcher=fake_fetcher)

        result = provider.discover(
            DiscoveryRequest(
                input_url="https://www.youtube.com/@ExampleChannel/videos",
                platform="youtube",
                limit=2,
                allow_all=False,
            )
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "youtube_data_api_channel_request_failed")
        self.assertNotIn("token", result.error_summary or "")
        self.assertNotIn("cookie", result.error_summary or "")
        self.assertNotIn("secret", result.error_summary or "")
        self.assertNotIn("fake-key", result.error_summary or "")

    def test_youtube_data_api_playlist_id_normalizes_supported_inputs(self) -> None:
        playlist_url = "https://www.youtube.com/playlist?list=PLplaylist123&si=share"
        watch_url = "https://www.youtube.com/watch?v=video123&list=PLplaylist123"

        self.assertEqual(youtube_data_api_playlist_id(playlist_url), "PLplaylist123")
        self.assertEqual(youtube_data_api_playlist_id(watch_url), "PLplaylist123")
        self.assertEqual(youtube_data_api_playlist_id("PLplaylist123"), "PLplaylist123")
        self.assertEqual(youtube_data_api_playlist_id("UUuploads123"), "UUuploads123")
        self.assertEqual(youtube_data_api_playlist_id("OLAK5uy_fixture123"), "OLAK5uy_fixture123")

    def test_youtube_data_api_playlist_id_rejects_plain_row_ids_and_channels(self) -> None:
        self.assertIsNone(youtube_data_api_playlist_id("1"))
        self.assertIsNone(youtube_data_api_playlist_id("internal-row-id"))
        self.assertIsNone(youtube_data_api_playlist_id("https://www.youtube.com/watch?v=abc123"))
        self.assertIsNone(youtube_data_api_playlist_id("https://www.youtube.com/@ExampleChannel/videos"))
        self.assertIsNone(youtube_data_api_playlist_id("https://example.com/?list=PLplaylist123"))

    def test_youtube_data_api_playlist_discovery_uses_injected_fetcher_with_limit(self) -> None:
        calls: list[tuple[str, dict[str, str], int]] = []
        provider = YouTubeDataApiPlaylistProvider(
            api_key="fake-key",
            fetcher=_sample_youtube_playlist_data_api_fetcher(calls),
            timeout=7,
        )

        result = provider.discover(
            DiscoveryRequest(
                input_url="https://www.youtube.com/watch?v=ignored&list=PLplaylist123",
                platform="youtube",
                limit=2,
                allow_all=False,
            )
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.provider, YOUTUBE_DATA_API_PLAYLIST_DISCOVERY_BACKEND)
        self.assertEqual([item.id for item in result.items], ["plvid1", "plvid2"])
        self.assertEqual(result.items[0].url, "https://www.youtube.com/watch?v=plvid1")
        self.assertEqual(result.items[0].title, "Video first enriched title")
        self.assertEqual(result.items[0].published_at, "2025-12-31T00:00:00Z")
        self.assertEqual(result.items[0].author, "Video Owner")
        self.assertEqual(result.items[0].raw_meta["playlist_id"], "PLplaylist123")
        self.assertEqual(result.items[0].raw_meta["channel_title"], "Video Owner")
        self.assertEqual(result.items[0].raw_meta["playlist_channel_title"], "Playlist Owner")
        self.assertEqual(result.items[0].raw_meta["duration"], "PT10S")
        self.assertEqual(result.items[0].raw_meta["view_count"], "10")
        self.assertEqual(result.items[0].raw_meta["privacy_status"], "public")
        self.assertEqual(calls[0][0], "playlistItems")
        self.assertEqual(calls[0][1]["playlistId"], "PLplaylist123")
        self.assertEqual(calls[0][1]["maxResults"], "2")
        self.assertEqual(calls[1][0], "playlistItems")
        self.assertEqual(calls[1][1]["pageToken"], "NEXT")
        self.assertEqual(calls[1][1]["maxResults"], "1")
        self.assertEqual(calls[2][0], "videos")
        self.assertEqual(calls[2][1]["id"], "plvid1,plvid2")
        self.assertTrue(all(call[1]["key"] == "fake-key" for call in calls))
        self.assertTrue(all(call[2] == 7 for call in calls))

    def test_youtube_data_api_playlist_discovery_requires_api_key_without_fetcher_call(self) -> None:
        calls: list[tuple[str, dict[str, str], int]] = []
        provider = YouTubeDataApiPlaylistProvider(
            api_key="",
            fetcher=_sample_youtube_playlist_data_api_fetcher(calls),
        )

        result = provider.discover(
            DiscoveryRequest(
                input_url="PLplaylist123",
                platform="youtube",
                limit=2,
                allow_all=False,
            )
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "youtube_data_api_key_missing")
        self.assertIn(YOUTUBE_DATA_API_KEY_ENV, result.error_summary or "")
        self.assertEqual(calls, [])

    def test_youtube_data_api_playlist_discovery_failure_codes_are_stable(self) -> None:
        def fake_fetcher(
            endpoint: str,
            params: dict[str, str],
            *,
            timeout: int = 30,
        ) -> tuple[int, str]:
            return 404, json.dumps(
                {
                    "error": {
                        "message": "The playlist identified with the request's playlistId parameter cannot be found.",
                        "errors": [{"reason": "playlistNotFound"}],
                    }
                }
            )

        provider = YouTubeDataApiPlaylistProvider(api_key="fake-key", fetcher=fake_fetcher)

        result = provider.discover(
            DiscoveryRequest(
                input_url="PLplaylist123",
                platform="youtube",
                limit=2,
                allow_all=False,
            )
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "youtube_data_api_playlist_not_found")

    def test_youtube_data_api_playlist_invalid_json_returns_failure(self) -> None:
        def fake_fetcher(
            endpoint: str,
            params: dict[str, str],
            *,
            timeout: int = 30,
        ) -> tuple[int, str]:
            return 200, "not-json"

        provider = YouTubeDataApiPlaylistProvider(api_key="fake-key", fetcher=fake_fetcher)

        result = provider.discover(
            DiscoveryRequest(
                input_url="PLplaylist123",
                platform="youtube",
                limit=2,
                allow_all=False,
            )
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "youtube_data_api_invalid_json")

    def test_youtube_data_api_playlist_empty_returns_failure(self) -> None:
        def fake_fetcher(
            endpoint: str,
            params: dict[str, str],
            *,
            timeout: int = 30,
        ) -> tuple[int, str]:
            return 200, json.dumps({"items": []})

        provider = YouTubeDataApiPlaylistProvider(api_key="fake-key", fetcher=fake_fetcher)

        result = provider.discover(
            DiscoveryRequest(
                input_url="PLplaylist123",
                platform="youtube",
                limit=2,
                allow_all=False,
            )
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "youtube_data_api_playlist_empty")

    def test_opencli_channel_args_are_bounded_and_json_formatted(self) -> None:
        self.assertEqual(
            build_opencli_youtube_channel_args("https://www.youtube.com/@ExampleChannel/videos", limit=7),
            ["opencli", "youtube", "channel", "@ExampleChannel", "--limit", "7", "-f", "json"],
        )
        self.assertEqual(
            build_opencli_youtube_channel_args("@ExampleChannel"),
            ["opencli", "youtube", "channel", "@ExampleChannel", "--limit", "20", "-f", "json"],
        )
        self.assertIsNone(
            build_opencli_youtube_channel_args("https://www.youtube.com/playlist?list=PL123")
        )

    def test_opencli_playlist_target_and_args_are_bounded(self) -> None:
        playlist_url = "https://www.youtube.com/watch?v=abc123&list=PL123&si=share"

        self.assertEqual(normalize_youtube_collection_url(playlist_url), "https://www.youtube.com/playlist?list=PL123")
        self.assertEqual(opencli_youtube_playlist_target(playlist_url), "PL123")
        self.assertEqual(opencli_youtube_playlist_target("PL123"), "PL123")
        self.assertIsNone(opencli_youtube_playlist_target("https://www.youtube.com/@ExampleChannel/videos"))
        self.assertEqual(
            build_opencli_youtube_playlist_args(playlist_url, limit=5),
            ["opencli", "youtube", "playlist", "PL123", "--limit", "5", "-f", "json"],
        )
        self.assertEqual(
            build_opencli_youtube_playlist_args("PL123"),
            ["opencli", "youtube", "playlist", "PL123", "--limit", "20", "-f", "json"],
        )

    def test_opencli_channel_json_parser_skips_metadata_and_builds_items(self) -> None:
        items = parse_opencli_youtube_channel_json(
            _sample_opencli_channel_json(),
            collection_url="https://www.youtube.com/@ExampleChannel/videos",
        )

        self.assertEqual(len(items), 2)
        self.assertEqual(items[0].platform, "youtube")
        self.assertEqual(items[0].input_type, "collection")
        self.assertEqual(items[0].source_type, "video")
        self.assertEqual(items[0].url, "https://www.youtube.com/watch?v=demoVideo01")
        self.assertEqual(items[0].id, "demoVideo01")
        self.assertEqual(items[0].title, "Synthetic channel video one")
        self.assertEqual(items[0].author, "ExampleChannel")
        self.assertEqual(items[0].raw_meta["channel_id"], "UC_synthetic_fixture_channel")
        self.assertEqual(items[0].raw_meta["channel_title"], "ExampleChannel")
        self.assertEqual(items[0].raw_meta["collection_url"], "https://www.youtube.com/@ExampleChannel/videos")
        self.assertEqual(items[0].raw_meta["discovery_provider"], YOUTUBE_DISCOVERY_BACKEND)
        self.assertEqual(items[0].raw_meta["duration"], "2:39")
        self.assertEqual(items[0].raw_meta["views"], "5.3K views")
        self.assertEqual(items[0].raw_meta["published_hint"], "11 hours ago")

    def test_opencli_playlist_json_parser_builds_items(self) -> None:
        payload = json.dumps(
            [
                {
                    "rank": 1,
                    "title": "First playlist video",
                    "url": "https://www.youtube.com/watch?v=vid123&list=PL123",
                    "duration": "1:23",
                    "views": "10 views",
                    "author": "Fixture Channel",
                },
                {
                    "rank": 2,
                    "title": "Second playlist video",
                    "video_id": "vid456",
                    "published_hint": "2 days ago",
                },
            ]
        )

        items = parse_opencli_youtube_playlist_output(
            payload,
            collection_url="https://www.youtube.com/playlist?list=PL123",
        )

        self.assertEqual([item.id for item in items], ["vid123", "vid456"])
        self.assertEqual(items[0].url, "https://www.youtube.com/watch?v=vid123")
        self.assertEqual(items[0].title, "First playlist video")
        self.assertEqual(items[0].author, "Fixture Channel")
        self.assertEqual(items[0].raw_meta["playlist_id"], "PL123")
        self.assertEqual(items[0].raw_meta["duration"], "1:23")
        self.assertEqual(items[0].raw_meta["views"], "10 views")
        self.assertEqual(items[0].raw_meta["discovery_provider"], YOUTUBE_PLAYLIST_DISCOVERY_BACKEND)
        self.assertEqual(items[1].url, "https://www.youtube.com/watch?v=vid456")
        self.assertEqual(items[1].raw_meta["published_hint"], "2 days ago")

    def test_opencli_playlist_json_parser_ignores_plain_id_rows(self) -> None:
        payload = json.dumps(
            [
                {"id": 1, "title": "row only no video"},
                {
                    "id": "internal-row-id",
                    "title": "URL wins over plain id",
                    "url": "https://www.youtube.com/watch?v=realVideo123",
                },
            ]
        )

        items = parse_opencli_youtube_playlist_output(
            payload,
            collection_url="https://www.youtube.com/playlist?list=PL123",
        )

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].id, "realVideo123")
        self.assertEqual(items[0].url, "https://www.youtube.com/watch?v=realVideo123")
        self.assertNotEqual(items[0].url, "https://www.youtube.com/watch?v=1")

        no_video_items = parse_opencli_youtube_playlist_output(
            json.dumps([{"id": 1, "title": "row only no video"}]),
            collection_url="https://www.youtube.com/playlist?list=PL123",
        )
        self.assertEqual(no_video_items, [])

    def test_opencli_playlist_json_parser_prefers_url_over_conflicting_video_id(self) -> None:
        items = parse_opencli_youtube_playlist_output(
            json.dumps(
                [
                    {
                        "title": "URL should win",
                        "video_id": "wrongVideoId",
                        "url": "https://www.youtube.com/watch?v=rightVideoId",
                    }
                ]
            ),
            collection_url="https://www.youtube.com/playlist?list=PL123",
        )

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].id, "rightVideoId")
        self.assertEqual(items[0].url, "https://www.youtube.com/watch?v=rightVideoId")

    def test_opencli_playlist_parser_accepts_csv_and_markdown_fixtures(self) -> None:
        csv_items = parse_opencli_youtube_playlist_output(
            "rank,title,url\n1,CSV video,https://youtu.be/csv123\n",
            collection_url="https://www.youtube.com/playlist?list=PL123",
        )
        markdown_items = parse_opencli_youtube_playlist_output(
            "| rank | title | url |\n| --- | --- | --- |\n| 1 | Markdown video | https://www.youtube.com/watch?v=md123 |\n",
            collection_url="https://www.youtube.com/playlist?list=PL123",
        )

        self.assertEqual(csv_items[0].id, "csv123")
        self.assertEqual(csv_items[0].title, "CSV video")
        self.assertEqual(markdown_items[0].id, "md123")
        self.assertEqual(markdown_items[0].title, "Markdown video")

    def test_opencli_playlist_provider_is_bounded_and_does_not_extract(self) -> None:
        calls: list[tuple[list[str], int]] = []

        def fake_runner(args: list[str], *, timeout: int = 120) -> tuple[int, str, str]:
            calls.append((args, timeout))
            return (
                0,
                json.dumps(
                    [
                        {"title": "First", "url": "https://www.youtube.com/watch?v=vid111"},
                        {"title": "Second", "url": "https://www.youtube.com/watch?v=vid222"},
                    ]
                ),
                "",
            )

        provider = OpenCliYouTubePlaylistProvider(runner=fake_runner, timeout=6)
        result = provider.discover(
            DiscoveryRequest(
                input_url="https://www.youtube.com/watch?v=ignored&list=PL123",
                platform="youtube",
                limit=1,
                allow_all=False,
            )
        )

        self.assertTrue(result.ok, result.error_summary)
        self.assertEqual(result.provider, YOUTUBE_PLAYLIST_DISCOVERY_BACKEND)
        self.assertEqual(calls, [(["opencli", "youtube", "playlist", "PL123", "--limit", "1", "-f", "json"], 6)])
        self.assertEqual(len(result.items), 1)
        self.assertEqual(result.items[0].id, "vid111")
        self.assertEqual(result.items[0].source_type, "video")
        self.assertIn("did not extract transcripts", " ".join(result.source_notes))

    def test_opencli_playlist_provider_classifies_failures(self) -> None:
        def fake_runner(args: list[str], *, timeout: int = 120) -> tuple[int, str, str]:
            return 1, "", "OpenCLI command timed out after 30 seconds."

        provider = OpenCliYouTubePlaylistProvider(runner=fake_runner)
        result = provider.discover(
            DiscoveryRequest(
                input_url="https://www.youtube.com/playlist?list=PL123",
                platform="youtube",
                limit=2,
            )
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "opencli_youtube_playlist_timeout")
        self.assertEqual(
            classify_youtube_playlist_discovery_failure("EMPTY_RESULT: youtube playlist returned no data"),
            "opencli_youtube_playlist_empty",
        )
        self.assertEqual(
            classify_youtube_playlist_discovery_failure("Sign in to confirm you're not a bot"),
            "youtube_bot_check",
        )
        self.assertEqual(
            classify_youtube_playlist_discovery_failure("login required; please log in"),
            "youtube_cookie_or_auth_required",
        )

    def test_opencli_playlist_provider_rejects_invalid_json_shape(self) -> None:
        def fake_runner(args: list[str], *, timeout: int = 120) -> tuple[int, str, str]:
            return 0, json.dumps({"playlistTitle": "No item rows"}), ""

        provider = OpenCliYouTubePlaylistProvider(runner=fake_runner)
        result = provider.discover(
            DiscoveryRequest(
                input_url="https://www.youtube.com/playlist?list=PL123",
                platform="youtube",
                limit=2,
            )
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "opencli_youtube_playlist_invalid_output")

    def test_opencli_youtube_discovery_uses_injected_runner_successfully(self) -> None:
        calls: list[tuple[list[str], int]] = []

        def fake_runner(args: list[str], *, timeout: int = 120) -> tuple[int, str, str]:
            calls.append((args, timeout))
            return 0, _sample_opencli_channel_json(), ""

        provider = OpenCliYouTubeChannelProvider(runner=fake_runner, timeout=5)

        result = provider.discover(
            DiscoveryRequest(
                input_url="https://www.youtube.com/@ExampleChannel/videos",
                platform="youtube",
                limit=7,
                allow_all=False,
            )
        )

        self.assertTrue(result.ok)
        self.assertEqual(len(result.items), 2)
        self.assertEqual(calls, [(["opencli", "youtube", "channel", "@ExampleChannel", "--limit", "7", "-f", "json"], 5)])
        self.assertIn("OpenCLI browser-context discovery", result.source_notes[0])
        self.assertTrue(result.metadata["requires_browser_context"])

    def test_opencli_youtube_discovery_malformed_json_returns_failure(self) -> None:
        calls: list[list[str]] = []

        def fake_runner(args: list[str], *, timeout: int = 120) -> tuple[int, str, str]:
            calls.append(args)
            return 0, "not-json", ""

        provider = OpenCliYouTubeChannelProvider(runner=fake_runner)

        result = provider.discover(
            DiscoveryRequest(
                input_url="https://www.youtube.com/@ExampleChannel/videos",
                platform="youtube",
                limit=3,
                allow_all=False,
            )
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "opencli_youtube_channel_invalid_json")
        self.assertEqual(len(calls), 1)

    def test_opencli_youtube_discovery_nonzero_exit_returns_sanitized_failure(self) -> None:
        def fake_runner(args: list[str], *, timeout: int = 120) -> tuple[int, str, str]:
            return 1, "", "cookie=secret token=secret Sign in to confirm"

        provider = OpenCliYouTubeChannelProvider(runner=fake_runner)

        result = provider.discover(
            DiscoveryRequest(
                input_url="https://www.youtube.com/@ExampleChannel/videos",
                platform="youtube",
                limit=3,
                allow_all=False,
            )
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "opencli_youtube_channel_failed")
        self.assertEqual(result.metadata["exit_code"], 1)
        self.assertNotIn("cookie", result.error_summary or "")
        self.assertNotIn("token", result.error_summary or "")
        self.assertNotIn("secret", result.error_summary or "")

    def test_opencli_youtube_discovery_rejects_unsupported_input_without_runner(self) -> None:
        calls: list[list[str]] = []

        def fake_runner(args: list[str], *, timeout: int = 120) -> tuple[int, str, str]:
            calls.append(args)
            return 0, _sample_opencli_channel_json(), ""

        provider = OpenCliYouTubeChannelProvider(runner=fake_runner)

        result = provider.discover(
            DiscoveryRequest(
                input_url="https://example.com/article",
                platform="web_article",
                limit=20,
                allow_all=False,
            )
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "youtube_collection_input_not_supported")
        self.assertEqual(calls, [])


class RegistryAndBackendTests(unittest.TestCase):
    def test_registry_selects_primary_and_mock_adapter_can_succeed(self) -> None:
        registry = create_default_registry()
        adapter = registry.adapter_for("https://youtu.be/example")
        selection = registry.select_backend("youtube")
        self.assertEqual(adapter.id, "youtube")
        self.assertEqual(selection.primary, "youtube-transcript-api")
        self.assertIn("opencli.youtube.transcript", selection.fallbacks)

        item = adapter.resolve_items("https://youtu.be/example")[0]
        result = adapter.extract_text(item, backend="mock")
        self.assertTrue(result.has_usable_text)

    def test_native_source_first_default_backend_chains(self) -> None:
        registry = create_default_registry()

        self.assertEqual(
            registry.backend_chain("bilibili"),
            (
                "bilibili-harvest.core",
                "opencli.bilibili.subtitle",
                "yt-dlp.bilibili",
                "asr.fallback",
            ),
        )
        self.assertEqual(
            registry.backend_chain("youtube"),
            (
                "youtube-transcript-api",
                "opencli.youtube.transcript",
                "yt-dlp.youtube",
                "asr.fallback",
            ),
        )
        self.assertEqual(
            registry.backend_chain("feishu"),
            (
                "lark-cli.docs.fetch",
                "lark-cli.drive.export-markdown",
                "lark-cli.markdown.fetch",
                "manual.markdown-export",
            ),
        )

    def test_requested_backend_override_uses_only_requested_backend(self) -> None:
        registry = create_default_registry()
        selection = registry.select_backend("youtube", requested="mock")

        self.assertEqual(selection.primary, "mock")
        self.assertEqual(selection.fallbacks, ())
        self.assertEqual(selection.chain, ("mock",))

    def test_no_fallback_uses_only_default_primary(self) -> None:
        registry = create_default_registry()
        selection = registry.select_backend("bilibili", allow_fallback=False)

        self.assertEqual(selection.primary, "bilibili-harvest.core")
        self.assertEqual(selection.fallbacks, ())
        self.assertEqual(selection.chain, ("bilibili-harvest.core",))

    def test_opencli_is_not_bilibili_or_youtube_default_primary(self) -> None:
        registry = create_default_registry()

        self.assertFalse(registry.select_backend("bilibili").primary.startswith("opencli."))
        self.assertFalse(registry.select_backend("youtube").primary.startswith("opencli."))

    def test_default_registry_can_load_platform_adapter_factory_without_core_edit(self) -> None:
        calls: list[str] = []

        class MarkerAdapter(StubPlatformAdapter):
            pass

        def fake_loader(platform_id: str) -> object:
            calls.append(platform_id)
            if platform_id != "feishu":
                return None

            def factory(factory_platform_id: str, default_backend: str) -> MarkerAdapter:
                return MarkerAdapter(factory_platform_id, default_backend)

            return factory

        registry = create_default_registry(adapter_factory_loader=fake_loader)

        self.assertIsInstance(registry.get_adapter("feishu"), MarkerAdapter)
        self.assertEqual(calls, list(PLATFORM_IDS))

    def test_runtime_backend_registry_loads_platform_extension_providers(self) -> None:
        item = ContentItem(
            platform="weixin",
            input_type="single",
            url="https://mp.weixin.qq.com/s/example",
            source_type="article",
        )

        class LaneBackendProvider:
            name = "lane.backend"

            def extract(self, request: BackendExtractRequest) -> TranscriptResult:
                return TranscriptResult(
                    text="lane text",
                    status="verified",
                    source_level="native_text",
                    backend=request.primary_backend,
                )

        def register_backend_providers(registry: object, options: BackendRuntimeOptions) -> None:
            registry.register("lane.backend", LaneBackendProvider())

        module = SimpleNamespace(register_backend_providers=register_backend_providers)
        provider_registry = create_runtime_backend_provider_registry(
            "weixin",
            BackendRuntimeOptions(),
            extension_module_loader=lambda platform: (module,),
        )
        executor = BackendExecutor(provider_registry=provider_registry)

        result = executor.extract(item, ("lane.backend",), allow_fallback=False)

        self.assertTrue(result.has_usable_text)
        self.assertEqual(result.backend, "lane.backend")

    def test_runtime_discovery_registry_loads_platform_extension_providers(self) -> None:
        class LaneDiscoveryProvider:
            name = "lane.discovery"

            def discover(self, request: DiscoveryRequest) -> DiscoveryResult:
                return DiscoveryResult.success(
                    provider=self.name,
                    items=[
                        ContentItem(
                            platform=request.platform,
                            input_type="collection",
                            url="https://mp.weixin.qq.com/s/example",
                            source_type="article",
                        )
                    ],
                )

        def register_discovery_providers(registry: object, options: BackendRuntimeOptions) -> None:
            registry.register("lane.discovery", LaneDiscoveryProvider(), platforms=("weixin",))

        module = SimpleNamespace(register_discovery_providers=register_discovery_providers)
        provider_registry = create_runtime_discovery_provider_registry(
            "weixin",
            BackendRuntimeOptions(),
            extension_module_loader=lambda platform: (module,),
        )
        executor = DiscoveryExecutor(provider_registry=provider_registry)

        result = executor.discover(
            DiscoveryRequest(
                input_url="https://mp.weixin.qq.com/s/example",
                platform="weixin",
                limit=2,
                allow_all=False,
            )
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.provider, "lane.discovery")
        self.assertEqual(len(result.items), 1)

    def test_runtime_discovery_registry_registers_youtube_playlist_data_api_only_when_enabled(self) -> None:
        request = DiscoveryRequest(
            input_url="PLplaylist123",
            platform="youtube",
            limit=1,
            allow_all=False,
        )
        safe_registry = create_runtime_discovery_provider_registry(
            "youtube",
            BackendRuntimeOptions(requested_discovery_provider=YOUTUBE_DATA_API_PLAYLIST_DISCOVERY_BACKEND),
        )
        safe_provider = safe_registry.provider_for(
            request,
            requested_provider=YOUTUBE_DATA_API_PLAYLIST_DISCOVERY_BACKEND,
        )

        self.assertIsNotNone(safe_provider)
        self.assertEqual(getattr(safe_provider, "name"), "skeleton.discovery")

        network_registry = create_runtime_discovery_provider_registry(
            "youtube",
            BackendRuntimeOptions(
                requested_discovery_provider=YOUTUBE_DATA_API_PLAYLIST_DISCOVERY_BACKEND,
                enable_network_backends=True,
            ),
        )
        network_provider = network_registry.provider_for(
            request,
            requested_provider=YOUTUBE_DATA_API_PLAYLIST_DISCOVERY_BACKEND,
        )

        self.assertIsInstance(network_provider, YouTubeDataApiPlaylistProvider)
        self.assertEqual(getattr(network_provider, "name"), YOUTUBE_DATA_API_PLAYLIST_DISCOVERY_BACKEND)

    def test_discovery_backend_aliases_are_normalized_in_backend_registry_module(self) -> None:
        self.assertEqual(normalize_discovery_backend("mock"), "mock.discovery")
        self.assertEqual(normalize_discovery_backend("data-api"), YOUTUBE_DATA_API_DISCOVERY_BACKEND)
        self.assertEqual(
            normalize_discovery_backend("data-api-playlist"),
            YOUTUBE_DATA_API_PLAYLIST_DISCOVERY_BACKEND,
        )
        self.assertEqual(normalize_discovery_backend("opencli"), YOUTUBE_DISCOVERY_BACKEND)
        self.assertIsNone(normalize_discovery_backend(None))

    def test_cli_process_uses_safe_skeleton_and_does_not_call_adapter_extract(self) -> None:
        class RecordingAdapter:
            id = "youtube"

            def __init__(self) -> None:
                self.extract_called = False

            def resolve_items(
                self, input_value: str, *, limit: int | None = None, all: bool = False
            ) -> list[ContentItem]:
                return [
                    ContentItem(
                        platform="youtube",
                        input_type="single",
                        url=input_value,
                        source_type="video",
                    )
                ]

            def extract_text(
                self,
                item: ContentItem,
                *,
                backend: str | None = None,
                backend_chain: tuple[str, ...] | None = None,
                allow_fallback: bool = True,
            ) -> TranscriptResult:
                self.extract_called = True
                raise AssertionError("default CLI path must not call adapter extraction")

        class RecordingRegistry:
            def __init__(self) -> None:
                self.adapter = RecordingAdapter()

            def adapter_for(self, input_value: str) -> RecordingAdapter:
                return self.adapter

            def select_backend(
                self,
                platform: str,
                requested: str | None = None,
                *,
                allow_fallback: bool = True,
            ) -> BackendSelection:
                return BackendSelection(
                    platform=platform,
                    primary="primary.backend",
                    fallbacks=("fallback.backend",),
                )

        registry = RecordingRegistry()
        old_create_default_registry = cli_module.create_default_registry
        try:
            cli_module.create_default_registry = lambda: registry  # type: ignore[assignment]
            with tempfile.TemporaryDirectory() as tmp_dir:
                result = cli_module._process_input(
                    "https://youtu.be/example",
                    SimpleNamespace(
                        all=False,
                        backend=None,
                        fallback=True,
                        limit=None,
                        out=tmp_dir,
                        overwrite=False,
                        runlog=None,
                    ),
                    command="ingest",
                )
        finally:
            cli_module.create_default_registry = old_create_default_registry

        self.assertFalse(result.ok)
        self.assertEqual(result.failed_count, 1)
        self.assertFalse(registry.adapter.extract_called)

    def test_cli_process_requested_mock_uses_local_safe_provider(self) -> None:
        class RecordingAdapter:
            id = "youtube"

            def __init__(self) -> None:
                self.extract_called = False

            def resolve_items(
                self, input_value: str, *, limit: int | None = None, all: bool = False
            ) -> list[ContentItem]:
                return [
                    ContentItem(
                        platform="youtube",
                        input_type="single",
                        url=input_value,
                        source_type="video",
                    )
                ]

            def extract_text(
                self,
                item: ContentItem,
                *,
                backend: str | None = None,
                backend_chain: tuple[str, ...] | None = None,
                allow_fallback: bool = True,
            ) -> TranscriptResult:
                self.extract_called = True
                raise AssertionError("mock provider should not call adapter extraction")

        class RecordingRegistry:
            def __init__(self) -> None:
                self.adapter = RecordingAdapter()

            def adapter_for(self, input_value: str) -> RecordingAdapter:
                return self.adapter

            def select_backend(
                self,
                platform: str,
                requested: str | None = None,
                *,
                allow_fallback: bool = True,
            ) -> BackendSelection:
                self.assert_requested = requested
                return BackendSelection(
                    platform=platform,
                    primary=requested or "primary.backend",
                    fallbacks=(),
                )

        registry = RecordingRegistry()
        old_create_default_registry = cli_module.create_default_registry
        try:
            cli_module.create_default_registry = lambda: registry  # type: ignore[assignment]
            with tempfile.TemporaryDirectory() as tmp_dir:
                result = cli_module._process_input(
                    "https://youtu.be/example",
                    SimpleNamespace(
                        all=False,
                        backend="mock",
                        fallback=True,
                        limit=None,
                        out=tmp_dir,
                        overwrite=False,
                        runlog=None,
                    ),
                    command="ingest",
                )
        finally:
            cli_module.create_default_registry = old_create_default_registry

        self.assertTrue(result.ok)
        self.assertEqual(result.success_count, 1)
        self.assertFalse(registry.adapter.extract_called)

    def test_cli_process_network_provider_requires_explicit_enable_flag(self) -> None:
        class RecordingAdapter:
            id = "youtube"

            def __init__(self) -> None:
                self.extract_called = False

            def resolve_items(
                self, input_value: str, *, limit: int | None = None, all: bool = False
            ) -> list[ContentItem]:
                return [
                    ContentItem(
                        platform="youtube",
                        input_type="single",
                        url=input_value,
                        source_type="video",
                    )
                ]

            def extract_text(
                self,
                item: ContentItem,
                *,
                backend: str | None = None,
                backend_chain: tuple[str, ...] | None = None,
                allow_fallback: bool = True,
            ) -> TranscriptResult:
                self.extract_called = True
                raise AssertionError("network provider should not call adapter extraction")

        class RecordingRegistry:
            def __init__(self) -> None:
                self.adapter = RecordingAdapter()

            def adapter_for(self, input_value: str) -> RecordingAdapter:
                return self.adapter

            def select_backend(
                self,
                platform: str,
                requested: str | None = None,
                *,
                allow_fallback: bool = True,
            ) -> BackendSelection:
                return BackendSelection(
                    platform=platform,
                    primary="youtube-transcript-api",
                    fallbacks=(),
                )

        class FakeNetworkProvider:
            def extract(self, request: BackendExtractRequest) -> TranscriptResult:
                return TranscriptResult(
                    text="[00:00:00] fake transcript\n",
                    status="verified",
                    source_level="native_caption",
                    backend=request.primary_backend,
                    source_notes=["fake network provider used by test"],
                )

        registry = RecordingRegistry()
        old_create_default_registry = cli_module.create_default_registry
        old_provider = backend_registry_module.YouTubeTranscriptApiProvider
        try:
            cli_module.create_default_registry = lambda: registry  # type: ignore[assignment]
            backend_registry_module.YouTubeTranscriptApiProvider = FakeNetworkProvider  # type: ignore[assignment]
            with tempfile.TemporaryDirectory() as tmp_dir:
                result = cli_module._process_input(
                    "https://youtu.be/example",
                    SimpleNamespace(
                        all=False,
                        backend=None,
                        fallback=True,
                        limit=None,
                        out=tmp_dir,
                        overwrite=False,
                        runlog=None,
                        enable_network_backends=True,
                    ),
                    command="ingest",
                )
        finally:
            cli_module.create_default_registry = old_create_default_registry
            backend_registry_module.YouTubeTranscriptApiProvider = old_provider

        self.assertTrue(result.ok)
        self.assertEqual(result.success_count, 1)
        self.assertFalse(registry.adapter.extract_called)

    def test_collect_default_discovery_failure_does_not_enter_extraction(self) -> None:
        old_build_backend_executor = cli_module._build_backend_executor
        old_create_default_registry = cli_module.create_default_registry
        old_provider = backend_registry_module.OpenCliYouTubeChannelProvider
        old_data_provider = backend_registry_module.YouTubeDataApiChannelProvider
        try:
            cli_module._build_backend_executor = (  # type: ignore[assignment]
                lambda *_, **__: (_ for _ in ()).throw(
                    AssertionError("collect discovery failure must not enter extraction")
                )
            )
            cli_module.create_default_registry = (  # type: ignore[assignment]
                lambda: (_ for _ in ()).throw(
                    AssertionError("collect discovery failure must not build adapter registry")
                )
            )
            backend_registry_module.OpenCliYouTubeChannelProvider = (  # type: ignore[assignment]
                lambda *_, **__: (_ for _ in ()).throw(
                    AssertionError("default collect must not register or construct OpenCLI discovery")
                )
            )
            backend_registry_module.YouTubeDataApiChannelProvider = (  # type: ignore[assignment]
                lambda *_, **__: (_ for _ in ()).throw(
                    AssertionError("default collect must not register or construct Data API discovery")
                )
            )
            with tempfile.TemporaryDirectory() as tmp_dir:
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    exit_code = cli_main(
                        [
                            "collect",
                            "https://www.youtube.com/@openmd/videos",
                            "--out",
                            tmp_dir,
                        ]
                    )
        finally:
            cli_module._build_backend_executor = old_build_backend_executor
            cli_module.create_default_registry = old_create_default_registry
            backend_registry_module.OpenCliYouTubeChannelProvider = old_provider
            backend_registry_module.YouTubeDataApiChannelProvider = old_data_provider

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 1)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["failed_count"], 1)
        self.assertIn("Skeleton discovery provider only", payload["message"])

    def test_collect_mock_discovered_items_enter_extract_executor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = cli_main(
                    [
                        "collect",
                        "https://www.youtube.com/@openmd/videos",
                        "--out",
                        tmp_dir,
                        "--backend",
                        "mock",
                        "--limit",
                        "2",
                    ]
                )

            payload = json.loads(stdout.getvalue())
            output_paths = [Path(value) for value in payload["outputs"]]

            self.assertEqual(exit_code, 0)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["item_count"], 2)
            self.assertEqual(payload["success_count"], 2)
            self.assertEqual(len(output_paths), 2)
            for output_path in output_paths:
                self.assertTrue(output_path.exists())
                self.assertIn("Mock transcript for Openmd skeleton", output_path.read_text(encoding="utf-8"))

            manifest_path = default_manifest_path(
                tmp_dir,
                platform="youtube",
                collection_url="https://www.youtube.com/@openmd/videos",
            )
            manifest_rows = [
                json.loads(line)
                for line in manifest_path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(len(manifest_rows), 2)
            self.assertEqual(manifest_rows[0]["video_id"], "openmd-mock-1")
            self.assertEqual(manifest_rows[0]["process_status"], "discovered")

    def test_collect_manifest_only_writes_manifest_without_extraction(self) -> None:
        old_build_backend_executor = cli_module._build_backend_executor
        old_create_default_registry = cli_module.create_default_registry
        try:
            cli_module._build_backend_executor = (  # type: ignore[assignment]
                lambda *_, **__: (_ for _ in ()).throw(
                    AssertionError("manifest-only collect must not build extraction executor")
                )
            )
            cli_module.create_default_registry = (  # type: ignore[assignment]
                lambda: (_ for _ in ()).throw(
                    AssertionError("manifest-only collect must not build adapter registry")
                )
            )
            with tempfile.TemporaryDirectory() as tmp_dir:
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    exit_code = cli_main(
                        [
                            "collect",
                            "https://www.youtube.com/@openmd/videos",
                            "--out",
                            tmp_dir,
                            "--backend",
                            "mock",
                            "--limit",
                            "2",
                            "--manifest-only",
                        ]
                    )

                payload = json.loads(stdout.getvalue())
                output_paths = [Path(value) for value in payload["outputs"]]
                manifest_path = default_manifest_path(
                    tmp_dir,
                    platform="youtube",
                    collection_url="https://www.youtube.com/@openmd/videos",
                )
                manifest_rows = [
                    json.loads(line)
                    for line in manifest_path.read_text(encoding="utf-8").splitlines()
                ]

                self.assertEqual(exit_code, 0)
                self.assertTrue(payload["ok"])
                self.assertEqual(payload["item_count"], 2)
                self.assertEqual(payload["success_count"], 2)
                self.assertEqual(payload["failed_count"], 0)
                self.assertEqual(payload["message"], "manifest written")
                self.assertEqual(output_paths, [manifest_path])
                self.assertEqual([row["video_id"] for row in manifest_rows], ["openmd-mock-1", "openmd-mock-2"])
                self.assertTrue(all(row["discovery_only"] for row in manifest_rows))
                runlog_rows = [
                    json.loads(line)
                    for line in (Path(tmp_dir) / "runlog.jsonl").read_text(encoding="utf-8").splitlines()
                ]
                self.assertEqual(runlog_rows[-1]["event_type"], "discovery")
                self.assertFalse((Path(tmp_dir) / "youtube").exists())
        finally:
            cli_module._build_backend_executor = old_build_backend_executor
            cli_module.create_default_registry = old_create_default_registry

    def test_collect_discovery_backend_mock_does_not_require_backend_mock(self) -> None:
        old_build_backend_executor = cli_module._build_backend_executor
        old_create_default_registry = cli_module.create_default_registry
        try:
            cli_module._build_backend_executor = (  # type: ignore[assignment]
                lambda *_, **__: (_ for _ in ()).throw(
                    AssertionError("manifest-only collect must not build extraction executor")
                )
            )
            cli_module.create_default_registry = (  # type: ignore[assignment]
                lambda: (_ for _ in ()).throw(
                    AssertionError("manifest-only collect must not build adapter registry")
                )
            )
            with tempfile.TemporaryDirectory() as tmp_dir:
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    exit_code = cli_main(
                        [
                            "collect",
                            "https://www.youtube.com/@openmd/videos",
                            "--out",
                            tmp_dir,
                            "--discovery-backend",
                            "mock",
                            "--limit",
                            "2",
                            "--manifest-only",
                        ]
                    )

                payload = json.loads(stdout.getvalue())
                manifest_path = default_manifest_path(
                    tmp_dir,
                    platform="youtube",
                    collection_url="https://www.youtube.com/@openmd/videos",
                )
                manifest_rows = [
                    json.loads(line)
                    for line in manifest_path.read_text(encoding="utf-8").splitlines()
                ]

                self.assertEqual(exit_code, 0)
                self.assertTrue(payload["ok"])
                self.assertEqual(payload["outputs"], [str(manifest_path)])
                self.assertEqual([row["video_id"] for row in manifest_rows], ["openmd-mock-1", "openmd-mock-2"])
        finally:
            cli_module._build_backend_executor = old_build_backend_executor
            cli_module.create_default_registry = old_create_default_registry

    def test_collect_discovery_backend_opencli_requires_browser_enable(self) -> None:
        old_build_backend_executor = cli_module._build_backend_executor
        old_create_default_registry = cli_module.create_default_registry
        old_provider = backend_registry_module.OpenCliYouTubeChannelProvider
        try:
            cli_module._build_backend_executor = (  # type: ignore[assignment]
                lambda *_, **__: (_ for _ in ()).throw(
                    AssertionError("discovery failure must not enter extraction")
                )
            )
            cli_module.create_default_registry = (  # type: ignore[assignment]
                lambda: (_ for _ in ()).throw(
                    AssertionError("discovery failure must not build adapter registry")
                )
            )
            backend_registry_module.OpenCliYouTubeChannelProvider = (  # type: ignore[assignment]
                lambda *_, **__: (_ for _ in ()).throw(
                    AssertionError("OpenCLI discovery must not be constructed without --enable-browser-backends")
                )
            )
            with tempfile.TemporaryDirectory() as tmp_dir:
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    exit_code = cli_main(
                        [
                            "collect",
                            "https://www.youtube.com/@openmd/videos",
                            "--out",
                            tmp_dir,
                            "--discovery-backend",
                            "opencli",
                            "--manifest-only",
                        ]
                    )
        finally:
            cli_module._build_backend_executor = old_build_backend_executor
            cli_module.create_default_registry = old_create_default_registry
            backend_registry_module.OpenCliYouTubeChannelProvider = old_provider

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 1)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["failed_count"], 1)
        self.assertIn("Skeleton discovery provider only", payload["message"])

    def test_collect_discovery_backend_data_api_requires_network_enable(self) -> None:
        old_build_backend_executor = cli_module._build_backend_executor
        old_create_default_registry = cli_module.create_default_registry
        old_provider = backend_registry_module.YouTubeDataApiChannelProvider
        try:
            cli_module._build_backend_executor = (  # type: ignore[assignment]
                lambda *_, **__: (_ for _ in ()).throw(
                    AssertionError("discovery failure must not enter extraction")
                )
            )
            cli_module.create_default_registry = (  # type: ignore[assignment]
                lambda: (_ for _ in ()).throw(
                    AssertionError("discovery failure must not build adapter registry")
                )
            )
            backend_registry_module.YouTubeDataApiChannelProvider = (  # type: ignore[assignment]
                lambda *_, **__: (_ for _ in ()).throw(
                    AssertionError("Data API discovery must not be constructed without --enable-network-backends")
                )
            )
            with tempfile.TemporaryDirectory() as tmp_dir:
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    exit_code = cli_main(
                        [
                            "collect",
                            "https://www.youtube.com/@openmd/videos",
                            "--out",
                            tmp_dir,
                            "--discovery-backend",
                            "data-api",
                            "--manifest-only",
                        ]
                    )
        finally:
            cli_module._build_backend_executor = old_build_backend_executor
            cli_module.create_default_registry = old_create_default_registry
            backend_registry_module.YouTubeDataApiChannelProvider = old_provider

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 1)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["failed_count"], 1)
        self.assertIn("Skeleton discovery provider only", payload["message"])

    def test_collect_network_enabled_uses_fake_data_api_discovery_manifest_only(self) -> None:
        calls: list[tuple[str, dict[str, str], int]] = []
        old_provider = backend_registry_module.YouTubeDataApiChannelProvider
        old_build_backend_executor = cli_module._build_backend_executor
        old_create_default_registry = cli_module.create_default_registry
        try:
            backend_registry_module.YouTubeDataApiChannelProvider = (  # type: ignore[assignment]
                lambda: YouTubeDataApiChannelProvider(
                    api_key="fake-key",
                    fetcher=_sample_youtube_data_api_fetcher(calls),
                )
            )
            cli_module._build_backend_executor = (  # type: ignore[assignment]
                lambda *_, **__: (_ for _ in ()).throw(
                    AssertionError("manifest-only collect must not build extraction executor")
                )
            )
            cli_module.create_default_registry = (  # type: ignore[assignment]
                lambda: (_ for _ in ()).throw(
                    AssertionError("manifest-only collect must not build adapter registry")
                )
            )
            with tempfile.TemporaryDirectory() as tmp_dir:
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    exit_code = cli_main(
                        [
                            "collect",
                            "https://www.youtube.com/@ExampleChannel/videos",
                            "--out",
                            tmp_dir,
                            "--enable-network-backends",
                            "--discovery-backend",
                            "data-api",
                            "--limit",
                            "2",
                            "--manifest-only",
                        ]
                    )

                payload = json.loads(stdout.getvalue())
                manifest_path = default_manifest_path(
                    tmp_dir,
                    platform="youtube",
                    collection_url="https://www.youtube.com/@ExampleChannel/videos",
                )
                manifest_rows = [
                    json.loads(line)
                    for line in manifest_path.read_text(encoding="utf-8").splitlines()
                ]

                self.assertEqual(exit_code, 0)
                self.assertTrue(payload["ok"])
                self.assertEqual(payload["outputs"], [str(manifest_path)])
                self.assertEqual([row["video_id"] for row in manifest_rows], ["vid1", "vid2"])
                self.assertEqual(manifest_rows[0]["channel_id"], "UC123")
                self.assertEqual(manifest_rows[0]["channel_title"], "Example Channel")
                self.assertEqual(manifest_rows[0]["duration"], "PT1M2S")
                self.assertEqual(manifest_rows[0]["view_count"], "100")
                self.assertEqual(manifest_rows[0]["discovery_provider"], YOUTUBE_DATA_API_DISCOVERY_BACKEND)
                self.assertEqual([call[0] for call in calls], ["channels", "playlistItems", "videos"])
        finally:
            backend_registry_module.YouTubeDataApiChannelProvider = old_provider
            cli_module._build_backend_executor = old_build_backend_executor
            cli_module.create_default_registry = old_create_default_registry

    def test_collect_browser_enabled_uses_fake_opencli_discovery_then_mock_extraction(self) -> None:
        calls: list[list[str]] = []

        def fake_runner(args: list[str], *, timeout: int = 120) -> tuple[int, str, str]:
            calls.append(args)
            return 0, _sample_opencli_channel_json(), ""

        old_provider = backend_registry_module.OpenCliYouTubeChannelProvider
        try:
            backend_registry_module.OpenCliYouTubeChannelProvider = (  # type: ignore[assignment]
                lambda: OpenCliYouTubeChannelProvider(runner=fake_runner)
            )
            with tempfile.TemporaryDirectory() as tmp_dir:
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    exit_code = cli_main(
                        [
                            "collect",
                            "https://www.youtube.com/@ExampleChannel/videos",
                            "--out",
                            tmp_dir,
                            "--enable-browser-backends",
                            "--backend",
                            "mock",
                            "--limit",
                            "2",
                        ]
                    )

                payload = json.loads(stdout.getvalue())
                output_paths = [Path(value) for value in payload["outputs"]]

                self.assertEqual(exit_code, 0)
                self.assertTrue(payload["ok"])
                self.assertEqual(payload["item_count"], 2)
                self.assertEqual(payload["success_count"], 2)
                self.assertEqual(calls, [["opencli", "youtube", "channel", "@ExampleChannel", "--limit", "2", "-f", "json"]])
                for output_path in output_paths:
                    self.assertTrue(output_path.exists())
                    self.assertIn("Mock transcript for Openmd skeleton", output_path.read_text(encoding="utf-8"))
                manifest_path = default_manifest_path(
                    tmp_dir,
                    platform="youtube",
                    collection_url="https://www.youtube.com/@ExampleChannel/videos",
                )
                manifest_rows = [
                    json.loads(line)
                    for line in manifest_path.read_text(encoding="utf-8").splitlines()
                ]
                self.assertEqual([row["video_id"] for row in manifest_rows], ["demoVideo01", "o3Example"])
                self.assertEqual(manifest_rows[0]["channel_id"], "UC_synthetic_fixture_channel")
                self.assertEqual(manifest_rows[0]["discovery_provider"], YOUTUBE_DISCOVERY_BACKEND)
        finally:
            backend_registry_module.OpenCliYouTubeChannelProvider = old_provider

    def test_collect_passes_limit_to_discovery_request(self) -> None:
        calls: list[DiscoveryRequest] = []

        class RecordingDiscoveryExecutor:
            def discover(self, request: DiscoveryRequest) -> DiscoveryResult:
                calls.append(request)
                return DiscoveryResult.failure(
                    provider="recording.discovery",
                    error_code="recording_failure",
                    error_summary="recording discovery stopped before extraction",
                )

        old_build_discovery_executor = cli_module._build_discovery_executor
        try:
            cli_module._build_discovery_executor = (  # type: ignore[assignment]
                lambda *_, **__: RecordingDiscoveryExecutor()
            )
            with tempfile.TemporaryDirectory() as tmp_dir:
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    exit_code = cli_main(
                        [
                            "collect",
                            "https://www.youtube.com/@openmd/videos",
                            "--out",
                            tmp_dir,
                            "--limit",
                            "7",
                        ]
                    )
        finally:
            cli_module._build_discovery_executor = old_build_discovery_executor

        self.assertEqual(exit_code, 1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].limit, 7)
        self.assertFalse(calls[0].allow_all)

    def test_collect_all_guard_blocks_before_discovery(self) -> None:
        old_build_discovery_executor = cli_module._build_discovery_executor
        try:
            cli_module._build_discovery_executor = (  # type: ignore[assignment]
                lambda *_, **__: (_ for _ in ()).throw(
                    AssertionError("--all guard must block before discovery")
                )
            )
            with tempfile.TemporaryDirectory() as tmp_dir:
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    exit_code = cli_main(
                        [
                            "collect",
                            "https://www.youtube.com/@openmd/videos",
                            "--out",
                            tmp_dir,
                            "--all",
                        ]
                    )
        finally:
            cli_module._build_discovery_executor = old_build_discovery_executor

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 1)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["failed_count"], 1)
        self.assertIn("--all requires explicit confirmation", payload["message"])

    def test_cli_help_still_available(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli_main(["--help"]), 0)

    def test_cli_result_json_is_ascii_safe_for_windows_console(self) -> None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            cli_module._print_result(
                cli_module.CliResult(
                    ok=False,
                    command="collect",
                    message="包含非 ASCII 的标题",
                )
            )

        output = stdout.getvalue()
        output.encode("cp1252")
        self.assertIn("\\u5305\\u542b", output)

    def test_registry_lists_podwise_as_xiaoyuzhou_fallback(self) -> None:
        registry = create_default_registry()
        chain = registry.backend_chain("xiaoyuzhou")
        self.assertEqual(chain[0], "opencli.xiaoyuzhou.transcript")
        self.assertIn("podwise.transcript", chain)

    def test_registry_uses_opencli_xiaoe_adapter(self) -> None:
        registry = create_default_registry()
        adapter = registry.adapter_for("https://demo.xet.citv.cn/p/example")
        chain = registry.backend_chain("xiaoe")

        self.assertIsInstance(adapter, OpenCliPlatformAdapter)
        self.assertEqual(adapter.id, "xiaoe")
        self.assertEqual(chain[0], "opencli.xiaoe.transcript")
        self.assertIn("opencli.xiaoe.content", chain)

    def test_xiaoe_transcript_uses_opencli_output_markdown(self) -> None:
        adapter = OpenCliPlatformAdapter("xiaoe", "opencli.xiaoe.transcript")
        item = ContentItem(
            platform="xiaoe",
            input_type="single",
            url="https://demo.xet.citv.cn/p/course/video/example",
            source_type="video",
        )
        calls: list[tuple[str, ...]] = []

        def fake_opencli(args: list[str], **_: object) -> tuple[int, str, str]:
            calls.append(tuple(args))
            out_dir = Path(args[args.index("--out") + 1])
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "lesson.md").write_text(
                "# Xiaoe lesson\n\n[00:00:01] transcript text\n",
                encoding="utf-8",
            )
            return (0, '{"outputPath":"redacted"}', "")

        old_opencli = opencli_adapters._run_opencli
        try:
            opencli_adapters._run_opencli = fake_opencli
            result = adapter.extract_text(item, allow_fallback=False)
        finally:
            opencli_adapters._run_opencli = old_opencli

        self.assertTrue(result.has_usable_text)
        self.assertEqual(result.backend, "opencli.xiaoe.transcript")
        self.assertIn("[00:00:01] transcript text", result.text)
        self.assertEqual(calls[0][:2], ("xiaoe", "transcript"))
        self.assertIn("--keep-artifacts", calls[0])
        self.assertIn("false", calls[0])

    def test_xiaoyuzhou_credentials_require_matching_device_properties(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            credential_path = Path(tmp_dir) / "xiaoyuzhou.json"
            base_credentials = {
                "access_token": "access",
                "refresh_token": "refresh",
                "device_id": "device-1",
                "device_properties": "",
            }
            credential_path.write_text(
                json.dumps(base_credentials),
                encoding="utf-8",
            )

            ready, code, summary = opencli_adapters._check_xiaoyuzhou_credentials(
                credential_path
            )

            self.assertFalse(ready)
            self.assertEqual(code, "xiaoyuzhou_device_properties_missing")
            self.assertIn("device_properties", summary)

            base_credentials["device_properties"] = json.dumps(
                {
                    "uuid": "other-device",
                    "android_id": "android-id",
                    "oaid": "",
                    "vaid": "",
                    "aaid": "",
                },
                separators=(",", ":"),
            )
            credential_path.write_text(
                json.dumps(base_credentials),
                encoding="utf-8",
            )

            ready, code, summary = opencli_adapters._check_xiaoyuzhou_credentials(
                credential_path
            )

            self.assertFalse(ready)
            self.assertEqual(code, "xiaoyuzhou_device_mismatch")
            self.assertIn("uuid", summary)

            base_credentials["device_properties"] = json.dumps(
                {
                    "uuid": "device-1",
                    "android_id": "android-id",
                    "oaid": "",
                    "vaid": "",
                    "aaid": "",
                },
                separators=(",", ":"),
            )
            credential_path.write_text(
                json.dumps(base_credentials),
                encoding="utf-8",
            )

            ready, code, _summary = opencli_adapters._check_xiaoyuzhou_credentials(
                credential_path
            )

            self.assertTrue(ready)
            self.assertEqual(code, "ok")

            ios_credentials = {
                "access_token": "access",
                "refresh_token": "refresh",
                "device_id": "ios-device-1",
                "device_properties": json.dumps(
                    {
                        "idfa": "00000000-0000-0000-0000-000000000000",
                        "idfv": "idfv-value",
                    },
                    separators=(",", ":"),
                ),
            }
            credential_path.write_text(
                json.dumps(ios_credentials),
                encoding="utf-8",
            )

            ready, code, summary = opencli_adapters._check_xiaoyuzhou_credentials(
                credential_path
            )

            self.assertTrue(ready)
            self.assertEqual(code, "ok")
            self.assertIn("OpenCLI-compatible iOS", summary)

            ios_credentials["request_headers"] = {
                "user-agent": "Xiaoyuzhou/2.108.1 (build:3626; iOS 26.5.0)",
                "os": "ios",
                "os-version": "26.5.0",
                "manufacturer": "Apple",
                "model": "iPhone16,2",
                "market": "AppStore",
                "app-version": "2.108.1",
                "app-buildno": "3626",
                "bundleid": "app.podcast.cosmos",
                "x-jike-device-id": "ios-device-1",
            }
            credential_path.write_text(
                json.dumps(ios_credentials),
                encoding="utf-8",
            )

            ready, code, _summary = opencli_adapters._check_xiaoyuzhou_credentials(
                credential_path
            )

            self.assertTrue(ready)
            self.assertEqual(code, "ok")

    def test_xiaoyuzhou_falls_back_to_podwise_when_credentials_missing(self) -> None:
        adapter = OpenCliPlatformAdapter("xiaoyuzhou", "opencli.xiaoyuzhou.transcript")
        item = ContentItem(
            platform="xiaoyuzhou",
            input_type="single",
            url="https://www.xiaoyuzhoufm.com/episode/example",
            source_type="audio",
        )
        calls: list[tuple[str, tuple[str, ...]]] = []

        def fake_opencli(args: list[str], **_: object) -> tuple[int, str, str]:
            calls.append(("opencli", tuple(args)))
            if args[:2] == ["xiaoyuzhou", "transcript"]:
                return (
                    78,
                    "",
                    "Missing Xiaoyuzhou credentials. Expected xiaoyuzhou.json credential file.",
                )
            if args[:2] == ["web", "read"]:
                return (0, "# Sample Episode\n\npublic intro", "")
            return (1, "", "unexpected opencli call")

        def fake_podwise(args: list[str], **_: object) -> tuple[int, str, str]:
            calls.append(("podwise", tuple(args)))
            if args[:2] == ["search", "episode"]:
                return (
                    0,
                    "- Episode URL: https://podwise.ai/dashboard/episodes/123\n",
                    "",
                )
            if args[:2] == ["get", "transcript"]:
                return (0, "[00:00:01] speaker: transcript text\n", "")
            return (1, "", "unexpected podwise call")

        old_opencli = opencli_adapters._run_opencli
        old_podwise = opencli_adapters._run_podwise
        try:
            opencli_adapters._run_opencli = fake_opencli
            opencli_adapters._run_podwise = fake_podwise
            result = adapter.extract_text(item, allow_fallback=True)
        finally:
            opencli_adapters._run_opencli = old_opencli
            opencli_adapters._run_podwise = old_podwise

        self.assertTrue(result.has_usable_text)
        self.assertEqual(result.backend, "podwise.transcript")
        self.assertIn("[00:00:01]", result.text)
        self.assertIn(("podwise", ("get", "transcript", "https://podwise.ai/dashboard/episodes/123")), calls)

    def test_podwise_not_processed_does_not_auto_process_episode(self) -> None:
        adapter = OpenCliPlatformAdapter("xiaoyuzhou", "podwise.transcript")
        item = ContentItem(
            platform="xiaoyuzhou",
            input_type="single",
            url="https://podwise.ai/dashboard/episodes/123",
            source_type="audio",
        )
        calls: list[tuple[str, ...]] = []

        def fake_podwise(args: list[str], **_: object) -> tuple[int, str, str]:
            calls.append(tuple(args))
            return (1, "", "Error: episode has not been processed yet")

        old_podwise = opencli_adapters._run_podwise
        try:
            opencli_adapters._run_podwise = fake_podwise
            result = adapter.extract_text(item, backend="podwise.transcript")
        finally:
            opencli_adapters._run_podwise = old_podwise

        self.assertFalse(result.has_usable_text)
        self.assertEqual(result.error_code, "podwise_episode_not_processed")
        self.assertNotIn(("process", "https://podwise.ai/dashboard/episodes/123"), calls)

    def test_opencli_wrapper_runs_harmless_mock_command(self) -> None:
        backend = OpenCliBackend(executable=sys.executable, timeout=10)
        result = backend.run(["-c", "print('opencli wrapper ok')"], name="mock.opencli")
        self.assertTrue(result.success)
        self.assertIn("opencli wrapper ok", result.stdout_summary)


class ArticleProviderTests(unittest.TestCase):
    def test_article_provider_gates_register_only_when_enabled(self) -> None:
        safe_weixin = create_runtime_backend_provider_registry("weixin", BackendRuntimeOptions())
        self.assertEqual(
            safe_weixin.provider_for(WEIXIN_LOCAL_BACKEND).descriptor().provider,
            "openmd.skeleton",
        )
        self.assertEqual(
            safe_weixin.provider_for(OPENCLI_WEIXIN_DOWNLOAD_BACKEND).descriptor().provider,
            "openmd.skeleton",
        )

        network_weixin = create_runtime_backend_provider_registry(
            "weixin",
            BackendRuntimeOptions(enable_network_backends=True),
        )
        self.assertIsInstance(
            network_weixin.provider_for(WEIXIN_LOCAL_BACKEND),
            WeixinArticleLocalProvider,
        )

        browser_weixin = create_runtime_backend_provider_registry(
            "weixin",
            BackendRuntimeOptions(enable_browser_backends=True),
        )
        self.assertIsInstance(
            browser_weixin.provider_for(OPENCLI_WEIXIN_DOWNLOAD_BACKEND),
            OpenCliWeixinDownloadProvider,
        )
        self.assertIsInstance(
            browser_weixin.provider_for(OPENCLI_WEB_READ_BACKEND),
            OpenCliWebReadProvider,
        )

        safe_web = create_runtime_backend_provider_registry("web_article", BackendRuntimeOptions())
        self.assertEqual(
            safe_web.provider_for(WEB_ARTICLE_TRAFILATURA_BACKEND).descriptor().provider,
            "openmd.skeleton",
        )
        network_web = create_runtime_backend_provider_registry(
            "web_article",
            BackendRuntimeOptions(enable_network_backends=True),
        )
        self.assertIsInstance(
            network_web.provider_for(WEB_ARTICLE_TRAFILATURA_BACKEND),
            WebArticleTrafilaturaProvider,
        )
        browser_web = create_runtime_backend_provider_registry(
            "web_article",
            BackendRuntimeOptions(enable_browser_backends=True),
        )
        self.assertIsInstance(browser_web.provider_for(OPENCLI_WEB_READ_BACKEND), OpenCliWebReadProvider)

    def test_default_article_backend_chain_is_native_then_browser_then_heavy_fallback(self) -> None:
        registry = create_default_registry()

        self.assertEqual(
            registry.backend_chain("weixin"),
            (
                WEIXIN_LOCAL_BACKEND,
                WECHAT_ARTICLE_EXPORTER_DOWNLOAD_BACKEND,
                OPENCLI_WEIXIN_DOWNLOAD_BACKEND,
                OPENCLI_WEB_READ_BACKEND,
            ),
        )
        self.assertEqual(
            registry.backend_chain("web_article"),
            (WEB_ARTICLE_TRAFILATURA_BACKEND, OPENCLI_WEB_READ_BACKEND),
        )

    def test_weixin_local_provider_parses_fixture_metadata_content_images_and_code(self) -> None:
        provider = WeixinArticleLocalProvider(
            fetcher=lambda _url, _timeout: self._weixin_fixture_html(),
            min_article_chars=1,
        )
        item = ContentItem(
            platform="weixin",
            input_type="single",
            url="http://mp.weixin.qq.com/s/example&amp;foo=1",
            source_type="article",
        )

        result = provider.extract(
            BackendExtractRequest(item=item, backend_chain=(WEIXIN_LOCAL_BACKEND,), allow_fallback=False)
        )

        self.assertTrue(result.has_usable_text, result.error_summary)
        self.assertEqual(result.status, "verified")
        self.assertEqual(result.source_level, "native_text")
        self.assertEqual(result.backend, WEIXIN_LOCAL_BACKEND)
        self.assertEqual(result.title, "Fixture Title")
        self.assertEqual(result.author, "Fixture Account")
        self.assertEqual(result.published_at, "2023-11-15 06:13:20")
        self.assertIn("hello paragraph", result.text)
        self.assertIn("https://mmbiz.qpic.cn/demo/0?wx_fmt=png", result.text)
        self.assertIn("```python", result.text)
        self.assertIn("print('hi')", result.text)
        self.assertEqual(
            normalize_weixin_url("http://mp.weixin.qq.com/s/example&amp;foo=1"),
            "https://mp.weixin.qq.com/s/example&foo=1",
        )

    def test_asset_download_flag_localizes_images_and_keeps_failed_remote_links(self) -> None:
        item = ContentItem(
            platform="weixin",
            input_type="single",
            url="https://mp.weixin.qq.com/s/example",
            title="Weixin assets",
            source_type="article",
        )
        result = TranscriptResult(
            text=(
                "![ok](https://mmbiz.qpic.cn/demo/0?wx_fmt=jpeg)\n"
                "![bad](https://example.com/bad.png)\n"
            ),
            status="verified",
            source_level="native_text",
            backend=WEIXIN_LOCAL_BACKEND,
        )
        calls: list[tuple[str, dict[str, str], int]] = []

        def fake_fetcher(url: str, headers: dict[str, str], timeout: int) -> tuple[bytes, str | None]:
            calls.append((url, headers, timeout))
            if "bad.png" in url:
                raise RuntimeError("temporary image failure")
            return b"image-bytes", "image/jpeg"

        with tempfile.TemporaryDirectory() as tmp_dir:
            no_assets_path = write_item_result(item, result, tmp_dir, download_assets=False)
            no_assets_markdown = no_assets_path.read_text(encoding="utf-8")
            self.assertIn("https://mmbiz.qpic.cn/demo/0?wx_fmt=jpeg", no_assets_markdown)
            self.assertFalse((no_assets_path.parent / "assets" / "images").exists())

            output_path = write_item_result(
                item,
                result,
                tmp_dir,
                overwrite=True,
                download_assets=True,
                asset_fetcher=fake_fetcher,
            )
            markdown = output_path.read_text(encoding="utf-8")
            image_path = output_path.parent / "assets" / "images" / "img_001.jpg"

            self.assertTrue(image_path.exists())
            self.assertEqual(image_path.read_bytes(), b"image-bytes")
            self.assertIn("![ok](assets/images/img_001.jpg)", markdown)
            self.assertIn("![bad](https://example.com/bad.png)", markdown)
            self.assertIn("asset_download_failed", markdown)
            self.assertEqual(calls[0][1]["Referer"], "https://mp.weixin.qq.com/")

    def test_localize_markdown_images_has_non_throwing_failure_mode(self) -> None:
        def failing_fetcher(_url: str, _headers: dict[str, str], _timeout: int) -> tuple[bytes, str | None]:
            raise RuntimeError("image backend unavailable")

        with tempfile.TemporaryDirectory() as tmp_dir:
            markdown, notes = localize_markdown_images(
                "![x](https://example.com/image.png)",
                tmp_dir,
                fetcher=failing_fetcher,
            )

        self.assertEqual(markdown, "![x](https://example.com/image.png)")
        self.assertTrue(any(note.startswith("asset_download_failed:") for note in notes))

    def test_web_article_trafilatura_provider_uses_mock_api_and_metadata(self) -> None:
        calls: list[dict[str, object]] = []

        def fake_extractor(downloaded: object, **kwargs: object) -> str:
            calls.append({"downloaded": downloaded, **kwargs})
            return "# Extracted Title\n\nBody text with enough useful article characters.\n\n![](https://example.com/a.png)"

        provider = WebArticleTrafilaturaProvider(
            fetcher=lambda url: f"downloaded:{url}",
            extractor=fake_extractor,
            metadata_extractor=lambda _downloaded: {
                "title": "Metadata Title",
                "author": "Metadata Author",
                "date": "2026-06-06",
            },
            min_article_chars=1,
        )
        item = ContentItem(
            platform="web_article",
            input_type="single",
            url="https://example.com/article",
            source_type="article",
        )

        result = provider.extract(
            BackendExtractRequest(
                item=item,
                backend_chain=(WEB_ARTICLE_TRAFILATURA_BACKEND,),
                allow_fallback=False,
            )
        )

        self.assertTrue(result.has_usable_text, result.error_summary)
        self.assertEqual(result.backend, WEB_ARTICLE_TRAFILATURA_BACKEND)
        self.assertEqual(result.source_level, "native_text")
        self.assertEqual(result.title, "Metadata Title")
        self.assertEqual(result.author, "Metadata Author")
        self.assertEqual(result.published_at, "2026-06-06")
        self.assertIn("include_images", calls[0])
        self.assertTrue(calls[0]["include_images"])
        self.assertTrue(calls[0]["include_links"])

    def test_web_article_trafilatura_empty_content_returns_structured_failure(self) -> None:
        provider = WebArticleTrafilaturaProvider(
            fetcher=lambda _url: "downloaded",
            extractor=lambda *_args, **_kwargs: "too short",
            min_article_chars=20,
        )
        item = ContentItem(
            platform="web_article",
            input_type="single",
            url="https://example.com/article",
            source_type="article",
        )

        result = provider.extract(
            BackendExtractRequest(
                item=item,
                backend_chain=(WEB_ARTICLE_TRAFILATURA_BACKEND,),
                allow_fallback=False,
            )
        )

        self.assertFalse(result.has_usable_text)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.error_code, "web_article_empty_content")
        self.assertEqual(result.source_level, "partial_extraction")

    def test_opencli_weixin_download_fallback_reads_temp_markdown(self) -> None:
        calls: list[tuple[list[str], int]] = []

        def fake_runner(command: list[str], timeout: int) -> tuple[int, str, str]:
            calls.append((command, timeout))
            out_dir = Path(command[command.index("--output") + 1])
            article_dir = out_dir / "article"
            article_dir.mkdir(parents=True)
            (article_dir / "article.md").write_text("# OpenCLI Title\n\nraw body", encoding="utf-8")
            return 0, json.dumps([{"title": "OpenCLI Title", "author": "OpenCLI Author"}]), ""

        provider = OpenCliWeixinDownloadProvider(runner=fake_runner, timeout=5)
        item = ContentItem(
            platform="weixin",
            input_type="single",
            url="https://mp.weixin.qq.com/s/example",
            source_type="article",
        )

        result = provider.extract(
            BackendExtractRequest(
                item=item,
                backend_chain=(OPENCLI_WEIXIN_DOWNLOAD_BACKEND,),
                allow_fallback=False,
            )
        )

        self.assertTrue(result.has_usable_text, result.error_summary)
        self.assertEqual(result.backend, OPENCLI_WEIXIN_DOWNLOAD_BACKEND)
        self.assertEqual(result.source_level, "third_party_extractor")
        self.assertEqual(result.title, "OpenCLI Title")
        self.assertIn("--download-images", calls[0][0])
        self.assertIn("false", calls[0][0])

    def test_opencli_web_read_fallback_returns_markdown_stdout(self) -> None:
        calls: list[tuple[list[str], int]] = []

        def fake_runner(command: list[str], timeout: int) -> tuple[int, str, str]:
            calls.append((command, timeout))
            return 0, "# Web Fallback\n\nraw article body", ""

        provider = OpenCliWebReadProvider(runner=fake_runner, timeout=6)
        item = ContentItem(
            platform="web_article",
            input_type="single",
            url="https://example.com/article",
            source_type="article",
        )

        result = provider.extract(
            BackendExtractRequest(
                item=item,
                backend_chain=(OPENCLI_WEB_READ_BACKEND,),
                allow_fallback=False,
            )
        )

        self.assertTrue(result.has_usable_text, result.error_summary)
        self.assertEqual(result.backend, OPENCLI_WEB_READ_BACKEND)
        self.assertEqual(result.title, "Web Fallback")
        self.assertIn("--stdout", calls[0][0])
        self.assertIn("--download-images", calls[0][0])

    def test_provider_failure_and_runlog_do_not_leak_sensitive_artifacts(self) -> None:
        provider = WebArticleTrafilaturaProvider(
            fetcher=lambda _url: (_ for _ in ()).throw(
                ValueError("cookie=secret token=secret headers={'x': 'y'} <html>raw</html>")
            ),
            extractor=lambda *_args, **_kwargs: "unused",
        )
        item = ContentItem(
            platform="web_article",
            input_type="single",
            url="https://example.com/article?token=secret&view=1",
            source_type="article",
        )

        result = provider.extract(
            BackendExtractRequest(
                item=item,
                backend_chain=(WEB_ARTICLE_TRAFILATURA_BACKEND,),
                allow_fallback=False,
            )
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            runlog = Path(tmp_dir) / "runlog.jsonl"
            append_runlog_entry(runlog, build_item_runlog_entry(item, result))
            content = runlog.read_text(encoding="utf-8").lower()

        for forbidden in ("cookie", "token", "headers", "secret", "<html", "raw</html>"):
            self.assertNotIn(forbidden, result.error_summary or "")
            self.assertNotIn(forbidden, content)

    @staticmethod
    def _weixin_fixture_html() -> str:
        return """
        <html><body>
          <h1 id="activity-name">Fixture Title</h1>
          <a id="js_name">Fixture Account</a>
          <script>var create_time = '1700000000';</script>
          <div id="js_content">
            <p>hello paragraph</p>
            <p><img alt="cover" data-src="//mmbiz.qpic.cn/demo/0?wx_fmt=png" /></p>
            <div class="code-snippet__fix">
              <pre data-lang="python"><code>print('hi')</code></pre>
            </div>
          </div>
        </body></html>
        """


if __name__ == "__main__":
    unittest.main()
