import { runGetBatchExport } from "../pipeline/get-batch-export.js";
import { listGetSubscriptions } from "../providers/get-subscription.js";
import { resolveGetExportRuntimeOptions } from "./get-export-runtime-options.js";

interface GetExportCommandDependencies {
  listSubscriptions: typeof listGetSubscriptions;
  resolveRuntimeOptions: typeof resolveGetExportRuntimeOptions;
  runGetBatchExport: typeof runGetBatchExport;
}

function createDefaultDependencies(): GetExportCommandDependencies {
  return {
    listSubscriptions: listGetSubscriptions,
    resolveRuntimeOptions: resolveGetExportRuntimeOptions,
    runGetBatchExport
  };
}

function requireString(value: unknown, name: string): string {
  if (typeof value !== "string" || value.trim().length === 0) {
    throw new Error(`${name} is required`);
  }
  return value.trim();
}

function parseOptionalString(value: unknown): string | undefined {
  if (typeof value !== "string") {
    return undefined;
  }
  const trimmed = value.trim();
  return trimmed || undefined;
}

function parseOptionalInt(value: unknown): number | undefined {
  if (value === undefined || value === null || value === "") {
    return undefined;
  }
  const parsed = Number.parseInt(String(value), 10);
  if (!Number.isFinite(parsed) || parsed <= 0) {
    throw new Error("comment-limit must be a positive integer");
  }
  return parsed;
}

export async function runGetSubscriptionsCommand(
  _kwargs: Record<string, unknown>,
  overrides?: Partial<GetExportCommandDependencies>
) {
  const deps = {
    ...createDefaultDependencies(),
    ...overrides
  } satisfies GetExportCommandDependencies;

  const subscriptions = await deps.listSubscriptions();
  return subscriptions.map((item) => ({
    creatorId: item.creatorId,
    creatorName: item.creatorName,
    statsInfo: item.statsInfo ?? "",
    subjectUrl: item.subjectUrl
  }));
}

export async function runGetExportSubscriptionCommand(
  kwargs: Record<string, unknown>,
  overrides?: Partial<GetExportCommandDependencies>
) {
  const deps = {
    ...createDefaultDependencies(),
    ...overrides
  } satisfies GetExportCommandDependencies;

  const runtime = await deps.resolveRuntimeOptions({
    outDir: parseOptionalString(kwargs.out),
    persistOutputDir: typeof kwargs.out === "string" && kwargs.out.trim().length > 0
  });
  const subjectUrl = parseOptionalString(kwargs["subject-url"]);
  const creatorId = parseOptionalString(kwargs["creator-id"]);
  if (!subjectUrl && !creatorId) {
    throw new Error("subject-url or creator-id is required");
  }

  const result = await deps.runGetBatchExport({
    subjectUrl,
    creatorId,
    creatorName: parseOptionalString(kwargs["creator-name"]),
    outDir: runtime.outDir,
    startDate: parseOptionalString(kwargs["start-date"]),
    endDate: parseOptionalString(kwargs["end-date"]),
    skipExisting: kwargs["skip-existing"] === undefined ? true : Boolean(kwargs["skip-existing"]),
    includeComments: kwargs["with-comments"] === undefined ? false : Boolean(kwargs["with-comments"]),
    commentLimit: parseOptionalInt(kwargs["comment-limit"])
  }, undefined);

  return [{
    creatorDir: result.creatorDir,
    creatorId: result.creatorId,
    creatorName: result.creatorName,
    total: result.total,
    success: result.success,
    skipped: result.skipped,
    failed: result.failed
  }];
}

export { requireString };
