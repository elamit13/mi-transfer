"""
Core Download Engine for Mint Downloader.
Manages the background aria2c JSON-RPC daemon (16 segmented connections per file),
orchestrates yt-dlp/ffmpeg and MEGA AES-CTR tasks, enforces concurrency limits,
and persists download queues and settings in SQLite3.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import secrets
import shutil
import socket
import sqlite3
import subprocess
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiohttp
from aiohttp import web

from .extractors.cloud_extractor import (
    CloudExtractor,
    ExtractorError,
    _sanitize_filename,
    decrypt_mega_file,
    detect_host,
)
from .extractors.video_extractor import (
    DownloadCancelledError,
    VideoExtractor,
    VideoTaskProgress,
    is_video_or_stream_url,
)


STATUS_LABELS_ES = {
    "resolving": "Analizando enlace...",
    "queued": "En cola",
    "active": "Descargando",
    "merging": "Uniendo A/V (ffmpeg)",
    "decrypting": "Descifrando MEGA",
    "paused": "Pausado",
    "stopped": "Detenido",
    "complete": "Completado",
    "error": "Error",
}

_SPLIT_ARCH_RE = re.compile(r"(\.(?:zip|rar|7z)\.\d{3})$")
_TAR_EXT_RE = re.compile(r"(\.tar\.(?:gz|bz2|xz|zst))$")
_ORIGIN_MAP = (
    ("youtube.com", "YouTube"),
    ("youtu.be", "YouTube"),
    ("mediafire.com", "MediaFire"),
    ("mega.nz", "MEGA"),
    ("mega.co.nz", "MEGA"),
    ("drive.google.com", "Google Drive"),
    ("googleusercontent.com", "Google Drive"),
    ("1drv.ms", "OneDrive"),
    ("onedrive.live.com", "OneDrive"),
    ("sharepoint.com", "OneDrive"),
    ("1fichier.com", "1fichier"),
)


@dataclass
class DownloadItem:
    id: int
    url: str
    direct_url: str = ""
    gid: str = ""
    filename: str = "Analizando..."
    host: str = ""
    save_dir: str = ""
    total_size: int = 0
    completed_size: int = 0
    speed: float = 0.0
    eta: int = 0
    status: str = "queued"
    error_msg: str = ""
    extractor_type: str = "direct"
    headers: Dict[str, str] = field(default_factory=dict)
    extra: Dict[str, Any] = field(default_factory=dict)

    @property
    def progress_fraction(self) -> float:
        if self.status == "complete":
            return 1.0
        if self.total_size <= 0:
            return 0.0
        return min(1.0, max(0.0, self.completed_size / self.total_size))

    @property
    def progress_percent(self) -> float:
        return self.progress_fraction * 100.0

    @property
    def file_extension(self) -> str:
        """Return the file extension / container format (.mkv, .mp4, .mp3, .rar, .zip, etc.)."""
        name_lower = (self.filename or "").lower().strip()
        if "." in name_lower:
            m_split_arch = _SPLIT_ARCH_RE.search(name_lower)
            if m_split_arch:
                return m_split_arch.group(1)
            m_tar = _TAR_EXT_RE.search(name_lower)
            if m_tar:
                return m_tar.group(1)
            _, ext = os.path.splitext(name_lower)
            if ext and len(ext) <= 10:
                return ext
        ext_extra = str(self.extra.get("ext") or "").strip().lower()
        if ext_extra:
            return ext_extra if ext_extra.startswith(".") else f".{ext_extra}"
        if self.extractor_type in ("magnet", "torrent_file"):
            return ".torrent"
        return "—"

    @property
    def origin_site_name(self) -> str:
        """Return the clean website/domain name from where the file is being downloaded."""
        source_page = str(self.extra.get("source_page") or "").strip()
        if not source_page and self.headers:
            for hk, hv in self.headers.items():
                if hk.lower() in ("referer", "origin") and hv:
                    source_page = hv
                    break
        for candidate_url in (source_page, self.url, self.direct_url):
            if not candidate_url:
                continue
            if candidate_url[:7].lower() == "magnet:":
                return "BitTorrent"
            try:
                netloc = (urllib.parse.urlparse(candidate_url).netloc or "").lower().removeprefix("www.")
            except Exception:
                netloc = ""
            if not netloc:
                continue
            for dom_frag, friendly in _ORIGIN_MAP:
                if dom_frag in netloc:
                    return friendly
            return netloc
        return self.host or "Directo"

    @property
    def container_name(self) -> str:
        c = str(self.extra.get("container") or self.extra.get("subfolder") or "").strip()
        return c if c else "General"

    @property
    def status_display(self) -> str:
        base = STATUS_LABELS_ES.get(self.status, self.status)
        if self.status == "error" and self.error_msg:
            return f"Error: {self.error_msg[:80]}"
        return base


def _find_aria2c_binary() -> tuple[str, dict[str, str]]:
    """Locate system or bundled aria2c binary and prepare environment variables."""
    env = os.environ.copy()
    project_root = Path(__file__).resolve().parent.parent
    local_bin = project_root / ".local_deps" / "usr" / "bin" / "aria2c"
    local_lib = project_root / ".local_deps" / "usr" / "lib" / "x86_64-linux-gnu"

    sys_bin = shutil.which("aria2c")
    if sys_bin and "/.local_deps/" not in sys_bin:
        return sys_bin, env

    if local_bin.exists():
        ld_prev = env.get("LD_LIBRARY_PATH", "")
        if str(local_lib) not in ld_prev:
            env["LD_LIBRARY_PATH"] = f"{local_lib}:{ld_prev}" if ld_prev else str(local_lib)
        return str(local_bin), env

    if sys_bin:
        return sys_bin, env

    raise RuntimeError(
        "No se encontró el binario 'aria2c'. Ejecuta ./install.sh para instalar las dependencias."
    )


def _find_free_port(preferred: int = 6800) -> int:
    for port in [preferred, *range(16800, 16850)]:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class DownloadEngine:
    """
    Asynchronous download engine backed by aria2c JSON-RPC, yt-dlp/ffmpeg,
    modular cloud extractors, and SQLite persistence.
    """

    def __init__(
        self,
        db_path: Optional[str] = None,
        default_save_dir: Optional[str] = None,
        max_concurrent: int = 4,
    ) -> None:
        config_dir = Path.home() / ".config" / "mint-downloader"
        config_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path or str(config_dir / "downloads.db")

        downloads_folder = Path.home() / "Descargas"
        if not downloads_folder.exists():
            downloads_folder = Path.home() / "Downloads"
        downloads_folder.mkdir(parents=True, exist_ok=True)

        self.save_dir = default_save_dir or str(downloads_folder)
        self.max_concurrent = max_concurrent
        self.speed_limit_kbps = 0
        self.bg_mode = "default"
        self.bg_color = "#1e293b"
        self.bg_image = ""
        self.bg_opacity = 0.55

        self.aria2_bin, self.aria2_env = _find_aria2c_binary()
        self.rpc_port = _find_free_port(6800)
        self.rpc_secret = secrets.token_hex(12)
        self.rpc_url = f"http://127.0.0.1:{self.rpc_port}/jsonrpc"
        self.aria2_proc: Optional[subprocess.Popen] = None

        self.cloud_extractor = CloudExtractor()
        self.video_extractor = VideoExtractor(aria2c_path=self.aria2_bin)

        self.items: Dict[int, DownloadItem] = {}
        self.state_version: int = 0
        self._video_cancel_events: Dict[int, threading.Event] = {}
        self._video_tasks: Dict[int, asyncio.Task] = {}
        self._decrypt_tasks: set[int] = set()
        self._session: Optional[aiohttp.ClientSession] = None
        self._bridge_runner: Optional[web.AppRunner] = None
        self.bridge_port: int = 16890
        self._running = False
        self._last_browser_sync: float = 0.0
        self._db_lock = threading.Lock()
        self._db_conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._db_conn.row_factory = sqlite3.Row

        self._init_db()
        self._load_state_from_db()

    def _bump_version(self) -> None:
        self.state_version += 1

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
                CREATE TABLE IF NOT EXISTS downloads (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    gid TEXT DEFAULT '',
                    url TEXT NOT NULL,
                    direct_url TEXT DEFAULT '',
                    filename TEXT NOT NULL,
                    host TEXT DEFAULT '',
                    save_dir TEXT NOT NULL,
                    total_size INTEGER DEFAULT 0,
                    completed_size INTEGER DEFAULT 0,
                    status TEXT DEFAULT 'queued',
                    error_msg TEXT DEFAULT '',
                    extractor_type TEXT DEFAULT 'direct',
                    headers_json TEXT DEFAULT '{}',
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
                if k == "save_dir" and os.path.isdir(v):
                    self.save_dir = v
                elif k == "max_concurrent":
                    try:
                        self.max_concurrent = max(1, min(16, int(v)))
                    except ValueError:
                        pass
                elif k == "speed_limit_kbps":
                    try:
                        self.speed_limit_kbps = max(0, int(v))
                    except ValueError:
                        pass
                elif k == "bg_mode":
                    self.bg_mode = v
                elif k == "bg_color":
                    self.bg_color = v
                elif k == "bg_image":
                    self.bg_image = v
                elif k == "bg_opacity":
                    try:
                        self.bg_opacity = max(0.0, min(0.9, float(v)))
                    except ValueError:
                        pass

            for row in conn.execute(
                "SELECT id, url, direct_url, filename, host, save_dir, total_size, completed_size, "
                "status, error_msg, extractor_type, headers_json, extra_json FROM downloads ORDER BY id ASC"
            ):
                st = row["status"]
                if st in ("active", "resolving", "merging", "decrypting"):
                    st = "paused"
                item = DownloadItem(
                    id=row["id"],
                    gid="",
                    url=row["url"],
                    direct_url=row["direct_url"] or "",
                    filename=row["filename"],
                    host=row["host"] or detect_host(row["url"]),
                    save_dir=row["save_dir"] or self.save_dir,
                    total_size=int(row["total_size"] or 0),
                    completed_size=int(row["completed_size"] or 0),
                    status=st,
                    error_msg=row["error_msg"] or "",
                    extractor_type=row["extractor_type"] or "direct",
                    headers=json.loads(row["headers_json"] or "{}"),
                    extra=json.loads(row["extra_json"] or "{}"),
                )
                if item.status != "complete":
                    try:
                        disk_sz = os.stat(os.path.join(item.save_dir, item.filename)).st_size
                        if disk_sz > item.completed_size:
                            item.completed_size = disk_sz
                    except OSError:
                        pass
                self.items[item.id] = item
        self._bump_version()

    def _save_setting(self, key: str, value: str) -> None:
        with self._db_lock, self._db_conn as conn:
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)),
            )

    def _insert_item_db(self, item: DownloadItem) -> int:
        now = time.time()
        with self._db_lock, self._db_conn as conn:
            cur = conn.execute(
                """
                INSERT INTO downloads (
                    gid, url, direct_url, filename, host, save_dir,
                    total_size, completed_size, status, error_msg,
                    extractor_type, headers_json, extra_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.gid,
                    item.url,
                    item.direct_url,
                    item.filename,
                    item.host,
                    item.save_dir,
                    item.total_size,
                    item.completed_size,
                    item.status,
                    item.error_msg,
                    item.extractor_type,
                    json.dumps(item.headers),
                    json.dumps(item.extra),
                    now,
                    now,
                ),
            )
            return int(cur.lastrowid)

    def _update_item_db(self, item: DownloadItem) -> None:
        now = time.time()
        with self._db_lock, self._db_conn as conn:
            conn.execute(
                """
                UPDATE downloads SET
                    gid=?, direct_url=?, filename=?, host=?, save_dir=?,
                    total_size=?, completed_size=?, status=?, error_msg=?,
                    extractor_type=?, headers_json=?, extra_json=?, updated_at=?
                WHERE id=?
                """,
                (
                    item.gid,
                    item.direct_url,
                    item.filename,
                    item.host,
                    item.save_dir,
                    item.total_size,
                    item.completed_size,
                    item.status,
                    item.error_msg,
                    item.extractor_type,
                    json.dumps(item.headers),
                    json.dumps(item.extra),
                    now,
                    item.id,
                ),
            )

    def _delete_item_db(self, item_id: int) -> None:
        with self._db_lock, self._db_conn as conn:
            conn.execute("DELETE FROM downloads WHERE id=?", (item_id,))

    async def start(self) -> None:
        """Start the aria2c JSON-RPC daemon and initialize the async HTTP session."""
        if self._running:
            return

        cmd = [
            self.aria2_bin,
            "--enable-rpc=true",
            "--rpc-listen-all=false",
            f"--rpc-listen-port={self.rpc_port}",
            f"--rpc-secret={self.rpc_secret}",
            "--max-connection-per-server=16",
            "--split=16",
            "--min-split-size=1M",
            f"--max-concurrent-downloads={self.max_concurrent}",
            "--continue=true",
            "--allow-overwrite=true",
            "--auto-file-renaming=false",
            "--file-allocation=none",
            "--no-proxy=127.0.0.1,localhost",
            "--connect-timeout=15",
            "--timeout=30",
            "--max-tries=5",
            "--retry-wait=2",
            "--enable-dht=true",
            "--bt-enable-lpd=true",
            "--enable-peer-exchange=true",
            "--bt-max-peers=80",
            "--seed-time=0",
            "--follow-torrent=mem",
            "--summary-interval=0",
            "--quiet=true",
        ]
        if self.speed_limit_kbps > 0:
            cmd.append(f"--max-overall-download-limit={self.speed_limit_kbps}K")

        self.aria2_proc = subprocess.Popen(
            cmd,
            env=self.aria2_env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        connector = aiohttp.TCPConnector(limit=32, ttl_dns_cache=300)
        self._session = aiohttp.ClientSession(connector=connector, timeout=aiohttp.ClientTimeout(total=25))
        self._running = True

        for _ in range(30):
            try:
                ver = await self.rpc_call("aria2.getVersion")
                if ver and "version" in ver:
                    break
            except Exception:
                await asyncio.sleep(0.1)

        await self._start_browser_bridge()

    async def shutdown(self) -> None:
        """Gracefully pause active tasks, persist state in SQLite, and stop aria2c."""
        self._running = False

        for ev in list(self._video_cancel_events.values()):
            ev.set()

        now = time.time()
        changed_rows = []
        for item in self.items.values():
            if item.status in ("active", "resolving", "merging", "decrypting"):
                item.status = "paused"
                item.speed = 0.0
                changed_rows.append(
                    (
                        item.gid,
                        item.direct_url,
                        item.filename,
                        item.host,
                        item.save_dir,
                        item.total_size,
                        item.completed_size,
                        item.status,
                        item.error_msg,
                        item.extractor_type,
                        json.dumps(item.headers),
                        json.dumps(item.extra),
                        now,
                        item.id,
                    )
                )
        if changed_rows:
            with self._db_lock, self._db_conn as conn:
                conn.executemany(
                    """
                    UPDATE downloads SET
                        gid=?, direct_url=?, filename=?, host=?, save_dir=?,
                        total_size=?, completed_size=?, status=?, error_msg=?,
                        extractor_type=?, headers_json=?, extra_json=?, updated_at=?
                    WHERE id=?
                    """,
                    changed_rows,
                )

        if self._bridge_runner:
            try:
                await self._bridge_runner.cleanup()
            except Exception:
                pass
            self._bridge_runner = None

        if self._session:
            try:
                await self.rpc_call("aria2.shutdown")
            except Exception:
                pass
            await self._session.close()
            self._session = None

        if self.aria2_proc:
            try:
                self.aria2_proc.terminate()
                self.aria2_proc.wait(timeout=2)
            except Exception:
                try:
                    self.aria2_proc.kill()
                except Exception:
                    pass
            self.aria2_proc = None

        try:
            with self._db_lock:
                self._db_conn.close()
        except Exception:
            pass

    async def _start_browser_bridge(self) -> None:
        """
        Start a local HTTP bridge server on 127.0.0.1:16890 for the Firefox IDM-style extension.
        Endpoints:
          - GET  /api/ping -> {"status": "ok", "app": "Mint Downloader"}
          - GET  /api/formats?url=... -> Available video resolutions/sizes via yt-dlp
          - POST /api/enqueue -> Add direct download, video with quality, magnet, or torrent
        """
        cors_headers = {
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type, Authorization",
        }

        async def _handle_options(_req: web.Request) -> web.Response:
            return web.Response(status=204, headers=cors_headers)

        async def _handle_ping(_req: web.Request) -> web.Response:
            return web.json_response(
                {"status": "ok", "app": "Mint Downloader", "save_dir": self.save_dir},
                headers=cors_headers,
            )

        async def _handle_formats(req: web.Request) -> web.Response:
            url = (req.query.get("url") or "").strip()
            if not url:
                return web.json_response({"error": "Missing url"}, status=400, headers=cors_headers)
            try:
                info = await self.video_extractor.list_qualities(url)
                choices_data = [
                    {
                        "id": c.id,
                        "label": c.label,
                        "height": c.height,
                        "fps": c.fps,
                        "filesize": c.filesize,
                        "ext": c.ext,
                    }
                    for c in info["choices"]
                ]
                return web.json_response(
                    {"title": info["title"], "duration": info["duration"], "choices": choices_data},
                    headers=cors_headers,
                )
            except Exception as exc:
                return web.json_response({"error": str(exc)}, status=500, headers=cors_headers)

        async def _handle_enqueue(req: web.Request) -> web.Response:
            try:
                body = await req.json()
            except Exception:
                return web.json_response({"error": "Invalid JSON"}, status=400, headers=cors_headers)

            url = (body.get("url") or "").strip()
            urls = body.get("urls") or ([url] if url else [])
            quality = (body.get("quality") or "best").strip()
            container = (body.get("container") or "General").strip()
            filename_hint = (body.get("filename") or "").strip()
            source_page = (body.get("source_page") or "").strip()
            req_headers = body.get("headers") or {}

            if not urls:
                return web.json_response({"error": "No URLs provided"}, status=400, headers=cors_headers)

            added = await self.add_urls(
                urls=urls,
                save_dir=self.save_dir,
                quality=quality,
                auto_start=True,
                container=container,
                custom_headers=req_headers if isinstance(req_headers, dict) else None,
                filename_hint=filename_hint,
                source_page=source_page,
            )
            return web.json_response(
                {"status": "queued", "count": len(added), "ids": [i.id for i in added]},
                headers=cors_headers,
            )

        app = web.Application()
        app.router.add_route("OPTIONS", "/api/{tail:.*}", _handle_options)
        app.router.add_get("/api/ping", _handle_ping)
        app.router.add_get("/api/formats", _handle_formats)
        app.router.add_post("/api/enqueue", _handle_enqueue)

        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        for port in (16890, 16891, 16892):
            try:
                site = web.TCPSite(runner, "127.0.0.1", port)
                await site.start()
                self.bridge_port = port
                self._bridge_runner = runner
                break
            except OSError:
                continue

    async def rpc_call(self, method: str, params: Optional[List[Any]] = None) -> Any:
        if not self._session:
            connector = aiohttp.TCPConnector(limit=32, ttl_dns_cache=300)
            self._session = aiohttp.ClientSession(connector=connector, timeout=aiohttp.ClientTimeout(total=25))
        rpc_params: List[Any] = [f"token:{self.rpc_secret}"]
        if params:
            rpc_params.extend(params)
        payload = {
            "jsonrpc": "2.0",
            "id": "mint-dl",
            "method": method,
            "params": rpc_params,
        }
        async with self._session.post(self.rpc_url, json=payload) as resp:
            data = await resp.json()
            if "error" in data:
                raise RuntimeError(data["error"].get("message", str(data["error"])))
            return data.get("result")

    async def rpc_multicall_status(self, gids: List[str], keys: List[str]) -> List[Optional[Dict[str, Any]]]:
        """Fetch aria2.tellStatus for multiple GIDs in a single JSON-RPC system.multicall HTTP round-trip."""
        if not gids:
            return []
        if len(gids) == 1:
            try:
                return [await self.rpc_call("aria2.tellStatus", [gids[0], keys])]
            except Exception:
                return [None]

        calls = [
            {"methodName": "aria2.tellStatus", "params": [f"token:{self.rpc_secret}", gid, keys]}
            for gid in gids
        ]
        if not self._session:
            connector = aiohttp.TCPConnector(limit=32, ttl_dns_cache=300)
            self._session = aiohttp.ClientSession(connector=connector, timeout=aiohttp.ClientTimeout(total=25))
        payload = {
            "jsonrpc": "2.0",
            "id": "mint-dl-batch",
            "method": "system.multicall",
            "params": [calls],
        }
        try:
            async with self._session.post(self.rpc_url, json=payload) as resp:
                data = await resp.json()
                raw_results = data.get("result")
                if not isinstance(raw_results, list):
                    return [None] * len(gids)
                out: List[Optional[Dict[str, Any]]] = []
                for entry in raw_results:
                    if isinstance(entry, list) and entry and isinstance(entry[0], dict):
                        out.append(entry[0])
                    else:
                        out.append(None)
                return out
        except Exception:
            return [None] * len(gids)

    @staticmethod
    def _ensure_unique_filename(save_dir: str, filename: str, item_id: int, existing_items: Dict[int, DownloadItem]) -> str:
        """
        Ensure two different URLs never write to the same filename on disk (preventing two concurrent
        downloads from corrupting each other's .part-Frag files or yt-dlp skipping a new movie
        because an older movie had the same generic name like index-s1080p-v1-a1.mp4).
        """
        base, ext = os.path.splitext(filename)
        if not ext:
            ext = ".mp4"
        candidate = f"{base}{ext}"
        active_names = {
            it.filename
            for iid, it in existing_items.items()
            if iid != item_id and it.save_dir == save_dir and it.status in ("resolving", "queued", "active", "merging")
        }
        counter = 1
        while candidate in active_names or os.path.exists(os.path.join(save_dir, candidate)):
            candidate = f"{base} ({counter}){ext}"
            counter += 1
        return candidate

    def set_save_dir(self, path: str) -> None:
        os.makedirs(path, exist_ok=True)
        self.save_dir = path
        self._save_setting("save_dir", path)

    def set_background_theme(
        self,
        mode: str,
        color: str = "",
        image_path: str = "",
        opacity: Optional[float] = None,
    ) -> None:
        self.bg_mode = mode
        if color:
            self.bg_color = color
            self._save_setting("bg_color", color)
        if image_path is not None:
            self.bg_image = image_path
            self._save_setting("bg_image", image_path)
        if opacity is not None:
            self.bg_opacity = max(0.0, min(0.9, float(opacity)))
            self._save_setting("bg_opacity", str(self.bg_opacity))
        self._save_setting("bg_mode", mode)

    async def set_max_concurrent(self, limit: int) -> None:
        self.max_concurrent = max(1, min(16, int(limit)))
        self._save_setting("max_concurrent", str(self.max_concurrent))
        self._bump_version()
        if self._running:
            try:
                await self.rpc_call(
                    "aria2.changeGlobalOption",
                    [{"max-concurrent-downloads": str(self.max_concurrent)}],
                )
            except Exception:
                pass

    async def set_speed_limit_kbps(self, kbps: int) -> None:
        self.speed_limit_kbps = max(0, int(kbps))
        self._save_setting("speed_limit_kbps", str(self.speed_limit_kbps))
        if self._running:
            limit_str = f"{self.speed_limit_kbps}K" if self.speed_limit_kbps > 0 else "0"
            try:
                await self.rpc_call(
                    "aria2.changeGlobalOption",
                    [{"max-overall-download-limit": limit_str}],
                )
            except Exception:
                pass

    async def add_urls(
        self,
        urls: List[str],
        save_dir: Optional[str] = None,
        quality: str = "best",
        auto_start: bool = True,
        container: str = "",
        custom_headers: Optional[Dict[str, str]] = None,
        filename_hint: str = "",
        source_page: str = "",
    ) -> List[DownloadItem]:
        target_dir = save_dir or self.save_dir
        clean_container = container.strip().strip("/\\") if container else ""
        if clean_container and clean_container.lower() != "general":
            target_dir = os.path.join(target_dir, clean_container)
        os.makedirs(target_dir, exist_ok=True)
        added: List[DownloadItem] = []

        for raw_url in urls:
            url = raw_url.strip()
            if not url:
                continue

            # Deduplicate: if an active or resolving task already has this exact URL, skip creating a duplicate!
            already_running = next(
                (
                    ex
                    for ex in self.items.values()
                    if ex.url == url and ex.status in ("resolving", "queued", "active", "merging")
                ),
                None,
            )
            if already_running:
                added.append(already_running)
                continue

            # Expand multi-file folder links (e.g., MediaFire /folder/<key>/#<subkey>)
            if self.cloud_extractor.is_folder_url(url):
                try:
                    entries = await self.cloud_extractor.expand_folder(url, session=self._session)
                    for entry in entries:
                        entry_dir = (
                            os.path.join(target_dir, entry.subfolder)
                            if entry.subfolder
                            else target_dir
                        )
                        os.makedirs(entry_dir, exist_ok=True)
                        eff_container = clean_container or entry.subfolder or "General"
                        item = DownloadItem(
                            id=0,
                            url=entry.url,
                            filename=entry.filename,
                            host=entry.host,
                            save_dir=entry_dir,
                            total_size=entry.size,
                            status="resolving",
                            extra={
                                "quality": quality,
                                "subfolder": entry.subfolder,
                                "container": eff_container,
                            },
                        )
                        item.id = self._insert_item_db(item)
                        self.items[item.id] = item
                        self._bump_version()
                        added.append(item)
                        asyncio.create_task(self._resolve_and_enqueue(item, auto_start=auto_start))
                    continue
                except Exception as exc:
                    item = DownloadItem(
                        id=0,
                        url=url,
                        filename=os.path.basename(url.split("?")[0]) or "Carpeta",
                        host=detect_host(url),
                        save_dir=target_dir,
                        status="error",
                        error_msg=str(exc),
                        extra={"quality": quality, "container": clean_container or "General"},
                    )
                    item.id = self._insert_item_db(item)
                    self.items[item.id] = item
                    self._bump_version()
                    added.append(item)
                    continue

            # If the user pasted a direct download*.mediafire.com link and there is already a
            # pending/error MediaFire task with the same quickkey, bind it directly to that task!
            mf_qk = self.cloud_extractor.extract_mediafire_quickkey(url)
            if mf_qk and "download" in url and "mediafire.com" in url:
                matched_existing = False
                for existing in self.items.values():
                    if existing.status in ("error", "paused", "queued", "stopped"):
                        ex_qk = self.cloud_extractor.extract_mediafire_quickkey(existing.url)
                        if ex_qk and ex_qk == mf_qk:
                            existing.direct_url = url
                            existing.error_msg = ""
                            existing.status = "queued"
                            self._update_item_db(existing)
                            self._bump_version()
                            if auto_start:
                                asyncio.create_task(self.start_task(existing.id))
                            added.append(existing)
                            matched_existing = True
                            break
                if matched_existing:
                    continue

            host = detect_host(url)
            clean_path = url.split("?")[0].split("#")[0].rstrip("/")
            base_candidate = os.path.basename(clean_path)
            if base_candidate in ("", "file", "download"):
                parts = [p for p in clean_path.split("/") if p and p not in ("file", "download")]
                base_candidate = parts[-1] if parts else "Analizando..."
            item = DownloadItem(
                id=0,
                url=url,
                filename=filename_hint or base_candidate or "Analizando...",
                host=host,
                save_dir=target_dir,
                status="resolving",
                headers=dict(custom_headers) if custom_headers else {},
                extra={
                    "quality": quality,
                    "container": clean_container or "General",
                    "filename_hint": filename_hint,
                    "source_page": source_page,
                },
            )
            item.id = self._insert_item_db(item)
            self.items[item.id] = item
            self._bump_version()
            added.append(item)
            asyncio.create_task(self._resolve_and_enqueue(item, auto_start=auto_start))

        return added

    async def _resolve_and_enqueue(self, item: DownloadItem, auto_start: bool = True) -> None:
        try:
            quality = item.extra.get("quality", "best")
            if is_video_or_stream_url(item.url):
                resolved = await self.video_extractor.probe(
                    item.url,
                    quality=quality,
                    custom_headers=item.headers if item.headers else None,
                )
            else:
                resolved = await self.cloud_extractor.resolve(item.url, session=self._session)

            item.direct_url = resolved.direct_url
            hint = str(item.extra.get("filename_hint") or "").strip()
            generic_hls_names = ("download.bin", "index", "master", "playlist", "stream", "video", "manifest")
            res_base_low = os.path.splitext(resolved.filename)[0].lower()
            if hint and any(res_base_low.startswith(g) for g in generic_hls_names):
                if not os.path.splitext(hint)[1]:
                    hint = f"{hint}.mp4"
                raw_fname = _sanitize_filename(hint)
            elif any(res_base_low.startswith(g) for g in generic_hls_names):
                url_path = urllib.parse.urlparse(item.url).path.strip("/")
                parts = [p for p in url_path.split("/") if p]
                token_tag = parts[-2][:8] if len(parts) >= 2 else f"stream_{item.id}"
                raw_fname = _sanitize_filename(f"{os.path.splitext(resolved.filename)[0]}_{token_tag}.mp4")
            else:
                raw_fname = resolved.filename

            item.filename = self._ensure_unique_filename(
                item.save_dir, raw_fname, item.id, self.items
            )
            item.host = resolved.host
            item.total_size = resolved.size
            merged_headers = dict(resolved.headers)
            if item.headers:
                merged_headers.update(item.headers)
            item.headers = merged_headers
            item.extractor_type = resolved.extractor_type
            item.extra.update(resolved.extra)
            item.error_msg = ""
            item.status = "queued"
            self._update_item_db(item)
            self._bump_version()

            if auto_start:
                await self.start_task(item.id)

        except ExtractorError as exc:
            item.status = "error"
            item.error_msg = str(exc)
            self._update_item_db(item)
            self._bump_version()
        except Exception as exc:
            item.status = "error"
            item.error_msg = f"Error inesperado: {exc}"
            self._update_item_db(item)
            self._bump_version()

    async def start_task(self, item_id: int) -> None:
        item = self.items.get(item_id)
        if not item or item.status == "complete":
            return

        if not item.direct_url or item.status == "error":
            item.status = "resolving"
            item.error_msg = ""
            self._update_item_db(item)
            self._bump_version()
            await self._resolve_and_enqueue(item, auto_start=True)
            return

        if item.extractor_type == "video":
            await self._start_video_task(item)
            return

        if item.gid:
            try:
                st = await self.rpc_call("aria2.tellStatus", [item.gid, ["status"]])
                if st and st.get("status") == "paused":
                    await self.rpc_call("aria2.unpause", [item.gid])
                    item.status = "active"
                    self._update_item_db(item)
                    self._bump_version()
                    return
            except Exception:
                item.gid = ""

        out_filename = (
            item.extra.get("encrypted_filename", item.filename)
            if item.extractor_type == "mega"
            else item.filename
        )
        options: Dict[str, Any] = {
            "dir": item.save_dir,
            "max-connection-per-server": "16",
            "split": "16",
            "min-split-size": "1M",
            "continue": "true",
        }
        if item.extractor_type not in ("torrent_file", "magnet"):
            options["out"] = out_filename
        if item.headers:
            options["header"] = [f"{k}: {v}" for k, v in item.headers.items()]

        try:
            if item.extractor_type == "torrent_file" and os.path.isfile(item.direct_url):
                with open(item.direct_url, "rb") as tf:
                    b64_torrent = base64.b64encode(tf.read()).decode("ascii")
                gid = await self.rpc_call("aria2.addTorrent", [b64_torrent, [], options])
            else:
                gid = await self.rpc_call("aria2.addUri", [[item.direct_url], options])
            item.gid = str(gid)
            item.status = "active"
            item.error_msg = ""
            self._update_item_db(item)
            self._bump_version()
        except Exception as exc:
            item.status = "error"
            item.error_msg = f"Fallo al iniciar en aria2c: {exc}"
            self._update_item_db(item)
            self._bump_version()

    async def _start_video_task(self, item: DownloadItem) -> None:
        if item.id in self._video_tasks and not self._video_tasks[item.id].done():
            return

        cancel_ev = threading.Event()
        self._video_cancel_events[item.id] = cancel_ev
        item.status = "active"
        item.error_msg = ""
        self._update_item_db(item)
        self._bump_version()

        def _on_progress(prog: VideoTaskProgress) -> None:
            item.completed_size = prog.downloaded_bytes
            if prog.total_bytes > 0:
                item.total_size = prog.total_bytes
            item.speed = prog.speed
            item.eta = prog.eta
            if prog.status in ("active", "merging"):
                item.status = prog.status
            self._bump_version()

        async def _runner() -> None:
            try:
                quality = item.extra.get("quality", "best")
                await self.video_extractor.download_stream(
                    url=item.url,
                    save_dir=item.save_dir,
                    filename=item.filename,
                    quality=quality,
                    progress_cb=_on_progress,
                    cancel_event=cancel_ev,
                    custom_headers=item.headers if item.headers else None,
                )
                item.status = "complete"
                item.speed = 0.0
                item.eta = 0
                if item.total_size == 0 and item.completed_size > 0:
                    item.total_size = item.completed_size
                elif item.total_size > 0:
                    item.completed_size = item.total_size
                self._update_item_db(item)
            except DownloadCancelledError:
                if item.status not in ("paused", "stopped"):
                    item.status = "paused"
                item.speed = 0.0
                item.eta = 0
                self._update_item_db(item)
            except Exception as exc:
                item.status = "error"
                item.error_msg = str(exc)
                item.speed = 0.0
                item.eta = 0
                self._update_item_db(item)
            finally:
                self._video_tasks.pop(item.id, None)
                self._video_cancel_events.pop(item.id, None)
                self._bump_version()

        self._video_tasks[item.id] = asyncio.create_task(_runner())

    async def pause_task(self, item_id: int) -> None:
        item = self.items.get(item_id)
        if not item or item.status not in ("active", "queued", "resolving", "merging"):
            return

        if item.extractor_type == "video":
            ev = self._video_cancel_events.get(item.id)
            if ev:
                ev.set()
            item.status = "paused"
            item.speed = 0.0
            item.eta = 0
            self._update_item_db(item)
            self._bump_version()
            return

        if item.gid:
            try:
                await self.rpc_call("aria2.pause", [item.gid])
            except Exception:
                pass
        item.status = "paused"
        item.speed = 0.0
        item.eta = 0
        self._update_item_db(item)
        self._bump_version()

    async def stop_task(self, item_id: int) -> None:
        item = self.items.get(item_id)
        if not item or item.status == "complete":
            return

        if item.extractor_type == "video":
            ev = self._video_cancel_events.get(item.id)
            if ev:
                ev.set()
            item.status = "stopped"
            item.speed = 0.0
            item.eta = 0
            self._update_item_db(item)
            self._bump_version()
            return

        if item.gid:
            try:
                await self.rpc_call("aria2.forceRemove", [item.gid])
            except Exception:
                pass
            item.gid = ""

        item.status = "stopped"
        item.speed = 0.0
        item.eta = 0
        self._update_item_db(item)
        self._bump_version()

    async def remove_task(self, item_id: int, delete_files: bool = False) -> None:
        item = self.items.get(item_id)
        if not item:
            return

        await self.stop_task(item_id)
        self._video_tasks.pop(item_id, None)
        self._video_cancel_events.pop(item_id, None)
        if item.gid:
            try:
                await self.rpc_call("aria2.removeDownloadResult", [item.gid])
            except Exception:
                pass

        if delete_files:
            candidates = [
                os.path.join(item.save_dir, item.filename),
                os.path.join(item.save_dir, f"{item.filename}.aria2"),
                os.path.join(item.save_dir, f"{item.filename}.part"),
            ]
            enc_name = item.extra.get("encrypted_filename")
            if enc_name:
                candidates.append(os.path.join(item.save_dir, enc_name))
                candidates.append(os.path.join(item.save_dir, f"{enc_name}.aria2"))
            for path in candidates:
                try:
                    os.remove(path)
                except OSError:
                    pass

        self.items.pop(item_id, None)
        self._delete_item_db(item_id)
        self._bump_version()

    async def clear_completed(self) -> int:
        """Remove all completed downloads from the queue and database without deleting finished files on disk."""
        completed_ids = [
            item_id for item_id, item in list(self.items.items()) if item.status == "complete"
        ]
        if not completed_ids:
            return 0
        for item_id in completed_ids:
            item = self.items.pop(item_id, None)
            if item and item.gid:
                try:
                    await self.rpc_call("aria2.removeDownloadResult", [item.gid])
                except Exception:
                    pass
        with self._db_lock, self._db_conn as conn:
            conn.executemany("DELETE FROM downloads WHERE id=?", [(iid,) for iid in completed_ids])
        self._bump_version()
        return len(completed_ids)

    def assign_container(self, item_ids: List[int], container_name: str) -> None:
        """Assign one or more download items to a named container (package)."""
        clean_name = container_name.strip() or "General"
        now = time.time()
        rows = []
        for item_id in item_ids:
            item = self.items.get(item_id)
            if item:
                item.extra["container"] = clean_name
                rows.append((json.dumps(item.extra), now, item.id))
        if rows:
            with self._db_lock, self._db_conn as conn:
                conn.executemany("UPDATE downloads SET extra_json=?, updated_at=? WHERE id=?", rows)
            self._bump_version()

    def get_containers(self) -> List[str]:
        """Return sorted list of unique container names currently in use."""
        names = {"General"}
        for item in self.items.values():
            names.add(item.container_name)
        return sorted(names, key=lambda x: (0 if x == "General" else 1, x.lower()))

    async def start_all(self) -> None:
        for item_id, item in list(self.items.items()):
            if item.status in ("queued", "paused", "stopped", "error"):
                await self.start_task(item_id)

    async def pause_all(self) -> None:
        for item_id, item in list(self.items.items()):
            if item.status in ("active", "queued", "resolving"):
                await self.pause_task(item_id)

    async def refresh_statuses(self) -> tuple[float, int]:
        if not self._running:
            return 0.0, 0

        # Every 2.5 seconds, check if any MediaFire item waiting on browser verification
        # now has a live (non-expired) resolved direct link in Firefox places.sqlite
        now = time.time()
        if now - self._last_browser_sync >= 2.5:
            self._last_browser_sync = now
            waiting_mf = [
                it
                for it in self.items.values()
                if it.host == "MediaFire"
                and not it.direct_url
                and it.status in ("error", "paused", "queued")
            ]
            if waiting_mf and self._session:
                resolved_map = await asyncio.to_thread(
                    self.cloud_extractor.find_browser_resolved_mediafire_links
                )
                for it in waiting_mf:
                    qk = self.cloud_extractor.extract_mediafire_quickkey(it.url)
                    if qk and qk in resolved_map:
                        cand_url = resolved_map[qk]
                        is_ok, _, _, _ = await self.cloud_extractor.verify_mediafire_direct_link(
                            self._session, cand_url, expected_size=it.total_size
                        )
                        if is_ok:
                            it.direct_url = cand_url
                            it.error_msg = ""
                            it.status = "queued"
                            self._update_item_db(it)
                            self._bump_version()
                            asyncio.create_task(self.start_task(it.id))

        keys = [
            "gid",
            "status",
            "totalLength",
            "completedLength",
            "downloadSpeed",
            "errorCode",
            "errorMessage",
            "followedBy",
            "bittorrent",
        ]
        poll_items = [
            item
            for item in list(self.items.values())
            if item.gid
            and item.extractor_type != "video"
            and item.status in ("active", "queued", "resolving")
            and item.id not in self._decrypt_tasks
        ]

        if poll_items:
            infos = await self.rpc_multicall_status([it.gid for it in poll_items], keys)
            mutated = False
            for item, info in zip(poll_items, infos):
                if not info:
                    continue

                # If a Magnet metadata download spawned a follow-up BitTorrent GID, switch to it!
                followed_by = info.get("followedBy")
                if followed_by and isinstance(followed_by, list) and followed_by[0]:
                    item.gid = str(followed_by[0])
                    self._update_item_db(item)
                    mutated = True
                    try:
                        info = await self.rpc_call("aria2.tellStatus", [item.gid, keys])
                    except Exception:
                        continue
                    if not info:
                        continue

                bt_info = info.get("bittorrent") or {}
                bt_name = (bt_info.get("info") or {}).get("name")
                if bt_name and (item.filename.startswith("magnet_") or item.filename.endswith(".torrent")):
                    if item.filename != bt_name:
                        item.filename = bt_name
                        mutated = True

                a_status = info.get("status", "")
                tot = int(info.get("totalLength") or 0)
                comp = int(info.get("completedLength") or 0)
                spd = float(info.get("downloadSpeed") or 0.0)

                if tot > 0 and not (item.total_size > 10 * 1024 * 1024 and tot < 512 * 1024):
                    if item.total_size != tot:
                        item.total_size = tot
                        mutated = True
                if item.completed_size != comp:
                    item.completed_size = comp
                    mutated = True
                if item.speed != spd:
                    item.speed = spd
                    mutated = True
                new_eta = int((item.total_size - item.completed_size) / spd) if (spd > 0 and item.total_size > item.completed_size) else 0
                if item.eta != new_eta:
                    item.eta = new_eta
                    mutated = True

                if a_status == "active":
                    if item.status != "active":
                        item.status = "active"
                        mutated = True
                elif a_status == "waiting":
                    if item.status != "queued":
                        item.status = "queued"
                        mutated = True
                elif a_status == "paused":
                    item.speed = 0.0
                    if item.status != "paused":
                        item.status = "paused"
                        mutated = True
                elif a_status == "complete":
                    item.speed = 0.0
                    item.eta = 0
                    target_file = os.path.join(item.save_dir, item.filename)
                    if item.host == "MediaFire":
                        try:
                            if os.path.getsize(target_file) < 256 * 1024:
                                with open(target_file, "rb") as tf:
                                    head_bytes = tf.read(512).lstrip()
                                if head_bytes.startswith((b"<!DOCTYPE", b"<html", b"<HTML")):
                                    os.remove(target_file)
                                    item.direct_url = ""
                                    item.gid = ""
                                    item.completed_size = 0
                                    item.status = "error"
                                    item.error_msg = (
                                        "El enlace temporal expiró (página HTML recibida). "
                                        "Haz clic en «Verificar en navegador» para renovarlo."
                                    )
                                    self._update_item_db(item)
                                    mutated = True
                                    continue
                        except OSError:
                            pass
                    if item.extractor_type == "mega" and item.id not in self._decrypt_tasks and item.status != "complete":
                        self._decrypt_tasks.add(item.id)
                        asyncio.create_task(self._finalize_mega_decryption(item))
                        mutated = True
                    elif item.extractor_type != "mega" and item.status != "complete":
                        item.status = "complete"
                        self._update_item_db(item)
                        mutated = True
                elif a_status == "error":
                    item.status = "error"
                    item.speed = 0.0
                    item.eta = 0
                    item.error_msg = info.get("errorMessage") or f"Código aria2: {info.get('errorCode')}"
                    self._update_item_db(item)
                    mutated = True

            if mutated:
                self._bump_version()

        total_speed = 0.0
        active_count = 0
        for it in self.items.values():
            st = it.status
            if st in ("active", "merging"):
                total_speed += it.speed
            if st in ("active", "resolving", "merging", "decrypting"):
                active_count += 1
        return total_speed, active_count

    async def _finalize_mega_decryption(self, item: DownloadItem) -> None:
        item.status = "decrypting"
        self._bump_version()
        enc_name = item.extra.get("encrypted_filename", f".{item.filename}.mega_enc")
        enc_path = os.path.join(item.save_dir, enc_name)
        out_path = os.path.join(item.save_dir, item.filename)
        key_hex = item.extra.get("mega_key_hex", "")
        iv_hex = item.extra.get("mega_iv_hex", "")

        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(
                None,
                decrypt_mega_file,
                enc_path,
                out_path,
                key_hex,
                iv_hex,
                None,
            )
            item.status = "complete"
            item.completed_size = item.total_size
            self._update_item_db(item)
        except Exception as exc:
            item.status = "error"
            item.error_msg = f"Error al descifrar MEGA: {exc}"
            self._update_item_db(item)
        finally:
            self._decrypt_tasks.discard(item.id)
            self._bump_version()
