"""
Native Asynchronous Cloud Uploaders for Mint Uploader.
Implements chunked/resumable uploads, persistent OAuth2/session auto-refresh,
pause/stop cancellation, and real-time speed/progress telemetry for:
  1. GDrive    (Google Drive REST API v3)
  2. MEGA      (Native MEGA API v2 + AES-128-CTR + CBC-MAC)
  3. OneDrive  (Microsoft Graph API v1.0 Resumable Upload Session)
  4. Dropbox   (Dropbox API v2 Chunked Upload Session)
  5. Mediafire (MediaFire REST API v1.5 Simple & Resumable Upload)
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import html
import json
import mimetypes
import os
import re
import secrets
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import aiohttp
from aiohttp import web

try:
    from Cryptodome.Cipher import AES
    from Cryptodome.Util import Counter
    _HAS_CRYPTODOME = True
except ImportError:
    _HAS_CRYPTODOME = False
    from cryptography.hazmat.backends import default_backend
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


DEFAULT_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

OAUTH_REDIRECT_PORTS = (16895, 16896, 16897, 16898, 16899)


class UploadError(Exception):
    """Base error for cloud upload operations."""


class UploadAuthError(UploadError):
    """Raised when credentials/tokens are invalid, revoked, or password changed."""


class UploadCancelledError(Exception):
    """Internal control signal raised when an upload is paused or stopped."""


@dataclass
class UploadProgress:
    uploaded_bytes: int = 0
    total_bytes: int = 0
    speed: float = 0.0
    eta: int = 0
    status: str = "uploading"


@dataclass
class CloudEntry:
    """Represents a remote file or folder inside a synchronized cloud account."""
    id: str
    name: str
    is_folder: bool
    size: int = 0
    modified: str = ""
    path: str = ""
    web_url: str = ""
    download_url: str = ""
    provider: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64url_decode(data: str) -> bytes:
    data = data.replace("-", "+").replace("_", "/").replace(",", "")
    pad = (-len(data)) % 4
    if pad:
        data += "=" * pad
    return base64.b64decode(data)


def _open_browser_url(url: str) -> None:
    try:
        subprocess.Popen(
            ["xdg-open", url],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass


def parse_google_credentials_json(filepath: str) -> tuple[str, str]:
    """Extract (client_id, client_secret) from a Google Cloud OAuth credentials.json file."""
    with open(filepath, "r", encoding="utf-8") as f:
        data = json.load(f)
    block = data.get("installed") or data.get("web") or data
    cid = str(block.get("client_id") or "").strip()
    csec = str(block.get("client_secret") or "").strip()
    if not cid:
        raise UploadError("El archivo JSON no contiene un 'client_id' válido de Google OAuth.")
    return cid, csec


async def _throttle_speed(chunk_bytes: int, elapsed: float, speed_limit_kbps: int) -> None:
    if speed_limit_kbps <= 0 or chunk_bytes <= 0:
        return
    target_time = chunk_bytes / (speed_limit_kbps * 1024.0)
    if target_time > elapsed:
        await asyncio.sleep(min(2.0, target_time - elapsed))


# ---------------------------------------------------------------------------
# Built-in Local OAuth2 Loopback Flow (PKCE + Permanent Refresh Token)
# ---------------------------------------------------------------------------

OAUTH_CONFIGS: Dict[str, Dict[str, str]] = {
    "gdrive": {
        "auth_url": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_url": "https://oauth2.googleapis.com/token",
        "scope": "https://www.googleapis.com/auth/drive.file https://www.googleapis.com/auth/userinfo.email",
    },
    "onedrive": {
        "auth_url": "https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
        "token_url": "https://login.microsoftonline.com/common/oauth2/v2.0/token",
        "scope": "offline_access Files.ReadWrite User.Read",
    },
    "dropbox": {
        "auth_url": "https://www.dropbox.com/oauth2/authorize",
        "token_url": "https://api.dropboxapi.com/oauth2/token",
        "scope": "",
    },
}


async def run_browser_oauth2_flow(
    session: aiohttp.ClientSession,
    provider: str,
    client_id: str,
    client_secret: str = "",
    cancel_event: Optional[asyncio.Event] = None,
    timeout_sec: float = 240.0,
) -> Dict[str, Any]:
    """
    Start a temporary local HTTP loopback server (127.0.0.1:16895/oauth/callback),
    open the user's browser for OAuth2 consent with PKCE, and exchange the code
    for an access_token + permanent refresh_token.
    """
    cfg = OAUTH_CONFIGS.get(provider)
    if not cfg:
        raise UploadError(f"Proveedor OAuth2 no soportado: {provider}")
    if not client_id.strip():
        raise UploadError("Debes ingresar el ID de cliente (Client ID) para iniciar sesión en el navegador.")

    code_verifier = _b64url_encode(secrets.token_bytes(48))
    code_challenge = _b64url_encode(hashlib.sha256(code_verifier.encode("ascii")).digest())
    state_token = secrets.token_urlsafe(24)

    result_future: asyncio.Future[tuple[str, str]] = asyncio.get_running_loop().create_future()

    async def _handle_callback(req: web.Request) -> web.Response:
        err = req.query.get("error")
        err_desc = req.query.get("error_description") or ""
        code = req.query.get("code") or ""
        state = req.query.get("state") or ""

        if not result_future.done():
            if err:
                result_future.set_result(("", f"{err}: {err_desc}".strip(": ")))
            elif state != state_token:
                result_future.set_result(("", "Estado de seguridad OAuth inválido (state mismatch)."))
            elif not code:
                result_future.set_result(("", "No se recibió el código de autorización."))
            else:
                result_future.set_result((code, ""))

        if err or not code or state != state_token:
            safe_err = html.escape(err or "Código inválido")
            safe_desc = html.escape(err_desc)
            html_body = (
                "<!DOCTYPE html><html lang='es'><meta charset='utf-8'>"
                "<body style='background:#12141a;color:#f8fafc;font-family:sans-serif;text-align:center;padding:60px'>"
                "<h2 style='color:#f87171'>No se pudo completar la sincronización</h2>"
                f"<p>{safe_err} {safe_desc}</p>"
                "<p>Puedes cerrar esta pestaña y reintentar en Mi Transfer.</p></body></html>"
            )
            return web.Response(text=html_body, content_type="text/html")

        html_body = (
            "<!DOCTYPE html><html lang='es'><meta charset='utf-8'>"
            "<body style='background:#12141a;color:#f8fafc;font-family:sans-serif;text-align:center;padding:60px'>"
            "<h2 style='color:#34d399'>✓ Cuenta sincronizada con Mi Transfer</h2>"
            "<p>Tu sesión se guardó de forma permanente. Ya puedes cerrar esta pestaña.</p></body></html>"
        )
        return web.Response(text=html_body, content_type="text/html")

    app = web.Application()
    app.router.add_get("/oauth/callback", _handle_callback)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()

    bound_port = 0
    for port in OAUTH_REDIRECT_PORTS:
        try:
            site = web.TCPSite(runner, "127.0.0.1", port)
            await site.start()
            bound_port = port
            break
        except OSError:
            continue

    if not bound_port:
        await runner.cleanup()
        raise UploadError("No se pudo abrir un puerto local (16895-16899) para el retorno OAuth.")

    redirect_uri = f"http://127.0.0.1:{bound_port}/oauth/callback"

    params: Dict[str, str] = {
        "client_id": client_id.strip(),
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "state": state_token,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    if cfg["scope"]:
        params["scope"] = cfg["scope"]
    if provider == "gdrive":
        params["access_type"] = "offline"
        params["prompt"] = "consent"
    elif provider == "dropbox":
        params["token_access_type"] = "offline"

    auth_url = f"{cfg['auth_url']}?{urllib.parse.urlencode(params)}"
    _open_browser_url(auth_url)

    try:
        waiters = [result_future]
        cancel_task = None
        if cancel_event:
            cancel_task = asyncio.create_task(cancel_event.wait())
            waiters.append(cancel_task)
        done, _ = await asyncio.wait(waiters, timeout=timeout_sec, return_when=asyncio.FIRST_COMPLETED)
        if cancel_task and not cancel_task.done():
            cancel_task.cancel()
        if not result_future.done():
            if cancel_event and cancel_event.is_set():
                raise UploadCancelledError("Sincronización cancelada por el usuario.")
            raise UploadError("Tiempo de espera agotado esperando la autorización en el navegador.")
        code, err_msg = result_future.result()
        if err_msg or not code:
            raise UploadAuthError(f"Error de autorización: {err_msg}")
    finally:
        await runner.cleanup()

    token_data: Dict[str, str] = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": client_id.strip(),
        "redirect_uri": redirect_uri,
        "code_verifier": code_verifier,
    }
    if client_secret.strip():
        token_data["client_secret"] = client_secret.strip()

    async with session.post(cfg["token_url"], data=token_data) as resp:
        raw = await resp.json(content_type=None)
        if resp.status >= 400 or "error" in raw:
            desc = raw.get("error_description") or raw.get("error") or f"HTTP {resp.status}"
            raise UploadAuthError(f"No se pudo intercambiar el token OAuth2: {desc}")

    expires_in = int(raw.get("expires_in") or 3600)
    return {
        "auth_type": "oauth2",
        "client_id": client_id.strip(),
        "client_secret": client_secret.strip(),
        "access_token": str(raw.get("access_token") or ""),
        "refresh_token": str(raw.get("refresh_token") or ""),
        "expires_at": time.time() + max(60, expires_in - 60),
    }


async def run_rclone_authorize(provider: str) -> Dict[str, Any]:
    """
    Optional zero-config browser OAuth helper if the user already has `rclone` installed.
    Runs `rclone authorize <backend>` and extracts the resulting permanent OAuth2 token JSON.
    """
    rclone_bin = shutil.which("rclone")
    if not rclone_bin:
        raise UploadError("No se encontró 'rclone' instalado en el sistema.")

    backend_map = {"gdrive": "drive", "onedrive": "onedrive", "dropbox": "dropbox"}
    backend = backend_map.get(provider)
    if not backend:
        raise UploadError(f"El proveedor {provider} no usa OAuth de rclone.")

    proc = await asyncio.create_subprocess_exec(
        rclone_bin,
        "authorize",
        backend,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=240.0)
    except asyncio.TimeoutError:
        proc.kill()
        raise UploadError("Tiempo de espera agotado en la autorización con rclone.")

    out_txt = (stdout_b or b"").decode("utf-8", errors="replace")
    m = re.search(r"--->\s*(\{.*?\})\s*<---", out_txt, re.S)
    if not m:
        err_txt = (stderr_b or b"").decode("utf-8", errors="replace").strip()
        raise UploadAuthError(f"No se obtuvo el token desde rclone: {err_txt[:160] or 'cancelado'}")

    tok = json.loads(m.group(1))
    return {
        "auth_type": "rclone_token",
        "access_token": str(tok.get("access_token") or ""),
        "refresh_token": str(tok.get("refresh_token") or ""),
        "expires_at": time.time() + 3000,
    }


def _build_cookie_header(cookies: Any, domain_suffixes: tuple[str, ...]) -> str:
    """Build an HTTP Cookie header string from stored web_session cookies matching domain_suffixes."""
    if not isinstance(cookies, list):
        return ""
    pairs: list[str] = []
    seen: set[str] = set()
    for c in cookies:
        if not isinstance(c, dict):
            continue
        name = str(c.get("name") or "").strip()
        val = str(c.get("value") or "")
        dom = str(c.get("domain") or "").lower().lstrip(".")
        if not name or name in seen:
            continue
        if domain_suffixes and not any(dom == s or dom.endswith("." + s) for s in domain_suffixes):
            continue
        seen.add(name)
        pairs.append(f"{name}={val}")
    return "; ".join(pairs)


async def run_web_login_window(
    provider: str,
    profile_dir: str,
    silent_refresh: bool = False,
    browse_mode: bool = False,
    custom_url: str = "",
    timeout_sec: float = 600.0,
) -> Dict[str, Any]:
    """
    Launch the native isolated GTK3 + WebKit2 login/browser window (`ui/auth_webview.py`)
    with a dedicated persistent profile directory, returning captured cookies,
    account email/name, and web session tokens.
    """
    helper_script = Path(__file__).resolve().parent.parent.parent / "ui" / "auth_webview.py"
    if not helper_script.is_file():
        raise UploadError("No se encontró el módulo de ventana de inicio de sesión (ui/auth_webview.py).")

    fd, out_path = tempfile.mkstemp(prefix=f"mint_auth_{provider}_", suffix=".json")
    os.close(fd)
    try:
        os.chmod(out_path, 0o600)
    except OSError:
        pass

    cmd = [
        sys.executable,
        str(helper_script),
        "--provider",
        provider,
        "--profile-dir",
        profile_dir,
        "--output-json",
        out_path,
    ]
    if custom_url:
        cmd.extend(["--url", custom_url])
    if browse_mode:
        cmd.append("--browse")
    if silent_refresh:
        cmd.append("--silent-refresh")
        cmd.extend(["--timeout", str(int(min(25, max(8, timeout_sec))))])

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout_sec)
        except asyncio.TimeoutError:
            proc.kill()
            raise UploadError("Se agotó el tiempo de espera en la ventana de inicio de sesión.")

        if not os.path.isfile(out_path) or os.path.getsize(out_path) == 0:
            err_txt = (stderr_b or b"").decode("utf-8", errors="replace").strip()
            raise UploadError(
                f"La ventana de inicio de sesión se cerró sin datos: {err_txt[:180] or 'cancelado'}"
            )

        with open(out_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        if data.get("cancelled") or not data.get("ok"):
            raise UploadCancelledError("Inicio de sesión cancelado por el usuario.")

        return data
    finally:
        try:
            if os.path.exists(out_path):
                os.unlink(out_path)
        except OSError:
            pass


class _WebListDaemonWorker:
    """Persistent warm WebKit2 headless worker for fast repeated subfolder browsing."""

    def __init__(self, provider: str, profile_dir: str) -> None:
        self.provider = provider
        self.profile_dir = profile_dir
        self.proc: Optional[asyncio.subprocess.Process] = None
        self.lock = asyncio.Lock()

    async def _ensure_started(self, helper_script: Path) -> asyncio.subprocess.Process:
        if self.proc and self.proc.returncode is None and self.proc.stdin and self.proc.stdout:
            return self.proc
        self.proc = await asyncio.create_subprocess_exec(
            sys.executable,
            str(helper_script),
            "--provider",
            self.provider,
            "--profile-dir",
            self.profile_dir,
            "--daemon",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            limit=4 * 1024 * 1024,
        )
        # Wait briefly for ready handshake line
        assert self.proc.stdout is not None
        ready_line = await asyncio.wait_for(self.proc.stdout.readline(), timeout=12.0)
        if not ready_line:
            await self.close()
            raise UploadError("No se pudo iniciar el servicio de lectura web.")
        return self.proc

    async def request_list(
        self,
        helper_script: Path,
        folder_id: str,
        folder_path: str,
        timeout_sec: float,
    ) -> Dict[str, Any]:
        async with self.lock:
            proc = await self._ensure_started(helper_script)
            assert proc.stdin is not None and proc.stdout is not None
            req_id = secrets.token_hex(6)
            req = {
                "cmd": "list",
                "req_id": req_id,
                "folder_id": folder_id,
                "folder_path": folder_path,
                "timeout": int(max(8, min(25, timeout_sec - 2))),
            }
            try:
                proc.stdin.write((json.dumps(req) + "\n").encode("utf-8"))
                await proc.stdin.drain()
                raw_line = await asyncio.wait_for(proc.stdout.readline(), timeout=timeout_sec)
                if not raw_line:
                    await self.close()
                    raise UploadError("El extractor web finalizó inesperadamente.")
                return json.loads(raw_line.decode("utf-8", errors="replace"))
            except Exception:
                await self.close()
                raise

    async def close(self) -> None:
        proc = self.proc
        self.proc = None
        if not proc:
            return
        try:
            if proc.returncode is None and proc.stdin:
                proc.stdin.write(b'{"cmd":"quit"}\n')
                await proc.stdin.drain()
                proc.stdin.close()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=1.5)
                except asyncio.TimeoutError:
                    proc.kill()
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


_WEB_DAEMON_POOL: Dict[tuple[str, str], _WebListDaemonWorker] = {}


async def shutdown_web_list_daemons() -> None:
    """Cleanly terminate all warm background WebKit2 listing workers."""
    workers = list(_WEB_DAEMON_POOL.values())
    _WEB_DAEMON_POOL.clear()
    for w in workers:
        await w.close()


async def run_web_list_folder(
    provider: str,
    profile_dir: str,
    folder_id: str = "",
    folder_path: str = "",
    timeout_sec: float = 18.0,
) -> Dict[str, Any]:
    """
    Silently load the cloud folder inside a warm persistent offscreen WebKit2 worker
    (reused across subfolder clicks, images disabled, auto-exits when idle) and return
    {entries, cookies, tokens, account_email, account_name, needs_login}.
    """
    helper_script = Path(__file__).resolve().parent.parent.parent / "ui" / "auth_webview.py"
    if not helper_script.is_file():
        raise UploadError("No se encontró el extractor web (ui/auth_webview.py).")

    key = (provider, profile_dir)
    worker = _WEB_DAEMON_POOL.get(key)
    if worker is None:
        # Cap warm workers to 2 max so RAM stays minimal
        if len(_WEB_DAEMON_POOL) >= 2:
            oldest_key = next(iter(_WEB_DAEMON_POOL))
            old_w = _WEB_DAEMON_POOL.pop(oldest_key)
            await old_w.close()
        worker = _WebListDaemonWorker(provider, profile_dir)
        _WEB_DAEMON_POOL[key] = worker

    data = await worker.request_list(helper_script, folder_id, folder_path, timeout_sec)
    if data.get("needs_login"):
        raise UploadAuthError("La sesión web expiró o cambió la contraseña. Re-abre la sesión.")
    return data


_DRIVE_IVD_RE = re.compile(r"window\._DRIVE_ivd\s*=\s*'((?:\\.|[^'\\])*)'")
_DRIVE_EMAIL_RE = re.compile(r'"oPEP7c"\s*:\s*"([^"@\s]+@[^"\s]+)"')
_JS_HEX_ESC_RE = re.compile(r"\\x([0-9a-fA-F]{2})")


def _unescape_drive_ivd(raw_js_str: str) -> str:
    s = _JS_HEX_ESC_RE.sub(lambda m: chr(int(m.group(1), 16)), raw_js_str)
    s = s.replace("\\/", "/").replace("\\'", "'")
    return s


async def _fast_gdrive_web_list_folder(
    session: aiohttp.ClientSession,
    creds: Dict[str, Any],
    folder_id: str = "",
    folder_path: str = "",
) -> Optional[tuple[List[CloudEntry], Dict[str, Any]]]:
    """
    Ultra-fast (~0.4s) direct HTTP folder listing for Google Drive web_session accounts
    by parsing Google Drive's server-embedded `window._DRIVE_ivd` payload using saved cookies,
    avoiding WebKit2 startup completely whenever possible.
    """
    cookie_hdr = _build_cookie_header(creds.get("cookies"), ("google.com", "drive.google.com"))
    if not cookie_hdr:
        return None
    fid = folder_id.strip()
    url = (
        f"https://drive.google.com/drive/folders/{urllib.parse.quote(fid)}"
        if fid and fid != "root"
        else "https://drive.google.com/drive/my-drive"
    )
    headers = {
        "Cookie": cookie_hdr,
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
    }
    try:
        async with session.get(
            url, headers=headers, allow_redirects=True, timeout=aiohttp.ClientTimeout(total=8)
        ) as resp:
            final_url = str(resp.url).lower()
            if "accounts.google.com" in final_url or resp.status >= 400:
                return None
            html_txt = await resp.text(errors="ignore")
    except Exception:
        return None

    updated = dict(creds)
    m_email = _DRIVE_EMAIL_RE.search(html_txt)
    if m_email:
        email = m_email.group(1).strip()
        updated["account_email"] = email
        if not updated.get("detected_name"):
            esc_em = re.escape(email)
            m_name = re.search(rf'"([A-Za-zÀ-ÿ][A-Za-zÀ-ÿ0-9 ._-]{{1,45}})"\s*,\s*"{esc_em}"', html_txt)
            if not m_name:
                m_name = re.search(rf'"{esc_em}"\s*,\s*"([A-Za-zÀ-ÿ][A-Za-zÀ-ÿ0-9 ._-]{{1,45}})"', html_txt)
            if m_name and "http" not in m_name.group(1) and "@" not in m_name.group(1):
                updated["detected_name"] = m_name.group(1).strip()

    m_ivd = _DRIVE_IVD_RE.search(html_txt)
    if not m_ivd:
        return None

    try:
        parsed = json.loads(_unescape_drive_ivd(m_ivd.group(1)))
    except Exception:
        return None

    raw_list = parsed[0] if isinstance(parsed, list) and parsed and isinstance(parsed[0], list) else []
    if not raw_list:
        return None

    base_p = folder_path.strip("/")
    entries: List[CloudEntry] = []
    seen: set[str] = set()
    for it in raw_list:
        if not isinstance(it, list) or len(it) < 4:
            continue
        item_id = it[0].strip() if isinstance(it[0], str) else ""
        name = it[2].strip() if isinstance(it[2], str) else ""
        mime = it[3].strip() if isinstance(it[3], str) else ""
        if not item_id or len(item_id) < 10 or not name or "/" not in mime or item_id in seen:
            continue
        seen.add(item_id)
        is_dir = mime == "application/vnd.google-apps.folder"
        size = 0
        if not is_dir and len(it) > 13 and isinstance(it[13], (int, float, str)):
            try:
                size = int(it[13])
            except (ValueError, TypeError):
                size = 0
        mod_str = ""
        if len(it) > 9 and isinstance(it[9], (int, float)) and it[9] > 1_000_000_000:
            ts = it[9] / 1000.0 if it[9] > 10_000_000_000 else float(it[9])
            try:
                mod_str = time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))
            except Exception:
                mod_str = ""
        entries.append(
            CloudEntry(
                id=item_id,
                name=name,
                is_folder=is_dir,
                size=0 if is_dir else size,
                modified=mod_str,
                path=f"{base_p}/{name}" if base_p else name,
                web_url=(
                    f"https://drive.google.com/drive/folders/{urllib.parse.quote(item_id)}"
                    if is_dir
                    else f"https://drive.google.com/file/d/{urllib.parse.quote(item_id)}/view"
                ),
                download_url=(
                    ""
                    if is_dir
                    else f"https://drive.google.com/uc?export=download&id={urllib.parse.quote(item_id)}"
                ),
                provider="gdrive",
            )
        )

    if not entries:
        return None
    entries.sort(key=lambda e: (not e.is_folder, e.name.lower()))
    return entries, updated


def _raw_web_entries_to_cloud(provider: str, raw_entries: Any) -> List[CloudEntry]:
    out: List[CloudEntry] = []
    if not isinstance(raw_entries, list):
        return out
    for it in raw_entries:
        if not isinstance(it, dict):
            continue
        name = str(it.get("name") or "").strip()
        if not name:
            continue
        is_dir = bool(it.get("is_folder"))
        out.append(
            CloudEntry(
                id=str(it.get("id") or name),
                name=name,
                is_folder=is_dir,
                size=0 if is_dir else int(it.get("size") or 0),
                modified=str(it.get("modified") or ""),
                path=str(it.get("path") or name),
                web_url=str(it.get("web_url") or ""),
                download_url=str(it.get("download_url") or ""),
                provider=provider,
            )
        )
    out.sort(key=lambda e: (not e.is_folder, e.name.lower()))
    return out


# ---------------------------------------------------------------------------
# Base Uploader Interface
# ---------------------------------------------------------------------------

class BaseCloudUploader:
    provider_key: str = ""

    async def authenticate(
        self,
        session: aiohttp.ClientSession,
        creds: Dict[str, Any],
    ) -> tuple[str, Dict[str, Any]]:
        """Verify credentials/tokens, refresh if needed, and return (account_email, updated_creds)."""
        raise NotImplementedError

    async def list_folder(
        self,
        session: aiohttp.ClientSession,
        creds: Dict[str, Any],
        folder_id: str = "",
        folder_path: str = "",
    ) -> tuple[List[CloudEntry], Dict[str, Any]]:
        """List remote folders and files inside folder_id/folder_path."""
        raise UploadError("Este proveedor aún no soporta listado nativo de carpetas.")

    async def create_folder(
        self,
        session: aiohttp.ClientSession,
        creds: Dict[str, Any],
        parent_id: str = "",
        parent_path: str = "",
        name: str = "Nueva carpeta",
    ) -> tuple[CloudEntry, Dict[str, Any]]:
        """Create a new remote folder inside parent_id/parent_path."""
        raise UploadError("Este proveedor aún no soporta crear carpetas desde el explorador nativo.")

    async def get_download_request(
        self,
        session: aiohttp.ClientSession,
        creds: Dict[str, Any],
        entry: CloudEntry,
    ) -> tuple[str, Dict[str, str], Dict[str, Any]]:
        """Resolve (download_url, headers, updated_creds) for downloading a remote CloudEntry."""
        if entry.download_url:
            return entry.download_url, {}, dict(creds)
        if entry.web_url:
            return entry.web_url, {}, dict(creds)
        raise UploadError("No se pudo obtener un enlace de descarga para este archivo.")

    async def upload_file(
        self,
        session: aiohttp.ClientSession,
        creds: Dict[str, Any],
        local_path: str,
        filename: str,
        remote_folder: str,
        extra: Dict[str, Any],
        progress_cb: Callable[[UploadProgress], None],
        cancel_event: asyncio.Event,
        get_speed_limit_kbps: Callable[[], int],
    ) -> tuple[str, Dict[str, Any]]:
        """
        Upload local_path to the cloud account.
        Returns (share_url, updated_creds).
        """
        raise NotImplementedError


_GDRIVE_API_KEY_RE = re.compile(r'"(?:QTs5if|FdrFJe|apiKey)"\s*:\s*"(AIzaSy[0-9A-Za-z_-]{33})"')
_GDRIVE_ANY_API_KEY_RE = re.compile(r'"(AIzaSy[0-9A-Za-z_-]{33})"')
_GDRIVE_WEB_ORIGIN = "https://drive.google.com"
_GDRIVE_WEB_UA = "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"


def _build_gdrive_sapisid_headers(creds: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """Build Google Drive web API `SAPISIDHASH` + Cookie headers from saved web_session cookies."""
    cookies = creds.get("cookies")
    if not isinstance(cookies, list) or not cookies:
        return None
    cookie_map: Dict[str, str] = {}
    for c in cookies:
        if not isinstance(c, dict):
            continue
        dom = str(c.get("domain") or "").lower().lstrip(".")
        if not (dom == "google.com" or dom.endswith(".google.com")):
            continue
        name = str(c.get("name") or "").strip()
        val = str(c.get("value") or "")
        if name and val and name not in cookie_map:
            cookie_map[name] = val

    sapisid = cookie_map.get("SAPISID") or cookie_map.get("__Secure-3PAPISID") or cookie_map.get("APISID")
    if not sapisid:
        return None

    ts = int(time.time())
    parts: List[str] = []
    h_main = hashlib.sha1(f"{ts} {sapisid} {_GDRIVE_WEB_ORIGIN}".encode("utf-8")).hexdigest()
    parts.append(f"SAPISIDHASH {ts}_{h_main}")
    p1 = cookie_map.get("__Secure-1PAPISID")
    if p1:
        h1 = hashlib.sha1(f"{ts} {p1} {_GDRIVE_WEB_ORIGIN}".encode("utf-8")).hexdigest()
        parts.append(f"SAPISID1PHASH {ts}_{h1}")
    p3 = cookie_map.get("__Secure-3PAPISID")
    if p3:
        h3 = hashlib.sha1(f"{ts} {p3} {_GDRIVE_WEB_ORIGIN}".encode("utf-8")).hexdigest()
        parts.append(f"SAPISID3PHASH {ts}_{h3}")

    cookie_hdr = _build_cookie_header(cookies, ("google.com", "drive.google.com"))
    if not cookie_hdr:
        return None
    return {
        "Authorization": " ".join(parts),
        "Cookie": cookie_hdr,
        "Origin": _GDRIVE_WEB_ORIGIN,
        "Referer": f"{_GDRIVE_WEB_ORIGIN}/",
        "X-Goog-AuthUser": "0",
        "User-Agent": _GDRIVE_WEB_UA,
    }


def _extract_gdrive_web_api_key_from_html(html_txt: str) -> str:
    m = _GDRIVE_API_KEY_RE.search(html_txt)
    if m:
        return m.group(1)
    m2 = _GDRIVE_ANY_API_KEY_RE.search(html_txt)
    return m2.group(1) if m2 else ""


# ---------------------------------------------------------------------------
# 1. Google Drive (GDrive) — REST API v3 & Web SAPISIDHASH v2internal
# ---------------------------------------------------------------------------

class GDriveUploader(BaseCloudUploader):
    provider_key = "gdrive"
    CHUNK_SIZE = 4 * 1024 * 1024  # 4 MiB (multiple of 256 KiB required by Google)

    async def _ensure_token(self, session: aiohttp.ClientSession, creds: Dict[str, Any]) -> Dict[str, Any]:
        updated = dict(creds)
        access_token = str(updated.get("access_token") or "")
        refresh_token = str(updated.get("refresh_token") or "")
        expires_at = float(updated.get("expires_at") or 0.0)

        if access_token and (not refresh_token or time.time() < expires_at - 60):
            return updated

        if not refresh_token:
            if access_token:
                return updated
            if updated.get("auth_type") == "web_session":
                raise UploadError(
                    "Sesión web de GDrive activa (guardada para Downloader). "
                    "Para subir archivos directo por Google Drive API, vincula también OAuth2 en «Agregar cuenta → Opciones avanzadas»."
                )
            raise UploadAuthError("Sesión de GDrive expirada. Vuelve a sincronizar la cuenta.")

        client_id = str(updated.get("client_id") or "")
        client_secret = str(updated.get("client_secret") or "")
        if not client_id:
            if access_token:
                return updated
            raise UploadAuthError("Falta el Client ID para renovar el token de GDrive.")

        data = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
        }
        if client_secret:
            data["client_secret"] = client_secret

        async with session.post(OAUTH_CONFIGS["gdrive"]["token_url"], data=data) as resp:
            body = await resp.json(content_type=None)
            if resp.status >= 400 or "error" in body:
                err_code = str(body.get("error") or "")
                if err_code in ("invalid_grant", "unauthorized_client"):
                    raise UploadAuthError(
                        "La sesión de GDrive fue revocada o cambió la contraseña. Re-sincroniza la cuenta."
                    )
                raise UploadError(f"Error al renovar token de GDrive: {body.get('error_description') or err_code}")

        updated["access_token"] = str(body["access_token"])
        updated["expires_at"] = time.time() + max(60, int(body.get("expires_in") or 3600) - 60)
        if body.get("refresh_token"):
            updated["refresh_token"] = str(body["refresh_token"])
        return updated

    async def authenticate(
        self,
        session: aiohttp.ClientSession,
        creds: Dict[str, Any],
    ) -> tuple[str, Dict[str, Any]]:
        if creds.get("auth_type") == "web_session" and not creds.get("access_token") and not creds.get("refresh_token"):
            cookie_hdr = _build_cookie_header(creds.get("cookies"), ("google.com", "drive.google.com"))
            if not cookie_hdr:
                raise UploadAuthError("La sesión web de GDrive no tiene cookies activas. Re-abre el inicio de sesión.")
            async with session.get(
                "https://drive.google.com/drive/my-drive",
                headers={"Cookie": cookie_hdr, "User-Agent": DEFAULT_UA},
                allow_redirects=True,
            ) as resp:
                final_url = str(resp.url)
                if "accounts.google.com" in final_url or resp.status >= 400:
                    raise UploadAuthError("La sesión web de GDrive expiró o cambió la contraseña. Re-sincroniza.")
            email = str(creds.get("account_email") or creds.get("email") or "Sesión web GDrive")
            return email, dict(creds)

        updated = await self._ensure_token(session, creds)
        headers = {"Authorization": f"Bearer {updated['access_token']}"}
        async with session.get(
            "https://www.googleapis.com/drive/v3/about?fields=user(displayName,emailAddress)",
            headers=headers,
        ) as resp:
            if resp.status == 401:
                updated["expires_at"] = 0
                updated = await self._ensure_token(session, updated)
                headers = {"Authorization": f"Bearer {updated['access_token']}"}
                async with session.get(
                    "https://www.googleapis.com/drive/v3/about?fields=user(displayName,emailAddress)",
                    headers=headers,
                ) as resp2:
                    if resp2.status >= 400:
                        raise UploadAuthError("Token de GDrive inválido o expirado.")
                    data = await resp2.json(content_type=None)
            elif resp.status >= 400:
                raise UploadAuthError(f"Error al verificar cuenta GDrive (HTTP {resp.status}).")
            else:
                data = await resp.json(content_type=None)
        user = data.get("user") or {}
        email = str(user.get("emailAddress") or user.get("displayName") or "Cuenta GDrive")
        return email, updated

    async def _ensure_folder(
        self,
        session: aiohttp.ClientSession,
        headers: Dict[str, str],
        remote_folder: str,
    ) -> Optional[str]:
        parts = [p.strip() for p in remote_folder.strip("/\\").split("/") if p.strip()]
        if not parts:
            return None
        parent_id = "root"
        for name in parts:
            escaped = name.replace("'", "\\'")
            q = (
                f"name='{escaped}' and mimeType='application/vnd.google-apps.folder' "
                f"and '{parent_id}' in parents and trashed=false"
            )
            params = {"q": q, "fields": "files(id,name)", "pageSize": "1"}
            async with session.get("https://www.googleapis.com/drive/v3/files", headers=headers, params=params) as r:
                if r.status == 200:
                    files = (await r.json(content_type=None)).get("files") or []
                    if files:
                        parent_id = files[0]["id"]
                        continue
            meta = {
                "name": name,
                "mimeType": "application/vnd.google-apps.folder",
                "parents": [parent_id],
            }
            async with session.post(
                "https://www.googleapis.com/drive/v3/files?fields=id",
                headers={**headers, "Content-Type": "application/json"},
                json=meta,
            ) as cr:
                if cr.status >= 400:
                    return None
                parent_id = (await cr.json(content_type=None)).get("id") or parent_id
        return parent_id

    async def _ensure_web_api_key(
        self,
        session: aiohttp.ClientSession,
        creds: Dict[str, Any],
    ) -> tuple[str, Dict[str, Any]]:
        updated = dict(creds)
        key = str(updated.get("drive_web_api_key") or "").strip()
        if key:
            return key, updated
        cookie_hdr = _build_cookie_header(updated.get("cookies"), ("google.com", "drive.google.com"))
        if not cookie_hdr:
            return "", updated
        try:
            async with session.get(
                "https://drive.google.com/drive/my-drive",
                headers={
                    "Cookie": cookie_hdr,
                    "User-Agent": _GDRIVE_WEB_UA,
                    "Accept": "text/html,application/xhtml+xml",
                },
                allow_redirects=True,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if "accounts.google.com" in str(resp.url).lower() or resp.status >= 400:
                    return "", updated
                html_txt = await resp.text(errors="ignore")
            key = _extract_gdrive_web_api_key_from_html(html_txt)
            if key:
                updated["drive_web_api_key"] = key
            m_em = _DRIVE_EMAIL_RE.search(html_txt)
            if m_em and not updated.get("account_email"):
                updated["account_email"] = m_em.group(1).strip()
        except Exception:
            pass
        return key, updated

    async def _ensure_folder_web(
        self,
        session: aiohttp.ClientSession,
        headers: Dict[str, str],
        api_key: str,
        remote_folder: str,
    ) -> str:
        parts = [p.strip() for p in remote_folder.strip("/\\").split("/") if p.strip()]
        if not parts:
            return "root"
        parent_id = "root"
        for name in parts:
            escaped = name.replace("'", "\\'")
            q = (
                f"title='{escaped}' and mimeType='application/vnd.google-apps.folder' "
                f"and '{parent_id}' in parents and trashed=false"
            )
            params = {
                "q": q,
                "fields": "items(id,title)",
                "maxResults": "1",
                "supportsAllDrives": "true",
                "includeItemsFromAllDrives": "true",
                "key": api_key,
            }
            async with session.get(
                "https://clients6.google.com/drive/v2internal/files",
                headers=headers,
                params=params,
            ) as r:
                if r.status == 200:
                    items = (await r.json(content_type=None)).get("items") or []
                    if items:
                        parent_id = str(items[0].get("id") or parent_id)
                        continue
            meta = {
                "title": name,
                "mimeType": "application/vnd.google-apps.folder",
                "parents": [{"id": parent_id}],
            }
            async with session.post(
                f"https://clients6.google.com/drive/v2internal/files?supportsAllDrives=true&key={urllib.parse.quote(api_key)}",
                headers={**headers, "Content-Type": "application/json"},
                json=meta,
            ) as cr:
                if cr.status >= 400:
                    return parent_id
                parent_id = str((await cr.json(content_type=None)).get("id") or parent_id)
        return parent_id

    async def _upload_file_web_sapisid(
        self,
        session: aiohttp.ClientSession,
        creds: Dict[str, Any],
        local_path: str,
        filename: str,
        remote_folder: str,
        extra: Dict[str, Any],
        progress_cb: Callable[[UploadProgress], None],
        cancel_event: asyncio.Event,
        get_speed_limit_kbps: Callable[[], int],
    ) -> tuple[str, Dict[str, Any]]:
        api_key, updated = await self._ensure_web_api_key(session, creds)
        headers = _build_gdrive_sapisid_headers(updated)
        if not headers or not api_key:
            raise UploadAuthError(
                "No se pudo autenticar la subida web de GDrive (falta cookie SAPISID o API key). "
                "Re-abre la sesión de GDrive en «Cuentas → Abrir sesión»."
            )

        total_size = os.path.getsize(local_path)
        mime_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        session_uri = str(extra.get("upload_session_uri") or "")
        offset = 0

        if session_uri and total_size > 0:
            try:
                probe_hdrs = {
                    **headers,
                    "Content-Length": "0",
                    "Content-Range": f"bytes */{total_size}",
                }
                async with session.put(session_uri, headers=probe_hdrs) as probe:
                    if probe.status == 308:
                        rng = probe.headers.get("Range", "")
                        if "-" in rng:
                            offset = int(rng.split("-")[-1]) + 1
                    elif probe.status in (200, 201):
                        res = await probe.json(content_type=None)
                        fid = str(res.get("id") or "")
                        return str(res.get("alternateLink") or (f"https://drive.google.com/file/d/{fid}/view" if fid else "")), updated
                    else:
                        session_uri = ""
            except Exception:
                session_uri = ""

        if not session_uri:
            target_parent = str(extra.get("remote_folder_id") or "").strip()
            if not target_parent:
                if remote_folder.strip():
                    target_parent = await self._ensure_folder_web(session, headers, api_key, remote_folder)
                else:
                    target_parent = "root"
            metadata: Dict[str, Any] = {
                "title": filename,
                "parents": [{"id": target_parent}],
            }
            init_headers = {
                **headers,
                "Content-Type": "application/json; charset=UTF-8",
                "X-Upload-Content-Type": mime_type,
                "X-Upload-Content-Length": str(total_size),
            }
            init_url = (
                "https://clients6.google.com/upload/drive/v2internal/files"
                f"?uploadType=resumable&supportsAllDrives=true&key={urllib.parse.quote(api_key)}"
            )
            async with session.post(init_url, headers=init_headers, json=metadata) as init_resp:
                if init_resp.status in (401, 403):
                    err_t = await init_resp.text()
                    if init_resp.status == 401:
                        raise UploadAuthError("Sesión web de GDrive expirada. Re-abre la sesión en «Cuentas».")
                    raise UploadError(f"GDrive rechazó el permiso en esta carpeta (HTTP 403): {err_t[:140]}")
                if init_resp.status >= 400:
                    err_t = await init_resp.text()
                    raise UploadError(f"GDrive rechazó la subida (HTTP {init_resp.status}): {err_t[:140]}")
                session_uri = init_resp.headers.get("Location", "")
                if not session_uri:
                    raise UploadError("GDrive no devolvió URL de sesión reanudable.")
                extra["upload_session_uri"] = session_uri

        if total_size == 0:
            async with session.put(session_uri, data=b"", headers={**headers, "Content-Length": "0"}) as r0:
                res = await r0.json(content_type=None)
                fid = str(res.get("id") or "")
                return str(res.get("alternateLink") or (f"https://drive.google.com/file/d/{fid}/view" if fid else "")), updated

        start_mono = time.monotonic()
        bytes_this_run = 0
        file_id = ""
        web_link = ""

        with open(local_path, "rb") as f:
            f.seek(offset)
            while offset < total_size:
                if cancel_event.is_set():
                    raise UploadCancelledError("Subida pausada o detenida.")
                chunk = f.read(self.CHUNK_SIZE)
                if not chunk:
                    break
                end_byte = offset + len(chunk) - 1
                chunk_t0 = time.monotonic()
                put_headers = {
                    **headers,
                    "Content-Length": str(len(chunk)),
                    "Content-Range": f"bytes {offset}-{end_byte}/{total_size}",
                    "Content-Type": mime_type,
                }
                async with session.put(session_uri, data=chunk, headers=put_headers) as put_resp:
                    if put_resp.status in (200, 201):
                        res = await put_resp.json(content_type=None)
                        file_id = str(res.get("id") or "")
                        web_link = str(res.get("alternateLink") or res.get("webViewLink") or "")
                        offset += len(chunk)
                        bytes_this_run += len(chunk)
                    elif put_resp.status == 308:
                        offset += len(chunk)
                        bytes_this_run += len(chunk)
                    elif put_resp.status == 401:
                        raise UploadAuthError("Sesión web de GDrive expirada durante la subida.")
                    else:
                        txt = await put_resp.text()
                        raise UploadError(f"Error al subir bloque a GDrive (HTTP {put_resp.status}): {txt[:140]}")

                chunk_elapsed = time.monotonic() - chunk_t0
                await _throttle_speed(len(chunk), chunk_elapsed, get_speed_limit_kbps())
                total_elapsed = max(0.05, time.monotonic() - start_mono)
                speed = bytes_this_run / total_elapsed
                eta = int((total_size - offset) / speed) if speed > 0 else 0
                progress_cb(UploadProgress(offset, total_size, speed, eta, "uploading"))

        extra.pop("upload_session_uri", None)
        if not web_link and file_id:
            web_link = f"https://drive.google.com/file/d/{file_id}/view"
        return web_link, updated

    async def upload_file(
        self,
        session: aiohttp.ClientSession,
        creds: Dict[str, Any],
        local_path: str,
        filename: str,
        remote_folder: str,
        extra: Dict[str, Any],
        progress_cb: Callable[[UploadProgress], None],
        cancel_event: asyncio.Event,
        get_speed_limit_kbps: Callable[[], int],
    ) -> tuple[str, Dict[str, Any]]:
        if creds.get("auth_type") == "web_session" and not creds.get("access_token") and not creds.get("refresh_token"):
            return await self._upload_file_web_sapisid(
                session=session,
                creds=creds,
                local_path=local_path,
                filename=filename,
                remote_folder=remote_folder,
                extra=extra,
                progress_cb=progress_cb,
                cancel_event=cancel_event,
                get_speed_limit_kbps=get_speed_limit_kbps,
            )

        updated = await self._ensure_token(session, creds)
        total_size = os.path.getsize(local_path)
        mime_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        headers = {"Authorization": f"Bearer {updated['access_token']}"}

        session_uri = str(extra.get("upload_session_uri") or "")
        offset = 0

        # Attempt to resume an existing Google Drive upload session
        if session_uri and total_size > 0:
            try:
                async with session.put(
                    session_uri,
                    headers={"Content-Length": "0", "Content-Range": f"bytes */{total_size}"},
                ) as probe:
                    if probe.status == 308:
                        rng = probe.headers.get("Range", "")
                        if "-" in rng:
                            offset = int(rng.split("-")[-1]) + 1
                    elif probe.status in (200, 201):
                        res = await probe.json(content_type=None)
                        fid = res.get("id", "")
                        return res.get("webViewLink") or (f"https://drive.google.com/file/d/{fid}/view" if fid else ""), updated
                    else:
                        session_uri = ""
            except Exception:
                session_uri = ""

        if not session_uri:
            metadata: Dict[str, Any] = {"name": filename}
            direct_pid = str(extra.get("remote_folder_id") or "").strip()
            if direct_pid:
                metadata["parents"] = [direct_pid]
            elif remote_folder.strip():
                folder_id = await self._ensure_folder(session, headers, remote_folder)
                if folder_id:
                    metadata["parents"] = [folder_id]
            init_headers = {
                **headers,
                "Content-Type": "application/json; charset=UTF-8",
                "X-Upload-Content-Type": mime_type,
                "X-Upload-Content-Length": str(total_size),
            }
            async with session.post(
                "https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable&fields=id,name,webViewLink",
                headers=init_headers,
                json=metadata,
            ) as init_resp:
                if init_resp.status == 401:
                    raise UploadAuthError("Sesión de GDrive expirada. Vuelve a sincronizar la cuenta.")
                if init_resp.status >= 400:
                    err_t = await init_resp.text()
                    raise UploadError(f"GDrive rechazó la subida (HTTP {init_resp.status}): {err_t[:120]}")
                session_uri = init_resp.headers.get("Location", "")
                if not session_uri:
                    raise UploadError("GDrive no devolvió URL de sesión reanudable.")
                extra["upload_session_uri"] = session_uri

        if total_size == 0:
            async with session.put(session_uri, data=b"", headers={"Content-Length": "0"}) as r0:
                res = await r0.json(content_type=None)
                fid = res.get("id", "")
                return res.get("webViewLink") or (f"https://drive.google.com/file/d/{fid}/view" if fid else ""), updated

        start_mono = time.monotonic()
        bytes_this_run = 0
        file_id = ""
        web_link = ""

        with open(local_path, "rb") as f:
            f.seek(offset)
            while offset < total_size:
                if cancel_event.is_set():
                    raise UploadCancelledError("Subida pausada o detenida.")
                chunk = f.read(self.CHUNK_SIZE)
                if not chunk:
                    break
                end_byte = offset + len(chunk) - 1
                chunk_t0 = time.monotonic()
                put_headers = {
                    "Content-Length": str(len(chunk)),
                    "Content-Range": f"bytes {offset}-{end_byte}/{total_size}",
                    "Content-Type": mime_type,
                }
                async with session.put(session_uri, data=chunk, headers=put_headers) as put_resp:
                    if put_resp.status in (200, 201):
                        res = await put_resp.json(content_type=None)
                        file_id = str(res.get("id") or "")
                        web_link = str(res.get("webViewLink") or "")
                        offset += len(chunk)
                        bytes_this_run += len(chunk)
                    elif put_resp.status == 308:
                        offset += len(chunk)
                        bytes_this_run += len(chunk)
                    elif put_resp.status == 401:
                        raise UploadAuthError("Token de GDrive expirado durante la subida.")
                    else:
                        txt = await put_resp.text()
                        raise UploadError(f"Error al subir bloque a GDrive (HTTP {put_resp.status}): {txt[:120]}")

                chunk_elapsed = time.monotonic() - chunk_t0
                await _throttle_speed(len(chunk), chunk_elapsed, get_speed_limit_kbps())
                total_elapsed = max(0.05, time.monotonic() - start_mono)
                speed = bytes_this_run / total_elapsed
                eta = int((total_size - offset) / speed) if speed > 0 else 0
                progress_cb(UploadProgress(offset, total_size, speed, eta, "uploading"))

        extra.pop("upload_session_uri", None)
        if not web_link and file_id:
            web_link = f"https://drive.google.com/file/d/{file_id}/view"
        return web_link, updated

    async def list_folder(
        self,
        session: aiohttp.ClientSession,
        creds: Dict[str, Any],
        folder_id: str = "",
        folder_path: str = "",
    ) -> tuple[List[CloudEntry], Dict[str, Any]]:
        if creds.get("auth_type") == "web_session" and not creds.get("access_token") and not creds.get("refresh_token"):
            # Priority 1 (~150-300 ms): Direct Google Drive Web API (clients6 + SAPISIDHASH)
            api_key, updated = await self._ensure_web_api_key(session, creds)
            sapisid_hdrs = _build_gdrive_sapisid_headers(updated)
            if api_key and sapisid_hdrs:
                target_id = folder_id.strip() or "root"
                params = {
                    "q": f"'{target_id}' in parents and trashed = false",
                    "fields": "items(id,title,mimeType,fileSize,modifiedDate,alternateLink,downloadUrl,webContentLink)",
                    "orderBy": "folder,title_natural",
                    "maxResults": "200",
                    "supportsAllDrives": "true",
                    "includeItemsFromAllDrives": "true",
                    "key": api_key,
                }
                try:
                    async with session.get(
                        "https://clients6.google.com/drive/v2internal/files",
                        headers=sapisid_hdrs,
                        params=params,
                        timeout=aiohttp.ClientTimeout(total=10),
                    ) as resp:
                        if resp.status == 200:
                            data = await resp.json(content_type=None)
                            base_p = folder_path.strip("/")
                            entries: List[CloudEntry] = []
                            for f in data.get("items") or []:
                                is_dir = f.get("mimeType") == "application/vnd.google-apps.folder"
                                name = str(f.get("title") or "").strip()
                                fid = str(f.get("id") or "").strip()
                                if not fid or not name:
                                    continue
                                mod = str(f.get("modifiedDate") or "")[:16].replace("T", " ")
                                entries.append(
                                    CloudEntry(
                                        id=fid,
                                        name=name,
                                        is_folder=is_dir,
                                        size=0 if is_dir else int(f.get("fileSize") or 0),
                                        modified=mod,
                                        path=f"{base_p}/{name}" if base_p else name,
                                        web_url=str(
                                            f.get("alternateLink")
                                            or (
                                                f"https://drive.google.com/drive/folders/{fid}"
                                                if is_dir
                                                else f"https://drive.google.com/file/d/{fid}/view"
                                            )
                                        ),
                                        download_url="" if is_dir else str(f.get("downloadUrl") or ""),
                                        provider="gdrive",
                                    )
                                )
                            entries.sort(key=lambda e: (not e.is_folder, e.name.lower()))
                            return entries, updated
                except Exception:
                    pass

            # Priority 2 (~400 ms): Direct HTML _DRIVE_ivd parser
            fast = await _fast_gdrive_web_list_folder(session, updated, folder_id=folder_id, folder_path=folder_path)
            if fast is not None:
                return fast

            # Priority 3: Warm persistent offscreen WebKit2 worker
            prof = str(updated.get("web_profile_dir") or "")
            if not prof or not os.path.isdir(prof):
                raise UploadAuthError(f"Falta el perfil web de {self.provider_key}. Re-abre la sesión.")
            res = await run_web_list_folder(self.provider_key, prof, folder_id=folder_id, folder_path=folder_path)
            if isinstance(res.get("cookies"), list) and res["cookies"]:
                updated["cookies"] = res["cookies"]
            if res.get("account_email"):
                updated["account_email"] = str(res["account_email"])
            if res.get("account_name"):
                updated["detected_name"] = str(res["account_name"])
            toks = res.get("tokens") if isinstance(res.get("tokens"), dict) else {}
            for tk, tv in toks.items():
                if tv:
                    updated[tk] = tv
            entries = _raw_web_entries_to_cloud(self.provider_key, res.get("entries"))
            return entries, updated

        updated = await self._ensure_token(session, creds)
        headers = {"Authorization": f"Bearer {updated['access_token']}"}
        target_id = folder_id.strip() or "root"
        params = {
            "q": f"'{target_id}' in parents and trashed=false",
            "fields": "files(id,name,mimeType,size,modifiedTime,webViewLink)",
            "orderBy": "folder,name",
            "pageSize": "200",
        }
        async with session.get("https://www.googleapis.com/drive/v3/files", headers=headers, params=params) as resp:
            if resp.status == 401:
                updated["expires_at"] = 0
                updated = await self._ensure_token(session, updated)
                headers = {"Authorization": f"Bearer {updated['access_token']}"}
                async with session.get("https://www.googleapis.com/drive/v3/files", headers=headers, params=params) as r2:
                    if r2.status >= 400:
                        raise UploadAuthError("Sesión de GDrive expirada al listar archivos.")
                    data = await r2.json(content_type=None)
            elif resp.status >= 400:
                txt = await resp.text()
                raise UploadError(f"Error al listar carpeta de GDrive (HTTP {resp.status}): {txt[:120]}")
            else:
                data = await resp.json(content_type=None)

        entries: List[CloudEntry] = []
        base_p = folder_path.strip("/")
        for f in data.get("files") or []:
            is_dir = f.get("mimeType") == "application/vnd.google-apps.folder"
            name = str(f.get("name") or "")
            fid = str(f.get("id") or "")
            child_path = f"{base_p}/{name}" if base_p else name
            mod = str(f.get("modifiedTime") or "")[:16].replace("T", " ")
            entries.append(
                CloudEntry(
                    id=fid,
                    name=name,
                    is_folder=is_dir,
                    size=0 if is_dir else int(f.get("size") or 0),
                    modified=mod,
                    path=child_path,
                    web_url=str(f.get("webViewLink") or f"https://drive.google.com/file/d/{fid}/view"),
                    provider="gdrive",
                )
            )
        entries.sort(key=lambda e: (not e.is_folder, e.name.lower()))
        return entries, updated

    async def create_folder(
        self,
        session: aiohttp.ClientSession,
        creds: Dict[str, Any],
        parent_id: str = "",
        parent_path: str = "",
        name: str = "Nueva carpeta",
    ) -> tuple[CloudEntry, Dict[str, Any]]:
        if creds.get("auth_type") == "web_session" and not creds.get("access_token") and not creds.get("refresh_token"):
            api_key, updated = await self._ensure_web_api_key(session, creds)
            sapisid_hdrs = _build_gdrive_sapisid_headers(updated)
            if not api_key or not sapisid_hdrs:
                raise UploadAuthError("Sesión web de GDrive expirada. Re-abre la sesión en «Cuentas».")
            pid = parent_id.strip() or "root"
            clean_name = name.strip() or "Nueva carpeta"
            meta = {
                "title": clean_name,
                "mimeType": "application/vnd.google-apps.folder",
                "parents": [{"id": pid}],
            }
            async with session.post(
                f"https://clients6.google.com/drive/v2internal/files?supportsAllDrives=true&key={urllib.parse.quote(api_key)}",
                headers={**sapisid_hdrs, "Content-Type": "application/json"},
                json=meta,
            ) as resp:
                if resp.status >= 400:
                    txt = await resp.text()
                    raise UploadError(f"No se pudo crear carpeta en GDrive (HTTP {resp.status}): {txt[:120]}")
                d = await resp.json(content_type=None)
            base_p = parent_path.strip("/")
            cname = str(d.get("title") or clean_name)
            fid = str(d.get("id") or "")
            return (
                CloudEntry(
                    id=fid,
                    name=cname,
                    is_folder=True,
                    path=f"{base_p}/{cname}" if base_p else cname,
                    web_url=str(d.get("alternateLink") or f"https://drive.google.com/drive/folders/{fid}"),
                    provider="gdrive",
                ),
                updated,
            )

        updated = await self._ensure_token(session, creds)
        headers = {"Authorization": f"Bearer {updated['access_token']}", "Content-Type": "application/json"}
        pid = parent_id.strip() or "root"
        meta = {
            "name": name.strip() or "Nueva carpeta",
            "mimeType": "application/vnd.google-apps.folder",
            "parents": [pid],
        }
        async with session.post(
            "https://www.googleapis.com/drive/v3/files?fields=id,name,webViewLink",
            headers=headers,
            json=meta,
        ) as resp:
            if resp.status >= 400:
                txt = await resp.text()
                raise UploadError(f"No se pudo crear carpeta en GDrive: {txt[:120]}")
            d = await resp.json(content_type=None)
        base_p = parent_path.strip("/")
        cname = str(d.get("name") or name)
        return (
            CloudEntry(
                id=str(d.get("id") or ""),
                name=cname,
                is_folder=True,
                path=f"{base_p}/{cname}" if base_p else cname,
                web_url=str(d.get("webViewLink") or ""),
                provider="gdrive",
            ),
            updated,
        )

    async def get_download_request(
        self,
        session: aiohttp.ClientSession,
        creds: Dict[str, Any],
        entry: CloudEntry,
    ) -> tuple[str, Dict[str, str], Dict[str, Any]]:
        if creds.get("auth_type") == "web_session" and not creds.get("access_token") and not creds.get("refresh_token"):
            api_key, updated = await self._ensure_web_api_key(session, creds)
            sapisid_hdrs = _build_gdrive_sapisid_headers(updated)
            if api_key and sapisid_hdrs:
                url = (
                    f"https://clients6.google.com/drive/v2internal/files/{urllib.parse.quote(entry.id)}"
                    f"?alt=media&supportsAllDrives=true&key={urllib.parse.quote(api_key)}"
                )
                return url, sapisid_hdrs, updated
            cookie_hdr = _build_cookie_header(updated.get("cookies"), ("google.com", "googleusercontent.com", "drive.google.com"))
            url = entry.download_url or f"https://drive.google.com/uc?export=download&id={entry.id}"
            hdrs = {"User-Agent": _GDRIVE_WEB_UA}
            if cookie_hdr:
                hdrs["Cookie"] = cookie_hdr
            return url, hdrs, updated
        updated = await self._ensure_token(session, creds)
        url = f"https://www.googleapis.com/drive/v3/files/{entry.id}?alt=media"
        headers = {"Authorization": f"Bearer {updated['access_token']}"}
        return url, headers, updated


# ---------------------------------------------------------------------------
# 2. MEGA — Native MEGA API v2 (PBKDF2/v1+v2 Login + Encrypted AES-CTR Upload)
# ---------------------------------------------------------------------------

def _aes_ecb_encrypt(data: bytes, key: bytes) -> bytes:
    if _HAS_CRYPTODOME:
        return AES.new(key, AES.MODE_ECB).encrypt(data)
    c = Cipher(algorithms.AES(key), modes.ECB(), backend=default_backend()).encryptor()
    return c.update(data) + c.finalize()


def _aes_ecb_decrypt(data: bytes, key: bytes) -> bytes:
    if _HAS_CRYPTODOME:
        return AES.new(key, AES.MODE_ECB).decrypt(data)
    c = Cipher(algorithms.AES(key), modes.ECB(), backend=default_backend()).decryptor()
    return c.update(data) + c.finalize()


def _aes_cbc_encrypt(data: bytes, key: bytes, iv: bytes = b"\x00" * 16) -> bytes:
    if len(data) % 16 != 0:
        data += b"\x00" * (16 - (len(data) % 16))
    if _HAS_CRYPTODOME:
        return AES.new(key, AES.MODE_CBC, iv=iv).encrypt(data)
    c = Cipher(algorithms.AES(key), modes.CBC(iv), backend=default_backend()).encryptor()
    return c.update(data) + c.finalize()


def _mega_a32_to_bytes(a: tuple[int, ...] | list[int]) -> bytes:
    return struct.pack(f">{len(a)}I", *a)


def _mega_bytes_to_a32(b: bytes) -> tuple[int, ...]:
    if len(b) % 4 != 0:
        b += b"\x00" * (4 - (len(b) % 4))
    return struct.unpack(f">{len(b) // 4}I", b)


def _mega_prepare_key_v1(password_bytes: bytes) -> bytes:
    a32 = _mega_bytes_to_a32(password_bytes)
    pkey = [0x93C467E3, 0x7DB0C7A4, 0xD1BE3F81, 0x0152CB56]
    for _ in range(0x10000):
        for j in range(0, len(a32), 4):
            key = [a32[j + i] if j + i < len(a32) else 0 for i in range(4)]
            pkey = list(_mega_bytes_to_a32(_aes_ecb_encrypt(_mega_a32_to_bytes(pkey), _mega_a32_to_bytes(key))))
    return _mega_a32_to_bytes(pkey)


def _mega_stringhash_v1(email_lower: str, aes_key: bytes) -> str:
    s32 = _mega_bytes_to_a32(email_lower.encode("utf-8"))
    h32 = [0, 0, 0, 0]
    for i, val in enumerate(s32):
        h32[i & 3] ^= val
    h_bytes = _mega_a32_to_bytes(h32)
    for _ in range(0x4000):
        h_bytes = _aes_ecb_encrypt(h_bytes, aes_key)
    h_a32 = _mega_bytes_to_a32(h_bytes)
    return _b64url_encode(_mega_a32_to_bytes((h_a32[0], h_a32[2])))


def _mega_mpi_to_int(raw: bytes, offset: int) -> tuple[int, int]:
    bit_len = struct.unpack(">H", raw[offset : offset + 2])[0]
    byte_len = (bit_len + 7) // 8
    val = int.from_bytes(raw[offset + 2 : offset + 2 + byte_len], "big")
    return val, offset + 2 + byte_len


def _mega_chunk_schedule(total_size: int) -> list[tuple[int, int]]:
    """Standard MEGA chunk layout: 128K, 256K, ..., 1M, then 1M repeating."""
    if total_size == 0:
        return [(0, 0)]
    chunks: list[tuple[int, int]] = []
    pos = 0
    idx = 1
    while pos < total_size:
        size = min(idx * 131072, 1048576)
        size = min(size, total_size - pos)
        chunks.append((pos, size))
        pos += size
        if idx < 8:
            idx += 1
    return chunks


class MegaUploader(BaseCloudUploader):
    provider_key = "mega"

    def __init__(self) -> None:
        self._seq = secrets.randbelow(1000000) + 1

    async def _api_call(
        self,
        session: aiohttp.ClientSession,
        payload: list[Dict[str, Any]],
        sid: str = "",
    ) -> Any:
        self._seq += 1
        url = f"https://g.api.mega.co.nz/cs?id={self._seq}"
        if sid:
            url += f"&sid={sid}"
        async with session.post(url, json=payload, headers={"User-Agent": DEFAULT_UA}) as resp:
            if resp.status >= 400:
                raise UploadError(f"MEGA API devolvió HTTP {resp.status}.")
            data = await resp.json(content_type=None)
        if isinstance(data, int):
            if data == -15:
                raise UploadAuthError("ESID")
            if data in (-9, -11):
                raise UploadAuthError("Correo o contraseña incorrectos en MEGA.")
            raise UploadError(f"Error de MEGA API (código {data}).")
        if isinstance(data, list) and data and isinstance(data[0], int) and data[0] < 0:
            code = data[0]
            if code == -15:
                raise UploadAuthError("ESID")
            if code in (-9, -11):
                raise UploadAuthError("Correo o contraseña incorrectos en MEGA.")
            if code == -17:
                raise UploadError("Cuota de almacenamiento/transferencia excedida en MEGA.")
            raise UploadError(f"Error de MEGA API (código {code}).")
        return data[0] if isinstance(data, list) and data else data

    async def _login_fresh(self, session: aiohttp.ClientSession, email: str, password: str) -> Dict[str, Any]:
        email_l = email.strip().lower()
        if not email_l or not password:
            raise UploadAuthError("Debes ingresar correo y contraseña de MEGA.")

        us0 = await self._api_call(session, [{"a": "us0", "user": email_l}])
        if not isinstance(us0, dict):
            raise UploadAuthError("No se pudo iniciar sesión en MEGA.")

        ver = int(us0.get("v") or 1)
        if ver == 2:
            salt = _b64url_decode(str(us0["s"]))
            dk = hashlib.pbkdf2_hmac("sha512", password.encode("utf-8"), salt, 100000, dklen=32)
            pw_key = dk[:16]
            uh = _b64url_encode(dk[16:32])
        else:
            pw_key = _mega_prepare_key_v1(password.encode("utf-8"))
            uh = _mega_stringhash_v1(email_l, pw_key)

        res = await self._api_call(session, [{"a": "us", "user": email_l, "uh": uh}])
        if not isinstance(res, dict) or "k" not in res or "csid" not in res:
            raise UploadAuthError("Credenciales inválidas en MEGA (o cuenta requiere verificación 2FA).")

        enc_mk = _b64url_decode(str(res["k"]))
        master_key = _aes_ecb_decrypt(enc_mk, pw_key)[:16]

        enc_privk = _b64url_decode(str(res["privk"]))
        privk_pad = enc_privk + (b"\x00" * ((-len(enc_privk)) % 16))
        privk_dec = _aes_ecb_decrypt(privk_pad, master_key)

        off = 0
        p, off = _mega_mpi_to_int(privk_dec, off)
        q, off = _mega_mpi_to_int(privk_dec, off)
        d, off = _mega_mpi_to_int(privk_dec, off)

        csid_raw = _b64url_decode(str(res["csid"]))
        c_int, _ = _mega_mpi_to_int(csid_raw, 0)
        m_int = pow(c_int, d, p * q)
        m_bytes = m_int.to_bytes((m_int.bit_length() + 7) // 8, "big")
        sid = _b64url_encode(m_bytes[:43])

        # Fetch Cloud Drive root node handle (t == 2)
        tree = await self._api_call(session, [{"a": "f", "c": 1}], sid=sid)
        root_handle = ""
        if isinstance(tree, dict):
            for node in tree.get("f") or []:
                if node.get("t") == 2:
                    root_handle = str(node.get("h") or "")
                    break

        return {
            "auth_type": "mega",
            "email": email_l,
            "password": password,
            "sid": sid,
            "master_key_hex": master_key.hex(),
            "root_handle": root_handle,
        }

    async def authenticate(
        self,
        session: aiohttp.ClientSession,
        creds: Dict[str, Any],
    ) -> tuple[str, Dict[str, Any]]:
        updated = dict(creds)
        email = str(updated.get("email") or updated.get("account_email") or "").strip().lower()
        password = str(updated.get("password") or "")
        sid = str(updated.get("sid") or "")

        # Convert base64 master key captured from mega.nz web localStorage if present
        if not updated.get("master_key_hex") and updated.get("master_key_b64"):
            try:
                mk_raw = _b64url_decode(str(updated["master_key_b64"]))
                if len(mk_raw) >= 16:
                    updated["master_key_hex"] = mk_raw[:16].hex()
            except Exception:
                pass

        if sid and updated.get("master_key_hex"):
            try:
                ug = await self._api_call(session, [{"a": "ug"}], sid=sid)
                if isinstance(ug, dict) and ug.get("email") and not email:
                    email = str(ug["email"]).strip().lower()
                    updated["email"] = email
                if not updated.get("root_handle"):
                    tree = await self._api_call(session, [{"a": "f", "c": 1}], sid=sid)
                    if isinstance(tree, dict):
                        for node in tree.get("f") or []:
                            if node.get("t") == 2:
                                updated["root_handle"] = str(node.get("h") or "")
                                break
                return email or "Cuenta MEGA", updated
            except UploadAuthError:
                pass
            except Exception:
                pass

        # Try silent web-profile refresh if this account was synced via the login window
        if updated.get("auth_type") == "web_session" and updated.get("web_profile_dir"):
            try:
                ref = await run_web_login_window(
                    "mega", str(updated["web_profile_dir"]), silent_refresh=True, timeout_sec=18.0
                )
                toks = ref.get("tokens") or {}
                if ref.get("cookies"):
                    updated["cookies"] = ref["cookies"]
                if toks.get("sid"):
                    updated["sid"] = str(toks["sid"])
                if toks.get("master_key_b64"):
                    updated["master_key_b64"] = str(toks["master_key_b64"])
                    mk_raw = _b64url_decode(str(toks["master_key_b64"]))
                    if len(mk_raw) >= 16:
                        updated["master_key_hex"] = mk_raw[:16].hex()
                if updated.get("sid") and updated.get("master_key_hex"):
                    ug = await self._api_call(session, [{"a": "ug"}], sid=updated["sid"])
                    if isinstance(ug, dict) and ug.get("email"):
                        updated["email"] = str(ug["email"]).strip().lower()
                    if not updated.get("root_handle"):
                        tree = await self._api_call(session, [{"a": "f", "c": 1}], sid=updated["sid"])
                        if isinstance(tree, dict):
                            for node in tree.get("f") or []:
                                if node.get("t") == 2:
                                    updated["root_handle"] = str(node.get("h") or "")
                                    break
                    return str(updated.get("email") or email or "Cuenta MEGA"), updated
            except Exception:
                pass
            if not (email and password):
                raise UploadAuthError(
                    "La sesión web de MEGA expiró o requiere re-autenticación. Abre la ventana de inicio de sesión."
                )

        fresh = await self._login_fresh(session, email, password)
        return fresh["email"], fresh

    async def upload_file(
        self,
        session: aiohttp.ClientSession,
        creds: Dict[str, Any],
        local_path: str,
        filename: str,
        remote_folder: str,
        extra: Dict[str, Any],
        progress_cb: Callable[[UploadProgress], None],
        cancel_event: asyncio.Event,
        get_speed_limit_kbps: Callable[[], int],
    ) -> tuple[str, Dict[str, Any]]:
        _, updated = await self.authenticate(session, creds)
        sid = updated["sid"]
        master_key = bytes.fromhex(updated["master_key_hex"])
        root_handle = updated["root_handle"]
        total_size = os.path.getsize(local_path)

        try:
            u_res = await self._api_call(session, [{"a": "u", "s": total_size}], sid=sid)
        except UploadAuthError as exc:
            if str(exc) == "ESID":
                updated = await self._login_fresh(session, updated["email"], updated["password"])
                sid = updated["sid"]
                master_key = bytes.fromhex(updated["master_key_hex"])
                root_handle = updated["root_handle"]
                u_res = await self._api_call(session, [{"a": "u", "s": total_size}], sid=sid)
            else:
                raise

        upload_url = str(u_res.get("p") or "")
        if not upload_url:
            raise UploadError("MEGA no devolvió URL de subida.")

        # Generate per-file random AES-128 key + 64-bit IV nonce
        file_key = secrets.token_bytes(16)
        iv_nonce = secrets.token_bytes(8)
        iv_a32 = _mega_bytes_to_a32(iv_nonce)
        mac_iv = iv_nonce + iv_nonce  # 16 bytes: (iv0, iv1, iv0, iv1)

        schedule = _mega_chunk_schedule(total_size)
        chunk_macs: list[bytes] = []
        completion_handle = ""

        start_mono = time.monotonic()
        uploaded = 0

        with open(local_path, "rb") as f:
            for pos, csize in schedule:
                if cancel_event.is_set():
                    raise UploadCancelledError("Subida pausada o detenida.")
                raw_chunk = f.read(csize) if csize > 0 else b""
                chunk_t0 = time.monotonic()

                # Compute per-chunk CBC-MAC
                padded = raw_chunk + (b"\x00" * ((-len(raw_chunk)) % 16))
                if padded:
                    cbc_out = _aes_cbc_encrypt(padded, file_key, iv=mac_iv)
                    chunk_macs.append(cbc_out[-16:])
                else:
                    chunk_macs.append(b"\x00" * 16)

                # Encrypt chunk with AES-128-CTR at exact 16-byte block offset
                block_counter = pos // 16
                ctr_iv = iv_nonce + struct.pack(">Q", block_counter)
                if raw_chunk:
                    if _HAS_CRYPTODOME:
                        init_val = int.from_bytes(ctr_iv, "big")
                        ctr = Counter.new(128, initial_value=init_val)
                        enc_chunk = AES.new(file_key, AES.MODE_CTR, counter=ctr).encrypt(raw_chunk)
                    else:
                        c = Cipher(algorithms.AES(file_key), modes.CTR(ctr_iv), backend=default_backend()).encryptor()
                        enc_chunk = c.update(raw_chunk) + c.finalize()
                else:
                    enc_chunk = b""

                async with session.post(f"{upload_url}/{pos}", data=enc_chunk) as resp:
                    if resp.status >= 400:
                        raise UploadError(f"Error al subir bloque a MEGA (HTTP {resp.status}).")
                    resp_text = (await resp.text()).strip()
                    if resp_text:
                        completion_handle = resp_text

                uploaded += len(raw_chunk)
                chunk_elapsed = time.monotonic() - chunk_t0
                await _throttle_speed(len(raw_chunk), chunk_elapsed, get_speed_limit_kbps())
                total_elapsed = max(0.05, time.monotonic() - start_mono)
                speed = uploaded / total_elapsed
                eta = int((total_size - uploaded) / speed) if speed > 0 else 0
                progress_cb(UploadProgress(uploaded, total_size, speed, eta, "uploading"))

        if not completion_handle:
            raise UploadError("MEGA no confirmó el identificador de finalización de subida.")

        # Compute overall meta-MAC across all chunk MACs
        meta_mac_raw = b"\x00" * 16
        for cm in chunk_macs:
            xored = bytes(a ^ b for a, b in zip(meta_mac_raw, cm))
            meta_mac_raw = _aes_ecb_encrypt(xored, file_key)
        mm32 = _mega_bytes_to_a32(meta_mac_raw)
        meta_mac_pair = (mm32[0] ^ mm32[1], mm32[2] ^ mm32[3])

        fk32 = _mega_bytes_to_a32(file_key)
        full_key_a32 = (
            fk32[0] ^ iv_a32[0],
            fk32[1] ^ iv_a32[1],
            fk32[2] ^ meta_mac_pair[0],
            fk32[3] ^ meta_mac_pair[1],
            iv_a32[0],
            iv_a32[1],
            meta_mac_pair[0],
            meta_mac_pair[1],
        )
        full_key_bytes = _mega_a32_to_bytes(full_key_a32)
        enc_node_key = _aes_ecb_encrypt(full_key_bytes, master_key)

        attrs_bytes = b"MEGA" + json.dumps({"n": filename}, separators=(",", ":")).encode("utf-8")
        enc_attrs = _aes_cbc_encrypt(attrs_bytes, file_key)

        put_res = await self._api_call(
            session,
            [
                {
                    "a": "p",
                    "t": root_handle,
                    "n": [
                        {
                            "h": completion_handle,
                            "t": 0,
                            "a": _b64url_encode(enc_attrs),
                            "k": _b64url_encode(enc_node_key),
                        }
                    ],
                }
            ],
            sid=sid,
        )

        share_url = ""
        try:
            node_h = ""
            if isinstance(put_res, dict) and put_res.get("f"):
                node_h = str(put_res["f"][0].get("h") or "")
            if node_h:
                pub_h = await self._api_call(session, [{"a": "l", "n": node_h}], sid=sid)
                if isinstance(pub_h, str) and pub_h:
                    share_url = f"https://mega.nz/file/{pub_h}#{_b64url_encode(full_key_bytes)}"
        except Exception:
            pass

        return share_url, updated

    @staticmethod
    def _decrypt_mega_node_key(enc_key_str: str, master_key: bytes) -> Optional[bytes]:
        try:
            # Node key string format: "<owner_handle>:<b64url_encrypted_key>"
            parts = enc_key_str.split("/")
            for part in parts:
                if ":" in part:
                    b64_part = part.split(":", 1)[1]
                else:
                    b64_part = part
                raw = _b64url_decode(b64_part)
                if len(raw) in (16, 32):
                    return _aes_ecb_decrypt(raw, master_key)
        except Exception:
            pass
        return None

    @staticmethod
    def _decrypt_mega_attrs(enc_attr_b64: str, node_key: bytes) -> Dict[str, Any]:
        try:
            if len(node_key) == 32:
                aes_key = bytes(a ^ b for a, b in zip(node_key[:16], node_key[16:32]))
            else:
                aes_key = node_key[:16]
            raw = _b64url_decode(enc_attr_b64)
            if len(raw) % 16 != 0:
                raw += b"\x00" * (16 - len(raw) % 16)
            if _HAS_CRYPTODOME:
                dec = AES.new(aes_key, AES.MODE_CBC, iv=b"\x00" * 16).decrypt(raw)
            else:
                c = Cipher(algorithms.AES(aes_key), modes.CBC(b"\x00" * 16), backend=default_backend()).decryptor()
                dec = c.update(raw) + c.finalize()
            txt = dec.rstrip(b"\x00").decode("utf-8", errors="ignore")
            if txt.startswith("MEGA"):
                txt = txt[4:]
            end = txt.rfind("}")
            if end != -1:
                return json.loads(txt[: end + 1])
        except Exception:
            pass
        return {}

    async def list_folder(
        self,
        session: aiohttp.ClientSession,
        creds: Dict[str, Any],
        folder_id: str = "",
        folder_path: str = "",
    ) -> tuple[List[CloudEntry], Dict[str, Any]]:
        _, updated = await self.authenticate(session, creds)
        sid = updated["sid"]
        master_key = bytes.fromhex(updated["master_key_hex"])
        tree = await self._api_call(session, [{"a": "f", "c": 1}], sid=sid)
        nodes = tree.get("f") if isinstance(tree, dict) else []
        if not isinstance(nodes, list):
            nodes = []

        root_h = updated.get("root_handle") or ""
        if not root_h:
            for n in nodes:
                if n.get("t") == 2:
                    root_h = str(n.get("h") or "")
                    updated["root_handle"] = root_h
                    break
        target_parent = folder_id.strip() or root_h
        base_p = folder_path.strip("/")
        entries: List[CloudEntry] = []

        for n in nodes:
            if str(n.get("p") or "") != target_parent:
                continue
            ntype = int(n.get("t") or 0)
            if ntype not in (0, 1):
                continue
            nh = str(n.get("h") or "")
            nkey = self._decrypt_mega_node_key(str(n.get("k") or ""), master_key)
            attrs = self._decrypt_mega_attrs(str(n.get("a") or ""), nkey) if nkey else {}
            name = str(attrs.get("n") or nh)
            is_dir = ntype == 1
            ts = int(n.get("ts") or 0)
            mod = time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts > 0 else ""
            child_path = f"{base_p}/{name}" if base_p else name
            extra_d: Dict[str, Any] = {}
            if nkey:
                extra_d["mega_node_key_b64"] = _b64url_encode(nkey)
            entries.append(
                CloudEntry(
                    id=nh,
                    name=name,
                    is_folder=is_dir,
                    size=0 if is_dir else int(n.get("s") or 0),
                    modified=mod,
                    path=child_path,
                    provider="mega",
                    extra=extra_d,
                )
            )
        entries.sort(key=lambda e: (not e.is_folder, e.name.lower()))
        return entries, updated

    async def create_folder(
        self,
        session: aiohttp.ClientSession,
        creds: Dict[str, Any],
        parent_id: str = "",
        parent_path: str = "",
        name: str = "Nueva carpeta",
    ) -> tuple[CloudEntry, Dict[str, Any]]:
        _, updated = await self.authenticate(session, creds)
        sid = updated["sid"]
        master_key = bytes.fromhex(updated["master_key_hex"])
        pid = parent_id.strip() or updated.get("root_handle") or ""
        clean_name = name.strip() or "Nueva carpeta"
        fkey = secrets.token_bytes(16)
        attr_bytes = f'MEGA{json.dumps({"n": clean_name}, separators=(",", ":"))}'.encode("utf-8")
        attr_pad = attr_bytes + b"\x00" * ((16 - (len(attr_bytes) % 16)) % 16)
        enc_attrs = _b64url_encode(_aes_cbc_encrypt(attr_pad, fkey, b"\x00" * 16))
        enc_key = _b64url_encode(_aes_ecb_encrypt(fkey, master_key))
        res = await self._api_call(
            session,
            [{"a": "p", "t": pid, "n": [{"h": "xxxxxxxx", "t": 1, "a": enc_attrs, "k": enc_key}]}],
            sid=sid,
        )
        nh = ""
        if isinstance(res, dict) and res.get("f"):
            nh = str(res["f"][0].get("h") or "")
        base_p = parent_path.strip("/")
        return (
            CloudEntry(
                id=nh,
                name=clean_name,
                is_folder=True,
                path=f"{base_p}/{clean_name}" if base_p else clean_name,
                provider="mega",
            ),
            updated,
        )

    async def get_download_request(
        self,
        session: aiohttp.ClientSession,
        creds: Dict[str, Any],
        entry: CloudEntry,
    ) -> tuple[str, Dict[str, str], Dict[str, Any]]:
        _, updated = await self.authenticate(session, creds)
        sid = updated["sid"]
        node_key_b64 = str(entry.extra.get("mega_node_key_b64") or "")
        pub_h = await self._api_call(session, [{"a": "l", "n": entry.id}], sid=sid)
        if isinstance(pub_h, str) and pub_h and node_key_b64:
            # MintTransfer's CloudExtractor natively decrypts https://mega.nz/file/<pub>#<key>!
            return f"https://mega.nz/file/{pub_h}#{node_key_b64}", {}, updated
        raise UploadError("No se pudo generar el enlace de descarga desde MEGA para este archivo.")


# ---------------------------------------------------------------------------
# 3. OneDrive — Microsoft Graph API v1.0 Resumable Upload Session
# ---------------------------------------------------------------------------

class OneDriveUploader(BaseCloudUploader):
    provider_key = "onedrive"
    CHUNK_SIZE = 320 * 1024 * 10  # 3.2 MiB (exact multiple of 320 KiB required by Graph API)

    async def _ensure_token(self, session: aiohttp.ClientSession, creds: Dict[str, Any]) -> Dict[str, Any]:
        updated = dict(creds)
        access_token = str(updated.get("access_token") or "")
        refresh_token = str(updated.get("refresh_token") or "")
        expires_at = float(updated.get("expires_at") or 0.0)

        if access_token and (not refresh_token or time.time() < expires_at - 60):
            return updated

        if not refresh_token:
            if access_token and (expires_at <= 0 or time.time() < expires_at - 60):
                return updated
            if updated.get("auth_type") == "web_session" and updated.get("web_profile_dir"):
                try:
                    ref = await run_web_login_window(
                        "onedrive", str(updated["web_profile_dir"]), silent_refresh=True, timeout_sec=18.0
                    )
                    toks = ref.get("tokens") or {}
                    if ref.get("cookies"):
                        updated["cookies"] = ref["cookies"]
                    if toks.get("access_token"):
                        updated["access_token"] = str(toks["access_token"])
                        updated["expires_at"] = float(toks.get("expires_at") or (time.time() + 3000))
                        if toks.get("refresh_token"):
                            updated["refresh_token"] = str(toks["refresh_token"])
                        if toks.get("client_id"):
                            updated["client_id"] = str(toks["client_id"])
                        return updated
                except Exception:
                    pass
                if access_token:
                    return updated
                raise UploadError(
                    "Sesión web de OneDrive activa (guardada para Downloader). "
                    "Para subir directo por Microsoft Graph API, re-abre la sesión o vincula OAuth2 en Opciones avanzadas."
                )
            if access_token:
                return updated
            raise UploadAuthError("Sesión de OneDrive expirada. Vuelve a sincronizar la cuenta.")

        client_id = str(updated.get("client_id") or "")
        if not client_id:
            if access_token:
                return updated
            raise UploadAuthError("Falta el Client ID para renovar la sesión de OneDrive.")

        data = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
            "scope": OAUTH_CONFIGS["onedrive"]["scope"],
        }
        if updated.get("client_secret"):
            data["client_secret"] = str(updated["client_secret"])

        async with session.post(OAUTH_CONFIGS["onedrive"]["token_url"], data=data) as resp:
            body = await resp.json(content_type=None)
            if resp.status >= 400 or "error" in body:
                err_code = str(body.get("error") or "")
                if err_code in ("invalid_grant", "interaction_required"):
                    raise UploadAuthError("La sesión de OneDrive expiró o cambió la contraseña. Re-sincroniza.")
                raise UploadError(f"Error al renovar token OneDrive: {body.get('error_description') or err_code}")

        updated["access_token"] = str(body["access_token"])
        updated["expires_at"] = time.time() + max(60, int(body.get("expires_in") or 3600) - 60)
        if body.get("refresh_token"):
            updated["refresh_token"] = str(body["refresh_token"])
        return updated

    async def authenticate(
        self,
        session: aiohttp.ClientSession,
        creds: Dict[str, Any],
    ) -> tuple[str, Dict[str, Any]]:
        if creds.get("auth_type") == "web_session" and not creds.get("access_token") and not creds.get("refresh_token"):
            cookie_hdr = _build_cookie_header(
                creds.get("cookies"), ("live.com", "onedrive.com", "microsoft.com", "sharepoint.com")
            )
            if not cookie_hdr:
                raise UploadAuthError("La sesión web de OneDrive no tiene cookies activas. Re-abre el inicio de sesión.")
            async with session.get(
                "https://onedrive.live.com/",
                headers={"Cookie": cookie_hdr, "User-Agent": DEFAULT_UA},
                allow_redirects=True,
            ) as resp:
                final_url = str(resp.url).lower()
                if "login.live.com" in final_url or "login.microsoftonline.com" in final_url or resp.status >= 400:
                    raise UploadAuthError("La sesión web de OneDrive expiró o cambió la contraseña. Re-sincroniza.")
            email = str(creds.get("account_email") or creds.get("email") or "Sesión web OneDrive")
            return email, dict(creds)

        updated = await self._ensure_token(session, creds)
        headers = {"Authorization": f"Bearer {updated['access_token']}"}
        async with session.get("https://graph.microsoft.com/v1.0/me", headers=headers) as resp:
            if resp.status == 401:
                updated["expires_at"] = 0
                updated = await self._ensure_token(session, updated)
                headers = {"Authorization": f"Bearer {updated['access_token']}"}
                async with session.get("https://graph.microsoft.com/v1.0/me", headers=headers) as resp2:
                    if resp2.status >= 400:
                        raise UploadAuthError("Token de OneDrive inválido o expirado.")
                    data = await resp2.json(content_type=None)
            elif resp.status >= 400:
                raise UploadAuthError(f"Error al verificar cuenta OneDrive (HTTP {resp.status}).")
            else:
                data = await resp.json(content_type=None)
        email = str(data.get("mail") or data.get("userPrincipalName") or data.get("displayName") or "Cuenta OneDrive")
        return email, updated

    async def upload_file(
        self,
        session: aiohttp.ClientSession,
        creds: Dict[str, Any],
        local_path: str,
        filename: str,
        remote_folder: str,
        extra: Dict[str, Any],
        progress_cb: Callable[[UploadProgress], None],
        cancel_event: asyncio.Event,
        get_speed_limit_kbps: Callable[[], int],
    ) -> tuple[str, Dict[str, Any]]:
        updated = await self._ensure_token(session, creds)
        total_size = os.path.getsize(local_path)
        headers = {"Authorization": f"Bearer {updated['access_token']}"}

        folder_clean = remote_folder.strip("/\\")
        remote_path = f"{folder_clean}/{filename}" if folder_clean else filename
        encoded_path = urllib.parse.quote(remote_path)

        session_uri = str(extra.get("upload_session_uri") or "")
        offset = 0

        if session_uri and total_size > 0:
            try:
                async with session.get(session_uri) as probe:
                    if probe.status == 200:
                        pdata = await probe.json(content_type=None)
                        ranges = pdata.get("nextExpectedRanges") or []
                        if ranges:
                            offset = int(str(ranges[0]).split("-")[0] or 0)
                    else:
                        session_uri = ""
            except Exception:
                session_uri = ""

        if not session_uri:
            create_url = f"https://graph.microsoft.com/v1.0/me/drive/root:/{encoded_path}:/createUploadSession"
            payload = {"item": {"@microsoft.graph.conflictBehavior": "rename", "name": filename}}
            async with session.post(create_url, headers=headers, json=payload) as resp:
                if resp.status == 401:
                    raise UploadAuthError("Sesión de OneDrive expirada.")
                if resp.status >= 400:
                    txt = await resp.text()
                    raise UploadError(f"OneDrive rechazó la subida (HTTP {resp.status}): {txt[:120]}")
                sdata = await resp.json(content_type=None)
                session_uri = str(sdata.get("uploadUrl") or "")
                if not session_uri:
                    raise UploadError("OneDrive no devolvió URL de sesión de subida.")
                extra["upload_session_uri"] = session_uri

        if total_size == 0:
            async with session.put(
                f"https://graph.microsoft.com/v1.0/me/drive/root:/{encoded_path}:/content",
                headers=headers,
                data=b"",
            ) as r0:
                res = await r0.json(content_type=None)
                return str(res.get("webUrl") or ""), updated

        start_mono = time.monotonic()
        bytes_this_run = 0
        web_url = ""

        with open(local_path, "rb") as f:
            f.seek(offset)
            while offset < total_size:
                if cancel_event.is_set():
                    raise UploadCancelledError("Subida pausada o detenida.")
                chunk = f.read(self.CHUNK_SIZE)
                if not chunk:
                    break
                end_byte = offset + len(chunk) - 1
                chunk_t0 = time.monotonic()
                put_headers = {
                    "Content-Length": str(len(chunk)),
                    "Content-Range": f"bytes {offset}-{end_byte}/{total_size}",
                }
                async with session.put(session_uri, data=chunk, headers=put_headers) as put_resp:
                    if put_resp.status in (200, 201):
                        res = await put_resp.json(content_type=None)
                        web_url = str(res.get("webUrl") or "")
                        offset += len(chunk)
                        bytes_this_run += len(chunk)
                    elif put_resp.status == 202:
                        offset += len(chunk)
                        bytes_this_run += len(chunk)
                    elif put_resp.status == 401:
                        raise UploadAuthError("Sesión de OneDrive expirada.")
                    else:
                        txt = await put_resp.text()
                        raise UploadError(f"Error al subir bloque a OneDrive (HTTP {put_resp.status}): {txt[:120]}")

                chunk_elapsed = time.monotonic() - chunk_t0
                await _throttle_speed(len(chunk), chunk_elapsed, get_speed_limit_kbps())
                total_elapsed = max(0.05, time.monotonic() - start_mono)
                speed = bytes_this_run / total_elapsed
                eta = int((total_size - offset) / speed) if speed > 0 else 0
                progress_cb(UploadProgress(offset, total_size, speed, eta, "uploading"))

        extra.pop("upload_session_uri", None)
        return web_url, updated

    async def list_folder(
        self,
        session: aiohttp.ClientSession,
        creds: Dict[str, Any],
        folder_id: str = "",
        folder_path: str = "",
    ) -> tuple[List[CloudEntry], Dict[str, Any]]:
        if creds.get("auth_type") == "web_session" and not creds.get("access_token") and not creds.get("refresh_token"):
            prof = str(creds.get("web_profile_dir") or "")
            if not prof or not os.path.isdir(prof):
                raise UploadAuthError(f"Falta el perfil web de {self.provider_key}. Re-abre la sesión.")
            res = await run_web_list_folder(self.provider_key, prof, folder_id=folder_id, folder_path=folder_path)
            updated = dict(creds)
            if isinstance(res.get("cookies"), list) and res["cookies"]:
                updated["cookies"] = res["cookies"]
            if res.get("account_email"):
                updated["account_email"] = str(res["account_email"])
            if res.get("account_name"):
                updated["detected_name"] = str(res["account_name"])
            toks = res.get("tokens") if isinstance(res.get("tokens"), dict) else {}
            for tk, tv in toks.items():
                if tv:
                    updated[tk] = tv
            entries = _raw_web_entries_to_cloud(self.provider_key, res.get("entries"))
            return entries, updated

        updated = await self._ensure_token(session, creds)
        headers = {"Authorization": f"Bearer {updated['access_token']}"}
        if folder_id.strip():
            url = f"https://graph.microsoft.com/v1.0/me/drive/items/{folder_id.strip()}/children"
        else:
            url = "https://graph.microsoft.com/v1.0/me/drive/root/children"
        params = {
            "$select": "id,name,size,folder,file,lastModifiedDateTime,webUrl,@microsoft.graph.downloadUrl",
            "$top": "200",
        }
        async with session.get(url, headers=headers, params=params) as resp:
            if resp.status == 401:
                updated["expires_at"] = 0
                updated = await self._ensure_token(session, updated)
                headers = {"Authorization": f"Bearer {updated['access_token']}"}
                async with session.get(url, headers=headers, params=params) as r2:
                    if r2.status >= 400:
                        raise UploadAuthError("Sesión de OneDrive expirada al listar archivos.")
                    data = await r2.json(content_type=None)
            elif resp.status >= 400:
                txt = await resp.text()
                raise UploadError(f"Error al listar OneDrive (HTTP {resp.status}): {txt[:120]}")
            else:
                data = await resp.json(content_type=None)

        base_p = folder_path.strip("/")
        entries: List[CloudEntry] = []
        for it in data.get("value") or []:
            is_dir = "folder" in it
            name = str(it.get("name") or "")
            child_path = f"{base_p}/{name}" if base_p else name
            mod = str(it.get("lastModifiedDateTime") or "")[:16].replace("T", " ")
            entries.append(
                CloudEntry(
                    id=str(it.get("id") or ""),
                    name=name,
                    is_folder=is_dir,
                    size=0 if is_dir else int(it.get("size") or 0),
                    modified=mod,
                    path=child_path,
                    web_url=str(it.get("webUrl") or ""),
                    download_url=str(it.get("@microsoft.graph.downloadUrl") or ""),
                    provider="onedrive",
                )
            )
        entries.sort(key=lambda e: (not e.is_folder, e.name.lower()))
        return entries, updated

    async def create_folder(
        self,
        session: aiohttp.ClientSession,
        creds: Dict[str, Any],
        parent_id: str = "",
        parent_path: str = "",
        name: str = "Nueva carpeta",
    ) -> tuple[CloudEntry, Dict[str, Any]]:
        updated = await self._ensure_token(session, creds)
        headers = {"Authorization": f"Bearer {updated['access_token']}", "Content-Type": "application/json"}
        url = (
            f"https://graph.microsoft.com/v1.0/me/drive/items/{parent_id.strip()}/children"
            if parent_id.strip()
            else "https://graph.microsoft.com/v1.0/me/drive/root/children"
        )
        clean_name = name.strip() or "Nueva carpeta"
        body = {"name": clean_name, "folder": {}, "@microsoft.graph.conflictBehavior": "rename"}
        async with session.post(url, headers=headers, json=body) as resp:
            if resp.status >= 400:
                txt = await resp.text()
                raise UploadError(f"No se pudo crear carpeta en OneDrive: {txt[:120]}")
            d = await resp.json(content_type=None)
        base_p = parent_path.strip("/")
        cname = str(d.get("name") or clean_name)
        return (
            CloudEntry(
                id=str(d.get("id") or ""),
                name=cname,
                is_folder=True,
                path=f"{base_p}/{cname}" if base_p else cname,
                web_url=str(d.get("webUrl") or ""),
                provider="onedrive",
            ),
            updated,
        )

    async def get_download_request(
        self,
        session: aiohttp.ClientSession,
        creds: Dict[str, Any],
        entry: CloudEntry,
    ) -> tuple[str, Dict[str, str], Dict[str, Any]]:
        updated = await self._ensure_token(session, creds)
        if entry.download_url:
            return entry.download_url, {}, updated
        headers = {"Authorization": f"Bearer {updated['access_token']}"}
        url = f"https://graph.microsoft.com/v1.0/me/drive/items/{entry.id}/content"
        return url, headers, updated


# ---------------------------------------------------------------------------
# 4. Dropbox — API v2 Chunked Upload Session
# ---------------------------------------------------------------------------

class DropboxUploader(BaseCloudUploader):
    provider_key = "dropbox"
    CHUNK_SIZE = 4 * 1024 * 1024  # 4 MiB

    async def _ensure_token(self, session: aiohttp.ClientSession, creds: Dict[str, Any]) -> Dict[str, Any]:
        updated = dict(creds)
        access_token = str(updated.get("access_token") or "")
        refresh_token = str(updated.get("refresh_token") or "")
        expires_at = float(updated.get("expires_at") or 0.0)

        if access_token and (not refresh_token or time.time() < expires_at - 60):
            return updated

        if not refresh_token:
            if access_token:
                return updated
            if updated.get("auth_type") == "web_session":
                raise UploadError(
                    "Sesión web de Dropbox activa (guardada para Downloader). "
                    "Para subir directo por Dropbox API, vincula también OAuth2 en «Agregar cuenta → Opciones avanzadas»."
                )
            raise UploadAuthError("Sesión de Dropbox expirada. Vuelve a sincronizar la cuenta.")

        client_id = str(updated.get("client_id") or "")
        if not client_id:
            if access_token:
                return updated
            raise UploadAuthError("Falta el Client ID para renovar la sesión de Dropbox.")

        data = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
        }
        if updated.get("client_secret"):
            data["client_secret"] = str(updated["client_secret"])

        async with session.post(OAUTH_CONFIGS["dropbox"]["token_url"], data=data) as resp:
            body = await resp.json(content_type=None)
            if resp.status >= 400 or "error" in body:
                err_code = str(body.get("error") or "")
                if err_code in ("invalid_grant", "invalid_token"):
                    raise UploadAuthError("La sesión de Dropbox expiró o fue revocada. Re-sincroniza.")
                raise UploadError(f"Error al renovar token Dropbox: {body.get('error_description') or err_code}")

        updated["access_token"] = str(body["access_token"])
        updated["expires_at"] = time.time() + max(60, int(body.get("expires_in") or 14400) - 60)
        return updated

    async def authenticate(
        self,
        session: aiohttp.ClientSession,
        creds: Dict[str, Any],
    ) -> tuple[str, Dict[str, Any]]:
        if creds.get("auth_type") == "web_session" and not creds.get("access_token") and not creds.get("refresh_token"):
            cookie_hdr = _build_cookie_header(creds.get("cookies"), ("dropbox.com",))
            if not cookie_hdr:
                raise UploadAuthError("La sesión web de Dropbox no tiene cookies activas. Re-abre el inicio de sesión.")
            async with session.get(
                "https://www.dropbox.com/home",
                headers={"Cookie": cookie_hdr, "User-Agent": DEFAULT_UA},
                allow_redirects=True,
            ) as resp:
                final_url = str(resp.url).lower()
                if "/login" in final_url or resp.status >= 400:
                    raise UploadAuthError("La sesión web de Dropbox expiró o cambió la contraseña. Re-sincroniza.")
            email = str(creds.get("account_email") or creds.get("email") or "Sesión web Dropbox")
            return email, dict(creds)

        updated = await self._ensure_token(session, creds)
        headers = {"Authorization": f"Bearer {updated['access_token']}"}
        async with session.post("https://api.dropboxapi.com/2/users/get_current_account", headers=headers) as resp:
            if resp.status == 401:
                updated["expires_at"] = 0
                updated = await self._ensure_token(session, updated)
                headers = {"Authorization": f"Bearer {updated['access_token']}"}
                async with session.post("https://api.dropboxapi.com/2/users/get_current_account", headers=headers) as resp2:
                    if resp2.status >= 400:
                        raise UploadAuthError("Token de Dropbox inválido o expirado.")
                    data = await resp2.json(content_type=None)
            elif resp.status >= 400:
                raise UploadAuthError(f"Error al verificar cuenta Dropbox (HTTP {resp.status}).")
            else:
                data = await resp.json(content_type=None)
        email = str(data.get("email") or (data.get("name") or {}).get("display_name") or "Cuenta Dropbox")
        return email, updated

    async def upload_file(
        self,
        session: aiohttp.ClientSession,
        creds: Dict[str, Any],
        local_path: str,
        filename: str,
        remote_folder: str,
        extra: Dict[str, Any],
        progress_cb: Callable[[UploadProgress], None],
        cancel_event: asyncio.Event,
        get_speed_limit_kbps: Callable[[], int],
    ) -> tuple[str, Dict[str, Any]]:
        updated = await self._ensure_token(session, creds)
        total_size = os.path.getsize(local_path)
        auth_header = {"Authorization": f"Bearer {updated['access_token']}"}

        folder_clean = remote_folder.strip("/\\")
        remote_path = f"/{folder_clean}/{filename}" if folder_clean else f"/{filename}"

        session_id = str(extra.get("dropbox_session_id") or "")
        offset = int(extra.get("dropbox_offset") or 0) if session_id else 0

        start_mono = time.monotonic()
        bytes_this_run = 0

        with open(local_path, "rb") as f:
            f.seek(offset)
            if not session_id:
                first_chunk = f.read(self.CHUNK_SIZE)
                if cancel_event.is_set():
                    raise UploadCancelledError("Subida pausada o detenida.")
                chunk_t0 = time.monotonic()
                if len(first_chunk) == total_size:
                    # Single-shot finish for files <= 4 MiB
                    arg = json.dumps({"path": remote_path, "mode": "add", "autorename": True})
                    async with session.post(
                        "https://content.dropboxapi.com/2/files/upload",
                        headers={**auth_header, "Content-Type": "application/octet-stream", "Dropbox-API-Arg": arg},
                        data=first_chunk,
                    ) as resp:
                        if resp.status == 401:
                            raise UploadAuthError("Sesión de Dropbox expirada.")
                        if resp.status >= 400:
                            txt = await resp.text()
                            raise UploadError(f"Error al subir a Dropbox (HTTP {resp.status}): {txt[:120]}")
                        meta = await resp.json(content_type=None)
                        remote_path = str(meta.get("path_display") or remote_path)
                    progress_cb(UploadProgress(total_size, total_size, len(first_chunk) / max(0.05, time.monotonic() - chunk_t0), 0, "uploading"))
                    offset = total_size
                else:
                    async with session.post(
                        "https://content.dropboxapi.com/2/files/upload_session/start",
                        headers={
                            **auth_header,
                            "Content-Type": "application/octet-stream",
                            "Dropbox-API-Arg": json.dumps({"close": False}),
                        },
                        data=first_chunk,
                    ) as resp:
                        if resp.status == 401:
                            raise UploadAuthError("Sesión de Dropbox expirada.")
                        if resp.status >= 400:
                            txt = await resp.text()
                            raise UploadError(f"Dropbox rechazó inicio de subida (HTTP {resp.status}): {txt[:120]}")
                        sdata = await resp.json(content_type=None)
                        session_id = str(sdata.get("session_id") or "")
                    offset = len(first_chunk)
                    bytes_this_run += len(first_chunk)
                    extra["dropbox_session_id"] = session_id
                    extra["dropbox_offset"] = offset
                    await _throttle_speed(len(first_chunk), time.monotonic() - chunk_t0, get_speed_limit_kbps())
                    spd = bytes_this_run / max(0.05, time.monotonic() - start_mono)
                    progress_cb(UploadProgress(offset, total_size, spd, int((total_size - offset) / spd) if spd > 0 else 0, "uploading"))

            while offset < total_size:
                if cancel_event.is_set():
                    raise UploadCancelledError("Subida pausada o detenida.")
                chunk = f.read(self.CHUNK_SIZE)
                if not chunk:
                    break
                is_last = (offset + len(chunk)) >= total_size
                chunk_t0 = time.monotonic()
                if is_last:
                    arg = json.dumps(
                        {
                            "cursor": {"session_id": session_id, "offset": offset},
                            "commit": {"path": remote_path, "mode": "add", "autorename": True},
                        }
                    )
                    endpoint = "https://content.dropboxapi.com/2/files/upload_session/finish"
                else:
                    arg = json.dumps({"cursor": {"session_id": session_id, "offset": offset}, "close": False})
                    endpoint = "https://content.dropboxapi.com/2/files/upload_session/append_v2"

                async with session.post(
                    endpoint,
                    headers={**auth_header, "Content-Type": "application/octet-stream", "Dropbox-API-Arg": arg},
                    data=chunk,
                ) as resp:
                    if resp.status == 401:
                        raise UploadAuthError("Sesión de Dropbox expirada.")
                    if resp.status >= 400:
                        txt = await resp.text()
                        if "incorrect_offset" in txt:
                            extra.pop("dropbox_session_id", None)
                            extra.pop("dropbox_offset", None)
                        raise UploadError(f"Error al subir bloque a Dropbox (HTTP {resp.status}): {txt[:120]}")
                    if is_last:
                        meta = await resp.json(content_type=None)
                        remote_path = str(meta.get("path_display") or remote_path)

                offset += len(chunk)
                bytes_this_run += len(chunk)
                extra["dropbox_offset"] = offset
                await _throttle_speed(len(chunk), time.monotonic() - chunk_t0, get_speed_limit_kbps())
                spd = bytes_this_run / max(0.05, time.monotonic() - start_mono)
                progress_cb(UploadProgress(offset, total_size, spd, int((total_size - offset) / spd) if spd > 0 else 0, "uploading"))

        extra.pop("dropbox_session_id", None)
        extra.pop("dropbox_offset", None)

        share_url = ""
        try:
            async with session.post(
                "https://api.dropboxapi.com/2/sharing/create_shared_link_with_settings",
                headers={**auth_header, "Content-Type": "application/json"},
                json={"path": remote_path},
            ) as sresp:
                if sresp.status == 200:
                    share_url = str((await sresp.json(content_type=None)).get("url") or "")
        except Exception:
            pass

        return share_url, updated

    async def list_folder(
        self,
        session: aiohttp.ClientSession,
        creds: Dict[str, Any],
        folder_id: str = "",
        folder_path: str = "",
    ) -> tuple[List[CloudEntry], Dict[str, Any]]:
        if creds.get("auth_type") == "web_session" and not creds.get("access_token") and not creds.get("refresh_token"):
            prof = str(creds.get("web_profile_dir") or "")
            if not prof or not os.path.isdir(prof):
                raise UploadAuthError(f"Falta el perfil web de {self.provider_key}. Re-abre la sesión.")
            res = await run_web_list_folder(self.provider_key, prof, folder_id=folder_id, folder_path=folder_path)
            updated = dict(creds)
            if isinstance(res.get("cookies"), list) and res["cookies"]:
                updated["cookies"] = res["cookies"]
            if res.get("account_email"):
                updated["account_email"] = str(res["account_email"])
            if res.get("account_name"):
                updated["detected_name"] = str(res["account_name"])
            toks = res.get("tokens") if isinstance(res.get("tokens"), dict) else {}
            for tk, tv in toks.items():
                if tv:
                    updated[tk] = tv
            entries = _raw_web_entries_to_cloud(self.provider_key, res.get("entries"))
            return entries, updated

        updated = await self._ensure_token(session, creds)
        headers = {
            "Authorization": f"Bearer {updated['access_token']}",
            "Content-Type": "application/json",
        }
        clean_path = folder_path.strip("/")
        api_path = f"/{clean_path}" if clean_path else ""
        payload = {"path": api_path, "limit": 200}
        async with session.post(
            "https://api.dropboxapi.com/2/files/list_folder", headers=headers, json=payload
        ) as resp:
            if resp.status == 401:
                updated["expires_at"] = 0
                updated = await self._ensure_token(session, updated)
                headers["Authorization"] = f"Bearer {updated['access_token']}"
                async with session.post(
                    "https://api.dropboxapi.com/2/files/list_folder", headers=headers, json=payload
                ) as r2:
                    if r2.status >= 400:
                        raise UploadAuthError("Sesión de Dropbox expirada al listar archivos.")
                    data = await r2.json(content_type=None)
            elif resp.status >= 400:
                txt = await resp.text()
                raise UploadError(f"Error al listar Dropbox (HTTP {resp.status}): {txt[:120]}")
            else:
                data = await resp.json(content_type=None)

        entries: List[CloudEntry] = []
        for it in data.get("entries") or []:
            tag = str(it.get(".tag") or "")
            is_dir = tag == "folder"
            name = str(it.get("name") or "")
            pdisp = str(it.get("path_display") or it.get("path_lower") or "").lstrip("/")
            mod = str(it.get("client_modified") or it.get("server_modified") or "")[:16].replace("T", " ")
            entries.append(
                CloudEntry(
                    id=str(it.get("id") or pdisp),
                    name=name,
                    is_folder=is_dir,
                    size=0 if is_dir else int(it.get("size") or 0),
                    modified=mod,
                    path=pdisp,
                    provider="dropbox",
                )
            )
        entries.sort(key=lambda e: (not e.is_folder, e.name.lower()))
        return entries, updated

    async def create_folder(
        self,
        session: aiohttp.ClientSession,
        creds: Dict[str, Any],
        parent_id: str = "",
        parent_path: str = "",
        name: str = "Nueva carpeta",
    ) -> tuple[CloudEntry, Dict[str, Any]]:
        updated = await self._ensure_token(session, creds)
        headers = {
            "Authorization": f"Bearer {updated['access_token']}",
            "Content-Type": "application/json",
        }
        clean_name = name.strip() or "Nueva carpeta"
        base = parent_path.strip("/")
        target = f"/{base}/{clean_name}" if base else f"/{clean_name}"
        async with session.post(
            "https://api.dropboxapi.com/2/files/create_folder_v2",
            headers=headers,
            json={"path": target, "autorename": True},
        ) as resp:
            if resp.status >= 400:
                txt = await resp.text()
                raise UploadError(f"No se pudo crear carpeta en Dropbox: {txt[:120]}")
            d = (await resp.json(content_type=None)).get("metadata") or {}
        pdisp = str(d.get("path_display") or target).lstrip("/")
        return (
            CloudEntry(
                id=str(d.get("id") or pdisp),
                name=str(d.get("name") or clean_name),
                is_folder=True,
                path=pdisp,
                provider="dropbox",
            ),
            updated,
        )

    async def get_download_request(
        self,
        session: aiohttp.ClientSession,
        creds: Dict[str, Any],
        entry: CloudEntry,
    ) -> tuple[str, Dict[str, str], Dict[str, Any]]:
        if creds.get("auth_type") == "web_session" and not creds.get("access_token") and not creds.get("refresh_token"):
            cookie_hdr = _build_cookie_header(creds.get("cookies"), ("dropbox.com",))
            url = entry.download_url or (f"{entry.web_url}?dl=1" if entry.web_url else "")
            if not url:
                raise UploadError("No se encontró enlace directo para este archivo de Dropbox.")
            hdrs = {"User-Agent": DEFAULT_UA}
            if cookie_hdr:
                hdrs["Cookie"] = cookie_hdr
            return url, hdrs, dict(creds)
        updated = await self._ensure_token(session, creds)
        headers = {
            "Authorization": f"Bearer {updated['access_token']}",
            "Content-Type": "application/json",
        }
        target = f"/{entry.path.lstrip('/')}"
        async with session.post(
            "https://api.dropboxapi.com/2/files/get_temporary_link",
            headers=headers,
            json={"path": target},
        ) as resp:
            if resp.status >= 400:
                txt = await resp.text()
                raise UploadError(f"No se pudo obtener enlace de Dropbox: {txt[:120]}")
            d = await resp.json(content_type=None)
        link = str(d.get("link") or "")
        if not link:
            raise UploadError("Dropbox no devolvió enlace de descarga.")
        return link, {}, updated


# ---------------------------------------------------------------------------
# 5. Mediafire — REST API v1.5 (Permanent Encrypted Session + Streaming Upload)
# ---------------------------------------------------------------------------

def _sha256_file_sync(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            blk = f.read(1024 * 1024)
            if not blk:
                break
            h.update(blk)
    return h.hexdigest()


class MediafireUploader(BaseCloudUploader):
    provider_key = "mediafire"
    APP_ID = "42511"
    SIMPLE_THRESHOLD = 20 * 1024 * 1024  # 20 MiB

    async def _login_fresh(self, session: aiohttp.ClientSession, email: str, password: str) -> Dict[str, Any]:
        email_c = email.strip()
        if not email_c or not password:
            raise UploadAuthError("Debes ingresar correo y contraseña de Mediafire.")

        sig = hashlib.sha1(f"{email_c}{password}{self.APP_ID}".encode("utf-8")).hexdigest()
        form = {
            "email": email_c,
            "password": password,
            "application_id": self.APP_ID,
            "signature": sig,
            "token_version": "1",
            "response_format": "json",
        }
        async with session.post(
            "https://www.mediafire.com/api/1.5/user/get_session_token.php",
            data=form,
            headers={"User-Agent": DEFAULT_UA},
        ) as resp:
            data = await resp.json(content_type=None)
        r = data.get("response") or {}
        if r.get("result") != "Success" or not r.get("session_token"):
            err_code = int(r.get("error") or 0)
            msg = str(r.get("message") or "Credenciales inválidas en Mediafire.")
            if err_code in (107, 108, 109):
                raise UploadAuthError(f"Correo o contraseña incorrectos en Mediafire: {msg}")
            raise UploadAuthError(f"No se pudo iniciar sesión en Mediafire: {msg}")

        return {
            "auth_type": "mediafire",
            "email": email_c,
            "password": password,
            "session_token": str(r["session_token"]),
            "token_time": time.time(),
        }

    async def authenticate(
        self,
        session: aiohttp.ClientSession,
        creds: Dict[str, Any],
    ) -> tuple[str, Dict[str, Any]]:
        updated = dict(creds)
        email = str(updated.get("email") or updated.get("account_email") or "").strip()
        password = str(updated.get("password") or "")
        tok = str(updated.get("session_token") or "")

        if tok:
            try:
                async with session.get(
                    "https://www.mediafire.com/api/1.5/user/get_info.php",
                    params={"session_token": tok, "response_format": "json"},
                    headers={"User-Agent": DEFAULT_UA},
                ) as resp:
                    data = await resp.json(content_type=None)
                if (data.get("response") or {}).get("result") == "Success":
                    uinfo = (data.get("response") or {}).get("user_info") or {}
                    return str(uinfo.get("email") or email or "Cuenta Mediafire"), updated
            except Exception:
                pass

        # Exchange saved web_session cookies for an official MediaFire v1.5 session_token
        cookie_hdr = _build_cookie_header(updated.get("cookies"), ("mediafire.com",))
        if cookie_hdr:
            try:
                async with session.get(
                    "https://www.mediafire.com/application/get_session_token.php",
                    params={"response_format": "json"},
                    headers={"Cookie": cookie_hdr, "User-Agent": DEFAULT_UA},
                ) as resp:
                    data = await resp.json(content_type=None)
                new_tok = str((data.get("response") or {}).get("session_token") or "")
                if new_tok:
                    updated["session_token"] = new_tok
                    updated["token_time"] = time.time()
                    async with session.get(
                        "https://www.mediafire.com/api/1.5/user/get_info.php",
                        params={"session_token": new_tok, "response_format": "json"},
                        headers={"User-Agent": DEFAULT_UA},
                    ) as uresp:
                        udata = await uresp.json(content_type=None)
                    uinfo = (udata.get("response") or {}).get("user_info") or {}
                    resolved_email = str(uinfo.get("email") or email or "Cuenta Mediafire")
                    updated["email"] = resolved_email
                    return resolved_email, updated
            except Exception:
                pass
            if not (email and password):
                raise UploadAuthError(
                    "La sesión web de Mediafire expiró o cambió la contraseña. Re-abre la ventana de inicio de sesión."
                )

        fresh = await self._login_fresh(session, email, password)
        return fresh["email"], fresh

    async def upload_file(
        self,
        session: aiohttp.ClientSession,
        creds: Dict[str, Any],
        local_path: str,
        filename: str,
        remote_folder: str,
        extra: Dict[str, Any],
        progress_cb: Callable[[UploadProgress], None],
        cancel_event: asyncio.Event,
        get_speed_limit_kbps: Callable[[], int],
    ) -> tuple[str, Dict[str, Any]]:
        _, updated = await self.authenticate(session, creds)
        tok = updated["session_token"]
        total_size = os.path.getsize(local_path)
        file_hash = await asyncio.to_thread(_sha256_file_sync, local_path)

        # Check resumable/instant upload availability
        check_params = {
            "session_token": tok,
            "filename": filename,
            "size": str(total_size),
            "hash": file_hash,
            "resumable": "yes" if total_size > self.SIMPLE_THRESHOLD else "no",
            "response_format": "json",
        }
        async with session.post(
            "https://www.mediafire.com/api/1.5/upload/check.php",
            data=check_params,
            headers={"User-Agent": DEFAULT_UA},
        ) as cresp:
            cdata = await cresp.json(content_type=None)
        cr = cdata.get("response") or {}
        if cr.get("result") != "Success":
            raise UploadError(f"Mediafire rechazó verificación previa: {cr.get('message') or 'Error'}")

        # Instant upload if identical hash already exists on MediaFire
        if cr.get("hash_exists") == "yes" and cr.get("file_exists") != "yes":
            inst_params = {
                "session_token": tok,
                "filename": filename,
                "size": str(total_size),
                "hash": file_hash,
                "response_format": "json",
            }
            async with session.post(
                "https://www.mediafire.com/api/1.5/upload/instant.php",
                data=inst_params,
                headers={"User-Agent": DEFAULT_UA},
            ) as iresp:
                idata = await iresp.json(content_type=None)
            ir = idata.get("response") or {}
            qk = str(ir.get("quickkey") or "")
            if qk:
                progress_cb(UploadProgress(total_size, total_size, 0.0, 0, "uploading"))
                return f"https://www.mediafire.com/file/{qk}/{urllib.parse.quote(filename)}/file", updated

        upload_key = ""
        start_mono = time.monotonic()

        resumable_info = cr.get("resumable_upload") or {}
        unit_size = int(resumable_info.get("unit_size") or 0)
        num_units = int(resumable_info.get("number_of_units") or 0)

        if total_size > self.SIMPLE_THRESHOLD and unit_size > 0 and num_units > 0:
            uploaded = 0
            with open(local_path, "rb") as f:
                for uid in range(num_units):
                    if cancel_event.is_set():
                        raise UploadCancelledError("Subida pausada o detenida.")
                    chunk = f.read(unit_size)
                    if not chunk:
                        break
                    uhash = hashlib.sha256(chunk).hexdigest()
                    chunk_t0 = time.monotonic()
                    uheaders = {
                        "User-Agent": DEFAULT_UA,
                        "Content-Type": "application/octet-stream",
                        "x-filename": urllib.parse.quote(filename),
                        "x-filesize": str(total_size),
                        "x-filehash": file_hash,
                        "x-unit-id": str(uid),
                        "x-unit-size": str(len(chunk)),
                        "x-unit-hash": uhash,
                    }
                    async with session.post(
                        "https://www.mediafire.com/api/1.5/upload/resumable.php",
                        params={"session_token": tok, "response_format": "json"},
                        headers=uheaders,
                        data=chunk,
                    ) as uresp:
                        udata = await uresp.json(content_type=None)
                    ur = udata.get("response") or {}
                    if ur.get("result") != "Success":
                        raise UploadError(f"Error en bloque {uid} de Mediafire: {ur.get('message') or 'Error'}")
                    uk = (ur.get("doupload") or {}).get("key")
                    if uk:
                        upload_key = str(uk)

                    uploaded += len(chunk)
                    await _throttle_speed(len(chunk), time.monotonic() - chunk_t0, get_speed_limit_kbps())
                    spd = uploaded / max(0.05, time.monotonic() - start_mono)
                    progress_cb(UploadProgress(uploaded, total_size, spd, int((total_size - uploaded) / spd) if spd > 0 else 0, "uploading"))
        else:
            # Streaming simple upload with live progress
            async def _file_stream():
                sent = 0
                with open(local_path, "rb") as f:
                    while True:
                        if cancel_event.is_set():
                            raise UploadCancelledError("Subida pausada o detenida.")
                        t0 = time.monotonic()
                        blk = f.read(256 * 1024)
                        if not blk:
                            break
                        yield blk
                        sent += len(blk)
                        await _throttle_speed(len(blk), time.monotonic() - t0, get_speed_limit_kbps())
                        spd = sent / max(0.05, time.monotonic() - start_mono)
                        progress_cb(UploadProgress(sent, total_size, spd, int((total_size - sent) / spd) if spd > 0 else 0, "uploading"))

            sheaders = {
                "User-Agent": DEFAULT_UA,
                "Content-Type": "application/octet-stream",
                "Content-Length": str(total_size),
                "x-filename": urllib.parse.quote(filename),
                "x-filesize": str(total_size),
                "x-filehash": file_hash,
            }
            async with session.post(
                "https://www.mediafire.com/api/1.5/upload/simple.php",
                params={"session_token": tok, "response_format": "json"},
                headers=sheaders,
                data=_file_stream(),
            ) as sresp:
                sdata = await sresp.json(content_type=None)
            sr = sdata.get("response") or {}
            if sr.get("result") != "Success":
                raise UploadError(f"Mediafire rechazó la subida: {sr.get('message') or 'Error'}")
            upload_key = str((sr.get("doupload") or {}).get("key") or "")

        share_url = ""
        if upload_key:
            progress_cb(UploadProgress(total_size, total_size, 0.0, 0, "finalizing"))
            for _ in range(30):
                if cancel_event.is_set():
                    raise UploadCancelledError("Subida cancelada.")
                await asyncio.sleep(1.0)
                async with session.get(
                    "https://www.mediafire.com/api/1.5/upload/poll_upload.php",
                    params={"key": upload_key, "response_format": "json"},
                    headers={"User-Agent": DEFAULT_UA},
                ) as presp:
                    pdata = await presp.json(content_type=None)
                du = (pdata.get("response") or {}).get("doupload") or {}
                if str(du.get("status")) == "99":
                    qk = str(du.get("quickkey") or "")
                    if qk:
                        share_url = f"https://www.mediafire.com/file/{qk}/{urllib.parse.quote(filename)}/file"
                    break

        return share_url, updated

    async def list_folder(
        self,
        session: aiohttp.ClientSession,
        creds: Dict[str, Any],
        folder_id: str = "",
        folder_path: str = "",
    ) -> tuple[List[CloudEntry], Dict[str, Any]]:
        _, updated = await self.authenticate(session, creds)
        tok = updated["session_token"]
        fkey = folder_id.strip() or "myfiles"
        base_p = folder_path.strip("/")
        entries: List[CloudEntry] = []

        for ctype in ("folders", "files"):
            params = {
                "session_token": tok,
                "folder_key": fkey,
                "content_type": ctype,
                "chunk": "1",
                "response_format": "json",
            }
            async with session.get(
                "https://www.mediafire.com/api/1.5/folder/get_content.php",
                params=params,
                headers={"User-Agent": DEFAULT_UA},
            ) as resp:
                data = await resp.json(content_type=None)
            r = data.get("response") or {}
            if r.get("result") != "Success":
                raise UploadError(f"Error al listar Mediafire: {r.get('message') or 'Error'}")
            fc = r.get("folder_content") or {}
            if ctype == "folders":
                for f in fc.get("folders") or []:
                    name = str(f.get("name") or "")
                    fk = str(f.get("folderkey") or "")
                    entries.append(
                        CloudEntry(
                            id=fk,
                            name=name,
                            is_folder=True,
                            modified=str(f.get("created") or "")[:16],
                            path=f"{base_p}/{name}" if base_p else name,
                            provider="mediafire",
                        )
                    )
            else:
                for f in fc.get("files") or []:
                    name = str(f.get("filename") or "")
                    qk = str(f.get("quickkey") or "")
                    links = f.get("links") or {}
                    entries.append(
                        CloudEntry(
                            id=qk,
                            name=name,
                            is_folder=False,
                            size=int(f.get("size") or 0),
                            modified=str(f.get("created") or "")[:16],
                            path=f"{base_p}/{name}" if base_p else name,
                            web_url=str(links.get("normal_download") or f"https://www.mediafire.com/file/{qk}/{urllib.parse.quote(name)}/file"),
                            provider="mediafire",
                        )
                    )
        entries.sort(key=lambda e: (not e.is_folder, e.name.lower()))
        return entries, updated

    async def create_folder(
        self,
        session: aiohttp.ClientSession,
        creds: Dict[str, Any],
        parent_id: str = "",
        parent_path: str = "",
        name: str = "Nueva carpeta",
    ) -> tuple[CloudEntry, Dict[str, Any]]:
        _, updated = await self.authenticate(session, creds)
        tok = updated["session_token"]
        clean_name = name.strip() or "Nueva carpeta"
        params = {
            "session_token": tok,
            "foldername": clean_name,
            "response_format": "json",
        }
        if parent_id.strip() and parent_id.strip() != "myfiles":
            params["parent_key"] = parent_id.strip()
        async with session.post(
            "https://www.mediafire.com/api/1.5/folder/create.php",
            data=params,
            headers={"User-Agent": DEFAULT_UA},
        ) as resp:
            data = await resp.json(content_type=None)
        r = data.get("response") or {}
        if r.get("result") != "Success":
            raise UploadError(f"No se pudo crear carpeta en Mediafire: {r.get('message') or 'Error'}")
        fk = str(r.get("folder_key") or "")
        base_p = parent_path.strip("/")
        return (
            CloudEntry(
                id=fk,
                name=str(r.get("name") or clean_name),
                is_folder=True,
                path=f"{base_p}/{clean_name}" if base_p else clean_name,
                provider="mediafire",
            ),
            updated,
        )

    async def get_download_request(
        self,
        session: aiohttp.ClientSession,
        creds: Dict[str, Any],
        entry: CloudEntry,
    ) -> tuple[str, Dict[str, str], Dict[str, Any]]:
        _, updated = await self.authenticate(session, creds)
        tok = updated["session_token"]
        params = {
            "session_token": tok,
            "quick_key": entry.id,
            "link_type": "direct_download",
            "response_format": "json",
        }
        async with session.get(
            "https://www.mediafire.com/api/1.5/file/get_links.php",
            params=params,
            headers={"User-Agent": DEFAULT_UA},
        ) as resp:
            data = await resp.json(content_type=None)
        links = (data.get("response") or {}).get("links") or []
        if links and isinstance(links, list):
            direct = str(links[0].get("direct_download") or links[0].get("normal_download") or "")
            if direct:
                return direct, {}, updated
        if entry.web_url:
            return entry.web_url, {}, updated
        raise UploadError("No se pudo obtener el enlace de descarga de Mediafire.")


_UPLOADERS: Dict[str, BaseCloudUploader] = {
    "gdrive": GDriveUploader(),
    "mega": MegaUploader(),
    "onedrive": OneDriveUploader(),
    "dropbox": DropboxUploader(),
    "mediafire": MediafireUploader(),
}


def get_uploader_for_provider(provider: str) -> BaseCloudUploader:
    up = _UPLOADERS.get(provider)
    if not up:
        raise UploadError(f"Proveedor de subida no soportado: {provider}")
    return up
