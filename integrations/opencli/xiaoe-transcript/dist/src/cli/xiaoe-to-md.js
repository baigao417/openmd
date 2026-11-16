#!/usr/bin/env node
import { Command } from "commander";
import { resolveRuntimeOptions } from "../app/runtime-options.js";
import { runXiaoeToMarkdown } from "../workflow.js";
const program = new Command();
program
    .name("xiaoe-to-md")
    .argument("<url>", "xiaoe tong course page url")
    .option("--out <dir>", "output directory for the generated markdown")
    .option("--provider <provider>", "auto | tingwu | get", "auto")
    .option("--keep-artifacts", "keep downloaded media and temp files on success", false)
    .option("--timeout-minutes <minutes>", "timeout hint for long-running download/transcript stages in minutes", "30")
    .action(async (url, options) => {
    try {
        const runtime = await resolveRuntimeOptions({
            outDir: options.out,
            provider: options.provider,
            persistOutputDir: !!options.out
        });
        const result = await runXiaoeToMarkdown({
            url,
            outDir: runtime.outDir,
            provider: runtime.provider,
            keepArtifacts: options.keepArtifacts,
            timeoutMinutes: Number.parseInt(options.timeoutMinutes, 10)
        });
        console.log(`Markdown saved to ${result.outputPath}`);
        console.log(`Provider: ${result.provider}`);
        console.log(`Coverage: ${(result.coverage * 100).toFixed(2)}%`);
    }
    catch (error) {
        console.error(error instanceof Error ? error.message : String(error));
        process.exitCode = 1;
    }
});
await program.parseAsync(process.argv);
