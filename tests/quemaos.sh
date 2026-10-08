#!/usr/bin/env bash
#
# Pruebas de la sonda de QuemaOS de Drive (deploy/quemaos): arranca un Drive de verdad (Python),
# mide con él activo y luego parado para comprobar el «caído».
#
#   LUX=/ruta/a/lux PYTHON=/ruta/a/python tests/quemaos.sh
#
# LUX: cualquier binario de Lux con sqlite/http/os (el de ~/Calendar/build/vendor/lux/lux sirve).
# PYTHON: un Python con las dependencias de Drive (por defecto, venv/bin/python o python3).
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$HERE")"
LUX="${LUX:-lux}"
PY="${PYTHON:-$( [ -x "$ROOT/venv/bin/python" ] && echo "$ROOT/venv/bin/python" || echo python3 )}"
PORT="${DRIVE_TEST_PORT:-8195}"
TMP="$(mktemp -d)"
PID=""
cleanup() { [ -n "$PID" ] && kill "$PID" 2>/dev/null; wait 2>/dev/null; rm -rf "$TMP"; }
trap cleanup EXIT

export DRIVE_DATABASE__URL="sqlite:///$TMP/drive.db" DRIVE_STORAGE__ROOT="$TMP/blobs" \
       DRIVE_SECURITY__SECRET_KEY="clave-de-pruebas-suficientemente-larga-0123456789" \
       DRIVE_CONFIG="$ROOT/config/config.yaml" DRIVE_MAINTENANCE__ENABLED=false \
       DRIVE_RATE_LIMIT__ENABLED=false BIND_PORT="$PORT"
export PROBE_DB="$TMP/drive.db" PROBE_STORAGE_ROOT="$TMP/blobs" PROBE_URL="http://127.0.0.1:$PORT"
cd "$ROOT"
"$PY" -m app.cli init >/dev/null 2>&1 || { echo "no se pudo inicializar Drive con $PY"; exit 1; }
"$PY" -m app.cli createuser ana --role user --password 'Clave-De-Prueba-1' >/dev/null 2>&1 || { echo "no se pudo crear la cuenta"; exit 1; }
"$PY" -m uvicorn app.main:app --host 127.0.0.1 --port "$PORT" >"$TMP/drive.log" 2>&1 &
PID=$!
for _ in $(seq 1 75); do curl -fs "$PROBE_URL/healthz" >/dev/null 2>&1 && break; sleep 0.2; done
curl -fs "$PROBE_URL/healthz" >/dev/null || { echo "Drive no arranca"; tail -20 "$TMP/drive.log"; exit 1; }

# Una cuenta y un fichero de 10 bytes, por el camino normal (sesión + API).
curl -s -c "$TMP/jar" -o /dev/null -d 'username=ana&password=Clave-De-Prueba-1&next=/' "$PROBE_URL/login"
printf '0123456789' > "$TMP/a.txt"
curl -s -b "$TMP/jar" -o /dev/null -w '%{http_code}' -F "file=@$TMP/a.txt" "$PROBE_URL/api/files" | grep -q 201 || { echo "no se pudo subir el fichero de prueba"; exit 1; }

status=0
echo "== con Drive activo"
"$LUX" test "$ROOT/deploy/quemaos" "$HERE/quemaos_test.lux" -- activo || status=1

echo "== con Drive parado"
kill "$PID"; wait "$PID" 2>/dev/null; PID=""
"$LUX" test "$ROOT/deploy/quemaos" "$HERE/quemaos_test.lux" -- caido || status=1

echo "== el lanzador: rango de la suite, base SQLite y arranque"
mkdir -p "$TMP/opt" && cp "$(command -v "$LUX")" "$TMP/opt/lux" && cp -r "$ROOT/deploy/quemaos" "$TMP/opt/quemaos" && cp "$ROOT/deploy/quemaos-run.sh" "$TMP/opt/"
for bad in 8000 9699 9800 abc; do
  out=$(PROBE_PORT=$bad "$TMP/opt/quemaos-run.sh" 2>&1); code=$?
  if [ $code -ne 0 ] && echo "$out" | grep -q "entre 9700 y 9799"; then echo "  ok    rechaza $bad"; else echo "  FAIL  acepta $bad ($code): $out"; status=1; fi
done
out=$(DRIVE_DATABASE__URL="postgresql://x/y" "$TMP/opt/quemaos-run.sh" 2>&1); code=$?
if [ $code -ne 0 ] && echo "$out" | grep -q "SQLite"; then echo "  ok    rechaza una base que no es SQLite"; else echo "  FAIL  acepta Postgres: $out"; status=1; fi
PROBE_PORT=9791 "$TMP/opt/quemaos-run.sh" >"$TMP/probe.log" 2>&1 & PID=$!
for _ in $(seq 1 50); do curl -fs http://127.0.0.1:9791/quemaos/status >/dev/null 2>&1 && break; sleep 0.2; done
body=$(curl -s http://127.0.0.1:9791/quemaos/status)
echo "$body" | grep -q '"id":"drive"' && echo "  ok    responde en 9791 (Drive parado: $(echo "$body" | sed -E 's/.*"status":"([a-z]+)".*/\1/'))" || { echo "  FAIL  sin respuesta: $body"; status=1; }
[ $status -eq 0 ] && echo "TODO CORRECTO" || echo "HAY FALLOS"
exit $status
