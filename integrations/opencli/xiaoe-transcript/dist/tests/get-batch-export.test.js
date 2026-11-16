import { mkdir, mkdtemp, readFile, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { describe, expect, test, vi } from "vitest";
import { buildGetTranscriptOnlyMarkdown, runGetBatchExport } from "../src/pipeline/get-batch-export.js";
describe("buildGetTranscriptOnlyMarkdown", () => {
    test("renders a title followed by transcript text without extra metadata", () => {
        const markdown = buildGetTranscriptOnlyMarkdown({
            title: "视频标题",
            content: "第一行\n第二行"
        });
        expect(markdown).toBe("# 视频标题\n\n第一行\n第二行\n");
    });
    test("appends a comments section when comment excerpts are provided", () => {
        const markdown = buildGetTranscriptOnlyMarkdown({
            title: "视频标题",
            content: "逐字稿正文",
            comments: [
                { nickname: "Flash", text: "第一条评论", diggCount: 6 },
                { nickname: "无计划", text: "第二条评论", diggCount: 0 }
            ]
        });
        expect(markdown).toContain("## 评论区摘录");
        expect(markdown).toContain("- Flash（6赞）：第一条评论");
        expect(markdown).toContain("- 无计划：第二条评论");
    });
});
describe("runGetBatchExport", () => {
    test("writes transcript files, dedupes duplicate titles, and records failures in index.json", async () => {
        const outDir = await mkdtemp(path.join(os.tmpdir(), "get-export-out-"));
        const collectTranscriptNotes = vi.fn().mockResolvedValue({
            creatorId: "vnd1y25Y",
            creatorName: "冤种小徐",
            subjectUrl: "https://www.biji.com/subject/vnd1y25Y/DEFAULT?followId=1217293&followName=%E5%86%A4%E7%A7%8D%E5%B0%8F%E5%BE%90",
            items: [
                {
                    noteId: "n1",
                    title: "同名标题",
                    publishedAt: "2026-04-08T16:10:00.000Z",
                    content: "第一篇文字稿",
                    contentSource: "media_text"
                },
                {
                    noteId: "n2",
                    title: "同名标题",
                    publishedAt: "2026-04-07T16:10:00.000Z",
                    content: "第二篇文字稿",
                    contentSource: "summary"
                },
                {
                    noteId: "n3",
                    title: "失败条目",
                    publishedAt: "2026-04-06T16:10:00.000Z",
                    error: "detail failed"
                }
            ]
        });
        const result = await runGetBatchExport({
            subjectUrl: "https://www.biji.com/subject/vnd1y25Y/DEFAULT?followId=1217293&followName=%E5%86%A4%E7%A7%8D%E5%B0%8F%E5%BE%90",
            outDir,
            skipExisting: false
        }, { collectTranscriptNotes });
        expect(result.creatorDir).toContain("冤种小徐_vnd1y25Y");
        expect(result.success).toBe(2);
        expect(result.failed).toBe(1);
        const first = path.join(result.creatorDir, "同名标题.md");
        const second = path.join(result.creatorDir, "同名标题 (2).md");
        expect(await readFile(first, "utf8")).toContain("第一篇文字稿");
        expect(await readFile(second, "utf8")).toContain("第二篇文字稿");
        const index = JSON.parse(await readFile(path.join(result.creatorDir, "index.json"), "utf8"));
        expect(index.items).toHaveLength(3);
        expect(index.items[2]).toEqual(expect.objectContaining({
            noteId: "n3",
            status: "failed",
            error: "detail failed"
        }));
    });
    test("skips existing notes using the previous index mapping and preserves file contents", async () => {
        const outDir = await mkdtemp(path.join(os.tmpdir(), "get-export-skip-"));
        const creatorDir = path.join(outDir, "冤种小徐_vnd1y25Y");
        const transcriptPath = path.join(creatorDir, "同名标题.md");
        await mkdir(creatorDir, { recursive: true });
        await writeFile(transcriptPath, "# 同名标题\n\n已有内容\n", "utf8");
        await writeFile(path.join(creatorDir, "index.json"), JSON.stringify({
            creatorId: "vnd1y25Y",
            creatorName: "冤种小徐",
            items: [
                {
                    noteId: "n1",
                    title: "同名标题",
                    status: "success",
                    transcriptPath
                }
            ]
        }, null, 2));
        const collectTranscriptNotes = vi.fn().mockResolvedValue({
            creatorId: "vnd1y25Y",
            creatorName: "冤种小徐",
            subjectUrl: "https://www.biji.com/subject/vnd1y25Y/DEFAULT?followId=1217293&followName=%E5%86%A4%E7%A7%8D%E5%B0%8F%E5%BE%90",
            items: [
                {
                    noteId: "n1",
                    title: "同名标题",
                    publishedAt: "2026-04-08T16:10:00.000Z",
                    content: "新的内容",
                    contentSource: "media_text"
                }
            ]
        });
        const result = await runGetBatchExport({
            subjectUrl: "https://www.biji.com/subject/vnd1y25Y/DEFAULT?followId=1217293&followName=%E5%86%A4%E7%A7%8D%E5%B0%8F%E5%BE%90",
            outDir,
            skipExisting: true
        }, { collectTranscriptNotes });
        expect(result.skipped).toBe(1);
        expect(await readFile(transcriptPath, "utf8")).toContain("已有内容");
    });
    test("filters notes by start and end date before writing files", async () => {
        const outDir = await mkdtemp(path.join(os.tmpdir(), "get-export-filter-"));
        const collectTranscriptNotes = vi.fn().mockResolvedValue({
            creatorId: "vnd1y25Y",
            creatorName: "冤种小徐",
            subjectUrl: "https://www.biji.com/subject/vnd1y25Y/DEFAULT?followId=1217293&followName=%E5%86%A4%E7%A7%8D%E5%B0%8F%E5%BE%90",
            items: [
                {
                    noteId: "n1",
                    title: "4月8日",
                    publishedAt: "2026-04-08T16:10:00.000Z",
                    content: "保留",
                    contentSource: "media_text"
                },
                {
                    noteId: "n2",
                    title: "4月1日",
                    publishedAt: "2026-04-01T16:10:00.000Z",
                    content: "过滤",
                    contentSource: "summary"
                }
            ]
        });
        const result = await runGetBatchExport({
            subjectUrl: "https://www.biji.com/subject/vnd1y25Y/DEFAULT?followId=1217293&followName=%E5%86%A4%E7%A7%8D%E5%B0%8F%E5%BE%90",
            outDir,
            startDate: "2026-04-05",
            endDate: "2026-04-08",
            skipExisting: false
        }, { collectTranscriptNotes });
        expect(result.total).toBe(1);
        expect(result.items.map((item) => item.noteId)).toEqual(["n1"]);
    });
    test("writes fetched douyin comments into the exported markdown when enabled", async () => {
        const outDir = await mkdtemp(path.join(os.tmpdir(), "get-export-comments-"));
        const collectTranscriptNotes = vi.fn().mockResolvedValue({
            creatorId: "EJlxBxyJ",
            creatorName: "Cesare凯撒自然健身",
            subjectUrl: "https://www.biji.com/subject/EJlxBxyJ/DEFAULT?followId=1228442",
            items: [
                {
                    noteId: "n1",
                    title: "家里迎来新成员",
                    publishedAt: "2026-04-08T16:10:00.000Z",
                    sourceUrl: "https://www.douyin.com/video/7627047128858328650",
                    content: "逐字稿正文",
                    contentSource: "linked_web"
                }
            ]
        });
        const createCommentCollector = vi.fn().mockReturnValue({
            fetchCommentsBySourceUrl: vi.fn().mockResolvedValue({
                status: "success",
                awemeId: "7627047128858328650",
                collectionName: "日常记录",
                collectionId: "mix-1",
                comments: [
                    { nickname: "Flash", text: "第一条评论", diggCount: 6 },
                    { nickname: "无计划", text: "第二条评论", diggCount: 0 }
                ]
            }),
            close: vi.fn().mockResolvedValue(undefined)
        });
        const result = await runGetBatchExport({
            subjectUrl: "https://www.biji.com/subject/EJlxBxyJ/DEFAULT?followId=1228442",
            outDir,
            skipExisting: false,
            includeComments: true,
            commentLimit: 6
        }, {
            collectTranscriptNotes,
            createCommentCollector
        });
        const transcriptPath = path.join(result.creatorDir, "日常记录", "家里迎来新成员.md");
        const markdown = await readFile(transcriptPath, "utf8");
        expect(markdown).toContain("逐字稿正文");
        expect(markdown).toContain("## 评论区摘录");
        expect(markdown).toContain("Flash（6赞）");
        expect(result.items[0]).toEqual(expect.objectContaining({
            noteId: "n1",
            status: "success",
            commentStatus: "success",
            commentCount: 2,
            awemeId: "7627047128858328650",
            collectionName: "日常记录",
            collectionId: "mix-1",
            transcriptPath
        }));
    });
    test("skips comment collection when the note source url is not a douyin video", async () => {
        const outDir = await mkdtemp(path.join(os.tmpdir(), "get-export-comment-skip-"));
        const collectTranscriptNotes = vi.fn().mockResolvedValue({
            creatorId: "EJlxBxyJ",
            creatorName: "Cesare凯撒自然健身",
            subjectUrl: "https://www.biji.com/subject/EJlxBxyJ/DEFAULT?followId=1228442",
            items: [
                {
                    noteId: "n1",
                    title: "非抖音来源",
                    publishedAt: "2026-04-08T16:10:00.000Z",
                    sourceUrl: "https://example.com/video/1",
                    content: "逐字稿正文",
                    contentSource: "linked_web"
                }
            ]
        });
        const fetchCommentsBySourceUrl = vi.fn().mockResolvedValue({
            status: "skipped",
            reason: "source url is not a douyin video"
        });
        const createCommentCollector = vi.fn().mockReturnValue({
            fetchCommentsBySourceUrl,
            close: vi.fn().mockResolvedValue(undefined)
        });
        const result = await runGetBatchExport({
            subjectUrl: "https://www.biji.com/subject/EJlxBxyJ/DEFAULT?followId=1228442",
            outDir,
            skipExisting: false,
            includeComments: true
        }, {
            collectTranscriptNotes,
            createCommentCollector
        });
        expect(fetchCommentsBySourceUrl).toHaveBeenCalledWith("https://example.com/video/1", 10);
        expect(result.items[0]).toEqual(expect.objectContaining({
            noteId: "n1",
            commentStatus: "skipped",
            commentCount: 0,
            commentError: "source url is not a douyin video"
        }));
        expect(await readFile(path.join(result.creatorDir, "非抖音来源.md"), "utf8")).not.toContain("## 评论区摘录");
    });
    test("keeps notes at the creator root when douyin metadata has no collection info", async () => {
        const outDir = await mkdtemp(path.join(os.tmpdir(), "get-export-no-collection-"));
        const collectTranscriptNotes = vi.fn().mockResolvedValue({
            creatorId: "EJlxBxyJ",
            creatorName: "Cesare凯撒自然健身",
            subjectUrl: "https://www.biji.com/subject/EJlxBxyJ/DEFAULT?followId=1228442",
            items: [
                {
                    noteId: "n1",
                    title: "未入合集视频",
                    publishedAt: "2026-04-08T16:10:00.000Z",
                    sourceUrl: "https://www.douyin.com/video/7627047128858328650",
                    content: "逐字稿正文",
                    contentSource: "linked_web"
                }
            ]
        });
        const createCommentCollector = vi.fn().mockReturnValue({
            fetchCommentsBySourceUrl: vi.fn().mockResolvedValue({
                status: "success",
                awemeId: "7627047128858328650",
                comments: []
            }),
            close: vi.fn().mockResolvedValue(undefined)
        });
        const result = await runGetBatchExport({
            subjectUrl: "https://www.biji.com/subject/EJlxBxyJ/DEFAULT?followId=1228442",
            outDir,
            skipExisting: false,
            includeComments: true
        }, {
            collectTranscriptNotes,
            createCommentCollector
        });
        expect(result.items[0]).toEqual(expect.objectContaining({
            noteId: "n1",
            collectionName: undefined,
            collectionId: undefined,
            transcriptPath: path.join(result.creatorDir, "未入合集视频.md")
        }));
    });
});
