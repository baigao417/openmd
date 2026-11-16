import { createServer } from "node:http";
import { URL } from "node:url";
import { readTranscriptConfig, readTranscriptState, updateTranscriptConfig, writeTranscriptState } from "./transcript-config-store.js";
export async function createTingwuHelperServer(options) {
    const rootDir = options.rootDir;
    let busy = false;
    async function readJsonBody(req) {
        const chunks = [];
        for await (const chunk of req) {
            chunks.push(Buffer.from(chunk));
        }
        if (chunks.length === 0) {
            return {};
        }
        return JSON.parse(Buffer.concat(chunks).toString("utf8"));
    }
    function send(res, status, data) {
        res.writeHead(status, {
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type",
            "Content-Type": "application/json; charset=utf-8"
        });
        res.end(JSON.stringify(data));
    }
    const server = createServer(async (req, res) => {
        if (req.method === "OPTIONS") {
            send(res, 204, {});
            return;
        }
        const url = new URL(req.url || "/", "http://127.0.0.1");
        if (req.method === "GET" && url.pathname === "/status") {
            send(res, 200, {
                ok: true,
                config: await readTranscriptConfig(rootDir),
                state: await readTranscriptState(rootDir)
            });
            return;
        }
        if (req.method === "GET" && url.pathname === "/config") {
            send(res, 200, await readTranscriptConfig(rootDir));
            return;
        }
        if (req.method === "POST" && url.pathname === "/config") {
            const body = await readJsonBody(req);
            const config = await updateTranscriptConfig(rootDir, {
                defaultOutputDir: typeof body.defaultOutputDir === "string" ? body.defaultOutputDir : undefined
            });
            send(res, 200, config);
            return;
        }
        if (req.method === "POST" && url.pathname === "/run-current") {
            if (busy) {
                send(res, 409, { ok: false, error: "A Tingwu export task is already running" });
                return;
            }
            const body = await readJsonBody(req);
            const config = await readTranscriptConfig(rootDir);
            const startedAt = new Date().toISOString();
            const request = {
                url: String(body.url || ""),
                title: String(body.title || ""),
                outDir: typeof body.outDirOverride === "string" && body.outDirOverride.trim()
                    ? body.outDirOverride.trim()
                    : config.defaultOutputDir,
                tabId: typeof body.tabId === "number" && Number.isFinite(body.tabId)
                    ? body.tabId
                    : undefined,
                pageId: typeof body.pageId === "string" && body.pageId.trim()
                    ? body.pageId.trim()
                    : undefined
            };
            if (!request.outDir.trim()) {
                send(res, 400, { ok: false, error: "请先设置默认保存目录" });
                return;
            }
            busy = true;
            await writeTranscriptState(rootDir, {
                status: "running",
                currentStage: "queued",
                log: ["任务已加入队列"],
                lastRun: {
                    url: request.url,
                    title: request.title,
                    provider: "tingwu",
                    tabId: request.tabId,
                    pageId: request.pageId,
                    startedAt
                }
            });
            void options.runCurrent(request, async (event) => {
                const current = await readTranscriptState(rootDir);
                const nextLog = [...(current.log ?? []), `${event.status}:${event.stage}${event.detail ? `:${event.detail}` : ""}`];
                await writeTranscriptState(rootDir, {
                    ...current,
                    status: current.status,
                    currentStage: event.stage,
                    log: nextLog.slice(-30)
                });
            }).then(async (result) => {
                busy = false;
                await writeTranscriptState(rootDir, {
                    status: "success",
                    currentStage: "done",
                    log: ["任务执行成功"],
                    lastRun: {
                        url: request.url,
                        title: request.title,
                        provider: result.provider,
                        tabId: request.tabId,
                        pageId: request.pageId,
                        outputPath: result.outputPath,
                        tempDir: result.tempDir,
                        startedAt,
                        finishedAt: new Date().toISOString()
                    }
                });
            }).catch(async (error) => {
                busy = false;
                await writeTranscriptState(rootDir, {
                    status: "error",
                    currentStage: "failed",
                    log: [error instanceof Error ? error.message : String(error)],
                    lastRun: {
                        url: request.url,
                        title: request.title,
                        provider: "tingwu",
                        tabId: request.tabId,
                        pageId: request.pageId,
                        error: error instanceof Error ? error.message : String(error),
                        startedAt,
                        finishedAt: new Date().toISOString()
                    }
                });
            });
            send(res, 202, { accepted: true, state: await readTranscriptState(rootDir) });
            return;
        }
        if (req.method === "POST" && url.pathname === "/shutdown") {
            send(res, 200, { ok: true });
            setTimeout(() => server.close(), 50);
            return;
        }
        send(res, 404, { ok: false, error: "Not found" });
    });
    await new Promise((resolve, reject) => {
        server.listen(options.port ?? 19827, "127.0.0.1", () => resolve());
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
