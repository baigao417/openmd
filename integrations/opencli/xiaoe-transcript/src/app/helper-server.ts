import { createXiaoeHelperServer } from "./helper-service.js";
import { getXiaoeRootDir } from "./config-store.js";
import { runXiaoeToMarkdown } from "../workflow.js";
import { readXiaoeCatalog, runXiaoeBatchTranscript } from "../pipeline/xiaoe-batch.js";

const port = Number.parseInt(process.env.XIAOE_HELPER_PORT ?? "19826", 10);
const rootDir = getXiaoeRootDir(process.env.XIAOE_HELPER_ROOT);

const helper = await createXiaoeHelperServer({
  rootDir,
  port,
  runCurrent: async (request, onStageChange) => {
    return await runXiaoeToMarkdown({
      url: request.url,
      outDir: request.outDir,
      provider: request.provider as "auto" | "tingwu" | "get",
      keepArtifacts: false,
      timeoutMinutes: request.timeoutMinutes ?? 180
    }, undefined, onStageChange);
  },
  readCatalog: async (courseUrl) => await readXiaoeCatalog(courseUrl),
  runBatch: async (batchOptions, onProgress) => await runXiaoeBatchTranscript(batchOptions, undefined, onProgress),
  exitOnShutdown: true
});

await import("./config-store.js").then(async ({ readXiaoeState, writeXiaoeState }) => {
  const state = await readXiaoeState(rootDir);
  if (state.status === "running") {
    await writeXiaoeState(rootDir, {
      ...state,
      status: "idle",
      currentStage: "idle",
      log: [...(state.log ?? []), "helper-restart:reset-stale-running-state"]
    });
  }
});

console.log(`[xiaoe-helper] listening on ${helper.baseUrl}`);

const stop = async () => {
  await helper.close();
  process.exit(0);
};

process.on("SIGINT", () => { void stop(); });
process.on("SIGTERM", () => { void stop(); });
