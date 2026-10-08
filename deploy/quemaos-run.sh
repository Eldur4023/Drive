#!/bin/bash
#
# Lanzador de la sonda de QuemaOS (drive-quemaos.service). Lee /etc/drive/drive.env (la base y el
# puerto de Drive, sólo para leerlos) y /etc/drive/quemaos.env (lo suyo).
#
# OJO con los nombres: Drive trata CUALQUIER variable DRIVE_* como configuración y se niega a
# arrancar si no la conoce, así que lo de la sonda NO puede llamarse DRIVE_* ni vivir en drive.env.
#
#   DRIVE_DATABASE__URL    la base de Drive (sqlite:////ruta/drive.db); sólo se lee
#   DRIVE_STORAGE__ROOT    el directorio de ficheros, para mirar el disco
#   BIND_PORT              dónde escucha Drive, para comprobar que responde (8000)
#   PROBE_PORT             puerto de la sonda: 9700-9799, el rango de la suite (9704)
#   PROBE_HOST     127.0.0.1 (QuemaOS sólo consulta el bucle local)
set -euo pipefail

_script="${BASH_SOURCE[0]}"
_dir="${_script%/*}"
[[ "$_dir" == "$_script" ]] && _dir="."
APP_DIR="$(cd "$_dir" && pwd)"

PORT="${PROBE_PORT:-9704}"
if ! [[ "$PORT" =~ ^97[0-9][0-9]$ ]]; then
    echo "PROBE_PORT debe estar entre 9700 y 9799 (es '$PORT')" >&2
    exit 1
fi

URL="${DRIVE_DATABASE__URL:-}"
if [[ "$URL" != sqlite:///* ]]; then
    echo "La sonda sólo sabe leer SQLite (DRIVE_DATABASE__URL='$URL')" >&2
    exit 1
fi
DB="${URL#sqlite:///}"                      # sqlite:////var/lib/drive/drive.db -> /var/lib/drive/drive.db
[[ "$DB" == /* ]] || DB="$APP_DIR/${DB#./}" # una ruta relativa cuelga de /opt/drive, como en el servicio

SCHEME=http
[[ -n "${TLS_CERT:-}" ]] && SCHEME=https
export PROBE_DB="$DB"
export PROBE_STORAGE_ROOT="${DRIVE_STORAGE__ROOT:-}"
# Drive puede escuchar sólo en la IP de Tailscale (BIND_HOST): el /healthz se comprueba donde escucha.
CHK="${BIND_HOST:-127.0.0.1}"; [[ "$CHK" == "0.0.0.0" ]] && CHK=127.0.0.1
export PROBE_URL="$SCHEME://$CHK:${BIND_PORT:-8000}"
export PROBE_PORT="$PORT"

echo "Sonda de QuemaOS en http://${PROBE_HOST:-127.0.0.1}:$PORT/quemaos/status (base: $DB)"
exec "$APP_DIR/lux" "$APP_DIR/quemaos" --no-watch --port "$PORT"
