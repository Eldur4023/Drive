# Lo que Lux todavía necesita para esta aplicación

La primera versión de este documento listaba 20 carencias. Lux ha cerrado casi
todas (commit `9f95c0d` de Lux) y la aplicación ya las usa:

| Carencia original | Ahora |
|---|---|
| Subidas de más de 16 MB | `app: max_body` + subida en streaming a disco → 2 GB |
| argon2 / bcrypt | `hash.argon2` / `hash.bcrypt`; `hash.verify` acepta hashes ajenos (se pueden migrar usuarios) |
| TOTP (SHA-1, base32, OTP) | módulo `otp`; desaparecen `openssl` y el base32 escrito a mano |
| Rutas repetían las comprobaciones | `abort(code, msg)` → `need_login`, `authz`, `allow` en `lib/ctx.lux` |
| `render()` y `await` en `on error` | funcionan (el manejador es asíncrono); la página de error es `error.html` y lleva el menú del usuario |
| `on error` y redirecciones, mensajes en `status()` | `abort(404, "mensaje")` llega a `error.message` |
| Sin gancho de arranque | `on start:` (esquema y avisos) |
| Sin modo script | `command` + `lux run`; `drive-cli` es su envoltorio (adiós a `/_cli`) |
| Sin cabeceras globales ni `host` | `app: headers:` (CSP) y `app: host "127.0.0.1"` |
| Zonas horarias | nombres IANA en `time.format` |
| `state` sin TTL | `state.hit` (ventana deslizante) y `state.ttl` (`Retry-After` real) |
| Formularios con campos repetidos | `form_list`; `request.query` (el `next` del login conserva la consulta) |
| `df`, `chmod`… | `os.disk_usage` |
| ETag/304/Content-Disposition a mano | `send_file(path, {"filename", "inline"})` |
| Listas en SQL | `where id in (?)` con una lista |
| `http` siempre seguía redirecciones | `follow_redirects`, `cookies` |

Queda esto, de más a menos importante:

## 1. Bug que se corrigió en Lux al adoptar `cookies`

El motor de cookies de `http` (`CURLOPT_COOKIEFILE ""`) vivía en un handle de
curl que cada hilo reutiliza, así que una cookie puesta por una respuesta se
colaba en una petición posterior no relacionada (1 de cada 20 en una prueba).
Arreglado con `CURLOPT_COOKIELIST "ALL"` antes de cada petición
(`http.cpp`); conviene un test de regresión en Lux.

## 2. Sin migraciones de esquema

El esquema se crea con `create table if not exists` en `on start:`, pero no hay
forma de versionarlo ni de aplicar cambios (`alter table`) de forma ordenada
entre versiones de la app.

## 3. TLS

Lux sigue sin servir TLS (decisión de diseño): nginx es obligatorio para
exponer Drive (`deploy/setup-tls.sh`). HSTS se pone ahí.

## 4. Menores

- `class` no admite campos `Json`: el principal y las filas son diccionarios
  `Json` y ni el compilador ni las plantillas comprueban sus campos.
- `openapi.json` (`docs`) lista las rutas pero sin esquemas de los formularios.
- No hay generador de QR: el del 2FA sale sólo si está instalada la orden
  `qrencode`; si no, se muestra la clave en texto.
- `hash.argon2` / `bcrypt` cargan `libargon2.so.1` / `libcrypt.so.1` del
  sistema (`apt install libargon2-1`, ya en `install.sh`).
- Pendiente de adoptar (ya soportado por Lux, no por falta de nada): las
  plantillas podrían llamar a las `fn` del proyecto en vez de recibir los datos
  ya formateados (`lib/ctx.lux: view_node`), y las pruebas podrían usar
  `lux test` / `follow_redirects: false`; siguen siendo una app Lux con un
  guion de shell.
- El desempate de versiones usa `rowid` (SQLite) y el motor está en el nombre de
  las llamadas (`sqlite.query`): pasar a PostgreSQL es sustituir el nombre.

## Cambios de comportamiento deliberados (no son carencias)

- Un permiso `owner` ya no se puede conceder al compartir (sólo `read`/`write`).
- `network.require_login_for: ["admin", "settings"]` se **aplica** ahora a
  `/admin` (en la versión Python sólo ocultaba el enlace).
- Las migas de pan de un enlace con token no enseñan lo que hay por encima de
  su raíz.
- Marcas de tiempo como enteros (ms UTC); `/docs` en lugar de `/api/docs`.
- Se pierden `database.*`, `server.host/port/workers`, `security.headers.*` y
  `logging.*` como ajustes de `config.json` (hoy: `app:` en `app.lux`, variables
  `DRIVE_HOST`, `DRIVE_MAX_BODY`, `BIND_PORT`).
