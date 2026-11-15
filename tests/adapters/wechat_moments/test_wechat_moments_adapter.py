from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from openmd.adapters.registry import create_default_registry
from openmd.adapters.wechat_moments.adapter import WechatMomentsAdapter
from openmd.adapters.wechat_moments.wx_cli import (
    OPENCLI_WX_SNS_FEED_DISCOVERY_BACKEND,
    OpenCliWechatMomentsProvider,
    build_opencli_wx_sns_feed_args,
    parse_opencli_wx_sns_feed_output,
)
from openmd.backends.base import DiscoveryRequest
from openmd.backends.executor import SkeletonDiscoveryProvider
from openmd.backends.registry import BackendRuntimeOptions, create_runtime_discovery_provider_registry
from openmd.cli import main as cli_main
from openmd.core.detector import detect_platform
from openmd.core.package_validator import validate_package


PERSON_SELECTOR = "wx://moments/Alice?since=2026-06-01&until=2026-06-23"


class WechatMomentsAdapterTests(unittest.TestCase):
    def test_detector_and_registry_load_wechat_moments_adapter(self) -> None:
        registry = create_default_registry()
        adapter = registry.adapter_for(PERSON_SELECTOR)

        self.assertEqual(detect_platform(PERSON_SELECTOR), "wechat_moments")
        self.assertIsInstance(adapter, WechatMomentsAdapter)
        self.assertEqual(adapter.id, "wechat_moments")
        self.assertEqual(registry.backend_chain("wechat_moments"), ("opencli.wx.sns-feed",))

    def test_resolves_explicit_person_selector_without_session_access(self) -> None:
        adapter = WechatMomentsAdapter()

        item = adapter.resolve_items(PERSON_SELECTOR, limit=5)[0]

        self.assertEqual(item.platform, "wechat_moments")
        self.assertEqual(item.input_type, "collection")
        self.assertEqual(item.source_type, "conversation")
        self.assertEqual(item.raw_meta["person"], "Alice")
        self.assertEqual(item.raw_meta["since"], "2026-06-01")
        self.assertEqual(item.raw_meta["until"], "2026-06-23")
        self.assertEqual(item.raw_meta["permission_scope"], "user_accessible")

    def test_broad_person_selector_is_rejected(self) -> None:
        adapter = WechatMomentsAdapter()

        with self.assertRaises(ValueError):
            adapter.resolve_items("wx://moments/all")

    def test_mock_package_smoke_writes_conversation_package(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = cli_main(
                    [
                        "ingest",
                        PERSON_SELECTOR,
                        "--out",
                        tmp_dir,
                        "--backend",
                        "mock",
                        "--package",
                    ]
                )

            payload = json.loads(stdout.getvalue())
            package_path = Path(payload["outputs"][0])
            metadata = json.loads((package_path / "metadata.json").read_text(encoding="utf-8"))

            self.assertEqual(exit_code, 0)
            self.assertTrue(payload["ok"])
            self.assertTrue(validate_package(package_path).ok)
            self.assertEqual(metadata["profile"], "conversation")
            self.assertEqual(metadata["source_platform"], "wechat_moments")
            self.assertEqual(list((package_path / "assets").iterdir()), [])

    def test_default_real_path_reports_refresh_needed_without_session_ownership(self) -> None:
        adapter = WechatMomentsAdapter()
        item = adapter.resolve_items(PERSON_SELECTOR)[0]

        result = adapter.extract_text(item, backend="opencli.wx.sns-feed", allow_fallback=False)

        status = result.metadata["high_privacy_status"]
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.error_code, "refresh_needed")
        self.assertEqual(result.metadata["failure_class"], "user_resolvable")
        self.assertEqual(status["state"], "refresh_needed")
        self.assertFalse(status["core_owns_session"])
        self.assertFalse(status["reads_private_database"])
        self.assertFalse(status["decrypts_database"])


class WechatMomentsDiscoveryProviderTests(unittest.TestCase):
    def test_discovery_provider_is_browser_gated(self) -> None:
        request = DiscoveryRequest(input_url=PERSON_SELECTOR, platform="wechat_moments", limit=2)
        safe = create_runtime_discovery_provider_registry("wechat_moments", BackendRuntimeOptions())
        browser = create_runtime_discovery_provider_registry(
            "wechat_moments",
            BackendRuntimeOptions(enable_browser_backends=True),
        )

        self.assertIsInstance(safe.provider_for(request), SkeletonDiscoveryProvider)
        self.assertIsInstance(browser.provider_for(request), OpenCliWechatMomentsProvider)

    def test_builds_opencli_wx_sns_feed_command_with_scope(self) -> None:
        self.assertEqual(
            build_opencli_wx_sns_feed_args(
                "Alice",
                since="2026-06-01",
                until="2026-06-23",
                limit=3,
            ),
            [
                "opencli",
                "wx",
                "sns-feed",
                "--user",
                "Alice",
                "--json",
                "--since",
                "2026-06-01",
                "--until",
                "2026-06-23",
                "--limit",
                "3",
            ],
        )

    def test_parses_common_opencli_moments_shapes(self) -> None:
        posts = parse_opencli_wx_sns_feed_output(
            json.dumps(
                {
                    "data": {
                        "posts": [
                            {
                                "sns_id": "p1",
                                "time": "2026-06-23T12:00:00+08:00",
                                "author": "Alice",
                                "content": "moment text",
                            },
                            {"author": "Alice", "media": [{"name": "photo.jpg"}]},
                        ]
                    }
                }
            )
        )

        self.assertEqual(len(posts), 2)
        self.assertEqual(posts[0]["id"], "p1")
        self.assertEqual(posts[0]["speaker"], "Alice")
        self.assertEqual(posts[0]["text"], "moment text")
        self.assertEqual(posts[1]["attachments"], [{"name": "photo.jpg"}])

    def test_fake_runner_returns_one_scoped_person_feed_item(self) -> None:
        calls: list[tuple[tuple[str, ...], int]] = []

        def fake_runner(command: list[str], timeout: int) -> tuple[int, str, str]:
            calls.append((tuple(command), timeout))
            return (
                0,
                json.dumps(
                    [
                        {"id": "p1", "timestamp": "2026-06-23T12:00:00+08:00", "speaker": "Alice", "text": "one"},
                        {"id": "p2", "timestamp": "2026-06-23T12:01:00+08:00", "speaker": "Alice", "text": "two"},
                    ]
                ),
                "",
            )

        provider = OpenCliWechatMomentsProvider(runner=fake_runner, timeout=7)
        result = provider.discover(
            DiscoveryRequest(input_url=PERSON_SELECTOR, platform="wechat_moments", limit=1)
        )

        self.assertTrue(result.ok, result.error_summary)
        self.assertEqual(result.provider, OPENCLI_WX_SNS_FEED_DISCOVERY_BACKEND)
        self.assertEqual(len(result.items), 1)
        self.assertEqual(result.items[0].platform, "wechat_moments")
        self.assertEqual(result.items[0].source_type, "conversation")
        self.assertEqual(len(result.items[0].raw_meta["messages"]), 1)
        self.assertEqual(calls[0][1], 7)

    def test_missing_opencli_returns_structured_blocker(self) -> None:
        provider = OpenCliWechatMomentsProvider(
            runner=lambda _command, _timeout: (127, "", "missing executable")
        )

        result = provider.discover(
            DiscoveryRequest(input_url=PERSON_SELECTOR, platform="wechat_moments", limit=2)
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "discovery_tool_missing")
        self.assertIn("missing executable", result.error_summary or "")

    def test_empty_opencli_result_reports_no_delta_status(self) -> None:
        provider = OpenCliWechatMomentsProvider(
            runner=lambda _command, _timeout: (0, "[]", "")
        )

        result = provider.discover(
            DiscoveryRequest(input_url=PERSON_SELECTOR, platform="wechat_moments", limit=2)
        )

        status = result.metadata["high_privacy_status"]
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "no_delta")
        self.assertEqual(status["state"], "no_delta")
        self.assertFalse(status["core_owns_session"])
        self.assertFalse(status["reads_private_database"])

    def test_safe_collect_skeleton_smoke_does_not_run_real_moments(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = cli_main(
                    [
                        "collect",
                        PERSON_SELECTOR,
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
