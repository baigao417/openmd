"""Synthetic conversation fixtures only; no user conversation content."""
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from openmd.adapters.registry import create_default_registry
from openmd.backends.ai_chat import OpenCliAiChatProvider, build_opencli_detail_args, conversation_id_for_url
from openmd.backends.base import BackendExtractRequest
from openmd.backends.registry import BackendRuntimeOptions, create_runtime_backend_provider_registry
from openmd.core.detector import detect_platform
from openmd.core.login import login_for_url
from openmd.core.models import ContentItem
from openmd.core.renderer import render_document, render_markdown_document


CID = "12345678-1234-1234-1234-123456789abc"
URL = f"https://grok.com/c/{CID}"
ANCHOR = "abcdef01-1234-1234-1234-123456789abc"
BACKEND = "opencli.grok.detail"
ROWS = [
    {"Role": "User", "Text": "Give a synthetic example."},
    {"Role": "Assistant", "Text": "```python\nprint('fixture')\n```\n\n[Reference](https://example.org)"},
    {"Role": "User", "Text": "Explain sign in and authentication."},
    {"Role": "Assistant", "Text": "  Keep indentation.\n\n$$x=1$$\n"},
]


@pytest.mark.parametrize("suffix", ["", f"?rid={ANCHOR}&other=value#reply"])
def test_grok_resolves_canonical_conversation_identity_and_anchor(suffix):
    assert detect_platform(URL + suffix) == "ai_chat"
    assert conversation_id_for_url("grok", URL + suffix) == CID
    adapter = create_default_registry().adapter_for(URL + suffix)
    item = adapter.resolve_items(URL + suffix)[0]
    assert item.id == CID
    assert item.url == URL
    assert item.raw_meta.get("anchor_response_id") == (ANCHOR if suffix else None)
    assert build_opencli_detail_args("grok", CID)[-2:] == ["--markdown", "true"]


def test_grok_provider_is_registered():
    registry = create_runtime_backend_provider_registry("ai_chat", BackendRuntimeOptions(enable_browser_backends=True))
    assert isinstance(registry.provider_for(BACKEND), OpenCliAiChatProvider)


def source(suffix=""):
    return ContentItem(platform="ai_chat", input_type="single", url=URL + suffix,
                       source_type="conversation", raw_meta={"chat_site": "grok"})


def extract(payload, exit_code=0, suffix="", stderr=""):
    calls = []
    provider = OpenCliAiChatProvider("grok", BACKEND, runner=lambda args, _: (calls.append(args) or (exit_code, json.dumps(payload), stderr)))
    return provider.extract(BackendExtractRequest(item=source(suffix), backend_chain=(BACKEND,))), calls


def test_grok_multiturn_verbatim_markdown_and_anchor_never_truncates():
    result, calls = extract({"ok": True, "data": ROWS}, suffix=f"?rid={ANCHOR}")
    assert result.status == "verified"
    assert calls[0][3] == CID
    assert [turn.role for turn in result.blocks] == [row["Role"] for row in ROWS]
    assert [turn.text for turn in result.blocks] == [row["Text"] for row in ROWS]
    assert result.metadata["source_url"] == URL
    assert result.metadata["anchor_response_id"] == ANCHOR
    assert result.metadata["turn_count"] == 4
    rendered = render_document(render_markdown_document(source(), result))
    for row in ROWS:
        assert row["Text"].rstrip() in rendered


def test_grok_package_persists_anchor_only_as_optional_metadata(tmp_path):
    from openmd.core.package_writer import write_item_package
    from openmd.core.package_validator import validate_package
    source_item = create_default_registry().adapter_for(URL).resolve_items(f"{URL}?rid={ANCHOR}")[0]
    provider = OpenCliAiChatProvider("grok", BACKEND, runner=lambda *_: (0, json.dumps(ROWS), ""))
    result = provider.extract(BackendExtractRequest(item=source_item, backend_chain=(BACKEND,)))
    written = write_item_package(source_item, result, tmp_path)
    metadata = json.loads((written.package_path / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["source_url"] == URL
    assert metadata["anchor_response_id"] == ANCHOR
    assert validate_package(written.package_path).ok


@pytest.mark.parametrize("exit_code", [0, 77, 1])
def test_grok_auth_required_is_normalized(exit_code):
    result, _ = extract({"ok": False, "error": {"code": "AUTH_REQUIRED", "message": "Synthetic auth required"}}, exit_code)
    assert result.error_code == "login_required"
    assert not result.text and not result.blocks
    assert result.metadata["login"] == {"site": "Grok", "url": "https://grok.com"}


@pytest.mark.parametrize("payload,exit_code", [([], 0), ({"ok": False, "error": {"code": "EMPTY_RESULT", "message": "Verify the signed-in account."}}, 1), ({"ok": False, "error": {"code": "EMPTY_RESULT", "message": "Sign in may be needed."}}, 0)])
def test_grok_empty_is_distinct_from_structured_auth(payload, exit_code):
    result, _ = extract(payload, exit_code)
    assert result.error_code == "ai_chat_empty"


@pytest.mark.parametrize("url", [
    "https://shop.h5.xiaoeknow.com/course?id=123",
    "https://shop.xet.citv.cn/course#lesson",
    "https://courses.example.org:8443/course?signature=synthetic",
    "https://shop.xiaoe-tech.com/course",
])
def test_xiaoe_login_uses_shop_origin(url):
    from urllib.parse import urlsplit
    parsed = urlsplit(url)
    assert login_for_url(url, "xiaoe") == {"site": "小鹅通", "url": f"{parsed.scheme}://{parsed.netloc}"}


def test_shop_login_fallback_and_other_origin_sources():
    assert login_for_url("invalid", "xiaoe") == {"site": "小鹅通", "url": "https://xiaoe-tech.com"}
    assert login_for_url("https://forum.example.org/t/1?private=synthetic", "discourse")["url"] == "https://forum.example.org"
    assert login_for_url("https://chatgpt.com/c/synthetic", "ai_chat") == {"site": "ChatGPT", "url": "https://chatgpt.com"}


def test_grok_runtime_uses_scoped_loading_patch(monkeypatch):
    from openmd.backends import ai_chat
    seen = []
    def run(command, **kwargs):
        seen.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, "[]", "")
    monkeypatch.setattr(ai_chat, "run_interruptibly", run)
    ai_chat._run_command(build_opencli_detail_args("grok", CID), 180)
    assert "--import=data:" in seen[0][1]["env"]["NODE_OPTIONS"]
    assert "NODE_OPTIONS" not in os.environ or seen[0][1]["env"]["NODE_OPTIONS"] != os.environ["NODE_OPTIONS"]
    ai_chat._run_command(build_opencli_detail_args("chatgpt", "synthetic"), 180)
    assert "env" not in seen[1][1]


@pytest.mark.parametrize("mode", ["hydrated", "never_hydrates", "source_drift"])
def test_grok_loading_patch_waits_for_assistants_and_stable_turns(tmp_path, mode):
    from openmd.backends import ai_chat
    node = shutil.which("node")
    if not node:
        pytest.skip("Node not installed")
    module = tmp_path / "clis/grok/detail.js"
    module.parent.mkdir(parents=True)
    (tmp_path / "package.json").write_text('{"type":"module","version":"2.0.3"}', encoding="utf-8")
    module.write_text('''
export async function detail(page) {
let bubbles = [];
const POLL_DEADLINE_MS = 20_000;
const startedAt = Date.now();
while (Date.now() - startedAt < POLL_DEADLINE_MS) {
    bubbles = await page.getMessages();
    if (bubbles.length > 0) break;
    await page.wait(1);
}
if (!bubbles.length) { throw new Error('empty'); }
return bubbles;
}
''', encoding="utf-8")
    probe = tmp_path / "probe.mjs"
    probe.write_text('''
import {detail} from './clis/grok/detail.js';
let tick=0;
Date.now=()=>tick*1000;
const page={ getMessages: async()=>tick<3 ? [{role:'User',text:'fixture',html:''}] : [{role:'User',text:'fixture',html:''},{role:'Assistant',text:'answer',html:'<p>answer</p>'}], wait:async()=>{tick++;} };
const turns=await detail(page);
if(turns.length!==2 || tick<5) throw new Error('returned before assistant hydration/stability');
console.log(JSON.stringify({turns:turns.length,tick}));
''', encoding="utf-8")
    if mode == "never_hydrates":
        probe.write_text(probe.read_text(encoding="utf-8").replace("tick<3", "true"), encoding="utf-8")
    elif mode == "source_drift":
        module.write_text(module.read_text(encoding="utf-8").replace("20_000", "21_000"), encoding="utf-8")
    env = ai_chat._grok_loading_environment()
    done = subprocess.run([node, str(probe)], capture_output=True, text=True, timeout=15, env=env)
    if mode == "hydrated":
        assert done.returncode == 0, done.stderr
        assert json.loads(done.stdout)["turns"] == 2
    else:
        assert done.returncode != 0
        assert ("grok_detail_not_stable" if mode == "never_hydrates" else "grok_detail_patch_incompatible") in done.stderr
