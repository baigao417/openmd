"""Offline PP-DocLayoutV3 CPU worker. No OCR model is loaded here."""

from __future__ import annotations

import argparse
import importlib.metadata
import importlib.util
import json
import math
import os
from pathlib import Path


def detect(image, folder: Path, threshold: float) -> list[dict]:
    import torch
    import transformers
    from .layout_filter import filter_overlap_boxes

    assert_cpu_only(torch)
    torch.set_num_threads(4)
    processor_class = getattr(transformers, "PPDocLayoutV3ImageProcessorFast", transformers.AutoImageProcessor)
    processor = processor_class.from_pretrained(folder, local_files_only=True, trust_remote_code=False)
    model = transformers.AutoModelForObjectDetection.from_pretrained(folder, local_files_only=True, trust_remote_code=False, torch_dtype=torch.float32).to("cpu").eval()
    inputs = processor(images=image, return_tensors="pt")
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
    parser.add_argument("--input", type=Path)
    parser.add_argument("--model", type=Path)
    parser.add_argument("--result", type=Path)
    parser.add_argument("--threshold", type=float, default=0.5)
    args = parser.parse_args()
    assert_cpu_only()
    os.environ.update({"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "TOKENIZERS_PARALLELISM": "false"})
    if args.probe:
        modules = {name: importlib.util.find_spec(name) is not None for name in ("torch", "transformers", "PIL", "numpy", "torchvision")}
        cuda_visible = None
        if modules["torch"]:
            import torch

            assert_cpu_only(torch)
            cuda_visible = torch.cuda.is_available()
        versions = {}
        for name in ("torch", "transformers", "torchvision"):
            try:
                versions[name] = importlib.metadata.version(name)
            except importlib.metadata.PackageNotFoundError:
                versions[name] = None
        print(json.dumps({"schema": "openmd.layout-probe/v1", "ready": all(modules.values()), "modules": modules, "versions": versions, "device": "cpu", "cpu_only": True, "cuda_visible": cuda_visible}))
        return 0
    if args.input is None or args.model is None or args.result is None:
        parser.error("--input, --model and --result required")
    from PIL import Image

    with Image.open(args.input) as source:
        regions = detect(source.convert("RGB"), args.model, args.threshold)
    args.result.write_text(json.dumps({"schema": "openmd.layout/v1", "regions": regions}), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
