"""Cross platform file-manager integration for local document conversion.

Installers only touch the current user's integration points. Conversion itself
uses the existing local document engine chain and a SyncManifest per output
tree, so a rerun never overwrites a user edited Markdown file.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import sys
from collections import Counter
from pathlib import Path
from typing import Any
from collections.abc import Callable

try:  # imported lazily in tests and absent on non-Windows hosts
    import winreg as _winreg
except ImportError:  # pragma: no cover - platform dependent
    _winreg = None

from openmd.core.detector import DOCUMENT_EXTENSIONS
from openmd.core.filename import safe_filename
from openmd.core.models import ContentItem
from openmd.core.sync_manifest import SyncManifest, source_id_for


SUPPORTED_SUFFIXES = frozenset(DOCUMENT_EXTENSIONS)
DEFAULT_MAX_FILES = 1000
DEFAULT_MAX_BYTES = 100 * 1024 * 1024
_DATA_URI = re.compile(r"data:image/([a-zA-Z0-9.+-]+);base64,([A-Za-z0-9+/=\r\n]+)")


def _hash_source(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _item(path: Path) -> ContentItem:
    absolute = path.resolve()
    identity = hashlib.sha256(str(absolute).encode("utf-8")).hexdigest()[:24]
    return ContentItem(
        platform="pdf",
        input_type="single",
        url=absolute.as_uri(),
        id=identity,
        title=path.stem,
        source_type="document",
        raw_meta={"local_path": str(absolute), "profile": "document", "permission_scope": "local_file"},
    )


def _materialize_images(text: str, destination: Path) -> str:
    matches = list(_DATA_URI.finditer(text))
    if not matches:
        return text
    image_dir = destination.with_name(destination.stem + ".assets") / "images"
    image_dir.mkdir(parents=True, exist_ok=True)
    replacements: dict[str, str] = {}
    for index, match in enumerate(matches, 1):
        token = match.group(0)
        if token in replacements:
            continue
        extension = "jpg" if match.group(1).lower() in {"jpeg", "jpg"} else match.group(1).lower().split("+", 1)[0]
        try:
            data = base64.b64decode(match.group(2), validate=True)
        except (ValueError, base64.binascii.Error):
            continue
        name = f"img_{index:03d}.{extension or 'bin'}"
        (image_dir / name).write_bytes(data)
        replacements[token] = f"{image_dir.name}/images/{name}"
    for old, new in replacements.items():
        text = text.replace(old, new)
    return text


def _convert_file(path: Path, output: Path) -> dict[str, Any]:
    from openmd.engines.registry import convert_document
    from openmd.engines.base import EngineLimits

    if path.suffix.lower() == ".md":
        text = path.read_text(encoding="utf-8-sig")
        status, error = ("verified", None) if text.strip() else ("failed", "document_empty_text")
    else:
        result = convert_document(path, EngineLimits(max_file_bytes=max(DEFAULT_MAX_BYTES, path.stat().st_size)))
        text, status, error = result.text, result.status, result.error_code
    if status not in {"verified", "partial"} or not text.strip():
        return {"status": "failed", "error": error or "document_empty_text"}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(_materialize_images(text, output), encoding="utf-8", newline="\n")
    return {"status": status, "error": None}


def _candidate_files(root: Path) -> list[Path]:
    if root.is_file():
        return [root] if root.suffix.lower() in SUPPORTED_SUFFIXES else []
    return sorted(p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in SUPPORTED_SUFFIXES)


def _output_relative(path: Path, source: Path, is_folder: bool, stem_counts: Counter) -> str:
    parent = path.parent.relative_to(source) if is_folder else Path()
    name = f"{path.name}.md" if stem_counts[(path.parent, path.stem.casefold())] > 1 else f"{path.stem}.md"
    return (parent / name).as_posix()


def convert_path(
    input_path: str | Path,
    *,
    out: str | Path | None = None,
    max_files: int = DEFAULT_MAX_FILES,
    max_bytes: int = DEFAULT_MAX_BYTES,
    confirm_large: bool = False,
    progress: Callable[[int, int, Path], None] | None = None,
) -> dict[str, Any]:
    """Convert one supported file or a folder into Markdown.

    Folders are mirrored beside the source as ``<name>-md``. Unsupported files
    are counted, and one conversion failure does not stop the remaining files.
    """
    source = Path(input_path).expanduser().resolve()
    if not source.exists():
        return {"ok": False, "error": "input_missing", "success": 0, "failed": 1, "skipped": 0, "unchanged": 0, "conflict": 0}
    is_folder = source.is_dir()
    destination = Path(out).expanduser().resolve() if out else (
        source.parent / f"{safe_filename(source.name, max_length=120)}-md" if is_folder else source.parent
    )
    files = _candidate_files(source)
    stem_counts = Counter((path.parent, path.stem.casefold()) for path in files)
    all_files = [p for p in (source.rglob("*") if is_folder else [source]) if p.is_file()]
    unsupported = sum(1 for p in all_files if p.suffix.lower() not in SUPPORTED_SUFFIXES)
    result: dict[str, Any] = {"ok": True, "success": 0, "failed": 0, "skipped": unsupported, "unchanged": 0, "conflict": 0, "outputs": [], "failures": [], "skipped_reasons": {"unsupported_format": unsupported}}
    if len(files) > max_files and not confirm_large:
        result.update({"ok": False, "error": "max_files_exceeded", "skipped": unsupported + len(files)})
        result["skipped_reasons"]["max_files_exceeded"] = len(files)
        return result
    manifest_root = destination
    manifest = SyncManifest(manifest_root)
    for index, path in enumerate(files, 1):
        try:
            size = path.stat().st_size
        except OSError as exc:
            result["failed"] += 1
            result["failures"].append({"path": str(path), "error": type(exc).__name__ + ": " + str(exc)})
            if progress:
                progress(index, len(files), path)
            continue
        if size > max_bytes and not confirm_large:
            result["skipped"] += 1
            result["skipped_reasons"]["max_bytes_exceeded"] = result["skipped_reasons"].get("max_bytes_exceeded", 0) + 1
            if progress:
                progress(index, len(files), path)
            continue
        relative = _output_relative(path, source, is_folder, stem_counts)
        target = manifest_root / relative
        try:
            item = _item(path)
            source_id = source_id_for(item)
            content_identity = _hash_source(path)
            previous = manifest.read(source_id)
            previous_target = manifest._target(previous["path"]) if previous else None
            if (previous and previous.get("path") == relative
                    and previous.get("content_identity") == content_identity and previous_target
                    and previous_target.is_file()
                    and hashlib.sha256(previous_target.read_bytes()).hexdigest() == previous.get("content_sha256")):
                result["success"] += 1
                result["unchanged"] += 1
                result["outputs"].append(str(manifest._target(previous["path"])))
                if progress:
                    progress(index, len(files), path)
                continue
            staging = target.with_name(f".{target.name}.staging")
            staging.parent.mkdir(parents=True, exist_ok=True)
            converted = _convert_file(path, staging)
            if converted["status"] == "failed":
                result["failed"] += 1
                result["failures"].append({"path": str(path), "error": converted["error"]})
                staging.unlink(missing_ok=True)
                if progress:
                    progress(index, len(files), path)
                continue
            content = staging.read_text(encoding="utf-8")
            synced = manifest.write(source_id, relative, content, content_identity=content_identity)
            staging.unlink(missing_ok=True)
            if not is_folder:
                _hide_manifest_directory(manifest.path.parent)
            result["success"] += 1
            result["outputs"].append(str(synced.path))
            if synced.operation == "unchanged":
                result["unchanged"] += 1
            elif synced.operation == "conflict_kept_user_edit":
                result["conflict"] += 1
        except Exception as exc:  # one bad input must not stop a folder run
            result["failed"] += 1
            result["failures"].append({"path": str(path), "error": type(exc).__name__ + ": " + str(exc)})
        if progress:
            progress(index, len(files), path)
    result["ok"] = result["failed"] == 0 and not result.get("error")
    return result


def _platform_name(platform: str | None = None) -> str:
    if platform:
        return platform.lower()
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def _command() -> str:
    if sys.platform.startswith("win"):
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        if pythonw.is_file():
            return f'"{pythonw}" -m openmd'
    executable = shutil.which("openmd") or "openmd"
    return f'"{executable}"' if sys.platform.startswith("win") else executable


def _applies_to_filter() -> str:
    return " OR ".join(f"System.FileExtension:={suffix}" for suffix in sorted(SUPPORTED_SUFFIXES))


def _is_uv_cache_executable(executable: str | None = None) -> bool:
    path = Path(executable or shutil.which("openmd") or "openmd")
    parts = [part.casefold() for part in path.parts]
    return any(parts[index:index + 2] == ["uv", "cache"] for index in range(len(parts) - 1)) or any(
        parts[index:index + 2] == [".cache", "uv"] for index in range(len(parts) - 1)
    )


def _set_windows_hidden_attribute(path: Path) -> None:
    import ctypes

    get_attributes = ctypes.windll.kernel32.GetFileAttributesW
    set_attributes = ctypes.windll.kernel32.SetFileAttributesW
    get_attributes.argtypes = [ctypes.c_wchar_p]
    get_attributes.restype = ctypes.c_uint32
    set_attributes.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32]
    set_attributes.restype = ctypes.c_int
    attributes = get_attributes(str(path))
    if attributes != 0xFFFFFFFF and not attributes & 0x2:
        set_attributes(str(path), attributes | 0x2)


def _hide_manifest_directory(path: Path) -> None:
    if not sys.platform.startswith("win") or not path.is_dir():
        return
    try:
        _set_windows_hidden_attribute(path)
    except Exception:
        # Metadata visibility must never make a successful conversion fail.
        pass


def _failure_message(error: str) -> str:
    messages = {
        "document_empty_text": "文档中没有可转换的文本",
        "engine_unavailable": "缺少此格式所需的转换工具",
        "file_too_large": "文件超过可处理大小",
        "permission_denied": "没有读取该文件的权限",
        "input_missing": "找不到输入文件",
        "FileNotFoundError": "找不到输入文件或所需转换工具",
        "PermissionError": "没有读取或写入该文件的权限",
        "IsADirectoryError": "当前输入要求文件，但收到的是文件夹",
        "UnicodeDecodeError": "文本编码无法识别",
        "ValueError": "文件内容或格式无效",
        "TimeoutError": "转换等待超时",
        "OSError": "读取或写入文件时发生错误",
        "RuntimeError": "转换工具运行失败",
    }
    code = error.split(":", 1)[0].strip()
    if code in messages:
        return f"{messages[code]}（{code}）"
    if code and code.replace("_", "").isalnum() and code[0].isalpha():
        return f"转换失败（{code}）"
    return "转换失败"


def _win_keys() -> list[tuple[str, str, str]]:
    command = f'{_command()} shell-menu convert --gui "%1"'
    return [
        (r"Software\Classes\*\shell\OpenmdToMarkdown", "用 Openmd 转成 Markdown", command),
        (r"Software\Classes\Directory\shell\OpenmdToMarkdown", "用 Openmd 转成 Markdown", command),
        (r"Software\Classes\Directory\Background\shell\OpenmdToMarkdown", "用 Openmd 转成 Markdown", f'{_command()} shell-menu convert --gui "%V"'),
    ]


def _backup_path() -> Path:
    return Path(os.environ.get("OPENMD_DATA_DIR") or (Path.home() / ".openmd")) / "shell-menu-backup.json"


def install(*, platform: str | None = None) -> dict[str, Any]:
    platform = _platform_name(platform)
    if platform == "windows":
        executable = shutil.which("openmd")
        if _is_uv_cache_executable(executable):
            return {
                "installed": False,
                "platform": platform,
                "error": "temporary_uv_install",
                "next_action": "先用 `uv tool install openmd`（或 pipx）做持久安装，再运行 shell-menu install",
            }
        if _winreg is None:
            raise RuntimeError("windows_registry_unavailable")
        backup: dict[str, Any] = {}
        for key_path, label, command in _win_keys():
            backup[key_path] = {"existed": _registry_exists(key_path), "values": _registry_values(key_path)}
            command_path = key_path + r"\command"
            backup[command_path] = {"existed": _registry_exists(command_path), "values": _registry_values(command_path)}
            key = _winreg.CreateKey(_winreg.HKEY_CURRENT_USER, key_path)
            _winreg.SetValueEx(key, "", 0, _winreg.REG_SZ, label)
            if key_path == r"Software\Classes\*\shell\OpenmdToMarkdown":
                _winreg.SetValueEx(key, "AppliesTo", 0, _winreg.REG_SZ, _applies_to_filter())
            _winreg.CloseKey(key)
            command_key = _winreg.CreateKey(_winreg.HKEY_CURRENT_USER, key_path + r"\command")
            _winreg.SetValueEx(command_key, "", 0, _winreg.REG_SZ, command)
            _winreg.CloseKey(command_key)
        _backup_path().parent.mkdir(parents=True, exist_ok=True)
        _backup_path().write_text(json.dumps(backup, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"installed": True, "platform": platform, "scope": "HKCU", "menu_visibility": "Windows 11 may place legacy entries under Show more options."}
    if platform == "macos":
        root = Path.home() / "Library" / "Services" / "用 Openmd 转成 Markdown.workflow" / "Contents"
        root.mkdir(parents=True, exist_ok=True)
        (root / "document.wflow").write_text(_automator_workflow(), encoding="utf-8")
        (root / "Info.plist").write_text('''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict><key>CFBundleIdentifier</key><string>dev.openmd.shell-menu</string><key>CFBundleName</key><string>用 Openmd 转成 Markdown</string></dict></plist>
''', encoding="utf-8")
        return {"installed": True, "platform": platform, "path": str(root.parent)}
    script = Path.home() / ".local" / "share" / "nautilus" / "scripts" / "用 Openmd 转成 Markdown"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(
        f'#!/bin/sh\ntarget="${{1:-$(printf \'%s\\n\' "$NAUTILUS_SCRIPT_SELECTED_FILE_PATHS" | sed -n \'1p\')}}"\n'
        f'exec {shlex.quote(_command())} shell-menu convert --gui "$target"\n',
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return {"installed": True, "platform": "linux", "path": str(script)}


def uninstall(*, platform: str | None = None) -> dict[str, Any]:
    platform = _platform_name(platform)
    if platform == "windows":
        if _winreg is None:
            raise RuntimeError("windows_registry_unavailable")
        try:
            backup = json.loads(_backup_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            backup = {}
        for key_path, _, _ in reversed(_win_keys()):
            for path in (key_path + r"\command", key_path):
                prior = backup.get(path)
                if isinstance(prior, dict) and prior.get("existed"):
                    _restore_registry_key(path, prior.get("values", {}))
                else:
                    _remove_owned_registry_key(path)
        if _backup_path().exists():
            _backup_path().unlink()
        return {"removed": True, "platform": platform}
    if platform == "macos":
        path = Path.home() / "Library" / "Services" / "用 Openmd 转成 Markdown.workflow"
        shutil.rmtree(path, ignore_errors=True)
        return {"removed": True, "platform": platform}
    path = Path.home() / ".local" / "share" / "nautilus" / "scripts" / "用 Openmd 转成 Markdown"
    path.unlink(missing_ok=True)
    return {"removed": True, "platform": platform}


def status(*, platform: str | None = None) -> dict[str, Any]:
    platform = _platform_name(platform)
    if platform == "windows":
        installed = bool(_winreg is not None and all(_registry_exists(path) for path, _, _ in _win_keys()))
    elif platform == "macos":
        installed = (Path.home() / "Library" / "Services" / "用 Openmd 转成 Markdown.workflow").exists()
    else:
        installed = (Path.home() / ".local" / "share" / "nautilus" / "scripts" / "用 Openmd 转成 Markdown").exists()
    return {"installed": installed, "platform": platform}


def _registry_exists(path: str) -> bool:
    try:
        key = _winreg.OpenKey(_winreg.HKEY_CURRENT_USER, path)
        _winreg.CloseKey(key)
        return True
    except OSError:
        return False


def _registry_values(path: str) -> dict[str, str]:
    try:
        key = _winreg.OpenKey(_winreg.HKEY_CURRENT_USER, path)
        try:
            values: dict[str, str] = {}
            index = 0
            while True:
                try:
                    name, value, _kind = _winreg.EnumValue(key, index)
                except OSError:
                    break
                if isinstance(value, str):
                    values[name] = value
                index += 1
            return values
        finally:
            _winreg.CloseKey(key)
    except OSError:
        return {}


def _restore_registry_key(path: str, values: dict[str, str]) -> None:
    key = _winreg.CreateKey(_winreg.HKEY_CURRENT_USER, path)
    try:
        index = 0
        names: list[str] = []
        while True:
            try:
                name, _value, _kind = _winreg.EnumValue(key, index)
            except OSError:
                break
            names.append(name)
            index += 1
        for name in names:
            try:
                _winreg.DeleteValue(key, name)
            except OSError:
                pass
        for name, value in values.items():
            _winreg.SetValueEx(key, name, 0, _winreg.REG_SZ, value)
    finally:
        _winreg.CloseKey(key)


def _remove_owned_registry_key(path: str) -> None:
    try:
        key = _winreg.OpenKey(_winreg.HKEY_CURRENT_USER, path)
    except OSError:
        return
    try:
        for name in ("", "AppliesTo"):
            try:
                _winreg.DeleteValue(key, name)
            except OSError:
                pass
    finally:
        _winreg.CloseKey(key)
    try:
        _winreg.DeleteKey(_winreg.HKEY_CURRENT_USER, path)
    except OSError:
        # Preserve children/values another application or user added later.
        pass


def cmd_shell_menu(args) -> int:
    action = args.shell_menu_action
    if action == "convert":
        gui = bool(getattr(args, "gui", False))
        window = None
        progress = None
        if gui:
            try:
                import tkinter as tk
                from tkinter import messagebox
                window = tk.Tk()
                window.title("用 Openmd 转成 Markdown")
                label = tk.Label(window, text="正在准备本地转换…", padx=24, pady=18)
                label.pack()
                window.update()
                progress = lambda index, total, path: (label.config(text=f"正在转换 {index}/{total}：{path.name}"), window.update_idletasks())
            except Exception:
                window = None
                progress = None
        confirm_large = args.confirm_large
        if window is not None and not confirm_large:
            root_path = Path(args.input).expanduser()
            candidates = _candidate_files(root_path) if root_path.exists() else []
            too_large = len(candidates) > args.max_files or any(p.stat().st_size > args.max_bytes for p in candidates)
            if too_large:
                confirm_large = messagebox.askyesno("输入规模较大", f"输入超过默认限制（{args.max_files} 个文件 / 每个文件 {args.max_bytes // (1024 * 1024)} MiB）。仍要继续吗？", parent=window)
        value = convert_path(args.input, out=args.out, max_files=args.max_files, max_bytes=args.max_bytes, confirm_large=confirm_large, progress=progress)
        if window is not None:
            failures = value.get("failures", [])
            summary = f"成功 {value.get('success', 0)} · 未变化 {value.get('unchanged', 0)} · 保留编辑 {value.get('conflict', 0)} · 跳过 {value.get('skipped', 0)} · 失败 {value.get('failed', 0)}"
            label.config(text=summary)
            if failures:
                reasons = "\n".join(f"{Path(row['path']).name}：{_failure_message(str(row['error']))}" for row in failures[:8])
                messagebox.showwarning("转换完成，但有文件失败", summary + "\n\n" + reasons + "\n\n扫描版 PDF 请安装并启用本机 OCR 后重试。", parent=window)
            window.update_idletasks()
            window.after(8000, window.destroy)
            window.mainloop()
            return 0 if value.get("ok") else 1
        if getattr(args, "json", False):
            print(json.dumps(value, ensure_ascii=True, sort_keys=True))
        else:
            print(f"转换结果：成功 {value.get('success', 0)}，未变化 {value.get('unchanged', 0)}，保留编辑 {value.get('conflict', 0)}，跳过 {value.get('skipped', 0)}，失败 {value.get('failed', 0)}")
            for row in value.get("failures", []):
                print(f"失败：{row['path']}：{_failure_message(str(row['error']))}")
        return 0 if value.get("ok") else 1
    value = {"install": install, "uninstall": uninstall, "status": status}[action](platform=args.platform)
    print(json.dumps(value, ensure_ascii=True, sort_keys=True))
    return 1 if action == "install" and value.get("installed") is False else 0


def _automator_workflow() -> str:
    command = shlex.quote(_command()).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict><key>AMDocumentVersion</key><string>2</string>
<key>actions</key><array><dict><key>action</key><dict>
<key>AMAccepts</key><dict><key>Container</key><string>List</string><key>Optional</key><true/><key>Types</key><array><string>com.apple.file-system.object</string></array></dict>
<key>AMActionVersion</key><string>1.0</string><key>AMBundleIdentifier</key><string>com.apple.RunShellScript</string><key>AMParameterProperties</key><dict/></dict>
<key>parameters</key><dict><key>COMMAND_STRING</key><string>{command} shell-menu convert --gui "$@"</string><key>inputMethod</key><string>2</string><key>shell</key><string>/bin/zsh</string><key>workingDirectory</key><string>"$HOME"</string></dict>
<key>UUID</key><string>5B477F7A-23E0-4E7D-9F08-0F3D00000001</string></dict><key>isEnabled</key><true/><key>name</key><string>用 Openmd 转成 Markdown</string><key>workflowActionType</key><string>com.apple.RunShellScript</string></dict></array>
<key>connectors</key><dict/><key>workflowMetaData</key><dict><key>workflowTypeIdentifier</key><string>com.apple.Automator.servicesMenu</string></dict>
<key>inputTypeIdentifier</key><string>com.apple.Automator.fileSystemObject</string><key>outputTypeIdentifier</key><string>com.apple.Automator.nothing</string><key>serviceInputTypeIdentifier</key><string>com.apple.Automator.fileSystemObject</string><key>serviceApplication</key><string>Finder</string></dict></plist>'''
