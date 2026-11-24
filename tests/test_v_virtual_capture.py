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
    assert "150" in source and "90_000" in source


def test_virtual_capture_waits_for_async_content_and_only_stops_on_scroll_stall():
    source = (ROOT / "src" / "openmd" / "browser_extension" / "page_capture.js").read_text(encoding="utf-8")
    assert "const MIN_STEP_SETTLE_MS = 1_400;" in source
    assert "await waitQuiet(container, MIN_STEP_SETTLE_MS)" in source
    assert "const moved = after > current + 1;" in source
    assert "newCount === before && !moved" in source
    assert "const afterTotal = container === document.documentElement" in source
    assert "stopReason === \"end_reached\" && unique.length === unique[unique.length - 1]" in source


def test_extension_capture_uses_chrome_scripting_signatures():
    background = (ROOT / "src" / "openmd" / "browser_extension" / "background.js").read_text(encoding="utf-8")
    assert 'executeScript({ target: { tabId }, files: ["page_capture.js"] })' in background
    assert "func: async (aiHosts)" in background
    assert "args: [AI_CHAT_HOSTS]" in background


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


def test_virtual_capture_detects_computed_matrix_transforms_and_prefers_inner_container():
    """Round16 real-page finding: computed transforms are matrix(), and a scrollable document
    must not win over the inner list that actually holds the virtual rows."""
    source = (ROOT / "src" / "openmd" / "browser_extension" / "page_capture.js").read_text(encoding="utf-8")
    assert "/matrix|translate/i.test(style.transform)" in source
    assert 'style.transform !== "none"' in source
    assert "best.container.contains(candidate)" in source
    assert "translate(?:3d|Y|X)?" not in source
