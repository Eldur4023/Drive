#!/usr/bin/env bash
#
# Actualiza el código de una instalación ya existente, sin tocar la base de
# datos, los blobs, config.json ni el secreto de firma.
#
# No se ejecuta a mano: lo lanzan deploy.sh y deploy.bat sobre el servidor.
# Para una instalación desde cero, usa install.sh.
#
#   sudo bash deploy/remote-update.sh [PUERTO_HEALTHZ]

set -euo pipefail

APP_DIR=/opt/drive
ENV_FILE=/etc/drive/drive.env
SERVICE_USER=drive

# El puerto se toma del propio servidor salvo que se indique otro: así la
# comprobación final no falla por haberlo cambiado allí.
HEALTH_PORT="${1:-}"
if [[ -z "$HEALTH_PORT" && -f "$ENV_FILE" ]]; then
    HEALTH_PORT="$(grep -E '^BIND_PORT=' "$ENV_FILE" | cut -d= -f2 || true)"
fi
HEALTH_PORT="${HEALTH_PORT:-8000}"

if [[ $EUID -ne 0 ]]; then
    echo "Ejecútalo como root (sudo)." >&2
    exit 1
fi
if [[ ! -f "$APP_DIR/app/app.lux" ]]; then
    echo "No hay ninguna instalación en $APP_DIR. Usa install.sh." >&2
    exit 1
fi

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PREVIOUS="$APP_DIR/app.anterior"
LUX_PREVIOUS=""

echo "==> Parando el servicio"
systemctl stop drive || true

echo "==> Guardando la versión anterior"
rm -rf "$PREVIOUS"
mv "$APP_DIR/app" "$PREVIOUS"
# La unidad y el binario también se guardan: si el fallo viene de ellos (un
# ExecStart que no existe, un binario incompatible), restaurar sólo el código
# dejaría el servicio muerto.
UNIT_BACKUP=""
if [[ -f /etc/systemd/system/drive.service ]]; then
    UNIT_BACKUP="$APP_DIR/drive.service.anterior"
    cp /etc/systemd/system/drive.service "$UNIT_BACKUP"
fi
if [[ -f "$SOURCE_DIR/lux" && -x /usr/local/bin/lux ]]; then
    LUX_PREVIOUS="$APP_DIR/lux.anterior"
    cp /usr/local/bin/lux "$LUX_PREVIOUS"
fi

restaurar() {
    echo "!!! La actualización ha fallado: se vuelve a la versión anterior." >&2
    rm -rf "$APP_DIR/app"
    if [[ -d "$PREVIOUS" ]]; then
        mv "$PREVIOUS" "$APP_DIR/app"
    fi
    if [[ -n "$LUX_PREVIOUS" && -f "$LUX_PREVIOUS" ]]; then
        install -m 755 "$LUX_PREVIOUS" /usr/local/bin/lux
        rm -f "$LUX_PREVIOUS"
    fi
    if [[ -n "$UNIT_BACKUP" && -f "$UNIT_BACKUP" ]]; then
        cp "$UNIT_BACKUP" /etc/systemd/system/drive.service
        systemctl daemon-reload
        rm -f "$UNIT_BACKUP"
    fi
    systemctl start drive || true
    exit 1
}
trap restaurar ERR

echo "==> Copiando el código nuevo"
cp -r "$SOURCE_DIR/app" "$APP_DIR/"
cp "$SOURCE_DIR/deploy/run.sh" "$APP_DIR/run.sh"
chmod 755 "$APP_DIR/run.sh"
rm -rf "$APP_DIR/deploy"
cp -r "$SOURCE_DIR/deploy" "$APP_DIR/deploy"
rm -f "$APP_DIR/deploy/lux"
cp "$SOURCE_DIR/deploy/drive-cli.sh" /usr/local/bin/drive-cli
chmod 755 /usr/local/bin/drive-cli
if [[ -f "$SOURCE_DIR/lux" ]]; then
    echo "==> Instalando el binario de Lux enviado"
    install -m 755 "$SOURCE_DIR/lux" /usr/local/bin/lux
fi

# config.json no se pisa nunca: puede tener ajustes propios del servidor.
if [[ ! -f "$APP_DIR/config/config.json" ]]; then
    mkdir -p "$APP_DIR/config"
    cp "$SOURCE_DIR/config/config.json" "$APP_DIR/config/config.json"
fi

echo "==> Revisando el fichero de entorno"
anadir_si_falta() {
    local clave="$1" valor="$2"
    if ! grep -q "^${clave}=" "$ENV_FILE"; then
        echo "${clave}=${valor}" >> "$ENV_FILE"
        echo "    añadido ${clave}=${valor}"
    fi
}
anadir_si_falta "DRIVE_DB" "/var/lib/drive/drive.db"
anadir_si_falta "DRIVE_STORAGE__ROOT" "/var/lib/drive/blobs"
anadir_si_falta "BIND_PORT" "8000"
chown root:"$SERVICE_USER" "$ENV_FILE"
chmod 640 "$ENV_FILE"

chown -R root:"$SERVICE_USER" "$APP_DIR"
chmod -R g+rX "$APP_DIR"

# La unidad de systemd puede haber cambiado entre versiones.
if ! cmp -s "$SOURCE_DIR/deploy/drive.service" /etc/systemd/system/drive.service; then
    echo "==> Actualizando la unidad de systemd"
    cp "$SOURCE_DIR/deploy/drive.service" /etc/systemd/system/drive.service
    systemctl daemon-reload
fi

echo "==> Arrancando el servicio"
systemctl start drive

# El esquema nuevo se crea con la primera petición; /healthz no toca la base de
# datos, así que se hace además una petición a una página real.
for _ in $(seq 1 15); do
    sleep 1
    if curl -fsS --max-time 3 "http://127.0.0.1:$HEALTH_PORT/healthz" >/dev/null 2>&1; then
        break
    fi
done

if ! curl -fsS --max-time 10 "http://127.0.0.1:$HEALTH_PORT/login" >/dev/null 2>&1; then
    journalctl -u drive -n 30 --no-pager >&2 || true
    restaurar
fi

trap - ERR

echo "==> Drive responde en http://127.0.0.1:$HEALTH_PORT"
rm -rf "$PREVIOUS"
rm -f "$APP_DIR/drive.service.anterior" "$APP_DIR/lux.anterior"
echo
echo "Actualización completada."
systemctl status drive --no-pager --lines=0 || true
