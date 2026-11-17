// Minimal port of archived Tingwu upload / poll / API export logic.
// Uses the pinned OpenCLI browser session, not credentials or legacy plugins.
import {copyFile, unlink} from 'node:fs/promises';
import {randomUUID} from 'node:crypto';
import {dirname, join, resolve} from 'node:path';
import {pathToFileURL} from 'node:url';

const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const fail = code => { throw new Error(code); };
let activePhase = 'startup';
const phaseSeconds = {};
async function phase(name, call) {
  activePhase = name;
  const started = performance.now();
  try {return await call();}
  finally {phaseSeconds[name] = (performance.now() - started) / 1000;}
}

export function parseTranscript(payload, expectedTitle, duration) {
  if (payload?.tag?.showName !== expectedTitle) fail('tingwu_identity_mismatch');
  let paragraphs;
  try { paragraphs = JSON.parse(payload.result).pg; } catch { fail('tingwu_protocol_invalid'); }
  if (!Array.isArray(paragraphs)) fail('tingwu_protocol_invalid');
  let end = 0, nonempty = 0;
  const segments = [];
  for (const paragraph of paragraphs) {
    const words = Array.isArray(paragraph.sc) ? paragraph.sc : [];
    const text = words.map(w => typeof w.tc === 'string' ? w.tc : '').join('').trim();
    if (!text) continue;
    nonempty++;
    const start = words.find(w => Number.isFinite(w.bt) && w.bt >= 0)?.bt;
    const last = words.reduce((n, w) => Number.isFinite(w.et) ? Math.max(n, w.et) : n, 0);
    end = Math.max(end, last / 1000);
    if (start !== undefined) segments.push({start_seconds: start / 1000, text});
  }
  if (!segments.length || nonempty !== segments.length || end / duration < .98) return null;
  if (end / duration > 1.02) fail('tingwu_identity_mismatch');
  return {ok: true, segments, coverage_end_seconds: end};
}

async function openUpload(page, deadline) {
  while (Date.now() < deadline) {
    const state = await page.evaluate(`(() => {
      if (location.hostname !== 'tingwu.aliyun.com') return 'auth';
      if (document.querySelector('input[type="file"]')) return 'ready';
      const local = document.querySelector('[data-e2e-test-id="homepage_file_div"]');
      const home = document.querySelector('[data-e2e-test-id="home_fileTrans_div"]');
      if (local instanceof HTMLElement) {local.click(); return 'waiting';}
      if (home instanceof HTMLElement) {home.click(); return 'waiting';}
      return /登录|扫码/.test(document.body.innerText || '') ? 'auth' : 'waiting';
    })()`);
    if (state === 'auth') fail('tingwu_auth_required');
    if (state === 'ready') return;
    await sleep(1000);
  }
  fail('tingwu_upload_timeout');
}

async function protectUpload(page, title) {
  // The site's getUnTransFiles effect may replace its Map and drop an active
  // local upload. Port only the guard for this unique upload, released on exit.
  const ready = await page.evaluate(`(() => {
    const root = document.querySelector('[data-e2e-test-id="homeTransFile_list_div"]');
    const key = root && Object.keys(root).find(k => k.startsWith('__reactFiber'));
    let fiber = key ? root[key] : null, store;
    for (let i = 0; fiber && i < 40; i++, fiber = fiber.return) {
      const p = fiber.memoizedProps || {}, candidate = p.value?.store || p.store;
      if (candidate?.getState?.().transModel?.transFileList instanceof Map) {store = candidate; break;}
    }
    if (!store?.subscribe || !store?.dispatch) return false;
    const expected = ${JSON.stringify(title)};
    let cached, restoring = false, finished = false, unsubscribe;
    const cancel = event => {
      const control = event.target?.closest?.('[data-e2e-test-id="transFile_cancelItem_icon"], [data-e2e-test-id="transFile_deleteItem_icon"]');
      if (control?.closest('li')?.innerText?.includes(expected)) release();
    };
    const release = () => {finished = true; unsubscribe?.(); document.removeEventListener('click', cancel, true);};
    document.addEventListener('click', cancel, true);
    window.__openmdReleaseUploadGuard = release;
    unsubscribe = store.subscribe(() => {
      if (restoring || finished) return;
      const files = store.getState().transModel?.transFileList;
      if (!(files instanceof Map)) return;
      const row = [...files.values()].find(f => f.title === expected);
      if (row && ![4,5].includes(Number(row.status))) {release(); return;}
      if (row) {cached = {...cached, ...row}; return;}
      if (!cached || ![4,5].includes(Number(cached.status))) return;
      restoring = true;
      try {const next = new Map(files); next.set(cached.fileId, cached);
        store.dispatch({type:'transModel/updateState', payload:{transFileList:next}});
      } finally {restoring = false;}
    });
    return true;
  })()`);
  if (!ready) fail('tingwu_protocol_invalid');
}

async function allowSlowUpload(page) {
  // Preserve the archived fix for the site's short OSS XHR timeout. No headers,
  // signed locators, request bodies or account values are read or exported.
  await page.evaluate(`(() => {
    if (location.hostname !== 'tingwu.aliyun.com') throw new Error('tingwu_auth_required');
    const originalOpen = XMLHttpRequest.prototype.open, originalSend = XMLHttpRequest.prototype.send;
    const uploads = new WeakSet();
    XMLHttpRequest.prototype.open = function(method, url, ...args) {
      const host = new URL(url, location.href).hostname;
      if (String(method).toUpperCase() === 'PUT' && /^.+\\.oss-cn-[a-z0-9-]+\\.aliyuncs\\.com$/.test(host)) uploads.add(this);
      return originalOpen.call(this, method, url, ...args);
    };
    XMLHttpRequest.prototype.send = function(body) {
      if (uploads.has(this) && this.timeout > 0 && this.timeout < 300000) this.timeout = 300000;
      return originalSend.call(this, body);
    };
    window.__openmdReleaseUploadTimeout = () => {
      XMLHttpRequest.prototype.open = originalOpen; XMLHttpRequest.prototype.send = originalSend;
    };
  })()`);
}

async function waitStart(page, title, deadline) {
  while (Date.now() < deadline) {
    const ready = await page.evaluate(`(() => {
      const button = [...document.querySelectorAll('button')].find(n => n.innerText.includes('开始转写'));
      return !!button && !button.disabled && document.body.innerText.includes(${JSON.stringify(title)});
    })()`);
    if (ready) {
      await page.evaluate(`(() => {
        const button = [...document.querySelectorAll('button')].find(n => n.innerText.includes('开始转写'));
        if (!button || button.disabled) throw new Error('tingwu_upload_timeout');
        button.click(); return true;
      })()`);
      return;
    }
    await sleep(1000);
  }
  fail('tingwu_upload_timeout');
}

async function pollRecord(page, title, duration, deadline) {
  let recordId = null;
  while (Date.now() < deadline) {
    if (!recordId) {
      const record = await page.evaluate(`(async () => {
        const response = await fetch('/api/trans/request?c=web', {method:'POST', headers:{'content-type':'application/json'}, credentials:'include',
          body:JSON.stringify({action:'getTransList',version:'1.0',userId:'',filter:{status:[],fileTypes:[],beginTime:'',mediaType:'',endTime:'',showName:'',read:'',dirId:0,lang:'',shareUserId:'',client:''},orderType:0,orderDesc:true,preview:1,pageNo:1,pageSize:50})});
        if (response.status === 401 || response.status === 403) return {auth:true};
        const api = await response.json();
        if (String(api.code) !== '0' || !Array.isArray(api.data)) return null;
        const row = api.data.find(item => item.tag?.showName === ${JSON.stringify(title)} && /^[a-z0-9]+$/i.test(item.transId || ''));
        return row ? {id:row.transId, status:Number(row.status)} : null;
      })()`);
      if (record?.auth) fail('tingwu_auth_required');
      if (record && (record.status === 2 || record.status === 11 || record.status >= 100)) fail('tingwu_server_failed');
      if (record?.status === 0) recordId = record.id;
    }
    if (recordId) {
      // Export only the matching record's original API text; never virtualized
      // editor snippets, summaries, credentials or complete response objects.
      const payload = await page.evaluate(`(async () => {
        const response = await fetch('/api/trans/getTransResult?c=web', {method:'POST', headers:{'content-type':'application/json'}, credentials:'include', body:JSON.stringify({transId:${JSON.stringify(recordId)}})});
        if (response.status === 401 || response.status === 403) return {auth:true};
        const api = await response.json();
        if (String(api.code) !== '0' || !api.data?.result) return null;
        return {tag:{showName:api.data.tag?.showName},result:api.data.result};
      })()`);
      if (payload?.auth) fail('tingwu_auth_required');
      if (payload) {const result = parseTranscript(payload, title, duration); if (result) return result;}
    }
    await sleep(3000);
  }
  fail(recordId ? 'tingwu_coverage_incomplete' : 'tingwu_submission_timeout');
}

async function main() {
  const [host, audio, rawDuration, rawTimeout, mode] = process.argv.slice(2);
  const duration = Number(rawDuration), timeout = Number(rawTimeout);
  const moduleUrl = file => pathToFileURL(resolve(host, 'dist/src', file)).href;
  const {cli, Strategy} = await import(moduleUrl('registry-api.js'));
  const {getRegistry} = await import(moduleUrl('registry.js'));
  const {executeCommand} = await import(moduleUrl('execution.js'));
  const title = 'openmd-' + randomUUID();
  const uploaded = join(dirname(audio), title + '.mp3');
  cli({site:'openmd-tingwu', name:'audio', access:'write', domain:'tingwu.aliyun.com',
    strategy:Strategy.COOKIE, browser:true, navigateBefore:false,
    args:[{name:'timeout',type:'int',default:timeout}],
    async func(page) {
      try {
        const deadline = Date.now() + timeout * 1000;
        await phase('navigate', () => page.goto('https://tingwu.aliyun.com/home', {waitUntil:'domcontentloaded', settleMs:0}));
        await phase('open_upload', () => openUpload(page, Math.min(deadline, Date.now() + 60000)));
        if (mode === 'probe') return {ok:true,upload_ready:true};
        await allowSlowUpload(page);
        await protectUpload(page, title);
        await copyFile(audio, uploaded);
        await phase('upload_file', () => page.setFileInput([uploaded], 'input[type="file"]'));
        await phase('submit', () => waitStart(page, title, Math.min(deadline, Date.now() + 300000)));
        const result = await phase('poll', () => pollRecord(page, title, duration, deadline));
        return {...result, phase_seconds:phaseSeconds};
      } finally {
        await page.evaluate('window.__openmdReleaseUploadGuard?.()').catch(() => {});
        await page.evaluate('window.__openmdReleaseUploadTimeout?.()').catch(() => {});
        await unlink(uploaded).catch(() => {});
      }
    }
  });
  // Same pinned dispatcher ABI as the working Xiaoe adapters, including the
  // persistent write lease, context routing and owned-tab cleanup. No plugins.
  return executeCommand(getRegistry().get('openmd-tingwu/audio'), {}, false,
    {windowMode:'background',siteSession:'persistent',keepTab:false,trace:'off'});
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  try {process.stdout.write(JSON.stringify(await main()) + '\n');}
  catch (error) {
    const codes = new Set(['tingwu_auth_required', 'tingwu_upload_timeout', 'tingwu_submission_timeout', 'tingwu_server_failed', 'tingwu_identity_mismatch', 'tingwu_coverage_incomplete', 'tingwu_protocol_invalid']);
    const code = codes.has(error?.message) ? error.message : /BrowserConnect|SessionBusy|Bridge|BrowserCommand/.test(error?.name || '') ? 'tingwu_browser_unavailable' : 'tingwu_failed';
    process.stdout.write(JSON.stringify({ok:false,error_code:code,phase:activePhase,phase_seconds:phaseSeconds}) + '\n');
    process.exitCode = 1;
  }
}
