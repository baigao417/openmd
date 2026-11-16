"""Real CPU-only trees; standalone so SIGINT/crash tests cannot interrupt pytest.

No rescue code. An outer harness timeout is a failure, never a passing receipt.
"""

import contextlib
import io
import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from openmd.runtime import interpreters as runtime
from openmd.runtime import tempdirs


def until(predicate, budget=5):
    deadline = time.monotonic() + budget
    while not predicate():
        assert time.monotonic() < deadline, "checkpoint exceeded budget"
        time.sleep(.01)


def proc_process_snapshot(pid, expected_start_time_ticks=None):
    """Best-effort Linux process identity and ownership fields for a failure."""
    if os.name == "nt" or not Path("/proc").is_dir():
        return {"available": False, "reason": "procfs unavailable"}
    stat_path = Path(f"/proc/{int(pid)}/stat")
    status_path = Path(f"/proc/{int(pid)}/status")
    item = {"pid": int(pid), "stat_path": str(stat_path), "status_path": str(status_path)}
    try:
        raw = stat_path.read_text(encoding="ascii", errors="replace")
        fields = raw.rsplit(")", 1)[1].split()
        item["stat"] = {
            "state": fields[0],
            "ppid": int(fields[1]),
            "pgid": int(fields[2]),
            "sid": int(fields[3]),
            "start_time_ticks": int(fields[19]),
        }
        item["expected_start_time_ticks"] = expected_start_time_ticks
        item["identity_matches"] = (
            None if expected_start_time_ticks is None
            else item["stat"]["start_time_ticks"] == expected_start_time_ticks
        )
    except (OSError, IndexError, ValueError) as exc:
        item["stat_error"] = repr(exc)
    try:
        item["status"] = status_path.read_text(encoding="ascii", errors="replace")
    except OSError as exc:
        item["status_error"] = repr(exc)
    item["alive"] = tempdirs.pid_alive(int(pid), start_time=expected_start_time_ticks)
    return item


def proc_group_members(pgid):
    """List current members of a process group without relying on external tools."""
    if os.name == "nt" or not Path("/proc").is_dir():
        return {"available": False, "reason": "procfs unavailable"}
    members = []
    errors = []
    try:
        entries = tuple(Path("/proc").iterdir())
    except OSError as exc:
        return {"available": False, "reason": repr(exc)}
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "stat").read_text(encoding="ascii", errors="replace")
            fields = raw.rsplit(")", 1)[1].split()
            if int(fields[2]) == int(pgid):
                members.append({"pid": int(entry.name), "state": fields[0], "ppid": int(fields[1]), "sid": int(fields[3])})
        except (OSError, IndexError, ValueError) as exc:
            errors.append({"pid": entry.name, "error": repr(exc)})
    result = {"available": True, "pgid": int(pgid), "members": members}
    if errors:
        result["errors"] = errors
    return result


def process_snapshot(pids, start_times=None):
    start_times = start_times or {}
    return {name: proc_process_snapshot(pid, start_times.get(name)) for name, pid in pids.items()}


def posix_process_diagnostics(pids, start_times, killpg_calls):
    """Capture enough state to distinguish group races, session changes and D state."""
    if os.name == "nt":
        return {"available": False, "reason": "non-Windows diagnostics only"}
    snapshot = process_snapshot(pids, start_times)
    last_target = killpg_calls[-1].get("target") if killpg_calls else None
    for item in snapshot.values():
        stat = item.get("stat")
        if stat is not None and last_target is not None:
            item["matches_last_killpg_group"] = stat["pgid"] == last_target
    return {
        "available": Path("/proc").is_dir(),
        "controller": proc_process_snapshot(os.getpid()),
        "processes": snapshot,
        "current_group": proc_group_members(last_target) if last_target is not None else None,
        "killpg_calls": killpg_calls,
    }



def process_ownership_diagnostics(pids, created):
    """Capture Windows process/job state before TemporaryDirectory cleanup.

    The scenario deliberately keeps this best-effort: diagnostics must still
    be emitted when a runner has already torn down a process or denies a query.
    """
    result = {"processes": {}, "owned_jobs": [], "current_process": {}}
    if os.name != "nt":
        return {"available": False, "reason": "non-Windows"}
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    get_current_process = kernel.GetCurrentProcess
    get_current_process.restype = wintypes.HANDLE
    is_process_in_job = kernel.IsProcessInJob
    is_process_in_job.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
    is_process_in_job.restype = wintypes.BOOL
    open_process = kernel.OpenProcess
    open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    open_process.restype = wintypes.HANDLE
    close_handle = kernel.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL
    query_job = kernel.QueryInformationJobObject
    query_job.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p]
    query_job.restype = wintypes.BOOL

    try:
        from openmd.runtime import _windows_job as win
        for index, child in enumerate(created):
            owner = getattr(child, "_openmd_owner", None)
            handle = getattr(owner, "handle", None)
            item = {"index": index, "pid": getattr(child, "pid", None), "handle_open": bool(handle)}
            if handle:
                info = win.EXTENDED_LIMITS()
                if query_job(handle, 9, ctypes.byref(info), ctypes.sizeof(info), None):
                    flags = int(info.BasicLimitInformation.LimitFlags)
                    item["limit_flags"] = flags
                    item["kill_on_job_close"] = bool(flags & 0x2000)
                    item["breakaway_ok"] = bool(flags & 0x800)
                    item["silent_breakaway_ok"] = bool(flags & 0x1000)
                else:
                    item["query_error"] = ctypes.get_last_error()
            result["owned_jobs"].append(item)
        current = {"pid": os.getpid()}
        in_job = wintypes.BOOL()
        if is_process_in_job(get_current_process(), None, ctypes.byref(in_job)):
            current["in_job"] = bool(in_job.value)
            current["job_limit_flags"] = None
            current["job_limit_flags_note"] = "current job handle is not exposed by Win32 API"
        else:
            current["in_job_error"] = ctypes.get_last_error()
        result["current_process"] = current
    except BaseException as exc:
        result["api_error"] = repr(exc)

    for name, pid in pids.items():
        item = {"pid": pid, "alive": tempdirs.pid_alive(pid)}
        handle = open_process(0x1000, False, int(pid))
        if handle:
            try:
                memberships = []
                for job in result["owned_jobs"]:
                    child = created[job["index"]]
                    owner = getattr(getattr(child, "_openmd_owner", None), "handle", None)
                    if not owner:
                        continue
                    member = wintypes.BOOL()
                    if is_process_in_job(handle, owner, ctypes.byref(member)):
                        memberships.append(bool(member.value))
                item["in_owned_jobs"] = memberships
            finally:
                close_handle(handle)
        else:
            item["open_error"] = ctypes.get_last_error()
        result["processes"][name] = item

    # WMI gives the command line and parent PID without adding a dependency.
    try:
        ids = ",".join(str(int(pid)) for pid in pids.values())
        query = (
            "$ids=@(" + ids + "); "
            "Get-CimInstance Win32_Process | Where-Object { $ids -contains $_.ProcessId } | "
            "Select-Object ProcessId,ParentProcessId,CommandLine | ConvertTo-Json -Compress"
        )
        probe = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", query],
                               capture_output=True, text=True, timeout=5)
        result["wmi_returncode"] = probe.returncode
        result["wmi"] = json.loads(probe.stdout) if probe.stdout.strip() else []
        if probe.stderr.strip():
            result["wmi_stderr"] = probe.stderr.strip()
    except BaseException as exc:
        result["wmi_error"] = repr(exc)
    result["available"] = True
    return result


def job_limit_flags(handle):
    if os.name != "nt" or not handle:
        return None
    import ctypes
    from openmd.runtime import _windows_job as win
    info = win.EXTENDED_LIMITS()
    if not win.QueryInformationJobObject(handle, 9, ctypes.byref(info), ctypes.sizeof(info), None):
        return {"query_error": ctypes.get_last_error()}
    flags = int(info.BasicLimitInformation.LimitFlags)
    return {
        "limit_flags": flags,
        "kill_on_job_close": bool(flags & 0x2000),
        "breakaway_ok": bool(flags & 0x800),
        "silent_breakaway_ok": bool(flags & 0x1000),
    }


def tree_command(marker, *, leader_wait=False, inherit_pipes=True):
    leaf = "import time;time.sleep(30)"
    middle = (
        "import subprocess,sys,os,json,time;from pathlib import Path;"
        "p=subprocess.Popen([sys.executable,'-c',sys.argv[3]]);"
        "Path(sys.argv[1]).write_text(json.dumps({'root':int(sys.argv[2]),'middle':os.getpid(),'leaf':p.pid}));"
        "time.sleep(30)"
    )
    leader = (
        "import subprocess,sys,os,time;from pathlib import Path;"
        "p=subprocess.Popen([sys.executable,'-c',sys.argv[2],sys.argv[1],str(os.getpid()),sys.argv[3]]"
        + (");" if inherit_pipes else ",stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);")
        + "marker=Path(sys.argv[1]);"
        "\nwhile not marker.exists(): time.sleep(.01)\n"
        + ("time.sleep(30)" if leader_wait else "")
    )
    return [sys.executable, "-c", leader, str(marker), middle, leaf]


def run_case(case, root):
    marker = root / "pids.json"
    record = {"case": case}
    created = []
    killpg_calls = []
    known_pids = {}
    known_start_times = {}
    launch = runtime.owned_popen
    original_killpg = os.killpg if os.name != "nt" else None

    def remember_pids(pids):
        known_pids.update(pids)
        for name, pid in pids.items():
            known_start_times.setdefault(name, tempdirs.pid_start_time(pid))
        return {name: known_start_times[name] for name in pids}

    def record_launch(*args, **kwargs):
        child = launch(*args, **kwargs)
        created.append(child)
        owner = getattr(child, "_openmd_owner", None)
        flags = job_limit_flags(getattr(owner, "handle", None))
        if flags is not None:
            record.setdefault("owned_jobs_at_launch", []).append({"pid": child.pid, **flags})
        return child

    def record_killpg(pgid, sig):
        call = {
            "target": pgid,
            "signal": sig,
            "before": process_snapshot(known_pids, known_start_times),
            "group_before": proc_group_members(pgid),
            "started_at": time.monotonic(),
        }
        try:
            result = original_killpg(pgid, sig)
        except BaseException as exc:
            call["exception"] = repr(exc)
            raise
        else:
            call["return_value"] = repr(result)
            return result
        finally:
            call["after"] = process_snapshot(known_pids, known_start_times)
            call["group_after"] = proc_group_members(pgid)
            call["elapsed_s"] = time.monotonic() - call["started_at"]
            killpg_calls.append(call)

    runtime.owned_popen = record_launch
    if original_killpg is not None:
        os.killpg = record_killpg
    interrupt_errors = []
    before_threads = set(threading.enumerate())
    is_file = case.startswith("file-")
    is_sigint = case.endswith("sigint")
    leader_wait = is_file and case != "file-normal"
    command = tree_command(marker, leader_wait=leader_wait, inherit_pipes=case != "pipe-normal")

    if os.name == "nt":
        import ctypes
        ctypes.windll.kernel32.SetConsoleCtrlHandler(None, False)
    signal.signal(signal.SIGINT, signal.default_int_handler)

    def interrupt():
        try:
            until(marker.exists)
            pids = json.loads(marker.read_text())
            start_times = remember_pids(pids)
            if not leader_wait:
                until(lambda: not tempdirs.pid_alive(pids["root"], start_time=start_times["root"]))
            record["root_dead_before_interrupt"] = not tempdirs.pid_alive(pids["root"], start_time=start_times["root"])
            record["alive_before_interrupt"] = {k: tempdirs.pid_alive(p, start_time=start_times[k]) for k, p in pids.items()}
            record["signal_at"] = time.monotonic()
            signal.raise_signal(signal.SIGINT)
        except BaseException as exc:
            interrupt_errors.append(repr(exc))

    def workload(args=None):
        if case == "browser-cancel":
            from openmd import browser_host
            status = {"id": "synthetic", "state": "running"}
            browser_host.read_status = lambda job_id: json.loads((root / "status.json").read_text())

            def cancel():
                try:
                    until(marker.exists)
                    pids = json.loads(marker.read_text())
                    start_times = remember_pids(pids)
                    until(lambda: not tempdirs.pid_alive(pids["root"], start_time=start_times["root"]))
                    record["root_dead_before_cancel"] = True
                    record["alive_before_cancel"] = {k: tempdirs.pid_alive(p, start_time=start_times[k]) for k, p in pids.items()}
                    browser_host._write_status(root, {**status, "state": "cancelled"})
                except BaseException as exc:
                    interrupt_errors.append(repr(exc))

            thread = threading.Thread(target=cancel, daemon=True)
            thread.start()
            result = browser_host._run_openmd(command, root, status)
            thread.join(1)
            assert record["root_dead_before_cancel"]
            return result.returncode
        if case == "layout-normal":
            from openmd.ocr import layout
            layout.layout_runtime = lambda: (Path(sys.executable), None)
            layout.worker_command = lambda *a, **k: command
            layout._run([], 8, root)
            return 0
        if case == "tool-normal":
            from types import SimpleNamespace
            from openmd.routing import runner
            runner.resolve_launcher = lambda card: None
            runner.build_argv = lambda *a, **k: command
            return runner.run_tool({}, {"timeout_s": 8}, SimpleNamespace(url="https://example.org", id="synthetic"), root).exit_code
        if is_file:
            with (root / "worker.log").open("wb") as log:
                child = runtime.owned_popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=log)
                try:
                    return runtime.wait_interruptibly(child, .9 if case == "file-timeout" else 8)
                finally:
                    record["leader_exit"] = child.returncode
                    record["job_closed"] = getattr(child._openmd_owner, "handle", None) is None if os.name == "nt" else child._openmd_owner.closed
        return runtime.run_interruptibly(command, capture_output=True, timeout=.9 if case == "pipe-timeout" else 8).returncode

    interrupter = threading.Thread(target=interrupt, daemon=True) if is_sigint else None
    started = time.monotonic()
    if interrupter:
        interrupter.start()
    error_stream = io.StringIO()
    try:
        if is_sigint:
            import openmd.cli as cli
            cli.cmd_ingest = workload
            with contextlib.redirect_stderr(error_stream):
                record["exit_code"] = cli.main(["ingest", "https://example.org/synthetic", "--out", str(root)])
            assert record["exit_code"] == 130
            assert "已取消。" in error_stream.getvalue()
            record["stderr"] = error_stream.getvalue()
            record["seconds_after_signal"] = time.monotonic() - record["signal_at"]
            assert record["seconds_after_signal"] < 2.5
            if not is_file:
                assert record["root_dead_before_interrupt"]
                assert record["alive_before_interrupt"]["middle"] and record["alive_before_interrupt"]["leaf"]
        else:
            try:
                result = workload()
            except subprocess.TimeoutExpired:
                record["timed_out"] = True
            else:
                record["leader_exit"] = result
            assert record.get("timed_out", False) == case.endswith("timeout")
        record["elapsed_s"] = time.monotonic() - started
        assert record["elapsed_s"] < 4
        pids = json.loads(marker.read_text())
        start_times = remember_pids(pids)
        deadline = time.monotonic() + 2
        while any(tempdirs.pid_alive(p, start_time=start_times[k]) for k, p in pids.items()) and time.monotonic() < deadline:
            time.sleep(.01)
        record["pids"] = pids
        record["alive_after"] = {k: tempdirs.pid_alive(p, start_time=start_times[k]) for k, p in pids.items()}
        if os.name != "nt":
            record["process_diagnostics"] = posix_process_diagnostics(pids, start_times, killpg_calls)
        if any(record["alive_after"].values()):
            if os.name == "nt":
                record["process_diagnostics"] = process_ownership_diagnostics(pids, created)
            print(json.dumps(record, ensure_ascii=False), flush=True)
            assert not any(record["alive_after"].values()), record["process_diagnostics"]
        if interrupter:
            interrupter.join(1)
        assert not interrupt_errors, interrupt_errors
        until(lambda: not any(t not in before_threads and t.is_alive() for t in threading.enumerate()), budget=1)
        record["extra_io_threads"] = []
        record["pipe_handles_closed"] = all(stream.closed for child in created
            for stream in (child.stdin, child.stdout, child.stderr) if stream is not None)
        assert record["pipe_handles_closed"]
        assert "external_rescue_required" not in record
        return record
    finally:
        runtime.owned_popen = launch
        if original_killpg is not None:
            os.killpg = original_killpg


def marker_lifecycle(root):
    """155 real sequential child lifetimes; controller then exits for real."""
    work = Path(tempdirs.owned_mkdtemp(prefix="openmd-lifetimes-", dir=root))
    pids = []
    children = []
    max_bytes = 0
    max_records = 0
    for _ in range(155):
        child = runtime.owned_popen([sys.executable, "-c", "pass"], stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        pids.append(child.pid)
        children.append(child)
        marker = work / tempdirs.OWNER_FILE
        max_bytes = max(max_bytes, marker.stat().st_size)
        max_records = max(max_records, len(json.loads(marker.read_text()).get("pids", [])))
        assert runtime.wait_interruptibly(child, 5, poll_s=.01) == 0
    # A dead incoming PID must still prune previously completed workers.
    tempdirs.note_process_owner(child)
    final = json.loads(marker.read_text())
    assert final["pids"] == [os.getpid()]
    assert max_bytes <= tempdirs.MAX_OWNER_BYTES
    assert max_records <= tempdirs.MAX_OWNER_PIDS
    return {"case": "marker-lifetimes", "directory": str(work), "pids": pids,
            "child_returncodes": [child.returncode for child in children],
            "controller": os.getpid(), "max_marker_bytes": max_bytes,
            "max_records": max_records, "final": final}


if __name__ == "__main__":
    case = sys.argv[1]
    if case == "marker-lifetimes":
        result = marker_lifecycle(Path(sys.argv[2]))
    elif case == "crash-controller":
        child = runtime.owned_popen(tree_command(Path(sys.argv[2]), leader_wait=True))
        until(Path(sys.argv[2]).exists)
        os._exit(17)  # No Python cleanup; Job must close at OS process teardown.
    elif case == "crash-during-launch":
        import ctypes as C
        from openmd.runtime import _windows_job as win
        get_pid = win._api("GetProcessId", win.W.DWORD, win.W.HANDLE)
        def crash(job, process):
            Path(sys.argv[2]).write_text(json.dumps({"root": get_pid(process)}))
            os._exit(18)  # Before explicit Assign/Resume; atomic JOB_LIST owns it.
        win.AssignProcessToJobObject = crash
        runtime.owned_popen([sys.executable, "-c", "import time;time.sleep(30)"])
    else:
        box = Path(tempfile.mkdtemp(prefix="openmd-ownership-test-"))
        try:
            result = run_case(case, box)
        except BaseException:
            shutil.rmtree(box, ignore_errors=True)
            raise
        try:
            shutil.rmtree(box)
        except BaseException as exc:
            result["cleanup_error"] = repr(exc)
            result["cleanup_diagnostics"] = process_ownership_diagnostics(result.get("pids", {}), [])
            print(json.dumps(result, ensure_ascii=False), flush=True)
            raise
    print(json.dumps(result, ensure_ascii=False), flush=True)
