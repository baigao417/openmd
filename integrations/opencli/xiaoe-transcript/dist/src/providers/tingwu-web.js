import { basename, extname } from "node:path";
import { copyFile, mkdir, writeFile } from "node:fs/promises";
import path from "node:path";
import { OpenCliPage } from "../opencli/client.js";
import { assertTingwuRecordIdentity, parseTingwuApiTranscriptResult, matchesTingwuRecordTitle } from "./tingwu-page.js";
import { sanitizeFileName } from "../utils/files.js";
import { runCommand } from "../utils/command.js";
import { verifySilentAudioTail } from "./audio-tail.js";
async function uploadFile(page, filePath, uploadName) {
    const fileName = uploadName || basename(filePath);
    const uploadPath = path.join(path.dirname(filePath), fileName);
    if (uploadPath !== filePath) {
        await copyFile(filePath, uploadPath);
    }
    await page.setFileInput([uploadPath], 'input[type="file"]');
    return fileName;
}
async function allowSlowOssUpload(page) {
    await page.evaluate(`(() => {
    if (location.hostname !== 'tingwu.aliyun.com') throw new Error('Unexpected Tingwu upload host');
    if (window.__xiaoeOssUploadTimeoutPatched) return;
    const open = XMLHttpRequest.prototype.open;
    const send = XMLHttpRequest.prototype.send;
    const uploads = new WeakSet();
    XMLHttpRequest.prototype.open = function(method, url, ...args) {
      const host = new URL(url, location.href).hostname;
      if (String(method).toUpperCase() === 'PUT' && /^.+\\.oss-cn-[a-z0-9-]+\\.aliyuncs\\.com$/.test(host)) uploads.add(this);
      else uploads.delete(this);
      return open.call(this, method, url, ...args);
    };
    XMLHttpRequest.prototype.send = function(body) {
      const bytes = body?.size ?? body?.byteLength ?? 0;
      if (uploads.has(this) && bytes > 0) {
        if (this.timeout > 0 && this.timeout < 300000) this.timeout = 300000;
        const stats = window.__xiaoeOssUploadStats ||= [];
        const stat = {bytes,timeoutMs:this.timeout,loaded:0,startedAt:Date.now(),status:0,event:'sending'};
        stats.push(stat); if (stats.length > 100) stats.shift();
        this.upload.addEventListener('progress', e => {stat.loaded=e.loaded;});
        for (const event of ['load','error','timeout','abort']) this.addEventListener(event, () => {
          stat.event=event;stat.status=this.status;stat.endedAt=Date.now();
        }, {once:true});
      }
      return send.call(this, body);
    };
    window.__xiaoeOssUploadTimeoutPatched = true;
  })()`);
}
async function openUploadModal(page) {
    const deadline = Date.now() + 120000;
    while (Date.now() < deadline) {
        const hasLocalCard = await page.evaluate(`
      (() => !!document.querySelector('[data-e2e-test-id="homepage_file_div"]'))()
    `).catch(() => false);
        if (!hasLocalCard) {
            await page.evaluate(`(() => {
        const home = document.querySelector('[data-e2e-test-id="home_fileTrans_div"]');
        if (home instanceof HTMLElement) home.click();
        return true;
      })()`).catch(() => undefined);
            await new Promise(resolve => setTimeout(resolve, 1000));
            continue;
        }
        const hasFileInput = await page.evaluate(`
      (() => !!document.querySelector('input[type="file"]'))()
    `).catch(() => false);
        if (hasFileInput) {
            return;
        }
        await page.evaluate(`(() => {
      const local = document.querySelector('[data-e2e-test-id="homepage_file_div"]');
      if (local instanceof HTMLElement) local.click();
      return true;
    })()`).catch(() => undefined);
        await new Promise(resolve => setTimeout(resolve, 1000));
    }
    const state = await page.evaluate(`({host:location.hostname,path:location.pathname})`).catch(() => ({ host: 'unavailable', path: '' }));
    throw new Error('Tingwu local upload modal did not appear at ' + state.host + state.path);
}
async function waitForStartReady(page, expectedFileName) {
    const title = expectedFileName.replace(/\.[^.]+$/, '');
    const deadline = Date.now() + 5 * 60 * 1000;
    while (Date.now() < deadline) {
        const state = await page.evaluate(`
      (() => {
        const bodyText = document.body.innerText || '';
        const btn = Array.from(document.querySelectorAll('button')).find((node) => (node.innerText || '').includes('开始转写'));
        return {
          ready: !!btn && !(btn instanceof HTMLButtonElement && btn.disabled) && bodyText.includes(${JSON.stringify(title)})
        };
      })()
    `).catch(() => ({ ready: false }));
        if (state.ready) {
            return;
        }
        await new Promise(resolve => setTimeout(resolve, 3000));
    }
    throw new Error(`Tingwu upload did not reach a clickable 开始转写 state for ${title}`);
}
async function startTranscription(page) {
    await page.evaluate(`(() => {
    const btn = Array.from(document.querySelectorAll('button')).find((node) => (node.innerText || '').includes('开始转写'));
    if (!(btn instanceof HTMLElement)) throw new Error('Tingwu 开始转写 button not found');
    btn.click();
    return true;
  })()`);
}
async function waitForAudioCoverage(page, input, uploadedName) {
    const probe = await runCommand("ffprobe", ["-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", input.audioPath], { timeoutMs: 30000 });
    const audioDurationSec = Number.parseFloat(probe.stdout);
    if (!Number.isFinite(audioDurationSec) || audioDurationSec <= 0)
        throw new Error("Unable to verify Tingwu audio duration");
    const expected = uploadedName.replace(/\.[^.]+$/, "");
    const deadline = Date.now() + input.timeoutMinutes * 60000;
    while (Date.now() < deadline) {
        const response = await page.evaluate(`(async () => {
      const parts = location.pathname.split('/');
      if (parts.length !== 4 || parts[1] !== 'doc' || parts[2] !== 'transcripts' || !parts[3]) throw new Error('Tingwu transcript path missing');
      const response = await fetch('/api/trans/getTransResult?c=web', {method:'POST', headers:{'content-type':'application/json'}, body:JSON.stringify({transId:parts[3]}), credentials:'include'});
      const api = await response.json();
      if (api.code !== '0' || !api.data?.result) return {ready:false,lastEndSec:0,paragraphs:0,url:location.href};
      if (api.data.tag?.showName !== ${JSON.stringify(expected)}) throw new Error('Tingwu audio coverage identity mismatch');
      const paragraphs = JSON.parse(api.data.result).pg || [];
      const lastEndMs = paragraphs.reduce((end, p) => (p.sc || []).reduce((last, w) => Math.max(last, w.et || 0), end), 0);
      const nonempty = paragraphs.filter(p => (p.sc || []).some(w => String(w.tc || '').trim()));
      return {ready:true,lastEndSec:lastEndMs/1000,paragraphs:nonempty.length,url:location.href,payload:{duration:api.data.duration,tag:{showName:api.data.tag.showName},result:api.data.result}};
    })()`);
        const { payload, ...evidence } = response;
        const transcript = payload ? parseTingwuApiTranscriptResult(payload, input.preferredTitle) : null;
        const coverage = evidence.lastEndSec / audioDurationSec;
        const snapshotComplete = evidence.ready && evidence.paragraphs > 0 && transcript?.segments.length === evidence.paragraphs;
        const silentTailVerified = snapshotComplete && coverage < 0.98
            ? await verifySilentAudioTail(input.audioPath, evidence.lastEndSec, audioDurationSec) : false;
        const receipt = JSON.stringify({ expectedTitle: expected, sourceUrl: input.sourceUrl, audioDurationSec, ...evidence, coverage, silentTailVerified, checkedAt: new Date().toISOString() }, null, 2);
        await writeFile(path.join(input.tempDir, "tingwu-coverage.json"), receipt);
        if (snapshotComplete && (coverage >= 0.98 || silentTailVerified)) {
            const evidenceDir = process.env.OPENCLI_TINGWU_EVIDENCE_DIR;
            if (evidenceDir) {
                await mkdir(evidenceDir, { recursive: true });
                const resourceId = input.resourceId || /(?:l|v)_[a-z0-9]+/.exec(input.sourceUrl)?.[0] || path.basename(input.tempDir);
                await writeFile(path.join(evidenceDir, resourceId + ".json"), receipt);
            }
            // Return the exact API snapshot that passed the coverage and paragraph
            // gates. A later transient API failure must never select virtualized UI
            // fragments while retaining this snapshot's full duration.
            return { ...transcript, transcriptDurationSec: evidence.lastEndSec };
        }
        await new Promise(resolve => setTimeout(resolve, 15000));
    }
    throw new Error("Tingwu transcript did not reach 98 percent independent audio coverage; local audio retained");
}
function isTranscriptUrl(url) {
    return /\/doc\/transcripts\//.test(url);
}
async function protectActiveUploadState(page, expectedTitle) {
    const installed = await page.evaluate(`(() => {
    const root = document.querySelector('[data-e2e-test-id="homeTransFile_list_div"]');
    const fiberKey = root && Object.keys(root).find(key => key.startsWith('__reactFiber'));
    let fiber = fiberKey ? root[fiberKey] : null;
    let store;
    for (let depth = 0; fiber && depth < 40; depth++, fiber = fiber.return) {
      const props = fiber.memoizedProps || {};
      const candidate = props.value?.store || props.store;
      if (candidate?.getState?.().transModel?.transFileList instanceof Map) { store = candidate; break; }
    }
    if (!store?.subscribe || !store?.dispatch) return false;
    const expected = ${JSON.stringify(expectedTitle)};
    let cached;
    let restoring = false;
    let cancelled = false;
    window.__xiaoeUploadStateGuard = { expected, captured: false, restored: 0, finished: false };
    const info = window.__xiaoeUploadStateGuard;
    const cancel = event => {
      const control = event.target?.closest?.('[data-e2e-test-id="transFile_cancelItem_icon"], [data-e2e-test-id="transFile_deleteItem_icon"]');
      if (control?.closest('li')?.innerText?.includes(expected)) cancelled = true;
    };
    document.addEventListener('click', cancel, true);
    let unsubscribe;
    const release = () => { info.finished = true; unsubscribe?.(); document.removeEventListener('click', cancel, true); };
    unsubscribe = store.subscribe(() => {
      if (restoring || info.finished) return;
      if (cancelled) { release(); return; }
      const model = store.getState().transModel;
      const files = model?.transFileList;
      if (!(files instanceof Map)) return;
      const row = [...files.values()].find(file => file.title === expected);
      if (row && ![4, 5].includes(Number(row.status))) { release(); return; }
      if (row) { cached = { ...cached, ...row }; info.captured = true; return; }
      if (!cached || ![4, 5].includes(Number(cached.status))) return;
      // The site's init/getUnTransFiles effects replace the whole Map and omit
      // local status-4 records. Losing this row also loses the getLink needed
      // by its syncPutLink completion callback. Keep only this active upload.
      restoring = true;
      try {
        const restored = new Map(files);
        restored.set(cached.fileId, cached);
        store.dispatch({ type: 'transModel/updateState', payload: { transFileList: restored } });
        info.restored++;
      } finally { restoring = false; }
    });
    return true;
  })()`);
    if (!installed)
        throw new Error("Tingwu upload state could not be protected before submission; local audio retained");
}
export async function openTranscriptRecord(page, expectedFileName, timeoutMinutes) {
    const title = expectedFileName.replace(/\.[^.]+$/, '');
    const deadline = Date.now() + timeoutMinutes * 60 * 1000;
    let submissionProgressAt = Date.now();
    let uploadPercent = -1;
    let uploadRetries = 0;
    let lastUploadRetryAt = 0;
    let recordSeen = false;
    let lastRecordQueryAt = 0;
    while (Date.now() < deadline) {
        // The upload drawer is initially collapsed. Its rows are not mounted until
        // expanded, so a missing row cannot mean that the upload has finished.
        await page.evaluate(`(() => {
      const root = document.querySelector('[data-e2e-test-id="homeTransFile_list_div"]');
      if (root && !root.querySelector('.titSpan')) {
        const toggle = root.querySelector('[data-e2e-test-id="transFile_upAndDown_icon"]');
        const button = toggle?.closest('div');
        if (button instanceof HTMLElement) button.click();
      }
    })()`).catch(() => undefined);
        const upload = await page.evaluate(`(() => {
      const expected = ${JSON.stringify(title)};
      const row = Array.from(document.querySelectorAll('li')).find(node =>
        Array.from(node.querySelectorAll('.titSpan')).some(n => (n.textContent || '').trim() === expected));
      const text = row?.innerText || '';
      const percent = /已上传\\s*\\((\\d+)%\\)/.exec(text);
      return { active: !!percent, failed: !!row?.classList.contains('error'), percent: percent ? Number(percent[1]) : -1 };
    })()`).catch(() => ({ active: false, failed: false, percent: -1 }));
        if (upload.percent > uploadPercent) {
            uploadPercent = upload.percent;
            submissionProgressAt = Date.now();
        }
        if (upload.failed) {
            if (Date.now() - lastUploadRetryAt >= 30000) {
                if (uploadRetries >= 2)
                    throw new Error(`Tingwu upload failed after two retries for ${title}; local audio retained`);
                const retried = await page.evaluate(`(() => {
          const row = Array.from(document.querySelectorAll('li.error')).find(node =>
            Array.from(node.querySelectorAll('.titSpan')).some(n => (n.textContent || '').trim() === ${JSON.stringify(title)}));
          const retry = row?.querySelector('.titleImgRe');
          if (!(retry instanceof HTMLElement)) return false;
          retry.click(); return true;
        })()`);
                if (!retried)
                    throw new Error(`Tingwu upload failed without an available retry for ${title}`);
                uploadRetries += 1;
                lastUploadRetryAt = Date.now();
                submissionProgressAt = Date.now();
            }
            await new Promise(resolve => setTimeout(resolve, 2000));
            continue;
        }
        const currentUrl = await page.evaluate('location.href').catch(() => '');
        if (isTranscriptUrl(currentUrl)) {
            const ready = await page.evaluate(`(() => (document.body.innerText || '').includes('原文'))()`)
                .catch(() => false);
            if (ready)
                return;
        }
        else {
            // The home page can retain a stale recent-record list after its upload
            // drawer finishes. Query the same list endpoint used by My records,
            // then navigate only to the exact fresh upload title.
            if (Date.now() - lastRecordQueryAt >= 15000) {
                lastRecordQueryAt = Date.now();
                const record = await page.evaluate(`(async () => {
          const response = await fetch('/api/trans/request?c=web', {method:'POST',headers:{'content-type':'application/json'},credentials:'include',body:JSON.stringify({action:'getTransList',version:'1.0',userId:'',filter:{status:[],fileTypes:[],beginTime:'',mediaType:'',endTime:'',showName:'',read:'',dirId:0,lang:'',shareUserId:'',client:''},orderType:0,orderDesc:true,preview:1,pageNo:1,pageSize:50})});
          const result = await response.json();
          if (result.code !== '0' || !Array.isArray(result.data)) return null;
          const expected = ${JSON.stringify(title)};
          const match = result.data.find(item => item.tag?.showName === expected && /^[a-z0-9]+$/i.test(item.transId || ''));
          return match ? {transId:match.transId,status:Number(match.status),statusMsg:match.statusMsg,uploadGuard:window.__xiaoeUploadStateGuard} : null;
        })()`).catch(() => null);
                if (record) {
                    if (process.env.OPENCLI_TINGWU_EVIDENCE_DIR) {
                        await mkdir(process.env.OPENCLI_TINGWU_EVIDENCE_DIR, { recursive: true });
                        await writeFile(path.join(process.env.OPENCLI_TINGWU_EVIDENCE_DIR, 'active-tingwu-server-status.json'), JSON.stringify({ expectedTitle: title, ...record, checkedAt: new Date().toISOString() }, null, 2));
                    }
                    if (record.status === 0) {
                        await page.goto('https://tingwu.aliyun.com/doc/transcripts/' + record.transId);
                        recordSeen = true;
                        continue;
                    }
                    if (record.status === 1 || record.status === 3) {
                        recordSeen = true;
                    }
                    else if (record.status === 2 || record.status === 11 || record.status >= 100) {
                        throw new Error('Tingwu server reported upload/transcription failure, status=' + record.status + '; local audio retained');
                    }
                }
            }
            const transcriptTab = (await page.tabs().catch(() => []))
                .find((tab) => isTranscriptUrl(tab.url || '')
                && matchesTingwuRecordTitle((tab.title || '').replace(/\s+-\s+通义听悟\s*$/, ''), title)
                && (tab.page || tab.targetId || tab.id));
            const tabId = transcriptTab?.page || transcriptTab?.targetId || transcriptTab?.id;
            if (tabId) {
                await page.selectTab(tabId);
                continue;
            }
        }
        if (!recordSeen && Date.now() - submissionProgressAt >= 30 * 60 * 1000) {
            throw new Error(`Tingwu did not create a visible transcript record for ${title} after submission`);
        }
        await new Promise(resolve => setTimeout(resolve, 2000));
    }
    throw new Error(`Tingwu transcript record ${title} did not open before timeout`);
}
export const tingwuProvider = {
    async transcribe(input) {
        const workspace = `xiaoe-to-md:provider:tingwu:${Date.now()}`;
        const page = new OpenCliPage(workspace);
        try {
            await writeFile(path.join(input.tempDir, 'tingwu-session.json'), JSON.stringify({ workspace, sourceUrl: input.sourceUrl, resourceId: input.resourceId, stage: 'opening-home', at: new Date().toISOString() }, null, 2));
            await page.goto(process.env.TINGWU_URL || 'https://tingwu.aliyun.com/home');
            await openUploadModal(page);
            await allowSlowOssUpload(page);
            const ext = extname(input.audioPath) || '.mp3';
            const resourceId = input.resourceId || /(?:l|v)_[a-z0-9]+/.exec(input.sourceUrl)?.[0];
            const uploadBase = resourceId ? `wealth-${resourceId}-${Date.now().toString(36)}` : `${sanitizeFileName(input.preferredTitle).slice(0, 32)}-${Date.now().toString(36)}`;
            await protectActiveUploadState(page, uploadBase);
            const uploadedName = await uploadFile(page, input.audioPath, `${uploadBase}${ext}`);
            await waitForStartReady(page, uploadedName);
            await startTranscription(page);
            await writeFile(path.join(input.tempDir, 'tingwu-session.json'), JSON.stringify({ workspace, sourceUrl: input.sourceUrl, resourceId: input.resourceId, uploadedName, stage: 'submitted', at: new Date().toISOString() }, null, 2));
            await openTranscriptRecord(page, uploadedName, input.timeoutMinutes);
            await assertTingwuRecordIdentity(page, uploadedName);
            return await waitForAudioCoverage(page, input, uploadedName);
        }
        finally {
            await page.closeWindow();
        }
    }
};
