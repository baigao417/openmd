from pathlib import Path

from scripts.check_readme_consistency import README_FILES, check


def test_four_readmes_have_matching_structure_and_absolute_links():
    result = check(Path(__file__).resolve().parents[1])
    baseline = result[README_FILES[0]]
    assert baseline["headings"] >= 1
    assert baseline["table_rows"] >= 1
    assert baseline["links"] >= 1
    assert baseline["code_fences"] >= 2
    assert all(item["relative_links"] == 0 for item in result.values())
