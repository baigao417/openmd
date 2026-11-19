from pathlib import Path

import pytest

from openmd.shell_menu import convert_path, install, uninstall, status


def test_txt_conversion_writes_markdown(tmp_path):
    source = tmp_path / "note.txt"
    source.write_text("第一行\n第二行\n", encoding="utf-8")

    result = convert_path(source)

    assert result["success"] == 1
    output = source.with_suffix(".md")
    assert output.is_file()
    assert "第一行" in output.read_text(encoding="utf-8")


def test_folder_mirror_rerun_and_user_edit_are_protected(tmp_path):
    folder = tmp_path / "docs"
    nested = folder / "nested"
    nested.mkdir(parents=True)
    (folder / "a.txt").write_text("a", encoding="utf-8")
    (nested / "b.txt").write_text("b", encoding="utf-8")
    (folder / "skip.bin").write_bytes(b"binary")

    first = convert_path(folder)
    mirror = tmp_path / "docs-md"
    assert first["success"] == 2
    assert (mirror / "a.md").is_file()
    assert (mirror / "nested" / "b.md").is_file()
    assert first["skipped"] == 1

    second = convert_path(folder)
    assert second["unchanged"] == 2

    edited = mirror / "a.md"
    edited.write_text("user edit", encoding="utf-8")
    third = convert_path(folder)
    assert third["conflict"] == 1
    assert edited.read_text(encoding="utf-8") == "user edit"
    assert list(mirror.glob("a.openmd-update*.md"))


def test_large_source_is_skipped_until_confirmed(tmp_path):
    source = tmp_path / "large.txt"
    source.write_text("large fixture", encoding="utf-8")

    blocked = convert_path(source, max_bytes=1)
    assert blocked["success"] == 0
    assert blocked["skipped_reasons"]["max_bytes_exceeded"] == 1

    confirmed = convert_path(source, max_bytes=1, confirm_large=True)
    assert confirmed["success"] == 1


def test_unsupported_single_file_is_counted_and_ignored(tmp_path):
    source = tmp_path / "archive.bin"
    source.write_bytes(b"binary")

    result = convert_path(source)

    assert result["success"] == 0
    assert result["skipped"] == 1
    assert result["skipped_reasons"]["unsupported_format"] == 1


def test_install_uninstall_roundtrip_uses_fake_home_and_registry(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("OPENMD_DATA_DIR", str(tmp_path / "data"))

    class Key:
        def __init__(self, store, path):
            self.store, self.path = store, path

    existing = r"Software\Classes\*\shell\OpenmdToMarkdown"
    existing_command = existing + r"\command"
    store = {
        existing: {"": "Previous menu label", "CustomValue": "keep"},
        existing_command: {"": "previous.exe %1", "CustomCommandValue": "keep"},
    }
    class FakeWinreg:
        HKEY_CURRENT_USER = object()
        REG_SZ = 1
        def CreateKey(self, root, path):
            store.setdefault(path, {})
            return Key(store, path)
        def SetValueEx(self, key, name, reserved, kind, value):
            key.store[key.path][name] = value
        def OpenKey(self, root, path):
            if path not in store:
                raise FileNotFoundError(path)
            return Key(store, path)
        def QueryValueEx(self, key, name):
            return key.store[key.path][name], self.REG_SZ
        def DeleteValue(self, key, name):
            key.store[key.path].pop(name, None)
        def DeleteKey(self, root, path):
            store.pop(path, None)
        def CloseKey(self, key):
            return None

    monkeypatch.setattr("openmd.shell_menu._winreg", FakeWinreg())
    installed = install(platform="windows")
    assert installed["installed"] is True
    assert status(platform="windows")["installed"] is True
    removed = uninstall(platform="windows")
    assert removed["removed"] is True
    assert status(platform="windows")["installed"] is False
    assert store[existing][""] == "Previous menu label"
    assert store[existing_command][""] == "previous.exe %1"
    assert store[existing]["CustomValue"] == "keep"
    assert store[existing_command]["CustomCommandValue"] == "keep"


@pytest.mark.parametrize("platform", ["macos", "linux"])
def test_unix_shell_menu_install_uninstall_roundtrip_uses_temporary_home(monkeypatch, tmp_path, platform):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))

    installed = install(platform=platform)
    assert installed["installed"] is True
    assert status(platform=platform)["installed"] is True
    removed = uninstall(platform=platform)
    assert removed["removed"] is True
    assert status(platform=platform)["installed"] is False
