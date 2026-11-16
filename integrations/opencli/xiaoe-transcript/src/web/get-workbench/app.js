const state = {
  environment: null,
  subscriptions: [],
  selectedSubjectUrl: "",
  selectedCreatorId: "",
  selectedCreatorName: "",
  startDate: "",
  endDate: "",
  skipExisting: true,
  drawerOpen: false,
  loadingSubscriptions: false,
  loadingStatus: false,
  startingHelper: false,
  exporting: false,
  filterInteracting: false,
  pendingRender: false,
  message: "",
  error: ""
};

let pollTimer = 0;

async function fetchJson(url, init) {
  const response = await fetch(url, init);
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(body.error || `Request failed: ${response.status}`);
  }
  return body;
}

function deriveOutputDir() {
  return state.environment?.state?.lastRun?.creatorDir
    || state.environment?.config?.defaultOutputDir
    || "";
}

function deriveLogLines() {
  return state.environment?.state?.log ?? [];
}

function derivePreviewLines() {
  const lines = deriveLogLines();
  return lines.slice(-3);
}

function renderSubscriptionOptions() {
  if (state.loadingSubscriptions) {
    return `<option value="">正在加载订阅博主...</option>`;
  }
  if (state.subscriptions.length === 0) {
    return `<option value="">暂无可用订阅博主</option>`;
  }
  return [
    `<option value="">请选择订阅博主</option>`,
    ...state.subscriptions.map((item) => `
      <option value="${item.subjectUrl}" ${item.subjectUrl === state.selectedSubjectUrl ? "selected" : ""}>
        ${item.creatorName}
      </option>
    `)
  ].join("");
}

function renderPreviewLogs() {
  const lines = derivePreviewLines();
  if (lines.length === 0) {
    return `<div class="visually-muted">导出开始后，这里会显示最近几条日志。</div>`;
  }
  return lines.map((line) => `<div class="log-preview-bubble"><div class="log-preview-line">${line}</div></div>`).join("");
}

function renderDrawerLogs() {
  const lines = deriveLogLines();
  if (lines.length === 0) {
    return `<div class="visually-muted">当前没有完整日志可显示。</div>`;
  }
  return lines.map((line) => `<div class="log-line">${line}</div>`).join("");
}

function isFilterControl(target) {
  return target instanceof HTMLElement && target.matches("[data-subscription-select], [data-start-date], [data-end-date], [data-skip-existing]");
}

function renderApp(force = false) {
  if (state.filterInteracting && !force) {
    state.pendingRender = true;
    return;
  }
  state.pendingRender = false;
  const helperStatus = state.environment?.state?.status ?? "stopped";
  const canExport = helperStatus !== "stopped" && state.selectedSubjectUrl && !state.exporting;
  const successCount = state.environment?.state?.successCount ?? 0;
  const skippedCount = state.environment?.state?.skippedCount ?? 0;
  const failedCount = state.environment?.state?.failedCount ?? 0;
  const outputDir = deriveOutputDir();

  const app = document.querySelector("#app");
  app.innerHTML = `
    <div class="workbench">
      <header class="workbench-header">
        <div>
          <div class="eyebrow">LOCAL GET EXPORT WORKBENCH</div>
          <h1 class="title">Get 导出工作台</h1>
          <p class="subtitle">在一个页面里完成环境确认、订阅博主选择、日期配置和实时导出跟踪。</p>
        </div>
        <div class="badge-row">
          <div class="badge">本地网站</div>
          <div class="badge">三栏工作台</div>
          <div class="badge">覆盖式日志抽屉</div>
        </div>
      </header>

      ${state.error ? `<div class="error-state">${state.error}</div>` : ""}
      ${state.message ? `<div class="empty-state">${state.message}</div>` : ""}

      <main class="columns">
        <section class="panel">
          <div class="panel-header">
            <div class="eyebrow">STEP 1 / LOCAL AUTH</div>
            <h2 class="panel-title">授权与环境</h2>
          </div>
          <div class="panel-body">
            <div class="hint">确认浏览器已登录 Get、helper 在线、默认目录正确。helper 未启动时可直接从这里启动。</div>
            <div class="status-card">
              <div class="soft-label">Current Status</div>
              <div class="soft-value">Helper: ${helperStatus}</div>
              <div class="hint">${state.environment?.config?.defaultOutputDir || "尚未读取默认导出目录"}</div>
            </div>
            <div class="button-row">
              <button class="button button-primary" data-refresh-status>${state.loadingStatus ? "刷新中..." : "刷新状态"}</button>
              <button class="button button-secondary" data-start-helper ${state.startingHelper ? "disabled" : ""}>${state.startingHelper ? "启动中..." : "启动 helper"}</button>
            </div>
          </div>
        </section>

        <section class="panel">
          <div class="panel-header">
            <div class="eyebrow">STEP 2 / WORKSPACE</div>
            <h2 class="panel-title">选择博主与范围</h2>
          </div>
          <div class="panel-body">
            <div class="soft-grid">
              <div class="soft-card">
                <div class="soft-label">订阅博主</div>
                <select class="select" data-subscription-select>
                  ${renderSubscriptionOptions()}
                </select>
              </div>
              <div class="soft-card">
                <div class="soft-label">主题 ID</div>
                <div class="soft-value">${state.selectedCreatorId || "未选择"}</div>
              </div>
            </div>

            <div class="field-grid">
              <label class="field">
                <span class="field-label">开始日期</span>
                <input class="input" data-start-date type="date" value="${state.startDate}" />
              </label>
              <label class="field">
                <span class="field-label">结束日期</span>
                <input class="input" data-end-date type="date" value="${state.endDate}" />
              </label>
            </div>
            <div class="hint">开始日期和结束日期都可以留空。留空则导出全部。</div>

            <div class="soft-card toggle-row">
              <div>
                <div class="soft-label">Export Mode</div>
                <div class="hint">优先导出紫色来源块进入后的 /web 正文页。</div>
              </div>
              <label>
                <input data-skip-existing type="checkbox" ${state.skipExisting ? "checked" : ""} />
                跳过已存在
              </label>
            </div>

            <button class="button button-primary" data-export ${canExport ? "" : "disabled"}>
              ${state.exporting ? "导出提交中..." : "开始导出"}
            </button>
          </div>
        </section>

        <section>
          <div class="panel">
            <div class="panel-header">
              <div class="eyebrow">STEP 3 / EXPORT</div>
              <h2 class="panel-title">导出进度</h2>
            </div>
            <div class="panel-body">
              <div class="metrics-grid">
                <div class="metric">
                  <div class="soft-label">Success</div>
                  <div class="metric-value">${successCount}</div>
                </div>
                <div class="metric">
                  <div class="soft-label">Skipped</div>
                  <div class="metric-value">${skippedCount}</div>
                </div>
                <div class="metric">
                  <div class="soft-label">Failed</div>
                  <div class="metric-value">${failedCount}</div>
                </div>
                <div class="metric">
                  <div class="soft-label">Stage</div>
                  <div class="metric-value" style="font-size:20px;">${state.environment?.state?.currentStage || "idle"}</div>
                </div>
              </div>
              <div class="soft-card">
                <div class="soft-label">输出目录</div>
                <div class="hint">${outputDir || "导出开始后会显示当前输出目录"}</div>
              </div>
            </div>
          </div>

          <div class="log-card" style="margin-top:18px;">
            <div class="log-card-header">
              <div>
                <div class="eyebrow" style="color:#8e857a;">LIVE LOG</div>
                <div class="log-card-title">实时导出日志</div>
              </div>
              <button class="button button-secondary" data-log-drawer-trigger>展开</button>
            </div>
            <div class="log-preview">
              ${renderPreviewLogs()}
            </div>
          </div>
        </section>
      </main>
    </div>

    <div class="drawer-overlay ${state.drawerOpen ? "is-open" : ""}" data-log-drawer>
      <div class="log-drawer-panel">
        <div class="log-drawer-top">
          <div class="log-drawer-header">
            <div>
              <div class="eyebrow" style="color:#938a7f;">EXPANDED LOG</div>
              <div class="log-drawer-title">完整导出日志</div>
              <div class="log-drawer-copy">查看完整进度、复制日志，必要时回到工作台继续配置下一次导出。</div>
            </div>
            <div class="button-row">
              <button class="button button-ghost" data-copy-log>复制</button>
              <button class="button button-secondary" data-log-drawer-close>收起</button>
            </div>
          </div>
        </div>
        <div class="pill-row">
          <div class="pill">成功 ${successCount}</div>
          <div class="pill">跳过 ${skippedCount}</div>
          <div class="pill">失败 ${failedCount}</div>
          <div class="pill">${outputDir || "尚未生成输出目录"}</div>
        </div>
        <div class="log-drawer-body">
          <div class="log-drawer-scroll">${renderDrawerLogs()}</div>
        </div>
      </div>
    </div>
  `;
}

async function refreshStatus() {
  state.loadingStatus = true;
  renderApp();
  try {
    state.environment = await fetchJson("/api/status");
    state.error = "";
  } catch (error) {
    state.error = error instanceof Error ? error.message : String(error);
  } finally {
    state.loadingStatus = false;
    renderApp();
  }
}

async function loadSubscriptions() {
  state.loadingSubscriptions = true;
  renderApp();
  try {
    const data = await fetchJson("/api/subscriptions");
    state.subscriptions = data.items ?? [];
    if (!state.selectedSubjectUrl && state.subscriptions.length > 0) {
      const first = state.subscriptions[0];
      state.selectedSubjectUrl = first.subjectUrl;
      state.selectedCreatorId = first.creatorId;
      state.selectedCreatorName = first.creatorName;
    }
    state.error = "";
  } catch (error) {
    state.error = error instanceof Error ? error.message : String(error);
  } finally {
    state.loadingSubscriptions = false;
    renderApp();
  }
}

async function startHelper() {
  state.startingHelper = true;
  renderApp();
  try {
    await fetchJson("/api/helper/start", { method: "POST" });
    state.message = "Get helper 已启动";
    await refreshStatus();
  } catch (error) {
    state.error = error instanceof Error ? error.message : String(error);
  } finally {
    state.startingHelper = false;
    renderApp();
  }
}

async function submitExport() {
  state.exporting = true;
  renderApp();
  try {
    await fetchJson("/api/export", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        subjectUrl: state.selectedSubjectUrl,
        creatorId: state.selectedCreatorId,
        creatorName: state.selectedCreatorName,
        startDate: state.startDate || undefined,
        endDate: state.endDate || undefined,
        skipExisting: state.skipExisting
      })
    });
    state.message = "导出任务已提交";
    await refreshStatus();
    schedulePolling();
  } catch (error) {
    state.error = error instanceof Error ? error.message : String(error);
  } finally {
    state.exporting = false;
    renderApp();
  }
}

async function copyLog() {
  const content = deriveLogLines().join("\n");
  try {
    await navigator.clipboard.writeText(content);
    state.message = "日志已复制";
  } catch {
    state.error = "当前环境不支持复制日志";
  }
  renderApp();
}

function schedulePolling() {
  clearTimeout(pollTimer);
  const nextDelay = state.environment?.state?.status === "running" ? 2000 : 10000;
  pollTimer = window.setTimeout(async () => {
    await refreshStatus();
    schedulePolling();
  }, nextDelay);
}

document.addEventListener("click", async (event) => {
  const target = event.target;
  if (!(target instanceof HTMLElement)) {
    return;
  }
  if (target.matches("[data-refresh-status]")) {
    await refreshStatus();
    return;
  }
  if (target.matches("[data-start-helper]")) {
    await startHelper();
    return;
  }
  if (target.matches("[data-export]")) {
    await submitExport();
    return;
  }
  if (target.matches("[data-log-drawer-trigger]")) {
    state.drawerOpen = true;
    renderApp();
    return;
  }
  if (target.matches("[data-log-drawer-close]")) {
    state.drawerOpen = false;
    renderApp();
    return;
  }
  if (target.matches("[data-copy-log]")) {
    await copyLog();
  }
});

document.addEventListener("change", (event) => {
  const target = event.target;
  if (!(target instanceof HTMLInputElement || target instanceof HTMLSelectElement)) {
    return;
  }

  if (target.matches("[data-subscription-select]")) {
    state.selectedSubjectUrl = target.value;
    const selected = state.subscriptions.find((item) => item.subjectUrl === target.value);
    state.selectedCreatorId = selected?.creatorId ?? "";
    state.selectedCreatorName = selected?.creatorName ?? "";
    renderApp();
    return;
  }

  if (target.matches("[data-start-date]")) {
    state.startDate = target.value;
    renderApp(true);
    return;
  }

  if (target.matches("[data-end-date]")) {
    state.endDate = target.value;
    renderApp(true);
    return;
  }

  if (target.matches("[data-skip-existing]")) {
    state.skipExisting = target.checked;
    renderApp(true);
  }
});

document.addEventListener("input", (event) => {
  const target = event.target;
  if (!(target instanceof HTMLInputElement)) {
    return;
  }

  if (target.matches("[data-start-date]")) {
    state.startDate = target.value;
    return;
  }

  if (target.matches("[data-end-date]")) {
    state.endDate = target.value;
  }
});

document.addEventListener("focusin", (event) => {
  if (isFilterControl(event.target)) {
    state.filterInteracting = true;
  }
});

document.addEventListener("focusout", (event) => {
  if (!isFilterControl(event.target)) {
    return;
  }
  window.setTimeout(() => {
    const active = document.activeElement;
    if (!isFilterControl(active)) {
      state.filterInteracting = false;
      if (state.pendingRender) {
        renderApp(true);
      }
    }
  }, 0);
});

renderApp();
await refreshStatus();
await loadSubscriptions();
schedulePolling();
