import { createServer, type IncomingMessage, type ServerResponse } from "node:http";
import { URL } from "node:url";

import { listGetSubscriptions } from "../providers/get-subscription.js";
import type { GetBatchExportResult, GetExportRunState, GetSubscriptionSummary } from "../types.js";
import {
  readGetExportConfig,
  readGetExportState,
  updateGetExportConfig,
  writeGetExportState
} from "./get-export-config-store.js";

export interface RunGetExportRequest {
  subjectUrl?: string;
  creatorId?: string;
  creatorName?: string;
  outDir: string;
  startDate?: string;
  endDate?: string;
  skipExisting: boolean;
  includeComments?: boolean;
  commentLimit?: number;
}

export interface GetHelperServer {
  baseUrl: string;
  close(): Promise<void>;
}

export async function createGetHelperServer(options: {
  rootDir?: string;
  port?: number;
  listSubscriptions?: () => Promise<GetSubscriptionSummary[]>;
  runExport: (
    request: RunGetExportRequest,
    onProgress?: (event: {
      stage: string;
      status: "started" | "completed" | "failed";
      detail?: string;
      counts?: Pick<GetExportRunState, "total" | "successCount" | "skippedCount" | "failedCount">;
    }) => Promise<void>
  ) => Promise<GetBatchExportResult>;
}): Promise<GetHelperServer> {
  const rootDir = options.rootDir;
  const listSubscriptions = options.listSubscriptions ?? listGetSubscriptions;
  let busy = false;

  async function readJsonBody(req: IncomingMessage): Promise<Record<string, unknown>> {
    const chunks: Buffer[] = [];
    for await (const chunk of req) {
      chunks.push(Buffer.from(chunk));
    }
    if (chunks.length === 0) {
      return {};
    }
    return JSON.parse(Buffer.concat(chunks).toString("utf8")) as Record<string, unknown>;
  }

  function send(res: ServerResponse, status: number, data: unknown): void {
    res.writeHead(status, {
      "Access-Control-Allow-Origin": "*",
      "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
      "Access-Control-Allow-Headers": "Content-Type",
      "Content-Type": "application/json; charset=utf-8"
    });
    res.end(JSON.stringify(data));
  }

  const server = createServer(async (req, res) => {
    if (req.method === "OPTIONS") {
      send(res, 204, {});
      return;
    }

    const url = new URL(req.url || "/", "http://127.0.0.1");
    if (req.method === "GET" && url.pathname === "/status") {
      send(res, 200, {
        ok: true,
        config: await readGetExportConfig(rootDir),
        state: await readGetExportState(rootDir)
      });
      return;
    }

    if (req.method === "GET" && url.pathname === "/config") {
      send(res, 200, await readGetExportConfig(rootDir));
      return;
    }

    if (req.method === "POST" && url.pathname === "/config") {
      const body = await readJsonBody(req);
      const config = await updateGetExportConfig(rootDir, {
        defaultOutputDir: typeof body.defaultOutputDir === "string" ? body.defaultOutputDir : undefined
      });
      send(res, 200, config);
      return;
    }

    if (req.method === "GET" && url.pathname === "/subscriptions") {
      const items = await listSubscriptions();
      send(res, 200, { items });
      return;
    }

    if (req.method === "POST" && url.pathname === "/run-export") {
      if (busy) {
        send(res, 409, { ok: false, error: "A Get export task is already running" });
        return;
      }
      const body = await readJsonBody(req);
      const config = await readGetExportConfig(rootDir);
      const outDir = typeof body.outDirOverride === "string" && body.outDirOverride.trim()
        ? body.outDirOverride.trim()
        : config.defaultOutputDir;
      if (!outDir.trim()) {
        send(res, 400, { ok: false, error: "请先设置默认保存目录" });
        return;
      }

      const startedAt = new Date().toISOString();
      const request: RunGetExportRequest = {
        subjectUrl: typeof body.subjectUrl === "string" ? body.subjectUrl.trim() || undefined : undefined,
        creatorId: typeof body.creatorId === "string" ? body.creatorId.trim() || undefined : undefined,
        creatorName: typeof body.creatorName === "string" ? body.creatorName.trim() || undefined : undefined,
        outDir,
        startDate: typeof body.startDate === "string" ? body.startDate.trim() || undefined : undefined,
        endDate: typeof body.endDate === "string" ? body.endDate.trim() || undefined : undefined,
        skipExisting: body.skipExisting === undefined ? true : Boolean(body.skipExisting),
        includeComments: body.includeComments === undefined ? false : Boolean(body.includeComments),
        commentLimit: typeof body.commentLimit === "number" && Number.isFinite(body.commentLimit) ? body.commentLimit : undefined
      };
      if (!request.subjectUrl && !request.creatorId) {
        send(res, 400, { ok: false, error: "请至少提供 subjectUrl 或 creatorId" });
        return;
      }

      busy = true;
      await writeGetExportState(rootDir, {
        status: "running",
        currentStage: "queued",
        total: 0,
        successCount: 0,
        skippedCount: 0,
        failedCount: 0,
        log: ["任务已加入队列"],
        lastRun: {
          subjectUrl: request.subjectUrl,
          creatorId: request.creatorId,
          creatorName: request.creatorName,
          startedAt
        }
      });

      void options.runExport(request, async (event) => {
        const current = await readGetExportState(rootDir);
        const nextLog = [...(current.log ?? []), `${event.status}:${event.stage}${event.detail ? `:${event.detail}` : ""}`];
        await writeGetExportState(rootDir, {
          ...current,
          currentStage: event.stage,
          total: event.counts?.total ?? current.total,
          successCount: event.counts?.successCount ?? current.successCount,
          skippedCount: event.counts?.skippedCount ?? current.skippedCount,
          failedCount: event.counts?.failedCount ?? current.failedCount,
          log: nextLog.slice(-50)
        });
      }).then(async (result) => {
        busy = false;
        await writeGetExportState(rootDir, {
          status: "success",
          currentStage: "done",
          total: result.total,
          successCount: result.success,
          skippedCount: result.skipped,
          failedCount: result.failed,
          log: ["任务执行成功"],
          lastRun: {
            subjectUrl: request.subjectUrl,
            creatorId: result.creatorId,
            creatorName: result.creatorName,
            creatorDir: result.creatorDir,
            total: result.total,
            success: result.success,
            skipped: result.skipped,
            failed: result.failed,
            startedAt,
            finishedAt: new Date().toISOString()
          }
        });
      }).catch(async (error) => {
        busy = false;
        await writeGetExportState(rootDir, {
          status: "error",
          currentStage: "failed",
          total: 0,
          successCount: 0,
          skippedCount: 0,
          failedCount: 1,
          log: [error instanceof Error ? error.message : String(error)],
          lastRun: {
            subjectUrl: request.subjectUrl,
            creatorId: request.creatorId,
            creatorName: request.creatorName,
            error: error instanceof Error ? error.message : String(error),
            startedAt,
            finishedAt: new Date().toISOString()
          }
        });
      });

      send(res, 202, { accepted: true, state: await readGetExportState(rootDir) });
      return;
    }

    if (req.method === "POST" && url.pathname === "/shutdown") {
      send(res, 200, { ok: true });
      setTimeout(() => server.close(), 50);
      return;
    }

    send(res, 404, { ok: false, error: "Not found" });
  });

  await new Promise<void>((resolve, reject) => {
    server.listen(options.port ?? 19828, "127.0.0.1", () => resolve());
    server.once("error", reject);
  });

  const address = server.address();
  if (!address || typeof address === "string") {
    throw new Error("Unable to determine helper server address");
  }

  return {
    baseUrl: `http://127.0.0.1:${address.port}`,
    close: async () => {
      await new Promise<void>((resolve, reject) => {
        server.close((error) => {
          if (error) {
            reject(error);
            return;
          }
          resolve();
        });
      });
    }
  };
}
