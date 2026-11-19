# Mi Transfer

Gestor nativo de **descargas aceleradas**, **subidas multi-cuenta a la nube** y **explorador integrado de carpetas en la nube** para **Linux Mint** (Ubuntu/Debian), construido con **Python**, **GTK4**, **WebKit2**, **aria2c JSON-RPC**, **yt-dlp/ffmpeg** y extensión estilo IDM para **Firefox**.

## Características principales
- **Descargas aceleradas (`aria2c` + `asyncio`):** Hasta 16 conexiones simultáneas por archivo, soporte BitTorrent (`.torrent` y enlaces magnéticos), extractores modulares (MEGA, MediaFire, Google Drive, OneDrive, 1fichier, YouTube/HLS/DASH con selector de calidades) y extensión Firefox IDM (puerto local 16890).
- **Subidas multi-cuenta y explorador integrado en la misma ventana:** Botón de giro en la barra superior para alternar entre **Descargas** y **Subidas**, con soporte para múltiples cuentas sincronizadas de **GDrive, MEGA, OneDrive, Dropbox y Mediafire**.
- **Selector directo de servidor en el panel izquierdo (`Cuentas en la nube`):** Muestra todas tus cuentas con formato `Nombre (Servidor)` (ej. `Personal (GDrive)`, `Trabajo (MEGA)`). Al hacer clic en cualquier cuenta se cargan al instante sus carpetas y archivos dentro de la misma ventana sin abrir ventanas adicionales.
- **Ventana de inicio de sesión y sesiones permanentes compartidas:** Inicio de sesión directo en ventana nativa (`WebKit2`) con perfil persistente por cuenta, cifrado **AES-256-GCM** en SQLite WAL y exportación de cookies compartidas (`0600`) para Subidas y Descargas.
- **Interfaz GTK4 nativa 100% en español:** Diseño oscuro moderno, barras organizadas de doble fila, panel lateral por categorías, búsqueda instantánea, selección múltiple y fondo personalizable por color o imagen.

## Instalación y ejecución

```bash
./install.sh
```

Para verificar el estado de todos los subsistemas:

```bash
python3 main.py --self-test
```

Para iniciar **Mi Transfer** directamente:

```bash
python3 main.py
```
### Aviso
# Hecho totaltemente con IA.
