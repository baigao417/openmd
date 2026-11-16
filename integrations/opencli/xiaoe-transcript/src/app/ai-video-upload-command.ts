import { existsSync } from "node:fs";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import path from "node:path";

type ProviderName = "gemini" | "qwen";

type BrowserPage = {
  goto(url: string, options?: unknown): Promise<void>;
  evaluate<T = unknown>(js: string): Promise<T>;
  evaluateWithArgs?<T = unknown>(js: string, args: Record<string, unknown>): Promise<T>;
  wait?(options: number | { time?: number; timeout?: number; text?: string; selector?: string }): Promise<void>;
  setFileInput?(files: string[], selector?: string): Promise<void>;
  uploadFiles?(ref: string, files: string[], opts?: unknown): Promise<unknown>;
  pressKey?(key: string): Promise<void>;
  getCurrentUrl?(): Promise<string | null>;
  cdp?<T = unknown>(method: string, params?: Record<string, unknown>): Promise<T>;
};

interface ProviderConfig {
  provider: ProviderName;
  homeUrl: string;
  modeLabels: string[];
  uploadLabels: string[];
  sendLabels: string[];
  loginHints: string[];
}

const PROVIDERS: Record<ProviderName, ProviderConfig> = {
  gemini: {
    provider: "gemini",
    homeUrl: "https://gemini.google.com/app",
    modeLabels: ["Pro", "Gemini 2.5 Pro", "2.5 Pro"],
    uploadLabels: ["上传", "Upload", "Attach", "添加文件", "Add files", "plus", "附件"],
    sendLabels: ["Send message", "发送消息", "Send", "发送"],
    loginHints: ["Sign in", "登录", "使用你的 Google 账号"]
  },
  qwen: {
    provider: "qwen",
    homeUrl: "https://chat.qwen.ai/",
    modeLabels: ["思考", "Think", "Thinking", "DeepThink", "深度思考"],
    uploadLabels: ["上传", "Upload", "Attach", "Attachment", "附件", "添加文件", "Add files", "文件", "图片", "视频"],
    sendLabels: ["发送", "Send", "提交"],
    loginHints: ["登录", "Sign in", "Log in", "扫码登录", "用千问APP扫码登录"]
  }
};

function asString(value: unknown, fallback = ""): string {
  return value === undefined || value === null ? fallback : String(value);
}

function asBool(value: unknown, fallback = false): boolean {
  if (value === undefined || value === null || value === "") return fallback;
  if (typeof value === "boolean") return value;
  const text = String(value).trim().toLowerCase();
  return text === "1" || text === "true" || text === "yes" || text === "y";
}

function asNumber(value: unknown, fallback: number): number {
  const n = Number(value);
  return Number.isFinite(n) && n > 0 ? n : fallback;
}

function providerFrom(value: unknown): ProviderName {
  const provider = asString(value).trim().toLowerCase();
  if (provider === "gemini" || provider === "qwen") return provider;
  throw new Error(`provider must be gemini or qwen, got: ${provider || "<empty>"}`);
}

function splitFiles(raw: unknown): string[] {
  const text = asString(raw).trim();
  if (!text) throw new Error("files is required");
  const files = text
    .split(/\s*[|;]\s*|\r?\n/)
    .map((item) => item.trim().replace(/^"(.*)"$/, "$1"))
    .filter(Boolean)
    .map((file) => path.resolve(file));
  if (files.length === 0) throw new Error("files resolved to empty list");
  const missing = files.filter((file) => !existsSync(file));
  if (missing.length > 0) {
    throw new Error(`missing file(s): ${missing.join("; ")}`);
  }
  return files;
}

async function readPrompt(kwargs: Record<string, unknown>): Promise<string> {
  const inline = asString(kwargs.prompt).trim();
  if (inline) return inline;
  const promptFile = asString(kwargs["prompt-file"]).trim();
  if (!promptFile) throw new Error("prompt or prompt-file is required");
  const resolved = path.resolve(promptFile);
  if (!existsSync(resolved)) throw new Error(`prompt-file not found: ${resolved}`);
  return await readFile(resolved, "utf8");
}

function expectedBatchIdFromPrompt(prompt: string): string {
  const exact = prompt.match(/`batch_id`\s*必须为\s*`([^`]+)`/);
  if (exact?.[1]) return exact[1].trim();
  const heading = prompt.match(/^#\s*Batch\s+(.+)$/m);
  if (heading?.[1]) return heading[1].trim();
  return "";
}

function quoteJson(value: unknown): string {
  return JSON.stringify(value);
}

async function evaluateWithArgs<T>(
  page: BrowserPage,
  js: string,
  args: Record<string, unknown>
): Promise<T> {
  return await page.evaluate<T>(`(() => {
    const __args = ${quoteJson(args)};
    return (${js})(__args);
  })()`);
}

async function waitMs(page: BrowserPage, ms: number): Promise<void> {
  if (typeof page.wait === "function") {
    await page.wait(ms).catch(async () => {
      await page.evaluate(`new Promise(resolve => setTimeout(resolve, ${Math.max(0, ms)}))`);
    });
    return;
  }
  await page.evaluate(`new Promise(resolve => setTimeout(resolve, ${Math.max(0, ms)}))`);
}

function textClickScript(args: { labels: string[]; exact?: boolean }): string {
  return `({ labels, exact }) => {
    const norm = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
    const wanted = labels.map(norm).filter(Boolean);
    const elements = Array.from(document.querySelectorAll('button, [role="button"], a, label, div, span, mat-icon'));
    const candidates = elements
      .map((element) => {
        const rect = element.getBoundingClientRect();
        const text = norm(element.innerText || element.textContent || element.getAttribute('aria-label') || element.getAttribute('title') || element.getAttribute('data-tooltip') || '');
        return { element, text, width: rect.width, height: rect.height, area: rect.width * rect.height };
      })
      .filter(({ element, text, width, height, area }) => {
        if (!text || width <= 0 || height <= 0) return false;
        if (["HTML", "BODY", "MAIN"].includes(element.tagName)) return false;
        if (area > 250000) return false;
        return wanted.some((label) => exact ? text === label : text.toLowerCase().includes(label.toLowerCase()));
      })
      .sort((a, b) => a.text.length - b.text.length || a.area - b.area);
    const hit = candidates[0];
    if (!hit) return null;
    const clickable = hit.element.closest('button, [role="button"], a, label') || hit.element;
    clickable.click();
    return hit.text;
  }`;
}

async function clickText(page: BrowserPage, labels: string[], exact = false): Promise<string | null> {
  return await evaluateWithArgs<string | null>(page, textClickScript({ labels, exact }), { labels, exact });
}

function buttonClickScript(args: { labels: string[]; exact?: boolean }): string {
  return `({ labels, exact }) => {
    const norm = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
    const wanted = labels.map(norm).filter(Boolean);
    const elements = Array.from(document.querySelectorAll('button, [role="button"], a, label'));
    const candidates = elements
      .map((element) => {
        const rect = element.getBoundingClientRect();
        const parts = [
          element.getAttribute('aria-label'),
          element.getAttribute('title'),
          element.getAttribute('data-tooltip'),
          element.innerText,
          element.textContent
        ];
        const text = norm(parts.filter(Boolean).join(' '));
        const disabled = element.hasAttribute('disabled') || element.getAttribute('aria-disabled') === 'true';
        return { element, text, disabled, width: rect.width, height: rect.height, area: rect.width * rect.height };
      })
      .filter(({ text, disabled, width, height, area }) => {
        if (!text || disabled || width <= 0 || height <= 0 || area > 250000) return false;
        return wanted.some((label) => exact ? text === label : text.toLowerCase().includes(label.toLowerCase()));
      })
      .sort((a, b) => a.text.length - b.text.length || a.area - b.area);
    const hit = candidates[0];
    if (!hit) return null;
    hit.element.click();
    return hit.text;
  }`;
}

async function clickButtonText(page: BrowserPage, labels: string[], exact = false): Promise<string | null> {
  return await evaluateWithArgs<string | null>(page, buttonClickScript({ labels, exact }), { labels, exact });
}

async function navigateProvider(page: BrowserPage, config: ProviderConfig, urlOverride?: string): Promise<void> {
  const target = urlOverride || config.homeUrl;
  await page.goto(target, { waitUntil: "load", settleMs: 1500 }).catch(async () => {
    await page.goto(target);
  });
  await waitMs(page, 1500);
}

async function probePage(page: BrowserPage, config: ProviderConfig): Promise<Record<string, unknown>> {
  const state = await page.evaluate<Record<string, unknown>>(`(() => {
    const visible = (el) => {
      const rect = el.getBoundingClientRect();
      return rect.width > 0 && rect.height > 0;
    };
    const fileInputs = Array.from(document.querySelectorAll('input[type="file"]')).map((input) => ({
      accept: input.getAttribute('accept') || '',
      multiple: !!input.multiple,
      visible: visible(input),
      files: input.files ? input.files.length : 0
    }));
    const buttons = Array.from(document.querySelectorAll('button, [role="button"], label, a'))
      .map((el) => String(el.innerText || el.textContent || el.getAttribute('aria-label') || el.getAttribute('title') || '').replace(/\\s+/g, ' ').trim())
      .filter(Boolean)
      .slice(0, 80);
    const text = document.body ? String(document.body.innerText || '') : '';
    return {
      title: document.title || '',
      url: location.href,
      fileInputs,
      buttons,
      bodySample: text.slice(0, 1200)
    };
  })()`);
  const text = asString(state.bodySample);
  const loginHint = config.loginHints.find((hint) => text.includes(hint)) || "";
  return {
    ...state,
    loginHint,
    canSeeFileInput: Array.isArray(state.fileInputs) && state.fileInputs.length > 0
  };
}

async function enableMode(page: BrowserPage, config: ProviderConfig, requestedMode: string): Promise<string> {
  const mode = requestedMode.trim().toLowerCase();
  if (!mode) return "";
  if (config.provider === "qwen" && (mode === "think" || mode === "deepthink")) {
    return await clickText(page, config.modeLabels) || "";
  }
  if (config.provider === "gemini" && mode === "pro") {
    return await clickText(page, config.modeLabels) || "";
  }
  return await clickText(page, [requestedMode, ...config.modeLabels]) || "";
}

const UPLOAD_TARGET_ATTR = "data-opencli-video-upload-target";

async function markCompatibleFileInput(page: BrowserPage): Promise<string> {
  return await page.evaluate<string>(`(() => {
    const compatible = (input) => {
      const accept = String(input.getAttribute('accept') || '').toLowerCase();
      if (input.disabled) return false;
      if (accept.includes('.json') || accept.includes('application/json')) return false;
      return !accept || accept.includes('video') || accept.includes('*') || accept.includes('mp4') || accept.includes('mov');
    };
    for (const input of Array.from(document.querySelectorAll('input[type="file"]'))) {
      input.removeAttribute('${UPLOAD_TARGET_ATTR}');
    }
    const inputs = Array.from(document.querySelectorAll('input[type="file"]')).filter(compatible);
    const target = inputs[inputs.length - 1];
    if (!target) return '';
    target.setAttribute('${UPLOAD_TARGET_ATTR}', '1');
    return \`input[type="file"][${UPLOAD_TARGET_ATTR}="1"]\`;
  })()`).catch(() => "");
}

async function waitForFileInput(page: BrowserPage, config: ProviderConfig, timeoutMs: number): Promise<string> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const selector = await markCompatibleFileInput(page);
    if (selector) return selector;
    await openUploadControls(page, config).catch(() => null);
    await waitMs(page, 750);
  }
  return "";
}

async function openUploadControls(page: BrowserPage, config: ProviderConfig): Promise<void> {
  if (config.provider === "gemini") {
    const opened = await cdpClickSelector(page, 'button[aria-label="Upload & tools"]')
      || await evaluateWithArgs<string | null>(page, `() => {
          const button = document.querySelector('button[aria-label="Upload & tools"]');
          if (!button) return null;
          const rect = button.getBoundingClientRect();
          const eventInit = {
            bubbles: true,
            cancelable: true,
            view: window,
            clientX: rect.left + rect.width / 2,
            clientY: rect.top + rect.height / 2
          };
          for (const type of ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {
            button.dispatchEvent(new MouseEvent(type, eventInit));
          }
          return 'Upload & tools';
        }`, {}) || await clickButtonText(page, ["Upload & tools"], true);
    await waitMs(page, 500);
    await clickButtonText(page, [
      "Upload files",
      "Upload file",
      "Upload from computer",
      "Add files",
      "上传文件",
      "从设备上传",
      "添加文件"
    ]).catch(() => null);
    if (opened) return;
  }
  await clickText(page, config.uploadLabels).catch(() => null);
}

async function cdpClickSelector(page: BrowserPage, selector: string): Promise<string | null> {
  if (typeof page.cdp !== "function") return null;
  const point = await evaluateWithArgs<{ x: number; y: number } | null>(page, `({ selector }) => {
    const el = document.querySelector(selector);
    if (!el) return null;
    const rect = el.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) return null;
    return { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 };
  }`, { selector }).catch(() => null);
  if (!point) return null;
  await page.cdp("Input.dispatchMouseEvent", { type: "mouseMoved", x: point.x, y: point.y }).catch(() => undefined);
  await page.cdp("Input.dispatchMouseEvent", { type: "mousePressed", x: point.x, y: point.y, button: "left", clickCount: 1 });
  await page.cdp("Input.dispatchMouseEvent", { type: "mouseReleased", x: point.x, y: point.y, button: "left", clickCount: 1 });
  return selector;
}

async function uploadFiles(page: BrowserPage, files: string[], config: ProviderConfig): Promise<Record<string, unknown>> {
  const selector = await waitForFileInput(page, config, 20_000);
  if (!selector) {
    throw new Error("no file input found after clicking upload controls");
  }

  if (typeof page.setFileInput === "function") {
    await page.setFileInput(files, selector);
  } else if (typeof page.uploadFiles === "function") {
    await page.uploadFiles("file", files, { firstOnMulti: true });
  } else {
    throw new Error("active OpenCLI page does not expose setFileInput/uploadFiles");
  }

  const basenames = files.map((file) => path.basename(file));
  await evaluateWithArgs(page, `() => {
    for (const input of Array.from(document.querySelectorAll('input[type="file"]'))) {
      input.dispatchEvent(new Event('input', { bubbles: true }));
      input.dispatchEvent(new Event('change', { bubbles: true }));
    }
    return true;
  }`, {});
  const attachmentState = await waitForAttachmentReady(page, basenames, config);
  await acceptProviderModals(page, config);
  return await page.evaluate<Record<string, unknown>>(`(() => ({
    fileInputs: Array.from(document.querySelectorAll('input[type="file"]')).map(input => ({
      files: input.files ? Array.from(input.files).map(file => file.name) : [],
      accept: input.getAttribute('accept') || '',
      multiple: !!input.multiple
    })),
    textSample: (document.body.innerText || '').slice(0, 2000),
    attachmentState: ${quoteJson(attachmentState)}
  }))()`);
}

async function acceptProviderModals(page: BrowserPage, config: ProviderConfig): Promise<string> {
  if (config.provider !== "gemini") return "";
  const labels = ["Agree", "I agree", "同意", "我同意", "Accept"];
  const deadline = Date.now() + 15_000;
  while (Date.now() < deadline) {
    const clicked = await clickButtonText(page, labels, true) || await clickButtonText(page, labels, false);
    if (clicked) {
      await waitMs(page, 1000);
      return clicked;
    }
    await waitMs(page, 1000);
  }
  return "";
}

async function waitForAttachmentReady(
  page: BrowserPage,
  basenames: string[],
  config: ProviderConfig
): Promise<Record<string, unknown>> {
  const timeoutMs = config.provider === "gemini" ? 90_000 : 45_000;
  const deadline = Date.now() + timeoutMs;
  const lowerNames = basenames.map((name) => name.toLowerCase());
  let last: Record<string, unknown> = {};
  while (Date.now() < deadline) {
    last = await evaluateWithArgs<Record<string, unknown>>(page, `({ lowerNames }) => {
      const text = String(document.body?.innerText || '');
      const lower = text.toLowerCase();
      const visibleNames = lowerNames.filter((name) => lower.includes(name));
      const uploading = /uploading|上传中|processing|处理中|analyzing|正在|loading/i.test(text);
      const disabledSend = Array.from(document.querySelectorAll('button, [role="button"]')).some((button) => {
        const label = String(button.getAttribute('aria-label') || button.getAttribute('title') || button.textContent || '');
        const disabled = button.hasAttribute('disabled') || button.getAttribute('aria-disabled') === 'true';
        return /send|发送/i.test(label) && disabled;
      });
      return {
        visibleNames,
        uploading,
        disabledSend,
        textLength: text.length
      };
    }`, { lowerNames }).catch(() => ({}));
    const visibleNames = Array.isArray(last.visibleNames) ? last.visibleNames : [];
    if (visibleNames.length >= Math.min(1, basenames.length) && !last.uploading && !last.disabledSend) {
      return { ...last, ready: true, waitedMs: timeoutMs - Math.max(0, deadline - Date.now()) };
    }
    await waitMs(page, 3000);
  }
  return { ...last, ready: false, waitedMs: timeoutMs };
}

async function fillPrompt(page: BrowserPage, prompt: string): Promise<Record<string, unknown>> {
  return await evaluateWithArgs<Record<string, unknown>>(page, `({ prompt }) => {
    const visible = (el) => {
      const rect = el.getBoundingClientRect();
      return rect.width > 0 && rect.height > 0;
    };
    const candidates = Array.from(document.querySelectorAll('textarea, [contenteditable="true"], [role="textbox"], div[contenteditable]'))
      .filter((el) => visible(el))
      .sort((a, b) => {
        const ar = a.getBoundingClientRect();
        const br = b.getBoundingClientRect();
        return (br.width * br.height) - (ar.width * ar.height);
      });
    const el = candidates[0];
    if (!el) return { filled: false, reason: 'textbox not found' };
    el.focus();
    if (el instanceof HTMLTextAreaElement || el instanceof HTMLInputElement) {
      el.value = prompt;
      el.dispatchEvent(new Event('input', { bubbles: true }));
      el.dispatchEvent(new Event('change', { bubbles: true }));
      return { filled: true, mode: el.tagName.toLowerCase(), length: prompt.length };
    }
    document.execCommand('selectAll', false);
    document.execCommand('insertText', false, prompt);
    el.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText', data: prompt }));
    return { filled: true, mode: 'contenteditable', length: prompt.length };
  }`, { prompt });
}

async function sendPrompt(page: BrowserPage, config: ProviderConfig): Promise<string> {
  await waitMs(page, config.provider === "gemini" ? 3000 : 600);
  const clicked = await clickButtonText(page, config.sendLabels) || await clickText(page, config.sendLabels);
  if (clicked) return clicked;
  if (typeof page.pressKey === "function") {
    await page.pressKey("Enter");
    return "Enter";
  }
  throw new Error("send button not found and pressKey is unavailable");
}

function tryParseJsonCandidate(text: string): unknown | null {
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

function extractJsonObjects(text: string): unknown[] {
  const objects: unknown[] = [];
  const fenced = [...text.matchAll(/```(?:json)?\s*([\s\S]*?)```/gi)].map((m) => m[1]);
  for (const block of fenced) {
    const parsed = tryParseJsonCandidate(block.trim());
    if (parsed) objects.push(parsed);
  }

  let start = -1;
  let depth = 0;
  let inString = false;
  let escaped = false;
  for (let i = 0; i < text.length; i += 1) {
    const ch = text[i];
    if (inString) {
      if (escaped) {
        escaped = false;
      } else if (ch === "\\") {
        escaped = true;
      } else if (ch === "\"") {
        inString = false;
      }
      continue;
    }
    if (ch === "\"") {
      inString = true;
      continue;
    }
    if (ch === "{") {
      if (depth === 0) start = i;
      depth += 1;
    } else if (ch === "}") {
      depth -= 1;
      if (depth === 0 && start >= 0) {
        const parsed = tryParseJsonCandidate(text.slice(start, i + 1));
        if (parsed) objects.push(parsed);
        start = -1;
      }
    }
  }
  return objects;
}

function selectDraftJson(text: string, expectedBatchId = ""): Record<string, unknown> | null {
  const candidates = extractJsonObjects(text);
  for (let i = candidates.length - 1; i >= 0; i -= 1) {
    const item = candidates[i];
    if (item && typeof item === "object" && Array.isArray((item as { records?: unknown }).records)) {
      const record = item as Record<string, unknown>;
      const batchId = asString(record.batch_id).trim();
      if (batchId === "string" || batchId === "") continue;
      if (expectedBatchId && batchId !== expectedBatchId) continue;
      return record;
    }
  }
  return null;
}

async function waitForDraftJson(
  page: BrowserPage,
  expectedRecords: number,
  timeoutSeconds: number,
  expectedBatchId = ""
): Promise<{ json: Record<string, unknown> | null; rawText: string; stable: boolean }> {
  const deadline = Date.now() + timeoutSeconds * 1000;
  let lastText = "";
  let stableRounds = 0;
  let lastJson: Record<string, unknown> | null = null;

  while (Date.now() < deadline) {
    const text = await page.evaluate<string>(`(() => document.body ? document.body.innerText || '' : '')()`);
    const draft = selectDraftJson(text, expectedBatchId);
    if (draft) {
      lastJson = draft;
      const records = draft.records;
      if (Array.isArray(records) && records.length >= expectedRecords) {
        await waitMs(page, 2000);
        return { json: draft, rawText: text, stable: true };
      }
    }

    if (text === lastText && text.length > 0) {
      stableRounds += 1;
    } else {
      stableRounds = 0;
      lastText = text;
    }
    if (stableRounds >= 4 && lastJson) {
      return { json: lastJson, rawText: text, stable: true };
    }
    await waitMs(page, 5000);
  }

  const rawText = await page.evaluate<string>(`(() => document.body ? document.body.innerText || '' : '')()`).catch(() => lastText);
  return { json: selectDraftJson(rawText, expectedBatchId), rawText, stable: false };
}

async function writeOutput(outPath: string, payload: unknown): Promise<string> {
  const resolved = path.resolve(outPath);
  await mkdir(path.dirname(resolved), { recursive: true });
  await writeFile(resolved, JSON.stringify(payload, null, 2), "utf8");
  return resolved;
}

export async function runAiVideoProbeCommand(page: BrowserPage, kwargs: Record<string, unknown>): Promise<Array<Record<string, unknown>>> {
  const provider = providerFrom(kwargs.provider);
  const config = PROVIDERS[provider];
  await navigateProvider(page, config, asString(kwargs.url).trim() || undefined);
  const modeClicked = await enableMode(page, config, asString(kwargs.mode).trim() || (provider === "qwen" ? "think" : "pro")).catch(() => "");
  const state = await probePage(page, config);
  return [{
    provider,
    status: "probed",
    url: state.url,
    fileInputs: Array.isArray(state.fileInputs) ? state.fileInputs.length : 0,
    fileInputDetails: Array.isArray(state.fileInputs) ? JSON.stringify(state.fileInputs).slice(0, 500) : "",
    canSeeFileInput: state.canSeeFileInput,
    modeClicked,
    loginHint: state.loginHint,
    buttons: Array.isArray(state.buttons) ? state.buttons.slice(0, 20).join(" | ") : "",
    message: state.loginHint ? `login hint visible: ${state.loginHint}` : "page loaded"
  }];
}

export async function runAiVideoSubmitCommand(page: BrowserPage, kwargs: Record<string, unknown>): Promise<Array<Record<string, unknown>>> {
  const provider = providerFrom(kwargs.provider);
  const config = PROVIDERS[provider];
  const files = splitFiles(kwargs.files);
  const prompt = await readPrompt(kwargs);
  const expectedBatchId = expectedBatchIdFromPrompt(prompt);
  const out = asString(kwargs.out).trim();
  const timeoutSeconds = asNumber(kwargs.timeout, 600);
  const dryRun = asBool(kwargs["dry-run"], false);
  const mode = asString(kwargs.mode).trim() || (provider === "qwen" ? "think" : "pro");

  await navigateProvider(page, config, asString(kwargs.url).trim() || undefined);
  const modeClicked = await enableMode(page, config, mode).catch(() => "");
  const before = await probePage(page, config);

  if (dryRun) {
    return [{
      provider,
      status: "dry_run",
      uploaded: 0,
      files: files.length,
      url: before.url,
      modeClicked,
      message: "dry-run stopped before upload"
    }];
  }

  const uploadState = await uploadFiles(page, files, config);
  const fillState = await fillPrompt(page, prompt);
  if (!fillState.filled) {
    throw new Error(`failed to fill prompt: ${asString(fillState.reason)}`);
  }
  await acceptProviderModals(page, config);
  let sentBy = await sendPrompt(page, config);
  const acceptedAfterSend = await acceptProviderModals(page, config);
  if (acceptedAfterSend) {
    sentBy = `${sentBy}+${acceptedAfterSend}`;
    await waitMs(page, 1000);
    sentBy = `${sentBy}+${await sendPrompt(page, config).catch(() => "resend_skipped")}`;
  }
  const draft = await waitForDraftJson(page, files.length, timeoutSeconds, expectedBatchId);
  const jsonPayload = draft.json ?? {
    provider,
    status: "raw_text_only",
    rawText: draft.rawText
  };
  const outputPath = out ? await writeOutput(out, jsonPayload) : "";
  const records = Array.isArray((jsonPayload as { records?: unknown }).records)
    ? ((jsonPayload as { records: unknown[] }).records.length)
    : 0;
  const attachmentState = uploadState.attachmentState && typeof uploadState.attachmentState === "object"
    ? uploadState.attachmentState as Record<string, unknown>
    : {};
  return [{
    provider,
    status: draft.json ? "ok" : "no_json",
    uploaded: files.length,
    records,
    out: outputPath,
    batchId: asString((jsonPayload as { batch_id?: unknown }).batch_id),
    modeClicked,
    sentBy,
    stable: draft.stable,
    uploadInputs: Array.isArray(uploadState.fileInputs) ? uploadState.fileInputs.length : "",
    attachmentReady: attachmentState.ready ?? "",
    attachmentVisibleNames: Array.isArray(attachmentState.visibleNames) ? attachmentState.visibleNames.join("|") : "",
    attachmentWaitedMs: attachmentState.waitedMs ?? "",
    message: draft.json ? "draft JSON captured" : "model response did not contain parseable draft JSON"
  }];
}
