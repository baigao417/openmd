"""Check that the four public README translations keep the same Markdown shape."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
README_FILES = ("README.md", "README.zh-CN.md", "README.ja.md", "README.ko.md")
_LINK_RE = re.compile(r"(?<!!)\[[^\]]+\]\(https?://[^)]+\)")
_RELATIVE_RE = re.compile(r"(?<!!)\[[^\]]+\]\((?!https?://|#)[^)]+\)")


def metrics(path: Path) -> dict[str, int]:
    text = path.read_text(encoding="utf-8")
    return {
        "headings": len(re.findall(r"^#{1,6}\s", text, re.MULTILINE)),
        "table_rows": len(re.findall(r"^\|", text, re.MULTILINE)),
        "links": len(_LINK_RE.findall(text)),
        "code_fences": len(re.findall(r"^```", text, re.MULTILINE)),
        "relative_links": len(_RELATIVE_RE.findall(text)),
    }


def check(root: Path = ROOT) -> dict[str, dict[str, int]]:
    result = {name: metrics(root / name) for name in README_FILES}
    baseline = result[README_FILES[0]]
    for name, current in result.items():
        if current["relative_links"]:
            raise AssertionError(f"{name} contains relative links: {current}")
        for key in ("headings", "table_rows", "links", "code_fences"):
            if current[key] != baseline[key]:
                raise AssertionError(f"{name} {key}={current[key]} != {baseline[key]}")
    return result


if __name__ == "__main__":
    import json

    print(json.dumps(check(), ensure_ascii=False, indent=2))
