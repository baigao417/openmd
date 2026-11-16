import { describe, expect, test } from "vitest";

import { mergeTranscriptParts } from "../src/providers/transcript-merge.js";

describe("mergeTranscriptParts", () => {
  test("offsets timestamps and joins raw text across parts", () => {
    const merged = mergeTranscriptParts("课程", [
      {
        offsetSec: 0,
        result: {
          provider: "get",
          title: "part-1",
          transcriptDurationSec: 40,
          chapters: [{ title: "开始", timestamp: "00:00", startSec: 0 }],
          segments: [{ timestamp: "00:05", startSec: 5, text: "第一段" }],
          rawText: "A"
        }
      },
      {
        offsetSec: 300,
        result: {
          provider: "get",
          title: "part-2",
          transcriptDurationSec: 50,
          chapters: [{ title: "继续", timestamp: "00:10", startSec: 10 }],
          segments: [{ timestamp: "00:20", startSec: 20, text: "第二段" }],
          rawText: "B"
        }
      }
    ]);

    expect(merged.title).toBe("课程");
    expect(merged.transcriptDurationSec).toBe(350);
    expect(merged.chapters[1]).toEqual({
      title: "继续",
      timestamp: "05:10",
      startSec: 310
    });
    expect(merged.segments[1]).toEqual({
      timestamp: "05:20",
      startSec: 320,
      text: "第二段"
    });
    expect(merged.rawText).toContain("## Part 1");
    expect(merged.rawText).toContain("## Part 2");
  });
});
