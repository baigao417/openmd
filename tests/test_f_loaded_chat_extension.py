"""Execute extension entry points with a synthetic Chrome API, never live tabs."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(not shutil.which("node"), reason="Node is needed for the extension harness")
def test_video_context_menus_cover_all_web_pages_and_target_the_clicked_context(tmp_path):
    script = tmp_path / "video-menu.cjs"
    script.write_text(r'''
const fs=require('fs'), vm=require('vm');
const listeners={}; const event=n=>({addListener:f=>(listeners[n]??=[]).push(f)});
const created=[]; const native=[];
const chrome={runtime:{onInstalled:event('installed'),onMessage:event('message'),onStartup:event('startup'),sendNativeMessage:(_h,m,cb)=>{native.push(m);cb({ok:true,protocol_version:1,save_to:'synthetic',job:{id:'synthetic',url:m.url}});}},alarms:{create:()=>{},onAlarm:event('alarm')},notifications:{create:()=>{},onButtonClicked:event('notification'),onClosed:event('closed')},contextMenus:{onClicked:event('context'),removeAll:cb=>cb(),create:item=>created.push(item)},storage:{local:{get:async()=>({settings:{locations:[{id:'d',name:'D',path:'synthetic'}],defaultId:'d'}}),set:async()=>{}},onChanged:event('storage')},action:{onClicked:event('icon'),setPopup:async()=>{},setTitle:async()=>{},setBadgeText:async()=>{},setBadgeBackgroundColor:async()=>{}},tabs:{query:async()=>[]}};
const ctx=vm.createContext({chrome,URL,setTimeout:()=>{},crypto:{randomUUID:()=> 'id'},console});
vm.runInContext(fs.readFileSync(process.argv[2],'utf8').replace(/^import .*;$/m,''),ctx);
(async()=>{for(const fn of listeners.installed||[]) await fn({reason:'install'}); const menus=created.filter(x=>x.id.startsWith('save-video-')); const click=(info)=>(listeners.context||[])[0](info,{url:'https://example.org/feed',title:'Feed'}); await click({menuItemId:'save-video-file',pageUrl:'https://example.org/feed',linkUrl:'https://example.org/post/1'}); await click({menuItemId:'save-video-text',pageUrl:'https://example.org/post/2'}); await click({menuItemId:'save-video-file',pageUrl:'https://example.org/post/3',srcUrl:'blob:https://example.org/abc'}); console.log(JSON.stringify({menus,native}));})().catch(e=>{console.error(e);process.exitCode=1;});
''', encoding='utf-8')
    result = subprocess.run([shutil.which("node"), str(script), str(ROOT / "src/openmd/browser_extension/background.js")], capture_output=True, text=True, encoding="utf-8", check=True, timeout=20)
    payload = json.loads(result.stdout)
    assert {menu["id"] for menu in payload["menus"]} == {"save-video-text", "save-video-file"}
    assert all(menu["documentUrlPatterns"] == ["http://*/*", "https://*/*"] for menu in payload["menus"])
    assert all(menu["contexts"] == ["page", "link", "video"] for menu in payload["menus"])
    sent = [(message["url"], message["video"]) for message in payload["native"] if message.get("url")]
    assert sent[-3:] == [("https://example.org/post/1", "file"), ("https://example.org/post/2", "text"), ("https://example.org/post/3", "file")]


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
    ai_urls = [f"https://{host}/chat/demo" for host in HOSTS]
    xhs_urls = [
        "https://www.xiaohongshu.com/explore/abc123",
        "https://www.xiaohongshu.com/discovery/item/def456",
    ]
    urls = ai_urls + xhs_urls + ["https://www.douyin.com/video/123456", "https://example.org/article", "https://claude.ai/chat/demo"]
    result = subprocess.run([shutil.which("node"), str(script), str(ROOT / "src/openmd/browser_extension/background.js"), json.dumps(urls)], capture_output=True, text=True, encoding="utf-8", check=True, timeout=20)
    rows = json.loads(result.stdout)
    assert len(rows) == len(urls) * 4
    loaded_pages = set(ai_urls + xhs_urls)
    for row in rows:
        expect_snapshot = row["url"] in loaded_pages or row["entry"] == "snapshot"
        assert row["calls"] == ([{"kind": "snapshot", "id": 41}] if expect_snapshot else [{"kind": "url", "urls": [row["url"]]}]), row


def test_popup_supplies_tab_identity():
    source = (ROOT / "src/openmd/browser_extension/popup.js").read_text(encoding="utf-8")
    assert "tabId: t.id" in source


@pytest.mark.skipif(not shutil.which("node"), reason="Node is needed for the extension harness")
def test_extension_settings_forward_thinking_option_without_live_chrome(tmp_path):
    script = tmp_path / "thinking.cjs"
    script.write_text(r'''
const fs=require('fs'), vm=require('vm');
const event={addListener:()=>{}};
const store={settings:{locations:[{id:'default',path:'synthetic'}],defaultId:'default'}};
const chrome={runtime:{onInstalled:event,onMessage:event,onStartup:event,sendNativeMessage:(_h,_m,cb)=>cb({ok:true,protocol_version:1,extension_hash:'a'.repeat(64)})},alarms:{create:()=>{},onAlarm:event},notifications:{onButtonClicked:event,onClosed:event},contextMenus:{onClicked:event},storage:{local:{get:async()=>store,set:async()=>{}},onChanged:event},action:{onClicked:event,setPopup:async()=>{},setTitle:async()=>{}}};
const ctx=vm.createContext({chrome,URL,setTimeout:()=>{}});
vm.runInContext(fs.readFileSync(process.argv[2],'utf8').replace(/^import .*;$/m,''),ctx);
vm.runInContext(`var sent=[],snapshotUrl='https://chatgpt.com/c/demo';captureSnapshot=async()=>({html:'<p>Synthetic</p>',url:snapshotUrl});native=async(message)=>{sent.push(message);return {ok:true,job:{id:'synthetic'}}};track=async()=>{};`,ctx);
(async()=>{for(const value of [undefined,false,true]){store.settings.includeThinking=value;await vm.runInContext('saveSnapshot(42)',ctx);} for(const value of [false,true]){store.settings.allowFeedbackPackage=value;await vm.runInContext('saveSnapshot(42)',ctx);} store.settings.includeThinking=true; vm.runInContext(`snapshotUrl='https://www.xiaohongshu.com/explore/abc123'`,ctx); await vm.runInContext('saveSnapshot(42)',ctx); console.log(JSON.stringify(ctx.sent));})().catch(e=>{console.error(e);process.exitCode=1;});
''', encoding="utf-8")
    run = subprocess.run([shutil.which("node"), str(script), str(ROOT / "src/openmd/browser_extension/background.js")], capture_output=True, text=True, encoding="utf-8", check=True, timeout=20)
    messages = json.loads(run.stdout)
    assert [message["include_thinking"] for message in messages[:3]] == [False, False, True]
    assert [message["feedback_opt_in"] for message in messages[3:5]] == [False, True]
    assert messages[5]["include_thinking"] is False
    html = (ROOT / "src/openmd/browser_extension/options.html").read_text(encoding="utf-8")
    js = (ROOT / "src/openmd/browser_extension/options.js").read_text(encoding="utf-8")
    assert 'id="include-thinking"' in html
    assert 'settings.includeThinking' in js and '.checked' in js


@pytest.mark.skipif(not shutil.which("node"), reason="Node is needed for the extension harness")
def test_feedback_opt_in_notification_and_button(tmp_path):
    script = tmp_path / "feedback.cjs"
    script.write_text(r'''
const fs=require('fs'), vm=require('vm');
const listeners={}; const event=n=>({addListener:f=>(listeners[n]??=[]).push(f)});
const store={settings:{locations:[{id:'default',path:'synthetic'}],defaultId:'default',allowFeedbackPackage:false}};
const notices=[]; const native=[];
const chrome={runtime:{onInstalled:event('installed'),onMessage:event('message'),onStartup:event('startup'),sendNativeMessage:(_h,m,cb)=>{native.push(m);cb({ok:true,job:{id:'job'}})}},alarms:{create:()=>{},onAlarm:event('alarm')},notifications:{create:(id,n)=>notices.push({id,n}),onButtonClicked:event('notification'),onClosed:event('closed')},contextMenus:{onClicked:event('context')},storage:{local:{get:async()=>store,set:async v=>Object.assign(store,v)},onChanged:event('storage')},action:{onClicked:event('icon'),setPopup:async()=>{},setTitle:async()=>{},setBadgeBackgroundColor:async()=>{},setBadgeText:async()=>{}},tabs:{query:async()=>[]}};
const ctx=vm.createContext({chrome,URL,setTimeout:()=>{},crypto:{randomUUID:()=> 'synthetic'},console});
vm.runInContext(fs.readFileSync(process.argv[2],'utf8').replace(/^import .*;$/m,''),ctx);
(async()=>{await vm.runInContext('notifyFeedback({id:"job",result:{status:"partial"}},{})',ctx); store.settings.allowFeedbackPackage=true; await vm.runInContext('notifyFeedback({id:"job",result:{status:"partial"}},{})',ctx); for(const f of listeners.notification||[]) await f('openmd-feedback:job',0); console.log(JSON.stringify({notices:notices,native}));})().catch(e=>{console.error(e);process.exitCode=1;});
''', encoding="utf-8")
    result = subprocess.run([shutil.which("node"), str(script), str(ROOT / "src/openmd/browser_extension/background.js")], capture_output=True, text=True, encoding="utf-8", check=True, timeout=20)
    payload = json.loads(result.stdout)
    assert len([item for item in payload["notices"] if item["id"].startswith("openmd-feedback:")]) == 1
    assert payload["native"][-1]["type"] == "feedback_report"
