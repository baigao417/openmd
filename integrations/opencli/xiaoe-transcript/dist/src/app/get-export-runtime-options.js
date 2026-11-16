import { readGetExportConfig, updateGetExportConfig } from "./get-export-config-store.js";
export async function resolveGetExportRuntimeOptions(input) {
    const config = await readGetExportConfig(input.rootDir);
    const normalizedOutDir = input.outDir === undefined ? undefined : input.outDir.trim();
    const outDir = normalizedOutDir ?? config.defaultOutputDir;
    if (input.persistOutputDir && normalizedOutDir !== undefined) {
        const updated = await updateGetExportConfig(input.rootDir, {
            defaultOutputDir: outDir
        });
        return {
            outDir,
            config: updated
        };
    }
    return {
        outDir,
        config
    };
}
