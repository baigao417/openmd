"""Process-local, marker-checked patches for the pinned AI detail modules."""
from __future__ import annotations

import json
import os
from urllib.parse import quote


# Chrome throttles timers in collapsed/inactive tabs. Read polling is scheduled
# in Node, never with page-side DOM stability timers. No focus/tab mutation.
_HELPERS = r'''
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


def detail_loading_environment(site: str, operation: str = "detail") -> dict[str, str]:
    """Only the selected detail module is rewritten; source drift fails closed."""
    edits = []
    if site in {"grok", "qwen", "yuanbao"}:
        edits = [
            ["let bubbles = [];", "let bubbles = []; let grokPrevious = ''; let grokStablePolls = 0; let grokStable = false;"],
            ["const POLL_DEADLINE_MS = 20_000;", "const POLL_DEADLINE_MS = 45_000;"],
            ["if (bubbles.length > 0) break;", """
const signature = JSON.stringify(bubbles.map(b => [b.role, b.text, b.html]));
grokStablePolls = signature === grokPrevious ? grokStablePolls + 1 : 0;
grokPrevious = signature;
if (bubbles.length > 0 && bubbles.some(b => /^assistant$/i.test(b.role)) && grokStablePolls >= 2 && !(await openmdGenerating(page))) {grokStable = true; break;}
"""],
            ["if (!bubbles.length)", "if (bubbles.length && !grokStable) throw Object.assign(new Error('grok_detail_not_stable'), {code:'AI_CHAT_NOT_STABLE',exitCode:75});\nif (!bubbles.length)"],
        ]
    elif site == "deepseek":
        edits = [["await ensureOnDeepSeek(page);", "// Navigate to the explicit conversation once; no redundant home wait."],
                 ["const messages = await getVisibleMessages(page);", "const messages = await openmdStableRows(page, () => getVisibleMessages(page));"]]
    elif site == "gemini":
        edits = [["const turns = await getGeminiVisibleTurns(page);", "const turns = await openmdStableRows(page, () => getGeminiVisibleTurns(page));"]]
    elif site == "doubao":
        edits = [["const { messages, meeting } = await getConversationDetail(page, conversationId);", "const { messages, meeting } = await openmdStableRows(page, () => getConversationDetail(page, conversationId), x => x.messages);"]]
    elif site != "chatgpt":
        raise ValueError("ai_chat_loading_site_unknown")
    if operation not in {"detail", "status", "history", "read"}:
        raise ValueError("ai_chat_loading_operation_unknown")
    selectors = {
        "grok": '[data-testid="user-message"], [data-testid="assistant-message"]',
        "chatgpt": '[data-message-author-role]',
        "deepseek": '.ds-message',
        "doubao": '[data-testid="union_message"]',
        "gemini": 'user-query, model-response',
        "qwen": '[data-chat-question-wrap], [data-chat-answers-wrap]',
        "yuanbao": '.agent-chat__list__item--human, .agent-chat__list__item--ai',
    }
    loader = "const selected = " + json.dumps(site) + ";\n"
    loader += "const operation = " + json.dumps(operation) + ";\n"
    loader += "const turnSelector = " + json.dumps(selectors[site]) + ";\n"
    loader += "const helpers = " + json.dumps(_HELPERS) + ";\n"
    loader += "const edits = " + json.dumps(edits if operation == "detail" else []) + ";\n"
    loader += r'''
export async function load(url, context, nextLoad) {
  const result = await nextLoad(url, context);
  if (!new URL(url).pathname.endsWith('/clis/' + selected + '/' + operation + '.js')) return result;
  let source = typeof result.source === 'string' ? result.source : new TextDecoder().decode(result.source);
  source = source.replace(/\r\n/g, '\n');
  for (const [before, after] of edits) {
    if (source.split(before).length !== 2) throw new Error(selected + '_detail_patch_incompatible');
    source = source.replace(before, after);
  }
  let entry = 'func: async (page, kwargs) => {';
  if (!source.includes(entry)) entry = 'func: async (page) => {';
  // The standalone Grok timing contract exports detail(page) directly.
  if (source.includes(entry)) source = source.replace(entry, entry + '\npage = openmdBackgroundPage(page);');
  else if (source.includes('site:')) throw new Error(selected + '_detail_patch_incompatible');
  if (operation === 'detail' && source.includes('site:')) {
    if (source.split('\n    },\n});').length !== 2) throw new Error(selected + '_detail_patch_incompatible');
    source = source.replace('func: async (page, kwargs) => {', 'func: openmdMeasured(async (page, kwargs) => {');
    source = source.replace('\n    },\n});', '\n    }, ' + JSON.stringify(turnSelector) + '),\n});');
  }
  return {...result, source: helpers + '\n' + source};
}
'''
    loader_url = "data:text/javascript," + quote(loader, safe="")
    bootstrap = "import {register} from 'node:module'; register(" + json.dumps(loader_url) + ", import.meta.url);"
    env = dict(os.environ)
    env.setdefault("OPENCLI_BROWSER_COMMAND_TIMEOUT", "120")
    env["NODE_OPTIONS"] = (env.get("NODE_OPTIONS", "") + " --max-old-space-size=1536 --import=data:text/javascript," + quote(bootstrap, safe="")).strip()
    return env
