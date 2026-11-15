"""T5 regressions: seam markers stay outside Markdown block structure."""

import pytest

from openmd.ocr.tiling import _html_blocks, _markdown_block_range, merge_pair, merge_texts


def _full_band():
    return {"a_window_chars": 500, "b_window_chars": 500, "overlap_bbox": [0, 1, 10, 2]}


@pytest.mark.parametrize("container", ["table", "pre", "details", "div", "script", "textarea"])
def test_html_containers_across_blank_lines_expand_from_internal_geometry(container):
    a = f'<{container}>\nheader\n\nvalue=1234567890\n\nend\n</{container}>'
    b = f'value=1234567899\n\nend\n</{container}>\n\nafter'
    merged, seam = merge_pair(a, b, len(a), geometry={"a_window_chars": 16, "b_window_chars": 16, "overlap_bbox": [0, 1, 10, 2]})
    assert seam["status"] == "merge_uncertain"
    assert a in merged and b in merged
    assert merged.index("[openmd variant A]") < merged.index(a)
    assert merged.index("[openmd variant B]") > merged.index(a) + len(a)
    assert "B" in seam["difference_literal_markdown"]
    assert seam["deleted_nonspace_chars"] == 0


def test_review_html_table_fresh_reproduction_preserves_both_whole_inputs():
    a = '<table>\n<tr><td>header</td></tr>\n\n<tr><td>1234567890</td></tr>\n\n</table>'
    b = '<tr><td>1234567899</td></tr>\n\n</table>\n\nafter'
    merged, seam = merge_pair(a, b, len(a), geometry={"a_window_chars": 16, "b_window_chars": 16, "overlap_bbox": [0, 1, 10, 2]})
    _assert_complete_readings(merged, a, b)
    assert seam["difference_ranges"] == [{"a_range": [0, len(a)], "b_range": [0, len(b)]}]
    assert seam["difference_literal_markdown"] == ["B"]
    assert merged.index("[openmd variant B]") > merged.index("</table>")


@pytest.mark.parametrize("opener,closer", [("<table>", "</table>"), ("<pre>", "</pre>"), ("<details>", "</details>"), ("<!--", "-->"), ("<unknown-widget>", "</unknown-widget>")])
@pytest.mark.parametrize("truncated", [False, True])
def test_html_comments_unknown_and_truncated_containers_are_atomic(opener, closer, truncated):
    a = opener + '\nfirst\n\nvalue=1234567890\n\nlast' + ('' if truncated else '\n' + closer)
    b = a.replace('1234567890', '1234567899')
    merged, seam = merge_pair(a, b, len(a), geometry={"a_window_chars": 8, "b_window_chars": 8, "overlap_bbox": [0, 1, 10, 2]})
    _assert_complete_readings(merged, a, b)
    if truncated or 'unknown' in opener:
        assert seam["difference_literal_markdown"] == ["A", "B"]
        assert merged.count('[openmd: unsafe source container; literal Markdown]') == 2
    else:
        assert seam["difference_literal_markdown"] == []


def test_new_bypass_mixed_case_tag_quoted_angle_and_nested_details():
    a = '<DETAILS data-x="a>b"><summary>heading</summary>\n\n<pre>\nvalue=1234567890\n\n</pre>\n</DETAILS>'
    b = a.replace('1234567890', '1234567899')
    merged, seam = merge_pair(a, b, len(a), geometry=_full_band())
    _assert_complete_readings(merged, a, b)
    assert not seam["difference_literal_markdown"]
    assert _html_blocks(a)[1] == []


def test_otsl_html_tables_and_third_tile_keep_first_seam_indivisible():
    from openmd.ocr.otsl import table_html
    a = table_html('<otsl><fcel>header<nl><fcel>1234567890</otsl>')
    b = a.replace('1234567890', '1234567899')
    first, _ = merge_pair(a, b, len(a), geometry=_full_band())
    c = '<pre>\nthird tile=54321\n\ntruncated'
    merged, seam = merge_pair(first, c, len(b), geometry={"a_window_chars": 8, "b_window_chars": 8, "overlap_bbox": [0, 1, 10, 2]})
    assert first.strip() in merged and a in merged and b in merged and c in merged
    assert seam["deleted_nonspace_chars"] == 0
    assert 'B' in seam["difference_literal_markdown"]
    integrated, result = merge_texts([a, b, c])
    assert a in integrated and b in integrated and c in integrated
    assert all(s["status"] == "merge_uncertain" for s in result['seams'])


@pytest.mark.parametrize('source', ['<table/>\n\nvalue=12345', '<table data-x="unterminated\n\nvalue=12345', '<!-- first\n\nvalue=12345', 'tail of comment -->\n\nafter'])
def test_malformed_or_orphan_html_is_explicit_literal(source):
    merged, seam = merge_pair(source, 'unrelated other tile', len(source), geometry=_full_band())
    assert source in merged and 'A' in seam['difference_literal_markdown']
    assert merged.index('[openmd variant A]') < merged.index(source)


def test_html_text_in_markdown_code_fence_does_not_create_html_container():
    source = '```html\n<table>\n\nunknown code\n```'
    assert _html_blocks(source)[1] == []


def test_unclosed_html_inside_literal_fence_cannot_swallow_later_container():
    code = '```html\n<!-- deliberately unclosed example\n```\n\n'
    table = '<table>\n<tr><td>header</td></tr>\n\n<tr><td>1234567890</td></tr>\n\n</table>'
    source = code + table
    point = source.index('1234567890')
    lo, hi = _markdown_block_range(source, point, point + 1)
    assert source[lo:hi] == table


def _assert_complete_readings(merged: str, a: str, b: str):
    assert a in merged
    assert b in merged
    marker = merged.index("<!-- openmd:seam-diff start -->")
    end = merged.index("<!-- openmd:seam-diff end -->")
    body = merged[marker:end]
    assert "[openmd variant A]\n\n" in body
    assert "\n\n[openmd variant B]\n\n" in body


def test_table_difference_keeps_complete_rows_and_numbers():
    a = "| key | value |\n|---|---|\n| abcdefghijklm | 1234567890 |"
    b = "| abcdefghijklm | 1234567899 |\n| next | 42 |"
    merged, seam = merge_pair(a, b, len(a), min_window=500)
    assert seam["status"] == "merge_uncertain"
    assert seam["difference_boundary"] == "markdown_block"
    _assert_complete_readings(merged, a, b)
    assert "1234567890" in merged and "1234567899" in merged
    assert "| abcdefghijklm | 123456789\n" not in merged


def test_list_item_with_lazy_continuation_is_not_split_at_inline_difference():
    a = "- install the package and verify checksum=1234567890\n  continue this item safely"
    b = "- install the package and verify checksum=1234567899\n  continue this item safely"
    merged, seam = merge_pair(a, b, len(a), min_window=500)
    assert seam["status"] == "merge_uncertain"
    _assert_complete_readings(merged, a, b)
    assert "checksum=1234567890" in merged and "checksum=1234567899" in merged


def test_fenced_code_block_with_blank_line_is_atomic():
    a = "```python\nvalue = 1234567890\n\nprint(value)\n```"
    b = "```python\nvalue = 1234567899\n\nprint(value)\n```"
    merged, seam = merge_pair(a, b, len(a), min_window=500, geometry=_full_band())
    assert seam["status"] == "merge_uncertain"
    _assert_complete_readings(merged, a, b)
    assert merged.count("```python") == 2


def test_truncated_fence_uses_explicit_literal_rendering_not_swallowed_labels():
    a = "```python\nvalue = 1234567890\n\nunfinished()"
    b = "```python\nvalue = 1234567899\n\nunfinished()"
    merged, seam = merge_pair(a, b, len(a), min_window=500, geometry=_full_band())
    assert seam["status"] == "merge_uncertain"
    assert seam["difference_literal_markdown"] == ["A", "B"]
    _assert_complete_readings(merged, a, b)
    assert merged.count("[openmd: unclosed source fence; literal Markdown]") == 2
    assert merged.count("````markdown\n") == 2
    assert merged.count("\n````\n") == 2


def test_indented_code_block_is_atomic_new_variant():
    a = "before\n\n    total = 1234567890\n\n    return total"
    b = "    total = 1234567899\n\n    return total\n\nnext"
    merged, seam = merge_pair(a, b, len(a), min_window=500)
    assert seam["status"] == "merge_uncertain"
    assert "    total = 1234567890\n\n    return total" in merged
    assert "    total = 1234567899\n\n    return total" in merged
    assert merged.startswith("before") and merged.endswith("next")
    assert "total = 1234567890" in merged and "total = 1234567899" in merged


def test_geometry_unresolved_band_uses_same_block_boundary():
    a = "| key | value |\n|---|---|\n| left | 1234567890 |"
    b = "| key | value |\n|---|---|\n| right | 1234567899 |"
    merged, seam = merge_pair(
        a,
        b,
        len(a),
        geometry={"a_window_chars": 8, "b_window_chars": 8, "overlap_bbox": [0, 1, 10, 2]},
    )
    assert seam["status"] == "merge_uncertain"
    assert seam["alignment"] == "unresolved_overlap_band"
    _assert_complete_readings(merged, a, b)
    assert seam["deleted_nonspace_chars"] == 0


def test_block_range_expands_mid_line_to_whole_nonblank_block():
    source = "intro\n\n| a | 1 |\n|---|---|\n| b | 2 |\n\nafter\n"
    start, end = _markdown_block_range(source, source.index("2"), source.index("2") + 1)
    assert source[start:end] == "| a | 1 |\n|---|---|\n| b | 2 |\n"


@pytest.mark.parametrize("arbiter,agreement", [(None, None), ("A", ["A"]), ("B", ["B"]), ("third", [])])
def test_arbiter_never_discards_the_other_complete_table_reading(arbiter, agreement):
    left = "| abcdefghijklm | 1234567890 |"
    right = "| abcdefghijklm | 1234567899 |"
    reading = {"A": left, "B": right, "third": "unrelated third reading"}.get(arbiter)
    merged, seam = merge_pair(left, right, len(left), min_window=500, arbitrator=reading)
    assert seam["status"] == "merge_uncertain"
    _assert_complete_readings(merged, left, right)
    assert seam["deleted_nonspace_chars"] == 0 and seam["mapping"] == []
    if arbiter is not None:
        assert seam["arbitration_agreement"] == agreement


def test_unaligned_unclosed_fences_get_whole_input_safe_differences_without_geometry():
    a = "```python\nwholly unrelated first fragment"
    b = "```python\nseparate second fragment"
    merged, seam = merge_pair(a, b, len(a))
    assert seam["status"] == "merge_uncertain"
    assert seam["alignment"] == "unresolved_whole_inputs"
    assert seam["difference_literal_markdown"] == ["A", "B"]
    _assert_complete_readings(merged, a, b)


def test_prior_seam_region_is_indivisible_when_new_boundary_hits_it():
    first, _ = merge_pair("old reading A only", "old reading B differs", 18)
    accumulated = "prior body\n\n" + first + "\n\nafter old seam"
    point = accumulated.index("old reading B")
    start, end = _markdown_block_range(accumulated, point, point + 1)
    region = accumulated[start:end]
    assert "<!-- openmd:seam-diff start -->" in region
    assert "<!-- openmd:seam-diff end -->" in region
    assert "old reading A only" in region and "old reading B differs" in region
    assert "prior body" not in region and "after old seam" not in region


def test_accumulated_prior_seam_stays_complete_in_later_geometry_difference():
    first, _ = merge_pair("old reading A only", "old reading B differs", 18)
    a = "prior paragraph\n\n" + first.rstrip()
    b = "unrelated new tile input"
    merged, seam = merge_pair(a, b, len(a), geometry={"a_window_chars": 8, "b_window_chars": 8, "overlap_bbox": [0, 1, 10, 2]})
    assert seam["status"] == "merge_uncertain"
    assert first.strip() in merged
    assert b in merged
    assert seam["deleted_nonspace_chars"] == 0
    start = seam["difference_ranges"][0]["a_range"][0]
    assert a[start:].startswith("<!-- openmd:seam-diff start -->")


@pytest.mark.parametrize("source,start_text,end_text", [
    ("- first\n\n  continue 12345\n\n  last line\n\noutside", "12345", "last line\n"),
    ("~~~python\nvalue = 12345\n\nfinish()\n~~~\n\noutside", "12345", "~~~\n"),
    ("```python\nvalue = 12345\n\ntruncated", "12345", "truncated"),
    ("    value = 12345\n\n    finish()\n\noutside", "12345", "    finish()\n"),
])
def test_ranges_retain_complete_multiline_markdown_constructs(source, start_text, end_text):
    point = source.index(start_text)
    start, end = _markdown_block_range(source, point, point + 1)
    assert start == 0
    assert source[start:end].endswith(end_text)
