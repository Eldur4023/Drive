# Drive

Almacenamiento de ficheros autoalojado para Ubuntu Server, accesible desde
cualquier sitio y con una política de acceso configurable hasta el detalle:
cuentas con contraseña y 2FA, ficheros públicos sin login, acceso libre desde
tu propia red, enlaces con token, perfiles por rol y cuotas.

FastAPI + SQLAlchemy + Jinja2. Un solo proceso, sin build de frontend, sin
Docker obligatorio y sin dependencias externas más allá de Python.

---

## Índice

- [Qué hace](#qué-hace)
- [Despliegue remoto por SSH](#despliegue-remoto-por-ssh)
- [Instalación en Ubuntu Server](#instalación-en-ubuntu-server)
- [HTTPS](#https)
- [Puesta en marcha manual](#puesta-en-marcha-manual)
- [Configuración](#configuración)
- [Las cinco vías de acceso](#las-cinco-vías-de-acceso)
- [Recetas](#recetas)
- [API](#api)
- [Órdenes de administración](#órdenes-de-administración)
- [Estado para QuemaOS](#estado-para-quemaos)
- [Seguridad](#seguridad)
- [Cómo está montado](#cómo-está-montado)
- [Pruebas](#pruebas)

---

## Qué hace

**Cuentas y perfiles**

- Usuario y contraseña con política configurable (longitud, mayúsculas, dígitos,
  símbolos, prohibir el nombre de usuario dentro de la contraseña).
- Hash con argon2 (o bcrypt), con rehash automático al cambiar de algoritmo.
- Segundo factor TOTP opcional, y obligatorio por rol si así se configura.
- Bloqueo por intentos fallidos, por cuenta y por IP.
- Registro abierto, cerrado o con aprobación manual; restricción por dominio de
  correo.
- Tres roles (`admin`, `user`, `guest`), cada uno con su cuota, su tamaño máximo
  de subida y sus permisos. Cualquier usuario puede llevar sus propios
  *overrides* por encima del rol.
- Perfil editable, preferencias de interfaz, sesiones activas revocables una a
  una, y tokens de API con ámbitos.

**Ficheros**

- Carpetas, subida múltiple, arrastrar y soltar, renombrar, mover, copiar.
- Papelera con retención configurable y restauración a su sitio original.
- Historial de versiones al sobrescribir, con restauración.
- Deduplicación por hash: el mismo contenido ocupa un solo blob en disco.
- Descarga con soporte de `Range` (vídeo y reanudación) y `ETag`.
- Miniaturas de imágenes, búsqueda, destacados y cuotas por usuario.

**Compartir**

- Con otros usuarios de la instancia, en lectura o escritura, con caducidad
  opcional; los permisos se heredan por las carpetas.
- Marcando algo como **público**: accesible por cualquiera sin cuenta.
- Con **enlaces de token**: contraseña opcional, caducidad, límite de descargas,
  restricción por red y modo buzón (el visitante sube sin ver el resto).
- Marcando algo como **visible en la red local**, sin login para quien esté
  dentro de casa.

**Operación**

- Panel de administración con estadísticas, gestión de usuarios, registro de
  auditoría y edición de ajustes en caliente.
- Tareas de mantenimiento internas: papelera, versiones, sesiones, auditoría y
  blobs huérfanos.

---

## Despliegue remoto por SSH

Desde tu equipo, sin copiar nada a mano:

```bash
./deploy/deploy.sh burnt@192.168.1.20              # Linux y macOS
deploy\deploy.bat burnt@192.168.1.20               # Windows
```

Empaqueta el proyecto, lo envía por SSH y ejecuta el instalador allí. **La
primera vez instala; a partir de entonces detecta la instalación existente y
sólo actualiza el código** — la base de datos, los ficheros subidos,
`config.yaml` y el secreto de firma se conservan siempre.

```
-p, --port PUERTO     Puerto SSH (22)
-i, --key FICHERO     Clave privada SSH
-d, --staging RUTA    Directorio temporal en el servidor (/tmp/drive-deploy)
    --health-port N   Puerto donde comprobar /healthz (8000)
    --install         Forzar instalación completa
    --update          Forzar sólo actualización de código
-y, --yes             No pedir confirmación
-n, --dry-run         Mostrar lo que haría, sin tocar nada
```

Antes de actuar comprueba la conexión, muestra un resumen y pide confirmación.
Con `--dry-run` puedes ver qué haría sin riesgo.

En las actualizaciones, si el servicio no llega a arrancar con el código nuevo
se **revierte automáticamente** a la versión anterior y se vuelve a levantar; el
despliegue termina en error y con el `journalctl` del fallo en pantalla.

**Requisitos.** En tu equipo: `ssh`, `scp` y `tar` (Windows 10 1803+ ya los
trae; si no, Git Bash). En el servidor: Ubuntu con acceso `sudo` para tu
usuario. Como `sudo` suele pedir contraseña, ejecuta el script desde una
terminal interactiva — usa `ssh -t` internamente para que puedas teclearla.

## Instalación en Ubuntu Server

Si prefieres hacerlo directamente en el servidor:

```bash
git clone <repositorio> drive && cd drive
sudo bash deploy/install.sh
```

El script crea el usuario de servicio, el entorno virtual, los directorios en
`/var/lib/drive`, genera un secreto de firma en `/etc/drive/drive.env` y deja el
servicio de systemd activo escuchando en `127.0.0.1:8000`.

Después crea el primer administrador:

```bash
sudo drive-cli createuser tunombre --role admin
```

Recién instalado escucha en `127.0.0.1:8000`, sin cifrar y sin acceso desde
fuera. Para exponerlo, elige uno de los dos caminos de la sección siguiente.

## HTTPS

### Sin proxy: la aplicación sirve TLS directamente

Es lo más simple y lo que recomiendo para una instancia propia. uvicorn hace
TLS de forma nativa (módulo `ssl` de la biblioteca estándar), así que no hace
falta nginx, ni Caddy, ni ninguna dependencia extra.

```bash
# Certificado de Let's Encrypt, en el 443
sudo bash deploy/setup-tls.sh letsencrypt drive.ejemplo.com tu@correo.com 443

# O en un puerto alto
sudo bash deploy/setup-tls.sh letsencrypt drive.ejemplo.com tu@correo.com 4023

# O autofirmado, para una red interna
sudo bash deploy/setup-tls.sh self-signed 192.168.1.20 4023
```

El script se ocupa de las tres cosas que suelen fallar:

- **Permisos del certificado.** Los ficheros de Let's Encrypt son sólo de root
  y el servicio no corre como root. Se deja una copia en `/etc/drive/tls`
  legible por el grupo del servicio.
- **Renovación.** Instala un *deploy hook* que repite esa copia y reinicia el
  servicio, porque uvicorn lee el certificado únicamente al arrancar. Sin ese
  reinicio seguirías sirviendo el certificado viejo hasta que caducara.
- **Coherencia de la configuración.** Ajusta `base_url` con su puerto,
  `cookie_secure`, `behind_proxy` y HSTS de una vez.

El puerto 443 no necesita root: la unidad concede `CAP_NET_BIND_SERVICE`, que
es la única capacidad que se le da.

**Let's Encrypt exige el puerto 80.** El reto HTTP-01 se valida siempre contra
el 80 y no admite otro. El script usa certbot en modo *standalone*, que lo
ocupa unos segundos durante la validación, así que el 80 debe ser accesible
desde Internet al emitir **y al renovar** (cada ~60 días). Compruébalo con
`sudo certbot renew --dry-run`. Si tu operador bloquea el 80, usa validación
DNS-01 o el modo `self-signed`.

**Lo que pierdes sin proxy delante:** HTTP/2 (uvicorn sólo habla HTTP/1.1),
descarga de TLS a un proceso separado y absorción de conexiones lentas. Para
compensar, el lanzador limita concurrencia y *backlog*. Para una instancia
personal o de equipo es de sobra; si esperas mucha carga o quieres HTTP/2,
pon un proxy.

### Con nginx delante

Si ya tienes nginx, o quieres HTTP/2 y varios sitios en la misma máquina:

```bash
sudo cp deploy/nginx.conf /etc/nginx/sites-available/drive
sudo ln -s /etc/nginx/sites-available/drive /etc/nginx/sites-enabled/
sudo certbot --nginx -d drive.ejemplo.com
sudo systemctl reload nginx
```

Para un puerto distinto del 443 usa
[deploy/nginx-puerto-personalizado.conf](deploy/nginx-puerto-personalizado.conf).

Con proxy hay que decírselo a la aplicación, o dejará de ver la IP real del
cliente y las redes de confianza no funcionarán. En `/etc/drive/drive.env`:

```
BIND_HOST=127.0.0.1
BEHIND_PROXY=1
TRUSTED_PROXY=127.0.0.1
```

Y en `config.yaml`:

```yaml
server:
  behind_proxy: true
  trusted_proxies: ["127.0.0.1/32"]
app:
  base_url: "https://drive.ejemplo.com"
security:
  session:
    cookie_secure: true
  headers:
    hsts: true
```

### Dónde escucha el proceso

Lo deciden estas variables de `/etc/drive/drive.env`, que lee
[deploy/run.sh](deploy/run.sh) al arrancar:

| Variable | Para qué |
|---|---|
| `BIND_HOST` | `127.0.0.1` con proxy delante, `0.0.0.0` para exponerse |
| `BIND_PORT` | Puerto de escucha |
| `BIND_WORKERS` | Procesos trabajadores |
| `TLS_CERT` / `TLS_KEY` | Certificado en PEM. Vacíos = HTTP sin cifrar |
| `BEHIND_PROXY` | `1` activa `--proxy-headers`; `0` los ignora |
| `TRUSTED_PROXY` | IPs de las que aceptar `X-Forwarded-*` |

`server.host` y `server.port` de `config.yaml` **no intervienen** cuando se
arranca con systemd: es la unidad quien se los pasa a uvicorn. Sólo se usan si
lanzas uvicorn a mano.

## Puesta en marcha manual

```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt

export DRIVE_SECURITY__SECRET_KEY="$(venv/bin/python -m app.cli secret)"
venv/bin/python -m app.cli init
venv/bin/python -m app.cli createuser admin --role admin

venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
```

---

## Configuración

Tres capas, de menor a mayor prioridad:

1. `config/config.yaml` — el fichero comentado, con todas las opciones.
2. Ajustes guardados desde `/admin/settings` — se guardan en la base de datos y
   se aplican sin reiniciar.
3. Variables de entorno `DRIVE_*` — ganan siempre. Son la vía de recuperación si
   un ajuste del panel deja la instancia inaccesible.

```bash
DRIVE_SECURITY__SECRET_KEY=...                   # security.secret_key
DRIVE_NETWORK__TRUSTED_MODE=readonly             # network.trusted_mode
DRIVE_NETWORK__TRUSTED_NETWORKS='["10.0.0.0/8"]' # listas en JSON
DRIVE_STORAGE__MAX_UPLOAD_SIZE=5GB               # tamaños con sufijo
DRIVE_SECURITY__SESSION__LIFETIME=12h            # duraciones con sufijo
```

Los tamaños admiten `B/KB/MB/GB/TB` y las duraciones `s/m/h/d/w`. Un valor nulo
o cero significa «sin límite».

Unas pocas claves sólo se pueden tocar en el fichero o por entorno, nunca desde
el panel: la URL de la base de datos, la ruta de los blobs, el secreto de firma,
el algoritmo de hash y los parámetros de escucha.

Para revisar la configuración efectiva:

```bash
python -m app.cli check
```

---

## Las cinco vías de acceso

Todo el control de acceso confluye en `app/permissions.py`, que devuelve el
permiso efectivo y **de dónde sale**, para que la interfaz pueda explicarlo:

| Vía | Cuándo aplica | Permiso |
|---|---|---|
| Propietario | Es tu fichero | total |
| Compartición | Te lo han compartido, directamente o por una carpeta padre | lectura o escritura |
| Público | Marcado como público, o dentro de una carpeta pública | lectura, sin cuenta |
| Enlace | Se entra por `/s/<token>` | lectura o subida, según el enlace |
| Red | La IP de origen está en `network.trusted_networks` | según la política de red |

Las reglas se evalúan en ese orden y gana la primera que concede. La papelera
sólo la ve su dueño, aunque el elemento fuera público antes de borrarse. A quien
no tiene ningún acceso se le responde 404 y no 403, para que la existencia de un
fichero ajeno no se pueda confirmar sondeando identificadores.

### Acceso desde la red local

```yaml
network:
  trusted_networks: ["192.168.1.0/24"]

  # none     -> pedir login igual que desde fuera
  # readonly -> navegar y descargar sin login
  # full     -> sesión automática como network.trusted_user
  trusted_mode: "readonly"

  # public       -> sólo lo marcado como público
  # shared       -> lo público + lo marcado «visible en red local»
  # trusted_user -> todo el contenido de trusted_user
  trusted_scope: "shared"

  # Estas acciones siguen pidiendo login aunque estés dentro
  require_login_for: ["upload", "delete", "share", "admin", "settings"]
```

Esto descansa por completo en conocer la IP real del cliente. Detrás de un proxy
inverso, `X-Forwarded-For` **sólo se acepta si el salto inmediato está declarado
en `server.trusted_proxies`**; en caso contrario cualquiera podría fingir estar
en tu red enviando una cabecera. Hay pruebas específicas de ambos casos.

---

## Recetas

**Que mi familia vea las fotos desde casa sin cuenta, pero no desde fuera**

```yaml
network:
  trusted_networks: ["192.168.1.0/24"]
  trusted_mode: "readonly"
  trusted_scope: "shared"
```

Después, en la carpeta de fotos, marca «Visible sin login desde las redes de
confianza» en su pantalla de detalles.

**Compartir un fichero con alguien de fuera**

Detalles del fichero → *Enlaces con token* → contraseña, caducidad y límite de
descargas si quieres → copiar el enlace.

**Recibir ficheros de un tercero sin darle cuenta**

Crea una carpeta, y sobre ella un enlace en modo *Sólo subida (buzón)*. Quien lo
reciba podrá dejar ficheros sin ver el resto de tu contenido.

**Un usuario que sólo pueda mirar**

Créalo con rol `guest`, o déjale su rol y pon `can_upload` y `can_delete` en
*Denegar* en su ficha de `/admin/users`.

**Sincronizar desde un script**

Crea un token de API en tu perfil con ámbito `write` y usa `/api/files`.

---

## API

Autenticación con `Authorization: Bearer <token>` o con la cookie de sesión.
Documentación interactiva en `/api/docs`.

```
GET    /api/me                      quién soy y qué puedo
GET    /api/files?parent_id=…       listar
GET    /api/files/{id}              metadatos, ruta e hijos
GET    /api/files/{id}/content      descargar
POST   /api/files                   subir (multipart: file, parent_id, overwrite)
POST   /api/folders                 crear carpeta
PATCH  /api/files/{id}              renombrar, mover, cambiar visibilidad
DELETE /api/files/{id}?permanent=   a la papelera o definitivo
POST   /api/files/{id}/restore      restaurar
POST   /api/files/{id}/shares       compartir con un usuario
POST   /api/files/{id}/links        crear un enlace
POST   /api/sync/ops                lote de operaciones: mkdir, move, trash (cliente de escritorio)
GET    /api/sync/tree?root_id=…     todo lo vivo bajo una carpeta, plano y con hash
GET    /api/search?q=…              buscar
GET    /api/usage                   cuota y consumo
```

```bash
TOKEN=...
curl -H "Authorization: Bearer $TOKEN" \
     -F "file=@copia.tar.gz" \
     https://drive.ejemplo.com/api/files
```

Los ámbitos del token (`read`, `write`, `share`) se comprueban en cada llamada,
así que un token de backup no puede borrar nada.

**Operaciones por lotes.** `POST /api/sync/ops` recibe `{"run": "<id>", "ops": [...]}` con
hasta 1000 operaciones que se aplican en orden y se confirman juntas. Cada una es atómica
(se valida entera antes de tocar nada), idempotente (repetir un lote no duplica ni
estropea nada) y deja rastro en la auditoría con el `run` y la `key` del cliente
(eventos `mkdir`, `move` y `delete` con `via: sync`; hay que tenerlos en `audit.events`).
Devuelve un resultado por operación, así que un fallo no corta el resto:

```
{"op": "mkdir", "key": "1", "parent": "<id>|$ref", "name": "sub", "ref": "a/sub"}   # reutiliza si ya existe
{"op": "move",  "key": "2", "id": "<id>", "parent": "<id>|$ref", "name": "nuevo"}   # falla si el nombre está ocupado
{"op": "trash", "key": "3", "id": "<id>"}                                          # a la papelera
```

`$ref` apunta a la carpeta que creó antes, en el mismo lote, el `mkdir` con esa `ref`.

---

## Órdenes de administración

```bash
python -m app.cli init                          # crear esquema y directorios
python -m app.cli createuser NOMBRE --role admin
python -m app.cli passwd NOMBRE
python -m app.cli listusers
python -m app.cli recompute                     # recalcular el espacio usado
python -m app.cli maintenance                   # forzar la limpieza ahora
python -m app.cli check                         # configuración efectiva y avisos
python -m app.cli secret                        # generar un secret_key
```

**Copia de seguridad.** Basta con la base de datos y el directorio de blobs:

```bash
systemctl stop drive
tar czf drive-$(date +%F).tar.gz /var/lib/drive
systemctl start drive
```

---

## Estado para QuemaOS

Drive abre un pequeño servidor HTTP propio, aparte del de la API, en `127.0.0.1:9701` (rango 9700-9799 de la
suite) con `GET /quemaos/status`: `{quemaos, id, name, status: ok|warn|error, message, metrics, extra}`.
No toca la base de datos ni el disco al responder (contesta con lo último que midió un hilo aparte, cada 30 s) y
se configura en la sección `quemaos:` de `config.yaml` (`enabled`, `host`, `port`). Con varios workers sólo el
primero la sirve. Código: [app/quemaos.py](app/quemaos.py).

---

## Seguridad

Lo que ya está resuelto:

- Los nombres que escribe el usuario **nunca** llegan al sistema de ficheros: el
  contenido se guarda como `<hash>` y el árbol vive en la base de datos, así que
  no hay path traversal posible por diseño.
- Contraseñas y tokens se guardan hasheados; los tokens de API y de sesión sólo
  existen en claro en el momento de crearse.
- El login tarda lo mismo exista la cuenta o no.
- Cookies `HttpOnly`, `SameSite` configurable y `Secure` opcional; sesiones con
  caducidad deslizante y tope absoluto.
- Un HTML subido por un usuario se sirve como texto plano al previsualizarlo,
  para que no se ejecute en el origen.
- Cabeceras CSP, `X-Frame-Options`, `X-Content-Type-Options`, `Referrer-Policy`
  y HSTS opcional.
- Límite de peticiones por IP en login, registro y descargas.
- La unidad de systemd corre con `ProtectSystem=strict` y un único directorio
  escribible.

Lo que tienes que hacer tú:

1. **Cambiar `security.secret_key`.** Con el valor por defecto cualquiera puede
   falsificar sesiones. El instalador lo genera; si instalas a mano, no lo
   olvides — `app.cli check` avisa.
2. **Servir por HTTPS** y poner `cookie_secure: true`.
3. **Declarar `trusted_proxies`** si hay un proxy delante.
4. Revisar `network.trusted_mode` antes de exponer el servicio a Internet: en
   `full`, cualquiera dentro de la red de confianza entra como ese usuario.

---

## Cómo está montado

```
app/
  config.py          configuración en capas (YAML < BD < entorno)
  settings_store.py  ajustes editables en caliente
  models.py          esquema
  database.py        motor y sesiones
  security.py        contraseñas, tokens, TOTP
  netutils.py        IP real del cliente y evaluación de redes
  ratelimit.py       límite por IP
  permissions.py     el corazón: quién puede qué, y por qué
  auth.py            sesiones, login, Principal de cada petición
  storage.py         blobs, hashes, rangos, cuotas
  files_service.py   árbol, papelera, versiones, cuotas
  shares_service.py  comparticiones y enlaces
  thumbnails.py      miniaturas
  audit.py           registro de auditoría
  maintenance.py     limpieza periódica
  cli.py             utilidades de consola
  main.py            arranque, middleware, errores
  routers/           auth, files, shares, public, profile, admin, api
  templates/         interfaz (Jinja2, funciona sin JavaScript)
  static/            una hoja de estilos y un script pequeño
config/config.yaml   configuración comentada
deploy/              systemd, nginx e instalador
tests/               70 pruebas de extremo a extremo
```

**Decisiones que conviene conocer**

- *Blobs opacos con metadatos en la base de datos.* Permite deduplicar,
  versionar y compartir sin tocar el sistema de ficheros, y elimina toda una
  clase de vulnerabilidades. A cambio, no puedes inspeccionar los ficheros con
  `ls`: usa la API o la interfaz.
- *SQLite por defecto, PostgreSQL cambiando una línea.* SQLite en modo WAL
  aguanta de sobra el uso doméstico o de un equipo pequeño.
- *Las tareas periódicas van en un hilo del propio proceso.* No hace falta cron
  ni un worker aparte. Si un día despliegas varios procesos, deja
  `maintenance.enabled` en uno solo.
- *El limitador de peticiones vive en memoria.* Suficiente para una instancia;
  con varias haría falta Redis.

**Lo que no incluye:** WebDAV, cliente de sincronización de escritorio, edición
de documentos en línea, cifrado en reposo ni antivirus en la subida.

---

## Pruebas

```bash
pip install -r requirements-dev.txt
pytest
```

70 pruebas de extremo a extremo sobre la aplicación real: autenticación y
bloqueos, cuotas y límites, papelera y versiones, deduplicación, las cinco vías
de acceso, suplantación de `X-Forwarded-For`, enlaces con contraseña y con
límite de descargas, buzones de subida, ámbitos de los tokens, ajustes en
caliente y renderizado de todas las páginas.
