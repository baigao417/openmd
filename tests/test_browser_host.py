from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path

import pytest

from openmd import browser_host
from openmd.browser_setup import EXTENSION_DIR, extension_id, write_host_files
from openmd.cli import main


@pytest.fixture(autouse=True)
def isolated_data(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENMD_DATA_DIR", str(tmp_path / "data"))
    # Unit tests must never start a real background queue runner: it would
    # process the same jobs concurrently with the test (flaky on CI).
    real_ensure_runner = browser_host.ensure_runner
    monkeypatch.setattr(browser_host, "ensure_runner", lambda spawn=None: real_ensure_runner(spawn=spawn or (lambda *a, **k: None)))


def _frame(value) -> bytes:
    data = json.dumps(value).encode("utf-8")
    return len(data).to_bytes(4, "little") + data


def test_protocol_round_trip_and_unknown_type():
    stdin = io.BytesIO(_frame({"type": "ping"}) + _frame({"type": "nope"}))
    stdout = io.BytesIO()
    assert browser_host.serve(stdin, stdout) == 0
    stdout.seek(0)
    first = browser_host.read_message(stdout)
    second = browser_host.read_message(stdout)
    assert first["ok"] and "save_to" in first
    assert second == {"ok": False, "error": "unknown_message_type"}


def test_oversized_message_is_rejected():
    with pytest.raises(ValueError):
        browser_host.read_message(io.BytesIO((browser_host.MAX_MESSAGE + 1).to_bytes(4, "little")))


@pytest.mark.parametrize("url", ["file:///C:/secret.txt", "javascript:alert(1)", "chrome://settings", "", 42])
def test_only_http_urls_start_jobs(url):
    reply = browser_host.handle({"type": "save", "url": url})
    assert not reply["ok"]
    assert not (browser_host.jobs_root()).exists() or not list(browser_host.jobs_root().iterdir())


def test_relative_destination_is_rejected():
    reply = browser_host.handle({"type": "save", "url": "https://example.org", "save_to": "notes"})
    assert not reply["ok"] and "save_to_must_be_absolute" in reply["error"]


def test_start_job_spawns_detached_runner(tmp_path):
    calls = []
    status = browser_host.start_job("https://example.org/a", str(tmp_path / "dest"), spawn=lambda cmd, **kw: calls.append((cmd, kw)))
    assert status["state"] == "queued"
    command, kwargs = calls[0]
    assert command[-2:] == ["openmd.browser_host", "--runner"]
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert browser_host.read_status(status["id"])["url"] == "https://example.org/a"


def test_run_job_records_success_and_failure(tmp_path):
    status = browser_host.start_job("https://example.org/a", str(tmp_path / "dest"), spawn=lambda *a, **k: None)
    job_dir = browser_host.jobs_root() / status["id"]
    saved = tmp_path / "dest" / "A title.md"

    def ok(command, **kwargs):
        assert command[command.index("-m") + 1:command.index("-m") + 3] == ["openmd", "go"] and "utf8" in command and "--save-to" in command and "--enable-cloud-backends" not in command
        return subprocess.CompletedProcess(command, 0, json.dumps({"ok": True, "saved_to": str(saved)}) + "\n", "")

    done = browser_host.run_job(job_dir, runner=ok)
    assert done["state"] == "done" and done["title"] == "A title" and "pid" not in done

    status = browser_host.start_job("https://example.org/b", str(tmp_path / "dest"), spawn=lambda *a, **k: None)
    job_dir = browser_host.jobs_root() / status["id"]
    failed = browser_host.run_job(job_dir, runner=lambda command, **kw: subprocess.CompletedProcess(command, 1, "", "处理失败：连接超时\n"))
    assert failed["state"] == "failed" and "连接超时" in failed["error"]
    assert [job["state"] for job in browser_host.recent_jobs()][:2] in (["failed", "done"], ["done", "failed"])


def test_status_rejects_path_like_ids():
    assert not browser_host.handle({"type": "status", "id": "../../etc"})["ok"]
    assert not browser_host.handle({"type": "reveal", "id": "0" * 32})["ok"]


def test_host_manifest_allows_only_this_extension(tmp_path):
    manifest_path = write_host_files(tmp_path, python=r"C:\Python\python.exe")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["name"] == browser_host.HOST_NAME
    assert manifest["allowed_origins"] == [f"chrome-extension://{extension_id()}/"]
    launcher = Path(manifest["path"]).read_text(encoding="utf-8")
    assert "-m openmd.browser_host" in launcher and "-I" in launcher


def test_extension_bundle_is_complete():
    manifest = json.loads((EXTENSION_DIR / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["manifest_version"] == 3 and "nativeMessaging" in manifest["permissions"]
    for path in [manifest["background"]["service_worker"], manifest["action"]["default_popup"], manifest["options_page"], "options.js", "popup.js", *manifest["icons"].values()]:
        assert (EXTENSION_DIR / path).is_file(), path


def test_daily_help_is_short_and_hidden_flags_still_parse(capsys):
    assert main(["--help"]) == 0
    text = capsys.readouterr().out
    assert "{go,batch}" in text and "collect" not in text.split("高级命令")[0]
    assert main(["go", "--help"]) == 0
    go_help = capsys.readouterr().out
    assert "--enable-asr" not in go_help and "--raw" in go_help
    from openmd.cli import build_parser

    args = build_parser().parse_args(["go", "https://example.org", "--enable-asr", "--backend", "mock", "--yes"])
    assert args.enable_asr and args.backend == "mock"


def test_pick_folder_message_dispatches(monkeypatch, tmp_path):
    monkeypatch.setattr(browser_host, "pick_folder", lambda initial: {"ok": True, "path": str(tmp_path), "initial": initial})
    reply = browser_host.handle({"type": "pick_folder", "initial": "D:/notes"})
    assert reply == {"ok": True, "path": str(tmp_path), "initial": "D:/notes"}


def test_background_job_never_opens_a_console_window(tmp_path):
    import os

    calls = []
    browser_host.start_job("https://example.org/a", str(tmp_path / "dest"), spawn=lambda cmd, **kw: calls.append(kw))
    flags = calls[0]["creationflags"]
    if os.name == "nt":
        assert flags & subprocess.CREATE_NO_WINDOW
        # DETACHED_PROCESS makes Windows ignore CREATE_NO_WINDOW; console children then pop up terminals.
        assert not flags & subprocess.DETACHED_PROCESS


def _ok_runner(order):
    def run(command, **kwargs):
        order.append(command[command.index("go") + 1])
        return subprocess.CompletedProcess(command, 0, json.dumps({"ok": True, "saved_to": f"C:/x/{len(order)}.md"}), "")
    return run


def test_batch_dedupes_rejects_and_runs_oldest_first(tmp_path):
    spawned = []
    reply = browser_host.start_batch(
        ["https://a.example/1", "https://a.example/2", "https://a.example/1", "file:///C:/x", "https://a.example/3"],
        str(tmp_path), spawn=lambda *a, **k: spawned.append(a))
    assert [j["url"] for j in reply["jobs"]] == ["https://a.example/1", "https://a.example/2", "https://a.example/3"]
    assert len(reply["rejected"]) == 1 and len(spawned) == 1
    order = []
    assert browser_host.run_queue(runner=_ok_runner(order)) == 3
    assert order == ["https://a.example/1", "https://a.example/2", "https://a.example/3"]
    assert not browser_host._lock_path().exists()
    assert all(browser_host.read_status(j["id"])["state"] == "done" for j in reply["jobs"])


def test_live_runner_lock_blocks_second_runner(tmp_path, monkeypatch):
    browser_host.start_batch(["https://a.example/1"], str(tmp_path), spawn=lambda *a, **k: None)
    browser_host._lock_path().write_text("4242", encoding="utf-8")
    monkeypatch.setattr(browser_host, "_pid_alive", lambda pid: pid == 4242)
    assert browser_host.run_queue(runner=_ok_runner([])) == 0
    monkeypatch.setattr(browser_host, "_pid_alive", lambda pid: False)  # stale lock after a reboot
    assert browser_host.run_queue(runner=_ok_runner([])) == 1


def test_interrupted_job_is_requeued_once_then_failed(tmp_path, monkeypatch):
    status = browser_host.start_job("https://a.example/1", str(tmp_path), spawn=lambda *a, **k: None)
    job_dir = browser_host.jobs_root() / status["id"]
    monkeypatch.setattr(browser_host, "_pid_alive", lambda pid: False)
    for attempts, expected in ((1, "queued"), (2, "failed")):
        browser_host._write_status(job_dir, {**browser_host.read_status(status["id"]), "state": "running", "pid": 99, "attempts": attempts})
        assert browser_host.handle({"type": "status", "id": status["id"]})["job"]["state"] == expected


def test_queued_work_resumes_after_reboot(tmp_path, monkeypatch):
    browser_host.start_batch(["https://a.example/1"], str(tmp_path), spawn=lambda *a, **k: None)
    spawned = []
    monkeypatch.setattr(browser_host, "_spawn_runner", lambda spawn=None: spawned.append(1))
    assert browser_host.ensure_runner() is True and spawned == [1]


def test_cancel_queued_job_is_skipped_by_runner(tmp_path):
    reply = browser_host.start_batch(["https://a.example/1", "https://a.example/2"], str(tmp_path), spawn=lambda *a, **k: None)
    browser_host.cancel_job(reply["jobs"][0]["id"])
    order = []
    browser_host.run_queue(runner=_ok_runner(order))
    assert order == ["https://a.example/2"]
    assert browser_host.read_status(reply["jobs"][0]["id"])["state"] == "cancelled"


def test_child_openmd_writes_utf8(tmp_path):
    seen = {}

    def run(command, **kwargs):
        seen.update(kwargs["env"])
        return subprocess.CompletedProcess(command, 0, json.dumps({"ok": True, "saved_to": "C:/x/这是一条价值百万的内容.md"}, ensure_ascii=False), "")

    status = browser_host.start_job("https://a.example/1", str(tmp_path), spawn=lambda *a, **k: None)
    done = browser_host.run_job(browser_host.jobs_root() / status["id"], runner=run)
    assert seen["PYTHONIOENCODING"] == "utf-8" and done["title"] == "这是一条价值百万的内容"


def test_concurrent_status_writers_never_share_a_temp_file(tmp_path, monkeypatch):
    # The queue runner and a status poller (_recover) can write the same job at
    # once; a shared "status.json.tmp" lets their bytes interleave (seen on CI).
    seen = []
    real_write_text = browser_host.Path.write_text

    def record(self, *args, **kwargs):
        seen.append(self.name)
        return real_write_text(self, *args, **kwargs)

    monkeypatch.setattr(browser_host.Path, "write_text", record)
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    browser_host._write_status(job_dir, {"id": "a", "state": "queued"})
    browser_host._write_status(job_dir, {"id": "a", "state": "running"})
    temps = [name for name in seen if name != "status.json"]
    assert len(temps) == 2 and len(set(temps)) == 2
    import json

    assert json.loads((job_dir / "status.json").read_text(encoding="utf-8"))["state"] == "running"
    assert not list(job_dir.glob("*.tmp"))
