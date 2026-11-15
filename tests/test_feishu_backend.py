"""Feishu backend provider regression tests with fake lark-cli runners."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from openmd.backends.base import BackendExtractRequest
from openmd.backends.executor import SkeletonBackendProvider
from openmd.backends.feishu import (
    LARK_CLI_DOCS_FETCH_BACKEND,
    LARK_CLI_DRIVE_EXPORT_MARKDOWN_BACKEND,
    LARK_CLI_MARKDOWN_FETCH_BACKEND,
    MANUAL_MARKDOWN_EXPORT_BACKEND,
    LarkCliDocsFetchProvider,
    LarkCliDriveExportMarkdownProvider,
    LarkCliMarkdownFetchProvider,
    ManualMarkdownExportProvider,
    _run_command,
    build_lark_docs_fetch_args,
    build_lark_drive_export_markdown_args,
    build_lark_drive_inspect_args,
    build_lark_markdown_fetch_args,
)
from openmd.backends.registry import BackendRuntimeOptions, create_runtime_backend_provider_registry
from openmd.core.models import ContentItem
from openmd.core.runlog import append_runlog_entry, build_item_runlog_entry


SENSITIVE_ERROR_TERMS = (
    "cookie",
    "token",
    "authorization",
    "header",
    "raw_html",
    "raw_response",
)


def _feishu_item(url: str = "https://example.feishu.cn/docx/docxfixture") -> ContentItem:
    return ContentItem(
        platform="feishu",
        input_type="single",
        url=url,
        source_type="document",
    )


def _request(item: ContentItem, backend: str) -> BackendExtractRequest:
    return BackendExtractRequest(item=item, backend_chain=(backend,), allow_fallback=False)


class FeishuRuntimeGateTests(unittest.TestCase):
    def test_real_lark_cli_provider_stays_unregistered_without_network_gate(self) -> None:
        registry = create_runtime_backend_provider_registry("feishu", BackendRuntimeOptions())

        provider = registry.provider_for(LARK_CLI_DOCS_FETCH_BACKEND)
        manual_provider = registry.provider_for(MANUAL_MARKDOWN_EXPORT_BACKEND)

        self.assertIsInstance(provider, SkeletonBackendProvider)
        self.assertNotIsInstance(provider, LarkCliDocsFetchProvider)
        self.assertIsInstance(manual_provider, ManualMarkdownExportProvider)

    def test_network_gate_registers_lark_cli_docs_fetch_provider(self) -> None:
        registry = create_runtime_backend_provider_registry(
            "feishu",
            BackendRuntimeOptions(enable_network_backends=True),
        )

        provider = registry.provider_for(LARK_CLI_DOCS_FETCH_BACKEND)

        self.assertIsInstance(provider, LarkCliDocsFetchProvider)


class FeishuCommandRunnerTests(unittest.TestCase):
    def test_run_command_resolves_lark_cli_cmd_for_windows_subprocess(self) -> None:
        calls: list[list[str]] = []

        def fake_which(name: str) -> str | None:
            if name == "lark-cli.cmd":
                return "C:\\tools\\node\\lark-cli.cmd"
            if name == "lark-cli":
                return "C:\\tools\\node\\lark-cli"
            return None

        def fake_run(args: list[str], **kwargs: object) -> object:
            calls.append(args)
            self.assertEqual(kwargs["timeout"], 3)
            return SimpleNamespace(returncode=0, stdout="lark-cli version 1.0.49", stderr="")

        with patch("openmd.backends.feishu.shutil.which", side_effect=fake_which), patch(
            "openmd.backends.feishu.run_interruptibly",
            side_effect=fake_run,
        ):
            exit_code, stdout, stderr = _run_command(["lark-cli", "--version"], timeout=3)

        self.assertEqual(exit_code, 0)
        self.assertEqual(stdout, "lark-cli version 1.0.49")
        self.assertEqual(stderr, "")
        self.assertEqual(calls, [["C:\\tools\\node\\lark-cli.cmd", "--version"]])


class FeishuDocsFetchProviderTests(unittest.TestCase):
    def test_docs_fetch_command_flags_and_success_result(self) -> None:
        calls: list[tuple[list[str], int]] = []

        def fake_runner(args: list[str], *, timeout: int = 120) -> tuple[int, str, str]:
            calls.append((args, timeout))
            return (
                0,
                json.dumps(
                    {
                        "ok": True,
                        "data": {
                            "document": {
                                "title": "Fixture Doc",
                                "content": "# Fixture Doc\n\nRaw Markdown body.",
                            }
                        },
                    }
                ),
                "",
            )

        provider = LarkCliDocsFetchProvider(runner=fake_runner, timeout=7)
        item = _feishu_item()

        result = provider.extract(_request(item, LARK_CLI_DOCS_FETCH_BACKEND))

        self.assertEqual(calls, [(build_lark_docs_fetch_args(item.url), 7)])
        self.assertTrue(result.has_usable_text)
        self.assertEqual(result.status, "verified")
        self.assertEqual(result.source_level, "native_text")
        self.assertEqual(result.backend, LARK_CLI_DOCS_FETCH_BACKEND)
        self.assertEqual(result.title, "Fixture Doc")
        self.assertIn("Raw Markdown body.", result.text)
        self.assertIn("Feishu native Markdown fetched through lark-cli docs +fetch.", result.source_notes)

    def test_wiki_inspect_docx_continues_to_docs_fetch(self) -> None:
        calls: list[tuple[list[str], int]] = []

        def fake_runner(args: list[str], *, timeout: int = 120) -> tuple[int, str, str]:
            calls.append((args, timeout))
            if args[:3] == ["lark-cli", "drive", "+inspect"]:
                return (
                    0,
                    json.dumps({"ok": True, "data": {"type": "docx", "token": "docxTOKEN", "title": "Wiki Doc"}}),
                    "",
                )
            if args[:3] == ["lark-cli", "docs", "+fetch"]:
                return (
                    0,
                    json.dumps({"ok": True, "data": {"document": {"content": "# Wiki Doc\n\nBody"}}}),
                    "",
                )
            return 99, "", "unexpected command"

        provider = LarkCliDocsFetchProvider(runner=fake_runner, timeout=9)
        item = _feishu_item("https://example.feishu.cn/wiki/wikfixture")

        result = provider.extract(_request(item, LARK_CLI_DOCS_FETCH_BACKEND))

        self.assertTrue(result.has_usable_text)
        self.assertEqual(result.title, "Wiki Doc")
        self.assertEqual(result.metadata["inspected"], True)
        self.assertEqual(
            calls,
            [
                (build_lark_drive_inspect_args(item.url), 9),
                (build_lark_docs_fetch_args("docxTOKEN"), 9),
            ],
        )

    def test_wiki_inspect_unsupported_object_returns_structured_failure(self) -> None:
        for object_type in ("bitable", "sheet"):
            with self.subTest(object_type=object_type):
                calls: list[list[str]] = []

                def fake_runner(args: list[str], *, timeout: int = 120) -> tuple[int, str, str]:
                    calls.append(args)
                    return (
                        0,
                        json.dumps({"ok": True, "data": {"type": object_type, "token": "unsupported"}}),
                        "",
                    )

                provider = LarkCliDocsFetchProvider(runner=fake_runner)
                item = _feishu_item(f"https://example.feishu.cn/wiki/{object_type}fixture")

                result = provider.extract(_request(item, LARK_CLI_DOCS_FETCH_BACKEND))

                self.assertFalse(result.has_usable_text)
                self.assertEqual(result.status, "failed")
                self.assertEqual(result.source_level, "partial_extraction")
                self.assertEqual(result.error_code, "unsupported_feishu_object")
                self.assertEqual(result.metadata["object_type"], object_type)
                self.assertEqual(len(calls), 1)

    def test_lark_cli_missing_and_nonzero_failures_do_not_leak_raw_streams(self) -> None:
        item = _feishu_item()

        def missing_runner(args: list[str], *, timeout: int = 120) -> tuple[int, str, str]:
            return 127, "stdout cookie token", "ENOENT authorization header raw_response"

        missing = LarkCliDocsFetchProvider(runner=missing_runner).extract(
            _request(item, LARK_CLI_DOCS_FETCH_BACKEND)
        )
        self.assertEqual(missing.error_code, "backend_dependency_missing")
        self._assert_no_sensitive_error_terms(missing.error_summary)

        def failing_runner(args: list[str], *, timeout: int = 120) -> tuple[int, str, str]:
            return 23, "stdout cookie token", "authorization header raw_html raw_response"

        failed = LarkCliDocsFetchProvider(runner=failing_runner).extract(
            _request(item, LARK_CLI_DOCS_FETCH_BACKEND)
        )
        self.assertEqual(failed.error_code, "backend_exit_23")
        self.assertEqual(failed.metadata, {"exit_code": 23})
        self._assert_no_sensitive_error_terms(failed.error_summary)

    def test_runlog_error_summary_does_not_persist_sensitive_raw_stream_terms(self) -> None:
        def failing_runner(args: list[str], *, timeout: int = 120) -> tuple[int, str, str]:
            return 23, "stdout cookie token", "authorization header raw_html raw_response"

        item = _feishu_item()
        result = LarkCliDocsFetchProvider(runner=failing_runner).extract(
            _request(item, LARK_CLI_DOCS_FETCH_BACKEND)
        )
        entry = build_item_runlog_entry(item, result)
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "runlog.jsonl"
            append_runlog_entry(path, entry)
            runlog_text = path.read_text(encoding="utf-8").lower()

        for term in SENSITIVE_ERROR_TERMS:
            self.assertNotIn(term, runlog_text)

    def _assert_no_sensitive_error_terms(self, value: str | None) -> None:
        text = (value or "").lower()
        for term in SENSITIVE_ERROR_TERMS:
            self.assertNotIn(term, text)


class FeishuFallbackProviderTests(unittest.TestCase):
    def test_drive_export_fallback_reads_temp_markdown_and_cleans_directory(self) -> None:
        calls: list[tuple[list[str], int]] = []
        export_dirs: list[Path] = []

        def fake_runner(
            args: list[str],
            *,
            timeout: int = 120,
            cwd: str | Path | None = None,
        ) -> tuple[int, str, str]:
            calls.append((args, timeout))
            if args[:3] == ["lark-cli", "drive", "+inspect"]:
                return (
                    0,
                    json.dumps({"ok": True, "data": {"type": "docx", "token": "docxTOKEN", "title": "Export Doc"}}),
                    "",
                )
            if args[:3] == ["lark-cli", "drive", "+export"]:
                self.assertIsNotNone(cwd)
                output_dir = Path(cwd or ".") / args[args.index("--output-dir") + 1]
                export_dirs.append(output_dir)
                exported_path = output_dir / "exported.md"
                exported_path.write_text("# Export Doc\n\nExported Markdown body.", encoding="utf-8")
                return 0, json.dumps({"ok": True, "data": {"saved_path": str(exported_path)}}), ""
            return 99, "", "unexpected command"

        provider = LarkCliDriveExportMarkdownProvider(runner=fake_runner, timeout=11)
        item = _feishu_item("https://example.feishu.cn/docx/docxfixture")

        result = provider.extract(_request(item, LARK_CLI_DRIVE_EXPORT_MARKDOWN_BACKEND))

        self.assertTrue(result.has_usable_text)
        self.assertEqual(result.status, "verified")
        self.assertEqual(result.source_level, "native_text")
        self.assertEqual(result.backend, LARK_CLI_DRIVE_EXPORT_MARKDOWN_BACKEND)
        self.assertIn("Exported Markdown body.", result.text)
        self.assertEqual(len(export_dirs), 1)
        self.assertFalse(export_dirs[0].exists())
        self.assertEqual(calls[0], (build_lark_drive_inspect_args(item.url), 11))
        self.assertEqual(
            calls[1][0],
            build_lark_drive_export_markdown_args(
                token="docxTOKEN",
                doc_type="docx",
                output_dir=".",
            ),
        )

    def test_markdown_fetch_fallback_parses_drive_native_markdown_content(self) -> None:
        calls: list[tuple[list[str], int]] = []

        def fake_runner(args: list[str], *, timeout: int = 120) -> tuple[int, str, str]:
            calls.append((args, timeout))
            return (
                0,
                json.dumps(
                    {
                        "ok": True,
                        "data": {
                            "file_token": "boxTOKEN",
                            "file_name": "README.md",
                            "content": "# Drive Markdown\n\nNative file body.",
                        },
                    }
                ),
                "",
            )

        provider = LarkCliMarkdownFetchProvider(runner=fake_runner, timeout=13)
        item = ContentItem(
            platform="feishu",
            input_type="single",
            url="boxTOKEN",
            source_type="document",
            raw_meta={"file_token": "boxTOKEN"},
        )

        result = provider.extract(_request(item, LARK_CLI_MARKDOWN_FETCH_BACKEND))

        self.assertTrue(result.has_usable_text)
        self.assertEqual(result.status, "verified")
        self.assertEqual(result.source_level, "native_text")
        self.assertEqual(result.backend, LARK_CLI_MARKDOWN_FETCH_BACKEND)
        self.assertEqual(result.title, "README.md")
        self.assertIn("Native file body.", result.text)
        self.assertEqual(calls, [(build_lark_markdown_fetch_args("boxTOKEN"), 13)])


class FeishuDefaultBackendChainTests(unittest.TestCase):
    def test_default_feishu_chain_matches_v0_fallback_order(self) -> None:
        from openmd.adapters.registry import DEFAULT_BACKENDS

        self.assertEqual(
            DEFAULT_BACKENDS["feishu"],
            (
                LARK_CLI_DOCS_FETCH_BACKEND,
                LARK_CLI_DRIVE_EXPORT_MARKDOWN_BACKEND,
                LARK_CLI_MARKDOWN_FETCH_BACKEND,
                MANUAL_MARKDOWN_EXPORT_BACKEND,
            ),
        )


if __name__ == "__main__":
    unittest.main()
