#!/bin/bash
#
# Lanzador del servicio. Lo ejecuta systemd; no se llama a mano.
#
# El shebang es la ruta absoluta a propósito, no "/usr/bin/env bash": la unidad
# puede restringir el PATH y con env el arranque fallaría.
#
# Lee /etc/drive/drive.env (que systemd ya ha cargado en el entorno):
#
#   BIND_PORT     Puerto de escucha de Lux (8000).
#   DRIVE_HOST    Dirección de escucha (127.0.0.1: sólo nginx llega a Lux).
#   DRIVE_MAX_BODY  Tamaño máximo de una petición (2GB).
#   DRIVE_DB      Fichero de la base de datos SQLite.
#   LUX_BIN       Binario de Lux (por defecto, el "lux" del PATH).
#
# Lux resuelve DRIVE_DB al compilar (env() en app/app.lux), así que tiene que
# estar en el entorno antes de lanzarlo; el resto de DRIVE_* los lee la
# aplicación en tiempo de ejecución (ver app/lib/config.lux).

set -euo pipefail

_script="${BASH_SOURCE[0]}"
_dir="${_script%/*}"
[[ "$_dir" == "$_script" ]] && _dir="."
APP_DIR="$(cd "$_dir" && pwd)"

export DRIVE_DB="${DRIVE_DB:-/var/lib/drive/drive.db}"
export DRIVE_CONFIG="${DRIVE_CONFIG:-$APP_DIR/config/config.json}"
BIND_PORT="${BIND_PORT:-8000}"
LUX_BIN="${LUX_BIN:-lux}"

if ! command -v "$LUX_BIN" >/dev/null 2>&1; then
    echo "No se encuentra el binario de Lux ($LUX_BIN). Instálalo con install.sh." >&2
    exit 1
fi

cd "$APP_DIR"
echo "Escuchando en http://127.0.0.1:$BIND_PORT (sin TLS: el cifrado lo pone nginx)"
# --no-watch: en producción no se vigilan los ficheros para recargar.
exec "$LUX_BIN" "$APP_DIR/app" --no-watch --port "$BIND_PORT"
