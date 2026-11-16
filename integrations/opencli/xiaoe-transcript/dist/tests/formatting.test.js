import { describe, expect, test } from "vitest";
import { computeCoverage } from "../src/pipeline/coverage.js";
import { buildMarkdown } from "../src/pipeline/markdown.js";
import { formatStageError } from "../src/utils/errors.js";
import { sanitizeFileName } from "../src/utils/files.js";
describe("sanitizeFileName", () => {
    test("removes Windows-illegal characters and collapses whitespace", () => {
        expect(sanitizeFileName('  小鹅通:课程<>?*"  第一课  ')).toBe("小鹅通-课程 第一课");
    });
});
describe("computeCoverage", () => {
    test("marks transcript incomplete below 85 percent", () => {
        expect(computeCoverage(84, 100)).toEqual({
            coverage: 0.84,
            isComplete: false
        });
    });
    test("marks transcript complete at or above 85 percent", () => {
        expect(computeCoverage(85, 100)).toEqual({
            coverage: 0.85,
            isComplete: true
        });
    });
});
describe("buildMarkdown", () => {
    test("renders only title and transcript lines in the normalized transcript format", () => {
        const markdown = buildMarkdown({
            title: "课程标题",
            sourceUrl: "https://example.com/course",
            provider: "tingwu",
            mediaDurationSec: 120,
            coverage: 0.95,
            generatedAt: "2026-04-03T10:00:00.000Z",
            chapters: [{ title: "第一章", timestamp: "00:00", startSec: 0 }],
            segments: [
                { timestamp: "00:00", startSec: 0, speaker: "发言人", text: "大家好" },
                { timestamp: "00:10", startSec: 10, speaker: "发言人 2", text: "欢迎来到课程" }
            ]
        });
        expect(markdown).toBe([
            "# 课程标题",
            "",
            "## Transcript",
            "",
            "[00:00] 大家好",
            "[00:10] 欢迎来到课程",
            ""
        ].join("\n"));
        expect(markdown).not.toContain("---");
        expect(markdown).not.toContain("## Source");
        expect(markdown).not.toContain("## Metadata");
        expect(markdown).not.toContain("## Chapters");
        expect(markdown).not.toContain("发言人:");
    });
    test("does not append raw transcript sections to normalized transcript markdown", () => {
        const markdown = buildMarkdown({
            title: "课程标题",
            sourceUrl: "https://example.com/course",
            provider: "tingwu",
            mediaDurationSec: 120,
            coverage: 0.95,
            generatedAt: "2026-04-03T10:00:00.000Z",
            chapters: [],
            segments: [
                { timestamp: "00:00", startSec: 0, text: "前面几句" }
            ],
            rawText: "这是完整正文第一段。\n这是完整正文第二段。"
        });
        expect(markdown).toBe([
            "# 课程标题",
            "",
            "## Transcript",
            "",
            "[00:00] 前面几句",
            ""
        ].join("\n"));
        expect(markdown).not.toContain("## Raw Transcript");
        expect(markdown).not.toContain("这是完整正文第一段。");
    });
});
describe("formatStageError", () => {
    test("includes stage, error, and temp directory hint", () => {
        expect(formatStageError("download", new Error("network unavailable"), "E:/tmp/job-1")).toContain("failed at download: network unavailable");
    });
});
