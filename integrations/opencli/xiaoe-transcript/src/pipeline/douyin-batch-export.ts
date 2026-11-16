import { defaultOutputDirectory } from "../utils/output-directory.js";
import { access, mkdtemp, mkdir, readFile, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";

import { createDouyinCommentCollector, normalizeDouyinCommentLimit } from "../providers/douyin-comments.js";
import { createDouyinVideoCollector } from "../providers/douyin-videos.js";
import type {
  BrowserCookie,
  DouyinBatchExportItemResult,
  DouyinBatchExportOptions,
  DouyinBatchExportResult,
  DouyinCommentSummary,
  DouyinVideoSummary
} from "../types.js";
import { runCommand, type CommandResult } from "../utils/command.js";
import { ensureParentDir, sanitizeFileName } from "../utils/files.js";

const DEFAULT_DOUYIN_BATCH_OUT_DIR = defaultOutputDirectory();
const DOWNLOAD_TIMEOUT_MS = 15 * 60 * 1000;
const TRANSCRIBE_TIMEOUT_MS = 30 * 60 * 1000;
const GENERIC_BATCH_SPEAKER_RE = /^发言人(?:\s+\d+)?$/;
const FASTER_WHISPER_MODEL = process.env.DOUYIN_ASR_MODEL || "small";

interface DouyinBatchDependencies {
  collectVideos: (input: { sourceUrl?: string; secUid?: string; limit?: number }) => Promise<{
    creatorName: string;
    creatorSecUid: string;
    userAgent?: string;
    cookies: BrowserCookie[];
    videos: DouyinVideoSummary[];
  }>;
  createCommentCollector: ReturnType<typeof createDouyinCommentCollector>;
  runCommand: (
    command: string,
    args: string[],
    options?: { cwd?: string; timeoutMs?: number; allowFailure?: boolean }
  ) => Promise<CommandResult>;
  transcribeAudio: (input: {
    audioPath: string;
    sourceUrl: string;
    preferredTitle: string;
    tempDir: string;
  }) => Promise<{ segments: Array<{ timestamp: string; speaker?: string; text: string }> }>;
}

function createDefaultDependencies(): DouyinBatchDependencies {
  const videoCollector = createDouyinVideoCollector();
  return {
    collectVideos: async (input) => {
      try {
        return await videoCollector.collectVideos(input);
      } finally {
        await videoCollector.close().catch(() => undefined);
      }
    },
    createCommentCollector: createDouyinCommentCollector(),
    runCommand,
    transcribeAudio: async (input) => {
      const pythonScript = [
        "import json, sys",
        "from faster_whisper import WhisperModel",
        `model = WhisperModel(${JSON.stringify(FASTER_WHISPER_MODEL)}, device='cpu', compute_type='int8')`,
        "segments, info = model.transcribe(sys.argv[1], language='zh', vad_filter=False, beam_size=5)",
        "rows = []",
        "for seg in segments:",
        "    text = (seg.text or '').strip()",
        "    if not text:",
        "        continue",
        "    rows.append({'timestamp': f\"{int(seg.start // 3600):02d}:{int((seg.start % 3600) // 60):02d}:{int(seg.start % 60):02d}\", 'speaker': None, 'text': text})",
        "print(json.dumps({'segments': rows}, ensure_ascii=False))"
      ].join("\n");
      const result = await runCommand("python", ["-X", "utf8", "-c", pythonScript, input.audioPath], {
        timeoutMs: TRANSCRIBE_TIMEOUT_MS
      });
      const parsed = JSON.parse(result.stdout) as { segments?: Array<{ timestamp: string; speaker?: string; text: string }> };
      return {
        segments: Array.isArray(parsed.segments) ? parsed.segments : []
      };
    }
  };
}

async function pathExists(filePath: string | undefined): Promise<boolean> {
  if (!filePath) {
    return false;
  }
  try {
    await access(filePath);
    return true;
  } catch {
    return false;
  }
}

function formatCookieHeader(cookies: BrowserCookie[]): string {
  return cookies.map((cookie) => `${cookie.name}=${cookie.value}`).join("; ");
}

async function downloadVideoFile(
  video: DouyinVideoSummary,
  outputPath: string,
  deps: DouyinBatchDependencies,
  cookies: BrowserCookie[],
  userAgent?: string
): Promise<void> {
  const headers: string[] = [`Referer: https://www.douyin.com/`];
  const cookieHeader = cookies.length > 0 ? formatCookieHeader(cookies) : "";
  if (cookieHeader) {
    headers.push(`Cookie: ${cookieHeader}`);
  }
  const args = ["-y"];
  if (userAgent) {
    args.push("-user_agent", userAgent);
  }
  args.push("-headers", `${headers.join("\r\n")}\r\n`, "-i", video.playUrl, "-c", "copy", outputPath);
  await deps.runCommand("ffmpeg", args, { timeoutMs: DOWNLOAD_TIMEOUT_MS });
}

async function normalizeMediaToMp3(inputPath: string, outputPath: string, deps: DouyinBatchDependencies): Promise<void> {
  await deps.runCommand("ffmpeg", [
    "-y",
    "-i",
    inputPath,
    "-vn",
    "-ac",
    "1",
    "-ar",
    "16000",
    "-codec:a",
    "libmp3lame",
    "-b:a",
    "64k",
    outputPath
  ], { timeoutMs: TRANSCRIBE_TIMEOUT_MS });
}

function formatSegmentLine(segment: { timestamp: string; speaker?: string; text: string }): string {
  const speaker = segment.speaker && !GENERIC_BATCH_SPEAKER_RE.test(segment.speaker)
    ? `${segment.speaker}: `
    : "";
  return `[${segment.timestamp}] ${speaker}${segment.text}`;
}

function buildTranscriptMarkdown(input: {
  title: string;
  sourceUrl: string;
  comments: DouyinCommentSummary[];
  segments: Array<{ timestamp: string; speaker?: string; text: string }>;
}): string {
  const lines = [
    `# ${input.title}`,
    "",
    `> 原视频: ${input.sourceUrl}`,
    "",
    "## 逐字稿",
    ""
  ];
  for (const segment of input.segments) {
    lines.push(formatSegmentLine(segment));
  }
  if (input.comments.length > 0) {
    lines.push("", "## 评论区摘录", "");
    for (const comment of input.comments) {
      const prefix = comment.nickname ? `${comment.nickname}${comment.diggCount > 0 ? `（${comment.diggCount}赞）` : ""}` : "匿名用户";
      lines.push(`- ${prefix}：${comment.text}`);
    }
  }
  lines.push("");
  return lines.join("\n");
}

async function readExistingIndex(creatorDir: string): Promise<{
  awemeIdToPaths: Map<string, { transcriptPath?: string }>;
  usedTranscriptPaths: Set<string>;
}> {
  const indexPath = path.join(creatorDir, "index.json");
  try {
    const raw = await readFile(indexPath, "utf8");
    const parsed = JSON.parse(raw) as {
      items?: Array<{ awemeId?: string; transcriptPath?: string }>;
    };
    const awemeIdToPaths = new Map<string, { transcriptPath?: string }>();
    const usedTranscriptPaths = new Set<string>();
    for (const item of parsed.items ?? []) {
      if (typeof item.transcriptPath === "string" && item.transcriptPath.trim()) {
        usedTranscriptPaths.add(item.transcriptPath);
      }
      if (typeof item.awemeId === "string") {
        awemeIdToPaths.set(item.awemeId, {
          transcriptPath: typeof item.transcriptPath === "string" ? item.transcriptPath : undefined
        });
      }
    }
    return { awemeIdToPaths, usedTranscriptPaths };
  } catch {
    return {
      awemeIdToPaths: new Map<string, { transcriptPath?: string }>(),
      usedTranscriptPaths: new Set<string>()
    };
  }
}

function createPathResolvers(
  creatorDir: string,
  existing: Awaited<ReturnType<typeof readExistingIndex>>
) {
  const allocate = (
    title: string,
    awemeId: string,
    collectionName?: string
  ) => {
    const existingPaths = existing.awemeIdToPaths.get(awemeId);
    const reused = existingPaths?.transcriptPath;
    if (reused) {
      return reused;
    }

    const base = sanitizeFileName(title) || awemeId;
    const parentDir = path.join(creatorDir, sanitizeFileName(collectionName?.trim() || "未归类"));
    let index = 0;
    while (true) {
      const fileName = index === 0 ? `${base}.md` : `${base} (${index + 1}).md`;
      const candidate = path.join(parentDir, fileName);
      if (!existing.usedTranscriptPaths.has(candidate)) {
        existing.usedTranscriptPaths.add(candidate);
        return candidate;
      }
      index += 1;
    }
  };

  return {
    allocateTranscriptPath: (title: string, awemeId: string, collectionName?: string) => allocate(title, awemeId, collectionName)
  };
}

export async function runDouyinBatchExport(
  options: DouyinBatchExportOptions,
  overrides?: Partial<DouyinBatchDependencies>,
  onProgress?: (event: {
    stage: string;
    status: "started" | "completed" | "failed";
    detail?: string;
    counts?: { total: number; successCount: number; skippedCount: number; failedCount: number };
  }) => Promise<void> | void
): Promise<DouyinBatchExportResult> {
  const base = createDefaultDependencies();
  const deps = {
    ...base,
    ...overrides
  } satisfies DouyinBatchDependencies;

  await onProgress?.({ stage: "collect-videos", status: "started" });
  const collected = await deps.collectVideos({
    sourceUrl: options.sourceUrl,
    secUid: options.secUid,
    limit: options.limit
  });
  await onProgress?.({ stage: "collect-videos", status: "completed" });

  const creatorDir = path.join(
    options.outDir || DEFAULT_DOUYIN_BATCH_OUT_DIR,
    `${sanitizeFileName(collected.creatorName)}_${sanitizeFileName(collected.creatorSecUid)}`
  );
  await mkdir(creatorDir, { recursive: true });
  const existing = await readExistingIndex(creatorDir);
  const { allocateTranscriptPath } = createPathResolvers(creatorDir, existing);

  const items: DouyinBatchExportItemResult[] = [];
  const commentLimit = normalizeDouyinCommentLimit(options.commentLimit);
  const commentCollector = deps.createCommentCollector;
  try {
    for (const [index, video] of collected.videos.entries()) {
      const transcriptPath = allocateTranscriptPath(video.title, video.awemeId, video.collectionName);
      const item: DouyinBatchExportItemResult = {
        awemeId: video.awemeId,
        title: video.title,
        sourceUrl: video.sourceUrl,
        collectionId: video.collectionId,
        collectionName: video.collectionName,
        transcriptPath,
        status: "failed",
        downloadStatus: "pending",
        transcriptStatus: "pending",
        commentStatus: options.includeComments ? "pending" : "skipped"
      };
      let workDir: string | undefined;

      try {
        if (options.skipExisting && await pathExists(transcriptPath)) {
          item.status = "skipped";
          item.downloadStatus = "skipped";
          item.transcriptStatus = "skipped";
          item.commentStatus = options.includeComments ? "skipped" : "skipped";
          item.commentCount = 0;
          items.push(item);
          await onProgress?.({
            stage: "export-video",
            status: "completed",
            detail: `[${index + 1}/${collected.videos.length}] ${video.title}`,
            counts: {
              total: collected.videos.length,
              successCount: items.filter((entry) => entry.status === "success").length,
              skippedCount: items.filter((entry) => entry.status === "skipped").length,
              failedCount: items.filter((entry) => entry.status === "failed").length
            }
          });
          continue;
        }

        workDir = options.keepArtifacts
          ? path.join(creatorDir, "_artifacts", video.awemeId)
          : await mkdtemp(path.join(os.tmpdir(), `douyin-batch-${video.awemeId}-`));
        const downloadDir = path.join(workDir, "downloads");
        await mkdir(downloadDir, { recursive: true });
        const tmpVideoPath = path.join(downloadDir, `${video.awemeId}.mp4`);

        await downloadVideoFile(video, tmpVideoPath, deps, collected.cookies, collected.userAgent);
        item.downloadStatus = "success";

        let comments: DouyinCommentSummary[] = [];
        if (options.includeComments) {
          try {
            const commentResult = await commentCollector.fetchCommentsBySourceUrl(video.sourceUrl, commentLimit);
            comments = commentResult.comments ?? [];
            item.commentStatus = commentResult.status === "success" ? "success" : "skipped";
            item.commentCount = comments.length;
          } catch (error) {
            item.commentStatus = "failed";
            item.commentCount = 0;
            item.error = undefined;
          }
        } else {
          item.commentStatus = "skipped";
          item.commentCount = 0;
        }

        const audioPath = path.join(workDir, `${video.awemeId}.mp3`);
        await normalizeMediaToMp3(tmpVideoPath, audioPath, deps);
        const transcript = await deps.transcribeAudio({
          audioPath,
          sourceUrl: video.sourceUrl,
          preferredTitle: video.title,
          tempDir: workDir
        });
        item.transcriptStatus = transcript.segments.length > 0 ? "success" : "failed";
        const markdown = buildTranscriptMarkdown({
          title: video.title,
          sourceUrl: video.sourceUrl,
          comments,
          segments: transcript.segments
        });
        await ensureParentDir(transcriptPath);
        await writeFile(transcriptPath, markdown, "utf8");

        item.status = item.transcriptStatus === "success" ? "success" : "failed";
        if (item.transcriptStatus !== "success" && !item.error) {
          item.error = "local ASR returned no transcript segments";
        }
        items.push(item);
      } catch (error) {
        item.status = "failed";
        item.error = error instanceof Error ? error.message : String(error);
        if (item.downloadStatus === "pending") {
          item.downloadStatus = "failed";
        }
        if (item.transcriptStatus === "pending") {
          item.transcriptStatus = "failed";
        }
        if (item.commentStatus === "pending") {
          item.commentStatus = "failed";
        }
        items.push(item);
      } finally {
        if (!options.keepArtifacts && workDir) {
          await rm(workDir, { force: true, recursive: true }).catch(() => undefined);
        }
      }

      await onProgress?.({
        stage: "export-video",
        status: "completed",
        detail: `[${index + 1}/${collected.videos.length}] ${video.title}`,
        counts: {
          total: collected.videos.length,
          successCount: items.filter((entry) => entry.status === "success").length,
          skippedCount: items.filter((entry) => entry.status === "skipped").length,
          failedCount: items.filter((entry) => entry.status === "failed").length
        }
      });
    }
  } finally {
    await commentCollector.close().catch(() => undefined);
  }

  const result: DouyinBatchExportResult = {
    creatorDir,
    creatorName: collected.creatorName,
    creatorSecUid: collected.creatorSecUid,
    total: items.length,
    success: items.filter((item) => item.status === "success").length,
    skipped: items.filter((item) => item.status === "skipped").length,
    failed: items.filter((item) => item.status === "failed").length,
    items
  };

  await writeFile(path.join(creatorDir, "index.json"), JSON.stringify({
    creatorName: result.creatorName,
    creatorSecUid: result.creatorSecUid,
    total: result.total,
    success: result.success,
    skipped: result.skipped,
    failed: result.failed,
    items: result.items
  }, null, 2), "utf8");

  return result;
}
