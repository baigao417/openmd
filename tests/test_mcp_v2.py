from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from openmd import browser_host as host, mcp_jobs
from openmd.mcp_server import create_server


@pytest.fixture(autouse=True)
def no_spawn(monkeypatch):
    monkeypatch.setattr(mcp_jobs, "_spawn", lambda: None)


def test_save_batch_has_bound_and_shared_queue(tmp_path):
    sources = []
    for index in range(2):
        path = tmp_path / f"source-{index}.txt"
        path.write_text(f"Synthetic {index}", encoding="utf-8")
        sources.append(str(path))
    result = mcp_jobs.save_batch(sources, str(tmp_path / "out"))
    assert result["batch_id"]
    assert [item["state"] for item in result["jobs"]] == ["queued", "queued"]
    with mcp_jobs.queue():
        statuses = [host.read_status(item["id"]) for item in result["jobs"]]
    assert {item["batch"] for item in statuses} == {result["batch_id"]}

    with pytest.raises(ValueError, match="too_many_sources"):
        mcp_jobs.save_batch(sources * 11, str(tmp_path / "out"))


def test_job_action_cancel_reuses_browser_host(tmp_path):
    queued = mcp_jobs.save("https://example.org", str(tmp_path))
    result = mcp_jobs.job(queued["id"], action="cancel")
    assert result["state"] == "cancelled"
    assert mcp_jobs.job(queued["id"])["state"] == "cancelled"


def test_resources_are_mcp_job_scoped_and_bounded(tmp_path):
    queued = mcp_jobs.save("https://example.org", str(tmp_path / "out"))
    artifact = tmp_path / "out" / "article.md"
    artifact.parent.mkdir()
    artifact.write_text("Synthetic resource body", encoding="utf-8")
    with mcp_jobs.queue():
        status = host.read_status(queued["id"])
        status.update(state="done", saved_to=str(artifact), status="verified")
        host._write_status(host.jobs_root() / queued["id"], status)
    index = json.loads(mcp_jobs.resource_index())
    assert any(item["uri"] == f"openmd://job/{queued['id']}" for item in index)
    assert mcp_jobs.resource_content(queued["id"]) == "Synthetic resource body"


def test_sdk_exposes_resources_and_structured_output(tmp_path):
    pytest.importorskip("mcp")

    async def scenario():
        async with create_connected_server_and_client_session(create_server()) as client:
            tools = (await client.list_tools()).tools
            assert all(tool.outputSchema for tool in tools)
            resources = await client.list_resources()
            assert any(str(resource.uri) == "openmd://jobs" for resource in resources.resources)
            body = await client.read_resource("openmd://jobs")
            assert body.contents

    from mcp.shared.memory import create_connected_server_and_client_session

    asyncio.run(scenario())


def test_mcp_docs_cover_uvx_cursor_and_windows_paths():
    root = Path(__file__).resolve().parents[1]
    for name in ("README.md", "README.zh-CN.md", "README.ja.md", "README.ko.md"):
        text = (root / name).read_text(encoding="utf-8")
        assert "uvx --from" in text
        assert "Cursor" in text or "Cursor" in text
        assert "Windows" in text
    skill = (root / "src/openmd/SKILL.md").read_text(encoding="utf-8")
    assert "openmd://jobs" in skill and "action=cancel" in skill and "progressToken" in skill
