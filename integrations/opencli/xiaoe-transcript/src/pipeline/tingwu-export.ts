import { writeFile } from "node:fs/promises";
import path from "node:path";

import { OpenCliPage } from "../opencli/client.js";
import { buildMarkdown } from "./markdown.js";
import type { BrowserCookie, TranscriptProviderName, TranscriptResult, WorkflowResult } from "../types.js";
import {
  collectTingwuTranscriptResult,
  looksLikeTingwuExtractedText,
  parseTingwuApiTranscriptResult,
  parseTingwuExtractedSegments
} from "../providers/tingwu-page.js";
import { assertOpenCliEnvironment } from "../workflow.js";
import { ensureParentDir, sanitizeFileName } from "../utils/files.js";

export interface TingwuExportOptions {
  url: string;
  outDir: string;
  preferredTitle?: string;
  tabId?: number;
  pageId?: string;
}

interface TingwuExportDependencies {
  assertEnvironment: () => Promise<void>;
  extractTranscript: (url: string, preferredTitle?: string, pageId?: string) => Promise<TranscriptResult>;
  writeMarkdownFile: (filePath: string, content: string) => Promise<void>;
}

type TingwuExportStageReporter = (event: {
  stage: string;
  status: "started" | "completed" | "failed";
  detail?: string;
}) => Promise<void> | void;

function extractTingwuTransId(url: string): string {
  try {
    const parsed = new URL(url);
    const match = parsed.pathname.match(/\/(?:doc\/)?transcripts\/([^/?#]+)/);
    return match?.[1] ? decodeURIComponent(match[1]) : "";
  } catch {
    return "";
  }
}

function formatCookieHeader(cookies: BrowserCookie[]): string {
  return cookies
    .filter((cookie) => cookie.name && cookie.value)
    .map((cookie) => `${cookie.name}=${cookie.value}`)
    .join("; ");
}

async function readTingwuApiTranscriptDirect(
  page: OpenCliPage,
  url: string,
  preferredTitle?: string
): Promise<TranscriptResult | null> {
  const transId = extractTingwuTransId(url);
  if (!transId) {
    return null;
  }

  const cookies = await page.getCookies({ url: "https://tingwu.aliyun.com/" });
  const cookieHeader = formatCookieHeader(cookies);
  if (!cookieHeader) {
    return null;
  }

  const userAgent = await page.evaluate<string>("navigator.userAgent || ''").catch(() => "");
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 120_000);
  try {
    const response = await fetch("https://tingwu.aliyun.com/api/trans/getTransResult?c=web", {
      method: "POST",
      headers: {
        "content-type": "application/json",
        "cookie": cookieHeader,
        "origin": "https://tingwu.aliyun.com",
        "referer": url,
        ...(userAgent ? { "user-agent": userAgent } : {})
      },
      body: JSON.stringify({ transId }),
      signal: controller.signal
    });
    if (!response.ok) {
      return null;
    }
    const apiResponse = await response.json() as {
      code?: string;
      data?: Parameters<typeof parseTingwuApiTranscriptResult>[0];
    };
    if (apiResponse.code !== "0" || !apiResponse.data) {
      return null;
    }
    return parseTingwuApiTranscriptResult(apiResponse.data, preferredTitle || "");
  } finally {
    clearTimeout(timer);
  }
}

function createDefaultDependencies(): TingwuExportDependencies {
  return {
    assertEnvironment: assertOpenCliEnvironment,
    extractTranscript: async (url, preferredTitle, pageId) => {
      const page = new OpenCliPage(`tingwu-export:${Date.now()}`, pageId);
      try {
        if (!pageId) {
          await page.goto(url);
        }
        const apiResult = await readTingwuApiTranscriptDirect(page, url, preferredTitle);
        if (apiResult) {
          return apiResult;
        }
        return await collectTingwuTranscriptResult(page, preferredTitle || "");
      } finally {
        if (!pageId) {
          await page.closeWindow();
        }
      }
    },
    writeMarkdownFile: async (filePath, content) => {
      await ensureParentDir(filePath);
      await writeFile(filePath, content, "utf8");
    }
  };
}

export { looksLikeTingwuExtractedText, parseTingwuExtractedSegments };

export async function runTingwuExportToMarkdown(
  options: TingwuExportOptions,
  overrides?: Partial<TingwuExportDependencies>,
  onStageChange?: TingwuExportStageReporter
): Promise<WorkflowResult> {
  const deps = {
    ...createDefaultDependencies(),
    ...overrides
  } satisfies TingwuExportDependencies;

  await onStageChange?.({ stage: "environment-check", status: "started" });
  await deps.assertEnvironment();
  await onStageChange?.({ stage: "environment-check", status: "completed" });
  await onStageChange?.({ stage: "extract-transcript", status: "started" });
  const transcript = await deps.extractTranscript(options.url, options.preferredTitle, options.pageId);
  await onStageChange?.({ stage: "extract-transcript", status: "completed" });
  const outputTitle = sanitizeFileName(options.preferredTitle || transcript.title || "tingwu-transcript");
  const outputPath = path.join(options.outDir, `${outputTitle}.md`);
  const mediaDurationSec = transcript.transcriptDurationSec || transcript.segments[transcript.segments.length - 1]?.startSec || 0;
  const provider: TranscriptProviderName = "tingwu";

  const markdown = buildMarkdown({
    title: options.preferredTitle || transcript.title || "tingwu-transcript",
    sourceUrl: options.url,
    provider,
    mediaDurationSec,
    coverage: 1,
    generatedAt: new Date().toISOString(),
    chapters: transcript.chapters,
    segments: transcript.segments,
    rawText: transcript.rawText
  });

  await onStageChange?.({ stage: "write-markdown", status: "started" });
  await deps.writeMarkdownFile(outputPath, markdown);
  await onStageChange?.({ stage: "write-markdown", status: "completed" });

  return {
    outputPath,
    provider,
    coverage: 1,
    tempDir: ""
  };
}
