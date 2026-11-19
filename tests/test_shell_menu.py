import json
from pathlib import Path

import pytest

from openmd.shell_menu import SUPPORTED_SUFFIXES, convert_path, install, uninstall, status


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


def test_same_stem_different_formats_keep_distinguishable_names_and_manifest_paths(tmp_path):
    folder = tmp_path / "docs"
    folder.mkdir()
    (folder / "sample.txt").write_text("text format", encoding="utf-8")
    (folder / "sample.html").write_text("<html><body>html format</body></html>", encoding="utf-8")

    first = convert_path(folder)
    mirror = tmp_path / "docs-md"
    assert first["success"] == 2
    assert (mirror / "sample.txt.md").is_file()
    assert (mirror / "sample.html.md").is_file()

    second = convert_path(folder)
    assert second["unchanged"] == 2
    assert set(second["outputs"]) == {str(mirror / "sample.txt.md"), str(mirror / "sample.html.md")}

    manifest_path = mirror / ".openmd" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    old_names = {"sample.html.md": "sample.md", "sample.txt.md": "sample-2.md"}
    for entry in manifest["entries"].values():
        previous_name = entry["path"]
        legacy_name = old_names[previous_name]
        (mirror / previous_name).rename(mirror / legacy_name)
        entry["path"] = legacy_name
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    migrated = convert_path(folder)
    assert set(migrated["outputs"]) == {str(mirror / "sample.txt.md"), str(mirror / "sample.html.md")}
    assert (mirror / "sample.txt.md").is_file()
    assert (mirror / "sample.html.md").is_file()


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
        existing: {"": "Previous menu label", "AppliesTo": "previous filter", "CustomValue": "keep"},
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
        def EnumValue(self, key, index):
            try:
                name, value = list(key.store[key.path].items())[index]
            except IndexError as exc:
                raise OSError("no more registry values") from exc
            return name, value, self.REG_SZ
        def DeleteValue(self, key, name):
            key.store[key.path].pop(name, None)
        def DeleteKey(self, root, path):
            store.pop(path, None)
        def CloseKey(self, key):
            return None

    monkeypatch.setattr("openmd.shell_menu._winreg", FakeWinreg())
    installed = install(platform="windows")
    assert installed["installed"] is True
    applies_to = store[existing]["AppliesTo"]
    assert applies_to.startswith("System.FileExtension:=")
    assert all(suffix in applies_to for suffix in SUPPORTED_SUFFIXES)
    assert "System.FileExtension:=" not in store[r"Software\Classes\Directory\shell\OpenmdToMarkdown"]
    assert status(platform="windows")["installed"] is True
    removed = uninstall(platform="windows")
    assert removed["removed"] is True
    assert status(platform="windows")["installed"] is False
    assert store[existing][""] == "Previous menu label"
    assert store[existing]["AppliesTo"] == "previous filter"
    assert store[existing_command][""] == "previous.exe %1"
    assert store[existing]["CustomValue"] == "keep"
    assert store[existing_command]["CustomCommandValue"] == "keep"


def test_windows_shell_command_uses_pythonw_when_available_and_falls_back(monkeypatch, tmp_path):
    from openmd import shell_menu

    interpreter = tmp_path / "python.exe"
    windowless = tmp_path / "pythonw.exe"
    interpreter.touch()
    windowless.touch()
    monkeypatch.setattr(shell_menu.sys, "platform", "win32")
    monkeypatch.setattr(shell_menu.sys, "executable", str(interpreter))
    monkeypatch.setattr(shell_menu.shutil, "which", lambda name: "C:/tools/openmd.exe" if name == "openmd" else None)

    assert shell_menu._command() == f'"{windowless}" -m openmd'

    windowless.unlink()
    assert shell_menu._command() == '"C:/tools/openmd.exe"'


def test_windows_install_rejects_uv_temporary_cli_with_next_action(monkeypatch, tmp_path):
    from openmd import shell_menu

    uv_cache = tmp_path / ".cache" / "uv" / "archive-v0" / "bin" / "openmd.exe"
    uv_cache.parent.mkdir(parents=True)
    uv_cache.touch()
    monkeypatch.setattr(shell_menu.shutil, "which", lambda name: str(uv_cache) if name == "openmd" else None)

    result = install(platform="windows")

    assert result["installed"] is False
    assert "先用 `uv tool install openmd`（或 pipx）做持久安装，再运行 shell-menu install" == result["next_action"]


def test_uv_cache_install_rejection_returns_nonzero_cli_exit(monkeypatch, tmp_path, capsys):
    from argparse import Namespace
    from openmd import shell_menu

    uv_cache = tmp_path / ".cache" / "uv" / "archive-v0" / "bin" / "openmd.exe"
    uv_cache.parent.mkdir(parents=True)
    uv_cache.touch()
    monkeypatch.setattr(shell_menu.shutil, "which", lambda name: str(uv_cache) if name == "openmd" else None)

    exit_code = shell_menu.cmd_shell_menu(Namespace(shell_menu_action="install", platform="windows"))

    assert exit_code == 1
    assert "uv tool install openmd" in json.loads(capsys.readouterr().out)["next_action"]


def test_file_menu_label_and_filter_are_localized_and_generated(monkeypatch):
    from openmd import shell_menu

    monkeypatch.setattr(shell_menu, "_command", lambda: '"C:/Python/pythonw.exe" -m openmd')
    file_key, label, _ = shell_menu._win_keys()[0]

    assert file_key == r"Software\Classes\*\shell\OpenmdToMarkdown"
    assert label == "用 Openmd 转成 Markdown"
    assert shell_menu._applies_to_filter() == " OR ".join(
        f"System.FileExtension:={suffix}" for suffix in sorted(SUPPORTED_SUFFIXES)
    )


def test_error_labels_are_chinese_and_hide_manifest_directory_is_best_effort(monkeypatch, tmp_path):
    from openmd import shell_menu

    assert shell_menu._failure_message("document_empty_text") == "文档中没有可转换的文本（document_empty_text）"
    assert shell_menu._failure_message("SomeError: details") == "转换失败（SomeError）"
    assert shell_menu._failure_message("PermissionError: access denied") == "没有读取或写入该文件的权限（PermissionError）"
    manifest_dir = tmp_path / ".openmd"
    manifest_dir.mkdir()
    calls = []

    def fail_to_hide(path):
        calls.append(path)
        raise OSError("access denied")

    monkeypatch.setattr(shell_menu, "_set_windows_hidden_attribute", fail_to_hide)
    monkeypatch.setattr(shell_menu.sys, "platform", "win32")
    source = tmp_path / "note.txt"
    source.write_text("conversion continues", encoding="utf-8")

    result = convert_path(source)

    assert result["success"] == 1
    assert source.with_suffix(".md").is_file()
    assert calls == [manifest_dir]


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
