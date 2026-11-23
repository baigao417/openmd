from types import SimpleNamespace
from unittest.mock import patch
import sys

from openmd.backends.ocr import OCRRouterProvider


def test_interactive_scan_asks_before_install_and_retries_current_request(monkeypatch, capsys):
    from openmd.core.models import TranscriptResult

    results = [
        TranscriptResult(text="", status="failed", source_level="ocr", backend="ocr.chain", title="scan", error_code="ollama_model_missing"),
        TranscriptResult(text="recognized", status="verified", source_level="ocr", backend="ocr.chain", title="scan"),
    ]
    monkeypatch.setattr("openmd.ocr.documents.extract_pdf", lambda *args, **kwargs: results.pop(0))
    monkeypatch.setattr("openmd.runtime.recommendations.cmd_models_install", lambda args: 0)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    prompts = []
    monkeypatch.setattr("builtins.input", lambda prompt: prompts.append(prompt) or "yes")
    request = SimpleNamespace(item=SimpleNamespace(platform="pdf", url="scan.pdf", title="scan"))
    provider = OCRRouterProvider("ocr.chain", SimpleNamespace(ocr_engine="ocr.chain", enable_network_backends=True))

    result = provider.extract(request)

    assert result.text == "recognized"
    assert not results
    assert "GiB" in prompts[0] and "PP-DocLayoutV3" in prompts[0]


def test_noninteractive_scan_returns_install_command_without_downloading(monkeypatch):
    from openmd.core.models import TranscriptResult

    result = TranscriptResult(text="", status="failed", source_level="ocr", backend="ocr.chain", title="scan", error_code="ollama_model_missing")
    monkeypatch.setattr("openmd.ocr.documents.extract_pdf", lambda *args, **kwargs: result)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    request = SimpleNamespace(item=SimpleNamespace(platform="pdf", url="scan.pdf", title="scan"))
    provider = OCRRouterProvider("ocr.chain", SimpleNamespace(ocr_engine="ocr.chain", enable_network_backends=True))

    returned = provider.extract(request)

    assert any("openmd models install ocr:chain" in note for note in returned.source_notes)
