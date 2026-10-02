# Drive (versión Lux)

Almacenamiento de ficheros autoalojado para Ubuntu Server, accesible desde
cualquier sitio y con una política de acceso configurable hasta el detalle:
cuentas con contraseña y 2FA, ficheros públicos sin login, acceso libre desde
tu propia red, enlaces con token, perfiles por rol y cuotas.

Reescritura completa de la versión FastAPI + SQLAlchemy + Jinja2 en
[Lux](../../Lux): un solo proceso, sin build de frontend, sin Docker, **sin una
línea de Python**. El código está en LuxScript (`app/`), las pruebas también
(`tests/`).

> [needed.md](needed.md) recoge lo que Lux todavía no cubre (poco) y qué
> carencias ya cerró. Lo más relevante: no hay migraciones de esquema y TLS sigue
> siendo cosa de nginx.

---

## Índice

- [Qué hace](#qué-hace)
- [Puesta en marcha](#puesta-en-marcha)
- [Configuración](#configuración)
- [Las cinco vías de acceso](#las-cinco-vías-de-acceso)
- [Recetas](#recetas)
- [API](#api)
- [Órdenes de administración](#órdenes-de-administración)
- [Despliegue en un servidor](#despliegue-en-un-servidor)
- [Seguridad](#seguridad)
- [Cómo está montado](#cómo-está-montado)
- [Pruebas](#pruebas)
- [Diferencias con la versión Python](#diferencias-con-la-versión-python)

---

## Qué hace

**Cuentas y perfiles**

- Usuario y contraseña con política configurable (longitud, mayúsculas, dígitos,
  símbolos, prohibir el nombre de usuario dentro de la contraseña). Hash
  argon2id (o bcrypt/PBKDF2) con rehash automático; también se verifican los
  hashes `$argon2…` / `$2b$…` de otros sistemas.
- Segundo factor TOTP opcional, y obligatorio por rol si así se configura
  (el QR sale si `qrencode` está instalado).
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
- Miniaturas de imágenes (WebP), búsqueda, destacados y cuotas por usuario.

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

## Puesta en marcha

Hace falta el binario de Lux (`Lux/build/lux`; véase `Lux/compile.sh`), las
bibliotecas `libargon2-1` y `tzdata`, y opcionalmente `qrencode`.

```bash
cd Drive/lux
export DRIVE_DB=./data/drive.db                      # base de datos SQLite
export DRIVE_STORAGE__ROOT=./data/blobs              # blobs y miniaturas
export DRIVE_SECURITY__SECRET_KEY="$(deploy/drive-cli.sh secret)"
lux ./app                                            # http://localhost:8000
```

La primera cuenta que se registre en `/register` es administradora. Con
`lux ./app` (sin `--no-watch`) los cambios en `app/` se recargan al guardar.

El esquema de la base de datos se crea en `on start:`; hasta entonces Lux
responde 503.

---

## Configuración

Cuatro capas, de menor a mayor prioridad:

1. Los *defaults* de `app/lib/config.lux` (`defaults()`).
2. `config/config.json` — el fichero con todas las opciones (o el que indique
   `DRIVE_CONFIG`). Es el equivalente del antiguo `config.yaml`: mismas claves,
   en JSON anidado.
3. Ajustes guardados desde `/admin/settings` — se guardan en la base de datos y
   se aplican sin reiniciar.
4. Variables de entorno `DRIVE_*` — ganan siempre. Son la vía de recuperación si
   un ajuste del panel deja la instancia inaccesible.

```bash
DRIVE_SECURITY__SECRET_KEY=...                   # security.secret_key
DRIVE_NETWORK__TRUSTED_MODE=readonly             # network.trusted_mode
DRIVE_NETWORK__TRUSTED_NETWORKS='["10.0.0.0/8"]' # listas en JSON
DRIVE_STORAGE__MAX_UPLOAD_SIZE=500MB             # tamaños con sufijo
DRIVE_SECURITY__SESSION__LIFETIME=12h            # duraciones con sufijo
```

Los tamaños admiten `B/KB/MB/GB/TB` y las duraciones `s/m/h/d/w`. Un valor nulo
o cero significa «sin límite».

Unas pocas claves sólo se pueden tocar en el fichero o por entorno, nunca desde
el panel: la ruta de los blobs, el secreto de firma y los algoritmos de hash.
`DRIVE_DB`, `DRIVE_HOST` y `DRIVE_MAX_BODY` (base de datos, dirección de
escucha y tope de una petición, 2GB) se leen al compilar y por eso son
variables sueltas y no claves de configuración.

Para revisar la configuración efectiva: `drive-cli check`.

Ajustes que ya no existen: `database.*`, `server.host/port/workers`,
`security.headers.*` (ahora `app: headers:` en `app.lux`) y `logging.*` (`app: log:`).

---

## Las cinco vías de acceso

Todo el control de acceso confluye en `app/lib/permissions.lux`, que devuelve el
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

```json
"network": {
  "trusted_networks": ["192.168.1.0/24"],

  "trusted_mode": "readonly",
  "trusted_scope": "shared",
  "require_login_for": ["upload", "delete", "share", "admin", "settings"]
}
```

- `trusted_mode`: `none` (pedir login igual que desde fuera), `readonly` (navegar
  y descargar sin login) o `full` (sesión automática como `network.trusted_user`).
- `trusted_scope`: `public` (sólo lo público), `shared` (lo público + lo marcado
  «visible en la red local») o `trusted_user` (todo el contenido de ese usuario).
- `require_login_for`: acciones que siguen pidiendo login aunque estés dentro.
  También se aplican a las rutas de `/admin`.

Esto descansa por completo en conocer la IP real del cliente. Detrás de un proxy
inverso, `X-Forwarded-For` **sólo se acepta si el salto inmediato está declarado
en `server.trusted_proxies`**; en caso contrario cualquiera podría fingir estar
en tu red enviando una cabecera. Hay pruebas específicas de ambos casos
(`net_forged`, `net_proxy`).

---

## Recetas

**Que mi familia vea las fotos desde casa sin cuenta, pero no desde fuera**

```json
"network": { "trusted_networks": ["192.168.1.0/24"], "trusted_mode": "readonly", "trusted_scope": "shared" }
```

Después, en la carpeta de fotos, marca «Visible sin login desde las redes de
confianza» en su pantalla de detalles.

**Compartir un fichero con alguien de fuera** — Detalles del fichero → *Enlaces
con token* → contraseña, caducidad y límite de descargas si quieres → copiar el
enlace.

**Recibir ficheros de un tercero sin darle cuenta** — Crea una carpeta, y sobre
ella un enlace en modo *Sólo subida (buzón)*.

**Un usuario que sólo pueda mirar** — Créalo con rol `guest`, o déjale su rol y
pon `can_upload` y `can_delete` en *Denegar* en su ficha de `/admin/users`.

**Sincronizar desde un script** — Crea un token de API en tu perfil con ámbito
`write` y usa `/api/files`.

---

## API

Autenticación con `Authorization: Bearer <token>` o con la cookie de sesión.
Documentación de las rutas en `/docs` (y `/openapi.json`). Los errores salen como
`{"detail": "..."}`.

```
GET    /api/me                      quién soy y qué puedo
GET    /api/files?parent_id=…       listar
GET    /api/files/{id}              metadatos, ruta e hijos
GET    /api/files/{id}/content      descargar
POST   /api/files                   subir (multipart: file, parent_id, overwrite)
POST   /api/folders                 crear carpeta (name, parent_id)
PATCH  /api/files/{id}              renombrar, mover, cambiar visibilidad
DELETE /api/files/{id}?permanent=   a la papelera o definitivo
POST   /api/files/{id}/restore      restaurar
POST   /api/files/{id}/shares       compartir con un usuario
POST   /api/files/{id}/links        crear un enlace
GET    /api/search?q=…              buscar
GET    /api/usage                   cuota y consumo
```

```bash
TOKEN=...
curl -H "Authorization: Bearer $TOKEN" \
     -F "file=@informe.pdf" \
     https://drive.ejemplo.com/api/files
```

Los ámbitos del token (`read`, `write`, `share`) se comprueban en cada llamada,
así que un token de backup no puede borrar nada.

---

## Órdenes de administración

`drive-cli` es un envoltorio de `lux run app -- <orden>` (`app/routes/commands.lux`):

```bash
sudo drive-cli createuser NOMBRE --role admin    # pide la contraseña
sudo drive-cli passwd NOMBRE
sudo drive-cli listusers
sudo drive-cli recompute                         # recalcular el espacio usado
sudo drive-cli maintenance                       # forzar la limpieza ahora
sudo drive-cli check                             # configuración efectiva y avisos
drive-cli secret                                 # generar un secret_key
```

**Copia de seguridad.** Basta con la base de datos y el directorio de blobs:

```bash
systemctl stop drive
tar czf drive-$(date +%F).tar.gz /var/lib/drive
systemctl start drive
```

---

## Despliegue en un servidor

Desde tu equipo, sin copiar nada a mano:

```bash
./deploy/deploy.sh --lux-bin ~/Github/Lux/build/lux burnt@192.168.1.20   # Linux y macOS
deploy\deploy.bat --lux-bin C:\ruta\lux burnt@192.168.1.20               # Windows
```

Empaqueta el proyecto (y el binario de Lux), lo envía por SSH y ejecuta el
instalador allí. **La primera vez instala; a partir de entonces detecta la
instalación existente y sólo actualiza el código** — la base de datos, los
ficheros subidos, `config.json` y el secreto de firma se conservan siempre. En
las actualizaciones, si el servicio no llega a responder con el código nuevo se
**revierte automáticamente**.

```
-p, --port PUERTO     Puerto SSH (22)
-i, --key FICHERO     Clave privada SSH
    --lux-bin FICHERO Binario de Lux a instalar (si no, el servidor debe tenerlo)
-d, --staging RUTA    Directorio temporal en el servidor (/tmp/drive-deploy)
    --health-port N   Puerto donde comprobar /healthz (8000)
    --install         Forzar instalación completa
    --update          Forzar sólo actualización de código
-y, --yes             No pedir confirmación
-n, --dry-run         Mostrar lo que haría, sin tocar nada
```

O directamente en el servidor: `sudo LUX_BIN=/ruta/lux bash deploy/install.sh`
(también acepta `LUX_SRC=/ruta/al/repositorio` para compilarlo allí).

El instalador crea el usuario de servicio, `/opt/drive`, `/var/lib/drive`, un
secreto de firma en `/etc/drive/drive.env` y el servicio de systemd, que escucha
en `127.0.0.1:8000`.

### HTTPS

Lux no sirve TLS (ni te deja elegir la dirección de escucha): **nginx es
obligatorio** para exponerlo. `setup-tls.sh` lo instala y lo configura:

```bash
# Certificado de Let's Encrypt, en el 443
sudo bash deploy/setup-tls.sh letsencrypt drive.ejemplo.com tu@correo.com 443

# En un puerto alto
sudo bash deploy/setup-tls.sh letsencrypt drive.ejemplo.com tu@correo.com 4023

# O autofirmado, para una red interna
sudo bash deploy/setup-tls.sh self-signed 192.168.1.20 4023
```

Genera el sitio de nginx a partir de `deploy/nginx.conf` (HSTS en
Let's Encrypt, límite de subida, cabeceras `X-Forwarded-*`), y ajusta `base_url`, la cookie `Secure` y el proxy de
confianza. **Let's Encrypt exige el puerto 80**: el reto HTTP-01 se valida
siempre contra él, también al renovar (cada ~60 días); compruébalo con
`sudo certbot renew --dry-run`.

Lux escucha sólo en `127.0.0.1` (`app: host`), de modo que sólo nginx llega a él.

> Los scripts de `deploy/` se han revisado con `bash -n` y probado
> `drive-cli.sh` contra una instancia real, pero **no se han ejecutado en un
> servidor Ubuntu** (no había uno a mano): pruébalos primero con `--dry-run`.

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
- Cabeceras `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy` y CSP
  (las pone Lux) y HSTS (nginx).
- Límite de peticiones por IP en login, registro y descargas.
- La unidad de systemd corre con `ProtectSystem=strict` y un único directorio
  escribible.

Lo que tienes que hacer tú:

1. **Cambiar `security.secret_key`.** Con el valor por defecto cualquiera puede
   falsificar sesiones. El instalador lo genera; si instalas a mano, no lo
   olvides — `drive-cli check` avisa.
2. **Servir por HTTPS** (`setup-tls.sh`).
3. Revisar `network.trusted_mode` antes de exponer el servicio a Internet: en
   `full`, cualquiera dentro de la red de confianza entra como ese usuario.

---

## Cómo está montado

```
app/
  app.lux            módulos importados y bloque app: (puerto, sqlite, plantillas)
  lib/
    util.lux         tamaños, duraciones, fechas, nombres de fichero
    config.lux       configuración en capas (defaults < JSON < BD < entorno)
    db.lux           esquema y consultas comunes
    security.lux     contraseñas (argon2), tokens, TOTP (otp)
    netutils.lux     IP real del cliente y evaluación de redes
    ratelimit.lux    límite por IP (state.hit, ventana deslizante)
    audit.lux        registro de auditoría
    permissions.lux  el corazón: quién puede qué, y por qué
    auth.lux         sesiones, login, el principal de cada petición
    storage.lux      blobs, hashes, cuotas de disco
    files_service.lux  árbol, papelera, versiones, cuotas
    shares_service.lux comparticiones y enlaces
    thumbnails.lux   miniaturas
    maintenance.lux  limpieza periódica (every "1m")
    ctx.lux          contexto de las plantillas y cortes (abort) de las rutas
  routes/            misc, auth, files, shares, public, profile, admin, api, commands
  templates/         interfaz (funciona sin JavaScript)
  public/            una hoja de estilos y un script pequeño
config/config.json   configuración con todas las opciones
deploy/              systemd, nginx e instaladores
tests/               pruebas (también en Lux)
needed.md            lo que Lux necesita
```

**Decisiones que conviene conocer**

- *Blobs opacos con metadatos en la base de datos.* Permite deduplicar,
  versionar y compartir sin tocar el sistema de ficheros, y elimina toda una
  clase de vulnerabilidades. A cambio, no puedes inspeccionar los ficheros con
  `ls`: usa la API o la interfaz.
- *Sesiones en la base de datos*, no en la cookie firmada de Lux: se pueden
  revocar una a una y listar en el perfil.
- *Los errores esperables se devuelven, no se lanzan.* Las funciones de
  `lib/` devuelven `{"ok": false, "error": …}`; la ruta decide si hace
  `rollback` y qué página muestra. Los cortes de permisos usan `abort()`
  (`need_login`, `authz`, `allow`) y el manejador `on error` los pinta.
- *El principal y las filas son diccionarios `Json`*; las plantillas reciben un
  único diccionario `c` con todo ya formateado (las plantillas de Lux no pueden
  llamar a funciones).
- *El limitador de peticiones vive en memoria* (`state`). Suficiente para una
  instancia.

**Lo que no incluye:** WebDAV, cliente de sincronización de escritorio, edición
de documentos en línea, cifrado en reposo ni antivirus en la subida.

---

## Pruebas

```bash
LUX=~/Github/Lux/build/lux tests/run.sh               # todos los escenarios
LUX=~/Github/Lux/build/lux tests/run.sh default net_proxy
```

Cada escenario (`tests/scenarios/NAME.json`) arranca una instancia limpia con
su propia configuración, base de datos y blobs, y una aplicación Lux
(`tests/*.lux`) la recorre por HTTP: autenticación y bloqueos, cuotas y
límites, papelera, versiones y retención, deduplicación, las cinco vías de
acceso, suplantación de `X-Forwarded-For`, enlaces con contraseña y con límite
de descargas, buzones, ámbitos de los tokens, ajustes en caliente, 2FA con
códigos TOTP reales, órdenes de consola (`lux run`), miniaturas y renderizado de todas las
páginas. `run.sh` termina con código 0 sólo si todo pasa.

---

## Diferencias con la versión Python

- Marcas de tiempo como enteros; sin clave `database.*` (SQLite).
- `/docs` en lugar de `/api/docs`.
- Un permiso `owner` ya no se puede conceder al compartir.
- `require_login_for` se aplica también a `/admin`.
- Las migas de pan de un enlace con token no revelan lo que hay por encima de la
  raíz del enlace.
- El TLS lo pone siempre nginx. Ver [needed.md](needed.md).
