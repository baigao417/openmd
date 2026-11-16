import { afterEach, describe, expect, test, vi } from "vitest";

import { createGetWorkbenchServer } from "../src/app/get-workbench-service.js";

const closers: Array<() => Promise<void>> = [];

afterEach(async () => {
  while (closers.length > 0) {
    await closers.pop()?.();
  }
});

describe("get workbench server", () => {
  test("exposes health, status, and subscriptions endpoints", async () => {
    const server = await createGetWorkbenchServer({
      port: 0,
      getHelperStatus: async () => ({
        ok: true,
        config: { defaultOutputDir: "C:/example/output/抖音博主" },
        state: { status: "idle", total: 0, successCount: 0, skippedCount: 0, failedCount: 0 }
      }),
      listSubscriptions: async () => [{
        creatorId: "vnd1y25Y",
        creatorName: "冤种小徐",
        subjectUrl: "https://www.biji.com/subject/vnd1y25Y/DEFAULT"
      }],
      runExport: async () => ({ accepted: true }),
      startHelper: async () => ({ started: true }),
      stopHelper: async () => ({ stopped: true })
    });
    closers.push(async () => server.close());

    const health = await fetch(`${server.baseUrl}/api/health`).then((res) => res.json());
    const status = await fetch(`${server.baseUrl}/api/status`).then((res) => res.json());
    const subscriptions = await fetch(`${server.baseUrl}/api/subscriptions`).then((res) => res.json());

    expect(health.ok).toBe(true);
    expect(status.config.defaultOutputDir).toBe("C:/example/output/抖音博主");
    expect(subscriptions.items).toHaveLength(1);
  });

  test("starts helper on demand and forwards export requests", async () => {
    const startHelper = vi.fn().mockResolvedValue({ started: true });
    const runExport = vi.fn().mockResolvedValue({ accepted: true });

    const server = await createGetWorkbenchServer({
      port: 0,
      getHelperStatus: async () => null,
      listSubscriptions: async () => [],
      runExport,
      startHelper,
      stopHelper: async () => ({ stopped: true })
    });
    closers.push(async () => server.close());

    const startResult = await fetch(`${server.baseUrl}/api/helper/start`, {
      method: "POST"
    }).then((res) => res.json());

    const exportResult = await fetch(`${server.baseUrl}/api/export`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        subjectUrl: "https://www.biji.com/subject/vnd1y25Y/DEFAULT",
        startDate: "2026-04-01",
        endDate: "2026-04-08",
        skipExisting: true
      })
    }).then((res) => res.json());

    expect(startResult.started).toBe(true);
    expect(exportResult.accepted).toBe(true);
    expect(startHelper).toHaveBeenCalledTimes(1);
    expect(runExport).toHaveBeenCalledWith(expect.objectContaining({
      subjectUrl: "https://www.biji.com/subject/vnd1y25Y/DEFAULT",
      startDate: "2026-04-01",
      endDate: "2026-04-08",
      skipExisting: true
    }));
  });
});
