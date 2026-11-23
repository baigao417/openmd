"""B站 player API answers the same logged-in request with a varying subtitle list
(measured 2026-11-23: 1,1,0,0 and 1,12,1,1 on four consecutive calls), so an
empty OpenCLI answer (exit 66 / EMPTY_RESULT) is retried a bounded number of times."""
from __future__ import annotations

from openmd.backends.base import BackendExtractRequest
from openmd.backends.bilibili import OpenCliBilibiliSubtitleProvider
from openmd.core.models import ContentItem

ROWS = '[{"from": 0.0, "to": 1.5, "content": "第一句"}, {"from": 1.5, "to": 3.0, "content": "第二句"}]'
EMPTY = (66, "", '{"ok":false,"error":{"code":"EMPTY_RESULT","exitCode":66}}')


def _request():
    return BackendExtractRequest(item=ContentItem("bilibili", "single", "https://www.bilibili.com/video/BV1Ys411k7yQ"), backend_chain=("opencli.bilibili.subtitle",))


def test_empty_subtitle_answer_is_retried_until_rows_arrive(monkeypatch):
    monkeypatch.setattr("openmd.backends.bilibili.time.sleep", lambda s: None)
    answers = [EMPTY, EMPTY, (0, ROWS, "")]
    calls = []
    provider = OpenCliBilibiliSubtitleProvider(runner=lambda cmd, t: (calls.append(cmd), answers.pop(0))[1])
    result = provider.extract(_request())
    assert result.status == "verified"
    assert "第一句" in result.text
    assert len(calls) == 3


def test_empty_subtitle_retry_is_bounded_and_other_failures_are_not_retried(monkeypatch):
    monkeypatch.setattr("openmd.backends.bilibili.time.sleep", lambda s: None)
    calls = []
    provider = OpenCliBilibiliSubtitleProvider(runner=lambda cmd, t: (calls.append(cmd), EMPTY)[1])
    result = provider.extract(_request())
    assert result.status != "verified" and len(calls) == 3

    calls.clear()
    provider = OpenCliBilibiliSubtitleProvider(runner=lambda cmd, t: (calls.append(cmd), (1, "", "boom"))[1])
    provider.extract(_request())
    assert len(calls) == 1


def test_bilibili_command_runner_injects_registered_loader(monkeypatch):
    from openmd.backends import bilibili

    captured = {}
    monkeypatch.setattr(bilibili.shutil, "which", lambda _: "opencli")
    monkeypatch.setattr(bilibili, "run_interruptibly", lambda command, **kwargs: captured.update(kwargs) or type("Done", (), {"returncode": 0, "stdout": "", "stderr": ""})())
    assert bilibili._run_command(["opencli", "bilibili", "subtitle", "BV123"], 20) == (0, "", "")
    assert "--import=data:text/javascript," in captured["env"]["NODE_OPTIONS"]
