"""A-review.md P1-1 reproducer: rescue removed, passing assertions added.
Only local CPU processes. Failure is a failure; no external kill/rescue path.
"""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))
import contextlib, io, json, os, signal, sys, tempfile, threading, time
from pathlib import Path
from openmd.runtime.interpreters import run_interruptibly
from openmd.runtime.tempdirs import pid_alive
import openmd.cli as cli
if os.name == 'nt':
    import ctypes
    ctypes.windll.kernel32.SetConsoleCtrlHandler(None, False)
signal.signal(signal.SIGINT, signal.default_int_handler)
with tempfile.TemporaryDirectory(prefix='openmd-review-A-cli-') as box:
    tempfile.tempdir = box
    marker = Path(box) / 'pids.json'
    record = {}; done = threading.Event()
    leaf = 'import time;time.sleep(30)'
    parent = ('import subprocess,sys,os,json;from pathlib import Path;'
              'p=subprocess.Popen([sys.executable,"-c",sys.argv[2]]);'
              'Path(sys.argv[1]).write_text(json.dumps({"root":os.getpid(),"leaf":p.pid}))')
    def interrupt_without_rescue():
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline: time.sleep(.01)
        pids = json.loads(marker.read_text()); record.update(pids)
        while pid_alive(pids['root']) and time.monotonic() < deadline: time.sleep(.01)
        record['root_dead_before_interrupt'] = not pid_alive(pids['root'])
        record['signal_at'] = time.monotonic(); signal.raise_signal(signal.SIGINT)
        record['completed_within_budget'] = done.wait(1.5)
    interrupter = threading.Thread(target=interrupt_without_rescue, daemon=True)
    def workload(args):
        interrupter.start()
        try:
            return run_interruptibly([sys.executable, '-c', parent, str(marker), leaf],
                                     capture_output=True, timeout=10).returncode
        finally: done.set()
    cli.cmd_ingest = workload
    err = io.StringIO()
    try:
        with contextlib.redirect_stderr(err):
            code = cli.main(['ingest', 'https://example.org/reviewer-synthetic', '--out', box])
        record.update(exit_code=code, stderr=err.getvalue(),
                      seconds_after_signal=round(time.monotonic()-record['signal_at'], 3))
    finally:
        interrupter.join(timeout=2)
    record['leaf_alive_after'] = pid_alive(record['leaf'])
    print(json.dumps(record, ensure_ascii=False, indent=2))
    assert 'external_rescue_required' not in record, record
    assert record['completed_within_budget'], record
    assert record['root_dead_before_interrupt'], record
    assert record['exit_code'] == 130 and '已取消。' in record['stderr'], record
    assert not record['leaf_alive_after'], record
    assert record['seconds_after_signal'] < 1.5, record
