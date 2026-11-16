import { OpenCliPage } from "../opencli/client.js";
const DOUYIN_API_AID = "6383";
const DEFAULT_DOUYIN_PAGE_SIZE = 20;
function createDefaultDependencies() {
    return {
        createPage: () => new OpenCliPage(`douyin-videos:${Date.now()}`),
        resolveSourceUrl: async (sourceUrl) => {
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
export function normalizeDouyinBatchLimit(limit) {
    if (limit === undefined) {
        return undefined;
    }
    const numeric = Number(limit);
    if (!Number.isFinite(numeric) || numeric <= 0) {
        return undefined;
    }
    return Math.floor(numeric);
}
export function extractSecUidFromDouyinUrl(sourceUrl) {
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
    }
    catch {
        return undefined;
    }
    return undefined;
}
function normalizeCreatorName(rawValue) {
    return typeof rawValue === "string" ? rawValue.trim() : "";
}
function isUsefulCreatorName(value) {
    if (!value) {
        return false;
    }
    return !["更多", "分享主页", "关注"].includes(value.trim());
}
function normalizeDateFromUnixSeconds(rawValue) {
    if (typeof rawValue !== "number" || !Number.isFinite(rawValue)) {
        return undefined;
    }
    return new Date(rawValue * 1000).toISOString();
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
async function fetchDouyinUserPostPage(page, secUid, maxCursor) {
    const params = new URLSearchParams({
        sec_user_id: secUid,
        max_cursor: maxCursor,
        count: String(DEFAULT_DOUYIN_PAGE_SIZE),
        aid: DOUYIN_API_AID
    });
    return await browserFetchJson(page, "GET", `https://www.douyin.com/aweme/v1/web/aweme/post/?${params.toString()}`, { referer: "https://www.douyin.com/" });
}
function mapAwemeItemToVideoSummary(item) {
    const awemeId = typeof item.aweme_id === "string" ? item.aweme_id : undefined;
    const desc = typeof item.desc === "string" ? item.desc.trim() : "";
    const video = item.video && typeof item.video === "object" ? item.video : undefined;
    const playAddr = video?.play_addr && typeof video.play_addr === "object" ? video.play_addr : undefined;
    const playUrl = Array.isArray(playAddr?.url_list) ? String((playAddr?.url_list)[0] ?? "") : "";
    const author = item.author && typeof item.author === "object" ? item.author : undefined;
    const statistics = item.statistics && typeof item.statistics === "object" ? item.statistics : undefined;
    const mixInfo = item.mix_info && typeof item.mix_info === "object" ? item.mix_info : undefined;
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
export function createDouyinVideoCollector(overrides) {
    const deps = {
        ...createDefaultDependencies(),
        ...overrides
    };
    const page = deps.createPage();
    return {
        async resolveUser(input) {
            const normalizedSecUid = input.secUid?.trim();
            if (normalizedSecUid) {
                await page.goto(`https://www.douyin.com/user/${normalizedSecUid}`);
                const cookies = await page.getCookies({ domain: "douyin.com" });
                const userAgent = await page.evaluate("navigator.userAgent").catch(() => undefined);
                const creatorName = await page.evaluate(`
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
        async collectVideos(input) {
            const resolved = await this.resolveUser(input);
            const limit = normalizeDouyinBatchLimit(input.limit);
            let cursor = "0";
            const videos = [];
            const seen = new Set();
            for (let pageIndex = 0; pageIndex < 500; pageIndex += 1) {
                const payload = await fetchDouyinUserPostPage(page, resolved.secUid, cursor);
                const items = Array.isArray(payload.aweme_list) ? payload.aweme_list : [];
                for (const rawItem of items) {
                    if (!rawItem || typeof rawItem !== "object") {
                        continue;
                    }
                    const mapped = mapAwemeItemToVideoSummary(rawItem);
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
        async close() {
            await page.closeWindow();
        }
    };
}
