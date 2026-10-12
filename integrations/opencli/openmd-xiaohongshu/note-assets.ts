import * as fs from 'node:fs';
import * as path from 'node:path';

import { formatCookieHeader } from '@jackwener/opencli/download';
import { downloadMedia } from '@jackwener/opencli/download/media-download';
import { cli, Strategy } from '@jackwener/opencli/registry';
import {
  ArgumentError,
  AuthRequiredError,
  CliError,
  CommandExecutionError,
  EmptyResultError,
} from '@jackwener/opencli/errors';
import type { IPage } from '@jackwener/opencli/types';

const NOTE_ID_RE = /^[0-9a-f]{24}$/i;
const USER_ID_RE = /^[A-Za-z0-9]{8,64}$/;
const DEFAULT_MAX_SCROLLS = 2000;
const DEFAULT_STABLE_ROUNDS = 4;
const DEFAULT_SCROLL_DELAY_MS = 1500;

type JsonObject = Record<string, unknown>;

export type NoteAsset = {
  type: 'image' | 'video';
  remoteUrl: string;
  filename: string;
};

export type NoteAssetPlan = {
  noteId: string;
  title: string;
  author: string;
  nativeText: string;
  tags: string[];
  mediaKind: 'text' | 'image_text' | 'video' | 'mixed';
  nativeTranscript: string;
  assets: NoteAsset[];
  imageCount: number;
  videoCount: number;
};

export type SafeAssetRow = {
  row_type: 'note' | 'asset';
  note_id: string;
  title: string;
  author: string;
  native_text: string;
  tags: string[];
  media_kind: string;
  native_transcript: string;
  image_count: number;
  video_count: number;
  asset_index: number;
  asset_type: string;
  asset_status: string;
  asset_error_code: string;
  local_path: string;
};

export class NoteAssetsError extends Error {
  readonly code: string;

  constructor(code: string, message: string) {
    super(message);
    this.name = 'NoteAssetsError';
    this.code = code;
  }
}

export function parseNoteId(input: string): string | null {
  const value = String(input ?? '').trim();
  if (NOTE_ID_RE.test(value)) return value.toLowerCase();
  const parsed = safeUrl(value);
  if (!parsed || !isXiaohongshuHost(parsed.hostname)) return null;
  const parts = parsed.pathname.split('/').filter(Boolean);
  if (parts.length === 3 && parts[0] === 'user' && parts[1] === 'profile') return null;
  const candidate = parts.at(-1) ?? '';
  return NOTE_ID_RE.test(candidate) ? candidate.toLowerCase() : null;
}

export function parseProfileId(input: string): string | null {
  const value = String(input ?? '').trim();
  if (!value) return null;
  if (USER_ID_RE.test(value) && !value.includes('/') && !value.includes('://')) return value;
  const parsed = safeUrl(value);
  if (!parsed || !isXiaohongshuHost(parsed.hostname)) return null;
  const parts = parsed.pathname.split('/').filter(Boolean);
  return parts.length === 3 && parts[0] === 'user' && parts[1] === 'profile' && USER_ID_RE.test(parts[2])
    ? parts[2]
    : null;
}

export function buildNoteAssetPlan(payload: unknown, fallbackNoteId: string): NoteAssetPlan {
  if (!isObject(payload)) {
    throw new NoteAssetsError('xiaohongshu_note_assets_invalid', 'Xiaohongshu note detail payload was malformed.');
  }
  const noteId = stringValue(payload.noteId) || fallbackNoteId;
  if (payload.targetMismatch === true || (fallbackNoteId && noteId.toLowerCase() !== fallbackNoteId.toLowerCase())) {
    throw new NoteAssetsError('xiaohongshu_note_target_invalid', 'The loaded note did not match the requested note.');
  }
  if (payload.targetUnresolved === true) {
    throw new NoteAssetsError('xiaohongshu_note_target_invalid', 'The loaded page did not identify a canonical note.');
  }
  if (!NOTE_ID_RE.test(noteId)) {
    throw new NoteAssetsError('xiaohongshu_note_id_invalid', 'Xiaohongshu note detail returned no stable note id.');
  }
  const rawImages = Array.isArray(payload.images) ? payload.images : [];
  const rawVideos = Array.isArray(payload.videos) ? payload.videos : [];
  const assets: NoteAsset[] = [];
  const seen = new Set<string>();
  let imageIndex = 0;
  for (const value of rawImages) {
    const remoteUrl = normalizeAllowedMediaUrl(stringValue(value));
    if (!remoteUrl || seen.has(remoteUrl)) continue;
    seen.add(remoteUrl);
    imageIndex += 1;
    assets.push({ type: 'image', remoteUrl, filename: `image_${String(imageIndex).padStart(3, '0')}.jpg` });
  }
  let videoIndex = 0;
  for (const value of rawVideos) {
    const remoteUrl = normalizeAllowedMediaUrl(stringValue(value));
    if (!remoteUrl || seen.has(remoteUrl)) continue;
    seen.add(remoteUrl);
    videoIndex += 1;
    assets.push({ type: 'video', remoteUrl, filename: `video_${String(videoIndex).padStart(3, '0')}.mp4` });
    break;
  }
  const imageCount = assets.filter((asset) => asset.type === 'image').length;
  const videoCount = assets.filter((asset) => asset.type === 'video').length;
  const mediaKind = videoCount && imageCount ? 'mixed' : videoCount ? 'video' : imageCount ? 'image_text' : 'text';
  return {
    noteId: noteId.toLowerCase(),
    title: redactSensitiveText(stringValue(payload.title)),
    author: redactSensitiveText(stringValue(payload.author)),
    nativeText: redactSensitiveText(stringValue(payload.nativeText)),
    tags: stringList(payload.tags).map(redactSensitiveText),
    mediaKind,
    nativeTranscript: redactSensitiveText(stringValue(payload.nativeTranscript)),
    assets,
    imageCount,
    videoCount,
  };
}

export function buildSafeAssetRows(
  plan: NoteAssetPlan,
  results: Array<{ index: number; status: string; size?: string }>,
  options: { downloaded: boolean },
): SafeAssetRow[] {
  const downloaded = options.downloaded;
  const common = {
    note_id: plan.noteId,
    title: plan.title,
    author: plan.author,
    native_text: plan.nativeText,
    tags: plan.tags,
    media_kind: plan.mediaKind,
    native_transcript: plan.nativeTranscript,
    image_count: plan.imageCount,
    video_count: plan.videoCount,
  };
  const rows: SafeAssetRow[] = [{
    row_type: 'note',
    ...common,
    asset_index: 0,
    asset_type: '',
    asset_status: downloaded ? 'metadata_ready' : 'download_disabled',
    asset_error_code: '',
    local_path: '',
  }];
  for (const [offset, asset] of plan.assets.entries()) {
    const result = results.find((entry) => entry.index === offset + 1);
    const success = downloaded && result?.status === 'success';
    rows.push({
      row_type: 'asset',
      ...common,
      asset_index: offset + 1,
      asset_type: asset.type,
      asset_status: success ? 'success' : downloaded ? 'failed' : 'not_downloaded',
      asset_error_code: success || !downloaded ? '' : classifyDownloadFailure(result?.size),
      local_path: success ? asset.filename : '',
    });
  }
  return rows;
}

const PROFILE_NOTE_RESOLVE_JS = (noteId: string) => `
  (() => {
    const noteId = ${JSON.stringify(noteId)};
    const unwrap = (value) => {
      if (!value || typeof value !== 'object') return value;
      if ('_value' in value) return value._value;
      if ('value' in value) return value.value;
      return value;
    };
    const isObject = (value) => Boolean(value) && typeof value === 'object' && !Array.isArray(value);
    const probePinia = (el) => el?.__vue_app__?.config?.globalProperties?.$pinia ?? null;
    let pinia = probePinia(document.querySelector('#app'));
    if (!pinia) {
      for (const el of document.querySelectorAll('*')) {
        pinia = probePinia(el);
        if (pinia) break;
      }
    }
    let store = null;
    if (pinia?._s) {
      for (const [id, candidate] of pinia._s.entries()) {
        const state = candidate?.$state ?? candidate;
        const candidateNotes = unwrap(state?.notes ?? candidate?.notes);
        if (Array.isArray(candidateNotes) && String(id).toLowerCase().includes('user')) {
          store = candidate;
          break;
        }
      }
    }
    const initial = window.__INITIAL_STATE__?.user;
    store = store || initial;
    const storeState = store?.$state ?? store;
    const notes = unwrap(store?.notes ?? storeState?.notes);
    const groups = Array.isArray(notes) ? notes : [];
    let noteCount = 0;
    for (const group of groups) {
      const entries = Array.isArray(group) ? group : [group];
      noteCount += entries.filter(Boolean).length;
      for (const entry of entries) {
        const card = entry?.noteCard ?? entry?.note_card ?? entry;
        const id = String(card?.noteId ?? card?.note_id ?? entry?.noteId ?? entry?.note_id ?? entry?.id ?? '');
        if (id !== noteId) continue;
        const userId = String(card?.user?.userId ?? card?.user?.user_id ?? '');
        const token = String(entry?.xsecToken ?? entry?.xsec_token ?? card?.xsecToken ?? card?.xsec_token ?? '');
        if (!userId || !token) return { found: true, signedUrl: '', loginWall: false, explicitExhausted: false, noteCount };
        const url = new URL('https://www.xiaohongshu.com/user/profile/' + userId + '/' + noteId);
        url.searchParams.set('xsec_token', token);
        url.searchParams.set('xsec_source', 'pc_user');
        return { found: true, signedUrl: url.toString(), loginWall: false, explicitExhausted: false, noteCount };
      }
    }
    const rawPageData = unwrap(
      store?.noteQueries ?? storeState?.noteQueries
      ?? store?.pageData ?? store?.page_data
      ?? storeState?.pageData ?? storeState?.page_data
      ?? store?.userPageData ?? storeState?.userPageData
      ?? initial?.userPageData
    );
    let activeGroupIndex = 0;
    let activeGroupCount = 0;
    for (let index = 0; index < groups.length; index += 1) {
      const count = Array.isArray(groups[index]) ? groups[index].filter(Boolean).length : groups[index] ? 1 : 0;
      if (count > activeGroupCount) {
        activeGroupCount = count;
        activeGroupIndex = index;
      }
    }
    let pageData = rawPageData;
    if (Array.isArray(rawPageData) && rawPageData[activeGroupIndex] != null) {
      pageData = rawPageData[activeGroupIndex];
    } else if (isObject(rawPageData) && rawPageData[String(activeGroupIndex)] != null) {
      pageData = rawPageData[String(activeGroupIndex)];
    }
    const hasMore = pageData?.hasMore ?? pageData?.has_more;
    const bodyText = String(document.body?.innerText || '');
    return {
      found: false,
      signedUrl: '',
      loginWall: location.pathname.indexOf('/login') === 0 || unwrap(store?.loggedIn ?? store?.logged_in) === false,
      explicitExhausted: hasMore === false || hasMore === 0 || /没有更多|暂无更多|到底了|已经到底/.test(bodyText.slice(-4000)),
      hasMore: hasMore === true || hasMore === 1,
      noteCount,
    };
  })()
`;

const PROFILE_NOTE_FETCH_MORE_JS = `
  (async () => {
    const unwrap = (value) => {
      if (!value || typeof value !== 'object') return value;
      if ('_value' in value) return value._value;
      if ('value' in value) return value.value;
      return value;
    };
    const probePinia = (el) => el?.__vue_app__?.config?.globalProperties?.$pinia ?? null;
    let pinia = probePinia(document.querySelector('#app'));
    if (!pinia) {
      for (const el of document.querySelectorAll('*')) {
        pinia = probePinia(el);
        if (pinia) break;
      }
    }
    let store = null;
    if (pinia?._s) {
      for (const [id, candidate] of pinia._s.entries()) {
        const state = candidate?.$state ?? candidate;
        const notes = unwrap(candidate?.notes ?? state?.notes);
        if (Array.isArray(notes) && String(id).toLowerCase().includes('user')) { store = candidate; break; }
      }
    }
    if (!store) return { requested: false, reason: 'store_missing' };
    const state = store?.$state ?? store;
    const notes = unwrap(store?.notes ?? state?.notes);
    const groups = Array.isArray(notes) ? notes : [];
    let activeIndex = 0;
    let activeCount = 0;
    for (let index = 0; index < groups.length; index += 1) {
      const count = Array.isArray(groups[index]) ? groups[index].filter(Boolean).length : groups[index] ? 1 : 0;
      if (count > activeCount) { activeIndex = index; activeCount = count; }
    }
    const queries = unwrap(store?.noteQueries ?? state?.noteQueries);
    const fetching = unwrap(store?.isFetchingNotes ?? state?.isFetchingNotes);
    const query = Array.isArray(queries) ? unwrap(queries[activeIndex]) : queries?.[String(activeIndex)] ?? queries;
    const isFetching = Array.isArray(fetching) ? Boolean(unwrap(fetching[activeIndex])) : Boolean(fetching?.[String(activeIndex)] ?? fetching);
    const hasMore = query?.hasMore ?? query?.has_more;
    if (hasMore === false || hasMore === 0) return { requested: false, reason: 'already_exhausted' };
    if (isFetching) return { requested: false, reason: 'already_fetching' };
    if (typeof store.fetchNotes !== 'function') return { requested: false, reason: 'fetch_action_missing' };
    try { await store.fetchNotes(); return { requested: true, reason: 'fetch_action' }; }
    catch { return { requested: false, reason: 'fetch_action_failed' }; }
  })()
`;

export const NOTE_DETAIL_JS = (noteId: string) => `
  (() => {
    const fallbackNoteId = ${JSON.stringify(noteId)};
    const clean = (value) => String(value ?? '').replace(/\\s+/g, ' ').trim();
    const rawText = (value) => String(value ?? '').replace(/\\r\\n?/g, '\\n').trim();
    const bodyText = String(document.body?.innerText || '');
    const result = {
      noteId: fallbackNoteId,
      securityBlock: /安全限制|访问链接异常/.test(bodyText) || /website-login\\/error|error_code=300017|error_code=300031/.test(location.href),
      loginWall: /登录后查看|请登录/.test(bodyText) || location.pathname.indexOf('/login') === 0,
      notFound: /页面不见了|笔记不存在|无法浏览/.test(bodyText),
      title: '', author: '', nativeText: '', tags: [], nativeTranscript: '', images: [], videos: [], diagnostics: {}
    };
    const locationMatch = String(location.pathname || '').match(/^\\/(?:explore|note|search_result|discovery\\/item)\\/([a-f0-9]{24})\\/?$|^\\/user\\/profile\\/[^/?#]+\\/([a-f0-9]{24})\\/?$/i);
    const pageNoteId = (locationMatch?.[1] || locationMatch?.[2] || '').toLowerCase();
    result.noteId = fallbackNoteId.toLowerCase() || pageNoteId;
    // A known requested id must never be replaced by another note after a
    // redirect. An unresolved share page is not evidence for any singleton.
    if (pageNoteId && fallbackNoteId && pageNoteId !== fallbackNoteId.toLowerCase()) {
      return { ...result, targetMismatch: true };
    }
    if (!result.noteId) return { ...result, targetUnresolved: true };
    // Only a canonical detail page can bind the rendered panel to the target.
    const scope = pageNoteId ? document.querySelector('#noteContainer') : null;
    result.title = clean(scope?.querySelector('#detail-title, .title')?.textContent);
    const description = scope?.querySelector('#detail-desc, .desc, .note-text');
    result.nativeText = rawText(description?.innerText ?? description?.textContent);
    result.author = clean(scope?.querySelector('.username, .author-wrapper .name')?.textContent);
    (scope?.querySelectorAll('#detail-desc a.tag, #detail-desc a[href*="search_result"]') || []).forEach((el) => {
      const value = clean(el.textContent); if (value) result.tags.push(value);
    });
    const pushUnique = (values, value) => {
      const normalized = String(value || '').trim();
      if (normalized && !values.includes(normalized)) values.push(normalized);
    };
    const state = window.__INITIAL_STATE__;
    const detailMap = state?.note?.noteDetailMap || state?.note?.note || {};
    const structuredNotes = [];
    const detailKeys = Object.keys(detailMap || {});
    const addNote = (entry, keyedId = '') => {
      const note = entry?.note ?? entry;
      if (!note || typeof note !== 'object' || Array.isArray(note)) return;
      const explicitId = String(note.noteId ?? note.note_id ?? note.id ?? '').toLowerCase();
      if ((explicitId || keyedId) !== result.noteId) return;
      structuredNotes.push(note);
    };
    addNote(detailMap[result.noteId], result.noteId);
    // Some hydration versions expose the note directly, rather than an id map.
    // Such objects require their own matching id; object count is never proof.
    if (structuredNotes.length === 0) addNote(state?.note?.note);
    for (const note of structuredNotes) {
      result.title = result.title || clean(note.title ?? note.displayTitle ?? note.display_title);
      // Hydration belongs to the validated target and retains the full source
      // body even when the rendered panel is collapsed or truncated.
      result.nativeText = rawText(note.desc ?? note.content ?? note.noteText) || result.nativeText;
      result.author = result.author || clean(note.user?.nickname ?? note.user?.nickName);
      const imageList = Array.isArray(note.imageList) ? note.imageList : Array.isArray(note.image_list) ? note.image_list : [];
      for (const image of imageList) {
        const url = image?.urlDefault || image?.urlPre || image?.url
          || image?.infoList?.find((item) => item?.imageScene === 'WB_DFT')?.url
          || image?.infoList?.[0]?.url || '';
        pushUnique(result.images, url);
      }
      const video = note.video || {};
      const candidates = [
        video.url, video.masterUrl, video.originVideoKey, video.consumer?.originVideoKey,
        ...(Array.isArray(video.media?.stream?.h264) ? video.media.stream.h264.map((item) => item?.masterUrl) : []),
      ];
      for (const candidate of candidates) {
        if (!candidate) continue;
        const value = String(candidate);
        pushUnique(result.videos, value.startsWith('http') ? value : 'https://sns-video-bd.xhscdn.com/' + value);
      }
      const transcriptValues = [note.videoText, note.video_text, note.subtitle, note.subtitles, note.captionInfo, note.caption_info];
      const lines = [];
      const visit = (value) => {
        if (typeof value === 'string') { if (rawText(value)) lines.push(rawText(value)); return; }
        if (Array.isArray(value)) { value.forEach(visit); return; }
        if (!value || typeof value !== 'object') return;
        for (const key of ['text','content','caption','sentence','utterance','value','items','segments','sentences','captions','subtitles','list']) {
          if (key in value) visit(value[key]);
        }
      };
      transcriptValues.forEach(visit);
      if (!result.nativeTranscript) result.nativeTranscript = lines.join('\\n');
    }
    if (result.images.length === 0) {
      for (const selector of [
        '.swiper-slide img', '.carousel-image img', '.note-slider img', '.note-image img',
        '.image-wrapper img', '.media-container img[src*="xhscdn"]',
      ]) {
        (scope?.querySelectorAll(selector) || []).forEach((image) => {
          const url = image.src || image.getAttribute('data-src') || '';
          if (/xhscdn|xiaohongshu|rednote/i.test(url)) pushUnique(result.images, url);
        });
      }
    }
    if (result.videos.length === 0) {
      for (const selector of ['video source', 'video[src]', '.player video', '.video-player video']) {
        (scope?.querySelectorAll(selector) || []).forEach((video) => {
          const url = video.src || video.getAttribute('src') || '';
          if (url && !url.startsWith('blob:')) pushUnique(result.videos, url);
        });
      }
    }
    result.diagnostics = {
      initial_state_note_present: Boolean(state?.note),
      detail_key_count: detailKeys.length,
      structured_note_count: structuredNotes.length,
      dom_image_count: document.querySelectorAll('img').length,
      scoped_image_count: scope?.querySelectorAll('.swiper-slide img, .carousel-image img, .note-slider img, .note-image img, .image-wrapper img, .media-container img').length || 0,
      background_image_count: document.querySelectorAll('[style*="background-image"]').length,
      dom_video_count: document.querySelectorAll('video, video source').length,
      candidate_image_count: result.images.length,
      candidate_video_count: result.videos.length,
    };
    return result;
  })()
`;

async function resolveSignedNoteUrl(
  page: IPage,
  profileId: string,
  noteId: string,
  options: { maxScrolls: number; stableRounds: number; scrollDelayMs: number; profileUrl?: string },
): Promise<string> {
  await page.goto(options.profileUrl || `https://www.xiaohongshu.com/user/profile/${profileId}`);
  await page.wait(2);
  let stable = 0;
  let stableRecoveryCount = 0;
  let previousCount = -1;
  let explicitEnd = false;
  for (let scroll = 0; scroll <= options.maxScrolls; scroll += 1) {
    const value = await page.evaluate(PROFILE_NOTE_RESOLVE_JS(noteId));
    if (!isObject(value)) throw new NoteAssetsError('xiaohongshu_profile_store_invalid', 'Profile store response was malformed.');
    if (value.loginWall === true) throw new NoteAssetsError('xiaohongshu_auth_challenge', 'Xiaohongshu profile requires login.');
    const signedUrl = stringValue(value.signedUrl);
    if (value.found === true && signedUrl) return signedUrl;
    const noteCount = boundedInteger(value.noteCount, 0, 0, 1_000_000);
    if (value.explicitExhausted === true) {
      explicitEnd = true;
      break;
    }
    stable = noteCount <= previousCount ? stable + 1 : 0;
    if (noteCount > previousCount) stableRecoveryCount = 0;
    previousCount = Math.max(previousCount, noteCount);
    if (stable >= options.stableRounds) {
      if (value.hasMore === true && stableRecoveryCount < 5) {
        stableRecoveryCount += 1;
        stable = 0;
      } else {
        break;
      }
    }
    if (scroll === options.maxScrolls) break;
    await page.autoScroll({ times: 1, delayMs: options.scrollDelayMs });
    await page.evaluate(PROFILE_NOTE_FETCH_MORE_JS);
    await page.wait(Math.max(0.2, options.scrollDelayMs / 1000));
  }
  if (explicitEnd) {
    throw new NoteAssetsError('xiaohongshu_note_not_found', 'The note was not visible before the creator profile reached an explicit end marker.');
  }
  throw new NoteAssetsError(
    'xiaohongshu_source_not_explicitly_exhausted',
    'The creator profile stabilized without an explicit end marker; retry note resolution.',
  );
}

function redactSensitiveText(value: string): string {
  return value
    .replace(/https?:\/\/[^\s]+/gi, (url) => {
      try {
        const parsed = new URL(url);
        for (const key of [...parsed.searchParams.keys()]) {
          if (/token|cookie|sign|auth|session/i.test(key)) parsed.searchParams.delete(key);
        }
        return parsed.toString();
      } catch { return '[redacted-url]'; }
    })
    .replace(/(?:bearer|cookie|authorization)\s*[:=]\s*[^\s]+/gi, '[redacted-credential]');
}

function isAllowedMediaUrl(value: string): boolean {
  const parsed = safeUrl(value);
  if (!parsed || parsed.protocol !== 'https:') return false;
  const host = parsed.hostname.toLowerCase();
  return ['xhscdn.com', 'xiaohongshu.com', 'rednote.com'].some(
    (allowed) => host === allowed || host.endsWith(`.${allowed}`),
  );
}

function normalizeAllowedMediaUrl(value: string): string {
  const input = String(value || '').trim();
  if (!input) return '';
  const normalized = input.startsWith('//') ? `https:${input}` : input.replace(/^http:\/\//i, 'https://');
  return isAllowedMediaUrl(normalized) ? normalized : '';
}

function classifyDownloadFailure(value: unknown): string {
  const text = stringValue(value).toLowerCase();
  if (/timeout|timed out|etimedout/.test(text)) return 'asset_download_timeout';
  if (/http|status|403|404|429|5\d\d/.test(text)) return 'asset_download_http_error';
  return 'asset_download_failed';
}

function stringList(value: unknown): string[] {
  return Array.isArray(value) ? value.map(stringValue).filter(Boolean) : [];
}

function safeUrl(value: string): URL | null {
  try { return new URL(value.includes('://') ? value : `https://${value}`); }
  catch { return null; }
}

function isXiaohongshuHost(hostname: string): boolean {
  const host = hostname.toLowerCase();
  return host === 'xiaohongshu.com' || host.endsWith('.xiaohongshu.com');
}

export function isSafeRuntimeSelector(value: string): boolean {
  const parsed = safeUrl(value);
  if (!parsed || !['http:', 'https:'].includes(parsed.protocol)) return false;
  const host = parsed.hostname.toLowerCase();
  if (host === 'xhslink.com' || host.endsWith('.xhslink.com')) return true;
  return isXiaohongshuHost(host)
    && Boolean(parsed.searchParams.get('xsec_token')?.trim())
    && Boolean(parseNoteId(value) || parseProfileId(value));
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

function boundedInteger(value: unknown, fallback: number, minimum: number, maximum: number): number {
  const numeric = numberValue(value);
  if (numeric === null) return fallback;
  return Math.min(maximum, Math.max(minimum, Math.round(numeric)));
}

export function translateError(error: unknown): Error {
  if (!(error instanceof NoteAssetsError)) {
    if (error instanceof Error && error.name === 'BrowserCommandError' && /^Navigation rejected\b/i.test(error.message)) {
      // Preserve only the recognized reason. Browser messages, codes and hints
      // may carry signed URLs or credentials and must never be forwarded.
      return new CliError(
        'NAVIGATION_REJECTED',
        'Navigation rejected by the browser host.',
        'Resolve the browser host navigation restriction before rerunning this command.',
      );
    }
    return new CommandExecutionError('Openmd Xiaohongshu note-assets failed during browser navigation or temporary media download.');
  }
  if (error.code.endsWith('_invalid')) return new ArgumentError(error.message);
  if (error.code === 'xiaohongshu_auth_challenge') return new AuthRequiredError('www.xiaohongshu.com', error.message);
  if (error.code === 'xiaohongshu_note_not_found') return new EmptyResultError('openmd-xiaohongshu note-assets', error.message);
  return new CommandExecutionError(`[${error.code}] ${error.message}`);
}

cli({
  site: 'openmd-xiaohongshu',
  name: 'note-assets',
  access: 'read',
  description: 'Resolve one canonical Xiaohongshu note inside the authenticated browser and export safe local assets.',
  example: 'opencli openmd-xiaohongshu note-assets <canonical-note-url> --creator <user-id> --output <temp-dir> -f json',
  domain: 'www.xiaohongshu.com',
  strategy: Strategy.COOKIE,
  browser: true,
  navigateBefore: false,
  defaultFormat: 'json',
  args: [
    { name: 'note', type: 'string', required: true, positional: true, help: 'Canonical note id or query-free note URL' },
    { name: 'selector_stdin', type: 'bool', default: false, help: 'Read a signed/share selector from stdin without placing it in argv, env, or files' },
    { name: 'creator', type: 'string', default: '', help: 'Optional creator URL or user id used to resolve the current signed note URL inside the browser' },
    { name: 'output', type: 'string', required: true, help: 'Caller-owned temporary output directory' },
    { name: 'download', type: 'bool', default: true, help: 'Download media into the caller-owned temporary directory' },
    { name: 'timeout', type: 'int', default: 900, help: 'Whole browser command timeout in seconds' },
    { name: 'diagnostics', type: 'bool', default: false, help: 'Return only safe page-shape counters for troubleshooting' },
    { name: 'max_scrolls', type: 'int', default: DEFAULT_MAX_SCROLLS, help: 'Profile resolution emergency stop' },
    { name: 'stable_rounds', type: 'int', default: DEFAULT_STABLE_ROUNDS, help: 'Stable profile snapshots before not-found' },
    { name: 'scroll_delay_ms', type: 'int', default: DEFAULT_SCROLL_DELAY_MS, help: 'Delay after profile scrolls' },
  ],
  columns: ['row_type','note_id','title','author','native_text','tags','media_kind','native_transcript','image_count','video_count','asset_index','asset_type','asset_status','asset_error_code','local_path','initial_state_note_present','detail_key_count','structured_note_count','dom_image_count','scoped_image_count','background_image_count','dom_video_count','candidate_image_count','candidate_video_count'],
  func: async (page, kwargs) => {
    try {
      let selector = '';
      if (kwargs.selector_stdin === true) {
        selector = fs.readFileSync(0, 'utf8').trim();
        if (!isSafeRuntimeSelector(selector)) {
          selector = '';
          throw new NoteAssetsError('xiaohongshu_selector_input_invalid', 'The stdin selector was not a supported Xiaohongshu note/share URL.');
        }
      }
      const selectorNoteId = parseNoteId(selector);
      const selectorProfileId = parseProfileId(selector);
      let noteId = parseNoteId(String(kwargs.note ?? '')) || selectorNoteId || '';
      if (!noteId && !selector) throw new NoteAssetsError('xiaohongshu_note_input_invalid', 'Expected a canonical Xiaohongshu note id or URL.');
      if (selectorProfileId && !noteId) throw new NoteAssetsError('xiaohongshu_note_input_invalid', 'A creator profile selector also requires a canonical note id.');
      const profileRaw = String(kwargs.creator ?? '').trim();
      const profileId = profileRaw ? parseProfileId(profileRaw) : selectorProfileId;
      if (profileRaw && !profileId) throw new NoteAssetsError('xiaohongshu_profile_input_invalid', 'The optional profile selector was invalid.');
      const outputInput = String(kwargs.output ?? '').trim();
      if (!outputInput) throw new NoteAssetsError('xiaohongshu_output_invalid', 'A caller-owned output directory is required.');
      const output = path.resolve(outputInput);
      fs.mkdirSync(output, { recursive: true });
      const runtimeProfileUrl = selectorProfileId ? selector : '';
      // Share links need their redirect to obtain the current signed note URL.
      // Keep the validated selector in memory until goto, including xhslink
      // selectors whose canonical note id cannot be known before navigation.
      let navigationUrl = selector && !selectorProfileId ? selector : `https://www.xiaohongshu.com/discovery/item/${noteId}`;
      selector = '';
      if (profileId && runtimeProfileUrl) {
        navigationUrl = await resolveSignedNoteUrl(page, profileId, noteId, {
          maxScrolls: boundedInteger(kwargs.max_scrolls, DEFAULT_MAX_SCROLLS, 1, 100_000),
          stableRounds: boundedInteger(kwargs.stable_rounds, DEFAULT_STABLE_ROUNDS, 2, 20),
          scrollDelayMs: boundedInteger(kwargs.scroll_delay_ms, DEFAULT_SCROLL_DELAY_MS, 200, 60_000),
          profileUrl: runtimeProfileUrl,
        });
      } else if (profileId && !kwargs.selector_stdin) {
        navigationUrl = await resolveSignedNoteUrl(page, profileId, noteId, {
          maxScrolls: boundedInteger(kwargs.max_scrolls, DEFAULT_MAX_SCROLLS, 1, 100_000),
          stableRounds: boundedInteger(kwargs.stable_rounds, DEFAULT_STABLE_ROUNDS, 2, 20),
          scrollDelayMs: boundedInteger(kwargs.scroll_delay_ms, DEFAULT_SCROLL_DELAY_MS, 200, 60_000),
          profileUrl: runtimeProfileUrl || undefined,
        });
      }
      await page.goto(navigationUrl);
      navigationUrl = '';
      await page.wait(2);
      const raw = await page.evaluate(NOTE_DETAIL_JS(noteId));
      if (!isObject(raw)) throw new NoteAssetsError('xiaohongshu_note_assets_invalid', 'Note detail response was malformed.');
      if (raw.securityBlock === true) throw new NoteAssetsError('xiaohongshu_security_block', 'Xiaohongshu blocked the note detail request.');
      if (raw.loginWall === true) throw new NoteAssetsError('xiaohongshu_auth_challenge', 'Xiaohongshu note detail requires login.');
      if (raw.notFound === true) throw new NoteAssetsError('xiaohongshu_note_not_found', 'The note is unavailable or was not found.');
      if (raw.targetMismatch === true || raw.targetUnresolved === true) {
        throw new NoteAssetsError('xiaohongshu_note_target_invalid', 'The loaded page did not identify the requested note.');
      }
      if (kwargs.diagnostics === true) {
        const diagnostics = isObject(raw.diagnostics) ? raw.diagnostics : {};
        return [{
          row_type: 'diagnostic',
          note_id: parseNoteId(String(raw.noteId ?? '')) || noteId,
          initial_state_note_present: raw.diagnostics && diagnostics.initial_state_note_present === true,
          detail_key_count: boundedInteger(diagnostics.detail_key_count, 0, 0, 1_000_000),
          structured_note_count: boundedInteger(diagnostics.structured_note_count, 0, 0, 1_000_000),
          dom_image_count: boundedInteger(diagnostics.dom_image_count, 0, 0, 1_000_000),
          scoped_image_count: boundedInteger(diagnostics.scoped_image_count, 0, 0, 1_000_000),
          background_image_count: boundedInteger(diagnostics.background_image_count, 0, 0, 1_000_000),
          dom_video_count: boundedInteger(diagnostics.dom_video_count, 0, 0, 1_000_000),
          candidate_image_count: boundedInteger(diagnostics.candidate_image_count, 0, 0, 1_000_000),
          candidate_video_count: boundedInteger(diagnostics.candidate_video_count, 0, 0, 1_000_000),
        }];
      }
      const plan = buildNoteAssetPlan(raw, noteId);
      noteId = '';
      const download = kwargs.download !== false;
      let results: Array<{ index: number; status: string; size?: string }> = [];
      if (download && plan.assets.length) {
        const cookies = formatCookieHeader(await page.getCookies({ domain: 'xiaohongshu.com' }));
        results = await downloadMedia(plan.assets.map((asset) => ({
          type: asset.type,
          url: asset.remoteUrl,
          filename: asset.filename,
        })), {
          output,
          cookies,
          timeout: 60_000,
          verbose: false,
        });
      }
      return buildSafeAssetRows(plan, results, { downloaded: download });
    } catch (error) {
      if (error instanceof EmptyResultError || error instanceof AuthRequiredError) throw error;
      throw translateError(error);
    }
  },
});
