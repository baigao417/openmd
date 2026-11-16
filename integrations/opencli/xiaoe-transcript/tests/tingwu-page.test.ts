import { describe, expect, test } from "vitest";

import { detectTingwuTranscriptPage } from "../src/app/tingwu-page.js";

describe("detectTingwuTranscriptPage", () => {
  test("recognizes Tingwu transcript pages", () => {
    const result = detectTingwuTranscriptPage({
      url: "https://tingwu.aliyun.com/doc/transcripts/abc123",
      title: "逐字稿标题"
    });

    expect(result.supported).toBe(true);
    expect(result.reason).toBeUndefined();
  });

  test("recognizes transcript pages without the doc prefix", () => {
    const result = detectTingwuTranscriptPage({
      url: "https://tingwu.aliyun.com/transcripts/xyz789",
      title: "逐字稿标题"
    });

    expect(result.supported).toBe(true);
    expect(result.reason).toBeUndefined();
  });

  test("rejects non transcript pages", () => {
    const result = detectTingwuTranscriptPage({
      url: "https://tingwu.aliyun.com/",
      title: "首页"
    });

    expect(result.supported).toBe(false);
    expect(result.reason).toContain("听悟逐字稿页面");
  });
});
