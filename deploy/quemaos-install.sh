#!/usr/bin/env bash
#
# Instala la sonda de estado de QuemaOS de Drive (app Lux aparte, drive-quemaos.service).
# Idempotente y sin tocar el servicio de Drive: si falla, Drive sigue como estaba.
#
#   sudo bash deploy/quemaos-install.sh [RUTA_AL_LUX]
#
# Lo normal es que lo lance deploy.sh (que envía el binario de Lux con --lux-bin). El binario
# sale de, por orden: el argumento, LUX_BIN, un fichero «quemaos-lux» junto a deploy/, o el
# /opt/drive/lux que ya hubiera. Drive es Python: este Lux es sólo para la sonda, y es propio
# (/opt/drive/lux), no el del sistema.
set -euo pipefail

APP_DIR=/opt/drive
ENV_FILE=/etc/drive/drive.env
SERVICE_USER=drive

[[ $EUID -eq 0 ]] || { echo "Ejecútalo como root (sudo)." >&2; exit 1; }
[[ -f "$APP_DIR/run.sh" && -f "$ENV_FILE" ]] || { echo "No hay una instalación de Drive en $APP_DIR." >&2; exit 1; }

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"       # .../deploy
PKG="$(dirname "$HERE")"
LUX_SRC="${1:-${LUX_BIN:-}}"
[[ -z "$LUX_SRC" && -f "$PKG/quemaos-lux" ]] && LUX_SRC="$PKG/quemaos-lux"
[[ -z "$LUX_SRC" && -x "$APP_DIR/lux" ]] && LUX_SRC="$APP_DIR/lux"
[[ -n "$LUX_SRC" && -f "$LUX_SRC" ]] || { echo "No hay binario de Lux para la sonda. Pásalo: deploy.sh --lux-bin FICHERO" >&2; exit 2; }

echo "==> Sonda de QuemaOS: copiando"
[[ "$LUX_SRC" -ef "$APP_DIR/lux" ]] || install -m 755 "$LUX_SRC" "$APP_DIR/lux"
rm -rf "$APP_DIR/quemaos"
cp -r "$HERE/quemaos" "$APP_DIR/quemaos"
install -m 755 "$HERE/quemaos-run.sh" "$APP_DIR/quemaos-run.sh"
PROBE_DB=/dev/null "$APP_DIR/lux" --check "$APP_DIR/quemaos" >/dev/null

# El puerto va en su propio fichero, NO en drive.env: Drive rechaza cualquier DRIVE_* que no conoce
# (y compartir fichero con él pondría en juego su arranque por una variable de la sonda).
QENV=/etc/drive/quemaos.env
if [[ ! -f "$QENV" ]]; then
    cat > "$QENV" <<'ENV'
# Sonda de QuemaOS de Drive. QuemaOS la busca en 127.0.0.1, puertos 9700-9799.
PROBE_PORT=9704
ENV
    chmod 640 "$QENV"; chown root:"$SERVICE_USER" "$QENV"
fi
PORT="$(grep -E '^PROBE_PORT=' "$QENV" | tail -1 | cut -d= -f2)"
PORT="${PORT:-9704}"

# Si el puerto lo tiene otra cosa que no es nuestra sonda, mejor parar que pisarla.
if ss -ltn 2>/dev/null | awk '{print $4}' | grep -qE "[:.]$PORT$" && ! systemctl is-active --quiet drive-quemaos; then
    echo "El puerto $PORT ya está ocupado por otra cosa: cámbialo en $QENV (PROBE_PORT, 9700-9799)." >&2
    exit 1
fi

chown -R root:"$SERVICE_USER" "$APP_DIR/quemaos" "$APP_DIR/quemaos-run.sh" "$APP_DIR/lux"
chmod -R g+rX "$APP_DIR/quemaos"

echo "==> Sonda de QuemaOS: servicio"
cp "$HERE/drive-quemaos.service" /etc/systemd/system/drive-quemaos.service
systemctl daemon-reload
systemctl enable drive-quemaos.service >/dev/null 2>&1
systemctl restart drive-quemaos.service

for _ in $(seq 1 20); do
    if curl -fsS --max-time 3 "http://127.0.0.1:$PORT/quemaos/status" 2>/dev/null | grep -q '"quemaos"'; then
        echo "==> Sonda de QuemaOS en http://127.0.0.1:$PORT/quemaos/status"
        exit 0
    fi
    sleep 1
done
journalctl -u drive-quemaos -n 20 --no-pager >&2 || true
echo "La sonda no responde en el puerto $PORT (journalctl -u drive-quemaos)." >&2
exit 1
