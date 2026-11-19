"""
GTK4 Native Cloud Folder & File Explorer Workspace for Mi Transfer.
Embedded directly inside the Subidas workspace so clicking any synchronized account
in the left sidebar (e.g. "Personal (GDrive)", "Trabajo (MEGA)") immediately switches
servers and displays that account's cloud folders & files right inside the window
without opening any extra window.
"""

from __future__ import annotations

import os
from typing import Callable, List, Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Gio", "2.0")
from gi.repository import Gdk, Gio, GLib, GObject, Gtk, Pango

from core.account_vault import CloudAccount
from core.engine import DownloadEngine
from core.upload_engine import UploadEngine
from core.uploaders import CloudEntry


class CloudEntryRow(GObject.Object):
    """GObject wrapper representing a folder or file inside the native cloud explorer."""

    __gtype_name__ = "CloudEntryRow"

    def __init__(self, entry: CloudEntry) -> None:
        super().__init__()
        self.entry: CloudEntry = entry
        self.entry_id: str = entry.id
        self.name: str = entry.name
        self.is_folder: bool = entry.is_folder
        self.path: str = entry.path or entry.name
        self.modified: str = entry.modified or "—"
        self.size_str: str = "—" if entry.is_folder else self._fmt_size(entry.size)
        if entry.is_folder:
            self.type_str = "Carpeta"
            self.icon_name = "folder-symbolic"
        else:
            _, ext = os.path.splitext(entry.name)
            self.type_str = ext.lower() if ext else "Archivo"
            self.icon_name = self._icon_for_ext(self.type_str)

    @staticmethod
    def _fmt_size(num_bytes: int) -> str:
        if num_bytes <= 0:
            return "—"
        val = float(num_bytes)
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if val < 1024.0 or unit == "TB":
                return f"{val:.1f} {unit}" if unit != "B" else f"{int(val)} B"
            val /= 1024.0
        return f"{val:.1f} TB"

    @staticmethod
    def _icon_for_ext(ext: str) -> str:
        e = (ext or "").lower()
        if e in {".mp4", ".mkv", ".webm", ".avi", ".mov", ".m4v", ".ts"}:
            return "video-x-generic-symbolic"
        if e in {".mp3", ".flac", ".wav", ".aac", ".ogg", ".opus", ".m4a"}:
            return "audio-x-generic-symbolic"
        if e in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg", ".bmp"}:
            return "image-x-generic-symbolic"
        if e in {".zip", ".rar", ".7z", ".tar", ".gz", ".xz", ".iso"}:
            return "package-x-generic-symbolic"
        if e in {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".txt", ".md"}:
            return "x-office-document-symbolic"
        if e in {".apk", ".xapk"}:
            return "phone-symbolic"
        if e in {".exe", ".msi", ".deb", ".rpm", ".appimage", ".sh"}:
            return "application-x-executable-symbolic"
        return "text-x-generic-symbolic"


class CloudExplorerPanel:
    """
    Native Cloud Folder Explorer embedded in Mi Transfer's Subidas panel.
    Switching accounts in the left sidebar calls `select_account(account_id)`
    to update the table immediately in-place.
    """

    def __init__(
        self,
        parent_window: Gtk.Window,
        upload_engine: UploadEngine,
        download_engine: DownloadEngine,
        submit_coro: Callable,
        on_add_account: Callable[[Optional[CloudAccount]], None],
        on_manage_accounts: Callable[[], None],
        on_show_queue: Callable[[], None],
        on_switch_to_downloader: Callable[[], None],
        on_accounts_updated: Optional[Callable[[], None]] = None,
    ) -> None:
        self.parent_window = parent_window
        self.upload_engine = upload_engine
        self.download_engine = download_engine
        self.submit = submit_coro
        self.on_add_account = on_add_account
        self.on_manage_accounts = on_manage_accounts
        self.on_show_queue = on_show_queue
        self.on_switch_to_downloader = on_switch_to_downloader
        self.on_accounts_updated = on_accounts_updated

        self._active_account_id: int = 0
        self._current_folder_id: str = ""
        self._current_folder_path: str = ""
        self._nav_stack: List[tuple[str, str]] = []
        self._search_query: str = ""
        self._active_chooser: Optional[Gtk.Widget] = None
        self._load_seq: int = 0

        self.root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.root.set_vexpand(True)
        self.root.set_hexpand(True)

        self._build_ui()

    def _make_toolbar_button(
        self, icon_name: str, label_text: str, tooltip: str, callback
    ) -> Gtk.Button:
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
        lbl = Gtk.Label(label=label_text)
        lbl.set_justify(Gtk.Justification.CENTER)
        lbl.set_wrap(True)
        lbl.set_wrap_mode(Pango.WrapMode.WORD)
        lbl.set_lines(2)
        lbl.set_max_width_chars(14)

        box.append(img)
        box.append(lbl)
        btn.set_child(box)
        btn.connect("clicked", callback)
        return btn

    def _build_ui(self) -> None:
        # 2-Row Equal-Sized Control Deck matching Mi Transfer's toolbar style
        deck = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        deck.add_css_class("toolbar-deck")

        btn_size_group = Gtk.SizeGroup(mode=Gtk.SizeGroupMode.BOTH)
        grid = Gtk.Grid()
        grid.set_row_spacing(6)
        grid.set_column_spacing(6)
        grid.set_halign(Gtk.Align.START)

        btn_up_files = self._make_toolbar_button(
            "document-send-symbolic",
            "Subir archivos aquí",
            "Seleccionar archivos locales y subirlos a esta carpeta de la nube",
            self._on_upload_files_here,
        )
        btn_up_files.add_css_class("pro-btn-primary")
        btn_up_folder = self._make_toolbar_button(
            "folder-new-symbolic",
            "Subir carpeta aquí",
            "Seleccionar una carpeta local y subirla dentro de esta ruta",
            self._on_upload_folder_here,
        )
        btn_dl_sel = self._make_toolbar_button(
            "folder-download-symbolic",
            "Descargar",
            "Enviar los archivos seleccionados a Mi Transfer — Descargas",
            self._on_download_selected,
        )
        btn_new_dir = self._make_toolbar_button(
            "list-add-symbolic",
            "Nueva carpeta",
            "Crear una carpeta nueva en esta ubicación de la nube",
            self._on_create_folder,
        )
        btn_refresh = self._make_toolbar_button(
            "view-refresh-symbolic",
            "Actualizar",
            "Recargar carpetas y archivos de esta cuenta",
            lambda *_: self._load_current_folder(force_refresh=True),
        )
        btn_open_web = self._make_toolbar_button(
            "web-browser-symbolic",
            "Abrir en web",
            "Abrir la interfaz web oficial de esta cuenta con tu sesión guardada",
            self._on_open_web_clicked,
        )
        btn_open_web.add_css_class("pro-btn-accent")

        btn_back = self._make_toolbar_button(
            "go-previous-symbolic",
            "Atrás",
            "Volver a la carpeta anterior",
            self._on_go_back,
        )
        btn_up = self._make_toolbar_button(
            "go-up-symbolic",
            "Subir nivel",
            "Ir a la carpeta padre",
            self._on_go_up,
        )
        btn_root = self._make_toolbar_button(
            "go-home-symbolic",
            "Raíz",
            "Ir a la carpeta principal de la cuenta",
            self._on_go_root,
        )
        btn_pin_dest = self._make_toolbar_button(
            "emblem-default-symbolic",
            "Fijar destino",
            "Guardar esta carpeta como destino predeterminado de subida",
            self._on_pin_destination,
        )
        btn_copy_link = self._make_toolbar_button(
            "edit-copy-symbolic",
            "Copiar enlace",
            "Copiar enlace del elemento seleccionado",
            self._on_copy_link,
        )
        btn_queue = self._make_toolbar_button(
            "view-list-symbolic",
            "Cola subidas",
            "Volver a la lista de progreso de subidas",
            lambda *_: self.on_show_queue(),
        )

        row0 = [btn_up_files, btn_up_folder, btn_dl_sel, btn_new_dir, btn_refresh, btn_open_web]
        row1 = [btn_back, btn_up, btn_root, btn_pin_dest, btn_copy_link, btn_queue]
        for b in row0 + row1:
            btn_size_group.add_widget(b)

        def _vsep() -> Gtk.Separator:
            s = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
            s.set_margin_start(3)
            s.set_margin_end(3)
            return s

        grid.attach(btn_up_files, 0, 0, 1, 1)
        grid.attach(btn_up_folder, 1, 0, 1, 1)
        grid.attach(_vsep(), 2, 0, 1, 2)
        grid.attach(btn_dl_sel, 3, 0, 1, 1)
        grid.attach(btn_new_dir, 4, 0, 1, 1)
        grid.attach(_vsep(), 5, 0, 1, 2)
        grid.attach(btn_refresh, 6, 0, 1, 1)
        grid.attach(btn_open_web, 7, 0, 1, 1)

        grid.attach(btn_back, 0, 1, 1, 1)
        grid.attach(btn_up, 1, 1, 1, 1)
        grid.attach(btn_root, 3, 1, 1, 1)
        grid.attach(btn_pin_dest, 4, 1, 1, 1)
        grid.attach(btn_copy_link, 6, 1, 1, 1)
        grid.attach(btn_queue, 7, 1, 1, 1)

        deck.append(grid)

        # Breadcrumb & pinned destination strip
        path_strip = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        path_strip.set_margin_top(2)

        lbl_loc = Gtk.Label(label="Ubicación:")
        lbl_loc.add_css_class("muted-label")
        self._breadcrumb_lbl = Gtk.Label(label="Selecciona una cuenta en la izquierda", xalign=0.0)
        self._breadcrumb_lbl.set_hexpand(True)
        self._breadcrumb_lbl.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        self._breadcrumb_lbl.add_css_class("speed-badge")

        self._pinned_lbl = Gtk.Label(label="Destino subida: /Raíz")
        self._pinned_lbl.add_css_class("bridge-badge")

        path_strip.append(lbl_loc)
        path_strip.append(self._breadcrumb_lbl)
        path_strip.append(self._pinned_lbl)
        deck.append(path_strip)

        self.root.append(deck)

        # --- MAIN TABLE CARD (Native Folder & File List or Loading/Empty Card) ---
        table_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        table_card.add_css_class("table-card")
        table_card.set_vexpand(True)
        table_card.set_hexpand(True)

        self._content_stack = Gtk.Stack()
        self._content_stack.set_vexpand(True)
        self._content_stack.set_hexpand(True)

        # Page A: Native Cloud ColumnView
        self._store = Gio.ListStore.new(CloudEntryRow)
        self._custom_filter = Gtk.CustomFilter.new(self._filter_row)
        self._filter_model = Gtk.FilterListModel.new(self._store, self._custom_filter)
        self._selection = Gtk.MultiSelection.new(self._filter_model)

        self._col_view = Gtk.ColumnView(model=self._selection)
        self._col_view.set_show_column_separators(False)
        self._col_view.set_show_row_separators(True)
        self._col_view.set_enable_rubberband(True)
        self._col_view.set_vexpand(True)
        self._col_view.set_hexpand(True)
        self._col_view.connect("activate", self._on_row_activated)

        self._add_columns()

        table_scroll = Gtk.ScrolledWindow()
        table_scroll.add_css_class("table-area")
        table_scroll.set_vexpand(True)
        table_scroll.set_hexpand(True)
        table_scroll.set_child(self._col_view)
        self._content_stack.add_named(table_scroll, "table")

        # Page B: Loading / Empty / Re-auth Notice Card
        notice_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        notice_box.set_valign(Gtk.Align.CENTER)
        notice_box.set_halign(Gtk.Align.CENTER)
        notice_box.set_margin_start(32)
        notice_box.set_margin_end(32)

        self._spinner = Gtk.Spinner()
        self._spinner.set_size_request(32, 32)
        self._notice_icon = Gtk.Image.new_from_icon_name("folder-remote-symbolic")
        self._notice_icon.set_pixel_size(44)
        self._notice_title = Gtk.Label(label="Explorador de Nube — Mi Transfer")
        self._notice_title.add_css_class("app-title-badge")
        self._notice_desc = Gtk.Label(
            label="Selecciona una cuenta en «Cuentas en la nube» para ver sus carpetas y archivos aquí."
        )
        self._notice_desc.set_wrap(True)
        self._notice_desc.set_max_width_chars(62)
        self._notice_desc.set_justify(Gtk.Justification.CENTER)
        self._notice_desc.add_css_class("muted-label")

        notice_actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        notice_actions.set_halign(Gtk.Align.CENTER)

        self._btn_notice_retry = Gtk.Button(label="Reintentar carga")
        self._btn_notice_retry.add_css_class("pro-btn-primary")
        self._btn_notice_retry.connect("clicked", lambda *_: self._load_current_folder(force_refresh=True))

        self._btn_notice_web = Gtk.Button(label="Abrir en web")
        self._btn_notice_web.add_css_class("pro-btn-accent")
        self._btn_notice_web.connect("clicked", self._on_open_web_clicked)

        self._btn_notice_add = Gtk.Button(label="Re-abrir sesión / OAuth2")
        self._btn_notice_add.connect("clicked", self._on_notice_add_clicked)

        notice_actions.append(self._btn_notice_retry)
        notice_actions.append(self._btn_notice_web)
        notice_actions.append(self._btn_notice_add)

        notice_box.append(self._spinner)
        notice_box.append(self._notice_icon)
        notice_box.append(self._notice_title)
        notice_box.append(self._notice_desc)
        notice_box.append(notice_actions)
        self._content_stack.add_named(notice_box, "notice")

        table_card.append(self._content_stack)
        table_card.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        # Footer selection & status strip
        footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        footer.add_css_class("table-footer")
        self._chk_select_all = Gtk.CheckButton(label="Seleccionar todo")
        self._chk_select_all.connect("toggled", self._on_select_all_toggled)
        self._status_lbl = Gtk.Label(
            label="Doble clic en carpeta para abrir · Doble clic en archivo para descargar con Mi Transfer",
            xalign=0.0,
        )
        self._status_lbl.set_hexpand(True)
        self._status_lbl.set_ellipsize(Pango.EllipsizeMode.END)
        self._status_lbl.add_css_class("muted-label")
        self._count_lbl = Gtk.Label(label="0 elementos")
        self._count_lbl.add_css_class("muted-label")

        footer.append(self._chk_select_all)
        footer.append(self._status_lbl)
        footer.append(self._count_lbl)
        table_card.append(footer)

        self.root.append(table_card)

        self._show_notice(
            "Explorador de carpetas en la nube",
            "Haz clic en cualquier cuenta de «Cuentas en la nube» (izquierda) para ver tus carpetas y archivos aquí mismo.",
            loading=False,
            show_retry=False,
            show_web=False,
            show_add=True,
        )

    def _add_columns(self) -> None:
        f_name = Gtk.SignalListItemFactory()

        def _s_name(_f, li):
            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            box.set_margin_top(4)
            box.set_margin_bottom(4)
            ic = Gtk.Image()
            ic.set_pixel_size(16)
            lb = Gtk.Label(xalign=0.0)
            lb.set_ellipsize(Pango.EllipsizeMode.END)
            lb.set_hexpand(True)
            box.append(ic)
            box.append(lb)
            li.set_child(box)

        def _b_name(_f, li):
            row: CloudEntryRow = li.get_item()
            box = li.get_child()
            ic = box.get_first_child()
            lb = ic.get_next_sibling()
            ic.set_from_icon_name(row.icon_name)
            lb.set_text(row.name)

        f_name.connect("setup", _s_name)
        f_name.connect("bind", _b_name)
        c_name = Gtk.ColumnViewColumn(title="Nombre", factory=f_name)
        c_name.set_expand(True)
        c_name.set_resizable(True)
        self._col_view.append_column(c_name)

        def _text_col(title: str, attr: str, width: int, xalign: float = 0.0) -> None:
            fac = Gtk.SignalListItemFactory()

            def _s(_f, li):
                lb = Gtk.Label(xalign=xalign)
                lb.set_ellipsize(Pango.EllipsizeMode.END)
                lb.set_margin_start(6)
                lb.set_margin_end(6)
                li.set_child(lb)

            def _b(_f, li):
                row: CloudEntryRow = li.get_item()
                li.get_child().set_text(str(getattr(row, attr, "")))

            fac.connect("setup", _s)
            fac.connect("bind", _b)
            col = Gtk.ColumnViewColumn(title=title, factory=fac)
            col.set_fixed_width(width)
            col.set_resizable(True)
            self._col_view.append_column(col)

        _text_col("Tipo", "type_str", 105, 0.0)
        _text_col("Tamaño", "size_str", 105, 1.0)
        _text_col("Modificado", "modified", 155, 0.0)
        _text_col("Ruta", "path", 210, 0.0)

    def set_search_query(self, query: str) -> None:
        self._search_query = (query or "").strip().lower()
        self._custom_filter.changed(Gtk.FilterChange.DIFFERENT)
        self._update_count_label()

    def _filter_row(self, item: CloudEntryRow) -> bool:
        if not self._search_query:
            return True
        hay = f"{item.name} {item.type_str} {item.path}".lower()
        return self._search_query in hay

    def _show_notice(
        self,
        title: str,
        desc: str,
        loading: bool = False,
        show_retry: bool = True,
        show_web: bool = True,
        show_add: bool = True,
    ) -> None:
        self._spinner.set_visible(loading)
        if loading:
            self._spinner.start()
        else:
            self._spinner.stop()
        self._notice_icon.set_visible(not loading)
        self._notice_title.set_text(title)
        self._notice_desc.set_text(desc)
        self._btn_notice_retry.set_visible(show_retry and not loading)
        self._btn_notice_web.set_visible(show_web and not loading)
        self._btn_notice_add.set_visible(show_add and not loading)
        self._content_stack.set_visible_child_name("notice")

    @property
    def active_account_id(self) -> int:
        return self._active_account_id

    def _get_active_account(self) -> Optional[CloudAccount]:
        if self._active_account_id <= 0:
            return None
        return self.upload_engine.vault.get_account(self._active_account_id)

    def select_account(self, account_id: int) -> None:
        """Switch the active cloud server and immediately load its folders & files in-place."""
        self._active_account_id = account_id
        self._current_folder_id = ""
        self._current_folder_path = ""
        self._nav_stack.clear()
        self._update_breadcrumb()
        self._update_pinned_label()
        self._load_current_folder()

    def _update_breadcrumb(self) -> None:
        acc = self._get_active_account()
        if not acc:
            self._breadcrumb_lbl.set_text("Sin cuenta seleccionada")
            return
        rel = f"/{self._current_folder_path}" if self._current_folder_path else "/Raíz"
        self._breadcrumb_lbl.set_text(f"{acc.display_title}  ›  {rel}")

    def _update_pinned_label(self) -> None:
        acc = self._get_active_account()
        if not acc:
            self._pinned_lbl.set_text("Destino subida: —")
            return
        pinned = self.upload_engine.get_default_remote_folder(acc.id)
        self._pinned_lbl.set_text(f"Destino subida: /{pinned or 'Raíz'}")

    def _update_count_label(self) -> None:
        n = self._filter_model.get_n_items()
        self._count_lbl.set_text(f"{n} elemento{'s' if n != 1 else ''}")

    def _render_entries(self, acc: CloudAccount, entries: List[CloudEntry], from_cache: bool = False) -> None:
        rows = [CloudEntryRow(e) for e in entries]
        self._store.splice(0, self._store.get_n_items(), rows)
        self._update_count_label()
        if entries:
            self._content_stack.set_visible_child_name("table")
            n_dirs = sum(1 for e in entries if e.is_folder)
            n_files = len(entries) - n_dirs
            tag = " (caché instantánea)" if from_cache else ""
            self._status_lbl.set_text(
                f"✓ {acc.display_title}: {n_dirs} carpeta(s) y {n_files} archivo(s){tag}. "
                "Doble clic en carpeta para entrar o en archivo para descargar."
            )
        else:
            self._show_notice(
                f"Sin elementos visibles en {acc.display_title}",
                (
                    "Esta carpeta está vacía o el servidor aún está cargando su contenido.\n"
                    "Puedes reintentar, subir archivos aquí o abrir la interfaz web."
                ),
                loading=False,
                show_retry=True,
                show_web=True,
                show_add=False,
            )
            self._status_lbl.set_text(f"Carpeta sin elementos visibles en {acc.display_title}.")

    def _load_current_folder(self, force_refresh: bool = False) -> None:
        acc = self._get_active_account()
        if not acc:
            return
        self._load_seq += 1
        seq = self._load_seq
        self._update_breadcrumb()
        self._update_pinned_label()

        # Instant cache path (< 2 ms): show immediately; revalidate silently only if stale (> 25 s)
        cached = (
            None
            if force_refresh
            else self.upload_engine.get_cached_folder(
                acc.id, self._current_folder_id, self._current_folder_path
            )
        )
        if cached is not None:
            cached_entries, age = cached
            self._render_entries(acc, cached_entries, from_cache=True)
            if age < 25.0:
                return
        else:
            self._status_lbl.set_text(f"⏳ Cargando carpetas de {acc.display_title}...")
            self._show_notice(
                f"Cargando {acc.display_title}...",
                "Leyendo tus carpetas y archivos en la nube en segundo plano (sin abrir ventanas).",
                loading=True,
            )

        fut = self.submit(
            self.upload_engine.list_remote_folder(
                account_id=acc.id,
                folder_id=self._current_folder_id,
                folder_path=self._current_folder_path,
                force_refresh=True,
            )
        )

        def _done() -> bool:
            if seq != self._load_seq:
                return False
            refreshed_acc = self._get_active_account() or acc
            self._update_breadcrumb()
            if self.on_accounts_updated:
                self.on_accounts_updated()
            try:
                entries: List[CloudEntry] = fut.result()
                self._render_entries(refreshed_acc, entries, from_cache=False)
            except Exception as exc:
                if cached is not None:
                    # Keep cached table visible on transient background refresh error
                    return False
                self._store.remove_all()
                self._update_count_label()
                msg = str(exc)
                self._show_notice(
                    f"{refreshed_acc.display_title}",
                    f"{msg}",
                    loading=False,
                    show_retry=True,
                    show_web=True,
                    show_add=True,
                )
                self._status_lbl.set_text(f"⚠ {msg}")
            return False

        fut.add_done_callback(lambda _f: GLib.idle_add(_done))

    def _on_row_activated(self, _cv: Gtk.ColumnView, pos: int) -> None:
        item = self._filter_model.get_item(pos)
        if not isinstance(item, CloudEntryRow):
            return
        if item.is_folder:
            self._nav_stack.append((self._current_folder_id, self._current_folder_path))
            self._current_folder_id = item.entry_id
            self._current_folder_path = item.path
            self._load_current_folder()
        else:
            self._download_entries([item.entry])

    def _on_go_back(self, _btn) -> None:
        if not self._nav_stack:
            return
        fid, fpath = self._nav_stack.pop()
        self._current_folder_id = fid
        self._current_folder_path = fpath
        self._load_current_folder()

    def _on_go_up(self, _btn) -> None:
        if self._nav_stack:
            self._on_go_back(None)
        elif self._current_folder_id or self._current_folder_path:
            self._on_go_root(None)

    def _on_go_root(self, _btn) -> None:
        self._nav_stack.clear()
        self._current_folder_id = ""
        self._current_folder_path = ""
        self._load_current_folder()

    def _selected_rows(self) -> List[CloudEntryRow]:
        out: List[CloudEntryRow] = []
        n = self._filter_model.get_n_items()
        for i in range(n):
            if self._selection.is_selected(i):
                it = self._filter_model.get_item(i)
                if isinstance(it, CloudEntryRow):
                    out.append(it)
        return out

    def _on_select_all_toggled(self, chk: Gtk.CheckButton) -> None:
        if chk.get_active():
            self._selection.select_all()
        else:
            self._selection.unselect_all()

    def _on_upload_files_here(self, _btn) -> None:
        acc = self._get_active_account()
        if not acc:
            self.on_add_account(None)
            return
        chooser = Gtk.FileChooserDialog(
            title=f"Subir archivos a {acc.display_title} (/{self._current_folder_path or 'Raíz'})",
            transient_for=self.parent_window,
            modal=True,
            action=Gtk.FileChooserAction.OPEN,
        )
        chooser.set_select_multiple(True)
        chooser.add_button("Cancelar", Gtk.ResponseType.CANCEL)
        ok = chooser.add_button("Subir aquí", Gtk.ResponseType.ACCEPT)
        ok.add_css_class("suggested-action")
        self._active_chooser = chooser

        def _resp(dlg: Gtk.FileChooserDialog, resp: int) -> None:
            paths: List[str] = []
            if resp == Gtk.ResponseType.ACCEPT:
                files = dlg.get_files()
                for i in range(files.get_n_items()):
                    gf = files.get_item(i)
                    if gf and gf.get_path():
                        paths.append(gf.get_path())
            dlg.destroy()
            self._active_chooser = None
            if paths:
                self.submit(
                    self.upload_engine.add_files(
                        paths,
                        [acc.id],
                        remote_folder=self._current_folder_path,
                        auto_start=True,
                        remote_folder_id=self._current_folder_id,
                    )
                )
                self.on_show_queue()

        chooser.connect("response", _resp)
        chooser.present()

    def _on_upload_folder_here(self, _btn) -> None:
        acc = self._get_active_account()
        if not acc:
            self.on_add_account(None)
            return
        chooser = Gtk.FileChooserDialog(
            title=f"Subir carpeta a {acc.display_title} (/{self._current_folder_path or 'Raíz'})",
            transient_for=self.parent_window,
            modal=True,
            action=Gtk.FileChooserAction.SELECT_FOLDER,
        )
        chooser.add_button("Cancelar", Gtk.ResponseType.CANCEL)
        ok = chooser.add_button("Subir carpeta aquí", Gtk.ResponseType.ACCEPT)
        ok.add_css_class("suggested-action")
        self._active_chooser = chooser

        def _resp(dlg: Gtk.FileChooserDialog, resp: int) -> None:
            folder_path = ""
            if resp == Gtk.ResponseType.ACCEPT:
                gf = dlg.get_file()
                if gf and gf.get_path():
                    folder_path = gf.get_path()
            dlg.destroy()
            self._active_chooser = None
            if folder_path:
                self.submit(
                    self.upload_engine.add_folder(
                        folder_path,
                        [acc.id],
                        remote_folder=self._current_folder_path,
                        auto_start=True,
                    )
                )
                self.on_show_queue()

        chooser.connect("response", _resp)
        chooser.present()

    def _on_download_selected(self, _btn) -> None:
        rows = [r for r in self._selected_rows() if not r.is_folder]
        if not rows:
            self._status_lbl.set_text("Selecciona uno o más archivos en la lista para descargarlos.")
            return
        self._download_entries([r.entry for r in rows])

    def _download_entries(self, entries: List[CloudEntry]) -> None:
        acc = self._get_active_account()
        if not acc or not entries:
            return
        self._status_lbl.set_text(
            f"⏳ Enviando {len(entries)} archivo(s) de {acc.display_title} a Mi Transfer — Descargas..."
        )

        async def _queue_all() -> int:
            queued = 0
            for entry in entries:
                url, headers = await self.upload_engine.resolve_remote_download(acc.id, entry)
                u_low = url.lower()
                is_direct_api = (
                    "clients6.google.com/drive/" in u_low
                    or "googleapis.com/drive/" in u_low
                    or "graph.microsoft.com/" in u_low
                    or "dl.dropboxusercontent.com/" in u_low
                )
                await self.download_engine.add_urls(
                    urls=[url],
                    auto_start=True,
                    custom_headers=headers or None,
                    filename_hint=entry.name,
                    source_page=acc.display_title,
                    skip_extractor=is_direct_api,
                    expected_size=entry.size,
                )
                queued += 1
            return queued

        fut = self.submit(_queue_all())

        def _done() -> bool:
            try:
                cnt = fut.result()
                self._status_lbl.set_text(f"✓ {cnt} archivo(s) enviados a Mi Transfer — Descargas.")
                self.on_switch_to_downloader()
            except Exception as exc:
                self._status_lbl.set_text(f"⚠ Error al preparar descarga: {exc}")
            return False

        fut.add_done_callback(lambda _f: GLib.idle_add(_done))

    def _on_create_folder(self, _btn) -> None:
        acc = self._get_active_account()
        if not acc:
            return
        dlg = Gtk.Window(
            title="Nueva carpeta en la nube",
            transient_for=self.parent_window,
            modal=True,
        )
        dlg.set_default_size(400, 150)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_margin_top(16)
        box.set_margin_bottom(16)
        box.set_margin_start(16)
        box.set_margin_end(16)
        dlg.set_child(box)

        box.append(Gtk.Label(label=f"Crear carpeta dentro de /{self._current_folder_path or 'Raíz'}:", xalign=0.0))
        entry = Gtk.Entry()
        entry.set_placeholder_text("Nombre de la carpeta...")
        box.append(entry)

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        sp = Gtk.Box()
        sp.set_hexpand(True)
        b_cancel = Gtk.Button(label="Cancelar")
        b_ok = Gtk.Button(label="Crear")
        b_ok.add_css_class("suggested-action")
        row.append(sp)
        row.append(b_cancel)
        row.append(b_ok)
        box.append(row)

        def _confirm(*_args) -> None:
            name = entry.get_text().strip()
            if not name:
                return
            dlg.destroy()
            self._status_lbl.set_text(f"⏳ Creando carpeta «{name}»...")
            fut = self.submit(
                self.upload_engine.create_remote_folder(
                    account_id=acc.id,
                    parent_id=self._current_folder_id,
                    parent_path=self._current_folder_path,
                    name=name,
                )
            )

            def _done() -> bool:
                try:
                    fut.result()
                    self._status_lbl.set_text(f"✓ Carpeta «{name}» creada.")
                    self._load_current_folder()
                except Exception as exc:
                    self._status_lbl.set_text(f"⚠ No se pudo crear carpeta: {exc}")
                return False

            fut.add_done_callback(lambda _f: GLib.idle_add(_done))

        b_cancel.connect("clicked", lambda *_: dlg.destroy())
        b_ok.connect("clicked", _confirm)
        entry.connect("activate", _confirm)
        dlg.present()

    def _on_pin_destination(self, _btn) -> None:
        acc = self._get_active_account()
        if not acc:
            return
        self.upload_engine.set_default_remote_folder(acc.id, self._current_folder_path)
        self._update_pinned_label()
        self._status_lbl.set_text(
            f"✓ Destino predeterminado de {acc.display_title} fijado en /{self._current_folder_path or 'Raíz'}."
        )

    def _on_copy_link(self, _btn) -> None:
        rows = self._selected_rows()
        if not rows:
            self._status_lbl.set_text("Selecciona un archivo o carpeta para copiar su enlace.")
            return
        url = rows[0].entry.web_url or rows[0].entry.download_url
        if not url:
            self._status_lbl.set_text("Este elemento no tiene enlace público directo disponible.")
            return
        display = Gdk.Display.get_default()
        if display:
            display.get_clipboard().set(url)
            self._status_lbl.set_text(f"✓ Enlace copiado: {url[:65]}")

    def _on_open_web_clicked(self, _btn) -> None:
        acc = self._get_active_account()
        if not acc:
            self.on_add_account(None)
            return
        self._status_lbl.set_text(f"⏳ Abriendo interfaz web de {acc.display_title}...")
        fut = self.submit(self.upload_engine.open_web_interface(acc.id))

        def _done() -> bool:
            try:
                fut.result()
                self._status_lbl.set_text(f"✓ Sesión web de {acc.display_title} actualizada.")
                if self.on_accounts_updated:
                    self.on_accounts_updated()
                self._load_current_folder()
            except Exception as exc:
                self._status_lbl.set_text(f"⚠ {exc}")
            return False

        fut.add_done_callback(lambda _f: GLib.idle_add(_done))

    def _on_notice_add_clicked(self, _btn) -> None:
        acc = self._get_active_account()
        self.on_add_account(acc)
