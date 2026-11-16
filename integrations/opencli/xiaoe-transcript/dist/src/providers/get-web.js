import { basename, extname } from "node:path";
import { readFile } from "node:fs/promises";
import { OpenCliPage } from "../opencli/client.js";
import { parseGetEventStream, summarizeGetStream } from "./get-parser.js";
import { mergeTranscriptParts } from "./transcript-merge.js";
import { runCommand } from "../utils/command.js";
import { splitBufferToBase64Chunks } from "../utils/upload-chunks.js";
import { formatTimestamp, parseTimestamp } from "../utils/time.js";
const NETWORK_INTERCEPTOR_JS = `(function(){if(window.__opencli_net)return;window.__opencli_net=[];var M=300,B=2000000,F=window.fetch;window.fetch=async function(){var r=await F.apply(this,arguments);try{var ct=r.headers.get('content-type')||'';if(ct.includes('json')||ct.includes('text/event-stream')||ct.includes('text')){var c=r.clone(),t=await c.text();if(window.__opencli_net.length<M){var b=t;if(ct.includes('application/json')&&t.length<=B){try{b=JSON.parse(t)}catch(e){}}window.__opencli_net.push({url:r.url||(arguments[0]&&arguments[0].url)||String(arguments[0]),method:(arguments[1]&&arguments[1].method)||'GET',status:r.status,ct:ct,body:b,size:t.length})}}}catch(e){}return r};var X=XMLHttpRequest.prototype,O=X.open,S=X.send;X.open=function(m,u){this._om=m;this._ou=u;return O.apply(this,arguments)};X.send=function(){var x=this;x.addEventListener('load',function(){try{var ct=x.getResponseHeader('content-type')||'';if((ct.includes('json')||ct.includes('text/event-stream')||ct.includes('text'))&&window.__opencli_net.length<M){var t=x.responseText,b=t;if(ct.includes('application/json')&&t.length<=B){try{b=JSON.parse(t)}catch(e){}}window.__opencli_net.push({url:x._ou,method:x._om||'GET',status:x.status,ct:ct,body:b,size:t?t.length:0})}}catch(e){}});return S.apply(this,arguments)}})()`;
const GET_SEGMENT_SECONDS = 600;
const GET_SEGMENT_TOLERANCE_SECONDS = 5;
function mimeFromFileName(fileName) {
    const ext = extname(fileName).toLowerCase();
    if (ext === ".mp3")
        return "audio/mpeg";
    if (ext === ".wav")
        return "audio/wav";
    if (ext === ".m4a")
        return "audio/mp4";
    if (ext === ".mp4")
        return "video/mp4";
    return "application/octet-stream";
}
async function uploadFileToGet(page, filePath) {
    const buffer = await readFile(filePath);
    const chunks = splitBufferToBase64Chunks(buffer, 512 * 1024);
    const fileName = basename(filePath);
    const mimeType = mimeFromFileName(fileName);
    await page.evaluate("(() => { window.__xiaoeUploadChunks = []; return true; })()");
    for (const chunk of chunks) {
        await page.evaluate(`(() => { window.__xiaoeUploadChunks.push(${JSON.stringify(chunk)}); return window.__xiaoeUploadChunks.length; })()`);
    }
    await page.evaluate(`
    (async () => {
      const chunks = window.__xiaoeUploadChunks || [];
      const arrays = chunks.map((chunk) => {
        const binary = atob(chunk);
        const bytes = new Uint8Array(binary.length);
        for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
        return bytes;
      });
      const total = arrays.reduce((sum, item) => sum + item.length, 0);
      const bytes = new Uint8Array(total);
      let offset = 0;
      for (const part of arrays) {
        bytes.set(part, offset);
        offset += part.length;
      }
      delete window.__xiaoeUploadChunks;
      const file = new File([bytes], ${JSON.stringify(fileName)}, { type: ${JSON.stringify(mimeType)} });
      const dt = new DataTransfer();
      dt.items.add(file);
      const target = document.querySelector('.upload-btn.media-box, .upload-wrapper, .editor-media-wrapper, [class*="upload"], [class*="drop"]');
      if (!(target instanceof HTMLElement)) throw new Error('Get upload target not found');
      for (const type of ['dragenter', 'dragover', 'drop']) {
        target.dispatchEvent(new DragEvent(type, { bubbles: true, cancelable: true, dataTransfer: dt }));
      }
      return { ok: true, name: file.name };
    })()
  `);
}
async function openImportPanel(page) {
    const deadline = Date.now() + 20000;
    while (Date.now() < deadline) {
        const ready = await page.evaluate(`
      (() => (document.body.innerText || '').includes('点此导入') || !!document.querySelector('.upload-btn.media-box, .upload-wrapper, .editor-media-wrapper'))()
    `);
        if (ready) {
            return;
        }
        await page.evaluate(`(() => {
      const target = document.querySelector('.item.import-media') || Array.from(document.querySelectorAll('div, span, button')).find((node) => (node.innerText || node.textContent || '').trim() === '导入音视频');
      if (target instanceof HTMLElement) target.click();
      return true;
    })()`).catch(() => undefined);
        await page.evaluate('new Promise(resolve => setTimeout(resolve, 1000))').catch(() => undefined);
    }
    throw new Error('Get upload panel did not appear');
}
async function clickGenerate(page) {
    await page.evaluate(`(() => {
    const btn = Array.from(document.querySelectorAll('button')).find((node) => (node.innerText || '').trim() === '生成笔记');
    if (!(btn instanceof HTMLElement)) throw new Error('Get 生成笔记 button not found');
    btn.click();
    return true;
  })()`);
}
async function waitForGenerateReady(page) {
    const deadline = Date.now() + 5 * 60 * 1000;
    while (Date.now() < deadline) {
        const ready = await page.evaluate(`(() => {
      const btn = Array.from(document.querySelectorAll('button')).find((node) => (node.innerText || '').trim() === '生成笔记');
      if (!(btn instanceof HTMLButtonElement)) return false;
      const bodyText = document.body.innerText || '';
      const uploadComplete = bodyText.includes('文件上传完成');
      return !btn.disabled && uploadComplete;
    })()`).catch(() => false);
        if (ready) {
            return;
        }
        await page.evaluate('new Promise(resolve => setTimeout(resolve, 3000))').catch(() => undefined);
    }
    throw new Error('Get upload did not reach a clickable 生成笔记 state');
}
function parseCreatedAt(value) {
    if (!value)
        return 0;
    const normalized = value.replace(' ', 'T');
    const parsed = Date.parse(normalized);
    return Number.isFinite(parsed) ? parsed : 0;
}
function extractSegmentsFromSentenceList(sentences) {
    return sentences.map((sentence) => {
        const startTime = sentence.start_time;
        const endTime = sentence.end_time;
        let startSec = 0;
        if (typeof startTime === "number") {
            startSec = startTime / 1000;
        }
        else if (typeof endTime === "number") {
            startSec = endTime / 1000;
        }
        else if (typeof sentence.seek === "number") {
            startSec = sentence.seek;
        }
        else {
            startSec = Number(sentence.start ?? sentence.begin ?? sentence.time ?? 0);
        }
        const text = String(sentence.text ?? sentence.sentence ?? sentence.content ?? sentence.transcript ?? sentence.origin ?? '').trim();
        const speakerRaw = sentence.speaker_name ?? sentence.speaker ?? sentence.speaker_id ?? sentence.me_speaker_id;
        return {
            timestamp: formatTimestamp(Number.isFinite(startSec) ? startSec : 0),
            startSec: Number.isFinite(startSec) ? startSec : 0,
            text,
            ...(speakerRaw ? { speaker: String(speakerRaw) } : {})
        };
    }).filter((segment) => segment.text.length > 0);
}
function extractChapters(sections) {
    return sections.map((section) => {
        const raw = section.seek ?? section.start_time ?? section.start ?? 0;
        const startSec = typeof raw === 'number' && raw > 1000 ? raw / 1000 : Number(raw);
        return {
            title: String(section.title ?? section.name ?? '未命名章节').trim(),
            timestamp: formatTimestamp(Number.isFinite(startSec) ? startSec : 0),
            startSec: Number.isFinite(startSec) ? startSec : 0
        };
    }).filter((section) => section.title.length > 0);
}
function extractSegmentsFromContent(content) {
    const segments = [];
    const regex = /\[(\d{2}:\d{2}(?::\d{2})?)\][^\n]*\n?([^\[]+)/g;
    for (const match of content.matchAll(regex)) {
        const timestamp = match[1];
        const text = (match[2] || '').replace(/<br\s*\/?>/g, '\n').trim();
        if (!text)
            continue;
        segments.push({
            timestamp,
            startSec: parseTimestamp(timestamp),
            text
        });
    }
    return segments;
}
async function waitForCompletedNote(page, startedAt, timeoutMinutes) {
    const deadline = Date.now() + timeoutMinutes * 60000;
    while (Date.now() < deadline) {
        await page.evaluate('new Promise(resolve => setTimeout(resolve, 5000))').catch(() => undefined);
        const captured = await page.evaluate('window.__opencli_net || []').catch(() => []);
        const streamBodies = captured
            .filter((entry) => String(entry.url || '').includes('stream_on_local_audio') && typeof entry.body === 'string')
            .map((entry) => String(entry.body));
        const streamSummary = summarizeGetStream(streamBodies.flatMap((body) => parseGetEventStream(body)));
        const notesResponse = [...captured]
            .reverse()
            .find((entry) => String(entry.url || '').includes('/voicenotes/web/notes?') && !!entry.body && typeof entry.body === 'object');
        const notes = (notesResponse?.body?.c?.list) ?? [];
        const note = notes.find((item) => {
            const createdAt = parseCreatedAt(item.created_at);
            if (streamSummary.noteId && (item.note_id === streamSummary.noteId || item.id === streamSummary.noteId)) {
                return true;
            }
            return createdAt >= startedAt - 60000 && (item.note_type === 'local_audio' || item.note_type === 'ai');
        }) ?? notes[0];
        const segments = streamSummary.sentences.length > 0
            ? extractSegmentsFromSentenceList(streamSummary.sentences)
            : extractSegmentsFromContent(String(note?.content ?? ''));
        const sections = extractChapters(streamSummary.sections);
        const rawText = String(note?.content ?? streamSummary.summaryText ?? '');
        if (note && (streamSummary.done || segments.length > 0 || rawText.length > 0)) {
            return { note, sections, segments, rawText };
        }
    }
    throw new Error('Get note generation did not finish before timeout');
}
async function getAudioDurationSec(filePath) {
    const result = await runCommand('ffprobe', ['-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', filePath], { timeoutMs: 30000 });
    const parsed = Number.parseFloat(result.stdout.trim());
    return Number.isFinite(parsed) ? parsed : 0;
}
async function splitAudioIntoParts(filePath, tempDir) {
    const duration = await getAudioDurationSec(filePath);
    if (duration <= GET_SEGMENT_SECONDS + GET_SEGMENT_TOLERANCE_SECONDS) {
        return [{ path: filePath, offsetSec: 0 }];
    }
    const parts = [];
    let index = 0;
    for (let offset = 0; offset < duration; offset += GET_SEGMENT_SECONDS) {
        const partPath = `${tempDir}\\get-segment-${String(index).padStart(3, '0')}.mp3`;
        await runCommand('ffmpeg', ['-y', '-ss', String(offset), '-t', String(GET_SEGMENT_SECONDS), '-i', filePath, '-codec:a', 'copy', partPath], { timeoutMs: 180000 });
        parts.push({ path: partPath, offsetSec: offset });
        index += 1;
    }
    return parts;
}
async function transcribeSingleSegment(input, audioPath, preferredTitle) {
    const page = new OpenCliPage(`xiaoe-to-md:provider:get:${Date.now()}`);
    const startedAt = Date.now();
    try {
        await page.goto(process.env.GET_BIJI_URL || 'https://www.biji.com/');
        await openImportPanel(page);
        await uploadFileToGet(page, audioPath);
        await waitForGenerateReady(page);
        await page.evaluate(NETWORK_INTERCEPTOR_JS).catch(() => undefined);
        await clickGenerate(page);
        const completed = await waitForCompletedNote(page, startedAt, input.timeoutMinutes);
        const title = (completed.note.title || preferredTitle || '未命名笔记').trim();
        const segments = completed.segments;
        const transcriptDurationSec = segments.length > 0 ? segments[segments.length - 1].startSec : 0;
        return {
            provider: 'get',
            title,
            transcriptDurationSec,
            chapters: completed.sections,
            segments,
            rawText: completed.rawText
        };
    }
    finally {
        await page.closeWindow();
    }
}
export const getProvider = {
    async transcribe(input) {
        const parts = await splitAudioIntoParts(input.audioPath, input.tempDir);
        if (parts.length === 1) {
            return await transcribeSingleSegment(input, parts[0].path, input.preferredTitle);
        }
        const mergedParts = [];
        for (const part of parts) {
            const result = await transcribeSingleSegment(input, part.path, `${input.preferredTitle} (part ${mergedParts.length + 1})`);
            mergedParts.push({ offsetSec: part.offsetSec, result });
        }
        return mergeTranscriptParts(input.preferredTitle, mergedParts);
    }
};
