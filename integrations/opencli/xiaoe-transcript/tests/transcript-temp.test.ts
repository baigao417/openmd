import { afterEach, expect, test, vi } from "vitest";
import { mkdir, mkdtemp, rm } from "node:fs/promises";
import path from "node:path";
import os from "node:os";
import { createTranscriptTempDir } from "../src/utils/transcript-temp.js";

const owned: string[] = [];
afterEach(async () => {
  vi.unstubAllEnvs();
  for (const directory of owned.splice(0)) await rm(directory, { recursive: true, force: true });
});

test("creates a fresh task temp directory by default", async () => {
  vi.stubEnv("OPENCLI_XIAOE_RESUME_DIR", "");
  const directory = await createTranscriptTempDir();
  owned.push(directory);
  expect(path.basename(directory)).toMatch(/^xiaoe-to-md-/);
});

test("resumes an explicit interrupted directory without allocating another", async () => {
  const directory = await mkdtemp(path.join(os.tmpdir(), "xiaoe-to-md-"));
  owned.push(directory);
  await mkdir(path.join(directory, "download-attempts"));
  vi.stubEnv("OPENCLI_XIAOE_RESUME_DIR", directory);
  expect((await createTranscriptTempDir()).toLowerCase()).toBe(directory.toLowerCase());
});

test("rejects unrelated directories and transcript directories without downloads", async () => {
  const unrelated = await mkdtemp(path.join(os.tmpdir(), "profile-test-"));
  const empty = await mkdtemp(path.join(os.tmpdir(), "xiaoe-to-md-"));
  owned.push(unrelated, empty);
  await expect(createTranscriptTempDir(unrelated)).rejects.toThrow("existing Xiaoe transcript temp directory");
  await expect(createTranscriptTempDir(empty)).rejects.toThrow();
});
