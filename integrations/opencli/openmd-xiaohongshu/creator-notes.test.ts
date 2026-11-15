import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

import {
  buildRuntimeNoteUrl,
  collectProfileNotes,
  CreatorNotesError,
  parseProfileTarget,
  projectProfileNotes,
  summarizeActivePagination,
  type ProfileSnapshot,
} from './creator-notes.ts';

const fixtureSpec = JSON.parse(
  readFileSync(new URL('../../../tests/fixtures/xiaohongshu/full-profile-snapshots.json', import.meta.url), 'utf8'),
);
const fixture = {
  user_id: fixtureSpec.user_id,
  snapshots: fixtureSpec.pages.map((page: { start: number; end: number; has_more: boolean }) => ({
    storePresent: true,
    notesPresent: true,
    loginWall: false,
    explicitExhausted: page.has_more === false,
    exhaustionReason: page.has_more === false ? 'pinia_has_more_false' : '',
    storeSource: 'pinia:user',
    noteGroups: [Array.from({ length: page.end - page.start + 1 }, (_, offset) => {
      const ordinal = page.start + offset;
      const noteId = ordinal.toString(16).padStart(24, '0');
      return {
        xsecToken: `runtime-token-${ordinal}`,
        noteCard: {
          noteId,
          displayTitle: `Fixture note ${ordinal}`,
          type: ordinal % 3 === 0 ? 'video' : 'normal',
          user: { userId: fixtureSpec.user_id, nickname: 'Fixture creator' },
          interactInfo: { likedCount: ordinal },
        },
      };
    })],
  })),
};

test('parseProfileTarget accepts user ids and strips signed profile queries', () => {
  assert.deepEqual(parseProfileTarget('683e522f000000001d008dba'), {
    userId: '683e522f000000001d008dba',
    profileUrl: 'https://www.xiaohongshu.com/user/profile/683e522f000000001d008dba',
  });
  assert.deepEqual(
    parseProfileTarget(
      'https://www.xiaohongshu.com/user/profile/683e522f000000001d008dba?xsec_token=runtime-secret&xsec_source=pc_feed',
    ),
    {
      userId: '683e522f000000001d008dba',
      profileUrl: 'https://www.xiaohongshu.com/user/profile/683e522f000000001d008dba',
    },
  );
});

test('projectProfileNotes emits canonical rows without signed urls or token fields', () => {
  const snapshot = projectProfileNotes(fixture.snapshots[0], fixture.user_id);
  assert.equal(snapshot.rows.length, 25);
  assert.match(snapshot.rows[0].url, /^https:\/\/www\.xiaohongshu\.com\/discovery\/item\/[0-9a-f]{24}$/);
  assert.equal(snapshot.rows[0].url.includes('xsec_'), false);
  const serialized = JSON.stringify(snapshot.rows);
  assert.equal(serialized.includes('xsec_token'), false);
  assert.equal(serialized.includes('runtime_note_url'), false);
  assert.equal(serialized.includes('runtime-token'), false);
  assert.equal(serialized.includes('cookie'), false);
  assert.equal(serialized.includes('raw_response'), false);
});

test('projectProfileNotes can expose note-specific selectors only in transient mode', () => {
  const snapshot = projectProfileNotes(
    fixture.snapshots[0],
    fixture.user_id,
    { includeRuntimeNoteUrls: true },
  );
  const runtimeUrl = snapshot.rows[0].runtime_note_url || '';
  assert.equal(new URL(runtimeUrl).searchParams.get('xsec_token'), 'runtime-token-1');
  assert.equal(new URL(runtimeUrl).searchParams.get('xsec_source'), 'pc_user');
  assert.equal(runtimeUrl.includes(snapshot.rows[0].note_id), true);
  assert.equal(buildRuntimeNoteUrl(fixture.user_id, snapshot.rows[0].note_id, ''), '');
});

test('projectProfileNotes accepts DOM fallback note-card groups', () => {
  const noteId = '689c37a5000000001c0361a6';
  const snapshot = projectProfileNotes({
    storePresent: true,
    notesPresent: true,
    loginWall: false,
    explicitExhausted: true,
    exhaustionReason: 'dom_end_marker',
    storeSource: 'dom:note-cards',
    noteGroups: [[{ noteCard: { noteId, displayTitle: 'DOM note', type: 'video' } }]],
  }, fixture.user_id);
  assert.equal(snapshot.rows.length, 1);
  assert.equal(snapshot.rows[0].note_id, noteId);
  assert.equal(snapshot.rows[0].type, 'video');
  assert.equal(snapshot.storeSource, 'dom:note-cards');
});

test('collectProfileNotes discovers more than 50 notes, deduplicates, and stops on explicit exhaustion', async () => {
  const snapshots = fixture.snapshots.map((value: unknown) => projectProfileNotes(value, fixture.user_id));
  let readIndex = 0;
  let scrollCount = 0;
  const result = await collectProfileNotes(
    async () => snapshots[Math.min(readIndex++, snapshots.length - 1)],
    async () => { scrollCount += 1; },
    { stableRounds: 4, maxScrolls: 20 },
  );

  assert.equal(result.rows.length, 65);
  assert.equal(new Set(result.rows.map((row) => row.note_id)).size, 65);
  assert.equal(scrollCount, 2);
  assert.equal(result.metadata.page_count, 3);
  assert.equal(result.metadata.source_exhausted, true);
  assert.equal(result.metadata.explicit_exhaustion, true);
  assert.equal(result.metadata.exhaustion_reason, 'pinia_has_more_false');
  assert.equal(result.metadata.duplicate_snapshot_count, 0);
  const serialized = JSON.stringify(result.rows);
  assert.equal(serialized.includes('xsec_token'), false);
  assert.equal(serialized.includes('runtime_note_url'), false);
  assert.equal(serialized.includes('runtime-token'), false);
});

test('collectProfileNotes blocks duplicate-only stable snapshots without claiming exhaustion', async () => {
  const base = projectProfileNotes(fixture.snapshots[0], fixture.user_id);
  const snapshots: ProfileSnapshot[] = [base, base, base, base];
  let index = 0;
  await assert.rejects(
    collectProfileNotes(
      async () => snapshots[Math.min(index++, snapshots.length - 1)],
      async () => {},
      { stableRounds: 3, stableRecoveryRounds: 1, maxScrolls: 10 },
    ),
    (error: unknown) =>
      error instanceof CreatorNotesError &&
      error.code === 'xiaohongshu_source_not_explicitly_exhausted',
  );
});

test('collectProfileNotes accepts an empty active Pinia cursor after stable snapshots', async () => {
  const base = projectProfileNotes(fixture.snapshots[0], fixture.user_id);
  const cursorEnded: ProfileSnapshot = {
    ...base,
    explicitExhausted: false,
    exhaustionReason: '',
    cursorEmptyCount: 1,
    cursorNonEmptyCount: 0,
    cursorKeys: ['pageData.cursor'],
  };
  let reads = 0;
  const result = await collectProfileNotes(
    async () => { reads += 1; return cursorEnded; },
    async () => {},
    { stableRounds: 2, maxScrolls: 10 },
  );
  assert.equal(reads, 3);
  assert.equal(result.metadata.explicit_exhaustion, true);
  assert.equal(result.metadata.exhaustion_reason, 'pinia_cursor_empty');
});

test('collectProfileNotes maps login walls without retaining snapshot secrets', async () => {
  await assert.rejects(
    collectProfileNotes(
      async () => ({
        rows: [],
        storePresent: true,
        notesPresent: true,
        loginWall: true,
        explicitExhausted: false,
        exhaustionReason: '',
        storeSource: 'pinia:user',
      }),
      async () => {},
    ),
    (error: unknown) =>
      error instanceof CreatorNotesError &&
      error.code === 'xiaohongshu_auth_challenge' &&
      !error.message.includes('token'),
  );
});

test('projectProfileNotes rejects malformed snapshots without echoing input', () => {
  assert.throws(
    () => projectProfileNotes('xsec_token=do-not-leak', fixture.user_id),
    (error: unknown) =>
      error instanceof CreatorNotesError &&
      error.code === 'xiaohongshu_profile_snapshot_invalid' &&
      !error.message.includes('do-not-leak'),
  );
});

test('active pagination ignores inactive tab has-more indicators', () => {
  const summary = summarizeActivePagination(
    [
      { hasMore: false },
      { hasMore: true },
    ],
    [Array.from({ length: 89 }, (_, index) => index + 1), []],
  );
  assert.equal(summary.activeGroupIndex, 0);
  assert.equal(summary.activeGroupCount, 89);
  assert.equal(summary.hasMoreTrueCount, 0);
  assert.equal(summary.hasMoreFalseCount, 1);
  assert.equal(summary.explicitExhausted, true);
  assert.deepEqual(summary.indicatorKeys, ['pageData.hasMore']);
});
