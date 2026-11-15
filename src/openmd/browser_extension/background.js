// Built-in OpenCLI Browser Bridge (Apache-2.0): lets the bundled opencli read pages
// through this browser, so users install one extension. See vendor/opencli-bridge/VENDOR.md.
import "./vendor/opencli-bridge/background.js";

// Openmd extension service worker.
// Daily flow: click the toolbar icon -> save the current page (or every highlighted tab) to the
// default location. Right-click a selection -> save every link inside it. Jobs run one by one.
const HOST = "com.openmd.host";
const POLL_MS = 2000;
const SETUP_HINT = "还没连上本机 openmd：请在终端运行 openmd browser-setup，然后重新加载插件。";

function native(message) {
  return new Promise((resolve) => {
    chrome.runtime.sendNativeMessage(HOST, message, (reply) => {
      const err = chrome.runtime.lastError;
      if (err) resolve({ ok: false, error: /not found|forbidden/i.test(err.message) ? SETUP_HINT : err.message });
      else resolve(reply || { ok: false, error: "本机 openmd 没有回应" });
    });
  });
}

const isWeb = (url) => /^https?:\/\//.test(url || "");

async function getSettings() {
  const { settings } = await chrome.storage.local.get("settings");
  return { videoMode: "note", ...(settings || { locations: [], defaultId: null, clickMode: "auto" }) };
}

async function applyClickMode() {
  const { clickMode, locations } = await getSettings();
  // Without a popup, a click fires action.onClicked and saves directly.
  const direct = clickMode === "auto" && locations.length > 0;
  await chrome.action.setPopup({ popup: direct ? "" : "popup.html" });
  await chrome.action.setTitle({ title: direct ? "保存到 Openmd（默认位置）。按住 Ctrl 多选标签页可一次保存多个" : "保存到 Openmd" });
}

async function seedSettings() {
  const current = await getSettings();
  if (current.locations.length) return;
  const ping = await native({ type: "ping" });
  if (!ping.ok || !ping.save_to) return;
  const id = crypto.randomUUID();
  await chrome.storage.local.set({ settings: { locations: [{ id, name: "默认", path: ping.save_to }], defaultId: id, clickMode: "auto" } });
}

// ---------------------------------------------------------------- tracking

async function tracked() {
  const { jobs = {}, batches = {} } = await chrome.storage.local.get(["jobs", "batches"]);
  return { jobs, batches };
}

async function showRunning(jobs) {
  const n = Object.keys(jobs).length;
  await chrome.action.setBadgeBackgroundColor({ color: "#1d5c49" });
  await chrome.action.setBadgeText({ text: n > 1 ? String(n) : n ? "…" : "" });
}

async function flash(text, color) {
  await chrome.action.setBadgeBackgroundColor({ color });
  await chrome.action.setBadgeText({ text });
  setTimeout(async () => showRunning((await tracked()).jobs), 6000);
}

function notify(title, message) {
  chrome.notifications.create(`openmd:${Date.now()}`, {
    type: "basic", iconUrl: "icons/icon-128.png", title, message: String(message || "").slice(0, 300),
  });
}

function fail(title, message) {
  flash("!", "#b3261e");
  notify(title, message);
}

async function locationFor(locationId) {
  const settings = await getSettings();
  return settings.locations.find((l) => l.id === (locationId || settings.defaultId)) || settings.locations[0];
}

async function track(jobs, batch, location) {
  const state = await tracked();
  for (const job of jobs) state.jobs[job.id] = { title: job.title || job.url, batch };
  state.batches[batch] = { total: jobs.length, failed: [], location };
  await chrome.storage.local.set(state);
  await showRunning(state.jobs);
  poll();
}

// ---------------------------------------------------------------- saving

// video: "note" (just mention the video), "text" (+ local transcript), "file" (+ the video file).
async function saveMany(items, locationId, video) {
  const web = items.filter((item) => isWeb(item.url));
  if (!web.length) return fail("没法保存", "只能保存普通网页（http / https）。");
  const location = await locationFor(locationId);
  if (!location) {
    chrome.runtime.openOptionsPage();
    return fail("先设置一个保存位置", "已打开设置页。");
  }
  video = video || (await getSettings()).videoMode || "note";
  const titles = Object.fromEntries(web.map((item) => [item.url, item.title]));
  const reply = web.length === 1
    ? await native({ type: "save", url: web[0].url, save_to: location.path, video })
    : await native({ type: "save_many", urls: web.map((item) => item.url), save_to: location.path, video });
  if (!reply.ok) return fail("没能开始保存", reply.error);
  const jobs = (reply.jobs || [reply.job]).map((job) => ({ ...job, title: titles[job.url] }));
  await track(jobs, reply.batch || jobs[0].id, location.name);
  return { ok: true, count: jobs.length, location: location.name };
}

async function highlightedTabs(tab) {
  const tabs = await chrome.tabs.query({ highlighted: true, windowId: tab.windowId });
  // Clicking the icon on a tab outside the highlighted set saves just that tab.
  return tabs.length > 1 && tabs.some((t) => t.id === tab.id) ? tabs : [tab];
}

async function linksInSelection(tabId) {
  const [{ result } = {}] = await chrome.scripting.executeScript({
    target: { tabId },
    func: () => {
      const selection = window.getSelection();
      const found = [];
      for (let i = 0; i < selection.rangeCount; i += 1) {
        const range = selection.getRangeAt(i);
        const root = range.commonAncestorContainer.nodeType === 1 ? range.commonAncestorContainer : range.commonAncestorContainer.parentElement;
        const anchors = root.closest("a[href]") ? [root.closest("a[href]")] : [...root.querySelectorAll("a[href]")];
        for (const a of anchors) if (range.intersectsNode(a)) found.push({ url: a.href, title: (a.innerText || a.title || a.href).trim().slice(0, 120) });
      }
      return found;
    },
  });
  const seen = new Set();
  return (result || []).filter((item) => /^https?:\/\//.test(item.url) && !seen.has(item.url) && seen.add(item.url));
}

// ---------------------------------------------------------------- polling

let polling = false;
async function poll() {
  if (polling) return;
  polling = true;
  try {
    for (;;) {
      const state = await tracked();
      const ids = Object.keys(state.jobs);
      if (!ids.length) break;
      const reply = await native({ type: "statuses", ids });
      if (reply.ok) {
        for (const id of ids) {
          const job = reply.jobs[id] || { state: "unknown" };
          if (!["done", "failed", "cancelled", "unknown"].includes(job.state)) continue;
          const meta = state.jobs[id];
          delete state.jobs[id];
          const batch = state.batches[meta.batch];
          if (batch && job.state !== "done" && job.state !== "cancelled") batch.failed.push(`${meta.title}：${job.error || "状态未知"}`);
          const remaining = Object.values(state.jobs).some((m) => m.batch === meta.batch);
          if (batch && !remaining) {
            delete state.batches[meta.batch];
            if (batch.failed.length) {
              const head = batch.total > 1 ? `${batch.total} 个里有 ${batch.failed.length} 个没保存成功` : "没能保存";
              fail(head, batch.failed.slice(0, 3).join("\n"));
            } else {
              await flash("✓", "#2c8a5f");
            }
          }
        }
        await chrome.storage.local.set(state);
        await showRunning(state.jobs);
      }
      await new Promise((r) => setTimeout(r, POLL_MS));
    }
  } finally {
    polling = false;
  }
}

// ---------------------------------------------------------------- entry points

chrome.action.onClicked.addListener(async (tab) => {
  const tabs = await highlightedTabs(tab);
  saveMany(tabs.map((t) => ({ url: t.url, title: t.title })));
});

chrome.runtime.onInstalled.addListener(async () => {
  chrome.contextMenus.removeAll(() => {
    chrome.contextMenus.create({ id: "save-page", title: "用 Openmd 保存此页", contexts: ["page"] });
    chrome.contextMenus.create({ id: "save-link", title: "用 Openmd 保存此链接", contexts: ["link"] });
    chrome.contextMenus.create({ id: "save-selection", title: "用 Openmd 保存选中的链接", contexts: ["selection"] });
    // Posts with an attached video (X for now): opt in per save, so a plain click stays fast.
    const posts = ["*://x.com/*", "*://*.x.com/*", "*://twitter.com/*", "*://*.twitter.com/*"];
    chrome.contextMenus.create({ id: "save-video-text", title: "用 Openmd 保存（含视频文字版）", contexts: ["page", "link", "video"], documentUrlPatterns: posts });
    chrome.contextMenus.create({ id: "save-video-file", title: "用 Openmd 保存（含视频文字版和视频文件）", contexts: ["page", "link", "video"], documentUrlPatterns: posts });
  });
  await seedSettings();
  await applyClickMode();
  if (!(await getSettings()).locations.length) chrome.runtime.openOptionsPage();
});

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  if (info.menuItemId === "save-link" && info.linkUrl) return saveMany([{ url: info.linkUrl, title: info.linkUrl }]);
  if (info.menuItemId === "save-page" && tab?.url) return saveMany([{ url: tab.url, title: tab.title }]);
  if (info.menuItemId === "save-video-text" || info.menuItemId === "save-video-file") {
    const url = info.linkUrl || info.pageUrl || tab?.url;
    const video = info.menuItemId === "save-video-file" ? "file" : "text";
    return saveMany([{ url, title: info.linkUrl ? url : tab?.title }], null, video);
  }
  if (info.menuItemId === "save-selection" && tab?.id) {
    try {
      const links = await linksInSelection(tab.id);
      if (!links.length) return fail("选中的区域里没有链接", "拖选一段包含链接的内容（比如课程目录、合集列表）再试。");
      return saveMany(links);
    } catch (err) {
      return fail("读不到这个页面的选中内容", String(err?.message || err));
    }
  }
});

chrome.storage.onChanged.addListener((changes) => {
  if (changes.settings) applyClickMode();
});

chrome.runtime.onMessage.addListener((msg, _sender, reply) => {
  if (msg?.cmd === "save") { saveMany(msg.items, msg.locationId).then(reply); return true; }
  if (msg?.cmd === "native") { native(msg.message).then(reply); return true; }
  return false;
});

chrome.runtime.onStartup.addListener(async () => { await applyClickMode(); poll(); });
applyClickMode();
poll();
