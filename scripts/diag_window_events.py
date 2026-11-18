"""Read-only 100ms Windows/Chrome evidence around a command (never saves streams).

Usage: python scripts/diag_window_events.py --output evidence.json --timeout 240 -- python -m openmd go URL -o OUTPUT --json
"""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
import urllib.request
import uuid


def chrome_snapshot():
    result = {}
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for method, arguments in (("windows.getAll", [{}]), ("tabs.query", [{}]), ("tabGroups.query", [{}])):
        body = {"id": str(uuid.uuid4()), "action": "chrome-call", "chromeMethod": method,
                "chromeArgs": arguments, "contextId": "openmd"}
        request = urllib.request.Request("http://127.0.0.1:19825/command", data=json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json", "X-OpenCLI": "1"})
        try:
            with opener.open(request, timeout=5) as response:
                payload = json.load(response)
            if not payload.get("ok"):
                result[method] = {"unavailable": payload.get("errorCode", "bridge_rejected")}
                continue
            # Allowlist: no titles, URLs, content, cookies or profile storage.
            keys = {"id", "windowId", "groupId", "focused", "state", "active", "audible", "mutedInfo", "collapsed"}
            result[method] = [{k: v for k, v in entry.items() if k in keys} for entry in payload.get("data", [])]
        except (OSError, ValueError, TypeError):
            result[method] = {"unavailable": "bridge_snapshot_unavailable"}
    return result


def windows():
    if os.name != "nt":
        return {}
    user = ctypes.windll.user32
    kernel = ctypes.windll.kernel32
    user.GetForegroundWindow.restype = wintypes.HWND
    user.IsWindowVisible.argtypes = (wintypes.HWND,)
    user.IsIconic.argtypes = (wintypes.HWND,)
    user.GetWindowRect.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.RECT))
    user.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
    user.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
    user.GetClassNameW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD))
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    foreground = user.GetForegroundWindow()
    screen = (user.GetSystemMetrics(76), user.GetSystemMetrics(77),
              user.GetSystemMetrics(78), user.GetSystemMetrics(79))
    found = {}
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    @callback_type
    def callback(hwnd, _):
        if not user.IsWindowVisible(hwnd):
            return True
        pid = wintypes.DWORD()
        user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        title = ctypes.create_unicode_buffer(1024)
        cls = ctypes.create_unicode_buffer(256)
        user.GetWindowTextW(hwnd, title, len(title))
        user.GetClassNameW(hwnd, cls, len(cls))
        rect = wintypes.RECT()
        user.GetWindowRect(hwnd, ctypes.byref(rect))
        minimized = bool(user.IsIconic(hwnd))
        intersects = rect.right > screen[0] and rect.bottom > screen[1] and rect.left < screen[0] + screen[2] and rect.top < screen[1] + screen[3]
        handle = kernel.OpenProcess(0x1000, False, pid.value)
        executable = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(len(executable))
        try:
            if handle:
                kernel.QueryFullProcessImageNameW(handle, 0, executable, ctypes.byref(size))
        finally:
            if handle:
                kernel.CloseHandle(handle)
        found[str(hwnd)] = {"pid": pid.value, "process": Path(executable.value).name,
                            "class": cls.value, "title_sha256": hashlib.sha256(title.value.encode()).hexdigest(),
                            "minimized": minimized, "rect": [rect.left, rect.top, rect.right, rect.bottom],
                            "screen_visible": not minimized and intersects,
                            "foreground": hwnd == foreground}
        return True
    user.EnumWindows(callback, 0)
    return found


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--timeout", type=float, default=240)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    baseline = windows()
    before = chrome_snapshot()
    started = time.monotonic()
    events = {}
    samples = 0
    audible = set()
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **options)
    timed_out = False
    while process.poll() is None:
        now = time.monotonic() - started
        current = windows()
        for hwnd, entry in current.items():
            if hwnd not in baseline:
                event = events.setdefault(hwnd, {**entry, "appeared_s": round(now, 3), "last_seen_s": 0,
                                                "stole_focus": False, "screen_visible_samples": 0})
                event["last_seen_s"] = round(now, 3)
                event["stole_focus"] |= entry["foreground"]
                event["screen_visible_samples"] += int(entry["screen_visible"])
        samples += 1
        if now > args.timeout:
            # Owned child tree only, through the core cancellation helper.
            from openmd.runtime.interpreters import kill_process_tree
            kill_process_tree(process)
            timed_out = True
            break
        time.sleep(.1)
    process.wait()
    after = chrome_snapshot()
    for tab in after.get("tabs.query", []) if isinstance(after.get("tabs.query"), list) else []:
        if tab.get("audible"):
            audible.add(tab["id"])
    for event in events.values():
        event["observed_span_s"] = round(event["last_seen_s"] - event["appeared_s"] + .1, 3)
        event["screen_visible_sample_s"] = round(event["screen_visible_samples"] * .1, 3)
    payload = {"schema": "openmd.window-diagnostic/v2", "interval_ms": 100, "samples": samples,
               "exit_code": process.returncode, "timed_out": timed_out,
               "seconds": round(time.monotonic() - started, 3), "windows": list(events.values()),
               "chrome_before": before, "chrome_after": after, "audible_after": sorted(audible),
               "limitations": ["Titles are hashes; no command argv or streams saved.",
                               "100ms sampling may miss shorter flashes; audible is a snapshot, not an acoustic measurement."]}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
