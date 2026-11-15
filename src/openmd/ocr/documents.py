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


class PDFDependencyMissing(RuntimeError):
    pass


def pymupdf():
    if importlib.util.find_spec("pymupdf") is None:
        raise PDFDependencyMissing('PyMuPDF missing. Install explicitly: python -m pip install "openmd[ocr-router]" (AGPL; assess licensing before distribution).')
    import pymupdf as module

    return module


def text_is_reliable(text: str, config: dict) -> bool:
    chars = [c for c in text if not c.isspace()]
    if len(chars) < config["textlayer_min_chars"]:
        return False
    good = sum(c.isprintable() and c not in {"\ufffd", "\x00"} for c in chars)
    return good / len(chars) >= config["textlayer_min_printable"] and sum(c.isalnum() for c in chars) / len(chars) >= 0.4


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


def _rendered_ink(pixmap, page, box, delta):
    pixels = _pixels(pixmap, page, box)
    if not pixels:
        return []
    # The dominant colour *inside* a glyph box is its background. Sampling only
    # outside the span mistakes a later coloured cover for the original glyph.
    background = tuple(statistics.median(p[2][channel] for p in pixels) for channel in range(3))
    return [(x, y) for x, y, rgb in pixels if max(abs(a - b) for a, b in zip(rgb, background)) > delta * 255]


def _intersection_area(a, b):
    return max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(0, min(a[3], b[3]) - max(a[1], b[1]))


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


def _visibility(page, config: dict, images: list, *, office_generated: bool = False) -> dict:
    """Reject whole-page visibility uncertainty; pixels alone never waive clips."""
    spans = page.get_texttrace()
    paint_groups = _text_paint_groups(page, spans) if not office_generated else {}
    drawings = page.get_drawings(extended=True)
    paint = page.get_bboxlog()
    total = invisible = low_contrast = obscured = vector_alternatives = glyph_alternatives = image_overlap = unproven = paint_overlap = 0
    # Clip paths may be arbitrary, nested, text-derived, or in a Form XObject.
    # Do not approximate the clipped glyph identity from its surviving pixels.
    clips = sum(drawing.get("type") == "clip" for drawing in drawings)
    content_clip = not office_generated and _content_has_clip(page)
    overlays = [entry.rect for entry in (page.annots() or ())]
    overlays.extend(entry.rect for entry in (page.widgets() or ()))
    pixmap = page.get_pixmap(dpi=72, alpha=False, colorspace=pymupdf().csRGB)
    for span in spans:
        chars = [char for char in span.get("chars", ()) if char[0] > 32 and not chr(char[0]).isspace()]
        count = len(chars)
        total += count
        if not count:
            continue
        box = span["bbox"]
        # MuPDF exposes a converter ligature's extra Unicode characters with
        # glyph id -1 and zero advance at the painted glyph's same origin.
        # Like outlined alternatives, this is only trusted for our conversion.
        cluster_alternatives = [char for char in chars if office_generated and char[1] == -1
                                and any(other[1] >= 0 and other[2] == char[2]
                                        and _intersection_area(other[3], page.rect) > 0
                                        for other in chars)]
        glyph_alternatives += len(cluster_alternatives)
        if span.get("layer") or span.get("type") not in {0, 1, 3}:
            unproven += count
        for char in chars:
            charbox = char[3]
            chararea = (charbox[2] - charbox[0]) * (charbox[3] - charbox[1])
            if char not in cluster_alternatives and (chararea <= 0 or _intersection_area(charbox, page.rect) < chararea - 0.001):
                unproven += 1
            if any(_intersection_area(charbox, image["bbox"]) > 0 for image in images if image.get("bbox")):
                image_overlap += 1
        # Every later overlapping paint can hide or alter a character.
        # This includes strokes, transparent fills, shades and images; do not
        # guess cover identity from shape, colour, or percentage of bbox area.
        later_paint = any(kind not in {"ignore-text", "fill-text", "stroke-text"}
                          and _intersection_area(box, bounds) > 0
                          for kind, bounds in paint[span.get("seqno", -1) + 1:])
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
                for later in spans)
            later_paint = later_paint or _paint_self_overlap(chars)
        if later_paint:
            paint_overlap += count
        if any(_intersection_area(box, rect) > 0 for rect in overlays):
            unproven += count
        ink = _rendered_ink(pixmap, page, box, config["textlayer_background_delta"])
        if office_generated and not later_paint and _office_vector_alternative(span, drawings, ink, page, pixmap):
            vector_alternatives += count
            continue
        if span.get("type") == 3 or span.get("opacity", 1) < 0.99:
            invisible += count
            continue
        if not ink or any(not _rendered_ink(pixmap, page, char[3], config["textlayer_background_delta"]) for char in chars if char not in cluster_alternatives):
            obscured += count
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
    return {"invisible_text_chars": invisible, "low_contrast_text_ratio": round(low_contrast / max(total, 1), 4), "low_contrast_text_chars": low_contrast,
            "render_obscured_text_chars": obscured, "text_image_overlap_chars": image_overlap,
            "vector_alternative_text_chars": vector_alternatives, "render_consistency_dpi": 72,
            "office_glyph_alternative_text_chars": glyph_alternatives,
            "clip_paths": clips, "overlapping_paint_text_chars": paint_overlap,
            "content_clip_operator": content_clip,
            "unproven_text_chars": unproven, "traced_text_chars": total,
            "textlayer_origin": "openmd_office_conversion" if office_generated else "user_pdf"}


def probe_page(page, config: dict, *, _office_generated: bool = False) -> dict:
    start = time.monotonic()
    # Preserve the PDF's native content stream. Forced y/x sorting interleaves
    # adjacent columns and changes source order on native multicolumn pages.
    text = page.get_text("text", sort=False)
    reliable = text_is_reliable(text, config)
    images = page.get_image_info()
    area = max(1, page.rect.width * page.rect.height)
    image_area = _covered_area([item["bbox"] for item in images if item.get("bbox")], page.rect)
    words = page.get_text("words")
    text_area = sum(max(0, w[2] - w[0]) * max(0, w[3] - w[1]) for w in words)
    reasons = []
    visibility = {"invisible_text_chars": 0, "low_contrast_text_ratio": 0.0, "low_contrast_text_chars": 0, "render_obscured_text_chars": 0, "text_image_overlap_chars": 0, "vector_alternative_text_chars": 0, "render_consistency_dpi": 72,
                  "clip_paths": 0, "content_clip_operator": False, "overlapping_paint_text_chars": 0, "unproven_text_chars": 0,
                  "traced_text_chars": 0, "office_glyph_alternative_text_chars": 0, "textlayer_origin": "openmd_office_conversion" if _office_generated else "user_pdf"}
    if text.strip():
        # Large scans may carry plausible but contradictory OCR text layers.
        # Their visible pixels are authoritative regardless of text density.
        if image_area / area >= config["textlayer_image_coverage"]:
            reasons.append("image_coverage")
        try:
            visibility = _visibility(page, config, images, office_generated=_office_generated)
        except (ValueError, RuntimeError, OverflowError):
            # A visibility parser failure is not permission to return its text.
            # Keep the page eligible for visual OCR, including mixed documents.
            reasons.append("visibility_probe_failed")
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
        if not reliable:
            reasons.append("text_quality")
        reliable = reliable and not reasons
    blank = not text.strip() and not images and not page.get_drawings()
    return {"page": page.number + 1, "route": "textlayer" if reliable or blank else "ocr", "reliable": reliable or blank, "blank": blank, "text": text, "text_chars": len(text.strip()), "native_reading_order": "pdf_content_stream", "image_area_ratio": round(min(1, image_area / area), 4), "text_area_ratio": round(min(1, text_area / area), 4), "textlayer_rejected_reason": reasons[0] if reasons else None, "textlayer_rejected_reasons": reasons, **visibility, "probe_seconds": round(time.monotonic() - start, 4)}


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
                if probe["route"] == "textlayer":
                    record = {**probe, "status": "verified", "engine": "PyMuPDF", "model": None, "digest": None, "fallback_used": False, "reason": None, "ovis_seconds": 0.0, "fallback_seconds": 0.0, "peak_vram_mb": None, "han_ratio": han_ratio(text), "seconds": probe["probe_seconds"], "slice_parameters": {}, "breakers": []}
                elif backend == "pdf.textlayer":
                    failure = document_failure(backend, title, "pdf_ocr_required", "One or more pages have no reliable native text; continue to ocr.chain.")
                    failure.metadata["pages"] = [{**probe, "status": "partial"}]
                    return failure
                elif not allow_ocr:
                    text = ""
                    record = {**probe, "status": "partial", "engine": backend, "fallback_used": False, "reason": "ocr_permission_required", "ovis_seconds": 0.0, "fallback_seconds": 0.0, "peak_vram_mb": None, "han_ratio": 0.0, "slice_parameters": {}, "breakers": []}
                    notes.append(f"Page {page.number + 1}: use --enable-network-backends to call the configured Ollama API.")
                else:
                    result = _render_and_recognize(page, module, config, backend, title)
                    text = result.text
                    record = result.metadata.get("pages", [{"page": page.number + 1, "route": "ocr", "status": "partial", "engine": backend, "fallback_used": False, "reason": result.error_code, "ovis_seconds": 0.0, "fallback_seconds": 0.0, "peak_vram_mb": None, "han_ratio": 0.0, "slice_parameters": {}, "breakers": []}])[0]
                    record.update({"dpi": config["pdf_dpi"], "probe_seconds": probe["probe_seconds"], "textlayer_rejected_reason": probe["textlayer_rejected_reason"], "textlayer_rejected_reasons": probe["textlayer_rejected_reasons"], "image_area_ratio": probe["image_area_ratio"], "invisible_text_chars": probe["invisible_text_chars"], "low_contrast_text_ratio": probe["low_contrast_text_ratio"]})
                    record.update({key: probe[key] for key in ("render_obscured_text_chars", "text_image_overlap_chars", "vector_alternative_text_chars", "render_consistency_dpi", "clip_paths", "content_clip_operator", "overlapping_paint_text_chars", "unproven_text_chars", "textlayer_origin", "low_contrast_text_chars", "office_glyph_alternative_text_chars")})
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
        has_ocr = any(p["route"] == "ocr" for p in pages)
        partial = any(p["status"] != "verified" for p in pages) or not bodies
        return TranscriptResult(text="\n\n".join(bodies), status="partial" if partial else "verified", source_level="ocr" if has_ocr else "native_text", backend=backend, title=title, error_code=abort_code or ("pdf_partial" if partial else None), source_notes=list(dict.fromkeys(["Pages retained in source order; reliable text layers use PyMuPDF, other pages use 144 DPI OCR.", "Mixed documents use source_level=ocr; per-page route records distinguish native text from OCR.", *(ocr_notes(pages) if has_ocr else []), *notes])), metadata={"profile": "document", "source_format": "pdf", "pages": pages, "page_mapping": "source_order", "mixed_routes": has_ocr and any(p["route"] == "textlayer" for p in pages), "pymupdf_version": module.VersionBind, "terminal_partial": partial, "abort_file": bool(abort_code)})
    except PDFDependencyMissing as exc:
        return document_failure(backend, title, "pymupdf_missing", str(exc))
    except (ImportError, OSError, ValueError, RuntimeError) as exc:
        return document_failure(backend, title, "pdf_page_extraction_failed", f"PDF page routing failed ({type(exc).__name__}); inspect dependencies and the local document.")
