"""Removed PDF placeholders must never become benchmark candidates again."""

import pytest

from openmd.routing.lifecycle import run_benchmark
from openmd.routing.loader import load_catalog


@pytest.mark.parametrize("candidate", ["pdf.docling", "paddleocr.pdf"])
def test_removed_pdf_placeholder_is_unknown_to_benchmark(candidate):
    assert candidate not in load_catalog().capabilities
    with pytest.raises(ValueError, match="^benchmark_candidates_invalid$"):
        run_benchmark(source="pdf", capability="content", candidates=[candidate], options=None)
