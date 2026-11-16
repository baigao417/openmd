import { execFileSync, spawn } from "node:child_process";
import { readFile } from "node:fs/promises";
import { createServer } from "node:http";
import path from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";
import { listGetSubscriptions } from "../providers/get-subscription.js";
import { getGetHelperStatus, runGetHelperExport, shutdownGetHelper, updateGetHelperConfig } from "./get-helper-client.js";
function getProjectRoot() {
    const moduleDir = path.dirname(fileURLToPath(import.meta.url));
    const sourceOrDistRoot = path.resolve(moduleDir, "../..");
    return path.basename(sourceOrDistRoot) === "dist"
        ? path.dirname(sourceOrDistRoot)
        : sourceOrDistRoot;
}
async function loadWorkbenchAssets() {
    const webRoot = path.resolve(getProjectRoot(), "src/web/get-workbench");
    const [html, css, js] = await Promise.all([
        readFile(path.join(webRoot, "index.html"), "utf8"),
        readFile(path.join(webRoot, "styles.css"), "utf8"),
        readFile(path.join(webRoot, "app.js"), "utf8")
    ]);
    return { html, css, js };
}
function createStoppedHelperStatus() {
    return {
        ok: false,
        config: {
            defaultOutputDir: ""
        },
        state: {
            status: "stopped",
            total: 0,
            successCount: 0,
            skippedCount: 0,
            failedCount: 0,
            log: []
        }
    };
}
async function defaultStartHelper() {
    const status = await getGetHelperStatus();
    if (status) {
        return { started: true };
    }
    const projectRoot = getProjectRoot();
    if (process.platform === "win32") {
        const psCommand = `Start-Process -FilePath 'cmd.exe' -ArgumentList '/c','npx','tsx','src/app/get-helper-server.ts' -WorkingDirectory '${projectRoot.replace(/'/g, "''")}' -WindowStyle Hidden`;
        execFileSync("powershell.exe", ["-NoProfile", "-Command", psCommand], {
            cwd: projectRoot,
            stdio: "ignore",
            windowsHide: true
        });
    }
    else {
        spawn("npx", ["tsx", "src/app/get-helper-server.ts"], {
            cwd: projectRoot,
            detached: true,
            stdio: "ignore",
            windowsHide: true
        }).unref();
    }
    const deadline = Date.now() + 30_000;
    while (Date.now() < deadline) {
        const nextStatus = await getGetHelperStatus();
        if (nextStatus) {
            return { started: true };
        }
        await new Promise((resolve) => setTimeout(resolve, 300));
    }
    throw new Error("Get helper did not start in time");
}
async function defaultStopHelper() {
    const stopped = await shutdownGetHelper();
    return { stopped };
}
function send(res, status, body, contentType) {
    res.writeHead(status, {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
        "Content-Type": contentType
    });
    res.end(body);
}
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
export async function createGetWorkbenchServer(options) {
    const assets = await loadWorkbenchAssets();
    const deps = {
        getHelperStatus: async () => await getGetHelperStatus(),
        listSubscriptions: listGetSubscriptions,
        runExport: runGetHelperExport,
        updateConfig: updateGetHelperConfig,
        startHelper: defaultStartHelper,
        stopHelper: defaultStopHelper,
        ...options
    };
    const server = createServer(async (req, res) => {
        try {
            if (req.method === "OPTIONS") {
                send(res, 204, "{}", "application/json; charset=utf-8");
                return;
            }
            const url = new URL(req.url || "/", "http://127.0.0.1");
            if (req.method === "GET" && url.pathname === "/api/health") {
                send(res, 200, JSON.stringify({ ok: true }), "application/json; charset=utf-8");
                return;
            }
            if (req.method === "GET" && url.pathname === "/api/status") {
                const status = await deps.getHelperStatus();
                send(res, 200, JSON.stringify(status ?? createStoppedHelperStatus()), "application/json; charset=utf-8");
                return;
            }
            if (req.method === "GET" && url.pathname === "/api/subscriptions") {
                send(res, 200, JSON.stringify({ items: await deps.listSubscriptions() }), "application/json; charset=utf-8");
                return;
            }
            if (req.method === "POST" && url.pathname === "/api/helper/start") {
                send(res, 200, JSON.stringify(await deps.startHelper()), "application/json; charset=utf-8");
                return;
            }
            if (req.method === "POST" && url.pathname === "/api/helper/stop") {
                send(res, 200, JSON.stringify(await deps.stopHelper()), "application/json; charset=utf-8");
                return;
            }
            if (req.method === "POST" && url.pathname === "/api/config") {
                const body = await readJsonBody(req);
                send(res, 200, JSON.stringify(await deps.updateConfig(body)), "application/json; charset=utf-8");
                return;
            }
            if (req.method === "POST" && url.pathname === "/api/export") {
                const body = await readJsonBody(req);
                send(res, 202, JSON.stringify(await deps.runExport(body)), "application/json; charset=utf-8");
                return;
            }
            if (req.method === "POST" && url.pathname === "/shutdown") {
                send(res, 200, JSON.stringify({ ok: true }), "application/json; charset=utf-8");
                setTimeout(() => server.close(), 50);
                return;
            }
            if (req.method === "GET" && url.pathname === "/") {
                send(res, 200, assets.html, "text/html; charset=utf-8");
                return;
            }
            if (req.method === "GET" && url.pathname === "/styles.css") {
                send(res, 200, assets.css, "text/css; charset=utf-8");
                return;
            }
            if (req.method === "GET" && url.pathname === "/app.js") {
                send(res, 200, assets.js, "application/javascript; charset=utf-8");
                return;
            }
            send(res, 404, JSON.stringify({ ok: false, error: "Not found" }), "application/json; charset=utf-8");
        }
        catch (error) {
            send(res, 500, JSON.stringify({
                ok: false,
                error: error instanceof Error ? error.message : String(error)
            }), "application/json; charset=utf-8");
        }
    });
    await new Promise((resolve, reject) => {
        server.listen(options?.port ?? 19829, "127.0.0.1", () => resolve());
        server.once("error", reject);
    });
    const address = server.address();
    if (!address || typeof address === "string") {
        throw new Error("Unable to determine workbench server address");
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
