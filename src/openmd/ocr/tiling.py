"""Tiling/merge ported from the approved local OCR selection synth run."""
from __future__ import annotations

from html.parser import HTMLParser
import hashlib
import json
import re
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path

def row_ink(img):
    """Fraction of 'ink' pixels per row (deviation from the row's median background)."""
    import numpy as np
    gray = np.asarray(img.convert("L"), dtype=np.int16)
    bg = np.median(gray, axis=1).astype(np.int16)
    return (np.abs(gray - bg[:, None]) > 40).mean(axis=1)


def choose_cut(ink, target, lo, hi, blank_thr=0.003):
    """Pick a row in [lo, hi]: center of the blank run nearest target, else min-ink row."""
    lo, hi = max(0, int(lo)), min(len(ink) - 1, int(hi))
    if hi < lo:
        return int(target), "forced"
    best, best_dist = None, None
    y = lo
    while y <= hi:
        if ink[y] < blank_thr:
            s = y
            while y + 1 <= hi and ink[y + 1] < blank_thr:
                y += 1
            c = (s + y) // 2
            dist = abs(c - target)
            if best is None or dist < best_dist:
                best, best_dist = c, dist
        y += 1
    if best is not None:
        return int(best), "blank"
    seg = ink[lo:hi + 1]
    return int(lo + int(seg.argmin())), "forced"


def plan_tiles(ink, height, tile_height=1440, overlap=0.18, search=0.15):
    ov = int(tile_height * overlap)
    tiles, start, start_q = [], 0, "top"
    while True:
        if height - start <= int(tile_height * 1.1):
            tiles.append((start, height, start_q, "bottom"))
            break
        target = start + tile_height
        end, q = choose_cut(ink, target, target - int(tile_height * search), target)
        if end <= start + ov:  # pathological: keep progress
            end, q = target, "forced"
        tiles.append((start, end, start_q, q))
        ns, start_q = choose_cut(ink, end - ov, end - int(ov * 1.3), end - int(ov * 0.7))
        start = max(ns, start + 1)
    return tiles


def split_image(path, out_dir, tile_height=1440, overlap=0.18, search=0.15, fmt="png"):
    from PIL import Image
    path, out_dir = Path(path), Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    with Image.open(path) as source:
        img = source.convert("RGB")
    w, h = img.size
    ink = row_ink(img)
    plan = plan_tiles(ink, h, tile_height, overlap, search)
    tiles = []
    prev_end = None
    for i, (y0, y1, sq, eq) in enumerate(plan, 1):
        name = f"tile-{i:03d}"
        img.crop((0, y0, w, y1)).save(out_dir / f"{name}.{fmt}")
        tiles.append({"index": i, "name": name, "file": f"{name}.{fmt}", "y0": y0, "y1": y1,
                      "height": y1 - y0, "overlap_prev": (prev_end - y0) if prev_end is not None else 0,
                      "start_cut": sq, "end_cut": eq,
                      "ink_at_start": round(float(ink[y0]), 5), "ink_at_end": round(float(ink[y1 - 1]), 5)})
        prev_end = y1
    h_sha = hashlib.sha256(path.read_bytes()).hexdigest()
    meta = {"source": path.name, "source_sha256": h_sha, "width": w, "height": h,
            "params": {"tile_height": tile_height, "overlap": overlap, "search": search, "format": fmt,
                       "blank_threshold": 0.003, "ink_delta": 40},
            "tiles": tiles}
    (out_dir / "tiles.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return meta


# ---------------------------------------------------------------------------
# merge
# ---------------------------------------------------------------------------
_SKIP = set("#*`|>_")

# This is the entire deletion-equivalence table. In particular, NFKC letter,
# digit, ligature and combining-character transformations are NOT permission
# to delete source text. Candidate search below may still use NFKC.
PUNCTUATION_EQUIVALENTS = dict(zip(
    "！＂＃＄％＆＇（）＊＋，－．／：；＜＝＞？＠［＼］＾＿｀｛｜｝～",
    "!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~",
))


def literal_view(text: str):
    """Deletion proof: one code point per non-whitespace source character."""
    chars, indexes = [], []
    for index, char in enumerate(text):
        if not char.isspace():
            chars.append(PUNCTUATION_EQUIVALENTS.get(char, char))
            indexes.append(index)
    return "".join(chars), indexes


def deletion_proof(a: str, b: str):
    """Map every deleted B character to an equal raw A character.

    The returned positions contain no recognized text and are safe for receipts.
    Whitespace is the sole unrecorded source character category.
    """
    av, ai = literal_view(a)
    bv, bi = literal_view(b)
    if av != bv:
        return None
    return {"deleted_nonspace_chars": len(bi), "authorized_nonspace_chars": len(bi),
            "deletion_mapping_coverage": 1.0,
            "mapping": [[x, y] for x, y in zip(ai, bi)]}


def norm_view(s: str):
    chars, idx = [], []
    for i, ch in enumerate(s):
        if ch.isspace() or ch in _SKIP:
            continue
        for c in unicodedata.normalize("NFKC", ch):
            if c.isspace() or c in _SKIP:
                continue
            chars.append(c)
            idx.append(i)
    return "".join(chars), idx


def _suffix_prefix_matches(tail: str, head: str, minimum: int):
    """Find exact seam candidates in linear time, including repeated borders."""
    # A unique sentinel keeps a match from crossing the head/tail boundary.
    values = list(head) + [None] + list(tail)
    borders = [0] * len(values)
    for i in range(1, len(values)):
        j = borders[i - 1]
        while j and values[i] != values[j]:
            j = borders[j - 1]
        if values[i] == values[j]:
            j += 1
        borders[i] = j
    size = borders[-1] if borders else 0
    matches = []
    while size >= minimum:
        matches.append(size)
        size = borders[size - 1]
    return matches


def _occurs_once(text: str, match: str):
    first = text.find(match)
    return first >= 0 and text.find(match, first + 1) < 0


def _authorize_candidate(a: str, b: str, size: int, bi: list[int]):
    """Candidate offsets cannot grant deletion; prove a literal suffix/prefix."""
    av, ai = literal_view(a)
    # Include ignored candidate punctuation at the boundary only when it has a
    # literal counterpart in A. A unique trailing '>' is left in B, never eaten.
    lower = bi[size - 1] + 1
    upper = bi[size] if size < len(bi) else len(b)
    proven = None
    for end in range(lower, upper + 1):
        if not a[-1:].isspace():
            end = len(b[:end].rstrip())
        bv, _ = literal_view(b[:end])
        if bv and len(bv) <= len(av) and av.endswith(bv):
            start = ai[len(av) - len(bv)]
            if len(norm_view(a[start:])[0]) == size:
                proof = deletion_proof(a[start:], b[:end])
                if proof is not None:
                    proven = start, end, proof
    return proven


def _local_candidate(a: str, b: str, wa: int, wb: int):
    """Find an anchored, monotonic alignment; it grants no deletion rights.

    Stable literal anchors bound the seam. A small unmatched edge remains a
    difference, not a discarded prefix/suffix. Large offsets stay unaligned.
    """
    av, ai = literal_view(a)
    bv, bi = literal_view(b)
    offset = max(0, len(av) - wa)
    tail, head = av[offset:], bv[:wb]
    blocks = [block for block in SequenceMatcher(None, tail, head, autojunk=False).get_matching_blocks() if block.size >= 3]
    candidates = []
    for first in blocks:
        if first.b > 2:
            continue
        start = max(0, first.a - first.b)
        for last in blocks:
            if last.a < first.a or last.b < first.b or len(tail) - last.a - last.size > 2:
                continue
            end = min(len(head), last.b + last.size + len(tail) - last.a - last.size)
            left, right = tail[start:], head[:end]
            alignment = SequenceMatcher(None, left, right, autojunk=False)
            # Two short stable edges may surround one symbol in a short
            # physical seam. Still require twelve literally equal characters
            # overall; no approximate character is ever granted deletion.
            if alignment.ratio() < 0.75 or sum(block.size for block in alignment.get_matching_blocks()) < 12:
                continue
            if min(first.size, last.size) < 12 and sum(max(j - i, y - x) for tag, i, j, x, y in alignment.get_opcodes() if tag != "equal") > 2:
                # A three-character coincidental edge cannot localize a long
                # unique suffix. Short edges only support tiny local edits.
                continue
            a_start = ai[offset + start]
            b_end = bi[end] if end < len(bi) else len(b)
            candidates.append((alignment.ratio(), a_start, b_end))
    if not candidates:
        return None
    candidates = sorted(set(candidates), reverse=True)
    best = candidates[0]
    # Identical source ranges are one candidate. Comparable alternate ranges
    # are ambiguous; do not rearrange source lines to manufacture an overlap.
    if any(abs(best[0] - other[0]) < 0.10 and best[1:] != other[1:] for other in candidates[1:]):
        return None
    return best[1], best[2]


_MARKDOWN_FENCE = re.compile(r"^(?:[ \t]*>[ \t]*)*[ \t]*(?:(?:[-+*]|\d+[.)])[ \t]+)?(`{3,}|~{3,})(.*)$")


def _fenced_blocks(text: str):
    """Return complete/truncated fence ranges and any unclosed opener."""
    ranges, opened, offset = [], None, 0
    for line in text.splitlines(keepends=True):
        match = _MARKDOWN_FENCE.match(line.rstrip("\r\n"))
        if match:
            marker, tail = match.groups()
            if opened is None:
                opened = (offset, marker[0], len(marker))
            elif marker[0] == opened[1] and len(marker) >= opened[2] and not tail.strip():
                ranges.append((opened[0], offset + len(line)))
                opened = None
        offset += len(line)
    if opened is not None:
        ranges.append((opened[0], len(text)))
    return ranges, opened


def _html_blocks(text: str):
    """Conservative HTML container ranges, independent of blank-line syntax.

    Only balanced known elements/comments are rendered as HTML. Unknown,
    mismatched or truncated input is displayed literally as one atomic source.
    Fenced Markdown is already literal and must not be parsed as HTML.
    """
    offsets = [0]
    for line in text.splitlines(keepends=True):
        offsets.append(offsets[-1] + len(line))
    fences, _ = _fenced_blocks(text)
    paired = set("a abbr address article aside audio b bdi bdo blockquote body button caption cite code colgroup data datalist dd del details dfn dialog div dl dt em fieldset figcaption figure footer form h1 h2 h3 h4 h5 h6 head header hgroup html i iframe ins kbd label legend li main map mark menu meter nav noscript object ol optgroup option output p picture pre progress q rp rt ruby s samp script search section select small span strong style sub summary sup table tbody td template textarea tfoot th thead time title tr u ul var video".split())
    void = set("area base br col embed hr img input link meta param source track wbr".split())

    class Containers(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=False)
            self.stack, self.ranges, self.reasons = [], [], set()

        def position(self):
            row, col = self.getpos()
            return offsets[row - 1] + col

        def skipped(self, start):
            return any(lo <= start < hi for lo, hi in fences)

        def handle_starttag(self, tag, attrs):
            start = self.position()
            if self.skipped(start):
                return
            end = start + len(self.get_starttag_text())
            if tag not in paired | void:
                self.reasons.add("unknown_html_container")
            if tag not in void:
                self.stack.append((tag, start))
            else:
                self.ranges.append((start, end))

        def handle_startendtag(self, tag, attrs):
            start = self.position()
            if self.skipped(start):
                return
            self.ranges.append((start, start + len(self.get_starttag_text())))
            # HTML self-closing syntax only closes void elements. Guessing XML
            # semantics for <table/> could put the labels inside a real table.
            if tag not in void:
                self.reasons.add("unknown_html_container" if tag not in paired else "unclosed_html_container")

        def handle_endtag(self, tag):
            start = self.position()
            if self.skipped(start):
                return
            if not self.stack or self.stack[-1][0] != tag:
                self.reasons.add("mismatched_html_container")
                return
            _, lo = self.stack.pop()
            self.ranges.append((lo, text.find(">", start) + 1))

        def handle_comment(self, data):
            start = self.position()
            if not self.skipped(start):
                end = text.find("-->", start)
                if end < 0:
                    self.reasons.add("unclosed_html_container")
                else:
                    self.ranges.append((start, end + 3))

        def handle_data(self, data):
            start = self.position()
            if not self.skipped(start) and (re.match(r"<(?:[A-Za-z!/?]|$)", text[start:]) or "-->" in data):
                self.reasons.add("malformed_html_container")

        def handle_decl(self, decl):
            if not self.skipped(self.position()):
                self.reasons.add("unknown_html_container")

        def unknown_decl(self, data):
            self.handle_decl(data)

        def handle_pi(self, data):
            self.handle_decl(data)

    parser = Containers()
    try:
        # Mask literal fences before parsing: an unclosed '<!--' inside code
        # must not swallow a real HTML container after the closing fence.
        masked = list(text)
        for lo, hi in fences:
            for index in range(lo, hi):
                if masked[index] not in "\r\n":
                    masked[index] = " "
        masked_text = "".join(masked)
        parser.feed(masked_text)
        parser.close()
        # HTMLParser.close() reports a truncated "<tag ..." as data on some
        # Python releases (3.12.2) and silently drops it on others (3.10,
        # 3.14). Check explicitly so the verdict does not depend on Python.
        if re.search(r"<[A-Za-z!/?][^>]*\Z", masked_text):
            parser.reasons.add("malformed_html_container")
    except (ValueError, AssertionError):
        parser.reasons.add("malformed_html_container")
    if parser.stack:
        parser.reasons.add("unclosed_html_container")
    if parser.reasons:
        return [(0, len(text))], sorted(parser.reasons)
    return parser.ranges, []


def _literal_reasons(raw):
    return (["unclosed_source_fence"] if _fenced_blocks(raw)[1] is not None else []) + _html_blocks(raw)[1]


def _markdown_block_range(text: str, start: int, end: int):
    """Expand a presentation range within the *complete* source Markdown.

    This deliberately overgroups adjacent nonblank lines: a table, wrapped
    paragraph, inline expression or lazy list continuation must never be split
    by a seam marker. Fences, indented code and lists can also span blank lines.
    These ranges grant no authority to delete or normalize source characters.
    """
    lines = text.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    ranges = []

    # Treat any uninterrupted run as atomic. This also covers tables without
    # leading/trailing pipes, multiline inline markup and unknown block syntax.
    first = None
    for i, line in enumerate(lines):
        if line.strip():
            if first is None:
                first = i
        elif first is not None:
            ranges.append((offsets[first], offsets[i]))
            first = None
    if first is not None:
        ranges.append((offsets[first], len(text)))

    list_item = re.compile(r"^[ ]{0,3}(?:[-+*]|\d+[.)])[ \t]+")
    fence_ranges, _ = _fenced_blocks(text)
    ranges.extend(fence_ranges)
    ranges.extend(_html_blocks(text)[0])

    i = 0
    while i < len(lines):
        line = lines[i]
        is_list = bool(list_item.match(line))
        is_code = line.startswith(("    ", "\t")) and bool(line.strip())
        if not (is_list or is_code):
            i += 1
            continue
        j = i + 1
        while j < len(lines):
            if not lines[j].strip():
                k = j + 1
                while k < len(lines) and not lines[k].strip():
                    k += 1
                if k == len(lines) or not (lines[k].startswith((" ", "\t")) or (is_list and list_item.match(lines[k]))):
                    break
                j = k
            elif is_list or lines[j].startswith(("    ", "\t")):
                # Unindented text before a blank is a possible lazy list
                # continuation. Keeping the whole list is conservative.
                j += 1
            else:
                break
        ranges.append((offsets[i], offsets[j]))
        i = max(i + 1, j)

    # Earlier seams can themselves contain blank lines and complete code blocks.
    # Keep their wrappers atomic when merging a third or subsequent tile.
    seam_start, depth = None, 0
    for i, line in enumerate(lines):
        if line.strip() == "<!-- openmd:seam-diff start -->":
            if not depth:
                seam_start = i
            depth += 1
        elif line.strip() == "<!-- openmd:seam-diff end -->" and depth:
            depth -= 1
            if not depth:
                ranges.append((offsets[seam_start], offsets[i + 1]))
    if depth:
        ranges.append((offsets[seam_start], len(text)))

    start, end = max(0, start), min(len(text), end)
    # Union overlapping structural ranges before expanding. A fence inside a
    # list, for example, must keep both the fence and the enclosing item intact.
    blocks = []
    for lo, hi in sorted(ranges):
        if blocks and lo < blocks[-1][1]:
            blocks[-1] = (blocks[-1][0], max(hi, blocks[-1][1]))
        else:
            blocks.append((lo, hi))
    for lo, hi in blocks:
        if lo < end and hi > start:
            start, end = min(start, lo), max(end, hi)
    return start, end


def _difference_block(raw_a: str, raw_b: str):
    # Blank lines separate labels from source tables/lists and separate the
    # wrapper from surrounding paragraphs. Both raw readings stay contiguous.
    def reading(raw):
        reasons = _literal_reasons(raw)
        if not reasons:
            return raw
        # A truncated source fence cannot safely contain our labels. Present
        # its *unchanged* raw Markdown literally inside a longer outer fence,
        # and disclose this rendering choice instead of inventing a source
        # closing fence. The real code points still form one contiguous copy.
        fence = "`" * max(3, 1 + max((len(run) for run in re.findall(r"`+", raw)), default=0))
        label = "unclosed source fence" if reasons == ["unclosed_source_fence"] else "unsafe source container"
        return "[openmd: " + label + "; literal Markdown]\n\n" + fence + "markdown\n" + raw + "\n" + fence

    return "\n\n<!-- openmd:seam-diff start -->\n\n[openmd variant A]\n\n" + reading(raw_a) + "\n\n[openmd variant B]\n\n" + reading(raw_b) + "\n\n<!-- openmd:seam-diff end -->\n\n"


def _localized_difference(a: str, b: str, a_start: int, b_end: int):
    # A literal alignment can locate a conflict, but even an equal character
    # prefix may belong to a number, table cell or code expression. Preserve
    # both full affected blocks rather than splice their shared characters.
    display_a, _ = _markdown_block_range(a, a_start, len(a))
    _, display_b = _markdown_block_range(b, 0, b_end)
    return a[:display_a] + _difference_block(a[display_a:], b[:display_b]) + b[display_b:], {
        "difference_ranges": [{"a_range": [display_a, len(a)], "b_range": [0, display_b]}],
        "difference_count": 1, "difference_boundary": "markdown_block",
        "difference_literal_markdown": [label for label, raw in (("A", a[display_a:]), ("B", b[:display_b])) if _literal_reasons(raw)],
        "difference_container_policy": "markdown_and_balanced_html_or_literal",
        "difference_literal_reasons": {label: _literal_reasons(raw) for label, raw in (("A", a[display_a:]), ("B", b[:display_b])) if _literal_reasons(raw)},
        "deleted_nonspace_chars": 0, "authorized_nonspace_chars": 0,
        "deletion_mapping_coverage": 1.0, "mapping": [],
        "alignment": "monotonic_literal", "a_overlap_range": [a_start, len(a)],
        "b_overlap_range": [0, b_end],
    }


def merge_pair(a: str, b: str, prev_len_norm: int, overlap=0.18, min_match=6, min_window=120,
               *, geometry: dict | None = None, arbitrator: str | None = None):
    """Candidate matching is permissive; deletion always needs raw proof.

    A conflict retains its two raw readings in complete Markdown blocks. Only
    an entirely proven overlap is deduplicated. Unaligned inputs remain intact,
    so neither geometry estimates nor an arbiter can discard symbols.
    """
    an, ai = norm_view(a)
    bn, bi = norm_view(b)
    frac = min(0.9, overlap * 2.5)
    wa = min(len(an), max(min_window, int(prev_len_norm * frac)))
    wb = min(len(bn), max(min_window, int(len(bn) * frac)))
    if geometry:
        wa = min(wa, max(1, geometry.get("a_window_chars", wa)))
        wb = min(wb, max(1, geometry.get("b_window_chars", wb)))
    tail, head = an[len(an) - wa:], bn[:wb]
    candidates = _suffix_prefix_matches(tail, head, max(1, min_match))
    size = candidates[0] if candidates else 0
    seam = {"window_prev": wa, "window_next": wb, "match_len": size,
            "candidate_count": len(candidates), "match_next_start": 0 if size else None,
            "dropped_prev_tail_chars": 0, "dropped_next_head_chars": 0,
            "deleted_nonspace_chars": 0, "authorized_nonspace_chars": 0,
            "deletion_mapping_coverage": 1.0}
    if geometry:
        seam["geometry"] = geometry
    if not candidates:
        reason = "no_anchored_overlap"
    elif len(candidates) != 1 or not _occurs_once(tail, head[:size]) or not _occurs_once(head, head[:size]):
        reason = "ambiguous_overlap"
    elif size < max(12, min_match):
        reason = "weak_overlap"
    elif size < len(bi) and bi[size - 1] == bi[size]:
        # NFKC can expand one source glyph into several characters. Never cut
        # that glyph when only part of its normalized value was duplicated.
        reason = "normalization_boundary"
    else:
        authorization = _authorize_candidate(a, b, size, bi)
        if authorization:
            a_start, b_end, proof = authorization
            proof["mapping"] = [[x + a_start, y] for x, y in proof["mapping"]]
            seam.update(proof)
            seam.update({"status": "merged", "dropped_next_head_chars": proof["deleted_nonspace_chars"],
                         "a_overlap_range": [a_start, len(a)], "b_overlap_range": [0, b_end]})
            return a + b[b_end:], seam
        reason = "raw_character_mismatch"
    seam.update({"status": "merge_uncertain", "reason": reason})
    # Ambiguous and weak candidates cannot be made trustworthy by fuzzy search.
    local = None
    if reason == "raw_character_mismatch":
        # The unique normalized candidate is already seam-anchored. Its raw
        # differences (including Markdown-looking symbols) can be localized
        # without guessing a later internal match or authorizing their loss.
        first = len(an) - size
        a_start = ai[first - 1] + 1 if first else 0
        b_end = bi[size] if size < len(bi) else len(b)
        local = a_start, b_end
    elif reason not in {"ambiguous_overlap", "weak_overlap", "normalization_boundary"}:
        local = _local_candidate(a, b, wa, wb)
    if local:
        a_start, b_end = local
        if not a[-1:].isspace():
            b_end = len(b[:b_end].rstrip())
        merged, proof = _localized_difference(a, b, a_start, b_end)
        seam.update(proof)
        seam["dropped_next_head_chars"] = proof["deleted_nonspace_chars"]
        seam["difference_format"] = "openmd:seam-diff"
        if arbitrator is not None:
            # Agreement identifies a preferred reading, never permission to
            # remove a differing B character. Preserve the conflict in output.
            agreement = []
            if deletion_proof(a[a_start:], arbitrator):
                agreement.append("A")
            if deletion_proof(b[:b_end], arbitrator):
                agreement.append("B")
            seam["arbitration_agreement"] = agreement
            seam["arbitration_result"] = "arbitration_not_deletion_authority" if agreement else "inconsistent"
        return merged, seam
    if geometry and geometry.get("overlap_bbox"):
        # We cannot prove a unique correspondence inside this physical band.
        # Localize the uncertainty without deleting ANY source character. This
        # is a presentation boundary only, never a guessed deduplication.
        av, ax = literal_view(a)
        bv, bx = literal_view(b)
        a_start = ax[max(0, len(av) - wa)] if av and wa else len(a)
        b_end = bx[wb] if wb < len(bx) else len(b)
        left, right = a[a_start:], b[:b_end]
        merged, proof = _localized_difference(a, b, a_start, b_end)
        seam.update(proof)
        seam.update({"difference_format": "openmd:seam-diff", "alignment": "unresolved_overlap_band"})
        if arbitrator is not None:
            agreement = [label for label, reading in (("A", left), ("B", right)) if deletion_proof(reading, arbitrator)]
            seam["arbitration_agreement"] = agreement
            seam["arbitration_result"] = "arbitration_not_deletion_authority" if agreement else "inconsistent"
        return merged, seam
    # No physical band or reliable candidate: preserve both *whole* inputs in
    # the same safe presentation format. Plain concatenation can let an
    # unclosed fence in A swallow B or silently combine unrelated structures.
    merged, proof = _localized_difference(a, b, 0, len(b))
    seam.update(proof)
    seam.update({"difference_format": "openmd:seam-diff", "alignment": "unresolved_whole_inputs"})
    if arbitrator is not None:
        agreement = [label for label, reading in (("A", a), ("B", b)) if deletion_proof(reading, arbitrator)]
        seam["arbitration_agreement"] = agreement
        seam["arbitration_result"] = "arbitration_not_deletion_authority" if agreement else "inconsistent"
    return merged, seam


def repeated_ngrams(text: str, n=24):
    s, _ = norm_view(text)
    seen, reps = {}, []
    i = 0
    while i + n <= len(s):
        g = s[i:i + n]
        if g in seen and i - seen[g] >= n:
            reps.append([seen[g], i])
            i += n
            continue
        seen.setdefault(g, i)
        i += 1
    return reps
