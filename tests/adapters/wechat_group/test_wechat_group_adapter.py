from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from openmd.adapters.registry import create_default_registry
from openmd.adapters.wechat_group.adapter import WechatGroupAdapter
from openmd.adapters.wechat_group.wx_cli import (
    OPENCLI_WX_HISTORY_DISCOVERY_BACKEND,
    OpenCliWechatGroupHistoryProvider,
    build_opencli_wx_history_args,
    parse_opencli_wx_history_output,
)
from openmd.backends.base import DiscoveryRequest
from openmd.backends.executor import SkeletonDiscoveryProvider
from openmd.backends.registry import BackendRuntimeOptions, create_runtime_discovery_provider_registry
from openmd.cli import main as cli_main
from openmd.core.detector import detect_platform
from openmd.core.package_validator import validate_package


GROUP_SELECTOR = "wx://group/Openmd%20Test?since=2026-06-01&until=2026-06-23"


class WechatGroupAdapterTests(unittest.TestCase):
    def test_detector_and_registry_load_wechat_group_adapter(self) -> None:
        registry = create_default_registry()
        adapter = registry.adapter_for(GROUP_SELECTOR)

        self.assertEqual(detect_platform(GROUP_SELECTOR), "wechat_group")
        self.assertIsInstance(adapter, WechatGroupAdapter)
        self.assertEqual(adapter.id, "wechat_group")
        self.assertEqual(registry.backend_chain("wechat_group"), ("opencli.wx.history",))

    def test_resolves_explicit_group_selector_without_session_access(self) -> None:
        adapter = WechatGroupAdapter()

        item = adapter.resolve_items(GROUP_SELECTOR, limit=5)[0]

        self.assertEqual(item.platform, "wechat_group")
        self.assertEqual(item.input_type, "collection")
        self.assertEqual(item.source_type, "conversation")
        self.assertEqual(item.raw_meta["group_name"], "Openmd Test")
        self.assertEqual(item.raw_meta["since"], "2026-06-01")
        self.assertEqual(item.raw_meta["until"], "2026-06-23")
        self.assertEqual(item.raw_meta["permission_scope"], "user_accessible")

    def test_mock_package_smoke_writes_conversation_package(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = cli_main(
                    [
                        "ingest",
                        GROUP_SELECTOR,
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
            combined = "\n".join(
                path.read_text(encoding="utf-8")
                for path in package_path.rglob("*")
                if path.is_file() and path.suffix in {".md", ".json"}
            )

            self.assertEqual(exit_code, 0)
            self.assertTrue(payload["ok"])
            self.assertTrue(validate_package(package_path).ok)
            self.assertEqual(metadata["profile"], "conversation")
            self.assertEqual(metadata["source_platform"], "wechat_group")
            self.assertEqual(list((package_path / "assets").iterdir()), [])
            self.assertNotIn("token=secret", combined)

    def test_default_real_path_reports_refresh_needed_without_session_ownership(self) -> None:
        adapter = WechatGroupAdapter()
        item = adapter.resolve_items(GROUP_SELECTOR)[0]

        result = adapter.extract_text(item, backend="opencli.wx.history", allow_fallback=False)

        status = result.metadata["high_privacy_status"]
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.error_code, "refresh_needed")
        self.assertEqual(result.metadata["failure_class"], "user_resolvable")
        self.assertEqual(status["state"], "refresh_needed")
        self.assertFalse(status["core_owns_session"])
        self.assertFalse(status["reads_private_database"])
        self.assertFalse(status["decrypts_database"])


class WechatGroupDiscoveryProviderTests(unittest.TestCase):
    def test_discovery_provider_is_browser_gated(self) -> None:
        request = DiscoveryRequest(input_url=GROUP_SELECTOR, platform="wechat_group", limit=2)
        safe = create_runtime_discovery_provider_registry("wechat_group", BackendRuntimeOptions())
        browser = create_runtime_discovery_provider_registry(
            "wechat_group",
            BackendRuntimeOptions(enable_browser_backends=True),
        )

        self.assertIsInstance(safe.provider_for(request), SkeletonDiscoveryProvider)
        self.assertIsInstance(browser.provider_for(request), OpenCliWechatGroupHistoryProvider)

    def test_builds_opencli_wx_history_command_with_scope(self) -> None:
        self.assertEqual(
            build_opencli_wx_history_args(
                "Openmd Test",
                since="2026-06-01",
                until="2026-06-23",
                limit=3,
            ),
            [
                "opencli",
                "wx",
                "history",
                "Openmd Test",
                "--json",
                "--since",
                "2026-06-01",
                "--until",
                "2026-06-23",
                "--limit",
                "3",
            ],
        )

    def test_parses_common_opencli_message_shapes(self) -> None:
        messages = parse_opencli_wx_history_output(
            json.dumps(
                {
                    "data": {
                        "messages": [
                            {
                                "msg_id": "m1",
                                "time": "2026-06-23T10:00:00+08:00",
                                "sender": "Alice",
                                "content": "hello",
                            },
                            {"sender": "Bob", "attachments": [{"name": "image.png"}]},
                        ]
                    }
                }
            )
        )

        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0]["id"], "m1")
        self.assertEqual(messages[0]["speaker"], "Alice")
        self.assertEqual(messages[0]["text"], "hello")
        self.assertEqual(messages[1]["attachments"], [{"name": "image.png"}])

    def test_fake_runner_returns_one_scoped_conversation_item(self) -> None:
        calls: list[tuple[tuple[str, ...], int]] = []

        def fake_runner(command: list[str], timeout: int) -> tuple[int, str, str]:
            calls.append((tuple(command), timeout))
            return (
                0,
                json.dumps(
                    [
                        {"id": "m1", "timestamp": "2026-06-23T10:00:00+08:00", "speaker": "Alice", "text": "one"},
                        {"id": "m2", "timestamp": "2026-06-23T10:01:00+08:00", "speaker": "Bob", "text": "two"},
                    ]
                ),
                "",
            )

        provider = OpenCliWechatGroupHistoryProvider(runner=fake_runner, timeout=7)
        result = provider.discover(
            DiscoveryRequest(input_url=GROUP_SELECTOR, platform="wechat_group", limit=1)
        )

        self.assertTrue(result.ok, result.error_summary)
        self.assertEqual(result.provider, OPENCLI_WX_HISTORY_DISCOVERY_BACKEND)
        self.assertEqual(len(result.items), 1)
        self.assertEqual(result.items[0].platform, "wechat_group")
        self.assertEqual(result.items[0].source_type, "conversation")
        self.assertEqual(len(result.items[0].raw_meta["messages"]), 1)
        self.assertEqual(calls[0][1], 7)

    def test_missing_opencli_returns_structured_blocker(self) -> None:
        provider = OpenCliWechatGroupHistoryProvider(
            runner=lambda _command, _timeout: (127, "", "missing executable")
        )

        result = provider.discover(
            DiscoveryRequest(input_url=GROUP_SELECTOR, platform="wechat_group", limit=2)
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "discovery_tool_missing")
        self.assertIn("missing executable", result.error_summary or "")

    def test_empty_opencli_result_reports_no_delta_status(self) -> None:
        provider = OpenCliWechatGroupHistoryProvider(
            runner=lambda _command, _timeout: (0, "[]", "")
        )

        result = provider.discover(
            DiscoveryRequest(input_url=GROUP_SELECTOR, platform="wechat_group", limit=2)
        )

        status = result.metadata["high_privacy_status"]
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "no_delta")
        self.assertEqual(status["state"], "no_delta")
        self.assertFalse(status["core_owns_session"])
        self.assertFalse(status["reads_private_database"])

    def test_safe_collect_skeleton_smoke_does_not_run_real_wechat(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = cli_main(
                    [
                        "collect",
                        GROUP_SELECTOR,
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
