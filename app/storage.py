"""Almacenamiento físico de los ficheros.

El contenido vive fuera del árbol lógico: cada fichero se guarda una sola vez
bajo ``<root>/ab/cd/<hash>`` y la jerarquía de carpetas existe únicamente en la
base de datos. Así los nombres que escribe el usuario nunca llegan al sistema
de ficheros, no hay path traversal posible, y dos copias del mismo contenido
comparten un solo blob.
"""

from __future__ import annotations

import hashlib
import logging
import mimetypes
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from .config import Config, get_config
from .models import Blob

log = logging.getLogger(__name__)

CHUNK_SIZE = 1024 * 1024


class StorageError(Exception):
    """Error recuperable de almacenamiento, con mensaje apto para el usuario."""


class QuotaExceeded(StorageError):
    pass


class FileTooLarge(StorageError):
    pass


class ExtensionNotAllowed(StorageError):
    pass


@dataclass
class StoredBlob:
    hash: str
    size: int
    #: False si el contenido ya existía y se ha reutilizado.
    created: bool


# --------------------------------------------------------------------------- #
# Rutas
# --------------------------------------------------------------------------- #


def storage_root(config: Config | None = None) -> Path:
    config = config or get_config()
    root = Path(config.storage.root).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    return root


def blob_path(digest: str, config: Config | None = None) -> Path:
    root = storage_root(config)
    return root / digest[:2] / digest[2:4] / digest


def thumb_path(digest: str, size: int, config: Config | None = None) -> Path:
    root = storage_root(config).parent / "thumbs" / str(size)
    return root / digest[:2] / digest[2:4] / f"{digest}.webp"


# --------------------------------------------------------------------------- #
# Validación previa
# --------------------------------------------------------------------------- #


def guess_mime(filename: str) -> str:
    return mimetypes.guess_type(filename)[0] or "application/octet-stream"


def check_filename(filename: str, config: Config | None = None) -> None:
    """Aplica las listas negra y blanca de extensiones y de tipos MIME."""
    config = config or get_config()
    storage = config.storage
    ext = Path(filename).suffix.lower()

    if storage.allowed_extensions and ext not in storage.allowed_extensions:
        raise ExtensionNotAllowed(
            f"Sólo se admiten estos tipos de fichero: "
            f"{', '.join(storage.allowed_extensions)}"
        )
    if ext in storage.blocked_extensions:
        raise ExtensionNotAllowed(f"Los ficheros «{ext}» no están permitidos.")
    if storage.blocked_mime_types and guess_mime(filename) in storage.blocked_mime_types:
        raise ExtensionNotAllowed("El tipo de este fichero no está permitido.")


def sanitize_name(name: str) -> str:
    """Deja un nombre presentable y sin separadores de ruta.

    No protege el sistema de ficheros —los blobs ya son opacos— pero evita
    nombres que confundirían al descargar (``../``, saltos de línea, nulos).
    """
    name = name.replace("\\", "/").split("/")[-1]
    name = "".join(c for c in name if c.isprintable() and c not in '\r\n\t\0')
    name = name.strip().strip(".")
    return (name or "sin-nombre")[:255]


# --------------------------------------------------------------------------- #
# Escritura
# --------------------------------------------------------------------------- #


def write_stream(
    source: BinaryIO,
    *,
    max_size: int | None = None,
    config: Config | None = None,
) -> StoredBlob:
    """Vuelca un flujo a disco calculando su hash sobre la marcha.

    Se escribe primero a un temporal en el mismo sistema de ficheros para que
    el movimiento final sea atómico: nunca queda un blob a medias con un hash
    que dice estar completo.
    """
    config = config or get_config()
    root = storage_root(config)
    digest = hashlib.new(config.storage.hash_algorithm)
    size = 0

    tmp_dir = root / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=tmp_dir, prefix="up-")
    tmp = Path(tmp_name)

    try:
        with os.fdopen(fd, "wb") as out:
            while True:
                chunk = source.read(CHUNK_SIZE)
                if not chunk:
                    break
                size += len(chunk)
                if max_size is not None and size > max_size:
                    raise FileTooLarge(
                        f"El fichero supera el máximo permitido "
                        f"({format_size(max_size)})."
                    )
                digest.update(chunk)
                out.write(chunk)

        hexdigest = digest.hexdigest()
        target = blob_path(hexdigest, config)
        if target.exists():
            tmp.unlink(missing_ok=True)
            return StoredBlob(hash=hexdigest, size=size, created=False)

        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(tmp), str(target))
        target.chmod(0o640)
        return StoredBlob(hash=hexdigest, size=size, created=True)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def acquire_blob(db: Session, stored: StoredBlob) -> Blob:
    """Registra el blob y suma una referencia.

    Es un upsert atómico: leer y luego insertar hacía que dos subidas con el
    mismo contenido a la vez chocaran con la clave única de ``blobs.hash``.
    """
    insert = pg_insert if db.get_bind().dialect.name == "postgresql" else sqlite_insert
    db.execute(
        insert(Blob)
        .values(hash=stored.hash, size=stored.size, refcount=1)
        .on_conflict_do_update(
            index_elements=[Blob.hash], set_={"refcount": Blob.refcount + 1}
        )
    )
    blob = db.get(Blob, stored.hash)
    assert blob is not None
    db.refresh(blob)  # el contador lo ha cambiado SQL, no el objeto en memoria
    return blob


def release_blob(db: Session, digest: str | None, config: Config | None = None) -> None:
    """Resta una referencia y borra el contenido cuando ya no lo usa nadie.

    El fichero se elimina en el acto, pero la fila se deja con ``refcount`` a
    cero para que la recoja :func:`purge_orphan_blobs`. Borrarla aquí obligaría
    a ordenar el flush respecto a los nodos que todavía la referencian, y una
    fila huérfana durante unos minutos no ocupa nada.
    """
    if not digest:
        return
    blob = db.get(Blob, digest)
    if blob is None:
        return
    blob.refcount = Blob.refcount - 1  # en SQL, para no perder cuentas en paralelo
    db.flush()
    db.refresh(blob)
    if blob.refcount <= 0:
        try:
            blob_path(digest, config).unlink(missing_ok=True)
        except OSError:
            log.warning("no se pudo borrar el blob %s", digest, exc_info=True)
        for size in (config or get_config()).thumbnails.sizes:
            thumb_path(digest, size, config).unlink(missing_ok=True)


def purge_orphan_blobs(db: Session, config: Config | None = None) -> int:
    """Elimina las filas de blobs que ya no referencia nadie.

    Recoge tanto lo liberado por :func:`release_blob` como lo que haya quedado
    a medias si un proceso murió durante una subida.
    """
    removed = 0
    for blob in db.scalars(select(Blob).where(Blob.refcount <= 0)).all():
        blob_path(blob.hash, config).unlink(missing_ok=True)
        db.delete(blob)
        removed += 1
    return removed


# --------------------------------------------------------------------------- #
# Lectura
# --------------------------------------------------------------------------- #


def open_blob(digest: str, config: Config | None = None) -> BinaryIO:
    path = blob_path(digest, config)
    if not path.is_file():
        raise StorageError("El contenido de este fichero no está disponible.")
    return path.open("rb")


def iter_blob(
    digest: str,
    start: int = 0,
    end: int | None = None,
    config: Config | None = None,
):
    """Generador de trozos, con soporte de rangos para vídeo y reanudación."""
    path = blob_path(digest, config)
    if not path.is_file():
        raise StorageError("El contenido de este fichero no está disponible.")

    remaining = None if end is None else (end - start + 1)
    with path.open("rb") as handle:
        handle.seek(start)
        while True:
            to_read = CHUNK_SIZE if remaining is None else min(CHUNK_SIZE, remaining)
            if to_read <= 0:
                break
            chunk = handle.read(to_read)
            if not chunk:
                break
            if remaining is not None:
                remaining -= len(chunk)
            yield chunk


def parse_range(header: str | None, size: int) -> tuple[int, int] | None:
    """Interpreta ``Range: bytes=a-b``. Devuelve ``None`` si no aplica."""
    if not header or not header.startswith("bytes=") or size == 0:
        return None
    spec = header[6:].split(",")[0].strip()
    start_s, _, end_s = spec.partition("-")
    try:
        if not start_s:
            # Sufijo: los últimos N bytes.
            length = int(end_s)
            if length <= 0:
                return None
            return max(0, size - length), size - 1
        start = int(start_s)
        end = int(end_s) if end_s else size - 1
    except ValueError:
        return None
    if start >= size or start > end:
        return None
    return start, min(end, size - 1)


# --------------------------------------------------------------------------- #
# Utilidades
# --------------------------------------------------------------------------- #


def format_size(num: int | None) -> str:
    if num is None:
        return "sin límite"
    step = 1024.0
    value = float(num)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < step or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= step
    return f"{value:.1f} TB"


def disk_usage(config: Config | None = None) -> tuple[int, int]:
    """Espacio usado y total del sistema de ficheros de los blobs."""
    usage = shutil.disk_usage(storage_root(config))
    return usage.used, usage.total
