import { defaultOutputDirectory } from "../src/utils/output-directory.js";
import { mkdtemp, mkdir, readFile, readdir, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";

import { describe, expect, test, vi } from "vitest";

import {
  DEFAULT_XIAOHONGSHU_BATCH_OUT_DIR,
  resolveBatchCreatorDir,
  resolveOpenCliInvocationFromNodeDir,
  runXiaohongshuBatch,
  selectBatchVideoNotes
} from "../src/pipeline/xiaohongshu-batch.js";

function isOpenCliInvocation(command: string, args: string[]): boolean {
  if (/(^|[\\/])opencli(?:\.cmd|\.ps1)?$/i.test(command)) {
    return true;
  }
  return /(^|[\\/])node(?:\.exe)?$/i.test(command)
    && typeof args[0] === "string"
    && /node_modules[\\/]@jackwener[\\/]opencli[\\/]dist[\\/]main\.js$/i.test(args[0]);
}

function getCliArgs(command: string, args: string[]): string[] {
  return /(^|[\\/])node(?:\.exe)?$/i.test(command) ? args.slice(1) : args;
}

describe("selectBatchVideoNotes", () => {
  test("keeps only videos in original order and limits to requested count", () => {
    const notes = [
      { id: "a1", title: "图文", type: "normal", likes: "1", url: "https://xhs/a1" },
      { id: "v1", title: "视频1", type: "video", likes: "2", url: "https://xhs/v1" },
      { id: "v2", title: "视频2", type: "video", likes: "3", url: "https://xhs/v2" },
      { id: "v3", title: "视频3", type: "video", likes: "4", url: "https://xhs/v3" }
    ];

    expect(selectBatchVideoNotes(notes, 2).map((item) => item.id)).toEqual(["v1", "v2"]);
  });
});

describe("resolveBatchCreatorDir", () => {
  test("uses the caller-provided out dir instead of hardcoding the default path", () => {
    const root = "E:/custom/out";
    const result = resolveBatchCreatorDir(root, "不甜的小七", "9477032739");

    expect(result.rootDir).toBe(root);
    expect(result.creatorDir).toBe(path.join(root, "不甜的小七_9477032739"));
    expect(DEFAULT_XIAOHONGSHU_BATCH_OUT_DIR).toBe(defaultOutputDirectory());
  });
});

describe("resolveOpenCliInvocationFromNodeDir", () => {
  test("prefers node plus the installed opencli main.js when it exists", () => {
    const resolved = resolveOpenCliInvocationFromNodeDir(
      "C:/nvm4w/nodejs/node.exe",
      "win32",
      (candidate) => candidate === "C:\\nvm4w\\nodejs\\node_modules\\@jackwener\\opencli\\dist\\main.js"
    );

    expect(resolved.command).toBe("C:/nvm4w/nodejs/node.exe");
    expect(resolved.argsPrefix).toEqual(["C:\\nvm4w\\nodejs\\node_modules\\@jackwener\\opencli\\dist\\main.js"]);
  });
});

describe("runXiaohongshuBatch", () => {
  test("writes transcript markdown files directly under creator folder", async () => {
    const outDir = await mkdtemp(path.join(os.tmpdir(), "xhs-batch-out-"));
    const runCommand = vi.fn(async (command: string, args: string[]) => {
      if (!isOpenCliInvocation(command, args)) {
        throw new Error(`unexpected command: ${command}`);
      }
      const cliArgs = getCliArgs(command, args);
      if (cliArgs[0] === "xiaohongshu" && cliArgs[1] === "user") {
        return {
          exitCode: 0,
          stdout: JSON.stringify([
            { id: "v1", title: "视频1", type: "video", likes: "1", url: "https://xhs/v1" },
            { id: "v2", title: "视频2", type: "video", likes: "2", url: "https://xhs/v2" }
          ]),
          stderr: ""
        };
      }
      if (cliArgs[0] === "xiaohongshu" && cliArgs[1] === "download") {
        return { exitCode: 0, stdout: "download ok", stderr: "" };
      }
      throw new Error(`unexpected args: ${cliArgs.join(" ")}`);
    });
    const transcribeDownloadedMedia = vi.fn(async ({ transcriptPath }: { transcriptPath: string }) => {
      await mkdir(path.dirname(transcriptPath), { recursive: true });
      await writeFile(transcriptPath, "# generated", "utf8");
      return { transcriptPath };
    });

    const result = await runXiaohongshuBatch({
      profileUrl: "https://www.xiaohongshu.com/user/profile/demo",
      creatorName: "不甜的小七",
      creatorHandle: "9477032739",
      outDir,
      limit: 2,
      provider: "tingwu",
      keepArtifacts: false
    }, { runCommand, transcribeDownloadedMedia });

    expect(result.items).toHaveLength(2);
    expect(result.creatorDir).toContain("不甜的小七_9477032739");

    const files = (await readdir(result.creatorDir)).sort();
    expect(files).toEqual(["视频1.md", "视频2.md"]);

    const transcriptPath = path.join(result.creatorDir, "视频1.md");
    expect(await readFile(transcriptPath, "utf8")).toContain("# generated");
  });

  test("keeps processing later notes when one note download or transcript step fails", async () => {
    const outDir = await mkdtemp(path.join(os.tmpdir(), "xhs-batch-out-"));
    const runCommand = vi.fn(async (command: string, args: string[]) => {
      if (!isOpenCliInvocation(command, args)) {
        throw new Error(`unexpected command: ${command}`);
      }
      const cliArgs = getCliArgs(command, args);
      if (cliArgs[0] === "xiaohongshu" && cliArgs[1] === "user") {
        return {
          exitCode: 0,
          stdout: JSON.stringify([
            { id: "v1", title: "视频1", type: "video", likes: "1", url: "https://xhs/v1" },
            { id: "v2", title: "视频2", type: "video", likes: "2", url: "https://xhs/v2" }
          ]),
          stderr: ""
        };
      }
      if (cliArgs[0] === "xiaohongshu" && cliArgs[1] === "download" && cliArgs[2] === "https://xhs/v1") {
        throw new Error("download failed");
      }
      if (cliArgs[0] === "xiaohongshu" && cliArgs[1] === "download" && cliArgs[2] === "https://xhs/v2") {
        return { exitCode: 0, stdout: "download ok", stderr: "" };
      }
      throw new Error(`unexpected args: ${cliArgs.join(" ")}`);
    });
    const transcribeDownloadedMedia = vi.fn(async ({ transcriptPath }: { transcriptPath: string }) => {
      await mkdir(path.dirname(transcriptPath), { recursive: true });
      await writeFile(transcriptPath, "# generated", "utf8");
      return { transcriptPath };
    });

    const result = await runXiaohongshuBatch({
      profileUrl: "https://www.xiaohongshu.com/user/profile/demo",
      creatorName: "不甜的小七",
      creatorHandle: "9477032739",
      outDir,
      limit: 2,
      provider: "tingwu",
      keepArtifacts: false
    }, { runCommand, transcribeDownloadedMedia });

    expect(result.items[0].status).toBe("failed");
    expect(result.items[1].status).toBe("success");
    expect(transcribeDownloadedMedia).toHaveBeenCalledTimes(1);
  });

  test("passes the final transcript path directly under creator folder to the local transcriber", async () => {
    const outDir = await mkdtemp(path.join(os.tmpdir(), "xhs-batch-out-"));
    const runCommand = vi.fn(async (command: string, args: string[]) => {
      if (!isOpenCliInvocation(command, args)) {
        throw new Error(`unexpected command: ${command}`);
      }
      const cliArgs = getCliArgs(command, args);
      if (cliArgs[0] === "xiaohongshu" && cliArgs[1] === "user") {
        return {
          exitCode: 0,
          stdout: JSON.stringify([
            { id: "v1", title: "视频1", type: "video", likes: "1", url: "https://xhs/v1" }
          ]),
          stderr: ""
        };
      }
      if (cliArgs[0] === "xiaohongshu" && cliArgs[1] === "download") {
        return { exitCode: 0, stdout: "download ok", stderr: "" };
      }
      throw new Error(`unexpected args: ${cliArgs.join(" ")}`);
    });
    const transcribeDownloadedMedia = vi.fn(async ({ transcriptPath }: { transcriptPath: string }) => {
      await mkdir(path.dirname(transcriptPath), { recursive: true });
      await writeFile(transcriptPath, "# generated", "utf8");
      return { transcriptPath };
    });

    await runXiaohongshuBatch({
      profileUrl: "https://www.xiaohongshu.com/user/profile/demo",
      creatorName: "不甜的小七",
      creatorHandle: "9477032739",
      outDir,
      limit: 1,
      provider: "tingwu",
      keepArtifacts: false
    }, { runCommand, transcribeDownloadedMedia });

    expect(transcribeDownloadedMedia).toHaveBeenCalledWith(expect.objectContaining({
      note: expect.objectContaining({ id: "v1" }),
      transcriptPath: path.join(outDir, "不甜的小七_9477032739", "视频1.md"),
      keepArtifacts: false
    }));
  });

  test("cleans stale intermediate files from previous runs before writing fresh transcripts", async () => {
    const outDir = await mkdtemp(path.join(os.tmpdir(), "xhs-batch-out-"));
    const creatorDir = path.join(outDir, "不甜的小七_9477032739");
    await mkdir(path.join(creatorDir, "01_old"), { recursive: true });
    await writeFile(path.join(creatorDir, "index.json"), "{\"stale\":true}", "utf8");
    await writeFile(path.join(creatorDir, "旧稿.md"), "stale", "utf8");

    const runCommand = vi.fn(async (command: string, args: string[]) => {
      if (!isOpenCliInvocation(command, args)) {
        throw new Error(`unexpected command: ${command}`);
      }
      const cliArgs = getCliArgs(command, args);
      if (cliArgs[0] === "xiaohongshu" && cliArgs[1] === "user") {
        return {
          exitCode: 0,
          stdout: JSON.stringify([
            { id: "v1", title: "视频1", type: "video", likes: "1", url: "https://xhs/v1" }
          ]),
          stderr: ""
        };
      }
      if (cliArgs[0] === "xiaohongshu" && cliArgs[1] === "download") {
        return { exitCode: 0, stdout: "download ok", stderr: "" };
      }
      throw new Error(`unexpected args: ${cliArgs.join(" ")}`);
    });
    const transcribeDownloadedMedia = vi.fn(async ({ transcriptPath }: { transcriptPath: string }) => {
      await writeFile(transcriptPath, "# fresh", "utf8");
      return { transcriptPath };
    });

    await runXiaohongshuBatch({
      profileUrl: "https://www.xiaohongshu.com/user/profile/demo",
      creatorName: "不甜的小七",
      creatorHandle: "9477032739",
      outDir,
      limit: 1,
      provider: "tingwu",
      keepArtifacts: false
    }, { runCommand, transcribeDownloadedMedia });

    const files = await readdir(creatorDir);
    expect(files).toEqual(["视频1.md"]);
  });
});
