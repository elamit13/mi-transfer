"""
GTK4 Native User Interface for Mint Downloader.
Connects asynchronously to DownloadEngine using a dedicated asyncio background loop
and updates the download table every 250 ms without blocking the GTK main thread.
"""

from __future__ import annotations

import asyncio
import html
import os
import re
import subprocess
import threading
import urllib.parse
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable, Dict, List, Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Gio", "2.0")
from gi.repository import Gdk, Gio, GLib, GObject, Gtk, Pango

from core.engine import DownloadEngine, DownloadItem
from core.extractors.video_extractor import VideoQualityChoice, is_video_or_stream_url


URL_EXTRACT_RE = re.compile(r"(?:https?|ftp|magnet)://[^\s<>\"']+|magnet:\?[^\s<>\"']+", re.I)

VIDEO_EXTENSIONS = {
    ".mp4", ".mkv", ".webm", ".avi", ".mov", ".flv", ".wmv", ".m4v", ".mpg", ".mpeg", ".ts", ".ogv", ".3gp",
}


class _HTMLAnchorExtractor(HTMLParser):
    """Extract href attributes from <a>, <area>, <iframe src>, and <source src> tags in rich HTML."""

    def __init__(self) -> None:
        super().__init__()
        self.links: List[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        tag_l = tag.lower()
        attr_dict = {k.lower(): (v or "") for k, v in attrs if k}
        candidate = ""
        if tag_l in ("a", "area"):
            candidate = attr_dict.get("href", "")
        elif tag_l in ("iframe", "source", "video", "audio", "embed"):
            candidate = attr_dict.get("src", "")
        if candidate:
            candidate = html.unescape(candidate).strip()
            if re.match(r"^(?:https?|ftp|magnet):", candidate, re.I):
                self.links.append(candidate)


def extract_urls_from_html_or_text(raw_content: str, base_only_from_href: bool = False) -> List[str]:
    """
    Extract valid download URLs from rich HTML (e.g., <a href="https://...">Texto representativo</a>),
    Markdown links ([Texto](https://...)), or plain text, while ignoring the non-link label text.
    """
    if not raw_content:
        return []

    found: List[str] = []

    # Decode UTF-16 if clipboard delivered BOM/null-interleaved HTML bytes
    if "\x00" in raw_content:
        raw_content = raw_content.replace("\x00", "")

    # 1. Parse HTML tags (<a href="...">Texto</a>)
    if "<" in raw_content and ">" in raw_content:
        parser = _HTMLAnchorExtractor()
        try:
            parser.feed(raw_content)
            found.extend(parser.links)
        except Exception:
            pass
        for m_href in re.finditer(r'href\s*=\s*["\']((?:https?|ftp|magnet):[^"\']+)["\']', raw_content, re.I):
            found.append(html.unescape(m_href.group(1).strip()))

    if base_only_from_href and found:
        return list(dict.fromkeys(found))

    # 2. Parse Markdown links [Texto representativo](https://...)
    for m_md in re.finditer(r"\]\(\s*((?:https?|ftp|magnet):[^\s\)]+)\s*\)", raw_content, re.I):
        found.append(m_md.group(1).strip())

    # 3. Extract any bare URLs in the text, stripping trailing punctuation
    cleaned_text = re.sub(r"<[^>]+>", " ", raw_content) if "<" in raw_content else raw_content
    for u in URL_EXTRACT_RE.findall(cleaned_text):
        u_clean = u.rstrip(".,;:)]}>\"'")
        if u_clean:
            found.append(u_clean)

    return list(dict.fromkeys(found))


def read_clipboard_urls_async(clipboard: Gdk.Clipboard, callback: Callable[[List[str]], None]) -> None:
    """
    Inspect the system clipboard prioritizing `text/html` (so copying hyperlinked text like
    'Capítulo 1', 'Capítulo 2' from a browser extracts the underlying <a href="..."> URLs)
    and combining it with plain text URLs, using 100% non-blocking async GIO stream reads.
    """
    formats = clipboard.get_formats()
    has_html = any(
        formats.contain_mime_type(mt)
        for mt in ("text/html", "text/moz_htmlcontext", "application/xhtml+xml")
    )

    def _read_plain_fallback(html_urls: List[str]) -> None:
        def _on_plain(cb2: Gdk.Clipboard, res2: Gio.AsyncResult) -> None:
            plain_urls: List[str] = []
            try:
                txt = cb2.read_text_finish(res2)
                if txt:
                    plain_urls = extract_urls_from_html_or_text(txt)
            except Exception:
                pass
            combined = list(dict.fromkeys(html_urls + plain_urls))
            callback(combined)

        clipboard.read_text_async(None, _on_plain)

    if has_html:
        cancellable = Gio.Cancellable.new()
        finished = {"done": False}

        def _on_timeout() -> bool:
            if not finished["done"]:
                finished["done"] = True
                cancellable.cancel()
                _read_plain_fallback([])
            return False

        GLib.timeout_add(1200, _on_timeout)

        def _on_html_stream(cb: Gdk.Clipboard, res: Gio.AsyncResult) -> None:
            if finished["done"]:
                return
            try:
                stream, _mime = cb.read_finish(res)
            except Exception:
                finished["done"] = True
                _read_plain_fallback([])
                return

            if not stream:
                finished["done"] = True
                _read_plain_fallback([])
                return

            chunks: List[bytes] = []

            def _read_next_chunk(st: Gio.InputStream, r: Gio.AsyncResult) -> None:
                if finished["done"]:
                    try:
                        st.close(None)
                    except Exception:
                        pass
                    return
                try:
                    gbytes = st.read_bytes_finish(r)
                    data = gbytes.get_data() if gbytes else b""
                except Exception:
                    data = b""

                if data and sum(len(c) for c in chunks) < 512 * 1024:
                    chunks.append(data)
                    st.read_bytes_async(
                        65536,
                        GLib.PRIORITY_DEFAULT,
                        cancellable,
                        _read_next_chunk,
                    )
                else:
                    finished["done"] = True
                    try:
                        st.close(None)
                    except Exception:
                        pass
                    raw_bytes = b"".join(chunks)
                    html_urls: List[str] = []
                    if raw_bytes:
                        try:
                            html_str = raw_bytes.decode("utf-8")
                        except UnicodeDecodeError:
                            html_str = raw_bytes.decode("utf-16", errors="ignore")
                        html_urls = extract_urls_from_html_or_text(html_str, base_only_from_href=True)
                    _read_plain_fallback(html_urls)

            stream.read_bytes_async(
                65536,
                GLib.PRIORITY_DEFAULT,
                cancellable,
                _read_next_chunk,
            )

        clipboard.read_async(
            ["text/html", "application/xhtml+xml"],
            GLib.PRIORITY_DEFAULT,
            cancellable,
            _on_html_stream,
        )
        return

    _read_plain_fallback([])


def is_video_filename(filename: str) -> bool:
    _, ext = os.path.splitext(filename.lower())
    return ext in VIDEO_EXTENSIONS


def open_path_with_system(path: str) -> None:
    """Open a local file or folder with the default desktop handler (xdg-open / Gio)."""
    try:
        subprocess.Popen(
            ["xdg-open", path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        try:
            Gio.AppInfo.launch_default_for_uri(Gio.File.new_for_path(path).get_uri(), None)
        except Exception:
            pass


def format_size(num_bytes: int) -> str:
    if num_bytes <= 0:
        return "—"
    units = ["B", "KB", "MB", "GB", "TB"]
    val = float(num_bytes)
    for unit in units:
        if val < 1024.0 or unit == "TB":
            return f"{val:.1f} {unit}" if unit != "B" else f"{int(val)} B"
        val /= 1024.0
    return f"{val:.1f} TB"


def format_speed_mbs(speed_bps: float) -> str:
    if speed_bps <= 0:
        return "0.00 MB/s"
    mbs = speed_bps / (1024.0 * 1024.0)
    return f"{mbs:.2f} MB/s"


def format_eta(seconds: int) -> str:
    if seconds <= 0:
        return "—"
    if seconds >= 86400:
        d = seconds // 86400
        h = (seconds % 86400) // 3600
        return f"{d}d {h}h"
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


class AsyncEngineRunner:
    """Runs DownloadEngine inside a dedicated background asyncio thread."""

    def __init__(self, engine: DownloadEngine) -> None:
        self.engine = engine
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run_loop, name="MintDL-AsyncLoop", daemon=True)
        self.total_speed_bps: float = 0.0
        self.active_count: int = 0

    def start(self) -> None:
        self.thread.start()
        self.submit(self._init_and_poll())

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    async def _init_and_poll(self) -> None:
        await self.engine.start()
        while self.engine._running:
            try:
                spd, act = await self.engine.refresh_statuses()
                self.total_speed_bps = spd
                self.active_count = act
            except Exception:
                pass
            await asyncio.sleep(0.20)

    def submit(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def stop(self) -> None:
        try:
            fut = asyncio.run_coroutine_threadsafe(self.engine.shutdown(), self.loop)
            fut.result(timeout=3.0)
        except Exception:
            pass
        self.loop.call_soon_threadsafe(self.loop.stop)


class DownloadRowObject(GObject.Object):
    """GObject wrapper representing a single row in the GTK4 ColumnView."""

    __gtype_name__ = "DownloadRowObject"

    def __init__(self, item: DownloadItem) -> None:
        super().__init__()
        self.item_id: int = item.id
        self.filename: str = ""
        self.file_ext: str = "—"
        self.origin: str = ""
        self.container: str = "General"
        self.host: str = ""
        self.size_str: str = ""
        self.progress_fraction: float = 0.0
        self.progress_text: str = "0.0%"
        self.speed_str: str = "0.00 MB/s"
        self.eta_str: str = "—"
        self.status_str: str = ""
        self.sync_from_item(item)

    def sync_from_item(self, item: DownloadItem) -> bool:
        """Update cached display fields; return True if any field changed."""
        new_filename = item.filename
        new_file_ext = item.file_extension
        new_origin = item.origin_site_name
        new_container = item.container_name
        new_host = item.host or "Directo"
        if item.total_size > 0:
            new_size = f"{format_size(item.completed_size)} / {format_size(item.total_size)}"
        elif item.completed_size > 0:
            new_size = format_size(item.completed_size)
        else:
            new_size = "—"

        new_frac = item.progress_fraction
        new_prog_txt = f"{item.progress_percent:.1f}%"
        new_speed = format_speed_mbs(item.speed)
        new_eta = format_eta(item.eta) if item.status == "active" else "—"
        new_status = item.status_display

        changed = (
            self.filename != new_filename
            or self.file_ext != new_file_ext
            or self.origin != new_origin
            or self.container != new_container
            or self.host != new_host
            or self.size_str != new_size
            or abs(self.progress_fraction - new_frac) > 0.0005
            or self.progress_text != new_prog_txt
            or self.speed_str != new_speed
            or self.eta_str != new_eta
            or self.status_str != new_status
        )
        if changed:
            self.filename = new_filename
            self.file_ext = new_file_ext
            self.origin = new_origin
            self.container = new_container
            self.host = new_host
            self.size_str = new_size
            self.progress_fraction = new_frac
            self.progress_text = new_prog_txt
            self.speed_str = new_speed
            self.eta_str = new_eta
            self.status_str = new_status
        return changed


class MintDownloaderWindow(Gtk.ApplicationWindow):
    """Main GTK4 Window for Mint Downloader."""

    def __init__(self, app: Gtk.Application, runner: AsyncEngineRunner) -> None:
        super().__init__(application=app, title="Mint Downloader")
        self.runner = runner
        self.engine = runner.engine
        self.set_default_size(1060, 620)
        self._setup_app_icon()

        self._row_map: Dict[int, DownloadRowObject] = {}
        self._item_widgets: Dict[int, Dict[str, Gtk.Widget]] = {}
        self._seen_clipboard_links: set[str] = set()
        self._tick_counter: int = 0
        self._active_container_filter: str = "Todos"
        self._known_containers: List[str] = ["Todos", "General"]
        self._active_chooser: Optional[Gtk.Widget] = None
        self._theme_provider = Gtk.CssProvider()
        display = Gdk.Display.get_default()
        if display:
            Gtk.StyleContext.add_provider_for_display(
                display,
                self._theme_provider,
                Gtk.STYLE_PROVIDER_PRIORITY_USER,
            )

        self._apply_css()
        self._build_ui()
        self._apply_background_theme()
        self._populate_initial_rows()

        self.column_view.connect("activate", self._on_row_activated)
        self._tick_source_id = GLib.timeout_add(250, self._on_ui_tick)
        self.connect("close-request", self._on_close_request)

    def _setup_app_icon(self) -> None:
        try:
            assets_dir = Path(__file__).resolve().parent.parent / "assets"
            display = Gdk.Display.get_default()
            if display and assets_dir.is_dir():
                theme = Gtk.IconTheme.get_for_display(display)
                theme.add_search_path(str(assets_dir))
            Gtk.Window.set_default_icon_name("mint-downloader")
            self.set_icon_name("mint-downloader")
        except Exception:
            pass

    def _apply_css(self) -> None:
        css = b"""
        .toolbar-deck {
            padding: 6px 12px;
            background: linear-gradient(180deg, alpha(@headerbar_bg_color, 0.96), alpha(@window_bg_color, 0.98));
            border-bottom: 1px solid alpha(currentColor, 0.14);
        }
        .toolbar-row {
            padding: 2px 0px;
        }
        .pro-btn {
            min-height: 26px;
            padding: 3px 10px;
            border-radius: 7px;
            font-size: 0.88em;
            font-weight: 600;
        }
        .app-title-badge {
            font-weight: 800;
            font-size: 0.95em;
            letter-spacing: 0.3px;
            margin-right: 4px;
        }
        .status-bar {
            padding: 5px 12px;
            border-top: 1px solid alpha(currentColor, 0.14);
            font-size: 0.88em;
        }
        .speed-badge {
            font-weight: 700;
            color: #10b981;
        }
        .bridge-badge {
            font-size: 0.82em;
            color: #38bdf8;
            font-weight: 600;
        }
        progressbar {
            min-width: 0px;
        }
        progressbar trough {
            min-width: 0px;
            min-height: 16px;
        }
        progressbar progress {
            min-width: 0px;
            min-height: 16px;
        }
        progressbar text {
            font-size: 0.80em;
            font-weight: 600;
        }
        """
        provider = Gtk.CssProvider()
        provider.load_from_data(css)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(),
            provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

    def _apply_background_theme(self) -> None:
        """Apply user-selected solid color or custom image wallpaper to the window."""
        mode = getattr(self.engine, "bg_mode", "default")
        color_hex = getattr(self.engine, "bg_color", "#1e293b") or "#1e293b"
        img_path = getattr(self.engine, "bg_image", "") or ""
        opacity = float(getattr(self.engine, "bg_opacity", 0.55))

        if mode == "default" or (mode == "image" and not os.path.isfile(img_path)):
            self._root_box.remove_css_class("custom-bg-root")
            self._theme_provider.load_from_data(b"")
            return

        self._root_box.add_css_class("custom-bg-root")

        if mode == "color":
            rgba = Gdk.RGBA()
            if not rgba.parse(color_hex):
                rgba.parse("#1e293b")
            lum = 0.299 * rgba.red + 0.587 * rgba.green + 0.114 * rgba.blue
            is_dark = lum < 0.55
            fg = "#f8fafc" if is_dark else "#0f172a"
            header_bg = "rgba(15, 23, 42, 0.45)" if is_dark else "rgba(255, 255, 255, 0.55)"
            row_hover = "rgba(255, 255, 255, 0.10)" if is_dark else "rgba(15, 23, 42, 0.08)"
            row_sel = "rgba(16, 185, 129, 0.40)" if is_dark else "rgba(16, 185, 129, 0.30)"
            sep_col = "rgba(255, 255, 255, 0.14)" if is_dark else "rgba(15, 23, 42, 0.14)"
            bg_rule = f"background-color: {rgba.to_string()}; background-image: none;"
        else:
            # Custom background image
            uri = GLib.filename_to_uri(os.path.abspath(img_path), None)
            fg = "#f8fafc"
            header_bg = "rgba(15, 23, 42, 0.62)"
            row_hover = "rgba(255, 255, 255, 0.12)"
            row_sel = "rgba(16, 185, 129, 0.45)"
            sep_col = "rgba(255, 255, 255, 0.16)"
            alpha_str = f"{opacity:.2f}"
            bg_rule = (
                f'background-image: linear-gradient(rgba(15, 23, 42, {alpha_str}), rgba(15, 23, 42, {alpha_str})), url("{uri}"); '
                "background-size: cover; background-position: center; background-repeat: no-repeat;"
            )

        css = f"""
        .custom-bg-root {{
            {bg_rule}
        }}
        .custom-bg-root .toolbar-deck {{
            background: {header_bg};
            border-bottom: 1px solid {sep_col};
        }}
        .custom-bg-root .status-bar {{
            background: {header_bg};
            border-top: 1px solid {sep_col};
        }}
        .custom-bg-root .toolbar-deck > box > label,
        .custom-bg-root .status-bar > label,
        .custom-bg-root .status-bar box > label {{
            color: {fg};
        }}
        .custom-bg-root .table-area,
        .custom-bg-root .table-area columnview,
        .custom-bg-root .table-area columnview > listview,
        .custom-bg-root .table-area columnview > header {{
            background-color: transparent;
            color: {fg};
        }}
        .custom-bg-root .table-area columnview > header button {{
            background-color: {header_bg};
            color: {fg};
            border-color: {sep_col};
        }}
        .custom-bg-root .table-area columnview > listview > row {{
            background-color: transparent;
            color: {fg};
        }}
        .custom-bg-root .table-area columnview > listview > row:hover {{
            background-color: {row_hover};
        }}
        .custom-bg-root .table-area columnview > listview > row:selected {{
            background-color: {row_sel};
        }}
        .custom-bg-root .table-area columnview label {{
            color: {fg};
        }}
        """.encode("utf-8")
        self._theme_provider.load_from_data(css)

    def _make_toolbar_button(self, icon_name: str, label_text: str, tooltip: str, callback) -> Gtk.Button:
        btn = Gtk.Button()
        btn.add_css_class("pro-btn")
        btn.set_tooltip_text(tooltip)
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5)
        img = Gtk.Image.new_from_icon_name(icon_name)
        img.set_pixel_size(14)
        lbl = Gtk.Label(label=label_text)
        box.append(img)
        box.append(lbl)
        btn.set_child(box)
        btn.connect("clicked", callback)
        return btn

    def _build_ui(self) -> None:
        root_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._root_box = root_box
        self.set_child(root_box)

        # 1. Stylish 2-Row Pro Control Deck (Half buttons on Top Row, Half on Bottom Row)
        deck_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        deck_box.add_css_class("toolbar-deck")

        row_top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        row_top.add_css_class("toolbar-row")

        row_bottom = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        row_bottom.add_css_class("toolbar-row")

        logo_path = Path(__file__).resolve().parent.parent / "assets" / "mint-downloader.svg"
        if logo_path.is_file():
            logo_img = Gtk.Image.new_from_file(str(logo_path))
            logo_img.set_pixel_size(24)
            logo_img.set_tooltip_text("Mint Downloader Pro")
            row_top.append(logo_img)

        brand_lbl = Gtk.Label(label="MINT DL")
        brand_lbl.add_css_class("app-title-badge")
        row_top.append(brand_lbl)

        # --- TOP ROW: Primary Queue Actions (5 buttons) ---
        self.btn_add = self._make_toolbar_button(
            "list-add-symbolic",
            "Agregar enlaces",
            "Pegar enlaces (YouTube con selector de calidades, MEGA, MediaFire, Magnet, hipervínculos HTML)",
            self._on_add_links_clicked,
        )
        self.btn_add.add_css_class("suggested-action")

        self.btn_torrent = self._make_toolbar_button(
            "document-open-symbolic",
            "Abrir .torrent",
            "Agregar archivo .torrent local o enlace magnético BitTorrent",
            self._on_add_torrent_clicked,
        )

        self.btn_start = self._make_toolbar_button(
            "media-playback-start-symbolic",
            "Iniciar",
            "Iniciar o reanudar descargas seleccionadas (o toda la cola)",
            self._on_start_clicked,
        )
        self.btn_pause = self._make_toolbar_button(
            "media-playback-pause-symbolic",
            "Pausar",
            "Pausar descargas manteniendo el progreso en disco",
            self._on_pause_clicked,
        )
        self.btn_stop = self._make_toolbar_button(
            "media-playback-stop-symbolic",
            "Detener",
            "Detener descargas seleccionadas",
            self._on_stop_clicked,
        )

        row_top.append(self.btn_add)
        row_top.append(self.btn_torrent)
        row_top.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        row_top.append(self.btn_start)
        row_top.append(self.btn_pause)
        row_top.append(self.btn_stop)

        top_spacer = Gtk.Box()
        top_spacer.set_hexpand(True)
        row_top.append(top_spacer)

        bridge_lbl = Gtk.Label(label="● Extensión Firefox IDM activa (127.0.0.1:16890)")
        bridge_lbl.add_css_class("bridge-badge")
        row_top.append(bridge_lbl)

        # --- BOTTOM ROW: Organization, Cleanup, Browser & Folders (5 buttons + filter) ---
        self.btn_remove = self._make_toolbar_button(
            "user-trash-symbolic",
            "Eliminar",
            "Eliminar de la cola y opcionalmente borrar archivos temporales",
            self._on_remove_clicked,
        )
        self.btn_clear_completed = self._make_toolbar_button(
            "edit-clear-all-symbolic",
            "Limpiar completados",
            "Quitar de la lista las descargas ya completadas (conserva los archivos en disco)",
            self._on_clear_completed_clicked,
        )
        self.btn_container = self._make_toolbar_button(
            "folder-new-symbolic",
            "Contenedor",
            "Agrupar enlaces seleccionados en un contenedor / paquete",
            self._on_assign_container_clicked,
        )
        self.btn_browser = self._make_toolbar_button(
            "web-browser-symbolic",
            "Verificar en navegador",
            "Abrir enlace seleccionado en el navegador para capturar descarga directa automáticamente",
            self._on_open_in_browser_clicked,
        )
        self.btn_open_dir = self._make_toolbar_button(
            "folder-symbolic",
            "Abrir carpeta",
            "Abrir la carpeta de descargas en el gestor de archivos",
            self._on_open_save_dir_clicked,
        )
        self.btn_folder = self._make_toolbar_button(
            "folder-open-symbolic",
            "Carpeta destino",
            f"Cambiar carpeta de destino (Actual: {self.engine.save_dir})",
            self._on_folder_clicked,
        )
        self.btn_theme = self._make_toolbar_button(
            "preferences-desktop-wallpaper-symbolic",
            "Fondo",
            "Personalizar color o imagen de fondo del programa",
            self._on_theme_clicked,
        )

        row_bottom.append(self.btn_remove)
        row_bottom.append(self.btn_clear_completed)
        row_bottom.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        row_bottom.append(self.btn_container)

        filter_lbl = Gtk.Label(label="Paquete:")
        filter_lbl.set_margin_start(4)
        row_bottom.append(filter_lbl)
        self.container_filter_model = Gtk.StringList.new(["Todos", "General"])
        self.container_filter_dropdown = Gtk.DropDown(model=self.container_filter_model)
        self.container_filter_dropdown.set_tooltip_text("Filtrar lista por contenedor/paquete")
        self.container_filter_dropdown.connect("notify::selected", self._on_container_filter_changed)
        row_bottom.append(self.container_filter_dropdown)

        row_bottom.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        row_bottom.append(self.btn_browser)

        bottom_spacer = Gtk.Box()
        bottom_spacer.set_hexpand(True)
        row_bottom.append(bottom_spacer)
        row_bottom.append(self.btn_theme)
        row_bottom.append(self.btn_open_dir)
        row_bottom.append(self.btn_folder)

        deck_box.append(row_top)
        deck_box.append(row_bottom)
        root_box.append(deck_box)

        # 2. Main Real-Time Download Table (Gtk.ColumnView with Mouse Drag Rubberband Multi-Selection)
        self.list_store = Gio.ListStore.new(DownloadRowObject)
        self.custom_filter = Gtk.CustomFilter.new(self._filter_row_by_container, None)
        self.filter_list_model = Gtk.FilterListModel.new(self.list_store, self.custom_filter)
        self.selection_model = Gtk.MultiSelection.new(self.filter_list_model)

        self.column_view = Gtk.ColumnView.new(self.selection_model)
        self.column_view.set_show_row_separators(True)
        self.column_view.set_show_column_separators(True)
        self.column_view.set_enable_rubberband(True)
        self.column_view.set_single_click_activate(False)
        self.column_view.set_vexpand(True)
        self.column_view.set_hexpand(True)

        # Ctrl+A shortcut to select all rows
        shortcut_ctrl = Gtk.ShortcutController.new()
        shortcut_ctrl.add_shortcut(
            Gtk.Shortcut.new(
                Gtk.ShortcutTrigger.parse_string("<Control>a"),
                Gtk.CallbackAction.new(lambda *_: (self.selection_model.select_all(), True)[1]),
            )
        )
        self.column_view.add_controller(shortcut_ctrl)

        self._add_text_column("Nombre", "filename", expand=True, min_width=220)
        self._add_text_column("Contenedor", "file_ext", expand=False, min_width=100)
        self._add_text_column("Origen", "origin", expand=False, min_width=130)
        self._add_text_column("Tamaño", "size_str", expand=False, min_width=140)
        self._add_progress_column("Progreso", min_width=170)
        self._add_text_column("Velocidad", "speed_str", expand=False, min_width=110)
        self._add_text_column("Tiempo Restante", "eta_str", expand=False, min_width=125)
        self._add_text_column("Estado", "status_str", expand=False, min_width=150)

        scrolled = Gtk.ScrolledWindow()
        scrolled.add_css_class("table-area")
        scrolled.set_vexpand(True)
        scrolled.set_hexpand(True)
        scrolled.set_child(self.column_view)
        self._scrolled = scrolled
        root_box.append(scrolled)

        # 3. Bottom Status Bar
        status_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14)
        status_bar.add_css_class("status-bar")

        self.lbl_folder = Gtk.Label(label=f"Destino: {self.engine.save_dir}")
        self.lbl_folder.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        self.lbl_folder.set_max_width_chars(42)

        status_spacer = Gtk.Box()
        status_spacer.set_hexpand(True)

        self.lbl_active = Gtk.Label(label=f"Descargas activas: 0 / {self.engine.max_concurrent}")

        conc_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        conc_box.append(Gtk.Label(label="Máx. simultáneas:"))
        self.spin_conc = Gtk.SpinButton.new_with_range(1, 16, 1)
        self.spin_conc.set_value(self.engine.max_concurrent)
        self.spin_conc.connect("value-changed", self._on_concurrency_changed)
        conc_box.append(self.spin_conc)

        self.lbl_total_speed = Gtk.Label(label="Velocidad Total: 0.00 MB/s")
        self.lbl_total_speed.add_css_class("speed-badge")

        self.btn_speed_limit = Gtk.MenuButton()
        self.btn_speed_limit.set_label(self._format_limit_button_label())
        self.btn_speed_limit.set_popover(self._build_speed_limit_popover())

        status_bar.append(self.lbl_folder)
        status_bar.append(status_spacer)
        status_bar.append(self.lbl_active)
        status_bar.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        status_bar.append(conc_box)
        status_bar.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        status_bar.append(self.lbl_total_speed)
        status_bar.append(self.btn_speed_limit)

        root_box.append(status_bar)

    def _filter_row_by_container(self, row_obj: DownloadRowObject, _user_data) -> bool:
        if not self._active_container_filter or self._active_container_filter == "Todos":
            return True
        return row_obj.container == self._active_container_filter

    def _on_container_filter_changed(self, dropdown: Gtk.DropDown, _pspec) -> None:
        idx = dropdown.get_selected()
        if idx != Gtk.INVALID_LIST_POSITION and idx < len(self._known_containers):
            self._active_container_filter = self._known_containers[idx]
        else:
            self._active_container_filter = "Todos"
        self.custom_filter.changed(Gtk.FilterChange.DIFFERENT)

    def _refresh_container_dropdown(self) -> None:
        containers = ["Todos"] + self.engine.get_containers()
        if containers != self._known_containers:
            prev = self._active_container_filter
            self._known_containers = containers
            self.container_filter_model.splice(0, self.container_filter_model.get_n_items(), containers)
            if prev in containers:
                self.container_filter_dropdown.set_selected(containers.index(prev))
            else:
                self.container_filter_dropdown.set_selected(0)

    def _open_item_file_or_folder(self, item_id: int) -> None:
        item = self.engine.items.get(item_id)
        if not item:
            return
        file_path = os.path.join(item.save_dir, item.filename)
        if is_video_filename(item.filename) and os.path.isfile(file_path):
            open_path_with_system(file_path)
        elif os.path.isdir(item.save_dir):
            open_path_with_system(item.save_dir)
        elif os.path.isdir(self.engine.save_dir):
            open_path_with_system(self.engine.save_dir)

    def _add_text_column(
        self,
        title: str,
        attr_name: str,
        expand: bool = False,
        min_width: int = 100,
    ) -> None:
        factory = Gtk.SignalListItemFactory()

        def _setup(_fac, list_item: Gtk.ListItem) -> None:
            lbl = Gtk.Label(xalign=0.0)
            lbl.set_ellipsize(Pango.EllipsizeMode.END)
            lbl.set_hexpand(True)
            lbl.set_halign(Gtk.Align.FILL)
            lbl.set_margin_start(6)
            lbl.set_margin_end(6)
            lbl.set_margin_top(4)
            lbl.set_margin_bottom(4)
            list_item.set_child(lbl)

        def _bind(_fac, list_item: Gtk.ListItem) -> None:
            row_obj: DownloadRowObject = list_item.get_item()
            lbl: Gtk.Label = list_item.get_child()
            if row_obj and lbl:
                val = getattr(row_obj, attr_name, "")
                lbl.set_text(str(val))
                lbl.set_tooltip_text(str(val))
                self._item_widgets.setdefault(row_obj.item_id, {})[attr_name] = lbl

        def _unbind(_fac, list_item: Gtk.ListItem) -> None:
            row_obj: DownloadRowObject = list_item.get_item()
            if row_obj and row_obj.item_id in self._item_widgets:
                self._item_widgets[row_obj.item_id].pop(attr_name, None)

        factory.connect("setup", _setup)
        factory.connect("bind", _bind)
        factory.connect("unbind", _unbind)

        col = Gtk.ColumnViewColumn.new(title, factory)
        col.set_expand(expand)
        col.set_resizable(True)
        col.set_fixed_width(min_width)
        self.column_view.append_column(col)

    def _add_progress_column(self, title: str, min_width: int = 170) -> None:
        factory = Gtk.SignalListItemFactory()

        def _setup(_fac, list_item: Gtk.ListItem) -> None:
            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
            box.set_hexpand(True)
            box.set_halign(Gtk.Align.FILL)
            box.set_valign(Gtk.Align.CENTER)
            box.set_margin_start(6)
            box.set_margin_end(6)

            pbar = Gtk.ProgressBar()
            pbar.set_show_text(True)
            pbar.set_hexpand(True)
            pbar.set_halign(Gtk.Align.FILL)
            pbar.set_valign(Gtk.Align.CENTER)
            pbar.set_ellipsize(Pango.EllipsizeMode.END)
            box.append(pbar)
            list_item.set_child(box)

        def _bind(_fac, list_item: Gtk.ListItem) -> None:
            row_obj: DownloadRowObject = list_item.get_item()
            box = list_item.get_child()
            pbar = box.get_first_child() if isinstance(box, Gtk.Box) else box
            if row_obj and isinstance(pbar, Gtk.ProgressBar):
                pbar.set_fraction(row_obj.progress_fraction)
                pbar.set_text(row_obj.progress_text)
                self._item_widgets.setdefault(row_obj.item_id, {})["progress"] = pbar

        def _unbind(_fac, list_item: Gtk.ListItem) -> None:
            row_obj: DownloadRowObject = list_item.get_item()
            if row_obj and row_obj.item_id in self._item_widgets:
                self._item_widgets[row_obj.item_id].pop("progress", None)

        factory.connect("setup", _setup)
        factory.connect("bind", _bind)
        factory.connect("unbind", _unbind)

        col = Gtk.ColumnViewColumn.new(title, factory)
        col.set_expand(False)
        col.set_resizable(True)
        col.set_fixed_width(min_width)
        self.column_view.append_column(col)

    def _build_speed_limit_popover(self) -> Gtk.Popover:
        pop = Gtk.Popover()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_top(10)
        box.set_margin_bottom(10)
        box.set_margin_start(10)
        box.set_margin_end(10)

        lbl = Gtk.Label(label="Límite global de velocidad (KB/s, 0 = Sin límite):")
        box.append(lbl)

        spin = Gtk.SpinButton.new_with_range(0, 1000000, 256)
        spin.set_value(self.engine.speed_limit_kbps)
        box.append(spin)

        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        btn_unlimited = Gtk.Button(label="Sin límite")
        btn_apply = Gtk.Button(label="Aplicar")
        btn_apply.add_css_class("suggested-action")

        def _apply_limit(kbps: int) -> None:
            spin.set_value(kbps)
            self.runner.submit(self.engine.set_speed_limit_kbps(kbps))
            self.btn_speed_limit.set_label(self._format_limit_button_label(kbps))
            pop.popdown()

        btn_unlimited.connect("clicked", lambda *_: _apply_limit(0))
        btn_apply.connect("clicked", lambda *_: _apply_limit(int(spin.get_value())))

        btn_box.append(btn_unlimited)
        btn_box.append(btn_apply)
        box.append(btn_box)

        pop.set_child(box)
        return pop

    def _format_limit_button_label(self, kbps: Optional[int] = None) -> str:
        val = self.engine.speed_limit_kbps if kbps is None else kbps
        if val <= 0:
            return "Límite: Sin límite"
        if val >= 1024:
            return f"Límite: {val / 1024.0:.1f} MB/s"
        return f"Límite: {val} KB/s"

    def _populate_initial_rows(self) -> None:
        for item in self.engine.items.values():
            row_obj = DownloadRowObject(item)
            self._row_map[item.id] = row_obj
            self.list_store.append(row_obj)

    def _on_ui_tick(self) -> bool:
        current_ids = set(self.engine.items.keys())
        known_ids = set(self._row_map.keys())

        for item_id in sorted(current_ids - known_ids):
            item = self.engine.items.get(item_id)
            if item:
                row_obj = DownloadRowObject(item)
                self._row_map[item_id] = row_obj
                self.list_store.append(row_obj)

        for item_id in known_ids - current_ids:
            row_obj = self._row_map.pop(item_id, None)
            self._item_widgets.pop(item_id, None)
            if row_obj:
                n = self.list_store.get_n_items()
                for idx in range(n):
                    if self.list_store.get_item(idx) == row_obj:
                        self.list_store.remove(idx)
                        break

        for item_id, row_obj in self._row_map.items():
            item = self.engine.items.get(item_id)
            if not item:
                continue
            if row_obj.sync_from_item(item):
                widgets = self._item_widgets.get(item_id, {})
                for attr in ("filename", "file_ext", "origin", "container", "host", "size_str", "speed_str", "eta_str", "status_str"):
                    w = widgets.get(attr)
                    if isinstance(w, Gtk.Label):
                        val_str = str(getattr(row_obj, attr))
                        w.set_text(val_str)
                        w.set_tooltip_text(val_str)
                pbar = widgets.get("progress")
                if isinstance(pbar, Gtk.ProgressBar):
                    pbar.set_fraction(row_obj.progress_fraction)
                    pbar.set_text(row_obj.progress_text)

        self._refresh_container_dropdown()
        self.lbl_total_speed.set_text(f"Velocidad Total: {format_speed_mbs(self.runner.total_speed_bps)}")
        self.lbl_active.set_text(f"Descargas activas: {self.runner.active_count} / {self.engine.max_concurrent}")

        # JDownloader-style Clipboard Watcher: every ~1.5s, if the user copied a direct
        # download*.mediafire.com URL while there are waiting MediaFire tasks, auto-bind it!
        self._tick_counter += 1
        if self._tick_counter % 6 == 0:
            has_waiting_mf = any(
                it.host == "MediaFire" and not it.direct_url and it.status in ("error", "paused", "queued")
                for it in self.engine.items.values()
            )
            if has_waiting_mf:
                clipboard = Gdk.Display.get_default().get_clipboard()

                def _on_clip_watch(cb, res) -> None:
                    try:
                        txt = cb.read_text_finish(res)
                        if not txt:
                            return
                        for u in URL_EXTRACT_RE.findall(txt):
                            if "download" in u and "mediafire.com" in u and u not in self._seen_clipboard_links:
                                self._seen_clipboard_links.add(u)
                                self.runner.submit(
                                    self.engine.add_urls([u], save_dir=self.engine.save_dir, auto_start=True)
                                )
                    except Exception:
                        pass

                clipboard.read_text_async(None, _on_clip_watch)

        return True

    def _get_selected_item_ids(self) -> List[int]:
        bitset = self.selection_model.get_selection()
        selected: List[int] = []
        n = self.filter_list_model.get_n_items()
        for idx in range(n):
            if bitset.contains(idx):
                row_obj: DownloadRowObject = self.filter_list_model.get_item(idx)
                if row_obj:
                    selected.append(row_obj.item_id)
        return selected

    def _on_add_torrent_clicked(self, _btn: Gtk.Button) -> None:
        dialog = Gtk.FileChooserDialog(
            title="Seleccionar archivo .torrent",
            transient_for=self,
            modal=True,
            action=Gtk.FileChooserAction.OPEN,
        )
        dialog.add_button("Cancelar", Gtk.ResponseType.CANCEL)
        btn_ok = dialog.add_button("Agregar .torrent", Gtk.ResponseType.ACCEPT)
        btn_ok.add_css_class("suggested-action")
        self._active_chooser = dialog

        filt = Gtk.FileFilter()
        filt.set_name("Archivos BitTorrent (*.torrent)")
        filt.add_pattern("*.torrent")
        dialog.add_filter(filt)

        try:
            dialog.set_current_folder(Gio.File.new_for_path(self.engine.save_dir))
        except Exception:
            pass

        def _on_response(dlg: Gtk.FileChooserDialog, response: int) -> None:
            if response == Gtk.ResponseType.ACCEPT:
                gfile = dlg.get_file()
                if gfile and gfile.get_path():
                    torrent_path = gfile.get_path()
                    cont = (
                        self._active_container_filter
                        if self._active_container_filter not in ("", "Todos")
                        else "General"
                    )
                    self.runner.submit(
                        self.engine.add_urls(
                            [torrent_path],
                            save_dir=self.engine.save_dir,
                            auto_start=True,
                            container=cont,
                        )
                    )
            dlg.destroy()
            self._active_chooser = None

        dialog.connect("response", _on_response)
        dialog.present()

    def _on_add_links_clicked(self, _btn: Gtk.Button) -> None:
        dialog = Gtk.Window(
            title="Agregar enlaces (Directos, YouTube, Cloud, Magnet / Torrent)",
            transient_for=self,
            modal=True,
            default_width=690,
            default_height=430,
        )

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        vbox.set_margin_top(14)
        vbox.set_margin_bottom(14)
        vbox.set_margin_start(14)
        vbox.set_margin_end(14)
        dialog.set_child(vbox)

        info_lbl = Gtk.Label(
            label=(
                "Selecciona y copia texto con hipervínculos desde cualquier página web, enlaces de YouTube, "
                "MEGA, MediaFire o enlaces magnéticos (magnet:?xt=urn:btih:...):"
            ),
            xalign=0.0,
        )
        info_lbl.set_wrap(True)
        vbox.append(info_lbl)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        scrolled.set_hexpand(True)

        text_view = Gtk.TextView()
        text_view.set_monospace(True)
        text_view.set_wrap_mode(Gtk.WrapMode.CHAR)
        buffer = text_view.get_buffer()
        scrolled.set_child(text_view)
        vbox.append(scrolled)

        # Video metadata & dynamic resolution selector
        yt_status_lbl = Gtk.Label(
            label="Tip: Si pegas un enlace de YouTube, se consultarán automáticamente todas sus resoluciones.",
            xalign=0.0,
        )
        yt_status_lbl.set_ellipsize(Pango.EllipsizeMode.END)
        vbox.append(yt_status_lbl)

        quality_ids: List[str] = ["best", "2160p", "1440p", "1080p", "720p", "480p", "360p", "audio"]
        quality_labels: List[str] = [
            "Máxima calidad (best)",
            "2160p 4K UHD (MP4)",
            "1440p 2K QHD (MP4)",
            "1080p Full HD (MP4)",
            "720p HD (MP4)",
            "480p SD (MP4)",
            "360p (MP4)",
            "Solo Audio (MP3 192 kbps)",
        ]
        quality_model = Gtk.StringList.new(quality_labels)
        quality_combo = Gtk.DropDown(model=quality_model)
        quality_combo.set_selected(0)

        probed_url_state = {"last_url": "", "probing": False}

        def _probe_youtube_qualities(target_url: str) -> None:
            if probed_url_state["probing"] or not target_url:
                return
            probed_url_state["probing"] = True
            probed_url_state["last_url"] = target_url
            yt_status_lbl.set_text(f"⏳ Consultando calidades disponibles en YouTube para: {target_url[:60]}...")
            btn_probe_yt.set_sensitive(False)

            async def _fetch_task():
                return await self.engine.video_extractor.list_qualities(target_url)

            fut = self.runner.submit(_fetch_task())

            def _apply_result() -> bool:
                probed_url_state["probing"] = False
                btn_probe_yt.set_sensitive(True)
                try:
                    info = fut.result()
                    choices: List[VideoQualityChoice] = info.get("choices") or []
                    title = info.get("title") or "Video"
                    if choices:
                        quality_ids.clear()
                        new_labels: List[str] = []
                        for c in choices:
                            quality_ids.append(c.id)
                            new_labels.append(c.label)
                        quality_model.splice(0, quality_model.get_n_items(), new_labels)
                        # Select 1080p by default if available, otherwise best (0)
                        sel_idx = next((i for i, qid in enumerate(quality_ids) if qid == "1080p"), 0)
                        quality_combo.set_selected(sel_idx)
                        yt_status_lbl.set_text(f"🎬 Video detectado: {title} ({len(choices)} calidades disponibles)")
                except Exception as exc:
                    yt_status_lbl.set_text(f"⚠️ No se pudieron listar calidades específicas: {exc}")
                return False

            fut.add_done_callback(lambda _f: GLib.idle_add(_apply_result))

        def _check_buffer_for_video() -> None:
            s_i = buffer.get_start_iter()
            e_i = buffer.get_end_iter()
            urls = extract_urls_from_html_or_text(buffer.get_text(s_i, e_i, True))
            for u in urls:
                if is_video_or_stream_url(u) and u != probed_url_state["last_url"]:
                    _probe_youtube_qualities(u)
                    break

        clipboard = Gdk.Display.get_default().get_clipboard()

        def _populate_urls(urls: List[str]) -> None:
            if urls:
                buffer.set_text("\n".join(dict.fromkeys(urls)) + "\n")
                _check_buffer_for_video()

        read_clipboard_urls_async(clipboard, _populate_urls)

        key_ctrl = Gtk.EventControllerKey.new()

        def _on_key_pressed(_ctrl, keyval: int, _keycode: int, state: Gdk.ModifierType) -> bool:
            is_ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
            if is_ctrl and keyval in (Gdk.KEY_v, Gdk.KEY_V):
                def _append_pasted_urls(urls: List[str]) -> None:
                    if urls:
                        start_i = buffer.get_start_iter()
                        end_i = buffer.get_end_iter()
                        existing = extract_urls_from_html_or_text(buffer.get_text(start_i, end_i, True))
                        merged = list(dict.fromkeys(existing + urls))
                        buffer.set_text("\n".join(merged) + "\n")
                        _check_buffer_for_video()
                read_clipboard_urls_async(clipboard, _append_pasted_urls)
                return True
            return False

        key_ctrl.connect("key-pressed", _on_key_pressed)
        text_view.add_controller(key_ctrl)

        opts_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        opts_box.append(Gtk.Label(label="Calidad YouTube/Video:"))
        opts_box.append(quality_combo)

        btn_probe_yt = Gtk.Button(label="Detectar calidades")
        btn_probe_yt.set_tooltip_text("Consultar todas las resoluciones disponibles del video pegado")
        btn_probe_yt.connect(
            "clicked",
            lambda *_: (probed_url_state.update({"last_url": ""}), _check_buffer_for_video()),
        )
        opts_box.append(btn_probe_yt)

        opts_box.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        opts_box.append(Gtk.Label(label="Contenedor:"))
        container_entry = Gtk.Entry()
        container_entry.set_placeholder_text("General (o nombre de paquete)")
        container_entry.set_hexpand(True)
        if self._active_container_filter not in ("", "Todos", "General"):
            container_entry.set_text(self._active_container_filter)
        opts_box.append(container_entry)

        vbox.append(opts_box)

        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        btn_cancel = Gtk.Button(label="Cancelar")
        btn_paste = Gtk.Button(label="Extraer enlaces del portapapeles")
        btn_accept = Gtk.Button(label="Agregar y Descargar")
        btn_accept.add_css_class("suggested-action")

        def _paste_manually(_b) -> None:
            read_clipboard_urls_async(clipboard, _populate_urls)

        def _confirm_add(_b) -> None:
            start_iter = buffer.get_start_iter()
            end_iter = buffer.get_end_iter()
            raw_text = buffer.get_text(start_iter, end_iter, True)
            urls = extract_urls_from_html_or_text(raw_text)
            if not urls:
                urls = [
                    line.strip()
                    for line in raw_text.splitlines()
                    if line.strip().startswith(("magnet:?", "http://", "https://", "ftp://", "/"))
                ]
            if urls:
                sel_idx = quality_combo.get_selected()
                q_val = (
                    quality_ids[sel_idx]
                    if (sel_idx != Gtk.INVALID_LIST_POSITION and sel_idx < len(quality_ids))
                    else "best"
                )
                cont_val = container_entry.get_text().strip() or "General"
                self.runner.submit(
                    self.engine.add_urls(
                        urls,
                        save_dir=self.engine.save_dir,
                        quality=q_val,
                        auto_start=True,
                        container=cont_val,
                    )
                )
            dialog.destroy()

        btn_cancel.connect("clicked", lambda *_: dialog.destroy())
        btn_paste.connect("clicked", _paste_manually)
        btn_accept.connect("clicked", _confirm_add)

        btn_row.append(btn_paste)
        btn_row.append(spacer)
        btn_row.append(btn_cancel)
        btn_row.append(btn_accept)
        vbox.append(btn_row)

        dialog.present()

    def _on_start_clicked(self, _btn: Gtk.Button) -> None:
        sel = self._get_selected_item_ids()
        if sel:
            for item_id in sel:
                self.runner.submit(self.engine.start_task(item_id))
        else:
            self.runner.submit(self.engine.start_all())

    def _on_pause_clicked(self, _btn: Gtk.Button) -> None:
        sel = self._get_selected_item_ids()
        if sel:
            for item_id in sel:
                self.runner.submit(self.engine.pause_task(item_id))
        else:
            self.runner.submit(self.engine.pause_all())

    def _on_stop_clicked(self, _btn: Gtk.Button) -> None:
        sel = self._get_selected_item_ids()
        targets = sel if sel else list(self.engine.items.keys())
        for item_id in targets:
            self.runner.submit(self.engine.stop_task(item_id))

    def _on_clear_completed_clicked(self, _btn: Gtk.Button) -> None:
        self.runner.submit(self.engine.clear_completed())

    def _on_assign_container_clicked(self, _btn: Gtk.Button) -> None:
        sel = self._get_selected_item_ids()
        dialog = Gtk.Window(
            title="Asignar a Contenedor",
            transient_for=self,
            modal=True,
            default_width=440,
            default_height=180,
        )
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        vbox.set_margin_top(14)
        vbox.set_margin_bottom(14)
        vbox.set_margin_start(14)
        vbox.set_margin_end(14)
        dialog.set_child(vbox)

        target_desc = (
            f"{len(sel)} enlace(s) seleccionado(s)"
            if sel
            else "todos los enlaces visibles en la lista"
        )
        lbl = Gtk.Label(
            label=f"Ingresa el nombre del contenedor para agrupar {target_desc}:",
            xalign=0.0,
        )
        lbl.set_wrap(True)
        vbox.append(lbl)

        entry = Gtk.Entry()
        entry.set_placeholder_text("Ej: Temporada 1, Películas, Curso Python...")
        if sel:
            first_item = self.engine.items.get(sel[0])
            if first_item:
                entry.set_text(first_item.container_name)
        vbox.append(entry)

        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        btn_cancel = Gtk.Button(label="Cancelar")
        btn_apply = Gtk.Button(label="Guardar en Contenedor")
        btn_apply.add_css_class("suggested-action")

        def _do_assign(_b) -> None:
            name = entry.get_text().strip() or "General"
            ids_to_update = sel if sel else list(self.engine.items.keys())
            self.engine.assign_container(ids_to_update, name)
            self._refresh_container_dropdown()
            self.custom_filter.changed(Gtk.FilterChange.DIFFERENT)
            dialog.destroy()

        btn_cancel.connect("clicked", lambda *_: dialog.destroy())
        btn_apply.connect("clicked", _do_assign)
        entry.connect("activate", _do_assign)

        btn_row.append(spacer)
        btn_row.append(btn_cancel)
        btn_row.append(btn_apply)
        vbox.append(btn_row)
        dialog.present()

    def _on_row_activated(self, _cv: Gtk.ColumnView, position: int) -> None:
        row_obj: DownloadRowObject = self.filter_list_model.get_item(position)
        if not row_obj:
            return
        self._open_item_file_or_folder(row_obj.item_id)

    def _on_open_in_browser_clicked(self, _btn: Gtk.Button) -> None:
        sel = self._get_selected_item_ids()
        if not sel:
            # Open the first MediaFire item that needs browser verification
            for it in self.engine.items.values():
                if it.host == "MediaFire" and not it.direct_url and it.status == "error":
                    sel = [it.id]
                    break
        for item_id in sel[:3]:
            item = self.engine.items.get(item_id)
            if item and item.url:
                Gio.AppInfo.launch_default_for_uri(item.url, None)

    def _on_open_save_dir_clicked(self, _btn: Gtk.Button) -> None:
        sel = self._get_selected_item_ids()
        if sel:
            item = self.engine.items.get(sel[0])
            if item and os.path.isdir(item.save_dir):
                open_path_with_system(item.save_dir)
                return
        open_path_with_system(self.engine.save_dir)

    def _on_remove_clicked(self, _btn: Gtk.Button) -> None:
        sel = self._get_selected_item_ids()
        if not sel:
            return

        dialog = Gtk.Window(
            title="Eliminar descargas",
            transient_for=self,
            modal=True,
            default_width=420,
            default_height=160,
        )
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        vbox.set_margin_top(14)
        vbox.set_margin_bottom(14)
        vbox.set_margin_start(14)
        vbox.set_margin_end(14)
        dialog.set_child(vbox)

        lbl = Gtk.Label(
            label=f"¿Deseas eliminar {len(sel)} enlace(s) seleccionado(s) de la cola?",
            xalign=0.0,
        )
        vbox.append(lbl)

        chk_files = Gtk.CheckButton(
            label="Eliminar también archivos temporales (.aria2 / parciales) del disco"
        )
        chk_files.set_active(True)
        vbox.append(chk_files)

        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        btn_cancel = Gtk.Button(label="Cancelar")
        btn_del = Gtk.Button(label="Eliminar")
        btn_del.add_css_class("destructive-action")

        def _do_delete(_b) -> None:
            del_disk = chk_files.get_active()
            for item_id in sel:
                self.runner.submit(self.engine.remove_task(item_id, delete_files=del_disk))
            dialog.destroy()

        btn_cancel.connect("clicked", lambda *_: dialog.destroy())
        btn_del.connect("clicked", _do_delete)

        btn_row.append(spacer)
        btn_row.append(btn_cancel)
        btn_row.append(btn_del)
        vbox.append(btn_row)
        dialog.present()

    def _on_folder_clicked(self, _btn: Gtk.Button) -> None:
        dialog = Gtk.FileChooserDialog(
            title="Seleccionar carpeta de destino",
            transient_for=self,
            modal=True,
            action=Gtk.FileChooserAction.SELECT_FOLDER,
        )
        dialog.add_button("Cancelar", Gtk.ResponseType.CANCEL)
        btn_ok = dialog.add_button("Seleccionar carpeta", Gtk.ResponseType.ACCEPT)
        btn_ok.add_css_class("suggested-action")
        self._active_chooser = dialog

        try:
            dialog.set_current_folder(Gio.File.new_for_path(self.engine.save_dir))
        except Exception:
            pass

        def _on_response(dlg: Gtk.FileChooserDialog, response: int) -> None:
            if response == Gtk.ResponseType.ACCEPT:
                gfile = dlg.get_file()
                if gfile and gfile.get_path():
                    new_dir = gfile.get_path()
                    self.engine.set_save_dir(new_dir)
                    self.lbl_folder.set_text(f"Destino: {new_dir}")
                    self.btn_folder.set_tooltip_text(f"Cambiar carpeta de destino (Actual: {new_dir})")
            dlg.destroy()
            self._active_chooser = None

        dialog.connect("response", _on_response)
        dialog.present()

    def _on_theme_clicked(self, _btn: Gtk.Button) -> None:
        dialog = Gtk.Window(
            title="Personalizar fondo de Mint Downloader",
            transient_for=self,
            modal=True,
            default_width=500,
            default_height=340,
        )
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        vbox.set_margin_top(16)
        vbox.set_margin_bottom(16)
        vbox.set_margin_start(16)
        vbox.set_margin_end(16)
        dialog.set_child(vbox)

        lbl_intro = Gtk.Label(
            label="Elige el fondo para la ventana principal:",
            xalign=0.0,
        )
        vbox.append(lbl_intro)

        # Mode selector buttons
        chk_default = Gtk.CheckButton(label="Tema predeterminado del sistema")
        chk_color = Gtk.CheckButton(label="Color sólido personalizado", group=chk_default)
        chk_image = Gtk.CheckButton(label="Imagen de fondo personalizada", group=chk_default)

        cur_mode = getattr(self.engine, "bg_mode", "default")
        if cur_mode == "color":
            chk_color.set_active(True)
        elif cur_mode == "image":
            chk_image.set_active(True)
        else:
            chk_default.set_active(True)

        vbox.append(chk_default)

        # Color row: ColorDialogButton + quick swatches
        color_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        color_row.append(chk_color)

        color_dialog = Gtk.ColorDialog(title="Escoger color de fondo", with_alpha=False)
        color_btn = Gtk.ColorDialogButton(dialog=color_dialog)
        init_rgba = Gdk.RGBA()
        if not init_rgba.parse(getattr(self.engine, "bg_color", "#1e293b") or "#1e293b"):
            init_rgba.parse("#1e293b")
        color_btn.set_rgba(init_rgba)
        color_row.append(color_btn)

        selected_state = {
            "color": init_rgba.to_string(),
            "image": getattr(self.engine, "bg_image", "") or "",
        }

        def _on_rgba_changed(btn: Gtk.ColorDialogButton, _pspec) -> None:
            chk_color.set_active(True)
            selected_state["color"] = btn.get_rgba().to_string()
            self.engine.set_background_theme("color", color=selected_state["color"])
            self._apply_background_theme()

        color_btn.connect("notify::rgba", _on_rgba_changed)

        swatches = [
            ("#0f172a", "Noche"),
            ("#1e293b", "Pizarra"),
            ("#064e3b", "Verde Mint"),
            ("#1e1b4b", "Índigo"),
            ("#3b0764", "Púrpura"),
            ("#f1f5f9", "Claro"),
        ]
        sw_css = []
        for idx, (hex_col, _tip) in enumerate(swatches):
            sw_css.append(
                f".bg-swatch-{idx} {{ background-color: {hex_col}; background-image: none; min-width: 24px; min-height: 24px; border-radius: 6px; padding: 0; }}"
            )
        sw_prov = Gtk.CssProvider()
        sw_prov.load_from_data("\n".join(sw_css).encode("utf-8"))
        if Gdk.Display.get_default():
            Gtk.StyleContext.add_provider_for_display(
                Gdk.Display.get_default(),
                sw_prov,
                Gtk.STYLE_PROVIDER_PRIORITY_USER,
            )

        for idx, (hex_col, tip) in enumerate(swatches):
            sw_btn = Gtk.Button()
            sw_btn.set_tooltip_text(tip)
            sw_btn.set_size_request(26, 26)
            sw_btn.add_css_class(f"bg-swatch-{idx}")

            def _pick_swatch(_b, c_hex=hex_col) -> None:
                chk_color.set_active(True)
                r = Gdk.RGBA()
                r.parse(c_hex)
                color_btn.set_rgba(r)
                selected_state["color"] = c_hex
                self.engine.set_background_theme("color", color=c_hex)
                self._apply_background_theme()

            sw_btn.connect("clicked", _pick_swatch)
            color_row.append(sw_btn)

        vbox.append(color_row)

        # Image row
        img_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        img_row.append(chk_image)
        btn_pick_img = Gtk.Button(label="Seleccionar imagen...")
        lbl_img_path = Gtk.Label(
            label=os.path.basename(selected_state["image"]) if selected_state["image"] else "Ninguna seleccionada",
            xalign=0.0,
        )
        lbl_img_path.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        lbl_img_path.set_hexpand(True)

        def _on_pick_image(_b) -> None:
            img_dlg = Gtk.FileChooserDialog(
                title="Seleccionar imagen de fondo",
                transient_for=dialog,
                modal=True,
                action=Gtk.FileChooserAction.OPEN,
            )
            img_dlg.add_button("Cancelar", Gtk.ResponseType.CANCEL)
            ok_b = img_dlg.add_button("Usar imagen", Gtk.ResponseType.ACCEPT)
            ok_b.add_css_class("suggested-action")
            self._active_chooser = img_dlg

            filt = Gtk.FileFilter()
            filt.set_name("Imágenes (*.png, *.jpg, *.jpeg, *.webp, *.svg)")
            for pat in ("*.png", "*.jpg", "*.jpeg", "*.webp", "*.bmp", "*.svg", "*.PNG", "*.JPG", "*.JPEG"):
                filt.add_pattern(pat)
            img_dlg.add_filter(filt)

            def _on_img_resp(d: Gtk.FileChooserDialog, resp: int) -> None:
                if resp == Gtk.ResponseType.ACCEPT:
                    gf = d.get_file()
                    if gf and gf.get_path():
                        p = gf.get_path()
                        selected_state["image"] = p
                        lbl_img_path.set_text(os.path.basename(p))
                        chk_image.set_active(True)
                        self.engine.set_background_theme(
                            "image",
                            image_path=p,
                            opacity=scale_opacity.get_value() / 100.0,
                        )
                        self._apply_background_theme()
                d.destroy()
                self._active_chooser = None

            img_dlg.connect("response", _on_img_resp)
            img_dlg.present()

        btn_pick_img.connect("clicked", _on_pick_image)
        img_row.append(btn_pick_img)
        img_row.append(lbl_img_path)
        vbox.append(img_row)

        # Opacity / readability scrim slider for background image
        op_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        op_box.append(Gtk.Label(label="Oscurecer imagen para lectura:"))
        scale_opacity = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 85, 5)
        scale_opacity.set_hexpand(True)
        scale_opacity.set_value(int(getattr(self.engine, "bg_opacity", 0.55) * 100))

        def _on_op_changed(sc: Gtk.Scale) -> None:
            if chk_image.get_active() and selected_state["image"]:
                self.engine.set_background_theme(
                    "image",
                    image_path=selected_state["image"],
                    opacity=sc.get_value() / 100.0,
                )
                self._apply_background_theme()

        scale_opacity.connect("value-changed", _on_op_changed)
        op_box.append(scale_opacity)
        vbox.append(op_box)

        def _on_mode_toggled(_chk) -> None:
            if chk_default.get_active():
                self.engine.set_background_theme("default")
                self._apply_background_theme()
            elif chk_color.get_active():
                self.engine.set_background_theme("color", color=selected_state["color"])
                self._apply_background_theme()
            elif chk_image.get_active() and selected_state["image"]:
                self.engine.set_background_theme(
                    "image",
                    image_path=selected_state["image"],
                    opacity=scale_opacity.get_value() / 100.0,
                )
                self._apply_background_theme()

        chk_default.connect("toggled", _on_mode_toggled)
        chk_color.connect("toggled", _on_mode_toggled)
        chk_image.connect("toggled", _on_mode_toggled)

        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        sp = Gtk.Box()
        sp.set_hexpand(True)
        btn_close = Gtk.Button(label="Listo")
        btn_close.add_css_class("suggested-action")
        btn_close.connect("clicked", lambda *_: dialog.destroy())
        btn_row.append(sp)
        btn_row.append(btn_close)
        vbox.append(btn_row)

        dialog.present()

    def _on_concurrency_changed(self, spin: Gtk.SpinButton) -> None:
        val = int(spin.get_value())
        self.runner.submit(self.engine.set_max_concurrent(val))

    def _on_close_request(self, _win: Gtk.Window) -> bool:
        if self._tick_source_id:
            GLib.source_remove(self._tick_source_id)
            self._tick_source_id = 0
        self.runner.stop()
        return False


class MintDownloaderApp(Gtk.Application):
    """GTK4 Application wrapper."""

    def __init__(self, runner: AsyncEngineRunner) -> None:
        super().__init__(
            application_id="org.linuxmint.MintDownloader",
            flags=Gio.ApplicationFlags.FLAGS_NONE,
        )
        self.runner = runner
        self.window: Optional[MintDownloaderWindow] = None

    def do_activate(self) -> None:
        if not self.window:
            self.window = MintDownloaderWindow(self, self.runner)
        self.window.present()
