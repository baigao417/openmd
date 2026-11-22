from __future__ import annotations

import pytest

from openmd.ocr.documents import _BBoxGrid, _opaque_fill_covers, _rendered_ink


class _Page:
    pass


class _PageRect:
    x0 = 0
    y0 = 0
    width = 3
    height = 3


class _Pixmap:
    width = 3
    height = 3
    n = 3
    stride = 9

    def __init__(self):
        pixels = [255, 255, 255] * 9
        offset = (1 * self.width + 1) * self.n
        pixels[offset:offset + 3] = [0, 0, 0]
        self._samples = bytes(pixels)
        self.samples_reads = 0

    @property
    def samples(self):
        self.samples_reads += 1
        return self._samples

    def pixel(self, x, y):
        raise AssertionError("the hot path must read the pixmap buffer in one pass")


def test_rendered_ink_uses_buffer_and_preserves_exact_pixel_coordinates():
    pytest.importorskip("numpy")
    page = _Page()
    page.rect = _PageRect()

    assert _rendered_ink(_Pixmap(), page, (0, 0, 3, 3), 0.1) == [(1, 1)]


def test_rendered_ink_materializes_same_page_pixmap_samples_once():
    pytest.importorskip("numpy")
    page = _Page()
    page.rect = _PageRect()
    pixmap = _Pixmap()
    page_rgb_cache = {}

    assert _rendered_ink(pixmap, page, (0, 0, 2, 2), 0.1, rgb_cache=page_rgb_cache) == [(1, 1)]
    assert _rendered_ink(pixmap, page, (1, 1, 3, 3), 0.1, rgb_cache=page_rgb_cache) == [(1, 1)]
    assert pixmap.samples_reads == 1


def test_opaque_fill_index_checks_only_overlapping_drawings():
    class CountedDrawing(dict):
        reads = 0

        def get(self, key, default=None):
            type(self).reads += 1
            return super().get(key, default)

    unrelated = [CountedDrawing(seqno=index + 10, fill=(0, 0, 0), fill_opacity=1,
                                rect=(1000 + index * 2, 1000, 1001 + index * 2, 1001),
                                scissor=None, items=(("re",),)) for index in range(1000)]
    covering = CountedDrawing(seqno=2000, fill=(0, 0, 0), fill_opacity=1,
                              rect=(1, 1, 4, 4), scissor=None, items=(("re",),))
    drawings = [*unrelated, covering]
    grid = _BBoxGrid([drawing.get("rect") for drawing in drawings])
    CountedDrawing.reads = 0

    assert _opaque_fill_covers(drawings, (2, 2, 3, 3), 5, drawing_grid=grid)
    assert CountedDrawing.reads < 10
