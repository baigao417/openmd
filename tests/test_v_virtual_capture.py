from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_shared_page_capture_script_declares_generic_bounded_protocol():
    script_path = ROOT / "src" / "openmd" / "browser_extension" / "page_capture.js"
    assert script_path.is_file()
    source = script_path.read_text(encoding="utf-8")
    assert "__OPENMD_PAGE_CAPTURE__" in source
    assert "data-paragraph-index" in source
    assert "content-visibility" in source
    assert "MutationObserver" in source
    assert "150" in source and "45_000" in source


def test_snapshot_item_carries_capture_meta(tmp_path):
    from openmd.backends.loaded_chat import snapshot_item

    page = tmp_path / "page.html"
    page.write_text("<html><body><main><p>body</p></main></body></html>", encoding="utf-8")
    capture = {
        "coverage": "scrolled_full",
        "virtual_detected": True,
        "stop_reason": "end_reached",
        "steps": 3,
        "elapsed_ms": 123,
        "seen_count": 4,
        "new_count": 4,
        "duplicate_count": 0,
        "collected_count": 4,
        "max_index": 3,
        "index_gaps": [],
        "declared_total": 4,
    }
    item = snapshot_item(page, "https://example.org/page", capture_meta=capture)
    assert item.raw_meta["capture"] == capture


def test_browser_host_persists_capture_meta_in_snapshot_job(tmp_path):
    from openmd import browser_host

    capture = {
        "coverage": "visible_only",
        "virtual_detected": True,
        "stop_reason": "time_limit",
        "steps": 150,
        "elapsed_ms": 45_000,
        "seen_count": 1,
        "new_count": 1,
        "duplicate_count": 0,
        "collected_count": 1,
        "max_index": 0,
        "index_gaps": [],
        "declared_total": None,
    }
    job = browser_host.start_snapshot_job(
        "<html><body>body</body></html>",
        "https://example.org/page",
        save_to=str(tmp_path),
        capture_meta=capture,
        spawn=lambda *args, **kwargs: None,
    )
    status = json.loads((Path(job["snapshot_path"]).parent / "status.json").read_text(encoding="utf-8"))
    assert status["capture_meta"] == capture
