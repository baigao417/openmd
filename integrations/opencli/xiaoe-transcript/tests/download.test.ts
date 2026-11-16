import { mkdtemp, readFile, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";

import { beforeEach, describe, expect, test, vi } from "vitest";

const { runCommandMock } = vi.hoisted(() => ({
  runCommandMock: vi.fn()
}));

vi.mock("../src/utils/command.js", () => ({
  runCommand: runCommandMock
}));

import { downloadMediaArtifact } from "../src/pipeline/download.js";
import type { DownloadStatusSnapshot, MediaDiscovery } from "../src/types.js";

type CommandCall = [string, string[], Record<string, unknown>?];
type CommandPredicate = (args: string[], outputPath: string) => boolean;
type ProbePredicate = (args: string[], filePath: string) => boolean;

interface CommandBehavior {
  ffmpegFailures: Array<{ predicate: CommandPredicate; error: Error }>;
  ytDlpFailures: Array<{ predicate: CommandPredicate; error: Error }>;
  ffprobeFailures: Array<{ predicate: ProbePredicate; error: Error }>;
  outputRules: Array<{ predicate: CommandPredicate; content: string }>;
  durationRules: Array<{ predicate: ProbePredicate; duration: number }>;
}

let commandBehavior: CommandBehavior;

function mediaPlaylist(segment = "seg-1.ts"): string {
  return `#EXTM3U\n#EXT-X-PLAYLIST-TYPE:VOD\n#EXTINF:4.0,\n${segment}\n#EXT-X-ENDLIST\n`;
}

function mediaPlaylistSegments(count: number): string {
  const lines = ["#EXTM3U", "#EXT-X-PLAYLIST-TYPE:VOD"];
  for (let index = 0; index < count; index += 1) {
    lines.push("#EXTINF:4.0,", `seg-${String(index).padStart(3, "0")}.ts`);
  }
  lines.push("#EXT-X-ENDLIST");
  return lines.join("\n");
}

function encryptedPlaylist(): string {
  return "#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI=\"enc.key\"\n#EXTINF:4.0,\nseg-1.ts\n#EXT-X-ENDLIST\n";
}

function textResponse(text: string): { ok: true; text: () => Promise<string> } {
  return {
    ok: true,
    text: async () => text
  };
}

function bufferResponse(text: string): { ok: true; arrayBuffer: () => Promise<ArrayBuffer> } {
  return {
    ok: true,
    arrayBuffer: async () => new TextEncoder().encode(text).buffer
  };
}

function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void; reject: (error: unknown) => void } {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

function baseMedia(mediaUrl: string): MediaDiscovery {
  return {
    pageTitle: "课程标题",
    sourceUrl: "https://example.com/course",
    mediaUrl,
    extractor: "network",
    referer: "https://example.com/course",
    userAgent: "Mozilla/5.0",
    browserCookies: []
  };
}

function mockSuccessfulProbeNormalizeAndDuration(duration = "120.5\n"): void {
  runCommandMock
    .mockResolvedValueOnce({ exitCode: 0, stdout: "{\"streams\":[{\"codec_type\":\"audio\"}]}\n", stderr: "" })
    .mockResolvedValueOnce({ exitCode: 0, stdout: "", stderr: "" })
    .mockResolvedValueOnce({ exitCode: 0, stdout: duration, stderr: "" });
}

function resetCommandBehavior(): void {
  commandBehavior = {
    ffmpegFailures: [],
    ytDlpFailures: [],
    ffprobeFailures: [],
    outputRules: [],
    durationRules: []
  };
}

function outputPathFromArgs(args: string[]): string {
  return args[args.length - 1];
}

function inputUrlFromArgs(args: string[]): string | undefined {
  const inputIndex = args.indexOf("-i");
  return inputIndex >= 0 ? args[inputIndex + 1] : undefined;
}

function isBenchmarkPath(filePath: string): boolean {
  return filePath.split(/[\\/]/).includes("download-benchmarks");
}

function rejectFfmpegWhen(predicate: CommandPredicate, message: string): void {
  commandBehavior.ffmpegFailures.push({ predicate, error: new Error(message) });
}

function rejectYtDlpWhen(predicate: CommandPredicate, message: string): void {
  commandBehavior.ytDlpFailures.push({ predicate, error: new Error(message) });
}

function rejectFfprobeWhen(predicate: ProbePredicate, message: string): void {
  commandBehavior.ffprobeFailures.push({ predicate, error: new Error(message) });
}

function writeOutputWhen(predicate: CommandPredicate, content: string): void {
  commandBehavior.outputRules.push({ predicate, content });
}

function durationWhen(predicate: ProbePredicate, duration: number): void {
  commandBehavior.durationRules.push({ predicate, duration });
}

function commandCalls(command: string): CommandCall[] {
  return runCommandMock.mock.calls.filter((call) => call[0] === command) as CommandCall[];
}

function findFfmpegCallByOutput(outputPath: string): CommandCall | undefined {
  return commandCalls("ffmpeg").find(([, args]) => outputPathFromArgs(args) === outputPath);
}

function findFfmpegCallByInput(inputUrl: string): CommandCall | undefined {
  return commandCalls("ffmpeg").find(([, args]) => inputUrlFromArgs(args) === inputUrl);
}

function defaultOutputFor(command: string, args: string[], outputPath: string): string {
  const rule = commandBehavior.outputRules.find((candidate) => candidate.predicate(args, outputPath));
  if (rule) {
    return rule.content;
  }
  if (command === "yt-dlp") {
    return "yt-dlp media";
  }
  if (path.basename(outputPath) === "audio.mp3") {
    return "normalized audio";
  }
  if (isBenchmarkPath(outputPath)) {
    return path.basename(outputPath) === "sample.ts" ? "hls sample" : "ffmpeg sample";
  }
  return "ffmpeg media";
}

function durationFor(args: string[], filePath: string): number {
  const rule = commandBehavior.durationRules.find((candidate) => candidate.predicate(args, filePath));
  if (rule) {
    return rule.duration;
  }
  if (isBenchmarkPath(filePath)) {
    return path.basename(filePath) === "sample.ts" ? 1 : 16;
  }
  return 120.5;
}

async function defaultRunCommand(command: string, args: string[]): Promise<{ exitCode: number; stdout: string; stderr: string }> {
  if (command === "ffmpeg") {
    const outputPath = outputPathFromArgs(args);
    const failure = commandBehavior.ffmpegFailures.find((candidate) => candidate.predicate(args, outputPath));
    if (failure) {
      throw failure.error;
    }
    await writeFile(outputPath, defaultOutputFor(command, args, outputPath));
    return { exitCode: 0, stdout: "", stderr: "" };
  }

  if (command === "ffprobe") {
    const filePath = outputPathFromArgs(args);
    const failure = commandBehavior.ffprobeFailures.find((candidate) => candidate.predicate(args, filePath));
    if (failure) {
      throw failure.error;
    }
    if (args.includes("-of") && args.includes("json")) {
      return { exitCode: 0, stdout: "{\"streams\":[{\"codec_type\":\"audio\"}]}\n", stderr: "" };
    }
    return { exitCode: 0, stdout: `${durationFor(args, filePath)}\n`, stderr: "" };
  }

  if (command === "yt-dlp") {
    const outputIndex = args.indexOf("-o") + 1;
    const outputPath = args[outputIndex].replace("%(ext)s", "mp4");
    const failure = commandBehavior.ytDlpFailures.find((candidate) => candidate.predicate(args, outputPath));
    if (failure) {
      throw failure.error;
    }
    await writeFile(outputPath, defaultOutputFor(command, args, outputPath));
    return { exitCode: 0, stdout: "", stderr: "" };
  }

  throw new Error(`${command} default mock is not configured`);
}

describe("downloadMediaArtifact", () => {
  beforeEach(() => {
    runCommandMock.mockReset();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    resetCommandBehavior();
    runCommandMock.mockImplementation(defaultRunCommand);
  });

  test("reports download attempt status with file size details", async () => {
    const tempDir = await mkdtemp(path.join(os.tmpdir(), "xiaoe-download-status-"));
    const media = baseMedia("https://media.example.com/video.mp4?token=secret-token");
    const ffmpegPath = path.join(tempDir, "download-attempts", "001-ffmpeg-1", "source.mp3");
    const statuses: DownloadStatusSnapshot[] = [];

    runCommandMock.mockImplementation(async (command: string, args: string[]) => {
      if (command === "ffmpeg") {
        const outputPath = args[args.length - 1];
        await writeFile(outputPath, outputPath === ffmpegPath ? "normalized mp3" : "sample mp3");
        return { exitCode: 0, stdout: "", stderr: "" };
      }
      if (command === "ffprobe" && args.includes("-of") && args.includes("json")) {
        return { exitCode: 0, stdout: "{\"streams\":[{\"codec_type\":\"audio\"}]}\n", stderr: "" };
      }
      if (command === "ffprobe") {
        return { exitCode: 0, stdout: args.includes("sample.mp3") ? "15\n" : "120.5\n", stderr: "" };
      }
      throw new Error(`${command} default mock is not configured`);
    });

    await downloadMediaArtifact(media, tempDir, undefined, (status) => {
      statuses.push(status);
    });

    expect(statuses).toEqual(expect.arrayContaining([
      expect.objectContaining({
        status: "running",
        benchmark: expect.objectContaining({
          status: "running",
          plans: expect.any(Array)
        })
      }),
      expect.objectContaining({
        status: "running",
        current: expect.objectContaining({
          status: "running",
          strategy: "ffmpeg",
          attempt: 1,
          attemptLabel: "ffmpeg-1",
          attemptPath: path.join(tempDir, "download-attempts", "001-ffmpeg-1")
        })
      }),
      expect.objectContaining({
        status: "success",
        current: expect.objectContaining({
          status: "success",
          strategy: "ffmpeg",
          path: ffmpegPath,
          bytes: "normalized mp3".length
        })
      })
    ]));
    expect(JSON.stringify(statuses)).not.toContain("secret-token");
    expect(JSON.stringify(statuses)).not.toContain("https://media.example.com/video.mp4");
  });

  test("resumes direct HLS downloads from persisted segment files", async () => {
    const tempDir = await mkdtemp(path.join(os.tmpdir(), "xiaoe-download-hls-resume-"));
    const media = baseMedia("https://media.example.com/main.m3u8");
    const playlist = [
      "#EXTM3U",
      "#EXT-X-PLAYLIST-TYPE:VOD",
      "#EXTINF:4.0,",
      "seg-1.ts",
      "#EXTINF:4.0,",
      "seg-2.ts",
      "#EXT-X-ENDLIST"
    ].join("\n");
    const directAttemptDir = path.join(tempDir, "download-attempts", "001-direct-hls-1");
    let seg1Fetches = 0;
    let seg2Fetches = 0;

    const fetchMock = vi.fn(async (url: string) => {
      if (url.endsWith("main.m3u8")) return textResponse(playlist);
      if (url.endsWith("seg-1.ts")) {
        seg1Fetches += 1;
        return bufferResponse(seg1Fetches === 2 ? "segment one" : "sample one");
      }
      if (url.endsWith("seg-2.ts")) {
        seg2Fetches += 1;
        return bufferResponse(seg2Fetches === 2 ? "segment two" : "sample two");
      }
      throw new Error(`unexpected fetch ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    rejectFfmpegWhen((args) => inputUrlFromArgs(args) === media.mediaUrl, "ffmpeg failed");

    const first = await downloadMediaArtifact(media, tempDir);

    const second = await downloadMediaArtifact(media, tempDir);

    expect(first.originalPath).toBe(path.join(directAttemptDir, "source.ts"));
    expect(second.originalPath).toBe(path.join(directAttemptDir, "source.ts"));
    await expect(readFile(path.join(directAttemptDir, "manifest.json"), "utf8")).resolves.toContain("seg-1.ts");
    await expect(readFile(path.join(directAttemptDir, "segments", "000000.ts"), "utf8")).resolves.toBe("segment one");
    await expect(readFile(path.join(directAttemptDir, "segments", "000001.ts"), "utf8")).resolves.toBe("segment two");
    await expect(readFile(path.join(directAttemptDir, "source.ts"), "utf8")).resolves.toBe("segment onesegment two");
    expect(seg1Fetches).toBe(3);
    expect(seg2Fetches).toBe(3);
  });

  test("downloads normalized mp3 directly with ffmpeg and skips a second normalization pass", async () => {
    const tempDir = await mkdtemp(path.join(os.tmpdir(), "xiaoe-download-"));
    const media = baseMedia("https://media.example.com/main.m3u8");
    const ffmpegPath = path.join(tempDir, "download-attempts", "001-ffmpeg-1", "source.mp3");

    const fetchMock = vi.fn(async (url: string) => {
      if (url.endsWith("main.m3u8")) return textResponse(mediaPlaylist());
      if (url.endsWith("seg-1.ts")) return bufferResponse("benchmark segment");
      throw new Error(`unexpected fetch ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    writeOutputWhen((_, outputPath) => outputPath === ffmpegPath, "normalized mp3");

    const result = await downloadMediaArtifact(media, tempDir);

    expect(result.originalPath).toBe(ffmpegPath);
    expect(result.audioPath).toBe(ffmpegPath);
    expect(findFfmpegCallByOutput(ffmpegPath)).toEqual([
      "ffmpeg",
      expect.arrayContaining([
        "-i",
        "https://media.example.com/main.m3u8",
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-codec:a",
        "libmp3lame",
        "-b:a",
        "64k",
        ffmpegPath
      ]),
      expect.objectContaining({
        timeoutMs: 90 * 60 * 1000,
        watchFile: {
          path: ffmpegPath,
          stallMs: 60 * 1000
        }
      })
    ]);
    expect(findFfmpegCallByInput(ffmpegPath)).toBeUndefined();
    expect(result.mediaDurationSec).toBe(120.5);
  });

  test("does not mark long-running ffmpeg output as stalled when it eventually succeeds", async () => {
    vi.useFakeTimers();
    try {
      const tempDir = await mkdtemp(path.join(os.tmpdir(), "xiaoe-download-"));
      const media = baseMedia("https://media.example.com/video.mp4");
      const ffmpegPath = path.join(tempDir, "download-attempts", "001-ffmpeg-1", "source.mp3");
      let resolveFfmpeg!: (value: { exitCode: number; stdout: string; stderr: string }) => void;

      runCommandMock.mockImplementation(async (command: string, args: string[]) => {
        const outputPath = command === "ffmpeg" ? outputPathFromArgs(args) : "";
        if (command === "ffmpeg" && outputPath === ffmpegPath) {
          await writeFile(ffmpegPath, "normalized mp3");
          return await new Promise((resolve) => {
            resolveFfmpeg = resolve;
          });
        }
        return await defaultRunCommand(command, args);
      });

      const resultPromise = downloadMediaArtifact(media, tempDir);
      await vi.waitFor(() => {
        expect(findFfmpegCallByOutput(ffmpegPath)).toBeDefined();
      });

      await vi.advanceTimersByTimeAsync(61_000);
      resolveFfmpeg({ exitCode: 0, stdout: "", stderr: "" });

      const result = await resultPromise;

      expect(result.originalPath).toBe(ffmpegPath);
      expect(result.audioPath).toBe(ffmpegPath);
      expect(findFfmpegCallByOutput(ffmpegPath)?.[2]).toEqual(expect.objectContaining({
        watchFile: {
          path: ffmpegPath,
          stallMs: 60 * 1000
        }
      }));
    } finally {
      vi.useRealTimers();
    }
  });

  test("tries alternate HLS ffmpeg candidates before expensive fallback download strategies", async () => {
    const tempDir = await mkdtemp(path.join(os.tmpdir(), "xiaoe-download-"));
    const media: MediaDiscovery = {
      ...baseMedia("https://bad-cdn.example.com/main.m3u8"),
      candidateMediaUrls: [
        "https://bad-cdn.example.com/main.m3u8",
        "https://good-cdn.example.com/main.m3u8"
      ]
    };
    const goodFfmpegPath = path.join(tempDir, "download-attempts", "002-ffmpeg-2", "source.mp3");
    const badFfmpegPath = path.join(tempDir, "download-attempts", "001-ffmpeg-1", "source.mp3");

    const fetchMock = vi.fn(async () => {
      throw new Error("HLS direct fallback reset");
    });
    vi.stubGlobal("fetch", fetchMock);

    rejectFfmpegWhen(
      (args, outputPath) => inputUrlFromArgs(args) === "https://bad-cdn.example.com/main.m3u8" && !isBenchmarkPath(outputPath),
      "ffmpeg bad CDN reset"
    );
    writeOutputWhen((_, outputPath) => outputPath === goodFfmpegPath, "good normalized mp3");

    const result = await downloadMediaArtifact(media, tempDir);

    expect(result.originalPath).toBe(goodFfmpegPath);
    expect(result.audioPath).toBe(goodFfmpegPath);
    expect(findFfmpegCallByOutput(badFfmpegPath)).toEqual([
      "ffmpeg",
      expect.arrayContaining(["-i", "https://bad-cdn.example.com/main.m3u8"]),
      expect.objectContaining({ timeoutMs: 90 * 60 * 1000 })
    ]);
    expect(findFfmpegCallByOutput(goodFfmpegPath)).toEqual([
      "ffmpeg",
      expect.arrayContaining(["-i", "https://good-cdn.example.com/main.m3u8"]),
      expect.objectContaining({ timeoutMs: 90 * 60 * 1000 })
    ]);
    expect(commandCalls("yt-dlp")).toHaveLength(0);
    expect(result.mediaDurationSec).toBe(120.5);
  });

  test("uses ffmpeg full download when its benchmark is fastest for HLS media", async () => {
    const tempDir = await mkdtemp(path.join(os.tmpdir(), "xiaoe-download-"));
    const media = baseMedia("https://media.example.com/main.m3u8");
    const ffmpegPath = path.join(tempDir, "download-attempts", "001-ffmpeg-1", "source.mp3");

    const fetchMock = vi.fn(async (url: string) => {
      if (url.endsWith("main.m3u8")) return textResponse(mediaPlaylist());
      if (url.endsWith("seg-1.ts")) return bufferResponse("benchmark segment");
      throw new Error(`unexpected fetch ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    writeOutputWhen((_, outputPath) => outputPath === ffmpegPath, "ffmpeg audio");

    const result = await downloadMediaArtifact(media, tempDir);
    const extracted = await readFile(result.originalPath, "utf8");

    expect(extracted).toBe("ffmpeg audio");
    expect(result.originalPath).toBe(ffmpegPath);
    expect(findFfmpegCallByOutput(ffmpegPath)).toEqual([
      "ffmpeg",
      expect.arrayContaining([
        "-i",
        "https://media.example.com/main.m3u8",
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-codec:a",
        "libmp3lame",
        "-b:a",
        "64k"
      ]),
      expect.objectContaining({
        timeoutMs: 90 * 60 * 1000,
        watchFile: {
          path: ffmpegPath,
          stallMs: 60 * 1000
        }
      })
    ]);
    expect(findFfmpegCallByOutput(ffmpegPath)?.[1]).toEqual(expect.arrayContaining(["-rw_timeout", "30000000"]));
    expect(commandCalls("yt-dlp")).toHaveLength(0);
    expect(result.mediaDurationSec).toBe(120.5);
  });

  test("marks direct HLS benchmark unusable when middle or tail sample segments fail", async () => {
    const tempDir = await mkdtemp(path.join(os.tmpdir(), "xiaoe-download-"));
    const media = baseMedia("https://media.example.com/main.m3u8");
    const ffmpegPath = path.join(tempDir, "download-attempts", "001-ffmpeg-1", "source.mp3");

    const fetchMock = vi.fn(async (url: string) => {
      if (url.endsWith("main.m3u8")) return textResponse(mediaPlaylistSegments(9));
      if (url.endsWith("seg-005.ts")) throw new Error("mid segment reset");
      if (/seg-(000|001|004|008)\.ts$/.test(url)) return bufferResponse(`sample ${url}`);
      throw new Error(`unexpected fetch ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    writeOutputWhen((_, outputPath) => outputPath === ffmpegPath, "ffmpeg audio");

    const result = await downloadMediaArtifact(media, tempDir);

    expect(result.originalPath).toBe(ffmpegPath);
    expect(fetchMock).toHaveBeenCalledWith("https://media.example.com/seg-000.ts", expect.any(Object));
    expect(fetchMock).toHaveBeenCalledWith("https://media.example.com/seg-004.ts", expect.any(Object));
    expect(fetchMock).toHaveBeenCalledWith("https://media.example.com/seg-005.ts", expect.any(Object));
    expect(fetchMock).toHaveBeenCalledWith("https://media.example.com/seg-008.ts", expect.any(Object));
    expect(findFfmpegCallByOutput(ffmpegPath)).toBeDefined();
    expect(commandCalls("yt-dlp")).toHaveLength(0);
  });

  test("prefers audio-only renditions discovered inside HLS master playlists", async () => {
    const tempDir = await mkdtemp(path.join(os.tmpdir(), "xiaoe-download-"));
    const media = baseMedia("https://media.example.com/master.m3u8");
    const ffmpegPath = path.join(tempDir, "download-attempts", "001-ffmpeg-1", "source.mp3");

    const fetchMock = vi.fn(async (url: string) => {
      if (url.endsWith("master.m3u8")) {
        return textResponse([
          "#EXTM3U",
          "#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID=\"audio\",NAME=\"普通话\",LANGUAGE=\"zh-CN\",DEFAULT=YES,AUTOSELECT=YES,URI=\"audio/main.m3u8\"",
          "#EXT-X-STREAM-INF:BANDWIDTH=1800000,RESOLUTION=1280x720,CODECS=\"avc1.64001f,mp4a.40.2\",AUDIO=\"audio\"",
          "video/720p.m3u8"
        ].join("\n"));
      }
      if (url.endsWith("audio/main.m3u8")) return textResponse(mediaPlaylist("audio-seg.ts"));
      if (url.endsWith("audio-seg.ts")) return bufferResponse("audio benchmark segment");
      throw new Error(`unexpected fetch ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    writeOutputWhen((_, outputPath) => outputPath === ffmpegPath, "audio rendition");

    const result = await downloadMediaArtifact(media, tempDir);
    const extracted = await readFile(result.originalPath, "utf8");

    expect(extracted).toBe("audio rendition");
    expect(findFfmpegCallByOutput(ffmpegPath)).toEqual([
      "ffmpeg",
      expect.arrayContaining(["-i", "https://media.example.com/audio/main.m3u8", "-vn", "-codec:a", "libmp3lame"]),
      expect.objectContaining({ timeoutMs: 90 * 60 * 1000 })
    ]);
  });

  test("probes HLS candidate playlists concurrently before selecting a download strategy", async () => {
    const tempDir = await mkdtemp(path.join(os.tmpdir(), "xiaoe-download-"));
    const media: MediaDiscovery = {
      ...baseMedia("https://cdn-a.example.com/master.m3u8"),
      candidateMediaUrls: ["https://cdn-b.example.com/master.m3u8"]
    };
    const firstProbe = deferred<{ ok: true; text: () => Promise<string> }>();
    const secondProbe = deferred<{ ok: true; text: () => Promise<string> }>();
    const ffmpegPath = path.join(tempDir, "download-attempts", "001-ffmpeg-1", "source.mp3");

    const fetchMock = vi.fn()
      .mockReturnValueOnce(firstProbe.promise)
      .mockReturnValueOnce(secondProbe.promise);
    vi.stubGlobal("fetch", fetchMock);

    runCommandMock
      .mockImplementationOnce(async () => {
        await writeFile(ffmpegPath, "normalized mp3");
        return { exitCode: 0, stdout: "", stderr: "" };
      })
      .mockResolvedValueOnce({ exitCode: 0, stdout: "{\"streams\":[{\"codec_type\":\"audio\"}]}\n", stderr: "" })
      .mockResolvedValueOnce({ exitCode: 0, stdout: "120.5\n", stderr: "" });

    const resultPromise = downloadMediaArtifact(media, tempDir);
    await vi.waitFor(() => {
      expect(fetchMock).toHaveBeenCalledTimes(2);
    });

    firstProbe.resolve(textResponse(mediaPlaylist()));
    secondProbe.resolve(textResponse(mediaPlaylist()));

    const result = await resultPromise;

    expect(result.originalPath).toBe(ffmpegPath);
    expect(fetchMock).toHaveBeenNthCalledWith(1, "https://cdn-a.example.com/master.m3u8", expect.any(Object));
    expect(fetchMock).toHaveBeenNthCalledWith(2, "https://cdn-b.example.com/master.m3u8", expect.any(Object));
  });

  test("falls back to direct HLS fetching and retries transient segment failures when ffmpeg is not usable", async () => {
    const tempDir = await mkdtemp(path.join(os.tmpdir(), "xiaoe-download-"));
    const media = baseMedia("https://media.example.com/main.m3u8");
    const directHlsPath = path.join(tempDir, "download-attempts", "001-direct-hls-1", "source.ts");
    let segmentFetches = 0;

    const fetchMock = vi.fn(async (url: string) => {
      if (url.endsWith("main.m3u8")) return textResponse(mediaPlaylist());
      if (url.endsWith("seg-1.ts")) {
        segmentFetches += 1;
        if (segmentFetches === 2) {
          throw new Error("network timeout");
        }
        return bufferResponse(segmentFetches === 1 ? "benchmark-segment" : "segment-1");
      }
      throw new Error(`unexpected fetch ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    rejectFfmpegWhen((args) => inputUrlFromArgs(args) === media.mediaUrl, "ffmpeg rejected source");

    const result = await downloadMediaArtifact(media, tempDir);
    const merged = await readFile(result.originalPath, "utf8");

    expect(result.originalPath).toBe(directHlsPath);
    expect(segmentFetches).toBe(3);
    expect(merged).toBe("segment-1");
    expect(commandCalls("yt-dlp")).toHaveLength(0);
  });

  test("falls back to yt-dlp when ffmpeg and direct HLS are not usable", async () => {
    const tempDir = await mkdtemp(path.join(os.tmpdir(), "xiaoe-download-"));
    const media = baseMedia("https://media.example.com/main.m3u8");
    const ytDlpPath = path.join(tempDir, "download-attempts", "003-yt-dlp-1", "source.mp4");

    const fetchMock = vi.fn(async (url: string) => {
      if (url.endsWith("main.m3u8")) return textResponse(encryptedPlaylist());
      throw new Error(`unexpected fetch ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    rejectFfmpegWhen((args) => inputUrlFromArgs(args) === media.mediaUrl, "ffmpeg rejected source");

    const result = await downloadMediaArtifact(media, tempDir);

    expect(result.originalPath).toBe(ytDlpPath);
    expect(commandCalls("yt-dlp")[0]).toEqual([
      "yt-dlp",
      expect.arrayContaining(["--socket-timeout", "30", "--retries", "3", "--fragment-retries", "3", "https://media.example.com/main.m3u8"]),
      expect.objectContaining({
        timeoutMs: 90 * 60 * 1000,
        watchFile: {
          directory: path.dirname(ytDlpPath),
          prefix: "source.",
          ignoreIncludes: ["-Frag"],
          ignoreSuffixes: [".ytdl"],
          stallMs: 60 * 1000
        }
      })
    ]);
    expect(result.mediaDurationSec).toBe(120.5);
  });

  test("tries alternate media URLs when all download strategies fail for the selected CDN", async () => {
    const tempDir = await mkdtemp(path.join(os.tmpdir(), "xiaoe-download-"));
    const media: MediaDiscovery = {
      ...baseMedia("https://bad-cdn.example.com/main.m3u8"),
      pageTitle: "直播回放",
      candidateMediaUrls: [
        "https://bad-cdn.example.com/main.m3u8",
        "https://good-cdn.example.com/main.m3u8"
      ]
    };
    const goodFfmpegPath = path.join(tempDir, "download-attempts", "002-ffmpeg-2", "source.mp3");
    const badFfmpegPath = path.join(tempDir, "download-attempts", "001-ffmpeg-1", "source.mp3");

    const fetchMock = vi.fn(async () => {
      throw new Error("selected CDN direct reset");
    });
    vi.stubGlobal("fetch", fetchMock);

    rejectFfmpegWhen(
      (args, outputPath) => inputUrlFromArgs(args) === "https://bad-cdn.example.com/main.m3u8" && !isBenchmarkPath(outputPath),
      "ffmpeg selected CDN reset"
    );
    writeOutputWhen((_, outputPath) => outputPath === goodFfmpegPath, "good ffmpeg audio");

    const result = await downloadMediaArtifact(media, tempDir);
    const extracted = await readFile(result.originalPath, "utf8");

    expect(findFfmpegCallByOutput(badFfmpegPath)).toEqual([
      "ffmpeg",
      expect.arrayContaining(["-i", "https://bad-cdn.example.com/main.m3u8"]),
      expect.objectContaining({ timeoutMs: 90 * 60 * 1000 })
    ]);
    expect(findFfmpegCallByOutput(goodFfmpegPath)).toEqual([
      "ffmpeg",
      expect.arrayContaining(["-i", "https://good-cdn.example.com/main.m3u8"]),
      expect.objectContaining({ timeoutMs: 90 * 60 * 1000 })
    ]);
    expect(extracted).toBe("good ffmpeg audio");
    expect(result.mediaDurationSec).toBe(120.5);
  });

  test("rejects an unreadable ffmpeg artifact and tries direct HLS before normalization", async () => {
    const tempDir = await mkdtemp(path.join(os.tmpdir(), "xiaoe-download-"));
    const invalidFfmpegPath = path.join(tempDir, "download-attempts", "001-ffmpeg-1", "source.mp3");
    const directHlsPath = path.join(tempDir, "download-attempts", "002-direct-hls-1", "source.ts");
    const media: MediaDiscovery = {
      ...baseMedia("https://media.example.com/main.m3u8"),
      pageTitle: "直播回放"
    };

    const fetchMock = vi.fn(async (url: string) => {
      if (url.endsWith("main.m3u8")) return textResponse(mediaPlaylist());
      if (url.endsWith("seg-1.ts")) return bufferResponse("segment-1");
      throw new Error(`unexpected fetch ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    writeOutputWhen((_, outputPath) => outputPath === invalidFfmpegPath, "not a readable mka");
    rejectFfprobeWhen((_, filePath) => filePath === invalidFfmpegPath, "ffprobe failed: End of file");

    const result = await downloadMediaArtifact(media, tempDir);

    expect(result.originalPath).toBe(directHlsPath);
    await expect(readFile(invalidFfmpegPath, "utf8")).resolves.toBe("not a readable mka");
    expect(commandCalls("ffprobe")).toEqual(expect.arrayContaining([
      ["ffprobe", expect.arrayContaining([directHlsPath]), { timeoutMs: 30000 }]
    ]));
  });

  test("preserves partial artifacts left by failed ffmpeg before direct HLS discovery", async () => {
    const tempDir = await mkdtemp(path.join(os.tmpdir(), "xiaoe-download-"));
    const invalidFfmpegPath = path.join(tempDir, "download-attempts", "001-ffmpeg-1", "source.mp3");
    const directHlsPath = path.join(tempDir, "download-attempts", "002-direct-hls-1", "source.ts");
    const media: MediaDiscovery = {
      ...baseMedia("https://media.example.com/main.m3u8"),
      pageTitle: "直播回放"
    };

    const fetchMock = vi.fn(async (url: string) => {
      if (url.endsWith("main.m3u8")) return textResponse(mediaPlaylist());
      if (url.endsWith("seg-1.ts")) return bufferResponse("segment-1");
      throw new Error(`unexpected fetch ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    runCommandMock.mockImplementation(async (command: string, args: string[]) => {
      if (command === "ffmpeg" && outputPathFromArgs(args) === invalidFfmpegPath) {
        await writeFile(invalidFfmpegPath, "partial ffmpeg artifact");
        throw new Error("ffmpeg failed after creating partial file");
      }
      return await defaultRunCommand(command, args);
    });

    const result = await downloadMediaArtifact(media, tempDir);

    expect(result.originalPath).toBe(directHlsPath);
    await expect(readFile(invalidFfmpegPath, "utf8")).resolves.toBe("partial ffmpeg artifact");
  });

  test("extends ffmpeg download timeout when the workflow timeout is higher", async () => {
    const tempDir = await mkdtemp(path.join(os.tmpdir(), "xiaoe-download-"));
    const media = baseMedia("https://media.example.com/video.mp4");
    const ffmpegPath = path.join(tempDir, "download-attempts", "001-ffmpeg-1", "source.mp3");

    durationWhen((_, filePath) => filePath === ffmpegPath, 7200);

    const result = await downloadMediaArtifact(media, tempDir, 180);

    expect(findFfmpegCallByOutput(ffmpegPath)?.[2]).toEqual(expect.objectContaining({ timeoutMs: 180 * 60 * 1000 }));
    expect(result.mediaDurationSec).toBe(7200);
  });

  test("honors an explicit shorter workflow timeout for media download commands", async () => {
    const tempDir = await mkdtemp(path.join(os.tmpdir(), "xiaoe-download-"));
    const media = baseMedia("https://media.example.com/video.mp4");
    const ffmpegPath = path.join(tempDir, "download-attempts", "001-ffmpeg-1", "source.mp3");

    await downloadMediaArtifact(media, tempDir, 30);

    expect(findFfmpegCallByOutput(ffmpegPath)?.[2]).toEqual(expect.objectContaining({ timeoutMs: 30 * 60 * 1000 }));
  });
});
