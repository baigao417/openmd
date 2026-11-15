"""Owned, recoverable temp directories; sweep only direct Openmd children of system temp."""

from __future__ import annotations

import ctypes
import json
import os
import shutil
import stat
import tempfile
import threading
import time
from pathlib import Path

OWNER_FILE = ".openmd-owner.json"
MAX_AGE_S = 24 * 60 * 60
MAX_OWNER_PIDS = 256
MAX_OWNER_BYTES = 4096
_startup_report = None
_owned_directories: set[Path] = set()
_owner_lock = threading.RLock()


def pid_alive(pid: int) -> bool:
    """Treat inaccessible process state as live. Never use Windows os.kill(pid, 0)."""
    if pid <= 0:
        return False
    if os.name == "nt":
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return ctypes.get_last_error() != 87  # invalid parameter means no such PID
        try:
            code = wintypes.DWORD()
            return not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # A killed-but-unreaped (zombie) process still answers signal 0, yet holds
    # no files and never runs again; orphans wait for init to reap them.
    try:
        with open(f"/proc/{pid}/stat", encoding="ascii", errors="replace") as stat:
            state = stat.read().rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        return True  # no /proc (macOS) or vanished mid-read: keep the safe answer
    return state not in {"Z", "X"}


def _read_owner(marker: Path) -> dict:
    """One bounded schema/size contract for readers and writers."""
    if _linked(marker):
        raise ValueError("linked owner marker")
    with marker.open("rb") as source:
        data = source.read(MAX_OWNER_BYTES + 1)
    if len(data) > MAX_OWNER_BYTES:
        raise ValueError("oversized owner marker")
    owner = json.loads(data)
    pid, pids = owner.get("pid"), owner.get("pids", [])
    if not isinstance(pids, list) or len(pids) > MAX_OWNER_PIDS or not all(
        isinstance(p, int) and not isinstance(p, bool) and 0 < p <= 0xFFFFFFFF
        for p in [pid, *pids]
    ) or not isinstance(owner.get("incomplete", False), bool):
        raise ValueError("invalid owner marker")
    return owner


def _write_owner(marker: Path, owner: dict) -> None:
    data = json.dumps(owner, separators=(",", ":")).encode("utf-8")
    if len(data) > MAX_OWNER_BYTES or len(owner.get("pids", [])) > MAX_OWNER_PIDS:
        raise ValueError("owner marker exceeds shared contract")
    # Readers see the old or the new complete record, never a truncated write.
    fd, name = tempfile.mkstemp(prefix=OWNER_FILE + ".", suffix=".tmp", dir=marker.parent)
    try:
        with os.fdopen(fd, "wb") as destination:
            destination.write(data)
        os.replace(name, marker)
    finally:
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass


def _mark(directory) -> None:
    try:
        _write_owner(Path(directory) / OWNER_FILE, {"pid": os.getpid()})
        with _owner_lock:
            _owned_directories.add(Path(directory).resolve())
    except OSError:
        # Failure to establish ownership must never leave an unprotected live directory.
        shutil.rmtree(directory, ignore_errors=True)
        raise


def owned_mkdtemp(*args, **kwargs):
    directory = tempfile.mkdtemp(*args, **kwargs)
    _mark(directory)
    return directory


def owned_temporary_directory(*args, **kwargs):
    directory = tempfile.TemporaryDirectory(*args, **kwargs)
    _mark(directory.name)
    return directory


def note_process_owner(process) -> None:
    """Protect active workspaces even if the controller dies before its workers.

    A conservative association with all active workspaces in this controller is
    intentional: retain an extra directory rather than remove a live worker's
    input. Prune exited owners on EVERY write, including duplicate/dead inputs.
    Inaccessible PIDs count as alive; true simultaneous overflow is unknown.
    """
    pid = getattr(process, "pid", None)
    if not isinstance(pid, int) or pid <= 0:
        return
    with _owner_lock:
        for directory in tuple(_owned_directories):
            marker = directory / OWNER_FILE
            try:
                if not marker.is_file():
                    _owned_directories.discard(directory)
                    continue
                owner = _read_owner(marker)
                candidates = dict.fromkeys([owner["pid"], *owner.get("pids", []), os.getpid(), pid])
                pids = [p for p in candidates if pid_alive(p)]
                incomplete = owner.get("incomplete", False) or len(pids) > MAX_OWNER_PIDS
                updated = {"pid": os.getpid(), "pids": pids[:MAX_OWNER_PIDS]}
                if incomplete:
                    # Never silently drop untracked live ownership. Such rare
                    # overflow stays unknown until an explicit manual cleanup.
                    updated["incomplete"] = True
                _write_owner(marker, updated)
            except (OSError, ValueError, AttributeError, TypeError):
                pass  # Unreadable/malformed markers are retained by the sweeper.


def _linked(path: Path) -> bool:
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _size(path: Path) -> int:
    total = 0
    try:
        for child in path.iterdir():
            if not _linked(child):
                total += _size(child) if child.is_dir() else child.stat().st_size
    except OSError:
        pass
    return total


def sweep_tempdirs(*, include_sizes: bool = True) -> dict:
    root = Path(tempfile.gettempdir()).resolve()
    report = {"root": str(root), "deleted": 0, "retained": 0, "failed": 0, "deleted_bytes": 0, "retained_bytes": 0, "sizes_measured": include_sizes, "errors": []}
    now = time.time()
    try:
        with os.scandir(root) as entries:
            for entry in entries:
                if not entry.name.startswith("openmd-"):
                    continue
                path = Path(entry.path)
                try:
                    # Check lexical and resolved parent immediately before any removal.
                    if _linked(path) or not entry.is_dir(follow_symlinks=False) or path.resolve().parent != root:
                        continue
                    age = now - entry.stat(follow_symlinks=False).st_mtime
                    size = _size(path) if include_sizes else 0
                    retain = age <= MAX_AGE_S
                    marker = path / OWNER_FILE
                    if not retain and marker.exists():
                        # Invalid/unreadable markers are unknown ownership, so retain.
                        try:
                            owner = _read_owner(marker)
                            retain = owner.get("incomplete", False) or any(pid_alive(p) for p in [owner["pid"], *owner.get("pids", [])])
                        except (OSError, ValueError, AttributeError):
                            retain = True
                    if retain:
                        report["retained"] += 1
                        report["retained_bytes"] += size
                        continue
                    # Revalidate after ownership and size inspection; never follow a swapped link.
                    if _linked(path) or path.resolve().parent != root:
                        continue
                    try:
                        shutil.rmtree(path)
                    except OSError:
                        report["retained"] += 1
                        report["retained_bytes"] += size
                        raise
                    report["deleted"] += 1
                    report["deleted_bytes"] += size
                except OSError as exc:
                    report["failed"] += 1
                    report["errors"].append({"directory": entry.name, "error": type(exc).__name__})
    except OSError as exc:
        report["failed"] += 1
        report["errors"].append({"error": type(exc).__name__})
    return report


def sweep_at_startup() -> dict:
    global _startup_report
    if _startup_report is None:
        # Never delay the CLI for directory removal or size scans. An interrupted
        # sweep is safe to retry on the next startup; doctor runs synchronously.
        report = {"started": True}
        _startup_report = report

        def sweep():
            report.update(sweep_tempdirs(include_sizes=False))

        threading.Thread(target=sweep, name="openmd-temp-sweep", daemon=True).start()
    return _startup_report
