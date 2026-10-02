#!/usr/bin/env bash
#
# Instalación de Drive en Ubuntu Server (22.04 / 24.04).
#
#   sudo bash deploy/install.sh
#
# Deja el servicio escuchando en 127.0.0.1:8000 bajo systemd. El TLS (nginx) se
# configura aparte con deploy/setup-tls.sh.
#
# Hace falta el binario de Lux. De dónde sale, por orden:
#
#   1. LUX_BIN=/ruta/a/lux           un binario ya compilado (se copia).
#   2. un fichero "lux" junto a deploy/ (lo envía deploy.sh --lux-bin).
#   3. el "lux" que ya esté en el PATH.
#   4. LUX_SRC=/ruta/al/repositorio  se compila ahí (cmake) e instala.

set -euo pipefail

APP_DIR=/opt/drive
DATA_DIR=/var/lib/drive
ENV_DIR=/etc/drive
SERVICE_USER=drive

if [[ $EUID -ne 0 ]]; then
    echo "Ejecútalo como root (sudo)." >&2
    exit 1
fi

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "==> Instalando dependencias del sistema"
apt-get update -qq
# Bibliotecas que enlaza el binario de Lux, nginx para el TLS, openssl (el
# instalador usa para el secreto), libargon2 (hashes de contraseña), tzdata
# (zonas horarias) y qrencode, opcional, para dibujar el QR del segundo factor.
apt-get install -y --no-install-recommends \
    ca-certificates curl openssl nginx qrencode \
    libsqlite3-0 libcurl4 libwebp7 libpng16-16 libjpeg-turbo8 libcairo2 \
    libjemalloc2 libpq5 libmysqlclient21 zlib1g libargon2-1 tzdata

echo "==> Comprobando el binario de Lux"
if [[ -n "${LUX_BIN:-}" ]]; then
    install -m 755 "$LUX_BIN" /usr/local/bin/lux
elif [[ -f "$SOURCE_DIR/lux" ]]; then
    install -m 755 "$SOURCE_DIR/lux" /usr/local/bin/lux
elif command -v lux >/dev/null 2>&1; then
    echo "    usando $(command -v lux)"
elif [[ -n "${LUX_SRC:-}" ]]; then
    echo "    compilando Lux en $LUX_SRC"
    apt-get install -y --no-install-recommends build-essential cmake \
        libsqlite3-dev libpq-dev libmysqlclient-dev libcairo2-dev \
        libcurl4-openssl-dev libjemalloc-dev libssl-dev libwebp-dev \
        libpng-dev libjpeg-dev zlib1g-dev
    cmake -S "$LUX_SRC" -B "$LUX_SRC/build" -DCMAKE_BUILD_TYPE=Release
    cmake --build "$LUX_SRC/build" -j"$(nproc)"
    install -m 755 "$LUX_SRC/build/lux" /usr/local/bin/lux
else
    echo "No hay binario de Lux. Pásalo con LUX_BIN=/ruta/lux o LUX_SRC=/ruta/repo," >&2
    echo "o instálalo antes en el PATH." >&2
    exit 1
fi

echo "==> Creando el usuario de servicio"
if ! id "$SERVICE_USER" &>/dev/null; then
    adduser --system --group --home "$DATA_DIR" --no-create-home "$SERVICE_USER"
fi

echo "==> Copiando la aplicación a $APP_DIR"
mkdir -p "$APP_DIR"
# Se borra el código anterior en vez de copiar encima: si no, un fichero que
# se haya eliminado del proyecto seguiría vivo en el servidor.
rm -rf "$APP_DIR/app"
cp -r "$SOURCE_DIR/app" "$APP_DIR/"
cp "$SOURCE_DIR/deploy/run.sh" "$APP_DIR/run.sh"
chmod 755 "$APP_DIR/run.sh"
# Los scripts de operación quedan en el servidor: setup-tls.sh y la plantilla
# de nginx hacen falta después de instalar, no sólo durante.
rm -rf "$APP_DIR/deploy"
cp -r "$SOURCE_DIR/deploy" "$APP_DIR/deploy"
rm -f "$APP_DIR/deploy/lux"
mkdir -p "$APP_DIR/config"
if [[ ! -f "$APP_DIR/config/config.json" ]]; then
    cp "$SOURCE_DIR/config/config.json" "$APP_DIR/config/config.json"
else
    echo "    config.json ya existe: se conserva el actual."
fi

echo "==> Preparando los directorios de datos"
mkdir -p "$DATA_DIR/blobs" "$DATA_DIR/thumbs" "$ENV_DIR"
chown -R "$SERVICE_USER:$SERVICE_USER" "$DATA_DIR"
chmod 750 "$DATA_DIR"

echo "==> Generando el secreto de firma"
if [[ ! -f "$ENV_DIR/drive.env" ]]; then
    SECRET="$(openssl rand -base64 48 | tr '+/' '-_' | tr -d '=\n')"
    cat > "$ENV_DIR/drive.env" <<ENV
# Configuración sensible de Drive. Las variables DRIVE_* tienen prioridad sobre
# config.json y sobre los ajustes del panel de administración.
DRIVE_SECURITY__SECRET_KEY=$SECRET
DRIVE_DB=$DATA_DIR/drive.db
DRIVE_STORAGE__ROOT=$DATA_DIR/blobs

# Puerto de Lux (lo lee run.sh). Escucha sólo en el bucle local; nginx lo
# expone (deploy/setup-tls.sh).
BIND_PORT=8000
ENV
    chmod 640 "$ENV_DIR/drive.env"
    chown root:"$SERVICE_USER" "$ENV_DIR/drive.env"
    echo "    secreto generado en $ENV_DIR/drive.env"
else
    echo "    $ENV_DIR/drive.env ya existe: se conserva."
fi

chown -R root:"$SERVICE_USER" "$APP_DIR"
chmod -R g+rX "$APP_DIR"

echo "==> Instalando el servicio"
cp "$SOURCE_DIR/deploy/drive.service" /etc/systemd/system/drive.service
cp "$SOURCE_DIR/deploy/drive-cli.sh" /usr/local/bin/drive-cli
chmod 755 /usr/local/bin/drive-cli
systemctl daemon-reload
systemctl enable --now drive.service

PUERTO="$(grep -E '^BIND_PORT=' "$ENV_DIR/drive.env" | cut -d= -f2)"
PUERTO="${PUERTO:-8000}"

# El esquema de la base de datos se crea con la primera petición.
for _ in $(seq 1 15); do
    sleep 1
    if curl -fsS --max-time 3 "http://127.0.0.1:$PUERTO/healthz" >/dev/null 2>&1; then
        break
    fi
done

echo
if curl -fsS --max-time 3 "http://127.0.0.1:$PUERTO/healthz" >/dev/null 2>&1; then
    echo "Drive responde en http://127.0.0.1:$PUERTO"
else
    echo "El servicio no responde todavía. Revisa:" >&2
    echo "  systemctl status drive; journalctl -u drive -n 50" >&2
fi
echo
echo "Crea el primer administrador con:"
echo "  sudo drive-cli createuser TUUSUARIO --role admin"
echo "(o abre la web: la primera cuenta que se registre será administradora)"
echo
echo "Ahora sólo es accesible desde la propia máquina y sin cifrar. Para exponerlo:"
echo "  sudo bash $APP_DIR/deploy/setup-tls.sh letsencrypt tu.dominio tu@correo 443"
echo "  sudo bash $APP_DIR/deploy/setup-tls.sh self-signed 192.168.1.20 4023"
