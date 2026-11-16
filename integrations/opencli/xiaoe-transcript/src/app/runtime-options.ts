import { readXiaoeConfig, updateXiaoeConfig, type XiaoeConfig } from "./config-store.js";

export interface ResolvedRuntimeOptions {
  outDir: string;
  provider: "auto" | "tingwu" | "get";
  config: XiaoeConfig;
}

export async function resolveRuntimeOptions(input: {
  rootDir?: string;
  outDir?: string;
  provider?: string;
  persistOutputDir?: boolean;
}): Promise<ResolvedRuntimeOptions> {
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
