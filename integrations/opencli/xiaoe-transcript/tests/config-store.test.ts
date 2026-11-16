import { mkdtemp, readdir, readFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";

import { describe, expect, test } from "vitest";

import {
  DEFAULT_XIAOE_CONFIG,
  readXiaoeConfig,
  readXiaoeState,
  updateXiaoeConfig,
  writeXiaoeState
} from "../src/app/config-store.js";

describe("config store", () => {
  test("returns defaults before config exists and persists updates", async () => {
    const rootDir = await mkdtemp(path.join(os.tmpdir(), "xiaoe-config-"));

    expect(await readXiaoeConfig(rootDir)).toEqual(DEFAULT_XIAOE_CONFIG);

    const updated = await updateXiaoeConfig(rootDir, {
      defaultOutputDir: "C:\\example\\output\\双语之路",
      defaultProvider: "tingwu"
    });

    expect(updated.defaultOutputDir).toBe("C:\\example\\output\\双语之路");
    expect(updated.defaultProvider).toBe("tingwu");
    expect(await readXiaoeConfig(rootDir)).toEqual(updated);
  });

  test("persists helper state to disk", async () => {
    const rootDir = await mkdtemp(path.join(os.tmpdir(), "xiaoe-state-"));

    await writeXiaoeState(rootDir, {
      status: "success",
      lastRun: {
        url: "https://example.com/course",
        title: "课程标题",
        provider: "tingwu",
        outputPath: "C:\\example\\output\\双语之路\\课程标题.md",
        startedAt: "2026-04-03T16:00:00.000Z",
        finishedAt: "2026-04-03T16:10:00.000Z"
      }
    });

    const state = await readXiaoeState(rootDir);
    expect(state.status).toBe("success");
    expect(state.lastRun?.outputPath).toContain("课程标题.md");
  });

  test("writes config without leaving temp files", async () => {
    const rootDir = await mkdtemp(path.join(os.tmpdir(), "xiaoe-config-store-"));

    await updateXiaoeConfig(rootDir, { defaultOutputDir: "C:\\example\\output\\atomic" });

    const config = await readXiaoeConfig(rootDir);
    const entries = await readdir(rootDir);
    const raw = await readFile(path.join(rootDir, "config.json"), "utf8");

    expect(config.defaultOutputDir).toBe("C:\\example\\output\\atomic");
    expect(raw).toContain("atomic");
    expect(entries.some((entry) => entry.includes(".tmp"))).toBe(false);
  });
});
