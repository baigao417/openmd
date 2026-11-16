from openmd.ocr.tiling import merge_pair, norm_view, repeated_ngrams

def merge_texts(texts, overlap=0.18, min_match=6):
    if not texts:
        return "", {"seams": [], "warnings": ["no input"]}
    merged = texts[0]
    prev_len = len(norm_view(texts[0])[0])
    seams = []
    for k, t in enumerate(texts[1:], 2):
        merged, seam = merge_pair(merged, t, prev_len, overlap, min_match)
        seam["seam"] = f"{k - 1}->{k}"
        seams.append(seam)
        prev_len = len(norm_view(t)[0])
    reps = repeated_ngrams(merged)
    warnings = [f"seam {s['seam']}: {s['status']} (possible missing or duplicated lines)"
                for s in seams if s["status"] != "merged"]
    if reps:
        warnings.append(f"{len(reps)} repeated 24-char spans in merged text (possible duplicate overlap)")
    return merged, {"tiles": len(texts), "seams": seams, "repeated_spans": reps[:20], "warnings": warnings,
                    "merged_norm_chars": len(norm_view(merged)[0])}
