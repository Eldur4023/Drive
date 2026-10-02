#!/usr/bin/env bash
#
# Actualiza el código de una instalación ya existente, sin tocar la base de
# datos, los blobs, config.yaml ni el secreto de firma.
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
if [[ ! -d "$APP_DIR/venv" ]]; then
    echo "No hay ninguna instalación en $APP_DIR. Usa install.sh." >&2
    exit 1
fi

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PREVIOUS="$APP_DIR/app.anterior"

echo "==> Parando el servicio"
systemctl stop drive || true

echo "==> Guardando la versión anterior"
rm -rf "$PREVIOUS"
if [[ -d "$APP_DIR/app" ]]; then
    mv "$APP_DIR/app" "$PREVIOUS"
fi
# La unidad también se guarda: si el fallo viene de ella (un ExecStart que no
# existe, por ejemplo), restaurar sólo el código dejaría el servicio muerto.
UNIT_BACKUP=""
if [[ -f /etc/systemd/system/drive.service ]]; then
    UNIT_BACKUP="$APP_DIR/drive.service.anterior"
    cp /etc/systemd/system/drive.service "$UNIT_BACKUP"
fi

restaurar() {
    echo "!!! La actualización ha fallado: se vuelve a la versión anterior." >&2
    rm -rf "$APP_DIR/app"
    if [[ -d "$PREVIOUS" ]]; then
        mv "$PREVIOUS" "$APP_DIR/app"
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
cp "$SOURCE_DIR/requirements.txt" "$APP_DIR/"
cp "$SOURCE_DIR/deploy/run.sh" "$APP_DIR/run.sh"
chmod 755 "$APP_DIR/run.sh"
rm -rf "$APP_DIR/deploy"
cp -r "$SOURCE_DIR/deploy" "$APP_DIR/deploy"
cp "$SOURCE_DIR/deploy/drive-cli.sh" /usr/local/bin/drive-cli
chmod 755 /usr/local/bin/drive-cli

# config.yaml no se pisa nunca: puede tener ajustes propios del servidor.
if [[ ! -f "$APP_DIR/config/config.yaml" ]]; then
    mkdir -p "$APP_DIR/config"
    cp "$SOURCE_DIR/config/config.yaml" "$APP_DIR/config/config.yaml"
fi

echo "==> Revisando el fichero de entorno"
# Las instalaciones anteriores a run.sh no tienen estas variables. Se añaden
# con los mismos valores que llevaba fija la unidad antigua, para que la
# actualización no cambie en silencio dónde ni cómo escucha el servicio.
anadir_si_falta() {
    local clave="$1" valor="$2"
    if ! grep -q "^${clave}=" "$ENV_FILE"; then
        echo "${clave}=${valor}" >> "$ENV_FILE"
        echo "    añadido ${clave}=${valor}"
    fi
}
if ! grep -q '^BIND_PORT=' "$ENV_FILE"; then
    printf '\n# --- Dónde y cómo escucha el proceso (lo lee run.sh) ---\n' >> "$ENV_FILE"
fi
anadir_si_falta "BIND_HOST" "127.0.0.1"
anadir_si_falta "BIND_PORT" "8000"
anadir_si_falta "BIND_WORKERS" "2"
anadir_si_falta "BEHIND_PROXY" "1"
anadir_si_falta "TRUSTED_PROXY" "127.0.0.1"
anadir_si_falta "TLS_CERT" ""
anadir_si_falta "TLS_KEY" ""
chown root:"$SERVICE_USER" "$ENV_FILE"
chmod 640 "$ENV_FILE"

echo "==> Actualizando dependencias"
"$APP_DIR/venv/bin/pip" install --quiet --upgrade -r "$APP_DIR/requirements.txt"

echo "==> Aplicando cambios de esquema"
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a
cd "$APP_DIR"
sudo -u "$SERVICE_USER" \
    env DRIVE_SECURITY__SECRET_KEY="${DRIVE_SECURITY__SECRET_KEY:-}" \
        DRIVE_DATABASE__URL="${DRIVE_DATABASE__URL:-}" \
        DRIVE_STORAGE__ROOT="${DRIVE_STORAGE__ROOT:-}" \
        DRIVE_CONFIG="$APP_DIR/config/config.yaml" \
    "$APP_DIR/venv/bin/python" -m app.cli init

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

# Un arranque limpio puede tardar un par de segundos; se comprueba de verdad
# que el proceso sigue en pie antes de dar el despliegue por bueno.
for _ in $(seq 1 10); do
    sleep 1
    if systemctl is-active --quiet drive; then
        break
    fi
done

if ! systemctl is-active --quiet drive; then
    journalctl -u drive -n 30 --no-pager >&2 || true
    restaurar
fi

trap - ERR

if command -v curl >/dev/null 2>&1; then
    # Si hay certificado configurado, el servicio habla HTTPS. Se valida con -k
    # porque un certificado autofirmado o emitido para el dominio público no
    # verifica contra 127.0.0.1.
    ESQUEMA=http
    EXTRA=()
    if grep -qE '^TLS_CERT=.+' "$ENV_FILE" 2>/dev/null; then
        ESQUEMA=https
        EXTRA=(-k)
    fi

    if curl -fsS "${EXTRA[@]}" --max-time 5 "$ESQUEMA://127.0.0.1:$HEALTH_PORT/healthz" >/dev/null 2>&1; then
        echo "==> /healthz responde en $ESQUEMA://127.0.0.1:$HEALTH_PORT"
    else
        echo "    Aviso: /healthz no responde en $ESQUEMA://127.0.0.1:$HEALTH_PORT."
        echo "    El servicio está activo; comprueba BIND_PORT en $ENV_FILE."
    fi
fi

rm -rf "$PREVIOUS"
rm -f "$APP_DIR/drive.service.anterior"
echo
echo "Actualización completada."
systemctl status drive --no-pager --lines=0 || true
