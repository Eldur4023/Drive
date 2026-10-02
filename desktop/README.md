# Drive Sync

Cliente de escritorio de Drive. Enlazas carpetas de tu equipo con carpetas de
Drive y **los cambios se aplican en los dos sentidos**, solos, también cuando el
equipo arranca y aún no ha iniciado sesión nadie.

La web de Drive sigue siendo la interfaz cómoda; esto es lo que mantiene tus
ficheros al día en cada máquina.

```
┌────────────── tu equipo ──────────────┐                 ┌──── servidor ────┐
│                                       │                 │                  │
│  drive-sync   (ventana, opcional)     │                 │      Drive       │
│    elige carpetas, muestra el estado  │                 │   (FastAPI)      │
│          │ HTTP, sólo 127.0.0.1       │                 │                  │
│          ▼                            │   HTTP + token  │  /api/sync/tree  │
│  servicio drive-sync  (systemd) ──────┼────────────────►│  /api/files …    │
│    vigila las carpetas y sincroniza   │                 │                  │
│                                       │                 └──────────────────┘
└───────────────────────────────────────┘
```

| | | |
|---|---|---|
| `daemon/` | **servicio** | LuxScript. Sincroniza. Corre con `lux`, sin ventana. |
| `app/` | **ventana** | LuxScript + GTK/WebKit. Sólo mando a distancia del servicio. |
| `deploy/` | instalación | unidad de systemd e instalador |
| `vendor/lux/` | Lux | copia de Lux (ver [Lux vendorizado](#lux-vendorizado)) |

## Instalar

Necesitas `cmake`, `g++` (C++20), `libgtk-3-dev`, `libwebkit2gtk-4.1-dev`,
`libcurl4-openssl-dev`, `libsqlite3-dev` y `sha256sum`.

```bash
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -j$(nproc)            # compila `lux` (servicio) y `drive-sync` (ventana)
sudo ./deploy/install.sh                  # instala y arranca el servicio como tu usuario
drive-sync                                # abre la ventana
```

En la ventana: pega la dirección de Drive y un **token de API** (en la web de
Drive: *Perfil → Tokens de API*, con los ámbitos `read` y `write`), y pulsa
**Añadir carpeta**: eliges la carpeta del equipo y dónde va en Drive.

El servicio arranca con la máquina (`systemctl status drive-sync`, registro en
`journalctl -u drive-sync -f`). Cerrar la ventana no lo para.

## Cómo sincroniza

Cada 5 s mira el disco (barato, sin red). Sólo habla con Drive si hay cambios
locales, si toca el sondeo (cada 60 s) o si pulsas *Sincronizar*. Al arrancar el
equipo la primera pasada pone al día lo que cambió en Drive mientras estuvo
apagado.

Compara tres versiones de cada ruta —lo que hay en disco, lo que hay en Drive y
**lo último que se sincronizó**— y con eso distingue «lo he borrado yo» de «es
nuevo en el otro lado»:

| local | Drive | última vez | qué pasó | qué hace |
|---|---|---|---|---|
| sí | no | no | nuevo en local | sube |
| no | sí | no | nuevo en Drive | baja |
| sí | no | sí | lo borraron en Drive | lo aparta a `.drive-sync-trash/` (o lo sube, si lo cambiaste) |
| no | sí | sí | lo borraste aquí | lo manda a la papelera de Drive (o lo baja, si cambió allí) |
| sí | sí | – | igual en los dos | nada |
| sí | sí | sí | cambió uno | sube o baja |
| sí | sí | – | **cambiaron los dos** | gana la edición más reciente; la otra se conserva |

**Nada se pierde sin rastro.** Un conflicto deja la copia perdedora junto al
fichero como `informe (conflicto 2026-10-02 18-30).pdf`. Al sobrescribir, Drive
archiva la versión anterior. Lo borrado va a la papelera de Drive o a
`.drive-sync-trash/` dentro de la carpeta local. Quitar un enlace no toca ningún
fichero.

**Salvaguardas.** Si una carpeta local aparece de golpe vacía (un disco sin
montar) o Drive devuelve una carpeta vacía que antes tenía ficheros, el servicio
**no toca nada** y avisa, en vez de entenderlo como «los borraron todos».
Un fichero que cambió hace menos de 2 s se deja para la pasada siguiente por si
aún se está escribiendo. Las descargas van a un `.drivepart` y se renombran al
terminar: nunca queda un fichero a medias.

## Límites conocidos

- **Renombrar o mover es «borrar + crear»**: se vuelve a subir. Detectarlo por
  hash evitaría la subida.
- **Sondeo, no inotify**: un cambio local tarda hasta 5 s (más los 2 s de
  margen) en subirse, y uno de Drive hasta 60 s en bajar (`poll_seconds`). Con
  cientos de miles de ficheros convendría un módulo nativo de inotify.
- **120 s por fichero**: Lux limita cada petición HTTP. Un fichero que no suba en
  ese tiempo se reintenta en la pasada siguiente; habría que trocearlo.
- No se conservan permisos ni fechas de modificación al bajar.
- Un fichero que cambia en los dos lados *entre dos pasadas* siempre es conflicto,
  aunque los cambios no se pisen.

## API local del servicio

Sólo en `127.0.0.1:7878`. Exige la cabecera `X-Drive-Sync-Token` con el contenido
de `/var/lib/drive-sync/api-token` (modo 0600, legible sólo por el usuario del
servicio). La ventana la usa; también sirve para scripts:

```
GET    /api/status                     estado, carpetas enlazadas
POST   /api/settings {server_url,token}  prueba y guarda la conexión con Drive
POST   /api/links {local_path,remote_id,remote_name}
DELETE /api/links/:id
POST   /api/links/:id/enabled {enabled}
POST   /api/sync                       «sincronizar ya»
GET    /api/remote/folders?parent_id=  carpetas de Drive
POST   /api/remote/folders {parent_id,name}
GET    /api/events                     actividad reciente
```

Ajustes por entorno (en la unidad): `DRIVE_SYNC_PORT`, `DRIVE_SYNC_HOME`,
`DRIVE_SYNC_DB`. El sondeo de Drive (`poll_seconds`) está en la tabla `settings`.

## Servidor

Usa la API de Drive (`/api/files`, `/api/folders`) más un endpoint añadido para
esto: `GET /api/sync/tree?root_id=…`, que devuelve todo lo que hay bajo una
carpeta —plano, con rutas relativas, tamaño, hash sha256 y fecha— para comparar
de una vez en lugar de recorrerla carpeta a carpeta. Lo borrado (en la papelera)
no aparece, de modo que «ausente» significa «borrado». Las subidas con carpetas
usan el campo `relpath` de `POST /api/files`.

## Lux vendorizado

`vendor/lux/` es una copia del árbol de trabajo de [Lux](../../Lux) en el momento
de vendorizar, sin `bench/`, `editors/` ni `.git`. Se usa sin cambios salvo dos
parches pequeños, ambos para la ventana de escritorio:

1. `src/lux_script/main.cpp`: `main` se llama `lux_main` cuando se compila con
   `-DLUX_EMBEDDED`. La app de escritorio ejecuta así **el mismo arranque de Lux**
   en un hilo (con `every:`, `on start:`, límites…) en lugar de mantener una copia
   propia que se queda anticuada, como pasó con el runtime de Lux-Local.
2. Módulo `window` (`modules/window.cpp`, `window_config.hpp`,
   `window_control.hpp`), copiado de Lux-Local, más `window.open_folder()` (selector
   nativo de carpeta), que Lux-Local no tenía.

Para actualizar Lux: vuelve a copiar su árbol, reaplica esos dos puntos y
compila.

## Pruebas

`tests/e2e.sh` levanta el servicio contra un Drive real y recorre el caso
completo con dos carpetas («dos máquinas») enlazadas a la misma carpeta de Drive:
sincronización inicial, ediciones, borrados, fichero subido desde la web,
conflicto tras reiniciar el servicio y las salvaguardas.

```bash
DRIVE_URL=http://localhost:8000 DRIVE_TOKEN=<token read+write> tests/e2e.sh
```
