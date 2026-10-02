#!/usr/bin/env bash
#
# Instalación de Drive en Ubuntu Server (22.04 / 24.04).
#
#   sudo bash deploy/install.sh
#
# Deja el servicio escuchando en 127.0.0.1:8000 bajo systemd. El proxy inverso
# con TLS se configura aparte con deploy/nginx.conf.

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
apt-get install -y --no-install-recommends \
    python3 python3-venv python3-dev build-essential \
    libjpeg-dev zlib1g-dev nginx

echo "==> Creando el usuario de servicio"
if ! id "$SERVICE_USER" &>/dev/null; then
    adduser --system --group --home "$DATA_DIR" --no-create-home "$SERVICE_USER"
fi

echo "==> Copiando la aplicación a $APP_DIR"
mkdir -p "$APP_DIR"
# Se borra el código anterior en vez de copiar encima: si no, un módulo que se
# haya eliminado del proyecto seguiría vivo en el servidor.
rm -rf "$APP_DIR/app"
cp -r "$SOURCE_DIR/app" "$SOURCE_DIR/requirements.txt" "$APP_DIR/"
cp "$SOURCE_DIR/deploy/run.sh" "$APP_DIR/run.sh"
chmod 755 "$APP_DIR/run.sh"
# Los scripts de operación quedan en el servidor: setup-tls.sh y las plantillas
# de nginx hacen falta después de instalar, no sólo durante.
rm -rf "$APP_DIR/deploy"
cp -r "$SOURCE_DIR/deploy" "$APP_DIR/deploy"
mkdir -p "$APP_DIR/config"
if [[ ! -f "$APP_DIR/config/config.yaml" ]]; then
    cp "$SOURCE_DIR/config/config.yaml" "$APP_DIR/config/config.yaml"
else
    echo "    config.yaml ya existe: se conserva el actual."
fi

echo "==> Creando el entorno virtual"
python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"

echo "==> Preparando los directorios de datos"
mkdir -p "$DATA_DIR/blobs" "$DATA_DIR/thumbs" "$ENV_DIR"
chown -R "$SERVICE_USER:$SERVICE_USER" "$DATA_DIR"
chmod 750 "$DATA_DIR"

echo "==> Generando el secreto de firma"
if [[ ! -f "$ENV_DIR/drive.env" ]]; then
    SECRET="$("$APP_DIR/venv/bin/python" -c 'import secrets; print(secrets.token_urlsafe(64))')"
    cat > "$ENV_DIR/drive.env" <<EOF
# Configuración sensible de Drive. Tiene prioridad sobre config.yaml.
DRIVE_SECURITY__SECRET_KEY=$SECRET
DRIVE_DATABASE__URL=sqlite:///$DATA_DIR/drive.db
DRIVE_STORAGE__ROOT=$DATA_DIR/blobs

# --- Dónde y cómo escucha el proceso --------------------------------------
# Estas variables las lee run.sh, no config.yaml. Para servir HTTPS
# directamente sin proxy, usa deploy/setup-tls.sh: rellena TLS_CERT y TLS_KEY
# y ajusta lo demás por ti.
#
#   BIND_HOST=127.0.0.1  sólo local, con un proxy delante
#   BIND_HOST=0.0.0.0    expuesto a la red directamente
BIND_HOST=127.0.0.1
BIND_PORT=8000
BIND_WORKERS=2
# 1 si hay nginx/traefik delante. Con 0 se ignoran las cabeceras
# X-Forwarded-*, que es lo correcto al no haber proxy: si no, cualquiera
# podría falsear su IP y aparentar venir de una red de confianza.
BEHIND_PROXY=1
TRUSTED_PROXY=127.0.0.1
# Rutas del certificado en PEM. Vacías = HTTP sin cifrar.
TLS_CERT=
TLS_KEY=
EOF
    chmod 600 "$ENV_DIR/drive.env"
    chown root:"$SERVICE_USER" "$ENV_DIR/drive.env"
    chmod 640 "$ENV_DIR/drive.env"
    echo "    secreto generado en $ENV_DIR/drive.env"
else
    echo "    $ENV_DIR/drive.env ya existe: se conserva."
fi

chown -R root:"$SERVICE_USER" "$APP_DIR"
chmod -R g+rX "$APP_DIR"

echo "==> Inicializando la base de datos"
cd "$APP_DIR"
set -a; source "$ENV_DIR/drive.env"; set +a
sudo -u "$SERVICE_USER" \
    env DRIVE_SECURITY__SECRET_KEY="$DRIVE_SECURITY__SECRET_KEY" \
        DRIVE_DATABASE__URL="$DRIVE_DATABASE__URL" \
        DRIVE_STORAGE__ROOT="$DRIVE_STORAGE__ROOT" \
        DRIVE_CONFIG="$APP_DIR/config/config.yaml" \
    "$APP_DIR/venv/bin/python" -m app.cli init

echo "==> Instalando el servicio"
cp "$SOURCE_DIR/deploy/drive.service" /etc/systemd/system/drive.service
cp "$SOURCE_DIR/deploy/drive-cli.sh" /usr/local/bin/drive-cli
chmod 755 /usr/local/bin/drive-cli
systemctl daemon-reload
systemctl enable --now drive.service

PUERTO="$(grep -E '^BIND_PORT=' "$ENV_DIR/drive.env" | cut -d= -f2)"

echo
echo "Listo. Comprobaciones:"
echo "  systemctl status drive"
echo "  curl -s localhost:${PUERTO:-8000}/healthz"
echo
echo "Crea el primer administrador con:"
echo "  sudo drive-cli createuser TUUSUARIO --role admin"
echo
echo "Ahora mismo escucha en 127.0.0.1:${PUERTO:-8000}, sin cifrar y sin acceso"
echo "desde fuera. Elige cómo exponerlo:"
echo
echo "  a) HTTPS directo, sin proxy (lo más simple):"
echo "     sudo bash $APP_DIR/deploy/setup-tls.sh letsencrypt tu.dominio tu@correo.com 443"
echo "     sudo bash $APP_DIR/deploy/setup-tls.sh self-signed 192.168.1.20 4023"
echo
echo "  b) Con nginx delante: $APP_DIR/deploy/nginx.conf y BEHIND_PROXY=1"
echo "     en $ENV_DIR/drive.env"
