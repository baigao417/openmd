import { defaultOutputDirectory } from "../utils/output-directory.js";
import { mkdir, readFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";

import { atomicWriteUtf8 } from "../utils/atomic-write.js";
import type { DownloadStatusSnapshot } from "../types.js";

export interface XiaoeConfig {
  defaultOutputDir: string;
  defaultProvider: "auto" | "tingwu" | "get";
}

export interface XiaoeRunState {
  status: "idle" | "running" | "success" | "error";
  currentStage?: string;
  download?: DownloadStatusSnapshot;
  log?: string[];
  lastRun?: {
    url: string;
    title: string;
    provider: string;
    outputPath?: string;
    tempDir?: string;
    error?: string;
    startedAt: string;
    finishedAt?: string;
  };
}

export const DEFAULT_XIAOE_CONFIG: XiaoeConfig = {
  defaultOutputDir: defaultOutputDirectory(),
  defaultProvider: "auto"
};

const DEFAULT_XIAOE_STATE: XiaoeRunState = {
  status: "idle"
};

export function getXiaoeRootDir(rootDir?: string): string {
  return rootDir ?? path.join(process.env.USERPROFILE || os.homedir(), ".opencli", "xiaoe-transcript");
}

function getConfigPath(rootDir?: string): string {
  return path.join(getXiaoeRootDir(rootDir), "config.json");
}

function getStatePath(rootDir?: string): string {
  return path.join(getXiaoeRootDir(rootDir), "state.json");
}

async function ensureDir(rootDir?: string): Promise<void> {
  await mkdir(getXiaoeRootDir(rootDir), { recursive: true });
}

async function atomicWrite(filePath: string, data: string): Promise<void> {
  await atomicWriteUtf8(filePath, data);
}

export async function readXiaoeConfig(rootDir?: string): Promise<XiaoeConfig> {
  try {
    const raw = await readFile(getConfigPath(rootDir), "utf8");
    const parsed = JSON.parse(raw) as Partial<XiaoeConfig>;
    return {
      defaultOutputDir: typeof parsed.defaultOutputDir === "string"
        ? parsed.defaultOutputDir
        : DEFAULT_XIAOE_CONFIG.defaultOutputDir,
      defaultProvider: parsed.defaultProvider === "auto" || parsed.defaultProvider === "tingwu" || parsed.defaultProvider === "get"
        ? parsed.defaultProvider
        : DEFAULT_XIAOE_CONFIG.defaultProvider
    };
  } catch {
    return { ...DEFAULT_XIAOE_CONFIG };
  }
}

export async function updateXiaoeConfig(rootDir: string | undefined, patch: Partial<XiaoeConfig>): Promise<XiaoeConfig> {
  const next = {
    ...(await readXiaoeConfig(rootDir)),
    ...patch
  } satisfies XiaoeConfig;
  await ensureDir(rootDir);
  await atomicWrite(getConfigPath(rootDir), JSON.stringify(next, null, 2));
  return next;
}

export async function readXiaoeState(rootDir?: string): Promise<XiaoeRunState> {
  try {
    const raw = await readFile(getStatePath(rootDir), "utf8");
    return JSON.parse(raw) as XiaoeRunState;
  } catch {
    return { ...DEFAULT_XIAOE_STATE };
  }
}

export async function writeXiaoeState(rootDir: string | undefined, state: XiaoeRunState): Promise<void> {
  await ensureDir(rootDir);
  await atomicWrite(getStatePath(rootDir), JSON.stringify(state, null, 2));
}
