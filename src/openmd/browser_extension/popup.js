const $ = (id) => document.getElementById(id);
const isWeb = (url) => /^https?:\/\//.test(url || "");

function message(text, ok) {
  const msg = $("msg");
  msg.textContent = text;
  msg.className = `msg ${ok ? "ok" : ""}`;
  msg.hidden = !text;
}

async function init() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  const highlighted = await chrome.tabs.query({ highlighted: true, currentWindow: true });
  const tabs = (highlighted.length > 1 ? highlighted : [tab]).filter((t) => isWeb(t?.url));
  const { settings } = await chrome.storage.local.get("settings");
  const locations = settings?.locations || [];
  if (!locations.length) {
    chrome.runtime.openOptionsPage();
    window.close();
    return;
  }
  $("label").textContent = tabs.length > 1 ? `把 ${tabs.length} 个标签页保存到` : "保存到";
  if (!tabs.length) message("这个页面不是普通网页，没法保存。");
  const list = $("locations");
  for (const loc of locations) {
    const li = document.createElement("li");
    const button = document.createElement("button");
    button.type = "button";
    button.className = "location";
    const name = document.createElement("span");
    name.className = "loc-name";
    name.textContent = loc.name + (loc.id === settings.defaultId ? "  · 默认" : "");
    const path = document.createElement("span");
    path.className = "loc-path";
    path.textContent = loc.path;
    button.append(name, path);
    button.disabled = !tabs.length;
    button.onclick = async () => {
      document.querySelectorAll(".location").forEach((b) => { b.disabled = true; });
      const reply = await chrome.runtime.sendMessage({ cmd: "save", items: tabs.map((t) => ({ url: t.url, title: t.title })), locationId: loc.id });
      if (reply?.ok) {
        message(reply.count > 1 ? `正在把 ${reply.count} 个页面依次保存到「${loc.name}」` : `正在保存到「${loc.name}」`, true);
        setTimeout(() => window.close(), 900);
      } else {
        message("没能开始保存，详情见通知。");
      }
    };
    li.append(button);
    list.append(li);
  }
  const snapshot = $("snapshot");
  snapshot.disabled = !tab?.id || !tabs.length;
  snapshot.onclick = async () => {
    snapshot.disabled = true;
    const reply = await chrome.runtime.sendMessage({ cmd: "snapshot", locationId: settings.defaultId });
    if (reply?.ok) {
      message(`正在把当前页快照保存到「${reply.location}」`, true);
      setTimeout(() => window.close(), 900);
    } else {
      snapshot.disabled = false;
      message("没能保存当前页，详情见通知。");
    }
  };
}

$("settings").addEventListener("click", (event) => {
  event.preventDefault();
  chrome.runtime.openOptionsPage();
  window.close();
});

init();
