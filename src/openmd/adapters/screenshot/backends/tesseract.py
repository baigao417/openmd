"""Tesseract screenshot backend placeholder.

The real OCR engine is intentionally not imported or executed by the
mock/gated lane. Future work can replace this placeholder after dependency,
privacy, and quality smoke are explicitly approved.
"""

from __future__ import annotations

from openmd.adapters.screenshot.ocr import MissingOCRBackend


class TesseractOCRBackend(MissingOCRBackend):
    def __init__(self) -> None:
        super().__init__("ocr.tesseract")
