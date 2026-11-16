import { access, mkdir, readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import { createDouyinCommentCollector, normalizeDouyinCommentLimit } from "../providers/douyin-comments.js";
import { collectGetTranscriptNotes } from "../providers/get-subscription.js";
import { ensureParentDir, sanitizeFileName } from "../utils/files.js";
function createDefaultDependencies() {
    return {
        collectTranscriptNotes: collectGetTranscriptNotes,
        createCommentCollector: () => createDouyinCommentCollector()
    };
}
async function pathExists(filePath) {
    if (!filePath) {
        return false;
    }
    try {
        await access(filePath);
        return true;
    }
    catch {
        return false;
    }
}
function normalizeBoundaryDate(rawValue, mode) {
    if (!rawValue) {
        return undefined;
    }
    const normalized = rawValue.trim();
    if (!normalized) {
        return undefined;
    }
    const suffix = mode === "start" ? "T00:00:00.000Z" : "T23:59:59.999Z";
    const parsed = new Date(normalized.includes("T") ? normalized : `${normalized}${suffix}`);
    return Number.isNaN(parsed.getTime()) ? undefined : parsed;
}
function shouldIncludePublishedAt(publishedAt, startDate, endDate) {
    if (!startDate && !endDate) {
        return true;
    }
    if (!publishedAt) {
        return false;
    }
    const parsed = new Date(publishedAt);
    if (Number.isNaN(parsed.getTime())) {
        return false;
    }
    if (startDate && parsed < startDate) {
        return false;
    }
    if (endDate && parsed > endDate) {
        return false;
    }
    return true;
}
export function buildGetTranscriptOnlyMarkdown(input) {
    const lines = [`# ${input.title}`, "", input.content.trim()];
    if (Array.isArray(input.comments) && input.comments.length > 0) {
        lines.push("", "## 评论区摘录", "");
        for (const comment of input.comments) {
            const prefix = comment.nickname ? `${comment.nickname}${comment.diggCount > 0 ? `（${comment.diggCount}赞）` : ""}` : "匿名用户";
            lines.push(`- ${prefix}：${comment.text}`);
        }
    }
    return `${lines.join("\n")}\n`;
}
async function readExistingIndex(creatorDir) {
    const indexPath = path.join(creatorDir, "index.json");
    try {
        const raw = await readFile(indexPath, "utf8");
        const parsed = JSON.parse(raw);
        const noteIdToPath = new Map();
        const usedPaths = new Set();
        for (const item of parsed.items ?? []) {
            if (typeof item.transcriptPath === "string" && item.transcriptPath.trim()) {
                usedPaths.add(item.transcriptPath);
            }
            if (typeof item.noteId === "string" && typeof item.transcriptPath === "string" && item.transcriptPath.trim()) {
                noteIdToPath.set(item.noteId, item.transcriptPath);
            }
        }
        return { noteIdToPath, usedPaths };
    }
    catch {
        return {
            noteIdToPath: new Map(),
            usedPaths: new Set()
        };
    }
}
function createTranscriptPathResolver(creatorDir, existing) {
    return (title, noteId, collectionName) => {
        if (noteId) {
            const existingPath = existing.noteIdToPath.get(noteId);
            if (existingPath) {
                existing.usedPaths.add(existingPath);
                return existingPath;
            }
        }
        const base = sanitizeFileName(title) || "get-transcript";
        const parentDir = collectionName?.trim()
            ? path.join(creatorDir, sanitizeFileName(collectionName))
            : creatorDir;
        let index = 0;
        while (true) {
            const fileName = index === 0 ? `${base}.md` : `${base} (${index + 1}).md`;
            const candidate = path.join(parentDir, fileName);
            if (!existing.usedPaths.has(candidate)) {
                existing.usedPaths.add(candidate);
                return candidate;
            }
            index += 1;
        }
    };
}
export async function runGetBatchExport(options, overrides, onProgress) {
    const deps = {
        ...createDefaultDependencies(),
        ...overrides
    };
    await onProgress?.({
        stage: "collect-notes",
        status: "started"
    });
    const collected = await deps.collectTranscriptNotes({
        subjectUrl: options.subjectUrl,
        creatorId: options.creatorId,
        creatorName: options.creatorName,
        startDate: options.startDate,
        endDate: options.endDate
    });
    await onProgress?.({
        stage: "collect-notes",
        status: "completed"
    });
    const creatorId = collected.creatorId || options.creatorId || "unknown";
    const creatorName = collected.creatorName || options.creatorName || creatorId;
    const creatorDir = path.join(options.outDir, `${sanitizeFileName(creatorName)}_${sanitizeFileName(creatorId)}`);
    await mkdir(creatorDir, { recursive: true });
    const existing = await readExistingIndex(creatorDir);
    const resolveTranscriptPath = createTranscriptPathResolver(creatorDir, existing);
    const startDate = normalizeBoundaryDate(options.startDate, "start");
    const endDate = normalizeBoundaryDate(options.endDate, "end");
    const itemsToProcess = collected.items.filter((item) => shouldIncludePublishedAt(item.publishedAt, startDate, endDate));
    const results = [];
    const commentCollector = options.includeComments ? deps.createCommentCollector() : undefined;
    const commentLimit = normalizeDouyinCommentLimit(options.commentLimit);
    const emitCounts = async (detail) => {
        await onProgress?.({
            stage: "export-note",
            status: "completed",
            detail,
            counts: {
                total: itemsToProcess.length,
                successCount: results.filter((item) => item.status === "success").length,
                skippedCount: results.filter((item) => item.status === "skipped").length,
                failedCount: results.filter((item) => item.status === "failed").length
            }
        });
    };
    try {
        for (const [index, item] of itemsToProcess.entries()) {
            if (item.error) {
                results.push({
                    noteId: item.noteId,
                    title: item.title,
                    publishedAt: item.publishedAt,
                    detailUrl: item.detailUrl,
                    sourceUrl: item.sourceUrl,
                    status: "failed",
                    error: item.error
                });
                await emitCounts(`[${index + 1}/${itemsToProcess.length}] ${item.title}`);
                continue;
            }
            if (!item.content) {
                results.push({
                    noteId: item.noteId,
                    title: item.title,
                    publishedAt: item.publishedAt,
                    detailUrl: item.detailUrl,
                    sourceUrl: item.sourceUrl,
                    status: "failed",
                    error: "Get note detail returned empty content"
                });
                await emitCounts(`[${index + 1}/${itemsToProcess.length}] ${item.title}`);
                continue;
            }
            let comments = [];
            let awemeId;
            let collectionId;
            let collectionName;
            let commentStatus = undefined;
            let commentError;
            if (commentCollector) {
                try {
                    const commentResult = await commentCollector.fetchCommentsBySourceUrl(item.sourceUrl, commentLimit);
                    comments = commentResult.comments ?? [];
                    awemeId = commentResult.awemeId;
                    collectionId = commentResult.collectionId;
                    collectionName = commentResult.collectionName;
                    commentStatus = commentResult.status;
                    commentError = commentResult.reason;
                }
                catch (error) {
                    commentStatus = "failed";
                    commentError = error instanceof Error ? error.message : String(error);
                }
            }
            const transcriptPath = resolveTranscriptPath(item.title, item.noteId, collectionName);
            if (options.skipExisting && await pathExists(transcriptPath)) {
                results.push({
                    noteId: item.noteId,
                    title: item.title,
                    publishedAt: item.publishedAt,
                    detailUrl: item.detailUrl,
                    sourceUrl: item.sourceUrl,
                    transcriptPath,
                    contentSource: item.contentSource,
                    awemeId,
                    collectionId,
                    collectionName,
                    commentStatus,
                    commentCount: comments.length,
                    commentError,
                    status: "skipped"
                });
                await emitCounts(`[${index + 1}/${itemsToProcess.length}] ${item.title}`);
                continue;
            }
            const markdown = buildGetTranscriptOnlyMarkdown({
                title: item.title,
                content: item.content,
                comments
            });
            await ensureParentDir(transcriptPath);
            await writeFile(transcriptPath, markdown, "utf8");
            results.push({
                noteId: item.noteId,
                title: item.title,
                publishedAt: item.publishedAt,
                detailUrl: item.detailUrl,
                sourceUrl: item.sourceUrl,
                transcriptPath,
                contentSource: item.contentSource,
                awemeId,
                collectionId,
                collectionName,
                commentStatus,
                commentCount: comments.length,
                commentError,
                status: "success"
            });
            await emitCounts(`[${index + 1}/${itemsToProcess.length}] ${item.title}`);
        }
    }
    finally {
        await commentCollector?.close().catch(() => undefined);
    }
    const result = {
        creatorDir,
        creatorId,
        creatorName,
        total: results.length,
        success: results.filter((item) => item.status === "success").length,
        skipped: results.filter((item) => item.status === "skipped").length,
        failed: results.filter((item) => item.status === "failed").length,
        items: results
    };
    await writeFile(path.join(creatorDir, "index.json"), JSON.stringify({
        creatorId,
        creatorName,
        subjectUrl: collected.subjectUrl,
        total: result.total,
        success: result.success,
        skipped: result.skipped,
        failed: result.failed,
        items: result.items
    }, null, 2), "utf8");
    return result;
}
