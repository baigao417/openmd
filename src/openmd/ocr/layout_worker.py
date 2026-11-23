"""Offline PP-DocLayoutV3 CPU worker. No OCR model is loaded here."""

from __future__ import annotations

import argparse
import importlib.metadata
import importlib.util
import json
import math
import os
from pathlib import Path


def select_device(requested: str, torch) -> str:
    if requested not in {"auto", "cpu", "cuda"}:
        raise ValueError("layout_device_invalid")
    available = bool(torch.cuda.is_available())
    if requested == "cuda" and not available:
        raise RuntimeError("layout_cuda_unavailable")
    return "cuda:0" if requested == "cuda" or (requested == "auto" and available) else "cpu"


def probe_payload(torch, modules: dict, versions: dict, requested: str = "auto") -> dict:
    if torch is None:
        return {"schema": "openmd.layout-probe/v1", "ready": False, "modules": modules,
                "versions": versions, "device": None, "cpu_only": None,
                "cuda_visible": None, "torch_cuda_build": None,
                "gpu_name": None, "gpu_memory_total_bytes": None,
                "gpu_memory_free_bytes": None}
    device = select_device(requested, torch)
    cuda_build = getattr(getattr(torch, "version", None), "cuda", None)
    memory_total = memory_free = None
    gpu_name = None
    if device.startswith("cuda"):
        try:
            memory_free, memory_total = torch.cuda.mem_get_info(0)
            gpu_name = torch.cuda.get_device_name(0)
        except (AttributeError, RuntimeError):
            pass
    return {"schema": "openmd.layout-probe/v1", "ready": all(modules.values()), "modules": modules,
            "versions": versions, "device": device, "cpu_only": device == "cpu",
            "cuda_visible": bool(torch.cuda.is_available()), "torch_cuda_build": cuda_build,
            "gpu_name": gpu_name, "gpu_memory_total_bytes": memory_total,
            "gpu_memory_free_bytes": memory_free}


def detect(image, folder: Path, threshold: float, requested_device: str = "auto") -> list[dict]:
    import torch
    import transformers
    from .layout_filter import filter_overlap_boxes

    device = select_device(requested_device, torch)
    if device == "cpu":
        torch.set_num_threads(4)
    processor_class = getattr(transformers, "PPDocLayoutV3ImageProcessorFast", transformers.AutoImageProcessor)
    processor = processor_class.from_pretrained(folder, local_files_only=True, trust_remote_code=False)
    model = transformers.AutoModelForObjectDetection.from_pretrained(folder, local_files_only=True, trust_remote_code=False, torch_dtype=torch.float32).to(device).eval()
    inputs = processor(images=image, return_tensors="pt")
    inputs = {key: value.to(device) if hasattr(value, "to") else value for key, value in inputs.items()}
    with torch.inference_mode():
        output = model(**inputs)
    result = processor.post_process_object_detection(output, target_sizes=[(image.height, image.width)], threshold=threshold)[0]
    regions = []
    for index, (box, label, score) in enumerate(zip(result["boxes"], result["labels"], result["scores"])):
        left, top, right, bottom = [float(v) for v in box]
        coords = [max(0, math.floor(left)), max(0, math.floor(top)), min(image.width, math.ceil(right)), min(image.height, math.ceil(bottom))]
        if coords[2] <= coords[0] or coords[3] <= coords[1]:
            continue
        labels = model.config.id2label
        category = labels.get(int(label), labels.get(str(int(label)), str(int(label))))
        regions.append({"order": index, "category": category, "bbox": coords, "score": float(score), "label": category, "coordinate": coords})
    # Preserve the learned upstream reading order, including inline formula filtering.
    filtered = filter_overlap_boxes({"boxes": regions}, "rect")["boxes"]
    return [{key: value for key, value in region.items() if key not in {"label", "coordinate"}} for region in filtered]


def assert_cpu_only(torch=None) -> None:
    # Reject a mis-launched worker instead of silently hiding a visible GPU.
    # The controller passes -1: an empty Windows environment value can leave
    # CUDA visible. Even an explicit mask must pass the actual runtime check.
    if os.environ.get("CUDA_VISIBLE_DEVICES") not in {"", "-1"}:
        raise RuntimeError("layout_cpu_only: CUDA_VISIBLE_DEVICES must hide all devices")
    if torch is not None and torch.cuda.is_available():
        raise RuntimeError("layout_cpu_only: CUDA is visible to the layout worker")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--device", choices=("auto", "cpu", "cuda"), default="auto")
    parser.add_argument("--input", type=Path)
    parser.add_argument("--model", type=Path)
    parser.add_argument("--result", type=Path)
    parser.add_argument("--threshold", type=float, default=0.5)
    args = parser.parse_args()
    os.environ.update({"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "TOKENIZERS_PARALLELISM": "false"})
    if args.probe:
        modules = {name: importlib.util.find_spec(name) is not None for name in ("torch", "transformers", "PIL", "numpy", "torchvision")}
        torch_module = None
        if modules["torch"]:
            import torch as torch_module

        versions = {}
        for name in ("torch", "transformers", "torchvision"):
            try:
                versions[name] = importlib.metadata.version(name)
            except importlib.metadata.PackageNotFoundError:
                versions[name] = None
        print(json.dumps(probe_payload(torch_module, modules, versions, args.device)))
        return 0
    if args.input is None or args.model is None or args.result is None:
        parser.error("--input, --model and --result required")
    from PIL import Image

    with Image.open(args.input) as source:
        regions = detect(source.convert("RGB"), args.model, args.threshold, args.device)
    args.result.write_text(json.dumps({"schema": "openmd.layout/v1", "regions": regions}), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
