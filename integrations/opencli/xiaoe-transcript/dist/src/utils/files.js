import { mkdir, rm } from "node:fs/promises";
import path from "node:path";
export function sanitizeFileName(input) {
    const trimmed = input.trim();
    const withDash = trimmed.replace(/[:：]+/g, "-");
    const cleaned = withDash
        .replace(/[<>"/\\|?*]+/g, " ")
        .replace(/\s+/g, " ")
        .replace(/-+/g, "-")
        .replace(/\s*-\s*/g, "-")
        .replace(/[. ]+$/g, "")
        .trim();
    return cleaned || "xiaoe-transcript";
}
export async function ensureParentDir(filePath) {
    await mkdir(path.dirname(filePath), { recursive: true });
}
export async function cleanupPaths(paths) {
    await Promise.all(paths.map(async (target) => {
        await rm(target, { force: true, recursive: true }).catch(() => undefined);
    }));
}
