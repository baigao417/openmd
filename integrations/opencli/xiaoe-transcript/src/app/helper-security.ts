import { randomUUID, timingSafeEqual } from "node:crypto";
import type { IncomingMessage } from "node:http";

export interface HelperSecurityOptions {
  token?: string;
  allowedOrigins?: string[];
}

export interface HelperAuthorizationResult {
  ok: boolean;
  status: number;
  error?: string;
}

export interface HelperSecurity {
  token: string;
  isTrustedOrigin(req: IncomingMessage): boolean;
  corsHeaders(req: IncomingMessage): Record<string, string>;
  authorize(req: IncomingMessage, options: { requireToken: boolean }): HelperAuthorizationResult;
}

function headerValue(req: IncomingMessage, name: string): string | undefined {
  const value = req.headers[name.toLowerCase()];
  return Array.isArray(value) ? value[0] : value;
}

function isLoopbackOrigin(origin: string): boolean {
  try {
    const parsed = new URL(origin);
    return parsed.protocol === "http:" && ["127.0.0.1", "localhost", "[::1]", "::1"].includes(parsed.hostname);
  } catch {
    return false;
  }
}

function isChromeExtensionOrigin(origin: string): boolean {
  try {
    return new URL(origin).protocol === "chrome-extension:";
  } catch {
    return false;
  }
}

function safeEqual(left: string, right: string): boolean {
  const leftBuffer = Buffer.from(left);
  const rightBuffer = Buffer.from(right);
  return leftBuffer.length === rightBuffer.length && timingSafeEqual(leftBuffer, rightBuffer);
}

export function createHelperSecurity(options: HelperSecurityOptions = {}): HelperSecurity {
  const token = options.token ?? randomUUID();
  const allowedOrigins = new Set(options.allowedOrigins ?? []);

  function isTrustedOrigin(req: IncomingMessage): boolean {
    const origin = headerValue(req, "origin");
    if (!origin) {
      return true;
    }
    return allowedOrigins.has(origin) || isChromeExtensionOrigin(origin) || isLoopbackOrigin(origin);
  }

  function corsHeaders(req: IncomingMessage): Record<string, string> {
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

  function authorize(req: IncomingMessage, authOptions: { requireToken: boolean }): HelperAuthorizationResult {
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
