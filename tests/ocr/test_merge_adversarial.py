from tests.ocr.seam_chain import merge_texts
"""Synthetic regressions for lossless, seam-anchored overlap merging."""
from unittest.mock import patch

from PIL import Image

from openmd.ocr.config import settings
from openmd.ocr.pipeline import LocalOCRBackend
from openmd.ocr.tiling import merge_pair


def assert_preserved(a, b, **kwargs):
    merged, receipt = merge_pair(a, b, len(a), **kwargs)
    assert a in merged and b in merged
    assert merged == "\n\n<!-- openmd:seam-diff start -->\n\n[openmd variant A]\n\n" + a + "\n\n[openmd variant B]\n\n" + b + "\n\n<!-- openmd:seam-diff end -->\n\n"
    assert receipt["status"] == "merge_uncertain"
    assert receipt["dropped_prev_tail_chars"] == 0
    assert receipt["dropped_next_head_chars"] == 0
    return receipt


def test_unique_b_prefix_is_not_discarded_to_reach_an_interior_match():
    a = "A" * 160 + "COMMONSEQUENCE1234567890"
    b = "UNIQUE_B_PREFIX_THAT_IS_NOT_OVERLAP" + "COMMONSEQUENCE1234567890" + "BTAIL"
    assert_preserved(a, b)


def test_match_must_reach_a_tail_without_discarding_unique_body():
    common = "COMMONSEQUENCE1234567890"
    assert_preserved("A" * 160 + common + "UNIQUE_A_TAIL", common + "BTAIL")


def test_even_a_single_nonmatching_b_character_must_be_preserved():
    common = "distinct seam anchor 1234567890"
    merged, receipt = merge_pair("previous content " + common, "X" + common + " next content", 100)
    assert receipt["status"] == "merge_uncertain"
    assert "X" in merged
    assert "next content" in merged
    assert receipt["deletion_mapping_coverage"] == 1


def test_repeated_anchor_inside_a_window_is_ambiguous():
    common = "COMMONSEQUENCE1234567890"
    receipt = assert_preserved(common + " different " + common, common + "BTAIL")
    assert receipt["reason"] == "ambiguous_overlap"


def test_repeated_anchor_inside_b_window_is_ambiguous():
    common = "COMMONSEQUENCE1234567890"
    receipt = assert_preserved("previous content " + common, common + " different " + common)
    assert receipt["reason"] == "ambiguous_overlap"


def test_multiple_suffix_prefix_candidates_are_ambiguous():
    common = "COMMONSEQUENCE1234567890"
    receipt = assert_preserved("previous content " + common * 2, common * 2 + "BTAIL")
    assert receipt["candidate_count"] == 2
    assert receipt["reason"] == "ambiguous_overlap"


def test_short_exact_match_keeps_both_original_inputs():
    receipt = assert_preserved("previous body 12345678", "12345678 next body")
    assert receipt["reason"] == "weak_overlap"


def test_normalization_cannot_delete_partially_matched_ligature():
    # Twelve normalized characters clear the independent weak-match guard,
    # isolating the rule that a source ligature must never be partly dropped.
    receipt = assert_preserved("previous content 12345678901f", "12345678901ﬃ next body")
    assert receipt["reason"] == "normalization_boundary"


def test_unique_exact_overlap_merges_only_the_duplicated_prefix():
    common = "共同接缝完整正文编号1234567890。\n"
    a = "上一片唯一正文。\n" + common
    b = common + "下一片唯一正文。\n"
    merged, receipt = merge_texts([a, b])
    assert merged == a + "下一片唯一正文。\n"
    assert receipt["seams"][0]["status"] == "merged"
    assert receipt["warnings"] == []


def test_uncertain_merge_preserves_original_whitespace():
    assert_preserved("  unique first input\n\n", "\n\n unique second input  ")


def test_review_pipeline_repro_is_partial_and_keeps_unique_prefix():
    prefix = "".join(chr(0x4E00 + n) for n in range(160))
    a = prefix + "COMMONSEQUENCE1234567890"
    b = "UNIQUE_B_PREFIX_THAT_IS_NOT_OVERLAP" + "COMMONSEQUENCE1234567890" + "BTAIL"
    config = settings()
    backend = LocalOCRBackend("ocr.chain")
    tile = {"status": "verified", "fallback_used": False, "reason": None, "attempts": []}
    with patch("openmd.ocr.pipeline.api_json", return_value={"models": [
        {"name": config["ovis_model"], "digest": "synthetic"}
    ]}), patch.object(backend, "_tile", side_effect=[(dict(tile), a), (dict(tile), b)]):
        result = backend._recognize_locked(
            Image.new("RGB", (400, 2000), "white"), "synthetic seam", 1, True, config
        )
    assert result.status == "partial"
    raw_readings = result.text.replace("\n\n<!-- openmd:seam-diff start -->\n\n[openmd variant A]\n\n", "").replace("\n\n[openmd variant B]\n\n", "").replace("\n\n<!-- openmd:seam-diff end -->\n\n", "")
    assert raw_readings == a + b
    assert result.metadata["pages"][0]["merge"]["seams"][0]["deleted_nonspace_chars"] == 0
    assert result.metadata["pages"][0]["reason"] == "merge_uncertain"
    assert result.metadata["pages"][0]["merge"]["warnings"]
