import { defaultOutputDirectory } from "../utils/output-directory.js";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
export const DEFAULT_GET_EXPORT_CONFIG = {
    defaultOutputDir: defaultOutputDirectory()
};
const DEFAULT_GET_EXPORT_STATE = {
    status: "idle",
    total: 0,
    successCount: 0,
    skippedCount: 0,
    failedCount: 0
};
export function getGetExportRootDir(rootDir) {
    return rootDir ?? path.join(process.env.USERPROFILE || os.homedir(), ".opencli", "get-export-helper");
}
function getConfigPath(rootDir) {
    return path.join(getGetExportRootDir(rootDir), "config.json");
}
function getStatePath(rootDir) {
    return path.join(getGetExportRootDir(rootDir), "state.json");
}
async function ensureDir(rootDir) {
    await mkdir(getGetExportRootDir(rootDir), { recursive: true });
}
async function atomicWrite(filePath, data) {
    const tempPath = `${filePath}.tmp`;
    await writeFile(tempPath, data, "utf8");
    await writeFile(filePath, data, "utf8");
}
export async function readGetExportConfig(rootDir) {
    try {
        const raw = await readFile(getConfigPath(rootDir), "utf8");
        const parsed = JSON.parse(raw);
        const configuredOutputDir = typeof parsed.defaultOutputDir === "string"
            ? parsed.defaultOutputDir
            : undefined;
        return {
            defaultOutputDir: configuredOutputDir
                ? configuredOutputDir
                : DEFAULT_GET_EXPORT_CONFIG.defaultOutputDir
        };
    }
    catch {
        return { ...DEFAULT_GET_EXPORT_CONFIG };
    }
}
export async function updateGetExportConfig(rootDir, patch) {
    const next = {
        ...(await readGetExportConfig(rootDir)),
        ...patch
    };
    await ensureDir(rootDir);
    await atomicWrite(getConfigPath(rootDir), JSON.stringify(next, null, 2));
    return next;
}
export async function readGetExportState(rootDir) {
    try {
        const raw = await readFile(getStatePath(rootDir), "utf8");
        return JSON.parse(raw);
    }
    catch {
        return { ...DEFAULT_GET_EXPORT_STATE };
    }
}
export async function writeGetExportState(rootDir, state) {
    await ensureDir(rootDir);
    await atomicWrite(getStatePath(rootDir), JSON.stringify(state, null, 2));
}
