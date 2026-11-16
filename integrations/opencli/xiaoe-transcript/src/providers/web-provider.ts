import type {
  ProviderInput,
  TranscriptChapter,
  TranscriptProvider,
  TranscriptProviderName,
  TranscriptResult,
  TranscriptSegment
} from "../types.js";
import path, { basename } from "node:path";
import { copyFile } from "node:fs/promises";
import { OpenCliPage } from "../opencli/client.js";
import { runCommand } from "../utils/command.js";
import { parseTimestamp } from "../utils/time.js";

interface WebProviderConfig {
  name: TranscriptProviderName;
  homeUrl: string;
  uploadLabels: string[];
  transcriptLabels: string[];
  copyLabels: string[];
  completionHints: string[];
}

interface ExtractedPayload {
  title: string;
  rawText: string;
  segments: TranscriptSegment[];
  chapters: TranscriptChapter[];
}

function quoteArray(values: string[]): string {
  return JSON.stringify(values);
}

function clickByTextScript(labels: string[]): string {
  return `
    (() => {
      const labels = ${quoteArray(labels)};
      const candidates = Array.from(document.querySelectorAll('button, [role="button"], a, label, span, div'))
        .map((element) => {
          const text = (element.innerText || element.textContent || '').trim();
          const rect = element.getBoundingClientRect();
          return { element, text, area: rect.width * rect.height, width: rect.width, height: rect.height };
        })
        .filter(({ text, width, height, area, element }) => {
          if (!text) return false;
          if (!labels.some(label => text.includes(label))) return false;
          if (width === 0 || height === 0) return false;
          if (["HTML", "BODY", "MAIN", "SECTION"].includes(element.tagName)) return false;
          return area < 200000;
        })
        .sort((left, right) => left.text.length - right.text.length || left.area - right.area);

      for (const { element, text } of candidates) {
        const clickable = element.closest('.item, [class*="upload"], [role="button"], button, a, label') || element;
        clickable.click();
        return text;
      }
      return null;
    })()
  `;
}

function transcriptExtractionScript(): string {
  return `
    (() => {
      const timestampPattern = /(?:\\[)?(\\d{1,2}:\\d{2}(?::\\d{2})?)(?:\\])?\\s*(.+)?/;
      const candidates = [];
      const seen = new Set();
      const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_ELEMENT);
      while (walker.nextNode()) {
        const element = walker.currentNode;
        if (!(element instanceof HTMLElement)) continue;
        const text = (element.innerText || '').trim();
        if (!text || text.length < 4) continue;
        if (seen.has(text)) continue;
        seen.add(text);
        candidates.push(text);
      }

      const segments = [];
      for (const line of candidates.flatMap(value => value.split(/\\n+/)).map(value => value.trim()).filter(Boolean)) {
        const match = line.match(timestampPattern);
        if (!match) continue;
        segments.push({
          timestamp: match[1],
          startSec: 0,
          text: (match[2] || '').trim()
        });
      }

      const dedupedSegments = [];
      const seenSegments = new Set();
      for (const segment of segments) {
        const key = segment.timestamp + '|' + segment.text;
        if (seenSegments.has(key)) continue;
        seenSegments.add(key);
        dedupedSegments.push(segment);
      }

      const chapterLines = candidates
        .flatMap(value => value.split(/\\n+/))
        .map(value => value.trim())
        .filter(value => /章节|chapter|part|第.+讲/i.test(value));

      const chapters = [];
      for (const line of chapterLines) {
        const match = line.match(/(\\d{1,2}:\\d{2}(?::\\d{2})?)?\\s*(.+)/);
        if (!match) continue;
        chapters.push({
          timestamp: match[1] || '00:00',
          startSec: 0,
          title: match[2].trim()
        });
      }

      return {
        title: document.title || '',
        rawText: (document.body.innerText || '').trim(),
        segments: dedupedSegments,
        chapters
      };
    })()
  `;
}

async function readClipboardText(): Promise<string> {
  const result = await runCommand(
    "powershell",
    ["-NoProfile", "-Command", "Get-Clipboard -Raw"],
    { allowFailure: true, timeoutMs: 10000 }
  );
  return result.stdout.trim();
}

function parseClipboardTranscript(text: string): ExtractedPayload {
  const lines = text.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
  const segments: TranscriptSegment[] = [];
  for (const line of lines) {
    const match = line.match(/(?:\[)?(\d{1,2}:\d{2}(?::\d{2})?)(?:\])?\s*(.+)?/);
    if (!match) {
      continue;
    }
    segments.push({
      timestamp: match[1],
      startSec: parseTimestamp(match[1]),
      text: (match[2] || "").trim()
    });
  }
  return {
    title: "",
    rawText: text,
    chapters: [],
    segments
  };
}

function normalizePayload(provider: TranscriptProviderName, preferredTitle: string, payload: ExtractedPayload): TranscriptResult {
  const segments = payload.segments.map((segment) => ({
    ...segment,
    startSec: segment.startSec || parseTimestamp(segment.timestamp)
  }));
  const chapters = payload.chapters.map((chapter) => ({
    ...chapter,
    startSec: chapter.startSec || parseTimestamp(chapter.timestamp)
  }));
  const transcriptDurationSec = segments.length > 0
    ? segments[segments.length - 1].startSec
    : 0;

  return {
    provider,
    title: payload.title || preferredTitle,
    transcriptDurationSec,
    chapters,
    segments,
    rawText: payload.rawText
  };
}

async function waitForFileInput(page: OpenCliPage, labels: string[], timeoutMs: number): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const surfaceReady = await page.evaluate<boolean>(`
      (() => {
        const hasFileInput = Array.from(document.querySelectorAll('input[type="file"]')).some((input) => input instanceof HTMLInputElement);
        const text = document.body.innerText || '';
        return hasFileInput || /点此导入|拖拽、粘贴音视频到这里|上传音视频|导入文件/.test(text);
      })()
    `);
    if (surfaceReady) {
      return;
    }
    await page.evaluate(clickByTextScript(labels)).catch(() => undefined);
    await page.evaluate("new Promise(resolve => setTimeout(resolve, 1000))");
  }
  throw new Error("unable to find a file input for provider upload");
}

async function uploadFileToPage(page: OpenCliPage, filePath: string): Promise<void> {
  const fileName = basename(filePath);
  const uploadPath = path.join(path.dirname(filePath), fileName);
  if (uploadPath !== filePath) {
    await copyFile(filePath, uploadPath);
  }
  await page.setFileInput([uploadPath], 'input[type="file"]');
}

async function waitForTranscript(page: OpenCliPage, config: WebProviderConfig, timeoutMinutes: number): Promise<ExtractedPayload> {
  const deadline = Date.now() + timeoutMinutes * 60000;
  let bestPayload: ExtractedPayload | undefined;
  let stableRounds = 0;
  let lastSegmentCount = 0;

  while (Date.now() < deadline) {
    await page.evaluate(clickByTextScript(config.transcriptLabels)).catch(() => undefined);
    const payload = await page.evaluate<ExtractedPayload>(transcriptExtractionScript());
    if (payload.segments.length > 0) {
      bestPayload = payload;
      if (payload.segments.length === lastSegmentCount) {
        stableRounds += 1;
      } else {
        stableRounds = 0;
      }
      lastSegmentCount = payload.segments.length;
    }

    const doneText = await page.evaluate<string>("document.body.innerText || ''");
    if (bestPayload && (stableRounds >= 2 || config.completionHints.some((hint) => doneText.includes(hint)))) {
      return bestPayload;
    }

    await page.evaluate("new Promise(resolve => setTimeout(resolve, 5000))");
  }

  if (bestPayload) {
    return bestPayload;
  }

  await page.evaluate(clickByTextScript(config.copyLabels)).catch(() => undefined);
  await page.evaluate("new Promise(resolve => setTimeout(resolve, 1000))");
  const clipboardText = await readClipboardText();
  if (!clipboardText) {
    throw new Error("provider page did not expose any transcript content before timeout");
  }
  return parseClipboardTranscript(clipboardText);
}

export function createWebTranscriptProvider(config: WebProviderConfig): TranscriptProvider {
  return {
    async transcribe(input: ProviderInput): Promise<TranscriptResult> {
      const page = new OpenCliPage(`xiaoe-to-md:provider:${config.name}`);

      try {
        await page.goto(process.env[config.name === "tingwu" ? "TINGWU_URL" : "GET_BIJI_URL"] || config.homeUrl);
        await waitForFileInput(page, config.uploadLabels, 20000);
        await page.evaluate(clickByTextScript(config.uploadLabels)).catch(() => undefined);
        await page.evaluate("new Promise(resolve => setTimeout(resolve, 1000))");
        await uploadFileToPage(page, input.audioPath);
        await page.evaluate("new Promise(resolve => setTimeout(resolve, 1500))");
        const payload = await waitForTranscript(page, config, input.timeoutMinutes);
        return normalizePayload(config.name, input.preferredTitle, payload);
      } finally {
        await page.closeWindow();
      }
    }
  };
}
