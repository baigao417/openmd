"""Safe local-only doctor projections for Openmd capability profiles."""

from __future__ import annotations

import importlib.util
import shutil
from typing import Any

from .capabilities import validate_capability_manifest


DOCTOR_PROFILES = ("all", "core", "web", "media", "documents", "authenticated-social", "tools")


def _module_available(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _media_diagnostics() -> dict[str, Any]:
    from openmd.runtime.interpreters import select_asr_runtime
    from openmd.runtime.models import configured_models
    from openmd.runtime.prepared_models import preparation_status

    runtime = select_asr_runtime()
    ffmpeg_available = shutil.which("ffmpeg") is not None
    ready = bool(runtime.probe.get("ready")) and ffmpeg_available
    next_action = "Local CUDA ASR runtime and models are ready" if ready else runtime.reason
    if not ffmpeg_available:
        next_action = "Install ffmpeg and make it available on PATH, then rerun openmd doctor --profile media"
    models = configured_models()
    qwen = models.get("qwen3-asr-1.7b")
    preparation = preparation_status(qwen.path, "qwen3-asr-1.7b") if qwen and qwen.path else {}
    if preparation.get("prepare_recommended"):
        next_action += "; reduce local model loading memory: openmd asr prepare-models (writes a separate copy)"
    return {
        "ffmpeg_on_path": ffmpeg_available,
        "local_asr_dependency": _module_available("faster_whisper"),
        "asr_runtime": runtime.as_dict(),
        "models": {key: model.as_dict() for key, model in models.items()},
        "model_preparation": preparation,
        "ready": ready,
        "next_action": next_action,
    }


def _document_diagnostics() -> dict[str, Any]:
    from openmd.ocr.config import settings
    from openmd.ocr.layout import probe_layout
    from openmd.ocr.office import soffice_path
    from openmd.ocr.ollama import probe_ollama
    from openmd.runtime.gpu_lock import gpu_lock_path
    from openmd.runtime.gpu_quarantine import quarantine_path

    try:
        ollama = probe_ollama(settings())
    except ValueError as exc:
        ollama = {"reachable": False, "ready": False, "models": {}, "error_code": getattr(exc, "code", "ocr_setting_invalid")}
    layout = probe_layout()
    layout.update({"device": "cpu", "cpu_only": True, "cuda_visible_policy": "reject", "gpu_lease_inherited": False})
    dependencies = {"pymupdf_available": _module_available("pymupdf"), "pillow_available": _module_available("PIL"), "numpy_available": _module_available("numpy")}
    ready = all(dependencies.values()) and ollama["ready"] and layout["ready"]
    return {"markitdown_available": _module_available("markitdown"), "ocr_dependency_available": _module_available("rapidocr_onnxruntime"), **dependencies, "ollama": ollama, "layout_runtime": layout, "libreoffice_available": soffice_path() is not None, "gpu_lock": {"path": str(gpu_lock_path()), "implementation": "OS file lock shared with ASR", "crash_recovery": "OS releases lease; persistent file is reused", "quarantine_exists": quarantine_path().exists(), "quarantine_recovery": "read-only wait for empty /api/ps; never unload other clients", "marker_only_reset": "openmd doctor --clear-gpu-quarantine"}, "ready": ready, "network_access": "configured_ollama_metadata_only", "next_action": "Document OCR dependencies are ready" if ready else "Start Ollama yourself and provide existing models / configure ocr_python and model_roots; Openmd does not install, download, pull or start services."}


def build_doctor_report(profile: str) -> dict[str, Any]:
    if profile not in DOCTOR_PROFILES:
        raise ValueError(f"unsupported doctor profile: {profile}")
    try:
        validate_capability_manifest()
        manifest_valid = True
    except ValueError:
        manifest_valid = False
    diagnostics: dict[str, Any] = {
        "profile": profile,
        "manifest_valid": manifest_valid,
        "network_access": "not_probed",
        "private_session_state": "not_read",
        "account_actions": "not_performed",
        "ready": manifest_valid,
    }
    from openmd.engines.diagnostics import document_formats
    diagnostics['document_formats'] = document_formats()
    from openmd.runtime.opencli_patches import patch_diagnostics
    diagnostics['opencli_patches'] = patch_diagnostics()
    from openmd.routing.loader import route_verification_status
    diagnostics['route_verification'] = route_verification_status()
    from openmd.runtime.recommendations import hardware, recommend
    diagnostics['hardware'] = hardware()
    diagnostics['model_recommendation'] = recommend(diagnostics['hardware'])
    if profile == "all":
        diagnostics.update(_media_diagnostics())
        diagnostics["openmd_on_path"] = shutil.which("openmd") is not None
        diagnostics["ready"] = bool(diagnostics["ready"] and manifest_valid)
        diagnostics["optional_profiles"] = {
            "web": {"opencli_on_path": shutil.which("opencli") is not None, "network_gate": "explicit_opt_in"},
            "documents": _document_diagnostics(),
        }
    elif profile == "core":
        diagnostics.update({"openmd_on_path": shutil.which("openmd") is not None})
    elif profile == "web":
        diagnostics.update({"opencli_on_path": shutil.which("opencli") is not None, "network_gate": "explicit_opt_in"})
    elif profile == "media":
        diagnostics.update(_media_diagnostics())
        diagnostics["ready"] = bool(diagnostics["ready"] and manifest_valid)
    elif profile == "documents":
        diagnostics.update(_document_diagnostics())
        diagnostics["ready"] = manifest_valid and diagnostics["ready"]
    elif profile == "tools":
        # Populated by the tool-card diagnostics without executing a command.
        from openmd.routing.providers import doctor_tools

        tool_report = doctor_tools()
        diagnostics["tools"] = tool_report
        diagnostics["ready"] = manifest_valid and bool(tool_report.get("ok", False))
    else:
        diagnostics.update({"session_owner": "browser_platform_or_sidecar", "private_session_probe": False})
    return diagnostics
