"""Image-only capacity and repetition checks, ported from the approved T2 run.

No reference answer is read here. These checks detect loops and excess length,
not isolated hallucinations, omissions or numeric errors.
"""

from __future__ import annotations

import re
import html


def normalize_for_breaker(text: str) -> str:
    # HTML table syntax/image placeholders are not visible characters. Counting
    # tags against a pixel text budget falsely trips layout-crop fallback pages.
    text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"\s+", " ", text)
    return re.sub(r"[#*_`|>$-]", "", text).strip()


def formula_like(text: str) -> bool:
    raw = text.strip()
    if not raw:
        return True
    # Formula-only fragments are exempt; prose containing a formula is not.
    prose = re.sub(r"\$\$.*?\$\$|\$[^$]*\$|\\\[.*?\\\]|\\\(.*?\\\)", "", raw, flags=re.S)
    if len(re.findall(r"[\u3400-\u9fffA-Za-z]", prose)) >= 15 and prose != raw:
        return False
    if re.search(r"\\(?:frac|sum|int|sqrt|left|right|begin|end|mathbf|mathrm|cdot|times)|\$|\\\[|\\\(", raw):
        return True
    han = len(re.findall(r"[\u3400-\u9fff]", raw))
    letters = len(re.findall(r"[A-Za-z]", raw))
    operators = len(re.findall(r"[=+*/^_{}<>∑∫√×÷±]|\.\.\.|…", raw))
    digits = len(re.findall(r"\d", raw))
    if han == 0 and (operators >= 1 or digits >= 2) and len(raw) <= 160 and letters <= max(8, 2 * (operators + digits)):
        return True
    return han <= 2 and operators >= 2 and (operators + digits) >= max(3, letters // 2)


def repetition_trip(text: str, config: dict) -> dict | None:
    # Remove fenced/math-only blocks before splitting sentences.
    clean = re.sub(r"```.*?```|\$\$.*?\$\$|\\\[.*?\\\]", " ", text, flags=re.S)
    counts: dict[str, int] = {}
    for piece in re.split(r"[。！？!?\n]|(?<=[a-z0-9])\.\s+", clean):
        norm = normalize_for_breaker(piece)
        if len(norm) < config["repeat_min_chars"] or formula_like(piece):
            continue
        counts[norm] = counts.get(norm, 0) + 1
        if counts[norm] >= config["repeat_count"]:
            # No recognized text enters a receipt (especially for private L3).
            return {"rule": "repeat_sentence", "count": counts[norm], "chars": len(norm)}
    return None


def estimate_expected_chars(image, config: dict) -> dict:
    import numpy as np
    from PIL import Image, ImageOps

    probe = image.copy()
    probe.thumbnail((1400, 1400), Image.Resampling.LANCZOS)
    dark = np.asarray(ImageOps.grayscale(probe), dtype=np.uint8) < 220
    active = dark.mean(axis=1) > 0.012
    bands, in_band = 0, False
    for value in active:
        if value and not in_band:
            bands += 1
        in_band = bool(value)
    bands = bands or max(1, round(probe.height / 80))
    width_chars = max(24, min(180, round(probe.width / 13)))
    density = float(dark.mean())
    density_factor = max(0.65, min(1.8, density / 0.055))
    estimated = int(max(256, min(120000, bands * width_chars * density_factor * 1.75)))
    return {"estimated_chars": estimated, "ink_rows": bands, "probe_size": list(probe.size), "ink_fraction": round(density, 5), "max_length_chars": int(estimated * config["length_multiplier"])}


def breaker_reason(text: str, budget: dict, config: dict) -> dict | None:
    repeated = repetition_trip(text, config)
    if repeated:
        return repeated
    length = len(normalize_for_breaker(text))
    if length > budget["max_length_chars"]:
        return {"rule": "length_gt_image_estimate", "observed_chars": length, "estimated_chars": budget["estimated_chars"], "multiplier": config["length_multiplier"]}
    return None
