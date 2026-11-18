"""Synthetic official export trees, never account data."""
import json
import zipfile

import pytest


def conversation(cid="fixture", title="Fixture", created=100):
    def node(parent, role, parts, children=(), kind="text"):
        return {"parent": parent, "children": list(children), "message": {
            "id": role + str(parent), "author": {"role": role}, "create_time": created,
            "content": {"content_type": kind, "parts": parts}}}
    return {"id": cid, "title": title, "create_time": created, "current_node": "final",
            "mapping": {
                "root": {"parent": None, "children": ["user"], "message": None},
                "user": node("root", "user", ["  question\n"], ["old", "tool"]),
                "old": node("user", "assistant", ["not the current branch"]),
                "tool": node("user", "tool", ["tool result"], ["image"]),
                "image": node("tool", "user", [{"content_type": "image_asset_pointer", "asset_pointer": "file-service://synthetic"}], ["final"], "multimodal_text"),
                "final": node("image", "assistant", ["```python\nprint('fixture')\n```\n"]),
            }}


def export_zip(tmp_path, rows):
    path = tmp_path / "export.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("conversations.json", json.dumps(rows))
        archive.writestr("../unrelated.txt", "must never extract zip members")
    return path


def test_current_branch_verbatim_tools_images_and_alternatives(tmp_path):
    from openmd.importers.chatgpt import load_conversations
    parsed = load_conversations(export_zip(tmp_path, [conversation()]))
    item, result = parsed[0]
    assert item.url == "https://chatgpt.com/c/fixture"
    assert [b.role for b in result.blocks] == ["user", "tool", "user", "assistant"]
    assert result.blocks[0].text == "  question\n"
    assert "not the current branch" not in result.text
    assert "file-service://synthetic" in result.blocks[2].text
    assert "```python" in result.blocks[-1].text
    assert result.metadata["alternative_branch_count"] == 1
    assert result.metadata["source_id"] == "chatgpt:fixture"
    from openmd.core.sync_manifest import source_id_for
    from openmd.adapters.registry import create_default_registry
    online = create_default_registry().adapter_for(item.url).resolve_items(item.url)[0]
    assert source_id_for(item) == source_id_for(online) == "chatgpt:fixture"
    assert not (tmp_path.parent / "unrelated.txt").exists()


def test_selection_and_empty_conversation(tmp_path):
    from openmd.importers.chatgpt import load_conversations
    rows = [conversation("early", "Earlier", 100), conversation("late", "LATER", 300),
            {"id": "empty", "title": "Empty", "mapping": {}, "current_node": None}]
    path = export_zip(tmp_path, rows)
    assert [item.id for item, _ in load_conversations(path, title="later", since=200, until=400)] == ["late"]
    assert len(load_conversations(path)) == 2
    assert load_conversations(path, since=101, until=299) == []


@pytest.mark.parametrize("bad", ["cycle", "missing", "unknown_current"])
def test_invalid_current_path_fails_closed(tmp_path, bad):
    from openmd.importers.chatgpt import load_conversations
    row = conversation()
    if bad == "cycle":
        row["mapping"]["user"]["parent"] = "final"
    elif bad == "missing":
        row["mapping"]["final"]["parent"] = "absent"
    else:
        row["current_node"] = "absent"
    with pytest.raises(ValueError, match="chatgpt_export"):
        load_conversations(export_zip(tmp_path, [row]))


def test_go_auto_detects_and_syncs_export_without_network(tmp_path, capsys, monkeypatch):
    from openmd.cli import main
    from openmd import interactive
    monkeypatch.setattr(interactive, "_authorize", lambda *_a, **_k: pytest.fail("offline import requested network authorization"))
    path = export_zip(tmp_path, [conversation("one"), conversation("two")])
    out = tmp_path / "out"
    for explicit in ([], ["--chatgpt-export"]):
        assert main(["go", str(path), "-o", str(out), "--json", *explicit]) == 0
        receipt = json.loads(capsys.readouterr().out)
        assert len(receipt["outputs"]) == 2
    assert len(list(out.rglob("*.md"))) == 2
    manifest = json.loads((out / ".openmd/manifest.json").read_text())
    assert set(manifest["entries"]) == {"chatgpt:one", "chatgpt:two"}
