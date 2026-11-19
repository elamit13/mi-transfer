"""
Encrypted Persistent Account Vault for Mint Uploader & Mint Downloader.
Stores synchronized cloud accounts (GDrive, MEGA, OneDrive, Dropbox, Mediafire)
encrypted at rest with AES-256-GCM in SQLite WAL, plus persistent WebKit2 session
profiles and shared Netscape cookie jars so sessions persist permanently for both
Mint Uploader and Mint Downloader unless the user changes their password or revokes access.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import shutil
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from Cryptodome.Cipher import AES
    _HAS_CRYPTODOME = True
except ImportError:
    _HAS_CRYPTODOME = False
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM


# Providers in the exact requested order
UPLOAD_PROVIDERS: list[tuple[str, str, str]] = [
    ("gdrive", "GDrive", "oauth2"),
    ("mega", "MEGA", "credentials"),
    ("onedrive", "OneDrive", "oauth2"),
    ("dropbox", "Dropbox", "oauth2"),
    ("mediafire", "Mediafire", "credentials"),
]

PROVIDER_DISPLAY_NAMES: Dict[str, str] = {key: label for key, label, _ in UPLOAD_PROVIDERS}
PROVIDER_ORDER: Dict[str, int] = {key: idx for idx, (key, _, _) in enumerate(UPLOAD_PROVIDERS)}
PROVIDER_AUTH_MODE: Dict[str, str] = {key: mode for key, _, mode in UPLOAD_PROVIDERS}

PROVIDER_DOMAINS: Dict[str, tuple[str, ...]] = {
    "gdrive": ("google.com", "googleusercontent.com", "gstatic.com"),
    "mega": ("mega.nz", "mega.co.nz", "mega.io"),
    "onedrive": ("live.com", "onedrive.com", "microsoft.com", "sharepoint.com", "microsoftonline.com"),
    "dropbox": ("dropbox.com", "dropboxusercontent.com"),
    "mediafire": ("mediafire.com",),
}


@dataclass
class CloudAccount:
    id: int
    provider: str
    label: str
    account_email: str = ""
    status: str = "connected"
    status_msg: str = "Sesión activa"
    creds: Dict[str, Any] = field(default_factory=dict)
    created_at: float = 0.0
    updated_at: float = 0.0
    session_scope: str = "both"  # "both" (Uploader + Downloader) or "uploader"
    web_profile_dir: str = ""

    @property
    def provider_name(self) -> str:
        return PROVIDER_DISPLAY_NAMES.get(self.provider, self.provider)

    @property
    def display_title(self) -> str:
        server = self.provider_name
        raw = self.label.strip() or self.account_email.strip()
        if raw.lower() in ("", server.lower(), f"cuenta {server.lower()}") and self.account_email.strip():
            raw = self.account_email.strip()
        if not raw or raw.lower() == server.lower():
            return f"Cuenta ({server})"
        if raw.lower().endswith(f"({server.lower()})"):
            return raw
        return f"{raw} ({server})"

    @property
    def shared_with_downloader(self) -> bool:
        return self.session_scope == "both"


class AccountVault:
    """Thread-safe AES-256-GCM encrypted SQLite store for permanent shared cloud sessions."""

    def __init__(self, db_path: str, key_path: Optional[str] = None) -> None:
        self.db_path = db_path
        self.config_dir = Path(db_path).resolve().parent
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.sessions_dir = self.config_dir / "sessions"
        self.web_profiles_dir = self.config_dir / "web_profiles"
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self.web_profiles_dir.mkdir(parents=True, exist_ok=True)
        for d in (self.sessions_dir, self.web_profiles_dir):
            try:
                os.chmod(str(d), 0o700)
            except OSError:
                pass

        self.shared_cookies_path = self.sessions_dir / "shared_cookies.txt"
        self.key_path = key_path or str(self.config_dir / ".vault.key")
        self._key = self._load_or_create_key()
        self._lock = threading.Lock()
        self._cache_list: Optional[List[CloudAccount]] = None
        self._cache_by_id: Dict[int, CloudAccount] = {}
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_table()

    def _invalidate_cache(self) -> None:
        self._cache_list = None
        self._cache_by_id.clear()

    def allocate_web_profile_dir(self, provider: str) -> str:
        """Create a dedicated persistent WebKit2 profile folder for an account."""
        slug = f"{provider}_{secrets.token_hex(8)}"
        p = self.web_profiles_dir / slug
        p.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(str(p), 0o700)
        except OSError:
            pass
        return str(p)

    def _load_or_create_key(self) -> bytes:
        kp = Path(self.key_path)
        if kp.is_file():
            raw = kp.read_bytes()
            if len(raw) == 32:
                return raw
        key = secrets.token_bytes(32)
        fd = os.open(str(kp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, key)
        finally:
            os.close(fd)
        try:
            os.chmod(str(kp), 0o600)
        except OSError:
            pass
        return key

    def _init_table(self) -> None:
        with self._lock, self._conn as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA temp_store=MEMORY")
            conn.execute("PRAGMA cache_size=-4000")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS cloud_accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    provider TEXT NOT NULL,
                    label TEXT NOT NULL,
                    account_email TEXT DEFAULT '',
                    status TEXT DEFAULT 'connected',
                    status_msg TEXT DEFAULT 'Sesión activa',
                    encrypted_creds TEXT NOT NULL,
                    created_at REAL DEFAULT 0,
                    updated_at REAL DEFAULT 0,
                    session_scope TEXT DEFAULT 'both',
                    web_profile_dir TEXT DEFAULT ''
                )
                """
            )
            # Graceful migration for existing databases
            cols = {
                str(r["name"])
                for r in conn.execute("PRAGMA table_info(cloud_accounts)").fetchall()
            }
            if "session_scope" not in cols:
                conn.execute("ALTER TABLE cloud_accounts ADD COLUMN session_scope TEXT DEFAULT 'both'")
            if "web_profile_dir" not in cols:
                conn.execute("ALTER TABLE cloud_accounts ADD COLUMN web_profile_dir TEXT DEFAULT ''")

    def _encrypt_dict(self, data: Dict[str, Any]) -> str:
        plaintext = json.dumps(data, separators=(",", ":")).encode("utf-8")
        nonce = secrets.token_bytes(12)
        if _HAS_CRYPTODOME:
            cipher = AES.new(self._key, AES.MODE_GCM, nonce=nonce)
            ct, tag = cipher.encrypt_and_digest(plaintext)
            blob = nonce + tag + ct
        else:
            aesgcm = AESGCM(self._key)
            ct_tag = aesgcm.encrypt(nonce, plaintext, None)
            blob = nonce + ct_tag[-16:] + ct_tag[:-16]
        return base64.b64encode(blob).decode("ascii")

    def _decrypt_dict(self, token_b64: str) -> Dict[str, Any]:
        if not token_b64:
            return {}
        try:
            blob = base64.b64decode(token_b64)
            if len(blob) < 28:
                return {}
            nonce = blob[:12]
            tag = blob[12:28]
            ct = blob[28:]
            if _HAS_CRYPTODOME:
                cipher = AES.new(self._key, AES.MODE_GCM, nonce=nonce)
                plaintext = cipher.decrypt_and_verify(ct, tag)
            else:
                aesgcm = AESGCM(self._key)
                plaintext = aesgcm.decrypt(nonce, ct + tag, None)
            parsed = json.loads(plaintext.decode("utf-8"))
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}

    def _row_to_account(self, row: sqlite3.Row) -> CloudAccount:
        return CloudAccount(
            id=int(row["id"]),
            provider=str(row["provider"]),
            label=str(row["label"]),
            account_email=str(row["account_email"] or ""),
            status=str(row["status"] or "connected"),
            status_msg=str(row["status_msg"] or "Sesión activa"),
            creds=self._decrypt_dict(str(row["encrypted_creds"] or "")),
            created_at=float(row["created_at"] or 0.0),
            updated_at=float(row["updated_at"] or 0.0),
            session_scope=str(row["session_scope"] or "both"),
            web_profile_dir=str(row["web_profile_dir"] or ""),
        )

    def list_accounts(self) -> List[CloudAccount]:
        with self._lock:
            if self._cache_list is not None:
                return list(self._cache_list)
            with self._conn as conn:
                rows = conn.execute(
                    "SELECT id, provider, label, account_email, status, status_msg, "
                    "encrypted_creds, created_at, updated_at, session_scope, web_profile_dir "
                    "FROM cloud_accounts ORDER BY id ASC"
                ).fetchall()
            accounts = [self._row_to_account(r) for r in rows]
            accounts.sort(key=lambda a: (PROVIDER_ORDER.get(a.provider, 99), a.id))
            self._cache_list = accounts
            self._cache_by_id = {a.id: a for a in accounts}
            return list(accounts)

    def get_account(self, account_id: int) -> Optional[CloudAccount]:
        with self._lock:
            if self._cache_list is not None:
                return self._cache_by_id.get(account_id)
        # Populate cache once on cold lookup
        self.list_accounts()
        with self._lock:
            return self._cache_by_id.get(account_id)

    def save_account(
        self,
        provider: str,
        label: str,
        account_email: str,
        creds: Dict[str, Any],
        status: str = "connected",
        status_msg: str = "Sesión activa",
        account_id: Optional[int] = None,
        session_scope: str = "both",
        web_profile_dir: str = "",
    ) -> CloudAccount:
        now = time.time()
        enc = self._encrypt_dict(creds)
        clean_label = label.strip() or account_email.strip() or PROVIDER_DISPLAY_NAMES.get(provider, provider)
        clean_scope = "both" if session_scope != "uploader" else "uploader"
        with self._lock, self._conn as conn:
            if account_id is not None:
                conn.execute(
                    """
                    UPDATE cloud_accounts SET
                        provider=?, label=?, account_email=?, status=?, status_msg=?,
                        encrypted_creds=?, updated_at=?, session_scope=?, web_profile_dir=?
                    WHERE id=?
                    """,
                    (
                        provider,
                        clean_label,
                        account_email,
                        status,
                        status_msg,
                        enc,
                        now,
                        clean_scope,
                        web_profile_dir,
                        account_id,
                    ),
                )
                aid = account_id
            else:
                cur = conn.execute(
                    """
                    INSERT INTO cloud_accounts (
                        provider, label, account_email, status, status_msg,
                        encrypted_creds, created_at, updated_at, session_scope, web_profile_dir
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        provider,
                        clean_label,
                        account_email,
                        status,
                        status_msg,
                        enc,
                        now,
                        now,
                        clean_scope,
                        web_profile_dir,
                    ),
                )
                aid = int(cur.lastrowid)
        acc = CloudAccount(
            id=aid,
            provider=provider,
            label=clean_label,
            account_email=account_email,
            status=status,
            status_msg=status_msg,
            creds=dict(creds),
            created_at=now,
            updated_at=now,
            session_scope=clean_scope,
            web_profile_dir=web_profile_dir,
        )
        with self._lock:
            self._invalidate_cache()
        self.export_shared_cookies()
        return acc

    def update_credentials(
        self,
        account_id: int,
        creds: Dict[str, Any],
        status: str = "connected",
        status_msg: str = "Sesión activa",
        account_email: Optional[str] = None,
        web_profile_dir: Optional[str] = None,
        label: Optional[str] = None,
    ) -> None:
        prev = self.get_account(account_id)
        cookies_changed = (prev is None) or (prev.creds.get("cookies") != creds.get("cookies"))
        now = time.time()
        enc = self._encrypt_dict(creds)
        with self._lock, self._conn as conn:
            fields = ["encrypted_creds=?", "status=?", "status_msg=?", "updated_at=?"]
            vals: list[Any] = [enc, status, status_msg, now]
            if account_email is not None:
                fields.append("account_email=?")
                vals.append(account_email)
            if web_profile_dir is not None:
                fields.append("web_profile_dir=?")
                vals.append(web_profile_dir)
            if label is not None and label.strip():
                fields.append("label=?")
                vals.append(label.strip())
            vals.append(account_id)
            conn.execute(
                f"UPDATE cloud_accounts SET {', '.join(fields)} WHERE id=?",
                tuple(vals),
            )
            self._invalidate_cache()
        if cookies_changed:
            self.export_shared_cookies()

    def rename_account(self, account_id: int, new_label: str) -> None:
        clean = new_label.strip()
        if not clean:
            return
        now = time.time()
        with self._lock, self._conn as conn:
            conn.execute(
                "UPDATE cloud_accounts SET label=?, updated_at=? WHERE id=?",
                (clean, now, account_id),
            )
            self._invalidate_cache()

    def update_status(self, account_id: int, status: str, status_msg: str) -> None:
        now = time.time()
        with self._lock, self._conn as conn:
            conn.execute(
                "UPDATE cloud_accounts SET status=?, status_msg=?, updated_at=? WHERE id=?",
                (status, status_msg, now, account_id),
            )
            self._invalidate_cache()

    def delete_account(self, account_id: int) -> None:
        acc = self.get_account(account_id)
        with self._lock, self._conn as conn:
            conn.execute("DELETE FROM cloud_accounts WHERE id=?", (account_id,))
            self._invalidate_cache()
        # Clean up isolated WebKit profile directory safely if inside web_profiles_dir
        if acc and acc.web_profile_dir:
            try:
                prof = Path(acc.web_profile_dir).resolve()
                base = self.web_profiles_dir.resolve()
                if prof.is_dir() and base in prof.parents:
                    shutil.rmtree(prof, ignore_errors=True)
            except Exception:
                pass
        # Clean up per-account cookie file
        try:
            acc_cookie = self.sessions_dir / f"account_{account_id}.cookies.txt"
            if acc_cookie.exists():
                acc_cookie.unlink()
        except OSError:
            pass
        self.export_shared_cookies()

    @staticmethod
    def _format_netscape_cookie_line(c: Dict[str, Any]) -> Optional[str]:
        domain = str(c.get("domain") or "").strip()
        name = str(c.get("name") or "").strip()
        value = str(c.get("value") or "")
        if not domain or not name:
            return None
        include_sub = "TRUE" if domain.startswith(".") else "FALSE"
        path = str(c.get("path") or "/")
        secure = "TRUE" if c.get("secure") else "FALSE"
        expires = int(c.get("expires") or 0)
        # Preserve session cookies across restarts by giving a far-future expiry if 0
        if expires <= 0:
            expires = int(time.time()) + 365 * 86400
        prefix = "#HttpOnly_" if c.get("http_only") else ""
        # Strip tabs/newlines to keep Netscape format strictly valid
        clean_val = value.replace("\t", " ").replace("\r", "").replace("\n", "")
        return f"{prefix}{domain}\t{include_sub}\t{path}\t{secure}\t{expires}\t{name}\t{clean_val}"

    def _write_secure_file(self, path: Path, text: str) -> None:
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, text.encode("utf-8"))
        finally:
            os.close(fd)
        try:
            os.chmod(str(path), 0o600)
        except OSError:
            pass

    def export_shared_cookies(self) -> str:
        """
        Export cookies from all accounts with session_scope='both' into Netscape
        cookies.txt format so Mint Downloader (yt-dlp / aria2c / aiohttp) can reuse
        every synchronized account session automatically without re-logging in.
        """
        accounts = self.list_accounts()
        shared_lines = [
            "# Netscape HTTP Cookie File",
            "# Generado automáticamente por Mint — Sesiones compartidas (Uploader + Downloader)",
        ]
        seen_keys: set[tuple[str, str, str]] = set()

        for acc in accounts:
            cookies = acc.creds.get("cookies")
            if not isinstance(cookies, list) or not cookies:
                continue
            acc_lines = [
                "# Netscape HTTP Cookie File",
                f"# Cuenta: {acc.display_title} ({acc.provider})",
            ]
            for c in cookies:
                if not isinstance(c, dict):
                    continue
                line = self._format_netscape_cookie_line(c)
                if not line:
                    continue
                acc_lines.append(line)
                if acc.shared_with_downloader:
                    dedup_key = (
                        str(c.get("domain") or "").lower(),
                        str(c.get("path") or "/"),
                        str(c.get("name") or ""),
                    )
                    if dedup_key not in seen_keys:
                        seen_keys.add(dedup_key)
                        shared_lines.append(line)
            try:
                self._write_secure_file(
                    self.sessions_dir / f"account_{acc.id}.cookies.txt",
                    "\n".join(acc_lines) + "\n",
                )
            except OSError:
                pass

        try:
            self._write_secure_file(
                self.shared_cookies_path,
                "\n".join(shared_lines) + "\n",
            )
        except OSError:
            pass
        return str(self.shared_cookies_path)

    def get_cookies_for_domain(self, domain_suffix: str) -> List[Dict[str, Any]]:
        """Retrieve shared cookies matching a domain suffix (ready for Downloader reuse)."""
        suffix = domain_suffix.lower().lstrip(".")
        matched: List[Dict[str, Any]] = []
        for acc in self.list_accounts():
            if not acc.shared_with_downloader:
                continue
            cookies = acc.creds.get("cookies")
            if not isinstance(cookies, list):
                continue
            for c in cookies:
                if not isinstance(c, dict):
                    continue
                dom = str(c.get("domain") or "").lower().lstrip(".")
                if dom == suffix or dom.endswith("." + suffix):
                    matched.append(c)
        return matched

    def close(self) -> None:
        try:
            with self._lock:
                self._conn.close()
        except Exception:
            pass
