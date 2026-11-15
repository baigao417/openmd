"""Real child-process termination and lock races; mock backend, no network."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from openmd.core.execution_policy import CompactOutputLock


SOURCE = Path(__file__).resolve().parents[1] / "src"


def _interpreter() -> str:
    # In a Windows venv, python.exe is a redirector that spawns the real
    # interpreter; killing the redirector would orphan the lock owner.
    base = getattr(sys, "_base_executable", None)
    if os.name == "nt" and sys.prefix != sys.base_prefix and base and Path(base).is_file():
        return base
    return sys.executable


def _start(script: str, *args: Path | str) -> subprocess.Popen[str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SOURCE) + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.Popen(
        [_interpreter(), "-c", script, *map(str, args)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
    )


def _wait_for(marker: Path, child: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 15
    while not marker.exists() and child.poll() is None and time.monotonic() < deadline:
        time.sleep(0.02)
    if not marker.exists():
        if child.poll() is not None:
            stdout, stderr = child.communicate(timeout=5)
            raise AssertionError(f"child exited {child.returncode}: {stdout} {stderr}")
        raise AssertionError("child did not reach the bounded checkpoint")


def _stop(child: subprocess.Popen[str]) -> None:
    if child.poll() is None:
        child.kill()
    child.communicate(timeout=5)


HOLD_LOCK = """
import sys, threading
from pathlib import Path
from openmd.core.execution_policy import CompactOutputLock
out = Path(sys.argv[1])
lock = CompactOutputLock.acquire(out, 'child-owner')
(out / 'ready').write_text('locked')
threading.Event().wait(30)
lock.release()
"""


def test_os_killed_owner_is_recovered_without_removing_other_artifacts(tmp_path: Path) -> None:
    retained = tmp_path / "existing-package.md"
    retained.write_bytes(b"existing artifact must survive recovery")
    child = _start(HOLD_LOCK, tmp_path)
    try:
        _wait_for(tmp_path / "ready", child)
        child.kill()  # TerminateProcess on Windows / SIGKILL on POSIX, no finally.
        child.wait(timeout=5)
        assert (tmp_path / ".openmd-compact.lock").exists()
        successor = CompactOutputLock.acquire(tmp_path, "successor")
        successor.release()
        assert not (tmp_path / ".openmd-compact.lock").exists()
        assert retained.read_bytes() == b"existing artifact must survive recovery"
    finally:
        _stop(child)


@pytest.mark.parametrize("phase", ["before-publish", "after-publish"])
def test_kill_during_owner_install_does_not_leave_a_torn_canonical_record(tmp_path: Path, phase: str) -> None:
    script = """
import sys, threading
from pathlib import Path
import openmd.core.execution_policy as policy
out, phase = Path(sys.argv[1]), sys.argv[2]
original = policy.os.link
def publish(source, destination):
    if phase == 'after-publish': original(source, destination)
    (out / 'install-ready').write_text('complete temporary record ready')
    threading.Event().wait(30)
    if phase == 'before-publish': original(source, destination)
policy.os.link = publish
policy.CompactOutputLock.acquire(out, 'initializing-owner')
"""
    child = _start(script, tmp_path, phase)
    try:
        _wait_for(tmp_path / "install-ready", child)
        child.kill()
        child.wait(timeout=5)
        assert (tmp_path / ".openmd-compact.lock").exists() == (phase == "after-publish")
        lock = CompactOutputLock.acquire(tmp_path, "successor")
        lock.release()
        assert not (tmp_path / ".openmd-compact.lock").exists()
    finally:
        _stop(child)


RACE_LOCK = """
import sys, time
from pathlib import Path
from openmd.core.execution_policy import CompactOutputLock
out, name = Path(sys.argv[1]), sys.argv[2]
(out / (name + '.ready')).write_text('ready')
deadline = time.monotonic() + 15
while not (out / 'start').exists():
    if time.monotonic() > deadline: raise RuntimeError('start timeout')
    time.sleep(.01)
try:
    lock = CompactOutputLock.acquire(out, name)
except FileExistsError:
    (out / (name + '.result')).write_text('blocked')
else:
    (out / (name + '.result')).write_text('acquired')
    while not (out / 'release').exists():
        if time.monotonic() > deadline: raise RuntimeError('release timeout')
        time.sleep(.01)
    lock.release()
"""


def test_cross_process_race_has_one_owner_and_live_owner_cannot_be_stolen(tmp_path: Path) -> None:
    # Start from a real abandoned lock so concurrent recovery is also exercised.
    abandoned = _start(HOLD_LOCK, tmp_path)
    try:
        _wait_for(tmp_path / "ready", abandoned)
    finally:
        _stop(abandoned)
    children = [_start(RACE_LOCK, tmp_path, f"racer-{index}") for index in range(3)]
    try:
        for index, child in enumerate(children):
            _wait_for(tmp_path / f"racer-{index}.ready", child)
        (tmp_path / "start").write_text("start")
        for index, child in enumerate(children):
            _wait_for(tmp_path / f"racer-{index}.result", child)
        results = [(tmp_path / f"racer-{index}.result").read_text() for index in range(3)]
        assert results.count("acquired") == 1
        assert results.count("blocked") == 2
        owner_bytes = (tmp_path / ".openmd-compact.lock").read_bytes()
        try:
            unexpected = CompactOutputLock.acquire(tmp_path, "steal-attempt")
        except FileExistsError:
            pass
        else:
            unexpected.release()
            raise AssertionError("a live process lost ownership")
        assert (tmp_path / ".openmd-compact.lock").read_bytes() == owner_bytes
        (tmp_path / "release").write_text("release")
        for child in children:
            stdout, stderr = child.communicate(timeout=5)
            assert child.returncode == 0, (stdout, stderr)
        assert not (tmp_path / ".openmd-compact.lock").exists()
    finally:
        for child in children:
            _stop(child)


def test_killed_cli_preserves_first_package_and_rerun_finishes_remaining_item(tmp_path: Path) -> None:
    inputs = tmp_path / "inputs.txt"
    inputs.write_text("https://example.com/hardkill-one\nhttps://example.com/hardkill-two\n")
    out = tmp_path / "artifacts"
    marker = tmp_path / "cli-ready"
    script = """
import sys, threading
from pathlib import Path
import openmd.cli as cli
original = cli._process_input
calls = 0
def process(value, args, *, command):
    global calls
    calls += 1
    if calls == 2:
        Path(sys.argv[3]).write_text('first item durable; second not started')
        threading.Event().wait(30)
    return original(value, args, command=command)
cli._process_input = process
raise SystemExit(cli.main(['batch', sys.argv[1], '--backend', 'mock', '--package', '--receipt', 'compact', '--out', sys.argv[2]]))
"""
    child = _start(script, inputs, out, marker)
    try:
        _wait_for(marker, child)
        child.kill()
        child.wait(timeout=5)
    finally:
        _stop(child)
    checkpoint = next((out / "runs").glob("*/checkpoint.json"))
    assert json.loads(checkpoint.read_text())["completed"] == 1
    assert len((checkpoint.parent / "items.jsonl").read_text().splitlines()) == 1
    first_packages = list(out.rglob("metadata.json"))
    assert len(first_packages) == 1
    before = {p: p.read_bytes() for p in first_packages[0].parent.rglob("*") if p.is_file()}
    run = _start(
        "import sys; from openmd.cli import main; raise SystemExit(main(sys.argv[1:]))",
        "batch", inputs, "--backend", "mock", "--package", "--receipt", "compact", "--out", out,
    )
    try:
        stdout, stderr = run.communicate(timeout=15)
        assert run.returncode == 0, (stdout, stderr)
        receipt = json.loads(stdout)
        assert receipt["status"] == "complete"
        assert receipt["counts"]["skipped"] == 1
        assert receipt["counts"]["discovered"] == 2
        assert len(list(out.rglob("metadata.json"))) == 2
        assert all(path.read_bytes() == data for path, data in before.items())
        assert not (out / ".openmd-compact.lock").exists()
    finally:
        _stop(run)
