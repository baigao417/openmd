import { readTranscriptConfig, updateTranscriptConfig } from "./transcript-config-store.js";
export async function resolveTranscriptRuntimeOptions(input) {
    const config = await readTranscriptConfig(input.rootDir);
    const normalizedOutDir = input.outDir === undefined ? undefined : input.outDir.trim();
    const outDir = normalizedOutDir ?? config.defaultOutputDir;
    const provider = input.provider === "tingwu" || input.provider === "get" || input.provider === "auto"
        ? input.provider
        : config.defaultProvider;
    if (input.persistOutputDir && normalizedOutDir !== undefined) {
        const updated = await updateTranscriptConfig(input.rootDir, {
            defaultOutputDir: outDir
        });
        return { outDir, provider, config: updated };
    }
    return { outDir, provider, config };
}
