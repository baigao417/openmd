import assert from 'node:assert/strict';
import test from 'node:test';

import {
  collectCreatorVideos,
  CreatorVideosError,
  extractTranscriptCandidates,
  normalizePostPage,
  parseCreatorTarget,
  projectCreatorVideo,
  resolveCreatorTarget,
} from './creator-videos.ts';

test('parseCreatorTarget accepts raw sec_uid and canonical profile URLs', () => {
  assert.deepEqual(parseCreatorTarget('MS4wLjABAAAA-test_user_123'), {
    secUid: 'MS4wLjABAAAA-test_user_123',
    creatorUrl: 'https://www.douyin.com/user/MS4wLjABAAAA-test_user_123',
  });
  assert.deepEqual(
    parseCreatorTarget('https://www.douyin.com/user/MS4wLjABAAAA-test_user_123?from_tab_name=main'),
    {
      secUid: 'MS4wLjABAAAA-test_user_123',
      creatorUrl: 'https://www.douyin.com/user/MS4wLjABAAAA-test_user_123',
    },
  );
});

test('resolveCreatorTarget follows a short link and finds the creator anchor', async () => {
  const page = {} as never;
  assert.deepEqual(await resolveCreatorTarget(
    page,
    'https://v.douyin.com/example/',
    async () => 'https://www.iesdouyin.com/share/user/MS4wLjABAAAA-anchor_user?sec_uid=MS4wLjABAAAA-anchor_user',
  ), {
    secUid: 'MS4wLjABAAAA-anchor_user',
    creatorUrl: 'https://www.douyin.com/user/MS4wLjABAAAA-anchor_user',
  });
});

test('collectCreatorVideos paginates to exhaustion and deduplicates aweme ids', async () => {
  const pages = new Map([
    ['0', { status_code: 0, aweme_list: [video('1000001'), video('1000002')], has_more: 1, max_cursor: 10 }],
    ['10', { status_code: 0, aweme_list: [video('1000002'), video('1000003')], has_more: 1, max_cursor: 20 }],
    ['20', { status_code: 0, aweme_list: [video('1000004')], has_more: 0, max_cursor: 20 }],
  ]);
  const cursors: string[] = [];
  const rows = await collectCreatorVideos(
    async (cursor) => {
      cursors.push(cursor);
      return pages.get(cursor);
    },
    { pageDelayMs: 0 },
  );
  assert.deepEqual(cursors, ['0', '10', '20']);
  assert.deepEqual(rows.map((row) => row.aweme_id), ['1000001', '1000002', '1000003', '1000004']);
  assert.deepEqual(rows.map((row) => row.index), [1, 2, 3, 4]);
});

test('collectCreatorVideos rejects a stalled cursor', async () => {
  await assert.rejects(
    collectCreatorVideos(
      async () => ({ status_code: 0, aweme_list: [video('1000001')], has_more: 1, max_cursor: 0 }),
      { pageDelayMs: 0 },
    ),
    (error: unknown) => error instanceof CreatorVideosError && error.code === 'douyin_pagination_cursor_stall',
  );
});

test('collectCreatorVideos rejects a duplicate-only page', async () => {
  let calls = 0;
  await assert.rejects(
    collectCreatorVideos(
      async () => {
        calls += 1;
        return calls === 1
          ? { status_code: 0, aweme_list: [video('1000001')], has_more: 1, max_cursor: 1 }
          : { status_code: 0, aweme_list: [video('1000001')], has_more: 1, max_cursor: 2 };
      },
      { pageDelayMs: 0 },
    ),
    (error: unknown) => error instanceof CreatorVideosError && error.code === 'douyin_pagination_duplicate_page',
  );
});

test('normalizePostPage maps authentication challenges without retaining response data', () => {
  assert.throws(
    () => normalizePostPage({ status_code: 403, status_msg: 'captcha verify required', cookie: 'secret' }),
    (error: unknown) =>
      error instanceof CreatorVideosError &&
      error.code === 'douyin_auth_challenge' &&
      !error.message.includes('secret'),
  );
});

test('projectCreatorVideo returns only transient acquisition fields and normalized candidates', () => {
  const row = projectCreatorVideo(
    {
      aweme_id: '1000001',
      desc: 'title',
      create_time: 1_700_000_000,
      author: { nickname: 'creator' },
      aweme_type: 0,
      video: { duration: 12_345, play_addr: { url_list: ['https://media.example/video'] } },
      statistics: { digg_count: 9 },
      video_text: [{ text: 'first line' }, { text: 'first line' }, { text: 'second line' }],
      raw_response: { cookie: 'secret' },
    },
    1,
  );
  assert.equal(row?.duration, 12);
  assert.equal(row?.media_kind, 'video');
  assert.deepEqual(row?.transcript_candidates, [
    { source: 'video_text', text: 'first line' },
    { source: 'video_text', text: 'second line' },
  ]);
  assert.equal('raw_response' in (row ?? {}), false);
});

test('projectCreatorVideo classifies image posts without exposing image URLs', () => {
  const row = projectCreatorVideo(
    {
      aweme_id: '1000068',
      desc: 'image post',
      aweme_type: 68,
      images: [{ url_list: ['https://image.example/secret'] }],
      video: { duration: 0, play_addr: { url_list: ['https://media.example/audio'] } },
    },
    1,
  );

  assert.equal(row?.aweme_type, 68);
  assert.equal(row?.image_count, 1);
  assert.equal(row?.media_kind, 'image_text');
  assert.equal('images' in (row ?? {}), false);
});

test('extractTranscriptCandidates ignores description and unrelated text fields', () => {
  assert.deepEqual(
    extractTranscriptCandidates({
      desc: 'not a transcript',
      video_text: { segments: [{ content: 'spoken text' }] },
      text_extra: [{ hashtag_name: 'topic' }],
    }),
    [{ source: 'video_text', text: 'spoken text' }],
  );
});

function video(awemeId: string) {
  return {
    aweme_id: awemeId,
    desc: `video ${awemeId}`,
    author: { nickname: 'creator' },
    video: { duration: 1000, play_addr: { url_list: [`https://media.example/${awemeId}`] } },
    statistics: { digg_count: 1 },
  };
}
