"""No GPU allocations: unsafe handoff, file abort, and cross-process recovery."""

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from PIL import Image

from openmd.core.models import ContentItem, TranscriptResult
from openmd.ocr.documents import extract_pdf
from openmd.ocr.config import settings
from openmd.ocr.ollama import OllamaError, unload_model
from openmd.ocr.pipeline import LocalOCRBackend
from openmd.runtime.gpu_lock import gpu_lock
from openmd.runtime.gpu_quarantine import GPUQuarantined, mark_quarantine, quarantine_path
from openmd.ocr.layout import LayoutUnavailable


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENMD_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("OPENMD_OLLAMA_URL", "http://127.0.0.1:18499")
    return tmp_path


def tags():
    return {"models": [{"name": "ovis3-bf16:latest", "digest": "synthetic"}]}


def test_unload_failure_stops_all_remaining_tiles_and_persists(runtime):
    with patch("openmd.ocr.pipeline.api_json", return_value=tags()), patch("openmd.ocr.pipeline.stream_recognize", return_value={"text": "actual first tile", "breaker": None}) as infer, patch("openmd.ocr.pipeline.unload_model", side_effect=OSError()), patch("openmd.ocr.pipeline.retain_breaker_output", return_value=None), patch("openmd.ocr.pipeline.GpuSampler"):
        result = LocalOCRBackend().recognize_image(Image.new("RGB", (400, 4000)), title="synthetic")
    assert result.status == "partial"
    assert result.error_code == "ollama_unload_unconfirmed"
    assert result.metadata["abort_file"] is True
    assert infer.call_count == 1
    assert result.metadata["pages"][0]["unprocessed_tiles"] > 0
    marker = json.loads(quarantine_path().read_text())
    assert marker["pid"] == os.getpid()
    assert marker["model"] == "ovis3-bf16:latest"
    assert marker["time"]
    with patch("openmd.ocr.ollama.api_json", side_effect=OSError()):
        with pytest.raises(GPUQuarantined), gpu_lock(timeout=0):
            pytest.fail("must not authorize another GPU client")
    assert quarantine_path().is_file()


@pytest.mark.parametrize("interruption", [KeyboardInterrupt, SystemExit])
def test_cancel_and_failed_cleanup_leave_quarantine(runtime, interruption):
    with patch("openmd.ocr.pipeline.api_json", return_value=tags()), patch("openmd.ocr.pipeline.stream_recognize", side_effect=interruption), patch("openmd.ocr.pipeline.unload_model", side_effect=OSError()), patch("openmd.ocr.pipeline.GpuSampler"):
        with pytest.raises(interruption):
            LocalOCRBackend().recognize_image(Image.new("RGB", (400, 500)), title="cancel")
    assert quarantine_path().exists()


def test_recovery_waits_readonly_then_checks_empty_before_yield(runtime):
    mark_quarantine("http://127.0.0.1:18499", "resident")
    events = []
    def ps(*args, **kwargs):
        events.append("ps")
        return {"models": [{"name": "resident"}]} if len(events) == 1 else {"models": []}
    with patch("openmd.ocr.ollama.api_json", side_effect=ps), patch("openmd.ocr.ollama.unload_model") as unload:
        with gpu_lock(timeout=1):
            events.append("client_loaded")
    unload.assert_not_called()
    assert events == ["ps", "ps", "client_loaded"]
    assert not quarantine_path().exists()


@pytest.mark.parametrize("resident", ["resident", "external-chat:latest"])
def test_quarantine_does_not_unload_even_same_named_external_model(runtime, resident):
    mark_quarantine("http://127.0.0.1:18499", "resident")
    with patch("openmd.ocr.ollama.api_json", return_value={"models": [{"name": resident}]}) as api, patch("openmd.ocr.ollama.unload_model") as unload:
        with pytest.raises(GPUQuarantined, match="^gpu_external_busy:"), gpu_lock(timeout=0):
            pytest.fail("resident service must not authorize model load")
    unload.assert_not_called()
    assert all(call.args[1] == "/api/ps" for call in api.call_args_list)
    assert quarantine_path().exists()


def test_manual_clear_removes_only_marker_without_service_requests(runtime, capsys):
    from openmd.cli import main
    quarantine_path().write_text("malformed marker")
    with patch("openmd.ocr.ollama.api_json") as api, patch("openmd.ocr.ollama.unload_model") as unload:
        assert main(["doctor", "--clear-gpu-quarantine"]) == 0
    api.assert_not_called()
    unload.assert_not_called()
    assert not quarantine_path().exists()
    assert json.loads(capsys.readouterr().out)["gpu_quarantine"]["models_unloaded"] is False


@pytest.mark.parametrize("layout", [LayoutUnavailable("not installed"), [{"category": "image", "bbox": [0, 0, 50, 50]}]])
def test_no_unload_without_any_request_to_that_model(runtime, layout):
    config = settings()
    model_tags = {"models": [{"name": "minicpm-ocr-f16:latest", "digest": "synthetic"}]}
    layout_result = patch("openmd.ocr.pipeline.detect_regions", side_effect=layout) if isinstance(layout, Exception) else patch("openmd.ocr.pipeline.detect_regions", return_value=(layout, 0))
    with layout_result, patch("openmd.ocr.pipeline.api_json", return_value={"models": [{"name": "minicpm-ocr-f16:latest"}]}), patch("openmd.ocr.pipeline.stream_recognize") as infer, patch("openmd.ocr.pipeline.unload_model") as unload, patch("openmd.ocr.pipeline.retain_breaker_output", return_value=None):
        import time
        record, _ = LocalOCRBackend()._attempt(Image.new("RGB", (50, 50)), config, model_tags, time.monotonic() + 10, "ocr.minicpm")
    infer.assert_not_called()
    unload.assert_not_called()
    assert record["unsafe_to_switch"]
    assert quarantine_path().exists()


def test_cancel_successful_unload_still_requires_next_client_confirmation(runtime):
    with patch("openmd.ocr.pipeline.api_json", return_value=tags()), patch("openmd.ocr.pipeline.stream_recognize", side_effect=KeyboardInterrupt), patch("openmd.ocr.pipeline.unload_model"), patch("openmd.ocr.pipeline.GpuSampler"):
        with pytest.raises(KeyboardInterrupt):
            LocalOCRBackend().recognize_image(Image.new("RGB", (400, 500)), title="cancel")
    assert quarantine_path().exists()
    with patch("openmd.ocr.ollama.api_json", return_value={"models": []}):
        with gpu_lock(timeout=0):
            assert not quarantine_path().exists()


@pytest.mark.parametrize("response", [{}, {"models": None}, {"models": [None]}, {"models": [{}]}])
def test_invalid_ps_is_not_empty_confirmation(runtime, response):
    mark_quarantine("http://127.0.0.1:18499", "resident")
    with patch("openmd.ocr.ollama.api_json", return_value=response):
        with pytest.raises(GPUQuarantined), gpu_lock(timeout=0):
            pytest.fail("invalid /api/ps must not authorize inference")
    assert quarantine_path().exists()


def test_unload_requires_valid_ps(runtime):
    with patch("openmd.ocr.ollama.api_json", side_effect=[{}, {}]):
        with pytest.raises(OllamaError, match="ollama_ps_invalid"):
            unload_model({"ollama_url": "http://127.0.0.1:18499"}, "model")


def test_cross_process_quarantine_blocks_next_gpu_client(runtime):
    code = "from openmd.runtime.gpu_lock import gpu_lock; from openmd.runtime.gpu_quarantine import mark_quarantine;\nwith gpu_lock(): mark_quarantine('http://127.0.0.1:18499','orphan')"
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, timeout=10)
    assert done.returncode == 0, done.stderr
    with patch("openmd.ocr.ollama.api_json", side_effect=OSError()):
        with pytest.raises(GPUQuarantined), gpu_lock(timeout=0):
            pytest.fail("persisted cross-process marker was ignored")


def test_pdf_abort_skips_even_remaining_native_pages(runtime):
    import pymupdf
    path = runtime / "pages.pdf"
    with pymupdf.open() as pdf:
        for _ in range(3):
            page = pdf.new_page()
            page.draw_rect(page.rect, fill=(0.5, 0.5, 0.5))
        pdf.save(path)
    partial = TranscriptResult(text="first page", status="partial", source_level="ocr", backend="ocr.chain", error_code="ollama_unload_unconfirmed", metadata={"abort_file": True, "pages": [{"page": 1, "route": "ocr", "status": "partial", "reason": "ollama_unload_unconfirmed"}]})
    with patch("openmd.ocr.documents._render_and_recognize", return_value=partial) as recognize:
        result = extract_pdf(ContentItem(platform="pdf", input_type="single", url=path.as_uri()), allow_ocr=True)
    assert recognize.call_count == 1
    assert result.error_code == "ollama_unload_unconfirmed"
    assert len(result.metadata["pages"]) == 3
    assert result.metadata["pages"][1]["reason"] == "gpu_quarantined_file_aborted"


def test_asr_inprocess_does_not_load_when_quarantined(runtime):
    from openmd.runtime.asr import WorkerASRBackend
    from openmd.runtime.interpreters import AsrRuntime
    audio = runtime / "audio.wav"
    audio.write_bytes(b"synthetic")
    mark_quarantine("http://127.0.0.1:18499", "resident")
    with patch.object(WorkerASRBackend, "_runtime", return_value=AsrRuntime("in_process", None, "synthetic")), patch("openmd.ocr.ollama.api_json", side_effect=OSError()), patch("openmd.runtime.asr.local_backend") as factory:
        result = WorkerASRBackend("asr.auto").transcribe(audio)
    assert result.error_code == "gpu_quarantined"
    factory.assert_not_called()


def test_asr_worker_does_not_load_when_quarantined(runtime):
    from openmd.runtime.asr_worker import main
    target = runtime / "result.json"
    mark_quarantine("http://127.0.0.1:18499", "resident")
    with patch.dict(os.environ), patch("openmd.ocr.ollama.api_json", side_effect=OSError()), patch("openmd.runtime.asr.local_backend") as factory:
        assert main(["--backend", "asr.auto", "--input", str(runtime / "audio.wav"), "--result", str(target)]) == 0
    assert json.loads(target.read_text())["result"]["error_code"] == "gpu_quarantined"
    factory.assert_not_called()


def test_layout_refuses_cuda_visibility(monkeypatch):
    from openmd.ocr.layout_worker import assert_cpu_only
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "0")
    with pytest.raises(RuntimeError, match="layout_cpu_only"):
        assert_cpu_only()
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "")
    with pytest.raises(RuntimeError, match="layout_cpu_only"):
        assert_cpu_only(Mock(cuda=Mock(is_available=lambda: True)))


def test_layout_accepts_cpu_mask_only_when_cuda_is_actually_hidden(monkeypatch):
    from openmd.ocr.layout_worker import assert_cpu_only
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "-1")
    assert_cpu_only(Mock(cuda=Mock(is_available=lambda: False)))
    with pytest.raises(RuntimeError, match="layout_cpu_only"):
        assert_cpu_only(Mock(cuda=Mock(is_available=lambda: True)))


def test_layout_controller_uses_windows_safe_cpu_mask(tmp_path):
    from openmd.ocr.layout import _run
    with patch("openmd.ocr.layout.layout_runtime", return_value=(Path(sys.executable), tmp_path)), patch("openmd.ocr.layout.owned_popen") as popen, patch("openmd.ocr.layout.wait_interruptibly"):
        popen.return_value.returncode = 0
        _run(["--probe"], 10, tmp_path)
    assert popen.call_args.kwargs["env"]["CUDA_VISIBLE_DEVICES"] == "-1"


def test_doctor_exposes_endpoint_failure(monkeypatch):
    from openmd.core.doctor import _document_diagnostics
    monkeypatch.setenv("OPENMD_OLLAMA_URL", "http://198.51.100.7:18499")
    with patch("openmd.ocr.layout.probe_layout", return_value={"ready": True}):
        result = _document_diagnostics()
    assert result["ollama"]["error_code"] == "ocr_ollama_url_not_loopback"
    assert not result["ready"]
    assert result["layout_runtime"]["cpu_only"] is True
