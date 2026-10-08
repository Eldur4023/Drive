#!/usr/bin/env bash
# Prueba de extremo a extremo: dos carpetas locales (dos «máquinas») enlazadas a la
# misma carpeta de Drive, con un Drive real por detrás.
#
#   DRIVE_URL=http://localhost:18777 DRIVE_TOKEN=<token read+write> \
#   LUX=build/vendor/lux/lux  tests/e2e.sh
set -u
: "${DRIVE_URL:?}" "${DRIVE_TOKEN:?}"
LUX=${LUX:-build/vendor/lux/lux}
ROOT=$(cd "$(dirname "$0")/.." && pwd)
W=$(mktemp -d); A=$W/A; B=$W/B; mkdir -p "$A" "$B" "$W/home"
# Puerto libre al azar: un servicio huérfano de otra corrida no puede compartirlo.
FREE_PORT=$(python3 -c "import socket; s=socket.socket(); s.bind(('127.0.0.1',0)); print(s.getsockname()[1])")
export DRIVE_SYNC_PORT=${DRIVE_SYNC_PORT:-$FREE_PORT} DRIVE_SYNC_HOME=$W/home DRIVE_SYNC_DB=$W/home/state.db
API=http://127.0.0.1:$DRIVE_SYNC_PORT
FAILS=0

ok()   { echo "  ok   $*"; }
bad()  { echo "  FAIL $*"; FAILS=$((FAILS+1)); }
check() { if eval "$2"; then ok "$1"; else bad "$1"; fi; }
# espera hasta 40 s a que una condición se cumpla
wait_for() { for _ in $(seq 40); do eval "$1" && return 0; sleep 1; done; return 1; }

drive() { curl -s -H "Authorization: Bearer $DRIVE_TOKEN" "$@"; }
sync_api() { curl -s -H "X-Drive-Sync-Token: $(cat "$W/home/api-token")" -H "Content-Type: application/json" "$@"; }
tree() { drive "$DRIVE_URL/api/sync/tree?root_id=$RID" | python3 -c "import sys,json; [print(i['path']) for i in json.load(sys.stdin)['items']]" | sort; }
start_daemon() { (cd "$ROOT" && exec "$LUX" --no-watch daemon >>"$W/daemon.log" 2>&1) & DPID=$!; sleep 2; }
stop_daemon() { [ -n "${DPID:-}" ] && kill "$DPID" 2>/dev/null && wait "$DPID" 2>/dev/null; DPID=; sleep 1; }
setpoll() { python3 -c "import sqlite3; c=sqlite3.connect('$W/home/state.db'); c.execute(\"insert into settings values ('poll_seconds','$1') on conflict(key) do update set value='$1'\"); c.commit()"; }

trap 'stop_daemon; rm -rf "$W"' EXIT
RID=$(drive -d "name=e2e-$$" "$DRIVE_URL/api/folders" | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")

start_daemon
sync_api -d "{\"server_url\":\"$DRIVE_URL\",\"token\":\"$DRIVE_TOKEN\"}" $API/api/settings | grep -q '"ok":true' && ok "ajustes aceptados" || bad "ajustes"
sync_api -d "{\"server_url\":\"$DRIVE_URL\",\"token\":\"mal\"}" $API/api/settings | grep -q '"ok":false' && ok "token inválido rechazado" || bad "token inválido aceptado"
setpoll 6
for d in "$A" "$B"; do sync_api -d "{\"local_path\":\"$d\",\"remote_id\":\"$RID\",\"remote_name\":\"e2e\"}" $API/api/links | grep -q '"ok":true' || bad "enlazar $d"; done
sync_api -d "{\"local_path\":\"$A/dentro\",\"remote_id\":\"$RID\",\"remote_name\":\"e2e\"}" $API/api/links | grep -q '"ok":false' && ok "enlace solapado rechazado" || bad "enlace solapado"

echo "# 1. carpeta nueva en A llega a Drive y a B"
mkdir -p "$A/sub/hondo" "$A/vacia"; echo hola > "$A/a.txt"; echo dentro > "$A/sub/b.txt"; echo hondo > "$A/sub/hondo/c.txt"; head -c 200000 /dev/urandom > "$A/bin.dat"
wait_for '[ -f "$B/sub/hondo/c.txt" ] && [ -d "$B/vacia" ] && [ -f "$B/bin.dat" ]' && ok "llegó a B" || bad "no llegó a B"
check "contenido idéntico" 'diff -r -x ".drive-sync*" "$A" "$B" >/dev/null'
check "sin carpetas duplicadas en Drive" '! tree | grep -q "(2)"'
check "carpeta vacía creada en Drive" 'tree | grep -qx vacia'

echo "# 2. editar en B llega a A"
sleep 3; echo "version dos" > "$B/a.txt"
wait_for 'grep -q "version dos" "$A/a.txt"' && ok "edición propagada" || bad "edición no propagada"

echo "# 3. borrar en B se borra en A y va a la papelera"
rm "$B/sub/b.txt"
wait_for '[ ! -e "$A/sub/b.txt" ]' && ok "borrado propagado a A" || bad "no se borró en A"
check "A lo conserva en su papelera local" 'find "$A/.drive-sync-trash" -name b.txt | grep -q .'
check "ya no está en el árbol de Drive" '! tree | grep -qx "sub/b.txt"'

echo "# 4. fichero subido directamente a Drive baja a las dos carpetas"
echo "desde la web" > "$W/web.txt"; drive -F "parent_id=$RID" -F "file=@$W/web.txt" "$DRIVE_URL/api/files" >/dev/null
wait_for '[ -f "$A/web.txt" ] && [ -f "$B/web.txt" ]' && ok "bajó a A y B" || bad "no bajó"

echo "# 5. conflicto con la máquina «apagada»: edición local y en Drive a la vez"
stop_daemon
sleep 3; echo "version local" > "$A/web.txt"
sleep 3; echo "version drive" > "$W/web.txt"
NID=$(drive "$DRIVE_URL/api/sync/tree?root_id=$RID" | python3 -c "import sys,json; print([i['id'] for i in json.load(sys.stdin)['items'] if i['path']=='web.txt'][0])")
drive -F "parent_id=$RID" -F "overwrite=true" -F "file=@$W/web.txt;filename=web.txt" "$DRIVE_URL/api/files" >/dev/null
start_daemon   # «arranca la máquina»: se pone al día sola
wait_for 'ls "$A" | grep -q conflicto' && ok "se guardó una copia de conflicto" || bad "sin copia de conflicto"
check "gana la edición más reciente (Drive)" 'grep -q "version drive" "$A/web.txt"'
check "la copia conserva la edición local" 'grep -q "version local" "$A"/web*conflicto*'

echo "# 5b. mover no vuelve a subir ni a bajar nada"
nid() { drive "$DRIVE_URL/api/sync/tree?root_id=$RID" | python3 -c "import sys,json; print([i['id'] for i in json.load(sys.stdin)['items'] if i['path']=='$1'][0])"; }
wait_for '[ -f "$A/bin.dat" ] && [ -f "$B/bin.dat" ] && [ -f "$B/sub/hondo/c.txt" ]' || bad "estado previo sin sincronizar"
sleep 8
ID1=$(nid bin.dat)
mkdir "$A/movido"; mv "$A/bin.dat" "$A/movido/bin.dat"
wait_for '[ -f "$B/movido/bin.dat" ] && [ ! -e "$B/bin.dat" ]' && ok "mover en A llegó a B" || bad "mover en A no llegó a B"
check "en Drive es el mismo fichero (no se resubió)" '[ "$(nid movido/bin.dat)" = "$ID1" ]'
check "ya no está en la ruta vieja de Drive" '! tree | grep -qx bin.dat'
INO=$(stat -c %i "$B/movido/bin.dat")
echo "otro" > "$A/renombrame.txt"; wait_for '[ -f "$B/renombrame.txt" ]' || bad "no llegó renombrame.txt"
ID2=$(nid renombrame.txt); sleep 3; mv "$A/renombrame.txt" "$A/renombrado.txt"
wait_for '[ -f "$B/renombrado.txt" ] && [ ! -e "$B/renombrame.txt" ]' && ok "renombrar en A llegó a B" || bad "renombrar no llegó"
check "renombrar conserva el fichero en Drive" '[ "$(nid renombrado.txt)" = "$ID2" ]'
INO2=$(stat -c %i "$B/renombrado.txt")
drive -X PATCH -d "name=otro-nombre.txt" "$DRIVE_URL/api/files/$ID2" >/dev/null
wait_for '[ -f "$B/otro-nombre.txt" ] && [ ! -e "$B/renombrado.txt" ]' && ok "mover en Drive llegó a B" || bad "mover en Drive no llegó a B"
check "B lo movió en disco (mismo inodo, no se bajó de nuevo)" '[ "$(stat -c %i "$B/otro-nombre.txt")" = "$INO2" ]'
check "A también" '[ -f "$A/otro-nombre.txt" ] && [ ! -e "$A/renombrado.txt" ]'

echo "# 5c. renombrar una carpeta entera es una sola llamada"
mkdir -p "$A/carp/sub"; for i in 1 2 3 4 5 6; do echo "contenido $i" > "$A/carp/f$i.txt"; done; echo prof > "$A/carp/sub/g.txt"
wait_for '[ -f "$B/carp/sub/g.txt" ] && [ -f "$B/carp/f6.txt" ]' || bad "carp no llegó a B"
sleep 8
CID=$(nid carp); FID=$(nid carp/f3.txt); GID=$(nid carp/sub/g.txt)
mv "$A/carp" "$A/carpeta-nueva"
wait_for '[ -f "$B/carpeta-nueva/sub/g.txt" ] && [ ! -e "$B/carp" ]' && ok "renombrar la carpeta en A llegó a B" || bad "no llegó a B"
check "la carpeta de Drive es la misma (un solo renombrado)" '[ "$(nid carpeta-nueva)" = "$CID" ]'
check "sus ficheros no se resubieron" '[ "$(nid carpeta-nueva/f3.txt)" = "$FID" ] && [ "$(nid carpeta-nueva/sub/g.txt)" = "$GID" ]'
check "no queda la ruta vieja en Drive" '! tree | grep -q "^carp/"'
check "el servicio cuenta 1 movimiento, no 7" 'grep -q "↑0 ↓0 borrados 0 conflictos 0 movidos 1" "$W/daemon.log"'

echo "# 6. salvaguarda: carpeta local vaciada de golpe"
mkdir "$W/aparte"; mv "$A"/* "$W/aparte/" 2>/dev/null; mv "$A"/.drive-sync-trash "$W/aparte/" 2>/dev/null
sleep 12
check "no se borra nada en Drive" 'tree | grep -qx "a.txt"'
check "se avisa del motivo" 'sync_api $API/api/status | grep -q "vacía"'

echo
[ $FAILS -ne 0 ] && { echo "--- log del servicio:"; tail -30 "$W/daemon.log" | cut -c1-220; }
[ $FAILS -eq 0 ] && echo "TODO BIEN" || echo "$FAILS fallos"
exit $FAILS
