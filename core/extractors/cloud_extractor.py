"""
Modular Cloud & Direct URL Extractors for Mint Downloader.
Resolves direct download links, headers, filenames, and encryption keys for:
- MEGA (mega.nz / mega.co.nz) with AES-CBC metadata & AES-CTR file decryption
- MediaFire (mediafire.com) including scrambled-url decoding
- Google Drive (drive.google.com / drive.usercontent.google.com) bypassing virus-scan prompts
- OneDrive / SharePoint (1drv.ms / onedrive.live.com / sharepoint.com)
- 1fichier (1fichier.com)
- Direct HTTP/HTTPS/FTP links
"""

from __future__ import annotations

import base64
import glob
import html
import json
import os
import re
import shutil
import sqlite3
import struct
import tempfile
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

import aiohttp

try:
    from Cryptodome.Cipher import AES
    from Cryptodome.Util import Counter
    HAS_CRYPTODOME = True
except ImportError:
    HAS_CRYPTODOME = False
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.backends import default_backend


DEFAULT_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)


class ExtractorError(Exception):
    """Base error for URL extraction failures."""


class DeadLinkError(ExtractorError):
    """Raised when a file has been deleted, expired, or returns 404."""


class ProtectedFileError(ExtractorError):
    """Raised when a file requires a password, login, or private permission."""


class BandwidthLimitError(ExtractorError):
    """Raised when the host enforces a quota, cooldown, or bandwidth limit."""


class AntiBotChallengeError(ExtractorError):
    """Raised when Cloudflare Turnstile / anti-bot challenge gates the page."""


@dataclass
class FolderEntry:
    url: str
    filename: str
    size: int = 0
    subfolder: str = ""
    host: str = "MediaFire"


@dataclass
class ResolvedDownload:
    original_url: str
    direct_url: str
    filename: str
    host: str
    size: int = 0
    headers: Dict[str, str] = field(default_factory=dict)
    extractor_type: str = "direct"
    extra: Dict[str, Any] = field(default_factory=dict)


def detect_host(url: str) -> str:
    """Return a clean human-readable host/provider name for a URL."""
    try:
        parsed = urllib.parse.urlparse(url)
        netloc = (parsed.netloc or "").lower()
    except Exception:
        netloc = ""

    if any(k in netloc for k in ("mega.nz", "mega.co.nz")):
        return "MEGA"
    if "mediafire.com" in netloc:
        return "MediaFire"
    if any(k in netloc for k in ("drive.google.com", "docs.google.com", "drive.usercontent.google.com")):
        return "Google Drive"
    if any(k in netloc for k in ("1drv.ms", "onedrive.live.com", "sharepoint.com")):
        return "OneDrive"
    if "1fichier.com" in netloc:
        return "1fichier"
    if url.lower().startswith("magnet:?"):
        return "BitTorrent (Magnet)"
    if url.lower().endswith(".torrent") or (url.startswith("/") and url.lower().endswith(".torrent")):
        return "BitTorrent (.torrent)"
    if any(k in netloc for k in ("youtube.com", "youtu.be")):
        return "YouTube"
    if any(k in netloc for k in ("twitch.tv", "vimeo.com", "dailymotion.com", "tiktok.com", "twitter.com", "x.com")):
        return netloc.replace("www.", "")
    if ".m3u8" in url.lower():
        return "HLS Stream"
    return netloc.replace("www.", "") or "Directo"


def _sanitize_filename(name: str) -> str:
    name = urllib.parse.unquote(name).strip()
    name = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", name)
    return name or "download.bin"


def _extract_filename_from_cd(content_disposition: str) -> Optional[str]:
    if not content_disposition:
        return None
    m = re.search(r"filename\*\s*=\s*UTF-8''([^;\r\n]+)", content_disposition, re.I)
    if m:
        return _sanitize_filename(urllib.parse.unquote(m.group(1).strip(' "\'')))
    m = re.search(r'filename\s*=\s*"([^"]+)"', content_disposition, re.I)
    if m:
        return _sanitize_filename(m.group(1))
    m = re.search(r"filename\s*=\s*([^;\r\n]+)", content_disposition, re.I)
    if m:
        return _sanitize_filename(m.group(1).strip(' "\''))
    return None


def _mega_base64_decode(data: str) -> bytes:
    data = data.replace("-", "+").replace("_", "/").replace(",", "")
    pad = (-len(data)) % 4
    if pad:
        data += "=" * pad
    return base64.b64decode(data)


def _mega_bytes_to_a32(b: bytes) -> tuple[int, ...]:
    if len(b) % 4 != 0:
        b += b"\x00" * (4 - (len(b) % 4))
    return struct.unpack(f">{len(b) // 4}I", b)


def _mega_a32_to_bytes(a: tuple[int, ...] | list[int]) -> bytes:
    return struct.pack(f">{len(a)}I", *a)


def _mega_cbc_decrypt(data: bytes, key_bytes: bytes) -> bytes:
    if len(data) % 16 != 0:
        data += b"\x00" * (16 - (len(data) % 16))
    iv = b"\x00" * 16
    if HAS_CRYPTODOME:
        cipher = AES.new(key_bytes, AES.MODE_CBC, iv=iv)
        return cipher.decrypt(data)
    cipher = Cipher(algorithms.AES(key_bytes), modes.CBC(iv), backend=default_backend())
    decryptor = cipher.decryptor()
    return decryptor.update(data) + decryptor.finalize()


def parse_mega_url(url: str) -> tuple[str, str]:
    """Extract (file_id, file_key_b64) from modern or legacy MEGA URLs."""
    m = re.search(r"mega\.(?:co\.)?nz/file/([a-zA-Z0-9_-]+)#([a-zA-Z0-9_-]+)", url)
    if m:
        return m.group(1), m.group(2)
    m = re.search(r"mega\.(?:co\.)?nz/#!([a-zA-Z0-9_-]+)!([a-zA-Z0-9_-]+)", url)
    if m:
        return m.group(1), m.group(2)
    if "mega.nz/folder/" in url or "mega.nz/#F!" in url:
        raise ProtectedFileError("Los enlaces de carpetas completas de MEGA requieren enlaces de archivo individual.")
    raise ExtractorError("Formato de enlace de MEGA inválido o sin clave de descifrado (#key).")


def unpack_mega_key(key_b64: str) -> tuple[bytes, bytes]:
    """Return (aes_key_16_bytes, ctr_iv_16_bytes) from MEGA URL key string."""
    raw = _mega_base64_decode(key_b64)
    a32 = _mega_bytes_to_a32(raw)
    if len(a32) < 8:
        raise ExtractorError("Clave de cifrado de MEGA incompleta o corrupta.")
    k = (a32[0] ^ a32[4], a32[1] ^ a32[5], a32[2] ^ a32[6], a32[3] ^ a32[7])
    iv = (a32[4], a32[5], 0, 0)
    return _mega_a32_to_bytes(k), _mega_a32_to_bytes(iv)


def decrypt_mega_attributes(at_b64: str, aes_key: bytes) -> dict[str, Any]:
    raw_at = _mega_base64_decode(at_b64)
    dec = _mega_cbc_decrypt(raw_at, aes_key).rstrip(b"\x00")
    if dec.startswith(b"MEGA"):
        json_str = dec[4:].decode("utf-8", errors="replace")
        return json.loads(json_str)
    raise ExtractorError("No se pudieron descifrar los metadatos del archivo de MEGA (clave incorrecta).")


def decrypt_mega_file(
    encrypted_path: str,
    output_path: str,
    key_hex: str,
    iv_hex: str,
    progress_cb: Optional[Callable[[int, int], None]] = None,
) -> None:
    """
    Decrypt an AES-128-CTR encrypted MEGA file downloaded by aria2c into output_path.
    Processes in 4 MB chunks for >1 GB/s throughput and <10 MB RAM usage.
    """
    key_bytes = bytes.fromhex(key_hex)
    iv_bytes = bytes.fromhex(iv_hex)
    total_size = os.path.getsize(encrypted_path)
    processed = 0

    if HAS_CRYPTODOME:
        initial_value = int.from_bytes(iv_bytes, byteorder="big")
        ctr = Counter.new(128, initial_value=initial_value)
        cipher = AES.new(key_bytes, AES.MODE_CTR, counter=ctr)
        decrypt_fn = cipher.decrypt
    else:
        cipher = Cipher(algorithms.AES(key_bytes), modes.CTR(iv_bytes), backend=default_backend())
        decryptor = cipher.decryptor()
        decrypt_fn = decryptor.update

    tmp_out = output_path + ".dec_tmp"
    with open(encrypted_path, "rb") as fin, open(tmp_out, "wb") as fout:
        while True:
            chunk = fin.read(4 * 1024 * 1024)
            if not chunk:
                break
            fout.write(decrypt_fn(chunk))
            processed += len(chunk)
            if progress_cb:
                progress_cb(processed, total_size)

    os.replace(tmp_out, output_path)
    try:
        os.remove(encrypted_path)
    except OSError:
        pass


class CloudExtractor:
    """Unified asynchronous cloud and direct URL resolver."""

    MEGA_ERRORS = {
        -2: (ExtractorError, "Argumentos inválidos en la API de MEGA (EARGS)."),
        -3: (ExtractorError, "Error temporal del servidor de MEGA (EAGAIN). Reintente."),
        -4: (BandwidthLimitError, "Límite de tasa alcanzado en MEGA (ERATELIMIT)."),
        -6: (DeadLinkError, "Demasiadas direcciones IP accediendo al enlace de MEGA."),
        -9: (DeadLinkError, "El archivo de MEGA no existe o fue eliminado (ENOENT)."),
        -11: (ProtectedFileError, "Acceso restringido al archivo de MEGA (EACCESS)."),
        -16: (DeadLinkError, "El archivo de MEGA fue bloqueado o suspendido (EBLOCKED)."),
        -17: (BandwidthLimitError, "Límite de ancho de banda de transferencia de MEGA excedido (EOVERQUOTA)."),
        -18: (ExtractorError, "El archivo de MEGA no está disponible temporalmente (ETEMPUNAVAIL)."),
    }

    def __init__(self, timeout: int = 25) -> None:
        self.timeout = aiohttp.ClientTimeout(total=timeout)

    @staticmethod
    def extract_mediafire_quickkey(url: str) -> str:
        """Extract the MediaFire quickkey from either a /file/<qk>/... or download*.mediafire.com/.../<qk>/... URL."""
        m_dl = re.search(r"download[0-9]*\.mediafire\.com/[^/]+/([a-zA-Z0-9]+)/", url)
        if m_dl:
            return m_dl.group(1)
        m_qk = re.search(r"mediafire\.com/(?:file|file_premium|download)/([a-zA-Z0-9]+)", url)
        if m_qk:
            return m_qk.group(1)
        m_param = re.search(r"[?&]quickkey=([a-zA-Z0-9]+)", url)
        if m_param:
            return m_param.group(1)
        return ""

    @staticmethod
    async def verify_mediafire_direct_link(
        session: aiohttp.ClientSession,
        direct_url: str,
        expected_size: int = 0,
    ) -> tuple[bool, str, Optional[str], int]:
        """
        Verify that a `https://download*.mediafire.com/...` URL is still live and has not
        expired into a `302 Found -> download_repair.php` HTML page.
        Returns (is_valid, final_url, filename, content_length).
        """
        try:
            async with session.head(direct_url, allow_redirects=False) as hresp:
                if hresp.status in (301, 302, 303, 307, 308):
                    loc = hresp.headers.get("Location", "")
                    if "download_repair.php" in loc or "error.php" in loc or "action=check" in loc:
                        return False, "", None, 0
                if hresp.status >= 400:
                    return False, "", None, 0
                ctype = (hresp.headers.get("Content-Type") or "").lower()
                if "text/html" in ctype:
                    return False, "", None, 0
                clen = int(hresp.headers.get("Content-Length", 0) or 0)
                if expected_size > 1024 * 1024 and 0 < clen < expected_size // 10:
                    return False, "", None, 0
                cd_name = _extract_filename_from_cd(hresp.headers.get("Content-Disposition", ""))
                return True, direct_url, cd_name, clen
        except Exception:
            return False, "", None, 0

    @staticmethod
    def find_browser_resolved_mediafire_links(quickkey: Optional[str] = None) -> Dict[str, str]:
        """
        Read-only lookup in local Firefox profile history (places.sqlite) for recently
        resolved `https://download*.mediafire.com/.../<quickkey>/...` direct links
        that the user already opened in their browser.
        Returns a mapping of {quickkey: direct_download_url}.
        """
        found: Dict[str, str] = {}
        ff_root = os.path.expanduser("~/.mozilla/firefox")
        if not os.path.isdir(ff_root):
            return found

        for places_path in glob.glob(os.path.join(ff_root, "*.default*", "places.sqlite")):
            tmp_fd, tmp_path = tempfile.mkstemp(prefix="mintdl_places_", suffix=".sqlite")
            os.close(tmp_fd)
            try:
                shutil.copy2(places_path, tmp_path)
                for wal_ext in ("-wal", "-shm"):
                    if os.path.exists(places_path + wal_ext):
                        shutil.copy2(places_path + wal_ext, tmp_path + wal_ext)
                with sqlite3.connect(tmp_path) as conn:
                    if quickkey:
                        rows = conn.execute(
                            "SELECT url FROM moz_places "
                            "WHERE url LIKE 'https://download%.mediafire.com/%/' || ? || '/%' "
                            "ORDER BY last_visit_date DESC LIMIT 5",
                            (quickkey,),
                        ).fetchall()
                    else:
                        rows = conn.execute(
                            "SELECT url FROM moz_places "
                            "WHERE url LIKE 'https://download%.mediafire.com/%' "
                            "ORDER BY last_visit_date DESC LIMIT 100"
                        ).fetchall()
                    for (u,) in rows:
                        qk = CloudExtractor.extract_mediafire_quickkey(u)
                        if qk and qk not in found:
                            found[qk] = u
            except Exception:
                pass
            finally:
                for ext in ("", "-wal", "-shm"):
                    try:
                        if os.path.exists(tmp_path + ext):
                            os.remove(tmp_path + ext)
                    except OSError:
                        pass
        return found

    @staticmethod
    def load_browser_mediafire_cookies() -> tuple[Dict[str, str], str]:
        """
        Load active MediaFire cookies and matching Firefox User-Agent from the user's
        Firefox profile in read-only mode so authenticated sessions are preserved.
        """
        cookies: Dict[str, str] = {}
        ua = DEFAULT_UA
        ff_root = os.path.expanduser("~/.mozilla/firefox")
        if not os.path.isdir(ff_root):
            return cookies, ua

        for compat_path in glob.glob(os.path.join(ff_root, "*.default*", "compatibility.ini")):
            try:
                with open(compat_path, "r", encoding="utf-8", errors="ignore") as f:
                    m_ver = re.search(r"LastVersion=([0-9]+\.[0-9]+)", f.read())
                    if m_ver:
                        ver = m_ver.group(1)
                        ua = f"Mozilla/5.0 (X11; Linux x86_64; rv:{ver}) Gecko/20100101 Firefox/{ver}"
                        break
            except Exception:
                pass

        for cookie_db in glob.glob(os.path.join(ff_root, "*.default*", "cookies.sqlite")):
            tmp_fd, tmp_path = tempfile.mkstemp(prefix="mintdl_cookies_", suffix=".sqlite")
            os.close(tmp_fd)
            try:
                shutil.copy2(cookie_db, tmp_path)
                for wal_ext in ("-wal", "-shm"):
                    if os.path.exists(cookie_db + wal_ext):
                        shutil.copy2(cookie_db + wal_ext, tmp_path + wal_ext)
                with sqlite3.connect(tmp_path) as conn:
                    for name, val in conn.execute(
                        "SELECT name, value FROM moz_cookies WHERE host LIKE '%mediafire.com%'"
                    ):
                        if name and val:
                            cookies[name] = val
            except Exception:
                pass
            finally:
                for ext in ("", "-wal", "-shm"):
                    try:
                        if os.path.exists(tmp_path + ext):
                            os.remove(tmp_path + ext)
                    except OSError:
                        pass
        return cookies, ua

    def is_folder_url(self, url: str) -> bool:
        """Return True if the URL points to a multi-file cloud folder."""
        parsed = urllib.parse.urlparse(url)
        netloc = (parsed.netloc or "").lower()
        path = (parsed.path or "").lower()
        if "mediafire.com" in netloc and ("/folder/" in path or "folderkey=" in url.lower()):
            return True
        return False

    async def expand_folder(self, url: str) -> list[FolderEntry]:
        """
        Expand a cloud folder URL (such as MediaFire /folder/<key>/#<subkey>)
        recursively into a list of individual file entries with relative subfolder paths.
        """
        host = detect_host(url)
        if host == "MediaFire" and self.is_folder_url(url):
            return await self._expand_mediafire_folder(url)
        return []

    async def _expand_mediafire_folder(self, url: str) -> list[FolderEntry]:
        """
        Resolve MediaFire folder contents via the public MediaFire REST API v1.5.
        If the URL contains a hash fragment (#subfolder_key), prioritizes that specific
        folder key; otherwise starts at the main folder key in the path.
        """
        parsed = urllib.parse.urlparse(url)
        frag = (parsed.fragment or "").strip().strip("/")
        m_path = re.search(r"/folder/([a-zA-Z0-9,_-]+)", parsed.path)
        path_key = m_path.group(1).split(",")[0] if m_path else ""

        root_key = frag if (frag and re.fullmatch(r"[a-zA-Z0-9_-]{10,}", frag)) else path_key
        if not root_key:
            raise ExtractorError("No se pudo extraer el identificador de carpeta (folder_key) de MediaFire.")

        headers = {"User-Agent": DEFAULT_UA}
        entries: list[FolderEntry] = []

        async with aiohttp.ClientSession(timeout=self.timeout, headers=headers) as session:
            root_name = await self._fetch_mediafire_folder_name(session, root_key)
            await self._walk_mediafire_folder(
                session=session,
                folder_key=root_key,
                rel_dir=_sanitize_filename(root_name) if root_name else "",
                out=entries,
                depth=0,
            )

        if not entries:
            raise DeadLinkError("La carpeta de MediaFire está vacía o es privada.")
        return entries

    async def _fetch_mediafire_folder_name(self, session: aiohttp.ClientSession, folder_key: str) -> str:
        info_url = (
            f"https://www.mediafire.com/api/1.5/folder/get_info.php"
            f"?folder_key={folder_key}&response_format=json"
        )
        try:
            async with session.get(info_url) as resp:
                data = await resp.json(content_type=None)
                f_info = data.get("response", {}).get("folder_info") or {}
                if not f_info:
                    infos = data.get("response", {}).get("folder_infos") or []
                    if infos:
                        f_info = infos[0]
                return str(f_info.get("name") or folder_key)
        except Exception:
            return folder_key

    async def _walk_mediafire_folder(
        self,
        session: aiohttp.ClientSession,
        folder_key: str,
        rel_dir: str,
        out: list[FolderEntry],
        depth: int,
    ) -> None:
        if depth > 5:
            return

        # 1. Fetch files in chunks
        chunk = 1
        while True:
            files_api = (
                f"https://www.mediafire.com/api/1.5/folder/get_content.php"
                f"?folder_key={folder_key}&content_type=files&chunk={chunk}&chunk_size=100&response_format=json"
            )
            async with session.get(files_api) as resp:
                data = await resp.json(content_type=None)
            fc = data.get("response", {}).get("folder_content", {})
            files = fc.get("files") or []
            for f in files:
                qk = f.get("quickkey", "")
                fname = _sanitize_filename(f.get("filename") or f"mediafire_{qk}.bin")
                fsize = int(f.get("size") or 0)
                links = f.get("links") or {}
                normal_dl = links.get("normal_download") or f"https://www.mediafire.com/file/{qk}/{fname}/file"
                out.append(
                    FolderEntry(
                        url=normal_dl,
                        filename=fname,
                        size=fsize,
                        subfolder=rel_dir,
                        host="MediaFire",
                    )
                )
            if fc.get("more_chunks") != "yes" or not files:
                break
            chunk += 1

        # 2. Fetch subfolders recursively
        sub_api = (
            f"https://www.mediafire.com/api/1.5/folder/get_content.php"
            f"?folder_key={folder_key}&content_type=folders&chunk=1&chunk_size=100&response_format=json"
        )
        async with session.get(sub_api) as resp:
            sub_data = await resp.json(content_type=None)
        subfolders = sub_data.get("response", {}).get("folder_content", {}).get("folders") or []
        for sf in subfolders:
            sf_key = sf.get("folderkey")
            sf_name = _sanitize_filename(sf.get("name") or sf_key or "subcarpeta")
            if sf_key:
                next_rel = os.path.join(rel_dir, sf_name) if rel_dir else sf_name
                await self._walk_mediafire_folder(session, sf_key, next_rel, out, depth + 1)

    async def resolve(self, url: str) -> ResolvedDownload:
        host = detect_host(url)
        if host == "MEGA":
            return await self._resolve_mega(url)
        if host == "MediaFire":
            return await self._resolve_mediafire(url)
        if host == "Google Drive":
            return await self._resolve_gdrive(url)
        if host == "OneDrive":
            return await self._resolve_onedrive(url)
        if host == "1fichier":
            return await self._resolve_1fichier(url)
        return await self._resolve_direct(url, host)

    async def _resolve_mega(self, url: str) -> ResolvedDownload:
        file_id, file_key = parse_mega_url(url)
        aes_key, ctr_iv = unpack_mega_key(file_key)

        api_url = "https://g.api.mega.co.nz/cs?id=1"
        payload = [{"a": "g", "g": 1, "p": file_id}]

        async with aiohttp.ClientSession(timeout=self.timeout, headers={"User-Agent": DEFAULT_UA}) as session:
            async with session.post(api_url, json=payload) as resp:
                if resp.status >= 400:
                    raise ExtractorError(f"MEGA API devolvió HTTP {resp.status}")
                data = await resp.json(content_type=None)

        if not isinstance(data, list) or not data:
            raise ExtractorError(f"Respuesta inesperada de MEGA: {data}")

        item = data[0]
        if isinstance(item, int):
            exc_cls, msg = self.MEGA_ERRORS.get(item, (ExtractorError, f"Error de MEGA código {item}"))
            raise exc_cls(msg)

        if not isinstance(item, dict):
            raise ExtractorError("Formato de respuesta desconocido desde MEGA.")

        if "e" in item and isinstance(item["e"], int):
            exc_cls, msg = self.MEGA_ERRORS.get(item["e"], (ExtractorError, f"Error de MEGA código {item['e']}"))
            raise exc_cls(msg)

        dl_url = item.get("g")
        size = int(item.get("s", 0))
        at_b64 = item.get("at", "")

        if not dl_url:
            raise BandwidthLimitError("MEGA no entregó URL de descarga (posible límite de cuota de IP).")

        try:
            attrs = decrypt_mega_attributes(at_b64, aes_key)
            filename = _sanitize_filename(attrs.get("n", f"mega_{file_id}.bin"))
        except Exception:
            filename = f"mega_{file_id}.bin"

        return ResolvedDownload(
            original_url=url,
            direct_url=dl_url,
            filename=filename,
            host="MEGA",
            size=size,
            headers={"User-Agent": DEFAULT_UA},
            extractor_type="mega",
            extra={
                "mega_key_hex": aes_key.hex(),
                "mega_iv_hex": ctr_iv.hex(),
                "encrypted_filename": f".{filename}.mega_enc",
            },
        )

    async def _resolve_mediafire(self, url: str) -> ResolvedDownload:
        browser_cookies, browser_ua = self.load_browser_mediafire_cookies()
        headers: Dict[str, str] = {
            "User-Agent": browser_ua or DEFAULT_UA,
            "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
        }
        if browser_cookies:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in browser_cookies.items())

        # If the user already passed a direct download*.mediafire.com URL
        if re.search(r"https?://download[0-9]*\.mediafire\.com/", url, re.I):
            fname = _sanitize_filename(os.path.basename(urllib.parse.urlparse(url).path))
            size = 0
            async with aiohttp.ClientSession(timeout=self.timeout, headers={"User-Agent": DEFAULT_UA}) as session:
                try:
                    async with session.head(url, allow_redirects=True) as hresp:
                        if hresp.status >= 400:
                            raise DeadLinkError("El enlace directo de MediaFire expiró o no es válido.")
                        cd_name = _extract_filename_from_cd(hresp.headers.get("Content-Disposition", ""))
                        if cd_name:
                            fname = cd_name
                        size = int(hresp.headers.get("Content-Length", 0) or 0)
                except DeadLinkError:
                    raise
                except Exception:
                    pass
            return ResolvedDownload(
                original_url=url,
                direct_url=url,
                filename=fname,
                host="MediaFire",
                size=size,
                headers={"User-Agent": DEFAULT_UA},
                extractor_type="mediafire",
            )

        quickkey = self.extract_mediafire_quickkey(url)

        api_filename = ""
        api_size = 0
        async with aiohttp.ClientSession(timeout=self.timeout, headers=headers) as session:
            if quickkey:
                try:
                    info_api = (
                        f"https://www.mediafire.com/api/1.5/file/get_info.php"
                        f"?quick_key={quickkey}&response_format=json"
                    )
                    async with session.get(info_api) as iresp:
                        idata = await iresp.json(content_type=None)
                        finfo = idata.get("response", {}).get("file_info") or {}
                        if finfo:
                            api_filename = _sanitize_filename(finfo.get("filename") or "")
                            api_size = int(finfo.get("size") or 0)
                except Exception:
                    pass

                # 1. Check if the user's browser already resolved a valid direct link for this quickkey
                browser_links = self.find_browser_resolved_mediafire_links(quickkey)
                candidate_dl = browser_links.get(quickkey)
                if candidate_dl:
                    is_valid, real_url, cd_name, real_size = await self.verify_mediafire_direct_link(
                        session, candidate_dl, expected_size=api_size
                    )
                    if is_valid:
                        fname = cd_name or api_filename or _sanitize_filename(
                            os.path.basename(urllib.parse.urlparse(real_url).path)
                        )
                        return ResolvedDownload(
                            original_url=url,
                            direct_url=real_url,
                            filename=fname,
                            host="MediaFire",
                            size=real_size or api_size,
                            headers={"User-Agent": DEFAULT_UA},
                            extractor_type="mediafire",
                            extra={"quickkey": quickkey},
                        )

                # 2. Try MediaFire REST API get_links (succeeds if direct_download is enabled on file/account)
                try:
                    links_api = (
                        f"https://www.mediafire.com/api/1.5/file/get_links.php"
                        f"?quick_key={quickkey}&link_type=direct_download&response_format=json"
                    )
                    async with session.get(links_api) as lresp:
                        ldata = await lresp.json(content_type=None)
                        links_list = ldata.get("response", {}).get("links") or []
                        if links_list and links_list[0].get("direct_download"):
                            dl_api_url = links_list[0]["direct_download"]
                            return ResolvedDownload(
                                original_url=url,
                                direct_url=dl_api_url,
                                filename=api_filename or f"mediafire_{quickkey}.bin",
                                host="MediaFire",
                                size=api_size,
                                headers={"User-Agent": DEFAULT_UA},
                                extractor_type="mediafire",
                                extra={"quickkey": quickkey},
                            )
                except Exception:
                    pass

            # 3. Fetch web page using clean URL without ?action=check first
            clean_url = url.split("?")[0] if "action=check" in url else url
            async with session.get(clean_url, allow_redirects=True) as resp:
                if resp.status == 404 or "error.php" in str(resp.url):
                    raise DeadLinkError("El archivo de MediaFire no existe o fue eliminado.")
                if resp.status == 429:
                    raise BandwidthLimitError("MediaFire está limitando las peticiones (HTTP 429).")
                content_type = resp.headers.get("Content-Type", "")
                if not content_type.startswith("text/html"):
                    fname = (
                        _extract_filename_from_cd(resp.headers.get("Content-Disposition", ""))
                        or api_filename
                        or _sanitize_filename(os.path.basename(urllib.parse.urlparse(str(resp.url)).path))
                    )
                    return ResolvedDownload(
                        original_url=url,
                        direct_url=str(resp.url),
                        filename=fname,
                        host="MediaFire",
                        size=int(resp.headers.get("Content-Length", 0) or api_size),
                        headers={"User-Agent": DEFAULT_UA},
                        extractor_type="mediafire",
                        extra={"quickkey": quickkey},
                    )
                page = await resp.text(errors="replace")
                cf_mitigated = resp.headers.get("cf-mitigated", "")
                status_code = resp.status

            if status_code == 403 and (
                "challenge" in cf_mitigated.lower()
                or "Just a moment..." in page
                or "_cf_chl_opt" in page
                or "challenges.cloudflare.com" in page
            ):
                label = f" ({api_filename})" if api_filename else ""
                raise AntiBotChallengeError(
                    f"Requiere verificación en navegador{label}. "
                    f"Haz doble clic en la fila para abrir en tu navegador; Mint Downloader capturará la descarga automáticamente."
                )

            if any(
                phrase in page
                for phrase in (
                    "Something appears to be missing",
                    "Invalid or Deleted File",
                    "File Removed for Violation",
                )
            ):
                raise DeadLinkError("El enlace de MediaFire está caído o fue eliminado.")

            if 'name="downloadp"' in page or "Enter Password" in page:
                raise ProtectedFileError("El archivo de MediaFire está protegido con contraseña.")

            direct_url = None
            m_scram = re.search(r'data-scrambled-url=["\']([^"\']+)["\']', page)
            if m_scram:
                try:
                    direct_url = base64.b64decode(m_scram.group(1)).decode("utf-8")
                except Exception:
                    direct_url = None

            if not direct_url:
                m_btn = re.search(
                    r'id=["\']downloadButton["\'][^>]*href=["\'](https?://[^"\']+)["\']',
                    page,
                    re.I,
                )
                if m_btn:
                    direct_url = html.unescape(m_btn.group(1))

            if not direct_url:
                m_dl = re.search(r"(https?://download[0-9]*\.mediafire\.com/[^\s\"'<>]+)", page)
                if m_dl:
                    direct_url = html.unescape(m_dl.group(1))

            if not direct_url:
                raise ExtractorError("No se pudo extraer el enlace directo desde MediaFire.")

            fname = api_filename or _sanitize_filename(os.path.basename(urllib.parse.urlparse(direct_url).path))
            size = api_size
            try:
                async with session.head(direct_url, allow_redirects=True) as hresp:
                    cd_name = _extract_filename_from_cd(hresp.headers.get("Content-Disposition", ""))
                    if cd_name:
                        fname = cd_name
                    size = int(hresp.headers.get("Content-Length", 0) or api_size)
            except Exception:
                pass

            return ResolvedDownload(
                original_url=url,
                direct_url=direct_url,
                filename=fname,
                host="MediaFire",
                size=size,
                headers={"User-Agent": DEFAULT_UA},
                extractor_type="mediafire",
                extra={"quickkey": quickkey},
            )

    async def _resolve_gdrive(self, url: str) -> ResolvedDownload:
        file_id = None
        patterns = [
            r"/file/d/([a-zA-Z0-9_-]{10,})",
            r"[?&]id=([a-zA-Z0-9_-]{10,})",
            r"/d/([a-zA-Z0-9_-]{10,})",
        ]
        for pat in patterns:
            m = re.search(pat, url)
            if m:
                file_id = m.group(1)
                break

        if not file_id:
            if "/folders/" in url:
                raise ProtectedFileError("Los enlaces de carpetas de Google Drive requieren enlaces de archivo individual.")
            raise ExtractorError("No se pudo identificar el ID de archivo de Google Drive.")

        headers = {"User-Agent": DEFAULT_UA}
        jar = aiohttp.CookieJar()
        dl_endpoint = f"https://drive.usercontent.google.com/download?id={file_id}&export=download&confirm=t"

        async with aiohttp.ClientSession(timeout=self.timeout, headers=headers, cookie_jar=jar) as session:
            async with session.get(dl_endpoint, allow_redirects=True) as resp:
                if resp.status == 404:
                    raise DeadLinkError("El archivo de Google Drive no existe (HTTP 404).")
                if resp.status in (401, 403):
                    raise ProtectedFileError("El archivo de Google Drive es privado o requiere permisos.")

                ctype = resp.headers.get("Content-Type", "")
                if not ctype.startswith("text/html"):
                    fname = _extract_filename_from_cd(resp.headers.get("Content-Disposition", "")) or f"gdrive_{file_id}.bin"
                    size = int(resp.headers.get("Content-Length", 0) or 0)
                    cookies = "; ".join(f"{c.key}={c.value}" for c in jar)
                    req_headers = dict(headers)
                    if cookies:
                        req_headers["Cookie"] = cookies
                    return ResolvedDownload(
                        original_url=url,
                        direct_url=str(resp.url),
                        filename=fname,
                        host="Google Drive",
                        size=size,
                        headers=req_headers,
                        extractor_type="gdrive",
                    )

                page = await resp.text(errors="replace")

            if any(
                msg in page
                for msg in (
                    "Too many users have viewed or downloaded this file",
                    "Download quota exceeded",
                    "Sorry, you can't view or download this file at this time",
                )
            ):
                raise BandwidthLimitError("Cuota de descarga excedida en Google Drive para este archivo.")

            if "accounts.google.com/ServiceLogin" in page or "You need access" in page:
                raise ProtectedFileError("El archivo de Google Drive es privado y requiere iniciar sesión.")

            m_action = re.search(r'<form[^>]+id="download-form"[^>]+action="([^"]+)"', page)
            action_url = html.unescape(m_action.group(1)) if m_action else "https://drive.usercontent.google.com/download"
            params: dict[str, str] = {"id": file_id, "export": "download", "confirm": "t"}
            for m_inp in re.finditer(r'<input[^>]+type="hidden"[^>]+name="([^"]+)"[^>]+value="([^"]*)"', page):
                params[m_inp.group(1)] = html.unescape(m_inp.group(2))

            confirmed_url = f"{action_url}?{urllib.parse.urlencode(params)}"
            fname = f"gdrive_{file_id}.bin"
            m_name = re.search(r'class="uc-name-size"[^>]*>\s*<a[^>]*>([^<]+)</a>', page)
            if m_name:
                fname = _sanitize_filename(html.unescape(m_name.group(1)))

            size = 0
            cookies = "; ".join(f"{c.key}={c.value}" for c in jar)
            req_headers = dict(headers)
            if cookies:
                req_headers["Cookie"] = cookies

            return ResolvedDownload(
                original_url=url,
                direct_url=confirmed_url,
                filename=fname,
                host="Google Drive",
                size=size,
                headers=req_headers,
                extractor_type="gdrive",
            )

    async def _resolve_onedrive(self, url: str) -> ResolvedDownload:
        headers = {"User-Agent": DEFAULT_UA}
        b64 = base64.urlsafe_b64encode(url.encode("utf-8")).decode("ascii").rstrip("=")
        api_url = f"https://api.onedrive.com/v1.0/shares/u!{b64}/root/content"

        async with aiohttp.ClientSession(timeout=self.timeout, headers=headers) as session:
            try:
                async with session.get(api_url, allow_redirects=False) as resp:
                    if resp.status in (301, 302, 303, 307, 308) and "Location" in resp.headers:
                        direct_url = resp.headers["Location"]
                        async with session.head(direct_url, allow_redirects=True) as hresp:
                            fname = _extract_filename_from_cd(hresp.headers.get("Content-Disposition", ""))
                            if not fname:
                                fname = _sanitize_filename(os.path.basename(urllib.parse.urlparse(direct_url).path))
                            size = int(hresp.headers.get("Content-Length", 0) or 0)
                            return ResolvedDownload(
                                original_url=url,
                                direct_url=direct_url,
                                filename=fname or "onedrive_file.bin",
                                host="OneDrive",
                                size=size,
                                headers=headers,
                                extractor_type="onedrive",
                            )
            except Exception:
                pass

            async with session.get(url, allow_redirects=True) as resp:
                if resp.status == 404:
                    raise DeadLinkError("El enlace de OneDrive no existe o expiró (HTTP 404).")
                if resp.status in (401, 403):
                    raise ProtectedFileError("El enlace de OneDrive requiere contraseña o inicio de sesión.")

                final_url = str(resp.url)
                parsed = urllib.parse.urlparse(final_url)
                q = urllib.parse.parse_qs(parsed.query)
                q["download"] = ["1"]
                dl_url = urllib.parse.urlunparse(parsed._replace(query=urllib.parse.urlencode(q, doseq=True)))

                async with session.head(dl_url, allow_redirects=True) as hresp:
                    if hresp.status == 404:
                        raise DeadLinkError("Archivo de OneDrive no encontrado.")
                    ctype = hresp.headers.get("Content-Type", "")
                    if ctype.startswith("text/html"):
                        raise ProtectedFileError(
                            "OneDrive requiere autenticación o contraseña para descargar este archivo."
                        )
                    fname = _extract_filename_from_cd(hresp.headers.get("Content-Disposition", ""))
                    if not fname:
                        fname = _sanitize_filename(os.path.basename(parsed.path)) or "onedrive_file.bin"
                    size = int(hresp.headers.get("Content-Length", 0) or 0)
                    return ResolvedDownload(
                        original_url=url,
                        direct_url=str(hresp.url),
                        filename=fname,
                        host="OneDrive",
                        size=size,
                        headers=headers,
                        extractor_type="onedrive",
                    )

    async def _resolve_1fichier(self, url: str) -> ResolvedDownload:
        headers = {
            "User-Agent": DEFAULT_UA,
            "Accept-Language": "en-US,en;q=0.9",
        }
        jar = aiohttp.CookieJar()
        async with aiohttp.ClientSession(timeout=self.timeout, headers=headers, cookie_jar=jar) as session:
            async with session.get(url, allow_redirects=True) as resp:
                if resp.status == 404:
                    raise DeadLinkError("El archivo de 1fichier no existe o fue eliminado (HTTP 404).")
                ctype = resp.headers.get("Content-Type", "")
                if not ctype.startswith("text/html"):
                    fname = _extract_filename_from_cd(resp.headers.get("Content-Disposition", "")) or "1fichier.bin"
                    return ResolvedDownload(
                        original_url=url,
                        direct_url=str(resp.url),
                        filename=fname,
                        host="1fichier",
                        size=int(resp.headers.get("Content-Length", 0) or 0),
                        headers=headers,
                        extractor_type="1fichier",
                    )
                page = await resp.text(errors="replace")

            if any(
                msg in page
                for msg in (
                    "The requested file could not be found",
                    "The requested file has been deleted",
                    "File not found",
                    "Le fichier demandé n'existe pas",
                )
            ):
                raise DeadLinkError("El enlace de 1fichier está caído o fue eliminado.")

            if 'name="pass"' in page:
                raise ProtectedFileError("El archivo de 1fichier está protegido con contraseña.")

            if any(
                msg in page
                for msg in (
                    "You must wait",
                    "Without subscription, you can only download one file at a time",
                    "Without premium status",
                )
            ):
                m_wait = re.search(r"You must wait\s+([^.<]+)", page)
                wait_info = f" ({m_wait.group(1).strip()})" if m_wait else ""
                raise BandwidthLimitError(f"Límite gratuito de 1fichier activo{wait_info}.")

            fname = "1fichier_download.bin"
            m_name = re.search(r">File Name\s*:\s*</td>\s*<td[^>]*>([^<]+)</td>", page, re.I)
            if m_name:
                fname = _sanitize_filename(html.unescape(m_name.group(1)))

            form_data = {"dl_no_ssl": "on", "dlinline": "on"}
            m_adz = re.search(r'name="adz"\s+value="([^"]+)"', page)
            if m_adz:
                form_data["adz"] = m_adz.group(1)

            async with session.post(url, data=form_data, allow_redirects=False) as post_resp:
                if post_resp.status in (301, 302, 303, 307, 308) and "Location" in post_resp.headers:
                    direct_url = post_resp.headers["Location"]
                else:
                    post_page = await post_resp.text(errors="replace")
                    m_link = re.search(
                        r'<a[^>]+href=["\'](https?://[a-zA-Z0-9.-]+\.1fichier\.com/[^"\']+)["\']',
                        post_page,
                    )
                    if not m_link:
                        if "You must wait" in post_page:
                            raise BandwidthLimitError("1fichier solicita tiempo de espera entre descargas gratuitas.")
                        raise ExtractorError("No se encontró el enlace directo final en 1fichier.")
                    direct_url = html.unescape(m_link.group(1))

            size = 0
            try:
                async with session.head(direct_url, allow_redirects=True) as hresp:
                    cd_name = _extract_filename_from_cd(hresp.headers.get("Content-Disposition", ""))
                    if cd_name:
                        fname = cd_name
                    size = int(hresp.headers.get("Content-Length", 0) or 0)
            except Exception:
                pass

            return ResolvedDownload(
                original_url=url,
                direct_url=direct_url,
                filename=fname,
                host="1fichier",
                size=size,
                headers=headers,
                extractor_type="1fichier",
            )

    async def _resolve_direct(self, url: str, host: str) -> ResolvedDownload:
        headers = {"User-Agent": DEFAULT_UA}
        if os.path.isfile(url) and url.lower().endswith(".torrent"):
            fname = _sanitize_filename(os.path.basename(url))
            return ResolvedDownload(
                original_url=url,
                direct_url=os.path.abspath(url),
                filename=fname,
                host="BitTorrent (.torrent)",
                size=os.path.getsize(url),
                headers={},
                extractor_type="torrent_file",
            )

        parsed = urllib.parse.urlparse(url)
        if parsed.scheme == "file" and parsed.path.lower().endswith(".torrent"):
            local_p = urllib.parse.unquote(parsed.path)
            if os.path.isfile(local_p):
                return ResolvedDownload(
                    original_url=url,
                    direct_url=os.path.abspath(local_p),
                    filename=_sanitize_filename(os.path.basename(local_p)),
                    host="BitTorrent (.torrent)",
                    size=os.path.getsize(local_p),
                    headers={},
                    extractor_type="torrent_file",
                )

        if parsed.scheme not in ("http", "https", "ftp", "magnet"):
            raise ExtractorError(f"Esquema de URL no soportado: {parsed.scheme or 'vacío'}")

        if parsed.scheme == "magnet":
            qs = urllib.parse.parse_qs(parsed.query)
            dn = (qs.get("dn") or [""])[0]
            if dn:
                fname = _sanitize_filename(dn)
            else:
                m_btih = re.search(r"xt=urn:btih:([a-zA-Z0-9]+)", url, re.I)
                fname = f"magnet_{m_btih.group(1)[:12]}.torrent" if m_btih else "magnet_download"
            return ResolvedDownload(
                original_url=url,
                direct_url=url,
                filename=fname,
                host="BitTorrent (Magnet)",
                size=0,
                headers={},
                extractor_type="magnet",
            )

        if parsed.scheme == "ftp":
            fname = _sanitize_filename(os.path.basename(parsed.path)) or "download.bin"
            return ResolvedDownload(
                original_url=url,
                direct_url=url,
                filename=fname,
                host=host,
                size=0,
                headers=headers,
                extractor_type="direct",
            )

        async with aiohttp.ClientSession(timeout=self.timeout, headers=headers) as session:
            resp_url = url
            size = 0
            fname = None
            status = 200

            try:
                async with session.head(url, allow_redirects=True) as resp:
                    status = resp.status
                    resp_url = str(resp.url)
                    if status < 400:
                        size = int(resp.headers.get("Content-Length", 0) or 0)
                        fname = _extract_filename_from_cd(resp.headers.get("Content-Disposition", ""))
            except Exception:
                status = 0

            if status in (0, 403, 405) or size == 0:
                range_headers = dict(headers)
                range_headers["Range"] = "bytes=0-0"
                async with session.get(url, headers=range_headers, allow_redirects=True) as gresp:
                    status = gresp.status
                    resp_url = str(gresp.url)
                    if status == 404:
                        raise DeadLinkError(f"Enlace caído (HTTP 404): {url}")
                    if status in (401, 403):
                        raise ProtectedFileError(f"Acceso denegado o protegido (HTTP {status}): {url}")
                    if status == 429:
                        raise BandwidthLimitError(f"Límite de ancho de banda / peticiones (HTTP 429): {url}")
                    if status >= 400:
                        raise ExtractorError(f"Error HTTP {status} al verificar enlace: {url}")

                    cr = gresp.headers.get("Content-Range", "")
                    if "/" in cr:
                        total_part = cr.split("/")[-1].strip()
                        if total_part.isdigit():
                            size = int(total_part)
                    if not size:
                        size = int(gresp.headers.get("Content-Length", 0) or 0)
                    if not fname:
                        fname = _extract_filename_from_cd(gresp.headers.get("Content-Disposition", ""))
            else:
                if status == 404:
                    raise DeadLinkError(f"Enlace caído (HTTP 404): {url}")
                if status in (401, 403):
                    raise ProtectedFileError(f"Acceso denegado (HTTP {status}): {url}")
                if status == 429:
                    raise BandwidthLimitError(f"Límite de ancho de banda (HTTP 429): {url}")
                if status >= 400:
                    raise ExtractorError(f"Error HTTP {status}: {url}")

            if not fname:
                path_part = urllib.parse.urlparse(resp_url).path
                base = os.path.basename(path_part)
                fname = _sanitize_filename(base) if base else "download.bin"

            return ResolvedDownload(
                original_url=url,
                direct_url=resp_url,
                filename=fname,
                host=host,
                size=size,
                headers=headers,
                extractor_type="direct",
            )
