#!/usr/bin/env bash
#
# Ejecuta las pruebas de Drive.
#
#   tests/run.sh               todos los escenarios
#   tests/run.sh default net_proxy
#
# Cada escenario arranca una instancia limpia de Drive (tests/scenarios/NAME.json
# como configuración, base de datos y blobs en un directorio temporal) y la
# aplicación de pruebas de tests/*.lux, que la recorre por HTTP y devuelve el
# resultado. Todo el código de las pruebas es Lux: este script sólo orquesta.
#
# Variables: LUX (ruta del binario, por defecto "lux"), APP_PORT (8200),
# RUNNER_PORT (8201).

set -u

LUX="${LUX:-lux}"
APP_PORT="${APP_PORT:-8200}"
RUNNER_PORT="${RUNNER_PORT:-8201}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$HERE")"
SECRET="clave-de-pruebas-suficientemente-larga-0123456789"

ALL=(default lockout closed approval quota maxsize nolinks ratelimit retention notrash trashfree
     net_none net_readonly net_full_a net_full_b net_forged net_proxy net_denied)
SCENARIOS=("$@")
[[ ${#SCENARIOS[@]} -eq 0 ]] && SCENARIOS=("${ALL[@]}")

PIDS=()
TMP="$(mktemp -d)"
cleanup() {
    for pid in "${PIDS[@]:-}"; do kill "$pid" 2>/dev/null; done
    wait 2>/dev/null
    rm -rf "$TMP"
}
trap cleanup EXIT

wait_for() {  # url: espera a que el servidor conteste (con cualquier estado)
    for _ in $(seq 1 100); do
        [[ "$(curl -s -o /dev/null -w '%{http_code}' "$1" 2>/dev/null)" != "000" ]] && return 0
        sleep 0.2
    done
    return 1
}

failed=0
for name in "${SCENARIOS[@]}"; do
    cfg="$HERE/scenarios/$name.json"
    if [[ ! -f "$cfg" ]]; then echo "Escenario desconocido: $name" >&2; failed=1; continue; fi

    dir="$TMP/$name"; mkdir -p "$dir"
    DRIVE_DB="$dir/drive.db" \
    DRIVE_STORAGE__ROOT="$dir/blobs" \
    DRIVE_SECURITY__SECRET_KEY="$SECRET" \
    DRIVE_CONFIG="$cfg" \
        "$LUX" "$ROOT/app" --no-watch --port "$APP_PORT" >"$dir/app.log" 2>&1 &
    app=$!; PIDS+=("$app")

    TEST_BASE="http://127.0.0.1:$APP_PORT" TEST_BLOBS="$dir/blobs" TEST_SECRET="$SECRET" \
    TEST_LUX="$LUX" TEST_APP="$ROOT/app" TEST_DB="$dir/drive.db" TEST_CONFIG="$cfg" \
        "$LUX" "$HERE" --no-watch --port "$RUNNER_PORT" >"$dir/runner.log" 2>&1 &
    runner=$!; PIDS+=("$runner")

    wait_for "http://127.0.0.1:$APP_PORT/healthz" && wait_for "http://127.0.0.1:$RUNNER_PORT/"

    out="$(curl -sS --max-time 900 -w '\n%{http_code}' "http://127.0.0.1:$RUNNER_PORT/run/$name")"
    code="${out##*$'\n'}"
    echo "${out%$'\n'*}"
    if [[ "$code" != "200" ]]; then
        failed=1
        echo "--- registro de la instancia ($name) ---"
        tail -n 20 "$dir/app.log"
    fi

    kill "$app" "$runner" 2>/dev/null
    wait "$app" "$runner" 2>/dev/null
    PIDS=()
done

if [[ $failed -eq 0 ]]; then echo "TODO CORRECTO"; else echo "HAY FALLOS"; fi
exit $failed
