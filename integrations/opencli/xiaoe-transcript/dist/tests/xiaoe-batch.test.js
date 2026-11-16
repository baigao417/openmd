import { access, mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { afterEach, describe, expect, test, vi } from "vitest";
import { buildXiaoeCatalogCommandArgs, filterXiaoeCatalogItems, getXiaoeBatchPaths, normalizeXiaoeCatalogCommandUrl, normalizeXiaoeCatalogItemUrls, normalizeXiaoeCatalogUrl, resolveOpenCliInvocationFromNodeDir, runXiaoeBatchTranscript } from "../src/pipeline/xiaoe-batch.js";
const tempDirs = [];
afterEach(async () => {
    while (tempDirs.length > 0) {
        const dir = tempDirs.pop();
        if (dir) {
            await rm(dir, { force: true, recursive: true });
        }
    }
});
async function makeTempDir(prefix) {
    const dir = await mkdtemp(path.join(os.tmpdir(), prefix));
    tempDirs.push(dir);
    return dir;
}
async function exists(filePath) {
    try {
        await access(filePath);
        return true;
    }
    catch {
        return false;
    }
}
function item(partial) {
    return {
        title: partial.title,
        resource_id: partial.resource_id,
        type: partial.type ?? "直播",
        url: partial.url ?? `https://app.h5.xet.citv.cn/v2/course/alive/${partial.resource_id}?type=2`,
        chapter: partial.chapter,
        status: partial.status,
        ch: partial.ch,
        no: partial.no
    };
}
const baseOptions = {
    courseUrl: "https://appabc.h5.xiaoecloud.com/p/course/course_1?x=1",
    provider: "auto",
    type: "直播",
    status: "all",
    limit: 0,
    skipExisting: true,
    resume: true,
    restart: false,
    dryRun: false,
    timeoutMinutes: 120,
    reuseTingwuExisting: false,
    tingwuExistingOnly: false
};
describe("xiaoe batch transcript pipeline", () => {
    test("resolves OpenCLI through a PATH node install when helper process PATH cannot spawn opencli", () => {
        const openCliDir = "C:\\nvm4w\\nodejs";
        const mainJs = path.join(openCliDir, "node_modules", "@jackwener", "opencli", "dist", "src", "main.js");
        const files = new Set([
            path.join(openCliDir, "node.exe"),
            mainJs
        ]);
        const invocation = resolveOpenCliInvocationFromNodeDir("E:\\Cursor\\cursor\\resources\\app\\resources\\helpers\\node.exe", "win32", (candidate) => files.has(candidate), openCliDir);
        expect(invocation).toEqual({
            command: path.join(openCliDir, "node.exe"),
            argsPrefix: [mainJs]
        });
    });
    test("runs catalog reads in a persistent background browser session", () => {
        expect(buildXiaoeCatalogCommandArgs({
            command: "node",
            argsPrefix: ["C:\\opencli\\dist\\src\\main.js"]
        }, "https://app.h5.xet.citv.cn/p/course/abc")).toEqual([
            "C:\\opencli\\dist\\src\\main.js",
            "xiaoe",
            "catalog",
            "https://app.h5.xet.citv.cn/p/course/abc",
            "-f",
            "json",
            "--site-session",
            "persistent",
            "--window",
            "background"
        ]);
    });
    test("normalizes xiaoecloud catalog URL and filters catalog rows", () => {
        expect(normalizeXiaoeCatalogUrl("https://appm1dwjnky5483.h5.xiaoecloud.com/p/course/abc?l=1"))
            .toBe("https://appm1dwjnky5483.h5.xet.citv.cn/p/course/abc?l=1");
        expect(normalizeXiaoeCatalogUrl("https://appttezzxyn5858.h5.xiaoeknow.com/p/course/ecourse/course_39wk6pepb0i3xTEPeoB79xWMusK?l_program=xe_know_pc"))
            .toBe("https://appttezzxyn5858.h5.xet.citv.cn/p/course/ecourse/course_39wk6pepb0i3xTEPeoB79xWMusK?l_program=xe_know_pc");
        expect(normalizeXiaoeCatalogUrl("https://appm1dwjnky5483.h5.xet.pomoho.com/p/course/ecourse/course_32Ha3c1VN3OQBVC1xxVoPdJKjJU?l_program=xe_know_pc&sub_course_list_mode=0"))
            .toBe("https://appm1dwjnky5483.h5.xet.citv.cn/p/course/ecourse/course_32Ha3c1VN3OQBVC1xxVoPdJKjJU?l_program=xe_know_pc&sub_course_list_mode=0");
        expect(normalizeXiaoeCatalogUrl("https://appm1dwjnky5483.h5.xet.citv.cn/p/course/abc?l=1"))
            .toBe("https://appm1dwjnky5483.h5.xet.citv.cn/p/course/abc?l=1");
        expect(normalizeXiaoeCatalogCommandUrl("https://appm1dwjnky5483.h5.xet.pomoho.com/p/course/ecourse/course_32Ha3c1VN3OQBVC1xxVoPdJKjJU?l_program=xe_know_pc&sub_course_list_mode=0"))
            .toBe("https://appm1dwjnky5483.h5.xet.pomoho.com/p/course/ecourse/course_32Ha3c1VN3OQBVC1xxVoPdJKjJU?l_program=xe_know_pc&sub_course_list_mode=0");
        expect(normalizeXiaoeCatalogCommandUrl("https://appm1dwjnky5483.h5.xiaoecloud.com/p/course/abc?l=1"))
            .toBe("https://appm1dwjnky5483.h5.xet.citv.cn/p/course/abc?l=1");
        const rows = [
            item({ title: "第一讲 直播", resource_id: "a_1", chapter: "入门", status: "100%" }),
            item({ title: "第二讲 直播", resource_id: "a_2", chapter: "进阶", status: "未学" }),
            item({ title: "第三讲 视频", resource_id: "v_1", type: "视频", chapter: "入门", status: "100%" })
        ];
        expect(filterXiaoeCatalogItems(rows, {
            type: "直播",
            status: "100%,未学",
            chapter: "入",
            title: "第一",
            resourceIds: ["a_1", "a_2"],
            limit: 0
        }).map((row) => row.resource_id)).toEqual(["a_1"]);
        expect(filterXiaoeCatalogItems(rows, {
            type: "all",
            status: "all",
            limit: 2
        }).map((row) => row.resource_id)).toEqual(["a_1", "a_2"]);
    });
    test("rewrites video resource catalog urls to playable video pages", () => {
        const rows = normalizeXiaoeCatalogItemUrls("https://appm1dwjnky5483.h5.xiaoecloud.com/p/course/ecourse/course_32Ha3c1VN3OQBVC1xxVoPdJKjJU?l_program=xe_know_pc", [
            item({
                title: "视频资源",
                resource_id: "v_6a09e172e4b0694c5bc4b65f",
                url: "https://appm1dwjnky5483.h5.xiaoecloud.com/v2/course/alive/v_6a09e172e4b0694c5bc4b65f?type=2"
            }),
            item({
                title: "直播资源",
                resource_id: "l_6a02fa3de4b0694c35079788",
                url: "https://appm1dwjnky5483.h5.xiaoecloud.com/v2/course/alive/l_6a02fa3de4b0694c35079788?type=2"
            })
        ]);
        expect(rows[0].url).toBe("https://appm1dwjnky5483.h5.xiaoecloud.com/p/course/video/v_6a09e172e4b0694c5bc4b65f?product_id=course_32Ha3c1VN3OQBVC1xxVoPdJKjJU&course_id=course_32Ha3c1VN3OQBVC1xxVoPdJKjJU&sub_course_id=");
        expect(rows[1].url).toBe("https://appm1dwjnky5483.h5.xiaoecloud.com/v2/course/alive/l_6a02fa3de4b0694c35079788?type=2");
    });
    test("dry-run returns selected items without writing batch state", async () => {
        const outDir = await makeTempDir("xiaoe-batch-dry-");
        const result = await runXiaoeBatchTranscript({
            ...baseOptions,
            outDir,
            dryRun: true,
            limit: 1
        }, {
            readCatalog: async () => [
                item({ title: "第一讲", resource_id: "a_1" }),
                item({ title: "第二讲", resource_id: "a_2" })
            ],
            runTranscript: vi.fn(),
            now: () => new Date("2026-05-29T00:00:00.000Z")
        });
        expect(result.total).toBe(1);
        expect(result.items?.map((row) => row.resourceId)).toEqual(["a_1"]);
        expect(await exists(getXiaoeBatchPaths(outDir).statePath)).toBe(false);
    });
    test("uses supplied catalog rows instead of re-reading catalog for selected batch items", async () => {
        const outDir = await makeTempDir("xiaoe-batch-supplied-catalog-");
        const readCatalog = vi.fn().mockRejectedValue(new Error("catalog should not be read"));
        const runTranscript = vi.fn().mockResolvedValue({
            outputPath: path.join(outDir, "第二讲.md"),
            provider: "tingwu",
            coverage: 1,
            tempDir: ""
        });
        const result = await runXiaoeBatchTranscript({
            ...baseOptions,
            outDir,
            resourceIds: ["a_2"],
            catalogItems: [
                item({ title: "第一讲", resource_id: "a_1" }),
                item({ title: "第二讲", resource_id: "a_2" })
            ],
            skipExisting: false
        }, {
            readCatalog,
            runTranscript,
            now: () => new Date("2026-05-29T00:00:00.000Z")
        });
        expect(readCatalog).not.toHaveBeenCalled();
        expect(runTranscript.mock.calls.map(([input]) => input.item.resource_id)).toEqual(["a_2"]);
        expect(result).toMatchObject({ total: 1, success: 1, skipped: 0, failed: 0 });
    });
    test("reuses an existing Tingwu transcript by title before downloading media", async () => {
        const outDir = await makeTempDir("xiaoe-batch-reuse-tingwu-");
        const reuseExistingTingwuTranscript = vi.fn().mockResolvedValue({
            outputPath: path.join(outDir, "第一讲.md"),
            provider: "tingwu",
            coverage: 1,
            coverageStatus: "complete",
            warnings: ["Reused existing Tingwu transcript by title: https://tingwu.aliyun.com/doc/transcripts/abc"],
            providerAttempts: [{
                    provider: "tingwu",
                    status: "success",
                    coverage: 1,
                    startedAt: "2026-05-29T00:00:00.000Z",
                    finishedAt: "2026-05-29T00:00:01.000Z"
                }],
            tempDir: ""
        });
        const runTranscript = vi.fn().mockRejectedValue(new Error("media download should not run"));
        const result = await runXiaoeBatchTranscript({
            ...baseOptions,
            outDir,
            skipExisting: false,
            reuseTingwuExisting: true,
            tingwuExistingSearchUrls: ["https://tingwu.aliyun.com/folders/256466"],
            catalogItems: [
                item({ title: "第一讲", resource_id: "a_1" })
            ]
        }, {
            reuseExistingTingwuTranscript,
            runTranscript,
            now: () => new Date("2026-05-29T00:00:00.000Z")
        });
        expect(reuseExistingTingwuTranscript).toHaveBeenCalledTimes(1);
        expect(reuseExistingTingwuTranscript).toHaveBeenCalledWith(expect.objectContaining({
            options: expect.objectContaining({
                tingwuExistingSearchUrls: ["https://tingwu.aliyun.com/folders/256466"]
            })
        }));
        expect(runTranscript).not.toHaveBeenCalled();
        expect(result).toMatchObject({ total: 1, success: 1, skipped: 0, failed: 0 });
        expect(result.items?.[0]).toMatchObject({
            provider: "tingwu",
            coverageStatus: "complete",
            outputPath: path.join(outDir, "第一讲.md")
        });
    });
    test("falls back to normal media workflow when no existing Tingwu title matches", async () => {
        const outDir = await makeTempDir("xiaoe-batch-reuse-tingwu-miss-");
        const reuseExistingTingwuTranscript = vi.fn().mockResolvedValue(undefined);
        const runTranscript = vi.fn().mockResolvedValue({
            outputPath: path.join(outDir, "第一讲.md"),
            provider: "tingwu",
            coverage: 0.99,
            tempDir: ""
        });
        const result = await runXiaoeBatchTranscript({
            ...baseOptions,
            outDir,
            skipExisting: false,
            reuseTingwuExisting: true,
            tingwuExistingSearchUrls: ["https://tingwu.aliyun.com/folders/256466"],
            catalogItems: [
                item({ title: "第一讲", resource_id: "a_1" })
            ]
        }, {
            reuseExistingTingwuTranscript,
            runTranscript,
            now: () => new Date("2026-05-29T00:00:00.000Z")
        });
        expect(reuseExistingTingwuTranscript).toHaveBeenCalledTimes(1);
        expect(runTranscript).toHaveBeenCalledTimes(1);
        expect(result).toMatchObject({ total: 1, success: 1, skipped: 0, failed: 0 });
    });
    test("can import only existing Tingwu transcripts and defer media downloads", async () => {
        const outDir = await makeTempDir("xiaoe-batch-tingwu-existing-only-");
        const paths = getXiaoeBatchPaths(outDir);
        const reuseExistingTingwuTranscript = vi.fn()
            .mockResolvedValueOnce({
            outputPath: path.join(outDir, "已有听悟.md"),
            provider: "tingwu",
            coverage: 1,
            coverageStatus: "complete",
            warnings: ["Reused existing Tingwu transcript by title: https://tingwu.aliyun.com/doc/transcripts/abc"],
            providerAttempts: [],
            tempDir: ""
        })
            .mockResolvedValueOnce(undefined);
        const runTranscript = vi.fn().mockRejectedValue(new Error("media download should be deferred"));
        const result = await runXiaoeBatchTranscript({
            ...baseOptions,
            outDir,
            skipExisting: false,
            reuseTingwuExisting: true,
            tingwuExistingOnly: true,
            catalogItems: [
                item({ title: "已有听悟", resource_id: "a_1" }),
                item({ title: "需要下载", resource_id: "a_2" })
            ]
        }, {
            reuseExistingTingwuTranscript,
            runTranscript,
            now: () => new Date("2026-05-29T00:00:00.000Z")
        });
        expect(reuseExistingTingwuTranscript).toHaveBeenCalledTimes(2);
        expect(runTranscript).not.toHaveBeenCalled();
        expect(result).toMatchObject({ total: 2, success: 1, skipped: 0, failed: 0, statePath: paths.statePath });
        expect(result.items?.[0]).toMatchObject({ status: "success", provider: "tingwu" });
        expect(result.items?.[1]).toMatchObject({ status: "pending" });
        expect(result.items?.[1].warnings?.join("\n")).toContain("Deferred media workflow");
        expect(await exists(paths.statePath)).toBe(true);
        expect(await exists(paths.indexPath)).toBe(true);
    });
    test("skips image-text resources instead of trying media discovery", async () => {
        const outDir = await makeTempDir("xiaoe-batch-skip-image-text-");
        const runTranscript = vi.fn().mockResolvedValue({
            outputPath: path.join(outDir, "直播课.md"),
            provider: "tingwu",
            coverage: 1,
            tempDir: ""
        });
        const result = await runXiaoeBatchTranscript({
            ...baseOptions,
            outDir,
            skipExisting: false,
            catalogItems: [
                item({ title: "加群须知", resource_id: "i_68c10522e4b0694ca1143fd7" }),
                item({ title: "直播课", resource_id: "l_68baf9cce4b0694c5b264616" })
            ]
        }, {
            readCatalog: async () => [],
            runTranscript,
            now: () => new Date("2026-05-29T00:00:00.000Z")
        });
        expect(runTranscript.mock.calls.map(([input]) => input.item.resource_id)).toEqual(["l_68baf9cce4b0694c5b264616"]);
        expect(result).toMatchObject({ total: 2, success: 1, skipped: 1, failed: 0 });
        expect(result.items?.find((row) => row.resourceId.startsWith("i_"))).toMatchObject({
            status: "skipped"
        });
    });
    test("uses catalog title/resource id for batch workflow output metadata", async () => {
        const outDir = await makeTempDir("xiaoe-batch-output-title-");
        const calls = [];
        const result = await runXiaoeBatchTranscript({
            ...baseOptions,
            outDir,
            resume: false,
            catalogItems: [{
                    title: "目录里的标题",
                    type: "直播",
                    resource_id: "v_resource_1",
                    url: "https://example.com/video"
                }]
        }, {
            prepareTranscript: async ({ item, options }) => {
                calls.push(`${item.resource_id}:${options.provider}`);
                return { item, outputPath: path.join(outDir, "目录里的标题.md") };
            },
            completeTranscript: async ({ prepared }) => ({
                outputPath: prepared.outputPath || path.join(outDir, "目录里的标题.md"),
                provider: "tingwu",
                coverage: 1,
                coverageStatus: "complete",
                warnings: [],
                providerAttempts: [{
                        provider: "tingwu",
                        status: "success",
                        coverage: 1,
                        startedAt: "2026-05-29T00:00:00.000Z",
                        finishedAt: "2026-05-29T00:00:01.000Z"
                    }],
                tempDir: "E:\\tmp\\job"
            }),
            now: () => new Date("2026-05-29T00:00:00.000Z")
        });
        expect(result.success).toBe(1);
        expect(calls).toEqual(["v_resource_1:auto"]);
        expect(result.items?.[0]).toMatchObject({
            coverageStatus: "complete",
            warnings: [],
            providerAttempts: [{ provider: "tingwu", status: "success", coverage: 1 }]
        });
    });
    test("prefetches the next item download while the current item is transcribing", async () => {
        const outDir = await makeTempDir("xiaoe-batch-prefetch-");
        const events = [];
        const prepareTranscript = vi.fn().mockImplementation(async ({ item: sourceItem }) => {
            events.push(`prepare-start:${sourceItem.resource_id}`);
            return {
                item: sourceItem,
                outputPath: path.join(outDir, `${sourceItem.title}.md`)
            };
        });
        const completeTranscript = vi.fn().mockImplementation(async ({ prepared }) => {
            events.push(`complete-start:${prepared.item.resource_id}`);
            if (prepared.item.resource_id === "a_1") {
                await new Promise((resolve) => setTimeout(resolve, 10));
            }
            events.push(`complete-end:${prepared.item.resource_id}`);
            return {
                outputPath: prepared.outputPath,
                provider: "tingwu",
                coverage: 1,
                tempDir: ""
            };
        });
        const result = await runXiaoeBatchTranscript({
            ...baseOptions,
            outDir,
            skipExisting: false
        }, {
            readCatalog: async () => [
                item({ title: "第一讲", resource_id: "a_1" }),
                item({ title: "第二讲", resource_id: "a_2" })
            ],
            prepareTranscript,
            completeTranscript,
            now: () => new Date("2026-05-29T00:00:00.000Z")
        });
        expect(result).toMatchObject({ total: 2, success: 2, skipped: 0, failed: 0 });
        expect(events.indexOf("prepare-start:a_2")).toBeGreaterThan(-1);
        expect(events.indexOf("prepare-start:a_2")).toBeLessThan(events.indexOf("complete-end:a_1"));
    });
    test("keeps prepared media aligned when an intervening item reuses Tingwu", async () => {
        const outDir = await makeTempDir("xiaoe-batch-reuse-prefetch-");
        const preparedIds = [];
        const completedIds = [];
        const result = await runXiaoeBatchTranscript({
            ...baseOptions,
            outDir,
            skipExisting: false,
            reuseTingwuExisting: true,
            catalogItems: [
                item({ title: "第一讲", resource_id: "a_1" }),
                item({ title: "第二讲", resource_id: "a_2" }),
                item({ title: "第三讲", resource_id: "a_3" })
            ]
        }, {
            reuseExistingTingwuTranscript: async ({ item: sourceItem }) => sourceItem.resource_id === "a_2"
                ? { outputPath: path.join(outDir, "第二讲.md"), provider: "tingwu", coverage: 1, tempDir: "" }
                : undefined,
            prepareTranscript: async ({ item: sourceItem }) => {
                preparedIds.push(sourceItem.resource_id);
                return { item: sourceItem, outputPath: path.join(outDir, `${sourceItem.title}.md`) };
            },
            completeTranscript: async ({ prepared }) => {
                completedIds.push(prepared.item.resource_id);
                return { outputPath: prepared.outputPath, provider: "tingwu", coverage: 1, tempDir: "" };
            },
            now: () => new Date("2026-05-29T00:00:00.000Z")
        });
        expect(result).toMatchObject({ total: 3, success: 3, failed: 0 });
        expect(preparedIds).toEqual(["a_1", "a_3"]);
        expect(completedIds).toEqual(["a_1", "a_3"]);
        expect(result.items?.[2].outputPath).toBe(path.join(outDir, "第三讲.md"));
    });
    test("continues when a prefetched item fails during download preparation", async () => {
        const outDir = await makeTempDir("xiaoe-batch-prefetch-fail-");
        const prepareTranscript = vi.fn().mockImplementation(async ({ item: sourceItem }) => {
            if (sourceItem.resource_id === "a_2") {
                throw new Error("download failed token=secret");
            }
            return {
                item: sourceItem,
                outputPath: path.join(outDir, `${sourceItem.title}.md`)
            };
        });
        const completeTranscript = vi.fn().mockImplementation(async ({ prepared }) => ({
            outputPath: prepared.outputPath,
            provider: "tingwu",
            coverage: 1,
            tempDir: ""
        }));
        const result = await runXiaoeBatchTranscript({
            ...baseOptions,
            outDir,
            skipExisting: false
        }, {
            readCatalog: async () => [
                item({ title: "第一讲", resource_id: "a_1" }),
                item({ title: "第二讲", resource_id: "a_2" }),
                item({ title: "第三讲", resource_id: "a_3" })
            ],
            prepareTranscript,
            completeTranscript,
            now: () => new Date("2026-05-29T00:00:00.000Z")
        });
        expect(result).toMatchObject({ total: 3, success: 2, skipped: 0, failed: 1 });
        expect(result.items?.find((row) => row.resourceId === "a_2")?.error).toContain("token=[REDACTED]");
        expect(completeTranscript).toHaveBeenCalledTimes(2);
    });
    test("skips existing markdown and removes state/index when all items succeed or skip", async () => {
        const outDir = await makeTempDir("xiaoe-batch-success-");
        await writeFile(path.join(outDir, "已存在.md"), "old", "utf8");
        const runTranscript = vi.fn().mockResolvedValue({
            outputPath: path.join(outDir, "新内容.md"),
            provider: "tingwu",
            coverage: 0.99,
            tempDir: ""
        });
        const result = await runXiaoeBatchTranscript({
            ...baseOptions,
            outDir
        }, {
            readCatalog: async () => [
                item({ title: "已存在", resource_id: "a_1" }),
                item({ title: "新内容", resource_id: "a_2" })
            ],
            runTranscript,
            now: () => new Date("2026-05-29T00:00:00.000Z")
        });
        const paths = getXiaoeBatchPaths(outDir);
        expect(runTranscript).toHaveBeenCalledTimes(1);
        expect(result).toMatchObject({ total: 2, success: 1, skipped: 1, failed: 0 });
        expect(result.statePath).toBeUndefined();
        expect(await exists(paths.statePath)).toBe(false);
        expect(await exists(paths.indexPath)).toBe(false);
        expect(await exists(paths.batchDir)).toBe(false);
    });
    test("refuses to skip different lessons with identical substantial transcript bodies", async () => {
        const outDir = await makeTempDir("xiaoe-batch-duplicate-existing-");
        const body = `## Transcript\n\n${"[00:01] same source text\n".repeat(250)}`;
        const first = `# 直播 16\n\n${body}`;
        const second = `# 直播 17\n\n${body}`;
        await writeFile(path.join(outDir, "直播 16.md"), first, "utf8");
        await writeFile(path.join(outDir, "直播 17.md"), second, "utf8");
        const runTranscript = vi.fn();
        await expect(runXiaoeBatchTranscript({ ...baseOptions, outDir }, {
            readCatalog: async () => [
                item({ title: "直播 16", resource_id: "l_16" }),
                item({ title: "直播 17", resource_id: "l_17" })
            ],
            runTranscript
        })).rejects.toThrow("identical transcript bodies");
        expect(runTranscript).not.toHaveBeenCalled();
        expect(await exists(getXiaoeBatchPaths(outDir).statePath)).toBe(false);
        expect(await readFile(path.join(outDir, "直播 16.md"), "utf8")).toBe(first);
        expect(await readFile(path.join(outDir, "直播 17.md"), "utf8")).toBe(second);
    });
    test("halts the queue when a newly generated lesson duplicates another body", async () => {
        const outDir = await makeTempDir("xiaoe-batch-duplicate-generated-");
        const body = `## Transcript\n\n${"[00:01] same source text\n".repeat(250)}`;
        const runTranscript = vi.fn(async ({ item: sourceItem }) => {
            const outputPath = path.join(outDir, `${sourceItem.title}.md`);
            await writeFile(outputPath, `# ${sourceItem.title}\n\n${body}`, "utf8");
            return { outputPath, provider: "tingwu", coverage: 1, tempDir: "" };
        });
        const result = await runXiaoeBatchTranscript({
            ...baseOptions,
            outDir,
            catalogItems: [
                item({ title: "第一讲", resource_id: "l_1" }),
                item({ title: "第二讲", resource_id: "l_2" }),
                item({ title: "第三讲", resource_id: "l_3" })
            ]
        }, { runTranscript });
        expect(runTranscript).toHaveBeenCalledTimes(2);
        expect(result).toMatchObject({ total: 3, success: 1, failed: 1, statePath: getXiaoeBatchPaths(outDir).statePath });
        expect(result.items?.map((row) => row.status)).toEqual(["success", "failed", "pending"]);
        expect(result.items?.[1].error).toContain("identical transcript bodies");
        expect(await exists(path.join(outDir, "第二讲.md"))).toBe(true);
    });
    test("continues after a single item failure and keeps state/index for resume", async () => {
        const outDir = await makeTempDir("xiaoe-batch-fail-");
        const runTranscript = vi.fn()
            .mockRejectedValueOnce(new Error("provider failed token=secret"))
            .mockResolvedValueOnce({
            outputPath: path.join(outDir, "第二讲.md"),
            provider: "get",
            coverage: 1,
            tempDir: ""
        });
        const result = await runXiaoeBatchTranscript({
            ...baseOptions,
            outDir,
            skipExisting: false
        }, {
            readCatalog: async () => [
                item({ title: "第一讲", resource_id: "a_1" }),
                item({ title: "第二讲", resource_id: "a_2" })
            ],
            runTranscript,
            now: () => new Date("2026-05-29T00:00:00.000Z")
        });
        const paths = getXiaoeBatchPaths(outDir);
        expect(runTranscript).toHaveBeenCalledTimes(2);
        expect(result).toMatchObject({ total: 2, success: 1, skipped: 0, failed: 1, statePath: paths.statePath });
        expect(await exists(paths.statePath)).toBe(true);
        expect(await exists(paths.indexPath)).toBe(true);
        const index = await readFile(paths.indexPath, "utf8");
        expect(index).toContain("第一讲");
        expect(index).toContain("token=[REDACTED]");
    });
    test("resume skips old success/skipped items and reruns failed/pending items", async () => {
        const outDir = await makeTempDir("xiaoe-batch-resume-");
        const paths = getXiaoeBatchPaths(outDir);
        await mkdir(paths.batchDir, { recursive: true });
        await writeFile(path.join(outDir, "已跳过.md"), "old", "utf8");
        await writeFile(paths.statePath, JSON.stringify({
            version: 1,
            courseUrl: baseOptions.courseUrl,
            normalizedCourseUrl: normalizeXiaoeCatalogUrl(baseOptions.courseUrl),
            outDir,
            options: {
                provider: "auto",
                type: "直播",
                status: "all",
                limit: 0,
                skipExisting: true,
                resume: true,
                restart: false,
                dryRun: false,
                timeoutMinutes: 120
            },
            startedAt: "2026-05-29T00:00:00.000Z",
            updatedAt: "2026-05-29T00:00:00.000Z",
            items: [
                { resourceId: "a_1", title: "已成功", url: "https://x.test/a_1", status: "success", outputPath: path.join(outDir, "已成功.md") },
                { resourceId: "a_2", title: "已跳过", url: "https://x.test/a_2", status: "skipped", outputPath: path.join(outDir, "已跳过.md") },
                { resourceId: "a_3", title: "旧失败", url: "https://x.test/a_3", status: "failed", error: "old error" },
                { resourceId: "a_4", title: "旧待处理", url: "https://x.test/a_4", status: "pending" }
            ]
        }), "utf8");
        const runTranscript = vi.fn().mockImplementation(async ({ item: sourceItem }) => ({
            outputPath: path.join(outDir, `${sourceItem.title}.md`),
            provider: "tingwu",
            coverage: 1,
            tempDir: ""
        }));
        const result = await runXiaoeBatchTranscript({
            ...baseOptions,
            outDir,
            skipExisting: false
        }, {
            readCatalog: async () => [
                item({ title: "已成功", resource_id: "a_1" }),
                item({ title: "已跳过", resource_id: "a_2" }),
                item({ title: "旧失败", resource_id: "a_3" }),
                item({ title: "旧待处理", resource_id: "a_4" })
            ],
            runTranscript,
            now: () => new Date("2026-05-29T01:00:00.000Z")
        });
        expect(runTranscript.mock.calls.map(([input]) => input.item.resource_id)).toEqual(["a_3", "a_4"]);
        expect(result).toMatchObject({ total: 4, success: 3, skipped: 1, failed: 0 });
        expect(await exists(paths.statePath)).toBe(false);
        expect(await exists(paths.indexPath)).toBe(false);
    });
    test("rejects resume when provider changed unless restart is true", async () => {
        const outDir = await makeTempDir("xiaoe-batch-resume-provider-");
        const paths = getXiaoeBatchPaths(outDir);
        await mkdir(paths.batchDir, { recursive: true });
        await writeFile(paths.statePath, JSON.stringify({
            version: 1,
            courseUrl: baseOptions.courseUrl,
            normalizedCourseUrl: normalizeXiaoeCatalogUrl(baseOptions.courseUrl),
            outDir,
            options: {
                provider: "tingwu",
                type: "直播",
                status: "all",
                limit: 0,
                skipExisting: true,
                resume: true,
                restart: false,
                dryRun: false,
                timeoutMinutes: 120
            },
            startedAt: "2026-05-29T00:00:00.000Z",
            updatedAt: "2026-05-29T00:00:00.000Z",
            items: [
                { resourceId: "v_1", title: "第一讲", url: "https://example.com/v_1", status: "pending" }
            ]
        }), "utf8");
        await expect(runXiaoeBatchTranscript({
            ...baseOptions,
            outDir,
            provider: "get",
            catalogItems: [item({ title: "第一讲", resource_id: "v_1" })]
        }, {
            runTranscript: async () => ({
                outputPath: path.join(outDir, "第一讲.md"),
                provider: "get",
                coverage: 1,
                tempDir: "E:\\tmp\\job"
            }),
            now: () => new Date("2026-05-29T00:01:00.000Z")
        })).rejects.toThrow("provider changed");
    });
});
