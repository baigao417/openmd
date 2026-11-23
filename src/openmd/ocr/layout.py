"""Bounded external layout worker; never imports torch in the controller."""

from __future__ import annotations

import json
import subprocess
import tempfile
import time
from pathlib import Path

from openmd.runtime.interpreters import owned_popen, communicate_interruptibly, process_options, terminate_quietly, wait_interruptibly, worker_command, worker_environment
from .config import layout_runtime

from openmd.runtime.tempdirs import owned_temporary_directory


class LayoutUnavailable(RuntimeError):
    pass


def _run(args: list[str], timeout: float, directory: Path, *, device: str = "cpu"):
    python, _ = layout_runtime()
    if python is None:
        raise LayoutUnavailable("fallback_unavailable: configure ocr_python / OPENMD_OCR_PYTHON")
    environment = worker_environment()
    if device == "cpu":
        # Windows CUDA may treat an empty value as unset. -1 hides every device.
        environment["CUDA_VISIBLE_DEVICES"] = "-1"
    args = [*args, "--device", device]
    with (directory / "layout.log").open("wb") as log:
        process = owned_popen(worker_command(python, *args, module="openmd.ocr.layout_worker"), cwd=directory, env=environment, stdin=subprocess.DEVNULL, stdout=log, stderr=log, **process_options())
        try:
            wait_interruptibly(process, timeout)
        except BaseException:
            terminate_quietly(process)
            raise
    if process.returncode:
        raise LayoutUnavailable("fallback_unavailable: external layout worker failed; check local torch/transformers and PP-DocLayoutV3 weights")


def detect_regions(image, config: dict, deadline: float) -> tuple[list[dict], float]:
    python, model = layout_runtime()
    if python is None or model is None:
        raise LayoutUnavailable("fallback_unavailable: configure ocr_python and a model_roots directory containing layout/PP-DocLayoutV3")
    start = time.monotonic()
    remaining = deadline - start
    if remaining <= 0:
        raise TimeoutError("page_deadline")
    try:
        with owned_temporary_directory(prefix="openmd-layout-") as tmp:
            directory = Path(tmp)
            source, result = directory / "input.png", directory / "result.json"
            image.save(source)
            device = config.get("layout_device", "auto")
            _run(["--input", str(source), "--model", str(model), "--result", str(result), "--threshold", str(config["layout_threshold"])], min(config["layout_seconds"], remaining), directory, device=device)
            if not result.is_file() or result.stat().st_size > 4 * 1024 * 1024:
                raise LayoutUnavailable("fallback_unavailable: invalid layout worker result")
            payload = json.loads(result.read_text(encoding="utf-8"))
            regions = payload.get("regions")
            if payload.get("schema") != "openmd.layout/v1" or not isinstance(regions, list) or not regions:
                raise LayoutUnavailable("fallback_unavailable: no layout regions")
            for region in regions:
                box = region.get("bbox")
                if not isinstance(region.get("category"), str) or not isinstance(box, list) or len(box) != 4 or not all(isinstance(v, int) for v in box) or not (0 <= box[0] < box[2] <= image.width and 0 <= box[1] < box[3] <= image.height):
                    raise LayoutUnavailable("fallback_unavailable: invalid layout region")
            return regions, time.monotonic() - start
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise LayoutUnavailable("fallback_unavailable: external layout worker unavailable") from exc


def probe_layout() -> dict:
    python, model = layout_runtime()
    report = {"python_configured": python is not None, "model_available": model is not None, "ready": False}
    if python is None or model is None:
        return report
    try:
        with owned_temporary_directory(prefix="openmd-layout-probe-") as tmp:
            directory = Path(tmp)
            environment = worker_environment()
            from .config import settings
            device = settings().get("layout_device", "auto")
            if device == "cpu":
                environment["CUDA_VISIBLE_DEVICES"] = "-1"
            process = owned_popen(worker_command(python, "--probe", "--device", device, module="openmd.ocr.layout_worker"), cwd=directory, env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **process_options())
            try:
                stdout, _ = communicate_interruptibly(process, timeout=15)
            except BaseException:
                terminate_quietly(process)
                raise
            if process.returncode or len(stdout) > 1024 * 1024:
                raise ValueError("layout probe failed")
            payload = json.loads(stdout)
            if payload.get("schema") != "openmd.layout-probe/v1":
                raise ValueError("invalid layout probe")
            report.update(payload)
    except (OSError, ValueError, subprocess.SubprocessError):
        report["error_code"] = "fallback_unavailable"
    return report
