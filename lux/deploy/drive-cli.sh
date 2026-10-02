#!/usr/bin/env bash
#
# Órdenes de administración de una instalación de servidor. Se instala como
# /usr/local/bin/drive-cli.
#
#   sudo drive-cli createuser NOMBRE [--role admin|user|guest] [--email X] [--password P]
#   sudo drive-cli passwd NOMBRE [--password P]
#   sudo drive-cli listusers
#   sudo drive-cli recompute
#   sudo drive-cli maintenance
#   sudo drive-cli check
#   drive-cli secret
#
# Es un envoltorio de `lux run` (app/routes/commands.lux): carga
# /etc/drive/drive.env y ejecuta la orden como el usuario del servicio, para
# que los ficheros que cree tengan el dueño correcto.

set -euo pipefail

ENV_FILE="${DRIVE_ENV_FILE:-/etc/drive/drive.env}"
APP_DIR="${DRIVE_APP_DIR:-/opt/drive}"
SERVICE_USER=drive

uso() {
    sed -n '3,15p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    exit "${1:-1}"
}

comando="${1:-}"
[[ -z "$comando" || "$comando" == "-h" || "$comando" == "--help" ]] && uso 0

# La clave nueva no necesita el servicio ni el entorno.
if [[ "$comando" == "secret" ]]; then
    openssl rand -base64 48 | tr '+/' '-_' | tr -d '=\n'
    echo
    exit 0
fi

if [[ ! -r "$ENV_FILE" ]]; then
    echo "No puedo leer $ENV_FILE. ¿Está Drive instalado? ¿Usas sudo?" >&2
    exit 1
fi
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a
export DRIVE_CONFIG="${DRIVE_CONFIG:-$APP_DIR/config/config.json}"

# Como el usuario del servicio si somos root; tal cual en el resto de casos.
if [[ $EUID -eq 0 ]] && id "$SERVICE_USER" &>/dev/null; then
    exec sudo -E -u "$SERVICE_USER" lux run "$APP_DIR/app" -- "$@"
fi
exec lux run "$APP_DIR/app" -- "$@"
