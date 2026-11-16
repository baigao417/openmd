const HELPER_CONFIGS = {
  xiaoe: {
    kind: "xiaoe",
    baseUrl: "http://127.0.0.1:19826",
    appTitle: "小鹅通转写",
    subtitle: "当前页一键转 Markdown",
    helperCommand: "opencli xiaoe helper-start",
    helperRunning: "Helper 已运行",
    helperStopped: "Helper 未运行",
    pageKindLabel: "已识别为小鹅通视频/直播页",
    defaultTitle: "未识别到课程标题",
    runLabel: "开始转写当前页",
    outputDirLabel: "默认保存目录",
    showProvider: true,
    buildRunPayload(tabInfo) {
      const outputDir = document.getElementById("output-dir").value.trim();
      return {
        url: tabInfo.url,
        title: tabInfo.title,
        provider: document.getElementById("provider").value,
        ...(outputDir ? { outDirOverride: outputDir } : {})
      };
    }
  },
  tingwu: {
    kind: "tingwu",
    baseUrl: "http://127.0.0.1:19827",
    appTitle: "通义听悟导出",
    subtitle: "当前页一键导出 Markdown",
    helperCommand: "opencli tingwu helper-start",
    helperRunning: "Helper 已运行",
    helperStopped: "Helper 未运行",
    pageKindLabel: "已识别为通义听悟逐字稿页",
    defaultTitle: "未识别到逐字稿标题",
    runLabel: "导出当前页逐字稿",
    outputDirLabel: "默认保存目录",
    showProvider: false,
    buildRunPayload(tabInfo) {
      const outputDir = document.getElementById("output-dir").value.trim();
      return {
        url: tabInfo.url,
        title: tabInfo.title,
        tabId: tabInfo.tabId,
        ...(outputDir ? { outDirOverride: outputDir } : {})
      };
    }
  }
};

const BATCH_AUTO_LOAD_STORAGE_KEY = "opencli.xiaoe.batchAutoLoad";

const POPUP_STATE = {
  helperReady: false,
  helperToken: "",
  pageSupported: false,
  batchItems: [],
  batchCourseUrlDirty: false,
  batchAutoKey: "",
  batchLoading: false,
  taskRunning: false,
  form: {
    outputDir: { dirty: false, lastSynced: "" },
    provider: { dirty: false, lastSynced: "auto" }
  }
};

let batchCatalogRestoreTimer = null;

function clearBatchCatalogRestoreTimer() {
  if (batchCatalogRestoreTimer) {
    window.clearTimeout(batchCatalogRestoreTimer);
    batchCatalogRestoreTimer = null;
  }
}

function restoreBatchAutoLoadPreference() {
  const checkbox = document.getElementById("batch-auto-load");
  if (!checkbox) {
    return;
  }
  const saved = localStorage.getItem(BATCH_AUTO_LOAD_STORAGE_KEY);
  if (saved === "true" || saved === "false") {
    checkbox.checked = saved === "true";
  }
}

function persistBatchAutoLoadPreference() {
  const checkbox = document.getElementById("batch-auto-load");
  if (!checkbox) {
    return;
  }
  localStorage.setItem(BATCH_AUTO_LOAD_STORAGE_KEY, checkbox.checked ? "true" : "false");
}

function setStatus(dotId, textId, kind, text) {
  const dot = document.getElementById(dotId);
  const node = document.getElementById(textId);
  dot.className = `dot ${kind}`;
  node.textContent = text;
}

function isKnownXiaoeHost(hostname) {
  return hostname.endsWith(".xet.citv.cn")
    || hostname.endsWith(".xet.pomoho.com")
    || hostname === "xiaoeknow.com"
    || hostname.endsWith(".xiaoeknow.com")
    || hostname === "xiaoecloud.com"
    || hostname.endsWith(".xiaoecloud.com");
}

function isLikelyXiaoeUrl(url) {
  try {
    return isKnownXiaoeHost(new URL(url).hostname);
  } catch {
    return false;
  }
}

function isXiaoeCourseCatalogUrl(url) {
  try {
    const parsed = new URL(url);
    if (!isKnownXiaoeHost(parsed.hostname)) {
      return false;
    }
    const pathname = parsed.pathname.replace(/\/+$/, "");
    if (/^\/(?:v\d+\/)?course\/(?:text|audio|alive|video)\//.test(pathname)
      || /^\/p\/course\/(?:text|audio|alive|video)\//.test(pathname)) {
      return false;
    }
    return /^\/p\/course\/[^/]+/.test(pathname)
      || /^\/(?:v\d+\/)?course\/column\/[^/]+/.test(pathname);
  } catch {
    return false;
  }
}

function hasXiaoeQueryHints(parsed) {
  return ["app_id", "product_id", "pro_id", "conduit_id"].some((key) => parsed.searchParams.has(key));
}

function isSupportedXiaoePath(pathname) {
  return /\/(?:p\/)?course\/video\//.test(pathname)
    || /\/(?:v4\/)?course\/alive\//.test(pathname);
}

function detectXiaoePage(url, title) {
  if (!url) return { supported: false, reason: "当前页面没有可用链接" };
  try {
    const parsed = new URL(url);
    const isXiaoeHost = isKnownXiaoeHost(parsed.hostname);
    const isSupportedPath = isSupportedXiaoePath(parsed.pathname);
    if (!isSupportedPath || (!isXiaoeHost && !hasXiaoeQueryHints(parsed))) {
      return { supported: false, reason: "当前页面不是支持的小鹅通视频或直播回放页" };
    }
    return { supported: true, title: (title || "").trim() };
  } catch {
    return { supported: false, reason: "当前页面链接无效" };
  }
}

function detectTingwuPage(url, title) {
  if (!url) return { supported: false, reason: "当前页面没有可用链接" };
  try {
    const parsed = new URL(url);
    const isTingwuHost = parsed.hostname === "tingwu.aliyun.com";
    const looksLikeTranscriptPath = /\/(?:doc\/)?transcripts\//.test(parsed.pathname);
    if (!isTingwuHost || !looksLikeTranscriptPath) {
      return { supported: false, reason: "当前页面不是支持的通义听悟逐字稿页" };
    }
    return { supported: true, title: (title || "").trim() };
  } catch {
    return { supported: false, reason: "当前页面链接无效" };
  }
}

function detectCurrentPage(url, title) {
  const tingwu = detectTingwuPage(url, title);
  if (tingwu.supported) {
    return { ...tingwu, kind: "tingwu" };
  }
  const xiaoe = detectXiaoePage(url, title);
  if (xiaoe.supported) {
    return { ...xiaoe, kind: "xiaoe" };
  }
  return {
    supported: false,
    kind: null,
    title: (title || "").trim(),
    reason: tingwu.reason || xiaoe.reason || "当前页面暂不支持"
  };
}

function humanizeHelperError(status, body) {
  const raw = body?.error || body?.message || `helper request failed: ${status}`;
  if (status === 400 && raw.includes("courseUrl")) return "请填写课程 URL";
  if (status === 400 && raw.includes("保存目录")) return "请先设置默认保存目录";
  if (status === 401) return "Helper 安全令牌缺失或已过期，请重启 helper 后重新打开扩展";
  if (status === 403) return "当前来源没有权限访问 Helper，请从 OpenCLI 扩展内操作";
  if (status === 409) return "已有任务正在运行，请等待完成后再开始";
  if (status === 501) return "当前 Helper 版本不支持该能力，请更新或重启 opencli xiaoe helper-start";
  return raw;
}

async function fetchHelper(baseUrl, path, init = {}) {
  const headers = {
    "Content-Type": "application/json",
    ...(POPUP_STATE.helperToken ? { "X-OpenCLI-Helper-Token": POPUP_STATE.helperToken } : {}),
    ...(init.headers || {})
  };
  const response = await fetch(`${baseUrl}${path}`, {
    ...init,
    headers
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(humanizeHelperError(response.status, body));
  }
  if (body.security?.token) {
    POPUP_STATE.helperToken = body.security.token;
  }
  return body;
}

async function getCurrentTabInfo() {
  const tabs = await chrome.tabs.query({ active: true, currentWindow: true });
  const tab = tabs[0];
  return {
    tabId: tab?.id,
    url: tab?.url || "",
    title: tab?.title || ""
  };
}

async function getBridgeStatus() {
  return await new Promise((resolve) => {
    chrome.runtime.sendMessage({ type: "getStatus" }, (resp) => {
      if (chrome.runtime.lastError || !resp) {
        resolve({ connected: false, reconnecting: false });
        return;
      }
      resolve(resp);
    });
  });
}

function renderTask(state) {
  const summary = document.getElementById("task-summary");
  const output = document.getElementById("task-output");
  const log = document.getElementById("task-log");
  if (!state) {
    summary.textContent = "暂无任务";
    output.textContent = "";
    log.textContent = "等待任务状态…";
    return;
  }
  const lastRun = state.lastRun || {};
  summary.textContent = `${state.status}${state.currentStage ? ` · ${state.currentStage}` : ""}`;
  output.textContent = lastRun.outputPath || lastRun.error || "";
  log.textContent = (state.log || []).length > 0 ? state.log.join("\n") : "暂无日志";
}

function isStateRunning(state) {
  return state?.status === "running";
}

function getBatchFormPayload() {
  const outputDir = document.getElementById("output-dir").value.trim();
  const courseUrl = document.getElementById("batch-course-url").value.trim();
  const provider = document.getElementById("provider").value;
  const type = document.getElementById("batch-type-filter").value.trim() || "直播";
  const status = document.getElementById("batch-status-filter").value.trim() || "all";
  const chapter = document.getElementById("batch-chapter-filter").value.trim();
  const title = document.getElementById("batch-title-filter").value.trim();
  const limit = Number.parseInt(document.getElementById("batch-limit").value || "0", 10);
  const timeoutMinutes = Number.parseInt(document.getElementById("batch-timeout").value || "180", 10);
  const restart = document.getElementById("batch-restart")?.checked === true;
  return {
    courseUrl,
    outDir: outputDir,
    provider,
    type,
    status,
    ...(chapter ? { chapter } : {}),
    ...(title ? { title } : {}),
    limit: Number.isFinite(limit) && limit >= 0 ? limit : 0,
    timeoutMinutes: Number.isFinite(timeoutMinutes) && timeoutMinutes > 0 ? timeoutMinutes : 180,
    skipExisting: true,
    resume: true,
    restart,
    dryRun: false
  };
}

function selectedBatchIds() {
  return [...document.querySelectorAll(".batch-check:checked")].map((node) => node.value);
}

function shouldSelectBatchItemByDefault(totalCount) {
  return totalCount <= 10;
}

function batchItemsMatch(leftItems, rightItems) {
  if (leftItems.length !== rightItems.length) {
    return false;
  }
  return leftItems.every((item, index) => (item.resource_id || "") === (rightItems[index]?.resource_id || ""));
}

function renderBatchItems() {
  const list = document.getElementById("batch-list");
  const count = document.getElementById("batch-count");
  const existingChecks = [...document.querySelectorAll(".batch-check")];
  const shouldPreserveSelection = existingChecks.length > 0;
  const previousSelectedIds = new Set(existingChecks.filter((node) => node.checked).map((node) => node.value));
  list.textContent = "";
  if (POPUP_STATE.batchItems.length === 0) {
    const empty = document.createElement("div");
    empty.className = "batch-empty";
    empty.textContent = "先读取课程目录，然后勾选要转写的直播内容。";
    list.appendChild(empty);
    count.textContent = "未读取目录";
    updateActionButtons();
    return;
  }

  for (const item of POPUP_STATE.batchItems) {
    const row = document.createElement("label");
    row.className = "batch-row";
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.className = "batch-check";
    const itemId = item.resource_id || "";
    checkbox.value = itemId;
    checkbox.checked = shouldPreserveSelection
      ? previousSelectedIds.has(itemId)
      : shouldSelectBatchItemByDefault(POPUP_STATE.batchItems.length);
    checkbox.addEventListener("change", updateActionButtons);

    const content = document.createElement("div");
    const title = document.createElement("div");
    title.className = "title";
    title.textContent = item.title || "(无标题)";
    const meta = document.createElement("div");
    meta.className = "batch-meta";
    meta.textContent = [item.chapter, item.type, item.status, item.resource_id].filter(Boolean).join(" · ");
    content.appendChild(title);
    content.appendChild(meta);

    row.appendChild(checkbox);
    row.appendChild(content);
    list.appendChild(row);
  }
  count.textContent = `已读取 ${POPUP_STATE.batchItems.length} 条，已选择 ${selectedBatchIds().length} 条`;
  updateActionButtons();
}

function renderBatchStatus(batchStatus) {
  const node = document.getElementById("batch-status");
  if (!batchStatus) {
    node.textContent = "批量任务状态：未开始";
    return;
  }
  const counts = batchStatus.progress?.counts;
  const countsText = counts
    ? `共 ${counts.total} 条，成功 ${counts.success} 条，跳过 ${counts.skipped} 条，失败 ${counts.failed} 条，待处理 ${counts.pending} 条`
    : "";
  const detail = batchStatus.progress?.detail ? ` · ${batchStatus.progress.detail}` : "";
  const result = batchStatus.result ? ` · statePath=${batchStatus.result.statePath || ""}` : "";
  const error = batchStatus.error ? ` · ${batchStatus.error}` : "";
  node.textContent = `批量任务状态：${batchStatus.status}${detail}${countsText ? ` · ${countsText}` : ""}${result}${error}`;
}

function applyCatalogStatus(catalogStatus) {
  if (!catalogStatus?.key || catalogStatus.key !== batchAutoLoadKey()) {
    return false;
  }

  POPUP_STATE.batchAutoKey = catalogStatus.key;
  if (catalogStatus.status === "running") {
    POPUP_STATE.batchLoading = true;
    renderBatchStatus({ status: "running", progress: { detail: "catalog: 后台读取中" } });
    return true;
  }

  POPUP_STATE.batchLoading = false;
  if (catalogStatus.status === "success") {
    const nextItems = catalogStatus.items || [];
    const hasRenderedItems = document.querySelectorAll(".batch-check").length > 0;
    if (!hasRenderedItems || !batchItemsMatch(POPUP_STATE.batchItems, nextItems)) {
      POPUP_STATE.batchItems = nextItems;
      renderBatchItems();
    }
    renderBatchStatus({ status: "idle", progress: { detail: `catalog: ${catalogStatus.total || 0}` } });
    return true;
  }

  if (catalogStatus.status === "error") {
    POPUP_STATE.batchItems = [];
    renderBatchItems();
    renderBatchStatus({ status: "error", error: catalogStatus.error || "读取目录失败" });
    return true;
  }

  return false;
}

function scheduleCatalogStatusRestore() {
  clearBatchCatalogRestoreTimer();
  batchCatalogRestoreTimer = window.setTimeout(() => {
    void restoreBatchCatalogFromHelper();
  }, 2000);
}

async function restoreBatchCatalogFromHelper() {
  if (document.body.dataset.mode !== "xiaoe" || !POPUP_STATE.helperReady) {
    return false;
  }
  try {
    const result = await fetchHelper(HELPER_CONFIGS.xiaoe.baseUrl, "/catalog-status");
    const applied = applyCatalogStatus(result.catalog);
    if (result.catalog?.status === "running" && result.catalog?.key === batchAutoLoadKey()) {
      scheduleCatalogStatusRestore();
    } else {
      clearBatchCatalogRestoreTimer();
    }
    updateActionButtons();
    return applied;
  } catch {
    return false;
  }
}

function resetFormSyncState() {
  POPUP_STATE.form.outputDir = { dirty: false, lastSynced: "" };
  POPUP_STATE.form.provider = { dirty: false, lastSynced: "auto" };
}

function syncInputFromHelper(element, stateKey, nextValue) {
  const state = POPUP_STATE.form[stateKey];
  const normalized = nextValue ?? "";
  if (!state.dirty || element.value === normalized) {
    element.value = normalized;
    state.dirty = false;
  }
  state.lastSynced = normalized;
}

function markFieldDirty(stateKey, value) {
  const state = POPUP_STATE.form[stateKey];
  state.dirty = value !== state.lastSynced;
}

function getRunCurrentReason() {
  const hasOutputDir = document.getElementById("output-dir").value.trim().length > 0;
  if (!POPUP_STATE.helperReady) return "请先启动 Helper";
  if (!POPUP_STATE.pageSupported) return "当前页面不是支持的小鹅通视频/直播回放页";
  if (!hasOutputDir) return "请先填写默认保存目录";
  return "当前页可直接转写";
}

function getBatchActionReason() {
  const isXiaoe = document.body.dataset.mode === "xiaoe";
  const hasOutputDir = document.getElementById("output-dir").value.trim().length > 0;
  const hasCatalogUrl = isXiaoeCourseCatalogUrl(document.getElementById("batch-course-url")?.value.trim() || "");
  const selectedCount = selectedBatchIds().length;
  const restart = document.getElementById("batch-restart")?.checked === true;
  if (!isXiaoe) return "批量转写仅支持小鹅通课程";
  if (!POPUP_STATE.helperReady) return "请先启动 Helper";
  if (!hasOutputDir) return "请先填写默认保存目录";
  if (!hasCatalogUrl) return "请打开课程目录页，或手动粘贴课程目录 URL";
  if (POPUP_STATE.batchLoading) return "正在读取目录，请稍候";
  if (POPUP_STATE.batchItems.length === 0) return "请先读取目录";
  if (selectedCount === 0) return "请至少选择一条内容";
  if (restart) return `已选择 ${selectedCount} 条，将忽略旧 batch-state.json 重新开始`;
  return `已选择 ${selectedCount} 条，可开始批量转写`;
}

function renderActionReasons() {
  document.getElementById("run-current-reason").textContent = getRunCurrentReason();
  const batchReason = document.getElementById("batch-action-reason");
  if (batchReason) {
    batchReason.textContent = getBatchActionReason();
  }
}

function updateActionButtons() {
  const hasOutputDir = document.getElementById("output-dir").value.trim().length > 0;
  document.getElementById("run-current").disabled = !(POPUP_STATE.helperReady && POPUP_STATE.pageSupported && hasOutputDir);
  document.getElementById("save-config").disabled = !POPUP_STATE.helperReady;
  document.getElementById("stop-current-task").disabled = !(POPUP_STATE.helperReady && POPUP_STATE.taskRunning);
  const isXiaoe = document.body.dataset.mode === "xiaoe";
  const hasCatalogUrl = isXiaoeCourseCatalogUrl(document.getElementById("batch-course-url")?.value.trim() || "");
  const selectedCount = selectedBatchIds().length;
  document.getElementById("batch-load").disabled = POPUP_STATE.batchLoading || !(isXiaoe && POPUP_STATE.helperReady && hasOutputDir && hasCatalogUrl);
  document.getElementById("batch-run").disabled = POPUP_STATE.batchLoading || !(isXiaoe && POPUP_STATE.helperReady && hasOutputDir && hasCatalogUrl && selectedCount > 0);
  document.getElementById("batch-select-all").disabled = POPUP_STATE.batchItems.length === 0;
  document.getElementById("batch-clear-all").disabled = POPUP_STATE.batchItems.length === 0;
  if (POPUP_STATE.batchItems.length > 0) {
    document.getElementById("batch-count").textContent = `已读取 ${POPUP_STATE.batchItems.length} 条，已选择 ${selectedCount} 条`;
  }
  renderActionReasons();
}

function applyModeUi(mode, pageInfo, tabInfo) {
  const config = HELPER_CONFIGS[mode];
  document.getElementById("app-title").textContent = config.appTitle;
  document.getElementById("app-subtitle").textContent = config.subtitle;
  document.getElementById("helper-command").textContent = config.helperCommand;
  document.getElementById("page-kind").textContent = pageInfo.supported ? config.pageKindLabel : "当前页尚未匹配支持的入口";
  document.getElementById("page-title").textContent = pageInfo.title || tabInfo.title || config.defaultTitle;
  document.getElementById("page-url").textContent = tabInfo.url || "";
  document.getElementById("page-reason").textContent = pageInfo.supported ? "当前页可直接执行" : (pageInfo.reason || "");
  document.getElementById("output-dir-label").textContent = config.outputDirLabel;
  document.getElementById("run-current").textContent = config.runLabel;
  document.getElementById("provider-field").classList.toggle("hidden", !config.showProvider);
  document.getElementById("batch-card").classList.toggle("hidden", mode !== "xiaoe");
}

function syncBatchCourseUrl(tabInfo) {
  const input = document.getElementById("batch-course-url");
  if (POPUP_STATE.batchCourseUrlDirty) {
    return;
  }
  if (isXiaoeCourseCatalogUrl(tabInfo.url)) {
    if (input.value !== tabInfo.url) {
      input.value = tabInfo.url;
      POPUP_STATE.batchAutoKey = "";
    }
    return;
  }
  if (input.value.trim()) {
    input.value = "";
    POPUP_STATE.batchItems = [];
    POPUP_STATE.batchAutoKey = "";
    renderBatchItems();
  }
}

async function refreshPopup() {
  const bridge = await getBridgeStatus();
  const bridgeHint = document.getElementById("bridge-hint");
  if (bridge.connected) {
    setStatus("bridge-dot", "bridge-text", "connected", "Bridge 已连接");
    bridgeHint.classList.add("hidden");
  } else if (bridge.reconnecting) {
    setStatus("bridge-dot", "bridge-text", "connecting", "Bridge 重连中");
    bridgeHint.classList.add("hidden");
  } else {
    setStatus("bridge-dot", "bridge-text", "disconnected", "Bridge 未连接");
    bridgeHint.classList.remove("hidden");
  }

  const tabInfo = await getCurrentTabInfo();
  const pageInfo = detectCurrentPage(tabInfo.url, tabInfo.title);
  const mode = pageInfo.kind || "xiaoe";
  if (document.body.dataset.mode && document.body.dataset.mode !== mode) {
    resetFormSyncState();
  }
  const config = HELPER_CONFIGS[mode];
  applyModeUi(mode, pageInfo, tabInfo);
  document.body.dataset.mode = mode;
  syncBatchCourseUrl(tabInfo);
  POPUP_STATE.pageSupported = pageInfo.supported;

  let helperStatus = null;
  try {
    helperStatus = await fetchHelper(config.baseUrl, "/status");
    setStatus("helper-dot", "helper-text", "connected", config.helperRunning);
    document.getElementById("helper-hint").classList.add("hidden");
    syncInputFromHelper(document.getElementById("output-dir"), "outputDir", helperStatus.config.defaultOutputDir || "");
    if (config.showProvider) {
      syncInputFromHelper(document.getElementById("provider"), "provider", helperStatus.config.defaultProvider || "auto");
    }
  } catch {
    POPUP_STATE.helperToken = "";
    setStatus("helper-dot", "helper-text", "disconnected", config.helperStopped);
    document.getElementById("helper-hint").classList.remove("hidden");
  }
  POPUP_STATE.helperReady = Boolean(helperStatus);

  renderTask(helperStatus?.state);
  POPUP_STATE.taskRunning = isStateRunning(helperStatus?.state);
  let catalogRestored = false;
  if (mode === "xiaoe" && helperStatus) {
    try {
      const batch = await fetchHelper(HELPER_CONFIGS.xiaoe.baseUrl, "/batch-status");
      renderBatchStatus(batch.batch);
      POPUP_STATE.taskRunning = POPUP_STATE.taskRunning || isStateRunning(batch.batch);
    } catch {
      renderBatchStatus(null);
    }
    catalogRestored = await restoreBatchCatalogFromHelper();
  } else {
    renderBatchStatus(null);
  }
  updateActionButtons();
  if (!catalogRestored) {
    void maybeAutoLoadBatchCatalog();
  }
}

async function saveConfig() {
  const mode = document.body.dataset.mode || "xiaoe";
  const config = HELPER_CONFIGS[mode];
  const outputDir = document.getElementById("output-dir").value.trim();
  const body = { defaultOutputDir: outputDir };
  if (config.showProvider) {
    body.defaultProvider = document.getElementById("provider").value;
  }
  await fetchHelper(config.baseUrl, "/config", {
    method: "POST",
    body: JSON.stringify(body)
  });
  await refreshPopup();
}

async function runCurrent() {
  const mode = document.body.dataset.mode || "xiaoe";
  const config = HELPER_CONFIGS[mode];
  const tabInfo = await getCurrentTabInfo();
  await fetchHelper(config.baseUrl, "/run-current", {
    method: "POST",
    body: JSON.stringify(config.buildRunPayload(tabInfo))
  });
  await refreshPopup();
}

async function loadBatchCatalog() {
  const payload = getBatchFormPayload();
  const key = batchAutoLoadKey();
  POPUP_STATE.batchAutoKey = key;
  POPUP_STATE.batchLoading = true;
  clearBatchCatalogRestoreTimer();
  document.getElementById("batch-status").textContent = "批量任务状态：正在读取目录…";
  updateActionButtons();
  try {
    const result = await fetchHelper(HELPER_CONFIGS.xiaoe.baseUrl, "/catalog", {
      method: "POST",
      body: JSON.stringify(payload)
    });
    POPUP_STATE.batchItems = result.items || [];
    renderBatchItems();
    renderBatchStatus({ status: "idle", progress: { detail: `catalog: ${result.total || 0}` } });
  } catch (error) {
    POPUP_STATE.batchItems = [];
    renderBatchItems();
    renderBatchStatus({ status: "error", error: error instanceof Error ? error.message : String(error) });
  } finally {
    POPUP_STATE.batchLoading = false;
    updateActionButtons();
  }
}

function batchAutoLoadKey() {
  const payload = getBatchFormPayload();
  return JSON.stringify({
    courseUrl: payload.courseUrl,
    provider: payload.provider,
    type: payload.type,
    status: payload.status,
    chapter: payload.chapter || "",
    title: payload.title || "",
    limit: payload.limit,
    restart: payload.restart
  });
}

async function maybeAutoLoadBatchCatalog() {
  const autoLoadEnabled = document.getElementById("batch-auto-load")?.checked;
  if (!autoLoadEnabled) {
    return;
  }
  const isXiaoe = document.body.dataset.mode === "xiaoe";
  const hasOutputDir = document.getElementById("output-dir").value.trim().length > 0;
  const hasCatalogUrl = isXiaoeCourseCatalogUrl(document.getElementById("batch-course-url").value.trim());
  if (!isXiaoe || !POPUP_STATE.helperReady || !hasOutputDir || !hasCatalogUrl || POPUP_STATE.batchLoading) {
    return;
  }
  const key = batchAutoLoadKey();
  if (POPUP_STATE.batchAutoKey === key) {
    return;
  }
  POPUP_STATE.batchAutoKey = key;
  await loadBatchCatalog();
}

function confirmLargeBatchRun(selectedCount) {
  if (selectedCount <= 10) {
    return true;
  }
  const outputDir = document.getElementById("output-dir").value.trim();
  return window.confirm(`即将转写 ${selectedCount} 条内容，预计耗时较长，并写入：\n${outputDir}\n\n是否继续？`);
}

async function runSelectedBatch() {
  const ids = selectedBatchIds();
  if (ids.length === 0) {
    return;
  }
  if (!confirmLargeBatchRun(ids.length)) {
    return;
  }
  const selectedIdSet = new Set(ids);
  const payload = {
    ...getBatchFormPayload(),
    resourceIds: ids,
    catalogItems: POPUP_STATE.batchItems.filter((item) => selectedIdSet.has(item.resource_id || "")),
    limit: 0
  };
  const result = await fetchHelper(HELPER_CONFIGS.xiaoe.baseUrl, "/batch-run", {
    method: "POST",
    body: JSON.stringify(payload)
  });
  renderBatchStatus(result.batch);
  await refreshPopup();
}

async function stopCurrentTask() {
  const mode = document.body.dataset.mode || "xiaoe";
  const config = HELPER_CONFIGS[mode];
  const confirmed = window.confirm("确定要停止当前任务吗？这会终止正在运行的任务并关闭 Helper。之后需要重新启动 Helper 才能开始新任务。");
  if (!confirmed) {
    return;
  }
  document.getElementById("stop-current-task").disabled = true;
  renderBatchStatus({ status: "idle", progress: { detail: "正在停止当前任务并关闭 Helper…" } });
  await fetchHelper(config.baseUrl, "/shutdown", {
    method: "POST",
    body: JSON.stringify({})
  }).catch(() => undefined);
  POPUP_STATE.helperReady = false;
  POPUP_STATE.taskRunning = false;
  setStatus("helper-dot", "helper-text", "disconnected", config.helperStopped);
  document.getElementById("helper-hint").classList.remove("hidden");
  renderTask({ status: "idle", currentStage: "stopped", log: ["已请求停止当前任务，请重新启动 Helper 后再开始新任务"] });
  updateActionButtons();
}

document.getElementById("save-config").addEventListener("click", () => {
  void saveConfig();
});

document.getElementById("output-dir").addEventListener("input", (event) => {
  markFieldDirty("outputDir", event.target.value);
  updateActionButtons();
});

document.getElementById("provider").addEventListener("change", (event) => {
  markFieldDirty("provider", event.target.value);
  POPUP_STATE.batchAutoKey = "";
  updateActionButtons();
});

document.getElementById("batch-course-url").addEventListener("input", () => {
  POPUP_STATE.batchCourseUrlDirty = true;
  POPUP_STATE.batchAutoKey = "";
  updateActionButtons();
});

document.getElementById("batch-auto-load").addEventListener("change", () => {
  persistBatchAutoLoadPreference();
  POPUP_STATE.batchAutoKey = "";
  void maybeAutoLoadBatchCatalog();
});

document.getElementById("batch-restart").addEventListener("change", () => {
  POPUP_STATE.batchAutoKey = "";
  updateActionButtons();
});

for (const id of ["batch-status-filter", "batch-type-filter", "batch-chapter-filter", "batch-title-filter", "batch-limit"]) {
  document.getElementById(id).addEventListener("input", () => {
    POPUP_STATE.batchAutoKey = "";
    POPUP_STATE.batchItems = [];
    renderBatchItems();
    updateActionButtons();
  });
}

document.getElementById("run-current").addEventListener("click", () => {
  void runCurrent();
});

document.getElementById("batch-load").addEventListener("click", () => {
  void loadBatchCatalog();
});

document.getElementById("batch-run").addEventListener("click", () => {
  void runSelectedBatch();
});

document.getElementById("stop-current-task").addEventListener("click", () => {
  void stopCurrentTask();
});

document.getElementById("batch-select-all").addEventListener("click", () => {
  document.querySelectorAll(".batch-check").forEach((node) => { node.checked = true; });
  updateActionButtons();
});

document.getElementById("batch-clear-all").addEventListener("click", () => {
  document.querySelectorAll(".batch-check").forEach((node) => { node.checked = false; });
  updateActionButtons();
});

async function copyText(text) {
  await navigator.clipboard.writeText(text);
}

document.getElementById("copy-doctor-command").addEventListener("click", () => {
  void copyText('E:\\Git\\Git\\bin\\bash.exe -lc "opencli doctor"');
});

document.getElementById("copy-helper-command").addEventListener("click", () => {
  void copyText(document.getElementById("helper-command").textContent || "opencli xiaoe helper-start");
});

restoreBatchAutoLoadPreference();
void refreshPopup();
setInterval(() => { void refreshPopup(); }, 2000);
