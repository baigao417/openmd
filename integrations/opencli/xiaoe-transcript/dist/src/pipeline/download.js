import { readdir } from "node:fs/promises";
import fs from "node:fs";
import path from "node:path";
import { runCommand } from "../utils/command.js";
import { atomicWriteUtf8 } from "../utils/atomic-write.js";
const DEFAULT_MEDIA_DOWNLOAD_TIMEOUT_MS = 90 * 60 * 1000;
const MEDIA_NORMALIZE_TIMEOUT_MS = 15 * 60 * 1000;
const MEDIA_PROBE_TIMEOUT_MS = 30000;
const HLS_PLAYLIST_PROBE_TIMEOUT_MS = 8000;
const FFMPEG_OUTPUT_STALL_TIMEOUT_MS = 60 * 1000;
const YTDLP_OUTPUT_STALL_TIMEOUT_MS = 60 * 1000;
const FFMPEG_HTTP_RW_TIMEOUT_US = "30000000";
const YTDLP_SOCKET_TIMEOUT_SECONDS = "30";
const HLS_SEGMENT_CONCURRENCY = 24;
const HLS_SEGMENT_TIMEOUT_MS = 30000;
const HLS_SEGMENT_MAX_DURATION_MS = 10 * 60 * 1000;
const HLS_FETCH_ATTEMPTS = 3;
const HLS_FAILED_SEGMENT_BUDGET = 3;
const HLS_REUSABLE_CDN_HOSTS = new Set([
    "encrypt-k-vod.xet.tech",
    "c-vod.hw-cdn.xiaoeknow.com",
    "btt-vod.xiaoeknow.com"
]);
const DOWNLOAD_BENCHMARK_TARGET_SECONDS = 15;
const DOWNLOAD_BENCHMARK_MAX_WALL_MS = 20_000;
const DOWNLOAD_BENCHMARK_CONCURRENCY = 4;
const HLS_BENCHMARK_PROBE_WINDOWS = 3;
const HLS_BENCHMARK_MAX_WALL_MS = 120000;
function resolveMediaDownloadTimeoutMs(timeoutMinutes) {
    if (!Number.isFinite(timeoutMinutes) || timeoutMinutes <= 0) {
        return DEFAULT_MEDIA_DOWNLOAD_TIMEOUT_MS;
    }
    return Math.floor(timeoutMinutes * 60 * 1000);
}
function isDirectHlsUrl(url) {
    return /\.m3u8(\?|$)/i.test(url);
}
function uniqueMediaUrls(media) {
    return [...new Set([media.mediaUrl, ...(media.candidateMediaUrls ?? [])].filter(Boolean))];
}
function withMediaUrl(media, mediaUrl) {
    if (media.mediaUrl === mediaUrl) {
        return media;
    }
    return {
        ...media,
        mediaUrl
    };
}
function buildFetchHeaders(media, cookieHeader) {
    const headers = {};
    if (media.referer) {
        headers.Referer = media.referer;
    }
    if (media.userAgent) {
        headers["User-Agent"] = media.userAgent;
    }
    if (cookieHeader) {
        headers.Cookie = cookieHeader;
    }
    return headers;
}
function hlsBenchmarkBudgetError() {
    return new Error(`HLS benchmark budget exceeded: maximumMs=${HLS_BENCHMARK_MAX_WALL_MS}`);
}
function remainingHlsBudgetMs(budget) {
    if (!budget) {
        return Number.POSITIVE_INFINITY;
    }
    if (budget.signal.aborted) {
        throw budget.signal.reason instanceof Error ? budget.signal.reason : hlsBenchmarkBudgetError();
    }
    const remaining = budget.deadlineAt - Date.now();
    if (remaining <= 0) {
        throw hlsBenchmarkBudgetError();
    }
    return remaining;
}
async function fetchText(url, headers, budget) {
    return await withHlsFetchRetries(async () => {
        return await fetchTextOnce(url, headers, HLS_SEGMENT_TIMEOUT_MS, budget);
    }, budget);
}
async function fetchTextOnce(url, headers, timeoutMs, budget) {
    remainingHlsBudgetMs(budget);
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), timeoutMs);
    try {
        const response = await fetch(url, {
            headers,
            signal: budget ? AbortSignal.any([controller.signal, budget.signal]) : controller.signal
        });
        if (!response.ok) {
            throw new Error(`playlist fetch failed: ${response.status}`);
        }
        return await response.text();
    }
    catch (error) {
        remainingHlsBudgetMs(budget);
        throw error;
    }
    finally {
        clearTimeout(timeout);
    }
}
function parseHlsAttributeList(input) {
    const attributes = {};
    const parts = [];
    let current = "";
    let quoted = false;
    for (const char of input) {
        if (char === "\"") {
            quoted = !quoted;
            current += char;
            continue;
        }
        if (char === "," && !quoted) {
            parts.push(current);
            current = "";
            continue;
        }
        current += char;
    }
    if (current) {
        parts.push(current);
    }
    for (const part of parts) {
        const separatorIndex = part.indexOf("=");
        if (separatorIndex < 1) {
            continue;
        }
        const key = part.slice(0, separatorIndex).trim().toUpperCase();
        let value = part.slice(separatorIndex + 1).trim();
        if (value.startsWith("\"") && value.endsWith("\"")) {
            value = value.slice(1, -1);
        }
        attributes[key] = value;
    }
    return attributes;
}
function resolvePlaylistUrl(baseUrl, candidateUrl) {
    try {
        return new URL(candidateUrl, baseUrl).toString();
    }
    catch {
        return undefined;
    }
}
function isAudioOnlyStreamVariant(attributes) {
    const codecs = attributes.CODECS?.toLowerCase();
    if (!codecs) {
        return false;
    }
    if (attributes.RESOLUTION) {
        return false;
    }
    const hasAudioCodec = /(?:mp4a|aac|ac-3|ec-3|opus|vorbis)/i.test(codecs);
    const hasVideoCodec = /(?:avc|hvc|hev|vp0?9|av01|dvh|theora)/i.test(codecs);
    return hasAudioCodec && !hasVideoCodec;
}
function scoreAudioRendition(attributes) {
    let score = 0;
    if (attributes.DEFAULT?.toUpperCase() === "YES") {
        score += 20;
    }
    if (attributes.AUTOSELECT?.toUpperCase() === "YES") {
        score += 5;
    }
    const language = attributes.LANGUAGE?.toLowerCase();
    if (language && /^(zh|zho|chi|cmn|yue)(-|$)/.test(language)) {
        score += 10;
    }
    const name = attributes.NAME?.toLowerCase() ?? "";
    if (/(default|main|audio|普通话|中文|国语|主音频)/i.test(name)) {
        score += 5;
    }
    return score;
}
function parseHlsAudioCandidateUrls(playlistUrl, content) {
    const lines = content
        .split(/\r?\n/)
        .map((line) => line.trim())
        .filter(Boolean);
    if (!lines.some((line) => line === "#EXTM3U")) {
        return [];
    }
    const candidates = [];
    let pendingStreamAttributes;
    for (const line of lines) {
        if (line.startsWith("#EXT-X-MEDIA:")) {
            const attributes = parseHlsAttributeList(line.slice("#EXT-X-MEDIA:".length));
            if (attributes.TYPE?.toUpperCase() === "AUDIO" && attributes.URI) {
                const url = resolvePlaylistUrl(playlistUrl, attributes.URI);
                if (url) {
                    candidates.push({
                        url,
                        score: 100 + scoreAudioRendition(attributes)
                    });
                }
            }
            continue;
        }
        if (line.startsWith("#EXT-X-STREAM-INF:")) {
            pendingStreamAttributes = parseHlsAttributeList(line.slice("#EXT-X-STREAM-INF:".length));
            continue;
        }
        if (!line.startsWith("#") && pendingStreamAttributes) {
            if (isAudioOnlyStreamVariant(pendingStreamAttributes)) {
                const url = resolvePlaylistUrl(playlistUrl, line);
                if (url) {
                    candidates.push({ url, score: 50 });
                }
            }
            pendingStreamAttributes = undefined;
        }
    }
    return [...new Map(candidates
            .sort((left, right) => right.score - left.score)
            .map((candidate) => [candidate.url, candidate.url])).values()];
}
async function expandPreferredMediaUrls(media, cookieHeader) {
    const headers = buildFetchHeaders(media, cookieHeader);
    const expandedGroups = await Promise.all(uniqueMediaUrls(media).map(async (mediaUrl, index) => {
        const startedAt = Date.now();
        const expanded = [];
        let probeSucceeded = false;
        if (isDirectHlsUrl(mediaUrl)) {
            try {
                const playlist = await fetchTextOnce(mediaUrl, headers, HLS_PLAYLIST_PROBE_TIMEOUT_MS);
                expanded.push(...parseHlsAudioCandidateUrls(mediaUrl, playlist));
                probeSucceeded = true;
            }
            catch {
                // Audio-only playlist probing is an optimization. Keep the original URL usable.
            }
        }
        expanded.push(mediaUrl);
        return {
            expanded,
            index,
            probeSucceeded,
            elapsedMs: Date.now() - startedAt
        };
    }));
    return [...new Set(expandedGroups
            .sort((left, right) => {
            if (left.probeSucceeded !== right.probeSucceeded) {
                return left.probeSucceeded ? -1 : 1;
            }
            if (left.probeSucceeded && right.probeSucceeded && left.elapsedMs !== right.elapsedMs) {
                return left.elapsedMs - right.elapsedMs;
            }
            return left.index - right.index;
        })
            .flatMap((group) => group.expanded))];
}
function parseHlsMediaPlaylist(playlistUrl, content) {
    const lines = content
        .split(/\r?\n/)
        .map((line) => line.trim())
        .filter(Boolean);
    if (!lines.some((line) => line === "#EXTM3U")) {
        throw new Error("not a valid HLS playlist");
    }
    if (lines.some((line) => line.startsWith("#EXT-X-STREAM-INF"))) {
        throw new Error("master playlists are not supported");
    }
    if (lines.some((line) => line.startsWith("#EXT-X-KEY"))) {
        throw new Error("encrypted playlists are not supported");
    }
    let pendingDurationSec;
    let mapUrl;
    const segments = [];
    for (const line of lines) {
        if (line.startsWith("#EXT-X-MAP:")) {
            const match = line.match(/URI="([^"]+)"/i);
            if (match?.[1]) {
                mapUrl = new URL(match[1], playlistUrl).toString();
            }
            continue;
        }
        if (line.startsWith("#EXTINF:")) {
            const parsedDuration = Number.parseFloat(line.slice("#EXTINF:".length).split(",")[0] ?? "");
            pendingDurationSec = Number.isFinite(parsedDuration) && parsedDuration > 0 ? parsedDuration : undefined;
            continue;
        }
        if (line.startsWith("#")) {
            continue;
        }
        segments.push({
            url: new URL(line, playlistUrl).toString(),
            durationSec: pendingDurationSec
        });
        pendingDurationSec = undefined;
    }
    if (segments.length === 0) {
        throw new Error("playlist does not contain media segments");
    }
    return { mapUrl, segments };
}
async function fetchArrayBuffer(url, headers, budget) {
    return await withHlsFetchRetries(async () => {
        return await fetchArrayBufferOnce(url, headers, budget);
    }, budget);
}
async function fetchArrayBufferOnce(url, headers, budget) {
    remainingHlsBudgetMs(budget);
    const startedAt = Date.now();
    const controller = new AbortController();
    let phase = "header";
    let receivedBytes = 0;
    let expectedBytes;
    let lastActivityAt = startedAt;
    let timeoutError;
    function abortForTimeout(reason) {
        if (controller.signal.aborted) {
            return;
        }
        const now = Date.now();
        timeoutError = new Error(`HLS segment ${reason} timeout: elapsedMs=${now - startedAt}, receivedBytes=${receivedBytes}, expectedBytes=${expectedBytes ?? "unknown"}, lastChunkGapMs=${now - lastActivityAt}`);
        controller.abort(timeoutError);
    }
    const timeout = setTimeout(() => abortForTimeout(phase), HLS_SEGMENT_TIMEOUT_MS);
    const absoluteTimeout = setTimeout(() => abortForTimeout("absolute"), HLS_SEGMENT_MAX_DURATION_MS);
    try {
        const response = await fetch(url, {
            headers,
            signal: budget ? AbortSignal.any([controller.signal, budget.signal]) : controller.signal
        });
        if (!response.ok) {
            throw new Error(`segment fetch failed: ${response.status}`);
        }
        // A slow segment may keep transferring beyond the header deadline.
        // Bound idle reads separately from the total request duration.
        phase = "body-idle";
        lastActivityAt = Date.now();
        const contentLengthHeader = response.headers.get("content-length");
        const contentLength = contentLengthHeader !== null && /^\d+$/.test(contentLengthHeader.trim())
            ? Number(contentLengthHeader)
            : Number.NaN;
        expectedBytes = Number.isSafeInteger(contentLength) && contentLength >= 0 ? contentLength : undefined;
        const validateContentLength = !response.headers.has("content-encoding");
        timeout.refresh();
        const reader = response.body?.getReader();
        if (!reader) {
            throw new Error("HLS segment response has no body");
        }
        const chunks = [];
        let totalBytes = 0;
        while (true) {
            const { done, value } = await reader.read();
            if (done) {
                break;
            }
            receivedBytes += value.byteLength;
            lastActivityAt = Date.now();
            timeout.refresh();
            chunks.push(value);
            totalBytes += value.byteLength;
        }
        if (totalBytes === 0) {
            throw new Error("HLS segment response body is empty");
        }
        if (validateContentLength && expectedBytes !== undefined && totalBytes !== expectedBytes) {
            throw new Error(`HLS segment body length mismatch: receivedBytes=${totalBytes}, expectedBytes=${expectedBytes}`);
        }
        const buffer = new Uint8Array(totalBytes);
        let offset = 0;
        for (const chunk of chunks) {
            buffer.set(chunk, offset);
            offset += chunk.byteLength;
        }
        return buffer.buffer;
    }
    catch (error) {
        remainingHlsBudgetMs(budget);
        throw timeoutError ?? error;
    }
    finally {
        clearTimeout(timeout);
        clearTimeout(absoluteTimeout);
    }
}
async function withHlsFetchRetries(operation, budget) {
    let lastError;
    for (let attempt = 1; attempt <= HLS_FETCH_ATTEMPTS; attempt += 1) {
        remainingHlsBudgetMs(budget);
        try {
            return await operation();
        }
        catch (error) {
            lastError = error;
            const remaining = remainingHlsBudgetMs(budget);
            if (attempt < HLS_FETCH_ATTEMPTS) {
                await new Promise((resolve) => setTimeout(resolve, Math.min(attempt * 250, remaining)));
            }
        }
    }
    throw lastError;
}
function segmentFileName(index) {
    return `${String(index).padStart(6, "0")}.ts`;
}
function countManifestSegments(manifest) {
    const success = manifest.segments.filter((segment) => segment.status === "success").length;
    const failed = manifest.segments.filter((segment) => segment.status === "failed").length;
    return {
        total: manifest.segments.length,
        success,
        failed,
        pending: manifest.segments.length - success - failed
    };
}
async function writeHlsManifest(filePath, manifest) {
    manifest.updatedAt = new Date().toISOString();
    await atomicWriteUtf8(filePath, `${JSON.stringify(manifest, null, 2)}\n`);
}
function stableHlsUrlIdentity(value) {
    try {
        const url = new URL(value);
        if (url.protocol !== "https:" || !HLS_REUSABLE_CDN_HOSTS.has(url.hostname) || url.port || url.username || url.password) {
            return undefined;
        }
        for (const key of ["sign", "t", "us"]) {
            url.searchParams.delete(key);
        }
        url.searchParams.sort();
        return `${url.pathname}${url.search}`;
    }
    catch {
        return undefined;
    }
}
function canReuseHlsManifest(current, donor, sameAttempt) {
    function sameUrl(left, right) {
        if (sameAttempt && left === right) {
            return true;
        }
        const identity = stableHlsUrlIdentity(left);
        return identity !== undefined && identity === stableHlsUrlIdentity(right);
    }
    if (!Array.isArray(donor.segments) || donor.segments.length !== current.segments.length || !sameUrl(current.playlistUrl, donor.playlistUrl)) {
        return false;
    }
    if (Boolean(current.mapUrl) !== Boolean(donor.mapUrl) || (current.mapUrl && donor.mapUrl && !sameUrl(current.mapUrl, donor.mapUrl))) {
        return false;
    }
    return current.segments.every((segment, index) => {
        const source = donor.segments[index];
        return source?.index === index && typeof source.url === "string" && sameUrl(segment.url, source.url);
    });
}
async function reuseSiblingHlsSegments(manifest, attemptDir) {
    const attemptsDir = path.dirname(attemptDir);
    const entries = await fs.promises.readdir(attemptsDir, { withFileTypes: true });
    for (const entry of entries) {
        if (!entry.isDirectory()) {
            continue;
        }
        const donorDir = path.join(attemptsDir, entry.name);
        let donor;
        try {
            donor = JSON.parse(await fs.promises.readFile(path.join(donorDir, "manifest.json"), "utf8"));
            if (!canReuseHlsManifest(manifest, donor, path.resolve(donorDir) === path.resolve(attemptDir))) {
                continue;
            }
        }
        catch {
            // Other strategies, incomplete manifests and unrelated resources are not donors.
            continue;
        }
        for (const segment of manifest.segments) {
            if (segment.status === "success") {
                continue;
            }
            const source = donor.segments[segment.index];
            if (source.status !== "success" || !Number.isSafeInteger(source.bytes) || source.bytes <= 0) {
                continue;
            }
            // Copied donor manifests may retain old absolute paths. Read only within
            // this attempt tree, using the indexed filename rather than manifest.path.
            const sourcePath = path.join(donorDir, "segments", segmentFileName(segment.index));
            try {
                const stats = await fs.promises.lstat(sourcePath);
                if (!stats.isFile() || stats.size !== source.bytes) {
                    continue;
                }
                if (path.resolve(sourcePath) !== path.resolve(segment.path)) {
                    await fs.promises.copyFile(sourcePath, segment.path);
                }
                segment.status = "success";
                segment.bytes = stats.size;
            }
            catch {
                // Missing or incomplete local files must be downloaded normally.
            }
        }
    }
}
async function createHlsManifest(parsed, playlistUrl, attemptDir) {
    const segmentsDir = path.join(attemptDir, "segments");
    await fs.promises.mkdir(segmentsDir, { recursive: true });
    const segments = [];
    for (const [index, segment] of parsed.segments.entries()) {
        const segmentPath = path.join(segmentsDir, segmentFileName(index));
        segments.push({ index, url: segment.url, path: segmentPath, status: "pending" });
    }
    const manifest = {
        playlistUrl,
        mapUrl: parsed.mapUrl,
        updatedAt: new Date().toISOString(),
        segments
    };
    await reuseSiblingHlsSegments(manifest, attemptDir);
    return manifest;
}
async function writeHlsOutput(outputPath, manifest, mapPath) {
    await fs.promises.writeFile(outputPath, "");
    if (mapPath) {
        await fs.promises.appendFile(outputPath, await fs.promises.readFile(mapPath));
    }
    for (const segment of manifest.segments) {
        if (segment.status !== "success") {
            throw new Error(`HLS segment ${segment.index} is not downloaded`);
        }
        await fs.promises.appendFile(outputPath, await fs.promises.readFile(segment.path));
    }
}
function uniqueSortedIndexes(indexes) {
    return [...new Set(indexes)].sort((left, right) => left - right);
}
function selectBenchmarkSegments(parsed) {
    const segmentCount = parsed.segments.length;
    if (segmentCount === 0) {
        return [];
    }
    const anchors = segmentCount <= HLS_BENCHMARK_PROBE_WINDOWS
        ? Array.from({ length: segmentCount }, (_, index) => index)
        : [0, Math.floor(segmentCount / 2), segmentCount - 1];
    const targetWindowSeconds = DOWNLOAD_BENCHMARK_TARGET_SECONDS / anchors.length;
    const selected = new Set();
    for (const anchor of anchors) {
        let totalDuration = 0;
        for (let index = anchor; index < segmentCount; index += 1) {
            selected.add(index);
            totalDuration += parsed.segments[index]?.durationSec ?? 0;
            if (totalDuration >= targetWindowSeconds) {
                break;
            }
            if (totalDuration === 0) {
                break;
            }
        }
    }
    return uniqueSortedIndexes([...selected]).map((index) => ({
        index,
        segment: parsed.segments[index]
    }));
}
async function writeHlsSampleOutput(outputPath, segments, mapPath) {
    await fs.promises.writeFile(outputPath, "");
    if (mapPath) {
        await fs.promises.appendFile(outputPath, await fs.promises.readFile(mapPath));
    }
    for (const segment of segments) {
        await fs.promises.appendFile(outputPath, await fs.promises.readFile(segment.path));
    }
}
async function benchmarkHlsSegmentsDirectly(media, attemptDir, label, cookieHeader) {
    const startedAt = Date.now();
    const budgetController = new AbortController();
    const budget = {
        deadlineAt: startedAt + HLS_BENCHMARK_MAX_WALL_MS,
        signal: budgetController.signal
    };
    const budgetTimeout = setTimeout(() => budgetController.abort(hlsBenchmarkBudgetError()), HLS_BENCHMARK_MAX_WALL_MS);
    let pendingSegments = [];
    const outputPath = path.join(attemptDir, "sample.ts");
    try {
        const headers = buildFetchHeaders(media, cookieHeader);
        const playlist = await fetchText(media.mediaUrl, headers, budget);
        const parsed = parseHlsMediaPlaylist(media.mediaUrl, playlist);
        const selected = selectBenchmarkSegments(parsed);
        const segmentsDir = path.join(attemptDir, "segments");
        await fs.promises.mkdir(segmentsDir, { recursive: true });
        let mapPath;
        if (parsed.mapUrl) {
            mapPath = path.join(attemptDir, "init.mp4");
            const initData = await fetchArrayBuffer(parsed.mapUrl, headers, budget);
            await fs.promises.writeFile(mapPath, Buffer.from(initData));
        }
        pendingSegments = selected.map(async ({ segment, index }) => {
            const segmentPath = path.join(segmentsDir, segmentFileName(index));
            let data;
            try {
                data = await fetchArrayBuffer(segment.url, headers, budget);
            }
            catch (error) {
                throw new Error(`benchmark HLS segment ${index} fetch failed: ${error instanceof Error ? error.message : String(error)}`);
            }
            await fs.promises.writeFile(segmentPath, Buffer.from(data));
            return { path: segmentPath };
        });
        const segmentFiles = await Promise.all(pendingSegments);
        await writeHlsSampleOutput(outputPath, segmentFiles, mapPath);
        const wallMs = Math.max(1, Date.now() - startedAt);
        const sampleDurationSec = await getReadableAudioDurationSeconds(outputPath, budget);
        const details = await benchmarkFileDetails(outputPath);
        remainingHlsBudgetMs(budget);
        return {
            status: "success",
            strategy: "direct-hls",
            label,
            samplePath: outputPath,
            sampleDurationSec,
            wallMs,
            ...details,
            effectiveSpeed: effectiveSpeed(sampleDurationSec, wallMs),
            normalizedAudio: false
        };
    }
    catch (error) {
        return {
            status: "failed",
            strategy: "direct-hls",
            label,
            normalizedAudio: false,
            error: budget.signal.aborted || Date.now() >= budget.deadlineAt
                ? hlsBenchmarkBudgetError().message
                : error instanceof Error ? error.message : String(error)
        };
    }
    finally {
        clearTimeout(budgetTimeout);
        budgetController.abort(new Error("HLS benchmark candidate finished"));
        await Promise.allSettled(pendingSegments);
    }
}
async function downloadHlsSegmentsDirectly(media, attemptDir, cookieHeader) {
    const headers = buildFetchHeaders(media, cookieHeader);
    const playlist = await fetchText(media.mediaUrl, headers);
    const parsed = parseHlsMediaPlaylist(media.mediaUrl, playlist);
    const outputPath = path.join(attemptDir, "source.ts");
    const manifestPath = path.join(attemptDir, "manifest.json");
    const manifest = await createHlsManifest(parsed, media.mediaUrl, attemptDir);
    await writeHlsManifest(manifestPath, manifest);
    let mapPath;
    if (parsed.mapUrl) {
        mapPath = path.join(attemptDir, "init.mp4");
        let hasMap = false;
        try {
            hasMap = (await fs.promises.stat(mapPath)).size > 0;
        }
        catch {
            hasMap = false;
        }
        if (!hasMap) {
            const initData = await fetchArrayBuffer(parsed.mapUrl, headers);
            await fs.promises.writeFile(mapPath, Buffer.from(initData));
        }
    }
    let stopAssigning = false;
    let failedSegments = 0;
    let firstError;
    async function worker(startIndex) {
        for (let index = startIndex; index < manifest.segments.length; index += HLS_SEGMENT_CONCURRENCY) {
            if (stopAssigning) {
                return;
            }
            const segment = manifest.segments[index];
            if (segment.status === "success") {
                continue;
            }
            try {
                const data = await fetchArrayBuffer(segment.url, headers);
                const buffer = Buffer.from(data);
                await fs.promises.writeFile(segment.path, buffer);
                segment.status = "success";
                segment.bytes = buffer.byteLength;
                delete segment.error;
                await writeHlsManifest(manifestPath, manifest);
            }
            catch (error) {
                segment.status = "failed";
                segment.error = error instanceof Error ? error.message : String(error);
                failedSegments += 1;
                if (failedSegments >= HLS_FAILED_SEGMENT_BUDGET) {
                    stopAssigning = true;
                }
                firstError ??= error;
                await writeHlsManifest(manifestPath, manifest);
            }
        }
    }
    const workers = Array.from({ length: Math.min(HLS_SEGMENT_CONCURRENCY, parsed.segments.length) }, (_, index) => worker(index));
    await Promise.allSettled(workers);
    if (firstError) {
        throw firstError;
    }
    await writeHlsOutput(outputPath, manifest, mapPath);
    await writeHlsManifest(manifestPath, manifest);
    return {
        path: outputPath,
        normalizedAudio: false
    };
}
function formatCookieHeader(cookies) {
    return cookies.map((cookie) => `${cookie.name}=${cookie.value}`).join("; ");
}
async function exportCookiesToNetscape(cookies, filePath) {
    const lines = [
        "# Netscape HTTP Cookie File",
        "# https://curl.se/docs/http-cookies.html"
    ];
    for (const cookie of cookies) {
        lines.push([
            cookie.domain,
            cookie.domain.startsWith(".") ? "TRUE" : "FALSE",
            cookie.path,
            cookie.secure ? "TRUE" : "FALSE",
            Math.floor(cookie.expirationDate ?? 0).toString(),
            cookie.name,
            cookie.value
        ].join("\t"));
    }
    await fs.promises.writeFile(filePath, `${lines.join("\n")}\n`, "utf8");
}
async function findFirstMatchingFile(directory, prefix) {
    const entries = await readdir(directory);
    return entries
        .filter((entry) => entry.startsWith(prefix))
        .sort()
        .map((entry) => path.join(directory, entry))[0];
}
function sanitizeAttemptLabel(label) {
    return label.replace(/[^a-z0-9._-]+/gi, "-").replace(/^-+|-+$/g, "").slice(0, 48) || "download";
}
async function createAttemptDir(tempDir, attemptNumber, label) {
    const attemptDir = path.join(tempDir, "download-attempts", `${String(attemptNumber).padStart(3, "0")}-${sanitizeAttemptLabel(label)}`);
    await fs.promises.mkdir(attemptDir, { recursive: true });
    return attemptDir;
}
async function createBenchmarkAttemptDir(tempDir, attemptNumber, label) {
    const attemptDir = path.join(tempDir, "download-benchmarks", `${String(attemptNumber).padStart(3, "0")}-${sanitizeAttemptLabel(label)}`);
    await fs.promises.mkdir(attemptDir, { recursive: true });
    return attemptDir;
}
async function downloadWithYtDlp(media, attemptDir, cookiesFile, timeoutMinutes) {
    const outputTemplate = path.join(attemptDir, "source.%(ext)s");
    const args = [
        "--no-progress",
        "--no-part",
        "--socket-timeout",
        YTDLP_SOCKET_TIMEOUT_SECONDS,
        "--retries",
        "3",
        "--fragment-retries",
        "3",
        "-o",
        outputTemplate
    ];
    if (cookiesFile) {
        args.push("--cookies", cookiesFile);
    }
    if (media.referer) {
        args.push("--referer", media.referer);
    }
    if (media.userAgent) {
        args.push("--add-header", `User-Agent:${media.userAgent}`);
    }
    args.push(media.mediaUrl);
    await runCommand("yt-dlp", args, {
        timeoutMs: resolveMediaDownloadTimeoutMs(timeoutMinutes),
        watchFile: {
            directory: attemptDir,
            prefix: "source.",
            ignoreIncludes: ["-Frag"],
            ignoreSuffixes: [".ytdl"],
            stallMs: YTDLP_OUTPUT_STALL_TIMEOUT_MS
        }
    });
    const filePath = await findFirstMatchingFile(attemptDir, "source.");
    if (!filePath) {
        throw new Error("yt-dlp finished without producing a media file");
    }
    return {
        path: filePath,
        normalizedAudio: false
    };
}
async function downloadWithFfmpeg(media, attemptDir, cookieHeader, timeoutMinutes) {
    const outputPath = path.join(attemptDir, "source.mp3");
    const headers = [];
    if (media.referer) {
        headers.push(`Referer: ${media.referer}`);
    }
    if (cookieHeader) {
        headers.push(`Cookie: ${cookieHeader}`);
    }
    const args = ["-y"];
    if (media.userAgent) {
        args.push("-user_agent", media.userAgent);
    }
    if (headers.length > 0) {
        args.push("-headers", `${headers.join("\r\n")}\r\n`);
    }
    if (/^https?:\/\//i.test(media.mediaUrl)) {
        args.push("-rw_timeout", FFMPEG_HTTP_RW_TIMEOUT_US);
    }
    args.push("-i", media.mediaUrl, "-vn", "-ac", "1", "-ar", "16000", "-codec:a", "libmp3lame", "-b:a", "64k", outputPath);
    await runCommand("ffmpeg", args, {
        timeoutMs: resolveMediaDownloadTimeoutMs(timeoutMinutes),
        watchFile: {
            path: outputPath,
            stallMs: FFMPEG_OUTPUT_STALL_TIMEOUT_MS
        }
    });
    return {
        path: outputPath,
        normalizedAudio: true
    };
}
async function benchmarkWithFfmpeg(media, attemptDir, label, cookieHeader) {
    const samplePath = path.join(attemptDir, "sample.mp3");
    const headers = [];
    if (media.referer) {
        headers.push(`Referer: ${media.referer}`);
    }
    if (cookieHeader) {
        headers.push(`Cookie: ${cookieHeader}`);
    }
    const args = ["-y"];
    if (media.userAgent) {
        args.push("-user_agent", media.userAgent);
    }
    if (headers.length > 0) {
        args.push("-headers", `${headers.join("\r\n")}\r\n`);
    }
    if (/^https?:\/\//i.test(media.mediaUrl)) {
        args.push("-rw_timeout", FFMPEG_HTTP_RW_TIMEOUT_US);
    }
    args.push("-i", media.mediaUrl, "-t", String(DOWNLOAD_BENCHMARK_TARGET_SECONDS), "-vn", "-ac", "1", "-ar", "16000", "-codec:a", "libmp3lame", "-b:a", "64k", samplePath);
    const startedAt = Date.now();
    try {
        await runCommand("ffmpeg", args, {
            timeoutMs: DOWNLOAD_BENCHMARK_MAX_WALL_MS,
            watchFile: {
                path: samplePath,
                stallMs: FFMPEG_OUTPUT_STALL_TIMEOUT_MS
            }
        });
        const wallMs = Math.max(1, Date.now() - startedAt);
        const sampleDurationSec = await getReadableAudioDurationSeconds(samplePath);
        const details = await benchmarkFileDetails(samplePath);
        return {
            status: "success",
            strategy: "ffmpeg",
            label,
            samplePath,
            sampleDurationSec,
            wallMs,
            ...details,
            effectiveSpeed: effectiveSpeed(sampleDurationSec, wallMs),
            normalizedAudio: true
        };
    }
    catch (error) {
        return {
            status: "failed",
            strategy: "ffmpeg",
            label,
            normalizedAudio: true,
            error: error instanceof Error ? error.message : String(error)
        };
    }
}
async function normalizeToMp3(inputPath, tempDir) {
    const outputPath = path.join(tempDir, "normalized.mp3");
    await runCommand("ffmpeg", ["-y", "-i", inputPath, "-vn", "-ac", "1", "-ar", "16000", "-codec:a", "libmp3lame", "-b:a", "64k", outputPath], { timeoutMs: MEDIA_NORMALIZE_TIMEOUT_MS });
    return outputPath;
}
async function getDurationSeconds(filePath, budget) {
    const result = await runCommand("ffprobe", ["-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", filePath], { timeoutMs: Math.min(MEDIA_PROBE_TIMEOUT_MS, remainingHlsBudgetMs(budget)) });
    const parsed = Number.parseFloat(result.stdout.trim());
    if (!Number.isFinite(parsed)) {
        throw new Error(`unable to read duration from ${filePath}`);
    }
    return parsed;
}
async function getReadableAudioDurationSeconds(filePath, budget) {
    await assertMediaHasAudio(filePath, budget);
    return await getDurationSeconds(filePath, budget);
}
function effectiveSpeed(sampleDurationSec, wallMs) {
    if (!Number.isFinite(sampleDurationSec) || sampleDurationSec <= 0 || !Number.isFinite(wallMs) || wallMs <= 0) {
        return 0;
    }
    return sampleDurationSec / (wallMs / 1000);
}
async function benchmarkFileDetails(filePath) {
    try {
        const stats = await fs.promises.stat(filePath);
        return { bytes: stats.size };
    }
    catch {
        return {};
    }
}
async function assertMediaHasAudio(filePath, budget) {
    const result = await runCommand("ffprobe", ["-v", "error", "-select_streams", "a:0", "-show_entries", "stream=codec_type", "-of", "json", filePath], { timeoutMs: Math.min(MEDIA_PROBE_TIMEOUT_MS, remainingHlsBudgetMs(budget)) });
    let parsed;
    try {
        parsed = JSON.parse(result.stdout);
    }
    catch {
        throw new Error(`downloaded media is not readable: ${filePath}`);
    }
    if (!parsed.streams?.some((stream) => stream.codec_type === "audio")) {
        throw new Error(`downloaded media has no readable audio stream: ${filePath}`);
    }
}
async function fileDetails(filePath) {
    if (!filePath) {
        return {};
    }
    try {
        const stats = await fs.promises.stat(filePath);
        return {
            bytes: stats.size,
            sizeMB: Math.round((stats.size / 1024 / 1024) * 100) / 100,
            lastWriteTime: stats.mtime.toISOString()
        };
    }
    catch {
        return {};
    }
}
async function hlsSegmentDetails(attemptDir) {
    try {
        const manifest = JSON.parse(await fs.promises.readFile(path.join(attemptDir, "manifest.json"), "utf8"));
        return countManifestSegments(manifest);
    }
    catch {
        return undefined;
    }
}
async function emitDownloadStatus(onStatus, status) {
    await onStatus?.(status);
}
function toBenchmarkSnapshot(result) {
    return {
        status: result.status,
        strategy: result.strategy,
        label: result.label,
        sampleDurationSec: result.sampleDurationSec,
        wallMs: result.wallMs,
        bytes: result.bytes,
        effectiveSpeed: result.effectiveSpeed,
        normalizedAudio: result.normalizedAudio,
        error: result.error
    };
}
async function benchmarkDownloadPlans(tempDir, plans, onStatus) {
    const benchmarkable = plans.filter((plan) => plan.benchmark);
    const results = [];
    let nextIndex = 0;
    await emitDownloadStatus(onStatus, {
        status: "running",
        benchmark: { status: "running", plans: [] }
    });
    async function worker() {
        while (nextIndex < benchmarkable.length) {
            const index = nextIndex;
            nextIndex += 1;
            const plan = benchmarkable[index];
            const attemptDir = await createBenchmarkAttemptDir(tempDir, index + 1, plan.label);
            const running = {
                status: "running",
                strategy: plan.strategy,
                label: plan.label,
                normalizedAudio: plan.normalizedAudio
            };
            await emitDownloadStatus(onStatus, {
                status: "running",
                benchmark: { status: "running", plans: [...results.map(toBenchmarkSnapshot), running] }
            });
            const result = await plan.benchmark(attemptDir);
            results.push(result);
            await emitDownloadStatus(onStatus, {
                status: "running",
                benchmark: { status: "running", plans: results.map(toBenchmarkSnapshot) }
            });
        }
    }
    await Promise.all(Array.from({ length: Math.min(DOWNLOAD_BENCHMARK_CONCURRENCY, benchmarkable.length) }, () => worker()));
    const ranked = results
        .filter((result) => result.status === "success" && (result.effectiveSpeed ?? 0) > 0)
        .map((benchmark) => ({
        benchmark,
        plan: benchmarkable.find((candidate) => candidate.label === benchmark.label && candidate.strategy === benchmark.strategy)
    }))
        .sort((left, right) => {
        const speedDelta = (right.benchmark.effectiveSpeed ?? 0) - (left.benchmark.effectiveSpeed ?? 0);
        if (speedDelta !== 0)
            return speedDelta;
        if (left.plan.strategy !== right.plan.strategy) {
            if (left.plan.strategy === "direct-hls")
                return -1;
            if (right.plan.strategy === "direct-hls")
                return 1;
        }
        return benchmarkable.indexOf(left.plan) - benchmarkable.indexOf(right.plan);
    });
    await emitDownloadStatus(onStatus, {
        status: "running",
        benchmark: {
            status: ranked.length > 0 ? "success" : "failed",
            plans: results.map(toBenchmarkSnapshot),
            selected: ranked[0] ? toBenchmarkSnapshot(ranked[0].benchmark) : undefined
        }
    });
    return ranked;
}
function buildDownloadPlans(candidates, cookieHeader, cookiesFile, timeoutMinutes) {
    const plans = [];
    for (const [index, candidate] of candidates.entries()) {
        const ffmpegLabel = `ffmpeg-${index + 1}`;
        plans.push({
            strategy: "ffmpeg",
            label: ffmpegLabel,
            media: candidate,
            normalizedAudio: true,
            benchmark: async (attemptDir) => await benchmarkWithFfmpeg(candidate, attemptDir, ffmpegLabel, cookieHeader),
            runFull: async (attemptDir) => await downloadWithFfmpeg(candidate, attemptDir, cookieHeader, timeoutMinutes)
        });
    }
    for (const [index, candidate] of candidates.entries()) {
        if (isDirectHlsUrl(candidate.mediaUrl)) {
            const directLabel = `direct-hls-${index + 1}`;
            plans.push({
                strategy: "direct-hls",
                label: directLabel,
                media: candidate,
                normalizedAudio: false,
                benchmark: async (attemptDir) => await benchmarkHlsSegmentsDirectly(candidate, attemptDir, directLabel, cookieHeader),
                runFull: async (attemptDir) => await downloadHlsSegmentsDirectly(candidate, attemptDir, cookieHeader)
            });
        }
    }
    for (const [index, candidate] of candidates.entries()) {
        plans.push({
            strategy: "yt-dlp",
            label: `yt-dlp-${index + 1}`,
            media: candidate,
            normalizedAudio: false,
            runFull: async (attemptDir) => await downloadWithYtDlp(candidate, attemptDir, cookiesFile, timeoutMinutes)
        });
    }
    return plans;
}
async function attemptSnapshot(input) {
    return {
        status: input.status,
        strategy: input.strategy.strategy,
        attempt: input.attempt,
        attemptLabel: input.strategy.label,
        attemptPath: input.attemptDir,
        path: input.artifactPath,
        ...(await fileDetails(input.artifactPath)),
        startedAt: input.startedAt,
        finishedAt: input.finishedAt,
        error: input.error,
        segments: await hlsSegmentDetails(input.attemptDir)
    };
}
async function chooseFirstUsableDownload(tempDir, plans, onStatus) {
    const errors = [];
    const attempts = [];
    for (const [index, plan] of plans.entries()) {
        let artifact;
        const attempt = index + 1;
        const attemptDir = await createAttemptDir(tempDir, attempt, plan.label);
        const startedAt = new Date().toISOString();
        const runningAttempt = await attemptSnapshot({
            status: "running",
            strategy: plan,
            attempt,
            attemptDir,
            startedAt
        });
        await emitDownloadStatus(onStatus, { status: "running", current: runningAttempt, attempts: [...attempts] });
        try {
            artifact = await plan.runFull(attemptDir);
            await assertMediaHasAudio(artifact.path);
            const successAttempt = await attemptSnapshot({
                status: "success",
                strategy: plan,
                attempt,
                attemptDir,
                startedAt,
                finishedAt: new Date().toISOString(),
                artifactPath: artifact.path
            });
            attempts.push(successAttempt);
            await emitDownloadStatus(onStatus, { status: "success", current: successAttempt, attempts: [...attempts] });
            return artifact;
        }
        catch (error) {
            const message = error instanceof Error ? error.message : String(error);
            errors.push(`${plan.label}: ${message}`);
            const failedAttempt = await attemptSnapshot({
                status: "failed",
                strategy: plan,
                attempt,
                attemptDir,
                startedAt,
                finishedAt: new Date().toISOString(),
                artifactPath: artifact?.path,
                error: message
            });
            attempts.push(failedAttempt);
            await emitDownloadStatus(onStatus, { status: "running", current: failedAttempt, attempts: [...attempts] });
        }
    }
    await emitDownloadStatus(onStatus, { status: "failed", attempts: [...attempts] });
    throw new Error(`no usable media download produced a readable audio stream: ${errors.join(" | ")}`);
}
async function runRankedFullDownloads(tempDir, ranked, onStatus) {
    const attempts = [];
    const benchmark = ranked[0]
        ? {
            status: "success",
            plans: ranked.map((item) => toBenchmarkSnapshot(item.benchmark)),
            selected: toBenchmarkSnapshot(ranked[0].benchmark)
        }
        : undefined;
    for (const [index, item] of ranked.entries()) {
        let artifact;
        const attempt = index + 1;
        const attemptDir = await createAttemptDir(tempDir, attempt, item.plan.label);
        const startedAt = new Date().toISOString();
        const runningAttempt = await attemptSnapshot({
            status: "running",
            strategy: item.plan,
            attempt,
            attemptDir,
            startedAt
        });
        await emitDownloadStatus(onStatus, { status: "running", current: runningAttempt, attempts: [...attempts], benchmark });
        try {
            artifact = await item.plan.runFull(attemptDir);
            await assertMediaHasAudio(artifact.path);
            const successAttempt = await attemptSnapshot({
                status: "success",
                strategy: item.plan,
                attempt,
                attemptDir,
                startedAt,
                finishedAt: new Date().toISOString(),
                artifactPath: artifact.path
            });
            attempts.push(successAttempt);
            await emitDownloadStatus(onStatus, { status: "success", current: successAttempt, attempts: [...attempts], benchmark });
            return artifact;
        }
        catch (error) {
            const failedAttempt = await attemptSnapshot({
                status: "failed",
                strategy: item.plan,
                attempt,
                attemptDir,
                startedAt,
                finishedAt: new Date().toISOString(),
                artifactPath: artifact?.path,
                error: error instanceof Error ? error.message : String(error)
            });
            attempts.push(failedAttempt);
            await emitDownloadStatus(onStatus, { status: "running", current: failedAttempt, attempts: [...attempts], benchmark });
        }
    }
    return undefined;
}
async function chooseFastestUsableDownload(tempDir, plans, onStatus) {
    const ranked = await benchmarkDownloadPlans(tempDir, plans, onStatus);
    const rankedArtifact = await runRankedFullDownloads(tempDir, ranked, onStatus);
    if (rankedArtifact) {
        return rankedArtifact;
    }
    return await chooseFirstUsableDownload(tempDir, plans, onStatus);
}
export async function downloadMediaArtifact(media, tempDir, timeoutMinutes, onStatus) {
    const cleanupPaths = [];
    let cookiesFile;
    const cookies = media.browserCookies ?? [];
    const cookieHeader = cookies.length > 0 ? formatCookieHeader(cookies) : undefined;
    if (cookies.length > 0) {
        cookiesFile = path.join(tempDir, "cookies.txt");
        await exportCookiesToNetscape(cookies, cookiesFile);
        cleanupPaths.push(cookiesFile);
    }
    const preferredMediaUrls = await expandPreferredMediaUrls(media, cookieHeader);
    const candidates = preferredMediaUrls.map((mediaUrl) => withMediaUrl(media, mediaUrl));
    const downloadPlans = buildDownloadPlans(candidates, cookieHeader, cookiesFile, timeoutMinutes);
    const originalArtifact = await chooseFastestUsableDownload(tempDir, downloadPlans, onStatus);
    const originalPath = originalArtifact.path;
    cleanupPaths.push(originalPath);
    const audioPath = originalArtifact.normalizedAudio
        ? originalPath
        : await normalizeToMp3(originalPath, tempDir);
    if (audioPath !== originalPath) {
        cleanupPaths.push(audioPath);
    }
    const mediaDurationSec = await getDurationSeconds(originalPath);
    return {
        originalPath,
        audioPath,
        mediaDurationSec,
        cleanupPaths
    };
}
