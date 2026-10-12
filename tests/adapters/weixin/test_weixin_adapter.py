from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

from openmd.adapters.registry import create_default_registry
from openmd.adapters.weixin.adapter import WeixinOfficialAccountAdapter
from openmd.adapters.weixin.discovery import (
    WECHAT_ARTICLE_EXPORTER_DISCOVERY_BACKEND,
    WECHAT_ARTICLE_EXPORTER_HTTP_DISCOVERY_BACKEND,
    WechatArticleExporterDiscoveryProvider,
    WechatArticleExporterHttpDiscoveryProvider,
    build_wechat_article_exporter_args,
    parse_wechat_article_exporter_output,
)
from openmd.adapters.weixin.report import (
    build_weixin_author_batch_report,
    load_runlog_entries,
    write_weixin_author_batch_report,
)
from openmd.backends.base import BackendExtractRequest, DiscoveryRequest
from openmd.backends.executor import SkeletonBackendProvider, SkeletonDiscoveryProvider
from openmd.backends.registry import (
    BackendRuntimeOptions,
    create_runtime_backend_provider_registry,
    create_runtime_discovery_provider_registry,
)
from openmd.backends.weixin import (
    WECHAT_ARTICLE_EXPORTER_DOWNLOAD_BACKEND,
    WechatArticleExporterDownloadProvider,
)
from openmd.cli import main as cli_main
from openmd.core.package_validator import validate_package
from openmd.core.runlog import append_runlog_entry, build_runlog_entry


class WeixinOfficialAccountAdapterTests(unittest.TestCase):
    def test_registry_loads_weixin_adapter_without_core_registry_edits(self) -> None:
        registry = create_default_registry()
        adapter = registry.adapter_for("https://mp.weixin.qq.com/s/article_slug")

        self.assertIsInstance(adapter, WeixinOfficialAccountAdapter)
        self.assertEqual(adapter.id, "weixin")

    def test_resolves_single_article_as_article_profile(self) -> None:
        adapter = WeixinOfficialAccountAdapter()

        item = adapter.resolve_items("http://mp.weixin.qq.com/s/article_slug?title=Demo")[0]

        self.assertEqual(item.platform, "weixin")
        self.assertEqual(item.input_type, "single")
        self.assertEqual(item.source_type, "article")
        self.assertEqual(item.id, "article_slug")
        self.assertEqual(item.raw_meta["profile"], "article")
        self.assertEqual(item.raw_meta["permission_scope"], "user_accessible")

    def test_resolves_author_selector_without_running_discovery(self) -> None:
        adapter = WeixinOfficialAccountAdapter()

        item = adapter.resolve_items("weixin://author/openmd", limit=10)[0]

        self.assertEqual(item.platform, "weixin")
        self.assertEqual(item.input_type, "collection")
        self.assertEqual(item.source_type, "article")
        self.assertEqual(item.raw_meta["collection_selector"], "weixin://author/openmd")
        self.assertEqual(item.raw_meta["discovery_limit"], 10)

    def test_mock_package_smoke_for_single_article_uses_no_real_backend(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = cli_main(
                    [
                        "ingest",
                        "https://mp.weixin.qq.com/s/article_slug?token=secret&view=1",
                        "--out",
                        tmp_dir,
                        "--backend",
                        "mock",
                        "--package",
                    ]
                )

            payload = json.loads(stdout.getvalue())
            package_path = Path(payload["outputs"][0])
            combined = "\n".join(
                path.read_text(encoding="utf-8")
                for path in package_path.rglob("*")
                if path.is_file() and path.suffix in {".md", ".json"}
            )

            self.assertEqual(exit_code, 0)
            self.assertTrue(payload["ok"])
            self.assertTrue(validate_package(package_path).ok)
            self.assertEqual(list((package_path / "assets").iterdir()), [])
            self.assertNotIn("token=secret", combined)
            self.assertIn("view=1", combined)


class WeixinDiscoveryProviderTests(unittest.TestCase):
    def test_discovery_provider_is_browser_gated(self) -> None:
        safe = create_runtime_discovery_provider_registry("weixin", BackendRuntimeOptions())
        browser = create_runtime_discovery_provider_registry(
            "weixin",
            BackendRuntimeOptions(enable_browser_backends=True),
        )

        request = DiscoveryRequest(
            input_url="https://mp.weixin.qq.com/cgi-bin/appmsg?action=list_ex",
            platform="weixin",
            limit=2,
        )
        self.assertIsInstance(safe.provider_for(request), SkeletonDiscoveryProvider)
        self.assertIsInstance(
            browser.provider_for(request),
            WechatArticleExporterHttpDiscoveryProvider,
        )
        legacy = create_runtime_discovery_provider_registry(
            "weixin",
            BackendRuntimeOptions(
                enable_browser_backends=True,
                requested_discovery_provider=WECHAT_ARTICLE_EXPORTER_DISCOVERY_BACKEND,
            ),
        )
        self.assertIsInstance(
            legacy.provider_for(request, requested_provider=WECHAT_ARTICLE_EXPORTER_DISCOVERY_BACKEND),
            WechatArticleExporterDiscoveryProvider,
        )

    def test_http_discovery_alias_registers_provider(self) -> None:
        registry = create_runtime_discovery_provider_registry(
            "weixin",
            BackendRuntimeOptions(
                enable_browser_backends=True,
                requested_discovery_provider="weixin-http",
            ),
        )

        request = DiscoveryRequest(
            input_url="weixin://author/请辩",
            platform="weixin",
            limit=2,
        )

        self.assertIsInstance(
            registry.provider_for(request, requested_provider=WECHAT_ARTICLE_EXPORTER_HTTP_DISCOVERY_BACKEND),
            WechatArticleExporterHttpDiscoveryProvider,
        )

    def test_builds_exporter_command_with_limit(self) -> None:
        self.assertEqual(
            build_wechat_article_exporter_args("weixin://author/openmd", limit=3),
            [
                "wechat-article-exporter",
                "articles",
                "--selector",
                "weixin://author/openmd",
                "--format",
                "json",
                "--limit",
                "3",
            ],
        )

    def test_parses_common_exporter_json_shapes(self) -> None:
        articles = parse_wechat_article_exporter_output(
            json.dumps(
                {
                    "data": {
                        "list": [
                            {
                                "url": "http://mp.weixin.qq.com/s/abc",
                                "title": "Article A",
                                "author": "Account A",
                                "publish_time": "2026-06-23",
                            },
                            {"url": "https://example.com/not-weixin"},
                        ]
                    }
                }
            )
        )

        self.assertEqual(len(articles), 1)
        self.assertEqual(articles[0].url, "https://mp.weixin.qq.com/s/abc")
        self.assertEqual(articles[0].title, "Article A")
        self.assertEqual(articles[0].author, "Account A")
        self.assertEqual(articles[0].published_at, "2026-06-23")

    def test_fake_runner_returns_weixin_content_items_and_respects_limit(self) -> None:
        calls: list[tuple[tuple[str, ...], int]] = []

        def fake_runner(command: list[str], timeout: int) -> tuple[int, str, str]:
            calls.append((tuple(command), timeout))
            return (
                0,
                json.dumps(
                    [
                        {
                            "url": "https://mp.weixin.qq.com/s/a",
                            "title": "Article A",
                            "author": "Account A",
                            "publish_time": "2026-06-20",
                        },
                        {
                            "url": "https://mp.weixin.qq.com/s/b",
                            "title": "Article B",
                            "author": "Account B",
                        },
                    ]
                ),
                "",
            )

        provider = WechatArticleExporterDiscoveryProvider(runner=fake_runner, timeout=7)
        result = provider.discover(
            DiscoveryRequest(
                input_url="weixin://author/openmd",
                platform="weixin",
                limit=1,
            )
        )

        self.assertTrue(result.ok, result.error_summary)
        self.assertEqual(result.provider, WECHAT_ARTICLE_EXPORTER_DISCOVERY_BACKEND)
        self.assertEqual(len(result.items), 1)
        self.assertEqual(result.items[0].platform, "weixin")
        self.assertEqual(result.items[0].source_type, "article")
        self.assertEqual(result.items[0].title, "Article A")
        self.assertEqual(result.items[0].raw_meta["discovery_provider"], WECHAT_ARTICLE_EXPORTER_DISCOVERY_BACKEND)
        self.assertEqual(calls[0][1], 7)

    def test_missing_exporter_returns_structured_blocker(self) -> None:
        provider = WechatArticleExporterDiscoveryProvider(
            runner=lambda _command, _timeout: (127, "", "missing executable")
        )

        result = provider.discover(
            DiscoveryRequest(
                input_url="weixin://author/openmd",
                platform="weixin",
                limit=2,
            )
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "discovery_tool_missing")
        self.assertIn("missing executable", result.error_summary or "")

    def test_safe_collect_skeleton_smoke_does_not_run_real_exporter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = cli_main(
                    [
                        "collect",
                        "https://mp.weixin.qq.com/cgi-bin/appmsg?action=list_ex",
                        "--out",
                        tmp_dir,
                        "--manifest-only",
                    ]
                )

            payload = json.loads(stdout.getvalue())
            runlog_rows = [
                json.loads(line)
                for line in (Path(tmp_dir) / "runlog.jsonl").read_text(encoding="utf-8").splitlines()
            ]

            self.assertEqual(exit_code, 1)
            self.assertFalse(payload["ok"])
            self.assertEqual(runlog_rows[-1]["backend"], "skeleton.discovery")
            self.assertEqual(runlog_rows[-1]["error_code"], "discovery_not_implemented")

    def test_weixin_all_requires_explicit_browser_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = cli_main(
                    [
                        "collect",
                        "weixin://author/请辩",
                        "--out",
                        tmp_dir,
                        "--manifest-only",
                        "--all",
                    ]
                )

            payload = json.loads(stdout.getvalue())
            runlog_rows = [
                json.loads(line)
                for line in (Path(tmp_dir) / "runlog.jsonl").read_text(encoding="utf-8").splitlines()
            ]

            self.assertEqual(exit_code, 1)
            self.assertFalse(payload["ok"])
            self.assertEqual(runlog_rows[-1]["error_code"], "all_requires_confirmation")

    def test_weixin_all_reaches_http_auth_gate_when_explicitly_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            stdout = io.StringIO()
            with (
                patch.dict("os.environ", {"OPENMD_WECHAT_ARTICLE_EXPORTER_AUTH_KEY": ""}),
                contextlib.redirect_stdout(stdout),
            ):
                exit_code = cli_main(
                    [
                        "collect",
                        "weixin://author/请辩",
                        "--out",
                        tmp_dir,
                        "--manifest-only",
                        "--all",
                        "--enable-browser-backends",
                        "--discovery-backend",
                        "weixin-http",
                    ]
                )

            payload = json.loads(stdout.getvalue())
            runlog_rows = [
                json.loads(line)
                for line in (Path(tmp_dir) / "runlog.jsonl").read_text(encoding="utf-8").splitlines()
            ]

            self.assertEqual(exit_code, 1)
            self.assertFalse(payload["ok"])
            self.assertEqual(runlog_rows[-1]["backend"], WECHAT_ARTICLE_EXPORTER_HTTP_DISCOVERY_BACKEND)
            self.assertEqual(runlog_rows[-1]["error_code"], "auth_key_required")


class WeixinHttpDiscoveryProviderTests(unittest.TestCase):
    def test_missing_auth_key_returns_structured_blocker_without_fetching(self) -> None:
        calls: list[str] = []
        provider = WechatArticleExporterHttpDiscoveryProvider(
            auth_key="",
            fetch_json=lambda url, _headers, _timeout: calls.append(url) or (200, "{}"),
        )

        result = provider.discover(
            DiscoveryRequest(
                input_url="weixin://author/请辩",
                platform="weixin",
                limit=2,
            )
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "auth_key_required")
        self.assertEqual(calls, [])
        self.assertNotIn("secret", result.error_summary or "")

    def test_http_provider_discovers_account_articles_with_pagination_and_limit(self) -> None:
        calls: list[tuple[str, dict[str, str], int]] = []

        def fake_fetch(url: str, headers: dict[str, str], timeout: int) -> tuple[int, str]:
            calls.append((url, dict(headers), timeout))
            parsed = urlparse(url)
            query = parse_qs(parsed.query)
            if parsed.path.endswith("/api/public/v1/account"):
                self.assertEqual(query["keyword"][0], "请辩")
                return (
                    200,
                    json.dumps(
                        {
                            "base_resp": {"ret": 0, "err_msg": "ok"},
                            "list": [
                                {
                                    "fakeid": "MzFakeQingbian==",
                                    "nickname": "请辩",
                                    "alias": "qingbian",
                                }
                            ],
                        }
                    ),
                )
            if parsed.path.endswith("/api/public/v1/article"):
                self.assertEqual(query["fakeid"][0], "MzFakeQingbian==")
                begin = int(query["begin"][0])
                rows = [
                    {
                        "link": "https://mp.weixin.qq.com/s/a",
                        "title": "Article A",
                        "author_name": "请辩",
                        "update_time": "2026-06-23",
                        "aid": "a_1",
                    },
                    {
                        "link": "https://mp.weixin.qq.com/s/b",
                        "title": "Article B",
                        "author_name": "请辩",
                        "update_time": "2026-06-22",
                        "aid": "b_1",
                    },
                    {
                        "link": "https://mp.weixin.qq.com/s/c",
                        "title": "Article C",
                        "author_name": "请辩",
                        "update_time": "2026-06-21",
                        "aid": "c_1",
                    },
                ][begin : begin + int(query["size"][0])]
                return (200, json.dumps({"base_resp": {"ret": 0, "err_msg": "ok"}, "articles": rows}))
            raise AssertionError(f"unexpected URL: {url}")

        provider = WechatArticleExporterHttpDiscoveryProvider(
            base_url="http://127.0.0.1:3000/",
            auth_key="secret-auth-key",
            fetch_json=fake_fetch,
            timeout=9,
            page_size=2,
        )

        result = provider.discover(
            DiscoveryRequest(
                input_url="weixin://author/请辩",
                platform="weixin",
                limit=3,
            )
        )

        self.assertTrue(result.ok, result.error_summary)
        self.assertEqual(result.provider, WECHAT_ARTICLE_EXPORTER_HTTP_DISCOVERY_BACKEND)
        self.assertEqual([item.title for item in result.items], ["Article A", "Article B", "Article C"])
        self.assertEqual(result.items[0].author, "请辩")
        self.assertEqual(result.items[0].raw_meta["discovery_provider"], WECHAT_ARTICLE_EXPORTER_HTTP_DISCOVERY_BACKEND)
        self.assertEqual(calls[0][1]["X-Auth-Key"], "secret-auth-key")
        self.assertEqual(calls[0][2], 9)
        self.assertEqual(len([call for call in calls if urlparse(call[0]).path.endswith("/api/public/v1/article")]), 2)
        self.assertNotIn("secret-auth-key", json.dumps(result.metadata, ensure_ascii=False))

    def test_http_provider_can_resolve_fakeid_selector_without_account_search(self) -> None:
        calls: list[str] = []

        def fake_fetch(url: str, _headers: dict[str, str], _timeout: int) -> tuple[int, str]:
            calls.append(url)
            parsed = urlparse(url)
            self.assertTrue(parsed.path.endswith("/api/public/v1/article"))
            self.assertEqual(parse_qs(parsed.query)["fakeid"][0], "MzFakeQingbian==")
            return (
                200,
                json.dumps(
                    {
                        "base_resp": {"ret": 0, "err_msg": "ok"},
                        "articles": [
                            {
                                "link": "https://mp.weixin.qq.com/s/a",
                                "title": "Article A",
                            }
                        ],
                    }
                ),
            )

        provider = WechatArticleExporterHttpDiscoveryProvider(
            auth_key="secret-auth-key",
            fetch_json=fake_fetch,
        )

        result = provider.discover(
            DiscoveryRequest(
                input_url="weixin://fakeid/MzFakeQingbian==",
                platform="weixin",
                limit=1,
            )
        )

        self.assertTrue(result.ok, result.error_summary)
        self.assertEqual(len(calls), 1)
        self.assertEqual(result.items[0].title, "Article A")

    def test_http_provider_sanitizes_auth_failure(self) -> None:
        provider = WechatArticleExporterHttpDiscoveryProvider(
            auth_key="secret-auth-key",
            fetch_json=lambda _url, _headers, _timeout: (
                403,
                "token=secret-auth-key cookie=abc authorization=Bearer xyz",
            ),
        )

        result = provider.discover(
            DiscoveryRequest(
                input_url="weixin://author/请辩",
                platform="weixin",
                limit=1,
            )
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "auth_required")
        self.assertNotIn("secret-auth-key", result.error_summary or "")
        self.assertNotIn("cookie", (result.error_summary or "").lower())


class WeixinExporterDownloadBackendTests(unittest.TestCase):
    def test_download_backend_is_browser_gated(self) -> None:
        safe = create_runtime_backend_provider_registry("weixin", BackendRuntimeOptions())
        browser = create_runtime_backend_provider_registry(
            "weixin",
            BackendRuntimeOptions(enable_browser_backends=True),
        )

        self.assertIsInstance(safe.provider_for(WECHAT_ARTICLE_EXPORTER_DOWNLOAD_BACKEND), SkeletonBackendProvider)
        self.assertIsInstance(
            browser.provider_for(WECHAT_ARTICLE_EXPORTER_DOWNLOAD_BACKEND),
            WechatArticleExporterDownloadProvider,
        )

    def test_download_backend_returns_markdown_from_sidecar_api(self) -> None:
        calls: list[tuple[str, int]] = []

        def fake_fetch(url: str, timeout: int) -> tuple[int, str]:
            calls.append((url, timeout))
            parsed = urlparse(url)
            self.assertTrue(parsed.path.endswith("/api/public/v1/download"))
            query = parse_qs(parsed.query)
            self.assertEqual(query["url"][0], "https://mp.weixin.qq.com/s/a")
            self.assertEqual(query["format"][0], "markdown")
            return (200, "# Article A\n\nBody from exporter markdown API.\n")

        item = WeixinOfficialAccountAdapter().resolve_items("https://mp.weixin.qq.com/s/a")[0]
        provider = WechatArticleExporterDownloadProvider(
            base_url="http://127.0.0.1:3000/",
            fetch_text=fake_fetch,
            timeout=5,
        )

        result = provider.extract(
            BackendExtractRequest(
                item=item,
                backend_chain=(WECHAT_ARTICLE_EXPORTER_DOWNLOAD_BACKEND,),
            )
        )

        self.assertTrue(result.has_usable_text, result.error_summary)
        self.assertEqual(result.backend, WECHAT_ARTICLE_EXPORTER_DOWNLOAD_BACKEND)
        self.assertEqual(result.title, "Article A")
        self.assertIn("Body from exporter markdown API.", result.text)
        self.assertEqual(calls[0][1], 5)

    def test_download_backend_removes_exporter_css_and_reader_chrome(self) -> None:
        item = WeixinOfficialAccountAdapter().resolve_items("https://mp.weixin.qq.com/s/a")[0]
        provider = WechatArticleExporterDownloadProvider(
            fetch_text=lambda _url, _timeout: (
                200,
                (
                    " #js\\_row\\_immersive\\_stream\\_wrap { max-width: 667px; margin: 0 auto; } "
                    "img { max-width: 100%; }\n\n"
                    "# Article A\n\n"
                    "在小说阅读器读本章\n\n"
                    "去阅读\n\n"
                    "Body from exporter markdown API.\n"
                ),
            ),
            min_article_chars=1,
        )

        result = provider.extract(
            BackendExtractRequest(
                item=item,
                backend_chain=(WECHAT_ARTICLE_EXPORTER_DOWNLOAD_BACKEND,),
            )
        )

        self.assertTrue(result.has_usable_text, result.error_summary)
        self.assertNotIn("max-width", result.text)
        self.assertNotIn("小说阅读器", result.text)
        self.assertTrue(result.text.startswith("# Article A"))
        self.assertIn("Body from exporter markdown API.", result.text)

    def test_download_backend_sanitizes_sidecar_http_failure(self) -> None:
        item = WeixinOfficialAccountAdapter().resolve_items("https://mp.weixin.qq.com/s/a")[0]
        provider = WechatArticleExporterDownloadProvider(
            fetch_text=lambda _url, _timeout: (599, "cookie=abc token=secret authorization=Bearer xyz"),
        )

        result = provider.extract(
            BackendExtractRequest(
                item=item,
                backend_chain=(WECHAT_ARTICLE_EXPORTER_DOWNLOAD_BACKEND,),
            )
        )

        self.assertFalse(result.has_usable_text)
        self.assertEqual(result.error_code, "sidecar_unreachable")
        self.assertNotIn("secret", result.error_summary or "")
        self.assertNotIn("cookie", (result.error_summary or "").lower())

    def test_download_backend_classifies_http_204_as_source_empty(self) -> None:
        item = WeixinOfficialAccountAdapter().resolve_items("https://mp.weixin.qq.com/s/a")[0]
        provider = WechatArticleExporterDownloadProvider(
            fetch_text=lambda _url, _timeout: (204, ""),
        )

        result = provider.extract(
            BackendExtractRequest(
                item=item,
                backend_chain=(WECHAT_ARTICLE_EXPORTER_DOWNLOAD_BACKEND,),
            )
        )

        self.assertFalse(result.has_usable_text)
        self.assertEqual(result.error_code, "weixin_source_empty")
        self.assertEqual(result.metadata["failure_class"], "source_unavailable")

    def test_download_backend_classifies_fixed_empty_shell_as_source_empty(self) -> None:
        item = WeixinOfficialAccountAdapter().resolve_items("https://mp.weixin.qq.com/s/a")[0]
        provider = WechatArticleExporterDownloadProvider(
            fetch_text=lambda _url, _timeout: (200, "# Article\n\n"),
            min_article_chars=1,
        )

        result = provider.extract(
            BackendExtractRequest(
                item=item,
                backend_chain=(WECHAT_ARTICLE_EXPORTER_DOWNLOAD_BACKEND,),
            )
        )

        self.assertFalse(result.has_usable_text)
        self.assertEqual(result.error_code, "weixin_source_empty")
        self.assertEqual(result.metadata["failure_class"], "source_unavailable")


class WeixinAuthorBatchReportTests(unittest.TestCase):
    def test_report_summarizes_covered_recovered_missing_and_sanitized_failures(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            runlog_path = Path(tmp_dir) / "runlog.jsonl"
            self._write_report_fixture(runlog_path)

            entries = load_runlog_entries([runlog_path])
            report = build_weixin_author_batch_report(entries)

            self.assertEqual(report["summary"]["total_items"], 4)
            self.assertEqual(report["summary"]["covered_count"], 2)
            self.assertEqual(report["summary"]["recovered_count"], 1)
            self.assertEqual(report["summary"]["missing_count"], 1)
            self.assertEqual(report["summary"]["failed_count"], 2)
            self.assertEqual(report["summary"]["fallback_attempt_item_count"], 1)
            self.assertEqual(report["final_failure_class_counts"]["source_unavailable"], 1)
            self.assertEqual(report["final_failure_class_counts"]["user_resolvable"], 1)
            self.assertEqual(report["all_attempt_error_code_counts"]["weixin_source_empty"], 2)
            self.assertEqual(
                report["fallback_backend_counts"][WECHAT_ARTICLE_EXPORTER_DOWNLOAD_BACKEND],
                1,
            )
            self.assertEqual(report["recovered_items"][0]["final_status"], "verified")
            self.assertEqual(report["missing_items"][0]["final_error_code"], "weixin_source_empty")

            serialized = json.dumps(report, ensure_ascii=False).lower()
            self.assertNotIn("secret", serialized)
            self.assertNotIn("cookie", serialized)
            self.assertNotIn("authorization", serialized)

    def test_write_report_outputs_json_and_markdown_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            runlog_path = Path(tmp_dir) / "runlog.jsonl"
            report_dir = Path(tmp_dir) / "report"
            self._write_report_fixture(runlog_path)

            result = write_weixin_author_batch_report(load_runlog_entries([runlog_path]), report_dir)

            self.assertIsNotNone(result.json_path)
            self.assertIsNotNone(result.markdown_path)
            assert result.json_path is not None
            assert result.markdown_path is not None
            self.assertTrue(result.json_path.exists())
            self.assertTrue(result.markdown_path.exists())
            markdown = result.markdown_path.read_text(encoding="utf-8")
            self.assertIn("## Recovered items", markdown)
            self.assertIn("## Missing items", markdown)
            payload = json.loads(result.json_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["summary"]["total_items"], 4)

    def test_cli_builds_weixin_author_batch_report_from_runlog(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            runlog_path = Path(tmp_dir) / "runlog.jsonl"
            report_dir = Path(tmp_dir) / "report"
            self._write_report_fixture(runlog_path)

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = cli_main(
                    [
                        "report",
                        "weixin-author-batch",
                        "--runlog",
                        str(runlog_path),
                        "--out",
                        str(report_dir),
                    ]
                )

            self.assertEqual(exit_code, 0)
            payload = json.loads(stdout.getvalue())
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["command"], "report")
            self.assertEqual(payload["item_count"], 4)
            self.assertEqual(payload["success_count"], 2)
            self.assertEqual(payload["failed_count"], 2)
            outputs = {Path(path).name for path in payload["outputs"]}
            self.assertEqual(
                outputs,
                {"weixin-author-batch-report.json", "weixin-author-batch-report.md"},
            )
            report_payload = json.loads((report_dir / "weixin-author-batch-report.json").read_text(encoding="utf-8"))
            self.assertEqual(report_payload["summary"]["recovered_count"], 1)
            self.assertEqual(report_payload["summary"]["missing_count"], 1)

    def _write_report_fixture(self, runlog_path: Path) -> None:
        rows = [
            self._entry(
                "https://mp.weixin.qq.com/s/recovered?token=secret&view=1",
                backend="weixin.article.local",
                status="failed",
                source_level="partial_extraction",
                failure_class="source_unavailable",
                error_code="weixin_source_empty",
                error_summary="local cache empty",
            ),
            self._entry(
                "https://mp.weixin.qq.com/s/recovered?token=secret&view=1",
                backend=WECHAT_ARTICLE_EXPORTER_DOWNLOAD_BACKEND,
                status="verified",
                source_level="third_party_extractor",
                output_path=runlog_path.parent / "recovered.md",
                package_id="pkg-recovered",
                package_operation="created",
            ),
            self._entry(
                "https://mp.weixin.qq.com/s/covered",
                backend="weixin.article.local",
                status="verified",
                source_level="primary",
                output_path=runlog_path.parent / "covered.md",
            ),
            self._entry(
                "https://mp.weixin.qq.com/s/missing",
                backend=WECHAT_ARTICLE_EXPORTER_DOWNLOAD_BACKEND,
                status="failed",
                source_level="partial_extraction",
                failure_class="source_unavailable",
                error_code="weixin_source_empty",
                error_summary="exporter returned empty result",
            ),
            self._entry(
                "https://mp.weixin.qq.com/s/auth",
                backend=WECHAT_ARTICLE_EXPORTER_DOWNLOAD_BACKEND,
                status="failed",
                source_level="partial_extraction",
                failure_class="user_resolvable",
                error_code="auth_required",
                error_summary="cookie=abc token=secret authorization=Bearer xyz",
            ),
            build_runlog_entry(
                input_url="https://www.youtube.com/watch?v=ignored",
                platform="youtube",
                backend="mock",
                status="verified",
                source_type="video",
                source_level="primary",
            ),
        ]
        for row in rows:
            append_runlog_entry(runlog_path, row)

    def _entry(
        self,
        input_url: str,
        *,
        backend: str,
        status: str,
        source_level: str,
        failure_class: str | None = None,
        error_code: str | None = None,
        error_summary: str | None = None,
        output_path: Path | None = None,
        package_id: str | None = None,
        package_operation: str | None = None,
    ) -> dict:
        return build_runlog_entry(
            input_url=input_url,
            platform="weixin",
            backend=backend,
            status=status,
            source_type="article",
            source_level=source_level,
            output_path=output_path,
            package_id=package_id,
            package_operation=package_operation,
            failure_class=failure_class,
            error_code=error_code,
            error_summary=error_summary,
        )


if __name__ == "__main__":
    unittest.main()
