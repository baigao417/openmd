"""Offline regression protection; real CLI evidence is recorded separately."""

from __future__ import annotations
from tests.ocr.seam_chain import merge_texts

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from openmd.backends.base import BackendExtractRequest
from openmd.backends.executor import BackendExecutor
from openmd.backends.registry import BackendRuntimeOptions, create_runtime_backend_provider_registry
from openmd.core.models import ContentItem, TranscriptResult
from openmd.ocr.breakers import breaker_reason, estimate_expected_chars, repetition_trip
from openmd.ocr.config import settings
from openmd.ocr.documents import extract_pdf, text_is_reliable
from openmd.ocr.layout import LayoutUnavailable
from openmd.ocr.office import recover_office, suspected_loss
from openmd.ocr.otsl import table_html
from openmd.ocr.pipeline import LocalOCRBackend
from openmd.ocr.tiling import plan_tiles
from openmd.runtime.gpu_lock import gpu_lock


def tag_data():
    return {"models": [{"name": "ovis3-bf16:latest", "digest": "ovis-digest"}, {"name": "minicpm-ocr-f16:latest", "digest": "mini-digest"}]}


def output(text="This is the text actually visible in the synthetic source image.", breaker=None):
    return {"text": text, "seconds": 0.01, "breaker": breaker, "tokens_generated": 20}


class BreakerTests(unittest.TestCase):
    def setUp(self):
        self.config = settings()

    def test_repetition_is_configurable(self):
        text = "同一句话需要足够长才会触发重复句检测规则。\n"
        self.assertIsNone(repetition_trip(text * 2, self.config))
        self.assertEqual(repetition_trip(text * 3, self.config)["rule"], "repeat_sentence")
        self.assertIsNone(repetition_trip(text * 3, {**self.config, "repeat_count": 4}))
        self.assertIsNone(repetition_trip("短句。" * 10, self.config))

    def test_formulas_exempt_prose_with_formula_is_not(self):
        self.assertIsNone(repetition_trip("$$\\sum_{i=1}^{n} a_i = 2500$$\n" * 4, self.config))
        self.assertIsNone(repetition_trip("i = 1, 2, ..., n\n" * 4, self.config))
        text = "这是足够长的解释句子，其中公式 $x=1$ 不应该豁免整句。\n"
        self.assertIsNotNone(repetition_trip(text * 3, self.config))

    def test_ordinary_english_with_numbers_not_formula(self):
        text = "The report recorded revenue of 12345 dollars in the current quarter.\n"
        self.assertIsNotNone(repetition_trip(text * 3, self.config))

    def test_length_budget_never_reads_truth(self):
        self.assertEqual(breaker_reason("abcde", {"max_length_chars": 4, "estimated_chars": 2}, self.config)["rule"], "length_gt_image_estimate")
        from PIL import Image

        budget = estimate_expected_chars(Image.new("RGB", (100, 200), "white"), self.config)
        self.assertEqual(budget["max_length_chars"], budget["estimated_chars"] * 2)

    def test_html_table_tags_do_not_spend_visible_capacity(self):
        table = "<table>" + "<tr><td>X</td><td>Y</td></tr>" * 50 + "</table>"
        self.assertIsNone(breaker_reason(table, {"max_length_chars": 400, "estimated_chars": 200}, self.config))

    def test_invalid_config_fails_closed(self):
        with patch("openmd.ocr.config.load_local_config", return_value={"ocr": {"tile_overlap": 1}}):
            with self.assertRaises(ValueError):
                settings()


class ChainTests(unittest.TestCase):
    def setUp(self):
        from PIL import Image

        self.image = Image.new("RGB", (400, 500), "white")
        self.regions = ([{"order": 0, "category": "text", "bbox": [0, 0, 400, 500], "score": 1.0}], 0.01)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.environment = patch.dict(os.environ, {"OPENMD_RUNTIME_DIR": self.temp.name})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def recognize(self, outputs, *, layout=None, name="ocr.chain"):
        with patch("openmd.ocr.pipeline.api_json", return_value=tag_data()), patch("openmd.ocr.pipeline.stream_recognize", side_effect=outputs) as stream, patch("openmd.ocr.pipeline.unload_model") as unload, patch("openmd.ocr.pipeline.detect_regions", return_value=layout or self.regions), patch("openmd.ocr.pipeline.retain_breaker_output", return_value="ignored-out/raw.md"):
            result = LocalOCRBackend(name).recognize_image(self.image, title="fixture")
            return result, stream, unload

    def test_primary_success_identity_and_notes(self):
        result, stream, unload = self.recognize([output()])
        self.assertEqual((result.status, result.source_level), ("verified", "ocr"))
        self.assertFalse(result.metadata["pages"][0]["fallback_used"])
        self.assertEqual(result.metadata["pages"][0]["models"][0]["digest"], "ovis-digest")
        self.assertTrue(any("不代表无幻觉" in note for note in result.source_notes))
        self.assertEqual(stream.call_count, 1)
        unload.assert_called_once()

    def test_fallback_completes_and_unloads_before_switch(self):
        events = []
        def streaming(*args, **kwargs):
            events.append(kwargs["model"])
            return output(breaker={"rule": "repeat_sentence"}) if len(events) == 1 else output()
        with patch("openmd.ocr.pipeline.api_json", return_value=tag_data()), patch("openmd.ocr.pipeline.stream_recognize", side_effect=streaming), patch("openmd.ocr.pipeline.unload_model", side_effect=lambda c, n: events.append("unload:" + n)), patch("openmd.ocr.pipeline.detect_regions", return_value=self.regions), patch("openmd.ocr.pipeline.retain_breaker_output", return_value=None):
            result = LocalOCRBackend().recognize_image(self.image, title="fixture")
        self.assertEqual(result.status, "verified")
        self.assertTrue(result.metadata["fallback_used"])
        self.assertEqual(events, ["ovis3-bf16:latest", "unload:ovis3-bf16:latest", "minicpm-ocr-f16:latest", "unload:minicpm-ocr-f16:latest"])

    def test_double_breaker_is_partial(self):
        result, _, _ = self.recognize([output(breaker={"rule": "repeat_sentence"}), output(breaker={"rule": "token_limit"})])
        self.assertEqual(result.status, "partial")
        self.assertTrue(result.metadata["terminal_partial"])
        self.assertEqual(len(result.metadata["pages"][0]["breakers"]), 2)

    def test_fallback_unavailable_is_partial(self):
        with patch("openmd.ocr.pipeline.api_json", side_effect=lambda url, path, **kwargs: {"models": []} if path == "/api/ps" else tag_data()), patch("openmd.ocr.pipeline.stream_recognize", return_value=output(breaker={"rule": "repeat_sentence"})), patch("openmd.ocr.pipeline.unload_model"), patch("openmd.ocr.pipeline.detect_regions", side_effect=LayoutUnavailable("missing")), patch("openmd.ocr.pipeline.retain_breaker_output", return_value=None):
            result = LocalOCRBackend().recognize_image(self.image, title="fixture")
        self.assertEqual(result.status, "partial")
        self.assertIn("fallback_unavailable", result.metadata["pages"][0]["reason"])

    def test_failed_unload_prevents_fallback(self):
        with patch("openmd.ocr.pipeline.api_json", return_value=tag_data()), patch("openmd.ocr.pipeline.stream_recognize", return_value=output()) as stream, patch("openmd.ocr.pipeline.unload_model", side_effect=OSError("unreachable")), patch("openmd.ocr.pipeline.retain_breaker_output", return_value=None):
            result = LocalOCRBackend().recognize_image(self.image, title="fixture")
        self.assertEqual(result.status, "partial")
        self.assertEqual(stream.call_count, 1)

    def test_failed_tile_remains_partial(self):
        image = self.image.resize((400, 4000))
        with patch("openmd.ocr.pipeline.api_json", return_value=tag_data()), patch.object(LocalOCRBackend, "_tile", side_effect=lambda *args: ({"status": "partial", "fallback_used": False, "reason": "deadline", "attempts": []}, "")):
            result = LocalOCRBackend().recognize_image(image, title="long")
        self.assertEqual(result.status, "partial")

    def test_permission_gate_never_calls_ollama(self):
        registry = create_runtime_backend_provider_registry("screenshot", BackendRuntimeOptions())
        item = ContentItem(platform="screenshot", input_type="single", url="local.png")
        with patch("openmd.ocr.ollama.api_json") as call:
            result = registry.provider_for("ocr.chain").extract(BackendExtractRequest(item=item, backend_chain=("ocr.chain",)))
        self.assertEqual(result.error_code, "ocr_permission_required")
        call.assert_not_called()

    def test_terminal_empty_partial_does_not_fall_through(self):
        registry = create_runtime_backend_provider_registry("screenshot", BackendRuntimeOptions())
        provider = registry.provider_for("ocr.chain")
        partial = TranscriptResult(text="", status="partial", source_level="ocr", backend="ocr.chain", metadata={"terminal_partial": True})
        with patch.object(provider, "extract", return_value=partial):
            result = BackendExecutor(provider_registry=registry).extract(ContentItem(platform="screenshot", input_type="single", url="local.png"), ("ocr.chain", "ocr.rapidocr"))
        self.assertEqual(result.backend, "ocr.chain")
        self.assertEqual(result.status, "partial")


class PDFOfficeTests(unittest.TestCase):
    def test_native_columns_preserve_content_stream_order(self):
        import pymupdf

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "columns.pdf"
            document = pymupdf.open()
            page = document.new_page()
            page.insert_text((40, 40), "Left column starts here with sufficient text.")
            page.insert_text((40, 120), "Left column continues below its first line.")
            page.insert_text((320, 40), "Right column starts only after the left.")
            document.save(path)
            document.close()
            result = extract_pdf(ContentItem(platform="pdf", input_type="single", url=path.as_uri()), backend="pdf.textlayer")
            self.assertLess(result.text.index("continues"), result.text.index("Right"))

    def test_textlayer_reliability(self):
        self.assertFalse(text_is_reliable("12", settings()))
        self.assertFalse(text_is_reliable("\ufffd" * 100, settings()))
        self.assertTrue(text_is_reliable("A reliable native text layer with sufficiently many readable characters.", settings()))

    def test_mixed_pdf_preserves_page_order(self):
        import pymupdf
        from PIL import Image
        import io

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mixed.pdf"
            document = pymupdf.open()
            page = document.new_page()
            page.insert_text((40, 40), "First native page has enough characters for a reliable text layer.")
            page = document.new_page()
            data = io.BytesIO()
            Image.new("RGB", (200, 300), "gray").save(data, format="PNG")
            page.insert_image(page.rect, stream=data.getvalue())
            document.save(path)
            document.close()
            item = ContentItem(platform="pdf", input_type="single", url=path.as_uri(), title="mixed")
            fake = TranscriptResult(text="Second scanned page content.", status="verified", source_level="ocr", backend="ocr.chain", metadata={"pages": [{"page": 2, "route": "ocr", "status": "verified", "fallback_used": False, "reason": None}]})
            with patch.object(LocalOCRBackend, "recognize_image", return_value=fake) as recognize:
                result = extract_pdf(item, allow_ocr=True)
            self.assertEqual(result.status, "verified")
            self.assertEqual(result.source_level, "ocr")
            self.assertLess(result.text.index("First"), result.text.index("Second"))
            self.assertEqual([p["route"] for p in result.metadata["pages"]], ["textlayer", "ocr"])
            self.assertEqual(recognize.call_count, 1)
            result = extract_pdf(item, allow_ocr=False)
            self.assertEqual(result.status, "partial")
            self.assertIn("First", result.text)
            native_only = extract_pdf(item, backend="pdf.textlayer", allow_ocr=False)
            self.assertEqual(native_only.error_code, "pdf_partial")
            self.assertIn("First", native_only.text)
            self.assertEqual(native_only.metadata["pages"][1]["reason"], "ocr_permission_required")

    def test_missing_pymupdf_install_hint(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x.pdf"
            path.write_bytes(b"%PDF")
            item = ContentItem(platform="pdf", input_type="single", url=path.as_uri())
            with patch("openmd.ocr.documents.importlib.util.find_spec", return_value=None):
                result = extract_pdf(item)
        self.assertEqual(result.error_code, "pymupdf_missing")
        self.assertIn("openmd-cli[ocr-router]", result.error_summary)

    def test_office_sparse_visual_recovery_missing_is_partial(self):
        from zipfile import ZipFile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "visual.docx"
            with ZipFile(path, "w") as archive:
                archive.writestr("word/media/image1.png", b"fixture")
                archive.writestr("word/charts/chart1.xml", "<chart/>")
            check = suspected_loss(path, "# Title\n\nshort text", settings())
            self.assertTrue(check["suspected"])
            item = ContentItem(platform="pdf", input_type="single", url=path.as_uri(), raw_meta={"local_path": str(path)})
            original = TranscriptResult(text="Short native text.", status="verified", source_level="native_text", backend="pdf.markitdown")
            with patch("openmd.ocr.office.soffice_path", return_value=None):
                result = recover_office(item, original, allow_ocr=True)
            self.assertEqual(result.status, "partial")
            self.assertEqual(result.text, original.text)
            self.assertEqual(result.error_code, "libreoffice_missing")


class GPUAndRouteTests(unittest.TestCase):
    def test_os_lock_serializes_two_processes_and_reclaims_crash(self):
        source_root = str(Path(__file__).resolve().parents[2] / "src")
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / "gpu.lock"
            code = "import sys,time;from pathlib import Path;from openmd.runtime.gpu_lock import gpu_lock;\nwith gpu_lock(path=Path(sys.argv[1]),timeout=10):\n print('entered',flush=True); time.sleep(30)"
            environment = {**os.environ, "PYTHONPATH": source_root}
            process = subprocess.Popen([sys.executable, "-u", "-c", code, str(lock)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=environment)
            try:
                self.assertEqual(process.stdout.readline().strip(), "entered")
                second = subprocess.run([sys.executable, "-c", "import sys;from pathlib import Path;from openmd.runtime.gpu_lock import gpu_lock;\ntry:\n with gpu_lock(path=Path(sys.argv[1]),timeout=.2): print('incorrect entry')\nexcept TimeoutError: print('blocked')", str(lock)], capture_output=True, text=True, env=environment, timeout=10)
                self.assertEqual(second.stdout.strip(), "blocked")
                with patch("openmd.runtime.gpu_lock.time.sleep", side_effect=KeyboardInterrupt):
                    with self.assertRaises(KeyboardInterrupt):
                        with gpu_lock(path=lock, timeout=10):
                            self.fail("waiting owner must not enter")
                process.kill()
                process.wait(timeout=5)
                with gpu_lock(path=lock, timeout=1):
                    self.assertTrue(lock.exists())
            finally:
                if process.poll() is None:
                    process.kill()
                process.communicate(timeout=5)

    def test_routes_and_providers_recognized(self):
        from openmd.routing.loader import default_backend_chains, load_catalog

        catalog = load_catalog()
        self.assertEqual(default_backend_chains(effective=False)["screenshot"], ("ocr.chain", "ocr.rapidocr", "ocr.tesseract"))
        self.assertEqual(default_backend_chains(effective=False)["pdf"], ("pdf.markitdown", "pdf.textlayer", "ocr.chain", "document.engines"))
        for platform in ("screenshot", "pdf"):
            registry = create_runtime_backend_provider_registry(platform, BackendRuntimeOptions())
            for name in ("ocr.chain", "ocr.ovis3", "ocr.minicpm"):
                self.assertIn(name, catalog.capabilities)
                self.assertEqual(registry.provider_for(name).descriptor().name, name)

    def test_asr_worker_owns_the_same_gpu_lease(self):
        from openmd.runtime.asr_worker import main
        from openmd.runtime.interpreters import worker_environment

        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"OPENMD_RUNTIME_DIR": tmp, "OPENMD_GPU_LOCK_TIMEOUT": "1"}):
            result_path = Path(tmp) / "result.json"
            test = self
            class Backend:
                def transcribe(self, source):
                    with test.assertRaises(TimeoutError):
                        with gpu_lock(timeout=0):
                            test.fail("ASR worker must already own the lease")
                    return TranscriptResult(text="fixture", status="verified", source_level="media_asr", backend="asr.auto")
            with patch("openmd.runtime.asr.local_backend", return_value=Backend()), patch("sys.argv", ["worker", "--backend", "asr.auto", "--input", str(Path(tmp) / "audio.wav"), "--result", str(result_path)]):
                self.assertEqual(main(), 0)
            self.assertTrue(result_path.is_file())
            self.assertEqual(worker_environment()["OPENMD_RUNTIME_DIR"], str(Path(tmp).resolve()))
            with gpu_lock(timeout=0):
                pass

    def test_table_spans(self):
        result = table_html("<fcel>A<lcel><nl><fcel>B<fcel>C<nl>")
        self.assertIn('colspan="2"', result)
        self.assertIn("<td>B</td>", result)

    def test_runlog_keeps_every_page_and_no_body(self):
        from openmd.core.runlog import build_item_runlog_entry
        from openmd.core.secrets import sanitize_runlog_entry

        pages = [{"page": i, "route": "ocr", "text": "PRIVATE RECOGNIZED BODY", "digest": "abcdef", "han_ratio": 0.9} for i in range(32)]
        result = TranscriptResult(text="body", status="verified", source_level="ocr", backend="ocr.chain", metadata={"pages": pages})
        entry = sanitize_runlog_entry(build_item_runlog_entry(ContentItem(platform="screenshot", input_type="single", url="local.png"), result))
        self.assertEqual(len(entry["document_metadata"]["pages"]), 32)
        self.assertNotIn("PRIVATE RECOGNIZED BODY", json.dumps(entry))

    def test_bench_accepts_local_samples_and_standalone_engine(self):
        from openmd.routing.lifecycle import run_benchmark
        from PIL import Image

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image = root / "small.png"
            Image.new("RGB", (16, 16), "white").save(image)
            samples = root / "samples.json"
            samples.write_text(json.dumps({"schema": "openmd.route-samples/v1", "samples": [{"id": "local", "source": "screenshot", "capability": "content", "url": image.as_uri()}]}), encoding="utf-8")
            result = TranscriptResult(text="real fixture result", status="verified", source_level="ocr", backend="ocr.ovis3")
            report = run_benchmark(source="screenshot", capability="content", candidates=["ocr.ovis3"], options=BackendRuntimeOptions(enable_network_backends=True), ledger_path=root / "ledger.jsonl", samples_path=samples, artifact_dir=root / "artifacts", executor=lambda *args: result)
            self.assertEqual(report["rows"][0]["candidate"], "ocr.ovis3")

    def test_package_keeps_page_receipts(self):
        from openmd.core.package_writer import write_item_package

        with tempfile.TemporaryDirectory() as tmp:
            item = ContentItem(platform="screenshot", input_type="single", url="file:///fixture/page.png", title="fixture", source_type="image_text", raw_meta={"profile": "document"})
            result = TranscriptResult(text="A complete raw text fixture.", status="verified", source_level="ocr", backend="ocr.chain", metadata={"pages": [{"page": 1, "route": "ocr", "digest": "abcdef"}]})
            written = write_item_package(item, result, Path(tmp))
            metadata = json.loads((written.package_path / "metadata.json").read_text(encoding="utf-8"))
            self.assertEqual(metadata["document_metadata"]["pages"][0]["digest"], "abcdef")


class StreamingTests(unittest.TestCase):
    def serve(self, chunks, delay=0):
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        received = []
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                received.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson")
                self.end_headers()
                try:
                    for chunk in chunks:
                        time.sleep(delay)
                        self.wfile.write(json.dumps(chunk).encode() + b"\n")
                        self.wfile.flush()
                except OSError:
                    pass
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server.server_address[1], received

    def stream(self, port, seconds=3):
        from PIL import Image
        from openmd.ocr.ollama import stream_recognize

        config = {**settings(), "ollama_url": f"http://127.0.0.1:{port}"}
        return stream_recognize(Image.new("RGB", (64, 64), "white"), model="test", prompt="raw OCR", config=config, deadline=time.monotonic() + seconds)

    def test_stream_token_limit_and_payload(self):
        port, received = self.serve([{"message": {"content": "source"}, "done": True, "done_reason": "length", "eval_count": 8192}])
        result = self.stream(port)
        self.assertEqual(result["breaker"]["rule"], "token_limit")
        self.assertEqual(received[0]["options"]["num_predict"], 8192)
        self.assertTrue(received[0]["stream"])

    def test_stream_deadline_cancels_socket(self):
        port, _ = self.serve([{"message": {"content": "delayed"}, "done": True}], delay=2)
        start = time.monotonic()
        result = self.stream(port, seconds=0.15)
        self.assertEqual(result["breaker"]["rule"], "page_deadline")
        self.assertLess(time.monotonic() - start, 1.0)

    def test_stream_without_done_is_partial_candidate(self):
        port, _ = self.serve([{"message": {"content": "incomplete source"}}])
        result = self.stream(port)
        self.assertEqual(result["breaker"]["rule"], "stream_incomplete")


if __name__ == "__main__":
    unittest.main()
