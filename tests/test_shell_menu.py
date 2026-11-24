import json
from importlib import resources
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
        existing: {"": "Previous menu label", "AppliesTo": "previous filter", "CustomValue": "keep", "Flags": 7, "Blob": b"\x01\x02"},
        existing_command: {"": "previous.exe %1", "CustomCommandValue": "keep"},
    }
    kinds = {(existing, "Flags"): 4, (existing, "Blob"): 3}
    class FakeWinreg:
        HKEY_CURRENT_USER = object()
        REG_SZ = 1
        def CreateKey(self, root, path):
            store.setdefault(path, {})
            return Key(store, path)
        def SetValueEx(self, key, name, reserved, kind, value):
            key.store[key.path][name] = value
            kinds[(key.path, name)] = kind
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
            return name, value, kinds.get((key.path, name), self.REG_SZ)
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
    assert store[existing]["Flags"] == 7 and kinds[(existing, "Flags")] == 4
    assert store[existing]["Blob"] == b"\x01\x02" and kinds[(existing, "Blob")] == 3


def test_windows_install_adds_brand_icon_and_uninstall_removes_it(monkeypatch, tmp_path):
    from openmd import shell_menu

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("OPENMD_DATA_DIR", str(tmp_path / "data"))
    icon = tmp_path / "openmd.ico"
    icon.write_bytes(b"ico")
    monkeypatch.setattr(shell_menu, "_brand_icon_path", lambda: icon)

    class Key:
        def __init__(self, store, path):
            self.store, self.path = store, path

    store = {}
    kinds = {}

    class FakeWinreg:
        HKEY_CURRENT_USER = object()
        REG_SZ = 1

        def CreateKey(self, root, path):
            store.setdefault(path, {})
            return Key(store, path)

        def SetValueEx(self, key, name, reserved, kind, value):
            key.store[key.path][name] = value
            kinds[(key.path, name)] = kind

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
            return name, value, kinds.get((key.path, name), self.REG_SZ)

        def DeleteValue(self, key, name):
            key.store[key.path].pop(name, None)

        def DeleteKey(self, root, path):
            store.pop(path, None)

        def CloseKey(self, key):
            return None

    monkeypatch.setattr(shell_menu, "_winreg", FakeWinreg())
    installed = shell_menu.install(platform="windows")
    assert installed["installed"] is True
    for key_path, _label, _command in shell_menu._win_keys():
        assert store[key_path]["Icon"] == str(icon)
    shell_menu.uninstall(platform="windows")
    assert all("Icon" not in values for values in store.values())


def test_brand_icon_is_packaged_and_gui_copy_is_readable():
    icon = resources.files("openmd").joinpath("browser_extension", "icons", "openmd.ico")
    assert icon.is_file()
    assert icon.read_bytes()[:4] == b"\x00\x00\x01\x00"


def test_gui_copy_helpers_build_title_progress_and_failure_summary():
    from openmd import shell_menu

    assert shell_menu._gui_title() == "Openmd · 转成 Markdown"
    assert shell_menu._gui_summary({"success": 2, "unchanged": 1, "conflict": 0, "skipped": 3, "failed": 1}) == (
        "成功 2 · 未变化 1 · 保留编辑 0 · 跳过 3 · 失败 1"
    )
    assert shell_menu._gui_failure_lines([
        {"path": "/tmp/a.pdf", "error": "document_empty_text"},
        {"path": "/tmp/b.pdf", "error": "PermissionError: denied"},
    ]) == ["a.pdf：文档中没有可转换的文本（document_empty_text）", "b.pdf：没有读取或写入该文件的权限（PermissionError）"]
    assert shell_menu._gui_failure_lines([
        {"path": r"C:\Users\private\meeting.mp4", "error": "asr_runtime_unavailable",
         "next_action": "openmd doctor --profile media"},
        {"path": r"C:\Users\private\missing.mp3", "error": "asr_model_missing",
         "next_action": "openmd models install asr:baseline"},
    ]) == [
        "meeting.mp4：本地语音运行环境未就绪（asr_runtime_unavailable）；下一步：openmd doctor --profile media",
        "missing.mp3：缺少本地语音模型（asr_model_missing）；下一步：openmd models install asr:baseline",
    ]
    assert shell_menu._gui_failure_lines([
        {"path": r"C:\Users\private\meeting.mp4", "error": "asr_runtime_unavailable",
         "next_action": r"openmd doctor --profile media C:\Users\private\secret"},
    ]) == ["meeting.mp4：本地语音运行环境未就绪（asr_runtime_unavailable）"]


def test_warm_gui_copy_helpers_match_selected_mascot_variant():
    from openmd import shell_menu

    assert shell_menu._gui_progress_heading(7, 12) == "仓鼠正在搬运 7 / 12"
    assert shell_menu._gui_progress_filename("a" * 80 + ".pdf") == "a" * 38 + "…pdf"
    value = {"success": 9, "unchanged": 1, "conflict": 1, "skipped": 0, "failed": 1}
    assert shell_menu._gui_completion_heading(value) == "搬完啦，有 1 个没搬动"
    assert shell_menu._gui_result_chips(value) == [
        ("成功 9", False), ("未变化 1", False), ("保留你的编辑 1", False), ("失败 1", True),
    ]
    # Zero counts other than success are hidden, as in the user-picked preview.
    assert shell_menu._gui_result_chips({"success": 0}) == [("成功 0", False)]
    assert shell_menu._gui_completion_subtitle(Path("E:/docs/input.pdf"), None) == "已放在原文件旁边"
    assert shell_menu._gui_completion_subtitle(Path("E:/docs/input"), Path("E:/docs/input-md")) == "已放到「input-md」文件夹"


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
    assert "先用 `uv tool install openmd-cli`（或 `pipx install openmd-cli`）做持久安装，再运行 shell-menu install" == result["next_action"]


def test_uv_cache_install_rejection_returns_nonzero_cli_exit(monkeypatch, tmp_path, capsys):
    from argparse import Namespace
    from openmd import shell_menu

    uv_cache = tmp_path / ".cache" / "uv" / "archive-v0" / "bin" / "openmd.exe"
    uv_cache.parent.mkdir(parents=True)
    uv_cache.touch()
    monkeypatch.setattr(shell_menu.shutil, "which", lambda name: str(uv_cache) if name == "openmd" else None)

    exit_code = shell_menu.cmd_shell_menu(Namespace(shell_menu_action="install", platform="windows"))

    assert exit_code == 1
    assert "uv tool install openmd-cli" in json.loads(capsys.readouterr().out)["next_action"]


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


@pytest.mark.parametrize("platform", ["macos", "linux"])
def test_unix_shell_menu_never_overwrites_or_deletes_a_foreign_same_name_item(monkeypatch, tmp_path, platform):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    if platform == "macos":
        foreign = tmp_path / "Library" / "Services" / "用 Openmd 转成 Markdown.workflow" / "Contents" / "document.wflow"
    else:
        foreign = tmp_path / ".local" / "share" / "nautilus" / "scripts" / "用 Openmd 转成 Markdown"
    foreign.parent.mkdir(parents=True)
    foreign.write_text("user's own item", encoding="utf-8")

    installed = install(platform=platform)
    assert installed["installed"] is False and installed["error"] == "menu_name_taken"
    assert status(platform=platform)["installed"] is False
    removed = uninstall(platform=platform)
    assert removed["removed"] is False and removed["error"] == "menu_not_owned"
    assert foreign.read_text(encoding="utf-8") == "user's own item"


def test_right_click_scanned_pdf_uses_shared_first_use_confirmation(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from openmd import shell_menu
    from openmd.core.models import TranscriptResult

    source = tmp_path / "scan.pdf"
    source.write_bytes(b"frozen scan marker")
    monkeypatch.setattr("openmd.engines.registry.convert_document", lambda *_args, **_kwargs: TranscriptResult(
        text="", status="failed", source_level="native_text", backend="openmd.pdf",
        title="scan", error_code="document_empty_text"))
    seen = []

    class Provider:
        def __init__(self, _name, _options):
            pass

        def extract(self, request, *, first_use_confirm=None):
            seen.append((request.item.platform, bool(first_use_confirm), first_use_confirm("model size and path")))
            return TranscriptResult(text="recognized scan", status="verified", source_level="ocr",
                                    backend="ocr.chain", title="scan")

    monkeypatch.setattr("openmd.backends.ocr.OCRRouterProvider", Provider)
    target = tmp_path / "scan.md"
    result = shell_menu._convert_file(source, target, ocr_confirm=lambda question: question.startswith("model size"))

    assert result["status"] == "verified"
    assert seen == [("pdf", True, True)]
    assert target.read_text(encoding="utf-8") == "recognized scan"


def test_right_click_scanned_image_uses_shared_first_use_confirmation(monkeypatch, tmp_path):
    from openmd import shell_menu
    from openmd.core.models import TranscriptResult
    from openmd.ocr.first_use import first_use_question
    from PIL import Image

    source = tmp_path / "scan.png"
    Image.new("RGB", (32, 48), "white").save(source)
    assert source in shell_menu._candidate_files(source)
    prompts = []
    results = [
        TranscriptResult(text="", status="failed", source_level="ocr", backend="ocr.chain",
                         title="scan", error_code="ollama_model_missing"),
        TranscriptResult(text="recognized image", status="verified", source_level="ocr",
                         backend="ocr.chain", title="scan"),
    ]
    monkeypatch.setattr("openmd.ocr.pipeline.LocalOCRBackend.recognize",
                        lambda *_args, **_kwargs: results.pop(0))
    monkeypatch.setattr("openmd.runtime.recommendations.cmd_models_install", lambda _args: 0)
    monkeypatch.setattr("openmd.runtime.recommendations.install_plan",
                        lambda *_args, **_kwargs: {"estimated_size_bytes": 3, "root": str(tmp_path), "runtime": None})
    monkeypatch.setattr("openmd.engines.registry.convert_document",
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("image must use OCR")))
    def confirm(question):
        prompts.append(question)
        return True

    # Run through the real provider used by the menu; OCRRouterProvider must
    # pass the same first-use question/install path used for CLI image OCR.
    target = tmp_path / "scan.md"
    converted = shell_menu._convert_file(source, target, ocr_confirm=confirm)

    assert converted["status"] == "verified"
    assert target.read_text(encoding="utf-8") == "recognized image"
    assert prompts == [first_use_question({"estimated_size_bytes": 3, "root": str(tmp_path), "runtime": None})]


def test_right_click_noninteractive_scan_returns_shared_install_command(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from openmd import shell_menu
    from openmd.core.models import TranscriptResult

    source = tmp_path / "scan.pdf"
    source.write_bytes(b"frozen scan marker")
    monkeypatch.setattr("openmd.engines.registry.convert_document", lambda *_args, **_kwargs: TranscriptResult(
        text="", status="failed", source_level="native_text", backend="openmd.pdf",
        title="scan", error_code="document_empty_text"))

    class Provider:
        def __init__(self, _name, _options):
            pass

        def extract(self, request, *, first_use_confirm=None):
            return TranscriptResult(text="", status="failed", source_level="ocr", backend="ocr.chain",
                                    title="scan", error_code="ollama_model_missing")

    monkeypatch.setattr("openmd.backends.ocr.OCRRouterProvider", Provider)
    result = shell_menu._convert_file(source, tmp_path / "scan.md")

    assert result["status"] == "failed"
    assert result["error"] == "ocr_install_required"
    assert result["next_action"] == "openmd models install ocr:chain"


def test_right_click_media_uses_the_shared_go_processing_entrypoint(monkeypatch, tmp_path):
    from openmd import shell_menu
    from openmd.cli import CliResult

    source = tmp_path / "meeting.mp4"
    source.write_bytes(b"fixture media")
    output = tmp_path / "meeting.md"
    prompt = lambda question: True
    seen = []

    def process_input(url, args, *, command):
        seen.append((url, args, command))
        assert args.enable_asr is True
        assert args.first_use_confirm is prompt
        rendered = Path(args.out) / "generic_media" / "meeting.md"
        rendered.parent.mkdir(parents=True)
        rendered.write_text("**00:00:00**\n\n第一行 第二行", encoding="utf-8")
        return CliResult(ok=True, command="go", status="verified", outputs=[str(rendered)])

    monkeypatch.setattr("openmd.cli._process_input", process_input)
    monkeypatch.setattr("openmd.adapters.local_audio.adapter.LocalAudioAdapter",
                        lambda *a, **k: pytest.fail("shell menu must use the go processing path"))
    result = shell_menu._convert_file(source, output, asr_confirm=prompt)

    assert {".mp3", ".mp4", ".ogg", ".opus", ".aac"}.issubset(shell_menu.SUPPORTED_SUFFIXES)
    assert result == {"status": "verified", "error": None}
    assert output.read_text(encoding="utf-8") == "**00:00:00**\n\n第一行 第二行"
    assert len(seen) == 1
    assert seen[0][0] == source.as_uri()
    assert seen[0][2] == "go"


@pytest.mark.parametrize(
    ("error_code", "expected"),
    [
        ("asr_model_missing", {"status": "failed", "error": "asr_install_required",
                                "next_action": "openmd models install asr:baseline"}),
        ("asr_runtime_unavailable", {"status": "failed", "error": "asr_runtime_unavailable",
                                      "next_action": "openmd doctor --profile media"}),
        ("asr_insufficient_memory", {"status": "failed", "error": "asr_insufficient_memory"}),
    ],
)
def test_right_click_media_maps_shared_go_failures(monkeypatch, tmp_path, error_code, expected):
    from openmd import shell_menu
    from openmd.cli import CliResult

    source = tmp_path / "meeting.mp3"
    source.write_bytes(b"fixture media")
    monkeypatch.setattr("openmd.cli._process_input", lambda *a, **k: CliResult(
        ok=False, command="go", status="failed", error_code=error_code,
    ))

    result = shell_menu._convert_file(source, tmp_path / "meeting.md")
    assert result == expected


def test_gui_failure_line_explains_asr_memory_shortage():
    from openmd import shell_menu

    assert shell_menu._gui_failure_lines([
        {"path": "meeting.mp4", "error": "asr_insufficient_memory"},
    ]) == ["meeting.mp4：可用内存不足，请关闭其他程序后重试（asr_insufficient_memory）"]
