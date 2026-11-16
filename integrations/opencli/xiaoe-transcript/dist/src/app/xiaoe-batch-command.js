import { readFile } from "node:fs/promises";
import { resolveRuntimeOptions } from "./runtime-options.js";
import { parseXiaoeCatalogOutput, runXiaoeBatchTranscript } from "../pipeline/xiaoe-batch.js";
function requireString(value, name) {
    if (typeof value !== "string" || value.trim().length === 0) {
        throw new Error(`${name} is required`);
    }
    return value.trim();
}
function optionalString(value) {
    return typeof value === "string" && value.trim().length > 0 ? value.trim() : undefined;
}
function parseProvider(value) {
    if (value === "auto" || value === "tingwu" || value === "get") {
        return value;
    }
    return "auto";
}
export function parseBoolean(value, defaultValue) {
    if (typeof value === "boolean") {
        return value;
    }
    if (typeof value === "number") {
        return value !== 0;
    }
    if (typeof value === "string") {
        const normalized = value.trim().toLowerCase();
        if (["true", "1", "yes", "y", "on"].includes(normalized)) {
            return true;
        }
        if (["false", "0", "no", "n", "off"].includes(normalized)) {
            return false;
        }
    }
    return defaultValue;
}
function parseNonNegativeInteger(value, defaultValue, name) {
    const raw = value === undefined || value === null || value === "" ? String(defaultValue) : String(value);
    const parsed = Number.parseInt(raw, 10);
    if (!Number.isFinite(parsed) || parsed < 0) {
        throw new Error(`${name} must be a non-negative integer`);
    }
    return parsed;
}
function parsePositiveInteger(value, defaultValue, name) {
    const raw = value === undefined || value === null || value === "" ? String(defaultValue) : String(value);
    const parsed = Number.parseInt(raw, 10);
    if (!Number.isFinite(parsed) || parsed <= 0) {
        throw new Error(`${name} must be a positive integer`);
    }
    return parsed;
}
export function parseResourceIds(value) {
    if (Array.isArray(value)) {
        const ids = value.map((item) => String(item).trim()).filter(Boolean);
        return ids.length > 0 ? ids : undefined;
    }
    if (typeof value !== "string") {
        return undefined;
    }
    const ids = value.split(",").map((part) => part.trim()).filter(Boolean);
    return ids.length > 0 ? ids : undefined;
}
export function parseStringList(value) {
    if (Array.isArray(value)) {
        const items = value.map((item) => String(item).trim()).filter(Boolean);
        return items.length > 0 ? items : undefined;
    }
    if (typeof value !== "string") {
        return undefined;
    }
    const items = value.split(/[,\n;]/).map((part) => part.trim()).filter(Boolean);
    return items.length > 0 ? items : undefined;
}
export async function parseXiaoeBatchCommandOptions(kwargs) {
    const courseUrl = requireString(kwargs["course-url"] ?? kwargs.courseUrl ?? kwargs.course_url ?? kwargs.url, "course-url");
    const catalogFile = optionalString(kwargs["catalog-file"]);
    let catalogItems;
    if (catalogFile) {
        catalogItems = parseXiaoeCatalogOutput((await readFile(catalogFile, "utf8")).replace(/^\uFEFF/, ""));
        const courseHost = new URL(courseUrl).hostname;
        const ids = new Set();
        if (catalogItems.length === 0)
            throw new Error("catalog-file contains no resources");
        for (const item of catalogItems) {
            if (!item.title.trim() || !/^[plvai]_[A-Za-z0-9]+$/.test(item.resource_id) || !item.type.trim() || ids.has(item.resource_id)) {
                throw new Error("catalog-file contains an invalid or duplicate resource");
            }
            const target = new URL(item.url);
            if (target.protocol !== "https:" || target.hostname !== courseHost)
                throw new Error("catalog-file resource host does not match course-url");
            ids.add(item.resource_id);
        }
        const selectedIds = parseResourceIds(kwargs["resource-id"] ?? kwargs.resourceId ?? kwargs.resource_id ?? kwargs.resourceIds);
        if (selectedIds?.some((id) => !ids.has(id)))
            throw new Error("catalog-file is missing a requested resource-id");
    }
    const runtime = await resolveRuntimeOptions({
        outDir: optionalString(kwargs.out),
        provider: optionalString(kwargs.provider),
        persistOutputDir: false
    });
    return {
        courseUrl,
        catalogItems,
        outDir: runtime.outDir,
        provider: parseProvider(runtime.provider),
        status: optionalString(kwargs.status) ?? "all",
        type: optionalString(kwargs.type) ?? "直播",
        chapter: optionalString(kwargs.chapter),
        title: optionalString(kwargs.title),
        resourceIds: parseResourceIds(kwargs["resource-id"] ?? kwargs.resourceId ?? kwargs.resource_id ?? kwargs.resourceIds),
        limit: parseNonNegativeInteger(kwargs.limit, 0, "limit"),
        skipExisting: parseBoolean(kwargs["skip-existing"], true),
        resume: parseBoolean(kwargs.resume, true),
        restart: parseBoolean(kwargs.restart, false),
        dryRun: parseBoolean(kwargs["dry-run"], false),
        timeoutMinutes: parsePositiveInteger(kwargs["timeout-minutes"], 120, "timeout-minutes"),
        reuseTingwuExisting: parseBoolean(kwargs["reuse-tingwu-existing"] ?? kwargs.reuseTingwuExisting, true),
        tingwuExistingOnly: parseBoolean(kwargs["tingwu-existing-only"] ?? kwargs.tingwuExistingOnly, false),
        tingwuExistingSearchUrls: parseStringList(kwargs["tingwu-existing-search-url"]
            ?? kwargs["tingwu-existing-search-urls"]
            ?? kwargs.tingwuExistingSearchUrl
            ?? kwargs.tingwuExistingSearchUrls)
    };
}
export async function runXiaoeBatchTranscriptCommand(kwargs, overrides) {
    const deps = {
        runBatch: runXiaoeBatchTranscript,
        ...overrides
    };
    const options = await parseXiaoeBatchCommandOptions(kwargs);
    const result = await deps.runBatch(options);
    return [{
            outDir: result.outDir,
            total: result.total,
            success: result.success,
            skipped: result.skipped,
            failed: result.failed,
            statePath: result.statePath ?? "",
            items: result.items ?? []
        }];
}
