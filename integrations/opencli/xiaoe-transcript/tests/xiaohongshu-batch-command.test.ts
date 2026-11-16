import { describe, expect, test, vi } from "vitest";

import { runXiaohongshuBatchCommand } from "../src/app/xiaohongshu-batch-command.js";

describe("runXiaohongshuBatchCommand", () => {
  test("passes explicit --out through to the batch runner and falls back to the default path otherwise", async () => {
    const runXiaohongshuBatch = vi.fn().mockResolvedValue({
      creatorDir: "E:/custom/out/不甜的小七_9477032739",
      items: []
    });

    await runXiaohongshuBatchCommand({
      "profile-url": "https://www.xiaohongshu.com/user/profile/demo",
      "creator-name": "不甜的小七",
      "creator-handle": "9477032739",
      out: "E:/custom/out",
      limit: "3",
      provider: "tingwu"
    }, { runXiaohongshuBatch });

    expect(runXiaohongshuBatch).toHaveBeenCalledWith(
      expect.objectContaining({ outDir: "E:/custom/out", limit: 3 }),
      undefined
    );
  });
});
