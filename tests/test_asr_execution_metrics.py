"""ASR receipts survive CLI writers without exposing transcript or runtime paths."""
import json
import math

from openmd.core.models import ContentItem, TranscriptResult
from openmd.core.package_writer import build_package_metadata
from openmd.core.runlog import append_runlog_entry, build_item_runlog_entry
from openmd.core.secrets import sanitize_asr_execution


def test_asr_receipt_keeps_selected_model_and_route_without_free_text():
    receipt = sanitize_asr_execution({
        "model": "local-Qwen3-ASR-1.7B", "duration": 68.1, "segment_count": 12,
        "asr_route": {"selected_backend": "asr.qwen3", "detected_language": "zh",
                      "reason": "private arbitrary text"},
        "play_url": "https://example.invalid/signed", "text": "source speech",
        "asr_backend": "douyin.transcript", "model_size": "C:/private/model",
    })
    assert receipt == {"model": "local-Qwen3-ASR-1.7B", "duration": 68.1,
                       "segment_count": 12, "asr_backend": "asr.qwen3",
                       "detected_language": "zh"}
    assert sanitize_asr_execution({"model": "Qwen/Qwen3-ASR-1.7B"}) == {"model": "Qwen/Qwen3-ASR-1.7B"}


def test_asr_metrics_survive_package_and_actual_runlog(tmp_path):
    item = ContentItem(platform="generic_media", input_type="single", url="file:///speech.wav", source_type="audio")
    metadata = {"profile": "media", "batch_size_requested": 4, "batch_size_effective": 2,
                "batch_size_histogram": {"2": 10, "1": 1}, "batch_oom_retries": 1,
                "batch_oom_attempts": [4], "inference_seconds": 12.5, "elapsed_seconds": 18,
                "asr_execution": {"elapsed_seconds": 20, "torch_peak_allocated_bytes": 4000,
                                  "torch_peak_reserved_bytes": 5000, "python": "private/path", "text": "secret transcript"}}
    result = TranscriptResult(text="[00:00:00] hello\n", status="verified", source_level="asr", backend="asr.qwen3", metadata=metadata)
    package = build_package_metadata(item, result, created_at="2026-11-09", updated_at="2026-11-09")
    path = tmp_path / "runlog.jsonl"
    append_runlog_entry(path, build_item_runlog_entry(item, result))
    runlog = json.loads(path.read_text(encoding="utf-8"))
    assert runlog["asr_execution"] == package["asr_execution"]
    assert runlog["asr_execution"]["elapsed_seconds"] == 20
    assert runlog["asr_execution"]["batch_size_histogram"] == {"1": 1, "2": 10}
    assert runlog["asr_execution"]["torch_peak_reserved_bytes"] == 5000
    assert "private/path" not in json.dumps(package)
    assert "secret transcript" not in path.read_text(encoding="utf-8")


def test_asr_metrics_only_finite_numeric_fields_and_bounded_histogram():
    assert sanitize_asr_execution(None) == {}
    value = sanitize_asr_execution({"elapsed_seconds": math.inf, "inference_seconds": -1,
                                    "batch_size_requested": True, "text": "words", "python": "path",
                                    "batch_oom_attempts": [32,16,8,4,2,1]*100,
                                    "batch_size_histogram": {"1": 4, "0": 2, "33": 3, "2": "bad", "3": True}})
    assert value == {"batch_oom_attempts": [32,16,8,4,2,1], "batch_size_histogram": {"1": 4}}
