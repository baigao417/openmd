import { readTranscriptConfig, updateTranscriptConfig, type TranscriptConfig } from "./transcript-config-store.js";

export interface ResolvedTranscriptRuntimeOptions {
  outDir: string;
  provider: "auto" | "tingwu" | "get";
  config: TranscriptConfig;
}

export async function resolveTranscriptRuntimeOptions(input: {
  rootDir?: string;
  outDir?: string;
  provider?: string;
  persistOutputDir?: boolean;
}): Promise<ResolvedTranscriptRuntimeOptions> {
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
