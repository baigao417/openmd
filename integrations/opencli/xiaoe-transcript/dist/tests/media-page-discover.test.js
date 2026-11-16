import { mkdtemp } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { beforeEach, describe, expect, test, vi } from "vitest";
const { evaluateMock, getCookiesMock, closeWindowMock, runCommandMock } = vi.hoisted(() => ({
    evaluateMock: vi.fn(),
    getCookiesMock: vi.fn(),
    closeWindowMock: vi.fn(),
    runCommandMock: vi.fn()
}));
vi.mock("../src/opencli/client.js", () => ({
    OpenCliPage: class {
        async goto() {
            return undefined;
        }
        async evaluate(code) {
            return await evaluateMock(code);
        }
        async getCookies() {
            return await getCookiesMock();
        }
        async closeWindow() {
            await closeWindowMock();
        }
    }
}));
vi.mock("../src/utils/command.js", () => ({
    runCommand: runCommandMock
}));
import { discoverMediaOnPage } from "../src/pipeline/media-page.js";
describe("discoverMediaOnPage", () => {
    beforeEach(() => {
        evaluateMock.mockReset();
        getCookiesMock.mockReset();
        closeWindowMock.mockReset();
        runCommandMock.mockReset();
    });
    test("prefers xiaoe play-url before browser page probes for known Xiaoe urls", async () => {
        const tempDir = await mkdtemp(path.join(os.tmpdir(), "xiaoe-discover-"));
        const pageUrl = "https://app.example.xiaoecloud.com/v4/course/alive/l_123?app_id=app123";
        runCommandMock.mockResolvedValue({
            exitCode: 0,
            stdout: JSON.stringify([
                {
                    title: "直播回放",
                    m3u8_url: "https://encrypt-k-vod.xet.tech/bucket/playlist_eof.m3u8?sign=ok",
                    method: "alive_lookback_api"
                }
            ]),
            stderr: ""
        });
        const media = await discoverMediaOnPage(pageUrl, tempDir);
        expect(media.mediaUrl).toBe("https://encrypt-k-vod.xet.tech/bucket/playlist_eof.m3u8?sign=ok");
        expect(media.pageTitle).toBe("直播回放");
        expect(evaluateMock).not.toHaveBeenCalled();
        expect(getCookiesMock).not.toHaveBeenCalled();
        expect(closeWindowMock).not.toHaveBeenCalled();
    });
    test("falls back to opencli xiaoe play-url when page probes find no media candidates", async () => {
        const tempDir = await mkdtemp(path.join(os.tmpdir(), "xiaoe-discover-"));
        const pageUrl = "https://example.com/video-without-media";
        evaluateMock.mockImplementation(async (code) => {
            if (code.includes("document.title")) {
                return { pageTitle: "直播回放", candidates: [], performanceCandidates: [] };
            }
            if (code.includes("get_lookback_list")) {
                return { code: 0, data: [] };
            }
            if (code.includes("window.__opencli_net")) {
                return [];
            }
            if (code.includes("catCatchAddMedia")) {
                return [];
            }
            if (code.includes("navigator.userAgent")) {
                return "Mozilla/5.0";
            }
            return true;
        });
        getCookiesMock.mockResolvedValue([]);
        runCommandMock.mockResolvedValue({
            exitCode: 0,
            stdout: JSON.stringify([
                {
                    title: "直播回放",
                    m3u8_url: "https://encrypt-k-vod.xet.tech/bucket/playlist_eof.m3u8?sign=ok",
                    method: "alive_lookback_api"
                }
            ]),
            stderr: ""
        });
        const media = await discoverMediaOnPage(pageUrl, tempDir);
        expect(media.mediaUrl).toBe("https://encrypt-k-vod.xet.tech/bucket/playlist_eof.m3u8?sign=ok");
        expect(media.candidateMediaUrls).toEqual([
            "https://encrypt-k-vod.xet.tech/bucket/playlist_eof.m3u8?sign=ok"
        ]);
        expect(runCommandMock).toHaveBeenCalledWith(process.platform === "win32" ? "E:\\Git\\Git\\bin\\bash.exe" : "opencli", process.platform === "win32"
            ? ["-lc", `opencli xiaoe play-url '${pageUrl}' -f json`]
            : ["xiaoe", "play-url", pageUrl, "-f", "json"], { timeoutMs: 120000, allowFailure: true });
        expect(closeWindowMock).toHaveBeenCalled();
    });
    test("retries direct xiaoe play-url before browser probes when the first attempts return no media", async () => {
        const tempDir = await mkdtemp(path.join(os.tmpdir(), "xiaoe-discover-"));
        const pageUrl = "https://app.example.xiaoecloud.com/v4/course/alive/l_123?app_id=app123";
        runCommandMock
            .mockResolvedValueOnce({
            exitCode: 0,
            stdout: "",
            stderr: ""
        })
            .mockResolvedValueOnce({
            exitCode: 1,
            stdout: "",
            stderr: "Page not found — stale page identity"
        })
            .mockResolvedValueOnce({
            exitCode: 0,
            stdout: JSON.stringify([{ m3u8_url: "https://btt-vod.xiaoeknow.com/bucket/playlist_eof.m3u8?sign=ok" }]),
            stderr: ""
        });
        const media = await discoverMediaOnPage(pageUrl, tempDir);
        expect(media.mediaUrl).toBe("https://btt-vod.xiaoeknow.com/bucket/playlist_eof.m3u8?sign=ok");
        const playUrlCalls = runCommandMock.mock.calls.filter(([, args]) => {
            return Array.isArray(args) && args.join(" ").includes("xiaoe play-url");
        });
        expect(playUrlCalls).toHaveLength(3);
        expect(evaluateMock).not.toHaveBeenCalled();
        expect(closeWindowMock).not.toHaveBeenCalled();
    });
});
