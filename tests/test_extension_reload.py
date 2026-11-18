"""Execute the actual worker in Node VM with synthetic chrome APIs only."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
HARNESS = r'''
const fs = require('fs');
const vm = require('vm');
const assert = require('assert/strict');
const worker = fs.readFileSync(process.argv[2], 'utf8').replace(/^import .*;$/m, '');
const HASH = 'a'.repeat(64), NEXT = 'b'.repeat(64);
function boot(store = {}, response = {ok:true,extension_updated:false,extension_hash:HASH,protocol_version:1}) {
  const events = {}, notices = [], requests = [], timers = [], alarms = [];
  let reloads = 0, nativeError;
  const event = name => ({addListener: fn => {events[name] = fn;}});
  store.settings ||= {locations:[{id:'default',path:'synthetic',name:'Synthetic'}],defaultId:'default'};
  const api = {response, holdGet:null, holdSet:null, holdNative:null, capture:null};
  const chrome = {
    runtime:{onInstalled:event('installed'),onMessage:event('message'),onStartup:event('startup'),
      reload:() => reloads++, openOptionsPage:() => {},
      sendNativeMessage:(_host, msg, cb) => {
        requests.push(msg);
        if (api.holdNative && msg.type !== 'ping') return api.holdNative(msg, cb);
        chrome.runtime.lastError = nativeError ? {message:nativeError} : undefined;
        cb(msg.type === 'statuses' ? {ok:true,jobs:Object.fromEntries(msg.ids.map(id=>[id,{state:'done'}]))} : api.response);
        delete chrome.runtime.lastError;
      }},
    alarms:{create:(name, spec) => alarms.push({name,...spec}),onAlarm:event('alarm')},
    notifications:{create:(id, notice)=>notices.push(notice),clear:async()=>{},onButtonClicked:event('notice'),onClosed:event('closed')},
    contextMenus:{onClicked:event('context'),removeAll:cb=>cb(),create:()=>{}},
    storage:{local:{get:async keys => {
      if (api.holdGet) await api.holdGet(keys);
      return JSON.parse(JSON.stringify(store));
    },set:async values => {
      if(api.holdSet) await api.holdSet(values);
      Object.assign(store,JSON.parse(JSON.stringify(values)));
    }},onChanged:event('storage')},
    action:{onClicked:event('icon'),setPopup:async()=>{},setTitle:async()=>{},setBadgeText:async()=>{},setBadgeBackgroundColor:async()=>{}},
    scripting:{executeScript:async()=>api.capture ? api.capture() : [{result:{html:'<p>synthetic</p>',url:'https://example.org',title:'Synthetic'}}]},
    tabs:{query:async()=>[{id:41,url:'https://example.org',windowId:1}]},
  };
  const context = vm.createContext({chrome, URL, crypto:{randomUUID:()=> 'synthetic'},
    setTimeout:(fn,ms)=>{timers.push({fn,ms});},console});
  vm.runInContext(worker, context);
  return {context,store,events,notices,requests,timers,alarms,api,
    get reloads(){return reloads;},error:value=>{nativeError=value;},run:code=>vm.runInContext(code,context)};
}
const settle = async () => {for(let i=0;i<60;i++) await new Promise(resolve=>setImmediate(resolve));};
const upgrade = s => {s.api.response={ok:true,extension_updated:true,extension_hash:NEXT,protocol_version:1};};
const scenario = process.argv[3];
(async()=>{
  const s = boot(); await settle();
  if(scenario === 'startup_alarm') {
    assert(s.requests.some(msg=>msg.type === 'ping'), 'must handshake even with configured locations');
    assert(s.alarms.some(alarm=>alarm.name === 'openmd-extension-update' && alarm.periodInMinutes >= 15));
    assert.equal(s.reloads, 0);
    const before=s.requests.length;
    await s.events.alarm({name:'foreign-vendor-alarm'}); await settle();
    assert.equal(s.requests.length,before);
    await s.events.alarm({name:'openmd-extension-update'}); await settle();
    assert(s.requests.length>before);
    assert.equal(s.reloads,0);
  } else if(scenario === 'idle_restart') {
    upgrade(s); await s.run('checkExtensionUpdate()'); await settle();
    assert.equal(s.reloads,1);
    await s.run('checkExtensionUpdate()'); assert.equal(s.reloads,1);
    const restarted=boot(s.store,s.api.response); await settle();
    assert.equal(restarted.reloads,0,'same hash must not reload after worker restart');
  } else if(scenario === 'hash_mismatch') {
    s.api.response={ok:true,extension_updated:false,extension_hash:NEXT,protocol_version:1};
    await s.run('checkExtensionUpdate()'); await settle();
    assert.equal(s.reloads,1,'another ping may have already refreshed the copy');
  } else if(scenario === 'busy_poll') {
    s.store.jobs={job:{batch:'batch',title:'synthetic'}};
    s.store.batches={batch:{total:1,failed:[]}};
    upgrade(s); await s.run('checkExtensionUpdate()');
    assert.equal(s.reloads,0);
    await s.run('poll()'); await settle();
    assert.equal(s.reloads,1,'must reload after the final tracked save finishes');
  } else if(scenario === 'snapshot_inflight') {
    let release;
    s.api.capture=()=>new Promise(resolve=>{release=()=>resolve([{result:{html:'<p>x</p>',url:'https://example.org'}}]);});
    const save=s.run('saveSnapshot(41)'); await settle();
    upgrade(s); await s.run('checkExtensionUpdate()');
    assert.equal(s.reloads,0,'snapshot acquisition is already an active save');
    s.api.response={ok:true,job:{id:'job',url:'https://example.org'}};
    release(); await save; await settle();
    // poll uses synthetic done statuses and should flush the pending update.
    assert.equal(s.reloads,1);
  } else if(scenario === 'save_during_commit') {
    let release;
    s.api.holdSet=values=>{
      if(!values.extensionUpdate?.reloadedHashes?.includes(NEXT)) return;
      s.api.holdSet=null;
      return new Promise(resolve=>{release=resolve;});
    };
    upgrade(s); const check=s.run('checkExtensionUpdate()'); await settle();
    assert(release,'reload decision must be persisted first');
    s.api.capture=()=>new Promise(()=>{});
    s.run('saveSnapshot(41)'); await settle();
    release(); await check; await settle();
    assert.equal(s.reloads,0,'save beginning while reload record is persisted must win');
  } else if(scenario === 'restart_pending') {
    s.store.jobs={job:{batch:'batch',title:'synthetic'}};
    s.store.batches={batch:{total:1,failed:[]}};
    upgrade(s); await s.run('checkExtensionUpdate()'); assert.equal(s.reloads,0);
    const restarted=boot(s.store,s.api.response); await settle();
    assert.equal(restarted.reloads,1,'pending reload survives service worker restart');
  } else { throw new Error('unknown scenario: '+scenario); }
  console.log(JSON.stringify({scenario,ok:true,reloads:s.reloads,requests:s.requests.length}));
})().catch(error=>{console.error(error);process.exitCode=1;});
'''


def run_scenario(tmp_path, scenario):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is needed for the offline worker harness")
    script = tmp_path / "reload.cjs"
    script.write_text(HARNESS, encoding="utf-8")
    result = subprocess.run([node, str(script), str(ROOT / "src/openmd/browser_extension/background.js"), scenario],
                            capture_output=True, text=True, encoding="utf-8", timeout=20)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["ok"] is True


@pytest.mark.parametrize("scenario", ["startup_alarm", "idle_restart", "hash_mismatch", "busy_poll",
                                     "snapshot_inflight", "save_during_commit", "restart_pending"])
def test_extension_update_lifecycle(tmp_path, scenario):
    run_scenario(tmp_path, scenario)
