#!/usr/bin/env bash
# Instala el servicio Drive Sync (el que sincroniza) como servicio de sistema.
#
#   sudo ./deploy/install.sh [--user NOMBRE]     instalar o actualizar
#   sudo ./deploy/install.sh --uninstall         quitarlo (no borra /var/lib/drive-sync)
#
# Antes: cmake -S . -B build && cmake --build build -j  (compila `lux` y la app).
set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "ejecútalo con sudo" >&2; exit 1; }
HERE=$(cd "$(dirname "$0")/.." && pwd)
APP_USER=${SUDO_USER:-}
UNINSTALL=0
while [ $# -gt 0 ]; do
  case $1 in
    --user) APP_USER=$2; shift 2 ;;
    --uninstall) UNINSTALL=1; shift ;;
    *) echo "opción desconocida: $1" >&2; exit 2 ;;
  esac
done

if [ $UNINSTALL -eq 1 ]; then
  systemctl disable --now drive-sync.service 2>/dev/null || true
  rm -f /etc/systemd/system/drive-sync.service /usr/local/bin/drive-sync /usr/share/applications/drive-sync.desktop /usr/share/icons/hicolor/256x256/apps/drive-sync.png
  rm -rf /opt/drive-sync
  systemctl daemon-reload
  echo "Desinstalado. Los ficheros sincronizados y /var/lib/drive-sync se han conservado."
  exit 0
fi

[ -n "$APP_USER" ] && [ "$APP_USER" != root ] || { echo "indica el usuario dueño de las carpetas: --user NOMBRE" >&2; exit 2; }
id "$APP_USER" >/dev/null || { echo "no existe el usuario $APP_USER" >&2; exit 2; }
LUX=$HERE/build/vendor/lux/lux
[ -x "$LUX" ] || { echo "falta $LUX: compila primero (cmake --build build)" >&2; exit 1; }
command -v sha256sum >/dev/null || { echo "falta sha256sum (coreutils)" >&2; exit 1; }

echo "==> Instalando en /opt/drive-sync (usuario: $APP_USER)"
install -d /opt/drive-sync
install -m 0755 "$LUX" /opt/drive-sync/lux
rm -rf /opt/drive-sync/daemon
cp -r "$HERE/daemon" /opt/drive-sync/daemon
/opt/drive-sync/lux --check /opt/drive-sync/daemon

# La ventana de escritorio (opcional): la misma que `./build/drive-sync`.
if [ -x "$HERE/build/drive-sync" ]; then
  install -m 0755 "$HERE/build/drive-sync" /usr/local/bin/drive-sync
  install -Dm 0644 "$HERE/app/icon.png" /usr/share/icons/hicolor/256x256/apps/drive-sync.png
  install -Dm 0644 "$HERE/deploy/drive-sync.desktop" /usr/share/applications/drive-sync.desktop
fi

sed "s/@USER@/$APP_USER/" "$HERE/deploy/drive-sync.service" > /etc/systemd/system/drive-sync.service
systemctl daemon-reload
systemctl enable drive-sync.service
systemctl restart drive-sync.service

echo "==> Esperando a que arranque…"
for _ in $(seq 20); do
  if curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:7878/api/status | grep -q 401; then ok=1; break; fi
  sleep 1
done
[ "${ok:-0}" = 1 ] || { journalctl -u drive-sync -n 20 --no-pager; echo "el servicio no arrancó" >&2; exit 1; }
echo
echo "Listo. El servicio sincroniza solo, incluso sin sesión iniciada."
echo "  estado:    systemctl status drive-sync"
echo "  registro:  journalctl -u drive-sync -f"
echo "  ventana:   drive-sync        (para enlazar carpetas y ver el estado)"
