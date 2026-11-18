"""Tools Openmd installs and runs for the user, so one `pip install openmd-cli` is enough.

opencli (Apache-2.0, npm `@jackwener/opencli`) powers the browser-session
routes (Bilibili subtitles, Weixin, X, Douyin ...). Node.js comes from the
`nodejs-wheel-binaries` dependency; opencli itself is installed once, on first
need, into ``<data>/tools/opencli`` at a pinned version.

Openmd always selects its pinned copy; other PATH installs are ignored.
Set ``OPENMD_AUTO_INSTALL_TOOLS=0`` to disable the automatic install, and
``OPENMD_NPM_REGISTRY`` to use a registry mirror.
"""

from __future__ import annotations

import importlib.util
import json
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
        if (not isinstance(setting, dict) or set(setting) - {"root", "config_dir"}
                or not isinstance(setting.get("root"), str) or not setting["root"].strip()):
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

    Returns "managed" (Openmd's copy) or "missing".
    Browser reads default to a background (minimized) window.
    """
    show = os.environ.get("OPENMD_SHOW_BROWSER", "").strip().lower() in {"1", "true", "yes", "on"}
    os.environ.setdefault("OPENCLI_WINDOW", "foreground" if show else "background")
    os.environ.setdefault("OPENCLI_CLOSE_OWNED_TAB", "1")
    os.environ["OPENCLI_CONFIG_DIR"] = str(config_directory())
    if _managed_ready():
        node = node_dir()
        # Move existing managed entries too: a later PATH install must never win.
        folders = [*([node] if node else []), managed_bin()]
        os.environ["PATH"] = os.pathsep.join([*(str(f) for f in folders),
            *(p for p in os.environ.get("PATH", "").split(os.pathsep) if p not in {str(f) for f in folders})])
        _prepare_bundled_node_host_package()
        return "managed"
    return "missing"


def auto_install_enabled() -> bool:
    return os.environ.get("OPENMD_AUTO_INSTALL_TOOLS", "1").strip().lower() not in {"0", "false", "no", "off"}


def ensure_opencli(say: Callable[[str], None] | None = None) -> str:
    """Install the pinned opencli once if no opencli is available.

    Returns the activate() state after the attempt; never raises for a failed
    install (the route then reports opencli as missing with a hint).
    """
    state = activate()
    if state == "managed" and (managed_package_root() / "package.json").is_file():
        from .opencli_adapters import prepare_bundled_plugins
        prepare_bundled_plugins()
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
    state = activate()
    if (managed_package_root() / "package.json").is_file():
        from .opencli_adapters import prepare_bundled_plugins
        prepare_bundled_plugins()
    return state


def status() -> dict[str, object]:
    """Doctor view: where opencli comes from, without probing the network."""
    other_path = os.pathsep.join(p for p in os.environ.get("PATH", "").split(os.pathsep)
                               if Path(p).resolve() != managed_bin().resolve())
    other = shutil.which("opencli", path=other_path)
    state = activate()
    config = config_directory()
    package = managed_package_root() / "package.json"
    version = json.loads(package.read_text(encoding="utf-8")).get("version") if package.is_file() else None
    return {
        "opencli": state,
        "opencli_version_pinned": OPENCLI_VERSION,
        "managed_path": str(managed_root()),
        "executable": str(managed_bin() / ("opencli.cmd" if os.name == "nt" else "opencli")),
        "actual_version": version,
        "config_dir": str(config),
        "plugins": sorted(p.name for p in (config / "plugins").iterdir()) if (config / "plugins").is_dir() else [],
        "ignored_path_opencli": other if other and Path(other).parent != managed_bin() else None,
        "bundled_node": node_dir() is not None,
        "auto_install": auto_install_enabled(),
    }


def config_directory() -> Path:
    setting = load_local_config().get("tools", {}).get("opencli", {})
    return Path(setting["config_dir"]).expanduser().resolve() if "config_dir" in setting else data_directory() / "opencli"


def command_environment(base=None) -> dict[str, str]:
    """2.0.3 discovery uses os.homedir, plugin lock uses HOME, profiles use CONFIG_DIR.

    A private home alias makes all three resolve to the same config directory,
    without patching npm files or changing the controller/user's home.
    """
    config = config_directory()
    config.mkdir(parents=True, exist_ok=True)
    home = config.parent / ("." + config.name + "-home")
    home.mkdir(parents=True, exist_ok=True)
    alias = home / ".opencli"
    if alias.exists() or alias.is_symlink():
        if alias.resolve() != config.resolve():
            raise ValueError("opencli_home_alias_conflict")
    else:
        try:
            alias.symlink_to(config, target_is_directory=True)
        except OSError:
            _create_junction(config, alias)
    env = dict(os.environ if base is None else base)
    env.update(OPENCLI_CONFIG_DIR=str(config), HOME=str(home), USERPROFILE=str(home))
    env.setdefault("OPENCLI_PROFILE", "openmd")
    env.setdefault("OPENCLI_CLOSE_OWNED_TAB", "1")
    env.setdefault("OPENCLI_WINDOW", "foreground" if env.get("OPENMD_SHOW_BROWSER") == "1" else "background")
    env.setdefault("NODE_OPTIONS", "--max-old-space-size=1536")
    return env


def command_uses_browser(args: list[str], *, package_root: Path | None = None) -> bool | None:
    """Read pinned command metadata; API commands have no browser CLI options."""
    if len(args) < 2:
        return None
    if args[0] in {"openmd-douyin", "openmd-xiaohongshu"}:
        return True
    try:
        manifest = json.loads(((package_root or managed_package_root()) / "cli-manifest.json").read_text(encoding="utf-8"))
        for row in manifest:
            if row.get("site") == args[0] and row.get("name") == args[1]:
                return row.get("browser") if type(row.get("browser")) is bool else None
    except (OSError, ValueError, TypeError):
        pass
    return None


def managed_command(args: list[str]) -> list[str]:
    node = node_dir()
    if node is None:
        raise OSError("managed_opencli_node_missing")
    if command_uses_browser(args) is True and "--site-session" not in args and not any(a.startswith("--site-session=") for a in args):
        args = [*args, "--site-session", "ephemeral", "--keep-tab", "false"]
    return [str(node / ("node.exe" if os.name == "nt" else "node")),
            str(managed_package_root() / "dist/src/main.js"), *args]


def _migration_files(source: Path):
    def walk(folder, relative, ancestors):
        resolved = folder.resolve()
        if resolved in ancestors:
            return
        for entry in sorted(folder.iterdir(), key=lambda p: p.name):
            name = relative / entry.name
            if entry.is_dir():
                yield from walk(entry, name, ancestors | {resolved})
            elif entry.is_file():
                yield entry, name
    if source.is_dir():
        yield from walk(source, Path(), set())


def migrate_config(*, source: Path | None = None, destination: Path | None = None, apply: bool = False):
    source = source or Path.home() / ".opencli"
    destination = destination or config_directory()
    if source.resolve() == destination.resolve() or source.resolve() in destination.resolve().parents:
        raise ValueError("opencli_migration_destination_invalid")
    files = list(_migration_files(source))
    receipt = {"schema": "openmd.opencli-migration/v1", "applied": apply,
               "files": [{"name": rel.as_posix(), "size": file.stat().st_size} for file, rel in files],
               "copied": [], "conflicts": [], "source_preserved": True}
    if apply:
        if any(not (destination / relative).resolve().is_relative_to(destination.resolve()) for _, relative in files):
            raise ValueError("opencli_migration_destination_link_escape")
        destination.mkdir(parents=True, exist_ok=True)
        for file, relative in files:
            target = destination / relative
            if target.exists():
                receipt["conflicts"].append(relative.as_posix())
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(file, target)
            receipt["copied"].append(relative.as_posix())
        (destination / "openmd-migration-receipt.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    return receipt


def command_access(args: list[str]) -> str:
    """Read registry metadata without executing the requested adapter. Unknown fails closed."""
    if args == ["list"] or args[:1] == ["list"]:
        return "read"
    if args in (["doctor"], ["status"], ["daemon", "status"], ["--help"], ["-h"]):
        return "read"
    if len(args) >= 2 and args[1] in {"--help", "-h"}:
        known_sites = {"chatgpt", "deepseek", "claude", "doubao", "gemini", "qwen", "yuanbao", "grok", "douyin", "xiaohongshu", "youtube", "bilibili", "weixin", "x", "xiaoe", "browser", "openmd-douyin", "openmd-xiaohongshu"}
        return "read" if args[0] in known_sites else "unknown"
    if len(args) < 2 or any(a.startswith("-") for a in args[:2]):
        return "unknown"
    env = command_environment()
    result = run_interruptibly(managed_command(["list", "--site", args[0], "--limit", "100", "-f", "json"]),
        env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
    from openmd.core.opencli_output import load_opencli_json
    payload = load_opencli_json(result.stdout)
    if result.returncode or not isinstance(payload, dict):
        return "unknown"
    for row in payload.get("commands", []):
        if row.get("command") == " ".join(args[:2]):
            return row.get("access", "unknown")
    return "unknown"


def passthrough(args: list[str]) -> int:
    """Inherit stdout/stderr directly, retaining the original exit status."""
    from .opencli_adapters import prepare_bundled_plugins
    prepare_bundled_plugins()
    env = None
    if len(args) >= 2 and args[0] in {"chatgpt", "deepseek", "grok", "doubao", "gemini", "qwen", "yuanbao"} and args[1] in {"detail", "status", "history", "read"}:
        from .ai_chat_loading import detail_loading_environment

        env = detail_loading_environment(args[0], args[1])
    return run_interruptibly(managed_command(args), env=command_environment(env), stdin=subprocess.DEVNULL).returncode
