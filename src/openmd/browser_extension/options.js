const $ = (id) => document.getElementById(id);
let settings = { locations: [], defaultId: null, clickMode: "auto", videoMode: "note", includeThinking: false, allowFeedbackPackage: false };

function native(message) {
  return chrome.runtime.sendMessage({ cmd: "native", message });
}

function isAbsoluteWindowsPath(p) {
  return /^[A-Za-z]:[\\/]/.test(p) || /^\\\\/.test(p);
}

let savedTimer;
async function persist() {
  settings.locations = settings.locations.filter((l) => l.name.trim() || l.path.trim());
  if (!settings.locations.some((l) => l.id === settings.defaultId)) settings.defaultId = settings.locations[0]?.id || null;
  await chrome.storage.local.set({ settings });
  $("saved").textContent = "已保存设置";
  clearTimeout(savedTimer);
  savedTimer = setTimeout(() => { $("saved").textContent = ""; }, 1500);
}

function row(loc) {
  const li = document.createElement("li");
  li.className = "row";

  const radio = document.createElement("input");
  radio.type = "radio";
  radio.name = "default";
  radio.id = `def-${loc.id}`;
  radio.title = "设为默认";
  radio.checked = loc.id === settings.defaultId;
  radio.onchange = () => { settings.defaultId = loc.id; persist(); };

  const name = document.createElement("input");
  name.type = "text";
  name.id = `name-${loc.id}`;
  name.className = "name";
  name.placeholder = "名字";
  name.value = loc.name;
  name.onchange = () => { loc.name = name.value.trim() || "未命名"; name.value = loc.name; persist(); };

  const path = document.createElement("input");
  path.type = "text";
  path.id = `path-${loc.id}`;
  path.className = "path";
  path.placeholder = "例如 D:\\资料\\收件箱";
  path.spellcheck = false;
  path.value = loc.path;
  const check = () => path.classList.toggle("invalid", !!path.value && !isAbsoluteWindowsPath(path.value));
  path.onchange = () => { loc.path = path.value.trim(); check(); persist(); };
  check();

  const pick = document.createElement("button");
  pick.type = "button";
  pick.className = "secondary";
  pick.textContent = "选择…";
  pick.onclick = async () => {
    pick.disabled = true;
    const reply = await native({ type: "pick_folder", initial: loc.path });
    pick.disabled = false;
    if (reply?.ok && reply.path) { loc.path = reply.path; path.value = reply.path; check(); persist(); }
  };

  const remove = document.createElement("button");
  remove.type = "button";
  remove.className = "remove";
  remove.textContent = "删除";
  remove.onclick = () => {
    settings.locations = settings.locations.filter((l) => l.id !== loc.id);
    persist();
    render();
  };

  li.append(radio, name, path, pick, remove);
  return li;
}

function render() {
  const rows = $("rows");
  rows.textContent = "";
  for (const loc of settings.locations) rows.append(row(loc));
  document.querySelectorAll('input[name="mode"]').forEach((r) => { r.checked = r.value === settings.clickMode; });
  document.querySelectorAll('input[name="video"]').forEach((r) => { r.checked = r.value === (settings.videoMode || "note"); });
  $("include-thinking").checked = settings.includeThinking === true;
  $("allow-feedback").checked = settings.allowFeedbackPackage === true;
}

async function init() {
  const stored = (await chrome.storage.local.get("settings")).settings;
  if (stored) settings = { videoMode: "note", includeThinking: false, allowFeedbackPackage: false, ...stored };
  const ping = await native({ type: "ping" });
  const host = $("host");
  host.textContent = ping?.ok ? "本机 openmd 已连接" : (ping?.error || "未连接本机 openmd");
  host.className = `host ${ping?.ok ? "ok" : "bad"}`;
  if (!settings.locations.length && ping?.ok && ping.save_to) {
    const id = crypto.randomUUID();
    settings = { locations: [{ id, name: "默认", path: ping.save_to }], defaultId: id, clickMode: "auto", videoMode: "note" };
    await persist();
  }
  render();
}

$("add").addEventListener("click", () => {
  const loc = { id: crypto.randomUUID(), name: `位置 ${settings.locations.length + 1}`, path: "" };
  settings.locations.push(loc);
  if (!settings.defaultId) settings.defaultId = loc.id;
  render();
  document.getElementById(`path-${loc.id}`).focus();
});

document.querySelectorAll('input[name="mode"]').forEach((r) => {
  r.addEventListener("change", () => { settings.clickMode = r.value; persist(); });
});

$("include-thinking").addEventListener("change", () => {
  settings.includeThinking = $("include-thinking").checked;
  persist();
});

$("allow-feedback").addEventListener("change", () => {
  settings.allowFeedbackPackage = $("allow-feedback").checked;
  persist();
});

init();

document.querySelectorAll('input[name="video"]').forEach((r) => {
  r.addEventListener("change", () => { settings.videoMode = r.value; persist(); });
});
