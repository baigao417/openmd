"""Optional PyMuPDF page probing/rendering and mixed native/OCR extraction."""

from __future__ import annotations

import hashlib
import importlib.util
import math
import re
import statistics
import time
from dataclasses import dataclass
from pathlib import Path

from openmd.adapters.local_audio.adapter import local_path_from_input
from openmd.core.models import TranscriptResult
from .config import settings
from .pipeline import LocalOCRBackend, han_ratio, ocr_notes


HARD_HIDDEN_REASONS = frozenset({
    "invisible_render_mode", "low_contrast_text", "tiny_text",
    "out_of_page_or_zero_width", "optional_content_layer",
    "annotation_or_widget_covered", "control_character",
    "opaque_fill_fully_covers_text", "clip_hides_text",
})
# A glyph is treated as clipped out when less than this share of its box lies
# inside the clip that applies to it.
CLIP_VISIBLE_FRACTION_MIN = 0.5
AMBIGUOUS_GEOMETRY_REASONS = frozenset({
    "overlapping_paint", "rendered_text_obscured", "clip_visibility_unproven",
    "text_image_overlap", "text_visibility_unproven", "trace_mapping_unproven",
})


class PDFDependencyMissing(RuntimeError):
    pass


def pymupdf():
    if importlib.util.find_spec("pymupdf") is None:
        raise PDFDependencyMissing('PyMuPDF missing. Install explicitly: python -m pip install "openmd-cli[ocr-router]" (AGPL; assess licensing before distribution).')
    import pymupdf as module

    return module


def text_is_reliable(text: str, config: dict) -> bool:
    chars = [c for c in text if not c.isspace()]
    if len(chars) < config["textlayer_min_chars"]:
        return False
    good = sum(c.isprintable() and c not in {"\ufffd", "\x00"} for c in chars)
    return good / len(chars) >= config["textlayer_min_printable"] and sum(c.isalnum() for c in chars) / len(chars) >= 0.4


def _raw_text_chars(page):
    """Return raw text-layer characters in MuPDF's native content order."""
    raw = page.get_text("rawdict", sort=False)
    return raw, [
        character
        for block in raw.get("blocks", ())
        if block.get("type") == 0
        for line in block.get("lines", ())
        for span in line.get("spans", ())
        for character in span.get("chars", ())
    ]


def _filtered_raw_text(raw, decisions, *, exclude_boxes=()):
    """Rebuild text from rawdict chars, never from get_text(full-page)."""
    decision_index = 0
    unmatched = 0
    output_blocks = []
    for block in raw.get("blocks", ()):
        if block.get("type") != 0:
            continue
        output_lines = []
        for line in block.get("lines", ()):
            output = []
            for character in line.get("spans", ()):
                for item in character.get("chars", ()):
                    codepoint = ord(item.get("c", " ")[0]) if item.get("c") else None
                    raw_bbox = item.get("bbox")
                    decision = None
                    # rawdict and texttrace may expose different synthetic
                    # continuation glyphs (notably Office ligatures). Align
                    # by the next matching Unicode value rather than letting
                    # one extra trace entry shift every following decision.
                    candidates = range(decision_index, min(len(decisions), decision_index + 8))
                    matching = []
                    for candidate_index in candidates:
                        candidate_codepoint = decisions[candidate_index]["codepoint"]
                        if (candidate_codepoint == codepoint
                                or (codepoint is not None and chr(codepoint).isspace()
                                    and chr(candidate_codepoint).isspace())):
                            candidate = decisions[candidate_index]
                            bbox = candidate.get("bbox")
                            distance = (sum(abs(a - b) for a, b in zip(raw_bbox, bbox))
                                        if raw_bbox is not None and bbox is not None else float("inf"))
                            matching.append((distance, candidate_index, candidate))
                    if matching:
                        _, candidate_index, decision = min(matching, key=lambda entry: entry[0])
                        decision_index = candidate_index + 1
                    if decision is None:
                        output.append(item.get("c", ""))
                        unmatched += 0 if item.get("c", "").isspace() else 1
                    elif (not decision.get("remove")
                          and not any(_intersection_area(raw_bbox, box) > 0
                                      for box in exclude_boxes if raw_bbox is not None)):
                        output.append(item.get("c", ""))
            output_lines.append("".join(output))
        if output_lines:
            output_blocks.append("\n".join(output_lines))
    return "\n\n".join(output_blocks) + ("\n" if output_blocks else ""), unmatched


def _bad_text_layer(raw):
    """Detect line-local decoding garbage before it reaches an agent."""
    bad_lines = bad_chars = 0
    for block in raw.get("blocks", ()):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", ()):
            chars = [item.get("c", "") for span in line.get("spans", ()) for item in span.get("chars", ())]
            if not chars:
                continue
            bad = sum(1 for char in chars if char == "\ufffd" or 0xE000 <= ord(char) <= 0xF8FF
                      or (not char.isprintable() and not char.isspace()))
            if bad and (bad >= 4 or bad / len(chars) > 0.2):
                bad_lines += 1
                bad_chars += bad
    return bad_lines, bad_chars


def _covered_area(rectangles: list[tuple[float, float, float, float]], bounds) -> float:
    """Union clipped image bounds; overlapping images cannot inflate coverage."""
    clipped = [(max(bounds.x0, x0), max(bounds.y0, y0), min(bounds.x1, x1), min(bounds.y1, y1)) for x0, y0, x1, y1 in rectangles]
    clipped = [rect for rect in clipped if rect[2] > rect[0] and rect[3] > rect[1]]
    edges = sorted({x for rect in clipped for x in (rect[0], rect[2])})
    area = 0.0
    for left, right in zip(edges, edges[1:]):
        intervals = sorted((y0, y1) for x0, y0, x1, y1 in clipped if x0 < right and x1 > left)
        covered, end = 0.0, float("-inf")
        for start, stop in intervals:
            covered += max(0.0, stop - max(start, end))
            end = max(end, stop)
        area += (right - left) * covered
    return area


def _span_rgb(span: dict) -> tuple[float, float, float]:
    color = span.get("color", (0.0,))
    if isinstance(color, (int, float)):
        color = (color,)
    if len(color) == 1:
        return (color[0],) * 3
    if len(color) == 4:
        c, m, y, k = color
        return tuple(1 - min(1, component + k) for component in (c, m, y))
    return tuple(color[:3])


def _pixels(pixmap, page, box):
    """Read clipped page coordinates from one bounded, low DPI RGB raster."""
    scale_x, scale_y = pixmap.width / page.rect.width, pixmap.height / page.rect.height
    x0 = max(0, math.floor((box[0] - page.rect.x0) * scale_x))
    y0 = max(0, math.floor((box[1] - page.rect.y0) * scale_y))
    x1 = min(pixmap.width, math.ceil((box[2] - page.rect.x0) * scale_x))
    y1 = min(pixmap.height, math.ceil((box[3] - page.rect.y0) * scale_y))
    return [(x, y, pixmap.pixel(x, y)[:3]) for y in range(y0, y1) for x in range(x0, x1)]


def _numpy_module():
    try:
        import numpy as np
    except ImportError:
        return None
    return np


def _pixmap_rgb_array(pixmap, np):
    """Expose the existing RGB raster as a zero-copy array when NumPy is present."""
    samples = np.frombuffer(pixmap.samples, dtype=np.uint8)
    rows = samples.reshape(pixmap.height, pixmap.stride)
    return rows[:, :pixmap.width * pixmap.n].reshape(pixmap.height, pixmap.width, pixmap.n)[:, :, :3]


def _rendered_ink(pixmap, page, box, delta, *, rgb_cache=None):
    np = _numpy_module()
    if np is not None:
        cache_key = id(pixmap)
        rgb = rgb_cache.get(cache_key) if rgb_cache is not None else None
        if rgb is None:
            rgb = _pixmap_rgb_array(pixmap, np)
            if rgb_cache is not None:
                rgb_cache[cache_key] = rgb
        scale_x, scale_y = pixmap.width / page.rect.width, pixmap.height / page.rect.height
        x0 = max(0, math.floor((box[0] - page.rect.x0) * scale_x))
        y0 = max(0, math.floor((box[1] - page.rect.y0) * scale_y))
        x1 = min(pixmap.width, math.ceil((box[2] - page.rect.x0) * scale_x))
        y1 = min(pixmap.height, math.ceil((box[3] - page.rect.y0) * scale_y))
        pixels = rgb[y0:y1, x0:x1]
        if pixels.size == 0:
            return []
        background = np.median(pixels.reshape(-1, 3), axis=0)
        mask = np.max(np.abs(pixels.astype("int16") - background), axis=2) > delta * 255
        ys, xs = np.nonzero(mask)
        return [(int(x + x0), int(y + y0)) for y, x in zip(ys, xs)]
    pixels = _pixels(pixmap, page, box)
    if not pixels:
        return []
    # The dominant colour *inside* a glyph box is its background. Sampling only
    # outside the span mistakes a later coloured cover for the original glyph.
    background = tuple(statistics.median(p[2][channel] for p in pixels) for channel in range(3))
    return [(x, y) for x, y, rgb in pixels if max(abs(a - b) for a, b in zip(rgb, background)) > delta * 255]


def _rendered_ink_region(page, box, dpi, delta):
    """Render only a glyph-sized crop for the high-resolution retry."""
    module = pymupdf()
    pad = 1.5
    clip = module.Rect(max(page.rect.x0, box[0] - pad), max(page.rect.y0, box[1] - pad),
                       min(page.rect.x1, box[2] + pad), min(page.rect.y1, box[3] + pad))
    pixmap = page.get_pixmap(dpi=dpi, clip=clip, alpha=False, colorspace=module.csRGB)
    np = _numpy_module()
    if np is not None:
        rgb = _pixmap_rgb_array(pixmap, np)
        if rgb.size == 0:
            return []
        background = np.median(rgb.reshape(-1, 3), axis=0)
        mask = np.max(np.abs(rgb.astype("int16") - background), axis=2) > delta * 255
        return [tuple(pixel) for pixel in rgb[mask].tolist()]
    pixels = [pixmap.pixel(x, y)[:3] for y in range(pixmap.height) for x in range(pixmap.width)]
    if not pixels:
        return []
    background = tuple(statistics.median(pixel[channel] for pixel in pixels) for channel in range(3))
    return [pixel for pixel in pixels if max(abs(a - b) for a, b in zip(pixel, background)) > delta * 255]


def _intersection_area(a, b):
    return max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(0, min(a[3], b[3]) - max(a[1], b[1]))


class _BBoxGrid:
    """Small page-local index for exact overlap checks on nearby PDF objects."""

    def __init__(self, boxes, *, cell_size=64):
        self.cell_size = cell_size
        self.bins = {}
        self.boxes = boxes
        self.large = []
        for index, box in enumerate(boxes):
            if box is None or len(box) != 4:
                continue
            keys = tuple(self._keys(box))
            if keys == ((-1, -1),):
                self.large.append(index)
                continue
            for key in keys:
                self.bins.setdefault(key, []).append(index)

    def _keys(self, box):
        size = self.cell_size
        x0, y0, x1, y1 = box
        left, right = math.floor(x0 / size), math.floor(x1 / size)
        top, bottom = math.floor(y0 / size), math.floor(y1 / size)
        # Malformed giant boxes must not allocate a page-sized number of bins.
        if (right - left + 1) * (bottom - top + 1) > 4096:
            return ((-1, -1),)
        return ((x, y) for x in range(left, right + 1) for y in range(top, bottom + 1))

    def query(self, box):
        keys = tuple(self._keys(box))
        if keys == ((-1, -1),):
            return range(len(self.boxes))
        seen = set()
        for key in keys:
            seen.update(self.bins.get(key, ()))
        seen.update(self.large)
        return seen


def _rect_contains(outer, inner, tolerance=0.01):
    return (outer[0] <= inner[0] + tolerance and outer[1] <= inner[1] + tolerance
            and outer[2] >= inner[2] - tolerance and outer[3] >= inner[3] - tolerance)


def _opaque_fill_covers(drawings, charbox, seqno, opaque_overlays=(), *, drawing_grid=None):
    """Return true only for a later opaque rectangular fill enclosing a glyph."""
    indices = drawing_grid.query(charbox) if drawing_grid is not None else range(len(drawings))
    for index in indices:
        drawing = drawings[index]
        if drawing.get("seqno", -1) <= seqno or drawing.get("fill") is None or drawing.get("rect") is None:
            continue
        if drawing.get("fill_opacity", 1) < 0.999 or not _rect_contains(drawing["rect"], charbox):
            continue
        if drawing.get("scissor") is not None and not _rect_contains(drawing["scissor"], charbox):
            continue
        items = drawing.get("items", ())
        if len(items) == 1 and items[0][0] == "re":
            return True
    return any(_rect_contains(annotation, charbox) for annotation in opaque_overlays)


def _clip_operator(data: bytes) -> bool:
    """Conservatively recognize path/text clips without parsing string contents.

    MuPDF's drawing device does not report text clipping (Tr 4..7). Comments,
    nested literal strings, escaped delimiters, hex strings and names are PDF
    lexical objects, not operators. Unknown Tr operands also fail closed.
    """
    delimiters = b"\x00\t\n\f\r ()<>[]{}/%"
    known = set(b"w J j M d ri i gs q Q cm m l c v y h re S s f F f* B B* b b* n BT ET Tc Tw Tz TL Tf Tr Ts Td TD Tm T* Tj TJ ' \" d0 d1 CS cs SC SCN sc scn G g RG rg K k sh Do MP DP BMC BDC EMC BX EX true false null R".split())
    index, previous = 0, None
    while index < len(data):
        char = data[index]
        if char in b"\x00\t\n\f\r ":
            index += 1
        elif char == ord("%"):
            match = re.search(rb"[\r\n]", data[index:])
            index = index + match.end() if match else len(data)
        elif char == ord("("):
            index, depth = index + 1, 1
            while index < len(data) and depth:
                if data[index] == ord("\\"):
                    index += 2
                    continue
                depth += (data[index] == ord("(")) - (data[index] == ord(")"))
                index += 1
            if depth:
                return True
            previous = None
        elif char == ord("<") and data[index:index + 2] != b"<<":
            stop = data.find(b">", index + 1)
            if stop < 0:
                return True
            index = len(data) if stop < 0 else stop + 1
            previous = None
        elif char == ord("/"):
            index += 1
            while index < len(data) and data[index] not in delimiters:
                index += 1
            previous = None
        elif char in b"<>[]{}":
            index += 1
            previous = None
        else:
            end = index + 1
            while end < len(data) and data[end] not in delimiters:
                end += 1
            token, index = data[index:end], end
            # Inline image payloads are binary rather than PDF lexical tokens.
            # Without a full content interpreter their following graphics state
            # cannot be proved; reject the user page rather than scan through it.
            if token in {b"W", b"W*", b"BI", b"ID", b"EI"}:
                return True
            if token not in known and not re.fullmatch(rb"[+-]?(?:\d+(?:\.\d*)?|\.\d+)", token):
                # Do not guess how a repairing renderer interprets malformed
                # or unknown instructions (including concatenated '4Tr').
                return True
            if token == b"Tr":
                try:
                    if float(previous) not in {0, 1, 2, 3}:
                        return True
                except (ValueError, TypeError):
                    return True
            previous = token
    return False


def _content_has_clip(page) -> bool:
    # The resource inventory includes nested Form XObjects. A clip inside any
    # referenced Form is enough to reject; resource reachability is not guessed.
    xrefs = set(page.get_contents()) | {entry[0] for entry in page.get_xobjects()}
    for xref in xrefs:
        data = page.parent.xref_stream(xref)
        if not isinstance(data, bytes):
            raise ValueError("unreadable_content_stream")
        if _clip_operator(data):
            return True
    return False


@dataclass(frozen=True)
class _OfficePDFOrigin:
    """An in-process capability issued at the converter call site, never metadata."""

    path: Path
    sha256: str


def _office_pdf_origin(pdf: Path, source: Path) -> _OfficePDFOrigin | None:
    # XLSX is deliberately outside the approved glyph-alternative exemption.
    if source.suffix.lower() not in {".docx", ".pptx"}:
        return None
    return _OfficePDFOrigin(pdf.resolve(), hashlib.sha256(pdf.read_bytes()).hexdigest())


def _office_vector_alternative(span, drawings, ink, page, pixmap):
    """Recognize converter extraction alternatives ONLY behind origin validation.

    Nearby vector ink is not a proof of character identity on arbitrary PDFs.
    The caller must hold the capability for this invocation's converted bytes.
    """
    if span.get("type") != 3 or not ink:
        return False
    box, seqno = span["bbox"], span.get("seqno", -1)
    candidates = [drawing["rect"] for drawing in drawings
                  if drawing.get("type") in {"f", "fs"}
                  and drawing.get("fill_opacity", 0) > 0.01
                  and drawing.get("seqno", seqno) < seqno
                  and _intersection_area(box, drawing["rect"]) > 0]
    if not candidates:
        return False
    def supported(point):
        x = page.rect.x0 + (point[0] + 0.5) * page.rect.width / pixmap.width
        y = page.rect.y0 + (point[1] + 0.5) * page.rect.height / pixmap.height
        return any(rect.x0 - 1 <= x <= rect.x1 + 1 and rect.y0 - 1 <= y <= rect.y1 + 1 for rect in candidates)
    return all(supported(point) for point in ink)


def _text_paint_groups(page, spans):
    """Identify two device traces emitted from one interpreter text object.

    MuPDF flushes a fill+stroke text operation as two callbacks carrying the
    SAME retained fz_text and transform (Tr2 / Tr6). Independent overlapping
    text has no such identity. Retaining the object is essential: allocator
    address reuse must never turn a later draw into the earlier operation.
    No string, font or geometry equality can grant this exemption.
    """
    if not any(s.get("type") == 0 for s in spans) or not any(s.get("type") == 1 for s in spans):
        return {}
    module = pymupdf()
    try:
        class PaintTrace(module.JM_new_texttrace_device):
            def __init__(self, out):
                super().__init__(out)
                self.fill = None
                self.groups = {}

            def fill_text(self, ctx, text, ctm, *args):
                retained = module.mupdf.FzText(module.mupdf.ll_fz_keep_text(text))
                self.fill = (retained, int(text.this), self.seqno,
                             (ctm.a, ctm.b, ctm.c, ctm.d, ctm.e, ctm.f))
                super().fill_text(ctx, text, ctm, *args)

            def stroke_text(self, ctx, text, stroke, ctm, *args):
                if self.fill is not None:
                    retained, pointer, seqno, matrix = self.fill
                    if (self.seqno == seqno + 1 and int(text.this) == pointer
                            and matrix == (ctm.a, ctm.b, ctm.c, ctm.d, ctm.e, ctm.f)):
                        self.groups[seqno] = self.groups[self.seqno] = seqno
                self.fill = None
                super().stroke_text(ctx, text, stroke, ctm, *args)

        out = []
        dev = PaintTrace(out)
        rotation = page.rotation
        try:
            page.set_rotation(0)
            prect = module.mupdf.fz_bound_page(page.this)
            dev.ptm = module.mupdf.FzMatrix(1, 0, 0, -1, 0, prect.y1)
            module.mupdf.fz_run_page(page.this, dev, module.mupdf.FzMatrix(), module.mupdf.FzCookie())
        finally:
            module.mupdf.fz_close_device(dev)
            page.set_rotation(rotation)
        # Version/parser mismatches grant no waiver. Identity comes from the
        # callbacks; this checks their sequence agrees with get_texttrace.
        if [(s["seqno"], s["type"]) for s in out] != [(s.get("seqno"), s.get("type")) for s in spans]:
            return {}
        # A text buffer can coalesce multiple source shows. Require the parsed
        # source show count to match the distinct device paints after pairing;
        # otherwise even a shared buffer cannot prove one source operation.
        # Forms/Type3 can expand or replay nested shows: no count-based waiver.
        if page.get_xobjects() or any(font[2] == "Type3" for font in page.get_fonts(full=True)):
            return {}

        class Shows(module.mupdf.PdfProcessor2):
            def __init__(self):
                super().__init__()
                self.count = 0
                for name in ("Tj", "TJ", "squote", "dquote"):
                    getattr(self, "use_virtual_op_" + name)()

            def show(self, *args):
                self.count += 1

            op_Tj = op_TJ = op_squote = op_dquote = show

        shows = Shows()
        pdf_page = module.mupdf.pdf_page_from_fz_page(page.this)
        doc = module.mupdf.pdf_document_from_fz_document(page.parent.this)
        module.mupdf.pdf_process_contents(shows, doc, module.mupdf.pdf_page_resources(pdf_page),
                                         module.mupdf.pdf_page_contents(pdf_page), module.mupdf.FzCookie())
        paints = {dev.groups.get(s["seqno"], s["seqno"]) for s in out}
        if shows.count != len(paints):
            return {}
        return dev.groups
    except (AttributeError, TypeError, ValueError, RuntimeError):
        return {}


def _paint_self_overlap(chars):
    """A buffered fz_text may contain several overlapping source text shows.

    Nonadjacent overlaps cannot be explained by ordinary adjacent kerning.
    This check only rejects: it never grants paint identity. Pairing a combined
    paint separately requires one parsed source show per device paint above.
    """
    active = []
    for index, box in sorted(enumerate(char[3] for char in chars), key=lambda pair: pair[1][0]):
        active = [(i, other) for i, other in active if other[2] > box[0]]
        if any(abs(index - i) > 1 and _intersection_area(box, other) > 0.001 for i, other in active):
            return True
        active.append((index, box))
    return False


def _unique_rectangles(rectangles):
    """Collapse duplicate clip/scissor bounds without changing overlap decisions."""
    unique = {}
    for rect in rectangles:
        if rect is not None:
            unique.setdefault(tuple(float(value) for value in rect), rect)
    return list(unique.values())


def _visibility(page, config: dict, images: list, *, office_generated: bool = False) -> dict:
    """Classify and filter individual glyphs while retaining native order."""
    spans = page.get_texttrace()
    paint_groups = _text_paint_groups(page, spans) if not office_generated else {}
    drawings = page.get_drawings(extended=True)
    clip_rects = _unique_rectangles(drawing.get("scissor") for drawing in drawings
                                    if drawing.get("type") == "clip" and drawing.get("scissor") is not None)
    clip_grid = _BBoxGrid([tuple(rect) for rect in clip_rects])
    paint = page.get_bboxlog()
    drawing_grid = _BBoxGrid([tuple(drawing["rect"]) if drawing.get("rect") is not None else None
                              for drawing in drawings])
    total = invisible = low_contrast = obscured = vector_alternatives = glyph_alternatives = image_overlap = unproven = paint_overlap = 0
    hard_reasons = []
    soft_reasons = []
    decisions = []
    # Clip paths may be arbitrary, nested, text-derived, or in a Form XObject.
    # Do not approximate the clipped glyph identity from its surviving pixels.
    clips = sum(drawing.get("type") == "clip" for drawing in drawings)
    content_clip = not office_generated and _content_has_clip(page)
    annotations = list(page.annots() or ())
    overlays = [entry.rect for entry in annotations]
    overlays.extend(entry.rect for entry in (page.widgets() or ()))
    opaque_overlays = [entry.rect for entry in annotations
                       if bool((entry.colors or {}).get("fill"))
                       and (getattr(entry, "opacity", 1) == -1
                            or getattr(entry, "opacity", 1) >= 0.999)]
    pixmap = page.get_pixmap(dpi=72, alpha=False, colorspace=pymupdf().csRGB)
    # This cache lives only for this page's visibility pass. All character
    # boxes share the same raster and rendering parameters.
    rgb_cache = {}
    span_boxes = [span.get("bbox") for span in spans]
    span_grid = _BBoxGrid(span_boxes)
    paint_boxes = [entry[1] for entry in paint]
    paint_grid = _BBoxGrid(paint_boxes)
    for span_index, span in enumerate(spans):
        chars = list(span.get("chars", ()))
        visible_chars = [char for char in chars if char[0] > 32 and not chr(char[0]).isspace()]
        count = len(visible_chars)
        total += count
        if not chars:
            continue
        box = span["bbox"]
        # MuPDF exposes a converter ligature's extra Unicode characters with
        # glyph id -1 and zero advance at the painted glyph's same origin.
        # Like outlined alternatives, this is only trusted for our conversion.
        cluster_alternatives = [char for char in visible_chars if office_generated and char[1] == -1
                                and any(other[1] >= 0 and other[2] == char[2]
                                        and _intersection_area(other[3], page.rect) > 0
                                        for other in visible_chars)]
        glyph_alternatives += len(cluster_alternatives)
        if span.get("layer") or span.get("type") not in {0, 1, 3}:
            unproven += count
        span_soft = set()
        span_hard = set()
        if span.get("size", float("inf")) <= 1.5:
            hard_reasons.append("tiny_text")
            span_hard.update(id(char) for char in visible_chars)
        for char in chars:
            charbox = char[3]
            chararea = (charbox[2] - charbox[0]) * (charbox[3] - charbox[1])
            codepoint = char[0]
            char_key = id(char)
            if chr(codepoint).isspace():
                continue
            if char[1] == -1 and not office_generated:
                span_soft.add(char_key)
                soft_reasons.append("trace_mapping_unproven")
            if char in cluster_alternatives:
                continue
            if codepoint < 32 and not chr(codepoint).isspace():
                span_hard.add(char_key)
                hard_reasons.append("control_character")
            if chararea <= 0 or _intersection_area(charbox, page.rect) < chararea - 0.001:
                span_hard.add(char_key)
                hard_reasons.append("out_of_page_or_zero_width")
            if any(_intersection_area(charbox, image["bbox"]) > 0
                   for image in images if image.get("bbox")):
                image_overlap += 1
                span_soft.add(id(char))
                soft_reasons.append("text_image_overlap")
        # Every later overlapping paint can hide or alter a character.
        # This includes strokes, transparent fills, shades and images; do not
        # guess cover identity from shape, colour, or percentage of bbox area.
        later_paint_indexes = [index for index in paint_grid.query(box)
                               if index > span.get("seqno", -1)
                               and paint[index][0] not in {"ignore-text", "fill-text", "stroke-text"}
                               and _intersection_area(box, paint[index][1]) > 0]
        later_paint = bool(later_paint_indexes)
        later_span_indexes = [index for index in span_grid.query(box)
                              if spans[index].get("seqno", -1) > span.get("seqno", -1)
                              and not (span.get("seqno", -1) in paint_groups
                                       and paint_groups.get(spans[index].get("seqno")) == paint_groups[span["seqno"]])
                              and spans[index].get("type") != 3
                              and _intersection_area(box, span_boxes[index]) > 0]
        if not office_generated:
            # bboxlog pads glyph paint boxes and span boxes include spaces;
            # those overlap ordinary adjacent words. Actual nonspace character
            # boxes must overlap before another text span can be an occluder.
            later_paint = later_paint or any(
                later.get("seqno", -1) > span.get("seqno", -1)
                and not (span.get("seqno") in paint_groups
                         and paint_groups.get(later.get("seqno")) == paint_groups[span["seqno"]])
                and later.get("type") != 3 and _intersection_area(box, later["bbox"]) > 0
                and any(_intersection_area(char[3], other[3]) > 0 for char in chars
                        for other in later.get("chars", ()) if other[0] > 32 and not chr(other[0]).isspace())
                for later in (spans[index] for index in later_span_indexes))
            later_paint = later_paint or _paint_self_overlap(chars)
        if later_paint:
            paint_overlap += count
        span_seqno = span.get("seqno", -1)
        later_span_chars = [span_boxes[index] for index in span_grid.query(box)
                            if spans[index].get("seqno", -1) > span_seqno
                            and not (span_seqno in paint_groups
                                     and paint_groups.get(spans[index].get("seqno")) == paint_groups[span_seqno])
                            and spans[index].get("type") != 3]
        later_paints = [paint[index][1] for index in paint_grid.query(box)
                        if index > span_seqno
                        and paint[index][0] not in {"ignore-text", "fill-text", "stroke-text"}
                        and _intersection_area(box, paint[index][1]) > 0
                        and not any(_intersection_area(paint[index][1], overlay) > 0 for overlay in overlays)]
        self_overlap = _paint_self_overlap(chars)
        if any(_intersection_area(char[3], rect) > 0 for char in visible_chars for rect in overlays):
            unproven += sum(1 for char in visible_chars
                            if any(_intersection_area(char[3], rect) > 0 for rect in overlays))
        # Span pixels are only an extraction alternative for our own Office
        # conversion, where outlined glyphs may accompany the text trace. For
        # arbitrary PDFs, keep clear vector text on the vector-evidence path;
        # uncertain individual glyphs still use the shared page raster below.
        ink = (_rendered_ink(pixmap, page, box, config["textlayer_background_delta"], rgb_cache=rgb_cache)
               if office_generated else [])
        if office_generated and not later_paint and _office_vector_alternative(span, drawings, ink, page, pixmap):
            vector_alternatives += count
            decisions.extend({"remove": False, "hard": False, "soft": False,
                              "bbox": char[3], "seqno": span.get("seqno", -1), "codepoint": char[0]}
                             for char in chars)
            continue
        for char in chars:
            charbox = char[3]
            char_key = id(char)
            if chr(char[0]).isspace():
                continue
            intersecting_clips = [clip_rects[index] for index in clip_grid.query(charbox)
                                  if _intersection_area(charbox, clip_rects[index]) > 0]
            clip_partially_covers_glyph = any(
                rect.x0 > charbox[0] + 0.01 or rect.y0 > charbox[1] + 0.01
                or rect.x1 < charbox[2] - 0.01 or rect.y1 < charbox[3] - 0.01
                for rect in intersecting_clips
            )
            clip_covers_glyph_bounds = any(
                rect.x0 <= charbox[0] + 0.01 and rect.y0 <= charbox[1] + 0.01
                and rect.x1 >= charbox[2] - 0.01 and rect.y1 >= charbox[3] - 0.01
                for rect in intersecting_clips
            )
            clip_unmapped = bool(content_clip or clips) and not intersecting_clips
            if clip_partially_covers_glyph:
                glyph_area = max(1e-6, (charbox[2] - charbox[0]) * (charbox[3] - charbox[1]))
                visible = min(_intersection_area(charbox, rect) for rect in intersecting_clips) / glyph_area
                if visible < CLIP_VISIBLE_FRACTION_MIN:
                    # Most of the glyph lies outside its clip: a reader cannot see it.
                    span_hard.add(char_key)
                    span_soft.discard(char_key)
                    hard_reasons.append("clip_hides_text")
                    continue
                span_soft.add(char_key)
                soft_reasons.append("clip_visibility_unproven")
                continue
            if (any(_intersection_area(charbox, bounds) > 0 for bounds in later_paints)
                    or any(_intersection_area(charbox, bounds) > 0 for bounds in later_span_chars)):
                span_soft.add(char_key)
                soft_reasons.append("overlapping_paint")
            if self_overlap:
                span_soft.add(char_key)
                soft_reasons.append("overlapping_paint")
            if char in cluster_alternatives:
                continue
            if span.get("type") == 3 or span.get("opacity", 1) <= 0:
                invisible += 1
                span_hard.add(char_key)
                hard_reasons.append("invisible_render_mode")
                continue
            if any(_rect_contains(tuple(rect), charbox) for rect in opaque_overlays):
                span_hard.add(char_key)
                span_soft.discard(char_key)
                hard_reasons.append("annotation_or_widget_covered")
                continue
            if _opaque_fill_covers(drawings, charbox, span_seqno, drawing_grid=drawing_grid):
                span_hard.add(char_key)
                span_soft.discard(char_key)
                hard_reasons.append("opaque_fill_fully_covers_text")
                continue
            if any(_intersection_area(charbox, bounds) > 0 for bounds in later_paints):
                # Page paint order has already established a geometric overlap;
                # the local raster check below only resolves ambiguous cases.
                continue
            if any(_intersection_area(charbox, bounds) > 0 for bounds in later_span_chars):
                # Another text paint may obscure this glyph. Native overlap
                # is soft evidence and must not trigger per-glyph DPI renders.
                continue
            geometry_uncertain = (
                any(_intersection_area(charbox, bounds) > 0 for bounds in later_paints)
                or any(_intersection_area(charbox, bounds) > 0 for bounds in later_span_chars)
                or any(_intersection_area(charbox, image["bbox"]) > 0 for image in images if image.get("bbox"))
                or any(_intersection_area(charbox, rect) > 0 for rect in overlays)
                or bool(intersecting_clips) or clip_unmapped or self_overlap or bool(span.get("layer"))
            )
            ink216 = [(1,)] if not geometry_uncertain else _rendered_ink(
                pixmap, page, charbox, config["textlayer_background_delta"], rgb_cache=rgb_cache)
            if geometry_uncertain and not ink216:
                ink216 = _rendered_ink_region(page, charbox, 216, config["textlayer_background_delta"])
                visibility_dpi = 216
            else:
                visibility_dpi = 72
            if geometry_uncertain and not ink216:
                obscured += 1
                if _opaque_fill_covers(drawings, charbox, span_seqno, opaque_overlays,
                                       drawing_grid=drawing_grid):
                    span_hard.add(char_key)
                    hard_reasons.append("opaque_fill_fully_covers_text")
                elif span.get("layer"):
                    span_hard.add(char_key)
                    hard_reasons.append("optional_content_layer")
                elif clip_unmapped:
                    # A clip is active, the glyph lies outside every clip box and
                    # leaves no ink even at 216 DPI: it is clipped out.
                    span_hard.add(char_key)
                    span_soft.discard(char_key)
                    hard_reasons.append("clip_hides_text")
                elif clip_covers_glyph_bounds:
                    span_soft.add(char_key)
                    soft_reasons.append("clip_visibility_unproven")
                elif any(_intersection_area(charbox, rect) > 0 for rect in overlays):
                    if _opaque_fill_covers(drawings, charbox, span_seqno, opaque_overlays,
                                           drawing_grid=drawing_grid):
                        span_hard.add(char_key)
                        hard_reasons.append("annotation_or_widget_covered")
                    else:
                        span_soft.add(char_key)
                        soft_reasons.append("annotation_or_widget_overlay")
                else:
                    span_soft.add(char_key)
                    soft_reasons.append("text_visibility_unproven")
                continue
            if later_paint:
                span_soft.add(char_key)
                soft_reasons.append("overlapping_paint")
            if content_clip or clips:
                span_soft.add(char_key)
                soft_reasons.append("clip_visibility_unproven")
            if any(_intersection_area(charbox, rect) > 0 for rect in overlays):
                span_soft.add(char_key)
                soft_reasons.append("annotation_or_widget_overlay")
            if span.get("layer"):
                span_soft.add(char_key)
                soft_reasons.append("text_visibility_unproven")
        x0, y0, x1, y1 = box
        samples = []
        for x in (x0 - 2, (x0 + x1) / 2, x1 + 2):
            for y in (y0 - 2, y1 + 2):
                px = int((x - page.rect.x0) * pixmap.width / page.rect.width)
                py = int((y - page.rect.y0) * pixmap.height / page.rect.height)
                if 0 <= px < pixmap.width and 0 <= py < pixmap.height:
                    samples.append(pixmap.pixel(px, py)[:3])
        if samples:
            background = tuple(statistics.median(sample[channel] for sample in samples) / 255 for channel in range(3))
            if max(abs(a - b) for a, b in zip(_span_rgb(span), background)) <= config["textlayer_background_delta"]:
                low_contrast += count
                for char in visible_chars:
                    if id(char) not in span_hard:
                        span_hard.add(id(char))
                        hard_reasons.append("low_contrast_text")
        for char in chars:
            remove = id(char) in span_hard
            decisions.append({"remove": remove, "hard": remove, "soft": id(char) in span_soft and not remove,
                              "bbox": char[3], "seqno": span.get("seqno", -1), "codepoint": char[0], "glyph_id": char[1]})
        unproven += len(span_soft - span_hard)
    if not any(decision["soft"] for decision in decisions):
        soft_reasons = []
    return {"invisible_text_chars": invisible, "low_contrast_text_ratio": round(low_contrast / max(total, 1), 4), "low_contrast_text_chars": low_contrast,
            "render_obscured_text_chars": obscured, "text_image_overlap_chars": image_overlap,
            "vector_alternative_text_chars": vector_alternatives, "render_consistency_dpi": 72,
            "office_glyph_alternative_text_chars": glyph_alternatives,
            "clip_paths": clips, "overlapping_paint_text_chars": paint_overlap,
            "content_clip_operator": content_clip,
            "unproven_text_chars": unproven, "traced_text_chars": total,
            "textlayer_origin": "openmd_office_conversion" if office_generated else "user_pdf",
            "decisions": decisions, "hard_reasons": sorted(set(hard_reasons)),
            "soft_reasons": sorted(set(soft_reasons)),
            "hidden_text_removed_chars": sum(1 for decision in decisions if decision["remove"]),
            "hidden_text_removed_positions": [decision["bbox"] for decision in decisions if decision["remove"]]}


def probe_page(page, config: dict, *, _office_generated: bool = False) -> dict:
    start = time.monotonic()
    # Preserve the PDF's native content stream. Forced y/x sorting interleaves
    # adjacent columns and changes source order on native multicolumn pages.
    raw_text, raw_chars = _raw_text_chars(page)
    text = "".join(item.get("c", "") for item in raw_chars)
    reliable = text_is_reliable(text, config)
    images = page.get_image_info()
    area = max(1, page.rect.width * page.rect.height)
    image_area = _covered_area([item["bbox"] for item in images if item.get("bbox")], page.rect)
    words = page.get_text("words")
    text_area = sum(max(0, w[2] - w[0]) * max(0, w[3] - w[1]) for w in words)
    reasons = []
    visibility = {"invisible_text_chars": 0, "low_contrast_text_ratio": 0.0, "low_contrast_text_chars": 0, "render_obscured_text_chars": 0, "text_image_overlap_chars": 0, "vector_alternative_text_chars": 0, "render_consistency_dpi": 72,
                  "clip_paths": 0, "content_clip_operator": False, "overlapping_paint_text_chars": 0, "unproven_text_chars": 0,
                  "traced_text_chars": 0, "office_glyph_alternative_text_chars": 0, "textlayer_origin": "openmd_office_conversion" if _office_generated else "user_pdf",
                  "decisions": [], "hard_reasons": [], "soft_reasons": [], "hidden_text_removed_chars": 0,
                  "hidden_text_removed_positions": []}
    if raw_chars:
        # Large scans may carry plausible but contradictory OCR text layers.
        # Their visible pixels are authoritative regardless of text density.
        if image_area / area >= config["textlayer_image_coverage"]:
            reasons.append("image_coverage")
        try:
            visibility = _visibility(page, config, images, office_generated=_office_generated)
            if image_area / area >= config["textlayer_image_coverage"]:
                for decision in visibility["decisions"]:
                    if (not chr(decision["codepoint"]).isspace()
                            and any(image.get("bbox")
                                    and _intersection_area(decision["bbox"], image["bbox"]) > 0
                                    for image in images)):
                        decision["remove"] = decision["hard"] = True
                if any(decision["remove"] for decision in visibility["decisions"]):
                    visibility["hard_reasons"] = sorted(set((*visibility["hard_reasons"], "image_coverage_text_overlap")))
                visibility["hidden_text_removed_chars"] = sum(1 for decision in visibility["decisions"] if decision["remove"])
                visibility["hidden_text_removed_positions"] = [decision["bbox"] for decision in visibility["decisions"] if decision["remove"]]
            text, unmatched = _filtered_raw_text(raw_text, visibility["decisions"])
            if not _office_generated and any(decision.get("glyph_id") == -1 and not decision.get("remove")
                                             for decision in visibility["decisions"]):
                visibility["soft_reasons"] = sorted(set((*visibility["soft_reasons"], "trace_mapping_unproven")))
                visibility["unproven_text_chars"] += 1
            if unmatched:
                visibility["soft_reasons"] = sorted(set((*visibility["soft_reasons"], "trace_mapping_unproven")))
                visibility["unproven_text_chars"] += unmatched
            reliable = text_is_reliable(text, config)
        except (ValueError, RuntimeError, OverflowError):
            # A visibility parser failure is not permission to return its text.
            # Keep the page eligible for visual OCR, including mixed documents.
            reasons.append("visibility_probe_failed")
            text = ""
            reliable = False
        if visibility["invisible_text_chars"]:
            reasons.append("invisible_render_mode")
        if visibility["low_contrast_text_chars"]:
            reasons.append("low_contrast_text")
        if visibility["render_obscured_text_chars"]:
            reasons.append("rendered_text_obscured")
        if visibility["text_image_overlap_chars"]:
            reasons.append("text_image_overlap")
        if (visibility["clip_paths"] or visibility["content_clip_operator"]) and not _office_generated:
            reasons.append("clip_visibility_unproven")
        if visibility["overlapping_paint_text_chars"]:
            reasons.append("overlapping_paint")
        if visibility["unproven_text_chars"] or not visibility["traced_text_chars"]:
            reasons.append("text_visibility_unproven")
        bad_lines, bad_chars = _bad_text_layer(raw_text)
        if bad_lines:
            reasons.append("broken_text_layer")
        if not reliable:
            reasons.append("text_quality")
        hard_reasons = set(visibility.get("hard_reasons", ()))
        soft_reasons = set(visibility.get("soft_reasons", ()))
        # Hard signals are removed from the reconstructed layer. Soft signals
        # keep their readable native text but force a truthful unverified flag.
        textlayer_unverified = bool(soft_reasons or bad_lines or visibility["unproven_text_chars"]
                                    or visibility["clip_paths"] or visibility["content_clip_operator"])
        # A page made entirely of removed glyphs has no safe native payload.
        has_safe_text = bool(text.strip()) and reliable
        force_ocr = ("image_coverage" in reasons or not has_safe_text
                     or bool(soft_reasons) or bool(bad_lines)
                     or "visibility_probe_failed" in reasons
                     or visibility["clip_paths"] > 0 or visibility["content_clip_operator"]
                     or ("text_quality" in reasons and not text.strip()))
        reliable = has_safe_text and not force_ocr
        if visibility.get("hard_reasons") and not has_safe_text:
            reasons.append("hard_hidden_text_removed")
        reasons = list(dict.fromkeys(reasons + sorted(hard_reasons | soft_reasons)))
    blank = not raw_chars and not images and not page.get_drawings()
    return {"page": page.number + 1, "route": "textlayer" if (reliable or (locals().get("has_safe_text") and not locals().get("force_ocr"))) or blank else "ocr", "reliable": reliable or blank, "blank": blank, "text": text, "filtered_text": text, "text_chars": len(text.strip()), "native_reading_order": "pdf_content_stream", "image_area_ratio": round(min(1, image_area / area), 4), "text_area_ratio": round(min(1, text_area / area), 4), "textlayer_rejected_reason": reasons[0] if reasons else None, "textlayer_rejected_reasons": reasons, "textlayer_unverified": bool(locals().get("textlayer_unverified", False)), "broken_text_layer_lines": locals().get("bad_lines", 0), "broken_text_layer_chars": locals().get("bad_chars", 0), **visibility, "probe_seconds": round(time.monotonic() - start, 4)}


def inspect_pdf(path: Path, config: dict | None = None) -> list[dict]:
    module = pymupdf()
    with module.open(path) as document:
        if document.needs_pass:
            raise ValueError("pdf_password_required")
        return [{key: value for key, value in probe_page(page, config or settings()).items() if key != "text"} for page in document]


def document_failure(backend: str, title: str, code: str, message: str) -> TranscriptResult:
    return TranscriptResult(text="", status="failed", source_level="partial_extraction", backend=backend, title=title, error_code=code, error_summary=message, source_notes=[message], metadata={"profile": "document"})


def _render_and_recognize(page, module, config, backend, title):
    try:
        from PIL import Image

        pixmap = page.get_pixmap(dpi=config["pdf_dpi"], alpha=False, colorspace=module.csRGB)
        image = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
        return LocalOCRBackend(backend).recognize_image(image, title=title, page=page.number + 1, tile=False)
    except (ImportError, OSError, ValueError, RuntimeError):
        return document_failure(backend, title, "pdf_page_render_failed", "A PDF page could not be rendered/recognized; other native pages were retained.")


def extract_pdf(item, *, backend: str = "ocr.chain", allow_ocr: bool = False, _office_origin: _OfficePDFOrigin | None = None) -> TranscriptResult:
    path = local_path_from_input(item.raw_meta.get("local_path") or item.url)
    title = item.title or (path.stem if path else "PDF document")
    if path is None or not path.is_file() or path.suffix.lower() != ".pdf":
        return document_failure(backend, title, "pdf_local_file_required", "PDF page routing requires an existing local PDF.")
    try:
        module = pymupdf()
        config = settings()
        office_generated = (isinstance(_office_origin, _OfficePDFOrigin)
                            and path.resolve() == _office_origin.path
                            and hashlib.sha256(path.read_bytes()).hexdigest() == _office_origin.sha256)
        pages, bodies, notes = [], [], []
        abort_code = None
        with module.open(path) as document:
            if document.needs_pass:
                return document_failure(backend, title, "pdf_password_required", "Encrypted PDF requires an unlocked local copy.")
            for page in document:
                if abort_code:
                    pages.append({"page": page.number + 1, "route": "ocr", "status": "partial", "engine": None, "reason": "gpu_quarantined_file_aborted", "fallback_used": False, "ovis_seconds": 0.0, "fallback_seconds": 0.0, "slice_parameters": {}, "breakers": []})
                    continue
                probe = probe_page(page, config, _office_generated=office_generated)
                text = probe.pop("text")
                native_unverified = probe["route"] == "textlayer" and probe.get("textlayer_unverified")
                if (probe["route"] == "ocr" and backend == "pdf.textlayer" and not allow_ocr
                        and probe.get("filtered_text", "").strip()):
                    # The explicitly selected native provider cannot invoke
                    # OCR. Expose its retained filtered text as an unverified
                    # native page so independent page completeness sees it.
                    probe["route"] = "textlayer"
                    probe["textlayer_unverified"] = True
                    probe["textlayer_rejected_reason"] = "textlayer_unverified"
                    native_unverified = True
                if probe["route"] == "textlayer" and not (native_unverified and allow_ocr):
                    page_status = "partial" if native_unverified or probe.get("hidden_text_removed_chars") else "verified"
                    record = {**probe, "status": page_status, "engine": "PyMuPDF", "model": None, "digest": None, "fallback_used": False, "reason": "textlayer_unverified" if native_unverified else None, "ovis_seconds": 0.0, "fallback_seconds": 0.0, "peak_vram_mb": None, "han_ratio": han_ratio(text), "seconds": probe["probe_seconds"], "slice_parameters": {}, "breakers": []}
                elif backend == "pdf.textlayer" and allow_ocr:
                    failure = document_failure(backend, title, "pdf_ocr_required", "One or more pages have no reliable native text; continue to ocr.chain.")
                    failure.metadata["pages"] = [{**probe, "status": "partial"}]
                    return failure
                elif not allow_ocr:
                    # A filtered native layer is a safe fallback when OCR was
                    # not authorized. Keep it and make the page/result
                    # partial instead of silently returning an empty page.
                    text = probe.get("filtered_text", "")
                    record = {**probe, "status": "partial", "engine": "PyMuPDF", "fallback_used": False, "reason": "ocr_permission_required", "ovis_seconds": 0.0, "fallback_seconds": 0.0, "peak_vram_mb": None, "han_ratio": han_ratio(text), "slice_parameters": {}, "breakers": []}
                    notes.append(f"Page {page.number + 1}: use --enable-network-backends to call the configured Ollama API.")
                else:
                    result = _render_and_recognize(page, module, config, backend, title)
                    text = result.text
                    record = result.metadata.get("pages", [{"page": page.number + 1, "route": "ocr", "status": "partial", "engine": backend, "fallback_used": False, "reason": result.error_code, "ovis_seconds": 0.0, "fallback_seconds": 0.0, "peak_vram_mb": None, "han_ratio": 0.0, "slice_parameters": {}, "breakers": []}])[0]
                    record.update({"dpi": config["pdf_dpi"], "probe_seconds": probe["probe_seconds"], "textlayer_rejected_reason": probe["textlayer_rejected_reason"], "textlayer_rejected_reasons": probe["textlayer_rejected_reasons"], "image_area_ratio": probe["image_area_ratio"], "invisible_text_chars": probe["invisible_text_chars"], "low_contrast_text_ratio": probe["low_contrast_text_ratio"]})
                    record.update({key: probe[key] for key in ("render_obscured_text_chars", "text_image_overlap_chars", "vector_alternative_text_chars", "render_consistency_dpi", "clip_paths", "content_clip_operator", "overlapping_paint_text_chars", "unproven_text_chars", "textlayer_origin", "low_contrast_text_chars", "office_glyph_alternative_text_chars", "hard_reasons", "soft_reasons", "hidden_text_removed_chars", "hidden_text_removed_positions", "textlayer_unverified", "broken_text_layer_lines", "broken_text_layer_chars")})
                    if result.status != "verified":
                        record["status"] = "partial"
                    notes.extend(result.source_notes)
                    if result.metadata.get("abort_file"):
                        abort_code = result.error_code or "gpu_quarantined"
                pages.append(record)
                if text.strip():
                    bodies.append(text.strip())
        if not pages:
            return document_failure(backend, title, "pdf_empty", "PDF has no pages.")
        has_ocr = any(p["route"] == "ocr" and p.get("engine") != "PyMuPDF" for p in pages)
        partial = any(p["status"] != "verified" for p in pages) or not bodies
        removed = sum(int(p.get("hidden_text_removed_chars", 0) or 0) for p in pages)
        if removed:
            notes.append(f"已移除 {removed} 个不可见字符；位置只保存在逐页回执中。")
        return TranscriptResult(text="\n\n".join(bodies), status="partial" if partial else "verified", source_level="ocr" if has_ocr else "native_text", backend=backend, title=title, error_code=abort_code or ("pdf_partial" if partial else None), source_notes=list(dict.fromkeys(["Pages retained in source order; reliable text layers use PyMuPDF, other pages use 144 DPI OCR.", "Mixed documents use source_level=ocr; per-page route records distinguish native text from OCR.", *(ocr_notes(pages) if has_ocr else []), *notes])), metadata={"profile": "document", "source_format": "pdf", "pages": pages, "page_mapping": "source_order", "mixed_routes": has_ocr and any(p["route"] == "textlayer" for p in pages), "pymupdf_version": module.VersionBind, "terminal_partial": partial, "abort_file": bool(abort_code)})
    except PDFDependencyMissing as exc:
        return document_failure(backend, title, "pymupdf_missing", str(exc))
    except (ImportError, OSError, ValueError, RuntimeError) as exc:
        return document_failure(backend, title, "pdf_page_extraction_failed", f"PDF page routing failed ({type(exc).__name__}); inspect dependencies and the local document.")
