"""Optional RapidOCR backend for local screenshots/images.

RapidOCR is imported lazily. The backend only reads local files or ``file://``
references and never captures screens, opens accounts, or fetches remote image
URLs.
"""

from __future__ import annotations

import importlib.util
import math
from pathlib import Path
from typing import Any

from openmd.adapters.local_audio.adapter import local_path_from_input
from openmd.adapters.screenshot.ocr import MissingOCRBackend, OCRBackend, classify_ocr_profile
from openmd.core.models import TranscriptResult
from openmd.core.secrets import sanitize_error_summary


class RapidOCRBackend(OCRBackend):
    name = "ocr.rapidocr"

    def is_available(self) -> bool:
        return importlib.util.find_spec("rapidocr_onnxruntime") is not None

    def recognize(self, source_ref: str, *, title: str) -> TranscriptResult:
        source_path = _local_existing_path(source_ref)
        if source_path is None:
            return _failed_result(
                self.name,
                source_ref,
                title,
                "ocr_local_file_required",
                "RapidOCR requires a local image file path or file:// reference.",
            )

        if not self.is_available():
            return MissingOCRBackend(self.name).recognize(str(source_path), title=title)

        try:
            from rapidocr_onnxruntime import RapidOCR

            rows, _elapsed = RapidOCR()(str(source_path))
        except Exception as exc:  # pragma: no cover - dependency-specific safety net
            return _failed_result(self.name, str(source_path), title, "ocr_extraction_failed", exc)

        lines, confidences = _extract_lines(rows)
        text = "\n".join(lines).strip()
        if not text:
            return _failed_result(
                self.name,
                str(source_path),
                title,
                "ocr_empty_text",
                "RapidOCR returned no recognized text.",
            )

        document_title = title or source_path.stem or "screenshot"
        body = _with_title(document_title, text)
        source_map, ambiguous = _layout_evidence(rows)
        return TranscriptResult(
            text=body,
            status="partial" if ambiguous else "verified",
            source_level="ocr",
            backend=self.name,
            source_notes=[
                "RapidOCR recognized text from a local screenshot/image.",
                "Raw screenshot file was referenced, not copied into the package.",
                *(["Side-by-side OCR blocks have ambiguous reading order; verify against the source image."] if ambiguous else []),
            ],
            title=document_title,
            metadata={
                "profile": classify_ocr_profile(body),
                "ocr_backend": self.name,
                "source_ref": str(source_path),
                "block_count": len(lines),
                "average_confidence": _average(confidences),
                "source_map": source_map,
                "reading_order": "ambiguous" if ambiguous else "engine_order_not_independently_verified",
            },
        )


def _local_existing_path(source_ref: str) -> Path | None:
    path = local_path_from_input(source_ref)
    if path is None:
        return None
    return path if path.is_file() else None


def _extract_lines(rows: Any) -> tuple[list[str], list[float]]:
    if not rows:
        return [], []
    lines: list[str] = []
    confidences: list[float] = []
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < 2:
            continue
        text = row[1]
        if isinstance(text, str) and text.strip():
            lines.append(text.strip())
        if len(row) >= 3 and isinstance(row[2], (int, float)):
            confidences.append(float(row[2]))
    return lines, confidences


def _average(values: list[float]) -> float | None:
    if not values:
        return None
    return round(sum(values) / len(values), 4)


def _layout_evidence(rows: Any) -> tuple[list[dict[str, Any]], bool]:
    """Retain pixel locators and flag side-by-side blocks without inventing order."""
    boxes: list[dict[str, Any]] = []
    if not isinstance(rows, (list, tuple)):
        return boxes, False
    line_number = 0
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < 2 or not isinstance(row[1], str) or not row[1].strip():
            continue
        line_number += 1
        try:
            points = [(float(point[0]), float(point[1])) for point in row[0]]
            if len(points) < 4 or not all(math.isfinite(v) for point in points for v in point):
                continue
            xs, ys = zip(*points)
            bbox = [min(xs), min(ys), max(xs), max(ys)]
            boxes.append({"line": line_number, "bbox": bbox, "unit": "pixel"})
        except (TypeError, ValueError, IndexError):
            continue
    # Only compare vertically overlapping blocks; this also flags table-like
    # layouts, whose semantics cannot be certified by text confidence alone.
    active: list[list[float]] = []
    ambiguous = False
    for entry in sorted(boxes, key=lambda entry: entry["bbox"][1]):
        box = entry["bbox"]
        active = [other for other in active if other[3] > box[1]]
        for other in active:
            overlap = min(other[3], box[3]) - max(other[1], box[1])
            height = min(other[3] - other[1], box[3] - box[1])
            gap = max(box[0] - other[2], other[0] - box[2])
            if height > 0 and overlap >= height / 2 and gap > height:
                ambiguous = True
        active.append(box)
    return boxes, ambiguous


def _with_title(title: str, text: str) -> str:
    if text.lstrip().startswith("#"):
        return text if text.endswith("\n") else f"{text}\n"
    return f"# {title}\n\n{text}\n"


def _failed_result(
    backend: str,
    source_ref: str,
    title: str,
    code: str,
    error: object,
) -> TranscriptResult:
    return TranscriptResult(
        text="",
        status="failed",
        source_level="partial_extraction",
        backend=backend,
        source_notes=[
            "OCR backend did not produce usable text.",
            "Raw screenshot file was referenced, not copied into the package.",
        ],
        error_code=code,
        error_summary=sanitize_error_summary(error),
        title=title or "screenshot",
        metadata={"profile": "document", "ocr_backend": backend, "source_ref": source_ref},
    )
