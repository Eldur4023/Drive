"""Tareas periódicas de limpieza.

Se ejecutan en un hilo aparte dentro del propio proceso: no hace falta cron ni
un worker externo para una instancia doméstica o de equipo pequeño.
"""

from __future__ import annotations

import logging
import threading
from datetime import timedelta

from sqlalchemy import delete, select

from . import audit, files_service, storage
from .config import get_config
from .database import session_scope
from .models import Node, NodeVersion, Session, User, utcnow

log = logging.getLogger(__name__)


def run_once() -> dict[str, int]:
    """Ejecuta una pasada de limpieza y devuelve lo que ha borrado."""
    config = get_config()
    result = {"trash": 0, "versions": 0, "sessions": 0, "audit": 0, "blobs": 0}

    with session_scope() as db:
        if config.maintenance.purge_trash:
            result["trash"] = files_service.purge_expired_trash(db, config)

        if config.maintenance.purge_versions and config.versions.retention:
            cutoff = utcnow() - timedelta(seconds=config.versions.retention)
            for version in db.scalars(
                select(NodeVersion).where(NodeVersion.created_at < cutoff)
            ).all():
                node = db.get(Node, version.node_id)
                owner = db.get(User, node.owner_id) if node else None
                storage.release_blob(db, version.blob_hash, config)
                if owner is not None:
                    owner.storage_used = max(0, (owner.storage_used or 0) - version.size)
                db.delete(version)
                result["versions"] += 1

        if config.maintenance.purge_sessions:
            outcome = db.execute(
                delete(Session).where(
                    (Session.expires_at < utcnow()) | (Session.revoked.is_(True))
                )
            )
            result["sessions"] = outcome.rowcount or 0

        if config.maintenance.purge_audit:
            result["audit"] = audit.purge(db, config)

        if config.maintenance.purge_orphan_blobs:
            result["blobs"] = storage.purge_orphan_blobs(db, config)

    if any(result.values()):
        log.info("mantenimiento: %s", result)
    return result


def _loop(stop: threading.Event) -> None:
    while not stop.is_set():
        interval = get_config().maintenance.interval or 3600
        # Se espera primero para no competir con el arranque del servidor.
        if stop.wait(interval):
            break
        if not get_config().maintenance.enabled:
            continue
        try:
            run_once()
        except Exception:
            log.exception("fallo en la tarea de mantenimiento")


def start() -> tuple[threading.Thread, threading.Event]:
    stop = threading.Event()
    thread = threading.Thread(target=_loop, args=(stop,), name="drive-maintenance", daemon=True)
    thread.start()
    return thread, stop
