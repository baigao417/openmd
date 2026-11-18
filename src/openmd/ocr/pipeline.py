"""Serial page/tile OCR, monitored Ovis3 → layout MiniCPM fallback."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from importlib.resources import files
from pathlib import Path

from openmd.adapters.local_audio.adapter import local_path_from_input
from openmd.adapters.screenshot.ocr import OCRBackend, classify_ocr_profile
from openmd.core.local_config import checkout_root
from openmd.core.models import TranscriptResult
from openmd.runtime.gpu_lock import gpu_lock, gpu_lock_path
from openmd.runtime.interpreters import process_options
from openmd.runtime.gpu_quarantine import GPUQuarantined, clear_quarantine, mark_quarantine
from .breakers import breaker_reason, estimate_expected_chars
from .config import settings
from .layout import LayoutUnavailable, detect_regions
from .ollama import OllamaError, api_json, model_identity, running_models, stream_recognize, unload_model
from .otsl import table_html
from .tiling import merge_pair, norm_view, repeated_ngrams, split_image

from openmd.runtime.tempdirs import owned_temporary_directory


def han_ratio(text: str) -> float:
    chars = [c for c in text if c.isalnum()]
    return round(sum("\u3400" <= c <= "\u9fff" for c in chars) / len(chars), 5) if chars else 0.0


def ocr_notes(pages: list[dict]) -> list[str]:
    identities = sorted({f"{a['engine']} model={a.get('model')} digest={a.get('digest')}" for page in pages for tile in page.get("tiles", [page]) for a in tile.get("attempts", [])})
    used = any(page.get("fallback_used") for page in pages)
    reasons = sorted({str(page.get("reason")) for page in pages if page.get("reason")})
    return [*identities, f"fallback_used={str(used).lower()}; reason={'; '.join(reasons) or 'none'}", "识别模型可能自动纠正原文错字", "fallback_used=false 不代表无幻觉", "Raw OCR Markdown only; no summary, paraphrase, translation or polishing was requested.", "Image/chart asset export is not implemented; bbox image tags are source references, not exported files."]


class GpuSampler:
    """Optional whole-device memory observation; not model allocation accounting."""
    def __enter__(self):
        self.values = []
        self.stop = threading.Event()

        def sample():
            while not self.stop.is_set():
                try:
                    value = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=3, **process_options())
                    self.values.append(float(value.stdout.strip().splitlines()[0]))
                except (OSError, ValueError, IndexError, subprocess.SubprocessError):
                    return
                self.stop.wait(0.35)

        self.thread = threading.Thread(target=sample, name="openmd-gpu-sampler", daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.stop.set()
        self.thread.join(timeout=4)

    @property
    def peak(self):
        return max(self.values) if self.values else None


def retain_breaker_output(text: str, attempt: dict) -> str | None:
    if not text:
        return None
    root = checkout_root()
    directory = root / "out/ocr-router/breaker-evidence" if root else gpu_lock_path().parent / "ocr-evidence"
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / (uuid.uuid4().hex + ".md")
    target.write_text(text, encoding="utf-8")
    return str(target)


def retain_seam_outputs(a: str, b: str, arbitrator: str | None, seam: dict, *, merged_a: str | None = None) -> dict:
    """Private OCR readings belong in ignored evidence, never JSON receipts."""
    root = checkout_root()
    directory = root / "out/ocr-router/seam-evidence" if root else gpu_lock_path().parent / "ocr-evidence"
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / (uuid.uuid4().hex + ".json")
    payload = json.dumps({"a": merged_a if merged_a is not None else a, "previous_tile": a, "b": b, "arbitrator": arbitrator, "alignment": seam, "mapping_basis": "a=accumulated_merge_before_seam,b=current_raw_tile"}, ensure_ascii=False, indent=2)
    target.write_text(payload, encoding="utf-8")
    return {"path": str(target), "sha256": hashlib.sha256(payload.encode()).hexdigest(),
            "a_chars": len(a), "b_chars": len(b), "arbitrator_chars": len(arbitrator) if arbitrator is not None else None}


def seam_geometry(a: str, b: str, previous: tuple, current: tuple) -> dict:
    """Project text lines monotonically onto the actual shared image band.

    OCR has no word coordinates, so this projection limits candidate search,
    never proves a deletion. The independent arbitration crop uses the exact
    pixel intersection, not this text-position estimate.
    """
    y0, y1 = max(previous[1], current[1]), min(previous[3], current[3])
    height = max(0, y1 - y0)
    def window(text, fraction, tail):
        lines = text.splitlines(keepends=True)
        # Sparse Markdown lines (e.g. a one-symbol final line) are not a
        # faithful pixel-height projection. Keep a second density estimate
        # so the candidate window cannot collapse below a short real seam.
        density = max(32, math.ceil(len(norm_view(text)[0]) * fraction * 2))
        if len(lines) > 2:
            count = max(1, math.ceil(len(lines) * fraction) + 1)
            band = "".join(lines[-count:] if tail else lines[:count])
            return max(density, len(norm_view(band)[0]))
        return density
    return {"overlap_bbox": [max(previous[0], current[0]), y0, min(previous[2], current[2]), y1],
            "a_window_chars": window(a, height / max(1, previous[3] - previous[1]), True),
            "b_window_chars": window(b, height / max(1, current[3] - current[1]), False),
            "candidate_window_basis": "monotonic_line_projection_estimate"}


class LocalOCRBackend(OCRBackend):
    def __init__(self, name: str = "ocr.chain") -> None:
        if name not in {"ocr.chain", "ocr.ovis3", "ocr.minicpm"}:
            raise ValueError("unknown OCR backend")
        self.name = name

    def recognize(self, source_ref: str, *, title: str) -> TranscriptResult:
        source = local_path_from_input(source_ref)
        if source is None or not source.is_file():
            return self.failed("ocr_local_file_required", title)
        try:
            from PIL import Image

            with Image.open(source) as raw:
                image = raw.convert("RGB")
            return self.recognize_image(image, title=title)
        except (ImportError, OSError, ValueError) as exc:
            return self.failed("ocr_image_unavailable", title, type(exc).__name__)

    def failed(self, code: str, title: str, detail: str | None = None) -> TranscriptResult:
        return TranscriptResult(text="", status="failed", source_level="ocr", backend=self.name, title=title, error_code=code, error_summary=detail or code, source_notes=["No model installation/download or Ollama lifecycle action was attempted."], metadata={"profile": "document"})

    def recognize_image(self, image, *, title: str, page: int = 1, tile: bool = True) -> TranscriptResult:
        try:
            config = settings()
            with gpu_lock(timeout=config["gpu_lock_seconds"]):
                return self._recognize_locked(image, title, page, tile, config)
        except (OSError, ValueError, TimeoutError, OllamaError, GPUQuarantined) as exc:
            code = getattr(exc, "code", None) or (str(exc).split(":", 1)[0] if isinstance(exc, (OllamaError, TimeoutError, GPUQuarantined)) else "ocr_runtime_unavailable")
            if isinstance(exc, GPUQuarantined):
                return TranscriptResult(text="", status="partial", source_level="ocr", backend=self.name, title=title, error_code=code, error_summary=str(exc), metadata={"terminal_partial": True, "abort_file": True, "profile": "document"})
            return self.failed(code, title, str(exc) if isinstance(exc, (OllamaError, TimeoutError)) else type(exc).__name__)

    def _recognize_locked(self, image, title: str, page: int, tile: bool, config: dict) -> TranscriptResult:
        tags = api_json(config["ollama_url"], "/api/tags")
        primary = "minicpm_model" if self.name == "ocr.minicpm" else "ovis_model"
        model_identity(tags, config[primary])
        started = time.monotonic()
        deadline = started + config["page_seconds"]
        receipts, texts, merge = [], [], {"seams": [], "warnings": [], "arbitration_attempts": []}
        text = ""
        params = {key: config[key] for key in ("tile_height", "tile_overlap", "tile_search", "tile_aspect_ratio")}
        with owned_temporary_directory(prefix="openmd-ocr-tiles-") as tmp:
            directory = Path(tmp)
            if tile and image.height > config["tile_height"] * 1.1 and image.height / image.width > config["tile_aspect_ratio"]:
                source = directory / "input.png"
                image.save(source)
                plan = split_image(source, directory / "tiles", config["tile_height"], config["tile_overlap"], config["tile_search"])
                rectangles = [(0, t["y0"], image.width, t["y1"]) for t in plan["tiles"]]
                params.update(plan["params"])
            else:
                rectangles = [(0, 0, image.width, image.height)]
            for index, box in enumerate(rectangles, 1):
                receipt, text = self._tile(image.crop(box), config, tags, deadline)
                receipt.update({"tile": index, "bbox": list(box)})
                receipts.append(receipt)
                texts.append(text)
                if any(a.get("unsafe_to_switch") for a in receipt["attempts"]):
                    text = "\n\n".join(texts)
                    break
                if index == 1:
                    merged = text
                    continue
                if text and texts[-2]:
                    geometry = seam_geometry(texts[-2], text, rectangles[index - 2], box)
                    merged, seam, arbitration = self._merge_seam(merged, texts[-2], text, image, geometry, config, tags, deadline, receipt)
                    seam["seam"] = f"{index - 1}->{index}"
                    merge["seams"].append(seam)
                    if arbitration is not None:
                        merge["arbitration_attempts"].append(arbitration)
                    if seam["status"] != "merged":
                        merge["warnings"].append(f"seam {index - 1}->{index}: merge_uncertain (localized variants retained where alignment is proven)")
                    text = merged
                    if arbitration is not None and arbitration.get("unsafe_to_switch"):
                        receipt.update({"status": "partial", "reason": "ollama_unload_unconfirmed"})
                        break
                else:
                    merged += "\n\n" + text
                    text = merged
                    merge["warnings"].append("One or more tiles have no text; overlap merge was skipped.")
        text = text if texts else ""
        reps = repeated_ngrams(text)
        merge.update({"tiles": len(texts), "repeated_spans": reps[:20], "merged_norm_chars": len(norm_view(text)[0])})
        if reps:
            merge["warnings"].append(f"{len(reps)} repeated 24-char spans in merged text (possible duplicate overlap)")
        partial = not text.strip() or any(r["status"] != "verified" for r in receipts) or bool(merge["warnings"])
        attempts = [attempt for r in receipts for attempt in r["attempts"]] + merge["arbitration_attempts"]
        unsafe = any(a.get("unsafe_to_switch") for a in attempts)
        peaks = [a["peak_vram_mb"] for a in attempts if a.get("peak_vram_mb") is not None]
        reasons = sorted({r["reason"] for r in receipts if r.get("reason")})
        if merge["warnings"]:
            reasons.append("merge_uncertain")
        record = {"page": page, "route": "ocr", "status": "partial" if partial else "verified", "engine": sorted({a["engine"] for a in attempts}), "models": [{"name": a["model"], "digest": a["digest"]} for a in attempts], "fallback_used": any(r["fallback_used"] for r in receipts), "reason": "; ".join(reasons) or None, "ovis_seconds": round(sum(a["seconds"] for a in attempts if a["engine"] == "ocr.ovis3"), 3), "fallback_seconds": round(sum(a["seconds"] for a in attempts if a["engine"] == "ocr.minicpm"), 3), "seconds": round(time.monotonic() - started, 3), "peak_vram_mb": max(peaks) if peaks else None, "peak_vram_scope": "whole_device_observed", "han_ratio": han_ratio(text), "slice_parameters": params, "tiles": receipts, "merge": merge, "breakers": [a["breaker"] for a in attempts if a.get("breaker")]}
        version = "ollama-models:" + hashlib.sha256(json.dumps(record["models"], sort_keys=True).encode()).hexdigest()
        record["unprocessed_tiles"] = len(rectangles) - len(receipts)
        return TranscriptResult(text=text, status=record["status"], source_level="ocr", backend=self.name, title=title, error_code="ollama_unload_unconfirmed" if unsafe else "ocr_partial" if partial else None, source_notes=[*ocr_notes([record]), f"slice_parameters={json.dumps(params, sort_keys=True)}", *merge["warnings"]], metadata={"profile": classify_ocr_profile(text), "pages": [record], "terminal_partial": partial, "abort_file": unsafe, "fallback_used": record["fallback_used"], "tool_run": {"tool_version": version, "tool_version_verified": True}})

    def _merge_seam(self, merged: str, previous: str, current: str, image, geometry: dict,
                    config: dict, tags: dict, deadline: float, receipt: dict):
        text, seam = merge_pair(merged, current, len(norm_view(previous)[0]), config["tile_overlap"], geometry=geometry)
        if seam["status"] == "merged":
            try:
                seam["raw_outputs"] = retain_seam_outputs(previous, current, None, seam, merged_a=merged)
            except OSError:
                seam["raw_outputs"] = {"retained": False}
            return text, seam, None
        attempt, arbitration_text = None, None
        successful = [a for a in receipt["attempts"] if not a.get("breaker") and a.get("request_kind") == "recognition"]
        box = geometry["overlap_bbox"]
        if successful and box[2] > box[0] and box[3] > box[1] and time.monotonic() < deadline:
            # The containing _recognize_locked holds the same OS GPU lease.
            # No chain/fallback call here: one crop, one identical engine.
            attempt, arbitration_text = self._attempt(image.crop(box), config, tags, deadline, successful[-1]["engine"])
            attempt["purpose"] = "seam_arbitration"
            seam["arbitration"] = {"status": "failed" if attempt.get("breaker") else "completed",
                                   "engine": attempt["engine"], "seconds": attempt["seconds"],
                                   "bbox": box, "reason": attempt.get("breaker")}
            if not attempt.get("breaker"):
                text, revised = merge_pair(merged, current, len(norm_view(previous)[0]), config["tile_overlap"], geometry=geometry, arbitrator=arbitration_text)
                revised["arbitration"] = seam["arbitration"]
                seam = revised
        else:
            seam["arbitration"] = {"status": "unavailable", "reason": "page_deadline" if time.monotonic() >= deadline else "source_engine_or_overlap_unavailable"}
        try:
            seam["raw_outputs"] = retain_seam_outputs(previous, current, arbitration_text, seam, merged_a=merged)
        except OSError:
            seam["raw_outputs"] = {"retained": False}
        return text, seam, attempt

    def _attempt(self, image, config: dict, tags: dict, deadline: float, engine: str) -> tuple[dict, str]:
        key = "ovis_model" if engine == "ocr.ovis3" else "minicpm_model"
        identity = model_identity(tags, config[key])
        start = time.monotonic()
        record = {"engine": engine, "model": identity["name"], "digest": identity["digest"], "seconds": 0.0, "peak_vram_mb": None, "breaker": None, "request_kind": "recognition"}
        text = ""
        sampler = None
        own_request_started = False
        # Persist before any attempt, including a hard process termination. Only
        # a confirmed unload can clear this state while the GPU lease is held.
        mark_quarantine(config["ollama_url"], identity["name"])
        try:
            with GpuSampler() as sampler:
                try:
                    if engine == "ocr.ovis3":
                        prompt = files("openmd.ocr").joinpath("ovis-prompt.txt").read_text(encoding="utf-8")
                        own_request_started = True
                        output = stream_recognize(image, model=identity["name"], prompt=prompt, config=config, deadline=deadline)
                        text = output.pop("text")
                        record.update(output)
                    else:
                        regions, layout_seconds = detect_regions(image, config, deadline)
                        blocks, pieces = [], []
                        for region in regions:
                            if time.monotonic() >= deadline:
                                record["breaker"] = {"rule": "page_deadline"}
                                break
                            category = region["category"].lower()
                            block = dict(region)
                            if category in {"image", "picture", "chart"}:
                                coords = [int(v / (image.width if i % 2 == 0 else image.height) * 1000) for i, v in enumerate(region["bbox"])]
                                part = '<img src="images/bbox_' + "_".join(map(str, coords)) + '.jpg" />'
                                block["recognition_skipped"] = True
                            else:
                                prompt = "Table Recognition:" if "table" in category else "Formula Recognition:" if "formula" in category and "number" not in category else "Text Recognition:"
                                own_request_started = True
                                output = stream_recognize(image.crop(region["bbox"]), model=identity["name"], prompt=prompt, config=config, deadline=deadline)
                                part = output.pop("text")
                                block.update(output)
                                if output.get("breaker"):
                                    record["breaker"] = output["breaker"]
                                if "table" in category and any(tag in part for tag in ("<fcel>", "<ecel>")):
                                    block["raw_output_sha256"] = hashlib.sha256(part.encode()).hexdigest()
                                    part = table_html(part)
                                if "formula" in category and "number" not in category and part.strip() and not any(mark in part for mark in ("$$", "\\[", "\\begin{")):
                                    part = "$$\n" + part.strip().strip("$") + "\n$$"
                            pieces.append(part)
                            blocks.append(block)
                            text = "\n\n".join(pieces)
                            if record["breaker"]:
                                break
                        record.update({"blocks": blocks, "layout_seconds": layout_seconds, "detected_regions": len(regions)})
                        # The fallback actually reads many crop images. Its
                        # capacity is their combined image-only estimate, not
                        # the downsampled full-page estimate used by Ovis. The
                        # latter undercounts dense newspapers after layout.
                        budgets = [b["capacity"] for b in blocks if b.get("capacity")]
                        if budgets:
                            capacity = {"estimated_chars": sum(b["estimated_chars"] for b in budgets), "max_length_chars": sum(b["max_length_chars"] for b in budgets), "scope": "layout_crop_images"}
                        else:
                            capacity = estimate_expected_chars(image, config)
                        record["capacity"] = capacity
                        record["breaker"] = record["breaker"] or breaker_reason(text, capacity, config)
                finally:
                    # This also runs on Ctrl+C. A failed unload prevents fallback.
                    pending = sys.exc_info()[1]
                    canceling = isinstance(pending, (KeyboardInterrupt, SystemExit))
                    try:
                        if own_request_started:
                            unload_model(config, identity["name"])
                        elif running_models(api_json(config["ollama_url"], "/api/ps")):
                            record["unsafe_to_switch"] = True
                            raise OllamaError("gpu_external_busy")
                        if canceling or (pending is not None and "cancel_unconfirmed" in str(pending)):
                            record["unsafe_to_switch"] = True
                        else:
                            clear_quarantine()
                    except BaseException as exc:
                        record["unsafe_to_switch"] = True
                        # Retain the pre-inference marker even when another
                        # Ctrl+C interrupts cleanup. Preserve cancellation.
                        if not canceling:
                            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                                raise
                            raise OllamaError("ollama_unload_unconfirmed") from exc
            record["peak_vram_mb"] = sampler.peak
        except LayoutUnavailable:
            record["breaker"] = {"rule": "fallback_unavailable"}
        except (OSError, ValueError, TimeoutError, OllamaError) as exc:
            record["breaker"] = {"rule": str(exc).split(":", 1)[0] if isinstance(exc, (OllamaError, TimeoutError)) else "engine_error"}
            if "unload" in str(exc) or "cancel_unconfirmed" in str(exc):
                record["unsafe_to_switch"] = True
        record["seconds"] = round(time.monotonic() - start, 3)
        if sampler is not None:
            record["peak_vram_mb"] = sampler.peak
        if not text.strip() and not record["breaker"]:
            record["breaker"] = {"rule": "empty_output"}
        if record["breaker"]:
            try:
                record["raw_output_path"] = retain_breaker_output(text, record)
            except OSError:
                record["raw_output_retained"] = False
        return record, text

    def _tile(self, image, config: dict, tags: dict, deadline: float) -> tuple[dict, str]:
        record = {"status": "verified", "fallback_used": False, "reason": None, "attempts": []}
        if time.monotonic() >= deadline:
            return {**record, "status": "partial", "reason": "page_deadline"}, ""
        primary = "ocr.minicpm" if self.name == "ocr.minicpm" else "ocr.ovis3"
        try:
            attempt, text = self._attempt(image, config, tags, deadline, primary)
        except LayoutUnavailable:
            return {**record, "status": "partial", "reason": "fallback_unavailable"}, ""
        record["attempts"].append(attempt)
        if not attempt["breaker"]:
            return record, text
        record.update({"status": "partial", "reason": attempt["breaker"]["rule"]})
        if self.name != "ocr.chain" or attempt.get("unsafe_to_switch"):
            return record, text
        record["fallback_used"] = True
        try:
            fallback, repaired = self._attempt(image, config, tags, deadline, "ocr.minicpm")
            record["attempts"].append(fallback)
            if fallback["breaker"]:
                rule = fallback["breaker"]["rule"]
                record["reason"] += "; " + (rule if rule == "fallback_unavailable" else "fallback_" + rule)
            else:
                record["status"] = "verified"
            return record, repaired or text
        except (LayoutUnavailable, OllamaError):
            record["reason"] += "; fallback_unavailable"
            return record, text
