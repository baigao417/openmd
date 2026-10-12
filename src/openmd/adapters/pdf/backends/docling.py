"""Docling PDF backend placeholder.

The real parser is intentionally not imported or executed by the mock/gated
lane.  Future work can replace this placeholder after dependency and quality
smoke are explicitly approved.
"""

from __future__ import annotations

from openmd.adapters.pdf.parser import MissingPDFParser


class DoclingPDFParser(MissingPDFParser):
    def __init__(self) -> None:
        super().__init__("pdf.docling")
