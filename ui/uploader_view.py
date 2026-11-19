"""
GTK4 Native User Interface for Mint Uploader (integrated inside Mint Downloader).
Mirrors the exact visual design, dark card aesthetics, 2-row equal-sized vertical-icon
control deck, left category sidebar, 8-column table with percentage-over-bar progress,
and status bar of Mint Downloader — 100% in Spanish.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
from typing import Callable, Dict, List, Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Gio", "2.0")
from gi.repository import Gdk, Gio, GLib, GObject, Gtk, Pango

from core.account_vault import (
    PROVIDER_AUTH_MODE,
    PROVIDER_DISPLAY_NAMES,
    UPLOAD_PROVIDERS,
    CloudAccount,
)
from core.engine import DownloadEngine
from core.upload_engine import UploadEngine, UploadItem
from core.uploaders import parse_google_credentials_json
from ui.cloud_explorer import CloudExplorerPanel


_SPLIT_ARCH_RE = re.compile(r"\.(?:zip|rar|7z)\.\d{3}$")

UPLOAD_CATEGORY_EXTENSIONS: Dict[str, set[str]] = {
    "music": {".mp3", ".flac", ".wav", ".aac", ".ogg", ".opus", ".m4a", ".wma", ".aiff", ".alac", ".mid", ".midi"},
    "compressed": {
        ".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".zst", ".tgz", ".tbz2",
        ".iso", ".cab", ".lzma", ".tar.gz", ".tar.bz2", ".tar.xz", ".tar.zst",
    },
    "videos": {
        ".mp4", ".mkv", ".webm", ".avi", ".mov", ".flv", ".wmv", ".m4v", ".mpg", ".mpeg", ".ts", ".ogv", ".3gp",
    },
    "programs": {
        ".exe", ".msi", ".deb", ".rpm", ".appimage", ".flatpak", ".snap", ".sh",
        ".bin", ".dmg", ".pkg", ".run", ".jar", ".bat", ".cmd",
    },
    "documents": {
        ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".txt", ".csv",
        ".odt", ".ods", ".odp", ".rtf", ".epub", ".mobi", ".md", ".json", ".xml",
    },
    "apks": {".apk", ".xapk", ".apkm", ".apks"},
    "images": {".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg", ".bmp", ".ico", ".tiff", ".tif", ".heic", ".avif", ".raw"},
}

UPLOAD_CATEGORY_ICONS: Dict[str, str] = {
    "music": "audio-x-generic-symbolic",
    "compressed": "package-x-generic-symbolic",
    "videos": "video-x-generic-symbolic",
    "programs": "application-x-executable-symbolic",
    "documents": "x-office-document-symbolic",
    "apks": "phone-symbolic",
    "images": "image-x-generic-symbolic",
    "other": "text-x-generic-symbolic",
}

UPLOAD_SIDEBAR_CATEGORIES: list[tuple[str, str, str]] = [
    ("music", "Música", "audio-x-generic-symbolic"),
    ("compressed", "Comprimidos", "package-x-generic-symbolic"),
    ("videos", "Videos", "video-x-generic-symbolic"),
    ("programs", "Programas", "application-x-executable-symbolic"),
    ("documents", "Documentos", "x-office-document-symbolic"),
    ("apks", "APKs", "phone-symbolic"),
    ("images", "Imágenes", "image-x-generic-symbolic"),
]

PROVIDER_OAUTH_GUIDES: Dict[str, str] = {
    "gdrive": (
        "1. Abre console.cloud.google.com y crea un proyecto.\n"
        "2. Activa la «Google Drive API» en APIs y servicios.\n"
        "3. Crea credenciales OAuth 2.0 (App de escritorio) y descarga credentials.json\n"
        "   o copia tu Client ID y Client Secret.\n"
        "4. Agrega como URI de redirección: http://127.0.0.1:16895/oauth/callback"
    ),
    "onedrive": (
        "1. Abre portal.azure.com → Registros de aplicaciones → Nuevo registro.\n"
        "2. Tipo: Cuentas personales y organizativas.\n"
        "3. URI de redirección (Cliente público/nativo): http://127.0.0.1:16895/oauth/callback\n"
        "4. Copia el «Id. de aplicación (cliente)» en el campo Client ID."
    ),
    "dropbox": (
        "1. Abre dropbox.com/developers/apps → Create app (Scoped access / Full Dropbox).\n"
        "2. En «Permissions» activa files.content.write, files.content.read y sharing.write.\n"
        "3. En «Redirect URIs» agrega: http://127.0.0.1:16895/oauth/callback\n"
        "4. Copia el «App key» en el campo Client ID."
    ),
}


def classify_upload_category(ext_str: str) -> str:
    ext = (ext_str or "").lower().strip()
    if _SPLIT_ARCH_RE.search(ext):
        return "compressed"
    for cat_key, exts in UPLOAD_CATEGORY_EXTENSIONS.items():
        if ext in exts:
            return cat_key
    return "other"


def _fmt_size(num_bytes: int) -> str:
    if num_bytes <= 0:
        return "—"
    val = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if val < 1024.0 or unit == "TB":
            return f"{val:.1f} {unit}" if unit != "B" else f"{int(val)} B"
        val /= 1024.0
    return f"{val:.1f} TB"


def _fmt_speed(speed_bps: float) -> str:
    if speed_bps <= 0:
        return "0.00 MB/s"
    return f"{speed_bps / (1024.0 * 1024.0):.2f} MB/s"


def _fmt_eta(seconds: int) -> str:
    if seconds <= 0:
        return "—"
    if seconds >= 86400:
        return f"{seconds // 86400}d {(seconds % 86400) // 3600}h"
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:02d}" if h > 0 else f"{m:02d}:{s:02d}"


def _fmt_pct(pct: float, status: str) -> str:
    if status == "complete" or pct >= 99.95:
        return "100%"
    if pct <= 0.0:
        return "0%"
    if abs(pct - round(pct)) < 0.05:
        return f"{int(round(pct))}%"
    return f"{pct:.1f}%"


class UploadRowObject(GObject.Object):
    """GObject wrapper representing a single row in the Mint Uploader ColumnView."""

    __gtype_name__ = "UploadRowObject"

    def __init__(self, item: UploadItem) -> None:
        super().__init__()
        self.item_id: int = item.id
        self.account_id: int = item.account_id
        self.provider: str = item.provider
        self.filename: str = ""
        self.file_ext: str = "—"
        self.destination: str = ""
        self.size_str: str = ""
        self.progress_fraction: float = 0.0
        self.progress_text: str = "0%"
        self.speed_str: str = "0.00 MB/s"
        self.eta_str: str = "—"
        self.status_str: str = ""
        self.status_raw: str = ""
        self.category: str = "other"
        self.icon_name: str = "text-x-generic-symbolic"
        self.share_url: str = ""
        self.sync_from_item(item)

    def sync_from_item(self, item: UploadItem) -> bool:
        new_filename = item.filename
        new_ext = item.file_extension
        new_dest = item.destination_display
        if item.total_size > 0:
            new_size = f"{_fmt_size(item.uploaded_size)} / {_fmt_size(item.total_size)}"
        elif item.uploaded_size > 0:
            new_size = _fmt_size(item.uploaded_size)
        else:
            new_size = "—"

        new_frac = item.progress_fraction
        new_pct = _fmt_pct(item.progress_percent, item.status)
        new_speed = _fmt_speed(item.speed)
        new_eta = _fmt_eta(item.eta) if item.status == "uploading" else "—"
        new_status = item.status_display
        new_raw = item.status
        new_cat = classify_upload_category(new_ext)
        new_icon = UPLOAD_CATEGORY_ICONS.get(new_cat, "text-x-generic-symbolic")
        new_share = item.share_url

        changed = (
            self.filename != new_filename
            or self.file_ext != new_ext
            or self.destination != new_dest
            or self.size_str != new_size
            or abs(self.progress_fraction - new_frac) > 0.0005
            or self.progress_text != new_pct
            or self.speed_str != new_speed
            or self.eta_str != new_eta
            or self.status_str != new_status
            or self.status_raw != new_raw
            or self.category != new_cat
            or self.icon_name != new_icon
            or self.share_url != new_share
        )
        if changed:
            self.filename = new_filename
            self.file_ext = new_ext
            self.destination = new_dest
            self.size_str = new_size
            self.progress_fraction = new_frac
            self.progress_text = new_pct
            self.speed_str = new_speed
            self.eta_str = new_eta
            self.status_str = new_status
            self.status_raw = new_raw
            self.category = new_cat
            self.icon_name = new_icon
            self.share_url = new_share
        return changed


class UploaderPanel:
    """
    Complete Mint Uploader workspace panel mounted inside MintDownloaderWindow's flip Gtk.Stack.
    """

    def __init__(
        self,
        parent_window: Gtk.ApplicationWindow,
        upload_engine: UploadEngine,
        download_engine: DownloadEngine,
        submit_coro: Callable,
        open_theme_cb: Callable,
        open_path_cb: Callable[[str], None],
        on_switch_to_downloader_cb: Callable[[], None],
        on_accounts_changed_cb: Optional[Callable[[int], None]] = None,
    ) -> None:
        self.win = parent_window
        self.engine = upload_engine
        self.download_engine = download_engine
        self.submit = submit_coro
        self._open_theme_cb = open_theme_cb
        self._open_path_cb = open_path_cb
        self._on_switch_to_downloader_cb = on_switch_to_downloader_cb
        self._on_accounts_changed_cb = on_accounts_changed_cb

        self._row_map: Dict[int, UploadRowObject] = {}
        self._item_widgets: Dict[int, Dict[str, Gtk.Widget]] = {}
        self._sidebar_row_widgets: Dict[tuple[str, str], Gtk.Widget] = {}
        self._sidebar_account_widgets: Dict[int, Gtk.Widget] = {}
        self._active_right_view: str = "queue"  # "queue" or "cloud"
        self._active_cloud_account_id: int = 0
        self._active_status_filter: str = "all"
        self._active_category_filter: str = "all"
        self._active_account_filter_id: int = 0  # 0 = Todas
        self._known_account_entries: List[tuple[int, str]] = [(0, "Todas")]
        self._search_query: str = ""
        self._syncing_select_all: bool = False
        self._last_state_version: int = -1
        self._last_accounts_version: int = -1
        self._last_stats: tuple[float, int, int] = (-1.0, -1, -1)
        self._active_chooser: Optional[Gtk.Widget] = None

        self.root = self._build_ui()
        self._populate_initial_rows()
        self._refresh_accounts_ui()

    def _create_modal(
        self,
        title: str,
        width: int,
        height: int,
        spacing: int = 12,
        margin: int = 14,
    ) -> tuple[Gtk.Window, Gtk.Box]:
        dialog = Gtk.Window(
            title=title,
            transient_for=self.win,
            modal=True,
            default_width=width,
            default_height=height,
        )
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=spacing)
        vbox.set_margin_top(margin)
        vbox.set_margin_bottom(margin)
        vbox.set_margin_start(margin)
        vbox.set_margin_end(margin)
        dialog.set_child(vbox)
        return dialog, vbox

    def _make_toolbar_button(self, icon_name: str, label_text: str, tooltip: str, callback) -> Gtk.Button:
        btn = Gtk.Button()
        btn.add_css_class("pro-btn")
        btn.set_tooltip_text(tooltip)
        btn.set_valign(Gtk.Align.FILL)
        btn.set_halign(Gtk.Align.FILL)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.set_halign(Gtk.Align.CENTER)
        box.set_valign(Gtk.Align.CENTER)

        img = Gtk.Image.new_from_icon_name(icon_name)
        img.set_pixel_size(18)
        img.set_halign(Gtk.Align.CENTER)

        lbl = Gtk.Label(label=label_text)
        lbl.set_justify(Gtk.Justification.CENTER)
        lbl.set_halign(Gtk.Align.CENTER)
        lbl.set_wrap(True)
        lbl.set_wrap_mode(Pango.WrapMode.WORD)
        lbl.set_lines(2)
        lbl.set_max_width_chars(13)

        box.append(img)
        box.append(lbl)
        btn.set_child(box)
        btn.connect("clicked", callback)
        self._toolbar_size_group.add_widget(btn)
        return btn

    def _build_sidebar_group(
        self,
        group_key: str,
        title: str,
        icon_name: str,
        default_expanded: bool = False,
    ) -> Gtk.Box:
        wrapper = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)

        group_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        group_row.add_css_class("sidebar-group-row")

        btn_main = Gtk.Button()
        btn_main.add_css_class("sidebar-main-btn")
        btn_main.set_hexpand(True)
        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        btn_icon = Gtk.Image.new_from_icon_name(icon_name)
        btn_icon.set_pixel_size(16)
        btn_lbl = Gtk.Label(label=title, xalign=0.0)
        btn_lbl.set_hexpand(True)
        btn_box.append(btn_icon)
        btn_box.append(btn_lbl)
        btn_main.set_child(btn_box)
        btn_main.connect("clicked", lambda _b, gk=group_key: self._select_sidebar_filter(gk, "all"))

        chevron_icon = Gtk.Image.new_from_icon_name(
            "pan-up-symbolic" if default_expanded else "pan-down-symbolic"
        )
        chevron_icon.set_pixel_size(14)
        btn_chevron = Gtk.Button()
        btn_chevron.add_css_class("sidebar-chevron-btn")
        btn_chevron.set_child(chevron_icon)
        btn_chevron.set_tooltip_text(f"Mostrar u ocultar categorías de {title}")

        group_row.append(btn_main)
        group_row.append(btn_chevron)
        wrapper.append(group_row)
        self._sidebar_row_widgets[(group_key, "all")] = group_row

        revealer = Gtk.Revealer()
        revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        revealer.set_reveal_child(default_expanded)

        sub_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        sub_box.set_margin_bottom(6)
        for cat_key, cat_label, cat_icon in UPLOAD_SIDEBAR_CATEGORIES:
            sub_btn = Gtk.Button()
            sub_btn.add_css_class("sidebar-sub-btn")
            row_content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            ic = Gtk.Image.new_from_icon_name(cat_icon)
            ic.set_pixel_size(15)
            lb = Gtk.Label(label=cat_label, xalign=0.0)
            lb.set_hexpand(True)
            row_content.append(ic)
            row_content.append(lb)
            sub_btn.set_child(row_content)
            sub_btn.connect(
                "clicked",
                lambda _b, gk=group_key, ck=cat_key: self._select_sidebar_filter(gk, ck),
            )
            sub_box.append(sub_btn)
            self._sidebar_row_widgets[(group_key, cat_key)] = sub_btn

        revealer.set_child(sub_box)
        wrapper.append(revealer)

        def _toggle_expand(_b) -> None:
            new_state = not revealer.get_reveal_child()
            revealer.set_reveal_child(new_state)
            chevron_icon.set_from_icon_name("pan-up-symbolic" if new_state else "pan-down-symbolic")

        btn_chevron.connect("clicked", _toggle_expand)
        return wrapper

    def _select_sidebar_filter(self, group_key: str, category_key: str) -> None:
        self._active_status_filter = group_key
        self._active_category_filter = category_key
        self._active_right_view = "queue"
        if hasattr(self, "_right_stack"):
            self._right_stack.set_visible_child_name("queue")
        self._update_sidebar_active_styles()
        self.custom_filter.changed(Gtk.FilterChange.DIFFERENT)
        self._update_selection_footer()

    def _select_cloud_account_view(self, account_id: int) -> None:
        """Switch the active cloud server and display its folders & files directly in the right panel."""
        self._active_cloud_account_id = account_id
        self._active_right_view = "cloud"
        if hasattr(self, "_right_stack"):
            self._right_stack.set_visible_child_name("cloud")
        self._update_sidebar_active_styles()
        if hasattr(self, "cloud_explorer"):
            self.cloud_explorer.select_account(account_id)

    def _show_upload_queue_view(self) -> None:
        self._active_right_view = "queue"
        if hasattr(self, "_right_stack"):
            self._right_stack.set_visible_child_name("queue")
        self._update_sidebar_active_styles()

    def _update_sidebar_active_styles(self) -> None:
        active_pair = (self._active_status_filter, self._active_category_filter)
        for pair, widget in self._sidebar_row_widgets.items():
            if self._active_right_view == "queue" and pair == active_pair:
                widget.add_css_class("sidebar-active")
            else:
                widget.remove_css_class("sidebar-active")
        for aid, widget in self._sidebar_account_widgets.items():
            if self._active_right_view == "cloud" and aid == self._active_cloud_account_id:
                widget.add_css_class("sidebar-active")
            else:
                widget.remove_css_class("sidebar-active")

    def _build_ui(self) -> Gtk.Box:
        page_root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        page_root.set_vexpand(True)
        page_root.set_hexpand(True)

        body_split = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        body_split.add_css_class("body-split")
        body_split.set_vexpand(True)
        body_split.set_hexpand(True)

        # --- LEFT COLUMN: Two clearly separated cards (1: Cuentas en la nube, 2: Todas las subidas...) ---
        left_column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        left_column.set_size_request(232, -1)
        left_column.set_hexpand(False)
        left_column.set_vexpand(True)

        # Card 1: Cuentas en la nube (server switcher buttons: "Nombre (Servidor)")
        accounts_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        accounts_card.add_css_class("sidebar-card")
        accounts_card.set_hexpand(False)
        accounts_card.set_vexpand(False)

        acc_hdr = Gtk.Label(label="Cuentas en la nube", xalign=0.0)
        acc_hdr.add_css_class("muted-label")
        acc_hdr.set_margin_start(8)
        acc_hdr.set_margin_top(2)
        accounts_card.append(acc_hdr)

        acc_scroll = Gtk.ScrolledWindow()
        acc_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        acc_scroll.set_propagate_natural_height(True)
        acc_scroll.set_max_content_height(190)

        self._sidebar_accounts_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        acc_scroll.set_child(self._sidebar_accounts_box)
        accounts_card.append(acc_scroll)

        btn_sb_add = Gtk.Button(label="+ Agregar cuenta")
        btn_sb_add.add_css_class("pro-btn-accent")
        btn_sb_add.set_margin_top(4)
        btn_sb_add.connect("clicked", self._on_add_account_clicked)
        accounts_card.append(btn_sb_add)

        left_column.append(accounts_card)

        # Card 2: Todas las subidas / Sin terminar / Terminadas (separate card below)
        categories_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        categories_card.add_css_class("sidebar-card")
        categories_card.set_hexpand(False)
        categories_card.set_vexpand(True)

        sidebar_scroll = Gtk.ScrolledWindow()
        sidebar_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        sidebar_scroll.set_vexpand(True)

        sidebar_items_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        sidebar_items_box.append(
            self._build_sidebar_group("all", "Todas las subidas", "go-up-symbolic", default_expanded=True)
        )
        sidebar_items_box.append(
            self._build_sidebar_group("unfinished", "Sin terminar", "folder-open-symbolic", default_expanded=False)
        )
        sidebar_items_box.append(
            self._build_sidebar_group("finished", "Terminadas", "folder-symbolic", default_expanded=False)
        )

        sidebar_scroll.set_child(sidebar_items_box)
        categories_card.append(sidebar_scroll)
        left_column.append(categories_card)

        self._update_sidebar_active_styles()

        body_split.append(left_column)

        # --- RIGHT STACK: "queue" (Upload queue) & "cloud" (Native Cloud Folder Explorer) ---
        self._right_stack = Gtk.Stack()
        self._right_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self._right_stack.set_transition_duration(180)
        self._right_stack.set_hexpand(True)
        self._right_stack.set_vexpand(True)

        # Page 1: Upload Queue Workspace
        right_panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        right_panel.set_hexpand(True)
        right_panel.set_vexpand(True)

        self._toolbar_size_group = Gtk.SizeGroup.new(Gtk.SizeGroupMode.BOTH)
        deck_card = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        deck_card.add_css_class("toolbar-deck")

        btn_grid = Gtk.Grid()
        btn_grid.set_row_spacing(6)
        btn_grid.set_column_spacing(6)

        self.btn_upload_files = self._make_toolbar_button(
            "document-send-symbolic",
            "Subir archivos",
            "Seleccionar uno o varios archivos locales para subir a una o varias cuentas sincronizadas",
            self._on_upload_files_clicked,
        )
        self.btn_upload_files.add_css_class("pro-btn-primary")

        self.btn_upload_folder = self._make_toolbar_button(
            "folder-new-symbolic",
            "Subir carpeta",
            "Subir todos los archivos de una carpeta local a tus cuentas en la nube",
            self._on_upload_folder_clicked,
        )
        self.btn_start = self._make_toolbar_button(
            "media-playback-start-symbolic",
            "Iniciar",
            "Iniciar o reanudar subidas seleccionadas (o toda la cola)",
            self._on_start_clicked,
        )
        self.btn_pause = self._make_toolbar_button(
            "media-playback-pause-symbolic",
            "Pausar",
            "Pausar subidas guardando el avance para reanudar luego",
            self._on_pause_clicked,
        )
        self.btn_stop = self._make_toolbar_button(
            "media-playback-stop-symbolic",
            "Detener",
            "Detener subidas seleccionadas",
            self._on_stop_clicked,
        )
        self.btn_add_account = self._make_toolbar_button(
            "contact-new-symbolic",
            "Agregar cuenta",
            "Sincronizar una cuenta nueva (GDrive, MEGA, OneDrive, Dropbox o Mediafire)",
            self._on_add_account_clicked,
        )
        self.btn_add_account.add_css_class("pro-btn-accent")

        self.btn_remove = self._make_toolbar_button(
            "user-trash-symbolic",
            "Eliminar",
            "Quitar subidas seleccionadas de la cola",
            self._on_remove_clicked,
        )
        self.btn_clear_completed = self._make_toolbar_button(
            "edit-clear-all-symbolic",
            "Limpiar completadas",
            "Quitar de la lista las subidas completadas",
            self._on_clear_completed_clicked,
        )
        self.btn_accounts = self._make_toolbar_button(
            "avatar-default-symbolic",
            "Cuentas",
            "Administrar cuentas sincronizadas, verificar sesión o desconectar",
            self._on_manage_accounts_clicked,
        )
        self.btn_copy_link = self._make_toolbar_button(
            "edit-copy-symbolic",
            "Copiar enlace",
            "Copiar al portapapeles el enlace compartido del archivo subido",
            self._on_copy_link_clicked,
        )
        self.btn_open_dir = self._make_toolbar_button(
            "folder-symbolic",
            "Abrir carpeta",
            "Abrir la carpeta local del archivo seleccionado",
            self._on_open_local_dir_clicked,
        )
        self.btn_theme = self._make_toolbar_button(
            "preferences-desktop-wallpaper-symbolic",
            "Fondo",
            "Personalizar color o imagen de fondo del programa",
            self._open_theme_cb,
        )

        # Row 0
        btn_grid.attach(self.btn_upload_files, 0, 0, 1, 1)
        btn_grid.attach(self.btn_upload_folder, 1, 0, 1, 1)
        sep1 = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        sep1.set_margin_start(3)
        sep1.set_margin_end(3)
        btn_grid.attach(sep1, 2, 0, 1, 2)
        btn_grid.attach(self.btn_start, 3, 0, 1, 1)
        btn_grid.attach(self.btn_pause, 4, 0, 1, 1)
        btn_grid.attach(self.btn_stop, 5, 0, 1, 1)
        sep2 = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        sep2.set_margin_start(3)
        sep2.set_margin_end(3)
        btn_grid.attach(sep2, 6, 0, 1, 2)
        btn_grid.attach(self.btn_add_account, 7, 0, 1, 1)

        # Row 1
        btn_grid.attach(self.btn_remove, 0, 1, 1, 1)
        btn_grid.attach(self.btn_clear_completed, 1, 1, 1, 1)
        btn_grid.attach(self.btn_accounts, 3, 1, 1, 1)
        btn_grid.attach(self.btn_copy_link, 4, 1, 1, 1)
        btn_grid.attach(self.btn_open_dir, 5, 1, 1, 1)
        btn_grid.attach(self.btn_theme, 7, 1, 1, 1)

        deck_card.append(btn_grid)

        deck_spacer = Gtk.Box()
        deck_spacer.set_hexpand(True)
        deck_card.append(deck_spacer)

        acc_filter_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        acc_filter_box.set_valign(Gtk.Align.CENTER)
        acc_filter_lbl = Gtk.Label(label="Cuenta:", xalign=0.0)
        acc_filter_lbl.add_css_class("muted-label")
        self.account_filter_model = Gtk.StringList.new(["Todas"])
        self.account_filter_dropdown = Gtk.DropDown(model=self.account_filter_model)
        self.account_filter_dropdown.set_tooltip_text("Filtrar subidas por cuenta sincronizada")
        self.account_filter_dropdown.connect("notify::selected", self._on_account_filter_changed)
        acc_filter_box.append(acc_filter_lbl)
        acc_filter_box.append(self.account_filter_dropdown)
        deck_card.append(acc_filter_box)

        right_panel.append(deck_card)

        # --- MAIN UPLOAD TABLE CARD ---
        table_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        table_card.add_css_class("table-card")
        table_card.set_vexpand(True)
        table_card.set_hexpand(True)

        self.list_store = Gio.ListStore.new(UploadRowObject)
        self.custom_filter = Gtk.CustomFilter.new(self._filter_row, None)
        self.filter_list_model = Gtk.FilterListModel.new(self.list_store, self.custom_filter)
        self.selection_model = Gtk.MultiSelection.new(self.filter_list_model)
        self.selection_model.connect("selection-changed", lambda *_: self._update_selection_footer())

        self.column_view = Gtk.ColumnView.new(self.selection_model)
        self.column_view.set_show_row_separators(True)
        self.column_view.set_show_column_separators(False)
        self.column_view.set_enable_rubberband(True)
        self.column_view.set_single_click_activate(False)
        self.column_view.set_vexpand(True)
        self.column_view.set_hexpand(True)
        self.column_view.connect("activate", self._on_row_activated)

        shortcut_ctrl = Gtk.ShortcutController.new()
        shortcut_ctrl.add_shortcut(
            Gtk.Shortcut.new(
                Gtk.ShortcutTrigger.parse_string("<Control>a"),
                Gtk.CallbackAction.new(lambda *_: (self.selection_model.select_all(), True)[1]),
            )
        )
        self.column_view.add_controller(shortcut_ctrl)

        self._add_name_column("Nombre", expand=True, min_width=230)
        self._add_text_column("Contenedor", "file_ext", expand=False, min_width=95)
        self._add_text_column("Destino", "destination", expand=False, min_width=155)
        self._add_text_column("Tamaño", "size_str", expand=False, min_width=135)
        self._add_progress_column("Progreso", min_width=165)
        self._add_text_column("Velocidad", "speed_str", expand=False, min_width=105)
        self._add_text_column("Tiempo Restante", "eta_str", expand=False, min_width=125)
        self._add_text_column("Estado", "status_str", expand=False, min_width=145)

        scrolled = Gtk.ScrolledWindow()
        scrolled.add_css_class("table-area")
        scrolled.set_vexpand(True)
        scrolled.set_hexpand(True)
        scrolled.set_child(self.column_view)
        table_card.append(scrolled)

        table_card.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        table_footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        table_footer.add_css_class("table-footer")

        self.chk_select_all = Gtk.CheckButton(label="Seleccionar todo")
        self.chk_select_all.connect("toggled", self._on_select_all_toggled)
        table_footer.append(self.chk_select_all)

        tf_spacer = Gtk.Box()
        tf_spacer.set_hexpand(True)
        table_footer.append(tf_spacer)

        self.lbl_selection_info = Gtk.Label(label="0 elementos")
        self.lbl_selection_info.add_css_class("muted-label")
        table_footer.append(self.lbl_selection_info)

        table_card.append(table_footer)
        right_panel.append(table_card)

        self._right_stack.add_named(right_panel, "queue")

        # Page 2: Embedded Native Cloud Folder Explorer (no extra window needed)
        self.cloud_explorer = CloudExplorerPanel(
            parent_window=self.win,
            upload_engine=self.engine,
            download_engine=self.download_engine,
            submit_coro=self.submit,
            on_add_account=lambda acc: self._on_add_account_clicked(None, resync_account=acc),
            on_manage_accounts=lambda: self._on_manage_accounts_clicked(None),
            on_show_queue=self._show_upload_queue_view,
            on_switch_to_downloader=self._on_switch_to_downloader_cb,
            on_accounts_updated=self._refresh_accounts_ui,
        )
        self._right_stack.add_named(self.cloud_explorer.root, "cloud")
        self._right_stack.set_visible_child_name("queue")

        body_split.append(self._right_stack)
        page_root.append(body_split)

        # --- BOTTOM STATUS BAR ---
        status_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14)
        status_bar.add_css_class("status-bar")

        self.lbl_accounts_summary = Gtk.Label(label="Sin cuentas sincronizadas — haz clic en «Agregar cuenta»")
        self.lbl_accounts_summary.set_ellipsize(Pango.EllipsizeMode.END)
        self.lbl_accounts_summary.set_max_width_chars(46)

        st_spacer = Gtk.Box()
        st_spacer.set_hexpand(True)

        self.lbl_active = Gtk.Label(label=f"Subidas activas: 0 / {self.engine.max_concurrent}")

        conc_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        conc_box.append(Gtk.Label(label="Máx. simultáneas:"))
        self.spin_conc = Gtk.SpinButton.new_with_range(1, 16, 1)
        self.spin_conc.set_value(self.engine.max_concurrent)
        self.spin_conc.connect("value-changed", self._on_concurrency_changed)
        conc_box.append(self.spin_conc)

        self.lbl_total_speed = Gtk.Label(label="Velocidad Total: 0.00 MB/s")
        self.lbl_total_speed.add_css_class("speed-badge")

        self.btn_speed_limit = Gtk.MenuButton()
        self.btn_speed_limit.set_label(self._format_limit_label())
        self.btn_speed_limit.set_popover(self._build_speed_limit_popover())

        status_bar.append(self.lbl_accounts_summary)
        status_bar.append(st_spacer)
        status_bar.append(self.lbl_active)
        status_bar.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        status_bar.append(conc_box)
        status_bar.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        status_bar.append(self.lbl_total_speed)
        status_bar.append(self.btn_speed_limit)

        page_root.append(status_bar)
        return page_root

    # ------------------------------------------------------------------
    # Table Column Factories (exact same look as Mint Downloader)
    # ------------------------------------------------------------------

    def _add_name_column(self, title: str, expand: bool = True, min_width: int = 230) -> None:
        factory = Gtk.SignalListItemFactory()

        def _setup(_fac, list_item: Gtk.ListItem) -> None:
            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            box.set_hexpand(True)
            box.set_halign(Gtk.Align.FILL)
            box.set_valign(Gtk.Align.CENTER)
            box.set_margin_start(8)
            box.set_margin_end(6)
            box.set_margin_top(6)
            box.set_margin_bottom(6)

            ic = Gtk.Image.new_from_icon_name("text-x-generic-symbolic")
            ic.set_pixel_size(16)
            ic.set_valign(Gtk.Align.CENTER)

            lbl = Gtk.Label(xalign=0.0)
            lbl.set_ellipsize(Pango.EllipsizeMode.END)
            lbl.set_hexpand(True)
            lbl.set_halign(Gtk.Align.FILL)
            lbl.set_valign(Gtk.Align.CENTER)

            box.append(ic)
            box.append(lbl)
            list_item.set_child(box)

        def _bind(_fac, list_item: Gtk.ListItem) -> None:
            row_obj: UploadRowObject = list_item.get_item()
            box = list_item.get_child()
            if row_obj and isinstance(box, Gtk.Box):
                ic = box.get_first_child()
                lbl = ic.get_next_sibling() if ic else None
                if isinstance(ic, Gtk.Image):
                    ic.set_from_icon_name(row_obj.icon_name)
                    self._item_widgets.setdefault(row_obj.item_id, {})["filename_icon"] = ic
                if isinstance(lbl, Gtk.Label):
                    lbl.set_text(row_obj.filename)
                    lbl.set_tooltip_text(row_obj.filename)
                    self._item_widgets.setdefault(row_obj.item_id, {})["filename"] = lbl

        def _unbind(_fac, list_item: Gtk.ListItem) -> None:
            row_obj: UploadRowObject = list_item.get_item()
            if row_obj and row_obj.item_id in self._item_widgets:
                self._item_widgets[row_obj.item_id].pop("filename_icon", None)
                self._item_widgets[row_obj.item_id].pop("filename", None)

        factory.connect("setup", _setup)
        factory.connect("bind", _bind)
        factory.connect("unbind", _unbind)

        col = Gtk.ColumnViewColumn.new(title, factory)
        col.set_expand(expand)
        col.set_resizable(True)
        col.set_fixed_width(min_width)
        self.column_view.append_column(col)

    def _add_text_column(self, title: str, attr_name: str, expand: bool = False, min_width: int = 100) -> None:
        factory = Gtk.SignalListItemFactory()

        def _setup(_fac, list_item: Gtk.ListItem) -> None:
            lbl = Gtk.Label(xalign=0.0)
            lbl.set_ellipsize(Pango.EllipsizeMode.END)
            lbl.set_hexpand(True)
            lbl.set_halign(Gtk.Align.FILL)
            lbl.set_valign(Gtk.Align.CENTER)
            lbl.set_margin_start(6)
            lbl.set_margin_end(6)
            lbl.set_margin_top(6)
            lbl.set_margin_bottom(6)
            list_item.set_child(lbl)

        def _bind(_fac, list_item: Gtk.ListItem) -> None:
            row_obj: UploadRowObject = list_item.get_item()
            lbl: Gtk.Label = list_item.get_child()
            if row_obj and lbl:
                val = str(getattr(row_obj, attr_name, ""))
                lbl.set_text(val)
                lbl.set_tooltip_text(val)
                self._item_widgets.setdefault(row_obj.item_id, {})[attr_name] = lbl

        def _unbind(_fac, list_item: Gtk.ListItem) -> None:
            row_obj: UploadRowObject = list_item.get_item()
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

    def _add_progress_column(self, title: str, min_width: int = 165) -> None:
        factory = Gtk.SignalListItemFactory()

        def _setup(_fac, list_item: Gtk.ListItem) -> None:
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            box.set_overflow(Gtk.Overflow.HIDDEN)
            box.set_hexpand(True)
            box.set_halign(Gtk.Align.FILL)
            box.set_valign(Gtk.Align.CENTER)
            box.set_margin_start(8)
            box.set_margin_end(8)
            box.set_margin_top(5)
            box.set_margin_bottom(5)

            pct_lbl = Gtk.Label(xalign=0.0)
            pct_lbl.add_css_class("progress-pct-label")
            pct_lbl.set_hexpand(True)
            pct_lbl.set_halign(Gtk.Align.FILL)

            pbar = Gtk.ProgressBar()
            pbar.add_css_class("slim-progress")
            pbar.set_show_text(False)
            pbar.set_hexpand(True)
            pbar.set_halign(Gtk.Align.FILL)
            pbar.set_valign(Gtk.Align.CENTER)

            box.append(pct_lbl)
            box.append(pbar)
            list_item.set_child(box)

        def _bind(_fac, list_item: Gtk.ListItem) -> None:
            row_obj: UploadRowObject = list_item.get_item()
            box = list_item.get_child()
            if row_obj and isinstance(box, Gtk.Box):
                pct_lbl = box.get_first_child()
                pbar = pct_lbl.get_next_sibling() if pct_lbl else None
                if isinstance(pct_lbl, Gtk.Label):
                    pct_lbl.set_text(row_obj.progress_text)
                    self._item_widgets.setdefault(row_obj.item_id, {})["progress_label"] = pct_lbl
                if isinstance(pbar, Gtk.ProgressBar):
                    pbar.set_fraction(row_obj.progress_fraction)
                    self._item_widgets.setdefault(row_obj.item_id, {})["progress_bar"] = pbar

        def _unbind(_fac, list_item: Gtk.ListItem) -> None:
            row_obj: UploadRowObject = list_item.get_item()
            if row_obj and row_obj.item_id in self._item_widgets:
                self._item_widgets[row_obj.item_id].pop("progress_label", None)
                self._item_widgets[row_obj.item_id].pop("progress_bar", None)

        factory.connect("setup", _setup)
        factory.connect("bind", _bind)
        factory.connect("unbind", _unbind)

        col = Gtk.ColumnViewColumn.new(title, factory)
        col.set_expand(False)
        col.set_resizable(True)
        col.set_fixed_width(min_width)
        self.column_view.append_column(col)

    # ------------------------------------------------------------------
    # Filtering, Search, Tick & Accounts Sync
    # ------------------------------------------------------------------

    def set_search_query(self, query: str) -> None:
        self._search_query = query.strip().lower()
        if self._active_right_view == "cloud" and hasattr(self, "cloud_explorer"):
            self.cloud_explorer.set_search_query(self._search_query)
            return
        self.custom_filter.changed(Gtk.FilterChange.DIFFERENT)
        self._update_selection_footer()

    def _filter_row(self, row_obj: UploadRowObject, _user_data) -> bool:
        if self._active_account_filter_id > 0:
            if row_obj.account_id != self._active_account_filter_id:
                return False

        if self._active_status_filter == "unfinished":
            if row_obj.status_raw == "complete":
                return False
        elif self._active_status_filter == "finished":
            if row_obj.status_raw != "complete":
                return False

        if self._active_category_filter and self._active_category_filter != "all":
            if row_obj.category != self._active_category_filter:
                return False

        if self._search_query:
            haystack = (
                f"{row_obj.filename} {row_obj.file_ext} {row_obj.destination} {row_obj.status_str}"
            ).lower()
            if self._search_query not in haystack:
                return False

        return True

    def _on_account_filter_changed(self, dropdown: Gtk.DropDown, _pspec) -> None:
        idx = dropdown.get_selected()
        if idx != Gtk.INVALID_LIST_POSITION and idx < len(self._known_account_entries):
            self._active_account_filter_id = self._known_account_entries[idx][0]
        else:
            self._active_account_filter_id = 0
        self.custom_filter.changed(Gtk.FilterChange.DIFFERENT)
        self._update_selection_footer()

    def _on_select_all_toggled(self, chk: Gtk.CheckButton) -> None:
        if self._syncing_select_all:
            return
        if chk.get_active():
            self.selection_model.select_all()
        else:
            self.selection_model.unselect_all()

    def _update_selection_footer(self) -> None:
        n_total = self.filter_list_model.get_n_items()
        n_sel = self.selection_model.get_selection().get_size()
        self._syncing_select_all = True
        try:
            self.chk_select_all.set_active(n_total > 0 and n_sel == n_total)
            self.chk_select_all.set_inconsistent(0 < n_sel < n_total)
        finally:
            self._syncing_select_all = False

        noun = "elemento" if n_total == 1 else "elementos"
        if n_sel > 0:
            sel_word = "seleccionado" if n_sel == 1 else "seleccionados"
            self.lbl_selection_info.set_text(f"{n_total} {noun} • {n_sel} {sel_word}")
        else:
            self.lbl_selection_info.set_text(f"{n_total} {noun}")

    def _get_selected_item_ids(self) -> List[int]:
        bitset = self.selection_model.get_selection()
        selected: List[int] = []
        n = self.filter_list_model.get_n_items()
        for idx in range(n):
            if bitset.contains(idx):
                ro: UploadRowObject = self.filter_list_model.get_item(idx)
                if ro:
                    selected.append(ro.item_id)
        return selected

    def _populate_initial_rows(self) -> None:
        rows: List[UploadRowObject] = []
        for item in self.engine.items.values():
            ro = UploadRowObject(item)
            self._row_map[item.id] = ro
            rows.append(ro)
        if rows:
            self.list_store.splice(0, self.list_store.get_n_items(), rows)
        self._last_state_version = self.engine.state_version
        self._update_selection_footer()

    def _refresh_accounts_ui(self) -> None:
        accounts = self.engine.list_accounts()
        self._last_accounts_version = self.engine.accounts_version

        # Update dropdown entries
        entries: List[tuple[int, str]] = [(0, "Todas")] + [(a.id, a.display_title) for a in accounts]
        if entries != self._known_account_entries:
            prev_id = self._active_account_filter_id
            self._known_account_entries = entries
            self.account_filter_model.splice(
                0,
                self.account_filter_model.get_n_items(),
                [title for _, title in entries],
            )
            sel_idx = next((i for i, (aid, _) in enumerate(entries) if aid == prev_id), 0)
            self.account_filter_dropdown.set_selected(sel_idx)

        # Rebuild cloud account server-switcher buttons in left sidebar ("Nombre (Servidor)")
        child = self._sidebar_accounts_box.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self._sidebar_accounts_box.remove(child)
            child = nxt
        self._sidebar_account_widgets.clear()

        if not accounts:
            empty_lbl = Gtk.Label(label="Sin cuentas aún", xalign=0.0)
            empty_lbl.add_css_class("muted-label")
            empty_lbl.set_margin_start(10)
            empty_lbl.set_margin_top(2)
            self._sidebar_accounts_box.append(empty_lbl)
            self.lbl_accounts_summary.set_text("Sin cuentas sincronizadas — haz clic en «Agregar cuenta»")
            if self._active_right_view == "cloud":
                self._show_upload_queue_view()
        else:
            for acc in accounts:
                btn = Gtk.Button()
                btn.add_css_class("sidebar-main-btn")
                dot = "●" if acc.status == "connected" else "⚠"
                row_b = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=7)
                dot_lbl = Gtk.Label(label=dot)
                dot_lbl.add_css_class("speed-badge" if acc.status == "connected" else "warn-badge")
                txt_lbl = Gtk.Label(label=acc.display_title, xalign=0.0)
                txt_lbl.set_ellipsize(Pango.EllipsizeMode.END)
                txt_lbl.set_hexpand(True)
                row_b.append(dot_lbl)
                row_b.append(txt_lbl)
                btn.set_child(row_b)
                btn.set_tooltip_text(
                    f"Cambiar servidor a {acc.display_title} y ver sus carpetas y archivos — {acc.status_msg}"
                )

                def _on_acc_click(_b, aid=acc.id) -> None:
                    self._select_cloud_account_view(aid)

                btn.connect("clicked", _on_acc_click)
                self._sidebar_accounts_box.append(btn)
                self._sidebar_account_widgets[acc.id] = btn

            self._update_sidebar_active_styles()
            names = ", ".join(a.display_title for a in accounts[:4])
            suffix = f" (+{len(accounts) - 4})" if len(accounts) > 4 else ""
            self.lbl_accounts_summary.set_text(f"Cuentas ({len(accounts)}): {names}{suffix}")

        if self._on_accounts_changed_cb:
            self._on_accounts_changed_cb(len(accounts))

    def on_ui_tick(self) -> None:
        """Called every 250 ms from main window tick; skips all work if state_version is unchanged."""
        if self.engine.accounts_version != self._last_accounts_version:
            self._refresh_accounts_ui()

        cur_ver = self.engine.state_version
        if cur_ver != self._last_state_version:
            self._last_state_version = cur_ver
            engine_items = self.engine.items
            list_mutated = False
            needs_refilter = False

            if engine_items.keys() != self._row_map.keys():
                current_ids = set(engine_items.keys())
                known_ids = set(self._row_map.keys())
                for iid in sorted(current_ids - known_ids):
                    item = engine_items.get(iid)
                    if item:
                        ro = UploadRowObject(item)
                        self._row_map[iid] = ro
                        self.list_store.append(ro)
                        list_mutated = True

                removed_ids = known_ids - current_ids
                if removed_ids:
                    for iid in removed_ids:
                        self._row_map.pop(iid, None)
                        self._item_widgets.pop(iid, None)
                    for idx in range(self.list_store.get_n_items() - 1, -1, -1):
                        ro = self.list_store.get_item(idx)
                        if ro and ro.item_id in removed_ids:
                            self.list_store.remove(idx)
                    list_mutated = True

            for iid, ro in self._row_map.items():
                item = engine_items.get(iid)
                if not item:
                    continue
                prev_key = (ro.status_raw, ro.category, ro.destination, ro.filename, ro.status_str)
                if ro.sync_from_item(item):
                    if prev_key != (ro.status_raw, ro.category, ro.destination, ro.filename, ro.status_str):
                        needs_refilter = True
                    widgets = self._item_widgets.get(iid)
                    if widgets:
                        ic = widgets.get("filename_icon")
                        if isinstance(ic, Gtk.Image):
                            ic.set_from_icon_name(ro.icon_name)
                        for attr in ("filename", "file_ext", "destination", "size_str", "speed_str", "eta_str", "status_str"):
                            w = widgets.get(attr)
                            if isinstance(w, Gtk.Label):
                                val_s = str(getattr(ro, attr))
                                w.set_text(val_s)
                                w.set_tooltip_text(val_s)
                        pct_lbl = widgets.get("progress_label")
                        if isinstance(pct_lbl, Gtk.Label):
                            pct_lbl.set_text(ro.progress_text)
                        pbar = widgets.get("progress_bar")
                        if isinstance(pbar, Gtk.ProgressBar):
                            pbar.set_fraction(ro.progress_fraction)

            has_filter = (
                self._active_account_filter_id > 0
                or self._active_status_filter != "all"
                or self._active_category_filter != "all"
                or bool(self._search_query)
            )
            if needs_refilter and has_filter:
                self.custom_filter.changed(Gtk.FilterChange.DIFFERENT)
                list_mutated = True
            if list_mutated:
                self._update_selection_footer()

        stats_key = (self.engine.total_speed_bps, self.engine.active_count, self.engine.max_concurrent)
        if stats_key != self._last_stats:
            prev_spd, prev_act, prev_max = self._last_stats
            self._last_stats = stats_key
            if stats_key[0] != prev_spd:
                self.lbl_total_speed.set_text(f"Velocidad Total: {_fmt_speed(stats_key[0])}")
            if stats_key[1] != prev_act or stats_key[2] != prev_max:
                self.lbl_active.set_text(f"Subidas activas: {stats_key[1]} / {stats_key[2]}")

    # ------------------------------------------------------------------
    # Speed Limit & Concurrency Controls
    # ------------------------------------------------------------------

    def _format_limit_label(self, kbps: Optional[int] = None) -> str:
        val = self.engine.speed_limit_kbps if kbps is None else kbps
        if val <= 0:
            return "Límite: Sin límite"
        if val >= 1024:
            return f"Límite: {val / 1024.0:.1f} MB/s"
        return f"Límite: {val} KB/s"

    def _build_speed_limit_popover(self) -> Gtk.Popover:
        pop = Gtk.Popover()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_top(10)
        box.set_margin_bottom(10)
        box.set_margin_start(10)
        box.set_margin_end(10)

        box.append(Gtk.Label(label="Límite global de subida (KB/s, 0 = Sin límite):"))
        spin = Gtk.SpinButton.new_with_range(0, 1000000, 256)
        spin.set_value(self.engine.speed_limit_kbps)
        box.append(spin)

        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        btn_unlim = Gtk.Button(label="Sin límite")
        btn_apply = Gtk.Button(label="Aplicar")
        btn_apply.add_css_class("suggested-action")

        def _apply(kbps: int) -> None:
            spin.set_value(kbps)
            self.engine.set_speed_limit_kbps(kbps)
            self.btn_speed_limit.set_label(self._format_limit_label(kbps))
            pop.popdown()

        btn_unlim.connect("clicked", lambda *_: _apply(0))
        btn_apply.connect("clicked", lambda *_: _apply(int(spin.get_value())))
        btn_box.append(btn_unlim)
        btn_box.append(btn_apply)
        box.append(btn_box)
        pop.set_child(box)
        return pop

    def _on_concurrency_changed(self, spin: Gtk.SpinButton) -> None:
        self.engine.set_max_concurrent(int(spin.get_value()))

    # ------------------------------------------------------------------
    # Toolbar Actions (Upload, Queue Controls, Copy Link, Accounts)
    # ------------------------------------------------------------------

    def _on_start_clicked(self, _btn: Gtk.Button) -> None:
        sel = self._get_selected_item_ids()
        if sel:
            for iid in sel:
                self.submit(self.engine.start_task(iid))
        else:
            self.submit(self.engine.start_all())

    def _on_pause_clicked(self, _btn: Gtk.Button) -> None:
        sel = self._get_selected_item_ids()
        if sel:
            for iid in sel:
                self.submit(self.engine.pause_task(iid))
        else:
            self.submit(self.engine.pause_all())

    def _on_stop_clicked(self, _btn: Gtk.Button) -> None:
        sel = self._get_selected_item_ids()
        targets = sel if sel else list(self.engine.items.keys())
        for iid in targets:
            self.submit(self.engine.stop_task(iid))

    def _on_remove_clicked(self, _btn: Gtk.Button) -> None:
        sel = self._get_selected_item_ids()
        if not sel:
            return
        for iid in sel:
            self.submit(self.engine.remove_task(iid))

    def _on_clear_completed_clicked(self, _btn: Gtk.Button) -> None:
        self.submit(self.engine.clear_completed())

    def _on_copy_link_clicked(self, _btn: Gtk.Button) -> None:
        sel = self._get_selected_item_ids()
        links: List[str] = []
        for iid in sel:
            it = self.engine.items.get(iid)
            if it and it.share_url:
                links.append(it.share_url)
        if links:
            display = Gdk.Display.get_default()
            if display:
                display.get_clipboard().set("\n".join(links))
            self.lbl_selection_info.set_text(f"✓ {len(links)} enlace(s) copiado(s) al portapapeles")
        else:
            self.lbl_selection_info.set_text("Selecciona una subida completada con enlace compartido")

    def _on_open_local_dir_clicked(self, _btn: Gtk.Button) -> None:
        sel = self._get_selected_item_ids()
        if sel:
            it = self.engine.items.get(sel[0])
            if it:
                parent = os.path.dirname(it.local_path)
                if os.path.isdir(parent):
                    self._open_path_cb(parent)
                    return
        self._open_path_cb(os.path.expanduser("~"))

    def _on_row_activated(self, _cv: Gtk.ColumnView, position: int) -> None:
        ro: UploadRowObject = self.filter_list_model.get_item(position)
        if not ro:
            return
        it = self.engine.items.get(ro.item_id)
        if not it:
            return
        if it.share_url:
            try:
                Gio.AppInfo.launch_default_for_uri(it.share_url, None)
                return
            except Exception:
                pass
        parent = os.path.dirname(it.local_path)
        if os.path.isdir(parent):
            self._open_path_cb(parent)

    # ------------------------------------------------------------------
    # Upload Files / Folder Dialogs (Multi-Account Destination Picker)
    # ------------------------------------------------------------------

    def _ensure_has_accounts_or_prompt(self) -> Optional[List[CloudAccount]]:
        accounts = self.engine.list_accounts()
        if accounts:
            return accounts

        dialog, vbox = self._create_modal("No hay cuentas sincronizadas", 440, 165)
        lbl = Gtk.Label(
            label=(
                "Aún no has sincronizado ninguna cuenta en la nube.\n"
                "Agrega una cuenta de GDrive, MEGA, OneDrive, Dropbox o Mediafire para comenzar a subir archivos."
            ),
            xalign=0.0,
        )
        lbl.set_wrap(True)
        vbox.append(lbl)

        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        sp = Gtk.Box()
        sp.set_hexpand(True)
        btn_cancel = Gtk.Button(label="Cancelar")
        btn_add = Gtk.Button(label="Agregar cuenta ahora")
        btn_add.add_css_class("suggested-action")
        btn_cancel.connect("clicked", lambda *_: dialog.destroy())
        btn_add.connect("clicked", lambda *_: (dialog.destroy(), self._on_add_account_clicked(None)))
        btn_row.append(sp)
        btn_row.append(btn_cancel)
        btn_row.append(btn_add)
        vbox.append(btn_row)
        dialog.present()
        return None

    def _on_upload_files_clicked(self, _btn: Gtk.Button) -> None:
        accounts = self._ensure_has_accounts_or_prompt()
        if not accounts:
            return

        chooser = Gtk.FileChooserDialog(
            title="Seleccionar archivos para subir",
            transient_for=self.win,
            modal=True,
            action=Gtk.FileChooserAction.OPEN,
        )
        chooser.set_select_multiple(True)
        chooser.add_button("Cancelar", Gtk.ResponseType.CANCEL)
        ok_btn = chooser.add_button("Continuar", Gtk.ResponseType.ACCEPT)
        ok_btn.add_css_class("suggested-action")
        self._active_chooser = chooser

        def _on_resp(dlg: Gtk.FileChooserDialog, resp: int) -> None:
            paths: List[str] = []
            if resp == Gtk.ResponseType.ACCEPT:
                files_model = dlg.get_files()
                if files_model:
                    for i in range(files_model.get_n_items()):
                        gf = files_model.get_item(i)
                        if gf and gf.get_path():
                            paths.append(gf.get_path())
            dlg.destroy()
            self._active_chooser = None
            if paths:
                self._show_upload_destination_dialog(paths, is_folder=False, accounts=accounts)

        chooser.connect("response", _on_resp)
        chooser.present()

    def _on_upload_folder_clicked(self, _btn: Gtk.Button) -> None:
        accounts = self._ensure_has_accounts_or_prompt()
        if not accounts:
            return

        chooser = Gtk.FileChooserDialog(
            title="Seleccionar carpeta para subir",
            transient_for=self.win,
            modal=True,
            action=Gtk.FileChooserAction.SELECT_FOLDER,
        )
        chooser.add_button("Cancelar", Gtk.ResponseType.CANCEL)
        ok_btn = chooser.add_button("Continuar", Gtk.ResponseType.ACCEPT)
        ok_btn.add_css_class("suggested-action")
        self._active_chooser = chooser

        def _on_resp(dlg: Gtk.FileChooserDialog, resp: int) -> None:
            folder_path = ""
            if resp == Gtk.ResponseType.ACCEPT:
                gf = dlg.get_file()
                if gf and gf.get_path():
                    folder_path = gf.get_path()
            dlg.destroy()
            self._active_chooser = None
            if folder_path:
                self._show_upload_destination_dialog([folder_path], is_folder=True, accounts=accounts)

        chooser.connect("response", _on_resp)
        chooser.present()

    def _show_upload_destination_dialog(
        self,
        paths: List[str],
        is_folder: bool,
        accounts: List[CloudAccount],
    ) -> None:
        dialog, vbox = self._create_modal("Seleccionar cuentas destino", 480, 340, spacing=10, margin=16)

        summary = (
            f"Carpeta seleccionada: {os.path.basename(paths[0])}"
            if is_folder
            else f"{len(paths)} archivo(s) seleccionado(s): {', '.join(os.path.basename(p) for p in paths[:3])}"
            + (f" (+{len(paths) - 3})" if len(paths) > 3 else "")
        )
        lbl_sum = Gtk.Label(label=summary, xalign=0.0)
        lbl_sum.set_wrap(True)
        vbox.append(lbl_sum)

        vbox.append(Gtk.Label(label="Elige una o varias cuentas donde subir:", xalign=0.0))

        scroll = Gtk.ScrolledWindow()
        scroll.set_vexpand(True)
        acc_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)

        checks: List[tuple[int, Gtk.CheckButton]] = []
        for idx, acc in enumerate(accounts):
            status_tag = "" if acc.status == "connected" else " (⚠ re-sincronizar)"
            chk = Gtk.CheckButton(label=f"{acc.display_title}{status_tag}")
            if self._active_account_filter_id > 0:
                chk.set_active(acc.id == self._active_account_filter_id)
            else:
                chk.set_active(idx == 0)
            acc_box.append(chk)
            checks.append((acc.id, chk))

        scroll.set_child(acc_box)
        vbox.append(scroll)

        folder_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        folder_row.append(Gtk.Label(label="Carpeta remota (opcional):"))
        folder_entry = Gtk.Entry()
        folder_entry.set_placeholder_text("Ej: Respaldos/Documentos (vacío = raíz)")
        folder_entry.set_hexpand(True)
        # Pre-fill pinned default upload folder if any checked account has one set
        for aid, chk in checks:
            if chk.get_active():
                pinned = self.engine.get_default_remote_folder(aid)
                if pinned:
                    folder_entry.set_text(pinned)
                break
        folder_row.append(folder_entry)
        vbox.append(folder_row)

        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        sp = Gtk.Box()
        sp.set_hexpand(True)
        btn_cancel = Gtk.Button(label="Cancelar")
        btn_go = Gtk.Button(label="Iniciar subida")
        btn_go.add_css_class("suggested-action")

        def _confirm(_b) -> None:
            chosen_ids = [aid for aid, chk in checks if chk.get_active()]
            if not chosen_ids:
                return
            rem_folder = folder_entry.get_text().strip()
            if is_folder:
                self.submit(self.engine.add_folder(paths[0], chosen_ids, remote_folder=rem_folder, auto_start=True))
            else:
                self.submit(self.engine.add_files(paths, chosen_ids, remote_folder=rem_folder, auto_start=True))
            dialog.destroy()

        btn_cancel.connect("clicked", lambda *_: dialog.destroy())
        btn_go.connect("clicked", _confirm)
        btn_row.append(sp)
        btn_row.append(btn_cancel)
        btn_row.append(btn_go)
        vbox.append(btn_row)
        dialog.present()

    # ------------------------------------------------------------------
    # Add Account Dialog (GDrive, MEGA, OneDrive, Dropbox, Mediafire)
    # ------------------------------------------------------------------

    def _on_add_account_clicked(
        self,
        _btn: Optional[Gtk.Button],
        resync_account: Optional[CloudAccount] = None,
    ) -> None:
        title = (
            f"Re-sincronizar cuenta ({resync_account.display_title})"
            if resync_account
            else "Agregar nueva cuenta en la nube"
        )
        dialog, vbox = self._create_modal(title, 540, 420, spacing=10, margin=16)

        # Provider dropdown in the exact requested order: GDrive, MEGA, OneDrive, Dropbox, Mediafire
        prov_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        prov_row.append(Gtk.Label(label="Servicio en la nube:"))
        provider_keys = [k for k, _, _ in UPLOAD_PROVIDERS]
        provider_labels = [lbl for _, lbl, _ in UPLOAD_PROVIDERS]
        prov_model = Gtk.StringList.new(provider_labels)
        prov_combo = Gtk.DropDown(model=prov_model)
        prov_combo.set_hexpand(True)
        if resync_account and resync_account.provider in provider_keys:
            prov_combo.set_selected(provider_keys.index(resync_account.provider))
            prov_combo.set_sensitive(False)
        else:
            prov_combo.set_selected(0)
        prov_row.append(prov_combo)
        vbox.append(prov_row)

        alias_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        alias_row.append(Gtk.Label(label="Nombre / Alias (opcional):"))
        alias_entry = Gtk.Entry()
        alias_entry.set_placeholder_text("Ej: Personal, Trabajo... (se detecta el correo automáticamente)")
        alias_entry.set_hexpand(True)
        if resync_account:
            alias_entry.set_text(resync_account.label)
        alias_row.append(alias_entry)
        vbox.append(alias_row)

        chk_share_dl = Gtk.CheckButton(
            label="Guardar esta sesión también para Mi Transfer Descargas (sesión permanente compartida)"
        )
        chk_share_dl.set_active(
            resync_account.shared_with_downloader if resync_account else True
        )
        vbox.append(chk_share_dl)

        # Primary Login Window callout box
        web_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        web_card.add_css_class("toolbar-deck")
        web_card.set_margin_top(4)

        lbl_web_title = Gtk.Label(
            label="Inicio de sesión en ventana directa (recomendado)",
            xalign=0.0,
        )
        lbl_web_title.add_css_class("speed-badge")
        lbl_web_desc = Gtk.Label(
            label=(
                "Se abrirá una ventana segura para iniciar sesión directamente en tu cuenta. "
                "Las cookies y el perfil se guardan cifrados para siempre en Uploader y Downloader "
                "a menos que cambies tu contraseña."
            ),
            xalign=0.0,
        )
        lbl_web_desc.set_wrap(True)
        lbl_web_desc.add_css_class("muted-label")

        btn_open_login_win = Gtk.Button(label="Abrir ventana de inicio de sesión")
        btn_open_login_win.add_css_class("suggested-action")

        web_card.append(lbl_web_title)
        web_card.append(lbl_web_desc)
        web_card.append(btn_open_login_win)
        vbox.append(web_card)

        # Collapsible Advanced / API / Direct Credentials section
        adv_expander = Gtk.Expander(label="Opciones avanzadas (Credenciales directas / API OAuth2 / Token)")
        adv_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        adv_box.set_margin_top(6)

        form_stack = Gtk.Stack()

        # --- Advanced Page 1: Direct Email + Password (MEGA & Mediafire) ---
        cred_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        email_entry = Gtk.Entry()
        email_entry.set_placeholder_text("tu.correo@ejemplo.com")
        if resync_account and resync_account.account_email:
            email_entry.set_text(resync_account.account_email)
        cred_box.append(Gtk.Label(label="Correo electrónico:", xalign=0.0))
        cred_box.append(email_entry)

        pass_entry = Gtk.PasswordEntry()
        pass_entry.set_show_peek_icon(True)
        pass_entry.set_property("placeholder-text", "Contraseña de tu cuenta")
        cred_box.append(Gtk.Label(label="Contraseña:", xalign=0.0))
        cred_box.append(pass_entry)
        form_stack.add_named(cred_box, "credentials")

        # --- Advanced Page 2: OAuth2 API (GDrive, OneDrive, Dropbox) ---
        oauth_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)

        has_rclone = shutil.which("rclone") is not None
        chk_browser = Gtk.CheckButton(label="Autorizar vía navegador externo (OAuth2 API)")
        chk_manual = Gtk.CheckButton(label="Ingresar Refresh Token / Access Token manualmente", group=chk_browser)
        chk_rclone: Optional[Gtk.CheckButton] = None
        if has_rclone:
            chk_rclone = Gtk.CheckButton(
                label="Autorizar automáticamente vía rclone",
                group=chk_browser,
            )
            chk_rclone.set_active(True)
        else:
            chk_browser.set_active(True)

        if chk_rclone:
            oauth_box.append(chk_rclone)
        oauth_box.append(chk_browser)
        oauth_box.append(chk_manual)

        cid_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        cid_entry = Gtk.Entry()
        cid_entry.set_placeholder_text("Client ID / App key")
        cid_entry.set_hexpand(True)
        btn_import_json = Gtk.Button(label="Importar JSON...")
        btn_import_json.set_tooltip_text("Cargar credentials.json de Google Cloud Console")
        cid_row.append(cid_entry)
        cid_row.append(btn_import_json)

        csec_entry = Gtk.PasswordEntry()
        csec_entry.set_show_peek_icon(True)
        csec_entry.set_property("placeholder-text", "Client Secret (requerido en GDrive)")

        refresh_entry = Gtk.Entry()
        refresh_entry.set_placeholder_text("Refresh Token (permanente)")
        access_entry = Gtk.Entry()
        access_entry.set_placeholder_text("Access Token (opcional si ingresas Refresh Token)")

        if resync_account and resync_account.creds:
            cid_entry.set_text(str(resync_account.creds.get("client_id") or ""))
            csec_entry.set_text(str(resync_account.creds.get("client_secret") or ""))

        oauth_fields = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        oauth_fields.append(Gtk.Label(label="ID de cliente (Client ID):", xalign=0.0))
        oauth_fields.append(cid_row)
        oauth_fields.append(Gtk.Label(label="Secreto de cliente (Client Secret):", xalign=0.0))
        oauth_fields.append(csec_entry)

        manual_fields = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        manual_fields.append(Gtk.Label(label="Refresh Token:", xalign=0.0))
        manual_fields.append(refresh_entry)
        manual_fields.append(Gtk.Label(label="Access Token (opcional):", xalign=0.0))
        manual_fields.append(access_entry)

        guide_lbl = Gtk.Label(xalign=0.0)
        guide_lbl.set_wrap(True)
        guide_lbl.set_selectable(True)
        guide_lbl.add_css_class("muted-label")

        oauth_box.append(oauth_fields)
        oauth_box.append(manual_fields)
        oauth_box.append(guide_lbl)
        form_stack.add_named(oauth_box, "oauth2")

        adv_box.append(form_stack)
        btn_sync_adv = Gtk.Button(label="Sincronizar con método avanzado")
        adv_box.append(btn_sync_adv)
        adv_expander.set_child(adv_box)
        vbox.append(adv_expander)

        status_lbl = Gtk.Label(
            label="Selecciona el servicio y pulsa «Abrir ventana de inicio de sesión».",
            xalign=0.0,
        )
        status_lbl.set_wrap(True)
        vbox.append(status_lbl)

        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        sp = Gtk.Box()
        sp.set_hexpand(True)
        btn_cancel = Gtk.Button(label="Cerrar")
        btn_row.append(sp)
        btn_row.append(btn_cancel)
        vbox.append(btn_row)

        cancel_state: Dict[str, Optional[asyncio.Event]] = {"ev": None}

        def _current_provider() -> str:
            idx = prov_combo.get_selected()
            if idx != Gtk.INVALID_LIST_POSITION and idx < len(provider_keys):
                return provider_keys[idx]
            return "gdrive"

        def _update_form_visibility(*_args) -> None:
            pkey = _current_provider()
            pname = PROVIDER_DISPLAY_NAMES.get(pkey, pkey)
            btn_open_login_win.set_label(f"Abrir ventana de inicio de sesión ({pname})")
            mode = PROVIDER_AUTH_MODE.get(pkey, "oauth2")
            form_stack.set_visible_child_name(mode)
            if mode == "oauth2":
                btn_import_json.set_visible(pkey == "gdrive")
                guide_lbl.set_text(PROVIDER_OAUTH_GUIDES.get(pkey, ""))
                use_rclone = bool(chk_rclone and chk_rclone.get_active())
                use_manual = chk_manual.get_active()
                oauth_fields.set_visible(not use_rclone)
                manual_fields.set_visible(use_manual)
                guide_lbl.set_visible(not use_rclone)

        prov_combo.connect("notify::selected", _update_form_visibility)
        chk_browser.connect("toggled", _update_form_visibility)
        chk_manual.connect("toggled", _update_form_visibility)
        if chk_rclone:
            chk_rclone.connect("toggled", _update_form_visibility)
        _update_form_visibility()

        def _on_import_json(_b) -> None:
            j_dlg = Gtk.FileChooserDialog(
                title="Seleccionar credentials.json de Google OAuth",
                transient_for=dialog,
                modal=True,
                action=Gtk.FileChooserAction.OPEN,
            )
            j_dlg.add_button("Cancelar", Gtk.ResponseType.CANCEL)
            ok_b = j_dlg.add_button("Cargar", Gtk.ResponseType.ACCEPT)
            ok_b.add_css_class("suggested-action")
            flt = Gtk.FileFilter()
            flt.set_name("Archivos JSON (*.json)")
            flt.add_pattern("*.json")
            j_dlg.add_filter(flt)
            self._active_chooser = j_dlg

            def _on_jresp(d: Gtk.FileChooserDialog, resp: int) -> None:
                if resp == Gtk.ResponseType.ACCEPT:
                    gf = d.get_file()
                    if gf and gf.get_path():
                        try:
                            cid, csec = parse_google_credentials_json(gf.get_path())
                            cid_entry.set_text(cid)
                            csec_entry.set_text(csec)
                            status_lbl.set_text("✓ Credenciales cargadas desde JSON.")
                        except Exception as exc:
                            status_lbl.set_text(f"⚠ {exc}")
                d.destroy()
                self._active_chooser = None

            j_dlg.connect("response", _on_jresp)
            j_dlg.present()

        btn_import_json.connect("clicked", _on_import_json)

        def _on_cancel(_b) -> None:
            if cancel_state["ev"]:
                try:
                    self.submit(self._set_async_event(cancel_state["ev"]))
                except Exception:
                    pass
            dialog.destroy()

        btn_cancel.connect("clicked", _on_cancel)

        def _set_busy(busy: bool) -> None:
            btn_open_login_win.set_sensitive(not busy)
            btn_sync_adv.set_sensitive(not busy)
            if not resync_account:
                prov_combo.set_sensitive(not busy)

        def _do_web_login(_b) -> None:
            pkey = _current_provider()
            pname = PROVIDER_DISPLAY_NAMES.get(pkey, pkey)
            alias = alias_entry.get_text().strip()
            share_dl = chk_share_dl.get_active()
            aid = resync_account.id if resync_account else None
            _set_busy(True)
            status_lbl.set_text(
                f"⏳ Ventana de inicio de sesión abierta para {pname}. Inicia sesión y pulsa «Guardar sesión»."
            )

            fut = self.submit(
                self.engine.sync_web_login_account(
                    provider=pkey,
                    alias=alias,
                    share_with_downloader=share_dl,
                    account_id=aid,
                )
            )

            def _on_done() -> bool:
                _set_busy(False)
                try:
                    acc: CloudAccount = fut.result()
                    self._refresh_accounts_ui()
                    status_lbl.set_text(f"✓ Cuenta sincronizada: {acc.display_title}")
                    dialog.destroy()
                except Exception as exc:
                    status_lbl.set_text(f"⚠ {exc}")
                return False

            fut.add_done_callback(lambda _f: GLib.idle_add(_on_done))

        def _do_sync_adv(_b) -> None:
            pkey = _current_provider()
            alias = alias_entry.get_text().strip()
            aid = resync_account.id if resync_account else None
            _set_busy(True)

            async def _task():
                if PROVIDER_AUTH_MODE.get(pkey) == "credentials":
                    return await self.engine.sync_credentials_account(
                        provider=pkey,
                        email=email_entry.get_text(),
                        password=pass_entry.get_text(),
                        alias=alias,
                        account_id=aid,
                    )
                if chk_rclone and chk_rclone.get_active():
                    return await self.engine.sync_rclone_account(
                        provider=pkey,
                        alias=alias,
                        account_id=aid,
                    )
                if chk_manual.get_active():
                    return await self.engine.sync_manual_token_account(
                        provider=pkey,
                        access_token=access_entry.get_text(),
                        refresh_token=refresh_entry.get_text(),
                        client_id=cid_entry.get_text(),
                        client_secret=csec_entry.get_text(),
                        alias=alias,
                        account_id=aid,
                    )
                ev = asyncio.Event()
                cancel_state["ev"] = ev
                return await self.engine.sync_oauth_browser_account(
                    provider=pkey,
                    client_id=cid_entry.get_text(),
                    client_secret=csec_entry.get_text(),
                    alias=alias,
                    account_id=aid,
                    cancel_event=ev,
                )

            if PROVIDER_AUTH_MODE.get(pkey) == "credentials":
                status_lbl.set_text(f"⏳ Verificando credenciales en {PROVIDER_DISPLAY_NAMES.get(pkey, pkey)}...")
            elif chk_manual.get_active():
                status_lbl.set_text(f"⏳ Validando token en {PROVIDER_DISPLAY_NAMES.get(pkey, pkey)}...")
            else:
                status_lbl.set_text("⏳ Abriendo navegador para autorizar acceso OAuth2...")

            fut = self.submit(_task())

            def _on_done() -> bool:
                _set_busy(False)
                try:
                    acc: CloudAccount = fut.result()
                    self._refresh_accounts_ui()
                    status_lbl.set_text(f"✓ Cuenta sincronizada: {acc.display_title}")
                    dialog.destroy()
                except Exception as exc:
                    status_lbl.set_text(f"⚠ Error: {exc}")
                return False

            fut.add_done_callback(lambda _f: GLib.idle_add(_on_done))

        btn_open_login_win.connect("clicked", _do_web_login)
        btn_sync_adv.connect("clicked", _do_sync_adv)
        dialog.present()

    @staticmethod
    async def _set_async_event(ev: asyncio.Event) -> None:
        ev.set()

    # ------------------------------------------------------------------
    # Manage Connected Accounts Dialog
    # ------------------------------------------------------------------

    def _on_manage_accounts_clicked(self, _btn: Optional[Gtk.Button]) -> None:
        dialog, vbox = self._create_modal("Cuentas sincronizadas — Mi Transfer", 650, 400, spacing=10, margin=16)

        lbl_info = Gtk.Label(
            label=(
                "Las sesiones se guardan cifradas permanentemente y se comparten entre Subidas y Descargas. "
                "Solo deberás volver a iniciar sesión si cambias tu contraseña o revocas el acceso."
            ),
            xalign=0.0,
        )
        lbl_info.set_wrap(True)
        lbl_info.add_css_class("muted-label")
        vbox.append(lbl_info)

        scroll = Gtk.ScrolledWindow()
        scroll.set_vexpand(True)
        list_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        scroll.set_child(list_box)
        vbox.append(scroll)

        status_lbl = Gtk.Label(label="", xalign=0.0)
        vbox.append(status_lbl)

        def _open_rename_modal(a_obj: CloudAccount) -> None:
            rdlg, rbox = self._create_modal(f"Renombrar {a_obj.display_title}", 380, 145, spacing=10, margin=16)
            rbox.append(Gtk.Label(label=f"Nombre visible para esta cuenta ({a_obj.provider_name}):", xalign=0.0))
            rentry = Gtk.Entry()
            rentry.set_text(a_obj.label)
            rentry.set_placeholder_text("Ej: Personal, Trabajo, Archivos...")
            rbox.append(rentry)
            rrow = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            rsp = Gtk.Box()
            rsp.set_hexpand(True)
            rcan = Gtk.Button(label="Cancelar")
            rok = Gtk.Button(label="Guardar")
            rok.add_css_class("suggested-action")
            rrow.append(rsp)
            rrow.append(rcan)
            rrow.append(rok)
            rbox.append(rrow)

            def _save_name(*_a) -> None:
                nl = rentry.get_text().strip()
                if nl:
                    self.engine.rename_account(a_obj.id, nl)
                    self._refresh_accounts_ui()
                    _populate_list()
                rdlg.destroy()

            rcan.connect("clicked", lambda *_: rdlg.destroy())
            rok.connect("clicked", _save_name)
            rentry.connect("activate", _save_name)
            rdlg.present()

        def _populate_list() -> None:
            child = list_box.get_first_child()
            while child:
                nxt = child.get_next_sibling()
                list_box.remove(child)
                child = nxt

            accounts = self.engine.list_accounts()
            if not accounts:
                empty = Gtk.Label(label="No tienes cuentas sincronizadas todavía.", xalign=0.0)
                empty.add_css_class("muted-label")
                list_box.append(empty)
                return

            for acc in accounts:
                row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
                info_col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
                info_col.set_hexpand(True)

                title_lbl = Gtk.Label(label=acc.display_title, xalign=0.0)
                title_lbl.set_ellipsize(Pango.EllipsizeMode.END)
                scope_tag = " · Subidas + Descargas" if acc.shared_with_downloader else " · Solo Subidas"
                st_txt = (
                    f"● {acc.status_msg}{scope_tag}"
                    if acc.status == "connected"
                    else f"⚠ {acc.status_msg}{scope_tag}"
                )
                sub_lbl = Gtk.Label(label=st_txt, xalign=0.0)
                sub_lbl.set_ellipsize(Pango.EllipsizeMode.END)
                sub_lbl.add_css_class("speed-badge" if acc.status == "connected" else "warn-badge")

                info_col.append(title_lbl)
                info_col.append(sub_lbl)
                row.append(info_col)

                btn_rename = Gtk.Button(label="Renombrar")
                btn_verify = Gtk.Button(label="Verificar")
                btn_resync = Gtk.Button(label="Abrir sesión")
                btn_resync.set_tooltip_text("Re-abrir ventana de inicio de sesión o actualizar credenciales")
                btn_del = Gtk.Button(label="Desconectar")
                btn_del.add_css_class("destructive-action")

                def _do_verify(_b, aid=acc.id, title=acc.display_title) -> None:
                    status_lbl.set_text(f"⏳ Verificando sesión de {title}...")
                    fut = self.submit(self.engine.verify_account(aid))

                    def _done() -> bool:
                        try:
                            fut.result()
                            status_lbl.set_text(f"✓ Sesión activa y verificada: {title}")
                        except Exception as exc:
                            status_lbl.set_text(f"⚠ {title}: {exc}")
                        self._refresh_accounts_ui()
                        _populate_list()
                        return False

                    fut.add_done_callback(lambda _f: GLib.idle_add(_done))

                def _do_resync(_b, a_obj=acc) -> None:
                    dialog.destroy()
                    self._on_add_account_clicked(None, resync_account=a_obj)

                def _do_del(_b, aid=acc.id) -> None:
                    self.engine.delete_account(aid)
                    self._refresh_accounts_ui()
                    _populate_list()

                btn_rename.connect("clicked", lambda _b, a_obj=acc: _open_rename_modal(a_obj))
                btn_verify.connect("clicked", _do_verify)
                btn_resync.connect("clicked", _do_resync)
                btn_del.connect("clicked", _do_del)

                row.append(btn_rename)
                row.append(btn_verify)
                row.append(btn_resync)
                row.append(btn_del)
                list_box.append(row)

        _populate_list()

        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        btn_new = Gtk.Button(label="+ Agregar cuenta")
        btn_new.add_css_class("suggested-action")
        sp = Gtk.Box()
        sp.set_hexpand(True)
        btn_close = Gtk.Button(label="Cerrar")

        btn_new.connect("clicked", lambda *_: (dialog.destroy(), self._on_add_account_clicked(None)))
        btn_close.connect("clicked", lambda *_: dialog.destroy())

        btn_row.append(btn_new)
        btn_row.append(sp)
        btn_row.append(btn_close)
        vbox.append(btn_row)
        dialog.present()
