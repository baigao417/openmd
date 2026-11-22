// Built-in OpenCLI Browser Bridge (Apache-2.0): lets the bundled opencli read pages
// through this browser, so users install one extension. See vendor/opencli-bridge/VENDOR.md.
import "./vendor/opencli-bridge/background.js";

// Openmd extension service worker.
// Daily flow: click the toolbar icon -> save the current page (or every highlighted tab) to the
// default location. Right-click a selection -> save every link inside it. Jobs run one by one.
const HOST = "com.openmd.host";
const PROTOCOL_VERSION = 1;
const POLL_MS = 2000;
const SETUP_HINT = "请运行 openmd browser-setup 后重新加载扩展。";

function native(message) {
  return new Promise((resolve) => {
    chrome.runtime.sendNativeMessage(HOST, message, (reply) => {
      const err = chrome.runtime.lastError;
      if (err) resolve({ ok: false, error: `${SETUP_HINT}（${err.message}）` });
      else resolve(reply || { ok: false, error: "本机 openmd 没有回应" });
    });
  });
}

// The update record survives MV3 worker suspension and runtime.reload(). Keep
// every attempted content hash, so a broken/mismatched copy cannot reload-loop.
const UPDATE_ALARM = "openmd-extension-update";
let activeSaves = 0;
let saveEpoch = 0;
let updateChecking = null;
let updateStateTail = Promise.resolve();

function withUpdateState(action) {
  const result = updateStateTail.then(action);
  updateStateTail = result.catch(() => {});
  return result;
}

async function saving(action) {
  activeSaves += 1;
  saveEpoch += 1;
  try { return await action(); }
  finally {
    activeSaves -= 1;
    await maybeReloadExtension();
  }
}

function maybeReloadExtension() {
  return withUpdateState(reloadExtensionWhenIdle);
}

async function reloadExtensionWhenIdle() {
  const epoch = saveEpoch;
  const { extensionUpdate = {}, jobs = {} } = await chrome.storage.local.get(["extensionUpdate", "jobs"]);
  const hash = extensionUpdate.pendingHash;
  const reloaded = extensionUpdate.reloadedHashes || [];
  if (!hash || reloaded.includes(hash) || activeSaves || Object.keys(jobs).length) return;
  await chrome.storage.local.set({ extensionUpdate: {
    ...extensionUpdate, currentHash: hash, pendingHash: null, reloadedHashes: [...reloaded, hash],
  } });
  // Saving may begin while storage.set yields. Give it priority and retain
  // the pending update without consuming this hash's one reload attempt.
  if (activeSaves || saveEpoch !== epoch) {
    await chrome.storage.local.set({ extensionUpdate });
    return;
  }
  chrome.runtime.reload();
}

async function showSetupNotice(reason) {
  const { extensionSetupNotice } = await chrome.storage.local.get("extensionSetupNotice");
  if (extensionSetupNotice === reason) return;
  await chrome.storage.local.set({ extensionSetupNotice: reason });
  fail("Openmd 扩展需要修复", `${reason} ${SETUP_HINT}`);
}

function checkExtensionUpdate() {
  if (updateChecking) return updateChecking;
  updateChecking = (async () => {
    const reply = await native({ type: "ping", protocol_version: PROTOCOL_VERSION });
    if (!reply.ok) return showSetupNotice(reply.error || "本机 openmd 无法启动。");
    if (!/^[a-f0-9]{64}$/.test(reply.extension_hash || "")) return showSetupNotice("本机 openmd 尚不支持扩展自动同步。");
    return withUpdateState(async () => {
      const { extensionUpdate = {} } = await chrome.storage.local.get("extensionUpdate");
      const hash = reply.extension_hash;
      const changed = reply.extension_updated || (extensionUpdate.currentHash && extensionUpdate.currentHash !== hash);
      const incompatible = reply.protocol_version !== PROTOCOL_VERSION || reply.protocol_compatible === false;
      // Reload a freshly repaired copy once before asking for manual setup. A
      // repeated incompatible hash (or an old host) needs explicit intervention.
      const canRepair = changed && !(extensionUpdate.reloadedHashes || []).includes(hash);
      if (incompatible && !canRepair) return showSetupNotice("扩展与本机 openmd 协议不兼容。");
      if (!incompatible) await chrome.storage.local.set({ extensionSetupNotice: null });
      await chrome.storage.local.set({ extensionUpdate: {
        ...extensionUpdate, currentHash: extensionUpdate.currentHash || hash,
        pendingHash: changed ? hash : extensionUpdate.pendingHash,
      } });
      await reloadExtensionWhenIdle();
    });
  })().catch((error) => fail("扩展更新检查失败", `${String(error?.message || error)} ${SETUP_HINT}`))
    .finally(() => { updateChecking = null; });
  return updateChecking;
}

const isWeb = (url) => /^https?:\/\//.test(url || "");
// Keep host coverage aligned with backends/loaded_chat.py; parsing lives there.
const AI_CHAT_HOSTS = ["chatgpt.com", "chat.openai.com", "grok.com", "chat.deepseek.com", "doubao.com", "www.doubao.com", "gemini.google.com", "www.qianwen.com", "qianwen.com", "qianwen.aliyun.com", "tongyi.aliyun.com", "yuanbao.tencent.com"];
const isLoadedAiChat = (url) => {
  try { const parsed = new URL(url); return isWeb(url) && AI_CHAT_HOSTS.includes(parsed.hostname); }
  catch { return false; }
};
const isLoadedXiaohongshuNote = (url) => {
  try {
    const parsed = new URL(url);
    return isWeb(url) && parsed.hostname === "www.xiaohongshu.com" && /^\/(?:explore|discovery\/item)\//.test(parsed.pathname);
  } catch { return false; }
};

async function getSettings() {
  const { settings } = await chrome.storage.local.get("settings");
  return { videoMode: "note", includeThinking: false, allowFeedbackPackage: false, ...(settings || { locations: [], defaultId: null, clickMode: "auto" }) };
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
  const ping = await native({ type: "ping", protocol_version: PROTOCOL_VERSION });
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
  const create = chrome.notifications?.create;
  if (typeof create !== "function") return;
  try {
    const result = create.call(chrome.notifications, `openmd:${Date.now()}`, {
      type: "basic", iconUrl: "icons/icon-128.png", title, message: String(message || "").slice(0, 300),
    });
    result?.catch?.(() => {});
  } catch { /* Notifications are optional; the badge and recent result remain available. */ }
}

function fail(title, message) {
  flash("!", "#b3261e");
  notify(title, message);
}

async function notifyLogin(job, meta) {
  const { loginNotices = {} } = await chrome.storage.local.get("loginNotices");
  // Persist callbacks across service-worker restarts; one notice per site.
  const existing = Object.keys(loginNotices).find((id) => loginNotices[id].login.url === job.login.url);
  const id = existing || `openmd-login:${job.id}`;
  const notice = loginNotices[id] || { login: job.login, groups: {} };
  // A site notice can cover multiple batches; each retry retains its own host
  // batch so it cannot accidentally resume historical failures from that site.
  notice.groups[job.batch || job.id] = { jobId: job.id, location: meta.location || "默认", snapshot: job.snapshot, tabId: meta.tabId };
  loginNotices[id] = notice;
  await chrome.storage.local.set({ loginNotices });
  if (existing) return;
  await flash("!", "#b3261e");
  notifyWithId(id, {
    type: "basic", iconUrl: "icons/icon-128.png", title: `需要登录 ${job.login.site}`,
    message: `请在浏览器登录 ${job.login.url}，同站点任务已暂停。`,
    buttons: [{ title: "去登录" }, { title: "已登录，重试" }],
    requireInteraction: true,
  });
}

async function notifyFeedback(job, meta) {
  const settings = await getSettings();
  if (settings.allowFeedbackPackage !== true) return;
  const id = `openmd-feedback:${job.id}`;
  notifyWithId(id, {
    type: "basic", iconUrl: "icons/icon-128.png", title: "保存结果需要检查",
    message: "结果不完整或失败。可生成脱敏问题报告，附加前请先检查。",
    buttons: [{ title: "生成问题报告" }], requireInteraction: true,
  });
}

function notifyWithId(id, notice) {
  const create = chrome.notifications?.create;
  if (typeof create !== "function") return;
  try {
    const result = create.call(chrome.notifications, id, notice);
    result?.catch?.(() => {});
  } catch { /* Notifications are optional. */ }
}

async function feedbackReport(jobId) {
  const settings = await getSettings();
  const location = await locationFor(settings.defaultId);
  return native({ type: "feedback_report", id: jobId, save_to: location?.path });
}

chrome.notifications?.onButtonClicked?.addListener?.(async (id, buttonIndex) => {
  if (!id.startsWith("openmd-feedback:") || buttonIndex !== 0) return;
  const jobId = id.slice("openmd-feedback:".length);
  const reply = await feedbackReport(jobId);
  if (reply?.ok) notify("问题报告已生成", `已保存到 ${reply.report?.output || "默认问题报告目录"}，请检查后再附加到 GitHub issue。`);
  else {
    const actions = { feedback_not_available: "请在扩展设置中开启脱敏问题包后重新保存一次。", job_not_found: "请确认任务 ID，并在同一 Openmd 数据目录中重试。" };
    notify("问题报告生成失败", reply?.next_action || actions[reply?.error_code] || reply?.error || "请在任务列表中重试。");
  }
});

chrome.notifications?.onButtonClicked?.addListener?.((id, buttonIndex) => saving(async () => {
  const { loginNotices = {} } = await chrome.storage.local.get("loginNotices");
  const notice = loginNotices[id];
  if (!notice) return;
  if (buttonIndex === 0 && isWeb(notice.login.url)) {
    await chrome.tabs.create({ url: notice.login.url });
  } else if (buttonIndex === 1) {
    const jobs = [];
    const pending = {};
    for (const [batch, group] of Object.entries(notice.groups)) {
      try {
        const snapshot = group.snapshot ? await captureSnapshot(group.tabId) : undefined;
        const reply = await native({ type: "retry_login", id: group.jobId, snapshot });
        if (reply.ok) jobs.push(...(reply.jobs || []).map((job) => ({ ...job, tabId: group.tabId })));
        else pending[batch] = group;
      } catch {
        pending[batch] = group;
      }
    }
    if (Object.keys(pending).length) {
      notice.groups = pending;
      loginNotices[id] = notice;
    } else {
      delete loginNotices[id];
      try { await chrome.notifications?.clear?.(id); } catch { /* Optional API. */ }
    }
    await chrome.storage.local.set({ loginNotices });
    if (jobs.length) await track(jobs, crypto.randomUUID(), "原保存位置");
    if (Object.keys(pending).length) fail("部分任务没能重试", "请在扩展中检查任务状态。");
  }
}));

chrome.notifications?.onClosed?.addListener?.(async (id, byUser) => {
  if (!byUser) return;
  const { loginNotices = {} } = await chrome.storage.local.get("loginNotices");
  if (loginNotices[id]) {
    delete loginNotices[id];
    await chrome.storage.local.set({ loginNotices });
  }
});

async function locationFor(locationId) {
  const settings = await getSettings();
  return settings.locations.find((l) => l.id === (locationId || settings.defaultId)) || settings.locations[0];
}

async function track(jobs, batch, location) {
  const state = await tracked();
  for (const job of jobs) state.jobs[job.id] = { title: job.title || job.url, batch, tabId: job.tabId };
  state.batches[batch] = { total: jobs.length, failed: [], location, loginPaused: false };
  await chrome.storage.local.set(state);
  await showRunning(state.jobs);
  poll();
}

// ---------------------------------------------------------------- saving

async function saveTabs(tabs, locationId) {
  return saving(() => saveTabsImpl(tabs, locationId));
}

async function saveTabsImpl(tabs, locationId) {
  const web = tabs.filter((tab) => isWeb(tab.url));
  if (!web.length) return fail("没法保存", "只能保存普通网页（http / https）。");
  // Flush ordinary URLs in their existing batch. AI pages require the actual
  // tab handle; never reopen them through saveMany's URL queue.
  let pending = [];
  let total = 0;
  let location;
  const flush = async () => {
    if (!pending.length) return { ok: true };
    const result = await saveMany(pending, locationId);
    pending = [];
    if (result?.ok) { total += result.count; location = result.location; }
    return result;
  };
  for (const tab of web) {
    if (!isLoadedAiChat(tab.url) && !isLoadedXiaohongshuNote(tab.url)) { pending.push(tab); continue; }
    const previous = await flush();
    if (!previous?.ok) return previous;
    const tabId = tab.id ?? tab.tabId;
    if (!Number.isInteger(tabId)) return fail("读不到当前对话", "请返回原对话标签页再保存。");
    const result = await saveSnapshot(tabId, locationId);
    if (!result?.ok) return result;
    total += result.count;
    location = result.location;
  }
  const last = await flush();
  return last?.ok ? { ok: true, count: total, location } : last;
}

// video: "note" (just mention the video), "text" (+ local transcript), "file" (+ the video file).
async function saveMany(items, locationId, video) {
  return saving(() => saveManyImpl(items, locationId, video));
}

async function saveManyImpl(items, locationId, video) {
  const web = items.filter((item) => isWeb(item.url));
  if (!web.length) return fail("没法保存", "只能保存普通网页（http / https）。");
  const location = await locationFor(locationId);
  if (!location) {
    chrome.runtime.openOptionsPage();
    return fail("先设置一个保存位置", "已打开设置页。");
  }
  const settings = await getSettings();
  video = video || settings.videoMode || "note";
  const feedbackOptIn = settings.allowFeedbackPackage === true;
  const titles = Object.fromEntries(web.map((item) => [item.url, item.title]));
  const reply = web.length === 1
    ? await native({ type: "save", url: web[0].url, save_to: location.path, video, feedback_opt_in: feedbackOptIn })
    : await native({ type: "save_many", urls: web.map((item) => item.url), save_to: location.path, video, feedback_opt_in: feedbackOptIn });
  if (!reply.ok) return fail("没能开始保存", reply.error);
  const jobs = (reply.jobs || [reply.job]).map((job) => ({ ...job, title: titles[job.url] }));
  await track(jobs, reply.batch || jobs[0].id, location.name);
  return { ok: true, count: jobs.length, location: location.name };
}

async function captureSnapshot(tabId) {
  const [{ result } = {}] = await chrome.scripting.executeScript({
    target: { tabId },
    args: [AI_CHAT_HOSTS],
    func: (aiHosts) => {
      let html = document.documentElement?.outerHTML || "";
      if (aiHosts.includes(location.hostname) && document.documentElement) {
        const clone = document.documentElement.cloneNode(true);
        const originals = document.documentElement.querySelectorAll("*");
        const copies = clone.querySelectorAll("*");
        originals.forEach((node, index) => {
          const style = getComputedStyle(node);
          if (style.display === "none" || style.visibility === "hidden") copies[index].setAttribute("data-openmd-hidden", "true");
        });
        // Retain rendered messages, not hydration payloads or composer values.
        clone.querySelectorAll("script, style, noscript, input, textarea").forEach((node) => node.remove());
        html = clone.outerHTML;
      }
      return { html, url: location.href, title: document.title || location.href };
    },
  });
  return result;
}

async function saveSnapshot(tabId, locationId) {
  return saving(() => saveSnapshotImpl(tabId, locationId));
}

async function saveSnapshotImpl(tabId, locationId) {
  const location = await locationFor(locationId);
  if (!location) {
    chrome.runtime.openOptionsPage();
    return fail("先设置一个保存位置", "已打开设置页。");
  }
  try {
    const result = await captureSnapshot(tabId);
    if (!result?.html || !/^https?:\/\//.test(result.url || "")) {
      return fail("没法保存当前页", "当前页面不是可保存的 http(s) 页面。");
    }
    const { includeThinking } = await getSettings();
    const reply = await native({ type: "save_snapshot", ...result, save_to: location.path, include_thinking: isLoadedAiChat(result.url) && includeThinking === true, feedback_opt_in: (await getSettings()).allowFeedbackPackage === true });
    if (!reply.ok) return fail("没能保存当前页", reply.error);
    await track([{ ...reply.job, tabId }], reply.job.id, location.name);
    return { ok: true, count: 1, location: location.name };
  } catch (err) {
    return fail("读不到这个页面", String(err?.message || err));
  }
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
          if (!["done", "failed", "paused", "cancelled", "unknown"].includes(job.state)) continue;
          const meta = state.jobs[id];
          delete state.jobs[id];
          const batch = state.batches[meta.batch];
          if (job.error_code === "login_required" && job.login) {
            if (batch) batch.loginPaused = true;
            await notifyLogin(job, { ...meta, location: batch?.location });
          } else if (batch && job.state !== "done" && job.state !== "cancelled") batch.failed.push(`${meta.title}：${job.error || "状态未知"}`);
          if (job.state === "failed" || job.state === "partial" || job.result?.status === "partial") await notifyFeedback(job, meta);
          const remaining = Object.values(state.jobs).some((m) => m.batch === meta.batch);
          if (batch && !remaining) {
            delete state.batches[meta.batch];
            if (batch.failed.length) {
              const head = batch.total > 1 ? `${batch.total} 个里有 ${batch.failed.length} 个没保存成功` : "没能保存";
              fail(head, batch.failed.slice(0, 3).join("\n"));
            } else if (!batch.loginPaused) {
              await flash("✓", "#2c8a5f");
            }
          }
        }
        await chrome.storage.local.set(state);
        await showRunning(state.jobs);
        if (!Object.keys(state.jobs).length) break;
      }
      await new Promise((r) => setTimeout(r, POLL_MS));
    }
  } finally {
    polling = false;
    await maybeReloadExtension();
  }
}

// ---------------------------------------------------------------- entry points

chrome.action.onClicked.addListener((tab) => saving(async () => {
  const tabs = await highlightedTabs(tab);
  return saveTabs(tabs);
}));

chrome.runtime.onInstalled.addListener(async () => {
  await installContextMenus();
  await seedSettings();
  await applyClickMode();
  if (!(await getSettings()).locations.length) chrome.runtime.openOptionsPage();
});

async function installContextMenus() {
  const menus = chrome.contextMenus;
  if (typeof menus?.create !== "function" || typeof menus?.removeAll !== "function") return;
  try {
    await new Promise((resolve, reject) => {
      try {
        const pending = menus.removeAll(resolve);
        if (pending?.then) pending.then(resolve, reject);
        else if (menus.removeAll.length === 0) resolve();
      } catch (error) { reject(error); }
    });
    menus.create({ id: "save-page", title: "用 Openmd 保存此页", contexts: ["page"] });
    menus.create({ id: "save-link", title: "用 Openmd 保存此链接", contexts: ["link"] });
    menus.create({ id: "save-selection", title: "用 Openmd 保存选中的链接", contexts: ["selection"] });
    // Keep the plain action fast; media is opt-in per save on every web page.
    const posts = ["http://*/*", "https://*/*"];
    menus.create({ id: "save-video-text", title: "用 Openmd 保存（含视频/音频文字版）", contexts: ["page", "link", "video"], documentUrlPatterns: posts });
    menus.create({ id: "save-video-file", title: "用 Openmd 保存（含视频/音频文件）", contexts: ["page", "link", "video"], documentUrlPatterns: posts });
  } catch { /* Context menus are optional in some Chromium hosts. */ }
}

chrome.contextMenus?.onClicked?.addListener?.((info, tab) => saving(async () => {
  if (info.menuItemId === "save-link" && info.linkUrl) return saveMany([{ url: info.linkUrl, title: info.linkUrl }]);
  if (info.menuItemId === "save-page" && tab?.url) return saveTabs([tab]);
  if (info.menuItemId === "save-video-text" || info.menuItemId === "save-video-file") {
    // A link click targets that link; on a video element the post page owns the
    // text (the element's srcUrl is usually a blob: stream, not a public URL).
    const url = (info.linkUrl && /^https?:/.test(info.linkUrl)) ? info.linkUrl : (info.pageUrl || tab?.url);
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
}));

chrome.storage.onChanged.addListener((changes) => {
  if (changes.settings) applyClickMode();
});

chrome.runtime.onMessage.addListener((msg, _sender, reply) => {
  if (msg?.cmd === "save") { saveTabs(msg.items, msg.locationId).then(reply); return true; }
  if (msg?.cmd === "snapshot") { saving(async () => { const [tab] = await chrome.tabs.query({ active: true, currentWindow: true }); return saveSnapshot(tab?.id, msg.locationId); }).then(reply); return true; }
  if (msg?.cmd === "native") { native(msg.message).then(reply); return true; }
  if (msg?.cmd === "import_xiaoyuzhou") {
    const getAllCookies = chrome.cookies?.getAll;
    if (typeof getAllCookies !== "function") {
      reply({ ok: false, error: "此浏览器暂不支持读取 cookies，无法导入小宇宙登录状态。" });
      return false;
    }
    let replied = false;
    const onCookies = (cookies) => {
      if (replied) return;
      replied = true;
      const error = chrome.runtime.lastError;
      if (error) { reply({ ok: false, error: "读取小宇宙登录状态失败，请确认扩展权限。" }); return; }
      const selected = Object.fromEntries((cookies || [])
        .filter((cookie) => ["x-jike-access-token", "x-jike-refresh-token"].includes(cookie.name))
        .map((cookie) => [cookie.name, cookie.value]));
      native({ type: "import_xiaoyuzhou", cookies: selected }).then((result) => {
        reply(result?.ok ? { ok: true } : { ok: false, error: "没有找到可用的小宇宙登录凭证，请先在此浏览器登录后重试。" });
      });
    };
    try {
      const pending = getAllCookies.call(chrome.cookies, { domain: "xiaoyuzhoufm.com" }, onCookies);
      pending?.catch?.(() => {
        if (!replied) { replied = true; reply({ ok: false, error: "读取小宇宙登录状态失败，请确认浏览器权限。" }); }
      });
    } catch {
      if (!replied) { replied = true; reply({ ok: false, error: "此浏览器暂不支持读取 cookies，无法导入小宇宙登录状态。" }); }
    }
    return true;
  }
  if (msg?.cmd === "recent_feedback") {
    native({ type: "recent" }).then((result) => reply({
      ok: result?.ok === true,
      jobs: (result?.jobs || []).map((job) => ({
        id: job.id, state: job.state, snapshot: job.snapshot === true,
        feedback_opt_in: job.feedback_opt_in === true, title: job.title,
        saved_to: job.saved_to, save_to: job.save_to,
        error: job.error, next_action: job.next_action,
      })),
      error: result?.error,
    }));
    return true;
  }
  if (msg?.cmd === "feedback_report") { feedbackReport(msg.id).then(reply); return true; }
  return false;
});

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === UPDATE_ALARM) return checkExtensionUpdate();
});
chrome.runtime.onStartup.addListener(async () => { await applyClickMode(); poll(); await checkExtensionUpdate(); });
chrome.alarms.create(UPDATE_ALARM, { periodInMinutes: 30 });
applyClickMode();
poll();
checkExtensionUpdate();
