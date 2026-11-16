import { afterEach, describe, expect, test, vi } from "vitest";
import { OpenCliPage } from "../src/opencli/client.js";
afterEach(() => vi.unstubAllGlobals());
describe("OpenCliPage navigation", () => {
    test("rebuilds a rejected tab lease once before navigating", async () => {
        const actions = [];
        vi.stubGlobal("fetch", vi.fn(async (_url, init) => {
            const body = JSON.parse(String(init.body));
            actions.push(body);
            if (body.action === "navigate" && actions.filter((item) => item.action === "navigate").length === 1) {
                return Response.json({ id: body.id, ok: false, error: "Navigation rejected." });
            }
            return Response.json({ id: body.id, ok: true, page: "fresh-page", data: {} });
        }));
        const page = new OpenCliPage("navigation-test", "stale-page");
        await page.goto("https://tingwu.aliyun.com/");
        expect(actions.map((item) => item.action)).toEqual(["navigate", "close-window", "navigate", "exec"]);
        expect(actions[0].page).toBe("stale-page");
        expect(actions[2].page).toBeUndefined();
        expect(actions[3].page).toBe("fresh-page");
    });
    test("surfaces repeated navigation rejection without further retries", async () => {
        const actions = [];
        vi.stubGlobal("fetch", vi.fn(async (_url, init) => {
            const body = JSON.parse(String(init.body));
            actions.push(String(body.action));
            return Response.json(body.action === "navigate"
                ? { id: body.id, ok: false, error: "Navigation rejected." }
                : { id: body.id, ok: true, data: {} });
        }));
        await expect(new OpenCliPage("navigation-test").goto("https://tingwu.aliyun.com/"))
            .rejects.toThrow("Navigation rejected.");
        expect(actions).toEqual(["navigate", "close-window", "navigate"]);
    });
});
