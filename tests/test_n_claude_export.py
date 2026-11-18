"""Entirely synthetic official export fixtures; no account or browser data."""
import json
import zipfile

import pytest


CODE = "```python\n" + "print('synthetic')\n" * 1000 + "```\n"


def conversation(cid="synthetic-one", name="Synthetic Claude", created="2026-11-18T01:00:00Z"):
    return {"uuid": cid, "name": name, "created_at": created,
            "updated_at": "2026-11-19T01:00:00Z", "future_row": {"ignored": True},
            "chat_messages": [
                {"uuid": "m1", "sender": "human", "text": "  question\n", "created_at": created,
                 "attachments": [{"file_name": "synthetic.txt", "file_type": "text/plain",
                                  "extracted_content": "DO_NOT_EXPAND_ATTACHMENT"}],
                 "files": [{"file_name": "synthetic.png", "file_type": "image/png",
                            "url": "DO_NOT_FOLLOW_FILE"}]},
                {"uuid": "m2", "sender": "assistant", "text": CODE,
                 "content": [{"type": "thinking", "thinking": "DO_NOT_EXPAND_THINKING"},
                             {"type": "text", "text": CODE},
                             {"type": "tool_use", "input": {"text": "DO_NOT_EXPAND_TOOL"}},
                             {"type": "tool_result", "content": "DO_NOT_EXPAND_RESULT"},
                             {"type": "future_kind", "text": "DO_NOT_GUESS_TEXT"}],
                 "future_message": 123},
            ]}


def export_file(tmp_path, rows, *, zipped=True):
    path = tmp_path / ("export.zip" if zipped else "conversations.json")
    if zipped:
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("conversations.json", json.dumps(rows))
            archive.writestr("../unrelated.txt", "never extract")
            archive.writestr("users.json", "not read")
    else:
        path.write_text(json.dumps(rows), encoding="utf-8-sig")
    return path


@pytest.mark.parametrize("zipped", [True, False])
def test_turns_verbatim_placeholders_and_identity(tmp_path, zipped):
    from openmd.importers.claude import load_conversations
    from openmd.core.sync_manifest import source_id_for
    item, result = load_conversations(export_file(tmp_path, [conversation()], zipped=zipped))[0]
    assert item.url == "https://claude.ai/chat/synthetic-one"
    assert result.metadata["source_id"] == source_id_for(item) == "claude:synthetic-one"
    assert [turn.role for turn in result.blocks] == ["user", "assistant"]
    assert result.blocks[0].text.startswith("  question\n")
    assert result.blocks[1].text.count(CODE) == 1
    assert result.blocks[0].message_id == "m1"
    assert result.metadata["turn_count"] == 2
    assert result.metadata["omission_counts"] == {
        "thinking": 1, "tool_use": 1, "tool_result": 1, "unknown": 1,
        "attachment": 1, "file": 1}
    assert result.metadata["unknown_field_count"] == 4
    assert result.status == "partial"
    assert "synthetic.txt" in result.text and "text/plain" in result.text
    assert "synthetic.png" in result.text and "image/png" in result.text
    assert "DO_NOT_" not in result.text


def test_selection_multiple_and_empty(tmp_path):
    from openmd.importers.claude import load_conversations
    rows = [conversation(), conversation("later", "LATER", "2026-11-19T01:00:00Z"),
            {"uuid": "empty", "name": "Empty", "chat_messages": []}]
    path = export_file(tmp_path, rows)
    assert len(load_conversations(path)) == 3
    empty = load_conversations(path)[-1][1]
    assert empty.metadata["turn_count"] == 0 and empty.status == "partial"
    assert [i.id for i, _ in load_conversations(path, title="later", since="2026-11-19", until="2026-11-20")] == ["later"]
    assert len(load_conversations(path, since=1794963600, until=1794963600)) == 1
    assert load_conversations(path, title="absent") == []
    with pytest.raises(ValueError, match="claude_export_invalid_time_range"):
        load_conversations(path, since=200, until=100)


def test_text_content_without_duplicate_and_missing_optional_fields(tmp_path):
    from openmd.importers.claude import load_conversations
    row = {"uuid": "minimal", "chat_messages": [
        {"sender": "human", "content": [{"type": "text", "text": " a\n"},
                                             {"type": "text", "text": "b "}]},
        {"sender": "assistant", "text": "fallback", "content": [{"type": "tool_use"}]},
        {"sender": "unrecognized", "text": "last"}]}
    item, result = load_conversations(export_file(tmp_path, [row]))[0]
    assert item.title == "Claude conversation"
    assert result.blocks[0].text == " a\nb "
    assert result.blocks[1].text == "fallback\n[Content block: tool_use]"
    assert result.blocks[2].role == "unknown"
    assert result.metadata["unknown_sender_count"] == 1


def test_adjacent_text_blocks_are_not_rewritten_by_marker_lookalikes(tmp_path):
    from openmd.importers.claude import load_conversations
    for suffix in ([], [{"type": "thinking"}]):
        blocks = [{"type": "text", "text": "[Content block: literal prose]"},
                  {"type": "text", "text": "still same text\n"}, *suffix]
        row = {"uuid": "adjacent", "chat_messages": [{"sender": "human", "content": blocks}]}
        text = load_conversations(export_file(tmp_path, [row]))[0][1].blocks[0].text
        assert text.startswith("[Content block: literal prose]still same text\n")


@pytest.mark.parametrize("bad", [None, {}, [1], [{"uuid": "x"}],
    [{"uuid": "../x", "chat_messages": []}], [{"chat_messages": []}],
    [{"uuid": "x", "chat_messages": "bad"}],
    [{"uuid": "x", "chat_messages": [None]}],
    [{"uuid": "x", "mapping": {}, "chat_messages": []}]])
def test_malformed_required_structure_fails_closed(tmp_path, bad):
    from openmd.importers.claude import load_conversations
    with pytest.raises(ValueError, match="claude_export"):
        load_conversations(export_file(tmp_path, bad))


@pytest.mark.parametrize("raw", [b"{broken", b"\xff", b"[NaN]"])
def test_invalid_json(tmp_path, raw):
    from openmd.importers.claude import load_conversations
    path = tmp_path / "conversations.json"
    path.write_bytes(raw)
    with pytest.raises(ValueError, match="claude_export"):
        load_conversations(path)


@pytest.mark.parametrize("member", ["../conversations.json", "/conversations.json",
    "folder/conversations.json", "C:\\conversations.json", "..\\conversations.json"])
def test_zip_does_not_resolve_unsafe_or_nested_paths(tmp_path, member):
    from openmd.importers.claude import load_conversations
    path = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(member, json.dumps([conversation()]))
    with pytest.raises(ValueError, match="claude_export_missing_or_oversized_conversations"):
        load_conversations(path)
    assert not (tmp_path / "conversations.json").exists()


def test_zip_duplicate_and_size_limit(tmp_path, monkeypatch):
    from openmd.importers import claude, common
    path = export_file(tmp_path, [conversation()])
    monkeypatch.setattr(common, "MAX_EXPORT_BYTES", 32)
    with pytest.raises(ValueError, match="claude_export"):
        claude.load_conversations(path)
    monkeypatch.undo()
    with zipfile.ZipFile(path, "a") as archive:
        with pytest.warns(UserWarning):
            archive.writestr("conversations.json", "[]")
    with pytest.raises(ValueError, match="claude_export_missing_or_oversized_conversations"):
        claude.load_conversations(path)


def test_formats_detect_each_other_without_false_positive(tmp_path):
    from openmd.importers.chatgpt import is_chatgpt_export
    from openmd.importers.claude import is_claude_export
    claude = export_file(tmp_path, [conversation()])
    assert is_claude_export(str(claude)) and not is_chatgpt_export(str(claude))
    chatgpt = export_file(tmp_path, [{"id": "gpt", "mapping": {}, "current_node": None}], zipped=False)
    assert is_chatgpt_export(str(chatgpt)) and not is_claude_export(str(chatgpt))
    chatgpt.write_text('[{"uuid":"x","chat_messages":[],"mapping":{}}]')
    assert not is_claude_export(str(chatgpt))
    chatgpt.write_text("[]")
    assert not is_claude_export(str(chatgpt))


@pytest.mark.parametrize("explicit", [False, True])
def test_cli_offline_sync_repeat_and_user_conflict(tmp_path, monkeypatch, capsys, explicit):
    from openmd.cli import main
    from openmd import interactive
    monkeypatch.setattr(interactive, "_authorize", lambda *_a, **_k: pytest.fail("offline importer requested authorization"))
    path = export_file(tmp_path, [conversation(), conversation("two", "Second")])
    destination = tmp_path / "out"
    args = ["go", str(path), "-o", str(destination), "--json"]
    if explicit:
        args += ["--claude-export"]
    for operation in ("created", "unchanged"):
        assert main(args) == 0
        receipt = json.loads(capsys.readouterr().out)
        assert receipt["status"] == "partial"
        assert len(receipt["outputs"]) == 2
        assert all(c["sync"]["operation"] == operation for c in receipt["conversations"])
    from pathlib import Path
    original = Path(receipt["outputs"][0])
    original.write_text(original.read_text(encoding="utf-8") + "\nSynthetic user edit\n", encoding="utf-8")
    assert main(args) == 0
    final = json.loads(capsys.readouterr().out)
    assert final["conversations"][0]["sync"]["operation"] == "conflict_kept_user_edit"
    assert original.read_text(encoding="utf-8").endswith("Synthetic user edit\n")
    manifest = json.loads((destination / ".openmd/manifest.json").read_text())
    assert set(manifest["entries"]) == {"claude:synthetic-one", "claude:two"}


def test_cli_filters_and_rejects_wrong_explicit_format(tmp_path, capsys):
    from openmd.cli import main
    path = export_file(tmp_path, [conversation(), conversation("later", "Later", "2026-11-19")])
    args = ["go", str(path), "-o", str(tmp_path / "out"), "--json", "--claude-export",
            "--claude-title", "later", "--claude-since", "2026-11-19", "--claude-until", "2026-11-20"]
    assert main(args) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert [c["source_id"] for c in receipt["conversations"]] == ["claude:later"]
    assert main(["go", str(path), "-o", str(tmp_path / "wrong"), "--chatgpt-export", "--json"]) != 0
    capsys.readouterr()
    assert not (tmp_path / "wrong").exists()


def test_sync_identity_matches_claude_detail_without_network():
    from openmd.core.sync_manifest import source_id_for
    from openmd.core.models import ContentItem
    item = ContentItem("ai_chat", "single", "https://claude.ai/chat/synthetic-one", id="ignored",
                       source_type="conversation", raw_meta={"chat_site": "claude"})
    assert source_id_for(item) == "claude:synthetic-one"
    from openmd.adapters.registry import create_default_registry
    detail_item = create_default_registry().adapter_for(item.url).resolve_items(item.url)[0]
    assert source_id_for(detail_item) == source_id_for(item)


def test_plain_text_only_export_is_verified_and_file_kind_is_preserved(tmp_path):
    from openmd.importers.claude import load_conversations
    row = {"uuid": "plain", "chat_messages": [{"sender": "human", "text": "  exact\n"}]}
    path = export_file(tmp_path, [row])
    _, result = load_conversations(path)[0]
    assert result.status == "verified" and result.blocks[0].text == "  exact\n"
    row["chat_messages"][0]["files"] = [{"file_name": "synthetic.png", "file_kind": "image"}]
    _, result = load_conversations(export_file(tmp_path, [row]))[0]
    assert "synthetic.png (image)" in result.text


def test_empty_and_conflicting_flags_stay_offline(tmp_path, capsys, monkeypatch):
    from openmd.cli import main
    from openmd import interactive
    monkeypatch.setattr(interactive, "_authorize", lambda *_a, **_k: pytest.fail("offline import authorization"))
    path = export_file(tmp_path, [])
    args = ["go", str(path), "-o", str(tmp_path / "out"), "--claude-export", "--json"]
    assert main(args) == 0
    assert json.loads(capsys.readouterr().out)["outputs"] == []
    assert main([*args, "--chatgpt-export"]) == 1
    capsys.readouterr()


@pytest.mark.parametrize("field,value", [("content", {}), ("attachments", {}), ("files", {})])
def test_malformed_optional_containers_have_safe_errors(tmp_path, field, value):
    from openmd.importers.claude import load_conversations
    row = conversation()
    row["chat_messages"][0][field] = value
    with pytest.raises(ValueError, match="claude_export_invalid_"):
        load_conversations(export_file(tmp_path, [row]))
