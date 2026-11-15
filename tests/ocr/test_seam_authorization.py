"""Literal deletion proofs and image-band arbitration, with no model loads."""
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from PIL import Image

from openmd.ocr.config import settings
from openmd.ocr.pipeline import LocalOCRBackend, seam_geometry
from openmd.ocr.tiling import deletion_proof, literal_view, merge_pair, merge_texts


@pytest.mark.parametrize("symbol", [">", "_", "*", "`", "|", "#"])
def test_normalization_is_never_authority_to_delete_a_literal_symbol(symbol):
    a = "First record: `limitvalue1234567890`"
    b = "`limitvalue" + symbol + "1234567890`\nFinal record is retained."
    merged, report = merge_texts([a, b])
    assert report["seams"][0]["status"] == "merge_uncertain"
    assert "[openmd variant B]\n\n`limitvalue" + symbol + "1234567890`" in merged
    assert "Final record is retained." in merged
    assert "openmd:seam-diff" in merged
    assert report["warnings"]


@pytest.mark.parametrize("left,right", [
    ('<a data-x="1234567890123456">same</a>', '<a data_x="1234567890123456">same</a>'),
    (r"$$sum_{i=1}^{1234567890} x_i$$", r"$$sum_{i=1}^{1234567890} x*i$$"),
    ("preceding anchor 1234567890123456 e\u0301 ending anchor 09876543210", "preceding anchor 1234567890123456 é ending anchor 09876543210"),
    ("preceding anchor 1234567890123456 ffi ending anchor 09876543210", "preceding anchor 1234567890123456 ﬃ ending anchor 09876543210"),
    ("preceding anchor 1234567890123456 A ending anchor 09876543210", "preceding anchor 1234567890123456 Ａ ending anchor 09876543210"),
])
def test_html_formula_and_unicode_differences_remain_partial(left, right):
    merged, seam = merge_pair("prefix\n" + left, right + "\nbody after seam", len(left))
    assert seam["status"] == "merge_uncertain"
    assert seam["deletion_mapping_coverage"] == 1
    assert seam["authorized_nonspace_chars"] == seam["deleted_nonspace_chars"]
    assert "body after seam" in merged
    assert deletion_proof(left, right) is None


@pytest.mark.parametrize("right", [
    "shared seam, with (punctuation)! 1234567890\n",
    "shared\n seam,\twith (punctuation)! 1234567890\n",
    "shared seam， with （punctuation）！ 1234567890\n",
])
def test_whitespace_and_explicit_fullwidth_punctuation_are_fully_authorized(right):
    left = "shared seam, with (punctuation)! 1234567890\n"
    merged, seam = merge_pair("before\n" + left, right + "after\n", len(left))
    assert merged == "before\n" + left + "after\n"
    assert seam["status"] == "merged"
    assert seam["deletion_mapping_coverage"] == 1
    assert seam["deleted_nonspace_chars"] == len(literal_view(right)[0])
    assert len(seam["mapping"]) == seam["deleted_nonspace_chars"]


def test_local_difference_retains_complete_readings_instead_of_inline_fragments():
    left_anchor, right_anchor = "stable left anchor 1234567890", "stable right anchor 0987654321"
    a = "unique prior body\n" + left_anchor + " value=111 " + right_anchor
    b = left_anchor + " value=112 " + right_anchor + "\nunique later body"
    merged, seam = merge_pair(a, b, len(a), min_window=500)
    assert seam["status"] == "merge_uncertain"
    assert merged.count(left_anchor) == 2
    assert merged.count(right_anchor) == 2
    assert a in merged
    assert b in merged
    assert "value=111" in merged and "value=112" in merged
    assert seam["difference_count"] == 1
    assert len(seam["mapping"]) == seam["deleted_nonspace_chars"]


def test_arbiter_agreement_is_not_permission_to_delete_an_alternative_symbol():
    a = "prefix\nshared long anchor 1234567890 value>2 end anchor 0987654321"
    b = "shared long anchor 1234567890 value_2 end anchor 0987654321\nnext"
    arbiter = a.split("\n", 1)[1]
    merged, seam = merge_pair(a, b, len(a), min_window=500, arbitrator=arbiter)
    assert seam["status"] == "merge_uncertain"
    assert ">" in merged and "_" in merged
    assert seam["arbitration_agreement"] == ["A"]
    assert seam["arbitration_result"] == "arbitration_not_deletion_authority"


def test_mutation_set_never_loses_unique_character_or_silently_verifies():
    anchor = "leftanchor0123456789 middleABCDEFGHIJ rightanchor9876543210"
    # A symbol after the complete overlap is new body, not a conflicting seam.
    for position in range(len(anchor)):
        b = anchor[:position] + "Ω" + anchor[position:] + "\nunique after"
        a = "unique before\n" + anchor
        merged, seam = merge_pair(a, b, len(a), min_window=500)
        assert "Ω" in merged, position
        assert seam["status"] == "merge_uncertain", position
        assert seam["deletion_mapping_coverage"] == 1
        assert len(seam.get("mapping", [])) == seam["deleted_nonspace_chars"]
        for ai, bi in seam.get("mapping", []):
            assert literal_view(a[ai])[0] == literal_view(b[bi])[0]


def test_retained_alignment_inputs_replay_mapping_after_prior_tiles(tmp_path, monkeypatch):
    import json
    from openmd.ocr.pipeline import retain_seam_outputs
    monkeypatch.setattr("openmd.ocr.pipeline.checkout_root", lambda: tmp_path)
    previous = "prior tile body\nseam anchor1234567890"
    accumulated = "first tile data\n" + previous
    current = "seam anchor1234567890\nafter"
    _, seam = merge_pair(accumulated, current, len(previous))
    receipt = retain_seam_outputs(previous, current, None, seam, merged_a=accumulated)
    payload = json.loads(Path(receipt['path']).read_text(encoding='utf-8'))
    for ai, bi in payload['alignment']['mapping']:
        assert literal_view(payload['a'][ai])[0] == literal_view(payload['b'][bi])[0]
    assert payload['previous_tile'] == previous


def test_geometry_is_a_candidate_limit_and_not_deletion_evidence():
    common = "shared seam anchor 1234567890"
    a, b = "prior\n" + common, common + "\nnext"
    merged, seam = merge_pair(a, b, len(a), geometry={"a_window_chars": 5, "b_window_chars": 5})
    assert seam["status"] == "merge_uncertain"
    assert common in merged
    assert seam["deleted_nonspace_chars"] == 0


def test_sparse_markdown_line_projection_does_not_hide_real_overlap():
    previous = "long native paragraph " * 12 + "\n\nshort\n\nx"
    geometry = seam_geometry(previous, "next line\n" * 12, (0, 0, 100, 1440), (0, 1200, 100, 2600))
    assert geometry["a_window_chars"] >= 32
    assert geometry["b_window_chars"] >= 32


def test_short_seam_with_two_stable_edges_keeps_complete_lines():
    left = "abcdefghijKlmno"
    right = "abcdefghijXKlmno"
    merged, seam = merge_pair("prior unique paragraph\n" + left, right + "\nnext paragraph", len(left))
    assert seam["status"] == "merge_uncertain"
    assert seam["difference_count"] == 1
    assert "[openmd variant B]\n\n" + right in merged
    assert left in merged and right in merged
    assert merged.count("abcdefghij") == 2
    assert seam["deletion_mapping_coverage"] == 1


def test_unalignable_physical_band_retains_both_readings_with_zero_deletions():
    a = "Unique previous paragraph.\nAAA111 previous tail"
    b = "ZZZ999 next head\nUnique next paragraph."
    geometry = {"a_window_chars": 20, "b_window_chars": 18, "overlap_bbox": [0, 900, 300, 1200]}
    merged, seam = merge_pair(a, b, len(a), geometry=geometry, arbitrator="third incompatible reading")
    assert seam["status"] == "merge_uncertain"
    assert seam["alignment"] == "unresolved_overlap_band"
    assert seam["deleted_nonspace_chars"] == 0
    assert seam["mapping"] == []
    assert seam["arbitration_result"] == "inconsistent"
    start = seam["difference_ranges"][0]["a_range"][0]
    end = seam["difference_ranges"][0]["b_range"][1]
    expected = a[:start] + "\n\n<!-- openmd:seam-diff start -->\n\n[openmd variant A]\n\n" + a[start:] + "\n\n[openmd variant B]\n\n" + b[:end] + "\n\n<!-- openmd:seam-diff end -->\n\n" + b[end:]
    assert merged == expected


def _successful_attempt(engine="ocr.ovis3"):
    return {"engine": engine, "model": "synthetic", "digest": "synthetic", "seconds": 0.1,
            "breaker": None, "request_kind": "recognition", "peak_vram_mb": None}


def test_pipeline_arbitrates_once_on_actual_shared_band_with_identical_engine():
    backend = LocalOCRBackend()
    a = "before\nshared anchor 1234567890123 item>1 ending anchor 0987654321"
    b = "shared anchor 1234567890123 item_1 ending anchor 0987654321\nafter"
    image = Image.new("RGB", (300, 2000))
    geometry = seam_geometry(a, b, (0, 0, 300, 1200), (0, 900, 300, 2000))
    # Full text windows here isolate arbitration from the line projection.
    geometry.update(a_window_chars=500, b_window_chars=500)
    receipt = {"attempts": [_successful_attempt("ocr.minicpm")]}
    with patch.object(backend, "_attempt", return_value=(_successful_attempt("ocr.minicpm"), a.split("\n", 1)[1])) as attempt, patch("openmd.ocr.pipeline.retain_seam_outputs", return_value={"path": "ignored/out.json", "sha256": "synthetic"}):
        text, seam, recorded = backend._merge_seam(a, a, b, image, geometry, settings(), {}, time.monotonic() + 10, receipt)
    assert attempt.call_count == 1
    assert attempt.call_args.args[0].size == (300, 300)
    assert attempt.call_args.args[-1] == "ocr.minicpm"
    assert recorded["purpose"] == "seam_arbitration"
    assert seam["arbitration"]["bbox"] == [0, 900, 300, 1200]
    assert seam["status"] == "merge_uncertain"
    assert "_" in text and ">" in text


def test_arbitration_unconfirmed_unload_aborts_remaining_tiles():
    backend = LocalOCRBackend()
    a = "before\nshared anchor 1234567890123 value=1 ending anchor 0987654321"
    b = "shared anchor 1234567890123 value=2 ending anchor 0987654321\nafter"
    receipt = {"status": "verified", "fallback_used": False, "reason": None, "attempts": [_successful_attempt()]}
    unsafe = {**_successful_attempt(), "unsafe_to_switch": True, "breaker": {"rule": "ollama_unload_unconfirmed"}}
    plan = {"params": {}, "tiles": [{"y0": 0, "y1": 1200}, {"y0": 900, "y1": 2100}, {"y0": 1800, "y1": 3000}]}
    geometry = {"overlap_bbox": [0, 900, 300, 1200], "a_window_chars": 500, "b_window_chars": 500}
    with patch("openmd.ocr.pipeline.api_json", return_value={"models": [{"name": "ovis3-bf16:latest", "digest": "synthetic"}]}), patch("openmd.ocr.pipeline.split_image", return_value=plan), patch("openmd.ocr.pipeline.seam_geometry", return_value=geometry), patch.object(backend, "_tile", side_effect=[(dict(receipt), a), (dict(receipt), b)]) as tile, patch.object(backend, "_attempt", return_value=(unsafe, "")), patch("openmd.ocr.pipeline.retain_seam_outputs", return_value={}):
        result = backend._recognize_locked(Image.new("RGB", (300, 3000)), "synthetic", 1, True, settings())
    assert tile.call_count == 2
    assert result.status == "partial"
    assert result.error_code == "ollama_unload_unconfirmed"
    assert result.metadata["abort_file"]
    assert result.metadata["pages"][0]["unprocessed_tiles"] == 1
