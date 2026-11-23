"""The sole persistent-process exception: the installed OpenCLI browser daemon.

Actual commands, status checks and all other workers retain launch-time ownership.
No Ollama lifecycle operations, model downloads or general detached launcher exist.
"""

from __future__ import annotations

import os
from pathlib import Path
import shlex
import shutil
import subprocess
import threading
import time

from openmd.core.opencli_output import load_opencli_json

_START_LOCK = threading.Lock()
_DAEMONS: list[subprocess.Popen] = []
_CONTROL_COMMANDS = {"daemon", "--help", "-h", "--version", "-V", "help", "list", "completion"}
_BRIDGE_PREPARED = False


def _opencli_launch(command, env=None) -> tuple[list[str], list[str]] | None:
    """Recognize npm shims and native Node entries, without executing a shell."""
    if isinstance(command, str):
        try:
            argv = [part.strip('"') for part in shlex.split(command, posix=False)]
        except ValueError:
            return None
    elif isinstance(command, (list, tuple)):
        argv = [os.fsdecode(part) for part in command]
    else:
        return None
    if not argv:
        return None
    first = Path(argv[0])
    if first.name.lower() in {"opencli", "opencli.cmd", "opencli.ps1", "opencli.exe", "opencli.bat"}:
        resolved = shutil.which(argv[0], path=None if env is None else env.get("PATH"))
        shim = Path(resolved or argv[0]).resolve()
        # Unix npm installs symlink the command directly to the package entry.
        if shim.as_posix().lower().endswith("/@jackwener/opencli/dist/src/main.js"):
            entry = shim
        elif shim.parent.name == ".bin":  # local install, e.g. Openmd's managed copy
            entry = shim.parent.parent / "@jackwener/opencli/dist/src/main.js"
        else:
            entry = shim.parent / "node_modules/@jackwener/opencli/dist/src/main.js"
        node = shim.parent / ("node.exe" if os.name == "nt" else "node")
        binary = str(node) if node.is_file() else shutil.which("node", path=None if env is None else env.get("PATH"))
        tail = argv[1:]
    elif first.name.lower() in {"node", "node.exe"} and len(argv) > 1:
        entry = Path(argv[1]).resolve()
        if not entry.as_posix().lower().endswith("/@jackwener/opencli/dist/src/main.js"):
            return None
        binary, tail = argv[0], argv[2:]
    else:
        return None
    # Status/stop/restart must never start the daemon as a side effect. The
    # recursive owned status command below consequently bypasses this helper.
    while tail and tail[0].startswith("--profile="):
        tail = tail[1:]
    if tail and tail[0] == "--profile":
        tail = tail[2:]
    if not tail or tail[0] in _CONTROL_COMMANDS:
        return None
    from .managed_tools import command_uses_browser
    if command_uses_browser(tail, package_root=entry.parents[2]) is False:
        return None
    daemon = entry.with_name("daemon.js")
    if not binary or not entry.is_file() or not daemon.is_file():
        return None
    return [str(binary), str(entry)], [str(binary), str(daemon)]


def _daemon_running(launcher, *, env=None, cwd=None) -> bool:
    from .interpreters import run_interruptibly

    try:
        result = run_interruptibly([*launcher, "daemon", "status"], env=env, cwd=cwd,
                                  stdin=subprocess.DEVNULL, capture_output=True,
                                  text=True, encoding="utf-8", errors="replace", timeout=10,
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        payload = load_opencli_json(result.stdout)
        if result.returncode or not isinstance(payload, dict) or not isinstance(payload.get("running"), bool):
            raise ValueError("invalid status")
        return payload["running"]
    except (ValueError, TypeError, subprocess.TimeoutExpired) as exc:
        # Do not blindly launch a second service when the status is unknown.
        raise OSError("OpenCLI daemon status unavailable; run opencli daemon status.") from exc


def _start_opencli_daemon_detached(daemon_argv, *, env=None):
    """Only called with the two-item installed Node/daemon.js launch specification.

    This is deliberately outside owned_popen: a command's kill-on-close Job must
    never acquire this intentional persistent service. No Job flag is weakened.
    """
    binary, script = daemon_argv
    if Path(binary).name.lower() not in {"node", "node.exe"} or not Path(script).as_posix().lower().endswith("/@jackwener/opencli/dist/src/daemon.js"):
        raise ValueError("Only the installed OpenCLI daemon may be detached")
    from .interpreters import process_options
    options = process_options()
    if os.name == "nt":
        options["creationflags"] |= subprocess.CREATE_BREAKAWAY_FROM_JOB
    process = subprocess.Popen([binary, script], env=env, cwd=str(Path(script).parent),
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, close_fds=True, **options)
    _DAEMONS[:] = [child for child in _DAEMONS if child.poll() is None]
    _DAEMONS.append(process)
    return process


OPENMD_BRIDGE_PROFILE_PREFIX = "openmd-"


def _bridge_profiles(timeout: float = 1.0) -> list[str]:
    """Connected Browser Bridge profile ids from the local daemon (never proxied)."""
    import json
    import urllib.request

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request("http://127.0.0.1:19825/status", headers={"X-OpenCLI": "1"})
    try:
        with opener.open(request, timeout=timeout) as response:  # noqa: S310 - loopback only
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError):
        return []
    return [str(p.get("contextId")) for p in payload.get("profiles") or [] if isinstance(p, dict) and p.get("contextId")]


def bridge_profile_env(command, env=None):
    """Select Openmd's built-in bridge when it is connected.

    With both the standalone OpenCLI extension and Openmd's built-in bridge
    connected, opencli refuses to guess (profile_required). Prefer ours, keep a
    user's explicit OPENCLI_PROFILE, and leave the single-bridge case untouched.
    Returns a new env mapping, or None when nothing needs to change.
    """
    base = os.environ if env is None else env
    if base.get("OPENCLI_PROFILE") or _opencli_launch(command, env) is None:
        return None
    deadline = time.monotonic() + 2
    profiles = _bridge_profiles()
    while not profiles and time.monotonic() < deadline:  # bridges reconnect to a fresh daemon
        time.sleep(.2)
        profiles = _bridge_profiles()
    openmd_profiles = [profile for profile in profiles if profile.startswith(OPENMD_BRIDGE_PROFILE_PREFIX)]
    if len(openmd_profiles) == 1 and len(profiles) > 1:
        return {**base, "OPENCLI_PROFILE": openmd_profiles[0]}
    if len(openmd_profiles) > 1:
        # Fail closed: selecting an arbitrary Openmd browser would silently use
        # whichever user's login state happened to win the connection race.
        return {**base, "OPENCLI_PROFILE": "openmd-profile-conflict"}
    return None


def ensure_opencli_daemon(command, *, env=None, cwd=None) -> None:
    launch = _opencli_launch(command, env)
    if launch is None:
        return
    check_daemon_version()
    launcher, daemon_argv = launch
    # Lock waits stay in short Python slices, like the owned process waiter.
    while not _START_LOCK.acquire(timeout=.1):
        pass
    # Pre-start is only an optimization (keep the daemon alive across owned
    # commands). Unknown status or a slow/failed start must not block the actual
    # command: it then runs exactly as before and reports its own errors.
    try:
        try:
            if _daemon_running(launcher, env=env, cwd=cwd):
                prepare_openmd_bridge(env)
                return
        except OSError:
            return  # status unknown: never launch a second service blindly
        process = _start_opencli_daemon_detached(daemon_argv, env=env)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                if _daemon_running(launcher, env=env, cwd=cwd):
                    prepare_openmd_bridge(env)
                    return
            except OSError:
                pass
            if process.poll() is not None:
                return
            time.sleep(.1)
    finally:
        _START_LOCK.release()


def daemon_status(timeout: float = 1.0):
    """Read the loopback daemon identity; no proxy, cookies or profile storage."""
    import json
    import urllib.error
    import urllib.request
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        request = urllib.request.Request("http://127.0.0.1:19825/status", headers={"X-OpenCLI": "1"})
        with opener.open(request, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.URLError as exc:
        import errno
        if getattr(exc.reason, "errno", None) in {errno.ECONNREFUSED, 10061} or getattr(exc.reason, "winerror", None) == 10061:
            return None
        raise OSError("opencli_daemon_status_unavailable") from exc
    except (OSError, ValueError) as exc:
        raise OSError("opencli_daemon_status_unavailable") from exc


def check_daemon_version():
    from .managed_tools import OPENCLI_VERSION
    status = daemon_status()
    if status is not None and status.get("daemonVersion") != OPENCLI_VERSION:
        # Never stop or replace another installation's service.
        version = str(status.get("daemonVersion", "unknown"))[:32]
        raise OSError(f"opencli_daemon_foreign: daemon {version}; expected {OPENCLI_VERSION}; close the other OpenCLI first")


def _bridge_request(action, **fields):
    import json
    import uuid
    import urllib.request
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    profiles = [profile for profile in _bridge_profiles() if profile.startswith(OPENMD_BRIDGE_PROFILE_PREFIX)]
    if len(profiles) != 1:
        raise OSError("openmd_chrome_bridge_profile_conflict" if profiles else "openmd_chrome_bridge_unavailable")
    request = urllib.request.Request("http://127.0.0.1:19825/command",
        data=json.dumps({"id": str(uuid.uuid4()), "contextId": profiles[0], "action": action, **fields}).encode(),
        headers={"Content-Type": "application/json", "X-OpenCLI": "1"})
    with opener.open(request, timeout=5) as response:
        return json.load(response)


def prepare_openmd_bridge(env=None):
    """Configure only our bridge; old loaded extensions get an honest deferred receipt."""
    global _BRIDGE_PREPARED
    selected_profile = (env or os.environ).get("OPENCLI_PROFILE")
    if selected_profile and not selected_profile.startswith(OPENMD_BRIDGE_PROFILE_PREFIX):
        return
    from openmd.core.local_config import load_local_config
    from openmd.core.preferences import data_directory
    from .tempdirs import pid_alive
    import json
    base = os.environ if env is None else env
    settings = load_local_config().get("browser", {})
    try:
        reply = _bridge_request("openmd-configure", ownerPid=os.getpid(),
            idleMs=float(base.get("OPENMD_BROWSER_IDLE_SECONDS", settings.get("idle_timeout_seconds", 120))) * 1000,
            sound=base.get("OPENMD_BROWSER_SOUND") == "1", show=base.get("OPENMD_SHOW_BROWSER") == "1",
            windowStrategy=base.get("OPENMD_BROWSER_WINDOW_STRATEGY", settings.get("window_strategy", "existing-window")))
        if not reply.get("ok"):
            receipt = {"status": "joint_reload_required", "tabsClosed": None, "windowsClosed": None, "failures": None}
        else:
            if _BRIDGE_PREPARED:
                return
            leases = _bridge_request("openmd-owned-leases").get("data", [])
            dead = [row["ownerPid"] for row in leases if type(row.get("ownerPid")) is int and row["ownerPid"] > 0 and not pid_alive(row["ownerPid"])]
            swept = _bridge_request("openmd-cleanup-orphans", deadPids=dead)
            receipt = {"status": "verified" if swept.get("ok") else "failed", **swept.get("data", {})}
            begun = _bridge_request("openmd-begin-job", ownerPid=os.getpid())
            if not begun.get("ok"):
                receipt["status"] = "failed"
        _BRIDGE_PREPARED = True
    except (OSError, ValueError, TypeError):
        receipt = {"status": "bridge_unavailable", "tabsClosed": None, "windowsClosed": None, "failures": None}
    # A separate browser lifecycle runlog avoids changing item result schemas.
    path = data_directory() / "browser-cleanup.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"event_type": "browser_cleanup", "profile": selected_profile or "openmd-auto", **receipt}) + "\n")


def finish_openmd_bridge():
    """Close this controller's owned leases and job anchor; never a user window."""
    global _BRIDGE_PREPARED
    if not _BRIDGE_PREPARED:
        return
    _BRIDGE_PREPARED = False
    from openmd.core.preferences import data_directory
    import json
    try:
        reply = _bridge_request("openmd-end-job", ownerPid=os.getpid())
        data = reply.get("data", {})
        status = "joint_reload_required" if not reply.get("ok") else "partial" if data.get("remainingOwned") or data.get("failures") else "verified"
        receipt = {"status": status, **data}
    except (OSError, ValueError, TypeError):
        receipt = {"status": "cleanup_unavailable"}
    try:
        path = data_directory() / "browser-cleanup.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"event_type": "browser_cleanup", "phase": "finish", "profile": os.environ.get("OPENCLI_PROFILE") or "openmd-auto", **receipt}) + "\n")
    except OSError:
        pass
