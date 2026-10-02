#!/usr/bin/env bash
#
# Pone HTTPS directamente en la aplicación, sin nginx ni ningún otro proxy.
#
#   sudo bash setup-tls.sh letsencrypt drive.ejemplo.com tu@correo.com [PUERTO]
#   sudo bash setup-tls.sh self-signed  192.168.1.20               [PUERTO]
#
# El puerto por defecto es 443. Con un puerto alto (4023, por ejemplo) no hacen
# falta privilegios especiales, pero la unidad ya concede CAP_NET_BIND_SERVICE
# para que el 443 funcione igualmente sin correr como root.
#
# Qué hace:
#   1. Obtiene el certificado (Let's Encrypt o autofirmado).
#   2. Deja una copia en /etc/drive/tls legible por el usuario del servicio.
#      Los originales de Let's Encrypt son sólo de root, y el servicio no corre
#      como root: sin esta copia, uvicorn no podría abrir la clave.
#   3. Instala un hook para que cada renovación repita el paso 2 y reinicie.
#   4. Configura /etc/drive/drive.env y reinicia el servicio.

set -euo pipefail

MODO="${1:-}"
DOMINIO="${2:-}"
CORREO="${3:-}"
PUERTO="${4:-443}"

ENV_FILE=/etc/drive/drive.env
TLS_DIR=/etc/drive/tls
SERVICE_USER=drive

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

mkdir -p "$TLS_DIR"
chown root:"$SERVICE_USER" "$TLS_DIR"
chmod 750 "$TLS_DIR"

case "$MODO" in
# --------------------------------------------------------------------------- #
letsencrypt)
    if [[ -z "$CORREO" ]]; then
        echo "Con letsencrypt hace falta un correo para los avisos de caducidad." >&2
        exit 1
    fi

    echo "==> Instalando certbot"
    apt-get update -qq
    apt-get install -y --no-install-recommends certbot

    # El reto HTTP-01 se valida SIEMPRE contra el puerto 80, sin excepción.
    # Como la aplicación escucha en otro puerto, certbot puede ocuparlo él
    # mismo en modo standalone durante los segundos que dura la validación.
    if [[ "$PUERTO" == "80" ]]; then
        echo "El puerto 80 debe quedar libre para validar el certificado." >&2
        echo "Usa otro puerto para la aplicación (443 o uno alto)." >&2
        exit 1
    fi
    if ss -ltn 2>/dev/null | grep -q ':80 '; then
        echo "Hay algo escuchando en el puerto 80; certbot lo necesita libre." >&2
        echo "Párala e inténtalo de nuevo (systemctl stop nginx, por ejemplo)." >&2
        exit 1
    fi

    echo "==> Obteniendo el certificado para $DOMINIO"
    echo "    El puerto 80 debe ser accesible desde Internet ahora mismo."
    certbot certonly --standalone \
        --non-interactive --agree-tos \
        --email "$CORREO" \
        -d "$DOMINIO"

    ORIGEN="/etc/letsencrypt/live/$DOMINIO"

    echo "==> Instalando el hook de renovación"
    mkdir -p /etc/letsencrypt/renewal-hooks/deploy
    cat > /etc/letsencrypt/renewal-hooks/deploy/drive.sh <<HOOK
#!/usr/bin/env bash
# Generado por setup-tls.sh. Copia el certificado renovado a un sitio que el
# usuario del servicio pueda leer y reinicia Drive para que lo cargue: uvicorn
# lee el certificado sólo al arrancar.
set -e
install -o root -g $SERVICE_USER -m 640 "$ORIGEN/fullchain.pem" "$TLS_DIR/cert.pem"
install -o root -g $SERVICE_USER -m 640 "$ORIGEN/privkey.pem"   "$TLS_DIR/key.pem"
systemctl restart drive
HOOK
    chmod 700 /etc/letsencrypt/renewal-hooks/deploy/drive.sh

    install -o root -g "$SERVICE_USER" -m 640 "$ORIGEN/fullchain.pem" "$TLS_DIR/cert.pem"
    install -o root -g "$SERVICE_USER" -m 640 "$ORIGEN/privkey.pem"   "$TLS_DIR/key.pem"

    ESQUEMA="https"
    ;;

# --------------------------------------------------------------------------- #
self-signed)
    echo "==> Generando un certificado autofirmado para $DOMINIO"
    # Con SAN, para que los navegadores lo acepten al añadir la excepción.
    TIPO_SAN="DNS"
    if [[ "$DOMINIO" =~ ^[0-9.]+$ ]]; then
        TIPO_SAN="IP"
    fi

    openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
        -keyout "$TLS_DIR/key.pem" -out "$TLS_DIR/cert.pem" \
        -subj "/CN=$DOMINIO" \
        -addext "subjectAltName=${TIPO_SAN}:${DOMINIO}" >/dev/null 2>&1

    chown root:"$SERVICE_USER" "$TLS_DIR/cert.pem" "$TLS_DIR/key.pem"
    chmod 640 "$TLS_DIR/cert.pem" "$TLS_DIR/key.pem"

    echo "    Certificado autofirmado: el navegador avisará la primera vez."
    echo "    Vale para una red interna; para Internet usa el modo letsencrypt."
    ESQUEMA="https"
    ;;

*)
    echo "Modo desconocido: $MODO (usa letsencrypt o self-signed)" >&2
    exit 1
    ;;
esac

# --------------------------------------------------------------------------- #
echo "==> Configurando el servicio"
set_env "BIND_HOST" "0.0.0.0"
set_env "BIND_PORT" "$PUERTO"
set_env "BEHIND_PROXY" "0"
set_env "TLS_CERT" "$TLS_DIR/cert.pem"
set_env "TLS_KEY" "$TLS_DIR/key.pem"

# base_url tiene que coincidir con lo que teclea el usuario, o los enlaces
# compartidos saldrán mal.
BASE_URL="$ESQUEMA://$DOMINIO"
if [[ "$PUERTO" != "443" ]]; then
    BASE_URL="$BASE_URL:$PUERTO"
fi
set_env "DRIVE_APP__BASE_URL" "$BASE_URL"
# Con HTTPS, la cookie de sesión no debe viajar nunca en claro.
set_env "DRIVE_SECURITY__SESSION__COOKIE_SECURE" "true"
# Ya no hay proxy: la IP del cliente es la del propio socket. Sin esto, la
# aplicación seguiría buscando cabeceras X-Forwarded-* que nadie escribe.
set_env "DRIVE_SERVER__BEHIND_PROXY" "false"
# HSTS sólo cuando el certificado es de una CA reconocida; con uno autofirmado
# dejaría el navegador clavado en un sitio que no puede validar.
if [[ "$MODO" == "letsencrypt" ]]; then
    set_env "DRIVE_SECURITY__HEADERS__HSTS" "true"
fi

chmod 640 "$ENV_FILE"
chown root:"$SERVICE_USER" "$ENV_FILE"

systemctl restart drive
sleep 2

echo
if systemctl is-active --quiet drive; then
    echo "Listo. La aplicación responde en $BASE_URL"
    echo
    echo "Abre el puerto en el cortafuegos si aún no lo has hecho:"
    echo "    sudo ufw allow $PUERTO/tcp"
    if [[ "$MODO" == "letsencrypt" ]]; then
        echo
        echo "La renovación es automática (certbot.timer). Para que funcione, el"
        echo "puerto 80 debe seguir siendo accesible desde Internet cuando toque"
        echo "renovar, dentro de unos 60 días. Compruébalo con:"
        echo "    sudo certbot renew --dry-run"
    fi
else
    echo "El servicio no ha arrancado. Revisa:" >&2
    journalctl -u drive -n 30 --no-pager >&2
    exit 1
fi
