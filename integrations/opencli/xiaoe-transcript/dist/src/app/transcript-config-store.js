import { defaultOutputDirectory } from "../utils/output-directory.js";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
export const DEFAULT_TRANSCRIPT_CONFIG = {
    defaultOutputDir: defaultOutputDirectory(),
    defaultProvider: "auto"
};
const DEFAULT_TRANSCRIPT_STATE = {
    status: "idle"
};
export function getTranscriptRootDir(rootDir) {
    return rootDir ?? path.join(process.env.USERPROFILE || os.homedir(), ".opencli", "transcript-helper");
}
function getConfigPath(rootDir) {
    return path.join(getTranscriptRootDir(rootDir), "config.json");
}
function getStatePath(rootDir) {
    return path.join(getTranscriptRootDir(rootDir), "state.json");
}
async function ensureDir(rootDir) {
    await mkdir(getTranscriptRootDir(rootDir), { recursive: true });
}
async function atomicWrite(filePath, data) {
    const tempPath = `${filePath}.tmp`;
    await writeFile(tempPath, data, "utf8");
    await writeFile(filePath, data, "utf8");
}
export async function readTranscriptConfig(rootDir) {
    try {
        const raw = await readFile(getConfigPath(rootDir), "utf8");
        const parsed = JSON.parse(raw);
        return {
            defaultOutputDir: typeof parsed.defaultOutputDir === "string"
                ? parsed.defaultOutputDir
                : DEFAULT_TRANSCRIPT_CONFIG.defaultOutputDir,
            defaultProvider: parsed.defaultProvider === "auto" || parsed.defaultProvider === "tingwu" || parsed.defaultProvider === "get"
                ? parsed.defaultProvider
                : DEFAULT_TRANSCRIPT_CONFIG.defaultProvider
        };
    }
    catch {
        return { ...DEFAULT_TRANSCRIPT_CONFIG };
    }
}
export async function updateTranscriptConfig(rootDir, patch) {
    const next = {
        ...(await readTranscriptConfig(rootDir)),
        ...patch
    };
    await ensureDir(rootDir);
    await atomicWrite(getConfigPath(rootDir), JSON.stringify(next, null, 2));
    return next;
}
export async function readTranscriptState(rootDir) {
    try {
        const raw = await readFile(getStatePath(rootDir), "utf8");
        return JSON.parse(raw);
    }
    catch {
        return { ...DEFAULT_TRANSCRIPT_STATE };
    }
}
export async function writeTranscriptState(rootDir, state) {
    await ensureDir(rootDir);
    await atomicWrite(getStatePath(rootDir), JSON.stringify(state, null, 2));
}
