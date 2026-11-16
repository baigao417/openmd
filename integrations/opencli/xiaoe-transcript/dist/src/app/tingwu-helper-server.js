import { createTingwuHelperServer } from "./tingwu-helper-service.js";
import { getTranscriptRootDir } from "./transcript-config-store.js";
import { runTingwuExportToMarkdown } from "../pipeline/tingwu-export.js";
const port = Number.parseInt(process.env.TINGWU_HELPER_PORT ?? "19827", 10);
const rootDir = getTranscriptRootDir(process.env.TINGWU_HELPER_ROOT);
const helper = await createTingwuHelperServer({
    rootDir,
    port,
    runCurrent: async (request, onStageChange) => {
        return await runTingwuExportToMarkdown({
            url: request.url,
            outDir: request.outDir,
            preferredTitle: request.title,
            tabId: request.tabId,
            pageId: request.pageId
        }, undefined, onStageChange);
    }
});
await import("./transcript-config-store.js").then(async ({ readTranscriptState, writeTranscriptState }) => {
    const state = await readTranscriptState(rootDir);
    if (state.status === "running") {
        await writeTranscriptState(rootDir, {
            ...state,
            status: "idle",
            currentStage: "idle",
            log: [...(state.log ?? []), "helper-restart:reset-stale-running-state"]
        });
    }
});
console.log(`[tingwu-helper] listening on ${helper.baseUrl}`);
const stop = async () => {
    await helper.close();
    process.exit(0);
};
process.on("SIGINT", () => { void stop(); });
process.on("SIGTERM", () => { void stop(); });
