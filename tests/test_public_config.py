"""Synthetic checks for explicitly user-provided configuration."""
from pathlib import Path

import pytest

from openmd.backends.bilibili_wbi import (
    BILIBILI_COOKIE_FILE_ENV,
    BILIBILI_COOKIE_HEADER_ENV,
    WbiSigningError,
    bilibili_cookie_header,
    bilibili_cookie_header_for_url,
)


def test_cookie_file_is_explicit_and_read_only(tmp_path: Path) -> None:
    path = tmp_path / "synthetic-header.txt"
    path.write_text("sample=demo", encoding="utf-8")
    before = path.read_bytes()
    assert bilibili_cookie_header({BILIBILI_COOKIE_FILE_ENV: str(path)}) == "sample=demo"
    assert path.read_bytes() == before


def test_foreign_host_does_not_read_cookie_file(tmp_path: Path) -> None:
    config = {BILIBILI_COOKIE_FILE_ENV: str(tmp_path / "missing")}
    assert bilibili_cookie_header_for_url("https://example.com/video", config) is None
    assert bilibili_cookie_header_for_url("https://bilibili.com.example.com/video", config) is None


def test_unreadable_file_error_has_no_private_path(tmp_path: Path) -> None:
    path = tmp_path / "user-private-location"
    with pytest.raises(WbiSigningError) as caught:
        bilibili_cookie_header({BILIBILI_COOKIE_FILE_ENV: str(path)})
    assert str(path) not in str(caught.value)


def test_multiline_header_is_rejected_without_echo(tmp_path: Path) -> None:
    path = tmp_path / "synthetic-header.txt"
    path.write_text("sample=demo\nother=demo", encoding="utf-8")
    with pytest.raises(WbiSigningError) as caught:
        bilibili_cookie_header({BILIBILI_COOKIE_FILE_ENV: str(path)})
    assert "sample" not in str(caught.value)


def test_empty_mapping_does_not_fall_back_to_process_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(BILIBILI_COOKIE_HEADER_ENV, "sample=demo")
    assert bilibili_cookie_header({}) is None
