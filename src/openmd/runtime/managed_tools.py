"""Tools Openmd installs and runs for the user, so one `pip install openmd-cli` is enough.

opencli (Apache-2.0, npm `@jackwener/opencli`) powers the browser-session
routes (Bilibili subtitles, Weixin, X, Douyin ...). Node.js comes from the
`nodejs-wheel-binaries` dependency; opencli itself is installed once, on first
need, into ``<data>/tools/opencli`` at a pinned version.

A user's own `opencli` on PATH always wins and is never modified.
Set ``OPENMD_AUTO_INSTALL_TOOLS=0`` to disable the automatic install, and
``OPENMD_NPM_REGISTRY`` to use a registry mirror.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable

from openmd.core.local_config import load_local_config
from openmd.core.preferences import data_directory
from openmd.runtime.interpreters import run_interruptibly

OPENCLI_PACKAGE = "@jackwener/opencli"
OPENCLI_VERSION = "2.0.3"
OPENCLI_BRIDGE_STORE_URL = "https://chromewebstore.google.com/detail/opencli/ildkmabpimmkaediidaifkhjpohdnifk"
_host_package_warning_shown = False


def node_dir() -> Path | None:
    """Directory holding the bundled `node` executable, if the wheel is installed."""
    spec = importlib.util.find_spec("nodejs_wheel")
    if spec is None or not spec.origin:
        return None
    root = Path(spec.origin).parent
    folder = root if os.name == "nt" else root / "bin"
    return folder if (folder / ("node.exe" if os.name == "nt" else "node")).is_file() else None


def _npm_cli(node: Path) -> Path | None:
    root = node if os.name == "nt" else node.parent
    candidate = root / "lib" / "node_modules" / "npm" / "bin" / "npm-cli.js"
    return candidate if candidate.is_file() else None


def managed_root() -> Path:
    tools = load_local_config().get("tools", {})
    if "opencli" in tools:
        setting = tools["opencli"]
        if (not isinstance(setting, dict) or set(setting) != {"root"}
                or not isinstance(setting["root"], str) or not setting["root"].strip()):
            raise ValueError("managed_opencli_location_invalid")
        return Path(setting["root"]).expanduser().resolve()
    return data_directory() / "tools" / "opencli"


def managed_bin() -> Path:
    return managed_root() / "node_modules" / ".bin"


def managed_package_root() -> Path:
    """Package root used by plugins when the managed host is active."""
    return managed_root() / "node_modules" / "@jackwener" / "opencli"


def _managed_ready() -> bool:
    return (managed_bin() / ("opencli.cmd" if os.name == "nt" else "opencli")).is_file()


def _prepend_path(*folders: Path) -> None:
    current = os.environ.get("PATH", "").split(os.pathsep)
    additions = [str(f) for f in folders if str(f) not in current]
    if additions:
        os.environ["PATH"] = os.pathsep.join([*additions, *current])


def _is_junction(path: Path) -> bool:
    # Path.is_symlink() does not identify junctions on Python 3.10/3.11.
    try:
        return getattr(path.lstat(), "st_reparse_tag", None) == getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", 0xA0000003)
    except FileNotFoundError:
        return False


def _create_junction(source: Path, target: Path) -> None:
    if os.name != "nt":
        raise OSError("Directory junctions require Windows")
    import _winapi

    _winapi.CreateJunction(str(source.resolve()), str(target))


def _prepare_bundled_node_host_package() -> None:
    """Expose the managed host at the bundled Node resolution root.

    Some user plugins were compiled by older OpenCLI versions that resolve
    ``@jackwener/opencli`` relative to ``process.execPath``.  The managed
    install keeps Node and npm under one private runtime directory, so provide
    the same package shape there without touching ``~/.opencli`` or a user's
    plugin directory.
    """
    global _host_package_warning_shown
    node = node_dir()
    source = managed_package_root()
    if node is None or not source.is_dir():
        return
    target = node / "node_modules" / "@jackwener" / "opencli"
    try:
        junction = _is_junction(target)
        if junction or target.is_symlink():
            try:
                if target.resolve() == source.resolve():
                    return
            except OSError:
                pass
            # rmdir removes only the junction, never the linked package tree.
            if junction:
                target.rmdir()
            else:
                target.unlink(missing_ok=True)
        elif target.exists():
            # Never replace a real package supplied by the Node runtime.
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            target.symlink_to(source, target_is_directory=True)
        except OSError:
            _create_junction(source, target)
    except OSError:
        if not _host_package_warning_shown:
            print("Openmd: browser plugin compatibility link could not be prepared; check runtime directory permissions and retry.", file=sys.stderr)
            _host_package_warning_shown = True


def activate() -> str:
    """Make a usable opencli visible to this process and its children.

    Returns "user" (own install on PATH), "managed" (Openmd's copy) or "missing".
    Browser reads default to a background (minimized) window.
    """
    show = os.environ.get("OPENMD_SHOW_BROWSER", "").strip().lower() in {"1", "true", "yes", "on"}
    os.environ.setdefault("OPENCLI_WINDOW", "foreground" if show else "background")
    if _managed_ready():
        if shutil.which("opencli") and Path(shutil.which("opencli")).parent != managed_bin():
            return "user"
        node = node_dir()
        _prepend_path(*([node] if node else []), managed_bin())
        _prepare_bundled_node_host_package()
        return "managed"
    return "user" if shutil.which("opencli") else "missing"


def auto_install_enabled() -> bool:
    return os.environ.get("OPENMD_AUTO_INSTALL_TOOLS", "1").strip().lower() not in {"0", "false", "no", "off"}


def ensure_opencli(say: Callable[[str], None] | None = None) -> str:
    """Install the pinned opencli once if no opencli is available.

    Returns the activate() state after the attempt; never raises for a failed
    install (the route then reports opencli as missing with a hint).
    """
    state = activate()
    if state != "missing" or not auto_install_enabled():
        return state
    node = node_dir()
    npm = _npm_cli(node) if node else None
    if node is None or npm is None:
        return "missing"
    say = say or (lambda message: print(message, file=sys.stderr))
    say("首次使用这类链接，正在准备浏览器读取组件（约 20MB，只需一次）…")
    root = managed_root()
    root.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix="opencli-", dir=root.parent))
    command = [str(node / ("node.exe" if os.name == "nt" else "node")), str(npm), "install", "--prefix", str(staging),
               f"{OPENCLI_PACKAGE}@{OPENCLI_VERSION}", "--no-audit", "--no-fund", "--loglevel=error", "--save-exact"]
    registry = os.environ.get("OPENMD_NPM_REGISTRY")
    if registry:
        command += ["--registry", registry]
    env = {**os.environ, "PATH": os.pathsep.join([str(node), os.environ.get("PATH", "")])}
    try:
        completed = run_interruptibly(command, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        shutil.rmtree(staging, ignore_errors=True)
        say(f"浏览器读取组件准备失败：{type(exc).__name__}；检查网络，或设置 OPENMD_NPM_REGISTRY 使用 npm 镜像后重试。")
        return "missing"
    if completed.returncode != 0:
        shutil.rmtree(staging, ignore_errors=True)
        tail = (completed.stderr or completed.stdout or "").strip().splitlines()[-1:] or [""]
        say(f"浏览器读取组件准备失败（{tail[0][:120]}）；检查网络，或设置 OPENMD_NPM_REGISTRY 使用 npm 镜像后重试。")
        return "missing"
    shutil.rmtree(root, ignore_errors=True)
    staging.rename(root)
    say("浏览器读取组件已就绪。")
    return activate()


def status() -> dict[str, object]:
    """Doctor view: where opencli comes from, without probing the network."""
    state = activate()
    return {
        "opencli": state,
        "opencli_version_pinned": OPENCLI_VERSION,
        "managed_path": str(managed_root()),
        "bundled_node": node_dir() is not None,
        "auto_install": auto_install_enabled(),
    }
