import * as fs from 'node:fs';

import { cli, Strategy } from '@jackwener/opencli/registry';
import {
  ArgumentError,
  AuthRequiredError,
  CommandExecutionError,
  EmptyResultError,
} from '@jackwener/opencli/errors';
import type { IPage } from '@jackwener/opencli/types';

const DEFAULT_SCROLL_DELAY_MS = 1500;
const DEFAULT_STABLE_ROUNDS = 4;
const DEFAULT_MAX_SCROLLS = 2000;
const DEFAULT_COMMAND_TIMEOUT_SECONDS = 1800;
const NOTE_ID_RE = /^[0-9a-f]{24}$/i;
const USER_ID_RE = /^[A-Za-z0-9]{8,64}$/;

type JsonObject = Record<string, unknown>;

export type ProfileTarget = {
  userId: string;
  profileUrl: string;
};

export type CreatorNoteRow = {
  index: number;
  note_id: string;
  title: string;
  author: string;
  published_at: string;
  type: string;
  likes: string;
  url: string;
  page_count?: number;
  scroll_count?: number;
  stable_round_count?: number;
  empty_snapshot_count?: number;
  duplicate_snapshot_count?: number;
  anomaly_count?: number;
  source_exhausted?: boolean;
  explicit_exhaustion?: boolean;
  exhaustion_reason?: string;
  store_source?: string;
  runtime_note_url?: string;
};

export type ProfileSnapshot = {
  rows: CreatorNoteRow[];
  storePresent: boolean;
  notesPresent: boolean;
  loginWall: boolean;
  explicitExhausted: boolean;
  exhaustionReason: string;
  storeSource: string;
  hasMoreTrueCount?: number;
  hasMoreFalseCount?: number;
  indicatorKeys?: string[];
  bottomMarker?: boolean;
  pageDataPresent?: boolean;
  activeGroupIndex?: number;
  activeGroupCount?: number;
  cursorEmptyCount?: number;
  cursorNonEmptyCount?: number;
  cursorKeys?: string[];
  pageDataKeys?: string[];
  storeKeys?: string[];
  paginationShape?: JsonObject;
};

export type DiscoveryMetadata = {
  page_count: number;
  scroll_count: number;
  stable_round_count: number;
  empty_snapshot_count: number;
  duplicate_snapshot_count: number;
  anomaly_count: number;
  source_exhausted: boolean;
  explicit_exhaustion: boolean;
  exhaustion_reason: string;
  store_source: string;
};

export class CreatorNotesError extends Error {
  readonly code: string;

  constructor(code: string, message: string) {
    super(message);
    this.name = 'CreatorNotesError';
    this.code = code;
  }
}

export function parseProfileTarget(input: string): ProfileTarget | null {
  const value = String(input ?? '').trim();
  if (USER_ID_RE.test(value) && !value.includes('://') && !value.includes('/')) {
    return {
      userId: value,
      profileUrl: `https://www.xiaohongshu.com/user/profile/${value}`,
    };
  }

  const parsed = safeUrl(value);
  if (!parsed || !isXiaohongshuHost(parsed.hostname)) return null;
  const parts = parsed.pathname.split('/').filter(Boolean);
  if (parts.length !== 3 || parts[0] !== 'user' || parts[1] !== 'profile') return null;
  const userId = parts[2] ?? '';
  if (!USER_ID_RE.test(userId)) return null;
  return {
    userId,
    profileUrl: `https://www.xiaohongshu.com/user/profile/${userId}`,
  };
}

function isSafeRuntimeProfileSelector(input: string): boolean {
  const parsed = safeUrl(String(input ?? '').trim());
  if (!parsed || !isXiaohongshuHost(parsed.hostname)) return false;
  const parts = parsed.pathname.split('/').filter(Boolean);
  return parts.length === 3
    && parts[0] === 'user'
    && parts[1] === 'profile'
    && USER_ID_RE.test(parts[2] ?? '')
    && Boolean(parsed.searchParams.get('xsec_token')?.trim());
}

export function projectProfileNotes(
  rawSnapshot: unknown,
  fallbackUserId: string,
  options: { includeRuntimeNoteUrls?: boolean } = {},
): ProfileSnapshot {
  if (!isObject(rawSnapshot)) {
    throw new CreatorNotesError(
      'xiaohongshu_profile_snapshot_invalid',
      'Xiaohongshu profile snapshot was not an object.',
    );
  }
  const storePresent = rawSnapshot.storePresent === true;
  const notesPresent = rawSnapshot.notesPresent === true;
  const loginWall = rawSnapshot.loginWall === true;
  const explicitExhausted = rawSnapshot.explicitExhausted === true;
  const exhaustionReason = stringValue(rawSnapshot.exhaustionReason);
  const storeSource = stringValue(rawSnapshot.storeSource) || 'unknown';
  const hasMoreTrueCount = boundedInteger(rawSnapshot.hasMoreTrueCount, 0, 0, 1_000_000);
  const hasMoreFalseCount = boundedInteger(rawSnapshot.hasMoreFalseCount, 0, 0, 1_000_000);
  const indicatorKeys = Array.isArray(rawSnapshot.indicatorKeys)
    ? rawSnapshot.indicatorKeys.map(stringValue).filter(Boolean).slice(0, 100)
    : [];
  const bottomMarker = rawSnapshot.bottomMarker === true;
  const pageDataPresent = rawSnapshot.pageDataPresent === true;
  const activeGroupIndex = boundedInteger(rawSnapshot.activeGroupIndex, 0, 0, 1000);
  const activeGroupCount = boundedInteger(rawSnapshot.activeGroupCount, 0, 0, 1_000_000);
  const cursorEmptyCount = boundedInteger(rawSnapshot.cursorEmptyCount, 0, 0, 1_000_000);
  const cursorNonEmptyCount = boundedInteger(rawSnapshot.cursorNonEmptyCount, 0, 0, 1_000_000);
  const cursorKeys = Array.isArray(rawSnapshot.cursorKeys)
    ? rawSnapshot.cursorKeys.map(stringValue).filter(Boolean).slice(0, 100)
    : [];
  const pageDataKeys = Array.isArray(rawSnapshot.pageDataKeys)
    ? rawSnapshot.pageDataKeys.map(stringValue).filter(Boolean).slice(0, 100)
    : [];
  const storeKeys = Array.isArray(rawSnapshot.storeKeys)
    ? rawSnapshot.storeKeys.map(stringValue).filter(Boolean).slice(0, 200)
    : [];
  const paginationShape = isObject(rawSnapshot.paginationShape) ? rawSnapshot.paginationShape : {};
  const groups = Array.isArray(rawSnapshot.noteGroups) ? rawSnapshot.noteGroups : [];
  const rows: CreatorNoteRow[] = [];
  const seen = new Set<string>();

  for (const entry of flattenNoteGroups(groups)) {
    if (!isObject(entry)) continue;
    const noteCard = isObject(entry.noteCard)
      ? entry.noteCard
      : isObject(entry.note_card)
        ? entry.note_card
        : entry;
    const noteId = stringValue(
      noteCard.noteId ?? noteCard.note_id ?? entry.noteId ?? entry.note_id ?? entry.id,
    );
    if (!NOTE_ID_RE.test(noteId) || seen.has(noteId)) continue;
    seen.add(noteId);

    const user = isObject(noteCard.user) ? noteCard.user : {};
    const userId = stringValue(user.userId ?? user.user_id) || fallbackUserId;
    const interact = isObject(noteCard.interactInfo)
      ? noteCard.interactInfo
      : isObject(noteCard.interact_info)
        ? noteCard.interact_info
        : {};
    const canonicalUrl = `https://www.xiaohongshu.com/discovery/item/${noteId}`;
    const row: CreatorNoteRow = {
      index: rows.length + 1,
      note_id: noteId,
      title: stringValue(noteCard.displayTitle ?? noteCard.display_title ?? noteCard.title),
      author: stringValue(user.nickname ?? user.nickName ?? noteCard.author_name),
      published_at: normalizePublishedAt(
        noteCard.publishTime ?? noteCard.publish_time ?? noteCard.time ?? entry.publish_time,
      ),
      type: stringValue(noteCard.type ?? entry.type),
      likes: stringValue(interact.likedCount ?? interact.liked_count ?? 0) || '0',
      url: canonicalUrl,
    };
    if (options.includeRuntimeNoteUrls === true) {
      const token = stringValue(
        entry.xsecToken ?? entry.xsec_token ?? noteCard.xsecToken ?? noteCard.xsec_token,
      );
      const runtimeNoteUrl = buildRuntimeNoteUrl(userId, noteId, token);
      if (runtimeNoteUrl) row.runtime_note_url = runtimeNoteUrl;
    }
    rows.push(row);
  }

  return {
    rows,
    storePresent,
    notesPresent,
    loginWall,
    explicitExhausted,
    exhaustionReason,
    storeSource,
    hasMoreTrueCount,
    hasMoreFalseCount,
    indicatorKeys,
    bottomMarker,
    pageDataPresent,
    activeGroupIndex,
    activeGroupCount,
    cursorEmptyCount,
    cursorNonEmptyCount,
    cursorKeys,
    pageDataKeys,
    storeKeys,
    paginationShape,
  };
}

export function buildRuntimeNoteUrl(userId: string, noteId: string, token: string): string {
  if (!USER_ID_RE.test(userId) || !NOTE_ID_RE.test(noteId) || !token.trim()) return '';
  const url = new URL(`https://www.xiaohongshu.com/user/profile/${userId}/${noteId}`);
  url.searchParams.set('xsec_token', token.trim());
  url.searchParams.set('xsec_source', 'pc_user');
  return url.toString();
}

export function summarizeActivePagination(
  rawPageData: unknown,
  noteGroups: unknown[],
): {
  activeGroupIndex: number;
  activeGroupCount: number;
  hasMoreTrueCount: number;
  hasMoreFalseCount: number;
  indicatorKeys: string[];
  explicitExhausted: boolean;
} {
  let activeGroupIndex = 0;
  let activeGroupCount = 0;
  for (let index = 0; index < noteGroups.length; index += 1) {
    const group = noteGroups[index];
    const count = Array.isArray(group) ? group.filter(Boolean).length : group ? 1 : 0;
    if (count > activeGroupCount) {
      activeGroupIndex = index;
      activeGroupCount = count;
    }
  }
  let pageData = rawPageData;
  if (Array.isArray(rawPageData) && rawPageData[activeGroupIndex] != null) {
    pageData = rawPageData[activeGroupIndex];
  } else if (isObject(rawPageData) && rawPageData[String(activeGroupIndex)] != null) {
    pageData = rawPageData[String(activeGroupIndex)];
  }
  const indicators: Array<{ key: string; value: boolean }> = [];
  const visit = (value: unknown, depth = 0, path = 'pageData') => {
    if (depth > 5 || !value) return;
    if (Array.isArray(value)) {
      value.forEach((entry, index) => visit(entry, depth + 1, `${path}[${index}]`));
      return;
    }
    if (!isObject(value)) return;
    for (const [key, child] of Object.entries(value)) {
      const normalized = key.replace(/[_-]/g, '').toLowerCase();
      if ((normalized === 'hasmore' || normalized === 'more') && (typeof child === 'boolean' || child === 0 || child === 1)) {
        indicators.push({ key: `${path}.${key}`, value: Boolean(child) });
      } else if ((normalized === 'nomore' || normalized === 'isend' || normalized === 'finished') && (typeof child === 'boolean' || child === 0 || child === 1)) {
        indicators.push({ key: `${path}.${key}`, value: !Boolean(child) });
      }
      visit(child, depth + 1, `${path}.${key}`);
    }
  };
  visit(pageData);
  const hasMoreTrueCount = indicators.filter((indicator) => indicator.value).length;
  const hasMoreFalseCount = indicators.length - hasMoreTrueCount;
  return {
    activeGroupIndex,
    activeGroupCount,
    hasMoreTrueCount,
    hasMoreFalseCount,
    indicatorKeys: [...new Set(indicators.map((indicator) => indicator.key))],
    explicitExhausted: hasMoreTrueCount === 0 && hasMoreFalseCount > 0,
  };
}

export async function collectProfileNotes(
  readSnapshot: () => Promise<ProfileSnapshot>,
  scroll: () => Promise<void>,
  options: {
    stableRounds?: number;
    stableRecoveryRounds?: number;
    maxScrolls?: number;
  } = {},
): Promise<{ rows: CreatorNoteRow[]; metadata: DiscoveryMetadata }> {
  const stableRoundsRequired = boundedInteger(
    options.stableRounds,
    DEFAULT_STABLE_ROUNDS,
    2,
    20,
  );
  const maxScrolls = boundedInteger(options.maxScrolls, DEFAULT_MAX_SCROLLS, 1, 100_000);
  const stableRecoveryRounds = boundedInteger(options.stableRecoveryRounds, 5, 0, 20);
  const rows: CreatorNoteRow[] = [];
  const seen = new Set<string>();
  let pageCount = 0;
  let scrollCount = 0;
  let stableRoundCount = 0;
  let emptySnapshotCount = 0;
  let duplicateSnapshotCount = 0;
  let stableRecoveryCount = 0;
  let storeSource = 'unknown';

  while (true) {
    const snapshot = await readSnapshot();
    pageCount += 1;
    storeSource = snapshot.storeSource || storeSource;
    if (snapshot.loginWall) {
      throw new CreatorNotesError(
        'xiaohongshu_auth_challenge',
        'Xiaohongshu profile discovery requires a valid logged-in browser session.',
      );
    }
    if (!snapshot.storePresent || !snapshot.notesPresent) {
      throw new CreatorNotesError(
        'xiaohongshu_profile_store_unavailable',
        'Xiaohongshu profile note store was unavailable after page navigation.',
      );
    }

    let added = 0;
    for (const row of snapshot.rows) {
      if (!NOTE_ID_RE.test(row.note_id) || seen.has(row.note_id)) continue;
      seen.add(row.note_id);
      rows.push({ ...row, index: rows.length + 1 });
      added += 1;
    }
    if (snapshot.rows.length === 0) emptySnapshotCount += 1;
    if (snapshot.rows.length > 0 && added === 0) duplicateSnapshotCount += 1;
    stableRoundCount = added === 0 ? stableRoundCount + 1 : 0;
    if (added > 0) stableRecoveryCount = 0;

    if (snapshot.explicitExhausted) {
      return finishDiscovery(rows, {
        page_count: pageCount,
        scroll_count: scrollCount,
        stable_round_count: stableRoundCount,
        empty_snapshot_count: emptySnapshotCount,
        duplicate_snapshot_count: duplicateSnapshotCount,
        anomaly_count: emptySnapshotCount + duplicateSnapshotCount,
        source_exhausted: true,
        explicit_exhaustion: true,
        exhaustion_reason: snapshot.exhaustionReason || 'explicit_page_end',
        store_source: storeSource,
      });
    }
    if (
      stableRoundCount >= stableRoundsRequired
      && (snapshot.cursorEmptyCount ?? 0) > 0
      && (snapshot.cursorNonEmptyCount ?? 0) === 0
    ) {
      return finishDiscovery(rows, {
        page_count: pageCount,
        scroll_count: scrollCount,
        stable_round_count: stableRoundCount,
        empty_snapshot_count: emptySnapshotCount,
        duplicate_snapshot_count: duplicateSnapshotCount,
        anomaly_count: emptySnapshotCount + duplicateSnapshotCount,
        source_exhausted: true,
        explicit_exhaustion: true,
        exhaustion_reason: 'pinia_cursor_empty',
        store_source: storeSource,
      });
    }
    if (stableRoundCount >= stableRoundsRequired) {
      if (stableRecoveryCount < stableRecoveryRounds && scrollCount < maxScrolls) {
        stableRecoveryCount += 1;
        stableRoundCount = 0;
        await scroll();
        scrollCount += 1;
        continue;
      }
      const diagnostics = safeSnapshotDiagnostics(snapshot, {
        note_count: rows.length,
        store_source: snapshot.storeSource,
        stable_recovery_count: stableRecoveryCount,
        page_count: pageCount,
        scroll_count: scrollCount,
        stable_round_count: stableRoundCount,
      });
      const compactDiagnostics = {
        n: diagnostics.note_count,
        src: diagnostics.store_source,
        ht: diagnostics.has_more_true_count,
        hf: diagnostics.has_more_false_count,
        ik: Array.isArray(diagnostics.indicator_keys) ? diagnostics.indicator_keys.length : 0,
        bm: diagnostics.bottom_marker,
        pd: diagnostics.page_data_present,
        agi: diagnostics.active_group_index,
        agc: diagnostics.active_group_count,
        ce: diagnostics.cursor_empty_count,
        cn: diagnostics.cursor_non_empty_count,
        ck: diagnostics.cursor_keys,
        pk: diagnostics.page_data_keys,
        sk: diagnostics.store_keys,
        ps: diagnostics.pagination_shape,
        rc: diagnostics.stable_recovery_count,
        pages: diagnostics.page_count,
        scrolls: diagnostics.scroll_count,
      };
      throw new CreatorNotesError(
        'xiaohongshu_source_not_explicitly_exhausted',
        `Xiaohongshu profile rows stabilized without an explicit platform end marker; diag=${JSON.stringify(compactDiagnostics)}`,
      );
    }
    if (scrollCount >= maxScrolls) {
      throw new CreatorNotesError(
        'xiaohongshu_scroll_emergency_stop',
        `Xiaohongshu profile discovery exceeded ${maxScrolls} scrolls without a terminal signal.`,
      );
    }
    await scroll();
    scrollCount += 1;
  }
}

function safeSnapshotDiagnostics(
  snapshot: ProfileSnapshot,
  counters: JsonObject = {},
): JsonObject {
  return {
    note_count: snapshot.rows.length,
    store_source: snapshot.storeSource,
    has_more_true_count: snapshot.hasMoreTrueCount ?? 0,
    has_more_false_count: snapshot.hasMoreFalseCount ?? 0,
    indicator_keys: snapshot.indicatorKeys ?? [],
    bottom_marker: snapshot.bottomMarker === true,
    page_data_present: snapshot.pageDataPresent === true,
    active_group_index: snapshot.activeGroupIndex ?? 0,
    active_group_count: snapshot.activeGroupCount ?? 0,
    cursor_empty_count: snapshot.cursorEmptyCount ?? 0,
    cursor_non_empty_count: snapshot.cursorNonEmptyCount ?? 0,
    cursor_keys: snapshot.cursorKeys ?? [],
    page_data_keys: snapshot.pageDataKeys ?? [],
    store_keys: snapshot.storeKeys ?? [],
    pagination_shape: snapshot.paginationShape ?? {},
    explicit_exhausted: snapshot.explicitExhausted,
    exhaustion_reason: snapshot.exhaustionReason,
    ...counters,
  };
}

function finishDiscovery(
  rows: CreatorNoteRow[],
  metadata: DiscoveryMetadata,
): { rows: CreatorNoteRow[]; metadata: DiscoveryMetadata } {
  return {
    rows: rows.map((row, index) => ({
      ...row,
      index: index + 1,
      ...metadata,
    })),
    metadata,
  };
}

const PROFILE_SNAPSHOT_JS = `
  (() => {
    const safeClone = (value) => {
      try { return JSON.parse(JSON.stringify(value ?? null)); }
      catch { return null; }
    };
    const unwrap = (value) => {
      if (!value || typeof value !== 'object') return value;
      if ('_value' in value) return value._value;
      if ('value' in value) return value.value;
      return value;
    };
    const isObject = (value) => Boolean(value) && typeof value === 'object' && !Array.isArray(value);
    const findPinia = () => {
      const probe = (el) => el?.__vue_app__?.config?.globalProperties?.$pinia ?? null;
      let pinia = probe(document.querySelector('#app'));
      if (pinia) return pinia;
      for (const el of document.querySelectorAll('*')) {
        pinia = probe(el);
        if (pinia) return pinia;
      }
      return null;
    };
    const pinia = findPinia();
    let store = null;
    let storeSource = 'none';
    if (pinia?._s) {
      for (const [id, candidate] of pinia._s.entries()) {
        const state = candidate?.$state ?? candidate;
        const notes = unwrap(candidate?.notes ?? state?.notes);
        if (Array.isArray(notes) && String(id).toLowerCase().includes('user')) {
          store = candidate;
          storeSource = 'pinia:' + String(id);
          break;
        }
      }
    }
    if (!store && isObject(window.__INITIAL_STATE__?.user)) {
      store = window.__INITIAL_STATE__.user;
      storeSource = 'initial_state:user';
    }
    const storeState = store?.$state ?? store;
    const rawNotes = unwrap(store?.notes ?? storeState?.notes);
    const safeShape = (raw, depth = 0) => {
      const value = unwrap(raw);
      if (value == null) return { t: 'null' };
      if (typeof value === 'boolean') return { t: 'b', v: value };
      if (typeof value === 'number') return { t: 'n', v: Number.isFinite(value) ? value : null };
      if (typeof value === 'string') return { t: 's', e: value.length === 0, n: value.length };
      if (Array.isArray(value)) return {
        t: 'a', n: value.length,
        c: depth < 1 ? value.slice(0, 5).map((entry) => safeShape(entry, depth + 1)) : [],
      };
      if (!isObject(value)) return { t: typeof value };
      const keys = Object.keys(value).slice(0, 50);
      const shape = { t: 'o', k: keys, b: {}, n: {}, s: {} };
      for (const key of keys) {
        const child = unwrap(value[key]);
        if (typeof child === 'boolean') shape.b[key] = child;
        else if (typeof child === 'number') shape.n[key] = Number.isFinite(child) ? child : null;
        else if (typeof child === 'string') shape.s[key] = { e: child.length === 0, n: child.length };
      }
      return shape;
    };
    const paginationShape = {};
    for (const key of ['isFetchingNotes','userNoteFetchingStatus','noteQueries','firstFetchNote','pageScrolled','activeTab','activeSubTab','curDisplayTab']) {
      const candidate = store?.[key] ?? storeState?.[key];
      if (candidate !== undefined) paginationShape[key] = safeShape(candidate);
    }
    const domNotes = [];
    const seenDomNoteIds = new Set();
    for (const anchor of document.querySelectorAll('a[href]')) {
      const href = String(anchor.href || anchor.getAttribute('href') || '');
      const match = href.match(/\\/(?:explore|note|search_result|discovery\\/item)\\/([a-f0-9]{24})|\\/user\\/profile\\/[^/?#]+\\/([a-f0-9]{24})/i);
      const noteId = match ? (match[1] || match[2] || '') : '';
      if (!noteId || seenDomNoteIds.has(noteId)) continue;
      seenDomNoteIds.add(noteId);
      const card = anchor.closest('[class*="note-item"], [class*="note-card"], section, li') || anchor.parentElement;
      const title = String(
        anchor.querySelector('[class*="title"]')?.textContent
        || card?.querySelector('[class*="title"]')?.textContent
        || anchor.getAttribute('title')
        || ''
      ).replace(/\\s+/g, ' ').trim();
      const isVideo = Boolean(card?.querySelector('video, [class*="play"], [class*="video"]'));
      domNotes.push({ noteCard: { noteId, displayTitle: title, type: isVideo ? 'video' : '' } });
    }
    const rawPageData = unwrap(
      store?.noteQueries ?? storeState?.noteQueries
      ?? store?.pageData ?? store?.page_data
      ?? storeState?.pageData ?? storeState?.page_data
      ?? store?.userPageData ?? storeState?.userPageData
      ?? window.__INITIAL_STATE__?.user?.userPageData
    );
    const noteGroups = Array.isArray(rawNotes) && rawNotes.length > 0
      ? rawNotes
      : domNotes.length > 0
        ? [domNotes]
        : [];
    let activeGroupIndex = 0;
    let activeGroupCount = 0;
    for (let index = 0; index < noteGroups.length; index += 1) {
      const count = Array.isArray(noteGroups[index]) ? noteGroups[index].filter(Boolean).length : noteGroups[index] ? 1 : 0;
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
    const indicators = [];
    const cursorIndicators = [];
    const visit = (value, depth = 0, path = 'pageData') => {
      if (depth > 5 || !value) return;
      if (Array.isArray(value)) {
        for (let index = 0; index < value.length; index += 1) visit(value[index], depth + 1, path + '[' + index + ']');
        return;
      }
      if (!isObject(value)) return;
      for (const [key, child] of Object.entries(value)) {
        const normalized = key.replace(/[_-]/g, '').toLowerCase();
        if ((normalized === 'hasmore' || normalized === 'more') && (typeof child === 'boolean' || child === 0 || child === 1)) {
          indicators.push({ key: path + '.' + key, value: Boolean(child) });
        } else if ((normalized === 'nomore' || normalized === 'isend' || normalized === 'finished') && (typeof child === 'boolean' || child === 0 || child === 1)) {
          indicators.push({ key: path + '.' + key, value: !Boolean(child) });
        }
        if (normalized.includes('cursor')) {
          const empty = child == null || child === '' || (Array.isArray(child) && child.length === 0);
          cursorIndicators.push({ key: path + '.' + key, empty });
        }
        visit(child, depth + 1, path + '.' + key);
      }
    };
    visit(pageData);
    const bodyTail = String(document.body?.innerText || '').slice(-4000);
    const bottomMarker = /没有更多|暂无更多|到底了|已经到底|the end/i.test(bodyTail);
    const hasMoreTrueCount = indicators.filter((item) => item.value === true).length;
    const hasMoreFalseCount = indicators.filter((item) => item.value === false).length;
    const cursorEmptyCount = cursorIndicators.filter((item) => item.empty === true).length;
    const cursorNonEmptyCount = cursorIndicators.length - cursorEmptyCount;
    const explicitExhausted = bottomMarker || (indicators.length > 0 && hasMoreTrueCount === 0 && hasMoreFalseCount > 0);
    const pathName = String(location?.pathname || '');
    const loggedIn = unwrap(store?.loggedIn ?? store?.logged_in ?? storeState?.loggedIn ?? storeState?.logged_in);
    return {
      noteGroups: safeClone(noteGroups),
      storePresent: Boolean(store) || domNotes.length > 0,
      notesPresent: Array.isArray(rawNotes) || domNotes.length > 0,
      loginWall: pathName.indexOf('/login') === 0 || loggedIn === false,
      explicitExhausted,
      exhaustionReason: bottomMarker ? 'dom_end_marker' : explicitExhausted ? 'pinia_has_more_false' : '',
      storeSource: store ? storeSource : domNotes.length > 0 ? 'dom:note-cards' : storeSource,
      hasMoreTrueCount,
      hasMoreFalseCount,
      indicatorKeys: [...new Set(indicators.map((item) => item.key))].slice(0, 100),
      bottomMarker,
      pageDataPresent: Boolean(pageData && (typeof pageData !== 'object' || Object.keys(pageData).length > 0)),
      activeGroupIndex,
      activeGroupCount,
      cursorEmptyCount,
      cursorNonEmptyCount,
      cursorKeys: [...new Set(cursorIndicators.map((item) => item.key))].slice(0, 100),
      pageDataKeys: isObject(pageData) ? Object.keys(pageData).slice(0, 100) : [],
      storeKeys: store && typeof store === 'object'
        ? Object.keys(store).filter((key) => !key.startsWith('$') && !key.startsWith('_')).slice(0, 200)
        : [],
      paginationShape,
    };
  })()
`;

const PROFILE_FETCH_MORE_JS = `
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
        if (Array.isArray(notes) && String(id).toLowerCase().includes('user')) {
          store = candidate;
          break;
        }
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
    if (hasMore === false || hasMore === 0) return { requested: false, reason: 'already_exhausted', activeIndex, activeCount };
    if (isFetching) return { requested: false, reason: 'already_fetching', activeIndex, activeCount };
    if (typeof store.fetchNotes !== 'function') return { requested: false, reason: 'fetch_action_missing', activeIndex, activeCount };
    try {
      await store.fetchNotes();
      return { requested: true, reason: 'fetch_action', activeIndex, activeCount };
    } catch {
      return { requested: false, reason: 'fetch_action_failed', activeIndex, activeCount };
    }
  })()
`;

async function readProfileSnapshot(
  page: IPage,
  userId: string,
  includeRuntimeNoteUrls: boolean,
): Promise<ProfileSnapshot> {
  return projectProfileNotes(
    await page.evaluate(PROFILE_SNAPSHOT_JS),
    userId,
    { includeRuntimeNoteUrls },
  );
}

async function collectProfileNotesInBrowser(
  page: IPage,
  target: ProfileTarget,
  options: {
    stableRounds: number;
    maxScrolls: number;
    scrollDelayMs: number;
    report?: (stage: string, metadata?: JsonObject) => void;
    includeRuntimeNoteUrls?: boolean;
  },
): Promise<{ rows: CreatorNoteRow[]; metadata: DiscoveryMetadata }> {
  let initialHydrationAttempts = 0;
  return collectProfileNotes(
    async () => {
      let snapshot = await readProfileSnapshot(
        page,
        target.userId,
        options.includeRuntimeNoteUrls === true,
      );
      while (
        !snapshot.loginWall
        && (!snapshot.storePresent || !snapshot.notesPresent)
        && initialHydrationAttempts < 5
      ) {
        initialHydrationAttempts += 1;
        options.report?.('profile_hydration_wait', { attempt: initialHydrationAttempts });
        await page.wait(1);
        snapshot = await readProfileSnapshot(
          page,
          target.userId,
          options.includeRuntimeNoteUrls === true,
        );
      }
      options.report?.('profile_snapshot', safeSnapshotDiagnostics(snapshot));
      return snapshot;
    },
    async () => {
      await page.autoScroll({ times: 1, delayMs: options.scrollDelayMs });
      const fetchMore = await page.evaluate(PROFILE_FETCH_MORE_JS);
      if (isObject(fetchMore)) {
        options.report?.('profile_fetch_more', {
          requested: fetchMore.requested === true,
          reason: stringValue(fetchMore.reason),
          active_group_index: boundedInteger(fetchMore.activeIndex, 0, 0, 1000),
          active_group_count: boundedInteger(fetchMore.activeCount, 0, 0, 1_000_000),
        });
      }
      await page.wait(Math.max(0.2, options.scrollDelayMs / 1000));
    },
    options,
  );
}

function flattenNoteGroups(groups: unknown[]): unknown[] {
  const values: unknown[] = [];
  for (const group of groups) {
    if (Array.isArray(group)) values.push(...group);
    else if (group) values.push(group);
  }
  return values;
}

function translateRuntimeError(error: unknown): Error {
  if (!(error instanceof CreatorNotesError)) {
    return new CommandExecutionError(
      `Openmd Xiaohongshu creator discovery failed: ${error instanceof Error ? error.message : String(error)}`,
    );
  }
  if (error.code === 'xiaohongshu_profile_input_invalid') {
    return new ArgumentError(error.message);
  }
  if (error.code === 'xiaohongshu_auth_challenge') {
    return new AuthRequiredError('www.xiaohongshu.com', error.message);
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

function isXiaohongshuHost(hostname: string): boolean {
  const host = hostname.toLowerCase();
  return host === 'xiaohongshu.com' || host.endsWith('.xiaohongshu.com');
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

function normalizePublishedAt(value: unknown): string {
  const numeric = numberValue(value);
  if (numeric === null || numeric <= 0) return stringValue(value);
  const milliseconds = numeric > 10_000_000_000 ? numeric : numeric * 1000;
  return new Date(milliseconds).toISOString();
}

cli({
  site: 'openmd-xiaohongshu',
  name: 'creator-notes',
  access: 'read',
  description: 'Read every Xiaohongshu note visible on one creator profile by scrolling the authenticated page store to exhaustion.',
  example: 'opencli openmd-xiaohongshu creator-notes https://www.xiaohongshu.com/user/profile/example -f json',
  domain: 'www.xiaohongshu.com',
  strategy: Strategy.COOKIE,
  browser: true,
  navigateBefore: false,
  defaultFormat: 'json',
  args: [
    { name: 'creator', type: 'string', required: true, positional: true, help: 'Creator profile URL or user id' },
    { name: 'selector_stdin', type: 'bool', default: false, help: 'Read a signed creator selector from stdin without placing it in argv, env, files, or output' },
    { name: 'transient_note_urls', type: 'bool', default: false, help: 'Machine-only: include current per-note signed URLs in captured JSON; requires selector_stdin' },
    { name: 'timeout', type: 'int', default: DEFAULT_COMMAND_TIMEOUT_SECONDS, help: 'Whole command timeout in seconds' },
    { name: 'progress', type: 'bool', default: false, help: 'Write sanitized scroll progress to stderr' },
    { name: 'scroll_delay_ms', type: 'int', default: DEFAULT_SCROLL_DELAY_MS, help: 'Delay after each read-only page scroll' },
    { name: 'stable_rounds', type: 'int', default: DEFAULT_STABLE_ROUNDS, help: 'Unchanged snapshots required before stable exhaustion' },
    { name: 'max_scrolls', type: 'int', default: DEFAULT_MAX_SCROLLS, help: 'Emergency stop only; not a completeness limit' },
  ],
  columns: [
    'index',
    'note_id',
    'title',
    'author',
    'published_at',
    'type',
    'likes',
    'url',
    'page_count',
    'scroll_count',
    'stable_round_count',
    'empty_snapshot_count',
    'duplicate_snapshot_count',
    'anomaly_count',
    'source_exhausted',
    'explicit_exhaustion',
    'exhaustion_reason',
    'store_source',
    'runtime_note_url',
  ],
  func: async (page, kwargs) => {
    const report = (stage: string, metadata: JsonObject = {}) => {
      if (kwargs.progress !== true) return;
      process.stderr.write(`${JSON.stringify({ stage, ...metadata })}\n`);
    };
    try {
      let selector = '';
      if (kwargs.selector_stdin === true) {
        selector = fs.readFileSync(0, 'utf8').trim();
        if (!isSafeRuntimeProfileSelector(selector)) {
          selector = '';
          throw new CreatorNotesError(
            'xiaohongshu_profile_selector_invalid',
            'The stdin selector was not a supported signed Xiaohongshu creator profile URL.',
          );
        }
      }
      if (kwargs.transient_note_urls === true && kwargs.selector_stdin !== true) {
        throw new CreatorNotesError(
          'xiaohongshu_transient_note_urls_invalid',
          'Transient note URLs require a signed creator selector through stdin.',
        );
      }
      const target = parseProfileTarget(selector || String(kwargs.creator ?? ''));
      if (!target) {
        throw new CreatorNotesError(
          'xiaohongshu_profile_input_invalid',
          'Expected a Xiaohongshu creator profile URL or user id.',
        );
      }
      report('profile_navigation_started');
      let navigationUrl = selector || target.profileUrl;
      selector = '';
      await page.goto(navigationUrl);
      navigationUrl = '';
      await page.wait(2);
      report('profile_navigation_finished');
      const result = await collectProfileNotesInBrowser(page, target, {
        stableRounds: boundedInteger(kwargs.stable_rounds, DEFAULT_STABLE_ROUNDS, 2, 20),
        maxScrolls: boundedInteger(kwargs.max_scrolls, DEFAULT_MAX_SCROLLS, 1, 100_000),
        scrollDelayMs: boundedInteger(kwargs.scroll_delay_ms, DEFAULT_SCROLL_DELAY_MS, 200, 60_000),
        report,
        includeRuntimeNoteUrls: kwargs.transient_note_urls === true,
      });
      report('creator_discovery_finished', {
        item_count: result.rows.length,
        scroll_count: result.metadata.scroll_count,
        exhaustion_reason: result.metadata.exhaustion_reason,
      });
      if (result.rows.length === 0) {
        throw new EmptyResultError(
          'openmd-xiaohongshu creator-notes',
          'No accessible notes were visible for this creator in the current browser session.',
        );
      }
      return result.rows;
    } catch (error) {
      if (error instanceof EmptyResultError) throw error;
      throw translateRuntimeError(error);
    }
  },
});
