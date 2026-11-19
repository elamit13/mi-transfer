"""
Core Asynchronous Upload Engine for Mint Uploader.
Manages multi-account cloud synchronization (GDrive, MEGA, OneDrive, Dropbox, Mediafire),
persistent AES-256-GCM encrypted sessions, concurrent chunked uploads, pause/resume/stop,
speed throttling, and SQLite WAL queue persistence.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiohttp

from .account_vault import (
    PROVIDER_DISPLAY_NAMES,
    AccountVault,
    CloudAccount,
)
from .uploaders import (
    CloudEntry,
    UploadAuthError,
    UploadCancelledError,
    UploadError,
    UploadProgress,
    get_uploader_for_provider,
    run_browser_oauth2_flow,
    run_rclone_authorize,
    run_web_login_window,
    shutdown_web_list_daemons,
)


UPLOAD_STATUS_LABELS_ES: Dict[str, str] = {
    "queued": "En cola",
    "authenticating": "Autenticando...",
    "uploading": "Subiendo",
    "finalizing": "Finalizando...",
    "paused": "Pausado",
    "stopped": "Detenido",
    "complete": "Completado",
    "error": "Error",
}

_SPLIT_ARCH_RE = re.compile(r"(\.(?:zip|rar|7z)\.\d{3})$")
_TAR_EXT_RE = re.compile(r"(\.tar\.(?:gz|bz2|xz|zst))$")


@dataclass
class UploadItem:
    id: int
    local_path: str
    filename: str
    account_id: int
    provider: str
    account_label: str
    remote_folder: str = ""
    total_size: int = 0
    uploaded_size: int = 0
    speed: float = 0.0
    eta: int = 0
    status: str = "queued"
    share_url: str = ""
    error_msg: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)

    @property
    def progress_fraction(self) -> float:
        if self.status == "complete":
            return 1.0
        if self.total_size <= 0:
            return 0.0
        return min(1.0, max(0.0, self.uploaded_size / self.total_size))

    @property
    def progress_percent(self) -> float:
        return self.progress_fraction * 100.0

    @property
    def file_extension(self) -> str:
        name_lower = (self.filename or "").lower().strip()
        if "." in name_lower:
            m_split = _SPLIT_ARCH_RE.search(name_lower)
            if m_split:
                return m_split.group(1)
            m_tar = _TAR_EXT_RE.search(name_lower)
            if m_tar:
                return m_tar.group(1)
            _, ext = os.path.splitext(name_lower)
            if ext and len(ext) <= 10:
                return ext
        return "—"

    @property
    def destination_display(self) -> str:
        pname = PROVIDER_DISPLAY_NAMES.get(self.provider, self.provider)
        lbl = (self.account_label or "").strip()
        if lbl and lbl.lower() != pname.lower():
            return f"{pname} ({lbl})"
        return pname

    @property
    def status_display(self) -> str:
        base = UPLOAD_STATUS_LABELS_ES.get(self.status, self.status)
        if self.status == "error" and self.error_msg:
            return f"Error: {self.error_msg[:80]}"
        return base


class UploadEngine:
    """Asynchronous multi-account upload orchestrator backed by SQLite WAL and AES-256-GCM Vault."""

    def __init__(self, db_path: Optional[str] = None, max_concurrent: int = 3) -> None:
        config_dir = Path.home() / ".config" / "mint-downloader"
        config_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path or str(config_dir / "downloads.db")

        self.vault = AccountVault(self.db_path)
        self.max_concurrent = max_concurrent
        self.speed_limit_kbps = 0

        self.items: Dict[int, UploadItem] = {}
        self.state_version: int = 0
        self.accounts_version: int = 0
        self.total_speed_bps: float = 0.0
        self.active_count: int = 0

        self._tasks: Dict[int, asyncio.Task] = {}
        self._cancel_events: Dict[int, asyncio.Event] = {}
        self._folder_cache: Dict[tuple[int, str, str], tuple[float, List[CloudEntry]]] = {}
        self._folder_cache_ttl: float = 90.0
        self._session: Optional[aiohttp.ClientSession] = None
        self._running = False
        self._db_lock = threading.Lock()
        self._db_conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._db_conn.row_factory = sqlite3.Row

        self._init_db()
        self._load_state_from_db()

    def _bump_version(self) -> None:
        self.state_version += 1

    def _bump_accounts(self) -> None:
        self.accounts_version += 1
        self.state_version += 1

    def _recompute_stats(self) -> None:
        total_spd = 0.0
        act = 0
        for it in self.items.values():
            st = it.status
            if st == "uploading":
                total_spd += it.speed
            if st in ("authenticating", "uploading", "finalizing"):
                act += 1
        self.total_speed_bps = total_spd
        self.active_count = act

    def _init_db(self) -> None:
        with self._db_lock, self._db_conn as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA temp_store=MEMORY")
            conn.execute("PRAGMA cache_size=-4000")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS uploads (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    local_path TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    account_id INTEGER NOT NULL,
                    provider TEXT NOT NULL,
                    account_label TEXT DEFAULT '',
                    remote_folder TEXT DEFAULT '',
                    total_size INTEGER DEFAULT 0,
                    uploaded_size INTEGER DEFAULT 0,
                    status TEXT DEFAULT 'queued',
                    share_url TEXT DEFAULT '',
                    error_msg TEXT DEFAULT '',
                    extra_json TEXT DEFAULT '{}',
                    created_at REAL DEFAULT 0,
                    updated_at REAL DEFAULT 0
                )
                """
            )

    def _load_state_from_db(self) -> None:
        with self._db_lock, self._db_conn as conn:
            for row in conn.execute("SELECT key, value FROM settings"):
                k, v = row["key"], row["value"]
                if k == "upload_max_concurrent":
                    try:
                        self.max_concurrent = max(1, min(16, int(v)))
                    except ValueError:
                        pass
                elif k == "upload_speed_limit_kbps":
                    try:
                        self.speed_limit_kbps = max(0, int(v))
                    except ValueError:
                        pass

            for row in conn.execute(
                "SELECT id, local_path, filename, account_id, provider, account_label, "
                "remote_folder, total_size, uploaded_size, status, share_url, error_msg, extra_json "
                "FROM uploads ORDER BY id ASC"
            ):
                st = row["status"]
                if st in ("authenticating", "uploading", "finalizing"):
                    st = "paused"
                item = UploadItem(
                    id=int(row["id"]),
                    local_path=str(row["local_path"]),
                    filename=str(row["filename"]),
                    account_id=int(row["account_id"]),
                    provider=str(row["provider"]),
                    account_label=str(row["account_label"] or ""),
                    remote_folder=str(row["remote_folder"] or ""),
                    total_size=int(row["total_size"] or 0),
                    uploaded_size=int(row["uploaded_size"] or 0),
                    status=st,
                    share_url=str(row["share_url"] or ""),
                    error_msg=str(row["error_msg"] or ""),
                    extra=json.loads(row["extra_json"] or "{}"),
                )
                self.items[item.id] = item
        self._bump_version()

    def _save_setting(self, key: str, value: str) -> None:
        with self._db_lock, self._db_conn as conn:
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)),
            )

    def _insert_item_db(self, item: UploadItem) -> int:
        now = time.time()
        with self._db_lock, self._db_conn as conn:
            cur = conn.execute(
                """
                INSERT INTO uploads (
                    local_path, filename, account_id, provider, account_label,
                    remote_folder, total_size, uploaded_size, status, share_url,
                    error_msg, extra_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.local_path,
                    item.filename,
                    item.account_id,
                    item.provider,
                    item.account_label,
                    item.remote_folder,
                    item.total_size,
                    item.uploaded_size,
                    item.status,
                    item.share_url,
                    item.error_msg,
                    json.dumps(item.extra),
                    now,
                    now,
                ),
            )
            return int(cur.lastrowid)

    def _update_item_db(self, item: UploadItem) -> None:
        now = time.time()
        with self._db_lock, self._db_conn as conn:
            conn.execute(
                """
                UPDATE uploads SET
                    uploaded_size=?, total_size=?, status=?, share_url=?,
                    error_msg=?, extra_json=?, updated_at=?
                WHERE id=?
                """,
                (
                    item.uploaded_size,
                    item.total_size,
                    item.status,
                    item.share_url,
                    item.error_msg,
                    json.dumps(item.extra),
                    now,
                    item.id,
                ),
            )

    def _delete_item_db(self, item_id: int) -> None:
        with self._db_lock, self._db_conn as conn:
            conn.execute("DELETE FROM uploads WHERE id=?", (item_id,))

    async def start(self) -> None:
        if self._running:
            return
        connector = aiohttp.TCPConnector(limit=24, ttl_dns_cache=300)
        self._session = aiohttp.ClientSession(
            connector=connector,
            timeout=aiohttp.ClientTimeout(total=None, sock_connect=25, sock_read=120),
        )
        self._running = True

    async def shutdown(self) -> None:
        self._running = False
        for ev in list(self._cancel_events.values()):
            ev.set()

        now = time.time()
        paused_rows = []
        for item in self.items.values():
            if item.status in ("authenticating", "uploading", "finalizing"):
                item.status = "paused"
                item.speed = 0.0
                paused_rows.append(
                    (
                        item.uploaded_size,
                        item.total_size,
                        item.status,
                        item.share_url,
                        item.error_msg,
                        json.dumps(item.extra),
                        now,
                        item.id,
                    )
                )
        if paused_rows:
            with self._db_lock, self._db_conn as conn:
                conn.executemany(
                    "UPDATE uploads SET uploaded_size=?, total_size=?, status=?, share_url=?, "
                    "error_msg=?, extra_json=?, updated_at=? WHERE id=?",
                    paused_rows,
                )

        await shutdown_web_list_daemons()

        if self._session:
            await self._session.close()
            self._session = None

        try:
            with self._db_lock:
                self._db_conn.close()
        except Exception:
            pass
        self.vault.close()

    def _get_session(self) -> aiohttp.ClientSession:
        if not self._session or self._session.closed:
            connector = aiohttp.TCPConnector(limit=24, ttl_dns_cache=300, enable_cleanup_closed=True)
            self._session = aiohttp.ClientSession(
                connector=connector,
                timeout=aiohttp.ClientTimeout(total=None, sock_connect=25, sock_read=120),
            )
        return self._session

    # ------------------------------------------------------------------
    # Account Synchronization & Management
    # ------------------------------------------------------------------

    def list_accounts(self) -> List[CloudAccount]:
        return self.vault.list_accounts()

    async def sync_web_login_account(
        self,
        provider: str,
        alias: str = "",
        share_with_downloader: bool = True,
        account_id: Optional[int] = None,
    ) -> CloudAccount:
        """
        Open a dedicated native login window (WebKit2) for the selected provider,
        persist the full browser profile on disk, extract cookies + web session tokens,
        and save them permanently for both Mint Uploader and Mint Downloader.
        """
        existing = self.vault.get_account(account_id) if account_id is not None else None
        profile_dir = (
            existing.web_profile_dir
            if existing and existing.web_profile_dir and os.path.isdir(existing.web_profile_dir)
            else self.vault.allocate_web_profile_dir(provider)
        )

        captured = await run_web_login_window(
            provider=provider,
            profile_dir=profile_dir,
            silent_refresh=False,
        )

        cookies = captured.get("cookies") if isinstance(captured.get("cookies"), list) else []
        tokens = captured.get("tokens") if isinstance(captured.get("tokens"), dict) else {}
        if not cookies and not tokens:
            raise UploadAuthError(
                "No se detectaron cookies ni sesión iniciada. Inicia sesión antes de pulsar «Guardar sesión»."
            )

        # Merge with existing credentials if re-authenticating so OAuth tokens aren't lost
        creds: Dict[str, Any] = dict(existing.creds) if existing else {}
        if not creds.get("refresh_token"):
            creds["auth_type"] = "web_session"
        creds["web_profile_dir"] = profile_dir
        creds["cookies"] = cookies
        creds["login_url"] = str(captured.get("current_url") or "")
        creds["captured_at"] = time.time()

        # Copy extracted provider tokens (MEGA sid/k, OneDrive MSAL, MediaFire session_token, etc.)
        for k, v in tokens.items():
            if v:
                creds[k] = v

        detected_email = str(captured.get("account_email") or "").strip()
        detected_name = str(captured.get("account_name") or "").strip()

        # Optional live verification/enrichment for providers with extracted API tokens
        try:
            if (
                (provider == "mega" and creds.get("sid") and (creds.get("master_key_b64") or creds.get("master_key_hex")))
                or (provider == "mediafire" and (creds.get("session_token") or cookies))
                or (provider == "onedrive" and creds.get("access_token"))
            ):
                uploader = get_uploader_for_provider(provider)
                v_email, enriched = await uploader.authenticate(self._get_session(), creds)
                creds = enriched
                if v_email and not detected_email:
                    detected_email = v_email
        except Exception:
            # Keep captured web session cookies safely even if API verification needs a follow-up
            pass

        final_email = detected_email or (existing.account_email if existing else "")
        if final_email:
            creds["account_email"] = final_email
        if detected_name:
            creds["detected_name"] = detected_name

        # Prefer explicit alias -> detected account name -> email prefix -> existing -> fallback
        auto_name = detected_name
        if not auto_name and final_email and "@" in final_email:
            auto_name = final_email.split("@", 1)[0]
        final_label = (
            alias.strip()
            or auto_name
            or final_email
            or (existing.label if existing else "")
            or f"Cuenta {PROVIDER_DISPLAY_NAMES.get(provider, provider)}"
        )
        scope = "both" if share_with_downloader else "uploader"
        status_msg = (
            "Sesión guardada (Subidas + Descargas)"
            if share_with_downloader
            else "Sesión guardada permanentemente"
        )

        acc = self.vault.save_account(
            provider=provider,
            label=final_label,
            account_email=final_email,
            creds=creds,
            status="connected",
            status_msg=status_msg,
            account_id=account_id,
            session_scope=scope,
            web_profile_dir=profile_dir,
        )
        self._bump_accounts()
        return acc

    async def sync_credentials_account(
        self,
        provider: str,
        email: str,
        password: str,
        alias: str = "",
        account_id: Optional[int] = None,
    ) -> CloudAccount:
        """Authenticate MEGA or Mediafire account and persist encrypted session forever."""
        uploader = get_uploader_for_provider(provider)
        session = self._get_session()
        verified_email, creds = await uploader.authenticate(
            session,
            {"email": email.strip(), "password": password},
        )
        acc = self.vault.save_account(
            provider=provider,
            label=alias.strip() or verified_email,
            account_email=verified_email,
            creds=creds,
            status="connected",
            status_msg="Sesión guardada permanentemente",
            account_id=account_id,
        )
        self._bump_accounts()
        return acc

    async def sync_oauth_browser_account(
        self,
        provider: str,
        client_id: str,
        client_secret: str = "",
        alias: str = "",
        account_id: Optional[int] = None,
        cancel_event: Optional[asyncio.Event] = None,
    ) -> CloudAccount:
        """Run browser PKCE OAuth2 flow for GDrive, OneDrive, or Dropbox and save permanent refresh token."""
        session = self._get_session()
        raw_creds = await run_browser_oauth2_flow(
            session=session,
            provider=provider,
            client_id=client_id,
            client_secret=client_secret,
            cancel_event=cancel_event,
        )
        uploader = get_uploader_for_provider(provider)
        verified_email, updated_creds = await uploader.authenticate(session, raw_creds)
        acc = self.vault.save_account(
            provider=provider,
            label=alias.strip() or verified_email,
            account_email=verified_email,
            creds=updated_creds,
            status="connected",
            status_msg="Sesión guardada permanentemente",
            account_id=account_id,
        )
        self._bump_accounts()
        return acc

    async def sync_rclone_account(
        self,
        provider: str,
        alias: str = "",
        account_id: Optional[int] = None,
    ) -> CloudAccount:
        """Use local rclone authorize browser helper (if installed) to capture a permanent OAuth2 token."""
        raw_creds = await run_rclone_authorize(provider)
        session = self._get_session()
        uploader = get_uploader_for_provider(provider)
        verified_email, updated_creds = await uploader.authenticate(session, raw_creds)
        acc = self.vault.save_account(
            provider=provider,
            label=alias.strip() or verified_email,
            account_email=verified_email,
            creds=updated_creds,
            status="connected",
            status_msg="Sesión guardada permanentemente",
            account_id=account_id,
        )
        self._bump_accounts()
        return acc

    async def sync_manual_token_account(
        self,
        provider: str,
        access_token: str = "",
        refresh_token: str = "",
        client_id: str = "",
        client_secret: str = "",
        alias: str = "",
        account_id: Optional[int] = None,
    ) -> CloudAccount:
        """Verify and permanently save a manually supplied OAuth2 refresh/access token."""
        if not access_token.strip() and not refresh_token.strip():
            raise UploadError("Debes ingresar al menos un Refresh Token o un Access Token.")
        raw_creds = {
            "auth_type": "oauth2",
            "client_id": client_id.strip(),
            "client_secret": client_secret.strip(),
            "access_token": access_token.strip(),
            "refresh_token": refresh_token.strip(),
            "expires_at": 0.0 if refresh_token.strip() else time.time() + 3600,
        }
        session = self._get_session()
        uploader = get_uploader_for_provider(provider)
        verified_email, updated_creds = await uploader.authenticate(session, raw_creds)
        acc = self.vault.save_account(
            provider=provider,
            label=alias.strip() or verified_email,
            account_email=verified_email,
            creds=updated_creds,
            status="connected",
            status_msg="Sesión guardada permanentemente",
            account_id=account_id,
        )
        self._bump_accounts()
        return acc

    async def verify_account(self, account_id: int) -> CloudAccount:
        """Verify and refresh a saved account's permanent session silently."""
        acc = self.vault.get_account(account_id)
        if not acc:
            raise UploadError("La cuenta seleccionada no existe.")
        session = self._get_session()
        uploader = get_uploader_for_provider(acc.provider)
        try:
            verified_email, updated_creds = await uploader.authenticate(session, acc.creds)
            self.vault.update_credentials(
                account_id=acc.id,
                creds=updated_creds,
                status="connected",
                status_msg="Sesión activa",
                account_email=verified_email,
            )
            acc.account_email = verified_email
            acc.creds = updated_creds
            acc.status = "connected"
            acc.status_msg = "Sesión activa"
            self._bump_accounts()
            return acc
        except UploadAuthError as exc:
            self.vault.update_status(acc.id, "reauth_required", str(exc))
            self._bump_accounts()
            raise

    def delete_account(self, account_id: int) -> None:
        self.invalidate_folder_cache(account_id)
        self.vault.delete_account(account_id)
        self._bump_accounts()

    def rename_account(self, account_id: int, new_label: str) -> None:
        self.vault.rename_account(account_id, new_label)
        self._bump_accounts()

    # ------------------------------------------------------------------
    # Cloud Explorer (Native & Web Interface Synchronization + Fast Cache)
    # ------------------------------------------------------------------

    def get_cached_folder(
        self,
        account_id: int,
        folder_id: str = "",
        folder_path: str = "",
    ) -> Optional[tuple[List[CloudEntry], float]]:
        """Return (entries, age_seconds) from in-memory cache if still valid, else None."""
        key = (int(account_id), folder_id.strip(), folder_path.strip("/"))
        hit = self._folder_cache.get(key)
        if not hit:
            return None
        ts, entries = hit
        age = time.monotonic() - ts
        if age > self._folder_cache_ttl:
            self._folder_cache.pop(key, None)
            return None
        return list(entries), age

    def invalidate_folder_cache(self, account_id: Optional[int] = None) -> None:
        if account_id is None:
            self._folder_cache.clear()
            return
        aid = int(account_id)
        for k in [k for k in self._folder_cache if k[0] == aid]:
            self._folder_cache.pop(k, None)

    def get_default_remote_folder(self, account_id: int) -> str:
        key = f"upload_default_folder_{int(account_id)}"
        with self._db_lock, self._db_conn as conn:
            row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return str(row["value"]).strip("/") if row and row["value"] else ""

    def set_default_remote_folder(self, account_id: int, folder_path: str) -> None:
        key = f"upload_default_folder_{int(account_id)}"
        self._save_setting(key, folder_path.strip().strip("/\\"))

    async def list_remote_folder(
        self,
        account_id: int,
        folder_id: str = "",
        folder_path: str = "",
        force_refresh: bool = False,
    ) -> List[CloudEntry]:
        """List remote folders and files inside the chosen cloud account (cached for instant navigation)."""
        cache_key = (int(account_id), folder_id.strip(), folder_path.strip("/"))
        if not force_refresh:
            cached = self.get_cached_folder(account_id, folder_id, folder_path)
            if cached is not None:
                return cached[0]

        acc = self.vault.get_account(account_id)
        if not acc:
            raise UploadError("La cuenta seleccionada ya no existe.")
        uploader = get_uploader_for_provider(acc.provider)
        session = self._get_session()
        try:
            entries, updated_creds = await uploader.list_folder(
                session=session,
                creds=acc.creds,
                folder_id=folder_id,
                folder_path=folder_path,
            )
            self._folder_cache[cache_key] = (time.monotonic(), list(entries))
            det_name = str(updated_creds.get("detected_name") or "").strip()
            det_email = str(updated_creds.get("account_email") or "").strip()
            pname = PROVIDER_DISPLAY_NAMES.get(acc.provider, acc.provider)
            is_generic_label = acc.label.strip().lower() in (
                "",
                pname.lower(),
                f"cuenta {pname.lower()}",
                "cuenta",
            )
            new_label: Optional[str] = None
            if is_generic_label:
                if det_name:
                    new_label = det_name
                elif det_email and "@" in det_email:
                    new_label = det_email.split("@", 1)[0]

            if updated_creds != acc.creds or acc.status != "connected" or new_label or (det_email and det_email != acc.account_email):
                self.vault.update_credentials(
                    acc.id,
                    updated_creds,
                    status="connected",
                    status_msg="Sesión activa",
                    account_email=det_email or acc.account_email,
                    label=new_label,
                )
                self._bump_accounts()
            return entries
        except UploadAuthError as exc:
            self.vault.update_status(acc.id, "reauth_required", str(exc))
            self._bump_accounts()
            raise

    async def create_remote_folder(
        self,
        account_id: int,
        parent_id: str = "",
        parent_path: str = "",
        name: str = "Nueva carpeta",
    ) -> CloudEntry:
        """Create a new remote folder inside the chosen cloud account."""
        acc = self.vault.get_account(account_id)
        if not acc:
            raise UploadError("La cuenta seleccionada ya no existe.")
        uploader = get_uploader_for_provider(acc.provider)
        session = self._get_session()
        entry, updated_creds = await uploader.create_folder(
            session=session,
            creds=acc.creds,
            parent_id=parent_id,
            parent_path=parent_path,
            name=name,
        )
        self.invalidate_folder_cache(acc.id)
        if updated_creds != acc.creds:
            self.vault.update_credentials(acc.id, updated_creds, status="connected", status_msg="Sesión activa")
            self._bump_accounts()
        return entry

    async def resolve_remote_download(
        self,
        account_id: int,
        entry: CloudEntry,
    ) -> tuple[str, Dict[str, str]]:
        """Resolve a direct/authenticated download URL and headers for MintTransfer Downloader."""
        acc = self.vault.get_account(account_id)
        if not acc:
            raise UploadError("La cuenta seleccionada ya no existe.")
        uploader = get_uploader_for_provider(acc.provider)
        session = self._get_session()
        url, headers, updated_creds = await uploader.get_download_request(
            session=session,
            creds=acc.creds,
            entry=entry,
        )
        if updated_creds != acc.creds:
            self.vault.update_credentials(acc.id, updated_creds, status="connected", status_msg="Sesión activa")
            self._bump_accounts()
        return url, headers

    async def open_web_interface(self, account_id: int) -> Optional[CloudAccount]:
        """Open the cloud provider's live web interface using the account's persistent profile."""
        acc = self.vault.get_account(account_id)
        if not acc:
            raise UploadError("La cuenta seleccionada ya no existe.")
        profile_dir = (
            acc.web_profile_dir
            if acc.web_profile_dir and os.path.isdir(acc.web_profile_dir)
            else self.vault.allocate_web_profile_dir(acc.provider)
        )
        try:
            captured = await run_web_login_window(
                provider=acc.provider,
                profile_dir=profile_dir,
                silent_refresh=False,
                browse_mode=True,
                timeout_sec=3600.0,
            )
        except UploadCancelledError:
            # User simply closed the web explorer window; still persist profile_dir if newly allocated
            if not acc.web_profile_dir:
                creds = dict(acc.creds)
                creds["web_profile_dir"] = profile_dir
                self.vault.update_credentials(acc.id, creds, web_profile_dir=profile_dir)
                self._bump_accounts()
            return acc

        creds = dict(acc.creds)
        creds["web_profile_dir"] = profile_dir
        if isinstance(captured.get("cookies"), list) and captured["cookies"]:
            creds["cookies"] = captured["cookies"]
        tokens = captured.get("tokens") if isinstance(captured.get("tokens"), dict) else {}
        for k, v in tokens.items():
            if v:
                creds[k] = v
        email = str(captured.get("account_email") or acc.account_email or "").strip()
        det_name = str(captured.get("account_name") or "").strip()
        if email:
            creds["account_email"] = email
        if det_name:
            creds["detected_name"] = det_name

        pname = PROVIDER_DISPLAY_NAMES.get(acc.provider, acc.provider)
        is_generic = acc.label.strip().lower() in (
            "",
            pname.lower(),
            f"cuenta {pname.lower()}",
            "cuenta",
        )
        new_label: Optional[str] = None
        if is_generic:
            if det_name:
                new_label = det_name
            elif email and "@" in email:
                new_label = email.split("@", 1)[0]

        self.vault.update_credentials(
            acc.id,
            creds,
            status="connected",
            status_msg="Sesión sincronizada (Subidas + Descargas)" if acc.shared_with_downloader else "Sesión activa",
            account_email=email,
            web_profile_dir=profile_dir,
            label=new_label,
        )
        self._bump_accounts()
        return self.vault.get_account(acc.id)

    # ------------------------------------------------------------------
    # Upload Queue Management
    # ------------------------------------------------------------------

    def set_max_concurrent(self, limit: int) -> None:
        self.max_concurrent = max(1, min(16, int(limit)))
        self._save_setting("upload_max_concurrent", str(self.max_concurrent))
        self._bump_version()

    def set_speed_limit_kbps(self, kbps: int) -> None:
        self.speed_limit_kbps = max(0, int(kbps))
        self._save_setting("upload_speed_limit_kbps", str(self.speed_limit_kbps))

    async def add_files(
        self,
        file_paths: List[str],
        account_ids: List[int],
        remote_folder: str = "",
        auto_start: bool = True,
        remote_folder_id: str = "",
    ) -> List[UploadItem]:
        added: List[UploadItem] = []
        accounts = [self.vault.get_account(aid) for aid in account_ids]
        valid_accounts = [a for a in accounts if a is not None]
        if not valid_accounts:
            return added

        clean_folder = remote_folder.strip().strip("/\\")
        clean_fid = remote_folder_id.strip()
        for fpath in file_paths:
            if not fpath or not os.path.isfile(fpath):
                continue
            try:
                fsize = os.path.getsize(fpath)
            except OSError:
                continue
            fname = os.path.basename(fpath)
            for acc in valid_accounts:
                extra_data: Dict[str, Any] = {}
                if clean_fid:
                    extra_data["remote_folder_id"] = clean_fid
                item = UploadItem(
                    id=0,
                    local_path=os.path.abspath(fpath),
                    filename=fname,
                    account_id=acc.id,
                    provider=acc.provider,
                    account_label=acc.label or acc.account_email,
                    remote_folder=clean_folder,
                    total_size=fsize,
                    uploaded_size=0,
                    status="queued",
                    extra=extra_data,
                )
                item.id = self._insert_item_db(item)
                self.items[item.id] = item
                added.append(item)

        if added:
            self._bump_version()
            if auto_start:
                self._schedule_queue()
        return added

    async def add_folder(
        self,
        folder_path: str,
        account_ids: List[int],
        remote_folder: str = "",
        auto_start: bool = True,
    ) -> List[UploadItem]:
        if not folder_path or not os.path.isdir(folder_path):
            return []
        root_name = os.path.basename(os.path.normpath(folder_path))
        base_remote = f"{remote_folder.strip('/\\')}/{root_name}" if remote_folder.strip("/\\") else root_name
        added_all: List[UploadItem] = []
        for dirpath, _dirnames, filenames in os.walk(folder_path):
            rel = os.path.relpath(dirpath, folder_path)
            sub_remote = base_remote if rel == "." else f"{base_remote}/{rel.replace(os.sep, '/')}"
            full_paths = [os.path.join(dirpath, fn) for fn in sorted(filenames)]
            if full_paths:
                added_all.extend(
                    await self.add_files(full_paths, account_ids, remote_folder=sub_remote, auto_start=False)
                )
        if added_all and auto_start:
            self._schedule_queue()
        return added_all

    def _active_running_count(self) -> int:
        return sum(1 for t in self._tasks.values() if not t.done())

    def _schedule_queue(self) -> None:
        if not self._running:
            return
        while self._active_running_count() < self.max_concurrent:
            next_item = next(
                (
                    it
                    for it in self.items.values()
                    if it.status == "queued" and (it.id not in self._tasks or self._tasks[it.id].done())
                ),
                None,
            )
            if not next_item:
                break
            self._spawn_upload_task(next_item)

    def _spawn_upload_task(self, item: UploadItem) -> None:
        cancel_ev = asyncio.Event()
        self._cancel_events[item.id] = cancel_ev
        item.status = "authenticating"
        item.error_msg = ""
        self._update_item_db(item)
        self._recompute_stats()
        self._bump_version()

        async def _runner() -> None:
            try:
                if not os.path.isfile(item.local_path):
                    raise UploadError("El archivo local ya no existe en disco.")
                acc = self.vault.get_account(item.account_id)
                if not acc:
                    raise UploadAuthError("La cuenta destino fue eliminada. Selecciona otra cuenta.")

                item.total_size = os.path.getsize(item.local_path)
                uploader = get_uploader_for_provider(acc.provider)
                session = self._get_session()

                def _on_prog(p: UploadProgress) -> None:
                    item.uploaded_size = p.uploaded_bytes
                    if p.total_bytes > 0:
                        item.total_size = p.total_bytes
                    item.speed = p.speed
                    item.eta = p.eta
                    if p.status in ("uploading", "finalizing"):
                        item.status = p.status
                    self._recompute_stats()
                    self._bump_version()

                share_url, updated_creds = await uploader.upload_file(
                    session=session,
                    creds=acc.creds,
                    local_path=item.local_path,
                    filename=item.filename,
                    remote_folder=item.remote_folder,
                    extra=item.extra,
                    progress_cb=_on_prog,
                    cancel_event=cancel_ev,
                    get_speed_limit_kbps=lambda: self.speed_limit_kbps,
                )

                # Persist refreshed tokens/sessions automatically so login lasts forever
                if updated_creds != acc.creds or acc.status != "connected":
                    self.vault.update_credentials(acc.id, updated_creds, status="connected", status_msg="Sesión activa")
                    self._bump_accounts()

                item.share_url = share_url or ""
                item.status = "complete"
                item.uploaded_size = item.total_size
                item.speed = 0.0
                item.eta = 0
                item.error_msg = ""
                self._update_item_db(item)
                self.invalidate_folder_cache(item.account_id)
            except UploadCancelledError:
                if item.status not in ("paused", "stopped"):
                    item.status = "paused"
                item.speed = 0.0
                item.eta = 0
                self._update_item_db(item)
            except UploadAuthError as exc:
                item.status = "error"
                item.error_msg = str(exc)
                item.speed = 0.0
                item.eta = 0
                self.vault.update_status(item.account_id, "reauth_required", str(exc))
                self._bump_accounts()
                self._update_item_db(item)
            except Exception as exc:
                item.status = "error"
                item.error_msg = str(exc)
                item.speed = 0.0
                item.eta = 0
                self._update_item_db(item)
            finally:
                self._tasks.pop(item.id, None)
                self._cancel_events.pop(item.id, None)
                self._recompute_stats()
                self._bump_version()
                self._schedule_queue()

        self._tasks[item.id] = asyncio.create_task(_runner())

    async def start_task(self, item_id: int) -> None:
        item = self.items.get(item_id)
        if not item or item.status == "complete":
            return
        if item.id in self._tasks and not self._tasks[item.id].done():
            return
        item.status = "queued"
        item.error_msg = ""
        self._update_item_db(item)
        self._bump_version()
        self._schedule_queue()

    async def pause_task(self, item_id: int) -> None:
        item = self.items.get(item_id)
        if not item or item.status not in ("queued", "authenticating", "uploading", "finalizing"):
            return
        item.status = "paused"
        item.speed = 0.0
        item.eta = 0
        ev = self._cancel_events.get(item.id)
        if ev:
            ev.set()
        self._update_item_db(item)
        self._recompute_stats()
        self._bump_version()

    async def stop_task(self, item_id: int) -> None:
        item = self.items.get(item_id)
        if not item or item.status == "complete":
            return
        item.status = "stopped"
        item.speed = 0.0
        item.eta = 0
        item.uploaded_size = 0
        item.extra.pop("upload_session_uri", None)
        item.extra.pop("dropbox_session_id", None)
        item.extra.pop("dropbox_offset", None)
        ev = self._cancel_events.get(item.id)
        if ev:
            ev.set()
        self._update_item_db(item)
        self._recompute_stats()
        self._bump_version()

    async def remove_task(self, item_id: int) -> None:
        item = self.items.get(item_id)
        if not item:
            return
        await self.stop_task(item_id)
        self._tasks.pop(item_id, None)
        self._cancel_events.pop(item_id, None)
        self.items.pop(item_id, None)
        self._delete_item_db(item_id)
        self._recompute_stats()
        self._bump_version()

    async def clear_completed(self) -> int:
        completed_ids = [iid for iid, it in list(self.items.items()) if it.status == "complete"]
        if not completed_ids:
            return 0
        for iid in completed_ids:
            self.items.pop(iid, None)
        with self._db_lock, self._db_conn as conn:
            conn.executemany("DELETE FROM uploads WHERE id=?", [(iid,) for iid in completed_ids])
        self._bump_version()
        return len(completed_ids)

    async def start_all(self) -> None:
        for item in list(self.items.values()):
            if item.status in ("queued", "paused", "stopped", "error"):
                item.status = "queued"
                item.error_msg = ""
                self._update_item_db(item)
                self.invalidate_folder_cache(item.account_id)
        self._bump_version()
        self._schedule_queue()

    async def pause_all(self) -> None:
        for item_id, item in list(self.items.items()):
            if item.status in ("queued", "authenticating", "uploading", "finalizing"):
                await self.pause_task(item_id)
