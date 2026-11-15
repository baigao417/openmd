"""Persistent OpenCLI daemon exception must not relax ordinary Job ownership."""

import os
from pathlib import Path
import subprocess
from unittest.mock import Mock

import pytest

from openmd.runtime import interpreters, services


@pytest.fixture
def npm_opencli(tmp_path, monkeypatch):
    node = tmp_path / ("node.exe" if os.name == "nt" else "node")
    node.touch()
    shim = tmp_path / "opencli.cmd"
    shim.touch()
    entry = tmp_path / "node_modules/@jackwener/opencli/dist/src/main.js"
    entry.parent.mkdir(parents=True)
    entry.touch()
    daemon = entry.with_name("daemon.js")
    daemon.touch()
    monkeypatch.setattr(services.shutil, "which", lambda name, **kwargs: str(shim) if "opencli" in name else str(node))
    return [str(node), str(entry)], [str(node), str(daemon)], shim


def test_npm_shim_and_native_node_have_same_daemon(npm_opencli):
    launcher, daemon, shim = npm_opencli
    for command in ([str(shim), "bilibili", "subtitle"], [*launcher, "bilibili", "subtitle"], f'"{shim}" "bilibili" "subtitle"'):
        assert services._opencli_launch(command) == (launcher, daemon)


@pytest.mark.parametrize("control", ["daemon", "help", "list", "--help", "--version"])
def test_control_commands_never_start_service(npm_opencli, control):
    launcher, _, shim = npm_opencli
    assert services._opencli_launch([*launcher, control, "stop"]) is None
    assert services._opencli_launch([str(shim), control]) is None
    assert services._opencli_launch([*launcher, "--profile", "work", control]) is None
    assert services._opencli_launch([*launcher, "--profile=work", control]) is None


def test_unix_npm_symlink_entry(npm_opencli, monkeypatch):
    launcher, daemon, _ = npm_opencli
    monkeypatch.setattr(services.shutil, "which", lambda name, **kwargs: launcher[1] if name == "opencli" else launcher[0])
    assert services._opencli_launch(["opencli", "bilibili", "subtitle"]) == (launcher, daemon)


@pytest.mark.parametrize("argv", [["ollama", "serve"], ["python", "server.py"], ["node", "other.js"], "", None])
def test_other_commands_have_no_service_exception(argv):
    assert services._opencli_launch(argv) is None


def test_existing_daemon_unchanged(npm_opencli, monkeypatch):
    launcher, _, _ = npm_opencli
    start = Mock()
    monkeypatch.setattr(services, "_daemon_running", Mock(return_value=True))
    monkeypatch.setattr(services, "_start_opencli_daemon_detached", start)
    services.ensure_opencli_daemon([*launcher, "bilibili", "subtitle"])
    start.assert_not_called()


def test_cold_daemon_ready_before_actual_owned_command(npm_opencli, monkeypatch):
    launcher, daemon, _ = npm_opencli
    running = Mock(side_effect=[False, False, True])
    child = Mock()
    child.poll.return_value = None
    start = Mock(return_value=child)
    monkeypatch.setattr(services, "_daemon_running", running)
    monkeypatch.setattr(services, "_start_opencli_daemon_detached", start)
    services.ensure_opencli_daemon([*launcher, "bilibili", "subtitle"])
    start.assert_called_once_with(daemon, env=None)
    assert running.call_count == 3


def test_status_unknown_skips_prestart_without_blocking(npm_opencli, monkeypatch):
    launcher, _, _ = npm_opencli
    start = Mock()
    monkeypatch.setattr(services, "_daemon_running", Mock(side_effect=OSError("status unknown")))
    monkeypatch.setattr(services, "_start_opencli_daemon_detached", start)
    services.ensure_opencli_daemon([*launcher, "bilibili", "subtitle"])  # must not raise
    start.assert_not_called()


def test_prestart_cancellation_propagates_and_releases_lock(npm_opencli, monkeypatch):
    launcher, _, _ = npm_opencli
    monkeypatch.setattr(services, "_daemon_running", Mock(side_effect=KeyboardInterrupt))
    with pytest.raises(KeyboardInterrupt):
        services.ensure_opencli_daemon([*launcher, "bilibili", "subtitle"])
    assert services._START_LOCK.acquire(blocking=False)
    services._START_LOCK.release()


def test_daemon_start_failure_falls_through_to_command(npm_opencli, monkeypatch):
    launcher, _, _ = npm_opencli
    monkeypatch.setattr(services, "_daemon_running", Mock(return_value=False))
    monkeypatch.setattr(services, "_start_opencli_daemon_detached", Mock(return_value=Mock(poll=Mock(return_value=1))))
    services.ensure_opencli_daemon([*launcher, "bilibili", "subtitle"])  # command reports its own errors
    assert services._START_LOCK.acquire(blocking=False)
    services._START_LOCK.release()


def test_detached_process_is_only_the_fixed_installed_daemon(npm_opencli, monkeypatch):
    _, daemon, _ = npm_opencli
    popen = Mock(return_value=Mock(poll=Mock(return_value=None)))
    monkeypatch.setattr(services.subprocess, "Popen", popen)
    monkeypatch.setattr(services, "_DAEMONS", [])
    services._start_opencli_daemon_detached(daemon)
    argv = popen.call_args.args[0]
    options = popen.call_args.kwargs
    assert argv == daemon and options["cwd"] == str(Path(daemon[1]).parent)
    assert options["stdin"] == options["stdout"] == options["stderr"] == subprocess.DEVNULL
    assert options["close_fds"] is True
    if os.name == "nt":
        assert options["creationflags"] == subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        assert options["start_new_session"] is True
    with pytest.raises(ValueError, match="Only the installed"):
        services._start_opencli_daemon_detached(["python", "anything.py"])
    assert popen.call_count == 1


def test_status_uses_owned_run_and_unwraps_envelope(monkeypatch):
    run = Mock(return_value=subprocess.CompletedProcess([], 0, '{"ok":true,"data":{"running":true}}', ""))
    monkeypatch.setattr(interpreters, "run_interruptibly", run)
    assert services._daemon_running(["node", "main.js"])
    assert run.call_args.args[0] == ["node", "main.js", "daemon", "status"]
    assert run.call_args.kwargs["timeout"] == 10


@pytest.mark.parametrize("output", ["not-json", '{"ok":false,"error":{}}', '{"running":"false"}'])
def test_status_invalid_does_not_mean_stopped(monkeypatch, output):
    monkeypatch.setattr(interpreters, "run_interruptibly", Mock(return_value=subprocess.CompletedProcess([], 0, output, "")))
    with pytest.raises(OSError, match="status unavailable"):
        services._daemon_running(["node", "main.js"])


def test_browser_controller_prestarts_before_outer_job(monkeypatch, tmp_path):
    from openmd import browser_host

    events = []
    prestart = Mock(side_effect=lambda *args, **kwargs: events.append("prestart"))
    process = Mock(pid=123, returncode=0)
    monkeypatch.setattr(browser_host, "ensure_opencli_daemon", prestart)
    monkeypatch.setattr(browser_host, "owned_popen", Mock(side_effect=lambda *args, **kwargs: events.append("owned") or process))
    monkeypatch.setattr(browser_host, "_write_status", Mock())
    monkeypatch.setattr(browser_host, "communicate_interruptibly", Mock(return_value=("out", "err")))
    monkeypatch.setattr(browser_host, "terminate_quietly", Mock())
    command = ["python", "-I", "-m", "openmd", "go", "https://example.org", "--enable-browser-backends"]
    result = browser_host._run_openmd(command, tmp_path, {"id": "fixture"})
    assert events == ["prestart", "owned"]
    assert prestart.call_args.args[0] == ["opencli", "browser-job-read"]
    assert prestart.call_args.kwargs["cwd"] == str(tmp_path)
    assert result.returncode == 0 and result.stdout == "out" and result.stderr == "err"


def test_browser_controller_without_browser_gate_keeps_service_untouched(monkeypatch, tmp_path):
    from openmd import browser_host

    prestart = Mock()
    process = Mock(pid=123, returncode=0)
    monkeypatch.setattr(browser_host, "ensure_opencli_daemon", prestart)
    monkeypatch.setattr(browser_host, "owned_popen", Mock(return_value=process))
    monkeypatch.setattr(browser_host, "_write_status", Mock())
    monkeypatch.setattr(browser_host, "communicate_interruptibly", Mock(return_value=("", "")))
    monkeypatch.setattr(browser_host, "terminate_quietly", Mock())
    browser_host._run_openmd(["python", "-m", "openmd", "go", "https://example.org"], tmp_path, {"id": "fixture"})
    browser_host._run_openmd(["python", "fixture.py", "--enable-browser-backends"], tmp_path, {"id": "fixture"})
    prestart.assert_not_called()
