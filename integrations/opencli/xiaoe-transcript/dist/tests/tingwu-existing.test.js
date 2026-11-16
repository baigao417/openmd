import { mkdir, mkdtemp, readFile, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { describe, expect, test, vi } from "vitest";
import { buildTingwuTitleCandidates, runTingwuExistingTranscriptToMarkdown, titleMatchesTingwuCandidate } from "../src/pipeline/tingwu-existing.js";
describe("Tingwu existing transcript reuse", () => {
    test("builds title candidates compatible with previous Tingwu upload names", () => {
        const title = "第 01 讲：小鹅通/直播？转写*测试".repeat(3);
        const candidates = buildTingwuTitleCandidates(title);
        expect(candidates[0]).toBe(title);
        expect(candidates[1]).not.toContain("/");
        expect(candidates[1]).not.toContain("*");
        expect(candidates.some((candidate) => candidate.length === 48)).toBe(true);
    });
    test("matches visible Tingwu titles against the original catalog title", () => {
        expect(titleMatchesTingwuCandidate("第01讲小鹅通直播转写测试.mp3", "第 01 讲：小鹅通/直播？转写*测试")).toBe(true);
        expect(titleMatchesTingwuCandidate("2025", "2025.11.23 关于平庸让人细思极恐的认知…")).toBe(false);
        expect(titleMatchesTingwuCandidate("2025.11.16 竟然没人从这个角度给省钱建议", "2025.11.23 关于平庸让人细思极恐的认知…")).toBe(false);
        expect(titleMatchesTingwuCandidate("2025.11.23 关于平庸让人细思极恐的认知", "2025.11.23 关于平庸让人细思极恐的认知…")).toBe(true);
        expect(titleMatchesTingwuCandidate("完全不同的标题", "第 01 讲：小鹅通/直播？转写*测试")).toBe(false);
    });
    test("returns undefined when no matching Tingwu transcript exists", async () => {
        const outDir = await mkdtemp(path.join(os.tmpdir(), "tingwu-existing-none-"));
        const result = await runTingwuExistingTranscriptToMarkdown({
            title: "不存在的标题",
            outDir
        }, {
            assertEnvironment: vi.fn().mockResolvedValue(undefined),
            findExistingTranscript: vi.fn().mockResolvedValue(undefined),
            writeMarkdownFile: vi.fn(),
            now: () => new Date("2026-05-30T00:00:00.000Z")
        });
        expect(result).toBeUndefined();
    });
    test("writes markdown from a matching existing Tingwu transcript without media download", async () => {
        const outDir = await mkdtemp(path.join(os.tmpdir(), "tingwu-existing-hit-"));
        const result = await runTingwuExistingTranscriptToMarkdown({
            title: "小鹅通目录标题",
            preferredTitle: "小鹅通目录标题",
            sourceUrl: "https://app.example.com/p/course/video/v_1",
            resourceId: "v_1",
            outDir
        }, {
            assertEnvironment: vi.fn().mockResolvedValue(undefined),
            findExistingTranscript: vi.fn().mockResolvedValue({
                url: "https://tingwu.aliyun.com/doc/transcripts/abc123",
                transcript: {
                    provider: "tingwu",
                    title: "听悟里的标题",
                    transcriptDurationSec: 12,
                    chapters: [],
                    segments: [
                        { timestamp: "00:01", startSec: 1, speaker: "发言人", text: "已有逐字稿内容" }
                    ],
                    rawText: "已有逐字稿内容"
                }
            }),
            writeMarkdownFile: vi.fn(async (filePath, content) => {
                await mkdir(path.dirname(filePath), { recursive: true });
                await writeFile(filePath, content, "utf8");
            }),
            now: () => new Date("2026-05-30T00:00:00.000Z")
        });
        expect(result?.provider).toBe("tingwu");
        expect(result?.coverageStatus).toBe("complete");
        expect(result?.outputPath).toBe(path.join(outDir, "小鹅通目录标题.md"));
        expect(result?.warnings?.[0]).toContain("Reused existing Tingwu transcript");
        const markdown = await readFile(result.outputPath, "utf8");
        expect(markdown).toBe([
            "# 小鹅通目录标题",
            "",
            "## Transcript",
            "",
            "[00:01] 已有逐字稿内容",
            ""
        ].join("\n"));
        expect(markdown).not.toContain("source_url");
        expect(markdown).not.toContain("resource_id");
        expect(markdown).not.toContain("发言人:");
    });
});
