"""
Video & HLS Stream Extractor for Mint Downloader.
Integrates yt-dlp and ffmpeg with 16-connection acceleration and real-time progress hooks.
Supports YouTube, Vimeo, Twitch, Dailymotion, TikTok, X/Twitter, and .m3u8 / HLS streams.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import threading
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

import yt_dlp

from .cloud_extractor import (
    BandwidthLimitError,
    DeadLinkError,
    ExtractorError,
    ProtectedFileError,
    ResolvedDownload,
    _sanitize_filename,
    detect_host,
)


VIDEO_DOMAINS = (
    "youtube.com",
    "youtu.be",
    "twitch.tv",
    "vimeo.com",
    "dailymotion.com",
    "tiktok.com",
    "twitter.com",
    "x.com",
    "facebook.com",
    "instagram.com",
    "soundcloud.com",
    "bilibili.com",
    "rumble.com",
)


QUALITY_FORMATS = {
    "best": "bestvideo+bestaudio/best",
    "2160p": "bestvideo[height<=2160]+bestaudio/best[height<=2160]/best",
    "1440p": "bestvideo[height<=1440]+bestaudio/best[height<=1440]/best",
    "1080p": "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best",
    "720p": "bestvideo[height<=720]+bestaudio/best[height<=720]/best",
    "480p": "bestvideo[height<=480]+bestaudio/best[height<=480]/best",
    "360p": "bestvideo[height<=360]+bestaudio/best[height<=360]/best",
    "audio": "bestaudio/best",
}


@dataclass
class VideoQualityChoice:
    id: str
    label: str
    height: int = 0
    fps: int = 0
    filesize: int = 0
    ext: str = "mp4"


def is_video_or_stream_url(url: str) -> bool:
    """Return True if the URL belongs to a streaming platform or is an HLS/DASH manifest."""
    u_low = url.lower()
    if ".m3u8" in u_low or ".mpd" in u_low:
        return True
    try:
        netloc = urllib.parse.urlparse(url).netloc.lower()
    except Exception:
        netloc = ""
    return any(dom in netloc for dom in VIDEO_DOMAINS)


@dataclass
class VideoTaskProgress:
    downloaded_bytes: int = 0
    total_bytes: int = 0
    speed: float = 0.0
    eta: int = 0
    status: str = "queued"
    filename: str = ""
    error: str = ""


class DownloadCancelledError(Exception):
    """Internal signal used to pause or stop an active yt-dlp download."""


class VideoExtractor:
    """Handles metadata extraction and high-speed video/HLS downloading with yt-dlp + ffmpeg."""

    _QUALITIES_CACHE_TTL = 120.0
    _QUALITIES_CACHE_MAX = 32

    def __init__(self, aria2c_path: Optional[str] = None, ffmpeg_path: Optional[str] = None) -> None:
        self.aria2c_path = aria2c_path or shutil.which("aria2c")
        self.ffmpeg_path = ffmpeg_path or shutil.which("ffmpeg") or "/usr/bin/ffmpeg"
        self._qualities_cache: Dict[str, tuple[float, dict[str, Any]]] = {}

    @staticmethod
    def _select_ytdlp_format(url: str, quality: str) -> tuple[str, bool]:
        u_low = url.lower()
        is_manifest = ".m3u8" in u_low or ".mpd" in u_low
        if is_manifest and quality in ("best", "2160p", "1440p", "1080p", "720p", "480p", "360p"):
            return "best", True
        if quality in QUALITY_FORMATS:
            return QUALITY_FORMATS[quality], is_manifest
        if quality.endswith("p") and quality[:-1].isdigit():
            h = int(quality[:-1])
            return f"bestvideo[height<={h}]+bestaudio/best[height<={h}]/best", is_manifest
        return quality or QUALITY_FORMATS["best"], is_manifest

    def _classify_ytdlp_error(self, err_msg: str) -> ExtractorError:
        low = err_msg.lower()
        if any(k in low for k in ("404", "not found", "video unavailable", "has been removed", "deleted")):
            return DeadLinkError(f"Video o transmisión no disponible: {err_msg}")
        if any(k in low for k in ("private", "sign in", "login", "members-only", "password", "age-restricted")):
            return ProtectedFileError(f"El video es privado o requiere autenticación: {err_msg}")
        if any(k in low for k in ("429", "too many requests", "rate limit", "quota")):
            return BandwidthLimitError(f"Límite de peticiones en plataforma de video: {err_msg}")
        return ExtractorError(f"Error al extraer transmisión de video: {err_msg}")

    async def list_qualities(self, url: str) -> dict[str, Any]:
        """
        Inspect a YouTube or streaming URL with yt-dlp and return its title, duration,
        and the exact list of available video/audio resolutions and estimated file sizes.
        """
        now = time.monotonic()
        cached = self._qualities_cache.get(url)
        if cached and (now - cached[0]) < self._QUALITIES_CACHE_TTL:
            return cached[1]

        loop = asyncio.get_running_loop()
        res = await loop.run_in_executor(None, self._list_qualities_sync, url)
        if len(self._qualities_cache) >= self._QUALITIES_CACHE_MAX:
            oldest_key = min(self._qualities_cache, key=lambda k: self._qualities_cache[k][0])
            self._qualities_cache.pop(oldest_key, None)
        self._qualities_cache[url] = (time.monotonic(), res)
        return res

    def _list_qualities_sync(self, url: str) -> dict[str, Any]:
        ydl_opts: Dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "extract_flat": False,
        }
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
        except Exception as exc:
            raise self._classify_ytdlp_error(str(exc)) from exc

        if not info:
            raise DeadLinkError("No se pudo obtener información del video.")
        if "entries" in info and info["entries"]:
            info = next((e for e in info["entries"] if e), info)

        title = info.get("title") or "Video"
        duration = int(info.get("duration") or 0)
        formats = info.get("formats") or []

        best_audio_size = 0
        height_map: Dict[int, VideoQualityChoice] = {}

        for f in formats:
            vcodec = f.get("vcodec") or "none"
            acodec = f.get("acodec") or "none"
            fsize = int(f.get("filesize") or f.get("filesize_approx") or 0)

            if vcodec == "none" and acodec != "none":
                if fsize > best_audio_size:
                    best_audio_size = fsize
                continue

            height = int(f.get("height") or 0)
            if height < 144:
                continue
            fps = int(f.get("fps") or 30)
            ext = f.get("ext") or "mp4"

            prev = height_map.get(height)
            if not prev or fsize > prev.filesize or (fps > prev.fps and fsize > 0):
                tag = ""
                if height >= 2160:
                    tag = "4K UHD"
                elif height >= 1440:
                    tag = "2K QHD"
                elif height >= 1080:
                    tag = "Full HD"
                elif height >= 720:
                    tag = "HD"
                elif height >= 480:
                    tag = "SD"
                fps_str = f" {fps}fps" if fps > 30 else ""
                est_total = (fsize + best_audio_size) if (acodec == "none" and fsize > 0) else fsize
                size_mb = f" (~{est_total / (1024 * 1024):.1f} MB)" if est_total > 0 else ""
                label = f"{height}p {tag}{fps_str} (MP4){size_mb}".replace("  ", " ").strip()
                height_map[height] = VideoQualityChoice(
                    id=f"{height}p",
                    label=label,
                    height=height,
                    fps=fps,
                    filesize=est_total,
                    ext=ext,
                )

        choices: list[VideoQualityChoice] = [
            VideoQualityChoice(id="best", label="Máxima calidad disponible (Automático)", height=9999)
        ]
        for h in sorted(height_map.keys(), reverse=True):
            choices.append(height_map[h])

        audio_mb = f" (~{best_audio_size / (1024 * 1024):.1f} MB)" if best_audio_size > 0 else ""
        choices.append(
            VideoQualityChoice(id="audio", label=f"Solo Audio MP3 192 kbps{audio_mb}", height=0, ext="mp3")
        )

        return {
            "title": title,
            "duration": duration,
            "choices": choices,
        }

    async def probe(
        self,
        url: str,
        quality: str = "best",
        custom_headers: Optional[Dict[str, str]] = None,
    ) -> ResolvedDownload:
        """Extract metadata (title, estimated size, host, format) without blocking the event loop."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._probe_sync, url, quality, custom_headers)

    def _probe_sync(
        self,
        url: str,
        quality: str = "best",
        custom_headers: Optional[Dict[str, str]] = None,
    ) -> ResolvedDownload:
        fmt, _ = self._select_ytdlp_format(url, quality)
        ydl_opts: Dict[str, Any] = {
            "format": fmt,
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "extract_flat": False,
        }
        if custom_headers:
            ydl_opts["http_headers"] = dict(custom_headers)
        if self.ffmpeg_path and os.path.exists(self.ffmpeg_path):
            ydl_opts["ffmpeg_location"] = self.ffmpeg_path

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
        except Exception as exc:
            # Fallback to generic "best" if a specific resolution filter wasn't present on an HLS/embed stream
            if fmt != "best":
                ydl_opts["format"] = "best"
                try:
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl2:
                        info = ydl2.extract_info(url, download=False)
                except Exception as exc2:
                    raise self._classify_ytdlp_error(str(exc2)) from exc2
            else:
                raise self._classify_ytdlp_error(str(exc)) from exc

        if not info:
            raise DeadLinkError("No se obtuvo información del enlace de video.")

        if "entries" in info and info["entries"]:
            info = next((e for e in info["entries"] if e), info)

        title = info.get("title") or "video_web"
        ext = "mp3" if quality == "audio" else (info.get("ext") or "mp4")
        if ext in ("m3u8", "mpd", "unknown_video"):
            ext = "mp4"
        filename = _sanitize_filename(f"{title}.{ext}")

        size = int(info.get("filesize") or info.get("filesize_approx") or 0)
        if not size and info.get("requested_formats"):
            size = sum(
                int(f.get("filesize") or f.get("filesize_approx") or 0)
                for f in info["requested_formats"]
            )

        host = detect_host(url)
        return ResolvedDownload(
            original_url=url,
            direct_url=info.get("url") or url,
            filename=filename,
            host=host,
            size=size,
            headers=dict(info.get("http_headers") or {}),
            extractor_type="video",
            extra={
                "quality": quality,
                "video_id": info.get("id", ""),
                "ext": ext,
            },
        )

    async def download_stream(
        self,
        url: str,
        save_dir: str,
        filename: str,
        quality: str = "best",
        progress_cb: Optional[Callable[[VideoTaskProgress], None]] = None,
        cancel_event: Optional[threading.Event] = None,
        custom_headers: Optional[Dict[str, str]] = None,
    ) -> str:
        """
        Download and merge audio/video or .m3u8 HLS stream using yt-dlp + ffmpeg
        with 16 concurrent fragment threads, reporting progress in real time.
        """
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            self._download_sync,
            url,
            save_dir,
            filename,
            quality,
            progress_cb,
            cancel_event,
            custom_headers,
        )

    def _download_sync(
        self,
        url: str,
        save_dir: str,
        filename: str,
        quality: str,
        progress_cb: Optional[Callable[[VideoTaskProgress], None]],
        cancel_event: Optional[threading.Event],
        custom_headers: Optional[Dict[str, str]] = None,
    ) -> str:
        os.makedirs(save_dir, exist_ok=True)
        base_name, ext = os.path.splitext(filename)
        if not ext:
            ext = ".mp3" if quality == "audio" else ".mp4"
        outtmpl = os.path.join(save_dir, f"{base_name}.%(ext)s")

        fmt, is_manifest = self._select_ytdlp_format(url, quality)
        state = VideoTaskProgress(filename=filename, status="active")
        last_emit = [0.0]

        def _hook(d: Dict[str, Any]) -> None:
            if cancel_event and cancel_event.is_set():
                raise DownloadCancelledError("Descarga pausada o detenida por el usuario.")

            st = d.get("status")
            if st == "downloading":
                now_mono = time.monotonic()
                if (now_mono - last_emit[0]) < 0.12:
                    return
                last_emit[0] = now_mono
                dl_bytes = int(d.get("downloaded_bytes") or 0)
                tot_bytes = int(d.get("total_bytes") or d.get("total_bytes_estimate") or 0)
                if tot_bytes <= 0:
                    frag_idx = int(d.get("fragment_index") or 0)
                    frag_cnt = int(d.get("fragment_count") or 0)
                    if frag_cnt > 0 and frag_idx > 0 and dl_bytes > 0:
                        tot_bytes = int(dl_bytes * (frag_cnt / max(1, frag_idx)))
                spd = float(d.get("speed") or 0.0)
                eta_val = int(d.get("eta") or 0)
                state.downloaded_bytes = dl_bytes
                if tot_bytes > 0:
                    state.total_bytes = tot_bytes
                state.speed = spd
                state.eta = max(0, eta_val)
                state.status = "active"
                if progress_cb:
                    progress_cb(state)
            elif st == "finished":
                dl_bytes = int(d.get("downloaded_bytes") or state.total_bytes or 0)
                state.downloaded_bytes = dl_bytes
                if dl_bytes > state.total_bytes:
                    state.total_bytes = dl_bytes
                state.speed = 0.0
                state.eta = 0
                state.status = "merging"
                if progress_cb:
                    progress_cb(state)

        ydl_opts: Dict[str, Any] = {
            "format": fmt,
            "outtmpl": outtmpl,
            "continuedl": True,
            "nopart": False,
            "noplaylist": True,
            "concurrent_fragment_downloads": 8 if is_manifest else 16,
            "retries": 15,
            "fragment_retries": 15,
            "skip_unavailable_fragments": False,
            "hls_use_mpegts": False,
            "quiet": True,
            "no_warnings": True,
            "progress_hooks": [_hook],
        }
        if custom_headers:
            ydl_opts["http_headers"] = dict(custom_headers)

        if quality == "audio":
            ydl_opts["postprocessors"] = [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }
            ]
        else:
            ydl_opts["merge_output_format"] = "mp4"
            ydl_opts["postprocessor_args"] = {"merger": ["-movflags", "+faststart"]}

        if self.ffmpeg_path and os.path.exists(self.ffmpeg_path):
            ydl_opts["ffmpeg_location"] = self.ffmpeg_path

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])
        except DownloadCancelledError:
            raise
        except Exception as exc:
            if cancel_event and cancel_event.is_set():
                raise DownloadCancelledError("Descarga pausada o detenida.") from exc
            if ydl_opts.get("format") != "best":
                ydl_opts["format"] = "best"
                try:
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl2:
                        ydl2.download([url])
                except Exception as exc2:
                    raise self._classify_ytdlp_error(str(exc2)) from exc2
            else:
                raise self._classify_ytdlp_error(str(exc)) from exc

        final_path = os.path.join(save_dir, filename)
        state.status = "complete"
        if os.path.exists(final_path):
            sz = os.path.getsize(final_path)
            state.downloaded_bytes = sz
            state.total_bytes = sz
        if progress_cb:
            progress_cb(state)
        return final_path
