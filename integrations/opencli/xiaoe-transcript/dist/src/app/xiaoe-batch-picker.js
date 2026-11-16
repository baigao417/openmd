export function renderXiaoeBatchPickerHtml() {
    return `<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>小鹅通批量转写</title>
  <style>
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 24px; color: #172033; background: #f7f8fb; }
    main { max-width: 1180px; margin: 0 auto; }
    h1 { margin: 0 0 16px; font-size: 26px; }
    .panel { background: #fff; border: 1px solid #e5e7eb; border-radius: 12px; padding: 16px; margin-bottom: 16px; box-shadow: 0 1px 2px rgba(15, 23, 42, 0.04); }
    label { display: block; font-size: 13px; color: #475569; margin: 8px 0 4px; }
    input, select { box-sizing: border-box; width: 100%; padding: 8px 10px; border: 1px solid #cbd5e1; border-radius: 8px; font: inherit; background: #fff; }
    button { border: 0; border-radius: 8px; padding: 9px 14px; font: inherit; cursor: pointer; background: #2563eb; color: #fff; }
    button.secondary { background: #64748b; }
    button:disabled { opacity: .55; cursor: not-allowed; }
    .grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; }
    .full { grid-column: 1 / -1; }
    .actions { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; margin-top: 12px; }
    .muted { color: #64748b; font-size: 13px; }
    .status { white-space: pre-wrap; background: #0f172a; color: #e2e8f0; border-radius: 8px; padding: 12px; min-height: 44px; overflow: auto; }
    table { width: 100%; border-collapse: collapse; font-size: 13px; }
    th, td { border-bottom: 1px solid #e5e7eb; padding: 8px; text-align: left; vertical-align: top; }
    th { position: sticky; top: 0; background: #f8fafc; z-index: 1; }
    tbody tr:hover { background: #f8fafc; }
    .table-wrap { max-height: 560px; overflow: auto; border: 1px solid #e5e7eb; border-radius: 10px; }
    .nowrap { white-space: nowrap; }
    @media (max-width: 860px) { .grid { grid-template-columns: 1fr; } .full { grid-column: auto; } }
  </style>
</head>
<body>
<main>
  <h1>小鹅通批量转写</h1>
  <section class="panel">
    <div class="grid">
      <div class="full">
        <label for="courseUrl">课程 URL</label>
        <input id="courseUrl" placeholder="https://appxxxx.h5.xiaoecloud.com/p/course/..." />
      </div>
      <div class="full">
        <label for="outDir">输出目录</label>
        <input id="outDir" placeholder="C:\\example\\output\\前哨战" />
      </div>
      <div>
        <label for="provider">Provider</label>
        <select id="provider"><option>auto</option><option>tingwu</option><option>get</option></select>
      </div>
      <div>
        <label for="status">学习状态</label>
        <input id="status" value="all" placeholder="all 或 100%,未学" />
      </div>
      <div>
        <label for="type">类型</label>
        <input id="type" value="直播" />
      </div>
      <div>
        <label for="limit">Limit（0 不限）</label>
        <input id="limit" type="number" min="0" value="0" />
      </div>
      <div>
        <label for="chapter">章节关键词</label>
        <input id="chapter" />
      </div>
      <div>
        <label for="title">标题关键词</label>
        <input id="title" />
      </div>
      <div>
        <label for="timeoutMinutes">单条超时（分钟）</label>
        <input id="timeoutMinutes" type="number" min="1" value="120" />
      </div>
      <div>
        <label for="skipExisting">Skip existing</label>
        <select id="skipExisting"><option value="true">true</option><option value="false">false</option></select>
      </div>
      <div>
        <label for="resume">Resume</label>
        <select id="resume"><option value="true">true</option><option value="false">false</option></select>
      </div>
    </div>
    <div class="actions">
      <button id="loadCatalog">读取目录</button>
      <button class="secondary" id="selectAll" disabled>全选</button>
      <button class="secondary" id="selectNone" disabled>全不选</button>
      <button id="startBatch" disabled>开始批量转写</button>
      <span class="muted" id="summary">未读取目录</span>
    </div>
  </section>

  <section class="panel">
    <h2>状态</h2>
    <div class="status" id="statusBox">idle</div>
  </section>

  <section class="panel">
    <h2>目录</h2>
    <div class="table-wrap">
      <table>
        <thead><tr><th class="nowrap">选择</th><th>章节</th><th>标题</th><th>类型</th><th>状态</th><th>resource_id</th></tr></thead>
        <tbody id="catalogBody"></tbody>
      </table>
    </div>
  </section>
</main>
<script>
const $ = (id) => document.getElementById(id);
let catalogItems = [];
let pollTimer = null;

function readForm() {
  return {
    courseUrl: $("courseUrl").value.trim(),
    outDir: $("outDir").value.trim(),
    provider: $("provider").value,
    status: $("status").value.trim() || "all",
    type: $("type").value.trim() || "直播",
    chapter: $("chapter").value.trim(),
    title: $("title").value.trim(),
    limit: Number.parseInt($("limit").value || "0", 10),
    skipExisting: $("skipExisting").value === "true",
    resume: $("resume").value === "true",
    timeoutMinutes: Number.parseInt($("timeoutMinutes").value || "120", 10)
  };
}

async function postJson(path, body) {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body)
  });
  const text = await response.text();
  const data = text ? JSON.parse(text) : {};
  if (!response.ok) throw new Error(data.error || response.statusText);
  return data;
}

function renderCatalog(items) {
  const body = $("catalogBody");
  body.textContent = "";
  for (const item of items) {
    const tr = document.createElement("tr");
    const checked = document.createElement("input");
    checked.type = "checkbox";
    checked.checked = true;
    checked.dataset.resourceId = item.resource_id;
    const checkboxTd = document.createElement("td");
    checkboxTd.appendChild(checked);
    tr.appendChild(checkboxTd);
    for (const key of ["chapter", "title", "type", "status", "resource_id"]) {
      const td = document.createElement("td");
      td.textContent = item[key] || "";
      tr.appendChild(td);
    }
    body.appendChild(tr);
  }
  $("summary").textContent = "已读取 " + items.length + " 条";
  $("selectAll").disabled = items.length === 0;
  $("selectNone").disabled = items.length === 0;
  $("startBatch").disabled = items.length === 0;
}

function setStatus(value) {
  $("statusBox").textContent = typeof value === "string" ? value : JSON.stringify(value, null, 2);
}

async function loadStatusDefaults() {
  try {
    const status = await fetch("/status").then((r) => r.json());
    if (status.config) {
      $("outDir").value = status.config.defaultOutputDir || "";
      $("provider").value = status.config.defaultProvider || "auto";
    }
  } catch {}
}

$("loadCatalog").addEventListener("click", async () => {
  try {
    setStatus("读取目录中...");
    const data = await postJson("/catalog", readForm());
    catalogItems = data.items || [];
    renderCatalog(catalogItems);
    setStatus({ ok: true, total: data.total, normalizedUrl: data.normalizedUrl });
  } catch (error) {
    setStatus("读取目录失败: " + (error instanceof Error ? error.message : String(error)));
  }
});

$("selectAll").addEventListener("click", () => {
  document.querySelectorAll("tbody input[type=checkbox]").forEach((box) => { box.checked = true; });
});
$("selectNone").addEventListener("click", () => {
  document.querySelectorAll("tbody input[type=checkbox]").forEach((box) => { box.checked = false; });
});

$("startBatch").addEventListener("click", async () => {
  try {
    const selected = Array.from(document.querySelectorAll("tbody input[type=checkbox]:checked")).map((box) => box.dataset.resourceId);
    if (selected.length === 0) throw new Error("请至少勾选一条内容");
    const selectedSet = new Set(selected);
    const payload = {
      ...readForm(),
      resourceIds: selected,
      catalogItems: catalogItems.filter((item) => selectedSet.has(item.resource_id || "")),
      dryRun: false
    };
    setStatus("提交批量任务...");
    const data = await postJson("/batch-run", payload);
    setStatus(data);
    if (pollTimer) window.clearInterval(pollTimer);
    pollTimer = window.setInterval(pollBatchStatus, 2000);
    await pollBatchStatus();
  } catch (error) {
    setStatus("提交失败: " + (error instanceof Error ? error.message : String(error)));
  }
});

async function pollBatchStatus() {
  try {
    const data = await fetch("/batch-status").then((r) => r.json());
    setStatus(data.batch || data);
    if (data.batch && data.batch.status !== "running" && pollTimer) {
      window.clearInterval(pollTimer);
      pollTimer = null;
    }
  } catch (error) {
    setStatus("状态读取失败: " + (error instanceof Error ? error.message : String(error)));
  }
}

loadStatusDefaults();
</script>
</body>
</html>`;
}
