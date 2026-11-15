"""Real CPU-only trees; standalone so SIGINT/crash tests cannot interrupt pytest.

No rescue code. An outer harness timeout is a failure, never a passing receipt.
"""

import contextlib
import io
import json
import os
from pathlib import Path
import signal
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
    launch = runtime.owned_popen

    def record_launch(*args, **kwargs):
        child = launch(*args, **kwargs)
        created.append(child)
        return child

    runtime.owned_popen = record_launch
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
            if not leader_wait:
                until(lambda: not tempdirs.pid_alive(pids["root"]))
            record["root_dead_before_interrupt"] = not tempdirs.pid_alive(pids["root"])
            record["alive_before_interrupt"] = {k: tempdirs.pid_alive(p) for k, p in pids.items()}
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
                    until(lambda: not tempdirs.pid_alive(pids["root"]))
                    record["root_dead_before_cancel"] = True
                    record["alive_before_cancel"] = {k: tempdirs.pid_alive(p) for k, p in pids.items()}
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
        until(lambda: not any(tempdirs.pid_alive(p) for p in pids.values()), budget=2)
        record["pids"] = pids
        record["alive_after"] = {k: tempdirs.pid_alive(p) for k, p in pids.items()}
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


def marker_lifecycle(root):
    """155 real sequential child lifetimes; controller then exits for real."""
    work = Path(tempdirs.owned_mkdtemp(prefix="openmd-lifetimes-", dir=root))
    pids = []
    max_bytes = 0
    max_records = 0
    for _ in range(155):
        child = runtime.owned_popen([sys.executable, "-c", "pass"], stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        pids.append(child.pid)
        marker = work / tempdirs.OWNER_FILE
        max_bytes = max(max_bytes, marker.stat().st_size)
        max_records = max(max_records, len(json.loads(marker.read_text()).get("pids", [])))
        runtime.wait_interruptibly(child, 5, poll_s=.01)
    # A dead incoming PID must still prune previously completed workers.
    tempdirs.note_process_owner(child)
    final = json.loads(marker.read_text())
    assert final["pids"] == [os.getpid()]
    assert max_bytes <= tempdirs.MAX_OWNER_BYTES
    assert max_records <= tempdirs.MAX_OWNER_PIDS
    return {"case": "marker-lifetimes", "directory": str(work), "pids": pids,
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
        with tempfile.TemporaryDirectory(prefix="openmd-ownership-test-") as box:
            result = run_case(case, Path(box))
    print(json.dumps(result, ensure_ascii=False), flush=True)
