/* Shared, site-neutral capture for virtualized pages.
 * The file is injected by the extension and evaluated verbatim by Kaze.
 * It deliberately has no platform or site names in it.
 * Detects CSS content-visibility:auto through the contentVisibility property.
 */
(function installOpenmdPageCapture() {
  const MAX_STEPS = 150;
  const MAX_TIME_MS = 45_000;
  const MAX_HTML_BYTES = 50 * 1024 * 1024;
  const MIN_QUIET_MS = 150;
  const MAX_QUIET_MS = 2_000;
  const MIN_STEP_SETTLE_MS = 1_400;
  const INDEX_ATTRIBUTES = ["data-index", "aria-rowindex", "data-item-index", "data-paragraph-index"];

  const numberValue = (value) => {
    if (value === null || value === undefined || value === "") return null;
    const number = Number(value);
    return Number.isFinite(number) ? number : null;
  };

  const normalizedText = (node) => (node?.innerText || node?.textContent || "")
    .replace(/\s+/g, " ").trim().slice(0, 4000);

  const hash = (value) => {
    let result = 2166136261;
    for (let index = 0; index < value.length; index += 1) {
      result ^= value.charCodeAt(index);
      result = Math.imul(result, 16777619);
    }
    return (result >>> 0).toString(16);
  };

  const indexOf = (node) => {
    for (const attribute of INDEX_ATTRIBUTES) {
      const value = numberValue(node.getAttribute?.(attribute));
      if (value !== null) return value;
    }
    return null;
  };

  const directOrOutermost = (nodes) => nodes.filter((node, index) => {
    for (let parent = node.parentElement; parent; parent = parent.parentElement) {
      if (nodes.includes(parent)) return false;
    }
    return index >= 0;
  });

  const scrollState = (container) => ({
    top: container === document.documentElement || container === document.body ? window.scrollY : container.scrollTop,
    left: container === document.documentElement || container === document.body ? window.scrollX : container.scrollLeft,
  });

  const setScroll = (container, top, left) => {
    if (container === document.documentElement || container === document.body) window.scrollTo(left, top);
    else container.scrollTo({ top, left, behavior: "auto" });
  };

  const scrollable = (node) => {
    if (!node) return false;
    const height = node === document.documentElement || node === document.body ? window.innerHeight : node.clientHeight;
    const total = node === document.documentElement || node === document.body ? document.documentElement.scrollHeight : node.scrollHeight;
    return total > Math.max(1, height) * 1.5;
  };

  const declaredTotal = (container) => {
    const text = normalizedText(container).slice(0, 20000);
    const patterns = [
      /(?:共|总计|合计)\s*([0-9]{1,7})\s*(?:条|项|段|条消息|篇|个)?/i,
      /(?:of|total)\s*([0-9]{1,7})/i,
    ];
    for (const pattern of patterns) {
      const match = text.match(pattern);
      if (match) return Number(match[1]);
    }
    return null;
  };

  const inspect = (container) => {
    const descendants = [...container.querySelectorAll("*")].slice(0, 12000);
    const indexed = descendants.filter((node) => indexOf(node) !== null).length;
    let absoluteTranslated = 0;
    let contentVisible = 0;
    for (const node of descendants) {
      let style;
      try { style = getComputedStyle(node); } catch { style = null; }
      if (!style) continue;
      if (style.position === "absolute" && /translate(?:3d|Y|X)?\s*\(/i.test(style.transform || "")) absoluteTranslated += 1;
      if (style.contentVisibility === "auto") contentVisible += 1;
    }
    const total = declaredTotal(container);
    const visibleItems = indexed || descendants.filter((node) => normalizedText(node)).length;
    const hasSignal = indexed > 0 || absoluteTranslated >= 2 || contentVisible > 0 || (total !== null && total > visibleItems);
    return { indexed, absoluteTranslated, contentVisible, declaredTotal: total, hasSignal };
  };

  const findContainer = () => {
    const candidates = [document.scrollingElement, document.documentElement, document.body,
      ...[...document.querySelectorAll("*")].filter((node) => scrollable(node))];
    const seen = new Set();
    let best = null;
    for (const candidate of candidates) {
      if (!candidate || seen.has(candidate) || !scrollable(candidate)) continue;
      seen.add(candidate);
      const signals = inspect(candidate);
      if (!signals.hasSignal) continue;
      const score = (signals.indexed ? 8 : 0) + (signals.contentVisible ? 4 : 0) +
        (signals.absoluteTranslated >= 2 ? 3 : 0) + (signals.declaredTotal !== null ? 2 : 0);
      if (!best || score > best.score) best = { container: candidate, signals, score };
    }
    return best;
  };

  const nodesFor = (container, signals) => {
    const descendants = [...container.querySelectorAll("*")].slice(0, 12000);
    let nodes = descendants.filter((node) => indexOf(node) !== null);
    if (nodes.length) return directOrOutermost(nodes);
    nodes = descendants.filter((node) => {
      let style;
      try { style = getComputedStyle(node); } catch { style = null; }
      return style && style.contentVisibility === "auto" && normalizedText(node);
    });
    if (nodes.length) return directOrOutermost(nodes);
    if (signals.absoluteTranslated >= 2) {
      nodes = descendants.filter((node) => {
        let style;
        try { style = getComputedStyle(node); } catch { style = null; }
        return style && style.position === "absolute" && /translate(?:3d|Y|X)?\s*\(/i.test(style.transform || "") && normalizedText(node);
      });
      if (nodes.length) return directOrOutermost(nodes);
    }
    return [...container.children].filter((node) => normalizedText(node));
  };

  const nodeKey = (node) => {
    const index = indexOf(node);
    if (index !== null) return `i:${index}`;
    return `h:${hash(`${node.tagName}:${normalizedText(node)}`)}`;
  };

  const nodePath = (node) => {
    const path = [];
    for (let current = node; current && current !== document.documentElement; current = current.parentElement) {
      path.unshift([...current.parentElement.children].indexOf(current));
    }
    return path;
  };

  const atPath = (root, path) => {
    let current = root;
    for (const index of path) current = current?.children?.[index];
    return current;
  };

  const waitQuiet = (container, minimumMs = MIN_QUIET_MS) => new Promise((resolve) => {
    const start = Date.now();
    let lastMutation = start;
    let timer;
    const observer = typeof MutationObserver === "function" ? new MutationObserver(() => { lastMutation = Date.now(); }) : null;
    observer?.observe(container, { subtree: true, childList: true, attributes: true, characterData: true });
    const finish = () => { observer?.disconnect(); clearTimeout(timer); resolve(); };
    const poll = () => {
      const elapsed = Date.now() - start;
      if (elapsed >= MAX_QUIET_MS || (Date.now() - lastMutation >= MIN_QUIET_MS && elapsed >= minimumMs)) return finish();
      timer = setTimeout(poll, MIN_QUIET_MS);
    };
    timer = setTimeout(poll, MIN_QUIET_MS);
  });

  const hiddenMarkedHtml = (clone) => {
    const originals = document.documentElement?.querySelectorAll("*") || [];
    const copies = clone.querySelectorAll("*");
    originals.forEach((node, index) => {
      const copy = copies[index];
      let style;
      try { style = getComputedStyle(node); } catch { style = null; }
      if (copy && style && (style.display === "none" || style.visibility === "hidden")) copy.setAttribute("data-openmd-hidden", "true");
    });
    clone.querySelectorAll("script, style, noscript, input, textarea").forEach((node) => node.remove());
    return clone;
  };

  const buildHtml = (rootPath, container, collected) => {
    const clone = document.documentElement.cloneNode(true);
    const target = atPath(clone, rootPath);
    const replacement = target === clone ? clone.querySelector("body") || clone : target;
    if (replacement && collected.length) replacement.innerHTML = collected.map((entry) => entry.node.outerHTML).join("");
    return hiddenMarkedHtml(clone).outerHTML;
  };

  const capturePage = async () => {
    const originalHtml = document.documentElement?.outerHTML || "";
    const base = {
      coverage: "full_page", virtual_detected: false, stop_reason: "not_virtual", steps: 0,
      elapsed_ms: 0, seen_count: 0, new_count: 0, duplicate_count: 0, collected_count: 0,
      max_index: null, index_gaps: [], declared_total: null,
    };
    const selected = findContainer();
    if (!selected) return { html: originalHtml, url: location.href, title: document.title || location.href, capture_meta: base };
    const { container, signals } = selected;
    const rootPath = nodePath(container);
    const originalPosition = scrollState(container);
    const started = Date.now();
    const entries = new Map();
    let seenCount = 0;
    let newCount = 0;
    let duplicateCount = 0;
    let steps = 0;
    let stopReason = "end_reached";
    let indexGaps = [];
    let html = originalHtml;
    try {
      setScroll(container, 0, originalPosition.left);
      const collect = () => {
        for (const node of nodesFor(container, signals)) {
          const key = nodeKey(node);
          seenCount += 1;
          if (entries.has(key)) { duplicateCount += 1; continue; }
          entries.set(key, { node: node.cloneNode(true), index: indexOf(node) });
          newCount += 1;
        }
      };
      collect();
      let noProgress = 0;
      while (steps < MAX_STEPS && Date.now() - started < MAX_TIME_MS) {
        const height = container === document.documentElement || container === document.body ? window.innerHeight : container.clientHeight;
        const total = container === document.documentElement || container === document.body ? document.documentElement.scrollHeight : container.scrollHeight;
        const current = container === document.documentElement || container === document.body ? window.scrollY : container.scrollTop;
        const next = Math.min(current + Math.max(200, height * 0.58), Math.max(0, total - height));
        if (next <= current + 1) break;
        const before = newCount;
        setScroll(container, next, originalPosition.left);
        steps += 1;
        await waitQuiet(container, MIN_STEP_SETTLE_MS);
        collect();
        const after = container === document.documentElement || container === document.body ? window.scrollY : container.scrollTop;
        const moved = after > current + 1;
        noProgress = newCount === before && !moved ? noProgress + 1 : 0;
        if (noProgress >= 3) { stopReason = "no_progress"; break; }
        const afterHeight = container === document.documentElement || container === document.body ? window.innerHeight : container.clientHeight;
        const afterTotal = container === document.documentElement || container === document.body ? document.documentElement.scrollHeight : container.scrollHeight;
        const max = Math.max(0, afterTotal - afterHeight);
        if (after >= max - 1) {
          await waitQuiet(container, MIN_STEP_SETTLE_MS); collect();
          await waitQuiet(container, MIN_STEP_SETTLE_MS); collect();
          break;
        }
      }
      if (steps >= MAX_STEPS) stopReason = "step_limit";
      else if (Date.now() - started >= MAX_TIME_MS) stopReason = "time_limit";
      const indexed = [...entries.values()].map((entry) => entry.index).filter((value) => value !== null).sort((a, b) => a - b);
      const unique = [...new Set(indexed)];
      const inferredOneBased = unique.length > 0 && unique[0] === 1 &&
        stopReason === "end_reached" && unique.length === unique[unique.length - 1];
      const oneBased = unique.length > 0 && unique[0] === 1 && (
        (signals.declaredTotal !== null && unique[unique.length - 1] <= signals.declaredTotal) ||
        (signals.declaredTotal === null && inferredOneBased)
      );
      const expected = signals.declaredTotal !== null ? signals.declaredTotal :
        (unique.length ? unique[unique.length - 1] + (inferredOneBased ? 0 : 1) : 0);
      const indexSet = new Set(unique);
      if (unique.length) {
        const first = oneBased ? 1 : 0;
        const last = oneBased ? expected : expected - 1;
        for (let index = first; index <= last; index += 1) if (!indexSet.has(index)) indexGaps.push(index);
      }
      const ordered = [...entries.values()].sort((left, right) => left.index === null ? (right.index === null ? 0 : 1) : right.index === null ? -1 : left.index - right.index);
      const candidateHtml = buildHtml(rootPath, container, ordered);
      if (new Blob([candidateHtml]).size > MAX_HTML_BYTES) {
        stopReason = "size_limit";
        html = originalHtml;
      } else html = candidateHtml;
      const complete = stopReason === "end_reached" && indexGaps.length === 0;
      const capture = {
        coverage: complete ? "scrolled_full" : "visible_only", virtual_detected: true, stop_reason: stopReason,
        steps, elapsed_ms: Date.now() - started, seen_count: seenCount, new_count: newCount,
        duplicate_count: duplicateCount, collected_count: entries.size,
        max_index: unique.length ? unique[unique.length - 1] : null, index_gaps: indexGaps,
        declared_total: signals.declaredTotal,
      };
      return { html, url: location.href, title: document.title || location.href, capture_meta: capture };
    } catch (error) {
      return { html: originalHtml, url: location.href, title: document.title || location.href, capture_meta: {
        ...base, virtual_detected: true, coverage: "visible_only", stop_reason: "error",
        steps, elapsed_ms: Date.now() - started, seen_count: seenCount, new_count: newCount,
        duplicate_count: duplicateCount, collected_count: entries.size,
        declared_total: signals.declaredTotal,
      } };
    } finally {
      setScroll(container, originalPosition.top, originalPosition.left);
    }
  };

  globalThis.__OPENMD_PAGE_CAPTURE__ = capturePage;
})();
