import { cli, Strategy } from '@jackwener/opencli/registry';
import {
  ArgumentError,
  AuthRequiredError,
  CommandExecutionError,
  EmptyResultError,
} from '@jackwener/opencli/errors';
import type { IPage } from '@jackwener/opencli/types';

const DEFAULT_PAGE_DELAY_MS = 800;
const DEFAULT_MAX_PAGES = 2000;
const DEFAULT_REQUEST_TIMEOUT_MS = 30_000;
const DEFAULT_COMMAND_TIMEOUT_SECONDS = 1800;
const DOUYIN_STABLE_BRIDGE_URL = 'https://www.douyin.com/robots.txt';
const PAGE_SIZE = 20;

const SEC_UID_RE = /^[A-Za-z0-9_.-]{6,}$/;
const AWEME_ID_RE = /^\d{6,}$/;
const AUTH_TERMS = /login|sign.?in|cookie|session|authorization|forbidden|captcha|verify|risk|登录|登陆|权限|验证|验证码|风控/i;

type JsonObject = Record<string, unknown>;

export type CreatorTarget = {
  secUid: string;
  creatorUrl: string;
};

export type CreatorVideoRow = {
  index: number;
  aweme_id: string;
  title: string;
  author: string;
  published_at: string;
  duration: number;
  digg_count: number;
  aweme_type: number;
  image_count: number;
  media_kind: 'video' | 'image_text';
  play_url: string;
  transcript_candidates: Array<{ source: string; text: string }>;
  page_count?: number;
  empty_page_count?: number;
  duplicate_page_count?: number;
  pagination_retry_count?: number;
  source_exhausted?: boolean;
};

export class CreatorVideosError extends Error {
  readonly code: string;

  constructor(code: string, message: string) {
    super(message);
    this.name = 'CreatorVideosError';
    this.code = code;
  }
}

export function parseCreatorTarget(input: string): CreatorTarget | null {
  const value = String(input ?? '').trim();
  if (SEC_UID_RE.test(value) && !value.includes('://') && !value.includes('/')) {
    return { secUid: value, creatorUrl: `https://www.douyin.com/user/${value}` };
  }

  const parsed = safeUrl(value);
  if (!parsed || !isDouyinHost(parsed.hostname)) return null;

  const queryCandidate = parsed.searchParams.get('sec_uid') ?? parsed.searchParams.get('secUid');
  if (queryCandidate && SEC_UID_RE.test(queryCandidate)) {
    return {
      secUid: queryCandidate,
      creatorUrl: `https://www.douyin.com/user/${queryCandidate}`,
    };
  }

  const parts = parsed.pathname.split('/').filter(Boolean);
  const userIndex = parts.findIndex((part) => part === 'user');
  const pathCandidate = userIndex >= 0 ? parts[userIndex + 1] : undefined;
  if (pathCandidate && SEC_UID_RE.test(pathCandidate)) {
    return {
      secUid: pathCandidate,
      creatorUrl: `https://www.douyin.com/user/${pathCandidate}`,
    };
  }
  return null;
}

export async function resolveCreatorTarget(
  page: IPage,
  input: string,
  resolveShortUrl: (url: string) => Promise<string> = resolveShortRedirect,
): Promise<CreatorTarget> {
  const direct = parseCreatorTarget(input);
  if (direct) return direct;

  const value = String(input ?? '').trim();
  const parsed = safeUrl(value);
  if (!parsed || parsed.hostname.toLowerCase() !== 'v.douyin.com') {
    throw new CreatorVideosError(
      'douyin_creator_input_invalid',
      'Expected a Douyin creator share URL, creator profile URL, or sec_uid.',
    );
  }

  try {
    const finalUrl = await resolveShortUrl(value);
    const resolved = parseCreatorTarget(finalUrl);
    if (resolved) return resolved;
  } catch {
    // Fall back to the browser session below; redirect resolution is best-effort.
  }

  await page.goto(value);
  await page.wait(3);
  const currentUrl =
    (await page.getCurrentUrl?.()) ??
    String(await page.evaluate('window.location.href'));
  const redirected = parseCreatorTarget(currentUrl);
  if (redirected) return redirected;

  const profileHref = String(
    await page.evaluate(
      `(() => document.querySelector('a[href*="/user/"]')?.href || '')()`,
    ),
  );
  const anchored = parseCreatorTarget(profileHref);
  if (anchored) return anchored;

  throw new CreatorVideosError(
    'douyin_creator_short_link_unresolved',
    'The Douyin share URL did not resolve to a creator profile in the current browser session.',
  );
}

export function normalizePostPage(payload: unknown): {
  items: JsonObject[];
  hasMore: boolean;
  nextCursor: string;
} {
  if (!isObject(payload)) {
    throw new CreatorVideosError('douyin_post_api_invalid_response', 'Douyin post API returned a non-object response.');
  }

  const statusCode = numberValue(payload.status_code);
  const statusMessage = stringValue(payload.status_msg ?? payload.message);
  if (statusCode !== null && statusCode !== 0) {
    if (statusCode === 401 || statusCode === 403 || AUTH_TERMS.test(statusMessage)) {
      throw new CreatorVideosError('douyin_auth_challenge', 'Douyin post API requires a valid logged-in browser session.');
    }
    throw new CreatorVideosError(
      'douyin_post_api_error',
      `Douyin post API returned status_code=${statusCode}.`,
    );
  }

  const rawItems = Array.isArray(payload.aweme_list)
    ? payload.aweme_list
    : Array.isArray(payload.awemeList)
      ? payload.awemeList
      : [];
  const items = rawItems.filter(isObject);
  const hasMore = booleanValue(payload.has_more ?? payload.hasMore);
  const nextCursor = stringValue(payload.max_cursor ?? payload.maxCursor ?? payload.cursor);
  return { items, hasMore, nextCursor };
}

export async function collectCreatorVideos(
  fetchPage: (cursor: string, count: number) => Promise<unknown>,
  options: {
    pageDelayMs?: number;
    maxPages?: number;
    sleep?: (milliseconds: number) => Promise<void>;
  } = {},
): Promise<CreatorVideoRow[]> {
  const pageDelayMs = boundedInteger(options.pageDelayMs, DEFAULT_PAGE_DELAY_MS, 0, 60_000);
  const maxPages = boundedInteger(options.maxPages, DEFAULT_MAX_PAGES, 1, 100_000);
  const sleep = options.sleep ?? ((milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds)));
  const rows: CreatorVideoRow[] = [];
  const seenIds = new Set<string>();
  let cursor = '0';

  for (let pageNumber = 1; pageNumber <= maxPages; pageNumber += 1) {
    const page = normalizePostPage(await fetchPage(cursor, PAGE_SIZE));
    let added = 0;
    for (const item of page.items) {
      const projected = projectCreatorVideo(item, rows.length + 1);
      if (!projected || seenIds.has(projected.aweme_id)) continue;
      seenIds.add(projected.aweme_id);
      rows.push(projected);
      added += 1;
    }

    if (!page.hasMore) return rows;
    if (page.items.length === 0) {
      throw new CreatorVideosError(
        'douyin_pagination_empty_page',
        `Douyin pagination returned has_more=true with an empty page at page ${pageNumber}.`,
      );
    }
    if (added === 0) {
      throw new CreatorVideosError(
        'douyin_pagination_duplicate_page',
        `Douyin pagination returned a duplicate-only page at page ${pageNumber}.`,
      );
    }
    if (!page.nextCursor || page.nextCursor === cursor) {
      throw new CreatorVideosError(
        'douyin_pagination_cursor_stall',
        `Douyin pagination cursor stalled at page ${pageNumber}.`,
      );
    }

    cursor = page.nextCursor;
    if (pageDelayMs > 0) await sleep(pageDelayMs);
  }

  throw new CreatorVideosError(
    'douyin_pagination_emergency_stop',
    `Douyin pagination exceeded the emergency stop of ${maxPages} pages before exhaustion.`,
  );
}

export function projectCreatorVideo(item: JsonObject, index: number): CreatorVideoRow | null {
  const awemeId = stringValue(item.aweme_id ?? item.awemeId ?? item.id);
  if (!AWEME_ID_RE.test(awemeId)) return null;

  const video = isObject(item.video) ? item.video : {};
  const statistics = isObject(item.statistics) ? item.statistics : {};
  const author = isObject(item.author) ? item.author : {};
  const imagePostInfo = isObject(item.image_post_info) ? item.image_post_info : {};
  const images = Array.isArray(item.images)
    ? item.images
    : Array.isArray(imagePostInfo.images)
      ? imagePostInfo.images
      : [];
  const awemeType = Math.max(0, Math.round(numberValue(item.aweme_type ?? item.awemeType) ?? 0));
  const imageCount = images.length;
  const playAddress = isObject(video.play_addr) ? video.play_addr : isObject(video.playAddr) ? video.playAddr : {};
  const playUrls = Array.isArray(playAddress.url_list)
    ? playAddress.url_list
    : Array.isArray(playAddress.urlList)
      ? playAddress.urlList
      : [];

  return {
    index,
    aweme_id: awemeId,
    title: stringValue(item.desc ?? item.title),
    author: stringValue(author.nickname ?? author.unique_id ?? item.author_name),
    published_at: normalizePublishedAt(item.create_time ?? item.createTime),
    duration: Math.max(0, Math.round((numberValue(video.duration) ?? 0) / 1000)),
    digg_count: Math.max(0, Math.round(numberValue(statistics.digg_count ?? statistics.diggCount) ?? 0)),
    aweme_type: awemeType,
    image_count: imageCount,
    media_kind: awemeType === 68 || imageCount > 0 ? 'image_text' : 'video',
    play_url: stringValue(playUrls[0]),
    transcript_candidates: extractTranscriptCandidates(item),
  };
}

export function extractTranscriptCandidates(item: JsonObject): Array<{ source: string; text: string }> {
  const candidates: Array<{ source: string; text: string }> = [];
  const seen = new Set<string>();
  const push = (source: string, value: unknown) => {
    const text = stringValue(value).replace(/\r\n?/g, '\n').trim();
    if (!text || seen.has(text)) return;
    seen.add(text);
    candidates.push({ source, text });
  };

  collectTextValues(item.video_text, 'video_text', push);
  collectTextValues(item.videoText, 'video_text', push);
  collectTextValues(item.subtitle, 'subtitle', push);
  collectTextValues(item.subtitles, 'subtitle', push);
  collectTextValues(item.caption_info, 'caption_info', push);
  collectTextValues(item.captionInfo, 'caption_info', push);
  return candidates;
}

function collectTextValues(
  value: unknown,
  source: string,
  push: (source: string, value: unknown) => void,
): void {
  if (typeof value === 'string') {
    push(source, value);
    return;
  }
  if (Array.isArray(value)) {
    for (const entry of value) collectTextValues(entry, source, push);
    return;
  }
  if (!isObject(value)) return;
  for (const key of ['text', 'content', 'caption', 'sentence', 'utterance', 'value']) {
    if (key in value) collectTextValues(value[key], source, push);
  }
  for (const key of ['items', 'segments', 'sentences', 'captions', 'subtitles', 'list']) {
    if (key in value) collectTextValues(value[key], source, push);
  }
}

async function collectCreatorVideosInBrowser(
  page: IPage,
  target: CreatorTarget,
  options: { pageDelayMs: number; maxPages: number; requestTimeoutMs: number },
): Promise<{ rows: CreatorVideoRow[]; pageCount: number }> {
  const script = `
    (async () => {
      const secUid = ${JSON.stringify(target.secUid)};
      const pageDelayMs = ${JSON.stringify(options.pageDelayMs)};
      const maxPages = ${JSON.stringify(options.maxPages)};
      const requestTimeoutMs = ${JSON.stringify(options.requestTimeoutMs)};
      const pageSize = ${JSON.stringify(PAGE_SIZE)};
      const rows = [];
      const seenIds = new Set();
      let cursor = '0';
      let emptyPageCount = 0;
      let duplicatePageCount = 0;
      let paginationRetryCount = 0;
      let emptyRetryCursor = '';
      let emptyRetryCount = 0;
      let duplicateRetryCursor = '';
      let duplicateRetryCount = 0;

      const stringValue = (value) => value == null ? '' : String(value).trim();
      const numberValue = (value) => {
        const numeric = typeof value === 'number' ? value : Number(value);
        return Number.isFinite(numeric) ? numeric : null;
      };
      const isObject = (value) => Boolean(value) && typeof value === 'object' && !Array.isArray(value);
      const normalizePublishedAt = (value) => {
        const numeric = numberValue(value);
        if (numeric == null || numeric <= 0) return stringValue(value);
        const milliseconds = numeric > 10000000000 ? numeric : numeric * 1000;
        return new Date(milliseconds).toISOString();
      };
      const collectTextValues = (value, source, push) => {
        if (typeof value === 'string') {
          push(source, value);
          return;
        }
        if (Array.isArray(value)) {
          for (const entry of value) collectTextValues(entry, source, push);
          return;
        }
        if (!isObject(value)) return;
        for (const key of ['text', 'content', 'caption', 'sentence', 'utterance', 'value']) {
          if (key in value) collectTextValues(value[key], source, push);
        }
        for (const key of ['items', 'segments', 'sentences', 'captions', 'subtitles', 'list']) {
          if (key in value) collectTextValues(value[key], source, push);
        }
      };
      const transcriptCandidates = (item) => {
        const candidates = [];
        const seen = new Set();
        const push = (source, value) => {
          const text = stringValue(value).replace(/\\r\\n?/g, '\\n').trim();
          if (!text || seen.has(text)) return;
          seen.add(text);
          candidates.push({ source, text });
        };
        collectTextValues(item.video_text, 'video_text', push);
        collectTextValues(item.videoText, 'video_text', push);
        collectTextValues(item.subtitle, 'subtitle', push);
        collectTextValues(item.subtitles, 'subtitle', push);
        collectTextValues(item.caption_info, 'caption_info', push);
        collectTextValues(item.captionInfo, 'caption_info', push);
        return candidates;
      };
      const project = (item) => {
        const awemeId = stringValue(item.aweme_id ?? item.awemeId ?? item.id);
        if (!/^\\d{6,}$/.test(awemeId)) return null;
        const video = isObject(item.video) ? item.video : {};
        const statistics = isObject(item.statistics) ? item.statistics : {};
        const author = isObject(item.author) ? item.author : {};
        const imagePostInfo = isObject(item.image_post_info) ? item.image_post_info : {};
        const images = Array.isArray(item.images)
          ? item.images
          : Array.isArray(imagePostInfo.images)
            ? imagePostInfo.images
            : [];
        const awemeType = Math.max(0, Math.round(numberValue(item.aweme_type ?? item.awemeType) ?? 0));
        const imageCount = images.length;
        const playAddress = isObject(video.play_addr) ? video.play_addr : isObject(video.playAddr) ? video.playAddr : {};
        const playUrls = Array.isArray(playAddress.url_list) ? playAddress.url_list : Array.isArray(playAddress.urlList) ? playAddress.urlList : [];
        return {
          index: rows.length + 1,
          aweme_id: awemeId,
          title: stringValue(item.desc ?? item.title),
          author: stringValue(author.nickname ?? author.unique_id ?? item.author_name),
          published_at: normalizePublishedAt(item.create_time ?? item.createTime),
          duration: Math.max(0, Math.round((numberValue(video.duration) ?? 0) / 1000)),
          digg_count: Math.max(0, Math.round(numberValue(statistics.digg_count ?? statistics.diggCount) ?? 0)),
          aweme_type: awemeType,
          image_count: imageCount,
          media_kind: awemeType === 68 || imageCount > 0 ? 'image_text' : 'video',
          play_url: stringValue(playUrls[0]),
          transcript_candidates: transcriptCandidates(item)
        };
      };
      const failure = (errorCode, errorMessage, pageCount) => ({
        ok: false,
        error_code: errorCode,
        error_message: errorMessage,
        page_count: pageCount
      });
      const success = (pageCount) => ({
        ok: true,
        page_count: pageCount,
        rows: rows.map((row) => ({
          ...row,
          page_count: pageCount,
          empty_page_count: emptyPageCount,
          duplicate_page_count: duplicatePageCount,
          pagination_retry_count: paginationRetryCount,
          source_exhausted: true
        }))
      });

      for (let pageNumber = 1; pageNumber <= maxPages; pageNumber += 1) {
        const params = new URLSearchParams({
          sec_user_id: secUid,
          max_cursor: cursor,
          count: String(pageSize),
          aid: '6383'
        });
        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), requestTimeoutMs);
        let payload;
        try {
          const response = await fetch('https://www.douyin.com/aweme/v1/web/aweme/post/?' + params.toString(), {
            method: 'GET',
            credentials: 'include',
            signal: controller.signal,
            headers: { Accept: 'application/json', referer: 'https://www.douyin.com/' }
          });
          const text = await response.text();
          try {
            payload = JSON.parse(text);
          } catch {
            const code = response.ok ? -2 : response.status;
            return failure(
              code === 401 || code === 403 ? 'douyin_auth_challenge' : 'douyin_post_api_invalid_json',
              code === 401 || code === 403 ? 'Douyin post API requires a valid logged-in browser session.' : 'Douyin post API returned non-JSON content.',
              pageNumber
            );
          }
        } catch (error) {
          return failure('douyin_post_api_fetch_failed', String(error && error.message || error), pageNumber);
        } finally {
          clearTimeout(timer);
        }

        if (!isObject(payload)) return failure('douyin_post_api_invalid_response', 'Douyin post API returned a non-object response.', pageNumber);
        const statusCode = numberValue(payload.status_code);
        const statusMessage = stringValue(payload.status_msg ?? payload.message);
        if (statusCode != null && statusCode !== 0) {
          const authLike = statusCode === 401 || statusCode === 403 || /login|sign.?in|cookie|session|authorization|forbidden|captcha|verify|risk|登录|登陆|权限|验证|验证码|风控/i.test(statusMessage);
          return failure(
            authLike ? 'douyin_auth_challenge' : 'douyin_post_api_error',
            authLike ? 'Douyin post API requires a valid logged-in browser session.' : 'Douyin post API returned a non-zero status.',
            pageNumber
          );
        }

        const rawItems = Array.isArray(payload.aweme_list) ? payload.aweme_list : Array.isArray(payload.awemeList) ? payload.awemeList : [];
        const items = rawItems.filter(isObject);
        let added = 0;
        for (const item of items) {
          const projected = project(item);
          if (!projected || seenIds.has(projected.aweme_id)) continue;
          seenIds.add(projected.aweme_id);
          rows.push(projected);
          added += 1;
        }

        const hasMoreValue = payload.has_more ?? payload.hasMore;
        const hasMore = typeof hasMoreValue === 'boolean'
          ? hasMoreValue
          : typeof hasMoreValue === 'number'
            ? hasMoreValue !== 0
            : ['1', 'true', 'yes'].includes(stringValue(hasMoreValue).toLowerCase());
        const nextCursor = stringValue(payload.max_cursor ?? payload.maxCursor ?? payload.cursor);
        if (!hasMore) return success(pageNumber);
        if (!nextCursor || nextCursor === cursor) return failure('douyin_pagination_cursor_stall', 'Douyin pagination cursor stalled.', pageNumber);
        if (items.length === 0) {
          if (emptyRetryCursor !== cursor) {
            emptyRetryCursor = cursor;
            emptyRetryCount = 0;
          }
          if (emptyRetryCount < 2) {
            emptyRetryCount += 1;
            paginationRetryCount += 1;
            pageNumber -= 1;
            await new Promise((resolve) => setTimeout(resolve, Math.max(pageDelayMs, 1500)));
            continue;
          }
          emptyPageCount += 1;
          emptyRetryCount = 0;
          cursor = nextCursor;
          if (pageDelayMs > 0) await new Promise((resolve) => setTimeout(resolve, pageDelayMs));
          continue;
        }
        if (added === 0) {
          if (duplicateRetryCursor !== cursor) {
            duplicateRetryCursor = cursor;
            duplicateRetryCount = 0;
          }
          if (duplicateRetryCount < 1) {
            duplicateRetryCount += 1;
            paginationRetryCount += 1;
            pageNumber -= 1;
            await new Promise((resolve) => setTimeout(resolve, Math.max(pageDelayMs, 1500)));
            continue;
          }
          duplicatePageCount += 1;
          duplicateRetryCount = 0;
          cursor = nextCursor;
          if (pageDelayMs > 0) await new Promise((resolve) => setTimeout(resolve, pageDelayMs));
          continue;
        }
        emptyRetryCount = 0;
        duplicateRetryCount = 0;
        cursor = nextCursor;
        if (pageDelayMs > 0) await new Promise((resolve) => setTimeout(resolve, pageDelayMs));
      }

      return failure('douyin_pagination_emergency_stop', 'Douyin pagination exceeded the emergency stop before exhaustion.', maxPages);
    })()
  `;
  const result = await page.evaluate(script);
  if (!isObject(result)) {
    throw new CreatorVideosError('douyin_browser_pagination_invalid_response', 'Browser pagination returned a non-object response.');
  }
  if (result.ok !== true) {
    throw new CreatorVideosError(
      stringValue(result.error_code) || 'douyin_browser_pagination_failed',
      stringValue(result.error_message) || 'Douyin browser pagination failed.',
    );
  }
  const rows = Array.isArray(result.rows)
    ? result.rows.filter(isObject).map((row) => row as CreatorVideoRow)
    : [];
  return {
    rows,
    pageCount: Math.max(0, Math.round(numberValue(result.page_count) ?? 0)),
  };
}

async function resolveShortRedirect(url: string): Promise<string> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 30_000);
  try {
    const response = await fetch(url, {
      method: 'HEAD',
      redirect: 'follow',
      signal: controller.signal,
      headers: { 'user-agent': 'Mozilla/5.0 Openmd-Douyin-Link-Resolver/1.0' },
    });
    if (!response.url) {
      throw new CreatorVideosError('douyin_creator_short_link_unresolved', 'Douyin short-link redirect returned no final URL.');
    }
    return response.url;
  } finally {
    clearTimeout(timer);
  }
}

function translateRuntimeError(error: unknown): Error {
  if (!(error instanceof CreatorVideosError)) {
    return new CommandExecutionError(
      `Openmd Douyin creator discovery failed: ${error instanceof Error ? error.message : String(error)}`,
    );
  }
  if (error.code === 'douyin_creator_input_invalid') {
    return new ArgumentError(error.message);
  }
  if (error.code === 'douyin_auth_challenge') {
    return new AuthRequiredError('www.douyin.com', error.message);
  }
  return new CommandExecutionError(`[${error.code}] ${error.message}`);
}

function safeUrl(value: string): URL | null {
  try {
    return new URL(value.includes('://') ? value : `https://${value}`);
  } catch {
    return null;
  }
}

function isDouyinHost(hostname: string): boolean {
  const host = hostname.toLowerCase();
  return host === 'douyin.com' || host.endsWith('.douyin.com') || host === 'iesdouyin.com' || host.endsWith('.iesdouyin.com');
}

function isObject(value: unknown): value is JsonObject {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

function stringValue(value: unknown): string {
  if (value === null || value === undefined) return '';
  return typeof value === 'string' ? value.trim() : String(value).trim();
}

function numberValue(value: unknown): number | null {
  const numeric = typeof value === 'number' ? value : Number(value);
  return Number.isFinite(numeric) ? numeric : null;
}

function booleanValue(value: unknown): boolean {
  if (typeof value === 'boolean') return value;
  if (typeof value === 'number') return value !== 0;
  return ['1', 'true', 'yes'].includes(stringValue(value).toLowerCase());
}

function boundedInteger(value: unknown, fallback: number, minimum: number, maximum: number): number {
  const numeric = numberValue(value);
  if (numeric === null) return fallback;
  return Math.min(maximum, Math.max(minimum, Math.round(numeric)));
}

function normalizePublishedAt(value: unknown): string {
  const numeric = numberValue(value);
  if (numeric === null || numeric <= 0) return stringValue(value);
  const milliseconds = numeric > 10_000_000_000 ? numeric : numeric * 1000;
  return new Date(milliseconds).toISOString();
}

cli({
  site: 'openmd-douyin',
  name: 'creator-videos',
  access: 'read',
  description: 'Resolve one Douyin creator link and paginate every video visible to the current browser session.',
  example: 'opencli openmd-douyin creator-videos https://v.douyin.com/example/ -f json',
  domain: 'www.douyin.com',
  strategy: Strategy.COOKIE,
  browser: true,
  defaultFormat: 'json',
  args: [
    { name: 'creator', type: 'string', required: true, positional: true, help: 'Creator share URL, profile URL, or sec_uid' },
    { name: 'timeout', type: 'int', default: DEFAULT_COMMAND_TIMEOUT_SECONDS, help: 'Whole command timeout in seconds for full pagination' },
    { name: 'progress', type: 'bool', default: false, help: 'Write sanitized page progress to stderr' },
    { name: 'page_delay_ms', type: 'int', default: DEFAULT_PAGE_DELAY_MS, help: 'Delay between API pages' },
    { name: 'max_pages', type: 'int', default: DEFAULT_MAX_PAGES, help: 'Emergency stop only; not a completeness limit' },
    { name: 'request_timeout_ms', type: 'int', default: DEFAULT_REQUEST_TIMEOUT_MS, help: 'Browser-context API timeout per page' },
  ],
  columns: [
    'index',
    'aweme_id',
    'title',
    'author',
    'published_at',
    'duration',
    'digg_count',
    'aweme_type',
    'image_count',
    'media_kind',
    'play_url',
    'transcript_candidates',
    'page_count',
    'empty_page_count',
    'duplicate_page_count',
    'pagination_retry_count',
    'source_exhausted',
  ],
  func: async (page, kwargs) => {
    const progress = kwargs.progress === true;
    const report = (stage: string, metadata: JsonObject = {}) => {
      if (!progress) return;
      process.stderr.write(`${JSON.stringify({ stage, ...metadata })}\n`);
    };
    try {
      report('resolve_creator_started');
      const target = await resolveCreatorTarget(page, String(kwargs.creator ?? ''));
      report('resolve_creator_finished');
      // Short-link resolution can take long enough for the pre-navigation target
      // to be replaced by Chrome. Drop the cached page identity before the real
      // creator navigation so Page.goto() acquires a fresh target deterministically.
      page.setActivePage?.(undefined);
      report('bridge_navigation_started');
      await page.goto(DOUYIN_STABLE_BRIDGE_URL);
      report('bridge_navigation_finished');
      await page.wait(3);
      report('bridge_navigation_settled');
      const requestTimeoutMs = boundedInteger(
        kwargs.request_timeout_ms,
        DEFAULT_REQUEST_TIMEOUT_MS,
        1000,
        120_000,
      );
      const pageDelayMs = boundedInteger(kwargs.page_delay_ms, DEFAULT_PAGE_DELAY_MS, 0, 60_000);
      const maxPages = boundedInteger(kwargs.max_pages, DEFAULT_MAX_PAGES, 1, 100_000);
      report('browser_pagination_started');
      const browserResult = await collectCreatorVideosInBrowser(page, target, {
        pageDelayMs,
        maxPages,
        requestTimeoutMs,
      });
      const rows = browserResult.rows;
      report('browser_pagination_finished', { page_count: browserResult.pageCount, item_count: rows.length });
      if (rows.length === 0) {
        throw new EmptyResultError(
          'openmd-douyin creator-videos',
          'No accessible videos were returned. Confirm the creator and logged-in Douyin session.',
        );
      }
      report('creator_discovery_finished', { item_count: rows.length });
      return rows;
    } catch (error) {
      if (error instanceof EmptyResultError) throw error;
      throw translateRuntimeError(error);
    }
  },
});
