import { createServer } from "node:http";
import { URL } from "node:url";
import { readXiaoeConfig, readXiaoeState, updateXiaoeConfig, writeXiaoeState } from "./config-store.js";
import { createHelperSecurity } from "./helper-security.js";
import { renderXiaoeBatchPickerHtml } from "./xiaoe-batch-picker.js";
import { terminateActiveCommands } from "../utils/command.js";
import { filterXiaoeCatalogItems, normalizeXiaoeCatalogItemUrls, normalizeXiaoeCatalogUrl } from "../pipeline/xiaoe-batch.js";
const HELPER_RESTARTED_LOG = "helper restarted while previous task was still marked running";
const HELPER_RESTARTED_ERROR = "helper restarted before the previous task completed";
function withoutRestartError(lastRun) {
    if (!lastRun) {
        return lastRun;
    }
    const { error: _error, ...rest } = lastRun;
    return rest;
}
function shouldResetRestartedState(state) {
    return state.status === "running"
        || (state.status === "error" && state.lastRun?.error === HELPER_RESTARTED_ERROR);
}
function resetRestartedState(state) {
    if (!shouldResetRestartedState(state)) {
        return state;
    }
    return {
        ...state,
        status: "idle",
        currentStage: "idle",
        log: [
            ...(state.log ?? []),
            HELPER_RESTARTED_LOG
        ].slice(-30),
        lastRun: withoutRestartError(state.lastRun)
    };
}
export async function createXiaoeHelperServer(options) {
    const rootDir = options.rootDir;
    const security = createHelperSecurity(options.security);
    let busy = false;
    let batchStatus = { status: "idle" };
    let catalogStatus = { status: "idle" };
    const initialState = await readXiaoeState(rootDir);
    if (shouldResetRestartedState(initialState)) {
        await writeXiaoeState(rootDir, resetRestartedState(initialState));
    }
    async function readVisibleState() {
        return await readXiaoeState(rootDir);
    }
    async function readJsonBody(req) {
        const chunks = [];
        for await (const chunk of req) {
            chunks.push(Buffer.from(chunk));
        }
        if (chunks.length === 0) {
            return {};
        }
        try {
            return JSON.parse(Buffer.concat(chunks).toString("utf8"));
        }
        catch (error) {
            throw new Error(`invalid JSON body: ${error instanceof Error ? error.message : String(error)}`);
        }
    }
    function send(req, res, status, data) {
        res.writeHead(status, {
            ...security.corsHeaders(req),
            "Content-Type": "application/json; charset=utf-8"
        });
        res.end(JSON.stringify(data));
    }
    function buildCatalogStatusKey(batchOptions) {
        return JSON.stringify({
            courseUrl: batchOptions.courseUrl,
            type: batchOptions.type,
            status: batchOptions.status,
            chapter: batchOptions.chapter || "",
            title: batchOptions.title || "",
            limit: batchOptions.limit
        });
    }
    function sendHtml(req, res, status, html) {
        res.writeHead(status, {
            ...security.corsHeaders(req),
            "Content-Type": "text/html; charset=utf-8"
        });
        res.end(html);
    }
    function rejectIfUnauthorized(req, res, requireToken) {
        const auth = security.authorize(req, { requireToken });
        if (auth.ok) {
            return false;
        }
        send(req, res, auth.status, { ok: false, error: auth.error });
        return true;
    }
    function optionalString(value) {
        return typeof value === "string" && value.trim().length > 0 ? value.trim() : undefined;
    }
    function parseBoolean(value, defaultValue) {
        if (typeof value === "boolean") {
            return value;
        }
        if (typeof value === "number") {
            return value !== 0;
        }
        if (typeof value === "string") {
            const normalized = value.trim().toLowerCase();
            if (["true", "1", "yes", "y", "on"].includes(normalized))
                return true;
            if (["false", "0", "no", "n", "off"].includes(normalized))
                return false;
        }
        return defaultValue;
    }
    function parseInteger(value, defaultValue, min) {
        const parsed = Number.parseInt(String(value ?? defaultValue), 10);
        if (!Number.isFinite(parsed) || parsed < min) {
            return defaultValue;
        }
        return parsed;
    }
    function parseResourceIds(value) {
        if (Array.isArray(value)) {
            const ids = value.map((item) => String(item).trim()).filter(Boolean);
            return ids.length > 0 ? ids : undefined;
        }
        if (typeof value === "string") {
            const ids = value.split(",").map((part) => part.trim()).filter(Boolean);
            return ids.length > 0 ? ids : undefined;
        }
        return undefined;
    }
    function parseStringList(value) {
        if (Array.isArray(value)) {
            const items = value.map((item) => String(item).trim()).filter(Boolean);
            return items.length > 0 ? items : undefined;
        }
        if (typeof value === "string") {
            const items = value.split(/[,\n;]/).map((part) => part.trim()).filter(Boolean);
            return items.length > 0 ? items : undefined;
        }
        return undefined;
    }
    function parseCatalogItems(value) {
        if (!Array.isArray(value)) {
            return undefined;
        }
        const items = value.map((item) => {
            const row = item && typeof item === "object" ? item : {};
            return {
                ch: typeof row.ch === "number" ? row.ch : undefined,
                chapter: typeof row.chapter === "string" ? row.chapter : undefined,
                no: typeof row.no === "number" ? row.no : undefined,
                title: String(row.title ?? ""),
                type: String(row.type ?? ""),
                resource_id: String(row.resource_id ?? row.resourceId ?? ""),
                url: String(row.url ?? ""),
                status: typeof row.status === "string" ? row.status : undefined
            };
        }).filter((item) => item.resource_id.trim().length > 0);
        return items.length > 0 ? items : undefined;
    }
    function providerFromBody(value, fallback) {
        return value === "auto" || value === "tingwu" || value === "get" ? value : fallback;
    }
    function buildBatchOptions(body, config) {
        return {
            courseUrl: String(body.courseUrl || body.url || "").trim(),
            outDir: optionalString(body.outDirOverride) ?? optionalString(body.outDir) ?? config.defaultOutputDir,
            provider: providerFromBody(body.provider, config.defaultProvider),
            type: optionalString(body.type) ?? "直播",
            status: optionalString(body.status) ?? "all",
            chapter: optionalString(body.chapter),
            title: optionalString(body.title),
            resourceIds: parseResourceIds(body.resourceIds ?? body["resource-id"]),
            limit: parseInteger(body.limit, 0, 0),
            skipExisting: parseBoolean(body.skipExisting ?? body["skip-existing"], true),
            resume: parseBoolean(body.resume, true),
            restart: parseBoolean(body.restart, false),
            dryRun: parseBoolean(body.dryRun ?? body["dry-run"], false),
            timeoutMinutes: parseInteger(body.timeoutMinutes ?? body["timeout-minutes"], 120, 1),
            reuseTingwuExisting: parseBoolean(body.reuseTingwuExisting ?? body["reuse-tingwu-existing"], true),
            tingwuExistingOnly: parseBoolean(body.tingwuExistingOnly ?? body["tingwu-existing-only"], false),
            tingwuExistingSearchUrls: parseStringList(body.tingwuExistingSearchUrls
                ?? body.tingwuExistingSearchUrl
                ?? body["tingwu-existing-search-urls"]
                ?? body["tingwu-existing-search-url"]),
            catalogItems: parseCatalogItems(body.catalogItems)
        };
    }
    const server = createServer(async (req, res) => {
        if (req.method === "OPTIONS") {
            if (rejectIfUnauthorized(req, res, false)) {
                return;
            }
            send(req, res, 204, {});
            return;
        }
        if (rejectIfUnauthorized(req, res, false)) {
            return;
        }
        const url = new URL(req.url || "/", "http://127.0.0.1");
        const mutatingRoute = req.method === "POST" && ["/config", "/catalog", "/batch-run", "/run-current", "/shutdown"].includes(url.pathname);
        if (mutatingRoute && rejectIfUnauthorized(req, res, true)) {
            return;
        }
        try {
            if (req.method === "GET" && url.pathname === "/status") {
                send(req, res, 200, {
                    ok: true,
                    security: { token: security.token },
                    config: await readXiaoeConfig(rootDir),
                    state: await readVisibleState()
                });
                return;
            }
            if (req.method === "GET" && url.pathname === "/batch") {
                sendHtml(req, res, 200, (options.renderBatchPickerHtml ?? renderXiaoeBatchPickerHtml)());
                return;
            }
            if (req.method === "GET" && url.pathname === "/batch-status") {
                send(req, res, 200, {
                    ok: true,
                    batch: batchStatus
                });
                return;
            }
            if (req.method === "GET" && url.pathname === "/catalog-status") {
                send(req, res, 200, {
                    ok: true,
                    catalog: catalogStatus
                });
                return;
            }
            if (req.method === "GET" && url.pathname === "/config") {
                send(req, res, 200, await readXiaoeConfig(rootDir));
                return;
            }
            if (req.method === "POST" && url.pathname === "/config") {
                const body = await readJsonBody(req);
                const config = await updateXiaoeConfig(rootDir, {
                    defaultOutputDir: typeof body.defaultOutputDir === "string" ? body.defaultOutputDir : undefined,
                    defaultProvider: body.defaultProvider === "auto" || body.defaultProvider === "tingwu" || body.defaultProvider === "get"
                        ? body.defaultProvider
                        : undefined
                });
                send(req, res, 200, config);
                return;
            }
            if (req.method === "POST" && url.pathname === "/catalog") {
                if (!options.readCatalog) {
                    send(req, res, 501, { ok: false, error: "catalog reader is not configured" });
                    return;
                }
                const body = await readJsonBody(req);
                const config = await readXiaoeConfig(rootDir);
                const batchOptions = buildBatchOptions(body, config);
                if (!batchOptions.courseUrl) {
                    send(req, res, 400, { ok: false, error: "courseUrl is required" });
                    return;
                }
                const normalizedUrl = normalizeXiaoeCatalogUrl(batchOptions.courseUrl);
                const catalogKey = buildCatalogStatusKey(batchOptions);
                catalogStatus = {
                    status: "running",
                    key: catalogKey,
                    courseUrl: batchOptions.courseUrl,
                    normalizedUrl,
                    startedAt: new Date().toISOString()
                };
                try {
                    const catalogItems = normalizeXiaoeCatalogItemUrls(batchOptions.courseUrl, await options.readCatalog(batchOptions.courseUrl));
                    const items = filterXiaoeCatalogItems(catalogItems, batchOptions);
                    catalogStatus = {
                        status: "success",
                        key: catalogKey,
                        courseUrl: batchOptions.courseUrl,
                        normalizedUrl,
                        total: items.length,
                        items,
                        startedAt: catalogStatus.startedAt,
                        finishedAt: new Date().toISOString()
                    };
                    send(req, res, 200, {
                        ok: true,
                        normalizedUrl,
                        total: items.length,
                        items
                    });
                }
                catch (error) {
                    catalogStatus = {
                        status: "error",
                        key: catalogKey,
                        courseUrl: batchOptions.courseUrl,
                        normalizedUrl,
                        error: error instanceof Error ? error.message : String(error),
                        startedAt: catalogStatus.startedAt,
                        finishedAt: new Date().toISOString()
                    };
                    send(req, res, 500, { ok: false, error: error instanceof Error ? error.message : String(error) });
                }
                return;
            }
            if (req.method === "POST" && url.pathname === "/batch-run") {
                if (!options.runBatch) {
                    send(req, res, 501, { ok: false, error: "batch runner is not configured" });
                    return;
                }
                if (busy) {
                    send(req, res, 409, { ok: false, error: "A transcript task is already running" });
                    return;
                }
                const body = await readJsonBody(req);
                const config = await readXiaoeConfig(rootDir);
                const batchOptions = buildBatchOptions(body, config);
                if (!batchOptions.courseUrl) {
                    send(req, res, 400, { ok: false, error: "courseUrl is required" });
                    return;
                }
                if (!batchOptions.outDir.trim()) {
                    send(req, res, 400, { ok: false, error: "请先设置默认保存目录" });
                    return;
                }
                busy = true;
                const startedAt = new Date().toISOString();
                batchStatus = {
                    status: "running",
                    startedAt
                };
                await writeXiaoeState(rootDir, {
                    status: "running",
                    currentStage: "batch-queued",
                    log: ["批量任务已加入队列"],
                    lastRun: {
                        url: batchOptions.courseUrl,
                        title: "小鹅通批量转写",
                        provider: batchOptions.provider,
                        startedAt
                    }
                });
                void options.runBatch(batchOptions, async (event) => {
                    batchStatus = {
                        ...batchStatus,
                        status: "running",
                        progress: event
                    };
                    const current = await readXiaoeState(rootDir);
                    const nextLog = [...(current.log ?? []), `${event.status}:${event.stage}${event.detail ? `:${event.detail}` : ""}`];
                    await writeXiaoeState(rootDir, {
                        ...current,
                        status: "running",
                        currentStage: `batch-${event.stage}`,
                        log: nextLog.slice(-30)
                    });
                }).then(async (result) => {
                    busy = false;
                    const finishedAt = new Date().toISOString();
                    const status = result.failed > 0 ? "error" : "success";
                    batchStatus = {
                        ...batchStatus,
                        status,
                        result,
                        finishedAt
                    };
                    await writeXiaoeState(rootDir, {
                        status,
                        currentStage: status === "success" ? "batch-done" : "batch-failed",
                        log: [status === "success" ? "批量任务执行成功" : "批量任务存在失败条目"],
                        lastRun: {
                            url: batchOptions.courseUrl,
                            title: "小鹅通批量转写",
                            provider: batchOptions.provider,
                            outputPath: result.statePath,
                            startedAt,
                            finishedAt
                        }
                    });
                }).catch(async (error) => {
                    busy = false;
                    const finishedAt = new Date().toISOString();
                    const message = error instanceof Error ? error.message : String(error);
                    batchStatus = {
                        ...batchStatus,
                        status: "error",
                        error: message,
                        finishedAt
                    };
                    await writeXiaoeState(rootDir, {
                        status: "error",
                        currentStage: "batch-failed",
                        log: [message],
                        lastRun: {
                            url: batchOptions.courseUrl,
                            title: "小鹅通批量转写",
                            provider: batchOptions.provider,
                            error: message,
                            startedAt,
                            finishedAt
                        }
                    });
                });
                send(req, res, 202, { accepted: true, batch: batchStatus });
                return;
            }
            if (req.method === "POST" && url.pathname === "/run-current") {
                if (busy) {
                    send(req, res, 409, { ok: false, error: "A transcript task is already running" });
                    return;
                }
                const body = await readJsonBody(req);
                const config = await readXiaoeConfig(rootDir);
                const startedAt = new Date().toISOString();
                const request = {
                    url: String(body.url || ""),
                    title: String(body.title || ""),
                    provider: body.provider === "tingwu" || body.provider === "get" || body.provider === "auto"
                        ? body.provider
                        : config.defaultProvider,
                    outDir: typeof body.outDirOverride === "string" && body.outDirOverride.trim()
                        ? body.outDirOverride.trim()
                        : config.defaultOutputDir,
                    timeoutMinutes: parseInteger(body.timeoutMinutes ?? body["timeout-minutes"], 180, 1)
                };
                if (!request.outDir.trim()) {
                    send(req, res, 400, { ok: false, error: "请先设置默认保存目录" });
                    return;
                }
                busy = true;
                await writeXiaoeState(rootDir, {
                    status: "running",
                    currentStage: "queued",
                    log: ["任务已加入队列"],
                    lastRun: {
                        url: request.url,
                        title: request.title,
                        provider: request.provider,
                        startedAt
                    }
                });
                void options.runCurrent(request, async (event) => {
                    const current = await readXiaoeState(rootDir);
                    const nextLog = [...(current.log ?? []), `${event.status}:${event.stage}${event.detail ? `:${event.detail}` : ""}`];
                    await writeXiaoeState(rootDir, {
                        ...current,
                        status: "running",
                        currentStage: event.stage,
                        download: event.download ?? current.download,
                        log: nextLog.slice(-30)
                    });
                }).then(async (result) => {
                    busy = false;
                    const current = await readXiaoeState(rootDir);
                    await writeXiaoeState(rootDir, {
                        ...current,
                        status: "success",
                        currentStage: "done",
                        log: ["任务执行成功"],
                        lastRun: {
                            url: request.url,
                            title: request.title,
                            provider: result.provider,
                            outputPath: result.outputPath,
                            tempDir: result.tempDir,
                            startedAt,
                            finishedAt: new Date().toISOString()
                        }
                    });
                }).catch(async (error) => {
                    busy = false;
                    const current = await readXiaoeState(rootDir);
                    const message = error instanceof Error ? error.message : String(error);
                    await writeXiaoeState(rootDir, {
                        ...current,
                        status: "error",
                        currentStage: "failed",
                        log: [message],
                        lastRun: {
                            url: request.url,
                            title: request.title,
                            provider: request.provider,
                            error: message,
                            startedAt,
                            finishedAt: new Date().toISOString()
                        }
                    });
                });
                send(req, res, 202, { accepted: true, state: await readXiaoeState(rootDir) });
                return;
            }
            if (req.method === "POST" && url.pathname === "/shutdown") {
                send(req, res, 200, { ok: true });
                setTimeout(() => {
                    terminateActiveCommands();
                    server.close(() => {
                        if (options.exitOnShutdown) {
                            process.exit(0);
                        }
                    });
                    if (options.exitOnShutdown) {
                        setTimeout(() => process.exit(0), 500);
                    }
                }, 50);
                return;
            }
            send(req, res, 404, { ok: false, error: "Not found" });
        }
        catch (error) {
            const message = error instanceof Error ? error.message : String(error);
            const status = message.includes("invalid JSON body") ? 400 : 500;
            send(req, res, status, { ok: false, error: message });
        }
    });
    await new Promise((resolve, reject) => {
        server.listen(options.port ?? 19826, "127.0.0.1", () => resolve());
        server.once("error", reject);
    });
    const address = server.address();
    if (!address || typeof address === "string") {
        throw new Error("Unable to determine helper server address");
    }
    return {
        baseUrl: `http://127.0.0.1:${address.port}`,
        close: async () => {
            await new Promise((resolve, reject) => {
                server.close((error) => {
                    if (error) {
                        reject(error);
                        return;
                    }
                    resolve();
                });
            });
        }
    };
}
