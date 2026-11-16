// Xiaoe (小鹅通) catalog — list chapters + sections of a course / column
// page (`h5.xet.citv.cn`).
//
// Replaces the legacy `pipeline:[]` form. The in-browser extraction logic
// (Vue store walking + auto-scroll-to-load + Vue child traversal) is retained
// with resource-ID-based column type labels — Xiaoe's pages are SPA-rendered and Vue's private API
// (`__vue__`, `$store`, `$children`, `chapter_box.__vue__`) is the only
// stable hook we have. JSDOM cannot reproduce the Vue runtime tree, so
// reorganising the IIFE without live verify would be silent-failure risk.
//
// What changes:
//   - `func` form + `Strategy.COOKIE` + `browser:true`.
//   - Typed errors: `ArgumentError` on missing url; `EmptyResultError`
//     when the IIFE yields zero rows (almost always means the cookie
//     expired or the URL is not a course page); `CommandExecutionError`
//     when `page.evaluate` rejects.
//   - Pure helpers (`typeLabel`, `columnTypeLabel`, `buildItemUrl`, `chapterUrlPath`)
//     are module-level exports and are embedded into the in-page IIFE
//     via `${fn.toString()}` so the live and the test path share one
//     source of truth.

import { cli, Strategy } from '@jackwener/opencli/registry';
import { CommandExecutionError, EmptyResultError } from '@jackwener/opencli/errors';
import { requireXiaoePageUrl } from './content.js';

// resource_type → human label. 1=图文 2=直播 3=音频 4=视频 6=专栏 8=大专栏.
// Returns the raw `String(t)` when the type is unknown (e.g. xiaoe rolls
// out a new resource type) — never silently swallows it.
export function typeLabel(t) {
    const map = { 1: '图文', 2: '直播', 3: '音频', 4: '视频', 6: '专栏', 8: '大专栏' };
    return map[Number(t)] || String(t || '');
}

// Column lists use a different resource_type enum from chapter sections.
// The resource identity remains stable across both schemas.
export function columnTypeLabel(item) {
    const prefix = String(item.resource_id || '').split('_')[0];
    const map = { i: '图文', l: '直播', a: '音频', v: '视频' };
    return map[prefix] || typeLabel(item.resource_type || item.chapter_type);
}

// Return the component that owns column pagination, rather than a copied or
// still-empty array higher in the Vue tree. No state is changed by this walk.
export function findColumnCatalog(root) {
    const seen = new WeakSet();
    let count = 0;
    let best = null;
    function walk(vm, depth) {
        if (!vm || depth > 10 || seen.has(vm) || count++ > 3000) return;
        seen.add(vm);
        const data = vm.$data || {};
        for (const key of ['SingleItemList', 'columnList', 'chapterChildren']) {
            const items = data[key];
            if (!Array.isArray(items)) continue;
            if (items.length && !items.some((item) => item?.resource_id)) continue;
            const canLoad = key === 'SingleItemList' && (typeof vm.getColumnList === 'function' || typeof vm.loadMoreSingleItem === 'function');
            const score = canLoad ? 2 : 1;
            if (!best || score > best.score || (score === best.score && items.length > best.items.length)) {
                best = { vm, items, score };
            }
        }
        for (const child of vm.$children || []) walk(child, depth + 1);
    }
    walk(root, 0);
    return best;
}

// Resolve a relative `jump_url` / `h5_url` / `url` against the page's
// origin. Returns '' when the item has no URL field at all.
export function buildItemUrl(item, origin) {
    const u = item.jump_url || item.h5_url || item.url || '';
    if (!u) return '';
    return u.startsWith('http') ? u : (origin + u);
}

// chapter_type → URL path for the section reader. Xiaoe routes chapter
// types to different player paths; returning `undefined` for unknown
// types lets the caller decide whether to emit `''` instead of guessing
// a bad URL.
export function chapterUrlPath(chType) {
    const map = { 1: '/v1/course/text/', 2: '/v2/course/alive/', 3: '/v1/course/audio/', 4: '/v1/course/video/' };
    return map[Number(chType)];
}

function isRecoverableNavigateDrop(error) {
    const message = error instanceof Error ? error.message : String(error);
    return message.includes('Browser connection dropped after the navigate command was dispatched');
}

async function reselectTabAfterNavigation(page, url) {
    if (typeof page.tabs !== 'function' || typeof page.selectTab !== 'function') return;
    let targetPath = '';
    try {
        targetPath = new URL(url).pathname;
    } catch {
        targetPath = '';
    }
    const tabs = await page.tabs();
    if (!Array.isArray(tabs)) return;
    const match = tabs.find((tab) => {
        const tabUrl = typeof tab.url === 'string' ? tab.url : '';
        try {
            const candidate = new URL(tabUrl);
            const target = new URL(url);
            return candidate.origin === target.origin && candidate.pathname === targetPath;
        } catch {
            return false;
        }
    });
    const pageId = match && (match.page || match.targetId || match.id || match.index);
    if (pageId !== undefined && pageId !== null) {
        await page.selectTab(pageId);
    }
}

function isTransientNavigationError(error) {
    const message = error instanceof Error ? error.message : String(error);
    return message.includes('Execution context was destroyed')
        || message.includes('Cannot find context')
        || message.includes('target closed')
        || message.includes('Browser connection dropped after the exec command was dispatched')
        || message.includes('Browser connection dropped after the navigate command was dispatched');
}

function sleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
}

async function reuseExistingCatalogTab(page, url) {
    if (typeof page.tabs !== 'function') return false;
    // tabs() is scoped to this OpenCLI session, not all user Chrome tabs.
    const tabs = await page.tabs();
    if (!Array.isArray(tabs)) return false;
    const match = tabs.find((tab) => tab.url === url);
    if (!match) return false;
    // Read the current tab before selectTab: explicitly borrowed user sessions
    // allow evaluation but deliberately prohibit tab selection and mutation.
    const currentUrl = await page.evaluate('location.href');
    if (currentUrl === url) return true;
    const pageId = match.page ?? match.targetId ?? match.id ?? match.index;
    if (pageId === undefined || pageId === null || typeof page.selectTab !== 'function') return false;
    await page.selectTab(pageId);
    return true;
}

async function navigateByLocationAssign(page, url) {
    const assignScript = `(() => {
  var targetUrl = ${JSON.stringify(url)};
  if (location.href !== targetUrl) location.href = targetUrl;
  return true;
})()`;
    try {
        await page.evaluate(assignScript);
    } catch (error) {
        if (!isTransientNavigationError(error)) throw error;
    }
    await reselectTabAfterNavigation(page, url);
}

async function waitForCatalogReady(page, url) {
    let targetPath = '';
    let targetHost = '';
    try {
        const target = new URL(url);
        targetPath = target.pathname;
        targetHost = target.hostname;
    } catch {
        targetPath = '';
    }
    for (let attempt = 0; attempt < 40; attempt++) {
        await sleep(500);
        try {
            const state = await page.evaluate(`(() => {
  ${findColumnCatalog.toString()}
  var root = document.querySelector('#app')?.__vue__;
  var store = root?.$store;
  var type = Number(store?.state?.coreInfo?.resource_type || 0);
  var column = (type === 6 || type === 8) ? findColumnCatalog(root) : null;
  return {
  url: location.href,
  authRequired: /\\/(?:login|auth|authorize)(?:\\/|$)/i.test(location.pathname)
    || /请先登录|登录后(?:查看|学习)|立即登录/.test(document.body?.innerText || ''),
  ready: !!store && type > 0 && ((type === 6 || type === 8) ? !!column?.items.length : !!(document.querySelector('#catalogue_scroll_list')
    || document.querySelector('.chapter_box')
    || document.querySelector('#app')))
  };
})()`);
            const currentUrl = state && typeof state.url === 'string' ? state.url : '';
            const current = currentUrl ? new URL(currentUrl) : null;
            if (state?.authRequired) {
                throw new Error('login is required; sign in to this Xiaoe shop in Chrome, then retry the catalog command');
            }
            if (state && state.ready && current?.hostname === targetHost && (!targetPath || current.pathname === targetPath)) return true;
        } catch (error) {
            if (!isTransientNavigationError(error)) throw error;
        }
    }
    return false;
}

export function resolveCatalogUrl(value) {
    const normalized = requireXiaoePageUrl(value, 'catalog');
    const original = new URL(value.trim());
    return /\.h5\.xet\.pomoho\.com$/i.test(original.hostname) ? original.toString() : normalized;
}

export function buildCatalogScript() {
    return `(async () => {
  ${typeLabel.toString()}
  ${columnTypeLabel.toString()}
  ${findColumnCatalog.toString()}
  ${buildItemUrl.toString()}
  ${chapterUrlPath.toString()}
  var el, store, coreInfo, resourceType;
  for (var hydrate = 0; hydrate < 40; hydrate++) {
    el = document.querySelector('#app');
    store = el && el.__vue__ && el.__vue__.$store;
    coreInfo = store && store.state.coreInfo || {};
    resourceType = Number(coreInfo.resource_type || 0);
    if (store && resourceType > 0) break;
    await new Promise(function(r) { setTimeout(r, 250); });
  }
  if (!store || !resourceType) throw new Error('course metadata did not finish loading; retry the catalog command');
  var origin = window.location.origin;
  var courseName = coreInfo.resource_name || '';

  function clickTab(name) {
    var tabs = document.querySelectorAll('span, div');
    for (var i = 0; i < tabs.length; i++) {
      if (tabs[i].children.length === 0 && tabs[i].textContent.trim() === name) {
        tabs[i].click(); return;
      }
    }
  }

  clickTab('目录');
  await new Promise(function(r) { setTimeout(r, 2000); });

  // ===== 专栏 / 大专栏 =====
  if (resourceType === 6 || resourceType === 8) {
    var column, columnItems = [], expected = 0, complete = false, previousCount = -1, stable = 0;
    var loadDeadline = Date.now() + 25000;
    for (var load = 0; load < 40 && Date.now() < loadDeadline; load++) {
      column = findColumnCatalog(el.__vue__);
      var data = column && column.vm.$data || {};
      var ids = new Set();
      columnItems = (column && column.items || []).filter(function(item) {
        if (!item || !item.resource_id || !/^[pvlai]_/.test(item.resource_id) || ids.has(item.resource_id)) return false;
        ids.add(item.resource_id);
        return true;
      });
      expected = Math.max(expected, Number(data.total) || 0);
      if (columnItems.length && ((expected > 0 && columnItems.length >= expected) || (data.ifEnd === true && !expected))) {
        complete = true; break;
      }
      if (data.ifEnd === true && expected > columnItems.length) break;
      stable = columnItems.length === previousCount ? stable + 1 : 0;
      previousCount = columnItems.length;
      // This method is exposed by the observed SingleItemList component and
      // performs the same read-only next-page request as scrolling its list.
      var loadNext = column && (typeof column.vm.getColumnList === 'function'
        ? column.vm.getColumnList : column.vm.loadMoreSingleItem);
      if (typeof loadNext === 'function') {
        if (!data.isLoading) {
          await Promise.race([
            Promise.resolve(loadNext.call(column.vm)),
            new Promise(function(r) { setTimeout(r, 1500); })
          ]);
        }
      } else {
        window.scrollTo(0, Math.max(0, document.body.scrollHeight - 1));
        window.scrollTo(0, document.body.scrollHeight);
        var targets = document.querySelectorAll('.scroll-view, .list-wrap, .scroller, #app, #catalogue_scroll_list, .van-list');
        for (var st = 0; st < targets.length; st++) {
          if (targets[st].scrollHeight > targets[st].clientHeight) targets[st].scrollTop = targets[st].scrollHeight;
        }
        if (!expected && columnItems.length && stable >= 4) { complete = true; break; }
      }
      await new Promise(function(r) { setTimeout(r, 500); });
    }
    if (!complete) {
      throw new Error('catalog pagination incomplete: loaded ' + columnItems.length + (expected ? ' of ' + expected : '') + ' resources; retry after the directory finishes loading');
    }
    return columnItems.map(function(item, index) {
      return {
        ch: 1, chapter: courseName, no: index + 1,
        title: item.resource_title || item.title || item.chapter_title || '',
        type: columnTypeLabel(item), resource_id: item.resource_id,
        url: buildItemUrl(item, origin),
        status: item.finished_state === 1 ? '已完成' : (item.resource_count ? item.resource_count + '节' : '')
      };
    });
  }

  function getScrollTargets() {
    return document.querySelectorAll('.scroll-view, .list-wrap, .scroller, #app');
  }
  function getMaxScrollHeight(scrollers) {
    var maxHeight = document.body.scrollHeight;
    for (var i = 0; i < scrollers.length; i++) {
      if (scrollers[i].scrollHeight > maxHeight) maxHeight = scrollers[i].scrollHeight;
    }
    return maxHeight;
  }

  // 模拟滚动以实现动态加载
  var prevMaxScrollHeight = 0;
  for (var sc = 0; sc < 20; sc++) {
    window.scrollTo(0, 999999);
    var scrollers = getScrollTargets();
    for(var si = 0; si < scrollers.length; si++) {
      if(scrollers[si].scrollHeight > scrollers[si].clientHeight) scrollers[si].scrollTop = scrollers[si].scrollHeight;
    }
    await new Promise(function(r) { setTimeout(r, 800); });

    // 点击可能存在的下拉/加载更多
    var moreTabs = document.querySelectorAll('button, [role="button"], span, p');
    for (var bi = 0; bi < moreTabs.length; bi++) {
      if (moreTabs[bi].children && moreTabs[bi].children.length > 2) continue;
      var t = ((moreTabs[bi].innerText || moreTabs[bi].textContent) || '').trim();
      if (t.length > 20) continue;
      if ((t === '点击加载更多' || t === '展开更多' || t === '加载更多') && moreTabs[bi].clientHeight > 0) {
        try { moreTabs[bi].click(); } catch(e){}
      }
    }

    var maxScrollHeight = getMaxScrollHeight(getScrollTargets());
    if (sc > 3 && maxScrollHeight === prevMaxScrollHeight) break;
    prevMaxScrollHeight = maxScrollHeight;
  }
  await new Promise(function(r) { setTimeout(r, 1000); });

  // ===== 普通课程 =====
  var chapters = document.querySelectorAll('.chapter_box');
  for (var ci = 0; ci < chapters.length; ci++) {
    var vue = chapters[ci].__vue__;
    if (vue && typeof vue.getSecitonList === 'function' && (!vue.isShowSecitonsList || !vue.chapterChildren.length)) {
      if (vue.isShowSecitonsList) vue.isShowSecitonsList = false;
      try { vue.getSecitonList(); } catch(e) {}
      await new Promise(function(r) { setTimeout(r, 1500); });
    }
  }
  await new Promise(function(r) { setTimeout(r, 3000); });

  var result = [];
  chapters = document.querySelectorAll('.chapter_box');
  for (var cj = 0; cj < chapters.length; cj++) {
    var v = chapters[cj].__vue__;
    if (!v) continue;
    var chTitle = (v.chapterItem && v.chapterItem.chapter_title) || '';
    var children = v.chapterChildren || [];
    for (var ck = 0; ck < children.length; ck++) {
      var child = children[ck];
      var resId = child.resource_id || child.chapter_id || '';
      var chType = child.chapter_type || child.resource_type || 0;
      var urlPath = chapterUrlPath(chType);
      result.push({
        ch: cj + 1,
        chapter: chTitle,
        no: ck + 1,
        title: child.chapter_title || child.resource_title || '',
        type: typeLabel(chType),
        resource_id: resId,
        url: urlPath ? origin + urlPath + resId + '?type=2' : '',
        status: child.is_finish === 1 ? '已完成' : (child.learn_progress > 0 ? child.learn_progress + '%' : '未学'),
      });
    }
  }
  return result;
})()`;
}

async function getXiaoeCatalog(page, args) {
    const url = resolveCatalogUrl(args.url);
    let rows;
    try {
        // Xiaoe course pages often keep analytics / long-polling requests open.
        // Waiting for full "load" can hang until OpenCLI aborts even though the
        // catalog DOM is already visible. Let the in-page extractor do its own
        // waits, scrolls, and "load more" clicks after the SPA root appears.
        // OpenCLI currently supports waitUntil: "load" | "none"; do not use
        // "domcontentloaded" here because it still follows the load path.
        if (!await reuseExistingCatalogTab(page, url)) {
            if (/\.h5\.xet\.pomoho\.com$/i.test(new URL(url).hostname)) {
                await page.goto(url, { waitUntil: 'none' });
            } else {
                await navigateByLocationAssign(page, url);
            }
        }
        if (!await waitForCatalogReady(page, url)) {
            const diagnostic = await page.evaluate(`(() => {
              const core = document.querySelector('#app')?.__vue__?.$store?.state?.coreInfo || {};
              return { host: location.hostname, path: location.pathname, resourceId: core.resource_id || '', type: core.resource_type || 0, state: document.readyState };
            })()`).catch(() => ({ state: 'unavailable' }));
            throw new Error('course page did not render on the requested host: ' + JSON.stringify(diagnostic));
        }
        rows = await page.evaluate(buildCatalogScript());
    } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        throw new CommandExecutionError(
            `Failed to read xiaoe catalog: ${message}`,
            'page may not have rendered or auth may be required',
        );
    }
    if (!Array.isArray(rows) || rows.length === 0) {
        throw new EmptyResultError(
            'xiaoe/catalog',
            'No catalog rows extracted — the URL may not be a course page or the login session has expired',
        );
    }
    return rows;
}

export const catalogCommand = cli({
    site: 'xiaoe',
    name: 'catalog',
    access: 'read',
    description: '小鹅通课程目录（支持普通课程、专栏、大专栏）',
    domain: 'h5.xet.citv.cn',
    navigateBefore: false,
    strategy: Strategy.COOKIE,
    browser: true,
    args: [
        { name: 'url', required: true, positional: true, help: '课程页面 URL' },
    ],
    columns: ['ch', 'chapter', 'no', 'title', 'type', 'resource_id', 'url', 'status'],
    func: getXiaoeCatalog,
});

export const __test__ = {
    buildCatalogScript,
};
