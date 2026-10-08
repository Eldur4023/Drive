"""Operaciones sobre el árbol de ficheros.

Toda mutación pasa por aquí para que cuota, versiones, papelera y contadores
de blobs se mantengan coherentes en un único sitio.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import timedelta
from typing import BinaryIO, Iterable

from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from . import storage
from .config import Config, get_config
from .models import Blob, Node, NodeVersion, Share, ShareLink, User, utcnow
from .storage import QuotaExceeded, StorageError

log = logging.getLogger(__name__)

MAX_TREE_DEPTH = 64


@dataclass
class Usage:
    used: int
    quota: int | None

    @property
    def unlimited(self) -> bool:
        return self.quota is None

    @property
    def free(self) -> int | None:
        return None if self.quota is None else max(0, self.quota - self.used)

    @property
    def percent(self) -> float:
        if self.quota is None or self.quota == 0:
            return 0.0
        return min(100.0, round(self.used * 100 / self.quota, 1))


# --------------------------------------------------------------------------- #
# Cuota
# --------------------------------------------------------------------------- #


def quota_of(user: User, config: Config | None = None) -> int | None:
    """Cuota efectiva: override individual > perfil del rol > default global."""
    config = config or get_config()
    if user.quota_override is not None:
        return user.quota_override or None
    role_quota = config.role(user.role.value).quota
    if role_quota is not None:
        return role_quota
    return config.storage.default_quota


def max_upload_of(user: User, config: Config | None = None) -> int | None:
    config = config or get_config()
    if user.max_upload_override is not None:
        return user.max_upload_override or None
    role_max = config.role(user.role.value).max_upload_size
    if role_max is not None:
        return role_max
    return config.storage.max_upload_size


def usage_of(user: User, config: Config | None = None) -> Usage:
    return Usage(used=user.storage_used or 0, quota=quota_of(user, config))


def recompute_usage(db: DbSession, user: User, config: Config | None = None) -> int:
    """Recalcula el espacio ocupado recorriendo los nodos del usuario."""
    config = config or get_config()
    condition = [Node.owner_id == user.id, Node.is_dir.is_(False)]
    if not config.trash.counts_against_quota:
        condition.append(Node.deleted_at.is_(None))

    total = db.scalar(select(func.coalesce(func.sum(Node.size), 0)).where(*condition)) or 0
    if config.versions.enabled:
        total += (
            db.scalar(
                select(func.coalesce(func.sum(NodeVersion.size), 0))
                .join(Node, Node.id == NodeVersion.node_id)
                .where(Node.owner_id == user.id)
            )
            or 0
        )
    user.storage_used = int(total)
    return user.storage_used


def _charge(user: User, delta: int) -> None:
    user.storage_used = max(0, (user.storage_used or 0) + delta)


def ensure_quota(user: User, extra: int, config: Config | None = None) -> None:
    quota = quota_of(user, config)
    if quota is None:
        return
    if (user.storage_used or 0) + extra > quota:
        raise QuotaExceeded(
            f"No hay espacio suficiente: quedan "
            f"{storage.format_size(max(0, quota - (user.storage_used or 0)))} "
            f"de {storage.format_size(quota)}."
        )


# --------------------------------------------------------------------------- #
# Consulta
# --------------------------------------------------------------------------- #


def children_of(
    db: DbSession,
    owner_id: str,
    parent_id: str | None,
    *,
    include_trashed: bool = False,
    order: str = "name",
) -> list[Node]:
    query = select(Node).where(Node.owner_id == owner_id, Node.parent_id == parent_id)
    if not include_trashed:
        query = query.where(Node.deleted_at.is_(None))

    columns = {
        "name": Node.name,
        "size": Node.size.desc(),
        "modified": Node.updated_at.desc(),
        "created": Node.created_at.desc(),
    }
    # Las carpetas primero, como en cualquier explorador.
    return list(db.scalars(query.order_by(Node.is_dir.desc(), columns.get(order, Node.name))))


def folder_sizes(db: DbSession, owner_id: str) -> dict[str, int]:
    """Tamaño de cada carpeta del usuario: la suma de lo vivo que cuelga de ella."""
    # ponytail: lee todos los nodos del usuario en cada llamada; con cientos de
    # miles de ficheros convendría guardar el total en la carpeta al subir/borrar.
    rows = db.execute(
        select(Node.id, Node.parent_id, Node.size, Node.is_dir)
        .where(Node.owner_id == owner_id, Node.deleted_at.is_(None))
    ).all()
    parent = {r.id: r.parent_id for r in rows}
    sizes = {r.id: 0 for r in rows if r.is_dir}
    for r in rows:
        if r.is_dir or not r.size:
            continue
        p = r.parent_id
        while p is not None and p in sizes:
            sizes[p] += r.size
            p = parent[p]
    return sizes


def trashed_of(db: DbSession, owner_id: str) -> list[Node]:
    """Elementos en la papelera cuyo padre no esté también en la papelera."""
    rows = db.scalars(
        select(Node)
        .where(Node.owner_id == owner_id, Node.deleted_at.is_not(None))
        .order_by(Node.deleted_at.desc())
    ).all()
    trashed_ids = {n.id for n in rows}
    return [n for n in rows if n.trashed_from_id not in trashed_ids]


def search(
    db: DbSession, owner_id: str, term: str, limit: int = 200
) -> list[Node]:
    pattern = f"%{term.strip()}%"
    return list(
        db.scalars(
            select(Node)
            .where(
                Node.owner_id == owner_id,
                Node.deleted_at.is_(None),
                Node.name.ilike(pattern),
            )
            .order_by(Node.is_dir.desc(), Node.name)
            .limit(limit)
        )
    )


def shared_with(db: DbSession, user_id: str) -> list[Node]:
    """Nodos que otros han compartido con este usuario y siguen vigentes."""
    now = utcnow()
    shares = db.scalars(select(Share).where(Share.user_id == user_id)).all()
    nodes: list[Node] = []
    for share in shares:
        if share.expires_at is not None and share.expires_at <= now:
            continue
        node = db.get(Node, share.node_id)
        if node is not None and node.deleted_at is None:
            nodes.append(node)
    return nodes


def descendants(db: DbSession, node: Node, depth: int = 0) -> Iterable[Node]:
    """Recorrido en profundidad de todo lo que cuelga de un nodo."""
    if depth > MAX_TREE_DEPTH:
        return
    for child in db.scalars(select(Node).where(Node.parent_id == node.id)):
        yield child
        if child.is_dir:
            yield from descendants(db, child, depth + 1)


def folder_size(db: DbSession, node: Node) -> int:
    return sum(n.size for n in descendants(db, node) if not n.is_dir)


# --------------------------------------------------------------------------- #
# Nombres
# --------------------------------------------------------------------------- #

_SUFFIX_RE = re.compile(r"^(?P<stem>.*?)(?: \((?P<n>\d+)\))?$")


def unique_name(
    db: DbSession,
    owner_id: str,
    parent_id: str | None,
    name: str,
    exclude_id: str | None = None,
) -> str:
    """Devuelve ``name`` o ``name (2)`` si ya existe un hermano vivo igual.

    ``exclude_id`` es el nodo que se renombra o mueve: no cuenta como hermano de
    sí mismo (si no, al moverlo se choca con su propia fila ya reubicada).
    """
    query = select(Node).where(
        Node.owner_id == owner_id,
        Node.parent_id == parent_id,
        Node.deleted_at.is_(None),
    )
    if exclude_id is not None:
        query = query.where(Node.id != exclude_id)
    taken = {n.name for n in db.scalars(query)}
    if name not in taken:
        return name

    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    base = _SUFFIX_RE.match(stem)
    stem = base.group("stem") if base else stem

    for index in range(2, 1000):
        candidate = f"{stem} ({index}){'.' + ext if dot else ''}"
        if candidate not in taken:
            return candidate
    raise StorageError("Demasiados ficheros con ese nombre en la carpeta.")


# --------------------------------------------------------------------------- #
# Mutaciones
# --------------------------------------------------------------------------- #


def create_folder(
    db: DbSession, owner: User, parent: Node | None, name: str
) -> Node:
    name = storage.sanitize_name(name)
    node = Node(
        owner_id=owner.id,
        parent_id=parent.id if parent else None,
        name=unique_name(db, owner.id, parent.id if parent else None, name),
        is_dir=True,
    )
    db.add(node)
    db.flush()
    return node


def ensure_folders(
    db: DbSession, owner: User, parent: Node | None, relpath: str
) -> Node | None:
    """Crea (o reutiliza) las carpetas de ``relpath`` bajo ``parent``.

    ``relpath`` es la ruta relativa de un fichero subido desde una carpeta
    (``fotos/2024/a.jpg``): se crean ``fotos`` y ``2024``; el último segmento es
    el nombre del fichero y se ignora. Devuelve la carpeta donde debe ir el fichero.
    """
    parts = [p for p in relpath.replace("\\", "/").split("/")[:-1] if p not in ("", ".", "..")]
    if len(parts) > 32:
        raise StorageError("La ruta de la carpeta es demasiado profunda.")
    for part in parts:
        name = storage.sanitize_name(part)
        child = db.scalar(
            select(Node).where(
                Node.owner_id == owner.id,
                Node.parent_id == (parent.id if parent else None),
                Node.name == name,
                Node.deleted_at.is_(None),
                Node.is_dir.is_(True),
            )
        )
        parent = child or create_folder(db, owner, parent, name)
    return parent


def save_upload(
    db: DbSession,
    owner: User,
    parent: Node | None,
    filename: str,
    source: BinaryIO,
    *,
    overwrite: bool = False,
    config: Config | None = None,
) -> Node:
    """Guarda un fichero subido, versionando si ya existía uno con ese nombre."""
    config = config or get_config()
    filename = storage.sanitize_name(filename)
    storage.check_filename(filename, config)

    parent_id = parent.id if parent else None
    existing = db.scalar(
        select(Node).where(
            Node.owner_id == owner.id,
            Node.parent_id == parent_id,
            Node.name == filename,
            Node.deleted_at.is_(None),
            Node.is_dir.is_(False),
        )
    )

    limit = max_upload_of(owner, config)
    stored = storage.write_stream(source, max_size=limit, config=config)

    # Mismo contenido que ya hay: nada que versionar ni que cobrar. El cliente
    # de escritorio resube ficheros sin cambios y cada vuelta sumaba una versión.
    if existing is not None and overwrite and existing.blob_hash == stored.hash:
        return existing

    # El coste para la cuota es el tamaño nuevo menos el que se reemplaza, y
    # sólo si la versión antigua no se conserva.
    replaced_size = 0
    if existing is not None and overwrite and not config.versions.enabled:
        replaced_size = existing.size
    try:
        ensure_quota(owner, stored.size - replaced_size, config)
    except QuotaExceeded:
        # El blob puede haber quedado escrito sin dueño; se recogerá en el
        # mantenimiento, pero si acabamos de crearlo lo borramos ya.
        if stored.created and db.get(Blob, stored.hash) is None:
            storage.blob_path(stored.hash, config).unlink(missing_ok=True)
        raise

    storage.acquire_blob(db, stored)

    if existing is not None and overwrite:
        _archive_version(db, existing, owner, config)
        if not config.versions.enabled:
            storage.release_blob(db, existing.blob_hash, config)
            _charge(owner, -existing.size)
        existing.blob_hash = stored.hash
        existing.size = stored.size
        existing.mime_type = storage.guess_mime(filename)
        existing.updated_at = utcnow()
        _charge(owner, stored.size)
        db.flush()
        return existing

    node = Node(
        owner_id=owner.id,
        parent_id=parent_id,
        name=unique_name(db, owner.id, parent_id, filename),
        is_dir=False,
        blob_hash=stored.hash,
        size=stored.size,
        mime_type=storage.guess_mime(filename),
    )
    db.add(node)
    _charge(owner, stored.size)
    db.flush()
    return node


def _archive_version(
    db: DbSession, node: Node, owner: User, config: Config
) -> None:
    """Conserva el contenido actual como versión antes de sobreescribirlo."""
    if not config.versions.enabled or not node.blob_hash:
        return

    version = NodeVersion(
        node_id=node.id,
        blob_hash=node.blob_hash,
        size=node.size,
        created_by=owner.id,
    )
    db.add(version)
    # La versión se queda con la referencia que tenía el nodo, así que no hace
    # falta tocar el refcount aquí: el nodo adquiere una nueva más adelante.
    db.flush()
    _prune_versions(db, node, owner, config)


def _prune_versions(db: DbSession, node: Node, owner: User, config: Config) -> None:
    versions = list(
        db.scalars(
            select(NodeVersion)
            .where(NodeVersion.node_id == node.id)
            .order_by(NodeVersion.created_at.desc())
        )
    )
    cutoff = (
        utcnow() - timedelta(seconds=config.versions.retention)
        if config.versions.retention
        else None
    )
    for index, version in enumerate(versions):
        too_many = index >= config.versions.max_per_file
        too_old = cutoff is not None and version.created_at < cutoff
        if too_many or too_old:
            storage.release_blob(db, version.blob_hash, config)
            _charge(owner, -version.size)
            db.delete(version)


def restore_version(
    db: DbSession, node: Node, version: NodeVersion, owner: User, config: Config | None = None
) -> Node:
    """Vuelve a una versión anterior, archivando la actual."""
    config = config or get_config()
    _archive_version(db, node, owner, config)
    node.blob_hash = version.blob_hash
    node.size = version.size
    node.updated_at = utcnow()
    db.delete(version)
    db.flush()
    return node


def rename(db: DbSession, node: Node, new_name: str) -> Node:
    name = storage.sanitize_name(new_name)
    if not node.is_dir:
        storage.check_filename(name)
    node.name = unique_name(db, node.owner_id, node.parent_id, name, node.id)
    node.updated_at = utcnow()
    db.flush()
    return node


def move(db: DbSession, node: Node, new_parent: Node | None) -> Node:
    if new_parent is not None:
        if not new_parent.is_dir:
            raise StorageError("El destino no es una carpeta.")
        if new_parent.id == node.id:
            raise StorageError("No se puede mover una carpeta dentro de sí misma.")
        # Impide crear un ciclo moviendo una carpeta a su propio subárbol.
        cursor: Node | None = new_parent
        for _ in range(MAX_TREE_DEPTH):
            if cursor is None:
                break
            if cursor.id == node.id:
                raise StorageError("No se puede mover una carpeta dentro de sí misma.")
            cursor = db.get(Node, cursor.parent_id) if cursor.parent_id else None

    node.parent_id = new_parent.id if new_parent else None
    node.name = unique_name(db, node.owner_id, node.parent_id, node.name, node.id)
    node.updated_at = utcnow()
    db.flush()
    return node


def copy(
    db: DbSession,
    node: Node,
    new_parent: Node | None,
    owner: User,
    config: Config | None = None,
    depth: int = 0,
) -> Node:
    """Copia un nodo (y su subárbol) reutilizando los blobs originales."""
    config = config or get_config()
    if depth > MAX_TREE_DEPTH:
        raise StorageError("La carpeta tiene demasiados niveles.")

    if not node.is_dir:
        ensure_quota(owner, node.size, config)

    parent_id = new_parent.id if new_parent else None
    clone = Node(
        owner_id=owner.id,
        parent_id=parent_id,
        name=unique_name(db, owner.id, parent_id, node.name),
        is_dir=node.is_dir,
        blob_hash=node.blob_hash,
        size=node.size,
        mime_type=node.mime_type,
        description=node.description,
    )
    db.add(clone)
    db.flush()

    if not node.is_dir and node.blob_hash:
        blob = db.get(Blob, node.blob_hash)
        if blob is not None:
            blob.refcount += 1
        _charge(owner, node.size)

    if node.is_dir:
        for child in db.scalars(
            select(Node).where(Node.parent_id == node.id, Node.deleted_at.is_(None))
        ):
            copy(db, child, clone, owner, config, depth + 1)

    return clone


def trash(db: DbSession, node: Node, config: Config | None = None) -> None:
    """Envía a la papelera (o borra del todo si la papelera está desactivada)."""
    config = config or get_config()
    if not config.trash.enabled:
        purge(db, node, config)
        return

    now = utcnow()
    node.trashed_from_id = node.parent_id
    node.deleted_at = now
    for child in descendants(db, node):
        if child.deleted_at is None:
            child.trashed_from_id = child.parent_id
            child.deleted_at = now

    if not config.trash.counts_against_quota:
        owner = db.get(User, node.owner_id)
        if owner is not None:
            _charge(owner, -(node.size if not node.is_dir else folder_size(db, node)))
    db.flush()


def restore(db: DbSession, node: Node, config: Config | None = None) -> Node:
    config = config or get_config()
    # Si el padre original ya no existe o está en la papelera, vuelve a la raíz.
    parent = db.get(Node, node.trashed_from_id) if node.trashed_from_id else None
    if parent is not None and parent.deleted_at is not None:
        parent = None

    node.parent_id = parent.id if parent else None
    node.name = unique_name(db, node.owner_id, node.parent_id, node.name, node.id)
    node.deleted_at = None
    node.trashed_from_id = None
    for child in descendants(db, node):
        child.deleted_at = None
        child.trashed_from_id = None

    if not config.trash.counts_against_quota:
        owner = db.get(User, node.owner_id)
        if owner is not None:
            size = node.size if not node.is_dir else folder_size(db, node)
            ensure_quota(owner, size, config)
            _charge(owner, size)
    db.flush()
    return node


def purge(db: DbSession, node: Node, config: Config | None = None) -> None:
    """Borrado definitivo: libera blobs, versiones y comparticiones."""
    config = config or get_config()
    owner = db.get(User, node.owner_id)
    counts_now = config.trash.counts_against_quota or node.deleted_at is None

    for target in list(descendants(db, node)) + [node]:
        for version in db.scalars(
            select(NodeVersion).where(NodeVersion.node_id == target.id)
        ):
            storage.release_blob(db, version.blob_hash, config)
            if owner is not None:
                _charge(owner, -version.size)
            db.delete(version)

        if not target.is_dir:
            storage.release_blob(db, target.blob_hash, config)
            if owner is not None and counts_now:
                _charge(owner, -target.size)

        for share in db.scalars(select(Share).where(Share.node_id == target.id)):
            db.delete(share)
        for link in db.scalars(select(ShareLink).where(ShareLink.node_id == target.id)):
            db.delete(link)

    db.delete(node)
    db.flush()


def empty_trash(db: DbSession, owner_id: str, config: Config | None = None) -> int:
    count = 0
    for node in trashed_of(db, owner_id):
        purge(db, node, config)
        count += 1
    return count


def purge_expired_trash(db: DbSession, config: Config | None = None) -> int:
    """Vacía de la papelera lo que haya superado la retención."""
    config = config or get_config()
    if not config.trash.enabled or not config.trash.retention:
        return 0
    cutoff = utcnow() - timedelta(seconds=config.trash.retention)
    count = 0
    rows = db.scalars(
        select(Node).where(Node.deleted_at.is_not(None), Node.deleted_at < cutoff)
    ).all()
    trashed_ids = {n.id for n in rows}
    for node in rows:
        # Sólo las raíces del borrado; los hijos caen con ellas.
        if node.trashed_from_id in trashed_ids:
            continue
        purge(db, node, config)
        count += 1
    return count
