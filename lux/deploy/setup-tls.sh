#!/usr/bin/env bash
#
# Pone nginx con HTTPS delante de Drive.
#
#   sudo bash setup-tls.sh letsencrypt drive.ejemplo.com tu@correo.com [PUERTO]
#   sudo bash setup-tls.sh self-signed  192.168.1.20               [PUERTO]
#
# Lux sólo habla HTTP sin cifrar (el TLS "pertenece al proxy inverso"), así que
# a diferencia de la versión Python, que servía TLS ella misma, aquí nginx es
# imprescindible para exponer Drive. El puerto por defecto es 443; con uno alto
# (4023, por ejemplo) no hacen falta privilegios especiales en el cortafuegos
# del operador.
#
# Qué hace:
#   1. Instala nginx (y certbot con letsencrypt).
#   2. Obtiene el certificado (Let's Encrypt o autofirmado).
#   3. Genera /etc/nginx/sites-available/drive a partir de deploy/nginx.conf.
#   4. Ajusta /etc/drive/drive.env: base_url, cookie Secure y proxy de confianza.
#   5. Reinicia Drive y recarga nginx.

set -euo pipefail

MODO="${1:-}"
DOMINIO="${2:-}"
CORREO="${3:-}"
PUERTO="${4:-443}"

APP_DIR=/opt/drive
ENV_FILE=/etc/drive/drive.env
TLS_DIR=/etc/drive/tls
SITE=/etc/nginx/sites-available/drive
PLANTILLA="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/nginx.conf"

if [[ $EUID -ne 0 ]]; then
    echo "Ejecútalo como root (sudo)." >&2
    exit 1
fi
if [[ -z "$MODO" || -z "$DOMINIO" ]]; then
    echo "Uso: setup-tls.sh {letsencrypt|self-signed} DOMINIO_O_IP [CORREO] [PUERTO]" >&2
    exit 1
fi
if [[ ! -f "$ENV_FILE" ]]; then
    echo "No existe $ENV_FILE. Instala primero con install.sh." >&2
    exit 1
fi

# Con self-signed el tercer argumento es en realidad el puerto.
if [[ "$MODO" == "self-signed" && -n "$CORREO" && "$CORREO" =~ ^[0-9]+$ ]]; then
    PUERTO="$CORREO"
    CORREO=""
fi

set_env() {
    local clave="$1" valor="$2"
    if grep -q "^${clave}=" "$ENV_FILE"; then
        sed -i "s|^${clave}=.*|${clave}=${valor}|" "$ENV_FILE"
    else
        echo "${clave}=${valor}" >> "$ENV_FILE"
    fi
}

APP_PORT="$(grep -E '^BIND_PORT=' "$ENV_FILE" | cut -d= -f2 || true)"
APP_PORT="${APP_PORT:-8000}"

echo "==> Instalando nginx"
apt-get update -qq
apt-get install -y --no-install-recommends nginx openssl

# Renderiza la plantilla de nginx con los valores de este servidor.
generar_sitio() {  # cert clave hsts
    local cert="$1" clave="$2" hsts="$3"
    sed -e "s|__DOMINIO__|$DOMINIO|g" \
        -e "s|__PUERTO__|$PUERTO|g" \
        -e "s|__CERT__|$cert|g" \
        -e "s|__CLAVE__|$clave|g" \
        -e "s|__APP_PORT__|$APP_PORT|g" \
        -e "s|__HSTS__|$hsts|g" \
        "$PLANTILLA" > "$SITE"
}

activar_sitio() {
    ln -sf "$SITE" /etc/nginx/sites-enabled/drive
    nginx -t
    systemctl enable --now nginx
    systemctl reload nginx
}

case "$MODO" in
# --------------------------------------------------------------------------- #
letsencrypt)
    if [[ -z "$CORREO" ]]; then
        echo "Con letsencrypt hace falta un correo para los avisos de caducidad." >&2
        exit 1
    fi
    if [[ "$PUERTO" == "80" ]]; then
        echo "El puerto 80 debe quedar libre para validar el certificado." >&2
        echo "Usa otro puerto para Drive (443 o uno alto)." >&2
        exit 1
    fi

    echo "==> Instalando certbot"
    apt-get install -y --no-install-recommends certbot

    # El reto HTTP-01 se valida SIEMPRE contra el puerto 80, sin excepción.
    # Se levanta un sitio provisional que sólo responde al reto.
    mkdir -p /var/www/html
    cat > "$SITE" <<ACME
server {
    listen 80;
    listen [::]:80;
    server_name $DOMINIO;
    location /.well-known/acme-challenge/ { root /var/www/html; }
    location / { return 404; }
}
ACME
    activar_sitio

    echo "==> Obteniendo el certificado para $DOMINIO"
    echo "    El puerto 80 debe ser accesible desde Internet ahora mismo."
    certbot certonly --webroot -w /var/www/html \
        --non-interactive --agree-tos --email "$CORREO" -d "$DOMINIO"

    echo "==> Instalando el hook de renovación"
    mkdir -p /etc/letsencrypt/renewal-hooks/deploy
    cat > /etc/letsencrypt/renewal-hooks/deploy/drive.sh <<'HOOK'
#!/usr/bin/env bash
# Generado por setup-tls.sh: nginx lee el certificado al arrancar o recargar.
systemctl reload nginx
HOOK
    chmod 700 /etc/letsencrypt/renewal-hooks/deploy/drive.sh

    ORIGEN="/etc/letsencrypt/live/$DOMINIO"
    generar_sitio "$ORIGEN/fullchain.pem" "$ORIGEN/privkey.pem" \
        'add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;'
    # El 80 se queda sólo para renovar el certificado y redirigir.
    cat >> "$SITE" <<REDIR

server {
    listen 80;
    listen [::]:80;
    server_name $DOMINIO;
    location /.well-known/acme-challenge/ { root /var/www/html; }
    location / { return 301 https://\$host$( [[ "$PUERTO" == "443" ]] || printf ':%s' "$PUERTO" )\$request_uri; }
}
REDIR
    ESQUEMA="https"
    ;;

# --------------------------------------------------------------------------- #
self-signed)
    echo "==> Generando un certificado autofirmado para $DOMINIO"
    mkdir -p "$TLS_DIR"
    chmod 750 "$TLS_DIR"
    # Con SAN, para que los navegadores lo acepten al añadir la excepción.
    TIPO_SAN="DNS"
    if [[ "$DOMINIO" =~ ^[0-9.]+$ ]]; then
        TIPO_SAN="IP"
    fi
    openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
        -keyout "$TLS_DIR/key.pem" -out "$TLS_DIR/cert.pem" \
        -subj "/CN=$DOMINIO" \
        -addext "subjectAltName=${TIPO_SAN}:${DOMINIO}" >/dev/null 2>&1
    chmod 600 "$TLS_DIR/key.pem"
    chmod 644 "$TLS_DIR/cert.pem"

    echo "    Certificado autofirmado: el navegador avisará la primera vez."
    echo "    Vale para una red interna; para Internet usa el modo letsencrypt."
    generar_sitio "$TLS_DIR/cert.pem" "$TLS_DIR/key.pem" ""
    ESQUEMA="https"
    ;;

*)
    echo "Modo desconocido: $MODO (usa letsencrypt o self-signed)" >&2
    exit 1
    ;;
esac

if ss -ltn 2>/dev/null | grep -E ":$PUERTO\\b" | grep -qv nginx; then
    echo "Aviso: algo más puede estar escuchando ya en el puerto $PUERTO." >&2
fi
activar_sitio

# --------------------------------------------------------------------------- #
echo "==> Configurando Drive"
# base_url tiene que coincidir con lo que teclea el usuario, o los enlaces
# compartidos saldrán mal.
BASE_URL="$ESQUEMA://$DOMINIO"
if [[ "$PUERTO" != "443" ]]; then
    BASE_URL="$BASE_URL:$PUERTO"
fi
set_env "DRIVE_APP__BASE_URL" "$BASE_URL"
# Con HTTPS, la cookie de sesión no debe viajar nunca en claro.
set_env "DRIVE_SECURITY__SESSION__COOKIE_SECURE" "true"
# nginx es el proxy: sus cabeceras X-Forwarded-* son de fiar y las de cualquier
# otro no.
set_env "DRIVE_SERVER__BEHIND_PROXY" "true"
set_env "DRIVE_SERVER__TRUSTED_PROXIES" '["127.0.0.1/32","::1/128"]'
chmod 640 "$ENV_FILE"
chown root:drive "$ENV_FILE"

systemctl restart drive
sleep 2

echo
if systemctl is-active --quiet drive && systemctl is-active --quiet nginx; then
    echo "Listo. Drive responde en $BASE_URL"
    echo
    echo "Abre el puerto en el cortafuegos si aún no lo has hecho:"
    echo "    sudo ufw allow $PUERTO/tcp"
    if [[ "$MODO" == "letsencrypt" ]]; then
        echo "    sudo ufw allow 80/tcp     # sólo para renovar el certificado"
        echo
        echo "La renovación es automática (certbot.timer); comprueba que funciona con:"
        echo "    sudo certbot renew --dry-run"
    fi
else
    echo "Algún servicio no ha arrancado. Revisa:" >&2
    journalctl -u drive -n 30 --no-pager >&2 || true
    systemctl status nginx --no-pager >&2 || true
    exit 1
fi
