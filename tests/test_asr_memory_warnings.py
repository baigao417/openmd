"""Memory estimates block below the peak; extra headroom is advisory."""
import pytest

from openmd.runtime import asr_memory


@pytest.mark.parametrize("origin,peak_mb", [("prepared", 2304), ("original", 5632), ("language_probe", 2560)])
def test_peak_is_the_hard_boundary_and_margin_only_warns(monkeypatch, origin, peak_mb):
    monkeypatch.setenv("OPENMD_ASR_" + origin.upper() + "_PEAK_MB", str(peak_mb))
    monkeypatch.setenv("OPENMD_ASR_MARGIN_MB", "1024")
    peak = peak_mb * asr_memory.MIB
    for available in (peak, peak + 1, peak + 1024 * asr_memory.MIB - 1):
        monkeypatch.setattr(asr_memory, "available_memory_bytes", lambda: available)
        report = asr_memory.check_memory(origin)
        assert report["memory_required_bytes"] == peak
        assert report["memory_warning_threshold_bytes"] == peak + 1024 * asr_memory.MIB
        assert report["memory_warning"] == "asr_memory_tight"
    monkeypatch.setattr(asr_memory, "available_memory_bytes", lambda: peak - 1)
    with pytest.raises(asr_memory.InsufficientASRMemory) as error:
        asr_memory.check_memory(origin)
    assert error.value.metrics["memory_required_bytes"] == peak
    assert "openmd asr prepare-models" in str(error.value)
    monkeypatch.setattr(asr_memory, "available_memory_bytes", lambda: peak + 1024 * asr_memory.MIB)
    assert "memory_warning" not in asr_memory.check_memory(origin)


def test_margin_changes_warning_without_changing_admission(monkeypatch):
    monkeypatch.setattr(asr_memory, "available_memory_bytes", lambda: 3334 * asr_memory.MIB)
    monkeypatch.setenv("OPENMD_ASR_LANGUAGE_PROBE_PEAK_MB", "2560")
    monkeypatch.setenv("OPENMD_ASR_MARGIN_MB", "1024")
    assert asr_memory.check_memory("language_probe")["memory_warning"] == "asr_memory_tight"
    monkeypatch.setenv("OPENMD_ASR_MARGIN_MB", "128")
    assert "memory_warning" not in asr_memory.check_memory("language_probe")


def test_probe_warning_survives_auto_result_runlog_and_resets(tmp_path, monkeypatch):
    from openmd.adapters.local_audio.backends.auto import AutoASRBackend
    from openmd.core.models import ContentItem, TranscriptResult
    from openmd.core.runlog import build_item_runlog_entry
    from openmd.core.secrets import sanitize_runlog_entry
    path = tmp_path / "audio.wav"
    path.write_bytes(b"audio")
    backend = AutoASRBackend()
    monkeypatch.setattr(backend, "is_available", lambda: True)
    probe = {"memory_warning": "asr_memory_tight", "memory_available_bytes": 3334 * asr_memory.MIB,
             "memory_required_bytes": 2560 * asr_memory.MIB, "memory_warning_threshold_bytes": 3584 * asr_memory.MIB}
    def detect(_):
        backend._probe_memory = probe
        return "zh", 1.0
    monkeypatch.setattr(backend, "detect_language", detect)
    class Prepared:
        def transcribe(self, _):
            return TranscriptResult(text="speech", status="verified", source_level="asr", backend="asr.qwen3",
                                    metadata={"model_origin": "prepared"})
    monkeypatch.setattr(backend, "choose", lambda _: (Prepared(), "test"))
    result = backend.transcribe(path)
    assert result.status == "verified"
    assert any("asr_memory_tight" in note for note in result.source_notes)
    item = ContentItem(url=path.as_uri(), platform="generic_media", input_type="file", source_type="audio")
    entry = sanitize_runlog_entry(build_item_runlog_entry(item, result))
    assert entry["asr_execution"]["model_origin"] == "prepared"
    assert entry["asr_execution"]["language_probe_memory"] == probe
    monkeypatch.setattr(backend, "detect_language", lambda _: ("zh", 1.0))
    assert not backend.transcribe(path).metadata.get("language_probe_memory")


def test_warning_receipts_allow_only_known_codes_and_numeric_fields():
    from openmd.core.secrets import sanitize_asr_execution
    value = sanitize_asr_execution({"memory_warning": "asr_memory_tight", "memory_warning_threshold_bytes": 100,
                                    "language_probe_memory": {"memory_warning": "asr_memory_tight", "memory_required_bytes": 20,
                                                              "unknown": "private", "memory_margin_bytes": -1}})
    assert value == {"memory_warning": "asr_memory_tight", "memory_warning_threshold_bytes": 100,
                     "language_probe_memory": {"memory_warning": "asr_memory_tight", "memory_required_bytes": 20}}
    assert sanitize_asr_execution({"memory_warning": "private", "language_probe_memory": "private"}) == {}


def test_measured_probe_default_and_larger_model_scaling(monkeypatch):
    monkeypatch.delenv("OPENMD_ASR_LANGUAGE_PROBE_PEAK_MB", raising=False)
    monkeypatch.setattr("openmd.core.local_config.load_local_config", lambda: {})
    monkeypatch.setattr(asr_memory, "available_memory_bytes", lambda: 16 * 1024**3)
    assert asr_memory.check_memory("language_probe")["memory_estimated_peak_bytes"] == 2496 * asr_memory.MIB
    assert asr_memory.check_memory("language_probe", largest_shard_bytes=3072 * asr_memory.MIB)["memory_required_bytes"] == 3840 * asr_memory.MIB


def test_qwen_warning_reaches_result_and_sanitized_receipt(tmp_path, monkeypatch):
    from openmd.adapters.local_audio.backends.qwen3_asr import Qwen3ASRBackend
    from openmd.core.secrets import asr_execution_metrics
    path = tmp_path / "audio.wav"
    path.write_bytes(b"audio")
    backend = Qwen3ASRBackend()
    monkeypatch.setattr(backend, "is_available", lambda: True)
    def run(_):
        backend._run_metadata = {"model_origin": "prepared", "memory_warning": "asr_memory_tight"}
        return ["[00:00:00] speech"], ["Chinese"], 5.0
    monkeypatch.setattr(backend, "_run", run)
    result = backend.transcribe(path)
    assert any("asr_memory_tight" in note for note in result.source_notes)
    assert asr_execution_metrics(result.metadata)["memory_warning"] == "asr_memory_tight"
