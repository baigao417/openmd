from __future__ import annotations

import base64
import contextlib
import io
import importlib.machinery
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from openmd.adapters.registry import create_default_registry
from openmd.adapters.screenshot.adapter import ScreenshotOcrAdapter
from openmd.adapters.screenshot.backends.rapidocr import RapidOCRBackend
from openmd.adapters.screenshot.ocr import classify_ocr_profile
from openmd.backends.base import DiscoveryRequest
from openmd.backends.executor import SkeletonDiscoveryProvider
from openmd.backends.registry import BackendRuntimeOptions, create_runtime_backend_provider_registry, create_runtime_discovery_provider_registry
from openmd.backends.screenshot import ScreenshotOCRProvider
from openmd.cli import main as cli_main
from openmd.core.detector import detect_platform
from openmd.core.package_validator import validate_package


class ScreenshotOcrAdapterTests(unittest.TestCase):
    def test_detector_and_registry_load_screenshot_adapter_without_registry_edits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            image_path = Path(tmp_dir) / "long-chat.png"
            _write_png(image_path)

            registry = create_default_registry()
            adapter = registry.adapter_for(str(image_path))

            self.assertEqual(detect_platform(str(image_path)), "screenshot")
            self.assertIsInstance(adapter, ScreenshotOcrAdapter)
            self.assertEqual(adapter.id, "screenshot")
            self.assertEqual(
                registry.backend_chain("screenshot"),
                ("ocr.chain", "ocr.rapidocr", "ocr.tesseract"),
            )

    def test_resolves_local_image_file_without_copying_raw_screenshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            image_path = Path(tmp_dir) / "long-chat.png"
            _write_png(image_path)

            adapter = ScreenshotOcrAdapter("screenshot", "screenshot.mock")
            item = adapter.resolve_items(str(image_path))[0]

            self.assertEqual(item.platform, "screenshot")
            self.assertEqual(item.source_type, "image_text")
            self.assertEqual(item.raw_meta["permission_scope"], "local_file")
            self.assertEqual(item.raw_meta["local_path"], str(image_path))
            self.assertEqual(len(item.id or ""), 12)

    def test_mock_backend_creates_valid_document_package_from_screenshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            image_path = root / "long-chat.png"
            out_dir = root / "out"
            _write_png(image_path)

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = cli_main(
                    [
                        "ingest",
                        str(image_path),
                        "--out",
                        str(out_dir),
                        "--backend",
                        "screenshot.mock",
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
            self.assertEqual(metadata["profile"], "document")
            self.assertEqual(metadata["source_level"], "ocr")
            self.assertEqual(list((package_path / "assets").iterdir()), [])
            self.assertIn("Mock OCR text", combined)
            self.assertIn("local_file", combined)

    def test_directory_input_routes_to_screenshot_adapter_and_batches_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            image_dir = root / "images"
            image_dir.mkdir()
            _write_png(image_dir / "long-a.png")
            _write_png(image_dir / "long-b.png")
            (image_dir / "long-b.png").write_bytes((image_dir / "long-b.png").read_bytes() + b"variant")
            (image_dir / "notes.txt").write_text("not an image", encoding="utf-8")
            out_dir = root / "out"

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = cli_main(
                    [
                        "ingest",
                        str(image_dir),
                        "--out",
                        str(out_dir),
                        "--backend",
                        "screenshot.mock",
                        "--package",
                    ]
                )

            payload = json.loads(stdout.getvalue())
            packages = sorted(out_dir.rglob("*.openmd"))

            self.assertEqual(exit_code, 0)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["item_count"], 2)
            self.assertEqual(len(packages), 2)
            self.assertTrue(all(validate_package(package).ok for package in packages))

    def test_default_ocr_backend_fails_structurally_without_real_ocr(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            image_path = root / "long-chat.png"
            out_dir = root / "out"
            _write_png(image_path)

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = cli_main(
                    [
                        "ingest",
                        str(image_path),
                        "--out",
                        str(out_dir),
                        "--package",
                    ]
                )

            payload = json.loads(stdout.getvalue())
            runlog_rows = [
                json.loads(line)
                for line in (out_dir / "runlog.jsonl").read_text(encoding="utf-8").splitlines()
            ]

            self.assertEqual(exit_code, 1)
            self.assertFalse(payload["ok"])
            self.assertEqual(runlog_rows[-1]["error_code"], "ocr_backend_missing")
            self.assertEqual(runlog_rows[-1]["backend"], "ocr.tesseract")
            self.assertEqual(list(out_dir.rglob("*.openmd")), [])

    def test_rapidocr_backend_recognizes_local_image_when_optional_dependency_is_available(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            image_path = Path(tmp_dir) / "long-chat.png"
            _write_png(image_path)
            fake_module = types.ModuleType("rapidocr_onnxruntime")
            fake_module.__spec__ = importlib.machinery.ModuleSpec("rapidocr_onnxruntime", loader=None)

            class FakeRapidOCR:
                def __call__(self, source: str):
                    self.source = source
                    return (
                        [
                            ([[0, 0], [1, 0], [1, 1], [0, 1]], "10:01 Alice: hi", 0.99),
                            ([[0, 2], [1, 2], [1, 3], [0, 3]], "10:02 Bob: hello", 0.97),
                        ],
                        0.01,
                    )

            fake_module.RapidOCR = FakeRapidOCR

            with patch.dict(sys.modules, {"rapidocr_onnxruntime": fake_module}):
                result = RapidOCRBackend().recognize(str(image_path), title="Chat screenshot")

        self.assertTrue(result.has_usable_text)
        self.assertEqual(result.status, "verified")
        self.assertEqual(result.source_level, "ocr")
        self.assertEqual(result.backend, "ocr.rapidocr")
        self.assertIn("10:01 Alice: hi", result.text)
        self.assertEqual(result.metadata["profile"], "conversation")
        self.assertEqual(result.metadata["block_count"], 2)
        self.assertEqual(result.metadata["average_confidence"], 0.98)

    def test_layout_classifier_detects_chat_like_ocr_text(self) -> None:
        self.assertEqual(
            classify_ocr_profile("10:01 Alice: hi\n10:02 Bob: hello\n"),
            "conversation",
        )
        self.assertEqual(classify_ocr_profile("A normal scanned document paragraph."), "document")


class ScreenshotBackendGateTests(unittest.TestCase):
    def test_optional_ocr_providers_report_missing_when_dependencies_are_unavailable(self) -> None:
        with patch.object(RapidOCRBackend, "is_available", return_value=False):
            safe = create_runtime_backend_provider_registry("screenshot", BackendRuntimeOptions())
            self.assertIsInstance(safe.provider_for("screenshot.mock"), ScreenshotOCRProvider)
            self.assertIsInstance(safe.provider_for("ocr.rapidocr"), ScreenshotOCRProvider)
            self.assertFalse(safe.provider_for("ocr.rapidocr").health().ok)
            self.assertIsInstance(safe.provider_for("ocr.paddle"), ScreenshotOCRProvider)
            self.assertFalse(safe.provider_for("ocr.paddle").health().ok)

    def test_safe_collect_skeleton_smoke_does_not_run_real_ocr_tools(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            image_path = root / "long-chat.png"
            _write_png(image_path)

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = cli_main(
                    [
                        "collect",
                        str(image_path),
                        "--out",
                        str(root / "out"),
                        "--manifest-only",
                    ]
                )

            payload = json.loads(stdout.getvalue())
            runlog_rows = [
                json.loads(line)
                for line in (root / "out" / "runlog.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            request = DiscoveryRequest(input_url=str(image_path), platform="screenshot", limit=2)
            discovery_registry = create_runtime_discovery_provider_registry(
                "screenshot",
                BackendRuntimeOptions(),
            )

            self.assertEqual(exit_code, 1)
            self.assertFalse(payload["ok"])
            self.assertIsInstance(discovery_registry.provider_for(request), SkeletonDiscoveryProvider)
            self.assertEqual(runlog_rows[-1]["backend"], "skeleton.discovery")
            self.assertEqual(runlog_rows[-1]["error_code"], "discovery_not_implemented")


def _write_png(path: Path) -> None:
    path.write_bytes(
        base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
        )
    )


if __name__ == "__main__":
    unittest.main()
