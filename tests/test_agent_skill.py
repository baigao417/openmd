from importlib.resources import files
from pathlib import Path


def test_agent_skill_is_packaged_and_has_receipt_contract():
    skill = Path(__file__).resolve().parents[1] / "src" / "openmd" / "SKILL.md"
    text = skill.read_text(encoding="utf-8")
    assert "status" in text and "source_level" in text
    assert "openmd opencli" in text
    assert '"SKILL.md"' in (skill.parents[2] / "pyproject.toml").read_text(encoding="utf-8")
    assert files("openmd").joinpath("SKILL.md").is_file()
