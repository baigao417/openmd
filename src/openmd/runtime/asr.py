"""ASR factory and bounded worker protocol used by every controller entrypoint."""

from __future__ import annotations

import json
import hashlib
import math
import os
import subprocess
import tempfile
from dataclasses import asdict, fields, replace
from pathlib import Path
from typing import Any

from openmd.adapters.local_audio.asr import ASRBackend
from openmd.core.models import COMPONENT_STATUS_VALUES, FAILURE_CLASS_VALUES, SOURCE_LEVEL_VALUES, STATUS_VALUES, ExtractionComponent, TranscriptResult
from openmd.core.secrets import sanitize_error_summary
from .interpreters import owned_popen, AsrRuntime, display_path, kill_process_tree, process_options, remove_tree_with_retry, select_asr_runtime, terminate_quietly, wait_interruptibly, worker_command, worker_environment

from openmd.runtime.tempdirs import owned_mkdtemp
from openmd.runtime.interpreters import run_interruptibly


ASR_BACKENDS = ("asr.auto", "asr.faster_whisper", "asr.qwen3")


def local_backend(name: str) -> ASRBackend:
    if name == "asr.auto":
        from openmd.adapters.local_audio.backends.auto import AutoASRBackend

        return AutoASRBackend()
    if name == "asr.faster_whisper":
        from openmd.adapters.local_audio.backends.faster_whisper import FasterWhisperASRBackend

        return FasterWhisperASRBackend()
    if name == "asr.qwen3":
        from openmd.adapters.local_audio.backends.qwen3_asr import Qwen3ASRBackend

        return Qwen3ASRBackend()
    raise ValueError(f"Unknown local ASR backend: {name}")


def backend_execution_config(name: str) -> dict[str, Any]:
    """Constructing fingerprints never starts a GPU probe or imports a model."""
    from openmd.core.local_config import load_local_config
    from .models import checkout_root

    config = local_backend(name).execution_config()
    local = load_local_config()
    explicit = os.getenv("OPENMD_ASR_PYTHON") or local.get("asr_python")
    root = checkout_root()
    candidates = [Path(explicit).expanduser()] if explicit else [Path(os.sys.executable)]
    if not explicit and root:
        candidates.append(root / "out/asr-bench/venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python"))
    identities = []
    for candidate in candidates:
        if not candidate.is_absolute():
            candidate = (root or Path.cwd()) / candidate
        try:
            stamp = candidate.stat().st_mtime_ns
        except OSError:
            stamp = None
        identities.append({"path": str(candidate.resolve()), "mtime_ns": stamp})
    # Hash locations/settings for resume identity without publishing machine paths
    # or starting a GPU probe during argument parsing.
    settings = {key: value for key, value in os.environ.items() if key.startswith(("OPENMD_FASTER_WHISPER_", "OPENMD_ASR_", "OPENMD_QWEN3_")) or key in {"OPENMD_MODEL_ROOTS", "OPENMD_PREPARED_MODEL_ROOT", "CUDA_VISIBLE_DEVICES", "HF_HOME", "HF_HUB_CACHE", "MODELSCOPE_CACHE"}}
    identity = {"interpreters": identities, "model_roots": local.get("model_roots", []), "asr_models": local.get("asr_models", {}), "asr_memory": local.get("asr_memory", {}), "settings": settings, "backend": config}
    config_id = hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=True).encode()).hexdigest()
    return {**config, "runtime_policy": "local_worker_v2", "runtime_configured": bool(explicit), "runtime_source": "explicit" if explicit else "checkout" if root else "current_interpreter", "runtime_config_id": config_id}


def get_asr_backend(name: str = "asr.auto") -> ASRBackend:
    if name not in ASR_BACKENDS:
        raise ValueError(f"Unknown local ASR backend: {name}")
    # Resolve lazily: provider registration, health checks and execution fingerprints
    # must not load torch or wait for a worker probe.
    return WorkerASRBackend(name)


def failed_result(name: str, code: str, error: object) -> TranscriptResult:
    return TranscriptResult(text="", status="failed", source_level="partial_extraction", backend=name, error_code=code, error_summary=sanitize_error_summary(error), source_notes=["Local ASR did not produce usable text; no cloud service or model download was used."], metadata={"profile": "media", "asr_backend": name})


def serialize_result(result: TranscriptResult) -> dict[str, Any]:
    return {"schema": "openmd.asr-result/v1", "result": asdict(result)}


def deserialize_result(payload: object) -> TranscriptResult:
    if not isinstance(payload, dict) or payload.get("schema") != "openmd.asr-result/v1":
        raise ValueError("invalid ASR worker schema")
    result = payload.get("result")
    if not isinstance(result, dict) or set(result) - {item.name for item in fields(TranscriptResult)}:
        raise ValueError("invalid ASR result fields")
    if not isinstance(result.get("text"), str) or result.get("status") not in STATUS_VALUES or result.get("source_level") not in SOURCE_LEVEL_VALUES or result.get("backend") not in ASR_BACKENDS:
        raise ValueError("invalid ASR result status, source or backend")
    for key in ("error_code", "error_summary", "title", "author", "published_at", "extraction_contract_version"):
        if result.get(key) is not None and not isinstance(result[key], str):
            raise ValueError(f"invalid ASR result {key}")
    notes = result.get("source_notes", [])
    metadata = result.get("metadata", {})
    if not isinstance(notes, list) or not all(isinstance(note, str) for note in notes) or not isinstance(metadata, dict):
        raise ValueError("invalid ASR notes or metadata")
    components = result.get("components", [])
    if not isinstance(components, list):
        raise ValueError("invalid ASR components")
    decoded: list[ExtractionComponent] = []
    for component in components:
        if not isinstance(component, dict) or set(component) - {item.name for item in fields(ExtractionComponent)}:
            raise ValueError("invalid ASR component fields")
        if component.get("status") not in COMPONENT_STATUS_VALUES or component.get("source_level") not in SOURCE_LEVEL_VALUES:
            raise ValueError("invalid ASR component status")
        if any(not isinstance(component.get(key), str) for key in ("id", "kind", "backend")):
            raise ValueError("invalid ASR component identity")
        if component.get("failure_class") is not None and component["failure_class"] not in FAILURE_CLASS_VALUES:
            raise ValueError("invalid ASR component failure class")
        for key in ("text_ref", "terminal_reason", "error_code"):
            if component.get(key) is not None and not isinstance(component[key], str):
                raise ValueError(f"invalid ASR component {key}")
        warnings = component.get("warnings", [])
        if not isinstance(warnings, list) or any(not isinstance(warning, str) for warning in warnings):
            raise ValueError("invalid ASR component warnings")
        decoded.append(ExtractionComponent(**component))
    if result["status"] in {"verified", "partial"} and not result["text"].strip():
        raise ValueError("successful ASR result has no text")
    return TranscriptResult(**{**result, "source_notes": notes, "metadata": metadata, "components": decoded})


def _timeout_for_audio(path: Path) -> float:
    try:
        done = run_interruptibly(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(path)], stdin=subprocess.DEVNULL, capture_output=True, timeout=10, check=True)
        duration = float(done.stdout.strip())
        return max(600.0, min(duration * 2, 12 * 60 * 60)) if math.isfinite(duration) and duration >= 0 else 600.0
    except (OSError, ValueError, subprocess.SubprocessError):
        return 600.0


class WorkerASRBackend(ASRBackend):
    def __init__(self, name: str, python: Path | None = None) -> None:
        self.name = name
        self.python = python

    def execution_config(self) -> dict[str, Any]:
        return backend_execution_config(self.name)

    def is_available(self) -> bool:
        return self._runtime().mode != "unavailable"

    def _runtime(self) -> AsrRuntime:
        if self.python is not None:
            return AsrRuntime("worker", self.python, "Explicit worker instance")
        return select_asr_runtime(backend=self.name)

    def transcribe(self, path: Path) -> TranscriptResult:
        from .gpu_quarantine import GPUQuarantined

        try:
            return self._transcribe(path)
        except TimeoutError as exc:
            return failed_result(self.name, "gpu_lock_timeout", exc)
        except GPUQuarantined as exc:
            return failed_result(self.name, exc.code, exc)

    def _transcribe(self, path: Path) -> TranscriptResult:
        path = path.expanduser().resolve()
        if not path.is_file():
            return failed_result(self.name, "asr_file_not_found", "Local audio file not found")
        try:
            runtime = self._runtime()
        except (OSError, ValueError) as exc:
            return failed_result(self.name, "asr_runtime_unavailable", exc)
        if runtime.mode == "in_process":
            from .gpu_lock import gpu_lock, release_torch_cache

            with gpu_lock():
                try:
                    result = local_backend(self.name).transcribe(path)
                finally:
                    release_torch_cache()
            return replace(result, source_notes=[*result.source_notes, runtime.reason], metadata={**result.metadata, "asr_runtime": {"mode": "in_process", "reason": runtime.reason}})
        if runtime.mode == "unavailable" or runtime.python is None:
            reports = runtime.probe.get("attempts", [runtime.probe])
            missing = sorted({name for report in reports if isinstance(report, dict) for name, model in report.get("models", {}).items() if not model.get("available", False)})
            if missing:
                return failed_result(self.name, "asr_model_missing", runtime.reason + "; missing or incomplete local models: " + ", ".join(missing))
            return failed_result(self.name, "asr_runtime_unavailable", runtime.reason)
        workdir = owned_mkdtemp(prefix="openmd-asr-worker-")
        try:
            result_path = Path(workdir) / "result.json"
            command = worker_command(runtime.python, "--backend", self.name, "--input", str(path), "--result", str(result_path))
            try:
                # Library chatter is bounded to a file and never becomes protocol data.
                with (Path(workdir) / "worker.log").open("wb") as log:
                    process = owned_popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=log, env=worker_environment(), cwd=workdir, **process_options())
                    try:
                        wait_interruptibly(process, _timeout_for_audio(path))
                    except subprocess.TimeoutExpired:
                        terminate_quietly(process)
                        return failed_result(self.name, "asr_worker_timeout", "Local ASR worker exceeded its bounded time limit")
                    except BaseException:
                        # Ctrl+C must surface as cancellation, never as a worker failure.
                        terminate_quietly(process)
                        raise
                if process.returncode:
                    with (Path(workdir) / "worker.log").open("rb") as log:
                        log.seek(max(0, log.seek(0, 2) - 2048))
                        detail = log.read().decode("utf-8", errors="replace")
                    return failed_result(self.name, "asr_worker_failed", detail or f"Worker exited with code {process.returncode}")
                if not result_path.is_file() or result_path.stat().st_size > 64 * 1024 * 1024:
                    raise ValueError("worker result missing or exceeds size limit")
                result = deserialize_result(json.loads(result_path.read_text(encoding="utf-8")))
                return replace(result, metadata={**result.metadata, "asr_runtime": {"mode": "worker", "python": display_path(runtime.python)}})
            except (OSError, ValueError, TypeError, subprocess.SubprocessError) as exc:
                return failed_result(self.name, "asr_worker_failed", exc)
        finally:
            # A killed worker's grandchild can briefly keep its cwd open on Windows; cleanup is
            # retried and best-effort so it can never replace the original exception.
            remove_tree_with_retry(Path(workdir))
