"""Native-host health checks use temporary registrations, never the real registry."""
import io
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from openmd import __version__, browser_setup


@pytest.fixture
def host(tmp_path, monkeypatch):
    from openmd import browser_diagnostics as diagnostics

    package = tmp_path / "package-extension"
    package.mkdir()
    (package / "manifest.json").write_text('{"version":"1"}', encoding="utf-8")
    (package / "background.js").write_text("// package", encoding="utf-8")
    root = tmp_path / "browser"
    extension = root / "extension"
    extension.mkdir(parents=True)
    for path in package.iterdir():
        (extension / path.name).write_bytes(path.read_bytes())
    python = tmp_path / "python.exe"
    python.touch()
    launcher = root / "host.cmd"
    launcher.write_text(f'@echo off\n"{python}" -I -m openmd.browser_host %*\n', encoding="utf-8")
    manifest = root / "host.json"
    manifest.write_text(json.dumps({"name": "com.openmd.host", "type": "stdio", "path": str(launcher)}), encoding="utf-8")
    monkeypatch.setattr(diagnostics, "registered_manifests", lambda: {"chrome": manifest})
    monkeypatch.setattr(diagnostics, "EXTENSION_DIR", package)
    monkeypatch.setattr(diagnostics, "run_interruptibly", lambda *a, **kw: subprocess.CompletedProcess(a[0], 0, json.dumps({"version": __version__}), ""))
    return diagnostics, root, python, manifest


def test_missing_openmd_in_registered_interpreter_is_detected(host, monkeypatch):
    diagnostics, root, _, _ = host
    monkeypatch.setattr(diagnostics, "run_interruptibly", lambda *a, **kw: subprocess.CompletedProcess(a[0], 1, "", "No module named openmd"))
    report = diagnostics.browser_host_diagnostics()
    assert not report["ready"]
    assert report["hosts"][0]["error_code"] == "host_interpreter_import_failed"
    assert report["next_action"] == diagnostics.REPAIR_ACTION
    assert not (root / "preferences.json").exists()


def test_outdated_extension_copy_is_detected(host):
    diagnostics, root, _, _ = host
    (root / "extension/background.js").write_text("// old", encoding="utf-8")
    report = diagnostics.browser_host_diagnostics()
    assert not report["ready"]
    assert report["hosts"][0]["extension_matches"] is False
    assert report["next_action"] == diagnostics.REPAIR_ACTION


def test_healthy_host_checks_registered_launcher_version_and_hashes_read_only(host, monkeypatch):
    diagnostics, root, python, _ = host
    calls = []
    before = {p.relative_to(root): (p.stat().st_mtime_ns, p.read_bytes()) for p in root.rglob("*") if p.is_file()}

    def probe(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, json.dumps({"version": __version__}), "")

    monkeypatch.setattr(diagnostics, "run_interruptibly", probe)
    report = diagnostics.browser_host_diagnostics()
    assert report["ready"] and report["registered"]
    assert report["hosts"][0]["extension_matches"]
    assert report["hosts"][0]["version_matches"]
    assert calls[0][0][:4] == [str(python), "-I", "-B", "-c"]
    assert calls[0][1]["timeout"] <= 15
    after = {p.relative_to(root): (p.stat().st_mtime_ns, p.read_bytes()) for p in root.rglob("*") if p.is_file()}
    assert after == before


@pytest.mark.parametrize("damage,error", [
    ("version", "host_version_mismatch"),
    ("launcher", "host_launcher_invalid"),
    ("manifest", "host_manifest_invalid"),
    ("missing", "host_manifest_unreadable"),
    ("timeout", "host_interpreter_timeout"),
])
def test_host_damage_returns_actionable_state(host, monkeypatch, damage, error):
    diagnostics, root, _, manifest = host
    if damage == "version":
        monkeypatch.setattr(diagnostics, "run_interruptibly", lambda *a, **kw: subprocess.CompletedProcess(a[0], 0, '{"version":"0.0.0"}', ""))
    elif damage == "launcher":
        (root / "host.cmd").write_text("unexpected command", encoding="utf-8")
    elif damage == "manifest":
        manifest.write_text("[]", encoding="utf-8")
    elif damage == "missing":
        manifest.unlink()
    else:
        def timeout(command, **kwargs):
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        monkeypatch.setattr(diagnostics, "run_interruptibly", timeout)
    report = diagnostics.browser_host_diagnostics()
    assert not report["ready"]
    assert report["hosts"][0]["error_code"] == error
    assert report["next_action"] == diagnostics.REPAIR_ACTION


def test_doctor_includes_browser_host_check(host):
    from openmd.core.doctor import build_doctor_report

    assert build_doctor_report("core")["browser_host"]["ready"]


def test_launcher_ping_sends_real_native_message_via_cmd(tmp_path, monkeypatch):
    from openmd.browser_host import read_message, write_message

    launcher = tmp_path / "host.cmd"
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        assert read_message(io.BytesIO(kwargs["input"])) == {"type": "ping"}
        reply = io.BytesIO()
        write_message(reply, {"ok": True})
        return subprocess.CompletedProcess(command, 0, reply.getvalue(), b"")

    monkeypatch.setattr(browser_setup, "run_interruptibly", run)
    assert browser_setup.launcher_ping(launcher)
    assert calls[0].endswith(f'""{launcher}""')
    assert "/c" in calls[0] and "/d" in calls[0]


@pytest.mark.parametrize("reply,code", [(b"", 1), (b"\x02\x00\x00\x00{}", 0), (b"\xff\xff\xff\xff", 0)])
def test_launcher_ping_rejects_failed_or_invalid_reply(tmp_path, monkeypatch, reply, code):
    monkeypatch.setattr(browser_setup, "run_interruptibly", lambda *a, **kw: subprocess.CompletedProcess(a[0], code, reply, b"private error omitted"))
    assert not browser_setup.launcher_ping(tmp_path / "host.cmd")


@pytest.mark.skipif(os.name != "nt", reason="Actual Windows cmd quoting")
def test_launcher_ping_executes_temporary_launcher_with_spaces(tmp_path):
    root = tmp_path / "host with spaces"
    root.mkdir()
    script = root / "ping.py"
    script.write_text('''
import json, sys
size = int.from_bytes(sys.stdin.buffer.read(4), "little")
assert json.loads(sys.stdin.buffer.read(size)) == {"type": "ping"}
reply = b'{"ok":true}'
sys.stdout.buffer.write(len(reply).to_bytes(4, "little") + reply)
''', encoding="utf-8")
    launcher = root / "host.cmd"
    launcher.write_text(f'@echo off\n"{sys.executable}" -I -B "{script}"\n', encoding="utf-8")
    assert browser_setup.launcher_ping(launcher)


@pytest.mark.skipif(os.name != "nt", reason="Windows native messaging registration")
def test_setup_fails_clearly_if_new_launcher_cannot_ping(tmp_path, monkeypatch, capsys):
    # Even on Windows this test only writes the temporary host and no registry.
    monkeypatch.setattr(browser_setup, "write_host_files", lambda: browser_setup.host_files(tmp_path)[0])
    monkeypatch.setattr(browser_setup, "launcher_ping", lambda path: False)
    monkeypatch.setattr(browser_setup, "_reg", lambda *a: pytest.fail("do not register a broken host"))
    assert browser_setup.install() == 1
    assert "ping" in capsys.readouterr().err
