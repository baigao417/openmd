"""Small synthetic weights exercise preparation without torch or model downloads."""
import json
import os
import struct

import pytest

from openmd.runtime import prepared_models as pm


def weights(path, tensors):
    header, offset, data = {}, 0, b""
    for key, value in tensors.items():
        header[key] = {"dtype": "U8", "shape": [len(value)], "data_offsets": [offset, offset + len(value)]}
        data += value
        offset += len(value)
    raw = json.dumps(header).encode()
    raw += b" " * (-len(raw) % 8)
    path.write_bytes(struct.pack("<Q", len(raw)) + raw + data)


@pytest.fixture
def source(tmp_path):
    src = tmp_path / "original"
    src.mkdir()
    weights(src / "model.safetensors", {"one": b"a" * 400, "two": b"b" * 400, "three": b"c" * 400})
    (src / "config.json").write_text('{"model_type":"test"}')
    return src


def test_prepare_is_bounded_verified_idempotent_and_preserves_source(source, tmp_path):
    before = {p.name: p.read_bytes() for p in source.iterdir()}
    dest = tmp_path / "prepared"
    result = pm.prepare_model(source, dest, max_shard_bytes=700)
    assert result["status"] == "prepared"
    assert pm.validate_prepared(source, dest)
    assert len(list(dest.glob("*.safetensors"))) == 3
    assert all(p.stat().st_size <= 700 for p in dest.glob("*.safetensors"))
    assert pm.prepare_model(source, dest, max_shard_bytes=700)["status"] == "already_prepared"
    assert {p.name: p.read_bytes() for p in source.iterdir()} == before


def test_verified_existing_copy_must_obey_requested_cap(source, tmp_path):
    dest = tmp_path / "prepared"
    pm.prepare_model(source, dest, max_shard_bytes=700)
    with pytest.raises(ValueError, match="requested shard limit"):
        pm.prepare_model(source, dest, max_shard_bytes=300)
    assert pm.validate_prepared(source, dest)


def test_corruption_and_source_change_reject_prepared(source, tmp_path):
    dest = tmp_path / "prepared"
    pm.prepare_model(source, dest, max_shard_bytes=700)
    shard = next(dest.glob("*.safetensors"))
    stat = shard.stat()
    raw = shard.read_bytes()
    shard.write_bytes(raw[:-1] + b"x")
    os.utime(shard, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert pm.validate_prepared(source, dest, verify_hashes=False)
    assert not pm.validate_prepared(source, dest)
    with pytest.raises(ValueError, match="destination"):
        pm.prepare_model(source, dest, max_shard_bytes=700)


def test_oversized_tensor_fails_without_publishing(source, tmp_path):
    dest = tmp_path / "prepared"
    with pytest.raises(ValueError, match="tensor.*limit"):
        pm.prepare_model(source, dest, max_shard_bytes=300)
    assert not dest.exists()


def test_interruption_can_be_retried(source, tmp_path, monkeypatch):
    dest = tmp_path / "prepared"
    original = pm._copy_range
    monkeypatch.setattr(pm, "_copy_range", lambda *a, **kw: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        pm.prepare_model(source, dest, max_shard_bytes=700)
    assert not dest.exists()
    monkeypatch.setattr(pm, "_copy_range", original)
    assert pm.prepare_model(source, dest, max_shard_bytes=700)["status"] == "prepared"


def test_rejects_index_traversal(source, tmp_path):
    (source / "model.safetensors.index.json").write_text(json.dumps({"weight_map": {"one": "../outside.safetensors"}}))
    with pytest.raises(ValueError):
        pm.prepare_model(source, tmp_path / "prepared", max_shard_bytes=700)


def test_source_change_invalidates_prepared(source, tmp_path):
    dest = tmp_path / "prepared"
    pm.prepare_model(source, dest, max_shard_bytes=700)
    (source / "config.json").write_text('{"changed":true}')
    assert not pm.validate_prepared(source, dest)


def test_chat_template_is_preserved_and_verified(source, tmp_path):
    template = source / "chat_template.json"
    template.write_text('{"chat_template":"fixture"}')
    dest = tmp_path / "prepared"
    pm.prepare_model(source, dest, max_shard_bytes=700)
    assert (dest / template.name).read_bytes() == template.read_bytes()
    assert pm.validate_prepared(source, dest)
    (dest / template.name).write_text('{"chat_template":"changed"}')
    assert not pm.validate_prepared(source, dest)


def test_no_rewrite_when_small_enough(source, tmp_path):
    dest = tmp_path / "prepared"
    assert pm.prepare_model(source, dest, max_shard_bytes=5000)["status"] == "not_needed"
    assert not dest.exists()


def test_malformed_dtype_shape_and_offsets_are_rejected(source, tmp_path):
    path = source / "model.safetensors"
    data = path.read_bytes()
    path.write_bytes(data.replace(b'"U8"', b'"XX"'))
    with pytest.raises(ValueError, match="dtype"):
        pm.prepare_model(source, tmp_path / "prepared", max_shard_bytes=700)


def test_select_prepared_or_original_and_doctor_hint(source, tmp_path, monkeypatch):
    monkeypatch.setattr(pm, "prepared_root", lambda: tmp_path / "models")
    dest = pm.prepared_path(source, "test")
    assert pm.select_model(source, "test") == (source, "original")
    pm.prepare_model(source, dest, max_shard_bytes=700)
    assert pm.select_model(source, "test") == (dest, "prepared")
    assert pm.preparation_status(source, "test")["model_origin"] == "prepared"


def test_provenance_and_guard_fields_survive_runlog_sanitizer():
    from openmd.core.secrets import sanitize_asr_execution
    value = sanitize_asr_execution({"model_origin": "original", "prepare_recommended": True,
                                    "memory_required_bytes": 1234, "unknown": "private"})
    assert value == {"model_origin": "original", "prepare_recommended": True,
                     "prepare_command": "openmd asr prepare-models", "memory_required_bytes": 1234}
    from openmd.core.runlog import classify_failure
    assert classify_failure("asr_insufficient_memory") == "user_resolvable"


def test_prepared_root_and_memory_settings_reach_isolated_worker(monkeypatch, tmp_path):
    from openmd.runtime.interpreters import worker_environment
    monkeypatch.setenv("OPENMD_PREPARED_MODEL_ROOT", str(tmp_path))
    monkeypatch.setenv("OPENMD_ASR_PREPARED_PEAK_MB", "2304")
    monkeypatch.setenv("OPENMD_ASR_MARGIN_MB", "1024")
    monkeypatch.setenv("OPENMD_ASR_LANGUAGE_PROBE_PEAK_MB", "2560")
    env = worker_environment()
    assert env["OPENMD_PREPARED_MODEL_ROOT"] == str(tmp_path.resolve())
    assert env["OPENMD_ASR_PREPARED_PEAK_MB"] == "2304"
    assert env["OPENMD_ASR_MARGIN_MB"] == "1024"
    assert env["OPENMD_ASR_LANGUAGE_PROBE_PEAK_MB"] == "2560"


def test_cli_prepare_dispatches_actual_preparer(source, tmp_path, capsys):
    from openmd.cli import main
    assert main(["asr", "prepare-models", "--model", str(source), "--max-shard-mb", "1", "--prepared-root", str(tmp_path / "models")]) == 1  # incomplete ASR model
    for name in ("tokenizer_config.json", "preprocessor_config.json", "tokenizer.json"):
        (source / name).write_text("{}")
    assert main(["asr", "prepare-models", "--model", str(source), "--max-shard-mb", "1", "--prepared-root", str(tmp_path / "models")]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "not_needed"


def test_memory_configuration_validation(tmp_path, monkeypatch):
    from openmd.core.local_config import load_local_config
    config = tmp_path / "local.json"
    monkeypatch.setenv("OPENMD_CONFIG", str(config))
    value = {"schema": "openmd.local/v1", "asr_models": {"prepared_root": "~/models"},
             "asr_memory": {"original_peak_mb": 5632, "prepared_peak_mb": 2304, "margin_mb": 1024}}
    config.write_text(json.dumps(value))
    assert load_local_config() == value
    value["asr_memory"]["margin_mb"] = 0
    config.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="asr_memory"):
        load_local_config()


def test_memory_guard_reports_boundaries(monkeypatch):
    from openmd.runtime import asr_memory
    monkeypatch.setattr(asr_memory, "available_memory_bytes", lambda: 3328 * 1024**2)
    assert asr_memory.check_memory("prepared")["memory_required_bytes"] == 3328 * 1024**2
    monkeypatch.setenv("OPENMD_ASR_MARGIN_MB", "2048")
    with pytest.raises(asr_memory.InsufficientASRMemory) as error:
        asr_memory.check_memory("prepared")
    assert error.value.metrics["memory_required_bytes"] == 4352 * 1024**2


def test_larger_prepared_shards_raise_memory_estimate(monkeypatch):
    from openmd.runtime import asr_memory
    monkeypatch.setattr(asr_memory, "available_memory_bytes", lambda: 16 * 1024**3)
    value = asr_memory.check_memory("prepared", largest_shard_bytes=2048 * 1024**2)
    assert value["memory_estimated_peak_bytes"] == 3584 * 1024**2


def test_memory_check_before_gpu_import(source, tmp_path, monkeypatch):
    from openmd.adapters.local_audio.backends import qwen3_asr as qwen
    from openmd.runtime.asr_memory import InsufficientASRMemory
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"audio")
    monkeypatch.setattr(qwen.Qwen3ASRBackend, "is_available", lambda self: True)
    monkeypatch.setattr(qwen, "require_model", lambda *a, **kw: source)
    monkeypatch.setattr(qwen, "check_memory", lambda *a, **kw: (_ for _ in ()).throw(InsufficientASRMemory("close programs")))
    result = qwen.Qwen3ASRBackend().transcribe(audio)
    assert result.error_code == "asr_insufficient_memory"
    assert result.metadata["model_origin"] == "original"


def test_memory_guard_fails_before_model_import(monkeypatch):
    from openmd.runtime import asr_memory
    monkeypatch.setattr(asr_memory, "available_memory_bytes", lambda: 1024)
    with pytest.raises(asr_memory.InsufficientASRMemory, match="cloud"):
        asr_memory.check_memory("original", largest_shard_bytes=4 * 1024**3)


def test_memory_failure_does_not_fall_back_to_whisper(tmp_path, monkeypatch):
    from openmd.adapters.local_audio.backends.auto import AutoASRBackend
    from openmd.adapters.local_audio.backends.qwen3_asr import Qwen3ASRBackend, _failed
    audio = tmp_path / "speech.wav"
    audio.write_bytes(b"audio")
    qwen = Qwen3ASRBackend()
    backend = AutoASRBackend(qwen3=qwen)
    monkeypatch.setattr(backend, "is_available", lambda: True)
    monkeypatch.setattr(backend, "detect_language", lambda p: ("zh", 1.0))
    monkeypatch.setattr(backend, "choose", lambda lang: (qwen, "test"))
    monkeypatch.setattr(qwen, "transcribe", lambda p: _failed(qwen.name, "asr_insufficient_memory", "close programs"))
    assert backend.transcribe(audio).error_code == "asr_insufficient_memory"


def test_language_probe_is_guarded_before_stack_import(tmp_path, monkeypatch):
    from openmd.adapters.local_audio.backends import auto
    from openmd.runtime import asr_memory
    monkeypatch.setattr(auto, "require_model", lambda *a, **kw: tmp_path)
    monkeypatch.setattr(asr_memory, "available_memory_bytes", lambda: 1024)
    monkeypatch.setattr(auto, "_load_torch_first", lambda: (_ for _ in ()).throw(AssertionError("stack imported")))
    with pytest.raises(asr_memory.InsufficientASRMemory):
        auto.AutoASRBackend().detect_language(tmp_path / "audio.wav")


def test_language_probe_memory_refusal_is_terminal(tmp_path, monkeypatch):
    from openmd.adapters.local_audio.backends.auto import AutoASRBackend, FasterWhisperASRBackend
    from openmd.runtime.asr_memory import InsufficientASRMemory
    path = tmp_path / "audio.wav"
    path.write_bytes(b"audio")
    backend = AutoASRBackend()
    monkeypatch.setattr(backend, "is_available", lambda: True)
    monkeypatch.setattr(backend, "detect_language", lambda p: (_ for _ in ()).throw(InsufficientASRMemory("memory refusal")))
    monkeypatch.setattr(FasterWhisperASRBackend, "transcribe", lambda *a: (_ for _ in ()).throw(AssertionError("fallback loaded")))
    assert backend.transcribe(path).error_code == "asr_insufficient_memory"
