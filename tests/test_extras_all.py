"""`pip install "openmd-cli[all]"` must keep covering every user-facing extra."""
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Extras that are deliberately left out of [all]:
#   pdf  - legacy MarkItDown PDF route, superseded by [document]
#   web  - empty, kept only for compatibility
#   dev  - contributor tooling
#   all  - the aggregate itself
EXCLUDED = {"pdf", "web", "dev", "all"}


def _extras() -> dict[str, list[str]]:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return data["project"]["optional-dependencies"]


def test_all_extra_references_every_user_facing_extra():
    extras = _extras()
    assert "all" in extras and len(extras["all"]) == 1
    match = re.fullmatch(r"openmd-cli\[([a-z0-9,\-]+)\]", extras["all"][0])
    assert match, extras["all"]
    included = set(match.group(1).split(","))
    expected = set(extras) - EXCLUDED
    assert included == expected, f"[all] is missing {sorted(expected - included)} or has unknown {sorted(included - expected)}"


def test_all_extra_only_names_existing_extras():
    extras = _extras()
    included = set(re.search(r"\[(.+)\]", extras["all"][0]).group(1).split(","))
    assert included <= set(extras)
