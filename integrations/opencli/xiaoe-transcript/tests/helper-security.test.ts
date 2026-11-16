import { describe, expect, test } from "vitest";

import { createHelperSecurity } from "../src/app/helper-security.js";

function req(headers: Record<string, string | undefined>) {
  return {
    headers
  } as unknown as import("node:http").IncomingMessage;
}

describe("helper security", () => {
  test("allows no-origin local tool requests", () => {
    const security = createHelperSecurity({ token: "secret-token" });
    const result = security.authorize(req({}), { requireToken: false });

    expect(result.ok).toBe(true);
    expect(result.status).toBe(200);
  });

  test("allows chrome extension origins", () => {
    const security = createHelperSecurity({ token: "secret-token" });
    const result = security.authorize(req({ origin: "chrome-extension://abcdefghijklmnop" }), { requireToken: false });

    expect(result.ok).toBe(true);
    expect(result.status).toBe(200);
  });

  test("rejects untrusted web origins before token checks", () => {
    const security = createHelperSecurity({ token: "secret-token" });
    const result = security.authorize(req({ origin: "https://evil.example" }), { requireToken: true });

    expect(result.ok).toBe(false);
    expect(result.status).toBe(403);
    expect(result.error).toContain("origin is not allowed");
  });

  test("requires a token for mutating helper requests", () => {
    const security = createHelperSecurity({ token: "secret-token" });
    const missing = security.authorize(req({ origin: "chrome-extension://abcdefghijklmnop" }), { requireToken: true });
    const valid = security.authorize(req({
      origin: "chrome-extension://abcdefghijklmnop",
      "x-opencli-helper-token": "secret-token"
    }), { requireToken: true });

    expect(missing.ok).toBe(false);
    expect(missing.status).toBe(401);
    expect(valid.ok).toBe(true);
  });

  test("returns CORS headers only for trusted browser origins", () => {
    const security = createHelperSecurity({ token: "secret-token" });

    expect(security.corsHeaders(req({ origin: "chrome-extension://abcdefghijklmnop" }))).toMatchObject({
      "Access-Control-Allow-Origin": "chrome-extension://abcdefghijklmnop",
      "Vary": "Origin"
    });
    expect(security.corsHeaders(req({ origin: "https://evil.example" }))).not.toHaveProperty("Access-Control-Allow-Origin");
  });
});
