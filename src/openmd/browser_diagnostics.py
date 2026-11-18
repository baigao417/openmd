"""Read-only native-host checks; never repair registration or extension files."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

from openmd import __version__
from openmd.browser_setup import EXTENSION_DIR, REGISTRY_KEYS
from openmd.browser_host import HOST_NAME
from openmd.runtime.interpreters import run_interruptibly


REPAIR_ACTION = "运行 openmd browser-setup 并在 chrome://extensions 重新加载"


def registered_manifests() -> dict[str, Path | None]:
    """Read only current-user Chrome/Edge registration, not a guessed data path.

    None denotes an unreadable registration; absent registrations are omitted.
    """
    if os.name != "nt":
        return {}
    import winreg

    result = {}
    for browser, key in zip(("chrome", "edge"), REGISTRY_KEYS):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key.removeprefix("HKCU\\") + "\\" + HOST_NAME) as handle:
                value, kind = winreg.QueryValueEx(handle, "")
            result[browser] = Path(value) if kind == winreg.REG_SZ and isinstance(value, str) and value else None
        except FileNotFoundError:
            continue
        except OSError:
            result[browser] = None
    return result


def _extension_hashes(root: Path) -> dict[str, str]:
    if not (root / "manifest.json").is_file():
        raise FileNotFoundError("extension_manifest_missing")
    return {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob("*") if path.is_file()
            and "__pycache__" not in path.relative_to(root).parts
            and path.suffix not in {".py", ".pyc"}}


def _host_diagnostics(manifest_path: Path | None) -> dict:
    state = {"ready": False, "version_matches": False, "extension_matches": False}
    if manifest_path is None:
        return {**state, "error_code": "host_registration_unreadable"}
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {**state, "error_code": "host_manifest_unreadable"}
    if (not isinstance(manifest, dict) or manifest.get("name") != HOST_NAME
            or manifest.get("type") != "stdio" or not isinstance(manifest.get("path"), str)):
        return {**state, "error_code": "host_manifest_invalid"}
    launcher = Path(manifest["path"])
    if not launcher.is_absolute():
        return {**state, "error_code": "host_manifest_invalid"}
    try:
        lines = [line.strip() for line in launcher.read_text(encoding="utf-8-sig").splitlines()
                 if line.strip() and line.strip().lower() != "@echo off"]
    except (OSError, ValueError):
        return {**state, "error_code": "host_launcher_unreadable"}
    # Only our generated launcher ABI is accepted; never execute arbitrary cmd
    # text while inspecting registration. -I ignores inherited PYTHONPATH.
    match = re.fullmatch(r'"([^"\r\n]+)" -I -m openmd\.browser_host %\*', lines[0]) if len(lines) == 1 else None
    if match is None or not Path(match[1]).is_absolute():
        return {**state, "error_code": "host_launcher_invalid"}
    error = None
    try:
        probe = run_interruptibly([match[1], "-I", "-B", "-c",
                                   "import json,openmd; print(json.dumps({'version':openmd.__version__}))"],
                                  capture_output=True, text=True, timeout=15)
        if probe.returncode:
            error = "host_interpreter_import_failed"
        else:
            version = json.loads(probe.stdout).get("version")
            state["version_matches"] = version == __version__
            if not state["version_matches"]:
                error = "host_version_mismatch"
    except subprocess.TimeoutExpired:
        error = "host_interpreter_timeout"
    except OSError:
        error = "host_interpreter_unavailable"
    except (ValueError, AttributeError):
        error = "host_interpreter_probe_invalid"
    try:
        state["extension_matches"] = _extension_hashes(launcher.parent / "extension") == _extension_hashes(EXTENSION_DIR)
        if not state["extension_matches"] and error is None:
            error = "host_extension_outdated"
    except OSError:
        if error is None:
            error = "host_extension_unreadable"
    state["ready"] = error is None
    if error:
        state["error_code"] = error
    return state


def browser_host_diagnostics() -> dict:
    """Check every registered host; importing its interpreter uses -I -B.

    Registration and file bytes are read only. No native host is started here:
    the ping reply may include a default destination, which doctor never reads.
    An absent optional host does not make core extraction unavailable.
    """
    registrations = registered_manifests()
    hosts = [{"browser": browser, **_host_diagnostics(path)} for browser, path in registrations.items()]
    ready = bool(hosts) and all(host["ready"] for host in hosts)
    return {"supported": os.name == "nt", "registered": bool(hosts), "ready": ready,
            "hosts": hosts, "read_only": True,
            "next_action": "" if ready or os.name != "nt" else REPAIR_ACTION}
