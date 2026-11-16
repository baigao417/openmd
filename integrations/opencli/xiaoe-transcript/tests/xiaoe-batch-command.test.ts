import { describe, expect, test, vi } from "vitest";

import {
  parseBoolean,
  parseResourceIds,
  parseStringList,
  parseXiaoeBatchCommandOptions,
  runXiaoeBatchTranscriptCommand
} from "../src/app/xiaoe-batch-command.js";

describe("xiaoe batch transcript command", () => {
  test("parses boolean and comma-separated resource ids safely", () => {
    expect(parseBoolean("false", true)).toBe(false);
    expect(parseBoolean("true", false)).toBe(true);
    expect(parseBoolean(undefined, true)).toBe(true);
    expect(parseResourceIds("a_1, a_2,,")).toEqual(["a_1", "a_2"]);
    expect(parseResourceIds(["a_3", " a_4 "])).toEqual(["a_3", "a_4"]);
    expect(parseStringList("https://tingwu.aliyun.com/folders/1; https://tingwu.aliyun.com/home"))
      .toEqual(["https://tingwu.aliyun.com/folders/1", "https://tingwu.aliyun.com/home"]);
  });

  test("maps OpenCLI kwargs to typed batch options", async () => {
    const options = await parseXiaoeBatchCommandOptions({
      "course-url": " https://appabc.h5.xiaoecloud.com/p/course/abc ",
      out: "C:\\example\\output\\前哨战",
      provider: "get",
      status: "100%,未学",
      type: "直播",
      chapter: "第一章",
      title: "复盘",
      "resource-id": "a_1,a_2",
      limit: "3",
      "skip-existing": "false",
      resume: "false",
      restart: "true",
      "dry-run": "true",
      "timeout-minutes": "45",
      "reuse-tingwu-existing": "false",
      "tingwu-existing-only": "true",
      "tingwu-existing-search-url": "https://tingwu.aliyun.com/folders/256466"
    });

    expect(options).toEqual({
      courseUrl: "https://appabc.h5.xiaoecloud.com/p/course/abc",
      outDir: "C:\\example\\output\\前哨战",
      provider: "get",
      status: "100%,未学",
      type: "直播",
      chapter: "第一章",
      title: "复盘",
      resourceIds: ["a_1", "a_2"],
      limit: 3,
      skipExisting: false,
      resume: false,
      restart: true,
      dryRun: true,
      timeoutMinutes: 45,
      reuseTingwuExisting: false,
      tingwuExistingOnly: true,
      tingwuExistingSearchUrls: ["https://tingwu.aliyun.com/folders/256466"]
    });
  });

  test("returns the summary row expected by OpenCLI", async () => {
    const runBatch = vi.fn().mockResolvedValue({
      outDir: "C:\\example\\output\\前哨战",
      total: 3,
      success: 1,
      skipped: 1,
      failed: 1,
      statePath: "C:\\example\\output\\前哨战\\.xiaoe-batch\\batch-state.json",
      items: [{ resourceId: "a_1", title: "第一讲", url: "https://x.test", status: "pending" }]
    });

    const rows = await runXiaoeBatchTranscriptCommand({
      "course-url": "https://appabc.h5.xiaoecloud.com/p/course/abc",
      out: "C:\\example\\output\\前哨战",
      "dry-run": true
    }, { runBatch });

    expect(runBatch).toHaveBeenCalledWith(expect.objectContaining({
      courseUrl: "https://appabc.h5.xiaoecloud.com/p/course/abc",
      outDir: "C:\\example\\output\\前哨战",
      dryRun: true,
      skipExisting: true,
      resume: true,
      timeoutMinutes: 120,
      reuseTingwuExisting: true,
      tingwuExistingOnly: false,
      tingwuExistingSearchUrls: undefined
    }));
    expect(rows).toEqual([{
      outDir: "C:\\example\\output\\前哨战",
      total: 3,
      success: 1,
      skipped: 1,
      failed: 1,
      statePath: "C:\\example\\output\\前哨战\\.xiaoe-batch\\batch-state.json",
      items: [{ resourceId: "a_1", title: "第一讲", url: "https://x.test", status: "pending" }]
    }]);
  });
});
