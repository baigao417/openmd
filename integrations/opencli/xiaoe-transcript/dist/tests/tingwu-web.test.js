import { describe, expect, test, vi } from "vitest";
import { openTranscriptRecord } from "../src/providers/tingwu-web.js";
describe("Tingwu upload record navigation", () => {
    test("accepts an automatically opened transcript without clicking a list row", async () => {
        const evaluate = vi.fn(async (code) => {
            if (code === "location.href")
                return "https://tingwu.aliyun.com/doc/transcripts/abc";
            if (code.includes("document.body.innerText"))
                return true;
            return undefined;
        });
        const page = { evaluate, tabs: vi.fn() };
        await openTranscriptRecord(page, "record.mp3", 1);
        expect(page.tabs).not.toHaveBeenCalled();
        expect(evaluate).not.toHaveBeenCalledWith(expect.stringContaining("clickable.click()"));
    });
    test("selects a transcript opened in another session tab", async () => {
        let currentUrl = "https://tingwu.aliyun.com/home";
        const page = {
            evaluate: vi.fn(async (code) => {
                if (code === "location.href")
                    return currentUrl;
                if (code.includes("document.body.innerText"))
                    return true;
                return undefined;
            }),
            tabs: vi.fn(async () => [{ page: "new-tab", url: "https://tingwu.aliyun.com/doc/transcripts/abc" }]),
            selectTab: vi.fn(async () => { currentUrl = "https://tingwu.aliyun.com/doc/transcripts/abc"; })
        };
        await openTranscriptRecord(page, "record.mp3", 1);
        expect(page.selectTab).toHaveBeenCalledWith("new-tab");
    });
    test("matches an uploaded filename with its audio suffix in the record list", async () => {
        let currentUrl = "https://tingwu.aliyun.com/home";
        const evaluate = vi.fn(async (code) => {
            if (code === "location.href")
                return currentUrl;
            if (code.includes("const expected =")) {
                expect(code).toContain("expected + '.mp3'");
                return true;
            }
            if (code.includes("setTimeout"))
                currentUrl = "https://tingwu.aliyun.com/doc/transcripts/abc";
            if (code.includes("document.body.innerText"))
                return true;
            return undefined;
        });
        const page = { evaluate, tabs: vi.fn(async () => []) };
        await openTranscriptRecord(page, "record.mp3", 1);
        expect(evaluate).toHaveBeenCalledWith(expect.stringContaining("const expected ="));
    });
    test("fails early when Start did not create a visible record", async () => {
        let clock = 0;
        const now = vi.spyOn(Date, "now").mockImplementation(() => (clock += 60_000));
        const page = {
            evaluate: vi.fn(async (code) => code === "location.href" ? "https://tingwu.aliyun.com/home" : false),
            tabs: vi.fn(async () => [])
        };
        try {
            await expect(openTranscriptRecord(page, "missing.mp3", 180))
                .rejects.toThrow("did not create a visible transcript record");
        }
        finally {
            now.mockRestore();
        }
    });
});
