import { writeFile } from "node:fs/promises";
import path from "node:path";
import { OpenCliPage } from "../opencli/client.js";
import { buildMarkdown } from "./markdown.js";
import { assertTingwuRecordIdentity, collectTingwuTranscriptResult } from "../providers/tingwu-page.js";
import { assertOpenCliEnvironment } from "../workflow.js";
import { ensureParentDir, sanitizeFileName } from "../utils/files.js";
const DEFAULT_EXISTING_TINGWU_LOOKUP_TIMEOUT_MS = 90_000;
function normalizeMatchText(value) {
    return value
        .toLowerCase()
        .replace(/\s+/g, "")
        .replace(/[^\p{L}\p{N}]+/gu, "");
}
export function buildTingwuTitleCandidates(title) {
    const trimmed = title.trim();
    const sanitized = sanitizeFileName(trimmed);
    return [...new Set([
            trimmed,
            sanitized,
            sanitized.slice(0, 48),
            trimmed.slice(0, 48)
        ].map((item) => item.trim()).filter(Boolean))];
}
export function titleMatchesTingwuCandidate(visibleTitle, targetTitle) {
    const visible = normalizeMatchText(visibleTitle);
    if (visible.length < 8) {
        return false;
    }
    const candidates = buildTingwuTitleCandidates(targetTitle)
        .map(normalizeMatchText)
        .filter((item) => item.length >= 8);
    return candidates.some((candidate) => {
        if (visible === candidate) {
            return true;
        }
        if (candidate.length >= 10 && visible.includes(candidate)) {
            return true;
        }
        if (visible.length >= 10 && candidate.startsWith(visible)) {
            return true;
        }
        if (candidate.length >= 10 && visible.startsWith(candidate)) {
            return true;
        }
        return false;
    });
}
function splitSearchUrls(value) {
    if (Array.isArray(value)) {
        return value.map((item) => item.trim()).filter(Boolean);
    }
    return (value ?? "")
        .split(/[,\n;]/)
        .map((item) => item.trim())
        .filter(Boolean);
}
function getTingwuExistingSearchUrls(searchUrls) {
    const explicit = splitSearchUrls(searchUrls);
    const configured = process.env.TINGWU_EXISTING_SEARCH_URLS ?? process.env.TINGWU_EXISTING_SEARCH_URL ?? process.env.TINGWU_URL;
    const urls = explicit.length > 0 ? explicit : splitSearchUrls(configured);
    return [...new Set(urls.length > 0 ? urls : ["https://tingwu.aliyun.com/home"])];
}
function isTingwuFolderUrl(url) {
    try {
        const parsed = new URL(url);
        return /(^|\.)tingwu\.aliyun\.com$/i.test(parsed.hostname) && /\/folders\/[^/?#]+/i.test(parsed.pathname);
    }
    catch {
        return false;
    }
}
async function sleep(page, ms) {
    await page.evaluate(`new Promise(resolve => setTimeout(resolve, ${Math.max(0, ms)}))`).catch(() => undefined);
}
async function searchTingwuHome(page, query) {
    const searched = await page.evaluate(`(() => {
    const query = ${JSON.stringify(query)};
    const inputs = Array.from(document.querySelectorAll('input, textarea'))
      .filter((node) => node instanceof HTMLInputElement || node instanceof HTMLTextAreaElement);
    const input = inputs.find((node) => {
      const text = [
        node.getAttribute('placeholder') || '',
        node.getAttribute('aria-label') || '',
        node.getAttribute('title') || '',
        node.className || ''
      ].join(' ');
      return /搜索|search/i.test(text);
    }) || inputs[0];
    if (!input) return false;
    input.focus();
    const proto = input instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
    if (setter) {
      setter.call(input, query);
    } else {
      input.value = query;
    }
    if (typeof InputEvent === 'function') {
      input.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText', data: query }));
    } else {
      input.dispatchEvent(new Event('input', { bubbles: true }));
    }
    input.dispatchEvent(new Event('change', { bubbles: true }));
    input.dispatchEvent(new KeyboardEvent('keydown', { bubbles: true, key: 'Enter', code: 'Enter', keyCode: 13, which: 13 }));
    input.dispatchEvent(new KeyboardEvent('keypress', { bubbles: true, key: 'Enter', code: 'Enter', keyCode: 13, which: 13 }));
    input.dispatchEvent(new KeyboardEvent('keyup', { bubbles: true, key: 'Enter', code: 'Enter', keyCode: 13, which: 13 }));
    return true;
  })()`).catch(() => false);
    if (searched) {
        await sleep(page, 3000);
    }
}
async function clickMatchingTingwuRecord(page, title) {
    const candidates = buildTingwuTitleCandidates(title);
    return await page.evaluate(`(() => {
    const candidates = ${JSON.stringify(candidates)};
    const normalize = (value) => String(value || '')
      .toLowerCase()
      .replace(/\\s+/g, '')
      .replace(/[^\\p{L}\\p{N}]+/gu, '');
    const normalizedCandidates = candidates.map(normalize).filter((item) => item.length >= 8);
    const matches = (text) => {
      const normalized = normalize(text);
      if (normalized.length < 8) return false;
      return normalizedCandidates.some((candidate) => {
        if (normalized === candidate) return true;
        if (candidate.length >= 10 && normalized.includes(candidate)) return true;
        if (normalized.length >= 10 && candidate.startsWith(normalized)) return true;
        if (candidate.length >= 10 && normalized.startsWith(candidate)) return true;
        return false;
      });
    };
    const textOf = (node) => (node && (node.innerText || node.textContent) || '').replace(/\\s+/g, ' ').trim();
    const findClickable = (node) => {
      const directLink = node instanceof HTMLAnchorElement ? node : node.closest('a');
      if (directLink instanceof HTMLAnchorElement && directLink.href) {
        return directLink;
      }
      const semantic = node.closest('[role="button"], [class*="groupCards"], [class*="cardContent"], [class*="card"], [class*="item"]');
      if (semantic instanceof HTMLElement && !['HTML', 'BODY'].includes(semantic.tagName)) {
        return semantic;
      }
      let current = node;
      for (let depth = 0; depth < 6 && current instanceof HTMLElement; depth += 1) {
        const cls = String(current.className || '');
        const style = getComputedStyle(current);
        if (
          current.onclick
          || style.cursor === 'pointer'
          || /groupCards|cardContent|card|item/i.test(cls)
        ) {
          return current;
        }
        current = current.parentElement;
      }
      return node instanceof HTMLElement && !['HTML', 'BODY'].includes(node.tagName) ? node : undefined;
    };
    const nodes = Array.from(document.querySelectorAll('a, [role="button"], [class*="twoLine"], [class*="groupCards"], [class*="cardContent"], [class*="item"], [class*="card"], [class*="list"], div, span'))
      .filter((node) => node instanceof HTMLElement)
      .map((node) => ({ node, text: textOf(node), className: String(node.className || '') }))
      .filter((entry) => entry.text.length > 0 && entry.text.length < 800 && matches(entry.text))
      .sort((left, right) => {
        const leftIsTitle = /twoLine/i.test(left.className) || left.text.length < 120;
        const rightIsTitle = /twoLine/i.test(right.className) || right.text.length < 120;
        if (leftIsTitle !== rightIsTitle) return leftIsTitle ? -1 : 1;
        return left.text.length - right.text.length;
      });

    for (const entry of nodes) {
      const clickable = findClickable(entry.node);
      if (clickable instanceof HTMLElement) {
        if (clickable instanceof HTMLAnchorElement && clickable.href) {
          location.href = clickable.href;
        } else {
          clickable.click();
        }
        return true;
      }
    }
    return false;
  })()`).catch(() => false);
}
async function waitForTingwuTranscriptPage(page) {
    const deadline = Date.now() + 15_000;
    while (Date.now() < deadline) {
        const url = await page.evaluate("location.href || ''").catch(() => "");
        if (/\/(?:doc\/)?transcripts\//.test(url)) {
            await sleep(page, 1500);
            return url;
        }
        await sleep(page, 500);
    }
    return undefined;
}
async function readTingwuFolderPageSignature(page) {
    return await page.evaluate(`(() => {
    const activePage = (document.querySelector('.ant-pagination-item-active')?.textContent || '').trim();
    const cards = Array.from(document.querySelectorAll('[class*="groupCards"], [class*="cardContent"]'))
      .map((node) => (node.textContent || '').replace(/\\s+/g, ' ').trim())
      .filter(Boolean)
      .slice(0, 20);
    return JSON.stringify({ activePage, cards });
  })()`).catch(() => "");
}
async function clickNextTingwuFolderPage(page) {
    const before = await readTingwuFolderPageSignature(page);
    const clicked = await page.evaluate(`(() => {
    const next = document.querySelector('.ant-pagination-next');
    if (!(next instanceof HTMLElement)) return false;
    const className = String(next.className || '');
    if (className.includes('ant-pagination-disabled') || next.getAttribute('aria-disabled') === 'true') {
      return false;
    }
    const button = next.querySelector('button');
    if (button instanceof HTMLButtonElement && button.disabled) {
      return false;
    }
    if (button instanceof HTMLElement) {
      button.click();
      return true;
    }
    next.click();
    return true;
  })()`).catch(() => false);
    if (!clicked) {
        return false;
    }
    const deadline = Date.now() + 15_000;
    while (Date.now() < deadline) {
        await sleep(page, 500);
        const after = await readTingwuFolderPageSignature(page);
        if (after && after !== before) {
            await sleep(page, 800);
            return true;
        }
    }
    return true;
}
async function scanTingwuFolderPagesForTitle(page, title) {
    const visited = new Set();
    for (let pageIndex = 0; pageIndex < 100; pageIndex += 1) {
        const signature = await readTingwuFolderPageSignature(page);
        if (signature) {
            if (visited.has(signature)) {
                return undefined;
            }
            visited.add(signature);
        }
        const clicked = await clickMatchingTingwuRecord(page, title);
        if (clicked) {
            const url = await waitForTingwuTranscriptPage(page);
            if (url) {
                return url;
            }
        }
        const moved = await clickNextTingwuFolderPage(page);
        if (!moved) {
            return undefined;
        }
    }
    return undefined;
}
async function findExistingTingwuTranscriptByTitle(title, preferredTitle, searchUrls) {
    const page = new OpenCliPage(`tingwu-existing:${Date.now()}`);
    try {
        const candidates = buildTingwuTitleCandidates(title);
        for (const searchUrl of getTingwuExistingSearchUrls(searchUrls)) {
            await page.goto(searchUrl);
            await sleep(page, 1500);
            if (isTingwuFolderUrl(searchUrl)) {
                const url = await scanTingwuFolderPagesForTitle(page, title);
                if (!url) {
                    continue;
                }
                await assertTingwuRecordIdentity(page, title);
                const transcript = await collectTingwuTranscriptResult(page, preferredTitle || title);
                return { url, transcript };
            }
            for (const candidate of candidates) {
                await searchTingwuHome(page, candidate);
                const clicked = await clickMatchingTingwuRecord(page, title);
                if (!clicked) {
                    continue;
                }
                const url = await waitForTingwuTranscriptPage(page);
                if (!url) {
                    await page.goto(searchUrl);
                    await sleep(page, 1000);
                    continue;
                }
                await assertTingwuRecordIdentity(page, title);
                const transcript = await collectTingwuTranscriptResult(page, preferredTitle || title);
                return { url, transcript };
            }
        }
        return undefined;
    }
    finally {
        await page.closeWindow();
    }
}
function createDefaultDependencies() {
    return {
        assertEnvironment: assertOpenCliEnvironment,
        findExistingTranscript: findExistingTingwuTranscriptByTitle,
        writeMarkdownFile: async (filePath, content) => {
            await ensureParentDir(filePath);
            await writeFile(filePath, content, "utf8");
        },
        now: () => new Date()
    };
}
async function withTimeout(promise, timeoutMs, message) {
    let timer;
    try {
        return await Promise.race([
            promise,
            new Promise((_, reject) => {
                timer = setTimeout(() => reject(new Error(message)), timeoutMs);
            })
        ]);
    }
    finally {
        if (timer) {
            clearTimeout(timer);
        }
    }
}
export async function runTingwuExistingTranscriptToMarkdown(options, overrides) {
    const deps = {
        ...createDefaultDependencies(),
        ...overrides
    };
    const title = options.title.trim();
    const outDir = options.outDir.trim();
    if (!title || !outDir) {
        return undefined;
    }
    await deps.assertEnvironment();
    const match = await withTimeout(deps.findExistingTranscript(title, options.preferredTitle, options.searchUrls), DEFAULT_EXISTING_TINGWU_LOOKUP_TIMEOUT_MS, `Existing Tingwu lookup timed out after ${DEFAULT_EXISTING_TINGWU_LOOKUP_TIMEOUT_MS / 1000}s`);
    if (!match) {
        return undefined;
    }
    const transcript = match.transcript;
    const markdownTitle = options.preferredTitle || transcript.title || title || "tingwu-transcript";
    const outputPath = path.join(outDir, `${sanitizeFileName(markdownTitle)}.md`);
    const mediaDurationSec = transcript.transcriptDurationSec || transcript.segments[transcript.segments.length - 1]?.startSec || 0;
    const provider = "tingwu";
    const generatedAt = deps.now().toISOString();
    const markdown = buildMarkdown({
        title: markdownTitle,
        sourceUrl: options.sourceUrl || match.url,
        resourceId: options.resourceId,
        provider,
        mediaDurationSec,
        coverage: 1,
        generatedAt,
        chapters: transcript.chapters,
        segments: transcript.segments,
        rawText: transcript.rawText
    });
    await deps.writeMarkdownFile(outputPath, markdown);
    return {
        outputPath,
        provider,
        coverage: 1,
        coverageStatus: "complete",
        warnings: [`Reused existing Tingwu transcript by title: ${match.url}`],
        providerAttempts: [{
                provider,
                status: "success",
                coverage: 1,
                startedAt: generatedAt,
                finishedAt: deps.now().toISOString()
            }],
        tempDir: ""
    };
}
