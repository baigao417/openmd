import { readXiaoeConfig, updateXiaoeConfig } from "./config-store.js";
export async function resolveRuntimeOptions(input) {
    const config = await readXiaoeConfig(input.rootDir);
    const normalizedOutDir = input.outDir === undefined ? undefined : input.outDir.trim();
    const outDir = normalizedOutDir ?? config.defaultOutputDir;
    const provider = input.provider === "tingwu" || input.provider === "get" || input.provider === "auto"
        ? input.provider
        : config.defaultProvider;
    if (input.persistOutputDir && normalizedOutDir !== undefined) {
        const updated = await updateXiaoeConfig(input.rootDir, {
            defaultOutputDir: outDir
        });
        return { outDir, provider, config: updated };
    }
    return { outDir, provider, config };
}
