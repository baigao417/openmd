"""One validated configuration for providers, workers and diagnostics."""

from __future__ import annotations

import json
import math
import os
from importlib.resources import files
from pathlib import Path

from openmd.core.local_config import load_local_config

from .endpoint import validate_ollama_url


def settings() -> dict:
    value = json.loads(files("openmd.ocr").joinpath("settings.json").read_text(encoding="utf-8"))
    local = load_local_config()
    overrides = local.get("ocr", {})
    if not isinstance(overrides, dict) or set(overrides) - set(value):
        raise ValueError("local_config_ocr_invalid")
    value.update(overrides)
    value["ovis_model"] = os.getenv("OPENMD_OCR_OVIS_MODEL") or value["ovis_model"]
    value["minicpm_model"] = os.getenv("OPENMD_OCR_MINICPM_MODEL") or value["minicpm_model"]
    for key, item in value.items():
        if key == 'engine':
            if item not in {'ocr.chain', 'ocr.ovis3', 'ocr.minicpm', 'ocr.rapidocr'}:
                raise ValueError('ocr_engine_unknown')
        elif key.endswith("_model"):
            if not isinstance(item, str) or not item.strip() or any(ord(c) < 32 for c in item):
                raise ValueError(f"ocr_setting_invalid: {key}")
        elif isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(item) or item <= 0:
            raise ValueError(f"ocr_setting_invalid: {key}")
    for key in ("repeat_min_chars", "repeat_count", "num_predict", "num_ctx", "max_pixels", "tile_height", "pdf_dpi", "textlayer_min_chars", "office_sparse_chars"):
        if not isinstance(value[key], int):
            raise ValueError(f"ocr_setting_integer_required: {key}")
    if not 0 < value["tile_overlap"] < 0.5 or not 0 < value["tile_search"] < 0.5:
        raise ValueError("ocr_tile_settings_invalid")
    if any(not 0 < value[key] <= 1 for key in ("layout_threshold", "textlayer_min_printable", "textlayer_image_coverage", "textlayer_hidden_ratio", "textlayer_background_delta")):
        raise ValueError("ocr_ratio_setting_invalid")
    url = (os.getenv("OPENMD_OLLAMA_URL") or local.get("ollama_url") or "http://127.0.0.1:11434").rstrip("/")
    value["ollama_url"] = validate_ollama_url(url)
    return value


def layout_runtime() -> tuple[Path | None, Path | None]:
    local = load_local_config()
    python_value = os.getenv("OPENMD_OCR_PYTHON") or local.get("ocr_python")
    python = Path(python_value).expanduser().resolve() if python_value else None
    explicit = os.getenv("OPENMD_OCR_LAYOUT_MODEL")
    if explicit:
        candidates = [Path(explicit).expanduser()]
    else:
        roots = [*(Path(p).expanduser() for p in os.getenv("OPENMD_MODEL_ROOTS", "").split(os.pathsep) if p), *(Path(p).expanduser() for p in local.get("model_roots", []))]
        candidates = [p for root in roots for p in (root / "layout", root / "PP-DocLayoutV3", root / "PaddlePaddle/PP-DocLayoutV3", root)]
    model = next((p.resolve() for p in candidates if (p / "config.json").is_file() and any(p.glob("*.safetensors"))), None)
    return python if python and python.is_file() else None, model
