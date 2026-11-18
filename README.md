# Mint Downloader

Gestor de descargas simultáneas nativo, ligero y ultrarrápido para **Linux Mint** (Ubuntu/Debian), construido con **Python**, **GTK4**, **aria2c JSON-RPC**, **yt-dlp/ffmpeg** y extensión estilo IDM para **Firefox**.

## Características principales
- **Motor acelerado (`aria2c` + `asyncio`):** Hasta 16 conexiones simultáneas por archivo, soporte BitTorrent (`.torrent` y enlaces magnéticos) y control global de descargas concurrentes y límite de velocidad.
- **Extractores modulares:**
  - **Nube y enlaces directos:** MEGA (con descifrado local AES-128-CTR en streaming), MediaFire (incluyendo expansión de carpetas, lectura de cookies/historial de Firefox y verificación en navegador), Google Drive, OneDrive y 1fichier.
  - **Video y transmisión web (`yt-dlp` + `ffmpeg`):** Selector dinámico de calidades (hasta 4K UHD y extracción de audio MP3) para YouTube, Twitch, Vimeo y transmisiones HLS/DASH (`.m3u8` / `.mpd`).
- **Extensión Firefox IDM (`firefox-extension/`):** Panel flotante arrastrable sobre reproductores web con selector `Calidad/...`, captura automática de descargas del navegador y puente HTTP local (`127.0.0.1:16890`).
- **Persistencia SQLite WAL:** Guarda la cola, contenedores/paquetes, carpeta de destino y fondo personalizado entre sesiones.
- **Interfaz GTK4 nativa en español:** Barra superior organizada de doble fila con botones uniformes, panel lateral de categorías (`Todas las descargas`, `Música`, `Comprimidos`, `Videos`, `Programas`, `Documentos`, `APKs`, `Imágenes`, `Sin terminar`, `Terminadas`), búsqueda instantánea, selección múltiple y fondo personalizable por color o imagen.

## Estructura del proyecto

```text
mint-downloader/
├── main.py                              # Punto de entrada y autodiagnóstico (--self-test)
├── install.sh                           # Instalador automático y lanzador .desktop
├── core/
│   ├── engine.py                        # Orquestador aria2c JSON-RPC, puente HTTP y SQLite WAL
│   └── extractors/
│       ├── cloud_extractor.py           # MEGA, MediaFire, Google Drive, OneDrive, 1fichier y directos
│       └── video_extractor.py           # yt-dlp + ffmpeg y caché de calidades
├── ui/
│   └── main_window.py                   # Interfaz GTK4 completa en español
├── firefox-extension/                   # Extensión estilo IDM para Firefox (.xpi)
└── assets/                              # Iconos SVG/PNG de la aplicación
```

## Instalación y ejecución

```bash
./install.sh
```

Para verificar el estado de todos los subsistemas (`Python`, `GTK4`, `aria2c`, `yt-dlp`, `ffmpeg` y `SQLite`):

```bash
python3 main.py --self-test
```

Para iniciar la aplicación directamente:

```bash
python3 main.py
```
### Aviso
# Hecho totaltemente con IA.
