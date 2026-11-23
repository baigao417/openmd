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

from openmd.core.detector import DOCUMENT_EXTENSIONS, IMAGE_TEXT_EXTENSIONS
from openmd.core.filename import safe_filename
from openmd.core.models import ContentItem
from openmd.core.sync_manifest import SyncManifest, source_id_for


SUPPORTED_SUFFIXES = frozenset((*DOCUMENT_EXTENSIONS, *IMAGE_TEXT_EXTENSIONS))
DEFAULT_MAX_FILES = 1000
DEFAULT_MAX_BYTES = 100 * 1024 * 1024
_DATA_URI = re.compile(r"data:image/([a-zA-Z0-9.+-]+);base64,([A-Za-z0-9+/=\r\n]+)")
_GUI_WINDOW_TITLE = "Openmd · 转成 Markdown"


def _hash_source(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _item(path: Path) -> ContentItem:
    absolute = path.resolve()
    is_image = absolute.suffix.lower() in IMAGE_TEXT_EXTENSIONS
    identity = hashlib.sha256(str(absolute).encode("utf-8")).hexdigest()[:24]
    return ContentItem(
        platform="screenshot" if is_image else "pdf",
        input_type="single",
        url=absolute.as_uri(),
        id=identity,
        title=path.stem,
        source_type="image_text" if is_image else "document",
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


def _convert_file(path: Path, output: Path, *, ocr_confirm: Callable[[str], bool] | None = None) -> dict[str, Any]:
    from openmd.engines.registry import convert_document
    from openmd.engines.base import EngineLimits

    is_image = path.suffix.lower() in IMAGE_TEXT_EXTENSIONS
    if path.suffix.lower() == ".md":
        text = path.read_text(encoding="utf-8-sig")
        status, error = ("verified", None) if text.strip() else ("failed", "document_empty_text")
    elif is_image:
        from types import SimpleNamespace
        from openmd.backends.ocr import OCRRouterProvider

        options = SimpleNamespace(ocr_engine="ocr.chain", enable_network_backends=True)
        result = OCRRouterProvider("ocr.chain", options).extract(
            SimpleNamespace(item=_item(path)), first_use_confirm=ocr_confirm
        )
        if result.error_code == "ollama_model_missing":
            return {"status": "failed", "error": "ocr_install_required",
                    "next_action": "openmd models install ocr:chain"}
        text, status, error = result.text, result.status, result.error_code
    else:
        result = convert_document(path, EngineLimits(max_file_bytes=max(DEFAULT_MAX_BYTES, path.stat().st_size)))
        if path.suffix.lower() == ".pdf" and result.error_code == "document_empty_text":
            from types import SimpleNamespace
            from openmd.backends.ocr import OCRRouterProvider

            options = SimpleNamespace(ocr_engine="ocr.chain", enable_network_backends=True)
            request = SimpleNamespace(item=_item(path))
            result = OCRRouterProvider("ocr.chain", options).extract(request, first_use_confirm=ocr_confirm)
            if result.error_code == "ollama_model_missing":
                return {"status": "failed", "error": "ocr_install_required",
                        "next_action": "openmd models install ocr:chain"}
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
    ocr_confirm: Callable[[str], bool] | None = None,
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
            converted = _convert_file(path, staging, ocr_confirm=ocr_confirm)
            if converted["status"] == "failed":
                result["failed"] += 1
                failure = {"path": str(path), "error": converted["error"]}
                if converted.get("next_action"):
                    failure["next_action"] = converted["next_action"]
                result["failures"].append(failure)
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
        "ocr_install_required": "安装扫描件 OCR 模型后重试：openmd models install ocr:chain",
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


def _brand_icon_path() -> Path:
    """Return the packaged ICO used by Windows shell entries and Tk."""
    from openmd.browser_setup import EXTENSION_DIR  # one owner of the packaged extension location

    return EXTENSION_DIR / "icons" / "openmd.ico"


def _brand_png_path() -> Path:
    from openmd.browser_setup import EXTENSION_DIR

    return EXTENSION_DIR / "icons" / "icon-128.png"


def _gui_title() -> str:
    return _GUI_WINDOW_TITLE


def _gui_progress_heading(index: int, total: int) -> str:
    return f"仓鼠正在搬运 {index} / {total}"


def _gui_progress_filename(name: str, max_chars: int = 42) -> str:
    if len(name) <= max_chars:
        return name
    suffix = Path(name).suffix
    tail = suffix[-3:] if suffix else ""
    head_len = max(1, max_chars - len(tail) - 1)
    return name[:head_len] + "…" + tail


def _gui_summary(value: dict[str, Any]) -> str:
    return (
        f"成功 {value.get('success', 0)} · 未变化 {value.get('unchanged', 0)} · "
        f"保留编辑 {value.get('conflict', 0)} · 跳过 {value.get('skipped', 0)} · "
        f"失败 {value.get('failed', 0)}"
    )


def _gui_failure_lines(failures: list[dict[str, Any]], limit: int = 5) -> list[str]:
    return [
        f"{Path(row.get('path', '')).name}：{_failure_message(str(row.get('error', '')))}"
        for row in failures[:limit]
    ]


def _gui_completion_heading(value: dict[str, Any]) -> str:
    failed = int(value.get("failed", 0) or 0)
    return f"搬完啦，有 {failed} 个没搬动" if failed else "搬完啦！"


def _gui_result_chips(value: dict[str, Any]) -> list[tuple[str, bool]]:
    # Success is always shown; other counts only when they happened (user-picked
    # preview shows "成功 9 · 未变化 1 · 保留你的编辑 1 · 失败 1", no zero chips).
    rows = [("成功", "success", False), ("未变化", "unchanged", False), ("保留你的编辑", "conflict", False),
            ("跳过", "skipped", False), ("失败", "failed", True)]
    return [(f"{label} {int(value.get(key, 0) or 0)}", bad) for label, key, bad in rows
            if key == "success" or int(value.get(key, 0) or 0)]


def _gui_completion_subtitle(input_path: str | Path, output_dir: str | Path | None) -> str:
    source = Path(input_path).expanduser()
    if source.is_file() or (not source.exists() and source.suffix):
        return "已放在原文件旁边"
    if output_dir:
        return f"已放到「{Path(output_dir).name}」文件夹"
    return "已放到 Markdown 文件夹"


def _set_process_dpi_awareness() -> None:
    if not sys.platform.startswith("win"):
        return
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass


def _center_window(window: Any, width: int = 560, height: int = 340) -> None:
    window.update_idletasks()
    screen_width = window.winfo_screenwidth()
    screen_height = window.winfo_screenheight()
    x = max(0, (screen_width - width) // 2)
    y = max(0, (screen_height - height) // 2)
    window.geometry(f"{width}x{height}+{x}+{y}")


def _set_window_icon(window: Any, tk: Any) -> None:
    try:
        if sys.platform.startswith("win"):
            window.iconbitmap(str(_brand_icon_path()))
        else:
            icon = tk.PhotoImage(file=str(_brand_png_path()))
            window.iconphoto(True, icon)
            window._openmd_icon = icon
    except Exception:
        pass


def _rounded_canvas_item(canvas: Any, tk: Any, width: int, height: int, *, fill: str, text: str, command: Callable[[], None] | None = None, text_color: str = "#6b4524") -> Any:
    widget = tk.Canvas(canvas, width=width, height=height, bd=0, highlightthickness=0, bg=canvas.cget("bg"))
    radius = min(height // 2, 18)
    widget.create_rectangle(radius, 0, width - radius, height, fill=fill, outline=fill)
    widget.create_oval(0, 0, radius * 2, height, fill=fill, outline=fill)
    widget.create_oval(width - radius * 2, 0, width, height, fill=fill, outline=fill)
    widget.create_text(width // 2, height // 2, text=text, fill=text_color, font=("Microsoft YaHei UI", 10, "bold"))
    widget._openmd_command = command
    widget.bind("<Button-1>", lambda _event: widget._openmd_command() if widget._openmd_command else None)
    return widget


def _gui_output_dir(input_path: str | Path, out: str | Path | None) -> Path | None:
    source = Path(input_path).expanduser()
    if out:
        return Path(out).expanduser()
    if source.is_dir():
        return source.parent / f"{safe_filename(source.name, max_length=120)}-md"
    return None


def _build_progress_window(window: Any, tk: Any, ttk: Any) -> dict[str, Any]:
    """Build the selected warm mascot GUI and return conversion callbacks."""
    peach, cream = "#FFE9C9", "#fff8ee"
    brown, muted = "#4a2f17", "#8a6646"
    orange, stripe, track = "#F2A65A", "#f0b273", "#f6dcbc"
    width = 560
    _set_process_dpi_awareness()
    window.title(_gui_title())
    window.resizable(False, False)
    window.configure(bg=peach)
    _set_window_icon(window, tk)
    state: dict[str, Any] = {"cancelled": False, "bar": (0.0, 1)}

    frame = tk.Frame(window, bg=peach, padx=18, pady=14)
    frame.pack(fill="both", expand=True)
    panel = tk.Frame(frame, bg=cream, padx=20, pady=18, highlightthickness=1, highlightbackground="#f1d6b3")
    panel.pack(fill="x")
    panel.grid_columnconfigure(1, weight=1)
    mascot_box = tk.Frame(panel, bg=peach, width=76, height=76)
    mascot_box.grid(row=0, column=0, rowspan=2, sticky="nw", padx=(0, 14))
    mascot_box.grid_propagate(False)
    mascot = tk.PhotoImage(file=str(_brand_png_path())).subsample(2, 2)
    window._openmd_mascot = mascot
    tk.Label(mascot_box, image=mascot, bg=peach).place(relx=.5, rely=.5, anchor="center")
    heading = tk.Label(panel, text=_gui_progress_heading(0, 0), bg=cream, fg=brown, font=("Microsoft YaHei UI", 16, "bold"), anchor="w")
    heading.grid(row=0, column=1, sticky="ew")
    subtitle = tk.Label(panel, text="正在准备本地转换…", bg=cream, fg=muted, font=("Microsoft YaHei UI", 10), anchor="w")
    subtitle.grid(row=1, column=1, sticky="ew", pady=(4, 0))
    bar = tk.Canvas(panel, width=10, height=12, bg=cream, bd=0, highlightthickness=0)
    bar.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(18, 0))
    result_area = tk.Frame(panel, bg=cream)
    result_area.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(16, 0))
    buttons = tk.Frame(panel, bg=cream)
    buttons.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(20, 0))

    def draw_bar(value: float, maximum: int) -> None:
        state["bar"] = (value, maximum)
        bar.delete("all")
        full = max(bar.winfo_width(), 10)
        filled = max(1, min(full, int(full * (value / max(maximum, 1)))))
        bar.create_rectangle(0, 1, full, 11, fill=track, outline=track)
        bar.create_rectangle(0, 1, filled, 11, fill=orange, outline=orange)
        for x in range(-12, filled, 12):
            bar.create_polygon(x, 11, x + 5, 11, x + 17, 1, x + 12, 1, fill=stripe, outline=stripe)
        # Stripes may cross the fill edge; repaint the unfilled track over them.
        if filled < full:
            bar.create_rectangle(filled, 0, full, 12, fill=cream, outline=cream)
            bar.create_rectangle(filled, 1, full, 11, fill=track, outline=track)
        else:
            bar.create_rectangle(full, 0, full + 20, 12, fill=cream, outline=cream)

    bar.bind("<Configure>", lambda _event: draw_bar(*state["bar"]))

    def fit_height() -> None:
        # Size the window to its content instead of a fixed height, keeping it centered.
        window.update_idletasks()
        _center_window(window, width, frame.winfo_reqheight())

    def set_progress(index: int, total: int, path: Path) -> None:
        if state["cancelled"]:
            return
        heading.configure(text=_gui_progress_heading(index, total))
        subtitle.configure(text=_gui_progress_filename(path.name))
        draw_bar(index, total)
        window.update_idletasks()
        window.update()

    def show_result(value: dict[str, Any], input_path: str | Path, output_dir: Path | None) -> None:
        if state["cancelled"]:
            return
        heading.configure(text=_gui_completion_heading(value))
        subtitle.configure(text=_gui_completion_subtitle(input_path, output_dir))
        draw_bar(1, 1)
        for child in result_area.winfo_children():
            child.destroy()
        chips = tk.Frame(result_area, bg=cream)
        chips.pack(anchor="w", fill="x")
        for label_text, is_bad in _gui_result_chips(value):
            chip = tk.Label(chips, text=label_text, bg="#fff3f1" if is_bad else "#fff", fg="#a3352b" if is_bad else "#6b4524", padx=10, pady=3, font=("Microsoft YaHei UI", 9, "bold"), bd=0, highlightthickness=1, highlightbackground="#f0b8b2" if is_bad else "#f1d6b3")
            chip.pack(side="left", padx=(0, 6))
        lines = _gui_failure_lines(value.get("failures", []))
        if lines:
            tk.Label(result_area, text="失败原因：\n" + "\n".join(lines), bg=cream, fg="#8a3a2f", justify="left", anchor="w", wraplength=width - 90, font=("Microsoft YaHei UI", 9)).pack(anchor="w", pady=(8, 0))
        for child in buttons.winfo_children():
            child.destroy()
        _rounded_canvas_item(buttons, tk, 72, 34, fill=track, text="关闭", command=window.destroy).pack(side="left")
        open_button = _rounded_canvas_item(buttons, tk, 132, 34, fill=orange, text="打开文件夹", text_color="#fff")
        open_button.pack(side="right")
        state["open_button"] = open_button
        fit_height()

    def set_open_command(command: Callable[[], None]) -> None:
        if state.get("open_button") is not None:
            state["open_button"]._openmd_command = command

    def cancel() -> None:
        state["cancelled"] = True
        window.destroy()

    _rounded_canvas_item(buttons, tk, 72, 34, fill=track, text="取消", command=cancel).pack(side="left")
    fit_height()
    draw_bar(0, 1)
    window.deiconify()
    window.lift()
    window.update_idletasks()
    window.update()
    return {"set_progress": set_progress, "show_result": show_result, "set_open_command": set_open_command, "cancelled": lambda: state["cancelled"]}


def _open_in_file_manager(path: Path) -> None:
    # No subprocess here: process launches belong to the runtime owners. The OS
    # shell opens the folder (Windows) and webbrowser hands a file URI to
    # open/xdg-open on macOS/Linux, the same handler a user double-click uses.
    try:
        if sys.platform.startswith("win"):
            os.startfile(str(path))
        else:
            import webbrowser

            webbrowser.open(path.resolve().as_uri())
    except Exception:
        pass


def _result_folder(input_path: str | Path, value: dict[str, Any]) -> Path:
    outputs = [Path(item) for item in value.get("outputs", []) if item]
    if outputs:
        return outputs[0].parent
    source = Path(input_path).expanduser()
    return source if source.is_dir() else source.parent


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
                "next_action": "先用 `uv tool install openmd-cli`（或 `pipx install openmd-cli`）做持久安装，再运行 shell-menu install",
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
            icon_path = _brand_icon_path()
            if not icon_path.is_file():
                raise RuntimeError(f"brand_icon_missing: {icon_path}")
            _winreg.SetValueEx(key, "Icon", 0, _winreg.REG_SZ, str(icon_path))
            if key_path == r"Software\Classes\*\shell\OpenmdToMarkdown":
                _winreg.SetValueEx(key, "AppliesTo", 0, _winreg.REG_SZ, _applies_to_filter())
            _winreg.CloseKey(key)
            command_key = _winreg.CreateKey(_winreg.HKEY_CURRENT_USER, key_path + r"\command")
            _winreg.SetValueEx(command_key, "", 0, _winreg.REG_SZ, command)
            _winreg.CloseKey(command_key)
        _backup_path().parent.mkdir(parents=True, exist_ok=True)
        _backup_path().write_text(json.dumps(backup, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"installed": True, "platform": platform, "scope": "HKCU", "menu_visibility": "Windows 11 may place legacy entries under Show more options."}
    target = _unix_menu_path(platform)
    if target.exists() and not _owns_unix_menu(platform, target):
        return {
            "installed": False,
            "platform": platform,
            "error": "menu_name_taken",
            "path": str(target),
            "next_action": "同名菜单项已存在且不是 Openmd 安装的；请先自行移走或改名该项，再运行 shell-menu install",
        }
    if platform == "macos":
        root = target / "Contents"
        root.mkdir(parents=True, exist_ok=True)
        (root / "document.wflow").write_text(_automator_workflow(), encoding="utf-8")
        (root / "Info.plist").write_text('''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict><key>CFBundleIdentifier</key><string>dev.openmd.shell-menu</string><key>CFBundleName</key><string>用 Openmd 转成 Markdown</string></dict></plist>
''', encoding="utf-8")
        return {"installed": True, "platform": platform, "path": str(root.parent)}
    script = target
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
    path = _unix_menu_path(platform)
    if path.exists() and not _owns_unix_menu(platform, path):
        # Never delete a same-name item that Openmd did not install.
        return {"removed": False, "platform": platform, "error": "menu_not_owned", "path": str(path)}
    if platform == "macos":
        shutil.rmtree(path, ignore_errors=True)
    else:
        path.unlink(missing_ok=True)
    return {"removed": True, "platform": platform}


def _unix_menu_path(platform: str) -> Path:
    if platform == "macos":
        return Path.home() / "Library" / "Services" / "用 Openmd 转成 Markdown.workflow"
    return Path.home() / ".local" / "share" / "nautilus" / "scripts" / "用 Openmd 转成 Markdown"


def _owns_unix_menu(platform: str, path: Path) -> bool:
    """True when the item at ``path`` carries the marker Openmd writes at install."""
    try:
        if platform == "macos":
            return "dev.openmd.shell-menu" in (path / "Contents" / "Info.plist").read_text(encoding="utf-8")
        return path.is_file() and "shell-menu convert --gui" in path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False


def status(*, platform: str | None = None) -> dict[str, Any]:
    platform = _platform_name(platform)
    if platform == "windows":
        installed = bool(_winreg is not None and all(_registry_exists(path) for path, _, _ in _win_keys()))
    else:
        path = _unix_menu_path(platform)
        installed = path.exists() and _owns_unix_menu(platform, path)
    return {"installed": installed, "platform": platform}


def _registry_exists(path: str) -> bool:
    try:
        key = _winreg.OpenKey(_winreg.HKEY_CURRENT_USER, path)
        _winreg.CloseKey(key)
        return True
    except OSError:
        return False


def _registry_values(path: str) -> dict[str, Any]:
    try:
        key = _winreg.OpenKey(_winreg.HKEY_CURRENT_USER, path)
        try:
            values: dict[str, Any] = {}
            index = 0
            while True:
                try:
                    name, value, kind = _winreg.EnumValue(key, index)
                except OSError:
                    break
                values[name] = _encode_registry_value(value, kind)
                index += 1
            return values
        finally:
            _winreg.CloseKey(key)
    except OSError:
        return {}


def _encode_registry_value(value: Any, kind: int) -> dict[str, Any]:
    """JSON-safe backup of one registry value that keeps its original type."""
    if isinstance(value, (bytes, bytearray)):
        return {"type": kind, "base64": base64.b64encode(bytes(value)).decode("ascii")}
    return {"type": kind, "value": value}


def _decode_registry_value(stored: Any) -> tuple[Any, int]:
    if isinstance(stored, dict) and "type" in stored:
        if "base64" in stored:
            return base64.b64decode(stored["base64"]), int(stored["type"])
        return stored.get("value"), int(stored["type"])
    return stored, _winreg.REG_SZ  # backups written before value types were kept


def _restore_registry_key(path: str, values: dict[str, Any]) -> None:
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
        for name, stored in values.items():
            value, kind = _decode_registry_value(stored)
            _winreg.SetValueEx(key, name, 0, kind, value)
    finally:
        _winreg.CloseKey(key)


def _remove_owned_registry_key(path: str) -> None:
    try:
        key = _winreg.OpenKey(_winreg.HKEY_CURRENT_USER, path)
    except OSError:
        return
    try:
        for name in ("", "AppliesTo", "Icon"):
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
        ui = None
        progress = None
        messagebox = None
        if gui:
            try:
                import tkinter as tk
                from tkinter import messagebox
                from tkinter import ttk

                _set_process_dpi_awareness()
                window = tk.Tk()
                ui = _build_progress_window(window, tk, ttk)
                root_path = Path(args.input).expanduser()
                total = len(_candidate_files(root_path)) if root_path.exists() else 0

                def update_progress(index: int, total: int, path: Path) -> None:
                    ui["set_progress"](index, total, path)

                progress = update_progress
            except Exception:
                window = None
                ui = None
                progress = None
        confirm_large = args.confirm_large
        if window is not None and not confirm_large:
            root_path = Path(args.input).expanduser()
            candidates = _candidate_files(root_path) if root_path.exists() else []
            too_large = len(candidates) > args.max_files or any(p.stat().st_size > args.max_bytes for p in candidates)
            if too_large:
                confirm_large = messagebox.askyesno("输入规模较大", f"输入超过默认限制（{args.max_files} 个文件 / 每个文件 {args.max_bytes // (1024 * 1024)} MiB）。仍要继续吗？", parent=window)
        ocr_confirm = (lambda question: messagebox.askyesno("首次安装扫描件 OCR", question, parent=window)) if window is not None else None
        value = convert_path(args.input, out=args.out, max_files=args.max_files, max_bytes=args.max_bytes,
                             confirm_large=confirm_large, progress=progress, ocr_confirm=ocr_confirm)
        if window is not None and ui is not None:
            if ui["cancelled"]():
                return 0 if value.get("ok") else 1
            failures = value.get("failures", [])
            ui["show_result"](value, args.input, _gui_output_dir(args.input, args.out))
            ui["set_open_command"](lambda: _open_in_file_manager(_result_folder(args.input, value)))
            window.update_idletasks()
            if not failures and value.get("ok") and not value.get("skipped"):
                window.after(6000, window.destroy)
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
    failed = (action == "install" and value.get("installed") is False) or (action == "uninstall" and value.get("removed") is False)
    return 1 if failed else 0


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
