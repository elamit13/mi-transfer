# Mint Downloader

Gestor de descargas simultáneas de alto rendimiento, nativo y ligero para **Linux Mint** (Ubuntu/Debian), desarrollado en **Rust** con interfaz **GTK4 + Libadwaita** como alternativa moderna y de bajo consumo de RAM (< 30 MB en reposo) frente a JDownloader.

## Características Principales
- **Motor Asíncrono Multi-Hilo (`tokio` + `reqwest` HTTP/2):** Descarga segmentada mediante cabeceras `Range: bytes=start-end` (8 hilos por archivo por defecto).
- **Dynamic Chunk Stealing:** Cuando un hilo finaliza su segmento antes de tiempo, divide automáticamente a la mitad el segmento activo con mayor cantidad de bytes pendientes para saturar el 100% del ancho de banda disponible hasta el último byte.
- **Persistencia Transaccional en SQLite (`rusqlite` WAL):** Guarda el estado de cada descarga (`Queued`, `Downloading`, `Paused`, `Completed`, `Error`) y el byte exacto de cada fragmento (`chunks`). Permite pausar, cerrar la aplicación y reanudar sin pérdida ni corrupción de datos.
- **Sistema Modular de Resolvers (`HostResolver`):**
  - **MEGA:** Descifrado en streaming **AES-128-CTR al vuelo** con búsqueda de desplazamiento (`StreamCipherSeek`) compatible con descargas multi-hilo sin esperar al final.
  - **MediaFire, Google Drive** (bypass de confirmación de escaneo de virus en archivos grandes), **OneDrive** y **1fichier** (manejo de cookies y token `adz`).
  - **Video y Streaming (`yt-dlp` + `ffmpeg`):** Extracción de metadatos y calidades (`yt-dlp --dump-json`) y descarga de flujos YouTube, Twitch, Vimeo y manifiestos HLS/DASH (`.m3u8` / `.mpd`).
- **Interfaz Gráfica GTK4 + Libadwaita:** Actualización reactiva limitada a **10 FPS (100 ms)** mediante canales `tokio::sync::mpsc` para evitar uso de CPU a altas velocidades de red, con detección automática de enlaces en el portapapeles.

## Instalación de Dependencias en Linux Mint (Ubuntu / Debian)

Para compilar y ejecutar desde el código fuente en Linux Mint:

```bash
sudo apt update
sudo apt install -y \
    build-essential \
    pkg-config \
    libgtk-4-dev \
    libadwaita-1-dev \
    libsqlite3-dev \
    libssl-dev \
    yt-dlp \
    ffmpeg \
    dpkg-dev
```

## Estructura del Workspace

```text
mint-downloader/
├── Cargo.toml                  # Configuración del Cargo Workspace y perfil release (LTO + strip)
├── build-deb.sh                # Script automatizado para generar el paquete instalador .deb
├── packaging/
│   └── mint-downloader.desktop # Lanzador de escritorio para Linux Mint (Cinnamon/MATE/Xfce)
├── core-engine/                # Biblioteca del motor de descargas, SQLite y Resolvers
│   ├── Cargo.toml
│   └── src/
│       ├── lib.rs              # API pública y suite de pruebas unitarias/integración
│       ├── models.rs           # Estructuras de datos, estados y comandos
│       ├── db.rs               # Capa de persistencia en SQLite (tablas downloads y chunks)
│       ├── downloader.rs       # Motor HTTP Range multi-hilo, Dynamic Chunk Stealing y AES-128-CTR
│       ├── resolvers.rs        # Trait HostResolver y plugins (MEGA, GDrive, MediaFire, OneDrive, 1fichier, yt-dlp)
│       └── engine.rs           # Orquestador de cola y emisor de telemetría a 10 FPS
└── mint-downloader-gui/        # Interfaz gráfica nativa GTK4 + Libadwaita
    ├── Cargo.toml
    └── src/
        ├── main.rs             # Inicialización de Tokio runtime, SQLite y aplicación Adwaita
        └── ui.rs               # Ventana principal, HeaderBar, tabla de progreso y modal de enlaces
```

## Compilación y Empaquetado `.deb`

```bash
# Ejecutar pruebas unitarias (descarga segmentada, Dynamic Chunk Stealing, pausa/reanudación y MEGA AES-CTR)
cargo test --workspace

# Construir binario optimizado y paquete .deb
./build-deb.sh

# Instalar el paquete .deb en Linux Mint
sudo dpkg -i mint-downloader_0.1.0_amd64.deb
```
### Aviso
# Hecho totaltemente con IA.
