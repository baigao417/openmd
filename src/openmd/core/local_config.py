"""Machine-local tool locations shared by the controller and isolated workers."""

from __future__ import annotations

import json
import math
import os
import re
from pathlib import Path
from typing import Any


SCHEMA = "openmd.local/v1"
DEFAULT_QWEN3_BATCH_SIZE = 4  # user decision 2026-11-09: ~2.1x faster; not byte-identical to batch 1
MAX_QWEN3_BATCH_SIZE = 32
_PRIVATE_KEY = re.compile(r"cookie|token|auth|session|password|secret|credential|api.?key", re.I)


def checkout_root() -> Path | None:
    root = Path(__file__).resolve().parents[3]
    return root if (root / "pyproject.toml").is_file() and (root / "src/openmd").is_dir() else None


def local_config_path() -> Path | None:
    explicit = os.environ.get("OPENMD_CONFIG")
    if explicit:
        return Path(explicit).expanduser().resolve()
    root = checkout_root()
    if root is not None and (root / "out/local-config.json").is_file():
        return root / "out/local-config.json"
    default = Path.home() / ".openmd/local.json"
    return default if default.is_file() else None


def _check_keys(value: Any) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if _PRIVATE_KEY.search(key):
                raise ValueError("local_config_private_field: credentials do not belong in Openmd location configuration")
            _check_keys(child)
    elif isinstance(value, list):
        for child in value:
            _check_keys(child)


def load_local_config() -> dict[str, Any]:
    """Return optional location metadata; explicit malformed configuration fails closed."""
    path = local_config_path()
    if path is None:
        return {}
    if not path.is_file():
        raise ValueError("local_config_missing: OPENMD_CONFIG must name an existing local JSON file")
    if path.stat().st_size > 128 * 1024:
        raise ValueError("local_config_too_large")
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise ValueError("local_config_invalid: expected local position configuration JSON") from exc
    if not isinstance(value, dict) or value.get("schema") != SCHEMA:
        raise ValueError("local_config_schema: expected openmd.local/v1")
    _check_keys(value)
    if set(value) - {"schema", "asr_python", "qwen3_batch_size", "ocr_python", "ollama_url", "ocr", "soffice", "model_roots", "tools", "route_overlay", "xiaoe_long_asr", "asr_models", "asr_memory", "browser"}:
        raise ValueError("local_config_unknown_field")
    if "browser" in value:
        setting = value["browser"]
        if (not isinstance(setting, dict) or set(setting) - {"idle_timeout_seconds", "window_strategy"}
                or type(setting.get("idle_timeout_seconds", 120)) not in (int, float)
                or not 1 <= setting.get("idle_timeout_seconds", 120) <= 86400
                or not isinstance(setting.get("window_strategy", "existing-window"), str)
                or setting.get("window_strategy", "existing-window") not in {"offscreen-reuse", "reuse", "existing-window"}):
            raise ValueError("local_config_browser_invalid")
    if "asr_models" in value:
        setting = value["asr_models"]
        if (not isinstance(setting, dict) or set(setting) - {"prepared_root"}
                or not isinstance(setting.get("prepared_root"), str) or not setting["prepared_root"].strip()):
            raise ValueError("local_config_asr_models_invalid")
    if "asr_memory" in value:
        setting = value["asr_memory"]
        if (not isinstance(setting, dict) or set(setting) - {"original_peak_mb", "prepared_peak_mb", "language_probe_peak_mb", "margin_mb"}
                or any(type(v) is not int or v <= 0 for v in setting.values())):
            raise ValueError("local_config_asr_memory_invalid")
    if "xiaoe_long_asr" in value:
        setting = value["xiaoe_long_asr"]
        if (not isinstance(setting, dict) or set(setting) - {"enabled", "threshold_seconds"}
                or type(setting.get("enabled")) is not bool
                or type(setting.get("threshold_seconds", 600)) not in (int, float)
                or not math.isfinite(setting.get("threshold_seconds", 600))
                or not 0 < setting.get("threshold_seconds", 600) <= 86400):
            raise ValueError("local_config_xiaoe_long_asr_invalid")
    if "qwen3_batch_size" in value:
        if type(value["qwen3_batch_size"]) is not int:
            raise ValueError("local_config_qwen3_batch_size_invalid: expected an integer")
        validate_qwen3_batch_size(value["qwen3_batch_size"])
    if "asr_python" in value and not isinstance(value["asr_python"], str):
        raise ValueError("local_config_asr_python_invalid")
    for key in ("ocr_python", "ollama_url", "soffice"):
        if key in value and not isinstance(value[key], str):
            raise ValueError(f"local_config_{key}_invalid")
    if "ocr" in value and not isinstance(value["ocr"], dict):
        raise ValueError("local_config_ocr_invalid")
    if "model_roots" in value and (not isinstance(value["model_roots"], list) or not all(isinstance(p, str) for p in value["model_roots"])):
        raise ValueError("local_config_model_roots_invalid")
    if "tools" in value and not isinstance(value["tools"], dict):
        raise ValueError("local_config_tools_invalid")
    if "opencli" in value.get("tools", {}):
        setting = value["tools"]["opencli"]
        if (not isinstance(setting, dict) or set(setting) - {"root", "config_dir"}
                or not isinstance(setting.get("root"), str) or not setting["root"].strip()
                or ("config_dir" in setting and (not isinstance(setting["config_dir"], str) or not setting["config_dir"].strip()))):
            raise ValueError("managed_opencli_location_invalid")
    if "route_overlay" in value and not isinstance(value["route_overlay"], str):
        raise ValueError("local_config_route_overlay_invalid")
    return value


def validate_qwen3_batch_size(value: object) -> int:
    """Validate a bounded batch size without importing the GPU stack."""
    if isinstance(value, str) and re.fullmatch(r"[0-9]+", value.strip()):
        value = int(value.strip())
    if type(value) is not int or not 1 <= value <= MAX_QWEN3_BATCH_SIZE:
        raise ValueError(f"qwen3_batch_size_invalid: expected an integer from 1 to {MAX_QWEN3_BATCH_SIZE}")
    return value


def qwen3_batch_size() -> int:
    """Environment overrides local JSON; scalar inference is the safe default."""
    local = load_local_config()
    value = os.environ.get("OPENMD_QWEN3_BATCH_SIZE", local.get("qwen3_batch_size", DEFAULT_QWEN3_BATCH_SIZE))
    return validate_qwen3_batch_size(value)
