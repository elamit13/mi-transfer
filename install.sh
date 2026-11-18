#!/usr/bin/env bash
# ==============================================================================
# Mint Downloader - Instalador automático para Linux Mint / Ubuntu / Debian
# Verifica e instala dependencias (python3-gi, gir1.2-gtk-4.0, aria2, yt-dlp,
# ffmpeg, aiohttp, pycryptodome) y registra el lanzador .desktop en el menú.
# ==============================================================================
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DESKTOP_DIR="${HOME}/.local/share/applications"
DESKTOP_FILE="${DESKTOP_DIR}/mint-downloader.desktop"

echo "============================================================"
echo " Instalando Mint Downloader en: ${APP_DIR}"
echo "============================================================"

# 1. Verificar paquetes del sistema (con sudo si está disponible de forma interactiva, o user-space)
MISSING_PKGS=()
for pkg in python3-gi gir1.2-gtk-4.0 aria2 ffmpeg python3-aiohttp python3-pycryptodome; do
    if ! dpkg -s "$pkg" >/dev/null 2>&1; then
        MISSING_PKGS+=("$pkg")
    fi
done

if [ "${#MISSING_PKGS[@]}" -gt 0 ]; then
    echo "[*] Paquetes del sistema faltantes: ${MISSING_PKGS[*]}"
    if [ "$(id -u)" -eq 0 ]; then
        apt-get update -y
        apt-get install -y "${MISSING_PKGS[@]}"
    elif sudo -n true 2>/dev/null; then
        sudo apt-get update -y
        sudo apt-get install -y "${MISSING_PKGS[@]}"
    else
        echo "[*] Configurando dependencias en espacio de usuario (.local_deps y .venv)..."
        mkdir -p "${APP_DIR}/.local_deps"
        if [ ! -x "${APP_DIR}/.local_deps/usr/bin/aria2c" ] || [ ! -f "${APP_DIR}/.local_deps/usr/lib/x86_64-linux-gnu/girepository-1.0/Gtk-4.0.typelib" ]; then
            TMP_DEB_DIR="$(mktemp -d)"
            (
                cd "${TMP_DEB_DIR}"
                apt-get download aria2 libaria2-0 gir1.2-gtk-4.0
                for deb in ./*.deb; do
                    dpkg-deb -x "${deb}" "${APP_DIR}/.local_deps"
                done
            )
            rm -rf "${TMP_DEB_DIR}"
        fi
    fi
else
    echo "[OK] Todos los paquetes base de APT están instalados."
fi

# 2. Verificar yt-dlp
if ! command -v yt-dlp >/dev/null 2>&1; then
    echo "[*] Instalando yt-dlp..."
    if command -v uv >/dev/null 2>&1; then
        uv tool install yt-dlp || true
    elif command -v pip3 >/dev/null 2>&1; then
        pip3 install --user yt-dlp
    fi
else
    echo "[OK] yt-dlp detectado en: $(command -v yt-dlp)"
fi

# 3. Verificar entorno virtual local con paquetes del sistema (PyGObject + aiohttp + pycryptodomex)
if [ -x "${APP_DIR}/.venv/bin/python" ] && "${APP_DIR}/.venv/bin/python" -c "import aiohttp, Cryptodome, yt_dlp" >/dev/null 2>&1; then
    echo "[OK] Entorno virtual Python (.venv) verificado con aiohttp, Cryptodome y yt_dlp."
elif ! python3 -c "import aiohttp, Cryptodome, yt_dlp" >/dev/null 2>&1; then
    if [ ! -d "${APP_DIR}/.venv" ]; then
        if command -v uv >/dev/null 2>&1; then
            uv venv --system-site-packages "${APP_DIR}/.venv"
        else
            python3 -m venv --system-site-packages "${APP_DIR}/.venv"
        fi
    fi
    if command -v uv >/dev/null 2>&1; then
        uv pip install --python "${APP_DIR}/.venv/bin/python" aiohttp pycryptodomex yt-dlp
    else
        "${APP_DIR}/.venv/bin/pip" install aiohttp pycryptodomex yt-dlp
    fi
fi

# 4. Dar permisos de ejecución al punto de entrada
chmod +x "${APP_DIR}/main.py"

# 5. Crear el lanzador .desktop para el menú de Linux Mint (Cinnamon / MATE / Xfce)
mkdir -p "${DESKTOP_DIR}"
mkdir -p "${HOME}/.local/share/icons/hicolor/scalable/apps"
if [ -f "${APP_DIR}/assets/mint-downloader.svg" ]; then
    cp -f "${APP_DIR}/assets/mint-downloader.svg" "${HOME}/.local/share/icons/hicolor/scalable/apps/mint-downloader.svg"
    cp -f "${APP_DIR}/assets/mint-downloader.svg" "${HOME}/.local/share/icons/hicolor/scalable/apps/org.linuxmint.MintDownloader.svg"
    gtk-update-icon-cache -f -t "${HOME}/.local/share/icons/hicolor" >/dev/null 2>&1 || true
fi

cat > "${DESKTOP_FILE}" <<EOF
[Desktop Entry]
Version=1.0
Type=Application
Name=Mint Downloader
GenericName=Gestor de Descargas
Comment=Gestor de descargas simultáneas ligero y ultrarrápido (aria2c + yt-dlp)
Exec="${APP_DIR}/.venv/bin/python" "${APP_DIR}/main.py" %U
Path=${APP_DIR}
Icon=${APP_DIR}/assets/mint-downloader.svg
Terminal=false
Categories=Network;FileTransfer;GTK;
StartupNotify=true
StartupWMClass=org.linuxmint.MintDownloader
EOF

chmod +x "${DESKTOP_FILE}"
if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "${DESKTOP_DIR}" >/dev/null 2>&1 || true
fi

echo "[OK] Lanzador .desktop creado en: ${DESKTOP_FILE}"

# 6. Ejecutar autodiagnóstico final
echo "[*] Ejecutando verificación de módulos y motores..."
/usr/bin/python3 "${APP_DIR}/main.py" --self-test

echo "============================================================"
echo " Instalación completada con éxito."
echo " Puedes abrir 'Mint Downloader' desde el menú de Linux Mint"
echo " o ejecutando: /usr/bin/python3 ${APP_DIR}/main.py"
echo "============================================================"
