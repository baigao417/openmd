import { writeFile } from "node:fs/promises";
import path from "node:path";
import { fetchDaemonStatus } from "./opencli/client.js";
import { computeCoverageWithRawText } from "./pipeline/coverage.js";
import { downloadMediaArtifact } from "./pipeline/download.js";
import { buildMarkdown } from "./pipeline/markdown.js";
import { discoverMediaOnPage } from "./pipeline/media-page.js";
import { getProvider } from "./providers/get-web.js";
import { tingwuProvider } from "./providers/tingwu-web.js";
import { redactSensitiveText, runCommand } from "./utils/command.js";
import { WorkflowStageError } from "./utils/errors.js";
import { cleanupPaths, ensureParentDir, sanitizeFileName } from "./utils/files.js";
import { createTranscriptTempDir } from "./utils/transcript-temp.js";
export async function assertOpenCliEnvironment() {
    await runCommand("opencli", ["doctor"], {
        allowFailure: true,
        timeoutMs: 20000
    });
    const status = await fetchDaemonStatus();
    if (!status) {
        throw new Error("opencli daemon is not reachable on localhost:19825");
    }
    if (!status.extensionConnected) {
        throw new Error("opencli browser bridge extension is not connected");
    }
}
function createDefaultDependencies() {
    return {
        assertEnvironment: assertOpenCliEnvironment,
        createTempDir: createTranscriptTempDir,
        discoverMedia: discoverMediaOnPage,
        downloadMedia: downloadMediaArtifact,
        providers: {
            tingwu: tingwuProvider,
            get: getProvider
        },
        writeMarkdownFile: async (filePath, content) => {
            await ensureParentDir(filePath);
            await writeFile(filePath, content, "utf8");
        },
        cleanupArtifacts: cleanupPaths
    };
}
async function runStage(stage, tempDir, callback, onStageChange) {
    try {
        await onStageChange?.({ stage, status: "started" });
        const result = await callback();
        await onStageChange?.({ stage, status: "completed" });
        return result;
    }
    catch (error) {
        await onStageChange?.({
            stage,
            status: "failed",
            detail: error instanceof Error ? error.message : String(error)
        });
        throw new WorkflowStageError(stage, error, tempDir);
    }
}
function getProviderOrder(mode) {
    if (mode === "tingwu") {
        return ["tingwu"];
    }
    if (mode === "get") {
        return ["get"];
    }
    return ["tingwu", "get"];
}
export async function runXiaoeToMarkdown(options, overrides, onStageChange) {
    return await runMediaToMarkdown(options, overrides, onStageChange);
}
function resolveWorkflowDependencies(overrides) {
    return {
        ...createDefaultDependencies(),
        ...overrides
    };
}
export async function runMediaToMarkdown(options, overrides, onStageChange) {
    const prepared = await prepareMediaToMarkdown(options, overrides, onStageChange);
    return await completePreparedMediaToMarkdown(prepared, overrides, onStageChange);
}
export async function prepareMediaToMarkdown(options, overrides, onStageChange) {
    const outputDir = options.outDir.trim();
    if (!outputDir) {
        throw new Error("请先设置输出目录");
    }
    const deps = resolveWorkflowDependencies(overrides);
    let tempDir;
    try {
        await runStage("environment-check", tempDir, async () => {
            await deps.assertEnvironment();
        }, onStageChange);
        tempDir = await runStage("create-temp-dir", tempDir, async () => await deps.createTempDir(), onStageChange);
        const media = await runStage("discover-media", tempDir, async () => {
            return await deps.discoverMedia(options.url, tempDir);
        }, onStageChange);
        const downloaded = await runStage("download", tempDir, async () => {
            const result = await deps.downloadMedia(media, tempDir, options.timeoutMinutes, async (download) => {
                await onStageChange?.({ stage: "download", status: "started", download });
            });
            if (media.expectedDurationSec && result.mediaDurationSec + 2 < media.expectedDurationSec * 0.98) {
                throw new Error(`Downloaded audio is shorter than the authenticated video metadata: ${result.mediaDurationSec.toFixed(2)}s of ${media.expectedDurationSec.toFixed(2)}s; artifacts retained`);
            }
            return result;
        }, onStageChange);
        return {
            options,
            tempDir,
            media,
            downloaded,
            cleanupTargets: downloaded.cleanupPaths
        };
    }
    catch (error) {
        if (error instanceof WorkflowStageError) {
            throw error;
        }
        throw new WorkflowStageError("workflow", error, tempDir);
    }
}
export async function completePreparedMediaToMarkdown(prepared, overrides, onStageChange) {
    const { options, tempDir, media, downloaded, cleanupTargets } = prepared;
    const outputDir = options.outDir.trim();
    if (!outputDir) {
        throw new Error("请先设置输出目录");
    }
    const deps = resolveWorkflowDependencies(overrides);
    try {
        const providerOrder = getProviderOrder(options.provider);
        let selectedProvider;
        let coverage = 0;
        let coverageStatus = "partial";
        let transcript;
        const warnings = [];
        const providerAttempts = [];
        for (const providerName of providerOrder) {
            const startedAt = new Date().toISOString();
            try {
                const result = await runStage(`transcribe-${providerName}`, tempDir, async () => {
                    return await deps.providers[providerName].transcribe({
                        audioPath: downloaded.audioPath,
                        sourceUrl: options.url,
                        resourceId: options.resourceId,
                        preferredTitle: options.outputTitle || media.pageTitle,
                        tempDir: tempDir,
                        timeoutMinutes: options.timeoutMinutes
                    });
                }, onStageChange);
                const computed = computeCoverageWithRawText(result.transcriptDurationSec, downloaded.mediaDurationSec, result.rawText);
                coverage = computed.coverage;
                coverageStatus = computed.isComplete ? "complete" : "partial";
                transcript = result;
                selectedProvider = providerName;
                providerAttempts?.push({
                    provider: providerName,
                    status: "success",
                    coverage,
                    startedAt,
                    finishedAt: new Date().toISOString()
                });
                if (providerName === "tingwu" && options.provider === "auto" && !computed.isComplete && providerOrder.includes("get")) {
                    warnings.push("Tingwu coverage is partial; fell back to get");
                    continue;
                }
                break;
            }
            catch (error) {
                const message = redactSensitiveText(error instanceof Error ? error.message : String(error));
                providerAttempts?.push({
                    provider: providerName,
                    status: "failed",
                    error: message,
                    startedAt,
                    finishedAt: new Date().toISOString()
                });
                if (providerName === "tingwu" && options.provider === "auto" && providerOrder.includes("get")) {
                    warnings.push("Tingwu failed; fell back to get");
                    continue;
                }
                throw error;
            }
        }
        if (!transcript || !selectedProvider) {
            throw new WorkflowStageError("provider-selection", new Error("no transcript provider produced usable output"), tempDir);
        }
        if (coverageStatus === "partial") {
            warnings.push(`Final transcript coverage is partial (${(coverage * 100).toFixed(2)}%)`);
        }
        const markdownTitle = options.outputTitle || transcript.title || media.pageTitle;
        const outputTitle = sanitizeFileName(markdownTitle);
        const outputPath = path.join(outputDir, `${outputTitle}.md`);
        const markdown = buildMarkdown({
            title: markdownTitle,
            sourceUrl: options.url,
            resourceId: options.resourceId,
            provider: selectedProvider,
            mediaDurationSec: downloaded.mediaDurationSec,
            coverage,
            generatedAt: new Date().toISOString(),
            chapters: transcript.chapters,
            segments: transcript.segments,
            rawText: transcript.rawText
        });
        await runStage("write-markdown", tempDir, async () => {
            await deps.writeMarkdownFile(outputPath, markdown);
        }, onStageChange);
        if (!options.keepArtifacts && tempDir) {
            await deps.cleanupArtifacts([...cleanupTargets, tempDir]);
        }
        return {
            outputPath,
            provider: selectedProvider,
            coverage,
            coverageStatus,
            warnings,
            providerAttempts,
            tempDir
        };
    }
    catch (error) {
        if (error instanceof WorkflowStageError) {
            throw error;
        }
        throw new WorkflowStageError("workflow", error, tempDir);
    }
}
