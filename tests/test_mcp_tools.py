from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from openmd import browser_host
from openmd import mcp_jobs
from openmd.mcp_server import create_server


@pytest.fixture(autouse=True)
def no_background_jobs(monkeypatch):
    monkeypatch.setattr(mcp_jobs, "_spawn", lambda: None)


def test_save_uses_separate_shared_queue_and_closed_cli_gates(tmp_path):
    source = tmp_path / "source.txt"
    source.write_text("Synthetic source", encoding="utf-8")
    job = mcp_jobs.save(str(source), str(tmp_path / "out"))
    assert job["state"] == "queued"
    assert browser_host.read_status(job["id"])["state"] == "unknown"
    with mcp_jobs.queue():
        stored = browser_host.read_status(job["id"])
        assert stored["permissions"] == {name: False for name in mcp_jobs.GATES}
        command = mcp_jobs.command(stored)
        assert "-I" in command and "--execute" in command
        assert browser_host.run_queue(runner=lambda cmd, **kw: subprocess.CompletedProcess(
            cmd, 0, json.dumps({"ok": True, "saved_to": str(tmp_path / "out" / "source.md"),
                                "status": "partial", "source_level": "native_text",
                                "completeness": {"status": "unknown"},
                                "capture": {"coverage": "visible_only", "collected_count": 8, "declared_total": 61,
                                            "index_gaps": [9]}, "sync": {"status": "created"}}), "")) == 1
    polled = mcp_jobs.job(job["id"])
    assert polled["state"] == "done" and polled["status"] == "partial"
    assert polled["source_level"] == "native_text"
    assert polled["completeness"] == {"status": "unknown"}
    assert polled["capture"] == {"coverage": "visible_only", "collected_count": 8, "declared_total": 61,
                                  "index_gaps": [9]}
    assert polled["sync"] == {"status": "created"}


def test_permissions_are_explicit_and_core_authorization_is_not_inferred(tmp_path):
    from openmd.cli import build_parser
    from openmd.interactive import _authorize

    saved = mcp_jobs.save("https://example.org/article", str(tmp_path), network=True)
    with mcp_jobs.queue():
        status = browser_host.read_status(saved["id"])
    args = mcp_jobs.arguments(status)
    assert args.enable_network_backends
    assert not args.enable_browser_backends and not args.enable_cloud_backends
    default = mcp_jobs.arguments({**status, "permissions": {name: False for name in mcp_jobs.GATES}})
    assert _authorize(default, status["url"], tty=False)
    assert all(not getattr(default, flag.lstrip("-").replace("-", "_")) for flag in mcp_jobs.GATES.values())
    assert not build_parser().parse_args(["go", "https://example.org"]).enable_network_backends


def test_snapshot_reuses_existing_queue_and_polls_content(tmp_path):
    page = tmp_path / "page.html"
    page.write_text("<article>Synthetic page</article>", encoding="utf-8")
    queued = mcp_jobs.save_page("https://example.org/page", str(tmp_path / "out"), html_path=str(page))
    with mcp_jobs.queue():
        stored = browser_host.read_status(queued["id"])
        assert stored["snapshot"] and Path(stored["snapshot_path"]).read_text() == page.read_text()
        assert mcp_jobs.arguments(stored).as_snapshot
        markdown = tmp_path / "out" / "page.md"
        markdown.parent.mkdir()
        markdown.write_text("Synthetic Markdown", encoding="utf-8")
        browser_host.run_queue(runner=lambda cmd, **kw: subprocess.CompletedProcess(
            cmd, 0, json.dumps({"ok": True, "saved_to": str(markdown), "status": "verified"}), ""))
        assert not Path(stored["snapshot_path"]).exists()
    assert "content" not in mcp_jobs.job(queued["id"])
    assert mcp_jobs.job(queued["id"], include_content=True)["content"] == "Synthetic Markdown"


def test_sdk_client_lists_and_calls_all_four_tools(tmp_path, monkeypatch):
    pytest.importorskip("mcp")
    from mcp.shared.memory import create_connected_server_and_client_session

    monkeypatch.setattr(mcp_jobs, "doctor", lambda: {"ready": True, "profile": "core"})

    async def scenario():
        server = create_server()
        async with create_connected_server_and_client_session(server._mcp_server) as client:
            tools = (await client.list_tools()).tools
            assert {tool.name for tool in tools} == {"openmd_save", "openmd_save_page", "openmd_job", "openmd_doctor"}
            for tool in tools:
                assert tool.annotations.readOnlyHint == (tool.name in {"openmd_job", "openmd_doctor"})
            saved = await client.call_tool("openmd_save", {"source": "https://example.org/a", "output_directory": str(tmp_path)})
            assert not saved.isError
            queued = saved.structuredContent
            polled = await client.call_tool("openmd_job", {"job_id": queued["id"]})
            assert not polled.isError and polled.structuredContent["state"] == "queued"
            snapshot = await client.call_tool("openmd_save_page", {"source_url": "https://example.org/page", "output_directory": str(tmp_path), "html": "<article>Synthetic</article>"})
            assert not snapshot.isError and snapshot.structuredContent["state"] == "queued"
            diagnosis = await client.call_tool("openmd_doctor", {})
            assert not diagnosis.isError and diagnosis.structuredContent["ready"]
    asyncio.run(scenario())
