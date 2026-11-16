import { mkdtemp, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { afterEach, describe, expect, test } from "vitest";
import { createLocalFileServer } from "../src/utils/local-file-server.js";
const cleanupTasks = [];
afterEach(async () => {
    while (cleanupTasks.length > 0) {
        const task = cleanupTasks.pop();
        if (task) {
            await task();
        }
    }
});
describe("createLocalFileServer", () => {
    test("serves a local file with permissive CORS headers", async () => {
        const tempDir = await mkdtemp(path.join(os.tmpdir(), "upload-server-"));
        const filePath = path.join(tempDir, "sample.txt");
        await writeFile(filePath, "hello upload", "utf8");
        const server = await createLocalFileServer(filePath);
        cleanupTasks.push(async () => {
            await server.close();
        });
        const response = await fetch(server.url);
        expect(response.status).toBe(200);
        expect(response.headers.get("access-control-allow-origin")).toBe("*");
        expect(await response.text()).toBe("hello upload");
    });
});
