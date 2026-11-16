import { defaultOutputDirectory } from "../src/utils/output-directory.js";
import { mkdtemp } from "node:fs/promises";
import os from "node:os";
import path from "node:path";

import { afterEach, describe, expect, test } from "vitest";

import { createGetHelperServer } from "../src/app/get-helper-service.js";

const closeTasks: Array<() => Promise<void>> = [];

afterEach(async () => {
  while (closeTasks.length > 0) {
    const task = closeTasks.pop();
    if (task) {
      await task();
    }
  }
});

describe("get helper service", () => {
  test("exposes get export config and persists updates", async () => {
    const rootDir = await mkdtemp(path.join(os.tmpdir(), "get-helper-"));
    const helper = await createGetHelperServer({
      rootDir,
      port: 0,
      listSubscriptions: async () => [],
      runExport: async () => ({
        creatorDir: "C:\\example\\output\\抖音博主\\冤种小徐_vnd1y25Y",
        creatorId: "vnd1y25Y",
        creatorName: "冤种小徐",
        total: 1,
        success: 1,
        skipped: 0,
        failed: 0,
        items: []
      })
    });
    closeTasks.push(async () => {
      await helper.close();
    });

    const statusBefore = await fetch(`${helper.baseUrl}/status`).then((response) => response.json());
    expect(statusBefore.config.defaultOutputDir).toBe(defaultOutputDirectory());

    const configAfter = await fetch(`${helper.baseUrl}/config`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ defaultOutputDir: "C:\\example\\output\\Get导出" })
    }).then((response) => response.json());
    expect(configAfter.defaultOutputDir).toBe("C:\\example\\output\\Get导出");
  });

  test("lists available subscriptions and stores export counters after a run", async () => {
    const rootDir = await mkdtemp(path.join(os.tmpdir(), "get-helper-run-"));
    const helper = await createGetHelperServer({
      rootDir,
      port: 0,
      listSubscriptions: async () => [{
        creatorId: "vnd1y25Y",
        creatorName: "冤种小徐",
        subjectUrl: "https://www.biji.com/subject/vnd1y25Y/DEFAULT"
      }],
      runExport: async (_request, onProgress) => {
        await onProgress?.({
          stage: "export-note",
          status: "started",
          detail: "[1/2] 导出中"
        });
        return {
          creatorDir: "C:\\example\\output\\抖音博主\\冤种小徐_vnd1y25Y",
          creatorId: "vnd1y25Y",
          creatorName: "冤种小徐",
          total: 2,
          success: 1,
          skipped: 1,
          failed: 0,
          items: []
        };
      }
    });
    closeTasks.push(async () => {
      await helper.close();
    });

    const subscriptions = await fetch(`${helper.baseUrl}/subscriptions`).then((response) => response.json());
    expect(subscriptions.items).toHaveLength(1);

    const accepted = await fetch(`${helper.baseUrl}/run-export`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        subjectUrl: "https://www.biji.com/subject/vnd1y25Y/DEFAULT?followId=1217293&followName=%E5%86%A4%E7%A7%8D%E5%B0%8F%E5%BE%90"
      })
    }).then((response) => response.json());

    expect(accepted.accepted).toBe(true);

    const deadline = Date.now() + 10_000;
    let status;
    do {
      status = await fetch(`${helper.baseUrl}/status`).then((response) => response.json());
      if (status.state.status === "success") {
        break;
      }
      await new Promise((resolve) => setTimeout(resolve, 100));
    } while (Date.now() < deadline);

    expect(status.state.status).toBe("success");
    expect(status.state.total).toBe(2);
    expect(status.state.successCount).toBe(1);
    expect(status.state.skippedCount).toBe(1);
  });
});
