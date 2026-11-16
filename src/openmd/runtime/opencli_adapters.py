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
from .managed_tools import managed_package_root, node_dir, OPENCLI_VERSION

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
    root = (user_dir or data_directory() / "opencli") / "clis/xiaoe"
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
        node = node_dir()
        if node is None:
            return 127, "", "node_missing"
        env = {**os.environ, "NODE_OPTIONS": "--max-old-space-size=1536", "OPENCLI_CONFIG_DIR": str(root.parents[1])}
        command = [str(node / ("node.exe" if os.name == "nt" else "node")), str(root / "dispatch.js"), str(host), args[2], args[3]]
        done = run_interruptibly(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, env=env)
        return done.returncode, done.stdout, done.stderr
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        return 1, "", f"xiaoe_dispatch_failed: {type(exc).__name__}"
