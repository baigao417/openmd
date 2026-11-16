import { describe, expect, test, vi } from "vitest";
import { runGetExportSubscriptionCommand, runGetSubscriptionsCommand } from "../src/app/get-export-command.js";
describe("runGetSubscriptionsCommand", () => {
    test("maps subscription summaries into command rows", async () => {
        const listSubscriptions = vi.fn().mockResolvedValue([
            {
                creatorId: "vnd1y25Y",
                creatorName: "冤种小徐",
                subjectUrl: "https://www.biji.com/subject/vnd1y25Y/DEFAULT",
                statsInfo: "1个内容 · 1人在用"
            }
        ]);
        const rows = await runGetSubscriptionsCommand({}, { listSubscriptions });
        expect(rows).toEqual([
            expect.objectContaining({
                creatorId: "vnd1y25Y",
                creatorName: "冤种小徐"
            })
        ]);
    });
});
describe("runGetExportSubscriptionCommand", () => {
    test("falls back to the new default output directory when --out is omitted", async () => {
        const runGetBatchExport = vi.fn().mockResolvedValue({
            creatorDir: "C:/example/output/抖音博主/冤种小徐_vnd1y25Y",
            creatorId: "vnd1y25Y",
            creatorName: "冤种小徐",
            total: 1,
            success: 1,
            skipped: 0,
            failed: 0,
            items: []
        });
        await runGetExportSubscriptionCommand({
            "subject-url": "https://www.biji.com/subject/vnd1y25Y/DEFAULT?followId=1217293&followName=%E5%86%A4%E7%A7%8D%E5%B0%8F%E5%BE%90"
        }, {
            resolveRuntimeOptions: vi.fn().mockResolvedValue({
                outDir: "C:/example/output/抖音博主",
                config: { defaultOutputDir: "C:/example/output/抖音博主" }
            }),
            runGetBatchExport
        });
        expect(runGetBatchExport).toHaveBeenCalledWith(expect.objectContaining({
            outDir: "C:/example/output/抖音博主"
        }), undefined);
    });
    test("passes explicit output and date filters through to the batch runner", async () => {
        const runGetBatchExport = vi.fn().mockResolvedValue({
            creatorDir: "E:/custom/out/冤种小徐_vnd1y25Y",
            creatorId: "vnd1y25Y",
            creatorName: "冤种小徐",
            total: 3,
            success: 2,
            skipped: 1,
            failed: 0,
            items: []
        });
        await runGetExportSubscriptionCommand({
            "subject-url": "https://www.biji.com/subject/vnd1y25Y/DEFAULT?followId=1217293&followName=%E5%86%A4%E7%A7%8D%E5%B0%8F%E5%BE%90",
            out: "E:/custom/out",
            "start-date": "2026-04-01",
            "end-date": "2026-04-08",
            "skip-existing": true
        }, {
            resolveRuntimeOptions: vi.fn().mockResolvedValue({
                outDir: "E:/custom/out",
                config: { defaultOutputDir: "E:/custom/out" }
            }),
            runGetBatchExport
        });
        expect(runGetBatchExport).toHaveBeenCalledWith(expect.objectContaining({
            subjectUrl: "https://www.biji.com/subject/vnd1y25Y/DEFAULT?followId=1217293&followName=%E5%86%A4%E7%A7%8D%E5%B0%8F%E5%BE%90",
            outDir: "E:/custom/out",
            startDate: "2026-04-01",
            endDate: "2026-04-08",
            skipExisting: true
        }), undefined);
    });
    test("passes comment export options through to the batch runner", async () => {
        const runGetBatchExport = vi.fn().mockResolvedValue({
            creatorDir: "E:/custom/out/Cesare凯撒自然健身_EJlxBxyJ",
            creatorId: "EJlxBxyJ",
            creatorName: "Cesare凯撒自然健身",
            total: 1,
            success: 1,
            skipped: 0,
            failed: 0,
            items: []
        });
        await runGetExportSubscriptionCommand({
            "subject-url": "https://www.biji.com/subject/EJlxBxyJ/DEFAULT?followId=1228442",
            "with-comments": true,
            "comment-limit": "6"
        }, {
            resolveRuntimeOptions: vi.fn().mockResolvedValue({
                outDir: "C:/example/output/抖音博主",
                config: { defaultOutputDir: "C:/example/output/抖音博主" }
            }),
            runGetBatchExport
        });
        expect(runGetBatchExport).toHaveBeenCalledWith(expect.objectContaining({
            includeComments: true,
            commentLimit: 6
        }), undefined);
    });
});
