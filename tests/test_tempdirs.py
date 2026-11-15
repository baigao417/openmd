import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from openmd.runtime import tempdirs


def directory(root, name, *, old=False, pid=None):
    path = root / name
    path.mkdir()
    (path / "payload").write_bytes(b"test data")
    if pid is not None:
        (path / tempdirs.OWNER_FILE).write_text(json.dumps({"pid": pid}))
    if old:
        stamp = time.time() - 25 * 3600
        os.utime(path, (stamp, stamp))
    return path


def test_sweep_age_owner_prefix_and_sizes(monkeypatch, tmp_path):
    monkeypatch.setattr(tempdirs.tempfile, "gettempdir", lambda: str(tmp_path))
    alive = os.getpid()
    dead = 2147483647
    assert tempdirs.pid_alive(alive)
    assert not tempdirs.pid_alive(dead)
    old = directory(tmp_path, "openmd-old", old=True)
    dead_owner = directory(tmp_path, "openmd-dead", old=True, pid=dead)
    live = directory(tmp_path, "openmd-live", old=True, pid=alive)
    new = directory(tmp_path, "openmd-new")
    foreign = directory(tmp_path, "other-old", old=True)
    report = tempdirs.sweep_tempdirs()
    assert not old.exists() and not dead_owner.exists()
    assert live.exists() and new.exists() and foreign.exists()
    assert report["deleted"] == 2 and report["retained"] == 2 and report["failed"] == 0
    assert report["deleted_bytes"] >= 18 and report["retained_bytes"] >= 18


def test_markers_are_written_for_both_creation_paths(tmp_path):
    with tempdirs.owned_temporary_directory(prefix="openmd-test-", dir=tmp_path) as name:
        from pathlib import Path
        assert json.loads((Path(name) / tempdirs.OWNER_FILE).read_text())["pid"] == os.getpid()
    path = tempdirs.owned_mkdtemp(prefix="openmd-test-", dir=tmp_path)
    assert json.loads((Path(path) / tempdirs.OWNER_FILE).read_text())["pid"] == os.getpid()


def test_live_worker_keeps_directory_after_controller_dies(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from pathlib import Path

    monkeypatch.setattr(tempdirs.tempfile, "gettempdir", lambda: str(tmp_path))
    path = Path(tempdirs.owned_mkdtemp(prefix="openmd-worker-", dir=tmp_path))
    tempdirs.note_process_owner(SimpleNamespace(pid=os.getpid()))
    marker = path / tempdirs.OWNER_FILE
    owner = json.loads(marker.read_text())
    assert os.getpid() in owner["pids"]
    owner["pid"] = 2147483647  # vanished controller; worker is still alive
    marker.write_text(json.dumps(owner))
    stamp = time.time() - 25 * 3600
    os.utime(path, (stamp, stamp))
    assert tempdirs.sweep_tempdirs()["retained"] == 1
    assert path.exists()


def test_invalid_owner_and_remove_failure_are_retained(monkeypatch, tmp_path):
    monkeypatch.setattr(tempdirs.tempfile, "gettempdir", lambda: str(tmp_path))
    invalid = directory(tmp_path, "openmd-invalid", pid=os.getpid())
    (invalid / tempdirs.OWNER_FILE).write_text("invalid")
    old = time.time() - 26 * 3600
    os.utime(invalid, (old, old))
    locked = directory(tmp_path, "openmd-locked", old=True)
    def denied(path):
        raise PermissionError("fixture")
    monkeypatch.setattr(tempdirs.shutil, "rmtree", denied)
    report = tempdirs.sweep_tempdirs()
    assert report["deleted"] == 0 and report["retained"] == 2 and report["failed"] == 1
    assert invalid.exists() and locked.exists()


def test_link_is_skipped_and_target_is_untouched(monkeypatch, tmp_path):
    monkeypatch.setattr(tempdirs.tempfile, "gettempdir", lambda: str(tmp_path))
    linked = directory(tmp_path, "openmd-junction", old=True)
    # Deterministic reparse-point branch independent of Windows symlink privilege.
    monkeypatch.setattr(tempdirs, "_linked", lambda p: p == linked)
    assert tempdirs.sweep_tempdirs()["deleted"] == 0
    assert linked.exists()


def test_startup_sweep_is_once_nonblocking_and_skips_sizes(monkeypatch):
    import threading
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def sweep(**kwargs):
        calls.append(kwargs)
        entered.set()
        release.wait(2)
        return {"deleted": 0}

    monkeypatch.setattr(tempdirs, "_startup_report", None)
    monkeypatch.setattr(tempdirs, "sweep_tempdirs", sweep)
    try:
        started = time.monotonic()
        first = tempdirs.sweep_at_startup()
        assert time.monotonic() - started < .5
        assert entered.wait(1)
        assert tempdirs.sweep_at_startup() is first
        assert calls == [{"include_sizes": False}]
    finally:
        release.set()


def test_doctor_includes_cleanup_counts_and_sizes(monkeypatch, capsys):
    import openmd.cli as cli
    report = {"deleted": 2, "retained": 1, "failed": 0, "deleted_bytes": 123, "retained_bytes": 45}
    monkeypatch.setattr(tempdirs, "sweep_tempdirs", lambda: report)
    assert cli.main(["doctor", "--profile", "core"]) == 0
    assert json.loads(capsys.readouterr().out)["temp_cleanup"] == report


def test_155_real_pid_lifetimes_and_last_controller_exit_are_sweepable(monkeypatch, tmp_path):
    from openmd.runtime.interpreters import run_interruptibly

    script = Path(__file__).with_name("process_ownership_scenarios.py")
    result = run_interruptibly([sys.executable, str(script), "marker-lifetimes", str(tmp_path)],
                              capture_output=True, text=True, encoding="utf-8", timeout=45)
    assert result.returncode == 0, result.stdout + result.stderr
    record = json.loads(result.stdout)
    assert len(record["pids"]) == 155
    assert len(record["child_returncodes"]) == 155
    assert record["child_returncodes"] == [0] * 155
    assert not tempdirs.pid_alive(record["controller"])
    path = Path(record["directory"])
    stamp = time.time() - 25 * 3600
    os.utime(path, (stamp, stamp))
    monkeypatch.setattr(tempdirs.tempfile, "gettempdir", lambda: str(tmp_path))
    report = tempdirs.sweep_tempdirs()
    assert report["deleted"] == 1 and not path.exists()


def test_marker_supports_160_simultaneous_lifetimes_beyond_old_read_limit(monkeypatch, tmp_path):
    monkeypatch.setattr(tempdirs, "_owned_directories", set())
    monkeypatch.setattr(tempdirs.tempfile, "gettempdir", lambda: str(tmp_path))
    path = Path(tempdirs.owned_mkdtemp(prefix="openmd-many-", dir=tmp_path))
    active = {os.getpid(), *range(2000000000, 2000000160)}
    monkeypatch.setattr(tempdirs, "pid_alive", lambda pid: pid in active)
    for pid in range(2000000000, 2000000160):
        tempdirs.note_process_owner(SimpleNamespace(pid=pid))
    marker = path / tempdirs.OWNER_FILE
    assert 1024 < marker.stat().st_size <= tempdirs.MAX_OWNER_BYTES
    assert len(tempdirs._read_owner(marker)["pids"]) == 161
    for pid in range(2000000000, 2000000159):
        active.remove(pid)
        tempdirs.note_process_owner(SimpleNamespace(pid=2000000159))
    assert tempdirs._read_owner(marker)["pids"] == [os.getpid(), 2000000159]
    active.remove(os.getpid())  # Controller exited, last worker still owns input.
    stamp = time.time() - 25 * 3600
    os.utime(path, (stamp, stamp))
    assert tempdirs.sweep_tempdirs()["retained"] == 1
    active.clear()
    assert tempdirs.sweep_tempdirs()["deleted"] == 1
    assert not path.exists()


def test_dead_pid_registration_prunes_even_when_input_is_already_recorded(monkeypatch, tmp_path):
    monkeypatch.setattr(tempdirs, "_owned_directories", set())
    active = {os.getpid(), 12345}
    monkeypatch.setattr(tempdirs, "pid_alive", lambda pid: pid in active)
    path = Path(tempdirs.owned_mkdtemp(prefix="openmd-prune-", dir=tmp_path))
    tempdirs.note_process_owner(SimpleNamespace(pid=12345))
    active.remove(12345)
    tempdirs.note_process_owner(SimpleNamespace(pid=12345))
    assert tempdirs._read_owner(path / tempdirs.OWNER_FILE)["pids"] == [os.getpid()]


def test_simultaneous_owner_overflow_is_bounded_and_unknown_is_preserved(monkeypatch, tmp_path):
    monkeypatch.setattr(tempdirs, "_owned_directories", set())
    path = Path(tempdirs.owned_mkdtemp(prefix="openmd-overflow-", dir=tmp_path))
    active = True
    monkeypatch.setattr(tempdirs, "pid_alive", lambda pid: active)
    for pid in range(2000000000, 2000000000 + tempdirs.MAX_OWNER_PIDS + 2):
        tempdirs.note_process_owner(SimpleNamespace(pid=pid))
    marker = path / tempdirs.OWNER_FILE
    owner = tempdirs._read_owner(marker)
    assert len(owner["pids"]) == tempdirs.MAX_OWNER_PIDS
    assert owner["incomplete"] and marker.stat().st_size <= tempdirs.MAX_OWNER_BYTES
    active = False
    tempdirs.note_process_owner(SimpleNamespace(pid=12345))
    assert tempdirs._read_owner(marker)["pids"] == []
    assert tempdirs._read_owner(marker)["incomplete"]
    stamp = time.time() - 25 * 3600
    os.utime(path, (stamp, stamp))
    monkeypatch.setattr(tempdirs.tempfile, "gettempdir", lambda: str(tmp_path))
    assert tempdirs.sweep_tempdirs()["retained"] == 1 and path.exists()


@pytest.mark.parametrize("content", ['{"pid":true}', '[1]', '{"pid":1,"pids":"unknown"}', '{"pid":1,"incomplete":"maybe"}', 'x' * (tempdirs.MAX_OWNER_BYTES + 1)])
def test_unknown_markers_are_never_overwritten_or_swept(monkeypatch, tmp_path, content):
    monkeypatch.setattr(tempdirs, "_owned_directories", set())
    monkeypatch.setattr(tempdirs.tempfile, "gettempdir", lambda: str(tmp_path))
    path = Path(tempdirs.owned_mkdtemp(prefix="openmd-unknown-", dir=tmp_path))
    marker = path / tempdirs.OWNER_FILE
    marker.write_text(content)
    tempdirs.note_process_owner(SimpleNamespace(pid=os.getpid()))
    assert marker.read_text() == content
    stamp = time.time() - 25 * 3600
    os.utime(path, (stamp, stamp))
    assert tempdirs.sweep_tempdirs()["retained"] == 1


def test_owner_replace_failure_leaves_complete_previous_marker(monkeypatch, tmp_path):
    monkeypatch.setattr(tempdirs, "_owned_directories", set())
    path = Path(tempdirs.owned_mkdtemp(prefix="openmd-atomic-", dir=tmp_path))
    marker = path / tempdirs.OWNER_FILE
    original = marker.read_bytes()

    def denied(*args):
        raise PermissionError("replace denied")

    monkeypatch.setattr(tempdirs.os, "replace", denied)
    tempdirs.note_process_owner(SimpleNamespace(pid=os.getpid()))
    assert marker.read_bytes() == original
    assert list(path.iterdir()) == [marker]


def test_unreadable_ownership_stays_unchanged_and_retained(monkeypatch, tmp_path):
    monkeypatch.setattr(tempdirs, "_owned_directories", set())
    monkeypatch.setattr(tempdirs.tempfile, "gettempdir", lambda: str(tmp_path))
    path = Path(tempdirs.owned_mkdtemp(prefix="openmd-denied-", dir=tmp_path))
    marker = path / tempdirs.OWNER_FILE
    original = marker.read_bytes()

    def denied(*args):
        raise PermissionError("read denied")

    monkeypatch.setattr(tempdirs, "_read_owner", denied)
    tempdirs.note_process_owner(SimpleNamespace(pid=os.getpid()))
    assert marker.read_bytes() == original
    stamp = time.time() - 25 * 3600
    os.utime(path, (stamp, stamp))
    assert tempdirs.sweep_tempdirs()["retained"] == 1
