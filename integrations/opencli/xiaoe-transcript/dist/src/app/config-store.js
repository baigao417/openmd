import { defaultOutputDirectory } from "../utils/output-directory.js";
import { mkdir, readFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { atomicWriteUtf8 } from "../utils/atomic-write.js";
export const DEFAULT_XIAOE_CONFIG = {
    defaultOutputDir: defaultOutputDirectory(),
    defaultProvider: "auto"
};
const DEFAULT_XIAOE_STATE = {
    status: "idle"
};
export function getXiaoeRootDir(rootDir) {
    return rootDir ?? path.join(process.env.USERPROFILE || os.homedir(), ".opencli", "xiaoe-transcript");
}
function getConfigPath(rootDir) {
    return path.join(getXiaoeRootDir(rootDir), "config.json");
}
function getStatePath(rootDir) {
    return path.join(getXiaoeRootDir(rootDir), "state.json");
}
async function ensureDir(rootDir) {
    await mkdir(getXiaoeRootDir(rootDir), { recursive: true });
}
async function atomicWrite(filePath, data) {
    await atomicWriteUtf8(filePath, data);
}
export async function readXiaoeConfig(rootDir) {
    try {
        const raw = await readFile(getConfigPath(rootDir), "utf8");
        const parsed = JSON.parse(raw);
        return {
            defaultOutputDir: typeof parsed.defaultOutputDir === "string"
                ? parsed.defaultOutputDir
                : DEFAULT_XIAOE_CONFIG.defaultOutputDir,
            defaultProvider: parsed.defaultProvider === "auto" || parsed.defaultProvider === "tingwu" || parsed.defaultProvider === "get"
                ? parsed.defaultProvider
                : DEFAULT_XIAOE_CONFIG.defaultProvider
        };
    }
    catch {
        return { ...DEFAULT_XIAOE_CONFIG };
    }
}
export async function updateXiaoeConfig(rootDir, patch) {
    const next = {
        ...(await readXiaoeConfig(rootDir)),
        ...patch
    };
    await ensureDir(rootDir);
    await atomicWrite(getConfigPath(rootDir), JSON.stringify(next, null, 2));
    return next;
}
export async function readXiaoeState(rootDir) {
    try {
        const raw = await readFile(getStatePath(rootDir), "utf8");
        return JSON.parse(raw);
    }
    catch {
        return { ...DEFAULT_XIAOE_STATE };
    }
}
export async function writeXiaoeState(rootDir, state) {
    await ensureDir(rootDir);
    await atomicWrite(getStatePath(rootDir), JSON.stringify(state, null, 2));
}
