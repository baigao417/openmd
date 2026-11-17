"""Conservative physical-memory check before importing the local ASR stack."""
from __future__ import annotations

import os
import sys

MIB = 1024**2


class InsufficientASRMemory(RuntimeError):
    def __init__(self, message: str, metrics: dict | None = None) -> None:
        super().__init__(message)
        self.metrics = metrics or {}


def available_memory_bytes() -> int:
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes
        class MemoryStatus(ctypes.Structure):
            _fields_ = [("length", wintypes.DWORD), ("load", wintypes.DWORD),
                        *[(name, ctypes.c_ulonglong) for name in ("total", "available", "page_total", "page_available", "virtual_total", "virtual_available", "extended")]]
        state = MemoryStatus()
        state.length = ctypes.sizeof(state)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(state)):
            raise OSError("physical memory probe failed")
        return state.available
    if sys.platform.startswith("linux"):
        from pathlib import Path
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) * 1024
    # psutil is part of typical ASR environments; no GPU or model import occurs.
    try:
        import psutil
        return psutil.virtual_memory().available
    except ImportError as exc:
        raise OSError("physical memory probe unavailable; install psutil in the ASR environment") from exc


def check_memory(origin: str, *, largest_shard_bytes: int = 0) -> dict:
    from openmd.core.local_config import load_local_config
    config = load_local_config().get("asr_memory", {})
    # Windows/CUDA measurements and receipt sources: docs/benchmarks/asr/local-memory.md.
    # Peak is the hard gate; the configurable margin only warns about headroom.
    baseline = {"prepared": 2304, "original": 5632, "language_probe": 2496}[origin]
    overhead = 768 if origin == "language_probe" else 1536
    default = max(baseline, largest_shard_bytes // MIB + overhead)
    def setting(name, fallback):
        value = os.getenv("OPENMD_ASR_" + name.upper() + "_MB", config.get(name + "_mb", fallback))
        if isinstance(value, str) and value.isdecimal():
            value = int(value)
        if type(value) is not int or value <= 0:
            raise ValueError("asr_memory_invalid: expected positive integer MiB")
        return value * MIB
    peak = setting(origin + "_peak", default)
    margin = setting("margin", 1024)
    try:
        available = available_memory_bytes()
    except OSError as exc:
        raise InsufficientASRMemory("asr_insufficient_memory: physical memory could not be checked; install psutil or use cloud ASR") from exc
    report = {"memory_available_bytes": available, "memory_required_bytes": peak,
              "memory_estimated_peak_bytes": peak, "memory_margin_bytes": margin,
              "memory_warning_threshold_bytes": peak + margin}
    if available < peak:
        raise InsufficientASRMemory(
            f"asr_insufficient_memory: available {available // MIB} MiB; need {peak // MIB} MiB "
            f"for {origin} model. Close other programs or explicitly use cloud ASR. "
            "Prepare smaller shards with openmd asr prepare-models; estimates are configurable.", report)
    if available < peak + margin:
        report["memory_warning"] = "asr_memory_tight"
    return report


def memory_warning_notes(report: dict) -> list[str]:
    if report.get("memory_warning") == "asr_memory_tight":
        return ["asr_memory_tight: available physical memory is below the estimated peak plus warning margin; continuing local ASR."]
    return []
