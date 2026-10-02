#!/usr/bin/env bash
# Actualiza Drive de escritorio desde GitHub. Lo lanza la propia app (botón
# «Actualizar»), como el usuario, no como root:
#
#   git pull → compilar → pkexec install.sh
#
# Sólo la instalación necesita permisos: pkexec muestra el diálogo de contraseña
# del sistema, así la contraseña nunca pasa por la app. La salida va al registro
# que la ventana muestra; la última línea es «==> LISTO» o «==> ERROR».

# Todo dentro de main: `git pull` puede reescribir este fichero mientras corre, y
# bash lee los scripts a trozos.
main() {
  set -euo pipefail
  trap 'echo "==> ERROR"' ERR
  local here repo
  here=$(cd "$(dirname "$0")/.." && pwd)
  repo=$(cd "$here/.." && pwd)

  echo "==> Descargando cambios"
  git -C "$repo" pull --ff-only

  echo "==> Compilando"
  cmake -S "$here" -B "$here/build" -DCMAKE_BUILD_TYPE=Release >/dev/null
  cmake --build "$here/build" --target lux-bin drive-sync -j"$(nproc)"

  echo "==> Instalando (pide tu contraseña)"
  pkexec "$here/deploy/install.sh" --user "$(id -un)"

  echo "==> LISTO"
}
main "$@"; exit
