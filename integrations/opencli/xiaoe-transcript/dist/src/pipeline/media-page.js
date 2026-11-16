import { readFile } from "node:fs/promises";
import path from "node:path";
import { OpenCliPage } from "../opencli/client.js";
import { runCommand } from "../utils/command.js";
const MEDIA_URL_PATTERN = /https?:\/\/[^\s"'<>]+?\.(?:m3u8|mpd|mp4|m4a|mp3|aac|wav|flac|webm|ogg)(?:\?[^\s"'<>]*)?/ig;
const NETWORK_INTERCEPTOR_JS = `(function(){if(window.__opencli_net)return;window.__opencli_net=[];var M=200,B=50000,F=window.fetch;window.fetch=async function(){var r=await F.apply(this,arguments);try{var ct=r.headers.get('content-type')||'';if(ct.includes('json')||ct.includes('text')){var c=r.clone(),t=await c.text();if(window.__opencli_net.length<M){var b=null;if(t.length<=B)try{b=JSON.parse(t)}catch(e){b=t}window.__opencli_net.push({url:r.url||(arguments[0]&&arguments[0].url)||String(arguments[0]),method:(arguments[1]&&arguments[1].method)||'GET',status:r.status,size:t.length,ct:ct,body:b})}}}catch(e){}return r};var X=XMLHttpRequest.prototype,O=X.open,S=X.send;X.open=function(m,u){this._om=m;this._ou=u;return O.apply(this,arguments)};X.send=function(){var x=this;x.addEventListener('load',function(){try{var ct=x.getResponseHeader('content-type')||'';if((ct.includes('json')||ct.includes('text'))&&window.__opencli_net.length<M){var t=x.responseText,b=null;if(t&&t.length<=B)try{b=JSON.parse(t)}catch(e){b=t}window.__opencli_net.push({url:x._ou,method:x._om||'GET',status:x.status,size:t?t.length:0,ct:ct,body:b})}}catch(e){}});return S.apply(this,arguments)}})()`;
const DEFAULT_BROWSER_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36";
function unique(items) {
    return [...new Set(items)];
}
function looksLikeMediaUrl(value) {
    return /\.(m3u8|mpd|mp4|m4a|mp3|aac|wav|flac|webm|ogg)(\?|$)/i.test(value);
}
function normalizeMediaUrl(value) {
    return value
        .replace(/&amp;/gi, "&")
        .replace(/&#x2F;/gi, "/")
        .replace(/&#47;/gi, "/");
}
export function collectMediaUrlsDeep(value, maxDepth = 6) {
    const results = [];
    const seen = new Set();
    function visit(input, depth) {
        if (depth > maxDepth || input === null || input === undefined) {
            return;
        }
        if (typeof input === "string") {
            const matches = input.match(MEDIA_URL_PATTERN) ?? [];
            for (const match of matches) {
                const normalized = normalizeMediaUrl(match);
                if (!seen.has(normalized)) {
                    seen.add(normalized);
                    results.push(normalized);
                }
            }
            return;
        }
        if (Array.isArray(input)) {
            for (const item of input) {
                visit(item, depth + 1);
            }
            return;
        }
        if (typeof input === "object") {
            for (const item of Object.values(input)) {
                visit(item, depth + 1);
            }
        }
    }
    visit(value, 0);
    return results.sort((left, right) => left.localeCompare(right));
}
function scoreMediaUrl(url) {
    if (/\/confusion_index\//i.test(url)) {
        return -100;
    }
    let cdnBonus = 0;
    try {
        const host = new URL(url).host.toLowerCase();
        if (host === "c-vod.hw-cdn.xiaoeknow.com") {
            cdnBonus = 6;
        }
        else if (host === "btt-vod.xiaoeknow.com") {
            cdnBonus = 4;
        }
    }
    catch {
        cdnBonus = 0;
    }
    if (/\.m4a(\?|$)/i.test(url) || /\.mp3(\?|$)/i.test(url) || /\.wav(\?|$)/i.test(url)) {
        return 100 + cdnBonus;
    }
    const resolutionMatch = url.match(/[?&](?:resolution|res)=([0-9]{3,4})x([0-9]{3,4})(?:&|$)/i);
    const resolutionArea = resolutionMatch
        ? Number(resolutionMatch[1]) * Number(resolutionMatch[2])
        : undefined;
    const lowerResolutionBonus = resolutionArea
        ? Math.max(0, 10 - Math.min(resolutionArea / 1_000_000, 10))
        : 0;
    if (/\.m3u8(\?|$)/i.test(url) || /\.mpd(\?|$)/i.test(url)) {
        return 80 + lowerResolutionBonus + cdnBonus;
    }
    if (/\.mp4(\?|$)/i.test(url) || /\.webm(\?|$)/i.test(url)) {
        return 60 + lowerResolutionBonus + cdnBonus;
    }
    return 10;
}
export function collectXiaoeAliveLookbackUrls(value) {
    if (!value || typeof value !== "object") {
        return [];
    }
    const data = value.data;
    if (!Array.isArray(data)) {
        return [];
    }
    const lines = data;
    const sortedLines = [...lines].sort((left, right) => Number(Boolean(right.default)) - Number(Boolean(left.default)));
    const results = [];
    for (const line of sortedLines) {
        const sharpness = Array.isArray(line.line_sharpness) ? line.line_sharpness : [];
        const sortedSharpness = [...sharpness].sort((left, right) => Number(Boolean(right.default)) - Number(Boolean(left.default)));
        for (const item of sortedSharpness) {
            if (typeof item.url === "string" && looksLikeMediaUrl(item.url)) {
                results.push(normalizeMediaUrl(item.url));
            }
        }
    }
    return unique(results);
}
function parseXiaoeAliveInfo(url) {
    try {
        const parsed = new URL(url);
        const aliveId = (parsed.pathname.match(/\/alive\/([^/?#]+)/) ?? [])[1] ?? "";
        const appId = parsed.searchParams.get("app_id")
            || (parsed.hostname.match(/^(app[a-z0-9]+)\./i) ?? [])[1]
            || "";
        if (!appId || !aliveId) {
            return null;
        }
        return { appId, aliveId };
    }
    catch {
        return null;
    }
}
async function xiaoeAliveLookbackFallback(page, url) {
    const info = parseXiaoeAliveInfo(url);
    if (!info) {
        return [];
    }
    try {
        const response = await page.evaluate(`
      (async () => {
        const appId = ${JSON.stringify(info.appId)};
        const aliveId = ${JSON.stringify(info.aliveId)};
        const endpoint = "/_alive/v3/get_lookback_list?app_id=" + encodeURIComponent(appId) + "&alive_id=" + encodeURIComponent(aliveId) + "&protection=0";
        const response = await fetch(endpoint, { credentials: "include" });
        return await response.json();
      })()
    `);
        return collectXiaoeAliveLookbackUrls(response);
    }
    catch {
        return [];
    }
}
export function chooseBestMediaCandidate(candidates) {
    return [...candidates]
        .filter((candidate) => scoreMediaUrl(candidate) > 0)
        .sort((left, right) => scoreMediaUrl(right) - scoreMediaUrl(left))[0];
}
function dedupeCookies(cookies) {
    const byKey = new Map();
    for (const cookie of cookies) {
        byKey.set(`${cookie.domain}|${cookie.path}|${cookie.name}`, cookie);
    }
    return [...byKey.values()];
}
async function recordFallback(url, tempDir) {
    const recordDir = path.join(tempDir, "record-fallback");
    await runCommand("opencli", ["record", url, "--out", recordDir, "--timeout", "12000", "--poll", "2000"], {
        timeoutMs: 30000,
        allowFailure: true
    });
    const capturedPath = path.join(recordDir, "captured.json");
    try {
        const content = await readFile(capturedPath, "utf8");
        const parsed = JSON.parse(content);
        const requestUrls = (parsed.requests ?? [])
            .map((entry) => entry.url)
            .filter((entry) => typeof entry === "string" && looksLikeMediaUrl(entry));
        const nestedUrls = (parsed.requests ?? []).flatMap((entry) => {
            return [
                ...collectMediaUrlsDeep(entry.responseBody),
                ...collectMediaUrlsDeep(entry.body)
            ];
        });
        return unique([...requestUrls, ...nestedUrls]);
    }
    catch {
        return [];
    }
}
async function catCatchFallback(page) {
    const results = await page.evaluate(`
    (() => {
      const urls = new Set();
      const html = document.documentElement.innerHTML || "";
      const regex = ${MEDIA_URL_PATTERN.toString()};
      for (const match of html.match(regex) || []) {
        urls.add(match);
      }
      for (const url of urls) {
        try {
          window.postMessage({
            action: "catCatchAddMedia",
            url,
            href: location.href,
            referer: location.href
          });
        } catch {}
      }
      return Array.from(urls);
    })()
  `);
    return unique(results ?? []);
}
function parseJsonArrayFromCommandOutput(output) {
    const start = output.indexOf("[");
    const end = output.lastIndexOf("]");
    if (start < 0 || end < start) {
        return [];
    }
    try {
        const parsed = JSON.parse(output.slice(start, end + 1));
        return Array.isArray(parsed) ? parsed : [];
    }
    catch {
        return [];
    }
}
function titleFromPlayUrlRows(rows) {
    for (const row of rows) {
        if (row && typeof row === "object" && typeof row.title === "string") {
            const title = row.title.trim();
            if (title) {
                return title;
            }
        }
    }
    return undefined;
}
function isLikelyXiaoeUrl(url) {
    try {
        const hostname = new URL(url).hostname.toLowerCase();
        return hostname === "xiaoeknow.com"
            || hostname.endsWith(".xiaoeknow.com")
            || hostname === "xiaoecloud.com"
            || hostname.endsWith(".xiaoecloud.com")
            || hostname.endsWith(".xet.citv.cn")
            || hostname.endsWith(".xet.pomoho.com");
    }
    catch {
        return false;
    }
}
function bashQuote(value) {
    return `'${value.replace(/'/g, "'\\''")}'`;
}
async function openCliXiaoePlayUrlFallback(url) {
    const opencliCommand = process.platform === "win32"
        ? {
            command: "E:\\Git\\Git\\bin\\bash.exe",
            args: ["-lc", `opencli xiaoe play-url ${bashQuote(url)} -f json`]
        }
        : {
            command: "opencli",
            args: ["xiaoe", "play-url", url, "-f", "json"]
        };
    for (let attempt = 1; attempt <= 3; attempt += 1) {
        const result = await runCommand(opencliCommand.command, opencliCommand.args, { timeoutMs: 120000, allowFailure: true });
        if (result.exitCode === 0) {
            const rows = parseJsonArrayFromCommandOutput(result.stdout);
            const urls = unique(rows.flatMap((row) => collectMediaUrlsDeep(row)));
            if (urls.length > 0) {
                return {
                    title: titleFromPlayUrlRows(rows),
                    expectedDurationSec: rows.flatMap((row) => {
                        if (!row || typeof row !== "object")
                            return [];
                        const item = row;
                        const duration = Number(item.duration_sec);
                        return item.method === "api_direct" && String(item.resource_id || "").startsWith("v_") && Number.isFinite(duration) && duration > 0 ? [duration] : [];
                    })[0],
                    candidates: urls
                };
            }
        }
        if (attempt < 3) {
            await new Promise((resolve) => setTimeout(resolve, attempt * 500));
        }
    }
    return { candidates: [] };
}
function mediaFromPlayUrlFallback(url, fallback) {
    const candidates = unique(fallback.candidates.map((candidate) => normalizeMediaUrl(candidate)));
    const mediaUrl = chooseBestMediaCandidate(candidates);
    if (!mediaUrl) {
        return undefined;
    }
    return {
        pageTitle: fallback.title || "xiaoe-media",
        sourceUrl: url,
        mediaUrl,
        candidateMediaUrls: candidates,
        expectedDurationSec: fallback.expectedDurationSec,
        extractor: "network",
        referer: url,
        userAgent: DEFAULT_BROWSER_USER_AGENT,
        browserCookies: []
    };
}
export async function discoverMediaOnPage(url, tempDir) {
    if (isLikelyXiaoeUrl(url)) {
        const directMedia = mediaFromPlayUrlFallback(url, await openCliXiaoePlayUrlFallback(url));
        if (directMedia) {
            return directMedia;
        }
    }
    const page = new OpenCliPage(`media-transcript:media:${Date.now()}`);
    try {
        await page.goto(url);
        const domProbe = await page.evaluate(`
      (() => {
        const urls = new Set();
        const add = (value) => {
          if (typeof value === "string" && /\\.(m3u8|mpd|mp4|m4a|mp3|aac|wav|flac|webm|ogg)(\\?|$)/i.test(value)) {
            urls.add(value);
          }
        };

        for (const element of document.querySelectorAll("video, audio, source")) {
          add(element.currentSrc || element.src || element.getAttribute("src"));
        }

        const html = document.documentElement.innerHTML || "";
        const htmlMatches = html.match(${MEDIA_URL_PATTERN.toString()}) || [];
        for (const match of htmlMatches) {
          add(match);
        }

        const perf = performance.getEntriesByType("resource")
          .map((entry) => entry.name)
          .filter((entry) => /\\.(m3u8|mpd|mp4|m4a|mp3|aac|wav|flac|webm|ogg)(\\?|$)/i.test(entry));

        return {
          pageTitle: document.title || "",
          candidates: Array.from(urls),
          performanceCandidates: perf
        };
      })()
    `);
        let candidates = unique([
            ...(await xiaoeAliveLookbackFallback(page, url)),
            ...(domProbe.candidates ?? []),
            ...(domProbe.performanceCandidates ?? [])
        ].map((candidate) => normalizeMediaUrl(candidate)));
        if (candidates.length === 0) {
            await page.evaluate(NETWORK_INTERCEPTOR_JS);
            await page.evaluate(`
        (() => {
          const media = document.querySelector("video, audio");
          if (media && typeof media.play === "function") {
            media.play().catch(() => undefined);
          }
          for (const element of document.querySelectorAll("button, [role='button']")) {
            const text = (element.innerText || "").trim();
            if (/播放|继续|开始|试看|play|start/i.test(text)) {
              element.click();
              break;
            }
          }
          return true;
        })()
      `).catch(() => undefined);
            await page.evaluate("new Promise(resolve => setTimeout(resolve, 4000))");
            const networkEntries = await page.evaluate("window.__opencli_net || []");
            const networkCandidates = (networkEntries ?? []).flatMap((entry) => {
                return [
                    ...(entry.url && looksLikeMediaUrl(entry.url) ? [entry.url] : []),
                    ...collectMediaUrlsDeep(entry.body)
                ];
            });
            candidates = unique([...candidates, ...networkCandidates].map((candidate) => normalizeMediaUrl(candidate)));
        }
        if (candidates.length === 0) {
            candidates = unique([...candidates, ...(await recordFallback(url, tempDir))].map((candidate) => normalizeMediaUrl(candidate)));
        }
        if (candidates.length === 0) {
            candidates = unique([...candidates, ...(await catCatchFallback(page))].map((candidate) => normalizeMediaUrl(candidate)));
        }
        if (candidates.length === 0) {
            const fallback = await openCliXiaoePlayUrlFallback(url);
            candidates = unique([...candidates, ...fallback.candidates].map((candidate) => normalizeMediaUrl(candidate)));
            if (!domProbe.pageTitle && fallback.title) {
                domProbe.pageTitle = fallback.title;
            }
        }
        const mediaUrl = chooseBestMediaCandidate(candidates);
        if (!mediaUrl) {
            throw new Error("no downloadable media URL found on the page");
        }
        const sourceCookies = await page.getCookies({ url });
        const mediaCookies = await page.getCookies({ url: mediaUrl }).catch(() => []);
        const userAgent = await page.evaluate("navigator.userAgent");
        return {
            pageTitle: domProbe.pageTitle || "media-transcript",
            sourceUrl: url,
            mediaUrl,
            candidateMediaUrls: candidates,
            extractor: candidates.includes(mediaUrl) && (domProbe.candidates ?? []).includes(mediaUrl) ? "dom" : "network",
            referer: url,
            userAgent,
            browserCookies: dedupeCookies([...sourceCookies, ...mediaCookies])
        };
    }
    finally {
        await page.closeWindow();
    }
}
