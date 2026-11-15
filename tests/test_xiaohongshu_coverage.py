from __future__ import annotations

import argparse
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import openmd.cli as cli_module
from openmd.backends.base import DiscoveryResult
from openmd.backends.executor import BackendExecutor, BackendProviderRegistry
from openmd.backends.registry import BackendRuntimeOptions
from openmd.backends.xiaohongshu_providers import (
    XIAOHONGSHU_COMPOSITE_BACKEND,
    XiaohongshuCompositeProvider,
)
from openmd.cli import CliResult, _write_xiaohongshu_coverage_summary, main as cli_main
from openmd.core.coverage import CollectionCoverage
from openmd.core.models import ContentItem, ExtractionComponent, TranscriptResult


def _item(index: int, kind: str) -> ContentItem:
    return ContentItem(
        platform="xiaohongshu",
        input_type="collection",
        url=f"https://www.xiaohongshu.com/discovery/item/{index:024x}",
        id=f"{index:024x}",
        source_type="article" if kind == "text" else kind,
        raw_meta={"media_kind": kind},
    )


def _coverage_payload() -> dict[str, object]:
    coverage = CollectionCoverage()
    coverage.record_result(
        TranscriptResult(
            text="native and image text",
            status="partial",
            source_level="native_text",
            backend="fixture",
            components=[
                ExtractionComponent(
                    "native_text", "native_text", "verified", "native_text", "fixture"
                ),
                ExtractionComponent(
                    "image_ocr",
                    "image_ocr",
                    "unavailable",
                    "ocr",
                    "fixture.ocr",
                    terminal_reason="source image unavailable",
                    error_code="image_unavailable",
                    failure_class="source_unavailable",
                ),
            ],
        )
    )
    coverage.record_metadata(
        {
            "status": "verified",
            "components": [
                {
                    "id": "native_text",
                    "status": "verified",
                    "source_level": "native_text",
                },
                {
                    "id": "video_transcript",
                    "status": "verified",
                    "source_level": "media_asr",
                },
            ],
        },
        skipped_existing=True,
    )
    coverage.record(
        status="failed",
        components=[
            {
                "id": "video_transcript",
                "status": "retryable",
                "source_level": "partial_extraction",
            }
        ],
    )
    return coverage.as_dict()


class XiaohongshuCoverageTests(unittest.TestCase):
    def test_full_collect_not_found_is_complete_with_terminal_gaps(self) -> None:
        item = _item(1, "mixed")
        discovery = DiscoveryResult.success(
            provider="fixture.xiaohongshu.discovery",
            items=[item],
            metadata={
                "normalized_url": "https://www.xiaohongshu.com/user/profile/fixture",
                "source_exhausted": True,
                "explicit_exhaustion": True,
                "page_count": 1,
                "scroll_count": 1,
                "anomaly_count": 0,
            },
        )
        provider = XiaohongshuCompositeProvider(
            BackendRuntimeOptions(enable_browser_backends=True),
            runner=lambda _args, _timeout, _selector: (1, "", "note not found 404"),
        )
        registry = BackendProviderRegistry(allow_skeleton_fallback=False)
        registry.register(XIAOHONGSHU_COMPOSITE_BACKEND, provider)
        executor = BackendExecutor(provider_registry=registry)

        with tempfile.TemporaryDirectory() as tmp_dir:
            stdout = io.StringIO()
            with (
                mock.patch.object(
                    cli_module,
                    "_build_discovery_executor",
                    return_value=SimpleNamespace(discover=lambda _request: discovery),
                ),
                mock.patch.object(
                    cli_module,
                    "_build_backend_executor",
                    return_value=executor,
                ),
                contextlib.redirect_stdout(stdout),
            ):
                exit_code = cli_main(
                    [
                        "collect",
                        "https://www.xiaohongshu.com/user/profile/fixture",
                        "--out",
                        tmp_dir,
                        "--all",
                        "--package",
                        "--enable-browser-backends",
                        "--backend",
                        XIAOHONGSHU_COMPOSITE_BACKEND,
                    ]
                )
            summary = json.loads(
                (Path(tmp_dir) / "coverage-summary.json").read_text(encoding="utf-8")
            )

        self.assertEqual(exit_code, 0, stdout.getvalue())
        self.assertEqual(summary["outcome"], "complete_with_gaps")
        self.assertEqual(summary["terminal_gaps"], 3)
        self.assertEqual(summary["terminal_gap_items"], 1)
        self.assertEqual(summary["retryable"], 0)
        self.assertEqual(summary["retryable_items"], 0)
        self.assertEqual(summary["failed"], 0)
        self.assertEqual(summary["remaining"], 0)

    def test_generic_coverage_tracks_items_components_sources_and_resume(self) -> None:
        payload = _coverage_payload()

        self.assertEqual(payload["attempted"], 3)
        self.assertEqual(payload["verified"], 1)
        self.assertEqual(payload["partial"], 1)
        self.assertEqual(payload["failed"], 1)
        self.assertEqual(payload["skipped_existing"], 1)
        self.assertEqual(payload["terminal_gaps"], 1)
        self.assertEqual(payload["terminal_gap_items"], 1)
        self.assertEqual(payload["retryable"], 1)
        self.assertEqual(payload["retryable_items"], 1)
        self.assertEqual(payload["component_counts"]["native_text"]["verified"], 2)
        self.assertEqual(payload["component_counts"]["image_ocr"]["unavailable"], 1)
        self.assertEqual(payload["component_counts"]["video_transcript"]["retryable"], 1)
        self.assertEqual(
            payload["component_source_counts"]["video_transcript"],
            {"asr": 1, "partial_extraction": 1},
        )

    def test_summary_contains_required_counts_and_no_transient_locators(self) -> None:
        items = [_item(1, "text"), _item(2, "image_text"), _item(3, "video"), _item(4, "mixed")]
        with tempfile.TemporaryDirectory() as tmp_dir:
            args = argparse.Namespace(out=tmp_dir, package=True)
            result = CliResult(
                ok=False,
                command="collect",
                item_count=3,
                success_count=2,
                failed_count=1,
                skipped_count=1,
                coverage=_coverage_payload(),
            )
            path = _write_xiaohongshu_coverage_summary(
                args=args,
                result=result,
                discovered_items=items,
                discovery_metadata={
                    "source_exhausted": True,
                    "explicit_exhaustion": True,
                    "page_count": 4,
                    "scroll_count": 7,
                    "anomaly_count": 2,
                    "stable_round_count": 1,
                    "empty_snapshot_count": 1,
                    "duplicate_snapshot_count": 3,
                    "signed_url": "https://media.example/cdn/video?xsec_token=secret",
                    "raw_path": r"C:\private\raw-media\video.mp4",
                },
                start_index=0,
                max_items=3,
            )
            text = path.read_text(encoding="utf-8")
            payload = json.loads(text)

        self.assertEqual(payload["discovered"], 4)
        self.assertEqual(
            [payload[key] for key in ("text", "image_text", "video", "mixed")],
            [1, 1, 1, 1],
        )
        self.assertTrue(payload["source_exhausted"])
        self.assertTrue(payload["explicit_exhaustion"])
        self.assertEqual(payload["page_count"], 4)
        self.assertEqual(payload["scroll_count"], 7)
        self.assertEqual(payload["anomaly_count"], 2)
        self.assertEqual(payload["terminal_gaps"], 1)
        self.assertEqual(payload["retryable"], 1)
        self.assertEqual(payload["failed"], 1)
        self.assertEqual(payload["skipped_existing"], 1)
        self.assertEqual(payload["remaining_unattempted"], 1)
        self.assertEqual(payload["remaining"], 2)
        self.assertIn("native_text", payload["component_counts"])
        self.assertIn("image_ocr", payload["component_counts"])
        self.assertIn("video_transcript", payload["component_counts"])
        for forbidden in ("xsec_token", "media.example", "raw-media", "manifest", "runlog"):
            self.assertNotIn(forbidden, text.lower())

    def test_full_collect_writes_summary_for_markdown_and_package_modes(self) -> None:
        items = [_item(1, "text"), _item(2, "image_text"), _item(3, "video"), _item(4, "mixed")]
        discovery = DiscoveryResult.success(
            provider="fixture.xiaohongshu.discovery",
            items=items,
            metadata={
                "normalized_url": "https://www.xiaohongshu.com/user/profile/fixture",
                "source_exhausted": True,
                "explicit_exhaustion": True,
                "page_count": 2,
                "scroll_count": 3,
                "anomaly_count": 0,
            },
        )
        processed = CliResult(
            ok=True,
            command="collect",
            item_count=4,
            success_count=4,
            coverage={
                "attempted": 4,
                "verified": 4,
                "partial": 0,
                "failed": 0,
                "skipped_existing": 0,
                "terminal_gaps": 0,
                "terminal_gap_items": 0,
                "retryable": 0,
                "retryable_items": 0,
                "component_counts": {},
                "component_source_counts": {},
                "source_counts": {},
            },
        )

        for package_mode in (False, True):
            with self.subTest(package_mode=package_mode), tempfile.TemporaryDirectory() as tmp_dir:
                argv = [
                    "collect",
                    "https://www.xiaohongshu.com/user/profile/fixture",
                    "--out",
                    tmp_dir,
                    "--all",
                    "--enable-browser-backends",
                ]
                if package_mode:
                    argv.append("--package")
                stdout = io.StringIO()
                with (
                    mock.patch.object(
                        cli_module,
                        "_build_discovery_executor",
                        return_value=SimpleNamespace(discover=lambda _request: discovery),
                    ),
                    mock.patch.object(cli_module, "_process_items", return_value=processed),
                    contextlib.redirect_stdout(stdout),
                ):
                    exit_code = cli_main(argv)

                payload = json.loads((Path(tmp_dir) / "coverage-summary.json").read_text(encoding="utf-8"))
                self.assertEqual(exit_code, 0)
                self.assertEqual(payload["package_mode"], package_mode)
                self.assertEqual(payload["outcome"], "verified")
                self.assertEqual(payload["remaining"], 0)


if __name__ == "__main__":
    unittest.main()
