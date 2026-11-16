import { mkdtemp, mkdir, readFile, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";

import { describe, expect, test, vi } from "vitest";

import {
  looksLikeTingwuExtractedText,
  parseTingwuExtractedSegments,
  runTingwuExportToMarkdown
} from "../src/pipeline/tingwu-export.js";
import {
  looksLikeTingwuTranscriptStillProcessing,
  parseTingwuApiTranscriptResult,
  selectLikelyTingwuTranscriptBlocks
} from "../src/providers/tingwu-page.js";

describe("parseTingwuExtractedSegments", () => {
  test("parses speaker names, timestamps, and text blocks from extracted Tingwu content", () => {
    const segments = parseTingwuExtractedSegments([
      "发言人 1 00:01",
      "大家好，欢迎来到今天的内容。",
      "发言人 2 00:08",
      "这里是第二段。"
    ].join("\n"));

    expect(segments).toEqual([
      {
        speaker: "发言人 1",
        timestamp: "00:01",
        startSec: 1,
        text: "大家好，欢迎来到今天的内容。"
      },
      {
        speaker: "发言人 2",
        timestamp: "00:08",
        startSec: 8,
        text: "这里是第二段。"
      }
    ]);
  });

  test("parses timestamp-only headings followed by transcript paragraphs", () => {
    const segments = parseTingwuExtractedSegments([
      "00:25",
      "好，可以看见我电脑屏幕吗？可以的话扣一个。",
      "00:39",
      "好，咱们先开始。这一课是这六课当中最简单的一课。"
    ].join("\n"));

    expect(segments).toEqual([
      {
        timestamp: "00:25",
        startSec: 25,
        text: "好，可以看见我电脑屏幕吗？可以的话扣一个。"
      },
      {
        timestamp: "00:39",
        startSec: 39,
        text: "好，咱们先开始。这一课是这六课当中最简单的一课。"
      }
    ]);
  });

  test("parses multiline speaker labels and deduplicates repeated transcript blocks", () => {
    const segments = parseTingwuExtractedSegments([
      "发言人",
      "00:00",
      "第一段内容",
      "发言人",
      "00:30",
      "第二段内容",
      "发言人",
      "00:00",
      "第一段内容",
      "发言人",
      "00:30",
      "第二段内容"
    ].join("\n"));

    expect(segments).toEqual([
      {
        speaker: "发言人",
        timestamp: "00:00",
        startSec: 0,
        text: "第一段内容"
      },
      {
        speaker: "发言人",
        timestamp: "00:30",
        startSec: 30,
        text: "第二段内容"
      }
    ]);
  });
});

describe("looksLikeTingwuExtractedText", () => {
  test("accepts extracted transcript content without speaker labels when multiple timestamps exist", () => {
    const result = looksLikeTingwuExtractedText([
      "00:25",
      "第一段内容",
      "00:39",
      "第二段内容"
    ].join("\n"));

    expect(result).toBe(true);
  });
});

describe("looksLikeTingwuTranscriptStillProcessing", () => {
  test("detects Tingwu transcript pages that are still generating the full summary or transcript content", () => {
    const result = looksLikeTingwuTranscriptStillProcessing([
      "通义听悟",
      "已暂停",
      "全文概要处理中，请稍后查看...",
      "关键词",
      "发言总结"
    ].join("\n"));

    expect(result).toBe(true);
  });
});

describe("selectLikelyTingwuTranscriptBlocks", () => {
  test("prefers speaker/timestamp transcript blocks over summary containers", () => {
    const result = selectLikelyTingwuTranscriptBlocks([
      "关键词\n全文概要\n00:00\n维生素D：免疫调节与健康守护\n这是一段摘要",
      "发言人\n00:00\n嗨大家好，今天我想详细分享维生素D。",
      "发言人\n01:11\n首先它的第一个作用是针对免疫健康。"
    ]);

    expect(result).toContain("发言人\n00:00\n嗨大家好");
    expect(result).toContain("发言人\n01:11\n首先它的第一个作用");
    expect(result).not.toContain("关键词");
    expect(result).not.toContain("这是一段摘要");
  });
});

describe("parseTingwuApiTranscriptResult", () => {
  test("uses Tingwu API paragraphs instead of the visible virtualized DOM subset", () => {
    const result = parseTingwuApiTranscriptResult({
      duration: 6873,
      tag: { showName: "完整听悟逐字稿" },
      result: JSON.stringify({
        pg: [
          {
            ui: "1",
            sc: [
              { bt: 51990, et: 52551, tc: "真服了" },
              { bt: 52551, et: 52738, tc: "你" },
              { bt: 52738, et: 53112, tc: "这个" },
              { bt: 53112, et: 53490, tc: "老六。" }
            ]
          },
          {
            ui: "1",
            sc: [
              { bt: 6806540, et: 6806890, tc: "OK" },
              { bt: 6806890, et: 6807240, tc: "好了，" },
              { bt: 6807241, et: 6807940, tc: "基本上" }
            ]
          }
        ]
      })
    }, "");

    expect(result?.title).toBe("完整听悟逐字稿");
    expect(result?.transcriptDurationSec).toBe(6873);
    expect(result?.segments).toEqual([
      {
        timestamp: "00:51",
        startSec: 51,
        speaker: "发言人",
        text: "真服了你这个老六。"
      },
      {
        timestamp: "01:53:26",
        startSec: 6806,
        speaker: "发言人",
        text: "OK好了，基本上"
      }
    ]);
  });
});

describe("runTingwuExportToMarkdown", () => {
  test("writes markdown from an existing Tingwu transcript page without re-transcribing media", async () => {
    const outputDir = await mkdtemp(path.join(os.tmpdir(), "tingwu-export-out-"));
    const extracted = {
      provider: "tingwu" as const,
      title: "听悟现成逐字稿",
      transcriptDurationSec: 12,
      chapters: [{ title: "第一章", timestamp: "00:00", startSec: 0 }],
      segments: [
        { timestamp: "00:01", startSec: 1, speaker: "发言人 1", text: "第一句" },
        { timestamp: "00:12", startSec: 12, speaker: "发言人 2", text: "第二句" }
      ],
      rawText: "raw"
    };

    const extractTranscript = vi.fn().mockResolvedValue(extracted);

    const result = await runTingwuExportToMarkdown(
      {
        url: "https://tingwu.aliyun.com/doc/transcripts/abc123",
        outDir: outputDir,
        pageId: "opencli-page-abc"
      },
      {
        assertEnvironment: vi.fn().mockResolvedValue(undefined),
        extractTranscript,
        writeMarkdownFile: vi.fn(async (filePath, content) => {
          await mkdir(path.dirname(filePath), { recursive: true });
          await writeFile(filePath, content, "utf8");
        })
      }
    );

    expect(result.provider).toBe("tingwu");
    expect(result.coverage).toBe(1);
    expect(extractTranscript).toHaveBeenCalledWith(
      "https://tingwu.aliyun.com/doc/transcripts/abc123",
      undefined,
      "opencli-page-abc"
    );

    const markdown = await readFile(result.outputPath, "utf8");
    expect(markdown).toBe([
      "# 听悟现成逐字稿",
      "",
      "## Transcript",
      "",
      "[00:01] 第一句",
      "[00:12] 第二句",
      ""
    ].join("\n"));
    expect(markdown).not.toContain("source_url");
    expect(markdown).not.toContain("发言人");
  });

  test("omits raw transcript sections from normalized Tingwu export markdown", async () => {
    const outputDir = await mkdtemp(path.join(os.tmpdir(), "tingwu-export-out-"));
    const extracted = {
      provider: "tingwu" as const,
      title: "听悟现成逐字稿",
      transcriptDurationSec: 12,
      chapters: [],
      segments: [
        { timestamp: "00:01", startSec: 1, speaker: "发言人 1", text: "第一句" },
        { timestamp: "00:12", startSec: 12, speaker: "发言人 2", text: "第二句" }
      ],
      rawText: "这里是完整原文。".repeat(40)
    };

    const extractTranscript = vi.fn().mockResolvedValue(extracted);

    const result = await runTingwuExportToMarkdown(
      {
        url: "https://tingwu.aliyun.com/doc/transcripts/abc123",
        outDir: outputDir,
        pageId: "opencli-page-abc"
      },
      {
        assertEnvironment: vi.fn().mockResolvedValue(undefined),
        extractTranscript,
        writeMarkdownFile: vi.fn(async (filePath, content) => {
          await mkdir(path.dirname(filePath), { recursive: true });
          await writeFile(filePath, content, "utf8");
        })
      }
    );

    const markdown = await readFile(result.outputPath, "utf8");
    expect(markdown).toBe([
      "# 听悟现成逐字稿",
      "",
      "## Transcript",
      "",
      "[00:01] 第一句",
      "[00:12] 第二句",
      ""
    ].join("\n"));
    expect(markdown).not.toContain("## Raw Transcript");
    expect(markdown).not.toContain("这里是完整原文。");
  });
});
