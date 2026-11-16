import { cli, Strategy } from '@jackwener/opencli/registry';
import { requireXiaoePageUrl } from './content.js';
cli({
    site: 'xiaoe',
    name: 'play-url',
    access: 'read',
    description: '小鹅通视频/音频/直播回放 M3U8 播放地址',
    domain: 'h5.xet.citv.cn',
    strategy: Strategy.COOKIE,
    args: [
        { name: 'url', required: true, positional: true, help: '小节页面 URL' },
    ],
    columns: ['title', 'resource_id', 'm3u8_url', 'candidate_media_urls', 'duration_sec', 'method'],
    browser: true,
    navigateBefore: false,
    async func(page, args) {
      const started = performance.now();
      await page.goto(requireXiaoePageUrl(args.url, 'play-url'), {waitUntil: 'domcontentloaded', settleMs: 0});
      const navigation = (performance.now() - started) / 1000;
      const rows = await page.evaluate(`(async () => {
  var pageUrl = window.location.href;
  var origin = window.location.origin;
  var resourceId = (pageUrl.match(/[val]_[a-f0-9]+/) || [])[0] || '';
  var productId = (pageUrl.match(/product_id=([^&]+)/) || [])[1] || '';
  var appId = (origin.match(/(app[a-z0-9]+)\\./) || [])[1] || '';
  // content_page carries the resource/product IDs in a base64url JSON path.
  // Without them the video detail API is skipped and generic page media may
  // select a short preview instead of this purchased resource.
  var encodedPage = new URL(pageUrl).pathname.match(/\\/content_page\\/([^/]+)/);
  if (encodedPage) {
    try {
      var encoded = encodedPage[1].replace(/-/g, '+').replace(/_/g, '/');
      encoded += '='.repeat((4 - encoded.length % 4) % 4);
      var pageData = JSON.parse(atob(encoded));
      if (!resourceId && /^[val]_[a-f0-9]+$/.test(pageData.resource_id || '')) resourceId = pageData.resource_id;
      if (!productId && /^p_[a-f0-9]+$/.test(pageData.product_id || '')) productId = pageData.product_id;
    } catch(e) {}
  }
  var isLive = resourceId.startsWith('l_') || pageUrl.includes('/alive/');
  var m3u8Url = '', method = '', title = document.title, duration = 0;
  var candidateMediaUrls = [];

  // 深度搜索 Vue 组件树找 M3U8
  function searchVueM3u8() {
    var el = document.querySelector('#app');
    if (!el || !el.__vue__) return '';
    var walk = function(vm, d) {
      if (!vm || d > 10) return '';
      var data = vm.$data || {};
      for (var k in data) {
        if (k[0] === '_' || k[0] === '$') continue;
        var v = data[k];
        if (typeof v === 'string' && v.includes('.m3u8')) return v;
        if (typeof v === 'object' && v) {
          try {
            var s = JSON.stringify(v);
            var m = s.match(/https?:[^"]*\\.m3u8[^"]*/);
            if (m) return m[0].replace(/\\\\\\//g, '/');
          } catch(e) {}
        }
      }
      if (vm.$children) {
        for (var c = 0; c < vm.$children.length; c++) {
          var f = walk(vm.$children[c], d + 1);
          if (f) return f;
        }
      }
      return '';
    };
    return walk(el.__vue__, 0);
  }

  function normalizeMediaUrl(value) {
    return String(value || '')
      .replace(/&amp;/g, '&')
      .replace(/\\\\u0026/g, '&')
      .replace(/\\\\\\//g, '/');
  }

  function isUsableM3u8(value) {
    return typeof value === 'string'
      && /\\.m3u8(\\?|$)/i.test(value)
      && !/\\/confusion_index\\//i.test(value);
  }

  function collectLookbackUrls(payload) {
    var data = payload && payload.data;
    if (!Array.isArray(data) && data && Array.isArray(data.list)) data = data.list;
    if (!Array.isArray(data)) return [];

    var urls = [];
    var seen = {};
    var lines = data.slice().sort(function(a, b) { return Number(!!b.default) - Number(!!a.default); });
    for (var li = 0; li < lines.length; li++) {
      var sharpness = Array.isArray(lines[li].line_sharpness) ? lines[li].line_sharpness : [];
      sharpness = sharpness.slice().sort(function(a, b) { return Number(!!b.default) - Number(!!a.default); });
      for (var si = 0; si < sharpness.length; si++) {
        var url = normalizeMediaUrl(sharpness[si] && sharpness[si].url);
        if (isUsableM3u8(url) && !seen[url]) {
          seen[url] = true;
          urls.push(url);
        }
      }
    }
    return urls;
  }

  // ===== 视频课: detail_info → getPlayUrl =====
  if (!isLive && resourceId.startsWith('v_')) {
    try {
      var detailRes = await fetch(origin + '/xe.course.business.video.detail_info.get/2.0.0', {
        method: 'POST', credentials: 'include',
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
        body: new URLSearchParams({
          'bizData[resource_id]': resourceId,
          'bizData[product_id]': productId || resourceId,
          'bizData[opr_sys]': 'MacIntel',
        }),
      });
      var detail = await detailRes.json();
      var vi = (detail.data || {}).video_info || {};
      title = vi.file_name || title;
      duration = vi.video_length || 0;
      if (vi.play_sign) {
        var userId = vi.user_id || ''; // Session remains browser-managed; never read cookies.
        var playRes = await fetch(origin + '/xe.material-center.play/getPlayUrl', {
          method: 'POST', credentials: 'include',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            org_app_id: appId, app_id: vi.material_app_id || appId,
            user_id: userId, play_sign: [vi.play_sign],
            play_line: 'A', opr_sys: 'MacIntel',
          }),
        });
        var playData = await playRes.json();
        if (playData.code === 0 && playData.data) {
          var matches = JSON.stringify(playData.data).match(/https?:[^"]*\\.(?:m3u8|mp3|m4a|aac|mp4)(?:\\?[^"]*)?/g) || [];
          for (var mi = 0; mi < matches.length; mi++) {
            var candidate = normalizeMediaUrl(matches[mi]);
            if (/\\.(?:m3u8|mp3|m4a|aac|mp4)(\\?|$)/i.test(candidate) && !/\\/confusion_index\\//i.test(candidate) && candidateMediaUrls.indexOf(candidate) < 0) candidateMediaUrls.push(candidate);
          }
          if (candidateMediaUrls.length) { m3u8Url = candidateMediaUrls[0]; method = 'api_direct'; }
        }
      }
    } catch(e) {}
  }

  // ===== 直播回放: 优先取普通 lookback playlist，避开 confusion_index 混淆索引 =====
  if (!m3u8Url && isLive && appId && resourceId) {
    try {
      var lookbackRes = await fetch(origin + '/_alive/v3/get_lookback_list?app_id=' + encodeURIComponent(appId) + '&alive_id=' + encodeURIComponent(resourceId) + '&protection=0', {
        credentials: 'include',
      });
      var lookbackData = await lookbackRes.json();
      var lookbackUrls = collectLookbackUrls(lookbackData);
      if (lookbackUrls.length) {
        candidateMediaUrls = lookbackUrls;
        m3u8Url = lookbackUrls[0];
        method = 'alive_lookback_api';
      }
    } catch(e) {}
  }

  // ===== 兜底: Performance API + Vue 搜索轮询 =====
  if (!m3u8Url) {
    for (var attempt = 0; attempt < 30; attempt++) {
      var entries = performance.getEntriesByType('resource');
      for (var i = 0; i < entries.length; i++) {
        var perfUrl = normalizeMediaUrl(entries[i].name);
        if (isUsableM3u8(perfUrl)) { m3u8Url = perfUrl; method = 'perf_api'; break; }
      }
      if (!m3u8Url) {
        var vueUrl = normalizeMediaUrl(searchVueM3u8());
        if (isUsableM3u8(vueUrl)) { m3u8Url = vueUrl; method = 'vue_search'; }
      }
      if (m3u8Url) break;
      await new Promise(function(r) { setTimeout(r, 500); });
    }
  }

  if (!duration) {
    var vid = document.querySelector('video'), aud = document.querySelector('audio');
    if (vid && vid.duration && !isNaN(vid.duration)) duration = Math.round(vid.duration);
    if (aud && aud.duration && !isNaN(aud.duration)) duration = Math.round(aud.duration);
  }

  if (m3u8Url && candidateMediaUrls.indexOf(m3u8Url) < 0) candidateMediaUrls.push(m3u8Url);
  return [{ title: title, resource_id: resourceId, m3u8_url: m3u8Url, candidate_media_urls: candidateMediaUrls, duration_sec: duration, method: method }];
})()
`);
      for (const row of rows || []) row.navigation_seconds = navigation;
      return rows;
    },
});
