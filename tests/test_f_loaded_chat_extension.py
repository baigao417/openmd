"""Execute extension entry points with a synthetic Chrome API, never live tabs."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(not shutil.which("node"), reason="Node is needed for the extension harness")
def test_ai_extension_entries_capture_current_tab_and_keep_non_ai_urls(tmp_path):
    script = tmp_path / "extension.cjs"
    script.write_text(r'''
const fs = require('fs');
const vm = require('vm');
const callbacks = {};
const event = name => ({addListener: fn => { callbacks[name] = fn; }});
const store = {settings:{locations:[{id:'default',path:'synthetic',name:'Synthetic'}],defaultId:'default'}};
let tabs = [];
const chrome = {
  runtime:{onInstalled:event('installed'),onMessage:event('message'),onStartup:event('startup'),sendNativeMessage:(_host,_msg,cb)=>cb({ok:true,protocol_version:1,extension_hash:'a'.repeat(64)})},
  alarms:{create:()=>{},onAlarm:event('alarm')},
  notifications:{onButtonClicked:event('notification'),onClosed:event('closed')},
  contextMenus:{onClicked:event('context')},
  storage:{local:{get:async () => store,set:async () => {}},onChanged:event('storage')},
  action:{onClicked:event('icon'),setPopup:async () => {},setTitle:async () => {}},
  tabs:{query:async () => tabs},
};
const context = vm.createContext({chrome,URL,setTimeout:() => {},crypto:{randomUUID:() => 'synthetic'}});
vm.runInContext(fs.readFileSync(process.argv[2], 'utf8').replace(/^import .*;$/m,''), context);
vm.runInContext(`var calls=[]; saveMany=async (items) => {calls.push({kind:'url',urls:items.map(x=>x.url)}); return {ok:true,count:items.length};}; saveSnapshot=async (id) => {calls.push({kind:'snapshot',id});return {ok:true,count:1};};`,context);
(async () => {
const urls = JSON.parse(process.argv[3]);
const results=[];
for(const url of urls){
  const tab={id:41,windowId:1,url,title:'Synthetic'}; tabs=[tab];
  context.calls.length=0; await callbacks.icon(tab); results.push({entry:'icon',url,calls:[...context.calls]});
  context.calls.length=0; await callbacks.context({menuItemId:'save-page'},tab); results.push({entry:'context',url,calls:[...context.calls]});
  context.calls.length=0;
  await new Promise(resolve => callbacks.message({cmd:'save',items:[{url,title:'Synthetic',tabId:41}]},{},resolve));
  results.push({entry:'popup',url,calls:[...context.calls]});
  context.calls.length=0;
  await new Promise(resolve => callbacks.message({cmd:'snapshot'},{},resolve));
  results.push({entry:'snapshot',url,calls:[...context.calls]});
}
console.log(JSON.stringify(results));
})().catch(error => { console.error(error);process.exitCode=1;});
''', encoding="utf-8")
    from openmd.backends.loaded_chat import HOSTS
    urls = [f"https://{host}/chat/demo" for host in HOSTS] + ["https://example.org/article", "https://claude.ai/chat/demo"]
    result = subprocess.run([shutil.which("node"), str(script), str(ROOT / "src/openmd/browser_extension/background.js"), json.dumps(urls)], capture_output=True, text=True, encoding="utf-8", check=True, timeout=20)
    rows = json.loads(result.stdout)
    assert len(rows) == len(urls) * 4
    for row in rows:
        expect_snapshot = row["url"] not in urls[-2:] or row["entry"] == "snapshot"
        assert row["calls"] == ([{"kind": "snapshot", "id": 41}] if expect_snapshot else [{"kind": "url", "urls": [row["url"]]}]), row


def test_popup_supplies_tab_identity():
    source = (ROOT / "src/openmd/browser_extension/popup.js").read_text(encoding="utf-8")
    assert "tabId: t.id" in source
