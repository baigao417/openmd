import { defaultOutputDirectory } from "../src/utils/output-directory.js";
import { mkdtemp } from "node:fs/promises";
import os from "node:os";
import path from "node:path";

import { afterEach, describe, expect, test, vi } from "vitest";

import { createXiaoeHelperServer } from "../src/app/helper-service.js";
import { readXiaoeState, writeXiaoeState } from "../src/app/config-store.js";

const closeTasks: Array<() => Promise<void>> = [];
const TRUSTED_ORIGIN = "chrome-extension://abcdefghijklmnop";

async function authorizedJsonHeaders(baseUrl: string): Promise<Record<string, string>> {
  const status = await fetch(`${baseUrl}/status`, {
    headers: { Origin: TRUSTED_ORIGIN }
  }).then((response) => response.json()) as { security?: { token?: string } };
  return {
    Origin: TRUSTED_ORIGIN,
    "Content-Type": "application/json",
    "X-OpenCLI-Helper-Token": status.security?.token ?? ""
  };
}

afterEach(async () => {
  while (closeTasks.length > 0) {
    const task = closeTasks.pop();
    if (task) {
      await task();
    }
  }
});

describe("xiaoe helper service", () => {
  test("resets stale running state on startup instead of surfacing an old error", async () => {
    const rootDir = await mkdtemp(path.join(os.tmpdir(), "xiaoe-helper-stale-"));
    await writeXiaoeState(rootDir, {
      status: "running",
      currentStage: "queued",
      log: ["任务已加入队列"],
      lastRun: {
        url: "https://example.com/course",
        title: "直播回放",
        provider: "auto",
        startedAt: "2026-05-08T00:00:00.000Z"
      }
    });

    const helper = await createXiaoeHelperServer({
      rootDir,
      port: 0,
      runCurrent: async () => ({
        outputPath: "C:\\example\\output\\双语之路\\课程.md",
        provider: "tingwu",
        coverage: 0.95,
        tempDir: "E:\\tmp\\job"
      })
    });
    closeTasks.push(async () => {
      await helper.close();
    });

    const status = await fetch(`${helper.baseUrl}/status`).then((response) => response.json());

    expect(status.state.status).toBe("idle");
    expect(status.state.currentStage).toBe("idle");
    expect(status.state.log.at(-1)).toContain("helper restarted");
    expect(status.state.lastRun.error).toBeUndefined();
  });

  test("migrates old helper-restarted error state on status reads", async () => {
    const rootDir = await mkdtemp(path.join(os.tmpdir(), "xiaoe-helper-stale-error-"));
    await writeXiaoeState(rootDir, {
      status: "error",
      currentStage: "failed",
      log: ["helper restarted while previous task was still marked running"],
      lastRun: {
        url: "https://example.com/course",
        title: "直播回放",
        provider: "auto",
        startedAt: "2026-05-08T00:00:00.000Z",
        finishedAt: "2026-05-08T00:10:00.000Z",
        error: "helper restarted before the previous task completed"
      }
    });

    const helper = await createXiaoeHelperServer({
      rootDir,
      port: 0,
      runCurrent: async () => ({
        outputPath: "C:\\example\\output\\双语之路\\课程.md",
        provider: "tingwu",
        coverage: 0.95,
        tempDir: "E:\\tmp\\job"
      })
    });
    closeTasks.push(async () => {
      await helper.close();
    });

    const status = await fetch(`${helper.baseUrl}/status`).then((response) => response.json());

    expect(status.state.status).toBe("idle");
    expect(status.state.currentStage).toBe("idle");
    expect(status.state.lastRun.error).toBeUndefined();
  });

  test("exposes config and persists updates", async () => {
    const rootDir = await mkdtemp(path.join(os.tmpdir(), "xiaoe-helper-"));
    const helper = await createXiaoeHelperServer({
      rootDir,
      port: 0,
      runCurrent: async () => ({
        outputPath: "C:\\example\\output\\双语之路\\课程.md",
        provider: "tingwu",
        coverage: 0.95,
        tempDir: "E:\\tmp\\job"
      })
    });
    closeTasks.push(async () => {
      await helper.close();
    });

    const statusBefore = await fetch(`${helper.baseUrl}/status`).then((response) => response.json());
    expect(statusBefore.config.defaultOutputDir).toBe(defaultOutputDirectory());

    const configAfter = await fetch(`${helper.baseUrl}/config`, {
      method: "POST",
      headers: await authorizedJsonHeaders(helper.baseUrl),
      body: JSON.stringify({ defaultOutputDir: "C:\\example\\output\\新的目录" })
    }).then((response) => response.json());
    expect(configAfter.defaultOutputDir).toBe("C:\\example\\output\\新的目录");

    const statusAfter = await fetch(`${helper.baseUrl}/status`).then((response) => response.json());
    expect(statusAfter.config.defaultOutputDir).toBe("C:\\example\\output\\新的目录");
  });

  test("keeps an empty default output directory and allows setting it again later", async () => {
    const rootDir = await mkdtemp(path.join(os.tmpdir(), "xiaoe-helper-empty-dir-"));
    const helper = await createXiaoeHelperServer({
      rootDir,
      port: 0,
      runCurrent: async () => ({
        outputPath: "C:\\example\\output\\双语之路\\课程.md",
        provider: "tingwu",
        coverage: 0.95,
        tempDir: "E:\\tmp\\job"
      })
    });
    closeTasks.push(async () => {
      await helper.close();
    });

    const configWhenCleared = await fetch(`${helper.baseUrl}/config`, {
      method: "POST",
      headers: await authorizedJsonHeaders(helper.baseUrl),
      body: JSON.stringify({ defaultOutputDir: "" })
    }).then((response) => response.json());
    expect(configWhenCleared.defaultOutputDir).toBe("");

    const statusWhenCleared = await fetch(`${helper.baseUrl}/status`).then((response) => response.json());
    expect(statusWhenCleared.config.defaultOutputDir).toBe("");

    const configAfterReset = await fetch(`${helper.baseUrl}/config`, {
      method: "POST",
      headers: await authorizedJsonHeaders(helper.baseUrl),
      body: JSON.stringify({ defaultOutputDir: "C:\\example\\output\\重新设置" })
    }).then((response) => response.json());
    expect(configAfterReset.defaultOutputDir).toBe("C:\\example\\output\\重新设置");
  });

  test("rejects running a task when no output directory is configured or overridden", async () => {
    const rootDir = await mkdtemp(path.join(os.tmpdir(), "xiaoe-helper-missing-dir-"));
    const helper = await createXiaoeHelperServer({
      rootDir,
      port: 0,
      runCurrent: async () => ({
        outputPath: "C:\\example\\output\\双语之路\\课程.md",
        provider: "tingwu",
        coverage: 0.95,
        tempDir: "E:\\tmp\\job"
      })
    });
    closeTasks.push(async () => {
      await helper.close();
    });

    await fetch(`${helper.baseUrl}/config`, {
      method: "POST",
      headers: await authorizedJsonHeaders(helper.baseUrl),
      body: JSON.stringify({ defaultOutputDir: "" })
    });

    const response = await fetch(`${helper.baseUrl}/run-current`, {
      method: "POST",
      headers: await authorizedJsonHeaders(helper.baseUrl),
      body: JSON.stringify({
        url: "https://example.com/course",
        title: "课程标题",
        provider: "auto"
      })
    });
    const body = await response.json();

    expect(response.status).toBe(400);
    expect(body.error).toContain("保存目录");
  });

  test("runs one task and reports latest result", async () => {
    const rootDir = await mkdtemp(path.join(os.tmpdir(), "xiaoe-helper-run-"));
    const helper = await createXiaoeHelperServer({
      rootDir,
      port: 0,
      runCurrent: async (request) => ({
        outputPath: path.join(request.outDir, "课程标题.md"),
        provider: request.provider,
        coverage: 0.987,
        tempDir: "E:\\tmp\\job"
      })
    });
    closeTasks.push(async () => {
      await helper.close();
    });

    const accepted = await fetch(`${helper.baseUrl}/run-current`, {
      method: "POST",
      headers: await authorizedJsonHeaders(helper.baseUrl),
      body: JSON.stringify({
        url: "https://example.com/course",
        title: "课程标题",
        provider: "auto"
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
    expect(status.state.lastRun.outputPath).toContain("课程标题.md");
    expect(status.state.lastRun.provider).toBe("auto");
  });

  test("preserves download benchmark details after run-current completes", async () => {
    const rootDir = await mkdtemp(path.join(os.tmpdir(), "xiaoe-helper-run-download-status-"));
    const helper = await createXiaoeHelperServer({
      rootDir,
      port: 0,
      runCurrent: async (_request, onStageChange) => {
        await onStageChange?.({
          stage: "download",
          status: "completed",
          download: {
            status: "success",
            current: {
              status: "success",
              strategy: "direct-hls",
              attempt: 1,
              attemptLabel: "direct-hls-1",
              bytes: 2097152,
              sizeMB: 2,
              segments: {
                total: 4,
                success: 4,
                pending: 0,
                failed: 0
              }
            },
            attempts: [],
            benchmark: {
              status: "success",
              plans: [
                {
                  status: "success",
                  strategy: "ffmpeg",
                  label: "ffmpeg-1",
                  sampleDurationSec: 30,
                  wallMs: 6000,
                  bytes: 524288,
                  effectiveSpeed: 5,
                  normalizedAudio: true
                },
                {
                  status: "success",
                  strategy: "direct-hls",
                  label: "direct-hls-1",
                  sampleDurationSec: 30,
                  wallMs: 3000,
                  bytes: 1048576,
                  effectiveSpeed: 10,
                  normalizedAudio: false
                }
              ],
              selected: {
                status: "success",
                strategy: "direct-hls",
                label: "direct-hls-1",
                sampleDurationSec: 30,
                wallMs: 3000,
                bytes: 1048576,
                effectiveSpeed: 10,
                normalizedAudio: false
              }
            }
          }
        });
        return {
          outputPath: "C:\\example\\output\\双语之路\\课程.md",
          provider: "auto",
          coverage: 0.95,
          tempDir: "E:\\tmp\\job"
        };
      }
    });
    closeTasks.push(async () => {
      await helper.close();
    });

    const accepted = await fetch(`${helper.baseUrl}/run-current`, {
      method: "POST",
      headers: await authorizedJsonHeaders(helper.baseUrl),
      body: JSON.stringify({
        url: "https://example.com/course",
        title: "课程标题",
        provider: "auto"
      })
    }).then((response) => response.json());

    expect(accepted.accepted).toBe(true);
    await vi.waitFor(async () => {
      const state = await readXiaoeState(rootDir);
      expect(state.status).toBe("success");
    });

    const status = await fetch(`${helper.baseUrl}/status`).then((response) => response.json());

    expect(status.state.status).toBe("success");
    expect(status.state.download.current.strategy).toBe("direct-hls");
    expect(status.state.download.benchmark.selected.strategy).toBe("direct-hls");
    expect(status.state.download.benchmark.plans).toHaveLength(2);
  });

  test("status reads do not reset a currently running task", async () => {
    const rootDir = await mkdtemp(path.join(os.tmpdir(), "xiaoe-helper-running-status-"));
    let finishTask!: () => void;
    const helper = await createXiaoeHelperServer({
      rootDir,
      port: 0,
      runCurrent: async (_request, onStageChange) => {
        await onStageChange?.({ stage: "download", status: "started" });
        await new Promise<void>((resolve) => {
          finishTask = resolve;
        });
        return {
          outputPath: "C:\\example\\output\\双语之路\\课程.md",
          provider: "auto",
          coverage: 0.95,
          tempDir: "E:\\tmp\\job"
        };
      }
    });
    closeTasks.push(async () => {
      finishTask?.();
      await helper.close();
    });

    const accepted = await fetch(`${helper.baseUrl}/run-current`, {
      method: "POST",
      headers: await authorizedJsonHeaders(helper.baseUrl),
      body: JSON.stringify({
        url: "https://example.com/course",
        title: "课程标题",
        provider: "auto"
      })
    }).then((response) => response.json());

    expect(accepted.accepted).toBe(true);
    await vi.waitFor(async () => {
      const state = await readXiaoeState(rootDir);
      expect(state.currentStage).toBe("download");
    });

    const status = await fetch(`${helper.baseUrl}/status`).then((response) => response.json());

    expect(status.state.status).toBe("running");
    expect(status.state.currentStage).toBe("download");
    expect(status.state.log).not.toContain("helper restarted while previous task was still marked running");

    finishTask();
  });

  test("status exposes download attempt details while run-current is downloading", async () => {
    const rootDir = await mkdtemp(path.join(os.tmpdir(), "xiaoe-helper-download-status-"));
    let finishTask!: () => void;
    const helper = await createXiaoeHelperServer({
      rootDir,
      port: 0,
      runCurrent: async (_request, onStageChange) => {
        await onStageChange?.({
          stage: "download",
          status: "started",
          download: {
            status: "running",
            current: {
              status: "running",
              strategy: "ffmpeg",
              attempt: 1,
              attemptLabel: "ffmpeg-1",
              attemptPath: "C:\\tmp\\job\\download-attempts\\001-ffmpeg-1",
              path: "C:\\tmp\\job\\download-attempts\\001-ffmpeg-1\\source.mp3",
              bytes: 1048576,
              sizeMB: 1,
              lastWriteTime: "2026-05-31T00:00:00.000Z"
            },
            attempts: []
          }
        });
        await new Promise<void>((resolve) => {
          finishTask = resolve;
        });
        return {
          outputPath: "C:\\example\\output\\双语之路\\课程.md",
          provider: "auto",
          coverage: 0.95,
          tempDir: "E:\\tmp\\job"
        };
      }
    });
    closeTasks.push(async () => {
      finishTask?.();
      await helper.close();
    });

    const accepted = await fetch(`${helper.baseUrl}/run-current`, {
      method: "POST",
      headers: await authorizedJsonHeaders(helper.baseUrl),
      body: JSON.stringify({
        url: "https://example.com/course",
        title: "课程标题",
        provider: "auto"
      })
    }).then((response) => response.json());

    expect(accepted.accepted).toBe(true);
    await vi.waitFor(async () => {
      const state = await readXiaoeState(rootDir);
      expect(state.download?.current?.strategy).toBe("ffmpeg");
    });

    const status = await fetch(`${helper.baseUrl}/status`).then((response) => response.json());

    expect(status.state.download.status).toBe("running");
    expect(status.state.download.current.strategy).toBe("ffmpeg");
    expect(status.state.download.current.attemptPath).toContain("001-ffmpeg-1");
    expect(status.state.download.current.path).toContain("source.mp3");
    expect(status.state.download.current.bytes).toBe(1048576);
    expect(status.state.download.current.lastWriteTime).toBe("2026-05-31T00:00:00.000Z");

    finishTask();
  });

  test("passes run-current timeout minutes from the request body", async () => {
    const rootDir = await mkdtemp(path.join(os.tmpdir(), "xiaoe-helper-run-timeout-"));
    let observedTimeout: number | undefined;
    const helper = await createXiaoeHelperServer({
      rootDir,
      port: 0,
      runCurrent: async (request) => {
        observedTimeout = request.timeoutMinutes;
        return {
          outputPath: path.join(request.outDir, "课程标题.md"),
          provider: request.provider,
          coverage: 0.987,
          tempDir: "E:\\tmp\\job"
        };
      }
    });
    closeTasks.push(async () => {
      await helper.close();
    });

    await fetch(`${helper.baseUrl}/run-current`, {
      method: "POST",
      headers: await authorizedJsonHeaders(helper.baseUrl),
      body: JSON.stringify({
        url: "https://example.com/course",
        title: "课程标题",
        provider: "auto",
        timeoutMinutes: 240
      })
    });

    await vi.waitFor(() => {
      expect(observedTimeout).toBe(240);
    });
  });

  test("exposes catalog endpoint with batch filters", async () => {
    const rootDir = await mkdtemp(path.join(os.tmpdir(), "xiaoe-helper-catalog-"));
    let capturedCatalogUrl = "";
    const helper = await createXiaoeHelperServer({
      rootDir,
      port: 0,
      runCurrent: async () => ({
        outputPath: "C:\\example\\output\\双语之路\\课程.md",
        provider: "tingwu",
        coverage: 0.95,
        tempDir: "E:\\tmp\\job"
      }),
      readCatalog: async (courseUrl) => {
        capturedCatalogUrl = courseUrl;
        return [
          {
            title: "第一讲",
            type: "直播",
            resource_id: "a_1",
            url: "https://x.test/a_1",
            chapter: "入门",
            status: "100%"
          },
          {
            title: "第二讲",
            type: "视频",
            resource_id: "v_1",
            url: "https://x.test/v_1",
            chapter: "入门",
            status: "100%"
          }
        ];
      }
    });
    closeTasks.push(async () => {
      await helper.close();
    });

    const response = await fetch(`${helper.baseUrl}/catalog`, {
      method: "POST",
      headers: await authorizedJsonHeaders(helper.baseUrl),
      body: JSON.stringify({
        courseUrl: "https://appabc.h5.xet.pomoho.com/p/course/abc",
        type: "直播",
        status: "100%"
      })
    });
    const body = await response.json();

    expect(response.status).toBe(200);
    expect(body.normalizedUrl).toBe("https://appabc.h5.xet.citv.cn/p/course/abc");
    expect(capturedCatalogUrl).toBe("https://appabc.h5.xet.pomoho.com/p/course/abc");
    expect(body.items.map((row: { resource_id: string }) => row.resource_id)).toEqual(["a_1"]);

    const cached = await fetch(`${helper.baseUrl}/catalog-status`, {
      headers: await authorizedJsonHeaders(helper.baseUrl)
    }).then((catalogResponse) => catalogResponse.json());
    expect(cached.catalog).toMatchObject({
      status: "success",
      courseUrl: "https://appabc.h5.xet.pomoho.com/p/course/abc",
      normalizedUrl: "https://appabc.h5.xet.citv.cn/p/course/abc",
      total: 1
    });
    expect(cached.catalog.key).toBe(JSON.stringify({
      courseUrl: "https://appabc.h5.xet.pomoho.com/p/course/abc",
      type: "直播",
      status: "100%",
      chapter: "",
      title: "",
      limit: 0
    }));
  });

  test("runs a batch task and reports batch status", async () => {
    const rootDir = await mkdtemp(path.join(os.tmpdir(), "xiaoe-helper-batch-"));
    let capturedCatalogItems: unknown;
    const helper = await createXiaoeHelperServer({
      rootDir,
      port: 0,
      runCurrent: async () => ({
        outputPath: "C:\\example\\output\\双语之路\\课程.md",
        provider: "tingwu",
        coverage: 0.95,
        tempDir: "E:\\tmp\\job"
      }),
      runBatch: async (request, onProgress) => {
        capturedCatalogItems = request.catalogItems;
        await onProgress?.({
          stage: "batch",
          status: "started",
          counts: { total: 2, success: 0, skipped: 0, failed: 0, pending: 2 }
        });
        return {
          outDir: request.outDir,
          total: 2,
          success: 1,
          skipped: 1,
          failed: 0
        };
      }
    });
    closeTasks.push(async () => {
      await helper.close();
    });

    const accepted = await fetch(`${helper.baseUrl}/batch-run`, {
      method: "POST",
      headers: await authorizedJsonHeaders(helper.baseUrl),
      body: JSON.stringify({
        courseUrl: "https://appabc.h5.xiaoecloud.com/p/course/abc",
        outDir: "C:\\example\\output\\前哨战",
        provider: "auto",
        resourceIds: ["a_1", "a_2"],
        catalogItems: [
          { title: "第一讲", type: "直播", resource_id: "a_1", url: "https://x.test/a_1", chapter: "入门", status: "100%" },
          { title: "第二讲", type: "直播", resource_id: "a_2", url: "https://x.test/a_2", chapter: "入门", status: "100%" }
        ]
      })
    }).then((response) => response.json());

    expect(accepted.accepted).toBe(true);

    const deadline = Date.now() + 10_000;
    let status;
    do {
      status = await fetch(`${helper.baseUrl}/batch-status`).then((response) => response.json());
      if (status.batch.status === "success") {
        break;
      }
      await new Promise((resolve) => setTimeout(resolve, 100));
    } while (Date.now() < deadline);

    expect(status.batch.status).toBe("success");
    expect(status.batch.result).toMatchObject({ total: 2, success: 1, skipped: 1, failed: 0 });
    expect(capturedCatalogItems).toEqual([
      { title: "第一讲", type: "直播", resource_id: "a_1", url: "https://x.test/a_1", chapter: "入门", status: "100%" },
      { title: "第二讲", type: "直播", resource_id: "a_2", url: "https://x.test/a_2", chapter: "入门", status: "100%" }
    ]);
  });

  test("rejects run-current while a batch task is running", async () => {
    const rootDir = await mkdtemp(path.join(os.tmpdir(), "xiaoe-helper-busy-batch-"));
    let resolveBatch!: (value: { outDir: string; total: number; success: number; skipped: number; failed: number }) => void;
    const batchPromise = new Promise<{ outDir: string; total: number; success: number; skipped: number; failed: number }>((resolve) => {
      resolveBatch = resolve;
    });
    const helper = await createXiaoeHelperServer({
      rootDir,
      port: 0,
      runCurrent: async () => ({
        outputPath: "C:\\example\\output\\双语之路\\课程.md",
        provider: "tingwu",
        coverage: 0.95,
        tempDir: "E:\\tmp\\job"
      }),
      runBatch: async () => await batchPromise
    });
    closeTasks.push(async () => {
      await helper.close();
    });

    await fetch(`${helper.baseUrl}/batch-run`, {
      method: "POST",
      headers: await authorizedJsonHeaders(helper.baseUrl),
      body: JSON.stringify({
        courseUrl: "https://appabc.h5.xiaoecloud.com/p/course/abc",
        outDir: "C:\\example\\output\\前哨战"
      })
    });

    const response = await fetch(`${helper.baseUrl}/run-current`, {
      method: "POST",
      headers: await authorizedJsonHeaders(helper.baseUrl),
      body: JSON.stringify({
        url: "https://example.com/course",
        title: "课程标题",
        provider: "auto"
      })
    });
    expect(response.status).toBe(409);

    resolveBatch({
      outDir: "C:\\example\\output\\前哨战",
      total: 1,
      success: 1,
      skipped: 0,
      failed: 0
    });
  });

  test("protects mutating helper routes with origin and token checks", async () => {
    const rootDir = await mkdtemp(path.join(os.tmpdir(), "xiaoe-helper-security-"));
    const helper = await createXiaoeHelperServer({
      rootDir,
      port: 0,
      security: { token: "test-helper-token" },
      runCurrent: async () => ({
        outputPath: "C:\\example\\output\\双语之路\\课程.md",
        provider: "tingwu",
        coverage: 0.95,
        tempDir: "E:\\tmp\\job"
      })
    });
    closeTasks.push(async () => {
      await helper.close();
    });

    const trustedStatus = await fetch(`${helper.baseUrl}/status`, {
      headers: { Origin: TRUSTED_ORIGIN }
    }).then((response) => response.json());
    expect(trustedStatus.security.token).toBe("test-helper-token");

    const missingToken = await fetch(`${helper.baseUrl}/config`, {
      method: "POST",
      headers: {
        Origin: TRUSTED_ORIGIN,
        "Content-Type": "application/json"
      },
      body: JSON.stringify({ defaultOutputDir: "C:\\example\\output\\blocked" })
    });
    expect(missingToken.status).toBe(401);

    const evilOrigin = await fetch(`${helper.baseUrl}/config`, {
      method: "POST",
      headers: {
        Origin: "https://evil.example",
        "Content-Type": "application/json",
        "X-OpenCLI-Helper-Token": "test-helper-token"
      },
      body: JSON.stringify({ defaultOutputDir: "C:\\example\\output\\blocked" })
    });
    expect(evilOrigin.status).toBe(403);

    const accepted = await fetch(`${helper.baseUrl}/config`, {
      method: "POST",
      headers: {
        Origin: TRUSTED_ORIGIN,
        "Content-Type": "application/json",
        "X-OpenCLI-Helper-Token": trustedStatus.security.token
      },
      body: JSON.stringify({ defaultOutputDir: "C:\\example\\output\\trusted" })
    }).then((response) => response.json());
    expect(accepted.defaultOutputDir).toBe("C:\\example\\output\\trusted");
  });

  test("returns 400 for invalid JSON bodies instead of rejecting the request handler", async () => {
    const rootDir = await mkdtemp(path.join(os.tmpdir(), "xiaoe-helper-invalid-json-"));
    const helper = await createXiaoeHelperServer({
      rootDir,
      port: 0,
      security: { token: "test-helper-token" },
      runCurrent: async () => ({
        outputPath: "C:\\example\\output\\双语之路\\课程.md",
        provider: "tingwu",
        coverage: 0.95,
        tempDir: "E:\\tmp\\job"
      })
    });
    closeTasks.push(async () => {
      await helper.close();
    });

    const response = await fetch(`${helper.baseUrl}/config`, {
      method: "POST",
      headers: {
        Origin: TRUSTED_ORIGIN,
        "Content-Type": "application/json",
        "X-OpenCLI-Helper-Token": "test-helper-token"
      },
      body: "{not-json"
    });
    const body = await response.json();

    expect(response.status).toBe(400);
    expect(body.error).toContain("invalid JSON");
  });
});
