"""Background browser timing and structured AI protocol regressions."""
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest


def test_background_grok_waits_use_node_clock_not_hidden_page_timers(tmp_path):
    from openmd.backends.ai_chat import _grok_loading_environment
    node = shutil.which("node")
    if not node:
        pytest.skip("Node not installed")
    module = tmp_path / "clis/grok/detail.js"
    module.parent.mkdir(parents=True)
    (tmp_path / "package.json").write_text('{"type":"module"}', encoding="utf-8")
    module.write_text('''
const cli = x => x;
export const command = cli({func: async (page, kwargs) => {
await page.goto('https://grok.com/c/fixture');
await page.wait(2);
let bubbles = [];
const POLL_DEADLINE_MS = 20_000;
const startedAt = Date.now();
while (Date.now() - startedAt < POLL_DEADLINE_MS) {
bubbles = await page.getMessages();
if (bubbles.length > 0) break;
await page.wait(1);
}
if (!bubbles.length) throw new Error('empty');
return bubbles;
}});
''', encoding="utf-8")
    probe = tmp_path / "probe.mjs"
    probe.write_text('''
import {command} from './clis/grok/detail.js';
let tick=0;
globalThis.setTimeout=(fn,ms)=>{tick++;fn();};
Date.now=()=>tick*1000;
const page={
goto:async(url,opts)=>{if(opts?.waitUntil!=='none')throw Error('hidden DOM settle timer');},
wait:async()=>{throw Error('hidden page timer');},
getMessages:async()=>[{role:'User',text:'fixture'},{role:'Assistant',text:'answer'}]
};
const rows=await command.func(page,{});
if(rows.length!==2||tick<3)throw Error('not stable');
console.log(JSON.stringify({rows:rows.length,tick}));
''', encoding="utf-8")
    done = subprocess.run([node, str(probe)], env=_grok_loading_environment(), capture_output=True, text=True, timeout=10)
    assert done.returncode == 0, done.stderr


def test_grok_timeout_keeps_machine_error_code():
    from openmd.backends.ai_chat import OpenCliAiChatProvider
    from openmd.backends.base import BackendExtractRequest
    from openmd.core.models import ContentItem
    item = ContentItem("ai_chat", "single", "https://grok.com/c/fixture", source_type="conversation", raw_meta={"chat_site":"grok"})
    provider = OpenCliAiChatProvider("grok", "opencli.grok.detail", runner=lambda *_: (75,json.dumps({"ok":False,"error":{"code":"TIMEOUT","message":"timeout"}}),""))
    result = provider.extract(BackendExtractRequest(item=item,backend_chain=(provider.name,)))
    assert result.error_code == "ai_chat_timeout"
    assert result.metadata["original_error_code"] == "TIMEOUT"


def test_deepseek_detail_skips_redundant_home_and_waits_for_late_reply(tmp_path):
    from openmd.runtime.ai_chat_loading import detail_loading_environment
    node=shutil.which("node")
    if not node:pytest.skip("Node not installed")
    module=tmp_path/"clis/deepseek/detail.js"
    module.parent.mkdir(parents=True)
    (tmp_path/"package.json").write_text('{"type":"module"}',encoding="utf-8")
    module.write_text('''
const cli=x=>x;
const ensureOnDeepSeek=async()=>{throw Error('redundant home navigation');};
const getVisibleMessages=page=>page.messages();
export const command=cli({site: 'deepseek',
    func: async (page, kwargs) => {
        await ensureOnDeepSeek(page);
        await page.goto('https://chat.deepseek.com/a/chat/s/fixture');
        const messages = await getVisibleMessages(page);
        return messages;
    },
});
''',encoding="utf-8")
    probe=tmp_path/"probe.mjs"
    probe.write_text('''
import {command} from './clis/deepseek/detail.js';
let tick=0;globalThis.setTimeout=(fn)=>{tick++;fn();};Date.now=()=>tick*1000;
const rows=await command.func({goto:async()=>{},evaluate:async(code)=>code.includes('.length')?2:false,messages:async()=>tick<3?[]:[{Role:'user',Text:'fixture'},{Role:'assistant',Text:'reply'}]},{});
if(rows.length!==2||tick<5)throw Error('late reply omitted');
''',encoding="utf-8")
    done=subprocess.run([node,str(probe)],env=detail_loading_environment("deepseek"),capture_output=True,text=True,timeout=10)
    assert done.returncode==0,done.stderr
