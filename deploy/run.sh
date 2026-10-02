#!/bin/bash
#
# Lanzador del servicio. Lo ejecuta systemd; no se llama a mano.
#
# El shebang es la ruta absoluta a propósito, no "/usr/bin/env bash": la unidad
# restringe el PATH al del entorno virtual, donde no hay ningún bash, y con env
# el arranque falla con "env: bash: No such file or directory".
#
# Monta los argumentos de uvicorn a partir de /etc/drive/drive.env, que es el
# único sitio donde se configura dónde y cómo escucha el proceso:
#
#   BIND_HOST      127.0.0.1 para escuchar sólo en local (con proxy delante),
#                  0.0.0.0 para exponerse a la red directamente.
#   BIND_PORT      Puerto de escucha.
#   BIND_WORKERS   Procesos trabajadores.
#   TLS_CERT       Certificado en PEM (cadena completa). Si está, se sirve HTTPS.
#   TLS_KEY        Clave privada en PEM.
#   BEHIND_PROXY   1 si hay nginx/traefik delante; 0 si se expone directamente.
#   TRUSTED_PROXY  IPs de las que aceptar X-Forwarded-* (sólo con BEHIND_PROXY=1).

set -euo pipefail

# El directorio se deduce sin llamar a dirname: 'cd' y 'pwd' son propios de
# bash, así que el lanzador funciona por restringido que esté el PATH.
_script="${BASH_SOURCE[0]}"
_dir="${_script%/*}"
[[ "$_dir" == "$_script" ]] && _dir="."
APP_DIR="$(cd "$_dir" && pwd)"
UVICORN="$APP_DIR/venv/bin/uvicorn"

BIND_HOST="${BIND_HOST:-127.0.0.1}"
BIND_PORT="${BIND_PORT:-8000}"
BIND_WORKERS="${BIND_WORKERS:-2}"
BEHIND_PROXY="${BEHIND_PROXY:-0}"
TRUSTED_PROXY="${TRUSTED_PROXY:-127.0.0.1}"
TLS_CERT="${TLS_CERT:-}"
TLS_KEY="${TLS_KEY:-}"

args=(
    app.main:app
    --host "$BIND_HOST"
    --port "$BIND_PORT"
    --workers "$BIND_WORKERS"
    --timeout-keep-alive 65
    # Sin proxy delante no hay nadie que absorba las conexiones lentas, así que
    # se limita la concurrencia para que una avalancha no tumbe el proceso.
    --limit-concurrency 512
    --backlog 2048
)

if [[ -n "$TLS_CERT" && -n "$TLS_KEY" ]]; then
    if [[ ! -r "$TLS_CERT" ]]; then
        echo "No se puede leer el certificado: $TLS_CERT" >&2
        exit 1
    fi
    if [[ ! -r "$TLS_KEY" ]]; then
        echo "No se puede leer la clave privada: $TLS_KEY" >&2
        echo "Los certificados de Let's Encrypt son de root: usa setup-tls.sh," >&2
        echo "que deja una copia legible por el usuario del servicio." >&2
        exit 1
    fi
    args+=(--ssl-certfile "$TLS_CERT" --ssl-keyfile "$TLS_KEY")
    echo "Escuchando en https://$BIND_HOST:$BIND_PORT"
else
    echo "Escuchando en http://$BIND_HOST:$BIND_PORT (sin TLS)"
fi

# --proxy-headers sólo cuando hay un proxy de verdad. Activarlo al estar
# expuesto directamente permitiría a cualquiera falsear su IP con una cabecera
# X-Forwarded-For y colarse como si viniera de una red de confianza.
if [[ "$BEHIND_PROXY" == "1" ]]; then
    args+=(--proxy-headers --forwarded-allow-ips "$TRUSTED_PROXY")
else
    args+=(--no-proxy-headers)
fi

cd "$APP_DIR"
exec "$UVICORN" "${args[@]}"
