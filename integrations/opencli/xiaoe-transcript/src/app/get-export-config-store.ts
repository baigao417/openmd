import { defaultOutputDirectory } from "../utils/output-directory.js";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";

import type { GetExportConfig, GetExportRunState } from "../types.js";

export const DEFAULT_GET_EXPORT_CONFIG: GetExportConfig = {
  defaultOutputDir: defaultOutputDirectory()
};

const DEFAULT_GET_EXPORT_STATE: GetExportRunState = {
  status: "idle",
  total: 0,
  successCount: 0,
  skippedCount: 0,
  failedCount: 0
};

export function getGetExportRootDir(rootDir?: string): string {
  return rootDir ?? path.join(process.env.USERPROFILE || os.homedir(), ".opencli", "get-export-helper");
}

function getConfigPath(rootDir?: string): string {
  return path.join(getGetExportRootDir(rootDir), "config.json");
}

function getStatePath(rootDir?: string): string {
  return path.join(getGetExportRootDir(rootDir), "state.json");
}

async function ensureDir(rootDir?: string): Promise<void> {
  await mkdir(getGetExportRootDir(rootDir), { recursive: true });
}

async function atomicWrite(filePath: string, data: string): Promise<void> {
  const tempPath = `${filePath}.tmp`;
  await writeFile(tempPath, data, "utf8");
  await writeFile(filePath, data, "utf8");
}

export async function readGetExportConfig(rootDir?: string): Promise<GetExportConfig> {
  try {
    const raw = await readFile(getConfigPath(rootDir), "utf8");
    const parsed = JSON.parse(raw) as Partial<GetExportConfig>;
    const configuredOutputDir = typeof parsed.defaultOutputDir === "string"
      ? parsed.defaultOutputDir
      : undefined;
    return {
      defaultOutputDir: configuredOutputDir
        ? configuredOutputDir
        : DEFAULT_GET_EXPORT_CONFIG.defaultOutputDir
    };
  } catch {
    return { ...DEFAULT_GET_EXPORT_CONFIG };
  }
}

export async function updateGetExportConfig(
  rootDir: string | undefined,
  patch: Partial<GetExportConfig>
): Promise<GetExportConfig> {
  const next = {
    ...(await readGetExportConfig(rootDir)),
    ...patch
  } satisfies GetExportConfig;
  await ensureDir(rootDir);
  await atomicWrite(getConfigPath(rootDir), JSON.stringify(next, null, 2));
  return next;
}

export async function readGetExportState(rootDir?: string): Promise<GetExportRunState> {
  try {
    const raw = await readFile(getStatePath(rootDir), "utf8");
    return JSON.parse(raw) as GetExportRunState;
  } catch {
    return { ...DEFAULT_GET_EXPORT_STATE };
  }
}

export async function writeGetExportState(rootDir: string | undefined, state: GetExportRunState): Promise<void> {
  await ensureDir(rootDir);
  await atomicWrite(getStatePath(rootDir), JSON.stringify(state, null, 2));
}
