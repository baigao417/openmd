"""Vendor the OpenCLI Browser Bridge into the Openmd extension (one extension for users).

    python scripts/vendor_opencli_bridge.py            # fetch pinned release, verify, patch, write
    python scripts/vendor_opencli_bridge.py --check    # verify the vendored copy matches this script

Source: the official release asset of jackwener/opencli (Apache-2.0). Bump the
three pins below together with OPENCLI_VERSION in src/openmd/runtime/managed_tools.py.
Standard library only.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import sys
import urllib.request
import urllib.error
import zipfile
from pathlib import Path

OPENCLI_TAG = "v2.0.3"
BRIDGE_VERSION = "1.0.27"
BRIDGE_SHA256 = "8636d42a0fe44d7ba3b0603dbcd9581e8269ccbca950136d0dfc0ffc39a8a213"
LICENSE_SHA256 = "0210b8b66cf00358242cb921ba2be3a46dfe0190159b1b952388a3880ce1ff54"
LICENSE_URL = f"https://raw.githubusercontent.com/jackwener/opencli/{OPENCLI_TAG}/LICENSE"
ZIP_URL = f"https://github.com/jackwener/opencli/releases/download/{OPENCLI_TAG}/opencli-extension-v{BRIDGE_VERSION}.zip"

ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / "src" / "openmd" / "browser_extension"
VENDOR = EXTENSION / "vendor" / "opencli-bridge"

# (description, original, replacement): each original must occur exactly as counted.
PATCHES = [
    (
        "fixed profile id so Openmd can select this bridge even when the standalone OpenCLI extension is also connected",
        "const generated = generateContextId();",
        'const generated = "openmd"; // Openmd patch: fixed Browser Bridge profile id',
        1,
    ),
    (
        "report the bridge's own version, not the host Openmd extension's manifest version",
        "chrome.runtime.getManifest().version",
        "OPENMD_BRIDGE_VERSION",
        2,
    ),
('P-C/P-D owned blank-first muted window; offscreen/reuse/existing-window strategies', '  const win = await chrome.windows.create({\n    url: startUrl,\n    focused: mode === "foreground",\n    width: 1280,\n    height: 900,\n    type: "normal"\n  });', '  const win = await openmdCreateOwnedWindow(startUrl, mode);', 1),
    (
        "tab group names show Openmd",
        'interactive: "OpenCLI Browser",',
        'interactive: "Openmd",',
        1,
    ),
    (
        "tab group names show Openmd (automation)",
        'automation: "OpenCLI Adapter"',
        'automation: "Openmd"',
        1,
    ),
]

PATCHES += [
    (
        'P-A persistent adapter idle timeout is finite and configurable',
        """if (adapterPersistent) return IDLE_TIMEOUT_NONE;""",
        """if (adapterPersistent) return overrides?.idleTimeoutMs ?? openmdOptions.idleMs;""",
        1,
    ),
    (
        'Openmd owned lifecycle options and profile-scoped orphan control',
        """async function handleCommand(cmd) {""",
        """const openmdOptions = { idleMs: 120000, sound: false, show: false, ownerPid: null, windowStrategy: "offscreen-reuse" };
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
  }""",
        1,
    ),
    (
        'P-B release owned tab identity without retaining idle pages or removing containers',
        """      const hasOtherOwnedLease = [...automationSessions.entries()].some(
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
""",
        """      if ([...automationSessions.entries()].some(([otherKey, other]) => otherKey !== leaseKey && other.preferredTabId === tabId)) {
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
""",
        1,
    ),
    (
        'P-C mute owned tab before navigation',
        """await chrome.tabs.create({ windowId, url: targetUrl, active: true })""",
        """await openmdCreateOwnedTab({ windowId, url: targetUrl, active: true })""",
        1,
    ),
    (
        'P-C mute owned tab before navigation',
        """await chrome.tabs.create({ windowId: scopedWindowId, url: BLANK_PAGE, active: true })""",
        """await openmdCreateOwnedTab({ windowId: scopedWindowId, url: BLANK_PAGE, active: true })""",
        1,
    ),
    (
        'P-C mute owned tab before navigation',
        """await chrome.tabs.create({ windowId, url: cmd.url ?? BLANK_PAGE, active: true })""",
        """await openmdCreateOwnedTab({ windowId, url: cmd.url ?? BLANK_PAGE, active: true })""",
        1,
    ),
    (
        'Never reuse user tabs dragged into an owned container',
        """(tab) => tab.id !== void 0 && initialTabIsAvailable(tab.id)""",
        """(tab) => tab.id !== void 0 && openmdOwnsTab(tab.id) && initialTabIsAvailable(tab.id)""",
        1,
    ),
    (
        'Choose only the created owned initial tab in an existing user window',
        """const initialTabId = tabs[0]?.id;""",
        """const initialTabId = tabs.find(t => openmdCreatedTabs.has(t.id) && initialTabIsAvailable(t.id))?.id;""",
        1,
    ),
    (
        'P-D collapsed owned group in an existing window',
        """collapsed: false
  });
  updateOwnedSessionWindowForTabs""",
        """collapsed: openmdOptions.windowStrategy === "existing-window"
  });
  updateOwnedSessionWindowForTabs""",
        1,
    ),
    (
        'Group automation leases only in existing-window mode',
        """if (role === "automation") return null;""",
        """if (role === "automation" && openmdOptions.windowStrategy !== "existing-window") return null;""",
        1,
    ),
    (
        'Never migrate user tabs during owned group convergence',
        """const tabIds = tabs.map((tab) => tab.id).filter((id) => id !== void 0);""",
        """const tabIds = tabs.map((tab) => tab.id).filter((id) => id !== void 0 && openmdOwnsTab(id));""",
        1,
    ),
    (
        'Track originating controller on owned leases',
        """windowRole: getWindowRole(key, ownership)""",
        """windowRole: getWindowRole(key, ownership),
    openmdOwnerPid: session.openmdOwnerPid ?? openmdOptions.ownerPid""",
        1,
    ),
    (
        'Persist controller identity across service worker suspension',
        """windowRole: session.windowRole,""",
        """windowRole: session.windowRole,
      openmdOwnerPid: session.openmdOwnerPid,""",
        1,
    ),
]

PATCHES += [('Restore controller identity with the exact recorded tab lease', 'owned: stored.owned,\n        preferredTabId: tabId', 'owned: stored.owned,\n        openmdOwnerPid: stored.openmdOwnerPid,\n        preferredTabId: tabId', 1)]

PATCHES += [('Refresh job anchor with every real command', '  resetWindowIdleTimer(leaseKey);\n  activeCommandCounts.set(leaseKey,', '  resetWindowIdleTimer(leaseKey);\n  if (openmdOptions.ownerPid) resetWindowIdleTimer(getLeaseKey("openmd-job:" + openmdOptions.ownerPid, "adapter"));\n  activeCommandCounts.set(leaseKey,', 1), ('Long commands keep their job anchor alive', '  session.idleTimer = setTimeout(async () => {\n    if ((activeCommandCounts.get(leaseKey) ?? 0) > 0) {\n      return;\n    }', '  session.idleTimer = setTimeout(async () => {\n    const jobActive = session.session?.startsWith("openmd-job:") && [...automationSessions.entries()].some(([key, item]) => item.openmdOwnerPid === session.openmdOwnerPid && (activeCommandCounts.get(key) ?? 0) > 0);\n    if ((activeCommandCounts.get(leaseKey) ?? 0) > 0 || jobActive) {\n      resetWindowIdleTimer(leaseKey);\n      return;\n    }', 1)]

# Openmd's own idle page (upstream idle.html is replaced, not patched).
IDLE_HTML = """<!doctype html>
<html lang="zh-CN">
<head><meta charset="utf-8"><title>Openmd</title>
<style>body{font:15px/1.6 system-ui,sans-serif;color:#555;background:#fafafa;display:grid;place-items:center;height:100vh;margin:0}</style></head>
<body><p>这是 Openmd 的后台读取窗口，可以忽略；关掉也不影响使用。</p></body>
</html>
"""
HEADER = (
    "// OpenCLI Browser Bridge {version} (Apache-2.0, https://github.com/jackwener/opencli),\n"
    "// vendored by scripts/vendor_opencli_bridge.py. Modified by Openmd: see VENDOR.md.\n"
    'const OPENMD_BRIDGE_VERSION = "{version}";\n'
)


def fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "openmd-vendor"})
    try:
        response = urllib.request.urlopen(request, timeout=30)  # noqa: S310 - fixed https URLs
    except urllib.error.URLError:
        response = urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=30)
    with response:
        return response.read()


def build(archive_path: Path | None = None, license_path: Path | None = None) -> dict[str, bytes]:
    archive = archive_path.read_bytes() if archive_path else fetch(ZIP_URL)
    digest = hashlib.sha256(archive).hexdigest()
    if digest != BRIDGE_SHA256:
        raise SystemExit(f"sha256 mismatch for {ZIP_URL}: {digest}")
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        background = bundle.read("dist/background.js").decode("utf-8")
        bundle.read("idle.html")  # must still exist upstream: the bridge navigates to it
    for description, original, replacement, count in PATCHES:
        found = background.count(original)
        if found != count:
            raise SystemExit(f"patch no longer applies ({description}): expected {count}, found {found}")
        background = background.replace(original, replacement)
    background = HEADER.format(version=BRIDGE_VERSION) + background
    patches = "\n".join(f"- {description}: `{original}` → `{replacement}`" for description, original, replacement, _ in PATCHES)
    vendor_md = (
        f"# OpenCLI Browser Bridge (vendored)\n\n"
        f"- Upstream: https://github.com/jackwener/opencli (Apache-2.0), release {OPENCLI_TAG}, "
        f"extension {BRIDGE_VERSION}\n- Asset: {ZIP_URL}\n- SHA-256: `{BRIDGE_SHA256}`\n"
        f"- Files: `dist/background.js` → `background.js`; upstream `idle.html` replaced by Openmd's own page at the extension root\n\n"
        f"## Modifications by Openmd\n\n{patches}\n\n"
        f"Regenerate with `python scripts/vendor_opencli_bridge.py`; never edit `background.js` by hand.\n"
    )
    license_bytes = license_path.read_bytes() if license_path else fetch(LICENSE_URL)
    if hashlib.sha256(license_bytes).hexdigest() != LICENSE_SHA256:
        raise SystemExit("license sha256 mismatch")
    return {
        "background.js": background.encode("utf-8"),
        "LICENSE": license_bytes,
        "VENDOR.md": vendor_md.encode("utf-8"),
        "../../idle.html": IDLE_HTML.encode("utf-8"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="compare the vendored files with a fresh build")
    parser.add_argument("--archive", type=Path, help="local official release archive, checked against the pinned SHA-256")
    parser.add_argument("--license-file", type=Path, help="local license, checked against the pinned SHA-256")
    args = parser.parse_args()
    files = build(args.archive, args.license_file)
    VENDOR.mkdir(parents=True, exist_ok=True)
    stale = []
    for name, data in files.items():
        target = (VENDOR / name).resolve()
        if args.check:
            current = target.read_bytes().replace(b"\r\n", b"\n") if target.is_file() else None
            if current != data.replace(b"\r\n", b"\n"):
                stale.append(str(target.relative_to(ROOT)))
        else:
            target.write_bytes(data)
    if stale:
        print("vendored bridge is stale:", ", ".join(stale))
        return 1
    print("vendored bridge is up to date" if args.check else f"wrote OpenCLI Browser Bridge {BRIDGE_VERSION} into {VENDOR.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
