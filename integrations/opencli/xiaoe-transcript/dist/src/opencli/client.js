import { existsSync } from "node:fs";
import path from "node:path";
import { runCommand } from "../utils/command.js";
const DAEMON_PORT = Number.parseInt(process.env.OPENCLI_DAEMON_PORT ?? "19825", 10);
const DAEMON_URL = `http://127.0.0.1:${DAEMON_PORT}`;
const OPENCLI_HEADERS = { "X-OpenCLI": "1" };
// The daemon needs an explicit context when several bridge profiles connect.
// Read at call time so a CLI override also reaches this direct HTTP client.
function profileContext() {
    const contextId = process.env.OPENCLI_PROFILE?.trim();
    return contextId ? { contextId } : {};
}
let commandCounter = 0;
function isRetryableBrowserError(error) {
    const message = error instanceof Error ? error.message : String(error);
    return message.includes("Detached while handling command")
        || message.includes("Inspected target navigated or closed")
        || message.includes("Target closed")
        || message.includes("Debugger is not attached");
}
function nextCommandId() {
    commandCounter += 1;
    return `xiaoe_${Date.now()}_${commandCounter}`;
}
async function request(pathname, init) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), init?.timeoutMs ?? 30000);
    try {
        return await fetch(`${DAEMON_URL}${pathname}`, {
            ...init,
            headers: {
                ...OPENCLI_HEADERS,
                ...(init?.headers ?? {})
            },
            signal: controller.signal
        });
    }
    finally {
        clearTimeout(timeout);
    }
}
export async function fetchDaemonStatus() {
    try {
        const query = new URLSearchParams(profileContext()).toString();
        const response = await request(`/status${query ? `?${query}` : ""}`, { method: "GET", timeoutMs: 2000 });
        if (!response.ok) {
            return null;
        }
        return await response.json();
    }
    catch {
        return null;
    }
}
export async function sendCommand(action, payload = {}, options = {}) {
    const timeoutMs = options.timeoutMs ?? 120000;
    const response = await request("/command", {
        method: "POST",
        timeoutMs: timeoutMs + 5000,
        headers: {
            "Content-Type": "application/json"
        },
        body: JSON.stringify({
            id: nextCommandId(),
            action,
            timeout: Math.ceil(timeoutMs / 1000),
            ...profileContext(),
            ...payload
        })
    });
    const result = await response.json();
    if (!result.ok) {
        throw new Error(result.error ?? `opencli command failed: ${action}`);
    }
    return result.data;
}
export async function sendCommandFull(action, payload = {}, options = {}) {
    const timeoutMs = options.timeoutMs ?? 120000;
    const response = await request("/command", {
        method: "POST",
        timeoutMs: timeoutMs + 5000,
        headers: {
            "Content-Type": "application/json"
        },
        body: JSON.stringify({
            id: nextCommandId(),
            action,
            timeout: Math.ceil(timeoutMs / 1000),
            ...profileContext(),
            ...payload
        })
    });
    const result = await response.json();
    if (!result.ok) {
        throw new Error(result.error ?? `opencli command failed: ${action}`);
    }
    return { data: result.data, page: result.page };
}
function wrapForEval(js) {
    const code = js.trim();
    if (!code) {
        return "undefined";
    }
    if (/^\([\s\S]*\)\s*\(.*\)\s*$/.test(code)) {
        return code;
    }
    if (/^(async\s+)?(\([^)]*\)|[A-Za-z_]\w*)\s*=>/.test(code)) {
        return `(${code})()`;
    }
    if (/^(async\s+)?function[\s(]/.test(code)) {
        return `(${code})()`;
    }
    return code;
}
const WAIT_FOR_DOM_STABLE_JS = (maxMs, quietMs) => `
new Promise(resolve => {
  if (!document.body) {
    setTimeout(() => resolve("nobody"), ${maxMs});
    return;
  }
  let timer = null;
  let cap = null;
  const done = (reason) => {
    clearTimeout(timer);
    clearTimeout(cap);
    observer.disconnect();
    resolve(reason);
  };
  const resetQuiet = () => {
    clearTimeout(timer);
    timer = setTimeout(() => done("quiet"), ${quietMs});
  };
  const observer = new MutationObserver(resetQuiet);
  observer.observe(document.body, { childList: true, subtree: true, attributes: true });
  resetQuiet();
  cap = setTimeout(() => done("capped"), ${maxMs});
})
`;
export class OpenCliPage {
    workspace;
    page;
    constructor(workspace, page) {
        this.workspace = workspace;
        this.page = page === undefined ? undefined : String(page);
    }
    sessionOptions() {
        return {
            session: this.workspace,
            surface: "browser"
        };
    }
    workspaceOptions() {
        return {
            ...this.sessionOptions(),
            ...(this.page !== undefined ? { page: this.page } : {})
        };
    }
    async goto(url) {
        let result;
        try {
            result = await sendCommandFull("navigate", {
                url,
                ...this.workspaceOptions()
            }, { timeoutMs: 5 * 60 * 1000 });
        }
        catch (error) {
            if ((error instanceof Error ? error.message : String(error)) !== "Navigation rejected.") {
                throw error;
            }
            const main = path.join(path.dirname(process.execPath), "node_modules", "@jackwener", "opencli", "dist", "src", "main.js");
            if (!existsSync(main))
                throw error;
            // Native open initializes the browser lease before navigating. Retrying
            // raw navigate repeats the rejected first-lease path on this extension.
            const opened = await runCommand(process.execPath, [main, "browser", this.workspace, "open", url, "--window", "foreground"], {
                timeoutMs: 5 * 60 * 1000
            });
            const native = JSON.parse(opened.stdout);
            if (typeof native.page !== "string" || !native.page) {
                throw new Error("OpenCLI navigation fallback did not return a page");
            }
            result = { data: native, page: native.page };
        }
        if (result.page) {
            this.page = result.page;
        }
        await this.evaluate(WAIT_FOR_DOM_STABLE_JS(1500, 500)).catch(() => undefined);
    }
    async bindToCurrentTab() {
        let result;
        try {
            result = await sendCommand("bind", {
                ...this.workspaceOptions()
            });
        }
        catch (error) {
            const message = error instanceof Error ? error.message : String(error);
            if (!message.includes("Unknown action: bind")) {
                throw error;
            }
            result = await sendCommand("bind-current", {
                ...this.workspaceOptions()
            });
        }
        if (result.page !== undefined) {
            this.page = result.page;
        }
        else if (result.tabId !== undefined) {
            this.page = String(result.tabId);
        }
    }
    async evaluate(js) {
        try {
            return await sendCommand("exec", {
                code: wrapForEval(js),
                ...this.workspaceOptions()
            });
        }
        catch (error) {
            if (!isRetryableBrowserError(error)) {
                throw error;
            }
            await new Promise((resolve) => setTimeout(resolve, 800));
            return await sendCommand("exec", {
                code: wrapForEval(js),
                ...this.workspaceOptions()
            });
        }
    }
    async getCookies(options = {}) {
        const cookies = await sendCommand("cookies", {
            ...options,
            ...this.workspaceOptions()
        });
        return Array.isArray(cookies) ? cookies : [];
    }
    async setFileInput(files, selector) {
        await sendCommand("set-file-input", {
            files,
            selector,
            ...this.workspaceOptions()
        }, { timeoutMs: 15 * 60 * 1000 });
    }
    async cdp(method, params = {}) {
        return await sendCommand("cdp", {
            cdpMethod: method,
            cdpParams: params,
            ...this.workspaceOptions()
        });
    }
    async tabs() {
        const result = await sendCommand("tabs", {
            op: "list",
            ...this.sessionOptions()
        });
        return Array.isArray(result) ? result : [];
    }
    async selectTab(page) {
        const result = await sendCommandFull("tabs", {
            op: "select",
            page,
            ...this.sessionOptions()
        });
        this.page = result.page ?? page;
    }
    async closeWindow() {
        await sendCommand("close-window", {
            ...this.workspaceOptions()
        }).catch(() => undefined);
    }
}
