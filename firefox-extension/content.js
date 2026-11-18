// Mint Downloader - IDM-Style Floating Video Overlay Content Script (Draggable + Real-Time Stream Sync)
(function () {
  if (window.__mintDownloaderInjected) return;
  window.__mintDownloaderInjected = true;

  let activeVideo = null;
  let overlayRoot = null;
  let panelEl = null;
  let dragHandleEl = null;
  let quickBtn = null;
  let resBtn = null;
  let dropdownEl = null;
  let statusBadge = null;

  let latestSniffedStream = null;
  let tabStreamsList = [];
  let cachedQualitiesUrl = "";
  let cachedChoices = null;
  let selectedQuality = "1080p";

  // User-customizable dragged position (persisted across pages)
  let userCustomPos = null; // { left: number, top: number }
  let isDragging = false;
  let dragStartX = 0;
  let dragStartY = 0;
  let panelStartLeft = 0;
  let panelStartTop = 0;

  if (typeof browser !== "undefined" && browser.storage && browser.storage.local) {
    browser.storage.local.get(["idmOverlayPos"]).then((res) => {
      if (res && res.idmOverlayPos && typeof res.idmOverlayPos.left === "number") {
        userCustomPos = res.idmOverlayPos;
      }
    }).catch(() => {});
  }

  function isStreamSite() {
    const h = location.hostname.toLowerCase();
    return (
      h.includes("youtube.com") ||
      h.includes("youtu.be") ||
      h.includes("vimeo.com") ||
      h.includes("twitch.tv") ||
      h.includes("dailymotion.com") ||
      h.includes("tiktok.com")
    );
  }

  function getEffectiveStreamUrl(videoEl) {
    if (isStreamSite()) {
      return location.href;
    }
    // Always prioritize the latest real network stream (.m3u8 / .mpd / .mp4) sniffed by background.js
    if (latestSniffedStream && latestSniffedStream.url) {
      return latestSniffedStream.url;
    }
    if (videoEl) {
      const src = videoEl.currentSrc || videoEl.src || "";
      if (
        src &&
        !src.startsWith("blob:") &&
        !src.toLowerCase().includes("un1v3rs4l.mp4")
      ) {
        return src;
      }
    }
    return "";
  }

  function cleanPageMovieTitle() {
    const raw = (document.title || "").trim();
    if (!raw) return "";
    return raw
      .replace(/^\(\d+\)\s*/, "")
      .replace(/\s*[-|]\s*(YouTube|Cuevana.*|Ver.*)/i, "")
      .replace(/^Ver\s+/i, "")
      .replace(/\s+online\s+gratis.*$/i, "")
      .trim();
  }

  function updateOverlayButtonLabel() {
    if (!quickBtn || !resBtn) return;
    let resText = selectedQuality || "1080p";
    if (activeVideo && activeVideo.videoHeight > 0) {
      resText = `${activeVideo.videoHeight}p`;
    } else if (latestSniffedStream && latestSniffedStream.resolution) {
      resText = latestSniffedStream.resolution;
    }
    if (resText === "best") resText = "Auto";
    if (resText === "audio") resText = "MP3";
    quickBtn.textContent = "⬇ Descargar";
    resBtn.textContent = `Calidad/${resText} ▾`;
  }

  browser.runtime.onMessage.addListener((msg) => {
    if (msg.action === "streamUpdated") {
      if (msg.latestStream) {
        latestSniffedStream = msg.latestStream;
        if (msg.latestStream.resolution) {
          selectedQuality = msg.latestStream.resolution;
        }
      }
      if (Array.isArray(msg.streams)) {
        tabStreamsList = msg.streams;
      }
      cachedQualitiesUrl = "";
      cachedChoices = null;
      updateOverlayButtonLabel();
      if (statusBadge && latestSniffedStream) {
        const badgeRes =
          (activeVideo && activeVideo.videoHeight ? `${activeVideo.videoHeight}p` : "") ||
          latestSniffedStream.resolution ||
          "Listo";
        statusBadge.textContent = `● ${badgeRes}`;
      }
    }
  });

  function createOverlay() {
    if (overlayRoot) return;
    const host = document.createElement("div");
    host.id = "mint-dl-idm-overlay-host";
    host.style.cssText =
      "position:fixed;z-index:2147483647;top:0;left:0;width:0;height:0;pointer-events:none;";
    const shadow = host.attachShadow({ mode: "open" });

    const style = document.createElement("style");
    style.textContent = `
      .idm-bar {
        position: fixed;
        pointer-events: auto;
        display: none;
        align-items: center;
        gap: 6px;
        background: linear-gradient(135deg, rgba(15, 23, 42, 0.96), rgba(15, 118, 110, 0.96));
        color: #f8fafc;
        padding: 5px 9px;
        border-radius: 8px;
        border: 1px solid rgba(46, 204, 113, 0.6);
        box-shadow: 0 6px 18px rgba(0, 0, 0, 0.5);
        font-family: system-ui, -apple-system, sans-serif;
        font-size: 12px;
        font-weight: 600;
        user-select: none;
        backdrop-filter: blur(6px);
      }
      .idm-drag-handle {
        cursor: grab;
        padding: 2px 5px;
        color: #94a3b8;
        font-size: 14px;
        line-height: 1;
        border-radius: 4px;
        display: inline-flex;
        align-items: center;
      }
      .idm-drag-handle:hover {
        color: #ffffff;
        background: rgba(255, 255, 255, 0.12);
      }
      .idm-drag-handle:active {
        cursor: grabbing;
      }
      .idm-btn {
        background: #10b981;
        color: #052e16;
        border: none;
        border-radius: 6px;
        padding: 4px 9px;
        font-weight: 700;
        font-size: 12px;
        cursor: pointer;
        display: inline-flex;
        align-items: center;
        gap: 5px;
      }
      .idm-btn:hover {
        background: #34d399;
      }
      .idm-select-btn {
        background: rgba(255, 255, 255, 0.14);
        color: #ffffff;
        border: 1px solid rgba(255, 255, 255, 0.22);
        border-radius: 6px;
        padding: 4px 7px;
        font-size: 11px;
        cursor: pointer;
      }
      .idm-select-btn:hover {
        background: rgba(255, 255, 255, 0.24);
      }
      .idm-dropdown {
        position: absolute;
        top: 100%;
        right: 0;
        margin-top: 6px;
        min-width: 290px;
        max-height: 310px;
        overflow-y: auto;
        background: #0f172a;
        border: 1px solid rgba(46, 204, 113, 0.55);
        border-radius: 8px;
        box-shadow: 0 10px 25px rgba(0,0,0,0.65);
        display: none;
        flex-direction: column;
        padding: 4px;
      }
      .idm-section {
        font-size: 10px;
        text-transform: uppercase;
        color: #94a3b8;
        padding: 4px 8px 2px;
        letter-spacing: 0.5px;
      }
      .idm-option {
        padding: 6px 10px;
        color: #e2e8f0;
        font-size: 12px;
        border-radius: 5px;
        cursor: pointer;
        display: flex;
        justify-content: space-between;
        align-items: center;
      }
      .idm-option:hover {
        background: rgba(16, 185, 129, 0.25);
        color: #ffffff;
      }
      .idm-status {
        font-size: 11px;
        color: #a7f3d0;
        margin-left: 3px;
      }
    `;

    panelEl = document.createElement("div");
    panelEl.className = "idm-bar";

    dragHandleEl = document.createElement("span");
    dragHandleEl.className = "idm-drag-handle";
    dragHandleEl.textContent = "⠿";
    dragHandleEl.title = "Arrastra para mover la barra a cualquier lugar (doble clic para restablecer posición)";

    quickBtn = document.createElement("button");
    quickBtn.className = "idm-btn";
    quickBtn.textContent = "⬇ Descargar";

    resBtn = document.createElement("button");
    resBtn.className = "idm-select-btn";
    resBtn.textContent = "Calidad/1080p ▾";
    resBtn.title = "Seleccionar calidad de video para descargar";

    statusBadge = document.createElement("span");
    statusBadge.className = "idm-status";

    dropdownEl = document.createElement("div");
    dropdownEl.className = "idm-dropdown";

    // Enable free drag & drop anywhere on the viewport
    const startDrag = (e) => {
      if (e.button !== 0) return;
      const tag = (e.target && e.target.tagName) || "";
      if (tag === "BUTTON" || (e.target && e.target.closest && e.target.closest(".idm-dropdown"))) {
        return;
      }
      e.preventDefault();
      e.stopPropagation();
      isDragging = true;
      dragStartX = e.clientX;
      dragStartY = e.clientY;
      const rect = panelEl.getBoundingClientRect();
      panelStartLeft = rect.left;
      panelStartTop = rect.top;
    };

    dragHandleEl.addEventListener("mousedown", startDrag);
    panelEl.addEventListener("mousedown", startDrag);

    dragHandleEl.addEventListener("dblclick", (e) => {
      e.stopPropagation();
      userCustomPos = null;
      if (typeof browser !== "undefined" && browser.storage && browser.storage.local) {
        browser.storage.local.remove("idmOverlayPos").catch(() => {});
      }
      if (activeVideo) positionOverlayForVideo(activeVideo);
    });

    window.addEventListener(
      "mousemove",
      (e) => {
        if (!isDragging || !panelEl) return;
        const dx = e.clientX - dragStartX;
        const dy = e.clientY - dragStartY;
        const newLeft = Math.max(4, Math.min(window.innerWidth - 120, panelStartLeft + dx));
        const newTop = Math.max(4, Math.min(window.innerHeight - 36, panelStartTop + dy));
        panelEl.style.left = `${newLeft}px`;
        panelEl.style.top = `${newTop}px`;
        userCustomPos = { left: newLeft, top: newTop };
      },
      true
    );

    window.addEventListener(
      "mouseup",
      () => {
        if (isDragging) {
          isDragging = false;
          if (
            userCustomPos &&
            typeof browser !== "undefined" &&
            browser.storage &&
            browser.storage.local
          ) {
            browser.storage.local.set({ idmOverlayPos: userCustomPos }).catch(() => {});
          }
        }
      },
      true
    );

    quickBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      syncWithBackground(activeVideo, (freshStream) => {
        const targetUrl =
          (freshStream && freshStream.url) || getEffectiveStreamUrl(activeVideo);
        if (!targetUrl) {
          statusBadge.textContent = "⏳ Reproduce el video 2s...";
          return;
        }
        const hdrs = (freshStream && freshStream.headers) || {};
        const fname = (freshStream && freshStream.filename) || buildFallbackFilename();
        const qToUse = isStreamSite() ? (selectedQuality || "1080p") : "best";
        triggerDownload(targetUrl, qToUse, hdrs, fname);
      });
    });

    resBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      const isVisible = dropdownEl.style.display === "flex";
      if (isVisible) {
        dropdownEl.style.display = "none";
      } else {
        syncWithBackground(activeVideo, () => {
          openQualitiesMenu(getEffectiveStreamUrl(activeVideo));
        });
      }
    });

    panelEl.appendChild(dragHandleEl);
    panelEl.appendChild(quickBtn);
    panelEl.appendChild(resBtn);
    panelEl.appendChild(statusBadge);
    panelEl.appendChild(dropdownEl);

    shadow.appendChild(style);
    shadow.appendChild(panelEl);
    document.documentElement.appendChild(host);
    overlayRoot = host;
    updateOverlayButtonLabel();
  }

  function buildFallbackFilename() {
    const base = cleanPageMovieTitle();
    if (!base) return "";
    const res =
      (activeVideo && activeVideo.videoHeight ? `${activeVideo.videoHeight}p` : "") ||
      (latestSniffedStream && latestSniffedStream.resolution) ||
      "";
    return res ? `${base} [${res}].mp4` : `${base}.mp4`;
  }

  function openQualitiesMenu(url) {
    dropdownEl.style.display = "flex";
    dropdownEl.innerHTML = "";

    const isYtOrMainStream = isStreamSite();

    // 1. On sites like Cuevana / embedded HLS players: show ONLY the clean captured streams
    // (1 per resolution, updated in real time when you switch quality in the video player)
    if (!isYtOrMainStream && tabStreamsList.length > 0) {
      const hdr = document.createElement("div");
      hdr.className = "idm-section";
      hdr.textContent = "Calidades capturadas del reproductor";
      dropdownEl.appendChild(hdr);

      for (const st of tabStreamsList) {
        const item = document.createElement("div");
        item.className = "idm-option";
        const sizeMb = st.size > 0 ? ` (${(st.size / (1024 * 1024)).toFixed(1)} MB)` : "";
        item.textContent = `🎬 ${st.title}${sizeMb}`;
        item.addEventListener("click", (ev) => {
          ev.stopPropagation();
          dropdownEl.style.display = "none";
          if (st.resolution) {
            selectedQuality = st.resolution;
            resBtn.textContent = `Calidad/${st.resolution} ▾`;
          }
          triggerDownload(st.url, "best", st.headers, st.filename || buildFallbackFilename());
        });
        dropdownEl.appendChild(item);
      }
      return;
    }

    if (!url) return;

    // 2. On YouTube / Twitch / Vimeo: query yt-dlp dynamic qualities for that video
    const hdr2 = document.createElement("div");
    hdr2.className = "idm-section";
    hdr2.textContent = "Seleccionar calidad de descarga";
    dropdownEl.appendChild(hdr2);

    const presets = [
      { id: "best", label: "⚡ Máxima calidad disponible (Auto)" },
      { id: "2160p", label: "🎬 2160p 4K UHD (MP4)" },
      { id: "1440p", label: "🎬 1440p 2K QHD (MP4)" },
      { id: "1080p", label: "🎬 1080p Full HD (MP4)" },
      { id: "720p", label: "🎬 720p HD (MP4)" },
      { id: "480p", label: "🎬 480p SD (MP4)" },
      { id: "360p", label: "🎬 360p (MP4)" },
      { id: "audio", label: "🎵 Solo Audio (MP3 192 kbps)" },
    ];
    const presetContainer = document.createElement("div");
    for (const p of presets) {
      const item = document.createElement("div");
      item.className = "idm-option";
      item.textContent = p.label;
      item.addEventListener("click", (ev) => {
        ev.stopPropagation();
        dropdownEl.style.display = "none";
        selectedQuality = p.id;
        const shortQ = p.id === "best" ? "Auto" : p.id === "audio" ? "MP3" : p.id;
        resBtn.textContent = `Calidad/${shortQ} ▾`;
        triggerDownload(url, p.id, null, buildFallbackFilename());
      });
      presetContainer.appendChild(item);
    }
    dropdownEl.appendChild(presetContainer);

    if (cachedQualitiesUrl === url && cachedChoices) {
      renderDynamicChoicesInto(presetContainer, url, cachedChoices);
      return;
    }

    browser.runtime
      .sendMessage({ action: "fetchQualities", url })
      .then((resp) => {
        if (resp && resp.ok && resp.data && Array.isArray(resp.data.choices)) {
          cachedQualitiesUrl = url;
          cachedChoices = resp.data.choices;
          renderDynamicChoicesInto(presetContainer, url, cachedChoices);
        }
      })
      .catch(() => {});
  }

  function renderDynamicChoicesInto(container, url, choices) {
    container.innerHTML = "";
    for (const c of choices) {
      const item = document.createElement("div");
      item.className = "idm-option";
      item.textContent = c.label;
      item.addEventListener("click", (ev) => {
        ev.stopPropagation();
        dropdownEl.style.display = "none";
        selectedQuality = c.id;
        const shortQ = c.id === "best" ? "Auto" : c.id === "audio" ? "MP3" : c.id;
        resBtn.textContent = `Calidad/${shortQ} ▾`;
        triggerDownload(url, c.id, null, buildFallbackFilename());
      });
      container.appendChild(item);
    }
  }

  function triggerDownload(url, quality, headers, filename) {
    statusBadge.textContent = "Enviando...";
    const hdrs =
      headers && Object.keys(headers).length > 0
        ? headers
        : latestSniffedStream && latestSniffedStream.url === url
        ? latestSniffedStream.headers || {}
        : {};
    const fname =
      filename ||
      (latestSniffedStream && latestSniffedStream.url === url
        ? latestSniffedStream.filename || ""
        : "") ||
      buildFallbackFilename();

    browser.runtime
      .sendMessage({
        action: "enqueueDownload",
        url,
        quality,
        container: "Videos Web",
        headers: hdrs,
        filename: fname,
      })
      .then((resp) => {
        if (resp && resp.ok) {
          statusBadge.textContent = `✓ En cola (${quality})`;
        } else {
          statusBadge.textContent = "⚠️ Abre Mint Downloader";
        }
        setTimeout(() => {
          if (statusBadge) statusBadge.textContent = "";
        }, 3500);
      });
  }

  function positionOverlayForVideo(video) {
    if (!video || !panelEl) return;
    const rect = video.getBoundingClientRect();
    if (rect.width < 160 || rect.height < 100) {
      panelEl.style.display = "none";
      return;
    }
    if (userCustomPos) {
      const clampedLeft = Math.max(4, Math.min(window.innerWidth - 120, userCustomPos.left));
      const clampedTop = Math.max(4, Math.min(window.innerHeight - 36, userCustomPos.top));
      panelEl.style.left = `${clampedLeft}px`;
      panelEl.style.top = `${clampedTop}px`;
      panelEl.style.display = "inline-flex";
      return;
    }
    const top = Math.max(8, rect.top + 10);
    const left = Math.max(8, Math.min(window.innerWidth - 350, rect.right - 335));
    panelEl.style.top = `${top}px`;
    panelEl.style.left = `${left}px`;
    panelEl.style.display = "inline-flex";
  }

  function syncWithBackground(video, onDone) {
    const src = video ? video.currentSrc || video.src || "" : "";
    const resStr = video && video.videoHeight > 0 ? `${video.videoHeight}p` : "";
    browser.runtime
      .sendMessage({
        action: "reportPageVideo",
        url: isStreamSite() ? location.href : src,
        title: document.title,
        resolution: resStr,
        isStreamPage: isStreamSite(),
      })
      .then((resp) => {
        if (resp) {
          if (resp.latestStream) latestSniffedStream = resp.latestStream;
          if (Array.isArray(resp.streams)) tabStreamsList = resp.streams;
          updateOverlayButtonLabel();
        }
        if (onDone) onDone(latestSniffedStream);
      })
      .catch(() => {
        if (onDone) onDone(latestSniffedStream);
      });
  }

  function attachVideoListeners(video) {
    if (video.__mintDlBound) return;
    video.__mintDlBound = true;

    const onVideoChange = () => {
      createOverlay();
      activeVideo = video;
      cachedQualitiesUrl = "";
      cachedChoices = null;
      syncWithBackground(video);
      updateOverlayButtonLabel();
      positionOverlayForVideo(video);
    };

    video.addEventListener("play", onVideoChange);
    video.addEventListener("loadedmetadata", onVideoChange);
    video.addEventListener("loadeddata", onVideoChange);
    video.addEventListener("resize", onVideoChange);
    video.addEventListener("mouseenter", () => {
      createOverlay();
      activeVideo = video;
      syncWithBackground(video);
      updateOverlayButtonLabel();
      positionOverlayForVideo(video);
    });
  }

  function scanVideos() {
    const videos = document.querySelectorAll("video");
    videos.forEach((v) => {
      attachVideoListeners(v);
      if (!v.paused && v.readyState >= 2) {
        createOverlay();
        activeVideo = v;
        updateOverlayButtonLabel();
        positionOverlayForVideo(v);
      }
    });
  }

  window.addEventListener(
    "scroll",
    () => {
      if (activeVideo && panelEl && panelEl.style.display !== "none" && !userCustomPos) {
        positionOverlayForVideo(activeVideo);
      }
    },
    { passive: true }
  );

  setInterval(scanVideos, 1200);
  scanVideos();
})();
