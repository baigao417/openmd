import { describe, expect, test } from "vitest";
import { buildTranscriptOnlyMarkdown } from "../src/pipeline/xiaohongshu-batch.js";
describe("buildTranscriptOnlyMarkdown", () => {
    test("renders only title and transcript lines without extra metadata sections", () => {
        const markdown = buildTranscriptOnlyMarkdown({
            title: "视频标题",
            segments: [
                { timestamp: "00:00", startSec: 0, text: "第一段" },
                { timestamp: "00:10", startSec: 10, speaker: "嘉宾", text: "第二段" }
            ]
        });
        expect(markdown).toContain("# 视频标题");
        expect(markdown).toContain("[00:00] 第一段");
        expect(markdown).toContain("[00:10] 嘉宾: 第二段");
        expect(markdown).not.toContain("---");
        expect(markdown).not.toContain("## Source");
        expect(markdown).not.toContain("## Metadata");
    });
});
