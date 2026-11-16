import { afterEach, expect, test, vi } from "vitest";
import { fetchDaemonStatus, sendCommand, sendCommandFull } from "../src/opencli/client.js";

afterEach(() => {
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
});

test("routes status and both direct command APIs to the explicit bridge profile", async () => {
  vi.stubEnv("OPENCLI_PROFILE", " openmd ");
  const requests: Array<{ url: string; body?: Record<string, unknown> }> = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init: RequestInit) => {
    requests.push({ url, body: init.body ? JSON.parse(String(init.body)) : undefined });
    return Response.json(init.method === "GET"
      ? { ok: true, extensionConnected: true, port: 19825 }
      : { ok: true, data: { title: "sample" }, page: "sample-page" });
  }));
  expect((await fetchDaemonStatus())?.extensionConnected).toBe(true);
  await sendCommand("exec", { session: "profile-test" });
  await sendCommandFull("navigate", { session: "profile-test", url: "https://example.com" });
  expect(new URL(requests[0].url).searchParams.get("contextId")).toBe("openmd");
  expect(requests.slice(1).map(r => r.body?.contextId)).toEqual(["openmd", "openmd"]);
});

test("keeps the daemon default when no nonempty profile was requested", async () => {
  vi.stubEnv("OPENCLI_PROFILE", "  ");
  const fetchMock = vi.fn(async (_url: string, _init: RequestInit) => Response.json({ ok: true, data: {} }));
  vi.stubGlobal("fetch", fetchMock);
  await fetchDaemonStatus();
  await sendCommand("exec");
  expect(String(fetchMock.mock.calls[0]?.[0])).toBe("http://127.0.0.1:19825/status");
  expect(JSON.parse(String(fetchMock.mock.calls[1]?.[1]?.body))).not.toHaveProperty("contextId");
});
