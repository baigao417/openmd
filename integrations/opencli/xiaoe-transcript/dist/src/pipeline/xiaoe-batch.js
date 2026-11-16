import { existsSync } from "node:fs";
import { access, readFile, readdir, rm, rmdir } from "node:fs/promises";
import { createHash } from "node:crypto";
import path from "node:path";
import process from "node:process";
import { completePreparedMediaToMarkdown, prepareMediaToMarkdown, runXiaoeToMarkdown } from "../workflow.js";
import { runTingwuExistingTranscriptToMarkdown } from "./tingwu-existing.js";
import { runCommand, redactSensitiveText } from "../utils/command.js";
import { atomicWriteUtf8 } from "../utils/atomic-write.js";
import { sanitizeFileName } from "../utils/files.js";
const OPENCLI_MAIN_JS_RELATIVE = path.join("node_modules", "@jackwener", "opencli", "dist", "src", "main.js");
const XIAOE_CATALOG_BROWSER_TIMEOUT_SECONDS = 300;
const XIAOE_CATALOG_COMMAND_TIMEOUT_MS = (XIAOE_CATALOG_BROWSER_TIMEOUT_SECONDS + 30) * 1000;
export function normalizeXiaoeCatalogUrl(url) {
    try {
        const parsed = new URL(url);
        if (/^[^.]+\.h5\.(?:xiaoecloud|xiaoeknow)\.com$/i.test(parsed.hostname)) {
            parsed.hostname = parsed.hostname.replace(/\.h5\.(?:xiaoecloud|xiaoeknow)\.com$/i, ".h5.xet.citv.cn");
            return parsed.toString();
        }
        if (/^[^.]+\.h5\.xet\.pomoho\.com$/i.test(parsed.hostname)) {
            parsed.hostname = parsed.hostname.replace(/\.h5\.xet\.pomoho\.com$/i, ".h5.xet.citv.cn");
            return parsed.toString();
        }
        return url;
    }
    catch {
        return url;
    }
}
export function normalizeXiaoeCatalogCommandUrl(url) {
    const raw = url.trim();
    try {
        const parsed = new URL(raw);
        if (/^[^.]+\.h5\.xet\.pomoho\.com$/i.test(parsed.hostname)) {
            return parsed.toString();
        }
    }
    catch {
        return raw;
    }
    return normalizeXiaoeCatalogUrl(raw);
}
function extractXiaoeCourseId(courseUrl) {
    try {
        const parsed = new URL(courseUrl);
        const fromQuery = parsed.searchParams.get("course_id") || parsed.searchParams.get("product_id");
        if (fromQuery) {
            return fromQuery;
        }
        return parsed.pathname.match(/course_[A-Za-z0-9]+/)?.[0];
    }
    catch {
        return undefined;
    }
}
function getPlayableXiaoeBaseUrl(courseUrl) {
    try {
        const parsed = new URL(courseUrl);
        parsed.hostname = parsed.hostname.replace(/\.h5\.xet\.citv\.cn$/i, ".h5.xiaoecloud.com");
        parsed.hostname = parsed.hostname.replace(/\.h5\.xet\.pomoho\.com$/i, ".h5.xiaoecloud.com");
        parsed.pathname = "/";
        parsed.search = "";
        parsed.hash = "";
        return parsed;
    }
    catch {
        return undefined;
    }
}
export function normalizeXiaoeCatalogItemUrls(courseUrl, items) {
    const courseId = extractXiaoeCourseId(courseUrl);
    const baseUrl = getPlayableXiaoeBaseUrl(courseUrl);
    if (!courseId || !baseUrl) {
        return items;
    }
    return items.map((item) => {
        const resourceId = item.resource_id.trim();
        if (!resourceId.startsWith("v_")) {
            return item;
        }
        const url = new URL(`/p/course/video/${resourceId}`, baseUrl);
        url.searchParams.set("product_id", courseId);
        url.searchParams.set("course_id", courseId);
        url.searchParams.set("sub_course_id", "");
        return {
            ...item,
            url: url.toString()
        };
    });
}
export function getXiaoeBatchPaths(outDir) {
    const batchDir = path.join(outDir, ".xiaoe-batch");
    return {
        batchDir,
        statePath: path.join(batchDir, "batch-state.json"),
        indexPath: path.join(batchDir, "index.md")
    };
}
function nodeBackedOpenCliInvocation(directory, fallbackNodePath, fileExists) {
    const mainJsPath = path.join(directory, OPENCLI_MAIN_JS_RELATIVE);
    if (!fileExists(mainJsPath)) {
        return undefined;
    }
    const nodePath = fileExists(path.join(directory, "node.exe"))
        ? path.join(directory, "node.exe")
        : fallbackNodePath;
    return {
        command: nodePath,
        argsPrefix: [mainJsPath]
    };
}
function openCliSearchDirs(nodeDir, platform, envPath) {
    const dirs = [
        nodeDir,
        ...(envPath ?? "").split(path.delimiter),
        ...(platform === "win32"
            ? [
                "C:\\nvm4w\\nodejs",
                path.join(process.env.APPDATA ?? "", "npm"),
                path.join(process.env.LOCALAPPDATA ?? "", "Programs", "nodejs")
            ]
            : [])
    ].map((dir) => dir.trim()).filter(Boolean);
    return [...new Set(dirs)];
}
export function resolveOpenCliInvocationFromNodeDir(nodeExecPath, platform = process.platform, fileExists = existsSync, envPath = process.env.PATH ?? process.env.Path) {
    const nodeDir = path.dirname(nodeExecPath);
    for (const directory of openCliSearchDirs(nodeDir, platform, envPath)) {
        const invocation = nodeBackedOpenCliInvocation(directory, nodeExecPath, fileExists);
        if (invocation) {
            return invocation;
        }
    }
    const preferred = platform === "win32"
        ? [path.join(nodeDir, "opencli.cmd"), path.join(nodeDir, "opencli.ps1"), path.join(nodeDir, "opencli")]
        : [path.join(nodeDir, "opencli")];
    const executable = preferred.find((candidate) => fileExists(candidate)) ?? "opencli";
    return {
        command: executable,
        argsPrefix: []
    };
}
function parseJsonRows(raw, label) {
    let parsed;
    try {
        parsed = JSON.parse(raw);
    }
    catch (error) {
        throw new Error(`${label} did not return valid JSON: ${error instanceof Error ? error.message : String(error)}`);
    }
    if (Array.isArray(parsed)) {
        return parsed;
    }
    if (parsed && typeof parsed === "object") {
        const object = parsed;
        for (const key of ["data", "rows", "items", "result"]) {
            if (Array.isArray(object[key])) {
                return object[key];
            }
        }
    }
    throw new Error(`${label} did not return a JSON array`);
}
function optionalNumber(value) {
    if (typeof value === "number" && Number.isFinite(value)) {
        return value;
    }
    if (typeof value === "string" && value.trim()) {
        const parsed = Number.parseInt(value, 10);
        return Number.isFinite(parsed) ? parsed : undefined;
    }
    return undefined;
}
function coerceCatalogItem(value) {
    const row = value && typeof value === "object" ? value : {};
    return {
        ch: optionalNumber(row.ch),
        chapter: typeof row.chapter === "string" ? row.chapter : undefined,
        no: optionalNumber(row.no),
        title: String(row.title ?? ""),
        type: String(row.type ?? ""),
        resource_id: String(row.resource_id ?? row.resourceId ?? ""),
        url: String(row.url ?? ""),
        status: typeof row.status === "string" ? row.status : undefined
    };
}
export function parseXiaoeCatalogOutput(raw) {
    return parseJsonRows(raw, "opencli xiaoe catalog").map(coerceCatalogItem);
}
export function buildXiaoeCatalogCommandArgs(invocation, normalizedUrl) {
    return [
        ...invocation.argsPrefix,
        "xiaoe",
        "catalog",
        normalizedUrl,
        "-f",
        "json",
        "--site-session",
        "persistent",
        "--window",
        "background"
    ];
}
function resolveXiaoeCatalogBrowserTimeoutEnv() {
    const current = Number.parseInt(process.env.OPENCLI_BROWSER_COMMAND_TIMEOUT ?? "", 10);
    if (Number.isFinite(current) && current > XIAOE_CATALOG_BROWSER_TIMEOUT_SECONDS) {
        return String(current);
    }
    return String(XIAOE_CATALOG_BROWSER_TIMEOUT_SECONDS);
}
export async function readXiaoeCatalog(courseUrl) {
    const commandUrl = normalizeXiaoeCatalogCommandUrl(courseUrl);
    const invocation = resolveOpenCliInvocationFromNodeDir(process.execPath);
    const result = await runCommand(invocation.command, buildXiaoeCatalogCommandArgs(invocation, commandUrl), {
        env: {
            OPENCLI_BROWSER_COMMAND_TIMEOUT: resolveXiaoeCatalogBrowserTimeoutEnv()
        },
        timeoutMs: XIAOE_CATALOG_COMMAND_TIMEOUT_MS
    });
    return parseXiaoeCatalogOutput(result.stdout);
}
function splitCsv(value) {
    return (value ?? "")
        .split(",")
        .map((part) => part.trim())
        .filter(Boolean);
}
function normalizeText(value) {
    return (value ?? "").trim().toLowerCase();
}
function includesKeyword(value, keyword) {
    const normalizedKeyword = normalizeText(keyword);
    if (!normalizedKeyword) {
        return true;
    }
    return normalizeText(value).includes(normalizedKeyword);
}
function matchesStatus(itemStatus, statusFilter) {
    const tokens = splitCsv(statusFilter);
    if (tokens.length === 0 || tokens.some((token) => token.toLowerCase() === "all")) {
        return true;
    }
    const normalized = normalizeText(itemStatus);
    return tokens.some((token) => normalizeText(token) === normalized);
}
function matchesType(itemType, typeFilter) {
    const normalized = normalizeText(typeFilter);
    if (!normalized || normalized === "all") {
        return true;
    }
    return normalizeText(itemType) === normalized;
}
function normalizeResourceIdSet(resourceIds) {
    const ids = (resourceIds ?? []).map((item) => item.trim()).filter(Boolean);
    return ids.length > 0 ? new Set(ids) : undefined;
}
export function filterXiaoeCatalogItems(items, options) {
    const resourceIdSet = normalizeResourceIdSet(options.resourceIds);
    const filtered = items.filter((item) => {
        if (!matchesType(item.type, options.type)) {
            return false;
        }
        if (!matchesStatus(item.status, options.status)) {
            return false;
        }
        if (!includesKeyword(item.chapter, options.chapter)) {
            return false;
        }
        if (!includesKeyword(item.title, options.title)) {
            return false;
        }
        if (resourceIdSet && !resourceIdSet.has(item.resource_id)) {
            return false;
        }
        return true;
    });
    const limit = Number.isFinite(options.limit) ? Math.floor(options.limit) : 0;
    return limit > 0 ? filtered.slice(0, limit) : filtered;
}
async function pathExists(filePath) {
    try {
        await access(filePath);
        return true;
    }
    catch {
        return false;
    }
}
function expectedMarkdownPath(outDir, item) {
    return path.join(outDir, `${sanitizeFileName(item.title)}.md`);
}
class DuplicateTranscriptError extends Error {
}
async function assertNoDuplicateExistingTranscripts(outDir, items) {
    const byBody = new Map();
    for (const item of items) {
        if (isNonMediaCatalogItem(item))
            continue;
        const filePath = expectedMarkdownPath(outDir, item);
        if (!await pathExists(filePath))
            continue;
        const markdown = await readFile(filePath, "utf8");
        const body = markdown.replace(/^# [^\r\n]*\r?\n/, "").trim();
        if (Buffer.byteLength(body, "utf8") < 4096)
            continue;
        const hash = createHash("sha256").update(body).digest("hex");
        byBody.set(hash, [...(byBody.get(hash) ?? []), item.title]);
    }
    const duplicates = [...byBody.values()].filter((titles) => titles.length > 1);
    if (duplicates.length > 0) {
        throw new DuplicateTranscriptError(`Existing Markdown files share identical transcript bodies: ${duplicates.map((titles) => titles.join(" | ")).join("; ")}; refusing skip-existing until reviewed`);
    }
}
function isNonMediaCatalogItem(item) {
    return item.resource_id.trim().startsWith("i_");
}
function toStateItem(item) {
    return {
        resourceId: item.resource_id,
        title: item.title,
        url: item.url,
        chapter: item.chapter,
        status: "pending"
    };
}
function createState(options, normalizedCourseUrl, items, now) {
    const { courseUrl: _courseUrl, outDir: _outDir, catalogItems: _catalogItems, ...stateOptions } = options;
    return {
        version: 1,
        courseUrl: options.courseUrl,
        normalizedCourseUrl,
        outDir: options.outDir,
        options: stateOptions,
        startedAt: now.toISOString(),
        updatedAt: now.toISOString(),
        items: items.map(toStateItem)
    };
}
async function readExistingState(statePath) {
    try {
        const raw = await readFile(statePath, "utf8");
        return JSON.parse(raw);
    }
    catch {
        return undefined;
    }
}
async function writeBatchState(statePath, state) {
    await atomicWriteUtf8(statePath, JSON.stringify(state, null, 2));
}
function summarizeState(state) {
    const success = state.items.filter((item) => item.status === "success").length;
    const skipped = state.items.filter((item) => item.status === "skipped").length;
    const failed = state.items.filter((item) => item.status === "failed").length;
    const pending = state.items.filter((item) => item.status === "pending" || item.status === "running").length;
    return {
        total: state.items.length,
        success,
        skipped,
        failed,
        pending
    };
}
function markdownCell(value) {
    return redactSensitiveText(String(value ?? ""))
        .replace(/\r?\n/g, " ")
        .replace(/\|/g, "\\|")
        .trim();
}
function buildBatchIndexMarkdown(state) {
    const counts = summarizeState(state);
    const failed = state.items.filter((item) => item.status === "failed");
    const pending = state.items.filter((item) => item.status === "pending" || item.status === "running");
    const success = state.items.filter((item) => item.status === "success" || item.status === "skipped");
    const lines = [
        "# 小鹅通批量转写状态",
        "",
        `source: ${markdownCell(state.courseUrl)}`,
        `outDir: ${markdownCell(state.outDir)}`,
        `startedAt: ${markdownCell(state.startedAt)}`,
        `updatedAt: ${markdownCell(state.updatedAt)}`,
        "",
        "## Summary",
        "",
        `- total: ${counts.total}`,
        `- success: ${counts.success}`,
        `- skipped: ${counts.skipped}`,
        `- failed: ${counts.failed}`,
        `- pending: ${counts.pending}`,
        "",
        "## Failed",
        "",
        "| chapter | title | resourceId | error |",
        "| --- | --- | --- | --- |",
        ...failed.map((item) => `| ${markdownCell(item.chapter)} | ${markdownCell(item.title)} | ${markdownCell(item.resourceId)} | ${markdownCell(item.error)} |`),
        "",
        "## Pending",
        "",
        "| chapter | title | resourceId |",
        "| --- | --- | --- |",
        ...pending.map((item) => `| ${markdownCell(item.chapter)} | ${markdownCell(item.title)} | ${markdownCell(item.resourceId)} |`),
        "",
        "## Success",
        "",
        "| chapter | title | outputPath |",
        "| --- | --- | --- |",
        ...success.map((item) => `| ${markdownCell(item.chapter)} | ${markdownCell(item.title)} | ${markdownCell(item.outputPath)} |`),
        ""
    ];
    return lines.join("\n");
}
async function writeBatchIndex(indexPath, state) {
    await atomicWriteUtf8(indexPath, buildBatchIndexMarkdown(state));
}
function assertSafeBatchPaths(outDir, batchDir) {
    const resolvedOutDir = path.resolve(outDir);
    const resolvedBatchDir = path.resolve(batchDir);
    if (path.basename(resolvedBatchDir) !== ".xiaoe-batch" || path.dirname(resolvedBatchDir) !== resolvedOutDir) {
        throw new Error(`unsafe xiaoe batch directory: ${resolvedBatchDir}`);
    }
}
async function cleanupSuccessfulBatchArtifacts(outDir, batchDir, statePath, indexPath) {
    assertSafeBatchPaths(outDir, batchDir);
    await rm(indexPath, { force: true }).catch(() => undefined);
    await rm(statePath, { force: true }).catch(() => undefined);
    const remains = await readdir(batchDir).catch(() => []);
    if (remains.length === 0) {
        await rmdir(batchDir).catch(() => undefined);
    }
}
function mergeResumeState(current, previous) {
    const previousByResourceId = new Map(previous.items.map((item) => [item.resourceId, item]));
    current.items = current.items.map((item) => {
        const old = previousByResourceId.get(item.resourceId);
        if (!old) {
            return item;
        }
        if (old.status === "success" || old.status === "skipped") {
            return {
                ...item,
                status: old.status,
                outputPath: old.outputPath,
                provider: old.provider,
                coverage: old.coverage,
                coverageStatus: old.coverageStatus,
                warnings: old.warnings,
                providerAttempts: old.providerAttempts,
                error: old.error,
                startedAt: old.startedAt,
                finishedAt: old.finishedAt
            };
        }
        return item;
    });
    current.startedAt = previous.startedAt || current.startedAt;
    return current;
}
function validateResumeCourseUrl(previous, normalizedCourseUrl) {
    const previousNormalized = previous.normalizedCourseUrl || normalizeXiaoeCatalogUrl(previous.courseUrl);
    if (previousNormalized !== normalizedCourseUrl) {
        throw new Error("已有 batch-state.json 属于不同课程 URL；如需忽略旧 state 请使用 --restart true");
    }
}
function validateResumeOptions(previous, current) {
    if (previous.outDir !== current.outDir) {
        throw new Error("已有 batch-state.json 的 outDir changed；如需忽略旧 state 请使用 --restart true");
    }
    if (previous.options.provider !== current.options.provider) {
        throw new Error("已有 batch-state.json 的 provider changed；如需忽略旧 state 请使用 --restart true");
    }
    if (previous.options.type !== current.options.type || previous.options.status !== current.options.status) {
        throw new Error("已有 batch-state.json 的 filter changed；如需忽略旧 state 请使用 --restart true");
    }
}
function createDefaultDependencies() {
    return {
        readCatalog: readXiaoeCatalog,
        prepareTranscript: async ({ item, options, onStageChange }) => {
            const prepared = await prepareMediaToMarkdown({
                url: item.url,
                outDir: options.outDir,
                provider: options.provider,
                keepArtifacts: false,
                timeoutMinutes: options.timeoutMinutes,
                outputTitle: item.title,
                resourceId: item.resource_id
            }, undefined, onStageChange);
            return { item, prepared };
        },
        completeTranscript: async ({ prepared, options, onStageChange }) => {
            if (!prepared.prepared) {
                throw new Error("prepared media is missing");
            }
            return await completePreparedMediaToMarkdown(prepared.prepared, undefined, onStageChange);
        },
        runTranscript: async ({ item, options, onStageChange }) => {
            return await runXiaoeToMarkdown({
                url: item.url,
                outDir: options.outDir,
                provider: options.provider,
                keepArtifacts: false,
                timeoutMinutes: options.timeoutMinutes,
                outputTitle: item.title,
                resourceId: item.resource_id
            }, undefined, onStageChange);
        },
        reuseExistingTingwuTranscript: async ({ item, options }) => {
            if (options.reuseTingwuExisting === false || options.provider === "get") {
                return undefined;
            }
            return await runTingwuExistingTranscriptToMarkdown({
                title: item.title,
                outDir: options.outDir,
                preferredTitle: item.title,
                sourceUrl: item.url,
                resourceId: item.resource_id,
                searchUrls: options.tingwuExistingSearchUrls
            });
        },
        now: () => new Date()
    };
}
async function updateStateArtifacts(paths, state, now) {
    state.updatedAt = now().toISOString();
    await writeBatchState(paths.statePath, state);
    await writeBatchIndex(paths.indexPath, state);
}
async function tryReuseExistingTingwuTranscript(input) {
    const { deps, entry, options, paths, state, now, onProgress } = input;
    if (options.reuseTingwuExisting === false || options.provider === "get") {
        return false;
    }
    entry.itemState.status = "running";
    entry.itemState.startedAt = entry.itemState.startedAt ?? now().toISOString();
    await updateStateArtifacts(paths, state, now);
    await onProgress?.({
        stage: "item",
        status: "started",
        detail: `${entry.itemState.title}:reuse-tingwu-existing`,
        counts: summarizeState(state)
    });
    try {
        const transcript = await deps.reuseExistingTingwuTranscript({
            item: entry.sourceItem,
            options,
            onStageChange: async (event) => {
                await onProgress?.({
                    stage: "item",
                    status: event.status,
                    detail: `${entry.itemState.title}:${event.stage}${event.detail ? `:${event.detail}` : ""}`,
                    counts: summarizeState(state)
                });
            }
        });
        if (!transcript) {
            await onProgress?.({
                stage: "item",
                status: "completed",
                detail: `${entry.itemState.title}:reuse-tingwu-existing:not-found`,
                counts: summarizeState(state)
            });
            return false;
        }
        entry.itemState.status = "success";
        entry.itemState.outputPath = transcript.outputPath;
        entry.itemState.provider = transcript.provider;
        entry.itemState.coverage = transcript.coverage;
        entry.itemState.coverageStatus = transcript.coverageStatus;
        entry.itemState.warnings = transcript.warnings;
        entry.itemState.providerAttempts = transcript.providerAttempts;
        entry.itemState.finishedAt = now().toISOString();
        delete entry.itemState.error;
        await updateStateArtifacts(paths, state, now);
        await onProgress?.({
            stage: "item",
            status: "completed",
            detail: `${entry.itemState.title}:reuse-tingwu-existing:hit`,
            counts: summarizeState(state)
        });
        return true;
    }
    catch (error) {
        const message = redactSensitiveText(error instanceof Error ? error.message : String(error));
        entry.itemState.warnings = [
            ...(entry.itemState.warnings ?? []),
            options.tingwuExistingOnly
                ? `Existing Tingwu lookup failed; deferred media workflow: ${message}`
                : `Existing Tingwu lookup failed; continued to media workflow: ${message}`
        ];
        await updateStateArtifacts(paths, state, now);
        await onProgress?.({
            stage: "item",
            status: "completed",
            detail: `${entry.itemState.title}:reuse-tingwu-existing:lookup-failed`,
            counts: summarizeState(state)
        });
        return false;
    }
}
async function deferMediaWorkflow(input) {
    const { entry, paths, state, now, onProgress } = input;
    entry.itemState.status = "pending";
    entry.itemState.warnings = Array.from(new Set([
        ...(entry.itemState.warnings ?? []),
        "Deferred media workflow; no existing Tingwu transcript was imported in this pass"
    ]));
    await updateStateArtifacts(paths, state, now);
    await onProgress?.({
        stage: "item",
        status: "completed",
        detail: `${entry.itemState.title}:media-workflow:deferred`,
        counts: summarizeState(state)
    });
}
function shouldUsePrefetchPipeline(overrides) {
    return Boolean(overrides?.prepareTranscript || overrides?.completeTranscript);
}
export async function runXiaoeBatchTranscript(options, overrides, onProgress) {
    const outDir = options.outDir.trim();
    if (!outDir) {
        throw new Error("请先设置输出目录");
    }
    const courseUrl = options.courseUrl.trim();
    if (!courseUrl) {
        throw new Error("course-url is required");
    }
    const deps = {
        ...createDefaultDependencies(),
        ...overrides
    };
    const normalizedCourseUrl = normalizeXiaoeCatalogUrl(courseUrl);
    await onProgress?.({ stage: "catalog", status: "started", detail: normalizedCourseUrl });
    const sourceCatalogItems = options.catalogItems ?? await deps.readCatalog(courseUrl);
    const catalogItems = normalizeXiaoeCatalogItemUrls(courseUrl, sourceCatalogItems);
    const selectedItems = filterXiaoeCatalogItems(catalogItems, options);
    await onProgress?.({ stage: "catalog", status: "completed", detail: String(selectedItems.length) });
    if (options.dryRun) {
        const items = selectedItems.map(toStateItem);
        return {
            outDir,
            total: items.length,
            success: 0,
            skipped: 0,
            failed: 0,
            items
        };
    }
    if (options.skipExisting) {
        await assertNoDuplicateExistingTranscripts(outDir, selectedItems);
    }
    const paths = getXiaoeBatchPaths(outDir);
    const now = deps.now;
    const previousState = await readExistingState(paths.statePath);
    let state = createState({ ...options, courseUrl, outDir }, normalizedCourseUrl, selectedItems, now());
    if (previousState && options.resume && !options.restart) {
        validateResumeCourseUrl(previousState, normalizedCourseUrl);
        validateResumeOptions(previousState, state);
        state = mergeResumeState(state, previousState);
    }
    await updateStateArtifacts(paths, state, now);
    await onProgress?.({ stage: "batch", status: "started", counts: summarizeState(state) });
    const queue = [];
    for (const [index, itemState] of state.items.entries()) {
        if (itemState.status === "success" || itemState.status === "skipped") {
            continue;
        }
        const sourceItem = selectedItems[index];
        const expectedPath = expectedMarkdownPath(outDir, sourceItem);
        if (isNonMediaCatalogItem(sourceItem)) {
            itemState.status = "skipped";
            itemState.warnings = ["Skipped non-media image-text resource"];
            itemState.finishedAt = now().toISOString();
            await updateStateArtifacts(paths, state, now);
            await onProgress?.({ stage: "item", status: "completed", detail: itemState.title, counts: summarizeState(state) });
            continue;
        }
        if (options.skipExisting && await pathExists(expectedPath)) {
            itemState.status = "skipped";
            itemState.outputPath = expectedPath;
            itemState.finishedAt = now().toISOString();
            await updateStateArtifacts(paths, state, now);
            await onProgress?.({ stage: "item", status: "completed", detail: itemState.title, counts: summarizeState(state) });
            continue;
        }
        queue.push({ index, itemState, sourceItem });
    }
    if (shouldUsePrefetchPipeline(overrides)) {
        const transcriptOptions = { ...options, courseUrl, outDir };
        const relayStage = (itemState) => async (event) => {
            await onProgress?.({
                stage: "item",
                status: event.status,
                detail: `${itemState.title}:${event.stage}${event.detail ? `:${event.detail}` : ""}`,
                counts: summarizeState(state)
            });
        };
        const prepareEntry = async (entry) => {
            entry.itemState.status = "running";
            entry.itemState.startedAt = entry.itemState.startedAt ?? now().toISOString();
            const preparePromise = deps.prepareTranscript({
                item: entry.sourceItem,
                options: transcriptOptions,
                onStageChange: relayStage(entry.itemState)
            });
            await updateStateArtifacts(paths, state, now);
            try {
                return { prepared: await preparePromise };
            }
            catch (error) {
                return { error };
            }
        };
        let preparedPromise;
        for (const [queueIndex, entry] of queue.entries()) {
            if (await tryReuseExistingTingwuTranscript({
                deps,
                entry,
                options: transcriptOptions,
                paths,
                state,
                now,
                onProgress
            })) {
                continue;
            }
            if (transcriptOptions.tingwuExistingOnly) {
                await deferMediaWorkflow({ entry, paths, state, now, onProgress });
                continue;
            }
            if (!entry.sourceItem.url.trim()) {
                entry.itemState.status = "failed";
                entry.itemState.error = "catalog item has no playable url";
                entry.itemState.finishedAt = now().toISOString();
                await updateStateArtifacts(paths, state, now);
                await onProgress?.({
                    stage: "item",
                    status: "failed",
                    detail: entry.itemState.title,
                    counts: summarizeState(state)
                });
                continue;
            }
            preparedPromise ??= prepareEntry(entry);
            const preparedResult = await preparedPromise;
            const nextEntry = queue[queueIndex + 1];
            const canPrefetchNext = !transcriptOptions.tingwuExistingOnly
                && (transcriptOptions.reuseTingwuExisting === false || transcriptOptions.provider === "get")
                && Boolean(nextEntry?.sourceItem.url.trim());
            preparedPromise = canPrefetchNext && nextEntry ? prepareEntry(nextEntry) : undefined;
            if ("error" in preparedResult) {
                entry.itemState.status = "failed";
                entry.itemState.error = redactSensitiveText(preparedResult.error instanceof Error ? preparedResult.error.message : String(preparedResult.error));
                entry.itemState.finishedAt = now().toISOString();
                await updateStateArtifacts(paths, state, now);
                await onProgress?.({
                    stage: "item",
                    status: "failed",
                    detail: entry.itemState.title,
                    counts: summarizeState(state)
                });
                continue;
            }
            let duplicateDetected = false;
            try {
                const transcript = await deps.completeTranscript({
                    prepared: preparedResult.prepared,
                    options: transcriptOptions,
                    onStageChange: relayStage(entry.itemState)
                });
                await assertNoDuplicateExistingTranscripts(outDir, selectedItems);
                entry.itemState.status = "success";
                entry.itemState.outputPath = transcript.outputPath;
                entry.itemState.provider = transcript.provider;
                entry.itemState.coverage = transcript.coverage;
                entry.itemState.coverageStatus = transcript.coverageStatus;
                entry.itemState.warnings = transcript.warnings;
                entry.itemState.providerAttempts = transcript.providerAttempts;
                entry.itemState.finishedAt = now().toISOString();
                delete entry.itemState.error;
            }
            catch (error) {
                duplicateDetected = error instanceof DuplicateTranscriptError;
                entry.itemState.status = "failed";
                entry.itemState.error = redactSensitiveText(error instanceof Error ? error.message : String(error));
                entry.itemState.finishedAt = now().toISOString();
            }
            await updateStateArtifacts(paths, state, now);
            await onProgress?.({
                stage: "item",
                status: entry.itemState.status === "failed" ? "failed" : "completed",
                detail: entry.itemState.title,
                counts: summarizeState(state)
            });
            if (duplicateDetected) {
                break;
            }
        }
    }
    else {
        for (const { itemState, sourceItem } of queue) {
            const entry = { itemState, sourceItem };
            if (await tryReuseExistingTingwuTranscript({
                deps,
                entry,
                options: { ...options, courseUrl, outDir },
                paths,
                state,
                now,
                onProgress
            })) {
                continue;
            }
            if (options.tingwuExistingOnly) {
                await deferMediaWorkflow({ entry, paths, state, now, onProgress });
                continue;
            }
            if (!sourceItem.url.trim()) {
                itemState.status = "failed";
                itemState.error = "catalog item has no playable url";
                itemState.finishedAt = now().toISOString();
                await updateStateArtifacts(paths, state, now);
                await onProgress?.({
                    stage: "item",
                    status: "failed",
                    detail: itemState.title,
                    counts: summarizeState(state)
                });
                continue;
            }
            itemState.status = "running";
            itemState.startedAt = now().toISOString();
            await updateStateArtifacts(paths, state, now);
            let duplicateDetected = false;
            try {
                const transcript = await deps.runTranscript({
                    item: sourceItem,
                    options: { ...options, courseUrl, outDir },
                    onStageChange: async (event) => {
                        await onProgress?.({
                            stage: "item",
                            status: event.status,
                            detail: `${itemState.title}:${event.stage}${event.detail ? `:${event.detail}` : ""}`,
                            counts: summarizeState(state)
                        });
                    }
                });
                await assertNoDuplicateExistingTranscripts(outDir, selectedItems);
                itemState.status = "success";
                itemState.outputPath = transcript.outputPath;
                itemState.provider = transcript.provider;
                itemState.coverage = transcript.coverage;
                itemState.coverageStatus = transcript.coverageStatus;
                itemState.warnings = transcript.warnings;
                itemState.providerAttempts = transcript.providerAttempts;
                itemState.finishedAt = now().toISOString();
                delete itemState.error;
            }
            catch (error) {
                duplicateDetected = error instanceof DuplicateTranscriptError;
                itemState.status = "failed";
                itemState.error = redactSensitiveText(error instanceof Error ? error.message : String(error));
                itemState.finishedAt = now().toISOString();
            }
            await updateStateArtifacts(paths, state, now);
            await onProgress?.({
                stage: "item",
                status: itemState.status === "failed" ? "failed" : "completed",
                detail: itemState.title,
                counts: summarizeState(state)
            });
            if (duplicateDetected) {
                break;
            }
        }
    }
    const counts = summarizeState(state);
    const cleanComplete = counts.failed === 0 && counts.pending === 0;
    const cleanDeferred = options.tingwuExistingOnly && counts.failed === 0;
    if (cleanComplete) {
        await cleanupSuccessfulBatchArtifacts(outDir, paths.batchDir, paths.statePath, paths.indexPath);
        await onProgress?.({ stage: "batch", status: "completed", counts });
        return {
            outDir,
            total: counts.total,
            success: counts.success,
            skipped: counts.skipped,
            failed: counts.failed,
            items: state.items
        };
    }
    await writeBatchState(paths.statePath, state);
    await writeBatchIndex(paths.indexPath, state);
    await onProgress?.({ stage: "batch", status: cleanDeferred ? "completed" : "failed", counts });
    return {
        outDir,
        total: counts.total,
        success: counts.success,
        skipped: counts.skipped,
        failed: counts.failed,
        statePath: paths.statePath,
        items: state.items
    };
}
