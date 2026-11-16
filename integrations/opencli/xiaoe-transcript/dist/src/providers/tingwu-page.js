import { parseTimestamp } from "../utils/time.js";
function normalizeRecordTitle(value) {
    return value
        .normalize("NFKC")
        .replace(/\.(?:mp3|m4a|wav|mp4|aac)$/i, "")
        .toLowerCase()
        .replace(/[^\p{L}\p{N}]+/gu, "");
}
export function matchesTingwuRecordTitle(actualTitle, expectedTitle) {
    const actual = normalizeRecordTitle(actualTitle);
    const expected = normalizeRecordTitle(expectedTitle);
    return expected.length >= 8 && actual === expected;
}
export async function assertTingwuRecordIdentity(page, expectedTitle, timeoutMs = 5000) {
    const deadline = Date.now() + timeoutMs;
    let observed = "";
    while (Date.now() < deadline) {
        const identity = await page.evaluate(`(() => ({
      url: location.href,
      title: (document.title || '').replace(/\\s+-\\s+通义听悟\\s*$/, '').trim()
    }))()`).catch(() => ({ url: "", title: "" }));
        if (/\/doc\/transcripts\/[^/?#]+/.test(identity.url)) {
            observed = identity.title;
            if (matchesTingwuRecordTitle(observed, expectedTitle))
                return;
        }
        await new Promise((resolve) => setTimeout(resolve, 250));
    }
    throw new Error(`Tingwu record identity mismatch: expected ${expectedTitle}, opened ${observed || "<unknown>"}`);
}
const TIMESTAMP_ONLY_RE = /^\[?(\d{1,2}:\d{2}(?::\d{2})?)\]?$/;
const TIMESTAMP_WITH_TEXT_RE = /^\[?(\d{1,2}:\d{2}(?::\d{2})?)\]?\s+(.+)$/;
const SPEAKER_TIMESTAMP_RE = /^(.+?)\s+(\d{1,2}:\d{2}(?::\d{2})?)$/;
const GENERIC_SPEAKER_RE = /^发言人(?:\s+\d+)?$/;
const SPEAKER_BLOCK_START_RE = /^发言人\s*\n\s*\d{1,2}:\d{2}(?::\d{2})?/;
const TIMESTAMP_BLOCK_START_RE = /^\d{1,2}:\d{2}(?::\d{2})?\s*\n/;
const TINGWU_PROCESSING_MARKERS = [
    "全文概要处理中",
    "请稍后查看",
    "转写处理中",
    "摘要处理中",
    "智能总结生成中"
];
const TINGWU_EDITOR_NOISE_LINES = new Set([
    "正文",
    "灵光乍现？快记录下来吧",
    "万语千言，心领神悟"
]);
async function waitForBodyText(page, text, timeoutMs) {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
        const present = await page.evaluate(`(() => (document.body.innerText || '').includes(${JSON.stringify(text)}))()`).catch(() => false);
        if (present) {
            return;
        }
        await page.evaluate('new Promise(resolve => setTimeout(resolve, 500))').catch(() => undefined);
    }
    throw new Error(`Tingwu page did not show expected text: ${text}`);
}
export function parseTingwuChapterSection(lines) {
    const chapters = [];
    const start = lines.indexOf("章节速览");
    if (start === -1)
        return chapters;
    const endCandidates = ["查看章节摘要", "智能内容由 AI 模型生成，我们不对内容准确性和完整性做任何保证，亦不代表我们的观点或态度", "原文"];
    let end = lines.length;
    for (const candidate of endCandidates) {
        const index = lines.indexOf(candidate, start + 1);
        if (index !== -1) {
            end = Math.min(end, index);
        }
    }
    for (let index = start + 1; index < end - 1; index += 1) {
        const timestamp = lines[index];
        if (!/^\d{2}:\d{2}(?::\d{2})?$/.test(timestamp))
            continue;
        const title = lines[index + 1]?.trim();
        if (!title)
            continue;
        chapters.push({
            title,
            timestamp,
            startSec: parseTimestamp(timestamp)
        });
    }
    return chapters;
}
function normalizeTingwuExtractedText(text) {
    return text
        .replace(/\r\n/g, "\n")
        .replace(/[\u200B-\u200D\uFEFF]/g, "")
        .trim();
}
function normalizeTingwuPlainText(text) {
    return normalizeTingwuExtractedText(text)
        .split(/\n+/)
        .map((line) => line.trim())
        .filter((line) => line.length > 0 && !TINGWU_EDITOR_NOISE_LINES.has(line))
        .join("\n");
}
function joinTingwuApiWords(words) {
    return words
        .map((word) => word.tc || "")
        .join("")
        .replace(/\s+/g, " ")
        .trim();
}
export function parseTingwuApiTranscriptResult(payload, preferredTitle) {
    if (!payload.result) {
        return null;
    }
    let parsed;
    try {
        parsed = JSON.parse(payload.result);
    }
    catch {
        return null;
    }
    const paragraphs = Array.isArray(parsed.pg) ? parsed.pg : [];
    const segments = [];
    let lastEndSec = 0;
    for (const paragraph of paragraphs) {
        const words = Array.isArray(paragraph.sc) ? paragraph.sc : [];
        const text = joinTingwuApiWords(words);
        if (!text) {
            continue;
        }
        const startMs = words.find((word) => typeof word.bt === "number")?.bt;
        const endMs = [...words].reverse().find((word) => typeof word.et === "number")?.et;
        if (typeof startMs !== "number") {
            continue;
        }
        if (typeof endMs === "number") {
            lastEndSec = Math.max(lastEndSec, Math.ceil(endMs / 1000));
        }
        segments.push({
            timestamp: formatApiTimestamp(startMs / 1000),
            startSec: Math.floor(startMs / 1000),
            text,
            speaker: paragraph.ui ? "发言人" : undefined
        });
    }
    if (segments.length === 0) {
        return null;
    }
    const durationSec = typeof payload.duration === "number" && payload.duration > 0
        ? payload.duration
        : lastEndSec;
    const rawText = segments
        .map((segment) => `[${segment.timestamp}] ${segment.speaker ? `${segment.speaker}: ` : ""}${segment.text}`)
        .join("\n");
    return {
        provider: "tingwu",
        title: preferredTitle || payload.tag?.showName || "tingwu-transcript",
        transcriptDurationSec: durationSec,
        chapters: [],
        segments,
        rawText
    };
}
function formatApiTimestamp(totalSeconds) {
    const seconds = Math.max(0, Math.floor(totalSeconds));
    const hh = Math.floor(seconds / 3600);
    const mm = Math.floor((seconds % 3600) / 60);
    const ss = seconds % 60;
    if (hh > 0) {
        return `${hh.toString().padStart(2, "0")}:${mm.toString().padStart(2, "0")}:${ss.toString().padStart(2, "0")}`;
    }
    return `${mm.toString().padStart(2, "0")}:${ss.toString().padStart(2, "0")}`;
}
function lineStartsNewSegment(line) {
    return TIMESTAMP_ONLY_RE.test(line)
        || TIMESTAMP_WITH_TEXT_RE.test(line)
        || (SPEAKER_TIMESTAMP_RE.test(line) && /[^\d:\[\]\s]/.test(line.replace(/\d{1,2}:\d{2}(?::\d{2})?/, "")));
}
function countTimestampAnchors(text) {
    const lines = normalizeTingwuExtractedText(text).split(/\n+/).map((line) => line.trim()).filter(Boolean);
    return lines.filter((line) => lineStartsNewSegment(line)).length;
}
function hasTranscriptPayload(text) {
    const lines = normalizeTingwuExtractedText(text).split(/\n+/).map((line) => line.trim()).filter(Boolean);
    if (SPEAKER_BLOCK_START_RE.test(text)) {
        return lines.length >= 3;
    }
    if (TIMESTAMP_BLOCK_START_RE.test(text)) {
        return lines.length >= 2;
    }
    return countTimestampAnchors(text) >= 1;
}
export function looksLikeTingwuExtractedText(text) {
    const normalized = normalizeTingwuExtractedText(text);
    if (!normalized || normalized.length < 10) {
        return false;
    }
    return countTimestampAnchors(normalized) >= 1;
}
export function looksLikeTingwuTranscriptStillProcessing(text) {
    const normalized = normalizeTingwuExtractedText(text);
    if (!normalized) {
        return false;
    }
    return TINGWU_PROCESSING_MARKERS.some((marker) => normalized.includes(marker));
}
export function selectLikelyTingwuTranscriptBlocks(textBlocks) {
    const normalizedBlocks = textBlocks
        .map((text) => normalizeTingwuExtractedText(text))
        .filter(Boolean);
    const likelyBlocks = normalizedBlocks.filter((text) => {
        if (text.includes("关键词") || text.includes("全文概要") || text.includes("章节速览") || text.startsWith("反馈：")) {
            return false;
        }
        if (!(SPEAKER_BLOCK_START_RE.test(text) || TIMESTAMP_BLOCK_START_RE.test(text))) {
            return false;
        }
        return hasTranscriptPayload(text);
    });
    const uniqueBlocks = [...new Set(likelyBlocks)];
    const compactBlocks = uniqueBlocks.filter((candidate, index, all) => {
        return !all.some((other, otherIndex) => {
            if (otherIndex === index)
                return false;
            if (other.length <= candidate.length)
                return false;
            return other.includes(candidate);
        });
    });
    return compactBlocks.join("\n");
}
export function parseTingwuExtractedSegments(text) {
    const lines = normalizeTingwuExtractedText(text).split(/\n+/).map((line) => line.trim()).filter(Boolean);
    const segments = [];
    let currentSpeaker;
    let currentTimestamp = null;
    let currentText = [];
    const flush = () => {
        if (!currentTimestamp)
            return;
        const content = currentText.join(" ").trim();
        if (!content)
            return;
        segments.push({
            timestamp: currentTimestamp,
            startSec: parseTimestamp(currentTimestamp),
            text: content,
            ...(currentSpeaker ? { speaker: currentSpeaker } : {})
        });
    };
    for (let index = 0; index < lines.length; index += 1) {
        const line = lines[index];
        const nextLine = lines[index + 1];
        if (line
            && nextLine
            && !TIMESTAMP_ONLY_RE.test(line)
            && !TIMESTAMP_WITH_TEXT_RE.test(line)
            && !SPEAKER_TIMESTAMP_RE.test(line)
            && TIMESTAMP_ONLY_RE.test(nextLine)
            && line.length <= 20) {
            flush();
            currentSpeaker = line;
            currentTimestamp = nextLine.match(TIMESTAMP_ONLY_RE)?.[1]?.trim() ?? null;
            currentText = [];
            index += 1;
            continue;
        }
        const timestampOnly = line.match(TIMESTAMP_ONLY_RE);
        if (timestampOnly) {
            flush();
            currentSpeaker = undefined;
            currentTimestamp = timestampOnly[1].trim();
            currentText = [];
            continue;
        }
        const timestampWithText = line.match(TIMESTAMP_WITH_TEXT_RE);
        if (timestampWithText) {
            flush();
            currentSpeaker = undefined;
            currentTimestamp = timestampWithText[1].trim();
            currentText = [timestampWithText[2].trim()];
            continue;
        }
        const speakerTimestamp = line.match(SPEAKER_TIMESTAMP_RE);
        if (speakerTimestamp) {
            flush();
            currentSpeaker = speakerTimestamp[1].trim();
            currentTimestamp = speakerTimestamp[2].trim();
            currentText = [];
            continue;
        }
        if (line === "正文" || line === "万语千言，心领神悟") {
            continue;
        }
        if (currentTimestamp) {
            currentText.push(line);
        }
    }
    flush();
    const deduped = [];
    const seen = new Set();
    for (const segment of segments) {
        const key = `${segment.speaker ?? ""}|${segment.timestamp}|${segment.text}`;
        if (seen.has(key)) {
            continue;
        }
        seen.add(key);
        deduped.push(segment);
    }
    return deduped;
}
async function readExtractedText(page) {
    const editorCandidates = await page.evaluate(`(() => {
    return Array.from(document.querySelectorAll('.tingwu-editor-content, .editorViewport.editorViewport-bg, .editorViewport, [contenteditable="true"]'))
      .filter((node) => node instanceof HTMLElement)
      .map((node) => node.innerText || node.textContent || '')
      .filter((text) => /\\d{2}:\\d{2}(?::\\d{2})?/.test(text));
  })()`).catch(() => []);
    const editorBest = [...editorCandidates]
        .map((text) => normalizeTingwuExtractedText(text))
        .filter((text) => looksLikeTingwuExtractedText(text))
        .sort((left, right) => countTimestampAnchors(right) - countTimestampAnchors(left) || right.length - left.length)[0] || "";
    const blockBest = await page.evaluate(`(() => {
      const text = (node) => ((node.innerText || node.textContent || '').replace(/\\s+$/g, '').trim());
      const blocks = Array.from(document.querySelectorAll('div, p, li, span'))
        .map((node) => text(node))
        .filter(Boolean);
      return JSON.stringify(blocks);
    })()`).then((raw) => {
        const blocks = JSON.parse(raw);
        return selectLikelyTingwuTranscriptBlocks(blocks);
    }).catch(() => "");
    return [editorBest, blockBest]
        .filter((text) => looksLikeTingwuExtractedText(text))
        .sort((left, right) => countTimestampAnchors(right) - countTimestampAnchors(left) || right.length - left.length)[0] || "";
}
async function readTingwuApiTranscript(page, preferredTitle) {
    const apiText = await page.evaluate(`async () => {
    const match = location.pathname.match(/\\/(?:doc\\/)?transcripts\\/([^/?#]+)/);
    const transId = match && match[1] ? decodeURIComponent(match[1]) : "";
    if (!transId) return "";
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 10000);
    try {
      const response = await fetch("/api/trans/getTransResult?c=web", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ transId }),
        credentials: "include",
        signal: controller.signal
      });
      return await response.text();
    } finally {
      clearTimeout(timer);
    }
  }`).catch(() => "");
    if (!apiText) {
        return null;
    }
    let apiResponse;
    try {
        apiResponse = JSON.parse(apiText);
    }
    catch {
        return null;
    }
    if (apiResponse.code !== "0" || !apiResponse.data) {
        return null;
    }
    const apiTitle = apiResponse.data.tag?.showName;
    if (apiTitle) {
        const pageTitle = await page.evaluate(`(document.title || '').replace(/\\s+-\\s+通义听悟\\s*$/, '').trim()`)
            .catch(() => "");
        if (pageTitle && !matchesTingwuRecordTitle(apiTitle, pageTitle)) {
            throw new Error(`Tingwu API transcript title mismatch: page ${pageTitle}, response ${apiTitle}`);
        }
    }
    return parseTingwuApiTranscriptResult(apiResponse.data, preferredTitle);
}
async function readFullTranscriptPlainText(page) {
    const fullText = await page.evaluate(`(() => {
    const node = document.querySelector('.tingwu-editor-content-container, .tingwu-editor-content');
    if (!(node instanceof HTMLElement)) return '';
    return node.innerText || node.textContent || '';
  })()`).catch(() => "");
    return normalizeTingwuPlainText(fullText);
}
async function collectTranscriptDiagnostics(page) {
    const payload = await page.evaluate(`(() => {
    const summarize = (text) => (text || '').replace(/\\s+/g, ' ').trim().slice(0, 220);
    const bodySnippet = summarize(document.body.innerText || '');
    const candidates = Array.from(document.querySelectorAll('*'))
      .filter((node) => node instanceof HTMLElement)
      .map((node) => {
        const text = node.innerText || node.textContent || '';
        if (!/\\d{1,2}:\\d{2}(?::\\d{2})?/.test(text)) return '';
        const tag = node.tagName.toLowerCase();
        const className = (node.className || '').toString().replace(/\\s+/g, '.').slice(0, 80);
        return tag + (className ? '.' + className : '') + ': ' + summarize(text);
      })
      .filter(Boolean)
      .slice(0, 5);
    return {
      url: location.href,
      title: document.title || '',
      bodySnippet,
      candidates
    };
  })()`).catch(() => ({
        url: "",
        title: "",
        bodySnippet: "",
        candidates: []
    }));
    const parts = [
        payload.url ? `url=${payload.url}` : "",
        payload.title ? `title=${payload.title}` : "",
        payload.bodySnippet ? `body=${payload.bodySnippet}` : "",
        payload.candidates.length > 0 ? `candidates=${payload.candidates.join(" | ")}` : "candidates=<none>"
    ].filter(Boolean);
    return parts.join("; ");
}
async function extractOriginalToEditor(page) {
    const oneClickExtracted = await page.evaluate(`(() => {
    const target = Array.from(document.querySelectorAll('div, button, span')).find((node) => {
      const text = (node.innerText || node.textContent || '').replace(/\\s+/g, ' ').trim();
      return text === '一键摘取';
    });
    if (!(target instanceof HTMLElement)) return false;
    target.click();
    return true;
  })()`).catch(() => false);
    if (oneClickExtracted) {
        await page.evaluate("new Promise(resolve => setTimeout(resolve, 3000))").catch(() => undefined);
        return;
    }
    await page.evaluate(`(() => {
    const target = document.querySelector('[data-e2e-test-id="trans_menu_extract_div"]');
    if (target instanceof HTMLElement) target.click();
    return true;
  })()`);
    await page.evaluate("new Promise(resolve => setTimeout(resolve, 500))").catch(() => undefined);
    await page.evaluate(`(() => {
    const target = Array.from(document.querySelectorAll('.item')).find((node) => (node.innerText || node.textContent || '').trim() === '摘取原文');
    if (target instanceof HTMLElement) target.click();
    return true;
  })()`);
    await waitForBodyText(page, "确定要将", 10_000);
    await page.evaluate(`(() => {
    const ensureChecked = (keyword) => {
      const target = Array.from(document.querySelectorAll('.copyCheckbox')).find((node) => (node.innerText || node.textContent || '').includes(keyword));
      if (!(target instanceof HTMLElement)) return;
      const classes = Array.from(target.classList.values()).join(' ');
      const checked = /checked|active|selected/.test(classes);
      if (!checked) target.click();
    };
    ensureChecked('发言人信息');
    ensureChecked('时间戳信息');
    const confirm = document.querySelector('.copyModel-button.copyModel-confirm');
    if (!(confirm instanceof HTMLElement)) throw new Error('Tingwu 原文摘取确认按钮未找到');
    confirm.click();
    return true;
  })()`);
    await page.evaluate("new Promise(resolve => setTimeout(resolve, 3000))").catch(() => undefined);
}
async function ensureExtractedText(page) {
    const deadline = Date.now() + 120_000;
    let extractionTriggered = false;
    while (Date.now() < deadline) {
        const extracted = await readExtractedText(page);
        if (extracted) {
            return extracted;
        }
        const bodyText = await page.evaluate("document.body.innerText || ''").catch(() => "");
        if (looksLikeTingwuTranscriptStillProcessing(bodyText)) {
            await page.evaluate("new Promise(resolve => setTimeout(resolve, 3000))").catch(() => undefined);
            continue;
        }
        if (!extractionTriggered) {
            await extractOriginalToEditor(page);
            extractionTriggered = true;
            continue;
        }
        await page.evaluate("new Promise(resolve => setTimeout(resolve, 1000))").catch(() => undefined);
    }
    const diagnostics = await collectTranscriptDiagnostics(page);
    throw new Error(`Tingwu page did not expose extracted transcript content (${diagnostics})`);
}
export async function collectTingwuTranscriptResult(page, preferredTitle) {
    const apiResult = await readTingwuApiTranscript(page, preferredTitle);
    if (apiResult) {
        return apiResult;
    }
    const chapterText = await page.evaluate("document.body.innerText || ''").catch(() => "");
    const chapters = parseTingwuChapterSection(chapterText.split(/\n+/).map((line) => line.trim()).filter(Boolean));
    const extractedText = await ensureExtractedText(page);
    const fullTranscriptText = await readFullTranscriptPlainText(page);
    const segments = parseTingwuExtractedSegments(extractedText);
    if (segments.length === 0) {
        throw new Error("Tingwu page did not expose timestamped transcript segments");
    }
    const pageTitle = await page.evaluate("document.title || ''").catch(() => "");
    const transcriptDurationSec = Math.max(segments[segments.length - 1]?.startSec ?? 0, chapters[chapters.length - 1]?.startSec ?? 0);
    const rawText = [fullTranscriptText, extractedText]
        .sort((left, right) => normalizeTingwuPlainText(right).length - normalizeTingwuPlainText(left).length)[0] || extractedText;
    return {
        provider: "tingwu",
        title: preferredTitle || pageTitle || "tingwu-transcript",
        transcriptDurationSec,
        chapters,
        segments,
        rawText
    };
}
