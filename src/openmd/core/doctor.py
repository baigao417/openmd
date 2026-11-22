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
    from openmd.media_tools import yt_dlp_diagnostics
    from openmd.runtime.interpreters import select_asr_runtime
    from openmd.runtime.models import configured_models, discover_models
    from openmd.runtime.prepared_models import preparation_status

    runtime = select_asr_runtime()
    from openmd.runtime.ffmpeg_tools import ffmpeg_source
    ffmpeg_source_value = ffmpeg_source()
    ffmpeg_available = ffmpeg_source_value != "missing"
    runtime_ready = bool(runtime.probe.get("ready") or runtime.probe.get("fallback_available"))
    # After `openmd models install asr:baseline` transcription works through the
    # in-process Whisper route; doctor must say so instead of reporting not-ready
    # because the optional full CUDA worker set is absent (round14 acceptance).
    ready = runtime_ready and ffmpeg_available
    available = [str(name) for name in runtime.probe.get("available_models", []) or []]
    if runtime.probe.get("ready"):
        next_action = "Local CUDA ASR runtime and models are ready"
    elif runtime.probe.get("fallback_available"):
        next_action = "本地语音转写可用（Whisper：" + ("、".join(available) or "已安装模型") + "）"
    else:
        next_action = runtime.reason
    if not ffmpeg_available:
        next_action = "Install openmd-cli[media] or system FFmpeg, then rerun openmd doctor --profile media"
    models = configured_models()
    qwen = models.get("qwen3-asr-1.7b")
    preparation = preparation_status(qwen.path, "qwen3-asr-1.7b") if qwen and qwen.path else {}
    if preparation.get("prepare_recommended"):
        next_action += "; reduce local model loading memory: openmd asr prepare-models (writes a separate copy)"
    return {
        "ffmpeg_on_path": ffmpeg_source_value == "system",
        "ffmpeg_source": ffmpeg_source_value,
        "local_asr_dependency": _module_available("faster_whisper"),
        "asr_runtime": runtime.as_dict(),
        "models": {key: model.as_dict() for key, model in models.items()},
        "discovered_models": discover_models(),
        "model_preparation": preparation,
        "yt_dlp": yt_dlp_diagnostics(),
        "ready": ready,
        "usable": runtime_ready and ffmpeg_available,
        "cuda_worker_ready": bool(runtime.probe.get("ready")),
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
    from openmd.engines.tools import MinerUEngine
    mineru = MinerUEngine().available()
    ready = all(dependencies.values()) and ollama["ready"] and layout["ready"]
    return {"markitdown_available": _module_available("markitdown"), "ocr_dependency_available": _module_available("rapidocr_onnxruntime"), **dependencies, "ollama": ollama, "layout_runtime": layout, "mineru": {**mineru, "install_policy": "user_managed_only", "next_action": None if mineru['ok'] else MinerUEngine.install_hint}, "libreoffice_available": soffice_path() is not None, "gpu_lock": {"path": str(gpu_lock_path()), "implementation": "OS file lock shared with ASR", "crash_recovery": "OS releases lease; persistent file is reused", "quarantine_exists": quarantine_path().exists(), "quarantine_recovery": "read-only wait for empty /api/ps; never unload other clients", "marker_only_reset": "openmd doctor --clear-gpu-quarantine"}, "ready": ready, "network_access": "configured_ollama_metadata_only", "next_action": "Document OCR dependencies are ready" if ready else "Start Ollama yourself and provide existing models / configure ocr_python and model_roots; Openmd does not install, download, pull or start services."}


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
    from openmd.platforms.xiaoyuzhou_auth import status as xiaoyuzhou_auth_status
    diagnostics["xiaoyuzhou_login"] = xiaoyuzhou_auth_status()
    from openmd.engines.diagnostics import document_formats
    diagnostics['document_formats'] = document_formats()
    from openmd.browser_diagnostics import browser_host_diagnostics
    diagnostics['browser_host'] = browser_host_diagnostics()
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
    host = diagnostics['browser_host']
    if host['registered'] and not host['ready']:
        diagnostics['ready'] = False
        diagnostics['next_action'] = host['next_action']
    return diagnostics
