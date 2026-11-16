import { mkdtemp, mkdir, readFile, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { describe, expect, test, vi } from "vitest";
import { runMediaToMarkdown, runXiaoeToMarkdown } from "../src/workflow.js";
function createProvider(result) {
    return {
        transcribe: vi.fn().mockResolvedValue(result)
    };
}
describe("runXiaoeToMarkdown", () => {
    test("runs the generic media workflow for non-xiaoe urls", async () => {
        const outputDir = await mkdtemp(path.join(os.tmpdir(), "media-out-"));
        const tempDir = await mkdtemp(path.join(os.tmpdir(), "media-temp-"));
        const audioPath = path.join(tempDir, "audio.wav");
        await writeFile(audioPath, "audio");
        const deps = {
            assertEnvironment: vi.fn().mockResolvedValue(undefined),
            createTempDir: vi.fn().mockResolvedValue(tempDir),
            discoverMedia: vi.fn().mockResolvedValue({
                pageTitle: "受限视频标题",
                sourceUrl: "https://example.com/private/video",
                mediaUrl: "https://media.example.com/master.m3u8",
                extractor: "network"
            }),
            downloadMedia: vi.fn().mockResolvedValue({
                audioPath,
                originalPath: path.join(tempDir, "original.mp4"),
                mediaDurationSec: 75,
                cleanupPaths: [audioPath]
            }),
            providers: {
                tingwu: createProvider({
                    provider: "tingwu",
                    title: "通用媒体标题",
                    transcriptDurationSec: 75,
                    chapters: [{ title: "开场", timestamp: "00:00", startSec: 0 }],
                    segments: [{ timestamp: "00:05", startSec: 5, text: "第一段内容" }]
                }),
                get: createProvider({
                    provider: "get",
                    title: "unused",
                    transcriptDurationSec: 0,
                    chapters: [],
                    segments: []
                })
            },
            writeMarkdownFile: vi.fn(async (filePath, content) => {
                await mkdir(path.dirname(filePath), { recursive: true });
                await writeFile(filePath, content, "utf8");
            }),
            cleanupArtifacts: vi.fn(async () => undefined)
        };
        const result = await runMediaToMarkdown({
            url: "https://example.com/private/video",
            outDir: outputDir,
            provider: "auto",
            keepArtifacts: false,
            timeoutMinutes: 30
        }, deps);
        expect(result.provider).toBe("tingwu");
        expect(result.coverage).toBe(1);
        expect(deps.downloadMedia).toHaveBeenCalledWith({
            pageTitle: "受限视频标题",
            sourceUrl: "https://example.com/private/video",
            mediaUrl: "https://media.example.com/master.m3u8",
            extractor: "network"
        }, tempDir, 30, expect.any(Function));
        const markdown = await readFile(result.outputPath, "utf8");
        expect(markdown).toBe([
            "# 通用媒体标题",
            "",
            "## Transcript",
            "",
            "[00:05] 第一段内容",
            ""
        ].join("\n"));
        expect(markdown).not.toContain("source_url");
        expect(deps.cleanupArtifacts).toHaveBeenCalledWith([audioPath, tempDir]);
    });
    test("keeps Tingwu as the final provider in auto mode once Tingwu coverage is complete", async () => {
        const outputDir = await mkdtemp(path.join(os.tmpdir(), "xiaoe-out-"));
        const tempDir = await mkdtemp(path.join(os.tmpdir(), "xiaoe-temp-"));
        const audioPath = path.join(tempDir, "audio.wav");
        await writeFile(audioPath, "audio");
        const getTranscribe = vi.fn().mockResolvedValue({
            provider: "get",
            title: "课程标题",
            transcriptDurationSec: 100,
            chapters: [{ title: "章节一", timestamp: "00:00", startSec: 0 }],
            segments: [
                { timestamp: "00:00", startSec: 0, text: "完整内容" },
                { timestamp: "01:40", startSec: 100, text: "结束" }
            ]
        });
        const deps = {
            assertEnvironment: vi.fn().mockResolvedValue(undefined),
            createTempDir: vi.fn().mockResolvedValue(tempDir),
            discoverMedia: vi.fn().mockResolvedValue({
                pageTitle: '课程<>标题',
                sourceUrl: "https://example.com/course",
                mediaUrl: "https://media.example.com/master.m3u8",
                extractor: "dom"
            }),
            downloadMedia: vi.fn().mockResolvedValue({
                audioPath,
                originalPath: path.join(tempDir, "original.mp4"),
                mediaDurationSec: 100,
                cleanupPaths: [audioPath]
            }),
            providers: {
                tingwu: createProvider({
                    provider: "tingwu",
                    title: "课程标题",
                    transcriptDurationSec: 100,
                    chapters: [{ title: "章节一", timestamp: "00:00", startSec: 0 }],
                    segments: [
                        { timestamp: "00:00", startSec: 0, text: "完整内容" },
                        { timestamp: "01:40", startSec: 100, text: "结束" }
                    ]
                }),
                get: {
                    transcribe: getTranscribe
                }
            },
            writeMarkdownFile: vi.fn(async (filePath, content) => {
                await mkdir(path.dirname(filePath), { recursive: true });
                await writeFile(filePath, content, "utf8");
            }),
            cleanupArtifacts: vi.fn(async (paths) => {
                for (const item of paths) {
                    await writeFile(item + ".cleaned", "1");
                }
            })
        };
        const result = await runXiaoeToMarkdown({
            url: "https://example.com/course",
            outDir: outputDir,
            provider: "auto",
            keepArtifacts: false,
            timeoutMinutes: 30
        }, deps);
        expect(result.provider).toBe("tingwu");
        expect(path.basename(result.outputPath)).toBe("课程标题.md");
        expect(getTranscribe).not.toHaveBeenCalled();
        const markdown = await readFile(result.outputPath, "utf8");
        expect(markdown).toBe([
            "# 课程标题",
            "",
            "## Transcript",
            "",
            "[00:00] 完整内容",
            "[01:40] 结束",
            ""
        ].join("\n"));
        expect(markdown).not.toContain('provider: "tingwu"');
    });
    test("uses explicit output title and resource id in markdown", async () => {
        const outputDir = await mkdtemp(path.join(os.tmpdir(), "xiaoe-out-"));
        const tempDir = await mkdtemp(path.join(os.tmpdir(), "xiaoe-temp-"));
        const audioPath = path.join(tempDir, "audio.wav");
        await writeFile(audioPath, "audio");
        const deps = {
            assertEnvironment: vi.fn().mockResolvedValue(undefined),
            createTempDir: vi.fn().mockResolvedValue(tempDir),
            discoverMedia: vi.fn().mockResolvedValue({
                pageTitle: "页面标题",
                sourceUrl: "https://example.com/course",
                mediaUrl: "https://media.example.com/master.m3u8",
                extractor: "dom"
            }),
            downloadMedia: vi.fn().mockResolvedValue({
                audioPath,
                originalPath: path.join(tempDir, "original.mp4"),
                mediaDurationSec: 100,
                cleanupPaths: [audioPath]
            }),
            providers: {
                tingwu: createProvider({
                    provider: "tingwu",
                    title: "Provider 标题",
                    transcriptDurationSec: 100,
                    chapters: [],
                    segments: [{ timestamp: "00:01", startSec: 1, text: "正文" }]
                }),
                get: createProvider({
                    provider: "get",
                    title: "unused",
                    transcriptDurationSec: 0,
                    chapters: [],
                    segments: []
                })
            },
            writeMarkdownFile: vi.fn(async (filePath, content) => {
                await mkdir(path.dirname(filePath), { recursive: true });
                await writeFile(filePath, content, "utf8");
            }),
            cleanupArtifacts: vi.fn(async () => undefined)
        };
        const result = await runXiaoeToMarkdown({
            url: "https://example.com/course",
            outDir: outputDir,
            provider: "auto",
            keepArtifacts: false,
            timeoutMinutes: 30,
            outputTitle: "目录标题",
            resourceId: "v_resource_1"
        }, deps);
        expect(path.basename(result.outputPath)).toBe("目录标题.md");
        const markdown = await readFile(result.outputPath, "utf8");
        expect(markdown).toBe([
            "# 目录标题",
            "",
            "## Transcript",
            "",
            "[00:01] 正文",
            ""
        ].join("\n"));
        expect(markdown).not.toContain("resource_id");
        expect(markdown).not.toContain("Resource ID");
    });
    test("keeps Tingwu as the final provider when full raw transcript text is substantial", async () => {
        const outputDir = await mkdtemp(path.join(os.tmpdir(), "xiaoe-out-"));
        const tempDir = await mkdtemp(path.join(os.tmpdir(), "xiaoe-temp-"));
        const audioPath = path.join(tempDir, "audio.wav");
        await writeFile(audioPath, "audio");
        const getTranscribe = vi.fn().mockResolvedValue({
            provider: "get",
            title: "课程标题",
            transcriptDurationSec: 1000,
            chapters: [],
            segments: [{ timestamp: "16:40", startSec: 1000, text: "不应被调用" }]
        });
        const deps = {
            assertEnvironment: vi.fn().mockResolvedValue(undefined),
            createTempDir: vi.fn().mockResolvedValue(tempDir),
            discoverMedia: vi.fn().mockResolvedValue({
                pageTitle: "课程标题",
                sourceUrl: "https://example.com/course",
                mediaUrl: "https://media.example.com/master.m3u8",
                extractor: "dom"
            }),
            downloadMedia: vi.fn().mockResolvedValue({
                audioPath,
                originalPath: path.join(tempDir, "original.mp4"),
                mediaDurationSec: 1000,
                cleanupPaths: [audioPath]
            }),
            providers: {
                tingwu: createProvider({
                    provider: "tingwu",
                    title: "课程标题",
                    transcriptDurationSec: 137,
                    chapters: [],
                    segments: [{ timestamp: "02:17", startSec: 137, text: "前面几分钟" }],
                    rawText: "完整逐字稿".repeat(500)
                }),
                get: {
                    transcribe: getTranscribe
                }
            },
            writeMarkdownFile: vi.fn(async (filePath, content) => {
                await mkdir(path.dirname(filePath), { recursive: true });
                await writeFile(filePath, content, "utf8");
            }),
            cleanupArtifacts: vi.fn(async () => undefined)
        };
        const result = await runXiaoeToMarkdown({
            url: "https://example.com/course",
            outDir: outputDir,
            provider: "auto",
            keepArtifacts: false,
            timeoutMinutes: 30
        }, deps);
        expect(result.provider).toBe("tingwu");
        expect(result.coverage).toBe(1);
        expect(getTranscribe).not.toHaveBeenCalled();
        const markdown = await readFile(result.outputPath, "utf8");
        expect(markdown).toBe([
            "# 课程标题",
            "",
            "## Transcript",
            "",
            "[02:17] 前面几分钟",
            ""
        ].join("\n"));
        expect(markdown).not.toContain('provider: "tingwu"');
        expect(markdown).not.toContain("## Raw Transcript");
    });
    test("falls back to get in auto mode when Tingwu coverage is still incomplete", async () => {
        const outputDir = await mkdtemp(path.join(os.tmpdir(), "xiaoe-out-"));
        const tempDir = await mkdtemp(path.join(os.tmpdir(), "xiaoe-temp-"));
        const audioPath = path.join(tempDir, "audio.wav");
        await writeFile(audioPath, "audio");
        const getTranscribe = vi.fn().mockResolvedValue({
            provider: "get",
            title: "课程标题",
            transcriptDurationSec: 600,
            chapters: [{ title: "完整章节", timestamp: "00:00", startSec: 0 }],
            segments: [
                { timestamp: "00:00", startSec: 0, text: "完整内容" },
                { timestamp: "10:00", startSec: 600, text: "结束" }
            ]
        });
        const deps = {
            assertEnvironment: vi.fn().mockResolvedValue(undefined),
            createTempDir: vi.fn().mockResolvedValue(tempDir),
            discoverMedia: vi.fn().mockResolvedValue({
                pageTitle: "课程标题",
                sourceUrl: "https://example.com/course",
                mediaUrl: "https://media.example.com/master.m3u8",
                extractor: "dom"
            }),
            downloadMedia: vi.fn().mockResolvedValue({
                audioPath,
                originalPath: path.join(tempDir, "original.mp4"),
                mediaDurationSec: 600,
                cleanupPaths: [audioPath]
            }),
            providers: {
                tingwu: createProvider({
                    provider: "tingwu",
                    title: "课程标题",
                    transcriptDurationSec: 29,
                    chapters: [],
                    segments: [{ timestamp: "00:29", startSec: 29, text: "只拿到一点点" }],
                    rawText: "只拿到一点点"
                }),
                get: {
                    transcribe: getTranscribe
                }
            },
            writeMarkdownFile: vi.fn(async (filePath, content) => {
                await mkdir(path.dirname(filePath), { recursive: true });
                await writeFile(filePath, content, "utf8");
            }),
            cleanupArtifacts: vi.fn(async () => undefined)
        };
        const result = await runXiaoeToMarkdown({
            url: "https://example.com/course",
            outDir: outputDir,
            provider: "auto",
            keepArtifacts: false,
            timeoutMinutes: 30
        }, deps);
        expect(result.provider).toBe("get");
        expect(getTranscribe).toHaveBeenCalledTimes(1);
        const markdown = await readFile(result.outputPath, "utf8");
        expect(markdown).toBe([
            "# 课程标题",
            "",
            "## Transcript",
            "",
            "[00:00] 完整内容",
            "[10:00] 结束",
            ""
        ].join("\n"));
        expect(markdown).not.toContain('provider: "get"');
    });
    test("falls back to Get in auto mode when Tingwu throws", async () => {
        const outputDir = await mkdtemp(path.join(os.tmpdir(), "xiaoe-out-"));
        const tempDir = await mkdtemp(path.join(os.tmpdir(), "xiaoe-temp-"));
        const audioPath = path.join(tempDir, "audio.wav");
        await writeFile(audioPath, "audio");
        const getTranscribe = vi.fn().mockResolvedValue({
            provider: "get",
            title: "课程标题",
            transcriptDurationSec: 100,
            chapters: [],
            segments: [{ timestamp: "01:40", startSec: 100, text: "Get 完整内容" }]
        });
        const deps = {
            assertEnvironment: vi.fn().mockResolvedValue(undefined),
            createTempDir: vi.fn().mockResolvedValue(tempDir),
            discoverMedia: vi.fn().mockResolvedValue({
                pageTitle: "课程标题",
                sourceUrl: "https://example.com/course",
                mediaUrl: "https://media.example.com/master.m3u8",
                extractor: "dom"
            }),
            downloadMedia: vi.fn().mockResolvedValue({
                audioPath,
                originalPath: path.join(tempDir, "original.mp4"),
                mediaDurationSec: 100,
                cleanupPaths: [audioPath]
            }),
            providers: {
                tingwu: { transcribe: vi.fn().mockRejectedValue(new Error("Tingwu upload failed token=secret")) },
                get: { transcribe: getTranscribe }
            },
            writeMarkdownFile: vi.fn(async (filePath, content) => {
                await mkdir(path.dirname(filePath), { recursive: true });
                await writeFile(filePath, content, "utf8");
            }),
            cleanupArtifacts: vi.fn(async () => undefined)
        };
        const result = await runXiaoeToMarkdown({
            url: "https://example.com/course",
            outDir: outputDir,
            provider: "auto",
            keepArtifacts: false,
            timeoutMinutes: 30
        }, deps);
        expect(result.provider).toBe("get");
        expect(getTranscribe).toHaveBeenCalledTimes(1);
        expect(result.providerAttempts?.map((attempt) => attempt.provider)).toEqual(["tingwu", "get"]);
        expect(result.providerAttempts?.[0].status).toBe("failed");
        expect(result.providerAttempts?.[0].error).toContain("token=[REDACTED]");
        expect(result.warnings?.join("\n")).toContain("Tingwu failed; fell back to get");
    });
    test("marks final low coverage output as partial with a warning", async () => {
        const outputDir = await mkdtemp(path.join(os.tmpdir(), "xiaoe-out-"));
        const tempDir = await mkdtemp(path.join(os.tmpdir(), "xiaoe-temp-"));
        const audioPath = path.join(tempDir, "audio.wav");
        await writeFile(audioPath, "audio");
        const deps = {
            assertEnvironment: vi.fn().mockResolvedValue(undefined),
            createTempDir: vi.fn().mockResolvedValue(tempDir),
            discoverMedia: vi.fn().mockResolvedValue({
                pageTitle: "课程标题",
                sourceUrl: "https://example.com/course",
                mediaUrl: "https://media.example.com/master.m3u8",
                extractor: "dom"
            }),
            downloadMedia: vi.fn().mockResolvedValue({
                audioPath,
                originalPath: path.join(tempDir, "original.mp4"),
                mediaDurationSec: 1000,
                cleanupPaths: [audioPath]
            }),
            providers: {
                tingwu: createProvider({
                    provider: "tingwu",
                    title: "课程标题",
                    transcriptDurationSec: 100,
                    chapters: [],
                    segments: [{ timestamp: "01:40", startSec: 100, text: "不完整内容" }]
                }),
                get: createProvider({
                    provider: "get",
                    title: "课程标题",
                    transcriptDurationSec: 120,
                    chapters: [],
                    segments: [{ timestamp: "02:00", startSec: 120, text: "仍不完整" }]
                })
            },
            writeMarkdownFile: vi.fn(async (filePath, content) => {
                await mkdir(path.dirname(filePath), { recursive: true });
                await writeFile(filePath, content, "utf8");
            }),
            cleanupArtifacts: vi.fn(async () => undefined)
        };
        const result = await runXiaoeToMarkdown({
            url: "https://example.com/course",
            outDir: outputDir,
            provider: "auto",
            keepArtifacts: false,
            timeoutMinutes: 30
        }, deps);
        expect(result.provider).toBe("get");
        expect(result.coverageStatus).toBe("partial");
        expect(result.warnings?.join("\n")).toContain("coverage is partial");
        expect(result.providerAttempts?.map((attempt) => attempt.coverage)).toEqual([0.1, 0.12]);
    });
    test("preserves temp artifacts when the workflow fails", async () => {
        const outputDir = await mkdtemp(path.join(os.tmpdir(), "xiaoe-out-"));
        const tempDir = await mkdtemp(path.join(os.tmpdir(), "xiaoe-temp-"));
        const deps = {
            assertEnvironment: vi.fn().mockResolvedValue(undefined),
            createTempDir: vi.fn().mockResolvedValue(tempDir),
            discoverMedia: vi.fn().mockRejectedValue(new Error("extension not connected")),
            downloadMedia: vi.fn(),
            providers: {
                tingwu: createProvider({
                    provider: "tingwu",
                    title: "unused",
                    transcriptDurationSec: 0,
                    chapters: [],
                    segments: []
                }),
                get: createProvider({
                    provider: "get",
                    title: "unused",
                    transcriptDurationSec: 0,
                    chapters: [],
                    segments: []
                })
            },
            writeMarkdownFile: vi.fn(),
            cleanupArtifacts: vi.fn()
        };
        await expect(runXiaoeToMarkdown({
            url: "https://example.com/course",
            outDir: outputDir,
            provider: "auto",
            keepArtifacts: false,
            timeoutMinutes: 30
        }, deps)).rejects.toThrow(/failed at discover-media: extension not connected/);
        expect(deps.cleanupArtifacts).not.toHaveBeenCalled();
    });
});
