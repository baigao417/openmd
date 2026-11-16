import { defaultOutputDirectory } from "../utils/output-directory.js";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";

export interface TranscriptConfig {
  defaultOutputDir: string;
  defaultProvider: "auto" | "tingwu" | "get";
}

export interface TranscriptRunState {
  status: "idle" | "running" | "success" | "error";
  currentStage?: string;
  log?: string[];
  lastRun?: {
    url: string;
    title: string;
    provider: string;
    tabId?: number;
    pageId?: string;
    outputPath?: string;
    tempDir?: string;
    error?: string;
    startedAt: string;
    finishedAt?: string;
  };
}

export const DEFAULT_TRANSCRIPT_CONFIG: TranscriptConfig = {
  defaultOutputDir: defaultOutputDirectory(),
  defaultProvider: "auto"
};

const DEFAULT_TRANSCRIPT_STATE: TranscriptRunState = {
  status: "idle"
};

export function getTranscriptRootDir(rootDir?: string): string {
  return rootDir ?? path.join(process.env.USERPROFILE || os.homedir(), ".opencli", "transcript-helper");
}

function getConfigPath(rootDir?: string): string {
  return path.join(getTranscriptRootDir(rootDir), "config.json");
}

function getStatePath(rootDir?: string): string {
  return path.join(getTranscriptRootDir(rootDir), "state.json");
}

async function ensureDir(rootDir?: string): Promise<void> {
  await mkdir(getTranscriptRootDir(rootDir), { recursive: true });
}

async function atomicWrite(filePath: string, data: string): Promise<void> {
  const tempPath = `${filePath}.tmp`;
  await writeFile(tempPath, data, "utf8");
  await writeFile(filePath, data, "utf8");
}

export async function readTranscriptConfig(rootDir?: string): Promise<TranscriptConfig> {
  try {
    const raw = await readFile(getConfigPath(rootDir), "utf8");
    const parsed = JSON.parse(raw) as Partial<TranscriptConfig>;
    return {
      defaultOutputDir: typeof parsed.defaultOutputDir === "string"
        ? parsed.defaultOutputDir
        : DEFAULT_TRANSCRIPT_CONFIG.defaultOutputDir,
      defaultProvider: parsed.defaultProvider === "auto" || parsed.defaultProvider === "tingwu" || parsed.defaultProvider === "get"
        ? parsed.defaultProvider
        : DEFAULT_TRANSCRIPT_CONFIG.defaultProvider
    };
  } catch {
    return { ...DEFAULT_TRANSCRIPT_CONFIG };
  }
}

export async function updateTranscriptConfig(rootDir: string | undefined, patch: Partial<TranscriptConfig>): Promise<TranscriptConfig> {
  const next = {
    ...(await readTranscriptConfig(rootDir)),
    ...patch
  } satisfies TranscriptConfig;
  await ensureDir(rootDir);
  await atomicWrite(getConfigPath(rootDir), JSON.stringify(next, null, 2));
  return next;
}

export async function readTranscriptState(rootDir?: string): Promise<TranscriptRunState> {
  try {
    const raw = await readFile(getStatePath(rootDir), "utf8");
    return JSON.parse(raw) as TranscriptRunState;
  } catch {
    return { ...DEFAULT_TRANSCRIPT_STATE };
  }
}

export async function writeTranscriptState(rootDir: string | undefined, state: TranscriptRunState): Promise<void> {
  await ensureDir(rootDir);
  await atomicWrite(getStatePath(rootDir), JSON.stringify(state, null, 2));
}
