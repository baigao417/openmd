"""Persistent uncertainty about an independent Ollama runner's GPU residency.

All operations run while the shared GPU lease is held. A marker is written
before inference, so process death cannot silently authorize another client.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path


class GPUQuarantined(RuntimeError):
    @property
    def code(self) -> str:
        return str(self).split(":", 1)[0]


def quarantine_path(lock_path: Path | None = None) -> Path:
    from .gpu_lock import gpu_lock_path

    return (lock_path or gpu_lock_path()).with_name("gpu-quarantine.json")


def mark_quarantine(url: str, model: str, *, reason: str = "inference_in_progress") -> None:
    from openmd.ocr.endpoint import validate_ollama_url

    target = quarantine_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    value = {"schema": "openmd.gpu-quarantine/v1", "pid": os.getpid(), "model": model,
             "url": validate_ollama_url(url), "time": datetime.now(timezone.utc).isoformat(), "reason": reason}
    temporary = target.with_suffix(f".{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, target)


def clear_quarantine(lock_path: Path | None = None) -> None:
    # Delete only the state marker, never the persistent OS lock inode.
    quarantine_path(lock_path).unlink(missing_ok=True)


def recover_quarantine(lock_path: Path | None = None, *, deadline: float | None = None) -> None:
    """Fail closed until /api/ps proves the recorded endpoint is empty."""
    target = quarantine_path(lock_path)
    if not target.exists():
        return
    from openmd.ocr.endpoint import validate_ollama_url
    from openmd.ocr.ollama import api_json, running_models

    try:
        value = json.loads(target.read_text(encoding="utf-8"))
        if value.get("schema") != "openmd.gpu-quarantine/v1" or not isinstance(value.get("model"), str):
            raise ValueError("invalid quarantine")
        url = validate_ollama_url(value["url"])
        # A model name or stale PID cannot establish ownership of requests on
        # this user-managed service. Recovery is strictly read-only.
        end = time.monotonic() if deadline is None else deadline
        while running_models(api_json(url, "/api/ps", timeout=max(0.1, min(3, end - time.monotonic())))):
            remaining = end - time.monotonic()
            if remaining <= 0:
                raise GPUQuarantined("gpu_external_busy: Ollama still has resident models; wait for the other client or keep_alive expiry and retry. Openmd will not unload them. Inspect with openmd doctor --profile documents; explicit marker-only reset: openmd doctor --clear-gpu-quarantine")
            time.sleep(min(0.1, remaining))
        clear_quarantine(lock_path)
    except GPUQuarantined:
        raise
    except Exception as exc:
        raise GPUQuarantined("gpu_quarantined: cannot confirm empty Ollama state; no GPU model was loaded. Inspect the service; explicit marker-only reset: openmd doctor --clear-gpu-quarantine") from exc


def clear_quarantine_manually() -> dict:
    """Explicit local marker reset under the OS lease; never contact Ollama."""
    from .gpu_lock import gpu_lock

    with gpu_lock(check_quarantine=False):
        target = quarantine_path()
        existed = target.exists()
        clear_quarantine()
    return {"marker_removed": existed, "models_unloaded": False, "ollama_contacted": False,
            "next_action": "Marker reset only. Ensure the shared GPU is safe before starting OCR or ASR."}
