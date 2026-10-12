from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from openmd.adapters.bilibili.adapter import BilibiliAdapter
from openmd.adapters.bilibili.discovery import build_opencli_bilibili_user_videos_args
from openmd.adapters.registry import create_default_registry
from openmd.backends.base import DiscoveryRequest
from openmd.backends.bilibili import (
    BilibiliNativeUserVideosProvider,
    OpenCliBilibiliUserVideosProvider,
)
from openmd.backends.executor import SkeletonDiscoveryProvider
from openmd.backends.registry import BackendRuntimeOptions, create_runtime_discovery_provider_registry
from openmd.cli import main as cli_main
from openmd.core.package_validator import validate_package


class BilibiliAdapterTests(unittest.TestCase):
    def test_registry_loads_bilibili_adapter_without_core_registry_edits(self) -> None:
        registry = create_default_registry()
        adapter = registry.adapter_for("https://www.bilibili.com/video/BV1abc123456/")

        self.assertIsInstance(adapter, BilibiliAdapter)
        self.assertEqual(adapter.id, "bilibili")

    def test_resolves_bv_as_media_video_item(self) -> None:
        adapter = BilibiliAdapter()

        item = adapter.resolve_items("BV1abc123456")[0]

        self.assertEqual(item.platform, "bilibili")
        self.assertEqual(item.input_type, "single")
        self.assertEqual(item.source_type, "video")
        self.assertEqual(item.id, "BV1abc123456")
        self.assertEqual(item.raw_meta["profile"], "media")
        self.assertEqual(item.raw_meta["permission_scope"], "user_accessible")

    def test_resolves_space_url_as_collection_selector_without_discovery(self) -> None:
        adapter = BilibiliAdapter()

        item = adapter.resolve_items("https://space.bilibili.com/441831884", limit=5)[0]

        self.assertEqual(item.platform, "bilibili")
        self.assertEqual(item.input_type, "collection")
        self.assertEqual(item.source_type, "video")
        self.assertEqual(item.id, "space-441831884")
        self.assertEqual(item.raw_meta["up_uid"], "441831884")
        self.assertEqual(item.raw_meta["discovery_limit"], 5)

    def test_mock_package_smoke_for_bv_writes_media_package(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = cli_main(
                    [
                        "ingest",
                        "https://www.bilibili.com/video/BV1abc123456/?token=secret&view=1",
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
            self.assertTrue((package_path / "transcript.md").exists())
            self.assertEqual(list((package_path / "assets").iterdir()), [])
            self.assertNotIn("token=secret", combined)
            self.assertNotIn("?token=", combined)


class BilibiliDiscoveryGateTests(unittest.TestCase):
    def test_discovery_providers_are_runtime_gated(self) -> None:
        request = DiscoveryRequest(
            input_url="https://space.bilibili.com/441831884",
            platform="bilibili",
            limit=2,
        )
        safe = create_runtime_discovery_provider_registry("bilibili", BackendRuntimeOptions())
        network = create_runtime_discovery_provider_registry(
            "bilibili",
            BackendRuntimeOptions(enable_network_backends=True),
        )
        browser = create_runtime_discovery_provider_registry(
            "bilibili",
            BackendRuntimeOptions(enable_browser_backends=True),
        )

        self.assertIsInstance(safe.provider_for(request), SkeletonDiscoveryProvider)
        self.assertIsInstance(network.provider_for(request), BilibiliNativeUserVideosProvider)
        self.assertIsInstance(browser.provider_for(request), OpenCliBilibiliUserVideosProvider)

    def test_builds_opencli_user_videos_args_with_limit(self) -> None:
        self.assertEqual(
            build_opencli_bilibili_user_videos_args(
                "https://space.bilibili.com/441831884",
                limit=3,
            ),
            ["opencli", "bilibili", "user-videos", "441831884", "--limit", "3"],
        )

    def test_safe_collect_skeleton_smoke_does_not_run_real_bilibili_backends(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = cli_main(
                    [
                        "collect",
                        "https://space.bilibili.com/441831884",
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


if __name__ == "__main__":
    unittest.main()
