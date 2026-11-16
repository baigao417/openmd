import { defaultOutputDirectory } from "../src/utils/output-directory.js";
import { mkdtemp } from "node:fs/promises";
import os from "node:os";
import path from "node:path";

import { afterEach, describe, expect, test } from "vitest";

import { createTingwuHelperServer } from "../src/app/tingwu-helper-service.js";

const closeTasks: Array<() => Promise<void>> = [];

afterEach(async () => {
  while (closeTasks.length > 0) {
    const task = closeTasks.pop();
    if (task) {
      await task();
    }
  }
});

describe("tingwu helper service", () => {
  test("exposes transcript helper config and persists updates", async () => {
    const rootDir = await mkdtemp(path.join(os.tmpdir(), "tingwu-helper-"));
    const helper = await createTingwuHelperServer({
      rootDir,
      port: 0,
      runCurrent: async () => ({
        outputPath: "C:\\example\\output\\transcripts\\逐字稿.md",
        provider: "tingwu",
        coverage: 1,
        tempDir: ""
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
      body: JSON.stringify({ defaultOutputDir: "C:\\example\\output\\transcripts" })
    }).then((response) => response.json());
    expect(configAfter.defaultOutputDir).toBe("C:\\example\\output\\transcripts");
  });

  test("keeps an empty default output directory and allows setting it again later", async () => {
    const rootDir = await mkdtemp(path.join(os.tmpdir(), "tingwu-helper-empty-dir-"));
    const helper = await createTingwuHelperServer({
      rootDir,
      port: 0,
      runCurrent: async () => ({
        outputPath: "C:\\example\\output\\transcripts\\逐字稿.md",
        provider: "tingwu",
        coverage: 1,
        tempDir: ""
      })
    });
    closeTasks.push(async () => {
      await helper.close();
    });

    const configWhenCleared = await fetch(`${helper.baseUrl}/config`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ defaultOutputDir: "" })
    }).then((response) => response.json());
    expect(configWhenCleared.defaultOutputDir).toBe("");

    const statusWhenCleared = await fetch(`${helper.baseUrl}/status`).then((response) => response.json());
    expect(statusWhenCleared.config.defaultOutputDir).toBe("");

    const configAfterReset = await fetch(`${helper.baseUrl}/config`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ defaultOutputDir: "C:\\example\\output\\transcripts\\reset" })
    }).then((response) => response.json());
    expect(configAfterReset.defaultOutputDir).toBe("C:\\example\\output\\transcripts\\reset");
  });

  test("rejects running a task when no output directory is configured or overridden", async () => {
    const rootDir = await mkdtemp(path.join(os.tmpdir(), "tingwu-helper-missing-dir-"));
    const helper = await createTingwuHelperServer({
      rootDir,
      port: 0,
      runCurrent: async () => ({
        outputPath: "C:\\example\\output\\transcripts\\逐字稿.md",
        provider: "tingwu",
        coverage: 1,
        tempDir: ""
      })
    });
    closeTasks.push(async () => {
      await helper.close();
    });

    await fetch(`${helper.baseUrl}/config`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ defaultOutputDir: "" })
    });

    const response = await fetch(`${helper.baseUrl}/run-current`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        url: "https://tingwu.aliyun.com/doc/transcripts/xyz",
        title: "当前页逐字稿",
        tabId: 123,
        pageId: "opencli-page-abc"
      })
    });
    const body = await response.json();

    expect(response.status).toBe(400);
    expect(body.error).toContain("保存目录");
  });

  test("runs one current-page export task and stores the latest result", async () => {
    const rootDir = await mkdtemp(path.join(os.tmpdir(), "tingwu-helper-run-"));
    let capturedRequest: unknown;
    const helper = await createTingwuHelperServer({
      rootDir,
      port: 0,
      runCurrent: async (request) => {
        capturedRequest = request;
        return {
          outputPath: path.join(request.outDir, "当前页逐字稿.md"),
          provider: "tingwu",
          coverage: 1,
          tempDir: ""
        };
      }
    });
    closeTasks.push(async () => {
      await helper.close();
    });

    const accepted = await fetch(`${helper.baseUrl}/run-current`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        url: "https://tingwu.aliyun.com/doc/transcripts/xyz",
        title: "当前页逐字稿",
        tabId: 123,
        pageId: "opencli-page-abc"
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
    expect(capturedRequest).toMatchObject({
      url: "https://tingwu.aliyun.com/doc/transcripts/xyz",
      title: "当前页逐字稿",
      tabId: 123,
      pageId: "opencli-page-abc"
    });
    expect(status.state.lastRun.outputPath).toContain("当前页逐字稿.md");
    expect(status.state.lastRun.provider).toBe("tingwu");
    expect(status.state.lastRun.tabId).toBe(123);
    expect(status.state.lastRun.pageId).toBe("opencli-page-abc");
  });
});
