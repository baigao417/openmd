"""Register (or remove) the Openmd native messaging host for Chrome and Edge on this user.

Writes only user-scope state: ``<data>/browser/`` (host manifest, launcher, extension copy) and the
``HKCU\\Software\\{Google\\Chrome,Microsoft\\Edge}\\NativeMessagingHosts\\com.openmd.host`` keys.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from openmd.browser_host import HOST_NAME
from openmd.core.preferences import data_directory
from openmd.runtime.interpreters import run_interruptibly

EXTENSION_DIR = Path(__file__).resolve().parent / "browser_extension"
REGISTRY_KEYS = (
    r"HKCU\Software\Google\Chrome\NativeMessagingHosts",
    r"HKCU\Software\Microsoft\Edge\NativeMessagingHosts",
)


def extension_id(manifest_path: Path = EXTENSION_DIR / "manifest.json") -> str:
    """Chrome's id for an unpacked extension whose manifest pins ``key``."""
    key = json.loads(manifest_path.read_text(encoding="utf-8"))["key"]
    digest = hashlib.sha256(base64.b64decode(key)).hexdigest()[:32]
    return "".join(chr(ord("a") + int(ch, 16)) for ch in digest)


def host_files(root: Path | None = None) -> tuple[Path, Path]:
    root = root or data_directory() / "browser"
    return root / f"{HOST_NAME}.json", root / "openmd-browser-host.cmd"


def write_host_files(root: Path | None = None, python: str = sys.executable) -> Path:
    manifest_path, launcher = host_files(root)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    launcher.write_text(f'@echo off\r\n"{python}" -I -m openmd.browser_host %*\r\n', encoding="utf-8")
    manifest = {
        "name": HOST_NAME,
        "description": "Openmd: save the current page as clean Markdown",
        "path": str(launcher),
        "type": "stdio",
        "allowed_origins": [f"chrome-extension://{extension_id()}/"],
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest_path


def _reg(*args: str) -> bool:
    return run_interruptibly(["reg", *args], capture_output=True, check=False).returncode == 0


def launcher_ping(launcher: Path) -> bool:
    """Exercise the written launcher with one native-message ping, not python -m.

    Never include raw stdout/stderr in errors: ping may contain a user path.
    """
    from openmd.browser_host import read_message, write_message

    request = io.BytesIO()
    write_message(request, {"type": "ping"})
    try:
        # cmd uses doubled quotes, not the backslash escaping that Python's
        # list2cmdline applies to argv lists. Keep this one fixed command string.
        command = f'"{os.environ.get("COMSPEC", "cmd.exe")}" /d /s /c ""{launcher}""'
        reply = run_interruptibly(command,
                                  input=request.getvalue(), capture_output=True, timeout=15)
        if reply.returncode != 0:
            return False
        message = read_message(io.BytesIO(reply.stdout))
        return isinstance(message, dict) and message.get("ok") is True
    except (OSError, ValueError, UnicodeError, subprocess.TimeoutExpired):
        return False


def install() -> int:
    if os.name != "nt":
        print("目前只支持 Windows。", file=sys.stderr)
        return 2
    manifest_path = write_host_files()
    if not launcher_ping(host_files(manifest_path.parent)[1]):
        print("浏览器原生宿主 launcher ping 自检失败；请确认当前 Python 已安装 openmd，再运行 openmd browser-setup。", file=sys.stderr)
        return 1
    registered = [key for key in REGISTRY_KEYS if _reg("add", f"{key}\\{HOST_NAME}", "/ve", "/t", "REG_SZ", "/d", str(manifest_path), "/f")]
    if not registered:
        print("注册失败：无法写入当前用户注册表。", file=sys.stderr)
        return 1
    folder = stable_extension_dir()
    print("浏览器插件的本地连接已就绪。")
    print("下一步（只需一次）：打开 chrome://extensions（Edge 为 edge://extensions）→ 打开“开发者模式”")
    print("→ “加载已解压的扩展程序” → 选择这个文件夹：")
    print(f"  {folder}")
    print(f"插件 ID 应显示为：{extension_id()}。升级 openmd 后再运行一次 openmd browser-setup 即可同步插件。")
    if sys.stdout.isatty():
        with contextlib.suppress(OSError):
            os.startfile(folder)  # type: ignore[attr-defined]  # Windows only, checked above
    return 0


def stable_extension_dir(root: Path | None = None) -> Path:
    """Copy the packaged extension to a path that survives reinstalls.

    An unpacked extension is loaded by folder path; the site-packages folder
    moves with every venv/pipx/Python change, ~/.openmd/browser/extension does
    not. The manifest key keeps the extension ID identical.
    """
    target = (root or data_directory() / "browser") / "extension"
    shutil.copytree(EXTENSION_DIR, target, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__", "*.py", "*.pyc"))
    return target


def uninstall() -> int:
    for key in REGISTRY_KEYS:
        _reg("delete", f"{key}\\{HOST_NAME}", "/f")
    for path in host_files():
        path.unlink(missing_ok=True)
    shutil.rmtree(data_directory() / "browser" / "extension", ignore_errors=True)
    print("已移除浏览器插件的本地连接（插件本身请在 chrome://extensions 里移除）。")
    return 0


def cmd_browser_setup(args: argparse.Namespace) -> int:
    return uninstall() if args.uninstall else install()
