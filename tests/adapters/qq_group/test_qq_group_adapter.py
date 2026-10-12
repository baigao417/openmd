from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from openmd.adapters.qq_group.adapter import QQGroupAdapter, parse_qq_export_file, parse_qq_txt_export
from openmd.adapters.qq_group.parsers.html import parse_qq_html_export
from openmd.adapters.qq_group.parsers.json import parse_qq_json_export
from openmd.adapters.registry import create_default_registry
from openmd.backends.base import DiscoveryRequest
from openmd.backends.executor import SkeletonDiscoveryProvider
from openmd.backends.registry import BackendRuntimeOptions, create_runtime_discovery_provider_registry
from openmd.cli import main as cli_main
from openmd.core.detector import detect_platform
from openmd.core.package_validator import validate_package


class QQGroupAdapterTests(unittest.TestCase):
    def test_detector_and_registry_load_qq_group_adapter_for_export_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            export_path = Path(tmp_dir) / "sample.qq.json"
            export_path.write_text("[]", encoding="utf-8")
            registry = create_default_registry()
            adapter = registry.adapter_for(str(export_path))

            self.assertEqual(detect_platform(str(export_path)), "qq_group")
            self.assertIsInstance(adapter, QQGroupAdapter)
            self.assertEqual(adapter.id, "qq_group")
            self.assertEqual(registry.backend_chain("qq_group"), ("qq_group.export",))

    def test_resolves_user_export_file_without_database_access(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            export_path = Path(tmp_dir) / "sample.qq.json"
            export_path.write_text("[]", encoding="utf-8")
            adapter = QQGroupAdapter()

            item = adapter.resolve_items(str(export_path), limit=5)[0]

            self.assertEqual(item.platform, "qq_group")
            self.assertEqual(item.input_type, "collection")
            self.assertEqual(item.source_type, "conversation")
            self.assertEqual(item.raw_meta["permission_scope"], "user_export")
            self.assertEqual(item.raw_meta["local_path"], str(export_path))
            self.assertEqual(item.raw_meta["export_format"], "json")

    def test_resolves_explicit_group_selector(self) -> None:
        adapter = QQGroupAdapter()

        item = adapter.resolve_items("qq://group/Openmd%20QQ")[0]

        self.assertEqual(item.platform, "qq_group")
        self.assertEqual(item.raw_meta["group_name"], "Openmd QQ")
        self.assertEqual(item.raw_meta["permission_scope"], "user_accessible")

    def test_default_real_parse_path_reports_export_required(self) -> None:
        adapter = QQGroupAdapter()
        item = adapter.resolve_items("qq://group/Openmd%20QQ")[0]

        result = adapter.extract_text(item, backend="qq_group.export", allow_fallback=False)

        status = result.metadata["high_privacy_status"]
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.error_code, "export_required")
        self.assertEqual(result.metadata["failure_class"], "user_resolvable")
        self.assertEqual(status["state"], "export_required")
        self.assertFalse(status["core_owns_session"])
        self.assertFalse(status["reads_private_database"])
        self.assertFalse(status["decrypts_database"])

    def test_default_real_parse_path_reads_only_user_export_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            export_path = Path(tmp_dir) / "sample.qq.txt"
            export_path.write_text("[2026-06-23 14:00] Carol: approved export\n", encoding="utf-8")
            adapter = QQGroupAdapter()
            item = adapter.resolve_items(str(export_path))[0]

            result = adapter.extract_text(item, backend="qq_group.export", allow_fallback=False)

            self.assertEqual(result.status, "verified")
            self.assertEqual(result.source_level, "manual_export")
            self.assertEqual(result.backend, "qq_group.export")
            self.assertIn("approved export", result.text)
            self.assertIn("no QQ database", " ".join(result.source_notes))

    def test_mock_package_smoke_writes_conversation_package(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            export_path = Path(tmp_dir) / "sample.qq.json"
            export_path.write_text(
                json.dumps({"messages": [{"time": "2026-06-23", "sender": "Alice", "content": "hello"}]}),
                encoding="utf-8",
            )
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = cli_main(
                    [
                        "ingest",
                        str(export_path),
                        "--out",
                        str(Path(tmp_dir) / "out"),
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
            self.assertEqual(metadata["source_platform"], "qq_group")
            self.assertEqual(metadata["source_level"], "third_party_extractor")
            self.assertEqual(list((package_path / "assets").iterdir()), [])

    def test_safe_collect_skeleton_smoke_does_not_parse_or_decrypt_real_qq_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            export_path = Path(tmp_dir) / "sample.qq.json"
            export_path.write_text("[]", encoding="utf-8")
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = cli_main(
                    [
                        "collect",
                        str(export_path),
                        "--out",
                        str(Path(tmp_dir) / "out"),
                        "--manifest-only",
                    ]
                )

            payload = json.loads(stdout.getvalue())
            runlog_rows = [
                json.loads(line)
                for line in (Path(tmp_dir) / "out" / "runlog.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            request = DiscoveryRequest(input_url=str(export_path), platform="qq_group", limit=2)
            discovery_registry = create_runtime_discovery_provider_registry("qq_group", BackendRuntimeOptions())

            self.assertEqual(exit_code, 1)
            self.assertFalse(payload["ok"])
            self.assertIsInstance(discovery_registry.provider_for(request), SkeletonDiscoveryProvider)
            self.assertEqual(runlog_rows[-1]["backend"], "skeleton.discovery")
            self.assertEqual(runlog_rows[-1]["error_code"], "discovery_not_implemented")


class QQGroupExportParserTests(unittest.TestCase):
    def test_parses_qq_chat_exporter_json_shape(self) -> None:
        messages = parse_qq_json_export(
            json.dumps(
                {
                    "data": {
                        "messages": [
                            {
                                "msg_id": "q1",
                                "time": "2026-06-23T14:00:00+08:00",
                                "sender": "Alice",
                                "content": "hello qq",
                            }
                        ]
                    }
                }
            )
        )

        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["id"], "q1")
        self.assertEqual(messages[0]["speaker"], "Alice")
        self.assertEqual(messages[0]["text"], "hello qq")

    def test_parses_html_export_shape(self) -> None:
        messages = parse_qq_html_export(
            """
            <div class="message">
              <span class="time">2026-06-23 14:00</span>
              <span class="sender">Bob</span>
              <span class="content">html hello</span>
            </div>
            """
        )

        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["timestamp"], "2026-06-23 14:00")
        self.assertEqual(messages[0]["speaker"], "Bob")
        self.assertEqual(messages[0]["text"], "html hello")

    def test_parses_plain_text_export_shape(self) -> None:
        messages = parse_qq_txt_export("[2026-06-23 14:00] Carol: txt hello\n")

        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["speaker"], "Carol")
        self.assertEqual(messages[0]["text"], "txt hello")

    def test_parse_export_file_dispatches_by_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            export_path = Path(tmp_dir) / "sample.qq.txt"
            export_path.write_text("[2026-06-23 14:00] Dave: dispatched\n", encoding="utf-8")

            messages = parse_qq_export_file(export_path)

            self.assertEqual(len(messages), 1)
            self.assertEqual(messages[0]["speaker"], "Dave")


if __name__ == "__main__":
    unittest.main()
