from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from openmd.adapters.douyin.adapter import DouyinAdapter
from openmd.adapters.douyin.sidecar import build_sidecar_url, is_allowed_sidecar_endpoint
from openmd.adapters.registry import create_default_registry
from openmd.backends.base import DiscoveryRequest
from openmd.backends.douyin import OpenCliDouyinUserVideosProvider, build_opencli_douyin_user_videos_args
from openmd.backends.executor import SkeletonDiscoveryProvider
from openmd.backends.registry import BackendRuntimeOptions, create_runtime_discovery_provider_registry
from openmd.cli import main as cli_main
from openmd.core.package_validator import validate_package


SEC_UID = "MS4wLjABAAAAfixtureSecUid"


class DouyinAdapterTests(unittest.TestCase):
    def test_registry_loads_douyin_adapter_without_core_registry_edits(self) -> None:
        registry = create_default_registry()
        adapter = registry.adapter_for(f"https://www.douyin.com/user/{SEC_UID}")

        self.assertIsInstance(adapter, DouyinAdapter)
        self.assertEqual(adapter.id, "douyin")
        self.assertEqual(registry.backend_chain("douyin"), ("douyin.metadata",))

    def test_resolves_user_url_as_collection_selector_without_discovery(self) -> None:
        adapter = DouyinAdapter()

        item = adapter.resolve_items(f"https://www.douyin.com/user/{SEC_UID}", limit=5)[0]

        self.assertEqual(item.platform, "douyin")
        self.assertEqual(item.input_type, "collection")
        self.assertEqual(item.source_type, "video")
        self.assertEqual(item.id, f"user-{SEC_UID}")
        self.assertEqual(item.raw_meta["sec_uid"], SEC_UID)
        self.assertEqual(item.raw_meta["discovery_limit"], 5)

    def test_resolves_single_video_or_modal_id_as_media_item(self) -> None:
        adapter = DouyinAdapter()

        item = adapter.resolve_items("https://www.douyin.com/jingxuan?modal_id=7652295009652490374")[0]

        self.assertEqual(item.platform, "douyin")
        self.assertEqual(item.input_type, "single")
        self.assertEqual(item.source_type, "video")
        self.assertEqual(item.id, "7652295009652490374")
        self.assertEqual(item.url, "https://www.douyin.com/video/7652295009652490374")

    def test_mock_package_smoke_for_single_video_writes_media_package(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = cli_main(
                    [
                        "ingest",
                        "https://www.douyin.com/jingxuan?modal_id=7652295009652490374&token=secret",
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


class DouyinDiscoveryAndSidecarGateTests(unittest.TestCase):
    def test_discovery_provider_is_browser_gated(self) -> None:
        request = DiscoveryRequest(
            input_url=f"https://www.douyin.com/user/{SEC_UID}",
            platform="douyin",
            limit=2,
        )
        safe = create_runtime_discovery_provider_registry("douyin", BackendRuntimeOptions())
        browser = create_runtime_discovery_provider_registry(
            "douyin",
            BackendRuntimeOptions(enable_browser_backends=True),
        )

        self.assertIsInstance(safe.provider_for(request), SkeletonDiscoveryProvider)
        self.assertIsInstance(browser.provider_for(request), OpenCliDouyinUserVideosProvider)

    def test_builds_bounded_opencli_user_videos_args(self) -> None:
        self.assertEqual(
            build_opencli_douyin_user_videos_args(SEC_UID, limit=99),
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
        )

    def test_sidecar_allowlist_blocks_account_and_write_endpoints(self) -> None:
        self.assertTrue(is_allowed_sidecar_endpoint("GET", "/api/v1/monitors"))
        self.assertTrue(is_allowed_sidecar_endpoint("GET", "/api/v1/contents"))
        self.assertTrue(is_allowed_sidecar_endpoint("GET", "/api/v1/contents/123"))
        self.assertTrue(is_allowed_sidecar_endpoint("GET", "/api/v1/contents/123/comments"))
        self.assertFalse(is_allowed_sidecar_endpoint("POST", "/api/v1/contents/123"))
        self.assertFalse(is_allowed_sidecar_endpoint("POST", "/api/v1/contents/123/comments"))
        self.assertFalse(is_allowed_sidecar_endpoint("GET", "/api/v1/contents/123/comments/reply"))
        self.assertFalse(is_allowed_sidecar_endpoint("GET", "/api/v1/accounts"))
        self.assertFalse(is_allowed_sidecar_endpoint("GET", "/api/v1/admin/users"))
        self.assertFalse(is_allowed_sidecar_endpoint("GET", "/api/v1/creator/upload"))
        self.assertFalse(is_allowed_sidecar_endpoint("POST", "/auth/login"))
        self.assertFalse(is_allowed_sidecar_endpoint("POST", "/api/v1/contents/123/upload"))
        self.assertEqual(
            build_sidecar_url("http://127.0.0.1:8080/", "/api/v1/contents/123?token=secret"),
            "http://127.0.0.1:8080/api/v1/contents/123",
        )

    def test_safe_collect_skeleton_smoke_does_not_run_real_douyin_backends(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = cli_main(
                    [
                        "collect",
                        f"https://www.douyin.com/user/{SEC_UID}",
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
