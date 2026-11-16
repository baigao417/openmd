import { createServer } from "node:http";
import { basename } from "node:path";
import { readFile } from "node:fs/promises";
function guessContentType(fileName) {
    const lower = fileName.toLowerCase();
    if (lower.endsWith(".wav"))
        return "audio/wav";
    if (lower.endsWith(".mp3"))
        return "audio/mpeg";
    if (lower.endsWith(".m4a"))
        return "audio/mp4";
    if (lower.endsWith(".mp4"))
        return "video/mp4";
    return "application/octet-stream";
}
export async function createLocalFileServer(filePath) {
    const fileName = basename(filePath);
    const server = createServer(async (_request, response) => {
        try {
            const body = await readFile(filePath);
            response.writeHead(200, {
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "GET, OPTIONS",
                "Access-Control-Allow-Headers": "*",
                "Content-Type": guessContentType(fileName),
                "Content-Length": String(body.byteLength),
                "Content-Disposition": `inline; filename=\"${fileName}\"`
            });
            response.end(body);
        }
        catch (error) {
            response.writeHead(500, {
                "Access-Control-Allow-Origin": "*",
                "Content-Type": "text/plain; charset=utf-8"
            });
            response.end(error instanceof Error ? error.message : String(error));
        }
    });
    await new Promise((resolve, reject) => {
        server.listen(0, "127.0.0.1", () => resolve());
        server.once("error", reject);
    });
    const address = server.address();
    if (!address || typeof address === "string") {
        throw new Error("unable to determine local upload server address");
    }
    return {
        url: `http://127.0.0.1:${address.port}/${encodeURIComponent(fileName)}`,
        fileName,
        close: async () => {
            await new Promise((resolve, reject) => {
                server.close((error) => {
                    if (error) {
                        reject(error);
                        return;
                    }
                    resolve();
                });
            });
        }
    };
}
