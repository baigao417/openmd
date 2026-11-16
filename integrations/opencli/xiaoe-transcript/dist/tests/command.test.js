import process from "node:process";
import { mkdtemp } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { describe, expect, test } from "vitest";
import { runCommand } from "../src/utils/command.js";
describe("runCommand", () => {
    test("reports timeout errors explicitly", async () => {
        await expect(runCommand(process.execPath, ["-e", "setTimeout(() => {}, 10000)"], { timeoutMs: 50 })).rejects.toThrow(/timed out after 50ms/i);
    });
    test("redacts sensitive headers and signed urls in failure output", async () => {
        const script = [
            "process.stderr.write('Cookie: session=super-secret\\n');",
            "process.stderr.write('https://example.com/media.m3u8?sign=top-secret&t=123&us=abc\\n');",
            "process.exit(1);"
        ].join("");
        try {
            await runCommand(process.execPath, ["-e", script], { timeoutMs: 1000 });
            throw new Error("expected runCommand to reject");
        }
        catch (error) {
            expect(error).toBeInstanceOf(Error);
            const message = error.message;
            expect(message).toContain("Cookie: [REDACTED]");
            expect(message).toContain("sign=%5BREDACTED%5D");
            expect(message).not.toContain("super-secret");
            expect(message).not.toContain("top-secret");
        }
    });
    test("passes environment overrides to child processes", async () => {
        const result = await runCommand(process.execPath, ["-e", "process.stdout.write(process.env.OPENCLI_TEST_ENV || '')"], {
            env: {
                OPENCLI_TEST_ENV: "from-run-command-test"
            },
            timeoutMs: 1000
        });
        expect(result.stdout).toBe("from-run-command-test");
    });
    test("kills a process when a watched output file stalls", async () => {
        const tempDir = await mkdtemp(path.join(os.tmpdir(), "run-command-watch-"));
        const watchedPath = path.join(tempDir, "output.bin");
        await expect(runCommand(process.execPath, ["-e", "setTimeout(() => {}, 10000)"], {
            timeoutMs: 10000,
            watchFile: {
                path: watchedPath,
                stallMs: 50,
                pollMs: 10
            }
        })).rejects.toThrow(/watched output did not grow/i);
    });
    test("kills a process when watched directory outputs stall", async () => {
        const tempDir = await mkdtemp(path.join(os.tmpdir(), "run-command-watch-dir-"));
        await expect(runCommand(process.execPath, ["-e", "setTimeout(() => {}, 10000)"], {
            timeoutMs: 10000,
            watchFile: {
                directory: tempDir,
                prefix: "source.",
                stallMs: 50,
                pollMs: 10
            }
        })).rejects.toThrow(/watched output did not grow/i);
    });
    test("ignores configured suffixes when watching directory growth", async () => {
        const tempDir = await mkdtemp(path.join(os.tmpdir(), "run-command-watch-ignore-"));
        const stateFile = path.join(tempDir, "source.mp4.ytdl");
        await expect(runCommand(process.execPath, ["-e", `require('fs').writeFileSync(${JSON.stringify(stateFile)}, 'progress marker'); setTimeout(() => {}, 10000)`], {
            timeoutMs: 10000,
            watchFile: {
                directory: tempDir,
                prefix: "source.",
                ignoreSuffixes: [".ytdl"],
                stallMs: 50,
                pollMs: 10
            }
        })).rejects.toThrow(/watched output did not grow/i);
    });
    test("ignores configured filename fragments when watching directory growth", async () => {
        const tempDir = await mkdtemp(path.join(os.tmpdir(), "run-command-watch-frag-"));
        const fragmentFile = path.join(tempDir, "source.mp4-Frag4");
        await expect(runCommand(process.execPath, ["-e", `require('fs').writeFileSync(${JSON.stringify(fragmentFile)}, 'temporary fragment'); setTimeout(() => {}, 10000)`], {
            timeoutMs: 10000,
            watchFile: {
                directory: tempDir,
                prefix: "source.",
                ignoreIncludes: ["-Frag"],
                stallMs: 50,
                pollMs: 10
            }
        })).rejects.toThrow(/watched output did not grow/i);
    });
});
