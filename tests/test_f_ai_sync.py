from pathlib import Path


def test_conversation_sync_ignores_only_generated_export_clock(tmp_path):
    from openmd.core.models import ContentItem, TranscriptResult, TurnBlock
    from openmd.core.output import write_item_result
    item = ContentItem("ai_chat", "single", "https://grok.com/c/fixture", id="fixture", source_type="conversation")
    result = TranscriptResult("fixture body", "verified", "browser_rendered", "fixture", blocks=[TurnBlock("User", "fixture body")])
    sync = {}
    first = write_item_result(item, result, tmp_path, captured_at="2026-11-18T01:00:00Z", sync_receipt=sync)
    original = first.read_bytes()
    write_item_result(item, result, tmp_path, captured_at="2026-11-18T02:00:00Z", sync_receipt=sync)
    assert sync["operation"] == "unchanged"
    assert first.read_bytes() == original
    first.write_text(first.read_text(encoding="utf-8")+"\nUser edit\n", encoding="utf-8")
    updated = write_item_result(item, result, tmp_path, captured_at="2026-11-18T03:00:00Z", sync_receipt=sync)
    assert sync["operation"] == "conflict_kept_user_edit"
    assert updated != first


def test_daily_save_sync_uses_final_destination_manifest(tmp_path):
    from openmd.core.models import ContentItem, TranscriptResult, TurnBlock
    from openmd.core.output import write_item_result
    from openmd.interactive import _save
    item = ContentItem("ai_chat", "single", "https://grok.com/c/fixture", id="fixture", source_type="conversation")
    result = TranscriptResult("fixture body", "verified", "browser_rendered", "fixture", blocks=[TurnBlock("User", "fixture body")])
    destination = tmp_path / "saved"
    targets = []
    for index in range(2):
        stage = tmp_path / str(index)
        source = write_item_result(item, result, stage, captured_at=f"2026-11-18T0{index}:00:00Z")
        targets.append(_save(stage, destination, title="Fixture", source=source))
    assert targets[0] == targets[1]
    assert (destination / ".openmd/manifest.json").is_file()
    assert len(list(destination.glob("*.md"))) == 1
    targets[0].write_text("user edit", encoding="utf-8")
    stage = tmp_path / "third"
    source = write_item_result(item, result, stage)
    conflict = _save(stage, destination, title="Fixture", source=source)
    assert conflict.name == "Fixture.openmd-update.md"
    assert targets[0].read_text() == "user edit"
