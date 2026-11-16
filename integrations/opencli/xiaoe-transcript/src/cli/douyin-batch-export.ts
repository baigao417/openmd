#!/usr/bin/env node
import { defaultOutputDirectory } from "../utils/output-directory.js";
import { Command } from "commander";

import { runDouyinBatchExport } from "../pipeline/douyin-batch-export.js";

const program = new Command();

program
  .name("douyin-batch-export")
  .option("--source-url <url>", "douyin user/share url")
  .option("--sec-uid <secUid>", "douyin sec_uid")
  .option("--out <dir>", "output directory", defaultOutputDirectory())
  .option("--limit <count>", "optional limit for testing")
  .option("--comment-limit <count>", "hot comments to keep per video", "10")
  .option("--skip-existing", "skip videos that already have both video and transcript", true)
  .option("--keep-artifacts", "keep temp work directories", false)
  .action(async (options: {
    sourceUrl?: string;
    secUid?: string;
    out: string;
    limit?: string;
    commentLimit: string;
    skipExisting: boolean;
    keepArtifacts: boolean;
  }) => {
    try {
      const result = await runDouyinBatchExport({
        sourceUrl: options.sourceUrl,
        secUid: options.secUid,
        outDir: options.out,
        limit: options.limit ? Number.parseInt(options.limit, 10) : undefined,
        commentLimit: Number.parseInt(options.commentLimit, 10),
        includeComments: true,
        skipExisting: options.skipExisting,
        keepArtifacts: options.keepArtifacts
      }, undefined, (event) => {
        const counts = event.counts
          ? ` total=${event.counts.total} success=${event.counts.successCount} skipped=${event.counts.skippedCount} failed=${event.counts.failedCount}`
          : "";
        console.log(`[${new Date().toISOString()}] ${event.status}:${event.stage}${event.detail ? ` ${event.detail}` : ""}${counts}`);
      });

      console.log(JSON.stringify({
        creatorDir: result.creatorDir,
        creatorName: result.creatorName,
        creatorSecUid: result.creatorSecUid,
        total: result.total,
        success: result.success,
        skipped: result.skipped,
        failed: result.failed
      }, null, 2));
    } catch (error) {
      console.error(error instanceof Error ? error.message : String(error));
      process.exitCode = 1;
    }
  });

await program.parseAsync(process.argv);
