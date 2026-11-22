# OpenCLI Browser Bridge (vendored)

- Upstream: https://github.com/jackwener/opencli (Apache-2.0), release v2.0.3, extension 1.0.27
- Asset: https://github.com/jackwener/opencli/releases/download/v2.0.3/opencli-extension-v1.0.27.zip
- SHA-256: `8636d42a0fe44d7ba3b0603dbcd9581e8269ccbca950136d0dfc0ffc39a8a213`
- Files: `dist/background.js` → `background.js`; upstream `idle.html` replaced by Openmd's own page at the extension root

## Modifications by Openmd

- fixed profile id so Openmd can select this bridge even when the standalone OpenCLI extension is also connected: `const generated = generateContextId();` → `const generated = "openmd"; // Openmd patch: fixed Browser Bridge profile id`
- report the bridge's own version, not the host Openmd extension's manifest version: `chrome.runtime.getManifest().version` → `OPENMD_BRIDGE_VERSION`
- P-C/P-D owned blank-first muted window; offscreen/reuse/existing-window strategies: `  const win = await chrome.windows.create({
    url: startUrl,
    focused: mode === "foreground",
    width: 1280,
    height: 900,
    type: "normal"
  });` → `  const win = await openmdCreateOwnedWindow(startUrl, mode);`
- tab group names show Openmd: `interactive: "OpenCLI Browser",` → `interactive: "Openmd",`
- tab group names show Openmd (automation): `automation: "OpenCLI Adapter"` → `automation: "Openmd"`
- P-A persistent adapter idle timeout is finite and configurable: `if (adapterPersistent) return IDLE_TIMEOUT_NONE;` → `if (adapterPersistent) return overrides?.idleTimeoutMs ?? openmdOptions.idleMs;`
- Openmd owned lifecycle options and profile-scoped orphan control: `async function handleCommand(cmd) {` → `const openmdOptions = { idleMs: 120000, sound: false, show: false, ownerPid: null, windowStrategy: "offscreen-reuse" };
const openmdCreatedTabs = new Set();
chrome.tabs.onRemoved.addListener(id => openmdCreatedTabs.delete(id));
function openmdOwnsTab(id) {
  return openmdCreatedTabs.has(id) || [...automationSessions.values()].some(s => s.owned && s.preferredTabId === id);
}
async function openmdMuteOwnedTab(id) {
  if (!id || openmdOptions.sound) return;
  await chrome.tabs.update(id, { muted: true }).catch(() => console.warn("[openmd] owned tab mute failed"));
}
async function openmdCreateOwnedTab(properties) {
  const url = properties.url;
  const tab = await chrome.tabs.create({ ...properties, url: "about:blank", active: openmdOptions.windowStrategy === "existing-window" && !openmdOptions.show ? false : properties.active ?? false });
  openmdCreatedTabs.add(tab.id);
  await openmdMuteOwnedTab(tab.id);
  if (url && url !== "about:blank") return chrome.tabs.update(tab.id, { url });
  return tab;
}
async function openmdCreateOwnedWindow(startUrl, mode) {
  if (openmdOptions.show) mode = "foreground";
  if (mode !== "foreground" && openmdOptions.windowStrategy === "existing-window") {
    const windows = await chrome.windows.getAll({windowTypes:["normal"]});
    const current = windows.find(w => w.focused && !w.incognito) ?? windows.find(w => !w.incognito);
    if (current) {
      await openmdCreateOwnedTab({windowId:current.id, url:startUrl, active:false});
      return current;
    }
  }
  const win = await chrome.windows.create({
    url: "about:blank", focused: mode === "foreground", width:1280, height:900, type:"normal",
    ...(mode !== "foreground" && openmdOptions.windowStrategy === "offscreen-reuse" ? {left: -32000, top: -32000} : {})
  });
  const initial = (await chrome.tabs.query({windowId:win.id}))[0];
  if (initial?.id) {
    openmdCreatedTabs.add(initial.id);
    await openmdMuteOwnedTab(initial.id);
    if (startUrl !== "about:blank") await chrome.tabs.update(initial.id, {url:startUrl});
  }
  if (mode !== "foreground") await chrome.windows.update(win.id, { state: "minimized" }).catch(() => {});
  return win;
}
async function openmdControl(cmd) {
  if (currentContextId !== "openmd" || cmd.contextId !== "openmd") throw new CommandFailure("openmd_profile_required", "Cleanup is restricted to the openmd profile");
  if (cmd.action === "openmd-configure") {
    if (Number.isFinite(cmd.idleMs) && cmd.idleMs >= 1000 && cmd.idleMs <= 86400000) openmdOptions.idleMs = cmd.idleMs;
    openmdOptions.sound = cmd.sound === true;
    openmdOptions.show = cmd.show === true;
    if (Number.isInteger(cmd.ownerPid) && cmd.ownerPid > 0) openmdOptions.ownerPid = cmd.ownerPid;
    if (["offscreen-reuse", "reuse", "existing-window"].includes(cmd.windowStrategy)) openmdOptions.windowStrategy = cmd.windowStrategy;
    return {id:cmd.id, ok:true, data:{...openmdOptions}};
  }
  if (cmd.action === "openmd-owned-leases") return {id:cmd.id, ok:true, data:[...automationSessions.values()].filter(s=>s.owned && s.contextId === "openmd").map(s=>({ownerPid:s.openmdOwnerPid}))};
  if (cmd.action === "openmd-begin-job") {
    if (!Number.isInteger(cmd.ownerPid) || cmd.ownerPid <= 0) throw new CommandFailure("invalid_owner", "Missing job identity");
    const key = getLeaseKey("openmd-job:" + cmd.ownerPid, "adapter");
    setSessionOverride(key, {lifecycle:"persistent"});
    if (!automationSessions.has(key)) await createOwnedTabLeaseUnlocked(key, "about:blank");
    return {id:cmd.id, ok:true, data:{started:true}};
  }
  let tabsClosed = 0, windowsClosed = 0, failures = 0;
  const before = new Set((await chrome.windows.getAll()).map(w=>w.id));
  const active = new Set(Array.isArray(cmd.activeSessions) ? cmd.activeSessions : []);
  for (const [key, lease] of [...automationSessions]) {
    if (!lease.owned || lease.contextId !== "openmd" || active.has(lease.session) || (activeCommandCounts.get(key) ?? 0) > 0) continue;
    // A multi-step browser lease with an unexpired deadline is still in use.
    if (cmd.action === "openmd-end-job") {
      if (lease.openmdOwnerPid !== cmd.ownerPid) continue;
    } else if (lease.idleDeadlineAt > Date.now() && !(cmd.deadPids ?? []).includes(lease.openmdOwnerPid)) continue;
    if (lease.preferredTabId === null) continue;
    try { await releaseLease(key, "openmd orphan sweep"); tabsClosed++; }
    catch { failures++; }
  }
  const after = new Set((await chrome.windows.getAll()).map(w=>w.id));
  windowsClosed = [...before].filter(id=>!after.has(id)).length;
  const remainingOwned = [...automationSessions.values()].filter(s=>s.owned && s.contextId === "openmd" && (cmd.action !== "openmd-end-job" || s.openmdOwnerPid === cmd.ownerPid)).length;
  return {id:cmd.id, ok:true, data:{tabsClosed, windowsClosed, failures, remainingOwned}};
}

async function handleCommand(cmd) {
  if (cmd.action === "openmd-configure" || cmd.action === "openmd-cleanup-orphans" || cmd.action === "openmd-owned-leases" || cmd.action === "openmd-begin-job" || cmd.action === "openmd-end-job") {
    try { return await withLeaseMutation(() => openmdControl(cmd)); } catch (err) { return errorResult(cmd.id, err); }
  }`
- P-B release owned tab identity without retaining idle pages or removing containers: `      const hasOtherOwnedLease = [...automationSessions.entries()].some(
        ([otherLease, otherSession]) => otherLease !== leaseKey && otherSession.owned && otherSession.windowId === session.windowId && otherSession.preferredTabId !== null
      );
      await safeDetach(tabId);
      evictTab(tabId);
      if (hasOtherOwnedLease) {
        await chrome.tabs.remove(tabId).catch(() => {
        });
        console.log(`[opencli] Released owned tab lease ${tabId} (session=${session.session}, surface=${session.surface}, ${reason})`);
      } else {
        try {
          const role = getOwnedWindowRole(leaseKey);
          const idleUrl = role === "interactive" ? await parkIdleInteractiveTab(tabId, session.windowId) : null;
          const tab = await chrome.tabs.update(tabId, { url: idleUrl ?? BLANK_PAGE, active: true });
          const group = await ensureOwnedContainerGroup(getOwnedWindowRole(leaseKey), session.windowId, [tab.id ?? tabId]);
          if (group) session.windowId = group.windowId;
          console.log(`[opencli] Released owned tab lease ${tabId} as reusable placeholder (session=${session.session}, surface=${session.surface}, ${reason})`);
        } catch {
          await chrome.tabs.remove(tabId).catch(() => {
          });
          console.log(`[opencli] Released owned tab lease ${tabId} (session=${session.session}, surface=${session.surface}, ${reason})`);
        }
      }
` → `      if ([...automationSessions.entries()].some(([otherKey, other]) => otherKey !== leaseKey && other.preferredTabId === tabId)) {
        throw new CommandFailure("session_busy", "Another lease uses this tab");
      }
      if (openmdOwnsTab(tabId)) {
        await safeDetach(tabId);
        evictTab(tabId);
        const tab = await chrome.tabs.get(tabId).catch(() => null);
        if (tab) {
          await chrome.tabs.ungroup(tabId);
          await chrome.tabs.remove(tabId);
        }
        openmdCreatedTabs.delete(tabId);
      }
`
- P-C mute owned tab before navigation: `await chrome.tabs.create({ windowId, url: targetUrl, active: true })` → `await openmdCreateOwnedTab({ windowId, url: targetUrl, active: true })`
- P-C mute owned tab before navigation: `await chrome.tabs.create({ windowId: scopedWindowId, url: BLANK_PAGE, active: true })` → `await openmdCreateOwnedTab({ windowId: scopedWindowId, url: BLANK_PAGE, active: true })`
- P-C mute owned tab before navigation: `await chrome.tabs.create({ windowId, url: cmd.url ?? BLANK_PAGE, active: true })` → `await openmdCreateOwnedTab({ windowId, url: cmd.url ?? BLANK_PAGE, active: true })`
- Never reuse user tabs dragged into an owned container: `(tab) => tab.id !== void 0 && initialTabIsAvailable(tab.id)` → `(tab) => tab.id !== void 0 && openmdOwnsTab(tab.id) && initialTabIsAvailable(tab.id)`
- Choose only the created owned initial tab in an existing user window: `const initialTabId = tabs[0]?.id;` → `const initialTabId = tabs.find(t => openmdCreatedTabs.has(t.id) && initialTabIsAvailable(t.id))?.id;`
- P-D collapsed owned group in an existing window: `collapsed: false
  });
  updateOwnedSessionWindowForTabs` → `collapsed: openmdOptions.windowStrategy === "existing-window"
  });
  updateOwnedSessionWindowForTabs`
- Group automation leases only in existing-window mode: `if (role === "automation") return null;` → `if (role === "automation" && openmdOptions.windowStrategy !== "existing-window") return null;`
- Never migrate user tabs during owned group convergence: `const tabIds = tabs.map((tab) => tab.id).filter((id) => id !== void 0);` → `const tabIds = tabs.map((tab) => tab.id).filter((id) => id !== void 0 && openmdOwnsTab(id));`
- Track originating controller on owned leases: `windowRole: getWindowRole(key, ownership)` → `windowRole: getWindowRole(key, ownership),
    openmdOwnerPid: session.openmdOwnerPid ?? openmdOptions.ownerPid`
- Persist controller identity across service worker suspension: `windowRole: session.windowRole,` → `windowRole: session.windowRole,
      openmdOwnerPid: session.openmdOwnerPid,`
- import the fixed X page reader into the Browser Bridge module: `const CHROME_METHODS = {` → `import { openmdXReadPage } from "../../x_browser_main.js";
const CHROME_METHODS = {`
- allow only the fixed X read handler in an already-open X tab: `async function callChrome(method, args, protectedTabs, protectedGroups) {
  const { receiver, fn } = resolve(method);` → `async function openmdReadX(args) {
  const fail = (error_code, next_action) => ({ok:false,error_code,next_action});
  if (!Array.isArray(args) || args.length !== 1 || !/^\d+$/.test(String(args[0]))) throw nativeError("invalid_args", "openmd.x.read requires a numeric X post id.");
  const tabs = await chrome.tabs.query({url:["https://x.com/*","https://twitter.com/*"]});
  const tab = tabs.find(value => value.id !== void 0 && ["x.com","www.x.com","twitter.com","www.twitter.com"].includes(new URL(value.url).hostname.toLowerCase()));
  if (!tab) return fail("x_browser_no_tab","Open x.com in Chrome and retry; Openmd will continue to the next browser route.");
  if (typeof chrome.scripting?.executeScript !== "function") return fail("x_browser_permission_denied","Reload the Openmd extension and grant its requested permissions, then retry.");
  try {
    const [injection] = await chrome.scripting.executeScript({target:{tabId:tab.id},world:"MAIN",func:openmdXReadPage,args:[String(args[0])]});
    return injection?.result ?? fail("x_browser_api_failed","Retry once; Openmd will continue to the next browser route.");
  } catch (error) {
    const permission = String(error?.message || error).toLowerCase().includes("permission");
    return fail(permission?"x_browser_permission_denied":"x_browser_api_failed",permission?"Reload the Openmd extension and grant its requested permissions, then retry.":"Retry once; Openmd will continue to the next browser route.");
  }
}
async function callChrome(method, args, protectedTabs, protectedGroups) {
  if (method === "openmd.x.read") return await openmdReadX(args);
  const { receiver, fn } = resolve(method);`
- Restore controller identity with the exact recorded tab lease: `owned: stored.owned,
        preferredTabId: tabId` → `owned: stored.owned,
        openmdOwnerPid: stored.openmdOwnerPid,
        preferredTabId: tabId`
- Refresh job anchor with every real command: `  resetWindowIdleTimer(leaseKey);
  activeCommandCounts.set(leaseKey,` → `  resetWindowIdleTimer(leaseKey);
  if (openmdOptions.ownerPid) resetWindowIdleTimer(getLeaseKey("openmd-job:" + openmdOptions.ownerPid, "adapter"));
  activeCommandCounts.set(leaseKey,`
- Long commands keep their job anchor alive: `  session.idleTimer = setTimeout(async () => {
    if ((activeCommandCounts.get(leaseKey) ?? 0) > 0) {
      return;
    }` → `  session.idleTimer = setTimeout(async () => {
    const jobActive = session.session?.startsWith("openmd-job:") && [...automationSessions.entries()].some(([key, item]) => item.openmdOwnerPid === session.openmdOwnerPid && (activeCommandCounts.get(key) ?? 0) > 0);
    if ((activeCommandCounts.get(leaseKey) ?? 0) > 0 || jobActive) {
      resetWindowIdleTimer(leaseKey);
      return;
    }`
- Quiet Openmd browser and adapter surfaces share the automation container: `function getOwnedWindowRole(key) {
  return getSurfaceFromKey(key) === "browser" ? "interactive" : "automation";
}` → `function getOwnedWindowRole(key) {
  return getSurfaceFromKey(key) === "browser" && (openmdOptions.show || currentContextId !== "openmd") ? "interactive" : "automation";
}`

Regenerate with `python scripts/vendor_opencli_bridge.py`; never edit `background.js` by hand.
