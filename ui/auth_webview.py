#!/usr/bin/env python3
"""
Ventana nativa de inicio de sesión y extractor web silencioso (GTK3 + WebKit2 4.1) para Mi Transfer.
Se ejecuta en un proceso aislado para evitar conflictos entre GTK3/WebKit2 y GTK4.
- Modo normal: ventana de inicio de sesión para guardar cookies + perfil permanente.
- Modo --browse: abre la interfaz web oficial de la nube con la sesión guardada.
- Modo --list-folder: carga la carpeta de la nube de forma 100% invisible (sin abrir ventana)
  usando el perfil persistente guardado y devuelve las carpetas/archivos y el nombre real de la cuenta.
- Modo --silent-refresh: renueva cookies y tokens en segundo plano sin mostrar ventana.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("WebKit2", "4.1")
gi.require_version("Soup", "3.0")

from gi.repository import Gdk, GLib, Gtk, Soup, WebKit2


DESKTOP_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"
)

PROVIDER_LOGIN_CONFIG: Dict[str, Dict[str, Any]] = {
    "gdrive": {
        "name": "GDrive",
        "login_url": "https://accounts.google.com/ServiceLogin?continue=https%3A%2F%2Fdrive.google.com%2Fdrive%2Fmy-drive&service=wise",
        "home_url": "https://drive.google.com/drive/my-drive",
        "success_hosts": ("drive.google.com",),
        "auth_cookies": ("SID", "SAPISID", "__Secure-3PSID", "__Secure-1PSID"),
        "hint": "Inicia sesión en tu cuenta de Google. Al entrar a Google Drive podrás guardar la sesión.",
    },
    "mega": {
        "name": "MEGA",
        "login_url": "https://mega.nz/login",
        "home_url": "https://mega.nz/fm",
        "success_hosts": ("mega.nz",),
        "success_paths": ("/fm",),
        "auth_cookies": (),
        "hint": "Inicia sesión en tu cuenta de MEGA. Al cargar tus archivos pulsa «Guardar sesión».",
    },
    "onedrive": {
        "name": "OneDrive",
        "login_url": "https://onedrive.live.com/?v=login",
        "home_url": "https://onedrive.live.com/",
        "success_hosts": ("onedrive.live.com", "sharepoint.com"),
        "auth_cookies": ("RPSSecAuth", "WLSSC", "FedAuth", "MSPAuth"),
        "hint": "Inicia sesión con tu cuenta de Microsoft / OneDrive y espera a que carguen tus archivos.",
    },
    "dropbox": {
        "name": "Dropbox",
        "login_url": "https://www.dropbox.com/login",
        "home_url": "https://www.dropbox.com/home",
        "success_hosts": ("www.dropbox.com", "dropbox.com"),
        "success_paths": ("/home", "/h", "/work"),
        "auth_cookies": ("jar", "bjar", "lid", "gvc"),
        "hint": "Inicia sesión en tu cuenta de Dropbox. Al entrar a tu inicio pulsa «Guardar sesión».",
    },
    "mediafire": {
        "name": "Mediafire",
        "login_url": "https://www.mediafire.com/login/",
        "home_url": "https://app.mediafire.com/myfiles",
        "success_hosts": ("app.mediafire.com", "www.mediafire.com"),
        "success_paths": ("/myfiles", "/folder", "/file"),
        "auth_cookies": ("ukey", "skey", "user"),
        "hint": "Inicia sesión en Mediafire. Al entrar a «My Files» pulsa «Guardar sesión».",
    },
}


def _build_folder_url(provider: str, folder_id: str, folder_path: str) -> str:
    cfg = PROVIDER_LOGIN_CONFIG.get(provider, PROVIDER_LOGIN_CONFIG["gdrive"])
    fid = (folder_id or "").strip()
    fpath = (folder_path or "").strip("/")
    if provider == "gdrive":
        if fid and fid != "root":
            return f"https://drive.google.com/drive/folders/{urllib.parse.quote(fid)}"
        return "https://drive.google.com/drive/my-drive"
    if provider == "dropbox":
        if fpath:
            encoded = "/".join(urllib.parse.quote(p) for p in fpath.split("/"))
            return f"https://www.dropbox.com/home/{encoded}"
        return "https://www.dropbox.com/home"
    if provider == "mega":
        if fid:
            return f"https://mega.nz/fm/{urllib.parse.quote(fid)}"
        return "https://mega.nz/fm"
    if provider == "onedrive":
        if fid:
            return f"https://onedrive.live.com/?id={urllib.parse.quote(fid)}"
        return "https://onedrive.live.com/"
    if provider == "mediafire":
        if fid and fid != "myfiles":
            return f"https://app.mediafire.com/folder/{urllib.parse.quote(fid)}"
        return "https://app.mediafire.com/myfiles"
    return cfg["home_url"]


# IMPORTANT: MUST be a SYNCHRONOUS function (NOT async!) because WebKit2's evaluate_javascript
# returns the expression value immediately without awaiting Promises.
_EXTRACTOR_JS = r"""
(function(provider, collectEntries, basePath) {
    const out = {
        email: "",
        name: "",
        url: String(window.location.href || ""),
        title: String(document.title || ""),
        tokens: {},
        entries: [],
        needs_login: false
    };

    const emailRe = /[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}/;
    function pickEmail(val) {
        if (!val || out.email) return;
        const m = String(val).match(emailRe);
        if (m) out.email = m[0];
    }
    function pickNameFromGoogleLabel(label) {
        if (!label) return;
        const s = String(label);
        pickEmail(s);
        // Matches: "Cuenta de Google: Usuario Ejemplo\n(usuario@example.com)" or "Google Account: Usuario (usuario@example.com)"
        const m = s.match(/(?:Cuenta de Google|Google Account|Compte Google|Conta do Google)\s*:\s*([^\n(]+)/i);
        if (m && m[1]) {
            const clean = m[1].trim();
            if (clean && !clean.includes("@")) {
                out.name = clean;
                return;
            }
        }
        // Fallback: "Usuario Ejemplo (usuario@example.com)"
        const m2 = s.match(/^([^(\n:]{2,50})\s*\([A-Za-z0-9._%+-]+@/);
        if (m2 && m2[1] && !out.name) {
            const clean2 = m2[1].trim();
            if (clean2 && !clean2.includes("@")) out.name = clean2;
        }
    }
    function parseSizeToBytes(txt) {
        if (!txt) return 0;
        const m = String(txt).replace(",", ".").match(/([\d.]+)\s*(TB|GB|MB|KB|B|bytes?)/i);
        if (!m) return 0;
        const num = parseFloat(m[1]) || 0;
        const u = m[2].toUpperCase();
        if (u.startsWith("T")) return Math.round(num * 1099511627776);
        if (u.startsWith("G")) return Math.round(num * 1073741824);
        if (u.startsWith("M")) return Math.round(num * 1048576);
        if (u.startsWith("K")) return Math.round(num * 1024);
        return Math.round(num);
    }

    const hrefLow = out.url.toLowerCase();
    if (
        hrefLow.includes("accounts.google.com/servicelogin") ||
        hrefLow.includes("accounts.google.com/v3/signin") ||
        hrefLow.includes("accounts.google.com/interactive") ||
        hrefLow.includes("dropbox.com/login") ||
        hrefLow.includes("mega.nz/login") ||
        hrefLow.includes("mediafire.com/login") ||
        hrefLow.includes("login.live.com") ||
        hrefLow.includes("login.microsoftonline.com")
    ) {
        out.needs_login = true;
    }

    if (provider === "mega") {
        try {
            const sid = window.localStorage.getItem("sid") || "";
            const mk = window.localStorage.getItem("k") || "";
            const u = window.localStorage.getItem("u") || "";
            const email = window.localStorage.getItem("email") || window.localStorage.getItem("e") || "";
            if (sid) out.tokens.sid = sid;
            if (mk) out.tokens.master_key_b64 = mk;
            if (u) out.tokens.user_handle = u;
            pickEmail(email);
            const nameEl = document.querySelector(".account-name, .fm-account-name, .top-menu-account-name");
            if (nameEl && nameEl.textContent) out.name = nameEl.textContent.trim();
        } catch (e) {}
    } else if (provider === "onedrive") {
        try {
            const stores = [window.localStorage, window.sessionStorage];
            for (const st of stores) {
                if (!st) continue;
                for (let i = 0; i < st.length; i++) {
                    const k = st.key(i);
                    if (!k) continue;
                    const raw = st.getItem(k) || "";
                    if (!raw || raw.length > 32768) continue;
                    const kl = k.toLowerCase();
                    if (kl.includes("accesstoken") || kl.includes("refreshtoken") || kl.includes("msal") || kl.includes("account") || kl.includes("token")) {
                        try {
                            const obj = JSON.parse(raw);
                            if (obj && typeof obj === "object") {
                                if (obj.username) pickEmail(obj.username);
                                if (obj.name && !out.name) out.name = String(obj.name).trim();
                                const target = String(obj.target || obj.scope || "").toLowerCase();
                                const credType = String(obj.credentialType || "").toLowerCase();
                                if (obj.secret) {
                                    if (credType === "accesstoken" && (target.includes("files") || target.includes("onedrive") || target.includes("graph") || !out.tokens.access_token)) {
                                        out.tokens.access_token = String(obj.secret);
                                        if (obj.expiresOn) out.tokens.expires_at = Number(obj.expiresOn) || 0;
                                        if (obj.clientId) out.tokens.client_id = String(obj.clientId);
                                    } else if (credType === "refreshtoken" && !out.tokens.refresh_token) {
                                        out.tokens.refresh_token = String(obj.secret);
                                        if (obj.clientId) out.tokens.client_id = String(obj.clientId);
                                    }
                                }
                                if (obj.access_token && !out.tokens.access_token) {
                                    out.tokens.access_token = String(obj.access_token);
                                }
                            }
                        } catch (_) {}
                    }
                }
            }
        } catch (e) {}
    } else if (provider === "gdrive") {
        try {
            // 1. Account avatar button aria-label
            const acctNodes = document.querySelectorAll(
                "a[href*='SignOutOptions'], a[aria-label*='Cuenta de Google'], a[aria-label*='Google Account'], a[aria-label*='@'], button[aria-label*='@']"
            );
            for (const n of acctNodes) {
                pickNameFromGoogleLabel(n.getAttribute("aria-label") || n.getAttribute("title") || "");
                if (out.name && out.email) break;
            }
            // 2. Google global data (WIZ_global_data) — email & Drive web API key
            if (window.WIZ_global_data && typeof window.WIZ_global_data === "object") {
                if (!out.email && window.WIZ_global_data.oPEP7c) {
                    pickEmail(window.WIZ_global_data.oPEP7c);
                }
                for (const k of Object.keys(window.WIZ_global_data)) {
                    const v = window.WIZ_global_data[k];
                    if (typeof v !== "string") continue;
                    if (!out.tokens.drive_web_api_key && /^AIzaSy[0-9A-Za-z_-]{33}$/.test(v)) {
                        out.tokens.drive_web_api_key = v;
                    }
                    if (!out.email && v.includes("@") && v.length < 120) {
                        pickEmail(v);
                    }
                }
            }
            // 3. Inline Google account metadata scripts (often contains ["Full Name", ..., "email@gmail.com"])
            if (!out.name && out.email) {
                const scripts = document.querySelectorAll("script");
                const escEmail = out.email.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
                const reName = new RegExp('"([A-Za-zÀ-ÿ][A-Za-zÀ-ÿ0-9 ._-]{1,45})"\\s*,\\s*"' + escEmail + '"');
                const reNameRev = new RegExp('"' + escEmail + '"\\s*,\\s*"([A-Za-zÀ-ÿ][A-Za-zÀ-ÿ0-9 ._-]{1,45})"');
                for (let i = 0; i < Math.min(scripts.length, 40); i++) {
                    const txt = scripts[i].textContent || "";
                    if (!txt.includes(out.email)) continue;
                    const m1 = txt.match(reName) || txt.match(reNameRev);
                    if (m1 && m1[1] && !m1[1].includes("http") && !m1[1].includes("@")) {
                        out.name = m1[1].trim();
                        break;
                    }
                }
            }
        } catch (e) {}
    } else if (provider === "dropbox") {
        try {
            const acctEl = document.querySelector("[aria-label*='@'], .dig-Avatar, [data-testid*='account']");
            if (acctEl) pickNameFromGoogleLabel(acctEl.getAttribute("aria-label") || acctEl.textContent);
            const metaEmail = document.querySelector("meta[name='email']");
            if (metaEmail) pickEmail(metaEmail.getAttribute("content"));
        } catch (e) {}
    } else if (provider === "mediafire") {
        try {
            const nameEl = document.querySelector(".user-name, .account-name, [data-testid='user-name']");
            if (nameEl && nameEl.textContent) out.name = nameEl.textContent.trim();
        } catch (e) {}
    }

    if (!out.email) {
        try {
            const nodes = document.querySelectorAll("[aria-label*='@'], [title*='@']");
            for (const n of nodes) {
                pickNameFromGoogleLabel(n.getAttribute("aria-label") || n.getAttribute("title"));
                if (out.email) break;
            }
        } catch (e) {}
    }

    // --- DOM folder & file extraction for native explorer (zero pop-up window) ---
    if (collectEntries && !out.needs_login) {
        const seen = new Set();
        const prefix = basePath ? basePath.replace(/^\/+|\/+$/g, "") : "";
        function joinPath(name) {
            return prefix ? (prefix + "/" + name) : name;
        }

        if (provider === "gdrive") {
            try {
                // Nudge Drive virtualized grid layout
                try { window.dispatchEvent(new Event("resize")); } catch (_) {}

                const mainArea = document.querySelector("[role='main']") || document.body;
                // Collect candidate elements from both List View ([role='row']) and Grid View ([data-id])
                const rawNodes = [];
                const rows = mainArea.querySelectorAll("[role='row'][data-id], [role='gridcell'][data-id], [data-target][data-id], [data-id]");
                for (const el of rows) rawNodes.push(el);

                const skipNames = /^(mi unidad|my drive|compartidos conmigo|shared with me|recientes|recent|destacados|starred|papelera|trash|spam|almacenamiento|storage|nuevo|new|ordenadores|computers|inicio|home|carpeta|folder|nombre|name|propietario|owner|última modificación|last modified|tamaño del archivo|file size)$/i;

                for (const el of rawNodes) {
                    const fid = (el.getAttribute("data-id") || "").trim();
                    if (!fid || fid.length < 10 || fid.startsWith("_") || fid.includes(" ") || seen.has(fid)) continue;
                    if (el.closest("nav, [role='navigation'], header, [role='banner'], [role='complementary'], [role='menubar'], [role='toolbar']")) continue;
                    // Skip hidden elements
                    if (el.getAttribute("aria-hidden") === "true") continue;

                    const target = (el.getAttribute("data-target") || "").toLowerCase();
                    const aria = (el.getAttribute("aria-label") || "").trim();
                    const innerText = (el.innerText || "").trim();
                    if (!aria && !innerText) continue;

                    let name = "";
                    const nameSelectors = [
                        ".KL4NAf",
                        ".Q5txwe",
                        ".a-s-T",
                        "[data-tooltip-only-on-overflow='true']",
                        "[data-column-field='6'] [data-tooltip]",
                        "[data-column-field='6']",
                        "[data-tooltip]"
                    ];
                    for (const sel of nameSelectors) {
                        const nn = el.querySelector(sel);
                        if (nn) {
                            const cand = (nn.getAttribute("data-tooltip") || nn.textContent || "").trim();
                            if (cand && !skipNames.test(cand)) {
                                name = cand;
                                break;
                            }
                        }
                    }
                    if (!name && aria) {
                        name = aria.split(/\s+(?:Carpeta de Google Drive|Google Drive Folder|Más información|More info|Compartido|Shared)/i)[0].trim();
                    }
                    if (!name && innerText) {
                        const firstLn = innerText.split("\n").map(s => s.trim()).filter(Boolean)[0] || "";
                        name = firstLn;
                    }
                    name = name.replace(/\s*(?:Carpeta de Google Drive|Google Drive Folder|Carpeta compartida|Shared folder|Carpeta|Folder)\s*$/i, "").trim();
                    if (!name || name.length > 260 || skipNames.test(name)) continue;

                    // Detect folder vs file reliably in both Spanish and English Google Drive
                    const subAria = Array.from(el.querySelectorAll("[aria-label], [data-tooltip], [title]"))
                        .slice(0, 8)
                        .map(n => (n.getAttribute("aria-label") || n.getAttribute("data-tooltip") || n.getAttribute("title") || ""))
                        .join(" ");
                    const fullInfo = (aria + " " + subAria + " " + innerText + " " + target).toLowerCase();
                    const hasFileExt = /\.[a-z0-9]{2,5}$/i.test(name);
                    const hasFolderSvg = !!el.querySelector("svg path[d*='M10 4H4'], svg path[d*='M20 6h-8l-2-2']");
                    const isFolder = (
                        target === "folder" ||
                        fullInfo.includes("carpeta de google drive") ||
                        fullInfo.includes("google drive folder") ||
                        fullInfo.includes("carpeta compartida") ||
                        fullInfo.includes("shared folder") ||
                        (hasFolderSvg && !hasFileExt) ||
                        ((fullInfo.includes("carpeta") || fullInfo.includes("folder")) && !hasFileExt)
                    );

                    let sizeBytes = 0;
                    let modStr = "";
                    const lines = innerText.split("\n").map(s => s.trim()).filter(Boolean);
                    for (const ln of lines.slice(1)) {
                        if (!sizeBytes && /[\d.,]+\s*(TB|GB|MB|KB|B|bytes?)\b/i.test(ln)) {
                            sizeBytes = parseSizeToBytes(ln);
                        } else if (!modStr && (/\d{1,2}\s+[a-z]{3}/i.test(ln) || /\d{4}/.test(ln) || /\d{1,2}:\d{2}/.test(ln))) {
                            modStr = ln.slice(0, 24);
                        }
                    }

                    seen.add(fid);
                    out.entries.push({
                        id: fid,
                        name: name,
                        is_folder: isFolder,
                        size: isFolder ? 0 : sizeBytes,
                        modified: modStr,
                        path: joinPath(name),
                        web_url: isFolder
                            ? ("https://drive.google.com/drive/folders/" + encodeURIComponent(fid))
                            : ("https://drive.google.com/file/d/" + encodeURIComponent(fid) + "/view"),
                        download_url: isFolder
                            ? ""
                            : ("https://drive.google.com/uc?export=download&id=" + encodeURIComponent(fid))
                    });
                }

                // Strategy B: Fallback to Google Drive's embedded window._DRIVE_ivd bootstrap data
                if (out.entries.length === 0 && window._DRIVE_ivd) {
                    try {
                        const parsed = typeof window._DRIVE_ivd === "string"
                            ? JSON.parse(window._DRIVE_ivd)
                            : window._DRIVE_ivd;
                        const list = Array.isArray(parsed) && Array.isArray(parsed[0]) ? parsed[0] : [];
                        for (const it of list) {
                            if (!Array.isArray(it) || it.length < 4) continue;
                            const fid = typeof it[0] === "string" ? it[0].trim() : "";
                            const name = typeof it[2] === "string" ? it[2].trim() : "";
                            const mime = typeof it[3] === "string" ? it[3].trim() : "";
                            if (!fid || fid.length < 10 || !name || !mime.includes("/") || seen.has(fid)) continue;
                            const isFolder = mime === "application/vnd.google-apps.folder";
                            const sz = Number(it[13]) || 0;
                            seen.add(fid);
                            out.entries.push({
                                id: fid,
                                name: name,
                                is_folder: isFolder,
                                size: isFolder ? 0 : sz,
                                modified: "",
                                path: joinPath(name),
                                web_url: isFolder
                                    ? ("https://drive.google.com/drive/folders/" + encodeURIComponent(fid))
                                    : ("https://drive.google.com/file/d/" + encodeURIComponent(fid) + "/view"),
                                download_url: isFolder
                                    ? ""
                                    : ("https://drive.google.com/uc?export=download&id=" + encodeURIComponent(fid))
                            });
                        }
                    } catch (_) {}
                }
            } catch (e) {}
        } else if (provider === "dropbox") {
            try {
                const rows = document.querySelectorAll("[data-testid='sl-list-row'], tr.brws-file-row, [role='row']");
                for (const row of rows) {
                    const link = row.querySelector("a[href*='/home/'], a[href*='/preview/'], a[href*='/scl/']");
                    if (!link) continue;
                    const href = link.getAttribute("href") || "";
                    const name = (link.textContent || link.getAttribute("aria-label") || "").trim();
                    if (!name || seen.has(href)) continue;
                    seen.add(href);
                    const fullHref = href.startsWith("http") ? href : ("https://www.dropbox.com" + href);
                    const isFolder = href.includes("/home/") && !href.includes("preview=");
                    const txt = row.innerText || "";
                    out.entries.push({
                        id: href,
                        name: name,
                        is_folder: isFolder,
                        size: isFolder ? 0 : parseSizeToBytes(txt),
                        modified: "",
                        path: joinPath(name),
                        web_url: fullHref,
                        download_url: isFolder ? "" : (fullHref + (fullHref.includes("?") ? "&dl=1" : "?dl=1"))
                    });
                }
            } catch (e) {}
        } else if (provider === "onedrive") {
            try {
                const rows = document.querySelectorAll("[data-automationid='DetailsRow'], [role='row'][data-item-key]");
                for (const row of rows) {
                    const key = row.getAttribute("data-item-key") || "";
                    const nameEl = row.querySelector("[data-automationid='FieldRenderer-name'], button[title], a[title]");
                    const name = (nameEl && (nameEl.getAttribute("title") || nameEl.textContent) || "").trim();
                    if (!name || seen.has(key || name)) continue;
                    seen.add(key || name);
                    const txt = (row.innerText || "").toLowerCase();
                    const isFolder = txt.includes("carpeta") || txt.includes("folder") || !!row.querySelector("[data-icon-name*='Folder']");
                    out.entries.push({
                        id: key || name,
                        name: name,
                        is_folder: isFolder,
                        size: isFolder ? 0 : parseSizeToBytes(row.innerText || ""),
                        modified: "",
                        path: joinPath(name),
                        web_url: out.url,
                        download_url: ""
                    });
                }
            } catch (e) {}
        } else if (provider === "mega") {
            try {
                const rows = document.querySelectorAll(".megaListItem, .file-block, tr.tranfer-filetype-txt");
                for (const row of rows) {
                    const rid = row.getAttribute("id") || row.getAttribute("data-id") || "";
                    const nameEl = row.querySelector(".tranfer-filetype-txt, .block-view-file-name, .filename");
                    const name = (nameEl ? nameEl.textContent : row.textContent || "").trim().split("\n")[0];
                    if (!name || seen.has(rid || name)) continue;
                    seen.add(rid || name);
                    const isFolder = row.classList.contains("folder") || !!row.querySelector(".folder-icon");
                    out.entries.push({
                        id: rid || name,
                        name: name,
                        is_folder: isFolder,
                        size: isFolder ? 0 : parseSizeToBytes(row.innerText || ""),
                        modified: "",
                        path: joinPath(name),
                        web_url: out.url,
                        download_url: ""
                    });
                }
            } catch (e) {}
        }
    }

    return JSON.stringify(out);
})(%PROVIDER_JSON%, %COLLECT_JSON%, %BASE_PATH_JSON%)
"""


def _write_result_json(path: str, payload: Dict[str, Any]) -> None:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


class AuthLoginWindow:
    """Native GTK3 + WebKit2 login/browser/silent-listing runner with persistent per-account profile."""

    def __init__(
        self,
        provider: str,
        profile_dir: str,
        output_json: str = "",
        custom_url: str = "",
        silent_refresh: bool = False,
        browse_mode: bool = False,
        list_folder: bool = False,
        daemon_mode: bool = False,
        folder_id: str = "",
        folder_path: str = "",
        timeout_sec: int = 16,
    ) -> None:
        self.provider = provider
        self.cfg = PROVIDER_LOGIN_CONFIG.get(provider, PROVIDER_LOGIN_CONFIG["gdrive"])
        self.profile_dir = Path(profile_dir)
        self.output_json = output_json
        self.silent_refresh = silent_refresh
        self.browse_mode = browse_mode
        self.daemon_mode = daemon_mode
        self.list_folder = list_folder or daemon_mode
        self.folder_id = folder_id
        self.folder_path = folder_path
        self.headless = silent_refresh or list_folder or daemon_mode
        self.timeout_sec = timeout_sec
        self._poll_attempts = 0
        self._js_in_flight = False
        self._req_timeout_id: int = 0
        self._idle_timeout_id: int = 0
        self._active_req_id: str = ""
        self._busy_request: bool = False

        if custom_url:
            self.start_url = custom_url
        elif self.list_folder:
            self.start_url = _build_folder_url(provider, folder_id, folder_path)
        elif silent_refresh or browse_mode:
            self.start_url = self.cfg["home_url"]
        else:
            self.start_url = self.cfg["login_url"]

        self._result_written = False
        self._saving = False
        self._session_detected = False

        data_dir = self.profile_dir / "data"
        cache_dir = self.profile_dir / "cache"
        data_dir.mkdir(parents=True, exist_ok=True)
        cache_dir.mkdir(parents=True, exist_ok=True)
        for d in (self.profile_dir, data_dir, cache_dir):
            try:
                os.chmod(str(d), 0o700)
            except OSError:
                pass

        self.data_manager = WebKit2.WebsiteDataManager(
            base_data_directory=str(data_dir),
            base_cache_directory=str(cache_dir),
        )
        self.cookie_manager = self.data_manager.get_cookie_manager()
        self.cookie_manager.set_persistent_storage(
            str(self.profile_dir / "cookies.sqlite"),
            WebKit2.CookiePersistentStorage.SQLITE,
        )
        self.cookie_manager.set_accept_policy(WebKit2.CookieAcceptPolicy.ALWAYS)

        self.web_context = WebKit2.WebContext.new_with_website_data_manager(self.data_manager)
        self.web_context.set_cache_model(
            WebKit2.CacheModel.DOCUMENT_VIEWER if self.headless else WebKit2.CacheModel.WEB_BROWSER
        )

        self.settings = WebKit2.Settings()
        self.settings.set_user_agent(DESKTOP_USER_AGENT)
        self.settings.set_enable_javascript(True)
        self.settings.set_javascript_can_open_windows_automatically(not self.headless)
        self.settings.set_enable_html5_local_storage(True)
        self.settings.set_enable_html5_database(True)
        self.settings.set_enable_developer_extras(False)
        if self.headless:
            # Skip images/media/WebGL in headless listing mode to slash latency and RAM
            self.settings.set_auto_load_images(False)
            self.settings.set_enable_media(False)
            self.settings.set_enable_webgl(False)
            self.settings.set_enable_smooth_scrolling(False)

        self.webview = WebKit2.WebView.new_with_context(self.web_context)
        self.webview.set_settings(self.settings)
        self.webview.connect("load-changed", self._on_load_changed)
        self.webview.connect("notify::uri", self._on_uri_changed)
        self.webview.connect("notify::estimated-load-progress", self._on_load_progress)
        self.webview.connect("create", self._on_create_popup)

        if self.headless:
            self._build_offscreen()
            if self.daemon_mode:
                self._setup_daemon_io()
            else:
                self._req_timeout_id = GLib.timeout_add_seconds(self.timeout_sec, self._on_headless_timeout)
                self.webview.load_uri(self.start_url)
        else:
            self._build_window()
            self.webview.load_uri(self.start_url)

    def _build_offscreen(self) -> None:
        """Mount WebView inside an invisible OffscreenWindow so SPAs render fully with zero UI popup."""
        self.win = Gtk.OffscreenWindow()
        self.win.set_default_size(1280, 900)
        self.webview.set_size_request(1280, 900)
        self.win.add(self.webview)
        self.win.show_all()

    def _reset_idle_timer(self) -> None:
        if not self.daemon_mode:
            return
        if self._idle_timeout_id:
            GLib.source_remove(self._idle_timeout_id)
            self._idle_timeout_id = 0
        # Exit cleanly after 90 s of inactivity to keep memory footprint at zero
        self._idle_timeout_id = GLib.timeout_add_seconds(90, self._on_daemon_idle_exit)

    def _on_daemon_idle_exit(self) -> bool:
        self._idle_timeout_id = 0
        if not self._busy_request:
            Gtk.main_quit()
        return False

    def _setup_daemon_io(self) -> None:
        self._stdin_buf = ""
        self._reset_idle_timer()
        GLib.io_add_watch(
            sys.stdin.fileno(),
            GLib.IOCondition.IN | GLib.IOCondition.HUP | GLib.IOCondition.ERR,
            self._on_daemon_stdin,
        )
        # Signal parent that warm WebKit2 daemon is ready
        sys.stdout.write(json.dumps({"ready": True, "provider": self.provider}) + "\n")
        sys.stdout.flush()

    def _on_daemon_stdin(self, fd: int, condition: Any) -> bool:
        if condition & (GLib.IOCondition.HUP | GLib.IOCondition.ERR):
            Gtk.main_quit()
            return False
        try:
            chunk = os.read(fd, 8192)
        except OSError:
            Gtk.main_quit()
            return False
        if not chunk:
            Gtk.main_quit()
            return False
        self._stdin_buf += chunk.decode("utf-8", errors="replace")
        while "\n" in self._stdin_buf:
            line, self._stdin_buf = self._stdin_buf.split("\n", 1)
            line = line.strip()
            if not line:
                continue
            try:
                cmd = json.loads(line)
            except Exception:
                continue
            if cmd.get("cmd") == "quit":
                Gtk.main_quit()
                return False
            if cmd.get("cmd") == "list":
                self._start_daemon_list_request(cmd)
        return True

    def _start_daemon_list_request(self, cmd: Dict[str, Any]) -> None:
        self._reset_idle_timer()
        if self._req_timeout_id:
            GLib.source_remove(self._req_timeout_id)
            self._req_timeout_id = 0
        self._active_req_id = str(cmd.get("req_id") or "")
        self.folder_id = str(cmd.get("folder_id") or "")
        self.folder_path = str(cmd.get("folder_path") or "")
        req_timeout = int(cmd.get("timeout") or self.timeout_sec)
        self._busy_request = True
        self._saving = False
        self._result_written = False
        self._poll_attempts = 0
        self._js_in_flight = False
        target_url = _build_folder_url(self.provider, self.folder_id, self.folder_path)
        self._req_timeout_id = GLib.timeout_add_seconds(max(8, req_timeout), self._on_headless_timeout)
        self.webview.load_uri(target_url)

    def _apply_css(self) -> None:
        css = b"""
        window.mint-auth-win {
            background-color: #12141a;
            color: #e2e8f0;
        }
        .auth-topbar {
            background-color: #181b24;
            border-bottom: 1px solid rgba(255, 255, 255, 0.08);
            padding: 8px 12px;
        }
        .auth-bottombar {
            background-color: #161922;
            border-top: 1px solid rgba(255, 255, 255, 0.08);
            padding: 6px 12px;
        }
        .auth-provider-pill {
            background-color: rgba(16, 185, 129, 0.16);
            color: #34d399;
            border: 1px solid rgba(52, 211, 153, 0.40);
            border-radius: 99px;
            padding: 3px 10px;
            font-weight: 700;
            font-size: 12px;
        }
        .auth-url-entry {
            background-color: #12141a;
            color: #cbd5e1;
            border: 1px solid rgba(255, 255, 255, 0.10);
            border-radius: 8px;
            padding: 4px 10px;
            font-size: 12px;
        }
        .auth-status-wait {
            color: #94a3b8;
            font-size: 12px;
            font-weight: 600;
        }
        .auth-status-ready {
            color: #34d399;
            font-size: 12px;
            font-weight: 700;
        }
        .auth-btn-save {
            background-image: linear-gradient(135deg, #10b981, #059669);
            background-color: #10b981;
            color: #ffffff;
            border: 1px solid #34d399;
            border-radius: 8px;
            padding: 6px 14px;
            font-weight: 700;
        }
        .auth-btn-save:hover {
            background-image: linear-gradient(135deg, #34d399, #10b981);
        }
        .auth-btn-nav {
            background-color: rgba(255, 255, 255, 0.06);
            background-image: none;
            color: #e2e8f0;
            border: 1px solid rgba(255, 255, 255, 0.10);
            border-radius: 8px;
            padding: 5px 9px;
        }
        .auth-btn-nav:hover {
            background-color: rgba(255, 255, 255, 0.12);
        }
        progressbar.auth-loadbar trough {
            min-height: 3px;
            background-color: #12141a;
        }
        progressbar.auth-loadbar progress {
            min-height: 3px;
            background-color: #10b981;
        }
        """
        provider = Gtk.CssProvider()
        provider.load_from_data(css)
        screen = Gdk.Screen.get_default()
        if screen:
            Gtk.StyleContext.add_provider_for_screen(
                screen, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )

    def _build_window(self) -> None:
        self._apply_css()
        win_title = (
            f"{self.cfg['name']} — Mi Transfer"
            if self.browse_mode
            else f"Iniciar sesión en {self.cfg['name']} — Mi Transfer"
        )
        self.win = Gtk.Window(title=win_title)
        self.win.get_style_context().add_class("mint-auth-win")
        self.win.set_default_size(1100 if self.browse_mode else 980, 740 if self.browse_mode else 720)
        self.win.set_position(Gtk.WindowPosition.CENTER)
        self.win.connect("delete-event", self._on_delete_event)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        topbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        topbar.get_style_context().add_class("auth-topbar")

        btn_back = Gtk.Button.new_from_icon_name("go-previous-symbolic", Gtk.IconSize.BUTTON)
        btn_back.get_style_context().add_class("auth-btn-nav")
        btn_back.set_tooltip_text("Atrás")
        btn_back.connect("clicked", lambda _: self.webview.go_back())
        topbar.pack_start(btn_back, False, False, 0)

        btn_reload = Gtk.Button.new_from_icon_name("view-refresh-symbolic", Gtk.IconSize.BUTTON)
        btn_reload.get_style_context().add_class("auth-btn-nav")
        btn_reload.set_tooltip_text("Recargar página")
        btn_reload.connect("clicked", lambda _: self.webview.reload())
        topbar.pack_start(btn_reload, False, False, 0)

        lbl_prov = Gtk.Label(label=self.cfg["name"])
        lbl_prov.get_style_context().add_class("auth-provider-pill")
        topbar.pack_start(lbl_prov, False, False, 0)

        self.url_entry = Gtk.Entry()
        self.url_entry.set_editable(False)
        self.url_entry.set_can_focus(False)
        self.url_entry.set_text(self.start_url)
        self.url_entry.get_style_context().add_class("auth-url-entry")
        topbar.pack_start(self.url_entry, True, True, 0)

        save_label = "Sincronizar sesión y cerrar" if self.browse_mode else "Guardar sesión"
        self.btn_save = Gtk.Button(label=save_label)
        self.btn_save.get_style_context().add_class("auth-btn-save")
        self.btn_save.set_tooltip_text(
            "Guarda permanentemente esta sesión para Mi Transfer (Subidas y Descargas)"
        )
        self.btn_save.connect("clicked", lambda _: self._capture_and_finish())
        topbar.pack_end(self.btn_save, False, False, 0)

        btn_cancel = Gtk.Button(label="Cerrar" if self.browse_mode else "Cancelar")
        btn_cancel.get_style_context().add_class("auth-btn-nav")
        btn_cancel.connect(
            "clicked",
            lambda _: self._capture_and_finish() if self.browse_mode else self._cancel_and_quit(),
        )
        topbar.pack_end(btn_cancel, False, False, 0)

        root.pack_start(topbar, False, False, 0)

        self.progress_bar = Gtk.ProgressBar()
        self.progress_bar.get_style_context().add_class("auth-loadbar")
        root.pack_start(self.progress_bar, False, False, 0)

        root.pack_start(self.webview, True, True, 0)

        bottombar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        bottombar.get_style_context().add_class("auth-bottombar")

        hint_txt = (
            f"Explorando {self.cfg['name']} en interfaz web con tu sesión guardada."
            if self.browse_mode
            else self.cfg["hint"]
        )
        self.status_lbl = Gtk.Label(label=hint_txt)
        self.status_lbl.set_xalign(0.0)
        self.status_lbl.set_ellipsize(3)
        self.status_lbl.get_style_context().add_class("auth-status-wait")
        bottombar.pack_start(self.status_lbl, True, True, 0)

        shared_lbl = Gtk.Label(label="Mi Transfer · Sesión permanente (Subidas + Descargas)")
        shared_lbl.get_style_context().add_class("auth-status-wait")
        bottombar.pack_end(shared_lbl, False, False, 0)

        root.pack_start(bottombar, False, False, 0)

        self.win.add(root)
        self.win.show_all()

    def _on_create_popup(self, _webview: WebKit2.WebView, _nav_action: WebKit2.NavigationAction) -> Optional[WebKit2.WebView]:
        if self.headless:
            return None
        popup_win = Gtk.Window(title=f"Verificación — {self.cfg['name']}")
        popup_win.set_default_size(620, 680)
        popup_win.set_transient_for(self.win)
        popup_view = WebKit2.WebView.new_with_related_view(self.webview)
        popup_view.set_settings(self.settings)
        popup_view.connect("close", lambda _v: popup_win.destroy())
        popup_win.add(popup_view)
        popup_win.show_all()
        return popup_view

    def _on_uri_changed(self, webview: WebKit2.WebView, _pspec: Any) -> None:
        if self.headless:
            return
        uri = webview.get_uri() or ""
        if uri:
            self.url_entry.set_text(uri)
        self._check_login_state()

    def _on_load_progress(self, webview: WebKit2.WebView, _pspec: Any) -> None:
        if self.headless:
            return
        prog = webview.get_estimated_load_progress()
        self.progress_bar.set_fraction(0.0 if (prog >= 0.99 or prog <= 0.0) else prog)

    def _on_load_changed(self, _webview: WebKit2.WebView, event: WebKit2.LoadEvent) -> None:
        if event != WebKit2.LoadEvent.FINISHED:
            return
        if not self.headless:
            self.progress_bar.set_fraction(0.0)
            self._check_login_state()
            return
        if self.daemon_mode and not self._busy_request:
            return
        if self.list_folder:
            GLib.timeout_add(180, self._poll_headless_folder)
        elif self.silent_refresh:
            GLib.timeout_add(900, self._trigger_headless_capture)

    def _build_extractor_js(self, collect_entries: bool) -> str:
        return (
            _EXTRACTOR_JS.replace("%PROVIDER_JSON%", json.dumps(self.provider))
            .replace("%COLLECT_JSON%", "true" if collect_entries else "false")
            .replace("%BASE_PATH_JSON%", json.dumps(self.folder_path))
        )

    def _poll_headless_folder(self) -> bool:
        if self._saving or self._result_written or self._js_in_flight:
            return False
        self._poll_attempts += 1
        self._js_in_flight = True
        js = self._build_extractor_js(collect_entries=True)
        try:
            self.webview.evaluate_javascript(js, -1, None, None, None, self._on_headless_poll_done)
        except Exception:
            self._js_in_flight = False
            self._finish_with_js_info({})
        return False

    def _on_headless_poll_done(self, webview: WebKit2.WebView, result: Any) -> None:
        self._js_in_flight = False
        info: Dict[str, Any] = {}
        try:
            js_val = webview.evaluate_javascript_finish(result)
            if js_val is not None:
                raw = js_val.to_string()
                if raw and raw.startswith("{"):
                    parsed = json.loads(raw)
                    if isinstance(parsed, dict):
                        info = parsed
        except Exception:
            info = {}

        entries = info.get("entries") if isinstance(info.get("entries"), list) else []
        tokens = info.get("tokens") if isinstance(info.get("tokens"), dict) else {}
        needs_login = bool(info.get("needs_login"))
        has_api_tokens = bool(
            (self.provider == "mega" and tokens.get("sid") and tokens.get("master_key_b64"))
            or (self.provider == "onedrive" and tokens.get("access_token"))
            or (self.provider == "mediafire" and tokens.get("session_token"))
        )

        # Return immediately as soon as entries or tokens are available (no wasted extra sleep)
        if needs_login or has_api_tokens or entries or self._poll_attempts >= 20:
            self._saving = True
            self._finish_with_js_info(info)
            return

        GLib.timeout_add(280, self._poll_headless_folder)

    def _trigger_headless_capture(self) -> bool:
        if not self._saving and not self._result_written:
            self._capture_and_finish()
        return False

    def _on_headless_timeout(self) -> bool:
        if not self._saving and not self._result_written:
            self._capture_and_finish()
        return False

    def _url_looks_logged_in(self, uri: str) -> bool:
        if not uri:
            return False
        parsed = urlparse(uri)
        host = (parsed.hostname or "").lower()
        path = (parsed.path or "").lower()
        hosts = self.cfg.get("success_hosts", ())
        paths = self.cfg.get("success_paths", ())

        host_ok = any(host == h or host.endswith("." + h) for h in hosts)
        if not host_ok:
            return False
        if "login" in path or "signin" in path or "servicelogin" in path:
            return False
        if paths:
            return any(path.startswith(p) for p in paths)
        return True

    def _check_login_state(self) -> None:
        uri = self.webview.get_uri() or ""
        if not self._url_looks_logged_in(uri):
            return
        self.cookie_manager.get_all_cookies(None, self._on_check_cookies_done)

    def _on_check_cookies_done(self, mgr: WebKit2.CookieManager, result: Any) -> None:
        try:
            cookies = mgr.get_all_cookies_finish(result) or []
        except Exception:
            cookies = []
        names = {c.get_name() for c in cookies}
        req_cookies = self.cfg.get("auth_cookies", ())
        if not req_cookies or any(rc in names for rc in req_cookies):
            if not self._session_detected:
                self._session_detected = True
                self.status_lbl.set_text(
                    f"✓ Sesión iniciada en {self.cfg['name']}. Pulsa «Guardar sesión» para sincronizar."
                )
                self.status_lbl.get_style_context().remove_class("auth-status-wait")
                self.status_lbl.get_style_context().add_class("auth-status-ready")
                if not self.browse_mode:
                    self.btn_save.set_label("✓ Guardar sesión")

    def _capture_and_finish(self) -> None:
        if self._saving or self._result_written:
            return
        self._saving = True
        if not self.headless:
            self.btn_save.set_sensitive(False)
            self.btn_save.set_label("Guardando...")
            self.status_lbl.set_text("Capturando cookies, nombre de cuenta y sesión...")

        js = self._build_extractor_js(collect_entries=self.list_folder or self.browse_mode)
        try:
            self.webview.evaluate_javascript(
                js, -1, None, None, None, self._on_js_extracted
            )
        except Exception:
            self._finish_with_js_info({})

    def _on_js_extracted(self, webview: WebKit2.WebView, result: Any) -> None:
        info: Dict[str, Any] = {}
        try:
            js_val = webview.evaluate_javascript_finish(result)
            if js_val is not None:
                raw = js_val.to_string()
                if raw and raw.startswith("{"):
                    parsed = json.loads(raw)
                    if isinstance(parsed, dict):
                        info = parsed
        except Exception:
            info = {}
        self._finish_with_js_info(info)

    def _finish_with_js_info(self, js_info: Dict[str, Any]) -> None:
        self._js_info = js_info
        self.cookie_manager.get_all_cookies(None, self._on_final_cookies_done)

    def _on_final_cookies_done(self, mgr: WebKit2.CookieManager, result: Any) -> None:
        serialized: List[Dict[str, Any]] = []
        try:
            cookies = mgr.get_all_cookies_finish(result) or []
            for c in cookies:
                exp_unix = 0
                try:
                    dt = c.get_expires()
                    if dt is not None:
                        exp_unix = int(dt.to_unix())
                except Exception:
                    exp_unix = 0
                serialized.append(
                    {
                        "name": str(c.get_name() or ""),
                        "value": str(c.get_value() or ""),
                        "domain": str(c.get_domain() or ""),
                        "path": str(c.get_path() or "/"),
                        "secure": bool(c.get_secure()),
                        "http_only": bool(c.get_http_only()),
                        "expires": exp_unix,
                    }
                )
        except Exception:
            serialized = []

        js_info = getattr(self, "_js_info", {}) or {}
        email = str(js_info.get("email") or "").strip()
        name = str(js_info.get("name") or "").strip()
        tokens = js_info.get("tokens") if isinstance(js_info.get("tokens"), dict) else {}
        entries = js_info.get("entries") if isinstance(js_info.get("entries"), list) else []
        needs_login = bool(js_info.get("needs_login"))
        current_url = self.webview.get_uri() or str(js_info.get("url") or "")

        if not email:
            m = re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", str(js_info.get("title") or ""))
            if m:
                email = m.group(0)

        payload = {
            "ok": True,
            "cancelled": False,
            "req_id": self._active_req_id,
            "provider": self.provider,
            "account_email": email,
            "account_name": name,
            "current_url": current_url,
            "profile_dir": str(self.profile_dir),
            "cookies": serialized,
            "tokens": tokens,
            "entries": entries,
            "needs_login": needs_login,
        }
        if self._req_timeout_id:
            GLib.source_remove(self._req_timeout_id)
            self._req_timeout_id = 0

        if self.daemon_mode:
            self._busy_request = False
            self._saving = False
            self._result_written = True
            self._reset_idle_timer()
            sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
            sys.stdout.flush()
            return

        self._result_written = True
        _write_result_json(self.output_json, payload)
        Gtk.main_quit()

    def _cancel_and_quit(self) -> None:
        if not self._result_written:
            self._result_written = True
            if self.output_json:
                _write_result_json(
                    self.output_json,
                    {"ok": False, "cancelled": True, "provider": self.provider},
                )
        Gtk.main_quit()

    def _on_delete_event(self, _win: Gtk.Window, _ev: Any) -> bool:
        if self.browse_mode:
            # In browse mode, capture updated cookies + account name before closing
            self._capture_and_finish()
            return True
        self._cancel_and_quit()
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Ventana web y extractor silencioso Mi Transfer")
    parser.add_argument("--provider", required=True, choices=list(PROVIDER_LOGIN_CONFIG.keys()))
    parser.add_argument("--profile-dir", required=True)
    parser.add_argument("--output-json", default="")
    parser.add_argument("--url", default="")
    parser.add_argument("--silent-refresh", action="store_true")
    parser.add_argument("--browse", action="store_true")
    parser.add_argument("--list-folder", action="store_true")
    parser.add_argument("--daemon", action="store_true")
    parser.add_argument("--folder-id", default="")
    parser.add_argument("--folder-path", default="")
    parser.add_argument("--timeout", type=int, default=16)
    args = parser.parse_args()

    AuthLoginWindow(
        provider=args.provider,
        profile_dir=args.profile_dir,
        output_json=args.output_json,
        custom_url=args.url,
        silent_refresh=args.silent_refresh,
        browse_mode=args.browse,
        list_folder=args.list_folder,
        daemon_mode=args.daemon,
        folder_id=args.folder_id,
        folder_path=args.folder_path,
        timeout_sec=args.timeout,
    )
    Gtk.main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
