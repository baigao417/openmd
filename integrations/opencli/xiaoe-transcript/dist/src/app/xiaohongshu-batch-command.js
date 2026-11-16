import { runXiaohongshuBatch, DEFAULT_XIAOHONGSHU_BATCH_OUT_DIR } from "../pipeline/xiaohongshu-batch.js";
function requireString(value, name) {
    if (typeof value !== "string" || value.trim().length === 0) {
        throw new Error(`${name} is required`);
    }
    return value.trim();
}
function parseLimit(value) {
    const parsed = Number.parseInt(String(value ?? "9"), 10);
    if (!Number.isFinite(parsed) || parsed <= 0) {
        throw new Error("limit must be a positive integer");
    }
    return parsed;
}
function parseProvider(value) {
    if (value === "tingwu" || value === "auto" || value === "get") {
        return value;
    }
    return "tingwu";
}
export async function runXiaohongshuBatchCommand(kwargs, overrides) {
    const deps = {
        runXiaohongshuBatch,
        ...overrides
    };
    const outDir = typeof kwargs.out === "string" && kwargs.out.trim().length > 0
        ? kwargs.out.trim()
        : DEFAULT_XIAOHONGSHU_BATCH_OUT_DIR;
    const result = await deps.runXiaohongshuBatch({
        profileUrl: requireString(kwargs["profile-url"], "profile-url"),
        creatorName: requireString(kwargs["creator-name"], "creator-name"),
        creatorHandle: requireString(kwargs["creator-handle"], "creator-handle"),
        outDir,
        limit: parseLimit(kwargs.limit),
        provider: parseProvider(kwargs.provider),
        keepArtifacts: Boolean(kwargs["keep-artifacts"])
    }, undefined);
    return [{
            creatorDir: result.creatorDir,
            total: result.items.length,
            success: result.items.filter((item) => item.status === "success").length,
            failed: result.items.filter((item) => item.status === "failed").length
        }];
}
