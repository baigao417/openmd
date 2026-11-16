import { OpenCliPage } from "../opencli/client.js";
const DOUYIN_COMMENT_API_AID = "6383";
const DEFAULT_DOUYIN_COMMENT_LIMIT = 10;
const MAX_DOUYIN_COMMENT_LIMIT = 20;
function createDefaultDependencies() {
    return {
        createPage: () => new OpenCliPage(`douyin-comments:${Date.now()}`),
        resolveSourceUrl: resolveDouyinSourceUrl
    };
}
export function normalizeDouyinCommentLimit(limit) {
    const numeric = Number(limit);
    if (!Number.isFinite(numeric)) {
        return DEFAULT_DOUYIN_COMMENT_LIMIT;
    }
    return Math.min(MAX_DOUYIN_COMMENT_LIMIT, Math.max(1, Math.round(numeric)));
}
export function extractDouyinAwemeIdFromUrl(sourceUrl) {
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
    }
    catch {
        return undefined;
    }
    return undefined;
}
export async function resolveDouyinSourceUrl(sourceUrl) {
    const response = await fetch(sourceUrl, {
        method: "GET",
        redirect: "follow",
        headers: {
            "User-Agent": "Mozilla/5.0"
        }
    });
    return response.url || sourceUrl;
}
async function browserFetchJson(page, method, url, headers = {}) {
    return await page.evaluate(`
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
async function fetchDouyinAwemeDetail(page, awemeId) {
    const params = new URLSearchParams({
        aweme_id: awemeId,
        aid: DOUYIN_COMMENT_API_AID
    });
    const payload = await browserFetchJson(page, "GET", `https://www.douyin.com/aweme/v1/web/aweme/detail/?${params.toString()}`, { referer: "https://www.douyin.com/" });
    return payload && typeof payload === "object" && "aweme_detail" in payload
        ? (payload.aweme_detail ?? {})
        : {};
}
function mapCommentRows(payload, limit) {
    const comments = payload && typeof payload === "object" && "comments" in payload
        ? payload.comments
        : undefined;
    if (!Array.isArray(comments)) {
        return [];
    }
    return comments.slice(0, limit).map((comment) => ({
        text: typeof comment.text === "string" ? comment.text : "",
        diggCount: typeof comment.digg_count === "number" ? comment.digg_count : 0,
        nickname: typeof comment.user === "object" && comment.user && "nickname" in comment.user
            ? String(comment.user.nickname ?? "")
            : ""
    }));
}
export function createDouyinCommentCollector(overrides) {
    const deps = {
        ...createDefaultDependencies(),
        ...overrides
    };
    const page = deps.createPage();
    let ready = false;
    async function ensureReady() {
        if (ready) {
            return;
        }
        await page.goto("https://www.douyin.com/");
        ready = true;
    }
    return {
        async fetchCommentsBySourceUrl(sourceUrl, limit) {
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
                ? detail.mix_info
                : undefined;
            const params = new URLSearchParams({
                aweme_id: awemeId,
                count: String(normalizeDouyinCommentLimit(limit)),
                cursor: "0",
                aid: DOUYIN_COMMENT_API_AID
            });
            const payload = await browserFetchJson(page, "GET", `https://www.douyin.com/aweme/v1/web/comment/list/?${params.toString()}`, { referer: "https://www.douyin.com/" });
            return {
                status: "success",
                awemeId,
                collectionId: typeof mixInfo?.mix_id === "string" ? mixInfo.mix_id : undefined,
                collectionName: typeof mixInfo?.mix_name === "string" ? mixInfo.mix_name : undefined,
                comments: mapCommentRows(payload, normalizeDouyinCommentLimit(limit))
            };
        },
        async close() {
            await page.closeWindow();
        }
    };
}
