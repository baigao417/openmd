"""Single registry for pinned OpenCLI edits and dispatcher ABI dependencies.

All matching is exact and read-only unless the bridge generator explicitly
writes its own output. Runtime loaders patch memory, never the npm package.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
from urllib.parse import quote


OPENCLI_TAG = 'v2.0.3'
BRIDGE_VERSION = '1.0.27'
BRIDGE_SHA256 = '8636d42a0fe44d7ba3b0603dbcd9581e8269ccbca950136d0dfc0ffc39a8a213'
BRIDGE_GENERATED_SHA256 = '1cf540a2b2199bdaeb2524f0a8a7f5844822650c6c7ebefb30c4282ec5c24f4c'  # populated from the already verified generated file
# (description, original, replacement): each original must occur exactly as counted.
BRIDGE_PATCHES = [
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

BRIDGE_PATCHES += [
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

BRIDGE_PATCHES += [('Restore controller identity with the exact recorded tab lease', 'owned: stored.owned,\n        preferredTabId: tabId', 'owned: stored.owned,\n        openmdOwnerPid: stored.openmdOwnerPid,\n        preferredTabId: tabId', 1)]

BRIDGE_PATCHES += [('Refresh job anchor with every real command', '  resetWindowIdleTimer(leaseKey);\n  activeCommandCounts.set(leaseKey,', '  resetWindowIdleTimer(leaseKey);\n  if (openmdOptions.ownerPid) resetWindowIdleTimer(getLeaseKey("openmd-job:" + openmdOptions.ownerPid, "adapter"));\n  activeCommandCounts.set(leaseKey,', 1), ('Long commands keep their job anchor alive', '  session.idleTimer = setTimeout(async () => {\n    if ((activeCommandCounts.get(leaseKey) ?? 0) > 0) {\n      return;\n    }', '  session.idleTimer = setTimeout(async () => {\n    const jobActive = session.session?.startsWith("openmd-job:") && [...automationSessions.entries()].some(([key, item]) => item.openmdOwnerPid === session.openmdOwnerPid && (activeCommandCounts.get(key) ?? 0) > 0);\n    if ((activeCommandCounts.get(leaseKey) ?? 0) > 0 || jobActive) {\n      resetWindowIdleTimer(leaseKey);\n      return;\n    }', 1)]

BRIDGE_PATCHES += [('Quiet Openmd browser and adapter surfaces share the automation container',
             'function getOwnedWindowRole(key) {\n  return getSurfaceFromKey(key) === "browser" ? "interactive" : "automation";\n}',
             'function getOwnedWindowRole(key) {\n  return getSurfaceFromKey(key) === "browser" && (openmdOptions.show || currentContextId !== "openmd") ? "interactive" : "automation";\n}', 1)]

# Chrome throttles page timers in inactive tabs. Schedule read polling in Node.
# These helpers and every injection are checked as loader entries below.
_AI_HELPERS = r'''
function openmdBackgroundPage(page) {
  return new Proxy(page, {get(target, key) {
    // The session already owns its target. Gemini's helper selects the same
    // hidden target to activate animations; reading must not steal focus.
    if (key === 'selectTab') return async () => {};
    if (key === 'goto') return (url, options) => target.goto(url, {...options, waitUntil: 'none'});
    if (key === 'wait') return async (options) => {
      if (typeof options === 'number') return new Promise(r => setTimeout(r, options * 1000));
      if (typeof options?.time === 'number') return new Promise(r => setTimeout(r, options.time * 1000));
      if (options?.selector) {
        const end = Date.now() + (options.timeout ?? 10) * 1000;
        while (Date.now() < end) {
          if (await target.evaluate('!!document.querySelector(' + JSON.stringify(options.selector) + ')')) return;
          await new Promise(r => setTimeout(r, 500));
        }
        throw new Error('openmd_selector_wait_timeout');
      }
      return target.wait(options);
    };
    const value = Reflect.get(target, key, target);
    return typeof value === 'function' ? value.bind(target) : value;
  }});
}
async function openmdGenerating(page) {
  if (typeof page.evaluate !== 'function') return false;
  return !!(await page.evaluate(`!!document.querySelector('[data-testid="stop-button"], [data-testid="stop-response-button"], button[aria-label="Stop generating"], button[aria-label="停止生成"]')`));
}
function openmdMeasured(handler, selector) {
  return async (page, kwargs) => {
    const rows = await handler(page, kwargs);
    if (!Array.isArray(rows)) return rows;
    const count = await page.evaluate('document.querySelectorAll(' + JSON.stringify(selector) + ').length');
    const generating = await openmdGenerating(page);
    return rows.map(row => ({...row, ...(Number.isInteger(count) && count > 0 ? {OpenmdPageTurnCount: count} : {}), Generating: generating}));
  };
}
async function openmdStableRows(page, read, select = x => x) {
  let previous = '', stable = 0;
  const deadline = Date.now() + 45000;
  while (Date.now() < deadline) {
    const value = await read();
    const rows = select(value);
    const signature = JSON.stringify(rows);
    stable = signature === previous ? stable + 1 : 0;
    previous = signature;
    if (Array.isArray(rows) && rows.some(r => /^(assistant|model|助手)$/i.test(r.Role ?? r.role ?? '')) && stable >= 2 && !(await openmdGenerating(page))) return value;
    await new Promise(r => setTimeout(r, 1000));
  }
  const err = new Error('AI conversation did not finish loading');
  err.code = 'AI_CHAT_NOT_STABLE'; err.exitCode = 75;
  throw err;
}
'''

_BUBBLE_EDITS = (
    ('let bubbles = [];',
     "let bubbles = []; let grokPrevious = ''; let grokStablePolls = 0; let grokStable = false;"),
    ('const POLL_DEADLINE_MS = 20_000;', 'const POLL_DEADLINE_MS = 45_000;'),
    ('if (bubbles.length > 0) break;', """
const signature = JSON.stringify(bubbles.map(b => [b.role, b.text, b.html]));
grokStablePolls = signature === grokPrevious ? grokStablePolls + 1 : 0;
grokPrevious = signature;
if (bubbles.length > 0 && bubbles.some(b => /^assistant$/i.test(b.role)) && grokStablePolls >= 2 && !(await openmdGenerating(page))) {grokStable = true; break;}
"""),
    ('if (!bubbles.length)',
     "if (bubbles.length && !grokStable) throw Object.assign(new Error('grok_detail_not_stable'), "
     "{code:'AI_CHAT_NOT_STABLE',exitCode:75});\nif (!bubbles.length)"),
)
_DETAIL_EDITS = {
    'grok': _BUBBLE_EDITS,
    'qwen': _BUBBLE_EDITS,
    'yuanbao': _BUBBLE_EDITS,
    'deepseek': (
        ('await ensureOnDeepSeek(page);', '// Navigate to the explicit conversation once; no redundant home wait.'),
        ('const messages = await getVisibleMessages(page);',
         'const messages = await openmdStableRows(page, () => getVisibleMessages(page));'),
    ),
    'gemini': (
        ('const turns = await getGeminiVisibleTurns(page);',
         'const turns = await openmdStableRows(page, () => getGeminiVisibleTurns(page));'),
    ),
    'doubao': (
        ('const { messages, meeting } = await getConversationDetail(page, conversationId);',
         'const { messages, meeting } = await openmdStableRows(page, '
         '() => getConversationDetail(page, conversationId), x => x.messages);'),
    ),
}
_AI_SELECTORS = {
    'grok': '[data-testid="user-message"], [data-testid="assistant-message"]',
    'chatgpt': '[data-message-author-role]',
    'deepseek': '.ds-message',
    'doubao': '[data-testid="union_message"]',
    'gemini': 'user-query, model-response',
    'qwen': '[data-chat-question-wrap], [data-chat-answers-wrap]',
    'yuanbao': '.agent-chat__list__item--human, .agent-chat__list__item--ai',
}


@dataclass(frozen=True)
class Patch:
    id: str
    kind: str
    target: str
    original: str
    replacement: str
    count: int
    description: str
    versions: tuple[str, ...] = ('2.0.3',)
    verification: str = 'exact occurrence count, in registry order'


def _ai_loader_patches() -> tuple[Patch, ...]:
    patches = []
    for site, selector in _AI_SELECTORS.items():
        for operation in ('detail', 'history', 'status', 'read'):
            target = f'clis/{site}/{operation}.js'
            prefix = f'{site}-{operation}'
            description = f'{site} {operation}: Node timers, background reads and stable assistant turns'
            def add(label: str, before: str, after: str) -> None:
                patches.append(Patch(f'{prefix}-{label}', 'loader', target, before, after, 1, description))

            marker = "import { cli, Strategy } from '@jackwener/opencli/registry';"
            add('helpers', marker, _AI_HELPERS + '\n' + marker)
            page_only = operation == 'status' or (operation == 'read' and site in {'deepseek', 'doubao', 'gemini', 'yuanbao'})
            entry = 'func: async (page) => {' if page_only else 'func: async (page, kwargs) => {'
            if operation == 'detail':
                add('handler', entry, 'func: openmdMeasured(async (page, kwargs) => {\npage = openmdBackgroundPage(page);')
                add('measurement', '\n    },\n});', '\n    }, ' + json.dumps(selector) + '),\n});')
                for index, (before, after) in enumerate(_DETAIL_EDITS.get(site, []), start=1):
                    add(f'stability-{index}', before, after)
            else:
                add('handler', entry, entry + '\npage = openmdBackgroundPage(page);')
    return tuple(patches)


PATCHES = tuple(
    Patch(f'bridge-{i:02d}', 'bridge', 'dist/background.js', before, after, count, description)
    for i, (description, before, after, count) in enumerate(BRIDGE_PATCHES, start=1)
) + _ai_loader_patches() + (
    Patch('xiaoe-dispatch-registry-api', 'dispatcher', 'dist/src/registry-api.js',
          "export { cli, Strategy, getRegistry, fullName, registerCommand } from './registry.js';", '', 1,
          'Xiaoe/owned adapters import registry API through a scoped resolver', verification='exact ABI marker; no npm file mutation'),
    Patch('xiaoe-dispatch-registry', 'dispatcher', 'dist/src/registry.js', 'export function getRegistry() {', '', 1,
          'Xiaoe and Tingwu dispatcher getRegistry ABI', verification='exact ABI marker; no npm file mutation'),
    Patch('xiaoe-dispatch-execution', 'dispatcher', 'dist/src/execution.js',
          'export async function executeCommand(cmd, rawKwargs, debug = false, opts = {}) {', '', 1,
          'Xiaoe and Tingwu dispatcher executeCommand ABI', verification='exact ABI marker; no npm file mutation'),
    Patch('xiaoe-dispatch-errors', 'dispatcher', 'dist/src/errors.js', 'export class CommandExecutionError extends CliError {', '', 1,
          'Owned adapter structured errors ABI', verification='exact ABI marker; no npm file mutation'),
    Patch('xiaoe-dispatch-overlay', 'overlay', 'integrations/opencli/xiaoe/dispatch.js', '4b30b454009b0e0838aec21f2bc64ba0a1877a1221c36a9a811e89345b51f611', '', 1,
          'Scoped registry/errors resolver and ephemeral dispatcher options', verification='bundled source SHA-256'),
    Patch('tingwu-dispatch-overlay', 'overlay', 'src/openmd/runtime/tingwu_bridge.mjs', '1038f00e23ea55889e1accaafebfa054d3da8ce04527b763070df707e70e9302', '', 1,
          'Scoped cloud dispatcher; explicit cloud gate remains required', verification='bundled source SHA-256'),
)


def apply_patch(source: str, patch: Patch, *, version: str) -> str:
    if version not in patch.versions:
        raise ValueError(f'opencli_patch_version_incompatible:{patch.id}:{version}')
    if patch.kind == 'overlay':
        if hashlib.sha256(source.encode()).hexdigest() != patch.original:
            raise ValueError(f'opencli_patch_hash_mismatch:{patch.id}')
        return source
    found = source.count(patch.original)
    if found != patch.count:
        raise ValueError(f'opencli_patch_mismatch:{patch.id}:expected={patch.count}:found={found}')
    return source.replace(patch.original, patch.replacement) if patch.kind != 'dispatcher' else source


def patch_bridge(source: str, *, version: str = OPENCLI_TAG.removeprefix('v')) -> str:
    for patch in PATCHES:
        if patch.kind == 'bridge':
            source = apply_patch(source, patch, version=version)
    return source


def _bundled_bridge_source() -> str:
    path = Path(__file__).resolve().parents[1] / 'browser_extension/vendor/opencli-bridge/background.js'
    source = path.read_text(encoding='utf-8')
    if hashlib.sha256(source.encode()).hexdigest() != BRIDGE_GENERATED_SHA256:
        raise ValueError('opencli_bundled_bridge_hash_mismatch')
    source = source.split('\n', 3)[3]  # generator's three-line header
    for patch in reversed([p for p in PATCHES if p.kind == 'bridge']):
        if source.count(patch.replacement) != patch.count:
            raise ValueError(f'opencli_patch_reverse_mismatch:{patch.id}')
        source = source.replace(patch.replacement, patch.original)
    return source


def _overlay_source(patch: Patch) -> str:
    checkout = Path(__file__).resolve().parents[3] / patch.target
    if checkout.is_file():
        return checkout.read_text(encoding='utf-8')
    if patch.id == 'tingwu-dispatch-overlay':
        return Path(__file__).with_name('tingwu_bridge.mjs').read_text(encoding='utf-8')
    import importlib.metadata
    distribution = importlib.metadata.distribution('openmd-cli')
    marker = 'share/openmd/opencli/xiaoe/dispatch.js'
    for file in distribution.files or ():
        if str(file).replace('\\', '/').endswith(marker):
            return Path(distribution.locate_file(file)).read_text(encoding='utf-8')
    raise OSError('opencli_patch_overlay_missing')


def check_package_patches(package_root: Path, *, bridge_source: str | None = None, version: str | None = None) -> dict:
    bridge_scope = 'candidate_archive' if bridge_source is not None else 'bundled_extension'
    try:
        actual = json.loads((package_root / 'package.json').read_text(encoding='utf-8'))['version']
    except (OSError, ValueError, KeyError):
        actual = None
    version = version or actual or 'missing'
    results = []
    bridge_error = None
    if bridge_source is None:
        try:
            bridge_source = _bundled_bridge_source()
        except (OSError, ValueError) as exc:
            bridge_error = str(exc) if isinstance(exc, ValueError) else 'opencli_bundled_bridge_missing'
    sources = {}
    for patch in PATCHES:
        error = None
        try:
            if version != actual:
                raise ValueError('opencli_package_version_mismatch')
            if patch.kind == 'bridge':
                if bridge_error:
                    raise ValueError(bridge_error)
                bridge_source = apply_patch(bridge_source, patch, version=version)
            elif patch.kind == 'overlay':
                apply_patch(_overlay_source(patch), patch, version=version)
            else:
                if patch.target not in sources:
                    sources[patch.target] = (package_root / patch.target).read_text(encoding='utf-8')
                sources[patch.target] = apply_patch(sources[patch.target], patch, version=version)
        except (OSError, ValueError) as exc:
            error = str(exc) if isinstance(exc, ValueError) else 'opencli_patch_target_missing'
        results.append({'id': patch.id, 'kind': patch.kind, 'target': patch.target, 'ok': error is None, 'error_code': error})
    return {'total': len(results), 'ok': all(p['ok'] for p in results), 'version': actual,
            'bridge_scope': bridge_scope, 'patches': results}


def patch_diagnostics() -> dict:
    from .managed_tools import managed_package_root
    return check_package_patches(managed_package_root())


def validate_dispatcher(package_root: Path) -> None:
    version = json.loads((package_root / 'package.json').read_text(encoding='utf-8'))['version']
    for patch in PATCHES:
        if patch.kind in {'dispatcher', 'overlay'}:
            try:
                source = _overlay_source(patch) if patch.kind == 'overlay' else (package_root / patch.target).read_text(encoding='utf-8')
            except OSError:
                raise ValueError(f'opencli_patch_target_missing:{patch.id}') from None
            apply_patch(source, patch, version=version)


def loader_environment(site: str, operation: str = "detail", *, base: dict[str, str] | None = None) -> dict[str, str]:
    """Create a process-scoped loader from this registry, with no disk edits."""
    patches = [p for p in PATCHES if p.kind == 'loader' and p.target == f'clis/{site}/{operation}.js']
    if not patches:
        raise ValueError('opencli_patch_loader_not_registered:' + site + ':' + operation)
    edits = [{'id': p.id, 'target': '/' + p.target, 'before': p.original, 'after': p.replacement,
              'count': p.count, 'versions': p.versions} for p in patches]
    loader = r"""
import {readFile} from 'node:fs/promises';
export async function load(url, context, nextLoad) {
  const result = await nextLoad(url, context);
  const edits = EDITS.filter(p => new URL(url).pathname.endsWith(p.target));
  if (!edits.length) return result;
  let version;
  try { version = JSON.parse(await readFile(new URL('../../package.json', url), 'utf8')).version; }
  catch { throw new Error('opencli_patch_version_unreadable:' + edits[0].id); }
  let source = typeof result.source === 'string' ? result.source : new TextDecoder().decode(result.source);
  source = source.replace(/\r\n/g, '\n');
  for (const p of edits) {
    if (!p.versions.includes(version)) throw new Error('opencli_patch_version_incompatible:' + p.id);
    if (source.split(p.before).length !== p.count + 1) throw new Error('opencli_patch_mismatch:' + p.id);
    source = source.split(p.before).join(p.after);
  }
  return {...result, source};
}
""".replace('EDITS', json.dumps(edits))
    loader_url = 'data:text/javascript,' + quote(loader, safe='')
    bootstrap = "import {register} from 'node:module'; register(" + json.dumps(loader_url) + ', import.meta.url);'
    env = dict(os.environ if base is None else base)
    env.setdefault('OPENCLI_BROWSER_COMMAND_TIMEOUT', '120')
    env['NODE_OPTIONS'] = (env.get('NODE_OPTIONS', '') + ' --max-old-space-size=1536 --import=data:text/javascript,' + quote(bootstrap, safe='')).strip()
    return env
