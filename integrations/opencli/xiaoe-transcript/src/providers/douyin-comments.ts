import { OpenCliPage } from "../opencli/client.js";
import type { DouyinCommentSummary } from "../types.js";

const DOUYIN_COMMENT_API_AID = "6383";
const DEFAULT_DOUYIN_COMMENT_LIMIT = 10;
const MAX_DOUYIN_COMMENT_LIMIT = 20;

interface DouyinCommentsPageLike {
  goto(url: string): Promise<void>;
  evaluate<T = unknown>(js: string): Promise<T>;
  closeWindow(): Promise<void>;
}

interface DouyinCommentCollectorDependencies {
  createPage: () => DouyinCommentsPageLike;
  resolveSourceUrl: (sourceUrl: string) => Promise<string>;
}

export interface DouyinCommentFetchResult {
  status: "success" | "skipped";
  awemeId?: string;
  collectionId?: string;
  collectionName?: string;
  comments: DouyinCommentSummary[];
  reason?: string;
}

export interface DouyinCommentCollector {
  fetchCommentsBySourceUrl(sourceUrl: string | undefined, limit: number): Promise<DouyinCommentFetchResult>;
  close(): Promise<void>;
}

function createDefaultDependencies(): DouyinCommentCollectorDependencies {
  return {
    createPage: () => new OpenCliPage(`douyin-comments:${Date.now()}`),
    resolveSourceUrl: resolveDouyinSourceUrl
  };
}

export function normalizeDouyinCommentLimit(limit: number | undefined): number {
  const numeric = Number(limit);
  if (!Number.isFinite(numeric)) {
    return DEFAULT_DOUYIN_COMMENT_LIMIT;
  }
  return Math.min(MAX_DOUYIN_COMMENT_LIMIT, Math.max(1, Math.round(numeric)));
}

export function extractDouyinAwemeIdFromUrl(sourceUrl: string | undefined): string | undefined {
  if (!sourceUrl) {
    return undefined;
  }

  try {
    const parsed = new URL(sourceUrl);
    const modalId = parsed.searchParams.get("modal_id");
    if (modalId && /^\d+$/.test(modalId)) {
      return modalId;
    }

    if (!/(^|\.)douyin\.com$|(^|\.)iesdouyin\.com$/.test(parsed.hostname)) {
      return undefined;
    }

    const videoMatch = parsed.pathname.match(/\/(?:share\/)?video\/(\d+)/);
    if (videoMatch) {
      return videoMatch[1];
    }
  } catch {
    return undefined;
  }

  return undefined;
}

export async function resolveDouyinSourceUrl(sourceUrl: string): Promise<string> {
  const response = await fetch(sourceUrl, {
    method: "GET",
    redirect: "follow",
    headers: {
      "User-Agent": "Mozilla/5.0"
    }
  });
  return response.url || sourceUrl;
}

async function browserFetchJson<T>(
  page: DouyinCommentsPageLike,
  method: "GET" | "POST",
  url: string,
  headers: Record<string, string> = {}
): Promise<T> {
  return await page.evaluate<T>(`
    (async () => {
      const response = await fetch(${JSON.stringify(url)}, {
        method: ${JSON.stringify(method)},
        credentials: "include",
        headers: {
          "Content-Type": "application/json",
          ...${JSON.stringify(headers)}
        }
      });
      return await response.json();
    })()
  `);
}

async function fetchDouyinAwemeDetail(
  page: DouyinCommentsPageLike,
  awemeId: string
): Promise<Record<string, unknown>> {
  const params = new URLSearchParams({
    aweme_id: awemeId,
    aid: DOUYIN_COMMENT_API_AID
  });
  const payload = await browserFetchJson<Record<string, unknown>>(
    page,
    "GET",
    `https://www.douyin.com/aweme/v1/web/aweme/detail/?${params.toString()}`,
    { referer: "https://www.douyin.com/" }
  );
  return payload && typeof payload === "object" && "aweme_detail" in payload
    ? ((payload as { aweme_detail?: Record<string, unknown> }).aweme_detail ?? {})
    : {};
}

function mapCommentRows(payload: unknown, limit: number): DouyinCommentSummary[] {
  const comments = payload && typeof payload === "object" && "comments" in payload
    ? (payload as { comments?: Array<Record<string, unknown>> }).comments
    : undefined;
  if (!Array.isArray(comments)) {
    return [];
  }

  return comments.slice(0, limit).map((comment) => ({
    text: typeof comment.text === "string" ? comment.text : "",
    diggCount: typeof comment.digg_count === "number" ? comment.digg_count : 0,
    nickname: typeof comment.user === "object" && comment.user && "nickname" in comment.user
      ? String((comment.user as { nickname?: unknown }).nickname ?? "")
      : ""
  }));
}

export function createDouyinCommentCollector(
  overrides?: Partial<DouyinCommentCollectorDependencies>
): DouyinCommentCollector {
  const deps = {
    ...createDefaultDependencies(),
    ...overrides
  } satisfies DouyinCommentCollectorDependencies;
  const page = deps.createPage();
  let ready = false;

  async function ensureReady(): Promise<void> {
    if (ready) {
      return;
    }
    await page.goto("https://www.douyin.com/");
    ready = true;
  }

  return {
    async fetchCommentsBySourceUrl(sourceUrl: string | undefined, limit: number): Promise<DouyinCommentFetchResult> {
      if (!sourceUrl?.trim()) {
        return {
          status: "skipped",
          comments: [],
          reason: "source url is empty"
        };
      }

      const resolvedSourceUrl = extractDouyinAwemeIdFromUrl(sourceUrl)
        ? sourceUrl
        : await deps.resolveSourceUrl(sourceUrl);
      const awemeId = extractDouyinAwemeIdFromUrl(resolvedSourceUrl);
      if (!awemeId) {
        return {
          status: "skipped",
          comments: [],
          reason: "source url is not a douyin video"
        };
      }

      await ensureReady();
      const detail = await fetchDouyinAwemeDetail(page, awemeId);
      const mixInfo = detail.mix_info && typeof detail.mix_info === "object"
        ? detail.mix_info as Record<string, unknown>
        : undefined;

      const params = new URLSearchParams({
        aweme_id: awemeId,
        count: String(normalizeDouyinCommentLimit(limit)),
        cursor: "0",
        aid: DOUYIN_COMMENT_API_AID
      });
      const payload = await browserFetchJson<Record<string, unknown>>(
        page,
        "GET",
        `https://www.douyin.com/aweme/v1/web/comment/list/?${params.toString()}`,
        { referer: "https://www.douyin.com/" }
      );

      return {
        status: "success",
        awemeId,
        collectionId: typeof mixInfo?.mix_id === "string" ? mixInfo.mix_id : undefined,
        collectionName: typeof mixInfo?.mix_name === "string" ? mixInfo.mix_name : undefined,
        comments: mapCommentRows(payload, normalizeDouyinCommentLimit(limit))
      };
    },

    async close(): Promise<void> {
      await page.closeWindow();
    }
  };
}
