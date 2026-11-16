import os from "node:os";
import path from "node:path";
import { afterEach, expect, test, vi } from "vitest";

import { defaultOutputDirectory } from "../src/utils/output-directory.js";

afterEach(() => vi.unstubAllEnvs());

test("default output belongs to the current user and has no workstation root", () => {
  vi.stubEnv("OPENCLI_TRANSCRIPT_OUT_DIR", "");
  expect(defaultOutputDirectory()).toBe(path.resolve(os.homedir(), "Documents", "Openmd", "transcripts"));
});

test("explicit environment output is resolved consistently", () => {
  vi.stubEnv("OPENCLI_TRANSCRIPT_OUT_DIR", "relative-output");
  expect(defaultOutputDirectory()).toBe(path.resolve("relative-output"));
});
