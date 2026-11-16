import { OpenCliPage } from "../opencli/client.js";
import type { DouyinVideoSummary } from "../types.js";

const DOUYIN_API_AID = "6383";
const DEFAULT_DOUYIN_PAGE_SIZE = 20;

interface DouyinVideosPageLike {
  goto(url: string): Promise<void>;
  evaluate<T = unknown>(js: string): Promise<T>;
  getCookies(options?: { domain?: string; url?: string }): Promise<Array<{
    name: string;
    value: string;
    domain: string;
    path: string;
    secure: boolean;
    httpOnly: boolean;
    expirationDate?: number;
  }>>;
  closeWindow(): Promise<void>;
}

interface DouyinVideosDependencies {
  createPage: () => DouyinVideosPageLike;
  resolveSourceUrl: (sourceUrl: string) => Promise<string>;
}

interface DouyinPostPagePayload {
  aweme_list?: Array<Record<string, unknown>>;
  has_more?: number;
  max_cursor?: number;
}

export interface DouyinResolvedUser {
  creatorName?: string;
  secUid: string;
  userAgent?: string;
  cookies: Array<{
    name: string;
    value: string;
    domain: string;
    path: string;
    secure: boolean;
    httpOnly: boolean;
    expirationDate?: number;
  }>;
}

export interface DouyinVideoCollector {
  resolveUser(input: { sourceUrl?: string; secUid?: string }): Promise<DouyinResolvedUser>;
  collectVideos(input: { sourceUrl?: string; secUid?: string; limit?: number }): Promise<{
    creatorName: string;
    creatorSecUid: string;
    userAgent?: string;
    cookies: DouyinResolvedUser["cookies"];
    videos: DouyinVideoSummary[];
  }>;
  close(): Promise<void>;
}

function createDefaultDependencies(): DouyinVideosDependencies {
  return {
    createPage: () => new OpenCliPage(`douyin-videos:${Date.now()}`),
    resolveSourceUrl: async (sourceUrl: string) => {
      const response = await fetch(sourceUrl, {
        method: "GET",
        redirect: "follow",
        headers: {
          "User-Agent": "Mozilla/5.0"
        }
      });
      return response.url || sourceUrl;
    }
  };
}

export function normalizeDouyinBatchLimit(limit: number | undefined): number | undefined {
  if (limit === undefined) {
    return undefined;
  }
  const numeric = Number(limit);
  if (!Number.isFinite(numeric) || numeric <= 0) {
    return undefined;
  }
  return Math.floor(numeric);
}

export function extractSecUidFromDouyinUrl(sourceUrl: string | undefined): string | undefined {
  if (!sourceUrl) {
    return undefined;
  }
  try {
    const parsed = new URL(sourceUrl);
    if (!/(^|\.)douyin\.com$|(^|\.)iesdouyin\.com$|(^|\.)v\.douyin\.com$/.test(parsed.hostname)) {
      return undefined;
    }
    const querySecUid = parsed.searchParams.get("sec_uid");
    if (querySecUid) {
      return querySecUid;
    }

    const userMatch = parsed.pathname.match(/\/user\/([^/?]+)/);
    if (userMatch) {
      return userMatch[1];
    }
  } catch {
    return undefined;
  }
  return undefined;
}

function normalizeCreatorName(rawValue: unknown): string {
  return typeof rawValue === "string" ? rawValue.trim() : "";
}

function isUsefulCreatorName(value: string | undefined): boolean {
  if (!value) {
    return false;
  }
  return !["更多", "分享主页", "关注"].includes(value.trim());
}

function normalizeDateFromUnixSeconds(rawValue: unknown): string | undefined {
  if (typeof rawValue !== "number" || !Number.isFinite(rawValue)) {
    return undefined;
  }
  return new Date(rawValue * 1000).toISOString();
}

async function browserFetchJson<T>(
  page: DouyinVideosPageLike,
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

async function fetchDouyinUserPostPage(
  page: DouyinVideosPageLike,
  secUid: string,
  maxCursor: string
): Promise<DouyinPostPagePayload> {
  const params = new URLSearchParams({
    sec_user_id: secUid,
    max_cursor: maxCursor,
    count: String(DEFAULT_DOUYIN_PAGE_SIZE),
    aid: DOUYIN_API_AID
  });
  return await browserFetchJson<DouyinPostPagePayload>(
    page,
    "GET",
    `https://www.douyin.com/aweme/v1/web/aweme/post/?${params.toString()}`,
    { referer: "https://www.douyin.com/" }
  );
}

function mapAwemeItemToVideoSummary(item: Record<string, unknown>): DouyinVideoSummary | undefined {
  const awemeId = typeof item.aweme_id === "string" ? item.aweme_id : undefined;
  const desc = typeof item.desc === "string" ? item.desc.trim() : "";
  const video = item.video && typeof item.video === "object" ? item.video as Record<string, unknown> : undefined;
  const playAddr = video?.play_addr && typeof video.play_addr === "object" ? video.play_addr as Record<string, unknown> : undefined;
  const playUrl = Array.isArray(playAddr?.url_list) ? String((playAddr?.url_list as unknown[])[0] ?? "") : "";
  const author = item.author && typeof item.author === "object" ? item.author as Record<string, unknown> : undefined;
  const statistics = item.statistics && typeof item.statistics === "object" ? item.statistics as Record<string, unknown> : undefined;
  const mixInfo = item.mix_info && typeof item.mix_info === "object" ? item.mix_info as Record<string, unknown> : undefined;

  if (!awemeId || !playUrl) {
    return undefined;
  }

  return {
    awemeId,
    title: desc || awemeId,
    sourceUrl: `https://www.douyin.com/video/${awemeId}`,
    playUrl,
    durationSec: Math.max(1, Math.round((typeof video?.duration === "number" ? video.duration : 0) / 1000)),
    diggCount: typeof statistics?.digg_count === "number" ? statistics.digg_count : 0,
    commentCount: typeof statistics?.comment_count === "number" ? statistics.comment_count : 0,
    shareCount: typeof statistics?.share_count === "number" ? statistics.share_count : 0,
    collectCount: typeof statistics?.collect_count === "number" ? statistics.collect_count : 0,
    createTime: normalizeDateFromUnixSeconds(item.create_time),
    creatorName: normalizeCreatorName(author?.nickname) || "douyin-user",
    creatorSecUid: normalizeCreatorName(author?.sec_uid),
    collectionId: normalizeCreatorName(mixInfo?.mix_id) || undefined,
    collectionName: normalizeCreatorName(mixInfo?.mix_name) || undefined
  };
}

export function createDouyinVideoCollector(
  overrides?: Partial<DouyinVideosDependencies>
): DouyinVideoCollector {
  const deps = {
    ...createDefaultDependencies(),
    ...overrides
  } satisfies DouyinVideosDependencies;
  const page = deps.createPage();

  return {
    async resolveUser(input: { sourceUrl?: string; secUid?: string }): Promise<DouyinResolvedUser> {
      const normalizedSecUid = input.secUid?.trim();
      if (normalizedSecUid) {
        await page.goto(`https://www.douyin.com/user/${normalizedSecUid}`);
        const cookies = await page.getCookies({ domain: "douyin.com" });
        const userAgent = await page.evaluate<string>("navigator.userAgent").catch(() => undefined);
        const creatorName = await page.evaluate<string>(`
          (() => {
            const raw = document.body.innerText || "";
            const lines = raw.split(/\\n+/).map((line) => line.trim()).filter(Boolean);
            const marker = lines.indexOf('分享主页');
            if (marker > 0) {
              return lines[marker - 1] || '';
            }
            return '';
          })()
        `).catch(() => "");
        return {
          secUid: normalizedSecUid,
          creatorName: normalizeCreatorName(creatorName) || undefined,
          userAgent,
          cookies
        };
      }

      if (!input.sourceUrl?.trim()) {
        throw new Error("douyin batch export requires sourceUrl or secUid");
      }

      const resolvedUrl = extractSecUidFromDouyinUrl(input.sourceUrl)
        ? input.sourceUrl
        : await deps.resolveSourceUrl(input.sourceUrl);
      const secUid = extractSecUidFromDouyinUrl(resolvedUrl);
      if (!secUid) {
        throw new Error("unable to resolve douyin sec_uid from source url");
      }
      return await this.resolveUser({ secUid });
    },

    async collectVideos(input: { sourceUrl?: string; secUid?: string; limit?: number }) {
      const resolved = await this.resolveUser(input);
      const limit = normalizeDouyinBatchLimit(input.limit);
      let cursor = "0";
      const videos: DouyinVideoSummary[] = [];
      const seen = new Set<string>();

      for (let pageIndex = 0; pageIndex < 500; pageIndex += 1) {
        const payload = await fetchDouyinUserPostPage(page, resolved.secUid, cursor);
        const items = Array.isArray(payload.aweme_list) ? payload.aweme_list : [];
        for (const rawItem of items) {
          if (!rawItem || typeof rawItem !== "object") {
            continue;
          }
          const mapped = mapAwemeItemToVideoSummary(rawItem as Record<string, unknown>);
          if (!mapped || seen.has(mapped.awemeId)) {
            continue;
          }
          seen.add(mapped.awemeId);
          videos.push(mapped);
        if (limit && videos.length >= limit) {
          return {
              creatorName: isUsefulCreatorName(resolved.creatorName) ? String(resolved.creatorName) : mapped.creatorName,
              creatorSecUid: resolved.secUid,
              userAgent: resolved.userAgent,
              cookies: resolved.cookies,
              videos
            };
          }
        }

        if (!payload.has_more) {
          break;
        }
        cursor = String(payload.max_cursor ?? "0");
      }

      return {
        creatorName: isUsefulCreatorName(resolved.creatorName) ? String(resolved.creatorName) : (videos[0]?.creatorName || resolved.secUid),
        creatorSecUid: resolved.secUid,
        userAgent: resolved.userAgent,
        cookies: resolved.cookies,
        videos
      };
    },

    async close(): Promise<void> {
      await page.closeWindow();
    }
  };
}
