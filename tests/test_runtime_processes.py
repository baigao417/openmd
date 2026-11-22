"""Regression boundary for all subprocess waits, plus real non-GPU process trees."""

import ast
import gc
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import Mock

import pytest

from openmd.runtime import interpreters as runtime
from openmd.runtime.tempdirs import pid_alive, pid_start_time


def pid_alive_with_identity(pid, start_time):
    return pid_alive(pid, start_time=start_time) if start_time is not None else pid_alive(pid)


def direct_waits(source):
    tree = ast.parse(source)
    aliases = {"subprocess"}
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            aliases.update(a.asname or a.name for a in node.names if a.name == "subprocess")
        elif isinstance(node, ast.ImportFrom) and node.module == "subprocess":
            imported.update(a.asname or a.name for a in node.names if a.name in {"run", "call", "check_call", "check_output"})
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if (isinstance(f, ast.Name) and f.id in imported) or (
            isinstance(f, ast.Attribute) and (
                f.attr in {"wait", "communicate"} or
                (isinstance(f.value, ast.Name) and f.value.id in aliases and f.attr in {"run", "call", "check_call", "check_output"})
            )
        ):
            yield ast.unparse(node)


# Every exception names the exact call; increasing a timeout breaks this guard.
DIRECT_WAIT_ALLOWLIST = {
    # Unbounded I/O is confined to a daemon thread; the main thread waits in slices.
    ("runtime/interpreters.py", "process.communicate(input=input)"),
    ("runtime/interpreters.py", "finished.wait(min(poll_s, remaining))"),
    ("runtime/interpreters.py", "finished.wait(poll_s)"),
    # Background GPU sampling only; maximum 3s, no worker/media/model launched.
    ("ocr/pipeline.py", "subprocess.run(['nvidia-smi', '--query-gpu=memory.used', '--format=csv,noheader,nounits'], capture_output=True, text=True, timeout=3, **process_options())"),
    # A thread event, not a child-process wait.
    ("ocr/pipeline.py", "self.stop.wait(0.35)"),
}


def test_no_direct_blocking_subprocess_waits():
    root = Path(__file__).resolve().parents[1] / "src/openmd"
    found = {(p.relative_to(root).as_posix(), call) for p in root.rglob("*.py") for call in direct_waits(p.read_text(encoding="utf-8-sig"))}
    assert found == DIRECT_WAIT_ALLOWLIST, f"Unreviewed waits: {found - DIRECT_WAIT_ALLOWLIST}; stale exceptions: {DIRECT_WAIT_ALLOWLIST - found}"


def test_guard_catches_aliases_and_unbounded_waits():
    assert len(list(direct_waits("import subprocess as sp\nfrom subprocess import run as execute\nsp.run(['long'])\nexecute(['long'])\np.communicate()\np.wait(timeout=60)"))) == 4


@pytest.mark.parametrize("text", [False, True])
def test_run_preserves_stdio_exit_and_large_pipe_output(text):
    code = "import sys;data=sys.stdin.read();sys.stdout.write(data+'x'*200000);sys.stderr.write('error');sys.exit(7)"
    result = runtime.run_interruptibly([sys.executable, "-c", code], input="hello" if text else b"hello", capture_output=True, text=text, timeout=10)
    assert result.returncode == 7
    assert result.stdout == ("hello" + "x" * 200000 if text else b"hello" + b"x" * 200000)
    assert result.stderr == ("error" if text else b"error")


def test_check_preserves_called_process_error():
    with pytest.raises(subprocess.CalledProcessError) as error:
        runtime.run_interruptibly([sys.executable, "-c", "import sys;print('out');sys.exit(9)"], capture_output=True, check=True, timeout=10)
    assert error.value.returncode == 9
    assert error.value.stdout.strip() == b"out"


def test_timeout_preserves_budget_and_output():
    with pytest.raises(subprocess.TimeoutExpired) as error:
        runtime.run_interruptibly([sys.executable, "-u", "-c", "import time;print('before timeout');time.sleep(60)"], capture_output=True, timeout=.5)
    assert error.value.timeout == .5
    assert "before timeout" in str(error.value.stdout)


def test_timeout_includes_stdin_write_when_child_never_reads():
    with pytest.raises(subprocess.TimeoutExpired) as error:
        runtime.run_interruptibly([sys.executable, "-c", "import time;time.sleep(60)"], input=b"x" * 1000000, capture_output=True, timeout=.4)
    assert error.value.timeout == .4


def test_communicate_interrupt_kills_tree_before_reraising(monkeypatch):
    child = Mock()
    child.poll.side_effect = KeyboardInterrupt
    child.communicate.side_effect = KeyboardInterrupt
    killed = []
    monkeypatch.setattr(runtime, "terminate_quietly", lambda p: killed.append(p))
    with pytest.raises(KeyboardInterrupt):
        runtime.communicate_interruptibly(child, 60)
    assert killed == [child]
    child.wait.assert_not_called()


def test_kill_process_tree_terminates_real_descendants(tmp_path):
    marker = tmp_path / "pids.txt"
    code = '''import json,subprocess,sys,time
from pathlib import Path
p=subprocess.Popen([sys.executable,"-c","import time;time.sleep(60)"])
start_time=None
try:
 with open(f"/proc/{p.pid}/stat",encoding="ascii",errors="replace") as stat:
  start_time=int(stat.read().rsplit(")",1)[1].split()[19])
except (OSError,IndexError,ValueError):
 pass
Path(sys.argv[1]).write_text(json.dumps({"pid":p.pid,"start_time":start_time}))
time.sleep(60)'''
    child = runtime.owned_popen([sys.executable, "-c", code, str(marker)], **runtime.process_options())
    child_start_time = pid_start_time(child.pid)
    try:
        deadline = time.monotonic() + 10
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(.02)
        assert marker.exists()
        grandchild_record = json.loads(marker.read_text())
        grandchild = grandchild_record["pid"]
        grandchild_start_time = grandchild_record["start_time"]
        assert pid_alive_with_identity(child.pid, child_start_time)
        assert pid_alive_with_identity(grandchild, grandchild_start_time)
        runtime.terminate_quietly(child)
        deadline = time.monotonic() + 5
        while pid_alive_with_identity(grandchild, grandchild_start_time) and time.monotonic() < deadline:
            time.sleep(.02)
        assert child.poll() is not None
        assert not pid_alive_with_identity(grandchild, grandchild_start_time)
    finally:
        runtime.terminate_quietly(child)


def test_real_sigint_during_pipe_capture_does_not_enter_stdlib_grace_wait():
    code = """import signal,threading,time,sys
from openmd.runtime.interpreters import run_interruptibly
signal.signal(signal.SIGINT, signal.default_int_handler)
def interrupt():
 time.sleep(.4)
 signal.raise_signal(signal.SIGINT)
threading.Thread(target=interrupt, daemon=True).start()
try:
 run_interruptibly([sys.executable, '-c', 'import time;time.sleep(60)'], capture_output=True, timeout=10)
except KeyboardInterrupt:
 print('cancelled')
 sys.exit(130)
"""
    environment = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
    started = time.monotonic()
    result = runtime.run_interruptibly([sys.executable, "-c", code], env=environment, capture_output=True, text=True, timeout=5)
    assert result.returncode == 130 and result.stdout.strip() == "cancelled"
    assert time.monotonic() - started < 5


def test_cli_keyboard_interrupt_uses_cancellation_message(monkeypatch, capsys):
    import openmd.cli as cli

    def cancelled(args):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "cmd_ingest", cancelled)
    assert cli.main(["ingest", "https://example.org", "--out", "."]) == 130
    assert "已取消。" in capsys.readouterr().err


SCENARIOS = Path(__file__).with_name("process_ownership_scenarios.py")


@pytest.mark.parametrize("case", ["pipe-sigint", "pipe-timeout", "pipe-normal", "file-normal", "file-timeout", "file-sigint", "layout-normal", "tool-normal", "browser-cancel"])
def test_launch_owned_lifetime_without_external_rescue(case):
    result = runtime.run_interruptibly([sys.executable, str(SCENARIOS), case], capture_output=True,
                                       text=True, encoding="utf-8", timeout=12)
    assert result.returncode == 0, result.stdout + result.stderr
    record = json.loads(result.stdout)
    assert "external_rescue_required" not in record
    assert not any(record["alive_after"].values())
    assert record["extra_io_threads"] == []
    assert record["pipe_handles_closed"]
    assert record["elapsed_s"] < 4


def test_review_reproducer_passes_without_its_rescue():
    # Copied from docs/agent-runs/2026-11-09-daily-use-hardening/evidence/ so tests never depend on run logs.
    script = Path(__file__).resolve().parent / "fixtures/runtime/review_repro_a.py"
    result = runtime.run_interruptibly([sys.executable, str(script)], capture_output=True,
                                       text=True, encoding="utf-8", timeout=12)
    assert result.returncode == 0, result.stdout + result.stderr
    record = json.loads(result.stdout)
    assert "external_rescue_required" not in record
    assert record["completed_within_budget"] and not record["leaf_alive_after"]


@pytest.mark.skipif(os.name != "nt", reason="Windows Job kill-on-close")
@pytest.mark.parametrize("case,exit_code", [("crash-controller", 17), ("crash-during-launch", 18)])
def test_controller_crash_closes_job_without_outer_job_or_cleanup(tmp_path, case, exit_code):
    marker = tmp_path / "crash.json"
    # Raw Popen is essential: an outer owned Job could mask a broken inner Job.
    child = subprocess.Popen([sys.executable, str(SCENARIOS), case, str(marker)],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    assert child.wait(timeout=8) == exit_code
    pids = json.loads(marker.read_text())
    deadline = time.monotonic() + 3
    while any(pid_alive(p) for p in pids.values()) and time.monotonic() < deadline:
        time.sleep(.01)
    assert not any(pid_alive(p) for p in pids.values())


@pytest.mark.skipif(os.name != "nt", reason="Windows native launch error")
def test_assignment_failure_never_runs_child_and_closes_job(monkeypatch, tmp_path):
    from openmd.runtime import _windows_job as win
    get_pid = win._api("GetProcessId", win.W.DWORD, win.W.HANDLE)
    pids = []

    def reject(job, process):
        pids.append(get_pid(process))
        raise OSError("injected assign failure")

    marker = tmp_path / "must-not-run"
    monkeypatch.setattr(win, "AssignProcessToJobObject", reject)
    with pytest.raises(OSError, match="injected assign failure"):
        runtime.owned_popen([sys.executable, "-c", "from pathlib import Path;import sys;Path(sys.argv[1]).write_text('ran')", str(marker)])
    deadline = time.monotonic() + 2
    while any(pid_alive(p) for p in pids) and time.monotonic() < deadline:
        time.sleep(.01)
    assert pids and not any(pid_alive(p) for p in pids)
    assert not marker.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows process handle accounting")
def test_repeated_job_and_pipe_use_does_not_accumulate_handles():
    from openmd.runtime import _windows_job as win
    count = win._api("GetProcessHandleCount", win.W.BOOL, win.W.HANDLE, win.C.POINTER(win.W.DWORD))
    current = win._api("GetCurrentProcess", win.W.HANDLE)

    def handles():
        gc.collect()
        result = win.W.DWORD()
        assert count(current(), win.C.byref(result))
        return result.value

    command = [sys.executable, "-c", "import sys;sys.stdout.buffer.write(sys.stdin.buffer.read())"]
    runtime.run_interruptibly(command, input=b"warmup", capture_output=True, timeout=3)
    before = handles()
    for _ in range(20):
        assert runtime.run_interruptibly(command, input=b"data", capture_output=True, timeout=3).stdout == b"data"
    assert handles() <= before + 4


@pytest.mark.skipif(os.name != "nt", reason="Windows batch shim launch")
def test_owned_launch_resolves_generic_cmd_shim_and_preserves_ampersand_url(tmp_path, monkeypatch):
    child_script = tmp_path / "capture_args.py"
    child_script.write_text(
        "import json,sys;print(json.dumps(sys.argv[1:]))\n",
        encoding="utf-8",
    )
    shim = tmp_path / "fixture-tool.cmd"
    shim.write_text(
        f'@echo off\r\n"{sys.executable}" "{child_script}" %*\r\n',
        encoding="utf-8",
    )
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    path = os.pathsep.join((str(tmp_path), os.path.join(system_root, "System32")))
    monkeypatch.setenv("PATH", path)
    url = "https://example.test/watch?v=one&list=two"

    result = runtime.run_interruptibly(
        ["fixture-tool", "wx", url], capture_output=True, text=True, timeout=10
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == ["wx", url]


@pytest.mark.skipif(os.name != "nt", reason="Windows Job synchronization")
def test_job_close_waits_for_kernel_tree_termination(monkeypatch):
    from openmd.runtime import _windows_job as win

    waits = []
    job = object.__new__(win.Job)
    job.lock = threading.Lock()
    job.handle = 123
    monkeypatch.setattr(win, "TerminateJobObject", lambda handle, code: True)
    monkeypatch.setattr(win, "WaitForSingleObject", lambda handle, timeout: waits.append((handle, timeout)) or 0)
    monkeypatch.setattr(win, "CloseHandle", lambda handle: True)
    job.close()
    assert waits == [(123, 2000)]


def test_saved_posix_group_is_cleaned_once_after_leader_exits(monkeypatch):
    calls = []
    monkeypatch.setattr(runtime.signal, "SIGKILL", 9, raising=False)
    monkeypatch.setattr(os, "killpg", lambda pid, sig: calls.append((pid, sig)), raising=False)
    owner = runtime._ProcessGroup(12345)
    owner.close()
    owner.close()
    assert calls == [(12345, runtime.signal.SIGKILL)]


def test_waiting_workers_must_use_launch_time_ownership():
    root = Path(__file__).resolve().parents[1] / "src/openmd"
    allowed = {"runtime/interpreters.py", "runtime/services.py", "browser_host.py"}
    found = []
    for path in root.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8-sig"))):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and ast.unparse(node.func) == "subprocess.Popen":
                rel = path.relative_to(root).as_posix()
                if rel == "browser_host.py":
                    assert ast.literal_eval(node.args[0].elts[0]) == "explorer"
                if rel == "runtime/services.py":
                    # One reviewed persistent service; never a general exemption.
                    assert ast.unparse(node.args[0]) == "[binary, script]"
                    functions = [function for function in ast.walk(ast.parse(path.read_text(encoding="utf-8-sig")))
                                 if isinstance(function, ast.FunctionDef) and function.lineno <= node.lineno <= function.end_lineno]
                    assert [function.name for function in functions] == ["_start_opencli_daemon_detached"]
                if rel == "runtime/interpreters.py":
                    functions = [function.name for function in ast.walk(ast.parse(path.read_text(encoding="utf-8-sig")))
                                 if isinstance(function, ast.FunctionDef) and function.lineno <= node.lineno <= function.end_lineno]
                    # owned_popen and the taskkill helper own their children; spawn_detached is the one
                    # reviewed unowned launch (uninstall cleanup must outlive Openmd).
                    assert functions in (["owned_popen"], ["kill_process_tree"], ["spawn_detached"]), (functions, node.lineno)
                assert rel in allowed, (rel, node.lineno)
                found.append(rel)
    assert sorted(found) == ["browser_host.py"] + ["runtime/interpreters.py"] * 3 + ["runtime/services.py"]
