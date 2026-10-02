#!/usr/bin/env bash
#
# Envoltorio de las utilidades de consola en una instalación de servidor.
# Se instala como /usr/local/bin/drive-cli.
#
#   sudo drive-cli createuser jose --role admin
#   sudo drive-cli listusers
#   sudo drive-cli check
#
# Carga /etc/drive/drive.env y ejecuta la orden como el usuario del servicio,
# para que los ficheros que cree tengan el dueño correcto.

set -euo pipefail

APP_DIR=/opt/drive
ENV_FILE=/etc/drive/drive.env
SERVICE_USER=drive

if [[ $EUID -ne 0 ]]; then
    echo "Ejecútalo con sudo: sudo drive-cli $*" >&2
    exit 1
fi
if [[ ! -f "$ENV_FILE" ]]; then
    echo "No existe $ENV_FILE. ¿Está Drive instalado?" >&2
    exit 1
fi

# 'source' respeta comentarios y líneas en blanco; 'cat | xargs' no.
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

cd "$APP_DIR"
exec sudo -u "$SERVICE_USER" \
    env DRIVE_CONFIG="$APP_DIR/config/config.yaml" \
        DRIVE_SECURITY__SECRET_KEY="${DRIVE_SECURITY__SECRET_KEY:-}" \
        DRIVE_DATABASE__URL="${DRIVE_DATABASE__URL:-}" \
        DRIVE_STORAGE__ROOT="${DRIVE_STORAGE__ROOT:-}" \
    "$APP_DIR/venv/bin/python" -m app.cli "$@"
