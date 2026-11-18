// Mint Downloader - Firefox IDM Background Script (Real-Time Stream & Resolution Sniffer)
const BRIDGE_URL = "http://127.0.0.1:16890";

// Map of tabId -> { streams: Array, latestStream: Object, requestHeadersMap: Map<url, headers> }
const tabStateMap = new Map();
let interceptDownloads = true;

browser.storage.local.get(["interceptDownloads"]).then((res) => {
  if (typeof res.interceptDownloads === "boolean") {
    interceptDownloads = res.interceptDownloads;
  }
});

function getTabState(tabId) {
  if (!tabStateMap.has(tabId)) {
    tabStateMap.set(tabId, {
      streams: [],
      latestStream: null,
      headersByUrl: new Map(),
      pageTitle: "",
      pageUrl: "",
    });
  }
  return tabStateMap.get(tabId);
}

function inferResolutionLabel(url) {
  const m = url.match(/(?:^|[^0-9])(2160|1440|1080|720|480|360|240)p?(?:[^0-9]|$)/i);
  if (m) return `${m[1]}p`;
  if (url.toLowerCase().includes("master") && url.toLowerCase().includes(".m3u8")) {
    return "HLS Master (Multi-calidad)";
  }
  if (url.toLowerCase().includes(".m3u8")) return "HLS Stream";
  if (url.toLowerCase().includes(".mpd")) return "DASH Stream";
  return "";
}

function isSegmentOrPreroll(url, size) {
  const u = url.toLowerCase();
  const pathPart = u.split("?")[0];
  const lastSeg = pathPart.split("/").pop() || "";

  // 1. Check if the last URL path segment is base64-encoded (e.g. c2VnLTEtdjEtYTEud2VicA== -> seg-1-v1-a1.webp)
  const b64Candidate = lastSeg.replace(/-/g, "+").replace(/_/g, "/");
  if (/^[a-z0-9+/]{12,}={0,2}$/i.test(b64Candidate)) {
    try {
      const dec = atob(b64Candidate).toLowerCase();
      if (
        dec.includes("seg-") ||
        dec.includes("chunk-") ||
        dec.includes("frag-") ||
        dec.endsWith(".ts") ||
        dec.endsWith(".m4s") ||
        dec.endsWith(".webp") ||
        dec.endsWith(".jpg") ||
        dec.endsWith(".png")
      ) {
        return true;
      }
    } catch (_e) {}
  }

  // 2. Reject numbered HLS/DASH segment files (seg-1..., chunk-2..., fragment-3...)
  if (
    /(?:^|[\/_-])(seg|segment|chunk|frag|fragment|part)-?\d+/i.test(pathPart) ||
    pathPart.endsWith(".ts") ||
    pathPart.endsWith(".m4s") ||
    pathPart.endsWith(".webp") ||
    pathPart.endsWith(".vtt") ||
    pathPart.endsWith(".srt")
  ) {
    return true;
  }

  // 3. Reject ad/preroll clips and workers.dev obfuscated HLS chunks
  if (
    u.includes("un1v3rs4l") ||
    u.includes("workers.dev/file") ||
    u.includes("/preroll") ||
    u.includes("/vast/") ||
    u.includes("doubleclick.net") ||
    u.includes("googlesyndication")
  ) {
    return true;
  }

  const isManifest = u.includes(".m3u8") || u.includes(".mpd") || u.includes("master.txt");
  // For non-manifest direct video files, ignore small chunks (< 15 MB) which are MSE segments or ads
  if (!isManifest && size > 0 && size < 15 * 1024 * 1024) {
    return true;
  }
  return false;
}

function registerMediaForTab(tabId, item) {
  if (tabId < 0 || !item || !item.url) return;
  if (item.url.startsWith("blob:") || item.url.includes("127.0.0.1:16890")) return;

  const state = getTabState(tabId);
  const savedHeaders = state.headersByUrl.get(item.url) || item.headers || {};
  const resTag = item.resolution || inferResolutionLabel(item.url);

  const entry = {
    url: item.url,
    title: item.title || state.pageTitle || "Video detectado",
    filename: item.filename || "",
    type: item.type || "video",
    resolution: resTag,
    size: item.size || 0,
    isStreamPage: Boolean(item.isStreamPage),
    isRealStream: Boolean(item.isRealStream),
    headers: savedHeaders,
    timestamp: Date.now(),
  };

  // Deduplicate by resolution (or by base path without volatile token query string)
  // so the user only sees 1 clean entry per resolution instead of dozens of tokens!
  const baseKey = entry.url.split("?")[0];
  state.streams = state.streams.filter((x) => {
    if (x.url === entry.url) return false;
    if (x.url.split("?")[0] === baseKey) return false;
    if (resTag && x.resolution === resTag && x.isRealStream === entry.isRealStream) return false;
    return true;
  });

  state.streams.unshift(entry);
  if (state.streams.length > 5) state.streams.pop();

  if (entry.isRealStream || entry.isStreamPage) {
    state.latestStream = entry;
  }

  updateBadge(tabId);

  browser.tabs
    .sendMessage(tabId, {
      action: "streamUpdated",
      latestStream: state.latestStream || state.streams[0],
      streams: state.streams.slice(0, 4),
    })
    .catch(() => {});
}

function updateBadge(tabId) {
  const state = tabStateMap.get(tabId);
  const count = state ? state.streams.length : 0;
  browser.browserAction.setBadgeText({ text: count > 0 ? String(count) : "", tabId });
  browser.browserAction.setBadgeBackgroundColor({ color: "#10b981", tabId });
}

browser.tabs.onRemoved.addListener((tabId) => {
  tabStateMap.delete(tabId);
});

browser.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  if (changeInfo.status === "loading" && changeInfo.url) {
    tabStateMap.delete(tabId);
    updateBadge(tabId);
  }
  const st = getTabState(tabId);
  if (tab && tab.title) {
    st.pageTitle = tab.title;
  }
  if (tab && tab.url && tab.url.startsWith("http")) {
    st.pageUrl = tab.url;
  }
  if (tab && tab.url && isStreamingWatchPage(tab.url)) {
    registerMediaForTab(tabId, {
      url: tab.url,
      title: tab.title || "Video de Streaming",
      type: "Streaming",
      isStreamPage: true,
    });
  }
});

function isStreamingWatchPage(url) {
  if (!url) return false;
  const u = url.toLowerCase();
  return (
    u.includes("youtube.com/watch") ||
    u.includes("youtube.com/shorts/") ||
    u.includes("youtu.be/") ||
    u.includes("vimeo.com/") ||
    u.includes("twitch.tv/videos/") ||
    u.includes("dailymotion.com/video/") ||
    u.includes("tiktok.com/")
  );
}

// Capture outgoing request headers (Referer, Origin, User-Agent, Cookie) so yt-dlp/aria2c can authenticate HLS/MP4
browser.webRequest.onBeforeSendHeaders.addListener(
  (details) => {
    if (details.tabId < 0) return;
    const hdrs = {};
    for (const h of details.requestHeaders || []) {
      const n = h.name.toLowerCase();
      if (n === "referer" || n === "origin" || n === "user-agent" || n === "cookie") {
        hdrs[h.name] = h.value;
      }
    }
    if (Object.keys(hdrs).length > 0) {
      const state = getTabState(details.tabId);
      state.headersByUrl.set(details.url, hdrs);
      if (state.headersByUrl.size > 200) {
        const firstKey = state.headersByUrl.keys().next().value;
        state.headersByUrl.delete(firstKey);
      }
    }
  },
  { urls: ["<all_urls>"] },
  ["requestHeaders"]
);

const MEDIA_EXT_RE = /\.(mp4|mkv|webm|m3u8|mpd|mov|avi|flv|mp3|m4a|aac)(\?|$)/i;
const MEDIA_MIME_PREFIXES = [
  "video/",
  "audio/",
  "application/vnd.apple.mpegurl",
  "application/x-mpegurl",
  "application/dash+xml",
];

browser.webRequest.onHeadersReceived.addListener(
  (details) => {
    if (details.tabId < 0) return;
    const url = details.url;
    const uLow = url.toLowerCase();
    if (
      uLow.includes("googlevideo.com/videoplayback") ||
      uLow.includes("127.0.0.1:16890") ||
      uLow.endsWith(".ts") ||
      uLow.includes(".ts?") ||
      uLow.endsWith(".m4s") ||
      uLow.includes(".m4s?")
    ) {
      return;
    }

    let contentType = "";
    let contentLength = 0;
    for (const h of details.responseHeaders || []) {
      const name = h.name.toLowerCase();
      if (name === "content-type") contentType = (h.value || "").toLowerCase();
      if (name === "content-length") contentLength = parseInt(h.value || "0", 10) || 0;
    }

    // Never register text/html pages (like vidy.st/movie/1368337 iframe wrappers) as downloadable video files!
    if (contentType.includes("text/html")) {
      return;
    }

    const isManifest =
      uLow.includes(".m3u8") ||
      uLow.includes(".mpd") ||
      contentType.includes("mpegurl") ||
      contentType.includes("dash+xml");
    const isMediaMime = MEDIA_MIME_PREFIXES.some((p) => contentType.startsWith(p));
    const isMediaExt = MEDIA_EXT_RE.test(url);

    if (isManifest || isMediaMime || isMediaExt) {
      if (isSegmentOrPreroll(url, contentLength)) {
        return;
      }

      const state = getTabState(details.tabId);
      const rawFile = decodeURIComponent(url.split("?")[0].split("/").pop() || "video.mp4");
      const resLabel = inferResolutionLabel(url);
      const baseTitle = state.pageTitle
        ? state.pageTitle
            .replace(/^\(\d+\)\s*/, "")
            .replace(/\s*[-|]\s*(YouTube|Cuevana.*|Ver.*)/i, "")
            .replace(/^Ver\s+/i, "")
            .replace(/\s+online\s+gratis.*$/i, "")
            .trim()
        : rawFile;
      const displayTitle = resLabel ? `[${resLabel}] ${baseTitle}` : `${baseTitle} (${rawFile})`;
      const cleanMovieFilename = resLabel ? `${baseTitle} [${resLabel}].mp4` : `${baseTitle}.mp4`;

      registerMediaForTab(details.tabId, {
        url,
        title: displayTitle,
        filename: cleanMovieFilename,
        type: isManifest ? "HLS/DASH Stream" : contentType.split(";")[0] || "video/mp4",
        resolution: resLabel,
        size: contentLength,
        isStreamPage: isManifest,
        isRealStream: true,
      });
    }
  },
  { urls: ["<all_urls>"] },
  ["responseHeaders"]
);

// IDM-Style Automatic Browser Download Interception
browser.downloads.onCreated.addListener(async (downloadItem) => {
  if (!interceptDownloads) return;
  const url = downloadItem.url || "";
  if (!url.startsWith("http://") && !url.startsWith("https://") && !url.startsWith("ftp://")) {
    return;
  }
  if (url.includes("127.0.0.1:16890")) return;

  try {
    const pingResp = await fetch(`${BRIDGE_URL}/api/ping`);
    if (!pingResp.ok) return;

    await browser.downloads.cancel(downloadItem.id).catch(() => {});
    await browser.downloads.erase({ id: downloadItem.id }).catch(() => {});

    const filename = downloadItem.filename
      ? downloadItem.filename.split(/[\\/]/).pop()
      : "";

    await fetch(`${BRIDGE_URL}/api/enqueue`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        url,
        filename,
        quality: "best",
        container: "General",
        source_page: downloadItem.referrer || "",
      }),
    });
  } catch (_err) {}
});

browser.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.action === "reportPageVideo" && sender.tab) {
    const tabId = sender.tab.id;
    const state = getTabState(tabId);
    if (msg.title && sender.frameId === 0) {
      state.pageTitle = msg.title;
    }
    // Only register page URL directly if it is a known watch page (YouTube/Twitch/Vimeo) or direct media file,
    // NOT an HTML embed wrapper like vidy.st/movie/...
    if (msg.isStreamPage || (msg.url && MEDIA_EXT_RE.test(msg.url))) {
      registerMediaForTab(tabId, {
        url: msg.url,
        title: msg.title || sender.tab.title || "Video en página",
        type: msg.isStreamPage ? "Streaming" : "HTML5 Video",
        resolution: msg.resolution || "",
        isStreamPage: Boolean(msg.isStreamPage),
        isRealStream: !msg.isStreamPage,
      });
    }
    sendResponse({
      ok: true,
      latestStream: state.latestStream || state.streams[0] || null,
      streams: state.streams.slice(0, 15),
    });
    return true;
  }

  if (msg.action === "getTabMedia") {
    const state = tabStateMap.get(msg.tabId);
    sendResponse({
      items: state ? state.streams : [],
      latestStream: state ? state.latestStream : null,
      interceptDownloads,
    });
    return true;
  }

  if (msg.action === "setIntercept") {
    interceptDownloads = Boolean(msg.value);
    browser.storage.local.set({ interceptDownloads });
    sendResponse({ ok: true, interceptDownloads });
    return true;
  }

  if (msg.action === "fetchQualities") {
    fetch(`${BRIDGE_URL}/api/formats?url=${encodeURIComponent(msg.url)}`)
      .then((r) => r.json())
      .then((data) => sendResponse({ ok: true, data }))
      .catch((err) => sendResponse({ ok: false, error: String(err) }));
    return true;
  }

  if (msg.action === "enqueueDownload") {
    const tabId = sender.tab ? sender.tab.id : msg.tabId;
    const state = tabId != null ? tabStateMap.get(tabId) : null;
    const savedHeaders =
      (state && state.headersByUrl.get(msg.url)) || msg.headers || {};
    const tabUrl =
      (sender.tab && sender.tab.url) ||
      (state && state.pageUrl) ||
      msg.source_page ||
      "";

    fetch(`${BRIDGE_URL}/api/enqueue`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        url: msg.url,
        quality: msg.quality || "best",
        container: msg.container || "Videos Web",
        filename: msg.filename || "",
        source_page: tabUrl,
        headers: savedHeaders,
      }),
    })
      .then((r) => r.json())
      .then((data) => sendResponse({ ok: true, data }))
      .catch((err) => sendResponse({ ok: false, error: String(err) }));
    return true;
  }

  return false;
});
