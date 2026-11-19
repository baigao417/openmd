"""Install owned adapters into an isolated OpenCLI user adapter directory.

The normal user's ~/.opencli and existing adapters are never changed. Dispatch
uses the real pinned OpenCLI execution ABI without its global plugin discovery.
"""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import subprocess

from openmd.core.preferences import data_directory
from .interpreters import run_interruptibly
from .managed_tools import managed_package_root, node_dir, OPENCLI_VERSION, config_directory, command_environment, managed_command
from .opencli_patches import validate_dispatcher

ADAPTER_FILES = ("play-url.js", "catalog.js", "content.js", "dispatch.js")


def _source() -> Path:
    checkout = Path(__file__).resolve().parents[3] / "integrations/opencli/xiaoe"
    if checkout.is_dir():
        return checkout
    distribution = importlib.metadata.distribution("openmd-cli")
    for file in distribution.files or ():
        if str(file).replace("\\", "/").endswith("share/openmd/opencli/xiaoe/dispatch.js"):
            return Path(distribution.locate_file(file)).parent
    raise RuntimeError("xiaoe_adapter_resources_missing")


def install_xiaoe_adapters(user_dir: Path | None = None) -> Path:
    root = (user_dir or config_directory()) / "openmd-owned/xiaoe"
    root.mkdir(parents=True, exist_ok=True)
    manifest = root / "owned-files.json"
    previous = json.loads(manifest.read_text(encoding="utf-8")) if manifest.is_file() else {}
    source = _source()
    contents = {name: (source / name).read_bytes() for name in ADAPTER_FILES}
    for name in contents:
        target = root / name
        if target.exists():
            digest = hashlib.sha256(target.read_bytes()).hexdigest()
            if digest != previous.get(name) and target.read_bytes() != contents[name]:
                raise ValueError(f"adapter_modified: {name}")
    for name, data in contents.items():
        target = root / name
        if not target.is_file() or target.read_bytes() != data:
            target.write_bytes(data)
    manifest.write_text(json.dumps({name: hashlib.sha256(data).hexdigest() for name, data in contents.items()}, sort_keys=True) + "\n", encoding="utf-8")
    return root


def run_xiaoe(args: list[str], *, timeout: int = 300) -> tuple[int, str, str]:
    """Only three read adapters; no credential file or legacy plugin loading."""
    if args[:2] != ["opencli", "xiaoe"] or len(args) < 4 or args[2] not in {"play-url", "catalog", "content"}:
        return 2, "", "unsupported_xiaoe_command"
    try:
        root = install_xiaoe_adapters()
        host = Path(os.environ["OPENMD_XIAOE_HOST"]) if os.environ.get("OPENMD_XIAOE_HOST") else managed_package_root()
        if json.loads((host / "package.json").read_text(encoding="utf-8"))["version"] != OPENCLI_VERSION:
            return 2, "", "opencli_protocol_version_mismatch"
        validate_dispatcher(host)
        node = node_dir()
        if node is None:
            return 127, "", "node_missing"
        env = command_environment()
        from .services import ensure_opencli_daemon
        ensure_opencli_daemon(managed_command(["xiaoe", args[2]]), env=env)
        command = [str(node / ("node.exe" if os.name == "nt" else "node")), str(root / "dispatch.js"), str(host), args[2], args[3]]
        done = run_interruptibly(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, env=env)
        return done.returncode, done.stdout, done.stderr
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        if isinstance(exc, ValueError) and str(exc).startswith('opencli_patch_'):
            return 1, '', str(exc)
        return 1, "", f"xiaoe_dispatch_failed: {type(exc).__name__}"


def run_tingwu(audio: Path, *, duration: float, timeout: int = 1800) -> tuple[int, str, str]:
    """Run only the packaged cloud audio bridge; never discover legacy plugins."""
    import uuid
    site = "openmd-tingwu-" + uuid.uuid4().hex
    started = False
    try:
        host = Path(os.environ['OPENMD_XIAOE_HOST']) if os.environ.get('OPENMD_XIAOE_HOST') else managed_package_root()
        if json.loads((host / 'package.json').read_text(encoding='utf-8'))['version'] != OPENCLI_VERSION:
            return 1, '{"ok":false,"error_code":"tingwu_protocol_invalid"}', ''
        validate_dispatcher(host)
        node = node_dir()
        if node is None:
            return 1, '{"ok":false,"error_code":"tingwu_browser_unavailable"}', ''
        env = command_environment()
        env["OPENMD_TINGWU_SITE"] = site
        from .services import ensure_opencli_daemon
        ensure_opencli_daemon(managed_command(["openmd-tingwu", "audio"]), env=env)
        command = [str(node / ('node.exe' if os.name == 'nt' else 'node')),
            str(Path(__file__).with_name('tingwu_bridge.mjs')), str(host), str(audio), str(duration), str(timeout)]
        started = True
        done = run_interruptibly(command, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=timeout + 30, env=env)
        # Third-party diagnostic streams are intentionally never returned.
        return done.returncode, done.stdout, ''
    except ValueError as exc:
        code = str(exc) if str(exc).startswith('opencli_patch_') else 'tingwu_browser_unavailable'
        return 1, json.dumps({'ok': False, 'error_code': code}), ''
    except (OSError, RuntimeError, subprocess.SubprocessError):
        return 1, '{"ok":false,"error_code":"tingwu_browser_unavailable"}', ''
    finally:
        # Parent-side fallback also runs when the JS worker is cancelled/killed.
        if started:
            close_tingwu_session("site:" + site)


def close_tingwu_session(session):
    from .services import _bridge_request
    try:
        reply = _bridge_request("close-owned-tab", session=session, surface="adapter")
        return bool(reply.get("ok"))
    except (OSError, ValueError):
        return False


def prepare_bundled_plugins() -> list[str]:
    """Install owned adapters; Node's built-in TS stripper needs no extra dependency.

    No writes through migrated plugin junctions; conflicting user edits fail.
    """
    import shutil
    node = node_dir()
    if node is None:
        raise OSError("managed_opencli_node_missing")
    install_xiaoe_adapters()
    installed = []
    for name in ("openmd-douyin", "openmd-xiaohongshu"):
        source = Path(__file__).resolve().parents[3] / "integrations/opencli" / name
        if not source.is_dir():
            distribution = importlib.metadata.distribution("openmd-cli")
            marker = f"share/openmd/opencli/{name}/package.json"
            source = next((Path(distribution.locate_file(f)).parent for f in distribution.files or () if str(f).replace("\\", "/").endswith(marker)), None)
        if source is None:
            raise OSError("opencli_plugin_resources_missing")
        target = config_directory() / "plugins" / name
        if target.is_symlink() or getattr(target.lstat() if target.exists() else None, "st_reparse_tag", None):
            raise OSError("opencli_plugin_external_link: materialize during migration first")
        target.mkdir(parents=True, exist_ok=True)
        manifest_path = target / "openmd-owned-files.json"
        previous = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else {}
        contents = {}
        for file in source.iterdir():
            if file.name.endswith(".test.ts") or file.suffix not in {".json", ".ts"}:
                continue
            data = file.read_bytes()
            contents[file.name] = data
            if file.suffix == ".ts":
                script = "const fs=require('node:fs');const m=require('node:module');process.stdout.write(m.stripTypeScriptTypes(fs.readFileSync(process.argv[1],'utf8'),{mode:'transform'}));"
                result = run_interruptibly([str(node / ("node.exe" if os.name == "nt" else "node")), "-e", script, str(file)], capture_output=True, timeout=30)
                if result.returncode:
                    raise OSError("opencli_plugin_transpile_failed")
                contents[file.with_suffix(".js").name] = result.stdout
        for name_, data in contents.items():
            file = target / name_
            if file.is_file() and file.read_bytes() != data and hashlib.sha256(file.read_bytes()).hexdigest() != previous.get(name_):
                raise ValueError(f"adapter_modified: {name_}")
        for name_, data in contents.items():
            file = target / name_
            if not file.is_file() or file.read_bytes() != data:
                file.write_bytes(data)
        manifest_path.write_text(json.dumps({k: hashlib.sha256(v).hexdigest() for k, v in contents.items()}, sort_keys=True) + "\n", encoding="utf-8")
        installed.append(target.name)
    return installed
