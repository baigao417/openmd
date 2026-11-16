import { randomUUID, timingSafeEqual } from "node:crypto";
function headerValue(req, name) {
    const value = req.headers[name.toLowerCase()];
    return Array.isArray(value) ? value[0] : value;
}
function isLoopbackOrigin(origin) {
    try {
        const parsed = new URL(origin);
        return parsed.protocol === "http:" && ["127.0.0.1", "localhost", "[::1]", "::1"].includes(parsed.hostname);
    }
    catch {
        return false;
    }
}
function isChromeExtensionOrigin(origin) {
    try {
        return new URL(origin).protocol === "chrome-extension:";
    }
    catch {
        return false;
    }
}
function safeEqual(left, right) {
    const leftBuffer = Buffer.from(left);
    const rightBuffer = Buffer.from(right);
    return leftBuffer.length === rightBuffer.length && timingSafeEqual(leftBuffer, rightBuffer);
}
export function createHelperSecurity(options = {}) {
    const token = options.token ?? randomUUID();
    const allowedOrigins = new Set(options.allowedOrigins ?? []);
    function isTrustedOrigin(req) {
        const origin = headerValue(req, "origin");
        if (!origin) {
            return true;
        }
        return allowedOrigins.has(origin) || isChromeExtensionOrigin(origin) || isLoopbackOrigin(origin);
    }
    function corsHeaders(req) {
        const origin = headerValue(req, "origin");
        if (!origin || !isTrustedOrigin(req)) {
            return {};
        }
        return {
            "Access-Control-Allow-Origin": origin,
            "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type,X-OpenCLI-Helper-Token",
            "Vary": "Origin"
        };
    }
    function authorize(req, authOptions) {
        if (!isTrustedOrigin(req)) {
            return { ok: false, status: 403, error: "origin is not allowed" };
        }
        if (!authOptions.requireToken) {
            return { ok: true, status: 200 };
        }
        const requestToken = headerValue(req, "x-opencli-helper-token") ?? "";
        if (!requestToken || !safeEqual(requestToken, token)) {
            return { ok: false, status: 401, error: "helper token is required" };
        }
        return { ok: true, status: 200 };
    }
    return {
        token,
        isTrustedOrigin,
        corsHeaders,
        authorize
    };
}
