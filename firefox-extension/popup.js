const BRIDGE_URL = "http://127.0.0.1:16890";
let currentTabId = null;

async function checkBridge() {
  const badge = document.getElementById("bridgeStatus");
  try {
    const r = await fetch(`${BRIDGE_URL}/api/ping`);
    if (r.ok) {
      badge.textContent = "● Conectado";
      badge.style.background = "rgba(16, 185, 129, 0.2)";
      badge.style.color = "#6ee7b7";
      return true;
    }
  } catch (_e) {}
  badge.textContent = "○ App cerrada";
  badge.style.background = "rgba(239, 68, 68, 0.2)";
  badge.style.color = "#fca5a5";
  return false;
}

function renderMediaItems(items, activeTab) {
  const listEl = document.getElementById("mediaList");

  const effectiveItems = [...(items || [])];
  if (
    effectiveItems.length === 0 &&
    activeTab &&
    activeTab.url &&
    (activeTab.url.includes("youtube.com/watch") ||
      activeTab.url.includes("youtu.be/") ||
      activeTab.url.includes("vimeo.com/") ||
      activeTab.url.includes("twitch.tv/"))
  ) {
    effectiveItems.push({
      url: activeTab.url,
      title: activeTab.title || "Video en pestaña actual",
      type: "Streaming",
      isStreamPage: true,
    });
  }

  if (!effectiveItems.length) return;
  listEl.innerHTML = "";

  for (const item of effectiveItems) {
    const card = document.createElement("div");
    card.className = "media-card";

    const title = document.createElement("div");
    title.className = "media-title";
    const sizeInfo =
      item.size && item.size > 0
        ? ` • ${(item.size / (1024 * 1024)).toFixed(1)} MB`
        : "";
    title.textContent = `${item.title}${sizeInfo}`;

    const controls = document.createElement("div");
    controls.className = "media-controls";

    const select = document.createElement("select");
    const defaultOptions = [
      { id: "best", label: "Máxima calidad (Auto)" },
      { id: "2160p", label: "2160p 4K UHD (MP4)" },
      { id: "1440p", label: "1440p 2K QHD (MP4)" },
      { id: "1080p", label: "1080p Full HD (MP4)" },
      { id: "720p", label: "720p HD (MP4)" },
      { id: "480p", label: "480p SD (MP4)" },
      { id: "360p", label: "360p (MP4)" },
      { id: "audio", label: "Solo Audio (MP3)" },
    ];
    for (const opt of defaultOptions) {
      const o = document.createElement("option");
      o.value = opt.id;
      o.textContent = opt.label;
      if (item.resolution && opt.id === item.resolution) {
        o.selected = true;
      } else if (!item.resolution && opt.id === "1080p") {
        o.selected = true;
      }
      select.appendChild(o);
    }

    if (
      item.isStreamPage ||
      item.url.includes("youtube.com") ||
      item.url.includes("youtu.be") ||
      item.url.includes(".m3u8")
    ) {
      browser.runtime
        .sendMessage({ action: "fetchQualities", url: item.url })
        .then((qResp) => {
          if (qResp && qResp.ok && qResp.data && Array.isArray(qResp.data.choices)) {
            select.innerHTML = "";
            for (const c of qResp.data.choices) {
              const o = document.createElement("option");
              o.value = c.id;
              o.textContent = c.label;
              if (c.id === "1080p") o.selected = true;
              select.appendChild(o);
            }
          }
        })
        .catch(() => {});
    }

    const btn = document.createElement("button");
    btn.className = "dl-btn";
    btn.textContent = "⬇ Descargar";
    btn.addEventListener("click", async () => {
      btn.disabled = true;
      btn.textContent = "Enviando...";
      const r = await browser.runtime.sendMessage({
        action: "enqueueDownload",
        tabId: currentTabId,
        url: item.url,
        quality: select.value,
        container: "Videos Web",
        filename: item.filename || (item.isStreamPage ? "" : item.title),
        source_page: (activeTab && activeTab.url) || "",
        headers: item.headers || {},
      });
      if (r && r.ok) {
        btn.textContent = "✓ En cola";
      } else {
        btn.textContent = "Error (Abre App)";
        btn.disabled = false;
      }
    });

    controls.appendChild(select);
    controls.appendChild(btn);
    card.appendChild(title);
    card.appendChild(controls);
    listEl.appendChild(card);
  }
}

async function initPopup() {
  await checkBridge();

  const tabs = await browser.tabs.query({ active: true, currentWindow: true });
  if (!tabs.length) return;
  const activeTab = tabs[0];
  currentTabId = activeTab.id;

  const resp = await browser.runtime.sendMessage({
    action: "getTabMedia",
    tabId: activeTab.id,
  });

  const chk = document.getElementById("chkIntercept");
  chk.checked =
    resp && typeof resp.interceptDownloads === "boolean"
      ? resp.interceptDownloads
      : true;
  chk.addEventListener("change", () => {
    browser.runtime.sendMessage({ action: "setIntercept", value: chk.checked });
  });

  renderMediaItems((resp && resp.items) || [], activeTab);
}

document.addEventListener("DOMContentLoaded", initPopup);
