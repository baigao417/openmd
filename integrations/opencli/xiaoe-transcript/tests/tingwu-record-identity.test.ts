import { describe, expect, test, vi } from "vitest";

import type { OpenCliPage } from "../src/opencli/client.js";
import { assertTingwuRecordIdentity, collectTingwuTranscriptResult, matchesTingwuRecordTitle } from "../src/providers/tingwu-page.js";

describe("Tingwu record identity", () => {
  const expected = "「双语之路直播 vol.016」重点问题解答 part.16";
  const previous = "「双语之路直播 vol.015」重点问题解答 part.15";

  test("accepts the same uploaded title with an audio suffix", () => {
    expect(matchesTingwuRecordTitle(`${expected}.mp3`, expected)).toBe(true);
    expect(matchesTingwuRecordTitle(previous, expected)).toBe(false);
  });

  test("rejects a previous record even when its transcript page is ready", async () => {
    const page = {
      evaluate: vi.fn(async () => ({
        url: "https://tingwu.aliyun.com/doc/transcripts/old-record",
        title: previous
      }))
    } as unknown as OpenCliPage;

    await expect(assertTingwuRecordIdentity(page, expected, 20)).rejects.toThrow("record identity mismatch");
  });

  test("accepts the requested record after a page transition", async () => {
    const page = {
      evaluate: vi.fn()
        .mockResolvedValueOnce({ url: "https://tingwu.aliyun.com/home", title: "首页" })
        .mockResolvedValue({ url: "https://tingwu.aliyun.com/doc/transcripts/new-record", title: expected })
    } as unknown as OpenCliPage;

    await expect(assertTingwuRecordIdentity(page, expected, 1000)).resolves.toBeUndefined();
  });

  test("rejects an API transcript for a different record than the page", async () => {
    const page = {
      evaluate: vi.fn(async (code: string) => code.includes("getTransResult")
        ? JSON.stringify({ code: "0", data: { tag: { showName: previous }, result: "{}" } })
        : expected)
    } as unknown as OpenCliPage;

    await expect(collectTingwuTranscriptResult(page, expected)).rejects.toThrow("API transcript title mismatch");
  });
});
