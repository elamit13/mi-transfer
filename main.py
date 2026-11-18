#!/usr/bin/env python3
"""
Mint Downloader - Gestor de descargas simultáneas ligero y ultrarrápido para Linux Mint.
Punto de entrada principal (GTK4 + aria2c JSON-RPC + yt-dlp/ffmpeg + Extractores Cloud).
"""

from __future__ import annotations

import glob
import os
import site
import sys
from pathlib import Path


def _bootstrap_local_environment() -> None:
    """
    Automatically re-exec into the local .venv Python interpreter if invoked with
    system /usr/bin/python3, so all .venv packages (such as yt-dlp 2026+) are loaded
    cleanly without conflicts from /usr/lib/python3/dist-packages.
    """
    base_dir = Path(__file__).resolve().parent
    venv_python = base_dir / ".venv" / "bin" / "python"
    if venv_python.is_file() and os.access(venv_python, os.X_OK):
        try:
            if Path(sys.executable).resolve() != venv_python.resolve():
                os.execv(str(venv_python), [str(venv_python), str(Path(__file__).resolve()), *sys.argv[1:]])
        except Exception:
            pass

    if str(base_dir) not in sys.path:
        sys.path.insert(0, str(base_dir))

    if sys.prefix == sys.base_prefix:
        for sp in glob.glob(str(base_dir / ".venv" / "lib" / "python*" / "site-packages")):
            site.addsitedir(sp)
            if sp in sys.path:
                sys.path.remove(sp)
            sys.path.insert(1, sp)

    local_typelib = base_dir / ".local_deps" / "usr" / "lib" / "x86_64-linux-gnu" / "girepository-1.0"
    if local_typelib.is_dir():
        cur_typelib = os.environ.get("GI_TYPELIB_PATH", "")
        if str(local_typelib) not in cur_typelib:
            os.environ["GI_TYPELIB_PATH"] = (
                f"{local_typelib}:{cur_typelib}" if cur_typelib else str(local_typelib)
            )

    local_bin = base_dir / ".local_deps" / "usr" / "bin"
    if local_bin.is_dir():
        cur_path = os.environ.get("PATH", "")
        if str(local_bin) not in cur_path:
            os.environ["PATH"] = f"{local_bin}:{cur_path}" if cur_path else str(local_bin)

    local_lib = base_dir / ".local_deps" / "usr" / "lib" / "x86_64-linux-gnu"
    if local_lib.is_dir():
        cur_ld = os.environ.get("LD_LIBRARY_PATH", "")
        if str(local_lib) not in cur_ld:
            os.environ["LD_LIBRARY_PATH"] = f"{local_lib}:{cur_ld}" if cur_ld else str(local_lib)


_bootstrap_local_environment()

import asyncio
from core.engine import DownloadEngine
from ui.main_window import AsyncEngineRunner, MintDownloaderApp


async def _run_self_test() -> int:
    """Run a quick diagnostic check of aria2c, yt-dlp, ffmpeg, GTK4, and SQLite."""
    import shutil
    import gi

    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    print("=== Diagnóstico de Mint Downloader ===")
    print(f"  [OK] Python: {sys.version.split()[0]}")
    print(
        f"  [OK] GTK4: {Gtk.get_major_version()}.{Gtk.get_minor_version()}.{Gtk.get_micro_version()}"
    )

    engine = DownloadEngine()
    await engine.start()
    ver = await engine.rpc_call("aria2.getVersion")
    print(f"  [OK] aria2c RPC activo: v{ver.get('version')} (puerto {engine.rpc_port})")
    print(f"  [OK] yt-dlp: {shutil.which('yt-dlp') or 'instalado (módulo python)'}")
    print(f"  [OK] ffmpeg: {shutil.which('ffmpeg') or 'no encontrado'}")
    print(f"  [OK] SQLite DB: {engine.db_path}")
    await engine.shutdown()
    print("=== Todos los subsistemas están operativos ===")
    return 0


def main() -> int:
    if "--self-test" in sys.argv or "--check" in sys.argv:
        return asyncio.run(_run_self_test())

    engine = DownloadEngine()
    runner = AsyncEngineRunner(engine)
    runner.start()

    app = MintDownloaderApp(runner)
    exit_code = app.run(sys.argv)
    runner.stop()
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
