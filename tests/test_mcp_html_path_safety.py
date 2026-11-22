from __future__ import annotations

import pytest

from openmd import mcp_jobs


@pytest.mark.parametrize("name", ["secret.env", "notes.txt", "no-extension", "fake.html"])
def test_save_page_rejects_non_html_paths(tmp_path, name: str, monkeypatch) -> None:
    path = tmp_path / name
    path.write_text("just ordinary text", encoding="utf-8")
    monkeypatch.setattr(mcp_jobs, "_spawn", lambda: None)
    with pytest.raises(ValueError, match="html_path_not_html"):
        mcp_jobs.save_page("https://example.org/page", str(tmp_path / "out"), html_path=str(path))


@pytest.mark.parametrize("suffix", [".html", ".HTM", ".mhtml", ".xhtml"])
def test_save_page_accepts_html_path(tmp_path, suffix: str, monkeypatch) -> None:
    path = tmp_path / f"page{suffix}"
    path.write_text("\ufeff  <!doctype html><html><body>Example</body></html>", encoding="utf-8")
    monkeypatch.setattr(mcp_jobs, "_spawn", lambda: None)
    queued = mcp_jobs.save_page("https://example.org/page", str(tmp_path / "out"), html_path=str(path))
    assert queued["state"] == "queued"


def test_save_page_rejects_symlink_path(tmp_path, monkeypatch) -> None:
    private = tmp_path / "private.html"
    private.write_text("<html><body>Private</body></html>", encoding="utf-8")
    inside = tmp_path / "subdir"
    inside.mkdir()
    link = inside / "alias.html"
    try:
        link.symlink_to(private)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is not available")
    monkeypatch.setattr(mcp_jobs, "_spawn", lambda: None)
    with pytest.raises(ValueError, match="html_path_symlink_not_allowed"):
        mcp_jobs.save_page("https://example.org/page", str(inside / "out"), html_path=str(link))
