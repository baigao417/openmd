import { createGetHelperServer } from "./get-helper-service.js";
import { getGetExportRootDir } from "./get-export-config-store.js";
import { runGetBatchExport } from "../pipeline/get-batch-export.js";
const port = Number.parseInt(process.env.GET_HELPER_PORT ?? "19828", 10);
const rootDir = getGetExportRootDir(process.env.GET_HELPER_ROOT);
const helper = await createGetHelperServer({
    rootDir,
    port,
    runExport: async (request, onProgress) => {
        return await runGetBatchExport({
            subjectUrl: request.subjectUrl,
            creatorId: request.creatorId,
            creatorName: request.creatorName,
            outDir: request.outDir,
            startDate: request.startDate,
            endDate: request.endDate,
            skipExisting: request.skipExisting
        }, undefined, onProgress);
    }
});
console.log(`[get-helper] listening on ${helper.baseUrl}`);
process.on("SIGINT", async () => {
    await helper.close();
    process.exit(0);
});
process.on("SIGTERM", async () => {
    await helper.close();
    process.exit(0);
});
