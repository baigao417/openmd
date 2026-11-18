from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from openmd import browser_host as host, mcp_jobs
from openmd.mcp_server import create_server


@pytest.fixture(autouse=True)
def no_spawn(monkeypatch):
    monkeypatch.setattr(mcp_jobs, "_spawn", lambda: None)


@pytest.mark.parametrize("destination", ["", "notes", ".", "../notes"])
def test_destination_must_be_explicit_absolute(destination):
    with pytest.raises(ValueError, match="output_directory_must_be_absolute"):
        mcp_jobs.save("https://example.org", destination)


@pytest.mark.parametrize("source", ["https://user:secret@example.org/a", "https://example.org?token=secret", "https://example.org/#access_token=secret"])
def test_credential_urls_rejected_before_enqueue(tmp_path, source):
    with pytest.raises(ValueError, match="credential_url_not_allowed"):
        mcp_jobs.save(source, str(tmp_path))
    with pytest.raises(ValueError, match="credential_url_not_allowed"):
        mcp_jobs.save_page(source, str(tmp_path), html="<p>Synthetic</p>")


def test_html_limits_apply_to_file_and_multibyte_string(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_jobs, "MAX_HTML_BYTES", 12)
    path = tmp_path / "snapshot.html"
    path.write_text("中" * 5, encoding="utf-8")
    for kwargs in ({"html": "中" * 5}, {"html_path": str(path)}):
        with pytest.raises(ValueError, match="snapshot_too_large"):
            mcp_jobs.save_page("https://example.org", str(tmp_path), **kwargs)
    for kwargs in ({}, {"html": "a", "html_path": str(path)}):
        with pytest.raises(ValueError, match="provide_exactly_one"):
            mcp_jobs.save_page("https://example.org", str(tmp_path), **kwargs)


def test_content_is_bounded_and_confined_to_job_destination(tmp_path, monkeypatch):
    output = tmp_path / "out"
    output.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("Private outside text", encoding="utf-8")
    queued = mcp_jobs.save("https://example.org", str(output))
    with mcp_jobs.queue():
        status = host.read_status(queued["id"])
        status.update(state="done", saved_to=str(outside))
        directory = host.jobs_root() / queued["id"]
        host._write_status(directory, status)
    assert "content" not in mcp_jobs.job(queued["id"], include_content=True)
    inside = output / "inside.md"
    inside.write_text("0123456789abcdef", encoding="utf-8")
    with mcp_jobs.queue():
        status["saved_to"] = str(inside)
        host._write_status(directory, status)
    monkeypatch.setattr(mcp_jobs, "MAX_CONTENT_CHARS", 10)
    result = mcp_jobs.job(queued["id"], include_content=True)
    assert result["content"] == "0123456789" and result["content_truncated"]
    assert result["saved_to"] == str(inside)


def test_receipts_and_job_errors_never_echo_source_body_or_secrets(tmp_path):
    marker = "PRIVATE_BODY_SENTINEL"
    queued = mcp_jobs.save("https://example.org", str(tmp_path))
    raw = {"ok": False, "status": "failed", "error": marker, "title": marker,
           "completeness": {"status": "unknown", "body": marker},
           "sync": {"status": "unchanged", "body": marker},
           "login": {"required": True, "cookie": "SYNTHETIC_SECRET"},
           "next_action": marker}
    with mcp_jobs.queue():
        host.run_queue(runner=lambda command, **kw: subprocess.CompletedProcess(command, 1, json.dumps(raw), marker))
        stored = host.read_status(queued["id"])
    result = mcp_jobs.job(queued["id"])
    assert marker not in json.dumps(result) and "SYNTHETIC_SECRET" not in json.dumps(result)
    assert marker not in json.dumps(stored)
    assert result["status"] == "failed" and result["error_code"] == "extraction_failed"
    assert result["next_action"]


def test_execute_cannot_remember_preferences(tmp_path, monkeypatch):
    source = tmp_path / "sample.txt"
    source.write_text("Original synthetic words", encoding="utf-8")
    queued = mcp_jobs.save(str(source), str(tmp_path / "out"))
    def activation_forbidden():
        raise AssertionError("local saves must not change managed browser tools")
    monkeypatch.setattr("openmd.runtime.managed_tools.activate", activation_forbidden)
    monkeypatch.setattr("openmd.runtime.services.finish_openmd_bridge", lambda: None)
    import openmd.interactive as interactive
    seen = []
    monkeypatch.setattr(interactive, "cmd_go", lambda args: seen.append(args) or 0)
    monkeypatch.setattr("sys.argv", ["mcp_jobs.py", "--execute", queued["id"]])
    assert mcp_jobs.main() == 0
    assert seen[0]._explicit_permissions
    from openmd.core.preferences import preferences_path
    assert not preferences_path().exists()


def test_sdk_validation_and_os_errors_do_not_echo_input(tmp_path, monkeypatch):
    pytest.importorskip("mcp")
    from mcp.shared.memory import create_connected_server_and_client_session

    marker = "PRIVATE_BODY_SENTINEL"
    monkeypatch.setattr(mcp_jobs, "doctor", lambda: (_ for _ in ()).throw(OSError(marker)))

    async def scenario():
        async with create_connected_server_and_client_session(create_server()) as client:
            invalid = await client.call_tool("openmd_save_page", {"source_url": "https://example.org", "output_directory": str(tmp_path), "html": {"body": marker}})
            failed = await client.call_tool("openmd_doctor", {})
            for result in (invalid, failed):
                assert result.isError
                assert marker not in result.model_dump_json()
    asyncio.run(scenario())


def test_doctor_is_bounded_and_does_not_cleanup_or_probe(monkeypatch):
    monkeypatch.setattr("openmd.core.doctor.build_doctor_report", lambda profile: {"ready": False, "manifest_valid": True, "raw": "PRIVATE_BODY_SENTINEL"})
    result = mcp_jobs.doctor()
    assert len(json.dumps(result)) < 1500
    assert not result["ready"] and result["next_action"]
    assert "PRIVATE_BODY_SENTINEL" not in json.dumps(result)


def test_paused_jobs_expose_login_and_unknown_ids_have_next_action(tmp_path):
    queued = mcp_jobs.save("https://example.org", str(tmp_path))
    with mcp_jobs.queue():
        stored = host.read_status(queued["id"])
        stored.update(state="paused", error_code="login_required", login={"site": "Example", "url": "https://example.org"})
        host._write_status(host.jobs_root() / queued["id"], stored)
    paused = mcp_jobs.job(queued["id"])
    assert paused["login"] == {"required": True}
    assert paused["error_code"] == "login_required" and paused["next_action"]
    unknown = mcp_jobs.job("0" * 32)
    assert unknown["state"] == "unknown"
    assert unknown["error_code"] == "job_not_found" and unknown["next_action"]


def test_explicit_content_redacts_credentials_without_logging_body(tmp_path):
    queued = mcp_jobs.save("https://example.org", str(tmp_path))
    artifact = tmp_path / "note.md"
    artifact.write_text("Synthetic text\nCookie: SYNTHETIC_SECRET\n", encoding="utf-8")
    with mcp_jobs.queue():
        stored = host.read_status(queued["id"])
        stored.update(state="done", saved_to=str(artifact))
        host._write_status(host.jobs_root() / queued["id"], stored)
    reply = mcp_jobs.job(queued["id"], include_content=True)
    assert reply["content_redacted"]
    assert "Synthetic text" in reply["content"]
    assert "SYNTHETIC_SECRET" not in reply["content"]
