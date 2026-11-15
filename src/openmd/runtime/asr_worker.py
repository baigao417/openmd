"""Offline ASR worker. Only a result file carries transcript protocol data."""

from __future__ import annotations

import argparse
import contextlib
import importlib.metadata
import importlib.util
import json
import os
import platform
import sys
import time
from dataclasses import replace
from pathlib import Path


def build_probe() -> dict:
    from .interpreters import REQUIRED_MODULES, display_path
    from .models import configured_models

    modules = {name: importlib.util.find_spec(name) is not None for name in REQUIRED_MODULES}
    versions = {}
    for name, distribution in (("torch", "torch"), ("ctranslate2", "ctranslate2"), ("faster_whisper", "faster-whisper"), ("qwen_asr", "qwen-asr"), ("funasr", "funasr")):
        try:
            versions[name] = importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
    report = {"schema": "openmd.asr-probe/v1", "python": display_path(Path(sys.executable)), "version": platform.python_version(), "modules": modules, "versions": versions, "torch_cuda_build": None, "torch_cuda_available": False, "ctranslate2_cuda_devices": 0, "gpu_name": None, "models": {key: model.as_dict() for key, model in configured_models().items()}, "ready": False}
    errors = []
    try:
        if modules["torch"]:
            import torch

            report["torch_cuda_build"] = torch.version.cuda
            report["torch_cuda_available"] = torch.cuda.is_available()
            if report["torch_cuda_available"]:
                report["gpu_name"] = torch.cuda.get_device_name(0)
        if modules["faster_whisper"]:
            from openmd.adapters.local_audio.backends.faster_whisper import _prepare_windows_dll_paths

            _prepare_windows_dll_paths()
        if modules["ctranslate2"]:
            import ctranslate2

            report["ctranslate2_cuda_devices"] = ctranslate2.get_cuda_device_count()
    except Exception as exc:
        from openmd.core.secrets import sanitize_error_summary

        errors.append(sanitize_error_summary(exc))
    report["ready"] = all(modules.values()) and report["torch_cuda_available"] and report["ctranslate2_cuda_devices"] > 0 and all(model["available"] for model in report["models"].values())
    report["backend_readiness"] = {
        "asr.faster_whisper": modules["faster_whisper"] and modules["ctranslate2"] and report["models"]["whisper-large-v3"]["available"],
        "asr.qwen3": all(modules[key] for key in ("torch", "qwen_asr", "funasr", "soundfile")) and report["torch_cuda_available"] and all(report["models"][key]["available"] for key in ("qwen3-asr-1.7b", "fsmn-vad-zh")),
        "asr.auto": report["ready"],
    }
    if os.getenv("OPENMD_ASR_AUTO_ZH_ENGINE") == "whisper":
        report["backend_readiness"]["asr.auto"] = report["backend_readiness"]["asr.faster_whisper"] and report["models"]["whisper-large-v3-turbo"]["available"]
    report["errors"] = errors
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--backend", choices=("asr.auto", "asr.faster_whisper", "asr.qwen3"))
    parser.add_argument("--input", type=Path)
    parser.add_argument("--result", type=Path)
    args = parser.parse_args(argv)
    os.environ.update({"OPENMD_ASR_WORKER": "1", "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "MODELSCOPE_OFFLINE": "1"})
    if args.probe:
        with contextlib.redirect_stdout(sys.stderr):
            report = build_probe()
        print(json.dumps(report, ensure_ascii=True, allow_nan=False))
        return 0
    if args.backend is None or args.input is None or args.result is None:
        parser.error("--backend, --input and --result are required")
    from .asr import serialize_result
    from .gpu_quarantine import GPUQuarantined

    # Share a structured failure with the controller; never initialize an ASR
    # model when quarantine recovery fails inside the lease acquisition.
    try:
        return _transcribe_locked(args)
    except GPUQuarantined as exc:
        from .asr import failed_result

        args.result.write_text(json.dumps(serialize_result(failed_result(args.backend, exc.code, exc))), encoding="utf-8")
        return 0


def _transcribe_locked(args) -> int:
    from .asr import local_backend, serialize_result
    from .gpu_lock import gpu_lock, release_torch_cache

    # The inference process owns the lease. If its controller crashes, an orphan
    # worker keeps the lease until its inference exits, so a new OCR cannot race it.
    with gpu_lock():
        started = time.perf_counter()
        try:
            with contextlib.redirect_stdout(sys.stderr):
                result = local_backend(args.backend).transcribe(args.input)
            torch = sys.modules.get("torch")
            execution = {"python_version": platform.python_version(), "torch_cuda_build": None, "torch_cuda_available": False,
                         "elapsed_seconds": round(time.perf_counter() - started, 3)}
            if torch is not None:
                execution.update({"torch_cuda_build": torch.version.cuda, "torch_cuda_available": torch.cuda.is_available()})
                if execution["torch_cuda_available"]:
                    execution.update({"gpu_name": torch.cuda.get_device_name(0), "torch_peak_allocated_bytes": torch.cuda.max_memory_allocated(),
                                      "torch_peak_reserved_bytes": torch.cuda.max_memory_reserved()})
        finally:
            release_torch_cache()
    result = replace(result, metadata={**result.metadata, "asr_execution": execution})
    args.result.write_text(json.dumps(serialize_result(result), ensure_ascii=False, allow_nan=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
