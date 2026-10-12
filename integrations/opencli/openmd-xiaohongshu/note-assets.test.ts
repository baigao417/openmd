import assert from 'node:assert/strict';
import test from 'node:test';
import { runInNewContext } from 'node:vm';
import { getRegistry } from '@jackwener/opencli/registry';
import { ArgumentError, CliError } from '@jackwener/opencli/errors';

import { NOTE_DETAIL_JS, buildNoteAssetPlan, buildSafeAssetRows, isSafeRuntimeSelector, parseNoteId, parseProfileId, translateError } from './note-assets.ts';

const NOTE_ID = 'ffff00000000000000000002';
const OTHER_ID = 'ffff00000000000000000003';

// Selector matches are declared per fixture node; querySelector uses document
// order, just like the browser (comma-separated selectors have no priority).
function fixtureNode(selectors: string[], text = '', src = '', children: any[] = []) {
  return {
    selectors, textContent: text, innerText: text, src,
    getAttribute: (name: string) => name === 'src' ? src : null,
    querySelectorAll(selector: string) {
      const choices = selector.split(',').map((value) => value.trim());
      return children.filter((node) => choices.some((value) => node.selectors.includes(value)));
    },
    querySelector(selector: string) { return this.querySelectorAll(selector)[0] ?? null; },
  };
}

function extractFixture({ target = NOTE_ID, pathname = `/explore/${NOTE_ID}`, scoped = [] as any[], outside = [] as any[], state = {} as any, container = true } = {}) {
  const scope = fixtureNode(['#noteContainer'], '', '', scoped);
  const document = Object.assign(fixtureNode([], '', '', [...outside, ...(container ? [scope, ...scoped] : [])]), {
    body: { innerText: '' },
  });
  return runInNewContext(NOTE_DETAIL_JS(target), {
    document, window: { __INITIAL_STATE__: state },
    location: { pathname, href: `https://www.xiaohongshu.com${pathname}` },
  });
}

test('detail extraction ignores recommendation fields preceding the target panel', () => {
  const raw = extractFixture({
    outside: [fixtureNode(['.title'], 'Recommendation'), fixtureNode(['.desc'], 'Wrong body'), fixtureNode(['.username'], 'Wrong author')],
    scoped: [fixtureNode(['#detail-title', '.title'], 'Target'), fixtureNode(['#detail-desc', '.desc'], 'Target body'), fixtureNode(['.username'], 'Target author')],
  });
  assert.equal(raw.title, 'Target');
  assert.equal(raw.nativeText, 'Target body');
  assert.equal(raw.author, 'Target author');
});

test('untitled target remains untitled instead of inheriting recommendation title', () => {
  const raw = extractFixture({ outside: [fixtureNode(['.title'], 'Recommendation')], scoped: [fixtureNode(['#detail-desc'], 'Target body')] });
  assert.equal(raw.title, '');
});

test('a mismatched singleton hydration entry cannot supply text or media', () => {
  const raw = extractFixture({ state: { note: { noteDetailMap: { [OTHER_ID]: { note: { noteId: OTHER_ID, title: 'Wrong', desc: 'Wrong body', imageList: [{ urlDefault: 'https://sns-img-bd.xhscdn.com/wrong.jpg' }] } } } } } });
  assert.equal(raw.title, '');
  assert.equal(raw.nativeText, '');
  assert.equal(raw.images.length, 0);
  assert.equal(raw.diagnostics.structured_note_count, 0);
});

test('share redirect resolves its target from the canonical page path', () => {
  const raw = extractFixture({ target: '', pathname: `/discovery/item/${NOTE_ID}`, state: { note: { noteDetailMap: { [OTHER_ID]: { note: { title: 'Wrong' } }, [NOTE_ID]: { note: { noteId: NOTE_ID, title: 'Resolved' } } } } } });
  assert.equal(raw.noteId, NOTE_ID);
  assert.equal(raw.title, 'Resolved');
});

test('native text retains paragraphs and internal whitespace through safe rows', () => {
  const body = '第一行  保留空格\n第二行\n\n下一段';
  const raw = extractFixture({ scoped: [fixtureNode(['#detail-desc'], body)] });
  assert.equal(buildSafeAssetRows(buildNoteAssetPlan(raw, NOTE_ID), [], { downloaded: false })[0].native_text, body);
});

test('matching hydration body wins over truncated DOM while DOM fills a missing body', () => {
  const body = '原始第一段\n\n完整第二段  保留空格';
  for (const desc of [body, '']) {
    const raw = extractFixture({
      scoped: [fixtureNode(['#detail-desc'], '原始第一段…展开')],
      state: { note: { noteDetailMap: { [NOTE_ID]: { note: { noteId: NOTE_ID, desc } } } } },
    });
    const rows = buildSafeAssetRows(buildNoteAssetPlan(raw, NOTE_ID), [], { downloaded: false });
    assert.equal(rows[0].native_text, desc || '原始第一段…展开');
  }
});

test('media bind to the matching structured note, never recommendations or global scripts', () => {
  const raw = extractFixture({
    state: { note: { noteDetailMap: { [NOTE_ID]: { note: { noteId: NOTE_ID, imageList: [{ urlDefault: 'https://sns-img-bd.xhscdn.com/target.jpg' }] } } } } },
    outside: [fixtureNode(['script'], 'https://sns-video-bd.xhscdn.com/wrong.mp4'), fixtureNode(['.swiper-slide img', 'img'], '', 'https://sns-img-bd.xhscdn.com/wrong.jpg')],
  });
  assert.deepEqual(Array.from(raw.images), ['https://sns-img-bd.xhscdn.com/target.jpg']);
  assert.equal(raw.videos.length, 0);
});

test('DOM media fallback is restricted to the current note container', () => {
  const raw = extractFixture({
    scoped: [fixtureNode(['.swiper-slide img', 'img'], '', 'https://sns-img-bd.xhscdn.com/target.jpg')],
    outside: [fixtureNode(['.swiper-slide img', 'img'], '', 'https://sns-img-bd.xhscdn.com/wrong.jpg'), fixtureNode(['video[src]'], '', 'https://sns-video-bd.xhscdn.com/wrong.mp4')],
  });
  assert.deepEqual(Array.from(raw.images), ['https://sns-img-bd.xhscdn.com/target.jpg']);
  assert.equal(raw.videos.length, 0);
});

test('a redirect to another canonical note fails closed before producing an asset plan', () => {
  const raw = extractFixture({ pathname: `/explore/${OTHER_ID}`, scoped: [fixtureNode(['#detail-title'], 'Wrong')] });
  assert.equal(raw.targetMismatch, true);
  assert.equal(raw.title, '');
  assert.throws(() => buildNoteAssetPlan(raw, NOTE_ID), /did not match/);
  assert.throws(() => buildNoteAssetPlan({ noteId: OTHER_ID }, NOTE_ID), /did not match/);
});

test('an unresolved share cannot infer its target from singleton hydration', () => {
  const raw = extractFixture({ target: '', pathname: '/a/fixture', state: { note: { noteDetailMap: { [OTHER_ID]: { note: { noteId: OTHER_ID, title: 'Wrong' } } } } } });
  assert.equal(raw.targetUnresolved, true);
  assert.equal(raw.title, '');
  assert.throws(() => buildNoteAssetPlan(raw, ''), /canonical note/);
});

test('hydration cannot contradict the target map key with its internal id', () => {
  const raw = extractFixture({ state: { note: { noteDetailMap: { [NOTE_ID]: { note: { noteId: OTHER_ID, title: 'Wrong', video: { url: 'https://sns-video-bd.xhscdn.com/wrong.mp4' } } } } } } });
  assert.equal(raw.diagnostics.structured_note_count, 0);
  assert.equal(raw.title, '');
  assert.equal(raw.videos.length, 0);
});

test('direct structured note requires matching identity and preserves raw text and repeated speech', () => {
  const note = { noteId: NOTE_ID, desc: '段落一\r\n\r\n段落二  空格', subtitles: [{ text: '重复\n原文' }, { text: '重复\n原文' }], video: { media: { stream: { h264: [{ masterUrl: 'https://sns-video-bd.xhscdn.com/target.mp4' }] } } } };
  const raw = extractFixture({ state: { note: { note } } });
  assert.equal(raw.nativeText, '段落一\n\n段落二  空格');
  assert.equal(raw.nativeTranscript, '重复\n原文\n重复\n原文');
  assert.deepEqual(Array.from(raw.videos), ['https://sns-video-bd.xhscdn.com/target.mp4']);
});

test('missing note container does not promote global detail-looking DOM into target evidence', () => {
  const raw = extractFixture({ container: false, outside: [fixtureNode(['#detail-title', '.title'], 'Wrong'), fixtureNode(['#detail-desc', '.desc'], 'Wrong body'), fixtureNode(['.swiper-slide img', 'img'], '', 'https://sns-img-bd.xhscdn.com/wrong.jpg')] });
  assert.equal(raw.title, '');
  assert.equal(raw.nativeText, '');
  assert.equal(raw.images.length, 0);
});

test('a route with trailing unrelated segments does not establish share target identity', () => {
  const raw = extractFixture({ target: '', pathname: `/explore/${NOTE_ID}/unrelated`, scoped: [fixtureNode(['#detail-title'], 'Wrong')] });
  assert.equal(raw.targetUnresolved, true);
});

test('parse canonical note and profile selectors without retaining queries', () => {
  assert.equal(parseNoteId(`https://www.xiaohongshu.com/discovery/item/${NOTE_ID}?xsec_token=secret`), NOTE_ID);
  assert.equal(parseProfileId('https://www.xiaohongshu.com/user/profile/ffff00000000000000000006?xsec_token=secret'), 'ffff00000000000000000006');
  assert.equal(parseProfileId(`https://www.xiaohongshu.com/user/profile/ffff00000000000000000006/${NOTE_ID}?xsec_token=secret`), null);
});

test('note-assets accepts only signed Xiaohongshu runtime profile selectors', () => {
  assert.equal(isSafeRuntimeSelector(
    'https://www.xiaohongshu.com/user/profile/ffff00000000000000000006?xsec_token=runtime-only',
  ), true);
  assert.equal(isSafeRuntimeSelector(
    'https://www.xiaohongshu.com/user/profile/ffff00000000000000000006',
  ), false);
  assert.equal(isSafeRuntimeSelector(
    'https://xiaohongshu.com.attacker.example/user/profile/ffff00000000000000000006?xsec_token=runtime-only',
  ), false);
});

test('buildNoteAssetPlan preserves ordered images and one video', () => {
  const plan = buildNoteAssetPlan({
    noteId: NOTE_ID,
    title: 'Fixture title',
    nativeText: 'Fixture body',
    images: [
      'https://sns-img-bd.xhscdn.com/cover.jpg',
      'https://sns-img-bd.xhscdn.com/second.jpg',
      'https://sns-img-bd.xhscdn.com/third.jpg',
    ],
    videos: [
      'https://sns-video-bd.xhscdn.com/primary.mp4',
      'https://sns-video-bd.xhscdn.com/duplicate-stream.mp4',
    ],
  }, NOTE_ID);
  assert.deepEqual(plan.assets.map((asset) => asset.filename), [
    'image_001.jpg', 'image_002.jpg', 'image_003.jpg', 'video_001.mp4',
  ]);
  assert.equal(plan.mediaKind, 'mixed');
  assert.equal(plan.imageCount, 3);
  assert.equal(plan.videoCount, 1);
});

test('safe rows expose local relative paths and no signed/CDN/browser secrets', () => {
  const plan = buildNoteAssetPlan({
    noteId: NOTE_ID,
    title: 'title https://example.test/a?xsec_token=secret',
    nativeText: 'body Cookie: private',
    nativeTranscript: 'spoken text',
    images: ['https://sns-img-bd.xhscdn.com/cover.jpg'],
    videos: ['https://sns-video-bd.xhscdn.com/primary.mp4'],
  }, NOTE_ID);
  const rows = buildSafeAssetRows(plan, [
    { index: 1, status: 'success' },
    { index: 2, status: 'success' },
  ], { downloaded: true });
  assert.deepEqual(rows.slice(1).map((row) => row.local_path), ['image_001.jpg', 'video_001.mp4']);
  const output = JSON.stringify(rows);
  for (const forbidden of ['xsec_token', 'secret', 'xhscdn', 'runtime_note_url', 'cookie', 'authorization']) {
    assert.equal(output.toLowerCase().includes(forbidden), false, forbidden);
  }
});

test('video-only plan retains native transcript but never remote URLs in safe rows', () => {
  const plan = buildNoteAssetPlan({
    noteId: NOTE_ID,
    nativeTranscript: 'native subtitle line',
    videos: ['https://sns-video-bd.xhscdn.com/primary.mp4'],
  }, NOTE_ID);
  const rows = buildSafeAssetRows(plan, [], { downloaded: false });
  assert.equal(rows[0].media_kind, 'video');
  assert.equal(rows[0].native_transcript, 'native subtitle line');
  assert.equal(JSON.stringify(rows).includes('sns-video'), false);
});

test('generic runtime errors never echo signed or CDN URLs', () => {
  const error = translateError(new Error(
    'goto failed https://www.xiaohongshu.com/explore/abc?xsec_token=secret https://sns-video-bd.xhscdn.com/private.mp4',
  ));
  const output = String(error.message).toLowerCase();
  for (const forbidden of ['xsec_token', 'secret', 'xhscdn', 'private.mp4']) {
    assert.equal(output.includes(forbidden), false, forbidden);
  }
});

test('typed navigation rejection retains a safe machine code without raw details or hints', () => {
  const error = Object.assign(new Error('Navigation rejected: https://www.xiaohongshu.com/explore/fixture?xsec_token=private'), {
    name: 'BrowserCommandError', hint: 'Cookie: private', code: 'unsafe-secret-code',
  });
  const translated = translateError(error);
  assert.ok(translated instanceof CliError);
  assert.equal(translated.code, 'NAVIGATION_REJECTED');
  assert.match(translated.message, /Navigation rejected/);
  const output = JSON.stringify({ message: translated.message, code: translated.code, hint: translated.hint });
  for (const forbidden of ['http', 'xsec_token', 'private', 'Cookie', 'unsafe-secret-code']) {
    assert.equal(output.includes(forbidden), false, forbidden);
  }
});

test('untyped navigation-looking errors keep the generic sanitized fallback', () => {
  const translated = translateError(new Error('Navigation rejected: https://example.test/?xsec_token=private'));
  assert.ok(translated instanceof CliError);
  assert.equal(translated.code, 'COMMAND_EXEC');
  assert.equal(translated.message.includes('private'), false);
});

test('media allowlist rejects hostname substring tricks', () => {
  const plan = buildNoteAssetPlan({
    noteId: NOTE_ID,
    images: [
      'https://sns-img-bd.xhscdn.com/allowed.jpg',
      'https://xhscdn.attacker.example/blocked.jpg',
      'https://notxhscdn.com/blocked.jpg',
    ],
    videos: [
      'https://sns-video-bd.xhscdn.com/allowed.mp4',
      'https://xiaohongshu.com.attacker.example/blocked.mp4',
    ],
  }, NOTE_ID);
  assert.deepEqual(plan.assets.map((asset) => asset.filename), ['image_001.jpg', 'video_001.mp4']);
});

test('media plan normalizes protocol-relative and http Xiaohongshu assets to https', () => {
  const plan = buildNoteAssetPlan({
    noteId: NOTE_ID,
    images: ['//sns-img-bd.xhscdn.com/relative.jpg', 'http://ci.xiaohongshu.com/plain.jpg'],
  }, NOTE_ID);
  assert.deepEqual(plan.assets.map((asset) => asset.remoteUrl), [
    'https://sns-img-bd.xhscdn.com/relative.jpg',
    'https://ci.xiaohongshu.com/plain.jpg',
  ]);
});

test('empty output is rejected before browser navigation', async () => {
  const command = getRegistry().get('openmd-xiaohongshu/note-assets');
  assert.ok(command);
  await assert.rejects(
    command.func({} as never, { note: NOTE_ID, output: '' }),
    (error: unknown) => error instanceof ArgumentError && error.message.includes('output directory'),
  );
});

test('profile ids are not parsed as note ids', () => {
  assert.equal(parseNoteId('https://www.xiaohongshu.com/user/profile/ffff00000000000000000006?xsec_token=fixture'), null);
  assert.equal(parseNoteId(`https://www.xiaohongshu.com/search_result/${NOTE_ID}?xsec_token=fixture`), NOTE_ID);
});

test('stdin selectors reach navigation unchanged for direct and share URLs', async () => {
  const { spawnSync } = await import('node:child_process');
  const { fileURLToPath } = await import('node:url');
  const { mkdtempSync, rmSync } = await import('node:fs');
  const { tmpdir } = await import('node:os');
  const { join } = await import('node:path');
  const output = mkdtempSync(join(tmpdir(), 'openmd-selector-test-'));
  const script = `
    import assert from 'node:assert/strict';
    import {runInNewContext} from 'node:vm';
    import {getRegistry} from '@jackwener/opencli/registry';
    await import(process.env.OPENMD_TEST_MODULE);
    let navigation = '';
    const page = {
      goto: async (url) => { navigation = url; },
      wait: async () => {},
      evaluate: async (js) => runInNewContext(js, {
        window: {__INITIAL_STATE__: {note: {noteDetailMap: {'${NOTE_ID}': {note: {noteId:'${NOTE_ID}', title:'Fixture', desc:'Fixture body'}}}}}},
        document: {body:{innerText:''}, querySelector: () => null, querySelectorAll: () => []},
        location: {pathname:'/discovery/item/${NOTE_ID}', href:'https://www.xiaohongshu.com/discovery/item/${NOTE_ID}'},
      }),
    };
    const rows = await getRegistry().get('openmd-xiaohongshu/note-assets').func(page, {
      note: process.argv[2], selector_stdin:true, output:process.argv[1], download:false,
    });
    assert.equal(navigation, process.argv[3]);
    assert.equal(rows[0].note_id, '${NOTE_ID}');
    assert.equal(rows[0].native_text, 'Fixture body');
    console.log('PASS');
  `;
  try {
    for (const module of ['./note-assets.ts', './note-assets.js']) {
    for (const selector of [
      `https://www.xiaohongshu.com/search_result/${NOTE_ID}?xsec_token=fixture&xsec_source=pc_search`,
      'https://xhslink.com/a/fixture',
    ]) {
      const result = spawnSync(process.execPath, ['--input-type=module', '-e', script, output, selector.includes('xhslink.com') ? selector : NOTE_ID, selector], {
        cwd: fileURLToPath(new URL('.', import.meta.url)), input: selector, encoding:'utf8',
        env: { ...process.env, OPENMD_TEST_MODULE: module },
      });
      assert.equal(result.status, 0, result.stderr);
      assert.equal(result.stdout.trim(), 'PASS');
    }
    }
  } finally {
    rmSync(output, {recursive:true, force:true});
  }
});
