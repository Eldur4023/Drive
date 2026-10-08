"""Sonda de estado para QuemaOS.

Contrato (el mismo que el de Homeflix, `routes/quemaos.lux`):

    GET /quemaos/status  ->  {"quemaos": 1, "id": "drive", "name": "Drive",
                              "status": "ok" | "warn" | "error", "message": "...",
                              "metrics": {...}, "extra": {...}}

Dos reglas, por la misma razón que en Homeflix:

* Va en **su propio servidor HTTP**, en un puerto del rango 9700-9799 y fuera del de la
  API: si los hilos de la API se saturan de tráfico real, la sonda no se queda en cola
  y no da un falso «caído» por simple espera.
* La petición **no toca la base de datos ni el disco**: responde con lo último que midió
  un hilo aparte (cada 30 s). Si ese hilo se para, la propia sonda lo delata.
"""

from __future__ import annotations

import json
import logging
import shutil
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from sqlalchemy import func, select

from .config import Config
from .database import session_scope
from .models import Blob, Node, ShareLink, User

log = logging.getLogger(__name__)

REFRESH_SECONDS = 30
STALE_WARN = 90      # sin medir desde hace tanto: aviso
STALE_ERROR = 300    # ...y esto: error (el hilo de medida ha muerto)


class Probe:
    def __init__(self, config: Config, host: str | None = None, port: int | None = None):
        self.config = config
        self.host = host if host is not None else config.quemaos.host
        self.port = port if port is not None else config.quemaos.port
        self._lock = threading.Lock()
        self._data: dict = {
            "status": "warn", "message": "Arrancando: aún sin medidas",
            "metrics": {}, "extra": {}, "at": 0.0,
        }
        self._stop = threading.Event()
        self._server: ThreadingHTTPServer | None = None
        self._threads: list[threading.Thread] = []

    # -- medida (hilo aparte; la única parte que toca BD y disco) ---------- #

    def measure(self) -> dict:
        root = Path(self.config.storage.root)
        metrics: dict = {}
        extra: dict = {"almacenamiento": str(root)}
        status, message = "ok", ""
        try:
            with session_scope() as db:
                metrics["usuarios"] = db.scalar(select(func.count()).select_from(User)) or 0
                metrics["archivos"] = db.scalar(
                    select(func.count()).select_from(Node).where(
                        Node.is_dir.is_(False), Node.deleted_at.is_(None))) or 0
                metrics["en_papelera"] = db.scalar(
                    select(func.count()).select_from(Node).where(Node.deleted_at.is_not(None))) or 0
                metrics["bytes_guardados"] = db.scalar(select(func.coalesce(func.sum(Blob.size), 0))) or 0
                metrics["enlaces_activos"] = db.scalar(
                    select(func.count()).select_from(ShareLink).where(ShareLink.revoked.is_(False))) or 0
        except Exception as exc:   # noqa: BLE001 - cualquier fallo de BD es «error», no una caída de la sonda
            return {"status": "error", "message": "La base de datos no responde: %s" % exc,
                    "metrics": metrics, "extra": extra, "at": time.time()}

        # El directorio se crea con la primera subida: sin nada guardado todavía, que falte es normal.
        stored = metrics["bytes_guardados"] > 0
        if not root.is_dir() and stored:
            status, message = "error", "No existe el directorio de ficheros: %s" % root
        else:
            probe_dir = root if root.is_dir() else _first_existing(root)
            usage = shutil.disk_usage(probe_dir)
            free_pct = round(100 * usage.free / usage.total, 1) if usage.total else 0
            metrics["disco_libre_pct"] = free_pct
            extra["disco_libre"] = "%s de %s" % (_human(usage.free), _human(usage.total))
            if free_pct < 3:
                status, message = "error", "Disco casi lleno: %s %% libre" % free_pct
            elif free_pct < 10:
                status, message = "warn", "Poco espacio en disco: %s %% libre" % free_pct
        if not message:
            message = "%d usuarios · %d archivos · %s" % (
                metrics["usuarios"], metrics["archivos"], _human(metrics["bytes_guardados"]))
        return {"status": status, "message": message, "metrics": metrics, "extra": extra, "at": time.time()}

    def refresh(self) -> None:
        data = self.measure()
        with self._lock:
            self._data = data

    # -- respuesta (sin tocar nada más que la caché) ----------------------- #

    def payload(self) -> dict:
        with self._lock:
            d = dict(self._data)
        status, message = d["status"], d["message"]
        age = time.time() - d["at"] if d["at"] else None
        if age is not None and age > STALE_ERROR:
            status, message = "error", "Sin medidas desde hace %d min: el hilo de medida se ha parado" % (age // 60)
        elif age is not None and age > STALE_WARN and status == "ok":
            status, message = "warn", "Medidas desactualizadas (hace %d s)" % age
        return {
            "quemaos": 1, "id": "drive", "name": "Drive", "status": status, "message": message,
            "metrics": d["metrics"], "extra": d["extra"],
        }

    # -- ciclo de vida ----------------------------------------------------- #

    def start(self) -> bool:
        """Abre el listener y arranca la medida. False si el puerto ya está ocupado
        (con varios workers sólo el primero la sirve)."""
        probe = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                if self.path.split("?")[0].rstrip("/") == "/quemaos/status":
                    body = json.dumps(probe.payload(), ensure_ascii=False).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Cache-Control", "no-store")
                else:
                    body = b'{"detail":"not found"}'
                    self.send_response(404)
                    self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):  # sin ruido en el registro
                pass

        class Server(ThreadingHTTPServer):
            daemon_threads = True
            allow_reuse_address = False   # si otro worker ya la tiene, que falle el bind

        try:
            self._server = Server((self.host, self.port), Handler)
        except OSError as exc:
            log.info("sonda QuemaOS: %s:%s no disponible (%s); probablemente la sirve otro worker", self.host, self.port, exc)
            return False
        self.port = self._server.server_address[1]
        self.refresh()
        for target, name in ((self._server.serve_forever, "quemaos-http"), (self._loop, "quemaos-medida")):
            t = threading.Thread(target=target, name=name, daemon=True)
            t.start()
            self._threads.append(t)
        log.info("sonda QuemaOS en http://%s:%s/quemaos/status", self.host, self.port)
        return True

    def _loop(self) -> None:
        while not self._stop.wait(REFRESH_SECONDS):
            try:
                self.refresh()
            except Exception:   # noqa: BLE001
                log.exception("sonda QuemaOS: fallo al medir")

    def stop(self) -> None:
        self._stop.set()
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        for t in self._threads:
            t.join(timeout=3)


def _first_existing(path: Path) -> Path:
    while not path.exists() and path != path.parent:
        path = path.parent
    return path


def _human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return ("%d %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024
    return "%d B" % n


def start(config: Config) -> Probe | None:
    if not config.quemaos.enabled:
        return None
    probe = Probe(config)
    return probe if probe.start() else None
