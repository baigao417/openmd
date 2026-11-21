"""Local path/protocol tests; real recognition is checked with bounded audio smoke."""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
import shutil
import venv
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from openmd.runtime import models
from openmd.runtime.asr import WorkerASRBackend, deserialize_result, get_asr_backend, serialize_result
from openmd.runtime.interpreters import AsrRuntime, select_asr_runtime, worker_environment
from openmd.core.models import ExtractionComponent, TranscriptResult


def _model(path: Path, files=("config.json", "model.bin", "tokenizer.json")) -> Path:
    path.mkdir(parents=True)
    for name in files:
        (path / name).write_text("{}", encoding="utf-8")
    return path


def _isolated_models(monkeypatch, tmp_path):
    monkeypatch.setattr(models, "checkout_root", lambda: None)
    monkeypatch.setattr(models, "_local_config", lambda: {})
    monkeypatch.setattr(models.Path, "home", lambda: tmp_path / "empty-home")
    for key in ("OPENMD_MODEL_ROOTS", "HF_HOME", "HF_HUB_CACHE", "MODELSCOPE_CACHE"):
        monkeypatch.delenv(key, raising=False)


def test_explicit_local_model_and_incomplete_directory(monkeypatch, tmp_path):
    _isolated_models(monkeypatch, tmp_path)
    local = _model(tmp_path / "local")
    assert models.require_model(str(local), family="whisper") == local
    (local / "tokenizer.json").unlink()
    with pytest.raises(models.ModelNotFoundError, match="tokenizer.json"):
        models.require_model(str(local), family="whisper")


def test_configured_modelscope_and_hf_snapshot_resolution(monkeypatch, tmp_path):
    _isolated_models(monkeypatch, tmp_path)
    root = tmp_path / "model-root"
    expected = _model(root / "models" / "mobiuslabsgmbh--faster-whisper-large-v3-turbo" / "snapshots" / "master")
    monkeypatch.setenv("OPENMD_MODEL_ROOTS", str(root))
    assert models.require_model("large-v3-turbo", family="whisper") == expected
    monkeypatch.delenv("OPENMD_MODEL_ROOTS")
    hub = tmp_path / "hf"
    cache = hub / "models--Systran--faster-whisper-large-v3"
    expected = _model(cache / "snapshots" / "revision")
    (cache / "refs").mkdir()
    (cache / "refs" / "main").write_text("revision")
    monkeypatch.setenv("HF_HUB_CACHE", str(hub))
    result = models.resolve_model("large-v3", family="whisper")
    assert result.path == expected
    assert result.origin == "huggingface_cache"
    assert str(tmp_path) not in json.dumps(result.as_dict())


def test_missing_model_does_not_fall_through_to_download(monkeypatch, tmp_path):
    _isolated_models(monkeypatch, tmp_path)
    backend_module = importlib.import_module("openmd.adapters.local_audio.backends.faster_whisper")
    audio = tmp_path / "speech.wav"
    audio.write_bytes(b"fixture")
    backend = backend_module.FasterWhisperASRBackend(model_size=str(tmp_path / "missing"))
    monkeypatch.setattr(backend, "is_available", lambda: True)
    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace(WhisperModel=lambda *a, **k: pytest.fail("model constructor must not run for missing models")))
    result = backend.transcribe(audio)
    assert result.error_code == "asr_model_missing"
    assert result.status == "failed"
    assert result.metadata["source_ref"] == "speech.wav"


def test_auto_missing_detector_returns_model_error_without_fallback(monkeypatch, tmp_path):
    from openmd.adapters.local_audio.backends.auto import AutoASRBackend

    _isolated_models(monkeypatch, tmp_path)
    monkeypatch.setenv("OPENMD_ASR_AUTO_EN_MODEL", str(tmp_path / "missing"))
    backend = AutoASRBackend()
    monkeypatch.setattr(backend, "is_available", lambda: True)
    audio = tmp_path / "speech.wav"
    audio.write_bytes(b"fixture")
    result = backend.transcribe(audio)
    assert result.error_code == "asr_model_missing"


def test_qwen_missing_shard_is_rejected(monkeypatch, tmp_path):
    _isolated_models(monkeypatch, tmp_path)
    directory = _model(tmp_path / "qwen", ("config.json", "tokenizer_config.json", "preprocessor_config.json", "tokenizer.json", "first.safetensors"))
    (directory / "model.safetensors.index.json").write_text(json.dumps({"weight_map": {"a": "first.safetensors", "b": "missing.safetensors"}}))
    resolved = models.resolve_model(str(directory), family="qwen3")
    assert resolved.path is None
    assert "weight shard" in resolved.missing


def test_offline_guard_restores_previous_environment(monkeypatch):
    monkeypatch.setenv("HF_HUB_OFFLINE", "0")
    monkeypatch.delenv("MODELSCOPE_OFFLINE", raising=False)
    with models.offline_model_loading():
        assert os.environ["HF_HUB_OFFLINE"] == "1"
        assert os.environ["MODELSCOPE_OFFLINE"] == "1"
    assert os.environ["HF_HUB_OFFLINE"] == "0"
    assert "MODELSCOPE_OFFLINE" not in os.environ


def test_worker_result_protocol_checks_status_metadata_and_components():
    result = TranscriptResult(text="[00:00:00] 原文", status="verified", source_level="asr", backend="asr.qwen3", components=[ExtractionComponent(id="speech", kind="text", status="verified", source_level="asr", backend="asr.qwen3")])
    assert deserialize_result(serialize_result(result)) == result
    broken = serialize_result(result)
    broken["result"]["metadata"] = []
    with pytest.raises(ValueError, match="metadata"):
        deserialize_result(broken)
    broken = serialize_result(result)
    broken["result"]["components"][0]["status"] = "wrong"
    with pytest.raises(ValueError, match="component status"):
        deserialize_result(broken)
    with pytest.raises(ValueError, match="no text"):
        deserialize_result(serialize_result(replace(result, text="")))


def test_factory_is_lazy_and_worker_environment_has_no_cloud_credentials(monkeypatch):
    monkeypatch.setenv("HF_TOKEN", "fixture-secret")
    monkeypatch.setenv("OPENMD_SILICONFLOW_API_KEY", "fixture-secret")
    monkeypatch.setenv("OPENMD_QWEN3_MODEL", "local-model")
    backend = get_asr_backend("asr.auto")
    assert isinstance(backend, WorkerASRBackend)
    env = worker_environment()
    assert "HF_TOKEN" not in env and "OPENMD_SILICONFLOW_API_KEY" not in env
    assert env["OPENMD_QWEN3_MODEL"] == "local-model"
    assert env["HF_HUB_OFFLINE"] == env["OPENMD_ASR_WORKER"] == "1"


def test_explicit_unhealthy_runtime_does_not_silently_fallback(monkeypatch, tmp_path):
    import openmd.runtime.interpreters as module

    monkeypatch.setenv("OPENMD_ASR_PYTHON", str(tmp_path / "missing-python"))
    monkeypatch.delenv("OPENMD_ASR_WORKER", raising=False)
    monkeypatch.setattr(module, "load_local_config", lambda: {})
    monkeypatch.setattr(module, "probe_asr_runtime", lambda *a, **k: {"ready": False})
    assert select_asr_runtime().mode == "unavailable"
    monkeypatch.setenv("OPENMD_ASR_WORKER", "1")
    assert select_asr_runtime().mode == "in_process"


def test_explicit_whisper_runtime_does_not_require_qwen_models(monkeypatch, tmp_path):
    import openmd.runtime.interpreters as module

    monkeypatch.setenv("OPENMD_ASR_PYTHON", str(tmp_path / "worker-python"))
    monkeypatch.delenv("OPENMD_ASR_WORKER", raising=False)
    monkeypatch.setattr(module, "load_local_config", lambda: {})
    monkeypatch.setattr(module, "probe_asr_runtime", lambda *a, **k: {"ready": False, "backend_readiness": {"asr.faster_whisper": True, "asr.auto": False}})
    assert select_asr_runtime(backend="asr.faster_whisper").mode == "worker"
    assert select_asr_runtime().mode == "unavailable"


def test_local_model_execution_config_omits_full_machine_path(monkeypatch, tmp_path):
    from openmd.adapters.local_audio.backends.faster_whisper import FasterWhisperASRBackend

    _isolated_models(monkeypatch, tmp_path)
    local = _model(tmp_path / "private-model")
    payload = FasterWhisperASRBackend(model_size=str(local)).execution_config()
    assert payload["model"] == "local-private-model"
    assert str(tmp_path) not in json.dumps(payload)


def test_worker_reports_malformed_json_without_trusting_exit_zero(monkeypatch, tmp_path):
    import openmd.runtime.asr as module

    audio = tmp_path / "speech.wav"
    audio.write_bytes(b"fixture")
    monkeypatch.setattr(module, "select_asr_runtime", lambda **kwargs: AsrRuntime("worker", Path(sys.executable), "fixture"))
    monkeypatch.setattr(module, "_timeout_for_audio", lambda path: 1)

    class Process:
        returncode = 0

        def __init__(self, command, **kwargs):
            Path(command[command.index("--result") + 1]).write_text('{"schema":"wrong"}')

        def poll(self):
            return 0

    monkeypatch.setattr(module, "owned_popen", Process)
    result = get_asr_backend().transcribe(audio)
    assert result.error_code == "asr_worker_failed"


def test_worker_timeout_stops_process_tree(monkeypatch, tmp_path):
    import openmd.runtime.asr as module

    audio = tmp_path / "speech.wav"
    audio.write_bytes(b"fixture")
    monkeypatch.setattr(module, "select_asr_runtime", lambda **kwargs: AsrRuntime("worker", Path(sys.executable), "fixture"))
    monkeypatch.setattr(module, "_timeout_for_audio", lambda path: 0.3)
    killed = []

    class Process:
        args = ["worker"]

        def __init__(self, command, **kwargs):
            pass

        def poll(self):
            return None

    monkeypatch.setattr(module, "owned_popen", Process)
    monkeypatch.setattr(module, "terminate_quietly", lambda process: killed.append(process))
    result = get_asr_backend().transcribe(audio)
    assert result.error_code == "asr_worker_timeout"
    assert len(killed) == 1


def test_worker_ctrl_c_propagates_and_removes_workdir(monkeypatch, tmp_path):
    import openmd.runtime.asr as module

    audio = tmp_path / "speech.wav"
    audio.write_bytes(b"fixture")
    monkeypatch.setattr(module, "select_asr_runtime", lambda **kwargs: AsrRuntime("worker", Path(sys.executable), "fixture"))
    monkeypatch.setattr(module, "_timeout_for_audio", lambda path: 300)
    monkeypatch.setattr(module.tempfile, "tempdir", str(tmp_path / "tmp"))
    (tmp_path / "tmp").mkdir()
    killed = []

    class Process:
        args = ["worker"]

        def __init__(self, command, **kwargs):
            pass

        def poll(self):
            raise KeyboardInterrupt

    monkeypatch.setattr(module, "owned_popen", Process)
    monkeypatch.setattr(module, "terminate_quietly", lambda process: killed.append(process))
    with pytest.raises(KeyboardInterrupt):
        get_asr_backend().transcribe(audio)
    assert len(killed) == 1
    assert not list((tmp_path / "tmp").glob("openmd-asr-worker-*"))


def test_installed_controller_cannot_shadow_worker_dependencies(monkeypatch, tmp_path):
    """A wheel's siblings and the caller cwd never enter isolated worker imports."""
    import openmd.runtime.interpreters as module

    controller_site = tmp_path / "controller-site"
    package = controller_site / "openmd"
    shutil.copytree(Path(module.__file__).resolve().parents[1], package, ignore=shutil.ignore_patterns("__pycache__"))
    (controller_site / "torch.py").write_text("raise RuntimeError('controller CPU torch shadowed CUDA worker')")
    hostile_cwd = tmp_path / "caller"
    hostile_cwd.mkdir()
    (hostile_cwd / "torch.py").write_text("raise RuntimeError('caller torch imported')")
    (hostile_cwd / "openmd").mkdir()
    (hostile_cwd / "openmd" / "__init__.py").write_text("raise RuntimeError('caller openmd imported')")
    worker = tmp_path / "worker"
    venv.EnvBuilder(with_pip=False).create(worker)
    python = worker / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    done = subprocess.run([str(python), "-I", "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"], capture_output=True, check=True, text=True)
    worker_site = Path(done.stdout.strip())
    (worker_site / "torch.py").write_text("from types import SimpleNamespace\nversion=SimpleNamespace(cuda='fixture-cuda')\ncuda=SimpleNamespace(is_available=lambda:True,get_device_name=lambda n:'worker GPU')\n")
    (worker_site / "ctranslate2.py").write_text("def get_cuda_device_count(): return 1\n")
    for name in module.REQUIRED_MODULES[2:]:
        (worker_site / (name + ".py")).write_text("")
    config = tmp_path / "local.json"
    config.write_text(json.dumps({"schema": "openmd.local/v1"}))
    monkeypatch.setenv("OPENMD_CONFIG", str(config))
    monkeypatch.setenv("PYTHONPATH", str(controller_site))
    monkeypatch.setattr(module, "__file__", str(package / "runtime" / "interpreters.py"))
    done = subprocess.run(module.worker_command(python, "--probe"), cwd=hostile_cwd, env=module.worker_environment(), capture_output=True, timeout=30, check=True)
    report = json.loads(done.stdout)
    assert report["torch_cuda_build"] == "fixture-cuda"
    assert report["torch_cuda_available"] is True
    assert report["gpu_name"] == "worker GPU"
    assert "PYTHONPATH" not in module.worker_environment()


@pytest.mark.parametrize("code", ["asr_runtime_probe_timeout", "asr_runtime_probe_failed"])
def test_transient_probe_failure_never_changes_to_cpu_fallback(monkeypatch, tmp_path, code):
    import openmd.runtime.interpreters as module

    monkeypatch.delenv("OPENMD_ASR_PYTHON", raising=False)
    monkeypatch.delenv("OPENMD_ASR_WORKER", raising=False)
    monkeypatch.setattr(module, "load_local_config", lambda: {})
    monkeypatch.setattr(module, "checkout_root", lambda: tmp_path)
    monkeypatch.setattr(module.importlib.util, "find_spec", lambda name: object())
    monkeypatch.setattr(module, "probe_asr_runtime", lambda *a, **kw: {"ready": False, "error_code": code})
    monkeypatch.setattr(models, "configured_models", lambda: {key: SimpleNamespace(path=tmp_path) for key in ("whisper-large-v3", "whisper-large-v3-turbo")})
    runtime = module.select_asr_runtime()
    assert runtime.mode == "unavailable"
    assert "probe did not complete" in runtime.reason


def test_cpu_fallback_doctor_never_claims_cuda_ready(monkeypatch):
    import openmd.core.doctor as doctor
    import openmd.runtime.interpreters as module

    reason = "No complete CUDA worker is ready; using the documented local Whisper fallback"
    monkeypatch.setattr(module, "select_asr_runtime", lambda: AsrRuntime("in_process", Path(sys.executable), reason, {"ready": False, "fallback_available": True}))
    monkeypatch.setattr(doctor.shutil, "which", lambda name: "fixture")
    monkeypatch.setattr(models, "configured_models", lambda: {})
    report = doctor._media_diagnostics()
    assert report["ready"] is False
    assert report["next_action"] == reason


def test_runtime_config_fingerprint_detects_interpreter_and_model_location_changes(monkeypatch, tmp_path):
    import openmd.core.local_config as local_config
    from openmd.runtime.asr import backend_execution_config

    _isolated_models(monkeypatch, tmp_path)
    monkeypatch.setattr(local_config, "load_local_config", lambda: {})
    python = tmp_path / "private-worker" / "python.exe"
    python.parent.mkdir()
    python.write_bytes(b"fixture")
    monkeypatch.setenv("OPENMD_ASR_PYTHON", str(python))
    first = backend_execution_config("asr.auto")
    assert str(tmp_path) not in json.dumps(first)
    monkeypatch.setenv("OPENMD_ASR_PYTHON", str(python.parent / "other-python.exe"))
    assert backend_execution_config("asr.auto")["runtime_config_id"] != first["runtime_config_id"]
    monkeypatch.setenv("OPENMD_ASR_PYTHON", str(python))
    monkeypatch.setenv("OPENMD_QWEN3_MODEL", str(tmp_path / "other-model"))
    assert backend_execution_config("asr.auto")["runtime_config_id"] != first["runtime_config_id"]


def test_transcript_metadata_uses_model_labels_instead_of_local_paths(monkeypatch, tmp_path):
    import openmd.adapters.local_audio.backends.faster_whisper as whisper
    from openmd.adapters.local_audio.backends.qwen3_asr import Qwen3ASRBackend

    _isolated_models(monkeypatch, tmp_path)
    model = _model(tmp_path / "private-model")
    audio = tmp_path / "speech.wav"
    audio.write_bytes(b"fixture")

    class WhisperModel:
        def __init__(self, *args, **kwargs):
            pass

        def transcribe(self, *args, **kwargs):
            return [SimpleNamespace(text="Original speech", start=0, end=1)], SimpleNamespace(language="en", duration=1)

    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace(WhisperModel=WhisperModel))
    monkeypatch.setattr(whisper, "_prepare_windows_dll_paths", lambda: None)
    backend = whisper.FasterWhisperASRBackend(model_size=str(model))
    monkeypatch.setattr(backend, "is_available", lambda: True)
    result = backend.transcribe(audio)
    assert result.metadata["model_size"] == "local-private-model"
    assert str(tmp_path) not in json.dumps(result.metadata)
    qwen = Qwen3ASRBackend(model=str(model))
    monkeypatch.setattr(qwen, "is_available", lambda: True)
    monkeypatch.setattr(qwen, "_run", lambda path: (["[00:00:00] Original speech"], ["English"], 1))
    result = qwen.transcribe(audio)
    assert result.metadata["model"] == "local-private-model"
    assert str(tmp_path) not in json.dumps(result.metadata)


def test_worker_resolves_relative_locations_before_changing_cwd(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    model = _model(tmp_path / "local-model")
    monkeypatch.setenv("OPENMD_CONFIG", "./local.json")
    monkeypatch.setenv("OPENMD_QWEN3_MODEL", "./local-model")
    monkeypatch.setenv("OPENMD_ASR_AUTO_EN_MODEL", "large-v3-turbo")
    monkeypatch.setenv("OPENMD_MODEL_ROOTS", "./model-root")
    env = worker_environment()
    assert env["OPENMD_CONFIG"] == str(tmp_path / "local.json")
    assert env["OPENMD_QWEN3_MODEL"] == str(model)
    assert env["OPENMD_ASR_AUTO_EN_MODEL"] == "large-v3-turbo"
    assert env["OPENMD_MODEL_ROOTS"] == str(tmp_path / "model-root")

def test_gpu_lock_uses_openmd_data_directory(monkeypatch, tmp_path):
    from openmd.runtime.gpu_lock import gpu_lock_path

    monkeypatch.setenv("OPENMD_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("OPENMD_RUNTIME_DIR", raising=False)
    assert gpu_lock_path() == (tmp_path / "data" / "runtime" / "gpu.lock").resolve()


def test_models_install_plan_defaults_under_data_directory(monkeypatch, tmp_path):
    from openmd.runtime.recommendations import install_plan

    monkeypatch.setenv("OPENMD_DATA_DIR", str(tmp_path / "data"))
    plan = install_plan("asr:baseline")
    assert Path(plan["root"]) == (tmp_path / "data" / "models")


def test_auto_runtime_accepts_only_baseline_model(monkeypatch, tmp_path):
    import openmd.runtime.interpreters as module
    from openmd.runtime.models import ResolvedModel

    monkeypatch.delenv("OPENMD_ASR_WORKER", raising=False)
    monkeypatch.delenv("OPENMD_ASR_PYTHON", raising=False)
    monkeypatch.setattr(module, "load_local_config", lambda: {})
    monkeypatch.setattr(module.importlib.util, "find_spec", lambda name: object() if name == "faster_whisper" else None)
    monkeypatch.setattr(module, "probe_asr_runtime", lambda *a, **k: {"ready": False})
    turbo = ResolvedModel("whisper-large-v3-turbo", tmp_path / "turbo", "configured_root")
    missing = ResolvedModel("whisper-large-v3", None, "missing", ("model.bin",))
    monkeypatch.setattr("openmd.runtime.models.configured_models", lambda: {
        "whisper-large-v3": missing, "whisper-large-v3-turbo": turbo,
    })
    runtime = module.select_asr_runtime(refresh=True)
    assert runtime.mode == "in_process"
    assert runtime.probe["fallback_available"] is True
    assert runtime.probe["available_models"] == ["whisper-large-v3-turbo"]


def test_discovery_marks_unknown_ctranslate_model_unverified(monkeypatch, tmp_path):
    _isolated_models(monkeypatch, tmp_path)
    root = tmp_path / "models"
    model = _model(root / "custom-small")
    monkeypatch.setenv("OPENMD_MODEL_ROOTS", str(root))
    results = models.discover_models()
    assert {"name": "custom-small", "origin": "unverified", "evaluated": False, "runnable": True} in results


def test_discovery_ignores_incomplete_unverified_model(monkeypatch, tmp_path):
    _isolated_models(monkeypatch, tmp_path)
    root = tmp_path / "models"
    _model(root / "incomplete", ("model.bin", "config.json"))
    monkeypatch.setenv("OPENMD_MODEL_ROOTS", str(root))
    assert models.discover_models() == []


def test_auto_selects_unverified_runnable_whisper_directory(monkeypatch, tmp_path):
    from openmd.adapters.local_audio.backends.auto import AutoASRBackend

    _isolated_models(monkeypatch, tmp_path)
    model = _model(tmp_path / "small-unreviewed")
    monkeypatch.setenv("OPENMD_MODEL_ROOTS", str(tmp_path))
    backend = AutoASRBackend()
    monkeypatch.setattr(backend, "_whisper_device", lambda: {"device": "cpu", "compute_type": "int8"})
    monkeypatch.setattr(backend, "_device", lambda: ("cpu", "int8"))
    selected, reason = backend.choose("en")
    assert selected.model_size == str(model)
    assert "small-unreviewed" in reason


def test_model_install_noninteractive_returns_action_without_download(monkeypatch, tmp_path, capsys):
    from argparse import Namespace
    from openmd.runtime import recommendations

    monkeypatch.setenv("OPENMD_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(recommendations.sys.stdin, "isatty", lambda: False)
    assert recommendations.cmd_models_install(Namespace(tier="asr:baseline", root=None, dry_run=False)) == 2
    assert "openmd models install asr:baseline" in capsys.readouterr().out
    assert not (tmp_path / "data" / "models").exists()


def test_model_install_uses_huggingface_mirror_after_primary_failure(monkeypatch, tmp_path, capsys):
    from argparse import Namespace
    from types import SimpleNamespace
    from openmd.runtime import recommendations

    root = tmp_path / "models"
    monkeypatch.setenv("OPENMD_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(recommendations.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: "y")
    endpoints = []

    def download(*, repo_id, revision, local_dir, endpoint):
        endpoints.append(endpoint)
        if endpoint == "https://huggingface.co":
            raise OSError("primary unavailable")
        directory = Path(local_dir)
        for name in ("model.bin", "config.json", "tokenizer.json"):
            (directory / name).write_bytes(b"fixture")

    monkeypatch.setitem(sys.modules, "huggingface_hub", SimpleNamespace(snapshot_download=download))
    assert recommendations.cmd_models_install(Namespace(tier="asr:baseline", root=root, dry_run=False)) == 0
    assert endpoints == ["https://huggingface.co", "https://hf-mirror.com"]
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["downloads"][0]["verified"]


def test_model_install_uses_modelscope_after_huggingface_routes_fail(monkeypatch, tmp_path, capsys):
    from argparse import Namespace
    from types import ModuleType, SimpleNamespace
    from openmd.runtime import recommendations

    monkeypatch.setenv("OPENMD_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(recommendations.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: "y")
    attempts = []

    def hf_download(**kwargs):
        attempts.append(kwargs["endpoint"])
        raise OSError("mirror unavailable")

    def modelscope_download(*, model_id, revision, local_dir):
        attempts.append("modelscope")
        directory = Path(local_dir)
        for name in ("model.bin", "config.json", "tokenizer.json"):
            (directory / name).write_bytes(b"fixture")

    ms_download = ModuleType("modelscope_hub.compat")
    ms_download.snapshot_download = modelscope_download
    monkeypatch.setitem(sys.modules, "modelscope_hub", ModuleType("modelscope_hub"))
    monkeypatch.setitem(sys.modules, "modelscope_hub.compat", ms_download)
    monkeypatch.setitem(sys.modules, "huggingface_hub", SimpleNamespace(snapshot_download=hf_download))
    assert recommendations.cmd_models_install(Namespace(tier="asr:baseline", root=None, dry_run=False)) == 0
    assert attempts == ["https://huggingface.co", "https://hf-mirror.com", "modelscope"]
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["downloads"][0]["verified"]


def test_doctor_marks_available_whisper_fallback_ready():
    from openmd.doctor_view import capability_rows

    rows = capability_rows({"profile_diagnostics": {"asr_runtime": {"probe": {"ready": False, "fallback_available": True}}}})
    asr = next(row for row in rows if row[1] == "无字幕视频 / 播客本机转录")
    assert asr[0] is True
