#!/usr/bin/env bash
#
# Despliega Drive en un servidor remoto por SSH.
#
#   ./deploy/deploy.sh usuario@servidor
#   ./deploy/deploy.sh -p 2222 -i ~/.ssh/id_drive root@192.168.1.10
#   ./deploy/deploy.sh --update usuario@servidor
#   ./deploy/deploy.sh --lux-bin ~/Github/Lux/build/lux usuario@servidor
#
# Empaqueta el proyecto, lo copia por SSH y ejecuta el instalador allí. Si ya
# hay una instalación, actualiza sólo el código: la base de datos, los ficheros
# subidos, config.json y el secreto de firma se conservan siempre.

set -euo pipefail

SSH_PORT=22
SSH_KEY=""
REMOTE_STAGING="/tmp/drive-deploy"
HEALTH_PORT=8000
MODE="auto"          # auto | install | update
LUX_BIN=""           # binario de Lux que se envía con el paquete (opcional)
ASSUME_YES=0
DRY_RUN=0
TARGET=""

rojo()  { printf '\033[31m%s\033[0m\n' "$*"; }
verde() { printf '\033[32m%s\033[0m\n' "$*"; }
gris()  { printf '\033[90m%s\033[0m\n' "$*"; }

uso() {
    cat <<'FIN'
Uso: deploy.sh [opciones] [usuario@]servidor

Opciones:
  -p, --port PUERTO     Puerto SSH (por defecto 22)
  -i, --key FICHERO     Clave privada SSH
  -d, --staging RUTA    Directorio temporal en el servidor (/tmp/drive-deploy)
      --health-port N   Puerto donde comprobar /healthz (8000)
      --lux-bin FICHERO Binario de Lux a instalar en el servidor (si no, el
                        servidor debe tener ya "lux" en el PATH)
      --install         Forzar instalación completa
      --update          Forzar actualización de código solamente
  -y, --yes             No pedir confirmación
  -n, --dry-run         Mostrar lo que se haría, sin tocar nada
  -h, --help            Esta ayuda

Requisitos en el servidor: Ubuntu con acceso sudo para el usuario indicado.
FIN
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        -p|--port)      SSH_PORT="$2"; shift 2 ;;
        -i|--key)       SSH_KEY="$2"; shift 2 ;;
        -d|--staging)   REMOTE_STAGING="$2"; shift 2 ;;
        --health-port)  HEALTH_PORT="$2"; shift 2 ;;
        --lux-bin)      LUX_BIN="$2"; shift 2 ;;
        --install)      MODE="install"; shift ;;
        --update)       MODE="update"; shift ;;
        -y|--yes)       ASSUME_YES=1; shift ;;
        -n|--dry-run)   DRY_RUN=1; shift ;;
        -h|--help)      uso; exit 0 ;;
        -*)             rojo "Opción desconocida: $1"; uso; exit 1 ;;
        *)
            if [[ -n "$TARGET" ]]; then
                rojo "Sólo se admite un servidor de destino."
                exit 1
            fi
            TARGET="$1"; shift ;;
    esac
done

if [[ -z "$TARGET" ]]; then
    rojo "Falta el servidor de destino."
    uso
    exit 1
fi

for herramienta in ssh scp tar; do
    if ! command -v "$herramienta" >/dev/null 2>&1; then
        rojo "Falta '$herramienta' en este equipo."
        exit 1
    fi
done

RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$RAIZ"

for necesario in app/app.lux config/config.json deploy/install.sh; do
    if [[ ! -e "$necesario" ]]; then
        rojo "No parece la raíz del proyecto: falta $necesario"
        exit 1
    fi
done

if [[ -n "$LUX_BIN" && ! -x "$LUX_BIN" ]]; then
    rojo "No se encuentra el binario de Lux (o no es ejecutable): $LUX_BIN"
    exit 1
fi

SSH_OPTS=(-p "$SSH_PORT" -o ConnectTimeout=10)
SCP_OPTS=(-P "$SSH_PORT" -o ConnectTimeout=10)
if [[ -n "$SSH_KEY" ]]; then
    SSH_OPTS+=(-i "$SSH_KEY")
    SCP_OPTS+=(-i "$SSH_KEY")
fi

remoto()    { ssh "${SSH_OPTS[@]}" "$TARGET" "$@"; }
remoto_tty() { ssh -t "${SSH_OPTS[@]}" "$TARGET" "$@"; }

# --------------------------------------------------------------------------- #
# Comprobaciones previas
# --------------------------------------------------------------------------- #

echo
gris "Conectando con $TARGET (puerto $SSH_PORT)…"
if ! remoto "true" 2>/dev/null; then
    rojo "No se puede conectar por SSH con $TARGET."
    echo "Comprueba el host, el usuario, el puerto y tu clave."
    exit 1
fi

SO_REMOTO="$(remoto ". /etc/os-release 2>/dev/null && echo \$PRETTY_NAME || uname -s")"

YA_INSTALADO=0
if remoto "test -f /opt/drive/app/app.lux"; then
    YA_INSTALADO=1
fi
if remoto "test -d /opt/drive/venv"; then
    rojo "Hay una instalación de la versión Python en /opt/drive (venv/)."
    echo "Esta versión no migra sus datos (otro esquema y otros hashes de contraseña):"
    echo "instala en otra máquina o desinstala antes la anterior. Ver README.md."
    exit 1
fi

if [[ "$MODE" == "auto" ]]; then
    if [[ $YA_INSTALADO -eq 1 ]]; then MODE="update"; else MODE="install"; fi
fi

if [[ "$MODE" == "update" && $YA_INSTALADO -eq 0 ]]; then
    rojo "Se ha pedido --update pero no hay ninguna instalación en /opt/drive."
    exit 1
fi

# --------------------------------------------------------------------------- #
# Resumen y confirmación
# --------------------------------------------------------------------------- #

echo
echo "  Destino     : $TARGET:$SSH_PORT"
echo "  Sistema     : $SO_REMOTO"
if [[ "$MODE" == "install" ]]; then
    echo "  Operación   : instalación completa"
    echo "                paquetes del sistema, usuario 'drive', /opt/drive,"
    echo "                /var/lib/drive, secreto de firma y servicio systemd"
else
    echo "  Operación   : actualización de código"
    echo "                se conservan base de datos, ficheros, config.json y secreto"
fi
if [[ -n "$LUX_BIN" ]]; then
    echo "  Binario Lux : $LUX_BIN (se envía y se instala)"
fi
echo "  Temporal    : $TARGET:$REMOTE_STAGING"
echo

if [[ $DRY_RUN -eq 1 ]]; then
    verde "Simulación: no se ha modificado nada."
    exit 0
fi

if [[ $ASSUME_YES -eq 0 ]]; then
    read -r -p "¿Continuar? [s/N] " respuesta
    case "$respuesta" in
        s|S|si|Si|SI|sí|Sí) ;;
        *) echo "Cancelado."; exit 0 ;;
    esac
fi

# --------------------------------------------------------------------------- #
# Empaquetado y envío
# --------------------------------------------------------------------------- #

TMP_LOCAL="$(mktemp -d)"
trap 'rm -rf "$TMP_LOCAL"' EXIT
PAQUETE="$TMP_LOCAL/drive.tar.gz"

echo
gris "==> Empaquetando el proyecto"
if [[ -n "$LUX_BIN" ]]; then
    cp "$LUX_BIN" "$TMP_LOCAL/lux"
fi
tar czf "$PAQUETE" \
    --exclude='data' \
    --exclude='.lux-native' \
    app config deploy README.md needed.md \
    ${LUX_BIN:+-C "$TMP_LOCAL" lux}
gris "    $(du -h "$PAQUETE" | cut -f1)"

gris "==> Enviando a $TARGET"
remoto "rm -rf '$REMOTE_STAGING' && mkdir -p '$REMOTE_STAGING'"
scp "${SCP_OPTS[@]}" -q "$PAQUETE" "$TARGET:$REMOTE_STAGING/drive.tar.gz"
remoto "cd '$REMOTE_STAGING' && tar xzf drive.tar.gz && rm drive.tar.gz"

# --------------------------------------------------------------------------- #
# Ejecución remota
# --------------------------------------------------------------------------- #

echo
if [[ "$MODE" == "install" ]]; then
    gris "==> Instalando (puede pedirte la contraseña de sudo)"
    remoto_tty "cd '$REMOTE_STAGING' && sudo bash deploy/install.sh"
else
    gris "==> Actualizando (puede pedirte la contraseña de sudo)"
    remoto_tty "cd '$REMOTE_STAGING' && sudo bash deploy/remote-update.sh $HEALTH_PORT"
fi

remoto "rm -rf '$REMOTE_STAGING'"

echo
verde "Despliegue terminado."
if [[ "$MODE" == "install" ]]; then
    cat <<FIN

Siguientes pasos, en el servidor:

  # Primer administrador
  sudo drive-cli createuser TUNOMBRE --role admin

  # Revisar la configuración
  sudo drive-cli check

Ahora escucha en 127.0.0.1:8000, sin cifrar y sin acceso desde fuera. Lux no
sirve TLS: para exponerlo se pone nginx delante (lo instala el script):

  sudo bash /opt/drive/deploy/setup-tls.sh letsencrypt tu.dominio tu@correo 443
  sudo bash /opt/drive/deploy/setup-tls.sh self-signed 192.168.1.20 4023
FIN
fi
