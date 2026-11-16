import { mkdtemp, mkdir, readFile, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { describe, expect, test, vi } from "vitest";
import { runDouyinBatchExport } from "../src/pipeline/douyin-batch-export.js";
describe("runDouyinBatchExport", () => {
    test("writes transcripts under collection folders and does not persist video files", async () => {
        const outDir = await mkdtemp(path.join(os.tmpdir(), "douyin-batch-"));
        const collectVideos = vi.fn().mockResolvedValue({
            creatorName: "Cesare凯撒自然健身",
            creatorSecUid: "sec123",
            userAgent: "Mozilla/5.0",
            cookies: [],
            videos: [
                {
                    awemeId: "a1",
                    title: "视频1",
                    sourceUrl: "https://www.douyin.com/video/a1",
                    playUrl: "https://media.example.com/a1.mp4",
                    durationSec: 15,
                    diggCount: 10,
                    commentCount: 2,
                    shareCount: 1,
                    collectCount: 1,
                    creatorName: "Cesare凯撒自然健身",
                    creatorSecUid: "sec123",
                    collectionId: "mix1",
                    collectionName: "自然健身干货"
                }
            ]
        });
        const fetchCommentsBySourceUrl = vi.fn().mockResolvedValue({
            status: "success",
            comments: [{ nickname: "Flash", text: "评论1", diggCount: 3 }]
        });
        const createCommentCollector = {
            fetchCommentsBySourceUrl,
            close: vi.fn().mockResolvedValue(undefined)
        };
        const runCommand = vi.fn(async (command, args) => {
            if (command !== "ffmpeg") {
                throw new Error(`unexpected command: ${command}`);
            }
            const outputPath = args[args.length - 1];
            await mkdir(path.dirname(outputPath), { recursive: true });
            await writeFile(outputPath, "artifact", "utf8");
            return { exitCode: 0, stdout: "", stderr: "" };
        });
        const transcribeAudio = vi.fn().mockResolvedValue({
            segments: [
                { timestamp: "00:00", text: "逐字稿第一句" }
            ]
        });
        const result = await runDouyinBatchExport({
            secUid: "sec123",
            outDir,
            includeComments: true,
            skipExisting: false,
            keepArtifacts: false
        }, {
            collectVideos,
            createCommentCollector,
            runCommand,
            transcribeAudio
        });
        const creatorDir = path.join(outDir, "Cesare凯撒自然健身_sec123");
        const transcriptPath = path.join(creatorDir, "自然健身干货", "视频1.md");
        expect(result.creatorDir).toBe(creatorDir);
        expect(result.success).toBe(1);
        expect(await readFile(transcriptPath, "utf8")).toContain("## 评论区摘录");
        expect(result.items[0]?.videoPath).toBeUndefined();
    });
});
