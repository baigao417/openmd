"""Runtime extension updates use synthetic bundles and isolated data only."""
import shutil
from pathlib import Path

import pytest

from openmd import browser_host, browser_setup


@pytest.fixture
def bundle(tmp_path, monkeypatch):
    source = tmp_path / "package"
    source.mkdir()
    (source / "background.js").write_text("new worker", encoding="utf-8")
    (source / "manifest.json").write_text('{"version":"2"}', encoding="utf-8")
    monkeypatch.setattr(browser_setup, "EXTENSION_DIR", source)
    monkeypatch.setenv("OPENMD_DATA_DIR", str(tmp_path / "data"))
    return source, tmp_path / "data" / "browser" / "extension"


def test_ping_refreshes_changed_copy_and_is_idempotent(bundle):
    source, target = bundle
    target.mkdir(parents=True)
    (target / "obsolete.js").write_text("old", encoding="utf-8")
    reply = browser_host.handle({"type": "ping"})
    assert reply["ok"] and reply["extension_updated"] is True
    assert len(reply["extension_hash"]) == 64
    assert (target / "background.js").read_bytes() == (source / "background.js").read_bytes()
    assert not (target / "obsolete.js").exists()
    before = {p.name: p.stat().st_mtime_ns for p in target.iterdir()}
    again = browser_host.handle({"type": "ping"})
    assert again["extension_updated"] is False
    assert again["extension_hash"] == reply["extension_hash"]
    assert {p.name: p.stat().st_mtime_ns for p in target.iterdir()} == before


def test_ping_reports_unwritable_copy_without_destroying_old(bundle, monkeypatch):
    _, target = bundle
    target.mkdir(parents=True)
    old = target / "background.js"
    old.write_text("old worker", encoding="utf-8")
    def denied(*args, **kwargs):
        raise PermissionError("synthetic permission denial")
    monkeypatch.setattr(shutil, "copytree", denied)
    reply = browser_host.handle({"type": "ping"})
    assert reply["ok"] is False
    assert reply["error_code"] == "extension_sync_failed"
    assert "browser-setup" in reply["error"]
    assert old.read_text(encoding="utf-8") == "old worker"


def test_publish_failure_rolls_back_complete_previous_copy(bundle, monkeypatch):
    _, target = bundle
    target.mkdir(parents=True)
    (target / "background.js").write_text("old worker", encoding="utf-8")
    original = Path.replace
    def denied(self, destination):
        if self.parent.name.startswith(".extension-stage-") and Path(destination) == target:
            raise PermissionError("synthetic publish denial")
        return original(self, destination)
    monkeypatch.setattr(Path, "replace", denied)
    reply = browser_host.handle({"type": "ping"})
    assert reply["ok"] is False and reply["error_code"] == "extension_sync_failed"
    assert [p.name for p in target.iterdir()] == ["background.js"]
    assert (target / "background.js").read_text(encoding="utf-8") == "old worker"


def test_content_hash_detects_names_and_bytes_but_ignores_python_cache(bundle):
    from openmd.browser_extension_sync import extension_content_hash
    source, _ = bundle
    first = extension_content_hash(source)
    cache = source / "__pycache__"
    cache.mkdir()
    (cache / "ignored.pyc").write_bytes(b"cache")
    assert extension_content_hash(source) == first
    (source / "background.js").rename(source / "renamed.js")
    assert extension_content_hash(source) != first
    renamed = extension_content_hash(source)
    (source / "renamed.js").write_text("changed", encoding="utf-8")
    assert extension_content_hash(source) != renamed


def test_stable_setup_uses_same_runtime_sync(bundle):
    from openmd.browser_extension_sync import extension_content_hash
    source, target = bundle
    assert browser_setup.stable_extension_dir() == target
    assert extension_content_hash(source) == extension_content_hash(target)
    assert browser_host.handle({"type": "ping"})["extension_updated"] is False
