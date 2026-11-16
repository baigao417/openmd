import { defaultOutputDirectory } from "../utils/output-directory.js";
import { existsSync } from "node:fs";
import { mkdtemp, mkdir, readdir, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import process from "node:process";
import { tingwuProvider } from "../providers/tingwu-web.js";
import { runCommand } from "../utils/command.js";
import { ensureParentDir, sanitizeFileName } from "../utils/files.js";
export const DEFAULT_XIAOHONGSHU_BATCH_OUT_DIR = defaultOutputDirectory();
const FETCH_CREATOR_NOTES_TIMEOUT_MS = 2 * 60 * 1000;
const DOWNLOAD_NOTE_TIMEOUT_MS = 15 * 60 * 1000;
const TRANSCRIBE_NOTE_TIMEOUT_MS = 30 * 60 * 1000;
const LOCAL_TRANSCRIBE_TIMEOUT_MINUTES = 30;
const LOCAL_MEDIA_EXTENSIONS = new Set([".mp4", ".m4a", ".mp3", ".wav", ".webm", ".mkv", ".mov", ".aac"]);
const GENERIC_BATCH_SPEAKER_RE = /^发言人(?:\s+\d+)?$/;
function createDefaultDependencies() {
    return {
        runCommand,
        transcribeDownloadedMedia: defaultTranscribeDownloadedMedia
    };
}
export function resolveOpenCliInvocationFromNodeDir(nodeExecPath, platform = process.platform, fileExists = existsSync) {
    const nodeDir = path.dirname(nodeExecPath);
    const mainJsPath = path.join(nodeDir, "node_modules", "@jackwener", "opencli", "dist", "main.js");
    if (fileExists(mainJsPath)) {
        return {
            command: nodeExecPath,
            argsPrefix: [mainJsPath]
        };
    }
    const preferred = platform === "win32"
        ? [path.join(nodeDir, "opencli.cmd"), path.join(nodeDir, "opencli.ps1"), path.join(nodeDir, "opencli")]
        : [path.join(nodeDir, "opencli")];
    const executable = preferred.find((candidate) => fileExists(candidate)) ?? "opencli";
    return {
        command: executable,
        argsPrefix: []
    };
}
function normalizeLimit(limit) {
    if (!Number.isFinite(limit) || limit <= 0) {
        return 1;
    }
    return Math.floor(limit);
}
function parseJsonArray(raw, label) {
    let parsed;
    try {
        parsed = JSON.parse(raw);
    }
    catch (error) {
        throw new Error(`${label} did not return valid JSON: ${error instanceof Error ? error.message : String(error)}`);
    }
    if (!Array.isArray(parsed)) {
        throw new Error(`${label} did not return a JSON array`);
    }
    return parsed;
}
function padSeq(index) {
    return String(index + 1).padStart(2, "0");
}
export function selectBatchVideoNotes(notes, limit) {
    return notes.filter((item) => item.type === "video").slice(0, normalizeLimit(limit));
}
export function resolveBatchCreatorDir(rootDir, creatorName, creatorHandle) {
    const folderName = `${sanitizeFileName(creatorName)}_${sanitizeFileName(creatorHandle)}`;
    return {
        rootDir,
        creatorDir: path.join(rootDir, folderName)
    };
}
async function fetchCreatorNotes(profileUrl, limit, deps) {
    const fetchLimit = String(Math.max(normalizeLimit(limit) * 3, 20));
    const invocation = resolveOpenCliInvocationFromNodeDir(process.execPath);
    const result = await deps.runCommand(invocation.command, [
        ...invocation.argsPrefix,
        "xiaohongshu",
        "user",
        profileUrl,
        "--limit",
        fetchLimit,
        "-f",
        "json"
    ], {
        timeoutMs: FETCH_CREATOR_NOTES_TIMEOUT_MS
    });
    return parseJsonArray(result.stdout, "opencli xiaohongshu user");
}
function buildWorkPaths(workDir) {
    return {
        workDir,
        downloadDir: path.join(workDir, "downloads")
    };
}
function createBaseItemResult(note, seq, workDir, downloadDir, transcriptPath) {
    const now = new Date().toISOString();
    return {
        seq,
        noteId: note.id,
        title: note.title,
        url: note.url,
        likes: note.likes,
        status: "failed",
        itemDir: workDir,
        downloadDir,
        metaPath: "",
        transcriptPath,
        downloadStatus: "pending",
        transcriptStatus: "pending",
        startedAt: now,
        finishedAt: now
    };
}
async function listFilesRecursive(rootDir) {
    const entries = await readdir(rootDir, { withFileTypes: true });
    const files = [];
    for (const entry of entries) {
        const fullPath = path.join(rootDir, entry.name);
        if (entry.isDirectory()) {
            files.push(...await listFilesRecursive(fullPath));
            continue;
        }
        if (entry.isFile()) {
            files.push(fullPath);
        }
    }
    return files;
}
async function findFirstDownloadedMediaFile(downloadDir) {
    const files = await listFilesRecursive(downloadDir);
    const mediaFile = files.find((filePath) => LOCAL_MEDIA_EXTENSIONS.has(path.extname(filePath).toLowerCase()));
    if (!mediaFile) {
        throw new Error(`no local media file found under ${downloadDir}`);
    }
    return mediaFile;
}
async function probeMediaDurationSeconds(filePath) {
    const result = await runCommand("ffprobe", [
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        filePath
    ], {
        timeoutMs: 30_000
    });
    const parsed = Number.parseFloat(result.stdout.trim());
    if (!Number.isFinite(parsed)) {
        throw new Error(`unable to read duration from ${filePath}`);
    }
    return parsed;
}
async function normalizeMediaToMp3(inputPath, outputPath) {
    await runCommand("ffmpeg", [
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
    ], {
        timeoutMs: 15 * 60 * 1000
    });
}
function formatSegmentLine(segment) {
    const speaker = segment.speaker && !GENERIC_BATCH_SPEAKER_RE.test(segment.speaker)
        ? `${segment.speaker}: `
        : "";
    return `[${segment.timestamp}] ${speaker}${segment.text}`;
}
export function buildTranscriptOnlyMarkdown(input) {
    const lines = [
        `# ${input.title}`,
        ""
    ];
    for (const segment of input.segments) {
        lines.push(formatSegmentLine(segment));
    }
    lines.push("");
    return lines.join("\n");
}
async function writeTranscriptMarkdown(transcript, transcriptPath) {
    const markdown = buildTranscriptOnlyMarkdown({
        title: transcript.title,
        segments: transcript.segments
    });
    await ensureParentDir(transcriptPath);
    await writeFile(transcriptPath, markdown, "utf8");
}
async function defaultTranscribeDownloadedMedia(input) {
    const localMediaPath = await findFirstDownloadedMediaFile(input.downloadDir);
    const normalizedAudioPath = path.join(input.workDir, "transcript-source.mp3");
    await normalizeMediaToMp3(localMediaPath, normalizedAudioPath);
    try {
        const transcript = await tingwuProvider.transcribe({
            audioPath: normalizedAudioPath,
            sourceUrl: input.note.url,
            preferredTitle: input.note.title,
            tempDir: input.workDir,
            timeoutMinutes: LOCAL_TRANSCRIBE_TIMEOUT_MINUTES
        });
        await writeTranscriptMarkdown(transcript, input.transcriptPath);
        return { transcriptPath: input.transcriptPath };
    }
    finally {
        if (!input.keepArtifacts) {
            await rm(normalizedAudioPath, { force: true }).catch(() => undefined);
        }
    }
}
function createTranscriptPathAllocator(creatorDir) {
    const seen = new Map();
    return (title) => {
        const base = sanitizeFileName(title) || "transcript";
        const current = seen.get(base) ?? 0;
        seen.set(base, current + 1);
        const fileName = current === 0 ? `${base}.md` : `${base} (${current + 1}).md`;
        return path.join(creatorDir, fileName);
    };
}
async function runSingleItem(options, note, seq, creatorDir, allocateTranscriptPath, deps) {
    const workDir = options.keepArtifacts
        ? path.join(creatorDir, "_artifacts", `${seq}_${note.id}`)
        : await mkdtemp(path.join(os.tmpdir(), `xhs-batch-${note.id}-`));
    const paths = buildWorkPaths(workDir);
    const transcriptPath = allocateTranscriptPath(note.title);
    await mkdir(paths.downloadDir, { recursive: true });
    const item = createBaseItemResult(note, seq, workDir, paths.downloadDir, transcriptPath);
    try {
        const invocation = resolveOpenCliInvocationFromNodeDir(process.execPath);
        await deps.runCommand(invocation.command, [
            ...invocation.argsPrefix,
            "xiaohongshu",
            "download",
            note.url,
            "--output",
            paths.downloadDir
        ], {
            timeoutMs: DOWNLOAD_NOTE_TIMEOUT_MS
        });
        item.downloadStatus = "success";
        const transcriptResult = await deps.transcribeDownloadedMedia({
            note,
            workDir: paths.workDir,
            downloadDir: paths.downloadDir,
            transcriptPath,
            keepArtifacts: options.keepArtifacts
        });
        item.transcriptPath = transcriptResult.transcriptPath;
        item.transcriptStatus = "success";
        item.status = "success";
        item.finishedAt = new Date().toISOString();
        return item;
    }
    catch (error) {
        item.status = "failed";
        item.error = error instanceof Error ? error.message : String(error);
        item.finishedAt = new Date().toISOString();
        if (item.downloadStatus === "pending") {
            item.downloadStatus = "failed";
            item.transcriptStatus = "skipped";
        }
        else if (item.transcriptStatus === "pending") {
            item.transcriptStatus = "failed";
        }
        return item;
    }
    finally {
        if (!options.keepArtifacts) {
            await rm(workDir, { force: true, recursive: true }).catch(() => undefined);
        }
    }
}
export async function runXiaohongshuBatch(options, overrides) {
    const deps = {
        ...createDefaultDependencies(),
        ...overrides
    };
    const notes = await fetchCreatorNotes(options.profileUrl, options.limit, deps);
    const selected = selectBatchVideoNotes(notes, options.limit);
    if (selected.length === 0) {
        throw new Error("creator profile returned no video notes");
    }
    const { creatorDir } = resolveBatchCreatorDir(options.outDir, options.creatorName, options.creatorHandle);
    await rm(creatorDir, { force: true, recursive: true }).catch(() => undefined);
    await mkdir(creatorDir, { recursive: true });
    const allocateTranscriptPath = createTranscriptPathAllocator(creatorDir);
    const items = [];
    for (const [index, note] of selected.entries()) {
        const seq = padSeq(index);
        const item = await runSingleItem(options, note, seq, creatorDir, allocateTranscriptPath, deps);
        items.push(item);
    }
    return {
        creatorDir,
        items
    };
}
