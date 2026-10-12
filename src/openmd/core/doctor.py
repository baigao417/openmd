"""Safe local-only doctor projections for Openmd capability profiles."""

from __future__ import annotations

import importlib.util
import shutil
from typing import Any

from .capabilities import validate_capability_manifest


DOCTOR_PROFILES = ("core", "web", "media", "documents", "authenticated-social")


def _module_available(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


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
    }
    if profile == "core":
        diagnostics.update({"openmd_on_path": shutil.which("openmd") is not None})
    elif profile == "web":
        diagnostics.update({"opencli_on_path": shutil.which("opencli") is not None, "network_gate": "explicit_opt_in"})
    elif profile == "media":
        diagnostics.update({"ffmpeg_on_path": shutil.which("ffmpeg") is not None, "local_asr_dependency": _module_available("faster_whisper")})
    elif profile == "documents":
        diagnostics.update({"markitdown_available": _module_available("markitdown"), "ocr_dependency_available": _module_available("rapidocr_onnxruntime")})
    else:
        diagnostics.update({"session_owner": "browser_platform_or_sidecar", "private_session_probe": False})
    return diagnostics
